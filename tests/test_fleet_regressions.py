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
    monkeypatch.setattr(conductor.terminals, "stop_other_fleet", lambda *a, **k: None)
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


def test_day_flows_are_published_only_after_equity(tmp_path, monkeypatch) -> None:
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


def test_port_owner_caches_a_free_port(tmp_path, monkeypatch) -> None:
    calls = {"n": 0}

    class FakeRun:
        def __init__(self, *a, **k):
            calls["n"] += 1
            self.stdout = ""
            self.stderr = ""

    monkeypatch.setattr(worker.subprocess, "run", FakeRun, raising=False)
    monkeypatch.setattr(worker, "_PORT_OWNER_CACHE", {"ts": 0.0, "pid": 0}, raising=False)
    worker.port_owner()
    worker.port_owner()
    assert calls["n"] == 1  # the second lookup is served from the cache


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
