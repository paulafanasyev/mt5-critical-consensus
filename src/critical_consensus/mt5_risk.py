"""Read-only MetaTrader 5 account-risk capture.

The module never sends/places/modifies/cancels orders. It requires an operator-supplied
day-start equity and equity high-water mark; absence of those baselines blocks the
snapshot rather than pretending drawdown metrics are known.
"""
from __future__ import annotations

import importlib
import math
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping


def collect_mt5_risk_snapshot(
    *, symbol: str, day_start_equity: float | None, equity_high_watermark: float | None,
    proposed_risk_fraction: float | None, symbol_groups: Mapping[str, str],
    now: datetime | None = None, terminal_path: str | None = None,
    max_positions: int = 1000,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return {"status": "DENY", "blockers": ["CURRENT_TIME_INVALID"], "trade_allowed": False}
    current = current.astimezone(timezone.utc)
    symbol = str(symbol).strip().upper()
    blockers: list[str] = []
    if not symbol:
        blockers.append("SYMBOL_MISSING")
    day_start = day_start_equity if isinstance(day_start_equity, (int, float)) and not isinstance(day_start_equity, bool) else None
    equity_peak = equity_high_watermark if isinstance(equity_high_watermark, (int, float)) and not isinstance(equity_high_watermark, bool) else None
    proposed_risk = proposed_risk_fraction if isinstance(proposed_risk_fraction, (int, float)) and not isinstance(proposed_risk_fraction, bool) else None
    for value, name in ((day_start, "DAY_START_EQUITY"), (equity_peak, "EQUITY_HIGH_WATERMARK")):
        if value is None or not math.isfinite(float(value)) or value <= 0:
            blockers.append(name + "_MISSING_OR_INVALID")
    if proposed_risk is None or not math.isfinite(float(proposed_risk)) or not 0 <= float(proposed_risk) <= 1:
        blockers.append("PROPOSED_RISK_FRACTION_MISSING_OR_INVALID")
    group = str(symbol_groups.get(symbol, "")).strip() if isinstance(symbol_groups, Mapping) else ""
    if not group:
        blockers.append("SYMBOL_CORRELATION_GROUP_UNKNOWN")
    if blockers:
        return {"status": "DENY", "blockers": sorted(set(blockers)), "trade_allowed": False}

    try:
        mt5 = importlib.import_module("MetaTrader5")
    except ImportError:
        return {"status": "DENY", "blockers": ["METATRADER5_PACKAGE_MISSING"], "trade_allowed": False}
    initialized = mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()
    if not initialized:
        return {"status": "DENY", "blockers": ["MT5_INITIALIZE_FAILED"], "last_error": str(mt5.last_error()[0]), "trade_allowed": False}
    try:
        info = mt5.account_info()
        if info is None:
            return {"status": "DENY", "blockers": ["MT5_ACCOUNT_INFO_MISSING"], "trade_allowed": False}
        trade_mode = int(getattr(info, "trade_mode", -1))
        demo_mode = int(getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0))
        contest_mode = int(getattr(mt5, "ACCOUNT_TRADE_MODE_CONTEST", -2))
        mode = "demo" if trade_mode == demo_mode else ("contest" if trade_mode == contest_mode else "real" if trade_mode >= 0 else "unknown")
        positions_raw = mt5.positions_get()
        if positions_raw is None:
            return {"status": "DENY", "blockers": ["MT5_POSITIONS_UNAVAILABLE"], "account_mode": mode, "trade_allowed": False}
        positions = list(positions_raw)
        if len(positions) > max_positions:
            return {"status": "DENY", "blockers": ["MT5_POSITION_COUNT_IMPLAUSIBLE"], "account_mode": mode, "trade_allowed": False}

        day_start_utc = current.replace(hour=0, minute=0, second=0, microsecond=0)
        deals_raw = mt5.history_deals_get(day_start_utc, current)
        if deals_raw is None:
            return {"status": "DENY", "blockers": ["MT5_DAILY_HISTORY_UNAVAILABLE"], "account_mode": mode, "trade_allowed": False}
        deals = list(deals_raw)
        equity = float(info.equity)
        balance = float(info.balance)
        daily_pnl = sum(float(getattr(deal, "profit", 0.0))
                        + float(getattr(deal, "commission", 0.0))
                        + float(getattr(deal, "swap", 0.0))
                        + float(getattr(deal, "fee", 0.0)) for deal in deals)
        daily_loss_fraction = max(0.0, -daily_pnl / float(day_start))
        drawdown_fraction = max(0.0, (float(equity_peak) - equity) / float(equity_peak))
        correlated = 0
        for position in positions:
            pos_symbol = str(getattr(position, "symbol", "")).upper()
            if str(symbol_groups.get(pos_symbol, "")) == group:
                correlated += 1
        if mode != "demo":
            blockers.append("MT5_ACCOUNT_NOT_DEMO")
        if not math.isfinite(equity) or equity <= 0 or not math.isfinite(balance) or balance <= 0:
            blockers.append("MT5_ACCOUNT_EQUITY_OR_BALANCE_INVALID")
        if float(equity_peak) < equity:
            # The caller's high-water mark is inconsistent with currently observed equity.
            blockers.append("EQUITY_HIGH_WATERMARK_BELOW_CURRENT_EQUITY")
        if not math.isfinite(daily_pnl) or not math.isfinite(daily_loss_fraction) or not math.isfinite(drawdown_fraction):
            blockers.append("MT5_RISK_METRICS_NONFINITE")
        return {
            "status": "PASS" if not blockers else "DENY",
            "observed_at": current.isoformat(), "account_mode": mode,
            "terminal_server": str(getattr(info, "server", "")),
            "symbol": symbol, "symbol_group": group, "account_currency": str(getattr(info, "currency", "")),
            "account_equity": equity, "account_balance": balance,
            "day_start_equity": float(day_start), "equity_high_watermark": float(equity_peak),
            "daily_pnl": daily_pnl, "daily_loss_fraction": daily_loss_fraction,
            "drawdown_fraction": drawdown_fraction, "risk_fraction": float(proposed_risk),
            "open_positions": len(positions), "correlated_open_positions": correlated,
            "position_symbols": sorted(str(getattr(position, "symbol", "")).upper() for position in positions),
            "daily_deals_count": len(deals), "within_high_impact_news_window": None,
            "news_status": "NOT_CONNECTED_FAIL_CLOSED", "trade_allowed": False,
            "blockers": sorted(set(blockers)),
            "evidence_limit": "Risk snapshot reads terminal state only; day-start equity/high-water mark are operator inputs, and news-window status is not connected.",
        }
    except (TypeError, ValueError, OverflowError, AttributeError):
        return {"status": "DENY", "blockers": ["MT5_RISK_SNAPSHOT_PARSE_FAILED"], "trade_allowed": False}
    finally:
        mt5.shutdown()
