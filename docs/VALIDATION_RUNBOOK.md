# MT5 Critical Consensus: full validation runbook

Status as of this change: infrastructure and tests are being added; production readiness is not implied. Do not connect a live account. The intended order is data → persistence → agent evidence → calibration → EA demo → end-to-end verification.

## 1. Read-only market data

On the Windows host that already has MetaTrader 5 installed and logged in:

```powershell
python -m pip install -e ".[data]" MetaTrader5
$env:DATABASE_URL = "postgresql://USER:PASSWORD@127.0.0.1:5432/critical_consensus"
python scripts/import_mt5_history.py --live-terminal --symbol EURUSD --timeframe H1 --from-utc 2026-10-01T00:00:00Z --to-utc 2026-10-30T00:00:00Z
```

The connector only calls `initialize`, `symbol_select`, `copy_rates_range`, and `shutdown`. It contains no order submission calls. For CSV export, specify the broker/server timezone explicitly:

```powershell
python scripts/import_mt5_history.py --csv .\EURUSD_H1.csv --source-timezone Europe/Helsinki --symbol EURUSD --timeframe H1
```

Do not assume the broker's export timezone; verify it in MT5 first. Naive timestamps without an explicit timezone are rejected. The source CSV is read-only. OHLC rows are validated for finite prices, valid ranges, ordered unique UTC timestamps, symbol and timeframe consistency.

## 2. PostgreSQL and Redis

Install the optional `data` extra, set `DATABASE_URL` and `REDIS_URL`, then run:

```bash
python -m critical_consensus.persistence --migrate
python scripts/run_worker.py
```

The worker is a durable event writer for `cc:signals:v1`, not a trade executor. It acknowledges Redis entries only after the PostgreSQL transaction succeeds; an invalid/poison event is written to `cc:dead-letter:v1` and only then ACKed, while a handler exception leaves the entry pending for retry. The `processed_events` inbox protects replays; the same event ID with a different payload is rejected. Market bars and emitted forecast probabilities cannot be silently edited. `--migrate` applies the base schema and every ordered `db/migrations/*.sql` file in one transaction, and every DB-backed script also calls it at startup; migrations are forward-only and idempotent. Apply migrations with a dedicated database role and restrict production DB privileges; the sample integration role used by CI is intentionally more permissive than production should be. The database name in this runbook (`critical_consensus`) matches `POSTGRES_DB` in `docker-compose.yml`.

## 3. Agent consensus

For a controlled provider test, define provider metadata with environment variable names only; API secrets themselves belong in the environment/secret manager. For example:

`scripts/run_agent_analysis.py` requires a reachable `DATABASE_URL`: it applies migrations, persists the collection event locally, and only then publishes to Redis when `REDIS_URL` is set. Exit codes: `0` means `READY_FOR_CHIEF` and a published Redis event, `2` means the review ledger is incomplete (`GATHER_MORE`/`ESCALATE`), and `3` means the ledger is complete but Redis publication failed or was not configured. None of these codes authorizes a trade.

```powershell
$env:MT5_AGENT_KEY_PLATFORM = "<set-in-your-secret-manager>"
$env:MT5_AGENT_KEY_NEWS = "<set-in-your-secret-manager>"
$env:MT5_AGENT_KEY_TECHNICAL = "<set-in-your-secret-manager>"
$env:MT5_AGENT_KEY_RISK = "<set-in-your-secret-manager>"
$env:MT5_AGENT_KEY_COMPLIANCE = "<set-in-your-secret-manager>"
$env:MT5_AGENT_KEY_CONTRADICTION = "<set-in-your-secret-manager>"
$env:MT5_AGENT_KEY_DEVIL = "<set-in-your-secret-manager>"
$env:MT5_AGENT_KEY_PORTFOLIO = "<set-in-your-secret-manager>"
$env:MT5_AGENT_KEY_QUANT = "<set-in-your-secret-manager>"
$env:MT5_AGENT_PROVIDERS = '[{"agent_id":"PlatformScoutAgent","role":"analyst","independence_group":"vendor-platform","model":"YOUR_PLATFORM_MODEL","api_key_env":"MT5_AGENT_KEY_PLATFORM","base_url":"https://platform-provider.example/v1"},{"agent_id":"GlobalNewsAgent","role":"analyst","independence_group":"vendor-news","model":"YOUR_NEWS_MODEL","api_key_env":"MT5_AGENT_KEY_NEWS","base_url":"https://news-provider.example/v1"},{"agent_id":"TechnicalAnalystAgent","role":"analyst","independence_group":"vendor-technical","model":"YOUR_TECHNICAL_MODEL","api_key_env":"MT5_AGENT_KEY_TECHNICAL","base_url":"https://technical-provider.example/v1"},{"agent_id":"RiskManagerAgent","role":"risk_review","independence_group":"vendor-risk","model":"YOUR_RISK_MODEL","api_key_env":"MT5_AGENT_KEY_RISK","base_url":"https://risk-provider.example/v1"},{"agent_id":"ComplianceAgent","role":"critic","independence_group":"vendor-compliance","model":"YOUR_COMPLIANCE_MODEL","api_key_env":"MT5_AGENT_KEY_COMPLIANCE","base_url":"https://compliance-provider.example/v1"},{"agent_id":"ContradictionAgent","role":"critic","independence_group":"vendor-contradiction","model":"YOUR_CONTRADICTION_MODEL","api_key_env":"MT5_AGENT_KEY_CONTRADICTION","base_url":"https://contradiction-provider.example/v1"},{"agent_id":"DevilAdvocateAgent","role":"devil_advocate","independence_group":"vendor-devil","model":"YOUR_DEVIL_ADVOCATE_MODEL","api_key_env":"MT5_AGENT_KEY_DEVIL","base_url":"https://devil-advocate-provider.example/v1"},{"agent_id":"PortfolioAgent","role":"analyst","independence_group":"vendor-portfolio","model":"YOUR_PORTFOLIO_MODEL","api_key_env":"MT5_AGENT_KEY_PORTFOLIO","base_url":"https://portfolio-provider.example/v1"},{"agent_id":"QuantModelAgent","role":"analyst","independence_group":"vendor-quant","model":"YOUR_QUANT_MODEL","api_key_env":"MT5_AGENT_KEY_QUANT","base_url":"https://quant-provider.example/v1"}]'
python scripts/run_agent_analysis.py --symbol EURUSD --timeframe H1 --bars 200
```

Each provider must have a unique configured HTTPS endpoint origin and unique endpoint/model pair. The runtime blocks duplicate origins even when declared group names differ. This is only a configuration-diversity check; statistical independence of training data and model behaviour remains NOT PROVEN and requires an external audit.
The template contains all nine mandatory IDs from `certainty_thresholds.yaml`. The runner checks that all nine identities are registered before it calls any model endpoint, and it requires four functional roles: analyst, critic, devil_advocate and risk_review. Each configured agent must have its own endpoint origin and unique model identity; duplicate origins/models are rejected at configuration level. The runner then reads a read-only market snapshot, calls the providers and collects signal-bound verdicts. When all nine pass, it invokes the configured providers again for anonymized peer reviews (two reviewers per verdict), validates citations against the signal evidence registry, registers each CHALLENGE and asks the DevilAdvocate provider to resolve it in at most three rounds. Missing/invalid reviews produce `GATHER_MORE`; unresolved challenges produce `ESCALATE`; only a complete evidence-backed ledger returns `READY_FOR_CHIEF`. The complete result is written to a PostgreSQL audit event and published to Redis when `REDIS_URL` is configured. This is still not a Certainty Gate decision: real walk-forward calibration and system-health metrics, contradiction scans, source ranking, portfolio/risk evidence, deterministic nine-step Chief reasoning and permission request are not fabricated by the runner. LLM confidence remains `UNCALIBRATED_SUBJECTIVE`.

`chief_review` here is an LLM reviewer role, not the deterministic `chief_skeptic_pass` gate. The full `evaluate_signal_bundle(...)` gate still requires the complete peer-review/challenge ledger, source and risk metadata, all nine Chief reasoning steps, permission binding and human authorization packet. The read-only runner does not auto-populate or bypass those prerequisites and never creates an order. Shared model families, vendors, prompts or market sources can still create correlated errors; role/group labels do not prove independence.

Register actual model/provider callables using `AgentSpec` and `collect_agent_verdicts`. Set the required number of distinct providers and independent groups explicitly. A provider must return signal-bound verdict ID, agent ID, SHA-256 input binding, UTC creation time, positive TTL and evidence references. Errors, timeouts and invalid output cause `GATHER_MORE`.

The `independence_group` value is declared metadata only. It is **not proof** that models, training data, vendors, prompts or market inputs are independent. Production onboarding must document and review those dependencies. The existing policy pipeline remains the only place where gate sequencing is evaluated; collection success never authorizes a trade.

## 4. Walk-forward calibration

Capture the model forecast before its outcome is available, persist the exact issue timestamp and canonical input hash, then resolve it once the outcome is known. Export rows to a CSV with columns:

```csv
model_id,signal_id,issued_at,resolved_at,probability,outcome
technical-v1,eurusd-h1-001,2026-10-01T10:00:00Z,2026-10-01T11:00:00Z,0.61,1
```

Then run:

```bash
python scripts/evaluate_calibration.py predictions.csv --output calibration-report.json
```

The evaluator fits Platt scaling only on earlier forecasts whose outcomes were already resolved before the next test block. It reports Brier score, log loss, reliability bins, ECE, high-confidence false-certainty rate, sample counts and a deterministic paired bootstrap interval. With too few observations or an interval that does not show improvement, the result remains `NOT_PROVEN`. The script cannot independently prove that a supplied historical forecast was genuinely captured before the outcome; only a trusted append-only capture pipeline and audit trail can provide that provenance.

This is probability calibration, not a strategy/backtest profitability test. A calibrated probability does not prove positive expectancy, execution quality or future profit.

## 5. Full deterministic Chief Gate

Once the upstream services have produced a trusted evidence packet, evaluate the entire deterministic path (verdict metadata → peer review → challenge ledger → Certainty Gate → nine-step ChiefAgent → Permission Gate → human-review packet):

The bundle must arrive inside a signed HMAC-SHA256 evidence envelope and the calibration metrics must arrive as a separately signed governance attestation. Generate two different secrets (32–256 bytes each) and export them plus their issuers:

```powershell
$env:MT5_EVIDENCE_HMAC_KEY = "<32-256 byte secret>"
$env:MT5_EVIDENCE_ISSUER   = "decision-bundle-service"
$env:MT5_GOVERNANCE_HMAC_KEY = "<a different 32-256 byte secret>"
$env:MT5_GOVERNANCE_ISSUER = "calibration-service"
$env:MT5_OPERATOR_HMAC_KEY = "<a third 32-256 byte secret>"
$env:MT5_OPERATOR_ISSUER = "operator-console"
$env:DATABASE_URL = "postgresql://USER:PASSWORD@DB_HOST:5432/critical_consensus"
python scripts/evaluate_signal_bundle.py .\trusted-decision-bundle.json --output .\decision-result.json --persist
```

Use `critical_consensus.evidence.sign_bundle` on the producer side; the signature covers the canonical JSON without `user_decision`/`user_decision_envelope`, so editing any field after signing fails verification with `EVIDENCE_*` blockers and the run degrades to `GATHER_MORE`. A human approval passed to the pipeline must be a separate `operator`-domain envelope signed by `MT5_OPERATOR_HMAC_KEY` with issuer `MT5_OPERATOR_ISSUER`; those values must differ from the evidence and governance key/issuer, and the signed decision must carry the exact `signal_id` and `chief_reasoning_hash`. Missing configuration, a shared key/issuer, a stale signature or a mismatched binding yields `DENY` with `OPERATOR_*`/`USER_DECISION_*` blockers. Evidence signers therefore cannot approve their own trade. Governance attestations are built by the calibration service with its own key and expire after 24 hours; the pipeline rejects them if the issuer, signature, freshness window or metric types are wrong. The CLI derives `mandatory_agents` and `compliance_agent_id` from policy config, never from the bundle.

The bundle must include the exact immutable `signal_input` object, its `signal_input_hash`, the nine policy-mandatory agent verdicts, two validated peer reviews per verdict, a registered/resolved challenge ledger, at least three evidence-backed counter-hypotheses, fresh credible source records, all nine Chief reasoning steps, tail-risk scenarios and a permission request bound to the Chief reasoning hash and to the signal's instrument/direction. The pipeline independently checks each verdict's input hash against the same signal snapshot. Missing data remains `GATHER_MORE`, a challenge deadlock escalates, and failed Chief/Permission checks remain HOLD/DENY.

The CLI will not accept or process a supplied `user_decision`; it only evaluates the policy path and emits a human-review packet. Any separately authenticated operator approval must go through `user_authorization_gate` with a nonce store, where each nonce is consumed exactly once (`authorization_nonces` in PostgreSQL, or the in-memory store for tests) and a replayed nonce is denied. `--persist` first reads the persisted kill-switch state and latest MT5 risk snapshot for the requested instrument, and requires the switch to be explicitly inactive plus a fresh demo snapshot whose request hash exactly matches instrument/direction/size/expected price/SL/TP, whose symbol matches the order, and whose `risk_value`/`risk_fraction`, daily loss and drawdown reconcile (1% tolerance) with the exact order risk and the captured equity baselines. Missing, stale, denied or mismatched risk evidence makes the result `DENY`; the current MT5 collector leaves the news-window status unknown until a trusted calendar adapter is connected, so the persisted-risk path remains blocked on that missing evidence. On a valid risk check, `--persist` stores verified hash-chain events and the resulting decision in append-only PostgreSQL tables. It does not submit, queue, authorize or create an order. A `REQUIRE_HUMAN` result means only that the packet is ready for a separately authenticated operator review.

Do not fill missing fields with synthetic scores, fabricated source credibility, guessed reviewer records or hand-entered calibration labels. Synthetic fixtures are allowed only inside tests and must remain explicitly marked as test data.

## 6. MQL5 EA and demo forward

Run `python scripts/check_mql5_safety.py` for static source guard checks. They verify fail-closed defaults, demo-account validation, expiry, SL/TP, slippage and preflight checks. They also require the strict loopback URL parser (exact `127.0.0.1` or `localhost` host, explicit numeric port, no userinfo/query/fragment characters, no whitespace or non-ASCII), a header-safe `BridgeToken` (24–256 characters of `[A-Za-z0-9._~-]`, validated before it is placed in a header), and an idempotency tombstone written and flushed before `OrderSend`. They do **not** compile the EA.

The idempotency tombstone lives in terminal global variables, which are flushed to disk but are not transactional across a terminal crash. The bridge's PostgreSQL `client_order_id`/`command_id` record and post-trade position reconciliation remain authoritative; an uncertain or timed-out send is never retried blindly.

A human with the intended MetaTrader 5 terminal must compile `mql5/CriticalConsensusEA.mq5` in MetaEditor and attach it only to a demo account with the default `EnableDemoExecution=false`. Preserve that disabled default while compiling/smoke-testing. Record the MetaEditor build/version, compiler output, terminal build, broker, account mode, symbol specification and Journal/Experts logs. If and only if the EA has been reviewed and the bridge returns a schema-validated demo command, enable demo execution for the controlled test. `AllowLiveExecution=true` intentionally blocks the EA; there is no live-trading approval path in this scaffold.

Do not describe the EA as compiled or forward-tested until those host-side artifacts exist. Avoid placing credentials in logs, GitHub artifacts or screenshots.

## 7. Risk and kill switch

Capture a read-only terminal risk snapshot on the Windows MT5 host and persist it. The account-day baseline and equity high-water mark must be verified by the operator; if those are unavailable, the collector returns DENY instead of inventing drawdown. Supply a symbol-to-exposure-group JSON file covering every tradable symbol and its existing positions. MT5 reports tick and deal timestamps in broker server wall-clock time, not UTC, so `--server-utc-offset-seconds` is mandatory and expressed in seconds, must be a whole number of minutes (multiple of 60) within ±14 hours, e.g. `10800` for UTC+3. The collector subtracts that offset before comparing the quote against UTC and uses it for the broker's midnight day boundary; a missing or malformed offset fails closed with `MT5_SERVER_UTC_OFFSET_MISSING_OR_INVALID` before MT5 is initialized. The snapshot reads account equity/balance, open positions and current-day deal P/L; it does not send orders. The news-window field stays unknown because a news calendar is not integrated, so the risk-controlled permission check remains DENY until a verified news adapter is provided.

```powershell
$env:DATABASE_URL = "postgresql://USER:PASSWORD@DB_HOST:5432/critical_consensus"
python scripts/capture_mt5_risk.py --symbol EURUSD --direction BUY --size 0.10 --expected-price 1.10010 --stop-loss 1.09910 --take-profit 1.10200 --server-utc-offset-seconds 10800 --day-start-equity 10000 --equity-high-watermark 10200 --symbol-groups .\\symbol-groups.json
```

The global emergency stop is persisted and active by default. Inspect it with authenticated `GET /v1/risk/kill-switch`; activate it with authenticated `POST /v1/risk/kill-switch`. Disabling requires both the bridge key and a separately configured `MT5_KILL_SWITCH_ADMIN_TOKEN` of at least 32 characters, supplied in `X-Kill-Switch-Admin`. Every switch change is added to an append-only hash-linked event log. The separate admin token is an operational guard, not a substitute for a proper TOTP/signature flow.

The risk-controlled permission endpoint requires an inactive persisted kill switch, a passing snapshot no older than 15 seconds, demo-account mode, operator-provided baselines, and a request hash matching the exact instrument/direction/size/expected price/SL/TP. It then recomputes `size × |entry − SL| / tick_size × tick_value_loss` and the resulting fraction of equity, recomputes daily loss and drawdown from the captured equity baselines, and denies with `RISK_*_DOES_NOT_MATCH_*`/`RISK_SNAPSHOT_SYMBOL_MISMATCH` if the persisted aggregates disagree beyond 1% or the tick specification is missing. Risk per trade excludes future fees, spread changes and gap risk. Missing database, missing row, stale snapshot or unknown field is a deny. Before each demo order, the external execution service must also check broker volume/stop constraints and position reconciliation. The news-window field remains fail-closed while the calendar integration is absent.

Use a demo account only. Test at least: missing SL, missing TP, stale command, duplicate client order ID, adverse slippage, volume-step mismatch, closed market, rejected OrderCheck, network timeout after OrderSend and kill-switch activation. An ambiguous broker response requires reconciliation; never blindly retry.

## 8. End-to-end evidence / definitions

The added CI checks cover unit-level import validation, multi-agent timeout/identity/TTL handling, walk-forward math, PostgreSQL immutability/idempotency and Redis-to-PostgreSQL commit-before-ACK flow (valid events ACK after commit; poison events are DLQ'd then ACKed; handler failures stay pending). These integration checks run only when `TEST_DATABASE_URL`/`TEST_REDIS_URL` are configured and are skipped otherwise. The MQL5 source guard remains static-only.

Evidence labels:
- **VERIFIED:** reproducible test/log/artifact exists for that exact layer.
- **NOT PROVEN:** there is only source code, a configuration or a passing synthetic test without the real service/device/broker evidence.
- **PENDING EXTERNAL:** requires the Windows MetaTrader/MetaEditor and broker demo terminal.

The chain must be demonstrated in order: captured market data → durable prediction before outcome → independent-review evidence → chronological calibration report → risk-gate trace → human demo approval → MT5 demo command → broker result/position reconciliation. Passing API tests alone is not proof of a working MT5 trade or profitability.
