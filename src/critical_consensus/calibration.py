"""Calibration utilities. Shrinkage is a conservative heuristic, not a calibrator."""
from __future__ import annotations
import math
from typing import Iterable, Mapping, Sequence


def brier_score(probabilities: Sequence[float], outcomes: Sequence[int]) -> float:
    if len(probabilities) != len(outcomes) or not probabilities:
        raise ValueError("probabilities/outcomes must be non-empty and have equal length")
    converted_probabilities: list[float] = []
    for probability in probabilities:
        if isinstance(probability, bool):
            raise ValueError("probabilities must be numeric values, not booleans")
        try:
            value = float(probability)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("probabilities must be numeric values") from exc
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("probabilities must be finite and in [0, 1]")
        converted_probabilities.append(value)
    converted_outcomes: list[int] = []
    for outcome in outcomes:
        if isinstance(outcome, bool):
            raise ValueError("outcomes must be binary numeric values, not booleans")
        try:
            value = float(outcome)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("outcomes must be binary 0/1") from exc
        if not math.isfinite(value) or value not in (0.0, 1.0):
            raise ValueError("outcomes must be binary 0/1")
        converted_outcomes.append(int(value))
    return sum((p - y) ** 2 for p, y in zip(converted_probabilities, converted_outcomes)) / len(converted_probabilities)


def calibration_shrinkage(
    confidence: float,
    *,
    base_rate: float,
    brier: float,
    brier_threshold: float = 0.12,
    penalty_scale: float = 0.25,
    high_confidence_accuracy: float | None = None,
    high_confidence_threshold: float = 0.95,
    poor_accuracy_threshold: float = 0.70,
    overconfidence_multiplier: float = 0.50,
) -> float:
    """Penalize confidence when historical Brier/overconfidence is poor.

    Never raises the confidence. This heuristic must not replace an out-of-sample
    Platt/isotonic calibration model with reliability diagrams and adequate sample size.
    """
    numeric_values = (confidence, base_rate, brier, brier_threshold, penalty_scale,
                      high_confidence_threshold, poor_accuracy_threshold, overconfidence_multiplier)
    if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in numeric_values):
        raise ValueError("calibration inputs must be finite numeric values")
    if not (0 <= confidence <= 1 and 0 <= base_rate <= 1 and 0 <= brier <= 1):
        raise ValueError("confidence, base_rate and brier must be in [0,1]")
    if not (0 <= brier_threshold <= 1 and 0 <= high_confidence_threshold <= 1
            and 0 <= poor_accuracy_threshold <= 1 and 0 <= overconfidence_multiplier <= 1):
        raise ValueError("calibration thresholds/multipliers must be in [0,1]")
    if high_confidence_accuracy is not None and (isinstance(high_confidence_accuracy, bool)
            or not math.isfinite(float(high_confidence_accuracy)) or not 0 <= float(high_confidence_accuracy) <= 1):
        raise ValueError("high_confidence_accuracy must be null or in [0,1]")
    if penalty_scale <= 0:
        raise ValueError("penalty_scale must be positive")
    excess = max(0.0, brier - brier_threshold)
    factor = max(0.0, min(1.0, 1.0 - excess / penalty_scale))
    adjusted = base_rate + (confidence - base_rate) * factor
    if (confidence >= high_confidence_threshold and high_confidence_accuracy is not None
            and high_confidence_accuracy < poor_accuracy_threshold):
        adjusted = base_rate + (adjusted - base_rate) * overconfidence_multiplier
    return max(0.0, min(confidence, adjusted, 1.0))


def weighted_consensus(verdicts: Sequence[Mapping], *, base_rate: float = 0.5,
                       brier_threshold: float = 0.12) -> float:
    """Weighted mean of conservatively adjusted confidences; not a majority vote.

    Weights use 1-Brier as a simple bounded reliability weight. For production, use
    out-of-sample reliability bins and a documented weighting model.
    """
    if not verdicts:
        raise ValueError("at least one verdict is required")
    numerator = denominator = 0.0
    for item in verdicts:
        p = float(item.get("confidence", 0.0))
        score = float(item.get("calibration_meta", {}).get("brier_score", 1.0))
        accuracy = item.get("calibration_meta", {}).get("high_confidence_accuracy")
        adjusted = calibration_shrinkage(
            p, base_rate=base_rate, brier=score, brier_threshold=brier_threshold,
            high_confidence_accuracy=accuracy,
        )
        weight = max(0.01, 1.0 - score)
        numerator += adjusted * weight
        denominator += weight
    return numerator / denominator


def source_credibility(*, verified_accuracy: float, authenticity: float, independence: float,
                       freshness: float, transparency: float, lead_time: float,
                       fake_rate: float, bias_penalty: float) -> float:
    """Transparent heuristic score; every input is normalized to [0,1]."""
    vals = [verified_accuracy, authenticity, independence, freshness, transparency,
            lead_time, fake_rate, bias_penalty]
    if any(isinstance(x, bool) or not math.isfinite(float(x)) or not 0 <= x <= 1 for x in vals):
        raise ValueError("all credibility dimensions must be finite numeric values in [0,1]")
    base = (0.30 * verified_accuracy + 0.20 * authenticity + 0.15 * independence
            + 0.15 * freshness + 0.10 * transparency + 0.10 * lead_time)
    penalized = base * (1 - 0.50 * fake_rate) * (1 - 0.25 * bias_penalty)
    return max(0.0, min(1.0, penalized))
