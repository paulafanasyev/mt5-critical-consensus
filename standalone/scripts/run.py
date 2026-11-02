"""One-command launcher: first run configures Telegram interactively, then starts
the live demo on real market data with demo money. Designed to be started via
start.bat (double-click) or `python scripts/run.py`.

The operator only: pastes the bot token once, sends /start to the bot once,
chooses the work mode. Everything else (deps, chat_id, config, launch) is automatic.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from critical_consensus.telegram_approvals import (  # noqa: E402
    TelegramApprovalError,
    detect_chat_id,
    validate_token,
)

CONFIG_PATH = ROOT / "telegram.local.json"  # contains the bot token — must never be committed


def ensure_dependencies() -> None:
    try:
        import critical_consensus  # noqa: F401
        import httpx  # noqa: F401
        return
    except ImportError:
        pass
    print("Первый запуск: устанавливаю зависимости / First run: installing dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-e", str(ROOT), "-q"])
    subprocess.check_call([sys.executable, "-m", "pip", "install", "httpx", "-q"])
    print("Готово / Done.")


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def configure_telegram(config: dict) -> dict | None:
    """First run: ask for the token, validate it, auto-detect chat_id. Returns channel env."""
    if config.get("telegram_token") and config.get("telegram_chat_id"):
        return {"TELEGRAM_BOT_TOKEN": config["telegram_token"], "TELEGRAM_CHAT_ID": config["telegram_chat_id"]}

    print("\n" + "=" * 72)
    print("ШАГ 1 / STEP 1 — Telegram")
    print("=" * 72)
    print("Чтобы получать подтверждения сделок на телефон:")
    print("  1. В Telegram открой @BotFather → /newbot → придумай имя и username")
    print("     / To create a bot: open @BotFather → /newbot")
    token = input("Вставь токен бота / Paste the bot token (Enter — пропустить / skip): ").strip()
    if not token:
        print(">> Telegram пропущен / Telegram skipped — подтверждения будут в консоли / console approval.")
        return None
    try:
        username = validate_token(token)
    except TelegramApprovalError as exc:
        print(f">> Токен не принят / token rejected: {exc}. Telegram пропущен / skipped.")
        return None
    print(f">> Бот принят / bot OK: @{username}")
    print("\nШАГ 2 / STEP 2 — напиши своему боту /send /start to your bot in Telegram")
    print(f"   (открой чат @{username} и нажми /start / open the chat and press /start)")
    try:
        chat_id = detect_chat_id(token, timeout_seconds=120)
    except (TelegramApprovalError, TimeoutError) as exc:
        print(f">> chat_id не определён / chat_id detection failed: {exc}. Telegram пропущен / skipped.")
        return None
    print(f">> chat_id найден / detected: {chat_id}")
    config["telegram_token"] = token
    config["telegram_chat_id"] = chat_id
    save_config(config)
    print(">> Сохранено / saved to telegram.local.json (в .gitignore, не попадёт в git)")
    return {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id}


def choose_mode(config: dict, forced: str | None) -> str:
    if forced in ("confirmation", "autonomous"):
        return forced
    if config.get("work_mode") in ("confirmation", "autonomous"):
        return config["work_mode"]
    print("\n" + "=" * 72)
    print("ШАГ 3 / STEP 3 — режим работы / work mode")
    print("=" * 72)
    print("  [1] confirmation — каждая сделка через твоё подтверждение (по умолчанию)")
    print("      every trade waits for your explicit approval (default)")
    print("  [2] autonomous  — без вопросов: только demo, потолок 0.05 лота")
    print("      no questions asked: demo-only, 0.05 lot cap")
    choice = input("Выбор / choice [1/2]: ").strip()
    mode = "autonomous" if choice == "2" else "confirmation"
    config["work_mode"] = mode
    save_config(config)
    return mode


def main() -> int:
    parser = argparse.ArgumentParser(description="One-command launcher for the critical-consensus demo.")
    parser.add_argument("--trades", type=int, default=3, help="how many signals to evaluate")
    parser.add_argument("--mode", choices=("confirmation", "autonomous"), default=None,
                        help="override the saved work mode")
    parser.add_argument("--reconfigure", action="store_true", help="forget saved Telegram config")
    parser.add_argument("--autonomous", action="store_true", help="shorthand for --mode autonomous")
    args = parser.parse_args()
    if args.autonomous:
        args.mode = "autonomous"

    print("=" * 72)
    print("  CRITICAL CONSENSUS — живое демо / live demo")
    print("  Реальные данные EUR/USD + демо-деньги / real EURUSD data + demo money")
    print("  Ни один ордер не отправляется / no order is ever sent")
    print("=" * 72)

    ensure_dependencies()

    config = load_config()
    if args.reconfigure and CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
        config = {}
        print(">> Сохранённая конфигурация сброшена / saved config reset.")
    telegram_env = configure_telegram(config)
    mode = choose_mode(config, args.mode)

    env = dict(os.environ)
    if telegram_env:
        env.update(telegram_env)
        print(f"\n>> Режим / mode: {mode} — подтверждения придут в Telegram")
    else:
        print(f"\n>> Режим / mode: {mode} — подтверждения в консоли / console approval")

    cmd = [sys.executable, str(ROOT / "scripts" / "demo_real_data.py"),
           "--trades", str(args.trades)]
    if telegram_env:
        cmd.append("--telegram")
    if mode == "autonomous":
        cmd.append("--autonomous")
    result = subprocess.run(cmd, env=env)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
