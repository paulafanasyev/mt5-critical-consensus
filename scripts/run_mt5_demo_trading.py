"""Route an approved agent proposal to the demo EA over the localhost bridge.

The order-free ``critical_consensus.mt5_agent`` package decides and sizes; this
script is the only place that knows an order can exist. It builds one schema-valid
command per approved signal, hands it to :mod:`mt5_demo_bridge`, waits for the EA to
pick it up, and reconciles the result against the broker's own deal history by magic
number and comment. It never retries an uncertain result: a command the EA picked up
without an observed fill stops the run and is reported as requiring manual
reconciliation.

Hard gates before a command is built: the terminal account must be demo, the policy
must require a demo account and must name the account it expects — an unbound
`expected_login` would match every account the broker has, and the order goes to
whichever one the terminal holds — the fresh quote must still match the approved entry,
geometry and broker stops levels must hold, the volume must be inside the broker's
step/min/max and the runner's cap, and the margin must be funded. After the operator
answers, the agent's own post-approval gates are re-run on the proposal — quote age,
spread, slippage bound, risk/reward at the fill, re-sizing and the additive daily
budget — and the command carries the price and volume those gates produced. The EA
independently re-checks every one of those plus the kill switch, the TTL and its own
compile-time demo gate.

The emergency stop is a file, not a variable: ``kill_switch.json`` next to the tombstones
is armed by an authenticated POST to the bridge or by this runner's own halt, seeded ARMED
by a first run that finds no record -- a policy that omits the stop is not a grant of
permission to trade -- and released only with the separate admin token. Both this process
and the EA read that same record on every cycle, so a stop an operator pulled survives a
restart, a stop released elsewhere is obeyed here, and anything
other than a provably inactive record -- unreadable, deleted, or never written -- counts as
armed.

An interrupt is not a clean finish either: a Ctrl-C that lands after a command reached the
bridge queues that command as unconfirmed, arms the durable stop and exits 4, because only
the broker's deals can say whether the EA sent the order.

What makes those two records durable is where they live. ``--tombstones`` is resolved against
the repository rather than the directory the command was typed in, so no `cd` can move the
stop or the served-id history, and the default run id is derived from that state path plus the
policy file. The id matters because the order ids are built from it: a random one per process
would make every restart look like a first-ever order to the bridge, and the refusal that
stops a duplicate signal from being sent twice would only ever have protected one process.
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
_REPO_ROOT = _SCRIPTS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from critical_consensus.mt5_agent import (AgentPolicy, DecisionJournal, MT5Agent, codes, i18n,
                                          load_policy)
from critical_consensus.mt5_agent.backend import (LiveMT5Backend,
                            resolve_broker_symbol)
from critical_consensus.mt5_agent.i18n import (DEFAULT_LANGUAGE, describe, normalize_language,
                                               text)
from mt5_demo_bridge import (ADMIN_TOKEN_ENV, BRIDGE_TOKEN_ENV, IDENTIFIER_PATTERN,
                             BridgeError, DemoBridge, KillSwitchStore, SEEDED_REASON,
                             kill_switch_state_path, release_refusal, validate_token)
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
# The most the terminal clock and this process's clock may disagree by and a deal still be
# accepted as the answer to a command. ``entry_deals`` converts broker history to UTC with the
# server offset, so the residual is the offset's own granularity plus drift; a minute covers
# both without letting a stale deal through, which is what the floor is there to catch.
DEAL_CLOCK_SKEW_SECONDS = 60
DEFAULT_TTL_SECONDS = 60
MAX_TTL_SECONDS = 600
COMMAND_EVENT = "demo_order_command"
SERVED_EVENT = "demo_order_served"
OUTCOME_EVENT = "demo_order_outcome"
ACK_EVENT = "demo_reconciliation_ack"
RISK_BASELINE_EVENT = "demo_risk_baseline"
SYMBOL_SKIPPED_EVENT = "demo_symbol_unavailable"
SYMBOL_ALIAS_EVENT = "demo_symbol_alias"
STATE_FILLED = "FILLED"
STATE_EXPIRED = "EXPIRED"
STATE_UNCONFIRMED = "UNCONFIRMED"
STATE_REFUSED = "REFUSED"
STATE_DUPLICATE = "DUPLICATE"
STATE_STOPPED = "STOPPED"

# ``MT5Agent.post_approval_gates``: proposal and sizing dicts in, blockers and the fill the
# command may carry out. It and the durable kill-switch reader are the two dependencies an
# executor cannot be built without, because they are the only authorities on "may I send".
PostApprovalGates = Callable[[Mapping[str, Any], Mapping[str, Any]],
                             tuple[tuple[str, ...], Mapping[str, Any]]]


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


def resolve_state_path(raw: str | Path) -> Path:
    """Pin durable state to the repository, not to whichever directory the command was typed in.

    The tombstones and the kill switch beside them are the only memory that outlives a
    process: which order ids were already served, and whether an operator pulled the stop.
    Resolved against the current directory, the same command would trade against a different
    record set depending on where it was launched from, and running it from an empty
    directory would look exactly like a first-ever run.
    """
    path = Path(raw)
    return path if path.is_absolute() else (_REPO_ROOT / path).resolve()


def default_run_id_for(tombstones_path: str | Path, policy_path: str | Path) -> str:
    """A run id that survives a restart, because the order ids are built from it.

    ``client_order_id_for`` is deterministic in (run id, signal, volume) so the bridge can
    refuse a second order for the same signal. A random run id would keep that promise only
    inside one process: restart after an unknown outcome and the retry carries a fresh id the
    tombstones have never seen, which is how a dedup guard becomes a duplicate order. Deriving
    the id from the state file and the policy still separates different accounts and policies.
    """
    return "demo-" + _digest(str(resolve_state_path(tombstones_path)),
                             str(Path(policy_path).resolve()))[:12]


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
def broker_level(value: float, spec: Any) -> float:
    """The number the broker receives: MT5 rounds every price to the symbol's digits."""
    factor = 10 ** int(spec.digits)
    return math.floor(value * factor + 0.5) / factor


def price_gates(proposal: Mapping[str, Any], tick: Any, spec: Any,
                *, max_slippage_points: int = MAX_SLIPPAGE_POINTS) -> tuple[float, list[str]]:
    """Fresh executable price plus the reasons the quote no longer fits the proposal.

    Each guard measures the rounded level, because that is what the order carries: checking the
    unrounded pair queues a stop that rounds onto the price, or onto the broker's stops level,
    after the check passed - and the reference EA refuses it on the chart, so the journal shows a
    send attempt with no matching refusal here.
    """
    direction = proposal["direction"]
    live = broker_level(float(tick.ask if direction == "BUY" else tick.bid), spec)
    sl = broker_level(float(proposal["stop_loss"]), spec)
    tp = broker_level(float(proposal["take_profit"]), spec)
    reasons: list[str] = []
    if abs(live - float(proposal["entry_price"])) / spec.point > max_slippage_points:
        reasons.append(codes.PRICE_MOVED_DURING_CONFIRMATION)
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
                     symbol: str | None = None,
                     not_before: datetime | None = None) -> dict[str, Any] | None:
    """The broker's entry deal for one command, found by magic and the EA's comment.

    ``not_before`` is the moment this process built the command. MT5 history is a time range
    and the comment is text the EA wrote, so without a floor a deal left over from an earlier
    attempt of the same signal - the id is a hash of run, proposal and lot, so two identical
    cycles really do share one - reads as the fill of an order that was never sent. A deal
    with no usable timestamp is skipped for the same reason: it cannot be placed after the
    command, so it cannot be proven to belong to it. The allowance is for the residual
    difference between the terminal clock and this one after the server offset is applied.
    """
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
        if not_before is not None:
            moment = _number(deal.get("time_epoch"))
            if moment is None or moment < not_before.timestamp() - DEAL_CLOCK_SKEW_SECONDS:
                continue
        return dict(deal)
    return None


def deal_mismatches(deal: Mapping[str, Any], command: Mapping[str, Any], *, point: float,
                    max_slippage_points: int) -> list[str]:
    """Why a comment-matched deal is not the order this command sent, if it is not.

    Matching the comment proves only that the EA wrote it. The lot and the price are what
    prove that the broker filled *this* order, and the slippage bound is the one the command
    was approved against: a fill 40 points away from the approved entry is a different trade,
    and a deal for another lot is a different risk. Claiming either as ``FILLED`` would put a
    number in the journal the broker never honoured.
    """
    reasons: list[str] = []
    size = _number(command.get("size"))
    volume = _number(deal.get("volume"))
    if size is None or volume is None or volume <= 0:
        reasons.append("FILL_VOLUME_UNVERIFIABLE")
    elif abs(volume - size) > 1e-9:
        reasons.append("FILL_VOLUME_MISMATCH")
    expected = _number(command.get("expected_price"))
    price = _number(deal.get("price"))
    if expected is None or price is None or price <= 0 or not math.isfinite(point) \
            or point <= 0:
        reasons.append("FILL_PRICE_UNVERIFIABLE")
    elif abs(price - expected) / point > max_slippage_points:
        reasons.append("FILL_PRICE_OUTSIDE_SLIPPAGE_BOUND")
    return reasons


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
        if match_entry_deal(deals, magic=magic, order_id=order_id, not_before=moment) is None:
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
    Intraday baseline and peak values are persisted in the append-only decision journal,
    keyed by broker account and server day, so restarting this process cannot reset drawdown.
    """

    def __init__(self, *, out: Any = None, language: str = DEFAULT_LANGUAGE,
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._peak: float | None = None
        self._day: Any = None
        self._day_start: float | None = None
        self._journal: DecisionJournal | None = None
        # One of this adapter's behaviours cannot be read off a number, only witnessed: a day's
        # risk budget starting over. The runner gives it the operator's stream and language; a
        # library caller keeps a silent adapter.
        self._out = out
        self._language = normalize_language(language)

    def symbols(self) -> list[str]:
        """Every symbol name this server publishes, for instrument aliasing.

        The runner's adapter, not the package: ``mt5_agent`` promises exactly nine
        order-free terminal calls, and a broker-portability convenience has no business
        widening that surface. Read whole rather than searched per symbol, because a
        suffix guess made from a partial list trades the wrong contract.
        """
        rows = self._mt5.symbols_get()
        if rows is None:
            raise RuntimeError("MT5 symbols_get() returned nothing")
        return [str(row.name) for row in rows]

    def _say(self, key: str, **placeholders: Any) -> None:
        if self._out is not None:
            print(text(key, self._language, **placeholders), file=self._out)

    def attach_journal(self, journal: DecisionJournal) -> None:
        """Attach the durable audit journal before any risk-gated cycle can run."""
        if not isinstance(journal, DecisionJournal):
            raise TypeError("risk baseline journal must be a DecisionJournal")
        self._journal = journal
        self._day = None
        self._day_start = None
        self._peak = None

    def _server_day(self) -> Any:
        return (datetime.now(UTC) + timedelta(seconds=self.server_utc_offset_seconds)).date()

    def _restore_baselines(self, account: Any, day: Any, equity: float) -> tuple[float, float, bool]:
        journal = getattr(self, "_journal", None)
        if journal is None:
            # The standalone adapter remains useful in isolated unit tests. The actual
            # demo runner attaches its journal before constructing agents or executors.
            return equity, equity, True

        integrity = journal.chain_status()
        if not integrity.get("valid"):
            raise RuntimeError("risk baseline journal integrity check failed")

        day_key = day.isoformat()
        records = journal.events(limit=None, event_type=RISK_BASELINE_EVENT)
        matching: list[tuple[float, float]] = []
        for event in records:
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise RuntimeError("risk baseline journal payload is invalid")
            if payload.get("account_login") != account.login \
                    or payload.get("account_server") != account.server:
                continue
            if payload.get("server_day") != day_key:
                continue
            start = _number(payload.get("day_start_equity"))
            peak = _number(payload.get("peak_equity"))
            if start is None or peak is None or start <= 0 or peak < start:
                raise RuntimeError("risk baseline journal values are invalid")
            matching.append((start, peak))

        if not matching:
            if records:
                # The journal holds baselines, just none for this account, server and broker
                # day. Either the server day rolled over or the terminal reconnected elsewhere:
                # in both cases the drawdown and the daily loss budget start again from live
                # equity, which is indistinguishable from an unbroken run unless it is said.
                self._say("runner.risk_baseline_seeded", login=account.login,
                          server=account.server, day=day_key, equity=f"{equity:.2f}")
            return equity, equity, True
        day_start = matching[0][0]
        if any(start != day_start for start, _peak in matching):
            raise RuntimeError("risk baseline journal has conflicting day-start values")
        peak = max(value for _start, value in matching)
        return day_start, max(peak, equity), equity > peak

    def _record_baselines(self, account: Any, day: Any, equity: float,
                          day_start: float, peak: float) -> None:
        journal = getattr(self, "_journal", None)
        if journal is None:
            return
        journal.record(RISK_BASELINE_EVENT, {
            "account_login": int(account.login),
            "account_server": str(account.server),
            "server_day": day.isoformat(),
            "day_start_equity": float(day_start),
            "peak_equity": float(peak),
            "observed_equity": float(equity),
            "at": datetime.now(UTC).isoformat(),
            "broker_order_sent": False,
        })

    def _ensure_baselines(self) -> tuple[float, float]:
        account = self.account()
        equity = float(account.equity)
        if not math.isfinite(equity) or equity <= 0:
            raise RuntimeError("risk baseline equity is invalid")
        day = self._server_day()
        if self._day != day or self._day_start is None or self._peak is None:
            day_start, peak, needs_record = self._restore_baselines(account, day, equity)
            if needs_record:
                self._record_baselines(account, day, equity, day_start, peak)
            self._day = day
            self._day_start = day_start
            self._peak = peak
        elif equity > self._peak:
            # Persist the high-water mark before making it available to the risk gate. A
            # journal write failure therefore blocks the cycle instead of losing the peak.
            self._record_baselines(account, day, equity, self._day_start, equity)
            self._peak = equity
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
        # Every unusable row on this path is a refusal, not a skip. The agent reads the property
        # through a gate that blocks the cycle when it raises, so an unreadable day is one it
        # declines to trade; silently dropping the row would instead report a day that looks
        # better than it is and let the budget keep spending.
        total = 0.0
        for deal in rows:
            if int(deal.get("magic", 0)) != MAGIC_NUMBER:
                continue
            if int(deal.get("entry", -1)) != 1:  # DEAL_ENTRY_OUT
                continue
            moment = deal.get("time_epoch")
            if isinstance(moment, bool) or not isinstance(moment, (int, float)):
                raise RuntimeError("risk baseline deal row has no usable time")
            if moment < day_start_epoch:
                continue
            profit = _number(deal.get("profit"))
            if profit is None:
                raise RuntimeError("risk baseline deal profit is unreadable")
            total += profit
            for name in ("swap", "commission", "fee"):
                if name not in deal:
                    # Absent is a broker that does not report the column, which is ordinary.
                    continue
                value = _number(deal[name])
                if value is None:
                    raise RuntimeError(f"risk baseline deal {name} is unreadable")
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


KillSwitchRead = Callable[[], Mapping[str, Any]]


def stop_is_armed(read: KillSwitchRead | None) -> bool:
    """The durable emergency stop both this process and the EA read.

    Anything other than a provably inactive switch blocks: a missing reader, a raising
    one, or a payload without a boolean flag cannot be shown to be safe, and the EA fails
    closed on the same read.
    """
    if read is None:
        return True
    try:
        state = read()
    except Exception:
        return True
    return not isinstance(state, Mapping) or state.get("kill_switch_active") is not False


class DemoExecutor:
    """One approved proposal -> one command -> one observed or unknown broker outcome."""

    def __init__(self, *, queue: Any, journal: DecisionJournal, terminal: Any,
                 policy: AgentPolicy, language: str, run_id: str,
                 post_approval_gates: PostApprovalGates,
                 kill_switch: KillSwitchRead,
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
        self._gates = post_approval_gates
        self._kill_switch = kill_switch
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
        # The command this executor handed to the bridge and has not seen an outcome for.
        # From the moment a command is in the queue the EA can take it, and a process that
        # dies before the fill is known cannot tell an order the broker took from one it did
        # not -- only the deals say, and they say later. The runner reads this on the way out.
        self.pending_send: Mapping[str, Any] | None = None

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
    def _stop_refusal(self) -> Outcome:
        self._say(text("runner.refused", self._language,
                       reason=describe(codes.KILL_SWITCH_ACTIVE, self._language)))
        return self._outcome(STATE_REFUSED, reason=codes.KILL_SWITCH_ACTIVE,
                             broker_order_sent=False)

    def offer(self, proposal: Mapping[str, Any], sizing: Mapping[str, Any]) -> Outcome:
        now = self._now()
        if stop_is_armed(self._kill_switch):
            # Before anything else, and before any market read: the operator's stop outranks
            # every gate below because it is a decision, not a measurement.
            return self._stop_refusal()
        if str(proposal.get("symbol", "")).strip().upper() != self._symbol.strip().upper():
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
        proposed_volume = float(sizing["volume"])
        live, blockers = price_gates(proposal, tick, spec,
                                     max_slippage_points=self._max_slippage)
        blockers.extend(volume_gates(proposed_volume, spec, max_volume=self._max_volume))
        try:
            margin = spec.margin_requirement(proposed_volume, account.leverage, live)
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

        gate_blockers, gate_info = self._gates(proposal, sizing)
        blockers = tuple(sorted(set(gate_blockers)))
        fill_price = _number(gate_info.get("fill_price"))
        final_volume = _number(gate_info.get("final_volume"))
        if not blockers and (fill_price is None or final_volume is None):
            # A gate that passes without naming a price and a lot has handed the order builder
            # nothing to build from; that is a broken input, not a licence to reuse the
            # pre-approval quote the operator was shown.
            blockers = (codes.PROPOSAL_UNREADABLE,)
        if not blockers:
            # The demo cap and the broker's step are the runner's own promise, so they are
            # checked against the lot the agent finally sized, not the one the cycle proposed.
            blockers = tuple(sorted(set(volume_gates(final_volume, spec,
                                                     max_volume=self._max_volume))))
        if blockers:
            reason = ",".join(blockers)
            self._say(text("runner.refused", self._language,
                           reason=", ".join(describe(code, self._language) for code in blockers)))
            return self._outcome(STATE_REFUSED, reason=reason, broker_order_sent=False)

        if stop_is_armed(self._kill_switch):
            # The confirmation prompt and the gate run both take time, so a stop pulled while
            # the operator was answering still has to reach the queue. This is the last read
            # before anything can leave this process.
            return self._stop_refusal()
        command_id = uuid.uuid4().hex
        order_id = client_order_id_for(self._run_id, proposal, final_volume)
        command = build_command(
            command_id=command_id, client_order_id=order_id,
            permission_id=permission_id_for(self._run_id, self._policy.version),
            signal_id=signal_id_for(proposal), instrument=self._symbol,
            direction=str(proposal["direction"]), size=final_volume,
            expected_price=float(fill_price),
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
        self.pending_send = {"command_id": command_id, "client_order_id": order_id,
                             "symbol": self._symbol}
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
            # What the agent's post-approval gates measured at the moment of sending: the fill
            # the command prices from, the lot it re-sized to, and the risk that lot carries.
            "post_approval_gates": dict(gate_info),
            "broker_order_sent": False,
            "stage": "queued",
        })
        self._say(text("runner.queued", self._language, command_id=command_id,
                       direction=str(proposal["direction"]), symbol=str(proposal["symbol"]),
                       volume=f"{final_volume:.2f}"))

        if not self._wait_for_serve(command):
            # The bridge drops a command once its expiry passes and never hands it out again,
            # so an expired command cannot become an order: this is a known non-send.
            self.pending_send = None
            self._say(text("runner.expired", self._language))
            return self._outcome(STATE_EXPIRED, reason="COMMAND_EXPIRED",
                                 command_id=command_id, client_order_id=order_id,
                                 queued=True, broker_order_sent=False)
        self._journal.record(SERVED_EVENT, {
            "symbol": self._symbol, "command_id": command_id, "client_order_id": order_id,
            "broker_order_sent": None, "state": "SERVED"})
        outcome = self._await_fill(command=queued, command_id=command_id, order_id=order_id,
                                   commanded_at=now, point=spec.point)
        # Only a fill we actually read, or a timeout that reported itself, ends the uncertainty.
        self.pending_send = None
        return outcome

    def journal_interrupted_send(self) -> Outcome | None:
        """Book a send this process handed to the bridge and never saw the outcome of.

        Ctrl-C between the enqueue and the fill leaves the same question a timeout leaves: the
        EA may already have sent the order, and the process that would have read the deals is
        gone. The command is journalled as unconfirmed so the audit trail ends on the truth
        rather than on a `SERVED` with nothing after it.
        """
        send = self.pending_send
        if send is None:
            return None
        self.pending_send = None
        self._say(text("runner.interrupted_send", self._language,
                       command_id=str(send["command_id"]), symbol=str(send["symbol"])))
        return self._outcome(STATE_UNCONFIRMED, reason="INTERRUPTED_BEFORE_FILL_WAS_KNOWN",
                             command_id=str(send["command_id"]),
                             client_order_id=str(send["client_order_id"]),
                             stop_run=True, requires_reconciliation=True, queued=True,
                             broker_order_sent=None)

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

    def _await_fill(self, *, command: Mapping[str, Any], command_id: str, order_id: str,
                    commanded_at: datetime, point: float) -> Outcome:
        # Read a little history for the clock skew, but only accept a deal that postdates the
        # command: the window is how wide to look, the floor is what counts as an answer.
        since = commanded_at - timedelta(minutes=10)
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
                                    symbol=self._symbol, not_before=commanded_at)
            if deal is not None:
                mismatches = deal_mismatches(deal, command, point=point,
                                             max_slippage_points=self._max_slippage)
                if mismatches:
                    # The comment proves the EA wrote this deal, not that it is the order we
                    # asked for. A different lot or a price outside the approved slippage is a
                    # position on the account this run cannot describe, so nothing here claims
                    # it as a fill: the operator reconciles it with the broker.
                    self._say(text("runner.fill_mismatch", self._language,
                                   ticket=deal.get("ticket"),
                                   reasons=", ".join(mismatches)))
                    return self._outcome(STATE_UNCONFIRMED, reason=",".join(mismatches),
                                         command_id=command_id, client_order_id=order_id,
                                         deal=deal, stop_run=True,
                                         requires_reconciliation=True, queued=True,
                                         broker_order_sent=None)
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
    # No --bridge-token: a secret in argv is readable by anyone who can see a process list,
    # and stays in the shell history the operator pasted it into. MT5_BRIDGE_TOKEN only.
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
    parser.add_argument("--release-kill-switch", action="store_true",
                        help=text("cli.release_kill_switch", language))
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


def _set_stop(switch: KillSwitchStore, *, language: str, reason: str,
              actor: str = "runner", active: bool = True) -> None:
    """Pull or release the same durable stop an operator can, so it outlives this process."""
    try:
        switch.record(active=active, actor_id=actor, reason=reason, now=datetime.now(UTC))
    except (BridgeError, OSError) as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        print(text("runner.kill_switch_state_lost", language, error=str(code)), file=sys.stderr)


def _release_kill_switch(args: argparse.Namespace, language: str, token: str) -> int:
    """Lift a persisted stop outside a run, guarded by the separate admin secret.

    The bridge only answers while a runner is alive, so an operator who armed the stop and
    then stopped the process would otherwise have one way left: editing JSON by hand. This
    is that way, with the admin secret taken from the environment like every other
    credential here.
    """
    path = kill_switch_state_path(args.tombstones)
    try:
        switch = KillSwitchStore(path)
    except BridgeError as exc:
        print(text("out.error", language, reason=f"{exc.code} ({path})"), file=sys.stderr)
        return 2
    admin = os.environ.get(ADMIN_TOKEN_ENV, "")
    # A command line carries no header, so here the environment value is both the configured
    # admin token and the proof that it was offered; the reuse and length rules still apply.
    refusal = release_refusal(admin_token=admin, bridge_token=token, supplied=admin)
    if refusal:
        print(text("runner.kill_switch_release_refused", language, reason=refusal),
              file=sys.stderr)
        return 2
    try:
        record = switch.record(active=False, actor_id=os.environ.get("USERNAME", "operator"),
                               reason="released from the command line",
                               now=datetime.now(UTC))
    except (BridgeError, OSError) as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        print(text("runner.kill_switch_release_refused", language, reason=str(code)),
              file=sys.stderr)
        return 2
    print(text("runner.kill_switch_released", language, file=str(path),
               updated_at=record["updated_at"]), file=sys.stderr)
    return 0


def _run(args: argparse.Namespace, policy: AgentPolicy, language: str, token: str,
         run_id: str, terminal: DemoTerminal,
         symbols: list[str] | None = None) -> int:
    bridge: DemoBridge | None = None
    journal: DecisionJournal | None = None
    switch_path = kill_switch_state_path(args.tombstones)
    try:
        switch = KillSwitchStore(switch_path)
    except BridgeError as exc:
        print(text("out.error", language, reason=f"{exc.code} ({switch_path})"), file=sys.stderr)
        return 2
    if switch.read() is None:
        # No durable record exists for this path yet, so this run leaves one: the state file
        # is what makes a later deletion a corruption rather than a fresh start. It is seeded
        # ARMED and never from the policy's own flag, because a new process cannot tell a path
        # that was never used from one whose stop an operator deleted, and `active: false` in a
        # policy is an absence of intent to halt, not a grant of permission to trade. Lifting
        # the seeded stop takes the separate admin secret, like any other release.
        _set_stop(switch, language=language, actor="policy", active=True,
                  reason=str(policy.kill_switch_reason if policy.kill_switch_active
                             else SEEDED_REASON))
        if not policy.kill_switch_active:
            print(text("runner.kill_switch_seeded", language, file=str(switch_path),
                       reason=SEEDED_REASON), file=sys.stderr)
    try:
        account = terminal.account()
        if not account.is_demo:
            print(text("runner.not_demo", language), file=sys.stderr)
            return 2
        if policy.expected_login is None or policy.expected_server is None:
            # The order goes to whichever account this terminal happens to be signed into, so
            # the policy has to name it. A comparison against nothing passes for every account
            # on earth: the demo flag says only how the account is funded, and a broker-side
            # or operator-side change of login would otherwise be silently traded on.
            print(text("runner.identity_unbound", language), file=sys.stderr)
            return 2
        if account.login != policy.expected_login or account.server != policy.expected_server:
            print(text("runner.identity_mismatch", language, login=account.login,
                       server=account.server), file=sys.stderr)
            return 2

        journal = DecisionJournal(args.db, run_id=run_id)
        terminal.attach_journal(journal)
        try:
            bridge = DemoBridge(token=token, tombstones_path=args.tombstones,
                                kill_switch_store=switch, host="127.0.0.1",
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
        # Retail MT5 servers rarely list XAUUSD under that name — Exness, RoboForex and ITAM
        # append an account-type suffix — so the requested names are resolved against the
        # server's own list once, up front, and the resolved name is what every later call,
        # proposal and order carries. A failed resolution falls back to the requested name
        # rather than dropping the instrument: symbol_spec still refuses it with its own,
        # better reason.
        listed: list[str] | None = None
        read_list = getattr(terminal, "symbols", None)
        if callable(read_list):
            try:
                listed = list(read_list())
            except Exception as exc:
                print(text("runner.symbol_unavailable", language, symbol="*",
                           reason=skip_reason(exc)), file=sys.stderr)
        specs: dict[str, Any] = {}
        unavailable: dict[str, str] = {}
        aliases: dict[str, str] = {}
        resolved_symbols: list[str] = []
        for symbol in symbols:
            requested = symbol
            if listed is not None:
                try:
                    symbol = resolve_broker_symbol(requested, listed)
                except Exception:
                    symbol = requested
            if symbol != requested:
                aliases[requested] = symbol
                journal.record(SYMBOL_ALIAS_EVENT, {
                    "requested": requested, "resolved": symbol, "broker_order_sent": False})
                print(text("runner.symbol_aliased", language, requested=requested,
                           resolved=symbol), file=sys.stderr)
            resolved_symbols.append(symbol)
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
        if aliases and callable(getattr(terminal, "use_broker_names", None)):
            # Every later read goes through the adapter under the internal upper-case key, so
            # it has to know the server's own spelling to ask for the instrument at all.
            terminal.use_broker_names({requested.upper(): exact
                                       for requested, exact in aliases.items()})
        active = [symbol for symbol in resolved_symbols if symbol in specs]
        if not active:
            print(text("runner.no_symbols", language), file=sys.stderr)
            return 3

        operator_state = ApprovalState()
        agents: dict[str, tuple[MT5Agent, DemoExecutor]] = {}
        for symbol in active:
            # A live run takes contract data from the live terminal for every instrument,
            # including the policy's own symbol: the declared spec is a record of what was
            # measured once, and a stale or broker-inconsistent number in it silently mis-sizes
            # real orders. Measured here: MetaQuotes-Demo's XAUUSD tick fields understate a
            # lot's cash per price unit tenfold, so sizing from the declaration sent 0.04 lots
            # where the risk budget allowed 0.01. The declaration still serves offline callers.
            per_policy = replace(policy, symbol=symbol, symbol_spec=None)
            approver = None
            if per_policy.execution_mode == "confirm":
                approver = _make_approver(language=language, state=operator_state,
                                          digits=specs[symbol].digits, out=sys.stderr,
                                          source=sys.stdin)
            agent = MT5Agent(backend=terminal, policy=per_policy, journal=journal)
            executor = DemoExecutor(queue=bridge, journal=journal, terminal=terminal,
                                    policy=per_policy, language=language, run_id=run_id,
                                    post_approval_gates=agent.post_approval_gates,
                                    kill_switch=switch.payload,
                                    symbol=symbol, ttl_seconds=args.ttl_seconds,
                                    fill_timeout_seconds=args.fill_timeout,
                                    max_volume=args.max_volume, approver=approver,
                                    operator_state=operator_state, out=sys.stderr)
            agents[symbol] = (agent, executor)
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
        if stop_is_armed(switch.payload):
            print(text("runner.kill_switch_armed", language,
                       reason=str(switch.payload().get("kill_switch_reason") or "")),
                  file=sys.stderr)

        statuses = (codes.STATUS_BLOCKED, codes.STATUS_NO_SIGNAL, codes.STATUS_OBSERVED,
                    codes.STATUS_PAPER_OPENED)
        counts = {symbol: {status: 0 for status in statuses} for symbol in active}
        tallies = {symbol: {"signals": 0, "commands": 0, STATE_FILLED: 0, STATE_EXPIRED: 0,
                            STATE_UNCONFIRMED: 0, STATE_REFUSED: 0, STATE_DUPLICATE: 0}
                   for symbol in active}
        passes = 0
        halted = False
        halted_by = ""
        interrupted = False
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
                            # Durable, not a variable: a halt over an unknown send must still
                            # block the EA after this process exits and the next one starts.
                            _set_stop(switch, language=language,
                                      reason=f"{symbol}:{outcome.state}")
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
            for symbol, (_agent, executor) in agents.items():
                outcome = executor.journal_interrupted_send()
                if outcome is None:
                    continue
                # A command the EA could still take is an unknown send, and an unknown send
                # outlives this process: the stop has to be durable and the exit code has to
                # say that the operator's job is not finished.
                tally = tallies[symbol]
                tally["commands"] += 1
                tally[STATE_UNCONFIRMED] += 1
                _set_stop(switch, language=language, reason=f"{symbol}:{STATE_UNCONFIRMED}")
                halted = True
                halted_by = symbol
                interrupted = True
                print(text("runner.stopped_uncertain", language), file=sys.stderr)

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
            "interrupted_with_send_in_flight": interrupted,
            "kill_switch_armed": stop_is_armed(switch.payload),
            "kill_switch_reason": str(switch.payload().get("kill_switch_reason") or ""),
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
        # A Ctrl-C that leaves a live command behind is not a clean finish: the caller has to
        # reconcile with the broker before trusting this journal again.
        return 4 if interrupted else 0
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
        args.tombstones = str(resolve_state_path(args.tombstones))
        if not policy.require_demo or policy.mode not in ("observe", "paper"):
            raise ValueError(text("runner.policy_gate", language))
        if args.execution is not None:
            policy = replace(policy, execution_mode=args.execution, ui_language=language)
        else:
            policy = replace(policy, ui_language=language)
        symbols = parse_symbols(args.symbols, language=language) or [policy.symbol]
        if len(symbols) > 50:
            raise ValueError(text("err.symbols", language, value=f"{len(symbols)} symbols"))
        token = os.environ.get(BRIDGE_TOKEN_ENV)
        try:
            token = validate_token(token)
        except BridgeError:
            raise ValueError(text("runner.bridge_token_missing", language)) from None
    except (ValueError, OSError) as exc:
        print(text("out.error", language, reason=str(exc)), file=sys.stderr)
        return 2

    run_id = args.run_id or default_run_id_for(args.tombstones, args.policy)
    if args.release_kill_switch:
        # Lifting a stop is not a trade: no terminal, no journal, no bridge, no cycle runs.
        return _release_kill_switch(args, language, token)
    try:
        terminal = DemoTerminal(server_utc_offset_seconds=policy.server_utc_offset_seconds,
                                terminal_path=args.terminal_path, timeout_ms=args.timeout_ms,
                                out=sys.stderr, language=language)
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
