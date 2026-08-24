#!/usr/bin/env python3
"""Run the full estate sale workflow against three labelled sample items.

    python scripts/estate_demo.py [--fresh] [--out estate/demo-output]

Uses the mock vision provider and placeholder comparables only. No API key,
no network call, and no real market data are involved.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="estate/demo-output")
    parser.add_argument("--fresh", action="store_true",
                        help="wipe the demo database and inventory before running")
    parser.add_argument("--catalog-url", default="https://example.invalid/catalog")
    parser.add_argument("--region", default="the local area")
    parser.add_argument("--db", default="",
                        help="SQLite path for the demo database. Use this when the "
                             "output directory is on a network or virtualised mount "
                             "where SQLite cannot take file locks.")
    args = parser.parse_args()

    out = Path(args.out)
    db_path = out / "demo.db"
    inv_path = out / "inventory"

    if args.fresh and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    if args.db:
        db_path = Path(args.db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.resolve()}"
    os.environ["ESTATE_INVENTORY_DIR"] = str(inv_path.resolve())
    os.environ.setdefault("ESTATE_VISION_PROVIDER", "mock")
    os.environ.setdefault("ESTATE_BRAND_NAME", "The Collection")

    from estate.demo import run_demo
    from estate._compat import get_session, init_db

    init_db()
    session = get_session()

    print("=" * 78)
    print("ESTATE SALE MVP — END-TO-END DEMONSTRATION")
    print("All data below is SAMPLE/PLACEHOLDER. No real market evidence is used.")
    print("=" * 78)

    result = run_demo(session, out_dir=str(out), catalog_url=args.catalog_url,
                      region=args.region)

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for item_id, data in result["items"].items():
        print("  %-16s %-18s price=$%-8s floor=$%-8s confidence=%-22s %s"
              % (item_id, data["sample"], data["price"], data["floor"],
                 data["confidence"], data["primary"]))
    print("\n  Open the catalogue: {}/index.html".format(result["site"]["output"]))
    print("  Every price above is derived from PLACEHOLDER comparables and must")
    print("  never be treated as a valuation.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
