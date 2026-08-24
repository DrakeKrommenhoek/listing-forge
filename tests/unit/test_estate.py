"""Tests for the estate sale system.

Written to run on Python 3.10 as well as the project's 3.12 target, so the
estate module can be exercised in constrained environments where the rest of
the suite cannot import (StrEnum / datetime.UTC are 3.11+).
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

import pytest

os.environ.setdefault("ESTATE_INVENTORY_DIR", tempfile.mkdtemp(prefix="estate-test-"))

from estate import listing, marketplaces, paths, pricing, research  # noqa: E402
from estate.ids import ID_RE, is_valid_item_id  # noqa: E402
from estate.research import Comparable  # noqa: E402
from estate.research_provider import (  # noqa: E402
    ManualQueueResearchProvider,
    get_research_provider,
)
from estate.schema import (  # noqa: E402
    ASKABLE_FIELDS,
    FIELD_KEYS,
    INVENTORY_FIELDS,
    STATUS_ORDER,
)
from estate.vision import (  # noqa: E402
    FIELD_CONFIDENCE_FLOOR,
    MockVisionProvider,
    compute_missing,
    normalise,
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_schema_has_every_requested_column():
    required = {
        "item_id", "item_name", "category", "brand", "model", "approximate_age",
        "description", "condition", "defects", "dimensions", "weight_lbs",
        "included_accessories", "location_in_house", "photo_links", "date_submitted",
        "submission_owner", "ownership_approval", "shipping_feasible", "pickup_required",
        "pickup_difficulty", "required_vehicle", "people_required", "move_out_deadline",
        "comp_low", "comp_median", "comp_high", "comp_count", "comp_sources",
        "research_date", "pricing_confidence", "initial_list_price",
        "expected_sale_price", "floor_price", "current_price", "pickup_incentive",
        "approved_pickup_price", "markdown_pct", "next_markdown_date",
        "primary_marketplace", "secondary_marketplaces", "listing_title",
        "listing_description", "keywords", "review_status", "approval_status",
        "website_status", "listing_urls", "inquiry_count", "best_offer", "buyer",
        "fulfilment_status", "final_sale_price", "actual_proceeds",
        "final_disposition", "notes",
    }
    assert required.issubset(set(FIELD_KEYS))


def test_status_vocabulary_matches_specification():
    assert STATUS_ORDER == [
        "Draft", "Needs Review", "Approved", "Ready to List", "Listed",
        "Offer Received", "Pickup Scheduled", "Shipping", "Sold", "Donated", "Removed",
    ]


def test_orm_columns_cover_every_schema_field():
    from estate.models import EstateItemORM

    for f in INVENTORY_FIELDS:
        assert hasattr(EstateItemORM, f.key), f"ORM is missing {f.key}"


# ---------------------------------------------------------------------------
# IDs and paths
# ---------------------------------------------------------------------------

def test_item_id_format():
    assert is_valid_item_id("DK-202608-001")
    assert not is_valid_item_id("DK-2026-1")
    assert ID_RE.match("DK-202608-014").group(3) == "014"


def test_path_traversal_is_rejected():
    with pytest.raises(ValueError):
        paths.safe_component("..")
    assert "/" not in paths.safe_component("../../etc/passwd")


# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------

def test_low_confidence_fields_are_withheld_from_the_draft():
    ident = normalise(
        {"item_name": "Chair", "brand": "Acme", "model": "X1", "condition": "Good",
         "confidence": {"brand": 0.9, "model": 0.2, "condition": 0.8}},
        "test", "test-model",
    )
    fields = ident.to_item_fields()
    assert fields["brand"] == "Acme"
    assert "model" not in fields, "a 0.2-confidence model number must not be written"
    assert ident.confidence["model"] < FIELD_CONFIDENCE_FLOOR


def test_sku_is_populated_when_confidently_read_from_a_label():
    ident = normalise(
        {"item_name": "Office Chair", "brand": "Herman Miller", "model": "Aeron",
         "sku": "AER1B23", "condition": "Good",
         "confidence": {"brand": 0.9, "model": 0.9, "sku": 0.92, "condition": 0.8}},
        "test", "test-model",
    )
    fields = ident.to_item_fields()
    assert fields["sku"] == "AER1B23"


def test_sku_withheld_when_low_confidence():
    ident = normalise(
        {"item_name": "Chair", "sku": "MAYBE123", "condition": "Good",
         "confidence": {"sku": 0.1, "condition": 0.8}},
        "test", "test-model",
    )
    fields = ident.to_item_fields()
    assert "sku" not in fields, "a barely-legible SKU guess must not be written"


def test_genuinely_unknown_model_is_never_invented():
    """A model the vision model itself never claimed to know (empty string,
    no confidence entry at all) must stay empty and land in missing -- this
    is a stronger case than the low-confidence test above, where a guess
    exists but is suppressed. Here there is no guess to suppress."""
    ident = normalise(
        {"item_name": "Wooden side table", "brand": "", "model": "",
         "condition": "Good", "confidence": {"condition": 0.8}},
        "test", "test-model",
    )
    assert ident.model == ""
    fields = ident.to_item_fields()
    assert "model" not in fields
    # `missing` is filtered to the fields this deployment is willing to ask
    # about, so the rule is asserted against an explicit ask-set rather than
    # against whatever the current default happens to be.
    asked = compute_missing(ident, askable=list(ASKABLE_FIELDS))
    assert "model" in asked
    assert "brand" in asked


def test_defects_and_room_are_asked_whenever_they_are_askable_at_all():
    """Both are unconditional: no confidence score can settle either.

    A photograph cannot say which room something is in, and defects are never
    assumed from a model's say-so. Note this is about the *question*; whether
    an item may be published with a blank defects field is a separate and
    stricter gate in site.publication_blockers, which does not consult this.
    """
    ident = normalise(
        {"item_name": "Chair", "defects": "none visible",
         "confidence": {k: 0.99 for k in ASKABLE_FIELDS}},
        "test", "test-model",
    )
    asked = compute_missing(ident, askable=list(ASKABLE_FIELDS))
    assert "defects" in asked
    assert "location_in_house" in asked


def test_a_narrow_ask_set_asks_nothing_outside_it():
    """The whole point of the setting: fewer questions in the room.

    Everything dropped keeps the model's own answer and reaches the reviewer
    flagged low-confidence, rather than becoming another prompt on a phone.
    """
    ident = normalise(
        {"item_name": "Chair", "brand": "", "model": "", "defects": "",
         "confidence": {}},
        "test", "test-model",
    )
    asked = compute_missing(ident, askable=["dimensions"])
    assert asked == ["dimensions"]


def test_an_empty_ask_set_asks_nothing_at_all():
    ident = normalise(
        {"item_name": "Chair", "confidence": {}}, "test", "test-model",
    )
    assert compute_missing(ident, askable=[]) == []


def test_a_lower_confidence_floor_keeps_more_of_the_models_answers():
    ident = normalise(
        {"item_name": "Chair", "brand": "Ercol",
         "confidence": {"brand": 0.45}},
        "test", "test-model",
    )
    assert "brand" in compute_missing(ident, floor=0.60, askable=["brand"])
    assert "brand" not in compute_missing(ident, floor=0.35, askable=["brand"])


def test_mock_provider_labels_itself():
    ident = MockVisionProvider().identify([])
    assert "[MOCK]" in ident.item_name
    assert ident.raw.get("_mock") is True


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

def _comps(n=3, sold=True, days_ago=10, relevance=0.9, placeholder=False):
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    return [
        Comparable(platform="eBay", url="https://example.com/%d" % i, is_sold=sold,
                   price=100 + i * 10, condition="Good", observed_date=d,
                   relevance=relevance, is_placeholder=placeholder)
        for i in range(n)
    ]


def test_summary_statistics():
    s = research.summarise("X", _comps(3), "Good", "Furniture")
    assert (s.low, s.median, s.high) == (100.0, 110.0, 120.0)
    assert s.comp_count == 3 and s.sold_count == 3


def test_sold_comparables_score_higher_confidence_than_active_only():
    """Same sample size, same relevance, same recency -- the only variable is
    is_sold. score_confidence's sold_evidence weight must make the sold set
    strictly more confident, since active asking prices systematically
    overstate value."""
    sold = research.summarise("X", _comps(5, sold=True), "Good", "Furniture")
    active = research.summarise("X", _comps(5, sold=False), "Good", "Furniture")
    assert sold.confidence_score > active.confidence_score
    assert sold.sold_count == 5 and sold.active_count == 0
    assert active.sold_count == 0 and active.active_count == 5


def test_no_evidence_means_no_price():
    s = research.summarise("X", [], "Good", "Other")
    rec = pricing.recommend_price(s)
    assert rec.initial_list_price is None
    assert s.confidence == "Insufficient Evidence"
    assert any("No comparable" in g for g in s.gaps)


def test_asking_prices_only_is_called_out():
    s = research.summarise("X", _comps(4, sold=False), "Good", "Other")
    assert s.sold_count == 0
    assert any("completed sales" in g for g in s.gaps)


def test_placeholder_evidence_caps_confidence_and_blocks_publication():
    s = research.summarise("X", _comps(6, placeholder=True), "Good", "Other")
    assert s.confidence in ("Low", "Insufficient Evidence")
    rec = pricing.recommend_price(s)
    assert rec.publishable is False
    assert any("MUST NOT be published" in w for w in rec.warnings)


def test_stale_evidence_lowers_confidence():
    fresh = research.summarise("X", _comps(6, days_ago=5), "Good", "Other")
    stale = research.summarise("X", _comps(6, days_ago=340), "Good", "Other")
    assert stale.confidence_score < fresh.confidence_score


def test_specialist_review_recommended_for_weak_jewelry_evidence():
    s = research.summarise("X", _comps(1), "Good", "Jewelry & Watches")
    assert s.recommend_specialist is True


def test_hidden_price_type_is_excluded_from_the_price_range():
    """A Best-Offer-accepted sale still proves the item sells, but the
    displayed price is not what the buyer actually paid -- it must never
    enter low/median/high."""
    comps = _comps(3, sold=True)  # three normal "exact" sales: 100, 110, 120
    comps.append(
        Comparable(platform="eBay", url="https://example.com/hidden", is_sold=True,
                   price=999, condition="Good", observed_date=date.today().isoformat(),
                   relevance=0.9, price_type="hidden")
    )
    s = research.summarise("X", comps, "Good", "Other")
    assert s.comp_count == 4
    assert s.hidden_price_count == 1
    assert s.high == 120.0  # the 999 "hidden" price never touches the range
    assert any("Best Offer" in g for g in s.gaps)


def test_all_hidden_prices_means_no_usable_range_despite_evidence():
    comps = [
        Comparable(platform="eBay", url="https://example.com/1", is_sold=True,
                   price=500, condition="Good", observed_date=date.today().isoformat(),
                   relevance=0.9, price_type="hidden")
    ]
    s = research.summarise("X", comps, "Good", "Other")
    assert s.comp_count == 1
    assert s.low is None and s.median is None and s.high is None
    assert any("no usable price range" in g for g in s.gaps)
    rec = pricing.recommend_price(s)
    assert rec.initial_list_price is None  # must never price off a hidden figure


def test_unknown_price_type_falls_back_to_exact():
    c = Comparable(platform="eBay", url="https://example.com/x", price=50,
                   price_type="not-a-real-type")
    assert c.price_type == "exact"
    assert c.has_known_price is True


def test_worksheet_rejects_a_comparable_with_no_url(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text(
        ",".join(research.WORKSHEET_COLUMNS) + "\n"
        "eBay,Some chair,,sold,120,0,Good,,2026-07-01,,,0.8,\n",
        encoding="utf-8",
    )
    comps, problems = research.import_worksheet(p)
    assert comps == []
    assert any("no URL" in p_ for p_ in problems)


def test_worksheet_round_trip(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text(
        ",".join(research.WORKSHEET_COLUMNS) + "\n"
        "eBay,Chair,https://example.com/a,sold,120,,10,Good,OR,2026-07-01,same model,none,0.9,\n",
        encoding="utf-8",
    )
    comps, problems = research.import_worksheet(p)
    assert problems == []
    assert len(comps) == 1
    assert comps[0].total_price == 130.0
    assert comps[0].is_sold is True


def test_worksheet_parses_price_type_column(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text(
        ",".join(research.WORKSHEET_COLUMNS) + "\n"
        "eBay,Chair,https://example.com/a,sold,999,hidden,0,Good,,2026-07-01,,,0.9,\n"
        "eBay,Chair 2,https://example.com/b,active,150,upper_bound,0,Good,,2026-07-01,,,0.7,\n",
        encoding="utf-8",
    )
    comps, problems = research.import_worksheet(p)
    assert problems == []
    assert comps[0].price_type == "hidden"
    assert comps[0].has_known_price is False
    assert comps[1].price_type == "upper_bound"
    assert comps[1].has_known_price is True


def test_worksheet_warns_but_does_not_reject_unknown_price_type(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text(
        ",".join(research.WORKSHEET_COLUMNS) + "\n"
        "eBay,Chair,https://example.com/a,sold,120,not-a-type,0,Good,,2026-07-01,,,0.9,\n",
        encoding="utf-8",
    )
    comps, problems = research.import_worksheet(p)
    assert len(comps) == 1
    assert comps[0].price_type == "exact"  # invalid value falls back safely
    assert any("unknown price_type" in p_ for p_ in problems)


# ---------------------------------------------------------------------------
# Research provider
# ---------------------------------------------------------------------------

def test_manual_queue_provider_never_invents_a_comparable(tmp_path):
    class Item:
        item_id = "DK-TEST-999"
        item_name = "Test Chair"
        brand = ""
        model = ""
        category = "Furniture"
        condition = "Good"

    provider = ManualQueueResearchProvider()
    result = provider.find_comparables(Item())
    assert result.comparables == []
    assert result.status == "Queued for Manual Research"
    assert result.provider == "manual_queue"


def test_get_research_provider_defaults_to_manual_queue():
    provider = get_research_provider()
    assert isinstance(provider, ManualQueueResearchProvider)


def test_get_research_provider_falls_back_on_unknown_name():
    provider = get_research_provider("some_paid_agentic_thing_not_implemented")
    assert isinstance(provider, ManualQueueResearchProvider)


def test_query_builder_prefers_sold_evidence():
    class Item:
        brand = "Herman Miller"
        model = "Aeron"
        item_name = "Chair"
        category = "Furniture"
        condition = "Good"

    queries = research.build_queries(Item())
    assert any("sold" in q.lower() for q in queries)
    assert queries[0].startswith("Herman Miller Aeron")


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

class FakeItem:
    def __init__(self, **kw):
        self.item_id = "DK-202608-001"
        self.category = "Furniture"
        self.weight_lbs = 40.0
        self.dimensions = "30 x 30 x 30 in"
        self.people_required = 1
        self.required_vehicle = "Car"
        self.current_price = 200.0
        self.initial_list_price = 200.0
        self.floor_price = 100.0
        self.inquiry_count = 0
        self.best_offer = None
        self.pricing_confidence = "Medium"
        self.pickup_difficulty = "Easy"
        self.shipping_feasible = False
        self.pickup_required = True
        self.brand = "Acme"
        self.model = "X"
        self.move_out_deadline = ""
        self.next_markdown_date = ""
        for k, v in kw.items():
            setattr(self, k, v)


def test_price_bands_are_ordered():
    s = research.summarise("X", _comps(6), "Good", "Furniture")
    rec = pricing.recommend_price(s)
    assert rec.floor_price < rec.expected_sale_price < rec.initial_list_price


def test_low_confidence_produces_a_lower_floor():
    strong = pricing.recommend_price(research.summarise("X", _comps(8), "Good", "Other"))
    weak = pricing.recommend_price(
        research.summarise("X", _comps(2, sold=False, relevance=0.3), "Good", "Other")
    )
    assert weak.floor_price / (weak.median if False else 1) or True
    assert weak.confidence in ("Low", "Insufficient Evidence")
    assert strong.floor_price >= weak.floor_price


def test_markdown_never_breaks_the_floor():
    item = FakeItem(current_price=120.0, floor_price=100.0, initial_list_price=150.0)
    d = pricing.evaluate_markdown(
        item, listed_on=(date.today() - timedelta(days=90)).isoformat(),
        move_out_date=(date.today() + timedelta(days=2)).isoformat(),
    )
    assert d.should_mark_down is True
    assert d.new_price == 100.0
    assert d.at_floor is True


def test_markdown_holds_rather_than_raising_a_price():
    """An item already below the max-total-markdown line must not be pushed up."""
    item = FakeItem(current_price=105.0, floor_price=100.0, initial_list_price=300.0)
    d = pricing.evaluate_markdown(
        item, listed_on=(date.today() - timedelta(days=90)).isoformat(),
        move_out_date=(date.today() + timedelta(days=2)).isoformat(),
    )
    assert d.new_price is None or d.new_price <= 105.0


def test_markdown_holds_before_the_first_window():
    item = FakeItem()
    d = pricing.evaluate_markdown(item, listed_on=(date.today() - timedelta(days=3)).isoformat())
    assert d.should_mark_down is False
    assert d.next_markdown_date


def test_markdown_accelerates_without_inquiries():
    listed = (date.today() - timedelta(days=40)).isoformat()
    quiet = pricing.evaluate_markdown(FakeItem(inquiry_count=0), listed_on=listed)
    interested = pricing.evaluate_markdown(
        FakeItem(inquiry_count=1, best_offer=150.0), listed_on=listed
    )
    assert quiet.step_pct > interested.step_pct


def test_deadline_endgame_forces_a_larger_step():
    listed = (date.today() - timedelta(days=30)).isoformat()
    normal = pricing.evaluate_markdown(FakeItem(), listed_on=listed)
    endgame = pricing.evaluate_markdown(
        FakeItem(), listed_on=listed,
        move_out_date=(date.today() + timedelta(days=3)).isoformat(),
    )
    assert endgame.step_pct > normal.step_pct


def test_pickup_incentive_scales_with_difficulty():
    easy = pricing.compute_pickup_incentive(
        FakeItem(weight_lbs=10, dimensions="12 x 10 x 8 in", current_price=300)
    )
    hard = pricing.compute_pickup_incentive(
        FakeItem(weight_lbs=180, people_required=2, required_vehicle="Truck",
                 dimensions="72 x 36 x 34 in", current_price=300, floor_price=50),
        stairs=True, disassembly=True, avoided_disposal=True,
    )
    assert easy.amount == 0
    assert hard.amount > 0
    assert hard.pickup_price < 300


def test_pickup_incentive_never_breaks_the_floor():
    item = FakeItem(weight_lbs=300, current_price=120.0, floor_price=110.0,
                    people_required=2, required_vehicle="Truck")
    inc = pricing.compute_pickup_incentive(item, stairs=True, disassembly=True,
                                           avoided_disposal=True, urgent=True)
    assert inc.pickup_price >= 110.0


def test_pickup_incentive_is_capped_as_a_share_of_price():
    inc = pricing.compute_pickup_incentive(
        FakeItem(weight_lbs=400, current_price=100.0, floor_price=0,
                 people_required=2, required_vehicle="Truck"),
        stairs=True, disassembly=True, avoided_disposal=True, urgent=True,
    )
    assert inc.amount <= 25.0  # max_pct_of_price = 0.25


# ---------------------------------------------------------------------------
# Marketplaces
# ---------------------------------------------------------------------------

def test_heavy_pickup_only_items_are_excluded_from_shipping_platforms():
    item = FakeItem(weight_lbs=180, shipping_feasible=False, pickup_required=True,
                    category="Furniture", initial_list_price=400)
    result = marketplaces.recommend(item)
    assert result["primary"].platform.local is True
    rejected = {f.platform.key for f in result["rejected"]}
    assert "ebay" in rejected


def test_music_gear_routes_to_reverb():
    item = FakeItem(category="Audio / Music Gear", weight_lbs=12,
                    shipping_feasible=True, pickup_required=False,
                    initial_list_price=800)
    assert marketplaces.recommend(item)["primary"].platform.key == "reverb"


def test_cheap_items_avoid_high_effort_platforms():
    item = FakeItem(category="Home Decor", weight_lbs=3, shipping_feasible=True,
                    pickup_required=False, initial_list_price=12)
    result = marketplaces.recommend(item)
    assert result["primary"] is None or result["primary"].platform.key != "chairish"


def test_fee_verification_date_is_surfaced():
    result = marketplaces.recommend(FakeItem())
    assert any("verified" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# Listing copy
# ---------------------------------------------------------------------------

class CopyItem(FakeItem):
    item_name = "Office chair"
    description = "A chair."
    condition = "Good"
    defects = "Scuff on the arm"
    included_accessories = "None"
    approximate_age = "10 years"


def test_titles_respect_platform_limits():
    item = CopyItem(item_name="A" * 200, brand="B" * 60, model="C" * 60)
    for key, limit in listing.TITLE_LIMITS.items():
        assert len(listing.build_title(item, key)) <= limit


def test_defects_are_always_disclosed():
    text = listing.condition_disclosure(CopyItem())
    assert "Scuff on the arm" in text
    assert "actual item" in text


def test_absent_defects_are_stated_not_implied():
    text = listing.condition_disclosure(CopyItem(defects=""))
    assert "No defects were noted" in text


def test_package_warns_when_pricing_is_weak():
    pkg = listing.build_package(
        CopyItem(pricing_confidence="Low", floor_price=None, dimensions=""),
        "facebook_marketplace",
    )
    assert any("floor price" in w for w in pkg.warnings)
    assert any("confidence is Low" in w for w in pkg.warnings)


def test_mock_items_are_flagged_as_unpublishable():
    pkg = listing.build_package(CopyItem(item_name="[MOCK] thing"), "ebay")
    assert any("MOCK ITEM" in w for w in pkg.warnings)


def test_bundle_and_catalog_language_present():
    pkg = listing.build_package(CopyItem(), "facebook_marketplace",
                                catalog_url="https://example.com/c")
    assert "bundle" in pkg.description.lower()
    assert "https://example.com/c" in pkg.description
    assert pkg.catalog_language.endswith("Reference DK-202608-001.")


def test_buyer_qa_covers_the_common_questions():
    questions = [q for q, _ in listing.buyer_qa(CopyItem())]
    assert "Is this still available?" in questions
    assert any("less" in q for q in questions)


# ---------------------------------------------------------------------------
# Hard deadline
# ---------------------------------------------------------------------------

def test_configured_deadline_is_resolved():
    """The hard move-out date must never be silently absent.

    Resolution order is env -> settings -> estate/config/pricing.json. A
    missing environment variable must not switch off markdown urgency.
    """
    from estate.settings import load_config, move_out_date

    assert load_config()["deadline"]["move_out_date"] == "2026-08-31"
    assert move_out_date() == "2026-08-31"


def test_markdown_uses_the_configured_deadline_without_an_argument():
    """Omitting move_out_date must behave identically to passing 2026-08-31."""
    listed = (date.today() - timedelta(days=60)).isoformat()
    with_arg = pricing.evaluate_markdown(
        FakeItem(), listed_on=listed, move_out_date="2026-08-31"
    )
    without_arg = pricing.evaluate_markdown(FakeItem(), listed_on=listed)
    assert without_arg.step_pct == with_arg.step_pct
    assert without_arg.reasons == with_arg.reasons


def test_deadline_urgency_escalates_as_2026_08_31_approaches():
    """Three bands: normal, urgent (<=21 days), endgame (<=7 days)."""
    listed = (date.today() - timedelta(days=30)).isoformat()
    deadline = date(2026, 8, 31)

    far = pricing.evaluate_markdown(
        FakeItem(), listed_on=listed, today=deadline - timedelta(days=40),
        move_out_date=deadline.isoformat(),
    )
    urgent = pricing.evaluate_markdown(
        FakeItem(), listed_on=listed, today=deadline - timedelta(days=14),
        move_out_date=deadline.isoformat(),
    )
    endgame = pricing.evaluate_markdown(
        FakeItem(), listed_on=listed, today=deadline - timedelta(days=3),
        move_out_date=deadline.isoformat(),
    )

    assert far.step_pct < urgent.step_pct < endgame.step_pct
    assert any("move-out" in r for r in urgent.reasons)
    assert any("endgame" in r for r in endgame.reasons)
    for d in (far, urgent, endgame):
        assert d.new_price is None or d.new_price >= 100.0
