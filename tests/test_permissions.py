import math

from critical_consensus.permissions import OrderIntent, authorize_order


def test_live_execution_is_disabled_by_default():
    intent = OrderIntent("EURUSD", "BUY", 0.1, 1.0, 1.2, user_approved=True,
                         signed=True, expected_price=1.1)
    result = authorize_order(intent, live_mode=True)
    assert not result.allowed
    assert "live execution is disabled by policy" in result.reasons


def test_order_requires_finite_protective_levels_and_user_approval():
    intent = OrderIntent("EURUSD", "BUY", 0.1, None, None)
    result = authorize_order(intent)
    assert not result.allowed
    assert "stop loss is required and must be finite" in result.reasons
    assert "take profit is required and must be finite" in result.reasons
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
    buy = OrderIntent("EURUSD", "BUY", 0.1, 1.12, 1.2, user_approved=True, expected_price=1.1)
    sell = OrderIntent("EURUSD", "SELL", 0.1, 1.2, 1.1, user_approved=True, expected_price=1.1)
    assert "stop loss and take profit geometry is invalid" in authorize_order(buy).reasons
    assert "stop loss and take profit geometry is invalid" in authorize_order(sell).reasons


def test_allowlisted_demo_intent_passes_policy_but_does_not_execute():
    intent = OrderIntent("EURUSD", "SELL", 0.1, 1.2, 1.0, user_approved=True, expected_price=1.1)
    result = authorize_order(intent)
    assert result.allowed
