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

import asyncio
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
PREP_RE = re.compile(
    r"[,;.]?\s*\b(?:thinly |finely |roughly |coarsely |freshly |very )?"
    r"(?:sliced|diced|chopped|minced|grated|whisked|beaten|shredded|julienned|"
    r"crushed|peeled|cubed|halved|quartered|trimmed|rinsed|drained|cooked|"
    r"boneless|skinless|softened|melted|room temperature|to taste|optional)\b\.?",
    re.I)


def ingredient_noun(name: str) -> str:
    """Strip preparation from an ingredient name.

    "White Onion, sliced" and "Green Onion, thinly sliced" are the same pantry
    item as "onion" — the prep belongs in the step, not on the shelf. Used when
    creating a pantry row, never when displaying the source line.
    """
    out = PREP_RE.sub("", name)
    out = re.sub(r"\s*\([^)]*\)", "", out)
    out = re.sub(r"[,;.]\s*$", "", out).strip(" ,.;")
    out = re.sub(r"\s{2,}", " ", out)
    return out or name


# Words that describe how an ingredient was prepared or sized. They are noise
# for matching: "1 Large Egg, whisked" and "Eggs" are the same shelf item.
NOISE = re.compile(
    r"\b(fresh|freshly|dried|ground|chopped|sliced|minced|grated|whisked|beaten|"
    r"shredded|crushed|peeled|cubed|diced|halved|quartered|trimmed|rinsed|drained|"
    r"cooked|toasted|boneless|skinless|softened|melted|thinly|finely|roughly|"
    r"coarsely|large|small|medium|whole|optional|of|the|a|an)\b")


def _singular(tok: str) -> str:
    """Crude but sufficient: eggs -> egg, tomatoes -> tomato, leaves stays."""
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 4 and tok.endswith("oes"):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(_singular(t) for t in s.split()).strip()


# Words that separate two things sharing a head noun, grouped by what they
# distinguish. Two names conflict when they both carry a word from the SAME
# group and those words differ — "green onion" vs "yellow onion" is a colour
# disagreement, "salted" vs "unsalted" a salt one. Sharing a group harmlessly
# ("sesame oil" and "olive oil" are both oils) is not a conflict.
QUALIFIER_GROUPS = {
    "colour":   {"green", "yellow", "white", "red", "purple"},
    # which PART of the animal or plant — a breast, a thigh, or the stock you
    # boiled the bones into are all "chicken" and none of them substitute
    "part":     {"breast", "thigh", "wing", "leg", "mince", "fillet",
                 "stock", "broth", "bone"},
    # what FORM it takes — the juice, the zest and the oil of the same fruit
    # are three different things on three different shelves
    "form":     {"powder", "paste", "sauce", "oil", "vinegar", "flour",
                 "seed", "zest", "juice", "leave", "root", "syrup"},
    "dairy":    {"milk", "cream", "butter", "cheese", "yoghurt", "yogurt"},
    "salt":     {"salted", "unsalted"},
    "richness": {"double", "single", "skimmed", "semi"},
    "flourtype": {"plain", "self", "raising", "wholemeal", "bread", "strong"},
    "sugar":    {"caster", "icing", "granulated", "muscovado"},
    "grain":    {"short", "long", "basmati", "arborio"},
    "cure":     {"smoked", "unsmoked", "cured", "fresh"},
}
QUALIFIER_OF = {}
for _g, _words in QUALIFIER_GROUPS.items():
    for _w in _words:
        QUALIFIER_OF.setdefault(_w, _g)


def _conflicts(a: set[str], b: set[str]) -> bool:
    """True when the two names disagree on something that distinguishes them."""
    for group in QUALIFIER_GROUPS:
        ga = {t for t in a if QUALIFIER_OF.get(t) == group}
        gb = {t for t in b if QUALIFIER_OF.get(t) == group}
        if ga and gb and not (ga & gb):
            return True
    return False


def match_pantry(name: str, pantry: dict[str, dict[str, Any]]) -> tuple[str | None, float]:
    """Return (pantry_id, score 0-1).

    A WRONG match is worse than no match. An unmatched row is amber and you fix
    it in two seconds; a confidently wrong one silently logs chicken stock as
    the chicken breast you weighed, and poisons the dataset the whole project
    exists to collect. So this is deliberately conservative:

      - shared words carry the score, not string similarity. "green onion" and
        "yellow onion" are one character apart and different ingredients;
      - a raw fuzzy ratio can only win on its own if it is very high;
      - and if the names disagree on a distinguishing word — colour, cut, form,
        salted vs unsalted — the match is refused outright, however similar the
        strings look.
    """
    n = _norm(name)
    if not n:
        return None, 0.0
    ntok = set(n.split())
    best, best_score = None, 0.0
    for pid, item in pantry.items():
        for cand in (item.get("name", ""), item.get("nl", ""), item.get("tag", "")):
            c = _norm(cand)
            if not c:
                continue
            ctok = set(c.split())

            if _conflicts(ntok, ctok):
                continue                     # "breast" vs "stock" — not the same thing

            shared = ntok & ctok
            overlap = len(shared) / max(1, min(len(ntok), len(ctok)))
            ratio = SequenceMatcher(None, n, c).ratio()

            # string similarity alone is only trusted when it is near-identical
            score = max(overlap * 0.95, ratio if ratio >= 0.86 else ratio * 0.6)
            if shared and (c in n or n in c):
                score = max(score, 0.9)      # one name contains the other, whole
            if score > best_score:
                best, best_score = pid, score
    return (best, round(best_score, 3)) if best_score >= 0.6 else (None, round(best_score, 3))


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
- Pick the `vessel` from what the stage physically does. A stage that mixes,
  whisks or combines happens in a bowl, not on a board; a board is for cutting.
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

# Free tiers get busy. A 503 "high demand" is not a broken setup, it is a queue,
# and falling straight through to the regex parser because a server was busy for
# two seconds is the wrong call. So: retry with backoff, then try a smaller
# sibling model, which is usually far less contended than the flagship.
FALLBACKS = {
    "GEMINI_API_KEY":   ["gemini-3.5-flash-lite", "gemini-2.5-flash-lite", "gemini-2.5-flash"],
    "GROQ_API_KEY":     ["llama-3.1-8b-instant"],
    "CEREBRAS_API_KEY": ["llama3.1-8b"],
    "OPENAI_API_KEY":   ["gpt-4o-mini"],
}

# Statuses that mean "ask again later", not "you did it wrong".
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


# A model name belongs to exactly one vendor. MISE_MODEL left over from a
# previous provider is the single most confusing failure mode here: the key is
# valid, the endpoint is right, and the provider 404s on a name it has never
# heard of. So an override that plainly belongs elsewhere is ignored.
MODEL_OWNER = {"claude": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY",
               "gpt": "OPENAI_API_KEY", "o1": "OPENAI_API_KEY", "o3": "OPENAI_API_KEY"}


def _model_for(var: str, default_model: str) -> tuple[str, str | None]:
    """Returns (model, warning). The override wins unless it's for another vendor."""
    override = os.getenv("MISE_MODEL", "").strip()
    if not override:
        return default_model, None
    for prefix, owner in MODEL_OWNER.items():
        if override.lower().startswith(prefix) and owner != var:
            return default_model, (
                f"MISE_MODEL is set to '{override}', which is a "
                f"{owner.replace('_API_KEY', '').title()} model — but your key is "
                f"{var.replace('_API_KEY', '').title()}. Ignoring it and using "
                f"'{default_model}'. Remove or fix the MISE_MODEL line in .env.")
    return override, None


def which_provider() -> tuple[str, str, str, str] | None:
    """Returns (env_var_name, key, url, model), or None if nothing is configured."""
    if os.getenv("ANTHROPIC_API_KEY"):
        model, _ = _model_for("ANTHROPIC_API_KEY", "claude-sonnet-4-5")
        return ("ANTHROPIC_API_KEY", os.environ["ANTHROPIC_API_KEY"],
                "https://api.anthropic.com/v1/messages", model)
    for var, (url, default_model) in PROVIDERS.items():
        key = os.getenv(var)
        if key:
            model, _ = _model_for(var, default_model)
            return (var, key, url, model)
    return None


def provider_warning() -> str | None:
    """The mismatch note, if there is one — surfaced by /api/assist/health."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return _model_for("ANTHROPIC_API_KEY", "claude-sonnet-4-5")[1]
    for var, (url, default_model) in PROVIDERS.items():
        if os.getenv(var):
            return _model_for(var, default_model)[1]
    return None


class ProviderError(Exception):
    """A model call that failed, carrying enough detail to actually fix it.

    `HTTPStatusError` on its own tells you nothing — you cannot tell a bad key
    from a retired model name from a rate limit. The provider always says which
    in the response body, so that body is what gets shown.
    """
    def __init__(self, provider: str, model: str, status: int, detail: str):
        self.status, self.detail = status, detail
        # status 200 means the call succeeded and the *answer* was the problem;
        # "returned HTTP 200" would read as nonsense, so let the detail speak.
        msg = detail if status == 200 else (
            f"{provider} returned HTTP {status} for model '{model}' — {detail}")
        super().__init__(msg)


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
            429: " (you've hit the free-tier rate limit — wait and retry)",
            500: " (the provider had an internal error — not your setup)",
            502: " (the provider is having trouble — not your setup)",
            503: " (the provider is overloaded right now — nothing is wrong with "
                 "your setup, it usually clears within a few minutes)",
            504: " (the provider timed out — try again shortly)"}.get(r.status_code, "")
    raise ProviderError(provider, model, r.status_code, str(detail)[:300] + hint)


def _retry_after(r: "httpx.Response", attempt: int) -> float:
    """Honour the server's own advice if it gave any, else back off."""
    hdr = r.headers.get("retry-after")
    if hdr:
        try:
            return min(float(hdr), 20.0)
        except ValueError:
            pass
    return 1.5 * (2 ** attempt)          # 1.5s, 3s, 6s


def _reasoning_effort(model: str) -> str | None:
    """How hard to let the model think before answering.

    Gemini 2.5 can switch thinking off entirely; Gemini 3.x cannot, so ask for
    the minimum. This matters because reasoning tokens are spent from the SAME
    budget as the reply — a model that thinks for 3000 tokens under a 3000-token
    cap returns an empty answer, which is indistinguishable from a broken one
    unless you know to look for it.

    Turning structured extraction into a reasoning problem is also the wrong
    trade: reading amounts off a recipe does not need deliberation, it needs
    the budget spent on output.
    """
    m = model.lower()
    if m.startswith("gemini-2.5"):
        return "none"
    if m.startswith("gemini-"):
        return "low"
    return None


async def _one_call(cl: "httpx.AsyncClient", var: str, key: str, url: str, model: str,
                    system: str, messages: list[dict], max_tokens: int,
                    want_json: bool) -> "httpx.Response":
    if var == "ANTHROPIC_API_KEY":
        return await cl.post(url, headers={"x-api-key": key,
                                           "anthropic-version": "2023-06-01",
                                           "content-type": "application/json"},
                             json={"model": model, "max_tokens": max_tokens,
                                   "system": system, "messages": messages})
    body: dict[str, Any] = {"model": model, "max_tokens": max_tokens,
                            "messages": [{"role": "system", "content": system}] + messages}
    if want_json:
        body["response_format"] = {"type": "json_object"}
    effort = _reasoning_effort(model)
    if effort:
        body["reasoning_effort"] = effort
    return await cl.post(url, headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"}, json=body)


async def call_provider(system: str, messages: list[dict], max_tokens: int = 8000,
                        info: dict[str, Any] | None = None) -> str:
    """Every model call in Mise goes through here. Returns the raw text.

    Retries transient failures, then falls back to a smaller sibling model
    before giving up. A permanent error (bad key, unknown model) is raised
    immediately — retrying that just wastes your time and the free tier's.

    Pass `info` to find out what actually happened: which model answered, how
    many attempts it took, and whether it had to drop to a fallback. Silently
    using a different model than you configured would be a lie by omission.
    """
    prov = which_provider()
    if not prov:
        raise RuntimeError("no-api-key")
    var, key, url, model = prov
    models = [model] + [m for m in FALLBACKS.get(var, []) if m != model]

    last_r: "httpx.Response | None" = None
    last_model = model
    empty_note: str | None = None
    base_tokens = max_tokens
    async with httpx.AsyncClient(timeout=90) as cl:
        for m in models:
            want_json = var != "ANTHROPIC_API_KEY"
            stretched = False
            max_tokens = base_tokens        # each model starts from the same budget
            for attempt in range(MAX_ATTEMPTS + 1):
                r = await _one_call(cl, var, key, url, m, system, messages,
                                    max_tokens, want_json)
                if r.status_code == 400 and want_json:
                    # this provider doesn't do forced JSON mode. the prompt asks
                    # for bare JSON anyway, and the extractors cope either way.
                    want_json = False
                    r = await _one_call(cl, var, key, url, m, system, messages,
                                        max_tokens, False)
                last_r, last_model = r, m
                if r.is_success:
                    body = r.json()
                    if var == "ANTHROPIC_API_KEY":
                        text = "".join(b.get("text", "") for b in body.get("content", []))
                        reason = body.get("stop_reason", "")
                    else:
                        ch = (body.get("choices") or [{}])[0]
                        text = (ch.get("message") or {}).get("content") or ""
                        reason = ch.get("finish_reason", "")
                    if text.strip():
                        if info is not None:
                            info.update(model=m, attempts=attempt + 1,
                                        fallback=(m != model), budget=max_tokens)
                        return text
                    # Empty body with a length stop = the whole budget went on
                    # reasoning. Give it room once rather than reporting a
                    # failure the cook can do nothing about.
                    if reason in ("length", "max_tokens", "MAX_TOKENS") and not stretched:
                        stretched = True
                        max_tokens *= 4
                        continue
                    who = "Gemini" if var == "GEMINI_API_KEY" else var.split("_")[0].title()
                    if reason in ("length", "max_tokens", "MAX_TOKENS"):
                        empty_note = (
                            f"{who} model '{m}' answered with nothing — it spent its "
                            f"whole {max_tokens:,}-token budget reasoning and had none "
                            "left to write the reply with.")
                    else:
                        empty_note = (f"{who} model '{m}' returned an empty answer "
                                      f"(finish_reason: {reason or 'unknown'}).")
                    break
                if r.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_retry_after(r, attempt))
                    continue
                break
            if last_r is not None and last_r.is_success:
                continue         # it answered, but emptily — try the next model
            if last_r is not None and last_r.status_code not in RETRY_STATUS:
                break            # a 401 or 404 will not fix itself on another model

    if empty_note:
        if len(models) > 1:
            empty_note += (f" Every model I tried did the same ({', '.join(models)}), "
                           "which points at the prompt or the provider rather than the "
                           "model choice.")
        raise ProviderError(var.replace("_API_KEY", "").title(), last_model, 200, empty_note)
    _raise_readable(last_r, var, last_model)   # always raises
    raise RuntimeError("unreachable")


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


async def call_model(source_text: str, hint: str | None = None,
                     info: dict[str, Any] | None = None) -> list[RecipeDraft]:
    text = await call_provider(SYSTEM, [{"role": "user",
                                         "content": _build_prompt(source_text, hint)}],
                               info=info)
    return _extract_drafts(text)


# ── tier 5: regex fallback ──────────────────────────────────────────────────
def regex_draft(text: str) -> RecipeDraft:
    ings, steps = [], []
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        # An ingredient line starts with a quantity. A step is prose. Length is
        # not the difference between them — a long ingredient line is still an
        # ingredient line, and capping it is how the chicken ends up as a step.
        p = _parse_ing_line(line)
        if p:
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
        notes="Parsed by the local regex parser here on the Pi, not by a model. It only understands lines that start with a number, so check every row.",
        confidence="low")


# ── orchestrator ────────────────────────────────────────────────────────────
async def build_drafts(url: str | None, text: str | None, hint: str | None
                       ) -> tuple[list[RecipeDraft], str, list[str]]:
    """Walk the tiers until something works. Returns every recipe it found."""
    warnings: list[str] = []

    async def model_or_regex(src: str, prov: str, why_regex: str
                             ) -> tuple[list[RecipeDraft], str, list[str]]:
        info: dict[str, Any] = {}
        try:
            drafts = await call_model(src, hint, info=info)
            if info.get("fallback"):
                warnings.append(f"Your usual model was busy, so this was read by "
                                f"'{info['model']}' instead.")
            return drafts, prov, warnings
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


# What a stage does decides what it happens in. A bench stage that says "mix in
# a bowl" and gets assigned a chopping board isn't just cosmetically wrong — the
# cook board counts vessels to warn about conflicts, so a wrong vessel invents a
# conflict you don't have, or hides one you do.
VESSEL_HINTS = [
    (r"\b(mix|whisk|beat|combine|stir together|dissolve|marinate|toss|dress)\b",
     "Bench", "Mixing bowl"),
    (r"\b(chop|slice|dice|mince|cut|julienne|trim|carve)\b", "Bench", "Board"),
    (r"\b(fry|sear|saut|brown|sizzle)\b", "Stove top", "Frying pan"),
    (r"\b(boil|simmer|reduce|poach|blanch)\b", "Stove top", "Saucepan"),
    (r"\b(roast|bake)\b", "Oven", "Sheet tray"),
]


def fix_vessels(draft: RecipeDraft) -> RecipeDraft:
    """Correct an obviously wrong vessel from what the steps actually say."""
    for st in draft.stages:
        text = " ".join(x.t if hasattr(x, "t") else str(x) for x in st.steps).lower()
        for pattern, medium, vessel in VESSEL_HINTS:
            if st.medium == medium and re.search(pattern, text):
                st.vessel = vessel
                break
    return draft


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
                            # what this would be called on a shelf, if you have
                            # to create it: the thing, without the prep
                            "noun": ingredient_noun(g.name),
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
    if not which_provider():
        raise RuntimeError("no-api-key")
    payload = {"recipe": recipe, "instruction": instruction, "in_pantry": pantry_names[:120]}
    text = await call_provider(ADAPT_SYSTEM,
                               [{"role": "user", "content": json.dumps(payload)[:12000]}],
                               max_tokens=1500)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text[text.find("{"):text.rfind("}") + 1])

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
        return {"ok": True, "provider": var.replace("_API_KEY", "").title(),
                "model": model,
                "fallbacks": [m for m in FALLBACKS.get(var, []) if m != model]}
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
