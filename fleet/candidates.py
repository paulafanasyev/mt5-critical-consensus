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
# The park's own witness that a ticket stopped being only its money: on a netting account the
# operator's manual lot folds into the position this park opened, and the broker later closes the
# two of them together under one number.
MERGED = "lot_merged_with_manual"

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


def entry_note(signal, execution, spec, risk_usd: float, currency: str = "",
               rate: float = 0.0) -> dict:
    """Describe one trade at the moment it was taken: prices, geometry, and the drift already paid.

    `drift_points` is how far the market had already moved in our favour between the price the
    strategy computed and the price the broker filled. Positive drift is the "it is already
    growing" condition the operator wants to buy into: it is real, but it is paid for, and this is
    the only place in the park that writes down the bill.

    Both numbers have to be prices THIS side can get; the strategy stamps a sell at bid for that
    reason. Measured against an ask, a sell would pay the spread on a still market and the bill
    would be a story about how the book reads the quote, not about waiting.

    `drift_cost` is the bill in the currency the account is kept in, because that is what the
    broker's own tick value is quoted in — not dollars. A ruble account would print a ruble number
    under a dollar sign and a cent account a number a hundred times too small, so the note carries
    the account currency and the rate the worker already paid for, and the dollar column is only
    written while that rate exists. An unpriced bill stays blank instead of becoming a false sum.
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
        "currency": str(currency or ""),
        "rate_to_usd": _num(rate, 6) if rate > 0 else None,
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
        # Money of entry price given away to confirmation, priced by the broker's own contract:
        # (tick_value / tick_size) is the money per unit of price per one lot.
        per_lot = tick_value / tick_size
        given = (fill - asked) * direction
        note["drift_cost_per_lot"] = round(given * per_lot, 4)
        note["drift_cost"] = round(given * per_lot * float(execution.volume), 4)
        note["drift_cost_usd"] = (round(note["drift_cost"] * float(rate), 4)
                                  if rate > 0 else None)
    return note


def record(store, account, note: dict) -> None:
    """Journal the candidate. Never called on a path that could reach an order."""
    clash = sorted(set(note) & set(RESERVED_KEYS))
    if clash:
        raise ValueError(f"ключ заметки совпал со служебным полем журнала: {clash}")
    store.event(account.name, KIND, **note)


def ledger(store, limit: int = 5000) -> dict:
    """Join candidates to their outcomes and add up what the drift cost.

    An outcome is joined by account, symbol AND ticket: a ticket is unique on one server, not across
    the book this file keeps for every broker, so a bare ticket join would pay one account's trade
    out of another's profit. Only a ticket the broker's own comment named (`ticket_source == token`)
    lends its outcome to the sums: an inferred ticket on a netting account carries the whole merged
    position — the operator's lots included — so its money is reported as `closed_guessed` and
    `guessed_profit_usd`, beside the totals rather than inside them. Rows sharing one ticket are
    `shared_ticket`: the close belongs to that ticket, not to either entry, so none of them is
    settled and none is called open. A candidate the broker never named at all stays `blind`.

    A `lot_merged_with_manual` event is the park's own witness that one of its tickets stopped being
    only its money: the operator's manual lot folded into it, and the broker later closes the two
    together under one number. Such a row makes the ticket `merged_ticket` for as long as it is in
    the window, and its close pays nothing anywhere — not into `profit_gross_usd`, not into
    `guessed_profit_usd`, because no figure here separates the operator's half from ours.

    A dollar column is filled only from a row that says which currency its money was in. The broker
    quotes its tick value and reports its profit in the currency the account is kept in, so on a
    ruble account that number is rubles and on a cent account a hundredth of a dollar: `rate_to_usd`
    is the witness that the number was converted, and a row without it is counted — in
    `settled_unpriced` for a close, `drift_unpriced` for a bill — rather than added to a sum it does
    not belong to. An attributable close whose dollars are simply not known yet (the history read
    failed) is the same kind of row: counted, never summed.
    """
    rows = store.events_of((KIND, CLOSED, MERGED), limit=limit)
    notes: list[dict] = []
    closed: dict[tuple, dict] = {}
    merged_tickets: set[tuple[str, int]] = set()
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
        elif kind == MERGED:
            # `lots` is the list the worker journalled: every ticket on this account that grew volume
            # nobody here ordered. A ticket once folded is folded for the life of the position, so one
            # row anywhere in the window is enough to take its money out of this book's sums.
            for lot in data.get("lots") or []:
                try:
                    ticket = int(lot.get("ticket") or 0)
                except (TypeError, ValueError, AttributeError):
                    continue
                if ticket > 0:
                    merged_tickets.add((account, ticket))
    journal_rows = store.count_events(KIND)
    # One position can carry several candidate rows: on a netting account the park's own second entry
    # folds into the same ticket as the first, and a restart can write the same fill twice. The close
    # of such a ticket is one lot's money and cannot be said to belong to either row, so a shared
    # ticket is named before the join is attempted and its close is left in the index instead of
    # paid to whichever note happened to be read first.
    per_ticket: dict[tuple, int] = {}
    for note in notes:
        ticket = int(note.get("ticket") or 0)
        if ticket > 0:
            key = (note["account"], str(note.get("symbol") or "").casefold(), ticket)
            per_ticket[key] = per_ticket.get(key, 0) + 1
    trades = []
    for note in notes:
        ticket = int(note.get("ticket") or 0)
        key = (note["account"], str(note.get("symbol") or "").casefold(), ticket)
        shared = ticket > 0 and per_ticket[key] > 1
        # Consumed, not looked up: two candidate rows carrying the same ticket must not bill one
        # close out twice, so only an unshared row is allowed to take it.
        result = None if shared else closed.pop(key, None)
        priced = _num(note.get("rate_to_usd"), 6) is not None
        settled = result is not None
        profit_usd = (_num(result.get("profit_usd"), 2)
                      if settled and _num(result.get("rate_to_usd"), 6) is not None else None)
        # The ticket is what ties an outcome to this entry. `token` means the comment this park sent
        # named it; anything else means somebody inferred it, and on a netting account an inferred
        # ticket carries the whole merged position — the operator's money included. The same witness
        # that names a merged position is `merged`: the park itself said this ticket grew a lot nobody
        # here ordered, so its close is that lot's money and no arithmetic here separates it.
        merged = settled and (note["account"], ticket) in merged_tickets
        attributable = settled and note.get("ticket_source", "unknown") == "token" and not merged
        trades.append({
            "ticket": ticket,
            "ticket_source": note.get("ticket_source", "unknown"),
            "account": note["account"],
            "symbol": note.get("symbol", ""),
            "side": note.get("side", ""),
            "volume": note.get("volume"),
            "currency": str(note.get("currency") or ""),
            "rate_to_usd": _num(note.get("rate_to_usd"), 6),
            "drift_points": note.get("drift_points"),
            "drift_cost": note.get("drift_cost"),
            "drift_cost_usd": note.get("drift_cost_usd") if priced else None,
            "risk_usd": note.get("risk_usd"),
            "risk_reward": note.get("risk_reward"),
            "settled": settled,
            "shared": shared,
            "merged": merged,
            "attributable": attributable,
            "profit_gross_usd": profit_usd if attributable else None,
            # A merged ticket's dollars are not printed as a guess either: the number the broker
            # closed is the operator's lot and this lot's, and no figure here separates them.
            "guessed_profit_usd": (profit_usd if settled and not attributable and not merged
                                   else None),
            "exit": result.get("reason") if settled else "",
            "closed_by": result.get("by", "") if settled else "",
        })
    # Only a ticket the broker's own comment named makes an outcome this book may add up.
    done = [t for t in trades if t["attributable"] and t["profit_gross_usd"] is not None]
    won = [t for t in done if float(t["profit_gross_usd"]) > 0]
    lost = [t for t in done if float(t["profit_gross_usd"]) < 0]
    flat = [t for t in done if float(t["profit_gross_usd"]) == 0]
    guessed_settled = [t for t in trades if t["guessed_profit_usd"] is not None]
    blind = [t for t in trades if t["ticket"] == 0]
    # One ticket is one entry into the market. A retry that journalled the same fill twice must bill
    # the wait once: two rows of 25 points are one 25-point drift, and summing per row would make the
    # price of confirmation a multiple of how many lines the journal happened to keep.
    seen: set[tuple] = set()
    entries = []
    for t in trades:
        if t["ticket"]:
            key = (t["account"], str(t["symbol"] or "").casefold(), t["ticket"])
            if key in seen:
                continue
            seen.add(key)
        entries.append(t)
    paid = [t for t in entries if isinstance(t["drift_cost_usd"], (int, float))]
    measured = [t for t in entries if isinstance(t["drift_points"], (int, float))]
    # A close that joined a candidate but arrived before the journal knew its currency: the trade
    # finished, and its money is still a number in a currency nobody wrote down. A merged ticket is
    # not that: its number exists and belongs to two drivers, and `merged_ticket` says so separately.
    unpriced = [t for t in trades if t["settled"] and not t["merged"]
                and t["profit_gross_usd"] is None and t["guessed_profit_usd"] is None]
    return {
        "candidates": len(trades),
        # How many candidates the journal holds in total, and whether this read saw all of them:
        # a bounded window that ran out must not be printed as an empty book.
        "journal_rows": journal_rows,
        "truncated": journal_rows > len(trades),
        "closed": len(done),
        "open": len([t for t in trades if t["ticket"] and not t["settled"] and not t["shared"]]),
        "blind": len(blind),
        "settled_unpriced": len(unpriced),
        # A drift measured in points that nobody could price in dollars: not a zero, not a sum.
        "drift_unpriced": len([t for t in entries
                               if isinstance(t["drift_points"], (int, float))
                               and not isinstance(t["drift_cost_usd"], (int, float))]),
        # The joins nobody can vouch for: `only_position` guessed the ticket from a book with one
        # lot on it, `unknown` never found one. Their outcomes are shown apart and never summed in.
        "guessed": len([t for t in trades if t["ticket_source"] != "token"]),
        "closed_guessed": len(guessed_settled),
        "guessed_profit_usd": (round(sum(float(t["guessed_profit_usd"])
                                         for t in guessed_settled), 2)
                               if guessed_settled else 0.0),
        # Rows sharing one ticket with another entry: the close of that ticket is one lot's money and
        # this book cannot say whose, so none of them is settled and none is called open.
        "shared_ticket": len([t for t in trades if t["shared"]]),
        # Settled rows on a ticket the park itself reported as merged with the operator's manual lot:
        # the broker's number is real money and it is two drivers' money at once, so it is named here
        # and kept out of every sum and every win rate in this book.
        "merged_ticket": len([t for t in trades if t["merged"]]),
        "flat": len(flat),
        "by_source": {source: len([t for t in trades if t["ticket_source"] == source])
                      for source in sorted({t["ticket_source"] for t in trades})},
        # A break-even exit is neither a win nor a loss: counting it as a loss would price a flat
        # ticket as a defeat the strategy never had.
        "win_rate": (round(100.0 * len(won) / (len(won) + len(lost)), 1)
                     if won or lost else None),
        # Gross, exactly as the broker's own position report states it: no commission, no swap.
        "profit_gross_usd": (round(sum(float(t["profit_gross_usd"]) for t in done), 2)
                             if done else 0.0),
        "drift_cost_usd": round(sum(float(t["drift_cost_usd"]) for t in paid), 2) if paid else 0.0,
        "drift_priced": len(paid),
        "mean_drift_points": (round(sum(float(t["drift_points"]) for t in measured)
                                    / len(measured), 2) if measured else None),
        "trades": trades,
    }
