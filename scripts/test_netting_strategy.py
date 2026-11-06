"""Dry-run the netting-safe scenario on demo history before any real order.

Rules simulated (one position at a time, exactly as the netting account requires):
  * buy only when price sits at the 24-bar low (M5), that low is held, M1 has three rising
    closes, and M5/M15/H1 trends all agree upward;
  * stop = $5 of gold movement, target = +$0.30 banked early, otherwise exit on stop;
  * no averaging, no opposite entries, one trade per signal.
"""
from __future__ import annotations

import MetaTrader5 as mt5

TERMINAL = r"C:\mt5-team\terminal64.exe"
SYMBOL = "XAUUSD"
SL_USD = 5.0
TARGET_USD = 0.30
WARMUP = 60


def ema(values, period):
    k = 2 / (period + 1)
    out = values[0]
    for value in values[1:]:
        out = value * k + out * (1 - k)
    return out


def main() -> None:
    if not mt5.initialize(path=TERMINAL, timeout=10000):
        print("нет связи:", mt5.last_error())
        return
    mt5.symbol_select(SYMBOL, True)
    m5 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 3000)
    m15 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 1000)
    h1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 400)
    m1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M1, 0, 3000)
    mt5.shutdown()
    if m5 is None or m15 is None or h1 is None or m1 is None:
        print("нет данных")
        return

    c5 = [float(r["close"]) for r in m5]
    c15 = [float(r["close"]) for r in m15]
    c_h1 = [float(r["close"]) for r in h1]
    m1_by_time = {int(r["time"]): r for r in m1}
    m1_times = sorted(m1_by_time)

    trades = wins = losses = 0
    gross_win = gross_loss = 0.0
    i = WARMUP
    while i < len(m5) - 30:
        seg5 = c5[i - 55:i + 1]
        seg15 = c15[max(0, i // 3 - 55):i // 3 + 1] or c15[:1]
        seg_h1 = c_h1[max(0, i // 12 - 55):i // 12 + 1] or c_h1[:1]
        if len(seg5) < 55 or len(seg15) < 55 or len(seg_h1) < 55:
            i += 1
            continue
        trend5 = ema(seg5, 21) > ema(seg5, 55)
        trend15 = ema(seg15, 21) > ema(seg15, 55)
        trend_h1 = ema(seg_h1, 21) > ema(seg_h1, 55)
        low24 = min(float(r["low"]) for r in m5[i - 24:i + 1])
        close = float(m5[i]["close"])
        held = float(m5[i]["low"]) >= float(m5[i - 1]["low"]) and close > float(m5[i - 1]["close"])
        spans = [max(float(m5[k]["high"]) - float(m5[k]["low"]),
                     abs(float(m5[k]["high"]) - float(m5[k-1]["close"])),
                     abs(float(m5[k]["low"]) - float(m5[k-1]["close"]))) for k in range(i - 14, i)]
        atr5 = sum(spans) / len(spans) if spans else 0.0
        near = (close - low24) <= 0.6 * atr5 if atr5 else False
        # M1 confirmation: three rising closes inside this M5 window
        lo_t = int(m5[i]["time"]) - 300
        hi_t = int(m5[i]["time"])
        window = [m1_by_time[t] for t in m1_times if lo_t <= t <= hi_t]
        m1_up = False
        if len(window) >= 3:
            cc = [float(r["close"]) for r in window]
            m1_up = cc[-1] > cc[-2] > cc[-3]
        if not (trend5 and trend15 and trend_h1 and held and near and m1_up):
            i += 1
            continue
        entry = close
        sl = entry - SL_USD
        tp = entry + TARGET_USD
        outcome = None
        for j in range(i + 1, min(i + 60, len(m5))):
            low = float(m5[j]["low"])
            high = float(m5[j]["high"])
            if low <= sl:
                outcome = -SL_USD
                break
            if high >= tp:
                outcome = TARGET_USD
                break
        if outcome is None:
            i += 1
            continue
        trades += 1
        if outcome > 0:
            wins += 1
            gross_win += outcome
        else:
            losses += 1
            gross_loss += abs(outcome)
        i += 10
    pf = gross_win / gross_loss if gross_loss else float("inf")
    print(f"сделок {trades} | плюсов {wins} | минусов {losses} | винрейт {wins/trades*100 if trades else 0:.1f}%")
    print(f"итог {gross_win - gross_loss:+.2f} USD | Profit Factor {pf:.2f}")
    print(f"средний плюс {gross_win/wins if wins else 0:+.2f} | средний минус {gross_loss/losses if losses else 0:+.2f}")


if __name__ == "__main__":
    main()
