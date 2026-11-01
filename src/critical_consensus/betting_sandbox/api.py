"""Paper-betting sandbox API and Vietnamese web UI.

This app is intentionally separate from ``critical_consensus.api``: it has no
order, permission or trading endpoint and it never touches real money.
"""
from __future__ import annotations

import os
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .data import Dataset, DatasetError, Match, download_dataset, load_cached_dataset
from .evaluate import BetRecord, summarize, walk_forward
from .ledger import IdempotencyConflict, InsufficientFunds, LedgerError, PaperWallet
from .model import fit_model
from .pricing import implied_probabilities
from .staking import StakingPlan, decide_stake
from .ui_vi import render_index_page

POSITION = {"HOME": 0, "DRAW": 1, "AWAY": 2}
DEFAULT_PLAN = StakingPlan()


@dataclass
class SandboxState:
    db_path: Path
    cache_dir: Path
    initial_balance: int
    season: str
    division: str
    access_token: str | None
    wallet: PaperWallet
    dataset: Dataset | None = None
    data_error: str | None = None
    _model_cache: dict[date, object] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def match_map(self) -> dict[str, Match]:
        if self.dataset is None:
            return {}
        return {match.match_id: match for match in self.dataset.matches}

    def model_for(self, match: Match):
        with self._lock:
            cached = self._model_cache.get(match.date)
            if cached is not None:
                return cached
            history = [item for item in (self.dataset.matches if self.dataset else [])
                       if item.date < match.date]
            model = fit_model(history, as_of=match.date)
            self._model_cache[match.date] = model
            return model


class BetRequest(BaseModel):
    match_id: str = Field(min_length=1, max_length=200)
    selection: Literal["HOME", "DRAW", "AWAY"]
    stake: int | None = Field(default=None, ge=1, le=1_000_000_000)
    mode: Literal["flat", "kelly"] = "flat"
    idempotency_key: str | None = Field(default=None, max_length=200)


class BacktestRequest(BaseModel):
    mode: Literal["flat", "kelly"] = "flat"
    warmup_matches: int = Field(default=80, ge=0, le=2000)
    half_life_days: float = Field(default=180.0, gt=0, le=3650)


class DownloadRequest(BaseModel):
    season: str | None = Field(default=None, max_length=4)
    division: str | None = Field(default=None, max_length=4)
    refresh: bool = False


class ResetRequest(BaseModel):
    confirm: str = Field(max_length=20)


def create_app(*, db_path: str | Path | None = None, cache_dir: str | Path | None = None,
               initial_balance: int | None = None, season: str | None = None,
               division: str | None = None, access_token: str | None = None,
               matches: list[Match] | None = None) -> FastAPI:
    db_path = Path(db_path or os.environ.get("SANDBOX_DB_PATH", "sandbox_data/betting.db"))
    cache_dir = Path(cache_dir or os.environ.get("SANDBOX_CACHE_DIR", "sandbox_data/cache"))
    initial_balance = initial_balance if initial_balance is not None else int(
        os.environ.get("SANDBOX_INITIAL_BALANCE", "1000000"))
    season = season or os.environ.get("SANDBOX_SEASON", "2425")
    division = division or os.environ.get("SANDBOX_DIVISION", "E0")
    access_token = access_token if access_token is not None else os.environ.get("SANDBOX_ACCESS_TOKEN")

    dataset = None
    data_error = None
    if matches is not None:
        dataset = Dataset(list(matches), 0, division, season, "", "injected", True)
    else:
        try:
            dataset = load_cached_dataset(cache_dir, season, division)
        except (DatasetError, ValueError) as exc:
            data_error = str(exc)

    state = SandboxState(
        db_path=db_path, cache_dir=cache_dir, initial_balance=initial_balance,
        season=season, division=division, access_token=access_token or None,
        wallet=PaperWallet(db_path, initial_balance_units=initial_balance),
        dataset=dataset, data_error=data_error,
    )

    app = FastAPI(
        title="Critical Consensus — Sân chơi cá cược demo",
        description="Chỉ tiền demo. Không có tiền thật, không có nhà cái, không bảo đảm lợi nhuận.",
        version="0.4.0",
    )
    app.state.sandbox = state

    def access_key(x_sandbox_key: str | None = Header(default=None, alias="X-Sandbox-Key")) -> None:
        expected = state.access_token
        if expected and not secrets.compare_digest(x_sandbox_key or "", expected):
            raise HTTPException(status_code=401,
                                detail="Sai hoặc thiếu mã truy cập (X-Sandbox-Key).")

    router = APIRouter(prefix="/api", dependencies=[Depends(access_key)])

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(render_index_page())

    @router.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "demo_only": True,
            "data_loaded": state.dataset is not None,
            "matches": len(state.dataset.matches) if state.dataset else 0,
            "season": state.season,
            "division": state.division,
            "currency": state.wallet.wallet()["currency"],
            "data_error": state.data_error,
            "disclaimer_vi": DISCLAIMER_VI,
        }

    @router.get("/wallet")
    def wallet() -> dict[str, object]:
        return {"wallet": state.wallet.wallet(), "stats": state.wallet.stats()}

    @router.get("/matches")
    def matches(limit: int = Query(default=50, ge=1, le=200),
                offset: int = Query(default=0, ge=0)) -> dict[str, object]:
        if state.dataset is None:
            return {"matches": [], "total": 0, "data_loaded": False,
                    "data_error": state.data_error}
        ordered = sorted(state.dataset.matches,
                         key=lambda item: (item.date, item.match_id), reverse=True)
        page = ordered[offset:offset + limit]
        return {"matches": [_match_payload(state, match) for match in page],
                "total": len(ordered), "data_loaded": True,
                "season": state.season, "division": state.division}

    @router.post("/bets")
    def place_bet(request: BetRequest) -> dict[str, object]:
        match = state.match_map.get(request.match_id)
        if match is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy trận đấu.")
        position = POSITION[request.selection]
        prediction = state.model_for(match).predict(match.home, match.away)
        model_probabilities = (prediction.p_home, prediction.p_draw, prediction.p_away)
        market_probabilities = implied_probabilities([float(value) for value in match.odds])
        odds = float(match.odds[position])
        wallet = state.wallet.wallet()
        balance = int(wallet["balance_units"])
        plan = StakingPlan(mode=request.mode)
        decision = decide_stake(plan, model_probabilities[position], odds, balance)
        if decision.reason != "OK":
            raise HTTPException(status_code=409, detail={
                "code": decision.reason,
                "message_vi": _reason_vi(decision.reason),
                "edge": decision.edge,
            })
        allowed = min(decision.stake_units, balance)
        stake = min(request.stake, allowed) if request.stake is not None else allowed
        if stake < 1:
            raise HTTPException(status_code=409, detail={
                "code": "STAKE_BELOW_ONE_UNIT",
                "message_vi": _reason_vi("STAKE_BELOW_ONE_UNIT"),
            })
        key = request.idempotency_key or uuid.uuid4().hex
        try:
            bet = state.wallet.place_bet(
                match_id=match.match_id, match_date=match.date.isoformat(),
                division=match.division, home=match.home, away=match.away,
                selection=request.selection, odds=odds, stake_units=stake,
                model_probability=model_probabilities[position],
                market_probability=market_probabilities[position], idempotency_key=key,
            )
        except InsufficientFunds as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, LedgerError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"bet": bet, "wallet": state.wallet.wallet(),
                "stake_clamped": request.stake is not None and request.stake > allowed,
                "allowed_stake": allowed}

    @router.post("/matches/{match_id}/settle")
    def settle(match_id: str) -> dict[str, object]:
        match = state.match_map.get(match_id)
        if match is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy trận đấu.")
        settled = state.wallet.settle_match(match_id, match.result)
        return {"match_id": match_id, "result": match.result, "settled": settled,
                "wallet": state.wallet.wallet(), "stats": state.wallet.stats()}

    @router.get("/bets")
    def bets(limit: int = Query(default=100, ge=1, le=1000),
             status: str | None = Query(default=None)) -> dict[str, object]:
        try:
            rows = state.wallet.list_bets(limit=limit, status=status)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"bets": rows}

    @router.get("/report")
    def report() -> dict[str, object]:
        stats = state.wallet.stats()
        rows = state.wallet.list_bets(limit=1000)
        match_map = state.match_map
        records: list[BetRecord] = []
        for row in rows:
            if row["status"] not in ("WON", "LOST"):
                continue
            match = match_map.get(str(row["match_id"]))
            closing = None
            if match is not None and match.closing_odds is not None:
                closing = float(match.closing_odds[POSITION[str(row["selection"])]])
            odds = float(row["odds"])
            records.append(BetRecord(
                match_id=str(row["match_id"]), match_date=date.fromisoformat(str(row["match_date"])),
                division=str(row["division"]), home=str(row["home"]), away=str(row["away"]),
                selection=str(row["selection"]), odds=odds, closing_odds=closing,
                stake_units=int(row["stake_units"]),
                model_probability=float(row["model_probability"]),
                market_probability=float(row["market_probability"]), edge=float(row["edge"]),
                won=row["status"] == "WON", payout_units=int(row["payout_units"]),
                clv=(odds / closing - 1.0) if closing else None,
            ))
        curve = [state.initial_balance] + state.wallet.balance_curve()
        metrics = summarize(records, initial_bankroll_units=state.initial_balance,
                            bankroll_curve=curve)
        return {"wallet": state.wallet.wallet(), "stats": stats, "metrics": metrics,
                "disclaimer_vi": DISCLAIMER_VI}

    @router.post("/backtest")
    def backtest(request: BacktestRequest) -> dict[str, object]:
        if state.dataset is None:
            raise HTTPException(status_code=409, detail="Chưa có dữ liệu. Hãy tải dữ liệu trước.")
        plan = StakingPlan(mode=request.mode)
        result = walk_forward(state.dataset.matches,
                              initial_bankroll_units=state.initial_balance, plan=plan,
                              warmup_matches=request.warmup_matches,
                              half_life_days=request.half_life_days)
        summary = summarize(result.bets, initial_bankroll_units=result.initial_bankroll_units,
                            bankroll_curve=result.bankroll_curve)
        sample = [{
            "match_date": record.match_date.isoformat(), "home": record.home,
            "away": record.away, "selection": record.selection, "odds": record.odds,
            "stake_units": record.stake_units, "model_probability": record.model_probability,
            "market_probability": record.market_probability, "edge": record.edge,
            "won": record.won, "payout_units": record.payout_units, "clv": record.clv,
        } for record in result.bets[:20]]
        return {"summary": summary, "sample_bets": sample,
                "matches": result.matches_considered, "warmup_matches": result.warmup_matches,
                "mode": request.mode, "disclaimer_vi": DISCLAIMER_VI}

    @router.post("/download")
    def download(request: DownloadRequest) -> dict[str, object]:
        target_season = request.season or state.season
        target_division = request.division or state.division
        try:
            dataset = download_dataset(state.cache_dir, target_season, target_division,
                                       refresh=request.refresh)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except DatasetError as exc:
            raise HTTPException(status_code=502, detail=f"Không tải được dữ liệu: {exc}") from exc
        with state._lock:
            state.dataset = dataset
            state.data_error = None
            state.season = target_season
            state.division = target_division
            state._model_cache.clear()
        return {"loaded": True, "matches": len(dataset.matches),
                "skipped_rows": dataset.skipped_rows, "season": target_season,
                "division": target_division, "source": dataset.source,
                "sha256": dataset.sha256}

    @router.post("/reset")
    def reset(request: ResetRequest) -> dict[str, object]:
        if request.confirm != "RESET":
            raise HTTPException(status_code=400, detail="Gửi confirm=RESET để đặt lại ví demo.")
        stats = state.wallet.reset(state.initial_balance)
        return {"wallet": state.wallet.wallet(), "stats": stats}

    app.include_router(router)
    return app


def app_factory() -> FastAPI:
    return create_app()


DISCLAIMER_VI = (
    "Đây là mô phỏng với tiền demo, không phải tiền thật và không kết nối nhà cái. "
    "Cá cược có rủi ro; quá khứ không bảo đảm tương lai. Không có lợi nhuận được bảo đảm. 18+."
)


def _match_payload(state: SandboxState, match: Match) -> dict[str, object]:
    prediction = state.model_for(match).predict(match.home, match.away)
    model = (prediction.p_home, prediction.p_draw, prediction.p_away)
    market = implied_probabilities([float(value) for value in match.odds])
    odds = tuple(float(value) for value in match.odds)
    return {
        "match_id": match.match_id,
        "date": match.date.isoformat(),
        "division": match.division,
        "home": match.home,
        "away": match.away,
        "odds": {"home": odds[0], "draw": odds[1], "away": odds[2]},
        "model": {"home": model[0], "draw": model[1], "away": model[2]},
        "market": {"home": market[0], "draw": market[1], "away": market[2]},
        "edges": {"home": model[0] * odds[0] - 1.0,
                  "draw": model[1] * odds[1] - 1.0,
                  "away": model[2] * odds[2] - 1.0},
        "result_hidden": True,
    }


def _reason_vi(reason: str) -> str:
    return {
        "ODDS_BELOW_MINIMUM": "Tỷ lệ cược thấp hơn mức tối thiểu.",
        "ODDS_ABOVE_MAXIMUM": "Tỷ lệ cược cao hơn mức tối đa cho phép.",
        "EDGE_BELOW_MINIMUM": "Lợi thế mô hình không đủ lớn (dưới 2%).",
        "EMPTY_BANKROLL": "Ví demo đã hết tiền.",
        "STAKE_BELOW_ONE_UNIT": "Số tiền cược nhỏ hơn 1 đơn vị.",
    }.get(reason, reason)
