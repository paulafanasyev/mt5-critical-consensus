"""Chronological out-of-sample evaluation for timestamped binary forecasts.

This module evaluates probability forecasts, not trading P&L. Calibration is fit
only from forecasts whose outcomes were resolved before each test block began.
It cannot prove that input forecasts were actually recorded before their outcomes;
that provenance must be enforced by the capture pipeline.
"""
from __future__ import annotations

import csv
import hashlib
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROTOCOL_VERSION = "walk-forward-platt-v1"


@dataclass(frozen=True)
class Forecast:
    model_id: str
    signal_id: str
    issued_at: datetime
    resolved_at: datetime
    probability: float
    outcome: int


def _timestamp(raw: str, field: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601 with timezone") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(timezone.utc)


def parse_forecast_row(row: Mapping[str, Any], line_number: int = 0) -> Forecast:
    model_id, signal_id = str(row.get("model_id", "")).strip(), str(row.get("signal_id", "")).strip()
    if not model_id or not signal_id:
        raise ValueError(f"row {line_number}: model_id and signal_id are required")
    issued = _timestamp(row.get("issued_at", ""), "issued_at")
    resolved = _timestamp(row.get("resolved_at", ""), "resolved_at")
    if resolved <= issued:
        raise ValueError(f"row {line_number}: resolved_at must be later than issued_at")
    raw_probability = row.get("probability")
    if isinstance(raw_probability, bool):
        raise ValueError(f"row {line_number}: probability must be numeric")
    try:
        probability = float(raw_probability)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"row {line_number}: probability must be numeric") from exc
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError(f"row {line_number}: probability must be finite and in [0,1]")
    raw_outcome = row.get("outcome")
    if isinstance(raw_outcome, bool):
        outcome = int(raw_outcome)
    else:
        try:
            numeric_outcome = float(raw_outcome)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"row {line_number}: outcome must be 0 or 1") from exc
        if not math.isfinite(numeric_outcome) or numeric_outcome not in (0.0, 1.0):
            raise ValueError(f"row {line_number}: outcome must be 0 or 1")
        outcome = int(numeric_outcome)
    return Forecast(model_id, signal_id, issued, resolved, probability, outcome)


def load_forecasts_csv(path: str | Path) -> list[Forecast]:
    rows: list[Forecast] = []
    seen: set[tuple[str, str]] = set()
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"model_id", "signal_id", "issued_at", "resolved_at", "probability", "outcome"}
        headers = {str(name).strip() for name in (reader.fieldnames or [])}
        missing = sorted(required - headers)
        if missing:
            raise ValueError("forecast CSV missing columns: " + ", ".join(missing))
        for line, row in enumerate(reader, start=2):
            forecast = parse_forecast_row(row, line)
            key = (forecast.model_id, forecast.signal_id)
            if key in seen:
                raise ValueError(f"row {line}: duplicate model_id/signal_id forecast")
            seen.add(key)
            rows.append(forecast)
    if not rows:
        raise ValueError("forecast CSV contains no observations")
    return sorted(rows, key=lambda item: (item.issued_at, item.model_id, item.signal_id))


def _clip(p: float) -> float:
    return min(1.0 - 1e-12, max(1e-12, p))


def _logit(p: float) -> float:
    value = _clip(p)
    return math.log(value / (1.0 - value))


def _sigmoid(x: float) -> float:
    if x >= 0:
        e = math.exp(-min(x, 700.0))
        return 1.0 / (1.0 + e)
    e = math.exp(max(x, -700.0))
    return e / (1.0 + e)


def _fit_platt(rows: Sequence[Forecast], *, min_class_samples: int = 5) -> tuple[float, float] | None:
    if len(rows) < 2 * min_class_samples:
        return None
    positives = sum(row.outcome for row in rows)
    negatives = len(rows) - positives
    if positives < min_class_samples or negatives < min_class_samples:
        return None
    xs = [_logit(row.probability) for row in rows]
    ys = [row.outcome for row in rows]
    intercept, slope = 0.0, 1.0
    # Regularise slope toward identity to limit overfitting on a small calibration set.
    regularisation = 1e-3
    for _ in range(80):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in zip(xs, ys):
            q = _sigmoid(intercept + slope * x)
            w = max(1e-9, q * (1.0 - q))
            error = q - y
            g0 += error
            g1 += error * x
            h00 += w
            h01 += w * x
            h11 += w * x * x
        g1 += regularisation * (slope - 1.0)
        h00 += 1e-9
        h11 += regularisation + 1e-9
        determinant = h00 * h11 - h01 * h01
        if not math.isfinite(determinant) or determinant <= 1e-12:
            return None
        step0 = (h11 * g0 - h01 * g1) / determinant
        step1 = (h00 * g1 - h01 * g0) / determinant
        next_intercept = max(-20.0, min(20.0, intercept - step0))
        next_slope = max(-10.0, min(10.0, slope - step1))
        if abs(next_intercept - intercept) + abs(next_slope - slope) < 1e-7:
            intercept, slope = next_intercept, next_slope
            break
        intercept, slope = next_intercept, next_slope
    if not math.isfinite(intercept) or not math.isfinite(slope):
        return None
    return intercept, slope


def _wilson(successes: int, total: int, *, z: float = 1.959963984540054) -> dict[str, float] | None:
    if total <= 0:
        return None
    p = successes / total
    denom = 1.0 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4 * total)) / total) / denom
    return {"lower_95": max(0.0, center - margin), "upper_95": min(1.0, center + margin)}


def _reliability(probabilities: Sequence[float], outcomes: Sequence[int], *, bins: int = 10) -> dict[str, Any]:
    if len(probabilities) != len(outcomes) or not probabilities:
        return {"ece": None, "bins": []}
    result = []
    weighted_error = 0.0
    n = len(probabilities)
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        indices = [i for i, p in enumerate(probabilities)
                   if low <= p < high or (index == bins - 1 and p == 1.0)]
        if not indices:
            continue
        mean_p = sum(probabilities[i] for i in indices) / len(indices)
        rate = sum(outcomes[i] for i in indices) / len(indices)
        interval = _wilson(sum(outcomes[i] for i in indices), len(indices))
        result.append({"lower_probability": low, "upper_probability": high, "n": len(indices),
                       "mean_probability": mean_p, "observed_rate": rate, "observed_rate_ci95": interval})
        weighted_error += len(indices) / n * abs(mean_p - rate)
    return {"ece": weighted_error, "bins": result}


def _metrics(rows: Sequence[tuple[float, int]]) -> dict[str, Any]:
    if not rows:
        return {"sample_size": 0, "brier_score": None, "log_loss": None,
                "accuracy_at_0_5": None, "ece_10_bins": None, "reliability_bins": []}
    probabilities, outcomes = [x[0] for x in rows], [x[1] for x in rows]
    reliability = _reliability(probabilities, outcomes)
    high = [(p, y) for p, y in rows if p >= 0.95]
    return {
        "sample_size": len(rows),
        "brier_score": sum((p - y) ** 2 for p, y in rows) / len(rows),
        "log_loss": -sum(y * math.log(_clip(p)) + (1-y) * math.log(1-_clip(p)) for p, y in rows) / len(rows),
        "accuracy_at_0_5": sum((p >= 0.5) == bool(y) for p, y in rows) / len(rows),
        "ece_10_bins": reliability["ece"], "reliability_bins": reliability["bins"],
        "high_confidence_threshold": 0.95,
        "high_confidence_sample_size": len(high),
        "high_confidence_false_certainty_rate": (sum(1 for p, y in high if y == 0) / len(high)) if high else None,
        "high_confidence_false_rate_ci95": _wilson(sum(1 for p, y in high if y == 0), len(high)),
        "high_confidence_rate_status": "NOT_PROVEN" if len(high) < 30 else "SAMPLE_MEETS_MINIMUM",
    }


def _bootstrap_mean_ci(values: Sequence[float], *, iterations: int = 2000, seed: int = 20261030) -> dict[str, Any]:
    if len(values) < 2:
        return {"lower_95": None, "upper_95": None, "bootstrap_samples": 0}
    rng = random.Random(seed)
    n = len(values)
    samples = []
    for _ in range(iterations):
        samples.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    samples.sort()
    return {"lower_95": samples[int(0.025 * (iterations - 1))],
            "upper_95": samples[int(0.975 * (iterations - 1))],
            "bootstrap_samples": iterations, "seed": seed}


def _validate_forecasts(rows: Sequence[Forecast]) -> None:
    """Reject malformed in-memory rows just as strictly as CSV-loaded forecasts."""
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Forecast):
            raise ValueError(f"forecast {index}: expected Forecast instance")
        if (not isinstance(row.model_id, str) or not row.model_id.strip()
                or not isinstance(row.signal_id, str) or not row.signal_id.strip()):
            raise ValueError(f"forecast {index}: model_id and signal_id are required")
        key = (row.model_id, row.signal_id)
        if key in seen:
            raise ValueError(f"forecast {index}: duplicate model_id/signal_id forecast")
        seen.add(key)
        for field, stamp in (("issued_at", row.issued_at), ("resolved_at", row.resolved_at)):
            if (not isinstance(stamp, datetime) or stamp.tzinfo is None
                    or stamp.utcoffset() is None):
                raise ValueError(f"forecast {index}: {field} must include a timezone")
        if row.resolved_at <= row.issued_at:
            raise ValueError(f"forecast {index}: resolved_at must be later than issued_at")
        if isinstance(row.probability, bool):
            raise ValueError(f"forecast {index}: probability must be numeric")
        try:
            probability = float(row.probability)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"forecast {index}: probability must be numeric") from exc
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError(f"forecast {index}: probability must be finite and in [0,1]")
        if isinstance(row.outcome, bool) or not isinstance(row.outcome, int) or row.outcome not in (0, 1):
            raise ValueError(f"forecast {index}: outcome must be integer 0 or 1")


def evaluate_walk_forward(
    forecasts: Iterable[Forecast], *, min_train_samples: int = 30,
    min_class_samples: int = 5, refit_interval: int = 25,
) -> dict[str, Any]:
    """Evaluate each model chronologically; fit each calibration block on matured past data only."""
    rows = list(forecasts)
    if not rows:
        raise ValueError("at least one forecast is required")
    _validate_forecasts(rows)
    if min_train_samples < 2 * min_class_samples or min_class_samples < 1 or refit_interval < 1:
        raise ValueError("invalid minimum sample size or refit interval")
    rows.sort(key=lambda item: (item.issued_at, item.model_id, item.signal_id))
    report: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provenance_status": "NOT_VERIFIED_BY_SCRIPT",
        "important_limit": "Only use forecasts captured before outcomes were known; this report does not prove capture-time provenance and does not measure trading profitability.",
        "settings": {"min_train_samples": min_train_samples, "min_class_samples": min_class_samples,
                     "refit_interval": refit_interval, "high_confidence_threshold": 0.95,
                     "reliability_bins": 10},
        "total_forecasts": len(rows), "models": {},
    }
    models = sorted({row.model_id for row in rows})
    for model_id in models:
        model_rows = [row for row in rows if row.model_id == model_id]
        model_rows.sort(key=lambda item: (item.issued_at, item.signal_id))
        raw_all = [(row.probability, row.outcome) for row in model_rows]
        calibrated_rows: list[tuple[float, int, float]] = []
        trained_blocks = 0
        for offset in range(0, len(model_rows), refit_interval):
            block = model_rows[offset: offset + refit_interval]
            block_start = block[0].issued_at
            training = [row for row in model_rows
                        if row.issued_at < block_start and row.resolved_at < block_start]
            if len(training) < min_train_samples:
                continue
            fitted = _fit_platt(training, min_class_samples=min_class_samples)
            if fitted is None:
                continue
            intercept, slope = fitted
            trained_blocks += 1
            for row in block:
                calibrated = _sigmoid(intercept + slope * _logit(row.probability))
                calibrated_rows.append((calibrated, row.outcome, row.probability))
        raw_same = [(raw, outcome) for _, outcome, raw in calibrated_rows]
        calibrated_only = [(calibrated, outcome) for calibrated, outcome, _ in calibrated_rows]
        deltas = [((raw - outcome) ** 2) - ((calibrated - outcome) ** 2)
                  for (raw, outcome), (calibrated, _) in zip(raw_same, calibrated_only)]
        interval = _bootstrap_mean_ci(deltas)
        delta_mean = (sum(deltas) / len(deltas)) if deltas else None
        ci_low, ci_high = interval["lower_95"], interval["upper_95"]
        if len(calibrated_rows) < 50 or ci_low is None:
            comparison_status = "NOT_PROVEN"
        elif ci_low > 0:
            comparison_status = "VERIFIED_IMPROVEMENT_IN_BRIER"
        elif ci_high < 0:
            comparison_status = "VERIFIED_DEGRADATION_IN_BRIER"
        else:
            comparison_status = "NOT_PROVEN"
        report["models"][model_id] = {
            "total_resolved_forecasts": len(model_rows),
            "raw_all_forecasts": _metrics(raw_all),
            "walk_forward": {
                "calibrated_oos_count": len(calibrated_rows),
                "calibration_coverage": len(calibrated_rows) / len(model_rows),
                "fitted_blocks": trained_blocks,
                "raw_metrics_on_same_oos_subset": _metrics(raw_same),
                "calibrated_metrics": _metrics(calibrated_only),
                "brier_improvement_raw_minus_calibrated": delta_mean,
                "brier_improvement_bootstrap_ci95": interval,
                "calibration_comparison_status": comparison_status,
                "sample_status": "PRELIMINARY_REVIEW_ONLY" if len(calibrated_rows) >= 50 else "NOT_PROVEN",
            },
        }
    report["overall_status"] = (
        "PRELIMINARY_REVIEW_ONLY"
        if any(model["walk_forward"]["calibrated_oos_count"] >= 50 for model in report["models"].values())
        else "NOT_PROVEN"
    )
    return report


def evaluate_csv(path: str | Path, *, min_train_samples: int = 30,
                 min_class_samples: int = 5, refit_interval: int = 25) -> dict[str, Any]:
    source = Path(path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = evaluate_walk_forward(load_forecasts_csv(source), min_train_samples=min_train_samples,
                                   min_class_samples=min_class_samples, refit_interval=refit_interval)
    result["dataset"] = {"filename": source.name, "sha256": "sha256:" + digest}
    return result
