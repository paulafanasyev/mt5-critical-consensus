import math

from critical_consensus.permissions import OrderIntent, authorize_order


def test_live_execution_is_disabled_by_default():
    intent = OrderIntent("EURUSD", "BUY", 0.1, 1.0, 1.2, user_approved=True,
                         signed=True, expected_price=1.1)
    result = authorize_order(intent, live_mode=True)
    assert not result.allowed
    assert "live execution is disabled by policy" in result.reasons


def test_order_requires_stops_and_user_approval():
    intent = OrderIntent("EURUSD", "BUY", 0.1, None, None)
    result = authorize_order(intent)
    assert not result.allowed
    assert "stop loss is required" in result.reasons
    assert "take profit is required" in result.reasons
    assert "expected entry price is required and must be finite and positive" in result.reasons
    assert "explicit user approval is required" in result.reasons


def test_nonfinite_or_boolean_prices_and_volume_fail_closed():
    intents = [
        OrderIntent("EURUSD", "BUY", 0.1, math.nan, 1.2, user_approved=True, expected_price=1.1),
        OrderIntent("EURUSD", "BUY", 0.1, 1.0, math.inf, user_approved=True, expected_price=1.1),
        OrderIntent("EURUSD", "BUY", True, 1.0, 1.2, user_approved=True, expected_price=1.1),
        OrderIntent("EURUSD", "BUY", 0.1, 1.0, 1.2, user_approved=True, expected_price=float("nan")),
    ]
    for intent in intents:
        assert authorize_order(intent).allowed is False


def test_invalid_stop_take_geometry_fails_closed():
    intents = [
        OrderIntent("EURUSD", "BUY", 0.1, 1.12, 1.2, user_approved=True, expected_price=1.1),
        OrderIntent("EURUSD", "SELL", 0.1, 1.2, 1.1, user_approved=True, expected_price=1.1),
        OrderIntent("EURUSD", "BUY", 0.1, 0.0, 1.2, user_approved=True, expected_price=1.1),
        OrderIntent("EURUSD", "BUY", 0.1, 1.0, 1.1, user_approved=True, expected_price=1.1),
    ]
    for intent in intents:
        assert "stop loss and take profit geometry is invalid" in authorize_order(intent).reasons


def test_malformed_direction_is_denied_without_raising():
    intent = OrderIntent("EURUSD", [], 0.1, 1.0, 1.2, user_approved=True, expected_price=1.1)
    result = authorize_order(intent)
    assert result.allowed is False
    assert "direction must be BUY or SELL" in result.reasons


def test_valid_buy_and_sell_intents_pass_policy_but_never_execute():
    buy = OrderIntent("EURUSD", "BUY", 0.1, 1.0, 1.2, user_approved=True, expected_price=1.1)
    sell = OrderIntent("EURUSD", "SELL", 0.1, 1.2, 1.0, user_approved=True, expected_price=1.1)
    assert authorize_order(buy).allowed is True
    assert authorize_order(sell).allowed is True


def test_missing_expected_price_is_a_deliberate_deny():
    old_style = OrderIntent("EURUSD", "SELL", 0.1, 1.2, 1.0, user_approved=True)
    result = authorize_order(old_style)
    assert result.allowed is False
    assert "expected entry price is required and must be finite and positive" in result.reasons


def test_live_mode_requires_policy_enablement_and_signed_flag():
    valid = OrderIntent("EURUSD", "BUY", 0.1, 1.0, 1.2, user_approved=True,
                        signed=False, expected_price=1.1)
    denied = authorize_order(valid, live_mode=True, live_enabled=True)
    assert denied.allowed is False
    assert "live order requires a valid signature" in denied.reasons
