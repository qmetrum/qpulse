"""Stage 1 runner for the MPS-copula gate.

For each of {COVID, Terra/Luna, SVB} crisis events and {Q-NC-1, Q-NC-2, Q-NC-3}
quiet windows: fit Gaussian / t / MPS(χ∈{2,4,8}) on the reference period,
compute rolling 5-day log-likelihoods across reference (null distribution) and
eval window (signal), z-score the signal against the null, record peak-z.

Writes:
    gate/results/scores.csv   — all eval-window z-scores
    gate/results/verdict.md   — pass/fail per pre-committed criterion
    gate/results/timing.csv   — peak z-score day offset per (event, method)
"""
from __future__ import annotations

import argparse
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, List

from .copulas import GaussianCopula, MPSCopula, TCopula


ROLL = 5   # days per rolling-LL window

EVENTS = [
    {"name": "COVID",      "universe": "core8",   "onset": "2020-02-20", "type": "crisis"},
    {"name": "TerraLuna",  "universe": "crypto8", "onset": "2022-05-09", "type": "crisis"},
    {"name": "SVB",        "universe": "core8",   "onset": "2023-03-08", "type": "crisis"},
    {"name": "Q-NC-1",     "universe": "core8",   "onset": "2019-10-01", "type": "quiet"},
    {"name": "Q-NC-2",     "universe": "core8",   "onset": "2021-08-02", "type": "quiet"},
    {"name": "Q-NC-3",     "universe": "core8",   "onset": "2022-11-01", "type": "quiet"},
]

REF_DAYS = 252
REF_GAP_DAYS = 30
EVAL_DAYS = 20


def _slice_ref_and_eval(returns: pd.DataFrame, onset: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    onset_ts = pd.Timestamp(onset)
    trade_dates = returns.index
    if onset_ts < trade_dates[0]:
        raise ValueError(f"onset {onset} before data start {trade_dates[0]}")
    onset_idx = trade_dates.searchsorted(onset_ts)
    eval_end_idx = min(onset_idx + EVAL_DAYS, len(trade_dates))
    ref_end_idx = max(0, onset_idx - REF_GAP_DAYS)
    ref_start_idx = max(0, ref_end_idx - REF_DAYS)
    if ref_end_idx - ref_start_idx < REF_DAYS - 20:
        raise ValueError(f"insufficient reference history for {onset}")
    ref = returns.iloc[ref_start_idx:ref_end_idx]
    eval_ = returns.iloc[onset_idx:eval_end_idx]
    return ref, eval_


def _rolling_ll(per_obs_ll: np.ndarray, window: int = ROLL) -> np.ndarray:
    """Rolling sum of log-likelihoods over `window` consecutive obs."""
    T = len(per_obs_ll)
    if T < window:
        return np.array([])
    return np.array([per_obs_ll[i:i + window].sum() for i in range(T - window + 1)])


def _evaluate_method(method, ref: np.ndarray, eval_: np.ndarray) -> Dict[str, np.ndarray]:
    """Fit on ref, score per-obs LL on ref + eval, return rolling LLs."""
    method.fit(ref)
    ref_ll = method.log_prob(ref)
    eval_ll = method.log_prob(eval_)
    return {
        "ref_rolling": _rolling_ll(ref_ll),
        "eval_rolling": _rolling_ll(eval_ll),
    }


def _peak_drop_z(eval_rolling: np.ndarray, null: np.ndarray) -> tuple[float, int]:
    """Most-negative z-score in eval window, and its day offset."""
    mu, sd = null.mean(), null.std(ddof=1)
    if sd < 1e-12:
        return 0.0, 0
    z = (eval_rolling - mu) / sd
    j = int(np.argmin(z))
    return float(z[j]), j


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=None,
                        help="override gate/data/ directory")
    args = parser.parse_args()

    root = Path(__file__).parent
    data_dir = Path(args.data_dir) if args.data_dir else (root / "data")
    out_dir = root / "results"
    out_dir.mkdir(parents=True, exist_ok=True)

    universes = {}
    for name in ("core8", "crypto8"):
        f = data_dir / f"{name}_returns.csv"
        if not f.exists():
            raise SystemExit(f"missing {f}; run `python -m gate.data_pull` first")
        universes[name] = pd.read_csv(f, index_col=0, parse_dates=True)

    methods_spec = [
        ("gaussian",  lambda: GaussianCopula()),
        ("t",         lambda: TCopula()),
        ("mps_chi2",  lambda: MPSCopula(chi=2, d=4)),
        ("mps_chi4",  lambda: MPSCopula(chi=4, d=4)),
        ("mps_chi8",  lambda: MPSCopula(chi=8, d=4)),
    ]

    # (event, method, peak_z, peak_day_offset)
    rows: List[Dict] = []
    scores_rows: List[Dict] = []

    for ev in EVENTS:
        print(f"\n=== {ev['name']} ({ev['type']}) onset {ev['onset']} universe={ev['universe']} ===")
        returns_df = universes[ev["universe"]]
        try:
            ref_df, eval_df = _slice_ref_and_eval(returns_df, ev["onset"])
        except ValueError as e:
            print(f"  skip: {e}")
            continue
        ref = ref_df.values
        eval_ = eval_df.values
        print(f"  ref {ref_df.index[0].date()}→{ref_df.index[-1].date()}  ({len(ref)} rows)")
        print(f"  eval {eval_df.index[0].date()}→{eval_df.index[-1].date()}  ({len(eval_)} rows)")

        for mname, mfactory in methods_spec:
            try:
                method = mfactory()
                r = _evaluate_method(method, ref, eval_)
                peak_z, day = _peak_drop_z(r["eval_rolling"], r["ref_rolling"])
                print(f"    {mname:<10}  peak_z={peak_z:+.2f}  at day {day}")
                rows.append({
                    "event": ev["name"], "type": ev["type"], "method": mname,
                    "peak_z": peak_z, "peak_day": day,
                    "ref_null_mu": float(r["ref_rolling"].mean()),
                    "ref_null_sd": float(r["ref_rolling"].std(ddof=1)),
                })
                for i, v in enumerate(r["eval_rolling"]):
                    scores_rows.append({
                        "event": ev["name"], "method": mname,
                        "day": i, "rolling_ll": float(v),
                        "z": (float(v) - r["ref_rolling"].mean()) / max(r["ref_rolling"].std(ddof=1), 1e-12),
                    })
            except Exception as e:
                print(f"    {mname:<10}  ERROR: {type(e).__name__}: {e}")
                rows.append({
                    "event": ev["name"], "type": ev["type"], "method": mname,
                    "peak_z": float("nan"), "peak_day": -1,
                    "ref_null_mu": float("nan"), "ref_null_sd": float("nan"),
                })

    summary = pd.DataFrame(rows)
    scores = pd.DataFrame(scores_rows)
    summary.to_csv(out_dir / "timing.csv", index=False)
    scores.to_csv(out_dir / "scores.csv", index=False)

    verdict = _verdict(summary)
    (out_dir / "verdict.md").write_text(verdict)
    print("\n" + verdict)


def _verdict(df: pd.DataFrame) -> str:
    """Apply pre-committed gate criteria and return a markdown summary."""
    lines = ["# Gate Verdict — Stage 1\n"]
    crisis = df[df["type"] == "crisis"].copy()
    quiet = df[df["type"] == "quiet"].copy()

    # Collapse multi-chi MPS to "best MPS" per event via |peak_z|
    crisis_mps = crisis[crisis["method"].str.startswith("mps_")]
    best_mps = (
        crisis_mps.assign(abs_z=crisis_mps["peak_z"].abs())
        .sort_values("abs_z", ascending=False)
        .groupby("event").first()
        .reset_index()[["event", "method", "peak_z", "peak_day"]]
        .rename(columns={"method": "best_mps_chi", "peak_z": "mps_peak_z", "peak_day": "mps_day"})
    )
    gauss = (crisis[crisis["method"] == "gaussian"]
             [["event", "peak_z", "peak_day"]]
             .rename(columns={"peak_z": "gauss_peak_z", "peak_day": "gauss_day"}))
    tcop = (crisis[crisis["method"] == "t"]
            [["event", "peak_z", "peak_day"]]
            .rename(columns={"peak_z": "t_peak_z", "peak_day": "t_day"}))
    merged = best_mps.merge(gauss, on="event").merge(tcop, on="event")
    lines.append("## Crisis events — best MPS vs Gaussian / t\n")
    lines.append("```")
    lines.append(merged.to_string(index=False))
    lines.append("```")
    lines.append("")

    # Criterion 1: |mps_peak_z| >= 1.5 * |gauss_peak_z| on >=2 of 3 events
    c1_mask = merged["mps_peak_z"].abs() >= 1.5 * merged["gauss_peak_z"].abs()
    c1 = int(c1_mask.sum()) >= 2

    # Criterion 2: mps_day <= gauss_day + 2 on the events that satisfied c1
    # Treat "mps_day <= gauss_day + 2" as "timing consistent/leading"
    c2_mask = (merged["mps_day"] <= merged["gauss_day"] + 2)
    c2 = int((c1_mask & c2_mask).sum()) >= 2

    # Criterion 3: |mps_peak_z| > |t_peak_z| on >=1 of 3
    c3_mask = merged["mps_peak_z"].abs() > merged["t_peak_z"].abs()
    c3 = int(c3_mask.sum()) >= 1

    lines.append("\n## Detection criteria\n")
    lines.append(f"- **C1** MPS |peak-z| ≥ 1.5× Gaussian on ≥2 events:  `{c1}`  ({int(c1_mask.sum())}/3)")
    lines.append(f"- **C2** MPS timing ≤ Gaussian+2d on those events:   `{c2}`  ({int((c1_mask & c2_mask).sum())}/2 needed)")
    lines.append(f"- **C3** MPS |peak-z| > t-copula on ≥1 event:        `{c3}`  ({int(c3_mask.sum())}/3)")

    # Negative controls
    # Compute median crisis peak-z across MPS methods as the spurious-fire threshold
    thresh = best_mps["mps_peak_z"].abs().median()
    quiet_mps = quiet[quiet["method"].str.startswith("mps_")]
    quiet_mps_best = (
        quiet_mps.assign(abs_z=quiet_mps["peak_z"].abs())
        .sort_values("abs_z", ascending=False)
        .groupby("event").first()
        .reset_index()[["event", "peak_z"]]
    )
    n_spurious_mps = int((quiet_mps_best["peak_z"].abs() >= thresh).sum())
    quiet_t = quiet[quiet["method"] == "t"][["event", "peak_z"]]
    n_spurious_t = int((quiet_t["peak_z"].abs() >= thresh).sum())
    c4 = n_spurious_mps <= 1
    c5 = n_spurious_mps <= n_spurious_t

    lines.append("\n## Negative-control criteria\n")
    lines.append(f"Spurious-fire threshold (median crisis |peak-z|): `{thresh:.2f}`")
    lines.append(f"- **C4** MPS spurious fires ≤ 1 of 3 quiet windows:  `{c4}`  ({n_spurious_mps}/3)")
    lines.append(f"- **C5** MPS not more spurious than t-copula:        `{c5}`  (mps={n_spurious_mps} vs t={n_spurious_t})")

    all_pass = c1 and c2 and c3 and c4 and c5
    partial = (int(c1_mask.sum()) >= 2) and c4 and c5
    lines.append("\n## Decision\n")
    if all_pass:
        lines.append("**FULL PASS → ship MPS-copula as `copula_break` alert in Qpulse.**")
    elif partial and not (c4 and c5):
        lines.append("**FAIL (negative-control breach) → drop MPS; consider t-copula only.**")
    elif partial:
        lines.append("**PARTIAL PASS → ship t-copula as 'dependence monitoring', hold MPS as research prototype.**")
    else:
        lines.append("**FAIL → drop cross-asset feature for v1; revisit with better method later.**")

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
