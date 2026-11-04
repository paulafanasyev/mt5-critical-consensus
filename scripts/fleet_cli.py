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

from fleet import candidates, conductor, risk, strategy, terminals, worker  # noqa: E402
from fleet.broker import BrokerError, symbol  # noqa: E402
from fleet.config import DEFAULT_STORE, ConfigError, load, local_host  # noqa: E402
from fleet.store import Store, host_keys  # noqa: E402


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
    for account in _local(config, config.enabled()):
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
    try:
        mine = {a.name for a in _config(args).local()}
    except ConfigError:
        mine = None  # no readable registry: the file is all this command knows
    rows = store.latest_equity()
    if not rows:
        print("данных пока нет; запусти once или run")
    else:
        print(f"{'аккаунт':24} {'логин':>10} {'баланс':>12} {'equity':>12} {'поз':>4} "
              f"{'плавающий':>10} {'валюта':>6}")
        for account, login, balance, equity, positions, floating, currency in rows:
            foreign = "" if mine is None or account in mine else "  ← другая машина"
            print(f"{account:24} {login:>10} {balance:>12.2f} {equity:>12.2f} {positions:>4}"
                  f" {floating:>+10.2f} {currency:>6}{foreign}")
    book_all = store.portfolio()
    book = store.portfolio(sorted(mine)) if mine is not None else book_all
    label = "портфель этой машины" if mine is not None else "портфель (реестр не прочитан)"
    print(f"\n{label}: equity {book['equity']:.2f} USD | счётов {int(book['accounts'])}"
          f" | позиций {int(book['positions'])} | плавающий {book['floating']:+.2f}")
    extra = int(book_all["accounts"] - book["accounts"])
    if extra > 0:
        print(f"в файле ещё строк чужих машин: {extra}."
              " Лимиты парка считаются только по книге этой машины.")
    if conductor.STATE_FILE.exists():
        print(f"дирижёр: {conductor.STATE_FILE.read_text(encoding='utf-8')[:400]}")
    census = worker.roster_rows(store)
    if census is not None:
        logged = [row for row in census if row.get("login") is not None]
        blind = worker.blind_windows(census)
        print(f"\nокна на этой машине: {len(census)}, с логином {len(logged)},"
              f" не назвали счёт: {len(blind)}")
        for key, dirs in sorted(worker.duplicated_accounts(census).items()):
            print(f"  {key}: {len(dirs)} окна ({', '.join(dirs)}) — входы по этому счёту закрыты")
        if blind:
            print(f"  {len(blind)} окно не ответили по существу — парк не считает поле чистым,"
                  " входы закрыты (команда `terminals` покажет каждое)")
    else:
        print("\nсвежей переписи окон этой машины нет: входы закрыты, выходы идут."
              " Перепись снимает сам цикл; `terminals` — вручную.")
    print("\nпоследние события:")
    for stamp, account, kind, payload in store.recent_events(args.events):
        print(f"  {stamp} {account:20} {kind:16} {payload[:110]}")
    return 0


def _local(config: FleetConfig, accounts: list) -> list:
    """Accounts this machine may drive: a pinned account is never logged into from elsewhere."""
    return [a for a in accounts if config.runs_here(a)]


def _pinned_elsewhere(account, config: FleetConfig) -> str:
    return "" if config.runs_here(account) else f"счёт закреплён за машиной {account.host}"


def cmd_terminals(args: argparse.Namespace) -> int:
    """Census of the windows on this machine: which terminal is up, whose account it holds, how much money.

    Read-only. Nothing here launches or stops a terminal: the census walks the process list, checks
    each pid is still up, and asks only those windows to name their account — `initialize` starts a
    terminal that is not running, so a path that is no longer alive is never probed. Cost is one
    probe per window, and a window that is up but not logged in costs the full 4 s timeout.
    """
    store = Store(args.store)
    config = None
    try:
        config = _config(args)
    except Exception as exc:  # noqa: BLE001 - a broken registry must not hide the windows
        print(f"реестр не прочитан ({exc}); показываю только окна")
    rows = worker.refresh_roster(store, force=True)
    if not rows:
        print("ни одного запущенного терминала: парк пока ничего не видит")
        return 0
    print(f"{'папка терминала':38} {'логин':>10} {'сервер':22} {'баланс':>12} {'equity':>12}")
    for row in rows:
        if row.get("login") is None:
            print(f"{row.get('dir') or row.get('exe') or '<без пути>':38} {'—':>10}"
                  f" не отвечает: {row.get('error', '')}")
            continue
        balance = float(row.get("balance") or 0.0)
        equity = float(row.get("equity") or 0.0)
        print(f"{row['dir']:38} {row['login']:>10} {str(row['server'])[:22]:22}"
              f" {balance:>12.2f} {equity:>12.2f}")
    duplicates = worker.duplicated_accounts(rows)
    for key, dirs in sorted(duplicates.items()):
        print(f"\nВНИМАНИЕ: счёт {key} открыт в {len(dirs)} окнах: {', '.join(dirs)}."
              " Свободные деньги у них общие: входы по этому счёту парк закрывает.")
    blind = worker.blind_windows(rows)
    if blind:
        print(f"\nВНИМАНИЕ: {len(blind)} окно не назвали счёт и не молчат измеренно"
              " (-10005): парк не считает поле чистым, входы закрыты.")
    seen = {row["dir"] for row in rows if row.get("login") is not None}
    owned = {worker.window_dir(str(a.data_dir)) for a in (config.accounts if config else [])}
    idle = [row for row in rows if row.get("login") is not None
            and config is not None and row["dir"] not in owned]
    if idle:
        print(f"\nокна, которых нет в реестре (их парк не ведёт): {len(idle)}")
        for row in idle:
            print(f"  {row['dir']} — {row['login']}@{row['server']}")
    if config is not None:
        waiting = sorted(a.name for a in config.local()
                         if worker.window_dir(str(a.data_dir)) not in seen)
        if waiting:
            print(f"\nсчета этой машины без запущенного окна: {', '.join(waiting)}."
                  " Окно поднимает парк (init + run); пока его нет, по этим счетам в книге"
                  " нет ни одного свежего числа.")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Put one named account's own window down — never the desk's other terminals.

    With several brokers on one machine there is no park path to stop a single window, and without
    one `init --force` cannot rebuild an account's directory: it would wipe the folder a live
    terminal is running from and leave that window holding positions nobody manages.
    """
    config = _config(args)
    account = config.by_name(args.account)
    if account is None:
        print(f"счёт не найден: {args.account}")
        return 1
    problem = _pinned_elsewhere(account, config)
    if problem:
        print(f"{problem}: чужое окно парк не гасит")
        return 1
    pid = terminals.stop(account)
    if pid is None:
        print(f"{account.name}: из {account.data_dir} ничего не запущено, останавливать нечего")
        return 0
    Store(args.store).event(account.name, "window_stopped", pid=pid,
                            directory=str(account.data_dir))
    print(f"{account.name}: окно pid {pid} остановлено")
    print("  позиции у брокера остались открытыми: цикл парка поднимет окно сам, "
          "но до тех пор по этому счету выходы не ведутся")
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    config = _config(args)
    store = Store(args.store)
    accounts = _local(config, config.enabled())
    if args.account:
        chosen = [a for a in config.enabled() if a.name == args.account]
        if not chosen:
            print(f"счёт не найден: {args.account}")
            return 1
        problem = _pinned_elsewhere(chosen[0], config)
        if problem:
            print(f"{problem}: здесь его не запускай")
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
    problem = _pinned_elsewhere(account, config)
    if problem:
        print(f"{problem}: здесь его не подключай")
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


def cmd_candidates(args: argparse.Namespace) -> int:
    """The candidate ledger: every trade the park took, and what waiting for confirmation cost.

    This is the only honest way to answer «входи раньше и bigger на реальных деньках»: the drift
    column is the price the market had already given before the fill, priced by the broker's own
    contract. Nothing here sends anything — the module it reads cannot even import the broker.
    """
    store = Store(args.store)
    book = candidates.ledger(store, limit=args.limit)
    if not book["candidates"]:
        print("кандидатов пока нет: парк не исполнил ни одной сделки, которую можно было бы "
              "считать. Это не «плохая статистика» — это отсутствие измерения.")
        return 0
    win_rate = "—" if book["win_rate"] is None else f"{book['win_rate']}%"
    drift_mean = "—" if book["mean_drift_points"] is None else f"{book['mean_drift_points']}"
    print(f"кандидатов {book['candidates']} | закрыто {book['closed']} | "
          f"открыто {book['open']} | винрейт {win_rate}")
    print(f"прибыль закрытых: {book['profit_usd']:+.2f} USD | ожидание стоило "
          f"{drift_mean} пунктов в среднем, {book['drift_cost_usd']:+.2f} USD суммарно")
    print(f"\n{'тикет':>10} {'счёт':>10} {'символ':>10} {'стор':>5} {'лот':>6} "
          f"{'дрейф,п':>9} {'дрейф,$':>9} {'итог,$':>9} {'выход':>10}")
    for row in book["trades"]:
        drift = row["drift_points"]
        cost = row["drift_cost_usd"]
        profit = row["profit_usd"]
        volume = row["volume"]
        lot = f"{volume:.2f}" if isinstance(volume, (int, float)) else "—"
        print(f"{row['ticket']:>10} {row['account'][:10]:>10} {row['symbol'][:10]:>10} "
              f"{row['side']:>5} {lot:>6} "
              f"{f'{drift:.1f}' if drift is not None else '—':>9} "
              f"{f'{cost:+.2f}' if cost is not None else '—':>9} "
              f"{f'{profit:+.2f}' if profit is not None else 'открыт':>9} "
              f"{str(row['exit'])[:10]:>10}")
    print("\nчто это доказывает: сколько пункта и доллара стоит подтверждение сигнала."
          "\nчего оно не доказывает: что раньше было бы лучше — для этого нужна прибыльная "
          "система, а измеренные прогоны на настоящих барах убыточны (docs/DEMO_RESULTS.md).")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config_path = pathlib.Path(args.config).resolve() if args.config else None
    config = load(config_path)
    if not config.enabled():
        print("нет включённых счетов")
        return 1
    local = _local(config, config.enabled())
    if not local:
        print("на этой машине нет ни одного счёта: все закреплены за другими host")
        return 1
    shards = conductor.parse_shards(args.shards, config.shards()) or None
    engine = conductor.Conductor(config, Store(args.store), interval=args.interval,
                                 dry_run=args.dry_run, config_path=config_path, shards=shards,
                                 live=args.live, max_shards=args.max_shards)
    real = _real_names(local)
    if real and not args.live:
        print(f"реальные счета {real}: ордера выключены, нужен --live")
    elsewhere = [a.name for a in config.enabled() if not config.runs_here(a)]
    print(f"парк: {len(local)} счетов на этой машине, шарды {engine.shards}"
          + (f", закреплены за другой машиной {elsewhere}" if elsewhere else ""))
    engine.run_forever(force_lock=args.force_lock)
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
    host = local_host()
    day_key, base_key, _registry_key = host_keys(host)
    # Only this machine's keys. The unscoped legacy pair stays: another box sharing this book may
    # not have taken its own starting baseline from it yet, and wiping it would leave that machine
    # with no day measured at all — which its entry gate reads as "nothing to stop for".
    # The registry hash stays too: it records which accounts the book covers, and a day reset is
    # not a change of composition. Losing it would make the next tick announce a `baseline_rebased`
    # that blames a registry change which never happened.
    for key in (day_key, base_key, f"portfolio_day_held:{host}",
                f"portfolio_day_held_since:{host}"):
        store.set_meta(key, "")
    conductor.PAUSE_FILE.unlink(missing_ok=True)
    store.event("cli", "reset_day", host=host)
    print(f"дневная база портфеля сброшена для машины {host}, пауза снята")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Every line this CLI prints is Russian. A cp1252 console or a pipe raises UnicodeEncodeError
    # on the first Cyrillic character, which is a ValueError — so the crash was caught by the error
    # handler below and it crashed again printing the message. UTF-8 with replacement cannot abort
    # a status read while the park is trading.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
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

    stop = sub.add_parser("stop", help="остановить окно одного названного счёта")
    stop.add_argument("--account", required=True)
    stop.set_defaults(func=cmd_stop)

    run = sub.add_parser("run", help="запустить дирижёра")
    run.add_argument("--interval", type=int, default=30)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--live", action="store_true")
    run.add_argument("--shards", default="", help="например 0 или 0,2")
    run.add_argument("--max-shards", type=int, default=None,
                     help=f"потолок шардов на этой машине (по умолчанию "
                          f"{conductor.Conductor.MAX_SHARDS})")
    run.add_argument("--force-lock", action="store_true",
                     help="снять залипший lock дирижёра")
    run.set_defaults(func=cmd_run)

    pause = sub.add_parser("pause", help="включить паузу")
    pause.set_defaults(func=cmd_pause, action="pause")
    resume = sub.add_parser("resume", help="снять паузу")
    resume.set_defaults(func=cmd_pause, action="resume")
    reset = sub.add_parser("reset-day", help="сбросить дневную базу портфеля")
    reset.set_defaults(func=cmd_reset_day)
    windows = sub.add_parser("terminals", help="что за окна запущены и чей в них счёт")
    windows.set_defaults(func=cmd_terminals)
    cand = sub.add_parser("candidates",
                          help="журнал сделок: что стоило ждать подтверждения входа")
    cand.add_argument("--limit", type=int, default=5000,
                      help="сколько последних событий журнала разобрать")
    cand.set_defaults(func=cmd_candidates)
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
