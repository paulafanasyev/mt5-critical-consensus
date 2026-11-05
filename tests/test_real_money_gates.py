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


_DEMO_AND_REAL = """
accounts:
  - name: demo-metaquotes
    login: 114929502
    server: MetaQuotes-Demo
    mode: demo
    enabled: true
    magic: 7000001
    risk:
      max_lots: 0.10
  - name: exness-real
    login: 276261159
    server: Exness-MT5Real26
    mode: real
    enabled: true
    magic: 7000002
    risk:
      max_lots: 0.02
"""


def test_the_mirrors_demo_side_is_the_parks_magic_not_a_number_from_the_old_lab(
        tmp_path, monkeypatch) -> None:
    """Measured 2026-11-05: three park lots including a gold ticket in profit read as a flat demo.

    The park stamps the magic of its own registry row on its trades; the mirror only knew the lab EA
    that came before it, so it stood in front of the growing book and saw nothing to copy.
    """
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    registry = _registry(tmp_path, _DEMO_AND_REAL)
    assert real_mirror.park_magics(registry) == {7000001}
    book = [types.SimpleNamespace(ticket=1, magic=7000001, symbol="XAUUSD", profit=16.55),
            types.SimpleNamespace(ticket=2, magic=8184184, symbol="USDJPY", profit=2.24),
            types.SimpleNamespace(ticket=3, magic=0, symbol="EURUSD", profit=5.0),
            types.SimpleNamespace(ticket=4, magic=7000002, symbol="EURUSD", profit=5.0)]
    seen = [p.ticket for p in book if real_mirror.is_demo_lot(p, real_mirror.park_magics(registry))]
    assert seen == [1, 2]  # the park's gold lot and the old lab lot; the manual and real-row ones not


def test_a_registry_that_answers_nothing_leaves_the_old_lab_range_alone(tmp_path,
                                                                        monkeypatch) -> None:
    """A broken registry must not turn unknown magics into trades worth copying onto real money."""
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    broken = tmp_path / "accounts.yaml"
    broken.write_text("accounts: [{name: x, login: 1}]\n", encoding="utf-8")
    assert real_mirror.park_magics(broken) == set()
    assert real_mirror.is_demo_lot(types.SimpleNamespace(magic=999999), set()) is False
    assert real_mirror.is_demo_lot(types.SimpleNamespace(magic=8184184), set()) is True


def _conductor_book(tmp_path: pathlib.Path, equity: float, start,
                    *, age: float = 0.0) -> pathlib.Path:
    """The conductor's day measurement as it stands on disk, with its own age."""
    path = tmp_path / "conductor.json"
    path.write_text(json.dumps({"book": {"equity": equity}, "day_start_equity": start}),
                    encoding="utf-8")
    now = time.time()
    os.utime(path, (now - age, now - age))
    return path


def test_growth_is_the_days_measured_equity_not_the_last_process_that_died(tmp_path,
                                                                           monkeypatch) -> None:
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    path = _conductor_book(tmp_path, 10_010.0, 10_000.0)
    assert real_mirror.demo_growth(path) == (10.0, "")
    losing = _conductor_book(tmp_path, 9_990.0, 10_000.0)
    delta, why = real_mirror.demo_growth(losing)
    assert delta == -10.0 and why == ""


def test_a_stale_or_unopened_conductor_book_is_no_growth_at_all(tmp_path, monkeypatch) -> None:
    """The mirror bought on a heartbeat log whose writer had been dead for a day and a half.

    An absent measurement is not zero growth and it is not yesterday's trend either: it is a reason
    to buy nothing, and the reason has to be readable.
    """
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    stale = _conductor_book(tmp_path, 10_010.0, 10_000.0, age=real_mirror.BOOK_STALE_SEC + 600)
    delta, why = real_mirror.demo_growth(stale)
    assert delta == 0.0 and "не писал книгу" in why
    unopened = _conductor_book(tmp_path, 10_010.0, None)
    delta, why = real_mirror.demo_growth(unopened)
    assert delta == 0.0 and "день не открыт" in why
    delta, why = real_mirror.demo_growth(tmp_path / "absent-conductor.json")
    assert delta == 0.0 and "книги дирижёра нет" in why


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


def test_a_real_book_that_never_answered_is_not_the_same_as_a_flat_one(tmp_path,
                                                                       monkeypatch) -> None:
    """`positions_get()` has two failures, and the bridge used to hear them as one sentence.

    The advisor writes `"positions":[...]` on every pass, the flat one included. A state file without
    that key is a torn write or a build from before the field — nobody answered — and read as `[]` it
    tells the daily stop that everything of ours left the market (the day gets marked halted and no
    later cycle ever retries the exit) and tells the mirroring pass that nothing is held yet, so the
    next cycle buys a second real lot on a symbol that already carries one. `None` is the answer this
    file does not have, and here it is refused in both places.
    """
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    monkeypatch.setattr(real_bridge, "time", types.SimpleNamespace(sleep=lambda s: None,
                                                                   time=lambda: 0.0))
    ea = tmp_path / "dsh_real_state.json"
    cmd = tmp_path / "dsh_real_cmd.json"
    answered = {"login": 276261159, "server": "Exness-MT5Real26", "balance": 100.0, "equity": 99.0,
                "positions": [{"ticket": 5, "symbol": "EURUSD", "magic": real_bridge.REAL_MAGIC}]}
    torn = {key: value for key, value in answered.items() if key != "positions"}
    today = datetime.now(timezone.utc).date().isoformat()

    # The daily stop ordered the close, then asked the witness whether the lot is still in the market.
    ea.write_text(json.dumps(answered), encoding="utf-8")
    reads: list[int] = []

    def second_read(path):
        # The advisor dies between the cycle's first read and the one that confirms the exits.
        reads.append(1)
        return answered if len(reads) == 1 else torn

    monkeypatch.setattr(real_bridge, "read_ea_state", second_read)
    state = {"day": today, "day_start_equity": 100.0}
    sentence = real_bridge.run_cycle(state, lambda: "стоянка на месте", ea_path=ea, cmd_path=cmd)
    assert "UNPROVEN" in sentence and "книга реальных лотов не получена" in sentence, sentence
    assert not state.get("halted"), "день объявлен остановленным по файлу без ответа"
    assert reads, "свидетель не опрошен"

    # The same file on a normal cycle: openings wait for the answer, and say so.
    cmd.unlink()  # the close leg 1 armed stays in the folder: a stand is not a reason to disarm an exit
    monkeypatch.setattr(real_bridge, "read_ea_state", lambda path: dict(torn, equity=100.0))
    monkeypatch.setattr(real_bridge, "demo_book", lambda: [
        {"symbol": "EURUSD", "type": 0, "profit": 5.0, "ticket": 11}])
    monkeypatch.setattr(real_bridge, "hourly_uptrend", lambda symbol: True)
    monkeypatch.setattr(real_bridge, "livegate", types.SimpleNamespace(
        permitted_account=lambda *a, **k: types.SimpleNamespace(
            name="r", risk=types.SimpleNamespace(max_lots=0.01)),
        capped_lots=lambda row, asked: asked, exit_refusal=livegate.exit_refusal))
    sentence = real_bridge.run_cycle({"day": today, "day_start_equity": 100.0}, lambda: "",
                                     ea_path=ea, cmd_path=cmd)
    assert "NO BOOK" in sentence, sentence
    assert not cmd.exists(), "лот куплен на реальном счёте, когда книга не ответила"
    assert "не зная, что уже держится" in _lines(real_bridge)


def test_a_halted_day_that_lost_the_book_says_the_lots_may_still_be_there(
        tmp_path, monkeypatch) -> None:
    """"Halted for the day" is a report about the market, and a torn file cannot issue it.

    After a confirmed halt the bridge keeps trying to get its own lots out. When the advisor stops
    publishing the list, the honest sentence is "a lot of ours may still be in the market"; the old
    one said the day was halted and nothing more, which is how a stopped process reads as a finished
    one to the operator looking at the log.
    """
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    monkeypatch.setattr(real_bridge, "time", types.SimpleNamespace(sleep=lambda s: None,
                                                                   time=lambda: 0.0))
    ea = tmp_path / "dsh_real_state.json"
    ea.write_text(json.dumps({"login": 276261159, "server": "Exness-MT5Real26", "balance": 100.0,
                              "equity": 99.0}), encoding="utf-8")
    cmd = tmp_path / "dsh_real_cmd.json"
    halted = {"day": datetime.now(timezone.utc).date().isoformat(), "day_start_equity": 100.0,
              "halted": True}
    sentence = real_bridge.run_cycle(halted, lambda: "стоянка на месте", ea_path=ea, cmd_path=cmd)
    assert "не прислал список лотов" in sentence, sentence
    assert sentence != "halted for the day"


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


def test_a_halted_day_ends_the_openings_and_not_the_closings(tmp_path, monkeypatch) -> None:
    """A lot that outlived its own daily stop has to keep meeting a bridge that tries to free it.

    The halt is the end of the day for NEW money. Read as a global stand-down it becomes the worst
    outcome the stop can produce: the close the loop ordered is not confirmed by the next cycle (the
    broker was busy, the EA was not awake), the day is marked halted, and every later cycle returns
    early while the lot rides on a real account with nobody left to take it out.
    """
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    monkeypatch.setattr(real_bridge, "time", types.SimpleNamespace(sleep=lambda s: None,
                                                                   time=lambda: 0.0))
    ea = tmp_path / "dsh_real_state.json"

    def state_file(tickets: list[dict]) -> None:
        ea.write_text(json.dumps({"login": 276261159, "server": "Exness-MT5Real26",
                                  "balance": 100.0, "equity": 99.0, "positions": tickets}),
                      encoding="utf-8")

    state_file([{"ticket": 5, "symbol": "EURUSD", "magic": real_bridge.REAL_MAGIC}])
    cmd = tmp_path / "dsh_real_cmd.json"
    halted = {"day": datetime.now(timezone.utc).date().isoformat(), "day_start_equity": 100.0,
              "halted": True}
    sentence = real_bridge.run_cycle(halted, lambda: "стоянка на месте", ea_path=ea, cmd_path=cmd,
                                     registry=_registry(tmp_path))
    assert "повторяю закрытие" in sentence, sentence
    payload = json.loads(cmd.read_text(encoding="utf-8"))
    assert payload["action"] == "close" and payload["ticket"] == 5

    # The same state, once the witness agrees the lot is out: nothing left to write.
    cmd.unlink()
    state_file([])
    assert real_bridge.run_cycle(halted, lambda: "стоянка на месте", ea_path=ea,
                                 cmd_path=cmd, registry=_registry(tmp_path)) == "halted for the day"
    assert not cmd.exists()


def test_two_closes_written_without_a_gap_are_one_close_for_the_advisor(tmp_path,
                                                                        monkeypatch) -> None:
    """The EA polls this folder once a second, so the bridge's own pace is part of the order.

    Two lots of ours leave the market in the same cycle. Written back to back, the second file
    overwrites the first before the advisor has read either, and a loop that believes it closed both
    has armed one and lost the other — with the real money of both still in. The wait between writes
    is the only thing that makes one command file carry two commands.
    """
    real_bridge = _script("real_bridge", tmp_path, monkeypatch)
    events: list[tuple] = []
    origin = real_bridge.send_command

    def watch(command: dict, gate, *args, **kwargs) -> bool:
        sent = origin(command, gate, *args, **kwargs)
        events.append(("emit", command["action"], command.get("ticket")))
        return sent

    def fake_sleep(seconds: float) -> None:
        events.append(("wait", seconds))

    monkeypatch.setattr(real_bridge, "send_command", watch)
    monkeypatch.setattr(real_bridge, "time", types.SimpleNamespace(sleep=fake_sleep,
                                                                   time=lambda: 0.0))
    monkeypatch.setattr(real_bridge, "demo_book", lambda: [])  # the demo has let both go
    ea = tmp_path / "dsh_real_state.json"
    ea.write_text(json.dumps({"login": 276261159, "server": "Exness-MT5Real26", "balance": 100.0,
                              "equity": 100.0,
                              "positions": [{"ticket": 1, "symbol": "EURUSD",
                                             "magic": real_bridge.REAL_MAGIC},
                                            {"ticket": 2, "symbol": "GBPUSD",
                                             "magic": real_bridge.REAL_MAGIC}]}),
                  encoding="utf-8")
    cmd = tmp_path / "dsh_real_cmd.json"
    today = datetime.now(timezone.utc).date().isoformat()
    real_bridge.run_cycle({"day": today, "day_start_equity": 100.0}, lambda: "", ea_path=ea,
                          cmd_path=cmd, registry=_registry(tmp_path))
    assert [kind for kind, *_ in events] == ["emit", "wait", "emit", "wait"], events
    assert [item[2] for item in events if item[0] == "emit"] == [1, 2]
    waits = [item[1] for item in events if item[0] == "wait"]
    assert all(w >= 1 for w in waits), f"приказы пишутся вразнобой, советник увидит один: {waits}"


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


def test_a_shard_that_lost_its_registry_blocks_entries_and_keeps_closing(tmp_path, monkeypatch) -> None:
    """`run --shards 0` passes no `--config`, so the default path is the file that must be watched.

    A registry this process can no longer read is a reason not to open: the accounts, caps and modes
    its numbers promised are gone, and an opening is a statement made in those numbers. It is not a
    reason to stop the cycle, and that used to be exactly what happened — the shard `continue`d, so
    the lots the last good registry had let into the market met nobody. This is `8aa8db1`'s freeze
    moved one file up, and the shard still says the loss once per broken version.
    """
    registry = _registry(tmp_path)
    monkeypatch.setattr(shard, "CONFIG_PATH", registry)
    seen: list[dict] = []

    def fake_run_shard(cfg, store, shard_id, **kwargs):
        seen.append(kwargs)
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
    assert len(seen) == 3, "осколочный цикл замёрз вместе с реестром"
    assert [row["allow_entries"] for row in seen] == [True, False, False], seen
    assert book.count_events("config_rejected") == 1, "сломанный реестр нужно сказать один раз"


# --- the worker's own gates -----------------------------------------------------------------


def test_a_real_lot_is_still_cared_for_when_the_live_word_is_missing(tmp_path,
                                                                     monkeypatch) -> None:
    """`--live` is the operator's word about money going IN; a lot already in the market still has to
    be moved to break-even and closed. The same word used to freeze the closing half of the worker on
    a real account: the address of the money was proved above, the lot was ours, and the only exit it
    had left was the next cycle of a process that no longer ran. Closing is gated by `dry_run` —
    paper stands down — and by nothing else.
    """
    account = _demo_account(name="real", login=5, server="S", mode="real")
    store = _book(tmp_path, account)
    runner = worker.Worker(config.FleetConfig(accounts=[account]), store, dry_run=False,
                           allow_live=False)
    touched: list[str] = []
    _arm_management(monkeypatch, touched)
    day = worker.risk.DayState(day="2026-11-03", start_balance=1000.0)
    closed = runner._manage_positions(account, day, [_own_position(account, 91)])
    assert touched == ["modify", "close"], \
        f"закрыватель стоял из-за отсутствующего --live: {touched}"
    assert closed == 1


def test_a_dry_run_cycle_writes_nothing_to_the_broker(tmp_path, monkeypatch) -> None:
    """The one mode that stands down from a close is the one that has no broker behind it."""
    account = _demo_account()
    store = _book(tmp_path, account)
    runner = worker.Worker(config.FleetConfig(accounts=[account]), store, dry_run=True)
    touched: list[str] = []
    _arm_management(monkeypatch, touched)
    day = worker.risk.DayState(day="2026-11-03", start_balance=1000.0)
    assert runner._manage_positions(account, day, [_own_position(account, 92)]) == 0
    assert touched == [], "бумажный цикл тронул брокера"
    assert store.count_events("exit(dry run)") == 1


def test_a_paper_cycle_never_connects_to_a_real_account(tmp_path, monkeypatch) -> None:
    """`--dry-run` says nothing of the operator's is touched; a real login is where that breaks.

    A paper cycle on a live account is the worst of both: reaching it means logging into the broker
    that holds his money, and the cycle itself then places no protective level and closes nothing, so
    the journal can print a lot on a real login that was never guarded. The refusal belongs in front
    of `attach` — before a window is started — and it is said once, not once per thirty seconds.
    """
    real = _demo_account(name="real", login=5, server="S", mode="real")
    store = _book(tmp_path, real)
    runner = worker.Worker(config.FleetConfig(accounts=[real]), store, dry_run=True)
    monkeypatch.setattr(runner, "prepare_cycle", lambda: None)
    monkeypatch.setattr(runner, "attach", lambda account: (_ for _ in ()).throw(
        AssertionError("бумажный цикл подключился к счёту с настоящими деньгами")))
    result = runner.run_account(real, allow_entries=True)
    assert result.status == "error" and "dry-run" in result.detail
    assert store.count_events("dry_run_refused") == 1
    runner.run_account(real, allow_entries=True)
    assert store.count_events("dry_run_refused") == 1, "отказ пишет по строке за цикл"

    demo = _demo_account(name="paper", login=6, server="S")
    book = _book(tmp_path, demo)
    paper = worker.Worker(config.FleetConfig(accounts=[demo]), book, dry_run=True)
    monkeypatch.setattr(paper, "prepare_cycle", lambda: None)
    attached: list[str] = []
    monkeypatch.setattr(paper, "attach", lambda account: attached.append(account.name) or "")
    monkeypatch.setattr(paper, "_cycle", lambda account, allow_entries: (
        attached.append("cycle"), worker.WorkerResult(account.name, "ok", "бумага"))[1])
    paper.detach = lambda: None
    assert paper.run_account(demo).status == "ok"
    assert attached == ["paper", "cycle"], "бумажный цикл перестал играть на демо"


@pytest.mark.parametrize("mode,currency,equity,balance,why", [
    ("demo", "USD", 0.0, 1000.0, "нулевой баланс"),
    ("real", "USD", 1000.0, 0.0, "реальный счёт без денег"),
    ("demo", "RUR", 1000.0, 1000.0, "нет курса"),
])
def test_a_lot_is_still_closed_on_an_account_whose_money_cannot_be_read(
        tmp_path, monkeypatch, mode: str, currency: str, equity: float, balance: float,
        why: str) -> None:
    """Three sentences about the account's money, and none of them is a reason to stop closing.

    A lot survives all three readings: an account at zero equity, a real account whose balance the
    broker has already eaten, a demo priced in a currency the book cannot quote. Each of those numbers
    is a condition for the day's accounting and for new openings — and each used to `return` in front
    of the management call, which left the park's own lot in the market with no trailing rule, no
    break-even and no close while the process that opened it was still alive and still attached.
    """
    account = _demo_account(name=mode, login=5, server="S", mode=mode)
    store = _book(tmp_path, account)
    runner = worker.Worker(config.FleetConfig(accounts=[account]), store, dry_run=False,
                           allow_live=mode == "demo")
    touched: list[str] = []
    _arm_management(monkeypatch, touched)
    broke = worker.risk.AccountSnapshot(login=account.login, server=account.server,
                                        currency=currency, balance=balance, equity=equity,
                                        margin_free=balance, leverage=100, positions=1,
                                        floating=0.0, server_day="2026-11-03")
    monkeypatch.setattr(worker.risk.AccountSnapshot, "read", staticmethod(lambda day: broke))
    monkeypatch.setattr(worker.risk, "server_day_now", lambda: "2026-11-03")
    monkeypatch.setattr(worker.risk, "positions_for",
                        lambda acc: [_own_position(account, 93)])
    monkeypatch.setattr(worker.broker, "resolve", lambda base: None)  # ни RURUSD, ни USDURR
    result = runner._cycle(account, True)
    assert touched == ["modify", "close"], f"выход стоял за чтением «{why}»: {touched}"
    assert result.status == "flat" and why in result.detail, result.detail
    assert result.closed == 1, "закрытый лот не дошёл до оператора в статусе цикла"


def test_a_shard_told_to_stop_opening_still_runs_the_account(tmp_path, monkeypatch) -> None:
    """`allow_entries=False` is the entry door and nothing else, so it has to reach the cycle.

    The shard has one reason the operator did not choose for closing that door — a registry it can no
    longer read — and the half-door is only honest if the account's own cycle still runs: the exits,
    the break-even and the journal are the parts that do not need the file.
    """
    account = _demo_account()
    store = _book(tmp_path, account)
    seen: list[tuple] = []

    def spy(self, acc, allow_entries=True):
        seen.append((acc.name, allow_entries))
        return worker.WorkerResult(acc.name, "ok", "проверено")

    monkeypatch.setattr(worker.Worker, "run_account", spy)
    results = worker.run_shard(config.FleetConfig(accounts=[account]), store, 0,
                               pause_file=_no_stand(tmp_path), allow_entries=False)
    assert seen == [(account.name, False)], seen
    assert [r.status for r in results] == ["ok"], "полудверь съела весь цикл"


def _arm_management(monkeypatch, touched: list[str]) -> None:
    """Every write the park can make on a lot it owns, recorded under one name.

    `market_order` is armed to raise rather than to record: the closing half of a cycle never opens a
    lot, and an assertion that watches a list nobody appends to would pass whether it did or not.
    """
    monkeypatch.setattr(worker.broker, "symbol_exact", lambda name: _symbol())
    monkeypatch.setattr(worker.broker, "atr", lambda *a, **k: 0.001)
    monkeypatch.setattr(worker.broker, "modify_position", lambda *a, **k: touched.append("modify"))

    def fake_close(position, spec, **kwargs):
        touched.append("close")
        return float(position.volume)  # a full close, not a partial one

    monkeypatch.setattr(worker.broker, "close_position", fake_close)
    monkeypatch.setattr(worker.broker, "market_order", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("закрыватель открыл лот")))
    monkeypatch.setattr(worker.strategy, "break_even_level", lambda *a, **k: 1.1000)
    monkeypatch.setattr(worker.strategy, "exit_reason", lambda *a, **k: "тейк пробило")
    monkeypatch.setattr(worker.mt5, "symbol_info_tick",
                        lambda name: types.SimpleNamespace(bid=1.1010, ask=1.1010, time=1))


def _own_position(account: config.Account, ticket: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(ticket=ticket, symbol="EURUSD", type=0, volume=0.01,
                                 price_open=1.1000, profit=1.0, sl=1.0990, tp=0.0,
                                 magic=account.magic, time=0)


def test_a_gated_entry_is_not_counted_as_a_lot_opened(tmp_path, monkeypatch) -> None:
    """Two halves of `not sending`, and only one of them belongs in `opened`.

    `opened` is the number the conductor aggregates, the shard prints and the operator reads on
    screen. A paper cycle did see the signal it would have traded, so counting it is honest; a real
    account the operator has not spoken for has nothing in the market, and reporting one lot opened
    every thirty seconds was the status line lying about the book.
    """
    real = _demo_account(name="real", login=5, server="S", mode="real")
    store = _book(tmp_path, real)
    runner = worker.Worker(config.FleetConfig(accounts=[real]), store, dry_run=False,
                           allow_live=False)
    _arm_entries(monkeypatch, _symbol())
    monkeypatch.setattr(worker.broker, "market_order", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("открытие на настоящие деньги без --live")))
    snapshot, day = _snapshot(real)
    assert runner._try_entries(real, snapshot, [], day, 1.0) == 0
    assert store.count_events("entry(gated)") == 1

    paper = _demo_account(name="paper", login=6, server="S")
    book = _book(tmp_path, paper)
    paper_store = book
    cycling = worker.Worker(config.FleetConfig(accounts=[paper]), paper_store, dry_run=True)
    snapshot, day = _snapshot(paper)
    assert cycling._try_entries(paper, snapshot, [], day, 1.0) == 1
    assert paper_store.count_events("entry(dry)") == 1


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
    monkeypatch.setattr(real_mirror, "demo_growth", lambda *a, **k: (1.0, ""))
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
    monkeypatch.setattr(real_mirror, "demo_growth", lambda *a, **k: (1.0, ""))
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


def _blind_terminal(monkeypatch, *, which: str, account, positions=(),
                    demo=()) -> list[dict]:
    """One terminal's `positions_get()` answers None: a dropped channel, not a flat account.

    `which` names the side that lost the channel ("demo" or "real"); the other answers like
    `_fake_terminal` does, so the cycle reads a real book from a laboratory that said nothing.
    """
    sent: list[dict] = []

    class Blind:
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
            return account

        def positions_get(self, **kwargs):
            on_real_side = self.current == "r.exe"
            if (which == "real") == on_real_side:
                return None
            return list(positions if on_real_side else demo)

        def symbol_info_tick(self, symbol):
            return types.SimpleNamespace(bid=2000.0, ask=2000.5)

        def order_send(self, request):
            sent.append(request)
            return types.SimpleNamespace(retcode=10009)

    monkeypatch.setitem(sys.modules, "MetaTrader5", Blind())
    return sent


def test_a_demo_that_stopped_answering_sells_nothing_on_real_money(tmp_path,
                                                                   monkeypatch) -> None:
    """An empty demo book means the laboratory let the symbol go, and the mirror sells the copy.

    That sentence is true when the demo answers with an empty list and false when it does not answer
    at all — `positions_get()` returns None on a dropped IPC channel, which this machine has measured.
    Collapsing the two sold a real lot because a terminal forgot to reply.
    """
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    who = types.SimpleNamespace(login=276261159, server="Exness-MT5Real26", trade_mode=2,
                                balance=100.0, equity=100.0, currency="USD")
    ours = types.SimpleNamespace(ticket=41, symbol="XAUUSD", type=0, volume=0.01,
                                 price_open=2000.0, profit=4.0, sl=0.0, tp=0.0,
                                 magic=real_mirror.REAL_MAGIC)
    sent = _blind_terminal(monkeypatch, which="demo", account=who, positions=[ours])
    monkeypatch.setattr(real_mirror.livegate, "PAUSE_FILE", _no_stand(tmp_path))
    monkeypatch.setattr(real_mirror, "demo_growth", lambda *a, **k: (1.0, ""))
    code = real_mirror.main(["--real-path", "r.exe", "--live", "--once",
                             "--config", str(_registry(tmp_path))])
    assert code == 1
    assert sent == [], "реальный лот продан потому, что демо не ответил"
    assert "демо не ответил" in _lines(real_mirror)


def test_a_real_side_that_stopped_answering_buys_nothing_on_real_money(tmp_path,
                                                                       monkeypatch) -> None:
    """The mirror's dedup reads the real book, so None read as `[]` says "nothing is mirrored yet".

    Every growing demo leg then looks unmirrored, and the cycle buys a second real lot on a symbol
    that already carries one — twice over, since the next cycle cannot see the lot it just placed
    either. The same None also removes the early exits, which are enumerated off that book.
    """
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    who = types.SimpleNamespace(login=276261159, server="Exness-MT5Real26", trade_mode=2,
                                balance=100.0, equity=100.0, currency="USD")
    ours = types.SimpleNamespace(ticket=41, symbol="XAUUSD", type=0, volume=0.01,
                                 price_open=2000.0, profit=1.0, sl=0.0, tp=0.0,
                                 magic=real_mirror.REAL_MAGIC)
    sent = _blind_terminal(monkeypatch, which="real", account=who,
                           demo=[_growing_demo_ticket("XAUUSD", 51)])
    monkeypatch.setattr(real_mirror.livegate, "PAUSE_FILE", _no_stand(tmp_path))
    monkeypatch.setattr(real_mirror, "demo_growth", lambda *a, **k: (1.0, ""))
    monkeypatch.setattr(real_mirror, "hourly_uptrend", lambda *a, **k: True)
    code = real_mirror.main(["--real-path", "r.exe", "--live", "--once",
                             "--config", str(_registry(tmp_path))])
    assert code == 1
    assert sent == [], "копия куплена, когда реальный счёт не сказал, что уже держится"
    assert "реальный счёт не ответил" in _lines(real_mirror)


def _growing_demo_ticket(symbol: str, ticket: int, *, sl: float = 1990.0,
                         tp: float = 0.0, magic: int = 8180001) -> types.SimpleNamespace:
    """A demo lot the mirror is allowed to look at: the park's own magic, in profit, and rising."""
    return types.SimpleNamespace(ticket=ticket, symbol=symbol, type=0, volume=0.01,
                                 price_open=2000.0, profit=4.0, sl=sl, tp=tp,
                                 magic=magic)


def _permitted_open_seam(real_mirror, tmp_path, monkeypatch):
    """`--live`, an empty stand and a growing demo: the cycle reaches the opening door."""
    who = types.SimpleNamespace(login=276261159, server="Exness-MT5Real26", trade_mode=2,
                                balance=100.0, equity=100.0, currency="USD")
    monkeypatch.setattr(real_mirror.livegate, "PAUSE_FILE", _no_stand(tmp_path))
    monkeypatch.setattr(real_mirror, "demo_growth", lambda *a, **k: (1.0, ""))
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


def test_the_parks_own_lot_is_the_one_a_copy_is_bought_of(tmp_path, monkeypatch) -> None:
    """End to end, with nothing patched but the terminal: a magic-7000001 lot reaches the real door.

    Measured 2026-11-05 on the live demo book: the gold ticket 11252852400 was +16,55 $ and the mirror
    reported nothing to copy, because the only demo magic it knew was the old lab's `>= 8180000`.
    """
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    who = _permitted_open_seam(real_mirror, tmp_path, monkeypatch)
    park_lot = _growing_demo_ticket("XAUUSD", 11252852400, magic=7000001)
    fake = _fake_terminal(monkeypatch, account=who, positions=[],
                          demo=[park_lot], real_path="r.exe")
    code = real_mirror.main(["--real-path", "r.exe", "--live", "--once",
                             "--config", str(_registry(tmp_path, _DEMO_AND_REAL))])
    assert code == 0
    opens = [request for request in fake if request.get("comment") == "mirror demo"]
    assert len(opens) == 1, "лот парка не дошёл до двери открытия"
    assert opens[0]["volume"] <= 0.02


def test_a_growth_that_was_never_measured_is_said_and_buys_nothing(tmp_path,
                                                                   monkeypatch) -> None:
    """The other half of the mandate: `only on the growth`. No measurement is not a small growth."""
    real_mirror = _script("real_mirror", tmp_path, monkeypatch)
    who = _permitted_open_seam(real_mirror, tmp_path, monkeypatch)
    monkeypatch.setattr(real_mirror, "demo_growth",
                        lambda *a, **k: (0.0, "книги дирижёра нет на диске: роста не измерено"))
    park_lot = _growing_demo_ticket("XAUUSD", 11252852400, magic=7000001)
    fake = _fake_terminal(monkeypatch, account=who, positions=[],
                          demo=[park_lot], real_path="r.exe")
    code = real_mirror.main(["--real-path", "r.exe", "--live", "--once",
                             "--config", str(_registry(tmp_path, _DEMO_AND_REAL))])
    assert code == 0
    assert [request for request in fake if request.get("comment") == "mirror demo"] == []
    assert "копий не добавляю: книги дирижёра нет" in _lines(real_mirror)


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
    monkeypatch.setattr(real_mirror, "demo_growth", lambda *a, **k: (1.0, ""))
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
    monkeypatch.setattr(real_mirror, "demo_growth", lambda *a, **k: (1.0, ""))
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
