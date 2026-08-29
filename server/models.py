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
        return v if v in MEDIUMS else "Stove top"


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


class DraftStage(BaseModel):
    name: str
    medium: str = "Stove top"
    vessel: str = "Saucepan"
    needs: list[str] = []                     # names of other stages
    ing: list[DraftIngredient] = []
    steps: list[Step] = []


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


class ImportRequest(BaseModel):
    url: str | None = None
    text: str | None = None
    hint: str | None = None                   # "it's a 2-pan recipe", etc.


class ImportResponse(BaseModel):
    draft: RecipeDraft
    provenance: Literal["json-ld", "youtube-transcript", "page-text", "pasted-text", "regex"]
    matched: list[dict[str, Any]]             # per-ingredient pantry match + score
    unmatched: list[str]
    warnings: list[str] = []


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
