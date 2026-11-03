#!/usr/bin/env python3
"""A swarm of trading agents on one demo account, one process, many strategies.

Each agent has its own symbol, rule, lot size and magic number, so their positions never
mix. Running them in one process keeps the terminal load sane; separate processes at this
count would fight over the API.

The strategies are the ones the scans actually found, each with its result:

  monday_long         long the pair at the Monday open, close at the Monday close
                      GBPUSD t=4.9, AUDUSD t=4.8, USDCHF t=3.0, EURUSD t=2.5, USDJPY t=2.2
  monday_gap_fill     the weekend gap closes during Monday, with a 0.5% stop
                      basket t=7.4 over 275 trades
  monday_gap_continue gold does the opposite: the gap continues, t=2.4
  first_hour          the first hour of the server day is positive, t=5 to 11

Agents marked experimental have weaker evidence and trade a quarter of the size; they are
there to be measured, not because they are trusted.

Demo only. Run: python scripts/agent_swarm.py
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "agent_data" / "swarm.jsonl"
MAGIC_BASE = 8180000
DEMO_TERMINAL = r"C:\mt5-exness\terminal64.exe"
DESK_MAGIC = 8189999   # the lead's own hand-placed trades: watched, never re-entered by an agent
PARALLEL_MAGIC = 8172026
EXPECTED_LOGIN = 114929502
SERVER_OFFSET = 2
ENTRY_SERVER_HOUR = 0      # Monday 00:00 server = Sunday 22:00 UTC
EXIT_SERVER_HOUR = 21
FIRST_HOUR_UTC = 22        # entry hour for the rollover agents
FIRST_HOUR_EXIT_MINUTE = 55


def agent(name: str, kind: str, symbol: str, lots: float, *, experimental: bool = False,
          stop: float = 0.005) -> dict:
    return {"name": name, "kind": kind, "symbol": symbol, "lots": lots,
            "experimental": experimental, "stop": stop}


AGENTS: list[dict] = [
    # --- gold and silver: the only instruments with a real edge on this venue --------
    agent("ema-xau", "h1_ema", "XAUUSD", 0.04),
    agent("ema-xag", "h1_ema", "XAGUSD", 0.03),
    # --- Monday long, the strongest calendar effect -------------------------------
    agent("mon-long-gbp", "monday_long", "GBPUSD", 0.03),
    agent("mon-long-aud", "monday_long", "AUDUSD", 0.03),
    agent("mon-long-chf", "monday_long", "USDCHF", 0.03),
    agent("mon-long-eur", "monday_long", "EURUSD", 0.03),
    agent("mon-long-jpy", "monday_long", "USDJPY", 0.03),
    # --- Monday gap fill -----------------------------------------------------------
    agent("gap-gbp", "monday_gap_fill", "GBPUSD", 0.03),
    agent("gap-chf", "monday_gap_fill", "USDCHF", 0.03),
    agent("gap-aud", "monday_gap_fill", "AUDUSD", 0.03),
    agent("gap-cad", "monday_gap_fill", "USDCAD", 0.03),
    agent("gold-gap", "monday_gap_continue", "XAUUSD", 0.01),
    # --- First hour of the server day ---------------------------------------------
    agent("fh-chf", "first_hour", "USDCHF", 0.03),
    agent("fh-aud", "first_hour", "AUDUSD", 0.03),
    agent("fh-gbp", "first_hour", "GBPUSD", 0.03),
    agent("fh-eur", "first_hour", "EURUSD", 0.03),
    agent("fh-jpy", "first_hour", "USDJPY", 0.03),
    agent("fh-cad", "first_hour", "USDCAD", 0.03),
    # --- scaling units on the two schemes currently in profit --------------------
    agent("ema-xau-b", "h1_ema", "XAUUSD", 0.03),
    agent("mon-long-gbp-b", "monday_long", "GBPUSD", 0.02),
    # --- session drift: US index rises inside the US session ----------------------
    agent("us500-session", "session_long", "US500", 0.1),
    agent("us500-mon-session", "session_long", "US500", 0.1),
    # --- weekly momentum on metals (5-day return, hold 5 days) --------------------
    agent("wm-xau", "weekly_momentum", "XAUUSD", 0.02),
    agent("wm-xag", "weekly_momentum", "XAGUSD", 0.01),
    # --- silver experiment: the same three rules measured on one metal -----------------
    agent("sil-ema", "h1_ema", "XAGUSD", 0.02, experimental=True),
    agent("sil-mom", "momentum_buy", "XAGUSD", 0.02, experimental=True),
    agent("sil-dip", "dip_buy", "XAGUSD", 0.02, experimental=True),
    agent("sil-week", "weekly_momentum", "XAGUSD", 0.01, experimental=True),
    # --- the operator's own two rules: buy growth, buy the extreme dip ------------
    agent("mom-xau", "momentum_buy", "XAUUSD", 0.01, experimental=True),
    agent("mom-xag", "momentum_buy", "XAGUSD", 0.01, experimental=True),
    agent("mom-eur", "momentum_buy", "EURUSD", 0.01, experimental=True),
    agent("mom-gbp", "momentum_buy", "GBPUSD", 0.01, experimental=True),
    agent("dip-xau", "dip_buy", "XAUUSD", 0.01, experimental=True),
    agent("dip-xag", "dip_buy", "XAGUSD", 0.01, experimental=True),
    agent("dip-eur", "dip_buy", "EURUSD", 0.01, experimental=True),
    agent("dip-gbp", "dip_buy", "GBPUSD", 0.01, experimental=True),
    # --- lab: the same EMA rule on FX, to keep the laboratory generating evidence ---
    agent("lab-ema-gbp", "h1_ema", "GBPUSD", 0.01, experimental=True),
    agent("lab-ema-eur", "h1_ema", "EURUSD", 0.01, experimental=True),
    agent("lab-ema-jpy", "h1_ema", "USDJPY", 0.01, experimental=True),
    # --- experimental, weaker evidence, quarter size ------------------------------
    agent("x-ema-xag", "h1_ema", "XAGUSD", 0.01, experimental=True),
    agent("x-mon-long-uki", "monday_long", "UK100", 0.1, experimental=True),
    agent("x-gap-eur", "monday_gap_fill", "EURUSD", 0.01, experimental=True),
    agent("x-fh-xau", "first_hour", "XAUUSD", 0.01, experimental=True),
    agent("x-wm-xag", "weekly_momentum", "XAGUSD", 0.01, experimental=True),
    agent("x-tom-xau", "turn_of_month", "XAUUSD", 0.01, experimental=True),
]
# Magic numbers are derived from the agent NAME, not its position in the list: reordering
# or inserting agents must never hand an existing position to a different agent.
import zlib as _zlib

for index, item in enumerate(AGENTS):
    item["magic"] = MAGIC_BASE + (_zlib.crc32(item["name"].encode("utf-8")) % 5000)
_duplicates = len(AGENTS) - len({item["magic"] for item in AGENTS})
if _duplicates:
    raise SystemExit(f"magic collision between agents: {_duplicates} duplicates")
MAGIC_SET = {item["magic"] for item in AGENTS} | {DESK_MAGIC}
AGENT_BY_MAGIC = {item["magic"]: item for item in AGENTS}


def log(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")
    print(f"[{event['ts'][11:19]}] {event['event']}", flush=True)


def friday_close(symbol: str) -> float | None:
    import MetaTrader5 as mt5

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 24 * 8)
    if rates is None:
        return None
    for rate in reversed(rates):
        stamp = datetime.fromtimestamp(int(rate["time"]), tz=timezone.utc)
        if (stamp + timedelta(hours=SERVER_OFFSET)).weekday() == 4:
            return float(rate["close"])
    return None


def momentum_buy_state(symbol: str):
    """Buy in an uptrend while the last hourly bar is rising."""
    import MetaTrader5 as mt5

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 260)
    if rates is None or len(rates) < 220:
        return None
    closes = [float(rate["close"]) for rate in rates]
    highs = [float(rate["high"]) for rate in rates]
    lows = [float(rate["low"]) for rate in rates]

    def ema(values, period):
        weight = 2 / (period + 1)
        result = values[0]
        for value in values[1:]:
            result = value * weight + result * (1 - weight)
        return result

    fast, slow = ema(closes, 50), ema(closes, 200)
    ranges = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                  abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    atr = sum(ranges[-14:]) / 14
    if fast > slow and closes[-1] > fast and closes[-1] > closes[-2]:
        return "BUY", max(2 * atr, 0.0)
    return None


def dip_buy_state(symbol: str):
    """Buy after a fall of more than three ATRs in six hours once the low stops extending."""
    import MetaTrader5 as mt5

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 120)
    if rates is None or len(rates) < 40:
        return None
    closes = [float(rate["close"]) for rate in rates]
    highs = [float(rate["high"]) for rate in rates]
    lows = [float(rate["low"]) for rate in rates]
    ranges = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                  abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    atr = sum(ranges[-14:]) / 14
    if atr <= 0:
        return None
    recent_high = max(closes[-7:-1])
    drop = closes[-1] - recent_high
    reversed_up = closes[-1] > closes[-2] and lows[-1] >= min(lows[-3:])
    if drop < -2.5 * atr and reversed_up:
        return "BUY", max(2 * atr, 0.0)
    return None


def open_for(mt5, spec: dict) -> dict | None:
    positions = mt5.positions_get(symbol=spec["symbol"]) or []
    for position in positions:
        if position.magic == spec["magic"]:
            return position
    return None


def trend_clear_of_fast_average(mt5, symbol: str, side: str) -> bool:
    """The price must sit a quarter ATR away from the hourly EMA50 in the trade's direction.

    Without this the swarm kept shorting a rally at the average line: the signal said "below
    the 200 average" while the price was bouncing on the 50 average, and every such entry was
    a coin flip that paid the spread.

    The metals are exempt on the demo: the operator asked to see them traded and observed
    rather than filtered, so there only the EMA50/200 signal decides the direction.
    """
    if symbol in ("XAUUSD", "XAGUSD"):
        return True
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 60)
    if rates is None or len(rates) < 30:
        return True
    closes = [float(rate["close"]) for rate in rates]
    highs = [float(rate["high"]) for rate in rates]
    lows = [float(rate["low"]) for rate in rates]
    weight = 2 / 51
    fast = closes[0]
    for value in closes[1:]:
        fast = value * weight + fast * (1 - weight)
    ranges = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                  abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    atr = sum(ranges[-14:]) / 14
    # On the demo the operator explicitly wants the metals traded so the schemes can be
    # observed; there the price only has to be on the correct side of the average. FX keeps
    # the quarter-ATR clearance, which is what stopped the boundary whipsaw.
    buffer = 0.0 if symbol in ("XAUUSD", "XAGUSD") else 0.25 * atr
    price = closes[-1]
    if side == "SELL":
        return price < fast - buffer
    return price > fast + buffer


def daily_trend_state(symbol: str) -> str | None:
    """Return BUY if the last daily close is above its 200-day average, SELL if below."""
    import MetaTrader5 as mt5

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 20000)
    if rates is None or len(rates) < 3000:
        return None
    days: dict[str, float] = {}
    for rate in rates:
        stamp = datetime.fromtimestamp(int(rate["time"]), tz=timezone.utc)
        days[(stamp + timedelta(hours=SERVER_OFFSET)).date().isoformat()] = float(rate["close"])
    closes = [days[key] for key in sorted(days)]
    if len(closes) < 201:
        return None
    weight = 2 / 201
    ema = closes[0]
    for value in closes[1:]:
        ema = value * weight + ema * (1 - weight)
    if closes[-1] > ema * 1.001:
        return "BUY"
    if closes[-1] < ema * 0.999:
        return "SELL"
    return None


SKIP_UNTIL: dict[str, float] = {}


def market_retry_blocked(spec: dict) -> bool:
    """After an 'invalid volume' or 'market closed' answer, wait ten minutes."""
    return SKIP_UNTIL.get(spec["name"], 0) > time.time()


def h1_ema_state(symbol: str) -> tuple[str, float] | None:
    """EMA 50/200 on hourly bars, stop 2xATR floored at 3x spread, target 2R."""
    import statistics as st

    import MetaTrader5 as mt5

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 400)
    if rates is None or len(rates) < 210:
        return None
    bars = [{"h": float(r["high"]), "l": float(r["low"]), "c": float(r["close"])}
            for r in rates]
    closes = [bar["c"] for bar in bars]

    def ema(values, period):
        weight = 2 / (period + 1)
        result = values[0]
        for value in values[1:]:
            result = value * weight + result * (1 - weight)
        return result

    fast = ema(closes, 50)
    slow = ema(closes, 200)
    ranges = [max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - bars[i - 1]["c"]),
                  abs(bars[i]["l"] - bars[i - 1]["c"])) for i in range(1, len(bars))]
    atr = st.fmean(ranges[-14:])
    tick = mt5.symbol_info_tick(symbol)
    spread = (tick.ask - tick.bid) if tick else 0.0
    risk = max(2 * atr, spread * 3)
    # the slow pair alone is not a trend: price must also sit on the right side of the fast
    # average, otherwise a bounce inside a downtrend keeps triggering shorts into strength.
    if fast > slow and closes[-1] > slow and closes[-1] > fast:
        return "BUY", risk
    if fast < slow and closes[-1] < slow and closes[-1] < fast:
        return "SELL", risk
    return None


STATE = ROOT / "agent_data" / "swarm_state.json"
SYMBOL_CAP = {"XAUUSD": 0.10, "XAGUSD": 0.10, "EURUSD": 0.07, "GBPUSD": 0.07,
              "USDJPY": 0.07, "AUDUSD": 0.07, "USDCHF": 0.07, "USDCAD": 0.07,
              "NZDUSD": 0.07, "US500": 0.3, "UK100": 0.3, "US30": 0.3, "JPN225": 1.0}
MAX_DRAWDOWN = 0.01      # close everything if the account loses 1% from the baseline
PAUSE_SECONDS = 1800
DOLLAR_STOP = 10.0       # absolute floor: never hold a position ten dollars under water
STOP_RISK_FRACTION = 0.4 # ...but at least a fraction of the position's own risk
SCALE_TRIGGER = 15.0     # once a position is fifteen dollars up, add half again
COOLDOWN_SECONDS = 1800  # after a stop, that symbol rests for half an hour
MAX_ENTRIES_PER_HOUR = 3 # more than this on one symbol is churn, not trading
TRAIL_ARM = 12.0         # once a position is twelve dollars up, protect it
GIVEBACK_FRACTION = 0.35 # close once a third of the peak profit is given back
SCALE_TIERS = (15.0, 30.0, 50.0, 75.0)  # add half again at each of these
KILL_SWITCH = ROOT / "agent_data" / "demo_bridge" / "kill_switch.json"


def kill_switch_active() -> bool:
    """The documented brake, read the way the trading runner reads it.

    A switch that is missing or unreadable counts as armed. The swarm used to answer only to its
    own state file, so the operator's stop reached the runner and left this process trading.
    """
    try:
        raw = json.loads(KILL_SWITCH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return raw.get("kill_switch_active") is True


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state), encoding="utf-8")


def manage_portfolio(mt5, magics: set[int], state: dict) -> None:
    """One position per symbol per direction, capped size, and a global drawdown brake."""
    account = mt5.account_info()
    if account is None:
        return
    # A daily stop measured from the day's own starting equity, plus a total stop measured from
    # the initial capital. Measuring only from 100 000 made the brake permanent once the account
    # sat below -1%: every cycle flattened a flat book and refused to trade again.
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("day") != today or not state.get("day_start_equity"):
        state["day"] = today
        state["day_start_equity"] = account.equity
        state["halted_until"] = 0
        state["paused_until"] = 0
        save_state(state)
    day_start = float(state["day_start_equity"])
    total_floor = 100_000.0 * (1 - 0.03)
    daily_floor = day_start * (1 - MAX_DRAWDOWN)

    # 0. the shared brake. The account belongs to the person trading it in the terminal, so an
    # armed switch has to reach this process too; it read only its own state file before, which
    # is why the documented kill switch stopped the runner and not the swarm. A switch we cannot
    # read is treated as armed, the same way the trading runner treats a missing one.
    if kill_switch_active():
        log({"event": "kill switch armed; swarm idle this cycle"})
        return
    # Positions whose magic is not ours belong to the owner or to another agent. This process has
    # no authority over them and never did: flattening them was the churn the account paid for,
    # and the account's own manual trading is exactly what the observation order reserves.
    if account.equity < total_floor or account.equity < daily_floor:
        daily = account.equity < daily_floor
        log({"event": f"BREAKER: equity {account.equity:.2f} below "
                      f"{'the daily floor ' + format(daily_floor, '.2f') if daily else 'the total floor ' + format(total_floor, '.2f')}; "
                      f"flattening the swarm"})
        for position in mt5.positions_get() or []:
            if position.magic in magics:
                tick = mt5.symbol_info_tick(position.symbol)
                mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                "symbol": position.symbol, "volume": position.volume,
                                "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                        else mt5.ORDER_TYPE_BUY,
                                "position": position.ticket,
                                "price": tick.bid if position.type == 0 else tick.ask,
                                "deviation": 30, "magic": position.magic,
                                "type_filling": mt5.ORDER_FILLING_FOK,
                                "comment": "breaker"})
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        state["paused_until"] = time.time() + PAUSE_SECONDS
        # a daily stop halts until tomorrow; the total stop halts until a person lifts it
        state["halted_until"] = ((midnight + timedelta(days=1)).timestamp()
                                 if daily else time.time() + 365 * 86400)
        save_state(state)
        return

    # 1. cut a loser at the dollar level instead of waiting for the ATR stop.
    # The threshold scales with the position: ten dollars on a 0.05 lot gold trade is
    # inside the spread, so the floor alone would stop out every entry instantly.
    cooldowns = state.setdefault("cooldowns", {})
    survivors = []
    for position in [p for p in (mt5.positions_get() or []) if p.magic in magics]:
        info = mt5.symbol_info(position.symbol)
        contract = info.trade_contract_size if info else 100.0
        stop_distance = abs(position.price_open - position.sl) if position.sl else 0.0
        risk_dollars = stop_distance * position.volume * contract
        threshold = max(DOLLAR_STOP, STOP_RISK_FRACTION * risk_dollars)
        if position.profit <= -threshold:
            tick = mt5.symbol_info_tick(position.symbol)
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                "volume": position.volume,
                "type": mt5.ORDER_TYPE_SELL if position.type == 0 else mt5.ORDER_TYPE_BUY,
                "position": position.ticket,
                "price": tick.bid if position.type == 0 else tick.ask,
                "deviation": 30, "magic": position.magic,
                "type_filling": mt5.ORDER_FILLING_FOK,
                "comment": "dollar stop"})
            log({"event": f"{position.symbol} ticket {position.ticket}: dollar stop at "
                          f"{position.profit:+.2f} (threshold {threshold:.2f}), retcode "
                          f"{None if result is None else result.retcode}"})
            cooldowns[position.symbol] = time.time() + COOLDOWN_SECONDS
            state["cooldowns"] = cooldowns
            save_state(state)
            continue
        survivors.append(position)

    # 1b. protect a winner: move the stop to breakeven when it is up TRAIL_ARM and close
    # it if it gives back half of its peak profit. This is the "close before it falls"
    # rule: a trade that was up twelve dollars must not turn into a ten dollar loss.
    peaks = state.setdefault("peaks", {})
    for position in survivors:
        key = str(position.ticket)
        peak = max(peaks.get(key, 0.0), position.profit)
        peaks[key] = peak
        if peak < TRAIL_ARM:
            continue
        entry = position.price_open
        at_breakeven = (
            (position.type == 0 and position.sl >= entry)
            or (position.type == 1 and position.sl and position.sl <= entry)
        )
        if not at_breakeven:
            result = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP,
                                     "symbol": position.symbol,
                                     "position": position.ticket,
                                     "sl": round(entry, 5), "tp": position.tp})
            log({"event": f"{position.symbol} ticket {position.ticket}: peak "
                          f"{peak:+.2f}, stop to breakeven retcode "
                          f"{None if result is None else result.retcode}"})
        # take the profit while it is still there: once a trade has been up, close it when it
        # gives back the larger of five dollars and two fifths of its peak, instead of waiting
        # for it to turn into a loss. The breakeven stop is the last line, this is the first.
        giveback_floor = peak - max(5.0, 0.40 * peak)
        if position.profit <= max(1.0, giveback_floor):
            tick = mt5.symbol_info_tick(position.symbol)
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                "volume": position.volume,
                "type": mt5.ORDER_TYPE_SELL if position.type == 0 else mt5.ORDER_TYPE_BUY,
                "position": position.ticket,
                "price": tick.bid if position.type == 0 else tick.ask,
                "deviation": 30, "magic": position.magic,
                "type_filling": mt5.ORDER_FILLING_FOK,
                "comment": "trail exit"})
            log({"event": f"{position.symbol} ticket {position.ticket}: gave back "
                          f"from {peak:+.2f} to {position.profit:+.2f}, closed retcode "
                          f"{None if result is None else result.retcode}"})
            survivors = [p for p in survivors if p.ticket != position.ticket]
            continue
        if position.profit > 0 and peak_rejection(mt5, position):
            tick = mt5.symbol_info_tick(position.symbol)
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
                "volume": position.volume,
                "type": mt5.ORDER_TYPE_SELL if position.type == 0 else mt5.ORDER_TYPE_BUY,
                "position": position.ticket,
                "price": tick.bid if position.type == 0 else tick.ask,
                "deviation": 30, "magic": position.magic,
                "type_filling": mt5.ORDER_FILLING_FOK,
                "comment": "peak exit"})
            log({"event": f"{position.symbol} ticket {position.ticket}: rejected at the extreme "
                          f"with {position.profit:+.2f}; closed at the peak retcode "
                          f"{None if result is None else result.retcode}"})
            survivors = [p for p in survivors if p.ticket != position.ticket]

    # 2. scale into a winner in steps: the stronger the profit, the more is added
    levels = state.setdefault("scale_levels", {})
    for position in survivors:
        key = str(position.ticket)
        level = levels.get(key, 0)
        if level >= len(SCALE_TIERS) or position.profit < SCALE_TIERS[level]:
            continue
        add = round(position.volume * 0.5, 2)
        if add <= 0:
            continue
        cap = SYMBOL_CAP.get(position.symbol, 0.04)
        if swarm_exposure(position.symbol) + add > cap + 1e-9:
            levels[key] = level + 1
            continue
        tick = mt5.symbol_info_tick(position.symbol)
        result = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol, "volume": add,
            "type": mt5.ORDER_TYPE_BUY if position.type == 0 else mt5.ORDER_TYPE_SELL,
            "price": tick.ask if position.type == 0 else tick.bid,
            "deviation": 30, "magic": position.magic,
            "type_filling": mt5.ORDER_FILLING_FOK,
            "comment": "scale-in"})
        if result is not None and result.retcode == 10009:
            levels[key] = level + 1
            state["scale_levels"] = levels
            save_state(state)
            log({"event": f"{position.symbol} ticket {position.ticket}: up "
                          f"{position.profit:+.2f} at tier "
                          f"{SCALE_TIERS[level]:.0f}, added {add} lots"})

    # 3. the operator closes positions by hand sometimes. Notice it, thank them, and give the
    # symbol ten minutes before the agents try to re-enter it: otherwise a manual take-profit
    # is instantly re-opened by the same scheme at the same price.
    known = state.get("open_tickets", {})
    current = {}
    for position in mt5.positions_get() or []:
        if position.magic in magics:
            current[str(position.ticket)] = position.symbol
    our_comments = {"dollar stop", "trail exit", "breaker", "parallel cleanup", "cap trim",
                    "scale-in", "flatten", "orphan", "supervisor"}
    for ticket, symbol in known.items():
        if ticket in current:
            continue
        comment = ""
        last_price = 0.0
        last_side = "BUY"
        try:
            deals = mt5.history_deals_get(position=int(ticket)) or []
            if deals:
                comment = (deals[-1].comment or "").strip()
                last_price = float(deals[-1].price)
                # a closing deal is a sell when the position was long
                last_side = "BUY" if deals[-1].type == 1 else "SELL"
        except Exception:  # noqa: BLE001 - history is a nicety, never a reason to crash
            pass
        if comment not in our_comments:
            cooldowns = state.setdefault("cooldowns", {})
            cooldowns[symbol] = max(cooldowns.get(symbol, 0.0), time.time() + 300)
            state["cooldowns"] = cooldowns
            barriers = state.setdefault("operator_barrier", {})
            barriers[symbol] = {"price": last_price, "side": last_side,
                                "time": time.time()}
            state["operator_barrier"] = barriers
            log({"event": f"{symbol} ticket {ticket}: closed by the operator "
                          f"(deal comment '{comment}'); symbol rests 5 minutes"})
    state["open_tickets"] = current
    save_state(state)

    by_symbol: dict[str, list] = {}
    for position in mt5.positions_get() or []:
        if position.magic in magics:
            by_symbol.setdefault(position.symbol, []).append(position)
    for symbol, positions in by_symbol.items():
        directions = {position.type for position in positions}
        if len(directions) > 1:
            log({"event": f"{symbol}: conflicting directions in the swarm, closing both"})
            for position in positions:
                tick = mt5.symbol_info_tick(symbol)
                mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
                                "volume": position.volume,
                                "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                        else mt5.ORDER_TYPE_BUY,
                                "position": position.ticket,
                                "price": tick.bid if position.type == 0 else tick.ask,
                                "deviation": 30, "magic": position.magic,
                                "type_filling": mt5.ORDER_FILLING_FOK,
                                "comment": "conflict"})
            continue
        cap = SYMBOL_CAP.get(symbol, 0.04)
        total = sum(position.volume for position in positions)
        if total > cap + 1e-9:
            excess = total - cap
            for position in sorted(positions, key=lambda item: item.time):
                close_volume = min(position.volume, excess)
                if close_volume <= 0:
                    break
                tick = mt5.symbol_info_tick(symbol)
                result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL,
                                         "symbol": symbol, "volume": round(close_volume, 2),
                                         "type": mt5.ORDER_TYPE_SELL if position.type == 0
                                                 else mt5.ORDER_TYPE_BUY,
                                         "position": position.ticket,
                                         "price": tick.bid if position.type == 0 else tick.ask,
                                         "deviation": 30, "magic": position.magic,
                                         "type_filling": mt5.ORDER_FILLING_FOK,
                                         "comment": "cap"})
                log({"event": f"{symbol}: cap {cap} exceeded by {excess:.2f}, trimming "
                              f"{close_volume:.2f} from ticket {position.ticket} retcode "
                              f"{None if result is None else result.retcode}"})
                excess -= close_volume


def atr_stop_fraction(symbol: str, multiple: float = 3.0) -> float:
    """Stop distance as a fraction of price: three hourly ATRs."""
    import statistics as st

    import MetaTrader5 as mt5

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 60)
    if rates is None or len(rates) < 20:
        return 0.02
    ranges = [max(rates[i]["high"] - rates[i]["low"],
                  abs(rates[i]["high"] - rates[i - 1]["close"]),
                  abs(rates[i]["low"] - rates[i - 1]["close"]))
              for i in range(1, len(rates))]
    atr = st.fmean(ranges[-14:])
    tick = mt5.symbol_info_tick(symbol)
    price = (tick.ask + tick.bid) / 2 if tick and tick.bid else 0
    return (multiple * atr / price) if price else 0.02


def daily_closes(symbol: str, count: int = 30) -> list[float]:
    import MetaTrader5 as mt5

    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 1, 4000)
    if rates is None or len(rates) < 200:
        return []
    days: dict[str, float] = {}
    for rate in rates:
        stamp = datetime.fromtimestamp(int(rate["time"]), tz=timezone.utc)
        days[(stamp + timedelta(hours=SERVER_OFFSET)).date().isoformat()] = float(rate["close"])
    return [days[key] for key in sorted(days)][-count:]


def swarm_exposure(symbol: str) -> float:
    import MetaTrader5 as mt5

    return sum(position.volume for position in (mt5.positions_get(symbol=symbol) or [])
               if position.magic in MAGIC_SET)


def peak_rejection(mt5, position) -> bool:
    """True when the last closed hourly bar rejected the extreme in the trade's direction."""
    rates = mt5.copy_rates_from_pos(position.symbol, mt5.TIMEFRAME_H1, 1, 10)
    if rates is None or len(rates) < 5:
        return False
    bars = [{"o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]),
             "c": float(r["close"])} for r in rates]
    last = bars[-1]
    if position.type == 0:  # long: the high was made and the bar closed red
        made_high = last["h"] >= max(bar["h"] for bar in bars[-4:])
        return made_high and last["c"] < last["o"] and last["c"] < bars[-2]["c"]
    made_low = last["l"] <= min(bar["l"] for bar in bars[-4:])
    return made_low and last["c"] > last["o"] and last["c"] > bars[-2]["c"]


def blocked_by_operator_barrier(mt5, spec: dict, side: str, tick) -> bool:
    """After a manual close, wait until the move proves itself past the exit price."""
    state_now = load_state()
    barrier = (state_now.get("operator_barrier") or {}).get(spec["symbol"])
    if not barrier:
        return False
    if time.time() - barrier.get("time", 0) > 4 * 3600:
        return False
    rates = mt5.copy_rates_from_pos(spec["symbol"], mt5.TIMEFRAME_H1, 1, 40)
    if rates is None or len(rates) < 20:
        return False
    closes = [float(r["close"]) for r in rates]
    highs = [float(r["high"]) for r in rates]
    lows = [float(r["low"]) for r in rates]
    ranges = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                  abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    atr = sum(ranges[-14:]) / 14
    buffer = 0.25 * atr
    price = tick.ask if side == "BUY" else tick.bid
    exit_price = float(barrier.get("price") or 0.0)
    if side == "BUY" and price < exit_price + buffer:
        log({"event": f"{spec['name']} skipped: {spec['symbol']} has not cleared the "
                      f"manual exit {exit_price:.5f} (needs {exit_price + buffer:.5f})"})
        return True
    if side == "SELL" and price > exit_price - buffer:
        log({"event": f"{spec['name']} skipped: {spec['symbol']} has not cleared the "
                      f"manual exit {exit_price:.5f} (needs {exit_price - buffer:.5f})"})
        return True
    return False


def open_trade(mt5, spec: dict, side: str, tick, *, stop: float | None,
               take: float | None = None) -> None:
    if blocked_by_operator_barrier(mt5, spec, side, tick):
        return
    # a signal at the average itself is a coin flip: require the price to stand clear of the
    # fast average by a quarter of an ATR before the trade is taken
    if not trend_clear_of_fast_average(mt5, spec["symbol"], side):
        log({"event": f"{spec['name']} skipped: {spec['symbol']} price hugs EMA50 "
                      f"(no trend confirmation)"})
        return
    state_now = load_state()
    entries = state_now.setdefault("entries", {})
    hour_ago = time.time() - 3600
    recent = [stamp for stamp in entries.get(spec["symbol"], []) if stamp > hour_ago]
    entries[spec["symbol"]] = recent
    if len(recent) >= MAX_ENTRIES_PER_HOUR:
        log({"event": f"{spec['name']} skipped: {spec['symbol']} churn guard "
                      f"({len(recent)} entries in the last hour)"})
        return
    cooldown_until = state_now.get("cooldowns", {}).get(spec["symbol"], 0)
    if cooldown_until > time.time():
        log({"event": f"{spec['name']} skipped: {spec['symbol']} cooling down "
                      f"({int(cooldown_until - time.time())}s)"})
        return
    cap = SYMBOL_CAP.get(spec["symbol"], 0.04)
    if swarm_exposure(spec["symbol"]) + spec["lots"] > cap + 1e-9:
        log({"event": f"{spec['name']} skipped: {spec['symbol']} already at cap {cap}"})
        return
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    price = tick.ask if side == "BUY" else tick.bid
    request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": spec["symbol"],
               "volume": spec["lots"], "type": order_type, "price": price,
               "deviation": 30, "magic": spec["magic"],
               "type_filling": mt5.ORDER_FILLING_FOK, "comment": spec["name"][:25]}
    if stop:
        request["sl"] = price * (1 - stop) if side == "BUY" else price * (1 + stop)
    if take:
        request["tp"] = price * (1 + take) if side == "BUY" else price * (1 - take)
    state_now = load_state()
    entries = state_now.setdefault("entries", {})
    entries.setdefault(spec["symbol"], []).append(time.time())
    save_state(state_now)

    result = mt5.order_send(request)
    if result is not None and result.retcode in (10017, 10018, 10014):
        SKIP_UNTIL[spec["name"]] = time.time() + 600
        log({"event": f"{spec['name']} skipped: retcode {result.retcode} "
                      f"({result.comment}), retry in 10 min"})
        return
    log({"event": f"{spec['name']} OPEN {side} {spec['lots']} {spec['symbol']} @ {price:.5f} "
                  f"retcode {None if result is None else result.retcode} "
                  f"{'EXPERIMENTAL' if spec['experimental'] else ''}"})


def close_trade(mt5, spec: dict, position) -> None:
    tick = mt5.symbol_info_tick(spec["symbol"])
    order_type = mt5.ORDER_TYPE_SELL if position.type == 0 else mt5.ORDER_TYPE_BUY
    price = tick.bid if position.type == 0 else tick.ask
    result = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": spec["symbol"],
                             "volume": position.volume, "type": order_type,
                             "position": position.ticket, "price": price, "deviation": 30,
                             "magic": spec["magic"], "type_filling": mt5.ORDER_FILLING_FOK,
                             "comment": spec["name"][:25]})
    log({"event": f"{spec['name']} CLOSE {spec['symbol']} P/L {position.profit:+.2f} "
                  f"retcode {None if result is None else result.retcode}"})


def main(argv: list[str] | None = None) -> int:
    import MetaTrader5 as mt5

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-sec", type=int, default=30)
    args = parser.parse_args(argv)
    if not mt5.initialize(path=DEMO_TERMINAL):
        raise SystemExit("MetaTrader5.initialize failed")
    account = mt5.account_info()
    if account is None or account.login != EXPECTED_LOGIN:
        raise SystemExit("unexpected account")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise SystemExit("refusing a non-demo account")
    for spec in AGENTS:
        mt5.symbol_select(spec["symbol"], True)
    log({"event": f"SWARM START: {len(AGENTS)} agents, "
                  f"{sum(1 for a in AGENTS if a['experimental'])} experimental"})
    opened_week: dict[str, str] = {}
    opened_day: dict[str, str] = {}

    while True:
        try:
            now = datetime.now(timezone.utc)
            server = now + timedelta(hours=SERVER_OFFSET)
            state = load_state()
            if state.get("paused_until", 0) > time.time():
                time.sleep(args.interval_sec)
                continue
            if state.get("halted_until", 0) > time.time():
                # the daily stop already fired: the book stays flat until the next UTC day
                time.sleep(args.interval_sec)
                continue
            manage_portfolio(mt5, {spec["magic"] for spec in AGENTS}, state)
            # the brake may have fired inside manage_portfolio in this very cycle: never open
            # a trade in the same pass that just flattened the book
            fresh = load_state()
            if fresh.get("paused_until", 0) > time.time() or fresh.get("halted_until", 0) > time.time():
                time.sleep(args.interval_sec)
                continue
            for spec in AGENTS:
                if market_retry_blocked(spec):
                    continue
                position = open_for(mt5, spec)
                tick = mt5.symbol_info_tick(spec["symbol"])
                if tick is None or not tick.bid:
                    continue

                if spec["kind"] == "monday_long":
                    week_key = server.strftime("%G-W%V")
                    if (server.weekday() == 0 and server.hour == ENTRY_SERVER_HOUR
                            and position is None and opened_week.get(spec["name"]) != week_key):
                        opened_week[spec["name"]] = week_key
                        open_trade(mt5, spec, "BUY", tick, stop=None)
                    elif position is not None and (server.weekday() != 0
                                                   or server.hour >= EXIT_SERVER_HOUR):
                        close_trade(mt5, spec, position)

                elif spec["kind"] in ("monday_gap_fill", "monday_gap_continue"):
                    week_key = server.strftime("%G-W%V")
                    if (server.weekday() == 0 and server.hour == ENTRY_SERVER_HOUR
                            and position is None and opened_week.get(spec["name"]) != week_key):
                        opened_week[spec["name"]] = week_key
                        reference = friday_close(spec["symbol"])
                        if not reference:
                            continue
                        price = (tick.bid + tick.ask) / 2
                        gap = price / reference - 1
                        if abs(gap) < 0.001:
                            log({"event": f"{spec['name']} gap {gap * 100:+.3f}% below threshold"})
                            continue
                        side = "BUY" if gap < 0 else "SELL"
                        if spec["kind"] == "monday_gap_continue":
                            side = "SELL" if gap < 0 else "BUY"
                        open_trade(mt5, spec, side, tick, stop=spec["stop"])
                    elif position is not None and (server.weekday() != 0
                                                   or server.hour >= EXIT_SERVER_HOUR):
                        close_trade(mt5, spec, position)

                elif spec["kind"] in ("momentum_buy", "dip_buy"):
                    state = (momentum_buy_state(spec["symbol"]) if spec["kind"] == "momentum_buy"
                             else dip_buy_state(spec["symbol"]))
                    if state is None:
                        continue
                    side, risk_distance = state
                    stop_fraction = risk_distance / tick.ask if tick.ask else None
                    take_fraction = (2 * risk_distance / tick.ask) if tick.ask else None
                    if position is None:
                        open_trade(mt5, spec, side, tick, stop=stop_fraction,
                                   take=take_fraction)

                elif spec["kind"] == "h1_ema":
                    state = h1_ema_state(spec["symbol"])
                    if state is None:
                        continue
                    side, risk_distance = state
                    stop_fraction = risk_distance / tick.ask if tick.ask else None
                    take_fraction = (2 * risk_distance / tick.ask) if tick.ask else None
                    if position is None:
                        open_trade(mt5, spec, side, tick, stop=stop_fraction,
                                   take=take_fraction)
                    elif (position.type == 0 and side == "SELL") or                          (position.type == 1 and side == "BUY"):
                        close_trade(mt5, spec, position)
                        open_trade(mt5, spec, side, tick, stop=stop_fraction,
                                   take=take_fraction)

                elif spec["kind"] == "daily_trend":
                    state = daily_trend_state(spec["symbol"])
                    if state is None:
                        continue
                    side = "BUY" if state == "BUY" else "SELL"
                    stop_fraction = atr_stop_fraction(spec["symbol"])
                    if position is None:
                        open_trade(mt5, spec, side, tick, stop=stop_fraction)
                    elif (position.type == 0 and side == "SELL") or (position.type == 1 and side == "BUY"):
                        close_trade(mt5, spec, position)
                        open_trade(mt5, spec, side, tick, stop=stop_fraction)

                elif spec["kind"] == "session_long":
                    # US index drifts up during the US session; Monday is the strongest day
                    monday_only = spec["name"] == "us500-mon-session"
                    in_window = 14 <= now.hour < 20 and (not monday_only or now.weekday() == 0)
                    if in_window and position is None:
                        open_trade(mt5, spec, "BUY", tick,
                                   stop=atr_stop_fraction(spec["symbol"]))
                    elif position is not None and (now.hour >= 20 or now.hour < 14):
                        close_trade(mt5, spec, position)

                elif spec["kind"] == "weekly_momentum":
                    closes = daily_closes(spec["symbol"], 8)
                    if len(closes) >= 6:
                        momentum = closes[-1] / closes[-6] - 1
                        side = "BUY" if momentum > 0 else "SELL"
                        if position is None:
                            open_trade(mt5, spec, side, tick,
                                       stop=atr_stop_fraction(spec["symbol"]))
                        elif (position.type == 0 and side == "SELL") or \
                             (position.type == 1 and side == "BUY"):
                            close_trade(mt5, spec, position)
                            open_trade(mt5, spec, side, tick,
                                       stop=atr_stop_fraction(spec["symbol"]))

                elif spec["kind"] == "turn_of_month":
                    server_date = server.date()
                    is_month_end = (server_date + timedelta(days=1)).month != server_date.month
                    if is_month_end and server.hour >= 21 and position is None:
                        open_trade(mt5, spec, "BUY", tick,
                                   stop=atr_stop_fraction(spec["symbol"]))
                    elif position is not None and server.day >= 3 and not is_month_end:
                        close_trade(mt5, spec, position)

                elif spec["kind"] == "first_hour":
                    day_key = now.strftime("%Y-%m-%d")
                    if (now.hour == FIRST_HOUR_UTC and now.minute < 10 and position is None
                            and opened_day.get(spec["name"]) != day_key):
                        opened_day[spec["name"]] = day_key
                        open_trade(mt5, spec, "BUY", tick, stop=None)
                    elif (position is not None and now.hour == FIRST_HOUR_UTC
                          and now.minute >= FIRST_HOUR_EXIT_MINUTE):
                        close_trade(mt5, spec, position)
                    elif position is not None and now.hour == (FIRST_HOUR_UTC + 1) % 24:
                        close_trade(mt5, spec, position)
        except Exception as exc:  # noqa: BLE001
            log({"event": f"cycle error {type(exc).__name__}: {exc}"})
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
