import math
from typing import Optional


class StreamingStats:
    """O(1) streaming stats: EWMA mean + MAD for log-returns.

    Returns use EWMA-MAD because CUSUM handles accumulation downstream, and
    the 39.89 "self-inflation ceiling" (z ≤ 1/(1.2533·α)) is tolerable here -
    returns that cross CUSUM thresholds are flagged by that path regardless.
    For features where magnitude ranking matters (spread, quote-rate), use
    the P²-based quantile stats below instead.
    """

    __slots__ = (
        "alpha", "mean", "mad", "last_price", "last_ts_ns", "ready",
        "session_gap_ns", "warmup_ticks", "warmup_remaining", "session_break",
        "obs_count", "min_obs",
    )

    _MAD_TO_SIGMA = 1.2533141373155003

    def __init__(
        self,
        alpha: float = 0.02,
        session_gap_sec: float = 600.0,
        warmup_ticks: int = 5,
        min_obs: int = 0,
    ):
        self.alpha = alpha
        self.mean = 0.0
        self.mad = 0.0
        self.last_price = 0.0
        self.last_ts_ns = 0
        self.ready = False
        self.session_gap_ns = int(session_gap_sec * 1e9)
        self.warmup_ticks = warmup_ticks
        self.warmup_remaining = 0
        self.session_break = False
        self.obs_count = 0
        self.min_obs = min_obs

    def update(self, price: float, ts_ns: int) -> float:
        self.session_break = False

        if not self.ready:
            self.last_price = price
            self.last_ts_ns = ts_ns
            self.warmup_remaining = self.warmup_ticks
            self.ready = True
            return 0.0

        if ts_ns - self.last_ts_ns > self.session_gap_ns:
            self.last_price = price
            self.last_ts_ns = ts_ns
            self.warmup_remaining = self.warmup_ticks
            self.session_break = True
            return 0.0

        r = math.log(price / self.last_price) if price > 0 else 0.0
        self.last_price = price
        self.last_ts_ns = ts_ns
        a = self.alpha
        self.mean = (1 - a) * self.mean + a * r
        self.mad = (1 - a) * self.mad + a * abs(r - self.mean)
        if self.warmup_remaining > 0:
            self.warmup_remaining -= 1
        self.obs_count += 1
        return r

    def is_warming_up(self) -> bool:
        return self.warmup_remaining > 0

    def is_settled(self) -> bool:
        """True once we've absorbed enough observations for the EWMA baseline
        to be a meaningful reference. Use with `is_warming_up()` to gate scoring."""
        return self.obs_count >= self.min_obs

    def robust_z(self, r: float) -> float:
        if self.mad <= 1e-12:
            return 0.0
        return (r - self.mean) / (self._MAD_TO_SIGMA * self.mad)


# ---------------------------------------------------------------------------
# P² quantile estimator (Jain & Chlamtac, 1985).
#
# O(1) memory, O(1) per update, tracks a single target quantile p ∈ (0, 1)
# with five moving markers. Doesn't have EWMA-MAD's self-inflation ceiling:
# each anomaly shifts markers by at most ~1/n, so scale estimates built from
# multiple P² trackers give a stable reference even under a shock.
# ---------------------------------------------------------------------------

class P2Quantile:
    __slots__ = ("p", "n", "q", "pos", "dn", "npos", "_primed")

    def __init__(self, p: float):
        if not 0.0 < p < 1.0:
            raise ValueError("p must be in (0, 1)")
        self.p = p
        self.n = 0
        self.q = [0.0] * 5
        self.pos = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.dn = [0.0, p / 2.0, p, (1.0 + p) / 2.0, 1.0]
        self.npos = [1.0, 1.0 + 2.0 * p, 1.0 + 4.0 * p, 3.0 + 2.0 * p, 5.0]
        self._primed = False

    def update(self, x: float) -> None:
        if not self._primed:
            self.q[self.n] = x
            self.n += 1
            if self.n == 5:
                self.q.sort()
                self._primed = True
            return

        self.n += 1

        # Locate the cell k: q[k] <= x < q[k+1]
        if x < self.q[0]:
            self.q[0] = x
            k = 0
        elif x < self.q[1]:
            k = 0
        elif x < self.q[2]:
            k = 1
        elif x < self.q[3]:
            k = 2
        elif x <= self.q[4]:
            k = 3
        else:
            self.q[4] = x
            k = 3

        # Increment positions of markers k+1..4
        for i in range(k + 1, 5):
            self.pos[i] += 1.0
        # Increment desired positions for all markers
        for i in range(5):
            self.npos[i] += self.dn[i]

        # Adjust heights of internal markers (1..3) if needed
        for i in (1, 2, 3):
            d = self.npos[i] - self.pos[i]
            gap_right = self.pos[i + 1] - self.pos[i]
            gap_left = self.pos[i] - self.pos[i - 1]
            if (d >= 1.0 and gap_right > 1.0) or (d <= -1.0 and gap_left > 1.0):
                ds = 1.0 if d >= 0 else -1.0
                # Parabolic prediction
                qi = self.q[i]
                qi_p = self.q[i + 1]
                qi_m = self.q[i - 1]
                new_q = qi + (ds / (gap_right + gap_left)) * (
                    (gap_left + ds) * (qi_p - qi) / gap_right
                    + (gap_right - ds) * (qi - qi_m) / gap_left
                )
                # Monotonicity fallback: linear interpolation
                if not (qi_m < new_q < qi_p):
                    if ds > 0:
                        new_q = qi + (qi_p - qi) / gap_right
                    else:
                        new_q = qi - (qi_m - qi) / gap_left
                self.q[i] = new_q
                self.pos[i] += ds

    def value(self) -> float:
        """Current estimate of the p-th quantile. Before n>=5 returns 0.0."""
        if not self._primed:
            return 0.0
        return self.q[2]

    @property
    def primed(self) -> bool:
        return self._primed


# ---------------------------------------------------------------------------
# RobustQuantileStats - robust-z via streaming quartiles (P²).
#
# Tracks p25, p50, p75 simultaneously. robust_z = (x − p50) / (IQR / 1.35),
# where 1.35 ≈ 2·Φ⁻¹(0.75) makes IQR approximately σ under a Gaussian.
# No self-inflation ceiling: the markers absorb new observations smoothly.
# ---------------------------------------------------------------------------

class RobustQuantileStats:
    """Drop-in replacement for EWMA-MAD feature trackers where triage-ranking
    matters. Same (update, robust_z) interface as SpreadStats/QuoteRateStats.
    """

    __slots__ = ("q25", "q50", "q75", "ready")
    _IQR_TO_SIGMA = 1.34898

    def __init__(self):
        self.q25 = P2Quantile(0.25)
        self.q50 = P2Quantile(0.50)
        self.q75 = P2Quantile(0.75)
        self.ready = False

    def update(self, x: float) -> float:
        self.q25.update(x)
        self.q50.update(x)
        self.q75.update(x)
        if not self.ready and self.q50.primed:
            self.ready = True
        return x

    def robust_z(self, x: float) -> float:
        if not self.ready:
            return 0.0
        iqr = self.q75.value() - self.q25.value()
        if iqr <= 1e-12:
            return 0.0
        return (x - self.q50.value()) / (iqr / self._IQR_TO_SIGMA)


# ---------------------------------------------------------------------------
# SpreadStats - now P² based (no self-inflation ceiling).
# Keeps the (update, robust_z) API identical to the EWMA-MAD version.
# ---------------------------------------------------------------------------

class SpreadStats:
    """Streaming bid-ask spread (in bps) robust-z via P²-tracked quartiles.

    Feed every two-sided quote; `robust_z` returns a Gaussian-equivalent
    z-score without the EWMA-MAD self-inflation ceiling.
    """

    __slots__ = ("_rq", "obs_count", "min_obs")

    def __init__(self, alpha: float = 0.02, min_obs: int = 0):
        _ = alpha  # kept for backward-compat with factory code; P² path
        self._rq = RobustQuantileStats()
        self.obs_count = 0
        self.min_obs = min_obs

    def update(self, bid: float, ask: float) -> float:
        if bid <= 0.0 or ask <= 0.0 or ask <= bid:
            return 0.0
        mid = 0.5 * (bid + ask)
        spread_bps = (ask - bid) / mid * 10000.0
        self._rq.update(spread_bps)
        self.obs_count += 1
        return spread_bps

    def robust_z(self, spread_bps: float) -> float:
        return self._rq.robust_z(spread_bps)

    def is_settled(self) -> bool:
        return self.obs_count >= self.min_obs

    # Expose simple reflection for API/debug
    @property
    def mean(self) -> float:
        return self._rq.q50.value() if self._rq.ready else 0.0

    @property
    def mad(self) -> float:
        # Present a MAD-compatible "scale" for consumers that still check > 0
        if not self._rq.ready:
            return 0.0
        iqr = self._rq.q75.value() - self._rq.q25.value()
        return iqr / RobustQuantileStats._IQR_TO_SIGMA


class OBIStats:
    """Order-Book Imbalance: (bid_size - ask_size) / (bid_size + ask_size) ∈ [-1, 1].

    Kept on EWMA-MAD because OBI is bounded and the detector uses a binary
    fire/rearm threshold, not magnitude ranking. `alpha` is exposed separately
    in the detector (obi_alpha) because OBI has a longer characteristic
    timescale than return noise.
    """

    __slots__ = ("alpha", "mean", "mad", "last_obi", "ready")
    _MAD_TO_SIGMA = 1.2533141373155003

    def __init__(self, alpha: float = 0.01):
        self.alpha = alpha
        self.mean = 0.0
        self.mad = 0.0
        self.last_obi = 0.0
        self.ready = False

    def update(self, bid_size: float, ask_size: float) -> Optional[float]:
        if bid_size <= 0.0 or ask_size <= 0.0:
            return None
        total = bid_size + ask_size
        obi = (bid_size - ask_size) / total
        self.last_obi = obi
        if not self.ready:
            self.mean = obi
            self.ready = True
            return obi
        a = self.alpha
        self.mean = (1 - a) * self.mean + a * obi
        self.mad = (1 - a) * self.mad + a * abs(obi - self.mean)
        return obi

    def robust_z(self, obi: float) -> float:
        if self.mad <= 1e-12:
            return 0.0
        return (obi - self.mean) / (self._MAD_TO_SIGMA * self.mad)


class QuoteRateStats:
    """Streaming quote-arrival rate (events/sec) robust-z via P²-tracked quartiles.

    Keeps a rolling window of the last `window_size` timestamps; rate =
    window_size / (latest - first). Baseline via P² - no EWMA-MAD ceiling.
    """

    __slots__ = ("window_size", "timestamps", "_rq", "obs_count", "min_obs")

    def __init__(self, alpha: float = 0.02, window_size: int = 20, min_obs: int = 0):
        from collections import deque as _deque
        _ = alpha
        self.window_size = window_size
        self.timestamps = _deque(maxlen=window_size)
        self._rq = RobustQuantileStats()
        self.obs_count = 0
        self.min_obs = min_obs

    def update(self, ts_ns: int) -> float:
        self.timestamps.append(ts_ns)
        if len(self.timestamps) < self.window_size:
            return 0.0
        span_ns = self.timestamps[-1] - self.timestamps[0]
        if span_ns <= 0:
            return 0.0
        rate = self.window_size / (span_ns / 1e9)
        self._rq.update(rate)
        self.obs_count += 1
        return rate

    def robust_z(self, rate: float) -> float:
        return self._rq.robust_z(rate)

    def is_settled(self) -> bool:
        return self.obs_count >= self.min_obs

    @property
    def ready(self) -> bool:
        return self._rq.ready

    @property
    def mean(self) -> float:
        return self._rq.q50.value() if self._rq.ready else 0.0

    @property
    def mad(self) -> float:
        if not self._rq.ready:
            return 0.0
        iqr = self._rq.q75.value() - self._rq.q25.value()
        return iqr / RobustQuantileStats._IQR_TO_SIGMA


class BurstDetector:
    """Rolling alert-count burst detector - the degenerate Hawkes case.

    Tracks how many alerts (across all kinds) have fired on this symbol in
    the last `window_sec`. Maintains an EWMA baseline of that count. When the
    current count exceeds `k_thresh × baseline` AND passes the cooldown, a
    burst is emitted. This is equivalent to a Hawkes point process with a
    rectangular kernel - simpler to fit (no parameters to MLE), 80% of the
    suppression benefit. Upgrade to exponential-kernel Hawkes later if the
    clustering signal needs sharper temporal resolution.
    """

    __slots__ = (
        "window_ns", "baseline_alpha", "timestamps",
        "baseline_count", "k_thresh", "cooldown_ns", "_cooldown_until",
    )

    def __init__(
        self,
        window_sec: float = 10.0,
        baseline_alpha: float = 0.001,   # very slow so bursts don't drag baseline up
        k_thresh: float = 3.0,
        cooldown_sec: float = 10.0,
    ):
        from collections import deque as _deque
        self.window_ns = int(window_sec * 1e9)
        self.baseline_alpha = baseline_alpha
        self.timestamps = _deque()
        self.baseline_count = 0.0
        self.k_thresh = k_thresh
        self.cooldown_ns = int(cooldown_sec * 1e9)
        self._cooldown_until = 0

    def record(self, ts_ns: int, count: int = 1) -> bool:
        """Register `count` alert events at `ts_ns`. Returns True if a burst
        should be emitted (cooldown-respecting)."""
        for _ in range(count):
            self.timestamps.append(ts_ns)
        cutoff = ts_ns - self.window_ns
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()
        n = len(self.timestamps)
        a = self.baseline_alpha
        self.baseline_count = (1 - a) * self.baseline_count + a * n
        threshold = self.k_thresh * max(self.baseline_count, 1.0)
        if n > threshold and ts_ns > self._cooldown_until:
            self._cooldown_until = ts_ns + self.cooldown_ns
            return True
        return False

    def intensity(self) -> float:
        """Ratio of current window count over baseline (≥ 1 means elevated)."""
        if self.baseline_count <= 0:
            return float(len(self.timestamps))
        return len(self.timestamps) / max(self.baseline_count, 1.0)


def _ks_2samp(s1, s2) -> float:
    """Two-sample Kolmogorov-Smirnov statistic (max |F1 − F2| across values).

    Both inputs are iterable of floats (typically deque snapshots). O(n log n)
    via numpy sort + searchsorted. Returns D ∈ [0, 1].
    """
    import numpy as _np
    a = _np.sort(_np.fromiter(s1, dtype=_np.float64))
    b = _np.sort(_np.fromiter(s2, dtype=_np.float64))
    if a.size == 0 or b.size == 0:
        return 0.0
    allv = _np.concatenate((a, b))
    c1 = _np.searchsorted(a, allv, side="right") / a.size
    c2 = _np.searchsorted(b, allv, side="right") / b.size
    return float(_np.max(_np.abs(c1 - c2)))


class RegimeDetector:
    """Streaming two-sample KS regime-shift detector.

    Compares the distribution of recent returns (`short_size`) against a
    subsampled reference window (`ref_size × ref_stride` ticks of history).
    Every `check_every` ticks, runs KS; if D > `ks_thresh`, emits a
    regime_shift. Catches ensemble shifts (volatility doubling, heavy-tail
    emergence) that single-tick z-scores miss.

    Critical value at α=0.05 with n1=100, n2=500:
        D ≈ 1.36 · √((n1+n2)/(n1·n2)) ≈ 0.15
    We default `ks_thresh=0.25` - roughly the 1% significance level.
    """

    __slots__ = (
        "short_buf", "ref_buf", "short_size", "ref_size",
        "ref_stride", "_ref_counter", "_tick_count", "check_every",
        "ks_thresh", "cooldown_ns", "_cooldown_until_ns", "last_ks",
    )

    def __init__(
        self,
        short_size: int = 100,
        ref_size: int = 500,
        ref_stride: int = 50,
        check_every: int = 50,
        ks_thresh: float = 0.25,
        cooldown_sec: float = 60.0,
    ):
        from collections import deque as _deque
        self.short_buf = _deque(maxlen=short_size)
        self.ref_buf = _deque(maxlen=ref_size)
        self.short_size = short_size
        self.ref_size = ref_size
        self.ref_stride = ref_stride
        self._ref_counter = 0
        self._tick_count = 0
        self.check_every = check_every
        self.ks_thresh = ks_thresh
        self.cooldown_ns = int(cooldown_sec * 1e9)
        self._cooldown_until_ns = 0
        self.last_ks = 0.0

    def update(self, r: float, ts_ns: int) -> "tuple[bool, float]":
        """Feed a return; return (fire_regime_shift, last_ks_stat)."""
        self.short_buf.append(r)
        self._ref_counter += 1
        if self._ref_counter >= self.ref_stride:
            self._ref_counter = 0
            self.ref_buf.append(r)

        self._tick_count += 1
        if (self._tick_count < self.check_every
                or len(self.short_buf) < self.short_size
                or len(self.ref_buf) < self.ref_size):
            return False, self.last_ks
        if ts_ns <= self._cooldown_until_ns:
            self._tick_count = 0
            return False, self.last_ks

        self._tick_count = 0
        ks = _ks_2samp(self.short_buf, self.ref_buf)
        self.last_ks = ks
        if ks > self.ks_thresh:
            self._cooldown_until_ns = ts_ns + self.cooldown_ns
            return True, ks
        return False, ks


class VolumeStats:
    """Streaming trade-size baseline. Robust-z on each new trade's size to
    detect institutional prints, sweeps, news reactions. Uses EWMA-MAD on
    log(size) so the score is scale-free.
    """

    __slots__ = ("alpha", "mean", "mad", "obs_count", "min_obs", "ready")
    _MAD_TO_SIGMA = 1.2533141373155003

    def __init__(self, alpha: float = 0.02, min_obs: int = 0):
        self.alpha = alpha
        self.mean = 0.0
        self.mad = 0.0
        self.obs_count = 0
        self.min_obs = min_obs
        self.ready = False

    def update(self, size: float) -> float:
        """Feed a trade size. Returns log-size for scoring; 0.0 if size<=0."""
        import math as _m
        if size <= 0.0:
            return 0.0
        ls = _m.log(size)
        if not self.ready:
            self.mean = ls
            self.ready = True
            self.obs_count += 1
            return ls
        a = self.alpha
        self.mean = (1 - a) * self.mean + a * ls
        self.mad = (1 - a) * self.mad + a * abs(ls - self.mean)
        self.obs_count += 1
        return ls

    def robust_z(self, log_size: float) -> float:
        if self.mad <= 1e-12:
            return 0.0
        return (log_size - self.mean) / (self._MAD_TO_SIGMA * self.mad)

    def is_settled(self) -> bool:
        return self.obs_count >= self.min_obs


class MicropriceStats:
    """Microprice drift detector.

    microprice = (bid·ask_size + ask·bid_size) / (bid_size + ask_size)
    drift = (microprice − mid) / spread  ∈ [-1, +1]

    Drift positive → microprice biased toward ask (buying pressure).
    EWMA-MAD baseline; robust-z on the drift value detects sustained
    informational asymmetry before any trade prints.
    """

    __slots__ = ("alpha", "mean", "mad", "obs_count", "min_obs", "ready")
    _MAD_TO_SIGMA = 1.2533141373155003

    def __init__(self, alpha: float = 0.02, min_obs: int = 0):
        self.alpha = alpha
        self.mean = 0.0
        self.mad = 0.0
        self.obs_count = 0
        self.min_obs = min_obs
        self.ready = False

    def compute_drift(self, bid: float, ask: float, bid_size: float, ask_size: float) -> "Optional[float]":
        """Returns drift signal or None if quote is invalid / one-sided."""
        if bid_size <= 0.0 or ask_size <= 0.0 or bid <= 0 or ask <= bid:
            return None
        total = bid_size + ask_size
        microprice = (bid * ask_size + ask * bid_size) / total
        mid = 0.5 * (bid + ask)
        spread = ask - bid
        if spread <= 0.0:
            return None
        return (microprice - mid) / spread

    def update(self, drift: float) -> float:
        if not self.ready:
            self.mean = drift
            self.ready = True
            self.obs_count += 1
            return drift
        a = self.alpha
        self.mean = (1 - a) * self.mean + a * drift
        self.mad = (1 - a) * self.mad + a * abs(drift - self.mean)
        self.obs_count += 1
        return drift

    def robust_z(self, drift: float) -> float:
        if self.mad <= 1e-12:
            return 0.0
        return (drift - self.mean) / (self._MAD_TO_SIGMA * self.mad)

    def is_settled(self) -> bool:
        return self.obs_count >= self.min_obs


class CUSUM:
    """Two-sided CUSUM on the standardized residual."""

    __slots__ = ("k", "h", "s_pos", "s_neg")

    def __init__(self, k: float = 0.5, h: float = 5.0):
        self.k = k
        self.h = h
        self.s_pos = 0.0
        self.s_neg = 0.0

    def update(self, z: float) -> float:
        self.s_pos = max(0.0, self.s_pos + z - self.k)
        self.s_neg = min(0.0, self.s_neg + z + self.k)
        if self.s_pos > self.h:
            out = self.s_pos
            self.s_pos = 0.0
            return out
        if -self.s_neg > self.h:
            out = self.s_neg
            self.s_neg = 0.0
            return out
        return 0.0

    def reset(self) -> None:
        self.s_pos = 0.0
        self.s_neg = 0.0
