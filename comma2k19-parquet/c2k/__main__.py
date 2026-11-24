"""CLI: python -m c2k {convert,events,report,all} ..."""
from __future__ import annotations

import argparse
from pathlib import Path

from . import convert as convert_mod
from . import coverage, events


def main() -> int:
    ap = argparse.ArgumentParser(prog="c2k", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="NumPy segments -> partitioned Parquet")
    c.add_argument("--src", required=True, type=Path)
    c.add_argument("--out", required=True, type=Path, help="Parquet root")
    c.add_argument("--workers", type=int, default=1)
    c.add_argument("--force", action="store_true")
    c.add_argument("--max-segments", type=int, default=None)

    for name, helptext in (("events", "tag events with SQL"),
                           ("report", "coverage and gap report (needs events)"),
                           ("all", "events + report")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--parquet", required=True, type=Path)
        p.add_argument("--out", required=True, type=Path, help="results directory")
        if name != "report":
            p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                           help="override an event threshold (see c2k/events.py DEFAULTS)")

    a = ap.parse_args()
    overrides = {}
    for item in getattr(a, "param", []):
        key, sep, val = item.partition("=")
        if not sep:
            ap.error(f"--param expects KEY=VALUE, got {item!r}")
        overrides[key] = val
    if a.cmd == "convert":
        s = convert_mod.convert(a.src, a.out, a.workers, a.force, a.max_segments)
        print(f"found={s['found']} converted={s['converted']} skipped={s['skipped']} errors={len(s['errors'])}")
        return 1 if s["errors"] or s["found"] == 0 else 0
    if a.cmd in ("events", "all"):
        n = events.run(a.parquet, a.out, overrides)
        print(f"events: {n} rows -> {a.out}/events.parquet")
    if a.cmd in ("report", "all"):
        coverage.run(a.parquet, a.out)
        print(f"report -> {a.out}/REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
