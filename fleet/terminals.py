"""Terminal provisioning and process control.

One portable directory per account: binaries hard-linked from a clean install (so hundreds of
accounts cost megabytes), private config/logs, a startup file that points the terminal at exactly
one account, and no secrets written to disk.

The channel is per terminal, not per machine: measured on a box with three terminals running, each
published its own named pipe (MT5.Terminal.<64 hex>) and MetaTrader5.initialize(path=...) answered
from the terminal whose executable we named, from several processes at once. Loopback TCP 22346 was
bound by exactly one of the three and had nothing to do with which terminal answered. So this module
never stops one terminal to make room for another: it only ever stops the copy it is asked to stop.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess
import time

from .config import Account, server_key

SKIP_DIRS = {"config", "bases", "logs", "profiles", "sounds", "tester", "mql5"}
WRITABLE_DIRS = ("config", "logs", "MQL5", "MQL5/Experts", "MQL5/Scripts", "profiles", "sounds",
                 "Tester")
# Session material a manual login leaves behind; copied from seed_config, cleared before every copy.
_SEED_FILES = ("accounts.dat", "servers.dat", "srvsettings.dat", "settings.ini")
# The same list for a live account, whose saved login is never carried anywhere: see
# `_seed_saved_session`.
_SESSION_FILES_WITHOUT_LOGIN = tuple(name for name in _SEED_FILES if name != "accounts.dat")
# Broker identity, and it is not optional: `Config` is skipped as a directory, so a copy assembled
# from hardlinks arrives with no `terminal.lic` and no server list. Measured 2026-11-05 on the park's
# own GTC copies of a branded install — the window started, reported its broker in the header and
# wrote not one `Network` line to its log: an unlicensed build is the generic MetaQuotes terminal, it
# has no addresses to dial, and it cannot be told to by `initialize(login=...)` either. Copying these
# three files (never `accounts.dat`) is what turns a hardlinked skeleton into a terminal of a broker.
_BRAND_FILES = ("terminal.lic", "servers.dat", "dnsperf.dat")

_PID_CACHE: dict[str, tuple[float, int | None]] = {}
_CACHE_TTL = 15.0


def _ps(script: str, timeout: int = 30) -> str:
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    return result.stdout or ""


# --- provisioning ---------------------------------------------------------------------------

def _hardlink_tree(source: pathlib.Path, target: pathlib.Path) -> None:
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            # MT5 ships Config/Bases/Profiles with capital letters: a case-sensitive skip would
            # hardlink saved sessions and shared databases into every account folder.
            if item.name.casefold() in SKIP_DIRS:
                continue
            destination.mkdir(exist_ok=True)
            _hardlink_tree(item, destination)
            continue
        if destination.exists():
            continue
        try:
            os.link(item, destination)
        except OSError:
            shutil.copy2(item, destination)


def _binary_fingerprint(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def live_pids_in(directory: pathlib.Path) -> list[int]:
    """Pids of terminals running out of exactly this portable directory."""
    expected = str(directory / "terminal64.exe").lower()
    return [pid for pid, path in running_terminals() if path.lower() == expected]


def provision(account: Account, source: pathlib.Path | None = None,
              force: bool = False) -> pathlib.Path:
    """Create (or refresh) the portable directory for one account."""
    base = pathlib.Path(source or account.terminal_source)
    if not base.exists():
        raise FileNotFoundError(f"нет установленного терминала: {base}")
    target = account.data_dir
    binary = base / "terminal64.exe"
    fingerprint_file = target / ".fleet-source.sha"
    current = _binary_fingerprint(binary) if binary.exists() else ""
    stale = (target.exists() and fingerprint_file.exists()
             and fingerprint_file.read_text(encoding="utf-8").strip() != current)
    if target.exists() and (force or stale):
        # A wipe is a decommission, not an upgrade step: the window running from this directory
        # holds live positions and the margin they spend. Rebuilding underneath it leaves a driver
        # the park can no longer attach to, which is the one thing the census exists to notice.
        live = live_pids_in(target)
        if live:
            raise FileExistsError(
                f"из {target} работает терминал (pid {', '.join(str(pid) for pid in live)}): "
                f"сначала пауза и остановка окна этого счёта — fleet_cli.py pause; "
                f"fleet_cli.py stop --account {account.name}")
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    _hardlink_tree(base, target)
    for relative in WRITABLE_DIRS:
        (target / relative).mkdir(parents=True, exist_ok=True)
    _seed_saved_session(account)
    _brand_copy(account)  # after the seed: the install this copy is built from owns the identity
    write_startup_config(account)  # always ours: never a hardlinked copy of another account
    if current:
        fingerprint_file.write_text(current, encoding="utf-8")
    return target


def _brand_copy(account: Account) -> None:
    """Copy the broker's licence and address list into the copy's own `Config`.

    These are our bytes, never links: `Config` is a directory the hardlink walk skips, so linking one
    account's server list would make it the same inode as the install's, and the `terminal.ini`-style
    files MT5 rewrites in place would then rewrite the operator's install through it.
    """
    source = pathlib.Path(account.terminal_source) / "Config"
    target = account.data_dir / "config"
    target.mkdir(parents=True, exist_ok=True)
    for name in _BRAND_FILES:
        candidate = source / name
        if not candidate.is_file():
            continue
        destination = target / name
        destination.unlink(missing_ok=True)
        shutil.copy2(candidate, destination)


def seed_status(account: Account) -> tuple[bool, str]:
    """Whether this account has a saved session to start from, and why not when it has none."""
    if not account.seed_config:
        return False, "seed_config не задан: терминал не сможет авторизоваться сам"
    seed_dir = pathlib.Path(account.seed_config)
    if not seed_dir.exists():
        return False, f"нет папки seed_config: {seed_dir}"
    if not (seed_dir / "accounts.dat").exists():
        return False, f"в {seed_dir} нет accounts.dat с сохранённой сессией"
    return True, ""


def _detach(path: pathlib.Path) -> None:
    """Give this name its own bytes before anything writes through it.

    A portable folder is assembled from hardlinks, and the operator's own install can still be one
    of the names on that inode: measured on this machine, `Config/common.ini`, `Config/terminal.ini`
    and six more files under `C:\\mt5-fleet\\terminals\\114929502-metaquotes-demo` each carry three
    links, one of them in `C:\\Program Files\\MetaTrader 5\\Config`. `open(path, "w")` truncates the
    inode and not the name, so provisioning would rewrite the settings of the terminal the operator
    is trading in — his server list, his window layout, his login defaults.
    """
    try:
        if os.stat(path).st_nlink <= 1:
            return
        data = path.read_bytes()
    except OSError:
        return
    path.unlink(missing_ok=True)
    path.write_bytes(data)


def _seed_saved_session(account: Account) -> None:
    """Copy a session saved by an earlier manual login (accounts/servers data).

    The broker's accounts.dat holds every login that terminal has saved, not only this one, and it
    is binary: the park cannot filter it. So the session files are cleared immediately before a
    replacement is copied — a portable copy must never carry a stranger's saved logins into a new
    broker's folder.

    The clear is therefore gated on the replacement actually existing. A registry entry with no
    `seed_config`, or one pointing at a folder with no accounts.dat, would otherwise delete the
    working login this portable directory already has and copy nothing back: the terminal then
    starts at a password prompt the park cannot answer, and every entry of that account is gone for
    the day. `seed_status` says the same thing to the operator before a launch.

    A REAL account is never handed another terminal's saved login. Measured 2026-11-05: a copy of the
    park's `gtc-real` entry, seeded from the operator's own branded install, started, dialled
    `GTCGlobalSA-Server 2` and authorised as 52183174 with the password stored in that file — real
    money on a live server, with the registry naming a different login and no order ever having asked
    the park's risk gates. A live account's session is not provisioning material: it is something this
    account earns by logging in with its own `password_env`, which measured to write the session into
    this copy's own `Config` — and the window restarted against that is the one state MT5 leaves
    ALGO TRADING up in. So for a real account `accounts.dat` is left exactly as it is found, including
    when it is not found at all.
    """
    config_dir = account.data_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    if not account.seed_config:
        return
    seed_dir = pathlib.Path(account.seed_config)
    if not seed_dir.exists():
        return
    if account.is_real:
        for pattern in _SESSION_FILES_WITHOUT_LOGIN:
            candidate = seed_dir / pattern
            if candidate.exists():
                shutil.copy2(candidate, config_dir / pattern)
        return
    if not (seed_dir / "accounts.dat").exists():
        return
    for pattern in _SEED_FILES:
        (config_dir / pattern).unlink(missing_ok=True)  # common.ini is ours; these are sessions
    for pattern in _SEED_FILES:
        candidate = seed_dir / pattern
        if candidate.exists():
            shutil.copy2(candidate, config_dir / pattern)


_KEEP_ON_REWRITE = (("Environment", "Common"), ("PushID", "Notification"))
# Values the terminal derived for this machine and this session; a rewrite that drops them makes the
# terminal re-derive them at the next login, and there is nothing the park gains by making it.
_VALUE_STOPS = ("[", "\r\n", "\n", "\r", "Login=", "Server=", "Enabled=", "ProxyEnable=")
# Where a carried value ends. The file this reads can be the one that started the repair — keys run
# together on a single line — so a value is cut at the first key that cannot belong to it.


def _kept_ini_values(target: pathlib.Path) -> dict[str, str]:
    """The values from the file already on disk that the new file has to carry over.

    Read as UTF-16 with the line breaks allowed to be anywhere, because this runs on files MT5 wrote
    itself and on files a previous writer left with the keys glued into a single line — the shape that
    started this repair.
    """
    try:
        body = target.read_bytes().decode("utf-16-le", errors="replace").lstrip("﻿")
    except OSError:
        return {}
    kept: dict[str, str] = {}
    for key, _section in _KEEP_ON_REWRITE:
        at = body.find(f"{key}=")
        if at < 0:
            continue
        value = body[at + len(key) + 1:]
        cuts = [value.find(stop) for stop in _VALUE_STOPS if value.find(stop) >= 0]
        value = value[:min(cuts)] if cuts else value
        if value.strip():
            kept[key] = value.rstrip()
    return kept


def write_startup_config(account: Account) -> None:
    """Start-up file for exactly one account, in the shape MetaTrader 5 reads it.

    Secrets never go to disk: the worker logs in.

    Measured 2026-11-05 on the park's own demo copy. Its `Config/common.ini` was UTF-16 in which the
    first region had no line breaks at all — `...PreloadCharts=1[Experts]AllowDllImport=0Enabled=1
    Account=1...` — while `c:\\mt5-exness`, the operator's window that answers
    `terminal_info().trade_allowed = True`, is one CRLF-separated region with `[Experts]` and
    `Enabled=1` each on its own line. MT5's ini reader takes one `Key=Value` per line, so a section
    whose keys are glued is a section that declares nothing: that window came up connected and
    authorized, with `Enabled=1` present in the bytes and ALGO TRADING off, and every entry died on
    `retcode 10027` while the journal read `opened=0` cycle after cycle. Rewriting those same keys as
    UTF-16 LE with a BOM and CRLF — same directory, same login, nothing else touched — made the window
    answer `trade_allowed = True`, and its log said `'114929502': trading has been enabled, demo
    account - hedging mode`; the next cycle opened its first lot. MT5 then kept that file and extended
    `[Charts]` around it rather than re-gluing it.
    """
    config_dir = account.data_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "common.ini"
    _detach(target)  # this name may still be the operator's own install file
    kept = _kept_ini_values(target)
    sections = {
        "Common": [f"Login={account.login}", f"Server={account.server}", "ProxyEnable=0",
                   "NewsEnable=1", "CertInstall=0"],
        "Experts": ["Enabled=1", f"AllowLiveTrading={1 if account.is_real else 0}",
                    "AllowDllImport=0"],
        "Charts": ["MaxBars=20000"],
        "Notification": [],
        "StartUp": ["Symbol=EURUSD", "Period=H1"],
    }
    for key, section in _KEEP_ON_REWRITE:
        if key in kept:
            sections.setdefault(section, []).insert(0, f"{key}={kept[key]}")
    lines: list[str] = []
    for section, assignments in sections.items():
        if not assignments:
            continue
        lines += [f"[{section}]", *assignments, ""]
    target.write_bytes(b"\xff\xfe" + "\r\n".join(lines).encode("utf-16-le"))


# --- process control ------------------------------------------------------------------------

def launch(account: Account, extra_args: tuple[str, ...] = ()) -> subprocess.Popen:
    if not account.terminal_exe.exists():
        raise FileNotFoundError(f"терминал не подготовлен: {account.terminal_exe}")
    # MT5 re-serializes its own `Config/common.ini` when a window closes — measured 2026-11-05: after
    # the repair the file it left was 1064 bytes with `[Charts]` extended around the park's keys. What
    # a shutdown can also leave behind is the glued shape that switched ALGO TRADING off, so the park
    # re-asserts the switch at every start instead of trusting the last exit. The window is down here
    # by definition, and this is the only moment its start-up file is read.
    write_startup_config(account)
    _PID_CACHE.pop(str(account.data_dir).lower(), None)
    creation = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    return subprocess.Popen([str(account.terminal_exe), "/portable", *extra_args],
                            cwd=str(account.data_dir), creationflags=creation, close_fds=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def running_terminals() -> list[tuple[int, str]]:
    """Every terminal64.exe window up on this machine, with the directory it runs from.

    Read-only, and the only honest census there is: a terminal is who it is by its own path, and
    that is also the list the roster probe walks.
    """
    output = _ps("Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" | "
                 "ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }")
    rows: list[tuple[int, str]] = []
    for line in output.splitlines():
        if "|" not in line:
            continue
        pid_text, path = line.split("|", 1)
        if pid_text.strip().isdigit():
            rows.append((int(pid_text.strip()), path.strip()))
    return rows


def pids_alive(pids: list[int]) -> set[int]:
    """Which of these process ids are still up — one query, never one per window.

    The census asks a window who it is with `initialize(path=...)`, and initialize starts a terminal
    that is not running. A window that died between the process walk and the probe would therefore
    be opened by the census, with the saved session it was seeded with: so the probe only ever
    addresses a pid that is still alive at the moment of the probe.
    """
    if not pids:
        return set()
    wanted = {int(pid) for pid in pids}
    script = ("Get-Process -Id " + ",".join(str(pid) for pid in sorted(wanted)) +
              " -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }")
    return {int(line) for line in _ps(script).splitlines() if line.strip().isdigit()}


def running_pid(account: Account) -> int | None:
    key = str(account.data_dir).lower()
    cached = _PID_CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    expected = str(account.terminal_exe).lower()
    pid = next((candidate for candidate, path in running_terminals()
                if path.lower() == expected), None)
    if pid is not None:
        _PID_CACHE[key] = (now, pid)
    else:
        _PID_CACHE.pop(key, None)
    return pid


def stop(account: Account) -> int | None:
    """Stop this account's own terminal copy. Never any other terminal.

    The pid is looked up fresh: MT5 re-spawns itself after an update or a restart, and a cached pid
    would then kill a process that is already gone and leave the live window running.
    """
    expected = str(account.terminal_exe).lower()
    pid = next((candidate for candidate, path in running_terminals()
                if path.lower() == expected), None)
    _PID_CACHE.pop(str(account.data_dir).lower(), None)
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    return pid


# --- logs and authorization -----------------------------------------------------------------

def last_log_lines(account: Account, count: int = 40) -> list[str]:
    """Tail of the newest terminal log, decoding UTF-16 (MT5) or UTF-8 by evidence."""
    log_dir = account.data_dir / "logs"
    if not log_dir.exists():
        return []
    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return []
    path = logs[0]
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - 128 * 1024))
            raw = handle.read()
    except OSError:
        return []
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    elif b"\x00" in raw[:400]:
        text = raw.decode("utf-16-le", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    return text.splitlines()[-count:]


def authorized(account: Account) -> bool:
    """Whether this terminal's own log says the account's server authorised the login.

    The registry is hand-edited and the log line is the broker's spelling, so both go through the
    same normalisation as every other identity comparison: a registry that types `Servername ` or
    `servername` must not leave the park waiting on a login that already happened.
    """
    marker = f"authorized on {server_key(account.server)}"
    return any(marker in server_key(line) for line in last_log_lines(account, 200))


def wait_authorized(account: Account, timeout: float = 30.0, poll: float = 1.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if authorized(account):
            return True
        time.sleep(poll)
    return False


def password_for(account: Account) -> str | None:
    if not account.password_env:
        return None
    return os.environ.get(account.password_env) or None
