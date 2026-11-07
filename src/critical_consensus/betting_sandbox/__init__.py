"""Paper-only sports-betting sandbox.

This package never moves real money and never talks to a bookmaker. It uses
public historical football results/odds, a demo wallet persisted in SQLite, and
a simple Poisson baseline so the user can measure whether a staking rule would
have won or lost. Past results do not prove future profit.
"""
from __future__ import annotations

__all__ = ["pricing", "model", "data", "staking", "ledger", "evaluate"]
