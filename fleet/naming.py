"""Symbol naming that survives several brokers.

Every broker renames the same instrument: EURUSD here, EURUSDm there, EURUSD.a somewhere else.
Portfolio caps are counted per class, so the class has to come from the base name: matching the
whole string makes a suffixed dollar pair count as nothing, and a book of suffixes from several
brokers then passes a limit it has already broken.
"""
from __future__ import annotations

FX_BASE_LENGTH = 6


def _bases(symbol: str) -> list[str]:
    """The six letters at each end of the name.

    Suffixes are the common case (EURUSDm, EURUSD.pro), but some platforms prefix the feed name
    instead (PRO.EURUSD, FX:EURUSD), and reading only the head would drop those out of every class
    cap. The caller only ever asks whether one of the two ends is a pair.
    """
    letters = "".join(ch for ch in str(symbol).upper() if ch.isalpha())
    if len(letters) < FX_BASE_LENGTH:
        return []
    return [letters[:FX_BASE_LENGTH], letters[-FX_BASE_LENGTH:]]


def is_gold(symbol: str) -> bool:
    upper = str(symbol).upper()
    return "XAU" in upper or "GOLD" in upper


def is_usd_pair(symbol: str) -> bool:
    if is_gold(symbol):
        return False
    return any(base.startswith("USD") or base.endswith("USD") for base in _bases(symbol))
