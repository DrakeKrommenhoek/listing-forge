"""Nothing private may reach a build. Scans the whole site, not one file.

``test_emitted_bundle_contains_no_credentials_or_private_endpoints`` in
test_estate_inquiry_endpoint.py checks the serverless function's own source.
This file is the wider net: it builds a complete site from an item deliberately
loaded with every private value the system holds, then reads back **every byte
of every file in the output** — HTML, JSON, XML, text, and the emitted Python —
and fails on any of them.

The list is drawn from what would actually hurt if a stranger read it:

* ``floor_price`` — the least we will take. Publishing it ends every
  negotiation before it starts, and it is the reason bundle discounts are
  published as a coarse band rather than as a maximum discount (see
  estate/bundling.py). Both the label and the *value* are checked.
* internal and private notes, and the priority score — how we talk about our
  own inventory, not something a buyer is owed.
* the physical address — the whole point of publishing a region instead.
* the VPS host and port, and any ``/estate/`` private route — the private
  system's shape.
* ``ESTATE_REVIEW_TOKEN`` / ``review_token`` — approval credentials.
* anything matching ``bot<digits>:`` — the shape of a Telegram bot token.
* comparable source URLs and research worksheets — our evidence and method.
* every value of length >= 8 in the real ``.env``, whatever it happens to be.

That last one is the important one: it catches a secret nobody thought to add
to a list. It reads the repository's actual ``.env`` when there is one, so the
check tracks reality rather than a fixture.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

TMP = tempfile.mkdtemp(prefix="estate-leak-int-")
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
)
from estate._compat import get_session, init_db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

# Values planted on the item and its evidence. Every one of them is something
# the private side legitimately stores and the public side must never show.
SECRETS = {
    "floor_label": "floor_price",
    "floor_value": "137.5",
    "address": "48 Marchmont Row, Apartment 3B",
    "internal_note": "SELLERNOTE-do-not-publish-will-take-less-if-pushed",
    "private_note": "PRIVATENOTE-dad-thinks-this-is-worth-more",
    "comp_url": "https://www.ebay.com/itm/COMPSOURCE-999888777",
    "review_token": "REVIEWTOKEN-abc123def456",
    "bot_token": "bot7654321:AAHfakefakefakefakefakefakefake",
    "vps_host": "203.0.113.77",
    "vps_port": "8000",
    "worksheet": "DK-202608-001_comps_worksheet.csv",
}

FORBIDDEN_LITERALS = [
    "floor_price",
    "Floor Price",
    "internal_notes",
    "private_notes",
    "priority_score",
    "priority_reasons",
    "ESTATE_REVIEW_TOKEN",
    "review_token",
    "comp_sources",
    "research_worksheet",
    "_comps_worksheet",
    "/estate/review",
    "/estate/inquiry",
    "/estate/approve",
    "127.0.0.1:8000",
    "localhost:8000",
    "TELEGRAM_BOT_TOKEN=",
    "ESTATE_ALLOWED_SUBMITTER_IDS",
    "ESTATE_REVIEWER_IDS",
    "vision_raw",
    "approval_blockers",
    "research_blockers",
]

#: The shape of a Telegram bot token, whatever the digits happen to be.
BOT_TOKEN_RE = re.compile(r"bot\d+:")


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One site, built from an item stuffed with every private value."""
    out = tmp_path_factory.mktemp("leaky-site")
    session = get_session()
    try:
        item = pipeline.start_item(session, owner="telegram:1")
        pipeline.attach_photo(session, item.item_id, b"photo-bytes")
        ItemRepository(session).update(
            item.item_id,
            actor="test",
            item_name="Walnut Sideboard",
            category="Furniture",
            condition="Good",
            description="A walnut sideboard with one shallow scratch.",
            defects="A shallow scratch across the top, about four inches.",
            dimensions="60 x 18 x 32 in",
            ownership_approval=True,
            review_status="Reviewed",
            approval_status="Approved",
            website_status="Queued",
            status="Ready to List",
            current_price=275.0,
            floor_price=float(SECRETS["floor_value"]),
            # Everything below is private and must not survive the build.
            location_in_house=SECRETS["address"],
            notes=SECRETS["internal_note"] + " " + SECRETS["private_note"],
            priority_score=87,
            priority_reasons="Value 32, readiness 18, urgency 14",
            approval_blockers="none",
            research_blockers="none",
            comp_sources=[SECRETS["comp_url"]],
            listing_urls=[f"http://{SECRETS['vps_host']}:{SECRETS['vps_port']}/estate/review"],
        )
        CompRepository(session).add(
            item.item_id,
            platform="eBay",
            title="Walnut sideboard, sold",
            url=SECRETS["comp_url"],
            price=280.0,
            is_sold=True,
            is_placeholder=False,
            needs_confirmation=False,
        )
        site.build_site(
            session,
            out_dir=out,
            catalog_url="https://catalogue.example",
            email="sales@example.com",
            region="the Portland area",
        )
        # Assertions are scoped to this item rather than to a global count:
        # the estate integration modules share one process-wide engine, so
        # other modules' items may be present too. That only makes the scan
        # wider, which is the right direction for a leak test.
        assert (out / "items" / f"{item.item_id}.html").exists()
        yield out, item.item_id
    finally:
        session.close()


def _all_files(root: Path):
    """Every file in the build output, whatever its extension."""
    return [p for p in root.rglob("*") if p.is_file()]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""  # a JPEG is not a place a string secret hides


def test_the_build_actually_produced_something_to_scan(built):
    out, _item_id = built
    files = _all_files(out)
    assert len(files) > 8, [p.name for p in files]
    assert any(p.name == "index.html" for p in files)
    assert any(p.name == "inquiry.py" for p in files)
    assert any(p.suffix == ".json" for p in files)


def test_no_planted_secret_appears_anywhere_in_the_output(built):
    out, _item_id = built
    for path in _all_files(out):
        text = _read(path)
        if not text:
            continue
        for label, secret in SECRETS.items():
            if label == "vps_port":
                continue  # bare "8000" is checked as a host:port pair below
            assert secret not in text, (path.relative_to(out), label)


def test_no_private_field_name_or_route_appears_anywhere(built):
    out, _item_id = built
    for path in _all_files(out):
        text = _read(path)
        if not text:
            continue
        for literal in FORBIDDEN_LITERALS:
            assert literal not in text, (path.relative_to(out), literal)


def test_the_build_never_names_the_main_bot_token_variable(built):
    """The public endpoint gets its OWN bot, never the assistant's.

    A leak in a public serverless function must not hand anyone control of the
    bot that reads Drake's own messages, so the deployed code may reference
    ``ESTATE_INQUIRY_TELEGRAM_BOT_TOKEN`` and nothing else. Every occurrence of
    the string is checked for that prefix rather than banning it outright,
    which would be a false positive on the variable the endpoint legitimately
    needs.
    """
    out, _item_id = built
    needle = "TELEGRAM_BOT_TOKEN"
    for path in _all_files(out):
        text = _read(path)
        start = 0
        while True:
            at = text.find(needle, start)
            if at == -1:
                break
            assert text[:at].endswith("ESTATE_INQUIRY_"), (
                path.relative_to(out),
                text[max(0, at - 40):at + len(needle)],
            )
            start = at + len(needle)


def test_nothing_shaped_like_a_bot_token_appears_anywhere(built):
    out, _item_id = built
    for path in _all_files(out):
        text = _read(path)
        if not text:
            continue
        match = BOT_TOKEN_RE.search(text)
        assert match is None, (path.relative_to(out), match.group(0))


def test_the_floor_price_is_not_recoverable_from_the_published_band(built):
    """The band is deliberately coarse.

    ``max_discount = 1 - floor/price`` would publish the floor exactly. A band
    is an index into a three-row tier table — about two bits — so it bounds the
    floor loosely and reveals nothing usable.
    """
    import json

    out, item_id = built
    manifest = json.loads((out / "catalog_manifest.json").read_text(encoding="utf-8"))
    entry = manifest[item_id]

    assert set(entry) == {"status", "sold", "price", "band"}
    assert isinstance(entry["band"], int)
    assert entry["band"] in (0, 1, 2, 3)
    # The floor is 137.5 against a 275 price: exactly 50% of headroom, so the
    # band saturates. Anyone reading it learns only "at least 15% is possible".
    assert entry["band"] == 3

    # The floor value appears nowhere, and no published number equals it.
    blob = json.dumps(manifest)
    assert SECRETS["floor_value"] not in blob
    floor = float(SECRETS["floor_value"])
    for published in manifest.values():
        assert published.get("price") != floor


#: Settings whose values are *supposed* to appear on the public site. The
#: brand is the site's own name, the region is printed instead of the address,
#: the selling address is how a buyer reaches us, and the catalogue URL is the
#: site's own canonical URL. Excluding them is not a weakening of the scan --
#: including them would make it cry wolf on every build, which is how a
#: security test ends up ignored. Everything not named here must never appear.
PUBLISHED_BY_DESIGN = {
    "ESTATE_BRAND_NAME",
    "ESTATE_PICKUP_REGION",
    "ESTATE_SELLING_EMAIL",
    "ESTATE_CATALOG_URL",
    "ESTATE_SITE_ORIGIN",
}


def test_no_env_value_of_any_length_reaches_the_build(built):
    """The catch-all: scan for every value in the repository's real .env.

    This is the check that finds a secret nobody remembered to list. Values
    shorter than 8 characters are skipped because they collide with ordinary
    words and produce noise rather than signal; anything that actually needs
    protecting is longer than that.

    Settings in PUBLISHED_BY_DESIGN are excluded by *key*, never by value, so
    a secret that happens to equal the brand name is still caught.
    """
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        pytest.skip("no .env in this checkout")

    values = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in PUBLISHED_BY_DESIGN:
            continue
        value = value.strip().strip("'\"")
        if len(value) >= 8 and not value.startswith("./") and "/" != value:
            values.append(value)

    assert values, "the .env should contain something worth protecting"

    for path in _all_files(out_of(built)):
        text = _read(path)
        if not text:
            continue
        for value in values:
            # Never print the value itself in the failure message.
            assert value not in text, (
                f"a value from .env appeared in {path.name} "
                f"(length {len(value)}, starts with {value[:2]}...)"
            )


def out_of(built):
    out, _item_id = built
    return out


def test_the_public_pages_still_say_what_a_buyer_needs(built):
    """The scan must not be passing because the site is empty.

    A leak test that would also pass on a blank page proves nothing, so this
    asserts the things that SHOULD be public are.
    """
    out, item_id = built
    page = (out / "items" / f"{item_id}.html").read_text(encoding="utf-8")

    assert "Walnut Sideboard" in page
    assert "$275" in page
    assert "shallow scratch" in page  # the defect disclosure
    assert "60 x 18 x 32 in" in page
    assert "the Portland area" in page
    assert item_id in page


def test_the_region_is_published_but_the_address_is_not(built):
    out, _item_id = built
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "the Portland area" in index
    assert "Marchmont" not in index
    assert "Apartment" not in index
    assert "never publish our address" in index


def test_a_preview_build_is_also_scanned_and_also_clean(built, tmp_path):
    """A preview build relaxes the publication gates. It must not relax this."""
    session = get_session()
    try:
        site.build_site(
            session,
            out_dir=tmp_path,
            include_mock=True,
            catalog_url="https://catalogue.example",
            email="sales@example.com",
        )
    finally:
        session.close()

    for path in _all_files(tmp_path):
        text = _read(path)
        if not text:
            continue
        for label, secret in SECRETS.items():
            if label == "vps_port":
                continue
            assert secret not in text, (path.relative_to(tmp_path), label)
        for literal in FORBIDDEN_LITERALS:
            assert literal not in text, (path.relative_to(tmp_path), literal)

    # And it must be marked, and must refuse crawlers.
    assert "Disallow: /" in (tmp_path / "robots.txt").read_text(encoding="utf-8")
    assert "Preview build" in (tmp_path / "index.html").read_text(encoding="utf-8")
