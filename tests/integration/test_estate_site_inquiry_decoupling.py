"""Integration tests for the decoupled public-site inquiry flow.

Covers the milestone: the generated catalogue site must never POST inquiries
directly to the private VPS API. Instead it must POST to a relative
``api/inquiry`` path served by a standalone serverless function that
``build_site`` emits alongside the static pages, validated against a
build-time catalogue manifest. Also covers the related fix: a page for an
item that is no longer publishable must be removed on rebuild, not left
stale.

Runs entirely offline against a temporary SQLite file, same pattern as
tests/integration/test_estate_pipeline.py.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

TMP = tempfile.mkdtemp(prefix="estate-site-int-")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["ESTATE_INVENTORY_DIR"] = f"{TMP}/inventory"
os.environ["ESTATE_VISION_PROVIDER"] = "mock"

from estate._compat import config as _config  # noqa: E402
from estate._compat import database as _database  # noqa: E402

_config.get_settings.cache_clear()
_database._engine = None
_database._SessionLocal = None

from estate import pipeline, site  # noqa: E402
from estate.repository import CompRepository, ItemRepository  # noqa: E402
from estate._compat import get_session, init_db  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


@pytest.fixture()
def session():
    s = get_session()
    yield s
    s.close()


def _make_publishable_item(session, name="Walnut Side Table", price=225,
                           category="Furniture", **overrides):
    """Create an item that genuinely clears every publication gate.

    This helper used to set only the approval and website statuses, which was
    enough for the old three-condition gate. It is not enough now, and it
    should never have been: site.publication_blockers also requires confirmed
    ownership, a completed identification review, an approved floor price,
    at least one confirmed (non-placeholder, non-pending) comparable, an
    explicit defects disclosure, and a photograph.

    Tests that want an item to FAIL one specific gate pass an override rather
    than relying on the helper leaving something out by accident.
    """
    item = pipeline.start_item(session, owner="telegram:1")
    pipeline.attach_photo(session, item.item_id, b"fake-photo-bytes-" + name.encode())
    fields = {
        "item_name": name,
        "category": category,
        "condition": "Good",
        "defects": "A small scuff on the left rear leg. Nothing structural.",
        "description": "A solid walnut side table with one shallow scuff.",
        "ownership_approval": True,
        "review_status": "Reviewed",
        "approval_status": "Approved",
        "website_status": "Queued",
        "status": "Ready to List",
        "current_price": price,
        "floor_price": round(price * 0.5, 2),
    }
    fields.update(overrides)
    ItemRepository(session).update(item.item_id, actor="test", **fields)
    CompRepository(session).add(
        item.item_id,
        platform="eBay",
        title=f"{name}, sold",
        url=f"https://example.com/sold/{item.item_id}",
        price=price,
        is_sold=True,
        is_placeholder=False,
        needs_confirmation=False,
    )
    return item.item_id


def _make_unpublishable_item(session, name="Rejected Lamp"):
    item = pipeline.start_item(session, owner="telegram:1")
    ItemRepository(session).update(
        item.item_id, actor="test",
        item_name=name, category="Home Decor", condition="Fair",
        approval_status="Pending", website_status="Hidden",
        status="Draft",
    )
    return item.item_id


# ---------------------------------------------------------------------------
# Manifest + serverless function emission
# ---------------------------------------------------------------------------

def test_build_emits_catalog_manifest(tmp_path, session):
    item_id = _make_publishable_item(session)
    site.build_site(session, out_dir=tmp_path)

    manifest_path = tmp_path / "catalog_manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert item_id in manifest
    assert manifest[item_id]["status"] == "Ready to List"
    assert manifest[item_id]["sold"] is False


def test_build_emits_standalone_inquiry_function(tmp_path, session):
    _make_publishable_item(session)
    site.build_site(session, out_dir=tmp_path)

    fn = tmp_path / "api" / "inquiry.py"
    assert fn.exists()
    src = fn.read_text(encoding="utf-8")
    assert "class handler(BaseHTTPRequestHandler)" in src
    assert "def do_POST" in src
    # Must not reach back into the private VPS API.
    assert "127.0.0.1:8000" not in src
    assert "/estate/inquiry" not in src

    lib_dir = tmp_path / "api" / "_lib"
    assert (lib_dir / "__init__.py").exists()
    assert "def validate_inquiry" in (lib_dir / "inquiry_validation.py").read_text(encoding="utf-8")
    assert "class LocalLogNotifier" in (lib_dir / "inquiry_notifier.py").read_text(encoding="utf-8")


def test_emitted_lib_copies_match_source_modules(tmp_path, session):
    """The build must copy the real, tested modules verbatim -- not a
    hand-maintained duplicate that could drift out of sync."""
    import inspect

    from estate import inquiry_notifier, inquiry_validation

    _make_publishable_item(session)
    site.build_site(session, out_dir=tmp_path)

    lib_dir = tmp_path / "api" / "_lib"
    assert (lib_dir / "inquiry_validation.py").read_text(encoding="utf-8") == inspect.getsource(
        inquiry_validation
    )
    assert (lib_dir / "inquiry_notifier.py").read_text(encoding="utf-8") == inspect.getsource(
        inquiry_notifier
    )


# ---------------------------------------------------------------------------
# Forms point at the decoupled endpoint, never the private API
# ---------------------------------------------------------------------------

def test_item_page_posts_to_relative_endpoint_not_private_api(tmp_path, session):
    item_id = _make_publishable_item(session)
    site.build_site(session, out_dir=tmp_path)

    html = (tmp_path / "items" / f"{item_id}.html").read_text(encoding="utf-8")
    assert "../api/inquiry" in html
    assert "estate/inquiry" not in html
    assert "127.0.0.1:8000" not in html


def test_about_page_posts_to_relative_endpoint_not_private_api(tmp_path, session):
    _make_publishable_item(session)
    site.build_site(session, out_dir=tmp_path)

    html = (tmp_path / "about.html").read_text(encoding="utf-8")
    assert "api/inquiry" in html
    assert "estate/inquiry" not in html
    assert "127.0.0.1:8000" not in html


def test_api_base_argument_is_accepted_but_has_no_effect(tmp_path, session):
    """Backward compatibility: existing callers (scripts/estate_site.py,
    estate/demo.py) still pass api_base. It must not resurrect a direct
    private-API reference anywhere in the output."""
    item_id = _make_publishable_item(session)
    site.build_site(session, out_dir=tmp_path, api_base="http://127.0.0.1:8000")

    html = (tmp_path / "items" / f"{item_id}.html").read_text(encoding="utf-8")
    assert "127.0.0.1:8000" not in html


# ---------------------------------------------------------------------------
# Stale page cleanup
# ---------------------------------------------------------------------------

def test_page_removed_once_item_is_no_longer_publishable(tmp_path, session):
    item_id = _make_publishable_item(session)
    site.build_site(session, out_dir=tmp_path)

    page = tmp_path / "items" / f"{item_id}.html"
    assert page.exists()

    ItemRepository(session).update(
        item_id, actor="test", status="Removed", approval_status="Approved",
    )
    site.build_site(session, out_dir=tmp_path)

    assert not page.exists()
    manifest = json.loads((tmp_path / "catalog_manifest.json").read_text(encoding="utf-8"))
    assert item_id not in manifest


def test_unrelated_publishable_item_pages_survive_rebuild(tmp_path, session):
    """Cleanup must only remove pages for items that dropped out of the
    publishable set -- not every page on every build."""
    keep_id = _make_publishable_item(session, name="Keeper")
    remove_id = _make_publishable_item(session, name="Removed Later")
    site.build_site(session, out_dir=tmp_path)

    ItemRepository(session).update(remove_id, actor="test", status="Removed")
    site.build_site(session, out_dir=tmp_path)

    assert (tmp_path / "items" / f"{keep_id}.html").exists()
    assert not (tmp_path / "items" / f"{remove_id}.html").exists()


def test_unpublishable_item_never_appears_in_manifest_or_pages(tmp_path, session):
    unpublishable_id = _make_unpublishable_item(session)
    published_id = _make_publishable_item(session)
    site.build_site(session, out_dir=tmp_path)

    manifest = json.loads((tmp_path / "catalog_manifest.json").read_text(encoding="utf-8"))
    assert published_id in manifest
    assert unpublishable_id not in manifest
    assert (tmp_path / "items" / f"{published_id}.html").exists()
    assert not (tmp_path / "items" / f"{unpublishable_id}.html").exists()
