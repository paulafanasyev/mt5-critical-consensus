"""Pick a VPN Gate relay, write its OpenVPN config, and start the tunnel.

The operator's real account lives at Exness, whose servers this network blocks at the TCP
level. A system-level tunnel — not the HTTP proxy — is the only thing that carries MT5's trade
sockets, so this script downloads the public relay list, chooses the lowest-latency Asian
server, decodes its OpenVPN profile and launches openvpn.exe in the background.
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
OUT_DIR = Path(r"C:\openvpn")
LOG = OUT_DIR / "openvpn.log"
CONFIG = OUT_DIR / "vgate.ovpn"
OPENVPN = Path(r"C:\Program Files\OpenVPN\bin\openvpn.exe")
PREFERRED = ("Japan", "Korea", "Singapore", "Thailand", "Taiwan", "Hong Kong", "Vietnam")


def fetch_servers() -> list[dict]:
    request = urllib.request.Request(API, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(request, timeout=60) as response:
        text = response.read().decode("utf-8", errors="replace")
    rows = [line for line in text.splitlines()
            if line and not line.startswith("*") and not line.startswith("#")]
    servers = []
    for row in csv.reader(rows):
        if len(row) < 15:
            continue
        try:
            servers.append({
                "host": row[0], "ip": row[1], "score": int(row[2]), "ping": int(row[3]),
                "speed": int(row[4]), "country": row[5],
                "config": row[14],
            })
        except ValueError:
            continue
    return servers


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    servers = fetch_servers()
    print(f"серверов в списке: {len(servers)}")
    for server in servers:
        if server["country"] in PREFERRED and server["config"]:
            break
    else:
        server = next(s for s in servers if s["config"])
    servers.sort(key=lambda s: (s["country"] not in PREFERRED, s["ping"]))
    server = next(s for s in servers if s["config"])
    print(f"выбран: {server['host']} {server['ip']} {server['country']} ping {server['ping']} ms")
    try:
        config = base64.b64decode(server["config"]).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        print("не удалось декодировать конфиг:", exc)
        return 1
    CONFIG.write_text(config, encoding="utf-8")
    print(f"конфиг записан: {CONFIG} ({len(config)} символов)")

    for proc in subprocess.run(["tasklist", "/FI", "IMAGENAME eq openvpn.exe"],
                               capture_output=True, text=True).stdout.splitlines():
        if "openvpn.exe" in proc:
            pid = proc.split()[1]
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
            print(f"остановлен прошлый openvpn pid {pid}")

    creation = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    handle = subprocess.Popen(
        [str(OPENVPN), "--config", str(CONFIG), "--log", str(LOG), "--verb", "3"],
        creationflags=creation, close_fds=True,
    )
    print(f"openvpn запущен, pid {handle.pid}, ждём соединения...")
    for _ in range(30):
        time.sleep(3)
        if LOG.exists():
            text = LOG.read_text(encoding="utf-8", errors="replace")
            if "Initialization Sequence Completed" in text:
                print("ТУННЕЛЬ ПОДНЯТ")
                return 0
            if "AUTH_FAILED" in text or "Cannot open TUN" in text:
                print("ошибка туннеля:")
                print("\n".join(text.splitlines()[-8:]))
                return 1
    print("не дождались соединения, последние строки лога:")
    if LOG.exists():
        print("\n".join(LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
