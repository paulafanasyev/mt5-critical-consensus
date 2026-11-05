"""Mirror the demo book onto the real account while the demo is in profit.

The operator's rule: when the demo is growing, do the same on the real account. This script is the
bridge. It runs two terminal sessions in one process, sequentially:

  1. read the demo account (the laboratory) through its own terminal;
  2. decide, from the demo equity trend and each position's own profit, what deserves to be mirrored;
  3. connect to the separate real terminal and place the same trade with a much smaller size,
     copying the stop and target;
  4. close the real trade as soon as the demo counterpart is gone;
  5. stop everything for the day if the real account loses its daily allowance.

Nothing is opened by default. Every order in this file leaves through `send_order`, and `send_order`
asks `fleet.livegate` at the moment of sending — of the account the terminal names at that moment,
not of the one this cycle happened to read first, because a hand re-login in the middle of a run is
otherwise answered with somebody else's permission. An opening needs the stand
(`C:\\mt5-fleet\\PAUSE`) gone, `--live` on this invocation, and a registry row holding this login as
`mode: real` with `enabled: true`. The registry's own `max_lots` for that row is a hard ceiling on
what gets mirrored: a script that mirrors at its default 0.05 onto an account the operator capped at
0.01 is not copying the demo, it is multiplying the risk.

Those four words are the door to *opening*. Closing our own lot asks less (`livegate.exit_refusal`:
only that the terminal names the account), because a mirror that stops selling when the operator
parks the machine leaves real money whose only remaining exit is a process that is not running. A
refused entry is said in the log and the cycle continues with its exits.

And a copy is bought only with a level that survives this process. A demo ticket that carries neither
stop nor target is not mirrored — the same rule the file bridge states as `NO STOP LEVEL` — because
the mirror's own exits (`real_tp_usd`, `max_hold_min`) live in this loop, and a loop stops existing
when the laptop closes or the machine is parked.

It still refuses a terminal that is not logged into a real account — that check guards the money from
a mis-wired path; the checks above guard the money from this script being run at all.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleet import livegate  # noqa: E402

LOG = ROOT / "agent_data" / "real_mirror.jsonl"
STATE = ROOT / "agent_data" / "real_mirror_state.json"
DEMO_MAGIC_FLOOR = 8180000
REAL_MAGIC = 8272026
DEMO_TERMINAL = r"C:\mt5-exness\terminal64.exe"


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(f"[{event['ts'][11:19]}] {event['event']}", flush=True)


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state), encoding="utf-8")


def send_order(mt5, request: dict, gate) -> dict | None:
    """The only door to the real account: ask permission now, not when the loop was entered.

    The stand can appear between the start of a cycle and the moment of a send, so the answer is
    read again here. A refused order returns None and the caller keeps its state unchanged: the
    mirror must not remember a trade it never placed.
    """
    reason = gate()
    if reason:
        log({"event": f"NOT SENT {request.get('action')} {request.get('symbol')} "
                      f"{request.get('volume')}: {reason}"})
        return None
    return mt5.order_send(request)


def hourly_uptrend(mt5, symbol: str) -> bool:
    """Price above EMA20 and EMA20 above EMA50 on H1 — only buy what is really rising."""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 60)
    if rates is None or len(rates) < 50:
        return False
    closes = [float(rate["close"]) for rate in rates]

    def ema(values, period):
        weight = 2 / (period + 1)
        result = values[0]
        for value in values[1:]:
            result = value * weight + result * (1 - weight)
        return result

    fast, slow = ema(closes, 20), ema(closes, 50)
    return closes[-1] > fast > slow


def demo_equity_trend(points: int = 6) -> float:
    """Change in demo equity over the last few supervisor heartbeats."""
    path = ROOT / "agent_data" / "supervisor.jsonl"
    if not path.exists():
        return 0.0
    equities = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = event.get("event", "")
        if not text.startswith("HEARTBEAT"):
            continue
        parts = text.split()
        for index, word in enumerate(parts):
            if word == "equity" and index + 1 < len(parts):
                try:
                    equities.append(float(parts[index + 1]))
                except ValueError:
                    pass
    if len(equities) < points:
        return 0.0
    return equities[-1] - equities[-points]


def book_of(mt5) -> list | None:
    """The positions this terminal answered with, or None when it answered nothing at all.

    `positions_get()` returns None on a dropped channel and an empty list when the account is flat.
    Read as one value they say opposite sentences: an empty demo book tells the mirror "the demo let
    this lot go", and the real copy is sold; an empty real book tells it "nothing is mirrored yet",
    and a second lot is bought on every symbol the demo holds. This machine has measured the IPC
    timeout that produces the first of those, so the difference is not hypothetical.
    """
    book = mt5.positions_get()
    return None if book is None else list(book)


def main(argv: list[str] | None = None) -> int:
    import MetaTrader5 as mt5

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-path", required=True,
                        help="path to terminal64.exe of the real-account instance")
    parser.add_argument("--live", action="store_true",
                        help="единственный способ разрешить этому запуску тронуть реальные деньги")
    parser.add_argument("--config", default="",
                        help="реестр счетов; по умолчанию config/accounts.yaml")
    parser.add_argument("--interval-sec", type=int, default=30)
    parser.add_argument("--real-lots", type=float, default=0.01)
    parser.add_argument("--daily-stop-pct", type=float, default=0.5)
    parser.add_argument("--min-position-profit", type=float, default=0.0,
                        help="mirror only positions at least this much in profit")
    parser.add_argument("--real-tp-usd", type=float, default=3.0,
                        help="bank the real copy at this profit, earlier than the demo")
    parser.add_argument("--max-hold-min", type=int, default=90,
                        help="close the real copy after this many minutes, earlier than the demo")
    parser.add_argument("--once", action="store_true", help="один цикл и выйти")
    args = parser.parse_args(argv)
    registry = pathlib.Path(args.config).resolve() if args.config else None

    state = load_state()
    daily_key = datetime.now(timezone.utc).date().isoformat()
    if state.get("day") != daily_key:
        state["day"] = daily_key
        state["day_start_equity"] = None
        state["halted"] = False
        save_state(state)

    log({"event": "MIRROR START: demo -> real, growth only"})

    while True:
        try:
            # --- 1. the demo laboratory ---
            if not mt5.initialize(path=DEMO_TERMINAL):
                log({"event": "demo terminal unavailable, retry"})
                if args.once:
                    return 1
                time.sleep(args.interval_sec)
                continue
            demo_book = book_of(mt5)
            if demo_book is None:
                # Nothing is known about the demo this cycle, and both halves of the job below ask
                # that question: the closes are keyed to what the demo still holds, the copies to what
                # it is growing. A cycle that cannot read the laboratory sells real money on a ghost.
                log({"event": "демо не ответил на вопрос о позициях: в этом цикле ни копий, ни "
                              "выходов по «демо закрыл»"})
                mt5.shutdown()
                if args.once:
                    return 1
                time.sleep(args.interval_sec)
                continue
            demo_positions = [p for p in demo_book
                              if p.magic >= DEMO_MAGIC_FLOOR]
            demo_positions = [{"ticket": p.ticket, "symbol": p.symbol, "type": p.type,
                               "volume": p.volume, "price_open": p.price_open,
                               "sl": p.sl, "tp": p.tp, "profit": p.profit}
                              for p in demo_positions]
            mt5.shutdown()
            trend = demo_equity_trend()
            growth = trend > 0

            # --- 2. the real terminal ---
            if not mt5.initialize(path=args.real_path):
                log({"event": "real terminal unavailable, retry"})
                if args.once:
                    return 1
                time.sleep(args.interval_sec)
                continue
            real_account = mt5.account_info()
            if real_account is None:
                log({"event": "real terminal not logged in"})
                mt5.shutdown()
                if args.once:
                    return 1
                time.sleep(args.interval_sec)
                continue
            if real_account.trade_mode != 2:
                log({"event": f"refusing: {real_account.login} is not a real account"})
                mt5.shutdown()
                if args.once:
                    return 1
                time.sleep(args.interval_sec)
                continue

            # --- 2b. the operator's permission, asked of the account the terminal names right now ---
            def gate() -> str:
                # Not `real_account`: that object was read once, at the top of this cycle. A manual
                # re-login in the middle of a run used to be answered with the consent belonging to
                # the account that is no longer there, and the order went to the money that is.
                who = mt5.account_info()
                return livegate.refusal(getattr(who, "login", None), live=args.live,
                                        server=str(getattr(who, "server", "") or ""),
                                        config_path=registry)

            def exit_gate() -> str:
                # Closing is not the act the stand speaks about. The stand and `--live` are the
                # operator's word about putting new money in; a mirror that stops closing when he
                # parks the machine leaves real lots whose only exit is a process that is not running.
                who = mt5.account_info()
                return livegate.exit_refusal(getattr(who, "login", None),
                                             server=str(getattr(who, "server", "") or ""))

            reason = gate()
            if reason:
                # Said, and then the cycle goes on to do the half of its job the refusal says nothing
                # about: this used to be `return 1`, which turned the kill switch into a frozen book.
                log({"event": f"открытий нет: {reason}; свои лоты продолжаю закрывать"})
            row = livegate.permitted_account(real_account.login,
                                             server=str(real_account.server or ""),
                                             config_path=registry)
            if row is None:
                if not reason:
                    # The gate answered a moment ago and this read answers nothing. A registry that
                    # moved between the two is a registry nobody can trade on, and the `None` below
                    # would reach for a name, raise, and be swallowed as "cycle error" — the loop
                    # would do it again every interval with nothing in the log but a shrug.
                    log({"event": "стоп: реестр не отдаёт запись этого счёта, ни один ордер не шлёт"})
                    mt5.shutdown()
                    return 1
                # Already refused by the stand or by `--live`: there is nothing to open, so a missing
                # row cannot cost anything here except the lot number this cycle will not use.
                lots = 0.0
            else:
                lots = livegate.capped_lots(row, args.real_lots)
            if row is not None and lots < args.real_lots:
                log({"event": f"лот срезан по реестру: {args.real_lots} → {lots} "
                              f"(max_lots записи {row.name})"})
            real_book = book_of(mt5)
            if real_book is None:
                # Not a flat account: a question nobody answered. Read as `[]` it would say three
                # things at once, and all three are wrong — that the daily stop has nothing left in
                # the market to prove, that no symbol is mirrored yet so every growing demo leg is
                # still buyable, and that there is no own lot to sell early.
                log({"event": "реальный счёт не ответил на вопрос о позициях: в этом цикле ни "
                              "копий, ни выходов, ни подтверждения дневного стопа"})
                mt5.shutdown()
                if args.once:
                    return 1
                time.sleep(args.interval_sec)
                continue
            if state.get("day_start_equity") is None:
                state["day_start_equity"] = real_account.equity
                save_state(state)
            limit = state["day_start_equity"] * (1 - args.daily_stop_pct / 100)
            if real_account.equity < limit and not state.get("halted"):
                for position in [p for p in real_book
                                 if p.magic == REAL_MAGIC]:
                    tick = mt5.symbol_info_tick(position.symbol)
                    send_order(mt5, {"action": mt5.TRADE_ACTION_DEAL,
                                     "symbol": position.symbol, "volume": position.volume,
                                     "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                             else mt5.ORDER_TYPE_BUY,
                                     "position": position.ticket,
                                     "price": tick.bid if position.type == 0 else tick.ask,
                                     "deviation": 30, "magic": REAL_MAGIC,
                                     "type_filling": mt5.ORDER_FILLING_FOK,
                                     "comment": "daily stop"}, exit_gate)
                # "Everything of ours is closed" is a claim about the broker, not about this loop:
                # the send above only proves an order_send call, and a rejected one used to be enough
                # to set the halt — after which every later cycle refused to try the exit again. The
                # witness has to answer, too: an unanswered question is not a flat book, and marking
                # the day halted on one would tell every later cycle that the exit it never made is
                # already done.
                witness = book_of(mt5)
                if witness is None:
                    # Deliberately not a `continue`: the rest of this cycle still has work the daily
                    # stop does not own, and jumping out of it would also leak the terminal connection.
                    log({"event": "дневный стоп не подтверждён: счёт не ответил на второй вопрос о "
                                  "позициях, повторю в следующем цикле"})
                else:
                    remaining = [p for p in witness if p.magic == REAL_MAGIC]
                    if remaining:
                        log({"event": f"дневный стоп не подтверждён: {len(remaining)} своего лота "
                                      "ещё в рынке, повторю в следующем цикле"})
                    else:
                        state["halted"] = True
                        save_state(state)
                        log({"event": f"DAILY STOP: real equity {real_account.equity:.2f} below "
                                      f"{limit:.2f}, всё своё закрыто"})

            # The dedup below is only as good as this list: a book that did not answer would leave it
            # empty and every growing demo leg would look unmirrored.
            real_positions = [p for p in real_book if p.magic == REAL_MAGIC]
            other = len(real_book) - len(real_positions)
            if other:
                log({"event": f"{other} лот(ов) на реальном счёте не наши (magic != {REAL_MAGIC}): "
                              f"не тронуты"})
            mirrored = {position.symbol: position for position in real_positions}

            # The two halves of a mirror cycle answer different questions. Getting out is asked
            # of every cycle, whatever the entry door says: `halted`, the stand and a missing
            # `--live` are all words about money going IN, and a lot the demo already let go is
            # still ours to let go. `row.name` is only reachable when a row answered, so the
            # ceiling message names the account instead of crashing on None.
            entry_open = not state.get("halted") and not reason and row is not None
            if not entry_open:
                log({"event": "копий не добавляю: " + (
                    "день остановлен" if state.get("halted") else
                    reason or "реестр не отдаёт запись этого счёта")})
            # --- 3. close what the demo has already closed ---
            open_demo_symbols = {item["symbol"] for item in demo_positions}
            for symbol, position in list(mirrored.items()):
                if symbol in open_demo_symbols:
                    continue
                tick = mt5.symbol_info_tick(symbol)
                result = send_order(mt5, {
                    "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                    "volume": position.volume,
                    "type": mt5.ORDER_TYPE_SELL if position.type == 0
                            else mt5.ORDER_TYPE_BUY,
                    "position": position.ticket,
                    "price": tick.bid if position.type == 0 else tick.ask,
                    "deviation": 30, "magic": REAL_MAGIC,
                    "type_filling": mt5.ORDER_FILLING_FOK,
                    "comment": "demo closed"}, exit_gate)
                log({"event": f"{symbol}: демо закрыл, реальная копия "
                              f"{'закрыта' if result else 'НЕ закрыта'} rtc "
                              f"{None if result is None else result.retcode}"})

            # --- 4. mirror what is growing ---
            # This step used to sit inside the loop above, at its deepest indent, which broke the
            # mirror in both directions at once. One demo close re-ran the whole mirroring pass, so
            # the same growing demo ticket could be bought on real money once per closed symbol in one
            # cycle — the dedup below compares against the book as it was at the start of the cycle,
            # and a fill does not appear there until the next one. And a real account holding nothing
            # never mirrored anything, because the loop it lived in had nothing to iterate over: every
            # fresh day, before the first copy existed, was silently a day without mirroring.
            candidates = [item for item in demo_positions
                          if item["symbol"] not in mirrored
                          and item["profit"] >= args.min_position_profit]
            if entry_open and growth and candidates:
                # The volume belongs to the account the terminal names at this moment, exactly like
                # the consent does: a ceiling read from the account this cycle happened to ask first
                # is a lot sized for money that may no longer be in this window.
                who = mt5.account_info()
                permitted = livegate.permitted_account(getattr(who, "login", None),
                                                       server=str(getattr(who, "server", "") or ""),
                                                       config_path=registry)
                lots = livegate.capped_lots(permitted, args.real_lots)
                if permitted is None:
                    # Two different facts wear the same `lots == 0`. A row that caps the lot says the
                    # operator set the ceiling; a window that stopped naming the account this process
                    # was started for says the money in front of it is somebody else's. The second one
                    # is the one that must be readable, because it is the one that arrives unannounced.
                    log({"event": f"не зеркалит: перед этим запуском окно отвечает "
                                  f"{getattr(who, 'login', None)}/"
                                  f"{getattr(who, 'server', '') or 'без сервера'}, записи о котором "
                                  f"в реестре нет; свои лоты при этом продолжает закрывать"})
                elif lots <= 0:
                    log({"event": f"не зеркалит: потолок реестра для "
                                  f"{getattr(permitted, 'name', row.name)} — {lots} лота, "
                                  f"открытий не будет; свои лоты при этом продолжает закрывать"})
                else:
                    bought: set[str] = set()
                    for item in candidates:
                        if item["symbol"] in bought:
                            continue
                        if item["type"] == 1 or not hourly_uptrend(mt5, item["symbol"]):
                            log({"event": f"{item['symbol']}: не зеркалировано — реальный счёт "
                                          f"покупает только то, что действительно растёт"})
                            continue
                        tick = mt5.symbol_info_tick(item["symbol"])
                        if not tick:
                            continue
                        if not item["sl"] and not item["tp"]:
                            # The rule the file bridge already refuses an open with, in the other
                            # script that opens on the same account: a copy with neither level has no
                            # exit but this process's next cycle, and a process survives neither a
                            # crash nor a closed laptop nor a machine the operator parked.
                            log({"event": f"{item['symbol']}: не зеркалировано — у демо-билета нет "
                                          f"ни стопа, ни цели; реальный лот без защитного уровня "
                                          f"не покупаю"})
                            continue
                        side = mt5.ORDER_TYPE_BUY if item["type"] == 0 else mt5.ORDER_TYPE_SELL
                        price = tick.ask if item["type"] == 0 else tick.bid
                        request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": item["symbol"],
                                   "volume": lots, "type": side, "price": price,
                                   "deviation": 30, "magic": REAL_MAGIC,
                                   "type_filling": mt5.ORDER_FILLING_FOK,
                                   "comment": "mirror demo"}
                        if item["sl"]:
                            request["sl"] = item["sl"]
                        if item["tp"]:
                            request["tp"] = item["tp"]
                        result = send_order(mt5, request, gate)
                        if result is None:
                            continue
                        # `bought` records the fills, not the attempts: a copy the broker refused is
                        # not in the market, so the next candidate of the same symbol may still be
                        # bought, and the journal has to say which of the two happened.
                        if result.retcode == 10009:
                            bought.add(item["symbol"])
                        log({"event": f"{item['symbol']}: "
                                      f"{'зеркалит' if result.retcode == 10009 else 'НЕ отзеркалено'} "
                                      f"демо билет {item['ticket']} по {price:.5f} лот {lots} "
                                      f"rtc {result.retcode}"})
            # sell a little earlier than the demo: own small target, own time limit
            opened = state.setdefault("real_opened", {})
            for position in real_positions:
                key = str(position.ticket)
                opened.setdefault(key, time.time())
                age_min = (time.time() - opened[key]) / 60
                if position.profit >= args.real_tp_usd or age_min >= args.max_hold_min:
                    tick = mt5.symbol_info_tick(position.symbol)
                    result = send_order(mt5, {
                        "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                        "volume": position.volume,
                        "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                else mt5.ORDER_TYPE_BUY,
                        "position": position.ticket,
                        "price": tick.bid if position.type == 0 else tick.ask,
                        "deviation": 30, "magic": REAL_MAGIC,
                        "type_filling": mt5.ORDER_FILLING_FOK,
                        "comment": "real early exit"}, exit_gate)
                    if result is not None:
                        opened.pop(key, None)
                        log({"event": f"{position.symbol}: копия зафиксирована раньше демо на "
                                      f"{position.profit:+.2f} после {age_min:.0f} мин, rtc "
                                      f"{result.retcode}"})
            state["real_opened"] = opened
            save_state(state)
            mt5.shutdown()
            log({"event": f"HEARTBEAT demo_trend {trend:+.2f} growth {growth} лот {lots} "
                          f"real_equity {real_account.equity:.2f} mirrored {len(mirrored)}"})
            if args.once:
                return 0
        except Exception as exc:  # noqa: BLE001 - the bridge must never die
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
            if args.once:
                return 1
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
