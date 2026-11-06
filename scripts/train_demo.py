"""Train the dip-buy / peak-sell rules on demo history across M1, M5 and M15.

No orders, no real money: this is the laboratory the operator asked for. For every symbol and
timeframe it replays the same rules the real mirror uses and reports trades, win rate, profit
factor and expectancy, so the best timeframe is chosen from data instead of opinion.
"""
from __future__ import annotations

import itertools
import json
import pathlib

import MetaTrader5 as mt5

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "agent_data" / "train_demo.json"
TERMINAL = r"C:\mt5-team\terminal64.exe"

SYMBOLS = ("XAUUSD", "EURUSD", "GBPUSD")
TIMEFRAMES = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}
BARS = 20000


def ema(values, period: int) -> float:
    k = 2 / (period + 1)
    out = values[0]
    for value in values[1:]:
        out = value * k + out * (1 - k)
    return out


def atr(rates, index: int, period: int = 14) -> float:
    spans = []
    for i in range(max(1, index - period + 1), index + 1):
        spans.append(max(rates[i]["high"] - rates[i]["low"],
                         abs(rates[i]["high"] - rates[i - 1]["close"]),
                         abs(rates[i]["low"] - rates[i - 1]["close"])))
    return sum(spans) / len(spans) if spans else 0.0


def backtest(rates, fall_atr: float, sl_atr: float, rr: float, trend_n: int) -> dict:
    trades = wins = 0
    gross_win = gross_loss = 0.0
    index = trend_n + 15
    while index < len(rates) - 2:
        window = rates[max(0, index - 48):index + 1]
        highs = [float(r["high"]) for r in window]
        closes = [float(r["close"]) for r in rates[max(0, index - trend_n):index + 1]]
        a = atr(rates, index)
        if a <= 0:
            index += 1
            continue
        high = max(highs)
        price = float(rates[index]["close"])
        trend_up = ema(closes, min(21, len(closes) - 1)) > ema(closes, min(55, len(closes) - 1))
        dip = (high - price) >= fall_atr * a
        first_up = rates[index]["close"] > rates[index - 1]["close"]
        if not (dip and first_up and trend_up):
            index += 1
            continue
        entry = float(rates[index + 1]["open"])
        sl = entry - sl_atr * a
        tp = entry + rr * (entry - sl)
        outcome = None
        for j in range(index + 2, min(index + 2 + 200, len(rates))):
            low = float(rates[j]["low"])
            highj = float(rates[j]["high"])
            if low <= sl:
                outcome = -1.0
                break
            if highj >= tp:
                outcome = rr
                break
        if outcome is None:
            index += 1
            continue
        trades += 1
        if outcome > 0:
            wins += 1
            gross_win += outcome
        else:
            gross_loss += 1.0
        index += 2
    win_rate = wins / trades * 100 if trades else 0.0
    pf = gross_win / gross_loss if gross_loss else float("inf") if gross_win else 0.0
    expectancy = (gross_win - gross_loss) / trades if trades else 0.0
    return {"trades": trades, "win_rate": round(win_rate, 1), "pf": round(pf, 2) if pf != float("inf") else 999,
            "expectancy_r": round(expectancy, 3)}


def main() -> None:
    if not mt5.initialize(path=TERMINAL, timeout=10000):
        print("нет связи:", mt5.last_error())
        return
    results = []
    for symbol in SYMBOLS:
        mt5.symbol_select(symbol, True)
        for tf_name, tf in TIMEFRAMES.items():
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, BARS)
            if rates is None or len(rates) < 500:
                continue
            best = None
            for fall_atr, sl_atr, rr in itertools.product((0.6, 1.0, 1.4), (0.5, 1.0), (1.5, 2.0)):
                stat = backtest(rates, fall_atr, sl_atr, rr, 55)
                if stat["trades"] < 20:
                    continue
                score = stat["expectancy_r"] * stat["trades"]
                if best is None or score > best["score"]:
                    best = {**stat, "score": score, "fall_atr": fall_atr, "sl_atr": sl_atr, "rr": rr}
            if best:
                results.append({"symbol": symbol, "tf": tf_name, "bars": len(rates), **best})
                print(f"{symbol:7s} {tf_name:3s} bars {len(rates):6d} | trades {best['trades']:4d} | "
                      f"win {best['win_rate']:5.1f}% | PF {best['pf']:6.2f} | exp {best['expectancy_r']:+.3f}R | "
                      f"rules fall {best['fall_atr']} ATR, SL {best['sl_atr']} ATR, RR {best['rr']}")
    mt5.shutdown()
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("сохранено:", OUT)


if __name__ == "__main__":
    main()
