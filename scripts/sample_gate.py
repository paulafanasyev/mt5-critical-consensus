from critical_consensus.gates import certainty_gate
import yaml
from pathlib import Path

policy = yaml.safe_load(Path("config/certainty_thresholds.yaml").read_text(encoding="utf-8"))
mandatory_agents = policy["mandatory_agents"]
payload = {
    "signal_id": "demo-signal-001",
    "mandatory_agents": mandatory_agents,
    "verdicts": [
        {"agent_id": agent, "verdict": "APPROVE", "confidence": 0.99,
         "reviewer_count": 2, "calibration_meta": {"brier_score": 0.08}}
        for agent in mandatory_agents
    ],
    "consensus_score": 0.99, "open_challenges": [], "challenge_resolution_status": "RESOLVED", "contradictions_last_n_hours": 0,
    "devil_advocate_report": {"status": "PASS", "counter_hypotheses": [
        {"hypothesis": "h1", "evidence_refs": ["e1"]},
        {"hypothesis": "h2", "evidence_refs": ["e2"]},
        {"hypothesis": "h3", "evidence_refs": ["e3"]}]},
    "key_sources": [{"source_id": "public-1", "credibility": 0.90, "age_seconds": 10, "ttl_seconds": 900}],
    "risk_flags": [],
}
print(certainty_gate(payload, policy))
