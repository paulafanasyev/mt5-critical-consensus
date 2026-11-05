"""The consent a process needs before it touches money that is not play money.

Four paths in this repository can put an order on a live account: the park's own worker,
`scripts/real_mirror.py`, `scripts/real_bridge.py` and `scripts/desk_watch.py`. Each of them asks this
module, and each of them asks it again at the moment of sending.

The rule this module encodes is that permission is never the default and never a single word:

  * the stand — `C:\\mt5-fleet\\PAUSE`, the same file the conductor and every shard obey — must not
    be on disk. A kill switch some processes ignore is a kill switch that is not armed;
  * the operator must have said `--live` to this process, in this invocation;
  * the registry must name that account, call it `real`, and carry `enabled: true`. A login the
    registry does not carry is not an account this machine has any business trading, and the same
    row is where the operator's own lot ceiling lives.

An empty refusal is the only thing that means "go". Every reason is a Russian sentence, because the
person reading it at three in the morning is the operator, not a test.

`refusal` is the door to *opening*. Closing is a different act and asks a different question
(`exit_refusal`): the stand and `--live` are the operator's word about new money, and an order that
waits for him to lift the stand is a lot left without a stop. What both doors ask is that the broker
name the money — the login and the server it answers on. What only the opening door asks is the
registry row: an account the operator switched off must stop being bought, but its open lots still
have somebody who can sell them.
"""
from __future__ import annotations

import pathlib

from .config import CONFIG_PATH, PAUSE_FILE, ConfigError, load, server_key


def permitted_account(login, *, server: str = "", config_path: pathlib.Path | None = None):
    """The registry row that lets this live account trade, or None if nothing does.

    `mode: real` and `enabled: true` are the operator's word; the server has to agree with what the
    broker answered, normalised the same way every other identity comparison in this project does.
    An answer that names no server agrees with nothing: it is a window that stopped describing the
    money in front of it, and the row written for `Exness-MT5Real26` cannot authorise a terminal that
    will not say where it is.
    """
    wanted = str(login or "").strip()
    if not wanted.isdigit():
        return None
    answered = server_key(server)
    if not answered:
        return None
    try:
        config = load(config_path if config_path is not None else CONFIG_PATH)
    except (ConfigError, OSError, ValueError):
        return None
    for account in config.accounts:
        if str(account.login) != wanted or not account.is_real or not account.enabled:
            continue
        if server_key(account.server) != answered:
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


def stand_refusal(pause_file: pathlib.Path | None = None) -> str:
    """The kill switch's own answer, asked apart from the rest of the consent.

    A process that is allowed to trade play money still stops here: the stand is the operator's hand
    on the machine, and it outranks any per-account permission.
    """
    stand = pause_file if pause_file is not None else PAUSE_FILE
    if stand.exists():
        return (f"стоянка на месте ({stand}): пока этот файл существует, ни один ордер не отправлен")
    return ""


def refusal(login, *, live: bool, server: str = "",
            config_path: pathlib.Path | None = None,
            pause_file: pathlib.Path | None = None) -> str:
    """Why this process may not send a real order; an empty string is the operator's permission."""
    held = stand_refusal(pause_file)
    if held:
        return held
    if not live:
        return ("нужен явный флаг --live: копия демо-сделки на настоящие деньги не запускается "
                "по умолчанию и не запускается без слова оператора")
    return identity_refusal(login, server=server, config_path=config_path)


def identity_refusal(login, *, server: str = "",
                     config_path: pathlib.Path | None = None) -> str:
    """The part of the consent that outlives the stand: whose money this is.

    Split out because closing a lot is not the same act as opening one. The park's own rule is that
    the stand stops entries and never exits (an unmanaged position is worse than an unused signal),
    and a real lot mirrored from the demo is exactly such a position: if this process is the only
    thing that knows the lot is ours, parking the machine must not blindfold it. What still has to
    hold, at every close, is that the broker names the account and the registry still calls it ours.
    """
    wanted = str(login or "").strip()
    if not wanted.isdigit():
        return f"брокер не назвал номер счёта (отвечает {login!r}) — разрешать нечего"
    if permitted_account(login, server=server, config_path=config_path) is None:
        return (f"счёт {wanted} не разрешён реестром: нужна запись с mode: real и enabled: true, "
                f"на том сервере, который назвал брокер ({server or 'не назван'})")
    return ""


def exit_refusal(login, *, server: str = "") -> str:
    """Why this process may not close what it opened. Deliberately shorter than `refusal`.

    Two answers belong here, and both are about the lot rather than the licence. The terminal has to
    name the account: a close ordered against a window that answers no login is a sale of nobody's
    money in particular. And it has to name the server it is sitting on — the same login string
    exists on a broker's demo and real sides, and a window that stopped describing where it is cannot
    prove that the ticket in front of it is the one this process opened there. The stand, `--live` and
    the registry row gate *openings*; a protective exit that waits for the operator to lift the stand
    is an exit that never happens, and a row he switched off must not strand the lots it already
    holds.
    """
    wanted = str(login or "").strip()
    if not wanted.isdigit():
        return f"брокер не назвал номер счёта (отвечает {login!r}) — непонятно, чей лот закрывать"
    if not server_key(server):
        return (f"счёт {wanted} не назвал свой сервер (отвечает {server!r}) — непонятно, на каком "
                f"счёте этот лот, и закрывать его нечем")
    return ""
