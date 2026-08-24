#!/usr/bin/env python3
"""Export the inventory for Google Sheets or Excel.

    python scripts/estate_export.py --out exports/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="exports")
    args = parser.parse_args()

    from estate import exporter
    from estate._compat import get_session, init_db

    init_db()
    session = get_session()
    out = Path(args.out)

    print("inventory csv    %s" % exporter.export_csv(session, out / "inventory.csv"))
    print("comparables csv  %s" % exporter.export_comps_csv(session, out / "comparables.csv"))
    try:
        print("workbook         %s" % exporter.export_xlsx(session, out / "inventory.xlsx"))
    except ImportError:
        print("workbook         SKIPPED — pip install openpyxl to enable")
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
