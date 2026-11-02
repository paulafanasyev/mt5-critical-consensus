"""The MT5 agent loop: gather, decide, journal — and never send a broker order."""
from __future__ import annotations

import inspect
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from ..market_data import MarketBar
from . import codes
from .backend import (AccountInfo, MT5Backend, PositionSnapshot, SymbolSpec, Tick,
                       assert_backend_contract)
from .journal import OWN_LEG_EVENT_TYPE, DecisionJournal
from .policy import AgentPolicy
from .sizing import SizedOrder, size_position
from .strategy import Proposal, evaluate_signal

UTC = timezone.utc
# A baseline more than this many times the current equity is not a drawdown, it is a broken
# number; treating it as a real peak would either block every cycle or clear every gate.
_MAX_BASELINE_MULTIPLE = 1_000.0
# How far the fill may drift from the entry the operator approved, as a fraction of the
# approved stop distance. Beyond this the approval was for a different trade: the operator
# saw one risk/reward and would get another, so the cycle is refused instead of filled.
_MAX_REQUOTE_SLIP_FRACTION = 0.10

# How long a leg this run opened stays evidence of churn. Two hours is long enough to see a
# neighbour on a shared demo flatten the same symbol several times in a row, and short enough
# that ordinary play on an H1 signal - one leg, its stop, tomorrow's entry - never adds up to it.
_CHURN_WINDOW = timedelta(hours=2)
# Rows the churn gate reads out of the journal at a time. One row is written per distinct ticket,
# so this covers days of a seven-symbol book on a 20-second cycle.
_CHURN_SCAN_ROWS = 4_000

# Called with the journaled proposal and sizing dicts; only a literal True opens the position.
Approver = Callable[[Mapping[str, Any], Mapping[str, Any]], Any]


@dataclass(frozen=True)
class CycleResult:
    status: str
    blockers: tuple[str, ...]
    proposal: dict[str, Any] | None
    sizing: dict[str, Any] | None
    ticket: int | None
    equity: float
    recorded_at: str
    decision_hash: str
    broker_order_sent: bool
    open_positions: int = 0
    equity_recorded: bool = False
    equity_record_error: str = ""
    execution_mode: str = "confirm"
    cycle_recorded: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "blockers": list(self.blockers),
                "proposal": self.proposal, "sizing": self.sizing, "ticket": self.ticket,
                "equity": self.equity, "recorded_at": self.recorded_at,
                "decision_hash": self.decision_hash, "broker_order_sent": self.broker_order_sent,
                "open_positions": self.open_positions,
                "equity_recorded": self.equity_recorded,
                "equity_record_error": self.equity_record_error,
                "execution_mode": self.execution_mode,
                "cycle_recorded": self.cycle_recorded}


def trade_event_payload(trade: Any, *, execution_mode: str) -> dict[str, Any]:
    """Journal payload for one simulated paper fill; it can never represent a broker order."""
    payload = trade.as_dict()
    payload.update({"broker_order_sent": False, "execution_mode": execution_mode})
    return payload


def _number(value: Any) -> float | None:
    """Finite, non-boolean number or ``None`` — never a silent coercion of a broken field."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _equity_limits(backend: MT5Backend, equity: float) -> tuple[float, float] | None:
    try:
        day_start = _number(getattr(backend, "day_start_equity", None))
        peak = _number(getattr(backend, "peak_equity", None))
    except Exception:  # a backend property that raises is a missing baseline, not a crash
        return None
    if day_start is None or peak is None:
        return None
    if day_start <= 0 or peak <= 0:
        return None
    # A peak below the current equity or below the day's opening equity is not a peak; the
    # drawdown gate would then read a negative distance and never fire. Refuse the baselines.
    if peak < max(day_start, equity):
        return None
    if max(day_start, peak) > equity * _MAX_BASELINE_MULTIPLE:
        return None
    return day_start, peak


def _realized_daily_loss(backend: MT5Backend) -> float | None:
    """Loss already locked in today, or ``None`` when the backend cannot report it.

    ``MT5Backend`` requires ``realized_daily_pnl``. A missing attribute, a property that
    raises, or a non-numeric/bool/NaN value is unusable data for the daily gate: the cycle
    blocks with ``EQUITY_BASELINES_UNAVAILABLE`` instead of treating an unknown day as
    loss-free.

    The descriptor is resolved statically and invoked directly. Plain ``getattr`` would
    swallow an ``AttributeError`` raised inside a property and even fall back to a
    ``__getattr__`` hook, so a broken day ledger could read as "no attribute" or as some
    unrelated value instead of blocking.
    """
    try:
        descriptor = inspect.getattr_static(backend, "realized_daily_pnl")
    except AttributeError:
        return None
    try:
        raw = descriptor.__get__(backend, type(backend)) \
            if hasattr(descriptor, "__get__") else descriptor
    except Exception:
        return None
    value = _number(raw)
    if value is None:
        return None
    return -value if value < 0 else 0.0


def _iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _proposal_from_dict(raw: Mapping[str, Any]) -> Proposal | None:
    """Rebuild the proposal its own cycle journaled; a missing field is a broken input."""
    symbol, timeframe, direction = (raw.get(key) for key in ("symbol", "timeframe", "direction"))
    if not isinstance(symbol, str) or not isinstance(timeframe, str) \
            or direction not in ("BUY", "SELL"):
        return None
    evaluated_at = _iso_datetime(raw.get("evaluated_at"))
    prices = [_number(raw.get(key)) for key in ("entry_price", "stop_loss", "take_profit",
                                               "risk_distance", "reward_distance", "risk_reward",
                                               "atr", "confidence")]
    reasons = raw.get("reasons")
    if evaluated_at is None or any(price is None for price in prices) \
            or not isinstance(reasons, (list, tuple)) \
            or not all(isinstance(reason, str) for reason in reasons):
        return None
    return Proposal(symbol=symbol, timeframe=timeframe, direction=direction,
                    evaluated_at=evaluated_at, entry_price=prices[0], stop_loss=prices[1],
                    take_profit=prices[2], risk_distance=prices[3], reward_distance=prices[4],
                    risk_reward=prices[5], atr=prices[6], confidence=prices[7],
                    reasons=tuple(reasons))


def _sized_order_from_dict(raw: Mapping[str, Any]) -> SizedOrder | None:
    fields = [_number(raw.get(key)) for key in ("volume", "risk_value", "risk_fraction",
                                                "risk_per_lot", "margin_required")]
    if any(field is None for field in fields) or fields[0] <= 0:
        return None
    return SizedOrder(*[float(field) for field in fields])


def _fill_geometry_blockers(trade: Proposal, fill: float, spec: SymbolSpec) -> tuple[str, ...]:
    """At the price the broker would actually fill at, the order must still be executable."""
    blockers: list[str] = []
    if trade.direction == "BUY" and not trade.stop_loss < fill < trade.take_profit:
        blockers.append(codes.BUY_GEOMETRY_INVALID)
    elif trade.direction == "SELL" and not trade.take_profit < fill < trade.stop_loss:
        blockers.append(codes.SELL_GEOMETRY_INVALID)
    stops = spec.stops_level_points
    if stops:
        if abs(fill - trade.stop_loss) / spec.point < stops:
            blockers.append(codes.STOP_INSIDE_BROKER_STOPS_LEVEL)
        if abs(trade.take_profit - fill) / spec.point < stops:
            blockers.append(codes.TARGET_INSIDE_BROKER_STOPS_LEVEL)
    return tuple(blockers)


def _stop_distance(position: PositionSnapshot) -> float | None:
    """How far price must travel against this position before its stop is taken, or ``None``.

    Pure geometry, so it needs no symbol data: an unset stop, or one on the profitable side of
    entry, is not a measurable loss however the instrument is quoted or whichever currency it
    pays profit in.
    """
    if position.stop_loss <= 0:
        return None
    distance = (position.entry_price - position.stop_loss) \
        if position.direction == "BUY" else (position.stop_loss - position.entry_price)
    return distance if distance > 0 else None


def _position_stop_risk(spec: SymbolSpec, position: PositionSnapshot) -> float | None:
    """Account-currency loss if this position's stop is taken, or ``None`` when unmeasurable."""
    distance = _stop_distance(position)
    if distance is None:
        return None
    return distance * spec.cash_per_price_unit * position.volume


class MT5Agent:
    """One cycle = read terminal state, apply fail-closed checks, journal the decision.

    ``mode="paper"`` records a simulated fill on a local paper broker. Neither mode
    can place a real order: no code path in this package reaches a trading API.
    """

    def __init__(self, *, backend: MT5Backend, policy: AgentPolicy,
                 journal: DecisionJournal | None = None,
                 clock: Callable[[], datetime] | None = None,
                 approver: Approver | None = None) -> None:
        assert_backend_contract(backend)
        self._backend = backend
        self._policy = policy
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(UTC))
        self._approver = approver
        # Symbol specifications are read once per cycle; open positions on other symbols need
        # their own spec before their stop risk can be added to the day's exposure.
        self._spec_cache: dict[str, SymbolSpec] = {}
        # Whether the closing equity point was written, so the CLI can report it instead of
        # silently dropping the row that the run's final drawdown depends on.
        self.closing_equity_recorded = False
        self.closing_equity_error = ""
        # Tickets seen open on this symbol, with the moment each was first seen. The durable copy
        # of this lives in the journal; the local one is what the gate still has when a row could
        # not be written.
        self._churn_observations: dict[int, datetime] = {}

    @property
    def policy(self) -> AgentPolicy:
        return self._policy

    def run_cycle(self, *, open_paper_position: bool = True) -> CycleResult:
        policy = self._policy
        # Tick values and stops levels move between cycles; a cached spec is only valid for the
        # cycle that read it.
        self._spec_cache.clear()
        blockers: list[str] = []
        moment = self._clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            return self._finish(codes.STATUS_BLOCKED, (codes.CLOCK_NOT_UTC_AWARE,), None, None,
                                None, moment)
        moment = moment.astimezone(UTC)

        if policy.kill_switch_active:
            return self._finish(codes.STATUS_BLOCKED, (codes.KILL_SWITCH_ACTIVE,), None, None,
                                None, moment,
                                extra={"kill_switch_reason": policy.kill_switch_reason})

        account = self._read_account(moment)
        if isinstance(account, CycleResult):
            return account

        blockers.extend(self._identity_blockers(account))
        if blockers:
            return self._finish(codes.STATUS_BLOCKED, tuple(blockers), None, None, None, moment,
                                equity=account.equity)

        limits = _equity_limits(self._backend, account.equity)
        if limits is None:
            return self._finish(codes.STATUS_BLOCKED, (codes.EQUITY_BASELINES_UNAVAILABLE,), None,
                                None, None, moment, equity=account.equity)
        day_start, peak = limits
        daily_budget = day_start * policy.risk.max_daily_loss_fraction
        equity = account.equity
        # Caps are inclusive: equity sitting exactly on the line has spent the allowance.
        if (day_start - equity) >= daily_budget:
            blockers.append(codes.DAILY_LOSS_LIMIT_REACHED)
        realized_loss_value = _realized_daily_loss(self._backend)
        if realized_loss_value is None:
            # A present-but-unusable daily P&L is broken data, not "no loss today": refusing
            # the baseline is the only way the daily budget stays meaningful.
            return self._finish(codes.STATUS_BLOCKED, (codes.EQUITY_BASELINES_UNAVAILABLE,),
                                None, None, None, moment, equity=account.equity)
        # Banked losses and the loss still open in stops are both spending the same daily
        # allowance: a day that banked half the budget cannot also reserve all of it again.
        realized_loss = realized_loss_value
        if realized_loss >= daily_budget:
            blockers.append(codes.REALIZED_DAILY_LOSS_LIMIT_REACHED)
        if (peak - equity) >= peak * policy.risk.max_drawdown_fraction:
            blockers.append(codes.DRAWDOWN_LIMIT_REACHED)

        if not policy.window.contains(moment):
            blockers.append(codes.OUTSIDE_TRADING_WINDOW)
        blackout = policy.blackout_blocker(moment)
        if blackout:
            blockers.append(blackout)
        if blockers:
            return self._finish(codes.STATUS_BLOCKED, tuple(sorted(set(blockers))), None, None,
                                None, moment, equity=equity)

        positions = self._read_positions(moment)
        if isinstance(positions, CycleResult):
            return positions
        assert positions is not None
        # Recorded before the caps can end the cycle: a leg this run holds is exactly what the
        # cap gate reports back on, and the churn ledger would never see it.
        self._record_own_legs(positions, moment)
        cap_blockers = self._position_cap_blockers(positions)
        if cap_blockers:
            return self._finish(codes.STATUS_BLOCKED, cap_blockers, None, None, None, moment,
                                equity=equity, open_positions=len(positions))
        churn_blockers = self._churn_blockers(moment)
        if churn_blockers:
            return self._finish(codes.STATUS_BLOCKED, churn_blockers, None, None, None, moment,
                                equity=equity, open_positions=len(positions))

        market = self._read_market_data(moment)
        if isinstance(market, CycleResult):
            return market
        spec, tick, bars = market

        risk_blockers = self._open_book_blockers(positions, equity=equity,
                                                 daily_budget=daily_budget,
                                                 realized_loss=realized_loss)
        if risk_blockers:
            return self._finish(codes.STATUS_BLOCKED, risk_blockers, None, None, None, moment,
                                equity=equity, open_positions=len(positions))
        open_stop_risk = self._aggregate_stop_risk(positions)

        evaluation = evaluate_signal(bars, spec, tick, policy.strategy,
                                     max_spread_points=policy.risk.max_spread_points,
                                     expected_timeframe=policy.timeframe)
        if not evaluation.has_proposal:
            status = codes.STATUS_BLOCKED if evaluation.data_integrity_failure \
                else codes.STATUS_NO_SIGNAL
            return self._finish(status, evaluation.blockers, None, None, None, moment,
                                equity=equity, open_positions=len(positions),
                                extra={"features": evaluation.features})

        proposal = evaluation.proposal
        assert proposal is not None
        if proposal.risk_reward < policy.risk.min_risk_reward:
            return self._finish(codes.STATUS_BLOCKED, (codes.RISK_REWARD_BELOW_MINIMUM,),
                                proposal.as_dict(), None, None, moment, equity=equity,
                                open_positions=len(positions))

        sizing = size_position(spec=spec, equity=min(equity, account.balance),
                               risk_fraction_limit=policy.risk.risk_per_trade_fraction,
                               stop_distance=proposal.risk_distance, leverage=account.leverage,
                               margin_available=account.margin_free,
                               price=tick.reference[proposal.direction],
                               max_notional=min(equity, account.balance)
                               * policy.risk.max_notional_fraction,
                               max_volume=policy.risk.max_volume_lots)
        if not sizing.accepted:
            return self._finish(codes.STATUS_BLOCKED, (sizing.denial,), proposal.as_dict(), None,
                                None, moment, equity=equity, open_positions=len(positions))
        order = sizing.order
        assert order is not None
        if realized_loss + open_stop_risk + order.risk_value > daily_budget:
            return self._finish(codes.STATUS_BLOCKED,
                                (codes.AGGREGATE_STOP_RISK_LIMIT_REACHED,), proposal.as_dict(),
                                order.as_dict(), None, moment, equity=equity,
                                open_positions=len(positions),
                                extra={"open_stop_risk": open_stop_risk,
                                       "realized_daily_loss": realized_loss,
                                       "daily_budget": daily_budget})

        if policy.mode == "observe" or not open_paper_position:
            return self._finish(codes.STATUS_OBSERVED, (), proposal.as_dict(), order.as_dict(),
                                None, moment, equity=equity, open_positions=len(positions))
        declined = self._confirmation_blockers(
            proposal.as_dict(), {**order.as_dict(), "equity": min(equity, account.balance)})
        if declined:
            return self._finish(codes.STATUS_BLOCKED, declined, proposal.as_dict(),
                                order.as_dict(), None, moment, equity=equity,
                                open_positions=len(positions))
        # The operator may have deliberated for minutes; the quote that triggered the signal is
        # not necessarily the one a fill would get. Re-read it and refuse a moved market.
        requote_blockers, fresh_tick, fill_spec = self._requote(moment)
        if requote_blockers:
            return self._finish(codes.STATUS_BLOCKED, requote_blockers, proposal.as_dict(),
                                order.as_dict(), None, moment, equity=equity,
                                open_positions=len(positions))
        assert fresh_tick is not None and fill_spec is not None
        # The approval was for the entry and risk the operator saw. Re-bound both against the
        # fill actually available, then size on the fill price and the freshly read spec.
        fill_blockers, final_order, requote_info = self._bound_fill(
            proposal=proposal, approved=order, tick=fresh_tick, spec=fill_spec,
            risk_equity=min(equity, account.balance), leverage=account.leverage,
            margin_available=account.margin_free, open_stop_risk=open_stop_risk,
            daily_budget=daily_budget, realized_loss=realized_loss)
        if fill_blockers:
            return self._finish(codes.STATUS_BLOCKED, fill_blockers, proposal.as_dict(),
                                order.as_dict(), None, moment, equity=equity,
                                open_positions=len(positions),
                                extra={"requote": requote_info})
        return self._open_paper_position(proposal=proposal, order=final_order, tick=fresh_tick,
                                         moment=moment, equity=equity,
                                         open_positions=len(positions),
                                         extra={"requote": requote_info})

    def post_approval_gates(self, proposal: Mapping[str, Any],
                            sizing: Mapping[str, Any]) -> tuple[tuple[str, ...], dict[str, Any]]:
        """Re-run every gate on a trade an external executor is about to send.

        ``run_cycle`` stops at ``STATUS_OBSERVED`` for a policy that never opens a paper
        position, so the requote and re-binding a paper fill gets would otherwise never reach
        a broker order. This is the same check, called with the proposal and sizing dicts the
        cycle just handed out: it re-reads the terminal, refuses a moved or unaffordable
        market, and returns the price and volume the command may carry. It journals nothing —
        the executor owns the audit trail of its own command.

        ``({}, info)`` means the trade is still valid at ``info["fill_price"]`` for
        ``info["final_volume"]``; a non-empty blocker tuple means it must not be sent.
        """
        policy = self._policy
        # Tick values and stops levels move between reads; a cached spec is only valid for the
        # cycle that read it.
        self._spec_cache.clear()
        moment = self._clock()
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            return (codes.CLOCK_NOT_UTC_AWARE,), {}
        moment = moment.astimezone(UTC)
        if policy.kill_switch_active:
            return (codes.KILL_SWITCH_ACTIVE,), {}

        trade = _proposal_from_dict(proposal)
        approved = _sized_order_from_dict(sizing)
        if trade is None or approved is None:
            return (codes.PROPOSAL_UNREADABLE,), {}
        if trade.symbol != policy.symbol:
            return (codes.PROPOSAL_SYMBOL_MISMATCH,), {}

        blockers: list[str] = []
        account, code, _exc = self._account_read()
        if account is None:
            return (code,), {}
        blockers.extend(self._identity_blockers(account))

        equity = account.equity
        limits = _equity_limits(self._backend, equity)
        if limits is None:
            return (codes.EQUITY_BASELINES_UNAVAILABLE,), {}
        day_start, peak = limits
        daily_budget = day_start * policy.risk.max_daily_loss_fraction
        if (day_start - equity) >= daily_budget:
            blockers.append(codes.DAILY_LOSS_LIMIT_REACHED)
        realized_loss = _realized_daily_loss(self._backend)
        if realized_loss is None:
            return (codes.EQUITY_BASELINES_UNAVAILABLE,), {}
        if realized_loss >= daily_budget:
            blockers.append(codes.REALIZED_DAILY_LOSS_LIMIT_REACHED)
        if (peak - equity) >= peak * policy.risk.max_drawdown_fraction:
            blockers.append(codes.DRAWDOWN_LIMIT_REACHED)
        if not policy.window.contains(moment):
            blockers.append(codes.OUTSIDE_TRADING_WINDOW)
        blackout = policy.blackout_blocker(moment)
        if blackout:
            blockers.append(blackout)
        if blockers:
            return (tuple(sorted(set(blockers))), {})

        positions, code, _exc = self._positions_read()
        if positions is None:
            return (code,), {}
        cap_blockers = self._position_cap_blockers(positions)
        if cap_blockers:
            return (cap_blockers, {"open_positions": len(positions)})

        requote_blockers, fresh_tick, fill_spec = self._requote(moment)
        if requote_blockers:
            return requote_blockers, {}
        assert fresh_tick is not None and fill_spec is not None

        risk_blockers = self._open_book_blockers(positions, equity=equity,
                                                 daily_budget=daily_budget,
                                                 realized_loss=realized_loss)
        if risk_blockers:
            return (risk_blockers, {"open_positions": len(positions)})

        fill_blockers, final_order, info = self._bound_fill(
            proposal=trade, approved=approved, tick=fresh_tick, spec=fill_spec,
            risk_equity=min(equity, account.balance), leverage=account.leverage,
            margin_available=account.margin_free,
            open_stop_risk=self._aggregate_stop_risk(positions), daily_budget=daily_budget,
            realized_loss=realized_loss)
        info["open_positions"] = len(positions)
        if fill_blockers:
            return (fill_blockers, info)
        assert final_order is not None
        geometry_blockers = _fill_geometry_blockers(trade, float(info["fill_price"]), fill_spec)
        if geometry_blockers:
            return (geometry_blockers, info)
        info["final_risk_fraction"] = final_order.risk_fraction
        return ((), info)

    # ------------------------------------------------------------------ helpers
    def _finish_blocked(self, moment: datetime, code: str, exc: Exception,
                        equity: float | None = None) -> CycleResult:
        return self._finish(codes.STATUS_BLOCKED, (code,), None, None, None, moment,
                            equity=equity, extra={"error_kind": type(exc).__name__})

    def _account_read(self) -> tuple[AccountInfo | None, str, Exception | None]:
        try:
            account = self._backend.account()
        except Exception as exc:  # a terminal that will not answer must not trade
            return None, codes.ACCOUNT_READ_FAILED, exc
        if not isinstance(account, AccountInfo):
            # A backend that answers with None or a dict is broken, not flat: journal the
            # failure instead of crashing on the first attribute read.
            return None, codes.ACCOUNT_READ_FAILED, \
                TypeError("backend.account() must return AccountInfo")
        return account, "", None

    def _read_account(self, moment: datetime) -> AccountInfo | CycleResult:
        account, code, exc = self._account_read()
        if account is None:
            return self._finish_blocked(moment, code, exc)
        return account

    def _identity_blockers(self, account: AccountInfo) -> list[str]:
        policy = self._policy
        blockers: list[str] = []
        if policy.require_demo and not account.is_demo:
            blockers.append(codes.ACCOUNT_NOT_DEMO)
        if policy.expected_login is not None and account.login != policy.expected_login:
            blockers.append(codes.ACCOUNT_LOGIN_MISMATCH)
        if policy.expected_server is not None and account.server != policy.expected_server:
            blockers.append(codes.ACCOUNT_SERVER_MISMATCH)
        if _number(account.equity) is None or account.equity <= 0 \
                or _number(account.balance) is None or account.balance <= 0:
            blockers.append(codes.ACCOUNT_EQUITY_INVALID)
        if _number(account.margin_free) is None or account.margin_free <= 0:
            blockers.append(codes.ACCOUNT_MARGIN_INVALID)
        if isinstance(account.leverage, bool) or not isinstance(account.leverage, int) \
                or account.leverage < 1:
            blockers.append(codes.ACCOUNT_LEVERAGE_INVALID)
        return blockers

    def _positions_read(self) -> tuple[list[PositionSnapshot] | None, str, Exception | None]:
        try:
            positions = self._backend.positions()
        except Exception as exc:
            return None, codes.POSITION_READ_FAILED, exc
        if positions is None:
            # "The terminal did not answer" and "the account is flat" are different facts; only
            # the second one may release the position cap.
            return None, codes.POSITION_CAP_COUNT_UNKNOWN, None
        if not isinstance(positions, (list, tuple)):
            return None, codes.POSITION_READ_FAILED, \
                TypeError("backend.positions() must return a list")
        if not all(isinstance(position, PositionSnapshot) for position in positions):
            # Iterating a list of dicts would raise AttributeError after the cap was already
            # evaluated; a malformed position list is an unreadable book.
            return None, codes.POSITION_READ_FAILED, \
                TypeError("backend.positions() must return PositionSnapshot objects")
        return list(positions), "", None

    def _read_positions(self, moment: datetime) -> list[PositionSnapshot] | CycleResult | None:
        positions, code, exc = self._positions_read()
        if positions is None:
            if exc is not None:
                return self._finish_blocked(moment, code, exc)
            return self._finish(codes.STATUS_BLOCKED, (code,), None, None, None, moment)
        return positions

    def _ours_on_the_book(self, positions: list[PositionSnapshot]) -> list[PositionSnapshot]:
        """The positions this run opened, for the caps that mean "ours" not "the account".

        A policy with no magic owns the whole book. An unattributed row still counts: a reader
        that could not tell who opened it has not proved it was somebody else.
        """
        magic = self._policy.position_magic
        if magic is None:
            return positions
        return [position for position in positions
                if position.magic is None or position.magic == magic]

    def _position_cap_blockers(self, positions: list[PositionSnapshot]) -> tuple[str, ...]:
        cap = self._policy.risk.max_open_positions
        ours = self._ours_on_the_book(positions)
        blockers: list[str] = []
        if len(ours) > cap:
            blockers.append(codes.POSITION_CAP_EXCEEDED)
        elif len(ours) == cap:
            blockers.append(codes.POSITION_CAP_REACHED)
        if any(position.symbol == self._policy.symbol for position in ours):
            blockers.append(codes.SYMBOL_ALREADY_OPEN)
        return tuple(blockers)

    def _record_own_legs(self, positions: list[PositionSnapshot], moment: datetime) -> None:
        """Journal each of this symbol's legs the first time its ticket is seen.

        The ticket is the fact worth keeping: a position that opens and dies in twenty seconds
        leaves one row, and the rows are what the churn gate counts after the process that wrote
        them is gone. A row that cannot be written is not retried - the in-memory observation
        already happened, so the guard only loses its durability, never its current cycle.
        """
        if self._policy.risk.churn_max_legs_per_symbol is None or self._journal is None:
            return
        for position in positions:
            if position.ticket in self._churn_observations:
                continue
            if position.symbol != self._policy.symbol or not self._owns(position):
                continue
            self._churn_observations[position.ticket] = moment
            try:
                self._journal.record(OWN_LEG_EVENT_TYPE, {
                    "symbol": self._policy.symbol, "ticket": int(position.ticket),
                    "open_time": position.open_time.isoformat(), "seen_at": moment.isoformat(),
                    "execution_mode": self._policy.execution_mode, "broker_order_sent": False})
            except Exception:
                pass

    def _owns(self, position: PositionSnapshot) -> bool:
        magic = self._policy.position_magic
        return magic is None or position.magic is None or position.magic == magic

    def _churn_blockers(self, moment: datetime) -> tuple[str, ...]:
        """Stand this symbol down while its legs keep being reopened and dying.

        Measured on the shared demo: four FX legs opened 20 seconds apart were flattened by
        another process 21 seconds later, each for exactly the spread it had just paid, and the
        next cycle would have opened all four again. Reopening into a book somebody else is
        closing buys their exit forever, so the count of distinct recent tickets - durable rows
        plus what this process has seen - is what decides whether the signal is worth a lot.
        """
        limit = self._policy.risk.churn_max_legs_per_symbol
        if limit is None:
            return ()
        window_start = moment - _CHURN_WINDOW
        for ticket in [ticket for ticket, seen in self._churn_observations.items()
                       if seen < window_start]:
            del self._churn_observations[ticket]
        tickets = set(self._churn_observations)
        if self._journal is not None:
            for event in self._journal.events(limit=_CHURN_SCAN_ROWS,
                                              event_type=OWN_LEG_EVENT_TYPE):
                payload = event.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                if payload.get("symbol") != self._policy.symbol:
                    continue
                ticket = payload.get("ticket")
                if isinstance(ticket, bool) or not isinstance(ticket, int) or ticket <= 0:
                    continue
                # A row that declares its observation moment wrongly is counted, not skipped: a
                # ledger this run can no longer read is not evidence that the legs stopped dying.
                # Only a row that never carried one falls back to the time it was written.
                seen_at = payload.get("seen_at")
                seen = _iso_datetime(seen_at if seen_at is not None else event.get("timestamp"))
                if seen is None or seen >= window_start:
                    tickets.add(ticket)
        return (codes.LEG_CHURN_DETECTED,) if len(tickets) > limit else ()

    def _read_market_data(self, moment: datetime) -> tuple[SymbolSpec, Tick, list[MarketBar]] \
            | CycleResult:
        policy = self._policy
        try:
            spec = self._backend.symbol_spec(policy.symbol)
            tick = self._backend.tick(policy.symbol)
            bars = self._backend.bars(policy.symbol, policy.timeframe, policy.history_bars)
        except Exception as exc:
            return self._finish_blocked(moment, codes.MARKET_DATA_READ_FAILED, exc)
        if not isinstance(spec, SymbolSpec) or not isinstance(tick, Tick) \
                or not isinstance(bars, (list, tuple)) \
                or not all(isinstance(bar, MarketBar) for bar in bars):
            # A None, a dict or a stale object type is a read failure; touching .symbol on it
            # would escape as a traceback instead of a journaled BLOCKED cycle.
            return self._finish_blocked(moment, codes.MARKET_DATA_READ_FAILED,
                                        TypeError("backend returned malformed market data"))
        if spec.symbol != policy.symbol or tick.symbol != policy.symbol \
                or bars and bars[-1].symbol != policy.symbol:
            # The spec is what sizing and margin are computed from; if it came from another
            # instrument than the policy asked for, every downstream number is for the wrong
            # symbol, so the cycle must not reach the strategy or the paper book.
            return self._finish(codes.STATUS_BLOCKED, (codes.BAR_SYMBOL_MISMATCH,), None, None,
                                None, moment)
        age = (moment - tick.server_time).total_seconds()
        if age < -5:
            return self._finish(codes.STATUS_BLOCKED, (codes.QUOTE_FROM_THE_FUTURE,), None, None,
                                None, moment)
        if age > policy.risk.max_quote_age_seconds:
            return self._finish(codes.STATUS_BLOCKED, (codes.QUOTE_STALE,), None, None, None,
                                moment)
        return spec, tick, bars

    def _spec_for(self, symbol: str) -> SymbolSpec | None:
        cached = self._spec_cache.get(symbol)
        if cached is not None:
            return cached
        try:
            spec = self._backend.symbol_spec(symbol)
        except Exception:
            return None
        if not isinstance(spec, SymbolSpec):
            return None
        if spec.symbol != symbol:
            # A backend can answer for a different instrument than the one asked for; its
            # tick size, tick value and spread would then size and cap the wrong position.
            return None
        self._spec_cache[symbol] = spec
        return spec

    def _stop_risks(self, positions: list[PositionSnapshot]
                    ) -> tuple[tuple[float, ...], str | None]:
        """Each leg's stop cost in account currency, or the code for the first unmeasurable one.

        Our own legs are priced from the same spec the sizing path uses. A leg this run could
        never have opened — a pair whose profit currency is not the account's, so ``symbol_spec``
        refuses it — still holds money on this account, and the daily budget has to see it: the
        terminal's own money math prices it without borrowing the run's licence to trade there.
        A book that neither method can price yields a blocker, never a smaller reserve.
        """
        risks: list[float] = []
        for position in positions:
            spec = self._spec_for(position.symbol)
            if spec is None:
                if _stop_distance(position) is None:
                    return (), codes.POSITION_STOP_UNSET
                broker_risk = self._broker_stop_risk(position)
                if broker_risk is None:
                    return (), codes.MARKET_DATA_READ_FAILED
                risks.append(broker_risk)
                continue
            risk = _position_stop_risk(spec, position)
            if risk is None:
                # An open position with no stop, or a stop on the wrong side of entry, has no
                # measurable loss; the daily budget cannot be reserved against it.
                return (), codes.POSITION_STOP_UNSET
            risks.append(risk)
        return tuple(risks), None

    def _broker_stop_risk(self, position: PositionSnapshot) -> float | None:
        """Ask an optional terminal reader for a leg's stop cost, refusing a bad answer."""
        reader = getattr(self._backend, "position_stop_risk", None)
        if not callable(reader):
            return None
        try:
            value = _number(reader(position))
        except Exception:
            return None
        return value if value is not None and value >= 0 else None

    def _open_book_blockers(self, positions: list[PositionSnapshot], *, equity: float,
                            daily_budget: float, realized_loss: float = 0.0) -> tuple[str, ...]:
        """Positions already on the book can spend the daily allowance before we add another."""
        risks, unmeasurable = self._stop_risks(positions)
        if unmeasurable is not None:
            return (unmeasurable,)
        total = math.fsum(risks)
        # Banked loss and the loss still open in stops come out of the same budget.
        if realized_loss + total >= daily_budget or equity - total <= 0:
            return (codes.LOSS_FLOOR_BREACHED,)
        return ()

    def _aggregate_stop_risk(self, positions: list[PositionSnapshot]) -> float:
        """Total open stop risk; ``inf`` when any position cannot be measured.

        Both this and ``_open_book_blockers`` read the same measurement, so the number the
        daily budget is checked against is the number sizing is reduced by. ``inf`` is the
        fail-closed answer for a book they cannot price, not a licence to drop that leg.
        """
        risks, unmeasurable = self._stop_risks(positions)
        return math.inf if unmeasurable is not None else math.fsum(risks)

    def _confirmation_blockers(self, proposal: dict[str, Any],
                               sizing: dict[str, Any]) -> tuple[str, ...]:
        """Ask the operator in confirm mode; return () only on an explicit approval.

        ``confirm`` with no approver wired is a misconfiguration, not a licence to run
        unattended: the cycle is blocked rather than opened.
        """
        if not self._policy.confirmation_required:
            return ()
        if self._approver is None:
            return (codes.CONFIRMATION_UNAVAILABLE,)
        try:
            approved = self._approver(proposal, sizing)
        except Exception:  # a failing prompt is a refusal, never a pass
            return (codes.OPERATOR_CONFIRMATION_FAILED,)
        return () if approved is True else (codes.OPERATOR_DECLINED,)

    def _requote(self, moment: datetime) -> tuple[tuple[str, ...], Tick | None, SymbolSpec | None]:
        """Re-read the quote after approval; a moved, stale or wide market is a refusal."""
        policy = self._policy
        try:
            spec = self._backend.symbol_spec(policy.symbol)
            tick = self._backend.tick(policy.symbol)
        except Exception:
            return (codes.MARKET_DATA_READ_FAILED,), None, None
        if not isinstance(spec, SymbolSpec) or not isinstance(tick, Tick):
            return (codes.MARKET_DATA_READ_FAILED,), None, None
        if spec.symbol != policy.symbol or tick.symbol != policy.symbol:
            return (codes.BAR_SYMBOL_MISMATCH,), None, None
        age = (moment - tick.server_time).total_seconds()
        if age < -5:
            return (codes.QUOTE_FROM_THE_FUTURE,), None, None
        if age > policy.risk.max_quote_age_seconds:
            return (codes.QUOTE_STALE,), None, None
        if (tick.ask - tick.bid) / spec.point > policy.risk.max_spread_points:
            return (codes.SPREAD_ABOVE_LIMIT,), None, None
        return (), tick, spec

    def _bound_fill(self, *, proposal: Proposal, approved: SizedOrder, tick: Tick,
                    spec: SymbolSpec, risk_equity: float, leverage: int,
                    margin_available: float, open_stop_risk: float,
                    daily_budget: float, realized_loss: float = 0.0
                    ) -> tuple[tuple[str, ...], SizedOrder | None, dict[str, Any]]:
        """Re-price the approved trade at the fill the market would actually give.

        The operator approved a specific entry; a fill far from it is a different trade with
        a different risk. The drift is bounded as a fraction of the approved stop distance,
        the reward-to-risk is recomputed at the fill, and the volume is re-sized for the new
        stop distance — but never raised above what was approved.
        """
        policy = self._policy
        fill = float(tick.reference[proposal.direction])
        slip = abs(fill - proposal.entry_price)
        stop_distance = abs(fill - proposal.stop_loss)
        info: dict[str, Any] = {
            "fill_price": fill, "approved_entry_price": proposal.entry_price,
            "slippage": slip,
            "max_slippage": proposal.risk_distance * _MAX_REQUOTE_SLIP_FRACTION,
            "stop_distance_at_fill": stop_distance,
            "approved_volume": approved.volume,
        }
        if stop_distance <= 0 or slip > proposal.risk_distance * _MAX_REQUOTE_SLIP_FRACTION:
            return (codes.PRICE_MOVED_DURING_CONFIRMATION,), None, info
        risk_reward = abs(proposal.take_profit - fill) / stop_distance
        info["risk_reward_at_fill"] = risk_reward
        if risk_reward < policy.risk.min_risk_reward:
            return (codes.RISK_REWARD_BELOW_MINIMUM,), None, info
        resized = size_position(spec=spec, equity=risk_equity,
                                risk_fraction_limit=policy.risk.risk_per_trade_fraction,
                                stop_distance=stop_distance, leverage=leverage,
                                margin_available=margin_available, price=fill,
                                max_notional=risk_equity * policy.risk.max_notional_fraction,
                                max_volume=policy.risk.max_volume_lots)
        if not resized.accepted:
            return (resized.denial,), None, info
        candidate = resized.order
        assert candidate is not None
        volume = min(candidate.volume, approved.volume)
        risk_value = volume * candidate.risk_per_lot
        order = SizedOrder(volume=volume, risk_value=risk_value,
                           risk_fraction=risk_value / risk_equity,
                           risk_per_lot=candidate.risk_per_lot,
                           margin_required=spec.margin_requirement(volume, leverage, fill))
        info["final_volume"] = volume
        info["final_risk_value"] = risk_value
        info["final_margin_required"] = order.margin_required
        if order.margin_required > margin_available:
            # The paper book refuses a lot it cannot fund; naming the funding failure here keeps
            # the journaled reason precise instead of letting the broker raise PAPER_OPEN_REJECTED.
            return (codes.MARGIN_MINIMUM_LOT_UNFUNDED,), None, info
        if realized_loss + open_stop_risk + risk_value > daily_budget:
            return (codes.AGGREGATE_STOP_RISK_LIMIT_REACHED,), None, info
        return (), order, info

    def _open_paper_position(self, *, proposal: Proposal, order: SizedOrder, tick: Tick,
                             moment: datetime, equity: float, open_positions: int,
                             extra: Mapping[str, Any] | None = None) -> CycleResult:
        opener = getattr(self._backend, "open_position", None)
        payload = proposal.as_dict()
        sized = order.as_dict()
        if not callable(opener):
            return self._finish(codes.STATUS_BLOCKED,
                                (codes.PAPER_BROKER_REQUIRED_FOR_PAPER_MODE,), payload, sized,
                                None, moment, equity=equity,
                                open_positions=open_positions, extra=extra)
        try:
            # Fill at the quote just re-read, not at the pre-approval snapshot the operator saw.
            position = opener(direction=proposal.direction, volume=order.volume,
                              entry_price=tick.reference[proposal.direction],
                              stop_loss=proposal.stop_loss,
                              take_profit=proposal.take_profit, opened_at=tick.server_time)
        except Exception as exc:
            # The paper book refusing a lot is a decision, not a crash: journal the reason.
            failure = dict(extra or {})
            failure["error_kind"] = type(exc).__name__
            return self._finish(codes.STATUS_BLOCKED, (codes.PAPER_OPEN_REJECTED,), payload, sized,
                                None, moment, equity=equity, open_positions=open_positions,
                                extra=failure)
        return self._finish(codes.STATUS_PAPER_OPENED, (), payload, sized, int(position.ticket),
                            moment, equity=equity, open_positions=open_positions + 1,
                            extra=extra)

    def close_positions(self, reason: str = "MANUAL") -> list[dict[str, Any]]:
        self.closing_equity_recorded = False
        self.closing_equity_error = ""
        closer = getattr(self._backend, "close_all", None)
        if closer is None:
            raise RuntimeError("this backend has no paper book to close")
        trades = closer(reason)
        for trade in trades:
            if self._journal is not None:
                self._journal.record("paper_trade_closed", trade_event_payload(
                    trade, execution_mode=self._policy.execution_mode))
        if self._journal is not None and trades:
            # Closing the book changes equity: without a final chained point the report ends
            # on the pre-close value and understates the realized result. Best effort — the
            # run is ending — but the failure is recorded in ``closing_equity_error`` so the
            # CLI can say the final drawdown row is missing instead of hiding it.
            try:
                account = self._backend.account()
                open_positions = len(self._backend.positions() or [])
                self._journal.record_equity(
                    equity=float(account.equity), balance=float(account.balance),
                    open_positions=open_positions, at=self._clock(),
                    execution_mode=self._policy.execution_mode)
                self.closing_equity_recorded = True
            except Exception as exc:
                self.closing_equity_error = type(exc).__name__
        return [trade.as_dict() for trade in trades]

    def _finish(self, status: str, blockers: tuple[str, ...], proposal: dict | None,
                sizing: dict | None, ticket: int | None, moment: datetime, *,
                equity: float | None = None, open_positions: int = 0,
                extra: Mapping[str, Any] | None = None) -> CycleResult:
        payload: dict[str, Any] = {
            "mode": self._policy.mode, "symbol": self._policy.symbol,
            "timeframe": self._policy.timeframe,
            # Which execution mode made this decision is part of the audit, not of the locale:
            # the journal stores codes, and translations are rendered only when printed.
            "execution_mode": self._policy.execution_mode,
            "ui_language": self._policy.ui_language,
            "cycle_time": moment.isoformat() if isinstance(moment, datetime) else repr(moment),
            "status": status,
            "blockers": list(blockers), "proposal": proposal, "sizing": sizing,
            "paper_ticket": ticket, "open_positions": open_positions,
            "broker_order_sent": False,
            "execution_policy": codes.NO_BROKER_ORDER_API,
        }
        if extra:
            payload.update(dict(extra))
        # The equity point is a row of this same chain, so it is written first: then the cycle
        # event can truthfully say whether the point exists.
        equity_record_error = ""
        equity_recorded = False
        if self._journal is not None and equity is not None:
            try:
                account = self._backend.account()
                self._journal.record_equity(equity=equity, balance=account.balance,
                                            open_positions=open_positions, at=moment,
                                            execution_mode=self._policy.execution_mode)
                equity_recorded = True
            except Exception as exc:
                # Losing the equity point is reported, never swallowed: the run's drawdown
                # figures come from those rows.
                equity_record_error = type(exc).__name__
        payload["equity_recorded"] = equity_recorded
        payload["equity_record_error"] = equity_record_error
        event = None
        cycle_record_error = ""
        if self._journal is not None:
            try:
                event = self._journal.record("agent_cycle", payload)
            except Exception as exc:
                # A journal that stops accepting rows must not turn a decision into a crash:
                # the result says the decision went unjournaled instead.
                cycle_record_error = type(exc).__name__
        recorded_at = (event or {}).get("timestamp") or payload["cycle_time"]
        decision_hash = (event or {}).get("event_hash", "journal-unavailable")
        return CycleResult(status=status, blockers=tuple(blockers), proposal=proposal,
                           sizing=sizing, ticket=ticket,
                           equity=float(equity) if equity is not None else float("nan"),
                           recorded_at=recorded_at, decision_hash=str(decision_hash),
                           broker_order_sent=False, open_positions=open_positions,
                           equity_recorded=equity_recorded,
                           equity_record_error=equity_record_error,
                           execution_mode=self._policy.execution_mode,
                           cycle_recorded=event is not None)
