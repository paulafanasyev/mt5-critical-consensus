"""Fleet CLI: provision terminals, inspect the book, run one cycle or the whole park.

Examples:
    python scripts/fleet.py init
    python scripts/fleet.py once --account exness-real --dry-run
    python scripts/fleet.py run --interval 30
    python scripts/fleet.py status
    python scripts/fleet.py pause / resume
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet import conductor, risk, strategy, terminals, worker  # noqa: E402
from fleet.broker import BrokerError, symbol  # noqa: E402
from fleet.config import load  # noqa: E402
from fleet.store import Store  # noqa: E402

DEFAULT_STORE = pathlib.Path(r"C:\mt5-fleet") / "fleet.sqlite"


def _config(args: argparse.Namespace):
    return load(pathlib.Path(args.config) if args.config else None)


def cmd_init(args: argparse.Namespace) -> int:
    config = _config(args)
    if not config.enabled():
        print("нет включённых счетов; заполни config/accounts.yaml")
        return 1
    for account in config.enabled():
        target = terminals.provision(account, force=args.force)
        print(f"{account.name}: {target}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = Store(args.store)
    rows = store.latest_equity()
    if not rows:
        print("данных пока нет; запусти once или run")
    else:
        print(f"{'аккаунт':24} {'логин':>10} {'баланс':>12} {'equity':>12} {'поз':>4} {'плавающий':>10}")
        for account, login, balance, equity, positions, floating in rows:
            print(f"{account:24} {login:>10} {balance:>12.2f} {equity:>12.2f}"
                  f" {positions:>4} {floating:>+10.2f}")
    book = store.portfolio()
    print(f"\nпортфель: equity {book['equity']:.2f} | счётов {int(book['accounts'])}"
          f" | позиций {int(book['positions'])} | плавающий {book['floating']:+.2f}")
    print("\nпоследние события:")
    for stamp, account, kind, payload in store.recent_events(args.events):
        print(f"  {stamp} {account:20} {kind:14} {payload[:110]}")
    state = conductor.STATE_FILE
    if state.exists():
        print(f"\nдирижёр: {state.read_text(encoding='utf-8')[:400]}")
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    config = _config(args)
    store = Store(args.store)
    accounts = config.enabled()
    if args.account:
        chosen = [a for a in accounts if a.name == args.account]
        if not chosen:
            print(f"счёт не найден: {args.account}")
            return 1
        accounts = chosen
    elif args.shard is not None:
        accounts = [a for a in accounts if a.shard == args.shard]
    run = worker.Worker(config, store, dry_run=args.dry_run)
    for account in accounts:
        result = run.run_account(account)
        store.event(result.account, "cycle", status=result.status, detail=result.detail,
                    opened=result.opened, closed=result.closed)
        print(json.dumps(dataclasses.asdict(result), ensure_ascii=False))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Read-only probe of one account's terminal: symbols, spread, signals."""
    config = _config(args)
    account = config.by_name(args.account)
    if account is None:
        print(f"счёт не найден: {args.account}")
        return 1
    run = worker.Worker(config, Store(args.store), dry_run=True)
    error = run.attach(account)
    if error:
        print(f"подключение не удалось: {error}")
        run.detach()
        if not account.keep_running:
            terminals.stop(account)
        return 1
    try:
        snapshot = risk.AccountSnapshot.read()
        print(f"счёт: {account.name} {snapshot.login} {snapshot.server}"
              f" balance {snapshot.balance:.2f} equity {snapshot.equity:.2f}"
              f" leverage 1:{snapshot.leverage}")
        for base in account.symbols:
            spec = symbol(base)
            if spec is None:
                print(f"  {base:8} -> нет у брокера")
                continue
            print(f"  {base:8} -> {spec.name:12} digits {spec.digits} spread {spec.spread}"
                  f" lot {spec.volume_step} stops {spec.stops_level}")
        for sig in strategy.signals(account):
            print(f"  сигнал {sig.base_symbol} {sig.side} {sig.reason} {sig.price:.5f}"
                  f" SL {sig.stop:.5f} TP {sig.target:.5f}")
    finally:
        run.detach()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _config(args)
    if not config.enabled():
        print("нет включённых счетов")
        return 1
    engine = conductor.Conductor(config, Store(args.store), interval=args.interval,
                                 dry_run=args.dry_run)
    print(f"парк: {len(config.enabled())} счетов, шарды {config.shards()}")
    engine.run_forever()
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    conductor.FLEET_ROOT.mkdir(parents=True, exist_ok=True)
    if args.action == "pause":
        conductor.PAUSE_FILE.write_text("ручная пауза", encoding="utf-8")
        print(f"пауза включена: {conductor.PAUSE_FILE}")
    else:
        conductor.PAUSE_FILE.unlink(missing_ok=True)
        print("пауза снята")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MT5 fleet")
    parser.add_argument("--config", default="", help="путь к accounts.yaml")
    parser.add_argument("--store", default=str(DEFAULT_STORE))
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="разложить терминалы по счетам")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    status = sub.add_parser("status", help="показать книгу и события")
    status.add_argument("--events", type=int, default=12)
    status.set_defaults(func=cmd_status)

    once = sub.add_parser("once", help="один цикл по всем или выбранным счетам")
    once.add_argument("--account", default="")
    once.add_argument("--shard", type=int, default=None)
    once.add_argument("--dry-run", action="store_true")
    once.set_defaults(func=cmd_once)

    check = sub.add_parser("check", help="проверить счёт без торговли")
    check.add_argument("--account", required=True)
    check.set_defaults(func=cmd_check)

    run = sub.add_parser("run", help="запустить дирижёра")
    run.add_argument("--interval", type=int, default=30)
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)

    pause = sub.add_parser("pause", help="включить паузу")
    pause.set_defaults(func=cmd_pause, action="pause")
    resume = sub.add_parser("resume", help="снять паузу")
    resume.set_defaults(func=cmd_pause, action="resume")

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BrokerError as exc:
        print(f"ошибка брокера: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
