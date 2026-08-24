#!/usr/bin/env python3
"""The inventory command centre: what to work on next, and why.

    python scripts/estate_inventory.py                    # everything, ranked
    python scripts/estate_inventory.py --view quick_wins  # one named view
    python scripts/estate_inventory.py --views            # list the views
    python scripts/estate_inventory.py --explain DK-202608-002
    python scripts/estate_inventory.py --reprocess DK-202608-002
    python scripts/estate_inventory.py --reprioritise     # rescore everything

Read-only unless --reprocess or --reprioritise is passed. Neither of those
approves, prices for publication, or publishes anything: --reprocess runs the
same automated job the Telegram /done path runs, and stops at the review queue
exactly as it does there.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _money(v) -> str:
    return "     -" if not v else "%6.0f" % float(v)


def _row(item) -> str:
    return "{:>5.0f}  {:<16} {:<28} {:<22} {} {}".format(
        float(item.priority_score or 0),
        item.item_id,
        (item.item_name or "unnamed")[:28],
        (item.processing_stage or item.status or "")[:22],
        _money(item.expected_net_proceeds or item.expected_sale_price),
        (item.selling_difficulty or ""),
    )


def _header() -> str:
    return ("{:>5}  {:<16} {:<28} {:<22} {:>6} {}".format(
        "PRI", "ITEM ID", "NAME", "STAGE", "NET$", "DIFFICULTY"))


def _report_costs(session) -> int:
    """What identification has actually cost, per provider and per day.

    Estimates, not invoices — the per-call figures in vision._COST_PER_CALL_USD
    are rough by construction. The number that matters here is the call COUNT,
    which is exact, so an unexpected loop shows up as a count nobody expected
    rather than as a surprise at the end of the month.
    """
    from collections import defaultdict

    from estate.models import EstateEventORM

    rows = session.query(EstateEventORM).filter(
        EstateEventORM.event_type == "vision_identified"
    ).all()

    if not rows:
        print("No identification calls recorded yet.")
        return 0

    by_provider: dict = defaultdict(lambda: [0, 0.0])
    by_day: dict = defaultdict(lambda: [0, 0.0])
    for row in rows:
        provider = (row.detail or {}).get("provider", "unknown")
        cost = float((row.detail or {}).get("cost_usd", 0) or 0)
        day = row.created_at.date().isoformat() if row.created_at else "unknown"
        by_provider[provider][0] += 1
        by_provider[provider][1] += cost
        by_day[day][0] += 1
        by_day[day][1] += cost

    total_calls = sum(v[0] for v in by_provider.values())
    total_cost = sum(v[1] for v in by_provider.values())

    print("Identification calls: %d" % total_calls)
    print("Estimated spend:      $%.2f   (estimate, not an invoice)" % total_cost)
    print()
    print("By provider")
    for provider, (calls, cost) in sorted(by_provider.items()):
        print("  %-12s %4d call(s)   ~$%.2f" % (provider, calls, cost))
    print()
    print("Last 14 days")
    for day in sorted(by_day)[-14:]:
        calls, cost = by_day[day]
        print("  %-12s %4d call(s)   ~$%.2f" % (day, calls, cost))

    paid = sum(v[0] for k, v in by_provider.items() if k != "mock")
    if paid == 0:
        print()
        print("Every call so far was the free mock provider — nothing has been billed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--view", default="", help="a named view (see --views)")
    parser.add_argument("--views", action="store_true", help="list the available views")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--explain", default="", help="show one item's priority breakdown")
    parser.add_argument("--reprocess", default="",
                        help="re-run the automated job for one item ID")
    parser.add_argument("--reprioritise", action="store_true",
                        help="recompute every item's priority score")
    parser.add_argument("--costs", action="store_true",
                        help="what the vision provider has cost so far")
    args = parser.parse_args()

    from estate import orchestrator, priority
    from estate.repository import ItemRepository
    from estate._compat import get_session, init_db

    if args.views:
        for name, label in priority.VIEW_LABELS.items():
            print("  %-24s %s" % (name, label))
        return 0

    init_db()
    session = get_session()
    try:
        if args.costs:
            return _report_costs(session)

        if args.reprocess:
            job = orchestrator.process_item(session, args.reprocess)
            print("%s -> %s" % (args.reprocess, job.stage or "unchanged"))
            print(job.message)
            for w in job.warnings:
                print("  warning: " + w)
            for b in job.blockers:
                print("  blocker: " + b)
            return 0 if job.ok else 1

        if args.reprioritise:
            print("Rescored %d item(s)." % orchestrator.reprioritise_all(session))

        if args.explain:
            item = ItemRepository(session).get(args.explain)
            if item is None:
                print("No such item: %s" % args.explain)
                return 1
            print(item.priority_reasons or priority.score_item(item).explain())
            if item.approval_blockers:
                print("\nBlocking approval:")
                for b in item.approval_blockers.split("\n"):
                    if b.strip():
                        print("  - " + b)
            if item.research_blockers:
                print("\nEvidence gaps:")
                for b in item.research_blockers.split("\n"):
                    if b.strip():
                        print("  - " + b)
            return 0

        items = ItemRepository(session).all()
        if args.view:
            if args.view not in priority.VIEWS:
                print("Unknown view %r. Try --views." % args.view)
                return 1
            selected = priority.view(items, args.view, limit=args.limit)
            print(priority.VIEW_LABELS[args.view] + " (%d)" % len(selected))
        else:
            selected = priority.ranked(items, limit=args.limit)
            print("Open inventory, most worth your time first (%d shown)" % len(selected))

        print(_header())
        for item in selected:
            print(_row(item))
        if not selected:
            print("  (nothing matches)")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
