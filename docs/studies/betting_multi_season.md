# Betting sandbox: the same model across five seasons and three divisions

The parked sandbox's 2024-25 report showed +5.48% ROI on Kelly staking. One season is not
evidence, so the walk-forward was re-run on every season and division that football-data.co.uk
serves in the same format. Data was downloaded to `agent_data/betting_cache/` and every run
used `--offline`, so no result depends on a live fetch.

## Results (Kelly staking, model-Poisson vs Bet365/Pinnacle odds, no commission)

| Season | Division | Bets | ROI | Mean CLV |
|---|---|---|---|---|
| 2020-21 | E0 (PL) | 249 | **+19.77%** | +1.00% |
| 2021-22 | E0 | 238 | **−28.94%** | +0.56% |
| 2022-23 | E0 | 225 | −0.27% | +0.78% |
| 2023-24 | E0 | 232 | **−19.74%** | −0.99% |
| 2024-25 | E0 | 250 | **+5.48%** | +0.03% |
| 2023-24 | E1 (Championship) | 371 | **−20.06%** | +0.24% |
| 2024-25 | E1 | 358 | −9.46% | +0.04% |
| 2023-24 | SP1 (La Liga) | 246 | **−23.85%** | −1.05% |
| 2024-25 | SP1 | 246 | −18.29% | −0.18% |

**2 of 9 positive; mean ROI across the nine ≈ −10.5%.**

## What the table says

1. **The 2024-25 +5.48% is a best-of-nine artifact.** The same model on the same league a
   year earlier loses 19.7%; in the Championship and La Liga it loses 9–24%.
2. **Flat staking on 2024-25 loses 0.57% on the identical bets**, so even the one positive
   season is a staking-size accident, not a selection edge.
3. **CLV does not track ROI.** 2021-22 has +0.56% CLV and −28.9% ROI; 2023-24 has −0.99%
   CLV and −19.7% ROI. Over these samples CLV is noise, so it cannot be used as the
   "we have an edge" certificate the sandbox's roadmap hoped for.
4. **No commission is modelled.** Real bookmakers apply margin and stake limits; adding
   them only moves every cell further down.

## What would change the verdict

- A calibration stage (Platt/isotonic) fitted only on past dates, reported with a
  reliability diagram, and a Brier score that beats the de-vigged market on every season.
- A multi-league prior rather than one Poisson fit per division.
- Commission, stake limits and a minimum-CLV sample rule, declared before the run.
- A single pre-registered season-by-season rule: positive ROI net of commission in at
  least 4 of 5 seasons, with a bootstrap interval above zero pooled across seasons.

None of that exists today, and the current evidence points the other way.

## Bottom line

The betting sandbox is honestly built (walk-forward, de-vig, no martingale) and still has
no edge: nine season/division samples, two positive, mean −10.5%. It joins gold, FX,
indices and crypto in the same result.
