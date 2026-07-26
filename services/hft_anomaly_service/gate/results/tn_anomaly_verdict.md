# tn_anomaly Gate Verdict — REJECT

Run timestamp: 2026-05-07T16:36:52.041412+00:00
Gate spec: docs/tn_anomaly_gate.md

## Summary

- MPS fit: 832 params, 19.2s
- t-copula df: 20.0
- Holdout obs: 795
- t-copula events (|z|≥3.0): 35
- MPS events (|z|≥4.0): 697

## Per-criterion

### C1 — Corroboration with t-copula
- Required: ≥ 60% of t-copula events corroborated
- Observed: 74.3%  (26/35 t-copula events corroborated)
- **PASS**

### C2 — Lift over t-copula
- Required: ≥ 15% of MPS firings unique to MPS
- Observed: 96.3%  (671/697 MPS firings unique to MPS)
- **PASS**

### C3 — False positives in quiet windows
- Required: ≤ 0.1/day in each of 3 quiet 30-day windows
- Observed:
    - window @16: 27 firings (0.9/day) → FAIL
    - window @46: 24 firings (0.8/day) → FAIL
    - window @97: 22 firings (0.733/day) → FAIL
- **FAIL**

### C4 — Fit stability (KL)
- Required: KL ≤ 0.5 nats between disjoint refits
- Observed: KL(MPS_ref || MPS_second) ≈ 2.788 nats
- **FAIL**

### C5 — Compute budget
- Required: ≤ 30.0s for full fit on 252 days × 8 assets
- Observed: 19.2s
- **PASS**

---

## Verdict: **REJECT**

Failing criteria: C3, C4.
Detector is rejected. No partial integration. The runtime stays as-is.