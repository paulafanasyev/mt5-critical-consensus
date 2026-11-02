# Which broker unlocks which strategy

The current venue (MetaQuotes-Demo) has gold, FX and index CFDs. That is exactly the set
where our tested rules lose: the costs are 0.33R per trade against a gross signal of
0.03R, and the three anomalies that survived statistics need instruments this venue does
not have. This note says which account would unlock which test, in order of effort.

## The three anomalies and what each needs

| Anomaly | Evidence | Venue needed | Available at MetaQuotes? |
|---|---|---|---|
| Volatility premium (DVOL) | BTC +8.78 vol pts, 95% CI +8.24..+9.31 | crypto options (Deribit) | no |
| China overnight (short) | Shanghai −0.085%/day, t = −7.1 | CFFEX index futures | no |
| US/gold overnight drift | SPY +0.037%/day t=2.6; gold futures +0.044% t=5.0 | futures with real sessions | no (CFD has no session gap) |
| Funding carry | +4.9%/yr, costs 0.31% once | Bybit/OKX (already have) | works, paper running |

## Candidate accounts

### 1. Deribit — crypto options (fastest, no funding needed)

- What it unlocks: selling 30-day variance/strangles, the DVOL premium, and the
  option-quote reality check (bid/ask, not just the index).
- Access: public market data needs no account (already used). Trading needs registration
  and, for real money, KYC. A **testnet** account (`test.deribit.com`) gives free paper
  funds and a full API with the same endpoints.
- Effort: minutes for a testnet account; real account later.
- First test: snapshot the option chain hourly (running now), then simulate a 30-delta
  strangle sold at the quoted bid and settled at realised volatility.

### 2. Interactive Brokers — global futures and options

- What it unlocks: GC (gold futures), ES (S&P futures), real session boundaries, real
  commissions, a paper account with the same API.
- Access: account approval takes days; paper account is free after approval.
- Effort: medium (KYC, funding not required for paper).
- First test: the overnight drift in GC and ES with genuine futures costs (about 0.02% per
  round trip), which is the assumption that decides whether the drift is tradable.

### 3. Alpaca — US stocks and options, paper API

- What it unlocks: fast signup, paper trading with an API key, US equities and options.
- Effort: low (minutes).
- Limitation: no futures, no crypto options; cannot test either of the two strongest
  anomalies.

### 4. Chinese futures (CFFEX)

- What it unlocks: the strongest anomaly (Shanghai overnight, t = −7.1).
- Access: a domestic Chinese account is required in practice; not reachable from this
  setup.
- Verdict: skip until an account exists.

## Recommendation

1. **Deribit testnet first.** It is free, fast, and the quote data is already public, so
   the premium can be paper-tested without risking anything.
2. **IBKR paper second.** It is the only way to test the futures version of the overnight
   drift with realistic costs; approval takes days, so start it early.
3. Skip Alpaca and CFFEX for now: neither unlocks anything the first two do not.

## What is already happening without any account

- `scripts/deribit_options_collector.py` snapshots the full BTC and ETH option chain every
  hour (bid, ask, mark, implied vol per strike). After a few weeks this is the dataset for
  an honest premium test at quoted prices.
- `scripts/funding_carry_live.py` keeps the paper funding position accruing.
- `scripts/scenarios_paper.py` keeps the nine-scenario board with its halt guards.

## What to bring to me

For Deribit: a **testnet** API key (client id and secret) with trade permission on testnet
only. For IBKR: confirmation that a paper account exists; the connection then uses the
local TWS gateway.
