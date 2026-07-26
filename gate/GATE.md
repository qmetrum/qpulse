# MPS-Copula Gate — Stage 1 Plan

> **OUTCOME (2026-04-23): FAIL.** MPS missed 3 of 5 pre-committed criteria
> (see `results/verdict.md` for numbers). Classical t-copula was stronger
> on 2 of 3 crisis events and cleaner on quiet windows. Qpulse ships the
> classical `dependence_shift` detector (`app/batch/dependence_shift.py`),
> not MPS. This folder is an archive of the decision trail.


**Purpose:** decide whether MPS-copula ships in Qpulse's batch cross-asset detection path.

**Decision:** if MPS materially beats t-copula on known dependence-break events without spurious firing on quiet windows, ship MPS. If marginal or tied, ship t-copula. If neither works, drop the cross-asset feature.

## Revised parameters (constrained by dense-tensor infeasibility at N > 8)

| Parameter | Value | Reason |
|---|---|---|
| Assets (Stage 1) | **8** (per event) | Dense joint tensor infeasible at N=16 with existing code |
| Bins (d) | **4** (quantile-based) | 32 bins × 252 samples = ~8/bin; 4 is statistically reasonable |
| Bond dim (χ) | **{2, 4, 8}** | χ=1 is independence, χ=8 is full rank at d=4 |
| Reference window | 252 trading days ending 30d pre-event | Buffer prevents leakage |
| Evaluation window | 20 trading days from onset | Captures pre- and through-peak |
| Scoring | 5-day rolling log-likelihood | Reduces single-day noise |

## Asset universes

**Core-8 (COVID, SVB)** — sectors + macro hedges:
```
XLK   XLF   XLE   XLV   (4 sectors: tech, financials, energy, health)
SPY   TLT   GLD   HYG   (4 macro: equity, rates, gold, credit)
```

**Crypto-8 (Terra/Luna)** — swaps 2 sectors for crypto:
```
XLK   XLF                (2 sectors most exposed to crypto correlation)
SPY   TLT   GLD   HYG   (4 macro)
BTC-USD   ETH-USD       (2 crypto)
```

## Events and quiet windows

| Name | Type | Period | Notes |
|---|---|---|---|
| COVID | Crisis | 2019-01-02 → 2020-03-20 | Onset 2020-02-20 |
| Terra/Luna | Crisis | 2021-04-01 → 2022-06-06 | Onset 2022-05-09 |
| SVB | Crisis | 2022-02-01 → 2023-04-05 | Onset 2023-03-08 |
| Q-NC-1 | Quiet | 2019-10-01 → 2019-10-28 | Pre-COVID, stable growth |
| Q-NC-2 | Quiet | 2021-08-02 → 2021-08-27 | Mid-summer, no majors |
| Q-NC-3 | Quiet | 2022-11-01 → 2022-11-28 | Pre-SVB, post-FTX dust settled |

Each gets: reference window of 252d ending 30d before evaluation window start; evaluation window of 20 trading days.

## Methods tested (apples-to-apples)

1. **Gaussian copula** — correlation via Kendall tau, ppf = norm.ppf
2. **t-copula** — same correlation; df fit by max LL over {3, 5, 10, 20}
3. **MPS-copula** — joint-tensor → truncated MPS at χ ∈ {2, 4, 8}

All methods trained on the same reference returns with the **same bin edges** (quantile-based from reference period, used uniformly for eval & null).

## Scoring protocol

1. **Null distribution**: for each method, compute rolling 5-day LL across all 248 overlapping slices of the 252-day reference period. These form the null.
2. **Eval scoring**: compute rolling 5-day LL for each of the 16 slices in the 20-day eval window. Z-score against the method's null.
3. **Peak z-score**: the most negative (biggest LL drop) over the eval window.

## Pass criteria — pre-committed before running

### Detection (full pass, all required)

1. **MPS peak-z magnitude ≥ 1.5 × Gaussian peak-z magnitude** on at least 2 of 3 crisis events.
2. **MPS peak z-score timing** precedes or coincides with the market drawdown peak by ≤ 2 trading days on those events.
3. **MPS peak-z magnitude > t-copula peak-z magnitude** on at least 1 of 3 crisis events.

At least one bond dim (χ ∈ {2, 4, 8}) must satisfy criteria 1–3.

### Negative controls (pass required)

4. **MPS exceeds the median-crisis peak-z threshold on at most 1 of 3 quiet windows.**
5. **MPS does not spuriously fire more often than t-copula across the quiet set.**

Failing 4 or 5 inverts ship/no-ship regardless of detection performance.

## Outcomes

| Result | Action |
|---|---|
| Full pass (all 5 criteria) | Ship MPS-copula as `copula_break` alert in Qpulse |
| Partial pass (beats Gaussian on ≥2 events, misses others) | Ship **t-copula** as "dependence monitoring" |
| Fail (no consistent signal OR negative control breach) | Drop cross-asset feature for v1; revisit with better method later |

## Outputs

- `gate/data/*.csv` — cached price/return data
- `gate/results/scores.csv` — z-scores per method × event × day
- `gate/results/verdict.md` — pass/fail per criterion + decision
- `gate/results/timing.csv` — peak z-score timing per method × event
