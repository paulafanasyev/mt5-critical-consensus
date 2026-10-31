"""MetaTrader 5 agent loop: order-free market/account data, deterministic proposals,
paper execution and an append-only decision journal.

Two operator choices are configured through the policy: ``execution_mode`` (``auto``
opens positions on its own and is only legal for a demo account, ``confirm`` asks the
operator before every position) and ``ui_language`` (Russian, English or Vietnamese,
rendered by :mod:`critical_consensus.mt5_agent.i18n`).

Live order placement is intentionally absent from this package: no module here
imports an order-send API, and every journal record carries
``broker_order_sent = False``. Real execution requires a separate, explicit
policy decision outside this scaffold.
"""
from __future__ import annotations

from . import codes, i18n
from .agent import Approver, MT5Agent, CycleResult, trade_event_payload
from .backend import (AccountInfo, LiveMT5Backend, MT5Backend, PositionSnapshot, SymbolSpec,
                      Tick)
from .journal import DecisionJournal
from .paper import PaperBroker, PaperTrade
from .policy import (MODES, AgentPolicy, EXECUTION_MODES, RiskLimits, StrategyConfig,
                     TradingWindow, load_policy)
from .sizing import SizingResult, SizedOrder, size_position
from .strategy import Proposal, SignalEvaluation, evaluate_signal

__all__ = [
    "AccountInfo", "AgentPolicy", "Approver", "CycleResult", "DecisionJournal", "EXECUTION_MODES",
    "LiveMT5Backend", "MT5Agent", "MT5Backend", "MODES", "PaperBroker", "PaperTrade",
    "PositionSnapshot", "Proposal", "RiskLimits", "SignalEvaluation", "SizedOrder",
    "SizingResult", "SymbolSpec", "StrategyConfig", "Tick", "TradingWindow", "codes", "i18n",
    "evaluate_signal", "load_policy", "size_position", "trade_event_payload",
]
