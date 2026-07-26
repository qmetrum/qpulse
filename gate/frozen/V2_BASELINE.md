# v2 hot-path baseline — pre-registered generation rule

## Why a v2 exists

The v1 hot-path alerts (`btc_alerts_pitch_v1.csv`, 94 alerts) are the artifact the
published 60.0% / 0.05 figure was measured on. **Their generation parameters are
unrecorded and could not be recovered**: no script, doc, or commit in this repo
names the `app.backtest` invocation that produced them, the repository history is
a single squashed commit, and a parameter grid search over session-gap, warmup,
z-threshold, CUSUM h, and alpha reached at most 48 alerts against the file's 94 —
never a match. The alerts may also predate the current detector code.

So, precisely: **evaluation is reproducible, generation is not.** `gate/validate.sh`
re-derives the published numbers from the frozen alerts artifact on any clone, but
nobody can re-derive the alerts artifact itself from the replay data.

v2 closes that gap going forward with a generation command that *is* pinned.

## The pre-registered rule (fixed before any result was observed)

Parameters are the documented `app/backtest.py` defaults, with exactly one change,
justified structurally rather than by outcome:

| Parameter | Value | Source |
|---|---|---|
| `--alpha` | 0.02 | backtest.py default |
| `--z-thresh` | 4.0 | backtest.py default |
| `--cusum-h` | 5.0 | backtest.py default |
| `--cusum-k` | 0.5 | backtest.py default |
| `--warmup-ticks` | 5 | backtest.py default |
| `min_obs` | 0 | `PerSymbolDetector` default; backtest exposes no flag |
| `--session-gap-sec` | **604800** | **the one change** |

The session-gap change is structural, not a tuned knob. `btc_replay_pitch_v1.csv`
holds daily bars, so the 600 s default declares a new session at every single bar,
resets the detector state, and emits **zero** alerts on any daily-bar input. A
value long enough to span the gaps between bars is required for the file to be
processed at all.

**Correction (measured after the fact).** The rationale originally written here
claimed that "any value above 86400 s produces the same session structure" and
that 604800 s was "the smallest round value that works". Both were wrong, and
checking the file's actual gap structure disproves them: the series contains gaps
of exactly 86400 s and 172800 s (weekend double-gaps), and `app/features.py`
breaks a session on a strict `>`. So values in [86400, 172799] still break at
every weekend and emit **38** alerts, while values ≥ 172800 keep the series whole
and emit **42**. The true threshold is 172800 s, not 86400 s.

What survives that correction is the part that matters for integrity: the choice
of 604800 over any other value above the threshold **did not affect the result**.
Every session-gap in {172800, 259200, 604800, 2592000} produces a byte-identical
alerts file (sha256 `c62573fc11180786…`). The parameter was therefore not tuned
toward an outcome — but the justification given for it was asserted rather than
measured, which is the kind of claim this document exists to prevent. It is left
here corrected rather than quietly rewritten.

Whatever this command produces is published as the v2 baseline, including a worse
recall or a higher false-positive rate than v1.

## The pinned command

```bash
cd services/hft_anomaly_service
python -m app.backtest \
    --input ../../gate/frozen/btc_replay_pitch_v1.csv \
    --alerts-out ../../gate/frozen/btc_alerts_v2.csv \
    --alpha 0.02 --z-thresh 4.0 --cusum-h 5.0 --cusum-k 0.5 \
    --warmup-ticks 5 --session-gap-sec 604800
```

## Measured result

Filled in from the single run of the command above, evaluated against
`events/frozen/crypto_pitch_v1.csv` at ±86400 s tolerance with no `--kinds` filter
(the same evaluation as v1, so the two rows are comparable):

Artifact: `gate/frozen/btc_alerts_v2.csv` (42 alerts — 22 `robust_z`, 20 `cusum`;
sha256 `c62573fc11180786…`). Full record: `results/eval_pitch_v1/hot_v2_baseline.json`.

| Metric | v2 (reproducible) | v1 (frozen artifact) |
|---|---|---|
| Recall | **40.0%** (4/10), 95% CI 16.8–68.7% | 60.0% (6/10), 95% CI 31.3–83.2% |
| False positives in quiet windows | **0.00/day** (0 alerts / 42 quiet days), 95% CI 0.00–0.09 | 0.05/day (2 alerts / 42 quiet days), 95% CI 0.01–0.17 |
| Alerts emitted | 42 | 94 |
| Alert time coverage | 2019-01-07 → 2023-03-13 | 2019-01-14 → 2023-03-13 |

**v2 detects fewer of the labeled crises than v1 and fires in none of the quiet
windows.** That is the measured outcome of the pre-registered rule and it is
published unchanged. Detected: `ust_depeg_begins`, `luna_death_spiral`,
`ftx_bank_run`, `svb_contagion`. Missed: `china_mining_ban`,
`taproot_ath_aftermath`, `celsius_halt`, `ftx_bankruptcy`, `usdc_depeg`,
`bitcoin_etf_approval`.

Both confidence intervals are wide because the catalog holds 10 positives and 42
quiet days. On this sample size the v1/v2 recall intervals overlap heavily, so
these two numbers are **not** distinguishable evidence about detector quality —
which is the point of the discontinuity note below. Growing the catalog (roadmap
P3–P6) is what would make such a comparison meaningful.

One structural caveat applies to both rows: `bitcoin_etf_approval` (2024-01-11)
falls after the last alert in either file (2023-03-13), so no configuration could
have detected it. It is counted as a miss in the denominator above rather than
silently excluded.

## v1 → v2 discontinuity

These are two different alert sets over the same replay data, not a before/after
improvement of one pipeline. v1 stays in the repo verbatim as the artifact behind
the published pitch number. v2 is the reproducible baseline that every future
hot-path claim builds on. Do not present a v1-to-v2 delta as a change in detector
quality — the v1 generating configuration is unknown, so the comparison has no
controlled variable.
