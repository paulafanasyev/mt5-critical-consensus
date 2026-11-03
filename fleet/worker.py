"""Worker: owns one terminal at a time and trades the accounts of its shard.

MT5 exposes the local terminal through a single IPC channel, so a worker never tries to hold two
terminals at once. It attaches to the account's dedicated terminal, verifies that the login and
server really are the ones configured, manages open positions, optionally opens a new one, then
detaches so the next account can use the channel.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
import time
import traceback

import MetaTrader5 as mt5

from . import broker, risk, strategy, terminals
from .config import Account, FleetConfig
from .store import Store


@dataclasses.dataclass(slots=True)
class WorkerResult:
    account: str
    status: str
    detail: str = ""
    opened: int = 0
    closed: int = 0


class Worker:
    def __init__(self, config: FleetConfig, store: Store, pause_file: pathlib.Path | None = None,
                 dry_run: bool = False) -> None:
        self.config = config
        self.store = store
        self.pause_file = pause_file
        self.dry_run = dry_run

    # --- connection ---------------------------------------------------------------------
    def attach(self, account: Account) -> str:
        """Bind the API to this account's terminal; returns an error text or ''."""
        if account.network == "vpn":
            return "аккаунт помечен как vpn: туннель должен быть поднят дирижёром"
        if not account.terminal_exe.exists():
            terminals.provision(account)
        # MT5 answers the API on one port per Windows session: hand the channel over serially.
        terminals.stop_other_fleet(keep_dir=account.data_dir)
        if terminals.running_pid(account) is None:
            terminals.launch(account)
        # A saved session can authorize without writing a fresh log line, so the API call below
        # is the real proof: the log is only used to give a useful error when it fails.
        terminals.wait_authorized(account, timeout=45)
        password = terminals.password_for(account)
        kwargs = {"path": str(account.terminal_exe), "timeout": 20000}
        if password:
            kwargs.update(login=account.login, password=password, server=account.server)
        last_error = None
        for attempt in range(4):
            if mt5.initialize(**kwargs):
                last_error = None
                break
            last_error = mt5.last_error()
            mt5.shutdown()
            time.sleep(6 + attempt * 4)
        if last_error is not None:
            # A terminal that lost its server session often keeps the API port busy while
            # answering nothing; a fresh process is the reliable cure.
            terminals.stop(account)
            time.sleep(3)
            terminals.launch(account)
            terminals.wait_authorized(account, timeout=45)
            for attempt in range(3):
                if mt5.initialize(**kwargs):
                    last_error = None
                    break
                last_error = mt5.last_error()
                mt5.shutdown()
                time.sleep(8)
        if last_error is not None:
            tail = " | ".join(terminals.last_log_lines(account, 4))
            return f"initialize: {last_error} ({tail})"
        info = mt5.account_info()
        if info is None:
            mt5.shutdown()
            return "нет данных счёта после initialize"
        if info.login != account.login or info.server != account.server:
            mt5.shutdown()
            return f"привязан чужой счёт {info.login}/{info.server}"
        return ""

    def detach(self) -> None:
        """Release the API channel, stopping the terminal unless it is meant to stay up."""
        try:
            mt5.shutdown()
        except Exception:  # noqa: BLE001 - detaching must never raise
            pass

    # --- one account --------------------------------------------------------------------
    def run_account(self, account: Account) -> WorkerResult:
        error = self.attach(account)
        if error:
            return WorkerResult(account.name, "error", error)
        try:
            return self._cycle(account)
        except Exception as exc:  # noqa: BLE001 - a worker survives any single account
            self.store.event(account.name, "exception", error=str(exc),
                             trace=traceback.format_exc(limit=3))
            return WorkerResult(account.name, "exception", str(exc))
        finally:
            self.detach()
            if not account.keep_running:
                terminals.stop(account)

    def _cycle(self, account: Account) -> WorkerResult:
        snapshot = risk.AccountSnapshot.read()
        if snapshot is None:
            return WorkerResult(account.name, "error", "нет счёта")
        if snapshot.equity <= 0:
            return WorkerResult(account.name, "flat", "нулевой баланс")
        if account.is_real and snapshot.balance <= 0:
            return WorkerResult(account.name, "flat", "реальный счёт без денег")

        day = self._day_state(account, snapshot)
        self.store.equity(account.name, snapshot.login, snapshot.balance, snapshot.equity,
                          snapshot.positions, snapshot.floating)

        closed = self._manage_positions(account, day)
        opened = 0
        allowed, reason = risk.can_open(account, snapshot, day)
        if allowed:
            opened = self._open_best(account, snapshot)
        elif reason:
            self.store.event(account.name, "hold", reason=reason, equity=snapshot.equity)

        self._save_day(account, day)
        return WorkerResult(account.name, "ok", f"equity {snapshot.equity:.2f}",
                            opened=opened, closed=closed)

    # --- position management ------------------------------------------------------------
    def _manage_positions(self, account: Account, day: risk.DayState) -> int:
        closed = 0
        positions = risk.positions_for(account)
        for position in positions:
            spec = broker.symbol(position.symbol)
            if spec is None:
                continue
            tick = mt5.symbol_info_tick(spec.name)
            if tick is None:
                continue
            is_buy = position.type == mt5.POSITION_TYPE_BUY
            price = float(tick.bid if is_buy else tick.ask)
            entry = float(position.price_open)
            gain = (price - entry) if is_buy else (entry - price)
            peak_key = f"peak:{account.name}:{position.ticket}"
            peak = self._peak(peak_key, gain)
            reason = strategy.exit_reason(account, position, spec, peak)
            if not reason and gain > 0:
                atr = broker.atr(spec.name, mt5.TIMEFRAME_H1)
                if atr > 0 and gain >= atr * account.risk.trail_arm_atr:
                    reason = "тейк по ATR"
            if not reason:
                continue
            if self.dry_run:
                self.store.event(account.name, "exit(dry)", symbol=spec.name, reason=reason,
                                 gain=round(gain, 5))
                continue
            try:
                broker.close_position(position, spec)
                closed += 1
                day.realized += float(position.profit)
                self.store.event(account.name, "closed", symbol=spec.name, reason=reason,
                                 profit=round(float(position.profit), 2))
            except broker.BrokerError as exc:
                self.store.event(account.name, "close_failed", symbol=spec.name, error=str(exc))
        return closed

    def _peak(self, key: str, gain: float) -> float:
        peaks = getattr(self, "_peaks", None)
        if peaks is None:
            peaks = {}
            self._peaks = peaks
        current = max(peaks.get(key, 0.0), gain)
        peaks[key] = current
        return current

    # --- entries ------------------------------------------------------------------------
    def _open_best(self, account: Account, snapshot: risk.AccountSnapshot) -> int:
        opened = 0
        for signal in strategy.signals(account):
            spec = broker.symbol(signal.base_symbol)
            if spec is None:
                continue
            held = broker.open_positions(spec.name, account.magic)
            if held:
                continue
            volume = risk.size_for(account, spec, signal.stop_distance, snapshot.balance)
            if volume <= 0:
                self.store.event(account.name, "skip", symbol=spec.name, reason="объём 0")
                continue
            if self.dry_run:
                self.store.event(account.name, "entry(dry)", symbol=spec.name,
                                 side=signal.side, volume=volume, reason=signal.reason)
                opened += 1
                break
            try:
                result = broker.market_order(account.magic, spec, signal.side, volume,
                                             signal.stop, signal.target, comment="fleet")
            except broker.BrokerError as exc:
                self.store.event(account.name, "entry_failed", symbol=spec.name,
                                 error=str(exc))
                continue
            opened += 1
            self.store.trade(account.name, snapshot.login, spec.name, signal.side, volume,
                             signal.price, signal.stop, signal.target, result.retcode,
                             signal.reason)
            self.store.event(account.name, "opened", symbol=spec.name, side=signal.side,
                             volume=volume, price=signal.price, reason=signal.reason)
            break  # one new position per account per cycle
        return opened

    # --- day state ----------------------------------------------------------------------
    def _day_state(self, account: Account, snapshot: risk.AccountSnapshot) -> risk.DayState:
        stored = self.store.day(account.name)
        today = dt.date.today().isoformat()
        if stored and stored[0] == today:
            state = risk.DayState(dt.date.today(), stored[1], stored[2], stored[3])
        else:
            state = risk.DayState.fresh(snapshot.balance)
        state.rollover(snapshot.balance)
        return state

    def _save_day(self, account: Account, state: risk.DayState) -> None:
        self.store.save_day(account.name, state.day.isoformat(), state.start_balance,
                            state.realized, state.stopped)


def run_shard(config: FleetConfig, store: Store, shard: int, dry_run: bool = False,
              pause_file: pathlib.Path | None = None) -> list[WorkerResult]:
    worker = Worker(config, store, pause_file=pause_file, dry_run=dry_run)
    results = []
    for account in config.by_shard(shard):
        if pause_file is not None and pause_file.exists():
            results.append(WorkerResult(account.name, "paused", "файл паузы"))
            break
        results.append(worker.run_account(account))
        time.sleep(0.5)
    return results
