"""Every door to real money, tested from the outside.

Three processes can put an order on a live account: the park's worker, `scripts/real_mirror.py` and
`scripts/real_bridge.py`. Each of them is a file that can be copied, so the permission has to live in
the state of the machine — the stand, the `--live` word in this invocation, and the registry row —
and not in the source. These tests are the proof that the doors hold without that state.

They also cover the two gates that stop a refusal from costing money twice: the send-time identity
check, which must free the reservation it just took, and the broker's stops-level refusal, which must
be said once per rule instead of once per tick.
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import sys
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet import broker, config, livegate, shard, terminals, worker  # noqa: E402
from fleet.store import Store  # noqa: E402

_REAL_REGISTRY = """
accounts:
  - name: exness-real
    login: 276261159
    server: Exness-MT5Real26
    mode: real
    enabled: true
    risk:
      max_lots: 0.02
"""


def _registry(tmp_path: pathlib.Path, body: str = _REAL_REGISTRY) -> pathlib.Path:
    """A registry in tmp: the permission these tests hand out must not be the production one."""
    path = tmp_path / "accounts.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _no_stand(tmp_path: pathlib.Path) -> pathlib.Path:
    """A stand that is not on disk: no test may depend on the production `C:\\mt5-fleet\\PAUSE`."""
    return tmp_path / "STAND_OFF"


def _script(name: str, tmp_path: pathlib.Path, monkeypatch):
    """Import a `scripts/` module with its log and state files redirected into tmp."""
    sys.path.insert(0, str(ROOT / "scripts"))
    module = importlib.import_module(name)
    monkeypatch.setattr(module, "LOG", tmp_path / f"{name}.jsonl")
    monkeypatch.setattr(module, "STATE", tmp_path / f"{name}_state.json")
    return module


def _lines(module) -> str:
    return (module.LOG.read_text(encoding="utf-8") if module.LOG.exists() else "")


# --- livegate: the three words that have to agree -------------------------------------------

def test_the_stand_outranks_every_other_permission(tmp_path) -> None:
    registry = _registry(tmp_path)
    stand = tmp_path / "PAUSE"
    stand.write_text("", encoding="utf-8")
    refusal = livegate.refusal(276261159, live=True, server="Exness-MT5Real26",
                               config_path=registry, pause_file=stand)
    assert "стоянка" in refusal
    # the same permission, one second later, with the file gone
    assert livegate.refusal(276261159, live=True, server="Exness-MT5Real26",
                            config_path=registry, pause_file=_no_stand(tmp_path)) == ""


def test_real_money_needs_the_word_live_in_this_invocation(tmp_path) -> None:
    refusal = livegate.refusal(276261159, live=False, server="Exness-MT5Real26",
                               config_path=_registry(tmp_path),
                               pause_file=_no_stand(tmp_path))
    assert "--live" in refusal


def test_a_terminal_that_did_not_name_its_login_cannot_be_permitted(tmp_path) -> None:
    for login in (None, "", "не счёт"):
        refusal = livegate.refusal(login, live=True, server="Exness-MT5Real26",
                                   config_path=_registry(tmp_path),
                                   pause_file=_no_stand(tmp_path))
        assert "не назвал номер" in refusal


@pytest.mark.parametrize("body,why", [
    ("""
accounts:
  - name: other
    login: 111
    server: Exness-MT5Real26
    mode: real
""", "the registry does not carry this login"),
    (_REAL_REGISTRY.replace("mode: real", "mode: demo"), "a demo row is not consent for real money"),
    (_REAL_REGISTRY.replace("enabled: true", "enabled: false"), "the operator switched this row off"),
])
def test_a_registry_row_that_does_not_say_yes_refuses(tmp_path, body, why) -> None:
    refusal = livegate.refusal(276261159, live=True, server="Exness-MT5Real26",
                               config_path=_registry(tmp_path, body),
                               pause_file=_no_stand(tmp_path))
    assert "не разрешён реестром" in refusal, why


def test_the_server_the_broker_answered_has_to_be_the_one_that_was_enabled(tmp_path) -> None:
    registry = _registry(tmp_path)
    assert "не разрешён реестром" in livegate.refusal(
        276261159, live=True, server="Exness-MT5Real27", config_path=registry,
        pause_file=_no_stand(tmp_path))
    # one server, however typed: the same normalisation attach() uses
    assert livegate.refusal(276261159, live=True, server="  exness-mt5real26 ",
                            config_path=registry, pause_file=_no_stand(tmp_path)) == ""


def test_a_broken_or_missing_registry_refuses_instead_of_trading_the_last_one(tmp_path) -> None:
    for path in (tmp_path / "absent.yaml", _registry(tmp_path, "accounts:\n- name: a\n  oops: 1\n")):
        assert livegate.refusal(276261159, live=True, server="Exness-MT5Real26",
                                config_path=path, pause_file=_no_stand(tmp_path)) != ""


def test_the_row_that_allows_the_account_also_carries_its_lot_ceiling(tmp_path) -> None:
    row = livegate.permitted_account(276261159, server="Exness-MT5Real26",
                                     config_path=_registry(tmp_path))
    assert row is not None and row.risk.max_lots == 0.02


def test_the_ceiling_is_applied_before_the_mirrors_default_can_multiply_it(tmp_path) -> None:
    """A script that mirrors 0.05 onto an account capped at 0.01 is not copying the demo."""
    row = livegate.permitted_account(276261159, server="Exness-MT5Real26",
                                     config_path=_registry(tmp_path))
    assert livegate.capped_lots(row, 0.05) == 0.02
    assert livegate.capped_lots(row, 0.01) == 0.01
    assert livegate.capped_lots(None, 0.01) == 0.0


# --- scripts/real_mirror.py: one door -------------------------------------------------------

def test_the_mirror_writes_nothing_the_gate_refuses(tmp_path, monkeypatch) -> None:
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    sent: list[dict] = []

    class FakeMt5:
        def order_send(self, request):
            sent.append(request)
            return types.SimpleNamespace(retcode=10009)

    refused = real_mirror.send_order(FakeMt5(), {"action": 1, "symbol": "EURUSD", "volume": 0.01},
                                     lambda: "нет")
    assert refused is None and sent == []
    assert "NOT SENT" in _lines(real_mirror)
    allowed = real_mirror.send_order(FakeMt5(), {"action": 1, "symbol": "EURUSD", "volume": 0.01},
                                     lambda: "")
    assert allowed is not None and len(sent) == 1


def test_a_stand_put_back_mid_cycle_stops_the_next_send_of_the_same_run(tmp_path,
                                                                          monkeypatch) -> None:
    """The gate is read at the moment of sending, so a stand that appears mid-cycle bites."""
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    reason = {"text": ""}
    sent: list[dict] = []

    class FakeMt5:
        def order_send(self, request):
            sent.append(request)
            return types.SimpleNamespace(retcode=10009)

    mt5 = FakeMt5()
    assert real_mirror.send_order(mt5, {"action": 1, "symbol": "XAUUSD", "volume": 0.01},
                                 lambda: reason["text"]) is not None
    reason["text"] = "стоянка на месте"
    assert real_mirror.send_order(mt5, {"action": 1, "symbol": "EURUSD", "volume": 0.01},
                                 lambda: reason["text"]) is None
    assert [r["symbol"] for r in sent] == ["XAUUSD"]


# --- scripts/real_bridge.py: one door, and only its own lots --------------------------------

def test_a_bridge_nobody_permitted_does_not_even_open_the_demo_terminal(tmp_path, monkeypatch) -> None:
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    monkeypatch.setattr(real_bridge, "demo_book",
                        lambda: (_ for _ in ()).throw(AssertionError("demo terminal touched")))
    ea = tmp_path / "dsh_real_state.json"
    ea.write_text(json.dumps({"login": 276261159, "server": "Exness-MT5Real26", "balance": 100.0,
                              "equity": 100.0, "positions": []}), encoding="utf-8")
    cmd = tmp_path / "dsh_real_cmd.json"
    sentence = real_bridge.run_cycle({}, lambda: "нет слова --live", ea_path=ea, cmd_path=cmd)
    assert "bridge idle" in sentence and not cmd.exists()


def test_the_bridge_closes_only_the_lots_it_can_prove_are_its_own(tmp_path, monkeypatch) -> None:
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    monkeypatch.setattr(real_bridge, "time", types.SimpleNamespace(sleep=lambda s: None,
                                                                   time=lambda: 0.0))
    monkeypatch.setattr(real_bridge, "demo_book", lambda: [])
    monkeypatch.setattr(real_bridge, "hourly_uptrend", lambda symbol: False)
    ea = tmp_path / "dsh_real_state.json"
    ea.write_text(json.dumps({
        "login": 276261159, "server": "Exness-MT5Real26", "balance": 100.0, "equity": 100.0,
        "positions": [{"ticket": 1, "symbol": "EURUSD", "magic": real_bridge.REAL_MAGIC},
                      {"ticket": 2, "symbol": "GBPUSD", "magic": 12345}]}), encoding="utf-8")
    cmd = tmp_path / "dsh_real_cmd.json"
    real_bridge.run_cycle({}, lambda: "", ea_path=ea, cmd_path=cmd)
    assert json.loads(cmd.read_text(encoding="utf-8")) == {"action": "close", "ticket": 1}
    assert "не наши" in _lines(real_bridge)
    assert "2" not in cmd.read_text(encoding="utf-8")


def test_a_state_file_older_than_the_magic_field_closes_nothing(tmp_path, monkeypatch) -> None:
    """An unlabelled lot is the operator's until proven otherwise — guessing closes his money."""
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    monkeypatch.setattr(real_bridge, "demo_book", lambda: [])
    ea = tmp_path / "dsh_real_state.json"
    ea.write_text(json.dumps({"login": 276261159, "server": "Exness-MT5Real26", "balance": 100.0,
                              "equity": 100.0,
                              "positions": [{"ticket": 7, "symbol": "EURUSD"}]}), encoding="utf-8")
    cmd = tmp_path / "dsh_real_cmd.json"
    real_bridge.run_cycle({}, lambda: "", ea_path=ea, cmd_path=cmd)
    assert real_bridge.ours(json.loads(ea.read_text(encoding="utf-8"))) == []
    assert not cmd.exists()


def test_send_command_writes_no_file_on_a_refusal(tmp_path, monkeypatch) -> None:
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    cmd = tmp_path / "dsh_real_cmd.json"
    assert real_bridge.send_command({"action": "open", "symbol": "EURUSD"}, lambda: "стоянка",
                                    cmd_path=cmd) is False
    assert not cmd.exists()
    assert real_bridge.send_command({"action": "open", "symbol": "EURUSD"}, lambda: "",
                                    cmd_path=cmd) is True
    assert json.loads(cmd.read_text(encoding="utf-8"))["symbol"] == "EURUSD"


def test_a_bridge_started_without_the_word_exits_and_sends_nothing(tmp_path, monkeypatch) -> None:
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    monkeypatch.setattr(real_bridge, "send_command",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    monkeypatch.setattr(real_bridge, "EA_STATE", tmp_path / "dsh_real_state.json")
    assert real_bridge.main(["--config", str(_registry(tmp_path))]) == 1
    assert "ничего не отправлено" in _lines(real_bridge)


# --- the shard inherits the stand and the registry it actually reads ------------------------

def test_a_shard_started_by_hand_watches_the_same_stand_as_the_conductor(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def fake_run_shard(cfg, store, shard_id, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(shard.worker, "run_shard", fake_run_shard)
    monkeypatch.setattr(shard, "CONFIG_PATH", _registry(tmp_path))
    assert shard.main(["--shard", "0", "--once", "--store", str(tmp_path / "b.sqlite")]) == 0
    assert seen["pause_file"] == config.PAUSE_FILE


def test_a_shard_with_no_config_flag_stops_when_its_registry_breaks(tmp_path, monkeypatch) -> None:
    """`run --shards 0` passes no `--config`, so the default path is the file that must be watched."""
    registry = _registry(tmp_path)
    monkeypatch.setattr(shard, "CONFIG_PATH", registry)
    calls: list[int] = []

    def fake_run_shard(cfg, store, shard_id, **kwargs):
        calls.append(1)
        registry.write_text("accounts:\n- name: a\n  login: 1\n  server: S\n  mode: demo\n"
                            "  oops: 1\n", encoding="utf-8")
        later = time.time() + 5
        os.utime(registry, (later, later))
        return []

    monkeypatch.setattr(shard.worker, "run_shard", fake_run_shard)
    ticks: list[float] = []

    def fake_sleep(seconds):
        ticks.append(seconds)
        if len(ticks) >= 3:
            raise SystemExit(0)

    monkeypatch.setattr(shard.time, "sleep", fake_sleep)
    book = Store(tmp_path / "b.sqlite")
    with pytest.raises(SystemExit):
        shard.main(["--shard", "0", "--store", str(book.path), "--interval", "1"])
    assert len(calls) == 1, "the shard cycled on a registry it knew was broken"
    assert book.count_events("config_rejected") == 1


# --- the worker's own gates -----------------------------------------------------------------

def _demo_account(**over) -> config.Account:
    fields = dict(name="a", login=1, server="S", mode="demo")
    fields.update(over)
    return config.Account(**fields)


def _book(tmp_path: pathlib.Path, account: config.Account) -> Store:
    """A journal that answers every pre-entry gate, so a test fails on the gate it is about."""
    store = Store(tmp_path / "book.sqlite")
    store.set_portfolio_baseline(worker.local_host(), "2026-11-03", 1000.0)
    store.equity(account.name, account.login, 1000.0, 1000.0, 0, 0.0, "USD", 1.0, "2026-11-03")
    row = {"exe": "", "dir": worker.window_dir(str(account.data_dir)), "login": account.login,
           "server": account.server, "currency": "USD", "balance": 1000.0, "equity": 1000.0,
           "error": ""}
    store.set_meta(f"{worker.ROSTER_META_KEY}@{worker.local_host()}",
                   json.dumps({"taken": time.time(), "rows": [row]}))
    return store


def _symbol(**over) -> broker.BrokerSymbol:
    fields = dict(name="EURUSD", digits=5, point=0.00001, tick_size=0.00001, tick_value=1.0,
                  volume_min=0.01, volume_max=100.0, volume_step=0.01, stops_level=0,
                  freeze_level=0, spread=1, filling=3)
    fields.update(over)
    return broker.BrokerSymbol(**fields)


def _signal(**over) -> types.SimpleNamespace:
    fields = dict(base_symbol="EURUSD", side="buy", reason="test", price=1.10, stop=1.098,
                  target=1.104, atr=0.001, stop_distance=0.002)
    fields.update(over)
    return types.SimpleNamespace(**fields)


def _snapshot(account: config.Account):
    from fleet import risk as risk_mod

    return (risk_mod.AccountSnapshot(login=account.login, server=account.server, currency="USD",
                                     balance=1000.0, equity=1000.0, margin_free=1000.0,
                                     leverage=100, positions=0, floating=0.0,
                                     server_day="2026-11-03"),
            risk_mod.DayState(day="2026-11-03", start_balance=1000.0))


def _arm_entries(monkeypatch, spec: broker.BrokerSymbol, signal=None) -> None:
    signal = signal or _signal()
    monkeypatch.setattr(worker.strategy, "signals", lambda acc: [signal])
    monkeypatch.setattr(worker.broker, "symbol", lambda base: spec)
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: spec)
    monkeypatch.setattr(worker.broker, "open_positions", lambda *a, **k: [])
    monkeypatch.setattr(worker.risk, "can_open", lambda *a, **k: (True, ""))


def test_a_window_that_stops_answering_frees_its_reservation(tmp_path, monkeypatch) -> None:
    """A manual re-login used to raise inside the gate; a nameless window must cost nothing."""
    account = _demo_account()
    store = _book(tmp_path, account)
    runner = worker.Worker(config.FleetConfig(accounts=[account]), store, dry_run=False)
    _arm_entries(monkeypatch, _symbol())
    sent: list[int] = []
    monkeypatch.setattr(worker.broker, "market_order",
                        lambda *a, **k: sent.append(1))
    monkeypatch.setattr(worker.mt5, "account_info", lambda: None)
    snapshot, day = _snapshot(account)
    opened = runner._try_entries(account, snapshot, [], day, 1.0)
    assert opened == 0 and sent == []
    assert store.reserved_volume() == 0.0, "a blocked entry kept the portfolio risk slot"
    assert store.count_events("identity_lost") == 1


def test_a_registry_that_typed_the_server_loosely_is_not_blind_to_its_own_account(
        tmp_path, monkeypatch) -> None:
    account = _demo_account(server="S ")
    store = _book(tmp_path, account)
    runner = worker.Worker(config.FleetConfig(accounts=[account]), store, dry_run=False)
    _arm_entries(monkeypatch, _symbol())
    sent: list[int] = []
    monkeypatch.setattr(worker.broker, "market_order",
                        lambda *a, **k: sent.append(1) or types.SimpleNamespace(
                            volume=0.01, price=1.10, retcode=10009, token="t"))
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=1, server="S"))
    snapshot, day = _snapshot(account)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1
    assert sent == [1] and store.count_events("identity_lost") == 0


def test_the_brokers_own_minimum_distance_is_said_once_per_rule(tmp_path, monkeypatch) -> None:
    spec = _symbol(stops_level=20)
    account = _demo_account()
    store = _book(tmp_path, account)
    runner = worker.Worker(config.FleetConfig(accounts=[account]), store, dry_run=False)
    # a stop 15 points away, where this server wants at least 20
    signal = _signal(stop=1.09985, stop_distance=0.00015)
    _arm_entries(monkeypatch, spec, signal)
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=1, server="S"))
    refusals = _shape_refusals()
    ticks: list[int] = []

    def refuse(*a, **k):
        ticks.append(1)
        raise broker.StopsRefused(refusals[len(ticks) % 2])

    monkeypatch.setattr(worker.broker, "market_order", refuse)
    snapshot, day = _snapshot(account)
    for _ in range(4):
        assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert len(ticks) == 4
    assert store.count_events("entry_stops_refused") == 1
    assert store.reserved_volume() == 0.0


def _shape_refusals() -> tuple[str, str]:
    """Two distances a live price walks through under one and the same broker rule."""
    spec = _symbol(stops_level=20)
    return (broker.stops_refusal(spec, True, 1.10, 1.09985, 1.104),
            broker.stops_refusal(spec, True, 1.10003, 1.09985, 1.104))


def test_distances_of_one_rule_share_a_shape_and_another_rule_does_not() -> None:
    first, second = _shape_refusals()
    assert first and second and first != second
    assert worker._refusal_shape(first) == worker._refusal_shape(second)
    frozen = broker.stops_refusal(_symbol(freeze_level=30), True, 1.10, 1.09985, 1.104,
                                  current_sl=1.0999)
    assert worker._refusal_shape(frozen) != worker._refusal_shape(first)


def test_a_journal_that_fails_after_the_fill_does_not_hold_the_risk_slot(tmp_path,
                                                                          monkeypatch) -> None:
    account = _demo_account()
    store = _book(tmp_path, account)
    runner = worker.Worker(config.FleetConfig(accounts=[account]), store, dry_run=False)
    _arm_entries(monkeypatch, _symbol())
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=1, server="S"))
    monkeypatch.setattr(worker.broker, "market_order",
                        lambda *a, **k: types.SimpleNamespace(volume=0.01, price=1.10,
                                                              retcode=10009, token="t"))
    monkeypatch.setattr(store, "trade", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    snapshot, day = _snapshot(account)
    with pytest.raises(OSError):
        runner._try_entries(account, snapshot, [], day, 1.0)
    assert store.reserved_volume() == 0.0, "the lot is at the broker; the slot is nobody's debt"


# --- identity normalisation reaches the terminal log too -----------------------------------

def test_a_hand_typed_server_spelling_still_reads_the_login_as_authorized(tmp_path,
                                                                           monkeypatch) -> None:
    monkeypatch.setattr(config, "FLEET_ROOT", tmp_path)
    for spelling in ("Exness-MT5Real26", "exness-mt5real26", "EXNESS-MT5REAL26 "):
        account = config.Account(name="r", login=276261159, server=spelling, mode="real")
        log_dir = account.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "20261103.log").write_text(
            "2026.11.03 00:00:01.000\tLogin\tauthorized on Exness-MT5Real26 (build 4620)\n",
            encoding="utf-8")
        assert terminals.authorized(account), spelling
    wrong = config.Account(name="r2", login=276261159, server="Exness-MT5Real27", mode="real")
    other = wrong.data_dir / "logs"
    other.mkdir(parents=True, exist_ok=True)
    (other / "20261103.log").write_text(
        "2026.11.03 00:00:01.000\tLogin\tauthorized on Exness-MT5Real26 (build 4620)\n",
        encoding="utf-8")
    assert not terminals.authorized(wrong)
