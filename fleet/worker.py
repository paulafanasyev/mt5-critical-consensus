"""Worker: one connection per terminal, and it only ever trades the account it verified.

Measured on a box with three terminals running: each terminal publishes its own named pipe and
MetaTrader5.initialize(path=...) answers from the terminal whose executable was named, from several
processes at once. Nothing about the channel is exclusive to a session, so a worker does not stop
another terminal to make room, and it does not care which process holds loopback port 22346. What it
does care about is identity: before any order it checks that the terminal which answered is this
account's own copy and this account's own login, because a park running several brokers at once has
no excuse for trading the wrong window.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import time
import traceback
import types

import MetaTrader5 as mt5

from . import broker, network, risk, strategy, terminals
from .config import Account, FleetConfig, local_host
from .store import Store

MAX_FAILURES = 5

# How often a cycle re-asks the running windows who they are logged into, and how long an answer
# stays evidence. Sizing is a fraction of the balance this terminal can see; a second window logged
# into the same account spends the same free margin, and nobody but the broker adds the two up.
# Measured here (4 windows up): the two authorised ones answer in ~0.2 s each, the two unauthorised
# ones cost the full probe timeout — 8.37 s for the whole census. The cadence is what decides how
# long a manual login stays invisible, so it is set by that cost, not by politeness.
ROSTER_REFRESH_SECONDS = 300.0
ROSTER_EVIDENCE_SECONDS = 900.0
ROSTER_META_KEY = "terminal_roster"
ROSTER_ERROR_META_KEY = "terminal_roster_error"
ROSTER_PROBE_TIMEOUT_MS = 4000
# Measured: an unauthorised window publishes its pipe and answers (-10005, 'IPC timeout') until
# somebody logs in. It holds no margin, so it is not a second driver. Anything else that cannot be
# read — a silent process whose path the OS would not name, a probe that threw — is a blind spot.
ROSTER_SILENT_ERROR = "-10005"


@dataclasses.dataclass(slots=True)
class WorkerResult:
    account: str
    status: str
    detail: str = ""
    opened: int = 0
    closed: int = 0


def _directory(path_text: str) -> str:
    """Normalize the install directory a terminal reports, for comparison."""
    return str(path_text or "").strip().strip('"').rstrip("\\/").casefold()


def window_dir(path_text: str) -> str:
    """One spelling for "the folder this window lives in".

    The process list gives `C:\\mt5-team\\terminal64.exe`; a terminal answering the census names its
    own folder (`C:\\mt5-team`, measured on build 6296). Both have to land on the same string, or a
    second window on the same account would look like a different one.
    """
    text = str(path_text or "").strip().strip('"')
    if not text:
        return ""
    path = pathlib.PureWindowsPath(text)
    if path.name.casefold() == "terminal64.exe":
        path = path.parent
    return _directory(str(path))


def scan_roster(paths: list[str] | None = None) -> list[dict]:
    """Ask every terminal that is up right now which account it is logged into. Read-only.

    Only paths taken from the live process list are probed: `initialize()` starts a terminal that
    is not running, and the park must not open a window nobody asked for. No credentials are ever
    passed here — a probe that has to log in would change who is driving the money.
    """
    if paths is None:
        paths = [path for _pid, path in terminals.running_terminals()]
    rows: list[dict] = []
    for exe in sorted(paths, key=str.lower):
        row = {"exe": exe, "dir": window_dir(exe), "login": None, "server": "",
               "currency": "", "balance": None, "equity": None, "error": ""}
        if not str(exe).strip():
            # Win32_Process hides ExecutablePath from another user's or an elevated session. An
            # empty path is not "no terminal": initialize(path="") means the default registered
            # one, which the census would then start and count as someone else's window. It stays a
            # blind spot — named as one — rather than becoming a probe.
            row["error"] = "путь процесса не читается: окно есть, кто в нём — неизвестно"
            rows.append(row)
            continue
        try:
            if not mt5.initialize(path=exe, timeout=ROSTER_PROBE_TIMEOUT_MS):
                row["error"] = str(mt5.last_error())
            else:
                window = mt5.terminal_info()
                account = mt5.account_info()
                # The answer is authoritative: initialize(path=A) can be answered by window B, and
                # the folder we requested would then hide B's driver under A's name.
                row["dir"] = window_dir(getattr(window, "path", "") if window else "") or row["dir"]
                row["login"] = getattr(account, "login", None)
                row["server"] = str(getattr(account, "server", "") or "")
                row["balance"] = getattr(account, "balance", None)
                row["equity"] = getattr(account, "equity", None)
                row["currency"] = str(getattr(account, "currency", "") or "")
                if row["login"] is None:
                    row["error"] = "терминал открыл канал, но не назвал счёт"
        except Exception as exc:  # noqa: BLE001 - one hung window must not lose the whole census
            row["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            # The census is a visitor in someone else's window; it never stays for the next call.
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass
        rows.append(row)
    return rows


def is_silent(row: dict) -> bool:
    """A window measured to hold no session: it publishes its pipe and answers nothing until login."""
    return (row.get("login") is None and ROSTER_SILENT_ERROR in str(row.get("error") or ""))


def blind_windows(rows: list[dict]) -> list[dict]:
    """Windows the census could neither read nor rule out. They are not evidence of a clear field."""
    return [row for row in rows if row.get("login") is None and not is_silent(row)]


def duplicated_accounts(rows: list[dict]) -> dict[str, list[str]]:
    """Which accounts answer from more than one window on this machine.

    Key is `login@server` lowercased, value one entry per window that says so — two windows of the
    same folder count twice, because it is the count of drivers that spends the margin, not the
    count of spellings.
    """
    by: dict[str, list[str]] = {}
    for row in rows:
        if row.get("login") is None:
            continue
        key = f"{row['login']}@{str(row.get('server') or '').casefold()}"
        by.setdefault(key, []).append(str(row.get("dir") or row.get("exe") or ""))
    return {key: sorted(dirs) for key, dirs in by.items() if len(dirs) > 1}


def _host_key(name: str) -> str:
    """A key that belongs to one machine.

    The census is a fact about THIS machine's process list and the book is shared between boxes
    (that is why the portfolio day keys are host-scoped), so an unscoped census would have box A
    trade on the windows box B counted — and its own second driver would never appear.
    """
    return f"{name}@{local_host()}"


def refresh_roster(store: Store, *, force: bool = False, now: float | None = None) -> list[dict]:
    """Re-scan when the last answer is too old, and return the rows now on file.

    Call this only while no terminal connection is open: every probe ends with shutdown(), which
    would otherwise drop the connection the cycle is trading on.
    """
    moment = time.time() if now is None else now
    taken, rows = _roster_payload(store.get_meta(_host_key(ROSTER_META_KEY)))
    if not force and rows is not None and taken is not None \
            and moment - taken < ROSTER_REFRESH_SECONDS:
        return rows
    processes = terminals.running_terminals()
    live = terminals.pids_alive([pid for pid, _path in processes])
    scanned = scan_roster([path for pid, path in processes if pid in live])
    store.set_meta(_host_key(ROSTER_META_KEY), json.dumps({"taken": moment, "rows": scanned}))
    return scanned


def _roster_payload(text: str | None) -> tuple[float | None, list[dict] | None]:
    if not text:
        return None, None
    try:
        data = json.loads(text)
        return float(data["taken"]), [dict(row) for row in data["rows"]]
    except (TypeError, ValueError, KeyError, IndexError):
        return None, None


def roster_rows(store: Store, *, now: float | None = None) -> list[dict] | None:
    """The last census of THIS machine, or None when there is none to believe.

    Missing, unreadable, written by another host or older than ROSTER_EVIDENCE_SECONDS: the caller
    does not know who is in the field, and the gate treats that as a reason not to open — the same
    way an unmeasured day or an unpriced peer position is a reason not to open.
    """
    moment = time.time() if now is None else now
    taken, rows = _roster_payload(store.get_meta(_host_key(ROSTER_META_KEY)))
    if rows is None or taken is None or moment - taken > ROSTER_EVIDENCE_SECONDS:
        return None
    return rows


def answered_by(account: Account) -> tuple[bool, str]:
    """Is the terminal that answered to initialize() the one this account owns?

    Fail-closed: an empty answer is a refusal, not a pass. A park that cannot prove which window it
    is driving must not send orders through it.
    """
    info = mt5.terminal_info()
    answered = window_dir(getattr(info, "path", "") if info is not None else "")
    expected = window_dir(str(account.data_dir))
    if not answered:
        return False, "терминал не сообщил свой путь: непонятно, кто отвечает"
    if answered == expected:
        return True, ""
    return False, f"отвечает другой терминал: {answered} вместо {expected}"


class Worker:
    def __init__(self, config: FleetConfig, store: Store, pause_file: pathlib.Path | None = None,
                 dry_run: bool = False, allow_live: bool = False) -> None:
        self.config = config
        self.store = store
        self.pause_file = pause_file
        self.dry_run = dry_run
        self.allow_live = allow_live

    # --- connection ---------------------------------------------------------------------
    def prepare_cycle(self) -> None:
        """Take the census of running windows before anything is attached.

        It has to happen here: every probe ends with shutdown(), so probing after attach() would
        drop the live connection this cycle trades on. A census that cannot be taken costs the
        entries — the gate below fails closed on it — but never the exits, which is why the cycle
        still runs and still manages what is open.
        """
        try:
            refresh_roster(self.store)
        except Exception as exc:  # noqa: BLE001 - one unreadable process list must not cost a cycle
            detail = f"{type(exc).__name__}: {exc}"[:200]
            if self.store.get_meta(_host_key(ROSTER_ERROR_META_KEY)) != detail:
                self.store.set_meta(_host_key(ROSTER_ERROR_META_KEY), detail)
                self.store.event("shard", "roster_error", error=detail)
            return
        if self.store.get_meta(_host_key(ROSTER_ERROR_META_KEY)):
            self.store.set_meta(_host_key(ROSTER_ERROR_META_KEY), "")
            self.store.event("shard", "roster_restored")

    def attach(self, account: Account) -> str:
        if account.network == "vpn":
            ok, detail = network.ensure_tunnel(account.vpn_profile)
            if not ok:
                return f"нужен туннель: {detail}"
        if not account.terminal_exe.exists():
            terminals.provision(account)
        if terminals.running_pid(account) is None:
            terminals.launch(account)
        password = terminals.password_for(account)
        kwargs = {"path": str(account.terminal_exe), "timeout": 20000}
        if password:
            kwargs.update(login=account.login, password=password, server=account.server)
        for attempt in range(3):
            if mt5.initialize(**kwargs):
                break
            mt5.shutdown()
            if attempt == 0:
                terminals.wait_authorized(account, timeout=8)
            time.sleep(4 + attempt * 3)
        else:
            terminals.stop(account)
            time.sleep(2)
            terminals.launch(account)
            terminals.wait_authorized(account, timeout=20)
            for _ in range(2):
                if mt5.initialize(**kwargs):
                    break
                mt5.shutdown()
                time.sleep(6)
            else:
                tail = " | ".join(terminals.last_log_lines(account, 4))
                return f"initialize: {mt5.last_error()} ({tail})"
        # Which window answered? With several brokers running side by side this is not assumed.
        ok, detail = answered_by(account)
        if not ok:
            self.detach()
            return detail
        info = mt5.account_info()
        if info is None:
            mt5.shutdown()
            return "нет данных счёта после initialize"
        # The broker spells its server name; a registry typed by hand may not match its case.
        if info.login != account.login or (str(info.server).strip().casefold()
                                           != account.server.casefold()):
            mt5.shutdown()
            return f"привязан чужой счёт {info.login}/{info.server}"
        # The YAML label is not evidence: the broker's own trade_mode decides demo vs real.
        broker_real = int(info.trade_mode) == int(mt5.ACCOUNT_TRADE_MODE_REAL)
        if broker_real != account.is_real:
            mt5.shutdown()
            return (f"несовпадение режима: в реестре {'real' if account.is_real else 'demo'}, "
                    f"у брокера {'real' if broker_real else 'demo'} — торговля запрещена")
        return ""

    def detach(self) -> None:
        try:
            mt5.shutdown()
        except Exception:  # noqa: BLE001
            pass

    # --- one account --------------------------------------------------------------------
    def run_account(self, account: Account, allow_entries: bool = True) -> WorkerResult:
        # Census first, before attach() opens a connection: every probe ends with shutdown(), and
        # that would drop the channel this account is about to trade on. Inside the refresh window
        # this returns without touching anything, so it costs one walk of the process list per
        # ROSTER_REFRESH_SECONDS, not one per account.
        self.prepare_cycle()
        failures, next_attempt, last_error = self.store.health(account.name)
        if time.time() < next_attempt:
            return WorkerResult(account.name, "cooldown",
                                f"до следующей попытки {int(next_attempt - time.time())} с")
        if account.password_env and not terminals.password_for(account):
            return WorkerResult(account.name, "error",
                                f"секрет не найден: переменная {account.password_env} пуста")
        try:
            error = self.attach(account)
        except Exception as exc:  # noqa: BLE001 - a bad terminal must not kill the shard
            error = f"attach: {type(exc).__name__}: {exc}"
            self.store.event(account.name, "attach_exception", error=str(exc))
        if error:
            # Escalating but always finite: a broker outage must not brick an account forever.
            backoff = 60.0 if failures >= MAX_FAILURES else min(900.0, 5.0 * (2 ** failures))
            self.store.record_failure(account.name, error, backoff)
            if not account.keep_running:
                terminals.stop(account)
            return WorkerResult(account.name, "error", error)
        try:
            self.store.clear_failure(account.name)
            return self._cycle(account, allow_entries)
        except Exception as exc:  # noqa: BLE001 - one account never kills the worker
            self.store.event(account.name, "exception", error=str(exc),
                             trace=traceback.format_exc(limit=3))
            return WorkerResult(account.name, "exception", str(exc))
        finally:
            self.detach()
            if not account.keep_running:
                terminals.stop(account)

    def _cycle(self, account: Account, allow_entries: bool) -> WorkerResult:
        server_day = risk.server_day_now()
        snapshot = risk.AccountSnapshot.read(server_day)
        if snapshot is None:
            return WorkerResult(account.name, "error", "нет счёта")
        if snapshot.equity <= 0:
            return WorkerResult(account.name, "flat", "нулевой баланс")
        if account.is_real and snapshot.balance <= 0:
            return WorkerResult(account.name, "flat", "реальный счёт без денег")
        if snapshot.rate_to_usd() <= 0 and not account.is_real:
            return WorkerResult(account.name, "flat",
                                f"нет курса {snapshot.currency}→USD: книга счёт не видит")
        rate = snapshot.rate_to_usd(account.currency_scale)
        day, flows_payload = self._day_state(account, snapshot, server_day)
        positions = risk.positions_for(account)  # includes manual positions: they move equity

        closed = self._manage_positions(account, day, positions)
        # Re-read after management so the gates judge the account as it is now, not as it was.
        snapshot = risk.AccountSnapshot.read(server_day) or snapshot
        positions = risk.positions_for(account)
        if flows_payload:
            # Publish the flows before the equity row, with the FINAL balance
            # as witness: managing positions can change the balance between the two snapshots.
            parts = flows_payload.split("|")
            if len(parts) >= 4:
                parts[2] = f"{snapshot.balance:.8f}"
                flows_payload = "|".join(parts)
            self.store.set_meta(f"dayflows:{account.name}:{server_day}", flows_payload)
        self.store.save_positions(account.name, positions,
                                  self._mirror_risk_usd(account, positions, rate))

        self.store.equity(account.name, snapshot.login, snapshot.balance, snapshot.equity,
                          len(positions), snapshot.floating, snapshot.currency, rate,
                          server_day)
        opened = 0
        if allow_entries and rate > 0:
            opened = self._try_entries(account, snapshot, positions, day, rate)
        elif allow_entries and rate <= 0:
            self.store.event(account.name, "unpriced_account", currency=snapshot.currency)
        self._save_day(account, day)
        return WorkerResult(account.name, "ok", f"equity {snapshot.equity:.2f}",
                            opened=opened, closed=closed)

    def _announce_once(self, key: str, account_name: str, kind: str, **detail) -> None:
        """Write an event the first time a condition appears, not once per cycle per symbol.

        A 90 s cycle over a hundred accounts would otherwise fill the journal the operator has to
        read with the same line, and the line that matters would be lost in it.
        """
        if self.store.get_meta(key) == "1":
            return
        self.store.set_meta(key, "1")
        self.store.event(account_name, kind, **detail)

    def _second_window(self, account: Account) -> str:
        """Refuse new entries while this account is open in a window the park does not drive.

        attach() already proved which window this cycle trades through. The census answers the other
        half: whether somebody else — a manual login, another shard, a stray copy — is in the same
        money at the same time. Free margin is one number on the broker's side, and two drivers each
        sizing to a fraction of the balance spend it twice; exits must keep running regardless,
        because an unmanaged position is worse than an unused signal.

        The gate fails closed on a field it cannot see: no census of this machine, or a window that
        neither named its account nor measured-silent, is not evidence that nobody else drives this
        money. It is the same rule the park already applies to an unmeasured day and an unpriced
        peer position.
        """
        rows = roster_rows(self.store)
        key = _host_key(f"second_window:{account.name}")
        others: list[str] = []
        blind: list[str] = []
        reason = ""
        if rows is None:
            fingerprint = "no-census"
            reason = ("переписи окон этой машины нет (или она старше часа): непонятно, кто ещё "
                      "водит эти деньги — входы закрыты, выходы идут как обычно")
        else:
            mine = window_dir(str(account.data_dir))
            others = sorted(str(row.get("dir") or row.get("exe") or "")
                            for row in rows
                            if row.get("login") == account.login
                            and str(row.get("server") or "").casefold()
                            == account.server.casefold()
                            and str(row.get("dir") or "") != mine)
            blind = sorted(str(row.get("dir") or row.get("exe") or "") or "<окно без пути>"
                           for row in blind_windows(rows))
            if others:
                fingerprint = "|".join(others)
                reason = (f"счёт {account.login}@{account.server} открыт ещё в {len(others)} окне "
                          f"({', '.join(others)}): свободные деньги общего счёта тратятся дважды, "
                          "входы закрыты")
            elif blind:
                fingerprint = "blind|" + "|".join(blind)
                reason = (f"{len(blind)} окно не назвали свой счёт ({', '.join(blind)}): перепись "
                          "их не видит, а не считает чистыми — входы закрыты")
            else:
                fingerprint = ""
        if fingerprint:
            if (self.store.get_meta(key) or "") != fingerprint:
                self.store.set_meta(key, fingerprint)
                self.store.event(account.name,
                                 "account_second_window" if others else "roster_unknown",
                                 login=account.login, server=account.server,
                                 windows=others or blind,
                                 census="absent" if rows is None else "on_file")
            return reason
        if self.store.get_meta(key):
            # Only a census that actually saw the field can lift the block, so the all-clear always
            # means what its name says: this machine was counted and holds one window for this money.
            self.store.set_meta(key, "")
            self.store.event(account.name, "account_single_window", login=account.login,
                             server=account.server, census="fresh")
        return ""

    # --- position management ------------------------------------------------------------
    def _manage_positions(self, account: Account, day: risk.DayState, positions: list) -> int:
        closed = 0
        specs: dict[str, broker.BrokerSymbol | None] = {}
        for position in positions:
            name = position.symbol
            if name not in specs:
                specs[name] = broker.symbol_exact(name)
            spec = specs[name]
            if spec is None:
                self.store.event(account.name, "unmanaged", symbol=name,
                                 reason="нет спецификации символа")
                continue
            atr_entry = self._atr_entry(account, position, spec)
            tick = mt5.symbol_info_tick(name)
            if tick is None:
                continue
            is_buy = int(position.type) == int(mt5.POSITION_TYPE_BUY)
            price = float(tick.bid if is_buy else tick.ask)
            gain = (price - float(position.price_open)) if is_buy else \
                (float(position.price_open) - price)
            peak = max(self.store.peak(account.name, position.ticket), gain)
            if gain > 0:
                self.store.set_peak(account.name, position.ticket, peak)

            be_level = strategy.break_even_level(account, position, spec, atr_entry)
            if (be_level is not None and not self.dry_run
                    and not (account.is_real and not self.allow_live)):
                try:
                    broker.modify_position(int(position.ticket), name, be_level,
                                           float(position.tp), spec.digits)
                    self.store.event(account.name, "break_even", symbol=name,
                                     ticket=int(position.ticket), sl=be_level)
                except broker.BrokerError as exc:
                    self.store.event(account.name, "break_even_failed", symbol=name,
                                     error=str(exc))

            reason = strategy.exit_reason(account, position, spec, peak, atr_entry)
            if not reason:
                continue
            if self.dry_run or (account.is_real and not self.allow_live):
                self.store.event(account.name, "exit(gated)", symbol=name, reason=reason,
                                 gain=round(gain, 5))
                continue
            try:
                closed_volume = broker.close_position(position, spec)
            except broker.BrokerError as exc:
                self.store.event(account.name, "close_failed", symbol=name, error=str(exc))
                continue
            if closed_volume < float(position.volume) - 1e-9:
                self.store.event(account.name, "partial_close", symbol=name,
                                 closed=closed_volume, volume=float(position.volume))
                continue  # keep the peak and manage the remainder next cycle
            closed += 1
            day.realized += float(position.profit)
            self.store.close_position_row(account.name, int(position.ticket))
            self.store.event(account.name, "closed", symbol=name, reason=reason,
                             profit=round(float(position.profit), 2))
        return closed

    def _atr_entry(self, account: Account, position, spec: broker.BrokerSymbol) -> float:
        key = f"atr:{account.name}:{int(position.ticket)}"
        stored = self.store.get_meta(key)
        if stored:
            try:
                return float(stored)
            except ValueError:
                pass
        stop_distance = abs(float(position.price_open) - float(getattr(position, "sl", 0) or 0))
        if stop_distance > 0 and account.risk.stop_atr > 0:
            atr_value = stop_distance / account.risk.stop_atr
        else:
            atr_value = broker.atr(spec.name, mt5.TIMEFRAME_H1)
        self.store.set_meta(key, f"{atr_value:.8f}")
        return atr_value

    # --- entries ------------------------------------------------------------------------
    def _try_entries(self, account: Account, snapshot: risk.AccountSnapshot, positions: list,
                     day: risk.DayState, rate: float) -> int:
        if self._second_window(account):
            # Announced once per set of windows, inside _second_window: the trail does not need the
            # same warning every cycle. Position management already ran, so exits are not blocked.
            return 0
        allowed, reason = risk.can_open(account, snapshot, day, positions)
        if not allowed:
            self.store.event(account.name, "hold", reason=reason)
            return 0
        mine = [a.name for a in self.config.local()]
        book = self.store.portfolio(mine)
        book_stop = self.store.portfolio(mine, fresh_only=False)
        mirror = self.store.all_positions(max_age=10 ** 9, names=mine)  # last known, not only fresh
        book_positions = [types.SimpleNamespace(
            symbol=symbol, volume=volume, sl=sl, price_open=price_open, ticket=int(ticket),
            type=int(mt5.POSITION_TYPE_BUY) if side == "buy" else int(mt5.POSITION_TYPE_SELL))
                          for _acc, ticket, symbol, side, volume, price_open, _profit,
                          sl, _tp, _magic, _risk_usd in mirror]
        specs: dict[str, broker.BrokerSymbol | None] = {}
        for position in book_positions:
            specs[position.symbol] = broker.symbol_exact(position.symbol)
        current_risk = 0.0
        unpriced_risk = 0
        for row, position in zip(mirror, book_positions):
            stored = row[10]
            if stored is not None and float(stored) > 0:
                # Priced by the broker that holds it, in that account's currency, already
                # converted to dollars: this terminal has never seen that contract.
                current_risk += float(stored)
                continue
            if row[0] != account.name:
                # Somebody else's account, and its own shard could not price this stop. Re-pricing
                # it here would use our contract and our dollar rate for a broker we do not speak
                # to, and a cent or ruble account would silently cost a hundredth or a hundredth-
                # hundredth of its real risk.
                unpriced_risk += 1
                continue
            spec = specs.get(position.symbol)
            if spec is None:
                unpriced_risk += 1
                continue
            money = risk.position_risk_money(
                position, spec, self._atr_entry(account, position, spec), account.risk.stop_atr)
            if not money > 0:
                unpriced_risk += 1  # a zero here is a broker that described no tick value
                continue
            current_risk += money * rate
        if book_stop.get("unpriced", 0) > 0:
            if self.store.get_meta(f"unpriced_warned:{account.name}") != "1":
                self.store.set_meta(f"unpriced_warned:{account.name}", "1")
                self.store.event(account.name, "unpriced_accounts_book",
                                 count=book_stop["unpriced"])
            return 0
        if self.store.get_meta(f"unpriced_warned:{account.name}") == "1":
            self.store.set_meta(f"unpriced_warned:{account.name}", "0")
            self.store.event(account.name, "unpriced_book_resolved")
        if unpriced_risk:
            # Counting an unmeasurable stop as zero would let the portfolio risk cap pass a book
            # whose real risk nobody has added up yet.
            self.store.event(account.name, "portfolio_risk_unpriced", count=unpriced_risk)
            return 0
        day_pct = self._portfolio_day_pct(book_stop["equity"])
        unmeasured_key = f"day_unmeasured:{account.name}"
        if day_pct is None:
            # Nobody has measured this machine's day yet, so the daily loss limit is not a number.
            if self.store.get_meta(unmeasured_key) != "1":
                self.store.set_meta(unmeasured_key, "1")
                self.store.event(account.name, "portfolio_day_unmeasured",
                                 day=self.store.portfolio_baseline(local_host())[0] or "")
            return 0
        if self.store.get_meta(unmeasured_key) == "1":
            self.store.set_meta(unmeasured_key, "0")
            self.store.event(account.name, "portfolio_day_measured", day_pct=round(day_pct, 2))
        for signal in strategy.signals(account):
            spec = broker.symbol(signal.base_symbol)
            if spec is None:
                # A signal nobody can resolve is not a quiet no-trade: the park earns nothing and
                # the operator has to see which name the broker does not offer here.
                self._announce_once(f"no_symbol:{account.name}:{signal.base_symbol}",
                                    account.name, "signal_symbol_unresolved",
                                    symbol=signal.base_symbol)
                continue
            same_symbol = [p for p in positions if p.symbol == spec.name]
            if len(same_symbol) >= account.strategy.max_positions_per_symbol:
                continue
            if self._in_cooldown(account, spec.name):
                continue
            volume = risk.size_for(account, spec, signal.stop_distance, snapshot.balance)
            if volume <= 0:
                self.store.event(account.name, "skip", symbol=spec.name, reason="объём 0")
                continue
            risk_money = spec.distance_money(signal.stop_distance, volume)
            ok, why = risk.can_open_portfolio(
                self.config.portfolio, book_stop["equity"], current_risk, risk_money * rate,
                book_positions, specs, day_pct, new_symbol=spec.name, new_volume=volume,
                new_side=signal.side)
            if not ok:
                self.store.event(account.name, "portfolio_hold", reason=why)
                return 0
            reserved, why, slot = self.store.reserve(account.name, spec.name, volume,
                                                     self.config.portfolio, book["equity"],
                                                     risk_usd=risk_money * rate,
                                                     open_risk_usd=current_risk)
            if not reserved:
                self.store.event(account.name, "portfolio_hold", reason=why)
                return 0
            # The portable directory proved which window answered; only a live login proves whose
            # money this order would move. A manual re-login in that window is otherwise invisible.
            sending = not (self.dry_run or (account.is_real and not self.allow_live))
            if sending:
                who = mt5.account_info()
                if who is None or (int(who.login), str(who.server)) != (account.login, account.server):
                    self.store.release(slot)
                    self.store.event(account.name, "identity_lost", symbol=spec.name,
                                     detail=f"перед входом отвечает "
                                            f"{getattr(who, 'login', None)}/"
                                            f"{getattr(who, 'server', None)}")
                    return 0
            try:
                if not sending:
                    self.store.release(slot)
                    self.store.event(account.name, "entry(gated)", symbol=spec.name,
                                     side=signal.side, volume=volume, reason=signal.reason,
                                     risk_usd=round(risk_money * (rate or 1.0), 2))
                    return 1
                execution = broker.market_order(
                    account.magic, spec, signal.side, volume, signal.stop, signal.target,
                    signal.stop_distance, risk_money)
            except broker.BrokerError as exc:
                self.store.release(slot)
                self.store.event(account.name, "entry_failed", symbol=spec.name,
                                 error=str(exc))
                continue
            except Exception:
                # Anything else unwinds the whole cycle. A slot left behind is a money reservation
                # nobody owns, and it holds the portfolio risk cap for its full 600 s.
                self.store.release(slot)
                raise
            self.store.trade(account.name, snapshot.login, spec.name, signal.side,
                             execution.volume, signal.price, volume, execution.retcode,
                             signal.reason, execution.token)
            self.store.event(account.name, "opened", symbol=spec.name, side=signal.side,
                             volume=execution.volume, price=execution.price,
                             requested=signal.price, reason=signal.reason)
            fresh = broker.open_positions(spec.name, account.magic)
            for position in fresh:
                self.store.set_meta(f"atr:{account.name}:{int(position.ticket)}",
                                    f"{signal.atr:.8f}")
            try:
                fresh_positions = risk.positions_for(account)
                self.store.save_positions(account.name, fresh_positions,
                                          self._mirror_risk_usd(account, fresh_positions, rate))
            except Exception as exc:  # noqa: BLE001 - the lot is already at the broker
                # A read that failed after the fill must not strand the reservation: an owned slot
                # nobody releases holds the portfolio risk cap for its full lifetime, and the next
                # cycle re-reads the positions anyway.
                self.store.event(account.name, "mirror_refresh_failed", symbol=spec.name,
                                 error=f"{type(exc).__name__}: {exc}"[:160])
            self.store.release(slot)
            self._mark_entry(account, spec.name)
            return 1
        return 0

    def _in_cooldown(self, account: Account, symbol_name: str) -> bool:
        minutes = account.strategy.cooldown_minutes
        if minutes <= 0:
            return False
        stamp = self.store.get_meta(f"entry:{account.name}:{symbol_name}")
        if not stamp:
            return False
        try:
            when = dt.datetime.fromisoformat(stamp)
        except ValueError:
            return False
        return dt.datetime.now(dt.timezone.utc) - when < dt.timedelta(minutes=minutes)

    def _mark_entry(self, account: Account, symbol_name: str) -> None:
        self.store.set_meta(f"entry:{account.name}:{symbol_name}",
                            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))

    def _mirror_risk_usd(self, account: Account, positions: list, rate: float) -> dict[int, float]:
        """Price this account's own stop risk in dollars, by the broker that holds it.

        A peer shard reading the mirror cannot resolve these symbols: a contract on another broker
        means a different amount of money per point, and its name may not exist here at all.
        """
        if rate <= 0:
            return {}
        priced: dict[int, float] = {}
        specs: dict[str, broker.BrokerSymbol | None] = {}
        for position in positions:
            name = str(position.symbol)
            if name not in specs:
                specs[name] = broker.symbol_exact(name)
            spec = specs[name]
            if spec is None:
                continue  # stays NULL in the mirror: a reader must not read it as no risk
            money = risk.position_risk_money(
                position, spec, self._atr_entry(account, position, spec),
                account.risk.stop_atr)
            if not money > 0:
                # A contract the broker described with a zero tick value, or a stop we could not
                # measure, prices to 0.0 — and 0.0 in the mirror is a lie the portfolio cap believes.
                continue
            priced[int(position.ticket)] = money * rate
        return priced

    # --- book day -----------------------------------------------------------------------
    def _portfolio_day_pct(self, equity_usd: float) -> float | None:
        """This machine's day result in percent, or None when the day was never measured.

        None is not 0.0. `can_open_portfolio` reads this as "how much has today cost", and a
        missing measurement answered with 0 % tells a broker that nothing has been lost — which is
        exactly what an upgrade, a `reset-day` or a conductor that has not ticked yet leaves behind.
        """
        day, start = self.store.claim_portfolio_baseline(local_host())
        today = risk.server_day_now()
        if not start:
            return None
        if day and today < day:
            # This broker's clock has not reached the portfolio day yet; nothing to compare.
            # The other direction is deliberately allowed: the portfolio day is the slowest live
            # clock, so an account already on the next one is still inside the measured day, and
            # refusing to look would blind the stop exactly when the ahead clock is trading.
            return 0.0
        try:
            value = float(start)
        except ValueError:
            return None
        if value <= 0 or equity_usd <= 0:
            return None
        return (equity_usd - value) / value * 100

    # --- day state ----------------------------------------------------------------------
    def _day_state(self, account: Account, snapshot: risk.AccountSnapshot,
                   server_day: str) -> tuple[risk.DayState, str]:
        stored = self.store.day(account.name)
        day_start = risk.day_start_datetime(server_day)
        net_flow, realized_today, flows_payload = self._cached_day_flows(
            account, server_day, snapshot.balance, day_start)
        opened_with = snapshot.balance - net_flow - realized_today
        if opened_with <= 0:
            # The whole balance arrived today: there is no opening base, so no loss to measure.
            opened_with = snapshot.balance
            net_flow = 0.0
        if stored and stored[0] == server_day:
            state = risk.DayState(day=server_day, start_balance=stored[1],
                                  net_flow=net_flow, realized=stored[2], stopped=stored[3])
        else:
            state = risk.DayState.fresh(opened_with, server_day)
            state.net_flow = net_flow
        state.rollover(opened_with, server_day)
        return state, flows_payload

    def _cached_day_flows(self, account: Account, server_day: str, balance: float,
                          day_start: dt.datetime) -> tuple[float, float, str]:
        """Cache the day's flows, but invalidate the moment the balance moves under them.

        A cache that never expires is worse than no cache: a deposit after the first cycle would
        silently mask the day's loss. The balance is the witness that the flows changed.
        """
        key = f"dayflows:{account.name}:{server_day}"
        cached = self.store.get_meta(key)
        if cached:
            parts = cached.split("|")
            if len(parts) >= 4:
                try:
                    cached_flow, cached_realized, cached_balance, cached_at = (
                        float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
                except ValueError:
                    cached_flow = cached_realized = cached_balance = cached_at = None
                fresh = cached_at is not None and (time.time() - cached_at) < 120
                if (cached_balance is not None and abs(cached_balance - balance) < 1e-9
                        and fresh):
                    return cached_flow, cached_realized, ""
        try:
            net_flow = risk.daily_net_flow(day_start)
        except Exception:  # noqa: BLE001 - history may lag right after midnight
            net_flow = 0.0
        try:
            realized = risk.daily_realized(day_start)
        except Exception:  # noqa: BLE001
            realized = 0.0
        payload = f"{net_flow:.2f}|{realized:.2f}|{balance:.2f}|{time.time():.0f}"
        return net_flow, realized, payload

    def _save_day(self, account: Account, state: risk.DayState) -> None:
        self.store.save_day(account.name, state.day, state.start_balance, state.realized,
                            state.stopped)


def run_shard(config: FleetConfig, store: Store, shard: int, dry_run: bool = False,
              pause_file: pathlib.Path | None = None, allow_live: bool = False,
              ) -> list[WorkerResult]:
    """Run one shard. A pause blocks new entries but never blocks exits."""
    worker = Worker(config, store, pause_file=pause_file, dry_run=dry_run,
                    allow_live=allow_live)
    results = []
    for account in config.by_shard(shard):
        # The pause file can appear mid-cycle: check it per account, not once per shard.
        paused = bool(pause_file is not None and pause_file.exists())
        results.append(worker.run_account(account, allow_entries=not paused))
        time.sleep(0.3)
    return results
