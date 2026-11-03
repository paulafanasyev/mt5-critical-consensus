"""Worker: owns one terminal at a time and trades the accounts of its shard.

MT5 exposes the local terminal through a single IPC channel (port 22346 per Windows session), so
a worker never holds two terminals at once: it raises the tunnel if the broker needs one, hands
the channel to the account's own terminal, verifies the login and server, manages positions under
both account and portfolio limits, optionally opens one new trade, then releases everything.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
import time
import traceback

import MetaTrader5 as mt5

from . import broker, network, risk, strategy, terminals
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
            ok, detail = network.ensure_tunnel()
            if not ok:
                return f"нужен туннель: {detail}"
        if not account.terminal_exe.exists():
            terminals.provision(account)
        # MT5 answers the API on one port per Windows session: hand the channel over serially.
        terminals.stop_other_fleet(keep_dir=account.data_dir)
        if terminals.running_pid(account) is None:
            terminals.launch(account)
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
            for _ in range(3):
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
        """Release the API channel; the terminal itself is stopped by the caller."""
        try:
            mt5.shutdown()
        except Exception:  # noqa: BLE001 - detaching must never raise
            pass

    # --- one account --------------------------------------------------------------------
    def run_account(self, account: Account) -> WorkerResult:
        error = self.attach(account)
        if error:
            if not account.keep_running:
                terminals.stop(account)
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
        positions = risk.positions_for(account)
        self.store.equity(account.name, snapshot.login, snapshot.balance, snapshot.equity,
                          snapshot.positions, snapshot.floating)
        self.store.save_positions(account.name, positions)

        closed = self._manage_positions(account, day, positions)
        opened = 0
        allowed, reason = risk.can_open(account, snapshot, day)
        if allowed:
            allowed, reason = self._portfolio_allows(snapshot)
        if allowed:
            opened = self._open_best(account, snapshot)
        elif reason:
            self.store.event(account.name, "hold", reason=reason, equity=snapshot.equity)

        self._save_day(account, day)
        return WorkerResult(account.name, "ok",
                            f"equity {snapshot.equity:.2f}", opened=opened, closed=closed)

    def _portfolio_allows(self, snapshot: risk.AccountSnapshot) -> tuple[bool, str]:
        """Check the book-wide limits, using the snapshot other workers left in the store."""
        book = self.store.portfolio()
        known = self.store.open_positions()
        day_start = self.store.get_meta("portfolio_day_start_equity")
        day_stamp = self.store.get_meta("portfolio_day")
        day_pct = 0.0
        today = dt.date.today().isoformat()
        if day_start and day_stamp == today:
            try:
                start_value = float(day_start)
            except ValueError:
                start_value = 0.0
            if start_value > 0 and book["equity"] > 0:
                day_pct = (book["equity"] - start_value) / start_value * 100
        pseudo = [type("P", (), {"symbol": symbol, "volume": volume})
                  for _, symbol, volume in known]
        return risk.can_open_portfolio(self.config.portfolio, [snapshot], pseudo, day_pct)

    # --- position management ------------------------------------------------------------
    def _manage_positions(self, account: Account, day: risk.DayState, positions: list) -> int:
        closed = 0
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
            peak = max(self.store.peak(account.name, position.ticket), gain)
            if gain > 0:
                self.store.set_peak(account.name, position.ticket, peak)
            reason = strategy.exit_reason(account, position, spec, peak)
            if not reason:
                continue
            if self.dry_run:
                self.store.event(account.name, "exit(dry)", symbol=spec.name, reason=reason,
                                 gain=round(gain, 5))
                continue
            try:
                broker.close_position(position, spec)
            except broker.BrokerError as exc:
                self.store.event(account.name, "close_failed", symbol=spec.name, error=str(exc))
                continue
            closed += 1
            day.realized += float(position.profit)
            self.store.event(account.name, "closed", symbol=spec.name, reason=reason,
                             profit=round(float(position.profit), 2))
        return closed

    # --- entries ------------------------------------------------------------------------
    def _open_best(self, account: Account, snapshot: risk.AccountSnapshot) -> int:
        opened = 0
        for signal in strategy.signals(account):
            spec = broker.symbol(signal.base_symbol)
            if spec is None:
                continue
            if broker.open_positions(spec.name, account.magic):
                continue
            volume = risk.size_for(account, spec, signal.stop_distance, snapshot.balance)
            if volume <= 0:
                self.store.event(account.name, "skip", symbol=spec.name, reason="объём 0")
                continue
            if self.dry_run:
                self.store.event(account.name, "entry(dry)", symbol=spec.name,
                                 side=signal.side, volume=volume, reason=signal.reason)
                return 1
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
