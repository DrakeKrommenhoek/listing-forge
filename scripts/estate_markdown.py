#!/usr/bin/env python3
"""Daily markdown evaluation for listed estate items.

    python scripts/estate_markdown.py            # dry run, prints what would change
    python scripts/estate_markdown.py --apply    # writes the new prices

Safe to automate: markdowns only ever move a price DOWN, and never below the
floor price a human approved. Anything that would break the floor is clamped
and reported instead.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the new prices")
    parser.add_argument("--item", default="", help="limit to one item ID")
    args = parser.parse_args()

    from estate._compat import get_settings
    from estate import pricing
    from estate.repository import ItemRepository
    from estate.schema import ItemStatus
    from estate._compat import get_session, init_db

    init_db()
    session = get_session()
    settings = get_settings()
    repo = ItemRepository(session)

    changed = 0
    held = 0
    at_floor = 0

    print("Estate markdown evaluation — %s%s"
          % (date.today().isoformat(), "" if args.apply else "  (DRY RUN)"))
    print("-" * 78)

    for item in repo.all():
        if args.item and item.item_id != args.item:
            continue
        if item.status not in (ItemStatus.LISTED.value, ItemStatus.OFFER_RECEIVED.value):
            continue
        if item.approval_status != "Approved":
            continue

        listed_on = item.listed_on or item.research_date
        decision = pricing.evaluate_markdown(
            item, listed_on=listed_on,
            move_out_date=item.move_out_deadline or settings.estate_move_out_date,
        )

        if decision.at_floor:
            at_floor += 1
        if not decision.should_mark_down:
            held += 1
            print("  HOLD   %-16s $%-8s  %s"
                  % (item.item_id, item.current_price,
                     decision.reasons[0] if decision.reasons else "no change due"))
            continue

        print("  LOWER  %-16s $%-8s -> $%-8s (step %.0f%%, floor $%s)"
              % (item.item_id, item.current_price, decision.new_price,
                 decision.step_pct * 100, item.floor_price))
        for reason in decision.reasons:
            print("           - %s" % reason)

        if args.apply:
            assert decision.new_price >= (item.floor_price or 0), "floor violated"
            repo.update(item.item_id, actor="markdown_engine",
                        current_price=decision.new_price,
                        markdown_pct=decision.total_markdown_pct,
                        next_markdown_date=decision.next_markdown_date)
            repo.events.record(item.item_id, "marked_down", actor="markdown_engine",
                               new_price=decision.new_price,
                               step_pct=decision.step_pct,
                               reasons=decision.reasons)
        changed += 1

    print("-" * 78)
    print("  %d lowered, %d held, %d already at floor%s"
          % (changed, held, at_floor, "" if args.apply else "  (nothing written)"))
    if changed and not args.apply:
        print("  Re-run with --apply to write these prices.")
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
