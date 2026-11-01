# Gold: what moved it, 2020–2026 — facts, reported drivers, and what cannot be concluded

Status: working knowledge base. **Not** a trading system and **not** evidence of an edge.
The purpose is to record what actually happened, what was reported at the time, and to
separate the two from the stories told afterwards.

## Method

- Prices: Binance PAXG/USDT daily, 2 257 bars, 2020-08-28 .. 2026-11-01 (tokenised gold,
  quoted in USDT), plus MT5 XAUUSD hourly, 20 000 bars, 2023-06-06 .. 2026-10-30.
- Moves measured close-to-close; daily standard deviation over the sample: **0.98%**.
- Days with |move| ≥ 3%: **31 of 2 256** (1.4%).
- Reported drivers: contemporaneous press, searched and linked below. Where a link is
  absent, the attribution is marked as unverified.

## The largest moves and what was reported

| Date | Move | Price (PAXG) | Reported driver | Source |
|---|---|---|---|---|
| 2026-01-30 | **−10.1%** | 5 480 → 4 924 | Fed shock; gold and silver sold off together, silver −13% | [cnfin](https://www.cnfin.com/yw-lb/detail/20260202/4375195_1.html), [163](https://www.163.com/dy/article/KKHO6LRG05198CJN.html), [vietnam.vn](https://www.vietnam.vn/en/gia-vang-hom-nay-30-1-2026-quay-dau-giam-sau-9-5-trieu-dong-luong) |
| 2026-01-28 | +6.7% | 5 193 → 5 541 | sharp reversal upward two days before the crash | [cnfin](https://www.cnfin.com/dz-lb/detail/20260202/4374834_1.html) |
| 2026-02-05 | −6.6% | 5 031 → 4 700 | continuation of the same metals turmoil | [CRI](https://taiwan.cri.cn/2026-02-03/38639fa7-00e7-4b55-bf1d-2a957981f972.html) |
| 2025-10-21 | −6.1% | 4 364 → 4 098 | biggest one-day fall in 12 years after a record run | [Yonhap](https://www.yna.co.kr/amp/view/AKR20251022000652072) |
| 2025-10-16 | +4.7% | 4 238 → 4 437 | record high on geopolitical fears | [Nhan Dan](https://en.nhandan.vn/infographic-gold-price-surges-sharply-on-october-16-post154127.html), [baotintuc](https://baotintuc.vn/thi-truong-tien-te/gia-vang-the-gioi-tiep-tuc-pha-nguong-4300-usdounce-20251017073708039.htm) |
| 2026-08-05 | +5.0% | 4 069 → 4 272 | not verified here | — |
| 2024-04-13 | +4.1% | 2 355 → 2 452 | geopolitical escalation in the Middle East (widely reported at the time) | — |
| 2020-11-09 | −4.5% | 1 968 → 1 880 | vaccine efficacy announcement, risk-on rotation | — |
| 2021-01-08 | −4.3% | 1 955 → 1 872 | yields jumped after the US Senate run-off | — |

## The four driver classes that recur

1. **Monetary policy surprises.** Fed communication or data that moves real yields.
   Both 2025-10 and 2026-01/02 episodes sit here, with a *reversal* pattern: a record
   run, then the sharpest fall in years. The reported cause is the same each time
   (policy expectations), but the sign of the reaction depends on positioning.
2. **Geopolitics.** Escalation → bid for gold (2024-04, 2025-10-16); de-escalation or
   a risk-on shock → sell (2020-11-09 vaccine).
3. **Positioning and liquidation at records.** After a parabolic run, the largest moves
   are *down*, and they are larger than the up moves (2025-10-21 −6.1% after a record;
   2026-01-30 −10.1% after +6.7% two days earlier). Crowded longs exit through a
   narrower door than they entered.
4. **Dollar and real yields.** The common factor behind 2020-11 and 2021-01; the US
   ten-year real yield is the single most cited mechanical link.

## What this data cannot tell you

- **Attribution is not causation.** Every entry above is a narrative written after the
  fact: the same Fed surprise has produced up days and down days. No counterfactual
  exists, so "cause" here means "what reporters said at the time", nothing more.
- **Survivorship of stories.** We looked at the biggest moves and then found stories for
  them. Nobody records the hundreds of days when the same news produced no move at all,
  which is exactly the sample a test needs.
- **Direction is not tradable.** Knowing that geopolitical escalation lifts gold does not
  say when, how far, or whether the move survives costs: daily sd is 0.98%, a 2R stop is
  several times that, and the earlier studies in this repository found no rule family
  with a positive mean R after costs on this same data.

## What would turn this into knowledge that can be tested

An event study, not a story:

1. Build a timestamped event table (CPI, FOMC, NFP, geopolitical headlines) with the
   exact publication minute — not the date.
2. Define the event window before looking at returns (e.g., −30 min to +4 h).
3. Measure the abnormal return against a baseline of the same hour on non-event days.
4. Report the distribution, not one example, and the count of events that produced no
   move.
5. Only then ask whether any of it survives the cost of a trade.

Until that exists, this file is a documented memory of what was reported, not a source
of signals. Treating it as one would repeat the mistake this program has already made
three times: finding a story and calling it an edge.
