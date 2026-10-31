# MT5 Critical Consensus: full validation runbook

Status as of this change: production readiness is not implied. Do not connect a live account. The intended order is data → persistence → agent evidence → calibration → EA demo → end-to-end verification.

The Python scripts and API read process environment variables only; `.env` is consumed by Docker Compose, not loaded automatically by Python. Export the variables in the shell or inject them from a secret manager before running anything below.

## 1. Order-free market data

On the Windows host that already has MetaTrader 5 installed and logged in:

```powershell
python -m pip install -e ".[data]" MetaTrader5
$env:DATABASE_URL = "postgresql://USER:PASSWORD@127.0.0.1:5432/critical_consensus"
python scripts/import_mt5_history.py --live-terminal --symbol EURUSD --timeframe H1 --from-utc 2026-10-01T00:00:00Z --to-utc 2026-10-30T00:00:00Z --server-utc-offset-seconds 7200
```

The broker/server UTC offset in whole seconds is mandatory for `--live-terminal`: MT5 stamps bars in broker time, so defaulting to UTC would shift every imported bar. The MT5 connector only calls `initialize`, `symbol_select`, `copy_rates_range`, and `shutdown`; the script itself then applies migrations and appends bars to PostgreSQL. It contains no order submission calls. For CSV export, specify the broker/server timezone explicitly:

```powershell
python scripts/import_mt5_history.py --csv .\EURUSD_H1.csv --source-timezone Europe/Helsinki --symbol EURUSD --timeframe H1
```

Do not assume the broker's export timezone; verify it in MT5 first. Naive timestamps without an explicit timezone are rejected. The source CSV is read-only. OHLC rows are validated for finite prices, valid ranges, ordered unique UTC timestamps, symbol and timeframe consistency.

## 2. PostgreSQL and Redis

Install the optional `data` extra, set `DATABASE_URL` (required by `--migrate`) and `REDIS_URL`, then run:

```bash
python -m critical_consensus.persistence --migrate
python scripts/run_worker.py
```

The worker is a durable event writer for `cc:signals:v1`, not a trade executor. It acknowledges Redis entries only after the PostgreSQL transaction succeeds. A parse-invalid/poison event is written to `cc:dead-letter:v1` on first delivery and only then ACKed. A handler exception leaves the entry pending; the worker reclaims pending entries with `XAUTOCLAIM` (`reclaim_pending_messages`) on each loop, so a failing handler is retried up to `max_attempts=3` deliveries before it is dead-lettered and ACKed. The `processed_events` inbox protects replays; the same event ID with a different payload is rejected. Market bars and emitted forecast probabilities cannot be silently edited. `--migrate` applies the base schema and every ordered `db/migrations/*.sql` file in one transaction; it is forward-only and idempotent. Apply migrations with a dedicated database role and restrict production DB privileges; the sample integration role used by CI is intentionally more permissive than production should be. The database name in this runbook (`critical_consensus`) matches `POSTGRES_DB` in `docker-compose.yml`. DB-writing scripts (`run_worker.py`, `capture_mt5_risk.py`, `evaluate_signal_bundle.py --persist`, `import_mt5_history.py`, `run_agent_analysis.py`, `store_calibration_report.py`) call `migrate()` at startup; the read-only `export_resolved_forecasts.py` does not, so run `--migrate` once against a fresh database before exporting.

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
The template contains all nine mandatory IDs from `certainty_thresholds.yaml`. The runner checks that all nine identities are registered before it calls any model endpoint, and it requires four functional roles: analyst, critic, devil_advocate and risk_review. Each configured agent must have its own endpoint origin and unique model identity; duplicate origins/models are rejected at configuration level. The runner then reads an order-free market snapshot, calls the providers and collects signal-bound verdicts. When all nine pass, it invokes the configured providers again for anonymized peer reviews (two reviewers per verdict), validates citations against the signal evidence registry, registers each CHALLENGE and routes it to a registered resolver that is not the author of the challenged verdict (`make_non_author_challenge_resolver`), for at most three rounds. Missing/invalid reviews produce `GATHER_MORE`; unresolved challenges produce `ESCALATE`; only a complete evidence-backed ledger returns `READY_FOR_CHIEF`. The complete result is written to a PostgreSQL audit event and published to Redis when `REDIS_URL` is configured. This is still not a Certainty Gate decision: real walk-forward calibration and system-health metrics, contradiction scans, source ranking, portfolio/risk evidence, deterministic nine-step Chief reasoning and permission request are not fabricated by the runner. LLM confidence remains `UNCALIBRATED_SUBJECTIVE`.

`chief_review` here is an LLM reviewer role, not the deterministic `chief_skeptic_pass` gate. The full `evaluate_signal_bundle(...)` gate still requires the complete peer-review/challenge ledger, source and risk metadata, all nine Chief reasoning steps, permission binding and human authorization packet. The order-free runner does not auto-populate or bypass those prerequisites and never creates an order. Shared model families, vendors, prompts or market sources can still create correlated errors; role/group labels do not prove independence.

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

The bundle must arrive inside a signed HMAC-SHA256 evidence envelope, peer reviews/challenge ledger/Chief reasoning must arrive inside a separately signed `review` attestation, and the calibration metrics must arrive as a separately signed governance attestation. Generate four different secrets (32–256 bytes each) and export them plus their issuers:

```powershell
$env:MT5_EVIDENCE_HMAC_KEY = "<32-256 byte secret>"
$env:MT5_EVIDENCE_ISSUER   = "trusted-ingress"
$env:MT5_GOVERNANCE_HMAC_KEY = "<a different 32-256 byte secret>"
$env:MT5_GOVERNANCE_ISSUER = "calibration-service"
$env:MT5_OPERATOR_HMAC_KEY = "<a third 32-256 byte secret>"
$env:MT5_OPERATOR_ISSUER = "operator-console"
$env:MT5_REVIEW_HMAC_KEY = "<a fourth 32-256 byte secret>"
$env:MT5_REVIEW_ISSUER = "review-service"
$env:DATABASE_URL = "postgresql://USER:PASSWORD@DB_HOST:5432/critical_consensus"
python scripts/evaluate_signal_bundle.py .\trusted-decision-bundle.json --output .\decision-result.json --persist
```

Use `critical_consensus.evidence.sign_bundle` on the producer side; the signature covers the canonical JSON without `user_decision`/`user_decision_envelope`, so editing any field after signing fails verification with `EVIDENCE_*` blockers and the run degrades to `GATHER_MORE`. The review service must sign `{"signal_id", "verdicts", "peer_reviews", "challenge_records", "chief_reasoning"}` with `sign_review` and its own key/issuer; the ingress key cannot produce that attestation, and a missing, expired, foreign or tampered one fails with `REVIEW_*` blockers. CLI exit codes: `0` means the pipeline ran and returned `REQUIRE_HUMAN`, `2` means it ran and returned any other status, and `1` means the CLI could not complete (missing/invalid secrets, unreadable bundle, a supplied `user_decision`, or an unavailable database). Only exit `0` leaves a human-review packet; none of the codes creates an order. A human approval passed to the pipeline must be a separate `operator`-domain envelope signed by `MT5_OPERATOR_HMAC_KEY` with issuer `MT5_OPERATOR_ISSUER`; those values must differ from the evidence and governance key/issuer, and the signed decision must carry the exact `signal_id`, `chief_reasoning_hash`, `permission_request_sha256` (canonical hash of the approved order parameters) and, on the persisted-risk path, `risk_snapshot_sha256` (canonical hash of the terminal snapshot the risk gate validated). Missing configuration, a shared key/issuer or a mismatched binding yields `DENY` with `OPERATOR_*`/`USER_DECISION_*` blockers; a stale operator envelope is reported by the shared verifier as `EVIDENCE_*` blockers (evidence envelopes default to 300 s freshness with a 3600 s ceiling). Evidence signers therefore cannot approve their own trade. Governance attestations are built by the calibration service with its own key and expire after 24 hours by default; the pipeline rejects them if the issuer, signature, freshness window or metric types are wrong. The CLI derives `mandatory_agents` and `compliance_agent_id` from policy config, never from the bundle.

The bundle must include the exact immutable `signal_input` object, its `signal_input_hash`, the nine policy-mandatory agent verdicts, two validated peer reviews per verdict, a registered/resolved challenge ledger, at least three evidence-backed counter-hypotheses, fresh credible source records, all nine Chief reasoning steps, tail-risk scenarios and a permission request bound to the Chief reasoning hash and to the signal's instrument/direction. The pipeline independently checks each verdict's input hash against the same signal snapshot. Missing data remains `GATHER_MORE`, a challenge deadlock escalates, and failed Chief/Permission checks remain HOLD/DENY.

The CLI will not accept or process a supplied `user_decision`; it only evaluates the policy path and emits a human-review packet. Any separately authenticated operator approval must go through `user_authorization_gate` with a nonce store, where each nonce is consumed exactly once per operator (`authorization_nonces` in PostgreSQL, or the in-memory store for tests) and a replayed nonce is denied; an additional insert-only tombstone (`authorization_permissions`, migration 006) is keyed on a server-derived hash of the order (signal, instrument, direction, size, entry, SL, TP, mode — never the caller's `permission_id`), so renaming or re-issuing a permission cannot authorize the same order twice, and a missing permission store is denied rather than skipped. `--persist` first reads the persisted kill-switch state and latest MT5 risk snapshot for the requested instrument, and requires the switch to be explicitly inactive plus a fresh demo snapshot whose request hash exactly matches instrument/direction/size/expected price/SL/TP, whose symbol matches the order, whose account identity (`account_login`, `account_server`) is present and satisfies any policy pins, whose position aggregate is consistent (`position_symbols` length equals `open_positions`, `correlated_open_positions <= open_positions`, a positive open-stop risk with zero positions is rejected, and every open position has a fresh tick for its own symbol), and whose `risk_value`/`risk_fraction` (sized at the worse of reviewed and reference price over `min(account_equity, account_balance)`), daily loss, drawdown and `open_stop_risk_fraction` reconcile (1% tolerance) with the exact order risk and the captured equity baselines. Day-start equity must reconstruct `balance − daily_pnl` within 1% in both directions (an inflated baseline is rejected), and the equity high-water mark may not be below current equity or day-start equity. The snapshot must contain `open_stop_risk_fraction` as a JSON number in `[0, 1]`; a missing, quoted or `null` value is `DENY`, and the prospective caps add it to the realized daily-loss/drawdown fractions before comparing with the policy limits. Missing, stale, denied or mismatched risk evidence makes the result `DENY`; the current MT5 collector leaves the news-window status unknown until a trusted calendar adapter is connected, so the persisted-risk path remains blocked on that missing evidence. On a valid risk check, `--persist` stores verified hash-chain events and the resulting decision in append-only PostgreSQL tables. It does not submit, queue, authorize or create an order. A `REQUIRE_HUMAN` result means only that the packet is ready for a separately authenticated operator review.

Do not fill missing fields with synthetic scores, fabricated source credibility, guessed reviewer records or hand-entered calibration labels. Synthetic fixtures are allowed only inside tests and must remain explicitly marked as test data.

## 6. MQL5 EA and demo forward

Run `python scripts/check_mql5_safety.py` for static source guard checks (currently 426 checks, including 86 weakening mutations). They verify fail-closed defaults, demo-account validation, expiry (clock origin `TimeGMT()` with no offset), SL/TP bound to the order request, the validated volume and direction bound to `request.volume`/`request.type`, slippage and preflight checks. They also require the strict loopback URL parser (exact `127.0.0.1` or `localhost` host, explicit numeric port, no userinfo/query/fragment characters, no whitespace or non-ASCII), only the two validated URL inputs reaching `WebRequest`, a non-empty header-safe `BridgeToken` (24–256 characters of `[A-Za-z0-9._~-]`, validated at init and before it is placed in a header), exact `JsonTrue`/`JsonFalse` wrappers, a compile-time `const bool ENABLE_DEMO_EXECUTION = false` that no input or preset can enable, and both idempotency tombstones persisted and re-checked with `GlobalVariableCheck()` before `OrderSend`. `GlobalVariablesFlush()` returns `void` in the installed compiler (terminal build 6296), so it cannot gate the send: the checker rejects any source that uses its result as a value, and durable idempotency remains the bridge's exact-ID record rather than this in-memory tombstone. They do **not** compile the EA.

The idempotency tombstone lives in terminal global variables, which are flushed to disk but are not transactional across a terminal crash. The bridge's PostgreSQL `client_order_id`/`command_id` record and post-trade position reconciliation remain authoritative; an uncertain or timed-out send is never retried blindly.

Trust model for the local listener: the EA authenticates the bridge with `BridgeToken` over plain HTTP to a loopback address, so any other local process that can bind or intercept `127.0.0.1`/`localhost` (or a misconfigured proxy) could answer in its place. Keep the bridge on the same host as the terminal, bind it only to loopback, disable system/terminal HTTP proxies, and treat every local login as fully trusted. This is not a network or mutual-TLS boundary.

`scripts/build_demo_ea.py` performs the deliberate build step instead of a hand edit: it copies `mql5/CriticalConsensusEA.mq5`, flips exactly the one line `const bool ENABLE_DEMO_EXECUTION = false;` to `true`, refuses if the reviewed source shape no longer matches, optionally compiles with the installed MetaEditor and installs the result. MetaEditor's process exit code is not reliable, so success is decided by the "Result: N errors, M warnings" line in its log. `scripts/setup_demo_terminal.py` wraps the whole host preparation: it refuses to touch anything while `terminal64.exe`/`metatester64.exe` runs (MT5 rewrites `Config/common.ini` on exit, so a patch applied while it runs is silently lost), requires exactly one `[Experts]` section with exactly one each of `Enabled`, `WebRequest` and `WebRequestUrl`, keeps the file's UTF-16 LE BOM and CRLF endings, extends an existing URL allowlist rather than replacing it unless `--force-url`, writes through a temporary file plus `os.replace`, keeps one timestamped backup that it never overwrites, and re-reads the bytes to verify. It is a dry run unless `--apply` is passed, and it never decides anything about an order.

Host evidence recorded on 2026-10-31 (MetaQuotes-Demo, terminal build 6296, MetaTrader5 5.0.6231): `python scripts/setup_demo_terminal.py --apply` compiled the demo EA with **0 errors and 0 warnings**, installed `MQL5/Experts/CriticalConsensusEA_Demo.ex5` (37780 bytes, its generated source carrying `ENABLE_DEMO_EXECUTION = true` and `AllowLiveExecution = false`), and left `Config/common.ini` with `Enabled=1`, `WebRequest=1`, `WebRequestUrl=http://127.0.0.1:8000` next to a byte-identical backup. The repository source itself still carries `false`, and `scripts/check_mql5_safety.py` keeps it that way.

Still NOT PROVEN after that: the advisor polling the bridge. Nothing reached `MQL5/Logs` from the demo EA, because attaching an expert advisor to a chart is a manual step in the terminal GUI (Navigator → `CriticalConsensusEA_Demo` → drag onto a chart → set `BridgeUrl`, `KillSwitchUrl`, paste `BridgeToken` → keep the AutoTrading button on), and `WebRequest()` cannot execute in the Strategy Tester at all (documented MetaQuotes limitation), so no tester run can ever substitute for that attach. A GUI toggle of AutoTrading also rewrites `[Experts] Enabled=0` and can clear `WebRequestUrl`, so re-run `setup_demo_terminal.py --apply --skip-build` after any manual switch.

### 6.1 Demo execution end to end

Order-free decisioning first, then the only component that may create an order:

```powershell
python scripts/run_mt5_agent.py --db .\agent_data\mt5_agent.db run --policy .\config\mt5_agent.yaml --source live --language ru
$env:MT5_BRIDGE_TOKEN = "<24-256 characters from [A-Za-z0-9._~-]>"
python scripts/run_mt5_demo_trading.py --policy .\agent_data\live_demo_policy.yaml --db .\agent_data\mt5_demo.db --symbols all --cycles 0 --execution confirm --verbose
```

The runner starts the loopback bridge itself (it binds `127.0.0.1`, serves only the two GETs, holds one pending command, applies a 5–600 s TTL and fsyncs a tombstone before it serves a command), refuses a policy that does not require a demo account, refuses any account the terminal reports as non-demo, and reconciles filled orders against the broker's own deal history by magic number `8172026` and the `CC:` comment prefix. A command that was served without an observed fill ends the run and needs `--acknowledge-uncertain` before that order id is touched again; there is no retry path. `--symbols all` walks a curated universe and asks the live terminal for each specification, so gold and indices are sized from broker contract data and never from a hardcoded guess; a symbol the broker cannot serve is named in the journal with the terminal's own reason (`MT5 could not select symbol …`, or `pays profit in JPY, not in the account currency USD`).

Observed live run, 2026-10-31, thirteen requested instruments: seven available (EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD, XAGUSD, US500), six refused before the cycle for the reasons above; all seven blocked inside the cycle (`QUOTE_STALE` on the six with weekend-stale ticks — the last tick was 2026-10-30T21:00:00Z — and `MARKET_DATA_READ_FAILED` for `MT5 returned no tick for US500`). Zero signals, zero commands queued, zero filled, `broker_order_sent: false` in every record, `audit_chain_valid: true`. Quote age cannot be relaxed past 300 seconds by policy, so a weekend can never produce a command. On that account (balance 10.00 USD) the sizing arithmetic also shows the demo order could not have been funded at the policy's 0.5% risk: the risk budget is 0.05 USD while the broker's minimum lot risks 1.93–4.24 USD on those symbols, i.e. `MINIMUM_LOT_EXCEEDS_RISK_BUDGET` — a larger demo balance is a precondition for a real execution test, not a config to argue with.

Do not describe the EA as compiled until the MetaEditor log line and the installed `.ex5` exist, and do not describe a demo trade as executed until a broker deal with that magic number and comment is visible in the terminal's own history. Avoid placing credentials, bridge tokens or screenshots of the account panel in logs or artifacts.

## 7. Risk and kill switch

Capture an order-free terminal risk snapshot on the Windows MT5 host and persist it (it may add the symbol to Market Watch; it sends no order). The account-day baseline and equity high-water mark must be verified by the operator; if those are unavailable, the collector returns DENY instead of inventing drawdown. Supply a symbol-to-exposure-group JSON file covering every tradable symbol and its existing positions; `config/symbol-groups.example.json` is a starting point to copy and extend. MT5 reports tick and deal timestamps in broker server wall-clock time, not UTC, so `--server-utc-offset-seconds` is mandatory and expressed in seconds, must be a whole number of minutes (multiple of 60) within ±14 hours, e.g. `10800` for UTC+3. The collector subtracts that offset before comparing the quote against UTC and uses it for the broker's midnight day boundary; a missing or malformed offset fails closed with `MT5_SERVER_UTC_OFFSET_MISSING_OR_INVALID` before MT5 is initialized. The snapshot reads account equity/balance, account login/server, open positions and current-day deal P/L; it does not send orders. The account login and server are taken from `account_info()` and are mandatory (a missing login or blank server fails closed). Risk is sized at the worse of the reviewed and current reference price over `min(equity, balance)`, a fill drifting more than 10% of the stop distance is rejected, and starting equity + realised P/L must reconstruct the current balance (1% tolerance) before the snapshot passes. Each open position requires a fresh tick for its own symbol; a stale or missing per-symbol quote yields `OPEN_POSITION_STOP_RISK_UNKNOWN` rather than under-reporting stop risk as zero. The news-window field stays unknown because a news calendar is not integrated, so the risk-controlled permission check remains DENY until a verified news adapter is provided.

```powershell
$env:DATABASE_URL = "postgresql://USER:PASSWORD@DB_HOST:5432/critical_consensus"
python scripts/capture_mt5_risk.py --symbol EURUSD --direction BUY --size 0.10 --expected-price 1.10010 --stop-loss 1.09910 --take-profit 1.10200 --server-utc-offset-seconds 10800 --day-start-equity 10000 --equity-high-watermark 10200 --symbol-groups .\\symbol-groups.json
```

The global emergency stop is persisted and active by default. Inspect it with authenticated `GET /v1/risk/kill-switch`; activate it with authenticated `POST /v1/risk/kill-switch`. Activating only requires the bridge key because it moves the system toward the fail-closed state; disabling additionally requires a separately configured `MT5_KILL_SWITCH_ADMIN_TOKEN` of at least 32 characters, supplied in `X-Kill-Switch-Admin`. Every switch change is added to an append-only hash-linked event log. The separate admin token is an operational guard, not a substitute for a proper TOTP/signature flow.

The risk-controlled permission endpoint requires an inactive persisted kill switch, a passing snapshot no older than 15 seconds, demo-account mode, operator-provided baselines, and a request hash matching the exact instrument/direction/size/expected price/SL/TP. It then recomputes `size × |worst_entry − SL| / tick_size × tick_value_loss` at the worse of the reviewed and reference price and the resulting fraction over `min(account_equity, account_balance)`, recomputes daily loss and drawdown from the captured equity baselines, and denies with `RISK_*_DOES_NOT_MATCH_*`/`RISK_SNAPSHOT_SYMBOL_MISMATCH` if the persisted aggregates disagree beyond 1%, the tick specification is missing, the account identity is missing or mismatches a policy pin, the position aggregate is inconsistent (`position_symbols` length ≠ `open_positions`, `correlated_open_positions > open_positions`, or a positive open-stop risk with zero positions), the equity high-water mark is below current equity or the day-start equity, the day-start equity cannot reconstruct `balance − daily_pnl` beyond 1%, a fraction is non-finite, the reference price is missing, slippage exceeds 10% of the stop distance, the reference price crosses SL/TP, or the symbol group is empty. Daily-loss and drawdown caps are prospective: the new trade's risk fraction is added to the current loss/drawdown before comparing with the limit. Reward/risk is applied unconditionally (including flat snapshots): it must clear `min_expected_rr` at the conservative entry, after the value is recomputed from SL/TP (1% tolerance). Risk per trade excludes future fees, spread changes and gap risk. Missing database, missing row, stale snapshot or unknown field is a deny. Before each demo order, the external execution service must also check broker volume/stop constraints and position reconciliation. The news-window field remains fail-closed while the calendar integration is absent.

Use a demo account only. Test at least: missing SL, missing TP, stale command, duplicate client order ID, adverse slippage, volume-step mismatch, closed market, rejected OrderCheck, network timeout after OrderSend and kill-switch activation. An ambiguous broker response requires reconciliation; never blindly retry.

## 8. End-to-end evidence / definitions

The added CI checks cover unit-level import validation, multi-agent timeout/identity/TTL handling, walk-forward math, PostgreSQL immutability/idempotency and Redis-to-PostgreSQL commit-before-ACK flow (valid events ACK after commit; poison events are DLQ'd then ACKed; handler failures stay pending). These integration checks run only when `TEST_DATABASE_URL`/`TEST_REDIS_URL` are configured and are skipped otherwise. The MQL5 source guard remains static-only.

Evidence labels:
- **VERIFIED:** reproducible test/log/artifact exists for that exact layer.
- **NOT PROVEN:** there is only source code, a configuration or a passing synthetic test without the real service/device/broker evidence.
- **PENDING EXTERNAL:** requires the Windows MetaTrader/MetaEditor and broker demo terminal. On the 2026-10-31 host this label is only partly satisfied: the terminal, MetaEditor, the `MetaTrader5` package and a logged-in MetaQuotes-Demo account are present and were read (see section 6.1), while PostgreSQL/Redis were not exercised there, so persistence claims on that host remain PENDING EXTERNAL.

The chain must be demonstrated in order: captured market data → durable prediction before outcome → independent-review evidence → chronological calibration report → risk-gate trace → human demo approval → MT5 demo command → broker result/position reconciliation. Passing API tests alone is not proof of a working MT5 trade or profitability. On the 2026-10-31 host the chain stops between "MT5 demo command" and "broker result": the command layer is built, compiled and installed, but no command was ever queued (market closed, stale quotes) and no broker deal exists.
