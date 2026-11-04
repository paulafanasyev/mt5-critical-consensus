"""Regression tests for defects critics proved by running the old code.

Order idempotency, slippage money caps, partial fills, conductor hand-off and resume, crash-loop
backoff, the single-shard rule, and real-account gating — each is a bug that once shipped.
"""
from __future__ import annotations

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
def _note(side: str, asked: float, filled: float, volume: float = 0.02, ticket: int = 7):
    signal = types.SimpleNamespace(side=side, price=asked, stop=filled - 0.002 if side == "buy"
                                   else filled + 0.002, target=filled + 0.004 if side == "buy"
                                   else filled - 0.004, atr=0.001, reason="test")
    execution = types.SimpleNamespace(price=filled, volume=volume, position_ticket=ticket)
    spec = types.SimpleNamespace(name="EURUSD", point=0.00001, tick_size=0.00001, tick_value=1.0)
    return candidates.entry_note(signal, execution, spec, 2.0)


def test_the_candidate_bill_is_paid_in_the_direction_the_trade_went() -> None:
    """Slippage we profited from is a rebate, slippage we paid for is a cost. Signed, not absolute."""
    worse = _note("buy", 1.10000, 1.10025)   # market ran away before the fill
    assert worse["drift_points"] == 25.0
    assert worse["drift_usd_per_lot"] == 25.0  # 1.0 per 0.00001 -> 100 000 USD per lot per unit
    assert worse["drift_cost_usd"] == 0.5      # 0.50 USD on 0.02 lot
    better = _note("buy", 1.10000, 1.09980)   # filled below the signalled price
    assert better["drift_points"] == -20.0
    assert better["drift_cost_usd"] == -0.4
    sold = _note("sell", 1.10000, 1.09980)    # a sell filled lower is worse, not better
    assert sold["drift_points"] == 20.0
    assert sold["drift_cost_usd"] == 0.4


def test_a_candidate_with_no_contract_size_is_left_blank_instead_of_guessed() -> None:
    signal = types.SimpleNamespace(side="buy", price=1.10, stop=1.098, target=1.104,
                                   atr=0.001, reason="test")
    execution = types.SimpleNamespace(price=1.10, volume=0.02, position_ticket=7)
    blind_spec = types.SimpleNamespace(name="EURUSD", point=0.00001, tick_size=0.0, tick_value=0.0)
    bare = candidates.entry_note(signal, execution, blind_spec, 2.0)
    assert bare["drift_points"] == 0.0
    assert "drift_cost_usd" not in bare  # no broker contract -> no dollars, no invented ones
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
                ticket=7, drift_points=25.0, drift_cost_usd=0.5, risk_usd=2.0,
                risk_reward=2.0)
    store.event("a", candidates.KIND, symbol="XAUUSD", side="buy", volume=0.01,
                ticket=0, drift_points=3.0, drift_cost_usd=0.1, risk_usd=1.0)
    store.event("a", "closed", symbol="EURUSD", ticket=7, reason="trailing", profit=3.2)
    store.event("a", "closed", symbol="EURUSD", reason="old row, no ticket")  # pre-ticket journal
    book = candidates.ledger(store)
    assert book["candidates"] == 2
    assert book["closed"] == 1
    assert book["blind"] == 1            # ticket 0 is counted, nothing is inferred about it
    assert book["open"] == 0
    assert book["profit_gross_usd"] == 3.2
    assert book["win_rate"] == 100.0
    assert book["drift_cost_usd"] == 0.6  # the drift bill covers both entries, closed or not
    assert [t["exit"] for t in book["trades"]] == ["trailing", ""]  # oldest entry first


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
                              ticket_source="token"), _spec(), 2.0)
    assert note["drift_points"] == 0.0
    assert note["drift_cost_usd"] == 0.0


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
                ticket=7, drift_points=25.0, drift_cost_usd=0.5, risk_usd=2.0)
    store.event("gtc", "closed", symbol="EURUSD", ticket=7, reason="стоп", profit=-77.7)
    book = candidates.ledger(store)
    assert book["closed"] == 0
    assert book["open"] == 1
    assert book["profit_gross_usd"] == 0.0


def test_one_close_is_never_paid_out_twice(tmp_path) -> None:
    """A replayed fill writes two candidates for one ticket: one of them gets the close, not both."""
    store = Store(tmp_path / "book.sqlite")
    for _ in range(2):
        store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02,
                    ticket=7, drift_points=25.0, drift_cost_usd=0.5, risk_usd=2.0)
    store.event("a", "closed", symbol="EURUSD", ticket=7, reason="тейк", profit=50.0)
    book = candidates.ledger(store)
    assert book["candidates"] == 2
    assert book["closed"] == 1 and book["open"] == 1
    assert book["profit_gross_usd"] == 50.0


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
