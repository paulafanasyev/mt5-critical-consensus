"""Watch the market with the trading agent, send nothing.

The owner of demo 114929502 is trading it himself, so this run stands down from execution and
keeps observing instead: the same policy, the same gates, the same journal, on the same live bars.
There is no bridge here and no token read - the package has no order surface at all, so a send is
not merely declined, it is not expressible.

Two passes run every cycle.

The first is the trading agent exactly as production would call it. It is currently blocked on
every symbol, and that verdict is itself the observation: it says when the account frees up.

The second is a shadow. The gate above refuses to read a bar at all while the symbols keep getting
flattened, so the research value of the watch would be zero - no proposal, no price, nothing to
score later. The shadow hands the same agent an *empty book*: same terminal, same bars, same tick,
same account equity and margin, same strategy, sizing and geometry checks, but a book this process
holds nothing in and a day it has lost nothing on. Every contention gate then measures only the
shadow's own inactivity instead of the owner's trading, and nothing is disabled or overridden. The
shadow's ledger is a separate file, so it cannot teach the real run that it saw legs it did not.

What comes out is the forward evidence the shared account cannot give: what the strategy would
have opened today, at what size and risk, and when. `scripts/score_forward_shadow.py` turns that
journal into R multiples against the bars that followed, under the pre-registration in
`docs/studies/prereg_forward_shadow_2026_11.md`.

    PYTHONPATH=src:. .venv/Scripts/python.exe scripts/observe_shadow.py
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import yaml

from critical_consensus.mt5_agent import MT5Agent, codes
from critical_consensus.mt5_agent.backend import assert_backend_contract
from critical_consensus.mt5_agent.i18n import describe
from critical_consensus.mt5_agent.journal import DecisionJournal
from critical_consensus.mt5_agent.policy import AgentPolicy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_mt5_demo_trading import DemoTerminal  # reuse the production read adapter, unchanged

UTC = timezone.utc
POLICY_PATH = Path("config/forward/06_ema_all_universe_h1.yaml")
SYMBOLS = ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "XAUUSD", "XAGUSD", "US500")
DB = Path("agent_data/observe_live.db")
SHADOW_DB = Path("agent_data/shadow_live.db")
KILL_SWITCH = Path("agent_data/demo_bridge/kill_switch.json")
INTERVAL_SECONDS = 20
LANGUAGE = "ru"


class FlatBook:
    """The terminal as seen by a process that holds nothing and has lost nothing today.

    Only the three numbers that describe *this process's* book are replaced. Prices, contracts,
    bars, equity, balance, free margin and leverage all still come from the real terminal, so the
    shadow sizes against the money that is actually there.

    ``opened_leg_tickets`` and ``position_stop_risk`` are deliberately absent rather than
    delegated: the first is the churn gate's view of a shared account's history, and the shadow
    has no history of its own. The agent's optional-capability path already treats a backend
    without them as a book this process closes itself, which is exactly what a shadow is.
    """

    def __init__(self, terminal: DemoTerminal) -> None:
        self._terminal = terminal

    @property
    def realized_daily_pnl(self) -> float:
        return 0.0

    @property
    def day_start_equity(self) -> float:
        return self._terminal.account().equity

    @property
    def peak_equity(self) -> float:
        return self._terminal.account().equity

    def account(self):
        return self._terminal.account()

    def symbol_spec(self, symbol: str):
        return self._terminal.symbol_spec(symbol)

    def tick(self, symbol: str):
        return self._terminal.tick(symbol)

    def bars(self, symbol: str, timeframe: str, count: int):
        return self._terminal.bars(symbol, timeframe, count)

    def positions(self) -> list:
        return []

    def shutdown(self) -> None:
        return None


def kill_switch_active() -> bool:
    """Respect the documented brake even though this process could not trade with it off."""
    try:
        raw = json.loads(KILL_SWITCH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return raw.get("kill_switch_active") is True


def observe_policy(symbol: str) -> AgentPolicy:
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    policy = AgentPolicy.from_mapping(raw)
    # A live read takes the contract from the terminal, never from the declared spec: the
    # declaration is a measurement frozen at commit time and the metals' tick fields are wrong
    # on this server. Same as the trading run, so the two are comparable.
    return replace(policy, symbol=symbol, symbol_spec=None, mode="observe",
                   kill_switch_active=kill_switch_active())


def line(moment: str, symbol: str, verdict: str, detail: str) -> None:
    print(f"{moment} {symbol:8s} {verdict:24s} {detail}", flush=True)


def describe_shadow(result, symbol: str, fresh: set) -> str:
    if result.status == codes.STATUS_NO_SIGNAL:
        return "тень: нет сигнала"
    if result.status != codes.STATUS_OBSERVED:
        blockers = ", ".join(describe(code, LANGUAGE) for code in result.blockers)
        return f"тень: {describe(result.status, LANGUAGE)} {blockers or '-'}"
    proposal = result.proposal or {}
    sizing = result.sizing or {}
    key = (symbol, proposal.get("evaluated_at"))
    if key in fresh:
        mark = ""
    else:
        fresh.add(key)
        mark = "  *** НОВЫЙ БАР ***"
    side = "покупка" if proposal.get("direction") == "BUY" else "продажа"
    price = (f"{sizing.get('volume')} лот, риск {sizing.get('risk_value', 0):.0f} $"
             if sizing else "без объёма")
    return (f"тень: {side} {proposal.get('entry_price')} | стоп {proposal.get('stop_loss')} "
            f"цель {proposal.get('take_profit')} R {proposal.get('risk_reward')} "
            f"| {price}{mark}")


def main() -> int:
    # Windows gives a pipe cp1252, which cannot carry the Russian blocker labels the agent
    # describes; the watch is unreadable if the encoding kills the line that explains it.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    terminal = DemoTerminal(server_utc_offset_seconds=observe_policy("EURUSD")
                            .server_utc_offset_seconds, out=sys.stderr, language=LANGUAGE)
    journal = DecisionJournal(DB)
    # The same attachment the trading run makes: the day's equity baseline lives in the journal,
    # so a restart of the watch cannot reset the drawdown it is measuring.
    terminal.attach_journal(journal)
    shadow_journal = DecisionJournal(SHADOW_DB)
    agents = {symbol: MT5Agent(backend=terminal, policy=observe_policy(symbol), journal=journal)
              for symbol in SYMBOLS}
    shadows = {}
    for symbol in SYMBOLS:
        book = FlatBook(terminal)
        assert_backend_contract(book)
        shadows[symbol] = MT5Agent(backend=book, policy=observe_policy(symbol),
                                   journal=shadow_journal)
    print(f"{datetime.now(UTC):%H:%M:%S}Z observing {', '.join(SYMBOLS)}; mode=observe, "
          f"no bridge, no send path", flush=True)
    fresh: set = set()
    while True:
        for symbol, agent in agents.items():
            moment = f"{datetime.now(UTC):%H:%M:%S}"
            try:
                result = agent.run_cycle(open_paper_position=False)
            except Exception as exc:  # noqa: BLE001 - a lost terminal must not end the watch
                line(moment, symbol, "ЧТЕНИЕ СЛОМАЛОСЬ", f"книга: {exc}")
                continue
            moment = result.recorded_at[11:19]
            blockers = ", ".join(describe(code, LANGUAGE) for code in result.blockers)
            line(moment, symbol, describe(result.status, LANGUAGE), f"книга: {blockers or '-'}")
            try:
                shadowed = shadows[symbol].run_cycle(open_paper_position=False)
            except Exception as exc:  # noqa: BLE001
                line(moment, symbol, "ТЕНЬ СЛОМАЛАСЬ", str(exc))
                continue
            line("", symbol, "", describe_shadow(shadowed, symbol, fresh))
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
