"""Terminal provisioning: one disposable MT5 copy per account, launched and verified in code.

A fleet of hundreds of accounts cannot be driven by clicking in MT5. Each account gets its own
portable directory: binaries are hard-linked from a clean installation (so hundreds of accounts
cost seconds and megabytes, not gigabytes), while config, logs and MQL5 stay private. The
terminal is then started, waited for, verified and killed — all from here.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import time
from typing import Iterable

from .config import FLEET_ROOT, Account

SKIP_DIRS = {"config", "logs", "MQL5", "profiles", "sounds", "Tester", "bases", "MQL5"}
WRITABLE_DIRS = ("config", "logs", "MQL5", "MQL5/Experts", "MQL5/Scripts", "profiles", "sounds", "Tester")


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


def provision(account: Account, source: pathlib.Path | None = None, force: bool = False) -> pathlib.Path:
    """Create (or refresh) the portable directory for one account."""
    base = pathlib.Path(source or account.terminal_source)
    if not base.exists():
        raise FileNotFoundError(f"нет установленного терминала: {base}")
    target = account.data_dir
    if target.exists() and force:
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    _hardlink_tree(base, target)
    for relative in WRITABLE_DIRS:
        (target / relative).mkdir(parents=True, exist_ok=True)
    _seed_saved_session(account)
    if not (target / "config" / "common.ini").exists():
        write_startup_config(account)
    return target


def _seed_saved_session(account: Account) -> None:
    """Copy a broker session saved by an earlier manual login.

    MT5 keeps the account list and server addresses in encrypted data files. Copying them lets a
    provisioned terminal start already authorized, which is the difference between a fleet that
    works unattended and one that needs a human per account.
    """
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


def write_startup_config(account: Account, password: str | None = None) -> None:
    """Point the terminal at exactly one account.

    MT5 also accepts a Password key in common.ini for unattended start-up; when the fleet has
    one from the environment it is written here so the terminal authorizes without any GUI.
    """
    config_dir = account.data_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    common = config_dir / "common.ini"
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
        "SaveDeleted=0",
        "",
        "[Experts]",
        "Enabled=1",
        "AllowLiveTrading=1",
        "AllowDllImport=0",
        "Account=0",
        "Profile=0",
        "",
        "[StartUp]",
        "Symbol=EURUSD",
        "Period=H1",
        "",
    ]
    if password:
        lines.insert(3, f"Password={password}")
    common.write_text("\r\n".join(lines), encoding="utf-8")
    (config_dir / "portable.txt").write_text("", encoding="utf-8")


def launch(account: Account, extra_args: Iterable[str] = ()) -> subprocess.Popen:
    """Start the account's terminal detached in portable mode."""
    if not account.terminal_exe.exists():
        raise FileNotFoundError(f"терминал не подготовлен: {account.terminal_exe}")
    creation = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    return subprocess.Popen(
        [str(account.terminal_exe), "/portable", *extra_args],
        cwd=str(account.data_dir),
        creationflags=creation,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def running_pid(account: Account) -> int | None:
    """Return the pid whose command line belongs to this account's directory."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" | "
        "Where-Object { $_.CommandLine -like '*" + str(account.data_dir) + "*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True)
    text = result.stdout.strip().splitlines()
    return int(text[0]) if text and text[0].strip().isdigit() else None


def stop(account: Account) -> None:
    pid = running_pid(account)
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


def stop_all() -> None:
    """Kill every MT5 terminal on the machine (use when the API port must be free)."""
    subprocess.run(["taskkill", "/IM", "terminal64.exe", "/F"], capture_output=True)


def stop_other_fleet(keep_dir: pathlib.Path | None = None) -> int:
    """Stop fleet terminals except the one about to be used.

    MT5 binds its Python API port (22346) once per Windows session, so two terminals cannot both
    answer the API. The fleet therefore hands the channel from account to account by killing the
    previous terminal first.
    """
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True)
    stopped = 0
    root = str(FLEET_ROOT).lower()
    keep = str(keep_dir).lower() if keep_dir else ""
    for line in result.stdout.splitlines():
        if "|" not in line:
            continue
        pid_text, command = line.split("|", 1)
        if not pid_text.strip().isdigit():
            continue
        command_lower = command.lower()
        if root not in command_lower:
            continue
        if keep and keep in command_lower:
            continue
        subprocess.run(["taskkill", "/PID", pid_text.strip(), "/F"], capture_output=True)
        stopped += 1
    return stopped


def last_log_lines(account: Account, count: int = 40) -> list[str]:
    """Read the newest terminal log, coping with MT5's UTF-16 and UTF-8 log variants."""
    log_dir = account.data_dir / "logs"
    if not log_dir.exists():
        return []
    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return []
    try:
        raw = logs[0].read_bytes()
    except OSError:
        return []
    for encoding in ("utf-8", "utf-16-le", "utf-16"):
        try:
            text = raw.decode(encoding)
            if "\x00" in text[:200]:
                continue
            return text.splitlines()[-count:]
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").splitlines()[-count:]


def authorized(account: Account) -> bool:
    marker = f"authorized on {account.server}"
    return any(marker in line for line in last_log_lines(account, 200))


def wait_authorized(account: Account, timeout: float = 90.0, poll: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if authorized(account):
            return True
        time.sleep(poll)
    return False


def password_for(account: Account) -> str | None:
    """Secrets stay out of the registry: they are read from the environment."""
    if not account.password_env:
        return None
    return os.environ.get(account.password_env) or None


def ensure(account: Account, timeout: float = 90.0) -> bool:
    """Provision, launch and wait until the account is authorized."""
    provision(account, force=False)
    if running_pid(account) is None:
        launch(account)
    return wait_authorized(account, timeout)
