"""Inventory a MetaTrader 5 terminal: which instruments it really allows and what they cost.

Run it against any terminal instance (MetaQuotes demo, Exness demo, Exness real) to see what the
broker actually offers before a scheme is pointed at it:

    python scripts/terminal_inventory.py --path "C:\\mt5-exness\\terminal64.exe"

It prints the account, the trade mode, and for the instruments we care about the spread in
points and in percent of price — the number that decides whether an edge survives.
"""
from __future__ import annotations

import argparse

GROUPS = [
    ("FX majors", ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD")),
    ("metals", ("XAUUSD", "XAGUSD")),
    ("indices", ("US500", "US30", "USTEC", "US2000", "UK100", "DE40", "JPN225", "FRA40", "AUS200")),
    ("crypto", ("BTCUSD", "ETHUSD", "XRPUSD")),
]
MODE = {0: "DISABLED", 1: "LONG_ONLY", 2: "SHORT_ONLY", 3: "CLOSE_ONLY", 4: "FULL"}


def main(argv: list[str] | None = None) -> int:
    import MetaTrader5 as mt5

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=None, help="terminal64.exe of the instance to inspect")
    args = parser.parse_args(argv)

    if not mt5.initialize(**({"path": args.path} if args.path else {})):
        print(f"не удалось подключиться: {mt5.last_error()}")
        return 2
    try:
        account = mt5.account_info()
        if account is None:
            print("терминал не залогинен")
            return 2
        mode = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(account.trade_mode, "?")
        print(f"счёт {account.login}  сервер {account.server}  режим {mode}  "
              f"баланс {account.balance:.2f} {account.currency}")
        for title, names in GROUPS:
            print(f"\n{title}")
            for name in names:
                info = mt5.symbol_info(name)
                if info is None:
                    continue
                tick = mt5.symbol_info_tick(name)
                if tick is None or not tick.bid:
                    print(f"  {name:8} {MODE.get(info.trade_mode, '?'):10} нет котировки")
                    continue
                point = info.point or 0.00001
                spread_points = info.spread
                spread_price = spread_points * point
                pct = spread_price / tick.bid * 100
                print(f"  {name:8} {MODE.get(info.trade_mode, '?'):10} "
                      f"bid {tick.bid:<12} спред {spread_points:>5} пунктов "
                      f"({pct:.4f}%)")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
