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
import sqlite3
import sys
import time
import types
from datetime import datetime, timezone

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


# --- the two doors: opening is not closing --------------------------------------------------

def test_the_stand_stops_openings_and_leaves_the_closing_door_alone(tmp_path) -> None:
    """The hand on the machine stops new risk; it must not freeze the risk already in the market."""
    registry = _registry(tmp_path)
    stand = tmp_path / "PAUSE"
    stand.write_text("", encoding="utf-8")
    assert "стоянка" in livegate.refusal(276261159, live=True, server="Exness-MT5Real26",
                                        config_path=registry, pause_file=stand)
    # the same stand, the same account: taking our own lot out asks only who the lot belongs to
    assert livegate.exit_refusal(276261159, server="Exness-MT5Real26") == ""
    assert "не назвал свой сервер" in livegate.exit_refusal(276261159), (
        "окно, которое не говорит, где оно сидит, не доказало, что этот билет его")
    assert livegate.identity_refusal(276261159, server="Exness-MT5Real26",
                                     config_path=registry) == ""
    for login in (None, "", "не счёт"):
        assert "не назвал" in livegate.exit_refusal(login, server="Exness-MT5Real26")
    # a row the operator switched off stops new openings and still does not strand an open lot
    switched_off = _registry(tmp_path, _REAL_REGISTRY.replace("enabled: true", "enabled: false"))
    assert livegate.refusal(276261159, live=True, server="Exness-MT5Real26",
                            config_path=switched_off,
                            pause_file=_no_stand(tmp_path)) != ""
    assert livegate.exit_refusal(276261159, server="Exness-MT5Real26") == ""


def test_a_refusal_names_the_first_door_that_fired(tmp_path) -> None:
    """Three words missing and the operator is told the one that decides, not the last to answer."""
    registry = _registry(tmp_path)
    stand = tmp_path / "PAUSE"
    stand.write_text("", encoding="utf-8")
    assert "стоянка" in livegate.refusal(276261159, live=False, server="Exness-MT5Real27",
                                        config_path=registry, pause_file=stand)
    both_wrong = livegate.refusal(276261159, live=False, server="Exness-MT5Real27",
                                  config_path=registry, pause_file=_no_stand(tmp_path))
    assert "--live" in both_wrong, "слово оператора называется раньше, чем с ним спорит реестр"


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

def test_a_bridge_nobody_permitted_reads_the_demo_book_and_leaves_no_opening(
        tmp_path, monkeypatch) -> None:
    """The cycle that may not open still has to know what the demo let go.

    Reading the demo book touches no order — the door that writes the command file is the one the
    refusal closes — and this used to be the idle return's job: the book was never read, so a copy
    already sitting on real money kept riding with nothing under it but the broker's own stop. What
    the refusal does here is take back an OPEN an earlier, permitted cycle left armed.
    """
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    reads: list[int] = []
    monkeypatch.setattr(real_bridge, "demo_book", lambda: (reads.append(1), [])[1])
    ea = tmp_path / "dsh_real_state.json"
    ea.write_text(json.dumps({"login": 276261159, "server": "Exness-MT5Real26", "balance": 100.0,
                              "equity": 100.0, "positions": []}), encoding="utf-8")
    cmd = tmp_path / "dsh_real_cmd.json"
    cmd.write_text(json.dumps({"action": "open", "symbol": "EURUSD"}), encoding="utf-8")
    sentence = real_bridge.run_cycle({}, lambda: "нет слова --live", ea_path=ea, cmd_path=cmd)
    assert reads == [1], "демонская книга не прочитана — закрывать нечем"
    assert "bridge idle" in sentence
    assert not cmd.exists(), "открытое прежде разрешение осталось в папке"


def test_a_parked_bridge_still_closes_the_copy_whose_demo_lot_is_flat(tmp_path,
                                                                     monkeypatch) -> None:
    """The exit door answers to the lot, not to the operator's word about new money."""
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    monkeypatch.setattr(real_bridge, "time", types.SimpleNamespace(sleep=lambda s: None,
                                                                   time=lambda: 0.0))
    monkeypatch.setattr(real_bridge, "demo_book", lambda: [])  # the demo has let this symbol go
    ea = tmp_path / "dsh_real_state.json"
    ea.write_text(json.dumps({"login": 276261159, "server": "Exness-MT5Real26", "balance": 100.0,
                              "equity": 100.0,
                              "positions": [{"ticket": 1, "symbol": "EURUSD",
                                             "magic": real_bridge.REAL_MAGIC}]}), encoding="utf-8")
    cmd = tmp_path / "dsh_real_cmd.json"
    sentence = real_bridge.run_cycle({}, lambda: "нет слова --live", ea_path=ea, cmd_path=cmd)
    assert "bridge idle" in sentence
    payload = json.loads(cmd.read_text(encoding="utf-8"))
    assert payload["action"] == "close" and payload["ticket"] == 1, (
        "защитное закрытие снято вместе с разрешением на открытия")
    assert payload["login"] == 276261159


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
    text = cmd.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["action"] == "close" and payload["ticket"] == 1, "закрыт не тот билет"
    # Два советника читают эту папку: приказ без адреса достанется первому
    # проснувшемуся окну, то есть счёту, который никто не спрашивал.
    assert payload["login"] == 276261159
    assert time.time() < payload["exp"] <= time.time() + real_bridge.CMD_TTL_SEC + 5
    # `JsonStr` ищет `"key":"`; `json.dumps` по умолчанию пишет `"key": "`,
    # и тогда каждое поле невидимо исполнителю: ордер потерян,
    # а журнал продолжает твердить, что он отправлен.
    assert '": ' not in text, "растянутый json советник не читает"
    assert "не наши" in _lines(real_bridge)


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
                                    cmd_path=cmd, login=276261159) is False
    assert not cmd.exists()
    assert real_bridge.send_command({"action": "open", "symbol": "EURUSD"}, lambda: "",
                                    cmd_path=cmd, login=276261159) is True
    text = cmd.read_text(encoding="utf-8")
    assert json.loads(text)["symbol"] == "EURUSD"
    assert json.loads(text)["login"] == 276261159


def test_a_command_without_an_account_in_it_is_not_written_at_all(tmp_path, monkeypatch) -> None:
    """Two EAs read this folder; an order addressed to nobody is an order to whichever woke first."""
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    cmd = tmp_path / "dsh_real_cmd.json"
    for login in (None, "", "не счёт"):
        assert real_bridge.send_command({"action": "open", "symbol": "EURUSD"}, lambda: "",
                                        cmd_path=cmd, login=login) is False
        assert not cmd.exists()
    assert "без адреса" in _lines(real_bridge)


def test_a_parked_bridge_takes_back_its_open_and_leaves_its_close(tmp_path, monkeypatch) -> None:
    """The stand is the operator's hand on NEW money; a close is the way out of money already in.

    Taking a `close` out of the folder is the mistake this test exists to keep: the bridge that
    obeys the stand by disarming its own exits leaves a real lot with no stop and no process left
    that remembers it. An armed OPEN is a different file — that one is a hand he did not lift.
    """
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    cmd = tmp_path / "dsh_real_cmd.json"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    reason = {"text": ""}
    assert real_bridge.send_command({"action": "open", "symbol": "EURUSD",
                                     "stop_pct": real_bridge.REAL_STOP_PCT},
                                    lambda: reason["text"], cmd_path=cmd, login=276261159) is True
    reason["text"] = "стоянка на месте"
    assert real_bridge.send_command({"action": "open", "symbol": "GBPUSD",
                                     "stop_pct": real_bridge.REAL_STOP_PCT},
                                    lambda: reason["text"], cmd_path=cmd, login=276261159) is False
    assert not cmd.exists(), "отзыв разрешения оставил открытие в силе"
    assert real_bridge.send_command({"action": "close", "ticket": 1},
                                    lambda: reason["text"], cmd_path=cmd, login=276261159,
                                    server="Exness-MT5Real26") is True
    assert json.loads(cmd.read_text(encoding="utf-8"))["action"] == "close"
    # And the same refusal asked of a close already sitting in the folder: `withdraw` is what the
    # cycle calls when the door shuts, and it must not reach for the exit file.
    assert real_bridge.withdraw(cmd) is True and cmd.exists()
    assert "не снимает защиту" in _lines(real_bridge)
    cmd.write_text(json.dumps({"action": "open", "symbol": "EURUSD"}), encoding="utf-8")
    assert real_bridge.withdraw(cmd) is True and not cmd.exists()
    # The one question a close still asks is whose account is answering: an unnamed terminal may be
    # holding somebody else's lot, and this door does not open for a guess.
    assert real_bridge.send_command({"action": "close", "ticket": 2},
                                    lambda: reason["text"], cmd_path=cmd, login=None,
                                    server="Exness-MT5Real26") is False
    # The same login on a window that stopped saying where it sits is the same guess.
    assert real_bridge.send_command({"action": "close", "ticket": 3},
                                    lambda: reason["text"], cmd_path=cmd, login=276261159,
                                    server="") is False


def test_a_parked_bridge_still_closes_the_lot_it_opened(tmp_path, monkeypatch) -> None:
    """A stand between cycles used to freeze the exit: the whole cycle returned before the closes."""
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    ea = tmp_path / "dsh_real_state.json"
    ea.write_text(json.dumps({"login": 276261159, "server": "Exness-MT5Real26", "balance": 100.0,
                              "equity": 99.0,
                              "positions": [{"ticket": 1, "symbol": "EURUSD",
                                             "magic": real_bridge.REAL_MAGIC}]}), encoding="utf-8")
    cmd = tmp_path / "dsh_real_cmd.json"
    today = datetime.now(timezone.utc).date().isoformat()
    state = {"day": today, "day_start_equity": 100.0}
    sentence = real_bridge.run_cycle(state, lambda: "стоянка на месте", ea_path=ea, cmd_path=cmd)
    assert "DAILY STOP UNPROVEN" in sentence, sentence
    payload = json.loads(cmd.read_text(encoding="utf-8"))
    assert payload["action"] == "close" and payload["ticket"] == 1
    assert not state.get("halted"), "день объявлен остановленным без доказательства в рынке"
    # Same cycle, now with the witness agreeing that the lot left: the halt may be written.
    ea.write_text(json.dumps({"login": 276261159, "server": "Exness-MT5Real26", "balance": 100.0,
                              "equity": 99.0, "positions": []}), encoding="utf-8")
    sentence = real_bridge.run_cycle(state, lambda: "стоянка на месте", ea_path=ea, cmd_path=cmd)
    assert "DAILY STOP" in sentence and "UNPROVEN" not in sentence, sentence
    assert state["halted"] is True


def test_an_open_the_bridge_writes_carries_a_stop(tmp_path, monkeypatch) -> None:
    """An open whose only exit is this process's next cycle is a lot that survives a crash."""
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    monkeypatch.setattr(real_bridge, "demo_book", lambda: [
        {"symbol": "EURUSD", "type": 0, "profit": 5.0, "ticket": 11}])
    monkeypatch.setattr(real_bridge, "hourly_uptrend", lambda symbol: True)
    monkeypatch.setattr(real_bridge, "livegate", types.SimpleNamespace(
        permitted_account=lambda *a, **k: types.SimpleNamespace(name="r", risk=types.SimpleNamespace(
            max_lots=0.01)),
        capped_lots=lambda row, asked: asked, exit_refusal=livegate.exit_refusal))
    ea = tmp_path / "dsh_real_state.json"
    ea.write_text(json.dumps({"login": 276261159, "server": "Exness-MT5Real26", "balance": 100.0,
                              "equity": 100.0, "positions": []}), encoding="utf-8")
    cmd = tmp_path / "dsh_real_cmd.json"
    real_bridge.run_cycle({"day": "2000-01-01"}, lambda: "", ea_path=ea, cmd_path=cmd)
    payload = json.loads(cmd.read_text(encoding="utf-8"))
    assert payload["action"] == "open" and payload["stop_pct"] == real_bridge.REAL_STOP_PCT > 0


def test_a_bridge_without_a_stop_level_does_not_open(tmp_path, monkeypatch) -> None:
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    monkeypatch.setattr(real_bridge, "demo_book", lambda: [
        {"symbol": "EURUSD", "type": 0, "profit": 5.0, "ticket": 11}])
    monkeypatch.setattr(real_bridge, "hourly_uptrend", lambda symbol: True)
    monkeypatch.setattr(real_bridge, "REAL_STOP_PCT", 0.0)
    ea = tmp_path / "dsh_real_state.json"
    ea.write_text(json.dumps({"login": 276261159, "server": "Exness-MT5Real26", "balance": 100.0,
                              "equity": 100.0, "positions": []}), encoding="utf-8")
    cmd = tmp_path / "dsh_real_cmd.json"
    sentence = real_bridge.run_cycle({"day": datetime.now(timezone.utc).date().isoformat()},
                                 lambda: "", ea_path=ea, cmd_path=cmd)
    assert "NO STOP LEVEL" in sentence and not cmd.exists()


def test_a_cycle_nobody_permitted_takes_back_the_command_of_the_cycle_before_it(tmp_path,
                                                                              monkeypatch) -> None:
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    monkeypatch.setattr(real_bridge, "demo_book", lambda: (_ for _ in ()).throw(
        AssertionError("демо-терминал трогать не должен")))
    cmd = tmp_path / "dsh_real_cmd.json"
    cmd.write_text('{"action":"open","symbol":"EURUSD"}', encoding="utf-8")
    sentence = real_bridge.run_cycle({}, lambda: "нет слова --live",
                                     ea_path=tmp_path / "absent_state.json", cmd_path=cmd)
    assert "bridge idle" in sentence and not cmd.exists()


def test_withdraw_says_out_loud_when_a_command_cannot_be_taken_back(tmp_path,
                                                                    monkeypatch) -> None:
    """Windows will not delete a file the terminal still holds open, and that order is still live."""
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    cmd = tmp_path / "dsh_real_cmd.json"
    assert real_bridge.withdraw(cmd) is True, "пусто — снимать нечего, это не провал"
    cmd.mkdir()  # на этом пути папка: unlink её не сотрёт как файл
    assert real_bridge.withdraw(cmd) is False
    assert "НЕ СНЯЛ" in _lines(real_bridge)


def test_a_bridge_brought_back_without_the_word_starts_and_sends_nothing(
        tmp_path, monkeypatch) -> None:
    """A supervisor or a reboot that comes back without `--live` must not lose the closer.

    `return 1` at startup looked safe and was not: the lots a previously permitted run left on the
    real account are managed only by this process, so refusing to start them meant refusing to close
    them — the worst moment to lose the protective loop is right after the operator touched the
    machine. The refusal is a word about openings; it is said here, and every send asks it again.
    """
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    # The two EA files are bound to this machine's shared folder at import; a test that let the
    # defaults through would withdraw a command from the terminal the park is really using.
    monkeypatch.setattr(real_bridge, "EA_STATE", tmp_path / "dsh_real_state.json")
    monkeypatch.setattr(real_bridge, "EA_CMD", tmp_path / "dsh_real_cmd.json")
    sent: list[tuple] = []
    monkeypatch.setattr(real_bridge, "send_command",
                        lambda *a, **k: sent.append(a) or True)
    cycles: list[str] = []
    monkeypatch.setattr(real_bridge, "run_cycle",
                        lambda state, gate, **kwargs: (cycles.append("cycle"), "ok")[1])

    class _FirstSleepIsTheEndOfTheRun(Exception):
        pass

    def stop_after_the_first_wait(_seconds):
        raise _FirstSleepIsTheEndOfTheRun()

    monkeypatch.setattr(real_bridge.time, "sleep", stop_after_the_first_wait)
    with pytest.raises(_FirstSleepIsTheEndOfTheRun):
        real_bridge.main(["--config", str(_registry(tmp_path))])
    assert cycles == ["cycle"], "мост вышел до первого цикла: закрыватель потерялся"
    assert sent == []
    text = _lines(real_bridge)
    assert "нужен явный флаг --live" in text and "закрыватель работает" in text


# --- the shard inherits the stand and the registry it actually reads ------------------------

def test_a_shard_started_by_hand_watches_the_same_stand_as_the_conductor(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def fake_run_shard(cfg, store, shard_id, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(shard.worker, "run_shard", fake_run_shard)
    monkeypatch.setattr(shard, "CONFIG_PATH", _registry(tmp_path))
    stand = tmp_path / "PAUSE"
    monkeypatch.setattr(shard, "PAUSE_FILE", stand)
    assert shard.main(["--shard", "0", "--once", "--store", str(tmp_path / "b.sqlite")]) == 0
    assert seen["pause_file"] == stand


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


@pytest.mark.parametrize("answered_login,answered_server,allowed", [
    (276261159, "Exness-MT5Real26", True),
    ("276261159", "  exness-mt5real26 ", True),
    (276261159, "Exness-MT5Real27", False),
    (276261159, "", False),
    (276261159, None, False),
    (114929502, "MetaQuotes-Demo", False),
    (None, "Exness-MT5Real26", False),
    ("", "Exness-MT5Real26", False),
])
def test_the_two_implementations_of_the_identity_question_answer_it_once(
        tmp_path, answered_login, answered_server, allowed) -> None:
    """`worker._identity_matches` and `livegate.identity_refusal` are one question in two files.

    The park asks it between the reservation and the send; the scripts ask it before the door. Drift
    between the two is a lot that one of them thinks belongs to somebody else, so every answer a
    terminal can give is put to both and their verdicts are compared here.
    """
    registry = _registry(tmp_path)
    row = livegate.permitted_account(276261159, server="Exness-MT5Real26", config_path=registry)
    assert row is not None, "реестр не пустил собственный тест: проверка стала пустой"
    who = types.SimpleNamespace(login=answered_login, server=answered_server)
    assert worker._identity_matches(who, row) is allowed
    assert (livegate.identity_refusal(answered_login, server=answered_server,
                                      config_path=registry) == "") is allowed


def test_an_unpermitted_real_account_opens_nothing_in_the_park_either(tmp_path,
                                                                     monkeypatch) -> None:
    """The park's `--live` and the scripts' `live=` are the same word in the same consent."""
    registry = _registry(tmp_path)
    row = livegate.permitted_account(276261159, server="Exness-MT5Real26", config_path=registry)
    store = _book(tmp_path, row)
    runner = worker.Worker(config.FleetConfig(accounts=[row]), store, dry_run=False,
                           allow_live=False)
    _arm_entries(monkeypatch, _symbol())
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=row.login, server=row.server))
    sent: list[int] = []
    monkeypatch.setattr(worker.broker, "market_order", lambda *a, **k: sent.append(1))
    snapshot, day = _snapshot(row)
    runner._try_entries(row, snapshot, [], day, 1.0)
    assert sent == [], "лот куплен на настоящие деньги без слова оператора"
    assert store.count_events("entry(gated)") == 1
    assert store.reserved_volume() == 0.0, "закрытая дверью сделка держит слот риска"
    assert "--live" in livegate.refusal(row.login, live=False, server=row.server,
                                       config_path=registry, pause_file=_no_stand(tmp_path))
    assert livegate.refusal(row.login, live=True, server=row.server, config_path=registry,
                            pause_file=_no_stand(tmp_path)) == ""


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


# --- the stand lives in the code that sends, not in the caller that remembered -----------------

def test_a_parked_stand_stops_a_worker_nobody_asked(tmp_path, monkeypatch) -> None:
    """The conductor looks at the stand before it calls here; a hand-built Worker skipped that."""
    account = _demo_account()
    store = _book(tmp_path, account)
    stand = tmp_path / "PAUSE"
    stand.write_text("", encoding="utf-8")
    runner = worker.Worker(config.FleetConfig(accounts=[account]), store,
                           pause_file=stand, dry_run=False)
    _arm_entries(monkeypatch, _symbol())
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=1, server="S"))
    sent: list[int] = []
    monkeypatch.setattr(worker.broker, "market_order",
                        lambda *a, **k: sent.append(1) or types.SimpleNamespace(
                            volume=0.01, price=1.10, retcode=10009, token="t"))
    snapshot, day = _snapshot(account)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert sent == [], "лот куплен на припаркованной площадке"
    assert store.count_events("entries_paused") == 1, "стоянка объявляла себя каждый такт"
    stand.unlink()
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1
    assert store.count_events("entries_resumed") == 1
    assert sent == [1]


def test_the_stands_own_half_of_the_park_is_entries(tmp_path, monkeypatch) -> None:
    """The park splits the same way livegate does: the stand stops buying, never selling.

    An exit frozen behind the stand is a lot giving its profit back with nobody protecting it, and the
    operator who put the stand up expects it to stop the machine from adding risk, not to hold his own
    open trades hostage.
    """
    registry = _registry(tmp_path)
    row = livegate.permitted_account(276261159, server="Exness-MT5Real26", config_path=registry)
    store = _book(tmp_path, row)
    stand = tmp_path / "PAUSE"
    stand.write_text("", encoding="utf-8")
    runner = worker.Worker(config.FleetConfig(accounts=[row]), store, pause_file=stand,
                           dry_run=False, allow_live=True)
    position = types.SimpleNamespace(ticket=7, symbol="EURUSD", type=0, volume=0.01,
                                     price_open=1.10, profit=5.0, sl=1.09, tp=0.0,
                                     magic=row.magic, time=0)
    spec = _symbol()
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: spec)
    monkeypatch.setattr(worker.mt5, "symbol_info_tick",
                        lambda name: types.SimpleNamespace(bid=1.101, ask=1.101))
    monkeypatch.setattr(worker.strategy, "break_even_level", lambda *a, **k: None)
    monkeypatch.setattr(worker.strategy, "exit_reason", lambda *a, **k: "тейк")
    closed: list[int] = []
    monkeypatch.setattr(worker.broker, "close_position",
                        lambda *a, **k: (closed.append(1) or 0.01))
    store.save_positions(row.name, [position])
    day = worker.risk.DayState(day="2026-11-03", start_balance=1000.0)
    assert runner._manage_positions(row, day, [position]) == 1
    assert closed == [1], "стоянка заморозила выход из лота, который парк открыл сам"
    # and the same stand, the same run: nothing new is bought
    _arm_entries(monkeypatch, spec)
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=row.login, server=row.server))
    sent: list[int] = []
    monkeypatch.setattr(worker.broker, "market_order", lambda *a, **k: sent.append(1))
    snapshot, _ = _snapshot(row)
    assert runner._try_entries(row, snapshot, [], day, 1.0) == 0
    assert sent == []


def test_a_cooldown_that_cannot_be_written_is_said_out_loud(tmp_path, monkeypatch) -> None:
    """An entry the journal cannot stamp is a symbol that can be bought twice in a row."""
    account = _demo_account()
    store = _book(tmp_path, account)
    runner = worker.Worker(config.FleetConfig(accounts=[account]), store, dry_run=False)
    _arm_entries(monkeypatch, _symbol())
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=1, server="S"))
    monkeypatch.setattr(worker.broker, "market_order",
                        lambda *a, **k: types.SimpleNamespace(volume=0.01, price=1.10,
                                                              retcode=10009, token="t"))
    monkeypatch.setattr(store, "set_meta",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.Error("database is locked")))
    snapshot, day = _snapshot(account)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1, "сам лот прошёл"
    assert store.count_events("entry_mark_failed") == 1


def test_an_identity_gate_that_cannot_answer_frees_its_reservation(tmp_path,
                                                                    monkeypatch) -> None:
    account = _demo_account()
    store = _book(tmp_path, account)
    runner = worker.Worker(config.FleetConfig(accounts=[account]), store, dry_run=False)
    _arm_entries(monkeypatch, _symbol())
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: (_ for _ in ()).throw(RuntimeError("окно отвечает мимо")))
    sent: list[int] = []
    monkeypatch.setattr(worker.broker, "market_order", lambda *a, **k: sent.append(1))
    snapshot, day = _snapshot(account)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert sent == []
    assert store.reserved_volume() == 0.0, "упавшая проверка держала денежный слот 600 с"
    assert store.count_events("identity_check_failed") == 1


def test_a_filled_lot_forgets_the_refusal_it_just_answered(tmp_path, monkeypatch) -> None:
    """A note that outlives its proof silences the next time the same broker rule bites."""
    account = _demo_account()
    store = _book(tmp_path, account)
    runner = worker.Worker(config.FleetConfig(accounts=[account]), store, dry_run=False)
    monkeypatch.setattr(runner, "_in_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(worker.mt5, "account_info",
                        lambda: types.SimpleNamespace(login=1, server="S"))
    spec = _symbol(stops_level=20)
    _arm_entries(monkeypatch, spec, _signal(stop=1.09985, stop_distance=0.00015))
    key = f"stopsrefused:{account.name}:{spec.name}"
    refusal = broker.stops_refusal(spec, True, 1.10, 1.09985, 1.104)

    def refuse(*a, **k):
        raise broker.StopsRefused(refusal)

    monkeypatch.setattr(worker.broker, "market_order", refuse)
    snapshot, day = _snapshot(account)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert store.count_events("entry_stops_refused") == 1
    assert store.get_meta(key) == worker._refusal_shape(refusal)
    monkeypatch.setattr(worker.broker, "market_order",
                        lambda *a, **k: types.SimpleNamespace(volume=0.01, price=1.10,
                                                              retcode=10009, token="t"))
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 1
    assert store.get_meta(key) == "", "исполнение доказало, что геометрия больше не отказная"
    monkeypatch.setattr(worker.broker, "market_order", refuse)
    assert runner._try_entries(account, snapshot, [], day, 1.0) == 0
    assert store.count_events("entry_stops_refused") == 2, "снятая заметка обязана заговорить"


# --- the shard notices a registry it cannot see with a coarse clock -----------------------------

def test_a_registry_rewritten_inside_the_same_second_still_reloads(tmp_path) -> None:
    registry = _registry(tmp_path)
    first = shard._digest(registry)
    stamp = os.stat(registry).st_mtime
    _registry(tmp_path, _REAL_REGISTRY.replace("max_lots: 0.02", "max_lots: 0.01"))
    os.utime(registry, (stamp, stamp))
    assert os.stat(registry).st_mtime == stamp, "перезапись попала в тот же такт часов"
    assert shard._digest(registry) != first
    assert shard._digest(tmp_path / "net-takogo-failla.yaml") is None


def test_a_shard_that_cannot_read_its_registry_never_trades_and_says_why(tmp_path,
                                                                         monkeypatch) -> None:
    monkeypatch.setattr(shard.worker, "run_shard",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("торговал")))
    registry = _registry(tmp_path, "accounts:\n\t- name: a\n")
    book = Store(tmp_path / "b.sqlite")
    code = shard.main(["--shard", "0", "--once", "--config", str(registry),
                       "--store", str(book.path), "--pause-file", str(_no_stand(tmp_path))])
    assert code == 2
    assert book.count_events("config_rejected") == 1


def test_an_empty_pause_file_means_the_machines_stand(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def fake_run_shard(cfg, store, shard_id, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(shard.worker, "run_shard", fake_run_shard)
    monkeypatch.setattr(shard, "CONFIG_PATH", _registry(tmp_path))
    stand = tmp_path / "PAUSE_MINE"
    monkeypatch.setattr(shard, "PAUSE_FILE", stand)
    assert shard.main(["--shard", "0", "--once", "--pause-file", "",
                       "--store", str(tmp_path / "b.sqlite")]) == 0
    assert seen["pause_file"] == stand, "сбрасывать флаг — не значит сбрасывать стоянку"


def test_a_registry_caught_half_saved_refuses_instead_of_crashing(tmp_path) -> None:
    """A traceback out of the gate is not a refusal: the process dies without saying why it is idle."""
    broken = tmp_path / "accounts.yaml"
    broken.write_text("accounts:\n\t- name: a\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="не разобран как YAML"):
        config.load(broken)


# --- scripts/real_mirror.py: the loop, at its two doors ----------------------------------------

def _mt5_constants():
    """The broker's own numbers, taken now — not whatever a test that ran earlier left behind.

    Reading `sys.modules["MetaTrader5"]` at send time looked reasonable while it was the only copy of
    that dictionary: a test that installs its own terminal and then `del`etes the entry leaves the
    suite with nothing to borrow `TRADE_ACTION_DEAL` from, and the mirror cycle dies as a `cycle
    error` inside somebody else's run. Imported here, at collection, it is the installed module.
    """
    import MetaTrader5
    return types.SimpleNamespace(**{name: value for name, value in vars(MetaTrader5).items()
                                    if name.isupper()})


_MT5 = _mt5_constants()


def _fake_terminal(monkeypatch, *, answers: bool = True, account=None, positions=(),
                   demo=None, real_path: str = "r.exe") -> list[dict]:
    """`main` imports MetaTrader5 inside the function, so sys.modules is the honest seam.

    One object answers for two terminals, so it remembers which executable was last opened: the demo
    laboratory and the real account are read through the same calls in the same cycle. Names this fake
    does not implement — `TRADE_ACTION_DEAL` and the rest — come from `_MT5`, so the test cannot
    invent numbers a broker would not recognise and cannot be poisoned by a neighbour's fake.
    """
    sent: list[dict] = []
    constants = _MT5

    class Fake:
        def __init__(self) -> None:
            self.current = ""

        def __getattr__(self, name):
            return getattr(constants, name)

        def initialize(self, **kwargs):
            self.current = str(kwargs.get("path") or "")
            return answers

        def shutdown(self) -> None:
            return None

        def account_info(self):
            return account

        def positions_get(self, **kwargs):
            return list(positions if self.current == real_path
                        else (positions if demo is None else demo))

        def symbol_info_tick(self, symbol):
            return types.SimpleNamespace(bid=2000.0, ask=2000.5)

        def order_send(self, request):
            sent.append(request)
            return types.SimpleNamespace(retcode=10009)

    monkeypatch.setitem(sys.modules, "MetaTrader5", Fake())
    return sent


def test_the_mirror_retries_a_terminal_that_is_not_there(tmp_path, monkeypatch) -> None:
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    sent = _fake_terminal(monkeypatch, answers=False)
    code = real_mirror.main(["--real-path", "r.exe", "--live", "--once",
                             "--config", str(_registry(tmp_path))])
    assert code == 1
    assert sent == []
    assert "retry" in _lines(real_mirror)


def test_the_mirror_stops_when_the_registry_stops_answering(tmp_path, monkeypatch) -> None:
    """The gate answered a second ago; a registry handing back nothing now is not a licence."""
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    who = types.SimpleNamespace(login=276261159, server="Exness-MT5Real26", trade_mode=2,
                                balance=100.0, equity=100.0, currency="USD")
    sent = _fake_terminal(monkeypatch, account=who)
    monkeypatch.setattr(real_mirror, "demo_equity_trend", lambda *a, **k: 1.0)
    monkeypatch.setattr(real_mirror.livegate, "refusal", lambda *a, **k: "")
    monkeypatch.setattr(real_mirror.livegate, "permitted_account", lambda *a, **k: None)
    code = real_mirror.main(["--real-path", "r.exe", "--live", "--once",
                             "--config", str(_registry(tmp_path))])
    assert code == 1
    assert sent == []
    assert "не отдаёт запись" in _lines(real_mirror)


def test_a_parked_mirror_still_sells_the_copies_it_already_has(tmp_path, monkeypatch) -> None:
    """The stand and `--live` are words about money going IN; our own lots still have to get out.

    This cycle used to `return 1` on the first refusal, which made the kill switch hold a real account
    open — the mirror stopped selling a copy the demo had already let go and a copy that had reached
    its own target. Both exits now go through `exit_refusal`, and only the opening is refused.
    """
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    who = types.SimpleNamespace(login=276261159, server="Exness-MT5Real26", trade_mode=2,
                                balance=100.0, equity=100.0, currency="USD")
    ours = types.SimpleNamespace(ticket=41, symbol="XAUUSD", type=0, volume=0.01,
                                 price_open=2000.0, profit=4.0, sl=0.0, tp=0.0,
                                 magic=real_mirror.REAL_MAGIC)
    stand = tmp_path / "PAUSE"
    stand.write_text("", encoding="utf-8")
    monkeypatch.setattr(real_mirror.livegate, "PAUSE_FILE", stand)
    sent = _fake_terminal(monkeypatch, account=who, positions=[ours], demo=[], real_path="r.exe")
    monkeypatch.setattr(real_mirror, "demo_equity_trend", lambda *a, **k: 1.0)
    code = real_mirror.main(["--real-path", "r.exe", "--once", "--config", str(_registry(tmp_path))])
    assert code == 0, "запрет открытия превращал цикл в аварийный выход"
    comments = [request.get("comment") for request in sent]
    assert "demo closed" in comments, "демо закрыл, а копия осталась в рынке на припаркованной машине"
    assert "real early exit" in comments, "своя копия дошла до цели и не продана"
    assert all(request.get("magic") == real_mirror.REAL_MAGIC for request in sent)
    assert "mirror demo" not in comments, "стоянка, а открытие записано"
    text = _lines(real_mirror)
    assert "открытий нет" in text and "стоянка" in text
    # the same refusal, and the state remembers nothing it never did
    assert json.loads(real_mirror.STATE.read_text(encoding="utf-8")).get("halted") is False


def _growing_demo_ticket(symbol: str, ticket: int, *, sl: float = 1990.0,
                         tp: float = 0.0) -> types.SimpleNamespace:
    """A demo lot the mirror is allowed to look at: the park's own magic, in profit, and rising."""
    return types.SimpleNamespace(ticket=ticket, symbol=symbol, type=0, volume=0.01,
                                 price_open=2000.0, profit=4.0, sl=sl, tp=tp,
                                 magic=8180001)


def _permitted_open_seam(real_mirror, tmp_path, monkeypatch):
    """`--live`, an empty stand and a growing demo: the cycle reaches the opening door."""
    who = types.SimpleNamespace(login=276261159, server="Exness-MT5Real26", trade_mode=2,
                                balance=100.0, equity=100.0, currency="USD")
    monkeypatch.setattr(real_mirror.livegate, "PAUSE_FILE", _no_stand(tmp_path))
    monkeypatch.setattr(real_mirror, "demo_equity_trend", lambda *a, **k: 1.0)
    monkeypatch.setattr(real_mirror, "hourly_uptrend", lambda *a, **k: True)
    return who


def test_one_demo_symbol_is_bought_once_per_cycle(tmp_path, monkeypatch) -> None:
    """Two growing demo tickets of one symbol are one copy, not two.

    The mirroring pass used to live inside the loop over the lots the demo had closed, so one closed
    symbol re-ran it for every other closed symbol in the same cycle and the same growing ticket was
    bought on real money several times at once. The dedup compares against the book as it was at the
    start of the cycle, so the bound is one open per symbol per cycle, and it is this test that says
    so.
    """
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    who = _permitted_open_seam(real_mirror, tmp_path, monkeypatch)
    first = _growing_demo_ticket("XAUUSD", 11)
    second = _growing_demo_ticket("XAUUSD", 12)
    fake = _fake_terminal(monkeypatch, account=who, positions=[],
                          demo=[first, second], real_path="r.exe")
    code = real_mirror.main(["--real-path", "r.exe", "--live", "--once",
                             "--config", str(_registry(tmp_path))])
    assert code == 0
    opens = [request for request in fake if request.get("comment") == "mirror demo"]
    assert len(opens) == 1, f"два билета одного символа купились {len(opens)} раза"
    assert opens[0]["symbol"] == "XAUUSD"
    assert opens[0].get("sl") == 1990.0, "копия ушла в рынок без защитного уровня демо-билета"
    assert "зеркалит" in _lines(real_mirror)


def test_a_lot_without_a_stop_or_a_target_is_not_copied(tmp_path, monkeypatch) -> None:
    """A demo ticket with neither level has no exit but this process's next cycle; real money does not."""
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    who = _permitted_open_seam(real_mirror, tmp_path, monkeypatch)
    naked = _growing_demo_ticket("XAUUSD", 21, sl=0.0, tp=0.0)
    fake = _fake_terminal(monkeypatch, account=who, positions=[],
                          demo=[naked], real_path="r.exe")
    code = real_mirror.main(["--real-path", "r.exe", "--live", "--once",
                             "--config", str(_registry(tmp_path))])
    assert code == 0
    assert [request for request in fake if request.get("comment") == "mirror demo"] == []
    text = _lines(real_mirror)
    assert "ни стопа, ни цели" in text, "отказ на голом билете не назван"
    assert "зеркалит" not in text


def test_the_lot_is_sized_for_the_login_the_terminal_names_at_the_open(tmp_path,
                                                                       monkeypatch) -> None:
    """`real_account` was read at the top of the cycle; a re-login mid-cycle must not buy on its ceiling.

    Two permitted real rows, two different ceilings. The window names the 0.02 account when the cycle
    starts and the 0.01 account by the time the lot is sized. Sizing from the row read at the top buys
    0.02 of a lot against money that is no longer in this window — the same order the other account
    never agreed to. Consent, the row and the ceiling are all asked of the window as it answers now.
    """
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    registry = _registry(tmp_path, _REAL_REGISTRY + """  - name: exness-real-2
    login: 999000
    server: Exness-MT5Real26
    mode: real
    enabled: true
    risk:
      max_lots: 0.01
""")
    wide = types.SimpleNamespace(login=276261159, server="Exness-MT5Real26", trade_mode=2,
                                 balance=100.0, equity=100.0, currency="USD")
    narrow = types.SimpleNamespace(login=999000, server="Exness-MT5Real26", trade_mode=2,
                                   balance=50.0, equity=50.0, currency="USD")
    monkeypatch.setattr(real_mirror.livegate, "PAUSE_FILE", _no_stand(tmp_path))
    monkeypatch.setattr(real_mirror, "demo_equity_trend", lambda *a, **k: 1.0)
    monkeypatch.setattr(real_mirror, "hourly_uptrend", lambda *a, **k: True)
    answers = [wide, wide, narrow]

    sent: list[dict] = []

    class Shifting:
        def __init__(self) -> None:
            self.current = ""

        def __getattr__(self, name):
            return getattr(_MT5, name)

        def initialize(self, **kwargs):
            self.current = str(kwargs.get("path") or "")
            return True

        def shutdown(self) -> None:
            return None

        def account_info(self):
            return answers.pop(0) if answers else narrow

        def positions_get(self, **kwargs):
            return list([] if self.current == "r.exe"
                        else [_growing_demo_ticket("XAUUSD", 31)])

        def symbol_info_tick(self, symbol):
            return types.SimpleNamespace(bid=2000.0, ask=2000.5)

        def order_send(self, request):
            sent.append(request)
            return types.SimpleNamespace(retcode=10009)

    monkeypatch.setitem(sys.modules, "MetaTrader5", Shifting())
    code = real_mirror.main(["--real-path", "r.exe", "--live", "--once", "--real-lots", "0.05",
                             "--config", str(registry)])
    assert code == 0
    opens = [request for request in sent if request.get("comment") == "mirror demo"]
    assert len(opens) == 1, "отказ на смене логина должен быть назван, а не промолчан открытием"
    assert opens[0]["volume"] == 0.01, (f"лот {opens[0]['volume']} посчитан по потолку счёта, "
                                        f"которого в этом окне уже нет")
    assert not any(request.get("volume") == 0.02 for request in sent)


def test_a_window_that_relogged_to_a_stranger_says_so_instead_of_blaming_the_ceiling(
        tmp_path, monkeypatch) -> None:
    """`lots == 0` has two causes, and the operator has to be able to tell them apart at a glance.

    A row that caps the lot is the operator's own ceiling. A window that now answers a login the
    registry does not carry is somebody else's money, and the old sentence printed
    «потолок реестра для exness-real — 0.0 лота»: it named the account this process was started for
    and called its ceiling zero, which is a fact that is not true and sends the operator to edit a
    registry row that was never the problem.
    """
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    wide = types.SimpleNamespace(login=276261159, server="Exness-MT5Real26", trade_mode=2,
                                 balance=100.0, equity=100.0, currency="USD")
    stranger = types.SimpleNamespace(login=999000, server="Exness-Trial10", trade_mode=2,
                                     balance=50.0, equity=50.0, currency="USD")
    monkeypatch.setattr(real_mirror.livegate, "PAUSE_FILE", _no_stand(tmp_path))
    monkeypatch.setattr(real_mirror, "demo_equity_trend", lambda *a, **k: 1.0)
    monkeypatch.setattr(real_mirror, "hourly_uptrend", lambda *a, **k: True)
    answers = [wide, wide, stranger]
    sent: list[dict] = []

    class Shifting:
        def __getattr__(self, name):
            return getattr(_MT5, name)

        def initialize(self, **kwargs):
            self.current = str(kwargs.get("path") or "")
            return True

        def shutdown(self) -> None:
            return None

        def account_info(self):
            return answers.pop(0) if answers else stranger

        def positions_get(self, **kwargs):
            return list([] if self.current == "r.exe"
                        else [_growing_demo_ticket("XAUUSD", 41)])

        def symbol_info_tick(self, symbol):
            return types.SimpleNamespace(bid=2000.0, ask=2000.5)

        def order_send(self, request):
            sent.append(request)
            return types.SimpleNamespace(retcode=10009)

    monkeypatch.setitem(sys.modules, "MetaTrader5", Shifting())
    assert real_mirror.main(["--real-path", "r.exe", "--live", "--once",
                             "--config", str(_registry(tmp_path))]) == 0
    assert [request for request in sent if request.get("comment") == "mirror demo"] == []
    text = _lines(real_mirror)
    assert "окно отвечает 999000/Exness-Trial10" in text, "смена окна не названа причиной отказа"
    assert "записи о котором в реестре нет" in text
    assert "потолок реестра" not in text, "чужие деньги названы потолком своего счёта"
