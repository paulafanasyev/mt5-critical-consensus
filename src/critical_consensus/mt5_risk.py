"""Order-free MetaTrader 5 account, market and proposed-order risk capture.

It may call ``symbol_select`` to make a symbol visible in Market Watch; that is the only
call that changes terminal state, and no order is ever sent.

This module never sends/places/modifies/cancels orders. It requires a verified
broker-server-day-start equity, equity high-water mark, and the broker server's UTC
offset (MT5 timestamps are server wall-clock, not UTC). Proposed risk is computed from the
terminal's tick size/loss value and the exact requested volume/entry/SL; no user-entered
risk percentage is trusted as if it were a measured broker value.
"""
from __future__ import annotations

import importlib
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .risk_controls import risk_request_binding_hash


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _tick_utc(tick: Any, offset_seconds: int) -> float | None:
    """MT5 tick time is broker wall-clock; convert it to a UTC epoch or return None."""
    raw_tick_time = getattr(tick, "time_msc", None)
    if raw_tick_time is not None:
        tick_timestamp = _number(raw_tick_time)
        tick_timestamp = tick_timestamp / 1000.0 if tick_timestamp is not None else None
    else:
        tick_timestamp = _number(getattr(tick, "time", None))
    if tick_timestamp is None or tick_timestamp <= 0:
        return None
    return tick_timestamp - offset_seconds


def collect_mt5_risk_snapshot(
    *, symbol: str, day_start_equity: float | None, equity_high_watermark: float | None,
    proposed_request: Mapping[str, Any] | None, symbol_groups: Mapping[str, str],
    now: datetime | None = None, terminal_path: str | None = None,
    max_positions: int = 1000, max_slippage_points: int = 20, max_quote_age_seconds: int = 15,
    server_utc_offset_seconds: int | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return {"status": "DENY", "blockers": ["CURRENT_TIME_INVALID"], "trade_allowed": False}
    current = current.astimezone(timezone.utc)
    symbol = str(symbol).strip().upper()
    blockers: list[str] = []
    # MT5 returns broker-server wall-clock timestamps with no zone information. Quote age and
    # midnight day boundaries cannot be derived without an explicit, validated server offset.
    if (isinstance(server_utc_offset_seconds, bool)
            or not isinstance(server_utc_offset_seconds, int)
            or server_utc_offset_seconds % 60 != 0
            or abs(server_utc_offset_seconds) > 14 * 3600):
        blockers.append("MT5_SERVER_UTC_OFFSET_MISSING_OR_INVALID")
        offset_seconds = 0
    else:
        offset_seconds = server_utc_offset_seconds
    if not symbol:
        blockers.append("SYMBOL_MISSING")
    day_start = _number(day_start_equity)
    equity_peak = _number(equity_high_watermark)
    if day_start is None or day_start <= 0:
        blockers.append("DAY_START_EQUITY_MISSING_OR_INVALID")
    if equity_peak is None or equity_peak <= 0:
        blockers.append("EQUITY_HIGH_WATERMARK_MISSING_OR_INVALID")
    if isinstance(max_slippage_points, bool) or not isinstance(max_slippage_points, int) or max_slippage_points < 0:
        blockers.append("MAX_SLIPPAGE_POLICY_INVALID")
    group = str(symbol_groups.get(symbol, "")).strip() if isinstance(symbol_groups, Mapping) else ""
    if not group:
        blockers.append("SYMBOL_CORRELATION_GROUP_UNKNOWN")
    if not isinstance(proposed_request, Mapping):
        blockers.append("PROPOSED_ORDER_REQUEST_MISSING")
        request: dict[str, Any] = {}
        request_hash = None
    else:
        request = dict(proposed_request)
        if str(request.get("instrument", "")).strip().upper() != symbol:
            blockers.append("PROPOSED_ORDER_SYMBOL_MISMATCH")
        try:
            request_hash = risk_request_binding_hash(request)
        except ValueError:
            request_hash = None
            blockers.append("PROPOSED_ORDER_BINDING_INVALID")
    if blockers:
        return {"status": "DENY", "blockers": sorted(set(blockers)), "trade_allowed": False}

    direction = str(request["direction"]).strip().upper()
    size = float(request["size"])
    expected_price = float(request["expected_price"])
    sl_tp = request["sl_tp"]
    stop_loss = float(sl_tp["sl"])
    take_profit = float(sl_tp["tp"])

    if isinstance(max_quote_age_seconds, bool) or not isinstance(max_quote_age_seconds, int) or max_quote_age_seconds < 1:
        return {"status": "DENY", "blockers": ["MAX_QUOTE_AGE_POLICY_INVALID"], "trade_allowed": False}

    try:
        mt5 = importlib.import_module("MetaTrader5")
    except ImportError:
        return {"status": "DENY", "blockers": ["METATRADER5_PACKAGE_MISSING"], "trade_allowed": False}
    initialized = mt5.initialize(path=terminal_path) if terminal_path else mt5.initialize()
    if not initialized:
        return {"status": "DENY", "blockers": ["MT5_INITIALIZE_FAILED"], "last_error_code": int(mt5.last_error()[0]), "trade_allowed": False}
    try:
        info = mt5.account_info()
        if info is None:
            return {"status": "DENY", "blockers": ["MT5_ACCOUNT_INFO_MISSING"], "trade_allowed": False}
        login = getattr(info, "login", None)
        if isinstance(login, bool) or not isinstance(login, int) or login <= 0:
            return {"status": "DENY", "blockers": ["MT5_ACCOUNT_LOGIN_INVALID"], "trade_allowed": False}
        account_server = str(getattr(info, "server", "")).strip()
        if not account_server:
            return {"status": "DENY", "blockers": ["MT5_ACCOUNT_SERVER_MISSING"], "trade_allowed": False}
        trade_mode = int(getattr(info, "trade_mode", -1))
        demo_mode = int(getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0))
        contest_mode = int(getattr(mt5, "ACCOUNT_TRADE_MODE_CONTEST", -2))
        mode = "demo" if trade_mode == demo_mode else ("contest" if trade_mode == contest_mode else "real" if trade_mode >= 0 else "unknown")

        if not mt5.symbol_select(symbol, True):
            return {"status": "DENY", "blockers": ["MT5_SYMBOL_SELECTION_FAILED"], "account_mode": mode, "trade_allowed": False}
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return {"status": "DENY", "blockers": ["MT5_SYMBOL_SPECIFICATION_MISSING"], "account_mode": mode, "trade_allowed": False}
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"status": "DENY", "blockers": ["MT5_CURRENT_TICK_MISSING"], "account_mode": mode, "trade_allowed": False}
        quote_timestamp_utc = _tick_utc(tick, offset_seconds)
        if quote_timestamp_utc is None:
            return {"status": "DENY", "blockers": ["MT5_TICK_TIMESTAMP_MISSING"], "account_mode": mode, "trade_allowed": False}
        quote_age_seconds = current.timestamp() - quote_timestamp_utc
        if quote_age_seconds < -5:
            return {"status": "DENY", "blockers": ["MT5_QUOTE_FROM_FUTURE"], "account_mode": mode, "trade_allowed": False}
        if quote_age_seconds > max_quote_age_seconds:
            return {"status": "DENY", "blockers": ["MT5_QUOTE_STALE"], "account_mode": mode, "trade_allowed": False}

        tick_size = _number(getattr(symbol_info, "trade_tick_size", None))
        tick_value_loss = _number(getattr(symbol_info, "trade_tick_value_loss", None))
        if tick_value_loss is None or tick_value_loss <= 0:
            tick_value_loss = _number(getattr(symbol_info, "trade_tick_value", None))
        point = _number(getattr(symbol_info, "point", None))
        if tick_size is None or tick_size <= 0 or tick_value_loss is None or tick_value_loss <= 0:
            return {"status": "DENY", "blockers": ["MT5_TICK_RISK_SPECIFICATION_INVALID"], "account_mode": mode, "trade_allowed": False}
        if point is None or point <= 0:
            return {"status": "DENY", "blockers": ["MT5_SYMBOL_POINT_INVALID"], "account_mode": mode, "trade_allowed": False}
        reference_price = _number(getattr(tick, "ask" if direction == "BUY" else "bid", None))
        if reference_price is None or reference_price <= 0:
            return {"status": "DENY", "blockers": ["MT5_REFERENCE_PRICE_INVALID"], "account_mode": mode, "trade_allowed": False}
        slippage_points = abs(reference_price - expected_price) / point
        if slippage_points > max_slippage_points:
            blockers.append("PRICE_SLIPPAGE_LIMIT_EXCEEDED")
        risk_entry_price = (max(expected_price, reference_price) if direction == "BUY"
                            else min(expected_price, reference_price))
        stop_distance = abs(expected_price - stop_loss)
        if stop_distance <= 0 or abs(reference_price - expected_price) > stop_distance * 0.10:
            # A fill more than a tenth of the stop width away is no longer the order the
            # human approved, even when it is inside the broker's point-based limit.
            blockers.append("PRICE_SLIPPAGE_EXCEEDS_STOP_DISTANCE_FRACTION")
        if direction == "BUY":
            geometry_ok = stop_loss < risk_entry_price < take_profit
        else:
            geometry_ok = take_profit < risk_entry_price < stop_loss
        if not geometry_ok:
            blockers.append("RISK_CONSERVATIVE_ENTRY_OUTSIDE_GEOMETRY")

        positions_raw = mt5.positions_get()
        if positions_raw is None:
            return {"status": "DENY", "blockers": ["MT5_POSITIONS_UNAVAILABLE"], "account_mode": mode, "trade_allowed": False}
        positions = list(positions_raw)
        if len(positions) > max_positions:
            return {"status": "DENY", "blockers": ["MT5_POSITION_COUNT_IMPLAUSIBLE"], "account_mode": mode, "trade_allowed": False}

        server_current = current + timedelta(seconds=offset_seconds)
        deals_raw = mt5.history_deals_get(
            server_current.replace(hour=0, minute=0, second=0, microsecond=0), server_current)
        if deals_raw is None:
            return {"status": "DENY", "blockers": ["MT5_DAILY_HISTORY_UNAVAILABLE"], "account_mode": mode, "trade_allowed": False}
        deals = list(deals_raw)
        # MetaTrader also reports deposits/withdrawals as deals (DEAL_TYPE_BALANCE) whose
        # "profit" is the cashflow. Counting them as trading P/L would silently refresh the
        # daily-loss budget; the balance reconciliation below must catch them instead.
        trade_types = {int(getattr(mt5, "DEAL_TYPE_BUY", 0)), int(getattr(mt5, "DEAL_TYPE_SELL", 1))}
        trade_deals = [deal for deal in deals if int(getattr(deal, "type", -1)) in trade_types]
        daily_pnl = sum(
            float(getattr(deal, "profit", 0.0)) + float(getattr(deal, "commission", 0.0))
            + float(getattr(deal, "swap", 0.0)) + float(getattr(deal, "fee", 0.0))
            for deal in trade_deals
        )
        equity = _number(getattr(info, "equity", None))
        balance = _number(getattr(info, "balance", None))
        if equity is None or equity <= 0 or balance is None or balance <= 0:
            return {"status": "DENY", "blockers": ["MT5_ACCOUNT_EQUITY_OR_BALANCE_INVALID"], "account_mode": mode, "trade_allowed": False}
        if equity_peak < equity:
            blockers.append("EQUITY_HIGH_WATERMARK_BELOW_CURRENT_EQUITY")
        if equity_peak < day_start:
            blockers.append("EQUITY_HIGH_WATERMARK_BELOW_DAY_START")
        # Starting equity plus realised P/L must reconstruct the balance; otherwise the
        # operator baseline (or an unrecorded cashflow) understates the daily loss.
        if day_start + daily_pnl < balance * 0.99:
            blockers.append("DAY_START_EQUITY_BELOW_DAILY_PNL_IMPLIED_BALANCE")
        # Equity movement includes open/unrealised losses. Adjust the manually verified baseline
        # for deposits/withdrawals before capture; the collector cannot reliably infer cashflows.
        daily_loss_fraction = max(0.0, (day_start - equity) / day_start)
        drawdown_fraction = max(0.0, (equity_peak - equity) / equity_peak)
        # Equity can be above balance on floating profit; sizing on equity alone would let
        # the same absolute risk look smaller than it is relative to starting capital.
        risk_base = min(equity, balance)
        risk_value_account_currency = abs(risk_entry_price - stop_loss) / tick_size * tick_value_loss * size
        risk_fraction = risk_value_account_currency / risk_base

        def _position_stop_risk(position: Any) -> float | None:
            pos_symbol = str(getattr(position, "symbol", "")).strip().upper()
            pos_type = int(getattr(position, "type", -1))
            pos_volume = _number(getattr(position, "volume", None))
            pos_stop = _number(getattr(position, "sl", None))
            if (pos_type not in (0, 1) or pos_volume is None or pos_volume <= 0
                    or pos_stop is None or pos_stop <= 0):
                return None
            spec = symbol_info if pos_symbol == symbol else mt5.symbol_info(pos_symbol)
            if spec is None:
                return None
            pos_tick_size = _number(getattr(spec, "trade_tick_size", None))
            pos_tick_value = _number(getattr(spec, "trade_tick_value_loss", None))
            if pos_tick_value is None or pos_tick_value <= 0:
                pos_tick_value = _number(getattr(spec, "trade_tick_value", None))
            if pos_tick_size is None or pos_tick_size <= 0 or pos_tick_value is None or pos_tick_value <= 0:
                return None
            # Remaining risk is the distance from the *current* price to the stop, not from
            # the entry: a position moved to break-even has no loss left to realize.
            tick = mt5.symbol_info_tick(pos_symbol)
            if tick is None:
                return None
            # The quote must be as fresh as the traded symbol's: a stale holiday quote on a
            # correlated position would understate the exposure added to the caps.
            pos_quote_utc = _tick_utc(tick, offset_seconds)
            if pos_quote_utc is None:
                return None
            pos_quote_age = current.timestamp() - pos_quote_utc
            if pos_quote_age < -5 or pos_quote_age > max_quote_age_seconds:
                return None
            if pos_type == 0:
                price = _number(getattr(tick, "bid", None))
                distance = price - pos_stop if price is not None and price > 0 else None
            else:
                price = _number(getattr(tick, "ask", None))
                distance = pos_stop - price if price is not None and price > 0 else None
            if distance is None:
                return None
            return max(0.0, distance) / pos_tick_size * pos_tick_value * pos_volume

        open_stop_risk_value = 0.0
        for position in positions:
            position_risk = _position_stop_risk(position)
            if position_risk is None:
                blockers.append("OPEN_POSITION_STOP_RISK_UNKNOWN")
            else:
                open_stop_risk_value += position_risk
        open_stop_risk_fraction = open_stop_risk_value / risk_base
        # daily_pnl is legitimately negative on a losing day; only the fractions are bounded.
        metrics = (daily_pnl, daily_loss_fraction, drawdown_fraction, risk_value_account_currency,
                   risk_fraction, open_stop_risk_value, open_stop_risk_fraction)
        if not all(math.isfinite(v) for v in metrics) or any(
                v < 0 for v in (daily_loss_fraction, drawdown_fraction, risk_value_account_currency,
                                risk_fraction, open_stop_risk_value, open_stop_risk_fraction)
        ):
            blockers.append("MT5_RISK_METRICS_NONFINITE")

        correlated = 0
        position_symbols = sorted(str(getattr(position, "symbol", "")).upper() for position in positions)
        unknown_groups = sorted({pos_symbol for pos_symbol in position_symbols if not str(symbol_groups.get(pos_symbol, "")).strip()})
        if unknown_groups:
            blockers.append("OPEN_POSITION_GROUP_UNKNOWN")
        for pos_symbol in position_symbols:
            if str(symbol_groups.get(pos_symbol, "")).strip() == group:
                correlated += 1
        if mode != "demo":
            blockers.append("MT5_ACCOUNT_NOT_DEMO")

        return {
            "status": "PASS" if not blockers else "DENY", "observed_at": current.isoformat(),
            "account_mode": mode, "account_login": login, "account_server": account_server,
            "terminal_server": account_server,
            "symbol": symbol, "symbol_group": group, "account_currency": str(getattr(info, "currency", "")),
            "account_equity": equity, "account_balance": balance,
            "day_start_equity": float(day_start), "equity_high_watermark": float(equity_peak),
            "daily_pnl": daily_pnl, "daily_loss_fraction": daily_loss_fraction,
            "drawdown_fraction": drawdown_fraction, "risk_fraction": risk_fraction,
            "risk_value_account_currency": risk_value_account_currency,
            "open_stop_risk_value_account_currency": open_stop_risk_value,
            "open_stop_risk_fraction": open_stop_risk_fraction,
            "risk_tick_size": tick_size, "risk_tick_value_loss": tick_value_loss,
            "risk_entry_price": risk_entry_price,
            "request_binding_sha256": request_hash,
            "request_binding": {"instrument": symbol, "direction": direction, "size": size,
                                "expected_price": expected_price, "sl_tp": {"sl": stop_loss, "tp": take_profit}},
            "reference_price": reference_price, "quote_age_seconds": quote_age_seconds,
            "quote_utc": datetime.fromtimestamp(quote_timestamp_utc, tz=timezone.utc).isoformat(),
            "server_utc_offset_seconds": offset_seconds,
            "server_observed_at": server_current.isoformat(),
            "max_quote_age_seconds": max_quote_age_seconds, "slippage_points_at_capture": slippage_points,
            "max_slippage_points": max_slippage_points, "open_positions": len(positions),
            "correlated_open_positions": correlated, "position_symbols": position_symbols,
            "unknown_position_group_symbols": unknown_groups, "daily_deals_count": len(trade_deals),
            "within_high_impact_news_window": None, "news_status": "NOT_CONNECTED_FAIL_CLOSED",
            "trade_allowed": False, "blockers": sorted(set(blockers)),
            "evidence_limit": "Tick-value risk is an estimate before fees, slippage and gaps; proposed order fields are hash-bound and risk is sized at the worse of reviewed and reference price. Day-start equity/high-water mark and cash-flow adjustment are operator inputs. News status is not connected.",
        }
    except (TypeError, ValueError, OverflowError, AttributeError, KeyError):
        return {"status": "DENY", "blockers": ["MT5_RISK_SNAPSHOT_PARSE_FAILED"], "trade_allowed": False}
    finally:
        mt5.shutdown()
