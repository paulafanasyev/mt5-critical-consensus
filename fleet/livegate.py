"""The consent a process needs before it touches money that is not play money.

Two files in this repository can place an order on a live account (`scripts/real_mirror.py`,
`scripts/real_bridge.py`). Nothing else here can: the park's own worker refuses an account whose
registry `mode` is real unless `--live` was passed to it.

The rule this module encodes is that permission is never the default and never a single word:

  * the stand — `C:\\mt5-fleet\\PAUSE`, the same file the conductor and every shard obey — must not
    be on disk. A kill switch some processes ignore is a kill switch that is not armed;
  * the operator must have said `--live` to this process, in this invocation;
  * the registry must name that account, call it `real`, and carry `enabled: true`. A login the
    registry does not carry is not an account this machine has any business trading, and the same
    row is where the operator's own lot ceiling lives.

An empty refusal is the only thing that means "go". Every reason is a Russian sentence, because the
person reading it at three in the morning is the operator, not a test.
"""
from __future__ import annotations

import pathlib

from .config import CONFIG_PATH, PAUSE_FILE, ConfigError, load, server_key


def permitted_account(login, *, server: str = "", config_path: pathlib.Path | None = None):
    """The registry row that lets this live account trade, or None if nothing does.

    `mode: real` and `enabled: true` are the operator's word; the server has to agree with what the
    broker answered, normalised the same way every other identity comparison in this project does.
    """
    wanted = str(login or "").strip()
    if not wanted.isdigit():
        return None
    try:
        config = load(config_path if config_path is not None else CONFIG_PATH)
    except (ConfigError, OSError, ValueError):
        return None
    for account in config.accounts:
        if str(account.login) != wanted or not account.is_real or not account.enabled:
            continue
        if server and server_key(account.server) != server_key(server):
            continue
        return account
    return None


def capped_lots(row, asked: float) -> float:
    """The largest lot a real send may carry, given the row that authorised it.

    A script whose own default is 0.05 mirroring onto an account the operator capped at 0.01 is not
    copying the demo, it is multiplying the risk. No row, no permission, no lot.
    """
    ceiling = float(row.risk.max_lots) if row is not None else 0.0
    return min(float(asked), ceiling)


def refusal(login, *, live: bool, server: str = "",
            config_path: pathlib.Path | None = None,
            pause_file: pathlib.Path | None = None) -> str:
    """Why this process may not send a real order; an empty string is the operator's permission."""
    stand = pause_file if pause_file is not None else PAUSE_FILE
    if stand.exists():
        return (f"стоянка на месте ({stand}): пока этот файл существует, ни один реальный ордер "
                "не отправлен")
    if not live:
        return ("нужен явный флаг --live: копия демо-сделки на настоящие деньги не запускается "
                "по умолчанию и не запускается без слова оператора")
    wanted = str(login or "").strip()
    if not wanted.isdigit():
        return f"брокер не назвал номер счёта (отвечает {login!r}) — разрешать нечего"
    if permitted_account(login, server=server, config_path=config_path) is None:
        return (f"счёт {wanted} не разрешён реестром: нужна запись с mode: real и enabled: true, "
                f"на том сервере, который назвал брокер ({server or 'не назван'})")
    return ""
