# Changelog

## 0.3.0 — 2026-10-30

- Added HMAC-SHA256 evidence envelopes (`evidence.py`): the pipeline verifies issuer, freshness, nonce, payload hash and signature over the canonical bundle body before any gate runs. `canonical_hash`, provider hashing and persistence no longer coerce arbitrary objects to strings or accept NaN/Infinity.
- Added separately signed calibration-service governance attestations (`governance.py`). `certainty_gate` reads historical `dissent_rate`/`false_certainty_rate` only from a verified attestation, never from caller-supplied payload fields.
- `mandatory_agents` and `compliance_agent_id` are policy-owned. `compliance_status` is derived from the `ComplianceAgent` verdict, and permission requests must match the signal's instrument and direction.
- Authorization now requires a `decision_id` and an atomically consumed one-time nonce (`authorization.py`, `authorization_nonces` migration); replay returns `DENY`.
- Challenge resolution requires genuinely new evidence from the trusted registry; peer reviews and challenge records require unique IDs and valid timestamps.
- FastAPI policy endpoints require a signed `{"payload", "envelope"}` wrapper; `/health` reports configured verification honestly. Provider keys must use the `MT5_AGENT_KEY_*` environment namespace.
- Evidence and governance envelopes sign separate domains; the pipeline, API and CLI reject a shared signing key or issuer, require both issuer pins, and `/health` reports distinctness without exposing secrets.
- Calibration is attested rather than self-declared: governance attestations must include per-agent `agent_calibration`, and the verified rows overwrite any bundle-supplied `calibration_meta` before consensus is recomputed.
- Any caller-supplied `user_decision` now requires `require_persisted_risk` evidence; peer reviews and challenge records are TTL-bounded, the contradiction lookback must match policy, `news_or_rumor` must be explicit, and source freshness uses absolute `observed_at`.
- MT5 risk capture requires an explicit broker `server_utc_offset_seconds` and fails closed before initialization when it is missing or malformed; tick freshness and the broker-day deal window use that offset.
- Human approvals now require a third, distinct `operator` signing domain (`MT5_OPERATOR_HMAC_KEY`/`MT5_OPERATOR_ISSUER`) bound to the `signal_id` and `ChiefReasoning` hash; evidence and governance signers cannot approve a trade. Evidence exists only as a separate `user_decision_envelope`.
- The policy API recomputes `reviewer_count`, consensus and `open_challenges` from raw peer-review/challenge records instead of trusting bridge aggregates, and `/v1/permission/check` now returns 423 in favor of persisted-risk `/v1/risk/permission/check`.
- Persisted MT5 risk snapshots must reconcile `risk_value`/`risk_fraction` (from tick size/tick value), daily loss and drawdown against the exact order and captured equity baselines within 1%; symbol and tick-spec mismatches deny.
- MQL5 EA rejects non-exact loopback URLs (userinfo, query/fragment, whitespace, non-ASCII, non-numeric or out-of-range ports), validates a header-safe 24–256 character bridge token before use, and flushes the idempotency tombstone before `OrderSend`. The static checker pins comparison directions and thresholds (demo account mode, volume cap, slippage × point, TTL, expiry grace, FOK/IOC advertisement), strips comments before structural matching, requires the `OrderCheck` failure branch and an unconditional flush, and rejects 34 weakening mutations across 219 invariants. A Python reference model of the flat-object parser is exercised against shared accept/reject fixtures in `config/mql5_flat_object_cases.json` (duplicate keys, escapes, control characters, nesting, malformed numbers).
- Added PostgreSQL nonce-store and `--persist` CLI integration tests, made DB tests use unique symbols and restore the kill switch, documented migration 005 tombstone retention, and ignored generated `*.egg-info`/`build`/`dist` artifacts.
- Added `tzdata` for Windows and a UTC/`ZoneInfo` fallback for MT5 CSV timestamps.
- Verification: local `pytest -q` — 251 passed, 10 skipped (all skips require `TEST_DATABASE_URL` and/or `TEST_REDIS_URL`; no local PostgreSQL/Redis was available). Static MQL5 checks pass; broker, MetaEditor compilation, demo execution and live-market validation were not performed.

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

This is a reference scaffold, not a complete trading product. It does not ingest real market data, prove calibrated probability of profit, or demonstrate the persistence/worker code against a running deployment. It does not verify TOTP/signatures cryptographically or place broker orders. MT5 integration remains uncompiled and unverified in MetaEditor.
