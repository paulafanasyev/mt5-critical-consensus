"""Terminal provisioning and channel handoff.

One portable directory per account: binaries hard-linked from a clean install (so hundreds of
accounts cost megabytes), private config/logs, a startup file that points the terminal at exactly
one account, and no secrets written to disk. Because MT5 serves the Python API on a single port per
Windows session, the fleet stops every other fleet terminal before it hands the channel over.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess
import time

from .config import FLEET_ROOT, Account

SKIP_DIRS = {"config", "logs", "MQL5", "profiles", "sounds", "Tester", "bases"}
WRITABLE_DIRS = ("config", "logs", "MQL5", "MQL5/Experts", "MQL5/Scripts", "profiles", "sounds",
                 "Tester")

_PID_CACHE: dict[str, tuple[float, int | None]] = {}
_CACHE_TTL = 15.0


def _ps(script: str) -> str:
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True)
    return result.stdout or ""


# --- provisioning ---------------------------------------------------------------------------

def _hardlink_tree(source: pathlib.Path, target: pathlib.Path) -> None:
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            if item.name in SKIP_DIRS:
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
    if not (target / "config" / "common.ini").exists():
        write_startup_config(account)
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
    """Copy a session saved by an earlier manual login (accounts/servers data)."""
    if not account.seed_config:
        return
    seed_dir = pathlib.Path(account.seed_config)
    if not seed_dir.exists():
        return
    config_dir = account.data_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("accounts.dat", "servers.dat", "srvsettings.dat", "settings.ini"):
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
    _invalidate_port_cache()
    creation = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    return subprocess.Popen([str(account.terminal_exe), "/portable", *extra_args],
                            cwd=str(account.data_dir), creationflags=creation, close_fds=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _terminal_processes() -> list[tuple[int, str]]:
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


def running_pid(account: Account) -> int | None:
    key = str(account.data_dir).lower()
    cached = _PID_CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    expected = str(account.terminal_exe).lower()
    pid = next((candidate for candidate, path in _terminal_processes()
                if path.lower() == expected), None)
    if pid is not None:
        _PID_CACHE[key] = (now, pid)
    else:
        _PID_CACHE.pop(key, None)
    return pid


def stop(account: Account) -> None:
    pid = running_pid(account)
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        _PID_CACHE[str(account.data_dir).lower()] = (time.time(), None)
        _invalidate_port_cache()


def _invalidate_port_cache() -> None:
    """A killed terminal must not leave a stale port owner behind for the next account."""
    try:
        from . import worker

        worker._PORT_OWNER_CACHE["ts"] = 0.0
        worker._PORT_OWNER_CACHE["pid"] = 0
    except Exception:  # noqa: BLE001 - cache invalidation is best effort
        pass


def stop_all() -> None:
    """Kill every MT5 terminal: the API port must be free before another terminal starts."""
    subprocess.run(["taskkill", "/IM", "terminal64.exe", "/F"], capture_output=True)
    _PID_CACHE.clear()


def stop_other_fleet(keep_dir: pathlib.Path | None = None) -> int:
    """Stop fleet terminals except the one whose exact executable path is ``keep_dir``."""
    keep_exe = (str(pathlib.Path(keep_dir) / "terminal64.exe").lower()
                if keep_dir is not None else "")
    stopped = 0
    for pid, path in _terminal_processes():
        normalized = path.lower()
        if str(FLEET_ROOT).lower() not in normalized:
            continue
        if keep_exe and normalized == keep_exe:
            continue
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        stopped += 1
    if stopped:
        _PID_CACHE.clear()
        _invalidate_port_cache()
    return stopped


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
