"""CLI for the paper-betting sandbox (demo money only)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from critical_consensus.betting_sandbox.api import create_app
from critical_consensus.betting_sandbox.data import (DatasetError, download_dataset,
                                                     load_cached_dataset)
from critical_consensus.betting_sandbox.evaluate import summarize, walk_forward
from critical_consensus.betting_sandbox.ledger import PaperWallet
from critical_consensus.betting_sandbox.staking import StakingPlan

DISCLAIMER = ("Mô phỏng với tiền demo; không phải tiền thật và không bảo đảm lợi nhuận.")


def _dataset(args, *, offline: bool):
    cache_dir = Path(args.cache_dir)
    if offline:
        dataset = load_cached_dataset(cache_dir, args.season, args.division)
        if dataset is None:
            raise SystemExit(f"Không có dữ liệu cache cho {args.season}/{args.division}.")
        return dataset
    return download_dataset(cache_dir, args.season, args.division, refresh=args.refresh)


def _fmt(value: int) -> str:
    return f"{value:,}".replace(",", ".") + " ₫"


def cmd_download(args) -> int:
    try:
        dataset = download_dataset(Path(args.cache_dir), args.season, args.division,
                                   refresh=args.refresh)
    except (DatasetError, ValueError) as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        return 2
    print(f"Đã nạp {len(dataset.matches)} trận {args.season}/{args.division} "
          f"(bỏ qua {dataset.skipped_rows} dòng).")
    print(f"sha256: {dataset.sha256}")
    return 0


def cmd_backtest(args) -> int:
    try:
        dataset = _dataset(args, offline=args.offline)
    except (DatasetError, ValueError) as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        return 2
    plan = StakingPlan(mode=args.mode, flat_fraction=args.flat_fraction,
                       kelly_multiplier=args.kelly_fraction,
                       max_fraction=args.max_fraction, min_edge=args.min_edge,
                       min_odds=args.min_odds, max_odds=args.max_odds)
    result = walk_forward(dataset.matches, initial_bankroll_units=args.bankroll, plan=plan,
                          warmup_matches=args.warmup, half_life_days=args.half_life)
    summary = summarize(result.bets, initial_bankroll_units=result.initial_bankroll_units,
                        bankroll_curve=result.bankroll_curve)
    print(f"Trận xét: {result.matches_considered} · cược: {summary['bets']} · "
          f"chế độ: {args.mode}")
    print(f"Tổng cược: {_fmt(int(summary['staked_units']))} · "
          f"nhận về: {_fmt(int(summary['returned_units']))} · "
          f"lợi nhuận: {_fmt(int(summary['profit_units']))}")
    print(f"ROI: {summary['roi'] * 100:.2f}% · thắng: {summary['hit_rate'] * 100:.1f}% "
          f"(95% CI {summary['hit_rate_ci95'][0] * 100:.1f}–{summary['hit_rate_ci95'][1] * 100:.1f}%)")
    print(f"Số dư cuối: {_fmt(result.final_bankroll_units)} · "
          f"drawdown tối đa: {_fmt(int(summary['max_drawdown_units']))}")
    if summary["mean_clv"] is not None:
        print(f"CLV trung bình: {summary['mean_clv'] * 100:.2f}% "
              f"({summary['clv_samples']} mẫu)")
    print(DISCLAIMER)
    if args.output:
        payload = {"summary": summary, "mode": args.mode,
                   "season": args.season, "division": args.division,
                   "bets": [record.__dict__ | {"match_date": record.match_date.isoformat()}
                            for record in result.bets]}
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        print(f"Đã ghi báo cáo: {args.output}")
    return 0


def cmd_wallet(args) -> int:
    wallet = PaperWallet(args.db_path, initial_balance_units=args.bankroll)
    stats = wallet.stats()
    print(f"Số dư: {_fmt(int(stats['balance_units']))} · cược: {stats['bets']} "
          f"(chờ {stats['pending']}) · ROI: {stats['roi'] * 100:.2f}%")
    for bet in wallet.list_bets(limit=20):
        print(f"  {bet['match_date']} {bet['home']}–{bet['away']} "
              f"{bet['selection']} {bet['odds']:.2f} x {_fmt(int(bet['stake_units']))} "
              f"→ {bet['status']}")
    return 0


def cmd_reset(args) -> int:
    if not args.confirm:
        print("Thêm --confirm để đặt lại ví demo.", file=sys.stderr)
        return 2
    wallet = PaperWallet(args.db_path, initial_balance_units=args.bankroll)
    stats = wallet.reset(args.bankroll)
    print(f"Đã đặt lại ví demo: {_fmt(int(stats['balance_units']))}.")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    app = create_app(db_path=args.db_path, cache_dir=args.cache_dir,
                     initial_balance=args.bankroll, season=args.season,
                     division=args.division,
                     access_token=os.environ.get("SANDBOX_ACCESS_TOKEN"))
    print(f"Sân chơi demo: http://{args.host}:{args.port}")
    print(DISCLAIMER)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sân chơi cá cược demo (tiền demo).")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db-path", default="sandbox_data/betting.db")
    common.add_argument("--cache-dir", default="sandbox_data/cache")
    common.add_argument("--season", default="2425")
    common.add_argument("--division", default="E0")
    common.add_argument("--bankroll", type=int, default=1_000_000)
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download", parents=[common], help="Tải dữ liệu lịch sử thật")
    download.add_argument("--refresh", action="store_true")
    download.set_defaults(func=cmd_download)

    backtest = sub.add_parser("backtest", parents=[common],
                              help="Chạy walk-forward trên dữ liệu thật")
    backtest.add_argument("--mode", choices=["flat", "kelly"], default="flat")
    backtest.add_argument("--flat-fraction", type=float, default=0.01)
    backtest.add_argument("--kelly-fraction", type=float, default=0.25)
    backtest.add_argument("--max-fraction", type=float, default=0.02)
    backtest.add_argument("--min-edge", type=float, default=0.02)
    backtest.add_argument("--min-odds", type=float, default=1.5)
    backtest.add_argument("--max-odds", type=float, default=10.0)
    backtest.add_argument("--warmup", type=int, default=80)
    backtest.add_argument("--half-life", type=float, default=180.0)
    backtest.add_argument("--offline", action="store_true",
                          help="Chỉ dùng cache, không tải mạng")
    backtest.add_argument("--refresh", action="store_true")
    backtest.add_argument("--output", help="Ghi báo cáo JSON")
    backtest.set_defaults(func=cmd_backtest)

    wallet = sub.add_parser("wallet", parents=[common], help="Xem ví demo và cược gần đây")
    wallet.set_defaults(func=cmd_wallet)

    reset = sub.add_parser("reset", parents=[common], help="Đặt lại ví demo")
    reset.add_argument("--confirm", action="store_true")
    reset.set_defaults(func=cmd_reset)

    serve = sub.add_parser("serve", parents=[common], help="Chạy giao diện web tiếng Việt")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8010)
    serve.set_defaults(func=cmd_serve)
    return parser


def _setup_console() -> None:
    # Windows consoles default to a legacy code page that cannot encode Vietnamese.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
