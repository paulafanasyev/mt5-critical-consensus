"""The demo runner is the only component that can cause an order; its gates are tested as such."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from critical_consensus.mt5_agent import (AccountInfo, AgentPolicy, DecisionJournal, SymbolSpec,
                                          codes)
from critical_consensus.mt5_agent.i18n import text
from test_mt5_agent import make_policy as agent_policy

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_mt5_demo_trading as runner  # noqa: E402
from mt5_demo_bridge import BridgeError, NEXT_COMMAND_PATH  # noqa: E402
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
kill_switch: {active: false, reason: runner test}
schedule: {blocked_weekdays_utc: [], window_start_minute_utc: 0, window_end_minute_utc: 1440}
symbol_spec: {symbol: EURUSD, point: 0.00001, digits: 5, tick_size: 0.00001,
  tick_value: 1.0, volume_min: 0.01, volume_max: 100.0, volume_step: 0.01,
  spread_points: 10, stops_level_points: 0}
account: {require_demo: true}
"""


def make_policy(**overrides) -> AgentPolicy:
    """A policy bound to one demo account, because the send path refuses any other.

    ``AgentPolicy`` leaves ``expected_login`` optional for read-only research, and the shared
    fixture builds such a policy. Here every run is one that can put an order on the broker, so
    it states the account the fake terminal reports: login 42 on MetaQuotes-Demo.
    """
    base = {"account__expected_login": 42, "account__expected_server": "MetaQuotes-Demo"}
    base.update(overrides)
    return agent_policy(**base)


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

    def attach_journal(self, journal: DecisionJournal) -> None:
        # Mirrors DemoTerminal's runner contract; this adapter uses the runner's fake broker.
        self.journal = journal

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


class StubAgent:
    """A stand-in for ``MT5Agent``: a usable market with no setup, so the run only drives itself."""

    def __init__(self, **kwargs):
        self.policy = kwargs["policy"]

    def run_cycle(self, **_kwargs):
        return SimpleNamespace(status=codes.STATUS_NO_SIGNAL, blockers=(),
                               proposal=None, sizing=None, recorded_at=NOW.isoformat())

    def post_approval_gates(self, _proposal, _sizing):
        return (), {}


class OfferingAgent(StubAgent):
    """An agent that has a signal and a market that has not moved, so a run reaches the queue.

    ``_run`` wires the executor's gates from the agent it builds here, so this is the seam
    between the run loop and ``DemoExecutor.offer``: nothing short of an agent that offers
    drives a real command through the real bridge.
    """

    def run_cycle(self, **_kwargs):
        return SimpleNamespace(status=codes.STATUS_OBSERVED, blockers=(),
                               proposal=proposal(), sizing=sizing(),
                               recorded_at=NOW.isoformat())

    def post_approval_gates(self, _proposal, signal_sizing):
        return (), {"fill_price": 1.10010, "final_volume": float(signal_sizing["volume"])}


class EAQueryingBridge(runner.DemoBridge):
    """The bridge with the advisor attached, polling ``/v1/execution/next`` over a real socket.

    A command is only served by the HTTP layer, so a run that reaches a broker deal here has
    gone through the queue, the tombstone write and the ``served_at`` the executor waits on,
    exactly as it does against MetaTrader.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.commands_served: list[dict] = []
        self._stop_polling = threading.Event()

    def start(self) -> "EAQueryingBridge":
        super().start()
        threading.Thread(target=self._poll_commands, daemon=True).start()
        return self

    def _poll_commands(self) -> None:
        while not self._stop_polling.wait(0.05):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            try:
                connection.request("GET", NEXT_COMMAND_PATH,
                                   headers={"Host": f"127.0.0.1:{self.port}",
                                            "X-Bridge-Key": self._token})
                response = connection.getresponse()
                body = response.read()
                if response.status == 200:
                    self.commands_served.append(json.loads(body))
            except OSError:
                return
            finally:
                connection.close()

    def stop(self):
        self._stop_polling.set()
        return super().stop()


def proposal(*, direction: str = "BUY", entry: float = 1.10000, sl: float = 1.09000,
             tp: float = 1.12000, symbol: str = "EURUSD") -> dict:
    return {"symbol": symbol, "timeframe": "H1", "direction": direction,
            "evaluated_at": "2026-10-30T12:00:00+00:00", "entry_price": entry,
            "stop_loss": sl, "take_profit": tp, "risk_distance": abs(entry - sl),
            "reward_distance": abs(tp - entry), "risk_reward": 2.0, "atr": 0.001,
            "confidence": 0.5, "reasons": ["test"]}


def sizing(volume: float = 0.05, equity: float = 10_000.0) -> dict:
    return {"volume": volume, "risk_value": volume * 100.0, "risk_fraction": 0.0005,
            "risk_per_lot": 100.0, "margin_required": volume * 4000.0, "equity": equity}


def pass_gates(terminal: FakeTerminal) -> runner.PostApprovalGates:
    """Stand in for ``MT5Agent.post_approval_gates``: the market has not moved since the signal."""
    def gates(proposal, sizing):
        tick = terminal.tick(str(proposal["symbol"]))
        return (), {"fill_price": tick.ask if proposal["direction"] == "BUY" else tick.bid,
                    "final_volume": float(sizing["volume"])}
    return gates


def entry_deal(*, order_id: str, magic: int = runner.MAGIC_NUMBER, symbol: str = "EURUSD",
               entry: int = 0, ticket: int = 1001, price: float = 1.10010,
               volume: float = 0.05, at: datetime | None = None) -> dict:
    moment = at or NOW
    return {"magic": magic, "comment": runner.COMMENT_PREFIX + order_id[:20], "entry": entry,
            "symbol": symbol, "ticket": ticket, "position_id": ticket, "price": price,
            "volume": volume, "profit": 0.0, "swap": 0.0, "commission": 0.0, "fee": 0.0,
            "time_epoch": moment.timestamp(), "time": moment.isoformat()}


@pytest.fixture
def journal(tmp_path):
    decision_log = DecisionJournal(tmp_path / "journal.db", run_id="run-1")
    yield decision_log
    decision_log.close()


def armed_stop(active: bool = True):
    """A stand-in for ``KillSwitchStore.payload``: what the runner reads before it trades."""
    return lambda: ({"kill_switch_active": True, "kill_switch_reason": "operator"} if active
                    else {"kill_switch_active": False, "kill_switch_reason": "NOT_ARMED"})


def make_executor(*, journal: DecisionJournal, queue: FakeQueue | None = None,
                  terminal: FakeTerminal | None = None, policy=None,
                  approver=None, state: ApprovalState | None = None,
                  clock: Clock | None = None, fill_timeout: int = 5,
                  max_volume: float = runner.MAX_DEMO_VOLUME, out=None,
                  language: str = "ru", ttl: int = 60,
                  gates: runner.PostApprovalGates | None = None,
                  kill_switch=armed_stop(False),
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
        post_approval_gates=gates or pass_gates(terminal), kill_switch=kill_switch,
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


def test_unresolved_served_does_not_let_an_older_deal_answer_a_newer_command():
    """The client order id is a hash, so a reused signal really can collide with its own past.

    A deal that predates the tombstone cannot be the fill of that command. Resolving it would
    drop the id from the reconciliation list and let the run start on top of a position nobody
    accounted for.
    """
    records = [{"client_order_id": "ord-1", "at": NOW.isoformat()}]
    deals = [entry_deal(order_id="ord-1", at=NOW - timedelta(hours=3))]
    assert runner.unresolved_served(records, deals, magic=runner.MAGIC_NUMBER,
                                    since=NOW - timedelta(hours=24)) == ["ord-1"]
    # The same deal does resolve it once the command is the older of the two.
    older = [{"client_order_id": "ord-1", "at": (NOW - timedelta(hours=4)).isoformat()}]
    assert runner.unresolved_served(older, deals, magic=runner.MAGIC_NUMBER,
                                    since=NOW - timedelta(hours=24)) == []


def test_match_entry_deal_refuses_a_deal_stamped_before_the_command():
    deal = entry_deal(order_id="ord-1", at=NOW - timedelta(minutes=10))
    assert runner.match_entry_deal([deal], magic=runner.MAGIC_NUMBER, order_id="ord-1",
                                   symbol="EURUSD", not_before=NOW) is None
    # The tolerance is for clock disagreement, not for a different trade.
    assert runner.match_entry_deal([deal], magic=runner.MAGIC_NUMBER, order_id="ord-1",
                                   symbol="EURUSD",
                                   not_before=NOW - timedelta(minutes=11))["ticket"] == 1001


def test_match_entry_deal_needs_a_time_when_a_floor_is_demanded():
    """A row the terminal gave no timestamp for cannot be placed after the command."""
    deal = entry_deal(order_id="ord-1")
    del deal["time_epoch"]
    assert runner.match_entry_deal([deal], magic=runner.MAGIC_NUMBER, order_id="ord-1",
                                   not_before=NOW) is None


def test_deal_mismatches_compares_the_fill_to_the_order_actually_sent():
    command = {"size": 0.05, "expected_price": 1.10010}
    assert runner.deal_mismatches(entry_deal(order_id="ord-1"), command,
                                  point=0.00001, max_slippage_points=20) == []
    wrong_lot = runner.deal_mismatches(entry_deal(order_id="ord-1", volume=0.50), command,
                                       point=0.00001, max_slippage_points=20)
    assert wrong_lot == ["FILL_VOLUME_MISMATCH"]
    # 20 points is the bound the command was approved against; 30 points is another trade.
    slipped = runner.deal_mismatches(entry_deal(order_id="ord-1", price=1.10040), command,
                                    point=0.00001, max_slippage_points=20)
    assert slipped == ["FILL_PRICE_OUTSIDE_SLIPPAGE_BOUND"]
    assert runner.deal_mismatches(entry_deal(order_id="ord-1", price=1.10011), command,
                                  point=0.00001, max_slippage_points=20) == []
    assert runner.deal_mismatches({"comment": "CC:ord-1"}, command, point=0.00001,
                                  max_slippage_points=20) == ["FILL_VOLUME_UNVERIFIABLE",
                                                              "FILL_PRICE_UNVERIFIABLE"]


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


def test_a_deal_from_before_the_command_is_not_read_as_its_fill(journal):
    """The client order id is a hash of run, proposal and lot, so an old fill can match it.

    Accepting it would book a position this command never opened as ``FILLED`` and let the run
    continue trading on the strength of a deal from a previous attempt.
    """
    order_id = runner.client_order_id_for("run-1", proposal(), 0.05)
    stale = entry_deal(order_id=order_id, at=NOW - timedelta(hours=2))
    executor, _, _, _ = make_executor(journal=journal, terminal=FakeTerminal(deals=[stale]),
                                      fill_timeout=2)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_UNCONFIRMED
    assert outcome.reason == "NO_FILL_OBSERVED"
    assert outcome.deal is None and outcome.stop_run
    assert payloads(journal, runner.OUTCOME_EVENT)[-1]["broker_order_sent"] is None


def test_a_deal_for_another_lot_is_not_booked_as_the_fill_of_this_command(journal):
    """The comment proves the EA wrote the deal; only the size proves it is ours."""
    order_id = runner.client_order_id_for("run-1", proposal(), 0.05)
    executor, _, _, _ = make_executor(
        journal=journal, terminal=FakeTerminal(deals=[entry_deal(order_id=order_id,
                                                                 volume=1.00)]))
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_UNCONFIRMED
    assert outcome.reason == "FILL_VOLUME_MISMATCH"
    assert outcome.stop_run and outcome.requires_reconciliation
    assert outcome.deal is not None and outcome.deal["volume"] == 1.00
    payload = payloads(journal, runner.OUTCOME_EVENT)[-1]
    assert payload["broker_order_sent"] is None
    assert payload["deal"]["ticket"] == 1001


def test_a_fill_outside_the_approved_slippage_is_not_booked_as_a_fill(journal):
    """The slippage bound the command was approved against has to hold at the deal too."""
    order_id = runner.client_order_id_for("run-1", proposal(), 0.05)
    executor, _, _, _ = make_executor(
        journal=journal, terminal=FakeTerminal(deals=[entry_deal(order_id=order_id,
                                                                 price=1.10500)]))
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_UNCONFIRMED
    assert outcome.reason == "FILL_PRICE_OUTSIDE_SLIPPAGE_BOUND"
    assert outcome.stop_run and outcome.requires_reconciliation


def test_a_known_outcome_leaves_no_pending_send_for_an_interrupt_to_inherit(journal):
    """Once the broker has answered, a Ctrl-C says nothing about the order.

    ``pending_send`` is what the run loop reads when it is interrupted, so a fill that was
    read and a command the bridge will never hand out both have to clear it: leaving either
    set would arm a stop over a send whose outcome is already known.
    """
    order_id = runner.client_order_id_for("run-1", proposal(), 0.05)
    executor, _, _, _ = make_executor(
        journal=journal, terminal=FakeTerminal(deals=[entry_deal(order_id=order_id)]))
    assert executor.pending_send is None
    assert executor.offer(proposal(), sizing()).state == runner.STATE_FILLED
    assert executor.pending_send is None
    assert executor.journal_interrupted_send() is None

    expired, _, _, _ = make_executor(journal=journal, queue=FakeQueue(behaviour="expire"))
    assert expired.offer(proposal(), sizing()).state == runner.STATE_EXPIRED
    assert expired.pending_send is None
    assert expired.journal_interrupted_send() is None


def test_a_send_the_interrupt_arrived_over_is_journalled_as_unconfirmed(journal):
    """The command left this process; only the broker's deals can say what happened to it."""
    terminal = FakeTerminal(deals=[], deals_error=KeyboardInterrupt())
    executor, queue, _, _ = make_executor(journal=journal, terminal=terminal)
    with pytest.raises(KeyboardInterrupt):
        executor.offer(proposal(), sizing())
    assert len(queue.commands) == 1
    assert executor.pending_send is not None

    outcome = executor.journal_interrupted_send()
    assert outcome.state == runner.STATE_UNCONFIRMED and outcome.stop_run
    assert outcome.requires_reconciliation and outcome.queued
    payload = payloads(journal, runner.OUTCOME_EVENT)[-1]
    assert payload["reason"] == "INTERRUPTED_BEFORE_FILL_WAS_KNOWN"
    assert payload["state"] == runner.STATE_UNCONFIRMED
    assert payload["broker_order_sent"] is None
    assert payload["client_order_id"] == queue.commands[0]["client_order_id"]
    # Booked once: the same command must not be journalled twice by a second pass.
    assert executor.pending_send is None
    assert executor.journal_interrupted_send() is None


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


# ------------------------------------------------------------- post-approval gates
def test_gate_blockers_refuse_before_the_command_is_queued(journal):
    executor, queue, _, _ = make_executor(journal=journal,
                                          gates=lambda _p, _s: (("QUOTE_STALE",), {}))
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED and outcome.reason == codes.QUOTE_STALE
    assert outcome.queued is False and queue.commands == []
    assert payloads(journal, runner.COMMAND_EVENT) == []
    assert [p["broker_order_sent"] for p in payloads(journal, runner.OUTCOME_EVENT)] == [False]


def test_a_gate_that_passes_without_naming_a_fill_is_a_refusal(journal):
    executor, queue, _, _ = make_executor(journal=journal, gates=lambda _p, _s: ((), {}))
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED
    assert outcome.reason == codes.PROPOSAL_UNREADABLE
    assert queue.commands == []


def test_the_demo_cap_applies_to_the_lot_the_agent_resized_to(journal):
    executor, queue, _, _ = make_executor(
        journal=journal,
        gates=lambda _p, _s: ((), {"fill_price": 1.10010, "final_volume": 0.25}))
    outcome = executor.offer(proposal(), sizing(volume=0.05))
    assert outcome.state == runner.STATE_REFUSED
    assert "VOLUME_ABOVE_DEMO_CAP" in outcome.reason
    assert queue.commands == []


def test_the_command_carries_the_fill_and_lot_the_agent_gates_approved(journal):
    order_id = runner.client_order_id_for("run-1", proposal(), 0.03)
    terminal = FakeTerminal(deals=[entry_deal(order_id=order_id, price=1.10030, volume=0.03)])
    executor, queue, _, _ = make_executor(
        journal=journal, terminal=terminal,
        gates=lambda _p, _s: ((), {"fill_price": 1.10030, "final_volume": 0.03,
                                   "slippage": 0.00020, "risk_reward_at_fill": 1.9}))
    outcome = executor.offer(proposal(), sizing(volume=0.05))
    assert outcome.state == runner.STATE_FILLED
    command = queue.commands[0]
    # Not the pre-approval quote, and not the lot the cycle proposed: what the gates re-sized.
    assert command["size"] == 0.03 and command["expected_price"] == 1.10030
    assert command["client_order_id"] == order_id
    queued = payloads(journal, runner.COMMAND_EVENT)[0]
    assert queued["size"] == 0.03
    assert queued["post_approval_gates"]["risk_reward_at_fill"] == 1.9


def test_the_agent_gates_run_after_the_operator_answers(journal):
    seen = []

    def approve(_proposal, _sizing):
        seen.append("operator")
        return True

    def gates(_proposal, _sizing):
        seen.append("gates")
        return (), {"fill_price": 1.10010, "final_volume": 0.05}

    executor, _, _, _ = make_executor(journal=journal,
                                      policy=make_policy(execution_mode="confirm"),
                                      approver=approve, gates=gates)
    executor.offer(proposal(), sizing())
    assert seen == ["operator", "gates"]


def test_an_executor_without_the_agent_gates_cannot_be_built(journal):
    # Wiring the agent's post-approval gates is not optional: an executor that lacks them
    # would queue a command on the strength of a pre-approval quote.
    with pytest.raises(TypeError):
        runner.DemoExecutor(queue=FakeQueue(), journal=journal, terminal=FakeTerminal(),
                            policy=make_policy(), language="ru", run_id="run-1")


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


# ------------------------------------------------------------------- emergency stop
def test_an_armed_stop_refuses_before_any_market_read(journal):
    executor, queue, _, _ = make_executor(
        journal=journal, kill_switch=armed_stop(True),
        terminal=FakeTerminal(read_error=RuntimeError("terminal down")))
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED and outcome.reason == codes.KILL_SWITCH_ACTIVE
    assert outcome.queued is False and queue.commands == []


def test_a_stop_pulled_while_the_operator_was_answering_still_reaches_the_queue(journal):
    flipped = {"armed": False}

    def confirm(proposal, plan):
        flipped["armed"] = True
        return True

    executor, queue, _, _ = make_executor(
        journal=journal, kill_switch=lambda: {"kill_switch_active": flipped["armed"]},
        policy=make_policy(execution_mode="confirm"), approver=confirm)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED and outcome.reason == codes.KILL_SWITCH_ACTIVE
    assert queue.commands == []
    # A command that was never built must not leave identifiers in the journal either.
    last = payloads(journal, runner.OUTCOME_EVENT)[-1]
    assert last["command_id"] == "" and last["client_order_id"] == ""


@pytest.mark.parametrize("state", [None, {}, "armed", {"kill_switch_active": "yes"},
                                   {"kill_switch_active": None}])
def test_a_stop_that_cannot_be_proven_inactive_blocks(journal, state):
    executor, queue, _, _ = make_executor(journal=journal, kill_switch=lambda: state)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED and queue.commands == []


def test_a_stop_state_that_cannot_be_read_counts_as_armed(journal):
    def explode():
        raise RuntimeError("the state file is unreadable")

    executor, queue, _, _ = make_executor(journal=journal, kill_switch=explode)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED and outcome.reason == codes.KILL_SWITCH_ACTIVE
    assert queue.commands == []


def test_an_executor_cannot_be_built_without_a_stop_reader(journal):
    with pytest.raises(TypeError):
        runner.DemoExecutor(queue=FakeQueue(), journal=journal, terminal=FakeTerminal(),
                            policy=make_policy(execution_mode="auto"), language="ru",
                            run_id="run-1", post_approval_gates=pass_gates(FakeTerminal()))


def test_a_stop_written_by_one_process_blocks_the_next(tmp_path, journal):
    """The file is the whole contract: an arm outlives the bridge that recorded it."""
    path = tmp_path / "kill_switch.json"
    runner.KillSwitchStore(path).record(active=True, actor_id="operator", reason="halt",
                                        now=NOW)
    reopened = runner.KillSwitchStore(path)
    assert runner.stop_is_armed(reopened.payload)
    executor, queue, _, _ = make_executor(journal=journal, kill_switch=reopened.payload)
    outcome = executor.offer(proposal(), sizing())
    assert outcome.state == runner.STATE_REFUSED and queue.commands == []


def test_the_runners_own_halt_is_durable(tmp_path):
    store = runner.KillSwitchStore(tmp_path / "kill_switch.json")
    runner._set_stop(store, language="en", reason="EURUSD:STOPPED")
    assert runner.stop_is_armed(runner.KillSwitchStore(tmp_path / "kill_switch.json").payload)


def test_a_halt_that_cannot_be_written_still_blocks(tmp_path, monkeypatch, capsys):
    store = runner.KillSwitchStore(tmp_path / "kill_switch.json")

    def refuse(self, record):
        raise OSError("disk full")

    monkeypatch.setattr(runner.KillSwitchStore, "_write", refuse)
    runner._set_stop(store, language="en", reason="EURUSD:STOPPED")
    assert runner.stop_is_armed(store.payload)
    assert "OSError" in capsys.readouterr().err


def test_a_state_file_that_was_never_written_blocks_rather_than_passes(tmp_path):
    """A fresh path is not a released switch: nothing has ever said trading is allowed."""
    store = runner.KillSwitchStore(tmp_path / "kill_switch.json")
    assert store.read() is None
    assert runner.stop_is_armed(store.payload)


def test_releasing_a_persisted_stop_from_the_cli_needs_the_admin_secret(tmp_path, monkeypatch):
    path = tmp_path / "kill_switch.json"
    store = runner.KillSwitchStore(path)
    store.record(active=True, actor_id="operator", reason="halt", now=NOW)
    args = args_namespace(tombstones=str(tmp_path / "served.jsonl"))
    monkeypatch.delenv(runner.ADMIN_TOKEN_ENV, raising=False)
    assert runner._release_kill_switch(args, "en", TOKEN) == 2
    assert store.read()["kill_switch_active"] is True
    # The bridge key alone must never lift a stop, even when it is long enough.
    monkeypatch.setenv(runner.ADMIN_TOKEN_ENV, TOKEN)
    assert runner._release_kill_switch(args, "en", TOKEN) == 2
    assert store.read()["kill_switch_active"] is True
    monkeypatch.setenv(runner.ADMIN_TOKEN_ENV, "a" * 32)
    assert runner._release_kill_switch(args, "en", TOKEN) == 0
    assert runner.KillSwitchStore(path).read()["kill_switch_active"] is False
    # The release is obeyed by the process that armed it too, without a restart: the file is
    # the authority on every read, so no running bridge or runner can be stuck on its own
    # last word. The one thing a process does hold in memory is an arm that never reached disk.
    assert not runner.stop_is_armed(store.payload)


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


def test_relative_durable_state_is_anchored_to_the_repository(tmp_path):
    """A `cd` must not move the tombstones, or a stop armed elsewhere stops existing.

    The default ``--tombstones`` is written relative, so resolved against the current
    directory the same command would trade against a different record set — and from an empty
    directory it would look like a first-ever run, which after #68 is a run that arms a stop.
    """
    anchored = runner.resolve_state_path("agent_data/demo_bridge/served.jsonl")
    assert anchored == (runner._REPO_ROOT / "agent_data" / "demo_bridge"
                        / "served.jsonl").resolve()
    assert anchored.is_absolute()
    assert runner.resolve_state_path(tmp_path / "served.jsonl") == tmp_path / "served.jsonl"


def test_the_default_run_id_is_reused_by_the_next_process(tmp_path):
    module_root = tmp_path / "state"
    module_root.mkdir()
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(POLICY_YAML, encoding="utf-8")
    first = runner.default_run_id_for(module_root / "served.jsonl", policy_path)
    assert first.startswith("demo-")
    assert first == runner.default_run_id_for(module_root / "served.jsonl", policy_path)
    # A different state file or a different policy is a different set of orders, not a replay.
    assert first != runner.default_run_id_for(module_root / "other.jsonl", policy_path)
    gold = tmp_path / "gold.yaml"
    gold.write_text(POLICY_YAML.replace("symbol: EURUSD", "symbol: XAUUSD"), encoding="utf-8")
    assert first != runner.default_run_id_for(module_root / "served.jsonl", gold)


def test_the_command_line_reuses_one_run_id_across_two_processes(tmp_path, monkeypatch):
    """Restarting without ``--run-id`` must not mint order ids the tombstones have never seen."""
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(POLICY_YAML, encoding="utf-8")
    monkeypatch.setenv(runner.BRIDGE_TOKEN_ENV, TOKEN)
    monkeypatch.setattr(runner, "DemoTerminal", lambda **_kwargs: FakeTerminal())
    seen: list[str] = []
    monkeypatch.setattr(runner, "_run",
                        lambda *_args, **_kwargs: seen.append(_args[4]) or 0)
    argv = ["--policy", str(policy_path), "--db", str(tmp_path / "journal.db"),
            "--tombstones", str(tmp_path / "served.jsonl"), "--language", "en"]
    assert runner.main(list(argv)) == 0
    assert runner.main(list(argv)) == 0
    assert len(seen) == 2 and len(set(seen)) == 1
    assert seen[0].startswith("demo-")
    explicit = argv + ["--run-id", "operator-chosen"]
    assert runner.main(explicit) == 0
    assert seen[-1] == "operator-chosen"


def test_the_bridge_secret_cannot_be_put_on_the_command_line(monkeypatch):
    """A token in argv is readable from any process list and stays in the shell history.

    ``MT5_BRIDGE_TOKEN`` is the only way in, so there is no flag left to misuse by accident.
    """
    monkeypatch.delenv(runner.BRIDGE_TOKEN_ENV, raising=False)
    parser = runner._build_parser("en")
    with pytest.raises(SystemExit):
        parser.parse_args(["--policy", "p.yaml", "--db", "j.db", "--bridge-token", "s" * 40])
    assert "--bridge-token" not in parser.format_help()


@pytest.mark.parametrize("unbound", [
    {"account__expected_login": None},
    {"account__expected_server": None},
    {"account__expected_login": None, "account__expected_server": None},
])
def test_a_policy_that_names_no_account_never_reaches_the_queue(unbound, tmp_path, capsys):
    """The identity check is a comparison, so an empty side of it has to be a refusal.

    ``expected_login: null`` used to pass for every account on the broker, which is precisely
    the account the order would have gone to: the demo flag says only how an account is funded.
    """
    policy = make_policy(execution_mode="auto", **unbound)
    assert runner._run(run_args(tmp_path), policy, "en", TOKEN, "run-1", FakeTerminal()) == 2
    assert text("runner.identity_unbound", "en") in capsys.readouterr().err
    assert not (tmp_path / "journal.db").exists()


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
    # The refusal is the missing secret, not a policy that failed to load: the run has to say
    # which gate stopped it, or an operator reads a green policy and a real failure alike.
    assert text("runner.bridge_token_missing", "ru") in result.stderr


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

        def post_approval_gates(self, _proposal, _sizing):
            return (), {}

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
# Gold as MetaQuotes-Demo actually publishes it: a tick field describing a tenth of a lot and a
# contract size that says 100 ounces. The runner reads specs from the terminal, so its sizing
# tests must be fed the contradictory pair, not a hand-corrected one.
GOLD = SymbolSpec(symbol="XAUUSD", point=0.01, digits=2, tick_size=0.01, tick_value=0.1,
                  contract_size=100.0,
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


def test_a_broker_that_only_lists_suffixed_names_trades_those_names(tmp_path, monkeypatch,
                                                                    capsys):
    """Exness-style naming must not turn a second broker into "no instruments available"."""
    class SuffixTerminal(FakeTerminal):
        def __init__(self, listed, **kwargs):
            super().__init__(**kwargs)
            self._listed = list(listed)
            self.spec_calls: list[str] = []
            self.listed_names: dict[str, str] = {}

        def symbols(self):
            return list(self._listed)

        def symbol_spec(self, symbol):
            self.spec_calls.append(symbol)
            return super().symbol_spec(symbol)

        def use_broker_names(self, mapping):
            self.listed_names = dict(mapping)

    built: list = []

    class RecordingAgent(StubAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            built.append(self.policy)

    monkeypatch.setattr(runner, "MT5Agent", RecordingAgent)
    terminal = SuffixTerminal(["XAUUSDm", "EURUSD"], specs={"XAUUSDm": GOLD},
                              unavailable={"BTCUSD"}, ask=2400.05, bid=2399.95)
    code = runner._run(run_args(tmp_path), make_policy(execution_mode="auto"), "en", TOKEN,
                       "run-1", terminal, ["XAUUSD", "BTCUSD"])
    assert code == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["symbols_requested"] == ["XAUUSD", "BTCUSD"]
    assert summary["symbols_active"] == ["XAUUSDm"]
    assert set(summary["per_symbol"]) == {"XAUUSDm"}
    assert "lists XAUUSD as XAUUSDm" in captured.err
    # The declared symbol_spec describes gold on the server it was written for. An aliased
    # instrument is another broker's contract, so the numbers have to come from the terminal.
    # The policy keeps its own upper-case key; what has to be exact is the name that reaches
    # the terminal, because MT5 has no idea that XAUUSDM and XAUUSDm are the same symbol.
    assert [(policy.symbol, policy.symbol_spec) for policy in built] == [("XAUUSDM", None)]
    assert terminal.spec_calls == ["XAUUSDm", "BTCUSD"]
    assert terminal.listed_names == {"XAUUSD": "XAUUSDm"}
    journal = DecisionJournal(tmp_path / "journal.db", run_id="run-1")
    try:
        aliased = payloads(journal, runner.SYMBOL_ALIAS_EVENT)
        assert aliased == [{"requested": "XAUUSD", "resolved": "XAUUSDm",
                            "broker_order_sent": False}]
    finally:
        journal.close()


def test_a_run_with_no_available_instrument_is_refused(tmp_path, capsys):
    terminal = FakeTerminal(unavailable={"EURUSD", "XAUUSD"})
    code = runner._run(run_args(tmp_path),
                       make_policy(execution_mode="auto"), "en", TOKEN, "run-1", terminal,
                       ["EURUSD", "XAUUSD"])
    assert code == 3
    assert "None of the requested instruments" in capsys.readouterr().err


def test_a_policy_that_arrives_stopped_seeds_the_durable_stop(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "MT5Agent", StubAgent)
    policy = make_policy(execution_mode="auto", kill_switch__active=True,
                         kill_switch__reason="operator halt")
    assert runner._run(run_args(tmp_path), policy, "en", TOKEN, "run-1", FakeTerminal(),
                       ["EURUSD"]) == 0
    captured = capsys.readouterr()
    assert "The emergency stop is armed (operator halt)" in captured.err
    summary = json.loads(captured.out)
    assert summary["kill_switch_armed"] is True
    assert summary["kill_switch_reason"] == "operator halt"
    assert summary["commands_queued"] == 0
    # The stop is a file beside the tombstones, so it outlives this run.
    assert "operator halt" in (tmp_path / "kill_switch.json").read_text(encoding="utf-8")


def test_a_fresh_state_path_is_seeded_armed_not_from_the_policy(tmp_path, monkeypatch, capsys):
    """`active: false` in a policy is the absence of a halt, not consent to trade."""
    monkeypatch.setattr(runner, "MT5Agent", StubAgent)
    policy = make_policy(execution_mode="auto", kill_switch__active=False)
    assert runner._run(run_args(tmp_path), policy, "en", TOKEN, "run-1", FakeTerminal(),
                       ["EURUSD"]) == 0
    captured = capsys.readouterr()
    assert "held no record at all" in captured.err
    summary = json.loads(captured.out)
    assert summary["kill_switch_armed"] is True
    assert summary["commands_queued"] == 0
    seeded = json.loads((tmp_path / "kill_switch.json").read_text(encoding="utf-8"))
    assert seeded["kill_switch_active"] is True
    assert seeded["kill_switch_reason"] == runner.SEEDED_REASON


def test_deleting_the_record_from_outside_a_process_reseeds_it_armed(tmp_path, monkeypatch,
                                                                    capsys):
    """A new process cannot tell a never-used path from a deleted stop, so it must halt."""
    monkeypatch.setattr(runner, "MT5Agent", StubAgent)
    path = tmp_path / "kill_switch.json"
    runner.KillSwitchStore(path).record(active=True, actor_id="operator", reason="halt",
                                        now=NOW)
    path.unlink()
    policy = make_policy(execution_mode="auto", kill_switch__active=False)
    assert runner._run(run_args(tmp_path), policy, "en", TOKEN, "run-1", FakeTerminal(),
                       ["EURUSD"]) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["kill_switch_active"] is True
    assert json.loads(capsys.readouterr().out)["kill_switch_armed"] is True


def test_a_released_record_is_never_reseeded_by_the_next_run(tmp_path, monkeypatch, capsys):
    """The release is a durable operator act: a later run must not overwrite it with an arm."""
    monkeypatch.setattr(runner, "MT5Agent", StubAgent)
    release_stop(tmp_path)
    assert runner._run(run_args(tmp_path), make_policy(execution_mode="auto"), "en", TOKEN,
                       "run-1", FakeTerminal(), ["EURUSD"]) == 0
    captured = capsys.readouterr()
    assert "held no record at all" not in captured.err
    assert json.loads(captured.out)["kill_switch_armed"] is False


def test_a_stop_left_by_an_earlier_run_blocks_the_next_one(tmp_path, monkeypatch, capsys):
    """A policy that no longer mentions the stop cannot lift the one an operator pulled."""
    monkeypatch.setattr(runner, "MT5Agent", StubAgent)
    armed = make_policy(execution_mode="auto", kill_switch__active=True,
                        kill_switch__reason="operator halt")
    assert runner._run(run_args(tmp_path), armed, "en", TOKEN, "run-1", FakeTerminal(),
                       ["EURUSD"]) == 0
    capsys.readouterr()
    clean = make_policy(execution_mode="auto")
    assert runner._run(run_args(tmp_path), clean, "en", TOKEN, "run-2", FakeTerminal(),
                       ["EURUSD"]) == 0
    captured = capsys.readouterr()
    assert "The emergency stop is armed (operator halt)" in captured.err
    assert json.loads(captured.out)["kill_switch_armed"] is True


def release_stop(tmp_path: Path) -> None:
    """Model the operator act a run is no longer allowed to perform for itself.

    A fresh state path is seeded armed, so an end-to-end run has to begin from a released
    record exactly as a real one does: through the release guarded by the admin secret, not
    through a `kill_switch: {active: false}` line in a policy nobody read.
    """
    runner.KillSwitchStore(tmp_path / "kill_switch.json").record(
        active=False, actor_id="operator", reason="released for the test run", now=NOW)


def _run_with_real_bridge(tmp_path, monkeypatch, capsys, *, policy, terminal, release=True):
    """Drive ``_run`` end to end: the real bridge on a real socket, the agent that offers."""
    if release:
        release_stop(tmp_path)
    monkeypatch.setattr(runner, "MT5Agent", OfferingAgent)
    built: list[runner.DemoBridge] = []

    class Tracked(EAQueryingBridge):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(runner, "DemoBridge", Tracked)
    code = runner._run(run_args(tmp_path, fill_timeout=1), policy, "en", TOKEN, "run-1",
                       terminal, ["EURUSD"])
    captured = capsys.readouterr()
    summary = json.loads(captured.out) if captured.out.strip() else None
    return code, summary, captured.err, built[0] if built else None


def test_a_run_that_reaches_the_queue_is_served_and_matched_to_a_broker_deal(
        tmp_path, monkeypatch, capsys):
    """The whole demo chain in one test: signal, gates, queue, socket, serve, deal, fill.

    Every wiring mistake between the run loop and the executor shows up here: the command
    only reaches ``FILLED`` if the run handed the executor the agent's gates, the real
    bridge as its queue, and an unarmed reading of the durable stop.
    """
    order_id = runner.client_order_id_for("run-1", proposal(), 0.05)
    # ``_run`` keeps the wall clock, so the deal has to postdate the command it answers: a
    # fill stamped in the fixture's fixed NOW would be a deal from before the run started.
    terminal = FakeTerminal(deals=[entry_deal(order_id=order_id, at=datetime.now(UTC))])
    capsys.readouterr()
    code, summary, _err, bridge = _run_with_real_bridge(
        tmp_path, monkeypatch, capsys, policy=make_policy(execution_mode="auto"),
        terminal=terminal)
    assert code == 0
    assert summary["commands_queued"] == 1 and summary["filled"] == 1
    assert summary["broker_order_sent"] is True and summary["halted"] is False
    assert summary["kill_switch_armed"] is False
    assert len(bridge.commands_served) == 1
    served = bridge.commands_served[0]
    assert served["instrument"] == "EURUSD" and served["size"] == 0.05
    assert served["expected_price"] == 1.10010 and served["mode"] == "demo"
    journal = DecisionJournal(tmp_path / "journal.db", run_id="run-1")
    try:
        outcomes = payloads(journal, runner.OUTCOME_EVENT)
        assert outcomes[-1]["state"] == runner.STATE_FILLED
        assert outcomes[-1]["broker_order_sent"] is True
        assert payloads(journal, runner.SERVED_EVENT)[0]["command_id"] == served["command_id"]
    finally:
        journal.close()


def test_a_run_with_the_stop_armed_never_puts_a_command_in_the_queue(
        tmp_path, monkeypatch, capsys):
    """The stop the run reads is the durable file, not a variable the loop forgot to check.

    A run launched stopped must refuse before the market read, so the journal shows a
    refusal naming the stop and no command is ever built.
    """
    order_id = runner.client_order_id_for("run-1", proposal(), 0.05)
    terminal = FakeTerminal(deals=[entry_deal(order_id=order_id)])
    policy = make_policy(execution_mode="auto", kill_switch__active=True,
                         kill_switch__reason="operator halt")
    capsys.readouterr()
    code, summary, err, bridge = _run_with_real_bridge(tmp_path, monkeypatch, capsys,
                                                       policy=policy, terminal=terminal,
                                                       release=False)
    assert code == 0
    assert summary["commands_queued"] == 0 and summary["refused"] == 1
    assert summary["filled"] == 0 and summary["broker_order_sent"] is False
    assert summary["kill_switch_armed"] is True
    assert bridge is not None and bridge.commands_served == []
    journal = DecisionJournal(tmp_path / "journal.db", run_id="run-1")
    try:
        assert payloads(journal, runner.COMMAND_EVENT) == []
        refused = payloads(journal, runner.OUTCOME_EVENT)[-1]
        assert refused["state"] == runner.STATE_REFUSED
        assert refused["reason"] == codes.KILL_SWITCH_ACTIVE
        assert refused["broker_order_sent"] is False
    finally:
        journal.close()
    assert "The emergency stop is armed (operator halt)" in err


def test_a_served_command_that_shows_no_deal_arms_the_stop_for_the_next_process(
        tmp_path, monkeypatch, capsys):
    """An uncertain send is a durable stop, not a line in this process's log."""
    terminal = FakeTerminal(deals=[])
    capsys.readouterr()
    code, summary, _err, bridge = _run_with_real_bridge(
        tmp_path, monkeypatch, capsys, policy=make_policy(execution_mode="auto"),
        terminal=terminal)
    assert code == 0 and summary["halted"] is True
    assert summary["commands_queued"] == 1 and summary["unconfirmed"] == 1
    assert summary["requires_manual_reconciliation"] is True
    assert summary["kill_switch_armed"] is True
    assert len(bridge.commands_served) == 1
    armed = json.loads((tmp_path / "kill_switch.json").read_text(encoding="utf-8"))
    assert armed["kill_switch_active"] is True
    assert armed["kill_switch_reason"] == f"EURUSD:{runner.STATE_UNCONFIRMED}"
    # A brand-new process reads the same blocker without being told about it.
    assert runner.stop_is_armed(runner.KillSwitchStore(tmp_path / "kill_switch.json").payload)


def test_interrupting_the_run_over_a_served_command_arms_the_stop_and_exits_non_zero(
        tmp_path, monkeypatch, capsys):
    """Ctrl-C is not a clean finish once the command reached the bridge.

    The interrupt lands in the fill read, so nothing this process knows can say whether the
    EA sent the order. Before this, the run printed "operator stopped", reported an empty
    send count and exited 0, leaving the next process - or the operator - to guess.
    """
    terminal = FakeTerminal(deals=[], deals_error=KeyboardInterrupt())
    capsys.readouterr()
    code, summary, err, bridge = _run_with_real_bridge(
        tmp_path, monkeypatch, capsys, policy=make_policy(execution_mode="auto"),
        terminal=terminal)
    assert code == 4
    assert summary["interrupted_with_send_in_flight"] is True
    assert summary["commands_queued"] == 1 and summary["unconfirmed"] == 1
    assert summary["requires_manual_reconciliation"] is True
    assert summary["halted"] is True and summary["halted_by"] == "EURUSD"
    assert summary["kill_switch_armed"] is True
    assert len(bridge.commands_served) == 1
    armed = json.loads((tmp_path / "kill_switch.json").read_text(encoding="utf-8"))
    assert armed["kill_switch_active"] is True
    assert armed["kill_switch_reason"] == f"EURUSD:{runner.STATE_UNCONFIRMED}"
    assert "was already in the bridge" in err
    journal = DecisionJournal(tmp_path / "journal.db", run_id="run-1")
    try:
        outcome = payloads(journal, runner.OUTCOME_EVENT)[-1]
        assert outcome["state"] == runner.STATE_UNCONFIRMED
        assert outcome["reason"] == "INTERRUPTED_BEFORE_FILL_WAS_KNOWN"
        assert outcome["broker_order_sent"] is None
    finally:
        journal.close()


def test_an_interrupt_before_any_command_exists_exits_zero_and_leaves_the_stop_alone(
        tmp_path, monkeypatch, capsys):
    """The extra gate is for unknown sends, not for every Ctrl-C: a refused cycle is clean."""
    class InterruptingAgent(StubAgent):
        def run_cycle(self, **_kwargs):
            raise KeyboardInterrupt

    release_stop(tmp_path)
    monkeypatch.setattr(runner, "MT5Agent", InterruptingAgent)
    code = runner._run(run_args(tmp_path), make_policy(execution_mode="auto"), "en", TOKEN,
                       "run-1", FakeTerminal(), ["EURUSD"])
    captured = capsys.readouterr()
    assert code == 0
    summary = json.loads(captured.out)
    assert summary["interrupted_with_send_in_flight"] is False
    assert summary["halted"] is False and summary["commands_queued"] == 0
    assert summary["kill_switch_armed"] is False
    armed = json.loads((tmp_path / "kill_switch.json").read_text(encoding="utf-8"))
    assert armed["kill_switch_active"] is False


def test_a_stop_file_that_cannot_be_read_stops_the_run_before_it_starts(tmp_path, monkeypatch,
                                                                        capsys):
    (tmp_path / "kill_switch.json").write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(runner, "MT5Agent", StubAgent)
    code = runner._run(run_args(tmp_path), make_policy(execution_mode="auto"), "en", TOKEN,
                       "run-1", FakeTerminal(), ["EURUSD"])
    assert code == 2
    captured = capsys.readouterr()
    assert "KILL_SWITCH_FILE_CORRUPT" in captured.err
    assert not (tmp_path / "journal.db").exists()


def test_a_stop_file_that_cannot_be_read_cannot_be_released(tmp_path, monkeypatch):
    path = tmp_path / "kill_switch.json"
    path.write_text('{"kill_switch_active": tru', encoding="utf-8")
    monkeypatch.setenv(runner.ADMIN_TOKEN_ENV, "a" * 32)
    monkeypatch.delenv("USERNAME", raising=False)
    args = args_namespace(tombstones=str(tmp_path / "served.jsonl"))
    assert runner._release_kill_switch(args, "en", TOKEN) == 2


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
    terminal._journal = None
    terminal._offset = 0
    terminal._out = None
    terminal._language = "ru"
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


def test_risk_baselines_survive_runner_restart_and_are_scoped_to_the_broker_account(tmp_path):
    database = tmp_path / "risk-baselines.db"
    day = date(2026, 10, 30)

    def account(*, login: int, server: str, equity: float) -> AccountInfo:
        return AccountInfo(login=login, server=server, currency="USD", balance=equity,
                           equity=equity, margin_free=equity, leverage=100,
                           trade_mode="demo")

    first_journal = DecisionJournal(database, run_id="first-process")
    first = bare_terminal()
    first.attach_journal(first_journal)
    first._server_day = lambda: day
    first.account = lambda: account(login=501, server="Broker-Demo", equity=10_000.0)
    assert first.day_start_equity == 10_000.0

    # A new intraday high must be committed before the risk gate can use it.
    first.account = lambda: account(login=501, server="Broker-Demo", equity=10_500.0)
    assert first.peak_equity == 10_500.0
    first_journal.close()

    # A fresh process with lower equity must recover both the original day start and peak.
    second_journal = DecisionJournal(database, run_id="second-process")
    restarted = bare_terminal()
    restarted.attach_journal(second_journal)
    restarted._server_day = lambda: day
    restarted.account = lambda: account(login=501, server="Broker-Demo", equity=9_700.0)
    assert restarted.day_start_equity == 10_000.0
    assert restarted.peak_equity == 10_500.0

    # A different broker account must not inherit the first account's risk history.
    other_account = bare_terminal()
    other_account.attach_journal(second_journal)
    other_account._server_day = lambda: day
    other_account.account = lambda: account(login=502, server="Other-Demo", equity=20_000.0)
    assert other_account.day_start_equity == 20_000.0
    assert other_account.peak_equity == 20_000.0
    second_journal.close()


def test_a_day_budget_that_starts_over_says_so(tmp_path, capsys):
    """Re-seeding the daily risk budget is invisible in the numbers, so it has to be audible.

    A fresh account on a journal that already holds baselines legitimately counts from live
    equity — but that is also what a day rollover looks like, and an operator reading only
    ``day_start_equity`` cannot tell it from a run that has never lost anything. A first-ever
    journal stays quiet, because there is nothing there to have been lost.
    """
    journal = DecisionJournal(tmp_path / "announced-baselines.db", run_id="run-1")

    def standing(login: int, server: str, equity: float) -> runner.DemoTerminal:
        terminal = bare_terminal()
        terminal.attach_journal(journal)
        terminal._server_day = lambda: date(2026, 10, 30)
        terminal._out, terminal._language = sys.stderr, "en"
        terminal.account = lambda: AccountInfo(login=login, server=server, currency="USD",
                                               balance=equity, equity=equity,
                                               margin_free=equity, leverage=100,
                                               trade_mode="demo")
        return terminal

    assert standing(501, "Broker-Demo", 10_000.0).day_start_equity == 10_000.0
    assert "risk budget" not in capsys.readouterr().err
    assert standing(502, "Other-Demo", 20_000.0).day_start_equity == 20_000.0
    err = capsys.readouterr().err
    assert "daily risk budget" in err and "502" in err and "20000.00" in err
    journal.close()


def test_corrupt_risk_baseline_journal_fails_closed(tmp_path):
    journal = DecisionJournal(tmp_path / "corrupt-risk-baselines.db", run_id="run-1")
    terminal = bare_terminal()
    terminal.attach_journal(journal)
    terminal._server_day = lambda: date(2026, 10, 30)
    terminal.account = lambda: AccountInfo(
        login=501, server="Broker-Demo", currency="USD", balance=10_000.0,
        equity=10_000.0, margin_free=10_000.0, leverage=100, trade_mode="demo")
    journal.chain_status = lambda: {"valid": False, "reason": "TEST_CORRUPTION"}
    with pytest.raises(RuntimeError, match="journal integrity"):
        _ = terminal.day_start_equity
    journal.close()


def test_realized_daily_pnl_comes_from_closed_deals_of_this_magic_only():
    terminal = bare_terminal()
    now_stamp = datetime.now(UTC).timestamp()
    old_stamp = now_stamp - 3 * 86_400
    terminal.entry_deals = lambda since: [
        server_deal(stamp=now_stamp, profit=10.0, swap=-0.5, commission=-0.7),
        server_deal(stamp=now_stamp, entry=0, profit=999.0),          # entry deals are not P&L
        server_deal(stamp=now_stamp, magic=1, profit=555.0),          # another EA's trade
        server_deal(stamp=old_stamp, profit=777.0),                   # previous server day
    ]
    assert terminal.realized_daily_pnl == pytest.approx(10.0 - 0.5 - 0.7)


@pytest.mark.parametrize("junk", [
    {"profit": "nan"}, {"profit": None}, {"profit": float("nan")},
    {"time_epoch": "not a stamp"}, {"time_epoch": None}, {"commission": "nan"},
])
def test_a_closed_deal_that_cannot_be_read_refuses_the_day_rather_than_undercounting_it(junk):
    """A day this adapter cannot add up is a day it must not trade.

    Dropping the row would hand the agent a smaller realised loss than the account carries and
    let the daily budget keep spending. Raising is what the gate already understands: the agent
    reads this through a property and blocks the cycle with EQUITY_BASELINES_UNAVAILABLE.
    """
    terminal = bare_terminal()
    deal = server_deal(stamp=datetime.now(UTC).timestamp(), profit=10.0)
    deal.update(junk)
    terminal.entry_deals = lambda since: [deal]
    with pytest.raises(RuntimeError):
        _ = terminal.realized_daily_pnl


def test_a_cost_column_a_broker_does_not_report_is_still_a_number():
    """An absent swap column is not an unreadable one; the day must still add up."""
    terminal = bare_terminal()
    deal = server_deal(stamp=datetime.now(UTC).timestamp(), profit=10.0)
    del deal["swap"]
    terminal.entry_deals = lambda since: [deal]
    assert terminal.realized_daily_pnl == pytest.approx(10.0 - 0.7)


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


def test_entry_deals_asks_for_a_window_wide_enough_to_see_a_server_clock_fill():
    # The terminal returns nothing for a window ending close to UTC even when the deal exists,
    # so a fill the EA really placed read as an empty book and stopped the run. Measured live on
    # this host at 2026-11-02 04:58 UTC: one and the same seven-day left edge gave 1 row and no
    # magic-8172026 deal at now+4h, and 5 rows with both fills at now+8h — the right edge is what
    # clips, because these naive bounds are read on the server clock (UTC+7200 s).
    seen: list[tuple[datetime, datetime]] = []

    class Module:
        @staticmethod
        def history_deals_get(start, end):
            seen.append((start, end))
            return []

    terminal = bare_terminal()
    terminal._mt5 = Module()
    commanded = datetime.now(UTC) - timedelta(seconds=90)
    terminal.entry_deals(commanded)
    start, end = seen[0]
    assert start <= (commanded - timedelta(days=1)).replace(tzinfo=None)
    assert end >= (datetime.now(UTC) + timedelta(hours=12)).replace(tzinfo=None)


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
