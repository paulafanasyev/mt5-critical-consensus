from datetime import datetime, timedelta, timezone

from critical_consensus.risk_controls import (
    global_kill_switch_gate, risk_controlled_permission, risk_request_binding_hash,
    risk_snapshot_gate, risk_snapshot_matches_order,
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
            "account_balance": 10000.0, "daily_pnl": 0.0,
            "day_start_equity": 10000.0, "equity_high_watermark": 10200.0,
            "risk_fraction": 0.002, "risk_value_account_currency": 20.0,
            "risk_tick_size": 0.0001, "risk_tick_value_loss": 20.0,
            "reference_price": 1.1001,
            "daily_loss_fraction": 0.0, "drawdown_fraction": 0.0196,
            "open_stop_risk_fraction": 0.0,
            "open_positions": 1, "correlated_open_positions": 1,
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


def test_open_stop_risk_is_required_and_cannot_be_omitted():
    snapshot = good_snapshot()
    snapshot.pop("open_stop_risk_fraction")
    assert "OPEN_STOP_RISK_FRACTION_MISSING_OR_INVALID" in risk_snapshot_gate(snapshot, now=NOW)["blockers"]


def test_open_stop_risk_counts_toward_the_prospective_daily_cap():
    snapshot = good_snapshot()
    snapshot["open_stop_risk_fraction"] = 0.018
    result = risk_controlled_permission(
        good_request(), {"permission_matrix": {}},
        kill_switch_state=good_state(active=False), snapshot=snapshot, now=NOW,
    )
    assert result["status"] == "DENY"
    assert "PROJECTED_DAILY_LOSS_EXCEEDS_LIMIT" in result["blockers"]
    assert result["trade_allowed"] is False


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


def test_snapshot_aggregates_must_match_the_measured_order_and_equity():
    assert risk_snapshot_matches_order(good_request(), good_snapshot()) == []

    understated = good_snapshot()
    understated["risk_fraction"] = 0.0001
    assert "RISK_FRACTION_DOES_NOT_MATCH_ORDER" in risk_snapshot_matches_order(
        good_request(), understated)

    forged_value = good_snapshot()
    forged_value["risk_value_account_currency"] = 1.0
    assert "RISK_VALUE_DOES_NOT_MATCH_ORDER" in risk_snapshot_matches_order(
        good_request(), forged_value)

    forged_drawdown = good_snapshot()
    forged_drawdown["drawdown_fraction"] = 0.0
    assert "DRAWDOWN_DOES_NOT_MATCH_EQUITY" in risk_snapshot_matches_order(
        good_request(), forged_drawdown)

    wrong_symbol = good_snapshot()
    wrong_symbol["symbol"] = "XAUUSD"
    assert "RISK_SNAPSHOT_SYMBOL_MISMATCH" in risk_snapshot_matches_order(
        good_request(), wrong_symbol)


def test_risk_controlled_permission_denies_a_reconciled_mismatch_before_policy():
    snapshot = good_snapshot()
    snapshot["risk_fraction"] = 0.00001
    result = risk_controlled_permission(
        good_request(), {"permission_matrix": {}},
        kill_switch_state=good_state(active=False), snapshot=snapshot, now=NOW,
    )
    assert result["status"] == "DENY"
    assert "RISK_FRACTION_DOES_NOT_MATCH_ORDER" in result["blockers"]
    assert result["trade_allowed"] is False


def test_kill_switch_must_be_explicitly_persisted():
    state = good_state()
    state["status"] = "UNKNOWN"
    result = global_kill_switch_gate(state)
    assert result["status"] == "DENY"
    assert "KILL_SWITCH_STATE_NOT_PERSISTED" in result["blockers"]


def test_snapshot_rejects_peak_below_equity_and_empty_group():
    snapshot = good_snapshot()
    snapshot["equity_high_watermark"] = snapshot["account_equity"] - 1.0
    assert "RISK_SNAPSHOT_EQUITY_HIGH_WATERMARK_BELOW_EQUITY" in risk_snapshot_gate(snapshot, now=NOW)["blockers"]
    snapshot = good_snapshot()
    snapshot["symbol_group"] = "   "
    assert "SYMBOL_CORRELATION_GROUP_UNKNOWN" in risk_snapshot_gate(snapshot, now=NOW)["blockers"]


def test_daily_loss_fraction_is_recomputed_from_the_day_start_baseline():
    snapshot = good_snapshot()
    snapshot["account_equity"] = 9900.0
    snapshot["day_start_equity"] = 10000.0
    snapshot["drawdown_fraction"] = 0.0294
    snapshot["daily_loss_fraction"] = 0.0  # forged: real loss is 1%
    snapshot["risk_value_account_currency"] = 20.0
    assert "DAILY_LOSS_DOES_NOT_MATCH_EQUITY" in risk_snapshot_matches_order(good_request(), snapshot)


def test_tick_spec_must_be_present_positive_and_use_the_loss_value():
    for field in ("risk_tick_size", "risk_tick_value_loss"):
        for value in (None, 0, -1.0):
            snapshot = good_snapshot()
            snapshot[field] = value
            assert "RISK_SNAPSHOT_TICK_SPEC_MISSING_OR_INVALID" in risk_snapshot_matches_order(
                good_request(), snapshot), (field, value)
    # A profit tick value is not a loss tick value: a smaller one understates money at risk.
    snapshot = good_snapshot()
    snapshot["risk_tick_value_loss"] = 10.0
    assert "RISK_VALUE_DOES_NOT_MATCH_ORDER" in risk_snapshot_matches_order(good_request(), snapshot)


def test_risk_controlled_permission_reaches_human_review_only_when_every_gate_agrees():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    request = good_request()
    request.update({
        "permission_id": "perm-1", "expected_rr": 1.9,
        "chief_verdict": "CONFIRM", "certainty_status": "PASS", "compliance_status": "PASS",
        "risk_flags": [], "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "within_high_impact_news_window": False,
    })
    snapshot = good_snapshot()
    snapshot["observed_at"] = now.isoformat()
    snapshot["request_binding_sha256"] = risk_request_binding_hash(request)
    snapshot["within_high_impact_news_window"] = False
    matrix = {"permission_matrix": {
        "instruments": ["EURUSD"], "max_size_per_trade": 0.2, "max_positions": 5,
        "max_correlated_positions": 2, "max_risk_per_trade_fraction": 0.005,
        "daily_loss_limit_fraction": 0.02, "max_drawdown_fraction": 0.10,
        "allowed_directions": ["BUY", "SELL"],
        "allowed_time_utc": {"start": "07:00", "end": "19:00"}, "rollover_block_utc": None,
        "blocked_around_news": True, "blocked_flags": ["sanctions", "mnpi"],
        "min_expected_rr": 1.5, "expected_rr_tolerance_fraction": 0.01,
        "require_stop_loss": True, "require_take_profit": True,
        "max_command_ttl_seconds": 600, "demo_enabled": True, "live_enabled": False,
    }}
    result = risk_controlled_permission(
        request, matrix, kill_switch_state=good_state(active=False),
        snapshot=snapshot, now=now,
    )
    assert result["status"] == "REQUIRE_HUMAN"
    assert result["blockers"] == []
    assert result["risk_reconciliation"]["status"] == "PASS"
    # A positive policy result still never authorizes execution.
    assert result["trade_allowed"] is False
    assert result["allowed"] is False


def sell_request():
    return {"instrument": "EURUSD", "direction": "SELL", "size": 0.1, "expected_price": 1.1001,
            "sl_tp": {"sl": 1.1011, "tp": 1.0982}}


def test_risk_value_is_sized_at_the_worse_of_reviewed_and_reference_price():
    buy = good_snapshot()
    buy["reference_price"] = 1.10015  # ask drifted up from the reviewed 1.1001
    buy_value = abs(1.10015 - 1.0991) / 0.0001 * 20.0 * 0.1
    buy["risk_value_account_currency"] = buy_value
    buy["risk_fraction"] = buy_value / 10000.0
    assert risk_snapshot_matches_order(good_request(), buy) == []
    # Sizing at the reviewed entry understates money at risk once the fill can drift.
    buy["risk_value_account_currency"] = 20.0
    buy["risk_fraction"] = 0.002
    assert "RISK_VALUE_DOES_NOT_MATCH_ORDER" in risk_snapshot_matches_order(good_request(), buy)

    sell = good_snapshot()
    sell["reference_price"] = 1.10005  # bid drifted down from the reviewed 1.1001
    sell_value = abs(1.10005 - 1.1011) / 0.0001 * 20.0 * 0.1
    sell["risk_value_account_currency"] = sell_value
    sell["risk_fraction"] = sell_value / 10000.0
    assert risk_snapshot_matches_order(sell_request(), sell) == []
    sell["risk_value_account_currency"] = 20.0
    sell["risk_fraction"] = 0.002
    assert "RISK_VALUE_DOES_NOT_MATCH_ORDER" in risk_snapshot_matches_order(sell_request(), sell)


def test_slippage_beyond_a_tenth_of_the_stop_distance_is_denied():
    at_cap = good_snapshot()
    at_cap["reference_price"] = 1.1002  # exactly 10% of the 0.0010 stop distance
    at_cap["risk_value_account_currency"] = 22.0
    at_cap["risk_fraction"] = 0.0022
    assert risk_snapshot_matches_order(good_request(), at_cap) == []
    snapshot = good_snapshot()
    snapshot["reference_price"] = 1.1003
    assert "RISK_SLIPPAGE_EXCEEDS_STOP_DISTANCE_FRACTION" in risk_snapshot_matches_order(
        good_request(), snapshot)


def test_reference_price_beyond_the_reward_side_is_denied():
    snapshot = good_snapshot()
    snapshot["reference_price"] = 1.1030  # above take profit: the fill erases the reward
    blockers = risk_snapshot_matches_order(good_request(), snapshot)
    assert "RISK_CONSERVATIVE_ENTRY_OUTSIDE_GEOMETRY" in blockers
    assert "RISK_SLIPPAGE_EXCEEDS_STOP_DISTANCE_FRACTION" in blockers


def test_missing_reference_price_blocks_reconciliation():
    snapshot = good_snapshot()
    del snapshot["reference_price"]
    assert risk_snapshot_matches_order(good_request(), snapshot) == [
        "RISK_SNAPSHOT_REFERENCE_PRICE_MISSING_OR_INVALID"]


def test_adjusted_rr_below_floor_is_denied_even_when_reviewed_rr_passes():
    request = {"instrument": "EURUSD", "direction": "BUY", "size": 0.1, "mode": "demo",
               "expected_price": 1.1001, "sl_tp": {"sl": 1.0995, "tp": 1.1010}}
    snapshot = good_snapshot()
    snapshot["reference_price"] = 1.10016
    value = abs(1.10016 - 1.0995) / 0.0001 * 20.0 * 0.1
    snapshot["risk_value_account_currency"] = value
    snapshot["risk_fraction"] = value / 10000.0
    snapshot["request_binding_sha256"] = risk_request_binding_hash(request)
    matrix = {"permission_matrix": {"min_expected_rr": 1.5}}
    denied = risk_controlled_permission(
        request, matrix, kill_switch_state=good_state(active=False), snapshot=snapshot, now=NOW,
    )
    assert denied["status"] == "DENY"
    assert "RISK_ADJUSTED_RR_BELOW_MINIMUM" in denied["blockers"]
    assert denied["trade_allowed"] is False
    # At the reviewed price the same order still clears the reward floor.
    snapshot["reference_price"] = 1.1001
    value = abs(1.1001 - 1.0995) / 0.0001 * 20.0 * 0.1
    snapshot["risk_value_account_currency"] = value
    snapshot["risk_fraction"] = value / 10000.0
    reviewed = risk_controlled_permission(
        request, matrix, kill_switch_state=good_state(active=False), snapshot=snapshot, now=NOW,
    )
    assert "RISK_ADJUSTED_RR_BELOW_MINIMUM" not in reviewed["blockers"]


def test_snapshot_baselines_must_agree_with_each_other_and_the_balance():
    peak_below_start = good_snapshot()
    peak_below_start["day_start_equity"] = 10400.0
    assert "RISK_SNAPSHOT_EQUITY_HIGH_WATERMARK_BELOW_DAY_START" in risk_snapshot_gate(
        peak_below_start, now=NOW)["blockers"]

    understated_start = good_snapshot()
    understated_start["day_start_equity"] = 9000.0
    assert "DAY_START_EQUITY_BELOW_DAILY_PNL_IMPLIED_BALANCE" in risk_snapshot_gate(
        understated_start, now=NOW)["blockers"]

    for field, blocker in (("account_balance", "RISK_SNAPSHOT_ACCOUNT_BALANCE_MISSING_OR_INVALID"),
                           ("daily_pnl", "RISK_SNAPSHOT_DAILY_PNL_MISSING_OR_INVALID")):
        snapshot = good_snapshot()
        snapshot.pop(field)
        assert blocker in risk_snapshot_gate(snapshot, now=NOW)["blockers"], field
