"""End-to-end demonstration with three clearly-labelled sample items.

Everything this module produces is fake and says so:

- Item names are prefixed ``[SAMPLE]``.
- Photographs are generated placeholder graphics, not real objects.
- Comparables are written with ``is_placeholder=True`` and ``example.invalid``
  URLs that cannot resolve to a real listing.

The demo deliberately shows the approval guard REFUSING to approve an item
priced from placeholder evidence — that refusal is the feature, not a bug. It
then bypasses the guard explicitly (recording a ``demo_approval_bypass`` event)
so the downstream stages — listing packages, catalogue site, markdown, sale —
can be exercised too. The bypass exists only here and is never reachable from
Telegram or the web interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from estate import (
    approval,
    exporter,
    listing,
    marketplaces,
    paths,
    pricing,
    research,
    site,
)
from estate.repository import (
    CompRepository,
    InquiryRepository,
    ItemRepository,
)
from estate.schema import ItemStatus
from estate.settings import move_out_date as move_out_date_default
from estate.vision import MockVisionProvider

SAMPLE_PREFIX = "[SAMPLE]"


@dataclass
class Sample:
    key: str
    photos: int
    vision: dict
    answers: dict
    comps: list = field(default_factory=list)
    logistics: dict = field(default_factory=dict)


SAMPLES = [
    Sample(
        key="sample-a-chair",
        photos=5,
        vision={
            "item_name": f"{SAMPLE_PREFIX} Mesh ergonomic office chair",
            "category": "Furniture",
            "brand": "Placeholder Seating Co.",
            "model": "",
            "approximate_age": "roughly 10 years",
            "description": "SAMPLE DATA. Black mesh task chair with adjustable arms, "
                           "tilt lock, and a five-star base on castors.",
            "condition": "Good",
            "condition_observations": "Mesh intact; visible scuffing on one armrest.",
            "defects": "Scuff on left armrest",
            "dimensions": "27 x 27 x 41 in",
            "included_accessories": "",
            "identifying_details": "Maker's label under the seat pan",
            "confidence": {"item_name": 0.88, "category": 0.94, "brand": 0.71,
                           "model": 0.22, "condition": 0.79, "dimensions": 0.65},
            "overall_confidence": 0.72,
            "suggested_photos": ["label under the seat", "gas cylinder at full extension"],
            "suggested_questions": ["Does the height adjustment still hold?"],
        },
        answers={"model": "Task 2200", "defects": "Scuff on left armrest; one castor squeaks",
                 "included_accessories": "none", "location_in_house": "Study"},
        logistics={"weight_lbs": 44, "people_required": 2, "required_vehicle": "SUV or truck",
                   "pickup_difficulty": "Moderate", "shipping_feasible": False,
                   "pickup_required": True},
        comps=[
            dict(platform="PLACEHOLDER-eBay", title="[SAMPLE] similar mesh task chair, sold",
                 url="https://example.invalid/sample/chair-1", is_sold=True, price=210.0,
                 shipping_amount=0.0, condition="Good", observed_date="2026-07-12",
                 relevance=0.8, is_placeholder=True, location="PLACEHOLDER",
                 similarities="Same style and adjustment set",
                 differences="Unknown whether arms are identical"),
            dict(platform="PLACEHOLDER-Marketplace", title="[SAMPLE] mesh task chair, local",
                 url="https://example.invalid/sample/chair-2", is_sold=True, price=175.0,
                 condition="Good", observed_date="2026-06-28", relevance=0.7,
                 is_placeholder=True, location="PLACEHOLDER",
                 similarities="Comparable age and wear", differences="No arms"),
            dict(platform="PLACEHOLDER-Marketplace", title="[SAMPLE] mesh chair, asking",
                 url="https://example.invalid/sample/chair-3", is_sold=False, price=260.0,
                 condition="Excellent", observed_date="2026-07-25", relevance=0.6,
                 is_placeholder=True, location="PLACEHOLDER",
                 similarities="Same category", differences="Better condition, active listing"),
        ],
    ),
    Sample(
        key="sample-b-guitar",
        photos=6,
        vision={
            "item_name": f"{SAMPLE_PREFIX} Dreadnought acoustic guitar",
            "category": "Audio / Music Gear",
            "brand": "Placeholder Guitars",
            "model": "",
            "approximate_age": "1990s",
            "description": "SAMPLE DATA. Dreadnought-body acoustic guitar with a natural "
                           "spruce top, rosewood fingerboard, and chrome tuners.",
            "condition": "Excellent",
            "condition_observations": "No visible cracks; light pick wear below the soundhole.",
            "defects": "",
            "dimensions": "41 x 16 x 5 in",
            "included_accessories": "Hard case visible in photo",
            "identifying_details": "Headstock logo and interior label",
            "confidence": {"item_name": 0.91, "category": 0.96, "brand": 0.64,
                           "model": 0.18, "condition": 0.82, "dimensions": 0.55},
            "overall_confidence": 0.74,
            "suggested_photos": ["interior label through the soundhole", "neck joint"],
            "suggested_questions": ["Has the neck ever been reset?"],
        },
        answers={"model": "D-Series 400", "defects": "none",
                 "included_accessories": "Hard case, spare strings",
                 "dimensions": "41 x 16 x 5 in", "location_in_house": "Living room"},
        logistics={"weight_lbs": 14, "people_required": 1, "required_vehicle": "Car",
                   "pickup_difficulty": "Easy", "shipping_feasible": True,
                   "pickup_required": False},
        comps=[
            dict(platform="PLACEHOLDER-Reverb", title="[SAMPLE] dreadnought acoustic, sold",
                 url="https://example.invalid/sample/guitar-1", is_sold=True, price=520.0,
                 shipping_amount=45.0, condition="Excellent", observed_date="2026-07-18",
                 relevance=0.85, is_placeholder=True,
                 similarities="Same body shape and era", differences="Different finish"),
            dict(platform="PLACEHOLDER-Reverb", title="[SAMPLE] dreadnought, sold with case",
                 url="https://example.invalid/sample/guitar-2", is_sold=True, price=610.0,
                 shipping_amount=0.0, condition="Excellent", observed_date="2026-06-30",
                 relevance=0.8, is_placeholder=True,
                 similarities="Includes hard case", differences="Slightly newer"),
            dict(platform="PLACEHOLDER-eBay", title="[SAMPLE] acoustic guitar, sold",
                 url="https://example.invalid/sample/guitar-3", is_sold=True, price=445.0,
                 shipping_amount=55.0, condition="Good", observed_date="2026-07-02",
                 relevance=0.65, is_placeholder=True,
                 similarities="Same category", differences="Poorer condition"),
            dict(platform="PLACEHOLDER-Marketplace", title="[SAMPLE] acoustic, asking",
                 url="https://example.invalid/sample/guitar-4", is_sold=False, price=700.0,
                 condition="Like New", observed_date="2026-07-28", relevance=0.55,
                 is_placeholder=True,
                 similarities="Same body shape", differences="Active listing, better condition"),
        ],
    ),
    Sample(
        key="sample-c-coat",
        photos=4,
        vision={
            "item_name": f"{SAMPLE_PREFIX} Wool overcoat",
            "category": "Clothing & Accessories",
            "brand": "Placeholder Outerwear",
            "model": "",
            "approximate_age": "unknown",
            "description": "SAMPLE DATA. Mid-length wool overcoat in charcoal with notch "
                           "lapels and a half-belt at the back.",
            "condition": "Like New",
            "condition_observations": "No pilling or moth damage visible.",
            "defects": "",
            "dimensions": "",
            "included_accessories": "",
            "identifying_details": "Interior brand and size label",
            "confidence": {"item_name": 0.86, "category": 0.93, "brand": 0.48,
                           "model": 0.10, "condition": 0.77, "dimensions": 0.05},
            "overall_confidence": 0.60,
            "suggested_photos": ["interior size label", "lining", "cuffs and collar"],
            "suggested_questions": ["What size is on the label?", "Any moth damage?"],
        },
        answers={"brand": "Placeholder Outerwear", "model": "skip",
                 "defects": "none", "included_accessories": "none",
                 "dimensions": "Size 42R, 44 in length", "approximate_age": "skip",
                 "location_in_house": "Hall closet"},
        logistics={"weight_lbs": 4, "people_required": 1, "required_vehicle": "Car",
                   "pickup_difficulty": "Easy", "shipping_feasible": True,
                   "pickup_required": False},
        comps=[
            dict(platform="PLACEHOLDER-eBay", title="[SAMPLE] charcoal wool overcoat, sold",
                 url="https://example.invalid/sample/coat-1", is_sold=True, price=95.0,
                 shipping_amount=12.0, condition="Like New", observed_date="2026-07-20",
                 relevance=0.7, is_placeholder=True,
                 similarities="Same colour and length", differences="Different maker"),
            dict(platform="PLACEHOLDER-Poshmark", title="[SAMPLE] wool coat, asking",
                 url="https://example.invalid/sample/coat-2", is_sold=False, price=140.0,
                 condition="Excellent", observed_date="2026-07-26", relevance=0.5,
                 is_placeholder=True,
                 similarities="Similar silhouette", differences="Active listing"),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Placeholder photographs
# ---------------------------------------------------------------------------

def _placeholder_image(text: str, index: int, size=(1200, 900)) -> bytes:
    """A clearly-marked sample graphic. Never a real photograph."""
    import io

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return ("SAMPLE-PLACEHOLDER-%s-%d" % (text, index)).encode()

    shades = [(232, 228, 220), (216, 211, 200), (198, 194, 184), (240, 236, 228)]
    img = Image.new("RGB", size, shades[index % len(shades)])
    d = ImageDraw.Draw(img)
    d.rectangle([60, 60, size[0] - 60, size[1] - 60], outline=(120, 115, 105), width=4)
    d.text((90, 110), "SAMPLE PLACEHOLDER", fill=(150, 40, 40))
    d.text((90, 150), text, fill=(60, 58, 54))
    d.text((90, 190), "view %d - not a real photograph" % index, fill=(110, 106, 100))
    d.line([90, size[1] - 140, size[0] - 90, size[1] - 240], fill=(150, 146, 138), width=6)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class DemoRun:
    def __init__(self, session, move_out: str = "", catalog_url: str = "",
                 region: str = "the local area"):
        self.s = session
        self.items = ItemRepository(session)
        self.comps = CompRepository(session)
        self.move_out = move_out or move_out_date_default() or (
            date.today() + timedelta(days=45)
        ).isoformat()
        self.catalog_url = catalog_url
        self.region = region
        self.log: list = []

    def say(self, stage: str, detail: str = "") -> None:
        line = "  %-26s %s" % (stage, detail)
        self.log.append(line)
        print(line)

    def run(self) -> dict:
        from estate import pipeline

        results = {}
        provider = MockVisionProvider()
        provider.fixtures = {s.key: s.vision for s in SAMPLES}

        for sample in SAMPLES:
            print("\n" + "=" * 78)
            print(f"SAMPLE ITEM: {sample.key}")
            print("=" * 78)

            # 1. Telegram intake -------------------------------------------
            item = pipeline.start_item(
                self.s, owner="telegram:demo-dad", move_out_deadline=self.move_out
            )
            self.say("1. intake started", item.item_id)

            for n in range(1, sample.photos + 1):
                data = _placeholder_image(sample.key, n)
                _p, note = pipeline.attach_photo(
                    self.s, item.item_id, data, ext="jpg",
                    telegram_file_id="demo-%s-%d" % (sample.key, n),
                    media_group_id=f"demo-group-{sample.key}",
                )
            dupe = pipeline.attach_photo(self.s, item.item_id,
                                         _placeholder_image(sample.key, 1))[1]
            self.say("2. photos stored",
                     "%d photos; duplicate re-send handled as '%s'"
                     % (pipeline.photo_count(self.s, item.item_id), dupe))

            # 2. Identification --------------------------------------------
            photo_paths = [Path(p.local_path) for p in
                           pipeline.PhotoRepository(self.s).for_item(item.item_id)]
            ident = provider.identify(photo_paths, hint=sample.key)
            self.items.update(item.item_id, actor="vision:mock", **ident.to_item_fields())
            fresh = self.items.get(item.item_id)
            fresh.vision_raw = {"provider": "mock", "confidence": ident.confidence,
                                "overall_confidence": ident.overall_confidence}
            fresh.missing_fields = list(ident.missing)
            self.s.commit()
            self.say("3. identified", f"{ident.item_name} (confidence {ident.overall_confidence:.2f})")

            # 3. Missing-information questions ------------------------------
            asked = []
            guard = 0
            while guard < 20:
                guard += 1
                key, question = pipeline.next_question(self.s, item.item_id)
                if key is None:
                    break
                answer = sample.answers.get(key, "skip")
                pipeline.apply_answer(self.s, item.item_id, key, answer)
                asked.append(f"{key}={answer}")
            self.say("4. questions answered", ", ".join(asked) or "none needed")

            pipeline.finalise_draft(self.s, item.item_id, move_out_deadline=self.move_out)
            self.items.update(item.item_id, actor="demo", ownership_approval=True,
                              **sample.logistics)
            self.say("5. draft created",
                     f"status={self.items.get(item.item_id).status}; worksheet={research.worksheet_path(item.item_id).name}")

            # 4. Comparable research ---------------------------------------
            for c in sample.comps:
                self.comps.add(item.item_id, source="demo-placeholder", **c)
            summary = research.summarise(
                item.item_id, [research.Comparable(**{k: v for k, v in c.items()})
                               for c in sample.comps],
                self.items.get(item.item_id).condition or "",
                self.items.get(item.item_id).category or "",
            )
            self.say("6. comps recorded",
                     "n=%d (%d sold) low=%s median=%s high=%s confidence=%s"
                     % (summary.comp_count, summary.sold_count, summary.low,
                        summary.median, summary.high, summary.confidence))

            rec = pricing.recommend_price(summary)
            self.say("7. price recommended",
                     f"list={rec.initial_list_price} expected={rec.expected_sale_price} floor={rec.floor_price} | publishable={rec.publishable}")

            # 5. Human review — the guard must refuse -----------------------
            ok, message = approval.apply_decision(
                self.s, item.item_id, "approve", actor="demo:reviewer",
                catalog_url=self.catalog_url, region=self.region,
            )
            self.say("8. approval attempt",
                     "{} -- {}".format("ALLOWED" if ok else "CORRECTLY BLOCKED", message[:110]))
            assert not ok, "placeholder evidence must never pass the approval gate"

            # Explicit, audited bypass so the rest of the demo can run.
            self._demo_bypass(item.item_id, rec, summary)
            self.say("9. demo bypass",
                     "approved for DEMO ONLY; event 'demo_approval_bypass' recorded")

            # 6. Listing packages -------------------------------------------
            fresh = self.items.get(item.item_id)
            markets = marketplaces.recommend(fresh)
            incentive = pricing.compute_pickup_incentive(
                fresh, current_price=fresh.current_price,
                stairs=(fresh.pickup_difficulty in ("Hard", "Specialist Movers")),
                urgent=True,
            )
            packages = listing.build_all(
                fresh, markets, minimum_offer=fresh.floor_price,
                pickup_price=incentive.pickup_price, pickup_incentive=incentive.amount,
                catalog_url=self.catalog_url, region=self.region,
            )
            approval._write_listing_packages(item.item_id, packages)
            self.items.update(
                item.item_id, actor="demo",
                primary_marketplace=markets["primary"].platform.name if markets["primary"] else "",
                secondary_marketplaces=", ".join(f.platform.name for f in markets["secondary"]),
                pickup_incentive=incentive.amount,
                approved_pickup_price=incentive.pickup_price,
            )
            self.say("10. listing packages",
                     "{} -> {}".format(", ".join(packages),
                                   "%d file(s) in copy/" % (len(packages) + 1)))
            self.say("    pickup incentive",
                     "${} ({})".format(incentive.amount, "; ".join(incentive.factors) or "none"))

            # 7. Listed, inquiry, markdown ----------------------------------
            listed_on = (date.today() - timedelta(days=24)).isoformat()
            self.items.set_status(item.item_id, ItemStatus.LISTED.value, actor="demo")
            self.items.update(item.item_id, actor="demo", website_status="Queued",
                              listing_urls=[f"https://example.invalid/listing/{item.item_id}"],
                              listed_on=listed_on)

            InquiryRepository(self.s).add(
                item.item_id, channel="email", buyer_name="[SAMPLE] Buyer",
                buyer_contact="sample-buyer@example.invalid",
                message="Is this still available? Would you take less?",
                offer_amount=(rec.floor_price or 0) + 10,
            )
            fresh = self.items.get(item.item_id)
            self.say("11. inquiry routed",
                     "count=%d best offer=$%s -> selling inbox subject [%s]"
                     % (fresh.inquiry_count, fresh.best_offer, item.item_id))

            price_before = fresh.current_price
            floor = fresh.floor_price
            decision = pricing.evaluate_markdown(
                fresh, listed_on=listed_on, move_out_date=self.move_out
            )
            if decision.should_mark_down:
                self.items.update(item.item_id, actor="markdown_engine",
                                  current_price=decision.new_price,
                                  markdown_pct=decision.total_markdown_pct,
                                  next_markdown_date=decision.next_markdown_date)
            self.say("12. markdown evaluated",
                     f"${price_before} -> ${decision.new_price if decision.should_mark_down else price_before} (step {decision.step_pct * 100:.0f}%, floor ${floor})")
            self.say("    markdown reasoning", "; ".join(decision.reasons) or "standard cadence")
            assert decision.new_price is None or decision.new_price >= (floor or 0), \
                "markdown must never break the floor"

            results[item.item_id] = {
                "sample": sample.key,
                "price": self.items.get(item.item_id).current_price,
                "floor": self.items.get(item.item_id).floor_price,
                "confidence": summary.confidence,
                "primary": self.items.get(item.item_id).primary_marketplace,
            }

        # 8. Sold workflow on the first sample ------------------------------
        first_id = list(results)[0]
        first = self.items.get(first_id)
        sale_price = first.current_price
        fee = 0.0
        self.items.update(
            first_id, actor="demo", buyer="[SAMPLE] Buyer",
            fulfilment_status="Picked up", final_sale_price=sale_price,
            actual_proceeds=round(float(sale_price or 0) * (1 - fee), 2),
            final_disposition="Sold - local pickup", website_status="Sold (shown)",
        )
        self.items.set_status(first_id, ItemStatus.SOLD.value, actor="demo")
        print("")
        self.say("13. sold workflow",
                 f"{first_id} sold at ${sale_price}, proceeds recorded, catalogue marked Sold")

        return results

    def _demo_bypass(self, item_id: str, rec, summary) -> None:
        """Approve for the demo only, loudly and in the audit trail."""
        updates = dict(summary.as_item_fields())
        updates.update(rec.as_item_fields())
        updates.update(
            approval_status="Approved", review_status="Reviewed", website_status="Queued"
        )
        self.items.update(item_id, actor="demo:bypass",
                          **{k: v for k, v in updates.items() if v is not None})
        self.items.set_status(item_id, ItemStatus.APPROVED.value, actor="demo:bypass",
                              reason="DEMO ONLY - placeholder evidence")
        warning = ("Approved from PLACEHOLDER evidence for demonstration only. "
                   "Never reachable from Telegram or the review interface.")
        self.items.events.record(item_id, "demo_approval_bypass", actor="demo:bypass",
                                 warning=warning)
        item = self.items.get(item_id)
        approval._write_approval_record(item_id, {
            "item_id": item_id,
            "approved_by": "demo:bypass",
            "approved_at": date.today().isoformat(),
            "WARNING": warning,
            "prices": {
                "initial_list_price": item.initial_list_price,
                "expected_sale_price": item.expected_sale_price,
                "floor_price": item.floor_price,
                "current_price": item.current_price,
            },
            "evidence": {
                "comp_count": summary.comp_count,
                "sold_count": summary.sold_count,
                "confidence": summary.confidence,
                "placeholder_count": summary.placeholder_count,
                "sources": summary.sources,
            },
        })


def run_demo(session, out_dir: str = "estate/demo-output", move_out: str = "",
             catalog_url: str = "", region: str = "the local area") -> dict:
    run = DemoRun(session, move_out=move_out, catalog_url=catalog_url, region=region)
    results = run.run()

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    print("\n" + "=" * 78)
    print("ARTEFACTS")
    print("=" * 78)

    csv_path = exporter.export_csv(session, out / "inventory.csv")
    comps_path = exporter.export_comps_csv(session, out / "comparables.csv")
    print(f"  inventory csv              {csv_path}")
    print(f"  comparables csv            {comps_path}")
    try:
        xlsx_path = exporter.export_xlsx(session, out / "inventory.xlsx")
        print(f"  inventory workbook         {xlsx_path}")
    except ImportError:
        print("  inventory workbook         SKIPPED (openpyxl not installed)")

    report = site.build_site(
        session, out_dir=out / "site", region=region, api_base="http://127.0.0.1:8000",
        catalog_url=catalog_url, include_mock=True,
    )
    print("  catalogue site             %s (%d items, %d photos)"
          % (report["output"], report["items"], report["photos"]))
    for w in report["warnings"]:
        print(f"    WARNING: {w}")
    print(f"  per-item directories       {paths.inventory_root()}/<ITEM_ID>/")
    return {"items": results, "site": report, "csv": str(csv_path)}
