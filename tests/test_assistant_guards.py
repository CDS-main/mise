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


def test_mismatched_model_override_is_ignored(monkeypatch):
    """A Claude model name left in .env must not be sent to Gemini."""
    import importlib
    from server import assistant
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("MISE_MODEL", "claude-sonnet-4-5")
    var, key, url, model = assistant.which_provider()
    assert var == "GEMINI_API_KEY"
    assert model == "gemini-2.5-flash", model
    warn = assistant.provider_warning()
    assert warn and "claude-sonnet-4-5" in warn and "Gemini" in warn


def test_matching_model_override_is_honoured(monkeypatch):
    from server import assistant
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("MISE_MODEL", "gemini-3.7-flash")
    assert assistant.which_provider()[3] == "gemini-3.7-flash"
    assert assistant.provider_warning() is None


def test_unknown_model_names_are_left_alone(monkeypatch):
    """Don't second-guess a name we have no opinion about."""
    from server import assistant
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("MISE_MODEL", "llama-4-maverick")
    assert assistant.which_provider()[3] == "llama-4-maverick"


def test_ingredient_noun_strips_preparation():
    from server.assistant import ingredient_noun
    assert ingredient_noun("White Onion, sliced") == "White Onion"
    assert ingredient_noun("Green Onion, thinly sliced") == "Green Onion"
    assert ingredient_noun("Chicken Breast, boneless, skinless. Thinly sliced") == "Chicken Breast"
    assert ingredient_noun("Bread flour") == "Bread flour"          # nothing to strip
    assert ingredient_noun("sliced") == "sliced"                    # never returns empty


def test_long_ingredient_lines_are_not_demoted_to_steps():
    """The 74-char cap used to turn the chicken into a cooking instruction."""
    from server.assistant import regex_draft
    d = regex_draft(
        "1 Chicken Breast, boneless, skinless. Thinly sliced (Can use thigh as well)\n"
        "1/4 White Onion, sliced (about 1/4 cup)\n"
        "0.5 cup Dashi Broth (can use Hondashi seasoning)\n"
        "\nMix the dashi and simmer the onion in it until soft.")
    ing = d.stages[0].ing
    assert len(ing) == 3, [g.name for g in ing]
    assert ing[0].name.startswith("Chicken Breast")
    assert len(d.stages[0].steps) == 1


def test_transient_statuses_are_retryable_and_permanent_ones_are_not():
    from server.assistant import RETRY_STATUS
    for transient in (429, 500, 502, 503, 504):
        assert transient in RETRY_STATUS
    for permanent in (400, 401, 403, 404, 422):
        assert permanent not in RETRY_STATUS


def test_overload_error_says_it_is_not_your_fault():
    import httpx
    from server.assistant import ProviderError, _raise_readable
    r = httpx.Response(503, json={"error": {"message": "high demand"}},
                       request=httpx.Request("POST", "http://x"))
    try:
        _raise_readable(r, "GEMINI_API_KEY", "gemini-3.6-flash")
        assert False
    except ProviderError as e:
        assert "overloaded" in str(e)
        assert "nothing is wrong with your setup" in str(e)


def test_backoff_grows_and_honours_retry_after():
    import httpx
    from server.assistant import _retry_after
    plain = httpx.Response(503, request=httpx.Request("POST", "http://x"))
    waits = [_retry_after(plain, i) for i in range(3)]
    assert waits == sorted(waits) and waits[0] < waits[-1]
    told = httpx.Response(429, headers={"retry-after": "4"},
                          request=httpx.Request("POST", "http://x"))
    assert _retry_after(told, 0) == 4.0
    absurd = httpx.Response(429, headers={"retry-after": "9999"},
                            request=httpx.Request("POST", "http://x"))
    assert _retry_after(absurd, 0) == 20.0        # capped; never hang the import


def test_reasoning_is_minimised_or_disabled_per_model():
    """Reading amounts off a recipe is extraction, not deliberation.

    Gemini 3.x cannot switch thinking off, so ask for the least. 2.5 can, so
    switch it off entirely. Nothing else gets the parameter at all.
    """
    from server.assistant import _reasoning_effort
    assert _reasoning_effort("gemini-2.5-flash") == "none"
    assert _reasoning_effort("gemini-2.5-flash-lite") == "none"
    assert _reasoning_effort("gemini-3.6-flash") == "low"
    assert _reasoning_effort("gemini-3.5-flash-lite") == "low"
    assert _reasoning_effort("llama-3.3-70b-versatile") is None
    assert _reasoning_effort("gpt-4o-mini") is None
    assert _reasoning_effort("claude-sonnet-4-5") is None


def test_empty_answer_error_reads_as_an_answer_problem_not_an_http_one():
    from server.assistant import ProviderError
    e = ProviderError("Gemini", "gemini-3.6-flash", 200, "it answered with nothing")
    assert "HTTP 200" not in str(e)
    assert str(e) == "it answered with nothing"
    http = ProviderError("Gemini", "x", 404, "no such model")
    assert "HTTP 404" in str(http)


PANTRY = {p: {"id": p, "name": n} for p, n in [
    ("stock", "Chicken stock"), ("thighs", "Chicken thighs"),
    ("yonion", "Yellow onion"), ("eggs", "Eggs"), ("soy", "Soy sauce"),
    ("sesoil", "Sesame oil"), ("sugar", "Caster sugar"),
    ("rice", "Short-grain rice"), ("flour", "Bread flour"),
    ("olive", "Olive oil"), ("butter", "Salted butter"),
    ("toms", "Tinned tomatoes"), ("plain", "Plain flour"),
]}


def test_a_wrong_match_is_worse_than_no_match():
    """These all matched confidently and WRONGLY before the qualifier rule."""
    from server.assistant import match_pantry
    for name in ["Chicken Breast, boneless, skinless. Thinly sliced",
                 "Green Onion, thinly sliced",
                 "White Onion, sliced",
                 "Dashi Broth",
                 "Toasted Sesame Seeds",
                 "unsalted butter"]:
        pid, score = match_pantry(name, PANTRY)
        assert pid is None, f"{name} wrongly matched {PANTRY[pid]['name']} at {score}"


def test_real_matches_still_land():
    from server.assistant import match_pantry
    for name, want in [("Soy Sauce", "soy"), ("Sesame Oil", "sesoil"),
                       ("Large Egg, whisked", "eggs"), ("2 eggs", "eggs"),
                       ("Japanese Short Grain Rice, cooked", "rice"),
                       ("bread flour", "flour"), ("plain flour", "plain"),
                       ("chicken thighs", "thighs"), ("tinned tomatoes", "toms"),
                       ("salted butter", "butter"), ("olive oil", "olive")]:
        pid, score = match_pantry(name, PANTRY)
        assert pid == want, f"{name} -> {pid} (wanted {want}) at {score}"


def test_singular_and_plural_are_the_same_shelf_item():
    from server.assistant import _norm
    assert _norm("Eggs") == _norm("1 Large Egg, whisked").replace("1 ", "")
    assert _norm("tomatoes") == "tomato"


def test_sharing_a_qualifier_group_is_not_a_conflict():
    """Two oils are both oils — that's agreement, not disagreement."""
    from server.assistant import _conflicts
    assert not _conflicts({"sesame", "oil"}, {"olive", "oil"})
    assert _conflicts({"green", "onion"}, {"yellow", "onion"})
    assert _conflicts({"chicken", "breast"}, {"chicken", "stock"})
    assert _conflicts({"unsalted", "butter"}, {"salted", "butter"})
