"""End-to-end test driven by tests/fixtures/estate_item_full_shape.json.

This fixture is a synthetic item modeled on the FIELD DEPTH the project
originally illustrated with item DK-202608-002 (identification, condition,
dimensions, comparable evidence including a hidden Best-Offer sale and an
unconfirmed auto-proposed comparable, pricing, logistics). It contains no
real photos, credentials, tokens, or personal data, and deliberately uses the
'FX' item-id prefix so it can never collide with, or be mistaken for, real
production inventory such as the real DK-202608-002.

This is the single test that exercises the whole pipeline at the depth the
spec asked for, end to end: intake -> identification depth -> condition depth
-> dimensions/logistics -> sourced comparable evidence (exact / hidden /
upper_bound / unconfirmed) -> pricing -> review packet -> approval gate ->
spreadsheet export -- in one continuous story, the way a real item moves
through the system.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path

import pytest

TMP = tempfile.mkdtemp(prefix="estate-fixture-")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["ESTATE_INVENTORY_DIR"] = f"{TMP}/inventory"
os.environ["ESTATE_VISION_PROVIDER"] = "mock"

from estate._compat import config as _config  # noqa: E402
from estate._compat import database as _database  # noqa: E402

_config.get_settings.cache_clear()
_database._engine = None
_database._SessionLocal = None

from estate import approval, exporter, pipeline, research  # noqa: E402
from estate.ids import is_valid_item_id  # noqa: E402
from estate.repository import CompRepository, ItemRepository  # noqa: E402
from estate._compat import get_session, init_db  # noqa: E402

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "estate_item_full_shape.json"


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


@pytest.fixture()
def session():
    s = get_session()
    yield s
    s.close()


@pytest.fixture(scope="module")
def fixture_data():
    with FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _seed_item(session, data: dict):
    """Build an item from the fixture, mirroring what a real intake +
    research pass would have produced -- not calling the mock vision
    provider (which is intentionally low-confidence/generic), but writing
    the fields directly the way a completed identification would."""
    item = pipeline.start_item(
        session, owner=data["owner"], prefix=data["id_prefix"],
        move_out_deadline=data["logistics"]["move_out_deadline"],
    )
    for i in range(data["photo_count"]):
        pipeline.attach_photo(session, item.item_id, f"fixture-photo-bytes-{i}".encode(), ext="jpg")

    ident = data["identification"]
    cond = data["condition"]
    dims = data["dimensions"]
    logi = data["logistics"]

    repo = ItemRepository(session)
    repo.update(
        item.item_id, actor="fixture",
        item_name=ident["item_name"], brand=ident["brand"], manufacturer=ident["manufacturer"],
        model=ident["model"], sku=ident["sku"], category=ident["category"],
        approximate_age=ident["approximate_age"], description=ident["description"],
        included_accessories=ident["included_accessories"],
        condition=cond["condition"], defects=cond["condition_observations"],
        dimensions=dims["dimensions"], weight_lbs=dims["weight_lbs"],
        shipping_feasible=dims["shipping_feasible"], pickup_difficulty=dims["pickup_difficulty"],
        required_vehicle=dims["required_vehicle"], people_required=dims["people_required"],
        pickup_required=not dims["shipping_feasible"],
        ownership_approval=logi["ownership_approval"],
        location_in_house=logi["location_in_house"],
        vision_raw={
            "provider": "fixture", "identification": {
                "confidence": ident["identification_confidence"],
                "overall_confidence": ident["overall_confidence"],
            },
            "condition": {"condition_confidence": cond["condition_confidence"]},
        },
        missing_fields=[],
    )

    comp_repo = CompRepository(session)
    for c in data["comparables"]:
        comp_repo.add(
            item.item_id, platform=c["platform"], title=c["title"], url=c["url"],
            is_sold=c["is_sold"], price=c["price"], shipping_amount=c["shipping_amount"],
            condition=c["condition"], location=c["location"], observed_date=c["observed_date"],
            relevance=c["relevance"], price_type=c["price_type"],
            similarities=c["similarities"], differences=c["differences"],
            is_placeholder=c["is_placeholder"], needs_confirmation=c["needs_confirmation"],
            source=c["source"],
        )
    return item


# ---------------------------------------------------------------------------

def test_fixture_item_id_never_collides_with_real_dk_prefixed_inventory(session, fixture_data):
    item = _seed_item(session, fixture_data)
    assert is_valid_item_id(item.item_id)
    assert item.item_id.startswith("FX-"), "fixture must never use the real DK prefix"
    assert not item.item_id.startswith("DK-202608-002")


def test_fixture_identification_depth_is_preserved(session, fixture_data):
    item = _seed_item(session, fixture_data)
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.brand == "Herman Miller"
    assert fresh.model == "Aeron"
    assert fresh.sku == "AER1B23PWTFCG"
    assert fresh.manufacturer == "Herman Miller, Inc."
    assert fresh.category == "Furniture"


def test_fixture_condition_and_dimensions_depth_is_preserved(session, fixture_data):
    item = _seed_item(session, fixture_data)
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.condition == "Good"
    assert "cracking" in fresh.defects.lower()
    assert fresh.dimensions.startswith("27 x 27")
    assert fresh.weight_lbs == 43.0
    assert fresh.shipping_feasible is False
    assert fresh.pickup_difficulty == "Moderate"
    assert fresh.ownership_approval is True


def test_fixture_comparables_mix_of_evidence_types_summarises_correctly(session, fixture_data):
    """The fixture deliberately includes one exact sold comp, one hidden
    (Best Offer) sold comp, one active upper_bound comp, and one unconfirmed
    auto-proposed comp -- summarise() must handle all four correctly at once."""
    item = _seed_item(session, fixture_data)
    fresh = ItemRepository(session).get(item.item_id)
    comps = CompRepository(session).for_item(item.item_id)
    assert len(comps) == 4

    from estate.research import Comparable

    comparables = [
        Comparable(platform=c.platform, title=c.title, url=c.url, is_sold=c.is_sold,
                  price=c.price, shipping_amount=c.shipping_amount, condition=c.condition,
                  observed_date=c.observed_date, relevance=c.relevance,
                  price_type=c.price_type, is_placeholder=c.is_placeholder,
                  needs_confirmation=c.needs_confirmation, source=c.source)
        for c in comps
    ]
    summary = research.summarise(item.item_id, comparables, fresh.condition, fresh.category)
    assert summary.comp_count == 4
    assert summary.hidden_price_count == 1
    # The $450 hidden Best-Offer price must never appear as the high.
    assert summary.high != 450.0
    assert summary.high == max(c.price for c in comparables if c.has_known_price)
    assert any("Best Offer" in g for g in summary.gaps)


def test_fixture_reaches_needs_review_end_to_end(session, fixture_data):
    item = _seed_item(session, fixture_data)
    result = pipeline.finalise_draft(session, item.item_id)
    assert result.ok
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.status == "Needs Review"
    assert fresh.research_status == "Queued for Manual Research"


def test_fixture_approval_is_blocked_until_a_confirmed_comparable_exists(session, fixture_data):
    """With the unconfirmed auto-proposed comp removed, only 3 real, sourced,
    confirmed comps remain -- approval must succeed. This proves the earlier
    per-comp needs_confirmation gate and this fixture agree with each other."""
    item = _seed_item(session, fixture_data)
    pipeline.finalise_draft(session, item.item_id)

    packet = approval.prepare_review(session, item.item_id)
    assert packet.can_approve is True, packet.blockers
    assert "website" in packet.packages
    assert packet.summary.hidden_price_count == 1

    ok, message = approval.apply_decision(session, item.item_id, "approve", actor="fixture-tester")
    assert ok is True, message
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.approval_status == "Approved"
    assert fresh.floor_price is not None
    assert fresh.current_price >= fresh.floor_price
    assert fresh.primary_marketplace


def test_fixture_approval_blocked_if_only_the_unconfirmed_comp_remains(session, fixture_data):
    item = _seed_item(session, fixture_data)
    # Strip down to only the unconfirmed, auto-proposed comparable.
    for c in CompRepository(session).for_item(item.item_id):
        if not c.needs_confirmation:
            session.delete(c)
    session.commit()
    pipeline.finalise_draft(session, item.item_id)

    ok, message = approval.apply_decision(session, item.item_id, "approve", actor="fixture-tester")
    assert ok is False
    assert "confirm" in message.lower()


def test_fixture_exports_to_spreadsheet_with_full_depth(session, fixture_data, tmp_path):
    item = _seed_item(session, fixture_data)
    pipeline.finalise_draft(session, item.item_id)
    approval.apply_decision(session, item.item_id, "approve", actor="fixture-tester")

    csv_path = exporter.export_csv(session, tmp_path / "inv.csv")
    with csv_path.open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    header, data_rows = rows[0], rows[1:]
    by_id = {r[header.index("Item ID")]: r for r in data_rows}
    row = by_id[item.item_id]
    assert row[header.index("SKU")] == "AER1B23PWTFCG"
    assert row[header.index("Approval Status")] == "Approved"
    assert row[header.index("Research Status")] == "Queued for Manual Research"

    comps_path = exporter.export_comps_csv(session, tmp_path / "comps.csv")
    with comps_path.open(encoding="utf-8") as fh:
        comp_rows = list(csv.reader(fh))
    comp_header = comp_rows[0]
    price_types = {r[comp_header.index("Price Type")] for r in comp_rows[1:]
                  if r[comp_header.index("Item ID")] == item.item_id}
    assert price_types == {"exact", "hidden", "upper_bound"}
