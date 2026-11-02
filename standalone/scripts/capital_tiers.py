"""Capital tier configurator: given any balance (from $0.01 to millions), returns
the honest portfolio configuration — slots, strategies, risk limits — following
docs/strategy_playbook.md.

Usage:
    python scripts/capital_tiers.py            # table for standard balances
    python scripts/capital_tiers.py --balance 250   # config for a specific balance

The returned dict can be fed directly into live_portfolio.py (v6) slots.
"""
from __future__ import annotations

import argparse
import json

TIERS = [
    {"name": "PAPER",   "min": 0.0,     "max": 1.0,     "account": "paper (только симулятор)",
     "slots": 0, "risk": 0.0, "families": [],
     "note": "Ниже $1 реальная торговля невозможна математически (спред съедает всё). "
             "Только симулятор: live_portfolio на виртуальном балансе — учитесь без риска."},
    {"name": "CENT",    "min": 1.0,     "max": 10.0,    "account": "cent (обязательно)",
     "slots": 1, "risk": 0.01, "families": [("EMATREND", "EURUSDT", 1.0, "1h")],
     "note": "Только cent-счёт (баланс в центах). 1 слот, 1 позиция. Это обучение, не заработок."},
    {"name": "NANO",    "min": 10.0,    "max": 100.0,   "account": "cent",
     "slots": 2, "risk": 0.01, "families": [("EMATREND", "EURUSDT", 0.6, "1h"),
                                                  ("BOLLINGER", "EURUSDT", 0.4, "1h")],
     "note": "1-2 слота на verified-стратегиях EUR. Дневной лимит 3%."},
    {"name": "SMALL",   "min": 100.0,   "max": 500.0,   "account": "cent или micro",
     "slots": 3, "risk": 0.0075, "families": [("BOLLINGER", "EURUSDT", 0.5, "1h"),
                                                  ("DONCHIAN", "EURUSDT", 0.3, "1h"),
                                                  ("EMATREND", "PAXGUSDT", 0.2, "1h")],
     "note": "3 verified-слота. Дневной лимит 2%. Просадка 6% = стоп слота."},
    {"name": "MEDIUM",  "min": 500.0,   "max": 2000.0,  "account": "micro",
     "slots": 5, "risk": 0.005, "families": [("BOLLINGER", "EURUSDT", 0.3, "1h"),
                                                  ("DONCHIAN", "EURUSDT", 0.3, "1h"),
                                                  ("EMATREND", "PAXGUSDT", 0.25, "1h"),
                                                  ("BOLLINGER", "ETHUSDT", 0.1, "1h"),
                                                  ("TURTLES", "BTCUSDT", 0.05, "1h")],
     "note": "Полный портфель v5. Дневной лимит 2%, просадка 6%, риск 0.5%/сделку."},
    {"name": "LARGE",   "min": 2000.0,  "max": 100000.0, "account": "micro/standard + VPS",
     "slots": 5, "risk": 0.005, "families": [("BOLLINGER", "EURUSDT", 0.3, "1h"),
                                                  ("DONCHIAN", "EURUSDT", 0.3, "1h"),
                                                  ("EMATREND", "PAXGUSDT", 0.25, "1h"),
                                                  ("BOLLINGER", "ETHUSDT", 0.1, "1h"),
                                                  ("EMATREND", "BTCUSDT", 0.05, "1h")],
     "note": "VPS обязателен (24/7). Спред-фильтр, проскальзывание-контроль. "
             "Возможен DCA-слот на BTC вместо TURTLES."},
    {"name": "XL",      "min": 100000.0, "max": 1e12,   "account": "standard, мульти-брокер",
     "slots": 8, "risk": 0.0025, "families": [("BOLLINGER", "EURUSDT", 0.2, "1h"),
                                                  ("DONCHIAN", "EURUSDT", 0.2, "1h"),
                                                  ("EMATREND", "PAXGUSDT", 0.2, "1h"),
                                                  ("BOLLINGER", "ETHUSDT", 0.1, "1h"),
                                                  ("EMATREND", "BTCUSDT", 0.1, "1h"),
                                                  ("BOLLINGER", "GBPUSD", 0.1, "1h"),
                                                  ("DONCHIAN", "USDJPY", 0.05, "1h"),
                                                  ("EMATREND", "XAUUSD", 0.05, "1h")],
     "note": "Мульти-брокер (лимиты объёма), частичное исполнение, юр. оформление."},
]


def configure(balance: float) -> dict:
    for tier in TIERS:
        if tier["min"] <= balance < tier["max"]:
            slots = []
            for family, market, weight, tf in tier["families"]:
                slots.append({"family": family, "market": market, "weight": weight,
                              "tf": tf, "sub_balance": round(balance * weight, 2)})
            risk_dollars = balance * tier["risk"]
            return {"tier": tier["name"], "account": tier["account"],
                    "balance": round(balance, 2), "risk_per_trade_pct": tier["risk"] * 100,
                    "risk_per_trade_usd": round(risk_dollars, 2),
                    "daily_loss_limit_pct": 2.0 if balance < 2000 else 2.0,
                    "drawdown_stop_pct": 6.0, "slots": slots, "note": tier["note"]}
    return {"error": f"balance {balance} out of range"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--balance", type=float, default=None)
    args = parser.parse_args()

    if args.balance is not None:
        print(json.dumps(configure(args.balance), ensure_ascii=False, indent=1))
        return 0

    print("=== КОНФИГУРАЦИЯ ПОРТФЕЛЯ ДЛЯ ВСЕХ УРОВНЕЙ КАПИТАЛА ===\n")
    for b in (0.01, 5.0, 50.0, 250.0, 1000.0, 5000.0, 50000.0, 500000.0):
        cfg = configure(b)
        slot_str = "; ".join(f"{s['family']}/{s['market'].replace('USDT','')} "
                             f"{s['weight'] * 100:.0f}% (${s['sub_balance']})"
                             for s in cfg.get("slots", [])) or "— (paper only)"
        print(f"${b:<12g} [{cfg['tier']:<7}] {cfg['account']}")
        print(f"   риск/сделку: ${cfg['risk_per_trade_usd']} ({cfg['risk_per_trade_pct']}%)")
        print(f"   слоты: {slot_str}")
        print(f"   лимиты: дневной убыток {cfg['daily_loss_limit_pct']}%, просадка {cfg['drawdown_stop_pct']}%")
        print(f"   {cfg['note']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
