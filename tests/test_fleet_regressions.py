"""Regression tests for defects critics proved by running the old code.

Order idempotency, slippage money caps, partial fills, conductor hand-off and resume, crash-loop
backoff, the single-shard rule, and real-account gating — each is a bug that once shipped.
"""
from __future__ import annotations

import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet import broker, conductor, terminals, worker  # noqa: E402
from fleet.config import FleetConfig, PortfolioLimits  # noqa: E402
from fleet.store import Store  # noqa: E402


# --- order execution -------------------------------------------------------------------------

class FakeMT5:
    """Minimal stand-in for the MetaTrader5 module used by fleet.broker."""

    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_INVALID_FILL = 10030
    POSITION_TYPE_BUY = 0
    TIMEFRAME_H1 = 1
    TIMEFRAME_H4 = 4
    TIMEFRAME_D1 = 8

    def __init__(self, results, positions=None):
        self.results = list(results)
        self.positions = list(positions or [])
        self.sent: list[dict] = []
        self.modified: list[dict] = []

    def symbol_info_tick(self, name):
        return types.SimpleNamespace(bid=1.10000, ask=1.10002, time=1_700_000_000)

    def symbol_info(self, name):
        return types.SimpleNamespace(visible=True)

    def symbols_get(self):
        return []

    def order_send(self, request):
        self.sent.append(request)
        if request.get("action") == self.TRADE_ACTION_SLTP:
            self.modified.append(request)
            return types.SimpleNamespace(retcode=self.TRADE_RETCODE_DONE)
        return self.results.pop(0) if self.results else None

    def positions_get(self, symbol=None):
        return list(self.positions)

    def last_error(self):
        return (-1, "fake")


def _spec() -> broker.BrokerSymbol:
    return broker.BrokerSymbol(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001,
                               tick_value=1.0, volume_min=0.01, volume_max=100.0,
                               volume_step=0.01, stops_level=0, freeze_level=0, spread=5,
                               filling=3)


def test_a_lost_reply_never_opens_twice(monkeypatch) -> None:
    """order_send returns None, but the position carries our token: no retry, no duplicate."""
    fake = FakeMT5(results=[None])
    monkeypatch.setattr(broker, "mt5", fake)
    # Simulate the fill that happened despite the lost reply: token appears in the comment.
    token_holder: dict[str, str] = {}

    def positions_get(symbol=None):
        if not token_holder:
            return []
        return [types.SimpleNamespace(ticket=5, comment=f"fleet-{token_holder['token']}",
                                      volume=0.10, price_open=1.10002)]

    original_order_send = fake.order_send

    def order_send(request):
        token_holder["token"] = request["comment"].split("-", 1)[1]
        return original_order_send(request)

    monkeypatch.setattr(fake, "order_send", order_send)
    monkeypatch.setattr(fake, "positions_get", positions_get)
    execution = broker.market_order(7000001, _spec(), "buy", 0.10, sl=1.098, tp=1.104,
                                    stop_distance=0.002, risk_money=2.0)
    assert execution.volume == 0.10
    assert len(fake.sent) == 1  # no blind second send


def test_lost_reply_without_a_fill_is_reported_as_failure(monkeypatch) -> None:
    fake = FakeMT5(results=[None, None, None])
    monkeypatch.setattr(broker, "mt5", fake)
    monkeypatch.setattr(fake, "positions_get", lambda symbol=None: [])
    with pytest.raises(broker.BrokerError):
        broker.market_order(7000001, _spec(), "buy", 0.10, 1.098, 1.104, 0.002, 2.0)


def test_deviation_is_capped_by_stop_and_by_money(monkeypatch) -> None:
    fake = FakeMT5(results=[types.SimpleNamespace(retcode=10009, price=1.10002, volume=0.10)])
    monkeypatch.setattr(broker, "mt5", fake)
    spec = _spec()
    broker.market_order(1, spec, "buy", 0.10, 1.098, 1.104, stop_distance=0.0020,
                        risk_money=2.0)
    deviation = fake.sent[0]["deviation"]
    # 0.15 * 0.002 / 0.00001 = 30 points; money cap: 0.20*2.0 / (0.00001/0.00001*1.0*0.10) = 40
    assert deviation <= 30
    assert deviation >= 1


def test_partial_fill_is_accepted(monkeypatch) -> None:
    fake = FakeMT5(results=[types.SimpleNamespace(retcode=10010, price=1.10002, volume=0.05)])
    monkeypatch.setattr(broker, "mt5", fake)
    execution = broker.market_order(1, _spec(), "buy", 0.10, 1.098, 1.104, 0.002, 2.0)
    assert execution.volume == 0.05
    assert execution.retcode == 10010


def test_invalid_filling_mode_falls_back(monkeypatch) -> None:
    fake = FakeMT5(results=[types.SimpleNamespace(retcode=10030),
                            types.SimpleNamespace(retcode=10009, price=1.10002, volume=0.10)])
    monkeypatch.setattr(broker, "mt5", fake)
    execution = broker.market_order(1, _spec(), "buy", 0.10, 1.098, 1.104, 0.002, 2.0)
    assert execution.retcode == 10009
    assert len(fake.sent) == 2


def test_close_uses_the_positions_own_symbol(monkeypatch) -> None:
    position = types.SimpleNamespace(ticket=7, symbol="EURUSDm", type=0, volume=0.10,
                                     magic=1, price_open=1.10)
    fake = FakeMT5(results=[types.SimpleNamespace(retcode=10009, volume=0.10, price=1.1001)],
                   positions=[position])
    monkeypatch.setattr(broker, "mt5", fake)
    spec = _spec()
    spec.name = "EURUSDm"
    # resolution would say EURUSD, but the close must use the position's own symbol
    broker.close_position(position, spec)
    assert fake.sent[0]["symbol"] == "EURUSDm"


# --- conductor ------------------------------------------------------------------------------

class FakePopen:
    pid = 4242
    returncode = 1

    def __init__(self, command, **kwargs):
        self.command = command
        self.terminated = False
        FakePopen.last_command = command

    def poll(self):
        return None if not self.terminated else 1

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


def test_conductor_passes_config_and_store_and_uses_the_shard_filter(
        tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(conductor.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(conductor.terminals, "stop_all", lambda: None)
    store = Store(tmp_path / "book.sqlite")
    config = FleetConfig()
    engine = conductor.Conductor(config, store, config_path=tmp_path / "accounts.yaml",
                                 shards=[3])
    engine._spawn(3)
    command = FakePopen.last_command
    assert "--store" in command and str(store.path) in command
    assert "--config" in command and str((tmp_path / "accounts.yaml").resolve()) in command
    assert "--parent-pid" in command


def test_multi_shard_is_refused_on_one_machine() -> None:
    with pytest.raises(ValueError, match="один шард"):
        conductor.Conductor(FleetConfig(), Store(":memory:"), shards=[0, 1])


def test_pause_stops_the_park_and_resume_brings_it_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    monkeypatch.setattr(conductor, "STATE_FILE", tmp_path / "state.json")
    spawned: list[list[str]] = []
    stopped: list[int] = []

    def fake_stop_all():
        stopped.append(1)

    monkeypatch.setattr(conductor.terminals, "stop_all", fake_stop_all)
    monkeypatch.setattr(conductor.subprocess, "Popen",
                        lambda command, **kwargs: _record(spawned, command))
    engine = conductor.Conductor(FleetConfig(), Store(tmp_path / "book.sqlite"), shards=[0])
    engine._spawn(0)
    conductor.PAUSE_FILE.write_text("стоп", encoding="utf-8")
    state = engine.tick()
    assert state["paused"] is True
    # A pause blocks entries; shards keep running so stops and targets still fire.
    assert engine.children and not stopped
    before = len(spawned)
    engine.tick()
    assert len(spawned) == before  # no respawn storm while paused
    assert not engine.children[0].terminated
    conductor.PAUSE_FILE.unlink()
    engine.tick()
    assert len(spawned) == before  # existing shard is reused, not duplicated


def _record(sink: list, command: list) -> FakePopen:
    sink.append(command)
    return FakePopen(command)


def test_restart_backoff_prevents_a_crash_loop(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "absent")
    monkeypatch.setattr(conductor, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(conductor.terminals, "stop_all", lambda: None)
    spawned: list[list[str]] = []
    monkeypatch.setattr(conductor.subprocess, "Popen",
                        lambda command, **kwargs: _record(spawned, command))
    engine = conductor.Conductor(FleetConfig(), Store(tmp_path / "book.sqlite"), shards=[0])
    engine._spawn(0)
    engine.children[0].terminated = True  # simulate a crash
    engine.restarts[0] = 3
    engine.next_restart[0] = 10 ** 12  # backoff window still open
    engine.tick()
    assert len(spawned) == 1  # not restarted during the backoff window


def test_conductor_lock_is_exclusive(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(conductor, "LOCK_FILE", tmp_path / "conductor.lock")
    monkeypatch.setattr(conductor, "FLEET_ROOT", tmp_path)
    engine = conductor.Conductor(FleetConfig(), Store(tmp_path / "book.sqlite"), shards=[0])
    assert engine.acquire_lock() is True
    other = conductor.Conductor(FleetConfig(), Store(tmp_path / "book.sqlite"), shards=[0])
    monkeypatch.setattr(conductor, "_pid_alive", lambda pid: True)
    assert other.acquire_lock() is False
    engine.release_lock()


# --- worker gating --------------------------------------------------------------------------

def test_real_account_never_trades_without_live_flag(tmp_path, monkeypatch) -> None:
    from fleet.config import Account

    store = Store(tmp_path / "book.sqlite")
    account = Account(name="real", login=1, server="Srv", mode="real")
    runner = worker.Worker(FleetConfig(accounts=[account]), store, dry_run=False,
                           allow_live=False)
    events: list[tuple] = []
    monkeypatch.setattr(store, "event", lambda *a, **k: events.append((a, k)))
    assert (account.is_real and not runner.allow_live) is True
