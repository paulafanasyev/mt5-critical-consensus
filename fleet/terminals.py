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

from .config import Account

SKIP_DIRS = {"config", "bases", "logs", "profiles", "sounds", "tester", "mql5"}
WRITABLE_DIRS = ("config", "logs", "MQL5", "MQL5/Experts", "MQL5/Scripts", "profiles", "sounds",
                 "Tester")
# Session material a manual login leaves behind; copied from seed_config, cleared before every copy.
_SEED_FILES = ("accounts.dat", "servers.dat", "srvsettings.dat", "settings.ini")

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
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    _hardlink_tree(base, target)
    for relative in WRITABLE_DIRS:
        (target / relative).mkdir(parents=True, exist_ok=True)
    _seed_saved_session(account)
    write_startup_config(account)  # always ours: never a hardlinked copy of another account
    if current:
        fingerprint_file.write_text(current, encoding="utf-8")
    return target


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


def _seed_saved_session(account: Account) -> None:
    """Copy a session saved by an earlier manual login (accounts/servers data).

    The broker's accounts.dat holds every login that terminal has saved, not only this one, and it
    is binary: the park cannot filter it. So a previous seed's session files are always cleared
    first — a portable copy must never carry a stranger's saved logins into a new broker's folder.
    """
    config_dir = account.data_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for pattern in _SEED_FILES:
        (config_dir / pattern).unlink(missing_ok=True)  # common.ini is ours; these are sessions
    if not account.seed_config:
        return
    seed_dir = pathlib.Path(account.seed_config)
    if not seed_dir.exists():
        return
    for pattern in _SEED_FILES:
        candidate = seed_dir / pattern
        if candidate.exists():
            shutil.copy2(candidate, config_dir / pattern)


def write_startup_config(account: Account) -> None:
    """Start-up file for exactly one account. Secrets never go to disk: the worker logs in."""
    config_dir = account.data_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Common]",
        f"Login={account.login}",
        f"Server={account.server}",
        "ProxyEnable=0",
        "NewsEnable=1",
        "CertInstall=0",
        "",
        "[Charts]",
        "MaxBars=20000",
        "",
        "[Experts]",
        "Enabled=1",
        f"AllowLiveTrading={1 if account.is_real else 0}",
        "AllowDllImport=0",
        "",
        "[StartUp]",
        "Symbol=EURUSD",
        "Period=H1",
        "",
    ]
    with (config_dir / "common.ini").open("w", encoding="utf-8", newline="\r\n") as handle:
        handle.write("\n".join(lines))


# --- process control ------------------------------------------------------------------------

def launch(account: Account, extra_args: tuple[str, ...] = ()) -> subprocess.Popen:
    if not account.terminal_exe.exists():
        raise FileNotFoundError(f"терминал не подготовлен: {account.terminal_exe}")
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


def stop(account: Account) -> None:
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
    marker = f"authorized on {account.server}"
    return any(marker in line for line in last_log_lines(account, 200))


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
