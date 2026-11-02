from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "standalone" / "mql5" / "CriticalConsensusStandalone.mq5"
READY_COPY = ROOT / "standalone" / "mt5-ready" / "CriticalConsensusStandalone.mq5"


def test_standalone_sources_are_identical():
    assert SOURCE.read_bytes() == READY_COPY.read_bytes()


def test_fx_pips_are_not_mistaken_for_points():
    source = SOURCE.read_text(encoding="utf-8")
    assert "return point * ((digits == 3 || digits == 5) ? 10.0 : 1.0);" in source
    assert "SlPips * pip" in source
    assert "TakeProfitPips * pip" in source
    assert "SlPips * point" not in source
    assert "TakeProfitPips * point" not in source


def test_sell_signal_requires_falling_ema_slope_alignment():
    source = SOURCE.read_text(encoding="utf-8")
    assert "fast[1] < slow[1] && fast[1] < fast[0] && slow[1] <= slow[0]" in source
    assert "fast[1] > slow[1] && fast[1] > fast[0] && slow[1] >= slow[0]" in source
    assert "EMA direction/slope not aligned" in source


def test_risk_sizing_refuses_to_round_up_to_broker_minimum():
    source = SOURCE.read_text(encoding="utf-8")
    assert "RiskPercentPerTrade   = 0.50" in source
    assert "MathFloor(raw_volume / step" in source
    assert "risk budget is below broker minimum lot; trade refused" in source


def test_open_position_is_managed_on_every_tick():
    source = SOURCE.read_text(encoding="utf-8")
    tick = source.split("void OnTick()", 1)[1].split("void OnTimer()", 1)[0]
    assert tick.index("ManageOpenPositions();") < tick.index("iTime(")
    manager = source.split("void ManageOpenPositions()", 1)[1].split("void PollApproval()", 1)[0]
    assert "POSITION_MAGIC" in manager and "POSITION_SYMBOL" in manager
    assert "TRADE_ACTION_SLTP" in manager
    assert "candidate_sl <= old_sl + point * 0.5" in manager
    assert "candidate_sl >= tick.bid - min_distance" in manager
    assert "candidate_sl <= tick.ask + min_distance" in manager


def test_no_fixed_take_profit_by_default_and_trailing_protection_is_enabled():
    source = SOURCE.read_text(encoding="utf-8")
    assert "TakeProfitPips        = 0" in source
    assert "BreakEvenAfterPips    = 10" in source
    assert "TrailStartPips        = 20" in source
    assert "TrailDistancePips     = 10" in source
    assert "ManageOpenPositions();" in source
