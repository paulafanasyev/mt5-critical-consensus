"""Render committed evidence for a paper demo journal and a real-bar edge research run.

Market data is read-only here: only ``copy_rates_*`` is used, and this file contains no MT5
trading call. Everything it prints is either a journal event or a number recomputed from
real bars, so a chart can be regenerated from the same inputs and compared byte-for-byte.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

UTC = timezone.utc
SVG_HEADER = ('<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
              'viewBox="0 0 {width} {height}" role="img" aria-label="{title}">')
PLOT_LEFT = 72.0
PLOT_RIGHT = 16.0
PLOT_TOP = 34.0
PLOT_BOTTOM = 46.0
AXIS_COLOR = "#5b6470"
GRID_COLOR = "#d7dce2"
PALETTE = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#8c564b", "#e377c2")
DEFAULT_COST_STEPS_BPS = (0.0, 2.5, 5.0, 10.0, 20.0, 30.0)


def _escape(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def load_journal_events(db_path: Path) -> list[dict[str, Any]]:
    """Ordered ``audit_events`` rows; the journal schema is checked, never assumed."""
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
        if "audit_events" not in tables:
            raise ValueError(f"{db_path}: no audit_events table, so this is not a decision journal")
        rows = []
        for row in con.execute("select event_type, payload from audit_events order by seq"):
            payload = json.loads(row["payload"])
            payload.setdefault("event_type", row["event_type"])
            rows.append(payload)
        return rows
    finally:
        con.close()


def equity_series(events: Sequence[dict[str, Any]]) -> list[tuple[datetime, float]]:
    points = []
    for event in events:
        if event.get("event_type") != "equity_point":
            continue
        at = event.get("at")
        equity = event.get("equity")
        if not isinstance(at, str) or isinstance(equity, bool) or not isinstance(equity, (int, float)):
            continue
        try:
            moment = datetime.fromisoformat(at)
        except ValueError:
            continue
        points.append((moment, float(equity)))
    points.sort(key=lambda item: item[0])
    return points


def policy_cash_per_price_unit(tick_size: float, tick_value: float,
                               contract_size: float | None) -> float:
    """The cash per price unit a policy YAML declares, using the sizer's own source priority.

    ``SymbolSpec.cash_per_price_unit`` prefers the declared contract size because the broker's
    tick field is not reliable - MetaQuotes-Demo publishes XAUUSD at 10 $ per price unit per lot
    there while the contract and its own profit math say 100. Only a journal with no sizing rows
    of its own falls back to this.
    """
    if contract_size is not None:
        return contract_size
    return tick_value / tick_size


def recorded_cash_per_price_unit(events: Sequence[dict[str, Any]]) -> float | None:
    """The cash per price unit a run actually sized at, read back out of its own cycles.

    ``sizing.risk_per_lot`` is ``proposal.risk_distance`` times that number, so the pair divides
    it out without trusting any policy file. This is the basis a report has to use: a journal
    written before the contract-priced fix earned its P/L at the broker's tick field and was
    sized at the same field, so re-reading it against today's declared contract would divide an
    unchanged numerator by a tenfold larger denominator and redraw the same trades as
    one-tenth of the R they were booked at.
    """
    for event in events:
        if event.get("event_type") != "agent_cycle":
            continue
        sizing, proposal = event.get("sizing"), event.get("proposal")
        if not isinstance(sizing, dict) or not isinstance(proposal, dict):
            continue
        per_lot, distance = sizing.get("risk_per_lot"), proposal.get("risk_distance")
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               for value in (per_lot, distance)):
            continue
        if per_lot > 0 and distance > 0:
            ratio = float(per_lot) / float(distance)
            if math.isfinite(ratio) and ratio > 0:
                return ratio
    return None


def planned_risk_currency(trade: dict[str, Any], *, cash_per_price_unit: float) -> float | None:
    """Account-currency risk the trade was sized for: stop distance times cash per price unit."""
    entry, stop = trade.get("entry_price"), trade.get("stop_loss")
    volume = trade.get("volume")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (entry, stop, volume)):
        return None
    if not cash_per_price_unit or cash_per_price_unit <= 0:
        return None
    risk = abs(float(entry) - float(stop)) * cash_per_price_unit * float(volume)
    return risk if math.isfinite(risk) and risk > 0 else None


def r_multiples(events: Sequence[dict[str, Any]], *,
                cash_per_price_unit: float) -> list[tuple[str, float]]:
    """Realised P/L per closed paper trade divided by the risk that trade was sized for."""
    out = []
    for event in events:
        if event.get("event_type") != "paper_trade_closed":
            continue
        risk = planned_risk_currency(event, cash_per_price_unit=cash_per_price_unit)
        pnl = event.get("pnl")
        if risk is None or isinstance(pnl, bool) or not isinstance(pnl, (int, float)):
            continue
        opened = str(event.get("opened_at", ""))
        out.append((opened[:16], float(pnl) / risk))
    return out


def closes_of(bars: Sequence[dict[str, Any]]) -> list[float]:
    return [float(bar["close"]) for bar in bars]


def percent_returns(closes: Sequence[float]) -> list[float]:
    return [b / a - 1.0 for a, b in zip(closes, closes[1:]) if a]


def lag1_autocorrelation(values: Sequence[float]) -> float:
    mean = statistics.fmean(values)
    deviations = [value - mean for value in values]
    denominator = sum(deviation * deviation for deviation in deviations)
    if not denominator:
        return 0.0
    numerator = sum(a * b for a, b in zip(deviations, deviations[1:]))
    return numerator / denominator


def hourly_t_statistics(bars: Sequence[dict[str, Any]]) -> list[tuple[int, int, float]]:
    """Mean next-bar return per UTC hour divided by its standard error, over 24 buckets."""
    closes = closes_of(bars)
    rets = percent_returns(closes)
    buckets: dict[int, list[float]] = {}
    for bar, value in zip(bars[1:], rets):
        hour = datetime.fromtimestamp(int(bar["time"]), UTC).hour
        buckets.setdefault(hour, []).append(value)
    profile = []
    for hour in sorted(buckets):
        samples = buckets[hour]
        if len(samples) < 20:
            continue
        stdev = statistics.pstdev(samples)
        if stdev <= 0:
            continue
        t = statistics.fmean(samples) / (stdev / math.sqrt(len(samples)))
        profile.append((hour, len(samples), t))
    return profile


def bollinger_walk_forward(bars: Sequence[dict[str, Any]], *, period: int = 20,
                           bands: float = 2.0, costs_bps_per_side: float = 5.0) -> dict[str, Any]:
    """Chained, never-fitted mean reversion: fade a band touch on a closed bar, exit on the
    middle band or after ``2 * period`` bars, paying ``costs_bps_per_side`` on both legs."""
    cost = costs_bps_per_side / 10_000.0
    closes = closes_of(bars)
    trades: list[float] = []
    equity, peak, max_drawdown = 1.0, 1.0, 0.0
    side, entry_index = 0.0, -1
    for index in range(period + 1, len(closes)):
        window = closes[index - period:index]
        middle = statistics.fmean(window)
        stdev = statistics.pstdev(window)
        if stdev <= 0:
            continue
        price = closes[index]
        if side == 0.0:
            if price < middle - bands * stdev:
                side, entry_index = 1.0, index
            elif price > middle + bands * stdev:
                side, entry_index = -1.0, index
            continue
        touched_middle = price >= middle if side > 0 else price <= middle
        if touched_middle or index - entry_index >= 2 * period:
            net = side * (price / closes[entry_index] - 1.0) - 2 * cost
            trades.append(net)
            equity *= 1.0 + net
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
            side = 0.0
    return summarise(trades, equity, max_drawdown, len(bars))


def sma_trend_walk_forward(bars: Sequence[dict[str, Any]], *, fast: int = 5, slow: int = 20,
                           costs_bps_per_side: float = 5.0) -> dict[str, Any]:
    """The family the agent's strategy module uses, measured on the same bars and costs."""
    cost = costs_bps_per_side / 10_000.0
    closes = closes_of(bars)
    trades: list[float] = []
    equity, peak, max_drawdown = 1.0, 1.0, 0.0
    side, entry_price = 0.0, None
    for index in range(slow + 1, len(closes)):
        fast_mean = statistics.fmean(closes[index - fast:index])
        slow_mean = statistics.fmean(closes[index - slow:index])
        wanted = 1.0 if fast_mean > slow_mean else -1.0
        if wanted == side:
            continue
        if side and entry_price:
            net = side * (closes[index] / entry_price - 1.0) - 2 * cost
            trades.append(net)
            equity *= 1.0 + net
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
        side, entry_price = wanted, closes[index]
    return summarise(trades, equity, max_drawdown, len(bars))


def summarise(trades: Sequence[float], equity: float, max_drawdown: float,
              bars: int) -> dict[str, Any]:
    if not trades:
        return {"trades": 0, "win_rate": None, "mean_net_bps": None,
                "chained_return_pct": 0.0, "max_drawdown_pct": 0.0, "bars": bars}
    wins = sum(1 for trade in trades if trade > 0)
    return {"trades": len(trades), "win_rate": wins / len(trades),
            "mean_net_bps": statistics.fmean(trades) * 10_000.0,
            "chained_return_pct": (equity - 1.0) * 100.0,
            "max_drawdown_pct": max_drawdown * 100.0, "bars": bars}


def _scale(value: float, low: float, high: float, floor: float, ceiling: float) -> float:
    if high == low:
        return (floor + ceiling) / 2.0
    return ceiling - (value - low) / (high - low) * (ceiling - floor)


def _frame(width: float, height: float, title: str, subtitle: str) -> list[str]:
    parts = [SVG_HEADER.format(width=int(width), height=int(height), title=_escape(title)),
             f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
             f'<text x="{PLOT_LEFT}" y="20" font-family="sans-serif" font-size="14" '
             f'fill="#1a1f29">{_escape(title)}</text>',
             f'<text x="{PLOT_LEFT}" y="{height - 12}" font-family="sans-serif" font-size="11" '
             f'fill="{AXIS_COLOR}">{_escape(subtitle)}</text>']
    return parts


def _legend(parts: list[str], labels: Sequence[str], x: float, y: float) -> None:
    for offset, label in enumerate(labels):
        left = x + offset * 190.0
        parts.append(f'<rect x="{left}" y="{y - 8}" width="11" height="11" '
                     f'fill="{PALETTE[offset % len(PALETTE)]}"/>')
        parts.append(f'<text x="{left + 16}" y="{y}" font-family="sans-serif" font-size="11" '
                     f'fill="#1a1f29">{_escape(label)}</text>')


def line_chart(*, title: str, subtitle: str, series: Sequence[tuple[str, Sequence[tuple[float, float]]]],
               width: float = 940.0, height: float = 380.0,
               y_format: str = "plain", y_label: str = "") -> str:
    points = [pair for _, values in series for pair in values]
    if not points:
        raise ValueError("line_chart needs at least one point")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_low, x_high, y_low, y_high = min(xs), max(xs), min(ys), max(ys)
    pad = (y_high - y_low) * 0.06 or abs(y_high) * 0.06 or 1.0
    y_low, y_high = y_low - pad, y_high + pad
    plot_w = width - PLOT_LEFT - PLOT_RIGHT
    plot_h = height - PLOT_TOP - PLOT_BOTTOM
    parts = _frame(width, height, title, subtitle)
    for step in range(0, 5):
        value = y_low + (y_high - y_low) * step / 4.0
        y = _scale(value, y_low, y_high, PLOT_TOP + plot_h, PLOT_TOP)
        parts.append(f'<line x1="{PLOT_LEFT}" y1="{y:.1f}" x2="{PLOT_LEFT + plot_w}" '
                     f'y2="{y:.1f}" stroke="{GRID_COLOR}" stroke-width="1"/>')
        label = f"{value:,.0f}" if y_format == "plain" else f"{value:.1f}"
        parts.append(f'<text x="{PLOT_LEFT - 8}" y="{y + 4:.1f}" text-anchor="end" '
                     f'font-family="sans-serif" font-size="10" fill="{AXIS_COLOR}">{label}</text>')
    if y_label:
        parts.append(f'<text x="18" y="{PLOT_TOP - 12}" font-family="sans-serif" font-size="11" '
                     f'fill="{AXIS_COLOR}">{_escape(y_label)}</text>')
    if y_low < 0 < y_high:
        zero = _scale(0.0, y_low, y_high, PLOT_TOP + plot_h, PLOT_TOP)
        parts.append(f'<line x1="{PLOT_LEFT}" y1="{zero:.1f}" x2="{PLOT_LEFT + plot_w}" '
                     f'y2="{zero:.1f}" stroke="{AXIS_COLOR}" stroke-width="1.5" '
                     f'stroke-dasharray="4 3"/>')
    for index, (label, values) in enumerate(series):
        color = PALETTE[index % len(PALETTE)]
        path = []
        for x_value, y_value in values:
            px = PLOT_LEFT + (x_value - x_low) / ((x_high - x_low) or 1.0) * plot_w
            py = _scale(y_value, y_low, y_high, PLOT_TOP + plot_h, PLOT_TOP)
            path.append(f"{'M' if not path else 'L'}{px:.1f},{py:.1f}")
        parts.append(f'<path d="{" ".join(path)}" fill="none" stroke="{color}" stroke-width="1.8"/>')
    _legend(parts, [label for label, _ in series], PLOT_LEFT, PLOT_TOP - 12)
    parts.append("</svg>")
    return "\n".join(parts)


def bar_chart(*, title: str, subtitle: str, groups: Sequence[tuple[str, Sequence[tuple[str, float]]]],
              category_labels: Sequence[str], width: float = 940.0, height: float = 360.0,
              y_label: str = "") -> str:
    values = [pair for _, bars in groups for pair in bars]
    if not values:
        raise ValueError("bar_chart needs at least one bar")
    low = min(min(v for _, v in bars) for _, bars in groups)
    high = max(max(v for _, v in bars) for _, bars in groups)
    pad = (high - low) * 0.08 or abs(high) * 0.08 or 1.0
    low, high = min(low - pad, 0.0), high + pad
    plot_w = width - PLOT_LEFT - PLOT_RIGHT
    plot_h = height - PLOT_TOP - PLOT_BOTTOM
    parts = _frame(width, height, title, subtitle)
    for step in range(0, 5):
        value = low + (high - low) * step / 4.0
        y = _scale(value, low, high, PLOT_TOP + plot_h, PLOT_TOP)
        parts.append(f'<line x1="{PLOT_LEFT}" y1="{y:.1f}" x2="{PLOT_LEFT + plot_w}" '
                     f'y2="{y:.1f}" stroke="{GRID_COLOR}" stroke-width="1"/>')
        parts.append(f'<text x="{PLOT_LEFT - 8}" y="{y + 4:.1f}" text-anchor="end" '
                     f'font-family="sans-serif" font-size="10" fill="{AXIS_COLOR}">{value:,.1f}</text>')
    slot = plot_w / max(len(category_labels), 1)
    bar_w = max(min(slot * 0.8 / max(len(groups), 1), 26.0), 3.0)
    zero_y = _scale(0.0, low, high, PLOT_TOP + plot_h, PLOT_TOP)
    for group_index, (label, bars) in enumerate(groups):
        color = PALETTE[group_index % len(PALETTE)]
        for bar_index, (category, value) in enumerate(bars):
            if category_index(category, category_labels) < 0:
                continue
            centre = PLOT_LEFT + (category_index(category, category_labels) + 0.5) * slot
            offset = (group_index - (len(groups) - 1) / 2.0) * (bar_w + 1.0)
            x = centre + offset - bar_w / 2.0
            y = _scale(value, low, high, PLOT_TOP + plot_h, PLOT_TOP)
            top, bottom = min(y, zero_y), max(y, zero_y)
            parts.append(f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w:.1f}" '
                         f'height="{max(bottom - top, 0.6):.1f}" fill="{color}"/>')
    for i, category in enumerate(category_labels):
        x = PLOT_LEFT + (i + 0.5) * slot
        parts.append(f'<text x="{x:.1f}" y="{PLOT_TOP + plot_h + 16}" text-anchor="middle" '
                     f'font-family="sans-serif" font-size="9" fill="{AXIS_COLOR}">'
                     f'{_escape(category)}</text>')
    if y_label:
        parts.append(f'<text x="18" y="{PLOT_TOP - 12}" font-family="sans-serif" font-size="11" '
                     f'fill="{AXIS_COLOR}">{_escape(y_label)}</text>')
    _legend(parts, [label for label, _ in groups], PLOT_LEFT, PLOT_TOP - 12)
    parts.append("</svg>")
    return "\n".join(parts)


def category_index(category: str, category_labels: Sequence[str]) -> int:
    try:
        return list(category_labels).index(category)
    except ValueError:
        return -1


def fetch_real_bars(symbol: str, *, days: int) -> list[dict[str, Any]]:
    """Real H1 history from the running terminal. Market data only; no trading call."""
    import MetaTrader5 as mt5

    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"MT5 symbol_select failed for {symbol}: {mt5.last_error()}")
    end = datetime.now(UTC)
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_H1, end - timedelta(days=days), end)
    if rates is None:
        raise RuntimeError(f"MT5 returned no history for {symbol}: {mt5.last_error()}")
    return [{"time": int(row["time"]), "open": float(row["open"]), "high": float(row["high"]),
             "low": float(row["low"]), "close": float(row["close"])} for row in rates]


def read_symbol_spec(policy_path: Path) -> tuple[float, float, float | None]:
    """Return (tick_size, tick_value, contract_size) from a policy YAML.

    contract_size is None when the policy predates the field or leaves it non-positive, which is
    the same "unknown" reading ``SymbolSpec.from_terminal`` uses; the tick ratio is then the only
    estimate available.
    """
    import yaml

    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    spec = raw["symbol_spec"]
    contract_size = None
    if isinstance(spec, dict):
        value = spec.get("contract_size")
        if not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0:
            contract_size = float(value)
    return float(spec["tick_size"]), float(spec["tick_value"]), contract_size


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", action="append", default=[], metavar="LABEL=POLICY=DB",
                        help="one paper demo: label, its policy YAML for the tick spec, and journal DB")
    parser.add_argument("--research-symbols", default="",
                        help="comma-separated symbols to measure on real broker bars")
    parser.add_argument("--history-days", type=int, default=365)
    parser.add_argument("--cost-steps-bps", default=",".join(str(step) for step in
                                                             DEFAULT_COST_STEPS_BPS))
    parser.add_argument("--out-dir", default="docs/charts")
    parser.add_argument("--evidence-json", default="")
    return parser


def parse_journal_argument(argument: str) -> tuple[str, Path, Path]:
    label, _, rest = argument.partition("=")
    policy, _, db = rest.partition("=")
    if not label or not policy or not db:
        raise ValueError(f"--journal expects LABEL=POLICY=DB, got {argument!r}")
    return label, Path(policy), Path(db)


def render_demo_journals(specs: Sequence[tuple[str, Path, Path]], out_dir: Path,
                         evidence: dict[str, Any]) -> list[str]:
    written: list[str] = []
    curves: list[tuple[str, list[tuple[float, float]]]] = []
    r_bars: list[tuple[str, list[tuple[str, float]]]] = []
    r_labels: list[str] = []
    for label, policy_path, db_path in specs:
        tick_size, tick_value, contract_size = read_symbol_spec(policy_path)
        events = load_journal_events(db_path)
        # The basis the run itself booked at, when its cycles carried a proposal and a sizing;
        # a policy file is a description of the contract, not a record of this journal.
        declared = policy_cash_per_price_unit(tick_size, tick_value, contract_size)
        recorded = recorded_cash_per_price_unit(events)
        if recorded is None:
            cash, risk_basis = declared, ("policy_contract_size" if contract_size is not None
                                          else "policy_tick_ratio")
        else:
            cash, risk_basis = recorded, "journal_sizing"
        equity = equity_series(events)
        if not equity:
            raise ValueError(f"{db_path}: journal has no equity_point events, so nothing to plot")
        origin = equity[0][0].timestamp()
        curves.append((label, [((at.timestamp() - origin) / 86_400.0, value)
                               for at, value in equity]))
        multiples = r_multiples(events, cash_per_price_unit=cash)
        keys = [f"{i + 1}" for i in range(len(multiples))]
        r_bars.append((label, list(zip(keys, [value for _, value in multiples]))))
        r_labels = keys if len(keys) > len(r_labels) else r_labels
        closed = [event for event in events if event.get("event_type") == "paper_trade_closed"]
        pnl_total = sum(float(event.get("pnl", 0.0)) for event in closed)
        wins = sum(1 for event in closed if float(event.get("pnl", 0.0)) > 0)
        evidence.setdefault("demos", {})[label] = {
            "journal": str(db_path),
            "policy": str(policy_path),
            "tick_size": tick_size,
            "tick_value": tick_value,
            "contract_size": contract_size,
            "risk_cash_per_price_unit": round(cash, 6),
            "risk_basis": risk_basis,
            "cycles": sum(1 for event in events if event.get("event_type") == "agent_cycle"),
            "closed_trades": len(closed),
            "winning_trades": wins,
            "win_rate": (wins / len(closed)) if closed else None,
            "realized_pnl_account_currency": round(pnl_total, 4),
            "first_equity": equity[0][1],
            "final_equity": equity[-1][1],
            "max_equity": max(value for _, value in equity),
            "min_equity": min(value for _, value in equity),
            "r_multiples": [round(value, 4) for _, value in multiples],
            "planned_risk_currency_per_trade": [
                round(risk, 4) for risk in (
                    planned_risk_currency(event, cash_per_price_unit=cash)
                    for event in closed if event.get("pnl") is not None) if risk is not None],
            "broker_order_sent_anywhere": any(bool(event.get("broker_order_sent"))
                                              for event in events),
        }
    if curves:
        svg = line_chart(title="Equity over the paper demo, account currency",
                         subtitle="Paper fills on real broker bars. No order reached a broker.",
                         series=[(label, [(x, y) for x, y in values]) for label, values in curves],
                         y_label="equity")
        written.append(_write(out_dir / "demo_equity_curves.svg", svg))
    if r_bars and r_labels:
        svg = bar_chart(title="Realised R multiple per closed paper trade",
                        subtitle="R = P/L divided by the risk that trade was sized for. "
                                 "Stop at -1R, target at +2R.",
                        groups=r_bars, category_labels=r_labels, y_label="R")
        written.append(_write(out_dir / "demo_trade_r_multiples.svg", svg))
    return written


def render_research(symbols: Sequence[str], *, days: int, cost_steps: Sequence[float],
                    out_dir: Path, evidence: dict[str, Any]) -> list[str]:
    import MetaTrader5 as mt5

    written: list[str] = []
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    sweep: list[tuple[str, list[tuple[float, float]]]] = []
    hourly: list[tuple[str, list[tuple[str, float]]]] = []
    hour_labels: list[str] = []
    try:
        for symbol in symbols:
            bars = fetch_real_bars(symbol, days=days)
            closes = closes_of(bars)
            rets = percent_returns(closes)
            profile = hourly_t_statistics(bars)
            entry = evidence.setdefault("research", {}).setdefault(symbol, {
                "bars": len(bars),
                "first_bar_utc": datetime.fromtimestamp(bars[0]["time"], UTC).isoformat(),
                "last_bar_utc": datetime.fromtimestamp(bars[-1]["time"], UTC).isoformat(),
                "lag1_autocorrelation_h1_returns": round(lag1_autocorrelation(rets), 6),
                "hourly_t_statistics": {f"{hour:02d}": round(t, 3) for hour, _, t in profile},
                "cost_sweep": {},
            })
            for cost in cost_steps:
                key = f"{cost:g}"
                bollinger = bollinger_walk_forward(bars, costs_bps_per_side=cost)
                trend = sma_trend_walk_forward(bars, costs_bps_per_side=cost)
                entry["cost_sweep"][key] = {
                    "BOLL(20,2.0)": bollinger, "SMA(5/20)": trend}
            sweep.append((f"{symbol} Bollinger(20,2.0)",
                          [(cost, entry["cost_sweep"][f"{cost:g}"]["BOLL(20,2.0)"]["chained_return_pct"])
                           for cost in cost_steps]))
            sweep.append((f"{symbol} SMA(5/20)",
                          [(cost, entry["cost_sweep"][f"{cost:g}"]["SMA(5/20)"]["chained_return_pct"])
                           for cost in cost_steps]))
            hour_labels = [f"{hour:02d}" for hour, _, _ in profile] or hour_labels
            if profile:
                hourly.append((symbol, [(f"{hour:02d}", t) for hour, _, t in profile]))
    finally:
        mt5.shutdown()
    if sweep:
        svg = line_chart(title="Chained equity return vs cost per side, real H1 bars",
                         subtitle="One year of broker history per symbol, walk-forward, "
                                  "nothing fitted to the test window.",
                         series=sweep, y_format="decimal", y_label="chained return, %")
        written.append(_write(out_dir / "real_bar_cost_sweep.svg", svg))
    if hourly and hour_labels:
        svg = bar_chart(title="Hour-of-day t-statistic of the next H1 return",
                        subtitle="Mean hourly return over its standard error, same bars.",
                        groups=hourly, category_labels=hour_labels, y_label="t")
        written.append(_write(out_dir / "real_bar_hourly_tstats.svg", svg))
    return written


def _write(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out_dir)
    cost_steps = [float(item) for item in args.cost_steps_bps.split(",") if item.strip()]
    evidence: dict[str, Any] = {"generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                                "broker_order_sent_note": "every plotted fill is a paper fill"}
    written: list[str] = []
    specs = [parse_journal_argument(argument) for argument in args.journal]
    if specs:
        written.extend(render_demo_journals(specs, out_dir, evidence))
    symbols = [item.strip() for item in args.research_symbols.split(",") if item.strip()]
    if symbols:
        written.extend(render_research(symbols, days=args.history_days, cost_steps=cost_steps,
                                       out_dir=out_dir, evidence=evidence))
    if not written:
        print("nothing to render: pass --journal and/or --research-symbols")
        return 2
    if args.evidence_json:
        Path(args.evidence_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.evidence_json).write_text(json.dumps(evidence, indent=2, sort_keys=True),
                                           encoding="utf-8")
    for label, numbers in sorted(evidence.get("demos", {}).items()):
        print(f"{label}: trades={numbers['closed_trades']} "
              f"wins={numbers['winning_trades']} realized={numbers['realized_pnl_account_currency']:.2f} "
              f"equity={numbers['first_equity']:.2f} -> {numbers['final_equity']:.2f} "
              f"broker_order_sent={numbers['broker_order_sent_anywhere']}")
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
