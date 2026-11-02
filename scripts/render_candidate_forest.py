"""Draw the recorded development run as a forest plot, straight from the evidence JSON.

Nothing here recomputes or re-tunes a statistic: every number printed is read out of
``docs/evidence/candidates_dev_2026_11.json``, which the pre-registered study wrote. Re-running
the script over the same file reproduces the same bytes.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

SVG_HEADER = ('<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
              'viewBox="0 0 {width} {height}" role="img" aria-label="{title}">')
PLOT_LEFT = 250.0
PLOT_RIGHT = 300.0
PLOT_TOP = 84.0
ROW_HEIGHT = 62.0
AXIS_COLOR = "#5b6470"
GRID_COLOR = "#d7dce2"
ZERO_COLOR = "#111318"
INTERVAL_COLOR = "#1f77b4"
POINT_COLOR = "#d62728"
FAIL_COLOR = "#8c564b"


def _escape(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _fmt(value: float) -> str:
    return f"{value:+.4f}"


def _status_lines(row: dict[str, Any]) -> list[str]:
    lines = [row["status"].upper()]
    if row["double_cost"] is not None:
        lines.append(f"at double cost {_fmt(row['double_cost'])} R")
    if row["detectable_r"] is not None:
        lines.append(f"detectable here: {row['detectable_r']:.3f} R")
    return lines


def candidate_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidates in file order, carrying only the fields the plot needs."""
    rows = []
    for record in payload["candidates"].values():
        sweep = record.get("cost_sweep") or {}
        decision = record.get("decision") or {}
        double_mean = decision.get("double_cost_mean")
        if double_mean is None and "double" in sweep:
            double_mean = sweep["double"]["mean"]
        rows.append({
            "name": str(record["name"]),
            "n": int(record["n"]),
            "clusters": int(record["clusters"]),
            "mean": float(record["mean"]),
            "low": float(record["low"]),
            "high": float(record["high"]),
            "double_cost": None if double_mean is None else float(double_mean),
            "detectable_r": None if decision.get("detectable_r") is None
            else float(decision["detectable_r"]),
            "status": str(decision.get("status", "unknown")),
        })
    return rows


def _ticks(lo: float, hi: float) -> list[float]:
    step = 0.05
    first = math.ceil(lo / step) * step
    values = []
    value = first
    while value <= hi + 1e-12:
        values.append(round(value, 6))
        value += step
    return values


def render_svg(payload: dict[str, Any]) -> str:
    rows = candidate_rows(payload)
    if not rows:
        raise ValueError("evidence file lists no candidates")

    lo = min(r["low"] for r in rows)
    hi = max(r["high"] for r in rows)
    pad = (hi - lo) * 0.08 or 0.05
    lo, hi = lo - pad, hi + pad

    width = 1010
    height = int(PLOT_TOP + ROW_HEIGHT * len(rows) + 58)
    plot_right = width - PLOT_RIGHT
    baseline = height - 58.0

    def x(value: float) -> float:
        return PLOT_LEFT + (value - lo) / (hi - lo) * (plot_right - PLOT_LEFT)

    parts = [SVG_HEADER.format(width=width, height=height,
                               title="Net R per trade with bootstrap interval, development window")]
    window_name = str(payload.get("window", ""))
    span = (payload.get("windows") or {}).get(window_name) or ["?", "?"]
    subtitle = (f"{window_name} window {span[0]} .. {span[1]}   "
                f"confidence {float(payload.get('confidence', 0.0)):.4f}   "
                f"{int(payload.get('bootstrap_rounds', 0))} bootstrap rounds   "
                f"seed {payload.get('seed')}   "
                f"cache {str(payload.get('cache_manifest_sha256'))[:12]}")
    parts.append(f'<rect width="{width}" height="{height}" fill="#ffffff"/>')
    parts.append(f'<text x="16" y="26" font-family="sans-serif" font-size="15" fill="{ZERO_COLOR}">'
                 f'Recorded development run &#8212; net R per trade</text>')
    parts.append(f'<text x="16" y="44" font-family="sans-serif" font-size="11" fill="{AXIS_COLOR}">'
                 f'{_escape(subtitle)}</text>')

    for value in _ticks(lo, hi):
        parts.append(f'<line x1="{x(value):.2f}" y1="{PLOT_TOP - 10:.2f}" x2="{x(value):.2f}" '
                     f'y2="{baseline:.2f}" stroke="{GRID_COLOR}" stroke-width="1"/>')
        parts.append(f'<text x="{x(value):.2f}" y="{baseline + 16:.2f}" text-anchor="middle" '
                     f'font-family="sans-serif" font-size="10" fill="{AXIS_COLOR}">'
                     f'{"0.00" if abs(value) < 1e-12 else f"{value:+.2f}"}</text>')

    zero_x = x(0.0)
    parts.append(f'<line x1="{zero_x:.2f}" y1="{PLOT_TOP - 14:.2f}" x2="{zero_x:.2f}" '
                 f'y2="{baseline + 4:.2f}" stroke="{ZERO_COLOR}" stroke-width="1.5"/>')
    parts.append(f'<text x="{zero_x:.2f}" y="{PLOT_TOP - 20:.2f}" text-anchor="middle" '
                 f'font-family="sans-serif" font-size="10" fill="{ZERO_COLOR}">no edge</text>')

    for index, row in enumerate(rows):
        cy = PLOT_TOP + ROW_HEIGHT * index + ROW_HEIGHT / 2
        parts.append(f'<text x="16" y="{cy - 2:.2f}" font-family="monospace" font-size="12" '
                     f'fill="{ZERO_COLOR}">{_escape(row["name"])}</text>')
        parts.append(f'<text x="16" y="{cy + 14:.2f}" font-family="sans-serif" font-size="10" '
                     f'fill="{AXIS_COLOR}">{row["n"]} trades / {row["clusters"]} clusters</text>')
        parts.append(f'<line x1="{x(row["low"]):.2f}" y1="{cy:.2f}" x2="{x(row["high"]):.2f}" '
                     f'y2="{cy:.2f}" stroke="{INTERVAL_COLOR}" stroke-width="2.5"/>')
        for edge in (row["low"], row["high"]):
            parts.append(f'<line x1="{x(edge):.2f}" y1="{cy - 6:.2f}" x2="{x(edge):.2f}" '
                         f'y2="{cy + 6:.2f}" stroke="{INTERVAL_COLOR}" stroke-width="2.5"/>')
        parts.append(f'<rect x="{x(row["mean"]) - 4.5:.2f}" y="{cy - 4.5:.2f}" width="9" height="9" '
                     f'fill="{POINT_COLOR}"/>')
        right_x = plot_right + 14
        parts.append(f'<text x="{right_x:.2f}" y="{cy - 10:.2f}" font-family="monospace" font-size="12" '
                     f'fill="{POINT_COLOR}">{_escape(_fmt(row["mean"]) + " R net")}</text>')
        colour = ZERO_COLOR if row["status"] == "pass" else FAIL_COLOR
        for offset, line in enumerate(_status_lines(row)):
            parts.append(f'<text x="{right_x:.2f}" y="{cy + 6 + 13 * offset:.2f}" '
                         f'font-family="sans-serif" font-size="10" '
                         f'fill="{colour if offset == 0 else AXIS_COLOR}">{_escape(line)}</text>')

    parts.append(f'<text x="{PLOT_LEFT:.2f}" y="{height - 12:.2f}" font-family="sans-serif" '
                 f'font-size="10" fill="{AXIS_COLOR}">net R per trade, cluster bootstrap interval; '
                 f'an interval that covers zero is not evidence of an edge</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", default="docs/evidence/candidates_dev_2026_11.json",
                        type=Path)
    parser.add_argument("--out", default="docs/charts/candidates_dev_forest.svg", type=Path)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    payload = json.loads(args.evidence.read_text(encoding="utf-8"))
    svg = render_svg(payload)
    if args.stdout:
        print(svg)
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(svg, encoding="utf-8")
    print(f"wrote {args.out} from {args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
