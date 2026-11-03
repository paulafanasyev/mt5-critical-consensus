"""Fleet CLI: provision terminals, inspect the book, run one cycle or the whole park.

Examples:
    python scripts/fleet_cli.py init
    python scripts/fleet_cli.py check --account demo
    python scripts/fleet_cli.py once --account demo --dry-run
    python scripts/fleet_cli.py run --shards 0 --live
    python scripts/fleet_cli.py status
    python scripts/fleet_cli.py pause | resume | reset-day
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
from fleet.config import DEFAULT_STORE, load  # noqa: E402
from fleet.store import Store  # noqa: E402


def _config(args: argparse.Namespace):
    return load(pathlib.Path(args.config).resolve() if args.config else None)


def _real_names(accounts) -> list[str]:
    return [a.name for a in accounts if a.is_real]


def cmd_init(args: argparse.Namespace) -> int:
    config = _config(args)
    if not config.enabled():
        print("нет включённых счетов; заполни config/accounts.yaml")
        return 1
    failures = 0
    for account in config.enabled():
        try:
            target = terminals.provision(account, force=args.force)
            seed_ok, seed_detail = terminals.seed_status(account)
            if not seed_ok:
                print(f"{account.name}: {target} (сессия: {seed_detail})")
            else:
                print(f"{account.name}: {target}")
        except (FileNotFoundError, OSError) as exc:
            failures += 1
            print(f"{account.name}: не развёрнут — {exc}")
    return 0 if failures == 0 else 1


def cmd_status(args: argparse.Namespace) -> int:
    store = Store(args.store)
    rows = store.latest_equity()
    if not rows:
        print("данных пока нет; запусти once или run")
    else:
        print(f"{'аккаунт':24} {'логин':>10} {'баланс':>12} {'equity':>12} {'поз':>4} "
              f"{'плавающий':>10} {'валюта':>6}")
        for account, login, balance, equity, positions, floating, currency in rows:
            print(f"{account:24} {login:>10} {balance:>12.2f} {equity:>12.2f} {positions:>4}"
                  f" {floating:>+10.2f} {currency:>6}")
    book = store.portfolio()
    print(f"\nпортфель: equity {book['equity']:.2f} USD | счётов {int(book['accounts'])}"
          f" | позиций {int(book['positions'])} | плавающий {book['floating']:+.2f}")
    if conductor.STATE_FILE.exists():
        print(f"дирижёр: {conductor.STATE_FILE.read_text(encoding='utf-8')[:400]}")
    print("\nпоследние события:")
    for stamp, account, kind, payload in store.recent_events(args.events):
        print(f"  {stamp} {account:20} {kind:16} {payload[:110]}")
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

    real = _real_names(accounts)
    dry_run = args.dry_run
    if real and not args.live:
        dry_run = True
        print(f"реальные счета {real}: живые ордера выключены (нужен --live)")
    paused = conductor.PAUSE_FILE.exists()
    if paused and not args.force:
        print(f"пауза активна: {conductor.PAUSE_FILE}; снимай или используй --force")
        return 1
    runner = worker.Worker(config, store, dry_run=dry_run, allow_live=args.live)
    results = []
    for account in accounts:
        result = runner.run_account(account, allow_entries=not paused)
        results.append(result)
        store.event(result.account, "cycle", status=result.status, detail=result.detail,
                    opened=result.opened, closed=result.closed)
        print(json.dumps(dataclasses.asdict(result), ensure_ascii=False))
    healthy = {"ok", "flat", "cooldown", "blocked"}
    return 0 if all(r.status in healthy for r in results) else 2


def cmd_check(args: argparse.Namespace) -> int:
    config = _config(args)
    account = config.by_name(args.account)
    if account is None:
        print(f"счёт не найден: {args.account}")
        return 1
    runner = worker.Worker(config, Store(args.store), dry_run=True)
    error = runner.attach(account)
    if error:
        print(f"подключение не удалось: {error}")
        runner.detach()
        terminals.stop(account)
        return 1
    try:
        snapshot = risk.AccountSnapshot.read()
        if snapshot is None:
            print("нет данных счёта")
            return 1
        print(f"счёт: {account.name} {snapshot.login} {snapshot.server}"
              f" balance {snapshot.balance:.2f} equity {snapshot.equity:.2f}"
              f" валюта {snapshot.currency} курс к USD {snapshot.rate_to_usd():.4f}"
              f" день сервера {snapshot.server_day}")
        for base in account.symbols:
            spec = symbol(base)
            if spec is None:
                print(f"  {base:8} -> нет у брокера")
                continue
            print(f"  {base:8} -> {spec.name:12} digits {spec.digits} spread {spec.spread}"
                  f" step {spec.volume_step} stops {spec.stops_level}")
        for sig in strategy.signals(account):
            print(f"  сигнал {sig.base_symbol} {sig.side} {sig.reason} {sig.price:.5f}"
                  f" SL {sig.stop:.5f} TP {sig.target:.5f}")
    finally:
        runner.detach()
        if not account.keep_running:
            terminals.stop(account)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config_path = pathlib.Path(args.config).resolve() if args.config else None
    config = load(config_path)
    if not config.enabled():
        print("нет включённых счетов")
        return 1
    shards = [int(part) for part in args.shards.replace(" ", "").split(",") if part] or None
    engine = conductor.Conductor(config, Store(args.store), interval=args.interval,
                                 dry_run=args.dry_run, config_path=config_path, shards=shards,
                                 allow_multi_shard=args.allow_multi_shard, live=args.live)
    real = _real_names(config.enabled())
    if real and not args.live:
        print(f"реальные счета {real}: ордера выключены, нужен --live")
    print(f"парк: {len(config.enabled())} счетов, шарды {engine.shards}")
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


def cmd_reset_health(args: argparse.Namespace) -> int:
    store = Store(args.store)
    config = _config(args)
    names = [args.account] if args.account else [a.name for a in config.enabled()]
    for name in names:
        store.clear_failure(name)
        store.event(name, "health_reset")
    print(f"circuit breaker сброшен: {', '.join(names) or 'нет счетов'}")
    return 0


def cmd_reset_day(args: argparse.Namespace) -> int:
    store = Store(args.store)
    store.set_meta("portfolio_day", "")
    store.set_meta("portfolio_day_start_equity", "0")
    conductor.PAUSE_FILE.unlink(missing_ok=True)
    store.event("cli", "reset_day")
    print("дневная база портфеля сброшена, пауза снята")
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
    once.add_argument("--live", action="store_true")
    once.add_argument("--force", action="store_true")
    once.set_defaults(func=cmd_once)

    check = sub.add_parser("check", help="проверить счёт без торговли")
    check.add_argument("--account", required=True)
    check.set_defaults(func=cmd_check)

    run = sub.add_parser("run", help="запустить дирижёра")
    run.add_argument("--interval", type=int, default=30)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--live", action="store_true")
    run.add_argument("--shards", default="", help="например 0 или 0,2")
    run.add_argument("--allow-multi-shard", action="store_true")
    run.set_defaults(func=cmd_run)

    pause = sub.add_parser("pause", help="включить паузу")
    pause.set_defaults(func=cmd_pause, action="pause")
    resume = sub.add_parser("resume", help="снять паузу")
    resume.set_defaults(func=cmd_pause, action="resume")
    reset = sub.add_parser("reset-day", help="сбросить дневную базу портфеля")
    reset.set_defaults(func=cmd_reset_day)
    health = sub.add_parser("reset-health", help="разблокировать счёт после circuit breaker")
    health.add_argument("--account", default="")
    health.set_defaults(func=cmd_reset_health)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BrokerError as exc:
        print(f"ошибка брокера: {exc}")
        return 2
    except ValueError as exc:
        print(f"ошибка конфигурации: {exc}")
        return 3
    except OSError as exc:
        print(f"ошибка ввода-вывода: {exc}")
        return 2
    except RuntimeError as exc:
        print(f"ошибка запуска: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
