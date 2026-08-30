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


def test_vulgar_fractions_and_long_lines_parse():
    """The lines a real recipe actually contains."""
    from server.assistant import _parse_ing_line
    cases = [
        ("1 Chicken Breast, boneless, skinless. Thinly sliced (Can use thigh as well)", 1, "ea"),
        ("1/4 White Onion, sliced (about 1/4 cup)", 0.25, "ea"),
        ("1 Large Egg, whisked", 1, "ea"),           # not 'l' for litre
        ("1 stalk Green Onion, thinly sliced", 1, "stalk"),
        ("½ cup Dashi Broth (can use Hondashi seasoning)", 0.5, "cup"),
        ("500g bread flour", 500, "g"),              # no space
        ("1 1/2 cups water", 1.5, "cups"),
    ]
    for line, amt, unit in cases:
        got = _parse_ing_line(line)
        assert got is not None, f"dropped: {line}"
        assert abs(got.amt - amt) < 1e-6, (line, got.amt)
        assert got.unit == unit, (line, got.unit)


def test_large_egg_is_not_a_litre():
    """The bug this guards: 'l' matching inside 'Large'."""
    from server.assistant import _parse_ing_line
    got = _parse_ing_line("1 Large Egg, whisked")
    assert got.name.startswith("Large"), got.name


def test_counted_units_do_not_become_grams():
    from server.assistant import to_base
    assert to_base(2, "cloves") == (2, "ea")
    assert to_base(1, "stalk") == (1, "ea")


def test_provider_error_carries_the_reason():
    import httpx
    from server.assistant import ProviderError, _raise_readable
    r = httpx.Response(404, json={"error": {"message": "models/nope is not found"}},
                       request=httpx.Request("POST", "http://x"))
    try:
        _raise_readable(r, "GEMINI_API_KEY", "nope")
        assert False, "should have raised"
    except ProviderError as e:
        assert e.status == 404
        assert "not found" in str(e)
        assert "doesn't exist" in str(e)      # the plain-language hint
