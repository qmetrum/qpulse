# Qpulse Roadmap — Post-Publication Hardening & First Integration

**Scope.** One sequenced roadmap merging the three workstream designs (detector changes, catalog expansion & validation-tightening, Qsight integration) with every MAJOR adversarial-critique finding fixed in the plan itself and minors folded in where cheap. Repos: Qpulse at this repository, Qsight monorepo at the Qsight monorepo. Cost posture: scale-to-zero — no phase introduces always-on infrastructure. Honesty rule: every number that reaches docs or UI comes from a committed JSON artifact produced by a pre-registered run; where a metric does not exist yet it is written **to be measured**.

## Executive summary

The published pitch claims (60.0% recall / 0.05 FP-day hot path; 66.7% / 0.00 batch) reproduce exactly on the local machine but **not** from the just-published public repo: the evaluation inputs are gitignored, the equities catalog crashes `load_events()` (7-field rows against a 6-column header), and no recorded command regenerates the pitch alert files (current backtest defaults yield 0 alerts on daily bars). P0 repairs all of this in ~2 sessions and pins a fresh-clone-reproducible baseline. Qsight integration is structurally independent of all validation work and costs $0/month incremental, so it lands immediately after (P1) for maximum visible momentum: Qpulse alerts on the live dashboard, honestly labeled. Detector validation is calendar-bound (scheduled macro releases, ~2-week recording campaigns), so its enabler — the quote-capable capture/replay pipeline (P2) — comes next to start the clock early; catalog expansion (P3–P6) is pure desk work that fills the recording/event wall-clock, growing 92 → ~162 positives and 231 → ~924 quiet days under a pre-registered gate. New detector claims pass pre-registered gates or are published as failures. Core path: ~20–26 working sessions plus 4–8 weeks of passive wall-clock; total incremental infrastructure cost: $0 (one optional hourly-billed recording VPS window, torn down after use).

## Phase table

| Phase | Goal | Effort (sessions) | Depends on |
|---|---|---|---|
| **P0** Reproducibility pin & repo repair | Pitch numbers reproduce from a fresh clone; all catalogs load; generation gap honestly documented; v2 baseline pinned | 2 | — |
| **P1** Qsight integration MVP | Qpulse alerts on the Qsight dashboard, honestly labeled, $0 infra | 2 | P0 |
| **P2** Quote-capable capture/replay (v2 pipeline) | Record + replay trades **and** quotes; unblock every quote-side detector gate | 2–3 | P0 |
| **P3** Catalog hygiene & pre-registered expansion gate | Machine-linted catalogs with provenance; `CATALOG_GATE.md` committed before any expansion run | 2–3 | P0 |
| **P4** Data plumbing & one-command eval loop | One command pulls all data and runs the full per-class eval matrix | 3–4 | P3 |
| **P5** Recording campaign & new-detector gates | volume_spike / microprice_drift / OFI validated (or honestly failed) at tick horizon | 3–4 + 4–8 wks calendar | P2 |
| **P6** Catalog sourcing sprints | Positives 92 → ~162, quiet days 231 → ~924; first-ever runs for fx/rates/vol/commodities | 3–4 | P3, P4 |
| **P7** Ticks profile validation | `configs/ticks.env` PLACEHOLDER → sweep-derived, provenance-stamped | 2 + 3 market days wall-clock | P0 (P2 helps) |
| **P8** Claims & report update | Docs/pitch/UI cite only committed artifacts; expanded claim matrix | 1–2 | P5 and/or P6 outputs |
| **P9** (optional) Crypto L2 depth | Probe-gated order-book detector; explicit sign-off gate on any non-Alpaca fallback | 0.5–1 (+3–4 if go) | P2, P5 |

Dependency sketch:

```
P0 ─► P1  (integration — fastest visible win)
P0 ─► P2 ─► P5 (calendar-bound — start its clock early) ──► P8
P0 ─► P3 ─► P4 ─► P6 ──────────────────────────────────────► P8
P0 ─► P7 (independent filler during P5/P6 waits)   P2 ─► P9 (optional)
```

---

## P0 — Reproducibility pin & repo repair (2 sessions, no dependencies)

**Goal.** `bash gate/validate.sh` reproduces 60.0% / 0.05 (hot) and 66.7% / 0.00 (batch) **from a fresh clone of github.com/qmetrum/qpulse**; every catalog loads; the broken data→alerts half of the chain is documented honestly and replaced with a newly pinned, committed v2 generation command.

This phase merges both workstreams' M0s and fixes five verified defects the original M0s would have carried forward: (1) the eval inputs are gitignored and non-regenerable; (2) the equities quiet rows have 7 fields, not swapped columns — `load_events()` crashes on `float('quiet')`; (3) no known command regenerates `btc_alerts.csv` (defaults → 0 alerts; best grid attempt 48 vs 94); (4) the `--kinds cusum,spread_widen` question is already decided — the filterless eval reproduces 60.0%/0.05, the filtered one yields 40.0%/0.02, and `btc_alerts.csv` contains zero `spread_widen` alerts (55 robust_z + 39 cusum); (5) new sweep/backtest flags must default to current behavior or they invalidate `minute_bars.env` provenance.

**Tasks**
1. **Commit the frozen inputs.** Copy `gate/data/btc_alerts.csv` (~5 KB), `core_dep_alerts.csv` (~4 KB), `btc_replay.csv` (~65 KB), `spy_replay.csv` (~45 KB) into tracked `gate/frozen/` as `*_pitch_v1.csv` (or add `.gitignore` exceptions); record sha256 of each.
2. **Freeze pitch catalogs.** `events/frozen/crypto_pitch_v1.csv` = header + first 10 positive + 6 quiet rows of `events/crypto_canonical.csv` (42 quiet days); `events/frozen/macro_pitch_v1.csv` = header + first 9 positive + 5 quiet rows of `events/macro_canonical.csv` (35 quiet days). Verified: these subsets reproduce the pitch numbers exactly.
3. **`--json-out` on `services/hft_anomaly_service/app/evaluate.py`** (additive; stdout unchanged plus CIs): alerts path + sha256, events path + sha256, alerts ts-coverage range, tolerance_sec, kinds filter, recall + Wilson 95% CI, matched/total, median TTD, FP/day + Poisson 95% CI, quiet-day count, per-kind and per-event-type breakdowns, generated_at. Also correct the `evaluate.py:5` docstring that wrongly implies the hot-path number used `--kinds cusum,spread_widen`.
4. **`gate/validate.sh`** (no Makefile anywhere — standardize on `bash gate/validate.sh` and `python -m gate.run_all`): hot eval with **no `--kinds` flag**, `--tolerance 86400`, against the committed frozen inputs; batch eval `--tolerance 259200`; a load-check loop asserting `load_events()` succeeds on every file in `events/`; diff of each JSON against committed expected JSONs in `results/eval_pitch_v1/`; nonzero exit on any mismatch.
5. **Repair `events/equities_canonical.csv` lines 23–27**: rewrite each quiet row to exactly 6 fields — `ts_ns, SPY, quiet_<name>, <description keeps the date-range text>, quiet, 604800` (delete the inserted window-name field; this is not a two-column swap).
6. **Generation-chain honesty + v2 baseline.** Timebox (≤1 hour) a forensic attempt to recover the original `btc_alerts.csv` generation command; on expected failure, document in README: *"v1 hot-path alerts are a frozen artifact; their generation parameters are unrecorded — evaluation is reproducible, generation is not."* Then pin a **new** daily-bar-capable backtest command chosen by an a-priori rule committed before any recall is looked at (documented profile defaults with `--session-gap-sec` raised above 86400, e.g. 604800; explicit `--warmup` and min_obs values stated in the command). Run it once on the frozen replay file, commit `gate/frozen/btc_alerts_v2.csv` + its eval JSON, and publish whatever it says as the v2 reproducible baseline (recall/FP: **to be measured**), with an explicit v1→v2 discontinuity note. The v1 pitch row is preserved verbatim as a frozen artifact.
7. **`--min-obs` on `services/hft_anomaly_service/app/sweep.py`**, default **0** (current behavior — a default of 100 would silently invalidate `minute_bars.env`'s knee provenance); explicit values are passed in all pinned commands. Note in a comment that "live" min_obs is profile-dependent: `state.py` default 100 vs `configs/crypto.env` 200 (warmup 100).
8. **Doc annotations** (README, `docs/qpulse_one_pager.md`, `docs/qpulse_pitch.md`): each headline number cites frozen catalog file, frozen alerts artifact + sha256, tolerance (±1 day hot / ±3 days batch), results JSON path, and the alerts file's ts-coverage range; add one sentence noting that 1 of the 4 hot-path misses (bitcoin_etf_approval, 2024-01-11) lies outside alert coverage (ends 2023-03-13); correct any doc text implying a kinds filter.

**Files touched**: `.gitignore`, `gate/frozen/*` (new), `events/frozen/*` (new), `events/equities_canonical.csv`, `services/hft_anomaly_service/app/evaluate.py`, `services/hft_anomaly_service/app/sweep.py`, `gate/validate.sh` (new), `results/eval_pitch_v1/*` (new), README, `docs/qpulse_one_pager.md`, `docs/qpulse_pitch.md`.

**Acceptance (measured)**: from a **fresh clone**, `bash gate/validate.sh` exits 0, printing and writing JSON with exactly 60.0% recall / 0.05 FP-day (hot, filterless) and 66.7% / 0.00 (batch), each with Wilson/Poisson CIs; `git ls-files` shows every validate.sh input tracked; `load_events()` succeeds on all 7 canonical catalogs; the v2 baseline JSON and its full pinned generation command are committed (its numbers published as measured, whatever they are).

**Cost**: $0 (local compute only).

---

## P1 — Qsight integration MVP (2 sessions; depends on P0)

**Goal.** Qpulse alerts appear on the live Qsight dashboard via an authenticated ingest endpoint, at $0/month incremental, with honest context-aware labeling. Architecture A (webhook → ingest → AlertEvent → UI) with on-demand producers; always-on B rejected under the $12/mo precedent with an explicit re-entry criterion (a user-facing consumer justifies ~$3–10/mo — list-price estimates, not measured — and B is then a config flip of `HFT_WEBHOOK_URL`, zero code).

Two majors fixed here: the context-blind `validated_kind` flag, and naive-scheduler interference (cooldown cross-suppression, ~288 non-triggered RDS rows/day/rule, Explain-button explaining stale naive rows).

**Tasks**
1. **PR 1 (Qsight backend)** — `POST /qpulse/ingest` in `services/forecasting_service_py/app/main.py` (alerts section only) + new `services/forecasting_service_py/tests/test_qpulse_ingest.py`. Do NOT touch `app/reports/`, `app/agents/`, `app/db/models.py`, `alembic/`, `scripts/qsight.sh` (WIP collision set). Spec:
   - Auth: `X-Qpulse-Key` header vs `QPULSE_INGEST_KEY` env via `secrets.compare_digest`; 503 when unconfigured (ships inert), 401 on mismatch; no user identity accepted from the caller; batch cap 500 → 422.
   - Symbol normalization: `BTC/USD` → `BTC-USD` then `_canonical_symbol()`.
   - Fan-out to active `AlertRule`s with `alert_type=="anomaly"` and matching ticker; persist **only triggered** `AlertEvent`s through the existing cooldown logic.
   - **Payload (context-aware honesty)**: `{"qpulse": {kind, score, severity, narrative, details, ts_ns, price, symbol_raw, feed, asset_class}, "detector_source": "qpulse", "reference_gate": <artifact name or null>, "gated_on_reference_catalog": bool}`. The flag is computed from **(kind, asset_class)**, and the gated set derives from P0's artifact census: `(robust_z | cusum) × BTC daily-bar crisis catalog (v1)` and `dependence_shift × macro daily basket (v1)`. `spread_widen` is **removed** from the gated set (zero such alerts in the v1 artifact; the filterless eval is what reproduces 60%). UI may show only the reference-gate *name* ("gated on BTC daily-bar crisis catalog — v1 artifact"), never bare recall/FP numbers next to live alerts; everything else is labeled "experimental". Live-tick performance: **to be measured** (P5).
   - **Naive-branch gating in this PR** (not deferred): `_evaluate_alert_rule`'s anomaly branch skips rules with `extra_config.detector == "qpulse"`. This kills cooldown cross-suppression, the 300s evaluation-noise rows, and makes the Explain button (`agentsApi.explainAlert`, latest-event-by-rule) explain the actual Qpulse row — all without touching `app/agents/`.
   - Server-side provenance filter: add an optional `detector_source` query param to `GET /alerts/events` (5-line change in the already-permitted file) so the feed card can't be starved by threshold events.
   - Response: `{received, matched_rules, events_persisted, suppressed: [{symbol, kind}], unmatched_symbols}` — per-rule cooldown across kinds is an explicit design decision, and dropped kinds are visible.
   - Tests: create `TestClient` **without** the context manager (per `tests/test_var_backtest_endpoint.py:71` convention) or set `ALERT_SCHEDULER_ENABLED=0`, so the startup scheduler never fires network calls mid-test; toggle the module-level key via `monkeypatch.setattr`, not env vars. Cases: 503 unset key; 401 wrong key; fan-out visible to owner and invisible to others; cooldown suppression; unmatched symbol; naive branch skipped for qpulse-gated rules.
2. **Ops (one-time, user-run)**: add `QPULSE_INGEST_KEY` (`openssl rand -hex 32`) to the ECS task definition — either the console task-definition revision flow or the two-step CLI (`aws ecs register-task-definition` **and** `aws ecs update-service --task-definition`; the deploy workflow's force-new-deployment alone reuses the old revision).
3. **PR 2 (Qpulse)**: ~15 lines in `services/hft_anomaly_service/app/sinks/webhook_sink.py` + `services/hft_anomaly_service/app/live.py`: `HFT_WEBHOOK_KEY` env → `X-Qpulse-Key` header; also attach `feed`/`asset_class` to the payload. Push to GitHub only; nothing deploys.
4. **PR 3 (Qsight frontend, `services/frontend_nextjs/`)**: new `src/components/shared/AnomalyFeedCard.tsx` (polls `alertApi.events({triggered_only: true, detector_source: "qpulse", limit: 25})`, `refetchInterval: 60_000`) and `SeverityBadge.tsx`; mount in dashboard right column above `AlertRulesList`; `CreateAlertDialog` gains "Anomaly (Qpulse)" which sets `alert_type: "anomaly"` **and** `extra_config.detector: "qpulse"`; empty/stale state reads "Qpulse detector runs on-demand — last event ⟨relative time⟩"; experimental tag on non-gated kinds. Safe to deploy before the ops step (card shows the on-demand state).
5. **First light (laptop, $0)** — profile env files are not auto-loaded locally:
   ```bash
   cd "services/hft_anomaly_service"
   set -a; source ../../configs/crypto.env; set +a
   HFT_WEBHOOK_URL=https://<api>/qpulse/ingest HFT_WEBHOOK_KEY=<key> python -m app.live --source alpaca
   ```

**Acceptance (measured)**: new test file green; from a live laptop session, a BTC/USD alert produces an `AlertEvent` with `payload.qpulse` visible via `GET /alerts/events` and on the dashboard within one 60 s poll; a DB count of non-triggered AlertEvents for the qpulse rule after ≥15 min of scheduler uptime equals 0; a second same-rule alert inside the cooldown window appears in `suppressed` with its kind; no metric numbers rendered next to live alerts.

**Cost**: $0/month incremental (reuses running ECS task + RDS; one-time task-def revision; laptop producer). Rejected alternatives on record: Fargate 24/7 ≈ $9–10/mo on-demand / ≈ $3/mo Spot (state-loss risk), VPS ≈ €4–5/mo — all list-price estimates, all deferred until a live consumer exists.

---

## P2 — Quote-capable capture → replay → backtest → sweep (2–3 sessions; depends on P0)

**Goal.** Remove the structural blocker: the trades-only `ts_ns,symbol,price` pipeline makes every quote-side detector (spread, OBI, qrate, microprice, future OFI) and `volume_spike` impossible to validate offline. v2 event CSV: `ts_ns,symbol,etype,price,size,bid,ask,bid_size,ask_size` (`etype` ∈ {t,q}); legacy 3-column files remain readable via header sniffing.

**Tasks / files** (all `services/hft_anomaly_service/app/`): `recorder.py` (`CsvEventRecorder` writing Ticks **with size** and Quotes; keep `CsvTickRecorder`), `record.py` (`--source alpaca-market`, `--format v2`; crypto via `ALPACA_FEED=crypto`), `replay.py` (header sniffing; mixed Tick/Quote stream), `backtest.py` (dispatch Quote → `router.on_quote`; pass `size` through — kills the dead volume path; add `--min-obs` **default 0**, current behavior preserved), `pipeline.py` (size passthrough), `sweep.py` (v2 input, quotes, per-kind counts for all kinds). Note: Alpaca allows 1 concurrent websocket per feed — recording windows must not overlap live-service streaming (relevant once P1's producer runs).

**Acceptance (measured)**: (a) a 30-min local BTC/USD recording replayed through `backtest.py` yields quote-side kinds (`spread_widen`, `obi_extreme`, `quote_rate_spike`, `microprice_drift`) in `--alerts-out`; (b) double-replay of the same file is byte-identical; (c) **determinism regression, not pitch reproduction**: the P0-pinned v2 generation command on `gate/frozen/btc_replay_pitch_v1.csv` produces a byte-identical alert CSV before and after the refactor.

**Cost**: $0 (local recording windows).

---

## P3 — Catalog hygiene & pre-registered expansion gate (2–3 sessions; depends on P0)

**Tasks**
1. Schema v2: append `source_url,source_note` to all 7 catalogs (safe — `csv.DictReader` ignores unknown columns); backfill provenance where practical; unrecoverable rows get `source_note=legacy_unverified`, flagged, never silently counted as clean.
2. `gate/catalog_lint.py`: **field-count == header-count** (the class of corruption that broke equities and that DictReader swallows silently), midnight-UTC ts matching the description date, polarity domain, quiet ⇒ duration 604800 / positive ⇒ 0, no duplicate (symbol, date), quiet-vs-positive overlap tolerance, well-formed `source_url` on new rows. Wired into `gate/validate.sh`.
3. `events/CATALOG.md`: schema + sourcing policy (primary public sources per class: FOMC/BLS/FRED/Treasury; EDGAR 8-Ks and exchange halt notices; SEC/DOJ/CFTC orders and exchange incident pages; BoJ/BoE/SNB/ECB archives; Cboe/OPEC/EIA).
4. `gate/CATALOG_GATE.md`, committed **before** any data pull or detector run: target composition (positives 92 → ~162; quiet windows 132 × 7 d = **~924 quiet days** — the ~1,024 figure was an arithmetic error; totals must be recomputed from catalogs by tooling, never restated by hand); Wilson rationale (n=30–40 → ~±15–17 pt CI vs ±26 today; 200+ clean quiet days → FP < ~0.015/day by rule of three); tolerances ±86400 s hot / ±259200 s batch; hot-path generation config = the **P0-pinned v2 daily-bar command** (never the dead 600 s-session-gap defaults) with the v1→v2 discontinuity note; full batch command including `dependence_backtest --symbol SPY` attribution (without it, macro recall reads 0/9 on a symbol-join failure); **evaluability rule** — rows with no retrievable price history (verified: SIVB, FRC, TWTR return zero rows from yfinance) go into an explicit unevaluable bucket, excluded from recall denominators, never silently dropped; fallback source decision recorded (e.g. Stooq daily CSVs, or drop-and-document); quiet-window selection rule (20-day realized vol < 30th percentile of trailing year, ±5 d exclusion; humans may reject proposals with logged reasons, never add unproposed windows) registered before the proposer ever runs; expansion rows are holdout — tuning only on pitch-era dev rows; catalog freeze hash; DXY vs `DX-Y.NYB` vs `UUP` decision.

**Acceptance (measured)**: `catalog_lint` passes on all 7 catalogs; `CATALOG_GATE.md` committed with freeze hashes; `gate/validate.sh` still exits 0.

**Cost**: $0.

---

## P4 — Data plumbing & one-command eval loop (3–4 sessions; depends on P3)

**Tasks**
1. Parameterize `gate/data_pull.py` (`--symbols/--start/--end/--out-prefix`, symbol map for `^VIX`/FX/futures tickers, per-symbol on-disk cache refetching only missing ranges, exponential backoff for Yahoo 429s).
2. `gate/catalog_data.py`: per-catalog required range = [earliest event − 400 calendar days, latest event + 30 d] → pull → `wide_to_replay` → per-symbol replay + returns CSVs. Refresh past the hardcoded 2023-04-30 wall — this makes the **8 labeled post-wall positives + 1 quiet window** (not "~12") evaluable.
3. `gate/propose_quiet_windows.py` — an explicit deliverable here (it was load-bearing but unbudgeted in the original plan); runs only after its rule was registered in P3.
4. `gate/run_all.py`: per catalog → hot path (P0-pinned v2 backtest per replay CSV) and batch path (`dependence_backtest` incl. `--symbol` attribution) → `evaluate --json-out results/eval_<catalog>_<path>_<sha8>.json` at pre-registered tolerances → renders `results/catalog_matrix.md` (n, quiet days, recall + Wilson CI, median TTD, FP/day + Poisson upper bound, batch-only attribution). Every results JSON records the **full generation command line**. Sanity assertion before any recall computation: nonzero symbol overlap between alerts and events files (a silent 0/n from a symbol-format mismatch must be impossible).

**Acceptance (measured)**: `python -m gate.run_all` completes locally on crypto + macro end-to-end from freshly pulled data; matrix renders; all values in it come from the JSONs (all new-catalog numbers: **to be measured**).

**Cost**: $0 (yfinance daily is free; cache/backoff handles throttling).

---

## P5 — Recording campaign & new-detector gates (3–4 working sessions + 4–8 weeks calendar; depends on P2)

**Goal.** Validate the two dormant-but-wired detectors (VolumeStats, MicropriceStats) and the new best-level OFI detector the honest way: pre-register thresholds and pass/fail criteria **before** any eval recording; measure; document only what passed (failures are published as failures or labeled experimental with their measured numbers).

**Tasks**
1. Pre-registration docs first: `gate/prereg/volume_micro.md` and `gate/prereg/ofi.md` — frozen thresholds (current defaults, or values tuned only on separate tuning recordings), FP budgets (alerts per symbol-hour on quiet windows), scheduled-event coincidence criteria ("fires within M minutes of release on ≥k of n events, ≤j of n same-time controls").
2. **Recording campaign — budget stated up front** (the original plan understated this): ≥200 quiet symbol-hours per detector ≈ ~50 market-hours of iex across 4 liquid symbols (~8 trading days) plus ~100 continuous hours of 2-symbol crypto; the 1-websocket-per-feed limit serializes tuning vs eval recordings. In practice a ~2-week semi-attended operation. Laptop is $0; optionally use the existing `deploy/` scripts for a **short-lived, hourly-billed VPS window** for the long crypto captures (roughly €1–2 per ~100 h campaign at CX-class list rates — estimate, not measured; instance torn down after). No always-on infra.
3. `events/microstructure_scheduled.csv` (CPI ~monthly 08:30 ET; FOMC ~6-weekly 14:00 ET, preferred for iex) written in **feed-native symbols** (`BTC/USD`, bare tickers) — the canonical catalogs use yfinance-style symbols and `evaluate.py` joins on exact string; the P4 overlap assertion backstops this.
4. OFI implementation (1 session, the verified 4-touch seam): `features.py` `OFIStats` (Cont-style signed best-level flow, windowed sum, P² robust z, honoring min_obs), `detector.py` (compute in `on_quote` **before** the `last_bid/last_ask/last_*_size` overwrite at lines 209–212; kind `ofi_extreme`), `state.py` + `live.py` (`HFT_OFI_Z_THRESH` start 5.0, `HFT_OFI_WINDOW`, `HFT_OFI_ALPHA`), `narrative.py`. OFI shares the same recordings, but its thresholds must be pre-registered before its eval windows are replayed.
5. On pass: README hot-path table, `configs/crypto.env` + new `configs/equities_iex.env` gain threshold lines with provenance comments; P1's reference-gate labeling in Qsight updates accordingly.

**Acceptance (measured)**: pre-registered FP budget met on ≥200 quiet symbol-hours per detector (rates: **to be measured**, with CIs); coincidence criterion met on 3–5 accumulated scheduled events (**to be measured**); all results as committed JSONs via `--json-out`. A failed gate leaves the detector undocumented or explicitly experimental — that outcome is acceptable.

**Deferred**: P² migration of VolumeStats/MicropriceStats (must re-run this same gate if ever done).

**Cost**: $0 baseline; optional on-demand VPS window as above.

---

## P6 — Catalog sourcing sprints (3–4 sessions; depends on P3, P4; interleaves with P5's calendar waits)

- **Sprint A** — crypto + macro to targets; quiet-window generation via the registered proposer + logged review.
- **Sprint B** — equities: re-run with the P0-repaired quiet rows; add earnings-shock/halt events from EDGAR; per-symbol quiet windows. **Screen every candidate for data availability before it enters the catalog** (halted/collapsed names are disproportionately delisted); unevaluable rows go to the pre-registered bucket.
- **Sprint C** — fx / rates / volatility / commodities: first-ever harness runs for these 4 catalogs, plus growth to targets.

**Acceptance (measured)**: each sprint ends `catalog_lint`-clean, `run_all` executed, JSONs staged; per-class recall/FP: **to be measured** — the gate doc pre-commits to publishing whatever the matrix says, including recall drops on holdout rows.

**Cost**: $0.

---

## P7 — Ticks profile validation (2 sessions + 3 market days wall-clock; depends on P0; ideal filler during P5/P6 waits)

**Tasks**: extend `sweep.py` with `--alpha-grid`, `--session-gap-grid`, `--out` grid CSV (defaults preserve current behavior); record 3+ full iex sessions for 4 liquid symbols; sweep on sessions A/B, freeze parameters, then record holdout session C; rewrite `configs/ticks.env` with values + full provenance header (data window, symbols, sym·hr, knee, exact command, grid artifact path, explicit min_obs/warmup used — and which profile "live parity" means, given `crypto.env`'s 200/100 vs `state.py`'s 100/50) plus an explicit statement of what was NOT validated (recall vs labeled events; quote-side kinds) unless the P5 coincidence criterion is also run at these params.

**Acceptance (measured)**: the pre-registered alert-rate band (declared before the holdout recording, e.g. knee-region per-sym-hr ±50%) holds on holdout session C; grid CSV + holdout JSON staged. Knee values: **to be measured**.

**Cost**: $0.

---

## P8 — Claims & report update (1–2 sessions; depends on measured outputs of P5/P6)

**Tasks**: rebuild the public claim set exclusively from committed artifacts — headline matrix ("measured on N labeled events and M quiet days across 7 asset classes, catalog frozen at `<sha>`, pre-registered in `gate/CATALOG_GATE.md`", with N/M recomputed by tooling); v1 pitch row preserved verbatim as a frozen-artifact row with its coverage-range caveat; v2 baseline row; batch-only attribution section listing exactly which events only `dependence_shift` caught; every README/one-pager/pitch number links a `results/` path; Qsight UI reference-gate copy updated if new gates now cover live-tick contexts.

**Acceptance (measured)**: a grep of docs finds no performance number without an adjacent artifact path; `gate/validate.sh` still exits 0.

**Cost**: $0.

---

## P9 (optional) — Crypto L2 depth, probe-gated (0.5–1 session; +3–4 only on go; depends on P2, P5)

Equities depth stays **out of scope** (no free source; paid feeds violate cost posture and the Alpaca-only constraint).

- **P9a probe**: standalone script (pattern of `app/alpaca_check.py`) subscribing the `orderbooks` channel (`T=="o"`) on the existing `v1beta3/crypto/us` websocket — same keys, zero new auth. Log 10–15 min of BTC/USD depth/delta-rate/snapshot semantics. **Decision gate**: proceed only if ≥ ~5 usable levels and manageable message rate. The Binance `@depth20@100ms` fallback **exceeds the declared data-access envelope (Alpaca + yfinance) and requires explicit user sign-off as a recorded decision — it is not a default fallback**; if the probe fails and sign-off is withheld, the honest outcome is "crypto L2 out of scope", mirroring the equities exclusion.
- **P9b build** (on go): `Book` dataclass, book-state maintenance, `Router.on_book`, depth feature via the P5 OFI seam, JSONL sidecar capture + replay, pre-registered gate identical in structure to OFI's.

**Cost**: $0 (probe uses existing keys/connection).

---

## Cross-workstream sequencing — what blocks what

- **P0 blocks everything claim-bearing.** Both original M0s converged on this, and both critiques found P0 itself needed repair (gitignored inputs, misdiagnosed catalog bug, unrecorded generation chain). Nothing that states a number should land before P0.
- **Integration depends on neither expansion track.** It reuses existing tables, the webhook sink already exists, and its only real dependency is P0's honest census of what the gates actually covered (which fixes the labeling flag). It is the fastest user-visible win, so it goes second.
- **Catalog work does not gate detector implementation, and vice versa** — they share only P0. But detector validation (P5) is **calendar-bound** (4–8 weeks of scheduled macro events + ~2 weeks of recording hours), while catalog work is pure desk work. Therefore P2 (P5's enabler) outranks the catalog track in start order, and P3/P4/P6 fill P5's wall-clock. P7 is an independent filler after P0.
- **P8 is last**, consuming whatever measured artifacts exist; P9 is optional and probe-gated.

**Suggested session order for a solo founder** (one Claude Code session at a time): S1–2 P0 → S3–4 P1 (first light) → S5–7 P2 → S8 P5 prereg + start recordings (clock starts) → S9–10 P3 → S11–14 P4 → S15+ interleave P6 sprints and P5 gate runs as events accrue, with P7 in the gaps → P8 when the matrix is done → P9 if desired.

**Explicitly parked**: P² migration of Vol/Micro/OBI stats; z-based OBI; Hawkes burst upgrade; tick-granularity catalog events (Alpaca history is IEX-only for equities — any TTD claim must say "on IEX tick data" — and crypto history effectively starts ~2021); TopBar bell, per-ticker markers, `/anomalies` page; symbol-level `QpulseAlert` table; always-on architecture B (re-entry: a paying/live consumer; then it is a `HFT_WEBHOOK_URL` config flip).

## Appendix — MAJOR critique findings fixed in this synthesis

1. Broken data→alerts chain / unreproducible `btc_alerts.csv` → P0 task 6 (documented gap + newly pinned v2 command; P2 gate restated as determinism regression).
2. Gitignored, non-regenerable eval inputs on the public repo (found independently by two critiques) → P0 tasks 1 and acceptance ("fresh clone" test).
3. Misdiagnosed equities catalog corruption (7-field rows, `load_events()` crash) → P0 task 5 + load-check + P3 lint field-count rule.
4. "Frozen at current defaults" pre-registering a dead hot path (0 alerts on daily bars) → P3 gate references the P0-pinned v2 daily-bar config with discontinuity note; `run_all` records generation commands.
5. Delisted tickers (SIVB/FRC/TWTR) unevaluable via yfinance → P3 evaluability rule + fallback decision + P6 pre-entry screening.
6. Context-blind `validated_kind` stamping measured metrics onto unmeasured contexts → P1 payload records feed/asset_class, flag renamed and computed from (kind, context), UI names the reference gate and never shows bare numbers; `spread_widen` removed from the gated set per the P0 census.
7. Naive-scheduler interference (cooldown cross-suppression, RDS noise, broken Explain) → P1 pulls `extra_config.detector=="qpulse"` gating into PR 1.
8. (Cross-cutting) `--kinds cusum,spread_widen` ambiguity → decided now: filterless eval is pinned; the docstring and docs implying the filter are corrected in P0.

---

**Start here:** P0, task group 1–5 as a single staged PR in this repository: commit the four frozen input CSVs to `gate/frozen/` with sha256s, create `events/frozen/crypto_pitch_v1.csv` (first 10 positive + 6 quiet rows; 42 quiet days) and `events/frozen/macro_pitch_v1.csv` (first 9 positive + 5 quiet rows; 35 quiet days), add `--json-out` with Wilson/Poisson CIs and alerts ts-coverage to `services/hft_anomaly_service/app/evaluate.py`, rewrite `events/equities_canonical.csv` lines 23–27 to exactly 6 fields (`ts_ns,SPY,quiet_*,description,quiet,604800` — delete the inserted window-name field), and write `gate/validate.sh` running the hot eval with **no `--kinds` flag** at tolerance 86400 and the batch eval at tolerance 259200 against the committed inputs, plus a `load_events()` check over every catalog, diffing JSONs against committed expectations. Acceptance: `bash gate/validate.sh` exits 0 from a fresh clone, reproducing exactly 60.0%/0.05 and 66.7%/0.00. 
