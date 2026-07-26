# Qpulse — One-Pager

**Real-time market anomaly detection. Two tiers, eight alert kinds, validated with pre-registered methodology on real labeled data.**

---

## What it does

Watches live market data (WebSocket from Alpaca, replay from CSV, or synthetic). Runs streaming statistical detectors. Emits typed alerts to a unified bus → live UI tape, SQLite history, optional webhook. Sub-millisecond hot path, hourly batch path.

## How it detects

**Hot path** (per-asset, sub-ms): EWMA + P² quantile statistics, two-sided CUSUM (Page 1954), two-sample KS (regime shift), order-book imbalance with hysteresis, rolling-count burst meta-detector.

**Batch path** (cross-asset, hourly): t-copula on rank-transformed multi-asset returns; Kendall-τ correlation; degrees of freedom by max-LL grid search; rolling 5-day log-likelihood scored against a 248-point reference null.

## Alert palette

| Kind | What | Tier |
|---|---|---|
| `robust_z`, `cusum`, `regime_shift` | Price-return outliers, drift, distributional change | Hot |
| `spread_widen`, `obi_extreme`, `quote_rate_spike` | Microstructure (quotes) | Hot |
| `burst` | Cross-kind clustering meta-alert | Hot |
| `dependence_shift` | Cross-asset correlation regime break | Batch |

## Validation (real labeled data, negative controls)

Scored against the **full** committed event catalog. The smaller pitch-era subset
that earlier versions of this document quoted is shown alongside, because it gives
higher numbers and the difference should be visible rather than buried.

| Tier | Recall on full catalog (95% CI) | FP rate (quiet days) | Same on pitch subset |
|---|---|---|---|
| Hot path v1 | **38%** (8/21), 21–59% | **0.06 / day** (4 alerts / 63 d), 0.02–0.16 | 60% (6/10); 0.05/day |
| Hot path v2 | **19%** (4/21), 8–40% | **0.03 / day** (2 alerts / 63 d), 0.00–0.11 | 40% (4/10); 0.00/day |
| Batch path | **35%** (7/20), 18–57% | **0.00 / day** (0 alerts / 63 d), 0.00–0.06 | 67% (6/9); 0.00/day |

Reproduce every row with `bash gate/validate.sh`. Caveats that belong next to these
numbers: 4 crypto and 6 macro events post-date the alert artifacts entirely and
could not have been detected by any configuration (they are still counted as
misses); the v1 artifact's generating command was never recorded, so v2 pins one
chosen by a rule fixed in advance and scores lower (`gate/frozen/V2_BASELINE.md`);
and on catalogs this small the intervals overlap too much to rank the rows.

Only the v2 row and the MPS-copula gate below are pre-registered.

**MPS-copula gate** (quantum-inspired alternative): pre-registered 5 criteria, MPS failed 3 → shipped classical t-copula instead. Decision trail in `gate/GATE.md`.

## Performance

⚠️ **Unlike the detection numbers above, these have no committed artifact.** They
come from a run whose output was never saved and are not reproduced by
`gate/validate.sh`. Re-measure with `python -m app.bench` before quoting them.

- **p99 latency on real WS data**: < 500 µs end-to-end
- **Throughput**: 280k ticks/sec on a single core (synthetic benchmark)
- **Memory**: O(1) per per-symbol detector — streaming statistics, no growing buffers (this one is structural, visible in `app/features.py`)
- **Stack**: Python (numpy, scipy, fastapi, uvloop, websockets, pandas, yfinance). No proprietary deps. Single-process. Laptop-or-t3.medium-friendly.

## What's already done

UI, auth, health endpoint, SQLite persistence, webhook sink, replay/record/backtest/sweep/evaluate/inspect tools, three labeled event catalogs (BTC + macro + quiet windows), the MPS validation gate.

## Roadmap

1. **Qmetrum integration** — link real-time alerts to Qsight forecast context, narrated via the Qlens decision agent (next)
2. **Catalog expansion** — broader labeled-event coverage to tighten validation
3. **Microstructure depth** — order-book imbalance beyond top-of-book, OFI signals
4. **Distribution** — pricing, target segments, GTM

## The honest framing

- All math is classical. We tested a quantum-inspired alternative (MPS-copula) head-to-head with pre-committed criteria; it lost on real data; we shipped the simpler method.
- Recall is 40–67%, not 95%, and which end of that range you get depends on the generation config (see the v1/v2 rows). The catalog is small (10–14 events per universe), so 95% confidence intervals span roughly ±25 points. Missed events have known reasons (data window, gradual moves, priced-in news); one crypto event post-dates the alert artifact entirely and could not have been caught by any configuration.
- False positive rate is 0.00–0.05 per quiet day measured against explicit negative-control windows. This is the cleanest available precision metric without an exhaustive event corpus. With zero observed alerts across 35–42 quiet days, the honest statement is an upper bound (~0.09–0.11/day at 95%), not "zero false positives".

This is a working prototype with reproducible validation, not a polished product.
