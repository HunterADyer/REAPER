#!/usr/bin/env python3
"""Clean spurious :Function nodes + optionally ledger-claim address issues.

P0 from docs/skills/run-audit.md §3: the live cjson_001 run created 85 junk
:Function nodes because bare variable names (e.g. 'rdx_1') were MERGE'd as
Function {address: <bare>}. This script:

  1. DELETEs every :Function whose address is not a valid 0x[0-9a-f]+ hex
     (idempotent, DETACH).
  2. Reports the remaining valid count (should equal the real function count).
  3. Optionally --claims: prints ledger claim-addresses that are not in the
     graph (evidence recorded at instruction addresses instead of function
     start) for a human decision.

Usage:
    python3 scripts/clean_neo4j.py                   # delete spurious + report
    python3 scripts/clean_neo4j.py --claims          # also audit claim addrs
    python3 scripts/clean_neo4j.py --dry-run         # report only, no delete
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys

from neo4j import GraphDatabase

URI = "bolt://localhost:7687"
USER = "neo4j"
PASSWORD = "reaper"
LEDGER = "data/cjson_001_ledger.db"

_HEX_ADDR = re.compile(r"0x[0-9a-f]+")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--claims", action="store_true")
    p.add_argument("--uri", default=URI)
    args = p.parse_args()

    driver = GraphDatabase.driver(args.uri, auth=(USER, PASSWORD))
    try:
        with driver.session() as s:
            all_rows = s.run(
                "MATCH (f:Function) RETURN f.address AS a").data()
        valid = [r["a"] for r in all_rows if _HEX_ADDR.fullmatch(str(r["a"]))]
        spurious = [r["a"] for r in all_rows
                    if not _HEX_ADDR.fullmatch(str(r["a"]))]
        print(f"valid functions: {len(valid)}")
        print(f"spurious functions: {len(spurious)}")
        if args.dry_run:
            print("(--dry-run: no deletions)")
        elif spurious:
            with driver.session() as s:
                out = s.run(
                    "MATCH (f:Function) WHERE NOT f.address =~ '0x[0-9a-f]+' "
                    "DETACH DELETE f RETURN count(f) AS n").single()
                print(f"  deleted {out['n']} spurious node(s)")
            with driver.session() as s:
                after = s.run(
                    "MATCH (f:Function) RETURN count(f) AS n").single()
                print(f"  :Function count now: {after['n']}")

        if args.claims:
            con = sqlite3.connect(LEDGER)
            addrs = {r[0] for r in con.execute(
                "SELECT DISTINCT function_address FROM claims")}
            con.close()
            graph = {str(a) for a in valid}
            missing = sorted(a for a in addrs if a not in graph)
            print(f"\nledger claim-addresses NOT in graph: {len(missing)}")
            for a in missing[:40]:
                print(f"  {a}")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
