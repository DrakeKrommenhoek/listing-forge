"""The catalogue a stranger actually lands on.

Covers the shop-window milestone: category navigation, search and sort, cards
that say how you get the thing home, sold items kept visible as social proof,
item pages that respect the generated image order and state the defects
plainly, share-preview tags so a pasted link unfurls, an honest empty state,
and the publication gates that decide what is allowed on the page at all.

Also carries the regression test for the stored-XSS hole the previous
generator had: it embedded the catalogue as JSON inside a ``<script>`` tag and
rebuilt the grid with ``innerHTML``, so an item name containing ``</script>``
closed the element and executed. Item text originates from a vision model
reading a photographed label, which CLAUDE.md classes as untrusted content.

Runs entirely offline against a temporary SQLite file.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

TMP = tempfile.mkdtemp(prefix="estate-catalogue-int-")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["ESTATE_INVENTORY_DIR"] = f"{TMP}/inventory"
os.environ["ESTATE_VISION_PROVIDER"] = "mock"

from estate._compat import config as _config  # noqa: E402
from estate._compat import database as _database  # noqa: E402

_config.get_settings.cache_clear()
_database._engine = None
_database._SessionLocal = None

from estate import pipeline, site  # noqa: E402
from estate.repository import (  # noqa: E402
    CompRepository,
    ItemRepository,
    PhotoRepository,
)
from estate._compat import get_session, init_db  # noqa: E402

SITE_URL = "https://catalogue.example"


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


@pytest.fixture()
def session():
    s = get_session()
    yield s
    s.close()


def _jpeg(seed: int) -> bytes:
    """A real, tiny JPEG.

    Photographs have to be genuinely decodable here: the build reads their
    pixel dimensions so the browser can reserve the space, and it generates
    the EXIF-stripped web derivatives that are the only images ever published.
    Fake bytes would silently exercise neither path.
    """
    import io

    from PIL import Image

    image = Image.new("RGB", (120 + seed, 90 + seed), (40 + seed, 90, 140))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=70)
    return buffer.getvalue()


def make_item(session, *, name="Walnut Side Table", price=225.0,
              category="Furniture", photos=1, comps=True, **overrides):
    """An item that clears every publication gate unless told otherwise."""
    item = pipeline.start_item(session, owner="telegram:1")
    for n in range(photos):
        pipeline.attach_photo(session, item.item_id, _jpeg(n + len(name)))
    fields = {
        "item_name": name,
        "category": category,
        "condition": "Good",
        "defects": "A shallow scratch on the top, about two inches long.",
        "description": "A solid piece with one shallow scratch on the top.",
        "dimensions": "24 x 24 x 22 in",
        "ownership_approval": True,
        "review_status": "Reviewed",
        "approval_status": "Approved",
        "website_status": "Queued",
        "status": "Ready to List",
        "current_price": price,
        "floor_price": round(price * 0.5, 2),
        "pickup_required": True,
        "shipping_feasible": False,
    }
    fields.update(overrides)
    ItemRepository(session).update(item.item_id, actor="test", **fields)
    if comps:
        CompRepository(session).add(
            item.item_id,
            platform="eBay",
            title=f"{name} sold",
            url=f"https://example.com/sold/{item.item_id}",
            price=price,
            is_sold=True,
            is_placeholder=False,
            needs_confirmation=False,
        )
    return item.item_id


def build(session, tmp_path, **kwargs):
    kwargs.setdefault("catalog_url", SITE_URL)
    kwargs.setdefault("email", "sales@example.com")
    kwargs.setdefault("region", "the Portland area")
    return site.build_site(session, out_dir=tmp_path, **kwargs)


def empty_session(tmp_path):
    """A session on its own empty database.

    The estate integration modules all point the process-wide engine at their
    own temporary file at import time, which means whichever module imported
    last wins and they share one database at run time. That is fine for tests
    that assert about the items they created, but a genuinely-empty-catalogue
    test cannot get there by deleting everyone else's rows. It gets its own
    engine instead.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from estate import models as _estate_models  # noqa: F401
    from estate.migrations import ensure_estate_schema
    from estate._compat import Base

    engine = create_engine(
        f"sqlite:///{tmp_path}/empty.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    ensure_estate_schema(engine)
    return sessionmaker(bind=engine)()


def read(tmp_path, *parts):
    path = tmp_path
    for part in parts:
        path = path / part
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Publication gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"ownership_approval": False}, "Ownership"),
        ({"review_status": "In Review"}, "Identification review"),
        ({"approval_status": "Pending"}, "Approval status"),
        ({"website_status": "Hidden"}, "Website status"),
        ({"status": "Draft"}, "Lifecycle status"),
        ({"current_price": None}, "current price"),
        ({"floor_price": None}, "floor price"),
        ({"defects": ""}, "Defects field is empty"),
        ({"condition": "Unknown"}, "Condition is still Unknown"),
        ({"item_name": "[MOCK] Chair"}, "[MOCK]"),
        ({"description": "[MOCK] sample text"}, "[MOCK]"),
    ],
)
def test_each_publication_gate_blocks_on_its_own(session, override, expected):
    # The name deliberately avoids echoing the field being tested: these items
    # persist in the shared test database, and an item literally named
    # "floor_price" would trip the whole-site leak scan in
    # test_estate_site_leaks.py for no good reason.
    item_id = make_item(session, name=f"Blocked item {abs(hash(expected)) % 9973}",
                        **override)
    item = ItemRepository(session).get(item_id)
    blockers = site.publication_blockers(session, item)
    assert any(expected in b for b in blockers), (override, blockers)
    assert item_id not in {i.item_id for i in site.collect(session)}


def test_an_item_with_no_photographs_is_not_published(session):
    item_id = make_item(session, name="No photos here", photos=0)
    item = ItemRepository(session).get(item_id)
    assert any("photograph" in b.lower() for b in site.publication_blockers(session, item))


def test_an_item_with_no_confirmed_comparables_is_not_published(session):
    item_id = make_item(session, name="No evidence", comps=False)
    item = ItemRepository(session).get(item_id)
    assert any("comparable" in b.lower() for b in site.publication_blockers(session, item))


def test_placeholder_comparables_block_publication(session):
    item_id = make_item(session, name="Placeholder priced", comps=False)
    CompRepository(session).add(
        item_id, platform="PLACEHOLDER", title="made up", url="https://example.invalid/x",
        price=100, is_placeholder=True,
    )
    item = ItemRepository(session).get(item_id)
    blockers = site.publication_blockers(session, item)
    assert any("Placeholder" in b for b in blockers)


def test_a_comparable_awaiting_confirmation_does_not_unlock_publication(session):
    """An automated provider's proposal is evidence a human has not yet
    confirmed, and must not by itself put an item in front of a buyer."""
    item_id = make_item(session, name="Unconfirmed evidence", comps=False)
    CompRepository(session).add(
        item_id, platform="eBay", title="proposed", url="https://example.com/proposed",
        price=100, is_placeholder=False, needs_confirmation=True,
    )
    item = ItemRepository(session).get(item_id)
    assert any("confirmed comparable" in b for b in site.publication_blockers(session, item))


def test_a_fully_gated_item_publishes(session, tmp_path):
    item_id = make_item(session, name="Everything in order")
    item = ItemRepository(session).get(item_id)
    assert site.publication_blockers(session, item) == []
    report = build(session, tmp_path)
    assert report["items"] >= 1
    assert (tmp_path / "items" / f"{item_id}.html").exists()


def test_an_approved_but_blocked_item_is_reported_by_name(session, tmp_path):
    """An item a human already approved that is silently missing from the
    catalogue is the most confusing state this system can be in."""
    blocked = make_item(session, name="Approved but blank defects", defects="")
    report = build(session, tmp_path)
    held = dict(report["held_back"])
    assert blocked in held
    assert any("Defects field is empty" in b for b in held[blocked])
    assert any(blocked in w for w in report["warnings"])


# ---------------------------------------------------------------------------
# Untrusted item text
# ---------------------------------------------------------------------------


def test_hostile_item_text_cannot_break_out_of_the_page(session, tmp_path):
    """Regression: the old generator serialised items into a <script> blob and
    rebuilt the grid with innerHTML, so this name executed."""
    item_id = make_item(
        session,
        name='Chair </script><script>alert(1)</script>',
        defects='<img src=x onerror=alert(2)>',
        description='"><svg onload=alert(3)>',
    )
    build(session, tmp_path)

    for path in tmp_path.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "</script><script>alert(1)" not in text, path
        assert "<img src=x onerror" not in text, path
        assert "<svg onload=alert" not in text, path

    page = read(tmp_path, "items", f"{item_id}.html")
    assert "&lt;/script&gt;" in page or "&lt;script&gt;" in page


def test_the_grid_is_real_html_not_built_by_javascript(session, tmp_path):
    """With JavaScript off, blocked, or still loading, the catalogue must
    still be a catalogue."""
    item_id = make_item(session, name="Server rendered")
    build(session, tmp_path)
    index = read(tmp_path, "index.html")
    assert f'items/{item_id}.html' in index
    assert "Server rendered" in index
    assert "innerHTML" not in index


def test_no_third_party_resource_is_ever_requested(session, tmp_path):
    make_item(session, name="Privacy check")
    build(session, tmp_path)
    for path in tmp_path.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "<script src=" not in text, path
        assert "googletagmanager" not in text
        assert "fonts.googleapis" not in text
        assert "google-analytics" not in text
        assert "document.cookie" not in text


# ---------------------------------------------------------------------------
# Browsing: categories, search, sort
# ---------------------------------------------------------------------------


def test_a_page_is_written_for_every_category_that_has_something_in_it(
    session, tmp_path
):
    make_item(session, name="Sofa", category="Furniture")
    make_item(session, name="Drill", category="Tools & Equipment")
    make_item(session, name="Amp", category="Audio / Music Gear")
    build(session, tmp_path)

    assert (tmp_path / "category" / "furniture.html").exists()
    assert (tmp_path / "category" / "tools-equipment.html").exists()
    assert (tmp_path / "category" / "audio-music-gear.html").exists()


def test_empty_categories_never_appear(session, tmp_path):
    """A category chip leading to nothing makes a small sale look abandoned."""
    make_item(session, name="Only furniture here", category="Furniture")
    build(session, tmp_path)

    index = read(tmp_path, "index.html")
    assert "category/furniture.html" in index
    assert "category/jewelry-watches.html" not in index
    assert "Jewelry &amp; Watches" not in index
    assert not (tmp_path / "category" / "jewelry-watches.html").exists()


def test_a_category_page_that_empties_out_is_deleted_on_rebuild(session, tmp_path):
    item_id = make_item(session, name="Lone racket", category="Sporting Goods")
    build(session, tmp_path)
    page = tmp_path / "category" / "sporting-goods.html"
    assert page.exists()

    ItemRepository(session).update(item_id, actor="test", status="Removed")
    build(session, tmp_path)
    assert not page.exists()


def test_a_category_page_only_lists_its_own_items(session, tmp_path):
    keep = make_item(session, name="Bookcase", category="Furniture")
    other = make_item(session, name="Hammer", category="Tools & Equipment")
    build(session, tmp_path)

    page = read(tmp_path, "category", "furniture.html")
    assert f"items/{keep}.html" in page
    assert f"items/{other}.html" not in page


def test_cards_carry_the_data_the_search_and_sort_controls_need(session, tmp_path):
    item_id = make_item(session, name="Brass Lamp", price=88.0, category="Home Decor")
    build(session, tmp_path)
    index = read(tmp_path, "index.html")

    assert f'data-id="{item_id}"' in index
    assert 'data-price="88.0"' in index
    assert 'data-category="Home Decor"' in index
    assert 'data-condition="Good"' in index
    assert "data-search=" in index
    assert "data-added=" in index
    assert "brass lamp" in index  # the lowercased search haystack


def test_every_sort_option_is_offered(session, tmp_path):
    make_item(session, name="Sortable")
    build(session, tmp_path)
    index = read(tmp_path, "index.html")
    for value in ("newest", "price-asc", "price-desc", "name"):
        assert f'value="{value}"' in index


def test_the_default_order_is_newest_first_before_any_javascript_runs(
    session, tmp_path
):
    import time

    first = make_item(session, name="Older piece")
    time.sleep(1.05)
    second = make_item(session, name="Newer piece")
    build(session, tmp_path)

    index = read(tmp_path, "index.html")
    assert index.index(f"items/{second}.html") < index.index(f"items/{first}.html")


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def test_every_card_says_how_the_buyer_gets_it_home(session, tmp_path):
    make_item(session, name="Pickup only piece", pickup_required=True,
              shipping_feasible=False)
    make_item(session, name="Shippable piece", pickup_required=False,
              shipping_feasible=True)
    build(session, tmp_path)

    index = read(tmp_path, "index.html")
    assert "Local pickup" in index
    assert "Can be shipped" in index


def test_a_sold_item_stays_visible_and_is_clearly_marked(session, tmp_path):
    """Sold pieces are social proof, and they stop people asking about things
    that have gone."""
    sold_id = make_item(session, name="Already gone", status="Sold",
                        website_status="Sold (shown)")
    build(session, tmp_path)

    index = read(tmp_path, "index.html")
    assert f"items/{sold_id}.html" in index
    assert 'class="tag sold"' in index
    assert "Sold" in index

    page = read(tmp_path, "items", f"{sold_id}.html")
    assert "has sold" in page
    # No inquiry form on something that is gone.
    assert 'id="inquiry-form"' not in page
    # And it cannot be added to a bundle.
    assert f'data-add="{sold_id}"' not in page


def test_sold_items_sort_to_the_end_of_the_default_order(session, tmp_path):
    sold_id = make_item(session, name="Gone piece", status="Sold",
                        website_status="Sold (shown)")
    live_id = make_item(session, name="Live piece")
    build(session, tmp_path)

    index = read(tmp_path, "index.html")
    assert index.index(f"items/{live_id}.html") < index.index(f"items/{sold_id}.html")


def test_the_pickup_price_is_shown_when_it_differs(session, tmp_path):
    make_item(session, name="Collect and save", price=300.0,
              approved_pickup_price=265.0)
    build(session, tmp_path)
    index = read(tmp_path, "index.html")
    assert "$265" in index
    assert "if you collect" in index


# ---------------------------------------------------------------------------
# Item pages
# ---------------------------------------------------------------------------


def test_the_item_page_respects_the_generated_image_order(session, tmp_path):
    """build_website_copy decides which photograph leads. The site used to
    publish whatever order the database returned, so the hero shot was
    frequently not first."""
    item_id = make_item(session, name="Several angles", photos=4)
    originals = PhotoRepository(session).for_item(item_id, role="original")
    # Make the LAST photo the hero, so database order and image order differ.
    originals[-1].is_hero = True
    session.commit()

    build(session, tmp_path)
    page = read(tmp_path, "items", f"{item_id}.html")

    published = PhotoRepository(session).for_item(item_id, role="web")
    positions = {p.filename: page.index(p.filename) for p in published
                 if p.filename in page}
    assert len(positions) == 4, positions

    hero = next(p for p in published if p.is_hero)
    assert positions[hero.filename] == min(positions.values())
    assert "_hero." in hero.filename

    # The hero also leads the card on the index, and loads eagerly.
    index = read(tmp_path, "index.html")
    assert hero.filename in index
    assert 'loading="eager"' in index


def test_the_item_page_states_the_defects_plainly(session, tmp_path):
    item_id = make_item(
        session, name="Honest chair",
        defects="The left arm has a two-inch crack and the seat is stained.",
    )
    build(session, tmp_path)
    page = read(tmp_path, "items", f"{item_id}.html")

    assert "What is wrong with it" in page
    assert "two-inch crack" in page
    assert "rather you saw a flaw here" in page


def test_an_item_with_no_recorded_defects_still_says_something_honest(
    session, tmp_path
):
    item_id = make_item(session, name="Clean piece", defects="None observed on inspection")
    build(session, tmp_path)
    page = read(tmp_path, "items", f"{item_id}.html")
    assert "None observed on inspection" in page


def test_the_item_page_carries_the_specification_a_buyer_asks_for(
    session, tmp_path
):
    item_id = make_item(
        session, name="Measured piece", dimensions="30 x 18 x 26 in",
        weight_lbs=44, included_accessories="Two spare shelf pins",
    )
    build(session, tmp_path)
    page = read(tmp_path, "items", f"{item_id}.html")

    assert "30 x 18 x 26 in" in page
    assert "44 lb" in page
    assert "Two spare shelf pins" in page
    assert "the Portland area" in page
    assert item_id in page


def test_photographs_carry_real_alt_text_and_reserved_space(session, tmp_path):
    item_id = make_item(session, name="Described photos", photos=3)
    build(session, tmp_path)
    page = read(tmp_path, "items", f"{item_id}.html")

    assert 'alt="Described photos"' in page
    assert "photograph 2 of 3" in page
    assert "width=" in page and "height=" in page
    assert 'loading="lazy"' in page
    assert 'decoding="async"' in page


def test_the_page_is_navigable_by_keyboard_and_screen_reader(session, tmp_path):
    make_item(session, name="Accessible piece")
    build(session, tmp_path)
    index = read(tmp_path, "index.html")

    assert 'class="skip"' in index
    assert "<main" in index
    assert 'aria-label="Main"' in index
    assert 'aria-live="polite"' in index
    assert "<label for=" in index
    assert ":focus-visible" in index
    assert "prefers-reduced-motion" in index


# ---------------------------------------------------------------------------
# Share previews
# ---------------------------------------------------------------------------


def test_an_item_link_unfurls_with_a_photograph_a_title_and_a_price(
    session, tmp_path
):
    """Every marketplace post carries one of these links."""
    item_id = make_item(session, name="Shareable Table", price=340.0)
    build(session, tmp_path)
    page = read(tmp_path, "items", f"{item_id}.html")

    assert 'property="og:type" content="product"' in page
    assert 'property="og:title" content="Shareable Table' in page
    assert f'property="og:url" content="{SITE_URL}/items/{item_id}.html"' in page
    assert f'property="og:image" content="{SITE_URL}/photos/{item_id}/' in page
    assert 'property="product:price:amount" content="340"' in page
    assert 'property="og:availability" content="instock"' in page
    assert 'name="twitter:card" content="summary_large_image"' in page
    assert 'name="twitter:image"' in page
    assert f'rel="canonical" href="{SITE_URL}/items/{item_id}.html"' in page


def test_a_sold_item_unfurls_as_out_of_stock(session, tmp_path):
    item_id = make_item(session, name="Gone Table", status="Sold",
                        website_status="Sold (shown)")
    build(session, tmp_path)
    page = read(tmp_path, "items", f"{item_id}.html")
    assert 'property="og:availability" content="oos"' in page


def test_a_build_without_a_site_url_warns_instead_of_emitting_dead_previews(
    session, tmp_path
):
    make_item(session, name="No canonical")
    report = site.build_site(session, out_dir=tmp_path, catalog_url="",
                             email="sales@example.com")
    assert any("ESTATE_CATALOG_URL" in w for w in report["warnings"])
    assert not (tmp_path / "sitemap.xml").exists()


def test_a_sitemap_is_written_when_the_url_is_known(session, tmp_path):
    item_id = make_item(session, name="Indexed piece", category="Furniture")
    build(session, tmp_path)

    sitemap = read(tmp_path, "sitemap.xml")
    assert f"{SITE_URL}/items/{item_id}.html" in sitemap
    assert f"{SITE_URL}/category/furniture.html" in sitemap
    assert f"{SITE_URL}/bundle.html" in sitemap
    assert f"Sitemap: {SITE_URL}/sitemap.xml" in read(tmp_path, "robots.txt")


# ---------------------------------------------------------------------------
# Empty catalogue
# ---------------------------------------------------------------------------


def test_an_empty_catalogue_is_honest_rather_than_broken(tmp_path):
    blank = empty_session(tmp_path)
    try:
        report = build(blank, tmp_path / "site")
    finally:
        blank.close()
    tmp_path = tmp_path / "site"

    assert report["items"] == 0
    assert any("empty" in w.lower() for w in report["warnings"])

    index = read(tmp_path, "index.html")
    assert "Nothing is listed just yet" in index
    assert "Nothing matches those filters" in index  # present but hidden
    assert (tmp_path / "bundle.html").exists()
    assert (tmp_path / "about.html").exists()
    assert json.loads(read(tmp_path, "catalog_manifest.json")) == {}
    assert json.loads(read(tmp_path, "catalog.json")) == []


# ---------------------------------------------------------------------------
# The public feed
# ---------------------------------------------------------------------------


def test_the_public_feed_does_not_publish_the_internal_status_vocabulary(
    session, tmp_path
):
    item_id = make_item(session, name="Committed piece", status="Pickup Scheduled")
    build(session, tmp_path)

    feed = json.loads(read(tmp_path, "catalog.json"))
    entry = next(e for e in feed if e["id"] == item_id)
    assert entry["available"] is True
    assert "status" not in entry
    assert "Pickup Scheduled" not in read(tmp_path, "catalog.json")


def test_the_manifest_carries_price_and_band_but_never_the_floor(
    session, tmp_path
):
    item_id = make_item(session, name="Banded piece", price=200.0, floor_price=100.0)
    build(session, tmp_path)

    manifest = json.loads(read(tmp_path, "catalog_manifest.json"))
    entry = manifest[item_id]
    assert entry["price"] == 200.0
    assert entry["band"] == 3  # 50% of headroom absorbs every tier
    assert "floor" not in json.dumps(manifest).lower()
    assert "100.0" not in json.dumps(manifest)
