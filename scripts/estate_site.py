#!/usr/bin/env python3
"""Rebuild the catalogue website from approved inventory.

    python scripts/estate_site.py                 # build into estate/site
    python scripts/estate_site.py --out /var/www/catalog
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="estate/site")
    parser.add_argument(
        "--api-base", default="",
        help="deprecated, unused. Inquiries now POST to a relative api/inquiry "
             "path served by the standalone function this build emits -- see "
             "estate/serverless.py.",
    )
    args = parser.parse_args()

    from estate._compat import get_settings
    from estate import site
    from estate._compat import get_session, init_db

    init_db()
    session = get_session()
    settings = get_settings()

    report = site.build_site(
        session, out_dir=args.out, api_base=args.api_base,
        region=settings.estate_pickup_region,
        catalog_url=settings.estate_catalog_url,
    )
    print("Built %d item(s) in %d categor(y/ies), %d photo(s) -> %s"
          % (report["items"], report.get("categories", 0), report["photos"],
             report["output"]))
    if report.get("preview"):
        print("  PREVIEW BUILD -- not for publication.")
    for w in report["warnings"]:
        print("  WARNING: %s" % w)

    # An item a human already approved that is not on the site is the most
    # confusing state possible, so name it and say exactly which gate is
    # holding it rather than leaving someone to count rows.
    held = report.get("held_back") or []
    if held:
        print("\n%d approved item(s) held back from the catalogue:" % len(held))
        for item_id, blockers in held:
            print("  %s" % item_id)
            for blocker in blockers:
                print("      - %s" % blocker)

    session.close()
    return 1 if report["items"] == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
