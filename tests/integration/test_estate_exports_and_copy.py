"""Tests for task #18: the Future Only website copy generator, and the
spreadsheet/comparable-evidence export columns it and price_type depend on.

Runs entirely offline against a temporary SQLite file and the mock vision
provider, same pattern as test_estate_pipeline.py.
"""

from __future__ import annotations

import csv
import os
import tempfile

import pytest

TMP = tempfile.mkdtemp(prefix="estate-export-")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["ESTATE_INVENTORY_DIR"] = f"{TMP}/inventory"
os.environ["ESTATE_VISION_PROVIDER"] = "mock"

from estate._compat import config as _config  # noqa: E402
from estate._compat import database as _database  # noqa: E402

_config.get_settings.cache_clear()
_database._engine = None
_database._SessionLocal = None

from estate import approval, exporter, listing, pipeline  # noqa: E402
from estate.repository import (  # noqa: E402
    CompRepository,
    ItemRepository,
    PhotoRepository,
)
from estate.schema import INVENTORY_FIELDS  # noqa: E402
from estate._compat import get_session, init_db  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


@pytest.fixture()
def session():
    s = get_session()
    yield s
    s.close()


def _ready_item(session, photos=4):
    item = pipeline.start_item(session, owner="telegram:1")
    for i in range(photos):
        pipeline.attach_photo(session, item.item_id, b"photo-bytes-%d" % i, ext="jpg")
    ItemRepository(session).update(
        item.item_id, actor="test", item_name="Oak side table", brand="Crate & Barrel",
        category="Furniture", condition="Good", ownership_approval=True, weight_lbs=25,
        dimensions="24 x 24 x 22 in", shipping_feasible=True, pickup_required=False,
        description="A solid oak side table.",
    )
    for i in range(4):
        CompRepository(session).add(
            item.item_id, platform="eBay", title=f"table {i}",
            url=f"https://example.com/{i}", is_sold=True, price=90 + i * 5,
            condition="Good", observed_date="2026-07-15", relevance=0.85,
        )
    return item


# ---------------------------------------------------------------------------
# Website copy generator
# ---------------------------------------------------------------------------

def test_website_copy_has_every_required_field(session):
    item = _ready_item(session)
    photos = PhotoRepository(session).for_item(item.item_id)

    class Proxy:
        pass

    p = Proxy()
    fresh = ItemRepository(session).get(item.item_id)
    for attr in ("item_name", "brand", "model", "category", "condition", "defects",
                 "approximate_age", "dimensions", "weight_lbs", "included_accessories",
                 "description", "shipping_feasible", "pickup_required", "item_id",
                 "people_required", "required_vehicle"):
        setattr(p, attr, getattr(fresh, attr, None))
    p.current_price = 150.0
    p.initial_list_price = 150.0

    copy = listing.build_website_copy(p, photos=photos, catalog_url="https://example.com/cat",
                                      region="Denver")
    assert copy.product_title
    assert copy.subtitle or copy.condition_statement  # subtitle can fall back to condition
    assert copy.description
    assert copy.condition_statement
    assert copy.dimensions == "24 x 24 x 22 in"
    assert copy.shipping_statement
    assert copy.website_price == 150.0
    assert copy.category == "Furniture"
    assert copy.search_tags
    assert copy.image_order  # photos were attached
    assert copy.hero_image
    assert copy.bundle_statement
    assert copy.contact_cta and item.item_id in copy.contact_cta
    assert copy.warnings == []


def test_website_copy_flags_missing_dimensions_and_unknown_condition():
    class Item:
        item_id = "DK-TEST-001"
        item_name = "Mystery Box"
        brand = ""
        model = ""
        category = "Other"
        condition = "Unknown"
        defects = ""
        approximate_age = ""
        dimensions = ""
        weight_lbs = None
        included_accessories = ""
        description = ""
        shipping_feasible = False
        pickup_required = True
        people_required = 1
        required_vehicle = ""
        current_price = None
        initial_list_price = None

    copy = listing.build_website_copy(Item(), photos=[])
    assert any("Unknown" in w for w in copy.warnings)
    assert any("photos" in w.lower() for w in copy.warnings)
    assert any("dimensions" in w.lower() for w in copy.warnings)


def test_website_copy_honours_hero_photo_flag():
    class Photo:
        def __init__(self, filename, is_hero=False, sort_order=0):
            self.filename = filename
            self.is_hero = is_hero
            self.sort_order = sort_order

    photos = [Photo("a.jpg", sort_order=0), Photo("b.jpg", is_hero=True, sort_order=1),
             Photo("c.jpg", sort_order=2)]

    class Item:
        item_id = "DK-TEST-002"
        item_name = "Chair"
        brand = ""
        model = ""
        category = "Furniture"
        condition = "Good"
        defects = ""
        approximate_age = ""
        dimensions = "20x20x30"
        weight_lbs = 10
        included_accessories = ""
        description = "A chair."
        shipping_feasible = True
        pickup_required = False
        people_required = 1
        required_vehicle = ""
        current_price = 50.0
        initial_list_price = 50.0

    copy = listing.build_website_copy(Item(), photos=photos)
    assert copy.hero_image == "b.jpg"
    assert copy.image_order[0] == "b.jpg"  # hero always sorts first
    assert set(copy.image_order) == {"a.jpg", "b.jpg", "c.jpg"}


def test_website_copy_never_published_for_mock_items():
    class Item:
        item_id = "DK-TEST-003"
        item_name = "[MOCK] Sample Lamp"
        brand = ""
        model = ""
        category = "Home Decor"
        condition = "Good"
        defects = ""
        approximate_age = ""
        dimensions = "10x10x20"
        weight_lbs = 5
        included_accessories = ""
        description = ""
        shipping_feasible = True
        pickup_required = False
        people_required = 1
        required_vehicle = ""
        current_price = 20.0
        initial_list_price = 20.0

    copy = listing.build_website_copy(Item(), photos=[])
    assert any("MOCK" in w for w in copy.warnings)


def test_website_copy_markdown_and_dict_round_trip(session):
    item = _ready_item(session)
    photos = PhotoRepository(session).for_item(item.item_id)
    fresh = ItemRepository(session).get(item.item_id)
    copy = listing.build_website_copy(fresh, photos=photos)
    md = copy.to_markdown()
    assert copy.product_title in md
    assert copy.contact_cta in md
    d = copy.to_dict()
    assert d["website_price"] == copy.website_price


def test_prepare_review_always_includes_a_website_package(session):
    """Every reviewed item must carry the website copy alongside marketplace
    packages, whether or not a marketplace was recommended."""
    item = _ready_item(session)
    packet = approval.prepare_review(session, item.item_id)
    assert "website" in packet.packages
    assert isinstance(packet.packages["website"], listing.WebsiteCopy)


def test_approval_does_not_crash_when_website_is_the_only_package(session, monkeypatch):
    """Regression guard: apply_decision's listing_title/description/keywords
    extraction must skip the 'website' package (a WebsiteCopy, not a
    ListingPackage -- no .title attribute) even if it is the only entry."""
    from estate import marketplaces

    item = _ready_item(session)
    monkeypatch.setattr(marketplaces, "recommend", lambda proxy: {"primary": None, "secondary": []})
    ok, message = approval.apply_decision(session, item.item_id, "approve", actor="tester")
    assert ok is True, message
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.approval_status == "Approved"


# ---------------------------------------------------------------------------
# Spreadsheet export
# ---------------------------------------------------------------------------

def test_exported_csv_contains_every_user_specified_column(session, tmp_path):
    """The exhaustive spreadsheet field list from the spec, checked against
    what actually gets written -- not just what schema.py declares."""
    required_labels = {
        "Item ID", "Item Name", "Category", "Brand", "Model", "SKU", "Description",
        "Condition", "Defects", "Dimensions", "Weight (lbs)", "Location in House",
        "Photo Links", "Date Submitted", "Move-Out Deadline", "Shipping Feasibility",
        "Pickup Difficulty", "Comparable Low Price", "Comparable Median Price",
        "Comparable High Price", "Comparable Sample Size", "Sold Comparable Count",
        "Comparable Source Links", "Pricing Confidence", "Initial List Price",
        "Expected Selling Price", "Floor Price", "Current Price", "Pickup Incentive",
        "Approved Pickup Price", "Best Primary Marketplace", "Secondary Marketplaces",
        "Listing Title", "Listing Description", "Approval Status", "Research Status",
        "Website Status", "Notes",
    }
    labels = {f.label for f in INVENTORY_FIELDS}
    assert required_labels.issubset(labels)

    item = _ready_item(session)
    out = exporter.export_csv(session, tmp_path / "inv.csv")
    with out.open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    header, data_rows = rows[0], rows[1:]
    assert required_labels.issubset(set(header))
    ids = [r[header.index("Item ID")] for r in data_rows]
    assert item.item_id in ids


def test_comps_export_includes_price_type_and_confirmation_columns(session, tmp_path):
    item = _ready_item(session)
    CompRepository(session).add(
        item.item_id, platform="eBay", title="hidden sale", url="https://example.com/hidden",
        is_sold=True, price=999, price_type="hidden", condition="Good",
        observed_date="2026-07-01", relevance=0.8,
    )
    CompRepository(session).add(
        item.item_id, platform="eBay", title="auto proposed", url="https://example.com/auto",
        is_sold=True, price=80, needs_confirmation=True, condition="Good",
        observed_date="2026-07-01", relevance=0.9,
    )
    out = exporter.export_comps_csv(session, tmp_path / "comps.csv")
    with out.open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    header = rows[0]
    assert "Price Type" in header
    assert "Needs Confirmation?" in header
    by_title = {r[header.index("Title")]: r for r in rows[1:]}
    assert by_title["hidden sale"][header.index("Price Type")] == "hidden"
    assert by_title["auto proposed"][header.index("Needs Confirmation?")] == "YES"


def test_xlsx_export_builds_without_error_and_includes_new_sheets_and_columns(session, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    item = _ready_item(session)
    CompRepository(session).add(
        item.item_id, platform="eBay", title="hidden sale", url="https://example.com/hidden2",
        is_sold=True, price=999, price_type="hidden", condition="Good",
        observed_date="2026-07-01", relevance=0.8,
    )
    out = exporter.export_xlsx(session, tmp_path / "inv.xlsx")
    wb = openpyxl.load_workbook(out)
    assert "Comparable Evidence" in wb.sheetnames
    ws = wb["Comparable Evidence"]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    assert "Price Type" in header
    assert "Needs Confirmation?" in header
