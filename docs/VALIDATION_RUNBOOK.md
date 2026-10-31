# MT5 Critical Consensus: full validation runbook

Status: CI for the current decision/forecast changes is green, but production readiness is NOT PROVEN. Do not connect a live account. The intended order is data → persistence → agent evidence → objective forecast outcomes → calibration → EA demo → end-to-end verification.

## 1. Read-only market data

On the Windows host that already has MetaTrader 5 installed and logged in:

```powershell
python -m pip install -e ".[data]" MetaTrader5
$env:DATABASE_URL = "postgresql://USER:PASSWORD@127.0.0.1:5432/mt5cc"
python scripts/import_mt5_history.py --live-terminal --symbol EURUSD --timeframe H1 --from-utc 2026-10-01T00:00:00Z --to-utc 2026-10-30T00:00:00Z
```

The connector only calls `initialize`, `symbol_select`, `copy_rates_range`, and `shutdown`. It contains no order submission calls. For CSV export, specify the broker/server timezone explicitly:

```powershell
python scripts/import_mt5_history.py --csv .\EURUSD_H1.csv --source-timezone Europe/Helsinki --symbol EURUSD --timeframe H1
```

Do not assume the broker's export timezone; verify it in MT5 first. Naive timestamps without an explicit timezone are rejected. The source CSV is read-only. OHLC rows are validated for finite prices, valid ranges, ordered unique UTC timestamps, symbol and timeframe consistency. After the initial chronological history load, the PostgreSQL market-bar ledger accepts only forward inserts for each symbol/timeframe; identical replays remain idempotent, while late backfills are rejected because they could retroactively change which bar resolves an already-issued forecast. Historical corrections require a separately audited revision path.

## 2. PostgreSQL and Redis

Install the optional `data` extra, set `DATABASE_URL` and `REDIS_URL`, then run:

```bash
python -m critical_consensus.persistence --migrate
python scripts/run_worker.py
```

The worker is a durable event writer for `cc:signals:v1`, not a trade executor. It acknowledges Redis entries only after the PostgreSQL transaction succeeds. After a worker interruption, idle pending entries are reclaimed with Redis `XAUTOCLAIM` (30-second idle threshold) and passed through the same idempotent PostgreSQL handler before ACK. Handler failures are retried up to three attempts and then written to the dead-letter stream before the source entry is acknowledged. Delivery is at-least-once: PostgreSQL's `processed_events` inbox prevents duplicate durable effects, but Redis and PostgreSQL do not share an exactly-once transaction. The same event ID with a different payload is rejected. Market bars and emitted forecast probabilities cannot be silently edited. Apply migrations with a dedicated database role and restrict production DB privileges; the sample integration role used by CI is intentionally more permissive than production should be.

## 3. Agent consensus

For a controlled provider test, define provider metadata with environment variable names only; API secrets themselves belong in the environment/secret manager. For example:

```powershell
$env:OPENAI_API_KEY = "set-in-your-secret-manager"
$env:MT5_AGENT_PROVIDERS = '[{"agent_id":"PlatformScoutAgent","role":"analyst","independence_group":"vendor-platform","model":"YOUR_PLATFORM_MODEL","api_key_env":"PLATFORM_API_KEY","base_url":"https://platform-provider.example/v1"},{"agent_id":"GlobalNewsAgent","role":"analyst","independence_group":"vendor-news","model":"YOUR_NEWS_MODEL","api_key_env":"NEWS_API_KEY","base_url":"https://news-provider.example/v1"},{"agent_id":"TechnicalAnalystAgent","role":"analyst","independence_group":"vendor-technical","model":"YOUR_TECHNICAL_MODEL","api_key_env":"TECHNICAL_API_KEY","base_url":"https://technical-provider.example/v1"},{"agent_id":"RiskManagerAgent","role":"risk_review","independence_group":"vendor-risk","model":"YOUR_RISK_MODEL","api_key_env":"RISK_API_KEY","base_url":"https://risk-provider.example/v1"},{"agent_id":"ComplianceAgent","role":"critic","independence_group":"vendor-compliance","model":"YOUR_COMPLIANCE_MODEL","api_key_env":"COMPLIANCE_API_KEY","base_url":"https://compliance-provider.example/v1"},{"agent_id":"ContradictionAgent","role":"critic","independence_group":"vendor-contradiction","model":"YOUR_CONTRADICTION_MODEL","api_key_env":"CONTRADICTION_API_KEY","base_url":"https://contradiction-provider.example/v1"},{"agent_id":"DevilAdvocateAgent","role":"devil_advocate","independence_group":"vendor-devil","model":"YOUR_DEVIL_ADVOCATE_MODEL","api_key_env":"DEVIL_ADVOCATE_API_KEY","base_url":"https://devil-advocate-provider.example/v1"},{"agent_id":"PortfolioAgent","role":"analyst","independence_group":"vendor-portfolio","model":"YOUR_PORTFOLIO_MODEL","api_key_env":"PORTFOLIO_API_KEY","base_url":"https://portfolio-provider.example/v1"},{"agent_id":"QuantModelAgent","role":"analyst","independence_group":"vendor-quant","model":"YOUR_QUANT_MODEL","api_key_env":"QUANT_API_KEY","base_url":"https://quant-provider.example/v1"}]'
python scripts/run_agent_analysis.py --symbol EURUSD --timeframe H1 --bars 200 --source-id SRC_ID_FROM_CAPTURE_1 --source-id SRC_ID_FROM_CAPTURE_2
```

Each provider must have a unique configured HTTPS endpoint origin and unique endpoint/model pair. The runtime blocks duplicate origins even when declared group names differ. This is only a configuration-diversity check; statistical independence of training data and model behaviour remains NOT PROVEN and requires an external audit.
The template contains all nine mandatory IDs from `certainty_thresholds.yaml`. The runner requires 2–12 exact persisted source IDs from different operator-configured publisher independence groups; missing, stale, tampered, unregistered or single-group sources cause `GATHER_MORE` before any model call. It verifies each capture's bytes and SHA-256, then extracts visible HTML or JSON text and passes that untrusted document text together with the read-only market snapshot to the nine mandatory agents. Source text is data only: system prompts explicitly instruct models to ignore embedded directives, and peer-review/challenge callbacks receive only the same allowlisted evidence IDs and extracted text. Four functional roles are required: analyst, critic, devil_advocate and risk_review. Each configured agent must have its own endpoint origin and unique model identity; duplicate origins/models are rejected at configuration level. When all nine verdicts pass, providers perform anonymized peer reviews (two reviewers per verdict), validate citations against the signal evidence registry, register each CHALLENGE and attempt resolution in at most three rounds. Missing/invalid reviews produce `GATHER_MORE`; unresolved challenges produce `ESCALATE`; only a complete evidence-backed ledger returns `READY_FOR_CHIEF`. The complete result is written to a PostgreSQL audit event and published to Redis when `REDIS_URL` is configured. The runner now also hands the exact signal, verdicts, peer reviews and challenge ledger to `evaluate_signal_bundle(...)` with persisted publisher-provenance checks enabled, then stores the hash-linked deterministic decision trace in append-only `decision_runs`/`audit_events`. To export the exact handoff packet for replay or inspection, add `--decision-bundle-output .\\decision-bundle.json`. The runner does not synthesize calibration history, contradiction scans, source ranking, portfolio/risk evidence, deterministic nine-step Chief reasoning, a permission request or human approval. Until those independently produced inputs exist, the deterministic pipeline must remain at `GATHER_MORE` or `HOLD`; `READY_FOR_CHIEF` alone is not a full-pipeline PASS. The process returns success only if the deterministic gates reach `REQUIRE_HUMAN` and Redis publication succeeds. LLM confidence remains `UNCALIBRATED_SUBJECTIVE`.

`chief_review` here is an LLM reviewer role, not the deterministic `chief_skeptic_pass` gate. The runner automatically invokes the deterministic pipeline after agent review, but it intentionally stops short of filling fields that must come from separate evidence-producing stages: all nine Chief reasoning steps, calibrated historical metrics, complete risk metadata and a bound permission request. Passing deterministic checks produces a human-review packet only; this runner does not process human authorization and never creates an order. Shared model families, vendors, prompts or market sources can still create correlated errors; role/group labels do not prove independence.

Register actual model/provider callables using `AgentSpec` and `collect_agent_verdicts`. Set the required number of distinct providers and independent groups explicitly. A provider must return signal-bound verdict ID, agent ID, SHA-256 input binding, UTC creation time, positive TTL and evidence references. Errors, timeouts and invalid output cause `GATHER_MORE`.

The `independence_group` value is declared metadata only. It is **not proof** that models, training data, vendors, prompts or market inputs are independent. Production onboarding must document and review those dependencies. The existing policy pipeline remains the only place where gate sequencing is evaluated; collection success never authorizes a trade.

### Publisher evidence capture

The persisted decision path accepts source reports only when every `key_sources[].source_id` resolves to a byte-exact immutable capture in PostgreSQL and the source matches `config/source_registry.yaml`. The registry is intentionally empty by default. A configured hostname is an exact host allowlist entry; subdomains are not implicitly trusted and must be added individually. Query parameters are rejected unless explicitly listed for that publisher, and credential-like parameter names are always rejected. Do not add a publisher until its official HTTPS hostname, actual independence group, policy credibility weight, TTL and permitted publication-time attestation have been reviewed. Different hostnames do not establish independent ownership or independent reporting.

After adding an approved publisher profile, create a manifest containing the publisher ID and exact HTTPS article/API URL. A publisher without a configured timestamp pointer also needs an explicitly inspected `published_at` value and `operator_attested` basis:

```json
{
  "sources": [
    {
      "publisher_id": "OPERATOR_REVIEWED_ID",
      "url": "https://approved.example/path/to/specific-resource",
      "published_at": "2026-10-30T01:30:00Z",
      "publication_time_attestation": "operator_attested"
    }
  ]
}
```

For a publisher-specific JSON API whose timestamp field was verified by the operator, configure the profile as follows (the example pointer must be replaced with the real field path from that API):

```yaml
publishers:
  OPERATOR_REVIEWED_ID:
    domains: ["approved.example"]
    independence_group: "OPERATOR_REVIEWED_GROUP"
    credibility: 0.90
    ttl_seconds: 900
    allowed_publication_time_attestations: ["publisher_json_pointer"]
    published_at_json_pointer: "/metadata/published_at"
    allowed_query_parameters: []
```

For that profile, the manifest can omit both `published_at` and `publication_time_attestation`; the capture tool derives both timestamp and attestation from the configured pointer. Do not enable this mode unless the endpoint and timestamp semantics have been checked against the publisher's official API documentation.


Then run:

```bash
python scripts/capture_source_evidence.py source-manifest.json
```

For an API that returns a timezone-aware ISO-8601 publication field, the operator may configure `published_at_json_pointer` in that publisher's registry profile and set `allowed_publication_time_attestations: ["publisher_json_pointer"]`. The capture script derives the timestamp from the exact captured JSON bytes, persists the pointer and re-verifies the same field on every later source validation. A mismatch, missing field or malformed pointer blocks capture. Without this per-publisher JSON API configuration, the generic fetcher accepts only the explicit `operator_attested` basis; the operator must inspect and supply `published_at`. A JSON Pointer check proves which timestamp the allowlisted publisher response declared; it does not independently prove that the date is truthful or that the report is independent. It refuses non-HTTPS URLs, redirects, unapproved domains, IP literals, unapproved or credential-like query parameters, unsupported response types and oversized documents. DNS is resolved before connect; every answer must be public/global, and the TLS connection is pinned to one of those validated IPs while preserving hostname SNI and certificate verification, preventing DNS rebinding to loopback/private services. It stores exact response bytes, SHA-256, retrieval time and registry-derived metadata in append-only `source_evidence`. An operator must inspect the publication date and attest it; that attestation remains an operator assertion, not cryptographic proof. A persisted decision also requires at least two configured independence groups. Keep the registry empty rather than promoting guessed domains or scores.



## 4. Objective market-event forecasts and walk-forward calibration

The runner records each validated agent's **objective event probability** before peer review and before the target bar can close. It keeps the agent's qualitative verdict confidence as a separate field; subjective confidence is not used as the calibration probability.

The fixed target policy is `next-available-bar-close-above-reference-v1`:
- The reference is the last completed market bar in the exact signal snapshot, including its symbol, timeframe, UTC bar timestamp and close.
- The reference close must match an already persisted immutable `market_bars` row before the forecast is accepted.
- Each agent's issue timestamp is its own UTC `created_at`. The target is the first persisted bar for that symbol/timeframe whose bar-open timestamp is strictly later than that issue timestamp.
- Outcome is 1 only if that target bar closes strictly above the reference close; flat or lower close is 0.
- A target bar cannot be resolved until its full timeframe has elapsed plus the configured finalization grace. No later bar may replace the first eligible target bar.
- Missing reference/target bars remain pending. Manual outcome labels are disabled.

On the Windows host with MT5 installed, first import the newest closed bars. The import is read-only with respect to MT5 and does not send orders:

```powershell
python scripts/import_mt5_history.py --live-terminal --symbol EURUSD --timeframe H1 --from-utc 2026-10-30T00:00:00Z --to-utc 2026-10-31T00:00:00Z
```

For an existing CSV export, provide the verified broker/server timezone:

```powershell
python scripts/import_mt5_history.py --csv .\\EURUSD_H1.csv --source-timezone Europe/Helsinki --symbol EURUSD --timeframe H1
```

Then resolve only forecasts whose first target bar is present and complete:

```powershell
python scripts/resolve_forecasts.py --symbol EURUSD --timeframe H1 --limit 500
```

The resolver is idempotent. It stores an append-only evidence object containing the exact reference bar, target bar, their timestamps/OHLC/source metadata, forecast input hash, target policy and outcome hash. It does not create labels when a bar is missing or incomplete. Repeat after new market bars are imported.

Export only database-resolved objective market-event forecasts; legacy subjective-confidence rows are excluded:

```powershell
python scripts/export_resolved_forecasts.py --output .\\resolved-forecasts.csv
python scripts/evaluate_calibration_db.py --output .\\calibration-report.json
python scripts/store_calibration_report.py .\\calibration-report.json
```

The evaluator rejects incomplete or tampered evidence metadata, checks the canonical resolution-evidence hash and verifies that the declared outcome matches the reference and target closes. Legacy CSV rows without the new evidence contract can be parsed for diagnostics but force provenance status to `NOT_VERIFIED_BY_SCRIPT` and cannot advance the overall status beyond `NOT_PROVEN`. The database-native evaluator reads directly from `forecast_predictions` joined to `forecast_resolution_events`, re-validates every evidence payload, and hashes the exact exported ledger rows. It is the only evaluator allowed to label provenance `DATABASE_LEDGER_SELF_CONSISTENT_NOT_BROKER_AUTHENTICATED`; the CSV evaluator is diagnostic-only and always reports `NOT_VERIFIED_BY_SCRIPT`.

The walk-forward evaluator fits Platt scaling only on earlier forecasts whose outcomes were resolved before the next test block. It reports Brier score, log loss, reliability bins, ECE, high-confidence false-certainty rate, sample counts and deterministic paired-bootstrap intervals. Too few observations or an interval that does not show improvement remains `NOT_PROVEN`. The policy's minimum sample requirements still apply independently.

**Evidence limits:** hashes and database constraints demonstrate internal consistency and immutability within this PostgreSQL ledger. They do not independently authenticate broker-feed accuracy/completeness, prove statistical independence between providers, prove a profitable trading strategy or establish future performance. A calibration report is not a backtest, execution test or profitability claim. Do not manually label outcomes or promote legacy subjective-confidence rows into this dataset.

## 5. Full deterministic Chief Gate

Once the upstream services have produced a trusted evidence packet, evaluate the entire deterministic path (verdict metadata → peer review → challenge ledger → Certainty Gate → nine-step ChiefAgent → Permission Gate → human-review packet):

```powershell
$env:DATABASE_URL = "postgresql://USER:PASSWORD@DB_HOST:5432/mt5cc"
python scripts/evaluate_signal_bundle.py .\trusted-decision-bundle.json --output .\decision-result.json --persist
```

The bundle must include the exact immutable `signal_input` object, its `signal_input_hash`, the nine policy-mandatory agent verdicts, two validated peer reviews per verdict, a registered/resolved challenge ledger, at least three evidence-backed counter-hypotheses, fresh credible source records, verified historical dissent/false-certainty metrics, all nine Chief reasoning steps, tail-risk scenarios and a permission request bound to the Chief reasoning hash. Chief reasoning must independently carry the exact same `signal_input_hash`; the pipeline rejects a missing or mismatched value. Its `reasoning_hash` must be supplied and match the canonical body before evaluation; runtime no longer creates a missing hash. This hash is an integrity binding, not a digital signature. The pipeline independently checks each verdict's input hash against the same signal snapshot. Missing data remains `GATHER_MORE`, a challenge deadlock escalates, and failed Chief/Permission checks remain HOLD/DENY.

The nine Chief steps have fixed semantic identifiers; the schema and runtime gate reject generic or swapped labels:

| Step | Required check |
|---|---|
| a | `ADVERSARIAL_PASS` — attempt to disprove the proposed trade |
| b | `CONTRADICTION_CHECK` — reconcile contradictions and conflicting evidence |
| c | `SOURCE_AUDIT` — inspect source quality, freshness and provenance |
| d | `TAIL_RISK_CHECK` — assess low-probability/high-impact loss |
| e | `RED_TEAM_SELF_REVIEW` — challenge the Chief's own reasoning |
| f | `FULL_CHALLENGE_LEDGER_REVIEW` — read every OPEN/RESOLVED objection, including Devil's Advocate challenges |
| g | `GROUPTHINK_CHECK` — inspect dissent and correlated agreement |
| h | `SIMILAR_REGIME_CALIBRATION` — compare calibration in similar market regimes |
| i | `ERROR_PROBABILITY_SCENARIO` — formulate an explicit error-probability scenario |

Each step also needs `passed=true`, a non-empty explanation and at least one evidence reference. These references must belong to the signal's declared evidence registry. A syntactically valid reference list is not, by itself, proof that the referenced artifact was independently captured; persisted source provenance is checked separately when `--persist` is used.

The CLI will not accept or process a supplied `user_decision`; it only evaluates the policy path and emits a human-review packet. `--persist` first reads the persisted kill-switch state and latest MT5 risk snapshot for the requested instrument, and requires the switch to be explicitly inactive plus a fresh demo snapshot whose request hash exactly matches instrument/direction/size/expected price/SL/TP. Missing, stale, denied or mismatched risk evidence makes the result `DENY`; the current MT5 collector leaves the news-window status unknown until a trusted calendar adapter is connected, so the persisted-risk path remains blocked on that missing evidence. On a valid risk check, `--persist` stores verified hash-chain events and the resulting decision in append-only PostgreSQL tables. It does not submit, queue, authorize or create an order. A `REQUIRE_HUMAN` result means only that the packet is ready for a separately authenticated operator review.

Do not fill missing fields with synthetic scores, fabricated source credibility, guessed reviewer records or hand-entered calibration labels. Synthetic fixtures are allowed only inside tests and must remain explicitly marked as test data.

## 5. MQL5 EA and demo forward

Run `python scripts/check_mql5_safety.py` for static source guard checks. They verify fail-closed defaults, demo-account validation, expiry, SL/TP, slippage and preflight checks. They do **not** compile the EA.

A human with the intended MetaTrader 5 terminal must compile `mql5/CriticalConsensusEA.mq5` in MetaEditor and attach it only to a demo account with the default `EnableDemoExecution=false`. Preserve that disabled default while compiling/smoke-testing. Record the MetaEditor build/version, compiler output, terminal build, broker, account mode, symbol specification and Journal/Experts logs. The EA checks live spread against `MaxSpreadPoints` before `OrderCheck`, checks the kill switch before polling and rechecks after `OrderCheck` immediately before the order attempt; this narrows but does not atomically eliminate a race with remote activation. Immediately before the order attempt it acquires an account-scoped terminal mutex using atomic `GlobalVariableSetOnCondition`, checks both the current account-bound marker and the prior `CC_TRIED_` marker format (upgrade compatibility), writes the new marker, calls `GlobalVariablesFlush()`, then releases the mutex before `OrderSend`. Unsupported or overlong IDs are rejected rather than truncated or normalized into colliding keys. A marker written before `OrderSend` intentionally blocks blind retry after an ambiguous/failed send and requires reconciliation. Terminal global variables are local to one MT5 terminal, are automatically deleted after four weeks without access, and do not coordinate separate terminal installations. Therefore this is not a durable cross-terminal idempotency guarantee: a production server-side atomic authorization/lease and PostgreSQL execution idempotency ledger remain unimplemented and mandatory before any real dispatch path. If and only if the EA has been reviewed and the bridge returns a schema-validated demo command, enable demo execution for the controlled test. `AllowLiveExecution=true` intentionally blocks the EA; there is no live-trading approval path in this scaffold.

Do not describe the EA as compiled or forward-tested until those host-side artifacts exist. Avoid placing credentials in logs, GitHub artifacts or screenshots.

## 6. Risk and kill switch

Capture a read-only terminal risk snapshot on the Windows MT5 host and persist it. The account-day baseline and equity high-water mark must be verified by the operator; if those are unavailable, the collector returns DENY instead of inventing drawdown. Supply a symbol-to-exposure-group JSON file covering every tradable symbol and its existing positions. The snapshot reads account equity/balance, open positions and current-day deal P/L, checks quote age, bid/ask spread and slippage, and binds the exact proposed order; it does not send orders. The initial 20-point spread cap must be reviewed against the selected broker's symbol specification and demo spread distribution; it is not empirically calibrated. The news-window field stays unknown because a news calendar is not integrated, so the risk-controlled permission check remains DENY until a verified news adapter is provided.

```powershell
$env:DATABASE_URL = "postgresql://USER:PASSWORD@DB_HOST:5432/mt5cc"
python scripts/capture_mt5_risk.py --symbol EURUSD --direction BUY --size 0.10 --expected-price 1.10010 --stop-loss 1.09910 --take-profit 1.10200 --day-start-equity 10000 --equity-high-watermark 10200 --max-spread-points 20 --symbol-groups .\\symbol-groups.json
```

The global emergency stop is persisted and active by default. Inspect it with authenticated `GET /v1/risk/kill-switch`; activate it with authenticated `POST /v1/risk/kill-switch`. Disabling requires both the bridge key and a separately configured `MT5_KILL_SWITCH_ADMIN_TOKEN` of at least 32 characters, supplied in `X-Kill-Switch-Admin`. Every switch change is added to an append-only hash-linked event log. The separate admin token is an operational guard, not a substitute for a proper TOTP/signature flow.

The risk-controlled permission endpoint requires an inactive persisted kill switch, a passing snapshot no older than 15 seconds, demo-account mode, operator-provided baselines, and a request hash matching the exact instrument/direction/size/expected price/SL/TP. Risk per trade is estimated from the MT5 symbol's tick size and tick value at capture time. The snapshot records measured spread and checks it against the configured cap, but it cannot protect against spread widening after capture, future fees or gap risk. Missing database, missing row, stale snapshot or unknown field is a deny. Before each demo order, the external execution service must also check broker volume/stop constraints and position reconciliation. The news-window field remains fail-closed while the calendar integration is absent.

Use a demo account only. Test at least: missing SL, missing TP, stale command, duplicate client order ID, adverse slippage, volume-step mismatch, closed market, rejected OrderCheck, network timeout after OrderSend and kill-switch activation. An ambiguous broker response requires reconciliation; never blindly retry.

## 7. End-to-end evidence / definitions

The added CI checks cover unit-level import validation, multi-agent timeout/identity/TTL handling, walk-forward math, PostgreSQL immutability/idempotency and Redis-to-PostgreSQL commit-before-ACK flow. The MQL5 source guard remains static-only.

Evidence labels:
- **VERIFIED:** reproducible test/log/artifact exists for that exact layer.
- **NOT PROVEN:** there is only source code, a configuration or a passing synthetic test without the real service/device/broker evidence.
- **PENDING EXTERNAL:** requires the Windows MetaTrader/MetaEditor and broker demo terminal.

The chain must be demonstrated in order: captured market data → durable prediction before outcome → independent-review evidence → chronological calibration report → risk-gate trace → human demo approval → MT5 demo command → broker result/position reconciliation. Passing API tests alone is not proof of a working MT5 trade or profitability.
