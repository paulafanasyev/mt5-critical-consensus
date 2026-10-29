# Changelog

## 0.2.0 — 2026-10-29

- Made invalid/non-finite confidence, consensus, calibration, source and risk values fail closed; added explicit primary APPROVE confidence floors.
- Hardened ChiefAgent: all checks a–i need evidence; reasoning identity, timestamp, full challenge-ledger hash and evidence-backed tail-risk scenarios are required.
- Bound each PermissionRequest to the exact signal, ChiefReasoning hash, recalculated consensus score and source summary; mismatch blocks the handoff.
- Added a human-review packet that exposes all peer reviews, dissenting opinions, the challenge ledger, Chief reasoning, source reports, risk snapshot and tail-risk scenarios.
- Disabled HTTP authorization evaluation until a server-side permission store and real cryptographic 2FA/signature verification exist. Order submission remains disabled.
- Added Redis Streams idempotent publish/retry/DLQ helpers and PostgreSQL inbox schema; neither a worker service nor a production persistence adapter is deployed.
- Tightened local Compose secrets: no known default database passwords; unique local credentials must be configured explicitly.
- Verification in this environment: 77 pytest tests PASS, 11 JSON Schemas valid, 3 policy YAML files valid, and Docker Compose YAML parses. No MT5 terminal, MetaEditor, Docker runtime or PostgreSQL client was available for integration verification.

## Scope note

This is a reference scaffold, not a complete trading product. It does not ingest real market data, prove calibrated probability of profit, persist the full pipeline in a running database, deploy agent workers, verify TOTP/signatures cryptographically, or place broker orders. MT5 integration remains uncompiled and unverified in MetaEditor.
