# MT5 Critical Consensus: full validation runbook

Status as of this change: infrastructure and tests are being added; production readiness is not implied. Do not connect a live account. The intended order is data → persistence → agent evidence → calibration → EA demo → end-to-end verification.

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

Do not assume the broker's export timezone; verify it in MT5 first. Naive timestamps without an explicit timezone are rejected. The source CSV is read-only. OHLC rows are validated for finite prices, valid ranges, ordered unique UTC timestamps, symbol and timeframe consistency.

## 2. PostgreSQL and Redis

Install the optional `data` extra, set `DATABASE_URL` and `REDIS_URL`, then run:

```bash
python -m critical_consensus.persistence --migrate
python scripts/run_worker.py
```

The worker is a durable event writer for `cc:signals:v1`, not a trade executor. It acknowledges Redis entries only after the PostgreSQL transaction succeeds. The `processed_events` inbox protects replays; the same event ID with a different payload is rejected. Market bars and emitted forecast probabilities cannot be silently edited. Apply migrations with a dedicated database role and restrict production DB privileges; the sample integration role used by CI is intentionally more permissive than production should be.

## 3. Agent consensus

For a controlled provider test, define provider metadata with environment variable names only; API secrets themselves belong in the environment/secret manager. For example:

```powershell
$env:OPENAI_API_KEY = "set-in-your-secret-manager"
$env:MT5_AGENT_PROVIDERS = '[{"agent_id":"technical-a","independence_group":"vendor-a","model":"YOUR_MODEL","api_key_env":"OPENAI_API_KEY","base_url":"https://api.openai.com/v1"},{"agent_id":"technical-b","independence_group":"vendor-b","model":"YOUR_OTHER_MODEL","api_key_env":"OTHER_PROVIDER_KEY","base_url":"https://api.example.com/v1"},{"agent_id":"risk-review","independence_group":"vendor-c","model":"YOUR_RISK_MODEL","api_key_env":"RISK_PROVIDER_KEY","base_url":"https://api.example.net/v1"}]'
python scripts/run_agent_analysis.py --symbol EURUSD --timeframe H1 --bars 200
```

The example is a template, not a valid provider configuration until model names and keys exist. The runner reads recent bars from PostgreSQL, builds a deterministic read-only snapshot, requests JSON from the configured OpenAI-compatible endpoints, constrains cited evidence to the supplied snapshot reference, and writes an audit event. It returns `GATHER_MORE` if provider settings, evidence, freshness or the required number of agents/groups are not satisfied. The same vendor, training corpus, model family or source data across providers can still create correlated errors; group labels do not prove independence.

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

## 5. MQL5 EA and demo forward

Run `python scripts/check_mql5_safety.py` for static source guard checks. They verify fail-closed defaults, demo-account validation, expiry, SL/TP, slippage and preflight checks. They do **not** compile the EA.

A human with the intended MetaTrader 5 terminal must compile `mql5/CriticalConsensusEA.mq5` in MetaEditor and attach it only to a demo account with the default `EnableDemoExecution=false`. Preserve that disabled default while compiling/smoke-testing. Record the MetaEditor build/version, compiler output, terminal build, broker, account mode, symbol specification and Journal/Experts logs. If and only if the EA has been reviewed and the bridge returns a schema-validated demo command, enable demo execution for the controlled test. `AllowLiveExecution=true` intentionally blocks the EA; there is no live-trading approval path in this scaffold.

Do not describe the EA as compiled or forward-tested until those host-side artifacts exist. Avoid placing credentials in logs, GitHub artifacts or screenshots.

## 6. Risk and kill switch

Capture a read-only terminal risk snapshot on the Windows MT5 host and persist it. The account-day baseline and equity high-water mark must be verified by the operator; if those are unavailable, the collector returns DENY instead of inventing drawdown. Supply a symbol-to-exposure-group JSON file covering every tradable symbol and its existing positions. The snapshot reads account equity/balance, open positions and current-day deal P/L; it does not send orders. The news-window field stays unknown because a news calendar is not integrated, so the risk-controlled permission check remains DENY until a verified news adapter is provided.

```powershell
$env:DATABASE_URL = "postgresql://USER:PASSWORD@DB_HOST:5432/mt5cc"
python scripts/capture_mt5_risk.py --symbol EURUSD --day-start-equity 10000 --equity-high-watermark 10200 --risk-fraction 0.002 --symbol-groups .\\symbol-groups.json
```

The global emergency stop is persisted and active by default. Inspect it with authenticated `GET /v1/risk/kill-switch`; activate it with authenticated `POST /v1/risk/kill-switch`. Disabling requires both the bridge key and a separately configured `MT5_KILL_SWITCH_ADMIN_TOKEN` of at least 32 characters, supplied in `X-Kill-Switch-Admin`. Every switch change is added to an append-only hash-linked event log. The separate admin token is an operational guard, not a substitute for a proper TOTP/signature flow.

The risk-controlled permission endpoint requires an inactive persisted kill switch, a passing snapshot no older than 15 seconds, demo-account mode, operator-provided baselines and policy risk fields. Missing database, missing row, stale snapshot or unknown field is a deny. Before each demo order, the external execution service must also check broker volume/stop constraints and position reconciliation. The news-window field remains fail-closed while the calendar integration is absent.

Use a demo account only. Test at least: missing SL, missing TP, stale command, duplicate client order ID, adverse slippage, volume-step mismatch, closed market, rejected OrderCheck, network timeout after OrderSend and kill-switch activation. An ambiguous broker response requires reconciliation; never blindly retry.

## 7. End-to-end evidence / definitions

The added CI checks cover unit-level import validation, multi-agent timeout/identity/TTL handling, walk-forward math, PostgreSQL immutability/idempotency and Redis-to-PostgreSQL commit-before-ACK flow. The MQL5 source guard remains static-only.

Evidence labels:
- **VERIFIED:** reproducible test/log/artifact exists for that exact layer.
- **NOT PROVEN:** there is only source code, a configuration or a passing synthetic test without the real service/device/broker evidence.
- **PENDING EXTERNAL:** requires the Windows MetaTrader/MetaEditor and broker demo terminal.

The chain must be demonstrated in order: captured market data → durable prediction before outcome → independent-review evidence → chronological calibration report → risk-gate trace → human demo approval → MT5 demo command → broker result/position reconciliation. Passing API tests alone is not proof of a working MT5 trade or profitability.
