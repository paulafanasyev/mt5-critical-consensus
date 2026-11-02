"""Tests for the shadow pass: it sees prices, holds nothing, and has no way to send."""
from __future__ import annotations

import functools
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

UTC = timezone.utc


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=1)
def _module():
    path = _root() / "scripts" / "observe_shadow.py"
    spec = importlib.util.spec_from_file_location("observe_shadow", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["observe_shadow"] = module
    spec.loader.exec_module(module)
    return module


observe = _module()


class StubTerminal:
    """The read side of a terminal, with a book the shadow must not be able to see."""

    def __init__(self) -> None:
        self.account_calls = 0

    def account(self):
        self.account_calls += 1
        return SimpleNamespace(equity=1000.0, balance=980.0, currency="USD", margin=0.0,
                               margin_free=1000.0, margin_level=0.0, leverage=100, login=1,
                               server="MetaQuotes-Demo")

    def symbol_spec(self, symbol: str):
        return SimpleNamespace(symbol=symbol, point=0.01, digits=2, tick_value=1.0,
                               contract_size=100.0, volume_min=0.01, volume_max=100.0,
                               volume_step=0.01, trade_mode=0)

    def tick(self, symbol: str):
        return SimpleNamespace(bid=100.0, ask=100.5, time=datetime(2026, 11, 2, 12, tzinfo=UTC))

    def bars(self, symbol: str, timeframe: str, count: int):
        return [SimpleNamespace(time=datetime(2026, 11, 2, 12, tzinfo=UTC), open=100.0, high=101.0,
                                low=99.0, close=100.5, tick_volume=10, spread=1)] * count

    def positions(self):
        return [SimpleNamespace(ticket=1)]

    def shutdown(self) -> None:
        return None


def test_flat_book_honours_the_backend_contract():
    from critical_consensus.mt5_agent.backend import assert_backend_contract

    book = observe.FlatBook(StubTerminal())
    assert assert_backend_contract(book) is None


def test_flat_book_has_no_order_surface():
    book = observe.FlatBook(StubTerminal())
    for name in ("order_send", "place", "send", "modify", "cancel", "close_position",
                 "position_close", "order"):
        assert not hasattr(book, name), name


def test_flat_book_replaces_only_this_process_s_book():
    terminal = StubTerminal()
    book = observe.FlatBook(terminal)
    assert book.positions() == []
    assert book.realized_daily_pnl == 0.0
    assert book.day_start_equity == 1000.0
    assert book.peak_equity == 1000.0
    # The churn gate's own views stay absent rather than delegated: a shadow has no history.
    assert not hasattr(book, "opened_leg_tickets")
    assert not hasattr(book, "position_stop_risk")
    # Everything that prices a decision still comes from the real terminal.
    assert book.tick("XAUUSD").ask == 100.5
    assert len(book.bars("XAUUSD", "H1", 5)) == 5
    assert book.account().equity == 1000.0
    assert terminal.account_calls >= 3


def test_observe_policy_keeps_the_production_rule_and_declares_no_send(tmp_path, monkeypatch):
    policy = observe.observe_policy("EURUSD")
    assert policy.mode == "observe"
    assert policy.symbol == "EURUSD"
    # The live read takes the contract from the terminal, never from the declared spec.
    assert policy.symbol_spec is None
    assert policy.kill_switch_active == observe.kill_switch_active()


def test_kill_switch_state_is_read_from_the_file(tmp_path, monkeypatch):
    stop = tmp_path / "kill_switch.json"
    stop.write_text(json.dumps({"kill_switch_active": True}), encoding="utf-8")
    monkeypatch.setattr(observe, "KILL_SWITCH", stop)
    assert observe.kill_switch_active() is True
    monkeypatch.setattr(observe, "KILL_SWITCH", tmp_path / "absent.json")
    assert observe.kill_switch_active() is False
