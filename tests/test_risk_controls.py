from datetime import datetime, timedelta, timezone

from critical_consensus.risk_controls import (
    global_kill_switch_gate, risk_controlled_permission, risk_snapshot_gate, risk_request_binding_hash,
)


NOW = datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc)


def good_state(active=False):
    return {"kill_switch_active": active, "updated_by": "operator", "reason": "test state",
            "updated_at": (NOW - timedelta(seconds=1)).isoformat(), "status": "PERSISTED"}


def good_request():
    return {"instrument": "EURUSD", "direction": "BUY", "size": 0.1, "expected_price": 1.1001,
            "sl_tp": {"sl": 1.0991, "tp": 1.1020}, "mode": "demo"}


def good_snapshot():
    return {"status": "PASS", "observed_at": NOW.isoformat(), "account_mode": "demo",
            "symbol": "EURUSD", "symbol_group": "fx-major", "account_equity": 10000.0,
            "day_start_equity": 10000.0, "equity_high_watermark": 10200.0,
            "risk_fraction": 0.002, "daily_loss_fraction": 0.0, "drawdown_fraction": 0.0196,
            "open_positions": 1, "correlated_open_positions": 1,
            "spread_points": 10.0, "max_spread_points": 20.0,
            "quote_age_seconds": 1.0, "max_quote_age_seconds": 15,
            "request_binding_sha256": risk_request_binding_hash(good_request()),
            "within_high_impact_news_window": None}


def test_missing_or_active_kill_switch_blocks():
    assert "KILL_SWITCH_STATE_UNKNOWN" in global_kill_switch_gate(None)["blockers"]
    result = global_kill_switch_gate(good_state(active=True))
    assert result["status"] == "DENY"
    assert "KILL_SWITCH_ACTIVE_OR_INVALID" in result["blockers"]


def test_only_explicit_persisted_disabled_switch_passes_control_gate():
    assert global_kill_switch_gate(good_state(active=False))["status"] == "PASS"


def test_missing_stale_future_or_real_account_snapshot_blocks():
    assert "RISK_SNAPSHOT_MISSING" in risk_snapshot_gate(None, now=NOW)["blockers"]
    stale = good_snapshot()
    stale["observed_at"] = (NOW - timedelta(seconds=60)).isoformat()
    assert "RISK_SNAPSHOT_STALE" in risk_snapshot_gate(stale, now=NOW)["blockers"]
    future = good_snapshot()
    future["observed_at"] = (NOW + timedelta(minutes=2)).isoformat()
    assert "RISK_SNAPSHOT_FROM_FUTURE" in risk_snapshot_gate(future, now=NOW)["blockers"]
    real = good_snapshot()
    real["account_mode"] = "real"
    assert "MT5_ACCOUNT_NOT_DEMO_OR_UNKNOWN" in risk_snapshot_gate(real, now=NOW)["blockers"]


def test_risk_controlled_permission_blocks_when_global_switch_active_even_if_request_looks_valid():
    result = risk_controlled_permission(
        {"instrument": "EURUSD", "mode": "demo"}, {"permission_matrix": {}},
        kill_switch_state=good_state(active=True), snapshot=good_snapshot(), now=NOW,
    )
    assert result["status"] == "DENY"
    assert result["trade_allowed"] is False
    assert "KILL_SWITCH_ACTIVE_OR_INVALID" in result["blockers"]


def test_risk_controlled_permission_fails_closed_when_account_baseline_unknown():
    bad = good_snapshot()
    bad["equity_high_watermark"] = None
    result = risk_controlled_permission(
        {"instrument": "EURUSD", "mode": "demo"}, {"permission_matrix": {}},
        kill_switch_state=good_state(active=False), snapshot=bad, now=NOW,
    )
    assert result["status"] == "DENY"
    assert result["trade_allowed"] is False
    assert "RISK_SNAPSHOT_EQUITY_HIGH_WATERMARK_MISSING_OR_INVALID" in result["blockers"]

def test_risk_snapshot_cannot_be_reused_for_a_different_order_size():
    request = good_request()
    request["size"] = 0.2
    result = risk_controlled_permission(
        request, {"permission_matrix": {}},
        kill_switch_state=good_state(active=False), snapshot=good_snapshot(), now=NOW,
    )
    assert result["status"] == "DENY"
    assert "RISK_REQUEST_BINDING_MISMATCH" in result["blockers"]
    assert result["trade_allowed"] is False



def test_risk_snapshot_symbol_must_match_bound_request_instrument():
    snapshot = good_snapshot()
    snapshot["symbol"] = "GBPUSD"
    result = risk_controlled_permission(
        good_request(), {"permission_matrix": {}},
        kill_switch_state=good_state(active=False), snapshot=snapshot, now=NOW,
    )
    assert result["status"] == "DENY"
    assert "RISK_SNAPSHOT_SYMBOL_MISMATCH" in result["blockers"]
    assert result["trade_allowed"] is False


def test_risk_snapshot_rejects_impossible_correlated_position_count():
    snapshot = good_snapshot()
    snapshot["open_positions"] = 1
    snapshot["correlated_open_positions"] = 2
    result = risk_snapshot_gate(snapshot, now=NOW)
    assert result["status"] == "DENY"
    assert "CORRELATED_POSITION_COUNT_EXCEEDS_OPEN_POSITIONS" in result["blockers"]



def test_risk_snapshot_rejects_spread_above_capture_policy():
    snapshot = good_snapshot()
    snapshot["spread_points"] = 25.0
    result = risk_snapshot_gate(snapshot, now=NOW)
    assert result["status"] == "DENY"
    assert "RISK_SNAPSHOT_SPREAD_LIMIT_EXCEEDED" in result["blockers"]


def test_risk_snapshot_rejects_invalid_freshness_policy():
    snapshot = good_snapshot()
    for invalid in (True, False, 1.5, "15", None, float("nan"), float("inf"), 0, -1, 301):
        result = risk_snapshot_gate(snapshot, now=NOW, max_age_seconds=invalid)
        assert result["status"] == "DENY"
        assert result["blockers"] == ["RISK_SNAPSHOT_MAX_AGE_POLICY_INVALID"]


def test_risk_snapshot_accepts_bounded_positive_integer_freshness_policy():
    result = risk_snapshot_gate(good_snapshot(), now=NOW, max_age_seconds=15)
    assert result["status"] == "PASS"

def test_risk_snapshot_rejects_stale_quote_even_when_snapshot_itself_is_fresh():
    snapshot = good_snapshot()
    snapshot["quote_age_seconds"] = 45.0
    snapshot["observed_at"] = NOW.isoformat()
    result = risk_snapshot_gate(snapshot, now=NOW)
    assert result["status"] == "DENY"
    assert "MT5_QUOTE_STALE" in result["blockers"]


def test_risk_snapshot_rejects_missing_future_or_unbounded_quote_age_policy():
    snapshot = good_snapshot()
    for patch in (
        {"quote_age_seconds": None},
        {"quote_age_seconds": -6.0},
        {"max_quote_age_seconds": 16},
        {"max_quote_age_seconds": True},
        {"max_quote_age_seconds": "15"},
    ):
        invalid = {**snapshot, **patch}
        result = risk_snapshot_gate(invalid, now=NOW)
        assert result["status"] == "DENY"
        assert any(blocker.startswith("MT5_QUOTE_AGE") or blocker == "MT5_QUOTE_STALE"
                   for blocker in result["blockers"])

