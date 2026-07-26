# Qpulse

**Real-time market anomaly detection.** Qpulse watches live market data (Alpaca WebSocket, CSV replay, or a synthetic feed), runs streaming statistical detectors on a sub-millisecond per-tick hot path plus an hourly cross-asset batch path, and emits typed alerts to a unified bus that feeds a live web UI, SQLite history, and an optional webhook. It is part of the Qmetrum product line, alongside Qsight (forecasting) and Qlens (decisions). This is a working prototype with reproducible, pre-registered validation — see [Validation](#validation).

## Architecture

```
 feeds: Alpaca WS │ CSV replay │ synthetic
          │
          ▼
   Router → PerSymbolDetector          HOT PATH — per tick, per symbol
          │
          │            DependenceShiftDetector   BATCH PATH — hourly score,
          │                    │                 daily refit (opt-in)
          ▼                    ▼
        Bus  ──►  live UI tape (/ws/alerts) · SQLite sink · webhook sink
```

**Hot path** (`app/detector.py`, `app/features.py`) — O(1)-memory streaming statistics per symbol:

- EWMA mean + MAD baselines and P² streaming quantiles (Jain & Chlamtac, 1985) for robust z-scores on returns, bid-ask spread, and quote-arrival rate → `robust_z`, `spread_widen`, `quote_rate_spike`
- Two-sided CUSUM (Page, 1954) for cumulative drift → `cusum`
- Two-sample KS test, short window vs. strided reference, checked every N ticks → `regime_shift`
- Order-book imbalance with hysteresis (fires at an extreme threshold, re-arms only after the book crosses back past a lower one) → `obi_extreme`
- Burst meta-detector: rolling cross-kind alert count vs. an EWMA baseline → `burst`

**Batch path** (`app/batch/dependence_shift.py`) — a t-copula fit on rank-transformed daily log-returns of a configurable asset universe (default `XLK XLF XLE XLV SPY TLT GLD HYG`): correlation via Kendall τ, degrees of freedom by max-likelihood grid search, rolling 5-day log-likelihood scored hourly against a null built on the 252-day reference window, refit daily via yfinance → `dependence_shift`. Opt-in via `HFT_BATCH_ENABLED=true`. The method choice (classical t-copula over the quantum-inspired MPS-copula) came out of a pre-registered gate — decision trail in `gate/GATE.md`.

All alerts flow through one bus to the same sinks: WebSocket UI tape, `/alerts/recent` and `/alerts/history` endpoints, SQLite persistence, and an optional webhook with a minimum-score filter. The FastAPI service also exposes `/health` (unauthenticated feed-liveness probe), runtime config endpoints (`/config`, `/config/threshold`, `/config/symbols`, `/config/feed`), and `/ws/stats`; setting `HFT_API_TOKEN` enables Bearer-token auth on everything except health/UI routes.

## Repository layout

| Path | Purpose |
|---|---|
| `services/hft_anomaly_service/` | The Qpulse service: FastAPI app, hot-path detectors, feeds, sinks, batch path, and CLI tools (backtest, evaluate, sweep, bench, record, inspect; CSV replay runs via `app.live --source csv`) |
| `gate/` | Pre-registered MPS-copula vs. t-copula validation gate: scripts, cached data, and `results/` (verdict, scores, timing) |
| `events/` | Labeled event catalogs (CSV) across crypto, equities, macro, rates, FX, commodities, and volatility |
| `configs/` | Detector tuning profiles (`synthetic`, `minute_bars`, `ticks`, `crypto`) as `.env` files |
| `deploy/` | VPS deployment: runbook, `install.sh`, `sync.sh`, systemd unit, Caddyfile |
| `docs/` | One-pager and pitch deck |
| `docker-compose.yml` | Local compose stack for the service |
| `.env.example` | Template for the auto-loaded `.env` (keys, source, persistence, optional auth/webhook) |

Note there are two `gate/` trees by design: root `gate/` is the Stage-1 MPS-vs-t-copula research gate (run as `python -m gate.copula_gate` from the repo root), while `services/hft_anomaly_service/gate/` is the later pre-registered `tn_anomaly` gate (run from inside `services/hft_anomaly_service`). Each runner documents its required working directory.

## Quickstart

Python 3.11 (matches the Docker image).

### Local

```bash
cd services/hft_anomaly_service
pip install -r requirements.txt

# Synthetic feed — no keys needed. UI at http://localhost:8080/
python -m app.live

# Live Alpaca feed (paper keys — see Configuration)
python -m app.live --source alpaca

# Replay a recorded CSV (columns: ts_ns,symbol,price) at 60x speed
python -m app.live --source csv --csv-path ticks.csv --speed 60
```

The cross-asset batch path is opt-in: set `HFT_BATCH_ENABLED=true`.

Offline tools (same directory):

```bash
python -m app.bench                                  # latency/throughput benchmark on synthetic ticks
python -m app.record --out ticks.csv                 # record a live feed to a replay-compatible CSV
python -m app.backtest --input ticks.csv --alerts-out alerts.csv
python -m app.evaluate --alerts alerts.csv --events ../../events/crypto_canonical.csv
python -m app.sweep --input ticks.csv                # z/CUSUM threshold grid sweep
python -m app.inspect_alerts                         # summarize alerts from a running instance
```

### Docker

From the repo root:

```bash
docker compose up --build hft                    # synthetic profile (default), UI at http://localhost:8080/
HFT_PROFILE=minute_bars docker compose up hft    # switch detector tuning profile
docker compose run --rm hft python -m app.bench  # one-off latency benchmark
```

## Configuration

**Secrets and runtime settings** live in `.env` at the repo root. Copy `.env.example` to `.env` and fill in your Alpaca paper keys. The live service and recorder entry points auto-load it (searching the current directory and up to two parents, so it works from the root or from `services/hft_anomaly_service/`); real shell environment variables always win. The offline tools (backtest, evaluate, sweep, bench, inspect) take their inputs from CLI flags and do not read `.env`. `.env` is gitignored. Main variables: `ALPACA_API_KEY`/`ALPACA_API_SECRET`, `HFT_SOURCE` (`synthetic` | `csv` | `alpaca`), `ALPACA_FEED` (`crypto` | `iex` | `sip`), `HFT_SYMBOLS`, `HFT_DB_PATH`, and optional `HFT_API_TOKEN`, `HFT_WEBHOOK_URL`, `HFT_WEBHOOK_MIN_SCORE`.

**Detector tuning** lives in `configs/*.env` profiles, selected in Docker via `HFT_PROFILE` (compose loads `configs/${HFT_PROFILE:-synthetic}.env`); for local runs, export the same variables or put them in `.env`:

| Profile | Intended data | Status |
|---|---|---|
| `synthetic` | GBM synthetic feed (default) | Conservative generic settings |
| `minute_bars` | 1-minute bars | Grid-sweep validated (knee at z=4.5, h=8.0) |
| `ticks` | Sub-second ticks | Placeholder — explicitly not validated; re-tune with `app.sweep` |
| `crypto` | Alpaca crypto WS (24/7, quote-driven) | Tuned on a live BTC/ETH run; adds OBI/quote-rate/warmup settings |

The batch path is configured with `HFT_BATCH_ENABLED`, `HFT_BATCH_UNIVERSE`, `HFT_BATCH_REF_DAYS`, `HFT_BATCH_ROLL`, `HFT_BATCH_REFIT_SEC`, `HFT_BATCH_SCORE_SEC`, and `HFT_BATCH_Z_THRESH`. Config precedence at runtime: env vars < persisted config file < live API changes.

## Validation

All numbers below are sourced from files in this repo.

| Result | Numbers | Source |
|---|---|---|
| Hot path — BTC daily, 10 labeled crypto crises + 6 quiet windows | Recall 60% (6/10), median time-to-detect 0 ms, false positives 0.05/day on quiet windows | `docs/qpulse_one_pager.md`, `docs/qpulse_pitch.md` |
| Batch path — 8-asset macro basket, 9 labeled macro events + 5 quiet windows | Recall 67% (6/9), false positives 0.00/day on quiet windows | `docs/qpulse_one_pager.md`, `docs/qpulse_pitch.md` |
| Performance | p99 end-to-end latency < 500 µs on real Alpaca WS data; 280k ticks/sec single-core synthetic | `docs/qpulse_one_pager.md` |
| MPS-copula gate (Stage 1) | MPS failed 3 of 5 pre-registered criteria (beat Gaussian on only 1/3 events, failed timing, more spurious than t-copula on quiet windows) → the classical t-copula shipped as `dependence_shift` | `gate/results/verdict.md`, `gate/GATE.md`; raw scores in `gate/results/scores.csv`, `gate/results/timing.csv` |
| tn_anomaly (MPS Born-machine) gate | REJECT — passed corroboration (74.3%), lift (96.3%), and compute budget (19.2 s), but failed quiet-window FP rate (0.73–0.90/day vs. ≤ 0.10) and fit stability (KL ≈ 2.79 nats vs. ≤ 0.5); not integrated | `services/hft_anomaly_service/gate/results/tn_anomaly_verdict.md`, spec in `services/hft_anomaly_service/docs/tn_anomaly_gate.md` |

Honest framing (from the one-pager): all shipped math is classical; recall is measured against small event catalogs (10 and 9 events), and the FP rate is measured against explicit negative-control quiet windows.

## Deployment

For running Qpulse on a VPS (single EC2 instance, systemd unit, Caddy reverse proxy with automatic Let's Encrypt HTTPS, `sync.sh` code pushes), see **[`deploy/README.md`](deploy/README.md)**.
