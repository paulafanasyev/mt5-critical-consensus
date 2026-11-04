"""Candidate book: what each park trade would have looked like with real money — journalled only.

This module exists to answer one question with numbers instead of opinion: if the same trade had
been taken on a real account, slightly earlier and slightly bigger, what would it have cost? The
entry price the park pays is measured against the price the model actually asked for, so the cost
of waiting for confirmation becomes a sum of dollars rather than a feeling.

Nothing here can place a trade, and that is a tested property: the module imports no broker, no
MT5, no network, and it calls one method of the store it is handed — `event`, an insert. It turns
numbers the worker already has into journal rows, and reads journal rows back. A real account
stays unreachable from this code path.
"""
from __future__ import annotations

import json
import math

KIND = "candidate"
CLOSED = "closed"

# The journal writes these itself, so a note carrying one would collide with its own row.
RESERVED_KEYS = ("ts", "account", "kind")


def _num(value, digits: int = 8) -> float | None:
    """A number for the journal, or None where there is no number.

    NaN and Infinity are money-shaped tokens a broken broker read can hand back. Written into the
    book they poison every column that adds them up, and JSON has no way to spell either one.
    A blank is honest; a fake zero is not.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def entry_note(signal, execution, spec, risk_usd: float) -> dict:
    """Describe one trade at the moment it was taken: prices, geometry, and the drift already paid.

    `drift_points` is how far the market had already moved in our favour between the price the
    strategy computed and the price the broker filled. Positive drift is the "it is already
    growing" condition the operator wants to buy into: it is real, but it is paid for, and this is
    the only place in the park that writes down the bill.

    Both numbers have to be prices THIS side can get; the strategy stamps a sell at bid for that
    reason. Measured against an ask, a sell would pay the spread on a still market and the bill
    would be a story about how the book reads the quote, not about waiting.
    """
    fill = _num(execution.price)
    asked = _num(signal.price)
    direction = 1.0 if str(signal.side).casefold() == "buy" else -1.0
    point = _num(getattr(spec, "point", 0.0), 12) or 0.0
    stop = _num(signal.stop)
    target = _num(signal.target)
    note: dict = {
        "symbol": str(spec.name),
        "side": str(signal.side),
        "volume": _num(execution.volume, 4),
        "signal_price": asked,
        "fill_price": fill,
        "stop": stop,
        "target": target,
        "atr": _num(signal.atr),
        "reason": str(signal.reason),
        "ticket": int(getattr(execution, "position_ticket", 0) or 0),
        "ticket_source": str(getattr(execution, "ticket_source", "unknown")),
        "risk_usd": _num(risk_usd, 4),
        "drift_points": (round((fill - asked) * direction / point, 2)
                         if fill is not None and asked is not None and point > 0 else None),
    }
    stop_distance = None if fill is None or stop is None else abs(fill - stop)
    take_distance = None if target is None or stop is None else abs(target - stop)
    note["risk_reward"] = (round(take_distance / stop_distance, 3)
                           if stop_distance and take_distance is not None else None)
    tick_size = _num(getattr(spec, "tick_size", 0.0), 12) or 0.0
    tick_value = _num(getattr(spec, "tick_value", 0.0), 8) or 0.0
    if tick_size > 0 and tick_value > 0 and fill is not None and asked is not None:
        # Dollars of entry price given away to confirmation, priced by the broker's own contract:
        # (tick_value / tick_size) is the money per unit of price per one lot.
        per_lot = tick_value / tick_size
        given = (fill - asked) * direction
        note["drift_usd_per_lot"] = round(given * per_lot, 4)
        note["drift_cost_usd"] = round(given * per_lot * float(execution.volume), 4)
    return note


def record(store, account, note: dict) -> None:
    """Journal the candidate. Never called on a path that could reach an order."""
    clash = sorted(set(note) & set(RESERVED_KEYS))
    if clash:
        raise ValueError(f"ключ заметки совпал со служебным полем журнала: {clash}")
    store.event(account.name, KIND, **note)


def ledger(store, limit: int = 5000) -> dict:
    """Join candidates to their outcomes and add up what the drift cost.

    An outcome is joined by account AND ticket: a ticket is unique on one server, not across the
    book this file keeps for every broker, so a bare ticket join would pay one account's trade out
    of another's profit. A close the journal never tied to a candidate stays out of the profit
    column, and a candidate the broker never named stays `blind`: counted, nothing borrowed to
    explain it. The ledger exists to be checked against the broker's own history, so a guessed join
    is worse than an empty column.
    """
    rows = store.events_of((KIND, CLOSED), limit=limit)
    notes: list[dict] = []
    closed: dict[tuple, dict] = {}
    for ts, account, kind, payload in rows:
        try:
            data = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            continue
        if kind == KIND:
            data["ts"] = ts
            data["account"] = account
            notes.append(data)
        elif kind == CLOSED:
            ticket = int(data.get("ticket") or 0)
            if ticket > 0:
                closed[(account, str(data.get("symbol") or "").casefold(), ticket)] = {
                    "ts": ts, "account": account, **data}
    journal_rows = store.count_events(KIND)
    trades = []
    for note in notes:
        ticket = int(note.get("ticket") or 0)
        key = (note["account"], str(note.get("symbol") or "").casefold(), ticket)
        # Consumed, not looked up: two candidate rows carrying the same ticket (a restart that
        # replayed the fill, a journal written twice) must not bill one close out twice.
        result = closed.pop(key, None)
        trades.append({
            "ticket": ticket,
            "ticket_source": note.get("ticket_source", "unknown"),
            "account": note["account"],
            "symbol": note.get("symbol", ""),
            "side": note.get("side", ""),
            "volume": note.get("volume"),
            "drift_points": note.get("drift_points"),
            "drift_cost_usd": note.get("drift_cost_usd"),
            "risk_usd": note.get("risk_usd"),
            "risk_reward": note.get("risk_reward"),
            "profit_gross_usd": _num(result.get("profit"), 2) if result else None,
            "exit": result.get("reason") if result else "",
        })
    done = [t for t in trades if t["profit_gross_usd"] is not None]
    won = [t for t in done if float(t["profit_gross_usd"]) > 0]
    paid = [t for t in trades if isinstance(t["drift_cost_usd"], (int, float))]
    measured = [t for t in trades if isinstance(t["drift_points"], (int, float))]
    blind = [t for t in trades if t["ticket"] == 0]
    return {
        "candidates": len(trades),
        # How many candidates the journal holds in total, and whether this read saw all of them:
        # a bounded window that ran out must not be printed as an empty book.
        "journal_rows": journal_rows,
        "truncated": journal_rows > len(trades),
        "closed": len(done),
        "open": len([t for t in trades if t["profit_gross_usd"] is None]) - len(blind),
        "blind": len(blind),
        "win_rate": round(100.0 * len(won) / len(done), 1) if done else None,
        # Gross, exactly as the broker's own position report states it: no commission, no swap.
        "profit_gross_usd": (round(sum(float(t["profit_gross_usd"]) for t in done), 2)
                             if done else 0.0),
        "drift_cost_usd": round(sum(float(t["drift_cost_usd"]) for t in paid), 2) if paid else 0.0,
        "mean_drift_points": (round(sum(float(t["drift_points"]) for t in measured)
                                    / len(measured), 2) if measured else None),
        "trades": trades,
    }
