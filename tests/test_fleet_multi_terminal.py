"""The park addresses terminals one by one, and proves which window it is driving.

Founded on a measurement, not on folklore: with three MT5 terminals running on one box each published
its own named pipe, and MetaTrader5.initialize(path=...) answered from the terminal whose executable
was named — from two processes at the same time. The single API port every previous design guarded
was bound by exactly one of the three and did not decide who answered. So these tests pin the two
invariants that make several brokers safe to run at once:

  * an account is traded only through a terminal that proved it is that account's own copy, and
  * no code path kills a terminal that belongs to someone else, fleet or not.
"""
from __future__ import annotations

import argparse
import dataclasses
import io
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_cli  # noqa: E402
from fleet import conductor, terminals, worker  # noqa: E402
from fleet import config as fleet_config  # noqa: E402
from fleet.config import Account, FleetConfig, PortfolioLimits, load  # noqa: E402
from fleet.store import Store, host_keys  # noqa: E402

# The broker mode a census row carries, taken from the library the worker compares against: the real
# MetaTrader5 package names DEMO 2 and REAL 0, the CI stub in `tests/conftest.py` names them the other
# way round, so a test that wrote one of these as a number would mean the opposite on the other box.
_DEMO_MODE = int(worker.mt5.ACCOUNT_TRADE_MODE_DEMO)
_REAL_MODE = int(worker.mt5.ACCOUNT_TRADE_MODE_REAL)


def make_account(name: str, login: int, server: str, **overrides) -> Account:
    fields = {"name": name, "login": login, "server": server, "mode": "demo", "network": "direct"}
    fields.update(overrides)
    return Account(**fields)


class FakeMT5:
    """The library surface attach() touches: initialize, identity, account, shutdown."""

    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self, answering_dir: str | None, login: int, server: str,
                 trade_mode: int = 0) -> None:
        self.answers_with = answering_dir
        self.login = login
        self.server = server
        self.trade_mode = trade_mode
        self.initialize_calls: list[dict] = []
        self.shutdowns = 0

    def initialize(self, **kwargs) -> bool:
        self.initialize_calls.append(kwargs)
        return True

    def shutdown(self) -> None:
        self.shutdowns += 1

    def last_error(self):
        return (-1, "fake")

    def terminal_info(self):
        if self.answers_with is None:
            return None
        return types.SimpleNamespace(path=self.answers_with)

    def account_info(self):
        return types.SimpleNamespace(login=self.login, server=self.server,
                                     trade_mode=self.trade_mode)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A worker whose terminal process control is recorded instead of executed."""
    events: dict[str, list] = {"launched": [], "stopped": [], "killed": []}

    def fake_launch(account, extra_args=()):
        events["launched"].append(str(account.terminal_exe))
        return types.SimpleNamespace(pid=4242)

    def fake_stop(account):
        events["stopped"].append(str(account.data_dir))

    def record_run(command, *args, **kwargs):
        events["killed"].append(command)
        return types.SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(terminals, "launch", fake_launch)
    monkeypatch.setattr(terminals, "stop", fake_stop)
    monkeypatch.setattr(terminals, "provision", lambda account, *a, **k: None)
    monkeypatch.setattr(terminals, "running_pid", lambda account: None)
    monkeypatch.setattr(terminals, "wait_authorized", lambda account, **k: True)
    monkeypatch.setattr(terminals.subprocess, "run", record_run)
    store = Store(tmp_path / "book.sqlite")
    return store, events


@pytest.fixture(autouse=True)
def _windows_are_alive(monkeypatch):
    """A census probe only addresses a pid that is still up, which PowerShell answers.

    In a test the process list it hands over IS the liveness decision: nothing here may shell out,
    and nothing here may silently drop every window because a query returned nothing.
    """
    monkeypatch.setattr(terminals, "pids_alive", lambda pids: set(pids))


@pytest.fixture
def attach(monkeypatch):
    """Replace the library the worker talks to, so no real terminal is ever addressed."""
    def _attach(engine, account, answering_dir, login=None, trade_mode=0):
        fake = FakeMT5(answering_dir, login if login is not None else account.login,
                       account.server, trade_mode)
        monkeypatch.setattr(worker, "mt5", fake)
        return engine.attach(account), fake
    return _attach


# --- identity -------------------------------------------------------------------------------

def test_attach_refuses_a_terminal_that_is_not_the_accounts_own(tmp_path, fleet, attach) -> None:
    store, _ = fleet
    account = make_account("exness", 777001, "Exness-MT5Real26")
    engine = worker.Worker(FleetConfig(accounts=[account]), store)
    other = str(pathlib.Path("C:/mt5-fleet/terminals/999-other/terminal64.exe").parent)
    error, fake = attach(engine, account, other)
    assert "другой терминал" in error
    assert fake.shutdowns == 1  # the wrong window is released, never traded


def test_attach_refuses_a_terminal_that_hides_its_path(tmp_path, fleet, attach) -> None:
    store, _ = fleet
    account = make_account("gtc", 888001, "GTC-Go")
    engine = worker.Worker(FleetConfig(accounts=[account]), store)
    error, fake = attach(engine, account, None)
    assert "не сообщил свой путь" in error
    assert fake.shutdowns == 1


def test_attach_accepts_its_own_terminal_whatever_the_letter_case(tmp_path, fleet, attach) -> None:
    store, _ = fleet
    account = make_account("gtc", 888001, "GTC-Go")
    engine = worker.Worker(FleetConfig(accounts=[account]), store)
    reported = str(account.data_dir).upper() + "\\"
    error, fake = attach(engine, account, reported)
    assert error == ""
    assert fake.shutdowns == 0
    # and the request was addressed to this account's own executable
    assert fake.initialize_calls[0]["path"].lower() == str(account.terminal_exe).lower()


def test_attach_still_refuses_a_foreign_login_on_the_right_terminal(tmp_path, fleet, attach) -> None:
    store, _ = fleet
    account = make_account("gtc", 888001, "GTC-Go")
    engine = worker.Worker(FleetConfig(accounts=[account]), store)
    error, _fake = attach(engine, account, str(account.data_dir), login=114929502)
    assert "чужой счёт" in error


def test_attach_still_refuses_a_mode_that_the_registry_gets_wrong(tmp_path, fleet, attach) -> None:
    store, _ = fleet
    account = make_account("gtc", 888001, "GTC-Go", mode="demo")
    engine = worker.Worker(FleetConfig(accounts=[account]), store)
    # trade_mode 2 is the broker saying "real": the registry claiming demo must not trade it.
    error, _fake = attach(engine, account, str(account.data_dir), trade_mode=2)
    assert "несовпадение режима" in error


# --- no killing for room ---------------------------------------------------------------------

def test_attaching_one_account_starts_its_terminal_and_touches_no_other(tmp_path, fleet, attach) -> None:
    store, events = fleet
    exness = make_account("exness", 777001, "Exness-MT5Real26")
    gtc = make_account("gtc", 888001, "GTC-Go")
    engine = worker.Worker(FleetConfig(accounts=[exness, gtc]), store)

    for account in (exness, gtc):
        error, _fake = attach(engine, account, str(account.data_dir))
        assert error == ""
    assert events["launched"] == [str(exness.terminal_exe), str(gtc.terminal_exe)]
    assert events["stopped"] == []
    assert events["killed"] == []  # no taskkill of anybody else's window


def test_no_surface_left_stops_terminals_it_does_not_own() -> None:
    # The old helpers mass-killed every fleet terminal, and every terminal on the box.
    assert not hasattr(terminals, "stop_other_fleet")
    assert not hasattr(terminals, "stop_all")
    assert not hasattr(worker, "port_is_free_or_ours")
    assert not hasattr(worker, "port_owner")


# --- several brokers in one registry ---------------------------------------------------------

def test_three_brokers_get_three_channels_and_one_park(tmp_path) -> None:
    registry = tmp_path / "accounts.yaml"
    registry.write_text(
        """
accounts:
  - name: exness
    login: 777001
    server: Exness-MT5Real26
    mode: demo
    shard: 0
    symbols: [EURUSD, XAUUSD]
  - name: gtc
    login: 888001
    server: GTC-Go-Live
    mode: demo
    shard: 1
    symbols: [EURUSD]
  - name: metaquotes
    login: 114929502
    server: MetaQuotes-Demo
    mode: demo
    shard: 2
    symbols: [USDJPY]
portfolio:
  max_total_risk_pct: 2.0
  daily_stop_pct: 2.0
""", encoding="utf-8")
    config = load(registry)
    assert config.shards() == [0, 1, 2]
    directories = {account.data_dir for account in config.accounts}
    assert len(directories) == 3  # no two brokers share a terminal copy
    engine = conductor.Conductor(config, Store(tmp_path / "book.sqlite"), shards=[0, 1, 2])
    assert engine.shards == [0, 1, 2]


def test_portfolio_limits_no_longer_claim_one_process_per_session() -> None:
    assert "max_worker_processes" not in {field.name for field in dataclasses.fields(PortfolioLimits)}


# --- several brokers: whose machine, whose clock ----------------------------------------------

def test_pinned_account_is_not_driven_from_another_machine(monkeypatch) -> None:
    from fleet import config as fleet_config

    monkeypatch.setattr(fleet_config, "local_host", lambda: "box-a")
    here = make_account("exness", 1, "Exness-MT5Real26", shard=0)
    there = make_account("gtc", 2, "GTC-Go-Live", shard=1, host="BOX-B")
    same_host_other_case = make_account("metaquotes", 3, "MetaQuotes-Demo", shard=2, host="Box-A")
    park = FleetConfig(accounts=[here, there, same_host_other_case])
    assert park.runs_here(here) and park.runs_here(same_host_other_case)
    assert not park.runs_here(there)
    assert [a.name for a in park.by_shard(1)] == []
    assert park.shards() == [0, 2]
    engine = conductor.Conductor(park, Store(":memory:"))
    assert engine.shards == [0, 2]  # no shard process for a machine that owns nothing here


def test_brokers_on_different_clocks_keep_the_portfolio_baseline(tmp_path, monkeypatch) -> None:
    """A shard going dark must not move the portfolio a day backwards and re-arm its daily stop."""
    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    store = Store(tmp_path / "book.sqlite")
    exness = make_account("exness", 1, "Exness-MT5Real26")     # GMT+0: still 2026-11-02
    gtc = make_account("gtc", 2, "GTC-Go-Live")                # GMT+3: rolled to 2026-11-03
    engine = conductor.Conductor(FleetConfig(accounts=[exness, gtc]), store, shards=[0])
    store.set_meta("portfolio_registry_hash", engine._registry_hash())
    store.set_meta("portfolio_day", "2026-11-03")
    store.set_meta("portfolio_day_start_equity", "1000")
    store.equity("exness", 1, 980.0, 980.0, 0, 0.0, server_day="2026-11-02")
    book = store.portfolio([exness.name, gtc.name])
    engine._remember_baseline(book)
    assert engine.day_start_equity == 1000.0  # MIN over live rows fell back; the day does not
    assert "дневной стоп" in engine._portfolio_stop(book)  # and the -2% stop still fires


def test_the_park_refuses_more_shards_than_one_desktop_holds() -> None:
    """A shard is a process holding GUI terminals: the ceiling is the Windows desktop heap."""
    accounts = [make_account(f"broker{i}", i + 1, "S", shard=i)
                for i in range(conductor.Conductor.MAX_SHARDS + 1)]
    park = FleetConfig(accounts=accounts)
    with pytest.raises(ValueError, match="max-shards"):
        conductor.Conductor(park, Store(":memory:"))
    engine = conductor.Conductor(park, Store(":memory:"), max_shards=len(accounts))
    assert len(engine.shards) == conductor.Conductor.MAX_SHARDS + 1


def test_cli_never_logs_into_an_account_pinned_to_another_box(tmp_path, monkeypatch, capsys) -> None:
    registry = tmp_path / "accounts.yaml"
    registry.write_text("""
accounts:
  - name: exness
    login: 1
    server: Exness-MT5Real26
    mode: demo
  - name: gtc
    login: 2
    server: GTC-Go-Live
    mode: demo
    host: box-b
""", encoding="utf-8")
    monkeypatch.setattr(fleet_config, "local_host", lambda: "box-a")
    run: list[str] = []

    class FakeWorker:
        def __init__(self, *a, **k):
            pass

        def run_account(self, account, allow_entries=True):
            run.append(account.name)
            return worker.WorkerResult(account.name, "flat", "")

    monkeypatch.setattr(fleet_cli, "worker", types.SimpleNamespace(
        Worker=FakeWorker, WorkerResult=worker.WorkerResult))
    store_path = tmp_path / "fleet.sqlite"
    assert fleet_cli.main(["--config", str(registry), "--store", str(store_path), "once"]) == 0
    assert run == ["exness"]  # the pinned broker is never even attached from here
    run.clear()
    code = fleet_cli.main(["--config", str(registry), "--store", str(store_path),
                           "once", "--account", "gtc"])
    assert code == 1
    assert "box-b" in capsys.readouterr().out


def _entry_harness(tmp_path, monkeypatch, answering):
    """One signal, one slot, and a terminal that answers as `answering` at send time."""
    from fleet import risk as risk_mod

    account = make_account("a", 1, "S")
    # The peer is on this machine's book: another broker's shard, not another host's. A row from a
    # machine this one does not run is not counted here at all (see the veto test below).
    peer = make_account("gtc", 2, "GTC-Go-Live")
    store = Store(tmp_path / "book.sqlite")
    # The day the entries are gated against has to be a measurement the conductor made, not an
    # absent one: an unmeasured day now refuses entries instead of reporting a 0 % day.
    store.set_portfolio_baseline(fleet_config.local_host(), "2026-11-03", 1000.0)
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    runner = worker.Worker(FleetConfig(accounts=[account, peer]), store, dry_run=False)
    spec = worker.broker.BrokerSymbol(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001,
                                      tick_value=1.0, volume_min=0.01, volume_max=100.0,
                                      volume_step=0.01, stops_level=0, freeze_level=0, spread=1,
                                      filling=3)
    signal = types.SimpleNamespace(base_symbol="EURUSD", side="buy", reason="test", price=1.10,
                                   stop=1.098, target=1.104, atr=0.001, stop_distance=0.002)
    sent: list[int] = []
    monkeypatch.setattr(worker.strategy, "signals", lambda acc: [signal])
    monkeypatch.setattr(worker.broker, "symbol", lambda base: spec)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: spec)
    monkeypatch.setattr(worker.broker, "open_positions", lambda *a, **k: [])
    monkeypatch.setattr(worker.broker, "market_order",
                        lambda *a, **k: sent.append(1) or types.SimpleNamespace(
                            volume=0.01, price=1.10, retcode=10009, token="t"))
    monkeypatch.setattr(worker.risk, "can_open", lambda *a, **k: (True, ""))
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=answering[0], server=answering[1]))
    snapshot = risk_mod.AccountSnapshot(login=1, server="S", currency="USD", balance=1000.0,
                                        equity=1000.0, margin_free=1000.0, leverage=100,
                                        positions=0, floating=0.0, server_day="2026-11-03")
    day = risk_mod.DayState(day="2026-11-03", start_balance=1000.0)
    # A counted field with one window: the entry gate refuses to open while the census is missing,
    # and an entry test has to be about the thing it tests, not about the census underneath it.
    _census(store, [_row(str(account.data_dir), 1)])
    return account, runner, store, sent, snapshot, day, spec


def test_an_order_is_refused_when_the_window_changed_its_login(tmp_path, monkeypatch) -> None:
    # The portable directory proves WHICH terminal answers; only the live login proves whose money.
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (999, "Someone-Else"))
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    opened = runner._try_entries(account, snapshot, [], day, 1.0)
    assert opened == 0 and sent == []
    assert any(kind == "identity_lost" for _, _, kind, _ in store.recent_events(20))
    assert not store.reserved_volume()  # the slot came back, the reservation did not leak


def test_the_same_order_is_placed_when_the_login_is_ours(tmp_path, monkeypatch) -> None:
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S"))
    opened = runner._try_entries(account, snapshot, [], day, 1.0)
    assert opened == 1 and sent == [1]


def _seed_day(store: Store, engine, day: str, base: float) -> None:
    """This machine's measured day, the way the conductor itself writes it."""
    host = conductor.local_host()
    store.set_portfolio_registry_hash(host, engine._registry_hash())
    store.set_portfolio_baseline(host, day, base)


def _peer_row(store: Store, symbol: str, risk_usd: float | None) -> None:
    """One open position of another broker, as its own shard mirrors it into this book."""
    position = types.SimpleNamespace(ticket=7, symbol=symbol, type=0, volume=0.05,
                                     price_open=1.10, profit=0.0, sl=1.098, tp=0.0, magic=7)
    store.save_positions("gtc", [position], {7: risk_usd})


def test_a_peers_stop_risk_is_counted_in_dollars_not_as_zero(tmp_path, monkeypatch) -> None:
    """An account on another broker holds contracts this terminal has never seen."""
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S"))
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    _peer_row(store, "EURUSDm", 250.0)  # 250 USD already at risk on the peer's broker
    opened = runner._try_entries(account, snapshot, [], day, 1.0)
    assert opened == 0 and sent == []
    events = [(kind, payload) for _, _, kind, payload in store.recent_events(20)]
    assert any(kind == "portfolio_hold" and "риска" in payload for kind, payload in events)


def test_the_order_goes_through_when_no_peer_risk_is_outstanding(tmp_path, monkeypatch) -> None:
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S"))
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    opened = runner._try_entries(account, snapshot, [], day, 1.0)
    assert opened == 1 and sent == [1]  # 2 USD of new risk against a 2% cap of 1000 USD


def test_a_filled_order_writes_how_many_lots_of_its_ticket_are_ours(tmp_path,
                                                                   monkeypatch) -> None:
    """The merge guard begins at the fill, not at the next cycle's photograph of the book.

    On a netting account the operator's lot can fold into this ticket before the park looks at it
    again, and a witness taken from the volume it then finds is his half carried by our trailing
    stop. The number written here is the one this park ordered.
    """
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S"))
    monkeypatch.setattr(worker.broker, "open_positions", lambda *a, **k: [
        types.SimpleNamespace(ticket=77, symbol="EURUSD", type=0, volume=0.01)])
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1 and sent == [1]
    assert store.lot_witnesses(account.name) == {77: pytest.approx(0.01)}


def test_a_second_lot_ordered_into_the_same_ticket_grows_our_share_of_it(tmp_path,
                                                                         monkeypatch) -> None:
    """Our own money entering a witnessed ticket adds to the witness; only watching never does.

    A netting account folds a second entry into the position it already has, so the ticket that used
    to hold 0.01 of ours holds 0.02 — and a guard that could not tell that from the operator's
    growth would stand off from our own lot and call it two people's money.
    """
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S"))
    account.strategy.max_positions_per_symbol = 2
    store.note_lot_witness(account.name, 77, 0.01)
    held = types.SimpleNamespace(ticket=77, symbol="EURUSD", type=0, volume=0.01,
                                 price_open=1.10, profit=0.0, sl=1.098, tp=0.0,
                                 magic=account.magic)
    monkeypatch.setattr(worker.broker, "open_positions", lambda *a, **k: [
        types.SimpleNamespace(ticket=77, symbol="EURUSD", type=0, volume=0.02)])
    assert runner._try_entries(account, snapshot, [held], day, 1.0) == 1 and sent == [1]
    assert store.lot_witnesses(account.name) == {77: pytest.approx(0.02)}


def test_an_unpriced_peer_stop_refuses_entries_instead_of_counting_zero(tmp_path,
                                                                        monkeypatch) -> None:
    account, runner, store, sent, snapshot, day, spec = _entry_harness(tmp_path, monkeypatch,
                                                                       (1, "S"))
    monkeypatch.setattr(worker.broker, "symbol_exact",
                        lambda name: spec if name == "EURUSD" else None)
    _peer_row(store, "GOLD.a", None)  # the peer's own terminal could not price this stop
    opened = runner._try_entries(account, snapshot, [], day, 1.0)
    assert opened == 0 and sent == []
    assert any(kind == "portfolio_risk_unpriced" for _, _, kind, _ in store.recent_events(20))


def test_the_day_stop_still_bites_on_a_broker_clock_that_is_already_ahead(tmp_path,
                                                                         monkeypatch) -> None:
    """The portfolio day is the slowest live clock, so an ahead clock is still inside that day."""
    from fleet import risk as risk_mod
    from fleet.config import local_host

    store = Store(tmp_path / "book.sqlite")
    account = make_account("gtc", 2, "GTC-Go-Live")
    runner = worker.Worker(FleetConfig(accounts=[account]), store)
    store.set_portfolio_baseline(local_host(), "2026-11-02", 1000.0)
    monkeypatch.setattr(risk_mod, "server_day_now", lambda: "2026-11-03")
    assert runner._portfolio_day_pct(980.0) == pytest.approx(-2.0)
    monkeypatch.setattr(risk_mod, "server_day_now", lambda: "2026-11-01")
    assert runner._portfolio_day_pct(980.0) == 0.0  # this clock has not reached the measured day


def test_a_suffixed_symbol_still_counts_in_its_class_on_every_broker() -> None:
    """Caps are per class, and every broker writes EURUSD as something else."""
    from fleet import naming

    assert naming.is_usd_pair("EURUSDm") and naming.is_usd_pair("EURUSD.a")
    assert naming.is_usd_pair("USDJPY.raw") and not naming.is_usd_pair("EURGBP")
    assert not naming.is_usd_pair("USDT")  # a stablecoin is not a dollar FX pair
    assert not naming.is_usd_pair("XAUUSDm") and naming.is_gold("GOLDm")
    # A platform that prefixes the feed name would otherwise fall out of every class cap.
    assert naming.is_usd_pair("PRO.EURUSD") and naming.is_usd_pair("FX:USDJPY")
    assert not naming.is_usd_pair("PRO.XAUUSD") and naming.is_gold("PRO.XAUUSD")
    assert not naming.is_usd_pair("") and not naming.is_usd_pair("EURUS")


def test_a_demand_for_shards_is_checked_against_this_machines_registry() -> None:
    assert conductor.parse_shards("0,1,0", [0, 1, 2]) == [0, 1]  # a repeated id is one shard
    with pytest.raises(ValueError, match="нет среди счетов"):
        conductor.parse_shards("7", [0, 1])   # a typo would run a park that trades nothing
    with pytest.raises(ValueError, match="целое число"):
        conductor.parse_shards("zero", [0, 1])
    assert conductor.parse_shards("", [0, 1]) == []


def test_the_portfolio_day_waits_for_the_slowest_live_clock(tmp_path, monkeypatch) -> None:
    """One GMT+3 broker crossing midnight must not move the baseline off a losing day."""
    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    store = Store(tmp_path / "book.sqlite")
    exness = make_account("exness", 1, "Exness-MT5Real26")   # GMT+0: still 2026-11-02
    gtc = make_account("gtc", 2, "GTC-Go-Live")              # GMT+3: rolled to 2026-11-03
    engine = conductor.Conductor(FleetConfig(accounts=[exness, gtc]), store, shards=[0])
    _seed_day(store, engine, "2026-11-02", 2000.0)
    store.equity("exness", 1, 980.0, 980.0, 0, 0.0, server_day="2026-11-02")
    store.equity("gtc", 2, 980.0, 980.0, 0, 0.0, server_day="2026-11-03")
    book = store.portfolio([exness.name, gtc.name])
    engine._remember_baseline(book)
    assert store.portfolio_baseline(conductor.local_host()) == ("2026-11-02", "2000.00")
    assert engine.day_start_equity == 2000.0  # not rebased onto 1960, which is the -2% itself
    assert "дневной стоп" in engine._portfolio_stop(book)


def test_a_row_left_by_another_machine_weighs_nothing_here(tmp_path, monkeypatch) -> None:
    """An account pinned elsewhere has no writer on this box, so its row is not this portfolio."""
    from fleet import config as fleet_config

    monkeypatch.setattr(fleet_config, "local_host", lambda: "box-a")
    park = FleetConfig(accounts=[make_account("exness", 1, "Exness-MT5Real26"),
                                 make_account("gtc", 2, "GTC-Go-Live", host="BOX-B")])
    store = Store(tmp_path / "book.sqlite")
    store.equity("exness", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    store.equity("gtc", 2, 900.0, 800.0, 0, 0.0, "USD", 1.0, "2026-11-02")
    mine = [a.name for a in park.local()]
    assert mine == ["exness"]
    assert store.portfolio(mine, fresh_only=False)["equity"] == 1000.0
    assert store.live_server_day(mine) == "2026-11-03"  # a ghost cannot hold the day back


def test_a_stop_this_broker_cannot_price_is_never_priced_here(tmp_path, monkeypatch) -> None:
    """A peer's stop costs money on the peer's contract; our own tick value would be a guess."""
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S"))
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    _peer_row(store, "EURUSD", None)  # resolvable on THIS terminal, and 100x cheaper there
    opened = runner._try_entries(account, snapshot, [], day, 1.0)
    assert opened == 0 and sent == []
    assert any(kind == "portfolio_risk_unpriced" for _, _, kind, _ in store.recent_events(20))


def test_this_accounts_own_row_is_still_priced_by_its_own_terminal(tmp_path,
                                                                  monkeypatch) -> None:
    """The row this shard owns is the one row it may price: it is the broker that holds it."""
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S"))
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    position = types.SimpleNamespace(ticket=5, symbol="EURUSD", type=0, volume=0.05,
                                     price_open=1.10, profit=0.0, sl=1.098, tp=0.0, magic=1)
    store.save_positions("a", [position], {})  # written before this shard had a rate: NULL
    opened = runner._try_entries(account, snapshot, [], day, 1.0)
    assert opened == 1 and sent == [1]  # 1 USD of own risk against a 2% cap of 1000 USD


def test_a_zero_dollar_stop_price_stays_unknown(tmp_path, monkeypatch) -> None:
    """A broker that describes no tick value prices a stop at 0.0, and 0.0 is not a measurement."""
    account, runner, store, _sent, _snapshot, _day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                            (1, "S"))
    degenerate = worker.broker.BrokerSymbol(name="EURUSDm", digits=5, point=0.00001,
                                            tick_size=0.00001, tick_value=0.0, volume_min=0.01,
                                            volume_max=100.0, volume_step=0.01, stops_level=0,
                                            freeze_level=0, spread=1, filling=3)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: degenerate)
    position = types.SimpleNamespace(ticket=9, symbol="EURUSDm", type=0, volume=0.05,
                                     price_open=1.10, profit=0.0, sl=1.098, tp=0.0, magic=1)
    assert runner._mirror_risk_usd(account, [position], 1.0) == {}


def test_a_row_priced_at_zero_by_another_broker_refuses_entries(tmp_path,
                                                                monkeypatch) -> None:
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S"))
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    _peer_row(store, "EURUSDm", 0.0)  # a legacy row from a degenerate contract
    opened = runner._try_entries(account, snapshot, [], day, 1.0)
    assert opened == 0 and sent == []
    assert any(kind == "portfolio_risk_unpriced" for _, _, kind, _ in store.recent_events(20))


def test_the_day_does_not_roll_forward_while_an_account_is_dark(tmp_path, monkeypatch) -> None:
    """MIN is only the slowest clock if every clock is live: a dark shard must not open the day."""
    import time as time_mod

    from fleet.store import BOOK_FRESH_SECONDS

    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    store = Store(tmp_path / "book.sqlite")
    exness = make_account("exness", 1, "Exness-MT5Real26")   # GMT+0, and silent since yesterday
    gtc = make_account("gtc", 2, "GTC-Go-Live")              # GMT+3, already in the next day
    engine = conductor.Conductor(FleetConfig(accounts=[exness, gtc]), store, shards=[0])
    _seed_day(store, engine, "2026-11-02", 2000.0)
    store.equity("exness", 1, 980.0, 980.0, 0, 0.0, server_day="2026-11-02")
    store.equity("gtc", 2, 980.0, 980.0, 0, 0.0, server_day="2026-11-03")
    with store._connect() as conn:
        conn.execute("UPDATE book SET updated_epoch=? WHERE account='exness'",
                     (time_mod.time() - BOOK_FRESH_SECONDS - 1,))
    engine._remember_baseline(store.portfolio([exness.name, gtc.name]))
    assert engine.day_start_equity == 2000.0  # not rebased onto the -2% book
    assert store.portfolio_baseline(conductor.local_host())[0] == "2026-11-02"
    kinds = [kind for _, _, kind, _ in store.recent_events(20)]
    assert "new_day" not in kinds and "portfolio_day_held" in kinds


def test_two_machines_do_not_trade_under_each_others_baseline(tmp_path, monkeypatch) -> None:
    """One book file can be shared; the day in it is still each machine's own measurement."""
    from fleet import config as fleet_config

    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    park = FleetConfig(accounts=[make_account("exness", 1, "Exness-MT5Real26", host="box-a"),
                                 make_account("gtc", 2, "GTC-Go-Live", host="box-b")])
    store = Store(tmp_path / "book.sqlite")
    store.equity("exness", 1, 1000.0, 1000.0, 0, 0.0, server_day="2026-11-03")
    store.equity("gtc", 2, 500.0, 500.0, 0, 0.0, server_day="2026-11-03")

    monkeypatch.setattr(fleet_config, "local_host", lambda: "box-a")
    monkeypatch.setattr(conductor, "local_host", lambda: "box-a")
    conductor.Conductor(park, store, shards=[0])._remember_baseline(store.portfolio(["exness"]))
    monkeypatch.setattr(fleet_config, "local_host", lambda: "box-b")
    monkeypatch.setattr(conductor, "local_host", lambda: "box-b")
    engine = conductor.Conductor(park, store, shards=[0])
    engine._remember_baseline(store.portfolio(["gtc"]))

    assert engine.day_start_equity == 500.0  # box-b measured its own book, not the 1000 of box-a
    assert store.portfolio_baseline("box-a") == ("2026-11-03", "1000.00")
    assert store.portfolio_baseline("box-b") == ("2026-11-03", "500.00")


def test_each_box_starts_from_the_unscoped_baseline_it_finds(tmp_path, monkeypatch) -> None:
    """An upgrade must not re-base the day onto equity that has already fallen.

    A book written before the day was per-machine has one unscoped pair, and after the upgrade
    every box reads the same file. Each takes that pair as its own starting point — and the legacy
    keys stay, because clearing them would leave the second box with no day measured at all.
    """
    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    store = Store(tmp_path / "book.sqlite")
    store.set_meta("portfolio_day", "2026-11-03")
    store.set_meta("portfolio_day_start_equity", "1500")
    assert store.portfolio_baseline("box-a") == (None, None)  # no reader uses the legacy pair
    assert store.claim_portfolio_baseline("box-a") == ("2026-11-03", "1500")
    assert store.claim_portfolio_baseline("box-b") == ("2026-11-03", "1500")
    store.set_portfolio_baseline("box-a", "2026-11-03", 1400.0)
    assert store.portfolio_baseline("box-a") == ("2026-11-03", "1400.00")
    assert store.portfolio_baseline("box-b") == ("2026-11-03", "1500")  # box-b keeps its own
    assert store.get_meta("portfolio_day_start_equity") == "1500"


def test_a_reset_day_on_one_machine_leaves_the_other_one_measured(tmp_path, monkeypatch) -> None:
    """`reset-day` is this machine's operator switch, not a switch on the shared file."""
    import fleet_cli as fleet_cli_mod

    monkeypatch.setattr(fleet_cli_mod, "local_host", lambda: "box-a")
    # The real switch is a file in the fleet root; a test must not lift a pause on live money.
    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    store = Store(tmp_path / "book.sqlite")
    store.set_portfolio_baseline("box-a", "2026-11-03", 1000.0)
    store.set_portfolio_baseline("box-b", "2026-11-03", 700.0)
    store.set_portfolio_registry_hash("box-a", "abcdef1234")
    fleet_cli_mod.cmd_reset_day(argparse.Namespace(store=str(tmp_path / "book.sqlite")))
    assert store.portfolio_baseline("box-a") == ("", "")  # cleared, and not re-taken from a peer
    assert store.portfolio_baseline("box-b") == ("2026-11-03", "700.00")
    # the composition of the book survives a day reset; a reset is not a registry change
    assert store.portfolio_registry_hash("box-a") == "abcdef1234"


def test_a_day_held_for_hours_is_announced_once(tmp_path, monkeypatch) -> None:
    """One quiet broker over a weekend is one fact, not a couple of hundred identical events."""
    import time as time_mod

    from fleet.store import BOOK_FRESH_SECONDS

    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    store = Store(tmp_path / "book.sqlite")
    exness = make_account("exness", 1, "Exness-MT5Real26")   # silent since yesterday
    gtc = make_account("gtc", 2, "GTC-Go-Live")              # already in the next day
    engine = conductor.Conductor(FleetConfig(accounts=[exness, gtc]), store, shards=[0])
    _seed_day(store, engine, "2026-11-02", 2000.0)
    store.equity("exness", 1, 980.0, 980.0, 0, 0.0, server_day="2026-11-02")
    store.equity("gtc", 2, 980.0, 980.0, 0, 0.0, server_day="2026-11-03")
    with store._connect() as conn:
        conn.execute("UPDATE book SET updated_epoch=? WHERE account='exness'",
                     (time_mod.time() - BOOK_FRESH_SECONDS - 1,))
    book = store.portfolio([exness.name, gtc.name])
    for _ in range(3):
        engine._remember_baseline(book)  # the same dark shard, three consecutive ticks
    kinds = [kind for _, _, kind, _ in store.recent_events(20)]
    assert kinds.count("portfolio_day_held") == 1
    assert store.get_meta(f"portfolio_day_held:{fleet_config.local_host()}") == "exness"


def test_an_account_another_machine_writes_does_not_veto_entries_here(tmp_path,
                                                                      monkeypatch) -> None:
    """A peer box's unpriced row is not this machine's business, and must not freeze its entries."""
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S"))
    other = make_account("elsewhere", 3, "Other-Server", host="box-z")
    runner.config = FleetConfig(accounts=[account, other])
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    _peer_row(store, "EURUSDm", None)
    store.save_positions("elsewhere", [types.SimpleNamespace(
        ticket=11, symbol="GOLD.a", type=0, volume=0.05, price_open=1.10, profit=0.0,
        sl=1.098, tp=0.0, magic=11)], {11: None})
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1 and sent == [1]


def test_the_portfolio_risk_cap_is_reserved_not_only_checked(tmp_path) -> None:
    """Two workers reading the same risk number must not both pass it and both open."""
    from fleet import config as fleet_config

    limits = fleet_config.PortfolioLimits(max_total_risk_pct=2.0, max_usd_lots=100.0,
                                          max_gold_lots=100.0)
    store = Store(tmp_path / "book.sqlite")
    ok_a, _why_a, _slot_a = store.reserve("a", "EURUSD", 0.1, limits, 1000.0, risk_usd=15.0)
    assert ok_a
    ok_b, why_b, _slot_b = store.reserve("b", "GBPUSD", 0.1, limits, 1000.0, risk_usd=10.0)
    assert not ok_b and "риска" in why_b  # 15 + 10 > 2% of 1000
    store.release(_slot_a)
    ok_c, _why_c, _slot_c = store.reserve("b", "GBPUSD", 0.1, limits, 1000.0, risk_usd=10.0)
    assert ok_c  # the reservation is what bounded it, and it is released with the order


def test_the_money_cap_counts_positions_that_are_already_open(tmp_path) -> None:
    """Half the risk is held, half is being reserved: a check that sees only one passes the park.

    Three workers each price 15 USD of already-open stops and ask for 4 more against a 2% cap of
    1000 USD. Held risk is the same number for all of them, so the only part that races is what the
    others reserved — and it has to be summed in the same transaction that writes the slot.
    """
    from fleet import config as fleet_config

    limits = fleet_config.PortfolioLimits(max_total_risk_pct=2.0, max_usd_lots=100.0,
                                          max_gold_lots=100.0)
    store = Store(tmp_path / "book.sqlite")
    ok_first, _why, slot = store.reserve("a", "EURUSD", 0.1, limits, 1000.0,
                                         risk_usd=4.0, open_risk_usd=15.0)
    assert ok_first and slot
    ok_second, why_second, _ = store.reserve("b", "GBPUSD", 0.1, limits, 1000.0,
                                             risk_usd=4.0, open_risk_usd=15.0)
    assert not ok_second and "риска" in why_second  # 15 + 4 reserved + 4 > 20
    store.release(slot)
    assert store.reserve("b", "GBPUSD", 0.1, limits, 1000.0,
                         risk_usd=4.0, open_risk_usd=15.0)[0]


def test_a_shard_this_conductor_does_not_run_does_not_hold_the_day(tmp_path, monkeypatch) -> None:
    """`--shards 0` is a real deployment: shard 1's silence is not a reason to freeze the book."""
    import time as time_mod

    from fleet.store import BOOK_FRESH_SECONDS

    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    store = Store(tmp_path / "book.sqlite")
    exness = make_account("exness", 1, "Exness-MT5Real26", shard=0)   # GMT+0, run here
    gtc = make_account("gtc", 2, "GTC-Go-Live", shard=1)              # GMT+3, run by nobody
    engine = conductor.Conductor(FleetConfig(accounts=[exness, gtc]), store, shards=[0])
    _seed_day(store, engine, "2026-11-02", 2000.0)
    store.equity("exness", 1, 1960.0, 1960.0, 0, 0.0, server_day="2026-11-02")
    store.equity("gtc", 2, 1960.0, 1960.0, 0, 0.0, server_day="2026-11-03")
    with store._connect() as conn:
        conn.execute("UPDATE book SET updated_epoch=? WHERE account='gtc'",
                     (time_mod.time() - BOOK_FRESH_SECONDS - 1,))
    engine._remember_baseline(store.portfolio(["exness", "gtc"]))
    assert engine.day_held is None
    assert engine.day_start_equity == 2000.0
    assert store.portfolio_baseline(conductor.local_host())[0] == "2026-11-02"
    kinds = [kind for _, _, kind, _ in store.recent_events(20)]
    assert "portfolio_day_held" not in kinds


def test_an_all_dark_book_does_not_announce_a_resume_it_never_had(tmp_path, monkeypatch) -> None:
    """Over a weekend every clock is silent; that is not the day being handed back."""
    import time as time_mod

    from fleet.store import BOOK_FRESH_SECONDS

    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    store = Store(tmp_path / "book.sqlite")
    exness = make_account("exness", 1, "Exness-MT5Real26")
    engine = conductor.Conductor(FleetConfig(accounts=[exness]), store, shards=[0])
    _seed_day(store, engine, "2026-11-02", 2000.0)
    store.equity("exness", 1, 1960.0, 1960.0, 0, 0.0, server_day="2026-11-02")
    host = conductor.local_host()
    store.set_meta(f"portfolio_day_held:{host}", "exness")
    with store._connect() as conn:
        conn.execute("UPDATE book SET updated_epoch=?",
                     (time_mod.time() - BOOK_FRESH_SECONDS - 1,))
    engine._remember_baseline(store.portfolio(["exness"]))
    kinds = [kind for _, _, kind, _ in store.recent_events(20)]
    assert "portfolio_day_resumed" not in kinds
    assert store.get_meta(f"portfolio_day_held:{host}") == "exness"  # still held, still true
    assert engine.day_start_equity == 2000.0


def test_a_change_on_another_box_does_not_rebase_this_day(tmp_path, monkeypatch) -> None:
    """The registry is shared; the book is not. A peer's new account is not this day's event."""
    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    store = Store(tmp_path / "book.sqlite")
    mine = make_account("exness", 1, "Exness-MT5Real26")
    engine = conductor.Conductor(FleetConfig(accounts=[mine]), store, shards=[0])
    _seed_day(store, engine, "2026-11-02", 2000.0)
    store.equity("exness", 1, 1960.0, 1960.0, 0, 0.0, server_day="2026-11-02")
    engine.config = FleetConfig(accounts=[mine,
                                          make_account("gtc", 2, "GTC-Go-Live", host="box-b")])
    engine._remember_baseline(store.portfolio(["exness"]))
    assert engine.day_start_equity == 2000.0
    kinds = [kind for _, _, kind, _ in store.recent_events(20)]
    assert "baseline_rebased" not in kinds


def test_a_machine_with_no_accounts_of_its_own_writes_no_day(tmp_path, monkeypatch) -> None:
    """A registry whose every account belongs to another box has no book of its own to measure."""
    monkeypatch.setattr(conductor, "PAUSE_FILE", tmp_path / "PAUSE")
    store = Store(tmp_path / "book.sqlite")
    peer = make_account("gtc", 2, "GTC-Go-Live", host="box-z")
    engine = conductor.Conductor(FleetConfig(accounts=[peer]), store)
    store.equity("gtc", 2, 500.0, 500.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    engine._remember_baseline(store.portfolio([a.name for a in engine.config.local()]))
    assert store.claim_portfolio_baseline(fleet_config.local_host()) == (None, None)
    kinds = [kind for _, _, kind, _ in store.recent_events(20)]
    assert "new_day" not in kinds and "baseline_rebased" not in kinds


def test_an_empty_book_is_not_the_whole_shared_file(tmp_path) -> None:
    """`[]` is this machine owning nothing; only `None` asks for every account in the file."""
    store = Store(tmp_path / "book.sqlite")
    store.equity("gtc", 2, 500.0, 500.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    store.save_positions("gtc", [types.SimpleNamespace(
        ticket=7, symbol="XAUUSDc", type=0, volume=0.05, price_open=1.10, profit=0.0,
        sl=1.098, tp=0.0, magic=7)], {7: 12.0})
    assert store.portfolio([])["equity"] == 0.0
    assert store.portfolio(["gtc"])["equity"] == 500.0  # the same file, read with a name in it
    assert store.all_positions(max_age=10 ** 9, names=[]) == []
    assert len(store.all_positions(max_age=10 ** 9)) == 1


def test_status_reports_this_machines_book_not_the_shared_file(tmp_path, capsys,
                                                               monkeypatch) -> None:
    """The operator's money line is this machine's caps, not the sum of every box sharing the file."""
    import fleet_cli as fleet_cli_mod

    monkeypatch.setattr(conductor, "STATE_FILE", tmp_path / "conductor.json")
    monkeypatch.setattr(fleet_cli_mod, "_config",
                        lambda args: FleetConfig(accounts=[
                            make_account("mine", 1, "S"),
                            make_account("theirs", 2, "T", host="box-z")]))
    store = Store(tmp_path / "book.sqlite")
    store.equity("mine", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    store.equity("theirs", 2, 5000.0, 5000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    code = fleet_cli_mod.cmd_status(argparse.Namespace(store=str(tmp_path / "book.sqlite"),
                                                       events=5, config=""))
    out = capsys.readouterr().out
    assert code == 0
    assert "портфель этой машины: equity 1000.00 USD" in out
    assert "theirs" in out and "другая машина" in out  # the peer row stays visible, marked
    assert "строк чужих машин: 1" in out


def test_an_unmeasured_day_does_not_read_as_a_zero_percent_day(tmp_path, monkeypatch) -> None:
    """No baseline is not "nothing lost today": an upgrade or `reset-day` must not open the gate."""
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S"))
    store.equity("a", 1, 800.0, 800.0, 0, 0.0, "USD", 1.0, "2026-11-03")  # -20 % against 1000
    assert runner._portfolio_day_pct(800.0) == pytest.approx(-20.0)
    for key in host_keys(fleet_config.local_host())[:2]:
        store.set_meta(key, "")                       # exactly what `reset-day` leaves
    assert runner._portfolio_day_pct(800.0) is None
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0 and sent == []
    assert any(kind == "portfolio_day_unmeasured" for _, _, kind, _ in store.recent_events(20))
    store.set_portfolio_baseline(fleet_config.local_host(), "2026-11-03", 800.0)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1 and sent == [1]


def test_a_crash_after_reserving_gives_the_money_back(tmp_path, monkeypatch) -> None:
    """A slot held by a dead cycle is a risk reservation nobody owns for the next ten minutes."""
    account, runner, store, _sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                         (1, "S"))
    store.equity("a", 1, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")

    def explode(*_a, **_k):
        raise RuntimeError("the terminal died mid-send")

    monkeypatch.setattr(worker.broker, "market_order", explode)
    with pytest.raises(RuntimeError):
        runner._try_entries(account, snapshot, [], day, 1.0)
    assert store.reserved_volume() == 0.0


def test_prune_leaves_a_row_another_machine_is_writing(tmp_path) -> None:
    """A registry that does not name an account is not proof that nobody is trading it."""
    import time as time_mod

    store = Store(tmp_path / "book.sqlite")
    store.equity("here", 1, 1000.0, 1000.0, 0, 0.0, server_day="2026-11-03")
    store.equity("other-box", 2, 800.0, 800.0, 0, 0.0, server_day="2026-11-03")
    store.prune(["here"])
    assert store.account_equity("other-box") == 800.0  # fresh: somebody else owns this row
    with store._connect() as conn:
        conn.execute("UPDATE book SET updated_epoch=?", (time_mod.time() - 7200,))
    store.prune(["here"])
    assert store.account_equity("other-box") == 0.0  # nobody has written it for two hours


# --- the census: which window holds which account ---------------------------------------------

def _census(store: Store, rows: list[dict], taken: float | None = None,
            host: str | None = None) -> None:
    """Write the census the way the park writes it at the start of a cycle: keyed by its machine."""
    import time as time_mod

    key = f"{worker.ROSTER_META_KEY}@{host or fleet_config.local_host()}"
    store.set_meta(key, json.dumps(
        {"taken": time_mod.time() if taken is None else taken, "rows": rows}))


def _forget_census(store: Store) -> None:
    """Leave this machine with no census on file — the state of a fresh book or a pruned meta."""
    store.set_meta(f"{worker.ROSTER_META_KEY}@{fleet_config.local_host()}", "")


def _row(directory: str, login: int | None, server: str = "S", trade_mode=None,
        trade_allowed=None) -> dict:
    """One census row. `trade_mode` stays None unless a test says it: that is what a census written
    before the field existed looks like, and the gate reads an unnamed mode as no agreement.
    `trade_allowed` is the same story for the ALGO TRADING button of that window."""
    return {"exe": f"{directory}\\terminal64.exe", "dir": worker.window_dir(directory),
            "login": login, "server": server, "currency": "USD", "balance": 1000.0,
            "equity": 1000.0, "trade_mode": trade_mode, "trade_allowed": trade_allowed,
            "error": ""}


class CensusMT5:
    """Several windows answering at once, each naming ITS OWN folder.

    A window is given as `path: (login, server)` and may add a third element — the folder it
    actually reports. That crossing (we ask A, B answers) is measured behaviour of the API and the
    one thing the census must not paper over, so the fake cannot echo the requested path back. A
    fourth element is the mode the broker names for that window, a fifth whether that window has
    algorithmic trading switched on (`terminal_info().trade_allowed`).
    """

    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self, windows: dict[str, tuple]) -> None:
        self.windows: dict[str, tuple] = {}
        for path, value in windows.items():
            reported = value[2] if len(value) > 2 else path
            mode = value[3] if len(value) > 3 else None
            allowed = value[4] if len(value) > 4 else None
            self.windows[worker.window_dir(path)] = (value[0], value[1],
                                                     worker.window_dir(reported), mode, allowed)
        self.initialize_calls: list[dict] = []
        self.shutdowns = 0
        self.current: tuple | None = None

    def initialize(self, **kwargs) -> bool:
        self.initialize_calls.append(kwargs)
        self.current = self.windows.get(worker.window_dir(str(kwargs.get("path", ""))))
        return self.current is not None and self.current[0] is not None

    def last_error(self):
        return (-10005, "IPC timeout")

    def terminal_info(self):
        return types.SimpleNamespace(path=self.current[2] if self.current else "",
                                     trade_allowed=self.current[4] if self.current else None)

    def account_info(self):
        login, server = (self.current[0], self.current[1]) if self.current else (None, "")
        mode = self.current[3] if self.current else None
        return types.SimpleNamespace(login=login, server=server, currency="USD",
                                     balance=1000.0, equity=1000.0, trade_mode=mode)

    def shutdown(self) -> None:
        self.shutdowns += 1


def test_folder_and_exe_are_the_same_window() -> None:
    """Measured on build 6296: a running terminal names its folder, the process list names the exe."""
    assert worker.window_dir(r"C:\mt5-team\terminal64.exe") == worker.window_dir(r"C:\mt5-team")
    assert worker.window_dir("C:\\mt5-team\\") == worker.window_dir("c:/mt5-team/terminal64.exe")


def test_the_census_names_the_account_in_every_running_window(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "book.sqlite")
    fake = CensusMT5({r"C:\mt5-exness": (114929502, "MetaQuotes-Demo"),
                      r"C:\mt5-team": (777001, "Exness-MT5Real26")})
    monkeypatch.setattr(worker, "mt5", fake)
    monkeypatch.setattr(terminals, "running_terminals",
                        lambda: [(1, r"C:\mt5-team\terminal64.exe"),
                                 (2, r"C:\mt5-exness\terminal64.exe")])
    rows = worker.refresh_roster(store)
    assert {row["dir"] for row in rows} == {"c:\\mt5-exness", "c:\\mt5-team"}
    assert {row["login"] for row in rows} == {114929502, 777001}
    assert fake.shutdowns == 2  # every visit ends: the census never parks in someone's window


def test_the_census_asks_a_window_for_nothing_but_its_name(tmp_path, monkeypatch) -> None:
    """No login, no password: a probe that signs in would change who is driving the money."""
    fake = CensusMT5({r"C:\mt5-team": (1, "S")})
    monkeypatch.setattr(worker, "mt5", fake)
    monkeypatch.setattr(terminals, "running_terminals",
                        lambda: [(7, r"C:\mt5-team\terminal64.exe")])
    worker.refresh_roster(Store(tmp_path / "book.sqlite"))
    assert fake.initialize_calls == [{"path": r"C:\mt5-team\terminal64.exe", "timeout": 4000}]
    # A census runs inside the trading cycle: a probe that waits a minute would starve the exits.
    assert worker.ROSTER_PROBE_TIMEOUT_MS <= 5000


def test_a_window_that_died_before_the_probe_is_never_addressed(tmp_path, monkeypatch) -> None:
    """initialize() starts a terminal that is not running: a vanished pid must not be probed.

    The process walk and the probe are moments apart, and MT5 re-spawns itself on updates. Without
    the liveness re-check the census would raise the window that just closed — seeded with a saved
    session, so it would log in and drive money nobody asked it to.
    """
    store = Store(tmp_path / "book.sqlite")
    fake = CensusMT5({r"C:\mt5-team": (1, "S")})
    monkeypatch.setattr(worker, "mt5", fake)
    monkeypatch.setattr(terminals, "running_terminals",
                        lambda: [(7, r"C:\mt5-team\terminal64.exe"),
                                 (8, r"C:\mt5-gone\terminal64.exe")])
    monkeypatch.setattr(terminals, "pids_alive", lambda pids: {7})
    rows = worker.refresh_roster(store)
    assert [row["dir"] for row in rows] == ["c:\\mt5-team"]
    assert [call["path"] for call in fake.initialize_calls] == [r"C:\mt5-team\terminal64.exe"]


def test_a_window_that_stays_silent_is_recorded_not_fatal(tmp_path, monkeypatch) -> None:
    """An unauthenticated copy answers nothing: the census writes the silence and moves on."""
    fake = CensusMT5({r"C:\mt5-team": (1, "S"), r"C:\mt5-silent": (None, "")})
    monkeypatch.setattr(worker, "mt5", fake)
    monkeypatch.setattr(terminals, "running_terminals",
                        lambda: [(1, r"C:\mt5-team\terminal64.exe"),
                                 (2, r"C:\mt5-silent\terminal64.exe")])
    rows = worker.refresh_roster(Store(tmp_path / "book.sqlite"))
    silent = [row for row in rows if row["login"] is None]
    assert [row["dir"] for row in silent] == ["c:\\mt5-silent"]
    assert "IPC timeout" in silent[0]["error"]
    assert worker.duplicated_accounts(rows) == {}


def test_the_census_is_taken_once_per_refresh_window(tmp_path, monkeypatch) -> None:
    """Two cycles in a row must not each walk every window: the answer is good for a while."""
    import time as time_mod

    store = Store(tmp_path / "book.sqlite")
    fake = CensusMT5({r"C:\mt5-team": (1, "S")})
    monkeypatch.setattr(worker, "mt5", fake)
    monkeypatch.setattr(terminals, "running_terminals",
                        lambda: [(1, r"C:\mt5-team\terminal64.exe")])
    taken = time_mod.time()
    worker.refresh_roster(store, now=taken)
    worker.refresh_roster(store, now=taken + worker.ROSTER_REFRESH_SECONDS - 1)
    assert len(fake.initialize_calls) == 1
    worker.refresh_roster(store, now=taken + worker.ROSTER_REFRESH_SECONDS)
    assert len(fake.initialize_calls) == 2
    assert len(store.get_meta(worker._host_key(worker.ROSTER_META_KEY))) > 0


def test_the_census_cadence_is_written_in_minutes_not_symbols() -> None:
    """The two windows are literals here because swapping them silently disables the gate.

    Refresh longer than evidence means the census is stale by the time it is retaken: every cycle
    then trades on a count nobody can vouch for. Measured cost of one full census on four windows,
    two of them unauthorised, is 8.4 s — the cadence is set by that, not by taste.
    """
    assert worker.ROSTER_REFRESH_SECONDS == 300.0
    assert worker.ROSTER_EVIDENCE_SECONDS == 900.0
    assert worker.ROSTER_REFRESH_SECONDS < worker.ROSTER_EVIDENCE_SECONDS


def test_the_census_costs_one_walk_per_refresh_not_per_account(tmp_path, monkeypatch, fleet) -> None:
    """The census is inside every account's cycle; the cadence is what keeps that affordable.

    Four windows × two accounts × two cycles would be sixteen probes — several seconds each on a
    silent window — while two cycles over a shared book need one walk. A census that cost per
    account would starve the exits it is supposed to leave alone.
    """
    store, _events = fleet
    fake = CensusMT5({r"C:\mt5-a": (1, "S"), r"C:\mt5-b": (2, "G")})
    monkeypatch.setattr(worker, "mt5", fake)
    monkeypatch.setattr(terminals, "running_terminals",
                        lambda: [(1, r"C:\mt5-a\terminal64.exe"),
                                 (2, r"C:\mt5-b\terminal64.exe")])
    accounts = [make_account("a", 1, "S", shard=0), make_account("b", 2, "G", shard=0)]
    monkeypatch.setattr(worker.Worker, "attach", lambda self, acc: "")
    monkeypatch.setattr(worker.Worker, "_cycle",
                        lambda self, acc, allow_entries=True: worker.WorkerResult(acc.name, "ok"))
    for _ in range(2):
        worker.run_shard(FleetConfig(accounts=accounts), store, 0)
    assert len(fake.initialize_calls) == 2


# --- the gate: one account, one driver ---------------------------------------------------------

def _gate(tmp_path, monkeypatch, rows: list[dict], directory: str | None = None):
    """One signal, one slot, and a census that says what else holds this account."""
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(
        tmp_path, monkeypatch, (1, "S"))
    mine = directory or str(account.data_dir)
    _census(store, [_row(mine, 1)] + rows)
    return account, runner, store, sent, snapshot, day, None


def test_a_second_window_on_the_account_closes_new_entries(tmp_path, monkeypatch) -> None:
    account, runner, store, sent, snapshot, day, _spec = _gate(
        tmp_path, monkeypatch, [_row(r"C:\mt5-team", 1)])
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert sent == []
    events = [(kind, payload) for _, _, kind, payload in store.recent_events(20)]
    assert any(kind == "account_second_window" and "mt5-team" in payload for kind, payload in events)


def test_the_parks_own_window_is_not_a_second_window(tmp_path, monkeypatch) -> None:
    account, runner, store, sent, snapshot, day, _spec = _gate(tmp_path, monkeypatch, [])
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1
    assert sent == [1]


def test_another_brokers_window_does_not_close_this_account(tmp_path, monkeypatch) -> None:
    account, runner, store, sent, snapshot, day, _spec = _gate(
        tmp_path, monkeypatch, [_row(r"C:\mt5-gtc", 424242, "GTC-Go-Live")])
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1
    assert sent == [1]


def test_a_window_on_another_server_is_another_pile_of_money(tmp_path, monkeypatch) -> None:
    """Same login number at a different broker: two accounts, and only one of them is ours."""
    account, runner, store, sent, snapshot, day, _spec = _gate(
        tmp_path, monkeypatch, [_row(r"C:\mt5-team", 1, "GTC-Go-Live")])
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1


def test_the_second_window_is_announced_once_while_it_last(tmp_path, monkeypatch) -> None:
    account, runner, store, sent, snapshot, day, _spec = _gate(
        tmp_path, monkeypatch, [_row(r"C:\mt5-team", 1)])
    for _ in range(3):
        runner._try_entries(account, snapshot, [], day, 1.0)
    kinds = [kind for _, _, kind, _ in store.recent_events(30)]
    assert kinds.count("account_second_window") == 1


# --- one demo login answered by several windows ------------------------------------------------
#
# Measured on this machine 2026-11-05: `c:\mt5-exness` and `c:\mt5-team` both answer
# `114929502@MetaQuotes-Demo`, 99 212,61 USD, trade_mode 2 (demo). One pile of money seen twice is not
# two drivers of two piles, and the ambiguity it leaves — through which API session do we act — is the
# one `attach()` already settles by proving the registry's own copy before any order. So the park names
# one window the driver of record, says so out loud, and trades. Every case that genuinely must stay
# shut is pinned below, with its old wording.

def _demo_windows(*directories: str) -> list[dict]:
    """Windows that name one login, one server and one demo mode — the shape measured on this box."""
    return [_row(directory, 1, "S", trade_mode=_DEMO_MODE) for directory in directories]


def test_one_demo_login_in_two_windows_keeps_its_entries_and_names_the_driver(
        tmp_path, monkeypatch) -> None:
    account, runner, store, sent, snapshot, day, _spec = _gate(tmp_path, monkeypatch, [])
    _census(store, _demo_windows(r"C:\mt5-exness", r"C:\mt5-team"))
    assert runner._second_window(account) == ""  # the door is open
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1
    assert sent == [1]
    events = [(kind, payload) for _, _, kind, payload in store.recent_events(30)]
    assert "account_second_window" not in [kind for kind, _ in events]
    drivers = [json.loads(payload) for kind, payload in events if kind == "driver_chosen"]
    assert len(drivers) == 1
    # The sentence names the window it chose and says the other window is there: an operator reads the
    # decision, instead of inferring it from a book that traded anyway.
    assert drivers[0]["window"] == "c:\\mt5-exness"
    assert drivers[0]["others"] == ["c:\\mt5-team"]
    assert drivers[0]["login"] == 1 and drivers[0]["mode"] == "demo"


def test_the_driver_choice_is_the_same_window_whatever_order_the_census_answered() -> None:
    """Determinism is the whole rule: a choice that flipped per cycle would ping-pong the orders.

    Two cycles on one field have to send through the session the first cycle named, so the order the
    process list happened to answer in cannot be what decides.
    """
    rows = _demo_windows(r"C:\mt5-exness", r"C:\mt5-team")
    assert (worker.choose_driver_window(rows, "")
            == worker.choose_driver_window(list(reversed(rows)), "")
            == ("c:\\mt5-exness", ["c:\\mt5-team"]))


def test_the_registry_window_wins_over_the_sort_order(tmp_path) -> None:
    """The directory the registry names is the copy `attach()` proves, so it drives when it is up.

    Picked so the sort would NOT have answered it: `c:\\mt5-a-team` sorts before the fleet folder, so a
    rule of bare sort order would name an operator window the park never trades through.
    """
    account = make_account("a", 1, "S")
    mine = worker.window_dir(str(account.data_dir))
    rows = [_row(r"C:\mt5-a-team", 1, "S", trade_mode=_DEMO_MODE),
            _row(str(account.data_dir), 1, "S", trade_mode=_DEMO_MODE)]
    assert mine not in sorted(worker.window_dir(row["dir"]) for row in rows)[:1]
    owner, rest = worker.choose_driver_window(rows, mine)
    assert owner == mine
    assert rest == ["c:\\mt5-a-team"]


def test_the_driver_is_announced_once_per_process_even_for_a_fresh_worker(
        tmp_path, monkeypatch) -> None:
    """One sentence per start, not one per cycle.

    `run_shard` builds a new Worker every cycle, so an instance note would re-announce the same choice
    a hundred times a day; a note in the book would let the next start of this park make the choice
    again and stay silent about it. Both directions are pinned here: the same field is said once, and
    a field that moved is said again.
    """
    account, runner, store, _sent, _snapshot, _day, _spec = _gate(tmp_path, monkeypatch, [])
    _census(store, _demo_windows(r"C:\mt5-exness", r"C:\mt5-team"))
    for _ in range(3):
        assert runner._second_window(account) == ""
    next_cycle = worker.Worker(runner.config, store)  # a different Worker, the same process
    assert next_cycle._second_window(account) == ""
    kinds = [kind for _, _, kind, _ in store.recent_events(30)]
    assert kinds.count("driver_chosen") == 1
    _census(store, _demo_windows(r"C:\mt5-exness", r"C:\mt5-team", r"C:\mt5-third"))
    assert runner._second_window(account) == ""
    kinds = [kind for _, _, kind, _ in store.recent_events(30)]
    assert kinds.count("driver_chosen") == 2  # three windows is a different fact, and is said again


def test_windows_that_disagree_about_the_mode_still_close_the_entries(tmp_path, monkeypatch) -> None:
    """One login at two broker modes is not the same account twice: it is a mystery, and it is shut."""
    account, runner, store, sent, snapshot, day, _spec = _gate(tmp_path, monkeypatch, [])
    _census(store, [_row(r"C:\mt5-exness", 1, "S", trade_mode=_DEMO_MODE),
                    _row(r"C:\mt5-team", 1, "S", trade_mode=_REAL_MODE)])
    reason = runner._second_window(account)
    assert "открыт ещё в 2 окне" in reason and "входы закрыты" in reason
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert sent == []
    assert any(kind == "account_second_window" for _, _, kind, _ in store.recent_events(30))
    assert not any(kind == "driver_chosen" for _, _, kind, _ in store.recent_events(30))


def test_a_window_that_named_no_mode_is_not_an_agreement(tmp_path, monkeypatch) -> None:
    """A census row written before the field existed says nothing about which money this is.

    The old rows stay evidence for ROSTER_EVIDENCE_SECONDS after an upgrade, and an unmeasured pair has
    never been allowed to read as an agreed one anywhere else in this park.
    """
    account, runner, store, _sent, snapshot, day, _spec = _gate(tmp_path, monkeypatch, [])
    _census(store, _demo_windows(r"C:\mt5-exness") + [_row(r"C:\mt5-team", 1, "S")])
    assert "входы закрыты" in runner._second_window(account)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert _sent == []


def test_a_window_that_hides_its_path_is_never_made_the_driver(tmp_path, monkeypatch) -> None:
    """No folder, no driver: the park cannot name a session it cannot address."""
    hidden = {"exe": "", "dir": "", "login": 1, "server": "S", "currency": "USD",
              "balance": 1000.0, "equity": 1000.0, "trade_mode": _DEMO_MODE, "error": ""}
    account, runner, store, _sent, snapshot, day, _spec = _gate(tmp_path, monkeypatch, [])
    _census(store, _demo_windows(r"C:\mt5-exness") + [hidden])
    assert "входы закрыты" in runner._second_window(account)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert _sent == []
    # and the candidate list drops the window it cannot address rather than naming it
    assert worker.choose_driver_window([hidden] + _demo_windows(r"C:\mt5-team"), "") == (
        "c:\\mt5-team", [])


def test_a_shared_demo_login_with_a_window_nobody_read_stays_blocked(tmp_path, monkeypatch) -> None:
    """The field is clean only when every window in it was read: a blind copy closes the door."""
    account, runner, store, sent, snapshot, day, _spec = _gate(tmp_path, monkeypatch, [])
    _census(store, _demo_windows(r"C:\mt5-exness", r"C:\mt5-team")
            + [_row(r"C:\mt5-unknown", None, "")])
    assert runner._second_window(account) != ""
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert sent == []
    assert not any(kind == "driver_chosen" for _, _, kind, _ in store.recent_events(30))


def test_a_real_account_sharing_its_login_is_blocked_exactly_as_before(
        tmp_path, monkeypatch) -> None:
    """Real money is never handed a silently chosen session: that door did not move.

    The sentence is the one the operator has always read, word for word, the block keeps its event
    name and its once-per-set-of-windows cadence, and no driver is ever named for a real login.
    """
    _account, runner, store, _sent, _snapshot, _day, _spec = _gate(tmp_path, monkeypatch, [])
    real = make_account("a", 1, "S", mode="real")
    _census(store, [_row(r"C:\mt5-exness", 1, "S", trade_mode=_REAL_MODE),
                    _row(r"C:\mt5-team", 1, "S", trade_mode=_REAL_MODE)])
    assert runner._second_window(real) == (
        "счёт 1@S открыт ещё в 2 окне (c:\\mt5-exness, c:\\mt5-team): свободные деньги общего "
        "счёта тратятся дважды, входы закрыты")
    events = [(kind, payload) for _, _, kind, payload in store.recent_events(30)]
    assert any(kind == "account_second_window" for kind, _ in events)
    assert not any(kind == "driver_chosen" for kind, _ in events)
    rows = worker.roster_rows(store)
    assert worker.shared_demo_driver(rows, worker.account_key(1, "S"), real) == ("", [])


def test_a_demo_account_whose_windows_both_answer_real_is_blocked(tmp_path, monkeypatch) -> None:
    """The registry calling a login demo does not turn two REAL windows into demo money."""
    account, runner, store, _sent, snapshot, day, _spec = _gate(tmp_path, monkeypatch, [])
    assert account.is_real is False
    _census(store, [_row(r"C:\mt5-exness", 1, "S", trade_mode=_REAL_MODE),
                    _row(r"C:\mt5-team", 1, "S", trade_mode=_REAL_MODE)])
    assert "входы закрыты" in runner._second_window(account)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert _sent == []


def test_the_census_records_the_mode_of_every_window_it_reads(tmp_path, monkeypatch) -> None:
    """The gate compares what the broker calls this money, so the census has to carry it per window."""
    store = Store(tmp_path / "book.sqlite")
    fake = CensusMT5({r"C:\mt5-exness": (114929502, "MetaQuotes-Demo", r"C:\mt5-exness", _DEMO_MODE),
                      r"C:\mt5-team": (114929502, "MetaQuotes-Demo", r"C:\mt5-team", _REAL_MODE)})
    monkeypatch.setattr(worker, "mt5", fake)
    monkeypatch.setattr(terminals, "running_terminals",
                        lambda: [(1, r"C:\mt5-team\terminal64.exe"),
                                 (2, r"C:\mt5-exness\terminal64.exe")])
    rows = worker.refresh_roster(store)
    assert {row["dir"]: row["trade_mode"] for row in rows} == {
        "c:\\mt5-team": _REAL_MODE, "c:\\mt5-exness": _DEMO_MODE}
    assert not worker.shared_windows_agree(rows)  # one login, two modes: nobody chooses a session


# --- the driver is the window that can actually trade -------------------------------------------
#
# Measured on this machine 2026-11-05: the rule of the previous commit named `c:\\mt5-exness` because it
# sorts first, and that window answers `terminal_info().trade_allowed = False`; fourteen `entry_failed
# ... autotrading disabled by client (retcode 10027)` later the park had opened nothing while
# `c:\\mt5-team`, on the very same login, had the switch on. ALGO TRADING is a machine setting only a
# human can change, so the park reads it, prefers a window that has it, says what it saw once, and
# never tries to bypass or retry around it.

def _algo_rows(*directories: str, enabled: tuple[str, ...] = ()) -> list[dict]:
    """Windows naming one demo login, with the ALGO TRADING button on for the directories named."""
    on = {worker.window_dir(directory) for directory in enabled}
    return [_row(directory, 1, "S", trade_mode=_DEMO_MODE,
                 trade_allowed=worker.window_dir(directory) in on) for directory in directories]


def test_the_census_records_whether_a_window_is_allowed_to_trade(tmp_path, monkeypatch) -> None:
    """The preference reads `terminal_info().trade_allowed` per window, and writes nothing anywhere."""
    store = Store(tmp_path / "book.sqlite")
    fake = CensusMT5({r"C:\mt5-exness": (114929502, "MetaQuotes-Demo", r"C:\mt5-exness",
                                         _DEMO_MODE, True),
                      r"C:\mt5-team": (114929502, "MetaQuotes-Demo", r"C:\mt5-team",
                                       _DEMO_MODE, False)})
    monkeypatch.setattr(worker, "mt5", fake)
    monkeypatch.setattr(terminals, "running_terminals",
                        lambda: [(1, r"C:\mt5-team\terminal64.exe"),
                                 (2, r"C:\mt5-exness\terminal64.exe")])
    rows = worker.refresh_roster(store)
    assert {row["dir"]: row["trade_allowed"] for row in rows} == {
        "c:\\mt5-team": False, "c:\\mt5-exness": True}
    group = worker.windows_by_account(rows)[worker.account_key(114929502, "MetaQuotes-Demo")]
    assert worker.enabled_driver_dirs(group) == ["c:\\mt5-exness"]


def test_the_enabled_window_is_the_driver_when_the_first_cannot_trade(tmp_path, monkeypatch) -> None:
    """The preference is the fix: `c:\\mt5-team` has the switch on, so `c:\\mt5-team` is named."""
    account, runner, store, sent, snapshot, day, _spec = _gate(tmp_path, monkeypatch, [])
    _census(store, _algo_rows(r"C:\mt5-exness", r"C:\mt5-team", enabled=(r"C:\mt5-team",)))
    assert runner._second_window(account) == ""
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1
    assert sent == [1]
    drivers = [json.loads(payload) for _, _, kind, payload in store.recent_events(30)
               if kind == "driver_chosen"]
    assert len(drivers) == 1
    assert drivers[0]["window"] == "c:\\mt5-team"
    assert drivers[0]["others"] == ["c:\\mt5-exness"]
    assert drivers[0]["reason"] == worker.DRIVER_ENABLED
    assert drivers[0]["trade_allowed"] is True


def test_the_enabled_window_is_chosen_whatever_order_the_census_answered() -> None:
    """Determinism still decides the choice: the enabled window wins from either end of the field."""
    rows = _algo_rows(r"C:\mt5-exness", r"C:\mt5-team", enabled=(r"C:\mt5-team",))
    assert (worker.choose_driver_window(rows, "")
            == worker.choose_driver_window(list(reversed(rows)), "")
            == ("c:\\mt5-team", ["c:\\mt5-exness"]))


def test_the_enabled_window_wins_over_a_registry_folder_that_cannot_trade() -> None:
    """Decided and pinned: an enabled window is named the driver even when the registry names the
    disabled one, because a driver that cannot send an order is only a taller refusal.

    The registry copy stays the session `attach()` proves and trades through — that is what the reason
    `enabled` tells the operator: it is the registry's own window whose ALGO TRADING he has to switch
    on. The registry folder keeps the choice when it is itself enabled, which is reason `registry` and
    is pinned on the last line here.
    """
    account = make_account("a", 1, "S")
    mine = worker.window_dir(str(account.data_dir))
    other = _row(r"C:\mt5-z-team", 1, "S", trade_mode=_DEMO_MODE, trade_allowed=True)
    rows = [other, _row(str(account.data_dir), 1, "S", trade_mode=_DEMO_MODE, trade_allowed=False)]
    assert mine < other["dir"]  # so even the old bare sort named the registry folder first
    assert worker.choose_driver_window(rows, mine) == (other["dir"], [mine])
    assert worker.driver_window_choice(rows, mine)[2] == worker.DRIVER_ENABLED
    rows = [other, _row(str(account.data_dir), 1, "S", trade_mode=_DEMO_MODE, trade_allowed=True)]
    assert worker.driver_window_choice(rows, mine) == (mine, [other["dir"]], worker.DRIVER_REGISTRY)


def test_the_gate_names_the_enabled_window_when_the_registry_copy_cannot_trade(
        tmp_path, monkeypatch) -> None:
    """The same decision reaches the journal while the park's own copy is in the field."""
    account, runner, store, sent, snapshot, day, _spec = _gate(tmp_path, monkeypatch, [])
    _census(store, [_row(r"C:\mt5-z-team", 1, "S", trade_mode=_DEMO_MODE, trade_allowed=True),
                    _row(str(account.data_dir), 1, "S", trade_mode=_DEMO_MODE,
                         trade_allowed=False)])
    assert runner._second_window(account) == ""
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1
    assert sent == [1]
    drivers = [json.loads(payload) for _, _, kind, payload in store.recent_events(30)
               if kind == "driver_chosen"]
    assert drivers[0]["window"] == "c:\\mt5-z-team"
    assert drivers[0]["reason"] == worker.DRIVER_ENABLED
    assert drivers[0]["others"] == []  # the park's own copy was never an "other" window


def test_no_window_enabled_keeps_entries_and_names_the_machine_setting_once(
        tmp_path, monkeypatch) -> None:
    """Nothing to bypass: the door stays open, the switch is named, and the journal says it once.

    A park that retried an order the terminal refuses into the journal every cycle would be the same
    10027 noise this rule exists to explain, so the word rides on the once-per-process `driver_chosen`
    and the old deterministic order keeps naming the driver.
    """
    account, runner, store, sent, snapshot, day, _spec = _gate(tmp_path, monkeypatch, [])
    _census(store, _algo_rows(r"C:\mt5-exness", r"C:\mt5-team"))
    assert runner._second_window(account) == ""
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1
    assert sent == [1]
    drivers = [json.loads(payload) for _, _, kind, payload in store.recent_events(30)
               if kind == "driver_chosen"]
    assert len(drivers) == 1
    assert drivers[0]["reason"] == worker.DRIVER_NONE_ENABLED
    assert drivers[0]["trade_allowed"] is False
    assert drivers[0]["window"] == "c:\\mt5-exness"  # the old order decides, unchanged
    assert "ALGO TRADING" in drivers[0]["detail"]
    for _ in range(3):
        assert runner._second_window(account) == ""
    assert worker.Worker(runner.config, store)._second_window(account) == ""
    kinds = [kind for _, _, kind, _ in store.recent_events(30)]
    assert kinds.count("driver_chosen") == 1  # more cycles, another Worker: one sentence
    assert kinds.count("account_second_window") == 0


def test_status_says_the_chosen_window_can_trade(tmp_path, monkeypatch, capsys) -> None:
    """`status` names the window that can actually send, not merely the first path it saw."""
    store = Store(tmp_path / "book.sqlite")
    registry = tmp_path / "accounts.yaml"
    registry.write_text(_TWO_WINDOWS, encoding="utf-8")
    _census(store, [_row(r"C:\mt5-exness", 114929502, "MetaQuotes-Demo", trade_mode=_DEMO_MODE),
                    _row(r"C:\mt5-team", 114929502, "MetaQuotes-Demo", trade_mode=_DEMO_MODE,
                         trade_allowed=True)])
    monkeypatch.setattr(worker, "scan_roster",
                        lambda paths=None: pytest.fail("status must not probe terminals"))
    fleet_cli.main(["--config", str(registry), "--store", str(tmp_path / "book.sqlite"), "status"])
    out = capsys.readouterr().out
    assert "окно-водитель c:\\mt5-team" in out
    assert "алгоритмическое торговление включено" in out
    assert "входы по этому счёту закрыты" not in out


def test_status_names_the_machine_setting_when_no_window_can_trade(tmp_path, monkeypatch,
                                                                   capsys) -> None:
    """The one sentence the operator can act on: the terminal switch is off, and only he can flip it."""
    store = Store(tmp_path / "book.sqlite")
    registry = tmp_path / "accounts.yaml"
    registry.write_text(_TWO_WINDOWS, encoding="utf-8")
    _census(store, [_row(r"C:\mt5-exness", 114929502, "MetaQuotes-Demo", trade_mode=_DEMO_MODE,
                         trade_allowed=False),
                    _row(r"C:\mt5-team", 114929502, "MetaQuotes-Demo", trade_mode=_DEMO_MODE,
                         trade_allowed=False)])
    monkeypatch.setattr(worker, "scan_roster",
                        lambda paths=None: pytest.fail("status must not probe terminals"))
    fleet_cli.main(["--config", str(registry), "--store", str(tmp_path / "book.sqlite"), "status"])
    out = capsys.readouterr().out
    assert "окно-водитель c:\\mt5-exness" in out
    assert "ALGO TRADING" in out
    assert "входы по этому счёту закрыты" not in out
    assert "парк закрывает" not in out



def test_a_blocked_account_still_manages_every_position_it_holds(tmp_path, monkeypatch) -> None:
    """The gate closes new entries and nothing else.

    Moving the census check to the top of the cycle — the obvious tidy place for it — would stop
    break-even and stop-trailing on a live position for as long as somebody else holds the same
    account. That trade is never worth the signal it saves, so it is pinned here at cycle level.
    """
    from fleet import risk as risk_mod

    account, runner, store, sent, snapshot, day, _spec = _gate(
        tmp_path, monkeypatch, [_row(r"C:\mt5-team", 1)])
    position = types.SimpleNamespace(ticket=11, symbol="EURUSD", type=0, volume=0.01,
                                     price_open=1.10, profit=0.0, sl=1.098, tp=1.104, magic=7)
    seen: list[int] = []
    monkeypatch.setattr(risk_mod, "AccountSnapshot",
                        types.SimpleNamespace(read=lambda *a, **k: snapshot))
    monkeypatch.setattr(risk_mod, "positions_for", lambda acc: [position])
    monkeypatch.setattr(risk_mod, "daily_net_flow", lambda d: 0.0)
    monkeypatch.setattr(risk_mod, "daily_realized", lambda d: 0.0)
    monkeypatch.setattr(risk_mod, "server_day_now", lambda: day.day)
    monkeypatch.setattr(worker.Worker, "_mirror_risk_usd", lambda self, *a, **k: {})
    monkeypatch.setattr(worker.Worker, "_manage_positions",
                        lambda self, acc, d, positions, *extra:
                        seen.append(len(positions)) or 1)
    result = runner._cycle(account, allow_entries=True)
    assert seen == [1]  # management ran, and ran with the position in hand
    assert result.closed == 1 and result.opened == 0
    assert sent == []  # not one order sent into the shared margin


def test_an_out_of_date_census_is_not_evidence(tmp_path, monkeypatch) -> None:
    """A count nobody can vouch for opens the field no more than a clean one does.

    The block lifts when the park re-counts the windows — the cycle takes a census every
    ROSTER_REFRESH_SECONDS — not because the old count aged out and became unknown-safe.
    """
    import time as time_mod

    account, runner, store, sent, snapshot, day, _spec = _gate(
        tmp_path, monkeypatch, [_row(r"C:\mt5-team", 1)])
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    _census(store, [_row(str(account.data_dir), 1), _row(r"C:\mt5-team", 1)],
            taken=time_mod.time() - worker.ROSTER_EVIDENCE_SECONDS - 1)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    kinds = [kind for _, _, kind, _ in store.recent_events(30)]
    assert "account_single_window" not in kinds  # the field was never seen clear, so no all-clear
    assert "roster_unknown" in kinds
    _census(store, [_row(str(account.data_dir), 1)])
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1
    kinds = [kind for _, _, kind, _ in store.recent_events(30)]
    assert "account_single_window" in kinds  # and the operator sees why it lifted


def test_no_census_at_all_is_not_a_clean_field(tmp_path, monkeypatch) -> None:
    """A park that has not counted this machine's windows cannot claim nobody shares the money."""
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch, (1, "S"))
    _forget_census(store)
    assert worker.roster_rows(store) is None
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert sent == []


def test_a_census_written_by_another_machine_is_not_this_machines_evidence(tmp_path,
                                                                            monkeypatch) -> None:
    """The book is shared; the process list is not.

    Box B's census lists box B's windows. Read as evidence here it would say "one window per
    account" while box A holds a second driver that B never saw.
    """
    account, runner, store, sent, snapshot, day, _spec = _gate(
        tmp_path, monkeypatch, [_row(r"C:\mt5-team", 1)])
    _forget_census(store)
    _census(store, [_row(str(account.data_dir), 1)], host="box-that-is-not-this-one")
    assert worker.roster_rows(store) is None
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert sent == []
    kinds = [kind for _, _, kind, _ in store.recent_events(30)]
    assert "roster_unknown" in kinds


def test_a_window_the_os_would_not_name_closes_the_entries(tmp_path, monkeypatch) -> None:
    """Win32 hides ExecutablePath from another user's session: an unnamed window is not a clean one."""
    store = Store(tmp_path / "book.sqlite")
    fake = CensusMT5({})
    monkeypatch.setattr(worker, "mt5", fake)
    monkeypatch.setattr(terminals, "running_terminals", lambda: [(99, "")])
    rows = worker.refresh_roster(store)
    assert rows[0]["login"] is None and rows[0]["error"]
    assert [row["dir"] for row in worker.blind_windows(rows)] == [""]
    assert fake.initialize_calls == []  # an empty path would mean "the default terminal": never ask
    account, runner, _store, sent, snapshot, day, _spec = _gate(tmp_path, monkeypatch, [])
    _census(store, rows)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert sent == []


def test_an_empty_census_counts_as_a_field_with_no_other_driver(tmp_path, monkeypatch) -> None:
    """No window is up at census time — the park's own copy is started by attach()."""
    account, runner, store, sent, snapshot, day, _spec = _gate(tmp_path, monkeypatch, [])
    _census(store, [])
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1
    assert sent == [1]


def test_the_census_believes_the_window_that_answers_not_the_path_we_asked(
        tmp_path, monkeypatch) -> None:
    """initialize(path=A) can be answered by window B — measured, not folklore.

    Naming the row after the request would collapse two windows into one folder, hide the second
    driver, and let both spend the same free margin.
    """
    store = Store(tmp_path / "book.sqlite")
    fake = CensusMT5({r"C:\mt5-team\terminal64.exe": (1, "S", r"C:\mt5-exness"),
                      r"C:\mt5-exness\terminal64.exe": (1, "S", r"C:\mt5-exness")})
    monkeypatch.setattr(worker, "mt5", fake)
    monkeypatch.setattr(terminals, "running_terminals",
                        lambda: [(1, r"C:\mt5-team\terminal64.exe"),
                                 (2, r"C:\mt5-exness\terminal64.exe")])
    rows = worker.refresh_roster(store)
    assert [row["dir"] for row in rows] == ["c:\\mt5-exness", "c:\\mt5-exness"]
    assert worker.duplicated_accounts(rows) == {"1@s": ["c:\\mt5-exness", "c:\\mt5-exness"]}


def test_duplicated_accounts_does_not_merge_two_brokers_one_login(tmp_path) -> None:
    """Login 1 at Exness and login 1 at GTC are two piles of money, not one driver."""
    rows = [_row(r"C:\mt5-a", 1, "Exness-MT5Real26"), _row(r"C:\mt5-b", 1, "GTC-Go-Live")]
    assert worker.duplicated_accounts(rows) == {}
    assert worker.duplicated_accounts([_row(r"C:\mt5-a", 1, "Exness-MT5Real26"),
                                       _row(r"C:\mt5-b", 1, "exness-mt5real26")]) == {
        "1@exness-mt5real26": ["c:\\mt5-a", "c:\\mt5-b"]}


def test_the_census_runs_before_anything_is_attached(tmp_path, monkeypatch, fleet) -> None:
    """A probe ends with shutdown(): taken after the attach it would drop the live connection."""
    store, _events = fleet
    order: list[str] = []
    account = make_account("a", 1, "S", shard=0)
    monkeypatch.setattr(worker, "refresh_roster",
                        lambda store_arg, **k: order.append("census") or [])
    monkeypatch.setattr(worker.Worker, "attach",
                        lambda self, acc: order.append(f"attach:{acc.name}") or "тест: окна нет")
    results = worker.run_shard(FleetConfig(accounts=[account]), store, 0)
    assert order == ["census", "attach:a"]
    assert [r.status for r in results] == ["error"]


def test_a_cycle_survives_a_process_list_it_cannot_read(tmp_path, monkeypatch, fleet) -> None:
    """No process list is not a reason to lose the cycle: exits still have to run."""
    store, _events = fleet
    account = make_account("a", 1, "S", shard=0)

    def explode():
        raise OSError("no process list")

    monkeypatch.setattr(terminals, "running_terminals", explode)
    attached: list[str] = []
    monkeypatch.setattr(worker.Worker, "attach",
                        lambda self, acc: attached.append(acc.name) or "тест: окна нет")
    results = worker.run_shard(FleetConfig(accounts=[account]), store, 0)
    assert [r.status for r in results] == ["error"]
    assert attached == ["a"]  # an uncountable field costs the census, not the cycle
    assert any(kind == "roster_error" for _, _, kind, _ in store.recent_events(10))


# --- what the operator sees --------------------------------------------------------------------

_TWO_WINDOWS = """
accounts:
  - name: metaquotes
    login: 114929502
    server: MetaQuotes-Demo
    mode: demo
"""


def test_terminals_shows_two_windows_holding_one_account(tmp_path, monkeypatch, capsys) -> None:
    registry = tmp_path / "accounts.yaml"
    registry.write_text(_TWO_WINDOWS, encoding="utf-8")
    rows = [_row(r"C:\mt5-exness", 114929502, "MetaQuotes-Demo"),
            _row(r"C:\mt5-team", 114929502, "MetaQuotes-Demo")]
    monkeypatch.setattr(worker, "scan_roster", lambda paths=None: rows)
    code = fleet_cli.main(["--config", str(registry), "--store", str(tmp_path / "b.sqlite"),
                           "terminals"])
    out = capsys.readouterr().out
    assert code == 0
    # The warning names the account by its identity key, login@server in lowercase: that is the
    # same spelling the gate groups windows by, so what the operator reads is what was counted.
    assert "114929502@metaquotes-demo открыт в 2 окнах" in out
    assert "mt5-exness" in out and "mt5-team" in out


def test_status_shows_the_census_without_probing_a_terminal(tmp_path, monkeypatch, capsys) -> None:
    store = Store(tmp_path / "book.sqlite")
    registry = tmp_path / "accounts.yaml"
    registry.write_text(_TWO_WINDOWS, encoding="utf-8")
    _census(store, [_row(r"C:\mt5-exness", 114929502, "MetaQuotes-Demo"),
                    _row(r"C:\mt5-team", 114929502, "MetaQuotes-Demo")])
    monkeypatch.setattr(worker, "scan_roster",
                        lambda paths=None: pytest.fail("status must not probe terminals"))
    fleet_cli.main(["--config", str(registry), "--store", str(tmp_path / "book.sqlite"), "status"])
    out = capsys.readouterr().out
    assert "окна на этой машине: 2" in out
    assert "входы по этому счёту закрыты" in out


def test_status_names_the_driver_when_the_shared_login_is_demo(tmp_path, monkeypatch, capsys) -> None:
    """`status` must not promise a closed door about the one the gate just opened.

    The census and the registry are read the same way here as in the cycle; the line the operator sees
    is the decision, with the window it chose, and it still says nothing about the money path.
    """
    store = Store(tmp_path / "book.sqlite")
    registry = tmp_path / "accounts.yaml"
    registry.write_text(_TWO_WINDOWS, encoding="utf-8")
    _census(store, [_row(r"C:\mt5-exness", 114929502, "MetaQuotes-Demo", trade_mode=_DEMO_MODE),
                    _row(r"C:\mt5-team", 114929502, "MetaQuotes-Demo", trade_mode=_DEMO_MODE)])
    monkeypatch.setattr(worker, "scan_roster",
                        lambda paths=None: pytest.fail("status must not probe terminals"))
    fleet_cli.main(["--config", str(registry), "--store", str(tmp_path / "book.sqlite"), "status"])
    out = capsys.readouterr().out
    assert "окна на этой машине: 2" in out
    assert "входы по этому счёту закрыты" not in out
    assert "окно-водитель c:\\mt5-exness" in out
    assert "driver_chosen" in out  # and where to read the decision in the book


def test_terminals_names_the_driver_instead_of_promising_a_closed_door(tmp_path, monkeypatch,
                                                                      capsys) -> None:
    registry = tmp_path / "accounts.yaml"
    registry.write_text(_TWO_WINDOWS, encoding="utf-8")
    rows = [_row(r"C:\mt5-exness", 114929502, "MetaQuotes-Demo", trade_mode=_DEMO_MODE),
            _row(r"C:\mt5-team", 114929502, "MetaQuotes-Demo", trade_mode=_DEMO_MODE)]
    monkeypatch.setattr(worker, "scan_roster", lambda paths=None: rows)
    assert fleet_cli.main(["--config", str(registry), "--store", str(tmp_path / "b.sqlite"),
                           "terminals"]) == 0
    out = capsys.readouterr().out
    assert "114929502@metaquotes-demo открыт в 2 окнах" in out
    assert "парк закрывает" not in out
    assert "окно-водитель названо" in out and "c:\\mt5-exness" in out


def test_the_cli_reads_aloud_on_a_legacy_console(tmp_path, monkeypatch) -> None:
    """A cp1252 stdout used to abort `status` on its first Russian line.

    Worse than a garbled word: `UnicodeEncodeError` is a `ValueError`, so the CLI's own error
    handler caught it and then crashed printing "ошибка конфигурации" in Cyrillic too. The operator
    running this on a Russian Windows box — the normal case — got a traceback instead of a census.
    """
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    registry = tmp_path / "accounts.yaml"
    registry.write_text(_TWO_WINDOWS, encoding="utf-8")
    code = fleet_cli.main(["--config", str(registry), "--store", str(tmp_path / "book.sqlite"),
                           "status"])
    stream.flush()
    assert code == 0
    assert "портфель" in buf.getvalue().decode("utf-8")


# --- the identity question, asked the same way every time --------------------------------------
def test_the_gate_before_the_send_reads_a_hand_typed_server_name_as_attach_does(
        tmp_path, monkeypatch) -> None:
    """A registry someone typed says ` s ` where the broker says `S`: attach() knew, the send gate did not.

    The park attached, sized the lot and reserved the risk against its own account, then refused to
    spend it — journalling `identity_lost`, the line that means «this window was hijacked», on every
    symbol, forever.
    """
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S"))
    account.server = " s "
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1
    assert sent == [1]
    assert not any(kind == "identity_lost" for _ts, _a, kind, _p in store.recent_events(40))


def test_a_server_that_only_looks_similar_is_still_someone_else(tmp_path, monkeypatch) -> None:
    """Normalising case and space must not turn the gate into a substring test."""
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S-MT5Real26"))
    account.server = "S"
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert sent == []
    assert any(kind == "identity_lost" for _ts, _a, kind, _p in store.recent_events(40))


def test_a_stop_the_broker_will_not_take_is_a_hold_and_the_money_goes_back(
        tmp_path, monkeypatch) -> None:
    """The refusal arrives after the reservation: a slot nobody owns holds the portfolio cap for a
    full ten minutes, and a hold written as an error teaches the operator to ignore `entry_failed`."""
    account, runner, store, sent, snapshot, day, _spec = _entry_harness(tmp_path, monkeypatch,
                                                                        (1, "S"))
    monkeypatch.setattr(worker.broker, "market_order", lambda *a, **k: (
        (_ for _ in ()).throw(worker.broker.StopsRefused("EURUSD: стоп в 20.0 п. от цены"))))
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert sent == []
    assert not store.reserved_volume()
    kinds = [kind for _ts, _a, kind, _p in store.recent_events(40)]
    assert "entry_stops_refused" in kinds and "entry_failed" not in kinds and "opened" not in kinds
