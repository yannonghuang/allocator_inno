#!/usr/bin/env python3
"""Remove from bom.csv any row where (PARENT_ID, CHILD_ID) exists in code_conversion.csv as (PN_Source, PN_Target)."""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE_CONVERSION_CSV = ROOT / "csv" / "code_conversion.csv"
BOM_CSV = ROOT / "csv" / "bom.csv"

def main():
    # Build set of (PN_Source, PN_Target) from code_conversion.csv
    conversion_pairs = set()
    with open(CODE_CONVERSION_CSV, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            src = (row.get("PN_Source") or "").strip()
            tgt = (row.get("PN_Target") or "").strip()
            if src and tgt:
                conversion_pairs.add((src, tgt))

    # Read bom.csv and keep only rows not in that set
    kept = []
    removed = 0
    with open(BOM_CSV, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        fieldnames = r.fieldnames or []
        for row in r:
            parent = (row.get("PARENT_ID") or "").strip()
            child = (row.get("CHILD_ID") or "").strip()
            if (parent, child) in conversion_pairs:
                removed += 1
                continue
            kept.append(row)

    # Write back bom.csv
    with open(BOM_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(kept)

    print(f"Removed {removed} rows from bom.csv; {len(kept)} rows kept.")

if __name__ == "__main__":
    main()
