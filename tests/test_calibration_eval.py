from datetime import datetime, timedelta, timezone

import pytest

from critical_consensus.calibration_eval import (
    Forecast, evaluate_walk_forward, load_forecasts_csv, parse_forecast_row,
)


UTC = timezone.utc


def dataset(n=120):
    rows = []
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(n):
        issued = start + timedelta(days=i)
        outcome = int((i % 10) < 5)
        # The deliberately overconfident raw model is deterministic; tests validate the
        # protocol and report shape, not a claim that these observations represent markets.
        probability = 0.9 if outcome else 0.1
        rows.append(Forecast("test-model", f"s-{i}", issued, issued + timedelta(hours=1), probability, outcome))
    return rows


def test_walk_forward_calibration_uses_only_matured_past_and_reports_sample_size():
    report = evaluate_walk_forward(dataset(), min_train_samples=20, min_class_samples=5, refit_interval=10)
    model = report["models"]["test-model"]
    wf = model["walk_forward"]
    assert model["total_resolved_forecasts"] == 120
    assert wf["calibrated_oos_count"] > 50
    assert wf["sample_status"] == "PRELIMINARY_REVIEW_ONLY"
    assert report["provenance_status"] == "NOT_VERIFIED_BY_SCRIPT"
    assert "profitability" in report["important_limit"]


def test_insufficient_training_sample_is_not_proven():
    report = evaluate_walk_forward(dataset(15), min_train_samples=30, refit_interval=5)
    assert report["overall_status"] == "NOT_PROVEN"
    assert report["models"]["test-model"]["walk_forward"]["calibration_comparison_status"] == "NOT_PROVEN"


def test_forecast_parser_rejects_timezone_naive_input():
    with pytest.raises(ValueError, match="timezone"):
        parse_forecast_row({
            "model_id": "m", "signal_id": "s", "issued_at": "2026-01-01T12:00:00",
            "resolved_at": "2026-01-01T13:00:00Z", "probability": "0.7", "outcome": "1",
        })


def test_forecast_parser_rejects_invalid_probability_and_outcome():
    base = {"model_id": "m", "signal_id": "s", "issued_at": "2026-01-01T12:00:00Z",
            "resolved_at": "2026-01-01T13:00:00Z", "probability": "1.1", "outcome": "1"}
    with pytest.raises(ValueError, match=r"\[0,1\]"):
        parse_forecast_row(base)
    base["probability"] = "0.5"
    base["outcome"] = "0.3"
    with pytest.raises(ValueError, match="0 or 1"):
        parse_forecast_row(base)


def test_csv_loader_rejects_duplicate_model_signal(tmp_path):
    path = tmp_path / "forecasts.csv"
    path.write_text(
        "model_id,signal_id,issued_at,resolved_at,probability,outcome\n"
        "m,s1,2026-01-01T12:00:00Z,2026-01-01T13:00:00Z,0.7,1\n"
        "m,s1,2026-01-02T12:00:00Z,2026-01-02T13:00:00Z,0.6,0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_forecasts_csv(path)



def test_walk_forward_rejects_naive_datetimes_in_direct_forecast_objects():
    start = datetime(2026, 1, 1)
    row = Forecast("m", "s", start, start + timedelta(hours=1), 0.7, 1)
    with pytest.raises(ValueError, match="issued_at must include a timezone"):
        evaluate_walk_forward([row])


@pytest.mark.parametrize("probability", [float("nan"), float("inf"), -0.01, 1.01, True])
def test_walk_forward_rejects_invalid_direct_probabilities(probability):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    row = Forecast("m", "s", start, start + timedelta(hours=1), probability, 1)
    with pytest.raises(ValueError, match="probability"):
        evaluate_walk_forward([row])


@pytest.mark.parametrize("outcome", [-1, 2, 0.5, True])
def test_walk_forward_rejects_invalid_direct_outcomes(outcome):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    row = Forecast("m", "s", start, start + timedelta(hours=1), 0.7, outcome)
    with pytest.raises(ValueError, match="outcome must be integer 0 or 1"):
        evaluate_walk_forward([row])


def test_walk_forward_rejects_duplicate_model_signal_pairs():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = Forecast("m", "same", start, start + timedelta(hours=1), 0.7, 1)
    second = Forecast("m", "same", start + timedelta(days=1),
                      start + timedelta(days=1, hours=1), 0.6, 0)
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_walk_forward([first, second])


def test_walk_forward_rejects_outcome_resolved_before_forecast_issue():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    row = Forecast("m", "s", start, start - timedelta(seconds=1), 0.7, 1)
    with pytest.raises(ValueError, match="resolved_at must be later"):
        evaluate_walk_forward([row])
