from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from critical_consensus.mt5_readonly import MT5ReadOnlyAdapter, MT5ReadOnlyError

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


class FakeMT5:
    def __init__(self, tick=None, initialize_result=True, raise_on_tick=False):
        self.tick = tick
        self.initialize_result = initialize_result
        self.raise_on_tick = raise_on_tick
        self.initialized = False
        self.shutdown_called = False

    def initialize(self):
        self.initialized = True
        return self.initialize_result

    def shutdown(self):
        self.shutdown_called = True

    def symbol_info_tick(self, symbol):
        if self.raise_on_tick:
            raise RuntimeError("synthetic terminal failure")
        return self.tick


def make_adapter(client):
    return MT5ReadOnlyAdapter(
        client,
        source_id="mt5-demo-bridge",
        allowed_symbols=("EURUSD", "XAUUSD"),
        max_age_seconds=5,
        future_skew_seconds=1,
        clock=lambda: NOW,
    )


def make_tick(**overrides):
    values = {
        "bid": 1.0840,
        "ask": 1.0842,
        "time_msc": int((NOW - timedelta(seconds=1)).timestamp() * 1000),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_adapter_reads_allowlisted_snapshot_and_never_claims_trade_permission():
    client = FakeMT5(make_tick())
    adapter = make_adapter(client)
    adapter.connect()
    result = adapter.read_snapshot("EURUSD")
    assert result["valid"] is True
    assert result["status"] == "VALID_READ_ONLY_SNAPSHOT"
    assert result["snapshot"]["symbol"] == "EURUSD"
    assert result["snapshot"]["source_id"] == "mt5-demo-bridge"
    assert result["trade_allowed"] is False
    assert not hasattr(adapter, "order_send")
    adapter.close()
    assert client.shutdown_called is True


def test_disallowed_symbol_is_rejected_without_calling_terminal():
    client = FakeMT5(make_tick())
    adapter = make_adapter(client)
    adapter.connect()
    result = adapter.read_snapshot("GBPUSD")
    assert result["valid"] is False
    assert "SYMBOL_NOT_ALLOWLISTED" in result["blockers"]
    assert result["snapshot"] is None


def test_crossed_quote_and_stale_tick_are_rejected():
    crossed = make_adapter(FakeMT5(make_tick(bid=1.2, ask=1.1)))
    crossed.connect()
    result = crossed.read_snapshot("EURUSD")
    assert result["valid"] is False
    assert "CROSSED_QUOTE" in result["blockers"]

    stale = make_adapter(FakeMT5(make_tick(time_msc=int((NOW - timedelta(seconds=10)).timestamp() * 1000))))
    stale.connect()
    result = stale.read_snapshot("EURUSD")
    assert result["valid"] is False
    assert "QUOTE_STALE" in result["blockers"]


def test_unavailable_tick_and_terminal_read_failure_fail_closed():
    for client, code in ((FakeMT5(None), "MT5_TICK_UNAVAILABLE"),
                         (FakeMT5(make_tick(), raise_on_tick=True), "MT5_TICK_READ_FAILED")):
        adapter = make_adapter(client)
        adapter.connect()
        result = adapter.read_snapshot("EURUSD")
        assert result["valid"] is False
        assert result["blockers"] == [code]
        assert result["trade_allowed"] is False


def test_adapter_refuses_read_before_connect_and_failed_initialization():
    adapter = make_adapter(FakeMT5(make_tick()))
    with pytest.raises(MT5ReadOnlyError):
        adapter.read_snapshot("EURUSD")
    failed = make_adapter(FakeMT5(make_tick(), initialize_result=False))
    with pytest.raises(MT5ReadOnlyError):
        failed.connect()
    assert failed.connected is False


def test_missing_or_nonfinite_tick_values_are_rejected():
    adapter = make_adapter(FakeMT5(make_tick(bid=float("nan"), time_msc=None)))
    adapter.connect()
    result = adapter.read_snapshot("EURUSD")
    assert result["valid"] is False
    assert "BID_INVALID" in result["blockers"]
    assert "EVENT_TIME_INVALID" in result["blockers"]


def test_policy_configuration_is_bounded_and_explicit():
    with pytest.raises(ValueError):
        MT5ReadOnlyAdapter(FakeMT5(), source_id="x", allowed_symbols=("EURUSD",),
                           max_age_seconds=61, future_skew_seconds=1)
    with pytest.raises(ValueError):
        MT5ReadOnlyAdapter(FakeMT5(), source_id="x", allowed_symbols=("EURUSD",),
                           max_age_seconds=5, future_skew_seconds=3)
