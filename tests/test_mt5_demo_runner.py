"""The demo runner is the only component that can cause an order; its gates are tested as such."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from critical_consensus.mt5_agent import (AccountInfo, DecisionJournal, SymbolSpec, codes)
from critical_consensus.mt5_agent.i18n import text
from test_mt5_agent import make_policy

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_mt5_demo_trading as runner  # noqa: E402
from mt5_demo_bridge import BridgeError  # noqa: E402
from operator_answers import ApprovalState  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
SPEC = SymbolSpec(symbol="EURUSD", point=0.00001, digits=5, tick_size=0.00001, tick_value=1.0,
                  volume_min=0.01, volume_max=100.0, volume_step=0.01, spread_points=10,
                  stops_level_points=0)
TOKEN = "t" * 40
POLICY_YAML = """\
version: 1
mode: paper
symbol: EURUSD
timeframe: H1
history_bars: 120
server_utc_offset_seconds: 0
require_demo: true
kill_switch: {active: false, reason: runner test}
schedule: {blocked_weekdays_utc: [], window_start_minute_utc: 0, window_end_minute_utc: 1440}
symbol_spec: {symbol: EURUSD, point: 0.00001, digits: 5, tick_size: 0.00001,
  tick_value: 1.0, volume_min: 0.01, volume_max: 100.0, volume_step: 0.01,
  spread_points: 10, stops_level_points: 0}
account: {require_demo: true}
"""


# ------------------------------------------------------------------------ fixtures
class Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=float(seconds))

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


class FakeQueue:
    base_url = "http://127.0.0.1:8123"

    def __init__(self, *, behaviour: str = "serve", clock: Clock | None = None) -> None:
        self.behaviour = behaviour
        self.clock = clock or Clock()
        self.commands: list[dict] = []
        self._served: dict[str, datetime] = {}
        self._expired: dict[str, datetime] = {}

    def enqueue(self, command):
        if self.behaviour == "duplicate":
            raise BridgeError("DUPLICATE_COMMAND")
        if self.behaviour == "busy":
            raise BridgeError("QUEUE_BUSY")
        self.commands.append(dict(command))
        command_id = str(command["command_id"])
        if self.behaviour == "serve":
            self._served[command_id] = self.clock()
        elif self.behaviour == "expire":
            self._expired[command_id] = self.clock()
        return dict(command)

    def served_at(self, command_id):
        return self._served.get(command_id)

    def expired_at(self, command_id):
        return self._expired.get(command_id)


class FakeTerminal:
    def __init__(self, *, ask: float = 1.10010, bid: float = 1.10000,
                 equity: float = 10_000.0, margin_free: float = 10_000.0, leverage: int = 100,
                 deals=(), deals_error: Exception | None = None, spec: SymbolSpec = SPEC,
                 specs: dict[str, SymbolSpec] | None = None,
                 unavailable: set[str] | None = None,
                 trade_mode: str = "demo", server: str = "MetaQuotes-Demo",
                 read_error: Exception | None = None) -> None:
        self.ask, self.bid = ask, bid
        self.equity, self.margin_free, self.leverage = equity, margin_free, leverage
        self.deals, self.deals_error, self.spec = list(deals), deals_error, spec
        self.specs, self.unavailable = dict(specs or {}), set(unavailable or ())
        self.trade_mode, self.server, self.read_error = trade_mode, server, read_error

    def account(self) -> AccountInfo:
        if self.read_error is not None:
            raise self.read_error
        return AccountInfo(login=42, server=self.server, currency="USD",
                           balance=self.equity, equity=self.equity,
                           margin_free=self.margin_free, leverage=self.leverage,
                           trade_mode=self.trade_mode)

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        if symbol in self.unavailable:
            raise RuntimeError(f"symbol {symbol} is not available in the terminal")
        return self.specs.get(symbol, self.spec)

    def tick(self, symbol: str):
        return SimpleNamespace(ask=self.ask, bid=self.bid, symbol=symbol)

    def entry_deals(self, since: datetime):
        if self.deals_error is not None:
            raise self.deals_error
        return [dict(deal) for deal in self.deals]


def proposal(*, direction: str = "BUY", entry: float = 1.10000, sl: float = 1.09000,
             tp: float = 1.12000, symbol: str = "EURUSD") -> dict:
    return {"symbol": symbol, "timeframe": "H1", "direction": direction,
            "evaluated_at": "2026-10-30T12:00:00+00:00", "entry_price": entry,
            "stop_loss": sl, "take_profit": tp, "risk_distance": abs(entry - sl),
            "reward_distance": abs(tp - entry), "risk_reward": 2.0, "atr": 0.001,
            "confidence": 0.5, "reasons": ["test"]}


def sizing(volume: float = 0.05, equity: float = 10_000.0) -> dict:
    return {"volume": volume, "risk_value": volume * 100.0, "risk_fraction": 0.0005,
            "risk_per_lot": 100.0, "equity": equity}


def entry_deal(*, order_id: str, magic: int = runner.MAGIC_NUMBER, symbol: str = "EURUSD",
               entry: int = 0, ticket: int = 1001, price: float = 1.10010,
               volume: float = 0.05) -> dict:
    return {"magic": magic, "comment": runner.COMMENT_PREFIX + order_id[:20], "entry": entry,
            "symbol": symbol, "ticket": ticket, "position_id": ticket, "price": price,
            "volume": volume, "profit": 0.0, "swap": 0.0, "commission": 0.0, "fee": 0.0,
            "time_epoch": NOW.timestamp(), "time": NOW.isoformat()}


@pytest.fixture
def journal(tmp_path):
    decision_log = DecisionJournal(tmp_path / "journal.db", run_id="run-1")
    yield decision_log
    decision_log.close()


def make_executor(*, journal: DecisionJournal, queue: FakeQueue | None = None,
                  terminal: FakeTerminal | None = None, policy=None,
                  approver=None, state: ApprovalState | None = None,
                  clock: Clock | None = None, fill_timeout: int = 5,
                  max_volume: float = runner.MAX_DEMO_VOLUME, out=None,
                  language: str = "ru", ttl: int = 60,
                  symbol: str | None = None) -> tuple[runner.DemoExecutor,
                                                      FakeQueue, FakeTerminal, Clock]:
    clock = clock or Clock()
    queue = queue if queue is not None else FakeQueue(clock=clock)
    terminal = terminal or FakeTerminal()
    executor = runner.DemoExecutor(
        queue=queue, journal=journal, terminal=terminal,
        policy=policy or make_policy(execution_mode="auto"), language=language,
        run_id="run-1", symbol=symbol, ttl_seconds=ttl, fill_timeout_seconds=fill_timeout,
        poll_seconds=1.0, max_volume=max_volume, approver=approver,
        operator_state=state, clock=clock, sleep=clock.sleep, out=out)
    return executor, queue, terminal, clock


def payloads(journal: DecisionJournal, event_type: str) -> list[dict]:
    return [event["payload"] for event in journal.events(limit=None, event_type=event_type)]


# ------------------------------------------------------------------------ identities
def test_build_command_matches_the_schema_exactly():
    schema = json.loads((ROOT / "schemas" / "order_command.schema.json").read_text("utf-8"))
    command = runner.build_command(
        command_id="c" * 32, client_order_id="cc-" + "a" * 28,
        permission_id="perm-" + "b" * 20, signal_id="sig-" + "d" * 24,
        instrument="EURUSD", direction="BUY", size=0.05, expected_price=1.1001,
        sl=1.09, tp=1.12, user_decision_id="dec-" + "e" * 24, now=NOW, ttl_seconds=60)
    assert set(command) == set(schema["required"])
    assert command["mode"] == "demo" and command["user_decision"] == "APPROVE"
    assert command["user_approved"] is True
    assert command["expires_at_epoch"] - int(NOW.timestamp()) == 60
    assert datetime.fromisoformat(command["created_at"]) == NOW
    assert len(command["payload_hash"]) == 64
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(command, schema)


def test_payload_hash_covers_every_field_but_itself():
    base = dict(command_id="c" * 32, client_order_id="cc-" + "a" * 28,
                permission_id="perm-" + "b" * 20, signal_id="sig-" + "d" * 24,
                instrument="EURUSD", direction="BUY", size=0.05, expected_price=1.1001,
                sl=1.09, tp=1.12, user_decision_id="dec-" + "e" * 24, now=NOW, ttl_seconds=60)
    first = runner.build_command(**base)
    changed = runner.build_command(**{**base, "size": 0.06})
    assert first["payload_hash"] != changed["payload_hash"]


def test_identifiers_are_deterministic_bounded_and_volume_sensitive():
    one = proposal()
    assert runner.signal_id_for(one) == runner.signal_id_for(dict(one))
    assert runner.client_order_id_for("run-1", one, 0.05) == \
        runner.client_order_id_for("run-1", one, 0.05)
    assert runner.client_order_id_for("run-1", one, 0.05) != \
        runner.client_order_id_for("run-1", one, 0.06)
    for value in (runner.signal_id_for(one), runner.client_order_id_for("run-1", one, 0.05),
                  runner.permission_id_for("run-1", 1),
                  runner.decision_id_for("abc", "APPROVE")):
        assert 1 <= len(value) <= 128
        assert not any(ch in value for ch in "\x00\r\n ")


# ----------------------------------------------------------------------------- gates
def test_price_gates_accept_a_quote_that_still_fits_the_proposal():
    live, reasons = runner.price_gates(proposal(), SimpleNamespace(ask=1.10010, bid=1.10000),
                                       SPEC)
    assert live == 1.10010 and reasons == []


def test_price_gates_refuse_a_quote_that_moved_past_the_slippage_limit():
    _, reasons = runner.price_gates(proposal(), SimpleNamespace(ask=1.10300, bid=1.10290), SPEC)
    assert codes.PRICE_MOVED_DURING_CONFIRMATION in reasons


def test_price_gates_enforce_buy_and_sell_geometry_around_the_live_price():
    _, buy = runner.price_gates(proposal(sl=1.11000, tp=1.12000),
                                SimpleNamespace(ask=1.10010, bid=1.10000), SPEC)
    assert codes.BUY_GEOMETRY_INVALID in buy
    _, sell = runner.price_gates(proposal(direction="SELL", sl=1.09000, tp=1.08000),
                                 SimpleNamespace(ask=1.10010, bid=1.10000), SPEC)
    assert codes.SELL_GEOMETRY_INVALID in sell


def test_price_gates_respect_the_broker_stops_level_on_both_sides():
    tight = replace(SPEC, stops_level_points=2_000)
    _, reasons = runner.price_gates(proposal(), SimpleNamespace(ask=1.10010, bid=1.10000), tight)
    assert codes.STOP_INSIDE_BROKER_STOPS_LEVEL in reasons
    assert codes.TARGET_INSIDE_BROKER_STOPS_LEVEL in reasons


def test_sell_expected_price_is_the_bid():
    live, reasons = runner.price_gates(proposal(direction="SELL", entry=1.10000, sl=1.11000,
                                                tp=1.08000),
                                       SimpleNamespace(ask=1.10010, bid=1.09990), SPEC)
    assert reasons == [] and live == 1.09990


@pytest.mark.parametrize("volume, expected", [
    (0.005, {"VOLUME_BELOW_BROKER_MIN", "VOLUME_NOT_ON_STEP"}),
    (0.25, {"VOLUME_ABOVE_DEMO_CAP"}),
    (0.055, {"VOLUME_NOT_ON_STEP"}),
    (0.0, {"VOLUME_INVALID"}),
    (-1.0, {"VOLUME_INVALID"}),
    (float("nan"), {"VOLUME_INVALID"}),
])
def test_volume_gates_name_every_violation(volume, expected):
    assert set(runner.volume_gates(volume, SPEC)) == expected


def test_volume_gates_refuse_a_volume_above_the_broker_maximum():
    small = replace(SPEC, volume_max=0.10, volume_step=0.01, volume_min=0.01)
    reasons = runner.volume_gates(0.15, small, max_volume=0.10)
    assert "VOLUME_ABOVE_BROKER_MAX" in reasons and "VOLUME_ABOVE_DEMO_CAP" in reasons


# ---------------------------------------------------------------------- reconciliation
def test_match_entry_deal_requires_magic_comment_symbol_and_an_entry_deal():
    record = entry_deal(order_id="ord-1", ticket=77)
    deals = [entry_deal(order_id="ord-1", entry=1),
             entry_deal(order_id="ord-2"),
             entry_deal(order_id="ord-1", magic=1),
             entry_deal(order_id="ord-1", symbol="GBPUSD"),
             entry_deal(order_id="ord-1", ticket=78)]
    found = runner.match_entry_deal(deals, magic=runner.MAGIC_NUMBER, order_id="ord-1",
                                    symbol="EURUSD")
    assert found is not None and found["ticket"] == 78
    assert runner.match_entry_deal(deals, magic=runner.MAGIC_NUMBER, order_id="ord-1",
                                   symbol="GBPUSD")["ticket"] == 1001


def test_match_entry_deal_tolerates_broker_rows_with_missing_fields():
    assert runner.match_entry_deal([{"comment": None}], magic=runner.MAGIC_NUMBER,
                                   order_id="ord-1") is None
    assert runner.match_entry_deal([{"magic": "x", "entry": "y"}], magic=runner.MAGIC_NUMBER,
                                   order_id="ord-1") is None


def test_unresolved_served_reports_only_ids_inside_the_window_without_a_deal():
    records = [
        {"client_order_id": "ord-filled", "at": (NOW - timedelta(hours=1)).isoformat()},
        {"client_order_id": "ord-missing", "at": (NOW - timedelta(hours=2)).isoformat()},
        {"client_order_id": "ord-old", "at": (NOW - timedelta(hours=48)).isoformat()},
        {"client_order_id": "ord-bad-at", "at": "yesterday"},
        {"at": NOW.isoformat()},
    ]
    deals = [entry_deal(order_id="ord-filled")]
    unresolved = runner.unresolved_served(records, deals, magic=runner.MAGIC_NUMBER,
                                          since=NOW - timedelta(hours=24))
    assert unresolved == ["ord-missing"]


# ------------------------------------------------------------------------- executor
def test_a_fill_is_journaled_as_sent_only_after_the_broker_deal_is_seen(journal):
    order_id = runner.client_order_id_for("run-1", proposal(), 0.05)
    terminal = FakeTerminal(deals=[entry_deal(order_id=order_id)])
    executor, queue, _, _ = make_executor(journal=journal, terminal=terminal)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_FILLED and outcome.queued
    assert outcome.deal is not None and outcome.deal["ticket"] == 1001
    assert outcome.stop_run is False and outcome.requires_reconciliation is False
    sent = [payload["broker_order_sent"] for payload in payloads(journal, runner.OUTCOME_EVENT)]
    assert sent == [True]
    commands = payloads(journal, runner.COMMAND_EVENT)
    assert commands[0]["broker_order_sent"] is False and commands[0]["stage"] == "queued"
    assert commands[0]["decision"] == "AUTO"
    assert payloads(journal, runner.SERVED_EVENT)[0]["broker_order_sent"] is None
    assert len(queue.commands) == 1


def test_the_command_carries_the_live_price_the_approved_sizing_and_the_ttl(journal):
    executor, queue, _, _ = make_executor(journal=journal, terminal=FakeTerminal(deals=[]),
                                          ttl=45)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_UNCONFIRMED
    command = queue.commands[0]
    assert command["size"] == 0.05 and command["expected_price"] == 1.10010
    assert command["direction"] == "BUY" and command["instrument"] == "EURUSD"
    assert command["sl"] == 1.09 and command["tp"] == 1.12
    assert command["expires_at_epoch"] - int(NOW.timestamp()) == 45


def test_a_sell_command_is_priced_from_the_bid_it_could_execute_at(journal):
    sell = proposal(direction="SELL", entry=1.10000, sl=1.11000, tp=1.08000)
    order_id = runner.client_order_id_for("run-1", sell, 0.05)
    terminal = FakeTerminal(ask=1.10010, bid=1.09990,
                            deals=[entry_deal(order_id=order_id, price=1.09990)])
    executor, queue, _, _ = make_executor(journal=journal, terminal=terminal)
    outcome = executor.offer(sell, sizing())
    assert outcome.state == runner.STATE_FILLED
    assert queue.commands[0]["direction"] == "SELL"
    assert queue.commands[0]["expected_price"] == 1.09990


def test_an_expired_command_is_stopped_before_any_order_could_exist(journal):
    clock = Clock()
    queue = FakeQueue(behaviour="expire", clock=clock)
    executor, _, _, _ = make_executor(journal=journal, queue=queue, clock=clock)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_EXPIRED and outcome.queued
    assert outcome.stop_run is False
    assert payloads(journal, runner.SERVED_EVENT) == []
    assert [p["broker_order_sent"] for p in payloads(journal, runner.OUTCOME_EVENT)] == [False]


def test_an_unobserved_fill_stops_the_run_as_unknown_not_as_failed(journal):
    clock = Clock()
    executor, queue, _, _ = make_executor(journal=journal, clock=clock, fill_timeout=3)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_UNCONFIRMED
    assert outcome.stop_run and outcome.requires_reconciliation and outcome.queued
    assert outcome.reason == "NO_FILL_OBSERVED"
    assert len(queue.commands) == 1  # never retried
    assert [p["broker_order_sent"] for p in payloads(journal, runner.OUTCOME_EVENT)] == [None]


def test_an_unreadable_history_is_a_different_unknown_than_no_deal(journal):
    clock = Clock()
    terminal = FakeTerminal(deals_error=RuntimeError("history unavailable"))
    executor, _, _, _ = make_executor(journal=journal, terminal=terminal, clock=clock,
                                      fill_timeout=2)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_UNCONFIRMED
    assert outcome.reason == "FILL_UNVERIFIED_HISTORY_ERROR"
    assert outcome.requires_reconciliation and outcome.stop_run


def test_a_duplicate_id_stops_the_run_and_claims_neither_fill_nor_failure(journal):
    clock = Clock()
    queue = FakeQueue(behaviour="duplicate", clock=clock)
    executor, _, _, _ = make_executor(journal=journal, queue=queue, clock=clock)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_DUPLICATE
    assert outcome.queued is False
    assert outcome.stop_run and outcome.requires_reconciliation
    assert [p["broker_order_sent"] for p in payloads(journal, runner.OUTCOME_EVENT)] == [None]


def test_a_busy_queue_is_a_refusal_before_anything_was_queued(journal):
    clock = Clock()
    queue = FakeQueue(behaviour="busy", clock=clock)
    executor, _, _, _ = make_executor(journal=journal, queue=queue, clock=clock)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED and outcome.reason == "QUEUE_BUSY"
    assert outcome.queued is False
    assert payloads(journal, runner.COMMAND_EVENT) == []


def test_a_stale_quote_refuses_before_the_queue_is_touched(journal):
    clock = Clock()
    terminal = FakeTerminal(ask=1.10500, bid=1.10490)
    executor, queue, _, _ = make_executor(journal=journal, terminal=terminal, clock=clock)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED
    assert codes.PRICE_MOVED_DURING_CONFIRMATION in outcome.reason
    assert outcome.queued is False and queue.commands == []
    assert payloads(journal, runner.COMMAND_EVENT) == []
    assert [p["broker_order_sent"] for p in payloads(journal, runner.OUTCOME_EVENT)] == [False]


def test_an_unfunded_margin_refuses_before_sending(journal):
    terminal = FakeTerminal(margin_free=10.0)
    executor, queue, _, _ = make_executor(journal=journal, terminal=terminal)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED
    assert codes.MARGIN_MINIMUM_LOT_UNFUNDED in outcome.reason
    assert queue.commands == []


def test_a_terminal_read_failure_is_a_refusal_not_an_order(journal):
    terminal = FakeTerminal(read_error=RuntimeError("terminal closed"))
    executor, queue, _, _ = make_executor(journal=journal, terminal=terminal)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED
    assert outcome.reason == codes.MARKET_DATA_READ_FAILED
    assert queue.commands == []


def test_the_runner_cap_refuses_a_size_the_ea_would_also_refuse(journal):
    executor, queue, _, _ = make_executor(journal=journal, max_volume=0.02)
    outcome = executor.offer(proposal(), sizing(volume=0.05))
    assert outcome.state == runner.STATE_REFUSED
    assert "VOLUME_ABOVE_DEMO_CAP" in outcome.reason
    assert queue.commands == []


# ---------------------------------------------------------------------- confirmation
def test_confirm_mode_sends_only_after_the_operator_says_yes(journal):
    order_id = runner.client_order_id_for("run-1", proposal(), 0.05)
    terminal = FakeTerminal(deals=[entry_deal(order_id=order_id)])
    calls = []

    def approve(prop, plan):
        calls.append((prop, plan))
        return True

    executor, queue, _, _ = make_executor(journal=journal, terminal=terminal,
                                          policy=make_policy(execution_mode="confirm"),
                                          approver=approve)
    outcome = executor.offer(proposal(), sizing())
    assert calls and outcome.state == runner.STATE_FILLED
    assert payloads(journal, runner.COMMAND_EVENT)[0]["decision"] == "OPERATOR"


def test_confirm_mode_declines_without_a_queue_entry(journal):
    executor, queue, _, _ = make_executor(journal=journal,
                                          policy=make_policy(execution_mode="confirm"),
                                          approver=lambda prop, plan: False)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED
    assert outcome.reason == codes.OPERATOR_DECLINED and outcome.stop_run is False
    assert queue.commands == []


def test_confirm_mode_without_an_approver_cannot_approve_anything(journal):
    executor, queue, _, _ = make_executor(journal=journal,
                                          policy=make_policy(execution_mode="confirm"))
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED and queue.commands == []


def test_a_failing_prompt_is_a_decline(journal):
    def explode(prop, plan):
        raise RuntimeError("console died")

    executor, queue, _, _ = make_executor(journal=journal,
                                          policy=make_policy(execution_mode="confirm"),
                                          approver=explode)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED and queue.commands == []


def test_the_operator_stop_verb_stops_the_run(journal):
    state = ApprovalState()

    def stop(prop, plan):
        state.stop_requested = True
        return False

    executor, queue, _, _ = make_executor(journal=journal,
                                          policy=make_policy(execution_mode="confirm"),
                                          approver=stop, state=state)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_STOPPED
    assert outcome.stop_run is True and queue.commands == []


def test_auto_mode_never_calls_the_approver(journal):
    calls = []

    def approve(prop, plan):
        calls.append(1)
        return True

    executor, _, _, _ = make_executor(journal=journal, approver=approve)
    outcome = executor.offer(proposal(), sizing())
    assert calls == []
    assert outcome.state in (runner.STATE_UNCONFIRMED, runner.STATE_FILLED)


# ----------------------------------------------------------------------- CLI contract
def args_namespace(**overrides) -> SimpleNamespace:
    base = dict(db="journal.db", tombstones="served.jsonl", bridge_port=8000,
                ttl_seconds=60, fill_timeout=30, max_volume=0.20, cycles=1,
                interval_seconds=15, reconcile_window_hours=24,
                acknowledge_uncertain=False, verbose=False, live_bars=300,
                bridge_token=None, execution=None, run_id="run-1", language=None)
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("overrides", [
    {"ttl_seconds": 4}, {"ttl_seconds": 601}, {"fill_timeout": 0}, {"fill_timeout": 601},
    {"interval_seconds": 0}, {"interval_seconds": 3601}, {"max_volume": 0.0},
    {"max_volume": 0.21}, {"cycles": -1}, {"bridge_port": 0}, {"bridge_port": 65536},
    {"live_bars": 9}, {"reconcile_window_hours": 0}, {"reconcile_window_hours": 721},
])
def test_out_of_range_run_arguments_are_refused_before_anything_starts(overrides):
    with pytest.raises(ValueError):
        runner._validate_run_args(args_namespace(**overrides), "ru")


def test_a_negative_cycle_count_says_negative_not_at_least_one():
    with pytest.raises(ValueError) as caught:
        runner._validate_run_args(args_namespace(cycles=-1), "ru")
    assert "--cycles" in str(caught.value)
    assert str(caught.value) == text("err.non_negative", "ru", value="--cycles")


def test_acceptable_run_arguments_pass():
    runner._validate_run_args(args_namespace(ttl_seconds=5), "vi")
    runner._validate_run_args(args_namespace(ttl_seconds=600, cycles=0), "vi")


def test_language_resolution_order_is_flag_then_environment_then_default(monkeypatch):
    monkeypatch.setenv("MT5_AGENT_LANGUAGE", "en")
    assert runner._argv_language(["--language", "vi"]) == "vi"
    assert runner._argv_language(["--language=ru"]) == "ru"
    assert runner._argv_language([]) == "en"
    monkeypatch.setenv("MT5_AGENT_LANGUAGE", "de")
    assert runner._argv_language([]) == "ru"


def test_the_policy_language_is_the_last_fallback(monkeypatch):
    monkeypatch.delenv("MT5_AGENT_LANGUAGE", raising=False)
    policy = make_policy(ui_language="vi")
    args = args_namespace(language=None)
    args._fallback_language = "ru"
    assert runner._policy_language(policy, args) == "vi"
    args.language = "en"
    assert runner._policy_language(policy, args) == "en"


def test_a_missing_bridge_token_fails_cleanly_without_creating_the_journal(tmp_path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(POLICY_YAML, encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if key not in ("MT5_BRIDGE_TOKEN", "MT5_AGENT_LANGUAGE")}
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_mt5_demo_trading.py"),
         "--policy", str(policy_path), "--db", str(tmp_path / "journal.db"),
         "--language", "ru"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, env=env)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "journal.db").exists()


# ------------------------------------------------------------------- startup gates
def run_args(tmp_path: Path, **overrides) -> SimpleNamespace:
    # Port 0: each test binds an ephemeral loopback port instead of racing for 8000.
    return args_namespace(db=str(tmp_path / "journal.db"), bridge_port=0,
                          tombstones=str(tmp_path / "served.jsonl"), **overrides)


def test_a_real_account_is_refused_before_the_journal_exists(tmp_path):
    terminal = FakeTerminal(trade_mode="real")
    code = runner._run(run_args(tmp_path), make_policy(execution_mode="auto"), "ru", TOKEN,
                       "run-1", terminal)
    assert code == 2
    assert not (tmp_path / "journal.db").exists()


def test_an_identity_mismatch_is_refused(tmp_path):
    policy = make_policy(execution_mode="auto")
    policy = replace(policy, expected_login=999)
    terminal = FakeTerminal()
    assert runner._run(run_args(tmp_path), policy, "ru", TOKEN, "run-1", terminal) == 2
    assert not (tmp_path / "journal.db").exists()


def test_a_served_command_without_a_deal_blocks_the_run_until_acknowledged(tmp_path):
    stamp = datetime.now(UTC) - timedelta(minutes=5)
    (tmp_path / "served.jsonl").write_text(
        json.dumps({"command_id": "cmd-old", "client_order_id": "cc-old",
                    "at": stamp.isoformat()}) + "\n", encoding="utf-8")
    terminal = FakeTerminal(deals=[])
    assert runner._run(run_args(tmp_path), make_policy(execution_mode="auto"), "ru", TOKEN,
                       "run-1", terminal) == 3
    journal = DecisionJournal(tmp_path / "journal.db", run_id="run-1")
    try:
        assert payloads(journal, runner.ACK_EVENT) == []
    finally:
        journal.close()


def test_acknowledging_the_uncertain_startup_is_journaled_and_the_run_continues(
        tmp_path, monkeypatch, capsys):
    stamp = datetime.now(UTC) - timedelta(minutes=5)
    (tmp_path / "served.jsonl").write_text(
        json.dumps({"command_id": "cmd-old", "client_order_id": "cc-old",
                    "at": stamp.isoformat()}) + "\n", encoding="utf-8")
    terminal = FakeTerminal(deals=[])

    class StubAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_cycle(self, **_kwargs):
            return SimpleNamespace(status=codes.STATUS_NO_SIGNAL, blockers=(),
                                   proposal=None, sizing=None, recorded_at=NOW.isoformat())

    monkeypatch.setattr(runner, "MT5Agent", StubAgent)
    code = runner._run(run_args(tmp_path, acknowledge_uncertain=True),
                       make_policy(execution_mode="auto"), "en", TOKEN, "run-1", terminal)
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["audit_chain_valid"] is True
    assert summary["commands_queued"] == 0 and summary["filled"] == 0
    journal = DecisionJournal(tmp_path / "journal.db", run_id="run-1")
    try:
        acked = payloads(journal, runner.ACK_EVENT)
        assert len(acked) == 1 and acked[0]["order_ids"] == ["cc-old"]
        assert acked[0]["broker_order_sent"] is None
    finally:
        journal.close()


# ------------------------------------------------------------------- symbol universe
GOLD = SymbolSpec(symbol="XAUUSD", point=0.01, digits=2, tick_size=0.01, tick_value=1.0,
                  volume_min=0.01, volume_max=50.0, volume_step=0.01, spread_points=30,
                  stops_level_points=0)


def gold_proposal(**overrides) -> dict:
    values = dict(direction="BUY", entry=2400.00, sl=2380.00, tp=2440.00, symbol="XAUUSD")
    values.update(overrides)
    return proposal(**values)


def test_parse_symbols_accepts_a_list_and_expands_all_to_the_curated_universe():
    assert runner.parse_symbols(None) is None
    assert runner.parse_symbols("   ") is None
    assert runner.parse_symbols("all") == list(runner.DEMO_UNIVERSE)
    assert "XAUUSD" in runner.DEMO_UNIVERSE
    assert runner.parse_symbols(" eurusd , XAUUSD, eurusd ") == ["EURUSD", "XAUUSD"]


@pytest.mark.parametrize("value", ["EUR USD", "EUR/USD", "EURUSD;DROP", "x" * 200])
def test_parse_symbols_refuses_a_token_the_bridge_would_refuse(value):
    with pytest.raises(ValueError) as caught:
        runner.parse_symbols(value, language="en")
    assert "not usable" in str(caught.value)


def test_the_gold_executor_builds_a_gold_command_from_gold_specs(journal):
    terminal = FakeTerminal(spec=GOLD, ask=2400.05, bid=2399.95)
    executor, queue, _, _ = make_executor(journal=journal, terminal=terminal,
                                          fill_timeout=2, symbol="XAUUSD")
    outcome = executor.offer(gold_proposal(), sizing())
    assert outcome.state == runner.STATE_UNCONFIRMED  # no deal was shown, so nothing is claimed
    command = queue.commands[0]
    assert command["instrument"] == "XAUUSD"
    assert command["expected_price"] == 2400.05
    assert command["sl"] == 2380.00 and command["tp"] == 2440.00


def test_a_proposal_for_another_instrument_is_never_routed(journal):
    terminal = FakeTerminal(spec=GOLD, ask=2400.05, bid=2399.95)
    executor, queue, _, _ = make_executor(journal=journal, terminal=terminal,
                                          symbol="XAUUSD")
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED
    assert outcome.reason == codes.PROPOSAL_SYMBOL_MISMATCH
    assert queue.commands == []


def test_the_run_covers_every_requested_instrument(tmp_path, monkeypatch, capsys):
    terminal = FakeTerminal(specs={"EURUSD": SPEC, "XAUUSD": GOLD},
                            unavailable={"BTCUSD"})

    class StubAgent:
        def __init__(self, **kwargs):
            self.policy = kwargs["policy"]

        def run_cycle(self, **_kwargs):
            return SimpleNamespace(status=codes.STATUS_NO_SIGNAL, blockers=(),
                                   proposal=None, sizing=None, recorded_at=NOW.isoformat())

    monkeypatch.setattr(runner, "MT5Agent", StubAgent)
    code = runner._run(run_args(tmp_path),
                       make_policy(execution_mode="auto"), "en", TOKEN, "run-1", terminal,
                       ["EURUSD", "XAUUSD", "BTCUSD"])
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["symbols_requested"] == ["EURUSD", "XAUUSD", "BTCUSD"]
    assert summary["symbols_active"] == ["EURUSD", "XAUUSD"]
    assert summary["symbols_unavailable"] == {
        "BTCUSD": "RuntimeError: symbol BTCUSD is not available in the terminal"}
    assert set(summary["per_symbol"]) == {"EURUSD", "XAUUSD"}
    assert summary["passes"] == 1 and summary["cycles"] == 2
    assert summary["broker_order_sent"] is False
    journal = DecisionJournal(tmp_path / "journal.db", run_id="run-1")
    try:
        skipped = payloads(journal, runner.SYMBOL_SKIPPED_EVENT)
        assert [entry["symbol"] for entry in skipped] == ["BTCUSD"]
        assert skipped[0]["broker_order_sent"] is False
    finally:
        journal.close()


def test_a_run_with_no_available_instrument_is_refused(tmp_path, capsys):
    terminal = FakeTerminal(unavailable={"EURUSD", "XAUUSD"})
    code = runner._run(run_args(tmp_path),
                       make_policy(execution_mode="auto"), "en", TOKEN, "run-1", terminal,
                       ["EURUSD", "XAUUSD"])
    assert code == 3
    assert "None of the requested instruments" in capsys.readouterr().err


# ------------------------------------------------------------------------ DemoTerminal
def server_deal(*, stamp: float, magic: int = 8172026, entry: int = 1,
                profit: float = 10.0, swap: float = -0.5, commission: float = -0.7,
                fee: float = 0.0) -> dict:
    return {"magic": magic, "entry": entry, "profit": profit, "swap": swap,
            "commission": commission, "fee": fee, "time_epoch": stamp}


def bare_terminal() -> runner.DemoTerminal:
    terminal = object.__new__(runner.DemoTerminal)
    terminal._peak = None
    terminal._day = None
    terminal._day_start = None
    terminal._offset = 0
    return terminal


def test_equity_baselines_are_the_first_day_equity_and_the_running_peak():
    terminal = bare_terminal()
    values = iter([10_000.0, 10_100.0, 9_900.0, 11_000.0, 11_050.0])
    terminal.account = lambda: AccountInfo(
        login=1, server="Demo", currency="USD", balance=10_000.0, equity=next(values),
        margin_free=10_000.0, leverage=100, trade_mode="demo")
    terminal._server_day = lambda: date(2026, 10, 30)
    assert terminal.day_start_equity == 10_000.0
    assert terminal.peak_equity == 10_100.0
    assert terminal.peak_equity == 10_100.0  # a drawdown never lowers the peak
    terminal._server_day = lambda: date(2026, 10, 31)
    assert terminal.day_start_equity == 11_000.0
    assert terminal.peak_equity == 11_050.0


def test_realized_daily_pnl_comes_from_closed_deals_of_this_magic_only():
    terminal = bare_terminal()
    now_stamp = datetime.now(UTC).timestamp()
    old_stamp = now_stamp - 3 * 86_400
    terminal.entry_deals = lambda since: [
        server_deal(stamp=now_stamp, profit=10.0, swap=-0.5, commission=-0.7),
        server_deal(stamp=now_stamp, entry=0, profit=999.0),          # entry deals are not P&L
        server_deal(stamp=now_stamp, magic=1, profit=555.0),          # another EA's trade
        server_deal(stamp=old_stamp, profit=777.0),                   # previous server day
        server_deal(stamp=now_stamp, profit="nan"),                   # unusable amount
    ]
    assert terminal.realized_daily_pnl == pytest.approx(10.0 - 0.5 - 0.7)


def test_entry_deals_normalizes_terminal_rows():
    class Row:
        magic = 8172026
        comment = "CC:abc"
        entry = 0
        symbol = "EURUSD"
        ticket = 11
        position_id = 12
        price = 1.1
        volume = 0.05
        profit = 0.0
        swap = 0.0
        commission = -0.5
        fee = 0.0

    stamp = datetime.now(UTC).timestamp() - 60
    Row.time = stamp
    Row.time_msc = int(stamp * 1000)

    class Module:
        @staticmethod
        def history_deals_get(start, end):
            return [Row]

    terminal = bare_terminal()
    terminal._mt5 = Module()
    rows = terminal.entry_deals(datetime.now(UTC) - timedelta(hours=1))
    assert len(rows) == 1
    row = rows[0]
    assert row["magic"] == 8172026 and row["comment"] == "CC:abc" and row["entry"] == 0
    assert row["ticket"] == 11 and row["position_id"] == 12
    assert row["time_epoch"] == pytest.approx(stamp)
    assert abs(datetime.fromisoformat(row["time"])
               - datetime.fromtimestamp(stamp, tz=UTC)) < timedelta(seconds=1)


def test_an_empty_history_result_set_is_an_error_not_an_empty_book():
    class Module:
        @staticmethod
        def history_deals_get(start, end):
            return None

    terminal = bare_terminal()
    terminal._mt5 = Module()
    with pytest.raises(RuntimeError):
        terminal.entry_deals(datetime.now(UTC) - timedelta(hours=1))


def test_a_skip_reason_keeps_the_actionable_detail():
    # Verified against the real terminal: four universe symbols are refused because profit is
    # paid in a currency other than the account currency. "RuntimeError" alone hid that.
    reason = runner.skip_reason(RuntimeError(
        "MT5 symbol USDJPY pays profit in JPY, not in the account currency USD;"
        " sizing in account currency would be wrong"))
    assert reason.startswith("RuntimeError: ")
    assert "pays profit in JPY" in reason


def test_a_skip_reason_never_carries_paths_urls_or_credentials():
    for message in (r'cannot open C:\Users\op\Documents\journal.db',
                    'connect to socks5://127.0.0.1:1080 failed',
                    'refused: token=abc123 for login=10013404855',
                    r'\\server\share\terminal64.exe is missing',
                    'port = 8000 already in use'):
        assert runner.skip_reason(RuntimeError(message)) == "RuntimeError"


def test_an_empty_or_endlessly_long_reason_degrades_to_the_class_name():
    assert runner.skip_reason(RuntimeError("   \n  ")) == "RuntimeError"
    assert runner.skip_reason(RuntimeError("x" * (runner._MAX_REASON_CHARS + 1))) == "RuntimeError"
    assert runner.skip_reason(KeyError("boom")) == "KeyError: 'boom'"
