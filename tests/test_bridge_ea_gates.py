r"""The two file-channel bridges, read as the source of truth about what the terminal will obey.

Nothing in this suite can execute MQL5: MetaQuotes' compiler decides what a command means, and the
terminal decides whether to trade it. So these tests read the two `.mq5` files and pin the checks the
operator relies on — that the executor reads the fields Python writes, that a command which names
another account or outlived its permission is not traded, that a lot the operator never stamped is
not closed, and that the demo bridge does not share the real bridge's queue. A shared queue meant the
demo script ate the real bridge's command (`FileDelete`) and opened it as play money while the real
account waited for an order that had already been consumed.

The red-proof was measured on 2026-11-05 against `HEAD` (1986f8f) with only this file added: 12 of the
16 tests fail there. The other 4 passed before this round too — they hold facts the previous tree
already got right, and they stay so that a later edit cannot quietly give them up.
"""
from __future__ import annotations

import importlib
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

MQL5 = ROOT / "mql5"
REAL_EA = (MQL5 / "DSHRealBridge.mq5").read_text(encoding="utf-8")
DEMO_EA = (MQL5 / "DSHBridge.mq5").read_text(encoding="utf-8")
real_bridge = importlib.import_module("real_bridge")


def _default(source: str, name: str) -> str:
    match = re.search(r"input\s+\w+\s+" + name + r"\s*=\s*([^;]+);", source)
    assert match, name
    return match.group(1).strip().strip('"')


def _body(source: str, function: str) -> str:
    """The text of one function, to the next top-level definition, with whitespace collapsed."""
    start = source.index(function)
    rest = source[start + len(function):]
    ends = [rest.index(marker) for marker in ("\nvoid ", "\nbool ", "\nint ", "\nstring ",
                                              "\ndouble ", "\n//+---") if marker in rest]
    return re.sub(r"\s+", "", rest[:min(ends)] if ends else rest)


# --- the queue itself ---------------------------------------------------------------------

def test_the_real_bridge_reads_the_queue_python_writes() -> None:
    """A field name the executor cannot see is a silent order, so both sides are pinned here."""
    assert _default(REAL_EA, "CmdFile") == real_bridge.EA_CMD.name
    assert _default(REAL_EA, "StateFile") == real_bridge.EA_STATE.name


def test_the_demo_bridge_does_not_share_the_real_bridge_queue() -> None:
    """Two readers of one command file: whoever wakes first spends the other one's order."""
    assert _default(DEMO_EA, "CmdFile") != _default(REAL_EA, "CmdFile")
    assert _default(DEMO_EA, "StateFile") != _default(REAL_EA, "StateFile")
    assert _default(DEMO_EA, "CmdFile") != real_bridge.EA_CMD.name


@pytest.mark.parametrize("source,name", [(REAL_EA, "DSHRealBridge"), (DEMO_EA, "DSHBridge")])
def test_both_bridges_open_the_folder_python_actually_writes_to(source, name) -> None:
    """Without FILE_COMMON the advisor reads its own sandbox and never the file Python wrote.

    That failure is silent in the worst way: the log on the Python side still says the command was
    sent, and no order ever reaches the market.
    """
    assert source.count("FILE_COMMON") >= 3, name
    assert "WebRequest(" not in source, name


# --- the checks the executor makes on its own, because Python's promise is not a proof ------

@pytest.mark.parametrize("snippet,why", [
    ("AccountInfoInteger(ACCOUNT_LOGIN)",
     "команда без адреса уйдёт в первое проснувшееся окно"),
    ('JsonNum(raw,"login")', "советник обязан читать адрес, который пишет python"),
    ('JsonNum(raw,"exp")', "и срок, который пишет python"),
    ('JsonStr(raw,"action")', "поле, которое исполнитель не видит, — молча потерянный ордер"),
])
def test_the_real_bridge_refuses_a_command_it_cannot_verify(snippet, why) -> None:
    assert snippet in _body(REAL_EA, "void ProcessCommand"), why


def test_the_real_bridge_will_not_execute_a_command_that_outlived_its_permission() -> None:
    """Two clocks, because a file that survives the hand that wrote it is an order nobody signed."""
    body = _body(REAL_EA, "void ProcessCommand")
    assert "TimeGMT()" in body, "советник смотрит свои часы, а не обещание скрипта"
    assert "exp<=now_epoch" in body, "просроченный приказ не исполняется"
    assert "exp>now_epoch+MaxCommandTtlSec" in body, "срок не может быть бесконечным"


def test_the_real_bridge_caps_the_lot_of_an_ordered_open() -> None:
    """Python caps the volume by the registry row; the executor caps it again by its own input."""
    assert float(_default(REAL_EA, "MaxLots")) <= real_bridge.REAL_LOTS
    assert "lots>MaxLots" in _body(REAL_EA, "void ProcessCommand")


def test_the_real_bridge_refuses_to_open_a_lot_it_cannot_stop() -> None:
    """A real lot whose only exit is a Python loop survives exactly as long as that loop.

    The bridge used to accept an open with no `sl`, no `tp` and no percentage: if the script died after
    writing the command, the terminal held the position with nothing attached to it on a live account.
    The executor now asks for a protective level of its own before it sends.
    """
    body = _body(REAL_EA, "void ProcessCommand")
    assert 'JsonNum(raw,"stop_pct")' in body, "советник не читает процент, который пишет python"
    assert "if(sl<=0&&tp<=0&&stop_pct<=0)" in body, "открытие без защитного уровня не заблокировано"
    assert "BLOCKED:открытиебеззащитногоуровня" in body, "отказ должен быть виден в журнале советника"


def test_the_real_bridge_resolves_its_percentage_stop_against_the_price_it_picked() -> None:
    """Python cannot know the fill price; the level only becomes real inside the terminal.

    The side matters twice over: `side == 1` is the sell, and a sell is stopped above the market, so a
    percentage applied the other way would be a stop the broker fills instantly.
    """
    body = _body(REAL_EA, "bool OpenTrade")
    assert "stop=(side==1)?price*(1.0+stop_pct/100.0):price*(1.0-stop_pct/100.0)" in body
    assert "request.sl=NormalizeDouble(stop,digits)" in body, (
        "уровень без нормализации по digits — это сделка с invalid stops у большинства брокеров")
    assert "if(stop<=0&&stop_pct>0)" in body, "процент не должен перетирать явный stop-loss команды"


def test_the_stop_the_python_sends_is_a_real_distance_not_a_placeholder() -> None:
    """Both sides of the same number: an EA that would obey 0 % is an EA that obeys nothing."""
    assert 0.0 < real_bridge.REAL_STOP_PCT <= 1.0, real_bridge.REAL_STOP_PCT


def test_the_real_bridge_closes_only_the_lots_it_stamped() -> None:
    """Closing an operator's own lot spends his money under the name of an automated exit."""
    body = _body(REAL_EA, "bool ClosePosition")
    assert "PositionGetInteger(POSITION_MAGIC)!=InpMagic" in body


def test_the_real_bridge_stamps_its_own_magic_on_every_request() -> None:
    """A hardcoded number in one leg and an input in the other means the legs disagree silently."""
    requests = re.findall(r"request\.magic\s*=\s*([^;]+);", REAL_EA)
    assert len(requests) >= 2
    for request in requests:
        assert request.strip() == "InpMagic"


def test_the_magic_the_ea_stamps_is_the_magic_python_reads() -> None:
    """The one number that decides whether a lot is ours: both sides must say it, unchanged."""
    assert int(_default(REAL_EA, "InpMagic")) == real_bridge.REAL_MAGIC
    assert int(_default(DEMO_EA, "InpMagic")) == real_bridge.REAL_MAGIC
