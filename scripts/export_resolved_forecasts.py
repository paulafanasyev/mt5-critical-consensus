#!/usr/bin/env python3
"""Export resolved forecast records without modifying database rows."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from critical_consensus.persistence import PostgresRepository


FIELDS = ["model_id", "signal_id", "issued_at", "resolved_at", "probability", "outcome", "input_sha256", "target_policy_version", "target_event", "symbol", "timeframe", "reference_at", "reference_close", "resolved_bar_time", "resolved_bar_close", "resolution_source", "resolution_evidence_sha256", "resolution_evidence"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", help="optional exact model filter")
    parser.add_argument("--output", type=Path, default=Path("resolved-forecasts.csv"))
    args = parser.parse_args()
    rows = PostgresRepository().export_resolved_forecasts(model_id=args.model_id)
    if not rows:
        parser.error("no resolved forecasts found; no report was written")
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"exported_rows={len(rows)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
