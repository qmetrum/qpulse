# Gate Verdict — Stage 1

## Crisis events — best MPS vs Gaussian / t

```
    event best_mps_chi  mps_peak_z  mps_day  gauss_peak_z  gauss_day  t_peak_z  t_day
    COVID     mps_chi2    1.047339        0      0.966540          6  3.774592      0
      SVB     mps_chi8   -3.468631       10    -15.221280          1 -6.078582      1
TerraLuna     mps_chi2   -2.089586       14     -0.429687         10 -0.750618      7
```


## Detection criteria

- **C1** MPS |peak-z| ≥ 1.5× Gaussian on ≥2 events:  `False`  (1/3)
- **C2** MPS timing ≤ Gaussian+2d on those events:   `False`  (0/2 needed)
- **C3** MPS |peak-z| > t-copula on ≥1 event:        `True`  (1/3)

## Negative-control criteria

Spurious-fire threshold (median crisis |peak-z|): `2.09`
- **C4** MPS spurious fires ≤ 1 of 3 quiet windows:  `True`  (1/3)
- **C5** MPS not more spurious than t-copula:        `False`  (mps=1 vs t=0)

## Decision

**FAIL → drop cross-asset feature for v1; revisit with better method later.**
