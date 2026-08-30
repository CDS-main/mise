"""Recipe import and adaptation.

Tiered on purpose, cheapest and most reliable first:

  1. schema.org/Recipe JSON-LD   — most real recipe sites embed it. Perfect
                                   structured data, zero model calls, zero cost.
  2. YouTube auto-subtitles      — yt-dlp, transcript only, no video download.
  3. Page text                   — strip tags, hand to the model.
  4. Pasted text                 — hand to the model.
  5. Regex fallback              — no API key configured? still works, badly.

The model NEVER returns pantry ids, gram conversions, or scale factors. It
returns names and amounts as written; Python does the unit conversion and the
pantry matching. Anything numeric that ends up in your logs came from code.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import unicodedata
from difflib import SequenceMatcher
from typing import Any

import httpx

from .models import (ChatResponse, DraftIngredient, DraftStage, Proposal,
                     RecipeDraft, RecipeDraftSet)

UA = "Mozilla/5.0 (compatible; MiseKitchen/1.0; +local)"
TIMEOUT = 20.0

# ── unit conversion (deterministic, never the model's job) ──────────────────
TO_BASE = {
    "g": 1, "gram": 1, "grams": 1, "gr": 1,
    "kg": 1000, "kilo": 1000, "kilogram": 1000,
    "ml": 1, "milliliter": 1, "millilitre": 1,
    "l": 1000, "liter": 1000, "litre": 1000,
    "tbsp": 15, "tablespoon": 15, "tablespoons": 15, "el": 15,
    "tsp": 5, "teaspoon": 5, "teaspoons": 5, "tl": 5,
    "cup": 240, "cups": 240,
    "oz": 28.35, "ounce": 28.35, "lb": 453.6, "pound": 453.6,
    "ea": 1, "x": 1, "": 1,
}
COUNT_UNITS = {"ea", "x", "",
               "stalk", "stalks", "clove", "cloves", "slice", "slices",
               "piece", "pieces", "can", "cans", "tin", "tins",
               "sprig", "sprigs", "bunch", "bunches"}


def to_base(amt: float, unit: str) -> tuple[float, str]:
    u = (unit or "").strip().lower().rstrip(".")
    mult = TO_BASE.get(u, 1)
    base_unit = "ea" if u in COUNT_UNITS else ("ml" if u in {"ml", "l", "liter", "litre", "milliliter", "millilitre"} else "g")
    return round(amt * mult, 2), base_unit


# ── pantry matching (deterministic) ─────────────────────────────────────────
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\b(fresh|dried|ground|chopped|sliced|minced|large|small|medium|whole|of|the|a|an)\b", " ", s)
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def match_pantry(name: str, pantry: dict[str, dict[str, Any]]) -> tuple[str | None, float]:
    """Return (pantry_id, score 0-1). Exact token overlap beats fuzzy ratio."""
    n = _norm(name)
    if not n:
        return None, 0.0
    ntok = set(n.split())
    best, best_score = None, 0.0
    for pid, item in pantry.items():
        cands = [item.get("name", ""), item.get("nl", ""), item.get("tag", "")]
        for cand in cands:
            c = _norm(cand)
            if not c:
                continue
            ctok = set(c.split())
            overlap = len(ntok & ctok) / max(1, len(ctok))
            ratio = SequenceMatcher(None, n, c).ratio()
            score = max(overlap * 0.95, ratio)
            if c and (c in n or n in c):
                score = max(score, 0.9)
            if score > best_score:
                best, best_score = pid, score
    return (best, round(best_score, 3)) if best_score >= 0.55 else (None, round(best_score, 3))


# ── tier 1: JSON-LD ─────────────────────────────────────────────────────────
def _walk_all_recipes(node: Any, seen: list[int] | None = None) -> list[dict]:
    """Every schema.org Recipe on the page, in document order.

    Round-up and listicle pages ("12 weeknight pastas") embed one Recipe block
    per dish. Taking only the first is how you silently import the wrong one.
    """
    seen = [] if seen is None else seen
    out: list[dict] = []
    if isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(str(x).lower() == "recipe" for x in types if x):
            if id(node) not in seen:
                seen.append(id(node))
                out.append(node)
            return out
        for v in node.values():
            out += _walk_all_recipes(v, seen)
    elif isinstance(node, list):
        for v in node:
            out += _walk_all_recipes(v, seen)
    return out


ISO_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?")


def _iso_minutes(s: str | None) -> int:
    if not s:
        return 0
    m = ISO_DUR.match(str(s))
    if not m:
        return 0
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


# Recipe writers use ½ and ¼ constantly and they are not digits, so a bare \d
# regex silently drops those lines — which is how "½ cup dashi" vanishes from an
# import and you don't notice until the pan is dry.
VULGAR = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4",
          "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5", "⅙": "1/6",
          "⅚": "5/6", "⅐": "1/7", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8",
          "⅞": "7/8", "⅑": "1/9", "⅒": "1/10", "−": "-", "–": "-"}


def _defraction(line: str) -> str:
    for k, v in VULGAR.items():
        line = line.replace(k, v)
    # "1 1/2 cups" -> "1.5 cups"
    line = re.sub(r"\b(\d+)\s+(\d+)/(\d+)\b",
                  lambda m: str(round(int(m.group(1)) + int(m.group(2)) / int(m.group(3)), 4)), line)
    return line


QTY_LINE = re.compile(
    r"^\s*(?:[-*•]\s*)?(\d+(?:[.,/]\d+)?)\s*"
    r"(kg|g|gram[s]?|ml|l|liter|litre|tbsp|tablespoon[s]?|tsp|teaspoon[s]?|cups?|oz|lb|"
    r"stalks?|cloves?|slices?|pieces?|cans?|tins?|sprigs?|bunch(?:es)?|el|tl)?"
    r"(?=\s|$)\s*"
    r"(.{2,120})$", re.I)


def _parse_ing_line(line: str) -> DraftIngredient | None:
    line = _defraction(line.strip())
    m = QTY_LINE.match(line)
    if not m:
        return None
    raw_amt = m.group(1).replace(",", ".")
    try:
        if "/" in raw_amt:
            num, den = raw_amt.split("/", 1)
            amt = float(num) / float(den)
        else:
            amt = float(raw_amt)
    except Exception:
        return None
    if amt <= 0:
        return None
    name = m.group(3).strip(" ,.")
    # Drop the parenthetical the writer added for the reader, not the scale:
    # "White Onion, sliced (about 1/4 cup)" is an onion, not a cup.
    name = re.sub(r"\s*\([^)]*\)", "", name).strip(" ,.")
    return DraftIngredient(raw=line, name=name or m.group(3).strip(" ,."),
                           amt=amt, unit=(m.group(2) or "ea").lower())


def _all_json_ld(html: str) -> list[RecipeDraft]:
    blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                        html, re.S | re.I)
    out: list[RecipeDraft] = []
    for b in blocks:
        try:
            data = json.loads(b.strip())
        except Exception:
            continue
        for node in _walk_all_recipes(data):
            ings: list[DraftIngredient] = []
            for line in node.get("recipeIngredient", []) or []:
                parsed = _parse_ing_line(str(line))
                ings.append(parsed or DraftIngredient(raw=str(line), name=str(line), amt=1, unit="ea"))
            instr = node.get("recipeInstructions", []) or []
            steps = []
            if isinstance(instr, str):
                steps = [{"t": s.strip(), "mins": 5} for s in re.split(r"(?<=[.!?])\s+", instr) if len(s.strip()) > 8]
            else:
                for s in instr:
                    txt = s.get("text") if isinstance(s, dict) else str(s)
                    if txt and len(txt.strip()) > 4:
                        steps.append({"t": txt.strip()[:400], "mins": 5})
            if not ings:
                continue
            mins = _iso_minutes(node.get("totalTime")) or \
                (_iso_minutes(node.get("prepTime")) + _iso_minutes(node.get("cookTime"))) or 30
            name = node.get("name") or "Imported recipe"
            out.append(RecipeDraft(
                name=str(name)[:120], mins=mins or 30,
                basis_name=ings[0].name if ings else None,
                stages=[DraftStage(name="Everything", medium="Stove top", vessel="Saucepan",
                                   ing=ings, steps=[{"t": s["t"], "mins": s["mins"]} for s in steps] or
                                   [{"t": "Cook it.", "mins": 10}])],
                notes="Parsed from the page's own structured data — no model involved.",
                confidence="high"))
    return out


# ── tier 2: YouTube transcript ──────────────────────────────────────────────
def youtube_transcript(url: str) -> str | None:
    try:
        out = subprocess.run(
            ["yt-dlp", "--skip-download", "--write-auto-sub", "--sub-lang", "en",
             "--sub-format", "vtt", "-o", "/tmp/mise_sub", "--print", "%(description)s", url],
            capture_output=True, text=True, timeout=60)
        desc = out.stdout.strip()
        vtt = ""
        for p in ("/tmp/mise_sub.en.vtt", "/tmp/mise_sub.en-orig.vtt"):
            if os.path.exists(p):
                with open(p, encoding="utf-8", errors="ignore") as f:
                    vtt = f.read()
                os.remove(p)
                break
        cues = re.sub(r"(?m)^(WEBVTT|Kind:|Language:|\d{2}:.*|\s*)$", "", vtt)
        cues = re.sub(r"<[^>]+>", "", cues)
        lines, seen = [], set()
        for ln in (l.strip() for l in cues.splitlines()):
            if ln and ln not in seen:
                seen.add(ln)
                lines.append(ln)
        body = "\n".join(lines)
        combined = (desc + "\n\n" + body).strip()
        return combined or None
    except FileNotFoundError:
        return None
    except Exception:
        return None


# ── tier 3: page text ───────────────────────────────────────────────────────
def strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    txt = re.sub(r"(?s)<[^>]+>", "\n", html)
    txt = re.sub(r"&nbsp;?", " ", txt)
    txt = re.sub(r"\n{2,}", "\n", txt)
    return "\n".join(l.strip() for l in txt.splitlines() if l.strip())[:14000]


async def fetch(url: str) -> tuple[str | None, list[str]]:
    warn: list[str] = []
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT,
                                     headers={"User-Agent": UA}) as cl:
            r = await cl.get(url)
            if r.status_code >= 400:
                warn.append(f"{url} returned HTTP {r.status_code}. "
                            "Instagram and Pinterest block server fetches — paste the caption instead.")
                return None, warn
            return r.text, warn
    except Exception as e:
        warn.append(f"Could not fetch that URL ({type(e).__name__}). Paste the text instead.")
        return None, warn


# ── the model ───────────────────────────────────────────────────────────────
SYSTEM = """You convert cooking instructions into structured recipe drafts.

Rules you must not break:
- Use ONLY amounts and ingredient names that appear in the source. Never invent
  quantities. If an amount is missing, use 1 with unit "ea" and say so in notes.
- Return ingredient names exactly as a cook would say them ("bread flour",
  "chestnut mushrooms"). Never return database ids or codes.
- Do not convert units. Report the unit the source used.
- Split the work into STAGES that can run in parallel where the cooking really
  allows it. A stage is one vessel doing one job. Give each stage a `needs` list
  naming the stages that must finish first. A sauce reducing while pasta boils is
  two stages with no dependency; finishing the pasta in the sauce is a third that
  needs both. Do not invent parallelism that isn't there — a single-pan dish is
  one stage.
- Pick `basis_name`: the ingredient everything else scales from. For baking this
  is the flour. For a braise it is the meat. For pasta it is the pasta.
- Set confidence honestly. "low" if the source was vague or you had to guess.

Return {"recipes": [ ... ]} — a LIST. Almost always exactly one recipe. Return
several ONLY when the source plainly contains several distinct dishes (a
round-up post, a video covering three meals). Component sub-recipes of one dish
— a sauce, a dough, a garnish — are STAGES of that one recipe, not separate
recipes.

Return ONLY the JSON object, no prose, no code fence."""


def _schema_hint() -> str:
    return json.dumps(RecipeDraft.model_json_schema(), separators=(",", ":"))[:3500]


def _proposal_hint() -> str:
    return json.dumps(Proposal.model_json_schema(), separators=(",", ":"))[:3000]


# ── which model provider, and where it lives ────────────────────────────────
#
# The assistant does one job: unstructured text -> structured JSON. That job is
# small enough that a free model does it well, so Mise is not tied to any one
# vendor. Set ONE key in .env and the provider is picked automatically.
#
# Every provider below except Anthropic speaks the OpenAI chat-completions
# shape, so they share a single code path. Adding another is one table row.
PROVIDERS = {
    # env var            base url                                              default model
    "GROQ_API_KEY":      ("https://api.groq.com/openai/v1/chat/completions",
                          "llama-3.3-70b-versatile"),
    "GEMINI_API_KEY":    ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                          "gemini-2.5-flash"),
    "CEREBRAS_API_KEY":  ("https://api.cerebras.ai/v1/chat/completions",
                          "llama-3.3-70b"),
    "OPENROUTER_API_KEY": ("https://openrouter.ai/api/v1/chat/completions",
                          "meta-llama/llama-3.3-70b-instruct:free"),
    "OPENAI_API_KEY":    ("https://api.openai.com/v1/chat/completions",
                          "gpt-4o-mini"),
}


def which_provider() -> tuple[str, str, str, str] | None:
    """Returns (env_var_name, key, url, model), or None if nothing is configured."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return ("ANTHROPIC_API_KEY", os.environ["ANTHROPIC_API_KEY"],
                "https://api.anthropic.com/v1/messages",
                os.getenv("MISE_MODEL", "claude-sonnet-4-5"))
    for var, (url, default_model) in PROVIDERS.items():
        key = os.getenv(var)
        if key:
            return (var, key, url, os.getenv("MISE_MODEL", default_model))
    return None


class ProviderError(Exception):
    """A model call that failed, carrying enough detail to actually fix it.

    `HTTPStatusError` on its own tells you nothing — you cannot tell a bad key
    from a retired model name from a rate limit. The provider always says which
    in the response body, so that body is what gets shown.
    """
    def __init__(self, provider: str, model: str, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(f"{provider} returned HTTP {status} for model "
                         f"'{model}' — {detail}")


def _raise_readable(r: "httpx.Response", var: str, model: str) -> None:
    if r.is_success:
        return
    provider = var.replace("_API_KEY", "").title()
    try:
        j = r.json()
        detail = (j.get("error", {}).get("message")
                  if isinstance(j.get("error"), dict) else None) or json.dumps(j)
    except Exception:
        detail = r.text
    hint = {401: " (the key is wrong or not activated)",
            403: " (the key is rejected — check it's enabled for this API)",
            404: " (that model name doesn't exist for this key)",
            429: " (you've hit the free-tier rate limit — wait and retry)"}.get(r.status_code, "")
    raise ProviderError(provider, model, r.status_code, str(detail)[:300] + hint)


def _build_prompt(source_text: str, hint: str | None) -> str:
    user = f"Source:\n\n{source_text[:14000]}"
    if hint:
        user += f"\n\nThe cook adds: {hint}"
    user += f"\n\nMatch this JSON schema exactly:\n{_schema_hint()}"
    return user


def _raw_json(text: str) -> str:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("model returned no JSON object")
    return text[start:end + 1]


def _extract_drafts(text: str) -> list[RecipeDraft]:
    """Whatever the model wrapped its answer in, find the object and validate it.

    Validation is the whole point: a model that invents a negative quantity or
    drops a required field fails here, before anything reaches the UI.
    """
    blob = _raw_json(text)
    try:
        return RecipeDraftSet.model_validate_json(blob).recipes
    except Exception:
        return [RecipeDraft.model_validate_json(blob)]      # it ignored the wrapper


async def call_model(source_text: str, hint: str | None = None) -> list[RecipeDraft]:
    prov = which_provider()
    if not prov:
        raise RuntimeError("no-api-key")
    var, key, url, model = prov
    user = _build_prompt(source_text, hint)

    async with httpx.AsyncClient(timeout=90) as cl:
        if var == "ANTHROPIC_API_KEY":
            r = await cl.post(url, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01",
                "content-type": "application/json"},
                json={"model": model, "max_tokens": 3000, "system": SYSTEM,
                      "messages": [{"role": "user", "content": user}]})
            _raise_readable(r, var, model)
            body = r.json()
            text = "".join(b.get("text", "") for b in body.get("content", []))
        else:
            payload = {
                "model": model,
                "max_tokens": 3000,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": user}],
                "response_format": {"type": "json_object"},
            }
            r = await cl.post(url, headers={"Authorization": f"Bearer {key}",
                                            "Content-Type": "application/json"},
                              json=payload)
            if r.status_code == 400:
                # not every provider supports forced JSON mode. the prompt already
                # asks for bare JSON, and _extract_drafts copes either way.
                payload.pop("response_format", None)
                r = await cl.post(url, headers={"Authorization": f"Bearer {key}",
                                                "Content-Type": "application/json"},
                                  json=payload)
            _raise_readable(r, var, model)
            body = r.json()
            text = body["choices"][0]["message"]["content"] or ""

    return _extract_drafts(text)


# ── tier 5: regex fallback ──────────────────────────────────────────────────
def regex_draft(text: str) -> RecipeDraft:
    ings, steps = [], []
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        p = _parse_ing_line(line)
        if p and len(line) < 74:
            ings.append(p)
        elif len(line) > 22:
            steps.append({"t": line[:400], "mins": 5})
    if not ings:
        raise ValueError("no quantities found")
    return RecipeDraft(
        name="", basis_name=ings[0].name,
        stages=[DraftStage(name="Everything", ing=ings,
                           steps=[{"t": s["t"], "mins": s["mins"]} for s in steps] or
                           [{"t": "Cook it.", "mins": 10}])],
        notes="Parsed by the local regex fallback — no model key configured. Check every row.",
        confidence="low")


# ── orchestrator ────────────────────────────────────────────────────────────
async def build_drafts(url: str | None, text: str | None, hint: str | None
                       ) -> tuple[list[RecipeDraft], str, list[str]]:
    """Walk the tiers until something works. Returns every recipe it found."""
    warnings: list[str] = []

    async def model_or_regex(src: str, prov: str, why_regex: str
                             ) -> tuple[list[RecipeDraft], str, list[str]]:
        try:
            return await call_model(src, hint), prov, warnings
        except RuntimeError:
            warnings.append(why_regex)
            return [regex_draft(src)], "regex", warnings
        except ProviderError as e:
            warnings.append(f"The model call failed, so this was parsed locally instead. {e}")
            return [regex_draft(src)], "regex", warnings
        except Exception as e:
            warnings.append(f"Model call failed ({type(e).__name__}: {str(e)[:200]}); "
                            "fell back to the regex parser.")
            return [regex_draft(src)], "regex", warnings

    if text and text.strip():
        return await model_or_regex(
            text, "pasted-text",
            "No model API key set in .env — used the local regex parser instead.")

    if not url:
        raise ValueError("give me a url or some text")

    if re.search(r"(youtube\.com|youtu\.be)", url, re.I):
        tr = youtube_transcript(url)
        if tr:
            return await model_or_regex(
                tr, "youtube-transcript",
                "No model API key set in .env — a transcript needs the model to be useful.")
        warnings.append("yt-dlp is not installed, or that video has no subtitles.")

    walled = re.search(r"(instagram\.com|pinterest\.|facebook\.com|tiktok\.com)", url, re.I)
    if walled:
        # These refuse anonymous server fetches and there is no clever way
        # around it that doesn't mean parking your login on the Pi. yt-dlp
        # sometimes gets a public reel, so it's still worth one attempt.
        tr = youtube_transcript(url)
        if tr:
            return await model_or_regex(
                tr, "youtube-transcript",
                "No model API key set in .env — a transcript needs the model to be useful.")

    html, w = await fetch(url)
    warnings += w
    if not html:
        if walled:
            site = walled.group(1).rstrip(".").split(".")[0].title()
            raise ValueError(
                f"{site} won't let a server read that post — it blocks anything "
                "that isn't a logged-in browser. Open the post, copy the caption, "
                "and paste it into the box below. That works every time.")
        raise ValueError("; ".join(warnings) or "could not fetch that page")

    ld = _all_json_ld(html)
    if ld:
        if len(ld) > 1:
            warnings.append(f"That page has {len(ld)} recipes on it.")
        return ld, "json-ld", warnings

    body = strip_html(html)
    return await model_or_regex(
        body, "page-text",
        "No model API key set in .env — used the local regex parser on the page text.")


async def build_draft(url: str | None, text: str | None, hint: str | None
                      ) -> tuple[RecipeDraft, str, list[str]]:
    """Back-compat single-draft wrapper."""
    drafts, prov, warns = await build_drafts(url, text, hint)
    return drafts[0], prov, warns


def resolve(draft: RecipeDraft, pantry: dict[str, dict[str, Any]]
            ) -> tuple[list[dict[str, Any]], list[str]]:
    """Map every drafted ingredient onto something you own. Deterministic."""
    matched, unmatched = [], []
    for st in draft.stages:
        for g in st.ing:
            pid, score = match_pantry(g.name, pantry)
            base_amt, base_unit = to_base(g.amt, g.unit)
            if pid and pantry[pid].get("unit") == "ea" and g.unit not in COUNT_UNITS:
                base_amt = g.amt          # "2 lemons" not "2 g lemons"
                base_unit = "ea"
            matched.append({"stage": st.name, "raw": g.raw or g.name, "name": g.name,
                            "amt": base_amt, "unit": base_unit, "srcAmt": g.amt,
                            "srcUnit": g.unit, "id": pid, "score": score,
                            "station": g.station, "optional": g.optional})
            if not pid:
                unmatched.append(g.name)
    return matched, unmatched


ADAPT_SYSTEM = """You propose a MINIMAL edit to an existing recipe.

Return JSON only: {"summary": "...", "changes": [{"op":"replace|remove|add|note",
"stage":"...", "ingredient":"...", "to":"...", "amt":0, "unit":"g", "why":"..."}]}

Never restate the whole recipe. Never change amounts you were not asked to change.
Never remove the basis ingredient. If the request is impossible with what the cook
has, say so in `summary` and return an empty `changes` list."""


async def adapt(recipe: dict[str, Any], instruction: str, pantry_names: list[str]) -> dict[str, Any]:
    prov = which_provider()
    if not prov:
        raise RuntimeError("no-api-key")
    var, key, url, model = prov
    payload = {"recipe": recipe, "instruction": instruction, "in_pantry": pantry_names[:120]}
    content = json.dumps(payload)[:12000]

    async with httpx.AsyncClient(timeout=90) as cl:
        if var == "ANTHROPIC_API_KEY":
            r = await cl.post(url, headers={"x-api-key": key,
                                            "anthropic-version": "2023-06-01",
                                            "content-type": "application/json"},
                              json={"model": model, "max_tokens": 1500,
                                    "system": ADAPT_SYSTEM,
                                    "messages": [{"role": "user", "content": content}]})
            _raise_readable(r, var, model)
            text = "".join(b.get("text", "") for b in r.json().get("content", []))
        else:
            body = {"model": model, "max_tokens": 1500,
                    "messages": [{"role": "system", "content": ADAPT_SYSTEM},
                                 {"role": "user", "content": content}],
                    "response_format": {"type": "json_object"}}
            r = await cl.post(url, headers={"Authorization": f"Bearer {key}",
                                            "Content-Type": "application/json"}, json=body)
            if r.status_code == 400:
                body.pop("response_format", None)
                r = await cl.post(url, headers={"Authorization": f"Bearer {key}",
                                                "Content-Type": "application/json"}, json=body)
            _raise_readable(r, var, model)
            text = r.json()["choices"][0]["message"]["content"] or ""

    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text[text.find("{"):text.rfind("}") + 1])


# ── the assistant: it proposes, you approve, Python applies ─────────────────
#
# This is the same principle as the import path, extended to actions. The model
# is handed a read-only snapshot and returns a `Proposal`. It has no write
# endpoint, no database handle, and no way to reach one. Applying a proposal is
# the browser replaying it through the ordinary endpoints — the same ones your
# own clicks use — so there is exactly one write path in the system and it is
# the audited one.
#
# The practical consequence: the worst a confused model can do is waste your
# time. It cannot silently change a quantity you did not look at.
CHAT_SYSTEM = """You are the assistant inside Mise, a kitchen system. You help the
cook fix recipe drafts, correct their pantry, and build a shopping list.

YOU CANNOT CHANGE ANYTHING DIRECTLY. You return a PROPOSAL. The cook reads it as
a diff and clicks apply or discard. So:
- Propose the smallest change that does the job. Never restate things you are
  not changing.
- Every operation carries a short `why`. If you cannot justify it from what the
  cook said or from the data you were given, do not propose it.
- If you are missing something you need — which of two pantry rows they meant,
  what size their tin is — put it in `questions` and propose nothing. Asking is
  cheaper than a wrong guess they have to spot.
- Never invent quantities. If the cook says "I bought more flour", ask how much.
- `reply` is one or two sentences of plain speech. The diff shows the detail;
  do not narrate it line by line.

Scope rules:
- draft  — you may return a full revised `draft`. Keep every field you were not
           asked to change byte-identical. Amounts stay in the units given.
- pantry — use `pantry` ops. `id` must be a real id from the snapshot. Use
           `add` only for something genuinely not there.
- shop   — use `shop` ops. Names only, plus a quantity if the cook gave one.

Return ONLY the JSON object, no prose, no code fence."""


def _pantry_snapshot(pantry: dict[str, dict[str, Any]], limit: int = 140) -> list[dict]:
    """What the model is allowed to see. Ids, names, quantities — nothing else."""
    rows = []
    for pid, it in list(pantry.items())[:limit]:
        rows.append({"id": pid, "name": it.get("name"), "qty": it.get("qty"),
                     "unit": it.get("unit"), "expires": it.get("expires")})
    return rows


async def chat(message: str, scope: str, draft: dict[str, Any] | None,
               history: list[dict[str, str]], pantry: dict[str, dict[str, Any]]
               ) -> ChatResponse:
    prov = which_provider()
    if not prov:
        raise RuntimeError("no-api-key")
    var, key, url, model = prov

    context: dict[str, Any] = {"scope": scope, "pantry": _pantry_snapshot(pantry)}
    if draft:
        context["draft"] = draft
    user = (f"Context (read-only):\n{json.dumps(context)[:11000]}\n\n"
            f"The cook says: {message}\n\n"
            f"Match this JSON schema exactly:\n{_proposal_hint()}\n\n"
            f"When you return a `draft`, it must match:\n{_schema_hint()}")

    msgs = [{"role": h.get("role", "user")[:9], "content": str(h.get("content", ""))[:2000]}
            for h in history[-6:]]
    msgs.append({"role": "user", "content": user})

    async with httpx.AsyncClient(timeout=90) as cl:
        if var == "ANTHROPIC_API_KEY":
            r = await cl.post(url, headers={"x-api-key": key,
                                            "anthropic-version": "2023-06-01",
                                            "content-type": "application/json"},
                              json={"model": model, "max_tokens": 3000,
                                    "system": CHAT_SYSTEM, "messages": msgs})
            _raise_readable(r, var, model)
            text = "".join(b.get("text", "") for b in r.json().get("content", []))
        else:
            body = {"model": model, "max_tokens": 3000,
                    "messages": [{"role": "system", "content": CHAT_SYSTEM}] + msgs,
                    "response_format": {"type": "json_object"}}
            r = await cl.post(url, headers={"Authorization": f"Bearer {key}",
                                            "Content-Type": "application/json"}, json=body)
            if r.status_code == 400:
                body.pop("response_format", None)
                r = await cl.post(url, headers={"Authorization": f"Bearer {key}",
                                                "Content-Type": "application/json"}, json=body)
            _raise_readable(r, var, model)
            text = r.json()["choices"][0]["message"]["content"] or ""

    try:
        data = json.loads(_raw_json(text))
    except Exception:
        # It answered in prose instead of JSON. That's a miss, not a crash —
        # show the cook what it said and propose nothing.
        return ChatResponse(reply=(text.strip()[:600] or "It didn't answer.") +
                            "\n\n(That wasn't a change I could act on — nothing proposed.)")
    reply = str(data.pop("reply", "") or data.get("summary", "") or "Here's what I'd change.")

    # The gate. A proposal that doesn't fit the schema is discarded entirely
    # rather than half-applied — you get the sentence, not a broken change-set.
    proposal: Proposal | None = None
    try:
        proposal = Proposal.model_validate(data)
    except Exception as e:
        return ChatResponse(reply=f"{reply}\n\n(I couldn't put that into a valid change — "
                                  f"{type(e).__name__}. Nothing was proposed.)")

    if proposal.is_empty() and not proposal.questions:
        return ChatResponse(reply=reply)

    matched, unmatched = [], []
    if proposal.draft:
        matched, unmatched = resolve(proposal.draft, pantry)

    # Drop pantry ops pointing at rows that don't exist. A hallucinated id is
    # the single most likely failure here, and it should never reach the diff.
    kept = []
    for op in proposal.pantry:
        if op.op == "add" or (op.id and op.id in pantry):
            kept.append(op)
    dropped = len(proposal.pantry) - len(kept)
    proposal.pantry = kept
    if dropped:
        reply += f" (Dropped {dropped} change{'s' if dropped > 1 else ''} aimed at pantry rows that don't exist.)"

    return ChatResponse(reply=reply, proposal=proposal, matched=matched, unmatched=unmatched)


# ── diagnostics ─────────────────────────────────────────────────────────────
async def probe() -> dict[str, Any]:
    """One minimal real call. Returns what the provider actually said."""
    prov = which_provider()
    if not prov:
        return {"ok": False, "error": "no API key set in .env"}
    var, key, url, model = prov
    try:
        async with httpx.AsyncClient(timeout=30) as cl:
            if var == "ANTHROPIC_API_KEY":
                r = await cl.post(url, headers={"x-api-key": key,
                                                "anthropic-version": "2023-06-01",
                                                "content-type": "application/json"},
                                  json={"model": model, "max_tokens": 8,
                                        "messages": [{"role": "user", "content": "say ok"}]})
            else:
                r = await cl.post(url, headers={"Authorization": f"Bearer {key}",
                                                "Content-Type": "application/json"},
                                  json={"model": model, "max_tokens": 8,
                                        "messages": [{"role": "user", "content": "say ok"}]})
            _raise_readable(r, var, model)
        return {"ok": True, "provider": var.replace("_API_KEY", "").title(), "model": model}
    except ProviderError as e:
        return {"ok": False, "status": e.status, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}


async def list_models() -> dict[str, Any]:
    """Ask the provider which models this key may use.

    A 404 on a model name is the most common way a working key looks broken,
    and the only reliable cure is seeing the real list rather than guessing.
    """
    prov = which_provider()
    if not prov:
        return {"ok": False, "error": "no API key set in .env"}
    var, key, url, model = prov
    listing = url.rsplit("/chat/completions", 1)[0] + "/models"
    if var == "ANTHROPIC_API_KEY":
        listing, headers = "https://api.anthropic.com/v1/models", {
            "x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=30) as cl:
            r = await cl.get(listing, headers=headers)
            _raise_readable(r, var, model)
            body = r.json()
        rows = body.get("data") or body.get("models") or []
        names = sorted({str(m.get("id") or m.get("name", "")).split("/")[-1]
                        for m in rows if isinstance(m, dict)})
        return {"ok": True, "current": model, "current_is_available": model in names,
                "available": names}
    except ProviderError as e:
        return {"ok": False, "status": e.status, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
