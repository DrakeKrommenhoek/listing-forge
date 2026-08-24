"""Tests for hint_parser: extracting structured answers from Dad's one or two
sentence description, so the Telegram flow never re-asks something he already
said.

Deliberately offline and dependency-free (no DB, no vision provider) --
mirrors the deterministic, regex-only nature of the module under test. Python
3.10 compatible, same as the rest of tests/unit/test_estate.py.
"""

from __future__ import annotations

from estate.hint_parser import parse_hint


def test_empty_and_blank_input_resolves_nothing():
    assert parse_hint("").as_answers() == {}
    assert parse_hint("   ").as_answers() == {}
    assert parse_hint(None).as_answers() == {}  # never raises


def test_the_specification_example_sentence():
    text = (
        "Crate & Barrel wall decoration from the dining room. It is in good "
        "condition and can be shipped if needed."
    )
    answers = parse_hint(text).as_answers()
    assert answers["condition"] == "Good"
    assert answers["shipping_feasible"] is True
    assert answers["location_in_house"] == "Dining Room"
    # Ownership is never asserted by this sentence -- must stay unresolved.
    assert "ownership_approval" not in answers


def test_shipping_no_patterns():
    for text in ("Pickup only, too heavy to ship.", "Local pickup only please."):
        answers = parse_hint(text).as_answers()
        assert answers["shipping_feasible"] is False


def test_ownership_requires_an_explicit_statement():
    assert parse_hint("Not sure if I can sell this, might be my sister's.").as_answers()[
        "ownership_approval"
    ] is False
    assert parse_hint("It's mine to sell, no issues there.").as_answers()[
        "ownership_approval"
    ] is True
    # A sentence that says nothing about ownership resolves nothing --
    # conservative by design so the field is never guessed.
    assert "ownership_approval" not in parse_hint("A lamp from the office.").as_answers()


def test_defects_extraction_keeps_only_the_relevant_clause():
    text = "Master bedroom dresser, has a few scratches on top but works fine, pickup only, it's mine to sell."
    answers = parse_hint(text).as_answers()
    assert "scratches" in answers["defects"].lower()
    # The clause about pickup/ownership must not bleed into the defects field.
    assert "pickup" not in answers["defects"].lower()
    assert answers["location_in_house"] == "Master Bedroom"  # not swallowed by "bedroom"
    assert answers["shipping_feasible"] is False
    assert answers["ownership_approval"] is True


def test_condition_phrase_priority_like_new_not_swallowed_by_new():
    assert parse_hint("Like new, barely used.").as_answers()["condition"] == "Like New"
    assert parse_hint("New in the box, never opened.").as_answers()["condition"] == "New / Sealed"


def test_age_extraction_variants():
    assert parse_hint("This is from the 1970s.").as_answers()["approximate_age"] == "1970s"
    assert parse_hint("About 5 years old.").as_answers()["approximate_age"] == "5 years old"
    assert parse_hint("It's vintage.").as_answers()["approximate_age"] == "vintage"


def test_no_false_positive_defects_when_no_keyword_present():
    answers = parse_hint("A blue ceramic vase from the living room, good condition.").as_answers()
    assert "defects" not in answers
