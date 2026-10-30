#!/usr/bin/env python3
"""Store an existing calibration JSON report; never changes source observations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from critical_consensus.persistence import PostgresRepository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read calibration report: {type(exc).__name__}")
    if not isinstance(report, dict):
        parser.error("calibration report must be a JSON object")
    repo = PostgresRepository()
    repo.migrate()
    stored = repo.store_calibration_report(report)
    print(json.dumps(stored, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
