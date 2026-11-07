"""Try VPN Gate relays until the Exness terminal actually authorizes.

The tunnel is up, but the exchange still refuses to sync its symbol list. This script walks
through candidate relays (different countries and protocols), restarts the tunnel and the
Exness terminal for each one, and stops at the first that produces an authorization line in
the terminal log.
"""
from __future__ import annotations

import base64
import csv
import io
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

API = "https://www.vpngate.net/api/iphone/"
OUT = Path(r"C:\openvpn")
CONFIG = OUT / "vgate.ovpn"
LOG = OUT / "openvpn.log"
OPENVPN = Path(r"C:\Program Files\OpenVPN\bin\openvpn.exe")
EXNESS = Path(r"C:\Program Files\MetaTrader 5 - EXNESS\terminal64.exe")
TERMINAL_LOG = Path(r"C:\Program Files\MetaTrader 5 - EXNESS\logs")
COUNTRIES = ("Japan", "Korea", "Singapore", "Thailand", "Hong Kong", "Taiwan", "Vietnam")


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def servers() -> list[dict]:
    request = urllib.request.Request(API, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(request, timeout=60) as response:
        text = response.read().decode("utf-8", errors="replace")
    rows = [line for line in text.splitlines()
            if line and not line.startswith("*") and not line.startswith("#")]
    out = []
    for row in csv.reader(rows):
        if len(row) < 15 or not row[14]:
            continue
        try:
            out.append({"ip": row[1], "ping": int(row[3]), "country": row[5],
                        "config": row[14]})
        except ValueError:
            continue
    out.sort(key=lambda s: (s["country"] not in COUNTRIES, s["ping"]))
    return out


def stop_openvpn() -> None:
    for line in run(["tasklist", "/FI", "IMAGENAME eq openvpn.exe"]).stdout.splitlines():
        if "openvpn.exe" in line:
            run(["taskkill", "/PID", line.split()[1], "/F"])


def stop_terminal() -> None:
    for proc in subprocess.run(["wmic", "process", "where",
                                "name='terminal64.exe'", "get", "processid,commandline"],
                               capture_output=True, text=True).stdout.splitlines():
        if "EXNESS" in proc:
            parts = proc.split()
            if parts and parts[-1].isdigit():
                run(["taskkill", "/PID", parts[-1], "/F"])


def tunnel(server: dict) -> bool:
    OUT.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(base64.b64decode(server["config"]).decode("utf-8", errors="replace"),
                      encoding="utf-8")
    if LOG.exists():
        LOG.unlink()
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen([str(OPENVPN), "--config", str(CONFIG), "--log", str(LOG), "--verb", "3"],
                     creationflags=flags, close_fds=True)
    for _ in range(25):
        time.sleep(3)
        if LOG.exists() and "Initialization Sequence Completed" in LOG.read_text(
                encoding="utf-8", errors="replace"):
            return True
    return False


def start_terminal() -> None:
    subprocess.Popen([str(EXNESS), "/login:276261159", "/server:Exness-MT5Real26"],
                     cwd=str(EXNESS.parent), close_fds=True)


def authorized() -> bool:
    newest = sorted(TERMINAL_LOG.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not newest:
        return False
    text = newest[0].read_text(encoding="utf-8", errors="replace")
    return "authorized on Exness-MT5Real26" in text


def main() -> int:
    candidates = servers()
    print(f"кандидатов: {len(candidates)}")
    tried = 0
    for server in candidates:
        tried += 1
        print(f"[{tried}] {server['country']} {server['ip']} ping {server['ping']}", flush=True)
        stop_openvpn()
        stop_terminal()
        time.sleep(3)
        if not tunnel(server):
            print("   туннель не поднялся", flush=True)
            continue
        start_terminal()
        for _ in range(20):
            time.sleep(4)
            if authorized():
                print(f"ГОТОВО: {server['country']} {server['ip']} — Exness авторизован", flush=True)
                return 0
        print("   авторизации нет, следующий", flush=True)
        if tried >= 6:
            break
    print("ни один релей не дал авторизации", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
