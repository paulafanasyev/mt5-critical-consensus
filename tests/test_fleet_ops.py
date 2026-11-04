"""Operational tests: the CLI contract, terminal provisioning and the real-account gate.

These are the paths a critic found untested while the docs claimed they were covered: exit codes,
the startup file bytes, and the guarantee that a real account changes nothing without --live.
"""
from __future__ import annotations

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


def test_real_account_enables_live_trading_in_the_startup_file(tmp_path: pathlib.Path) -> None:
    account = Account(name="r", login=124, server="Live", mode="real",
                      terminal_source=_source(tmp_path))
    terminals.provision(account)
    raw = (account.data_dir / "config" / "common.ini").read_bytes()
    assert b"AllowLiveTrading=1" in raw


def test_provision_refreshes_binaries_replaced_at_source(tmp_path: pathlib.Path) -> None:
    source = _source(tmp_path)
    account = Account(name="a", login=125, server="Demo", mode="demo", terminal_source=source)
    terminals.provision(account)
    assert (account.data_dir / "terminal64.exe").read_bytes() == b"binary-v1"
    (source / "terminal64.exe").write_bytes(b"binary-v2")
    terminals.provision(account)
    assert (account.data_dir / "terminal64.exe").read_bytes() == b"binary-v2"


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
