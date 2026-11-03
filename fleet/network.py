"""Network: per-account VPN tunnels for brokers this network blocks.

A blocked broker is defeated only by a system tunnel: MT5 opens raw TCP sockets to its trade
servers and ignores the HTTP proxy setting for them. Each account names its own profile, the
session log is truncated per attempt (an old success must never look like a live tunnel), and the
tunnel is torn down when no account needs it any more.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TUNNEL_SCRIPT = REPO_ROOT / "scripts" / "vpn_gate_tunnel.py"
TUNNEL_DIR = pathlib.Path(r"C:\openvpn")
OPENVPN = pathlib.Path(r"C:\Program Files\OpenVPN\bin\openvpn.exe")


def session_log(profile: str) -> pathlib.Path:
    name = pathlib.Path(profile).stem or "default"
    return TUNNEL_DIR / f"openvpn-{name}.log"


def openvpn_pids() -> list[int]:
    script = ("Get-Process openvpn -ErrorAction SilentlyContinue | "
              "Select-Object -ExpandProperty Id")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True)
    return [int(part) for part in result.stdout.split() if part.isdigit()]


def tunnel_up(profile: str) -> bool:
    """A tunnel is up only if this profile's own session log ends with a successful start."""
    if not openvpn_pids():
        return False
    log = session_log(profile)
    if not log.exists():
        return False
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    tail = text.rsplit("Initialization Sequence Completed", 1)
    if len(tail) < 2:
        return False
    # Anything after the last successful init that says "exiting" means it already died.
    return "SIGTERM" not in tail[1][-2000:] and "ERROR: " not in tail[1][-2000:]


def ensure_tunnel(profile: str, timeout: float = 60.0) -> tuple[bool, str]:
    """Bring up this account's profile; returns (ok, detail)."""
    if not profile:
        return False, "network: vpn без vpn_profile"
    if tunnel_up(profile):
        return True, "туннель уже поднят"
    log = session_log(profile)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("", encoding="utf-8")  # a new session must not inherit an old success line
    config = pathlib.Path(profile)
    if config.exists() and OPENVPN.exists():
        creation = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        subprocess.Popen([str(OPENVPN), "--config", str(config), "--log", str(log), "--verb", "3"],
                         creationflags=creation, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif TUNNEL_SCRIPT.exists():
        import os

        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        subprocess.run([sys.executable, "-X", "utf8", str(TUNNEL_SCRIPT)], cwd=str(REPO_ROOT),
                       capture_output=True, text=True, timeout=timeout, env=env)
    else:
        return False, f"нет ни профиля {config}, ни скрипта {TUNNEL_SCRIPT}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tunnel_up(profile):
            return True, "туннель поднят"
        time.sleep(1.5)
    return False, f"туннель не поднялся за {int(timeout)} с (лог {log})"


def stop_tunnel() -> None:
    subprocess.run(["taskkill", "/IM", "openvpn.exe", "/F"], capture_output=True)
