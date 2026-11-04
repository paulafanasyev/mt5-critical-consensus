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

