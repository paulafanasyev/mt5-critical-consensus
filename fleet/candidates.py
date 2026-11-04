"""Candidate book: what each park trade would have looked like with real money — journalled only.

This module exists to answer one question with numbers instead of opinion: if the same trade had
been taken on a real account, slightly earlier and slightly bigger, what would it have cost? The
entry price the park pays is measured against the price the model actually asked for, so the cost
of waiting for confirmation becomes a sum of dollars rather than a feeling.

Nothing here can place a trade, and that is a tested property: the module imports no broker, no
MT5, no network. It turns numbers the worker already has into journal rows, and reads journal rows
back. A real account stays unreachable from this code path.
"""
from __future__ import annotations

import json
import math

KIND = "candidate"


def _finite(value: float) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def entry_note(signal, execution, spec, risk_usd: float) -> dict:
    """Describe one trade at the moment it was taken: prices, geometry, and the drift already paid.

    `drift_points` is how far the market had already moved in our favour between the price the
    strategy computed and the price the broker filled. Positive drift is the "it is already
    growing" condition the operator wants to buy into: it is real, but it is paid for, and this is
    the only place in the park that writes down the bill.
    """
    fill = float(execution.price)
    asked = float(signal.price)
    direction = 1.0 if str(signal.side).casefold() == "buy" else -1.0
    point = float(getattr(spec, "point", 0.0) or 0.0)
    note: dict = {
        "symbol": str(spec.name),
        "side": str(signal.side),
        "volume": float(execution.volume),
        "signal_price": round(asked, 8),
        "fill_price": round(fill, 8),
        "stop": round(float(signal.stop), 8),
        "target": round(float(signal.target), 8),
        "atr": round(float(signal.atr), 8),
        "reason": str(signal.reason),
        "ticket": int(getattr(execution, "position_ticket", 0) or 0),
        "risk_usd": round(float(risk_usd), 4) if _finite(risk_usd) else None,
        "drift_points": round((fill - asked) * direction / point, 2) if point > 0 else None,
    }
    stop_distance = abs(fill - float(signal.stop))
    target_distance = abs(float(signal.target) - fill)
    note["risk_reward"] = round(target_distance / stop_distance, 3) if stop_distance > 0 else None
    tick_size = float(getattr(spec, "tick_size", 0.0) or 0.0)
    tick_value = float(getattr(spec, "tick_value", 0.0) or 0.0)
    if tick_size > 0 and tick_value > 0:
        # Dollars of entry price given away to confirmation, priced by the broker's own contract:
        # (tick_value / tick_size) is the money per unit of price per one lot.
        per_lot = tick_value / tick_size
        given = (fill - asked) * direction
        note["drift_usd_per_lot"] = round(given * per_lot, 4)
        note["drift_cost_usd"] = round(given * per_lot * float(execution.volume), 4)
    return note


def record(store, account, note: dict) -> None:
    """Journal the candidate. Never called on a path that could reach an order."""
    store.event(account.name, KIND, **note)


def ledger(store, limit: int = 5000) -> dict:
    """Join candidates to their outcomes and add up what the drift cost.

    An outcome is joined by ticket only. A close the journal never tied to a candidate stays out of
    the profit column, and a candidate the broker never gave a ticket for stays `blind`: it is
    counted, but nothing is borrowed to explain it. The ledger exists to be checked against the
    broker's own history, so a guessed join is worse than an empty column.
    """
    rows = store.recent_events(limit=limit)
    notes: list[dict] = []
    closed: dict[int, dict] = {}
    for ts, account, kind, payload in reversed(list(rows)):
        try:
            data = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            continue
        if kind == KIND:
            data["ts"] = ts
            data["account"] = account
            notes.append(data)
        elif kind == "closed" and int(data.get("ticket") or 0) > 0:
            closed[int(data["ticket"])] = {"ts": ts, "account": account, **data}
    trades = []
    for note in notes:
        ticket = int(note.get("ticket") or 0)
        result = closed.get(ticket)
        trades.append({
            "ticket": ticket,
            "account": note["account"],
            "symbol": note.get("symbol", ""),
            "side": note.get("side", ""),
            "volume": note.get("volume"),
            "drift_points": note.get("drift_points"),
            "drift_cost_usd": note.get("drift_cost_usd"),
            "risk_usd": note.get("risk_usd"),
            "risk_reward": note.get("risk_reward"),
            "profit_usd": round(float(result["profit"]), 2) if result else None,
            "exit": result.get("reason") if result else "",
        })
    done = [t for t in trades if t["profit_usd"] is not None]
    won = [t for t in done if float(t["profit_usd"]) > 0]
    paid = [t for t in trades if isinstance(t["drift_cost_usd"], (int, float))]
    measured = [t for t in trades if isinstance(t["drift_points"], (int, float))]
    blind = [t for t in trades if t["ticket"] == 0]
    return {
        "candidates": len(trades),
        "closed": len(done),
        "open": len([t for t in trades if t["profit_usd"] is None]) - len(blind),
        "blind": len(blind),
        "win_rate": round(100.0 * len(won) / len(done), 1) if done else None,
        "profit_usd": round(sum(float(t["profit_usd"]) for t in done), 2) if done else 0.0,
        "drift_cost_usd": round(sum(float(t["drift_cost_usd"]) for t in paid), 2) if paid else 0.0,
        "mean_drift_points": round(
            sum(float(t["drift_points"]) for t in measured) / len(measured), 2)
        if measured else None,
        "trades": trades,
    }
