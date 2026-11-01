from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mt5_terminal_readonly_smoke.py"
SPEC = importlib.util.spec_from_file_location("mt5_terminal_readonly_smoke", SCRIPT)
PROBE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PROBE)


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self, *, trade_mode=None, login=123456, server="Broker-Demo",
                 connected=True, initialize=True, bid=1.10001, ask=1.10003):
        self.trade_mode = self.ACCOUNT_TRADE_MODE_DEMO if trade_mode is None else trade_mode
        self.account_login = login
        self.server = server
        self.login_kwargs = None
        self.login_result = True
        self.connected = connected
        self.initialize_result = initialize
        self.bid = bid
        self.ask = ask
        self.initialize_kwargs = None
        self.shutdown_called = False
        self.selected = []

    def initialize(self, **kwargs):
        self.initialize_kwargs = kwargs
        return self.initialize_result

    def last_error(self):
        return (-10004, "credentials intentionally not reproduced")

    def login(self, **kwargs):
        self.login_kwargs = kwargs
        return self.login_result

    def terminal_info(self):
        return SimpleNamespace(connected=self.connected, trade_allowed=True)

    def account_info(self):
        return SimpleNamespace(
            login=self.account_login, server=self.server, trade_mode=self.trade_mode,
            trade_allowed=True,
        )

    def symbol_info(self, symbol):
        return SimpleNamespace(visible=True, name=symbol)

    def symbol_select(self, symbol, enabled):
        self.selected.append((symbol, enabled))
        return True

    def symbol_info_tick(self, symbol):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        return SimpleNamespace(time_msc=now_ms, bid=self.bid, ask=self.ask)

    def shutdown(self):
        self.shutdown_called = True


def test_readonly_smoke_accepts_exact_demo_account_and_reads_quotes():
    mt5 = FakeMT5()
    report = PROBE.run_probe(
        mt5, login=123456, password="sensitive-password", server="Broker-Demo",
        symbols=["EURUSD", "GBPUSD"],
    )

    assert report["status"] == "PASS"
    assert report["mode"] == "DEMO"
    assert report["terminal_connected"] is True
    assert report["orders_submitted"] == 0
    assert report["positions_modified"] == 0
    assert [row["symbol"] for row in report["quotes"]] == ["EURUSD", "GBPUSD"]
    assert report["quotes"][0]["ask"] >= report["quotes"][0]["bid"]
    assert mt5.initialize_kwargs == {"timeout": 60_000}
    assert mt5.login_kwargs == {
        "login": 123456, "password": "sensitive-password", "server": "Broker-Demo",
    }
    assert mt5.shutdown_called is True
    assert "sensitive-password" not in json.dumps(report)
    assert not hasattr(mt5, "order_send")


def test_readonly_smoke_rejects_a_real_account_and_still_shuts_down():
    mt5 = FakeMT5(trade_mode=FakeMT5.ACCOUNT_TRADE_MODE_REAL)

    try:
        PROBE.run_probe(mt5, login=123456, password="secret", server="Broker-Demo",
                        symbols=["EURUSD"])
    except PROBE.ProbeError as exc:
        assert "not a MetaTrader DEMO account" in str(exc)
    else:
        raise AssertionError("real account was accepted")

    assert mt5.shutdown_called is True


def test_readonly_smoke_rejects_a_different_connected_login():
    mt5 = FakeMT5(login=654321)

    try:
        PROBE.run_probe(mt5, login=123456, password="secret", server="Broker-Demo",
                        symbols=["EURUSD"])
    except PROBE.ProbeError as exc:
        assert "does not match the configured demo login" in str(exc)
    else:
        raise AssertionError("mismatched account login was accepted")

    assert mt5.shutdown_called is True


def test_readonly_smoke_rejects_a_disconnected_terminal():
    mt5 = FakeMT5(connected=False)

    try:
        PROBE.run_probe(mt5, login=123456, password="secret", server="Broker-Demo",
                        symbols=["EURUSD"])
    except PROBE.ProbeError as exc:
        assert "not connected" in str(exc)
    else:
        raise AssertionError("disconnected terminal was accepted")

    assert mt5.shutdown_called is True


def test_readonly_smoke_rejects_an_invalid_market_quote():
    mt5 = FakeMT5(ask=1.09999)

    try:
        PROBE.run_probe(mt5, login=123456, password="secret", server="Broker-Demo",
                        symbols=["EURUSD"])
    except PROBE.ProbeError as exc:
        assert "ask is below bid" in str(exc)
    else:
        raise AssertionError("crossed quote was accepted")

    assert mt5.shutdown_called is True


def test_readonly_smoke_refuses_duplicate_or_malformed_symbols_before_connecting():
    mt5 = FakeMT5()
    for symbols in (["EURUSD", "EURUSD"], ["EURUSD;OrderSend"], [""]):
        try:
            PROBE.run_probe(mt5, login=123456, password="secret", server="Broker-Demo",
                            symbols=symbols)
        except PROBE.ProbeError as exc:
            assert "Symbols must be unique" in str(exc)
        else:
            raise AssertionError(f"invalid symbols accepted: {symbols!r}")
    assert mt5.initialize_kwargs is None
    assert mt5.shutdown_called is False


def test_readonly_smoke_redacts_terminal_initialization_failure():
    mt5 = FakeMT5(initialize=False)

    try:
        PROBE.run_probe(mt5, login=123456, password="top-secret", server="Broker-Demo",
                        symbols=["EURUSD"])
    except PROBE.ProbeError as exc:
        assert "error code -10004" in str(exc)
        assert "credentials intentionally" not in str(exc)
        assert "top-secret" not in str(exc)
    else:
        raise AssertionError("failed initialization was accepted")
