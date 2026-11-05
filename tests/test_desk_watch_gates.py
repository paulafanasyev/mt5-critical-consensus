"""The fourth door to a real account, and the one that used to ask nobody.

`scripts/desk_watch.py` manages two tickets by hand inside whatever account `C:\\mt5-exness` is logged
into, and that install belongs to a real broker — so its orders are real money. It asks now: the stand
stops every send including play money, a demo account may be managed exactly as the operator asked,
and anything that is not demo needs the same three words as every other door (`--live`, a registry row
calling this login real and enabled, the server the broker answered). The volume a real account may
carry comes from that row, not from this file's own default.

Red-proof measured 2026-11-05 against `HEAD` (1986f8f) with only this file added: all 11 tests fail —
the watcher asked nobody and sent its orders anyway.
"""
from __future__ import annotations

import importlib
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

_REGISTRY = """
accounts:
  - name: exness-real
    login: 276261159
    server: Exness-MT5Real26
    mode: real
    enabled: true
    risk:
      max_lots: 0.01
"""


def _desk(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts"))
    module = importlib.import_module("desk_watch")
    monkeypatch.setattr(module, "LOG", tmp_path / "desk.jsonl")
    monkeypatch.setattr(module, "STATE", tmp_path / "desk_state.json")
    return module


def _registry(tmp_path, body: str = _REGISTRY) -> pathlib.Path:
    path = tmp_path / "accounts.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _no_stand(tmp_path) -> pathlib.Path:
    return tmp_path / "STAND_OFF"


def _account(module, *, real: bool, login=276261159, server="Exness-MT5Real26"):
    return types.SimpleNamespace(
        login=login, server=server,
        trade_mode=(module.mt5.ACCOUNT_TRADE_MODE_REAL if real
                    else module.mt5.ACCOUNT_TRADE_MODE_DEMO))


def _lines(module) -> str:
    if not module.LOG.exists():
        return ""
    return "\n".join(json.loads(line)["event"]
                     for line in module.LOG.read_text(encoding="utf-8").splitlines() if line.strip())


# --- the three words, on both kinds of money ------------------------------------------------

def test_a_real_account_needs_the_same_three_words_as_every_other_door(tmp_path, monkeypatch) -> None:
    desk = _desk(tmp_path, monkeypatch)
    account = _account(desk, real=True)
    registry = _registry(tmp_path)
    assert "--live" in desk.refusal(account, live=False, registry=registry,
                                    pause_file=_no_stand(tmp_path))
    assert "не разрешён реестром" in desk.refusal(account, live=True, registry=None,
                                                 pause_file=_no_stand(tmp_path))
    assert desk.refusal(account, live=True, registry=registry,
                        pause_file=_no_stand(tmp_path)) == ""


def test_the_stand_stops_the_desk_watcher_even_on_play_money(tmp_path, monkeypatch) -> None:
    """The operator's hand is on the machine, not on one process: demo is stopped by it too."""
    desk = _desk(tmp_path, monkeypatch)
    account = _account(desk, real=False)
    assert desk.refusal(account, live=False, registry=None,
                        pause_file=_no_stand(tmp_path)) == ""
    stand = tmp_path / "PAUSE"
    stand.write_text("", encoding="utf-8")
    assert "стоянка" in desk.refusal(account, live=False, registry=None, pause_file=stand)


@pytest.mark.parametrize("login,server", [(None, ""), ("", ""), ("не счёт", "Exness-MT5Real26")])
def test_a_terminal_that_did_not_name_its_login_is_managed_by_nobody(tmp_path, monkeypatch,
                                                                    login, server) -> None:
    desk = _desk(tmp_path, monkeypatch)
    account = _account(desk, real=True, login=login, server=server)
    assert "не назвал номер" in desk.refusal(account, live=True, registry=_registry(tmp_path),
                                             pause_file=_no_stand(tmp_path))


def test_another_server_under_the_same_login_is_a_different_money(tmp_path, monkeypatch) -> None:
    desk = _desk(tmp_path, monkeypatch)
    account = _account(desk, real=True, server="Exness-MT5Real27")
    assert "не разрешён реестром" in desk.refusal(account, live=True, registry=_registry(tmp_path),
                                                 pause_file=_no_stand(tmp_path))


# --- the volume ---------------------------------------------------------------------------

def test_a_real_account_carries_the_ceiling_of_its_row_not_the_default_of_this_file(
        tmp_path, monkeypatch) -> None:
    """A 0.05 ask onto an account the operator capped at 0.01 is not a copy of the demo."""
    desk = _desk(tmp_path, monkeypatch)
    registry = _registry(tmp_path)
    assert desk.desk_lots(_account(desk, real=True), registry=registry, asked=0.05) == 0.01
    assert desk.desk_lots(_account(desk, real=True), registry=None) == 0.0
    # play money is the laboratory: no row caps it, and no row needs to
    assert desk.desk_lots(_account(desk, real=False), registry=None) == desk.DESK_LOTS
    assert desk.desk_lots(None, registry=registry) == 0.0


# --- the send ---------------------------------------------------------------------------

def test_the_desk_watcher_writes_no_order_the_gate_refuses(tmp_path, monkeypatch) -> None:
    desk = _desk(tmp_path, monkeypatch)
    sent: list[dict] = []
    monkeypatch.setattr(desk, "mt5", types.SimpleNamespace(
        order_send=lambda request: (sent.append(request) or types.SimpleNamespace(retcode=10009))))
    reason = {"text": "стоянка на месте"}
    assert desk.send({"comment": "desk: peak exit"}, lambda: reason["text"]) is None
    assert sent == []
    assert "NOT SENT" in _lines(desk)
    reason["text"] = ""
    assert desk.send({"comment": "desk: peak exit"}, lambda: reason["text"]) is not None
    assert len(sent) == 1


def _relogin_registry(tmp_path) -> pathlib.Path:
    """A row whose ceiling is below this file's own default, so a stale size is visible."""
    return _registry(tmp_path, _REGISTRY.replace(
        "      max_lots: 0.01", "      min_lots: 0.001\n      max_lots: 0.005"))


def _position():
    """A silver short that reached 6 dollars and has given the whole move back."""
    return types.SimpleNamespace(ticket=11206724238, symbol="XAGUSD", type=1, profit=0.0,
                                 volume=0.02, magic=8189999)


def _rates(n: int) -> list:
    return [{"close": 30.0, "high": 30.2, "low": 29.6} for _ in range(n)]


class _Terminal:
    """The real module's constants, this test's answers.

    `desk_watch` reads `mt5.ACCOUNT_TRADE_MODE_DEMO` and the order enums off whatever object is in
    its namespace, so the fake inherits them from the installed module instead of inventing numbers a
    broker would not recognise.
    """

    def __init__(self, mt5_module) -> None:
        self._constants = mt5_module
        self.sends: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._constants, name)

    def initialize(self, *a, **k):
        return True

    def shutdown(self) -> None:
        pass

    def last_error(self):
        return (0, "stub")

    def positions_get(self, **k):
        return [_position()]

    def symbol_info_tick(self, symbol):
        return types.SimpleNamespace(bid=29.9, ask=30.0)

    def symbol_info(self, symbol):
        return types.SimpleNamespace(trade_stops_level=0, point=0.00001)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        return _rates(count)

    def orders_get(self):
        return []

    def order_send(self, request):
        self.sends.append(request)
        return types.SimpleNamespace(retcode=10009)


def test_a_relogin_mid_run_sizes_the_next_lot_from_the_new_account(tmp_path, monkeypatch) -> None:
    """The volume is read at the moment of the send, because the gate already is.

    A run that started on the demo terminal passed the gate on demo and then sent `DESK_LOTS`, the one
    number in this file that `capped_lots` exists to refuse, after the window was re-logged into a real
    account capped below it. The gate re-reads the terminal every cycle; the size has to as well.
    """
    desk = _desk(tmp_path, monkeypatch)
    from fleet import livegate
    monkeypatch.setattr(livegate, "PAUSE_FILE", tmp_path / "STAND_NOT_ARMED", raising=False)
    terminal = _Terminal(desk.mt5)
    demo = types.SimpleNamespace(login=114929502, server="MetaQuotes-Demo",
                                 trade_mode=desk.mt5.ACCOUNT_TRADE_MODE_DEMO)
    real = types.SimpleNamespace(login=276261159, server="Exness-MT5Real26",
                                 trade_mode=desk.mt5.ACCOUNT_TRADE_MODE_REAL)
    counted: list[int] = []

    def account_info():
        # the first two questions are the ones asked before the loop; somebody then re-logs the
        # window into the real account and every question after that gets a different money
        counted.append(1)
        return demo if len(counted) <= 2 else real

    terminal.account_info = account_info
    monkeypatch.setattr(desk, "mt5", terminal)
    monkeypatch.setattr(desk, "TERMINAL", str(tmp_path / "terminal64.exe"))
    # the peak the watcher is holding a lot for, from the run before this one
    desk.STATE.write_text(json.dumps({"peaks": {"11206724238": 6.0}, "bought": []}),
                          encoding="utf-8")
    assert desk.main(["--live", "--config", str(_relogin_registry(tmp_path)), "--once"]) == 0
    pending = [s for s in terminal.sends
               if s.get("action") == desk.mt5.TRADE_ACTION_PENDING]
    assert len(pending) == 1, "после перелогина отложенный ордер не отправлен вообще"
    assert pending[0]["volume"] == 0.005, (f"деск купил {pending[0]['volume']} лота на счёте "
                                           "с потолком 0.005 — размер снят со старого аккаунта")
    closed = [s for s in terminal.sends if s.get("action") == desk.mt5.TRADE_ACTION_DEAL]
    assert [c["volume"] for c in closed] == [0.02], "свой лот закрыт не своим объёмом"


def test_a_stand_put_up_mid_cycle_stops_the_buy_and_not_the_exit(tmp_path, monkeypatch) -> None:
    """The operator's hand stops new risk; it must not freeze the risk already in the market."""
    desk = _desk(tmp_path, monkeypatch)
    from fleet import livegate
    stand = tmp_path / "PAUSE"
    monkeypatch.setattr(livegate, "PAUSE_FILE", stand, raising=False)
    terminal = _Terminal(desk.mt5)
    demo = types.SimpleNamespace(login=114929502, server="MetaQuotes-Demo",
                                 trade_mode=desk.mt5.ACCOUNT_TRADE_MODE_DEMO)
    terminal.account_info = lambda: demo

    def park_after_the_first_look(**k):
        if not stand.exists():
            stand.write_text("стоп", encoding="utf-8")
        return [_position()]

    terminal.positions_get = park_after_the_first_look
    monkeypatch.setattr(desk, "mt5", terminal)
    monkeypatch.setattr(desk, "TERMINAL", str(tmp_path / "terminal64.exe"))
    desk.STATE.write_text(json.dumps({"peaks": {"11206724238": 6.0}, "bought": []}),
                          encoding="utf-8")
    assert desk.main(["--config", str(_registry(tmp_path)), "--once"]) == 0
    actions = [(s.get("action"), s.get("comment")) for s in terminal.sends]
    assert actions == [(desk.mt5.TRADE_ACTION_DEAL, "desk: peak exit")], (
            f"стоянка изменила порядок выходов: {actions}")
    text = _lines(desk)
    assert "NOT SENT desk: buy the low" in text, "стоянка не названа в журнале"
    assert "peak" in text and "закрыт на вершине" in text


def test_a_desk_run_nobody_permitted_opens_nothing_but_still_reads_its_own_lots(
        tmp_path, monkeypatch) -> None:
    """Two doors, and only one of them is shut by the missing word.

    `--live` licenses new money. It does not license the desk to leave a lot it already put on a
    real account without a manager: the book has to be read to find that lot, and a run that exited
    with code 2 here would have been a run that walked away from open risk.
    """
    desk = _desk(tmp_path, monkeypatch)
    account = _account(desk, real=True)
    sent: list[dict] = []
    touched: list[str] = []

    class FakeTerminal:
        ACCOUNT_TRADE_MODE_REAL = 2
        ACCOUNT_TRADE_MODE_DEMO = 0

        def initialize(self, *a, **k):
            return True

        def account_info(self):
            return account

        def positions_get(self, **k):
            touched.append("positions")
            return []

        def order_send(self, request):
            sent.append(request)

        def shutdown(self):
            pass

        def last_error(self):
            return (0, "stub")

    monkeypatch.setattr(desk, "mt5", FakeTerminal())
    monkeypatch.setattr(desk, "TERMINAL", str(tmp_path / "terminal64.exe"))
    assert desk.main(["--config", str(_registry(tmp_path)), "--once"]) == 0
    assert sent == [], "открытия без слова оператора всё-таки отправлены"
    assert touched, "книга не прочитана — значит, и свои лоты никто не сторожит"
    text = _lines(desk)
    assert "нужен явный флаг --live" in text and "свои лоты продолжаю закрывать" in text
