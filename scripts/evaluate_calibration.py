#!/usr/bin/env python3
"""Generate a JSON walk-forward forecast-calibration report from a timestamped CSV."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from critical_consensus.calibration_eval import evaluate_csv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="Forecast CSV; existing file is read only")
    parser.add_argument("--output", type=Path, default=Path("calibration-report.json"))
    parser.add_argument("--min-train-samples", type=int, default=30)
    parser.add_argument("--min-class-samples", type=int, default=5)
    parser.add_argument("--refit-interval", type=int, default=25)
    args = parser.parse_args()
    report = evaluate_csv(args.csv_path, min_train_samples=args.min_train_samples,
                          min_class_samples=args.min_class_samples, refit_interval=args.refit_interval)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "dataset_sha256": report["dataset"]["sha256"],
        "overall_status": report["overall_status"], "models": {
            name: {
                "rows": data["total_resolved_forecasts"],
                "calibrated_oos": data["walk_forward"]["calibrated_oos_count"],
                "calibration_comparison": data["walk_forward"]["calibration_comparison_status"],
            } for name, data in report["models"].items()
        },
        "provenance_status": report["provenance_status"],
    }, indent=2))
    # NOT_PROVEN is a valid audit result and must not be converted into a false PASS.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
