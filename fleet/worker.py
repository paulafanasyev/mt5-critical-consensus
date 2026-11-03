"""Worker: owns the API channel for one account at a time and trades its shard's accounts.

MT5 exposes one API channel per Windows session, so a worker hands the channel from account to
account: preflight the port, raise the tunnel if the broker needs one, verify the login, manage
every position on the account (including manual ones), gate entries with account and book limits
reserved atomically, then release the terminal.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
import subprocess
import time
import traceback

import MetaTrader5 as mt5

from . import broker, network, risk, strategy, terminals
from .config import Account, FleetConfig
from .store import Store

PORT = 22346
MAX_FAILURES = 5


@dataclasses.dataclass(slots=True)
class WorkerResult:
    account: str
    status: str
    detail: str = ""
    opened: int = 0
    closed: int = 0


def port_owner() -> int | None:
    """Pid listening on the MT5 API port, if anything is listening at all."""
    script = ("Get-NetTCPConnection -LocalPort 22346 -State Listen -ErrorAction SilentlyContinue"
              " | Select-Object -First 1 -ExpandProperty OwningProcess")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True)
    text = result.stdout.strip()
    return int(text) if text.isdigit() else None


def port_is_free_or_ours(account: Account) -> tuple[bool, str]:
    owner = port_owner()
    if owner is None:
        return True, ""
    script = (f"Get-CimInstance Win32_Process -Filter \"ProcessId={owner}\" | "
              "Select-Object -ExpandProperty CommandLine")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                            capture_output=True, text=True)
    command = (result.stdout or "").lower()
    if str(account.data_dir).lower() in command:
        return True, ""
    return False, f"порт {PORT} занят чужим процессом {owner}"


class Worker:
    def __init__(self, config: FleetConfig, store: Store, pause_file: pathlib.Path | None = None,
                 dry_run: bool = False, allow_live: bool = False) -> None:
        self.config = config
        self.store = store
        self.pause_file = pause_file
        self.dry_run = dry_run
        self.allow_live = allow_live

    # --- connection ---------------------------------------------------------------------
    def attach(self, account: Account) -> str:
        if account.network == "vpn":
            ok, detail = network.ensure_tunnel(account.vpn_profile)
            if not ok:
                return f"нужен туннель: {detail}"
        ok, detail = port_is_free_or_ours(account)
        if not ok:
            return detail
        if not account.terminal_exe.exists():
            terminals.provision(account)
        terminals.stop_other_fleet(keep_dir=account.data_dir)
        if terminals.running_pid(account) is None:
            terminals.launch(account)
        password = terminals.password_for(account)
        kwargs = {"path": str(account.terminal_exe), "timeout": 20000}
        if password:
            kwargs.update(login=account.login, password=password, server=account.server)
        for attempt in range(3):
            if mt5.initialize(**kwargs):
                break
            mt5.shutdown()
            if attempt == 0:
                terminals.wait_authorized(account, timeout=8)
            time.sleep(4 + attempt * 3)
        else:
            terminals.stop(account)
            time.sleep(2)
            terminals.launch(account)
            terminals.wait_authorized(account, timeout=20)
            for _ in range(2):
                if mt5.initialize(**kwargs):
                    break
                mt5.shutdown()
                time.sleep(6)
            else:
                tail = " | ".join(terminals.last_log_lines(account, 4))
                return f"initialize: {mt5.last_error()} ({tail})"
        info = mt5.account_info()
        if info is None:
            mt5.shutdown()
            return "нет данных счёта после initialize"
        if info.login != account.login or info.server != account.server:
            mt5.shutdown()
            return f"привязан чужой счёт {info.login}/{info.server}"
        return ""

    def detach(self) -> None:
        try:
            mt5.shutdown()
        except Exception:  # noqa: BLE001
            pass

    # --- one account --------------------------------------------------------------------
    def run_account(self, account: Account, allow_entries: bool = True) -> WorkerResult:
        failures, next_attempt, last_error = self.store.health(account.name)
        if failures >= MAX_FAILURES:
            return WorkerResult(account.name, "blocked",
                                f"счёт заблокирован после {failures} сбоев: {last_error}")
        if time.time() < next_attempt:
            return WorkerResult(account.name, "cooldown",
                                f"до следующей попытки {int(next_attempt - time.time())} с")
        try:
            error = self.attach(account)
        except Exception as exc:  # noqa: BLE001 - a bad terminal must not kill the shard
            error = f"attach: {type(exc).__name__}: {exc}"
            self.store.event(account.name, "attach_exception", error=str(exc))
        if error:
            backoff = min(900.0, 5.0 * (2 ** min(failures, 6)))
            self.store.record_failure(account.name, error, backoff)
            if not account.keep_running:
                terminals.stop(account)
            return WorkerResult(account.name, "error", error)
        try:
            self.store.clear_failure(account.name)
            return self._cycle(account, allow_entries)
        except Exception as exc:  # noqa: BLE001 - one account never kills the worker
            self.store.event(account.name, "exception", error=str(exc),
                             trace=traceback.format_exc(limit=3))
            return WorkerResult(account.name, "exception", str(exc))
        finally:
            self.detach()
            if not account.keep_running:
                terminals.stop(account)

    def _cycle(self, account: Account, allow_entries: bool) -> WorkerResult:
        server_day = risk.server_day_now()
        snapshot = risk.AccountSnapshot.read(server_day)
        if snapshot is None:
            return WorkerResult(account.name, "error", "нет счёта")
        if snapshot.equity <= 0:
            return WorkerResult(account.name, "flat", "нулевой баланс")
        if account.is_real and snapshot.balance <= 0:
            return WorkerResult(account.name, "flat", "реальный счёт без денег")
        rate = snapshot.rate_to_usd()
        day = self._day_state(account, snapshot, server_day)
        positions = risk.positions_for(account)  # includes manual positions: they move equity

        closed = self._manage_positions(account, day, positions)
        # Re-read after management so the gates judge the account as it is now, not as it was.
        snapshot = risk.AccountSnapshot.read(server_day) or snapshot
        positions = risk.positions_for(account)
        self.store.equity(account.name, snapshot.login, snapshot.balance, snapshot.equity,
                          len(positions), snapshot.floating, snapshot.currency, rate or 0.0,
                          server_day)
        self.store.save_positions(account.name, positions)

        opened = 0
        if allow_entries:
            opened = self._try_entries(account, snapshot, positions, day, rate)
        self._save_day(account, day)
        return WorkerResult(account.name, "ok", f"equity {snapshot.equity:.2f}",
                            opened=opened, closed=closed)

    # --- position management ------------------------------------------------------------
    def _manage_positions(self, account: Account, day: risk.DayState, positions: list) -> int:
        closed = 0
        specs: dict[str, broker.BrokerSymbol | None] = {}
        for position in positions:
            name = position.symbol
            if name not in specs:
                specs[name] = broker.symbol_exact(name)
            spec = specs[name]
            if spec is None:
                self.store.event(account.name, "unmanaged", symbol=name,
                                 reason="нет спецификации символа")
                continue
            atr_entry = self._atr_entry(account, position, spec)
            tick = mt5.symbol_info_tick(name)
            if tick is None:
                continue
            is_buy = int(position.type) == int(mt5.POSITION_TYPE_BUY)
            price = float(tick.bid if is_buy else tick.ask)
            gain = (price - float(position.price_open)) if is_buy else \
                (float(position.price_open) - price)
            peak = max(self.store.peak(account.name, position.ticket), gain)
            if gain > 0:
                self.store.set_peak(account.name, position.ticket, peak)

            be_level = strategy.break_even_level(account, position, spec, atr_entry)
            if be_level is not None and not self.dry_run:
                try:
                    broker.modify_position(int(position.ticket), name, be_level,
                                           float(position.tp), spec.digits)
                    self.store.event(account.name, "break_even", symbol=name,
                                     ticket=int(position.ticket), sl=be_level)
                except broker.BrokerError as exc:
                    self.store.event(account.name, "break_even_failed", symbol=name,
                                     error=str(exc))

            reason = strategy.exit_reason(account, position, spec, peak, atr_entry)
            if not reason:
                continue
            if self.dry_run or (account.is_real and not self.allow_live):
                self.store.event(account.name, "exit(gated)", symbol=name, reason=reason,
                                 gain=round(gain, 5))
                continue
            try:
                broker.close_position(position, spec)
            except broker.BrokerError as exc:
                self.store.event(account.name, "close_failed", symbol=name, error=str(exc))
                continue
            closed += 1
            day.realized += float(position.profit)
            self.store.close_position_row(account.name, int(position.ticket))
            self.store.event(account.name, "closed", symbol=name, reason=reason,
                             profit=round(float(position.profit), 2))
        return closed

    def _atr_entry(self, account: Account, position, spec: broker.BrokerSymbol) -> float:
        key = f"atr:{account.name}:{int(position.ticket)}"
        stored = self.store.get_meta(key)
        if stored:
            try:
                return float(stored)
            except ValueError:
                pass
        stop_distance = abs(float(position.price_open) - float(getattr(position, "sl", 0) or 0))
        if stop_distance > 0 and account.risk.stop_atr > 0:
            atr_value = stop_distance / account.risk.stop_atr
        else:
            atr_value = broker.atr(spec.name, mt5.TIMEFRAME_H1)
        self.store.set_meta(key, f"{atr_value:.8f}")
        return atr_value

    # --- entries ------------------------------------------------------------------------
    def _try_entries(self, account: Account, snapshot: risk.AccountSnapshot, positions: list,
                     day: risk.DayState, rate: float) -> int:
        owned = [p for p in positions if p.magic == account.magic]
        allowed, reason = risk.can_open(account, snapshot, day, positions)
        if not allowed:
            self.store.event(account.name, "hold", reason=reason)
            return 0
        book = self.store.portfolio([a.name for a in self.config.enabled()])
        specs: dict[str, broker.BrokerSymbol | None] = {}
        for position in positions:
            specs[position.symbol] = broker.symbol_exact(position.symbol)
        current_risk = risk.book_risk_money(positions, specs)
        day_pct = self._portfolio_day_pct(book["equity"])
        for signal in strategy.signals(account):
            spec = broker.symbol(signal.base_symbol)
            if spec is None:
                continue
            same_symbol = [p for p in positions if p.symbol == spec.name]
            if len(same_symbol) >= account.strategy.max_positions_per_symbol:
                continue
            if self._in_cooldown(account, spec.name):
                continue
            new_risk = spec.distance_money(signal.stop_distance,
                                           max(spec.volume_min, account.risk.min_lots))
            ok, why = risk.can_open_portfolio(
                self.config.portfolio, book["equity"], current_risk, new_risk,
                positions, specs, day_pct)
            if not ok:
                self.store.event(account.name, "portfolio_hold", reason=why)
                return 0
            volume = risk.size_for(account, spec, signal.stop_distance, snapshot.balance)
            if volume <= 0:
                self.store.event(account.name, "skip", symbol=spec.name, reason="объём 0")
                continue
            risk_money = spec.distance_money(signal.stop_distance, volume)
            reserved, why, slot = self.store.reserve(account.name, spec.name, volume,
                                                     self.config.portfolio, book["equity"])
            if not reserved:
                self.store.event(account.name, "portfolio_hold", reason=why)
                return 0
            try:
                if self.dry_run or (account.is_real and not self.allow_live):
                    self.store.event(account.name, "entry(gated)", symbol=spec.name,
                                     side=signal.side, volume=volume, reason=signal.reason,
                                     risk_usd=round(risk_money * (rate or 1.0), 2))
                    return 1
                execution = broker.market_order(
                    account.magic, spec, signal.side, volume, signal.stop, signal.target,
                    signal.stop_distance, risk_money)
            except broker.BrokerError as exc:
                self.store.release(slot)
                self.store.event(account.name, "entry_failed", symbol=spec.name,
                                 error=str(exc))
                continue
            self.store.trade(account.name, snapshot.login, spec.name, signal.side,
                             execution.volume, signal.price, volume, execution.retcode,
                             signal.reason, execution.token)
            self.store.event(account.name, "opened", symbol=spec.name, side=signal.side,
                             volume=execution.volume, price=execution.price,
                             requested=signal.price, reason=signal.reason)
            fresh = broker.open_positions(spec.name, account.magic)
            for position in fresh:
                self.store.set_meta(f"atr:{account.name}:{int(position.ticket)}",
                                    f"{signal.atr:.8f}")
            self.store.save_positions(account.name, risk.positions_for(account))
            self.store.release(slot)
            self._mark_entry(account, spec.name)
            return 1
        return 0

    def _in_cooldown(self, account: Account, symbol_name: str) -> bool:
        minutes = account.strategy.cooldown_minutes
        if minutes <= 0:
            return False
        stamp = self.store.get_meta(f"entry:{account.name}:{symbol_name}")
        if not stamp:
            return False
        try:
            when = dt.datetime.fromisoformat(stamp)
        except ValueError:
            return False
        return dt.datetime.now(dt.timezone.utc) - when < dt.timedelta(minutes=minutes)

    def _mark_entry(self, account: Account, symbol_name: str) -> None:
        self.store.set_meta(f"entry:{account.name}:{symbol_name}",
                            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))

    # --- book day -----------------------------------------------------------------------
    def _portfolio_day_pct(self, equity_usd: float) -> float:
        start = self.store.get_meta("portfolio_day_start_equity")
        day = self.store.get_meta("portfolio_day")
        today = risk.server_day_now()
        if not start or day != today:
            return 0.0
        try:
            value = float(start)
        except ValueError:
            return 0.0
        if value <= 0 or equity_usd <= 0:
            return 0.0
        return (equity_usd - value) / value * 100

    # --- day state ----------------------------------------------------------------------
    def _day_state(self, account: Account, snapshot: risk.AccountSnapshot,
                   server_day: str) -> risk.DayState:
        stored = self.store.day(account.name)
        if stored and stored[0] == server_day:
            state = risk.DayState(day=server_day, start_balance=stored[1],
                                  realized=stored[2], stopped=stored[3])
        else:
            state = risk.DayState.fresh(snapshot.balance, server_day)
        state.rollover(snapshot.balance, server_day)
        if state.net_flow == 0.0:
            try:
                state.net_flow = risk.daily_net_flow(risk.day_start_datetime(state.day))
            except Exception:  # noqa: BLE001 - history may lag right after midnight
                state.net_flow = 0.0
        return state

    def _save_day(self, account: Account, state: risk.DayState) -> None:
        self.store.save_day(account.name, state.day, state.start_balance, state.realized,
                            state.stopped)


def run_shard(config: FleetConfig, store: Store, shard: int, dry_run: bool = False,
              pause_file: pathlib.Path | None = None, allow_live: bool = False,
              ) -> list[WorkerResult]:
    """Run one shard. A pause blocks new entries but never blocks exits."""
    worker = Worker(config, store, pause_file=pause_file, dry_run=dry_run,
                    allow_live=allow_live)
    paused = bool(pause_file is not None and pause_file.exists())
    results = []
    for account in config.by_shard(shard):
        results.append(worker.run_account(account, allow_entries=not paused))
        time.sleep(0.3)
    return results
