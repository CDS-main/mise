"""Pydantic schemas.

These are the contract between the browser, the server, and the assistant.
The assistant is *forced* to emit `RecipeDraft` — anything it returns that
doesn't validate is rejected before it reaches the UI. That is the whole
reason the model is allowed nowhere near the numbers that get logged.
"""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator

Station = Literal["dry", "wet", "pan", "prep", "direct"]

CUISINES = ["Italian", "Korean", "Japanese", "Chinese", "Middle Eastern", "American",
            "Spanish", "French", "Indian", "Thai", "Mexican", "Other"]
MEALS = ["Breakfast", "Brunch", "Lunch", "Dinner", "Snack"]
TASTES = ["Savoury", "Sweet", "Sour", "Salty", "Bitter", "Umami"]
MEDIUMS = ["Bench", "Stove top", "Oven", "Fry", "Microwave", "Hot water",
           "Sous vide", "Mixer", "Blend"]


class Ingredient(BaseModel):
    id: str                                   # pantry id, resolved server-side
    amt: float = Field(gt=0)
    station: Station = "prep"
    optional: bool = False
    integer: bool = False
    subs: list[str] = []


class Step(BaseModel):
    t: str = Field(min_length=3, max_length=400)
    mins: int = Field(default=5, ge=0, le=1440)
    optional: bool = False
    tip: str | None = None


class Stage(BaseModel):
    id: str
    name: str
    medium: str = "Stove top"
    vessel: str = "Saucepan"
    needs: list[str] = []
    ing: list[Ingredient] = []
    steps: list[Step] = []

    @field_validator("medium")
    @classmethod
    def _medium(cls, v: str) -> str:
        return coerce_medium(v)


class Recipe(BaseModel):
    id: str
    name: str
    cuisine: str = "Other"
    meal: str = "Dinner"
    taste: str = "Savoury"
    mins: int = 30
    themes: list[str] = []
    basis: str
    source: str | None = None
    stages: list[Stage]

    @field_validator("cuisine")
    @classmethod
    def _cuisine(cls, v): return v if v in CUISINES else "Other"

    @field_validator("meal")
    @classmethod
    def _meal(cls, v): return v if v in MEALS else "Dinner"

    @field_validator("taste")
    @classmethod
    def _taste(cls, v): return v if v in TASTES else "Savoury"


# ── what the LLM is allowed to emit ─────────────────────────────────────────
# Note it returns ingredient *names as written*, never pantry ids. Mapping a
# name to something you own is a deterministic job and stays in Python.
class DraftIngredient(BaseModel):
    raw: str = ""                             # the source line, for your review
    name: str                                 # "bread flour"
    amt: float = Field(gt=0)
    unit: str = "g"                           # g|ml|kg|l|tbsp|tsp|cup|ea
    station: Station = "prep"
    optional: bool = False


# Models emit words that describe the *job* rather than the equipment —
# "Assembly", "Serve", "None". They are not mediums, and letting them through
# breaks everything downstream: the vessel list is keyed by medium, so an
# unknown one leaves you unable to pick the bowl you own.
MEDIUM_ALIASES = {
    "none": "Bench", "": "Bench", "assembly": "Bench", "assemble": "Bench",
    "serve": "Bench", "serving": "Bench", "plating": "Bench", "plate": "Bench",
    "prep": "Bench", "preparation": "Bench", "counter": "Bench",
    "bowl": "Bench", "hand": "Bench", "no heat": "Bench", "raw": "Bench",
    "stovetop": "Stove top", "stove": "Stove top", "hob": "Stove top",
    "pan": "Stove top", "saucepan": "Stove top", "skillet": "Stove top",
    "baking": "Oven", "roast": "Oven", "grill": "Oven", "broil": "Oven",
    "deep fry": "Fry", "frying": "Fry", "microwave oven": "Microwave",
    "kettle": "Hot water", "boiling water": "Hot water",
    "blender": "Blend", "food processor": "Blend", "stand mixer": "Mixer",
}


def coerce_medium(v: str) -> str:
    v = (v or "").strip()
    if v in MEDIUMS:
        return v
    lower = v.lower()
    if lower in MEDIUM_ALIASES:
        return MEDIUM_ALIASES[lower]
    for m in MEDIUMS:                          # "Stove Top", "OVEN"
        if m.lower() == lower:
            return m
    return "Bench"


class DraftStage(BaseModel):
    name: str
    medium: str = "Stove top"
    vessel: str = "Saucepan"
    needs: list[str] = []                     # names of other stages
    ing: list[DraftIngredient] = []
    steps: list[Step] = []

    @field_validator("medium")
    @classmethod
    def _medium(cls, v: str) -> str:
        return coerce_medium(v)


class RecipeDraft(BaseModel):
    """The assistant's output. Every field is a suggestion you correct."""
    name: str
    cuisine: str = "Other"
    meal: str = "Dinner"
    taste: str = "Savoury"
    mins: int = 30
    servings: int | None = None
    basis_name: str | None = None             # which ingredient scales the rest
    stages: list[DraftStage]
    notes: str | None = None
    confidence: Literal["high", "medium", "low"] = "medium"


class RecipeDraftSet(BaseModel):
    """What the model returns. A page can hold more than one recipe."""
    recipes: list[RecipeDraft] = Field(min_length=1)


class ImportRequest(BaseModel):
    url: str | None = None
    text: str | None = None
    hint: str | None = None                   # "it's a 2-pan recipe", etc.
    choose: int | None = None                 # which candidate, once you've picked


PROVENANCE = Literal["json-ld", "youtube-transcript", "page-text", "pasted-text", "regex"]


class Candidate(BaseModel):
    index: int
    name: str
    ingredients: int
    mins: int


class ImportResponse(BaseModel):
    """Either a draft, or — when the source held several recipes — a choice.

    `draft` is None exactly when `candidates` has more than one entry and you
    haven't told us which you meant. Send the same request back with `choose`.
    """
    draft: RecipeDraft | None = None
    candidates: list[Candidate] = []
    provenance: PROVENANCE
    matched: list[dict[str, Any]] = []        # per-ingredient pantry match + score
    unmatched: list[str] = []
    warnings: list[str] = []


# ── the assistant proposes; you approve; Python applies ─────────────────────
# No model output is ever written anywhere. It returns a Proposal, the browser
# renders it as a diff, and only your click turns it into real writes through
# the same endpoints you'd use by hand. This schema is the enforcement point:
# anything that doesn't fit is rejected before you ever see it.
class PantryOp(BaseModel):
    op: Literal["add", "set_qty", "adjust", "set_field", "remove"]
    id: str | None = None                     # existing pantry row
    name: str | None = None                   # for `add`, or to identify a row
    qty: float | None = None
    unit: str | None = None
    field: str | None = None                  # for set_field: shelf, expires, bought…
    value: Any = None
    why: str = ""

    @field_validator("field")
    @classmethod
    def _safe_field(cls, v: str | None) -> str | None:
        allowed = {"shelf", "expires", "bought", "unit", "cls", "name", "tare", "note"}
        if v is not None and v not in allowed:
            raise ValueError(f"field must be one of {sorted(allowed)}")
        return v


class ShopOp(BaseModel):
    name: str
    qty: float | None = None
    unit: str | None = None
    why: str = ""


class Idea(BaseModel):
    """A dish it thinks you could make. Deliberately NOT a recipe.

    The model is good at "this sounds like what you're after and you have most
    of it". It is not good at inventing quantities, and a hallucinated 340 g of
    something would flow straight into your logs. So an idea carries a name, a
    reason, and what you'd be missing — and getting the actual recipe is a
    separate, sourced step you take yourself.
    """
    name: str = Field(max_length=90)
    why: str = Field(default="", max_length=280)
    cuisine: str = "Other"
    mins: int = Field(default=30, ge=1, le=1440)
    uses: list[str] = []                      # pantry names it leans on
    missing: list[str] = []                   # what you'd need to buy
    search: str = ""                          # a query that finds a real recipe


class Proposal(BaseModel):
    summary: str
    pantry: list[PantryOp] = []
    shop: list[ShopOp] = []
    draft: RecipeDraft | None = None          # a fully revised draft, to diff
    ideas: list[Idea] = Field(default=[], max_length=8)
    questions: list[str] = []                 # it needs something from you

    def is_empty(self) -> bool:
        return not (self.pantry or self.shop or self.draft or self.ideas)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    scope: Literal["draft", "pantry", "shop", "ideas"] = "draft"
    draft: dict[str, Any] | None = None       # the draft under review, if any
    history: list[dict[str, str]] = []        # [{role, content}], last few turns


class ChatResponse(BaseModel):
    reply: str
    proposal: Proposal | None = None
    matched: list[dict[str, Any]] = []        # if the proposal revised a draft
    unmatched: list[str] = []


class AdaptRequest(BaseModel):
    recipe: dict[str, Any]
    instruction: str = Field(min_length=2, max_length=500)
    pantry_ids: list[str] = []


class StatePut(BaseModel):
    rev: int
    settings: dict[str, Any] = {}
    pantry: list[dict[str, Any]] = []
    custom: list[dict[str, Any]] = []


class QtyAdjust(BaseModel):
    delta: float | None = None
    absolute: float | None = None
    bought: str | None = None
    expires: str | None = None
