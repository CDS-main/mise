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

from .models import DraftIngredient, DraftStage, RecipeDraft

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
COUNT_UNITS = {"ea", "x", ""}


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
def _walk_for_recipe(node: Any) -> dict | None:
    if isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(str(x).lower() == "recipe" for x in types if x):
            return node
        for v in node.values():
            found = _walk_for_recipe(v)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _walk_for_recipe(v)
            if found:
                return found
    return None


ISO_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?")


def _iso_minutes(s: str | None) -> int:
    if not s:
        return 0
    m = ISO_DUR.match(str(s))
    if not m:
        return 0
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


QTY_LINE = re.compile(
    r"^\s*(?:[-*•]\s*)?(\d+(?:[.,/]\d+)?)\s*"
    r"(kg|g|gram[s]?|ml|l|liter|litre|tbsp|tablespoon[s]?|tsp|teaspoon[s]?|cups?|oz|lb|el|tl)?\s*"
    r"(.{2,70})$", re.I)


def _parse_ing_line(line: str) -> DraftIngredient | None:
    m = QTY_LINE.match(line.strip())
    if not m:
        return None
    raw_amt = m.group(1).replace(",", ".")
    try:
        amt = eval(raw_amt) if "/" in raw_amt else float(raw_amt)  # noqa: S307 - digits only
    except Exception:
        return None
    if amt <= 0:
        return None
    return DraftIngredient(raw=line.strip(), name=m.group(3).strip(" ,."),
                           amt=amt, unit=(m.group(2) or "ea").lower())


def from_json_ld(html: str) -> RecipeDraft | None:
    blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                        html, re.S | re.I)
    for b in blocks:
        try:
            data = json.loads(b.strip())
        except Exception:
            continue
        node = _walk_for_recipe(data)
        if not node:
            continue
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
        return RecipeDraft(
            name=str(name)[:120], mins=mins or 30,
            basis_name=ings[0].name if ings else None,
            stages=[DraftStage(name="Everything", medium="Stove top", vessel="Saucepan",
                               ing=ings, steps=[{"t": s["t"], "mins": s["mins"]} for s in steps] or
                               [{"t": "Cook it.", "mins": 10}])],
            notes="Parsed from the page's own structured data — no model involved.",
            confidence="high")
    return None


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

Return ONLY the JSON object, no prose, no code fence."""


def _schema_hint() -> str:
    return json.dumps(RecipeDraft.model_json_schema(), separators=(",", ":"))[:3500]


async def call_model(source_text: str, hint: str | None = None) -> RecipeDraft:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("no-api-key")
    model = os.getenv("MISE_MODEL", "claude-sonnet-4-5")
    user = f"Source:\n\n{source_text[:14000]}"
    if hint:
        user += f"\n\nThe cook adds: {hint}"
    user += f"\n\nMatch this JSON schema exactly:\n{_schema_hint()}"
    async with httpx.AsyncClient(timeout=90) as cl:
        r = await cl.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 3000, "system": SYSTEM,
                  "messages": [{"role": "user", "content": user}]},
        )
        r.raise_for_status()
        body = r.json()
    text = "".join(b.get("text", "") for b in body.get("content", []))
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("model returned no JSON object")
    return RecipeDraft.model_validate_json(text[start:end + 1])   # rejects anything malformed


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
async def build_draft(url: str | None, text: str | None, hint: str | None
                      ) -> tuple[RecipeDraft, str, list[str]]:
    warnings: list[str] = []
    if text and text.strip():
        try:
            return await call_model(text, hint), "pasted-text", warnings
        except RuntimeError:
            warnings.append("No ANTHROPIC_API_KEY set — used the local regex parser instead.")
            return regex_draft(text), "regex", warnings
        except Exception as e:
            warnings.append(f"Model call failed ({type(e).__name__}); fell back to regex.")
            return regex_draft(text), "regex", warnings

    if not url:
        raise ValueError("give me a url or some text")

    if re.search(r"(youtube\.com|youtu\.be)", url, re.I):
        tr = youtube_transcript(url)
        if tr:
            try:
                return await call_model(tr, hint), "youtube-transcript", warnings
            except RuntimeError:
                warnings.append("No ANTHROPIC_API_KEY set — a transcript needs the model to be useful.")
                return regex_draft(tr), "regex", warnings
        warnings.append("yt-dlp is not installed or the video has no subtitles.")

    html, w = await fetch(url)
    warnings += w
    if not html:
        raise ValueError("; ".join(warnings) or "could not fetch that page")

    ld = from_json_ld(html)
    if ld:
        return ld, "json-ld", warnings

    body = strip_html(html)
    try:
        return await call_model(body, hint), "page-text", warnings
    except RuntimeError:
        warnings.append("No ANTHROPIC_API_KEY set — used the local regex parser on the page text.")
        return regex_draft(body), "regex", warnings


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
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("no-api-key")
    model = os.getenv("MISE_MODEL", "claude-sonnet-4-5")
    payload = {"recipe": recipe, "instruction": instruction, "in_pantry": pantry_names[:120]}
    async with httpx.AsyncClient(timeout=90) as cl:
        r = await cl.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": model, "max_tokens": 1500, "system": ADAPT_SYSTEM,
                                "messages": [{"role": "user",
                                              "content": json.dumps(payload)[:12000]}]})
        r.raise_for_status()
        body = r.json()
    text = "".join(b.get("text", "") for b in body.get("content", []))
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text[text.find("{"):text.rfind("}") + 1])
