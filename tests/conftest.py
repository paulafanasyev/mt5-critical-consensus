"""Test bootstrap: make the fleet importable, with or without the MetaTrader5 package.

The money tests must run on a machine that has no terminal installed (CI, a reviewer's laptop), so
when the real package is missing it is replaced by a stub that keeps module import cheap and makes
an accidental live call fail loudly instead of silently returning nothing.
"""
from __future__ import annotations

import importlib.util
import sys
import types


def _stub_mt5() -> types.ModuleType:
    module = types.ModuleType("MetaTrader5")

    class _Unavailable:
        def __getattr__(self, name: str):
            raise RuntimeError(
                "MetaTrader5 is not installed: this code path needs a live terminal")

    for constant, value in {
        "TIMEFRAME_H1": 16385, "TIMEFRAME_H4": 16388, "TIMEFRAME_D1": 16408,
        "POSITION_TYPE_BUY": 0, "POSITION_TYPE_SELL": 1,
        "ORDER_TYPE_BUY": 0, "ORDER_TYPE_SELL": 1,
        "TRADE_ACTION_DEAL": 1, "TRADE_ACTION_SLTP": 6,
        "TRADE_RETCODE_DONE": 10009, "TRADE_RETCODE_DONE_PARTIAL": 10010,
        "TRADE_RETCODE_REQUOTE": 10004, "TRADE_RETCODE_REJECT": 10006,
        "TRADE_RETCODE_INVALID": 10013, "TRADE_RETCODE_INVALID_VOLUME": 10014,
        "TRADE_RETCODE_INVALID_PRICE": 10015, "TRADE_RETCODE_INVALID_STOPS": 10016,
        "TRADE_RETCODE_TRADE_DISABLED": 10017, "TRADE_RETCODE_MARKET_CLOSED": 10018,
        "TRADE_RETCODE_NO_MONEY": 10019, "TRADE_RETCODE_PRICE_CHANGED": 10020,
        "TRADE_RETCODE_PRICE_OFF": 10021, "TRADE_RETCODE_INVALID_EXPIRATION": 10022,
        "TRADE_RETCODE_ORDER_CHANGED": 10023, "TRADE_RETCODE_TOO_MANY_REQUESTS": 10024,
        "TRADE_RETCODE_NO_CHANGES": 10025, "TRADE_RETCODE_SERVER_DISABLES_AT": 10026,
        "TRADE_RETCODE_CLIENT_DISABLES_AT": 10027, "TRADE_RETCODE_LOCKED": 10028,
        "TRADE_RETCODE_FROZEN": 10029, "TRADE_RETCODE_CONNECTION": 10031,
        "TRADE_RETCODE_LIMIT_ORDERS": 10032, "TRADE_RETCODE_LIMIT_VOLUME": 10034,
        "TRADE_RETCODE_INVALID_FILL": 10030, "TRADE_RETCODE_ONLY_REAL": 10038,
        "TRADE_RETCODE_TIMEOUT": 10012,
        "ORDER_FILLING_FOK": 0, "ORDER_FILLING_IOC": 1, "ORDER_FILLING_RETURN": 2,
        "ORDER_TIME_GTC": 0,
        "ACCOUNT_TRADE_MODE_DEMO": 0, "ACCOUNT_TRADE_MODE_REAL": 2,
    }.items():
        setattr(module, constant, value)
    module.account_info = _Unavailable().__getattr__
    module.initialize = lambda *a, **k: False
    module.shutdown = lambda: None
    module.last_error = lambda: (-9999, "stub")
    module.symbols_get = lambda: []
    module.symbol_info = lambda *a, **k: None
    module.symbol_info_tick = lambda *a, **k: None
    module.symbol_select = lambda *a, **k: False
    module.positions_get = lambda *a, **k: []
    module.history_deals_get = lambda *a, **k: []
    module.copy_rates_from_pos = lambda *a, **k: None
    module.order_send = lambda *a, **k: None
    return module


if importlib.util.find_spec("MetaTrader5") is None:
    sys.modules["MetaTrader5"] = _stub_mt5()
