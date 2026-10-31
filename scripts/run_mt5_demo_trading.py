"""Route an approved agent proposal to the demo EA over the localhost bridge.

The order-free ``critical_consensus.mt5_agent`` package decides and sizes; this
script is the only place that knows an order can exist. It builds one schema-valid
command per approved signal, hands it to :mod:`mt5_demo_bridge`, waits for the EA to
pick it up, and reconciles the result against the broker's own deal history by magic
number and comment. It never retries an uncertain result: a command the EA picked up
without an observed fill stops the run and is reported as requiring manual
reconciliation.

Hard gates before a command is built: the terminal account must be demo, the policy
must require a demo account, the fresh quote must still match the approved entry,
geometry and broker stops levels must hold, the volume must be inside the broker's
step/min/max and the runner's cap, and the margin must be funded. The EA independently
re-checks every one of those plus the kill switch, the TTL and its own compile-time
demo gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from critical_consensus.mt5_agent import (AgentPolicy, DecisionJournal, MT5Agent, codes, i18n,
                                          load_policy)
from critical_consensus.mt5_agent.backend import LiveMT5Backend
from critical_consensus.mt5_agent.i18n import describe, normalize_language, text
from mt5_demo_bridge import (BRIDGE_TOKEN_ENV, IDENTIFIER_PATTERN, BridgeError, DemoBridge,
                             validate_token)
from operator_answers import ApprovalState
from run_mt5_agent import EXECUTION_CHOICES, _make_approver

UTC = timezone.utc
LANGUAGE_ENV_VAR = "MT5_AGENT_LANGUAGE"
MAGIC_NUMBER = 8172026
COMMENT_PREFIX = "CC:"
# A curated liquid universe, not "every broker symbol": exotic crosses and illiquid CFDs
# would add spread and gap risk the strategy was never measured on. ``--symbols all``
# expands to this list; anything the terminal does not offer is reported, never invented.
DEMO_UNIVERSE = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
                 "EURGBP", "XAUUSD", "XAGUSD", "US500", "NAS100", "BTCUSD")
# The EA refuses anything above its MaxVolume input (0.20); the runner caps below it too,
# so a mis-sized proposal is refused here instead of consuming the EA's whole allowance.
MAX_DEMO_VOLUME = 0.20
MAX_SLIPPAGE_POINTS = 20
DEFAULT_TTL_SECONDS = 60
MAX_TTL_SECONDS = 600
COMMAND_EVENT = "demo_order_command"
SERVED_EVENT = "demo_order_served"
OUTCOME_EVENT = "demo_order_outcome"
ACK_EVENT = "demo_reconciliation_ack"
SYMBOL_SKIPPED_EVENT = "demo_symbol_unavailable"
STATE_FILLED = "FILLED"
STATE_EXPIRED = "EXPIRED"
STATE_UNCONFIRMED = "UNCONFIRMED"
STATE_REFUSED = "REFUSED"
STATE_DUPLICATE = "DUPLICATE"
STATE_STOPPED = "STOPPED"


# --------------------------------------------------------------------- identities
def _digest(*parts: Any) -> str:
    canonical = "|".join(str(part) for part in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def signal_id_for(proposal: Mapping[str, Any]) -> str:
    return "sig-" + _digest(proposal["symbol"], proposal["timeframe"],
                            proposal["evaluated_at"], proposal["direction"],
                            f"{float(proposal['entry_price']):.10f}")[:24]


def client_order_id_for(run_id: str, proposal: Mapping[str, Any], volume: float) -> str:
    """Deterministic across restarts: the bridge refuses a second order with this id."""
    return "cc-" + _digest(run_id, proposal["symbol"], proposal["direction"],
                           f"{float(volume):.8f}", f"{float(proposal['entry_price']):.10f}",
                           f"{float(proposal['stop_loss']):.10f}",
                           f"{float(proposal['take_profit']):.10f}")[:28]


def permission_id_for(run_id: str, policy_version: int) -> str:
    return "perm-" + _digest("permission", run_id, policy_version)[:20]


def decision_id_for(command_id: str, decision: str) -> str:
    return "dec-" + _digest("decision", command_id, decision)[:24]


_MAX_REASON_CHARS = 180
# A skip reason is operator-facing text. Quoting, connection or filesystem details must not
# ride along from an unexpected adapter exception, so anything that looks like one is dropped
# and the whole line falls back to the exception class name.
_REASON_NOISE = re.compile(r"""(?:[A-Za-z]:[\\/]|\\\\|://|\btoken\b|\bpassword\b|\blogin\s*[:=]
                                |\bport\s*[:=])""", re.IGNORECASE | re.VERBOSE)


def skip_reason(exc: BaseException) -> str:
    """Why a symbol was dropped: the class name plus a bounded, scrubbed detail if safe."""
    detail = " ".join(str(exc).split())
    if not detail or len(detail) > _MAX_REASON_CHARS or _REASON_NOISE.search(detail):
        return type(exc).__name__
    return f"{type(exc).__name__}: {detail}"


def parse_symbols(value: str | None, *, language: str = "ru") -> list[str] | None:
    """``all`` or a comma list; ``None``/empty means the policy's own symbol."""
    if value is None:
        return None
    text_value = str(value).strip()
    if not text_value:
        return None
    if text_value.lower() == "all":
        return list(DEMO_UNIVERSE)
    symbols: list[str] = []
    for token in text_value.split(","):
        symbol = token.strip().upper()
        if not symbol:
            continue
        # The same rule the bridge applies to ``instrument``: a symbol the command could
        # never legally carry is refused here instead of after the operator confirmed.
        if not IDENTIFIER_PATTERN.fullmatch(symbol):
            raise ValueError(text("err.symbols", language, value=token.strip()))
        if symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise ValueError(text("err.symbols", language, value=text_value))
    return symbols


def build_command(*, command_id: str, client_order_id: str, permission_id: str,
                  signal_id: str, instrument: str, direction: str, size: float,
                  expected_price: float, sl: float, tp: float, user_decision_id: str,
                  now: datetime, ttl_seconds: int) -> dict[str, Any]:
    """Assemble exactly the flat object the EA and the schema expect."""
    payload = {"command_id": command_id, "client_order_id": client_order_id,
               "permission_id": permission_id, "signal_id": signal_id,
               "instrument": instrument, "direction": direction,
               "size": float(size), "expected_price": float(expected_price),
               "sl": float(sl), "tp": float(tp), "mode": "demo",
               "user_decision_id": user_decision_id, "user_decision": "APPROVE",
               "user_approved": True,
               "expires_at_epoch": int(now.timestamp()) + int(ttl_seconds)}
    payload["payload_hash"] = _digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    payload["created_at"] = now.astimezone(UTC).isoformat()
    return payload


# ------------------------------------------------------------------------- gates
def price_gates(proposal: Mapping[str, Any], tick: Any, spec: Any,
                *, max_slippage_points: int = MAX_SLIPPAGE_POINTS) -> tuple[float, list[str]]:
    """Fresh executable price plus the reasons the quote no longer fits the proposal."""
    direction = proposal["direction"]
    live = float(tick.ask if direction == "BUY" else tick.bid)
    reasons: list[str] = []
    if abs(live - float(proposal["entry_price"])) / spec.point > max_slippage_points:
        reasons.append(codes.PRICE_MOVED_DURING_CONFIRMATION)
    sl, tp = float(proposal["stop_loss"]), float(proposal["take_profit"])
    if direction == "BUY" and not sl < live < tp:
        reasons.append(codes.BUY_GEOMETRY_INVALID)
    if direction == "SELL" and not tp < live < sl:
        reasons.append(codes.SELL_GEOMETRY_INVALID)
    stops = spec.stops_level_points
    if stops:
        if abs(live - sl) / spec.point < stops:
            reasons.append(codes.STOP_INSIDE_BROKER_STOPS_LEVEL)
        if abs(tp - live) / spec.point < stops:
            reasons.append(codes.TARGET_INSIDE_BROKER_STOPS_LEVEL)
    return live, reasons


def volume_gates(volume: float, spec: Any, *,
                 max_volume: float = MAX_DEMO_VOLUME) -> list[str]:
    if not math.isfinite(volume) or volume <= 0:
        return ["VOLUME_INVALID"]
    reasons: list[str] = []
    if volume > max_volume + 1e-12:
        reasons.append("VOLUME_ABOVE_DEMO_CAP")
    if volume < spec.volume_min - 1e-12:
        reasons.append("VOLUME_BELOW_BROKER_MIN")
    if volume > spec.volume_max + 1e-12:
        reasons.append("VOLUME_ABOVE_BROKER_MAX")
    steps = volume / spec.volume_step
    if abs(steps - round(steps)) > 1e-9:
        reasons.append("VOLUME_NOT_ON_STEP")
    return reasons


def match_entry_deal(deals: Any, *, magic: int, order_id: str,
                     symbol: str | None = None) -> dict[str, Any] | None:
    """The broker's entry deal for one command, found by magic and the EA's comment."""
    comment = COMMENT_PREFIX + str(order_id)[:20]
    for deal in deals:
        try:
            if int(deal.get("magic")) != magic:
                continue
            if int(deal.get("entry", -1)) != 0:  # DEAL_ENTRY_IN only
                continue
        except (TypeError, ValueError):
            continue
        if str(deal.get("comment", "")) != comment:
            continue
        if symbol is not None and str(deal.get("symbol", "")).upper() != symbol.upper():
            continue
        return dict(deal)
    return None


def _record_time(record: Mapping[str, Any]) -> datetime | None:
    raw = record.get("at")
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC)


def unresolved_served(records: Any, deals: Any, *, magic: int,
                      since: datetime) -> list[str]:
    """Served order ids from the tombstone window that have no entry deal at the broker."""
    unresolved: list[str] = []
    for record in records:
        moment = _record_time(record)
        if moment is None or moment < since:
            continue
        order_id = record.get("client_order_id")
        if not isinstance(order_id, str):
            continue
        if match_entry_deal(deals, magic=magic, order_id=order_id) is None:
            unresolved.append(order_id)
    return unresolved


# ----------------------------------------------------------------------- terminal
def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


class DemoTerminal(LiveMT5Backend):
    """Order-free read adapter plus the broker facts the runner needs to reconcile.

    ``LiveMT5Backend`` implements the order-free read surface; this subclass adds the
    equity baselines the agent requires and a deal-history reader used to prove a fill.
    Baselines are reconstructed from the live process (day start = first equity seen on
    the current broker-server day) and from the broker's own closed deals, never guessed.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._peak: float | None = None
        self._day: Any = None
        self._day_start: float | None = None

    def _server_day(self) -> Any:
        return (datetime.now(UTC) + timedelta(seconds=self.server_utc_offset_seconds)).date()

    def _ensure_baselines(self) -> tuple[float, float]:
        equity = float(self.account().equity)
        day = self._server_day()
        if self._day != day or self._day_start is None or self._peak is None:
            self._day = day
            self._day_start = equity
            self._peak = equity
        else:
            self._peak = max(self._peak, equity)
        return self._day_start, self._peak

    @property
    def day_start_equity(self) -> float:
        return self._ensure_baselines()[0]

    @property
    def peak_equity(self) -> float:
        return self._ensure_baselines()[1]

    @property
    def realized_daily_pnl(self) -> float:
        """Closed-deal profit for the current broker-server day, from the broker itself."""
        now = datetime.now(UTC)
        server_epoch = now.timestamp() + self.server_utc_offset_seconds
        day_start_epoch = (server_epoch // 86400) * 86400 - self.server_utc_offset_seconds
        start = datetime.fromtimestamp(day_start_epoch - 3600, tz=UTC)
        rows = self.entry_deals(start)
        total = 0.0
        for deal in rows:
            if int(deal.get("magic", 0)) != MAGIC_NUMBER:
                continue
            if int(deal.get("entry", -1)) != 1:  # DEAL_ENTRY_OUT
                continue
            moment = deal.get("time_epoch")
            if not isinstance(moment, (int, float)) or moment < day_start_epoch:
                continue
            profit = _number(deal.get("profit"))
            if profit is None:
                # A row whose profit cannot be read is not partially counted: half a deal
                # silently shifts the daily loss budget the agent gates on.
                continue
            total += profit
            for name in ("swap", "commission", "fee"):
                value = _number(deal.get(name))
                if value is not None:
                    total += value
        return total

    def entry_deals(self, since: datetime) -> list[dict[str, Any]]:
        """Deals since ``since``, widened by an hour on each side of the server clock skew."""
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        # The terminal's history filters are interpreted on the broker server clock; a wide
        # window makes the exact boundary convention irrelevant because every candidate is
        # re-filtered by magic and comment in ``match_entry_deal``.
        start = (since - timedelta(hours=1)).astimezone(UTC).replace(tzinfo=None)
        end = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)
        rows = self._mt5.history_deals_get(start, end)
        if rows is None:
            raise RuntimeError("MT5 history_deals_get() returned no result set")
        deals: list[dict[str, Any]] = []
        for row in rows:
            moment = self._offset_datetime(row, "deal")
            deals.append({
                "magic": int(getattr(row, "magic", 0) or 0),
                "comment": str(getattr(row, "comment", "") or ""),
                "entry": int(getattr(row, "entry", -1)),
                "symbol": str(getattr(row, "symbol", "") or ""),
                "ticket": int(getattr(row, "ticket", 0) or 0),
                "position_id": int(getattr(row, "position_id", 0) or 0),
                "price": float(getattr(row, "price", 0.0) or 0.0),
                "volume": float(getattr(row, "volume", 0.0) or 0.0),
                "profit": float(getattr(row, "profit", 0.0) or 0.0),
                "swap": float(getattr(row, "swap", 0.0) or 0.0),
                "commission": float(getattr(row, "commission", 0.0) or 0.0),
                "fee": float(getattr(row, "fee", 0.0) or 0.0),
                "time_epoch": moment.timestamp(),
                "time": moment.isoformat(),
            })
        return deals


# ----------------------------------------------------------------------- executor
@dataclass(frozen=True)
class Outcome:
    state: str
    reason: str = ""
    command_id: str = ""
    client_order_id: str = ""
    deal: Mapping[str, Any] | None = None
    stop_run: bool = False
    requires_reconciliation: bool = False
    # True only when the command actually entered the bridge queue, so a refusal before
    # enqueue is never counted as an order the EA may have seen.
    queued: bool = False


@dataclass
class RunState:
    """Bridge kill-switch state for the EA; flipped before the runner stops."""

    active: bool = False
    reason: str = "RUNNER"

    def payload(self) -> dict[str, Any]:
        return {"kill_switch_active": bool(self.active),
                "kill_switch_reason": str(self.reason) or "RUNNER"}


class DemoExecutor:
    """One approved proposal -> one command -> one observed or unknown broker outcome."""

    def __init__(self, *, queue: Any, journal: DecisionJournal, terminal: Any,
                 policy: AgentPolicy, language: str, run_id: str,
                 symbol: str | None = None,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 fill_timeout_seconds: int = 30, poll_seconds: float = 1.0,
                 max_volume: float = MAX_DEMO_VOLUME,
                 max_slippage_points: int = MAX_SLIPPAGE_POINTS,
                 approver: Callable[[Mapping[str, Any], Mapping[str, Any]], bool] | None = None,
                 operator_state: ApprovalState | None = None,
                 clock: Callable[[], datetime] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 out: Any = None) -> None:
        self._queue = queue
        self._journal = journal
        self._terminal = terminal
        self._policy = policy
        # One executor trades exactly one instrument; the run may hold several executives.
        self._symbol = str(symbol or policy.symbol).upper()
        self._language = language
        self._run_id = run_id
        self._ttl = ttl_seconds
        self._fill_timeout = fill_timeout_seconds
        self._poll = poll_seconds
        self._max_volume = max_volume
        self._max_slippage = max_slippage_points
        self._approver = approver
        self._state = operator_state or ApprovalState()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        self._out = out

    # ------------------------------------------------------------------ helpers
    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("runner clock must be timezone-aware")
        return moment.astimezone(UTC)

    def _say(self, line: str) -> None:
        if self._out is not None:
            self._out.write(line + "\n")
            self._out.flush()

    def _outcome(self, state: str, *, reason: str = "", command_id: str = "",
                 client_order_id: str = "", deal: Mapping[str, Any] | None = None,
                 stop_run: bool = False, requires_reconciliation: bool = False,
                 queued: bool = False, broker_order_sent: bool | None = False) -> Outcome:
        payload: dict[str, Any] = {
            "symbol": self._symbol, "state": state, "reason": reason,
            "command_id": command_id, "client_order_id": client_order_id,
            "broker_order_sent": broker_order_sent,
            "requires_manual_reconciliation": requires_reconciliation,
        }
        if deal is not None:
            payload["deal"] = dict(deal)
        self._journal.record(OUTCOME_EVENT, payload)
        return Outcome(state=state, reason=reason, command_id=command_id,
                       client_order_id=client_order_id, deal=deal, stop_run=stop_run,
                       requires_reconciliation=requires_reconciliation, queued=queued)

    # -------------------------------------------------------------------- offer
    def offer(self, proposal: Mapping[str, Any], sizing: Mapping[str, Any]) -> Outcome:
        now = self._now()
        if str(proposal.get("symbol", "")).upper() != self._symbol:
            # A proposal for another instrument must never be routed through this executor:
            # its spec, tick and reconciliation window all belong to ``self._symbol``.
            return self._outcome(STATE_REFUSED, reason=codes.PROPOSAL_SYMBOL_MISMATCH,
                                 broker_order_sent=False)
        try:
            account = self._terminal.account()
            spec = self._terminal.symbol_spec(self._symbol)
            tick = self._terminal.tick(self._symbol)
        except Exception as exc:  # a terminal that will not answer must not trade
            self._say(text("runner.refused", self._language,
                           reason=describe(codes.MARKET_DATA_READ_FAILED, self._language)))
            return self._outcome(STATE_REFUSED, reason=codes.MARKET_DATA_READ_FAILED,
                                 broker_order_sent=False)
        volume = float(sizing["volume"])
        live, blockers = price_gates(proposal, tick, spec,
                                     max_slippage_points=self._max_slippage)
        blockers.extend(volume_gates(volume, spec, max_volume=self._max_volume))
        try:
            margin = spec.margin_requirement(volume, account.leverage, live)
        except ValueError:
            margin = math.inf
        if margin > account.margin_free:
            blockers.append(codes.MARGIN_MINIMUM_LOT_UNFUNDED)
        if blockers:
            reason = ",".join(sorted(set(blockers)))
            self._say(text("runner.refused", self._language,
                           reason=", ".join(describe(code, self._language)
                                            for code in sorted(set(blockers)))))
            return self._outcome(STATE_REFUSED, reason=reason, broker_order_sent=False)

        approved = self._confirmation(proposal, sizing)
        if approved == "stop":
            self._say(text("runner.stopped", self._language))
            return self._outcome(STATE_STOPPED, reason=codes.OPERATOR_DECLINED,
                                 stop_run=True, broker_order_sent=False)
        if approved == "decline":
            return self._outcome(STATE_REFUSED, reason=codes.OPERATOR_DECLINED,
                                 broker_order_sent=False)

        command_id = uuid.uuid4().hex
        order_id = client_order_id_for(self._run_id, proposal, volume)
        command = build_command(
            command_id=command_id, client_order_id=order_id,
            permission_id=permission_id_for(self._run_id, self._policy.version),
            signal_id=signal_id_for(proposal), instrument=self._symbol,
            direction=str(proposal["direction"]), size=volume, expected_price=live,
            sl=float(proposal["stop_loss"]), tp=float(proposal["take_profit"]),
            user_decision_id=decision_id_for(command_id, "APPROVE"),
            now=now, ttl_seconds=self._ttl)
        try:
            queued = self._queue.enqueue(command)
        except BridgeError as exc:
            if exc.code == "DUPLICATE_COMMAND":
                self._say(text("runner.duplicate", self._language))
                return self._outcome(STATE_DUPLICATE, reason=exc.code, command_id=command_id,
                                     client_order_id=order_id, stop_run=True,
                                     requires_reconciliation=True, broker_order_sent=None)
            self._say(text("runner.refused", self._language, reason=exc.code))
            return self._outcome(STATE_REFUSED, reason=exc.code, command_id=command_id,
                                 client_order_id=order_id, broker_order_sent=False)
        command_id = str(queued["command_id"])
        self._journal.record(COMMAND_EVENT, {
            **{key: queued[key] for key in ("command_id", "client_order_id", "permission_id",
                                            "signal_id", "instrument", "direction", "size",
                                            "expected_price", "sl", "tp", "user_decision_id",
                                            "user_decision", "user_approved",
                                            "expires_at_epoch", "payload_hash", "created_at")},
            "decision": "AUTO" if approved == "approve" and self._approver is None
            else "OPERATOR",
            "execution_mode": self._policy.execution_mode,
            "bridge_base_url": getattr(self._queue, "base_url", ""),
            "broker_order_sent": False,
            "stage": "queued",
        })
        self._say(text("runner.queued", self._language, command_id=command_id,
                       direction=str(proposal["direction"]), symbol=str(proposal["symbol"]),
                       volume=f"{volume:.2f}"))

        if not self._wait_for_serve(command):
            self._say(text("runner.expired", self._language))
            return self._outcome(STATE_EXPIRED, reason="COMMAND_EXPIRED",
                                 command_id=command_id, client_order_id=order_id,
                                 queued=True, broker_order_sent=False)
        self._journal.record(SERVED_EVENT, {
            "symbol": self._symbol, "command_id": command_id, "client_order_id": order_id,
            "broker_order_sent": None, "state": "SERVED"})
        return self._await_fill(command_id=command_id, order_id=order_id,
                                since=now - timedelta(minutes=10))

    def _confirmation(self, proposal: Mapping[str, Any], sizing: Mapping[str, Any]) -> str:
        if self._policy.execution_mode != "confirm":
            return "approve"
        if self._approver is None:
            return "decline"
        try:
            approved = self._approver(proposal, sizing)
        except Exception:  # a failing prompt is a refusal, never a pass
            return "decline"
        if approved is True:
            return "approve"
        if self._state.stop_requested:
            return "stop"
        return "decline"

    def _wait_for_serve(self, command: Mapping[str, Any]) -> bool:
        command_id = str(command["command_id"])
        expires = float(command["expires_at_epoch"])
        while self._now().timestamp() <= expires + 1:
            if self._queue.served_at(command_id) is not None:
                return True
            if self._queue.expired_at(command_id) is not None:
                return False
            self._sleep(self._poll)
        return False

    def _await_fill(self, *, command_id: str, order_id: str, since: datetime) -> Outcome:
        deadline = self._now().timestamp() + self._fill_timeout
        last_error = ""
        while True:
            try:
                deals = self._terminal.entry_deals(since)
                last_error = ""
            except Exception as exc:
                last_error = type(exc).__name__
                deals = []
            deal = match_entry_deal(deals, magic=MAGIC_NUMBER, order_id=order_id,
                                    symbol=self._symbol)
            if deal is not None:
                self._say(text("runner.filled", self._language, ticket=deal.get("ticket"),
                               price=f"{float(deal.get('price', 0.0)):.5f}",
                               volume=f"{float(deal.get('volume', 0.0)):.2f}"))
                return self._outcome(STATE_FILLED, command_id=command_id,
                                     client_order_id=order_id, deal=deal, queued=True,
                                     broker_order_sent=True)
            if self._now().timestamp() >= deadline:
                break
            self._sleep(self._poll)
        if last_error:
            # A history read that never succeeded is a different uncertainty than "the
            # broker showed no deal": both stop the run, but the operator must know which.
            self._say(text("runner.unconfirmed_unreadable", self._language,
                           error=last_error, seconds=self._fill_timeout))
            return self._outcome(STATE_UNCONFIRMED, reason="FILL_UNVERIFIED_HISTORY_ERROR",
                                 command_id=command_id, client_order_id=order_id,
                                 stop_run=True, requires_reconciliation=True, queued=True,
                                 broker_order_sent=None)
        self._say(text("runner.unconfirmed", self._language, seconds=self._fill_timeout))
        return self._outcome(STATE_UNCONFIRMED, reason="NO_FILL_OBSERVED",
                             command_id=command_id, client_order_id=order_id,
                             stop_run=True, requires_reconciliation=True, queued=True,
                             broker_order_sent=None)


# --------------------------------------------------------------------------- CLI
def _build_parser(language: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=text("runner.description", language), allow_abbrev=False)
    parser.add_argument("--policy", required=True, help=text("cli.policy", language))
    parser.add_argument("--db", required=True, help=text("cli.db", language))
    parser.add_argument("--run-id", help=text("cli.run_id", language))
    parser.add_argument("--language", help=text("cli.language", language))
    parser.add_argument("--execution", choices=EXECUTION_CHOICES,
                        help=text("cli.execution", language))
    parser.add_argument("--symbols", help=text("cli.symbols", language))
    parser.add_argument("--bridge-port", type=int, default=8000,
                        help=text("cli.bridge_port", language))
    parser.add_argument("--bridge-token", help=text("cli.bridge_token", language))
    parser.add_argument("--tombstones", default="agent_data/demo_bridge/served.jsonl",
                        help=text("cli.tombstones", language))
    parser.add_argument("--cycles", type=int, default=0,
                        help=text("cli.runner_cycles", language))
    parser.add_argument("--interval-seconds", type=int, default=15,
                        help=text("cli.interval_seconds", language))
    parser.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS,
                        help=text("cli.ttl_seconds", language))
    parser.add_argument("--fill-timeout", type=int, default=30,
                        help=text("cli.fill_timeout", language))
    parser.add_argument("--reconcile-window-hours", type=int, default=24,
                        help=text("cli.reconcile_window", language))
    parser.add_argument("--max-volume", type=float, default=MAX_DEMO_VOLUME,
                        help=text("cli.max_volume", language))
    parser.add_argument("--live-bars", type=int, default=300,
                        help=text("cli.live_bars", language))
    parser.add_argument("--terminal-path", help=text("cli.terminal_path", language))
    parser.add_argument("--timeout-ms", type=int, default=60_000)
    parser.add_argument("--acknowledge-uncertain", action="store_true",
                        help=text("cli.acknowledge_uncertain", language))
    parser.add_argument("--verbose", action="store_true", help=text("cli.verbose", language))
    return parser


def _argv_language(argv: list[str]) -> str:
    candidate = None
    for index, item in enumerate(argv):
        if item.startswith("--language="):
            candidate = item.split("=", 1)[1]
        elif item == "--language" and index + 1 < len(argv):
            candidate = argv[index + 1]
    for value in (candidate, os.environ.get(LANGUAGE_ENV_VAR)):
        if value:
            try:
                return normalize_language(value)
            except ValueError:
                continue
    return i18n.DEFAULT_LANGUAGE


def _policy_language(policy: AgentPolicy, args: argparse.Namespace) -> str:
    candidates = [args.language, os.environ.get(LANGUAGE_ENV_VAR), policy.ui_language]
    for candidate in candidates:
        if candidate:
            try:
                return normalize_language(candidate)
            except ValueError:
                raise ValueError(text("err.language", args._fallback_language,
                                      value=candidate)) from None
    return i18n.DEFAULT_LANGUAGE


def _validate_run_args(args: argparse.Namespace, language: str) -> None:
    if not 5 <= args.ttl_seconds <= MAX_TTL_SECONDS:
        raise ValueError(text("cli.ttl_seconds", language))
    if not 1 <= args.fill_timeout <= 600:
        raise ValueError(text("cli.fill_timeout", language))
    if not 1 <= args.interval_seconds <= 3600:
        raise ValueError(text("cli.interval_seconds", language))
    if not 1 <= args.reconcile_window_hours <= 720:
        raise ValueError(text("cli.reconcile_window", language))
    if not 0 < args.max_volume <= MAX_DEMO_VOLUME:
        raise ValueError(text("cli.max_volume", language))
    if args.cycles < 0:
        raise ValueError(text("err.non_negative", language, value="--cycles"))
    if not 1 <= args.bridge_port <= 65535:
        raise ValueError(text("cli.bridge_port", language))
    if not 10 <= args.live_bars <= 100_000:
        raise ValueError(text("cli.live_bars", language))


def _run(args: argparse.Namespace, policy: AgentPolicy, language: str, token: str,
         run_id: str, terminal: DemoTerminal,
         symbols: list[str] | None = None) -> int:
    bridge: DemoBridge | None = None
    journal: DecisionJournal | None = None
    state = RunState(active=bool(policy.kill_switch_active),
                     reason=str(policy.kill_switch_reason or "POLICY"))
    try:
        account = terminal.account()
        if not account.is_demo:
            print(text("runner.not_demo", language), file=sys.stderr)
            return 2
        login_ok = policy.expected_login is None or account.login == policy.expected_login
        server_ok = policy.expected_server is None or account.server == policy.expected_server
        if not (login_ok and server_ok):
            print(text("runner.identity_mismatch", language, login=account.login,
                       server=account.server), file=sys.stderr)
            return 2

        journal = DecisionJournal(args.db, run_id=run_id)
        try:
            bridge = DemoBridge(token=token, tombstones_path=args.tombstones,
                                kill_switch=state.payload, host="127.0.0.1",
                                port=args.bridge_port, command_ttl_seconds=args.ttl_seconds)
        except (BridgeError, OSError) as exc:
            print(text("out.error", language, reason=str(exc)), file=sys.stderr)
            return 2
        now = datetime.now(UTC)
        since = now - timedelta(hours=args.reconcile_window_hours)
        records = bridge.tombstone_records()
        if records:
            try:
                deals = terminal.entry_deals(since)
            except Exception as exc:
                print(text("out.error", language, reason=f"{type(exc).__name__}"), file=sys.stderr)
                return 3
            unresolved = unresolved_served(records, deals, magic=MAGIC_NUMBER, since=since)
            if unresolved:
                listing = ",".join(unresolved[:10])
                if not args.acknowledge_uncertain:
                    print(text("runner.uncertain_startup", language, ids=listing),
                          file=sys.stderr)
                    return 3
                journal.record(ACK_EVENT, {"order_ids": unresolved,
                                           "acknowledged_at": now.isoformat(),
                                           "broker_order_sent": None})
                print(text("runner.acknowledged", language, ids=listing), file=sys.stderr)

        symbols = list(symbols or [policy.symbol])
        specs: dict[str, Any] = {}
        unavailable: dict[str, str] = {}
        for symbol in symbols:
            try:
                specs[symbol] = terminal.symbol_spec(symbol)
            except Exception as exc:
                # A symbol the broker does not offer cannot be traded, but it must be named
                # in the journal instead of silently disappearing from the run. The reason is
                # operator-facing: "pays profit in JPY" is actionable, "RuntimeError" is not.
                reason = skip_reason(exc)
                unavailable[symbol] = reason
                journal.record(SYMBOL_SKIPPED_EVENT, {
                    "symbol": symbol, "reason": reason, "broker_order_sent": False})
                print(text("runner.symbol_unavailable", language, symbol=symbol,
                           reason=reason), file=sys.stderr)
        active = [symbol for symbol in symbols if symbol in specs]
        if not active:
            print(text("runner.no_symbols", language), file=sys.stderr)
            return 3

        operator_state = ApprovalState()
        agents: dict[str, tuple[MT5Agent, DemoExecutor]] = {}
        for symbol in active:
            # The declared spec belongs to the policy's own symbol; for any other instrument
            # the live terminal is the only source of contract data, so it is dropped rather
            # than re-declared (a wrong declaration would silently mis-size gold).
            per_policy = (policy if symbol == policy.symbol
                          else replace(policy, symbol=symbol, symbol_spec=None))
            approver = None
            if per_policy.execution_mode == "confirm":
                approver = _make_approver(language=language, state=operator_state,
                                          digits=specs[symbol].digits, out=sys.stderr,
                                          source=sys.stdin)
            executor = DemoExecutor(queue=bridge, journal=journal, terminal=terminal,
                                    policy=per_policy, language=language, run_id=run_id,
                                    symbol=symbol, ttl_seconds=args.ttl_seconds,
                                    fill_timeout_seconds=args.fill_timeout,
                                    max_volume=args.max_volume, approver=approver,
                                    operator_state=operator_state)
            agents[symbol] = (MT5Agent(backend=terminal, policy=per_policy, journal=journal),
                              executor)
        try:
            bridge.start()
        except OSError as exc:
            print(text("out.error", language, reason=f"{type(exc).__name__}: {exc}"),
                  file=sys.stderr)
            return 3
        print(text("runner.ready", language, login=account.login, server=account.server,
                   balance=f"{account.balance:.2f}", leverage=account.leverage,
                   url=bridge.base_url), file=sys.stderr)
        print(text("runner.symbols_line", language, active=", ".join(active),
                   unavailable=", ".join(sorted(unavailable)) or "-"), file=sys.stderr)
        print(text("runner.ea_hint", language), file=sys.stderr)

        statuses = (codes.STATUS_BLOCKED, codes.STATUS_NO_SIGNAL, codes.STATUS_OBSERVED,
                    codes.STATUS_PAPER_OPENED)
        counts = {symbol: {status: 0 for status in statuses} for symbol in active}
        tallies = {symbol: {"signals": 0, "commands": 0, STATE_FILLED: 0, STATE_EXPIRED: 0,
                            STATE_UNCONFIRMED: 0, STATE_REFUSED: 0, STATE_DUPLICATE: 0}
                   for symbol in active}
        passes = 0
        halted = False
        halted_by = ""
        try:
            while args.cycles <= 0 or passes < args.cycles:
                for symbol in active:
                    agent, executor = agents[symbol]
                    result = agent.run_cycle(open_paper_position=False)
                    counts[symbol][result.status] = counts[symbol].get(result.status, 0) + 1
                    if args.verbose:
                        blockers = ", ".join(describe(code, language)
                                             for code in result.blockers) or "-"
                        print(f"{result.recorded_at} {symbol} "
                              f"{describe(result.status, language)}: {blockers}",
                              file=sys.stderr)
                    if result.status == codes.STATUS_OBSERVED and result.proposal \
                            and result.sizing:
                        tally = tallies[symbol]
                        tally["signals"] += 1
                        outcome = executor.offer(result.proposal, result.sizing)
                        if outcome.queued:
                            tally["commands"] += 1
                        if outcome.state in tally:
                            tally[outcome.state] += 1
                        if outcome.stop_run:
                            state.active = True
                            state.reason = f"{symbol}:{outcome.state}"
                            halted = True
                            halted_by = symbol
                            print(text("runner.stopped_uncertain", language),
                                  file=sys.stderr)
                            break
                if halted:
                    break
                passes += 1
                if args.cycles > 0 and passes >= args.cycles:
                    break
                time.sleep(args.interval_seconds)
        except KeyboardInterrupt:
            print(text("runner.stopped", language), file=sys.stderr)

        def total(key: str) -> int:
            return sum(tally[key] for tally in tallies.values())

        aggregate = {status: sum(counts[symbol].get(status, 0) for symbol in active)
                     for status in statuses}
        filled = total(STATE_FILLED)
        report = journal.report(run_id=run_id)
        summary = {
            "run_id": run_id, "language": language,
            "execution_mode": policy.execution_mode,
            "symbols_requested": symbols, "symbols_active": active,
            "symbols_unavailable": unavailable,
            "per_symbol": {
                symbol: {**counts[symbol],
                         "signals": tallies[symbol]["signals"],
                         "commands_queued": tallies[symbol]["commands"],
                         "filled": tallies[symbol][STATE_FILLED],
                         "expired": tallies[symbol][STATE_EXPIRED],
                         "unconfirmed": tallies[symbol][STATE_UNCONFIRMED],
                         "refused": tallies[symbol][STATE_REFUSED],
                         "duplicate": tallies[symbol][STATE_DUPLICATE],
                         "broker_order_sent": tallies[symbol][STATE_FILLED] > 0}
                for symbol in active},
            "account_login": account.login, "account_server": account.server,
            "account_currency": account.currency, "account_is_demo": account.is_demo,
            "passes": passes,
            "cycles": sum(aggregate.values()),
            "status_counts": aggregate,
            "signals": total("signals"), "commands_queued": total("commands"),
            "filled": filled, "expired": total(STATE_EXPIRED),
            "unconfirmed": total(STATE_UNCONFIRMED),
            "refused": total(STATE_REFUSED), "duplicate": total(STATE_DUPLICATE),
            "halted": halted, "halted_by": halted_by,
            "broker_order_sent": filled > 0,
            "requires_manual_reconciliation": total(STATE_UNCONFIRMED) > 0
            or total(STATE_DUPLICATE) > 0,
            "audit_chain_valid": report["verified_chain"]["valid"],
            "anchor_hash": report["anchor_hash"],
            "last_hash": report["verified_chain"]["last_hash"],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(text("runner.summary", language, run_id=run_id, signals=total("signals"),
                   commands=total("commands"), filled=filled,
                   expired=total(STATE_EXPIRED), unconfirmed=total(STATE_UNCONFIRMED),
                   refused=total(STATE_REFUSED),
                   chain=str(report["verified_chain"]["valid"]).lower()), file=sys.stderr)
        print(text("runner.disclaimer", language), file=sys.stderr)
        return 0
    finally:
        if bridge is not None:
            bridge.stop()
        if journal is not None:
            journal.close()


def main(argv: list[str] | None = None) -> int:
    # Help, reports and prompts are localized, so a Cyrillic or Vietnamese string can reach a
    # cp1252 pipe. UTF-8 with replacement keeps --help working and a demo run alive; stdin uses
    # surrogates so operator answers typed on a legacy console survive the decode.
    i18n.configure_console_output(sys.stdin, errors="surrogateescape")
    i18n.configure_console_output(sys.stdout, sys.stderr)
    argv = list(sys.argv[1:] if argv is None else argv)
    language = _argv_language(argv)
    parser = _build_parser(language)
    args = parser.parse_args(argv)
    args._fallback_language = language
    try:
        policy = load_policy(args.policy)
        language = _policy_language(policy, args)
        args._fallback_language = language
        _validate_run_args(args, language)
        if not policy.require_demo or policy.mode not in ("observe", "paper"):
            raise ValueError(text("runner.policy_gate", language))
        if args.execution is not None:
            policy = replace(policy, execution_mode=args.execution, ui_language=language)
        else:
            policy = replace(policy, ui_language=language)
        symbols = parse_symbols(args.symbols, language=language) or [policy.symbol]
        if len(symbols) > 50:
            raise ValueError(text("err.symbols", language, value=f"{len(symbols)} symbols"))
        token = args.bridge_token or os.environ.get(BRIDGE_TOKEN_ENV)
        try:
            token = validate_token(token)
        except BridgeError:
            raise ValueError(text("runner.bridge_token_missing", language)) from None
    except (ValueError, OSError) as exc:
        print(text("out.error", language, reason=str(exc)), file=sys.stderr)
        return 2

    run_id = args.run_id or f"demo-{uuid.uuid4().hex[:8]}"
    try:
        terminal = DemoTerminal(server_utc_offset_seconds=policy.server_utc_offset_seconds,
                                terminal_path=args.terminal_path, timeout_ms=args.timeout_ms)
    except Exception as exc:
        print(text("out.error", language, reason=f"{type(exc).__name__}: {exc}"),
              file=sys.stderr)
        return 2
    try:
        return _run(args, policy, language, token, run_id, terminal, symbols)
    finally:
        try:
            terminal.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
