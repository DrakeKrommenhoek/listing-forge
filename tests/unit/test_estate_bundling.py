"""Bundle pricing: the tier table, the bands, and the floor rule.

The floor rule is the one that matters. A bundle discount may never take any
selected item below its human-approved ``floor_price``, and the discount shown
on the public site is computed from a published *band* rather than from the
floor itself, precisely so that the floor is not recoverable by algebra. Both
halves of that are tested here.

Pure arithmetic only — no database, no filesystem, no network.
"""

from __future__ import annotations

import itertools

import pytest

from estate import bundling
from estate import inquiry_validation as iv

TIERS = [
    {"min_items": 2, "discount_pct": 0.05},
    {"min_items": 3, "discount_pct": 0.10},
    {"min_items": 5, "discount_pct": 0.15},
]


def quote(rows, round_to=5):
    return iv.bundle_quote(rows, TIERS, round_to=round_to)


def row(item_id, price, floor):
    """A basket entry built the way the site builds one: band, never floor."""
    return {
        "item_id": item_id,
        "price": price,
        "band": bundling.discount_band(price, floor, TIERS),
    }


# ---------------------------------------------------------------------------
# Tier table
# ---------------------------------------------------------------------------


def test_tier_discount_grows_with_basket_size():
    assert iv.tier_discount_for_count(1, TIERS) == 0
    assert iv.tier_discount_for_count(2, TIERS) == pytest.approx(0.05)
    assert iv.tier_discount_for_count(3, TIERS) == pytest.approx(0.10)
    assert iv.tier_discount_for_count(4, TIERS) == pytest.approx(0.10)
    assert iv.tier_discount_for_count(5, TIERS) == pytest.approx(0.15)
    assert iv.tier_discount_for_count(40, TIERS) == pytest.approx(0.15)


def test_malformed_tiers_are_dropped_not_reinterpreted():
    """A nonsensical tier is a config mistake. Silently turning it into some
    other discount would hide the mistake behind a plausible number."""
    messy = [
        {"min_items": 3, "discount_pct": 0.10},
        {"min_items": 1, "discount_pct": 0.50},  # a "bundle" of one
        {"min_items": 2, "discount_pct": 1.5},  # more than free
        {"min_items": 4, "discount_pct": -0.2},  # negative
        {"min_items": 2, "discount_pct": 0.05},
        "not a dict",
        {"min_items": "x", "discount_pct": "y"},
    ]
    cleaned = iv.normalise_tiers(messy)
    assert cleaned == [
        {"min_items": 2, "discount_pct": 0.05},
        {"min_items": 3, "discount_pct": 0.10},
    ]


def test_bundle_config_falls_back_rather_than_raising(monkeypatch):
    monkeypatch.setattr(
        bundling.estate_settings, "load_config", lambda: {"bundle": "nonsense"}
    )
    config = bundling.bundle_config()
    assert config["tiers"] == iv.normalise_tiers(bundling.DEFAULT_TIERS)
    assert config["max_items"] >= 1


def test_shipped_config_matches_the_documented_defaults():
    """The tiers Drake was told about are the tiers the file actually holds."""
    config = bundling.bundle_config()
    assert config["enabled"] is True
    assert config["tiers"] == TIERS


# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------


def test_band_reflects_how_much_headroom_an_item_has():
    assert bundling.discount_band(100, 99, TIERS) == 0  # 1% of room
    assert bundling.discount_band(100, 95, TIERS) == 1  # exactly 5%
    assert bundling.discount_band(100, 90, TIERS) == 2  # exactly 10%
    assert bundling.discount_band(100, 85, TIERS) == 3  # exactly 15%
    assert bundling.discount_band(100, 40, TIERS) == 3  # plenty


def test_band_boundaries_are_not_lost_to_floating_point():
    """1 - 90/100 is 0.09999999999999998 in binary floating point.

    Without a tolerance an item with exactly 10% of headroom silently drops a
    whole tier. Regression guard for that.
    """
    assert bundling.headroom(100, 90) < 0.10  # the float really is short
    assert bundling.discount_band(100, 90, TIERS) == 2


def test_an_item_without_an_approved_floor_gets_no_headroom():
    """A floor nobody approved is not a licence to discount. It is a gap."""
    assert bundling.headroom(100, None) == 0
    assert bundling.headroom(100, 0) == 0
    assert bundling.discount_band(100, None, TIERS) == 0


def test_an_item_without_a_price_gets_no_headroom():
    assert bundling.headroom(None, 50) == 0
    assert bundling.discount_band(None, 50, TIERS) == 0


def test_a_floor_at_or_above_the_price_gets_no_headroom():
    assert bundling.headroom(100, 100) == 0
    assert bundling.headroom(100, 140) == 0


# ---------------------------------------------------------------------------
# Quoting
# ---------------------------------------------------------------------------


def test_a_single_item_gets_no_bundle_discount():
    q = quote([row("A", 200, 100)])
    assert q["discount_pct"] == 0
    assert q["total"] == 200
    assert q["subtotal"] == 200


def test_an_unconstrained_basket_gets_the_full_tier():
    q = quote([row(str(i), 100, 10) for i in range(5)])
    assert q["discount_pct"] == pytest.approx(0.15)
    assert q["subtotal"] == 500
    assert q["total"] == 425
    assert q["capped_by_floor"] is False


def test_one_constrained_item_caps_the_whole_basket():
    """Five items would earn 15%, but one of them can only bear 5%."""
    rows = [row(str(i), 100, 10) for i in range(4)] + [row("tight", 100, 95)]
    q = quote(rows)
    assert q["tier_discount_pct"] == pytest.approx(0.15)
    assert q["discount_pct"] == pytest.approx(0.05)
    assert q["capped_by_floor"] is True


def test_an_item_that_cannot_be_discounted_at_all_zeroes_the_basket():
    rows = [row(str(i), 100, 10) for i in range(4)] + [row("stuck", 100, 100)]
    q = quote(rows)
    assert q["discount_pct"] == 0
    assert q["total"] == q["subtotal"]
    assert q["capped_by_floor"] is True


def test_an_unpriced_item_pins_the_basket_and_is_named():
    """We cannot promise a percentage off a number we have not published."""
    rows = [row("A", 100, 10), row("B", 200, 10),
            {"item_id": "C", "price": None, "band": 3}]
    q = quote(rows)
    assert q["discount_pct"] == 0
    assert q["subtotal"] == 300
    assert q["unpriced_items"] == ["C"]
    assert q["priced_count"] == 2


def test_the_quote_carries_every_line_a_reply_needs():
    q = quote([row("A", 100, 10), row("B", 250, 10), row("C", 75, 10)])
    assert [r["item_id"] for r in q["items"]] == ["A", "B", "C"]
    assert [r["price"] for r in q["items"]] == [100, 250, 75]
    assert q["count"] == 3
    assert q["subtotal"] == 425
    assert q["discount_pct"] == pytest.approx(0.10)
    assert q["discount_amount"] == pytest.approx(q["subtotal"] - q["total"])


def test_rounding_goes_up_never_down():
    """Rounding a total down could shave dollars off a basket already sitting
    on a floor. Up costs a buyer at most (step - 1) on an indicative figure."""
    q = quote([row("A", 101, 10), row("B", 101, 10)], round_to=5)
    exact = 202 * 0.95
    assert q["total"] >= exact
    assert q["total"] % 5 == 0


def test_rounding_never_exceeds_the_undiscounted_subtotal():
    q = quote([row("A", 3, 1), row("B", 3, 1)], round_to=100)
    assert q["total"] <= q["subtotal"]


def test_an_empty_basket_quotes_nothing():
    q = quote([])
    assert q["count"] == 0
    assert q["total"] == 0
    assert q["discount_pct"] == 0


# ---------------------------------------------------------------------------
# The floor rule, exhaustively
# ---------------------------------------------------------------------------


def test_the_floor_holds_across_a_sweep_of_constrained_baskets():
    """Throw many floor-constrained baskets at it and assert the invariant.

    For every basket, the discount actually applied must leave every single
    item at or above its own approved floor. This is the guarantee the whole
    banding scheme exists to provide, so it is checked by construction over a
    wide space rather than at a handful of hand-picked points.
    """
    prices = [25, 40, 99, 100, 250, 675, 1200]
    ratios = [0.99, 0.95, 0.9, 0.85, 0.8, 0.6, 0.45, 0.2]
    catalogue = [
        (f"DK-202608-{n:03d}", price, round(price * ratio, 2))
        for n, (price, ratio) in enumerate(itertools.product(prices, ratios), start=1)
    ]

    checked = 0
    for size in (2, 3, 4, 5, 6):
        for start in range(0, len(catalogue) - size):
            basket = catalogue[start:start + size]
            q = quote([row(i, p, f) for i, p, f in basket])
            discount = q["discount_pct"]
            for item_id, price, floor in basket:
                effective = price * (1 - discount)
                assert effective >= floor - 1e-6, (
                    item_id, price, floor, discount, effective
                )
            # And the total is never below the sum of the floors either.
            assert q["total"] >= sum(f for _, _, f in basket) - 1e-6
            checked += 1
    assert checked > 100, "the sweep should actually cover a lot of baskets"


def test_the_floor_holds_when_every_item_is_at_its_limit():
    """The pathological case: a basket where nothing has any headroom."""
    basket = [("A", 100, 99.5), ("B", 250, 249), ("C", 40, 39.9)]
    q = quote([row(i, p, f) for i, p, f in basket])
    assert q["discount_pct"] == 0
    for _item_id, price, floor in basket:
        assert price * (1 - q["discount_pct"]) >= floor


def test_a_band_cannot_be_inflated_by_the_caller():
    """A basket entry claiming a generous band is only ever built from the
    manifest, but if one did arrive the quote is still bounded by the tier
    table -- it can never exceed the deepest configured discount."""
    rows = [{"item_id": str(i), "price": 100, "band": 99} for i in range(5)]
    q = iv.bundle_quote(rows, TIERS, round_to=5)
    assert q["discount_pct"] == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# Baskets as untrusted input
# ---------------------------------------------------------------------------


MANIFEST = {
    "DK-202608-001": {"status": "Listed", "sold": False, "price": 100, "band": 3},
    "DK-202608-002": {"status": "Approved", "sold": False, "price": 250, "band": 1},
    "DK-202608-003": {"status": "Sold", "sold": True, "price": 80, "band": 0},
    "DK-202608-004": {"status": "Pickup Scheduled", "sold": False, "price": 60,
                      "band": 2},
}


def test_parse_basket_accepts_a_list_or_a_comma_string():
    assert iv.parse_basket(["DK-202608-001", "DK-202608-002"]) == [
        "DK-202608-001", "DK-202608-002"
    ]
    assert iv.parse_basket("DK-202608-001,DK-202608-002") == [
        "DK-202608-001", "DK-202608-002"
    ]


def test_parse_basket_drops_junk_and_duplicates_and_keeps_order():
    raw = [
        "DK-202608-002", "'; DROP TABLE items; --", "DK-202608-002",
        "../../etc/passwd", "", None, 42, "DK-202608-001",
    ]
    assert iv.parse_basket(raw) == ["DK-202608-002", "DK-202608-001"]


def test_parse_basket_is_bounded():
    huge = [f"DK-202608-{n:03d}" for n in range(1, 500)]
    assert len(iv.parse_basket(huge)) == iv.MAX_BUNDLE_ITEMS


def test_parse_basket_ignores_a_non_sequence():
    assert iv.parse_basket({"a": 1}) == []
    assert iv.parse_basket(None) == []


def test_validate_bundle_accepts_available_items():
    result = iv.validate_bundle(["DK-202608-001", "DK-202608-002"], MANIFEST)
    assert result.ok is True


def test_validate_bundle_rejects_an_unknown_item_and_names_it():
    result = iv.validate_bundle(["DK-202608-001", "DK-202608-999"], MANIFEST)
    assert result.ok is False
    assert "DK-202608-999" in "; ".join(result.errors)


def test_validate_bundle_rejects_a_sold_item_rather_than_dropping_it():
    """Silently selling three of the four things someone asked for, without
    saying which one went, is the small dishonesty that loses a buyer."""
    result = iv.validate_bundle(["DK-202608-001", "DK-202608-003"], MANIFEST)
    assert result.ok is False
    assert result.unavailable is True
    assert "DK-202608-003" in "; ".join(result.errors)


def test_validate_bundle_rejects_a_committed_item():
    result = iv.validate_bundle(["DK-202608-004"], MANIFEST)
    assert result.ok is False
    assert result.unavailable is True


def test_validate_bundle_rejects_an_empty_or_oversized_basket():
    assert iv.validate_bundle([], MANIFEST).ok is False
    too_many = [f"DK-202608-{n:03d}" for n in range(1, 60)]
    assert iv.validate_bundle(too_many, MANIFEST).ok is False


def test_manifest_entries_read_prices_from_the_manifest_not_the_request():
    entries = iv.manifest_entries(["DK-202608-001", "DK-202608-002"], MANIFEST)
    assert entries == [
        {"item_id": "DK-202608-001", "price": 100, "band": 3},
        {"item_id": "DK-202608-002", "price": 250, "band": 1},
    ]


def test_manifest_entries_skips_items_the_manifest_does_not_know():
    assert iv.manifest_entries(["DK-202608-999"], MANIFEST) == []
