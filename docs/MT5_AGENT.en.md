# MT5 agent: an observe/paper cycle on top of the terminal

This document describes the **MetaTrader 5 agent** layer — a neighbour of the
"reconnaissance → critical consensus → authorization" pipeline ([architecture.md](architecture.md)).
The agent reads terminal state, applies fail-closed risk gates and writes every decision to a
journal. It **never sends an order**: no module in `critical_consensus.mt5_agent` calls a trading
function, and a test enforces that rather than a promise.

Other languages: [Русский](MT5_AGENT.md) · [Tiếng Việt](MT5_AGENT.vi.md).

## Evidence status (read before use)

| Claim | Status | Evidence |
|---|---|---|
| A policy loads only with known keys; a live mode is rejected | **PROVEN** | `test_live_mode_and_unknown_keys_are_rejected` |
| One agent cycle applies its gates in a fixed order and blocks on any failure | **PROVEN** | 62 tests in `tests/test_mt5_agent.py`, 59 in `tests/test_mt5_agent_invariants.py`, 32 in `tests/test_mt5_agent_i18n.py` |
| Every blocker code the agent emits is declared, and has a label in all three languages | **PROVEN** | `test_every_emitted_blocker_is_a_declared_code`, `test_every_journaled_code_has_a_non_empty_label_in_all_three_languages` |
| The paper broker cannot see future bars and fills the stop before the target inside one bar | **PROVEN** | `test_paper_broker_hides_future_bars`, `test_paper_broker_fills_stop_before_target_in_the_same_bar` |
| Volume comes from the stop distance and the symbol specification, capped by risk *and* margin | **PROVEN** | `test_sizing_never_exceeds_the_risk_budget`, `test_sizing_caps_volume_by_margin_and_not_only_by_risk` |
| A journal edit is detected; a deleted prefix only against an anchor kept outside the file | **PROVEN** | `test_journal_detects_tampering`, `test_journal_anchor_detects_a_deleted_run_prefix`, `test_the_run_id_is_part_of_the_signed_body` |
| No journal record claims a sent broker order | **PROVEN** | `test_agent_never_reports_a_broker_order`, `test_package_source_contains_no_order_sending_surface` |
| `confirm` mode opens a position only on an explicit operator yes, and `auto` refuses a non-demo policy | **PROVEN** | `test_confirm_mode_without_an_approver_never_opens_a_position`, `test_automatic_execution_requires_a_demo_account_even_in_the_library` |
| The `LiveMT5Backend` adapter maps terminal replies and subtracts the server offset | **PROVEN against a stubbed package** | `test_live_backend_*`, `test_cli_live_source_subtracts_the_server_offset` |
| Reading a real MT5 terminal on this machine | **NOT PROVEN** | there is no `MetaTrader5` package and no `terminal64.exe` on this host, so nothing was read from a real terminal |
| Strategy profitability, and any trade-success rate | **NOT PROVEN** | backtest on a synthetic series; `confidence` is a heuristic score, not a calibrated probability. A 98% success rate is neither measured here nor achievable by this code |
| Execution on a real or a broker demo account | **NOT PROVEN and not implemented** | the Python package and the CLI contain no order-sending call, by design; the MQL5 reference template has a gated `OrderSend` (see "Deliberately not modelled") |

The local preview used while writing this document replays a **synthetic** EURUSD H1 series
(a seeded random walk with trends); it is not committed as an artifact, and the tests generate the
same series with their own `make_bars()` helper. The tests exercise the journal and the gates on
that series; any numbers they print illustrate mechanics, **not** a return estimate. Nothing here
decides when real money may be used: that is a separate, explicit decision outside this code, and
the current package cannot execute it either way.

## Package layout

| Module | Purpose |
|---|---|
| `mt5_agent/backend.py` | terminal access without order-sending: `AccountInfo`, `SymbolSpec`, `Tick`, `PositionSnapshot`, the `MT5Backend` protocol, the `LiveMT5Backend` adapter |
| `mt5_agent/policy.py` | `AgentPolicy`/`RiskLimits`/`StrategyConfig`/`TradingWindow`, YAML loading, refusal on unknown keys, `execution_mode`, `ui_language` |
| `mt5_agent/strategy.py` | deterministic SMA/ATR + momentum signal, stop/target geometry, blockers |
| `mt5_agent/sizing.py` | volume from `tick_size`/`tick_value`/`volume_step` under the risk budget and free margin |
| `mt5_agent/paper.py` | `PaperBroker`: bar replay without look-ahead, bid/ask-side fills, gaps, daily equity baselines |
| `mt5_agent/journal.py` | one SQLite hash chain (`audit_events`, equity points included), `verify_chain`, `report` |
| `mt5_agent/agent.py` | `MT5Agent.run_cycle()` — the gate order, confirmation gate, decision record |
| `mt5_agent/i18n.py` | Russian/English/Vietnamese labels for every code and every operator message |
| `mt5_agent/codes.py` | the single registry of statuses and blockers |
| `scripts/run_mt5_agent.py` | CLI: `run` (csv/live, auto/confirm, ru/en/vi), `report`, `reset --confirm` |
| `config/mt5_agent.example.yaml` | commented policy example |

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
locally in both modes; there is no broker order in either.

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
- Volume: `risk_per_lot = stop_distance / tick_size · tick_value`; the largest `volume_step` whose
  stop-out loss stays inside `min(equity, balance) · risk_per_trade_fraction` and whose margin —
  `volume × (tick_value / tick_size) × entry price / leverage` at the entry price — fits free
  margin. If even the minimum lot breaks the budget, the trade is refused — the stop is never
  widened.

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

## Deliberately not modelled

- Swap, commission and financing; intra-bar slippage (paper fills use the post-approval quote,
  bounded to 10% of the stop distance); partial fills and requotes beyond that bound;
  `tick_value` drift when a quote currency moves; broker DST changes mid-run (the offset is a
  constant you declare); baselines are per process, not persisted across restarts.
- No `order_send` or any other trading write in the Python package; the source scan
  `test_package_source_contains_no_order_sending_surface` covers the `mt5_agent` package only,
  while the CLI's own subprocess tests assert its JSON never reports a sent order. The MQL5
  reference template does contain an `OrderSend` call, but it sits behind the compile-time constant
  `ENABLE_DEMO_EXECUTION = false` (and a `AllowLiveExecution` input that must stay false), so it is
  unreachable without a source change and recompile; the Python agent never calls that template.
- No `mode: live`: the policy rejects it at load.
- The agent is not part of the authorization pipeline: `pipeline.evaluate_signal_bundle`, the
  Certainty Gate and human authorization remain the only (still closed) paths toward an order.
