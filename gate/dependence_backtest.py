"""Walk-forward retrospective backtest of the t-copula dependence_shift detector
on cached returns. Refits periodically, scores rolling 5-day LL, emits alerts
when |z| >= threshold.

    python -m gate.dependence_backtest \\
        --returns gate/data/crypto8_returns.csv \\
        --symbol BTC-USD \\
        --out gate/data/dependence_alerts.csv

`--symbol` is the ticker to attribute cross-asset alerts to (matches our
event catalog). Multiple symbols can be given with --symbols and one alert
per constituent is emitted (useful if the event catalog is multi-asset).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Make the service's batch.copulas importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "services" / "hft_anomaly_service"))
from app.batch.copulas import TCopula  # noqa: E402


def run(
    returns: pd.DataFrame,
    ref_days: int = 252,
    roll: int = 5,
    refit_every: int = 20,
    z_thresh: float = 3.0,
    symbols_attribution: list[str] | None = None,
) -> list[dict]:
    """Sliding-window retrospective scoring. For each test day t:
        ref = returns[t-ref_days:t-roll]
        score = rolling 5-day LL over returns[t-roll:t]
    Refits only every `refit_every` days (mirrors a nightly refit in prod).
    """
    alerts: list[dict] = []
    N = len(returns)
    if N < ref_days + roll:
        raise SystemExit(f"need at least {ref_days + roll} rows; got {N}")

    cop: TCopula | None = None
    null_mu = null_sd = 0.0
    last_fit_idx = -10_000

    for t in range(ref_days + roll, N + 1):
        # Refit if needed — use the most recent `ref_days` pre-eval
        if t - last_fit_idx >= refit_every:
            ref = returns.iloc[t - ref_days - roll : t - roll].values
            cop = TCopula()
            try:
                cop.fit(ref)
            except Exception as e:
                print(f"  fit failed at idx {t}: {e}")
                continue
            ref_ll = cop.log_prob(ref)
            rolling = np.array([ref_ll[i : i + roll].sum()
                                for i in range(len(ref_ll) - roll + 1)])
            null_mu = float(rolling.mean())
            null_sd = float(rolling.std(ddof=1))
            last_fit_idx = t

        if cop is None or null_sd < 1e-12:
            continue
        recent = returns.iloc[t - roll : t].values
        ll = float(cop.log_prob(recent).sum())
        z = (ll - null_mu) / null_sd
        if abs(z) >= z_thresh:
            ts_ns = int(pd.Timestamp(returns.index[t - 1]).timestamp() * 1e9)
            for sym in (symbols_attribution or [",".join(returns.columns)]):
                alerts.append({
                    "ts_ns": ts_ns,
                    "symbol": sym,
                    "kind": "dependence_shift",
                    "price": ll,
                    "score": z,
                })
    return alerts


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--returns", required=True)
    p.add_argument("--symbol", default=None,
                   help="single attribution symbol (e.g. BTC-USD to match events)")
    p.add_argument("--symbols", default=None,
                   help="comma-separated attribution symbols (one alert per ticker)")
    p.add_argument("--out", required=True)
    p.add_argument("--ref-days", type=int, default=252)
    p.add_argument("--roll", type=int, default=5)
    p.add_argument("--refit-every", type=int, default=20)
    p.add_argument("--z-thresh", type=float, default=3.0)
    args = p.parse_args()

    returns = pd.read_csv(args.returns, index_col=0, parse_dates=True)
    print(f"loaded {len(returns):,} rows × {len(returns.columns)} cols "
          f"({returns.index.min().date()} → {returns.index.max().date()})")

    syms: list[str] | None = None
    if args.symbols:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.symbol:
        syms = [args.symbol]

    alerts = run(returns, ref_days=args.ref_days, roll=args.roll,
                 refit_every=args.refit_every, z_thresh=args.z_thresh,
                 symbols_attribution=syms)
    print(f"emitted {len(alerts)} dependence_shift alerts")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts_ns", "symbol", "kind", "price", "score"])
        w.writeheader()
        w.writerows(alerts)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
