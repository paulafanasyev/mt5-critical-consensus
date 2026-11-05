"""Regression tests for defects critics proved by running the old code.

Order idempotency, slippage money caps, partial fills, conductor hand-off and resume, crash-loop
backoff, the single-shard rule, and real-account gating — each is a bug that once shipped.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet import broker, candidates, conductor, config, terminals, worker  # noqa: E402
from fleet.config import FleetConfig, PortfolioLimits  # noqa: E402
from fleet.store import Store  # noqa: E402


def _own_window(store, account) -> None:
    """A census saying exactly one window on this machine holds this account.

    The entry gate is fail-closed: with no fresh census of the machine's terminal windows the park
    cannot show that this account's free margin is not being spent by a driver it has not counted,
    so it refuses new entries and keeps managing what it already holds. A test that wants an order
    to reach the broker has to supply that proof.
    """
    row = {"exe": "", "dir": worker.window_dir(str(account.data_dir)), "login": account.login,
           "server": account.server, "currency": "USD", "balance": 1000.0, "equity": 1000.0,
           "error": ""}
    store.set_meta(f"{worker.ROSTER_META_KEY}@{worker.local_host()}",
                   json.dumps({"taken": time.time(), "rows": [row]}))


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
    store = Store(tmp_path / "book.sqlite")
    config = FleetConfig()
    engine = conductor.Conductor(config, store, config_path=tmp_path / "accounts.yaml",
                                 shards=[3])
    engine._spawn(3)
    command = FakePopen.last_command
    assert "--store" in command and str(store.path) in command
    assert "--config" in command and str((tmp_path / "accounts.yaml").resolve()) in command
    assert "--parent-pid" in command


def test_several_shards_are_allowed_on_one_machine() -> None:
    # Each terminal has its own channel, so shards do not fight over a port.
    engine = conductor.Conductor(FleetConfig(), Store(":memory:"), shards=[0, 1, 2])
    assert engine.shards == [0, 1, 2]


def test_pause_stops_the_park_and_resume_brings_it_back(tmp_path, monkeypatch) -> None:
    from fleet import terminals as fleet_terminals

    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    monkeypatch.setattr(conductor, "STATE_FILE", tmp_path / "state.json")
    spawned: list[list[str]] = []
    stopped: list[int] = []

    def fake_stop(account):
        stopped.append(account.login)

    monkeypatch.setattr(fleet_terminals, "stop", fake_stop)
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
    engine.stop()
    assert not stopped  # ending the park ends its shard processes, never a terminal


def _record(sink: list, command: list) -> FakePopen:
    sink.append(command)
    return FakePopen(command)


def test_restart_backoff_prevents_a_crash_loop(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "absent")
    monkeypatch.setattr(conductor, "STATE_FILE", tmp_path / "state.json")
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


def test_partial_close_keeps_the_peak_and_the_row(tmp_path, monkeypatch) -> None:
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store, allow_live=True)
    position = types.SimpleNamespace(ticket=7, symbol="EURUSD", type=0, volume=0.10,
                                     price_open=1.10, profit=5.0, sl=1.09, tp=0.0,
                                     magic=account.magic, time=0)
    spec = worker.broker.BrokerSymbol(name="EURUSD", digits=5, point=0.00001,
                                      tick_size=0.00001, tick_value=1.0, volume_min=0.01,
                                      volume_max=100.0, volume_step=0.01, stops_level=0,
                                      freeze_level=0, spread=1, filling=3)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: spec)
    monkeypatch.setattr(worker.mt5, "symbol_info_tick",
                        lambda name: types.SimpleNamespace(bid=1.101, ask=1.101))
    monkeypatch.setattr(worker.strategy, "break_even_level", lambda *a, **k: None)
    monkeypatch.setattr(worker.strategy, "exit_reason", lambda *a, **k: "тейк")
    monkeypatch.setattr(worker.broker, "close_position", lambda *a, **k: 0.04)  # partial fill
    store.save_positions(account.name, [position])
    store.set_peak(account.name, 7, 0.0010)
    day = worker.risk.DayState(day="2026-11-03", start_balance=1000.0)
    closed = runner._manage_positions(account, day, [position])
    assert closed == 0  # not a full close
    assert store.peak(account.name, 7) == 0.0010  # peak preserved
    assert store.open_positions()[0][1] == "EURUSD"  # mirror row kept


def test_a_manual_lot_counts_in_risk_but_is_never_touched(tmp_path, monkeypatch) -> None:
    """The operator's own trade is his, not the park's.

    Manual positions belong in the mirror and the caps because they move the same money, but a
    trailing rule closing them, or a break-even move onto their stop, takes the trade away from the
    person who opened it. Ownership is the magic the park stamps on its own orders.
    """
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store, allow_live=True)
    spec = worker.broker.BrokerSymbol(name="EURUSD", digits=5, point=0.00001,
                                      tick_size=0.00001, tick_value=1.0, volume_min=0.01,
                                      volume_max=100.0, volume_step=0.01, stops_level=0,
                                      freeze_level=0, spread=1, filling=3)
    mine = types.SimpleNamespace(ticket=11, symbol="EURUSD", type=0, volume=0.10,
                                 price_open=1.10, profit=5.0, sl=1.09, tp=0.0,
                                 magic=account.magic, time=0)
    theirs = types.SimpleNamespace(ticket=12, symbol="EURUSD", type=0, volume=0.20,
                                   price_open=1.10, profit=9.0, sl=1.09, tp=0.0,
                                   magic=0, time=0)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: spec)
    monkeypatch.setattr(worker.mt5, "symbol_info_tick",
                        lambda name: types.SimpleNamespace(bid=1.101, ask=1.101))
    monkeypatch.setattr(worker.strategy, "break_even_level", lambda *a, **k: 1.1005)
    monkeypatch.setattr(worker.strategy, "exit_reason", lambda *a, **k: "тейк")
    modified: list[int] = []
    closed: list[int] = []
    monkeypatch.setattr(worker.broker, "modify_position",
                        lambda ticket, *a, **k: modified.append(ticket) or True)
    monkeypatch.setattr(worker.broker, "close_position",
                        lambda position, *a, **k: closed.append(int(position.ticket))
                        or float(position.volume))
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    n = runner._manage_positions(account, day, [mine, theirs])

    assert closed == [11] and modified == [11]  # ticket 12 was never signalled
    assert n == 1  # only our own lot is reported as closed
    kinds = [kind for _, _, kind, _ in store.recent_events(limit=10)]
    assert "foreign_positions" in kinds
    row = next(payload for _, _, kind, payload in store.recent_events(limit=10)
               if kind == "foreign_positions")
    assert json.loads(row)["tickets"] == [12]
    assert json.loads(row)["lots"] == 0.2


def test_a_new_manual_trade_is_reported_again(tmp_path, monkeypatch) -> None:
    """Announce-once must not silence a second, different fact about the same account."""
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store, allow_live=True)
    spec = worker.broker.BrokerSymbol(name="EURUSD", digits=5, point=0.00001,
                                      tick_size=0.00001, tick_value=1.0, volume_min=0.01,
                                      volume_max=100.0, volume_step=0.01, stops_level=0,
                                      freeze_level=0, spread=1, filling=3)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: spec)
    monkeypatch.setattr(worker.mt5, "symbol_info_tick",
                        lambda name: types.SimpleNamespace(bid=1.101, ask=1.101))

    def lot(ticket: int, volume: float) -> types.SimpleNamespace:
        return types.SimpleNamespace(ticket=ticket, symbol="EURUSD", type=0, volume=volume,
                                     price_open=1.10, profit=0.0, sl=0.0, tp=0.0, magic=0, time=0)

    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    first = lot(21, 0.1)
    runner._manage_positions(account, day, [first])
    runner._manage_positions(account, day, [first])  # the same fact, 90 s later
    assert len([1 for _, _, kind, _ in store.recent_events(limit=20)
                if kind == "foreign_positions"]) == 1
    runner._manage_positions(account, day, [first, lot(22, 0.1)])  # a new manual trade
    assert len([1 for _, _, kind, _ in store.recent_events(limit=20)
                if kind == "foreign_positions"]) == 2


def test_reset_health_clears_the_breaker(tmp_path) -> None:
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import fleet_cli

    store = Store(tmp_path / "book.sqlite")
    store.record_failure("a", "down", 600)
    assert store.health("a")[0] == 1
    config_path = tmp_path / "accounts.yaml"
    config_path.write_text("accounts:\n  - name: a\n    login: 1\n    server: S\n    mode: demo\n",
                           encoding="utf-8")
    assert fleet_cli.main(["--config", str(config_path), "--store", str(store.path),
                           "reset-health"]) == 0
    assert store.health("a")[0] == 0


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




# --- regressions found in the third review -------------------------------------------------

def test_book_gate_accepts_the_numeric_side_from_the_mirror(tmp_path) -> None:
    """The mirror stores "buy"/"sell"; the gate needs a numeric side or it raises."""
    from fleet import risk
    from fleet.config import PortfolioLimits

    store = Store(tmp_path / "book.sqlite")

    class P:
        ticket = 1
        symbol = "XAUUSD"
        type = 0
        volume = 0.28
        price_open = 2000.0
        profit = 0.0
        sl = 1990.0
        tp = 0.0
        magic = 1

    store.save_positions("a", [P()])
    side, volume = store.all_positions()[0][3], store.all_positions()[0][4]
    book_positions = [types.SimpleNamespace(
        symbol="XAUUSD", volume=volume, sl=1990.0, price_open=2000.0,
        type=0 if side == "buy" else 1)]
    allowed, reason = risk.can_open_portfolio(
        PortfolioLimits(max_gold_lots=0.30), 10_000, 0.0, 0.05,
        book_positions, {"XAUUSD": None}, day_pct=0.0,
        new_symbol="XAUUSD", new_volume=0.05)
    assert not allowed and "золот" in reason.lower()


def test_portfolio_stop_alarms_on_an_incomplete_book(tmp_path, monkeypatch) -> None:
    from fleet.config import Account

    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    monkeypatch.setattr(conductor, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(conductor.subprocess, "Popen",
                        lambda command, **kwargs: _record([], command))
    store = Store(tmp_path / "book.sqlite")
    accounts = [Account(name="a", login=1, server="S", mode="demo"),
                Account(name="b", login=2, server="S", mode="demo")]
    engine = conductor.Conductor(FleetConfig(accounts=accounts), store, shards=[0])
    store.equity("a", 1, 1000.0, 500.0, 0, 0.0)  # "b" has not reported yet
    store.set_meta("portfolio_day_start_equity", "1000")
    store.set_meta("portfolio_day", "2026-11-03")
    state = engine.tick()
    assert not conductor.PAUSE_FILE.exists()  # a partial book must not fake a stop
    assert store.get_meta("book_incomplete") == "1"


def test_pause_mid_cycle_blocks_the_next_accounts(tmp_path, monkeypatch) -> None:
    from fleet.config import Account

    pause = tmp_path / "PAUSE"
    accounts = [Account(name=f"a{i}", login=i + 1, server="S", mode="demo") for i in range(3)]
    config = FleetConfig(accounts=accounts)
    flags: list[bool] = []

    def fake_run(self, account, allow_entries=True):
        flags.append(allow_entries)
        pause.write_text("стоп", encoding="utf-8")  # the pause appears during the first account
        return worker.WorkerResult(account.name, "ok")

    monkeypatch.setattr(worker.Worker, "run_account", fake_run)
    worker.run_shard(config, Store(tmp_path / "book.sqlite"), 0, pause_file=pause)
    assert flags == [True, False, False]


def test_conductor_passes_the_live_flag_to_shards(tmp_path, monkeypatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(conductor.subprocess, "Popen",
                        lambda command, **kwargs: _record(captured, command))
    engine = conductor.Conductor(FleetConfig(), Store(tmp_path / "b.sqlite"), shards=[0],
                                 live=True)
    engine._spawn(0)
    assert "--live" in captured[0]


# --- entry path and the day's opening balance ----------------------------------------------

def test_try_entries_places_one_order(tmp_path, monkeypatch) -> None:
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    # The day gate refuses an unmeasured day, so this book needs a measured one to enter under.
    store.set_portfolio_baseline(worker.local_host(), "2026-11-03", 1000.0)
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    _own_window(store, account)
    runner = worker.Worker(FleetConfig(accounts=[account]), store, dry_run=False)
    spec = worker.broker.BrokerSymbol(name="EURUSD", digits=5, point=0.00001,
                                      tick_size=0.00001, tick_value=1.0, volume_min=0.01,
                                      volume_max=100.0, volume_step=0.01, stops_level=0,
                                      freeze_level=0, spread=1, filling=3)
    signal = types.SimpleNamespace(base_symbol="EURUSD", side="buy", reason="test",
                                   price=1.10, stop=1.098, target=1.104, atr=0.001,
                                   stop_distance=0.002)
    sent: list[int] = []
    monkeypatch.setattr(worker.strategy, "signals", lambda acc: [signal])
    monkeypatch.setattr(worker.broker, "symbol", lambda base: spec)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: spec)
    monkeypatch.setattr(worker.broker, "market_order",
                        lambda *a, **k: sent.append(1) or types.SimpleNamespace(
                            volume=0.01, price=1.10, retcode=10009, token="t"))
    monkeypatch.setattr(worker.broker, "open_positions", lambda *a, **k: [])
    monkeypatch.setattr(worker.risk, "can_open", lambda *a, **k: (True, ""))
    # The send-time identity gate wants the attached window to answer as this account.
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=1, server="S"))
    snapshot = risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=1000.0,
                                       equity=1000.0, margin_free=1000.0, leverage=100,
                                       positions=0, floating=0.0, server_day="2026-11-03")
    day = risk_mod.DayState(day="2026-11-03", start_balance=1000.0)
    opened = runner._try_entries(account, snapshot, [], day, 1.0)
    assert opened == 1 and sent == [1]


def test_the_stand_is_read_again_one_statement_before_the_order(tmp_path, monkeypatch) -> None:
    """An operator who arms the switch during a cycle stops the order that cycle is about to send.

    The gate at the top of `_try_entries` is behind symbol probes, a portfolio read, a money
    reservation and an identity proof — tens of seconds on a slow broker. A kill switch that only
    holds when the cycle happened to start armed races with the person reaching for it, and the slot
    it reserved has to come back with the refusal.
    """
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    pause = tmp_path / "PAUSE"
    store = StoreCls(tmp_path / "book.sqlite")
    store.set_portfolio_baseline(worker.local_host(), "2026-11-03", 1000.0)
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    _own_window(store, account)
    runner = worker.Worker(FleetConfig(accounts=[account]), store, dry_run=False,
                           pause_file=pause)
    spec = worker.broker.BrokerSymbol(name="EURUSD", digits=5, point=0.00001,
                                      tick_size=0.00001, tick_value=1.0, volume_min=0.01,
                                      volume_max=100.0, volume_step=0.01, stops_level=0,
                                      freeze_level=0, spread=1, filling=3)
    signal = types.SimpleNamespace(base_symbol="EURUSD", side="buy", reason="test",
                                   price=1.10, stop=1.098, target=1.104, atr=0.001,
                                   stop_distance=0.002)
    sent: list[int] = []
    released: list = []
    monkeypatch.setattr(worker.strategy, "signals", lambda acc: [signal])
    monkeypatch.setattr(worker.broker, "symbol", lambda base: spec)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: spec)
    monkeypatch.setattr(worker.broker, "market_order",
                        lambda *a, **k: sent.append(1) or types.SimpleNamespace(
                            volume=0.01, price=1.10, retcode=10009, token="t"))
    monkeypatch.setattr(worker.broker, "open_positions", lambda *a, **k: [])
    monkeypatch.setattr(worker.risk, "can_open", lambda *a, **k: (True, ""))
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=1, server="S"))
    real_reserve = store.reserve

    def reserve_and_arm(*args, **kwargs):
        # The last thing before the send: the money is reserved, and now the operator says stop.
        pause.write_text("оператор встал", encoding="utf-8")
        return real_reserve(*args, **kwargs)

    monkeypatch.setattr(store, "reserve", reserve_and_arm)
    monkeypatch.setattr(store, "release",
                        lambda slot: released.append(slot))
    snapshot = risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=1000.0,
                                       equity=1000.0, margin_free=1000.0, leverage=100,
                                       positions=0, floating=0.0, server_day="2026-11-03")
    day = risk_mod.DayState(day="2026-11-03", start_balance=1000.0)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert sent == []
    assert len(released) == 1  # the reservation is not left to age out for 600 s
    assert _of(store, "entries_paused")


def test_a_cycle_started_under_an_armed_stand_never_reaches_the_send(tmp_path,
                                                                    monkeypatch) -> None:
    """The same door from the other side: on a parked machine nothing is probed, reserved or sent."""
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    pause = tmp_path / "PAUSE"
    pause.write_text("оператор встал", encoding="utf-8")
    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    store.set_portfolio_baseline(worker.local_host(), "2026-11-03", 1000.0)
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    _own_window(store, account)
    runner = worker.Worker(FleetConfig(accounts=[account]), store, dry_run=False,
                           pause_file=pause)
    monkeypatch.setattr(worker.strategy, "signals", lambda acc: [_signal()])
    monkeypatch.setattr(worker.broker, "symbol", lambda base: pytest.fail("зонд символа на стоянке"))
    monkeypatch.setattr(worker.broker, "market_order", lambda *a, **k: pytest.fail("отправлен ордер"))
    monkeypatch.setattr(worker.risk, "can_open", lambda *a, **k: (True, ""))
    snapshot = risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=1000.0,
                                        equity=1000.0, margin_free=1000.0, leverage=100,
                                        positions=0, floating=0.0, server_day="2026-11-03")
    assert runner._try_entries(account, snapshot, [], risk_mod.DayState(
        day="2026-11-03", start_balance=1000.0), 1.0) == 0
    assert _of(store, "entries_paused")


def _signal() -> types.SimpleNamespace:
    return types.SimpleNamespace(base_symbol="EURUSD", side="buy", reason="test", price=1.10,
                                 stop=1.098, target=1.104, atr=0.001, stop_distance=0.002)


def test_account_funded_today_is_still_allowed_to_trade(tmp_path, monkeypatch) -> None:
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    monkeypatch.setattr(risk_mod, "daily_net_flow", lambda day: 1000.0)
    account = Account(name="fresh", login=1, server="S", mode="real")
    store = StoreCls(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store)
    snapshot = types.SimpleNamespace(balance=1000.0, equity=1000.0, currency="USD")
    state, _payload = runner._day_state(account, snapshot, "2026-11-03")
    assert state.start_balance == 1000.0  # no zero base, so no -inf stop
    assert state.net_flow == 0.0
    assert state.daily_pct(1000.0) == 0.0


def test_naked_position_still_counts_in_book_risk() -> None:
    from fleet import risk as risk_mod

    spec = worker.broker.BrokerSymbol(name="XAUUSD", digits=2, point=0.01, tick_size=0.01,
                                      tick_value=1.0, volume_min=0.01, volume_max=100.0,
                                      volume_step=0.01, stops_level=0, freeze_level=0,
                                      spread=1, filling=3)
    naked = types.SimpleNamespace(symbol="XAUUSD", volume=0.05, price_open=2000.0, sl=0.0,
                                  type=0)
    total = risk_mod.book_risk_money([naked], {"XAUUSD": spec}, {"XAUUSD": 5.0})
    assert total > 0


def test_day_flow_cache_invalidates_after_a_deposit(tmp_path, monkeypatch) -> None:
    """A deposit after the first cycle must not be masked by the cached flow."""
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    calls = {"flow": 0}

    def fake_flow(day):
        calls["flow"] += 1
        return 100.0 if calls["flow"] > 1 else 0.0

    monkeypatch.setattr(risk_mod, "daily_net_flow", fake_flow)
    monkeypatch.setattr(risk_mod, "daily_realized", lambda day: 0.0)
    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store)
    state, _payload = runner._day_state(account, types.SimpleNamespace(balance=1000.0),
                                        "2026-11-03")
    assert state.net_flow == 0.0
    # a deposit lands: the balance moves, so the cached flows must be recomputed
    state, _payload = runner._day_state(account, types.SimpleNamespace(balance=1100.0),
                                        "2026-11-03")
    assert state.net_flow == 100.0
    assert state.daily_pct(1080.0) == -2.0


def test_book_flows_use_each_accounts_broker_day(tmp_path, monkeypatch) -> None:
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    store = StoreCls(tmp_path / "book.sqlite")
    # Broker A is already on the next day, broker B is still on the old one.
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-04")
    store.equity("b", 2, 1000.0, 800.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    store.set_meta("dayflows:b:2026-11-03", "-200.00|0.00|1000.00|0")
    engine = conductor.Conductor(FleetConfig(accounts=[
        Account(name="a", login=1, server="S", mode="demo"),
        Account(name="b", login=2, server="S", mode="demo")]), store, shards=[0])
    flow, realized = engine._book_flows()
    assert flow == -200.0 and realized == 0.0


def test_prune_keeps_dayflows_of_reported_broker_days(tmp_path) -> None:
    store = Store(tmp_path / "book.sqlite")
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-04")
    store.set_meta("dayflows:a:2026-11-04", "0|0|1000|0")
    store.set_meta("dayflows:a:2026-01-01", "0|0|1000|0")
    store.prune(["a"])
    assert store.get_meta("dayflows:a:2026-11-04") is not None
    assert store.get_meta("dayflows:a:2026-01-01") is None


def test_unpriced_account_flow_is_not_counted(tmp_path) -> None:
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    store = StoreCls(tmp_path / "book.sqlite")
    store.equity("u", 1, 1000.0, 1000.0, 0, 0.0, "USD", 0.0, "2026-11-03")  # unpriced
    store.set_meta("dayflows:u:2026-11-03", "500.00|0.00|1000.00|0")
    engine = conductor.Conductor(FleetConfig(accounts=[
        Account(name="u", login=1, server="S", mode="demo")]), store, shards=[0])
    flow, _ = engine._book_flows()
    assert flow == 0.0


def test_an_unpriced_account_makes_the_portfolio_stop_unmeasurable(tmp_path) -> None:
    """A sum that leaves an account out is not this portfolio's size, so no percentage of it is a stop."""
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    store = StoreCls(tmp_path / "book.sqlite")
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    store.equity("u", 2, 1000.0, 1000.0, 0, 0.0, "RUB", 0.0, "2026-11-03")  # no dollar price
    engine = conductor.Conductor(FleetConfig(accounts=[
        Account(name="a", login=1, server="S", mode="demo"),
        Account(name="u", login=2, server="S", mode="demo")]), store, shards=[0])
    engine.day_start_equity = 1000.0

    # A calm day on the priced part is the dangerous case: the invisible account keeps losing while
    # the last line of defence reports nothing to stop.
    stop = engine._portfolio_stop(store.portfolio(["a", "u"], fresh_only=False))
    assert "не измерим" in stop and "1 счёт" in stop, stop

    # The veto comes before the percentage: a loss that would have fired the stop is reported as
    # unmeasurable rather than as a number computed on part of the money.
    store.equity("a", 1, 700.0, 700.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    assert "не измерим" in engine._portfolio_stop(store.portfolio(["a", "u"], fresh_only=False))

    # Price the second account and the stop is a measurement again — and now it fires.
    store.equity("u", 2, 1000.0, 1000.0, 0, 0.0, "RUB", 0.013, "2026-11-03")
    priced = engine._portfolio_stop(store.portfolio(["a", "u"], fresh_only=False))
    assert "не измерим" not in priced
    assert "дневной стоп портфеля" in priced, priced


def test_day_state_does_not_publish_flows(tmp_path, monkeypatch) -> None:
    """A conductor must never see new flows against the previous equity row."""
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store)
    monkeypatch.setattr(risk_mod, "daily_net_flow", lambda day: 100.0)
    monkeypatch.setattr(risk_mod, "daily_realized", lambda day: 0.0)
    snapshot = types.SimpleNamespace(balance=1100.0, equity=1100.0, currency="USD",
                                     login=1, server="S", margin_free=1000.0,
                                     leverage=100, positions=0, floating=0.0)
    day, _payload = runner._day_state(account, snapshot, "2026-11-03")
    assert store.get_meta("dayflows:a:2026-11-03") is None  # not yet: equity comes first
    assert day.net_flow == 100.0


def test_book_flows_ignore_a_payload_with_a_different_balance(tmp_path) -> None:
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    store = StoreCls(tmp_path / "book.sqlite")
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    # payload says balance 1100 (new deposit) while the book row still says 1000
    store.set_meta("dayflows:a:2026-11-03", "100.00|0.00|1100.00|0")
    engine = conductor.Conductor(FleetConfig(accounts=[
        Account(name="a", login=1, server="S", mode="demo")]), store, shards=[0])
    flow, _ = engine._book_flows()
    assert flow == 0.0
    store.equity("a", 1, 1100.0, 1100.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    flow, _ = engine._book_flows()
    assert flow == 100.0


def test_cycle_completes_and_publishes_flows(tmp_path, monkeypatch) -> None:
    """The whole per-account cycle must run, not just the pieces around it."""
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store)
    snapshot = risk_mod.AccountSnapshot(login=1, server="S", currency="USD",
                                       balance=1100.0, equity=1100.0, margin_free=1000.0,
                                       leverage=100, positions=0, floating=0.0,
                                       server_day="2026-11-03")
    monkeypatch.setattr(risk_mod, "AccountSnapshot", types.SimpleNamespace(read=lambda *a, **k: snapshot))
    monkeypatch.setattr(risk_mod, "positions_for", lambda acc: [])
    monkeypatch.setattr(risk_mod, "daily_net_flow", lambda day: 100.0)
    monkeypatch.setattr(risk_mod, "daily_realized", lambda day: 0.0)
    monkeypatch.setattr(worker.Worker, "_manage_positions", lambda self, *a, **k: 0)
    monkeypatch.setattr(worker.Worker, "_try_entries", lambda self, *a, **k: 0)
    # The cycle keys its flow witness on the broker's own day: pin it, or the test only holds
    # until the wall clock reaches the next date.
    monkeypatch.setattr(risk_mod, "server_day_now", lambda: "2026-11-03")
    result = runner._cycle(account, allow_entries=True)
    assert result.status == "ok"
    assert store.get_meta("dayflows:a:2026-11-03") is not None  # published after equity


def test_cycle_publishes_flows_with_the_final_balance(tmp_path, monkeypatch) -> None:
    """Closing a position changes the balance: the witness must be the post-trading one."""
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store)
    snapshots = [
        risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=1000.0,
                                 equity=1000.0, margin_free=1000.0, leverage=100,
                                 positions=1, floating=0.0, server_day="2026-11-03"),
        risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=800.0,
                                 equity=800.0, margin_free=800.0, leverage=100,
                                 positions=0, floating=0.0, server_day="2026-11-03"),
    ]
    calls = {"n": 0}

    def read(*a, **k):
        value = snapshots[min(calls["n"], len(snapshots) - 1)]
        calls["n"] += 1
        return value

    monkeypatch.setattr(risk_mod, "AccountSnapshot",
                        types.SimpleNamespace(read=read))
    monkeypatch.setattr(risk_mod, "positions_for", lambda acc: [])
    monkeypatch.setattr(risk_mod, "daily_net_flow", lambda day: -200.0)
    monkeypatch.setattr(risk_mod, "daily_realized", lambda day: 0.0)
    monkeypatch.setattr(worker.Worker, "_manage_positions", lambda self, *a, **k: 1)
    monkeypatch.setattr(worker.Worker, "_try_entries", lambda self, *a, **k: 0)
    monkeypatch.setattr(risk_mod, "server_day_now", lambda: "2026-11-03")
    runner._cycle(account, allow_entries=True)
    payload = store.get_meta("dayflows:a:2026-11-03")
    assert payload is not None
    assert abs(float(payload.split("|")[2]) - 800.0) < 0.01  # final balance, not 1000


def test_witness_tolerates_a_fractional_balance(tmp_path) -> None:
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    store = StoreCls(tmp_path / "book.sqlite")
    store.equity("a", 1, 800.125, 800.125, 0, 0.0, "USD", 1.0, "2026-11-03")
    store.set_meta("dayflows:a:2026-11-03", "-200.00|0.00|800.12|0")
    engine = conductor.Conductor(FleetConfig(accounts=[
        Account(name="a", login=1, server="S", mode="demo")]), store, shards=[0])
    flow, _ = engine._book_flows()
    assert flow == -200.0


def test_flows_are_published_before_equity_so_a_withdrawal_cannot_fake_a_stop(tmp_path, monkeypatch) -> None:
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store)
    snapshots = [
        risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=1000.0,
                                 equity=1000.0, margin_free=1000.0, leverage=100,
                                 positions=0, floating=0.0, server_day="2026-11-03"),
        risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=900.0,
                                 equity=900.0, margin_free=900.0, leverage=100,
                                 positions=0, floating=0.0, server_day="2026-11-03"),
    ]
    calls = {"n": 0}

    def read(*a, **k):
        value = snapshots[min(calls["n"], len(snapshots) - 1)]
        calls["n"] += 1
        return value

    monkeypatch.setattr(risk_mod, "AccountSnapshot", types.SimpleNamespace(read=read))
    monkeypatch.setattr(risk_mod, "positions_for", lambda acc: [])
    monkeypatch.setattr(risk_mod, "daily_net_flow", lambda day: -100.0)
    monkeypatch.setattr(risk_mod, "daily_realized", lambda day: 0.0)
    monkeypatch.setattr(worker.Worker, "_manage_positions", lambda self, *a, **k: 0)
    monkeypatch.setattr(worker.Worker, "_try_entries", lambda self, *a, **k: 0)
    monkeypatch.setattr(risk_mod, "server_day_now", lambda: "2026-11-03")
    runner._cycle(account, allow_entries=True)
    payload = store.get_meta("dayflows:a:2026-11-03")
    assert payload is not None
    assert payload.startswith("-100.00")
    assert abs(float(payload.split("|")[2]) - 900.0) < 0.01


def test_interleaving_between_publish_and_equity_never_fakes_a_stop(tmp_path, monkeypatch) -> None:
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    monkeypatch.setattr(conductor, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(conductor.subprocess, "Popen", lambda c, **k: _record([], c))
    store = StoreCls(tmp_path / "book.sqlite")
    account = Account(name="a", login=1, server="S", mode="demo")
    engine = conductor.Conductor(FleetConfig(accounts=[account]), store, shards=[0])
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    store.set_meta("portfolio_day", conductor.risk.server_day_now() if hasattr(conductor, "risk") else "")
    store.set_meta("portfolio_day_start_equity", "1000")
    store.set_meta("portfolio_registry_hash", engine._registry_hash())
    # withdrawal published, equity row not yet updated: the witness does not match -> flow skipped
    store.set_meta("dayflows:a:" + (store._connect().execute(
        "SELECT server_day FROM book WHERE account='a'").fetchone()[0]),
        "-100.00|0.00|900.00000000|0")
    assert engine._portfolio_stop(store.portfolio(["a"], fresh_only=False)) == ""
    # equity catches up: the flow is applied and the result is still flat
    store.equity("a", 1, 900.0, 900.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    assert engine._portfolio_stop(store.portfolio(["a"], fresh_only=False)) == ""
    # deposit direction: published +100 before the equity row, then equity 1100
    store.set_meta("dayflows:a:2026-11-03", "100.00|0.00|1100.00000000|0")
    assert engine._portfolio_stop(store.portfolio(["a"], fresh_only=False)) == ""
    store.equity("a", 1, 1100.0, 1100.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    assert engine._portfolio_stop(store.portfolio(["a"], fresh_only=False)) == ""


def test_cycle_runs_the_real_entry_path(tmp_path, monkeypatch) -> None:
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    store.set_portfolio_baseline(worker.local_host(), "2026-11-03", 1000.0)
    _own_window(store, account)
    runner = worker.Worker(FleetConfig(accounts=[account]), store)
    spec = worker.broker.BrokerSymbol(name="EURUSD", digits=5, point=0.00001,
                                      tick_size=0.00001, tick_value=1.0, volume_min=0.01,
                                      volume_max=100.0, volume_step=0.01, stops_level=0,
                                      freeze_level=0, spread=1, filling=3)
    signal = types.SimpleNamespace(base_symbol="EURUSD", side="buy", reason="test",
                                   price=1.10, stop=1.098, target=1.104, atr=0.001,
                                   stop_distance=0.002)
    snapshot = risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=1000.0,
                                       equity=1000.0, margin_free=1000.0, leverage=100,
                                       positions=0, floating=0.0, server_day="2026-11-03")
    sent = []
    monkeypatch.setattr(risk_mod, "AccountSnapshot", types.SimpleNamespace(read=lambda *a, **k: snapshot))
    monkeypatch.setattr(risk_mod, "positions_for", lambda acc: [])
    monkeypatch.setattr(risk_mod, "daily_net_flow", lambda day: 0.0)
    monkeypatch.setattr(risk_mod, "daily_realized", lambda day: 0.0)
    monkeypatch.setattr(worker.Worker, "_manage_positions", lambda self, *a, **k: 0)
    monkeypatch.setattr(worker.strategy, "signals", lambda acc: [signal])
    monkeypatch.setattr(worker.broker, "symbol", lambda base: spec)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: spec)
    monkeypatch.setattr(worker.broker, "open_positions", lambda *a, **k: [])
    monkeypatch.setattr(worker.broker, "market_order",
                        lambda *a, **k: sent.append(1) or types.SimpleNamespace(
                            volume=0.02, price=1.10, retcode=10009, token="t"))
    # The send-time identity gate wants the attached window to answer as this account.
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=1, server="S"))
    result = runner._cycle(account, allow_entries=True)
    assert result.status == "ok"
    assert sent == [1]  # the real _try_entries reached the broker call


# --- the candidate book ------------------------------------------------------------------------
def _note(side: str, asked: float, filled: float, volume: float = 0.02, ticket: int = 7,
          currency: str = "USD", rate: float = 1.0):
    signal = types.SimpleNamespace(side=side, price=asked, stop=filled - 0.002 if side == "buy"
                                   else filled + 0.002, target=filled + 0.004 if side == "buy"
                                   else filled - 0.004, atr=0.001, reason="test")
    execution = types.SimpleNamespace(price=filled, volume=volume, position_ticket=ticket)
    spec = types.SimpleNamespace(name="EURUSD", point=0.00001, tick_size=0.00001, tick_value=1.0)
    return candidates.entry_note(signal, execution, spec, 2.0, currency, rate)


def test_the_candidate_bill_is_paid_in_the_direction_the_trade_went() -> None:
    """Slippage we profited from is a rebate, slippage we paid for is a cost. Signed, not absolute."""
    worse = _note("buy", 1.10000, 1.10025)   # market ran away before the fill
    assert worse["drift_points"] == 25.0
    assert worse["drift_cost_per_lot"] == 25.0  # 1.0 per 0.00001 -> 100 000 per lot per unit
    assert worse["drift_cost"] == 0.5           # 0.50 of the account currency on 0.02 lot
    assert worse["drift_cost_usd"] == 0.5       # a USD account: the bill is already in dollars
    better = _note("buy", 1.10000, 1.09980)    # filled below the signalled price
    assert better["drift_points"] == -20.0
    assert better["drift_cost_usd"] == -0.4
    sold = _note("sell", 1.10000, 1.09980)     # a sell filled lower is worse, not better
    assert sold["drift_points"] == 20.0
    assert sold["drift_cost_usd"] == 0.4


def test_the_drift_bill_is_converted_and_never_relabelled() -> None:
    """The broker's tick value is money in the ACCOUNT currency, so a dollar column has to be a product.

    On a ruble account printing tick_value under a dollar sign overstates the bill about a hundred
    times, and a cent account understates it a hundred times. Where the worker handed over no rate,
    the dollar column is blank: an unpriced bill is a counter, not a sum.
    """
    rub = _note("buy", 1.10000, 1.10025, currency="RUB", rate=0.01)
    assert rub["currency"] == "RUB" and rub["rate_to_usd"] == 0.01
    assert rub["drift_cost"] == 0.5
    assert rub["drift_cost_usd"] == 0.005
    unpriced = _note("buy", 1.10000, 1.10025, currency="RUB", rate=0.0)
    assert unpriced["drift_cost"] == 0.5
    assert unpriced["drift_cost_usd"] is None and unpriced["rate_to_usd"] is None


def test_a_candidate_with_no_contract_size_is_left_blank_instead_of_guessed() -> None:
    signal = types.SimpleNamespace(side="buy", price=1.10, stop=1.098, target=1.104,
                                   atr=0.001, reason="test")
    execution = types.SimpleNamespace(price=1.10, volume=0.02, position_ticket=7)
    blind_spec = types.SimpleNamespace(name="EURUSD", point=0.00001, tick_size=0.0, tick_value=0.0)
    bare = candidates.entry_note(signal, execution, blind_spec, 2.0, "USD", 1.0)
    assert bare["drift_points"] == 0.0
    assert "drift_cost" not in bare and "drift_cost_usd" not in bare
    # no broker contract -> no money in any currency, no invented ones
    assert bare["risk_usd"] == 2.0


def test_the_candidate_module_can_not_reach_a_broker() -> None:
    """The one property that lets this book exist at all: it is journalling, not trading."""
    import ast

    tree = ast.parse(pathlib.Path(candidates.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= {"json", "math", "__future__"}
    assert not hasattr(candidates, "mt5")
    assert "MetaTrader5" not in pathlib.Path(candidates.__file__).read_text(encoding="utf-8")


def test_the_ledger_joins_by_ticket_and_counts_what_it_can_not_join(tmp_path) -> None:
    store = Store(tmp_path / "book.sqlite")
    store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02,
                ticket=7, ticket_source="token", currency="USD", rate_to_usd=1.0,
                drift_points=25.0, drift_cost=0.5, drift_cost_usd=0.5, risk_usd=2.0,
                risk_reward=2.0)
    store.event("a", candidates.KIND, symbol="XAUUSD", side="buy", volume=0.01,
                ticket=0, ticket_source="unknown", currency="USD", rate_to_usd=1.0,
                drift_points=3.0, drift_cost=0.1, drift_cost_usd=0.1, risk_usd=1.0)
    store.event("a", "closed", symbol="EURUSD", ticket=7, reason="trailing", profit=3.2,
                by="парк", currency="USD", rate_to_usd=1.0, profit_usd=3.2)
    store.event("a", "closed", symbol="EURUSD", reason="old row, no ticket")  # pre-ticket journal
    book = candidates.ledger(store)
    assert book["candidates"] == 2
    assert book["closed"] == 1
    assert book["blind"] == 1            # ticket 0 is counted, nothing is inferred about it
    assert book["open"] == 0
    assert book["guessed"] == 1          # the ticket-less row is the one nobody can vouch for
    assert book["profit_gross_usd"] == 3.2
    assert book["win_rate"] == 100.0
    assert book["drift_cost_usd"] == 0.6  # the drift bill covers both entries, closed or not
    assert [t["exit"] for t in book["trades"]] == ["trailing", ""]  # oldest entry first
    assert [t["closed_by"] for t in book["trades"]] == ["парк", ""]  # who ended the trade


def test_a_row_the_journal_cannot_price_is_counted_not_summed(tmp_path) -> None:
    """A finished trade whose money was never converted, and a guessed ticket, get their own counters.

    Old journal rows and a cent or ruble account read before the worker knew its rate would otherwise
    be added to a dollar column as if the number were already dollars.
    """
    store = Store(tmp_path / "book.sqlite")
    store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02, ticket=7,
                ticket_source="only_position", currency="RUB", drift_points=25.0,
                drift_cost=0.5, risk_usd=2.0)
    store.event("a", "closed", symbol="EURUSD", ticket=7, reason="стоп", profit=-12.5,
                by="брокер")
    book = candidates.ledger(store)
    assert book["candidates"] == 1
    assert book["closed"] == 0 and book["open"] == 0   # settled, but not in dollars
    assert book["settled_unpriced"] == 1
    assert book["drift_unpriced"] == 1
    assert book["guessed"] == 1 and book["by_source"] == {"only_position": 1}
    assert book["profit_gross_usd"] == 0.0 and book["drift_cost_usd"] == 0.0
    assert book["win_rate"] is None
    assert book["trades"][0]["settled"] is True and book["trades"][0]["exit"] == "стоп"


def test_the_cycle_journals_a_candidate_and_never_holds_the_risk_slot_for_it(
        tmp_path, monkeypatch) -> None:
    """The lot is at the broker before the book is written; a broken note must not strand money."""
    from fleet import risk as risk_mod
    from fleet.config import Account

    account = Account(name="a", login=1, server="S", mode="demo")
    store = Store(tmp_path / "book.sqlite")
    store.set_portfolio_baseline(worker.local_host(), "2026-11-03", 1000.0)
    _own_window(store, account)
    runner = worker.Worker(FleetConfig(accounts=[account]), store)
    spec = broker.BrokerSymbol(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001,
                               tick_value=1.0, volume_min=0.01, volume_max=100.0,
                               volume_step=0.01, stops_level=0, freeze_level=0, spread=1,
                               filling=3)
    signal = types.SimpleNamespace(base_symbol="EURUSD", side="buy", reason="test",
                                   price=1.10, stop=1.098, target=1.104, atr=0.001,
                                   stop_distance=0.002)
    snapshot = risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=1000.0,
                                        equity=1000.0, margin_free=1000.0, leverage=100,
                                        positions=0, floating=0.0, server_day="2026-11-03")
    order: list[str] = []
    real_release, real_record = store.release, candidates.record
    monkeypatch.setattr(store, "release",
                        lambda slot: (order.append("release"), real_release(slot))[1])
    monkeypatch.setattr(candidates, "record",
                        lambda *a, **k: (order.append("record"), real_record(*a, **k))[1])
    monkeypatch.setattr(risk_mod, "AccountSnapshot",
                        types.SimpleNamespace(read=lambda *a, **k: snapshot))
    monkeypatch.setattr(risk_mod, "positions_for", lambda acc: [])
    monkeypatch.setattr(risk_mod, "daily_net_flow", lambda day: 0.0)
    monkeypatch.setattr(risk_mod, "daily_realized", lambda day: 0.0)
    monkeypatch.setattr(worker.Worker, "_manage_positions", lambda self, *a, **k: 0)
    monkeypatch.setattr(worker.strategy, "signals", lambda acc: [signal])
    monkeypatch.setattr(worker.broker, "symbol", lambda base: spec)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: spec)
    monkeypatch.setattr(worker.broker, "open_positions", lambda *a, **k: [])
    monkeypatch.setattr(worker.broker, "market_order", lambda *a, **k: types.SimpleNamespace(
        volume=0.02, price=1.10025, retcode=10009, token="t", position_ticket=7))
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=1, server="S"))
    assert runner._cycle(account, allow_entries=True).status == "ok"
    assert order == ["release", "record"]  # the reservation is freed before the book is touched
    book = candidates.ledger(store)
    assert book["candidates"] == 1
    assert book["open"] == 1 and book["blind"] == 0
    assert book["trades"][0]["drift_points"] == 25.0
    assert book["trades"][0]["drift_cost_usd"] == 0.5


def _entry_desk(tmp_path, monkeypatch, *, balance: float, max_lots: float,
                fill: float = 1.10025):
    """A worker whose entry path reaches a stubbed broker, with one buy signal on EURUSD."""
    from fleet import risk as risk_mod
    from fleet.config import Account, RiskProfile

    account = Account(name="a", login=1, server="S", mode="demo",
                      risk=RiskProfile(risk_per_trade_pct=0.25, max_lots=max_lots))
    store = Store(tmp_path / "book.sqlite")
    store.set_portfolio_baseline(worker.local_host(), "2026-11-03", balance)
    _own_window(store, account)
    runner = worker.Worker(FleetConfig(accounts=[account]), store)
    spec = broker.BrokerSymbol(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001,
                               tick_value=1.0, volume_min=0.01, volume_max=100.0,
                               volume_step=0.01, stops_level=0, freeze_level=0, spread=1,
                               filling=3)
    signal = types.SimpleNamespace(base_symbol="EURUSD", side="buy", reason="test",
                                   price=1.10, stop=1.098, target=1.104, atr=0.001,
                                   stop_distance=0.002)
    snapshot = risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=balance,
                                        equity=balance, margin_free=balance, leverage=100,
                                        positions=0, floating=0.0, server_day="2026-11-03")
    monkeypatch.setattr(risk_mod, "AccountSnapshot",
                        types.SimpleNamespace(read=lambda *a, **k: snapshot))
    monkeypatch.setattr(risk_mod, "positions_for", lambda acc: [])
    monkeypatch.setattr(risk_mod, "daily_net_flow", lambda day: 0.0)
    monkeypatch.setattr(risk_mod, "daily_realized", lambda day: 0.0)
    monkeypatch.setattr(worker.Worker, "_manage_positions", lambda self, *a, **k: 0)
    monkeypatch.setattr(worker.strategy, "signals", lambda acc: [signal])
    monkeypatch.setattr(worker.broker, "symbol", lambda base: spec)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: spec)
    monkeypatch.setattr(worker.broker, "open_positions", lambda *a, **k: [])
    monkeypatch.setattr(worker.broker, "market_order", lambda *a, **k: types.SimpleNamespace(
        volume=max_lots, price=fill, retcode=10009, token="t", position_ticket=7))
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=1, server="S"))
    return account, store, runner


def test_a_lot_ceiling_that_swallows_the_configured_risk_is_said_out_loud(
        tmp_path, monkeypatch) -> None:
    """0.25% promised, 0.02% taken: sizing quietly under a ceiling is a money lie the operator
    has to see once, not a number he can only discover by reading the code."""
    account, store, runner = _entry_desk(tmp_path, monkeypatch, balance=100000.0, max_lots=0.10)
    assert runner._cycle(account, allow_entries=True).status == "ok"
    rows = [json.loads(p) for _ts, _a, kind, p in store.recent_events(limit=50)
            if kind == "risk_under_taken"]
    assert len(rows) == 1  # once per symbol per configuration, not once per cycle
    assert rows[0]["max_lots"] == 0.10 and rows[0]["taken_lots"] == 0.10
    assert rows[0]["wanted_lots"] == 1.25  # 0.25% of 100 000 over 200 USD of loss per lot
    assert rows[0]["effective_pct"] == 0.02


def test_the_notice_is_quiet_when_the_ceiling_is_not_the_binding_limit(
        tmp_path, monkeypatch) -> None:
    """A small account sizes below its ceiling on its own: nothing is being swallowed there."""
    account, store, runner = _entry_desk(tmp_path, monkeypatch, balance=1000.0, max_lots=0.10)
    assert runner._cycle(account, allow_entries=True).status == "ok"
    kinds = [kind for _ts, _a, kind, _p in store.recent_events(limit=50)]
    assert "risk_under_taken" not in kinds
    assert account.risk.max_lots == 0.10


# --- naming the position a fill made (measured against the installed API) -------------------
# MetaTrader5.OrderSendResult is an 11-field structure — retcode, deal, order, volume, price, bid,
# ask, comment, request, request_id, retcode_external — and there is no position id in it. A join
# that read `result.position_id` therefore read 0 for every trade ever taken, and the candidate
# book could never meet its own outcomes.

def _reply(**over):
    fields = {"retcode": 10009, "deal": 555, "order": 554, "volume": 0.02, "price": 1.10002}
    fields.update(over)
    return types.SimpleNamespace(**fields)


def _lot(ticket, comment, magic, volume=0.02):
    return types.SimpleNamespace(ticket=ticket, comment=comment, volume=volume,
                                 price_open=1.10002, magic=magic, symbol="EURUSD")


def test_a_fill_is_named_by_the_comment_token_not_by_an_id_the_api_does_not_return(
        monkeypatch) -> None:
    fake = FakeMT5(results=[_reply()])
    monkeypatch.setattr(broker, "mt5", fake)

    def positions_get(symbol=None):
        if not fake.sent:
            return []
        return [_lot(77, fake.sent[-1]["comment"], 7000000)]

    monkeypatch.setattr(fake, "positions_get", positions_get)
    result = broker.market_order(7000000, _spec(), "buy", 0.02, 1.098, 1.104, 0.002, 2.0)
    assert result.position_ticket == 77
    assert result.ticket_source == "token"
    assert result.position_ticket != 555  # the deal number is a different id space


def test_two_tokens_that_share_a_prefix_never_steal_each_others_lot(monkeypatch) -> None:
    """The match is the whole comment `fleet-<token>`, and every token is six hex characters.

    A bare token is a short string: `12ab` sits inside `fleet-12ab3c` as happily as inside its own
    order, so one fill joined to another lot's ticket and the book paid that trade out of the wrong
    position's profit. Equal-length needles make containment mean equality — this test is the reason
    nobody may shorten a token without changing the match.
    """
    book = [_lot(11, "fleet-12ab3c", 7000000), _lot(22, "fleet-12ab99", 7000000)]
    monkeypatch.setattr(broker.mt5, "positions_get", lambda *a, **k: book)
    assert broker._find_by_token("12ab3c").ticket == 11
    assert broker._find_by_token("12ab99").ticket == 22
    assert broker._find_by_token("") is None


def test_a_netting_account_that_merged_the_lot_is_still_named(monkeypatch) -> None:
    """The position's comment is the first order's, but one lot on this symbol and magic exists."""
    fake = FakeMT5(results=[_reply()])
    monkeypatch.setattr(broker, "mt5", fake)
    monkeypatch.setattr(fake, "positions_get",
                        lambda symbol=None: [_lot(88, "fleet-older", 7000000, volume=0.05)])
    result = broker.market_order(7000000, _spec(), "buy", 0.02, 1.098, 1.104, 0.002, 2.0)
    assert (result.position_ticket, result.ticket_source) == (88, "only_position")


def test_two_lots_on_one_symbol_are_never_guesses(monkeypatch) -> None:
    """A hedge account with a second lot here has no answer: 0/unknown, and the book calls it blind."""
    fake = FakeMT5(results=[_reply()])
    monkeypatch.setattr(broker, "mt5", fake)
    monkeypatch.setattr(fake, "positions_get", lambda symbol=None: [
        _lot(91, "fleet-aaa", 7000000), _lot(92, "fleet-bbb", 7000000)])
    result = broker.market_order(7000000, _spec(), "buy", 0.02, 1.098, 1.104, 0.002, 2.0)
    assert (result.position_ticket, result.ticket_source) == (0, "unknown")
    note = candidates.entry_note(types.SimpleNamespace(
        side="buy", price=1.10000, stop=1.098, target=1.104, atr=0.001, reason="test"),
        result, _spec(), 2.0)
    assert note["ticket"] == 0 and note["ticket_source"] == "unknown"


def test_the_reply_object_itself_carries_no_position_id() -> None:
    """The defect this section exists for, pinned to the library that shipped it."""
    import MetaTrader5

    assert not hasattr(MetaTrader5.OrderSendResult, "position_id")


# --- what the drift bill is measured against -------------------------------------------------
def test_a_sell_is_stamped_at_the_price_it_can_actually_get(monkeypatch) -> None:
    """Bid-and-ask are one tick apart, and a sell filled at that bid moved zero points.

    Stamped at the ask, every sell would pay the spread as if it were the price of waiting for
    confirmation, and the mean drift the book prints would be a fact about quote reading.
    """
    from fleet import strategy

    monkeypatch.setattr(strategy.mt5, "symbol_info_tick",
                        lambda name: types.SimpleNamespace(bid=1.09980, ask=1.10000))
    bid, ask = strategy._prices("EURUSD")
    assert strategy._side_price(bid, ask, "sell") == 1.09980
    assert strategy._side_price(bid, ask, "buy") == 1.10000
    note = candidates.entry_note(types.SimpleNamespace(
        side="sell", price=1.09980, stop=1.10180, target=1.09580, atr=0.001, reason="test"),
        types.SimpleNamespace(price=1.09980, volume=0.02, position_ticket=9,
                              ticket_source="token"), _spec(), 2.0, "USD", 1.0)
    assert note["drift_points"] == 0.0
    assert note["drift_cost"] == 0.0 and note["drift_cost_usd"] == 0.0


def test_a_non_finite_price_is_blank_not_a_number_in_the_book() -> None:
    """A broken broker read hands back NaN-shaped money; JSON cannot spell it and sums would eat it."""
    signal = types.SimpleNamespace(side="buy", price=float("nan"), stop=1.098, target=1.104,
                                   atr=0.001, reason="test")
    execution = types.SimpleNamespace(price=float("inf"), volume=0.02, position_ticket=7,
                                      ticket_source="token")
    note = candidates.entry_note(signal, execution, _spec(), float("nan"))
    assert note["fill_price"] is None and note["signal_price"] is None
    assert note["drift_points"] is None and note["risk_usd"] is None
    assert "drift_cost_usd" not in note


def test_the_note_refuses_a_key_the_journal_already_owns(tmp_path) -> None:
    """`event` writes ts/account/kind itself; a note that tried to carry one is a bug, not a row."""
    from fleet.config import Account

    account = Account(name="a", login=1, server="S", mode="demo")
    store = Store(tmp_path / "book.sqlite")
    with pytest.raises(ValueError, match="журнал"):
        candidates.record(store, account, {"symbol": "EURUSD", "kind": "candidate"})
    assert [kind for _ts, _a, kind, _p in store.recent_events(limit=5)] == []


def test_a_broken_import_cannot_be_sneaked_past_the_allowlist() -> None:
    """The AST gate has to stop `__import__("MetaTrader5")`, not only `import MetaTrader5`."""
    import ast

    tree = ast.parse(pathlib.Path(candidates.__file__).read_text(encoding="utf-8"))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    # The door into the interpreter: an allowlist of imports means nothing if these are callable.
    assert called.isdisjoint({"__import__", "eval", "exec", "compile", "globals", "locals",
                              "vars", "open", "breakpoint"})
    # And no reach through a dunder attribute on an object the caller handed us.
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                and str(node.attr).startswith("__")]


def test_a_cycle_does_not_die_when_the_candidate_note_breaks(tmp_path, monkeypatch) -> None:
    """The lot is already at the broker; a note is a note, and the day state sits behind it."""
    account, store, runner = _entry_desk(tmp_path, monkeypatch, balance=1000.0, max_lots=0.10)
    monkeypatch.setattr(candidates, "record",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("бумага кончилась")))
    result = runner._cycle(account, allow_entries=True)
    assert result.status == "ok" and result.opened == 1
    kinds = [kind for _ts, _a, kind, _p in store.recent_events(limit=50)]
    assert "candidate_note_failed" in kinds and "opened" in kinds


def test_a_close_on_another_account_never_pays_this_trade(tmp_path) -> None:
    """Tickets are unique per server, and this book is kept for every broker at once."""
    store = Store(tmp_path / "book.sqlite")
    store.event("exness", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02,
                ticket=7, currency="USD", rate_to_usd=1.0,
                drift_points=25.0, drift_cost=0.5, drift_cost_usd=0.5, risk_usd=2.0)
    store.event("gtc", "closed", symbol="EURUSD", ticket=7, reason="стоп", profit=-77.7,
                by="брокер", currency="EUR", rate_to_usd=1.1, profit_usd=-85.47)
    book = candidates.ledger(store)
    assert book["closed"] == 0
    assert book["open"] == 1
    assert book["profit_gross_usd"] == 0.0


def test_two_rows_on_one_ticket_pay_the_drift_once_and_take_no_close(tmp_path) -> None:
    """A retry journalled the same fill twice, and a netting merge stamps two entries one ticket.

    Either way this book cannot say which line that ticket's close belongs to, so it pays none of
    them and calls none of them open; and one entry into the market is one 25-point wait, not two.
    """
    store = Store(tmp_path / "book.sqlite")
    for _ in range(2):
        store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02,
                    ticket=7, currency="USD", rate_to_usd=1.0,
                    drift_points=25.0, drift_cost=0.5, drift_cost_usd=0.5, risk_usd=2.0)
    store.event("a", "closed", symbol="EURUSD", ticket=7, reason="тейк", profit=50.0,
                by="парк", currency="USD", rate_to_usd=1.0, profit_usd=50.0)
    book = candidates.ledger(store)
    assert book["candidates"] == 2
    assert book["closed"] == 0 and book["open"] == 0  # neither settled nor waiting: unattributable
    assert book["shared_ticket"] == 2
    assert [t["shared"] for t in book["trades"]] == [True, True]
    assert book["profit_gross_usd"] == 0.0 and book["win_rate"] is None
    assert book["drift_cost_usd"] == 0.5 and book["drift_priced"] == 1
    assert book["mean_drift_points"] == 25.0


def test_a_second_entry_on_the_same_ticket_is_not_called_open(tmp_path) -> None:
    """The gap this replaced: the duplicate stayed «открыт» forever, because the close went to the first."""
    store = Store(tmp_path / "book.sqlite")
    store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02, ticket=7,
                ticket_source="token", currency="USD", rate_to_usd=1.0, drift_points=25.0,
                drift_cost=0.5, drift_cost_usd=0.5, risk_usd=2.0)
    store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02, ticket=7,
                ticket_source="only_position", currency="USD", rate_to_usd=1.0,
                drift_points=25.0, drift_cost=0.5, drift_cost_usd=0.5, risk_usd=2.0)
    store.event("a", "closed", symbol="EURUSD", ticket=7, reason="тейк", profit=50.0,
                by="парк", currency="USD", rate_to_usd=1.0, profit_usd=50.0)
    book = candidates.ledger(store)
    assert book["open"] == 0 and book["shared_ticket"] == 2
    assert book["guessed"] == 1  # one of the two rows still carries an inferred ticket


def test_a_settled_guess_is_counted_and_shown_outside_the_sum(tmp_path) -> None:
    """Finding: the CLI promised guessed joins stayed out of the totals while the ledger added them.

    They are out now: `closed`, `win_rate` and `profit_gross_usd` are token-only, and a guessed
    outcome still worth reading lives in its own counter.
    """
    store = Store(tmp_path / "book.sqlite")
    store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02, ticket=7,
                ticket_source="only_position", currency="USD", rate_to_usd=1.0,
                drift_points=0.0, drift_cost=0.0, drift_cost_usd=0.0, risk_usd=2.0)
    store.event("a", "closed", symbol="EURUSD", ticket=7, reason="стоп", profit=-12.5,
                by="брокер", currency="USD", rate_to_usd=1.0, profit_usd=-12.5)
    book = candidates.ledger(store)
    assert book["closed"] == 0 and book["win_rate"] is None
    assert book["profit_gross_usd"] == 0.0
    assert book["closed_guessed"] == 1 and book["guessed_profit_usd"] == -12.5
    row = book["trades"][0]
    assert row["settled"] and not row["attributable"]
    assert row["profit_gross_usd"] is None and row["guessed_profit_usd"] == -12.5
    assert book["settled_unpriced"] == 0  # it is priced, it simply is not ours to sum


def test_a_break_even_close_is_not_counted_as_a_loss(tmp_path) -> None:
    store = Store(tmp_path / "book.sqlite")
    for ticket, profit in ((7, 0.0), (8, 4.0), (9, -4.0)):
        store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02,
                    ticket=ticket, ticket_source="token", currency="USD", rate_to_usd=1.0,
                    drift_points=1.0, drift_cost=0.02, drift_cost_usd=0.02, risk_usd=2.0)
        store.event("a", "closed", symbol="EURUSD", ticket=ticket, reason="в ноль",
                    profit=profit, by="парк", currency="USD", rate_to_usd=1.0,
                    profit_usd=profit)
    book = candidates.ledger(store)
    assert book["closed"] == 3 and book["flat"] == 1
    assert book["win_rate"] == 50.0  # one win of two decided trades, not one of three
    assert book["profit_gross_usd"] == 0.0


def test_a_drift_that_was_never_converted_is_never_printed_as_free(tmp_path: pathlib.Path,
                                                                    capsys) -> None:
    """Finding: points measured without a rate came out as «+0.00 USD суммарно» — a zero, not a gap."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import fleet_cli

    store = Store(tmp_path / "book.sqlite")
    store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02, ticket=7,
                ticket_source="token", currency="RUB", drift_points=25.0,
                drift_cost=0.5, risk_usd=2.0)
    assert fleet_cli.main(["--store", str(store.path), "candidates"]) == 0
    out = capsys.readouterr().out
    assert "в долларах не посчитано" in out and "суммарно" not in out
    assert "дрейф без курса" in out
    assert "+0.00 USD суммарно" not in out


def test_a_window_that_ran_out_before_any_candidate_says_that(tmp_path: pathlib.Path,
                                                              capsys) -> None:
    """Finding: a journal of closes with the window spent on them read as an empty book."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import fleet_cli

    store = Store(tmp_path / "book.sqlite")
    for index in range(4):
        store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02,
                    ticket=100 + index, drift_points=1.0)
    for index in range(6):
        store.event("a", "closed", symbol="EURUSD", ticket=100 + index, reason="стоп",
                    profit=-1.0)
    assert fleet_cli.main(["--store", str(store.path), "candidates", "--limit", "4"]) == 0
    out = capsys.readouterr().out
    assert "разобрано только" in out and "Увеличь --limit" in out
    assert "кандидатов пока нет" not in out


def test_a_short_window_is_reported_as_partial_not_as_an_empty_book(tmp_path) -> None:
    """The limit is spent on candidates and closes only, and the reader says how many it missed."""
    store = Store(tmp_path / "book.sqlite")
    for index in range(300):
        store.event("a", "hold", reason="шум журнала")
    for index in range(4):
        store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02,
                    ticket=100 + index, drift_points=1.0, drift_cost_usd=0.1, risk_usd=2.0)
    book = candidates.ledger(store, limit=3)
    assert book["candidates"] == 3  # the newest three, not "nothing"
    assert book["journal_rows"] == 4
    assert book["truncated"] is True
    assert candidates.ledger(store)["truncated"] is False


# --- whose lot decides which cap (the first critic proved these on the committed code) ------
def _position(ticket: int, magic, symbol: str = "EURUSD", volume: float = 0.10):
    return types.SimpleNamespace(ticket=ticket, symbol=symbol, type=0, volume=volume,
                                 price_open=1.10, profit=0.0, sl=1.098, tp=0.0, magic=magic,
                                 time=0)


def test_a_foreign_book_does_not_spend_our_position_cap(tmp_path) -> None:
    """The count cap is this strategy's concentration limit, not the whole account's.

    A book full of lots the operator placed himself against a cap of three freezes the park: every
    cycle says "лимит позиций" and nothing trades. The money limits still read the whole account,
    because a manual lot moves the same equity — that is why the same lots stop us when the money
    is gone, not when the slots are.
    """
    from fleet import risk as risk_mod
    from fleet.config import Account, RiskProfile

    account = Account(name="a", login=1, server="S", mode="demo",
                      risk=RiskProfile(max_open_positions=2))
    snapshot = risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=1000.0,
                                        equity=1000.0, margin_free=1000.0, leverage=100,
                                        positions=8, floating=0.0, server_day="2026-11-04")
    day = risk_mod.DayState(day="2026-11-04", start_balance=1000.0)
    foreign = [_position(ticket, 0) for ticket in range(1, 9)]
    assert risk_mod.can_open(account, snapshot, day, foreign)[0] is True
    ours = [_position(20 + ticket, account.magic) for ticket in range(2)]
    allowed, reason = risk_mod.can_open(account, snapshot, day, ours + foreign)
    assert allowed is False and "лимит" in reason


def test_a_lot_that_reports_no_magic_at_all_is_not_ours(tmp_path) -> None:
    """A missing field on a broker object is not a crash in the middle of an exit pass."""
    from fleet import risk as risk_mod
    from fleet.config import Account

    account = Account(name="a", login=1, server="S", mode="demo")
    bare = [types.SimpleNamespace(ticket=1, symbol="EURUSD", volume=0.1)]
    assert risk_mod.own_in(account, bare) == []
    assert risk_mod.magic_of(bare[0]) == 0
    assert risk_mod.magic_of(_position(2, None)) == 0
    assert [p.ticket for p in risk_mod.own_in(account, [_position(3, account.magic)])] == [3]


def test_a_manual_lot_on_the_signal_symbol_does_not_cost_us_the_entry(tmp_path,
                                                                      monkeypatch) -> None:
    """max_positions_per_symbol is our rule about our own trades."""
    account, store, runner = _entry_desk(tmp_path, monkeypatch, balance=1000.0, max_lots=0.10)
    # 0.01 lot: its own stop risk stays inside the portfolio cap, so the only thing that could
    # refuse this entry is the per-symbol rule reading a lot that is not ours.
    monkeypatch.setattr(worker.risk, "positions_for", lambda acc: [_position(31, 0, volume=0.01)])
    sent: list = []

    def order(*args, **kwargs):
        sent.append(args)
        return types.SimpleNamespace(volume=0.10, price=1.10025, retcode=10009, token="t",
                                     position_ticket=7, ticket_source="token")

    monkeypatch.setattr(worker.broker, "market_order", order)
    assert runner._cycle(account, allow_entries=True).opened == 1
    assert len(sent) == 1


def test_a_locked_journal_still_closes_the_parks_own_lot(tmp_path, monkeypatch) -> None:
    """A report that cannot be written must not sit between this account and its exit.

    The shared book is one sqlite file for every shard on the machine. A writer that meets a lock
    it cannot wait out used to raise before the management loop even started, so a minute of
    unmanaged risk was the price of a line of text nobody read.
    """
    import sqlite3

    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store, allow_live=True)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: _spec())
    monkeypatch.setattr(worker.mt5, "symbol_info_tick",
                        lambda name: types.SimpleNamespace(bid=1.101, ask=1.101))
    monkeypatch.setattr(worker.strategy, "break_even_level", lambda *a, **k: 1.1005)
    monkeypatch.setattr(worker.strategy, "exit_reason", lambda *a, **k: "тейк")

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "set_meta", locked)
    monkeypatch.setattr(store, "get_meta", locked)
    closed: list[int] = []
    monkeypatch.setattr(worker.broker, "close_position",
                        lambda position, *a, **k: closed.append(int(position.ticket))
                        or float(position.volume))
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    assert runner._manage_positions(account, day, [_position(41, account.magic)]) == 1
    assert closed == [41]


def test_the_foreign_lots_are_reported_from_one_meta_row_per_account(tmp_path,
                                                                    monkeypatch) -> None:
    """Announcing every new set must not grow the journal one permanent row per trade ever made."""
    import sqlite3

    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store, allow_live=True)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: _spec())
    monkeypatch.setattr(worker.mt5, "symbol_info_tick",
                        lambda name: types.SimpleNamespace(bid=1.101, ask=1.101))
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    for first in range(1, 30):
        runner._manage_positions(account, day, [_position(100 + first, 0)])
    with sqlite3.connect(store.path) as conn:
        rows = conn.execute(
            "SELECT key FROM meta WHERE key LIKE '%foreign_positions%'").fetchall()
    assert len(rows) == 1  # one row, and its value moves with the set
    events = [kind for _ts, _a, kind, _p in store.recent_events(limit=200)]
    assert events.count("foreign_positions") >= 2  # every new fact is still said


def test_a_lot_the_park_placed_with_a_lost_magic_is_named_as_unmanaged(tmp_path,
                                                                       monkeypatch) -> None:
    """Our own entry note plus someone else's magic is a live risk nobody is driving."""
    from fleet.config import Account, FleetConfig
    from fleet.store import Store as StoreCls

    account = Account(name="a", login=1, server="S", mode="demo")
    store = StoreCls(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store, allow_live=True)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: _spec())
    monkeypatch.setattr(worker.mt5, "symbol_info_tick",
                        lambda name: types.SimpleNamespace(bid=1.101, ask=1.101))
    store.set_meta(f"atr:{account.name}:51", "0.00100000")
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    runner._manage_positions(account, day, [_position(51, 0)])
    rows = [json.loads(payload) for _ts, _a, kind, payload in store.recent_events(limit=20)
            if kind == "park_lot_unattributed"]
    assert len(rows) == 1 and rows[0]["tickets"] == [51]
    runner._manage_positions(account, day, [_position(51, 0)])  # the same fact, next cycle
    assert len([1 for _ts, _a, kind, _p in store.recent_events(limit=40)
                 if kind == "park_lot_unattributed"]) == 1


def test_an_account_configured_with_no_magic_would_own_every_manual_lot(tmp_path) -> None:
    """Magic 0 is the mark a hand-placed trade carries; a park configured with it takes them all."""
    from fleet.config import ConfigError

    target = tmp_path / "accounts.yaml"
    target.write_text("""
accounts:
  - name: a
    login: 1
    server: S
    mode: demo
    magic: 0
""", encoding="utf-8")
    with pytest.raises(ConfigError, match="magic"):
        config.load(target)


# --- the book that already exists on disk (measured on the production journal) ---------------

OLD_TABLES = """
CREATE TABLE equity (ts TEXT, account TEXT, login INTEGER, balance REAL, equity REAL,
                     positions INTEGER, floating REAL);
CREATE TABLE trades (ts TEXT, account TEXT, login INTEGER, symbol TEXT, side TEXT, volume REAL,
                     price REAL, sl REAL, tp REAL, retcode INTEGER, comment TEXT);
"""


def test_a_book_written_before_the_contract_columns_gains_them(tmp_path) -> None:
    """`CREATE TABLE IF NOT EXISTS` never touches a table that exists, so old rows block new code.

    Measured on the live journal: `equity` had no currency/rate_to_usd and `trades` had no
    requested_price/requested_volume/token. Every cycle died in the first INSERT, and the second
    one sat after the broker had already filled the order.
    """
    import sqlite3

    path = tmp_path / "book.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_TABLES)
    conn.commit()
    conn.close()

    store = Store(path)

    conn = sqlite3.connect(path)
    columns = {table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
               for table in ("equity", "trades")}
    conn.close()
    assert {"currency", "rate_to_usd"} <= set(columns["equity"])
    assert {"requested_price", "requested_volume", "token"} <= set(columns["trades"])
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, currency="EUR", rate_to_usd=1.08)
    store.trade("a", 1, "EURUSD", "buy", 0.1, 1.0, 0.1, 10009, "reason", "tok")
    assert store.account_equity("a") == pytest.approx(1080.0)


def test_a_lost_journal_row_is_reported_lost(tmp_path, monkeypatch) -> None:
    """A report that cannot be written must not mark itself announced.

    The journal can be locked by another window for the whole cycle. The park keeps trading — the
    row is a money trail, not a guard — but the caller has to be able to tell the two outcomes.
    """
    import sqlite3

    store = Store(tmp_path / "book.sqlite")

    def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_connect", locked)
    assert store.event("a", "close", ticket=51) is False
    conn = sqlite3.connect(tmp_path / "book.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    conn.close()
    assert '"close"' in store.trail.read_text(encoding="utf-8")  # the paper copy is all that is left


def test_the_prune_keeps_the_cooldown_and_loses_the_dead_ticket_note(tmp_path) -> None:
    """One lifetime rule for both kinds of note deleted the cooldown every 30 seconds.

    `entry:<account>:<symbol>` is the park's promise not to re-enter a symbol for
    `cooldown_minutes`; it belongs to no ticket. `atr:<account>:<ticket>` belongs to a lot that
    really does die. Pruning both against the position book meant a symbol cooled down, went
    un-cooled and re-entered on the next cycle — forever.
    """
    import datetime as dt

    store = Store(tmp_path / "book.sqlite")

    class P:
        ticket, symbol, type, volume = 51, "EURUSD", 0, 0.1
        price_open, profit, sl, tp, magic = 1.1, 0.0, 1.09, 0.0, 7

    store.save_positions("a", [P()])
    store.set_meta("atr:a:51", "0.00120000")
    store.set_meta("entry:a:EURUSD",
                   dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    store.set_meta("entry:a:XAUUSD",
                   (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30))
                   .isoformat(timespec="seconds"))
    store.prune()
    assert store.get_meta("atr:a:51") == "0.00120000"
    assert store.get_meta("entry:a:EURUSD")  # its cooldown has not run out
    assert store.get_meta("entry:a:XAUUSD") is None  # a month stale, worthless
    store.close_position_row("a", 51)
    store.prune()
    assert store.get_meta("atr:a:51") is None  # the lot is gone, so is its ATR


def test_the_mirror_answers_which_lots_the_book_last_saw(tmp_path) -> None:
    """A ticket that left the broker's book without our asking is a close nobody journalled."""
    store = Store(tmp_path / "book.sqlite")

    class P:
        ticket, symbol, type, volume = 51, "EURUSD", 0, 0.1
        price_open, profit, sl, tp, magic = 1.1, 0.0, 1.09, 0.0, 7

    store.save_positions("a", [P()])
    assert store.mirrored_positions("a") == {51: (7, 0.1, "EURUSD")}
    with store._connect() as conn:  # noqa: SLF001 - same package, the clock is what is being moved
        conn.execute("UPDATE positions SET updated_epoch=?", (time.time() - 900 - 1,))
    assert store.mirrored_positions("a") == {}  # a stale picture proves nothing about a close


def _managed(tmp_path, monkeypatch):
    """A worker whose broker answers with the fixed EURUSD spec and a live tick."""
    from fleet.config import Account, FleetConfig

    account = Account(name="a", login=1, server="S", mode="demo")
    store = Store(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store, allow_live=True)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: _spec())
    monkeypatch.setattr(worker.mt5, "symbol_info_tick",
                        lambda name: types.SimpleNamespace(bid=1.101, ask=1.101))
    return account, store, runner


def _of(store, kind: str) -> list[dict]:
    return [json.loads(payload) for _ts, _a, k, payload in store.recent_events(limit=200)
            if k == kind]


def test_a_lot_the_broker_closed_is_journalled_with_its_own_profit(tmp_path,
                                                                   monkeypatch) -> None:
    """A stop the broker hit is a finished trade, and the candidate book has to be paid for it.

    Every close ordered here writes its own journal line, so a ticket that stood in last cycle's
    mirror and is absent from this book left on its own — at its stop, from the platform, by a server
    rule. Missing those left the ledger calling a settled trade open forever: the drift bill counted
    against a profit column that never filled, and nobody could check the park against the broker.
    """
    account, store, runner = _managed(tmp_path, monkeypatch)
    store.save_positions("a", [_position(41, account.magic)])  # last cycle saw our lot

    def deals(position):  # noqa: ARG001 - the ticket arrives as a keyword
        return [types.SimpleNamespace(entry=0, profit=0.0, comment="fleet-1"),  # the opening deal
                types.SimpleNamespace(entry=1, profit=-12.5, comment="[SL]")]  # the closing one

    monkeypatch.setattr(worker.mt5, "history_deals_get", deals)
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    assert runner._manage_positions(account, day, [], "EUR", 1.10) == 0
    assert _of(store, "closed") == [{"symbol": "EURUSD", "ticket": 41, "reason": "[SL]",
                                     "by": "брокер", "profit": -12.5, "currency": "EUR",
                                     "rate_to_usd": 1.1, "profit_usd": -13.75}]
    runner._manage_positions(account, day, [], "EUR", 1.10)
    assert len(_of(store, "closed")) == 1  # said once: the mirror row went with the report


def test_a_broker_close_the_history_cannot_price_is_left_unpriced(tmp_path, monkeypatch) -> None:
    """An exit whose deal the terminal will not show is a missing number, not a result of zero."""
    account, store, runner = _managed(tmp_path, monkeypatch)
    store.save_positions("a", [_position(41, account.magic)])
    monkeypatch.setattr(worker.mt5, "history_deals_get", lambda **_k: None)  # history read failed
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    runner._manage_positions(account, day, [], "EUR", 1.10)
    row = _of(store, "closed")[0]
    assert row["by"] == "брокер" and row["ticket"] == 41 and row["symbol"] == "EURUSD"
    assert row["profit"] is None and row["profit_usd"] is None


def _exits(store, monkeypatch):
    """An order-sending broker and a strategy that always wants out: what a close test needs."""
    sent: list[str] = []
    monkeypatch.setattr(worker.broker, "modify_position", lambda *a, **k: sent.append("modify"))
    monkeypatch.setattr(worker.broker, "close_position",
                        lambda position, *a, **k: sent.append("close") or float(position.volume))
    monkeypatch.setattr(worker.strategy, "break_even_level", lambda *a, **k: 1.1005)
    monkeypatch.setattr(worker.strategy, "exit_reason", lambda *a, **k: "тейк")
    return sent


def test_a_manual_lot_merged_into_ours_is_not_modified_or_closed(tmp_path,
                                                                 monkeypatch) -> None:
    """On a netting account his trade folds into our ticket, so the magic alone no longer says whose.

    The witness remembers how many lots of this ticket we bought, and volume beyond it stops every
    order this park would send for that ticket: a trailing exit here would close his lot along with
    ours without ever asking him.
    """
    account, store, runner = _managed(tmp_path, monkeypatch)
    store.note_lot_witness("a", 31, 0.10)  # the park ordered these lots at the fill
    merged = _position(31, account.magic, volume=0.20)
    sent = _exits(store, monkeypatch)
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    assert runner._manage_positions(account, day, [merged], "USD", 1.0) == 0
    assert sent == []  # not one order into a ticket that is two people's money
    assert _of(store, "lot_merged_with_manual")[0]["lots"] == [
        {"ticket": 31, "asked": 0.1, "now": 0.2}]
    runner._manage_positions(account, day, [merged], "USD", 1.0)
    assert len(_of(store, "lot_merged_with_manual")) == 1  # the same fact is said once


def test_a_merge_survives_the_cycle_that_spotted_it(tmp_path, monkeypatch) -> None:
    """The end of the cycle rewrites the mirror with the merged volume; the witness must not follow.

    A guard that only lasts until the book next photographs the lot is a guard for one cycle: the
    trailing rule takes several cycles to fire, and by the cycle that closes the ticket the previous
    picture says 0.20 — the operator's half, adopted as ours.
    """
    account, store, runner = _managed(tmp_path, monkeypatch)
    store.note_lot_witness("a", 31, 0.10)
    merged = _position(31, account.magic, volume=0.20)
    sent = _exits(store, monkeypatch)
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    runner._manage_positions(account, day, [merged], "USD", 1.0)
    store.save_positions("a", [merged])  # what `_cycle` writes after management
    assert store.mirrored_positions("a")[31][1] == pytest.approx(0.20)
    assert runner._manage_positions(account, day, [merged], "USD", 1.0) == 0
    assert runner._manage_positions(account, day, [merged], "USD", 1.0) == 0
    assert sent == []
    assert store.lot_witnesses("a") == {31: pytest.approx(0.10)}


def test_a_lot_that_merged_while_this_shard_was_dark_is_still_two_people_money(tmp_path,
                                                                               monkeypatch) -> None:
    """The mirror is a photograph with a 900 s shelf life; the fold-in does not end when it does.

    After an outage longer than the mirror's TTL the first cycle back used to find no previous
    picture at all, call the ticket new — a lot nobody ordered could not have grown — and sell the
    whole merged lot, his half included, without asking.
    """
    account, store, runner = _managed(tmp_path, monkeypatch)
    store.save_positions("a", [_position(31, account.magic)])
    store.note_lot_witness("a", 31, 0.10)
    with store._connect() as conn:  # noqa: SLF001 - same package, the clock is what is being moved
        conn.execute("UPDATE positions SET updated_epoch=?", (time.time() - 900 - 1,))
    assert store.mirrored_positions("a") == {}  # the photograph is gone
    sent = _exits(store, monkeypatch)
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    assert runner._manage_positions(account, day,
                                    [_position(31, account.magic, volume=0.20)], "USD", 1.0) == 0
    assert sent == []
    assert _of(store, "lot_merged_with_manual")[0]["lots"] == [
        {"ticket": 31, "asked": 0.1, "now": 0.2}]


def test_a_lot_the_park_shrank_is_still_ours_to_manage(tmp_path, monkeypatch) -> None:
    """A ticket that got smaller is not a merge: the witness follows it down and the lot stays ours.

    Half of it can have been a partial close ordered here, a close the operator made from the
    platform, or the broker trimming it. Refusing to manage every shrink would strand our own money
    with nothing but the broker's stop in front of it.
    """
    account, store, runner = _managed(tmp_path, monkeypatch)
    store.note_lot_witness("a", 31, 0.10)
    sent = _exits(store, monkeypatch)
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    assert runner._manage_positions(account, day,
                                   [_position(31, account.magic, volume=0.05)], "USD", 1.0) == 1
    assert sent == ["modify", "close"]
    assert store.lot_witnesses("a") == {}  # the close settled it, and the record went with the lot
    assert _of(store, "lot_merged_with_manual") == []


def test_the_witness_is_written_at_the_fill_and_never_invented_by_looking(tmp_path) -> None:
    """Two writers, two rules: what we ordered may add, what we see may only subtract.

    `note_lot_witness` is called with the volume on the book every cycle. If it could raise the
    number, the merge it is meant to catch would be adopted the moment it happened.
    """
    store = Store(tmp_path / "book.sqlite")
    store.note_lot_witness("a", 41, 0.10)
    store.note_lot_witness("a", 41, 0.20)  # the market grew around the lot
    assert store.lot_witnesses("a") == {41: pytest.approx(0.10)}
    store.add_lot_witness("a", 41, 0.10)  # and this park bought another lot into the same ticket
    assert store.lot_witnesses("a") == {41: pytest.approx(0.20)}
    store.note_lot_witness("a", 41, 0.20)
    assert store.lot_witnesses("a") == {41: pytest.approx(0.20)}
    store.note_lot_witness("a", 41, 0.15)  # a partial close by either of us
    assert store.lot_witnesses("a") == {41: pytest.approx(0.15)}


def test_a_closed_ticket_takes_its_witness_and_its_peak_with_it(tmp_path) -> None:
    """The record of a lot that no longer exists is a row that never dies for a ticket to reuse."""
    store = Store(tmp_path / "book.sqlite")
    store.note_lot_witness("a", 41, 0.10)
    store.set_peak("a", 41, 12.5)
    store.close_position_row("a", 41)
    assert store.lot_witnesses("a") == {}
    assert store.peak("a", 41) == 0.0


def test_the_prune_ages_a_witness_by_the_cycle_that_last_saw_it(tmp_path) -> None:
    """A live lot keeps its proof through any outage; a month of nobody seeing it is a dead ticket.

    Pruning the witness against the mirror's freshness window would delete it in exactly the case it
    exists for, and pruning it against nothing at all leaves a row for every ticket the account ever
    had.
    """
    store = Store(tmp_path / "book.sqlite")
    store.note_lot_witness("a", 41, 0.10)
    store.note_lot_witness("a", 42, 0.10)
    with store._connect() as conn:  # noqa: SLF001 - same package, the clock is what is being moved
        conn.execute("UPDATE lot_witness SET updated_epoch=? WHERE ticket=42",
                     (time.time() - 31 * 86400,))
    store.prune(["a"])
    assert store.lot_witnesses("a") == {41: pytest.approx(0.10)}
    store.prune(["b"])
    assert store.lot_witnesses("a") == {}  # an account nobody here registers is not this book's


def test_a_window_answering_for_another_account_closes_nothing(tmp_path, monkeypatch) -> None:
    """What stands in front of an exit is the address, and `--live` is not an address.

    The park restarted without `--live` — the default of every restart path — used to drop trailing,
    giveback and break-even off every lot it had previously been permitted to open, leaving only the
    broker's own stop in front of the money. A close still has to know whose account this is: `attach`
    proves the window at connect time, and the cycle asks the same question of the account it is about
    to act on, because a hand re-login between the two is invisible to everything else in the file.
    """
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig

    account = Account(name="a", login=1, server="S", mode="demo")
    store = Store(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store, allow_live=False)
    stranger = risk_mod.AccountSnapshot(login=999, server="Someone-Else", currency="USD",
                                        balance=1000.0, equity=1000.0, margin_free=1000.0,
                                        leverage=100, positions=0, floating=0.0,
                                        server_day="2026-11-03")
    ours = risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=1000.0,
                                    equity=1000.0, margin_free=1000.0, leverage=100,
                                    positions=0, floating=0.0, server_day="2026-11-03")
    answering = [stranger]
    monkeypatch.setattr(risk_mod, "AccountSnapshot", types.SimpleNamespace(
        read=lambda *a, **k: answering[0]))
    monkeypatch.setattr(risk_mod, "positions_for", lambda acc: [])
    monkeypatch.setattr(risk_mod, "server_day_now", lambda: "2026-11-03")
    managed: list[str] = []
    monkeypatch.setattr(worker.Worker, "_manage_positions",
                        lambda self, *a, **k: managed.append("managed") or 0)
    monkeypatch.setattr(worker.Worker, "_try_entries", lambda self, *a, **k: 0)
    result = runner._cycle(account, allow_entries=True)
    assert result.status == "error" and managed == []
    assert _of(store, "exit_identity_lost")[0]["detail"].startswith("отвечает 999")

    answering[0] = ours
    assert runner._cycle(account, allow_entries=True).status == "ok"
    assert managed == ["managed"]  # no `--live` was ever asked, and no lot was left unmanaged
    assert _of(store, "exit_identity_restored")  # the note went back to empty, not left behind


def test_the_risk_mirror_prices_the_operators_lot_without_claiming_it(tmp_path,
                                                                     monkeypatch) -> None:
    """The money cap counts his lot; the authorship note must still say the park never placed it.

    The dollar mirror used to ask for this account's entry ATR of every position, which wrote
    `atr:<account>:<ticket>` for the operator's trades too — and the reporter then read its own note
    back and raised `park_lot_unattributed`, the alarm for a lot we placed whose magic has gone, on a
    machine where nothing of ours was lost.
    """
    account, store, runner = _managed(tmp_path, monkeypatch)
    his = _position(52, 0, volume=0.30)
    priced = runner._mirror_risk_usd(account, [his], 1.0)
    assert priced == {52: pytest.approx(60.0)}  # 200 points of stop distance at $1/point, 0.30 lot
    assert store.get_meta("atr:a:52") is None  # his money in the sum, his trade not ours to drive
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    runner._manage_positions(account, day, [his], "USD", 1.0)
    assert _of(store, "park_lot_unattributed") == []
    assert len(_of(store, "foreign_positions")) == 1


def test_a_lot_without_a_stop_costs_the_book_a_number_instead_of_a_guess(tmp_path,
                                                                         monkeypatch) -> None:
    """A manual trade with no stop has no measurable risk, and the mirror has to say so.

    The fallback used to price `abs(price_open - 0)` as the stop distance: gold at 2000 became an ATR
    of 2000 and a risk figure nobody had ever asked the broker to hold. A ticket the mirror leaves
    NULL is read by every shard as a reason to stop opening, never as a risk of zero.
    """
    account, store, runner = _managed(tmp_path, monkeypatch)
    naked = _position(53, 0)
    naked.sl = 0.0
    assert runner._mirror_risk_usd(account, [naked], 1.0) == {}
    assert store.get_meta("atr:a:53") is None


def test_a_lot_with_nothing_to_price_keeps_the_rules_the_broker_holds(tmp_path,
                                                                     monkeypatch) -> None:
    """No entry ATR means the ATR rules stand down — not that this lot runs without protection.

    0.0 is what `strategy` already reads as "no ATR": the target and stop-distance rules disable
    themselves, while the broker's own stop, the giveback from the peak and the time cap keep running
    on numbers the position itself carries.
    """
    account, store, runner = _managed(tmp_path, monkeypatch)
    seen: list[float] = []
    monkeypatch.setattr(worker.strategy, "break_even_level",
                        lambda _a, _p, _s, atr: seen.append(atr) or None)
    monkeypatch.setattr(worker.strategy, "exit_reason", lambda *a, **k: "")
    naked = _position(41, account.magic)
    naked.sl = 0.0
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    runner._manage_positions(account, day, [naked], "USD", 1.0)
    assert seen == [0.0]
    assert store.get_meta("atr:a:41") is None  # nothing was persisted as this lot's entry ATR
    assert len(_of(store, "lot_atr_unpriced")) == 1
    runner._manage_positions(account, day, [naked], "USD", 1.0)
    assert len(_of(store, "lot_atr_unpriced")) == 1  # and it is not repeated every cycle


def test_a_locked_journal_does_not_get_a_forged_entry_atr(tmp_path, monkeypatch) -> None:
    """A note this process cannot read is a note it must not write.

    The old code treated a locked read as an absent one, computed the ATR from the standing stop and
    persisted it as the lot's entry ATR — the authorship note every later cycle believes. Under a real
    lock nothing can be journalled at all (the announcement waits for the next readable cycle), but the
    guarantee that holds in both is that no number is invented for a lot someone else sized.
    """
    import sqlite3

    account, store, runner = _managed(tmp_path, monkeypatch)

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "get_meta", locked)
    monkeypatch.setattr(store, "set_meta", locked)
    naked = _position(44, account.magic)
    assert runner._atr_entry(account, naked, _spec()) is None
    with sqlite3.connect(store.path) as conn:
        rows = conn.execute("SELECT key FROM meta WHERE key LIKE 'atr:%'").fetchall()
    assert rows == []


def test_an_emptied_manual_book_is_said_once(tmp_path, monkeypatch) -> None:
    """The operator closing his own trades is a fact worth one line, and the line has an end."""
    account, store, runner = _managed(tmp_path, monkeypatch)
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    runner._manage_positions(account, day, [_position(60, 0)], "USD", 1.0)
    runner._manage_positions(account, day, [], "USD", 1.0)
    assert len(_of(store, "manual_lots_cleared")) == 1
    runner._manage_positions(account, day, [], "USD", 1.0)
    assert len(_of(store, "manual_lots_cleared")) == 1  # an empty book is said once, not per cycle


def test_a_journal_rebuilt_under_the_park_does_not_arm_the_day_stop(tmp_path) -> None:
    """An empty book beside an existing money trail is amnesia, not a new day.

    This is the state a scratch script with the wrong path leaves on this machine: the `.jsonl`
    copy of the trail survives, the SQLite book does not. Re-taking the baseline from today's
    equity would move the daily stop onto the number the losses already produced, so the day stays
    unmeasured — which holds entries — until the operator resets it on purpose.
    """
    path = tmp_path / "book.sqlite"
    # The state a loss leaves behind: the paper trail of a park that traded, and no SQLite book.
    (tmp_path / "book.jsonl").write_text(
        json.dumps({"ts": "2026-11-04T09:00:00+00:00", "account": "a", "kind": "closed",
                    "ticket": 51}) + "\n", encoding="utf-8")

    store = Store(path)
    assert store.created_new and store.trail_existed
    store.equity("a", 1, 900.0, 900.0, 0, 0.0)
    engine = conductor.Conductor(FleetConfig(), store, shards=[0])
    engine._remember_baseline(store.portfolio(["a"]))
    assert engine.day_start_equity is None
    kinds = [kind for _ts, _a, kind, _p in store.recent_events(40)]
    assert "journal_recreated" in kinds and "new_day" not in kinds

    store.event("cli", "reset_day", host=conductor.local_host())  # the operator's answer
    later = conductor.Conductor(FleetConfig(), store, shards=[0])
    later._remember_baseline(store.portfolio(["a"]))
    assert later.day_start_equity == pytest.approx(900.0)
    assert [k for _ts, _a, k, _p in store.recent_events(40)].count("journal_recreated") == 1


# --- the shard reads its registry, and the broker reads its own contract -----------------------
def _registry_yaml(name: str = "exness", login: int = 777001) -> str:
    return (f"accounts:\n  - name: {name}\n    login: {login}\n"
            f"    server: Exness-MT5Real26\n    mode: demo\n    shard: 0\n"
            f"    symbols: [EURUSD]\n")


def _drive_shard(tmp_path, monkeypatch, steps, *, cycles: int):
    """Run the shard loop with its entry door counted, mutating the registry between cycles.

    A shard cycles until it is killed, so `time.sleep` is the only seam: step k runs after cycle k,
    and the driver ends the run by raising out of a later sleep.

    Two of the three things returned are lists, because the shard has two jobs. `traded` counts the
    cycles that were allowed to open; `runs` counts every cycle that ran, with the entry door written
    next to it. A registry the process can no longer read closes the first and must never close the
    second: the lots the last good registry put into the market still have to meet a cycle.
    """
    from fleet import shard

    registry = tmp_path / "accounts.yaml"
    registry.write_text(_registry_yaml(), encoding="utf-8")
    store_path = tmp_path / "book.sqlite"
    traded: list[str] = []
    runs: list[bool] = []
    slept: list[int] = []

    def fake_run_shard(config, store, index, **kwargs):
        allowed = bool(kwargs.get("allow_entries", True))
        runs.append(allowed)
        if allowed:
            traded.append(config.accounts[0].name)
        return []

    monkeypatch.setattr(shard.worker, "run_shard", fake_run_shard)

    def fake_sleep(seconds):
        slept.append(len(slept) + 1)
        step = steps[len(slept) - 1] if len(slept) <= len(steps) else None
        if step is not None:
            step(registry)
        if len(slept) >= cycles:
            raise SystemExit("end of the driven run")

    monkeypatch.setattr(shard.time, "sleep", fake_sleep)
    with pytest.raises(SystemExit):
        shard.main(["--shard", "0", "--config", str(registry), "--store", str(store_path)])
    return traded, runs, Store(store_path)


def test_a_registry_the_shard_cannot_read_stops_it_from_trading(tmp_path, monkeypatch) -> None:
    """The loop swallowed every exception, including the one that says the registry is broken.

    A reload that raised fell into the same handler as a bad tick: sleep, then cycle again on the
    config loaded while the file was still sane — accounts, caps and modes the registry on disk no
    longer describes, silently, every 90 seconds.
    """
    def break_it(path: pathlib.Path) -> None:
        path.write_text("accounts: [\n", encoding="utf-8")

    traded, runs, store = _drive_shard(tmp_path, monkeypatch, [break_it], cycles=3)
    assert traded == ["exness"]
    assert runs == [True, False, False], "осколочный цикл замёрз вместе с реестром"
    rejected = _of(store, "config_rejected")
    assert len(rejected) == 1 and rejected[0]["error"] and rejected[0]["shard"] == 0
    assert _of(store, "config_reloaded") == []


def test_a_registry_that_fails_validation_is_a_halt_too(tmp_path, monkeypatch) -> None:
    """YAML that parses and is wrong is the common case: `load` raises ConfigError, not a traceback."""
    def break_it(path: pathlib.Path) -> None:
        path.write_text("accounts: []\nphantom: 1\n", encoding="utf-8")

    traded, runs, store = _drive_shard(tmp_path, monkeypatch, [break_it], cycles=3)
    assert traded == ["exness"] and runs == [True, False, False]
    assert len(_of(store, "config_rejected")) == 1


def test_a_vanished_registry_is_a_refusal_not_a_permission(tmp_path, monkeypatch) -> None:
    """The reload used to sit behind `path.exists()`: delete the file and the shard kept trading.

    The config in memory is the last good one, which is exactly the stale registry this module
    promises not to trade — and the guard made the silence permanent, because a missing file never
    changes its stamp.
    """
    traded, runs, store = _drive_shard(tmp_path, monkeypatch, [lambda path: path.unlink()],
                                       cycles=3)
    assert traded == ["exness"] and runs == [True, False, False]
    assert len(_of(store, "config_rejected")) == 1


def test_the_shard_trades_again_the_minute_the_registry_is_readable(tmp_path, monkeypatch) -> None:
    """A halt that cannot end is an outage: recovery has to be as loud as the refusal."""
    def break_it(path: pathlib.Path) -> None:
        path.write_text("accounts: []\nphantom: 1\n", encoding="utf-8")

    def fix_it(path: pathlib.Path) -> None:
        path.write_text(_registry_yaml(), encoding="utf-8")

    traded, runs, store = _drive_shard(tmp_path, monkeypatch, [break_it, fix_it], cycles=4)
    assert traded == ["exness"] * 3
    assert runs == [True, False, True, True], runs
    reloaded = _of(store, "config_reloaded")
    assert len(reloaded) == 1 and reloaded[0]["recovered_from"]


def _stops_spec(stops_level: int = 0, freeze_level: int = 0) -> broker.BrokerSymbol:
    return dataclasses.replace(_spec(), stops_level=stops_level, freeze_level=freeze_level)


def test_a_geometry_the_broker_bans_is_named_before_anything_is_sent() -> None:
    """`trade_stops_level` is the server's own minimum, measured from the live price, per side.

    A buy hangs its stop below the price and its target above it; a sell is the mirror image. A level
    on the wrong side is not a tight stop — it is a plan that crosses the market.
    """
    tight = _stops_spec(stops_level=300)  # 300 points = 0.0030
    assert "стоп" in broker.stops_refusal(tight, True, 1.10002, 1.098, 1.104)
    assert "тейк" in broker.stops_refusal(tight, True, 1.10002, 1.096, 1.10202)
    assert broker.stops_refusal(tight, True, 1.10002, 1.096, 1.10402) == ""
    assert "стоп" in broker.stops_refusal(tight, False, 1.10000, 1.10200, 1.096)
    assert broker.stops_refusal(tight, False, 1.10000, 1.10400, 1.096) == ""
    assert "стоп" in broker.stops_refusal(tight, True, 1.10002, 1.104, 1.106)
    # A server that names no minimum still takes a plan whose levels are on their own sides.
    assert broker.stops_refusal(_spec(), True, 1.10002, 1.10001, 1.10003) == ""


def test_a_level_that_crossed_the_price_is_refused_at_stops_level_zero() -> None:
    """The named minimum is a distance, not the side rule — and this broker names no distance.

    Measured 2026-11-05 on MetaQuotes-Demo, where all five park symbols answer
    `trade_stops_level = 0` and `trade_freeze_level = 0`. Asked non-committing through
    `order_check`, and once for real through a modify of our own 0.01 USDJPY lot: a long's stop one
    point above the live bid is `invalid stops (10016)`, the same point below it is `done`, a level
    sitting exactly at the bid is accepted, and a target under the bid is 10016 even though it is
    still above the ask. The server measures both levels against the side the position closes at;
    a zero minimum removes the distance and leaves the side.
    """
    free = _stops_spec()
    bid, ask, point = 1.10000, 1.10002, 0.00001
    assert "по ту сторону" in broker.stops_refusal(free, True, bid, bid + point, ask + 0.003)
    assert "по ту сторону" in broker.stops_refusal(free, True, bid, bid - 0.003, bid - point)
    assert "по ту сторону" in broker.stops_refusal(free, False, ask, ask - point, bid - 0.003)
    assert "по ту сторону" in broker.stops_refusal(free, False, ask, ask + 0.003, ask + point)
    # Equality is what the server took, so the guard may not invent a distance it was never given.
    assert broker.stops_refusal(free, True, bid, bid, ask) == ""
    assert broker.stops_refusal(free, True, bid, bid - point, ask + point) == ""
    assert broker.stops_refusal(free, False, ask, ask, bid) == ""


def test_a_stop_inside_the_spread_is_refused_before_the_send(monkeypatch) -> None:
    """The entry fills on the far side of the spread, but the server judges that stop by the bid.

    The guard used to measure the plan against the fill price, which is one spread too generous for
    a stop and one spread too strict for a target: a level between the bid and the ask of a buy read
    as clear space and went out to a market that answered 10016 for it.
    """
    fake = FakeMT5(results=[_reply()])
    monkeypatch.setattr(broker, "mt5", fake)
    with pytest.raises(broker.StopsRefused, match="по ту сторону"):
        broker.market_order(7000000, _spec(), "buy", 0.02, 1.10001, 1.10400, 0.002, 2.0)
    assert fake.sent == []
    # The same level with the same distance on the far side of the bid is the plan the broker takes.
    assert broker.market_order(7000000, _spec(), "buy", 0.02, 1.09999, 1.10400, 0.002, 2.0)
    assert fake.sent[-1]["price"] == 1.10002, "order goes out at the ask; the guard measured the bid"


def test_the_break_even_guard_sees_the_price_the_ask_will_meet(tmp_path, monkeypatch) -> None:
    """The park asks with the freshest tick it can read, not with the one the cycle opened on.

    Measured 2026-11-05: two cycles in a row sent a break-even stop into a market that had already
    fallen through that level, and both answers were `invalid stops (retcode 10016)`. The pre-flight
    guard stayed silent because it measured the level against a price read before the peak write and
    before `break_even_level` re-read the book — and with `trade_stops_level = 0` on this broker the
    side rule was the only one that could have seen it.
    """
    account, store, runner = _managed(tmp_path, monkeypatch)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: _stops_spec())
    book = {"bid": 1.101}
    monkeypatch.setattr(worker.mt5, "symbol_info_tick",
                        lambda name: types.SimpleNamespace(bid=book["bid"], ask=book["bid"]))

    def break_even(*args, **kwargs) -> float:
        book["bid"] = 1.099  # the market moves between the cycle's read and the ask
        return 1.1005

    monkeypatch.setattr(worker.strategy, "break_even_level", break_even)
    sent: list[str] = []
    monkeypatch.setattr(worker.broker, "modify_position", lambda *a, **k: sent.append("modify"))
    monkeypatch.setattr(worker.broker, "close_position",
                        lambda position, *a, **k: sent.append("close") or float(position.volume))
    monkeypatch.setattr(worker.strategy, "exit_reason", lambda *a, **k: None)
    day = worker.risk.DayState(day="2026-11-05", start_balance=1000.0)
    positions = [_position(41, account.magic)]
    assert runner._manage_positions(account, day, positions, "USD", 1.0) == 0
    assert sent == [], "уровень уже пересечён ценой — просить нечего"
    rows = _of(store, "break_even_refused")
    assert len(rows) == 1 and "по ту сторону" in rows[0]["detail"]


def test_a_lot_inside_the_freeze_band_is_locked_and_says_so() -> None:
    """`trade_freeze_level` locks a position the moment price comes near its standing stop.

    The check is on the levels the broker already holds, not on the ones we are about to ask for:
    asking is what the server forbids.
    """
    frozen = _stops_spec(freeze_level=10)
    assert "блокирует" in broker.stops_refusal(frozen, True, 1.10100, 1.10090, 1.105,
                                              current_sl=1.10090)
    assert broker.stops_refusal(frozen, True, 1.10100, 1.10090, 1.105,
                                current_sl=1.10050) == ""


def test_an_order_the_server_would_reject_is_never_sent(monkeypatch) -> None:
    """A refusal costs a journal line; a send costs money in flight and a retcode read after the fact."""
    fake = FakeMT5(results=[_reply()])
    monkeypatch.setattr(broker, "mt5", fake)
    with pytest.raises(broker.StopsRefused, match="стоп"):
        broker.market_order(7000000, _stops_spec(stops_level=300), "buy", 0.02,
                            1.098, 1.104, 0.002, 2.0)
    assert fake.sent == []


def test_a_break_even_the_server_would_reject_is_said_once_and_never_sent(tmp_path,
                                                                         monkeypatch) -> None:
    """A rejected repair of a live stop must not strand the exit rules that run after it."""
    account, store, runner = _managed(tmp_path, monkeypatch)
    monkeypatch.setattr(worker.broker, "symbol_exact",
                        lambda name: _stops_spec(stops_level=100))
    store.save_positions("a", [_position(41, account.magic)])
    sent: list[str] = []
    monkeypatch.setattr(worker.broker, "modify_position", lambda *a, **k: sent.append("modify"))
    monkeypatch.setattr(worker.broker, "close_position",
                        lambda position, *a, **k: sent.append("close") or float(position.volume))
    monkeypatch.setattr(worker.strategy, "break_even_level", lambda *a, **k: 1.1005)
    exits: list[int] = []
    monkeypatch.setattr(worker.strategy, "exit_reason", lambda *a, **k: exits.append(1) or None)
    day = worker.risk.DayState(day="2026-11-04", start_balance=1000.0)
    positions = [_position(41, account.magic)]
    assert runner._manage_positions(account, day, positions, "USD", 1.0) == 0
    assert sent == []
    rows = _of(store, "break_even_refused")
    assert len(rows) == 1 and rows[0]["ticket"] == 41 and "стоп" in rows[0]["detail"]
    assert exits  # the lot is still managed: only the repair stands down
    runner._manage_positions(account, day, positions, "USD", 1.0)
    assert len(_of(store, "break_even_refused")) == 1  # the same fact is said once


def test_a_settled_token_close_without_a_price_is_counted_and_never_crashes(
        tmp_path) -> None:
    """The row the worker writes today killed the reader that has to report it.

    `priced=False` journalls `profit=None, profit_usd=None` beside a known rate (the history read
    failed, so nobody has the broker's number). That row is settled and it is attributable, and the
    old `done` asked for its dollars anyway: `float(None)` raised out of `ledger`, the CLI exited 1
    and printed nothing at all — the one book the operator reads was unreadable because of a trade
    whose outcome is simply not known yet.
    """
    from fleet import candidates

    store = Store(tmp_path / "book.sqlite")
    store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02, ticket=77,
                ticket_source="token", currency="USD", rate_to_usd=1.0,
                drift_points=25.0, drift_cost=0.5, drift_cost_usd=0.5, risk_usd=2.0)
    store.event("a", candidates.CLOSED, symbol="EURUSD", ticket=77, reason="стоп/тейк брокера",
                by="брокер", profit=None, currency="USD", rate_to_usd=1.0, profit_usd=None)
    book = candidates.ledger(store)
    assert book["closed"] == 0 and book["profit_gross_usd"] == 0.0
    assert book["win_rate"] is None and book["flat"] == 0
    assert book["settled_unpriced"] == 1
    row = book["trades"][0]
    assert row["settled"] and row["attributable"] and row["profit_gross_usd"] is None
    assert row["guessed_profit_usd"] is None  # its dollars are unknown, not merely untrusted


# --- the broker's day, and a clock that slips backwards ------------------------------------------

class _Clock:
    """Four quotes of different ages, plus a count of how often the terminal was asked for bars."""

    TIMEFRAME_D1 = 8

    def __init__(self, ticks: dict, bars: dict):
        self.ticks = ticks
        self.bars = bars
        self.bars_read: list[str] = []

    def symbol_info(self, name):
        return types.SimpleNamespace(visible=True)

    def symbols_get(self):
        return []

    def symbol_info_tick(self, name):
        return self.ticks.get(name)

    def copy_rates_from_pos(self, name, timeframe, start, count):  # noqa: ARG002
        self.bars_read.append(name)
        bar = self.bars.get(name)
        return None if bar is None else [{"time": bar}]  # rates arrive as structured rows


def test_the_clock_is_the_newest_quote_not_the_first_that_answers(monkeypatch) -> None:
    """A freshly raised window answered with a day two days back, and the book believed it.

    Measured on the live park on 2026-11-05: `book.server_day` was 2026-11-05 for the first cycles
    of a window and 2026-11-03 four minutes later, with nothing about the account having changed.
    The clock asked four symbols and took the first answer; EURUSD, not yet subscribed in that
    terminal, had no live tick and answered from the cached daily bar of the last time it was
    synced — history, two days old, in place of the quote the same terminal was already holding.
    """
    clock = _Clock({"EURUSD": types.SimpleNamespace(time=0),  # not subscribed: no quote yet
                    "XAUUSD": types.SimpleNamespace(time=1793843760)},  # 2026-11-05T01:56Z
                   {"EURUSD": 1793746800,  # 2026-11-03T23:00Z: the stale bar that used to win
                    "GBPUSD": 1793826000})  # 2026-11-04T21:00Z
    monkeypatch.setattr(broker, "mt5", clock)
    assert broker.server_timestamp() == 1793843760
    assert clock.bars_read == []  # a live tick answers the clock; history is never consulted

    weekend = _Clock({}, {"EURUSD": 1793746800, "GBPUSD": 1793826000})
    monkeypatch.setattr(broker, "mt5", weekend)
    assert broker.server_timestamp() == 1793826000  # no tick anywhere: the newest bar is the day

    mute = _Clock({}, {})
    monkeypatch.setattr(broker, "mt5", mute)
    assert broker.server_timestamp() == 0.0
    # Only a terminal that says nothing at all hands the day over to the host clock.
    assert worker.risk.server_day_now() == time.strftime("%Y-%m-%d", time.gmtime())


def _day_clock(tmp_path, monkeypatch):
    """A worker on a demo account whose day is measured and whose clock can be moved by hand."""
    from fleet import risk as risk_mod
    from fleet.config import Account, FleetConfig

    account = Account(name="a", login=1, server="S", mode="demo")
    store = Store(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store, allow_live=True)
    clock = {"day": "2026-11-05"}
    monkeypatch.setattr(risk_mod, "server_day_now", lambda: clock["day"])
    monkeypatch.setattr(risk_mod, "daily_net_flow", lambda _day_start: 0.0)
    monkeypatch.setattr(risk_mod, "daily_realized", lambda _day_start: 0.0)
    monkeypatch.setattr(risk_mod, "positions_for", lambda _acc: [])

    snapshot_class = risk_mod.AccountSnapshot

    def read(day: str, money: float):
        return snapshot_class(login=1, server="S", currency="USD", balance=money,
                              equity=money, margin_free=money, leverage=100,
                              positions=0, floating=0.0, server_day=day)

    answering = [read("2026-11-05", 1000.0)]
    monkeypatch.setattr(risk_mod, "AccountSnapshot",
                        types.SimpleNamespace(read=lambda *a, **k: answering[0]))
    monkeypatch.setattr(worker.Worker, "_manage_positions", lambda self, *a, **k: 0)
    judged: list = []
    monkeypatch.setattr(worker.Worker, "_try_entries",
                        lambda self, _a, _s, _p, day, _r: judged.append(day) or 0)
    assert runner._cycle(account, allow_entries=True).status == "ok"
    # The day closes fifteen dollars down: the stop is armed and new entries are denied the account.
    store.save_day("a", "2026-11-05", 1000.0, -15.0, 1)
    return account, store, runner, clock, answering, read, judged


def test_a_stale_quote_does_not_rewind_the_day_a_stop_was_armed_on(tmp_path,
                                                                   monkeypatch) -> None:
    """The portfolio day already refuses to go back; the account's day must refuse too.

    `DayState` compares days as strings, so a day that moved backwards is simply `not stored[0]` and
    reads as a new one: the base moves under equity that has already lost, `realized` is forgotten
    and `stopped` returns to False. An account denied entries at its daily stop takes them again the
    same evening, and the book row it writes carries the old day into `live_server_day`, whose MIN
    would drag the portfolio back with it.
    """
    from fleet import risk as risk_mod

    account, store, runner, clock, answering, read, judged = _day_clock(tmp_path, monkeypatch)

    clock["day"] = "2026-11-03"
    answering[0] = read("2026-11-03", 985.0)
    assert runner._cycle(account, allow_entries=True).status == "ok"
    assert store.day("a")[:2] == ("2026-11-05", 1000.0)  # base and day held, not rebased
    assert store.day("a")[3] is True  # the armed stop survived the clock
    assert store.live_server_day(["a"]) == "2026-11-05"  # the book row did not regress either
    assert judged[-1].stopped is True
    allowed, reason = risk_mod.can_open(account, answering[0], judged[-1], [])
    assert not allowed and reason == "дневной стоп счёта сработал"
    assert runner._cycle(account, allow_entries=True).status == "ok"
    # The second stale cycle is the same fact: the hold is reported once per window, not per cycle.
    assert len(_of(store, "day_clock_regressed")) == 1

    clock["day"] = "2026-11-05"
    answering[0] = read("2026-11-05", 985.0)
    assert runner._cycle(account, allow_entries=True).status == "ok"
    assert len(_of(store, "day_clock_resumed")) == 1
    # Each direction keeps its own clock: the recovery line is not throttled away by the hold it
    # ends. A shared one did exactly that — the journal opened a hold and never closed it.
    assert float(store.get_meta("dayclock:live:a")) > 0.0
    assert float(store.get_meta("dayclock:held:a")) > 0.0
    assert store.day("a")[3] is True  # and the day still ends stopped


def test_a_flapping_clock_costs_the_journal_two_lines_not_one_per_cycle(tmp_path,
                                                                        monkeypatch) -> None:
    """Held every cycle, said once per direction: the same terminal answered live and stale by turns.

    Measured on the live demo account on 2026-11-05, the regression and the recovery arrived on
    nearly consecutive cycles — twelve lines in nine and a half minutes. The hold is not news every
    thirty seconds, and a journal the operator has to read cannot be filled with copies of one
    sentence: `dayclock:held:<account>` and `dayclock:live:<account>` remember when each sentence
    was last told, and the next one of the same direction waits out the window.
    """
    account, store, runner, clock, answering, read, _ = _day_clock(tmp_path, monkeypatch)
    for day in ("2026-11-03", "2026-11-05", "2026-11-03", "2026-11-03", "2026-11-05"):
        clock["day"] = day
        answering[0] = read(day, 985.0)
        assert runner._cycle(account, allow_entries=True).status == "ok"
        assert store.day("a")[:2] == ("2026-11-05", 1000.0)  # held through every answer
    regressed = _of(store, "day_clock_regressed")
    resumed = _of(store, "day_clock_resumed")
    assert len(regressed) == 1 and len(resumed) == 1
    assert regressed[0]["day"] == "2026-11-05" and regressed[0]["clock"] == "2026-11-03"
