#!/usr/bin/env python3
"""Evaluate a trusted, evidence-complete decision bundle through all deterministic gates.

This CLI never accepts an operator approval, creates an order, or enables execution.
It is intended for a local/internal evidence packet produced by trusted ingestion and
review services, not an arbitrary public HTTP payload.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from critical_consensus.audit import canonical_hash
from critical_consensus.persistence import PostgresRepository
from critical_consensus.pipeline import evaluate_signal_bundle


ROOT = Path(__file__).resolve().parents[1]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read decision bundle: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ValueError("decision bundle root must be a JSON object")
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read policy file: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"policy file must contain an object: {path.name}")
    return value


def run(bundle_path: Path, output_path: Path, *, persist: bool,
        policy_dir: Path = ROOT / "config") -> dict[str, Any]:
    bundle = _load_json(bundle_path)
    if "user_decision" in bundle:
        raise ValueError(
            "this CLI does not accept or process human approval; remove user_decision "
            "and use the separately authenticated server-side authorization process"
        )

    certainty_policy = _load_yaml(policy_dir / "certainty_thresholds.yaml")
    permission_matrix = _load_yaml(policy_dir / "permission_matrix.yaml")
    result = evaluate_signal_bundle(bundle, certainty_policy, permission_matrix)

    report: dict[str, Any] = {
        "result": result,
        "execution_enabled": False,
        "order_created": False,
        "human_authorization_processed": False,
        "source_bundle": bundle_path.name,
        "source_bundle_sha256": canonical_hash(bundle),
        "persistence": None,
    }
    if persist:
        signal_input = bundle.get("signal_input")
        signal_input_hash = bundle.get("signal_input_hash")
        if not isinstance(signal_input, dict) or signal_input_hash != canonical_hash(signal_input):
            raise ValueError("cannot persist a decision without a valid immutable signal_input hash")
        repo = PostgresRepository()
        repo.migrate()
        report["persistence"] = repo.persist_pipeline_result(
            result, signal_input_sha256=signal_input_hash,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False, default=str) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="trusted local JSON evidence bundle")
    parser.add_argument("--output", type=Path, default=Path("decision-result.json"))
    parser.add_argument("--policy-dir", type=Path, default=ROOT / "config")
    parser.add_argument("--persist", action="store_true",
                        help="persist the verified audit chain and result to DATABASE_URL")
    args = parser.parse_args()
    try:
        report = run(args.bundle, args.output, persist=args.persist, policy_dir=args.policy_dir)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    result = report["result"]
    print(json.dumps({
        "signal_id": result.get("signal_id"),
        "status": result.get("status"),
        "blockers": result.get("blockers", []),
        "audit_chain_valid": result.get("audit_chain_valid"),
        "decision_id": (report.get("persistence") or {}).get("decision_id"),
        "output": str(args.output),
        "execution_enabled": False,
        "order_created": False,
    }, indent=2))
    return 0 if result.get("status") == "REQUIRE_HUMAN" else 2


if __name__ == "__main__":
    raise SystemExit(main())
