"""Operational tests: the CLI contract, terminal provisioning and the real-account gate.

These are the paths a critic found untested while the docs claimed they were covered: exit codes,
the startup file bytes, and the guarantee that a real account changes nothing without --live.
"""
from __future__ import annotations

import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "scripts"))

import fleet_cli  # noqa: E402
from fleet import config as fleet_config  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_fleet_root(tmp_path, monkeypatch):
    """Provision into a temporary fleet root: tests must never touch the real fleet folder."""
    root = tmp_path / "fleet"
    monkeypatch.setattr(fleet_config, "FLEET_ROOT", root)
    monkeypatch.setattr(fleet_cli.worker, "Worker", fleet_cli.worker.Worker)
    return root
from fleet import terminals, worker  # noqa: E402
from fleet.config import Account, FleetConfig, load  # noqa: E402
from fleet.store import Store  # noqa: E402


# --- CLI contract ---------------------------------------------------------------------------

def test_missing_registry_is_a_clear_error(tmp_path: pathlib.Path, capsys) -> None:
    code = fleet_cli.main(["--config", str(tmp_path / "nope.yaml"),
                           "--store", str(tmp_path / "s.sqlite"), "once"])
    assert code == 3
    capsys.readouterr()


def test_once_reports_failures_with_exit_2(tmp_path: pathlib.Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "accounts.yaml"
    config_path.write_text("""
accounts:
  - name: demo
    login: 1
    server: Demo
    mode: demo
""", encoding="utf-8")
    store = Store(tmp_path / "fleet.sqlite")

    class FakeWorker:
        def __init__(self, *a, **k):
            pass

        def run_account(self, account, allow_entries=True):
            return worker.WorkerResult(account.name, "error", "terminal down")

    monkeypatch.setattr(fleet_cli, "worker", types.SimpleNamespace(Worker=FakeWorker,
                                                                   WorkerResult=worker.WorkerResult))
    code = fleet_cli.main(["--config", str(config_path), "--store", str(store.path), "once"])
    assert code == 2
    assert "terminal down" in capsys.readouterr().out


def test_real_account_without_live_is_dry_run(tmp_path: pathlib.Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "accounts.yaml"
    config_path.write_text("""
accounts:
  - name: real
    login: 2
    server: Broker-Live
    mode: real
""", encoding="utf-8")
    monkeypatch.setattr(worker.Worker, "attach", lambda self, account: "тест: без терминала")
    monkeypatch.setattr(worker.Worker, "detach", lambda self: None)
    code = fleet_cli.main(["--config", str(config_path),
                           "--store", str(tmp_path / "fleet.sqlite"), "once",
                           "--account", "real"])
    output = capsys.readouterr().out
    assert "живые ордера выключены" in output
    assert code in (0, 2)  # no terminal here: the cycle itself may fail, the gate is what matters


# --- terminal provisioning ------------------------------------------------------------------

def _two_broker_registry(tmp_path: pathlib.Path) -> pathlib.Path:
    """Two platforms on one box: one window is ours to stop, one belongs to another machine."""
    config_path = tmp_path / "accounts.yaml"
    config_path.write_text("""
accounts:
  - name: exness
    login: 5001
    server: Exness-Demo
    mode: demo
  - name: gtc
    login: 5002
    server: GTC-Demo
    mode: demo
    host: other-box
""", encoding="utf-8")
    return config_path


def test_stopping_a_window_names_one_account_and_touches_only_it(
        tmp_path: pathlib.Path, monkeypatch, capsys) -> None:
    config_path = _two_broker_registry(tmp_path)
    store_path = tmp_path / "fleet.sqlite"
    stopped: list[str] = []
    monkeypatch.setattr(terminals, "stop",
                        lambda account: stopped.append(account.name) or 4242)
    monkeypatch.setattr(terminals, "pids_alive", lambda pids: set())  # the kill really took
    code = fleet_cli.main(["--config", str(config_path), "--store", str(store_path),
                           "stop", "--account", "exness"])
    assert code == 0
    assert stopped == ["exness"]
    output = capsys.readouterr().out
    assert "pid 4242" in output
    assert "остались открытыми" in output  # the consequence, not just the verb
    assert ("window_stopped", "4242") in [
        (kind, str(json.loads(payload).get("pid")))
        for _, _, kind, payload in Store(store_path).recent_events(limit=5)]


def test_a_window_that_survived_the_kill_is_not_reported_as_stopped(
        tmp_path: pathlib.Path, monkeypatch, capsys) -> None:
    """`taskkill` can be refused, and a terminal re-spawns itself: the claim must follow the pid."""
    config_path = _two_broker_registry(tmp_path)
    store_path = tmp_path / "fleet.sqlite"
    monkeypatch.setattr(terminals, "stop", lambda account: 4242)
    monkeypatch.setattr(terminals, "pids_alive", lambda pids: {4242})
    code = fleet_cli.main(["--config", str(config_path), "--store", str(store_path),
                           "stop", "--account", "exness"])
    assert code == 1
    output = capsys.readouterr().out
    assert "всё ещё работает" in output and "окно pid 4242 остановлено" not in output
    assert [kind for _, _, kind, _ in Store(store_path).recent_events(limit=5)] == [
        "window_stop_failed"]


def test_the_cli_refuses_to_stop_a_window_another_machine_drives(
        tmp_path: pathlib.Path, monkeypatch, capsys) -> None:
    config_path = _two_broker_registry(tmp_path)
    called: list[str] = []
    monkeypatch.setattr(terminals, "stop", lambda account: called.append(account.name))
    code = fleet_cli.main(["--config", str(config_path), "--store", str(tmp_path / "f.sqlite"),
                           "stop", "--account", "gtc"])
    assert code == 1
    assert called == []
    assert "чужое окно парк не гасит" in capsys.readouterr().out


def test_stopping_a_window_that_is_not_up_says_so_and_journals_nothing(
        tmp_path: pathlib.Path, monkeypatch, capsys) -> None:
    """No pid means nothing was killed: a silent success must not read as a stopped window."""
    config_path = _two_broker_registry(tmp_path)
    store_path = tmp_path / "fleet.sqlite"
    monkeypatch.setattr(terminals, "stop", lambda account: None)
    code = fleet_cli.main(["--config", str(config_path), "--store", str(store_path),
                           "stop", "--account", "exness"])
    assert code == 0
    assert "останавливать нечего" in capsys.readouterr().out
    assert Store(store_path).recent_events(limit=5) == []  # nothing happened, nothing journalled


def _source(tmp_path: pathlib.Path) -> pathlib.Path:
    source = tmp_path / "install"
    (source / "config").mkdir(parents=True)
    (source / "MQL5" / "Experts").mkdir(parents=True)
    (source / "terminal64.exe").write_bytes(b"binary-v1")
    (source / "settings.ini").write_text("[x]", encoding="utf-8")
    return source


def test_startup_config_has_single_crlf_and_no_password(tmp_path: pathlib.Path) -> None:
    account = Account(name="a", login=123, server="Demo", mode="demo",
                      terminal_source=_source(tmp_path))
    terminals.provision(account)
    raw = (account.data_dir / "config" / "common.ini").read_bytes()
    assert b"\r\r\n" not in raw
    assert b"Login=123" in raw
    assert b"Password=" not in raw
    assert b"AllowLiveTrading=0" in raw  # demo account: live trading is not enabled


def test_a_portable_config_on_a_shared_inode_is_detached_before_it_is_written(
        tmp_path: pathlib.Path) -> None:
    """A hardlink is two names for one file: writing one rewrites the other.

    Measured on this machine: `Config/common.ini` and seven more under
    `C:\\mt5-fleet\\terminals\\114929502-metaquotes-demo` carry three links each, one of them in
    `C:\\Program Files\\MetaTrader 5\\Config`. `open(path, "w")` truncates the inode, so the park's
    first start-up file would have replaced the settings of the terminal the operator trades in.
    """
    import os

    account = Account(name="a", login=123, server="Demo", mode="demo",
                      terminal_source=_source(tmp_path))
    terminals.provision(account)
    ours = account.data_dir / "config" / "common.ini"
    install = tmp_path / "install" / "common.ini"
    try:
        install.unlink(missing_ok=True)
        os.link(ours, install)
    except OSError:
        pytest.skip("this filesystem has no hardlinks")
    operator_bytes = b"[Common]\r\nServer=Operator-Own-Install\r\n"
    install.write_bytes(operator_bytes)
    assert os.stat(ours).st_nlink == 2

    terminals.write_startup_config(account)

    assert os.stat(ours).st_nlink == 1
    assert install.read_bytes() == operator_bytes  # the install kept its own file
    assert b"Login=123" in ours.read_bytes() and b"Operator-Own" not in ours.read_bytes()


def test_a_second_provision_without_a_seed_keeps_the_login_that_already_works(
        tmp_path: pathlib.Path) -> None:
    """A missing `seed_config` is a reason not to copy, not a licence to erase.

    The operator's manual login leaves `accounts.dat` in this portable folder, and every `run`
    re-provisions it. Clearing the session material before checking whether a replacement exists
    left the terminal at a password prompt the park cannot answer — with its own working session in
    the recycle bin of the code path that deleted it.
    """
    source = _source(tmp_path)
    account = Account(name="a", login=123, server="Demo", mode="demo", terminal_source=source)
    terminals.provision(account)
    session = account.data_dir / "config" / "accounts.dat"
    session.write_bytes(b"saved-login-of-this-account")

    terminals.provision(account)  # what `run` does again on every start

    assert session.read_bytes() == b"saved-login-of-this-account"
    ok, detail = terminals.seed_status(account)
    assert not ok and "seed_config" in detail

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "accounts.dat").write_bytes(b"another-brokers-saved-logins")
    seeded = Account(name="b", login=124, server="Demo", mode="demo", terminal_source=source,
                     seed_config=seed)
    terminals.provision(seeded)
    assert (seeded.data_dir / "config" / "accounts.dat").read_bytes() == (
        b"another-brokers-saved-logins")


def test_real_account_enables_live_trading_in_the_startup_file(tmp_path: pathlib.Path) -> None:
    account = Account(name="r", login=124, server="Live", mode="real",
                      terminal_source=_source(tmp_path))
    terminals.provision(account)
    raw = (account.data_dir / "config" / "common.ini").read_bytes()
    assert b"AllowLiveTrading=1" in raw


def test_provision_refreshes_binaries_replaced_at_source(tmp_path: pathlib.Path,
                                                         monkeypatch) -> None:
    source = _source(tmp_path)
    account = Account(name="a", login=125, server="Demo", mode="demo", terminal_source=source)
    monkeypatch.setattr(terminals, "running_terminals", lambda: [])
    terminals.provision(account)
    assert (account.data_dir / "terminal64.exe").read_bytes() == b"binary-v1"
    (source / "terminal64.exe").write_bytes(b"binary-v2")
    terminals.provision(account)
    assert (account.data_dir / "terminal64.exe").read_bytes() == b"binary-v2"


def test_provision_refuses_to_wipe_a_directory_a_window_is_running_from(
        tmp_path: pathlib.Path, monkeypatch) -> None:
    """Rebuilding under a live window is how the park used to orphan its own money.

    The window keeps its positions and the margin they spend, while the directory it runs from is
    gone: the park can no longer attach to it, and the census then reports a driver it cannot name.
    """
    account = Account(name="a", login=128, server="Demo", mode="demo",
                      terminal_source=_source(tmp_path))
    monkeypatch.setattr(terminals, "running_terminals", lambda: [])
    terminals.provision(account)
    (account.data_dir / "config" / "keep.txt").write_text("session", encoding="utf-8")
    monkeypatch.setattr(terminals, "running_terminals",
                        lambda: [(777, str(account.terminal_exe))])
    with pytest.raises(FileExistsError, match="stop --account a"):
        terminals.provision(account, force=True)
    assert (account.data_dir / "config" / "keep.txt").read_text(encoding="utf-8") == "session"


def test_provision_rebuilds_the_same_account_once_its_window_is_down(
        tmp_path: pathlib.Path, monkeypatch) -> None:
    account = Account(name="a", login=129, server="Demo", mode="demo",
                      terminal_source=_source(tmp_path))
    monkeypatch.setattr(terminals, "running_terminals", lambda: [])
    terminals.provision(account)
    (account.data_dir / "config" / "common.ini").write_text("stale", encoding="utf-8")
    # A neighbour's window running from a different folder is not a reason to refuse.
    monkeypatch.setattr(terminals, "running_terminals",
                        lambda: [(888, str(account.data_dir.parent / "999-Demo" /
                                           "terminal64.exe"))])
    terminals.provision(account, force=True)
    assert b"Login=129" in (account.data_dir / "config" / "common.ini").read_bytes()


def test_seed_status_reports_a_missing_session(tmp_path: pathlib.Path) -> None:
    account = Account(name="a", login=126, server="Demo", mode="demo",
                      terminal_source=_source(tmp_path))
    ok, detail = terminals.seed_status(account)
    assert not ok and "seed_config" in detail
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "accounts.dat").write_bytes(b"x")
    account.seed_config = str(seed)
    ok, detail = terminals.seed_status(account)
    assert ok and detail == ""


def test_pid_cache_does_not_pin_a_missing_process(tmp_path: pathlib.Path, monkeypatch) -> None:
    account = Account(name="a", login=127, server="Demo", mode="demo",
                      terminal_source=_source(tmp_path))
    answers = iter([[], [(9, str(account.terminal_exe))]])

    def fake_processes():
        return next(answers, [])

    monkeypatch.setattr(terminals, "running_terminals", fake_processes)
    monkeypatch.setattr(terminals, "_PID_CACHE", {})
    assert terminals.running_pid(account) is None
    assert terminals.running_pid(account) == 9  # a cached "None" must not stick


# --- real-account gate is real, not a tautology ---------------------------------------------

def test_real_account_writes_no_orders_and_changes_no_stop_without_live(
        tmp_path: pathlib.Path, monkeypatch) -> None:
    account = Account(name="real", login=5, server="Srv", mode="real")
    store = Store(tmp_path / "book.sqlite")
    runner = worker.Worker(FleetConfig(accounts=[account]), store, dry_run=False,
                           allow_live=False)
    calls = {"order_send": 0, "modify": 0}
    monkeypatch.setattr(worker.broker, "market_order",
                        lambda *a, **k: calls.__setitem__("order_send", calls["order_send"] + 1))
    monkeypatch.setattr(worker.broker, "modify_position",
                        lambda *a, **k: calls.__setitem__("modify", calls["modify"] + 1))
    monkeypatch.setattr(worker.broker, "symbol_exact",
                        lambda name: worker.broker.BrokerSymbol(
                            name=name, digits=5, point=0.00001, tick_size=0.00001,
                            tick_value=1.0, volume_min=0.01, volume_max=100.0,
                            volume_step=0.01, stops_level=0, freeze_level=0, spread=1,
                            filling=3))
    monkeypatch.setattr(worker.broker, "close_position",
                        lambda *a, **k: calls.__setitem__("close", 1))
    monkeypatch.setattr(worker.broker, "atr", lambda *a, **k: 0.001)
    position = types.SimpleNamespace(ticket=1, symbol="EURUSD", type=0, volume=0.01,
                                     price_open=1.1000, profit=1.0, sl=1.0990, tp=0.0,
                                     magic=0, time=0)
    monkeypatch.setattr(worker.risk, "positions_for", lambda acc: [position])
    monkeypatch.setattr(worker.mt5, "symbol_info_tick",
                        lambda name: types.SimpleNamespace(bid=1.1006, ask=1.1006, time=1))
    monkeypatch.setattr(worker.strategy, "break_even_level",
                        lambda *a, **k: 1.1000)
    monkeypatch.setattr(worker.strategy, "exit_reason", lambda *a, **k: "тейк")
    day = worker.risk.DayState(day="2026-11-03", start_balance=1000.0)
    runner._manage_positions(account, day, [position])
    assert calls["order_send"] == 0 and calls["modify"] == 0 and calls.get("close", 0) == 0


# --- the VPN tunnel is the park's own, and only the park's ---------------------------------------

def test_the_tunnel_reader_separates_a_pid_from_its_command_line(monkeypatch) -> None:
    """One CIM call, one line per session: the tab is the only thing keeping pid and args apart."""
    from fleet import network

    out = ("111\tC:\\Program Files\\OpenVPN\\bin\\openvpn.exe --config C:\\openvpn\\a.ovpn\n"
           "222\t\n")
    monkeypatch.setattr(network.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=out))
    assert network.openvpn_sessions() == [
        (111, "C:\\Program Files\\OpenVPN\\bin\\openvpn.exe --config C:\\openvpn\\a.ovpn"),
        (222, "")]


def test_stopping_the_tunnel_leaves_the_desks_own_vpn_alone(monkeypatch) -> None:
    """The park's tunnel is named by the session log it was started with — nothing else is.

    `taskkill /IM openvpn.exe /F` closed every OpenVPN on the box, including the one the trading
    desk uses to reach its brokers: ending one account's tunnel then put every other account dark.
    """
    from fleet import network

    sessions = [(111, '"C:\\Program Files\\OpenVPN\\bin\\openvpn.exe" --config '
                      '"C:\\Users\\desk\\config\\corp.ovpn"'),
                (222, '"C:\\Program Files\\OpenVPN\\bin\\openvpn.exe" --config '
                      "C:\\openvpn\\broker.ovpn --log C:\\openvpn\\openvpn-broker.log --verb 3")]
    killed: list[list[str]] = []
    monkeypatch.setattr(network, "openvpn_sessions", lambda: sessions)
    monkeypatch.setattr(network.subprocess, "run", lambda cmd, **k: killed.append(cmd))
    assert network.stop_tunnel() == 1
    assert killed == [["taskkill", "/PID", "222", "/F"]]
    assert network.stop_tunnel(profile=r"C:\openvpn\other.ovpn") == 0
    assert len(killed) == 1


def test_the_candidate_ledger_reads_aloud_and_states_its_own_limits(
        tmp_path: pathlib.Path, capsys) -> None:
    """A table nobody can read is not a measurement — and a number that overclaims is worse."""
    from fleet import candidates

    store_path = tmp_path / "fleet.sqlite"
    store = Store(store_path)
    store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02, ticket=7,
                ticket_source="token", currency="USD", rate_to_usd=1.0,
                drift_points=25.0, drift_cost=0.5, drift_cost_usd=0.5, risk_usd=2.0,
                risk_reward=2.0)
    store.event("a", "closed", symbol="EURUSD", ticket=7, reason="trailing", profit=3.2,
                by="парк", currency="USD", rate_to_usd=1.0, profit_usd=3.2)
    assert fleet_cli.main(["--store", str(store_path), "candidates"]) == 0
    out = capsys.readouterr().out
    assert "3.20" in out and "0.50" in out
    assert "кто" in out and "парк" in out
    assert "чего оно не доказывает" in out


def test_a_book_the_journal_cannot_price_says_so_instead_of_summing_it(
        tmp_path: pathlib.Path, capsys) -> None:
    """Old rows carry a bare number and no currency: the CLI must name them, not add them to dollars."""
    from fleet import candidates

    store_path = tmp_path / "fleet.sqlite"
    store = Store(store_path)
    store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02, ticket=7,
                ticket_source="only_position", currency="RUB", drift_points=25.0,
                drift_cost=0.5, risk_usd=2.0)
    store.event("a", "closed", symbol="EURUSD", ticket=7, reason="стоп", profit=-12.5,
                by="брокер")
    assert fleet_cli.main(["--store", str(store_path), "candidates"]) == 0
    out = capsys.readouterr().out
    assert "нет билета" not in out  # the ticket exists; it is only a guess, not a blind row
    assert "итог не в $" in out and "оговорки:" in out
    assert "закрыто по билету брокера 0" in out and "+0.00 USD" in out


def test_an_empty_candidate_book_is_reported_as_no_measurement(tmp_path: pathlib.Path, capsys) -> None:
    store_path = tmp_path / "fleet.sqlite"
    assert fleet_cli.main(["--store", str(store_path), "candidates"]) == 0
    assert "кандидатов пока нет" in capsys.readouterr().out


def test_a_row_that_is_neither_open_nor_settled_is_printed_as_neither(
        tmp_path: pathlib.Path, capsys) -> None:
    """Two entries on one ticket: the close is one lot's money and the table cannot say whose."""
    from fleet import candidates

    store = Store(tmp_path / "fleet.sqlite")
    for _ in range(2):
        store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02, ticket=7,
                    currency="USD", rate_to_usd=1.0, drift_points=25.0, drift_cost=0.5,
                    drift_cost_usd=0.5, risk_usd=2.0)
    assert fleet_cli.main(["--store", str(tmp_path / "fleet.sqlite"), "candidates"]) == 0
    out = capsys.readouterr().out
    assert "общий билет" in out and "делят один билет" in out
    assert "закрыто по билету брокера 0" in out and "открыто 0" in out


def test_a_settled_guess_is_out_of_the_sum_and_says_where_it_went(
        tmp_path: pathlib.Path, capsys) -> None:
    """The blocker: the header promised guessed joins stayed out of the totals it then summed.

    Now the column shows `не в сумме`, and the caveat names the money it counted apart — a number the
    reader can see is honest; one silently missing from the table is not.
    """
    from fleet import candidates

    store = Store(tmp_path / "fleet.sqlite")
    store.event("a", candidates.KIND, symbol="EURUSD", side="buy", volume=0.02, ticket=7,
                ticket_source="only_position", currency="USD", rate_to_usd=1.0,
                drift_points=4.0, drift_cost=0.08, drift_cost_usd=0.08, risk_usd=2.0)
    store.event("a", "closed", symbol="EURUSD", ticket=7, reason="стоп", profit=-12.5,
                by="брокер", currency="USD", rate_to_usd=1.0, profit_usd=-12.5)
    assert fleet_cli.main(["--store", str(tmp_path / "fleet.sqlite"), "candidates"]) == 0
    out = capsys.readouterr().out
    assert "не в сумме" in out and "итог не в $" not in out
    assert "закрыто по билету брокера 0" in out and "посчитано отдельно: 1 на -12.50 USD" in out
    assert "+0.00 USD" in out  # the dollar column is empty because nothing was attributable
