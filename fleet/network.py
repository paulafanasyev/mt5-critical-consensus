"""Network: bring up the system tunnel for brokers that this network blocks.

Exness-style blocks are defeated only by a system-level tunnel, not by an HTTP proxy: MT5 opens
raw TCP sockets to its trade servers and ignores the proxy setting for them. Accounts marked
``network: vpn`` therefore require a live tunnel before their terminal may start.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TUNNEL_SCRIPT = REPO_ROOT / "scripts" / "vpn_gate_tunnel.py"
OPENVPN_LOG = pathlib.Path(r"C:\openvpn\openvpn.log")


def openvpn_running() -> bool:
    script = "Get-Process openvpn -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Id"
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True)
    return result.stdout.strip().isdigit()


def tunnel_up() -> bool:
    """A tunnel is up when openvpn runs and wrote its success line."""
    if not openvpn_running():
        return False
    try:
        text = OPENVPN_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "Initialization Sequence Completed" in text


def ensure_tunnel(timeout: float = 120.0) -> tuple[bool, str]:
    """Start the tunnel if needed; returns (ok, detail)."""
    if tunnel_up():
        return True, "туннель уже поднят"
    if not TUNNEL_SCRIPT.exists():
        return False, f"нет скрипта туннеля: {TUNNEL_SCRIPT}"
    python = sys.executable
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    subprocess.run([python, "-X", "utf8", str(TUNNEL_SCRIPT)], cwd=str(REPO_ROOT),
                   capture_output=True, text=True, timeout=timeout, env={**env})
    deadline = time.time() + 30
    while time.time() < deadline:
        if tunnel_up():
            return True, "туннель поднят"
        time.sleep(2)
    return False, "туннель не поднялся за 30 с"


def stop_tunnel() -> None:
    subprocess.run(["taskkill", "/IM", "openvpn.exe", "/F"], capture_output=True)
