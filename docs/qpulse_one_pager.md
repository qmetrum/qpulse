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

## Validation (pre-registered, real labeled data)

| Tier | Universe | Recall | FP rate (quiet days) |
|---|---|---|---|
| Hot path | BTC daily, 10 crypto crises | **60%** | **0.05 / day** |
| Batch path | 8-asset macro basket, 9 macro crises | **67%** | **0.00 / day** |

**MPS-copula gate** (quantum-inspired alternative): pre-registered 5 criteria, MPS failed 3 → shipped classical t-copula instead. Decision trail in `gate/GATE.md`.

## Performance

- **p99 latency on real WS data**: < 500 µs end-to-end
- **Throughput**: 280k ticks/sec on a single core (synthetic benchmark)
- **Memory**: O(1) per per-symbol detector — streaming statistics, no growing buffers
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
- Recall is 60–67%, not 95%. The catalog is small (10–14 events per universe). Missed events have known reasons (data window, gradual moves, priced-in news).
- False positive rate is 0.00–0.05 per quiet day measured against explicit negative-control windows. This is the cleanest available precision metric without an exhaustive event corpus.

This is a working prototype with reproducible validation, not a polished product.
