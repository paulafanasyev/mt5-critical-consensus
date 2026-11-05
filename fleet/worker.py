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
import re
import sqlite3
import time
import traceback
import types

import MetaTrader5 as mt5

from . import broker, candidates, network, risk, strategy, terminals
from .config import Account, FleetConfig, PAUSE_FILE, local_host, server_key
from .store import Store

MAX_FAILURES = 5
# How often one account may put one and the same clock sentence in the journal, per direction: the
# hold is re-reported while it lasts, and the recovery is not throttled away by the hold it ends.
# The hold itself is automatic and runs every cycle; the line is only its report. Measured on the
# live demo account on 2026-11-05, a terminal whose symbol was not subscribed yet flapped between the
# live tick and its own cached daily bar on nearly every cycle: twelve copies of these two sentences
# in nine and a half minutes, one every 48 seconds, i.e. about seventy-five an hour into the single
# book the operator reads. The same shape as the conductor's held portfolio day.
DAY_CLOCK_REANNOUNCE_SECONDS = 6 * 3600

# How often a cycle re-asks the running windows who they are logged into, and how long an answer
# stays evidence. Sizing is a fraction of the balance this terminal can see; a second window logged
# into the same account spends the same free margin, and nobody but the broker adds the two up.
# Measured here (4 windows up): the two authorised ones answer in ~0.2 s each, the two unauthorised
# ones cost the full probe timeout — 8.37 s and 8.63 s on two runs of the same field. The cadence is
# what decides how long a manual login stays invisible, so it is set by that cost, not by politeness.
ROSTER_REFRESH_SECONDS = 300.0
ROSTER_EVIDENCE_SECONDS = 900.0
ROSTER_META_KEY = "terminal_roster"
ROSTER_ERROR_META_KEY = "terminal_roster_error"
ROSTER_PROBE_TIMEOUT_MS = 4000
# Measured: an unauthorised window publishes its pipe and answers (-10005, 'IPC timeout') until
# somebody logs in. It holds no margin, so it is not a second driver. Anything else that cannot be
# read — a silent process whose path the OS would not name, a probe that threw — is a blind spot.
ROSTER_SILENT_ERROR = "-10005"
# How a login that answers from several windows gets one named driver: a window whose own terminal
# reports algorithmic trading switched on comes first — among those the directory the registry names
# for this account, otherwise the first path in the casefolded sort — and only when no window reports
# itself enabled does the old order decide. Said next to the choice in the journal so the operator
# does not have to read the code to learn why this window: measured 2026-11-05 at 07:08Z the demo
# login answers from `c:\\mt5-exness` with `terminal_info().trade_allowed = True` (login 114929502,
# 99 212,61 USD), and the same morning that button was off — every entry died on retcode 10027,
# which is why the rule prefers an enabled window at all. `c:\\mt5-team` and `c:\\mt5-real` answer
# `(-10005, 'IPC timeout')`: a window nobody logged into is not a driver, and its flag reads as
# "not seen enabled", not as "off".
DRIVER_ORDER = ("окно с включённым алгоритмическим торговлением: сначала папка из реестра, иначе "
                "первая по casefold-сортировке пути; если не включено нигде — прежний порядок")
# Why this window was named, written into the `driver_chosen` event: the registry folder reports
# itself enabled, another enabled window did, or no window reported itself enabled and the old order
# decided. The third is a machine setting only a human can change, so it is said, not retried.
DRIVER_REGISTRY = "registry"
DRIVER_ENABLED = "enabled"
DRIVER_NONE_ENABLED = "none-enabled"
# What the choice means for the money, said in the journal: the second window still spends the same
# margin either way, and when nothing is enabled the order dies on the client-side switch, not on a
# rule of the park.
DRIVER_DETAIL = ("тот же демо-счёт отвечает ещё из одного окна: окно-водитель названо раз на процесс; "
                 "в нём включено алгоритмическое торговление, ордера идут через копию из реестра, "
                 "которую доказал attach(), и второе окно по-прежнему тратит ту же маржу")
DRIVER_NONE_ENABLED_DETAIL = ("тот же демо-счёт отвечает ещё из одного окна, и ни в одном из них "
                              "перепись не видела включённого алгоритмического торговления "
                              "(кнопка ALGO TRADING в терминале): это машинная настройка, её включает "
                              "человек, парк её не трогает и не обходит; входы открыты, но брокер "
                              "ответит 10027, пока настройка выключена")
# The choice is announced once per process, not once per cycle: it is a fact about the census, and the
# book the operator reads does not need the same sentence every 36 seconds. Deliberately not a note in
# the book — a park that restarted would then make the choice again and stay silent about it.
_DRIVERS_ANNOUNCED: dict[tuple[str, str], str] = {}


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
               "currency": "", "balance": None, "equity": None, "trade_mode": None,
               "trade_allowed": None, "error": ""}
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
                # Which money this is, in the broker's own words: demo, contest or real. Two windows
                # that name the same login but disagree about this are not one account answered
                # twice, and the gate below compares modes rather than trusting the registry label.
                row["trade_mode"] = getattr(account, "trade_mode", None)
                # Whether this window would take an order at all: the terminal's own ALGO TRADING
                # button, read-only. None means the window did not name it, which is not a claim that
                # it is off — it is only the absence of a reason to prefer it as the driver.
                allowed = getattr(window, "trade_allowed", None) if window is not None else None
                row["trade_allowed"] = None if allowed is None else bool(allowed)
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


def account_key(login, server) -> str:
    """The identity the census groups windows by: `login@server`, server normalised.

    One spelling for the gate, the `status` line and the `terminals` warning: what the operator reads
    as a duplicated account has to be the same pair the entry door counted.
    """
    return f"{login}@{_server_key(server)}"


def windows_by_account(rows: list[dict]) -> dict[str, list[dict]]:
    """Every window that named its account, grouped by `account_key`, one entry per window.

    The rows are kept whole and not deduplicated by folder: two windows out of the same folder count
    twice, because it is the count of drivers that spends the margin, not the count of spellings.
    """
    by: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("login") is None:
            continue
        by.setdefault(account_key(row["login"], row.get("server")), []).append(dict(row))
    return by


def duplicated_accounts(rows: list[dict]) -> dict[str, list[str]]:
    """Which accounts answer from more than one window on this machine.

    Key is `login@server` lowercased, value one folder per window that says so.
    """
    return {key: sorted(str(row.get("dir") or row.get("exe") or "") for row in group)
            for key, group in windows_by_account(rows).items() if len(group) > 1}


def window_trade_mode(row: dict) -> int | None:
    """The mode this window's own `account_info` named, or None when it named nothing readable.

    None is not an agreement. A census written before the field existed, or a window that answered
    without it, leaves the park unable to say that both drivers sit on the same pile of demo money,
    and an unmeasured pair keeps the entry door shut exactly like an unmeasured day does.
    """
    try:
        return int(row.get("trade_mode"))
    except (TypeError, ValueError):
        return None


def shared_windows_agree(group: list[dict]) -> bool:
    """Whether several windows naming one login really describe one and the same account.

    Login and server are already the group key, so what can still differ is the broker's mode — a
    window that answers this `login@server` as REAL is not the same money twice, it is a mystery —
    and the folder: a window that hides its path cannot be called anyone's driver.
    """
    if not group:
        return False
    modes = [window_trade_mode(row) for row in group]
    if any(mode is None for mode in modes) or len(set(modes)) != 1:
        return False
    if modes[0] == int(mt5.ACCOUNT_TRADE_MODE_REAL):
        return False
    return all(window_dir(str(row.get("dir") or row.get("exe") or "")) for row in group)


def window_trade_allowed(row: dict) -> bool:
    """Whether THIS window reports algorithmic trading switched on, in the terminal's own words.

    `terminal_info().trade_allowed` is the client-side ALGO TRADING button: a machine setting the
    operator flips, which the park only reads and never touches. An unmeasured row — a census written
    before the field existed, or a window that answered without naming its terminal — reads as "not
    seen enabled": that loses the preference and nothing else, because refusing entries over a button
    would be the park closing its own door on a setting it cannot see from inside.
    """
    return bool(row.get("trade_allowed"))


def enabled_driver_dirs(group: list[dict]) -> list[str]:
    """Folders of the windows in one group that report themselves allowed to trade, casefolded sort."""
    return sorted({window_dir(str(row.get("dir") or row.get("exe") or ""))
                   for row in group if window_trade_allowed(row)} - {""})


def driver_window_choice(group: list[dict],
                         registry_dir: str = "") -> tuple[str, list[str], str]:
    """Name the driver of record among windows that share one account, the rest, and why this one.

    Deterministic by construction, because a choice that flipped between cycles would move the orders
    between two API sessions on one balance: a window whose terminal reports algorithmic trading
    enabled comes first — a driver that cannot send an order only parks the account on retcode 10027,
    which is what the bare sort did on 2026-10-30 — and among those the directory the registry names
    for this account wins, that being the copy `attach()` proves before anything is sent, otherwise
    the casefolded path sort decides, same answer every cycle for the same field.

    When no window reports itself enabled the OLD order decides and says `none-enabled`: the park does
    not get to blame a button it cannot read as off, so it keeps its entries and hands the operator
    the one sentence he can act on, once per process, instead of retrying into the journal.
    """
    dirs = sorted({window_dir(str(row.get("dir") or row.get("exe") or "")) for row in group})
    dirs = [directory for directory in dirs if directory]
    if not dirs:
        return "", [], DRIVER_NONE_ENABLED
    named = window_dir(registry_dir)
    enabled = enabled_driver_dirs(group)
    if named in enabled:
        owner, reason = named, DRIVER_REGISTRY
    elif enabled:
        owner, reason = enabled[0], DRIVER_ENABLED
    else:
        owner, reason = (named if named in dirs else dirs[0]), DRIVER_NONE_ENABLED
    return owner, [directory for directory in dirs if directory != owner], reason


def choose_driver_window(group: list[dict], registry_dir: str = "") -> tuple[str, list[str]]:
    """The driver of record and the rest, without the reason: the rule lives in `driver_window_choice`.
    """
    owner, rest, _reason = driver_window_choice(group, registry_dir)
    return owner, rest


def shared_demo_driver_choice(rows: list[dict], key: str,
                              account: Account | None) -> tuple[str, list[str], str]:
    """The window that drives a login several windows name, the rest, and why; ("", [], "") to block.

    Only demo money gets a chosen session: a real account never has one window picked for it silently,
    so an account that is real, missing from the registry, or sharing its field with a window nobody
    could read answers with ("", [], "") and the caller keeps blocking. Which of the agreed windows
    drives is `driver_window_choice`'s business: the one that can actually trade. One agreed window is
    already a shared login — the census cannot see the park's own copy, see the note inside.
    """
    if account is None or account.is_real:
        return "", [], ""
    if blind_windows(rows):
        return "", [], ""
    group = windows_by_account(rows).get(key) or []
    # One window is enough, and on this machine it is all the census can ever see: it is taken before
    # `attach()` opens anything (every probe ends with `shutdown()`), and a demo window lives one cycle
    # and dies in `finally`, so our own folder is not in it. Measured 2026-11-05: after the operator
    # logged out of `c:\\mt5-team` the group for `114929502@MetaQuotes-Demo` was one row
    # (`c:\\mt5-exness`, `trade_allowed = True`) and a rule that demanded two rows kept the demo
    # account's entries shut from 06:07Z — the door opened and closed on how many *foreign* windows
    # happened to be up, which was never the question. The caller only asks when it already saw a
    # window that is not ours; the answer it needs is whether those windows describe one demo pile.
    if not group or not shared_windows_agree(group):
        return "", [], ""
    return driver_window_choice(group, window_dir(str(account.data_dir)))


def shared_demo_driver(rows: list[dict], key: str, account: Account | None) -> tuple[str, list[str]]:
    """The driver of record and the rest, without the reason: see `shared_demo_driver_choice`.
    """
    owner, rest, _reason = shared_demo_driver_choice(rows, key, account)
    return owner, rest


def _server_key(text) -> str:
    """One server name, however it was typed.

    The broker writes its own spelling into `account_info`, the registry is hand-edited, and the
    portable marker is a text file someone saved with a trailing space. Every comparison of two
    account identities has to use the same normalisation, and it lives in `config` so the terminal
    log reader and the real-money gate use it too: attach() accepted `"Exness-Demo "`, the gate
    before the send compared the raw strings, and the park then refused its own account on every
    symbol while the operator read `identity_lost` in the journal.
    """
    return server_key(text)


_NUMBERS = re.compile(r"\d+(?:[.,]\d+)?")


def _refusal_shape(text: str) -> str:
    """Which broker rule fired, without the measurements that made it fire.

    A refusal names a distance, and the distance is a live number: fingerprint the sentence as it
    stands and the same refusal is re-announced every tick until the position closes, which is how a
    park that asked the broker once reads in the journal as a broker in trouble.
    """
    return _NUMBERS.sub("#", str(text or ""))


def _cash_ruler(spec: broker.BrokerSymbol) -> tuple[str, str] | None:
    """(fingerprint, detail) for the ruler this lot's money was measured with — when it needs saying.

    None is the ordinary case: the terminal's own calculator repeated the tick field and nothing
    contradicted it. Saying that every cycle would be noise; saying the other cases is the operator's
    only view of which number a dollar was cut from. The direction is written out of the two fields
    themselves, never out of a divergence ratio that loses its sign.
    """
    if spec.tick_size <= 0:
        return None
    if spec.tick_value_source == "tick-unchecked":
        # The broker's calculator was never asked, or had nothing to arbitrate: the tick stands, but
        # it stands unproved. `risk_basis` is the operator's only clue about which number a dollar was
        # cut from, so "the calculator repeated the tick" and "nobody could ask it" must not print
        # alike — the first is a proof, the second is only the absence of a contradiction.
        return (_refusal_shape(f"tick-unchecked:{spec.cash_probe_error}"),
                f"расчёт брокера молчал ({spec.cash_probe_error}): объём и риск меряны по тик-оценке, "
                "которую брокер не подтвердил")
    if spec.tick_value_source not in ("contract", "tick-proven"):
        return None
    tick_based = (spec.tick_value_raw or spec.tick_value) / spec.tick_size
    contract_based = spec.contract_size
    if tick_based <= 0 or contract_based <= 0:
        return None
    shown = (f"тик {tick_based:.6g}, контракт {contract_based:.6g}, "
             f"расчёт брокера {spec.calc_per_unit:.6g}")
    fingerprint = f"{spec.tick_value_source}:{tick_based:.6g}:{contract_based:.6g}"
    if spec.tick_value_source == "tick-proven":
        return fingerprint, (f"расчёт брокера подтвердил тик-оценку ({shown}): контракт с ней "
                             "расходится, но это не те деньги — объём и риск меряны по тику")
    ratio = contract_based / tick_based
    if ratio > 1:
        words = f"лот по контракту в {ratio:.2f} раз меньше лота по тик-оценке"
    else:
        words = f"лот по контракту в {1.0 / ratio:.2f} раз больше лота по тик-оценке"
    return fingerprint, (f"расчёт брокера подтвердил контракт ({shown}): {words}; объём и риск "
                         "меряны по контракту")


def _identity_matches(who, account: Account) -> bool:
    """Whether the terminal answering right now is the account this risk slot was reserved for.

    A window that answers with no login is a mismatch, not a crash: `int(None)` would raise through
    the reservation it was supposed to cancel and leave a dollar slot nobody owns for its whole life.
    """
    if who is None:
        return False
    try:
        login = int(who.login)
        server = _server_key(who.server)
    except (TypeError, ValueError, AttributeError):
        # An object that has no `server` at all is not a terminal that answered with somebody else's
        # login, and the caller cannot tell the two apart from outside: both mean "nobody here proved
        # this money", and neither may be a crash that strands the reservation this check protects.
        return False
    return (login, server) == (account.login, _server_key(account.server))


def _host_key(name: str) -> str:
    """A key that belongs to one machine.

    The census is a fact about THIS machine's process list and the book is shared between boxes
    (that is why the portfolio day keys are host-scoped), so an unscoped census would have box A
    trade on the windows box B counted — and its own second driver would never appear.
    """
    return f"{name}@{local_host()}"


def census() -> list[dict]:
    """A fresh look at the windows of this machine, written nowhere.

    `refresh_roster` does the same walk and files the answer as the evidence every shard trades on.
    That is why a diagnostic must not call it: a forced scan taken while one window is mid-restart,
    with its 4 s probe timeouts, replaces the row that decides who may open — and the park can go
    from `account_single_window` to `roster_unknown` (or back) for a quarter of an hour because
    somebody ran a command whose own help says it only looks.
    """
    processes = terminals.running_terminals()
    live = terminals.pids_alive([pid for pid, _path in processes])
    return scan_roster([path for pid, path in processes if pid in live])


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
    scanned = census()
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
        # No stand means the machine's stand, not "no stand": a Worker built by hand is still a
        # Worker on the operator's machine, and the kill switch is a fact about that machine.
        self.pause_file = PAUSE_FILE if pause_file is None else pathlib.Path(pause_file)
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
        if info.login != account.login or _server_key(info.server) != _server_key(account.server):
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
        if self.dry_run and account.is_real:
            # `--dry-run` promises that nothing of the operator's is touched, and a real account is the
            # one place where a paper cycle cannot keep that promise: reaching it means logging into
            # the broker that holds his money, while the cycle itself places no protective level and
            # closes nothing. Refused here, before a terminal is started — so the book never reads a
            # paper lot on a real login, which is the one thing a status line must not print.
            self._announce_once(_host_key(f"dryrun_refused:{account.name}"), account.name,
                                "dry_run_refused", login=account.login, server=account.server,
                                detail="бумажный цикл не подключается к счёту с настоящими деньгами")
            return WorkerResult(account.name, "error",
                                f"{account.login}: dry-run на реальном счёте — не подключаемся")
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

    def _held_day(self, account: Account, server_day: str) -> str:
        """The day this account is already being measured on, when its clock came back without it.

        `risk.server_day_now()` reads the terminal's own quotes, and a quote can be a cached daily
        bar from the last sync of a symbol nobody subscribed: measured on the live park 2026-11-05,
        three cycles in a row of one and the same window answered 2026-11-03, 2026-11-05,
        2026-11-03, 35-70 seconds apart. The conductor refuses that
        for the portfolio — `Conductor._remember_baseline` holds the day, because rebasing onto
        equity that already lost silently re-arms the daily stop. The account has the same hazard one
        level down and nothing was standing in front of it: `DayState` compares the day as a string,
        so a day that goes backwards is `not stored[0]`, which reads as a new day — the base moves,
        `realized` is forgotten and `stopped` goes back to False, and an account that had just been
        denied entries at minus one percent takes them again on the same losing day.

        So the day a stop was armed on outranks the clock that lost it. The cost is that an account
        moved to a broker whose clock really is behind resumes entries no earlier than the held day
        catches up: a late day is a slower day, never a re-armed stop.
        """
        stored = self.store.day(account.name)
        held = bool(stored and stored[0] and server_day and server_day < stored[0])
        self._note_day_clock(account, "held" if held else "live", stored[0] if held else "",
                              server_day)
        return stored[0] if held else server_day

    def _note_day_clock(self, account: Account, state: str, held_day: str, clock_day: str) -> None:
        """Report a clock that slipped behind its measured day, once per direction per window.

        The hold itself runs every cycle whether or not the journal says so; what the journal must
        not do is write one sentence seventy-five times an hour, which is what a flapping symbol did
        before the cadence (measured live 2026-11-05: twelve lines in nine and a half minutes, one
        every 48 seconds). Each direction keeps its own clock in `dayclock:held:<account>` /
        `dayclock:live:<account>`, because a shared one loses the second sentence: on a clock that
        answers stale and live on alternating cycles, the line saying "the clock caught up" arrives
        half a minute after the line saying "the day is held" and would be throttled away forever,
        leaving the operator with an open hold the journal never closes.
        """
        if state == "live" and not self._meta(f"dayclock:held:{account.name}"):
            # Nothing was ever held on this account: there is no recovery to announce.
            return
        key = f"dayclock:{state}:{account.name}"
        try:
            written = float(self._meta(key) or "")
        except ValueError:
            written = 0.0
        now = time.time()
        if written and now - written < DAY_CLOCK_REANNOUNCE_SECONDS:
            return
        if not self._note_meta(key, f"{now:.3f}"):
            return
        if held_day:
            self._landed(account.name, "day_clock_regressed", key, day=held_day,
                         clock=clock_day,
                         detail="часы терминала отстали от замеренного дня: день держится, база и "
                                "вооружённый дневной стоп не тронуты")
        else:
            self._landed(account.name, "day_clock_resumed", key, day=clock_day)

    def _cycle(self, account: Account, allow_entries: bool) -> WorkerResult:
        server_day = self._held_day(account, risk.server_day_now())
        snapshot = risk.AccountSnapshot.read(server_day)
        if snapshot is None:
            return WorkerResult(account.name, "error", "нет счёта")
        if not _identity_matches(snapshot, account):
            # The word that used to stand in front of an exit was `--live` — the operator's sentence
            # about money going IN, not about taking it out. A park restarted without it, which is
            # the default of every restart path, dropped trailing, giveback and break-even off every
            # lot it had been permitted to open and left only the broker's own stop. What belongs in
            # front of a close is the address: the window proved itself at `attach`, and this is the
            # same proof asked again after the connect, over the money the cycle is about to act on.
            self._report_change(f"exitidentity:{account.name}",
                                f"{snapshot.login}/{snapshot.server}",
                                account.name, "exit_identity_lost",
                                detail=f"отвечает {snapshot.login}/{snapshot.server}, а не "
                                       f"{account.login}@{account.server} — свои лоты не тронуты")
            return WorkerResult(account.name, "error",
                                f"привязан чужой счёт {snapshot.login}/{snapshot.server}")
        if self._meta(f"exitidentity:{account.name}"):
            # Cleared, not left: the next window that answers wrong is a new fact, and a note still
            # holding the old address would say it once per cycle as if nothing had changed.
            self._note_meta(f"exitidentity:{account.name}", "")
            self.store.event(account.name, "exit_identity_restored",
                             login=snapshot.login, server=snapshot.server)
        rate = snapshot.rate_to_usd(account.currency_scale)
        day, flows_payload = self._day_state(account, snapshot, server_day)
        positions = risk.positions_for(account)  # includes manual positions: they move equity

        closed = self._manage_positions(account, day, positions, snapshot.currency, rate)
        # These three money reads used to sit in front of the management call above, and each of them
        # is a condition a lot survives: an account at zero equity, a real account whose balance the
        # broker has already eaten, a demo priced in a currency the book cannot quote. In all three the
        # lot is still in the market and the exit rule still has a price to work from — closing it does
        # not need a rate, only the address proved above. What those readings do gate is the day's
        # accounting and new openings, so that is where they stand now.
        if snapshot.equity <= 0:
            return WorkerResult(account.name, "flat", "нулевой баланс", closed=closed)
        if account.is_real and snapshot.balance <= 0:
            return WorkerResult(account.name, "flat", "реальный счёт без денег", closed=closed)
        if snapshot.rate_to_usd() <= 0 and not account.is_real:
            return WorkerResult(account.name, "flat",
                                f"нет курса {snapshot.currency}→USD: книга счёт не видит",
                                closed=closed)
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

    def _meta(self, key: str) -> str | None:
        """A journal read that cannot break the caller.

        The notes are a cache and a report. A database locked by another shard for longer than the
        busy timeout is a reason to say nothing this cycle, not a reason to leave a live position
        without its exit rule.
        """
        try:
            return self.store.get_meta(key)
        except sqlite3.Error:
            return None

    def _note_meta(self, key: str, value: str) -> bool:
        """Write a journal note; False when the journal would not take it."""
        try:
            self.store.set_meta(key, value)
            return True
        except sqlite3.Error:
            return False

    def _announce_once(self, key: str, account_name: str, kind: str, **detail) -> None:
        """Write an event the first time a condition appears, not once per cycle per symbol.

        A 90 s cycle over a hundred accounts would otherwise fill the journal the operator has to
        read with the same line, and the line that matters would be lost in it.
        """
        if self._meta(key) == "1":
            return
        if not self._note_meta(key, "1"):
            # The journal is locked. A report that cannot be written must not stop the caller,
            # because the same call site sits between this account and its exits; next cycle tries
            # again and the condition is still true either way.
            return
        # An announcement that never reached the book is not an announcement: the note has to go back
        # to "not said", or the refusal this account was denied by disappears with the lock that ate
        # the row, and the next cycle reads the same condition and stays silent forever.
        self._landed(account_name, kind, key, **detail)

    def _landed(self, account_name: str, kind: str, note_key: str, **detail) -> bool:
        """Journal one event, and undo a `note_key` marker when the book did not take the row.

        `Store.event` writes the `.jsonl` trail whatever happens, so a rejected row leaves evidence
        outside the database; the database is what every reader of this park queries.
        """
        if self.store.event(account_name, kind, **detail):
            return True
        self._note_meta(note_key, "")
        return False

    def _second_window(self, account: Account) -> str:
        """Refuse new entries while this account is open in a window the park does not drive.

        attach() already proved which window this cycle trades through. The census answers the other
        half: whether somebody else — a manual login, another shard, a stray copy — is in the same
        money at the same time. Free margin is one number on the broker's side, and two drivers each
        sizing to a fraction of the balance spend it twice; exits must keep running regardless,
        because an unmanaged position is worse than an unused signal.

        Measured here on 2026-11-05: two operator windows (`c:\\mt5-exness`, `c:\\mt5-team`) answered
        the same demo login, the same server, the same mode and the same 99 212,61 — one pile of money
        seen twice, not two drivers of two piles. Later the same morning one of them logged out and the
        pile was seen once by the census plus once by us; what that leaves ambiguous is only "through
        which session do we act", and `attach()` settles that already by proving the registry's own copy
        before a single order. So a DEMO login that every window describes identically gets one window
        named as its driver of record and keeps its entries; that window is the one whose terminal
        reports algorithmic trading enabled, because a driver that cannot send an order is only a
        taller refusal, and `none-enabled` says so once per process instead of retrying into the
        journal; the operator reads the choice and its reason in the journal (`driver_chosen`)
        instead of inferring it from a book that traded anyway.

        What still stands, with its wording unchanged: a real account — real money is never given a
        silently chosen session —, windows that disagree about the broker's mode or hide their folder,
        a window the census could neither read nor measure silent, and a census that is missing or out
        of date.

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
        driver = ""
        driver_reason = ""
        if rows is None:
            fingerprint = "no-census"
            reason = ("переписи окон этой машины нет (или она старше часа): непонятно, кто ещё "
                      "водит эти деньги — входы закрыты, выходы идут как обычно")
        else:
            mine = window_dir(str(account.data_dir))
            others = sorted(str(row.get("dir") or row.get("exe") or "")
                            for row in rows
                            if row.get("login") == account.login
                            and _server_key(row.get("server")) == _server_key(account.server)
                            and str(row.get("dir") or "") != mine)
            blind = sorted(str(row.get("dir") or row.get("exe") or "") or "<окно без пути>"
                           for row in blind_windows(rows))
            if others and not blind:
                driver, _rest, driver_reason = shared_demo_driver_choice(
                    rows, account_key(account.login, account.server), account)
            if others and not driver:
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
        if driver:
            # The block is over, but the field is not one window: saying `account_single_window` here
            # would file two drivers as one. The driver sentence above is what lifts the door.
            if self.store.get_meta(key):
                self.store.set_meta(key, "")
            self._announce_driver(account, driver, others, driver_reason)
            return ""
        if self.store.get_meta(key):
            # Only a census that actually saw the field can lift the block, so the all-clear always
            # means what its name says: this machine was counted and holds one window for this money.
            self.store.set_meta(key, "")
            self.store.event(account.name, "account_single_window", login=account.login,
                             server=account.server, census="fresh")
        return ""

    def _announce_driver(self, account: Account, owner: str, others: list[str],
                         reason: str) -> None:
        """Write which window the park chose for a login that answers from several, once per process.

        Deliberately not a `meta` note: a note in the book would let the next start of this park make
        the same choice and say nothing about it, and the point of choosing is that the operator reads
        the decision rather than inferring it from a book that traded anyway. Keyed by the book as well
        as the account, so two stores in one process each get their own sentence, and re-announced
        within one process when the windows move and the choice moves with them.

        The reason travels with the choice: `registry` when the registry's own folder reports
        algorithmic trading enabled, `enabled` when a different enabled window won, `none-enabled`
        when no window reported itself enabled and the old order decided. The last is the sentence the
        operator can act on — ALGO TRADING is a button in someone else's terminal, so it is said once
        per process rather than chased with retries the terminal refuses every time.
        """
        rest = sorted(d for d in others if d != owner)
        fingerprint = "|".join([owner, *rest, reason])
        note = (account.name, str(self.store.path))
        if _DRIVERS_ANNOUNCED.get(note) == fingerprint:
            return
        _DRIVERS_ANNOUNCED[note] = fingerprint
        self.store.event(account.name, "driver_chosen", window=owner, login=account.login,
                         server=account.server, others=rest, order=DRIVER_ORDER, mode="demo",
                         reason=reason, trade_allowed=reason != DRIVER_NONE_ENABLED,
                         detail=DRIVER_DETAIL if reason != DRIVER_NONE_ENABLED else
                         DRIVER_NONE_ENABLED_DETAIL)

    # --- position management ------------------------------------------------------------
    def _manage_positions(self, account: Account, day: risk.DayState, positions: list,
                          currency: str = "", rate: float = 0.0) -> int:
        closed = 0
        # A lot the park did not place is the operator's, not the park's. Manual positions belong
        # in the risk caps and the equity mirror — they move the money this account trades with —
        # but moving their stop to break-even or closing them on a trailing rule is taking the
        # trade away from the person who opened it. Ownership is the magic number we stamp on
        # every order; anything else on the account is reported and left alone.
        own = risk.own_in(account, positions)
        foreign = [p for p in positions if risk.magic_of(p) != int(account.magic)]
        # The book's picture of the previous cycle, which is the only witness for the question the
        # live list cannot ask: which of our lots the broker has already closed. Whether a lot grew
        # volume nobody here ordered is asked of `lot_witness`, which is not a photograph.
        mirror = self.store.mirrored_positions(account.name)
        self._journal_broker_closes(account, mirror, positions, currency, rate)
        merged = self._merged_lots(account, self.store.lot_witnesses(account.name), positions)
        specs: dict[str, broker.BrokerSymbol | None] = {}
        for position in own:
            name = position.symbol
            ticket = int(position.ticket)
            if ticket in merged:
                continue  # reported: this ticket is two people's money now, and ours is not all of it
            if name not in specs:
                specs[name] = broker.symbol_exact(name)
            spec = specs[name]
            if spec is None:
                self.store.event(account.name, "unmanaged", symbol=name,
                                 reason="нет спецификации символа")
                continue
            atr_entry = self._atr_entry(account, position, spec)
            if atr_entry is None:
                # Nothing can say what this lot was sized from, so the rules that borrow that
                # number stand down. The broker's own stop and target, the giveback from the peak
                # and the time cap are priced by the position itself and keep running: 0.0 is how
                # `strategy` spells "no ATR", and unlike an invented ATR it moves nothing.
                self._announce_once(f"lotunpriced:{account.name}:{ticket}", account.name,
                                    "lot_atr_unpriced", symbol=name, ticket=ticket,
                                    detail="входной ATR неизвестен: правила по ATR стоят, "
                                           "брокерские стоп/тейк, откат и время работают")
                atr_entry = 0.0
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
            if be_level is not None and not self.dry_run:
                # The server owns the minimum distance it accepts, and for a standing lot it also
                # locks the position while the price is inside `freeze_level` of its stop or target.
                # Asking with an order buys a rejected modify that reads like a broker outage; the
                # refusal leaves the protection exactly where the broker has it now.
                key = f"bestops:{account.name}:{ticket}"
                # The freshest look the park can get before it asks: `price` above was read before a
                # sqlite peak-write and before `break_even_level` re-read the book, and the level it
                # produced is only legal while the market is on its own side of it. Measured
                # 2026-11-05 on USDJPY: two cycles sent a break-even stop into a market that had
                # already fallen through that level, and the answer both times was
                # `invalid stops (retcode 10016)` while the pre-flight guard stayed silent.
                fresh = mt5.symbol_info_tick(name)
                live_price = float(fresh.bid if is_buy else fresh.ask) if fresh is not None \
                    else price
                refused = broker.stops_refusal(spec, is_buy, live_price, be_level,
                                               float(position.tp),
                                               current_sl=float(position.sl),
                                               current_tp=float(position.tp))
                if refused:
                    # Shaped, not raw: the refusal quotes the distance it saw and the distance moves
                    # every tick. An unshaped fingerprint is a new event each cycle for one broker rule.
                    self._report_change(key, _refusal_shape(refused), account.name,
                                        "break_even_refused", symbol=name, ticket=ticket,
                                        detail=refused)
                else:
                    try:
                        broker.modify_position(ticket, name, be_level,
                                               float(position.tp), spec.digits)
                        self.store.event(account.name, "break_even", symbol=name,
                                         ticket=ticket, sl=be_level)
                        if self._meta(key):
                            # Cleared, not left: a refusal next week on the same level is a new fact.
                            self._note_meta(key, "")
                    except broker.BrokerError as exc:
                        # The geometry the broker saw, not only the word it answered: without the
                        # level and the price this event is undiagnosable after the fact, and the
                        # market it describes is gone.
                        self.store.event(account.name, "break_even_failed", symbol=name,
                                         ticket=ticket, error=str(exc), sl=be_level,
                                         tp=float(position.tp), price=live_price)

            reason = strategy.exit_reason(account, position, spec, peak, atr_entry)
            if not reason:
                continue
            if self.dry_run:
                # Only the paper mode stands down from a close. A real account without `--live` is
                # refused at the entry door, which is where that word belongs; here the address was
                # proved above and the lot is already in the market.
                self.store.event(account.name, "exit(dry run)", symbol=name, reason=reason,
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
            profit = float(position.profit)
            # `day.realized` counts only the closes ordered here: the broker's own history already
            # carries every other close into the balance, so borrowing it again would double the day.
            day.realized += profit
            self.store.close_position_row(account.name, ticket)
            self._report_closed(account, ticket, name, reason, profit, currency, rate,
                                by="парк")
        self._report_foreign_lots(account, foreign)
        return closed

    def _report_closed(self, account: Account, ticket: int, symbol: str, reason: str,
                       profit: float, currency: str, rate: float, by: str,
                       priced: bool = True) -> None:
        """Journal one finished lot in the money it was booked in and the money we can compare.

        The profit the broker reports is in the account's own currency, which on this park can be a
        ruble or a cent account: joined to the candidate book without a rate it would be read as
        dollars. `rate_to_usd` travels with the row so the ledger can price what it says, and stays
        NULL when nobody could resolve the rate — an unpriced close is missing from the sum, not 0.
        """
        self.store.event(account.name, "closed", symbol=symbol, ticket=ticket, reason=reason,
                         by=by, profit=round(profit, 2) if priced else None,
                         currency=currency or "", rate_to_usd=rate if rate > 0 else None,
                         profit_usd=(round(profit * rate, 2)
                                     if priced and rate > 0 else None))

    def _journal_broker_closes(self, account: Account, mirror: dict, positions: list,
                               currency: str, rate: float) -> None:
        """Write the exits the broker took without asking this park.

        Every close ordered here is journalled where it is ordered, so a ticket that stood in last
        cycle's book and is absent from this one left on its own — at its own stop or target, from
        the platform, or by a server rule. Missing those is what makes a candidate book that never
        pays out: the trade ran and finished, and the ledger is left calling it open forever while
        the drift bill keeps being counted against a profit column that stays empty.
        """
        live = {int(p.ticket) for p in positions}
        ours = int(account.magic)
        gone = sorted((ticket, symbol) for ticket, (magic, _volume, symbol) in mirror.items()
                      if magic == ours and ticket not in live)
        for ticket, symbol in gone:
            result = broker.position_result(ticket)
            # An empty history is not a flat exit: the broker settled this lot somewhere we cannot
            # read today, so the close is journalled unpriced rather than as a zero.
            profit, comment = result if result else (0.0, "")
            self._report_closed(account, ticket, symbol, comment or "стоп/тейк брокера",
                                profit, currency, rate, by="брокер", priced=result is not None)
            self.store.close_position_row(account.name, ticket)

    def _merged_lots(self, account: Account, witness: dict[int, float],
                     positions: list) -> set[int]:
        """Tickets of ours carrying volume we did not add, reported and left alone.

        On a netting account the operator's manual trade in the same direction folds into the
        position this park opened: the magic still says ours, but the ticket is now two people's
        money, and a trailing exit here would close his lot along with ours without ever asking.
        The witness holds how many lots of each ticket this park paid for, so growth past it stops
        modify and close for that ticket.

        It is a record of its own, not the mirror: the mirror goes stale after its own TTL, and a
        lot that folded while this shard was dark came back as "new to the book" — no previous
        volume it could have grown past — and was closed whole, the operator's half with it. And a
        witness kept in the mirror would have been rewritten with the merged volume at the end of
        the very cycle that spotted the merge, so the next cycle would have closed it anyway.

        A witness is written where this park's own money enters a ticket, at the fill, and follows a
        lot down when it shrinks: a shrunken ticket is not a merge — a partial close by either of
        us, or the broker trimming it — and is reported by the closes and managed as it stands.
        """
        merged: dict[int, tuple[float, float]] = {}
        for position in positions:
            if risk.magic_of(position) != int(account.magic):
                continue
            ticket = int(position.ticket)
            volume = float(position.volume)
            asked = witness.get(ticket)
            if asked is not None and volume > asked + 1e-9:
                merged[ticket] = (asked, volume)
            # Written back whatever the answer is, and `note_lot_witness` keeps the smaller of the
            # two numbers: a lot that shrank is followed down, a lot that grew is not adopted, and a
            # ticket the park has been standing off from keeps its witness alive through the prune.
            # A ticket nobody has recorded yet takes its first number here — it has no previous
            # volume it could have grown past, so the volume on the book today is all anybody here
            # owns for it, and from the next cycle the merge question has a witness of its own.
            self.store.note_lot_witness(account.name, ticket, volume)
        if merged:
            self._report_change(
                _host_key(f"lotmerged:{account.name}"),
                "|".join(f"{ticket}:{asked:.4f}:{volume:.4f}"
                         for ticket, (asked, volume) in sorted(merged.items())),
                account.name, "lot_merged_with_manual",
                lots=[{"ticket": ticket, "asked": asked, "now": volume}
                      for ticket, (asked, volume) in sorted(merged.items())],
                detail="ручной лот влился в наш на netting-счёте: этот билет парк не модифицирует "
                       "и не закрывает")
        return set(merged)

    def _report_foreign_lots(self, account: Account, foreign: list) -> None:
        """Say which lots on this account the park did not place — after the exits were taken.

        Reported last on purpose: the operator's trades do not need a journal line before this
        account's own risk is managed, and a write that fails must never sit in front of an exit.
        """
        key = _host_key(f"foreign_positions:{account.name}")
        if not foreign:
            if self._meta(key):
                self._note_meta(key, "")
                self.store.event(account.name, "manual_lots_cleared",
                                 detail="ручных позиций на счёте больше нет")
            return
        # The volume belongs in the fingerprint: a manual lot that grew — or folded into one of our
        # tickets, which `lot_merged_with_manual` reports separately — is a different fact, and a
        # fingerprint of bare tickets would keep quiet about it.
        fingerprint = "|".join(f"{int(p.ticket)}:{risk.magic_of(p)}:{float(p.volume):.4f}"
                               for p in sorted(foreign, key=lambda p: int(p.ticket)))
        self._report_change(
            key, fingerprint, account.name, "foreign_positions",
            tickets=sorted(int(p.ticket) for p in foreign),
            magics=sorted({risk.magic_of(p) for p in foreign}),
            lots=round(sum(float(p.volume) for p in foreign), 4))
        # A lot with our own entry note in the journal but someone else's magic is money the park
        # put at risk and nobody now manages: the config magic changed, or a second park instance
        # runs the same account. It must not be filed under "the operator's trade".
        noted = [ticket for ticket in sorted(int(p.ticket) for p in foreign)
                 if self._meta(f"atr:{account.name}:{ticket}")]
        if noted:
            self._report_change(_host_key(f"park_lot_unattributed:{account.name}"),
                                "|".join(map(str, noted)), account.name,
                                "park_lot_unattributed", tickets=noted, magic=account.magic,
                                detail="наша запись входа есть, магии нет — лот никто не ведёт")

    def _report_change(self, key: str, fingerprint: str, account_name: str, kind: str,
                       **detail) -> None:
        """Announce a condition that keeps changing: once per distinct value, from one meta row.

        `_announce_once` says a thing once ever. A manual trade is a new fact every time one
        appears, so it has to be re-announced — but the fingerprint goes in the value, not the
        key, or the meta table grows one row per trade the account ever made.
        """
        if (self._meta(key) or "") == fingerprint:
            return
        if not self._note_meta(key, fingerprint):
            return
        self._landed(account_name, kind, key, **detail)

    def _atr_entry(self, account: Account, position,
                   spec: broker.BrokerSymbol) -> float | None:
        """The ATR this lot was sized from, or None while nobody can say.

        `atr:<account>:<ticket>` is an authorship note: it is written when this park places the lot,
        and it is what makes a lot carrying someone else's magic still ours. Two ways the old code
        turned that note into a fabrication. A journal locked by another shard reads as an absent
        note, so the value computed here was persisted as the entry ATR of a lot that had been sized
        with a different number — and every later cycle believed the note it had just poisoned. And a
        lot with no stop priced `abs(price_open - 0)`: gold at 2000 gave an ATR of 2000, and the
        break-even and trailing rules then moved a stop by a distance nobody had ever asked for.
        """
        key = f"atr:{account.name}:{int(position.ticket)}"
        try:
            stored = self.store.get_meta(key)
        except sqlite3.Error:
            self._announce_once(f"atrlock:{account.name}:{int(position.ticket)}", account.name,
                                "entry_atr_unreadable", symbol=spec.name,
                                ticket=int(position.ticket),
                                detail="журнал залочен: нота входа не читается, лот без правила")
            return None
        if stored:
            try:
                return float(stored)
            except ValueError:
                pass
        stop = float(getattr(position, "sl", 0.0) or 0.0)
        stop_distance = abs(float(position.price_open) - stop) if stop > 0 else 0.0
        if stop_distance <= 0 or account.risk.stop_atr <= 0:
            return None
        atr_value = stop_distance / account.risk.stop_atr
        self._note_meta(key, f"{atr_value:.8f}")
        return atr_value

    # --- entries ------------------------------------------------------------------------
    def _stand_holds(self, account: Account) -> bool:
        """Whether the operator's kill switch stops this process from opening a lot.

        The conductor and `run_shard` both look at the stand before they call here, but they are not
        the only ways into a Worker: a hand-built one used to buy lots on a parked park. The check
        belongs to the code that sends, not to the caller that remembered to ask. Exits are
        deliberately not gated — a kill switch that strands an open position without its stop is
        worse than the entry it prevented.
        """
        key = f"stand:{account.name}"
        if not self.pause_file.exists():
            if self._meta(key) == "on":
                self._report_change(key, "off", account.name, "entries_resumed",
                                    stand=str(self.pause_file))
            return False
        self._report_change(key, "on", account.name, "entries_paused",
                            stand=str(self.pause_file))
        return True

    def _try_entries(self, account: Account, snapshot: risk.AccountSnapshot, positions: list,
                     day: risk.DayState, rate: float) -> int:
        if self._stand_holds(account):
            return 0
        if self._second_window(account):
            # Announced once per set of windows, inside _second_window: the trail does not need the
            # same warning every cycle. Position management already ran, so exits are not blocked.
            return 0
        allowed, reason = risk.can_open(account, snapshot, day, positions)
        if not allowed:
            self.store.event(account.name, "hold", reason=reason)
            return 0
        own = risk.own_in(account, positions)
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
        naked: list[int] = []  # our account's lots whose stop nobody wrote down
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
            money = risk.position_risk_money(position, spec, 0.0, account.risk.stop_atr)
            if not money > 0:
                # A lot with no stop on this account is the operator's, and its risk is a distance
                # nobody has written down: counted as unpriced, which holds entries, rather than
                # guessed from an ATR this park never traded and stamped as our authorship.
                unpriced_risk += 1  # a zero here is a broker that described no tick value
                naked.append(position.ticket)
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
            self.store.event(account.name, "portfolio_risk_unpriced", count=unpriced_risk,
                             naked_tickets=naked[:10])
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
            # One lot per symbol is this strategy's rule about its own trades. The operator's
            # position on the same symbol is his view, not a reason for the park to hold back.
            same_symbol = [p for p in own if p.symbol == spec.name]
            if len(same_symbol) >= account.strategy.max_positions_per_symbol:
                continue
            if self._in_cooldown(account, spec.name):
                continue
            if spec.tick_value_source == "unproven":
                # Two of the broker's own fields say different money and its calculator refused to
                # settle it. That is not a reason to pick one: the lot would be sized by preference,
                # and a preference in this branch is a factor of ten.
                # Shaped, not raw: the refusal quotes the three numbers it compared, and two of them
                # move with the price. A fingerprint of the sentence as written is a fresh event every
                # cycle for one stable broker rule.
                self._report_change(_host_key(f"cash_ruler:{account.name}:{spec.name}"),
                                    _refusal_shape(f"unproven:{spec.cash_probe_error}")[:120],
                                    account.name, "cash_ruler_unproven", symbol=spec.name,
                                    contract_size=spec.contract_size,
                                    profit_currency=spec.profit_currency,
                                    detail=f"входы по инструменту закрыты: {spec.cash_probe_error}")
                self.store.event(account.name, "skip", symbol=spec.name,
                                 reason="стоимость лота не доказана брокером")
                continue
            volume = risk.size_for(account, spec, signal.stop_distance, snapshot.balance)
            if volume <= 0:
                self.store.event(account.name, "skip", symbol=spec.name, reason="объём 0")
                continue
            risk_money = spec.distance_money(signal.stop_distance, volume)
            per_lot = spec.distance_money(signal.stop_distance, 1.0)
            ruler = _cash_ruler(spec)
            if ruler:
                # Fingered, not announced once: a broker that republishes a field has changed the
                # money this account is risking, and a note that never clears would let the old
                # verdict stand forever next to the new one.
                self._report_change(_host_key(f"cash_ruler:{account.name}:{spec.name}"), ruler[0],
                                    account.name, "cash_ruler", symbol=spec.name,
                                    contract_size=spec.contract_size,
                                    profit_currency=spec.profit_currency,
                                    tick_value=spec.tick_value_raw or spec.tick_value,
                                    calc_per_unit=spec.calc_per_unit, detail=ruler[1])
            elif spec.tick_value_source == "tick":
                self._note_meta(_host_key(f"cash_ruler:{account.name}:{spec.name}"), "")
            if per_lot > 0 and volume >= account.risk.max_lots - 1e-9:
                # The lot sits on the operator's ceiling, so the risk the config promises is not
                # the risk being taken. Sizing quietly is how a 0.25% account trades 0.03% for
                # months: the number has to be said out loud, once per symbol per configuration.
                wanted = (snapshot.balance * account.risk.risk_per_trade_pct / 100.0) / per_lot
                if wanted > volume + spec.volume_step:
                    self._announce_once(
                        _host_key(f"lot_cap:{account.name}:{spec.name}:"
                                  f"{account.risk.max_lots}:{account.risk.risk_per_trade_pct}"),
                        account.name, "risk_under_taken", symbol=spec.name,
                        configured_pct=account.risk.risk_per_trade_pct,
                        max_lots=account.risk.max_lots, wanted_lots=round(wanted, 3),
                        taken_lots=volume,
                        effective_pct=round(risk_money / snapshot.balance * 100, 4))
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
            try:
                who = mt5.account_info() if sending else None
                refused = sending and not _identity_matches(who, account)
            except Exception as exc:  # noqa: BLE001 - a gate that cannot answer is a refusal, not a crash
                # This sits between the reservation and the order. Anything that raises here — a
                # terminal object missing a field, a broker call that throws instead of answering —
                # used to unwind the cycle with the slot still owned, and a slot nobody releases holds
                # the portfolio's money cap for its whole 600 s.
                self.store.release(slot)
                self.store.event(account.name, "identity_check_failed", symbol=spec.name,
                                 error=f"{type(exc).__name__}: {exc}"[:200])
                return 0
            if refused:
                # Normalised exactly like attach(): a registry that types `Servername ` must not
                # pass the attach gate and then be refused by this one on every symbol.
                self.store.release(slot)
                self.store.event(account.name, "identity_lost", symbol=spec.name,
                                 detail=f"перед входом отвечает "
                                        f"{getattr(who, 'login', None)}/"
                                        f"{getattr(who, 'server', None)}")
                return 0
            try:
                if not sending:
                    self.store.release(slot)
                    word = "entry(dry)" if self.dry_run else "entry(gated)"
                    self.store.event(account.name, word, symbol=spec.name,
                                     side=signal.side, volume=volume, reason=signal.reason,
                                     risk_usd=round(risk_money * rate, 2))
                    # The two halves of `not sending` are different facts, and the journal says them
                    # with different words: `entry(dry)` is a paper cycle that saw the signal it would
                    # have traded, `entry(gated)` is a real account the operator has not spoken for,
                    # with nothing in the market. They count apart too — the paper cycle's signal is
                    # one lot opened, the refused one is none, and reporting it as opened per cycle is
                    # the status line lying about the book. `opened` is what the conductor, the shard's
                    # JSON and the operator's screen read.
                    return 1 if self.dry_run else 0
                # The last look at the stand, one statement before the money moves. The check at the
                # top of this method is behind a symbol probe, a portfolio read, a reservation and an
                # identity proof; on a slow broker that is tens of seconds in which the operator can
                # arm the file. A switch that only holds when the cycle happened to start armed is a
                # switch that races with the person who reached for it.
                if self._stand_holds(account):
                    self.store.release(slot)
                    return 0
                execution = broker.market_order(
                    account.magic, spec, signal.side, volume, signal.stop, signal.target,
                    signal.stop_distance, risk_money)
            except broker.StopsRefused as exc:
                # The server's own minimum distance, read before the send: nothing was ordered, the
                # reservation is freed, and the plan is left intact instead of being widened into a
                # trade whose risk nobody sized. Once per rule, not once per cycle: the same symbol
                # refuses the same way until the strategy changes, and a journal full of repeats hides
                # every other fact in it.
                self.store.release(slot)
                self._report_change(f"stopsrefused:{account.name}:{spec.name}",
                                    _refusal_shape(str(exc)), account.name,
                                    "entry_stops_refused", symbol=spec.name,
                                    side=signal.side, volume=volume, detail=str(exc)[:300])
                continue
            except broker.BrokerError as exc:
                self.store.release(slot)
                self.store.event(account.name, "entry_failed", symbol=spec.name,
                                 error=str(exc), side=signal.side, volume=volume,
                                 sl=signal.stop, tp=signal.target, requested=signal.price)
                continue
            except Exception:
                # Anything else unwinds the whole cycle. A slot left behind is a money reservation
                # nobody owns, and it holds the portfolio risk cap for its full 600 s.
                self.store.release(slot)
                raise
            # Everything from the fill on is a fact about a lot the broker already owns. None of it
            # may strand the reservation: a slot nobody releases holds the portfolio risk cap for its
            # whole 600 s, and the money is already in the market whatever these writes answer.
            try:
                self.store.trade(account.name, snapshot.login, spec.name, signal.side,
                                 execution.volume, signal.price, volume, execution.retcode,
                                 signal.reason, execution.token)
                self.store.event(account.name, "opened", symbol=spec.name, side=signal.side,
                                 volume=execution.volume, price=execution.price,
                                 requested=signal.price, reason=signal.reason,
                                 risk_usd=round(risk_money * rate, 2),
                                 risk_basis=spec.tick_value_source)
                fresh = broker.open_positions(spec.name, account.magic)
                already = {int(p.ticket) for p in same_symbol}
                try:
                    for position in fresh:
                        ticket = int(position.ticket)
                        self._note_meta(f"atr:{account.name}:{ticket}", f"{signal.atr:.8f}")
                        # The witness is written here, at the fill, not from next cycle's
                        # photograph: on a netting account the operator can fold into this ticket
                        # before the park looks at it again, and a witness seeded from the volume it
                        # then finds is his half carried by our trailing stop.
                        if ticket in already:
                            self.store.add_lot_witness(account.name, ticket, execution.volume)
                        else:
                            self.store.note_lot_witness(account.name, ticket, execution.volume)
                except Exception as exc:  # noqa: BLE001 - the lot is already at the broker
                    # A witness this book cannot write is a lot this book cannot prove: the next
                    # cycle adopts the volume it finds, and the merge guard for that ticket is only
                    # as old as the mirror again. Said out loud rather than left silent.
                    self.store.event(account.name, "lot_witness_failed", symbol=spec.name,
                                     error=f"{type(exc).__name__}: {exc}"[:160])
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
            finally:
                self.store.release(slot)
            # A lot that went through proves this symbol's geometry is no longer the one the server
            # refused: the note is a claim about a rule that has stopped firing. Left set, it silences
            # the next time the same rule bites — possibly weeks later, after the operator widened the
            # stop distance and the broker started saying no again.
            self._note_meta(f"stopsrefused:{account.name}:{spec.name}", "")
            self._mark_entry(account, spec.name)
            # Last, after the reservation is freed: bookkeeping must never be the reason a dollar
            # slot stays held. The lot is already at the broker and journalled as `opened`.
            try:
                candidates.record(self.store, account, candidates.entry_note(
                    signal, execution, spec, risk_money * rate, snapshot.currency, rate))
            except Exception as exc:  # noqa: BLE001 - a note is a note, the money is already out
                # Losing one row of the candidate book must not abort the cycle: the day state,
                # the cooldown and the remaining symbols all sit behind this line.
                self.store.event(account.name, "candidate_note_failed", symbol=spec.name,
                                 error=f"{type(exc).__name__}: {exc}"[:160])
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
        # This runs after the fill, in the middle of the loop over the account's symbols. Letting a
        # journal locked by a peer raise here would abort the cycle and take the candidate note with
        # it, and the lot the broker already owns would be traded again next cycle on the same
        # signal: an unrecorded cooldown is a symbol that can be bought twice in a row.
        if self._note_meta(f"entry:{account.name}:{symbol_name}",
                           dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")):
            return
        self.store.event(account.name, "entry_mark_failed", symbol=symbol_name,
                         detail="кулдаун не записан: этот символ можно купить повторно")

    def _mirror_risk_usd(self, account: Account, positions: list, rate: float) -> dict[int, float]:
        """Price this account's stop risk in dollars, by the broker that holds it.

        A peer shard reading the mirror cannot resolve these symbols: a contract on another broker
        means a different amount of money per point, and its name may not exist here at all.

        Every lot is priced here, the operator's included — his trade moves this account's money and
        therefore belongs in another shard's risk sum — but only by the stop the broker actually
        holds. Nothing in this function writes the `atr:` authorship note: this mirror prices other
        people's risk caps, it does not decide who is driving a lot, and an older version that called
        `_atr_entry` here stamped the operator's tickets with our entry ATR and then reported them as
        park lots nobody was managing. A lot with no stop prices to nothing and stays NULL.
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
            if spec.tick_value_source == "unproven":
                # The same 10x that closes this symbol's entries also corrupts a standing lot's price:
                # `unproven` deliberately keeps the published tick, and a portfolio cap built from a
                # tick the broker contradicted itself can understate the book by that same factor.
                # NULL is the honest answer — a peer that cannot price a lot says it cannot price it.
                continue
            money = risk.position_risk_money(position, spec, 0.0, account.risk.stop_atr)
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
              allow_entries: bool = True,
              ) -> list[WorkerResult]:
    """Run one shard. A pause blocks new entries but never blocks exits.

    `allow_entries=False` is the same half-door for a reason the operator did not choose: a registry
    this process can no longer read stops openings, because an opening is a promise about money made
    in the numbers that file holds. Taking that money back out needs only the address, which the
    cycle proves for itself.
    """
    worker = Worker(config, store, pause_file=pause_file, dry_run=dry_run,
                    allow_live=allow_live)
    stand = worker.pause_file  # the path the Worker itself obeys: one answer, not two defaults
    results = []
    for account in config.by_shard(shard):
        # The pause file can appear mid-cycle: check it per account, not once per shard.
        paused = stand.exists() or not allow_entries
        results.append(worker.run_account(account, allow_entries=not paused))
        time.sleep(0.3)
    return results
