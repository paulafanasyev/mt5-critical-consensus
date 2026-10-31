from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_mt5_terminal.py"
SPEC = spec_from_file_location("check_mt5_terminal", SCRIPT)
MODULE = module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_terminal_preflight_checks_connection_and_only_closed_bars(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        TIMEFRAME_H1=16385,
        initialize=lambda **kwargs: calls.append(("initialize", kwargs)) or True,
        terminal_info=lambda: SimpleNamespace(connected=True, name="MetaTrader 5", build=5500),
        symbol_select=lambda symbol, enabled: calls.append(("symbol_select", symbol, enabled)) or True,
        copy_rates_from_pos=lambda symbol, timeframe, start, count: (
            calls.append(("copy_rates_from_pos", symbol, timeframe, start, count))
            or [{"time": 1790848800}, {"time": 1790852400}]
        ),
        shutdown=lambda: calls.append(("shutdown",)),
        last_error=lambda: (-1, "mock error"),
    )
    monkeypatch.setattr(MODULE.importlib, "import_module", lambda name: fake)

    result = MODULE.probe_terminal("eurusd", "H1", "C:/MT5/terminal64.exe")

    assert result["status"] == "PASS"
    assert result["terminal_connected"] is True
    assert result["symbol"] == "EURUSD"
    assert result["closed_bars_checked"] == 2
    assert result["trading_api_called"] is False
    assert result["order_submission"] == "NOT_REQUESTED"
    assert ("copy_rates_from_pos", "EURUSD", 16385, 1, 2) in calls
    assert calls[-1] == ("shutdown",)


def test_terminal_preflight_rejects_disconnected_terminal_and_shuts_down(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        TIMEFRAME_M1=1,
        initialize=lambda **kwargs: True,
        terminal_info=lambda: SimpleNamespace(connected=False, name="MetaTrader 5", build=0),
        shutdown=lambda: calls.append("shutdown"),
        last_error=lambda: (-2, "not connected"),
    )
    monkeypatch.setattr(MODULE.importlib, "import_module", lambda name: fake)

    with pytest.raises(RuntimeError, match="not connected"):
        MODULE.probe_terminal("EURUSD", "M1")
    assert calls == ["shutdown"]
