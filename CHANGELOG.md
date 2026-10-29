# Changelog

## 0.2.0 — 2026-10-29

- Made invalid/non-finite confidence, consensus, calibration, source and risk values fail closed; added explicit primary APPROVE confidence floors.
- Hardened ChiefAgent: all checks a–i need evidence; reasoning identity, timestamp, full challenge-ledger hash and evidence-backed tail-risk scenarios are required.
- Bound each PermissionRequest to the exact signal, ChiefReasoning hash, recalculated consensus score and source summary; mismatch blocks the handoff.
- Added a human-review packet that exposes all peer reviews, dissenting opinions, the challenge ledger, Chief reasoning, source reports, risk snapshot and tail-risk scenarios.
- Disabled HTTP authorization evaluation until a server-side permission store and real cryptographic 2FA/signature verification exist. Order submission remains disabled.
- Added Redis Streams idempotent publish/retry/DLQ helpers and PostgreSQL inbox schema; neither a worker service nor a production persistence adapter is deployed.
- Tightened local Compose secrets: no known default database passwords; unique local credentials must be configured explicitly.
- Added scheduled `pip-audit` workflow. It found `PYSEC-2026-1845` in pytest 8.4.2; the dev dependency now requires pytest >=9.0.3. The audit subsequently passed.
- Verification: local source archive — 78 pytest tests PASS; GitHub Actions `main` — 84 tests PASS on Python 3.12 (`ubuntu-22.04`); dependency audit passed after upgrading pytest to >=9.0.3. Test workflow (84 passed): https://github.com/paulafanasyev/mt5-critical-consensus/actions/runs/40849840096. Dependency audit (no known vulnerabilities): https://github.com/paulafanasyev/mt5-critical-consensus/actions/runs/40849840095. The 11 JSON Schema files are syntactically valid. No MT5 terminal, MetaEditor, Docker runtime or PostgreSQL client was available for integration verification.

## Scope note

This is a reference scaffold, not a complete trading product. It does not ingest real market data, prove calibrated probability of profit, persist the full pipeline in a running database, deploy agent workers, verify TOTP/signatures cryptographically, or place broker orders. MT5 integration remains uncompiled and unverified in MetaEditor.
