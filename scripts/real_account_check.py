"""Check whether a second MetaTrader 5 terminal is logged into a real account.

The real account must live in its own terminal instance so the demo laboratory is never
touched. This script connects to that instance by path, prints what it finds, and refuses
to go further unless the account is a real one (trade_mode == 2).

Usage:
    python scripts/real_account_check.py --path "C:\\mt5-real\\terminal64.exe"
"""
from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    import MetaTrader5 as mt5

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, help="path to terminal64.exe of the real instance")
    args = parser.parse_args(argv)

    if not mt5.initialize(path=args.path):
        print(f"не удалось подключиться к терминалу: {mt5.last_error()}")
        print("проверь, что второй терминал установлен по этому пути и запущен")
        return 2
    try:
        account = mt5.account_info()
        if account is None:
            print("терминал не залогинен ни в один счёт")
            return 2
        mode = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(account.trade_mode, "?")
        print(f"счёт {account.login}  сервер {account.server}  режим {mode}")
        print(f"баланс {account.balance:.2f} {account.currency}  "
              f"плечо 1:{account.leverage}")
        if account.trade_mode != 2:
            print("это не реальный счёт — реальный раннер запускать нельзя")
            return 3
        print("реальный счёт найден; следующий шаг — политика реального раннера")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
