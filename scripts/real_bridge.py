"""Real-account bridge: the EA in the real terminal and this process share two files.

The MetaTrader5 Python API can only attach to one terminal at a time, and the demo swarm owns
that channel. So the real account is driven by the inverse arrangement: an Expert Advisor lives
inside the real terminal and exchanges files through the common MQL5\\Files folder.

    EA  -> dsh_real_state.json   (account, equity, positions with their magic) every second
    EA  <- dsh_real_cmd.json     (one command: open / close), read and deleted

Nothing in this file sends by default. `send_command` is the only door, and it asks `fleet.livegate`
on every knock — two questions, depending on which way the money is going. An OPENING needs all
three words: the stand (`C:\\mt5-fleet\\PAUSE`) gone, `--live` on this invocation, and a registry row
holding this login as a real account with `enabled: true` on the server the terminal named. A CLOSING
of a lot stamped with this bridge's magic asks only that the broker named the account it is closing
(`livegate.exit_refusal`): the stand and `--live` are the operator's word about money going in, and a
bridge that would not take money out while he holds the stand leaves a real position whose only exit is
a broker stop. The account is not
hardcoded here any more than the money is — an earlier version carried
`276261159@Exness-MT5Real26` in the source, which made any copy of this file a loaded gun pointed at
one specific live account.

A written command is a trigger left in another process's hands: the EA reads the common folder at its
own pace, after this process is gone, on whatever terminal it happens to be attached to. So every
command carries the moment it stops being an order (`exp`), the EA refuses one that has passed, and a
cycle that finds the gate shut takes the file back out of the folder instead of leaving it armed.

And it closes only the lots it recognises as its own: the EA stamps `REAL_MAGIC` on what it opens,
and a position with any other magic is the operator's money, which this process may count but must
never close. The lot it opens is capped by that same registry row: the script's own default is not a
permission to trade more than the operator allowed this account.
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

LOG = ROOT / "agent_data" / "real_bridge.jsonl"
STATE = ROOT / "agent_data" / "real_bridge_state.json"
COMMON = Path(r"C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\Common\Files")
EA_STATE = COMMON / "dsh_real_state.json"
EA_CMD = COMMON / "dsh_real_cmd.json"
DEMO_TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
# The magic DSHRealBridge.mq5 stamps on everything it opens. A position with any other magic is the
# operator's own lot: this process may count it in the risk it reports, but it must never close it.
REAL_MAGIC = 8272026
DAILY_STOP_PCT = 0.5
REAL_LOTS = 0.01
# How far from the fill the EA must put the broker's stop, in percent of price. This is the bridge's
# own number and not the registry's: the registry caps how MUCH of the account may be traded, and
# nothing in it says where a lot stops being a loss. 0 disables openings altogether rather than
# letting them go naked.
REAL_STOP_PCT = 0.6
# How long a command written into the common folder stays an order. The EA checks it against its own
# clock: a file that sat there because nobody was attached is not something to trade when somebody
# finally is.
CMD_TTL_SEC = 60


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


def read_ea_state(path: pathlib.Path = EA_STATE) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None


def ours(ea: dict) -> list[dict]:
    """The real positions this bridge opened — and only those.

    A state file written by an EA that predates the `magic` field yields nothing here: no field, no
    close. Guessing that an unlabelled lot was ours would mean closing the operator's trade.
    """
    lots = our_lots(ea)
    return [] if lots is None else lots


def our_lots(ea: dict) -> list[dict] | None:
    """The same list for a caller that has to tell a flat book from an unanswered one."""
    reported = reported_positions(ea)
    if reported is None:
        return None
    return [p for p in reported if int(p.get("magic") or 0) == REAL_MAGIC]


def reported_positions(ea: dict) -> list[dict] | None:
    """Every lot the advisor published, or None when its file carries no list at all.

    The advisor writes `"positions":[...]` on every pass, including the flat one, so a file without
    that key is a torn write or a build from before the field — a question nobody answered. As an
    empty list it answers the one question the daily stop waits for before it ends the day, and the
    bridge then reports "all of ours are out" over lots still riding on real money; as an empty list
    for the openings it says "nothing is mirrored yet" and the next cycle buys a second lot on a
    symbol that already has one.
    """
    positions = ea.get("positions")
    return None if positions is None else list(positions)


def withdraw(cmd_path: pathlib.Path) -> bool:
    """Take an armed *opening* back out of the folder the EA reads.

    Permission is not a property of a file. A command written while the gate was open stays on disk
    after the stand comes back, and the next EA attach trades it as though the operator had just
    said yes — which is how a script that refuses every cycle still spends money.

    A close is not that file. `close` is the lot leaving the market, and the stand is the operator's
    word about *new* money: taking a protective exit out of the folder would park a real position
    with nobody left to get rid of it. So a close stays where it is and gets named in the log.
    """
    if not cmd_path.exists():
        return True
    pending = ""
    try:
        pending = str(json.loads(cmd_path.read_text(encoding="utf-8", errors="replace"))
                      .get("action") or "")
    except (OSError, ValueError):
        pending = "не разобрано"
    if pending == "close":
        log({"event": f"стоянка не снимает защиту: close в {cmd_path} остаётся в силе"})
        return True
    try:
        cmd_path.unlink()
    except OSError as exc:
        log({"event": f"НЕ СНЯЛ команду {cmd_path}: {type(exc).__name__}: {exc} — "
                      "она ещё может быть исполнена"})
        return False
    log({"event": f"снял команду {cmd_path}: разрешения нет, ордер не отправлен"})
    return True


def send_command(command: dict, gate, cmd_path: pathlib.Path = EA_CMD, *, login=None,
                 server: str = "", now=time.time) -> bool:
    """The only door to a real account. `gate()` answers why it may not be walked right now.

    Only for openings. A close asks `livegate.exit_refusal`, which asks where this lot is — the login
    and the server the broker answered — but not the stand or `--live`, because those are the
    operator's words about putting money IN, not about taking it out.
    """
    closing = str(command.get("action") or "") == "close"
    reason = livegate.exit_refusal(login, server=server) if closing else gate()
    if reason:
        log({"event": f"NOT SENT {command}: {reason}"})
        withdraw(cmd_path)
        return False
    try:
        named = int(login)
    except (TypeError, ValueError):
        # A command with no account named in it is a command any attached terminal may execute, and
        # two of the EAs in this project read from one common folder. The EA checks this number
        # against its own; writing it empty would hand the order to whichever window woke up first.
        log({"event": f"NOT SENT {command}: брокер не назвал счёт — команда без адреса не пишется"})
        withdraw(cmd_path)
        return False
    payload = dict(command, login=named, exp=int(now()) + CMD_TTL_SEC)
    cmd_path.parent.mkdir(parents=True, exist_ok=True)
    # Compact, exactly like every other file this project hands to MQL5: `JsonStr` looks for
    # `"action":"`, and the default `json.dumps` spacing writes `"action": "`, which the EA reads as
    # an empty field and drops. A file the executor cannot parse is not a refused order, it is a
    # silently lost one, and the log here would still say it was sent.
    cmd_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    log({"event": f"command to EA: {payload}"})
    return True


def demo_book() -> list[dict] | None:
    import MetaTrader5 as mt5

    if not mt5.initialize(path=DEMO_TERMINAL, timeout=4000):
        return None
    try:
        account = mt5.account_info()
        if account is None or account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            return None
        positions = mt5.positions_get()
        if positions is None:
            # A dropped channel is not a flat demo account. Read as `[]` it tells the cycle below that
            # the demo let every symbol go, and every real copy is sold on an answer nobody gave.
            return None
        return [{"ticket": p.ticket, "symbol": p.symbol, "type": p.type,
                 "volume": p.volume, "profit": p.profit, "sl": p.sl, "tp": p.tp}
                for p in positions]
    finally:
        mt5.shutdown()


def hourly_uptrend(symbol: str) -> bool:
    import MetaTrader5 as mt5

    if not mt5.initialize(path=DEMO_TERMINAL, timeout=4000):
        return False
    try:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 60)
        if rates is None or len(rates) < 50:
            return False
        closes = [float(r["close"]) for r in rates]

        def ema(values, period):
            weight = 2 / (period + 1)
            result = values[0]
            for value in values[1:]:
                result = value * weight + result * (1 - weight)
            return result

        return closes[-1] > ema(closes, 20) > ema(closes, 50)
    finally:
        mt5.shutdown()


def run_cycle(state: dict, gate, *, ea_path: pathlib.Path = EA_STATE,
              cmd_path: pathlib.Path = EA_CMD,
              registry: pathlib.Path | None = None) -> str:
    """One pass over the two files; returns a sentence for the log.

    The entry door and the protective door are walked separately, on purpose. `gate()` says whether
    this cycle may put new money into the market; it never says anything about getting old money
    out, and a bridge that stops closing when the machine is parked turns the kill switch into an
    unmanaged real position. That is also why the demo book is read before the idle return: the
    closer needs to know what the demo let go, and a copy the demo has already flattened is not new
    money going in.
    """
    entry_refused = gate()
    ea = read_ea_state(ea_path)
    if ea is None:
        if entry_refused:
            # Nothing to protect: with no witness attached there is no lot of ours anywhere in the
            # answer, and an unpermitted cycle still must not leave an open armed.
            withdraw(cmd_path)
            return f"bridge idle: {entry_refused}"
        return "no EA state yet — is DSHRealBridge attached in the real terminal?"

    def emit(command: dict) -> bool:
        """Every command this cycle writes is addressed to the account the EA reported."""
        return send_command(command, gate, cmd_path, login=ea.get("login"),
                            server=ea.get("server"))

    def pending() -> list[dict] | None:
        """Our lots as the advisor last reported them: the state file is rewritten every second.

        None means the second question went unanswered — the file is gone, torn, or written by a build
        that publishes no positions. It used to fall back to the answer this cycle had already read,
        which is the book from before the closes it is now confirming.
        """
        fresh = read_ea_state(ea_path)
        return None if fresh is None else our_lots(fresh)

    if float(ea.get("balance") or 0) <= 0:
        return f"real balance {ea.get('balance')} — no funds, bridge idle"

    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("day") != today:
        state["day"] = today
        state["day_start_equity"] = float(ea.get("equity") or 0)
        state["halted"] = False
        save_state(state)
    day_start = float(state.get("day_start_equity") or ea.get("equity") or 0)
    equity = float(ea.get("equity") or 0)
    if day_start and equity < day_start * (1 - DAILY_STOP_PCT / 100) and not state.get("halted"):
        for position in ours(ea):
            emit({"action": "close", "ticket": position["ticket"]})
            # One command file and an advisor that reads it once a second: two writes in the same
            # millisecond are one write, and a protective loop that believes it closed three lots has
            # armed the last one and lost the other two.
            time.sleep(2)
        left = pending()
        if left is None:
            # The end of the day is a claim about the broker, and the broker has not answered. Read as
            # an empty book, a torn or pre-`positions` file would let the bridge halt the day and stop
            # trying while our lots were still riding on real money.
            log({"event": "дневный стоп не подтверждён: советник не прислал список лотов — "
                          "день не объявлен остановленным, следующий цикл повторит"})
            return (f"DAILY STOP UNPROVEN: equity {equity} below {day_start:.2f}, "
                    "книга реальных лотов не получена")
        if left:
            # A halt is a claim that the money is out of the market. Writing it because a file was
            # written — which is all `emit` proves — used to end the day with the lots still open and
            # with every later cycle refusing to retry them. So the claim waits for the witness.
            log({"event": f"дневный стоп не подтверждён: {len(left)} нашего лота ещё в рынке, "
                          "следующий цикл повторит закрытие"})
            return f"DAILY STOP UNPROVEN: equity {equity} below {day_start:.2f}, осталось {len(left)}"
        state["halted"] = True
        save_state(state)
        return f"DAILY STOP: equity {equity} below {day_start:.2f}"
    if state.get("halted"):
        # "Halted" is the end of opening, not a ban on exits: a lot that outlived its own daily stop
        # has to keep meeting a bridge that tries to get it out.
        still = ours(ea)
        for position in still:
            emit({"action": "close", "ticket": position["ticket"]})
            time.sleep(2)
        if reported_positions(ea) is None:
            return ("halted for the day: советник не прислал список лотов, свой лот мог остаться "
                    "в рынке — повторю в следующем цикле")
        return ("halted for the day" if not still
                else f"halted for the day: повторяю закрытие {len(still)} своего лота")

    book = demo_book()
    if book is None:
        return "demo book unavailable this cycle"
    our_positions = ours(ea)
    reported = reported_positions(ea)
    if reported is not None and len(our_positions) < len(reported):
        log({"event": f"{len(reported) - len(our_positions)} лот(ов) на реальном счёте не наши "
                      f"(magic != {REAL_MAGIC}): не закрыты и не тронуты"})
    demo_symbols = {item["symbol"] for item in book}

    # Close our own copy when the demo closed its own — above the entry door, on purpose.
    # `gate()` is the operator's word about money going IN; a real lot whose demo twin is already flat
    # is money that has to come out whether or not this cycle is allowed to open anything. This block
    # sat under the idle return, so putting up the stand left a copy riding on real money with nothing
    # but the broker's own stop.
    for position in our_positions:
        if position["symbol"] not in demo_symbols:
            emit({"action": "close", "ticket": position["ticket"]})
            time.sleep(2)

    if entry_refused:
        # Asked before any open is written: a cycle nobody permitted does not copy the demo book, and
        # it takes back any open an earlier permitted cycle left armed.
        withdraw(cmd_path)
        return f"bridge idle: {entry_refused}"

    if reported is None:
        # An empty `held` read off an unanswered book is "nothing is mirrored yet", and this loop
        # copies the demo book symbol by symbol: the next cycle would buy a second real lot on top of
        # the first. Openings wait for the answer; exits above never did.
        log({"event": "открытий нет: советник не прислал список реальных лотов — не зная, что уже "
                      "держится, легко открыть второй лот на тот же символ"})
        return (f"NO BOOK: real {ea.get('login')} позиции не сообщены, "
                f"открытия запрещены, демо {len(book)}")
    if not day_start:
        # The daily stop measures this cycle's equity against the equity the day started with. With
        # no starting number there is no reference, and a mirrored lot nobody can watch for a drawdown
        # is a lot whose only exit is the broker's own stop.
        log({"event": "открытий нет: дневный капитал не измерен — за лотом не следил бы ни один "
                      "дневный стоп"})
        return f"NO DAY MEASURE: real {ea.get('login')} открытия запрещены, демо {len(book)}"
    held = {p["symbol"] for p in reported}
    # mirror only profitable demo buys in a confirmed uptrend
    # The row that lets this account trade is also the row that says how much of it may be traded.
    # The script's own default is not a permission: 0.01 onto an account the operator capped at 0.005
    # is a bigger position than the registry allows, and this file used to never ask `capped_lots`.
    row = livegate.permitted_account(ea.get("login"), server=str(ea.get("server") or ""),
                                     config_path=registry)
    lots = livegate.capped_lots(row, REAL_LOTS)
    if lots <= 0:
        # A ceiling of zero stops opening, not closing: a lot the registry refuses to add is still a
        # lot the daily stop has to be able to get rid of.
        log({"event": f"открытий нет: реестр не даёт лота для {ea.get('login')} (потолок {lots})"})
    if REAL_STOP_PCT <= 0:
        log({"event": "открытий нет: REAL_STOP_PCT выключен, а рынок без стоп-уровня "
                      "на настоящих деньгах — это лот, который закрывает только этот процесс"})
        return f"NO STOP LEVEL: real {ea.get('login')} открытия запрещены, демо {len(book)}"
    opened = state.setdefault("opened", {})
    for item in [] if lots <= 0 else book:
        if item["type"] != 0 or item["symbol"] in held or item["profit"] <= 0:
            continue
        if not hourly_uptrend(item["symbol"]):
            continue
        # The stop travels as a percentage because this process does not know the price the EA will
        # fill at; the EA turns it into a broker-side level before it sends. An open command without
        # it is deliberately unwritable: a mirrored real lot whose only exit is this script's next
        # cycle is a lot that survives a crash, a closed laptop and a parked machine.
        if emit({"action": "open", "symbol": item["symbol"], "side": 0, "lots": lots,
                 "stop_pct": REAL_STOP_PCT}):
            opened[item["symbol"]] = time.time()
            save_state(state)
            time.sleep(2)
    return f"HEARTBEAT real {ea.get('login')} equity {equity} демо {len(book)} лот {lots}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Real-account bridge (шлёт только с --live)")
    parser.add_argument("--live", action="store_true",
                        help="единственный способ разрешить этому запуску тронуть реальные деньги")
    parser.add_argument("--config", default="",
                        help="реестр счетов; по умолчанию config/accounts.yaml")
    parser.add_argument("--interval-sec", type=int, default=10)
    parser.add_argument("--once", action="store_true", help="один цикл и выйти")
    args = parser.parse_args(argv)
    registry = pathlib.Path(args.config).resolve() if args.config else None

    def gate() -> str:
        ea = read_ea_state(EA_STATE) or {}
        return livegate.refusal(ea.get("login"), live=args.live,
                                server=str(ea.get("server") or ""), config_path=registry)

    state = load_state()
    log({"event": "REAL BRIDGE START (file channel, EA in the real terminal)"})
    if not args.once:
        reason = gate()
        if reason:
            # Said, and the bridge starts anyway. This used to `return 1`: a supervisor or a reboot
            # that brought the bridge back while the stand was up, or without the word `--live`, gave
            # up before its first cycle — and the lots a previously permitted run left in the market
            # lost their only closer exactly when the operator touched the machine. The refusal is a
            # word about openings; `run_cycle` asks it again at every send and still runs the exits.
            log({"event": f"открытий нет: {reason}; закрыватель работает"})
    while True:
        try:
            # Named here instead of left to the defaults, so that a caller who moves the two files
            # (a test, or a second deployment) moves them for the whole run — the function defaults
            # were bound to this machine's shared folder when the module was imported.
            log({"event": run_cycle(state, gate, ea_path=EA_STATE, cmd_path=EA_CMD,
                                    registry=registry)})
        except Exception as exc:  # noqa: BLE001 - the bridge must never die
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        if args.once:
            return 0
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
