from critical_consensus.permissions import OrderIntent, authorize_order


def test_live_execution_is_disabled_by_default():
    intent = OrderIntent("EURUSD", "BUY", 0.1, 1.0, 1.2, user_approved=True, signed=True)
    result = authorize_order(intent, live_mode=True)
    assert not result.allowed
    assert "live execution is disabled by policy" in result.reasons


def test_order_requires_stops_and_user_approval():
    intent = OrderIntent("EURUSD", "BUY", 0.1, None, None)
    result = authorize_order(intent)
    assert not result.allowed
    assert "stop loss is required" in result.reasons
    assert "take profit is required" in result.reasons
    assert "explicit user approval is required" in result.reasons


def test_allowlisted_demo_order_passes_policy():
    intent = OrderIntent("EURUSD", "SELL", 0.1, 1.2, 1.0, user_approved=True)
    result = authorize_order(intent)
    assert result.allowed
