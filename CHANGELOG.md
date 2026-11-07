# Changelog

## Unreleased — the park drives several brokers at once (the "one API port" premise was folklore)

- Measured on this machine before touching the code (`agent_data/probe_channel.py`, `agent_data/probe_channel2.py`, read-only): every running MT5 terminal publishes **its own** named pipe `MT5.Terminal.<64 hex>`, and `MetaTrader5.initialize(path=...)` answers from the terminal whose executable was named — from two Python processes at once, and even when both named the same terminal. Loopback `127.0.0.1:22346` was held by exactly **one** of the three terminals and did not decide who answered; an unauthenticated portable copy publishes its pipe and returns `(-10005, "IPC timeout")` until it logs in. The whole architecture had been built around the opposite claim.
- `fleet/terminals.py` (the mass-kill surface, deleted): `stop_all()` and `stop_other_fleet()` killed every `terminal64.exe` that was not this account's, because the port gate had to free "the" API port. In a park of different brokers that is not a cleanup, it is an orphan-maker: the killed terminal's account stops managing its exits while its positions stay open at the broker. `stop(account)` now kills only the process whose `ExecutablePath` equals that account's own portable `terminal_exe` (proved by `test_attaching_one_account_starts_its_terminal_and_touches_no_other` and `test_no_surface_left_stops_terminals_it_does_not_own`).
- `fleet/worker.py` (addressing, not possession): the port gate (`port_owner`, `port_is_free_or_ours`, the 22346 cache) is gone; in its place `answered_by(account)` reads `mt5.terminal_info().path` after `initialize` and refuses — fail-closed, before any balance is read or any order is sent — unless the directory the terminal reports is the account's own portable copy (casefold, trailing separators and quotes stripped). Two refusals that pre-existed it stay: the session's login/server must match the registry, and demo-vs-real is decided by the **broker's** `account_info.trade_mode`, not by the YAML label. Tests `test_attach_refuses_a_terminal_that_is_not_the_accounts_own`, `test_attach_refuses_a_terminal_that_hides_its_path`, `test_attach_accepts_its_own_terminal_whatever_the_letter_case`.
- `fleet/conductor.py` (several shards, one box): shards are only a way to spread accounts over worker processes, so the single-shard refusal and `--allow-multi-shard` are gone (`test_several_shards_are_allowed_on_one_machine`). `stop()` never touches a terminal, and `stop_tunnel()` is reached only for `network: vpn` accounts this machine actually runs — otherwise a conductor on box A switched off box B's VPN.
- `fleet/conductor.py::_remember_baseline` (the fail-open the multi-broker park exposed): the portfolio day was `MAX(server_day)` over the book, and brokers keep different clocks — a GMT+3 server is a whole day ahead of a GMT+0 server after 21:00 UTC there. When the GMT+3 shard went dark, `MAX` moved **backwards**, `stored_day != today` fired, and the day baseline was rebased onto equity that was already down, which reset the portfolio daily stop to 0 % and re-armed entries. The day is now clamped to `max(today, stored_day)`; proved by `test_brokers_on_different_clocks_keep_the_portfolio_baseline` (a −2 % book against a 1000 baseline still writes the PAUSE file instead of being re-based to 980).
- `fleet/config.py` + `scripts/fleet_cli.py` (`host` finally means something): `Account.host` was declared and documented as pinning an account to one machine but nothing read it, so a shared registry let any box provision, launch and **log into** an account the operator had pinned elsewhere. `runs_here()` now compares `socket.gethostname()` casefolded, `by_shard`/`shards` filter by it (so the conductor does not spawn a shard for a machine that owns nothing), and `init`/`once`/`check` refuse a pinned account with its host named. `run` prints how many accounts are local and which ones are pinned elsewhere.
- `fleet/config.py` (`PortfolioLimits.max_worker_processes`, deleted): it encoded the disproved one-terminal-per-session rule, and the registry is strictly validated, so **an existing `config/accounts.yaml` carrying that key now fails to load** with `неизвестные ключи ['max_worker_processes']` — delete the line. `config/accounts.example.yaml` now shows a three-broker park (`terminal_source` per platform, e.g. `C:\Program Files\Exness MT5Real26`, `C:\Program Files\GTC Go MT5`) and documents that per-broker symbol suffixes (`XAUUSDc`, `EURUSD.pro`) are resolved by `fleet/broker.py::resolve` against the terminal that owns the account.
- `docs/FLEET.md` rewritten where it taught the false premise: the limits section now states the measurement, adds "who answered me" as a documented gate, and says the park scales with the terminals it has rather than with the machines.

### What the critics found once several brokers really shared one book

- `fleet/conductor.py::_remember_baseline` (the clamp above was necessary but not sufficient): `MAX` still let one GMT+3 broker that crossed midnight **pull the portfolio day forward** while the GMT+0 brokers were still inside the day the baseline was measured on, so the same −2 % book was rebased as if a new day had begun. The day is now `Store.live_server_day()`, `MIN(server_day)` over rows seen in the last `BOOK_FRESH_SECONDS` **of this machine's accounts** — the slowest live clock, which can only advance when every broker still trading has advanced. `test_the_portfolio_day_waits_for_the_slowest_live_clock` (an Exness shard on 2026-11-02 next to a GTC shard on 2026-11-03 keeps the 11-02 baseline at 2000, and the day stop still fires).
- `fleet/worker.py::_portfolio_day_pct` (the mirror image of the same bug): with the day correctly held at the slowest clock, the **ahead** account's own `server_day` no longer equalled it, and the gate compared the two for equality — so exactly the broker that rolled over first got `0.0`, i.e. an unbounded daily loss allowance, at the moment its equity was already down. The comparison is now directional: a clock that has **not reached** the measured portfolio day returns `0.0` (there is nothing to compare yet), while a clock already past it is still inside that day and is measured against it. Proved by `test_the_day_stop_still_bites_on_a_broker_clock_that_is_already_ahead` (`−2 %` reported against a 1000 baseline on the ahead clock, `0.0` on the one behind it).
- `fleet/store.py` + `fleet/worker.py` (a peer's stop was worth zero): the portfolio stop-risk sum read every open position in the book and priced only what **this** terminal could see, so `EURUSDm` on a peer's broker — a symbol this `terminal_info` has no contract for — contributed nothing, and the more brokers in the park the larger the blind spot. The owner now stores its own per-position dollar risk in `positions.risk_usd` (`ALTER TABLE` on an old book, so pre-multi-broker rows simply read as unknown); a reader with no local contract uses that number, and a ticket whose risk nobody priced **refuses all new entries** and emits `portfolio_risk_unpriced` rather than counting it as zero. Tests `test_a_peers_stop_risk_is_counted_in_dollars_not_as_zero`, `test_the_order_goes_through_when_no_peer_risk_is_outstanding`, `test_an_unpriced_peer_stop_refuses_entries_instead_of_counting_zero`.
- `fleet/config.py::local()` + `tick()`/`_try_entries`/`_book_flows` (`the book belongs to a machine`): every aggregate was `SUM` over the whole `book` table, so a row left by a account pinned to another host — stale by definition, since nobody here refreshes it — moved this machine's portfolio equity, its day baseline and its daily stop. All reads now filter to `config.local()`; `prune()` deliberately still keeps `enabled()` so ignoring another box's rows never **deletes** them. Test `test_a_row_left_by_another_machine_weighs_nothing_here`.
- `fleet/naming.py` (new; caps were being silently skipped): `is_gold`/`is_usd_pair` matched the whole symbol string, and each broker names the same instrument differently (`EURUSDm`, `EURUSD.pro`, `XAUUSDc`), so a suffixed position counted as no class at once — gold cap, USD-pair cap and the per-symbol cap all passed a book that had already broken them. Classification now runs on the six-letter base name (`fx_base`), with `USDT` refused as a dollar pair on purpose. Test `test_a_suffixed_symbol_still_counts_in_its_class_on_every_broker`.
- `fleet/conductor.py::parse_shards` + `--max-shards` (`a typo that trades nothing looks healthy`): `--shards` was split and cast ad hoc, so `zero` raised an uncaught `ValueError`, `0,1,0` spawned the same shard twice — two processes racing one lock for one set of accounts — and `7`, an id only another machine owns, started a conductor supervising no children while printing a normal status. It now validates against `config.shards()` on this host, and `MAX_SHARDS` is a documented, overridable ceiling (`--max-shards`) because the real limit is the desktop heap each extra terminal needs, not the number of brokers. Tests `test_a_demand_for_shards_is_checked_against_this_machines_registry`, `test_the_park_refuses_more_shards_than_one_desktop_holds`.
- Tests: 23 in `tests/test_fleet_multi_terminal.py` (identity gate, no cross-terminal kill, three-broker registry → three distinct portable dirs, host pinning, broker clocks both directions, peer risk in dollars, unpriced peer refusing entries, machine-local book, suffix-tolerated classes, shard validation, deleted helpers really gone) and the pause/resume regression rewired to assert that ending the park stops shard processes and **never** a terminal.

### What the next two critics found, including in the fixes above

The first round's money logic was reviewed by a critic that reads the code, not the prose. It returned seven more defects, and after they were fixed a verification pass returned six more — five of them in the fixes themselves. Every one of those was reproduced before being changed.

- `fleet/store.py` + `fleet/worker.py` (`the day was per-machine, but only on paper`): the baseline keys were host-scoped while `portfolio_baseline()` still **fell back** to the unscoped legacy pair, so both boxes read the same number and the scoping changed nothing after an upgrade. Worse, the intended takeover (`claim_portfolio_baseline`) was dead code — it asked whether the scoped keys were empty, and the fallback had already filled them. Readers are now scoped-only, and each machine copies the legacy pair under its own host **once** (`portfolio_legacy_claimed@<host>`) instead of deleting it: clearing the shared key on the first claim would leave the second machine with no measured day, and the entry gate reads a missing baseline as "nothing to stop for". Tests `test_each_box_starts_from_the_unscoped_baseline_it_finds`, `test_two_machines_do_not_trade_under_each_others_baseline` (which previously asserted a key the code never wrote, and so proved nothing).
- `scripts/fleet_cli.py::reset-day` (`an operator switch for one machine`): it also wiped the unscoped legacy pair, which is exactly the only baseline a second box that had not started yet could read — so resetting A silently disarmed B's day gate. It now clears only `…@<hostname>` keys plus that host's hold markers. Test `test_a_reset_day_on_one_machine_leaves_the_other_one_measured`.
- `fleet/conductor.py::_registry_hash` (`a peer's registry is not this day's event`): the hash covered `config.enabled()` park-wide, so any account added or removed **on another box** re-based this machine's day onto today's equity mid-day — which is the one write that turns yesterday's loss into today's starting money. It now covers `config.local()`. Test `test_a_change_on_another_box_does_not_rebase_this_day`.
- `fleet/worker.py` (a zero is not a measurement): a broker whose `trade_tick_value` comes back empty priced the stop at `0.0`, and `0.0` in the mirror is a number the portfolio cap believes. A non-positive price is now never written (`_mirror_risk_usd` skips it, the row stays `NULL`), and a reader treats `NULL` **and** `0` as unknown → `portfolio_risk_unpriced` + no entries. Tests `test_a_zero_dollar_stop_price_stays_unknown`, `test_a_row_priced_at_zero_by_another_broker_refuses_entries`.
- `fleet/worker.py` (nobody re-prices a peer's broker): the risk sum had fallen back to *this* terminal's contract and *this* account's dollar rate for a symbol it does not trade — a cent or ruble account would have cost 100× or 10 000× of its real risk. Only the owning shard prices its own rows; everything else is unknown. Adding that path crashed on the reconstructed mirror namespace (`'types.SimpleNamespace' object has no attribute 'ticket'` at `fleet/worker.py:265`), because the book rows were rebuilt without the ticket `_atr_entry` needs — fixed by carrying `ticket` in `book_positions`, which is a genuine latent crash the new test found. Test `test_a_stop_this_broker_cannot_price_is_never_priced_here`.
- `fleet/store.py::reserve` + `fleet/worker.py` (the money cap was still not atomic): `reserve` summed **only** `risk_slots`, while the held positions' risk was added by `can_open_portfolio` *outside* the transaction, so neither check ever saw the other's total. Probed: 15 USD of open mirror risk against a 2 %-of-1000 cap, three workers reserving 4 USD each — all three returned `ok` and the book committed 27 USD against a 20 USD ceiling. `reserve` now takes `open_risk_usd` and adds it to the reservations inside the same `BEGIN IMMEDIATE`. Test `test_the_money_cap_counts_positions_that_are_already_open`.
- `fleet/worker.py` (a crash must not keep the reservation): `_try_entries` caught only `BrokerError`, so any other exception left the slot reserved for its full 600 s expiry — a money reservation nobody owns, refusing entries park-wide for ten minutes. Any exception now releases the slot before propagating. Test `test_a_crash_after_reserving_gives_the_money_back`.
- `fleet/worker.py` + `fleet/store.py::all_positions` (one peer's dark shard vetoed every machine): the position mirror was read park-wide, while `prune()` deliberately never deletes a row another registry might be writing — so a registered-but-silent account's unpriced position refused entries **everywhere, forever**. The mirror is now scoped to this machine's book, the same scope every other aggregate already used. Test `test_an_account_another_machine_writes_does_not_veto_entries_here`.
- `fleet/conductor.py` (the day hold could freeze the deployment it was written for): the hold compared freshness against `config.local()`, but a conductor only runs `self.shards`, so `--shards 0` — a documented way to start a park — held the portfolio day for good. It now holds only against the shards this process spawns, publishes the hold in `conductor.json` (`day_held: {day, dark, held_seconds}`), re-announces it on a fixed cadence rather than once per name-set, and stopped announcing a `portfolio_day_resumed` over a weekend, when every clock is dark and nothing was ever handed back. Tests `test_a_shard_this_conductor_does_not_run_does_not_hold_the_day`, `test_an_all_dark_book_does_not_announce_a_resume_it_never_had`.
- `fleet/naming.py` (a prefix is not a suffix): the class came from the first six letters, so `PRO.EURUSD` and `FX:USDJPY` — platforms that prefix the feed name — fell out of every class cap. Both ends are now tried and the unreadable `fx_base` helper is gone. Verified against `USDTUSD` (a dollar pair, contrary to one review note), `XAGUSD` and `BTCUSDT`: those land in the dollar-lot cap on purpose, because a silver or crypto leg quoted in dollars is dollar exposure whether or not it is a "pair".
- Tests: `tests/test_fleet_multi_terminal.py` is now 39 tests, and three of them were rewritten because they would have **passed with the fix reverted** — they seeded the unscoped keys and asserted the unscoped keys, so they only ever exercised the fallback this round removed.

### What the fourth critic's round closed, including a hole the round-3 fixes left behind

The critic's headline ("the tree was reverted, defect zero") turned out to be an artifact of a stale copy of `fleet/` that an earlier round had left in the git-ignored `agent_data/head_tree/`; that snapshot was readable as the working tree, so it is deleted. Every claim was then re-checked against HEAD `3e5dd2d`, and four real defects survived the check — one of them alleged, three of them found while pinning the others.

- `fleet/worker.py::_portfolio_day_pct` (`a missing measurement answered as 0 %`): the gate that exists to catch an unmeasured day **was** the fail-open. Probed on a throwaway book — no baseline, the state `reset-day` leaves, a baseline of `0`, and a baseline of garbage each returned `0.0` for a book already down 20 %, so `can_open_portfolio` was told "today has cost nothing" at exactly the moment it had no way to know. It now returns `None`, and `None` refuses entries: one `portfolio_day_unmeasured` event per account, `portfolio_day_measured` when the number comes back. Test `test_an_unmeasured_day_does_not_read_as_a_zero_percent_day`.
- `fleet/store.py::portfolio` / `all_positions` (`an empty list meant the whole file`): both built their `WHERE` clause from `if active:` / `if names:`, so the machine-local scoping added last round degenerated to **no filter** precisely when a machine owns nothing — and `tick()` passes `config.local()`, which is empty whenever every account is pinned to another box. Reproduced end-to-end: such a machine wrote its own day baseline as `('', '500.00')`, i.e. someone else's equity as this machine's starting money, with an empty day string, and announced it as `baseline_rebased`. `None` still asks for the whole file — that is the operator's view — while `[]` now means "nothing here", which leaves every cap, the day baseline and the day stop without a peer's number to mistake for their own. Tests `test_an_empty_book_is_not_the_whole_shared_file`, `test_a_machine_with_no_accounts_of_its_own_writes_no_day`.
- `scripts/fleet_cli.py::reset-day` (it also rewrote history): the wipe list still cleared `portfolio_registry_hash@<host>`, which records which accounts the book covers, not which day it is. Losing it made the next tick take the "composition changed" branch and publish `baseline_rebased` blaming a registry change that never happened — the trail then documents an event that the operator did not cause. A day reset now clears the day, its baseline and that host's hold markers, and nothing else. Pinned inside `test_a_reset_day_on_one_machine_leaves_the_other_one_measured`.
- `fleet/conductor.py` (a hold that shouted): the dark-shard hold re-announced itself every hour, so a broker switched off for a weekend wrote ~168 identical `portfolio_day_held` events into the trail that the operator reads to find the one problem. The cadence is now `DAY_HOLD_REANNOUNCE_SECONDS` (6 h); the state file still carries the live hold every tick, so nothing is hidden by waiting. Test `test_a_day_held_for_hours_is_announced_once`.
- `scripts/fleet_cli.py::status` (`the money line was not this machine's money`): it summed the whole shared file and printed it as *портфель*, while every limit the park applies is machine-local — with two boxes in one file, the operator read another machine's equity as his own result. The totals now come from `config.local()`, a row owned by a peer is marked `← другая машина`, the count of foreign rows is stated, and a book with no readable registry says so instead of pretending to be scoped. Test `test_status_reports_this_machines_book_not_the_shared_file`.
- Tests that only ever passed over an unmeasured day: `test_try_entries_places_one_order` and `test_cycle_runs_the_real_entry_path` went red the moment the day gate became fail-closed. They had never seeded a measured day (and the first never seeded a book row at all), so the entry path had been proven against a baseline that does not exist — the same hole, approached from the other side.
- Whole suite on this checkout: **1279 passed, 10 skipped in 100.17 s**, of which **113** are the five `tests/test_fleet*.py` files (44 in `tests/test_fleet_multi_terminal.py`).

### What the fifth critic round closed: the census became evidence, and two tools stopped killing things they do not own

Four critics reviewed the window census added in the previous round. Every finding below was
reproduced against the working tree before being changed, and the census was re-measured on this
machine with the shipped code (`python scripts/fleet_cli.py terminals`): two windows answering as
`114929502@MetaQuotes-Demo` with balance 99 331,31 USD and two windows answering
`(-10005, 'IPC timeout')` — 8,63 s for the whole field (8,37 s on the earlier run).

- `fleet/worker.py::_second_window` (`an uncounted field is not a clean field`): the gate read a
  missing census as "no second window" and, when one existed, ignored any window it could not read.
  It now fails closed on all three states — no census of this machine, a census older than
  `ROSTER_EVIDENCE_SECONDS`, or a window that neither named its account nor measured silent — with
  `roster_unknown` in the journal. Silent is a real category and is not blind: a window publishing
  its pipe and answering `(-10005, 'IPC timeout')` holds no session, therefore no margin, therefore
  it is not a driver. The all-clear is now only announced when a **fresh** census saw the field
  (`census="fresh"`), so the line never means "the evidence expired and we guessed clear". Tests
  `test_no_census_at_all_is_not_a_clean_field`, `test_an_out_of_date_census_is_not_evidence`,
  `test_a_window_the_os_would_not_name_closes_the_entries`, `test_the_parks_own_window_is_not_a_second_window`.
- `fleet/worker.py` (`the census is a fact about one machine`): the roster and its error keys were
  unscoped, while the shared SQLite book is written by several boxes — so box A traded on the
  windows box B counted, and its own second driver never appeared. Keys are now host-scoped, the
  same way the portfolio day keys already are. Test
  `test_a_census_written_by_another_machine_is_not_this_machines_evidence`.
- `fleet/worker.py` + `fleet/terminals.py` (`a probe must never raise a terminal`):
  `initialize(path=...)` **starts** a terminal that is not running, so a window that died between
  the process walk and the probe would have been opened by the census — with whatever session it
  was seeded with. `pids_alive()` re-checks the whole pid set in one query before any probe, and an
  empty `ExecutablePath` (Win32 hides it from another user's or an elevated session) is recorded as
  a named blind spot instead of being probed: `initialize(path="")` means the default registered
  terminal of this machine, which is somebody else's window. Tests
  `test_a_window_that_died_before_the_probe_is_never_addressed`, and the blank-path assertion in
  `test_a_window_the_os_would_not_name_closes_the_entries`.
- `fleet/worker.py` (`believe the window that answered, not the path we asked`): a probe now keeps
  the folder the terminal itself reports, falling back to the requested path only when the answer
  is empty; and a window that opens the channel but returns no `account_info` is an error row, not
  a silently empty one. `duplicated_accounts` groups on `login@server` casefolded and counts one
  entry **per window**, so two windows of one folder count twice — it is the count of drivers that
  spends the margin. Tests `test_the_census_believes_the_window_that_answers_not_the_path_we_asked`,
  `test_duplicated_accounts_does_not_merge_two_brokers_one_login`.
- `fleet/worker.py` (`the cadence is a measured cost`): 900 s refresh against 3600 s evidence meant
  a census could be an hour older than the moment it was retaken — every cycle then traded on a
  count nobody could vouch for. Now 300 s / 900 s, sized by the measured census cost above rather
  than by taste; `test_the_census_cadence_is_written_in_minutes_not_symbols` pins both literals and
  their order. `run_shard` stopped taking its own census before the accounts' cycles, and the
  census is one walk per refresh window per process rather than per account
  (`test_the_census_costs_one_walk_per_refresh_not_per_account`).
- `fleet/risk.py` (`a dropped read is not an empty book` — money): `positions_for` returned `[]`
  when `mt5.positions_get()` answered `None`, and the store's position mirror is **replaced** from
  that list: one failed IPC deleted this account's open risk from every shard's money cap (which
  then saw free risk and opened more) and skipped the exits of positions still live at the broker.
  It now raises, and `own_positions` goes through the same reader.
- `fleet/worker.py` (`a read that fails after the fill must not keep the reservation`): the
  post-fill mirror refresh sat between `reserve` and `release` and was not covered by the
  exception handler, so any failure there held a dollar reservation for its full 600 s and refused
  entries park-wide. The re-read is wrapped, the failure is journalled as `mirror_refresh_failed`,
  and the slot is released either way.
- `fleet/worker.py::attach` (`a hand-typed label is not a different session`): server comparison is
  case- and whitespace-insensitive. A registry written as `metaquotes-demo` against a broker that
  spells `MetaQuotes-Demo` grounded a live account forever.
- `scripts/fleet_cli.py` (`the operator could not read the tool that tells him who is in the box`):
  every line the park's CLI prints is Russian, and it was the one CLI in this repo that did not
  reconfigure its streams, so on a cp1252 console `terminals` died at the first Cyrillic character
  of the header — and, because `UnicodeEncodeError` is a `ValueError`, died again inside the error
  handler printing "ошибка конфигурации". Reproduced against the previous commit and covered by
  `test_the_cli_reads_aloud_on_a_legacy_console`. `status` now also counts the windows that did not
  name an account, and says plainly that with no census of this machine entries are closed while
  exits keep running.
- `fleet/network.py` + `fleet/conductor.py` (the last mass-kill surface): `stop_tunnel()` ran
  `taskkill /IM openvpn.exe /F`, which closes every OpenVPN on the box — on a desk that reaches its
  brokers through its own VPN, ending one account's tunnel put every other account dark. Sessions
  are now read once with their command lines and only the ones the park started (marked by
  `--log <TUNNEL_DIR>/...`) are signalled, optionally for one profile; the conductor journals how
  many it stopped. Tests `test_stopping_the_tunnel_leaves_the_desks_own_vpn_alone`,
  `test_the_tunnel_reader_separates_a_pid_from_its_command_line`.
- Accepted, and now documented instead of quietly kept: `scripts/system_stop.ps1 -All` still closes
  every `terminal64.exe` on the machine. It is the desk's emergency switch, not a park code path —
  `docs/FLEET.md` says so, including that positions left behind stay unmanaged until a cycle raises
  the window again. Orphan `second_window:*`/`no_symbol:*` meta rows are not pruned: they cost one
  row per account, the gate itself recomputes from the census every cycle, and `status`/`terminals`
  print the duplication whatever the journal decided to repeat.
- Tests: `tests/test_fleet_multi_terminal.py` is now **70** tests and the five fleet files **141**;
  two older entry tests went red the moment the gate became fail-closed — they had been proving the
  entry path against a field nobody had counted, so they now seed a census
  (`test_fleet_regressions.py::_own_window`). Whole suite on this checkout:
  **1307 passed, 10 skipped in 117,31 s**; `scripts/check_mql5_safety.py` — **473 invariants,
  105 mutations rejected**, which still does not prove compilation or execution on a terminal.
- `docs/FLEET.md` (`where is the money`, answered from the book rather than from the design): the
  park's own SQLite state was measured and written down — `trades 0`, `positions 0`, `book 0`, so
  the profit the park has produced is **0,00 USD**; the single `equity` row (balance 99 311,33,
  equity 99 310,21, 5 positions, floating −1,12) is the operator's own manual book on 114929502,
  because `positions_for` deliberately reads the whole account. The four cycle events are named
  with their timestamps, including that the last one's `opened: 1` counts a **dry-run** signal
  (USDJPY buy 0,1) and that no order ever left for the broker.

### What the ownership and measurement critics found: the park signed somebody else's lots, and its own bill never printed

Both accusations came from critics that read the installed API instead of the code's comments, and
both were true. They were reproduced before being fixed: run against a snapshot of the previous
`HEAD` (`agent_data/pre_fix`, same day), **18 of the tests in `tests/test_fleet_regressions.py`
fail** — all seven of "whose lot decides which cap" and eleven of the measurement/candidate ones.
The two new tests that pass there are `test_the_reply_object_itself_carries_no_position_id` and
`test_a_broken_import_cannot_be_sneaked_past_the_allowlist`: they assert facts that were already
true, which is exactly why the code was wrong and the suite was not.

- `fleet/risk.py` + `fleet/worker.py` (`a shared account is not our book`): `can_open` counted
  **every** position on the account against `max_open_positions`, and the exit pass modified and
  closed whatever was on the signal symbol — including the operator's manual lot. Concentration
  caps now read `risk.own_in(account, positions)` (magic equal to the account's), while the money
  caps keep reading the whole account, because a manual lot moves the same equity. The exit pass
  takes `own` and leaves `foreign` untouched, and reports it instead.
- `fleet/config.py` (the account that would own everything): `magic` is the only field separating
  the park's lots from the operator's, and MT5 marks a manual trade with `0`, so a registry with
  `magic: 0` made every manual lot "ours". `magic <= 0` is now a `ConfigError` at load.
- `fleet/worker.py::_report_foreign_lots` + `_report_change` (a signal that fires every cycle is
  noise): the foreign book was announced once per ever; it is now announced once per **change** of
  the ticket set, from one meta row per account, and a lot the park entered (an `atr:` note exists)
  that now reports a foreign magic is announced as `park_lot_unattributed` — nobody is trailing it.
- `fleet/worker.py::_meta`/`_note_meta` (a locked journal must not stand between an account and its
  exits): `set_meta` propagated `sqlite3.OperationalError` after its 15 s busy timeout, so a report
  could raise inside the exit pass. Reads and writes of report fingerprints now swallow only
  `sqlite3.Error` and return "not noted", which re-announces next cycle rather than losing the fact.
- `fleet/broker.py::resolve_ticket` (the measurement that justified the feature never fired):
  `Execution.position_ticket` came from `getattr(result, "position_id", 0)`, and
  `MetaTrader5.OrderSendResult` is an 11-field structseq — `retcode, deal, order, volume, price,
  bid, ask, comment, request, request_id, retcode_external` — with **no** `position_id`. Every row
  got ticket 0, `ledger()` joined nothing, `mean_drift_points` was `None` and `drift_cost_usd` 0,00
  forever. Tickets are now resolved from the `fleet-<6 hex>` comment the order was stamped with,
  falling back to "exactly one own lot on this symbol" and otherwise refusing to guess, with the
  level recorded per row as `ticket_source` (`token` / `only_position` / `unknown`).
- `fleet/strategy.py::_side_price` (a sell was billed for a cost it never paid): the signal price
  was the ask for both sides, so every short entry showed the full spread as "drift, the price of
  waiting". Buys are stamped at ask, sells at bid, and the stop/target geometry hangs off the same
  side's price; `(bid, ask)` come from one tick read.
- `fleet/candidates.py` + `scripts/fleet_cli.py` (three columns overclaimed): the profit column is
  `profit_gross_usd` and is printed as gross — the broker's own figure, no commission, no swap —
  because the park has no commission rate of its own to charge it with; a ledger cut short by its
  limit prints `journal_rows` and says it is partial instead of looking like an empty book; a row
  with no ticket says `нет билета` rather than `открыт`; and non-finite broker reads are blanks,
  never numbers. The join is `(account, symbol.casefold(), ticket)`, consumed with `pop()`, so one
  close cannot pay out two trades and a close on another account cannot pay this one.
- `fleet/candidates.py::record` (a note that silently overwrites a journal column): keys the journal
  owns (`ts`, `account`, `kind`) now raise instead of being spread into `Store.event`.
- `fleet/store.py` + `fleet/worker.py`: `events_of(kinds, limit)` and `count_events(kind)` so the
  ledger reads candidates and closes in one pass and can tell a truncated read from a small book;
  a candidate note that raises writes `candidate_note_failed` and cannot abort the cycle behind it.
- Tests: `tests/test_fleet_regressions.py` grew the two sections
  "naming the position a fill made (measured against the installed API)" and
  "whose lot decides which cap", plus candidate-behaviour tests including
  `test_the_reply_object_itself_carries_no_position_id` — the root cause is asserted against the
  installed package, not against a comment. Whole suite on this checkout: **1343 passed,
  10 skipped in 129,31 s**; the five fleet files are **177**; `scripts/check_mql5_safety.py` re-run
  same day: **473 invariants, 105 mutations rejected**. Under `FLEET_STRICT_GUARD=1` the fleet
  files stay green, so none of them writes to the combat journal — which still holds
  `3 cycle + 1 entry(dry)`, zero `opened`, zero `candidate`: the drift bill starts with the first
  real lot, and `entry(gated)`/`entry(dry)` deliberately never enter the book.

### What this round closed: three gates that did not exist, and a book that stopped adding up what it cannot price

Eighteen defects, each reproduced before being changed, and each cited by the test that is red on the
tree before its fix. Two baselines, both measured on 2026-11-04 with this checkout's own interpreter:

- against the pre-fix snapshot (`agent_data/pre_fix`), `tests/test_fleet_regressions.py` fails
  **49 of 90** and `tests/test_fleet_ops.py` + `tests/test_fleet_multi_terminal.py` fail **12 of 100**;
- against the commit this round's edits sit on top of (`HEAD` 1986f8f, exported with `git archive`
  and handed only the new test files), `tests/test_bridge_ea_gates.py` is **12 failed, 4 passed**
  (re-measured 2026-11-05; the earlier claim of 9 was written when that file held 13 tests) and the
  four command-addressing tests are **4 failed**.

The full red-proof of that tree, file by file, is in the next section: it turned out that one of
these doors does not fail at `HEAD`, it never stops.

- `fleet/store.py::_MIGRATIONS` (a book written by an older park refused its own new writes):
  measured on the production journal before touching anything — `equity` had 7 columns (no
  `currency`, no `rate_to_usd`) and `trades` still had the pre-contract split (`price`/`sl`/`tp`
  instead of `requested_price`/`requested_volume`/`token`). The current `INSERT`s raised
  `OperationalError` on that file: one inside the cycle **before** any entry, one **after** the broker
  had already accepted the order. `CREATE TABLE IF NOT EXISTS` cannot help — it never touches a table
  that exists. The missing columns are now added by `ALTER TABLE` on open, the same way `risk_usd`
  already was. Test `test_a_book_written_before_the_contract_columns_gains_them` opens a book without
  those columns, then runs both cycle writes — `equity(...)` and `trade(...)` — through it.
- `fleet/store.py::prune` (the cooldown was deleted every 30 seconds): one rule covered two different
  kinds of marker — `atr:<account>:<ticket>` and `entry:<account>:<symbol>` — and the ticket
  membership test deleted `entry:` rows the moment their symbol had no open position. Reproduced on
  the old code: with `cooldown_minutes: 45` a prune cycle erased the marker, so the park could
  re-enter the same symbol on the very next cycle, forever. Ticket notes (`atr:`, `atrlock:`,
  `lotunpriced:`, `bestops:`) now die with the position, and cooldown markers age out by the
  timestamp in their own value (`COOLDOWN_KEEP_DAYS = 7`). One test holds both halves:
  `test_the_prune_keeps_the_cooldown_and_loses_the_dead_ticket_note`.
- `fleet/terminals.py::_detach` + `_seed_saved_session` (provisioning wrote through the operator's
  install): the portable folders are assembled from **hardlinks**, and measured on this machine
  `Config/common.ini`, `Config/terminal.ini` and six more files under
  `C:\mt5-fleet\terminals\114929502-metaquotes-demo` each carried three links, one of them in
  `C:\Program Files\MetaTrader 5\Config`. `open(path, "w")` truncates the inode, not the name, so
  `init` was rewriting the settings of the terminal the operator trades in — his server list, his
  window layout, his saved logins. Every write now detaches the name first. Second defect in the same
  function, and worse in practice: the session files were cleared **before** checking the replacement
  existed, so a registry entry with no `seed_config` (or one pointing at a folder without an
  `accounts.dat`) deleted a working login and copied nothing back — the terminal then starts at a
  password prompt the park cannot answer. Tests
  `test_a_portable_config_on_a_shared_inode_is_detached_before_it_is_written`,
  `test_a_second_provision_without_a_seed_keeps_the_login_that_already_works`.
- `fleet/worker.py::_journal_broker_closes` + `fleet/store.py::mirrored_positions` (the exits that
  were never billed): every close ordered by the park journalled its own profit, and a lot the
  **broker** closed at its stop or target left no trace — the ticket simply vanished from the live
  list. A candidate book joined by `(account, symbol, ticket)` therefore called a finished trade
  `open` forever and kept counting its drift bill against a profit column that stayed empty. The
  previous cycle's mirror is now the witness for two questions the live list cannot ask — which of
  our lots are gone, and which lot grew volume nobody here ordered — and the closes are journalled
  from the broker's own deals (`position_result`), with `by` naming who closed it. An unreadable
  history journals the close **unpriced** (`priced=False` → `profit` NULL) rather than as a flat exit.
  Tests `test_a_lot_the_broker_closed_is_journalled_with_its_own_profit`,
  `test_a_broker_close_the_history_cannot_price_is_left_unpriced`.
- `fleet/worker.py::_merged_lots` (the netting account folded his lot into ours): on a netting account
  the operator's manual trade in the same direction merges into the position the park opened. The
  magic still says `ours`, so the old ownership split happily trailed and closed a ticket that had
  become two people's money — closing his lot along with ours without asking. Growth past the
  mirrored volume stops `modify` and `close` for that ticket and announces `lot_merged_with_manual`;
  a *shrunken* lot is not a merge (a partial close by either side, or the broker trimming it) and
  stays under management. Test `test_a_manual_lot_merged_into_ours_is_not_modified_or_closed`, and the
  book side of the same ticket is `test_a_ticket_merged_with_a_manual_lot_pays_nothing_into_the_sum`.
- `fleet/worker.py::_atr_entry` (an authorship note that was being forged twice): `atr:<account>:<ticket>`
  is what makes a lot carrying someone else's magic still ours. Two paths invented it. A journal locked
  by another shard read as an **absent** note, and the value computed that cycle was then persisted as
  the entry ATR of a lot sized from a different number — every later cycle believed the note it had
  just poisoned. And a lot with no stop priced `abs(price_open − 0)`: gold near 2000 gave an "entry ATR"
  of 2000, and break-even and trailing then moved a stop by a distance nobody had asked for. The
  function now returns `None` while nobody can say, and `_manage_positions` substitutes `0.0` — the
  value `strategy` already spells "no ATR" — so the ATR rules stand down while the broker's own
  stop/target, the giveback from the peak and the time cap keep running. `_mirror_risk_usd` no longer
  calls it at all: pricing other people's risk caps is not deciding who drives a lot, and the old call
  stamped the operator's tickets with our entry ATR and then reported them as park lots nobody was
  managing — the `park_lot_unattributed` alarm was crying wolf on every manual position. A lot with no
  stop stays unpriced (`naked_tickets` in `portfolio_risk_unpriced`) rather than guessed. Tests
  `test_a_locked_journal_does_not_get_a_forged_entry_atr`,
  `test_a_lot_without_a_stop_costs_the_book_a_number_instead_of_a_guess`,
  `test_a_manual_lot_counts_in_risk_but_is_never_touched`.
- `fleet/conductor.py::_journal_recreation` (an empty journal is not an empty day): found while
  investigating an outside hand that deleted the production book mid-session — restored byte-identically
  from a read-only copy (`sha256 dcc3220c…`, 86016 bytes, verified again today) and disclosed. Taking
  the day's baseline from today's equity after such a loss **re-arms the daily stop at the number the
  loss produced**, which is the fail-open a restart is not supposed to have, and the lots opened before
  it are still live at the broker. `Store` now records whether it opened a brand-new file while the
  `.jsonl` trail beside it survived; the conductor then holds entries with one `journal_recreated`
  event until the operator runs `reset-day`, which is an explicit answer to that recreation — the
  release and the per-machine scope of that answer are two tests,
  `test_a_journal_rebuilt_under_the_park_does_not_arm_the_day_stop` and
  `test_a_reset_day_on_one_machine_leaves_the_other_one_measured`.
- `fleet/candidates.py` + `fleet/worker.py` (the bill was in the wrong currency, and unpriced money was
  being summed anyway): `drift_cost` is a bill in **the account's own currency** — the broker's
  `tick_value` is quoted there, not in dollars — and only `drift_cost_usd` is a dollar figure, written
  exactly where a real `rate_to_usd` was resolved. The old code multiplied by `rate or 1.0`, so a ruble
  account's cost was printed as dollars and an account whose rate nobody resolved was priced at
  `1.0`. Proven red on the previous `HEAD`: a `RUB` row with `rate_to_usd = 0.01` came out of the CLI as
  `-12.50 USD` where the honest number is `-0.125`; the same row with no rate became a dollar figure out
  of thin air. Closes now carry `currency`, `rate_to_usd` and `profit_usd`, and a row the journal cannot
  price is **counted, not summed**: `settled_unpriced`, `drift_unpriced`, `guessed` (with `by_source`
  naming how many tickets came from `only_position` or `unknown` rather than the order comment), and the
  CLI says all of it under `оговорки:` — a closed-but-unpriced row prints `итог не в $`, never a number.
  Tests `test_the_drift_bill_is_converted_and_never_relabelled`,
  `test_a_row_the_journal_cannot_price_is_counted_not_summed`,
  `test_a_book_the_journal_cannot_price_says_so_instead_of_summing_it`.
- `fleet/broker.py::_find_by_token` (a token is a short string): positions were matched by the bare
  token as a substring, and `12ab` is inside `fleet-12ab3c45` as surely as inside its own order, so one
  fill could be joined to another lot's ticket and paid out of the wrong position's profit. The match is
  now the whole `fleet-<token>` comment. Tests
  `test_two_tokens_that_share_a_prefix_never_steal_each_others_lot`,
  `test_a_fill_is_named_by_the_comment_token_not_by_an_id_the_api_does_not_return`.
- `fleet/shard.py` (a registry nobody can read is the worst kind of stale): the cycle loop catches its
  own exceptions, so a config that stopped parsing — bad YAML, an invalid field, the file deleted under
  the process — left the shard trading the config it had loaded **before** the file broke: accounts,
  caps and modes the disk no longer names. It now halts cycling entirely on that version, writes one
  `config_rejected` per broken version, and announces `config_reloaded` with what it recovered from.
  Red on the previous `HEAD` — unparseable, invalid, vanished. Tests
  `test_a_registry_the_shard_cannot_read_stops_it_from_trading`,
  `test_a_registry_that_fails_validation_is_a_halt_too`,
  `test_a_vanished_registry_is_a_refusal_not_a_permission`,
  `test_the_shard_trades_again_the_minute_the_registry_is_readable`.
- `fleet/worker.py::_server_key` (the park refused its own account): `attach()` compared server names
  stripped and case-folded, the census key folded case only, and the gate immediately before the send
  compared the **raw** strings. A registry typing `MetaQuotes-Demo ` therefore passed attach and then
  got `identity_lost` on every symbol, releasing its reservation each time — the account was attachable
  and untradeable. Every identity comparison in the file now goes through one rule. Proven red on the
  previous `HEAD`; the guard half is kept deliberately: a server that only *looks* similar
  (`S` against `S-MT5Real26`) is still somebody else's money and the park still refuses it. Tests
  `test_the_gate_before_the_send_reads_a_hand_typed_server_name_as_attach_does`,
  `test_a_server_that_only_looks_similar_is_still_someone_else`.
- `fleet/broker.py::stops_refusal` + `StopsRefused` (the server owns the geometry, and reads its own
  contract before the money is in flight): `trade_stops_level` refuses a stop or target closer to the
  live price than its minimum (retcode 10016), and `trade_freeze_level` locks a standing position while
  the price is inside it of its stop or target. Learning either by sending means learning it while the
  order is already moving, and for the break-even modify it means a rejected request that reads like a
  broker outage. `market_order` now asks the spec first and raises `StopsRefused` — the entry path
  releases the reservation, journals `entry_stops_refused` (not `entry_failed`, which would look like
  the broker broke) and moves on; the break-even path reports `break_even_refused` once per change of
  the refusal and leaves the protection where the broker has it, while the broker's own stop, the
  giveback and the time cap keep running. Nothing is clamped, on purpose: a lot is sized for the stop
  distance the strategy computed, so pushing the stop to the server's minimum overspends the
  reservation the portfolio book made, and moving a target rewrites the geometry the candidate journal
  bills. Proven red on the previous `HEAD` (four node IDs — including that the old code sent the order
  and asked afterwards). Tests `test_a_geometry_the_broker_bans_is_named_before_anything_is_sent`,
  `test_a_lot_inside_the_freeze_band_is_locked_and_says_so`,
  `test_an_order_the_server_would_reject_is_never_sent`,
  `test_a_break_even_the_server_would_reject_is_said_once_and_never_sent`,
  `test_a_stop_the_broker_will_not_take_is_a_hold_and_the_money_goes_back`.
- `scripts/real_bridge.py` + `mql5/DSHRealBridge.mq5` (an order that outlived the permission that
  wrote it): the bridge wrote the command file and washed its hands. A stand put back between that
  write and the advisor's next timer left a live order in `MQL5\Common\Files`, and a command that
  named no account was addressed to every terminal attached to that folder — the one that woke up
  first bought the real money. Each command now carries the login it is meant for and an expiry
  (`CMD_TTL_SEC = 60`), is written compact because the advisor's `JsonStr` looks for `"action":"` and
  reads a spaced `"action": ` as an empty field, and is taken back the moment the gate shuts — with
  `НЕ СНЯЛ` in the log when Windows will not release the file the terminal still holds. The advisor
  believes none of this and checks for itself: `AccountInfoInteger(ACCOUNT_LOGIN)` against the
  command's login, `exp` against `TimeGMT()` and `MaxCommandTtlSec`, `MaxLots` before an open, and
  `POSITION_MAGIC` before a close. Tests `test_send_command_writes_no_file_on_a_refusal`,
  `test_a_command_without_an_account_in_it_is_not_written_at_all`,
  `test_a_command_written_while_the_gate_was_open_is_taken_back_when_it_shuts`,
  `test_withdraw_says_out_loud_when_a_command_cannot_be_taken_back`,
  `test_the_real_bridge_reads_the_queue_python_writes`,
  `test_the_real_bridge_refuses_a_command_it_cannot_verify`,
  `test_the_real_bridge_will_not_execute_a_command_that_outlived_its_permission`,
  `test_the_real_bridge_caps_the_lot_of_an_ordered_open`,
  `test_the_real_bridge_closes_only_the_lots_it_stamped`.
- `mql5/DSHBridge.mq5` (the demo window ate the real order): the demo bridge defaulted to
  `dsh_real_cmd.json`, so a play-money script deleted the real bridge's command and opened it on the
  demo account — a filled real order that the real bridge never learned about. The demo queue is its
  own pair of files now, and both advisors take their magic from one `InpMagic` input that the parity
  test pins to `real_bridge.REAL_MAGIC`. Tests
  `test_the_demo_bridge_does_not_share_the_real_bridge_queue`,
  `test_the_real_bridge_stamps_its_own_magic_on_every_request`,
  `test_the_magic_the_ea_stamps_is_the_magic_python_reads`.
- `fleet/livegate.py::stand_refusal` + `scripts/desk_watch.py` (a fourth door, and a stand that only
  two of them honoured): the chain is one function now — stand, then `--live` in this invocation, then
  a numeric login, then a registry row that says `mode: real` and `enabled: true` for the server the
  terminal actually answered — and the desk watcher goes through it like every other process. A
  watcher told which account to manage refuses a terminal that answers with a different server under
  the same login, and a real row's `max_lots` is applied before the file's own default could multiply
  it. Tests `test_a_real_account_needs_the_same_three_words_as_every_other_door`,
  `test_the_stand_stops_the_desk_watcher_even_on_play_money`,
  `test_a_terminal_that_did_not_name_its_login_is_managed_by_nobody`,
  `test_another_server_under_the_same_login_is_a_different_money`,
  `test_a_real_account_carries_the_ceiling_of_its_row_not_the_default_of_this_file`,
  `test_the_desk_watcher_writes_no_order_the_gate_refuses`.
- `fleet/worker.py` (gates that lived in the caller, and notes that outlived their proof): the stand
  is checked by `_try_entries` itself, so a Worker built by hand no longer buys on a parked park; it
  announces `entries_paused` / `entries_resumed` once per change of state and nothing else may pass.
  A cooldown the journal refused to stamp is now a fact (`entry_mark_failed`) instead of a silent
  second bite at the same symbol. An identity check that *raised* — a terminal object missing a
  field, a broker call that throws instead of answering — used to unwind between the reservation and
  the order, leaving the portfolio's money slot held for its full 600 s; it now releases, journals
  `identity_check_failed` and returns. A `stopsrefused:` note is cleared by the fill that disproves
  it, so the next time the same rule bites it is said again. Tests
  `test_a_parked_stand_stops_a_worker_nobody_asked`,
  `test_a_cooldown_that_cannot_be_written_is_said_out_loud`,
  `test_an_identity_gate_that_cannot_answer_frees_its_reservation`,
  `test_a_filled_lot_forgets_the_refusal_it_just_answered`.
- `fleet/shard.py` + `fleet/config.py` (a stamp that misses an edit, and a registry caught
  half-saved): the watched version is the sha256 of the file's bytes, not `st_mtime` — two saves
  inside one clock tick left the second invisible, and the shard went on trading a registry the disk
  no longer held. A shard whose registry cannot be read *at start* writes `config_rejected`, prints
  why, and returns 2 instead of dying in a window nobody watches; an empty `--pause-file` means the
  machine's stand, not the absence of one. On the parsing side a `yaml.YAMLError` is converted to
  `ConfigError`, which every caller already treats as "refuse to trade". Tests
  `test_a_registry_rewritten_inside_the_same_second_still_reloads`,
  `test_a_shard_that_cannot_read_its_registry_never_trades_and_says_why`,
  `test_an_empty_pause_file_means_the_machines_stand`,
  `test_a_registry_caught_half_saved_refuses_instead_of_crashing`.
- `scripts/real_mirror.py` (two doors in the loop): a terminal that does not answer is retried, and
  under `--once` the run exits 1 having sent nothing; a gate that answered a second ago and a registry
  that now returns no row is a stop, not a `NoneType` swallowed as "cycle error". Tests
  `test_the_mirror_retries_a_terminal_that_is_not_there`,
  `test_the_mirror_stops_when_the_registry_stops_answering`.
- Tests: `tests/test_fleet_regressions.py` is now 90 tests, `tests/test_fleet_multi_terminal.py` 73,
  and the real-money doors have three files of their own: `tests/test_real_money_gates.py` 39,
  `tests/test_bridge_ea_gates.py` 13 and `tests/test_desk_watch_gates.py` 9 (both new this round).
  Whole suite on this checkout, re-measured after the last edit on 2026-11-04: **1445 passed, 10
  skipped in 137,05 s**; the five fleet files are 218 and the three gate files 61. The production
  journal is untouched by the suite (`FLEET_STRICT_GUARD=1` compares hashes before and after) and
  read, that same day, as `trades 0`, `positions 0`, `book 0`, `risk_slots 0`, four events —
  `3 cycle + 1 entry(dry)`, zero `opened`, zero `candidate` — at 86016 bytes, `sha256 dcc3220c…`,
  the same hash the restored copy was verified against. The next section carries the reading of
  2026-11-05, which is different because the park itself wrote to it on 2026-11-04 evening.

### What the last four critics found: three of the four doors asked, the fourth one did not

Everything below was reproduced against `HEAD` (1986f8f) before being changed, and the whole set was
then re-measured on 2026-11-05 by exporting that commit with `git archive` and handing it **only**
this round's test files (`agent_data/red_thisround`). That tree is red exactly where the fixes are:

| file | against `HEAD` | green here |
| --- | --- | --- |
| `tests/test_fleet.py` | 1 failed, 11 passed | 12 |
| `tests/test_fleet_ops.py` | 5 failed, 24 passed | 29 |
| `tests/test_fleet_regressions.py` | 12 failed, 88 passed | 100 |
| `tests/test_fleet_multi_terminal.py` | 2 failed, 73 passed | 75 |
| `tests/test_real_money_gates.py` | 32 failed, 23 passed, **1 does not terminate** | 56 |
| `tests/test_bridge_ea_gates.py` | 12 failed, 4 passed | 16 |
| `tests/test_desk_watch_gates.py` | **11 failed, 0 passed** | 11 |

75 red and one that never returns, 223 green out of the same 299. Here: **299 passed** across those
seven files, and the whole suite **1482 passed, 10 skipped in 123,90 s**.

- `scripts/real_mirror.py` (the finding that hides as a hang, not a failure):
  `test_the_mirror_retries_a_terminal_that_is_not_there` does not assert-fail against the old code —
  it never ends. The `faulthandler` stack names it exactly: `scripts/real_mirror.py:163` inside
  `main()`, retrying a terminal that was never there, forever. A mirror that cannot reach its
  terminal does not stop, it just stops reporting. It now ends, and `--once` exits 1 having sent
  nothing.
- `scripts/desk_watch.py` (the fourth door, and it asked nobody): all 11 tests of
  `tests/test_desk_watch_gates.py` are red at `HEAD` — the watcher read the book and sent orders
  without the registry row, without `--live` in this invocation and without the stand being read.
  It is gated like the other three, and the door is split the way the money needs: a refusal shuts
  **openings**, while its own lots keep closing. `test_a_desk_run_nobody_permitted_opens_nothing_but_still_reads_its_own_lots`
  replaced the earlier "does not even read the book" test, because a watcher that refuses to look at
  its own open position is a watcher that leaves money under the broker's stop only.
- `scripts/real_bridge.py` + `fleet/livegate.py` (a stand that costs money to put up): the demo book
  is now read **before** the idle return, so a real copy whose demo twin already went flat still gets
  closed — the block sat under the idle return, so arming the stand left a lot riding on real money
  with nothing behind it but the broker's own stop. `withdraw()` takes back an armed *open* and
  refuses to take back a *close* ("не снимает защиту"). Tests
  `test_a_parked_bridge_still_closes_the_copy_whose_demo_lot_is_flat`,
  `test_a_parked_bridge_takes_back_its_open_and_leaves_its_close`,
  `test_the_stand_stops_openings_and_leaves_the_closing_door_alone`.
- `fleet/livegate.py` (an answer that names no server is not a wildcard): `exit_refusal`,
  `identity_refusal` and `permitted_account` now refuse a request that does not say which server it
  means. The row that authorises `Exness-MT5Real26` was, for the closing door, authorising any
  terminal that keeps quiet about where it is. Covered inside both files above.
- `scripts/real_bridge.py` (a test that could reach the operator's queue): `run_cycle()` took the
  production `EA_STATE` / `EA_CMD` paths as default arguments, so `withdraw()` called from a scratch
  run deletes `…\Common\Files\dsh_real_cmd.json` — the live bridge's actual queue. The paths are now
  named at the call site in `main()` and patched in every test; `test_a_bridge_brought_back_without_the_word_starts_and_sends_nothing`
  drives the daemon far enough to prove it starts, refuses, and sleeps.
- `fleet/worker.py` (the last look is one statement earlier): the stand was read at the top of the
  cycle, and a cycle is long — between the read and `broker.market_order` the operator can put the
  machine's stand up. `_try_entries` now re-reads it immediately before the send, frees the reserved
  money slot and journals `entries_paused`. Tests
  `test_the_stand_is_read_again_one_statement_before_the_order` (the stand is armed *inside*
  `reserve`, so the send is reached with a fresh fact and no order) and
  `test_a_cycle_started_under_an_armed_stand_never_reaches_the_send` (the symbol probe is patched to
  fail, so a cycle that touched the market at all is caught).
- `fleet/store.py` + `fleet/worker.py` (`lot_witness`: how much of a ticket is ours, durably): a
  netting account merges our 0.01 into somebody's 0.05, and the park used to decide whose share that
  was by looking at the merged volume each cycle — so a park that saw the merge late treated the whole
  ticket as its own and closed a manual lot it never opened. The share is now written at the fill
  (`add_lot_witness`), floored on observation (`note_lot_witness` uses `MIN`), dies with the ticket
  and ages by the cycle that last saw it. Tests
  `test_the_witness_is_written_at_the_fill_and_never_invented_by_looking`,
  `test_a_second_lot_ordered_into_the_same_ticket_grows_our_share_of_it`,
  `test_a_closed_ticket_takes_its_witness_and_its_peak_with_it`,
  `test_the_prune_ages_a_witness_by_the_cycle_that_last_saw_it`,
  `test_a_window_answering_for_another_account_closes_nothing`.
- `fleet/conductor.py::_portfolio_stop` (a sum that leaves an account out is not this portfolio's
  size): an account whose broker currency nobody can price in dollars contributes nothing to `equity`
  while its losses keep happening, and the daily stop — one percentage of one sum — was measuring
  that partial number and printing it as the portfolio. A non-zero `unpriced` now returns
  «дневный стоп портфеля не измерим» *before* the percentage is computed, both on a calm book and on
  one already down 30 %, because a calm day next to the invisible account is the case that matters.
  Test `test_an_unpriced_account_makes_the_portfolio_stop_unmeasurable`.
- `fleet/config.py` (an identity is a pair): a registry row with a blank, empty or whitespace-only
  `server:` is refused at load — «сервер не назван» — because every identity check downstream
  compares normalised strings, and a silent server matches a silent terminal.
  Test `test_an_account_naming_no_server_is_an_error`.
- `scripts/fleet_cli.py` (two commands that were not what they said): `reset-day` unlinked the
  operator's kill switch as a side effect, so an operator who armed the stand after a losing day and
  then accepted the reset had his money trading again without ever saying so — it now clears the
  baselines, journals `reset_day_paused` with the stand's own text and says «стоянка на месте,
  снимай её сам». And `terminals`, the window census, opened the journal and rewrote
  `terminal_roster@<host>` — the evidence the shards decide entries on — from a command that only
  meant to look; it now walks the process list and never constructs a `Store`. Tests
  `test_a_reset_day_leaves_the_armed_stand_armed`, `test_the_window_census_never_opens_the_journal`.
- `tests/test_mt5_churn_history.py` (a cleanup that broke someone else's suite): `del
  sys.modules["MetaTrader5"]` deleted the **installed** broker module for every test that ran after
  that file, so a later fake that borrowed `TRADE_ACTION_DEAL` and the rest borrowed nothing, and the
  failure surfaced in an unrelated test. The helper now puts back exactly what was there.
- Production journal, read on 2026-11-05 (`file:…?mode=ro`, nothing written): 86 016 bytes,
  `sha256 b1ac9153…`, `trades 0`, `positions 0`, `risk_slots 0`, `book 1`, 37 events — 28 `cycle`,
  one `entry(dry)`, and `started` / `running` / `spawned` / `new_day` / `baseline_rebased` /
  `book_incomplete` + `book_complete` / `account_second_window`. Realized income over this whole
  program is **0,00 USD**: the park has run cycles and refused, it has not been permitted to open.
  `lot_witness` is not in that file yet — the table is created by the schema's `CREATE TABLE IF NOT
  EXISTS` on the first open by this build, so it appears when the park restarts, not before.
- **That reading is only true of the file pair, and the doc now says so.** The 37 events are not in the
  86 016-byte base: the same database opened `mode=ro` (SQLite folds the journal in) gives 37 events /
  26 `equity` rows / `book 1`, and opened `immutable=1` (base only, and it answers `journal_mode:
  delete`) gives **4 events / 1 equity row / no `book` row**. `fleet.sqlite-wal` is 1 009 432 bytes and
  has never been checkpointed — its mtime is the park's last write, `14:58:32Z`. So "copy the journal"
  means copying both files, and `docs/FLEET.md` carries the two readings side by side instead of one
  number, because the number a reader would get from a single-file backup is the 2026-11-03 snapshot.
- The incident that snapshot is from, disclosed here rather than left in a scratch note: on 2026-11-04
  a red-proof helper in `agent_data/` copied the production journal, and its own cleanup called
  `p.unlink()` on the **source**, so `C:\mt5-fleet\fleet.sqlite` was deleted. It was restored from the
  only surviving copy, `agent_data/live_copy.sqlite` (snapshot 09:40 UTC that day, 86 016 bytes,
  `sha256 dcc3220c…`), byte for byte, hash verified. What cannot be recovered is anything the journal
  accepted between that snapshot and the deletion. What *can* be measured is the other half:
  `Store.event` writes every row to SQLite and to the `.jsonl` trail beside it, and on the 2026-11-05
  reading the two agree exactly — 37 event lines in the trail, 37 in the book, **0 rows in the trail
  missing from the book** — so nothing in the trail's coverage (from `2026-11-03T12:54:02Z`) was lost in
  the gap. Rows the trail never received are the unfalsifiable part, which is why `event()` returns
  whether the row landed and `_landed` only keeps an "already said" note when it did.
- Live machine, census re-run 2026-11-05 read-only: **two** operator windows answer login 114929502, not
  one — `C:\mt5-exness` (pid 8836) and `C:\mt5-team` (pid 8196), both `MetaQuotes-Demo`, balance
  99 206,51 / equity 99 211,93. `docs/FLEET.md` said "close `c:\mt5-team`" and is corrected to "leave
  exactly one window for this login", which today means closing both of the operator's; the park raises
  its own from `C:\mt5-fleet\terminals\114929502-metaquotes-demo` and shuts it in `finally`. The census
  took **0,76 s** against the 8,63 s measured on 2026-11-04, because the two blind windows that each
  paid the full 4-second `ROSTER_PROBE_TIMEOUT_MS` are not running. And the no-write claim was checked
  the way WAL mode actually requires: base `sha256`, WAL byte count **and** WAL mtime, event count,
  `meta` row count and the `terminal_roster@admin` value hash all identical before and after the command
  — a hash of the base file alone would not have proven it, since every write would have landed in the
  `-wal`.

### The day a stop was armed on outranks the clock that lost it (2026-11-05, on the live park)

Reproduced on the running account, not in a fixture: the same terminal answered a **day backwards** —
`2026-11-05`, then `2026-11-03`, then `2026-11-05` again on three consecutive cycles 35-70 s apart,
seven regressions in twelve minutes (journal lines `02:20:05`-`02:32:11Z`).

- `fleet/broker.py::server_timestamp` (the day came from history, not from the quote): it returned the
  **first** symbol that answered, and MT5 caches ticks and bars per symbol — a symbol nobody
  subscribed keeps the daily bar of its last sync, so the answer depended on which of four names the
  loop reached first. It now takes the **newest tick** among `CLOCK_SYMBOLS` and reads the daily-bar
  cache only when no tick answers at all, which is what a weekend looks like. Four tick reads cost
  milliseconds. This is the source improvement; it did **not** stop the flapping on its own — the
  live park regressed seven more times after it — which is why the account-level hold is the fix.
- `fleet/worker.py::_held_day` (the same hazard one level below the conductor's clamp): `DayState`
  compares days as strings, so "not the stored day" means tomorrow and yesterday alike — a backward
  day rebased the daily-stop baseline onto equity that had already lost and set `stopped` back to
  `False`, which is an armed stop silently disarmed on the same losing day. An account whose clock
  answers younger now keeps the day it is already measured on; only a forward step moves it. Proved by
  three tests in `tests/test_fleet_regressions.py`: on `a8ce7a3` exactly those three are red (`3
  failed`) and the first prints the defect as a number — the base moves to `('2026-11-03', 985.00)`
  instead of holding `('2026-11-05', 1000.00)`. On the live park the hold ran through every real
  regression: `daystate` stayed `('demo-metaquotes', '2026-11-05', 99330.64, 0.0, 0)` and the `book`
  row kept `2026-11-05`.
- `fleet/worker.py` journal noise (`DAY_CLOCK_REANNOUNCE_SECONDS = 6 * 3600`, per direction): untimed,
  the flapping clock wrote twelve lines in nine and a half minutes — one every 48 s, about
  seventy-five an hour, into the single book the operator reads. Each direction keeps its own clock
  because a shared one throttled the recovery away: the line "the clock caught up" arrives half a
  minute after "the day is held" and, on one window, was never written at all. Both markers store the
  epoch with milliseconds — the first version rounded it to whole seconds (`f"{now:.0f}"`), so a
  marker written at `t.4` read as `t+1` and the next cycle, 0,3 s later, computed a **negative**
  elapsed time and answered "not yet". Disclosed plainly: that is how the fix commit shipped —
  `bfc7e8b` ran green once (1485 passed) and red once (1484 passed, 1 failed on
  `test_a_stale_quote_does_not_rewind_the_day_a_stop_was_armed_on`), because the assertion depended on
  which side of a whole second the test happened to land on. A wall-clock-dependent test is a defect
  twice over, which is why this round re-ran the whole suite instead of the touched files.
- `tests/conftest.py` (the battle-folder guard assumed the park was a visitor): the suite could not
  report a result at all, because the guard raised `живые fleet.shard процессы после suite:
  4916/3088` on the **live park**. A shard now counts against the suite only when it does not drive
  `C:\mt5-fleet\fleet.sqlite` and was not running when the session started; while the park is live,
  its own writes make `mtime` worthless as evidence, so a content change is printed instead of
  failing the run. Losing `fleet.sqlite` or `fleet.jsonl` stays fatal whatever else is running —
  that is the 2026-11-04 incident, and no live state excuses it.
- Measured on this machine: whole suite **1485 passed, 10 skipped in 127,19 s** with
  `FLEET_STRICT_GUARD=1` and the park running (guard line: `живой парк ведёт журнал сам: […] — mtime
  не свидетельство`); `tests/test_fleet*.py` 236 in 22,69 s, the three gate files 83 in 7,58 s. Money
  read-only at 02:51Z: `trades 0`, park realized **0,00 USD**, one `book` row `99 212,61`, day base
  `99 330,64`, the account's own day at `−118,00 USD` and all of it the operator's manual trading
  (the same twenty minutes earlier said `−124,13`, because his closes were still settling);
  `mode=ro` sees 170 events where `immutable=1` sees 157.

### The operator's word about new money, and what it never authorized standing down (2026-11-05)

The critic read three rules as findings because nothing in the suite said them out loud; writing the
tests then found one real defect and one stale paragraph. Nine tests over two code commits
(`a5f86d4`, `9d499cb`), and every number below re-read rather than carried over.

- `scripts/real_mirror.py` (a refusal sentence that named the wrong thing): `lots <= 0` was one
  branch for two different facts — "the registry caps this row at zero lots" and "this window
  re-logged to a login the registry does not carry at all". The sentence printed the first excuse with
  the second account's name, sending the operator to edit a registry row that was never the problem.
  `permitted_account is None` is now named as itself. The three opening rules it already obeyed are
  pinned: one buy per symbol per cycle (dedup records **fills**, not attempts), no copy of a demo lot
  that carries neither a stop nor a target, and the lot ceiling taken from the login the terminal
  names at the moment of the open — `--real-lots 0.05` against a 0.01 row and a 0.02 row, re-logged
  mid-cycle, opens 0.01.
- `fleet/worker.py` (the journal said one word for two facts): `not sending` is "paper cycle, no broker
  behind it" and "real account the operator has not spoken for" at once. They already counted apart
  (`return 1 if self.dry_run else 0`, so a refused real entry never lands in `opened`), but both wrote
  `entry(gated)`, and `docs/FLEET.md` had been describing `entry(dry)`/`entry(gated)` as two names all
  along — the rename in `8aa8db1` collapsed them. The paper cycle says `entry(dry)` again.
- `docs/FLEET.md`, «Четыре двери» (prose two days behind the code): it still claimed the park was the
  one door that diverged — "a shard without `--live` does not touch a real account in either
  direction". `8aa8db1` really did freeze the closer with the same word that locked the entry door,
  `4027e77` removed that, and the paragraph survived it. The park now reads as what it does: exits are
  watched by `dry_run` alone, a real lot the park placed is moved to break-even and closed without
  `--live`, and the stand and the missing word stop openings only.
- `tests/test_fleet_ops.py` (a test whose name claimed the gate and proved ownership): the position
  arrived with `magic=0` against the account's default `7000000`, so `risk.own_in` set it aside before
  any gate was reached and the assertions held with the gate present or absent. Renamed to
  `test_a_lot_the_park_did_not_place_is_neither_moved_nor_closed`, with the docstring pointing at the
  two tests that do check the closing path.
- `scripts/real_bridge.py` (two legs the code already kept, now said): a day halted for opening is not
  a ban on exits — a lot that outlived its own daily stop must keep meeting a bridge that tries to
  free it; and between two command files the bridge sleeps, because the advisor polls the folder once a
  second and two writes in the same millisecond are one write. The red copy prints the defect itself:
  two `close` commands in the same second with the same `exp`.
- Red proof, per file, on copies with the rules stripped (`agent_data/` scratch, never the production
  journal): worker copy without the exit gate and without the `return 1` — **2 failed**
  (`test_a_real_lot_is_still_cared_for_when_the_live_word_is_missing`,
  `test_a_gated_entry_is_not_counted_as_a_lot_opened`); the shared word alone needed its own copy,
  because in the first one the count assertion fails before the journal is read — restoring only
  `entry(gated)` for both halves gives **1 failed** on `count_events("entry(dry)")`. The third worker
  test stays green by construction: it pins the opposite half — paper is the one mode that may stand a
  close down. Bridge copy without the halt-retry and the sleep — **2 failed**. Mirror copy without the
  three rules — **4 failed**.
- Measured after the work, whole suite **1494 passed, 10 skipped in 124,83 s** (park live,
  `FLEET_STRICT_GUARD=1`); five `tests/test_fleet*.py` **236 passed in 23,69 s**; three gate files
  **92 passed in 7,39 s** — `test_real_money_gates.py` went 56 → 65. Money read-only at 03:41Z:
  `trades 0`, `positions 0`, `risk_slots 0`, park realized **0,00 USD**, one `book` row `99 212,61`
  stamped 03:41:17Z, day base `99 330,64`, `dayflows` `0.00|-118.00|99212.61|1793850005` — the
  `−118,00 USD` is the operator's own day and had not moved since 02:51Z. Journal: 248 events / 202
  equity rows through `mode=ro` against 157 / 117 through `immutable=1`, with `-wal` at 2 488 512
  bytes over a 147 456-byte base (same hash `21dc44e9…` as an hour earlier — the base has not moved
  since the checkpoint; the WAL grew 25 KB between two reads of this one measurement). The day-clock
  markers are the same two numbers as an hour ago (`1793846837,701` held / `1793846873,625` live) with
  86 cycles since the last clock line. `C:\mt5-fleet\PAUSE` absent, memory 839,0 МБ free of 3974,0
  (78 %), and the newest roster (03:38:11Z) still names both operator windows — so entries stay
  blocked, which is the gate working, not the park broken.

## Unreleased — the status line that reported a live chain over a dead transport

- `agent_data/live_gold_ema_policy.yaml` (a claim this repo's own evidence refutes): its header said the gold EMA 50/200 variant was "the only family that was positive in the two-year replay over real broker bars". Both study files say the opposite — `docs/evidence/gold_study_development.txt` gives `ema n=618 win 31.9% meanR -0.368 CI [-0.525,-0.205]` on PAXG and `docs/evidence/gold_study_development_xauusd.txt` gives `ema n=124 win 41.9% meanR -0.261 [-0.635,+0.139]` on the broker's own XAUUSD, and **every rule row in both tables is negative at the point estimate**. So the live forward run has been testing a hypothesis the replay already refuted, and its header was the reason. The comment now carries both tables, the −31,13 realized on 2026-11-02, and the fact that all three closes came from outside the chain (magic 0, empty comment) so they measure nothing. No key was changed: the file still runs EMA 50/200 with `max_open_positions: 1`, and a running process would not see a policy edit anyway — `load_policy()` is called once, at `scripts/run_mt5_demo_trading.py:1384`, not per cycle.
- `scripts/mt5_session_scan.py` (which clock its windows are in): it converted `rate["time"]` with `datetime.fromtimestamp(..., tz=timezone.utc)` and printed the result as a UTC hour, but MT5 stamps bars on the **server** clock. Measured on the live terminal at 07:52:48 UTC: the newest EURUSD tick carried `time` 09:52:46, so +7200 s, and the window the evidence file `docs/evidence/mt5_session_scan.txt` called `USDCHF 00:00-01:00` is **22:00–23:00 UTC**. `server_offset_seconds()` now measures the offset from the terminal's own tick each run (rounded to whole hours, because the sub-hour remainder is the tick's age, not a clock difference) and the scan re-bases every bar through it; the header prints `hours are true UTC; this server was 2 h ahead of UTC at the read`. Re-run 2026-11-02 07:55–08:0x UTC into `docs/evidence/mt5_session_scan_utc.txt`, and the correction changes the *shape* of the finding, not just its label: the leaderboard is no longer fifteen unrelated windows but **one hour — 22:00–23:00 UTC — on seven USD-quoted pairs at once** (USDCHF +0.0303% t=11.30, AUDUSD +0.0256 t=8.61, GBPUSD +0.0152 t=7.59, USDCAD +0.0109 t=6.11, EURUSD +0.0115 t=5.07, USDJPY +0.0153 t=4.57), with USDCHF's 20:00–21:00 mirror at t=−13.52. |t|>3 fell from the old file's "57 of 1925" to **74 of 1912**, because re-basing merged two mislabelled hour grids into one.
- Out-of-sample work on that hour, run 2026-11-02 07:4x–07:5x UTC against 36280 real H1 bars from 2021-01-01 with the live spread subtracted once per day (`agent_data/session_oss_check.py`, read-only): USDCHF 22:00→23:00 UTC **n=1513 days, mean +0.0256 %/day, t=+14.37**, first half t=+10.13 (+0.0208), second half t=+10.48 (+0.0304), and positive in **every single year** — 2021 +9.06, 2022 +5.07, 2023 +4.14, 2024 +6.22, 2025 +8.05, 2026-to-date +4.23 (n=216). AUDUSD t=+12.68, GBPUSD +9.43, EURUSD +7.64, USDCAD +7.22 on the same hour. The scan's own caveat was honoured rather than talked around: this is a candidate because it survives a search over ~1900 hypotheses *and* splits cleanly, and the spread costs 0.24 of the 2.56 points of mean, which is why metals (1.5 bps) and crypto (10–26 bps/side, `docs/evidence/venue_spreads.txt:23-26`) cannot carry anything this thin.
- **And the reason the chain cannot trade it yet**: `agent_data/window_tpsl_grid.py` asks whether the only thing the current command grammar can do — an entry whose SL/TP the broker resolves — captures that hour, walking the real bar paths (a bar touching both levels counts the stop first) over TP {5,10,15,20,30,50} × SL {10,20,30,50,100} × hold {1,2,3} h on EURUSD, GBPUSD, AUDUSD, USDCAD. **Every one of the 90 shapes on all four symbols is negative**: the best cell in the whole grid is EURUSD hold 3 h TP 50 SL 100 at −1,7 points/day, t=−1,06; AUDUSD's tightest TP won 85,5 % of days and still lost 9,8 points/day. The close-to-close mean is real, but the hour's own noise is ~140 points against a 25-point edge, so asking the exit to be *touched* rather than *timed* is what destroys it. The gap is therefore not a parameter and not a risk setting: `scripts/mt5_demo_bridge.py:132` accepts `direction in ("BUY", "SELL")` only, `match_entry_deal()` accepts `entry == 0` (DEAL_ENTRY_IN) only, and nothing in the runner reconciles a close to the command that owned it — which is the same missing piece that made the −31,13 unattributable this morning. A `CLOSE` command (and `DEAL_ENTRY_OUT` reconciliation) is the next build; no parameter change to the four existing families can substitute for it, and that is now measured rather than asserted.
- `scripts/check_ea_transport.py` and `scripts/system_status.ps1` (a BROKEN verdict on a chain that had just filled an order): at 06:39 UTC the live chain read `ea_silent (BROKEN)`, 63 minutes after the advisor stamped `Order accepted` for deal `10938325361`, because an advisor with an empty queue prints *nothing*, and the bridge answering `:8000` is the runner build started at 05:43:12 — older than `GET /v1/health`, so it answers 404 and the poll counter that task #5 exists to read was unavailable. Silence plus a mute witness is not evidence of a dead pipe, so that combination now yields the new state **`unwitnessed_silence`** with `stale_from: order_accepted` and exit **2** ("no evidence to reason from"), while an empty port (`unreachable`) still reads `ea_silent`/1 because nothing listening *is* a proof. `system_status.ps1` prints its consequence only on exit 1 (`-eq 1`) and, for exit 2, `-> the check could not decide: read the meaning line above, and do not treat this as a dead transport.` Covered by `test_a_silence_after_a_proven_send_is_not_reported_as_a_dead_pipe` (three mute codes give `unwitnessed_silence`, `unreachable` does not) and `test_a_silence_without_a_witness_exits_two_not_one`, which serves a real 404 from an ephemeral loopback server instead of a fixture; `test_a_silence_is_never_overruled_by_a_bridge_that_cannot_answer` was rewritten to use the `error=4014` log, which is the case where a mute bridge genuinely excuses nothing.
- `scripts/system_status.ps1` (how many runners there actually are): the line printed `runner : 2 process(es)` while `Win32_Process` held pid 1904 and pid 7612 with identical command lines. They are one run: `.venv\Scripts\python.exe` is a stub that re-spawns the same argv in the base interpreter, so a launcher through the venv always looks like two — and a *real* double launch would have looked like four and read as normal. The status line now drops any runner whose parent is another runner, names the count as loops, and prints the pid that owns the port, so the two facts can be tied together: measured at 06:41 UTC on the live machine → `bridge :8000 : listening (pid 7612)` and `runner : 1 loop (pid 7612)`.
- Re-measured 06:41–07:52 UTC, the same account, and the numbers moved only as far as the one open position allows: balance **99 968,87** (= 100 000 − 31,13 realized, the three magic-0 closes at −1,56 / −24,90 / −4,67), equity 99 964,91 → 99 964,01 → floating −4,86 at 1.11876 against a SELL entered at 1,11823, `positions_total: 1`, journal `agent_data/demo_live_20261102.db` at seq 1414 and ~41 rows/30 s. Every cycle in the window is `status: BLOCKED` with `POSITION_CAP_REACHED` — for GBPUSD and EURUSD alike — so the signal is firing and the single slot is the gate; the day's `demo_order_command/served/outcome` count is still exactly 1 each. That position (`ticket 11189096387`, magic `8172026`, comment `CC:cc-1bac6713ce6d749fe`, SL 1,12042 / TP 1,11385) is the fill at 05:43:15 UTC, and its SL/TP reaching the broker is the first proof this chain has ever had that the EA's own geometry lands on the order. Meanwhile the transport is alive without the log saying so: `Get-NetTCPConnection -LocalPort 8000` over one 60 s window showed **40 new client ports arrive and 42 age out** (~40 polls/min against a 240 s TIME_WAIT table), and `run_mt5_demo_trading.py` makes no HTTP requests of its own — it hosts the queue in-process — so those connections are the advisor polling an empty queue. The doc line "the broker queue has not been exercised since 05:43" is therefore wrong in its second half: nothing has been *queued*, and it has been *polled* about forty times a minute.
- `scripts/system_status.ps1` (which log it reads): the EA log path was the literal `MQL5\logs\20261101.log`. On 2026-11-02 the operator therefore got **yesterday's** three tail lines — which were clean — while today's polls were all refusals, and the script's own header promised "EA log". It now takes the newest non-empty `*.log` by modification time and hands it to the checker below, and adds the consequence instead of leaving it to be inferred: `-> a command queued now expires at the bridge without reaching the broker.`
- `scripts/system_start.ps1` (which journal it opens): the launch path was the literal `agent_data\demo_live_20261101.db`, so every day after 2026-11-01 appended to the previous day's audit trail and re-read that day's risk baseline and served ids as if they were today's — the `run_id` itself derives from the state path, so the frozen name also froze the identity that `client_order_id` is built from. The path now takes the UTC day measured at launch (`demo_live_{yyyyMMdd}.db`, verified to produce `demo_live_20261102.db`), and because the launcher ends by calling `system_status.ps1`, the transport verdict prints on every start. The path expression itself is what was checked (evaluated on 2026-11-02 it yields `agent_data\demo_live_20261102.db`); the launcher was not run, because starting it would put a second bridge on `127.0.0.1:8000` over the runner already there.
- `scripts/check_ea_transport.py` (new): the runner and the bridge can both be perfectly alive while nothing reaches the broker, because the only thing that moves a queued command is the advisor's `WebRequest`, and a terminal restarted with `/config` loses the loopback allow list silently. This reads that log and names the state in one word — `request_not_allowed` (`error=4014`), `token_rejected` (`HTTP 401`), `token_missing`, `bridge_response_invalid`, `bridge_unreachable` (`error=5203`), `kill_switch_armed`, `chart_input_invalid`, `not_demo`, `command_expired`, `broker_gate_refused`, `malformed_command`, `tombstone_unavailable`, `duplicate_refused`, `order_accepted`, `ea_silent`, `never_polled`, `no_log`. Healthy is only `polling`/`idle`/`order_accepted` plus the two states where the transport demonstrably worked and something else refused (the armed stop, the duplicate guard) — those are the operator's own acts, not a broken pipe. A line older than `--stale-seconds` (default 120) becomes `ea_silent` **and keeps `stale_from`**, because "the advisor stopped trying" and "the advisor is trying and being refused" need different fixes. Exit 0 healthy, 1 broken transport, 2 no log to reason from. It reads one file and sends nothing.
- Every rule was written against the strings the advisor actually prints, and a test enforces that both sides stay together: it extracts all 43 `Print("BLOCKED…` / `Print("DUPLICATE BLOCKED…` forms from `mql5/CriticalConsensusEA.mq5` and fails if any of them classifies as a healthy poll or as `unclassified_blocked`. That fallback state exists on purpose — a new guard in the EA without a rule here would otherwise be reported as "the advisor is polling the bridge", which is exactly how this chain died twice on 2026-11-02 without saying so.
- Measured on the live chain when the checker was written (04:12–04:35 UTC, demo 114929502): terminal restarted 03:51:44 UTC, `CriticalConsensusEA_Demo (XAUUSD,H1)` initialized 03:51:45 with a valid token and then logged `BLOCKED: kill-switch state unavailable; HTTP -1, error=4014` on **356 consecutive polls** until 04:11:03, after which it wrote nothing at all; through all of it the bridge was listening — on the runner's own process (pid 4636, `127.0.0.1:8000`) — so the block was the terminal's permission, not the network and not the token. The gold runner kept cycling every 20 s and every cycle since 03:52 UTC is `BLOCKED ['POSITION_CAP_REACHED', 'SYMBOL_ALREADY_OPEN']` behind the 0.01 remnant, so it queued nothing to expire. Broker-side there are still exactly **2** deals with magic 8172026 and a `CC:` comment (EURUSD 0.06 @ 1.11563, XAUUSD 0.04 @ 3902.27), both from 00:00–00:07 UTC; equity 99 974.14 with the remnant at +0.60.
- Re-read at 05:31–05:33 UTC, and the transport block is no longer the block (this supersedes the 04:47–04:51 reading in the next bullet, which was true when measured). `Get-NetTCPConnection -LocalPort 8000 -State Listen` returns **nothing**, and `Win32_Process` holds **no `run_mt5_demo_trading.py` at all**: the gold journal's last row is seq 1096 `agent_cycle` at `2026-11-02T05:17:01.263800+00:00` (1096 rows, 14 minutes of silence at the read), its `.err` carries only the three-line startup banner, and the all-universe supervisor's last line is still `03:09:23Z heartbeat`. The advisor, meanwhile, is alive and polling: `20261102.log` (stamps on the machine clock, UTC+7) holds `CriticalConsensusEA initialized. Demo execution is ENABLED.` at 10:51:45 (= 03:51:45 UTC), then **356 `error=4014` lines from 03:51:48 to 04:11:03 UTC**, then a different refusal — `BLOCKED: kill-switch state unavailable; HTTP 1001, error=5203` — running to 05:33:22 UTC, 477 such lines across the day. So the allow-list act happened: the terminal now lets `WebRequest` out, and what it finds is an empty port. `check_ea_transport.py` on this state prints `bridge_unreachable` and exits 1, which is the correct verdict and the reason its "healthy" set excludes it. **What is left is not a GUI act but a secret**: `MT5_BRIDGE_TOKEN` and `MT5_KILL_SWITCH_ADMIN_TOKEN` are absent at Process, User *and* Machine scope (measured by length only, never printed), so no bridge can be started that the already-attached EA would authenticate against — a fresh token would produce `token_rejected` and look like a fix while being none.
- Re-measured at 04:47–04:51 UTC, and the earlier reading of "the runners are dead" was itself wrong: the **gold runner is alive and journaling** (`agent_data/demo_gold_live.db`, 942 rows / 457 cycles, last line `2026-11-02T04:51:19.899`, 20 s cadence), but all 300 of its most recent cycles are `['POSITION_CAP_REACHED', 'SYMBOL_ALREADY_OPEN']` — the 0.01 remnant occupies the only slot under `max_open_positions: 1`, so it queues nothing. The all-universe runner has been silent since 03:09:36, and its 26 outcomes decompose exactly: **13 `COMMAND_EXPIRED`, 12 `VOLUME_ABOVE_DEMO_CAP` (AUDUSD, NZDUSD, US500 four rounds each — the sized volume is above the 0.20-lot demo ceiling), 1 `NO_FILL_OBSERVED`**. Account 114929502: balance 99 973.54, equity 99 975.18, free margin 99 936.16, remnant **+1.64** floating, still exactly 2 deals with magic 8172026 and a `CC:` comment. `check_ea_transport.py` on that state prints `ea_silent` with `stale_from: request_not_allowed` and exits 1.
- The two runners also disagree about the same broker fact, which is worth stating separately from the transport: the gold run's outcome carries `broker_order_sent: true` and the whole deal `10935023434`, while the all-universe run journalled `NO_FILL_OBSERVED` at 00:00:40 for a command whose fill **is** in the account history (deal commented `CC:cc-3f62effe965d7ad51…`). That difference is now reproduced instead of hypothesised: probing `history_deals_get` on the live terminal at 04:58:51 UTC with the same seven-day left edge, a right edge of `now+4h` returns 1 row and **neither** magic-`8172026` deal while `now+8h` returns 5 rows and **both** — the right edge is what clips, because those naive bounds are read on the server clock (+7200 s), and the −1h/+1h window the code carried at 00:00:40 answers an empty book. `entry_deals()` carries the widened window (−7 days / +3 days) plus that measurement in its comment, and `test_entry_deals_asks_for_a_window_wide_enough_to_see_a_server_clock_fill` refuses any narrowing back below the measured-safe margin.
- Tests: **1028 passed, 10 skipped** at that reading, up from 996 — 28 new cases in `tests/test_check_ea_transport.py`, including the newest-file-not-a-literal case, UTF-16 decoding, an empty log, a directory that does not exist, the armed-stop staleness exemption, the CLI exit codes, and a guard that no operator script may name a day in code; plus 4 in `tests/test_mt5_demo_bridge.py` for the aborted-socket report, one per expected disconnect type and one that an unrelated exception still gets the full traceback.
- `scripts/mt5_demo_bridge.py`: the runner log contained six blocks reading `Exception occurred during processing of request from ('127.0.0.1', 5075x)` with `ConnectionAbortedError: [WinError 10053]`, which looks exactly like the bridge falling over. It was not: the advisor's own `WebRequest` gives up as soon as the terminal refuses the poll, the loopback socket dies mid-request, and `socketserver.handle_error` answers every such disconnect with a full 19-line traceback while the queue keeps serving normally — lines before and after them are plain command lifecycles. `ThreadingHTTPServer` is now subclassed with a `handle_error` that prints **one** line for `ConnectionAbortedError`, `ConnectionResetError` and `BrokenPipeError` and delegates everything else unchanged, so a real handler bug still gets the full report. Measured on a throwaway bridge on an ephemeral port (not `:8000`, which is the other agent's), three forced resets: with the filter disabled **57 stderr lines including tracebacks**, with it in place **3 lines, no traceback**, and the same bridge answers a later `GET /v1/risk/kill-switch` with 200 in both cases. Covered by 4 new cases in `tests/test_mt5_demo_bridge.py`.
- What the all-universe supervisor log actually records, since the tracebacks made it read as a crash log: `rc=3` **six times** from 00:01:58 to 00:07:04 at exactly 60 s apart, each one the same line — `В журнале моста есть отданные команды без подтверждённой сделки: cc-3f62effe965d7ad5111db40b5070` — the honest-start guard refusing to restart over the command served at 00:00:40 whose fill it had journalled as `NO_FILL_OBSERVED`, which is the `entry_deals()` clip above rather than a fault in the guard: that deal **is** in the account history, commented `CC:` plus the first twenty characters of the same id, so the broker took it and only the read missed it. Then `00:07:06 … 01:28:33 port 8000 held by foreign pid=12464; standing down, not killing` (six lines, ~1 h 21 min of deliberate stand-down, the constraint working); `02:22:57 runner pid=10928 exited rc=4294967295` = `-1`, and `2026-11-01T23:20:55 runner pid=14392 exited rc=3221225786` = `0xC000013A`, a console interrupt. Both of those are external terminations — the runner log holds **no traceback from `run_mt5_demo_trading.py` at any point**, so neither was a Python failure — and the supervisor restarted within the same second each time. Its last entry is `03:09:23Z heartbeat journal_rows=11116`: the supervisor stopped writing, which is why the runner has been silent since 03:09:36.
- Trading resumed at 05:43 UTC, and the third broker fill is the first one this repository can prove end to end from its own journal (measured 05:49:37 and 06:10:18 UTC, demo 114929502). `scripts/system_start.ps1` was launched at 05:43:12; the journal `agent_data/demo_live_20261102.db` holds **657 events / 324 cycles** from `05:43:13.106` to `06:10:16.472`, of which exactly one cycle is clean: command `b3d8fc393ea14b1b836da9e91fb9f8ac` / `cc-1bac6713ce6d749fe849117770c4` queued 05:43:13.368 (`SELL EURUSD`, expected 1.11823, `decision: AUTO`), served 05:43:15.374, outcome 05:43:32.402 `state: FILLED`, `broker_order_sent: true`, `requires_manual_reconciliation: false`. Broker-side the same order is deal **10938325361**, position **11189096387**, EURUSD SELL 0.09 @ 1.11823, SL 1.12042, TP 1.11385, magic 8172026, comment `CC:cc-1bac6713ce6d749fe` — so the widened `entry_deals()` window resolved a fill that the same code reported as `NO_FILL_OBSERVED` at 00:00:40, on the same account, twelve hours earlier. The command's own gates read `final_risk_value 19.7229` (`final_risk_fraction 0.000197`, inside the 0.0002 budget), `risk_reward_at_fill 2.0`, `slippage 0.0`, `final_margin_required 100.6407`. The other 323 cycles block behind that position: **241 `POSITION_CAP_REACHED`, 80 `POSITION_CAP_REACHED + SYMBOL_ALREADY_OPEN`, 2 `MINIMUM_LOT_EXCEEDS_RISK_BUDGET`, 1 clean** — `max_open_positions: 1` is account-wide, so one filled order stops every symbol in the set, and the two minimum-lot cycles are instruments whose smallest tradable lot already risks more than 20 USD. Account at 06:10:18: balance 99 968.87, equity 99 966.62, the open position at −2.25 floating.
- The realized side of that same read corrects a number this file carried: `history_deals_get` over −2 days/+3 days returns **7** deals — the three entries above with magic 8172026 and a `CC:` comment, three closing deals and the 100 000 deposit row, and **every one of those six non-entries carries magic 0 and an empty comment**: they are not this chain's commands, and no journal line claims them. Their profits are −1.56, −24.90 and −4.67, i.e. **−31.13**, and 100 000 − 31.13 = the balance the broker reports, so the whole closed loss on the account is that −31.13 and nothing is missing from the read. The earlier "realized −26.46" was measured before the last 0.01 XAUUSD closed at 05:17:11 UTC.
- `scripts/check_ea_transport.py` and `scripts/mt5_demo_bridge.py` (a BROKEN verdict that was not broken): the status line said `ea_silent` over a chain that had filled an order **two seconds earlier**, and the reason is that the advisor prints nothing while everything is fine. Measured across the three `MQL5\logs\*.log` files the whole corpus is **4904 lines: 4875 `BLOCKED`, 10 initialized, 8 `automated trading is disabled`, 6 enabled, 3 `Order accepted`, 2 `PROBE`** — there is no healthy-poll line in the source at all, so log silence cannot separate a live poll loop from a detached EA, and the checker was inferring one from it. The bridge is now the witness: `BridgeState` records the count and the timestamp of every **authenticated** GET on the two endpoints only the advisor polls (`/v1/risk/kill-switch`, `/v1/execution/next` — refused requests, POSTs, an `Origin` header and the health read itself are not counted, each pinned by a test), and a new token-guarded `GET /v1/health` answers with those counters plus the poll age measured on the bridge's own clock. The checker asks that endpoint **only** when its verdict rests on silence (`ea_silent`, `never_polled`), so a named refusal in the log is never papered over by a counter from elsewhere. Measured against the real log directory with a current-build bridge on an ephemeral port (not `:8000`): `--no-bridge` gives `ea_silent (BROKEN)` exit **1**, the same read with the bridge gives `polling (healthy)` exit **0** with `bridge: advisor polled 0.1 s ago, 2 polls total`; an unauthenticated `/v1/health` answers **401**; a stale poll (900 s) keeps `ea_silent` and says the silence is real; and every failure mode (`unreachable`, `http_401`, `http_404`, `not_json`, a malformed URL) answers with a reason instead of raising. Independent of the new endpoint, the port is demonstrably being polled right now: the loopback socket table over 06:05:06–06:06:36 UTC held **80 TIME_WAIT connections with 80 distinct peer ports, sliding from 62520–62673 to 62631–62789**, ~57 freshly closed connections per 45 s.
- Not applied to the running process, and that is the operator's act: the runner on `:8000` started at 05:43:12, before this change existed, so its bridge has no `/v1/health` and the checker says so honestly — `bridge: the bridge has no /v1/health: it is running an older build than this checker` — and leaves the log verdict alone. A restart of `scripts/run_mt5_demo_trading.py` would make the live verdict correct; it was not done here, because stopping the process that is currently trading is not this agent's call, and the open position's SL/TP live on the broker either way.
- Tests: **1038 passed, 10 skipped**, up from 1028 — 3 new cases in `tests/test_mt5_demo_bridge.py` (a poll on an EA path is counted and aged; the health read is not itself a poll and requires the token; refused reads, wrong tokens, `Origin` headers and POSTs count nothing) and 7 in `tests/test_check_ea_transport.py` (a fresh poll rescues a silent log, a stale poll confirms it, an unanswered bridge overrules nothing, a named refusal is not replaced, `never_polled` is rescued the same way, the probe never raises, and the token comes from the environment or the git-ignored file rather than an argument).

## Unreleased — round 12 review fixes (blocker: an absent kill-switch record is not consent)

- `scripts/run_mt5_demo_trading.py` (`_run`): a fresh kill-switch state path is now seeded **armed**, never from the policy's own flag. Previously a policy that omitted the stop wrote `kill_switch_active: false` into the first record, so deleting `kill_switch.json` between two processes disarmed the emergency stop with no admin token, no operator record and nothing in the journal — "absence is not consent" was asserted in the docs but not implemented. A new process cannot distinguish a path that was never used from one whose stop an operator deleted, so it now creates the record armed with `SEEDED_REASON` and halts before any order is built. Lifting a seeded stop takes `MT5_KILL_SWITCH_ADMIN_TOKEN` through `--release-kill-switch` like any other release; the run says so on stderr through the new trilingual `runner.kill_switch_seeded` key.
- `scripts/mt5_demo_bridge.py`: new `SEEDED_REASON = "SEEDED_ARMED"` constant, distinct from `UNSEEDED_REASON` (the read-side blocker) so an operator can tell a stop this run armed because nothing existed from one a broken record is being refused.
- Tests: 747 passed, 10 skipped (DB/Redis), up from 744 — 3 new cases in `tests/test_mt5_demo_runner.py` (a fresh path is armed rather than consented to and the run queues nothing; deleting a record from outside a process reseeds it armed; a released record is never reseeded by the next run) plus the `release_stop()` helper that models the operator act every end-to-end run now has to begin from.
- `scripts/run_mt5_demo_trading.py` (interrupt over a live command): a Ctrl-C that lands after a command entered the bridge queue used to print "the operator stopped demo execution", report `commands_queued: 0`, `requires_manual_reconciliation: false`, leave the durable stop unarmed and exit `0` — the one case where the run genuinely does not know whether the broker took the order was the one case reported as clean. `DemoExecutor` now remembers the command it handed to the queue in `pending_send` (cleared by a read fill, by a bridge-recorded expiry, since an expired command is never served again, and by any outcome it returned itself), and `journal_interrupted_send()` books the leftover as `UNCONFIRMED / INTERRUPTED_BEFORE_FILL_WAS_KNOWN` with `broker_order_sent: null`, so the audit trail ends on the truth instead of on a dangling `demo_order_served`. The run loop arms the same durable stop an uncertain timeout arms (`EURUSD:UNCONFIRMED`), counts the command, sets the new summary field `interrupted_with_send_in_flight` and exits **4** — a new code, because 0 would tell the operator the reconciliation was already done. An interrupt before any command exists still exits 0 and arms nothing.
- The same run now passes `out=sys.stderr` to `DemoExecutor`: `_say` wrote only when an `out` stream was supplied, so every localized per-command line the executor produces (`runner.queued`, `runner.filled`, `runner.refused`, `runner.expired`, `runner.unconfirmed` and the new `runner.interrupted_send`) was computed, formatted and thrown away during a real run.
- Tests: 752 passed, 10 skipped, up from 747 — 5 more cases (a resolved send leaves nothing pending, an interrupt over a served command is journalled once as unconfirmed, and the end-to-end pair: interrupt over a live command exits 4 with the stop armed and the reason readable, interrupt with no command exits 0 and leaves the released record alone).
- `scripts/run_mt5_demo_trading.py` (fill matching): the deal a command is matched to is now **dated and described**, not merely addressed. `match_entry_deal` had no time condition at all and `_await_fill` read history from `now - 10 minutes`, so a deal left over from an earlier attempt of the same signal — `client_order_id` is a hash of run id, proposal and lot, so two identical cycles really do share one — answered a command the broker never took, and the run booked `FILLED` and kept trading. `not_before` puts a floor at the moment the command was built (`DEAL_CLOCK_SKEW_SECONDS = 60` covers the residual disagreement between the terminal clock and this one after the server offset is applied), a row with no usable `time_epoch` is skipped because it cannot be proven to postdate the command, and the same floor now guards the startup reconciliation in `unresolved_served`. New `deal_mismatches(deal, command, point=, max_slippage_points=)` then checks the lot and the price the command actually carried: `FILL_VOLUME_MISMATCH`, `FILL_PRICE_OUTSIDE_SLIPPAGE_BOUND` against the runner's own 20-point bound, or `FILL_VOLUME_UNVERIFIABLE` / `FILL_PRICE_UNVERIFIABLE` when either side is not a usable number. A mismatch is **not** a fill and not a refusal either — the account holds something this run cannot describe — so it returns `UNCONFIRMED` with the deal attached, `broker_order_sent: null`, `stop_run` and `requires_manual_reconciliation`, journalled through the new trilingual `runner.fill_mismatch` line.
- Tests: 759 passed, 10 skipped, up from 752 — 7 more cases (a tombstone is not resolved by a deal predating it, the matcher floor and its tolerance, an undated row refused, `deal_mismatches` on lot/slippage/exact fill/unverifiable fields, and three executor cases: a stale deal times out as `NO_FILL_OBSERVED`, a wrong lot and a 490-point fill both stop the run instead of claiming `FILLED`).
- `scripts/run_mt5_demo_trading.py` (where durable state lives, and which run id it is keyed to): the default `--tombstones` path was resolved against the **current working directory** and the default `run_id` was `demo-{random 8 hex}`. Both quietly unmade a guarantee written in this file's own docstrings. `client_order_id_for` is deterministic in (run id, signal, volume) so the bridge can refuse a second order carrying an id it has already served — but with a fresh random id every process, a restart after an unknown outcome minted ids the tombstones had never seen, which is how the duplicate guard stopped only ever protecting one process. And run from a different directory, the same command traded against a different served-id history and a different kill switch; run from an empty one it looked exactly like a first-ever run, which after the blocker above is a run that *arms* a stop. New `resolve_state_path()` anchors a relative path at the repository root (`_REPO_ROOT`) and `default_run_id_for()` derives the id from that resolved state path plus the resolved policy file, so a restart re-joins the records it left behind while a different account, instrument set or policy still gets its own id.
- `scripts/mt5_demo_bridge.py` (`KillSwitchStore._write`): the atomic swap wrote to a fixed `kill_switch.json.tmp` name. Any two stores pointed at one path shared that scratch file — one could rename the other's half-written body into place, or unlink a file it was still filling, so the `os.replace` that exists to make a torn write impossible became the thing that could cause one. Each write now takes its own `tempfile.mkstemp` name in the same directory, and an exception before the rename unlinks it, leaving no orphan to be mistaken for state on the next read.
- Tests: 765 passed, 10 skipped, up from 759 — 5 more cases (a relative state path is anchored at the repository and an absolute one passes through; the default run id repeats for one state file and policy but differs across either; two `main()` calls without `--run-id` hand `_run` the same id while an explicit one is obeyed; two switch writes use distinct scratch names and leave the directory clean; a failed rename keeps the previous record, leaves no scratch file, and still holds the arm in memory). The shared `POLICY_YAML` fixture also lost a top-level `require_demo` key the loader rejects, which had been making the missing-bridge-token subprocess test exit 2 for the wrong reason; it now asserts the localized token refusal.
- `scripts/run_mt5_demo_trading.py` (which account, and which secret): the send path's identity gate was `policy.expected_login is None or account.login == policy.expected_login`, so a policy that named no account passed for **every** account the broker has — and the order really does go to whichever one the terminal is signed into, because `require_demo` says only how an account is funded. An unbound `expected_login` or `expected_server` is now a refusal before the journal is created, through the new trilingual `runner.identity_unbound` (exit 2). The read-only agent keeps its optional pins; a CSV paper run has no account to name, and its fixed paper identity is unchanged.
- The same file (`--bridge-token` removed): the bridge secret could be passed as a command-line argument, which puts it in any process list on the machine and in the shell history that typed it. `MT5_BRIDGE_TOKEN` is now the only way in, the orphaned `cli.bridge_token` help entry is gone, and `cli.run_id` / `cli.tombstones` help says what those arguments actually do now.
- `DemoTerminal` (two silent numbers made audible): `_restore_baselines` re-seeded the day's risk budget from live equity whenever the journal held baselines for a different account, server or broker day, which is indistinguishable from a run that has never lost anything — so the adapter now takes `out` and `language` from the runner and says so through the new trilingual `runner.risk_baseline_seeded` (a first-ever journal stays quiet, since there was nothing there to lose). `realized_daily_pnl` likewise stopped skipping rows it could not read: a this-magic closed deal with an unusable `time_epoch`, `profit` or a present-but-unparseable `swap`/`commission`/`fee` now raises, and the agent already treats a raising property as `EQUITY_BASELINES_UNAVAILABLE` and blocks the cycle. A cost column the broker does not report at all is still just absent.
- Tests: 777 passed, 10 skipped, up from 765 — 12 more cases (the bridge secret cannot be put on argv and is absent from `--help`; each unbound identity combination refuses with the localized message and creates no journal; a re-seeded day budget is announced with the account and the equity it counts from while a first journal is not; six unusable closed-deal shapes refuse the day; an unreported cost column still adds up). `--bridge-token` appeared in no test or document, and `tests/test_mt5_demo_runner.py` now builds its policies through a local `make_policy` that pins the fixture account, since the shared one is the read-only agent's.
- `mql5/CriticalConsensusEA.mq5` (`OnInit`): every chart input that a guard measures is now capped before the timer that uses it starts. `EA_VOLUME_CEILING = 0.20`, `EA_SLIPPAGE_CEILING = 20`, `EA_POLL_CEILING = 60` and `EA_TTL_CEILING = 600.0` are compile-time constants, and five fail-closed checks (`MagicNumber <= 0`, poll interval, volume, slippage, TTL) each print `BLOCKED: ...` and return `INIT_FAILED` ahead of `EventSetTimer(PollSeconds)`. The old `EventSetTimer(MathMax(1, PollSeconds))` clamp is gone: clamping a `PollSeconds = 0` to one second traded an operator's typo for a 1 Hz market poll, and a cap checked *after* the timer starts only ever applied to the second poll. An input the guard reads must be bounded before the guard runs.
- The same file (`ExecuteDemoCommand`): the slippage, geometry and broker-stops-level guards now measure **the numbers the broker receives**. `order_price`, `order_sl` and `order_tp` are the `NormalizeDouble(live_price/sl/tp, digits)` results, they are checked for positivity (rounding a sub-point stop against price can produce a level that is not distinguishable from it), and the same three locals are bound to `request.price`, `request.sl` and `request.tp`. Previously the guards read the unrounded doubles while the request carried the rounded ones, so the order that was checked and the order that was sent were two different orders — on a 3-digit symbol that is a sub-point difference in exactly the direction the stops-level guard exists to prevent. A guard on a number that is never sent is not a guard on the order.
- `scripts/build_demo_ea.py`: a build is now evidence rather than an assertion. It records `reviewed_sha256` (the checked-in source), `generated_sha256` (what was handed to the compiler, after the demo gate is switched) and `ex5_sha256`, and it unlinks any `CriticalConsensusEA_Demo.log` left by an earlier attempt before invoking MetaEditor, then requires the log to exist *and* to be newer than the moment the compile started. Without that, a compiler that refused to run — wrong path, locked directory, a GUI error dialog nobody saw — still produced "0 errors, 0 warnings" from last week's log, which is the exact failure mode a build check is supposed to catch.
- `scripts/check_mql5_safety.py`: the catalogue now proves all of the above — **473 static invariants and 105 weakening mutations, up from 340 and 86**. New pinned checks cover the four ceiling constants, the five `OnInit` caps, `EventSetTimer(PollSeconds)` using the capped input, and the three `order_*` bindings; new ordering checks require every cap to precede the timer, the rounding to precede the guards that use it, the guards to precede the request bindings, and — negatively — that no guard measures the unrounded price and the request never carries an unrounded level. Two existing mutations (`slippage threshold widened`, `stops level check disabled`) had to be re-anchored onto the `order_*` forms, which is the checker telling us the invariant moved rather than the source drifting.
- `scripts/system_start.ps1` (a secret that was in the repository): the bridge token was a literal in a tracked, **pushed** file, which is the credential that authorizes order commands on the demo bridge. The literal is gone; the script now takes `MT5_BRIDGE_TOKEN` from the environment, or reads it from the git-ignored `agent_data/demo_bridge/bridge_token.txt` beside the kill-switch state, or generates a 44-character one there on first use, and refuses anything shorter than the EA's own 24-character floor. It also stopped setting the token twice from a variable that no longer exists, and now launches the four instruments the live run actually trades against the live journal instead of gold alone. The exposed value has been rotated: the old token now gets `401` from the bridge and the EA fails closed on it (`BLOCKED: kill-switch state unavailable; HTTP 401`), which is the observed behaviour, not the hoped-for one. Loopback binding and the `Origin` rejection limited what a remote reader could do with it; rotation is still the only fix that does not depend on that.
- Tests: 840 passed, 10 skipped in total, up from 777 — 6 more cases here (`test_mql5_static_guards.py` 26→28, `test_demo_ea_build.py` 6→10: build provenance hashes recorded and differing between reviewed and generated source, a stale log refused, a log that never appears refused, the input caps holding before the timer, the guards measuring rounded levels); the other 57 came from the live paper-portfolio work that landed on this branch in between. `scripts/check_mql5_safety.py` passes against the current source (473 invariants, 105 mutations) and `MetaEditor64.exe` compiles the generated demo EA with 0 errors.
- `mt5_agent.resolve_broker_symbol` + `DemoTerminal.symbols/use_broker_names` (a second broker by name, not by luck): MetaQuotes-Demo lists `XAUUSD`; Exness, RoboForex, ITAM and AMarkets list `XAUUSDm`, `XAUUSDc`, `XAUUSD.i`. The runner asked for the bare name, `symbol_info()` returned nothing, `symbol_select()` failed, and every instrument on the new broker was reported unavailable — which reads like a strategy that stopped working and is only a spelling. Requested names are now resolved once against the server's own list, journalled as `demo_symbol_alias`, and printed (`This broker lists XAUUSD as XAUUSDm`). The rule is deliberately narrow because a wrong resolution is a different contract: an exact match always wins, only six-character instrument codes may be aliased (so `BTC` cannot resolve to the US stock ticker `BTCO`, which MetaQuotes-Demo really does list), the suffix is at most three characters, and two candidates refuse rather than guessing the account type. MT5 turns out to be **case-sensitive** — `symbol_info("XAUUSDM")` returns nothing on a server that lists `XAUUSDm` — so the exact spelling is what reaches the terminal and the order command, while the policy keeps its upper-case internal key; `DemoTerminal.use_broker_names()` is the mapping, and the executor's proposal check now upper-cases both sides instead of only one, which had made every aliased instrument refuse itself. `symbols_get` lives on the runner's adapter, not in `mt5_agent`: the package's nine-call order-free surface is an invariant test, and a portability convenience does not get to widen it.
- Tests: 862 passed, 10 skipped — 11 of them here (exact wins over suffixed; each observed suffix form resolves; several variants refuse; a short name never aliases to an unrelated ticker; a four-character suffix is a different instrument; the server's own casing survives a case-insensitive lookup; a genuinely absent instrument still refuses; the adapter maps both directions of the name and rejects two spellings on one key; and the end-to-end run that trades `XAUUSDm`, drops the declared `symbol_spec` for it, journals the alias and reports the base name as requested).
- `scripts/ea_logic_mirror.py` (new) + `tests/test_ea_logic_parity.py` (new) — the static checker proves the EA *source* keeps its guards; it cannot notice that the EA and the runner compute the same rule differently. This is the runtime half: a line-for-line Python port of `ValidateFlatObject`, `JsonString/JsonNumber/JsonBoolExact`, `IsSafeIdentifier`, `IsValidBridgeToken`, `Fnv1aHex`, `IsLoopbackHttpUrl`, `NormalizeDouble` and the whole `ExecuteDemoCommand` decision chain, run against what this repository's Python actually produces. The ceilings (`EA_VOLUME_CEILING`/`EA_SLIPPAGE_CEILING`/`EA_TTL_CEILING`), the default chart inputs, `MagicNumber` and the `required_keys[]` list are read **out of the .mq5 text**, so editing one side without the other fails a test; the required-field list is compared three ways (EA source, `mt5_demo_bridge.REQUIRED_KEYS`, `schemas/order_command.schema.json`). Bodies come from a bridge on a real socket — an armed and a released `GET /v1/risk/kill-switch`, including the reason string the bridge scrubs of `"`/`\`/control characters — so the port parses the bytes on the wire, not a fixture. The two number and identifier grammars are compared token by token against `json.loads` and the bridge's own regexes, with `NaN`/`Infinity` excluded the way the bridge's `allow_nan=False` excludes them. Running the file directly prints 8 self-checks.
- `scripts/run_mt5_demo_trading.py` (`price_gates`): the same rounding bug the EA had, found on the Python side by that harness. The gates measured the raw quote, the stop and the target, while the request the EA sends carries `NormalizeDouble`-ed levels, so on a 2-digit symbol at `live = 4000.005` with a 1.02-point stop distance and a 2-point broker stops level the runner **blocked** `STOP_INSIDE_BROKER_STOPS_LEVEL` on 1.02 while the chart would have taken the rounded pair at exactly 2.00. New `broker_level(value, spec)` rounds price, stop and target to `spec.digits` (MQL5's `MathRound`, so halves away from zero) before the slippage, geometry and stops-level checks, and the rounded price is what `offer()` hands to the margin calculation. The 200-point grid in the parity test now requires both implementations to give the same verdict on the same numbers; the lot-step tolerance is deliberately *not* required to match (a millionth there, a billionth here) and is pinned in the safe direction instead — the runner never queues a lot the EA would refuse.
- Tests: 971 passed, 10 skipped — 109 of them in `tests/test_ea_logic_parity.py`. `scripts/check_mql5_safety.py` still reports 473 invariants and 105 mutations against unchanged EA source, because this change lands entirely on the Python side of the boundary.

## Unreleased - contract-priced risk (blocker: a broker tick field sized gold ten times over budget)

- `mt5_agent.SymbolSpec` gained an optional `contract_size` and derives `cash_per_price_unit` from it, falling back to `trade_tick_value_loss / trade_tick_size`; `sizing.size_position`, `LiveMT5Backend.margin_requirement`, `agent._position_stop_risk` and `paper._pnl` now read that one property instead of the tick ratio. Measured on MetaQuotes-Demo on 2026-11-02: XAUUSD publishes `trade_tick_size=0.01` with `trade_tick_value_loss=0.1` - 10 $ per price unit per lot - while `trade_contract_size=100`, `mt5.order_calc_profit(ORDER_TYPE_BUY, "XAUUSD", 1.0, 3902.27, 3947.07) = 4480.0` and the margin the broker actually blocked for this run's own 0.04-lot order (156.09 $ at 3902.27, leverage 100) all say 100 $. Nothing was wrong with the account: the tick field is wrong for metals (XAGUSD says 0.5/0.001 = 500 against a 5000 contract). EURUSD (100 000) and US500 (1) agree, which is why only the metals were hit and why a suite built on FX-shaped specs stayed green.
- The live consequence, read back off the broker's own history: 0.0002 of 100 000 $ is a 20 $ budget against a 44.80-point 2x-ATR stop, and one 0.01 lot costs 44.80 $, so the honest answer is `MINIMUM_LOT_EXCEEDS_RISK_BUDGET`. Priced from the tick ratio the same command sent **0.04** lots - 179 $ at a stop the 20 $ budget approved. Broker history as read at 2026-11-02 (server clock, UTC+2): deal 10935023434 on order 11185923231, XAUUSD SELL 0.04 at 3902.27 with sl 3947.07 / tp 3812.41, then deal 10935754921, BUY 0.03 at 3910.57 for **-24.90 $**, leaving the 0.01 lot still open with 44.80 $ of stop risk. That closing deal is the fourth independent measurement of the same number: 8.30 points x 0.03 lot x 100 $ per unit per lot = 24.90 $ exactly, which is what the contract size says and eight times what the tick field said.
- `scripts/run_mt5_demo_trading.py` (`_run`): a live run now drops the policy's declared `symbol_spec` for **every** instrument, including the policy's own symbol, rather than trusting it for the base one. A declaration is a record of one measurement taken at one broker; the terminal is the measurement. Before this, the gold-only run sized from `config` and the tick fields in it.
- `mt5_risk.collect_mt5_risk_snapshot` + `risk_controls.risk_snapshot_matches_order`: the collector prices both the order's risk and every open position's stop risk from the contract figure and publishes what it used as `risk_cash_per_price_unit`, plus `risk_contract_size` when the contract was usable. The verifier reconciles against the published figure and **refuses** a row that recorded a usable contract size and then priced from something else (`RISK_CASH_PER_PRICE_UNIT_NOT_THE_CONTRACT_SIZE`) - a snapshot that agrees with its own tenfold understatement is exactly the thing this independent recompute exists to catch, and the old code would have signed off on it. Rows journalled before the field appeared still reconcile against their own tick pair. The contract counts as cash only while `currency_profit` matches the account currency: 100 troy ounces is not 100 dollars on a EUR-funded account, where the tick value - which the broker defines in account currency - stays the fallback. That is the same rule `SymbolSpec.from_terminal` enforces by refusing such a symbol outright.
- `config/forward/01..06`: every declared `symbol_spec` states its `contract_size` (EURUSD 100 000, XAUUSD 100), and 06 - the all-universe live policy - moves `risk_per_trade_fraction` from 0.0002 to 0.0005, the smallest fraction the minimum gold lot fits at a 2x-ATR stop. 0.0002 with correct pricing is not a conservative budget, it is a permanent refusal to trade metals.
- Tests: 990 passed, 10 skipped, up from 971 - 18 in the new `tests/test_mt5_contract_sizing.py` (the contract cash equals the terminal's own recorded `order_calc_profit` figures; the regression itself, 0.04 lots tick-priced against a contract-priced refusal; the minimum-lot denial; margin reconciling to the 156.09 $ actually blocked; FX unchanged; the tick fallback when the contract is absent or zero; a non-USD account falling back and publishing no contract size; a collector snapshot the independent verifier accepts, and that same snapshot denied once the tick figure is substituted for the contract one).
- What the all-universe run actually reaches, measured while verifying the fix: on this USD account `SymbolSpec.from_terminal` refuses USDJPY, USDCAD, USDCHF and EURGBP because their profit is not paid in USD, and the terminal serves no BTCUSD or NAS100, so 7 of the 13 instruments reach trading - EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD, XAGUSD, US500, each with its own cycles in the journal - and the run journals the other six as `demo_symbol_unavailable`. Their tick fields do speak account currency (USDJPY 633.60 per price unit against 630.00 from `order_calc_profit` scaled off one tick; USDCAD 71 567.62 against 72 000), so the refusal is not a money-field problem - it is the margin leg, where `volume x cash_per_price_unit x price / leverage` numerically returns base units for a cross. Recorded as a limitation in `docs/MT5_AGENT.md` / `.en.md` / `.vi.md`; lifting it needs a base-currency margin computation, not a deleted check.
- `config/forward/06_ema_all_universe_h1.yaml`: `max_open_positions` 1 → 7. The all-universe run had been trading one instrument the whole day: the gold position taken at the open filled the only slot, and every other symbol came back `POSITION_CAP_REACHED` - 200 consecutive cycles of it in the journal. Seven is the count this account reaches, and `SYMBOL_ALREADY_OPEN` still bounds each instrument to one position, so the change raises how many instruments are in, never how many times one is. Worst case is 7 × 0.0005 = 0.0035 of equity at the stops, inside the 0.02 daily-loss cap `RiskLimits` validates against.
- Docs: `docs/formulas.md` gives the source priority and the measured numbers; `docs/MT5_AGENT.md` / `.en.md` / `.vi.md` state the volume formula in `cash_per_price_unit` and record why the tick pair alone is a defect, not a simplification.
- The residual tick-only callers, found by following every read of `tick_value` after that landed (`config/forward/research/05_ema_gold_h1_research.yaml`, `config/mt5_agent.example.yaml`, `scripts/market_matrix.py:terminal_specs`, `scripts/crypto_matrix.py:build_spec`, `scripts/render_demo_evidence.py`, `agent_data/live_gold_ema_policy.yaml` and the git-ignored `agent_data/policies/XAUUSD.yaml`, `XAGUSD.yaml`, `demo_xauusd_real.yaml`): every one of them sized or reported from the tick ratio, because none of them declared a contract. The research and example YAMLs now state `contract_size` (XAUUSD 100, EURUSD 100 000), `walk_forward.py` inherits it because it builds its spec from `market_matrix.terminal_specs`, that function now copies `info.trade_contract_size` into the spec only when it is positive (an unset field reads as 0.0, which `SymbolSpec` would reject as a declared lie), `crypto_matrix.build_spec` states the 1-unit basis it had only been implying, and `tests/test_mt5_demo_runner.py`'s gold fixture is fed the contradictory pair the terminal really publishes - `tick_value=0.1, contract_size=100.0` - instead of a hand-corrected `tick_value=1.0`, so the live-path tests now exercise the fallback-proof rather than a number that was already right.
- `scripts/render_demo_evidence.py` (`planned_risk_currency`, `r_multiples`, `read_symbol_spec`): the evidence report divided realised P/L by `abs(entry-stop)/tick_size*tick_value*volume`, i.e. by the tick ratio, and each journal now states the divisor it used. The first attempt preferred the policy's declared `contract_size` over the ratio; measured against the two real journals that was wrong in the other direction, because a run priced both its P/L **and** its sizing at the tick field, so re-reading last month's gold journal against today's declared contract would have multiplied every risk figure by ten and redrawn booked `-1R` trades as `-0.1R` ones. The report now reads the basis out of the journal: `recorded_cash_per_price_unit(events)` divides `agent_cycle.sizing.risk_per_lot` by `agent_cycle.proposal.risk_distance`, which is the number the run itself booked at (`cash_per_price_unit` in `SymbolSpec`, exactly), and only a journal carrying no usable pair falls back to the policy file through `policy_cash_per_price_unit(tick_size, tick_value, contract_size)` - which prefers the contract, the way the sizer does. `read_symbol_spec` returns the declared contract size as a third value (`None` when the policy predates it, the same "unknown" reading `from_terminal` uses), and every journal in the evidence JSON records `contract_size`, `risk_cash_per_price_unit` and `risk_basis: journal_sizing|policy_contract_size|policy_tick_ratio`. Re-rendering `agent_data/demo_eurusd.db` and `agent_data/demo_xauusd.db` with the current code reproduces the committed `docs/charts/demo_evidence.json` risk columns and R lists exactly (EURUSD booked at 100 000, gold at the 10 $ its tick-based run used) - the committed artifacts were left alone because those journals have gained cycles since they were drawn.
- `mt5_agent.SymbolSpec.from_terminal`: `trade_tick_value_loss or trade_tick_value` could not tell an absent field from a broker that publishes 0.0, and a 0.0 that fell through to the dataclass raised a generic geometry error instead of naming the field. Both tick values are now checked for a positive number, and a symbol with neither gets `has no trade_tick_value`.
- Tests: 996 passed, 10 skipped, up from 990 - 6 more cases (a zero `trade_tick_value_loss` falls back to `trade_tick_value`; both at zero names the field; the report divides by the basis the journal booked rather than by the policy's contract, which is what keeps the committed gold evidence at `-1R`; a journal with no sizing pair does use the declared contract; a policy with neither keeps the tick ratio; a cycle whose proposal or sizing is unusable yields no basis). `tests/test_mt5_agent_sizing_caps.py` keeps its tick-ratio silver spec - the notional-cap assertions are about the cap, not the pricing - but its comment no longer claims to be what MetaQuotes-Demo publishes, since the 500 $ per price unit it uses is the field this section is about.

## Unreleased — documentation reconciliation of 2026-11-01 (no code changed)

Nothing asserts documentation in this repository: no test and no CI job reads these files, so every figure below was re-measured by hand in the session that wrote it (`.venv\Scripts\python.exe -m pytest -q`, `scripts/check_mql5_safety.py`, the demo journal, `Config/common.ini`, the EA's own log, the bridge state directory). The venv interpreter is part of the measurement, not a preference: several tests re-exec `sys.executable`, and a first run with a system Python that lacks the installed package reported 36 `ModuleNotFoundError` failures — an environment artifact that reads exactly like a regression.

- `docs/MT5_AGENT.md` (+ `.en.md`, `.vi.md`) — the evidence table moved two rows, and the reason is a measurement, not a rewording.
  - *"The EA really polls the bridge"* went from NOT PROVEN to **PROVEN**. Transport was proven without a trade, which is the only honest way to prove it on a closed market: arm the durable stop through the bridge's one write endpoint, watch the advisor's log, release it, watch the lines stop. `MQL5/Logs/20261101.log` (UTF-16) filled with `BLOCKED: kill switch is active or state is invalid.` on the advisor's own 3 s poll cadence between 13:52 and 13:55 UTC and went silent after `--release-kill-switch` — so the advisor parses the bridge's answer, which additionally proves the chart's `BridgeToken` matches the bridge key. The same file gave `HTTP 401` on every tick before the token was pasted into the chart inputs. The doc now carries the method as a procedure (`### Как проверить транспорт, когда рынок закрыт`) together with the three false conclusions it dispelled: an empty log does **not** mean a stopped advisor, MT5 flushes its log with a delay so "no new lines" must be re-read after ~30 s, and `HTTP 401` names exactly one cause — a key mismatch, not a missing market or a missing permission.
  - *"Terminal configuration allows algorithmic trading and loopback WebRequest"* is now **PROVEN, but not durable**, because the durability claim was false. MT5 rewrote `Config/common.ini` at 00:21 on 2026-11-01 — `Enabled=0` and a blank `WebRequestUrl` — ten minutes after `setup_demo_terminal.py --apply` had set those keys at 00:11. The allowlist has to hold in a running terminal, which is why the script still has to be re-run after any GUI toggle; the current values were re-verified in the file (`Enabled=1`, `WebRequest=1`, four loopback origins).
  - *A demo order executed by the broker* stays NOT PROVEN and now says what actually blocks it: `OUTSIDE_TRADING_WINDOW` in all 1386 journalled cycles of the 2026-11-01 run (as of 14:27:34 UTC), feed frozen since Friday 23:59 and a policy that blocks Sunday, first possible cycle Monday 2026-11-02 00:00 UTC — not a sizing problem on a 100 000 USD account, and not the transport.
- Three statements that the code contradicts were removed rather than softened: that the first run seeds the kill switch **from the policy's `kill_switch` section** (it seeds `SEEDED_ARMED`, because a policy describes how to trade and is not permission to trade); README's "479 passed / 423 checks / 85 mutations", now 971/10 and 473/105; and that the daily equity baselines "live in one process and do not survive a restart" — true for `PaperBroker`, false for the demo runner, whose `demo_risk_baseline` rows are journalled and restored per `(login, server, broker day)`.
- Two more went the same way while the observation was being written. An armed stop was described as making the run "print the reason and keep waiting for the operator": the code prints it once at startup and **keeps cycling**, because the durable `kill_switch.json` is enforced only in `DemoExecutor.offer()` — which a market-closed cycle never reaches — while gate 2 of the cycle reads the *policy's* `kill_switch` section. That is the real explanation of the 2026-11-01 journal holding **no `KILL_SWITCH_ACTIVE` blocker at all** during 35 armed minutes, and it is now stated as the two carriers of the stop, with the consequence for verification: transport is checked from the advisor's log, not from a blocker counter. Exit `3` also lost "the account state cannot be read" (account failures are exit `2`); the four `return 3` sites are no broker-served instrument, a `127.0.0.1` socket that will not bind, an unreadable deal history while commands were served, and unresolved served tombstones without `--acknowledge-uncertain`.
- Live counters are now written as timestamped snapshots rather than facts: the same journal read twice in one session gave 1058 and then 1386 `agent_cycle` rows, because a live run writes 12 per minute, and by the second read the bridge had been taken by a different runner on the same policy and tombstone path — hence the same `run_id` — journalling into a different file.
- New prose in the agent doc, all traced to code read this session: the broker-side instrument-name resolution (`resolve_broker_symbol`, exact match wins, six-character base plus a one-to-three-character suffix, several matches refuse instead of guessing, `demo_symbol_alias` in the journal — which is what makes `EURUSDm`, `XAUUSDc` and `xauusd` tradeable rather than "symbol unavailable"); the shared arithmetic of advisor and runner (`scripts/ea_logic_mirror.py`, 109 comparisons, FNV-1a over UTF-16LE bytes so `"a"` hashes to `089BE207B544F1E4`, and the found-and-fixed `price_gates` rounding defect at price 4000.005 / 1.02-point stop / 2-point stops level); the advisor's `OnInit` ceilings, including why an input the guard reads must be bounded before the guard runs (`MaxVolume = 100` from a chart field would execute a lot nobody negotiated from either side); and secrets, state paths and exit codes — no `--bridge-token` and no admin token on argv, because the command line is readable by other users of the host and lands in a `Win32_Process` snapshot; `--tombstones` anchored at the repository root and a run id derived from that path plus the policy file, because `client_order_id` is built from the run id and a random one would make every restart "the first order ever" and unmake the duplicate guard; exit `0` clean, `2` refused configuration, `3` cannot honestly start, `4` `interrupted_with_send_in_flight` — and an armed stop is deliberately *not* an exit code: the reason is printed once at startup and the cycle count keeps going (see the two-carriers bullet above).
- `docs/DEMO_RESULTS.md` (+ `.en.md`, `.vi.md`) gained the 2026-11-01 state as a table (MT5 demo runner; the multi-venue paper run over Bybit/OKX/Kraken with 6 slots and 6 open positions; the live portfolio in `evidence` mode at equity 9 996.17 at 14:45 UTC, 2 positions open and 0 closed) and withdrew its own outdated closing claim that the advisor was not attached to a chart and that `common.ini` carried no allowlist. The crypto runs are labelled paper on real public prices — they do not pass the agent's gates and prove nothing about broker execution — and `agent_data/multi_venue.jsonl` is stated to hold three launches' writes with no single-instance guard, so "18 opens" is an aggregate across sessions while "6 open, 0 closed" is the current process. One item still awaits the broker's own history: a deal with magic `8172026` and a `CC:` comment.
- `docs/VALIDATION_RUNBOOK.md` and `docs/ROLLBACK.md` were brought to the same branch state (`feat/demo-live-run-2026-11-01`, pushed to the public remote, **not** merged; `main` has diverged in both directions), the same current counts, and the same corrected kill-switch seeding and exit-code wording.
- Translation parity: all three languages of both docs carry the same sections, statuses, quoted log strings and numbers; each file keeps its own number formatting convention (`100 000` / `100,000` / `100.000`) and its own established status labels.

## Unreleased — real-data demo run, and the evidence that came out of it

- `scripts/render_demo_evidence.py` (new): turns a decision journal and a year of the broker's own
  H1 bars into four charts and one machine-readable `docs/charts/demo_evidence.json` — equity curves,
  the realised R multiple of every closed paper trade, a chained cost sweep from 0 to 30 bps per side
  for two strategy families, and the hour-of-day t-statistics. It reads the journal through
  `mode=ro` and refuses a database without `audit_events`, drops journal rows whose timestamp or
  number is not usable instead of coercing them, divides P/L by the risk the trade was actually sized
  for (from the policy's own tick spec), and emits hand-built SVG with every label escaped. It uses
  `copy_rates_range` only; this file contains no MT5 trading call. Charts are SVG rather than PNG
  because the repository venv has no plotting library, and a text format keeps them reviewable in a
  diff.
- `docs/DEMO_RESULTS.md` / `.en.md` / `.vi.md` (new) and `docs/charts/`: the first paper demo of the
  agent on real broker data for both instruments the operator asked for — MetaQuotes-Demo account
  `114929502`, 100 000 USD, 540 cycles per symbol: **XAUUSD 23 closed trades, 5 wins, −159.96;
  EURUSD 24 closed trades, 4 wins, −203.39; journal chain `valid: true` (1104 events for gold) and
  `broker_order_sent: false` in every record.** The docs say why rather than burying it: the stop is
  2 × ATR and the target is 2R, so a driftless walk wins exactly 1/3 of the time with zero expectancy
  before costs, and the observed 0.217 / 0.167 sit **below** that line. A lag-1 autocorrelation of
  −0.019 (XAUUSD) and −0.0067 (EURUSD) over 5742 / 6185 real bars explains the mechanism — the series
  retraces and SMA(5/20) enters after the retrace has already run. With 23 and 24 trades the losses are
  not statistically established (t = −1.57, −1.95), which the doc states instead of dressing them up.
- The same file records a cross-check against an independent workspace on public Binance candles whose
  single out-of-sample survivor, Bollinger(20, 2.0) on EURUSDT at +7.1%, **does not reproduce** on the
  broker's real bars: −22.4% at 5 bps per side, −66.1% at 20. Cost sweep, not a headline number: the
  0 bps row is physically impossible and is kept to show that neither family has a margin of safety.
- Tests on this tree: **744 passed, 10 skipped** (DB/Redis), up from 715 — 17 of those are new cases in
  `tests/test_render_demo_evidence.py` (a non-journal database refused, unusable rows skipped and
  points sorted, R computed against the sized risk, three unusable-trade fields, autocorrelation
  separating reversion from trend, costs only ever hurting by exactly the spread of the sweep, flat
  history trading nothing, SVG escaping that a `<script>` label cannot break, empty charts as loud
  errors, `--journal LABEL=POLICY=DB` parsing, and two renderer end-to-end cases that assert the exact
  numbers it writes into the evidence JSON). The remaining 12 landed with the read-only terminal smoke
  probe and the durable-baseline work on this branch, not with the evidence renderer.
- `standalone/docs/install_ru.md` / `install_vi.md`: the Telegram token shown as a format example was a
  string that no scanner can tell apart from a live credential (`1234567890:AA…`), so it is now
  `123456789:REPLACE_WITH_TOKEN_FROM_BOTFATHER` in all four places. The example still teaches the shape;
  it can no longer be mistaken for a secret or copy-pasted as one.

## Unreleased — round 11 review fixes

- `MT5Agent.post_approval_gates(proposal, sizing)` (new): the whole post-approval check a paper fill gets — kill switch, account identity and demo requirement, equity/daily-loss/drawdown baselines, trading window and blackout, position caps and the open book, quote age, spread, the 10% slippage bound against the approved entry, risk/reward recomputed at the fill, the re-sized lot, funded margin, and the broker `stops_level` at that fill — callable on the dicts a cycle already journaled, journaling nothing itself. `run_cycle` stops at `STATUS_OBSERVED` for a policy that does not open a paper position, so on the demo path these gates had never run.
- `scripts/run_mt5_demo_trading.py`: `DemoExecutor` now **requires** those gates and calls them after the operator answers, before the command exists. Previously the last check before queueing was a local 20-point slippage bound plus geometry, stops level, volume and margin against the pre-approval quote: no quote-age or spread limit, no risk/reward at the fill, no re-sizing, no additive daily budget, no re-read identity or position book. The command now carries the fill price and the lot the gates returned, `volume_gates` re-checks that re-sized lot against the 0.20 demo ceiling and the broker's step, and the gate measurements are journalled with the command under `post_approval_gates`.
- New blocker codes `PROPOSAL_UNREADABLE` (a proposal or sizing dict that cannot be turned back into a trade is a broken input, not a missing signal) with RU/EN/VI labels, and the refusal reason when the gates pass but name neither a fill nor a lot.
- `_read_account`/`_read_positions` split into non-journaling primitives plus thin journaling wrappers, so the gates can be reused without writing a second `agent_cycle` row; journal payloads are unchanged.
- Docs `docs/MT5_AGENT.en.md`/`.md`/`.vi`: the demo gate list item 3 no longer claims spread and tick age are checked before a command is built — it now states the two reads and names the full post-approval set. Tests: 666 passed, 10 skipped (DB/Redis), up from 647 — 13 new gate cases in `tests/test_mt5_agent.py`
(moved market, re-sized lot, stale quote, widened spread, kill switch, closed window, position
appearing after the signal, loss banked after the signal, stops level at the fill, broken proposal,
real account, no journal writes) and 6 in `tests/test_mt5_demo_runner.py` (refusal before queueing,
no-fill refusal, demo cap on the re-sized lot, command carrying the gate fill, ordering after the
operator answer, and an executor that cannot even be built without the gates).
- `scripts/mt5_demo_bridge.py`: the emergency stop the runbook and `scripts/activate_kill_switch.ps1` already documented now reaches the demo path. `KillSwitchStore` keeps one durable record in `kill_switch.json` beside the tombstones (rewritten whole through a temp file plus `os.replace` and fsynced), the EA's `GET /v1/risk/kill-switch` reads it, and the bridge takes exactly one write: `POST /v1/risk/kill-switch`. Arming needs only the bridge key and answers `status: "PERSISTED"` with `updated_at` and `event_hash` — what that script checks; releasing needs a separate `MT5_KILL_SWITCH_ADMIN_TOKEN` of at least 32 characters, different from `MT5_BRIDGE_TOKEN`, in `X-Kill-Switch-Admin` (403 when wrong, 503 when unconfigured or reused), and the refusal keeps the stop armed. No other path or verb writes. A state file that cannot be read is a refusal, never a default: the GET answers 503 and the runner blocks. An arm whose disk write fails is still armed in memory (`status: "MEMORY_ONLY"`, 503), a disarm whose write fails leaves the stop armed. The file is read when a store is built, so a corrupt record is a clean localized refusal before a run or a release starts, not a surprise mid-cycle.
- Every POST body is read out of the socket before the bridge answers, bounded by 1 MiB, because a verdict written while the client is still sending is a connection reset on Windows — and an operator whose stop request dies on a reset cannot tell whether the stop happened. A body past the 4096-byte cap is drained and refused with 413; a request that is not a JSON object, has a non-boolean `active`, a non-string `reason`/`actor_id`, or no body at all, is refused with 400 and changes nothing.
- `scripts/run_mt5_demo_trading.py`: `DemoExecutor` now **requires** a kill-switch reader and reads the same file the EA reads — before any market read, and again immediately before a command is built, after the operator answer and the post-approval gates. The in-process `RunState` is gone; a `STOPPED` outcome or a served-but-unconfirmed send arms the same durable record, so finishing the process does not finish the stop; `--release-kill-switch` lifts a persisted stop between runs with the admin token from the environment. The run summary carries `kill_switch_armed` and `kill_switch_reason`.
- New trilingual keys `cli.release_kill_switch` and `runner.kill_switch_armed`/`runner.kill_switch_state_lost`/`runner.kill_switch_released`/`runner.kill_switch_release_refused`.
- Docs: `docs/MT5_AGENT.en.md`/`.md`/`.vi` gained "The emergency stop on the demo path" (the file, the two secrets, the fail-closed reads, the durable halt) and corrected bridge/runner rows; `docs/VALIDATION_RUNBOOK.md` §6.1 states the one write the bridge takes and §7 now carries the arm/release commands with where the state lives; `docs/architecture.md` draws the operator POST and the `kill_switch.json` edges inside the demo subgraph.
- Tests: 707 passed, 10 skipped (DB/Redis), up from 666 — `tests/test_mt5_demo_bridge.py` 74 (durable arm and release, cross-process durability through a restart, the second secret in both directions, memory-only arm, corrupt file as refusal at construction, note cleaning, only-the-kill-switch-path writes, drained bodies behind 400/401/403/405/413) and `tests/test_mt5_demo_runner.py` 98 (armed before any market read, a stop pulled during the operator answer, five unreadable stop states all blocking, a raising reader as armed, an executor that cannot be built without a stop reader, a bridge arm blocking the next process, the runner's own halt being durable, a policy seed landing in the file and blocking the next run, a corrupt file refusing both the run and the release, and the CLI release refusing without the admin secret).

### Round 11 re-review: the stop cannot be disarmed by deleting a file, and the run loop is now tested through

Two independent hostile reviews of the above scored it 6/10 each and found three defects that were
real: absence of the state file was served as *disarmed*, the in-process arm was sticky, and nothing
drove `DemoExecutor.offer` through the real `_run`.

- `KillSwitchStore`: **absence is not consent**. A path with no record is now served as
  `{"kill_switch_active": true, "kill_switch_reason": "NOT_SEEDED"}` instead of a disarmed default, and
  a record that this process has seen (or written) and that then disappears is
  `KILL_SWITCH_FILE_CORRUPT` — the same 503 the advisor treats as blocked. Deleting `kill_switch.json`
  cannot disarm the EA or the runner in either direction, in this process or a new one.
- The sticky `_armed` override is gone: the file is re-read on every check, so a stop released by the
  CLI or by a second bridge is obeyed by a running runner and a running EA without a restart. The only
  thing kept in memory is an **arm whose disk write failed** (`_unwritten`), which keeps blocking until
  some write lands — losing a stop the operator just pulled is worse than holding one too long.
- `scripts/run_mt5_demo_trading.py`: `_arm_stop` → `_set_stop(active=…)`, and `_run` now always leaves
  a record when none exists (seeded from the policy's `kill_switch` section) instead of writing only
  when the policy says stopped — which is also what makes a later deletion count as corruption. An
  existing record is never overwritten: a stop somebody pulled outranks a policy that forgot it.
- Transport: every handler socket is bounded by `timeout = REQUEST_TIMEOUT_SECONDS` (10 s), so a client
  that stops mid-send — the very moment the bridge now spends draining a body before it answers —
  cannot pin a handler thread.
- `scripts/activate_kill_switch.ps1` verifies the answer it documents: a 64-character `event_hash` and an
  `updated_at` must be present, and the script then performs the same `GET /v1/risk/kill-switch` the
  advisor performs and fails if that read does not report the stop armed.
- Docs: the emergency-stop sections in all three languages, `docs/VALIDATION_RUNBOOK.md` §7 and
  `docs/ROLLBACK.md` no longer claim that a release made outside a running bridge needs a restart; they
  state the unseeded/deleted-file rule, the seed-only-when-absent rule, the 1 MiB drain bound, the 10 s
  socket timeout, the outcomes that arm durably (operator "stop", unconfirmed send, duplicate id), the
  required `--policy`/`--db` flags on `--release-kill-switch`, a two-header HTTP release example, and
  where each hash log lives (database event log on the API path, `event_hash` over the four fields on
  the demo path).
- Tests: 715 passed, 10 skipped, up from 707 — `tests/test_mt5_demo_bridge.py` 78 (a record deleted
  after being known is a refusal both in-process and to the EA, an unseeded store blocks, a release
  written elsewhere is seen without a restart, a refusal waits for the bytes the request declared —
  asserted with `select()` on a raw socket mid-send — and a stalled client is dropped), and
  `tests/test_mt5_demo_runner.py` 102 (three end-to-end runs through the real bridge with an EA stand-in
  polling `/v1/execution/next` over a real socket: signal → gates → queue → serve → broker deal →
  `FILLED`; a run launched with the stop armed building no command at all, which is the mutation the
  reviewers proved used to leave 98/98 green; and a served-but-unconfirmed send leaving the durable
  armed record for the next process).

## 0.5.0 — 2026-10-31

- New demo-only execution path, deliberately kept outside the order-free `critical_consensus.mt5_agent` package. `scripts/mt5_demo_bridge.py` serves exactly two GETs (`/v1/risk/kill-switch`, `/v1/execution/next`) on `127.0.0.1` only: `X-Bridge-Key` compared in constant time against a 24–256 character `[A-Za-z0-9._~-]` token from `MT5_BRIDGE_TOKEN`, a Host allowlist, any `Origin` refused, one pending command, TTL 5–600 s, identifiers constrained to `[A-Za-z0-9._:-]{1,128}`, fsynced idempotency tombstones written before a command is served, and no logging of the token or of a command body. `scripts/run_mt5_demo_trading.py` is the only component in the repository that can produce an order command; it re-checks demo account mode, the policy's `require_demo`, the pinned login/server, a freshly re-read quote, entry geometry against the broker `stops_level`, volume inside `volume_min`/`volume_step`/`volume_max` and under `--max-volume` (0.20, the same ceiling as the advisor's own `MaxVolume` input), funded margin and slippage ≤ 20 points before queueing, reconciles against the broker's own deal history by magic `8172026` and the `CC:` comment prefix, and stops the run on a served-but-unconfirmed command until `--acknowledge-uncertain`. There is no retry path anywhere in it, and it contains no MT5 trading call.
- `scripts/build_demo_ea.py` generates `CriticalConsensusEA_Demo.mq5` by flipping exactly the one line `const bool ENABLE_DEMO_EXECUTION = false;` and refusing when the reviewed source shape no longer matches, so the repository default stays `false` and pinned by the static checker; optional `--compile` drives the installed MetaEditor and decides success from its `Result: N errors, M warnings` line because the process exit code is unreliable, and `--install-dir` copies the source and `.ex5` into the terminal. `scripts/setup_demo_terminal.py` is the only tool that touches the host terminal: dry run unless `--apply`, refuses while `terminal64.exe`/`metatester64.exe` runs (exit 3) because MT5 rewrites `Config/common.ini` on exit, requires one `[Experts]` section carrying exactly one each of `Enabled`/`WebRequest`/`WebRequestUrl`, preserves UTF-16 LE with BOM and CRLF, extends an existing URL allowlist instead of replacing it unless `--force-url`, writes through a temp file plus `os.replace` behind a timestamped backup it never overwrites, and byte-verifies the result. `scripts/operator_answers.py` is now the shared whole-line answer parser for the CLI and the runner.
- MQL5: `mql5/CriticalConsensusEA.mq5` version bumped to `1.000`, `ValidateFlatObject` given an explicit terminal `return` (the compiler does not treat `while (true)` as non-returning), and the tombstone guard corrected to the real API: `GlobalVariablesFlush()` returns `void` in build 6296, so the send is gated on both tombstones being observable via `GlobalVariableCheck()` afterwards, and the flush is called but never used as a value. `scripts/check_mql5_safety.py` now pins that: 426 invariants, 86 rejected mutations, including a new mutation that gates the send on the void flush.
- New blocker code `PROPOSAL_SYMBOL_MISMATCH` in `codes.py` — one executor owns one instrument, so a proposal for another symbol is a routing error that must never reach the bridge.
- `i18n.py`: 20 trilingual `setup.*` messages covering every terminal-preparation decision, plus `configure_console_output()`, now used by `run_mt5_agent.py`, `run_mt5_demo_trading.py` and `setup_demo_terminal.py`. Localized output previously died on this host: a piped cp1252 stream raised `UnicodeEncodeError` while printing the Russian `--help`. Streams are reconfigured to UTF-8 with `errors="replace"` (and `surrogateescape` on stdin so `да`/`tất cả` survive a legacy console), so a report can no longer abort a run that is already executing.
- A skipped symbol now names why: `run_mt5_demo_trading.py` records `RuntimeError: MT5 symbol USDJPY pays profit in JPY, not in the account currency USD` instead of a bare class name, after a live `--symbols all` run made the difference decisive for the operator. Anything that looks like a path, URL, port or credential still degrades to the class name.
- Real-host evidence, first time for this project (MetaQuotes-Demo, terminal build 6296, `MetaTrader5` 5.0.6231, 2026-10-31): the live terminal was read through `LiveMT5Backend` (account `10013404855`, balance 10.00 USD, leverage 100, demo), the demo EA compiled with **0 errors and 0 warnings** and installed as `MQL5/Experts/CriticalConsensusEA_Demo.ex5` (37780 bytes), and `Config/common.ini` carries `Enabled=1`, `WebRequest=1`, `WebRequestUrl=http://127.0.0.1:8000` with a byte-verified backup. A `--symbols all` cycle against that terminal queued **zero** commands: seven of the thirteen instruments are offered (EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD, XAGUSD, US500) and all blocked — six on `QUOTE_STALE` (last tick `2026-10-30T21:00:00Z`, market closed for the weekend; `max_quote_age_seconds` is capped at 300 so a Saturday can never pass) and US500 on `MARKET_DATA_READ_FAILED` (`MT5 returned no tick for US500`); USDJPY/USDCAD/USDCHF/EURGBP are refused because profit is paid in a currency other than the account currency and NAS100/BTCUSD are not offered by this broker. Every record says `broker_order_sent: false` and the journal chain verified. Sizing arithmetic on the same live data shows why a 10 USD demo cannot host one policy-compliant trade: a 0.5% budget is 0.05 USD while the broker's minimum lot risks 1.93–4.24 USD, i.e. `MINIMUM_LOT_EXCEEDS_RISK_BUDGET`.
- Still **NOT PROVEN** after all of that, and stated as such in every document: the advisor actually polling the bridge (attaching it to a chart is a manual GUI step, and `WebRequest()` cannot run in the Strategy Tester at all — a MetaQuotes limitation), any demo fill by the broker, and profitability. The 0.4.0 line "no `MetaTrader5` package and no `terminal64.exe` here" is superseded by the observations above.
- Docs: `docs/MT5_AGENT.en.md`/`.md`/`.vi` gained a "Demo execution over the loopback bridge" chapter (the eight gates before a command exists, the live 2026-10-31 instrument table, the balance arithmetic, the exact PowerShell sequence and the manual attach step), the evidence table now separates "compiled and installed" (PROVEN) from "polled the bridge" and "filled by the broker" (NOT PROVEN), and both READMEs state the same. `docs/VALIDATION_RUNBOOK.md` §6 records the build/install/config proof, the `GlobalVariablesFlush()` void semantics, the fact that MT5 rewrites `common.ini` on exit and that a GUI AutoTrading toggle clears the patched keys, plus a new §6.1 with the end-to-end demo commands and where the evidence chain stops.
- Tests: 647 passed, 10 skipped (DB/Redis), up from 479 — new suites `tests/test_mt5_demo_bridge.py` (50), `tests/test_mt5_demo_runner.py` (75), `tests/test_setup_demo_terminal.py` (29) and `tests/test_demo_ea_build.py` (9), plus 5 console-encoding tests in `tests/test_mt5_agent_i18n.py` (37) that spawn `--help` under `PYTHONIOENCODING=cp1252` and drive a legacy-stderr refusal in-process. Version declared `0.5.0` in `pyproject.toml`, `critical_consensus.__version__` and the FastAPI app info, kept equal by `test_contracts.py`. No PostgreSQL/Redis integration, no live-account path and no profitability claim.

## 0.4.0 — 2026-10-30

- New package `critical_consensus.mt5_agent`: a fail-closed observe/paper agent for a MetaTrader 5 terminal. `backend.py` defines an order-free `MT5Backend` protocol (`account`, `symbol_spec`, `tick`, `bars`, `positions`, `shutdown`) and the `LiveMT5Backend` adapter. There is no order-send, modify or cancel surface anywhere in the package — a test scans the package source for order-sending terms, pins every terminal call to an order-free allowlist (`symbol_select`, which adds a missing symbol to Market Watch, is the only mutating call), and checks `dir(MT5Agent)` for no `order*`/`trade*` attribute.
- `LiveMT5Backend` lazy-imports `MetaTrader5`, requires an explicit `server_utc_offset_seconds` (whole minutes, ±14h) because the terminal stamps broker wall-clock time without a timezone, and re-raises only the `initialize()` error **code** — `last_error()` text can echo connection details and is never propagated or logged. Credentials go straight to `mt5.initialize` and are not stored on the instance. The agent and `scripts/import_mt5_history.py` now share one `market_data.server_offset`/`normalize_rate_rows` reader, so a tick, a bar and a schedule window can never be interpreted against two different clocks.
- `policy.py` loads YAML with `Schema`-free strict validation: unknown top-level or section keys raise, `mode: live` is rejected, `kill_switch.active` defaults to `true`, and the default trading window is 00:00–22:00 UTC with Sunday blocked. New `execution_mode` (`confirm` by default; `auto` is refused at load unless `account.require_demo: true`) and `ui_language` (`ru` by default) are validated and normalized.
- `agent.py` runs a fixed 13-step cycle (UTC-aware clock → kill switch → account identity/equity/leverage → equity baselines and daily/drawdown limits → schedule → position caps → market data → open-book stop risk → signal → risk-reward/sizing → aggregate stop risk → execution mode → post-approval requote). Any blocker aborts before a position is opened; a rejected proposal is still journaled as a cycle. Daily risk is additive: the day's banked loss, the open-stop risk and the new position's risk must fit one budget. Equity baselines are refused when missing, non-finite, non-positive, a peak below equity, or more than 1000× current equity; sizing uses `min(equity, balance)` and price-aware margin (`volume × (tick_value / tick_size) × entry price / leverage`). The served spec, tick and newest bar must all name the policy symbol, and every bar must carry the policy timeframe; a mismatch blocks with `BAR_SYMBOL_MISMATCH`/`BAR_TIMEFRAME_MISMATCH` before sizing or the paper book. In `confirm` mode an `Approver` must return a literal `True`: no approver is `CONFIRMATION_UNAVAILABLE`, an exception is `OPERATOR_CONFIRMATION_FAILED`, anything else is `OPERATOR_DECLINED`. A fresh quote is re-read before the fill, so a market that moved while the operator decided is refused (`MARKET_DATA_READ_FAILED`, `BAR_SYMBOL_MISMATCH`, `QUOTE_FROM_THE_FUTURE`, `QUOTE_STALE`, `SPREAD_ABOVE_LIMIT`); the fill may drift at most 10% of the approved stop distance (`PRICE_MOVED_DURING_CONFIRMATION`), the reward-to-risk is recomputed and the volume is re-sized on the fill stop, never above the approved lot, and only a fresh, bounded quote reaches `PAPER_OPENED` (`PAPER_OPEN_REJECTED` when the paper book refuses the lot). Each cycle record names `execution_policy: NO_BROKER_ORDER_API` and whether its equity point landed (`equity_recorded`/`equity_record_error`).
- New `i18n.py`: every status, blocker, prompt, error and disclaimer exists in Russian, English and Vietnamese; the journal stores machine codes only, and the JSON report keeps `blocker_histogram` code-keyed while `blocker_labels` renders the selected language. The same applies to `exit_reason_histogram`/`exit_reason_labels`, whose codes include `STOP_LOSS`, `TAKE_PROFIT_GAP` and `END_OF_RUN`. Language precedence is `--language` > `MT5_AGENT_LANGUAGE` > policy `ui_language` > `ru`, with placeholder parity enforced by tests; `--help`, a refused `reset` and a rejected policy `ui_language` are localized as well. Argument errors print a localized usage prefix and error line (argparse's detail stays English) and flag abbreviations are disabled; a junk value in `--language` or `MT5_AGENT_LANGUAGE` exits 2 before the journal is created, `reset` probes an existing journal read-only so its refusal speaks that journal's language, and a `report` without an override opens (and may create) the journal to learn its language.
- `paper.py` `PaperBroker` replays bars with strict no-look-ahead `seek()`: a position opened at bar time `t` cannot see bar `t+1`, and inside one bar the stop-loss is evaluated before the take-profit. `strategy.py` emits a deterministic SMA/ATR + momentum signal whose `confidence` is capped and tagged `heuristic_score_not_calibrated_probability`. `sizing.py` derives volume from `tick_size`/`tick_value`/`volume_step` against the risk budget and free margin, refusing with `MINIMUM_LOT_EXCEEDS_RISK_BUDGET`/`MARGIN_MINIMUM_LOT_UNFUNDED` — never a silent percentage-of-account, never a widened stop.
- `journal.py` stores decisions in one append-only SQLite hash chain (`audit_events`), equity points included, with `--run-id` scoping and `report --verify-anchor`; the `run_id` is part of the hashed body, a deleted prefix is caught only against an anchor kept outside the file (`PREVIOUS_HASH_MISMATCH`, with `anchor_source` naming `provided` or `first_event_previous_hash` for the self-anchor), closing the book writes a best-effort final equity point after a trade closes (its success or error is surfaced as `closing_equity_recorded`/`closing_equity_error`), and `report()` accepts an explicit `initial_equity` or falls back to the first recorded point and never implies broker delivery (`broker_order_sent: false`). `verify_chain()` reports `EVENT_CONTENT_HASH_MISMATCH` on a content edit and `PREVIOUS_HASH_MISMATCH` on a row deleted from the middle of the chain.
- `scripts/run_mt5_agent.py` CLI: `run --source csv|live` with `--execution auto|confirm` and `--language ru|en|vi`, `report`, and `reset --confirm`; `run --fresh` also requires `--confirm`, and either refusal exits 2 without creating or modifying the journal (`reset` may open an existing journal read-only to choose the language of its refusal). A failed `report --verify-anchor` still writes the single JSON object to stdout (the result lives under `anchor_check`, while `verified_chain` always self-anchors on the run's first row), exits 3 and explains the mismatch on stderr in the selected language; a self-anchored chain that fails internal verification still exits 0, reporting `verified_chain.valid: false` with a localized warning on stderr. `--language` is accepted before or after the subcommand; `report` falls back to the language of its own `--run-id` (the newest run when no id is given) and `reset` uses the newest run's language. Answers to the `confirm` prompt match whole lines only (`y`/`n`/`a`/`q` plus the full local words), so a fragment such as Russian `в` or Vietnamese `tất` cannot be mistaken for "yes to everything". `run` and `report` write exactly one JSON object to stdout and `reset` one localized sentence; prompts, the verbose trace, the summary and the disclaimer go to stderr. The live path goes through `LiveMT5Backend` so the required offset is subtracted before freshness and window checks; it makes no trading call (`symbol_spec()` calls `symbol_select()` only when the symbol is absent from Market Watch, which adds it to the terminal's symbol list), refuses a terminal signed into a real account when `account.require_demo: true` (the default) and a login or server mismatch when `account.expected_login`/`account.expected_server` are pinned against the identity the backend reports — the terminal's under `--source live`, the fixed `1000000000`/`PAPER-LOCAL` under CSV paper, so CSV pins must stay null. The policy `symbol_spec` is required for paper execution and sizing on every source; the live terminal supplies bars and identity, not the symbol specification. The summary also carries `exit_reason_histogram` and its localized `exit_reason_labels`. Login/password are never passed on the CLI or journaled. Paper closures are journaled in the cycle they fill.
- 153 tests across `tests/test_mt5_agent.py` (62), `tests/test_mt5_agent_invariants.py` (59) and `tests/test_mt5_agent_i18n.py` (32), including stubbed-`MetaTrader5` adapter tests, subprocess CLI tests for all three languages and both execution modes, the post-approval requote bound and re-sizing, the whole-line answer rule, additive daily risk, price-aware margin, a missing bar spread falling back to the symbol spread, a mid-chain row deletion, localized argument errors, junk-language commands that create no journal, and a source-text scan with an order-free terminal-call allowlist (including the one mutating `symbol_select`) and a `dir()` check that the package exposes no order-sending surface. `--source live` against a real terminal remains **NOT PROVEN**: no `MetaTrader5` package and no `terminal64.exe` here.
- Docs: `docs/MT5_AGENT.en.md`, `docs/MT5_AGENT.md` and `docs/MT5_AGENT.vi.md` (English, Russian, Vietnamese) with a PROVEN / NOT PROVEN evidence table citing the exact tests, the 13-step gate order (including the post-approval requote), execution modes, language precedence, the sizing formula, the paper-broker rules (including the close-of-bar clock) and the Windows VPS setup; both READMEs link the three docs.
- Verification: local `pytest -q` — 479 passed, 10 skipped (DB/Redis); static MQL5 checks — 423 checks (including 85 mutation rejections). No MT5 compiler, terminal, PostgreSQL/Redis or broker execution was available; the local preview run uses synthetic seeded bars, its numbers are not a profitability estimate, and a 98% trade-success rate is neither measured nor achievable by this code.

## 0.3.1 — 2026-10-30

- The review-domain attestation now also covers `verdicts`, so an evidence-key holder cannot swap verdicts after the review service signed; `/v1/chief/review` evaluates only the attested `chief_reasoning` instead of the flat payload.
- Deeply nested payloads raise `EvidenceError` (HTTP 4xx / `EVIDENCE_PAYLOAD_NOT_CANONICALLY_HASHABLE`) instead of `RecursionError` and a 500.
- Risk caps are additive: `open_stop_risk_fraction` is added to realized daily-loss/drawdown before the prospective `risk_fraction` check, must be a JSON number in `[0, 1]`, and is measured from the current price to the stop (break-even positions count zero). Missing/null/string-typed values deny.
- Risk snapshots now carry and require account identity: `account_login` (integer `>= 1`) and `account_server` (non-blank string); a policy may pin expected values and a mismatch denies. Risk is sized at the worse of reviewed and reference entry price against `min(account_equity, account_balance)`, so an inflated equity can no longer shrink the reported fraction.
- Position aggregates are cross-checked: `position_symbols` must contain exactly `open_positions` non-blank symbols, `correlated_open_positions` may not exceed `open_positions`, a positive open-stop risk with zero positions is rejected, and each open position requires a fresh tick for its own symbol (a stale per-symbol quote denies instead of under-reporting stop risk as zero). Day-start equity is reconstructed from `balance - daily_pnl` in both directions, rejecting an inflated baseline.
- The minimum-RR check is applied unconditionally (including flat snapshots) after resolving policy, and non-finite expected/observed fractions are rejected.
- The authorization tombstone is keyed on a server-derived hash of the order parameters rather than the caller's `permission_id`, so renaming or re-issuing a permission cannot authorize the same order twice. Operator approvals must also carry `risk_snapshot_sha256` binding them to the terminal snapshot the risk gate validated.
- `evaluate_signal_bundle.py` exits `1` (single-line error, no traceback) when it cannot run, keeping `2` exclusively for "evaluated, not REQUIRE_HUMAN".
- Redis worker: parse-invalid events are dead-lettered on first delivery and only then ACKed; handler failures stay pending and are reclaimed via `XAUTOCLAIM` (`reclaim_pending_messages`) on each loop, so they are retried up to `max_attempts=3` deliveries before being dead-lettered.
- MQL5 static checker: 421 invariants / 84 mutations — demo execution is a compile-time constant (`const bool ENABLE_DEMO_EXECUTION = false`) that no input or preset can enable, the validated volume and direction are pinned to `request.volume`/`request.type`, SL/TP are bound to the request, the exact `TimeGMT()` expiry origin is required, only the two validated URL inputs exist, `JsonTrue`/`JsonFalse` polarity is exact, guards are statement-pinned before `OrderSend`, and literal short-circuit terms are rejected.
- Docs: four issuer pins in the README quick start, review attestation includes `verdicts`, `open_stop_risk_fraction` formula with `min(equity, balance)` denominator and worst-entry price, account/position invariants, CLI exit codes, and a note that Python does not auto-load `.env`.
- Verification: local `pytest -q` — 326 passed, 10 skipped (DB/Redis); static MQL5 checks — 421 invariants, 84 mutations. No MT5 compiler, terminal, PostgreSQL/Redis or broker execution was available.

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
- Human approvals now require a third, distinct `operator` signing domain (`MT5_OPERATOR_HMAC_KEY`/`MT5_OPERATOR_ISSUER`) bound to the `signal_id`, `ChiefReasoning` hash and the canonical `permission_request_sha256` of the exact approved order parameters; evidence and governance signers cannot approve a trade. Evidence exists only as a separate `user_decision_envelope`.
- The policy API recomputes `reviewer_count`, consensus and `open_challenges` from raw peer-review/challenge records instead of trusting bridge aggregates, and `/v1/permission/check` now returns 423 in favor of persisted-risk `/v1/risk/permission/check`.
- Persisted MT5 risk snapshots must reconcile `risk_value`/`risk_fraction`, daily loss and drawdown against the exact order and captured equity baselines within 1%. Risk is sized at the worse of reviewed and reference price, a reference price beyond SL/TP or slippage above 10% of the stop distance denies, `min_expected_rr` is rechecked at that conservative entry, the intraday peak must be at least the day-start equity, and day-start equity must reconstruct `balance − daily_pnl` within 1%. Symbol, tick-spec, `reference_price`, `account_balance` and `daily_pnl` gaps deny.
- The bridge API revalidates verdict metadata (`signal_id`, canonical `signal_input` hash, `created_at`/TTL) before certainty evaluation; `MT5_BRIDGE_TOKEN` may not equal any signing key and `/health` reports bridge/signing separation plus operator-domain configuration. Authorization nonces are one-time per operator, so the same nonce cannot be replayed against a second permission. MNPI is matched on whitespace-stripped, normalized substrings (`mnpi`, `MNPI_SUSPECT`, ` MNPI`), RR floors use an epsilon-tolerant comparison, and policy sample counts must be integers.
- Peer reviews, challenge records and Chief reasoning now require a fourth, distinct `review` signing domain (`MT5_REVIEW_HMAC_KEY`/`MT5_REVIEW_ISSUER`) covering `{signal_id, peer_reviews, challenge_records, chief_reasoning}`. The pipeline, `/v1/certainty/evaluate`, `/v1/chief/review`, `/v1/risk/permission/check` and the CLI verify it and reject missing, expired, foreign-key or foreign-issuer attestations (`REVIEW_*`), so an ingress-key holder can no longer mint "independent" reviews or a Chief CONFIRM. `/health` reports review verification and key/issuer distinctness.
- Authorization adds a once-per-permission tombstone (`authorization_permissions`, migration 006) on top of the per-operator nonce: re-approving the same `permission_id` with a fresh nonce is denied, and a missing durable permission store fails closed. The human review packet reports the validated terminal risk snapshot with its source, validation and reconciliation instead of echoing client-declared aggregates.
- The persisted-risk API re-runs the entire signed pipeline from `signal_bundle` instead of trusting caller-declared verdict/certainty/compliance strings, and the kill-switch admin token must differ from the bridge token and every signing key.
- MT5 risk capture accepts negative daily PnL, excludes deposit/withdrawal cashflows from trade PnL and catches them via baseline reconciliation, and requires `open_stop_risk_fraction` (including it in prospective daily-loss/drawdown caps).
- Envelope verification fails closed on non-ASCII signatures and lone-surrogate payloads instead of raising an unhandled error.
- MQL5 EA rejects non-exact loopback URLs (userinfo, query/fragment, whitespace, non-ASCII, non-numeric or out-of-range ports), requires a non-empty header-safe 24–256 character bridge token at init, and checks the `GlobalVariablesFlush()` result before `OrderSend`. The static checker pins comparison directions and thresholds (demo account mode, volume cap, slippage × point, TTL, expiry grace, FOK/IOC advertisement), strips comments and string literals before structural matching, requires the three parser slots, pinned boolean ternary types and whole guarded block bodies, rejects parenthesised dead literals and approval-flag reassignment, and rejects 73 weakening mutations across 383 invariants. A Python reference model of the flat-object parser is exercised against shared bidirectional accept/reject fixtures in `config/mql5_flat_object_cases.json` (duplicate keys, escapes, control characters, nesting, malformed numbers).
- Added PostgreSQL nonce-store and `--persist` CLI integration tests, made DB tests use unique symbols and restore the kill switch, documented migration 005 tombstone retention, and ignored generated `*.egg-info`/`build`/`dist` artifacts.
- Added `tzdata` for Windows and a UTC/`ZoneInfo` fallback for MT5 CSV timestamps.
- Verification: local `pytest -q` — 309 passed, 10 skipped (all skips require `TEST_DATABASE_URL` and/or `TEST_REDIS_URL`; no local PostgreSQL/Redis was available). Static MQL5 checks: 383 invariants verified, 73 mutations rejected; broker, MetaEditor compilation, demo execution and live-market validation were not performed.

### The four doors, closed by reading the money path rather than the prose

- `fleet/shard.py` (a broken registry is not an order to stop managing the lot): a registry that changed under the process and could not be re-parsed set `halted`, and `halted` short-circuited the whole cycle — the closer included. An opening is a promise about money written in the numbers that file holds; taking that money back out needs only the address, which the cycle proves for itself. The shard now passes `allow_entries=not halted` into `run_shard` and keeps managing its own lots, and the `config_rejected` event says so (`entries="blocked"`, plus an `exits` line). Same defect `4027e77` removed from `worker.py` after `8aa8db1` put it there. Test `test_a_shard_that_lost_its_registry_blocks_entries_and_keeps_closing`.
- `fleet/worker.py` (`--dry-run` against a real account is not a paper cycle): the paper mode reached the broker that holds the operator's money — logging in, then placing nothing, arming nothing and closing nothing. Refused before a terminal is started (`dry_run_refused`, an error result), because a paper lot on a real login is the one thing a status line must not print. Test `test_a_paper_cycle_never_connects_to_a_real_account`.
- `fleet/worker.py` (three money reads stood in front of the closer): `equity <= 0`, a real `balance <= 0` and a currency with no USD→ rate each returned `flat` before `_manage_positions` was called. A lot survives all three and the exit rule still has a price to work from — closing needs the address, not the rate — so they now sit after management, each carrying `closed=closed`, and what they gate is the day's accounting and new openings. Test `test_a_lot_is_still_closed_on_an_account_whose_money_cannot_be_read`.
- `scripts/real_bridge.py` + `scripts/real_mirror.py` (a book that did not answer is not an empty book): every caller wrote `positions_get() or []`, collapsing `None` (dropped channel — this machine measured the `(-10005, "IPC timeout")` that produces it) into `[]` (flat account). Read as empty the collapse says "the demo let every symbol go" (real money sold on a ghost), "nothing is mirrored yet" (a second real lot bought on every symbol that already has one) and "the daily stop has nothing left to prove" (the day declared halted over lots still riding on real money). `reported_positions`/`our_lots`/`book_of` now return `None`, openings answer `NO BOOK`, the daily stop answers `DAILY STOP UNPROVEN`, and exits never waited. Tests `test_a_real_book_that_never_answered_is_not_the_same_as_a_flat_one`, `test_a_halted_day_that_lost_the_book_says_the_lots_may_still_be_there`, `test_a_demo_that_stopped_answering_sells_nothing_on_real_money`, `test_a_real_side_that_stopped_answering_buys_nothing_on_real_money`.
- Verification: full suite `pytest -q -p no:cacheprovider` — 1503 passed, 10 skipped in 126,36 s; each of the six new tests fails on a copy of its module with the rule stripped out.

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
