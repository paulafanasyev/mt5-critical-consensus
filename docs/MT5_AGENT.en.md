# MT5 agent: an observe/paper cycle on top of the terminal

This document describes the **MetaTrader 5 agent** layer — a neighbour of the
"reconnaissance → critical consensus → authorization" pipeline ([architecture.md](architecture.md)).
The agent reads terminal state, applies fail-closed risk gates and writes every decision to a
journal. It **never sends an order**: no module in `critical_consensus.mt5_agent` calls a trading
function, and a test enforces that rather than a promise.

Order sending exists only outside the package. The demo path
(`scripts/mt5_demo_bridge.py`, `scripts/run_mt5_demo_trading.py`, `scripts/build_demo_ea.py`,
`scripts/setup_demo_terminal.py` plus a compiled expert advisor) is described in "Demo execution
over the loopback bridge". It works **only against a demo account** and still does not move the
system any closer to real money.

Other languages: [Русский](MT5_AGENT.md) · [Tiếng Việt](MT5_AGENT.vi.md).

## Evidence status (read before use)

| Claim | Status | Evidence |
|---|---|---|
| A policy loads only with known keys; a live mode is rejected | **PROVEN** | `test_live_mode_and_unknown_keys_are_rejected` |
| One agent cycle applies its gates in a fixed order and blocks on any failure | **PROVEN** | 62 tests in `tests/test_mt5_agent.py`, 59 in `tests/test_mt5_agent_invariants.py`, 37 in `tests/test_mt5_agent_i18n.py` |
| Every blocker code the agent emits is declared, and has a label in all three languages | **PROVEN** | `test_every_emitted_blocker_is_a_declared_code`, `test_every_journaled_code_has_a_non_empty_label_in_all_three_languages` |
| The paper broker cannot see future bars and fills the stop before the target inside one bar | **PROVEN** | `test_paper_broker_hides_future_bars`, `test_paper_broker_fills_stop_before_target_in_the_same_bar` |
| Volume comes from the stop distance and the symbol specification, capped by risk *and* margin | **PROVEN** | `test_sizing_never_exceeds_the_risk_budget`, `test_sizing_caps_volume_by_margin_and_not_only_by_risk` |
| A journal edit is detected; a deleted prefix only against an anchor kept outside the file | **PROVEN** | `test_journal_detects_tampering`, `test_journal_anchor_detects_a_deleted_run_prefix`, `test_the_run_id_is_part_of_the_signed_body` |
| No journal record claims a sent broker order | **PROVEN** | `test_agent_never_reports_a_broker_order`, `test_package_source_contains_no_order_sending_surface` |
| `confirm` mode opens a position only on an explicit operator yes, and `auto` refuses a non-demo policy | **PROVEN** | `test_confirm_mode_without_an_approver_never_opens_a_position`, `test_automatic_execution_requires_a_demo_account_even_in_the_library` |
| The `LiveMT5Backend` adapter maps terminal replies and subtracts the server offset | **PROVEN against a stubbed package** | `test_live_backend_*`, `test_cli_live_source_subtracts_the_server_offset` |
| Reading a real MT5 terminal on this host | **PROVEN** | 2026-10-31: `MetaTrader5` 5.0.6231, `terminal64.exe` build 6296, demo account 10013404855 on `MetaQuotes-Demo` (balance 10.00 USD, leverage 100), live specifications and 120 H1 bars for EURUSD/GBPUSD/AUDUSD/NZDUSD/XAUUSD/XAGUSD; server offset 7200 s |
| The demo EA compiles and installs into the terminal | **PROVEN** | 2026-10-31: `scripts/build_demo_ea.py` flipped exactly one gate line in a generated copy, MetaEditor reported 0 errors and 0 warnings, and `MQL5/Experts/CriticalConsensusEA_Demo.ex5` (37780 bytes) was installed |
| The terminal configuration allows algo trading and loopback WebRequest | **PROVEN, but not durable** | 2026-10-31: `Config/common.ini` carries `Enabled=1`, `WebRequest=1`, `WebRequestUrl=http://127.0.0.1:8000`; the file is saved as UTF-16 LE with BOM and CRLF, byte-verified, with a backup next to it. Being overwritten by the live terminal is not a hypothesis for this file: on 2026-11-01 at 00:21 MT5 set `Enabled=0` and cleared `WebRequestUrl` after `setup_demo_terminal.py --apply` had applied the keys at 00:11. The current state was verified again on 2026-11-01: `Enabled=1`, `WebRequest=1`, `WebRequestUrl=http://127.0.0.1:8000;http://127.0.0.1;http://localhost:8000;http://localhost`. The permission has to be kept alive in a running terminal, not written into a file once |
| The EA actually polls the bridge (a real `WebRequest` from a chart) | **PROVEN** | 2026-11-01 13:52–13:55 UTC, demo account 114929502 on `MetaQuotes-Demo`, `CriticalConsensusEA_Demo` on a XAUUSD,H1 chart. The check is described in "How to check the transport when the market is closed": arming the durable stop through the bridge made `MQL5/Logs/20261101.log` (UTF-16) print `BLOCKED: kill switch is active or state is invalid.` exactly at the 3 s poll period, and releasing the stop stopped those lines — that is, the advisor got HTTP 200 on `/v1/risk/kill-switch` and 204 on `/v1/execution/next` with the same `BridgeToken` that was typed into the chart fields. Before that token was entered, the same log showed `BLOCKED: kill-switch state unavailable; HTTP 401` |
| Instrument names come from the broker, not from a guess | **PROVEN** | `resolve_broker_symbol` + `LiveMT5Backend.symbols()`/`use_broker_names` in `src/critical_consensus/mt5_agent/backend.py` and `scripts/run_mt5_demo_trading.py`; the tests in `tests/test_broker_symbol_alias.py` (11 of them). `XAUUSDm`/`XAUUSDc`/`xauusd` resolve to the server's own spelling, and several matches are a refusal, not a guess |
| The advisor's and the runner's gate arithmetic agree | **PROVEN** | `scripts/ea_logic_mirror.py` — a line-by-line Python mirror of `CriticalConsensusEA.mq5`; `tests/test_ea_logic_parity.py` — 109 comparisons (required fields, number and identifier grammar, tokens, the loopback URL, tombstones, volume, a grid of 200 prices for SL/TP against `stops_level`). This test found a real rounding divergence in `price_gates`, and it is fixed |
| A demo order executed by the broker | **NOT PROVEN** | the transport works, there is no queue: on 2026-11-01 the market has been closed since Friday 23:59 and the policy blocks Sunday, so every cycle yields `OUTSIDE_TRADING_WINDOW` and `broker_order_sent: false` in every record (1386 records as of 14:27:34 UTC, chain valid; the number is a snapshot — a live run writes 12 records a minute). The first cycle that could build a command is Monday 2026-11-02 00:00 UTC |
| Strategy profitability, and any trade-success rate | **NOT PROVEN** | backtest on a synthetic series; `confidence` is a heuristic score, not a calibrated probability. A 98% success rate is neither measured here nor achievable by this code. Measured runs on real bars are loss-making ([DEMO_RESULTS.md](DEMO_RESULTS.md)) |
| Execution on a real account | **NOT PROVEN and not implemented** | the Python package and the CLI contain no order-sending call, by design; the demo path refuses anything but a demo account (`AllowLiveExecution=false`, `require_demo`, the `ACCOUNT_TRADE_MODE` check), and a policy with `mode: live` is rejected at load |

The local preview used while writing this document replays a **synthetic** EURUSD H1 series
(a seeded random walk with trends); it is not committed as an artifact, and the tests generate the
same series with their own `make_bars()` helper. The tests exercise the journal and the gates on
that series; any numbers they print illustrate mechanics, **not** a return estimate. Nothing here
decides when real money may be used: that is a separate, explicit decision outside this code, and
the current package cannot execute it either way.

Check status as of 2026-11-01: `.venv\Scripts\python.exe -m pytest -q` — **971 passed, 10 skipped**
(the skips need `TEST_DATABASE_URL` and/or `TEST_REDIS_URL`; the `.venv` interpreter is required
rather than polite — part of the suite spawns `sys.executable` as a subprocess, so a system `python`
without the installed package reports 36 `ModuleNotFoundError` failures instead of a result);
`python scripts/check_mql5_safety.py` — **473 static
advisor invariants and 105 rejected weakening mutations**. The static check does not compile the
advisor source: MetaEditor is what confirms compilation, and only the broker confirms execution on
an account.

## Package layout

| Module | Purpose |
|---|---|
| `mt5_agent/backend.py` | terminal access without order-sending: `AccountInfo`, `SymbolSpec`, `Tick`, `PositionSnapshot`, the `MT5Backend` protocol, the `LiveMT5Backend` adapter |
| `mt5_agent/policy.py` | `AgentPolicy`/`RiskLimits`/`StrategyConfig`/`TradingWindow`, YAML loading, refusal on unknown keys, `execution_mode`, `ui_language` |
| `mt5_agent/strategy.py` | deterministic SMA/ATR + momentum signal, stop/target geometry, blockers |
| `mt5_agent/sizing.py` | volume from `cash_per_price_unit` (contract size, else the tick pair) and `volume_step` under the risk budget and free margin |
| `mt5_agent/paper.py` | `PaperBroker`: bar replay without look-ahead, bid/ask-side fills, gaps, daily equity baselines |
| `mt5_agent/journal.py` | one SQLite hash chain (`audit_events`, equity points included), `verify_chain`, `report` |
| `mt5_agent/agent.py` | `MT5Agent.run_cycle()` — the gate order, confirmation gate, decision record |
| `mt5_agent/i18n.py` | Russian/English/Vietnamese labels for every code and every operator message |
| `mt5_agent/codes.py` | the single registry of statuses and blockers |
| `scripts/run_mt5_agent.py` | CLI: `run` (csv/live, auto/confirm, ru/en/vi), `report`, `reset --confirm` |
| `config/mt5_agent.example.yaml` | commented policy example |

Everything that can put an order into the world lives apart from that package, and only for demo:

| Component | Purpose |
|---|---|
| `scripts/mt5_demo_bridge.py` | loopback-only HTTP bridge: two GETs (`/v1/risk/kill-switch`, `/v1/execution/next`), one authenticated POST on the kill-switch path and no other write, `X-Bridge-Key` check, the durable `kill_switch.json` the runner and the advisor both read, one pending command, TTL, fsynced idempotency tombstones, never logs the token or a command body |
| `scripts/run_mt5_demo_trading.py` | the only component that can build a command: takes the agent's decision, refuses while the durable stop is armed, re-checks every gate, queues it in the bridge and reconciles against the broker's own deal history; never retries an uncertain send |
| `scripts/build_demo_ea.py` | produces `CriticalConsensusEA_Demo.mq5` by flipping exactly one compile-time gate line, optionally compiles it with MetaEditor and installs the `.ex5` |
| `scripts/setup_demo_terminal.py` | dry run by default: refuses while the terminal runs, patches `[Experts]` in `Config/common.ini`, installs the built EA; decides nothing about an order |
| `scripts/operator_answers.py` | whole-line answer parsing (`да`/`vâng`/`y`) shared by the CLI and the demo runner |
| `mql5/CriticalConsensusEA.mq5` | the advisor source; in this repository the gate is always `false`, and the static checker pins that default |

## Gate order of one cycle

Every gate that finds a problem ends the cycle as `BLOCKED`; a usable market with no setup is
`NO_SIGNAL`, and observe mode is `OBSERVED`. The journal stores codes, never translated text.

1. `CLOCK_NOT_UTC_AWARE` — the agent clock must return a timezone-aware time.
2. `KILL_SWITCH_ACTIVE` — an active switch blocks before any market read. A missing
   `kill_switch` section loads as active with reason `POLICY_NOT_PROVIDED`; an active switch with
   an empty reason is a load error.
3. Account: `ACCOUNT_READ_FAILED`, then `ACCOUNT_NOT_DEMO`, `ACCOUNT_LOGIN_MISMATCH`,
   `ACCOUNT_SERVER_MISMATCH`, `ACCOUNT_EQUITY_INVALID`, `ACCOUNT_MARGIN_INVALID`,
   `ACCOUNT_LEVERAGE_INVALID`.
4. Equity baselines: `EQUITY_BASELINES_UNAVAILABLE` (missing, non-finite, a day-start or peak value
   ≤ 0, a peak below the current/day-start equity, or a baseline more than 1000× current equity),
   then `DAILY_LOSS_LIMIT_REACHED`, `REALIZED_DAILY_LOSS_LIMIT_REACHED`, `DRAWDOWN_LIMIT_REACHED`
   (all inclusive of the line). The backend must expose `realized_daily_pnl`; a missing
   attribute, a raising getter, or a value that is `None`, a string, a bool or a non-finite
   number is unusable data for the daily gate: the cycle blocks with
   `EQUITY_BASELINES_UNAVAILABLE` instead of treating an unknown day as loss-free.
5. Schedule: `OUTSIDE_TRADING_WINDOW`, `BLACKOUT_WINDOW_<n>`.
6. Positions: `POSITION_CAP_COUNT_UNKNOWN` when the terminal does not answer, `POSITION_READ_FAILED`,
   `POSITION_CAP_REACHED`/`POSITION_CAP_EXCEEDED`, `SYMBOL_ALREADY_OPEN`.
7. Market: `MARKET_DATA_READ_FAILED`, `BAR_SYMBOL_MISMATCH`, `QUOTE_FROM_THE_FUTURE`, `QUOTE_STALE`.
   The served spec, tick and newest bar must all name the policy symbol; a backend that ignores
   its symbol argument and answers for another instrument blocks with `BAR_SYMBOL_MISMATCH`
   before sizing or the paper book sees it.
8. Open book: `MARKET_DATA_READ_FAILED` (the spec of a position on another symbol cannot be read),
   `POSITION_STOP_UNSET` (an open position whose stop is not measurable),
   `LOSS_FLOOR_BREACHED` (existing stop risk plus the day's already banked loss exhausts the
   daily budget, or the losses protected by stops would meet or exceed the account equity).
9. Signal: strategy blockers (`INSUFFICIENT_BARS`, `NO_TREND_SIGNAL`, `SPREAD_ABOVE_LIMIT`,
   `PRIOR_CLOSE_NON_POSITIVE`, `ATR_NOT_MEANINGFUL`, `STOP_DISTANCE_INVALID`,
   `TAKE_PROFIT_PRICE_INVALID`, `STOP_INSIDE_BROKER_STOPS_LEVEL`,
   `TARGET_INSIDE_BROKER_STOPS_LEVEL`, `BUY_GEOMETRY_INVALID`, `SELL_GEOMETRY_INVALID`,
   `BAR_SERIES_INVALID`, `BAR_SYMBOL_MISMATCH`, `BAR_TIMEFRAME_MISMATCH`, `BAR_TIME_IN_FUTURE`,
   `BAR_TIME_TOO_OLD`) →
   `NO_SIGNAL`, or `BLOCKED` when the input itself was broken. `BAR_TIME_TOO_OLD` fires when the
   newest closed bar is more than two periods behind the quote, i.e. a whole bar is missing.
   `BAR_TIMEFRAME_MISMATCH` means the backend served a timeframe other than the policy's.
10. `RISK_REWARD_BELOW_MINIMUM`, then sizing: `MINIMUM_LOT_EXCEEDS_RISK_BUDGET` or
    `MARGIN_MINIMUM_LOT_UNFUNDED`.
11. `AGGREGATE_STOP_RISK_LIMIT_REACHED` — the day's banked loss plus open stop risk plus the new
    position's risk would exceed the daily budget.
12. Mode: `observe` → `OBSERVED`. In `paper` with `execution_mode: confirm` the operator is asked:
    `CONFIRMATION_UNAVAILABLE` (no operator wired), `OPERATOR_DECLINED`, `OPERATOR_CONFIRMATION_FAILED`.
13. Requote after approval: the quote is read again before the fill (`MARKET_DATA_READ_FAILED`,
    `BAR_SYMBOL_MISMATCH`, `QUOTE_FROM_THE_FUTURE`, `QUOTE_STALE`, `SPREAD_ABOVE_LIMIT`). The fill
    may drift at most 10% of the approved stop distance from the approved entry
    (`PRICE_MOVED_DURING_CONFIRMATION`); the reward-to-risk is recomputed at the fill
    (`RISK_REWARD_BELOW_MINIMUM`) and the volume is re-sized on the fill stop, never above the
    approved lot (`MINIMUM_LOT_EXCEEDS_RISK_BUDGET`, `MARGIN_MINIMUM_LOT_UNFUNDED`,
    `AGGREGATE_STOP_RISK_LIMIT_REACHED`). Only a fresh, bounded quote reaches `PAPER_OPENED`; a
    paper book that refuses the lot is `PAPER_OPEN_REJECTED`, and a backend with no paper book is
    `PAPER_BROKER_REQUIRED_FOR_PAPER_MODE`.

Every record the agent writes carries `"broker_order_sent": false` and `"execution_mode"`; each
cycle record also names `"execution_policy": "NO_BROKER_ORDER_API"` and whether its equity point
was written (`"equity_recorded"`, `"equity_record_error"`). The result object adds
`"cycle_recorded"`: a journal that refuses the cycle row yields a result with `false` instead of
aborting the run.

## Execution mode: automatic or ask first

`execution_mode` is the switch the operator asked for:

| Value | Behaviour | Constraint |
|---|---|---|
| `confirm` (default) | every new position is shown to the operator, and only an explicit yes opens it | needs an approver; without one the cycle is blocked, never opened |
| `auto` | the agent opens positions by itself | refused at policy load unless `account.require_demo: true`, so it can never drive a real account |

In the CLI, `confirm` prompts in the selected language and accepts `y`/`n`/`a`/`q`
(plus the full local words, e.g. `да`/`нет`/`все`/`стоп`, `vâng`/`không`/`tất cả`/`dừng`).
Whole lines only: `y` opens this one, `n` skips it, `a` stops asking for the rest of the run,
`q` ends the run with the report still written. A fragment such as Russian `в` or Vietnamese
`tất` is not an answer, so it can never be mistaken for "yes to everything". With no keyboard attached every proposal is declined as unconfirmed, and
the run continues so the audit trail still exists. `--execution auto|confirm` overrides the policy
for one run and is re-validated, so the demo-only rule holds either way. Positions are simulated
locally in both modes; there is no broker order in either. `run_mt5_agent.py` is that program; the
demo execution runner is a different one and is described next.

## Demo execution over the loopback bridge

The chain is single-path and its order is fixed: the `mt5_agent` package decides and sizes (it
cannot send), `run_mt5_demo_trading.py` re-checks that decision and queues exactly one command in
`mt5_demo_bridge.py`, the advisor polls the bridge over `WebRequest` on `127.0.0.1` and calls
`OrderSend` against a demo account, and the runner reconciles the result against the broker's own
deal history by magic number `8172026` and the `CC:<id>` comment.

Gates that must all hold before a command exists:

1. The terminal account must be a demo (`ACCOUNT_TRADE_MODE`), the policy must require one
   (`account.require_demo: true`), and `expected_login`/`expected_server` must match.
2. The policy mode must be `paper` (or `observe`), and `auto` is impossible without the demo
   requirement.
3. The quote is re-read twice. Before anyone is asked, entry, stop/target geometry, the broker
   `stops_level`, the volume cap and available margin are checked against fresh data. After the
   answer, `MT5Agent.post_approval_gates` re-reads the terminal and applies the whole set a paper
   fill gets — the kill switch, account identity, equity and daily-loss baselines, the trading
   window and the blackout, the position cap and the open book, quote age, spread, the slippage
   bound against the approved entry, risk/reward at the fill, the re-sized lot, margin and the
   stops level — and the command is built from the price and the lot those gates return, never
   from the pre-approval quote.
4. Volume must sit inside the broker's `volume_min`/`volume_step`/`volume_max`, below
   `--max-volume` (at most 0.20) and inside available margin; the advisor's own `MaxVolume` input
   is 0.20 as well.
5. The bridge holds one pending command, demands `X-Bridge-Key` (24–256 characters), applies a
   5–600 s TTL, and never serves the same `command_id` twice. `client_order_id` is deterministic,
   so restarting the runner does not create a second order.
6. In `confirm` mode a whole operator line is required (`да`/`vâng`/`y`); a fragment is not an
   answer.
7. The advisor re-checks all of the above independently, plus the kill switch through a fresh GET,
   the TTL, its compile-time `ENABLE_DEMO_EXECUTION` gate, `AllowLiveExecution`, the symbol, the
   volume and the stops, and it writes its idempotency tombstones before sending. It checks its own
   ceilings already at start-up and refuses to initialize when `PollSeconds` is outside 1–60,
   `MaxVolume` outside (0; 0.20], `MaxSlippagePoints` outside 1–20, `MaxCommandTtlSeconds` outside
   1–600, or `MagicNumber` is not positive: all of these values come from the chart fields, and an
   advisor that accepts `MaxVolume = 100` would execute a size that nobody agreed to from either
   side.
8. An uncertain result (command served, no fill observed) stops the run and needs an explicit
   `--acknowledge-uncertain`; there is no code path that re-sends.

### The advisor and the runner gate the same numbers

A gap between "what Python checks" and "what MQL5 checks" is cheaper to close by computation than
by attention. `scripts/ea_logic_mirror.py` is a line-by-line Python mirror of the advisor: the same
`JsonString`/`JsonNumber`/`JsonTrue`/`JsonFalse` on top of the same flat JSON-object parser, the
same grammar for numbers and identifiers, the same FNV-1a (it walks the UTF-16LE bytes, which is
why `"a"` → `089BE207B544F1E4`, not ASCII), the same tombstone keys, the same loopback URL parsing
and the same queue of checks in `ExecuteDemoCommand` — in the same order and with the same refusal
texts. The tests in `tests/test_ea_logic_parity.py` (109 comparisons) compare that mirror against
the live bridge (the real arm and disarm bodies are taken from the socket), against `json.loads`
with a strict `NaN`/`Infinity` refusal, against the bridge's regular expressions, against the text
of the `.mq5` itself (required fields, defaults, ceilings and the magic number are cross-checked
three ways), and against a grid of 200 prices where entry and stop are rounded in opposite
directions relative to `stops_level`.

The very first such grid found a live defect on our side. MT5 hands the broker numbers rounded to
`digits`, while `price_gates` compared unrounded ones against `stops_level`; at a price of 4000.005
with a 1.02-point stop and a stops level of 2 points, Python blocked the command while the advisor
would have accepted it. A gate that measures a number which never reaches the broker is not a gate.
Now `broker_level()` rounds entry, stop and take profit exactly the way `NormalizeDouble` does
(half away from zero), and every geometry, slippage and `stops_level` check runs on the rounded
levels; the parity test requires the two sets of refusals to match across the whole grid.

### The emergency stop on the demo path

The stop is a file, not a variable: `kill_switch.json`, written next to the tombstones. One record
holds the last switch change (`kill_switch_active`, `kill_switch_reason`, `actor_id`, `updated_at`,
`event_hash`), rewritten whole through a temporary file plus `os.replace` and fsynced. The advisor
reads that record on every poll and the runner reads it twice per offer — before any market read and
immediately before a command is built — and both re-read the file rather than a copy in memory, so an
operator arms or releases it while a run is in progress and the next read is the new answer. So the
documented `scripts/activate_kill_switch.ps1` stops this path too, and a stop pulled before a restart
is still pulled after it.

- Arming needs the bridge key only: `POST /v1/risk/kill-switch` with `{"active": true, "reason": …}`
  and `X-Bridge-Key`. The answer carries `status: "PERSISTED"`, `updated_at` and `event_hash`, which
  is what that script checks — it also requires a 64-character hash and then performs the same GET the
  advisor performs, so a stop the polled state does not agree with is reported as a failure.
  A request whose body cannot be read as a JSON object, whose
  `active` is not a real boolean or whose note fields are not strings is refused with 400 and
  changes nothing; a body larger than 4096 bytes is drained out of the socket (up to 1 MiB) and
  refused with 413, so the client always gets a verdict instead of a reset. A client that stops
  mid-send is dropped after 10 s rather than holding a handler thread.
- Releasing needs a second secret: `MT5_KILL_SWITCH_ADMIN_TOKEN`, at least 32 characters, different
  from `MT5_BRIDGE_TOKEN`, supplied in `X-Kill-Switch-Admin`. A missing or reused admin token is 503
  and a wrong one is 403; either way the stop stays armed. While no runner is alive,
  `python scripts/run_mt5_demo_trading.py --policy <policy.yaml> --db <journal.db>
  --release-kill-switch` reads the same file, takes the admin token from the environment and writes
  the disarm; `--policy` and `--db` are required by every invocation, including this one.
- Absence is not consent. A state that cannot be read is never treated as disarmed: no record at all
  is served as `{"kill_switch_active": true, "kill_switch_reason": "NOT_SEEDED"}`, a corrupt or
  unreadable `kill_switch.json` makes the runner refuse before it starts and makes the advisor's GET
  answer 503 (which the advisor treats as blocked), and a record that existed and then disappeared —
  a deleted file — is the same 503, never a fresh start. Deleting the state file therefore cannot
  disarm anything.
- The first run that finds no record creates one **armed** (`kill_switch_reason: "SEEDED_ARMED"`),
  not from the policy's `kill_switch` section. A policy describes how to trade, not permission to
  trade; silence in a settings file cannot release a stop. A record an operator left over HTTP or a
  halted run wrote is never overwritten. The one thing held in memory is an arm whose disk write
  failed: it keeps blocking (the answer says `MEMORY_ONLY`), and a disarm whose write fails leaves
  the stop armed.
- Every outcome that stops a run arms the same durable record — an operator's "stop" at the
  confirmation prompt, an unconfirmed send, a duplicate command id — so finishing the process does
  not finish the stop. A Ctrl-C that arrives after a command was already handed to the bridge is
  not counted as a clean finale either: the run marks the command unconfirmed, arms that same
  record and exits with code `4` (`interrupted_with_send_in_flight`), because only the broker's
  deals know whether the order went out.

### How to check the transport when the market is closed

The transport is verified without a single trade, and that is exactly how it should be done rather
than "wait for Monday". The advisor talks to the bridge on every timer tick, so the state of the
stop is visible in its log:

1. Start the runner and confirm that `terminal64.exe` is running, the advisor is attached to a
   chart, and the AutoTrading button is on.
2. Take a snapshot of the log: `MQL5/Logs/<YYYYMMDD>.log` is UTF-16, read it with
   `iconv -f UTF-16LE -t UTF-8`.
3. Arm the stop through the bridge: `POST /v1/risk/kill-switch` with `X-Bridge-Key` and a body of
   `{"active": true, "reason": "…"}`; the answer must be 200 with `status: "PERSISTED"`,
   `updated_at` and `event_hash`.
4. Wait at least one poll period. The sign of health is lines of
   `BLOCKED: kill switch is active or state is invalid.` at an even spacing; they mean the advisor
   received and parsed the bridge's answer, i.e. the `BridgeToken` in the chart fields matches the
   bridge key.
5. Release the stop (`--release-kill-switch`, see above) and confirm that those lines stopped and
   that there is no `Bridge returned HTTP` or `fail closed` line at all: silence after a release is
   a 200 on the stop path and a 204 on the command path, so both routes are authenticated.

Three false conclusions that already cost us time here: **an empty log is not a stopped advisor**
(a healthy EA with the stop released and an empty queue writes nothing); **MT5 flushes its log with
a delay**, so "no new lines" must be re-checked after half a minute instead of being declared at
once; and `BLOCKED: kill-switch state unavailable; HTTP 401` is neither a missing market nor a
missing command but exactly one thing: the key did not match.

### Instrument names come from the broker

`MetaQuotes-Demo` is not the reference spelling of an instrument, and the runner no longer pretends
it is. A requested name is matched against what the server actually serves: an exact match wins,
otherwise a six-character base (`XAUUSD`, `EURUSD`) is tried with suffixes of 1–3 characters, and
which suffix a given broker uses is that server's question, not the code's. One request that
several spellings match is resolved by refusal (`MT5 offers several variants of …; refusing to
guess which one this account trades`) rather than by the first line of a list: `XAUUSD` and
`XAUUSDc` are different instruments, and guessing means trading a cent lot instead of a full one.
The rest of the run then uses the server's own spelling, the renaming is written to the journal as
a `demo_symbol_alias` event, and the internal key name keeps two spellings from colliding in one
table. That is why `EURUSDm` from an Exness group, `XAUUSDc` on a cent account and the lowercase
`xauusd` stop being "symbol unavailable".

### Observation 2026-11-01 (four instruments, real terminal, market closed)

Demo account `114929502` on `MetaQuotes-Demo`, balance and equity 100 000 USD, leverage 100,
`trade_allowed`. Policy `agent_data/live_gold_ema_policy.yaml`: `mode: paper`,
`execution_mode: auto`, window 00:00–22:00 UTC, `blocked_weekdays_utc: [6]`. The run: 4 instruments
× one cycle every 20 s, and that process's journal stops at **1386 `agent_cycle` records, at
14:27:34 UTC**, every one with blocker `OUTSIDE_TRADING_WINDOW` and `broker_order_sent: false`,
chain valid. The number is a snapshot, not an invariant: a live run writes 12 records a minute
(4 instruments × 3 cycles), and by the time of this edit the bridge was held by a different runner
with the same `run_id` — because the run id, the kill-switch file and the served-id history all
derive from the tombstone path, not from a process number.

`BTCUSD` is deliberately absent from that list: the
broker does not offer it (`MetaQuotes-Demo` does not trade crypto), and the runner writes the
unavailability as its own line instead of silently dropping the instrument.

That journal contains no `KILL_SWITCH_ACTIVE` blocker at all, and the reason is not a dead stop: on
the demo path the stop has two different carriers. Gate 2 of the cycle order reads the
`kill_switch` section of the **policy** (`agent_data/live_gold_ema_policy.yaml` declares
`active: false`), while the durable `kill_switch.json` is checked by `DemoExecutor.offer()` — the
only place a command is ever built. With the market closed the cycle dies at the window gate and
never reaches `offer()`, so the stop armed from 13:52 to 14:27:59 UTC left no trace in the decision
journal; it left one in the advisor's log, which is exactly why transport is verified from the EA
log rather than from a blocker counter. That same log shows the block comes from the window, not
from broken transport: before 13:52 UTC the lines said `HTTP 401`, after the token was entered they
said `kill switch is active`, and after the release there was silence.

### Live observation on 2026-10-31 (`--symbols all`, real terminal)

Thirteen instruments were requested; the broker offers seven. The outcome of one cycle: **0
commands queued, 0 filled, `broker_order_sent: false` in every record, journal chain valid**.

| Instrument | Result | Why that is correct |
|---|---|---|
| EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD, XAGUSD | `BLOCKED` → `QUOTE_STALE` | last tick 2026-10-30T21:00:00Z; the market was closed for the weekend |
| US500 | `BLOCKED` → `MARKET_DATA_READ_FAILED` | `MT5 returned no tick for US500` — the specification reads, no quote exists |
| USDJPY, USDCAD, USDCHF, EURGBP | skipped before the cycle | `pays profit in JPY/CAD/CHF/GBP, not in the account currency USD` — sizing in account currency would be wrong |
| NAS100, BTCUSD | skipped before the cycle | `MT5 could not select symbol …` — not offered by this broker |

Quote age cannot be negotiated away: `max_quote_age_seconds` is only accepted between 1 and 300, so
a Saturday series always yields `QUOTE_STALE`. That is deliberate.

The reason this account would not have executed even with the market open is arithmetic on the live
specifications and the live H1 bars (a calculation, not a measured refusal): the risk budget is
`0.005 × min(equity, balance) = 0.05 USD` at a 10.00 USD balance, while the risk of the broker's
minimum lot is 3.27 USD on EURUSD, 3.35 on GBPUSD, 1.93 on AUDUSD, 1.98 on NZDUSD, 3.97 on XAUUSD
and 4.24 on XAGUSD. Every symbol would stop at `MINIMUM_LOT_EXCEEDS_RISK_BUDGET`. A 10 USD demo
account cannot host a single policy-compliant trade; at 10 000 USD the same 0.5% gives a 50 USD
budget and those numbers pass. From 2026-11-01 the run is on account 114929502 with a 100 000 USD
balance, which is exactly why "why is nothing executing" is no longer a sizing question: it is only
a question of the closed market and the blocked weekday.

### Running it

Secrets come from the environment only. Neither a `--bridge-token` nor an admin token on the
command line exists: an argument line is visible to the other users of the host and lands in a
`Win32_Process` snapshot, and neither the bridge state file nor the journal writes the key or the
response body.

```powershell
# 1. the terminal must be CLOSED: MT5 rewrites common.ini when it exits
python scripts/setup_demo_terminal.py                       # dry run: what would change
python scripts/setup_demo_terminal.py --apply               # build, install the EA, allow WebRequest
# 2. start the terminal, log into the demo account, attach CriticalConsensusEA_Demo to a chart by hand
# 3. turn AutoTrading on and keep the bridge open
$env:MT5_BRIDGE_TOKEN = "<24-256 characters>"
$env:MT5_KILL_SWITCH_ADMIN_TOKEN = "<at least 32 characters, different from the bridge key>"
python scripts/run_mt5_demo_trading.py --policy agent_data/live_demo_policy.yaml `
  --db agent_data/mt5_demo.db --symbols all --cycles 0 --execution confirm --verbose
```

`--cycles 0` means "keep going until the process is stopped". If the advisor is not attached, the
commands simply expire under the TTL — the journal reports them honestly as `expired`, and no order
reaches the broker. The AutoTrading button and `[Experts] Enabled=1` are two different switches: the
GUI button turns off everything, so `setup_demo_terminal.py` has to be re-run after any toggle made
inside the terminal; the transport check in the section above is precisely what separates "no
market" from "no permission".

The state paths and the run id do not depend on where the process was launched from. `--tombstones`
is resolved against the repository root, not the current directory, so a `cd` cannot move either the
stop file or the history of served ids; and the run id is derived by default from that path plus the
policy file instead of being random. That matters because `client_order_id` is built from it: a
random id per process would make every restart "the first order of its life", and the
duplicate-signal protection would stop protecting anything.

The runner's exit codes:

| Code | Meaning |
|---|---|
| `0` | the run finished because of `--cycles` or was stopped by the operator, and no command was left unconfirmed |
| `2` | the configuration was rejected before the run ever reached the market (policy, account binding, a missing or too short admin token with `--release-kill-switch`) |
| `3` | the run cannot start honestly: not one instrument is allowed by the broker, the bridge cannot open its `127.0.0.1` socket, the terminal's deal history cannot be read while commands were served, or the tombstones still hold served commands without a confirmed fill, which needs an explicit `--acknowledge-uncertain` |
| `4` | Ctrl-C after the command had already gone to the bridge (`interrupted_with_send_in_flight`): the same durable stop is armed, and that id may be touched again only after reconciling with the broker's deals |

An armed kill switch is neither an exit code nor a pause: the run prints the reason once at startup
and keeps counting cycles, because the durable stop lives in `offer()` and `offer()` runs only when
a signal already exists (the two carriers of the stop are described in the 2026-11-01 observation).

Releasing the stop while no runner is alive is possible only through the separate secret:

```powershell
python scripts/run_mt5_demo_trading.py --policy agent_data/live_demo_policy.yaml `
  --db agent_data/mt5_demo.db --release-kill-switch
```

## Languages

All operator-facing text — statuses, blockers, prompts, the summary, errors and the disclaimer —
exists in **Russian**, **English** and **Vietnamese**. Pick one with, in order of precedence:

```bash
MT5_AGENT_LANGUAGE=vi python scripts/run_mt5_agent.py --language en run ...   # flag beats env
```

then the policy's `ui_language`, then `ru`. The flag is accepted before or after the subcommand,
and if it is repeated the last occurrence wins, although every occurrence must name a supported
language.
`report` falls back to the language its own `--run-id` used (the newest run's language when no id is
given); `reset` uses the newest run's language, including when `--confirm` is missing and the
command is refused. `--help` prints the localized usage line, section titles and help entry; it is
built before the policy is read, so it follows the flag/environment/default language only. The machine codes in the
journal and in the JSON output do not change with the language (`blocker_histogram` stays
code-keyed), and `blocker_labels` renders the same keys in the selected language. `run`'s summary
JSON additionally carries `exit_reason_histogram` (codes such as `STOP_LOSS`, `TAKE_PROFIT_GAP`,
`END_OF_RUN`) and its `exit_reason_labels`; those two keys are not part of `report`'s JSON. `run`
and `report` write exactly one JSON object to stdout; `reset` writes
one localized sentence; prompts, the verbose trace, the summary and the disclaimer go to stderr.
Argument errors print the localized usage prefix and `Error:` line, although argparse's detail
message stays English; a rejected `ui_language` in the policy is reported in the selected language,
while other policy-validation text and terminal, CSV and backend failure reasons stay English too.

## Signal and volume

- `gap = (SMA_fast − SMA_slow) / ATR`; BUY requires `gap ≥ min_sma_gap_atr` **and**
  momentum > `min_momentum_abs`; SELL is symmetric. Otherwise `NO_TREND_SIGNAL`.
- Stop: `entry ∓ ATR · stop_atr_multiple`; target: `entry ± risk · take_profit_risk_multiple`.
  Both must respect the broker's `stops_level`.
- `confidence = 0.5 + 0.5·strength`, capped by `max_confidence` and tagged
  `heuristic_score_not_calibrated_probability`. It ranks setups; it is not a probability.
- Volume: `risk_per_lot = stop_distance · cash_per_price_unit`, where `cash_per_price_unit` is the
  account-currency value of a one-unit price move of one lot: `trade_contract_size` while
  `currency_profit` matches the account currency, otherwise `trade_tick_value / trade_tick_size`.
  The largest `volume_step` whose stop-out loss stays inside
  `min(equity, balance) · risk_per_trade_fraction`, and whose margin —
  `volume × cash_per_price_unit × entry price / leverage` — fits free margin. If even the minimum
  lot breaks the budget, the trade is refused — the stop is never widened. The tick pair alone
  understates metals: on MetaQuotes-Demo it says 10 $ per price unit per lot for XAUUSD against 100 $
  from the contract size, `order_calc_profit` and the margin actually blocked, and an order sized
  from it came out ten times the budget (`tests/test_mt5_contract_sizing.py`).

## Paper broker

`PaperBroker` implements the same order-free interface as the terminal, so the agent does not
distinguish the sources:

- `seek(moment)` moves the visible window; `bars()` and `tick()` never return data past the
  cursor — look-ahead is physically unavailable.
- The simulated clock reads the **close** of the last visible bar, not its open stamp: a bar's
  OHLC is only known once it closes, and dating the quote at the open would call it stale by up to
  one bar. Exits are still processed only on bars after the one the position was opened on.
- Within one bar the stop is checked before the target: a bar that touches both is a loss. A gap
  through a level fills at the bar open (`STOP_LOSS_GAP`/`TAKE_PROFIT_GAP`).
- A BUY is entered at the ask and exited at the bid, a SELL the other way round; the spread comes
  from each bar's `spread_points`, falling back to `SymbolSpec.spread_points` when a bar does not
  declare one or reports a non-positive spread.
- Daily baselines (`day_start_equity`, `peak_equity`, `realized_daily_pnl` counted per server day)
  drive the daily limits; if the backend cannot supply them the cycle blocks with
  `EQUITY_BASELINES_UNAVAILABLE`.

## Decision journal

One SQLite table, `audit_events`, is the whole audit trail: `agent_cycle`, `paper_trade_closed` and
`equity_point` events, chained with `make_audit_event`; each event is tagged with a `run_id` that is
part of the hashed body, which is what `--run-id` scopes a report to. Equity is chained like
everything else, so an edited drawdown row is a broken hash, not a statistic. If a run closed at
least one trade, closing the book writes a final equity point (best-effort: whether it landed and
any error are surfaced in the run summary as `closing_equity_recorded`/`closing_equity_error`, and
the run does not crash). `report()` returns cycle counts, the blocker histogram, drawdown,
`broker_order_sent: false` and `verified_chain`; its `initial_equity` can be passed explicitly (the
CLI run does), otherwise the first recorded point is used. `verify_chain()` fails on an edit and on
a row deleted from the middle of the chain: the first is `EVENT_CONTENT_HASH_MISMATCH`, the second
is `PREVIOUS_HASH_MISMATCH` because the next row links to a hash that is gone. A deleted **prefix**
is caught only with an anchor kept outside the file: `report()` self-anchors on the first surviving
event's `previous_hash`, so an honest prefix check needs an external `--verify-anchor <hash>`.
In that case the report is still written to stdout as the single JSON object, the exit code is 3, and
a localized explanation goes to stderr. Only an external anchor changes the exit code: when the
self-anchored `verified_chain` is invalid, `report` still exits 0, prints the JSON with
`verified_chain.valid: false` and writes a localized warning (`out.chain_invalid`) to stderr. The
CLI report's `anchor_check` carries the external check;
its `verified_chain` always verifies the run from its own first row. An anchor supplied against an
empty chain is refused (`PREVIOUS_HASH_MISMATCH`), so deleting every row is caught only when the
kept anchor is supplied; without an anchor an empty journal still reports an internally consistent
chain with `events: 0`. Only a non-empty string is an anchor: `--verify-anchor ""` counts as no
anchor.
`verified_chain.anchor_source` names the source: `provided` for an external anchor and
`first_event_previous_hash` for self-anchoring. Keep `anchor_hash` (the first row's
`previous_hash`) as the external anchor; `last_hash` is the chain tip and fails the anchor check
with `PREVIOUS_HASH_MISMATCH`. The first row of a brand-new journal has no `anchor_hash` (`null`),
so there is nothing earlier to anchor to. An anchor belongs to one run: pass the same `--run-id` to
the later `report`; without it, a journal whose earlier rows belong to other runs starts from an
earlier row and reports `PREVIOUS_HASH_MISMATCH` (exit 3).

## Running

```bash
cp config/mt5_agent.example.yaml config/mt5_agent.yaml   # then adjust it to your account
# The shipped example keeps kill_switch.active: true deliberately. Set it to false with a reason
# of your own before the first run, otherwise every cycle blocks with KILL_SWITCH_ACTIVE.
python scripts/run_mt5_agent.py --db agent_data/mt5_agent.db run \
  --policy config/mt5_agent.yaml --source csv --csv export_eurusd_h1.csv \
  --source-timezone Europe/London --balance 10000 --language ru
python scripts/run_mt5_agent.py --db agent_data/mt5_agent.db report --run-id <id> --verify-anchor <hash>
python scripts/run_mt5_agent.py --db agent_data/mt5_agent.db reset --confirm
```

Irreversible commands require an explicit `--confirm`: `run --fresh` (wipes the journal before a
run) and `reset` (wipes the journal). Without it the CLI exits 2 without creating or modifying
the journal. `reset` may still open an existing journal read-only to choose the language of its
refusal; no row is created, changed or removed. A plain `run` without `--fresh`, and `report`, work
without it.
`--source live` reads history from the installed terminal through `LiveMT5Backend` (a running
terminal or `terminal_path`, plus the `MetaTrader5` package), subtracting the required
`server_utc_offset_seconds`. It makes no trading call. `symbol_info()` is read-only, but when the
symbol is not yet in Market Watch, `symbol_spec()` falls back to `symbol_select()` — the documented
way to subscribe — which does add the symbol to the terminal's symbol list. It refuses a terminal
signed into a real account when `account.require_demo: true` (the default), and refuses a login or
server mismatch when `account.expected_login` / `account.expected_server` are pinned. Pinning is
enforced by the agent against the identity the backend reports: with `--source live` that is the
terminal's login and server, so pins must match it; a CSV paper run presents the fixed paper identity
(login `1000000000`, server `PAPER-LOCAL`). Any pin other than that paper identity blocks both CSV
cycles, so leave the pins null for CSV. Login and password are never passed on the CLI and never
reach the journal.

## Policy defaults

Fail-closed: `mode: paper`, `execution_mode: confirm`, `ui_language: ru`, `require_demo: true`,
`max_open_positions: 1`, `risk_per_trade_fraction: 0.005`, `max_daily_loss_fraction: 0.02`,
`max_drawdown_fraction: 0.06`, `max_quote_age_seconds: 15`, window `00:00–22:00 UTC`, Sunday
closed, `kill_switch.active: true` when the section is absent, and a missing
`server_utc_offset_seconds` is a load error rather than a silent UTC assumption. An unknown key in
the policy or in any of its sections is a load error. `strategy.take_profit_risk_multiple` below
`risk.min_risk_reward` is rejected as an impossible policy. The policy's `symbol_spec` supplies the
symbol specification for paper execution and sizing on every source; `--source live` supplies bars
and the account identity, not the symbol specification.

## Checking it on your own Windows VPS

1. Install the MT5 terminal and `pip install MetaTrader5` (Windows x64 only).
2. Log into a **demo account** in the terminal and keep it running.
3. Pin `account.expected_login` and `account.expected_server` in the policy. With `--source live`
   they must match the terminal; a CSV paper run presents the fixed `PAPER-LOCAL` identity, so
   leave them null there (otherwise every cycle blocks on the mismatch).
4. Disable the kill switch deliberately (`kill_switch.active: false` plus your own reason); the
   shipped example keeps it active, and an active switch blocks every cycle.
5. Set `server_utc_offset_seconds` to the broker server's real offset (whole minutes, ±14 h).
6. Start with `mode: observe` — the cycle records the decision but opens nothing.
7. Then `mode: paper` with `--source live` or a CSV export.
8. Check `report`: `verified_chain.valid` must be `true` and `broker_order_sent` must be `false`.
9. A small demo balance never reaches execution: at 10 USD with
   `risk_per_trade_fraction: 0.005` the risk budget is 0.05 USD, while the minimum lot of any of
   the symbols above risks 1.93–4.24 USD (see "Demo execution over the loopback bridge"), so the
   cycle stops at `MINIMUM_LOT_EXCEEDS_RISK_BUDGET`. Either open a 10 000 USD demo or raise the
   risk fraction knowingly and re-check the budget. On the account actually in use now (114929502,
   100 000 USD) this gate passes with margin, and the only block left comes from the closed market
   and the blocked weekday.
10. Only then is it worth preparing the terminal for demo execution:
    `python scripts/setup_demo_terminal.py` (dry run), then `--apply` with the terminal closed,
    then attaching `CriticalConsensusEA_Demo` to a chart by hand and switching AutoTrading on.

## Deliberately not modelled

- Swap, commission and financing; intra-bar slippage (paper fills use the post-approval quote,
  bounded to 10% of the stop distance); partial fills and requotes beyond that bound;
  `tick_value` drift when a quote currency moves; broker DST changes mid-run (the offset is a
  constant you declare). Intraday baselines live inside the `PaperBroker` process and do not survive
  a restart of a paper run; the demo runner's risk baselines are the exception both by design and by
  test: `demo_risk_baseline` is written to that same append-only journal and restored per
  `(login, server, broker day)`, so a process restart neither zeroes the daily baseline nor rolls
  back the equity peak
  (`test_risk_baselines_survive_runner_restart_and_are_scoped_to_the_broker_account`).
- Instruments whose profit is paid in a currency other than the account currency drop out of a
  live run: on a USD account `SymbolSpec.from_terminal` refuses USDJPY, USDCAD, USDCHF and EURGBP,
  and the running all-universe pass journals them as `demo_symbol_unavailable` - 8 of the 13
  instruments trade (this terminal serves no BTCUSD or NAS100 at all). The refusal is not paranoia:
  a cross's lot size is `contract_size` re-expressed through the quote currency's rate, so it moves
  with the pair. Measured on MetaQuotes-Demo 2026-11-02, the tick pair gives USDJPY 633.60 $ per
  price unit where `order_calc_profit` over one tick says 0.63 $ (630.00 per unit), USDCAD 71 567.62
  against 72 000 and USDCHF 121 228.29 against 121 000 - for crosses the broker's tick fields really
  do speak account currency. What is still missing is the margin leg: for a cross,
  `volume x cash_per_price_unit x price / leverage` numerically returns base units, not account
  currency, so it needs its own base-currency computation before the refusal can be lifted.
- No `order_send` or any other trading write in the Python package; the source scan
  `test_package_source_contains_no_order_sending_surface` covers the `mt5_agent` package only,
  while the CLI's own subprocess tests assert its JSON never reports a sent order. The MQL5
  reference template does contain an `OrderSend` call, but it sits behind the compile-time constant
  `ENABLE_DEMO_EXECUTION = false` (and a `AllowLiveExecution` input that must stay false), so it is
  unreachable without a source change and recompile; the Python agent never calls that template.
- The demo path in `scripts/` is not an exception to that rule: neither
  `run_mt5_demo_trading.py` nor `mt5_demo_bridge.py` contains a single MT5 trading call (a grep for
  `order_send|trade_|OrderSend` finds nothing). Python only holds an HTTP command alive until its
  TTL; the advisor inside the terminal is what can turn it into an order, which is why "Python sent
  an order" is impossible in this system even for demo.
- Not modelled and not measured: partial fills, a requote after `Done`, slippage relative to the
  approved entry, and a connection loss between the advisor and the bridge. All there is for those
  is the tombstones, the bridge's exact-ID record and a manual reconciliation against the deal
  history.
- No `mode: live`: the policy rejects it at load.
- The agent is not part of the authorization pipeline: `pipeline.evaluate_signal_bundle`, the
  Certainty Gate and human authorization remain the only (still closed) paths toward an order.
