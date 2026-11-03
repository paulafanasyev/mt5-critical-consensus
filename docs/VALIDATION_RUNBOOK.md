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

Run `python scripts/check_mql5_safety.py` for static source guard checks (currently 473 checks, including 105 mutation rejections). They verify fail-closed defaults, demo-account validation, expiry (clock origin `TimeGMT()` with no offset), SL/TP bound to the order request, the validated volume and direction bound to `request.volume`/`request.type`, slippage and preflight checks. They also require the strict loopback URL parser (exact `127.0.0.1` or `localhost` host, explicit numeric port, no userinfo/query/fragment characters, no whitespace or non-ASCII), only the two validated URL inputs reaching `WebRequest`, a non-empty header-safe `BridgeToken` (24–256 characters of `[A-Za-z0-9._~-]`, validated at init and before it is placed in a header), exact `JsonTrue`/`JsonFalse` wrappers, a compile-time `const bool ENABLE_DEMO_EXECUTION = false` that no input or preset can enable, and both idempotency tombstones persisted and re-checked with `GlobalVariableCheck()` before `OrderSend`. `GlobalVariablesFlush()` returns `void` in the installed compiler (terminal build 6296), so it cannot gate the send: the checker rejects any source that uses its result as a value, and durable idempotency remains the bridge's exact-ID record rather than this in-memory tombstone. They do **not** compile the EA.

Static source checks cannot say whether the two sides agree arithmetically, so the runtime mirror is a separate command: `python scripts/ea_logic_mirror.py` prints eight self-checks of a line-for-line Python port of `ExecuteDemoCommand` (same flat-JSON parser, same number/identifier/token grammar, the same FNV-1a over UTF-16LE bytes, the same loopback URL parser, the same ordered guards with the advisor's own refusal strings), and `python -m pytest tests/test_ea_logic_parity.py -q` runs 109 comparisons of that port against a live bridge's own socket bodies, `json.loads` with `NaN`/`Infinity` rejected, the bridge's regexes, and the `.mq5` text itself (required fields, defaults, ceilings, magic number). One comparison is a 200-point price grid where the stop distance and the broker's `stops_level` round in opposite directions; it is what found a real divergence on the Python side — `price_gates` measured an unrounded stop against a two-point level while the broker receives rounded numbers, so at price 4000.005 with a 1.02-point stop Python blocked a command the advisor would have accepted. The runner now rounds entry, stop and target with `broker_level()` (half away from zero, identical to `NormalizeDouble`) before every geometry, slippage and stops-level check. A guard on a number the broker never receives is not a guard.

The full Python suite is the other half of the evidence: `.venv\Scripts\python.exe -m pytest -q` — **1038 passed, 10 skipped** on 2026-11-02; every skip requires `TEST_DATABASE_URL` and/or `TEST_REDIS_URL`, which are not configured on this host. Use the venv interpreter: several tests re-exec `sys.executable` as a subprocess, so running the suite with a system Python that has no `critical_consensus` installed yields 36 `ModuleNotFoundError: No module named 'critical_consensus'` failures — an environment artifact, not a regression, and easy to misread as one.

The idempotency tombstone lives in terminal global variables, which are flushed to disk but are not transactional across a terminal crash. The bridge's PostgreSQL `client_order_id`/`command_id` record and post-trade position reconciliation remain authoritative; an uncertain or timed-out send is never retried blindly.

Trust model for the local listener: the EA authenticates the bridge with `BridgeToken` over plain HTTP to a loopback address, so any other local process that can bind or intercept `127.0.0.1`/`localhost` (or a misconfigured proxy) could answer in its place. Keep the bridge on the same host as the terminal, bind it only to loopback, disable system/terminal HTTP proxies, and treat every local login as fully trusted. This is not a network or mutual-TLS boundary.

`scripts/build_demo_ea.py` performs the deliberate build step instead of a hand edit: it copies `mql5/CriticalConsensusEA.mq5`, flips exactly the one line `const bool ENABLE_DEMO_EXECUTION = false;` to `true`, refuses if the reviewed source shape no longer matches, optionally compiles with the installed MetaEditor and installs the result. MetaEditor's process exit code is not reliable, so success is decided by the "Result: N errors, M warnings" line in its log. `scripts/setup_demo_terminal.py` wraps the whole host preparation: it refuses to touch anything while `terminal64.exe`/`metatester64.exe` runs (MT5 rewrites `Config/common.ini` on exit, so a patch applied while it runs is silently lost), requires exactly one `[Experts]` section with exactly one each of `Enabled`, `WebRequest` and `WebRequestUrl`, keeps the file's UTF-16 LE BOM and CRLF endings, extends an existing URL allowlist rather than replacing it unless `--force-url`, writes through a temporary file plus `os.replace`, keeps one timestamped backup that it never overwrites, and re-reads the bytes to verify. It is a dry run unless `--apply` is passed, and it never decides anything about an order.

Host evidence recorded on 2026-10-31 (MetaQuotes-Demo, terminal build 6296, MetaTrader5 5.0.6231): `python scripts/setup_demo_terminal.py --apply` compiled the demo EA with **0 errors and 0 warnings**, installed `MQL5/Experts/CriticalConsensusEA_Demo.ex5` (37780 bytes, its generated source carrying `ENABLE_DEMO_EXECUTION = true` and `AllowLiveExecution = false`), and left `Config/common.ini` with `Enabled=1`, `WebRequest=1`, `WebRequestUrl=http://127.0.0.1:8000` next to a byte-identical backup. The repository source itself still carries `false`, and `scripts/check_mql5_safety.py` keeps it that way.

What that did not prove — and what took until 2026-11-01 to prove — is the advisor actually polling the bridge. Attaching an expert advisor to a chart is a manual step in the terminal GUI (Navigator → `CriticalConsensusEA_Demo` → drag onto a chart → set `BridgeUrl`, `KillSwitchUrl`, paste `BridgeToken` → keep the AutoTrading button on), and `WebRequest()` cannot execute in the Strategy Tester at all (documented MetaQuotes limitation), so no tester run can ever substitute for that attach. A GUI toggle of AutoTrading also rewrites `[Experts] Enabled=0` and can clear `WebRequestUrl`, so re-run `setup_demo_terminal.py --apply --skip-build` after any manual switch — and verify the file again afterwards: at 00:21 on 2026-11-01 a running terminal rewrote `Config/common.ini` and dropped the keys that had been applied at 00:11.

Transport evidence, recorded 2026-11-01 13:52–13:55 UTC on demo account 114929502 at `MetaQuotes-Demo`, `CriticalConsensusEA_Demo` attached to XAUUSD,H1, read out of `MQL5/Logs/20261101.log` (the file is UTF-16: `iconv -f UTF-16LE -t UTF-8`):

- before the token was pasted into the chart inputs, every poll logged `BLOCKED: kill-switch state unavailable; HTTP 401, error=0` — the advisor was reaching the bridge and the bridge was refusing it;
- after the paste, arming the durable stop with an authenticated `POST /v1/risk/kill-switch` (HTTP 200, `status: "PERSISTED"`) produced `BLOCKED: kill switch is active or state is invalid.` on a steady 3-second cadence, which is the advisor's own `PollSeconds`;
- releasing it with `--release-kill-switch` stopped those lines, and the log contains no `Bridge returned HTTP` or `fail closed` line at all, so `GET /v1/risk/kill-switch` answered 200 `false` and `GET /v1/execution/next` answered 204 with the same `X-Bridge-Key`.

Run that probe instead of waiting for an open market whenever the question is "is the transport up". Three readings of it are wrong and each has already cost time here: an empty log does not mean the advisor stopped (a healthy advisor with an inactive switch and an empty queue prints nothing), the terminal flushes its log with a delay so "no new lines" must be re-read after half a minute rather than concluded at once, and `HTTP 401` is not a market condition — it means exactly one thing, that the key in the chart inputs differs from the bridge's.

Reading that log by hand is what let a dead transport look alive for hours, so it is now a command: `python scripts/check_ea_transport.py` takes the newest `MQL5/logs/*.log` by modification time (never a date typed into a script — `system_status.ps1` did that and printed yesterday's clean tail on 2026-11-02), decodes the UTF-16, and names the state: `request_not_allowed`, `token_rejected`, `token_missing`, `bridge_response_invalid`, `bridge_unreachable`, `kill_switch_armed`, `chart_input_invalid`, `not_demo`, `command_expired`, `broker_gate_refused`, `malformed_command`, `tombstone_unavailable`, `duplicate_refused`, `order_accepted`, `ea_silent`, `never_polled`, `no_log`. Healthy means only "a queued command could reach the broker", which is why an armed switch and a duplicate refusal count as healthy and a silent advisor counts as the worst state — it keeps `stale_from` so the last real reason survives the silence. Exit 0 healthy, 1 broken, 2 no log; `scripts/system_status.ps1` calls it, and `scripts/system_start.ps1` ends with the status, so a start that lost the allow list now says so on the spot. `tests/test_check_ea_transport.py` pins that all 43 refusal strings the advisor's source can print map to a named state, so a new guard cannot be mistaken for a healthy poll.

Transport evidence, recorded 2026-11-02 on the same account and chart, read with that checker: the terminal was restarted with `/config` at 03:51:44 UTC and `CriticalConsensusEA_Demo (XAUUSD,H1)` initialized 03:51:45 with a valid token, then logged `BLOCKED: kill-switch state unavailable; HTTP -1, error=4014` on **356 consecutive polls** until 04:11:03 — while the bridge was listening on `127.0.0.1:8000` on the runner's own process. `error=4014` is the terminal refusing the advisor's `WebRequest`, not a socket problem and not a token problem, and it is the exact failure the `--apply`-with-a-closed-terminal step exists to prevent; the runner kept queueing against it and every one of those commands expired unserved. After 04:11:03 the advisor wrote nothing at all, which the checker reports as `ea_silent` (`stale_from: request_not_allowed`): the AutoTrading switch was off or the EA detached. A queue with a TTL of 60 s and no polling advisor produces `COMMAND_EXPIRED` rows, not orders — check the transport before concluding anything about the strategy.

Still NOT PROVEN as of the 2026-11-01 readings above — and proven later the same week: a demo order reaching the broker. Two fills did happen on 2026-11-02 in the first seven minutes of the window (`agent_data/demo_bridge/served.jsonl` exists, two lines, 00:00 and 00:07 UTC; broker deals with magic `8172026` and `CC:` comments: EURUSD 0.06 @ 1.11563 and XAUUSD 0.04 @ 3902.27), so the path from a signal to a broker position is demonstrated. What remains unproven is a *sustained* run: no instrument has been served since 00:07:36 UTC, and both later attempts to trade died on transport permission and on a silent advisor rather than on a market decision. Do not describe a demo trade as executed until a broker deal with magic `8172026` and a `CC:` comment is visible in the terminal's own history.

### 6.1 Demo execution end to end

Order-free decisioning first, then the only component that may create an order:

```powershell
python scripts/run_mt5_agent.py --db .\agent_data\mt5_agent.db run --policy .\config\mt5_agent.yaml --source live --language ru
$env:MT5_BRIDGE_TOKEN = "<24-256 characters from [A-Za-z0-9._~-]>"
python scripts/run_mt5_demo_trading.py --policy .\agent_data\live_demo_policy.yaml --db .\agent_data\mt5_demo.db --symbols all --cycles 0 --execution confirm --verbose
```

The runner starts the loopback bridge itself (it binds `127.0.0.1`, serves the two GETs the advisor polls plus the operator's authenticated `GET /v1/health` read — which is on no path the advisor or the runner orders through — plus exactly one write, the authenticated `POST /v1/risk/kill-switch`; it holds one pending command, applies a 5–600 s TTL and fsyncs a tombstone before it serves a command), refuses a policy that does not require a demo account, refuses any account the terminal reports as non-demo, and reconciles filled orders against the broker's own deal history by magic number `8172026` and the `CC:` comment prefix. A command that was served without an observed fill ends the run and needs `--acknowledge-uncertain` before that order id is touched again; there is no retry path. `--symbols all` walks a curated universe and asks the live terminal for each specification, so gold and indices are sized from broker contract data and never from a hardcoded guess; a symbol the broker cannot serve is named in the journal with the terminal's own reason (`MT5 could not select symbol …`, or `pays profit in JPY, not in the account currency USD`). Instrument *spelling* is the broker's too: `resolve_broker_symbol` (in `src/critical_consensus/mt5_agent/backend.py`, tested by `tests/test_broker_symbol_alias.py`) takes an exact match when the server lists one, otherwise aliases a six-character base such as `XAUUSD` onto a one-to-three-character suffix, refuses with `MT5 offers several variants of …; refusing to guess which one this account trades` when several fit, and then uses the server's own spelling for the whole run — journaled as a `demo_symbol_alias` event, with the internal key normalised so two spellings cannot collide. That is what makes `EURUSDm` (a market group), `XAUUSDc` (a cent account) and lowercase `xauusd` resolvable instead of "unavailable", and it is why `BTCUSD` on `MetaQuotes-Demo` is reported as absent rather than guessed at.
Reconciling a fill is a history read, and the read is the part that was wrong. `entry_deals()` in
`scripts/run_mt5_demo_trading.py:557-570` asks `history_deals_get()` for a naive window because the
terminal interprets those bounds on the **server** clock, which on MetaQuotes-Demo runs 7200 s ahead of
UTC. Measured live at 04:58:51 UTC with the left edge fixed at minus seven days: a right edge of `now+4h`
returned 1 row and neither of the session's two magic-`8172026` deals, while `now+8h` returned 5 rows and
both fills. A clipped range reads as an empty book, so the command queued at 00:00:07 on 2026-11-02 was
journalled `NO_FILL_OBSERVED` for an order the broker had actually filled, the run armed its own
`EURUSD:UNCONFIRMED` stop, and six restarts (rc=3) were refused on that unresolved outcome until the
operator released the switch (`--release-kill-switch`, 00:07:05) and acknowledged the remainder
(`--acknowledge-uncertain`). The window is therefore oversized on both sides (−7 days / +3 days) and
`test_entry_deals_asks_for_a_window_wide_enough_to_see_a_server_clock_fill` refuses any narrowing below
the measured-safe margin; every candidate is still re-filtered by magic, comment and `not_before`, so a
wide read cannot invent a fill. If a run reports `NO_FILL_OBSERVED` for a command you can see in the
terminal's own history, check the window before checking the broker.

A runner log full of tracebacks does not mean the bridge is falling over. On 2026-11-02 the
all-universe log carried six blocks reading `Exception occurred during processing of request from
('127.0.0.1', 5075x)` with `ConnectionAbortedError: [WinError 10053]`, and the lines immediately before
and after each one are ordinary command lifecycles — the queue was serving fine. The terminal aborts its
own `WebRequest` as soon as it refuses the poll, so the loopback socket dies mid-request, and stdlib
`socketserver.handle_error` answered every such disconnect with a full traceback. The bridge now
subclasses its server: `ConnectionAbortedError`, `ConnectionResetError` and `BrokenPipeError` print one
line (`bridge: client 127.0.0.1:58613 closed the socket mid-request (ConnectionResetError)`) and anything
else still gets the standard report, so a genuine handler bug cannot hide in the noise. Measured on a
throwaway bridge bound to an ephemeral port — never `:8000`, which belongs to another process — three
forced resets produced **57 stderr lines with tracebacks** before the change and **3 lines with none**
after, and the same bridge answered a later `GET /v1/risk/kill-switch` with 200 in both cases;
`tests/test_mt5_demo_bridge.py` pins all three disconnect types and the non-disconnect case. Read the
exit codes next to them, not instead of them: six `rc=3` sixty seconds apart is the honest-start guard
refusing to restart over an unresolved served command, `rc=4294967295` (`-1`) and `rc=3221225786`
(`0xC000013A`, a console interrupt) are external terminations that leave **no Python traceback at all**,
and `port 8000 held by foreign pid=…; standing down, not killing` is the supervisor obeying a constraint
rather than a failure. If the runner log contains no traceback naming `run_mt5_demo_trading.py`, nothing
in the runner crashed — look for who removed the process.

A quiet advisor log does **not** mean a dead transport, and reading it as one is how a chain that had
filled a broker order two seconds earlier got reported `ea_silent (BROKEN)`. The advisor prints a line
only when it is refused or when something happens: measured across all three `MQL5\logs\*.log` files the
whole corpus is **4904 lines — 4875 `BLOCKED`, 10 initialization lines, 8 `automated trading is disabled`,
6 enabled, 3 `Order accepted`, 2 `PROBE`** — there is no healthy-poll string in the source, so silence
cannot separate a live poll loop from an EA detached from the chart. The witness is the bridge: it records
the count and timestamp of every **authenticated** GET on the two endpoints only the advisor polls
(`/v1/risk/kill-switch`, `/v1/execution/next`; refused requests, POSTs, an `Origin` header and the health
read itself are not counted) and answers `GET /v1/health` with those counters and the poll age on its own
clock — token required, and it is not a second execution path. `check_ea_transport.py` consults it **only**
when its verdict rests on silence (`ea_silent`, `never_polled`), so a named refusal in the log is never
overwritten by a counter from elsewhere. Measured against the real log directory with a current bridge on
an ephemeral port (never `:8000`): `--no-bridge` → `ea_silent (BROKEN)`, exit **1**; the same read with the
bridge → `polling (healthy)`, exit **0** with `bridge: advisor polled 0.1 s ago, 2 polls total`; an
unauthenticated `/v1/health` → **401**; a 900-second-old poll keeps `ea_silent` and says the silence is
real; `unreachable`, `http_401`, `http_404`, `not_json` and a malformed URL each return a reason instead of
raising. A runner older than this change has no `/v1/health`, and the checker then prints `the bridge has
no /v1/health: it is running an older build than this checker` and leaves the log verdict alone — restart
the runner to make the live verdict correct. Where the bridge itself cannot be asked, the loopback socket
table is a cross-check: `Get-NetTCPConnection -LocalPort 8000` in TIME_WAIT holds one entry per recently
closed poll, and 06:05:06–06:06:36 UTC it kept **80 entries with 80 distinct peer ports sliding from
62520–62673 to 62631–62789**, about 57 freshly closed connections per 45 s.


Secrets come from the environment only: there is no `--bridge-token` argument, and the admin token is never an argument either, because a Windows command line is visible to other users of the host and in a `Win32_Process` snapshot while neither the bridge state file nor the journal writes a key or a response body. `--tombstones` resolves against the repository root rather than the current directory, so no `cd` can move the durable stop or the served-id history, and the default run id derives from that state path plus the policy file — it matters because `client_order_id` is built from it, so a random id per process would make every restart look like a first-ever order. Exit codes: `0` finished cleanly with nothing unconfirmed, `2` configuration refused before any market read (including `--release-kill-switch` without a valid admin token), `3` the run cannot honestly start (no instrument the broker serves, a bridge socket that cannot bind `127.0.0.1`, an unreadable deal history while commands were served, or served tombstones with no observed fill and no `--acknowledge-uncertain`), `4` interrupted after a command was served (`interrupted_with_send_in_flight`), which also arms the durable stop. An armed switch is neither an exit code nor a pause: the runner prints the reason once at startup and keeps cycling, because the durable stop is enforced in `DemoExecutor.offer()` — reached only when a signal already exists.

Observed live run, 2026-11-01, demo account 114929502 at `MetaQuotes-Demo` (100 000 USD, leverage 100, trade allowed, terminal build 6296), four requested instruments XAUUSD, XAGUSD, EURUSD, GBPUSD with BTCUSD reported unavailable at that broker, one cycle per symbol every 20 s: 1386 `agent_cycle` records in that journal at 14:27:34 UTC, every one blocked `OUTSIDE_TRADING_WINDOW` (weekend-frozen feed plus `blocked_weekdays_utc: [6]`), `broker_order_sent: false` throughout, equity and balance flat at 100 000, `audit_chain_valid: true`, and no `agent_data/demo_bridge/served.jsonl` because no command was ever queued. A cycle count is a snapshot, never an invariant: a live run writes 12 `agent_cycle` rows a minute, and by the time of writing the bridge had been taken over by a second runner on the same policy and the same tombstone path — therefore the same `run_id` — journalling into `agent_data/demo_live_mine.db`. The same run is where the transport probe above was performed, and it also shows what that probe does **not** leave in the journal: the stop was armed for 35 minutes of that window and the journal holds no `KILL_SWITCH_ACTIVE` blocker at all, because gate 2 of the cycle reads the `kill_switch` section of the *policy* while the durable `kill_switch.json` is checked in `DemoExecutor.offer()`, which a market-closed cycle never reaches. Verify transport from the advisor's log, not from a blocker counter. On this account the sizing gate is no longer the obstacle it was on 2026-10-31: at 0.5% of 100 000 USD the risk budget is 500 USD against minimum-lot risks of 1.93–4.24 USD.

Observed live run, 2026-10-31, thirteen requested instruments: seven available (EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD, XAGUSD, US500), six refused before the cycle for the reasons above; all seven blocked inside the cycle (`QUOTE_STALE` on the six with weekend-stale ticks — the last tick was 2026-10-30T21:00:00Z — and `MARKET_DATA_READ_FAILED` for `MT5 returned no tick for US500`). Zero signals, zero commands queued, zero filled, `broker_order_sent: false` in every record, `audit_chain_valid: true`. Quote age cannot be relaxed past 300 seconds by policy, so a weekend can never produce a command. On that account (balance 10.00 USD) the sizing arithmetic also shows the demo order could not have been funded at the policy's 0.5% risk: the risk budget is 0.05 USD while the broker's minimum lot risks 1.93–4.24 USD on those symbols, i.e. `MINIMUM_LOT_EXCEEDS_RISK_BUDGET` — a larger demo balance is a precondition for a real execution test, not a config to argue with.

Do not describe the EA as compiled until the MetaEditor log line and the installed `.ex5` exist, and do not describe a demo trade as executed until a broker deal with that magic number and comment is visible in the terminal's own history. Avoid placing credentials, bridge tokens or screenshots of the account panel in logs or artifacts.

## 7. Risk and kill switch

Capture an order-free terminal risk snapshot on the Windows MT5 host and persist it (it may add the symbol to Market Watch; it sends no order). The account-day baseline and equity high-water mark must be verified by the operator; if those are unavailable, the collector returns DENY instead of inventing drawdown. Supply a symbol-to-exposure-group JSON file covering every tradable symbol and its existing positions; `config/symbol-groups.example.json` is a starting point to copy and extend. MT5 reports tick and deal timestamps in broker server wall-clock time, not UTC, so `--server-utc-offset-seconds` is mandatory and expressed in seconds, must be a whole number of minutes (multiple of 60) within ±14 hours, e.g. `10800` for UTC+3. The collector subtracts that offset before comparing the quote against UTC and uses it for the broker's midnight day boundary; a missing or malformed offset fails closed with `MT5_SERVER_UTC_OFFSET_MISSING_OR_INVALID` before MT5 is initialized. The snapshot reads account equity/balance, account login/server, open positions and current-day deal P/L; it does not send orders. The account login and server are taken from `account_info()` and are mandatory (a missing login or blank server fails closed). Risk is sized at the worse of the reviewed and current reference price over `min(equity, balance)`, a fill drifting more than 10% of the stop distance is rejected, and starting equity + realised P/L must reconstruct the current balance (1% tolerance) before the snapshot passes. Each open position requires a fresh tick for its own symbol; a stale or missing per-symbol quote yields `OPEN_POSITION_STOP_RISK_UNKNOWN` rather than under-reporting stop risk as zero. The news-window field stays unknown because a news calendar is not integrated, so the risk-controlled permission check remains DENY until a verified news adapter is provided.

```powershell
$env:DATABASE_URL = "postgresql://USER:PASSWORD@DB_HOST:5432/critical_consensus"
python scripts/capture_mt5_risk.py --symbol EURUSD --direction BUY --size 0.10 --expected-price 1.10010 --stop-loss 1.09910 --take-profit 1.10200 --server-utc-offset-seconds 10800 --day-start-equity 10000 --equity-high-watermark 10200 --symbol-groups .\\symbol-groups.json
```

The global emergency stop is persisted and active by default. Inspect it with authenticated `GET /v1/risk/kill-switch`; activate it with authenticated `POST /v1/risk/kill-switch`. Activating only requires the bridge key because it moves the system toward the fail-closed state; disabling additionally requires a separately configured `MT5_KILL_SWITCH_ADMIN_TOKEN` of at least 32 characters, supplied in `X-Kill-Switch-Admin`. On the API path, every switch change is added to an append-only hash-linked event log in the database; the demo path has no database, so its durability is the single JSON record, whose `event_hash` covers exactly `kill_switch_active|kill_switch_reason|actor_id|updated_at`. The separate admin token is an operational guard, not a substitute for a proper TOTP/signature flow.

The demo-only bridge answers the same two requests, so the same stop reaches the demo path. Its state is one durable file, `kill_switch.json`, written next to the tombstones: `scripts/activate_kill_switch.ps1` arms it against `http://127.0.0.1:8000` and checks the `status: "PERSISTED"` answer, the advisor reads it on every poll, and the runner reads it before any market data and again immediately before it builds a command. Both sides re-read the file on every check, so a change made in one process is obeyed by the other without a restart. A record that cannot be read counts as armed on both sides: no record at all is served as armed (`NOT_SEEDED`), and a file that existed and was deleted is the same refusal as a corrupt one — removing the state file cannot disarm anything. An arm that cannot be written to disk stays armed in memory (`MEMORY_ONLY`, HTTP 503). The first runner that finds no record seeds one as **armed**, with `kill_switch_reason: "SEEDED_ARMED"` — it does not read `kill_switch` from the policy, because a policy describes how to trade and is not permission to trade — and it never overwrites a record an operator or a halted run left behind. Lifting a stop while no runner is alive is `python scripts/run_mt5_demo_trading.py --policy … --db … --tombstones … --release-kill-switch`, which takes the same `MT5_KILL_SWITCH_ADMIN_TOKEN` from the environment and refuses without it; `--policy` and `--db` are required for every invocation, including this one.

```powershell
$env:MT5_BRIDGE_TOKEN = "<the running bridge's key>"
python scripts/run_mt5_demo_trading.py --policy .\agent_data\live_demo_policy.yaml --db .\agent_data\mt5_demo.db --tombstones .\agent_data\mt5_served.jsonl --cycles 0
.\scripts\activate_kill_switch.ps1 -Reason "Stop new MT5 demo commands for investigation"   # blocks the EA and the runner
# to lift it: the same --policy/--db/--tombstones, plus the second secret
$env:MT5_KILL_SWITCH_ADMIN_TOKEN = "<32+ characters, different from MT5_BRIDGE_TOKEN>"
python scripts/run_mt5_demo_trading.py --policy .\agent_data\live_demo_policy.yaml --db .\agent_data\mt5_demo.db --tombstones .\agent_data\mt5_served.jsonl --release-kill-switch
```

`--tombstones` decides where the record lives (`kill_switch.json` beside it, by default under `agent_data/demo_bridge/`), so the release must name the same path the run used. The bridge token is still required for the release command because the admin secret must differ from it.

A release while a runner is alive needs no CLI at all, because the bridge answers the write itself; both secrets go in headers, the bridge key as the authenticator of the request and the admin key as the proof that the direction is a release:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/risk/kill-switch `
  -Headers @{ "X-Bridge-Key" = $env:MT5_BRIDGE_TOKEN; "X-Kill-Switch-Admin" = $env:MT5_KILL_SWITCH_ADMIN_TOKEN } `
  -ContentType "application/json" -Body '{"active": false, "reason": "investigated, releasing"}'
```

The stop blocks new commands only: it does not close an open position or cancel an order already accepted by the broker, and a release made in one process is read by the running bridge and the running runner on their next check, so neither has to be restarted for either direction.

The risk-controlled permission endpoint requires an inactive persisted kill switch, a passing snapshot no older than 15 seconds, demo-account mode, operator-provided baselines, and a request hash matching the exact instrument/direction/size/expected price/SL/TP. It then recomputes `size × |worst_entry − SL| / tick_size × tick_value_loss` at the worse of the reviewed and reference price and the resulting fraction over `min(account_equity, account_balance)`, recomputes daily loss and drawdown from the captured equity baselines, and denies with `RISK_*_DOES_NOT_MATCH_*`/`RISK_SNAPSHOT_SYMBOL_MISMATCH` if the persisted aggregates disagree beyond 1%, the tick specification is missing, the account identity is missing or mismatches a policy pin, the position aggregate is inconsistent (`position_symbols` length ≠ `open_positions`, `correlated_open_positions > open_positions`, or a positive open-stop risk with zero positions), the equity high-water mark is below current equity or the day-start equity, the day-start equity cannot reconstruct `balance − daily_pnl` beyond 1%, a fraction is non-finite, the reference price is missing, slippage exceeds 10% of the stop distance, the reference price crosses SL/TP, or the symbol group is empty. Daily-loss and drawdown caps are prospective: the new trade's risk fraction is added to the current loss/drawdown before comparing with the limit. Reward/risk is applied unconditionally (including flat snapshots): it must clear `min_expected_rr` at the conservative entry, after the value is recomputed from SL/TP (1% tolerance). Risk per trade excludes future fees, spread changes and gap risk. Missing database, missing row, stale snapshot or unknown field is a deny. Before each demo order, the external execution service must also check broker volume/stop constraints and position reconciliation. The news-window field remains fail-closed while the calendar integration is absent.

Use a demo account only. Test at least: missing SL, missing TP, stale command, duplicate client order ID, adverse slippage, volume-step mismatch, closed market, rejected OrderCheck, network timeout after OrderSend and kill-switch activation. An ambiguous broker response requires reconciliation; never blindly retry.

## 8. End-to-end evidence / definitions

The added CI checks cover unit-level import validation, multi-agent timeout/identity/TTL handling, walk-forward math, PostgreSQL immutability/idempotency and Redis-to-PostgreSQL commit-before-ACK flow (valid events ACK after commit; poison events are DLQ'd then ACKed; handler failures stay pending). These integration checks run only when `TEST_DATABASE_URL`/`TEST_REDIS_URL` are configured and are skipped otherwise. The MQL5 source guard remains static-only.

Evidence labels:
- **VERIFIED:** reproducible test/log/artifact exists for that exact layer.
- **NOT PROVEN:** there is only source code, a configuration or a passing synthetic test without the real service/device/broker evidence.
- **PENDING EXTERNAL:** requires the Windows MetaTrader/MetaEditor and broker demo terminal. On the 2026-10-31 host this label is only partly satisfied: the terminal, MetaEditor, the `MetaTrader5` package and a logged-in MetaQuotes-Demo account are present and were read (see section 6.1), while PostgreSQL/Redis were not exercised there, so persistence claims on that host remain PENDING EXTERNAL.

The chain must be demonstrated in order: captured market data → durable prediction before outcome → independent-review evidence → chronological calibration report → risk-gate trace → human demo approval → MT5 demo command → broker result/position reconciliation. Passing API tests alone is not proof of a working MT5 trade or profitability. On the 2026-10-31 host the chain stops between "MT5 demo command" and "broker result": the command layer is built, compiled and installed, but no command was ever queued (market closed, stale quotes) and no broker deal exists.
