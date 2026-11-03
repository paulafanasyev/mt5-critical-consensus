"""Code-first MT5 fleet: many accounts, many brokers, one book.

The terminal is treated as disposable infrastructure; the trading logic, risk and accounting all
live here so a new broker is a config record rather than a manual session.
"""

__all__ = ["config", "terminals", "broker", "risk", "strategy", "store", "worker", "conductor"]
__version__ = "1.0.0"
