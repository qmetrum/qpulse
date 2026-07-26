from dataclasses import dataclass, field
from typing import Dict, List


@dataclass(slots=True)
class RuntimeState:
    active: bool = True
    default_z_thresh: float = 4.0
    default_cusum_h: float = 5.0
    default_cusum_k: float = 0.5
    alpha: float = 0.02
    session_gap_sec: float = 600.0
    # 50 ≈ 1.5× EWMA half-life at α=0.02 → baselines stabilized before scoring.
    # Daily bars override to a smaller value (5–10) where each tick is a day.
    warmup_ticks: int = 50
    # microstructure thresholds
    spread_z_thresh: float = 5.0
    obi_alpha: float = 0.01         # longer memory than return alpha; OBI persists
    obi_extreme_thresh: float = 0.9
    obi_rearm_thresh: float = 0.5   # require |OBI| < this to re-arm after firing
    qrate_z_thresh: float = 5.0
    qrate_cooldown_sec: float = 1.0
    # trade-size & microprice detectors
    vol_z_thresh: float = 4.0
    micro_z_thresh: float = 4.0
    # burst (rolling-count meta-detector)
    burst_window_sec: float = 10.0
    burst_k_thresh: float = 3.0
    burst_baseline_alpha: float = 0.001
    burst_cooldown_sec: float = 10.0
    # regime-shift (two-sample KS)
    regime_short: int = 100
    regime_ref: int = 500
    regime_ref_stride: int = 50
    regime_check_every: int = 50
    regime_ks_thresh: float = 0.25
    regime_cooldown_sec: float = 60.0
    # Phase 0.4 - use quote midprice as synthetic tick for trade-based detectors
    use_quote_mid: bool = True
    # Minimum observations before any z-score-based scoring; protects against
    # spurious alerts during EWMA / P² baseline convergence at startup.
    # 100 ≈ 3·EWMA half-life at α=0.02, well past noise.
    min_obs: int = 100
    # Symbols list and active feed name - persisted so runtime add/remove
    # survives restarts. Empty by default; populated at first startup from
    # CLI/env.
    symbols: List[str] = field(default_factory=list)
    feed_name: str = "iex"
    # per-symbol overrides (symbol -> z_thresh)
    z_thresh_overrides: Dict[str, float] = field(default_factory=dict)

    def z_thresh_for(self, symbol: str) -> float:
        return self.z_thresh_overrides.get(symbol, self.default_z_thresh)
