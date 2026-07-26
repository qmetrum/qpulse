"""Run the pre-registered tn_anomaly gate (docs/tn_anomaly_gate.md).

Pulls daily returns for the configured universe via yfinance, fits both
MPSBornMachine and TCopula on a 252-day reference window, scores a
holdout window, and evaluates the locked criteria C1-C5. Writes a
verdict to gate/results/tn_anomaly_verdict.md.

This script is the single source of truth for the pass/reject decision.
No criterion thresholds may be edited here; they come from the gate doc
and are mirrored in CRITERIA below for transparency.

Usage:
    cd services/hft_anomaly_service
    python -m gate.run_tn_anomaly_gate
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from app.batch.copulas import TCopula
from app.batch.dependence_shift import DEFAULT_UNIVERSE
from app.batch.mps_detector import MPSBornMachine


# Locked criteria (mirrors docs/tn_anomaly_gate.md). DO NOT EDIT.
CRITERIA = {
    "C1_corroboration_pct_min": 0.60,
    "C2_lift_pct_min":          0.15,
    "C3_fp_per_day_max":        0.10,
    "C4_kl_max":                0.5,
    "C5_fit_seconds_max":       30.0,
    "tcopula_z_thresh":         3.0,
    "mps_z_thresh":             4.0,
    "ref_days":                 252,
    "roll":                     5,
    "chi":                      4,
    "d":                        8,
    "fit_n_restarts":           3,
    "fit_max_iter":             500,
}


def _fetch_returns(universe: list[str], total_days: int) -> pd.DataFrame:
    import yfinance as yf
    start = (pd.Timestamp.utcnow()
             - pd.Timedelta(days=int(total_days * 1.6) + 90)).strftime("%Y-%m-%d")
    raw = yf.download(
        tickers=" ".join(universe), start=start,
        auto_adjust=True, progress=False, group_by="ticker", threads=True,
    )
    if isinstance(raw.columns, pd.MultiIndex):
        closes = {}
        for t in universe:
            if t in raw.columns.get_level_values(0):
                sub = raw[t]
                if "Close" in sub.columns:
                    closes[t] = sub["Close"]
        prices = pd.DataFrame(closes)
    else:
        prices = raw[["Close"]].rename(columns={"Close": universe[0]})
    prices = prices.reindex(columns=universe).ffill(limit=2).dropna()
    returns = np.log(prices / prices.shift(1)).dropna(how="all").dropna()
    return returns


def _rolling_z(per_obs_ll: np.ndarray, roll: int, null_mu: float, null_sd: float) -> np.ndarray:
    """For each i in [roll-1, len), compute (rolling-LL_i - mu) / sd."""
    if len(per_obs_ll) < roll:
        return np.array([])
    rolling_ll = np.array([per_obs_ll[i - roll + 1: i + 1].sum()
                           for i in range(roll - 1, len(per_obs_ll))])
    return (rolling_ll - null_mu) / max(null_sd, 1e-12)


def _fit_null(detector, ref_returns: np.ndarray, roll: int) -> tuple[float, float]:
    ll = detector.log_prob(ref_returns)
    rolling = np.array([ll[i: i + roll].sum() for i in range(len(ll) - roll + 1)])
    return float(rolling.mean()), float(rolling.std(ddof=1))


def _kl_via_sampling(p_model: MPSBornMachine, q_model: MPSBornMachine,
                     n_samples: int = 10000, seed: int = 1) -> float:
    """KL(P || Q) estimated by sampling x~P, computing log p(x) - log q(x).

    Uses bin-space samples; both models must share the same bin grid so we
    can evaluate q under the bins drawn from p. We approximate by mapping
    sampled bins back to bin-center returns under p's bin edges, then
    digitizing under q.
    """
    rng = np.random.default_rng(seed)
    bins_p = p_model.sample(n_samples, rng=rng)            # (n, N) ints under p's grid
    # Convert to "synthetic returns" using p's bin centers, then re-digitize under q
    centers = []
    for j in range(p_model.n_assets):
        edges = p_model._bin_edges[j]
        full = np.concatenate(([-np.inf], edges, [np.inf]))
        c = []
        for k in range(p_model.d):
            lo, hi = full[k], full[k + 1]
            if not np.isfinite(lo): lo = full[k + 1] - 1e-3
            if not np.isfinite(hi): hi = full[k] + 1e-3
            c.append(0.5 * (lo + hi))
        centers.append(np.array(c))
    synth = np.empty((n_samples, p_model.n_assets))
    for j in range(p_model.n_assets):
        synth[:, j] = centers[j][bins_p[:, j]]
    log_p = p_model.log_prob(synth)
    log_q = q_model.log_prob(synth)
    finite = np.isfinite(log_p) & np.isfinite(log_q)
    if finite.sum() == 0:
        return float("inf")
    return float(np.mean(log_p[finite] - log_q[finite]))


def _pick_quiet_windows(returns: pd.DataFrame, t_z_holdout: np.ndarray,
                        roll: int, n_windows: int = 3, win_days: int = 30) -> list[int]:
    """Pick n_windows non-overlapping starting indices into the holdout where:
       - t-copula |z| < 2 throughout
       - SPY realized vol (20d) is below the 1y trailing median.

    Returns starting indices into the holdout segment (relative to len(t_z_holdout)).
    """
    spy = returns.get("SPY")
    rv = spy.rolling(20).std() if spy is not None else None
    rv_median = float(rv.median()) if rv is not None and rv.notna().any() else float("inf")

    candidates: list[int] = []
    n = len(t_z_holdout)
    used: list[tuple[int, int]] = []
    # tc_z[s] corresponds to returns-index (len(returns) - len(tc_z)) + s
    # because rolling z is right-aligned over the holdout.
    rv_offset = len(returns) - len(t_z_holdout)
    for start in range(0, n - win_days + 1):
        end = start + win_days
        window_z = t_z_holdout[start:end]
        if np.any(np.abs(window_z) >= 2.0):
            continue
        if rv is not None:
            rv_window = rv.iloc[rv_offset + start: rv_offset + end].dropna()
            if len(rv_window) == 0 or rv_window.median() >= rv_median:
                continue
        # Non-overlap with already chosen
        if any(not (end <= s or start >= e) for s, e in used):
            continue
        candidates.append(start)
        used.append((start, end))
        if len(candidates) >= n_windows:
            break
    return candidates


def run_gate(universe: Optional[list[str]] = None, total_days: int = 900) -> dict:
    universe = universe or DEFAULT_UNIVERSE
    print(f"[gate] universe: {universe}")
    print(f"[gate] fetching {total_days} days of daily returns via yfinance...")
    returns = _fetch_returns(universe, total_days=total_days)
    print(f"[gate] got {len(returns)} rows × {returns.shape[1]} cols, "
          f"range {returns.index[0].date()} → {returns.index[-1].date()}")

    ref_days = CRITERIA["ref_days"]
    roll = CRITERIA["roll"]
    if len(returns) < 2 * ref_days + 90:
        raise SystemExit(
            f"Need at least {2 * ref_days + 90} days for fit-stability (C4); "
            f"got {len(returns)}. Increase total_days or run later."
        )

    ref_arr = returns.iloc[:ref_days].values
    holdout_arr = returns.iloc[ref_days:].values
    print(f"[gate] ref_days={ref_days}, holdout_days={len(holdout_arr)}")

    # ---------- Fit MPS (timing → C5) ----------
    print("[gate] fitting MPS (this is the C5 timing run)...")
    mps = MPSBornMachine(chi=CRITERIA["chi"], d=CRITERIA["d"])
    t0 = time.time()
    mps.fit(ref_arr, n_restarts=CRITERIA["fit_n_restarts"],
            max_iter=CRITERIA["fit_max_iter"])
    fit_seconds = time.time() - t0
    print(f"[gate] MPS fit done in {fit_seconds:.1f}s  ({mps.n_params()} params)")

    # ---------- Fit t-copula baseline ----------
    print("[gate] fitting t-copula baseline...")
    tc = TCopula()
    tc.fit(ref_arr)
    print(f"[gate] t-copula df={tc.df}")

    # ---------- Null distributions ----------
    mps_mu, mps_sd = _fit_null(mps, ref_arr, roll)
    tc_mu, tc_sd = _fit_null(tc, ref_arr, roll)
    print(f"[gate] nulls  MPS μ={mps_mu:.2f} σ={mps_sd:.2f}   "
          f"TC μ={tc_mu:.2f} σ={tc_sd:.2f}")

    # ---------- Score holdout ----------
    mps_ll_holdout = mps.log_prob(holdout_arr)
    tc_ll_holdout = tc.log_prob(holdout_arr)
    mps_z = _rolling_z(mps_ll_holdout, roll, mps_mu, mps_sd)
    tc_z = _rolling_z(tc_ll_holdout, roll, tc_mu, tc_sd)
    print(f"[gate] holdout z-scores: MPS n={len(mps_z)}, TC n={len(tc_z)}")

    # ---------- C1: corroboration ----------
    tc_thresh = CRITERIA["tcopula_z_thresh"]
    mps_thresh = CRITERIA["mps_z_thresh"]
    tc_events = np.abs(tc_z) >= tc_thresh
    mps_events = np.abs(mps_z) >= mps_thresh
    n_tc = int(tc_events.sum())
    if n_tc == 0:
        c1_pct = float("nan")
        c1_pass = False
        c1_note = "t-copula fired no events on holdout — cannot evaluate corroboration"
    else:
        c1_pct = float((tc_events & mps_events).sum() / n_tc)
        c1_pass = c1_pct >= CRITERIA["C1_corroboration_pct_min"]
        c1_note = f"{int((tc_events & mps_events).sum())}/{n_tc} t-copula events corroborated"

    # ---------- C2: lift ----------
    n_mps = int(mps_events.sum())
    if n_mps == 0:
        c2_pct = float("nan")
        c2_pass = False
        c2_note = "MPS fired no events on holdout"
    else:
        lift_count = int((mps_events & ~tc_events).sum())
        c2_pct = float(lift_count / n_mps)
        c2_pass = c2_pct >= CRITERIA["C2_lift_pct_min"]
        c2_note = f"{lift_count}/{n_mps} MPS firings unique to MPS"

    # ---------- C3: false positives in quiet windows ----------
    quiet_starts = _pick_quiet_windows(returns, tc_z, roll, n_windows=3, win_days=30)
    c3_window_results = []
    c3_pass = True
    for s in quiet_starts:
        e = s + 30
        firings = int((np.abs(mps_z[s:e]) >= mps_thresh).sum())
        per_day = firings / 30.0
        ok = per_day <= CRITERIA["C3_fp_per_day_max"]
        c3_window_results.append({
            "start_idx": s, "end_idx": e, "firings": firings,
            "per_day": round(per_day, 3), "pass": ok,
        })
        if not ok:
            c3_pass = False
    if len(quiet_starts) < 3:
        c3_pass = False  # could not find 3 windows

    # ---------- C4: fit stability via KL ----------
    print("[gate] fit stability check: refitting on second window...")
    second_start = ref_days + 90
    if second_start + ref_days > len(returns):
        c4_kl = float("inf")
        c4_pass = False
        c4_note = "not enough data for disjoint second window"
    else:
        second_arr = returns.iloc[second_start: second_start + ref_days].values
        mps2 = MPSBornMachine(chi=CRITERIA["chi"], d=CRITERIA["d"])
        mps2.fit(second_arr, n_restarts=CRITERIA["fit_n_restarts"],
                 max_iter=CRITERIA["fit_max_iter"])
        c4_kl = _kl_via_sampling(mps, mps2, n_samples=10000)
        c4_pass = abs(c4_kl) <= CRITERIA["C4_kl_max"]
        c4_note = f"KL(MPS_ref || MPS_second) ≈ {c4_kl:.3f} nats"

    # ---------- C5: compute budget ----------
    c5_pass = fit_seconds <= CRITERIA["C5_fit_seconds_max"]

    # ---------- Verdict ----------
    overall_pass = all([c1_pass, c2_pass, c3_pass, c4_pass, c5_pass])
    return {
        "criteria": CRITERIA,
        "fit_seconds": fit_seconds,
        "n_params": mps.n_params(),
        "tcopula_df": tc.df,
        "mps_null_mu": mps_mu, "mps_null_sd": mps_sd,
        "tc_null_mu": tc_mu, "tc_null_sd": tc_sd,
        "n_holdout_z_obs": len(mps_z),
        "tc_events_holdout": n_tc,
        "mps_events_holdout": n_mps,
        "C1": {"pass": c1_pass, "value": c1_pct, "threshold": CRITERIA["C1_corroboration_pct_min"], "note": c1_note},
        "C2": {"pass": c2_pass, "value": c2_pct, "threshold": CRITERIA["C2_lift_pct_min"], "note": c2_note},
        "C3": {"pass": c3_pass, "windows": c3_window_results, "threshold": CRITERIA["C3_fp_per_day_max"]},
        "C4": {"pass": c4_pass, "value": c4_kl, "threshold": CRITERIA["C4_kl_max"], "note": c4_note},
        "C5": {"pass": c5_pass, "value": fit_seconds, "threshold": CRITERIA["C5_fit_seconds_max"]},
        "overall_pass": overall_pass,
        "timestamp": pd.Timestamp.utcnow().isoformat(),
    }


def write_verdict(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    verdict = "PASS" if result["overall_pass"] else "REJECT"
    lines = [
        f"# tn_anomaly Gate Verdict — {verdict}",
        "",
        f"Run timestamp: {result['timestamp']}",
        f"Gate spec: docs/tn_anomaly_gate.md",
        "",
        "## Summary",
        "",
        f"- MPS fit: {result['n_params']} params, {result['fit_seconds']:.1f}s",
        f"- t-copula df: {result['tcopula_df']}",
        f"- Holdout obs: {result['n_holdout_z_obs']}",
        f"- t-copula events (|z|≥{result['criteria']['tcopula_z_thresh']}): {result['tc_events_holdout']}",
        f"- MPS events (|z|≥{result['criteria']['mps_z_thresh']}): {result['mps_events_holdout']}",
        "",
        "## Per-criterion",
        "",
        f"### C1 — Corroboration with t-copula",
        f"- Required: ≥ {result['C1']['threshold']*100:.0f}% of t-copula events corroborated",
        f"- Observed: {result['C1']['value']*100 if result['C1']['value']==result['C1']['value'] else float('nan'):.1f}%  ({result['C1']['note']})",
        f"- **{'PASS' if result['C1']['pass'] else 'FAIL'}**",
        "",
        f"### C2 — Lift over t-copula",
        f"- Required: ≥ {result['C2']['threshold']*100:.0f}% of MPS firings unique to MPS",
        f"- Observed: {result['C2']['value']*100 if result['C2']['value']==result['C2']['value'] else float('nan'):.1f}%  ({result['C2']['note']})",
        f"- **{'PASS' if result['C2']['pass'] else 'FAIL'}**",
        "",
        f"### C3 — False positives in quiet windows",
        f"- Required: ≤ {result['C3']['threshold']}/day in each of 3 quiet 30-day windows",
        "- Observed:",
    ]
    for w in result["C3"]["windows"]:
        lines.append(f"    - window @{w['start_idx']}: {w['firings']} firings ({w['per_day']}/day) → {'PASS' if w['pass'] else 'FAIL'}")
    if not result["C3"]["windows"]:
        lines.append("    - (no quiet windows found)")
    lines += [
        f"- **{'PASS' if result['C3']['pass'] else 'FAIL'}**",
        "",
        f"### C4 — Fit stability (KL)",
        f"- Required: KL ≤ {result['C4']['threshold']} nats between disjoint refits",
        f"- Observed: {result['C4']['note']}",
        f"- **{'PASS' if result['C4']['pass'] else 'FAIL'}**",
        "",
        f"### C5 — Compute budget",
        f"- Required: ≤ {result['C5']['threshold']}s for full fit on 252 days × 8 assets",
        f"- Observed: {result['C5']['value']:.1f}s",
        f"- **{'PASS' if result['C5']['pass'] else 'FAIL'}**",
        "",
        "---",
        "",
        f"## Verdict: **{verdict}**",
        "",
    ]
    if verdict == "PASS":
        lines.append("Integrate per docs/tn_anomaly_gate.md scope.")
    else:
        failed = [k for k in ("C1", "C2", "C3", "C4", "C5") if not result[k]["pass"]]
        lines.append(f"Failing criteria: {', '.join(failed)}.")
        lines.append("Detector is rejected. No partial integration. The runtime stays as-is.")
    path.write_text("\n".join(lines))
    print(f"[gate] verdict written → {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--total-days", type=int, default=900)
    parser.add_argument("--out", default="gate/results/tn_anomaly_verdict.md")
    parser.add_argument("--json-out", default="gate/results/tn_anomaly_result.json")
    args = parser.parse_args()

    result = run_gate(total_days=args.total_days)

    # Persist raw + markdown
    json_path = Path(args.json_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"[gate] raw result → {json_path}")

    write_verdict(result, Path(args.out))


if __name__ == "__main__":
    main()
