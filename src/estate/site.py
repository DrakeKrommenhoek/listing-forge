"""Static catalogue site generator — the public shop window.

Approved inventory only. ``build_site`` reads the database, filters to items a
human has approved for publication, generates metadata-free images, and writes
a self-contained static site.

Why static: it cannot leak the database, it cannot be SQL-injected, it costs
nothing to host, and it loads instantly on a phone in a driveway. The only
dynamic part is the inquiry endpoint, which posts to a same-origin, relative
``api/inquiry`` path — a standalone serverless function emitted alongside
this site by ``estate/serverless.py`` — never to the private VPS API. See
that module's docstring for the full architecture.

Rendering policy: server-side, always
-------------------------------------
Every card and every item page is real HTML in the file. JavaScript filters,
sorts and manages the bundle basket by showing and hiding elements that are
already in the document; it never constructs markup out of item data.

This is a correctness rule before it is a performance one. The previous
generator embedded the whole catalogue as a JSON blob inside a ``<script>``
tag and rebuilt the grid with ``innerHTML``. Item text ultimately originates
from a vision model reading whatever is printed on a photographed object,
which CLAUDE.md classes as untrusted content — and an item named
``Chair </script><script>…`` closed the tag and executed. That was a real,
reproducible stored-XSS hole, verified before this rewrite and covered by a
regression test after it. Rendering server-side through ``e()`` removes the
whole class of bug rather than patching the one instance, and it means the
catalogue still works with JavaScript disabled, blocked, or still loading.

Privacy rules baked in:
- The house address never appears. Location is shown as a region only.
- Only ``web`` derivatives (EXIF-stripped) are published.
- Buyer contact goes to the dedicated selling address, never a personal one.
- Floor prices, internal notes, priority scores, comparable sources and
  research worksheets are never written into a build. Bundle discounting
  publishes a coarse *band* per item rather than a maximum discount, because
  a maximum discount is algebraically the floor price — see
  ``estate/bundling.py``.
- No cookies, no tracking, no analytics, no third-party scripts, no external
  fonts. Nothing to disclose and nothing that leaks a buyer's browsing.
"""

from __future__ import annotations

import html
import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path

from estate import bundling, images, listing, paths, serverless
from estate.repository import CompRepository, ItemRepository, PhotoRepository
from estate.schema import CATEGORIES, PUBLISHABLE_STATUSES, ItemStatus

CROSS_LIST_NOTICE = (
    "Everything here is listed on other marketplaces at the same time. "
    "Availability can change without notice — please confirm before travelling."
)

BUNDLE_NOTICE = (
    "Bundle prices shown on this site are indicative. We confirm the final "
    "figure when we reply — a person checks every bundle."
)

#: Website statuses that mean "this item may appear publicly".
PUBLIC_WEBSITE_STATUSES = ("Queued", "Published", "Sold (shown)")

#: Item text that is rendered publicly. Scanned for mock markers so sample
#: data cannot reach a build through any field, not just the name.
_PUBLIC_TEXT_FIELDS = (
    "item_name",
    "brand",
    "model",
    "description",
    "defects",
    "dimensions",
    "included_accessories",
    "listing_title",
    "listing_description",
)

#: Photo slots mapped to what a screen-reader user should be told the
#: photograph actually shows. "Photo 3 of 7" is the last resort, not the
#: default.
_ALT_BY_SLOT = {
    "hero": "",
    "front": "seen from the front",
    "back": "seen from the back",
    "side-l": "seen from the left side",
    "side-r": "seen from the right side",
    "top": "seen from above",
    "bottom": "the underside",
    "label": "the maker's label",
    "serial": "the serial or model number",
    "accessories": "the accessories included",
    "dimensions": "being measured",
    "defect": "a close-up of the damage described in the listing",
    "detail": "a close-up detail",
}


def e(v) -> str:
    """HTML-escape a value for both text and attribute contexts."""
    return html.escape("" if v is None else str(v), quote=True)


def _json_for_html(obj) -> str:
    """Serialise JSON that is safe to embed inside a ``<script>`` element.

    ``json.dumps`` escapes for JSON, not for HTML: it will happily emit
    ``</script>`` inside a string and end the element early. Escaping the
    characters that can start an HTML construct keeps the value valid JSON
    while making it inert to an HTML parser.

    Only numeric and ID-shaped data is embedded this way anywhere in this
    module, but the helper exists so a future addition cannot reintroduce the
    hole by forgetting.
    """
    return (
        json.dumps(obj, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    """A stable, filesystem- and URL-safe slug for a category name."""
    slug = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return slug or "other"


def _money(value) -> str:
    """Whole dollars with thousands separators. Never a bare float."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    return f"${amount:,.0f}"


# ---------------------------------------------------------------------------
# Publication gating
#
# An item is public only when every one of these holds. This is the last gate
# before the outside world sees anything, and it deliberately re-checks
# conditions that approval.prepare_review also checks rather than delegating
# to them: approval can be bypassed (estate/demo.py does exactly that, on
# purpose), and website_status can be set by a direct repository update.
# Checking again here means neither route can put an unfinished item in front
# of a buyer.
#
# Nothing in this section may be relaxed to make a build produce more items.
# ---------------------------------------------------------------------------


def _has_mock_marker(item) -> bool:
    """True if ``[MOCK]`` appears anywhere in the item's public text.

    The previous check tested only whether ``item_name`` *started with* the
    marker, so a mock description under a clean name published silently.
    """
    for attr in _PUBLIC_TEXT_FIELDS:
        if "[MOCK]" in str(getattr(item, attr, "") or "").upper():
            return True
    return False


def publication_blockers(session, item, photos=None, comps=None) -> list:
    """Every reason this item may not be published, in plain language.

    Empty list means publishable. Used by ``collect`` to filter a build, and
    available to the review tooling so a reviewer can see exactly what stands
    between an item and the catalogue.
    """
    blockers = []

    # -- availability is public ---------------------------------------------
    if item.approval_status != "Approved":
        blockers.append("Approval status is not Approved.")
    if item.website_status not in PUBLIC_WEBSITE_STATUSES:
        blockers.append(
            f"Website status is {item.website_status or 'unset'}, so this item is "
            "not marked public."
        )
    if item.status not in PUBLISHABLE_STATUSES:
        blockers.append(f"Lifecycle status {item.status or 'unset'} is not publishable.")

    # -- no mock data --------------------------------------------------------
    if _has_mock_marker(item):
        blockers.append("Contains [MOCK] sample data and must never be published.")

    # -- ownership confirmed -------------------------------------------------
    if not item.ownership_approval:
        blockers.append("Ownership has not been confirmed.")

    # -- identification reviewed --------------------------------------------
    if item.review_status != "Reviewed":
        blockers.append(
            f"Identification review is {item.review_status or 'not recorded'}, "
            "not Reviewed."
        )
    if not str(item.item_name or "").strip():
        blockers.append("No item name.")

    # -- pricing approved by a human ----------------------------------------
    if not item.current_price:
        blockers.append("No approved current price.")
    if not item.floor_price:
        blockers.append("No approved floor price.")

    # -- comparable evidence reviewed and confirmed -------------------------
    if comps is None:
        comps = CompRepository(session).for_item(item.item_id)
    confirmed = [
        c
        for c in comps
        if getattr(c, "url", "")
        and not getattr(c, "is_placeholder", False)
        and not getattr(c, "needs_confirmation", False)
    ]
    if any(getattr(c, "is_placeholder", False) for c in comps):
        blockers.append("Placeholder comparables are still attached to this item.")
    if not confirmed:
        blockers.append("No confirmed comparable evidence behind this price.")

    # -- defects disclosed ---------------------------------------------------
    # An empty defects field means "nobody wrote anything here", which is not
    # the same as "there is nothing wrong with it" (see the field note in
    # schema.py). A buyer is owed an explicit statement either way, so a
    # reviewer who inspected it and found nothing types that in.
    if not str(item.defects or "").strip():
        blockers.append(
            "Defects field is empty. Record what was found — 'None observed on "
            "inspection' is a disclosure; blank is not."
        )
    if (item.condition or "Unknown") == "Unknown":
        blockers.append("Condition is still Unknown.")

    # -- photos present ------------------------------------------------------
    if photos is None:
        photos = PhotoRepository(session).for_item(item.item_id, role="web") or (
            PhotoRepository(session).for_item(item.item_id, role="original")
        )
    if not photos:
        blockers.append("No photographs.")

    # -- website copy complete ----------------------------------------------
    copy = listing.build_website_copy(item, photos=list(photos or []))
    if not str(copy.product_title or "").strip():
        blockers.append("Website copy has no title.")
    if not str(copy.description or "").strip():
        blockers.append("Website copy has no description.")
    if not str(copy.condition_statement or "").strip():
        blockers.append("Website copy has no condition statement.")

    return blockers


def _is_publishable(item, session=None, photos=None, comps=None) -> bool:
    """True when nothing blocks publication. See ``publication_blockers``."""
    return not publication_blockers(session, item, photos=photos, comps=comps)


def collect(session, include_mock: bool = False) -> list:
    """Every item that may appear in a build.

    ``include_mock`` switches the build into **preview mode**: the evidence,
    ownership and copy gates are relaxed so that sample data
    (``estate/demo.py``) can exercise the whole catalogue. A preview build is
    stamped on every page and gets a ``Disallow: /`` robots.txt — see
    ``build_site``. It is never the path a real build takes.
    """
    items = []
    for item in ItemRepository(session).all():
        if not publication_blockers(session, item):
            items.append(item)
        elif include_mock and item.approval_status == "Approved":
            items.append(item)
    return items


def held_back(session) -> list:
    """Approved items that a gate is still keeping off the site.

    An item a human has already signed off on but which does not appear on the
    catalogue is the most confusing state this system can be in, so the build
    reports it by name and reason rather than leaving someone to work out why
    the count is one short.
    """
    out = []
    for item in ItemRepository(session).all():
        if item.approval_status != "Approved":
            continue
        blockers = publication_blockers(session, item)
        if blockers:
            out.append((item.item_id, blockers))
    return out


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


def _photo_alt(name: str, filename: str, index: int, total: int) -> str:
    """Real alt text describing what this photograph actually shows."""
    slot = ""
    parts = Path(filename).stem.split("_")
    if len(parts) >= 3:
        slot = parts[-1]
    if slot == "hero":
        return name
    phrase = _ALT_BY_SLOT.get(slot)
    if phrase:
        return f"{name}, {phrase}"
    if total > 1:
        return f"{name}, photograph {index} of {total}"
    return name


def _image_size(path: Path) -> tuple:
    """Actual pixel dimensions, so the browser can reserve the space.

    Without width and height the page reflows as each photograph arrives,
    which on a phone means the sentence someone was reading jumps off the
    screen. Returns ``(None, None)`` when Pillow is unavailable; the CSS
    aspect-ratio box covers that case less precisely.
    """
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001 - a missing size is cosmetic, never fatal
        return (None, None)


def _ordered_photos(session, item):
    """Photos in the order the generated ``image_order`` asks for.

    ``listing.build_website_copy`` decides which photograph leads and in what
    order the rest follow. The site previously ignored that and published
    whatever order the database returned, so the hero shot frequently was not
    first. The generated copy is the authority.
    """
    photos = PhotoRepository(session).for_item(item.item_id, role="web")
    if not photos:
        photos = PhotoRepository(session).for_item(item.item_id, role="original")

    copy = listing.build_website_copy(item, photos=list(photos))
    order = list(copy.image_order or [])
    if not order:
        return photos, copy

    by_name = {p.filename: p for p in photos}
    ordered = [by_name[n] for n in order if n in by_name]
    seen = {p.filename for p in ordered}
    ordered += [p for p in photos if p.filename not in seen]
    return ordered, copy


def _item_payload(session, item, photo_dir_name: str, tiers) -> dict:
    photos, copy = _ordered_photos(session, item)
    sold = item.status == ItemStatus.SOLD.value
    price = item.current_price

    submitted = item.date_submitted
    if isinstance(submitted, datetime):
        submitted_iso = submitted.date().isoformat()
        submitted_epoch = int(submitted.timestamp())
    else:
        submitted_iso = str(submitted or "")
        submitted_epoch = 0

    return {
        "id": item.item_id,
        "name": copy.product_title or item.item_name or "Untitled",
        "subtitle": copy.subtitle or "",
        "brand": item.brand or "",
        "category": item.category or "Other",
        "condition": item.condition or "Unknown",
        "defects": item.defects or "",
        "dimensions": item.dimensions or "",
        "weight": item.weight_lbs,
        "accessories": item.included_accessories or "",
        "description": copy.description or item.description or "",
        "key_details": list(copy.key_details or []),
        "shipping_statement": copy.shipping_statement or "",
        "price": price,
        "pickup_price": item.approved_pickup_price,
        "shipping": bool(item.shipping_feasible),
        "pickup_only": bool(item.pickup_required),
        "sold": sold,
        "status": item.status,
        "submitted": submitted_iso,
        "submitted_epoch": submitted_epoch,
        # The ONLY bundle fact ever published about an item. Never the floor,
        # and never a maximum discount, which is the floor by another name.
        "band": 0 if sold else bundling.discount_band(price, item.floor_price, tiers),
        "photos": [
            {"file": p.filename, "src": f"{photo_dir_name}/{item.item_id}/{p.filename}"}
            for p in photos
        ],
    }


def _public_catalog_entry(p: dict) -> dict:
    """The item as published in ``catalog.json``.

    A deliberate subset. The internal lifecycle vocabulary ("Pickup
    Scheduled", "Offer Received") describes how the operation runs rather
    than anything a buyer needs, so the public feed carries a plain
    availability flag instead.
    """
    return {
        "id": p["id"],
        "name": p["name"],
        "brand": p["brand"],
        "category": p["category"],
        "condition": p["condition"],
        "description": p["description"],
        "dimensions": p["dimensions"],
        "price": p["price"],
        "pickup_price": p["pickup_price"],
        "available": not p["sold"],
        "shipping": p["shipping"],
        "pickup_only": p["pickup_only"],
        "photos": [ph["src"] for ph in p["photos"]],
        "url": f"items/{p['id']}.html",
    }


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

CSS = """
:root{--ink:#141310;--soft:#57524a;--faint:#767068;--line:#e3ded4;--bg:#faf9f6;
--paper:#fff;--accent:#26241f;--gold:#8a7238;--sold:#6d6862;--flag:#8a4a2c;
--ok:#2f6b46;--focus:#1a5fb4}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}
body{background:var(--bg);color:var(--ink);
font:17px/1.65 "Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
-webkit-font-smoothing:antialiased;padding-bottom:88px}
.sans{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
a{color:inherit}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,
textarea:focus-visible{outline:3px solid var(--focus);outline-offset:2px;border-radius:3px}
.skip{position:absolute;left:-9999px;top:0;background:var(--paper);padding:12px 18px;z-index:60}
.skip:focus{left:8px;top:8px}
img{max-width:100%;height:auto}
header.site{padding:30px 20px 22px;text-align:center;border-bottom:1px solid var(--line);
background:var(--paper)}
header.site .mark{font-size:12px;letter-spacing:.2em;text-transform:uppercase;
color:var(--gold);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif}
header.site h1{font-size:clamp(27px,5vw,42px);font-weight:400;margin:10px 0 8px}
header.site h1 a{text-decoration:none}
header.site p{color:var(--soft);font-size:16px;max-width:640px;margin:0 auto}
nav.site{display:flex;justify-content:center;flex-wrap:wrap;gap:4px;padding:8px 12px;
border-bottom:1px solid var(--line);background:var(--paper);position:sticky;top:0;z-index:20;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif;font-size:15px}
nav.site a{text-decoration:none;color:var(--soft);padding:10px 14px;border-radius:4px;
min-height:44px;display:flex;align-items:center}
nav.site a:hover{color:var(--ink);background:#f2efe8}
nav.site a[aria-current="page"]{color:var(--ink);box-shadow:inset 0 -2px 0 var(--gold)}
main{max-width:1240px;margin:0 auto;padding:26px 18px 60px}
h2{font-weight:400;font-size:25px;line-height:1.25}
.notice{background:#fdf8ea;border:1px solid #e3d5ac;padding:13px 16px;border-radius:5px;
font-size:15px;color:#57491e;margin-bottom:22px}
.cats{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 22px;padding:0;list-style:none}
.cats a{display:inline-block;padding:10px 15px;border:1px solid var(--line);border-radius:999px;
background:var(--paper);text-decoration:none;font-size:15px;color:var(--soft);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif}
.cats a:hover{border-color:var(--gold);color:var(--ink)}
.cats a[aria-current="page"]{background:var(--accent);color:#fff;border-color:var(--accent)}
.filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:14px;
margin-bottom:22px;padding:16px;background:var(--paper);border:1px solid var(--line);
border-radius:6px}
.filters label{display:block;font-size:14px;color:var(--soft);margin-bottom:5px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif}
.filters input,.filters select{width:100%;padding:11px 10px;border:1px solid #cfc8bb;
border-radius:4px;font:inherit;font-size:16px;background:#fff;min-height:46px;color:var(--ink)}
.count{font-size:15px;color:var(--soft);margin-bottom:14px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:22px;
list-style:none;padding:0}
@media(max-width:520px){.grid{grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:14px}
main{padding:20px 14px 50px}}
.card{background:var(--paper);border:1px solid var(--line);border-radius:6px;overflow:hidden;
display:flex;flex-direction:column;position:relative;transition:box-shadow .18s ease}
.card:hover{box-shadow:0 8px 22px rgba(0,0,0,.07)}
.card:focus-within{box-shadow:0 0 0 3px var(--focus)}
.card .shot{aspect-ratio:4/3;background:#efece5;overflow:hidden;position:relative}
.card .shot img{width:100%;height:100%;object-fit:cover;display:block}
.card .noshot{display:flex;align-items:center;justify-content:center;height:100%;
color:var(--faint);font-size:14px;text-align:center;padding:10px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif}
.card .body{padding:13px 15px 15px;flex:1;display:flex;flex-direction:column;gap:5px}
.card .brand{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--gold);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif}
.card h3{font-size:17px;font-weight:400;line-height:1.3}
.card h3 a{text-decoration:none}
.card h3 a::after{content:"";position:absolute;inset:0;z-index:0}
.card .meta{font-size:14px;color:var(--soft);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif}
.card .price{margin-top:auto;font-size:19px;padding-top:6px}
.card .price .was{color:var(--faint);font-size:14px;display:block}
.badges{display:flex;flex-wrap:wrap;gap:5px;margin-top:4px}
.badge{font-size:12px;padding:4px 8px;border-radius:3px;background:#f2efe8;color:var(--soft);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif;white-space:nowrap}
.badge.ships{background:#eaf2ec;color:var(--ok)}
.badge.pickup{background:#f4f0e6;color:#63511e}
.tag{position:absolute;top:10px;left:10px;background:rgba(255,255,255,.95);
padding:5px 10px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;border-radius:3px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif}
.tag.sold{background:var(--sold);color:#fff}
.card.sold .shot img{filter:grayscale(1);opacity:.55}
.card.sold h3,.card.sold .price{color:var(--sold)}
.add{position:relative;z-index:1;margin:0 15px 15px;min-height:46px;padding:10px;
border:1px solid #cfc8bb;background:#fff;border-radius:4px;cursor:pointer;font-size:15px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif;color:var(--ink)}
.add:hover{border-color:var(--accent)}
.add[aria-pressed="true"]{background:var(--accent);color:#fff;border-color:var(--accent)}
.empty{text-align:center;padding:52px 20px;color:var(--soft);background:var(--paper);
border:1px solid var(--line);border-radius:6px}
.detail{display:grid;grid-template-columns:1.2fr 1fr;gap:40px}
@media(max-width:860px){.detail{grid-template-columns:1fr;gap:24px}}
.gallery figure{margin-bottom:14px}
.gallery img{width:100%;border-radius:5px;border:1px solid var(--line);display:block;
background:#efece5}
.spec{width:100%;border-collapse:collapse;font-size:15px;margin:16px 0}
.spec th{text-align:left;padding:10px 12px 10px 0;color:var(--soft);font-weight:400;width:42%;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif;font-size:14px;
vertical-align:top}
.spec td{padding:10px 0;border-bottom:1px solid var(--line);vertical-align:top}
.disclose{background:#fdf4f0;border-left:4px solid var(--flag);padding:14px 16px;
border-radius:0 4px 4px 0;font-size:15px;margin:18px 0}
.disclose strong{display:block;margin-bottom:5px}
.btn{display:inline-block;padding:14px 24px;background:var(--accent);color:#fff;
text-decoration:none;border-radius:4px;font-size:15px;border:1px solid var(--accent);
cursor:pointer;min-height:48px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif}
.btn:hover{background:#3a372f}
.btn.ghost{background:transparent;color:var(--accent)}
.btn.ghost:hover{background:#f2efe8}
.btn[disabled]{opacity:.5;cursor:not-allowed}
form.inquiry{background:var(--paper);border:1px solid var(--line);border-radius:6px;
padding:20px;margin-top:20px}
form.inquiry label{display:block;font-size:14px;color:var(--soft);margin-bottom:5px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif}
form.inquiry input,form.inquiry textarea{width:100%;padding:11px;border:1px solid #cfc8bb;
border-radius:4px;font:inherit;font-size:16px;margin-bottom:14px;background:#fff;
min-height:46px;color:var(--ink)}
form.inquiry textarea{min-height:112px}
.hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
.formnote{font-size:15px;margin-top:10px}
.formnote.err{color:var(--flag)}
.formnote.ok{color:var(--ok)}
.bundlebar{position:fixed;left:0;right:0;bottom:0;background:var(--accent);color:#fff;
padding:12px 16px;display:none;gap:12px;align-items:center;justify-content:center;
flex-wrap:wrap;z-index:40;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif;font-size:15px}
.bundlebar.on{display:flex}
.bundlebar .btn{background:#fff;color:var(--accent);border-color:#fff;padding:10px 18px;
min-height:44px}
.bundlebar .clear{background:transparent;color:#fff;border:1px solid rgba(255,255,255,.5);
padding:10px 14px;border-radius:4px;cursor:pointer;font:inherit;min-height:44px}
.basket{list-style:none;padding:0}
.basket li{display:grid;grid-template-columns:88px 1fr auto;gap:14px;align-items:center;
padding:14px 0;border-bottom:1px solid var(--line)}
.basket img{width:88px;height:66px;object-fit:cover;border-radius:4px;background:#efece5}
.basket .noimg{display:block;width:88px;height:66px;border-radius:4px;background:#efece5}
.totals{margin-top:20px;padding:18px;background:var(--paper);border:1px solid var(--line);
border-radius:6px;font-size:16px}
.totals div{display:flex;justify-content:space-between;padding:5px 0;gap:16px}
.totals .grand{font-size:22px;border-top:1px solid var(--line);margin-top:8px;padding-top:12px}
.totals .save{color:var(--ok)}
.preview{background:var(--flag);color:#fff;padding:11px 16px;text-align:center;font-size:15px;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,sans-serif}
footer.site{border-top:1px solid var(--line);padding:32px 20px;text-align:center;
color:var(--soft);font-size:14px;background:var(--paper)}
footer.site p{margin-top:9px;max-width:660px;margin-left:auto;margin-right:auto}
.idtag{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;color:var(--faint)}
@media(prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
"""


# ---------------------------------------------------------------------------
# Shared chrome
# ---------------------------------------------------------------------------


def _head(*, title, description, brand, canonical="", image="", price=None,
          sold=False, kind="website") -> str:
    """The document head, including share-preview tags.

    Every marketplace post carries a link back here, so a pasted URL has to
    unfurl into a photograph, a title and a price rather than a bare address.
    OpenGraph covers Facebook, Messenger, Nextdoor, WhatsApp and iMessage;
    the Twitter tags cover the handful of readers that prefer them.

    Absolute URLs are required by every scraper, which is why a build without
    ``ESTATE_CATALOG_URL`` records a warning rather than silently emitting
    previews that will never render.
    """
    tags = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta name="theme-color" content="#faf9f6">',
        f"<title>{e(title)}</title>",
        f'<meta name="description" content="{e(description)}">',
        f'<meta property="og:site_name" content="{e(brand)}">',
        f'<meta property="og:type" content="{e(kind)}">',
        f'<meta property="og:title" content="{e(title)}">',
        f'<meta property="og:description" content="{e(description)}">',
        '<meta property="og:locale" content="en_US">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{e(title)}">',
        f'<meta name="twitter:description" content="{e(description)}">',
    ]
    if canonical:
        tags.append(f'<link rel="canonical" href="{e(canonical)}">')
        tags.append(f'<meta property="og:url" content="{e(canonical)}">')
    if image:
        tags.append(f'<meta property="og:image" content="{e(image)}">')
        tags.append(f'<meta property="og:image:alt" content="{e(title)}">')
        tags.append(f'<meta name="twitter:image" content="{e(image)}">')
    if price is not None:
        tags.append(f'<meta property="product:price:amount" content="{e(int(price))}">')
        tags.append('<meta property="product:price:currency" content="USD">')
        tags.append(
            '<meta property="og:availability" content="{}">'.format(
                "oos" if sold else "instock"
            )
        )
    tags.append('<meta name="robots" content="index,follow,max-image-preview:large">')
    tags.append(f"<style>{CSS}</style>")
    return "".join(tags)


def _nav(prefix: str = "", current: str = "") -> str:
    def link(href, label, key):
        mark = ' aria-current="page"' if key == current else ""
        return f'<a href="{prefix}{href}"{mark}>{label}</a>'

    return (
        '<nav class="site" aria-label="Main">'
        + link("index.html", "Everything", "index")
        + link("bundle.html", "Your bundle", "bundle")
        + link("about.html", "About &amp; contact", "about")
        + "</nav>"
    )


def _site_header(brand, tagline, prefix="", show_tagline=True) -> str:
    return (
        '<header class="site"><div class="mark">Private collection</div>'
        f'<h1><a href="{e(prefix)}index.html">{e(brand)}</a></h1>'
        + (f"<p>{e(tagline)}</p>" if show_tagline and tagline else "")
        + "</header>"
    )


def _footer(brand: str, email: str, region: str) -> str:
    contact = e(email) if email else "use the contact form"
    return (
        '<footer class="site"><p>{}{}</p>'
        "<p>Enquiries: {}</p>"
        "<p>{}</p>"
        "<p>We never publish our address. Collection details are shared once a time "
        "is agreed.</p>"
        "<p>This site sets no cookies, loads nothing from anyone else, and does not "
        "track you.</p></footer>"
    ).format(
        e(brand),
        (" &middot; " + e(region)) if region else "",
        contact,
        e(CROSS_LIST_NOTICE),
    )


def _preview_banner(preview: bool) -> str:
    if not preview:
        return ""
    return (
        '<div class="preview" role="status">Preview build — contains sample data. '
        "Not for publication.</div>"
    )


# ---------------------------------------------------------------------------
# Client-side behaviour
#
# Progressive enhancement only. Everything below operates on elements the
# server already rendered: it shows, hides, reorders and counts them. It never
# constructs markup from item data, which is what keeps the untrusted-content
# rule in CLAUDE.md true on the public site.
# ---------------------------------------------------------------------------

_BASKET_JS = """
(function(){
  var PRICES = __PRICES__, TIERS = __TIERS__, ROUND = __ROUND__, MAXITEMS = __MAXITEMS__;
  var KEY = "fo.basket", mem = [];

  // sessionStorage throws outright in some privacy modes rather than simply
  // returning null, so every touch is guarded and the in-memory copy is the
  // real source of truth. Selection survives navigation when storage works
  // and degrades to per-page when it does not. It never breaks the page.
  function store(){ try { return window.sessionStorage; } catch(err) { return null; } }
  function load(){
    var s = store();
    if(s){
      try {
        var v = JSON.parse(s.getItem(KEY) || "[]");
        if(Array.isArray(v)) return v.filter(function(i){ return PRICES[i]; });
      } catch(err) {}
    }
    return mem.slice();
  }
  function save(ids){
    mem = ids.slice();
    var s = store();
    if(s){ try { s.setItem(KEY, JSON.stringify(ids)); } catch(err) {} }
  }

  // A shared ?b= link is merged in once on load, so a buyer can send someone
  // "here are the four things I want" and have it survive the click.
  try {
    var fromUrl = new URLSearchParams(window.location.search).get("b");
    if(fromUrl){
      var merged = load();
      fromUrl.split(",").forEach(function(id){
        id = id.trim();
        if(id && PRICES[id] && merged.indexOf(id) === -1) merged.push(id);
      });
      save(merged.slice(0, MAXITEMS));
    }
  } catch(err) {}

  function tierPct(n){
    var pct = 0;
    for(var i = 0; i < TIERS.length; i++){ if(n >= TIERS[i][0]) pct = Math.max(pct, TIERS[i][1]); }
    return pct;
  }
  function quote(ids){
    var subtotal = 0, band = null, priced = 0;
    ids.forEach(function(id){
      var row = PRICES[id]; if(!row) return;
      var p = row[0], b = row[1];
      if(p > 0){ subtotal += p; priced++; } else { b = 0; }
      band = (band === null) ? b : Math.min(band, b);
    });
    var byCount = tierPct(ids.length);
    // The band cap is the binding constraint: how far the most constrained
    // item in this basket can go without crossing the least we may take for
    // it. That figure is not published and is not knowable from here.
    var byBand = (band === null || band <= 0 || !TIERS.length)
      ? 0 : TIERS[Math.min(band, TIERS.length) - 1][1];
    var pct = priced ? Math.min(byCount, byBand) : 0;
    var total = subtotal ? Math.ceil(subtotal * (1 - pct) / ROUND) * ROUND : 0;
    if(total > subtotal) total = subtotal;
    return {subtotal: subtotal, pct: pct, total: total,
            saved: Math.max(0, subtotal - total), cappedBy: pct < byCount,
            priced: priced, count: ids.length};
  }
  function money(v){ return "$" + Math.round(v).toLocaleString("en-US"); }

  function paint(){
    var ids = load(), q = quote(ids);
    var buttons = document.querySelectorAll("[data-add]");
    for(var i = 0; i < buttons.length; i++){
      var on = ids.indexOf(buttons[i].getAttribute("data-add")) !== -1;
      buttons[i].setAttribute("aria-pressed", on ? "true" : "false");
      if(!buttons[i].hasAttribute("data-keep-label")){
        buttons[i].textContent = on ? "In your bundle \\u2713" : "Add to bundle";
      }
    }
    var bar = document.getElementById("bundlebar");
    if(bar){
      if(ids.length) bar.className = "bundlebar on"; else bar.className = "bundlebar";
      var t = document.getElementById("bundlecount");
      if(t) t.textContent = ids.length + (ids.length === 1 ? " item" : " items")
        + (q.total ? " \\u00b7 about " + money(q.total) : "");
    }
    var rows = document.querySelectorAll("[data-basket-row]");
    for(var r = 0; r < rows.length; r++){
      rows[r].hidden = ids.indexOf(rows[r].getAttribute("data-basket-row")) === -1;
    }
    var set = function(id, text){
      var el = document.getElementById(id); if(el) el.textContent = text;
    };
    var toggle = function(id, hidden){
      var el = document.getElementById(id); if(el) el.hidden = hidden;
    };
    toggle("basketempty", ids.length > 0);
    toggle("basketfilled", ids.length === 0);
    set("sumcount", q.count + (q.count === 1 ? " item" : " items"));
    set("sumsubtotal", money(q.subtotal));
    set("sumdiscount", q.pct
      ? "\\u2212" + money(q.saved) + " (" + Math.round(q.pct * 100) + "%)" : "None yet");
    set("sumtotal", q.total ? money(q.total) : "\\u2014");
    toggle("sumcapped", !q.cappedBy || !q.count);
    var field = document.getElementById("basketfield");
    if(field) field.value = ids.join(",");
    var share = document.getElementById("sharelink");
    if(share) share.value = ids.length
      ? window.location.origin + window.location.pathname + "?b=" + ids.join(",") : "";
  }

  document.addEventListener("click", function(ev){
    var node = ev.target;
    while(node && node !== document){
      if(node.hasAttribute && node.hasAttribute("data-add")){
        ev.preventDefault();
        var id = node.getAttribute("data-add"), ids = load(), at = ids.indexOf(id);
        if(at === -1){ if(ids.length >= MAXITEMS) return; ids.push(id); }
        else { ids.splice(at, 1); }
        save(ids); paint(); return;
      }
      if(node.hasAttribute && node.hasAttribute("data-clear-basket")){
        ev.preventDefault(); save([]); paint(); return;
      }
      node = node.parentNode;
    }
  });
  window.__foPaint = paint;
  if(document.readyState !== "loading") paint();
  else document.addEventListener("DOMContentLoaded", paint);
})();
"""

_BROWSE_JS = """
(function(){
  var $ = function(id){ return document.getElementById(id); };
  var grid = $("grid"); if(!grid) return;
  var cards = [].slice.call(grid.querySelectorAll("[data-item]"));

  function apply(){
    var q = ($("q") ? $("q").value : "").trim().toLowerCase();
    var cat = $("cat") ? $("cat").value : "";
    var cond = $("cond") ? $("cond").value : "";
    var ful = $("fulfil") ? $("fulfil").value : "";
    var max = $("max") ? parseFloat($("max").value) : NaN;
    var avail = $("avail") ? $("avail").value : "all";
    var shown = 0;

    cards.forEach(function(card){
      var sold = card.getAttribute("data-sold") === "1";
      var price = parseFloat(card.getAttribute("data-price"));
      var ok = true;
      if(avail === "available" && sold) ok = false;
      if(ok && cat && card.getAttribute("data-category") !== cat) ok = false;
      if(ok && cond && card.getAttribute("data-condition") !== cond) ok = false;
      if(ok && ful === "ship" && card.getAttribute("data-ships") !== "1") ok = false;
      if(ok && ful === "pickup" && card.getAttribute("data-pickup") !== "1") ok = false;
      if(ok && !isNaN(max) && !isNaN(price) && price > max) ok = false;
      if(ok && q && card.getAttribute("data-search").indexOf(q) === -1) ok = false;
      card.hidden = !ok;
      if(ok) shown++;
    });

    var sort = $("sort") ? $("sort").value : "newest";
    var visible = cards.filter(function(c){ return !c.hidden; });
    visible.sort(function(a, b){
      // Sold pieces sink to the bottom whatever the sort. They stay on the
      // page as proof the sale is real and to stop people asking about
      // things that have gone, but they are not what a buyer came for.
      var sa = a.getAttribute("data-sold") === "1" ? 1 : 0;
      var sb = b.getAttribute("data-sold") === "1" ? 1 : 0;
      if(sa !== sb) return sa - sb;
      var pa = parseFloat(a.getAttribute("data-price"));
      var pb = parseFloat(b.getAttribute("data-price"));
      if(sort === "price-asc") return (isNaN(pa) ? Infinity : pa) - (isNaN(pb) ? Infinity : pb);
      if(sort === "price-desc") return (isNaN(pb) ? -Infinity : pb) - (isNaN(pa) ? -Infinity : pa);
      if(sort === "name") return a.getAttribute("data-name")
        .localeCompare(b.getAttribute("data-name"));
      return parseInt(b.getAttribute("data-added"), 10) - parseInt(a.getAttribute("data-added"), 10);
    });
    visible.forEach(function(card){ grid.appendChild(card); });

    var count = $("count");
    if(count) count.textContent = shown === 1 ? "1 piece" : shown + " pieces";
    var empty = $("empty");
    if(empty) empty.hidden = shown > 0;
    if(window.__foPaint) window.__foPaint();
  }

  ["q", "cat", "cond", "fulfil", "max", "avail", "sort"].forEach(function(id){
    var el = $(id); if(!el) return;
    el.addEventListener("input", apply);
    el.addEventListener("change", apply);
  });
  var reset = $("reset");
  if(reset) reset.addEventListener("click", function(){
    ["q", "max", "cat", "cond", "fulfil"].forEach(function(id){ if($(id)) $(id).value = ""; });
    if($("avail")) $("avail").value = "all";
    if($("sort")) $("sort").value = "newest";
    apply();
  });
  apply();
})();
"""

_FORM_JS = """
(function(){
  var form = document.getElementById("inquiry-form");
  if(!form) return;
  var note = document.getElementById("formnote");
  var button = form.querySelector("button[type=submit]");
  form.addEventListener("submit", function(ev){
    ev.preventDefault();
    note.className = "formnote";
    note.textContent = "Sending\\u2026";
    button.disabled = true;
    var data = {};
    new FormData(form).forEach(function(value, key){ data[key] = value; });
    if(data.items) data.items = String(data.items).split(",").filter(Boolean);
    fetch(__ENDPOINT__, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(data)
    }).then(function(res){
      return res.json().catch(function(){ return {}; }).then(function(body){
        return {ok: res.ok, body: body};
      });
    }).then(function(result){
      button.disabled = false;
      if(result.ok){
        note.className = "formnote ok";
        note.textContent = result.body.message || "Thank you \\u2014 we will be in touch.";
        form.reset();
        if(window.__foPaint) window.__foPaint();
      } else {
        // Never a false thank-you. If it did not get through, say so, and
        // give a route that does not depend on this endpoint working.
        note.className = "formnote err";
        note.textContent = (result.body.message || "That did not send.") + " __FALLBACK__";
      }
    }).catch(function(){
      button.disabled = false;
      note.className = "formnote err";
      note.textContent = "That did not send \\u2014 you may be offline. __FALLBACK__";
    });
  });
})();
"""


def _basket_js(payloads, config) -> str:
    """The basket script, with the price/band and tier tables baked in.

    The table is item IDs mapped to two numbers. No item *text* is ever
    emitted into a script element anywhere in this module.
    """
    prices = {
        p["id"]: [float(p["price"]) if p["price"] else 0, int(p["band"])]
        for p in payloads
        if not p["sold"]
    }
    tiers = [[t["min_items"], t["discount_pct"]] for t in config["tiers"]]
    return (
        _BASKET_JS.replace("__PRICES__", _json_for_html(prices))
        .replace("__TIERS__", _json_for_html(tiers))
        .replace("__ROUND__", str(config["round_to"] or 1))
        .replace("__MAXITEMS__", str(config["max_items"]))
    )


def _form_js(endpoint: str, email: str) -> str:
    fallback = (
        f"Please email {email} and we will pick it up there."
        if email
        else "Please try again in a few minutes."
    )
    return _FORM_JS.replace("__ENDPOINT__", json.dumps(endpoint)).replace(
        "__FALLBACK__", fallback.replace('"', "'").replace("\\", "")
    )


def _bundle_bar(prefix: str = "") -> str:
    return (
        '<div class="bundlebar" id="bundlebar" role="region" aria-label="Your bundle">'
        '<span id="bundlecount">0 items</span>'
        f'<a class="btn" href="{e(prefix)}bundle.html">Review bundle</a>'
        '<button type="button" class="clear" data-clear-basket>Clear</button>'
        "</div>"
    )


# ---------------------------------------------------------------------------
# Cards and grids
# ---------------------------------------------------------------------------


def _fulfilment_badge(p: dict) -> str:
    """Every card says how the buyer gets it home. No card is silent on it."""
    if p["shipping"] and not p["pickup_only"]:
        return '<span class="badge ships">Can be shipped</span>'
    if p["shipping"]:
        return '<span class="badge ships">Pickup or shipping</span>'
    return '<span class="badge pickup">Local pickup</span>'


def _card_html(p: dict, prefix: str, eager: bool) -> str:
    photo = p["photos"][0] if p["photos"] else None
    if photo:
        size = ""
        if photo.get("w") and photo.get("h"):
            size = f' width="{photo["w"]}" height="{photo["h"]}"'
        loading = (
            'loading="eager" fetchpriority="high"' if eager else 'loading="lazy"'
        )
        shot = (
            f'<img src="{e(prefix + photo["src"])}" alt="{e(p["name"])}"{size} '
            f'{loading} decoding="async">'
        )
    else:
        shot = '<div class="noshot">Photographs on request</div>'

    tag = '<span class="tag sold">Sold</span>' if p["sold"] else ""
    price = (
        "Sold" if p["sold"]
        else (_money(p["price"]) if p["price"] else "Price on request")
    )
    pickup_line = ""
    if not p["sold"] and p["pickup_price"] and p["pickup_price"] != p["price"]:
        pickup_line = (
            f'<span class="was">{e(_money(p["pickup_price"]))} if you collect</span>'
        )

    search = " ".join(
        [p["name"], p["brand"], p["id"], p["category"], p["condition"], p["description"]]
    ).lower()

    add_button = ""
    if not p["sold"]:
        add_button = (
            f'<button type="button" class="add" data-add="{e(p["id"])}" '
            'aria-pressed="false">Add to bundle</button>'
        )

    return (
        '<li class="card{sold}" data-item data-id="{id}" data-category="{cat}" '
        'data-condition="{cond}" data-price="{price_num}" data-sold="{is_sold}" '
        'data-ships="{ships}" data-pickup="{pickup}" data-added="{added}" '
        'data-name="{name_attr}" data-search="{search}">'
        '<div class="shot">{shot}{tag}</div>'
        '<div class="body">{brand}'
        '<h3><a href="{prefix}items/{id}.html">{name}</a></h3>'
        '<p class="meta sans">{cond}{dims}</p>'
        '<p class="badges">{badge}</p>'
        '<p class="price">{price}{pickup_line}</p>'
        '<p class="idtag">{id}</p>'
        "</div>{add}</li>"
    ).format(
        sold=" sold" if p["sold"] else "",
        id=e(p["id"]),
        cat=e(p["category"]),
        cond=e(p["condition"]),
        price_num=e(p["price"] if p["price"] else ""),
        is_sold="1" if p["sold"] else "0",
        ships="1" if p["shipping"] else "0",
        pickup="1" if p["pickup_only"] else "0",
        added=e(p["submitted_epoch"]),
        name_attr=e(p["name"]),
        search=e(search),
        shot=shot,
        tag=tag,
        brand=f'<p class="brand">{e(p["brand"])}</p>' if p["brand"] else "",
        prefix=e(prefix),
        name=e(p["name"]),
        dims=f' &middot; {e(p["dimensions"])}' if p["dimensions"] else "",
        badge=_fulfilment_badge(p),
        price=e(price),
        pickup_line=pickup_line,
        add=add_button,
    )


def _grid_html(payloads, prefix: str) -> str:
    cards = [_card_html(p, prefix, eager=(n < 4)) for n, p in enumerate(payloads)]
    return '<ul class="grid" id="grid">' + "".join(cards) + "</ul>"


def _category_nav(categories, prefix: str, current: str = "") -> str:
    """Category chips. Only categories that actually have something in them.

    An empty category is a dead end that makes a small catalogue look
    abandoned, so ``build_site`` never passes one in.
    """
    links = [
        '<li><a href="{}index.html"{}>All</a></li>'.format(
            e(prefix), ' aria-current="page"' if not current else ""
        )
    ]
    for name, count in categories:
        mark = ' aria-current="page"' if name == current else ""
        links.append(
            f'<li><a href="{e(prefix)}category/{e(_slug(name))}.html"{mark}>'
            f'{e(name)} <span aria-hidden="true">({count})</span>'
            f'<span class="hp">, {count} items</span></a></li>'
        )
    return '<ul class="cats sans" aria-label="Categories">' + "".join(links) + "</ul>"


def _filters_html(conditions, categories=None) -> str:
    cat_block = ""
    if categories:
        options = "".join(f"<option>{e(c)}</option>" for c, _ in categories)
        cat_block = (
            '<div><label for="cat">Category</label>'
            f'<select id="cat"><option value="">All categories</option>{options}'
            "</select></div>"
        )
    cond_options = "".join(f"<option>{e(c)}</option>" for c in conditions)
    return (
        '<form class="filters sans" role="search" aria-label="Filter and sort" '
        'onsubmit="return false">'
        '<div><label for="q">Search</label>'
        '<input id="q" type="search" placeholder="Name, brand, or reference" '
        'autocomplete="off"></div>'
        f"{cat_block}"
        '<div><label for="cond">Condition</label>'
        f'<select id="cond"><option value="">Any condition</option>{cond_options}'
        "</select></div>"
        '<div><label for="fulfil">Getting it home</label>'
        '<select id="fulfil"><option value="">Either</option>'
        '<option value="ship">Can be shipped</option>'
        '<option value="pickup">Local pickup</option></select></div>'
        '<div><label for="max">Maximum price</label>'
        '<input id="max" type="number" min="0" step="25" placeholder="Any" '
        'inputmode="numeric"></div>'
        '<div><label for="avail">Availability</label>'
        '<select id="avail"><option value="all">Everything, including sold</option>'
        '<option value="available">Available only</option></select></div>'
        '<div><label for="sort">Sort by</label>'
        '<select id="sort"><option value="newest">Most recently added</option>'
        '<option value="price-asc">Price: low to high</option>'
        '<option value="price-desc">Price: high to low</option>'
        '<option value="name">Name A to Z</option></select></div>'
        '<div style="display:flex;align-items:flex-end">'
        '<button type="button" class="btn ghost" id="reset">Reset</button></div>'
        "</form>"
    )


# ---------------------------------------------------------------------------
# Inquiry forms
# ---------------------------------------------------------------------------


def _inquiry_form(*, legend: str, item_id: str = "", basket: bool = False,
                  message_default: str = "", message_required: bool = False) -> str:
    if item_id:
        hidden = f'<input type="hidden" name="item_id" value="{e(item_id)}">'
    elif basket:
        hidden = '<input type="hidden" name="items" id="basketfield" value="">'
    else:
        hidden = (
            '<div><label for="ref">Item reference(s), if you have them</label>'
            '<input id="ref" name="item_id" autocomplete="off"></div>'
        )

    return (
        '<form class="inquiry sans" id="inquiry-form" novalidate>'
        f'<h2 style="font-size:19px;margin-bottom:14px">{e(legend)}</h2>'
        f"{hidden}"
        '<div class="hp" aria-hidden="true">'
        '<label for="website">Leave this field empty</label>'
        '<input id="website" name="website" tabindex="-1" autocomplete="off"></div>'
        '<div><label for="iname">Your name</label>'
        '<input id="iname" name="name" required autocomplete="name"></div>'
        '<div><label for="icontact">Email or phone</label>'
        '<input id="icontact" name="contact" required autocomplete="email"></div>'
        '<div><label for="ioffer">Your offer, if you want to make one (optional)</label>'
        '<input id="ioffer" name="offer" inputmode="decimal" autocomplete="off"></div>'
        '<div><label for="imessage">Message</label>'
        '<textarea id="imessage" name="message"{req}>{msg}</textarea></div>'
        '<button class="btn" type="submit">Send enquiry</button>'
        '<p class="formnote" id="formnote" role="status" aria-live="polite"></p>'
        "</form>"
    ).format(req=" required" if message_required else "", msg=e(message_default))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _index_html(payloads, categories, conditions, *, brand, tagline, email, region,
                site_url, config, preview) -> str:
    available = sum(1 for p in payloads if not p["sold"])
    description = (
        f"{available} pieces available from a private household collection"
        + (f" in {region}" if region else "")
        + ". Photographed, measured, and described with the flaws stated plainly."
    )
    hero = next((p["photos"][0]["src"] for p in payloads if p["photos"]), "")
    body = (
        _grid_html(payloads, "")
        if payloads
        else '<p class="empty">Nothing is listed just yet. Please check back shortly '
        "&mdash; or send us a note from the contact page and tell us what you are "
        "looking for.</p>"
    )

    return (
        '<!doctype html><html lang="en"><head>'
        + _head(
            title=brand,
            description=description,
            brand=brand,
            canonical=(site_url + "/") if site_url else "",
            image=(site_url + "/" + hero) if (site_url and hero) else "",
        )
        + "</head><body>"
        + '<a class="skip" href="#browse">Skip to the catalogue</a>'
        + _preview_banner(preview)
        + _site_header(brand, tagline)
        + _nav(current="index")
        + '<main id="browse">'
        + f'<div class="notice">{e(CROSS_LIST_NOTICE)}</div>'
        + "<h2>Browse the collection</h2>"
        + _category_nav(categories, "")
        + _filters_html(conditions, categories)
        + '<p class="count sans" id="count" role="status" aria-live="polite"></p>'
        + body
        + '<p class="empty" id="empty" hidden>Nothing matches those filters. Try '
        "widening the price or clearing the search.</p>"
        + "</main>"
        + _footer(brand, email, region)
        + _bundle_bar()
        + f"<script>{_basket_js(payloads, config)}</script>"
        + f"<script>{_BROWSE_JS}</script>"
        + "</body></html>"
    )


def _category_html(name, payloads, all_payloads, categories, conditions, *, brand,
                   email, region, site_url, config, preview) -> str:
    description = (
        f"{len(payloads)} pieces in {name.lower()} from a private household collection"
        + (f" in {region}" if region else "")
        + "."
    )
    hero = next((p["photos"][0]["src"] for p in payloads if p["photos"]), "")
    return (
        '<!doctype html><html lang="en"><head>'
        + _head(
            title=f"{name} · {brand}",
            description=description,
            brand=brand,
            canonical=(f"{site_url}/category/{_slug(name)}.html") if site_url else "",
            image=(site_url + "/" + hero) if (site_url and hero) else "",
        )
        + "</head><body>"
        + '<a class="skip" href="#browse">Skip to the catalogue</a>'
        + _preview_banner(preview)
        + _site_header(brand, "", "../", show_tagline=False)
        + _nav("../", current="index")
        + '<main id="browse">'
        + f"<h2>{e(name)}</h2>"
        + _category_nav(categories, "../", current=name)
        + _filters_html(conditions)
        + '<p class="count sans" id="count" role="status" aria-live="polite"></p>'
        + _grid_html(payloads, "../")
        + '<p class="empty" id="empty" hidden>Nothing matches those filters.</p>'
        + "</main>"
        + _footer(brand, email, region)
        + _bundle_bar("../")
        + f"<script>{_basket_js(all_payloads, config)}</script>"
        + f"<script>{_BROWSE_JS}</script>"
        + "</body></html>"
    )


def _gallery_html(p: dict) -> str:
    if not p["photos"]:
        return '<p class="sans">Photographs are available on request — just ask.</p>'
    total = len(p["photos"])
    out = []
    for n, photo in enumerate(p["photos"], start=1):
        size = ""
        if photo.get("w") and photo.get("h"):
            size = f' width="{photo["w"]}" height="{photo["h"]}"'
        loading = 'loading="eager" fetchpriority="high"' if n == 1 else 'loading="lazy"'
        alt = _photo_alt(p["name"], photo["file"], n, total)
        out.append(
            f'<figure><img src="../{e(photo["src"])}" alt="{e(alt)}"{size} '
            f'{loading} decoding="async"></figure>'
        )
    return "".join(out)


def _item_html(p, *, brand, email, region, site_url, config, all_payloads,
               preview) -> str:
    spec_rows = []
    if p["brand"]:
        spec_rows.append(("Maker", p["brand"]))
    spec_rows.append(("Condition", p["condition"]))
    if p["dimensions"]:
        spec_rows.append(("Dimensions", p["dimensions"]))
    if p["weight"]:
        spec_rows.append(("Weight", "{:.0f} lb".format(float(p["weight"]))))
    if p["accessories"]:
        spec_rows.append(("What is included", p["accessories"]))
    spec_rows.append(
        (
            "Getting it home",
            p["shipping_statement"]
            or ("Local pickup only" if p["pickup_only"] else "By arrangement"),
        )
    )
    if region:
        spec_rows.append(("Located in", region))
    spec_rows.append(("Reference", p["id"]))
    spec = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in spec_rows)

    defects = str(p["defects"] or "").strip()
    disclosure = (
        defects
        if defects and defects.lower() not in ("none", "n/a")
        else "Nothing was found on inspection. It is pre-owned and may show wear "
        "consistent with its age — ask for more photographs of any area and we "
        "will take them."
    )
    disclose_block = (
        '<div class="disclose"><strong>What is wrong with it</strong>'
        f"{e(disclosure)}"
        '<p style="margin-top:8px;font-size:14px">We would rather you saw a flaw '
        "here than found it in a driveway.</p></div>"
    )

    if p["sold"]:
        price_block = (
            '<p style="font-size:20px;color:#6d6862;margin:8px 0">This piece has '
            'sold.</p><p class="sans" style="font-size:15px;color:#57524a">It is left '
            "up so a shared link still works, and so nobody spends an evening asking "
            "about something that has gone.</p>"
        )
    else:
        pickup = ""
        if p["pickup_price"] and p["pickup_price"] != p["price"]:
            pickup = (
                '<p class="sans" style="font-size:15px;color:#57524a">'
                f'{e(_money(p["pickup_price"]))} if you collect it yourself.</p>'
            )
        price_block = (
            '<p style="font-size:31px;margin:8px 0">'
            + e(_money(p["price"]) if p["price"] else "Price on request")
            + "</p>"
            + pickup
        )

    details = ""
    if p["key_details"]:
        details = (
            '<ul class="sans" style="margin:14px 0 0 20px;font-size:15px">'
            + "".join(f"<li>{e(d)}</li>" for d in p["key_details"])
            + "</ul>"
        )

    add_button = ""
    if not p["sold"]:
        add_button = (
            '<p style="margin:18px 0"><button type="button" class="btn ghost" '
            f'data-add="{e(p["id"])}" aria-pressed="false">Add to bundle</button>'
            '<span class="sans" style="display:block;font-size:14px;color:#57524a;'
            'margin-top:8px">Buying several things is usually cheaper — add what you '
            "want and ask for one price.</span></p>"
        )

    hero = p["photos"][0]["src"] if p["photos"] else ""
    description = (p["description"] or p["name"])[:280]

    return (
        '<!doctype html><html lang="en"><head>'
        + _head(
            title=f"{p['name']} · {brand}",
            description=description,
            brand=brand,
            canonical=(f"{site_url}/items/{p['id']}.html") if site_url else "",
            image=(site_url + "/" + hero) if (site_url and hero) else "",
            price=p["price"] if p["price"] else None,
            sold=p["sold"],
            kind="product",
        )
        + "</head><body>"
        + '<a class="skip" href="#detail">Skip to this item</a>'
        + _preview_banner(preview)
        + _site_header(brand, "", "../", show_tagline=False)
        + _nav("../")
        + "<main>"
        + '<p class="sans" style="margin-bottom:18px"><a href="../index.html">'
        "&larr; All pieces</a> &middot; "
        + f'<a href="../category/{e(_slug(p["category"]))}.html">{e(p["category"])}</a></p>'
        + '<div class="detail" id="detail">'
        + f'<div class="gallery">{_gallery_html(p)}</div>'
        + "<div>"
        + (f'<p class="brand sans">{e(p["brand"])}</p>' if p["brand"] else "")
        + f'<h2 style="font-size:29px">{e(p["name"])}</h2>'
        + (
            f'<p class="sans" style="color:#57524a">{e(p["subtitle"])}</p>'
            if p["subtitle"]
            else ""
        )
        + f'<p class="idtag">{e(p["id"])}</p>'
        + price_block
        + f'<p class="badges">{_fulfilment_badge(p)}</p>'
        + f'<p style="margin:16px 0">{e(p["description"])}</p>'
        + details
        + f'<table class="spec">{spec}</table>'
        + disclose_block
        + add_button
        + f'<div class="notice">{e(CROSS_LIST_NOTICE)}</div>'
        + (
            ""
            if p["sold"]
            else _inquiry_form(
                legend=f"Ask about {p['id']}",
                item_id=p["id"],
                message_default=f"I'm interested in {p['name']} ({p['id']}).",
            )
        )
        + "</div></div></main>"
        + _footer(brand, email, region)
        + _bundle_bar("../")
        + f"<script>{_basket_js(all_payloads, config)}</script>"
        + f"<script>{_form_js('../api/inquiry', email)}</script>"
        + "</body></html>"
    )


def _basket_row(p: dict) -> str:
    photo = p["photos"][0] if p["photos"] else None
    img = (
        f'<img src="{e(photo["src"])}" alt="" loading="lazy" decoding="async">'
        if photo
        else '<span class="noimg" aria-hidden="true"></span>'
    )
    return (
        f'<li data-basket-row="{e(p["id"])}" hidden>'
        f"{img}"
        f'<div><a href="items/{e(p["id"])}.html">{e(p["name"])}</a>'
        f'<p class="meta sans idtag">{e(p["id"])} &middot; {e(p["condition"])}</p></div>'
        '<div style="text-align:right"><p>'
        f'{e(_money(p["price"]) if p["price"] else "On request")}</p>'
        f'<button type="button" class="add" style="margin:6px 0 0" data-add="{e(p["id"])}" '
        'data-keep-label aria-pressed="true">Remove</button></div>'
        "</li>"
    )


def _bundle_html(payloads, *, brand, email, region, site_url, config, preview) -> str:
    sellable = [p for p in payloads if not p["sold"]]
    tier_rows = "".join(
        "<li>{} or more items — about {:.0f}% off</li>".format(
            t["min_items"], t["discount_pct"] * 100
        )
        for t in config["tiers"]
    )
    rows = "".join(_basket_row(p) for p in sellable)

    return (
        '<!doctype html><html lang="en"><head>'
        + _head(
            title=f"Your bundle · {brand}",
            description="Pick several pieces and get one price for the lot.",
            brand=brand,
            canonical=(f"{site_url}/bundle.html") if site_url else "",
        )
        + "</head><body>"
        + '<a class="skip" href="#basket">Skip to your bundle</a>'
        + _preview_banner(preview)
        + _site_header(brand, "", show_tagline=False)
        + _nav(current="bundle")
        + '<main id="basket">'
        + "<h2>Your bundle</h2>"
        + '<p style="margin:12px 0 20px;color:#57524a">Buying more than one thing is '
        "welcome and usually cheaper. Add whatever you want and send one message — we "
        "reply with a single price for the lot.</p>"
        + f'<div class="notice">{e(BUNDLE_NOTICE)}</div>'
        + '<div id="basketempty"><p class="empty">Nothing selected yet. '
        '<a href="index.html">Browse the collection</a> and press &ldquo;Add to '
        "bundle&rdquo; on anything you like.</p></div>"
        + '<div id="basketfilled" hidden>'
        + f'<ul class="basket">{rows}</ul>'
        + '<div class="totals sans">'
        '<div><span>Items</span><strong id="sumcount">0 items</strong></div>'
        '<div><span>Subtotal</span><strong id="sumsubtotal">$0</strong></div>'
        '<div><span>Bundle discount</span>'
        '<strong class="save" id="sumdiscount">None yet</strong></div>'
        '<div class="grand"><span>Indicative total</span>'
        '<strong id="sumtotal">&mdash;</strong></div>'
        '<p id="sumcapped" hidden style="margin-top:10px;font-size:14px;color:#57524a">'
        "One of these is already priced close to the least we can take for it, so the "
        "discount on this particular selection is smaller than the usual rate. We will "
        "still do our best for you.</p>"
        '<p style="margin-top:10px;font-size:14px;color:#57524a">This figure is '
        "indicative. A person checks every bundle and confirms the real number when we "
        "reply — the website never sets a final price.</p>"
        "</div>"
        + '<h2 style="margin-top:28px;font-size:20px">Send us the lot</h2>'
        + _inquiry_form(legend="One message, every item above", basket=True)
        + '<p class="sans" style="margin-top:18px;font-size:14px;color:#57524a">'
        '<label for="sharelink">Link to this selection — copy it to share, or to come '
        'back later</label><input id="sharelink" readonly style="width:100%;padding:10px;'
        'border:1px solid #cfc8bb;border-radius:4px;font-size:15px"></p>'
        + "</div>"
        + '<h2 style="margin-top:32px;font-size:20px">How bundle pricing works</h2>'
        + f'<ul class="sans" style="margin:12px 0 0 20px">{tier_rows}</ul>'
        + '<p class="sans" style="margin-top:12px;color:#57524a;font-size:15px">'
        "Some pieces are already priced near the lowest we can go, and those cannot "
        "absorb the full discount. When one of them is in your bundle the percentage "
        "shown will be smaller — the figure on screen always accounts for it.</p>"
        + "</main>"
        + _footer(brand, email, region)
        + _bundle_bar()
        + f"<script>{_basket_js(payloads, config)}</script>"
        + f"<script>{_form_js('api/inquiry', email)}</script>"
        + "</body></html>"
    )


def _about_html(*, brand, tagline, email, region, site_url, payloads, config,
                preview) -> str:
    return (
        '<!doctype html><html lang="en"><head>'
        + _head(
            title=f"About and contact · {brand}",
            description=f"How this sale works, and how to reach us. {tagline}",
            brand=brand,
            canonical=(f"{site_url}/about.html") if site_url else "",
        )
        + "</head><body>"
        + '<a class="skip" href="#about">Skip to the content</a>'
        + _preview_banner(preview)
        + _site_header(brand, tagline)
        + _nav(current="about")
        + '<main id="about" style="max-width:740px">'
        + "<h2>About this collection</h2>"
        + '<p style="margin:14px 0">This is a private household collection being sold '
        "ahead of a move. Every piece is photographed as it actually is, measured, and "
        "described with its flaws stated plainly. What you see is what you get.</p>"
        + '<p style="margin:14px 0">Most pieces are available for local collection'
        + (f" in {e(region)}" if region else "")
        + ". Smaller items can be shipped. Larger pieces carry a local-collection price "
        "that reflects the cost we avoid when a buyer takes it away themselves.</p>"
        + f'<div class="notice">{e(CROSS_LIST_NOTICE)}</div>'
        + '<h2 style="margin:26px 0 12px">Why we list the damage</h2>'
        + '<p style="margin:14px 0">Every listing has a section saying what is wrong '
        "with the item. A scratch photographed and described is a scratch nobody argues "
        "about in a driveway. If we have missed something, tell us and we will add "
        "it.</p>"
        + '<h2 style="margin:26px 0 12px">Bundles</h2>'
        + '<p style="margin:14px 0">Buying several pieces is welcome and usually '
        'cheaper. Add what you want to <a href="bundle.html">your bundle</a> and send '
        "one message — you will see an indicative price straight away, and we confirm "
        "the real figure when we reply.</p>"
        + '<h2 style="margin:26px 0 12px">Your privacy</h2>'
        + '<p style="margin:14px 0">This site sets no cookies, runs no analytics, and '
        "loads nothing from any other company. Nobody is told that you visited. What "
        "you type into the form below goes to us and nowhere else.</p>"
        + '<h2 style="margin:26px 0 12px">Get in touch</h2>'
        + _inquiry_form(legend="Send us a message", message_required=True)
        + "</main>"
        + _footer(brand, email, region)
        + _bundle_bar()
        + f"<script>{_basket_js(payloads, config)}</script>"
        + f"<script>{_form_js('api/inquiry', email)}</script>"
        + "</body></html>"
    )


def _sitemap(site_url: str, payloads, categories) -> str:
    urls = ["", "about.html", "bundle.html"]
    urls += [f"category/{_slug(name)}.html" for name, _ in categories]
    urls += [f"items/{p['id']}.html" for p in payloads]
    today = date.today().isoformat()
    entries = "".join(
        f"<url><loc>{e(site_url)}/{e(u)}</loc><lastmod>{today}</lastmod></url>"
        for u in urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</urlset>"
    )


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_site(session, out_dir: Path | str = "estate/site", brand: str = "",
               tagline: str = "", email: str = "", region: str = "",
               api_base: str = "", catalog_url: str = "",
               include_mock: bool = False) -> dict:
    """Generate the whole static site. Returns a build report.

    ``api_base`` is accepted for backward compatibility with existing callers
    (``scripts/estate_site.py --api-base``, ``estate/demo.py``) but is no
    longer used: the generated inquiry forms POST to a same-origin, relative
    ``api/inquiry`` path served by the standalone serverless function this
    build emits (see ``estate/serverless.py``), never to the private VPS API.
    Passing it is a no-op.

    ``include_mock`` produces a **preview build**: sample data is allowed
    through the publication gates, every page carries a visible preview
    banner, and ``robots.txt`` disallows everything. It exists for
    ``estate/demo.py`` and for looking at layout changes without real
    inventory. A preview build must never be deployed publicly, and the
    report's first warning says so.
    """
    from estate._compat import get_settings

    settings = get_settings()
    brand = brand or settings.estate_brand_name or "The Collection"
    tagline = tagline or (
        "A private household collection, offered ahead of a move. Each piece "
        "photographed, measured, and described honestly."
    )
    email = email or settings.estate_selling_email
    region = region or settings.estate_pickup_region
    catalog_url = catalog_url or settings.estate_catalog_url
    site_url = (catalog_url or "").rstrip("/")

    config = bundling.bundle_config()

    out = Path(out_dir)
    (out / "items").mkdir(parents=True, exist_ok=True)
    (out / "category").mkdir(parents=True, exist_ok=True)
    photos_dir = out / "photos"
    if photos_dir.exists():
        shutil.rmtree(photos_dir)
    photos_dir.mkdir(parents=True, exist_ok=True)

    items = collect(session, include_mock=include_mock)
    report = {
        "items": 0,
        "photos": 0,
        "categories": 0,
        "warnings": [],
        "output": str(out.resolve()),
        "built": date.today().isoformat(),
        "preview": bool(include_mock),
        "bundle_tiers": config["tiers"],
    }

    if include_mock:
        report["warnings"].append(
            "PREVIEW BUILD: publication gates were relaxed and sample data may be "
            "present. Every page is stamped and robots.txt disallows crawling. Do "
            "not deploy this build publicly."
        )
    if not images.pillow_available():
        report["warnings"].append(
            "Pillow is not installed. Original photographs were copied without "
            "resizing OR EXIF stripping — do NOT publish this build. Install Pillow "
            "and rebuild."
        )
    if not site_url:
        report["warnings"].append(
            "ESTATE_CATALOG_URL is not set, so share-preview tags carry no absolute "
            "URLs and no sitemap was written. A link pasted into Facebook or a text "
            "message will show a bare address instead of the photograph and price. "
            "Set it before publishing."
        )
    if not email:
        report["warnings"].append(
            "ESTATE_SELLING_EMAIL is not set, so a buyer whose enquiry fails to send "
            "is given no fallback route to reach you."
        )

    payloads = []
    for item in items:
        images.build_web_images(session, item.item_id)
        payload = _item_payload(session, item, "photos", config["tiers"])

        dest_dir = photos_dir / item.item_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        kept = []
        for photo in payload["photos"]:
            name = photo["file"]
            src = paths.item_dir(item.item_id) / "web" / name
            if not src.exists():
                src = paths.item_dir(item.item_id) / "original" / name
            if not src.exists():
                continue
            dest = dest_dir / name
            shutil.copy2(src, dest)
            width, height = _image_size(dest)
            kept.append(
                {
                    "file": name,
                    "src": f"photos/{item.item_id}/{name}",
                    "w": width,
                    "h": height,
                }
            )
        payload["photos"] = kept
        report["photos"] += len(kept)
        payloads.append(payload)

    # Newest first is the server-side default, so the page is already in a
    # sensible order before any JavaScript runs. Sold pieces sink.
    payloads.sort(key=lambda p: (p["sold"], -p["submitted_epoch"], p["name"]))

    present = [p["category"] for p in payloads]
    categories = [(name, present.count(name)) for name in CATEGORIES if name in present]
    # An item whose category is outside the canonical list still needs a home.
    for name in sorted({c for c in present if c not in CATEGORIES}):
        categories.append((name, present.count(name)))
    conditions = sorted({p["condition"] for p in payloads})
    report["categories"] = len(categories)

    for payload in payloads:
        (out / "items" / f"{payload['id']}.html").write_text(
            _item_html(
                payload, brand=brand, email=email, region=region, site_url=site_url,
                config=config, all_payloads=payloads, preview=include_mock,
            ),
            encoding="utf-8",
        )

    for name, _count in categories:
        subset = [p for p in payloads if p["category"] == name]
        (out / "category" / f"{_slug(name)}.html").write_text(
            _category_html(
                name, subset, payloads, categories, conditions, brand=brand,
                email=email, region=region, site_url=site_url, config=config,
                preview=include_mock,
            ),
            encoding="utf-8",
        )

    # Item and category pages from a previous build that are no longer
    # publishable (removed, donated, rejected, un-approved, or a category that
    # emptied out) are deleted, so a stale page carrying an outdated price or
    # status never lingers on the public site.
    current_ids = {p["id"] for p in payloads}
    for existing in (out / "items").glob("*.html"):
        if existing.stem not in current_ids:
            existing.unlink()
    current_slugs = {_slug(name) for name, _ in categories}
    for existing in (out / "category").glob("*.html"):
        if existing.stem not in current_slugs:
            existing.unlink()

    for item in items:
        if item.website_status == "Queued":
            ItemRepository(session).update(
                item.item_id,
                actor="site_builder",
                website_status=(
                    "Sold (shown)"
                    if item.status == ItemStatus.SOLD.value
                    else "Published"
                ),
            )

    (out / "index.html").write_text(
        _index_html(
            payloads, categories, conditions, brand=brand, tagline=tagline,
            email=email, region=region, site_url=site_url, config=config,
            preview=include_mock,
        ),
        encoding="utf-8",
    )
    (out / "bundle.html").write_text(
        _bundle_html(
            payloads, brand=brand, email=email, region=region, site_url=site_url,
            config=config, preview=include_mock,
        ),
        encoding="utf-8",
    )
    (out / "about.html").write_text(
        _about_html(
            brand=brand, tagline=tagline, email=email, region=region,
            site_url=site_url, payloads=payloads, config=config,
            preview=include_mock,
        ),
        encoding="utf-8",
    )
    (out / "catalog.json").write_text(
        json.dumps([_public_catalog_entry(p) for p in payloads], indent=2),
        encoding="utf-8",
    )
    (out / "robots.txt").write_text(
        "User-agent: *\nDisallow: /\n"
        if include_mock
        else (
            "User-agent: *\nAllow: /\n"
            + (f"Sitemap: {site_url}/sitemap.xml\n" if site_url else "")
        ),
        encoding="utf-8",
    )
    if site_url and not include_mock:
        (out / "sitemap.xml").write_text(
            _sitemap(site_url, payloads, categories), encoding="utf-8"
        )
    elif (out / "sitemap.xml").exists():
        (out / "sitemap.xml").unlink()

    # Minimal approved-item manifest: the only thing the decoupled public
    # inquiry function needs in order to validate an item ID, price a basket,
    # and refuse anything that is spoken for -- without ever calling back into
    # the private VPS API. `band` is the published bundle constraint; the
    # floor price it derives from is NOT here and never will be.
    manifest = {
        p["id"]: {
            "status": p["status"],
            "sold": p["sold"],
            "price": p["price"],
            "band": p["band"],
        }
        for p in payloads
    }
    (out / "catalog_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (out / "bundle_config.json").write_text(
        json.dumps(
            {
                "enabled": config["enabled"],
                "tiers": config["tiers"],
                "round_to": config["round_to"],
                "max_items": config["max_items"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    serverless.emit_inquiry_function(out)

    report["items"] = len(payloads)
    if not include_mock:
        report["held_back"] = held_back(session)
        for item_id, blockers in report["held_back"]:
            report["warnings"].append(
                f"{item_id} is approved but is NOT on the site: " + " ".join(blockers)
            )
    else:
        report["held_back"] = []
    if not payloads:
        report["warnings"].append(
            "No approved items yet, so the catalogue is empty. Approve items in the "
            "review interface first."
        )
    return report
