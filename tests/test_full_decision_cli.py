from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from critical_consensus.audit import canonical_hash


ROOT = Path(__file__).resolve().parents[1]


def test_full_decision_cli_fails_closed_and_writes_auditable_result(tmp_path):
    signal_input = {"signal_id": "cli-smoke-001", "instrument": "EURUSD"}
    bundle = {
        "signal_id": signal_input["signal_id"],
        "signal_input": signal_input,
        "signal_input_hash": canonical_hash(signal_input),
        "verdicts": [],
    }
    bundle_path = tmp_path / "bundle.json"
    output_path = tmp_path / "result.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "evaluate_signal_bundle.py"),
         str(bundle_path), "--output", str(output_path)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["result"]["status"] == "GATHER_MORE"
    assert report["result"]["trade_allowed"] is False
    assert report["execution_enabled"] is False
    assert report["order_created"] is False
    assert report["human_authorization_processed"] is False
    assert report["result"]["audit_chain_valid"] is True


def test_full_decision_cli_rejects_user_supplied_approval(tmp_path):
    signal_input = {"signal_id": "cli-approval-001"}
    bundle = {
        "signal_id": signal_input["signal_id"],
        "signal_input": signal_input,
        "signal_input_hash": canonical_hash(signal_input),
        "verdicts": [],
        "user_decision": {"decision": "APPROVE", "user_id": "spoofed"},
    }
    bundle_path = tmp_path / "bundle-with-approval.json"
    output_path = tmp_path / "result.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "evaluate_signal_bundle.py"),
         str(bundle_path), "--output", str(output_path)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "does not accept or process human approval" in result.stderr
    assert not output_path.exists()
