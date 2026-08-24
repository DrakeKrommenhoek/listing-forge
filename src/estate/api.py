"""Review and approval web interface.

Security posture
----------------
This interface can change prices and approve items for publication, so it is
NOT safe to expose to the internet as-is. Two layers guard it:

1. It is only mounted when ``ESTATE_ENABLED`` is true.
2. Every route requires ``ESTATE_REVIEW_TOKEN`` — passed as ``?token=`` once,
   then held in a cookie. If the token is unset, the routes refuse to serve
   anything rather than defaulting to open.

Bind the API to 127.0.0.1 and reach it over an SSH tunnel:

    ssh -L 8000:127.0.0.1:8000 user@vps

That is the documented deployment. Putting this behind a public URL requires
real authentication, which this MVP does not have.
"""

from __future__ import annotations

import html
import secrets
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from estate._compat import get_settings
from estate import approval, exporter, paths, pricing
from estate.repository import InquiryRepository, ItemRepository
from estate.schema import CONDITIONS, ItemStatus
from estate._compat import get_logger
from estate._compat import get_session

logger = get_logger(__name__)

router = APIRouter(prefix="/estate", tags=["estate"])

COOKIE = "estate_review"


_TOKEN_WARNED = False


def _token() -> str:
    """The review interface's shared secret, however it was configured.

    Read through Settings rather than straight from ``os.environ``. Both end
    up in the same place when systemd starts the service with
    ``EnvironmentFile=``, but that is not the only way this process gets run:
    ``make run``, a shell with the venv activated, Docker Compose, and a
    developer laptop all load ``.env`` through pydantic-settings and leave
    ``os.environ`` untouched. In every one of those cases the old read
    returned empty, every route answered "Not authorised", and the page
    helpfully explained that the variable must be unset — while it sat in
    ``.env`` the whole time.

    An operator cannot debug that from the browser, so the unset case is also
    logged once, server-side. The value itself is never logged.
    """
    global _TOKEN_WARNED

    from estate._compat import get_settings

    try:
        token = (get_settings().estate_review_token or "").strip()
    except Exception:  # noqa: BLE001 - a config error must not 500 every route
        token = ""

    if not token and not _TOKEN_WARNED:
        _TOKEN_WARNED = True
        logger.warning({
            "action": "estate_review_disabled",
            "reason": "ESTATE_REVIEW_TOKEN is not set",
            "fix": "set it in .env (or the environment) and restart drake-api",
        })
    return token


def _authorised(request: Request) -> bool:
    expected = _token()
    if not expected:
        return False
    supplied = request.query_params.get("token") or request.cookies.get(COOKIE) or ""
    return secrets.compare_digest(supplied, expected)


def _deny() -> HTMLResponse:
    return HTMLResponse(
        "<h1>Not authorised</h1><p>Append <code>?token=…</code> using the value of "
        "ESTATE_REVIEW_TOKEN. If that variable is unset, the review interface is "
        "disabled by design.</p>",
        status_code=401,
    )


def e(value) -> str:
    return html.escape("" if value is None else str(value))


def _money(v) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health")
def estate_health() -> JSONResponse:
    """Unauthenticated liveness probe. Deliberately exposes counts only."""
    session = get_session()
    try:
        items = ItemRepository(session).all()
        by_status: dict = {}
        for i in items:
            by_status[i.status] = by_status.get(i.status, 0) + 1
        return JSONResponse(
            {
                "status": "ok",
                "estate_enabled": bool(get_settings().estate_enabled),
                "items": len(items),
                "by_status": by_status,
                "awaiting_approval": sum(
                    1 for i in items if i.approval_status == "Pending"
                ),
                "review_ui": "enabled" if _token() else "disabled (no ESTATE_REVIEW_TOKEN)",
            }
        )
    except Exception as exc:
        logger.error({"action": "estate_health_failed", "error_type": type(exc).__name__})
        return JSONResponse({"status": "error", "detail": type(exc).__name__}, status_code=500)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

STYLE = """
:root{--ink:#1a1a1a;--muted:#6b6b6b;--line:#e3e0da;--bg:#faf8f5;--accent:#2f4858;
--warn:#9c0006;--warnbg:#fdecea;--ok:#1b5e20;--okbg:#e8f5e9;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
header{background:var(--accent);color:#fff;padding:18px 28px}
header a{color:#cfe3ee;text-decoration:none;margin-right:18px}
h1{margin:0;font-size:20px;font-weight:600;letter-spacing:.02em}
main{max-width:1180px;margin:0 auto;padding:28px}
.card{background:#fff;border:1px solid var(--line);border-radius:8px;padding:20px;margin-bottom:20px}
.card h2{margin:0 0 14px;font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px}
.photos{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
.photos img{width:100%;height:170px;object-fit:cover;border-radius:6px;border:1px solid var(--line)}
label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:4px}
input,select,textarea{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:5px;
font:inherit;background:#fff}
textarea{min-height:80px}
.field{margin-bottom:14px}
.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11px;
letter-spacing:.05em;text-transform:uppercase;background:#eee;color:#444}
.badge.warn{background:var(--warnbg);color:var(--warn)}
.badge.ok{background:var(--okbg);color:var(--ok)}
.alert{background:var(--warnbg);border-left:3px solid var(--warn);padding:12px 14px;
margin-bottom:12px;font-size:14px;color:var(--warn)}
.note{background:#fffdf0;border-left:3px solid #e0b400;padding:12px 14px;margin-bottom:12px;font-size:14px}
.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:8px}
button{padding:10px 16px;border:1px solid var(--accent);background:var(--accent);color:#fff;
border-radius:5px;font:inherit;cursor:pointer}
button.secondary{background:#fff;color:var(--accent)}
button.danger{background:#fff;color:var(--warn);border-color:var(--warn)}
button[disabled]{opacity:.45;cursor:not-allowed}
.price{font-size:26px;font-weight:600}
.muted{color:var(--muted);font-size:13px}
pre{white-space:pre-wrap;background:#fbfaf8;border:1px solid var(--line);padding:12px;
border-radius:5px;font-size:13px}
a.item{color:var(--accent);font-weight:600;text-decoration:none}
"""


def _page(title: str, body: str, token: str = "") -> HTMLResponse:
    q = ("?token=" + token) if token else ""
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{e(title)}</title><style>{STYLE}</style></head><body>"
        "<header><h1>Estate Review</h1>"
        f"<div style='margin-top:8px'><a href='/estate/review{q}'>Queue</a>"
        f"<a href='/estate/export/inventory.csv{q}'>Inventory CSV</a>"
        f"<a href='/estate/export/inventory.xlsx{q}'>Workbook</a>"
        f"<a href='/estate/health'>Health</a></div></header><main>{body}</main></body></html>"
    )


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

@router.get("/review", response_class=HTMLResponse)
def review_queue(request: Request):
    if not _authorised(request):
        return _deny()
    token = request.query_params.get("token") or request.cookies.get(COOKIE) or ""
    session = get_session()
    try:
        items = approval.review_queue(session)
        rows = []
        for i in items:
            flags = []
            if i.approval_status == "Pending":
                flags.append("<span class='badge warn'>needs approval</span>")
            if i.pricing_confidence in ("Low", "Insufficient Evidence"):
                flags.append(f"<span class='badge warn'>{e(i.pricing_confidence)}</span>")
            if i.approval_status == "Approved":
                flags.append("<span class='badge ok'>approved</span>")
            rows.append(
                "<tr><td><a class='item' href='/estate/review/{}?token={}'>{}</a></td>"
                "<td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(e(i.item_id), e(token), e(i.item_id), e(i.item_name or "unnamed"),
                   e(i.category), e(i.status),
                   _money(i.current_price), _money(i.floor_price), " ".join(flags))
            )
        pending = sum(1 for i in items if i.approval_status == "Pending")
        body = (
            "<div class='card'><h2>Queue</h2>"
            "<p class='muted'>%d item(s); %d awaiting a human decision. "
            "Nothing is published until it is approved here.</p>"
            "<table><tr><th>ID</th><th>Item</th><th>Category</th><th>Status</th>"
            "<th>Price</th><th>Floor</th><th></th></tr>%s</table></div>"
            % (len(items), pending, "".join(rows) or "<tr><td colspan=7>No items yet.</td></tr>")
        )
        resp = _page("Review queue", body, token)
        resp.set_cookie(COOKIE, token, httponly=True, samesite="lax")
        return resp
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Item detail
# ---------------------------------------------------------------------------

def _field(name: str, label: str, value, kind: str = "text", choices=None) -> str:
    value = "" if value is None else value
    if choices:
        opts = "".join(
            "<option{}>{}</option>".format(" selected" if str(value) == c else "", e(c))
            for c in choices
        )
        control = f"<select name='{e(name)}'><option></option>{opts}</select>"
    elif kind == "longtext":
        control = f"<textarea name='{e(name)}'>{e(value)}</textarea>"
    else:
        control = f"<input name='{e(name)}' value='{e(value)}'>"
    return f"<div class='field'><label>{e(label)}</label>{control}</div>"


@router.get("/review/{item_id}", response_class=HTMLResponse)
def review_item(item_id: str, request: Request):
    if not _authorised(request):
        return _deny()
    token = request.query_params.get("token") or request.cookies.get(COOKIE) or ""
    settings = get_settings()
    session = get_session()
    try:
        packet = approval.prepare_review(
            session, item_id, catalog_url=settings.estate_catalog_url,
            region=settings.estate_pickup_region,
        )
        if packet.item is None:
            return _page("Not found", "<div class='card'>Item not found.</div>", token)
        item = packet.item

        photos = "".join(
            f"<img src='/estate/photo/{e(item_id)}/{e(Path(p.local_path).name)}?token={e(token)}' alt='{e(p.filename)}'>"
            for p in packet.photos if p.local_path
        ) or "<p class='muted'>No photographs stored.</p>"

        blockers = "".join(f"<div class='alert'>{e(b)}</div>" for b in packet.blockers)
        warnings = "".join(
            f"<div class='note'>{e(w)}</div>" for w in (packet.price.warnings if packet.price else [])
        )
        if packet.missing:
            warnings += ("<div class='note'>Still unanswered: {}</div>".format(e(", ".join(packet.missing))))
        if packet.low_confidence_fields:
            warnings += ("<div class='note'>Low model confidence on: {}</div>".format(e(", ".join(packet.low_confidence_fields))))

        comp_rows = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td><td><a href='{}' rel='noreferrer noopener' target='_blank'>source</a></td></tr>".format("<span class='badge warn'>MOCK</span> " + e(c.platform) if c.is_placeholder
               else e(c.platform),
               e(c.title[:70]), "Sold" if c.is_sold else "Active",
               _money(c.total_price), e(c.condition), e(c.observed_date),
               f"{c.relevance:.1f}", e(c.url))
            for c in packet.comps
        ) or f"<tr><td colspan=8 class='muted'>No comparable evidence recorded. Worksheet: {e(packet.worksheet_path)}</td></tr>"

        s = packet.summary
        pr = packet.price
        inc = packet.incentive
        prices = (
            "<div class='grid'>"
            "<div><label>Comparable range</label><div>%s – %s (median %s)</div></div>"
            "<div><label>Confidence</label><div><span class='badge %s'>%s</span> "
            "<span class='muted'>score %.2f, n=%d, %d sold</span></div></div>"
            "<div><label>Recommended list</label><div class='price'>%s</div></div>"
            "<div><label>Expected sale</label><div class='price'>%s</div></div>"
            "<div><label>Floor (never go below)</label><div class='price'>%s</div></div>"
            "<div><label>Local pickup price</label><div class='price'>%s</div>"
            "<div class='muted'>%s</div></div>"
            "</div>"
            % (_money(s.low), _money(s.high), _money(s.median),
               "warn" if s.confidence in ("Low", "Insufficient Evidence") else "ok",
               e(s.confidence), s.confidence_score, s.comp_count, s.sold_count,
               _money(pr.initial_list_price if pr else None),
               _money(pr.expected_sale_price if pr else None),
               _money(pr.floor_price if pr else None),
               _money(inc.pickup_price if inc else None),
               e(inc.explain() if inc else ""))
        )

        primary = packet.markets.get("primary")
        mk = "<p><strong>{}</strong>{}</p>".format(
            e(primary.platform.name if primary else "No suitable marketplace"),
            (" <span class='muted'>est. fee %.1f%%</span>" % (primary.estimated_fee_pct * 100))
            if primary else "",
        )
        if primary:
            mk += "<ul>" + "".join(f"<li>{e(r)}</li>" for r in primary.reasons) + "</ul>"
        if packet.markets.get("secondary"):
            mk += "<p class='muted'>Also: {}</p>".format(e(
                ", ".join(f.platform.name for f in packet.markets["secondary"])
            ))
        if packet.markets.get("rejected"):
            mk += "<p class='muted'>Ruled out: {}</p>".format(e(
                "; ".join(f"{f.platform.name} ({f.blockers[0]})"
                          for f in packet.markets["rejected"][:5])
            ))
        for w in packet.markets.get("warnings", []):
            mk += f"<div class='note'>{e(w)}</div>"

        copy_blocks = "".join(
            f"<details><summary><strong>{e(pkg.platform_name)}</strong></summary><pre>{e(pkg.to_markdown())}</pre></details>"
            for pkg in packet.packages.values()
        ) or "<p class='muted'>No listing copy generated yet.</p>"

        edit = (
            _field("item_name", "Item name", item.item_name)
            + _field("brand", "Brand", item.brand)
            + _field("model", "Model", item.model)
            + _field("condition", "Condition", item.condition, choices=CONDITIONS)
            + _field("defects", "Defects (always disclose)", item.defects, "longtext")
            + _field("dimensions", "Dimensions", item.dimensions)
            + _field("weight_lbs", "Weight (lbs)", item.weight_lbs)
            + _field("included_accessories", "Included accessories", item.included_accessories)
            + _field("location_in_house", "Room", item.location_in_house)
            + _field("ownership_approval", "Ownership confirmed",
                     "Yes" if item.ownership_approval else "No", choices=["Yes", "No"])
            + _field("shipping_feasible", "Can be shipped",
                     "Yes" if item.shipping_feasible else "No", choices=["Yes", "No"])
            + _field("initial_list_price", "Override list price", item.initial_list_price)
            + _field("floor_price", "Override floor price", item.floor_price)
            + _field("description", "Description", item.description, "longtext")
        )

        approve_attr = "" if packet.can_approve else " disabled"
        body = (
            "<div class='card'><h2>{} — {}</h2>"
            "<p><span class='badge'>{}</span> <span class='badge'>{}</span> "
            "<span class='muted'>submitted by {}</span></p>{}{}</div>"
            "<div class='card'><h2>Photographs</h2><div class='photos'>{}</div></div>"
            "<div class='card'><h2>Price</h2>{}<p class='muted'>{}</p></div>"
            "<div class='card'><h2>Comparable evidence</h2>"
            "<table><tr><th>Platform</th><th>Title</th><th>Type</th><th>Total</th>"
            "<th>Condition</th><th>Date</th><th>Rel.</th><th></th></tr>{}</table>"
            "<p class='muted'>{}</p></div>"
            "<div class='card'><h2>Marketplaces</h2>{}</div>"
            "<div class='card'><h2>Listing copy</h2>{}</div>"
            "<form method='post' action='/estate/review/{}?token={}'>"
            "<div class='card'><h2>Details — edit anything wrong</h2>{}"
            "<div class='field'><label>Note (recorded in the audit trail)</label>"
            "<textarea name='note'></textarea></div>"
            "<div class='actions'>"
            "<button name='action' value='approve'{}>Approve</button>"
            "<button class='secondary' name='action' value='save_edits'>Save edits</button>"
            "<button class='secondary' name='action' value='request_research'>Request more research</button>"
            "<button class='secondary' name='action' value='request_photos'>Request more photos</button>"
            "<button class='secondary' name='action' value='specialist'>Specialist appraisal</button>"
            "<button class='danger' name='action' value='donate'>Donate</button>"
            "<button class='danger' name='action' value='not_for_sale'>Not for sale</button>"
            "</div></div></form>".format(e(item.item_id), e(item.item_name or "unnamed"), e(item.status),
               e(item.approval_status), e(item.submission_owner), blockers, warnings,
               photos, prices, e(pr.basis if pr else ""), comp_rows,
               e(" ".join(s.gaps)), mk, copy_blocks,
               e(item_id), e(token), edit, approve_attr)
        )
        resp = _page(f"{item_id} review", body, token)
        resp.set_cookie(COOKIE, token, httponly=True, samesite="lax")
        return resp
    finally:
        session.close()


@router.post("/review/{item_id}")
async def review_action(item_id: str, request: Request):
    if not _authorised(request):
        return _deny()
    token = request.query_params.get("token") or request.cookies.get(COOKIE) or ""
    form = await request.form()
    action = form.get("action", "")
    note = form.get("note", "") or ""
    edits = {k: v for k, v in form.items() if k not in ("action", "note")}

    settings = get_settings()
    session = get_session()
    try:
        ok, message = approval.apply_decision(
            session, item_id, action, actor="reviewer:web", edits=edits, note=note,
            catalog_url=settings.estate_catalog_url, region=settings.estate_pickup_region,
        )
        logger.info({"action": "estate_review_decision", "item_id": item_id,
                     "decision": action, "ok": ok})
    finally:
        session.close()

    target = f"/estate/review/{item_id}?token={token}"
    if not ok:
        return _page(
            "Blocked",
            f"<div class='card'><div class='alert'>{e(message)}</div>"
            f"<p><a href='{e(target)}'>Back to the item</a></p></div>",
            token,
        )
    return RedirectResponse(target, status_code=303)


# ---------------------------------------------------------------------------
# Photos + exports
# ---------------------------------------------------------------------------

@router.get("/photo/{item_id}/{filename}")
def photo(item_id: str, filename: str, request: Request):
    if not _authorised(request):
        return _deny()
    try:
        safe_id = paths.safe_component(item_id)
        safe_name = paths.safe_component(filename)
    except ValueError:
        return JSONResponse({"error": "bad path"}, status_code=400)
    target = (paths.item_dir(safe_id) / "original" / safe_name).resolve()
    if not str(target).startswith(str(paths.inventory_root())) or not target.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(target)


@router.get("/export/inventory.csv")
def export_inventory_csv(request: Request):
    if not _authorised(request):
        return _deny()
    session = get_session()
    try:
        out = paths.inventory_root() / "_exports" / "inventory.csv"
        exporter.export_csv(session, out)
        return FileResponse(out, filename="inventory.csv", media_type="text/csv")
    finally:
        session.close()


@router.get("/export/inventory.xlsx")
def export_inventory_xlsx(request: Request):
    if not _authorised(request):
        return _deny()
    session = get_session()
    try:
        out = paths.inventory_root() / "_exports" / "inventory.xlsx"
        exporter.export_xlsx(session, out)
        return FileResponse(
            out, filename="inventory.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except ImportError:
        return JSONResponse(
            {"error": "openpyxl is not installed on this host; use the CSV export"},
            status_code=501,
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Buyer inquiry intake (website contact + bundle request form)
# ---------------------------------------------------------------------------

@router.post("/inquiry")
async def inquiry(
    item_id: str = Form(""),
    name: str = Form(""),
    contact: str = Form(""),
    message: str = Form(""),
    offer: str = Form(""),
    website: str = Form(""),  # honeypot
):
    """Public endpoint. Records an inquiry and increments the item's counter.

    No authentication by design — it backs the website contact form. It stores
    only what the buyer typed and never reveals inventory or address details.
    """
    if website.strip():
        return JSONResponse({"status": "ok"})  # bot trap; silently discard

    try:
        offer_amount = float(str(offer).replace("$", "").replace(",", "")) if offer.strip() else None
    except ValueError:
        offer_amount = None

    session = get_session()
    try:
        InquiryRepository(session).add(
            item_id=item_id.strip()[:40],
            channel="website",
            buyer_name=name.strip()[:120],
            buyer_contact=contact.strip()[:200],
            message=message.strip()[:4000],
            offer_amount=offer_amount,
        )
        logger.info({"action": "estate_inquiry_received", "item_id": item_id[:40]})
        return JSONResponse(
            {"status": "ok",
             "message": "Thank you — we will reply from our selling address shortly."}
        )
    except Exception as exc:
        logger.error({"action": "estate_inquiry_failed", "error_type": type(exc).__name__})
        return JSONResponse({"status": "error"}, status_code=500)
    finally:
        session.close()


@router.get("/markdown/preview")
def markdown_preview(request: Request):
    """What the markdown engine would do today, for every listed item."""
    if not _authorised(request):
        return _deny()
    session = get_session()
    try:
        settings = get_settings()
        out = []
        for item in ItemRepository(session).all():
            if item.status != ItemStatus.LISTED.value:
                continue
            decision = pricing.evaluate_markdown(
                item, listed_on=item.listed_on or item.research_date,
                move_out_date=settings.estate_move_out_date,
            )
            out.append({
                "item_id": item.item_id,
                "current_price": item.current_price,
                "floor_price": item.floor_price,
                "should_mark_down": decision.should_mark_down,
                "new_price": decision.new_price,
                "step_pct": decision.step_pct,
                "reasons": decision.reasons,
            })
        return JSONResponse({"items": out})
    finally:
        session.close()
