"""Grid-sweep z_thresh × cusum_h on a replay CSV and emit a comparison table.

Loads ticks once, runs the detector synchronously per param combo.

    python -m app.sweep --input /tmp/hft_data/sample.csv
    python -m app.sweep --input /tmp/hft_data/sample.csv --alpha 0.01 --warmup-ticks 10
"""
import argparse
import csv
from typing import List, Tuple

from .detector import Alert, PerSymbolDetector, Router


def load_ticks(path: str) -> List[Tuple[int, str, float]]:
    out: List[Tuple[int, str, float]] = []
    with open(path, "r", newline="") as f:
        for row in csv.DictReader(f):
            out.append((int(row["ts_ns"]), row["symbol"], float(row["price"])))
    return out


def run_detector(
    ticks: List[Tuple[int, str, float]],
    alpha: float,
    z_thresh: float,
    cusum_h: float,
    cusum_k: float,
    session_gap_sec: float,
    warmup_ticks: int,
    min_obs: int = 0,
) -> List[Alert]:
    def factory(sym: str) -> PerSymbolDetector:
        return PerSymbolDetector(
            sym, alpha=alpha, z_thresh=z_thresh, cusum_h=cusum_h, cusum_k=cusum_k,
            session_gap_sec=session_gap_sec, warmup_ticks=warmup_ticks,
            min_obs=min_obs,
        )
    router = Router(factory=factory)
    alerts: List[Alert] = []
    for ts_ns, sym, price in ticks:
        alerts.extend(router.on_tick(sym, price, ts_ns, ts_ns))
    return alerts


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--alpha", type=float, default=0.02)
    p.add_argument("--cusum-k", type=float, default=0.5)
    p.add_argument("--session-gap-sec", type=float, default=600.0)
    p.add_argument("--warmup-ticks", type=int, default=5)
    # Default 0 preserves historic sweep behaviour — configs/minute_bars.env's
    # knee was found without a min_obs gate, so changing the default here would
    # silently invalidate its provenance. Live runs set their own (state.py: 100,
    # configs/crypto.env: 200); pass this explicitly to match a live profile.
    p.add_argument("--min-obs", type=int, default=0,
                   help="min observations before a detector may fire (default 0 = sweep legacy)")
    p.add_argument("--z-grid", default="3.0,3.5,4.0,4.5,5.0,6.0,8.0")
    p.add_argument("--h-grid", default="4.0,5.0,6.0,8.0,10.0")
    args = p.parse_args()

    ticks = load_ticks(args.input)
    n = len(ticks)
    symbols = sorted({t[1] for t in ticks})
    sym_hours = len(symbols) * (ticks[-1][0] - ticks[0][0]) / 3.6e12

    print(f"input:        {args.input}")
    print(f"ticks:        {n:,}   symbols: {symbols}   span: {sym_hours:.1f} sym·hr")
    print(f"fixed params: alpha={args.alpha}  cusum_k={args.cusum_k}  "
          f"session_gap={int(args.session_gap_sec)}s  warmup={args.warmup_ticks}  "
          f"min_obs={args.min_obs}")
    print()

    z_grid = [float(x) for x in args.z_grid.split(",")]
    h_grid = [float(x) for x in args.h_grid.split(",")]

    header = (
        f"{'z_thresh':>8}  {'cusum_h':>7}  {'alerts':>6}  {'%ticks':>7}  "
        f"{'z':>4}  {'cusum':>5}  {'max|s|':>6}  {'per_sym_hr':>10}"
    )
    print(header)
    print("-" * len(header))

    for z in z_grid:
        for h in h_grid:
            alerts = run_detector(
                ticks, args.alpha, z, h, args.cusum_k,
                args.session_gap_sec, args.warmup_ticks, args.min_obs,
            )
            n_z = sum(1 for a in alerts if a.kind == "robust_z")
            n_c = sum(1 for a in alerts if a.kind == "cusum")
            max_s = max((abs(a.score) for a in alerts), default=0.0)
            pct = 100 * len(alerts) / n
            per = len(alerts) / sym_hours if sym_hours > 0 else 0
            print(
                f"{z:>8.1f}  {h:>7.1f}  {len(alerts):>6d}  {pct:>6.2f}%  "
                f"{n_z:>4d}  {n_c:>5d}  {max_s:>6.2f}  {per:>10.2f}"
            )
        print()


if __name__ == "__main__":
    main()
