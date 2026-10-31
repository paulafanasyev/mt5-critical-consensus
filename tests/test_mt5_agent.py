"""Adversarial tests for the MetaTrader 5 paper agent."""
from __future__ import annotations

import csv
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from critical_consensus.market_data import MarketBar
from critical_consensus.mt5_agent import (AccountInfo, AgentPolicy, DecisionJournal, MT5Agent,
                                          PaperBroker, PositionSnapshot, SymbolSpec, Tick,
                                          evaluate_signal, load_policy, size_position)
from critical_consensus.mt5_agent.codes import (STATUS_BLOCKED, STATUS_NO_SIGNAL,
                                                STATUS_OBSERVED, STATUS_PAPER_OPENED)

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]


def cli_env(**overrides: str) -> dict[str, str]:
    """A subprocess environment without the operator's own language choice."""
    env = {key: value for key, value in os.environ.items() if key != "MT5_AGENT_LANGUAGE"}
    env.update(overrides)
    return env

# A broker-server wall clock expressed as epoch seconds (the terminal has no timezone field).
SERVER_EPOCH = 1_767_300_000
SPEC = SymbolSpec(symbol="EURUSD", point=0.00001, digits=5, tick_size=0.00001, tick_value=1.0,
                  volume_min=0.01, volume_max=100.0, volume_step=0.01, spread_points=10,
                  stops_level_points=0)


def make_bars(count: int = 120, *, drift: float = 1.0009, start: float = 1.10,
              symbol: str = "EURUSD") -> list[MarketBar]:
    bars, price = [], start
    moment = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
    for _ in range(count):
        price *= drift
        open_price, close_price = price * 0.9998, price
        bars.append(MarketBar(symbol=symbol, timeframe="H1", time=moment, open=open_price,
                              high=max(open_price, close_price) * 1.0004,
                              low=min(open_price, close_price) * 0.9996, close=close_price,
                              tick_volume=1_000, spread_points=10))
        moment += timedelta(hours=1)
    return bars


def policy_raw(**overrides) -> dict:
    base = {
        "mode": "paper", "symbol": "EURUSD", "timeframe": "H1", "history_bars": 120,
        "server_utc_offset_seconds": 7200,
        # These tests aim at the risk gates; the confirm-versus-auto switch has its own cases.
        "execution_mode": "auto",
        "kill_switch": {"active": False, "reason": "paper research"},
        "schedule": {"blocked_weekdays_utc": [], "window_start_minute_utc": 0,
                     "window_end_minute_utc": 1440},
        "symbol_spec": {"symbol": "EURUSD", "point": 0.00001, "digits": 5, "tick_size": 0.00001,
                        "tick_value": 1.0, "volume_min": 0.01, "volume_max": 100.0,
                        "volume_step": 0.01, "spread_points": 10, "stops_level_points": 0},
    }
    for key, value in overrides.items():
        if key in {"mode", "symbol", "timeframe", "history_bars", "server_utc_offset_seconds",
                   "execution_mode", "ui_language"}:
            base[key] = value
        else:
            section, field_name = key.split("__", 1)
            base.setdefault(section, {})
            if base[section].get(field_name) is None and field_name not in base[section]:
                base[section] = dict(base[section])
            base[section][field_name] = value
    return base


def make_policy(**overrides) -> AgentPolicy:
    return AgentPolicy.from_mapping(policy_raw(**overrides))


@dataclass
class FakeBackend:
    account_info: AccountInfo = field(default_factory=lambda: AccountInfo(
        login=42, server="Demo-Server", currency="USD", balance=10_000.0, equity=10_000.0,
        margin_free=10_000.0, leverage=100, trade_mode="demo"))
    bar_series: list[MarketBar] = field(default_factory=lambda: make_bars())
    open_positions: list[PositionSnapshot] = field(default_factory=list)
    day_start_equity: float = 10_000.0
    peak_equity: float = 10_000.0
    realized_daily_pnl: float | None = 0.0
    quote_offset_seconds: float = 0.0
    clock: datetime | None = None
    calls: list[str] = field(default_factory=list)
    symbol_specs: dict[str, SymbolSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A quote is only fresh relative to the bar the fake terminal is standing on.
        if self.clock is None:
            self.clock = self.bar_series[-1].time

    def account(self) -> AccountInfo:
        self.calls.append("account")
        return self.account_info

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        self.calls.append("symbol_spec")
        key = symbol.strip().upper()
        if key in self.symbol_specs:
            return self.symbol_specs[key]
        return SPEC if key == SPEC.symbol else replace(SPEC, symbol=key)

    def tick(self, symbol: str) -> Tick:
        self.calls.append("tick")
        bar = self.bar_series[-1]
        half = SPEC.point * SPEC.spread_points / 2.0
        return Tick(symbol=symbol, bid=bar.close - half, ask=bar.close + half,
                    server_time=bar.time - timedelta(seconds=self.quote_offset_seconds),
                    server_utc_offset_seconds=7200)

    def bars(self, symbol: str, timeframe: str, count: int) -> list[MarketBar]:
        self.calls.append("bars")
        return self.bar_series[-count:]

    def positions(self) -> list[PositionSnapshot]:
        self.calls.append("positions")
        return list(self.open_positions)

    def shutdown(self) -> None:
        return None


def agent_for(backend: FakeBackend, policy: AgentPolicy, journal=None) -> MT5Agent:
    return MT5Agent(backend=backend, policy=policy, journal=journal, clock=lambda: backend.clock)


# --------------------------------------------------------------------- policy
def test_live_mode_and_unknown_keys_are_rejected():
    with pytest.raises(ValueError, match="mode must be one of"):
        AgentPolicy.from_mapping(policy_raw(mode="live"))
    with pytest.raises(ValueError, match="unknown policy keys"):
        AgentPolicy.from_mapping({**policy_raw(), "leverage": 500})
    with pytest.raises(ValueError, match="unknown keys in policy section risk"):
        AgentPolicy.from_mapping(policy_raw(**{"risk__mood": "greedy"}))


def test_kill_switch_defaults_to_active_and_needs_a_reason():
    raw = policy_raw()
    raw.pop("kill_switch")
    assert AgentPolicy.from_mapping(raw).kill_switch_active is True
    with pytest.raises(ValueError, match="active kill switch requires a reason"):
        AgentPolicy.from_mapping(policy_raw(**{"kill_switch__active": True,
                                               "kill_switch__reason": "  "}))


def test_policy_rejects_implausible_numbers():
    with pytest.raises(ValueError, match="risk_per_trade_fraction"):
        make_policy(**{"risk__risk_per_trade_fraction": 0.5})
    with pytest.raises(ValueError, match="server_utc_offset_seconds"):
        make_policy(server_utc_offset_seconds=3601)
    with pytest.raises(ValueError, match="history_bars must be at least"):
        make_policy(history_bars=40)
    with pytest.raises(ValueError, match="sma_fast must be smaller"):
        make_policy(**{"strategy__sma_fast": 30})
    with pytest.raises(ValueError, match="symbol_spec.symbol must match"):
        make_policy(**{"symbol_spec__symbol": "GBPUSD"})


def test_policy_refuses_a_null_symbol_and_an_atr_window_shrunk_by_min_bars():
    raw = policy_raw()
    raw["symbol"] = None
    with pytest.raises(ValueError, match="symbol must be a string"):
        AgentPolicy.from_mapping(raw)
    # ATR would average only min_bars-1 ranges instead of the configured window.
    with pytest.raises(ValueError, match="atr_period must be smaller"):
        make_policy(**{"strategy__atr_period": 60, "strategy__min_bars": 60})


def test_a_symbol_spec_whose_tick_ratio_overflows_is_refused():
    """A positive but absurd tick_size makes tick_value/tick_size infinite, which would
    raise mid-cycle inside sizing instead of failing when the spec is declared."""
    with pytest.raises(ValueError, match="tick_value / tick_size"):
        SymbolSpec(**{**policy_raw()["symbol_spec"], "tick_size": 1e-320})


def test_shipped_example_policy_loads():
    path = Path(__file__).resolve().parents[1] / "config" / "mt5_agent.example.yaml"
    policy = load_policy(path)
    assert policy.mode == "paper"
    assert policy.require_demo is True
    assert policy.symbol_specification().volume_min == 0.01


def test_blackout_windows_are_parsed_and_enforced():
    start = "2026-11-03T12:00:00+00:00"
    end = "2026-11-03T13:30:00+00:00"
    policy = make_policy(**{"schedule__blackout_windows_utc": [[start, end]]})
    assert policy.blackout_blocker(datetime(2026, 11, 3, 12, 30, tzinfo=UTC)) == "BLACKOUT_WINDOW_1"
    assert policy.blackout_blocker(datetime(2026, 11, 3, 14, 0, tzinfo=UTC)) is None
    with pytest.raises(ValueError, match="timezone-aware"):
        make_policy(**{"schedule__blackout_windows_utc": [["2026-11-03T12:00:00", end]]})


# -------------------------------------------------------------------- backend
def test_symbol_spec_and_tick_reject_impossible_values():
    with pytest.raises(ValueError, match="volume_step cannot exceed volume_min"):
        SymbolSpec(symbol="EURUSD", point=1e-5, digits=5, tick_size=1e-5, tick_value=1.0,
                   volume_min=0.01, volume_max=100.0, volume_step=0.5, spread_points=1,
                   stops_level_points=0)
    with pytest.raises(ValueError, match="ask cannot be below bid"):
        Tick(symbol="EURUSD", bid=1.2, ask=1.1,
             server_time=datetime(2026, 1, 5, tzinfo=UTC), server_utc_offset_seconds=0)
    with pytest.raises(ValueError, match="tick server_time must be a UTC-aware"):
        Tick(symbol="EURUSD", bid=1.1, ask=1.2, server_time=datetime(2026, 1, 5),
             server_utc_offset_seconds=0)
    assert SPEC.price_to_volume(0.009) == 0.0
    assert SPEC.price_to_volume(0.0199) == 0.01
    assert SPEC.price_to_volume(1e9) == 100.0
    # The broker maximum need not be a whole multiple of the step; clip the step count.
    odd_max = SymbolSpec(symbol="EURUSD", point=1e-5, digits=5, tick_size=1e-5, tick_value=1.0,
                         volume_min=0.02, volume_max=0.75, volume_step=0.02, spread_points=1,
                         stops_level_points=0)
    assert odd_max.price_to_volume(1e9) == 0.74


def test_margin_scales_with_the_instrument_price():
    one = SPEC.margin_requirement(1.0, 100, 1.0)
    assert SPEC.margin_requirement(1.0, 100, 2.0) == pytest.approx(2 * one)
    # At an instrument price of 2000 the minimum lot alone needs more margin than the account
    # holds, even though the same lot at price 1.0 is easily affordable.
    denied = size(stop_distance=0.00002, price=2_000.0, margin_available=10_000.0)
    assert not denied.accepted and denied.denial == "MARGIN_MINIMUM_LOT_UNFUNDED"
    with pytest.raises(ValueError, match="price"):
        SPEC.margin_requirement(1.0, 100, 0.0)


def test_account_info_requires_known_mode_and_positive_equity():
    with pytest.raises(ValueError, match="unknown account trade mode"):
        AccountInfo(login=1, server="s", currency="USD", balance=1.0, equity=1.0,
                    margin_free=0.0, leverage=10, trade_mode="probably-real")
    with pytest.raises(ValueError, match="equity must be positive"):
        AccountInfo(login=1, server="s", currency="USD", balance=1.0, equity=0.0,
                    margin_free=0.0, leverage=10, trade_mode="demo")


# ------------------------------------------------------------------- strategy
def test_signal_requires_trend_and_respects_spread_and_stops_level():
    bars = make_bars()
    spec = SPEC
    up_tick = Tick(symbol="EURUSD", bid=bars[-1].close, ask=bars[-1].close + spec.point,
                   server_time=bars[-1].time, server_utc_offset_seconds=0)
    config = make_policy().strategy
    evaluation = evaluate_signal(bars, spec, up_tick, config, max_spread_points=25, expected_timeframe="H1")
    assert evaluation.has_proposal and evaluation.proposal.direction == "BUY"
    assert evaluation.proposal.confidence <= config.max_confidence
    assert "heuristic_score_not_calibrated_probability" in evaluation.proposal.reasons

    wide = Tick(symbol="EURUSD", bid=bars[-1].close, ask=bars[-1].close + spec.point * 400,
                server_time=bars[-1].time, server_utc_offset_seconds=0)
    assert "SPREAD_ABOVE_LIMIT" in evaluate_signal(bars, spec, wide, config,
                                                   max_spread_points=25, expected_timeframe="H1").blockers

    flat = make_bars(drift=1.0)
    flat_tick = Tick(symbol="EURUSD", bid=flat[-1].close, ask=flat[-1].close + spec.point,
                     server_time=flat[-1].time, server_utc_offset_seconds=0)
    assert "NO_TREND_SIGNAL" in evaluate_signal(flat, spec, flat_tick, config,
                                                max_spread_points=25, expected_timeframe="H1").blockers

    down = make_bars(drift=0.9991)
    down_tick = Tick(symbol="EURUSD", bid=down[-1].close, ask=down[-1].close + spec.point,
                     server_time=down[-1].time, server_utc_offset_seconds=0)
    short = evaluate_signal(down, spec, down_tick, config, max_spread_points=25, expected_timeframe="H1")
    assert short.proposal is not None and short.proposal.direction == "SELL"
    assert short.proposal.take_profit < short.proposal.entry_price < short.proposal.stop_loss

    stops_level_spec = SymbolSpec(**{**policy_raw()["symbol_spec"],
                                     "stops_level_points": 10_000})
    assert evaluate_signal(bars, stops_level_spec, up_tick, config,
                           max_spread_points=25, expected_timeframe="H1").blockers == ("STOP_INSIDE_BROKER_STOPS_LEVEL",)


def test_strategy_rejects_short_series_and_symbol_mix():
    config = make_policy().strategy
    bars = make_bars(20)
    tick = Tick(symbol="EURUSD", bid=bars[-1].close, ask=bars[-1].close + SPEC.point,
                server_time=bars[-1].time, server_utc_offset_seconds=0)
    assert evaluate_signal(bars, SPEC, tick, config, max_spread_points=25, expected_timeframe="H1").blockers \
        == ("INSUFFICIENT_BARS",)
    other = [MarketBar(**{**bar.__dict__, "symbol": "GBPUSD"}) for bar in make_bars(40)]
    assert evaluate_signal(other, SPEC, tick, config, max_spread_points=25, expected_timeframe="H1").blockers \
        == ("INSUFFICIENT_BARS",)


def test_strategy_refuses_a_stop_at_or_below_zero():
    """ATR on a cheap, volatile instrument can push the stop through zero while the trend
    itself is valid; an order with a non-positive stop is not a tradeable proposal."""
    start = datetime(2026, 1, 5, tzinfo=UTC)
    bars = []
    for index in range(61):
        close = 0.05 if index >= 56 else 0.005
        bars.append(MarketBar("EURUSD", "H1", start + timedelta(hours=index), close,
                              0.7 if index == 60 else close * 1.01,
                              close * 0.99, close))
    tick = Tick(symbol="EURUSD", bid=bars[-1].close, ask=bars[-1].close + SPEC.point,
                server_time=bars[-1].time, server_utc_offset_seconds=0)
    evaluation = evaluate_signal(bars, SPEC, tick, make_policy().strategy,
                                 max_spread_points=25, expected_timeframe="H1")
    assert evaluation.blockers == ("STOP_DISTANCE_INVALID",), evaluation.features


def test_a_bar_series_that_lags_the_quote_by_more_than_one_period_is_refused():
    """Every indicator would describe a market a whole bar old; that is a broken feed."""
    bars = make_bars()
    config = make_policy().strategy

    def tick_at(delta):
        return Tick(symbol="EURUSD", bid=bars[-1].close, ask=bars[-1].close + SPEC.point,
                    server_time=bars[-1].time + delta, server_utc_offset_seconds=0)

    # One period past the open is the normal close of the newest bar; two is the boundary.
    assert "BAR_TIME_TOO_OLD" not in evaluate_signal(
        bars, SPEC, tick_at(timedelta(hours=2)), config, max_spread_points=25, expected_timeframe="H1").blockers
    stale = evaluate_signal(bars, SPEC, tick_at(timedelta(hours=2, seconds=1)), config,
                            max_spread_points=25, expected_timeframe="H1")
    assert stale.blockers == ("BAR_TIME_TOO_OLD",), stale.features
    # A stale feed is a data-integrity failure, so the cycle is BLOCKED, not "no signal".
    assert stale.data_integrity_failure is True


def test_strategy_refuses_a_series_on_another_timeframe():
    """Indicators, the stale-bar horizon and the journal label all derive from the bar label,
    so an H1 series served to an M5 policy would time the signal against the wrong market."""
    bars = make_bars()
    tick = Tick(symbol="EURUSD", bid=bars[-1].close, ask=bars[-1].close + SPEC.point,
                server_time=bars[-1].time, server_utc_offset_seconds=0)
    evaluation = evaluate_signal(bars, SPEC, tick, make_policy().strategy,
                                 max_spread_points=25, expected_timeframe="M5")
    assert evaluation.blockers == ("BAR_TIMEFRAME_MISMATCH",)
    assert evaluation.data_integrity_failure


def test_the_agent_blocks_when_the_backend_serves_another_timeframe():
    result = agent_for(FakeBackend(), make_policy(timeframe="M5")).run_cycle()
    assert result.blockers == ("BAR_TIMEFRAME_MISMATCH",), result.blockers
    assert result.status == STATUS_BLOCKED


class _BackendStuckOnAnotherSymbol(FakeBackend):
    """Serves GBPUSD whatever symbol the policy asks for, like a frozen terminal."""

    def __init__(self) -> None:
        super().__init__()
        self._spec = replace(SPEC, symbol="GBPUSD")
        self._bars = make_bars(symbol="GBPUSD")

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return self._spec

    def tick(self, symbol: str) -> Tick:
        bar = self._bars[-1]
        half = self._spec.point * self._spec.spread_points / 2.0
        return Tick(symbol="GBPUSD", bid=bar.close - half, ask=bar.close + half,
                    server_time=bar.time, server_utc_offset_seconds=7200)

    def bars(self, symbol: str, timeframe: str, count: int) -> list[MarketBar]:
        return self._bars[-count:]


def test_the_agent_blocks_when_the_backend_serves_another_symbol():
    """Sizing and margin come from the spec; a spec for the wrong instrument would open
    a EURUSD proposal against GBPUSD prices and journal it under the policy symbol."""
    result = agent_for(_BackendStuckOnAnotherSymbol(), make_policy()).run_cycle()
    assert result.blockers == ("BAR_SYMBOL_MISMATCH",), result.blockers
    assert result.status == STATUS_BLOCKED


# --------------------------------------------------------------------- sizing
def size(**kwargs) -> Any:
    defaults = {"spec": SPEC, "equity": 10_000.0, "risk_fraction_limit": 0.005,
                "stop_distance": 0.003, "leverage": 100, "margin_available": 10_000.0,
                "price": 1.10}
    return size_position(**{**defaults, **kwargs})


def test_sizing_never_exceeds_the_risk_budget():
    sized = size()
    assert sized.accepted and sized.order is not None
    assert sized.order.risk_fraction <= 0.005
    assert abs(sized.order.volume / SPEC.volume_step
               - round(sized.order.volume / SPEC.volume_step)) < 1e-9


def test_sizing_refuses_when_the_minimum_lot_already_breaks_the_budget():
    denied = size(stop_distance=0.09)
    assert not denied.accepted and denied.denial == "MINIMUM_LOT_EXCEEDS_RISK_BUDGET"
    with pytest.raises(ValueError, match="stop_distance"):
        size(stop_distance=0.0)


def test_sizing_caps_volume_by_margin_and_not_only_by_risk():
    """A very tight stop makes the risk budget imply a lot the account cannot fund."""
    risk_only = size(stop_distance=0.00002, margin_available=1e12)
    assert risk_only.accepted
    assert risk_only.order.volume == pytest.approx(25.0)
    # The same tight stop with the account's real free margin: notional at this price blocks
    # more than the risk budget would allow, so the margin cap must decide the lot.
    margin_capped = size(stop_distance=0.00002, margin_available=10_000.0)
    assert margin_capped.accepted
    assert margin_capped.order.volume < risk_only.order.volume
    assert margin_capped.order.margin_required <= 10_000.0 + 1e-9
    assert margin_capped.order.risk_value <= 50.0 + 1e-9

    thin_margin = size(stop_distance=0.00002, margin_available=1.0)
    assert not thin_margin.accepted
    assert thin_margin.denial == "MARGIN_MINIMUM_LOT_UNFUNDED"


def test_sizing_denials_distinguish_margin_from_risk():
    assert size(stop_distance=0.09).denial == "MINIMUM_LOT_EXCEEDS_RISK_BUDGET"
    assert size(margin_available=-5.0).denial == "MARGIN_MINIMUM_LOT_UNFUNDED"
    with pytest.raises(ValueError, match="margin_available"):
        size(margin_available=float("nan"))


# --------------------------------------------------------------- paper broker
def paper_broker(**kwargs) -> PaperBroker:
    bars = kwargs.pop("bars", make_bars())
    return PaperBroker(bars={"EURUSD": bars}, spec=SPEC, server_utc_offset_seconds=0, **kwargs)


def test_paper_broker_hides_future_bars():
    bars = make_bars()
    broker = paper_broker(bars=bars)
    broker.seek(bars[50].time)
    visible = broker.bars("EURUSD", "H1", 500)
    assert visible[-1].time == bars[50].time
    assert len(visible) == 51
    with pytest.raises(ValueError, match="no bars for"):
        broker.bars("XAUUSD", "H1", 10)


def test_paper_broker_fills_stop_before_target_in_the_same_bar():
    bars = make_bars(60)
    entry_broker = paper_broker(bars=bars)
    entry_broker.seek(bars[-1].time)
    entry = entry_broker.tick("EURUSD").ask
    # A later bar whose range covers both levels must be reported as a losing stop-out.
    spike = MarketBar(symbol="EURUSD", timeframe="H1", time=bars[-1].time + timedelta(hours=1),
                      open=entry, high=entry + 0.01, low=entry - 0.01, close=entry,
                      tick_volume=1, spread_points=10)
    broker = paper_broker(bars=[*bars, spike])
    broker.seek(bars[-1].time)
    broker.open_position(direction="BUY", volume=0.10, entry_price=entry,
                         stop_loss=entry - 0.001, take_profit=entry + 0.001)
    assert broker.trades == []          # the spike is still in the future at open time
    broker.seek(spike.time)
    assert broker.trades[-1].exit_reason == "STOP_LOSS"
    assert broker.trades[-1].pnl < 0
    assert entry_broker.trades == []


def test_a_bar_without_a_spread_uses_the_symbol_spread():
    """A CSV export without a Spread column yields None, which must not crash the broker."""
    bars = [replace(bar, spread_points=None) for bar in make_bars(60)]
    broker = paper_broker(bars=bars)
    broker.seek(bars[-1].time)
    tick = broker.tick("EURUSD")
    assert tick.ask - tick.bid == pytest.approx(SPEC.point * SPEC.spread_points)


def test_a_zero_spread_bar_falls_back_to_the_symbol_spread():
    """A zero in the Spread column is a missing value, not a zero-cost broker."""
    bars = [replace(bar, spread_points=0) for bar in make_bars(60)]
    broker = paper_broker(bars=bars)
    broker.seek(bars[-1].time)
    tick = broker.tick("EURUSD")
    assert tick.ask - tick.bid == pytest.approx(SPEC.point * SPEC.spread_points)


def test_a_fill_keeps_the_requested_stop_and_target():
    bars = make_bars(60)
    broker = paper_broker(bars=bars)
    broker.seek(bars[-1].time)
    entry = broker.tick("EURUSD").ask
    broker.open_position(direction="BUY", volume=0.10, entry_price=entry,
                         stop_loss=entry - 0.001, take_profit=entry + 0.001)
    position = broker.positions()[0]
    assert position.stop_loss == pytest.approx(entry - 0.001)
    assert position.take_profit == pytest.approx(entry + 0.001)


def test_paper_broker_rejects_bad_geometry_and_undersized_volume():
    bars = make_bars(60)
    broker = paper_broker(bars=bars)
    broker.seek(bars[-1].time)
    entry = broker.tick("EURUSD").ask
    with pytest.raises(ValueError, match="BUY geometry"):
        broker.open_position(direction="BUY", volume=0.1, entry_price=entry,
                             stop_loss=entry + 0.001, take_profit=entry - 0.001)
    with pytest.raises(ValueError, match="below the broker minimum lot"):
        broker.open_position(direction="BUY", volume=0.001, entry_price=entry,
                             stop_loss=entry - 0.001, take_profit=entry + 0.001)
    with pytest.raises(ValueError, match="maximum lot"):
        broker.open_position(direction="BUY", volume=101.0, entry_price=entry,
                             stop_loss=entry - 0.001, take_profit=entry + 0.001)
    with pytest.raises(ValueError, match="multiple of the broker step"):
        broker.open_position(direction="BUY", volume=0.105, entry_price=entry,
                             stop_loss=entry - 0.001, take_profit=entry + 0.001)
    with pytest.raises(ValueError, match="stop loss must be positive"):
        broker.open_position(direction="BUY", volume=0.1, entry_price=entry,
                             stop_loss=0.0, take_profit=entry + 0.001)


def test_the_paper_book_refuses_a_lot_it_cannot_fund():
    """Margin is price-aware: ten lots need ~11,000 against a 10,000 balance."""
    bars = make_bars(60)
    broker = paper_broker(bars=bars)
    broker.seek(bars[-1].time)
    entry = broker.tick("EURUSD").ask
    with pytest.raises(ValueError, match="more margin than the account has free"):
        broker.open_position(direction="BUY", volume=10.0, entry_price=entry,
                             stop_loss=entry - 0.001, take_profit=entry + 0.001)
    assert broker.positions() == []


# ------------------------------------------------------------------- live host
class _StubNamespace:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def install_stub_mt5(monkeypatch, *, initialize_ok=True, account=None, symbol=None,
                     tick=None, rates=(), positions=()):
    """Fake the MetaTrader5 package so the live adapter contract is testable on this host."""
    calls: list[str] = []
    module = _StubNamespace(
        TIMEFRAME_H1=2,
        initialize=lambda **kwargs: (calls.append("initialize"), initialize_ok)[1],
        last_error=lambda: (calls.append("last_error"), (-10000, "secret host detail"))[1],
        shutdown=lambda: calls.append("shutdown"),
        account_info=lambda: (calls.append("account_info"),
                              account if account is not None else _StubNamespace(
                                  login=77, server="Broker-Demo", currency="USD",
                                  balance=10_000.0, equity=10_000.0, margin_free=9_000.0,
                                  leverage=100, trade_mode=0))[1],
        symbol_select=lambda name, enable: (calls.append(f"symbol_select:{name}"), True)[1],
        symbol_info=lambda name: (calls.append("symbol_info"),
                                  symbol if symbol is not None else _StubNamespace(
                                      name="EURUSD", currency_profit="USD",
                                      point=1e-5, digits=5, trade_tick_size=1e-5,
                                      trade_tick_value_loss=1.0, trade_tick_value=1.0,
                                      volume_min=0.01, volume_max=100.0, volume_step=0.01,
                                      spread=12, trade_stops_level=10))[1],
        symbol_info_tick=lambda name: (calls.append("symbol_info_tick"),
                                       tick if tick is not None else _StubNamespace(
                                           bid=1.2, ask=1.20012, time=SERVER_EPOCH))[1],
        copy_rates_from_pos=lambda name, tf, shift, count: (
            calls.append("copy_rates_from_pos"), list(rates))[1],
        positions_get=lambda **kwargs: (calls.append("positions_get"), list(positions))[1],
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", module)
    return calls


def test_live_backend_maps_terminal_state_and_exposes_no_orders(monkeypatch):
    from critical_consensus.mt5_agent.backend import LiveMT5Backend

    rates = [{"time": SERVER_EPOCH - (119 - i) * 3600, "open": 1.2 + i * 1e-5,
              "high": 1.2 + i * 1e-5 + 5e-5, "low": 1.2 + i * 1e-5 - 5e-5,
              "close": 1.2 + i * 1e-5 + 1e-5, "tick_volume": 400 - i, "spread": 12}
             for i in range(120)]
    calls = install_stub_mt5(monkeypatch, rates=rates, positions=[_StubNamespace(
        ticket=5, symbol="EURUSD", type=1, volume=0.2, price_open=1.2001, sl=1.19,
        tp=1.21, time=SERVER_EPOCH)])

    backend = LiveMT5Backend(server_utc_offset_seconds=7200)
    account = backend.account()
    assert (account.login, account.server, account.trade_mode, account.is_demo) == \
        (77, "Broker-Demo", "demo", True)

    spec = backend.symbol_spec("eurusd")
    assert (spec.symbol, spec.volume_min, spec.spread_points, spec.stops_level_points) == \
        ("EURUSD", 0.01, 12, 10)

    tick = backend.tick("EURUSD")
    assert tick.bid < tick.ask and tick.reference["BUY"] == tick.ask
    # A UTC+2 server stamp must be converted to real UTC before any freshness check.
    assert tick.server_time == datetime.fromtimestamp(SERVER_EPOCH - 7200, tz=UTC)

    bars = backend.bars("EURUSD", "H1", 120)
    assert len(bars) == 120
    assert bars[0].time == datetime.fromtimestamp(SERVER_EPOCH - 119 * 3600 - 7200, tz=UTC)
    assert bars[-1].time == datetime.fromtimestamp(SERVER_EPOCH - 7200, tz=UTC)
    assert bars[0].source == "MetaTrader5.copy_rates_from_pos"

    positions = backend.positions()
    assert positions[0].direction == "SELL" and positions[0].ticket == 5
    backend.shutdown()
    assert {"initialize", "account_info", "copy_rates_from_pos", "positions_get",
            "shutdown"} <= set(calls)
    assert not [name for name in dir(backend)
                if any(word in name.lower() for word in
                       ("order", "trade", "send", "place", "cancel", "modify", "deal"))]


def test_live_backend_fails_closed_on_terminal_problems(monkeypatch):
    from critical_consensus.mt5_agent.backend import LiveMT5Backend

    calls = install_stub_mt5(monkeypatch, initialize_ok=False)
    with pytest.raises(RuntimeError, match="MT5 initialize failed") as excinfo:
        LiveMT5Backend(server_utc_offset_seconds=0)
    # last_error() text can echo connection details, so only the numeric code is re-raised.
    assert "secret host detail" not in str(excinfo.value)
    assert "code=-10000" in str(excinfo.value)
    assert "initialize" in calls

    monkeypatch.setitem(sys.modules, "MetaTrader5", _StubNamespace(
        initialize=lambda **kwargs: True, account_info=lambda: None, shutdown=lambda: None))
    backend = LiveMT5Backend(server_utc_offset_seconds=0)
    with pytest.raises(RuntimeError, match="no account"):
        backend.account()

    def missing_package(name):
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", missing_package)
    with pytest.raises(RuntimeError, match="MetaTrader5 Python package is required"):
        LiveMT5Backend(server_utc_offset_seconds=0)


def test_live_backend_credentials_are_all_or_nothing(monkeypatch):
    from critical_consensus.mt5_agent.backend import LiveMT5Backend

    install_stub_mt5(monkeypatch)
    with pytest.raises(ValueError, match="provided together"):
        LiveMT5Backend(server_utc_offset_seconds=0, login=77)
    with pytest.raises(ValueError, match="password must be non-empty"):
        LiveMT5Backend(server_utc_offset_seconds=0, login=77, password="  ",
                       server="Broker-Demo")
    with pytest.raises(ValueError, match="server_utc_offset_seconds"):
        LiveMT5Backend(server_utc_offset_seconds=45)


# ---------------------------------------------------------------------- agent
def test_kill_switch_blocks_before_any_market_read():
    backend = FakeBackend()
    agent = agent_for(backend, make_policy(**{"kill_switch__active": True,
                                              "kill_switch__reason": "halt"}))
    result = agent.run_cycle()
    assert result.status == STATUS_BLOCKED
    assert result.blockers == ("KILL_SWITCH_ACTIVE",)
    assert backend.calls == []


def test_real_account_or_unpinned_identity_blocks():
    backend = FakeBackend()
    backend.account_info = AccountInfo(login=42, server="Live-Server", currency="USD",
                                       balance=10_000.0, equity=10_000.0, margin_free=10_000.0,
                                       leverage=100, trade_mode="real")
    result = agent_for(backend, make_policy()).run_cycle()
    assert result.status == STATUS_BLOCKED and "ACCOUNT_NOT_DEMO" in result.blockers

    backend.account_info = AccountInfo(login=7, server="Demo-Server", currency="USD",
                                       balance=10_000.0, equity=10_000.0, margin_free=10_000.0,
                                       leverage=100, trade_mode="demo")
    result = agent_for(backend, make_policy(**{"account__expected_login": 42})).run_cycle()
    assert "ACCOUNT_LOGIN_MISMATCH" in result.blockers


def test_missing_equity_baselines_and_stale_quotes_block():
    backend = FakeBackend()
    backend.day_start_equity = None
    backend.peak_equity = None
    result = agent_for(backend, make_policy()).run_cycle()
    assert "EQUITY_BASELINES_UNAVAILABLE" in result.blockers

    backend = FakeBackend(peak_equity=0.0)
    assert "EQUITY_BASELINES_UNAVAILABLE" in agent_for(backend, make_policy()).run_cycle().blockers

    backend = FakeBackend(quote_offset_seconds=120)
    result = agent_for(backend, make_policy()).run_cycle()
    assert "QUOTE_STALE" in result.blockers

    backend = FakeBackend(quote_offset_seconds=-120)
    result = agent_for(backend, make_policy()).run_cycle()
    assert "QUOTE_FROM_THE_FUTURE" in result.blockers


@pytest.mark.parametrize("broken", [None, "10", True, float("nan"), float("inf")])
def test_a_present_but_unusable_daily_pnl_blocks_the_cycle(broken):
    """A backend that answers ``None``/junk for today's P&L is broken data, not a flat day."""
    backend = FakeBackend(realized_daily_pnl=broken)
    result = agent_for(backend, make_policy()).run_cycle()
    assert result.blockers == ("EQUITY_BASELINES_UNAVAILABLE",)
    assert result.status == STATUS_BLOCKED


class _BackendWithoutDailyPnl:
    """Delegates every call but never exposes ``realized_daily_pnl``."""

    def __init__(self, inner: FakeBackend) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name == "realized_daily_pnl":
            raise AttributeError(name)
        return getattr(self._inner, name)


class _BackendWithRaisingDailyPnl:
    """Exposes the property but its getter fails, as a broken day ledger would."""

    def __init__(self, inner: FakeBackend) -> None:
        self._inner = inner

    @property
    def realized_daily_pnl(self) -> float:
        raise AttributeError("day ledger unavailable")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.mark.parametrize("wrapper", [_BackendWithoutDailyPnl, _BackendWithRaisingDailyPnl])
def test_an_unreportable_daily_pnl_blocks_the_cycle(wrapper):
    """The protocol requires today's banked P&L: an unknown day is never treated as flat.

    Plain ``getattr(obj, name, default)`` would swallow an ``AttributeError`` raised inside
    a property and read it as absence; static lookup finds the attribute first, so a broken
    getter blocks like a missing one instead of silently zeroing the daily-loss term.
    """
    result = agent_for(wrapper(FakeBackend()), make_policy(mode="observe")).run_cycle()
    assert result.blockers == ("EQUITY_BASELINES_UNAVAILABLE",)
    assert result.status == STATUS_BLOCKED


def test_default_policy_window_and_weekend_are_closed():
    raw = policy_raw()
    raw.pop("schedule")
    policy = AgentPolicy.from_mapping(raw)
    late = FakeBackend(clock=datetime(2026, 1, 9, 23, 0, tzinfo=UTC))
    assert agent_for(late, policy).run_cycle().blockers == ("OUTSIDE_TRADING_WINDOW",)
    sunday = FakeBackend(clock=datetime(2026, 1, 11, 10, 0, tzinfo=UTC))
    assert agent_for(sunday, policy).run_cycle().blockers == ("OUTSIDE_TRADING_WINDOW",)


def test_position_cap_and_daily_loss_limits_block():
    backend = FakeBackend()
    agent_for(backend, make_policy()).run_cycle()
    backend.open_positions = [PositionSnapshot(ticket=9, symbol="EURUSD", direction="BUY",
                                               volume=0.1, entry_price=1.2, stop_loss=1.19,
                                               take_profit=1.25,
                                               open_time=datetime(2026, 1, 10, tzinfo=UTC))]
    result = agent_for(backend, make_policy()).run_cycle()
    assert "POSITION_CAP_REACHED" in result.blockers

    backend.open_positions = []
    backend.day_start_equity = 10_000.0
    backend.account_info = AccountInfo(login=42, server="Demo-Server", currency="USD",
                                       balance=9_700.0, equity=9_700.0, margin_free=9_700.0,
                                       leverage=100, trade_mode="demo")
    result = agent_for(backend, make_policy()).run_cycle()
    assert "DAILY_LOSS_LIMIT_REACHED" in result.blockers


def test_observe_mode_records_signal_without_a_position(tmp_path):
    backend = FakeBackend()
    journal = DecisionJournal(tmp_path / "journal.db")
    agent = agent_for(backend, make_policy(mode="observe"), journal)
    result = agent.run_cycle()
    assert result.status == STATUS_OBSERVED
    assert result.proposal is not None and result.ticket is None
    assert result.broker_order_sent is False
    event = journal.events(event_type="agent_cycle")[-1]
    assert event["payload"]["broker_order_sent"] is False
    assert event["payload"]["proposal"]["direction"] == "BUY"
    journal.close()


def test_paper_mode_opens_a_capped_position_and_journals_the_chain(tmp_path):
    bars = make_bars()
    broker = paper_broker(bars=bars)
    journal = DecisionJournal(tmp_path / "journal.db")
    agent = MT5Agent(backend=broker, policy=make_policy(), journal=journal, clock=broker.now)
    broker.seek(bars[60].time)
    first = agent.run_cycle()
    assert first.status == STATUS_PAPER_OPENED and first.ticket == 1
    assert first.sizing["risk_fraction"] <= 0.005
    # The success path must prove both journal rows exist, not only that a trade happened.
    assert first.cycle_recorded is True and first.equity_recorded is True
    events = journal.events(event_type="agent_cycle")
    assert events[-1]["event_hash"] == first.decision_hash
    assert broker.positions()[0].entry_price == pytest.approx(
        events[-1]["payload"]["requote"]["fill_price"])
    second = agent.run_cycle()
    assert second.status == STATUS_BLOCKED
    assert {"POSITION_CAP_REACHED", "SYMBOL_ALREADY_OPEN"} <= set(second.blockers)
    assert broker.equity() > 0
    report = journal.report(initial_equity=10_000.0)
    assert report["verified_chain"]["valid"] is True
    assert report["cycles"] == 2
    journal.close()


class _ShiftingPaper(PaperBroker):
    """Paper quote that moves between the signal read and the post-approval requote."""

    def __init__(self, *, shift: float = 0.00010, **kwargs) -> None:
        super().__init__(**kwargs)
        self.shift = shift
        self.tick_calls = 0

    def tick(self, symbol: str) -> Tick:
        tick = super().tick(symbol)
        self.tick_calls += 1
        if self.tick_calls < 2:
            return tick
        return Tick(symbol=tick.symbol, bid=tick.bid + self.shift, ask=tick.ask + self.shift,
                    server_time=tick.server_time,
                    server_utc_offset_seconds=tick.server_utc_offset_seconds)


def test_confirm_mode_fills_at_the_requoted_price_not_the_proposal(tmp_path):
    bars = make_bars()
    broker = _ShiftingPaper(bars={"EURUSD": bars}, spec=SPEC, server_utc_offset_seconds=0)
    broker.seek(bars[-1].time)
    journal = DecisionJournal(tmp_path / "journal.db")
    policy = make_policy(execution_mode="confirm")
    agent = MT5Agent(backend=broker, policy=policy, journal=journal, clock=broker.now,
                     approver=lambda proposal, sizing: True)
    result = agent.run_cycle()
    assert result.status == STATUS_PAPER_OPENED, result.blockers
    event = journal.events(event_type="agent_cycle")[-1]["payload"]
    assert event["requote"]["slippage"] == pytest.approx(0.00010)
    assert broker.positions()[0].entry_price == pytest.approx(
        event["proposal"]["entry_price"] + 0.00010)
    journal.close()


def test_open_stop_risk_and_banked_loss_share_the_daily_budget():
    position = PositionSnapshot(ticket=9, symbol="GBPUSD", direction="BUY", volume=10.0,
                                entry_price=1.2, stop_loss=1.19982, take_profit=1.21,
                                open_time=datetime(2026, 1, 10, tzinfo=UTC))
    policy = make_policy(**{"risk__max_open_positions": 2})
    backend = FakeBackend(open_positions=[position])
    # 0.00018 / 1e-5 * 1 * 10 = 180 of the 200 budget is already at risk; a new ~50 does not fit.
    result = agent_for(backend, policy).run_cycle()
    assert result.blockers == ("AGGREGATE_STOP_RISK_LIMIT_REACHED",), result.blockers

    # The same open risk plus 30 already banked today breaches the floor before sizing.
    backend.realized_daily_pnl = -30.0
    result = agent_for(backend, policy).run_cycle()
    assert result.blockers == ("LOSS_FLOOR_BREACHED",), result.blockers


def test_an_open_position_measured_with_another_instruments_spec_blocks():
    """A spec for a different instrument has another tick size and tick value, so open stop
    risk and the daily budget would be computed for the wrong contract."""
    position = PositionSnapshot(ticket=9, symbol="GBPUSD", direction="BUY", volume=10.0,
                                entry_price=1.2, stop_loss=1.19982, take_profit=1.21,
                                open_time=datetime(2026, 1, 10, tzinfo=UTC))
    backend = FakeBackend(open_positions=[position])
    backend.symbol_specs["GBPUSD"] = SPEC  # SPEC is EURUSD, despite the GBPUSD lookup
    result = agent_for(backend, make_policy(**{"risk__max_open_positions": 2})).run_cycle()
    assert result.blockers == ("MARKET_DATA_READ_FAILED",), result.blockers


def test_bound_fill_refuses_a_fill_sitting_exactly_on_the_stop():
    bars = make_bars()
    tick = Tick(symbol="EURUSD", bid=bars[-1].close, ask=bars[-1].close + SPEC.point,
                server_time=bars[-1].time, server_utc_offset_seconds=0)
    evaluation = evaluate_signal(bars, SPEC, tick, make_policy().strategy,
                                 max_spread_points=25, expected_timeframe="H1")
    proposal = evaluation.proposal
    assert proposal is not None and proposal.direction == "BUY"
    order = size(stop_distance=proposal.risk_distance, price=proposal.entry_price).order
    assert order is not None
    flat = Tick(symbol="EURUSD", bid=proposal.stop_loss, ask=proposal.stop_loss,
                server_time=bars[-1].time, server_utc_offset_seconds=0)
    agent = MT5Agent(backend=FakeBackend(), policy=make_policy(),
                     clock=lambda: bars[-1].time)
    blockers, final, info = agent._bound_fill(
        proposal=proposal, approved=order, tick=flat, spec=SPEC, risk_equity=10_000.0,
        leverage=100, margin_available=10_000.0, open_stop_risk=0.0, daily_budget=200.0)
    assert blockers == ("PRICE_MOVED_DURING_CONFIRMATION",)
    assert final is None and info["stop_distance_at_fill"] == 0.0


def test_journal_detects_tampering(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    journal.record("agent_cycle", {"status": "OBSERVED"})
    journal.record("agent_cycle", {"status": "BLOCKED", "blockers": ["QUOTE_STALE"]})
    assert journal.verify_chain()["valid"] is True
    with sqlite3.connect(tmp_path / "journal.db") as connection:
        connection.execute("UPDATE audit_events SET payload = '{\"status\":\"WIN\"}' "
                           "WHERE seq = 1")
    status = journal.verify_chain()
    assert status["valid"] is False
    assert status["index"] == 0
    assert status["reason"] == "EVENT_CONTENT_HASH_MISMATCH"
    journal.close()


def test_journal_detects_a_row_deleted_from_the_middle_of_a_run(tmp_path):
    """The next row's previous_hash no longer names the row that is actually before it."""
    journal = DecisionJournal(tmp_path / "journal.db")
    for status in ("OBSERVED", "BLOCKED", "NO_SIGNAL"):
        journal.record("agent_cycle", {"status": status})
    with sqlite3.connect(tmp_path / "journal.db") as connection:
        connection.execute("DELETE FROM audit_events WHERE seq = 2")
    status = journal.verify_chain()
    assert status["valid"] is False
    assert status["reason"] == "PREVIOUS_HASH_MISMATCH"
    journal.close()


def test_journal_reports_an_equity_point_without_a_second_unverified_table(tmp_path):
    """Equity lives in the chain: an edited drawdown row is a broken hash, not a statistic."""
    journal = DecisionJournal(tmp_path / "journal.db", run_id="run-a")
    moment = datetime(2026, 1, 5, 12, tzinfo=UTC)
    journal.record_equity(equity=10_000.0, balance=10_000.0, open_positions=0, at=moment)
    journal.record_equity(equity=9_400.0, balance=9_400.0, open_positions=0, at=moment)
    assert journal.equity_curve() == [10_000.0, 9_400.0]
    assert journal.report(initial_equity=10_000.0)["max_drawdown_account_currency"] == 600.0
    with sqlite3.connect(tmp_path / "journal.db") as connection:
        connection.execute("UPDATE audit_events SET payload = "
                           "REPLACE(payload, '9400', '9990') WHERE event_type = 'equity_point'")
    assert journal.verify_chain()["valid"] is False
    journal.close()


def test_journal_anchor_detects_a_deleted_run_prefix(tmp_path):
    """An anchor kept from run 1 catches a run-2 event removed from in front of the survivors.

    Without the anchor the file stays internally consistent, which is exactly why the
    operator has to keep one.
    """
    path = tmp_path / "journal.db"
    first = DecisionJournal(path, run_id="run-1")
    first.record("agent_cycle", {"status": "BLOCKED"})
    first.record("agent_cycle", {"status": "BLOCKED"})
    anchor = first.last_hash()
    first.close()

    second = DecisionJournal(path, run_id="run-2")
    second.record("agent_cycle", {"status": "OBSERVED"})
    second.record("agent_cycle", {"status": "OBSERVED"})
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM audit_events WHERE run_id = 'run-2' AND seq ="
                           " (SELECT MIN(seq) FROM audit_events WHERE run_id = 'run-2')")
    assert second.chain_status(run_id="run-2")["valid"] is True  # internally consistent
    checked = second.chain_status(run_id="run-2", anchor=anchor)
    assert checked["valid"] is False
    assert checked["reason"] == "PREVIOUS_HASH_MISMATCH"
    second.close()


def test_a_kept_anchor_rejects_an_emptied_journal(tmp_path):
    """Deleting every row is the strongest prefix deletion; an empty chain cannot match."""
    journal = DecisionJournal(tmp_path / "journal.db")
    journal.record("agent_cycle", {"status": "BLOCKED"})
    anchor = journal.last_hash()
    journal.reset()
    status = journal.chain_status(anchor=anchor)
    assert status["valid"] is False
    assert status["reason"] == "PREVIOUS_HASH_MISMATCH"
    assert status["events"] == 0
    journal.close()


def test_journal_run_id_confines_a_report_to_one_run(tmp_path):
    path = tmp_path / "journal.db"
    first = DecisionJournal(path, run_id="run-1")
    first.record("agent_cycle", {"status": "OBSERVED", "blockers": []})
    first.close()
    second = DecisionJournal(path, run_id="run-2")
    second.record("agent_cycle", {"status": "BLOCKED", "blockers": ["QUOTE_STALE"]})
    assert second.report(initial_equity=10_000.0)["cycles"] == 1
    assert second.report(initial_equity=10_000.0, run_id="run-1")["cycles"] == 1
    assert second.run_id == "run-2"
    second.close()


def test_agent_never_reports_a_broker_order(tmp_path):
    bars = make_bars()
    broker = paper_broker(bars=bars)
    journal = DecisionJournal(tmp_path / "journal.db")
    agent = MT5Agent(backend=broker, policy=make_policy(), journal=journal, clock=broker.now)
    for bar in bars[60:]:
        broker.seek(bar.time)
        agent.run_cycle()
    broker.seek(bars[-1].time)
    closed = agent.close_positions("END_OF_RUN")
    events = journal.events(limit=5_000)
    assert events
    # Every record the agent writes states this outright, not just the cycle events.
    for event in events:
        assert event["payload"].get("broker_order_sent") is False, event["event_type"]
        assert event["payload"].get("execution_mode") == "auto", event["event_type"]
    assert closed and all(trade["exit_reason"] == "END_OF_RUN" for trade in closed)
    assert broker.trades
    journal.close()


# ------------------------------------------------------------------------ CLI
def test_cli_run_produces_a_paper_report(tmp_path):
    csv_path = tmp_path / "eurusd_h1.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Date", "Time", "Open", "High", "Low", "Close", "TickVol", "Volume",
                         "Spread"])
        for bar in make_bars(150):
            writer.writerow([bar.time.strftime("%Y.%m.%d"), bar.time.strftime("%H:%M"),
                             f"{bar.open:.5f}", f"{bar.high:.5f}", f"{bar.low:.5f}",
                             f"{bar.close:.5f}", bar.tick_volume, 0, bar.spread_points])
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "version: 1\nmode: paper\nsymbol: EURUSD\n"
        "timeframe: H1\nhistory_bars: 120\nserver_utc_offset_seconds: 0\n"
        "execution_mode: auto\n"
        "kill_switch: {active: false, reason: cli test}\n"
        "schedule: {blocked_weekdays_utc: [], window_start_minute_utc: 0,\n"
        "  window_end_minute_utc: 1440}\n"
        "symbol_spec: {symbol: EURUSD, point: 0.00001, digits: 5, tick_size: 0.00001,\n"
        "  tick_value: 1.0, volume_min: 0.01, volume_max: 100.0, volume_step: 0.01,\n"
        "  spread_points: 10, stops_level_points: 0}\n",
        encoding="utf-8")
    journal_db = tmp_path / "journal.db"
    base = [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"), "--db", str(journal_db)]
    unconfirmed = subprocess.run([*base, "run", "--policy", str(policy_path),
                                  "--csv", str(csv_path), "--fresh"],
                                 cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", check=False, env=cli_env())
    assert unconfirmed.returncode == 2
    assert "--confirm" in unconfirmed.stderr
    assert not journal_db.exists()

    run = subprocess.run([*base, "run", "--policy", str(policy_path), "--csv", str(csv_path),
                          "--source-timezone", "UTC", "--fresh", "--confirm",
                          "--run-id", "cli-run", "--limit-cycles", "90"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", check=False, env=cli_env())
    assert run.returncode == 0, run.stderr
    assert '"broker_order_sent": false' in run.stdout
    summary = json.loads(run.stdout[:run.stdout.rindex("}") + 1])
    assert summary["cycles"] == 90
    assert summary["run_id"] == "cli-run"
    assert summary["audit_chain_valid"] is True
    # Every simulated fill, including intrabar stops, is in the journal the cycle it happened.
    assert summary["paper_trades_closed"] >= 1
    assert summary["paper_trades_journaled"] == summary["paper_trades_closed"]

    scoped = subprocess.run([*base, "report", "--run-id", "cli-run"], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            check=False, env=cli_env())
    assert json.loads(scoped.stdout)["cycles"] == 90
    forged = subprocess.run([*base, "report", "--run-id", "cli-run", "--verify-anchor",
                             "sha256:" + "0" * 64], cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False, env=cli_env())
    assert forged.returncode == 3
    # stdout stays exactly one JSON object even when the anchor check fails; the reason code
    # lives there, while stderr carries the localized prose for the operator.
    forged_report = json.loads(forged.stdout)
    assert forged_report["anchor_check"]["valid"] is False
    assert forged_report["anchor_check"]["reason"] == "PREVIOUS_HASH_MISMATCH"
    assert "PREVIOUS_HASH_MISMATCH" not in forged.stderr
    assert "не совпадает" in forged.stderr

    # A content edit is caught by the self-anchored check too, and the operator is told on stderr.
    with sqlite3.connect(journal_db) as connection:
        connection.execute("UPDATE audit_events SET recorded_at = "
                           "'2026-01-01T00:00:00+00:00' WHERE seq = 1")
    tampered = subprocess.run([*base, "report", "--run-id", "cli-run"], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False, env=cli_env())
    assert tampered.returncode == 0
    assert json.loads(tampered.stdout)["verified_chain"]["valid"] is False
    assert "не прошла внутреннюю" in tampered.stderr

    refused = subprocess.run([*base, "reset"], cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", check=False, env=cli_env())
    assert refused.returncode == 2
    assert "--confirm" in refused.stderr

    cleared = subprocess.run([*base, "reset", "--confirm"], cwd=ROOT, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", check=False,
                             env=cli_env())
    assert cleared.returncode == 0
    emptied = subprocess.run([*base, "report"], cwd=ROOT, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", check=False, env=cli_env())
    assert emptied.returncode == 0
    assert json.loads(emptied.stdout)["cycles"] == 0

    # After the reset an operator's kept anchor must fail: an emptied chain proves nothing.
    emptied_anchor = subprocess.run([*base, "report", "--verify-anchor", summary["last_hash"]],
                                    cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", check=False, env=cli_env())
    assert emptied_anchor.returncode == 3
    assert json.loads(emptied_anchor.stdout)["anchor_check"]["reason"] == \
        "PREVIOUS_HASH_MISMATCH"


def test_cli_rejects_a_run_id_the_journal_would_refuse(tmp_path):
    """The parser enforces the journal's rule, so the run fails before creating a database."""
    db = tmp_path / "journal.db"
    for bad in ("x" * 65, "line\nbreak"):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"), "--db", str(db),
             "run", "--policy", "missing.yaml", "--csv", "missing.csv", "--run-id", bad],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, env=cli_env())
        assert result.returncode == 2
        assert "--run-id" in result.stderr
        assert not db.exists()


def test_cli_stops_the_clock_at_the_last_cycle_it_ran(tmp_path):
    """A limited run must not seek to the end of the file and book fills nobody decided on."""
    csv_path = tmp_path / "eurusd_h1.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Date", "Time", "Open", "High", "Low", "Close", "TickVol", "Volume",
                         "Spread"])
        for bar in make_bars(150):
            writer.writerow([bar.time.strftime("%Y.%m.%d"), bar.time.strftime("%H:%M"),
                             f"{bar.open:.5f}", f"{bar.high:.5f}", f"{bar.low:.5f}",
                             f"{bar.close:.5f}", bar.tick_volume, 0, bar.spread_points])
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "version: 1\nmode: paper\nsymbol: EURUSD\n"
        "timeframe: H1\nhistory_bars: 120\nserver_utc_offset_seconds: 0\n"
        "execution_mode: auto\n"
        "kill_switch: {active: false, reason: cli test}\n"
        "schedule: {blocked_weekdays_utc: [], window_start_minute_utc: 0,\n"
        "  window_end_minute_utc: 1440}\n"
        "symbol_spec: {symbol: EURUSD, point: 0.00001, digits: 5, tick_size: 0.00001,\n"
        "  tick_value: 1.0, volume_min: 0.01, volume_max: 100.0, volume_step: 0.01,\n"
        "  spread_points: 10, stops_level_points: 0}\n",
        encoding="utf-8")
    journal_db = tmp_path / "journal.db"
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"), "--db", str(journal_db),
         "run", "--policy", str(policy_path), "--csv", str(csv_path), "--source-timezone",
         "UTC", "--fresh", "--confirm", "--limit-cycles", "10"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, env=cli_env())
    assert run.returncode == 0, run.stderr
    summary = json.loads(run.stdout)
    with sqlite3.connect(journal_db) as connection:
        points = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM audit_events WHERE event_type = 'equity_point'")]
        trades = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM audit_events WHERE event_type = 'paper_trade_closed'")]
    # Ten cycle points, plus the closing point written when the book is closed at the end.
    assert len(points) == 11
    assert summary["final_equity"] == pytest.approx(points[-1]["equity"], abs=1e-3)
    # The tenth cycle is bar 69; its close is 70 hours after the first bar opened.
    last_close = (datetime(2026, 1, 5, tzinfo=UTC) + timedelta(hours=70)).isoformat()
    assert points[-1]["at"] == last_close
    assert all(trade["closed_at"] <= last_close for trade in trades)


def write_mt5_stub(directory: Path, *, trade_mode=0, login=77) -> Path:
    """A MetaTrader5 module good enough to serve the CLI subprocess without a terminal."""
    directory.mkdir()
    (directory / "MetaTrader5.py").write_text(
        "TIMEFRAME_H1 = 2\n"
        f"EPOCH = {SERVER_EPOCH}\n"
        f"TRADE_MODE = {trade_mode}\n"
        f"LOGIN = {login}\n"
        "def initialize(**kwargs): return True\n"
        "def shutdown(): return None\n"
        "def last_error(): return (0, 'stub')\n"
        "def account_info():\n"
        "    return type('A', (), {'login': LOGIN, 'server': 'Broker-Demo', 'currency': 'USD',\n"
        "                          'balance': 10000.0, 'equity': 10000.0,\n"
        "                          'margin_free': 9000.0, 'leverage': 100,\n"
        "                          'trade_mode': TRADE_MODE})()\n"
        "def copy_rates_from_pos(symbol, timeframe, shift, count):\n"
        "    return [{'time': EPOCH - (count - 1 - i) * 3600,\n"
        "             'open': 1.2 + i * 0.00004, 'high': 1.2 + i * 0.00004 + 0.00002,\n"
        "             'low': 1.2 + i * 0.00004 - 0.00002, 'close': 1.2 + i * 0.00004,\n"
        "             'tick_volume': 500, 'spread': 12} for i in range(count)]\n",
        encoding="utf-8")
    return directory


def run_cli_with_stub(tmp_path: Path, *arguments: str):
    stub = write_mt5_stub(tmp_path / "stub")
    env = cli_env(PYTHONPATH=str(stub) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
         "--db", str(tmp_path / "journal.db"), *arguments],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, env=env)


LIVE_RUN = ("run", "--policy", str(ROOT / "config" / "mt5_agent.example.yaml"),
            "--source", "live", "--live-bars", "300", "--limit-cycles", "3", "--verbose",
            "--execution", "auto")


def test_cli_live_source_subtracts_the_server_offset(tmp_path):
    result = run_cli_with_stub(tmp_path, *LIVE_RUN)
    assert result.returncode == 0, result.stderr
    # The example server is UTC+2, so the 61st bar must land 7200 s before the raw stamp.
    expected = datetime.fromtimestamp(SERVER_EPOCH - 239 * 3600 - 7200, tz=UTC)
    # The verbose trace is human output, so it is on stderr; stdout is the JSON summary.
    assert result.stderr.splitlines()[0].startswith(expected.isoformat())
    assert "EURUSD" in result.stdout


def test_cli_live_source_refuses_a_real_account_terminal(tmp_path):
    """Reading history from a terminal signed into a live account is not this tool's job."""
    stub = write_mt5_stub(tmp_path / "stub", trade_mode=2)
    env = cli_env(PYTHONPATH=str(stub) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
         "--db", str(tmp_path / "journal.db"), *LIVE_RUN],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, env=env)
    assert result.returncode == 2
    assert "real" in result.stderr
    assert not (tmp_path / "journal.db").exists()


def test_cli_live_source_refuses_without_a_terminal(tmp_path):
    """The live source must exit with a reason, never a traceback, on any host."""
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "MetaTrader5.py").write_text('raise ImportError("no terminal on this host")\n',
                                         encoding="utf-8")
    env = cli_env(PYTHONPATH=str(stub) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_mt5_agent.py"),
         "--db", str(tmp_path / "journal.db"), "run",
         "--policy", str(ROOT / "config" / "mt5_agent.example.yaml"), "--source", "live"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, env=env)
    assert result.returncode == 2
    assert "MetaTrader5 Python package is required" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "journal.db").exists()
