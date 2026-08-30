"""The guards that make it safe to let a model near the kitchen data.

Run:  python -m pytest tests/ -q        (pip install pytest first)

These do not call a real model. A stub provider returns whatever payload each
test wants, which is the only way to test the failure modes that matter —
malformed output, hallucinated ids, prose instead of JSON.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest
from pydantic import ValidationError
from server.models import PantryOp, Proposal, RecipeDraft


def test_pantry_field_allowlist():
    """It may set an expiry. It may not reach `qty` through set_field."""
    PantryOp(op="set_field", id="flour", field="expires", value="2026-09-01")
    with pytest.raises(ValidationError):
        PantryOp(op="set_field", id="flour", field="qty", value=99999)


def test_negative_quantity_is_rejected():
    with pytest.raises(ValidationError):
        RecipeDraft(name="X", stages=[{"name": "S",
                                       "ing": [{"name": "flour", "amt": -5, "unit": "g"}],
                                       "steps": []}])


def test_empty_proposal_is_detectable():
    assert Proposal(summary="nothing").is_empty()
    assert not Proposal(summary="x", shop=[{"name": "salt"}]).is_empty()


def test_draft_set_requires_at_least_one_recipe():
    from server.models import RecipeDraftSet
    with pytest.raises(ValidationError):
        RecipeDraftSet(recipes=[])


def test_multi_recipe_json_ld():
    from server.assistant import _all_json_ld
    html = ('<script type="application/ld+json">{"@graph":['
            '{"@type":"Recipe","name":"A","recipeIngredient":["500 g flour"],'
            '"recipeInstructions":"Mix it well."},'
            '{"@type":"Recipe","name":"B","recipeIngredient":["200 g rice"],'
            '"recipeInstructions":"Rinse it well."}]}</script>')
    got = _all_json_ld(html)
    assert [r.name for r in got] == ["A", "B"]
