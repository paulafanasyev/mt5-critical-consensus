#!/usr/bin/env python3
"""Evaluate objective market-event forecasts read directly from the PostgreSQL ledger."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from critical_consensus.calibration_eval import evaluate_repository_forecasts
from critical_consensus.persistence import PostgresRepository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", help="optional exact provider/model identity filter")
    parser.add_argument("--output", type=Path, default=Path("calibration-report.json"))
    parser.add_argument("--min-train-samples", type=int, default=30)
    parser.add_argument("--min-class-samples", type=int, default=5)
    parser.add_argument("--refit-interval", type=int, default=25)
    args = parser.parse_args()

    repo = PostgresRepository()
    repo.migrate()
    report = evaluate_repository_forecasts(
        repo, model_id=args.model_id,
        min_train_samples=args.min_train_samples,
        min_class_samples=args.min_class_samples,
        refit_interval=args.refit_interval,
    )
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output),
        "dataset_sha256": report["dataset"]["sha256"],
        "overall_status": report["overall_status"],
        "provenance_status": report["provenance_status"],
        "models": {
            name: {
                "rows": data["total_resolved_forecasts"],
                "calibrated_oos": data["walk_forward"]["calibrated_oos_count"],
                "calibration_comparison": data["walk_forward"]["calibration_comparison_status"],
            }
            for name, data in report["models"].items()
        },
    }, indent=2))
    # NOT_PROVEN and PRELIMINARY_REVIEW_ONLY are valid audit outcomes, not CI failures.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
