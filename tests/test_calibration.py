import pytest
from critical_consensus.calibration import brier_score, calibration_shrinkage, source_credibility, weighted_consensus

def test_brier_score():
    assert brier_score([0.9, 0.2], [1, 0]) == pytest.approx(0.025)

def test_brier_rejects_mismatched_arrays():
    with pytest.raises(ValueError): brier_score([0.5], [0, 1])

def test_bad_brier_shrinks_confidence_not_upward():
    adjusted = calibration_shrinkage(0.99, base_rate=0.5, brier=0.30, brier_threshold=0.12)
    assert adjusted < 0.99
    assert adjusted >= 0.5

def test_overconfidence_penalty():
    adjusted = calibration_shrinkage(0.99, base_rate=0.5, brier=0.1, high_confidence_accuracy=0.6)
    assert adjusted == pytest.approx(0.745)

def test_weighted_consensus_needs_verdicts():
    with pytest.raises(ValueError): weighted_consensus([])

def test_source_credibility_decreases_with_fake_rate():
    kwargs = dict(verified_accuracy=0.9, authenticity=0.9, independence=0.9, freshness=0.9,
                  transparency=0.8, lead_time=0.8, bias_penalty=0.1)
    good = source_credibility(**kwargs, fake_rate=0.0)
    bad = source_credibility(**kwargs, fake_rate=0.8)
    assert good > bad


def test_brier_score_rejects_non_finite_and_non_binary_values():
    import math
    import pytest
    with pytest.raises(ValueError):
        brier_score([math.nan], [1])
    with pytest.raises(ValueError):
        brier_score([0.5], [1.5])
    with pytest.raises(ValueError):
        brier_score([True], [1])


def test_calibration_shrinkage_rejects_boolean_probability():
    import pytest
    with pytest.raises(ValueError):
        calibration_shrinkage(True, base_rate=0.5, brier=0.1)
