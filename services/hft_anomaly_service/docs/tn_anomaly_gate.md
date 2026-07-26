# tn_anomaly Detector — Pre-registered Gate

**Locked: 2026-05-07.** Hyperparameters, criteria, and decision rule are
fixed at this date. Nothing here may be tuned after seeing gate results;
the validator script reads from this doc and writes a pass/reject verdict.

This is the same gate-then-build pattern that rejected MPS-copula. If any
criterion below fails, the detector is rejected — no partial ship, no
re-tuning.

---

## What it is

A **Matrix Product State (MPS) Born-machine** model of the joint
distribution of N daily log-returns across the cross-asset universe.
MPS represents the joint probability tensor as a chain of low-rank
site tensors, parameter count O(N·d·χ²) instead of O(d^N).

- **Quantum-computing lineage**: Born machines and DMRG-style fitting
  are taken directly from quantum many-body / quantum machine-learning
  literature.
- **Runtime**: pure NumPy. No quantum SDK, no GPU, no quantum hardware.
- **Why it might add over the t-copula**: t-copula assumes a single
  scalar tail-dependence parameter (df). MPS captures non-Gaussian,
  asset-specific tail shapes and asymmetric co-movement — distributional
  features the t-copula approximates but doesn't fit.

Detector kind: `tn_anomaly`. Lives in the batch path alongside
`dependence_shift`. Does not replace it.

---

## Hyperparameters (locked)

| Param           | Value      | Rationale                                          |
|-----------------|------------|----------------------------------------------------|
| `chi`           | 4          | Cheap, expressive enough; revisit only if gate passes |
| `d` (bins)      | 8          | Quantile bins per asset                            |
| `ref_days`      | 252        | Match `dependence_shift`; ~1y of trading days      |
| `roll`          | 5          | Match `dependence_shift`                           |
| `z_thresh`      | 4.0        | Stricter than t-copula's 3.0 — MPS has more capacity to overfit |
| `universe`      | DEFAULT_UNIVERSE | `["XLK","XLF","XLE","XLV","SPY","TLT","GLD","HYG"]` |
| `fit_method`    | LBFGS-B on flattened MPS params, init from random + small jitter |
| `fit_max_iter`  | 500                                                              |
| `fit_n_restarts`| 3 (best-LL wins)                                                 |

Binning: empirical quantiles of each asset's reference returns map to
bin index ∈ {0..d-1}. Bin edges computed once at fit time and reused
at score time (out-of-distribution returns clip to nearest bin).

---

## Pre-registered criteria (must pass ALL)

### C1 — Corroboration with the validated baseline

On a 1-year held-out window (post-reference), at every score timestamp
both detectors compute their `|z|` against their own null. Count
events where the t-copula reports `|z| > 3` (its production threshold).
The MPS must flag (`|z| > 4`) on **≥ 60%** of those events.

Justification: the t-copula passed its own canonical-event gate, so its
high-z firings are an empirical proxy for genuinely anomalous joint
states. A useful new detector must at minimum corroborate.

### C2 — Lift over the baseline

Of all events the MPS flags, **≥ 15%** must be days where the
t-copula `|z| < 3`. This is the value-add: a detector that only
duplicates the t-copula adds no signal.

### C3 — False-positive rate on quiet windows

Three pre-selected 30-day windows where (a) t-copula was silent
(|z|<2 throughout) and (b) realized vol of SPY was below its
trailing-1y median. MPS firings per window must be **≤ 0.10/day**
on each window. Same bar the t-copula had to pass.

Window selection criteria are data-dependent and resolved at gate-run
time by the validator; the criteria themselves are locked here.

### C4 — Fit stability

Refit the MPS on two disjoint 252-day windows ≥ 90 days apart. KL
divergence between the two fitted distributions, estimated by
Monte-Carlo sampling (10000 samples from each) and density evaluation,
must be **≤ 0.5 nats**.

If the fit is unstable across reasonable historical periods, the
detector is too sensitive to its training window to be trusted.

### C5 — Compute budget

A full fit on 252 days × 8 assets must complete in **≤ 30 seconds**
wall-clock on a single CPU core. Same machine that runs the live
service. Validator times this and records the number.

---

## Decision rule

PASS = all of {C1, C2, C3, C4, C5}.
REJECT = any failure.

If REJECT: detector is not integrated. The runtime stays as-is. Verdict
is filed at `gate/results/tn_anomaly_verdict.md` with the failing
criterion(s) called out, and we move on.

If PASS: integrate as `tn_anomaly` in the batch path:
- new section in Settings UI under `dependence_shift`
- new option in History dropdown
- new kind color in CSS
- new narrative template
- persisted config

---

## What is NOT in scope

- Quantum hardware execution. Pure classical CPU.
- Replacing `dependence_shift`. Both run in parallel; corroborated alerts
  are stronger signals.
- Tuning hyperparameters after seeing gate results. Locked above.
- Marketing-only "quantum-inspired" labeling without working code. The
  point of this gate is to ship code that earns the label or none at all.
