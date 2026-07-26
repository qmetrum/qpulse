from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .features import (
    BurstDetector, CUSUM, MicropriceStats, OBIStats, QuoteRateStats,
    RegimeDetector, SpreadStats, StreamingStats, VolumeStats,
)
from .ring_buffer import RingBuffer


@dataclass(slots=True)
class Alert:
    symbol: str
    ts_ns: int
    price: float
    kind: str
    score: float
    ingest_ns: int
    # Structured context for explainability - populated per detector kind.
    # Examples: {"baseline_mean": 1.2, "baseline_mad": 0.3, "z_threshold": 4.0}
    details: Optional[Dict[str, Any]] = None


class PerSymbolDetector:
    __slots__ = (
        "symbol", "buf", "stats", "cusum", "z_thresh",
        "spread_stats", "spread_z_thresh",
        "obi_stats", "obi_extreme_thresh", "obi_rearm_thresh", "_obi_was_extreme",
        "qrate_stats", "qrate_z_thresh", "_qrate_cooldown_ns", "_qrate_cooldown_until_ns",
        "vol_stats", "vol_z_thresh",
        "micro_stats", "micro_z_thresh",
        "burst", "regime", "use_quote_mid",
        "last_bid", "last_ask", "last_bid_size", "last_ask_size",
    )

    def __init__(
        self,
        symbol: str,
        buffer_size: int = 4096,
        alpha: float = 0.02,
        z_thresh: float = 4.0,
        cusum_k: float = 0.5,
        cusum_h: float = 5.0,
        session_gap_sec: float = 600.0,
        warmup_ticks: int = 5,
        spread_alpha: float = 0.02,
        spread_z_thresh: float = 5.0,
        obi_alpha: float = 0.01,
        obi_extreme_thresh: float = 0.9,
        obi_rearm_thresh: float = 0.5,
        qrate_alpha: float = 0.02,
        qrate_window: int = 20,
        qrate_z_thresh: float = 5.0,
        qrate_cooldown_sec: float = 1.0,
        burst_window_sec: float = 10.0,
        burst_k_thresh: float = 3.0,
        burst_baseline_alpha: float = 0.001,
        burst_cooldown_sec: float = 10.0,
        regime_short: int = 100,
        regime_ref: int = 500,
        regime_ref_stride: int = 50,
        regime_check_every: int = 50,
        regime_ks_thresh: float = 0.25,
        regime_cooldown_sec: float = 60.0,
        use_quote_mid: bool = True,
        min_obs: int = 0,
        vol_z_thresh: float = 4.0,
        micro_z_thresh: float = 4.0,
    ):
        self.symbol = symbol
        self.buf = RingBuffer(buffer_size)
        self.stats = StreamingStats(
            alpha=alpha,
            session_gap_sec=session_gap_sec,
            warmup_ticks=warmup_ticks,
            min_obs=min_obs,
        )
        self.cusum = CUSUM(k=cusum_k, h=cusum_h)
        self.z_thresh = z_thresh
        self.spread_stats = SpreadStats(alpha=spread_alpha, min_obs=min_obs)
        self.spread_z_thresh = spread_z_thresh
        self.obi_stats = OBIStats(alpha=obi_alpha)
        self.obi_extreme_thresh = obi_extreme_thresh
        self.obi_rearm_thresh = obi_rearm_thresh
        self._obi_was_extreme = False
        self.qrate_stats = QuoteRateStats(alpha=qrate_alpha, window_size=qrate_window, min_obs=min_obs)
        self.qrate_z_thresh = qrate_z_thresh
        self._qrate_cooldown_ns = int(qrate_cooldown_sec * 1e9)
        self._qrate_cooldown_until_ns = 0
        self.burst = BurstDetector(
            window_sec=burst_window_sec,
            baseline_alpha=burst_baseline_alpha,
            k_thresh=burst_k_thresh,
            cooldown_sec=burst_cooldown_sec,
        )
        self.regime = RegimeDetector(
            short_size=regime_short,
            ref_size=regime_ref,
            ref_stride=regime_ref_stride,
            check_every=regime_check_every,
            ks_thresh=regime_ks_thresh,
            cooldown_sec=regime_cooldown_sec,
        )
        self.vol_stats = VolumeStats(alpha=alpha, min_obs=min_obs)
        self.vol_z_thresh = vol_z_thresh
        self.micro_stats = MicropriceStats(alpha=alpha, min_obs=min_obs)
        self.micro_z_thresh = micro_z_thresh
        self.use_quote_mid = use_quote_mid
        self.last_bid = 0.0
        self.last_ask = 0.0
        self.last_bid_size = 0.0
        self.last_ask_size = 0.0

    def _analyze_price(self, price: float, ts_ns: int, ingest_ns: int) -> List[Alert]:
        """Shared price-based analysis for trades (on_tick) and midprices (on_quote).
        Updates ring buffer + StreamingStats, runs cusum/robust_z/regime_shift.
        Returns any alerts (caller handles burst meta-alert)."""
        self.buf.push(price, ts_ns)
        r = self.stats.update(price, ts_ns)

        if self.stats.session_break:
            self.cusum.reset()
            return []
        if self.stats.is_warming_up() or not self.stats.is_settled():
            return []

        z = self.stats.robust_z(r)
        cusum_hit = self.cusum.update(z)

        out: List[Alert] = []
        if cusum_hit != 0.0:
            out.append(Alert(
                self.symbol, ts_ns, price, "cusum", cusum_hit, ingest_ns,
                details={
                    "cusum_h": self.cusum.h,
                    "cusum_k": self.cusum.k,
                    "log_return": r,
                    "z_in": z,
                },
            ))
        if abs(z) > self.z_thresh:
            out.append(Alert(
                self.symbol, ts_ns, price, "robust_z", z, ingest_ns,
                details={
                    "log_return": r,
                    "baseline_mean": self.stats.mean,
                    "baseline_mad": self.stats.mad,
                    "z_threshold": self.z_thresh,
                },
            ))

        regime_fire, ks = self.regime.update(r, ts_ns)
        if regime_fire:
            out.append(Alert(
                self.symbol, ts_ns, price, "regime_shift", ks, ingest_ns,
                details={
                    "ks_statistic": ks,
                    "ks_threshold": self.regime.ks_thresh,
                    "short_window": self.regime.short_size,
                    "ref_window": self.regime.ref_size,
                },
            ))

        return out

    def on_tick(self, price: float, ts_ns: int, ingest_ns: int, size: float = 0.0) -> List[Alert]:
        out = self._analyze_price(price, ts_ns, ingest_ns)

        # volume_spike detector - uses trade size if present
        if size > 0.0:
            log_size = self.vol_stats.update(size)
            if (self.vol_stats.is_settled()
                    and self.vol_stats.mad > 1e-12):
                z = self.vol_stats.robust_z(log_size)
                if z > self.vol_z_thresh:
                    import math as _m
                    baseline_size = _m.exp(self.vol_stats.mean)
                    out.append(Alert(
                        self.symbol, ts_ns, price, "volume_spike", z, ingest_ns,
                        details={
                            "size": size,
                            "baseline_mean_size": baseline_size,
                            "ratio_to_baseline": size / max(baseline_size, 1e-12),
                            "z_threshold": self.vol_z_thresh,
                        },
                    ))

        if out and self.burst.record(ts_ns, count=len(out)):
            out.append(Alert(
                self.symbol, ts_ns, price, "burst", self.burst.intensity(), ingest_ns,
                details={
                    "intensity_ratio": self.burst.intensity(),
                    "k_threshold": self.burst.k_thresh,
                    "window_sec": self.burst.window_ns / 1e9,
                    "alerts_in_window": len(self.burst.timestamps),
                },
            ))
        return out

    def on_quote(
        self,
        bid: float,
        ask: float,
        bid_size: float,
        ask_size: float,
        ts_ns: int,
        ingest_ns: int,
    ) -> List[Alert]:
        self.last_bid = bid
        self.last_ask = ask
        self.last_bid_size = bid_size
        self.last_ask_size = ask_size

        spread_bps = self.spread_stats.update(bid, ask)
        obi = self.obi_stats.update(bid_size, ask_size)
        rate = self.qrate_stats.update(ts_ns)

        mid = 0.5 * (bid + ask) if ask > 0 else 0.0

        # Phase 0.4 - treat midprice as a synthetic tick so cusum/robust_z/
        # regime_shift fire on quote-heavy feeds (Alpaca free crypto is the
        # canonical case; trades are rare or absent). Disable via
        # use_quote_mid=False on feeds with dense trades to avoid double-
        # counting in the baseline.
        if self.use_quote_mid and mid > 0:
            out: List[Alert] = self._analyze_price(mid, ts_ns, ingest_ns)
        else:
            out: List[Alert] = []

        # crossed_book - bid > ask is either bad data or extreme stress
        if bid > 0 and ask > 0 and bid > ask:
            out.append(Alert(
                self.symbol, ts_ns, mid, "crossed_book",
                bid - ask,  # cross size
                ingest_ns,
                details={"bid": bid, "ask": ask, "cross_amount": bid - ask},
            ))

        if (spread_bps > 0.0
                and self.spread_stats.mad > 1e-12
                and self.spread_stats.is_settled()):
            z = self.spread_stats.robust_z(spread_bps)
            if abs(z) > self.spread_z_thresh:
                out.append(Alert(
                    self.symbol, ts_ns, mid, "spread_widen", z, ingest_ns,
                    details={
                        "spread_bps": spread_bps,
                        "baseline_bps": self.spread_stats.mean,
                        "z_threshold": self.spread_z_thresh,
                        "bid": bid, "ask": ask,
                    },
                ))

        if obi is not None:
            obi_abs = abs(obi)
            if obi_abs > self.obi_extreme_thresh and not self._obi_was_extreme:
                self._obi_was_extreme = True
                out.append(Alert(
                    self.symbol, ts_ns, mid, "obi_extreme", obi, ingest_ns,
                    details={
                        "obi": obi,
                        "side": "bid" if obi > 0 else "ask",
                        "bid_size": bid_size,
                        "ask_size": ask_size,
                        "fire_threshold": self.obi_extreme_thresh,
                        "rearm_threshold": self.obi_rearm_thresh,
                    },
                ))
            elif obi_abs < self.obi_rearm_thresh:
                self._obi_was_extreme = False

        if (self.qrate_stats.ready
                and rate > 0
                and self.qrate_stats.mad > 1e-12
                and self.qrate_stats.is_settled()
                and ts_ns > self._qrate_cooldown_until_ns):
            z = self.qrate_stats.robust_z(rate)
            if z > self.qrate_z_thresh:
                self._qrate_cooldown_until_ns = ts_ns + self._qrate_cooldown_ns
                out.append(Alert(
                    self.symbol, ts_ns, mid, "quote_rate_spike", z, ingest_ns,
                    details={
                        "rate_qps": rate,
                        "baseline_qps": self.qrate_stats.mean,
                        "z_threshold": self.qrate_z_thresh,
                        "cooldown_sec": self._qrate_cooldown_ns / 1e9,
                    },
                ))

        # microprice_drift - informational asymmetry detector
        drift = self.micro_stats.compute_drift(bid, ask, bid_size, ask_size)
        if drift is not None:
            self.micro_stats.update(drift)
            if (self.micro_stats.is_settled()
                    and self.micro_stats.mad > 1e-12):
                z = self.micro_stats.robust_z(drift)
                if abs(z) > self.micro_z_thresh:
                    out.append(Alert(
                        self.symbol, ts_ns, mid, "microprice_drift", z, ingest_ns,
                        details={
                            "drift": drift,
                            "side": "ask" if drift > 0 else "bid",
                            "baseline_drift": self.micro_stats.mean,
                            "z_threshold": self.micro_z_thresh,
                        },
                    ))

        if out and self.burst.record(ts_ns, count=len(out)):
            out.append(Alert(
                self.symbol, ts_ns, mid, "burst", self.burst.intensity(), ingest_ns,
                details={
                    "intensity_ratio": self.burst.intensity(),
                    "k_threshold": self.burst.k_thresh,
                    "window_sec": self.burst.window_ns / 1e9,
                    "alerts_in_window": len(self.burst.timestamps),
                },
            ))

        return out


class Router:
    """One detector per symbol, created lazily."""

    __slots__ = ("detectors", "_factory")

    def __init__(self, factory=None):
        self.detectors: Dict[str, PerSymbolDetector] = {}
        self._factory = factory or (lambda sym: PerSymbolDetector(sym))

    def _get(self, symbol: str) -> PerSymbolDetector:
        det = self.detectors.get(symbol)
        if det is None:
            det = self._factory(symbol)
            self.detectors[symbol] = det
        return det

    def on_tick(self, symbol: str, price: float, ts_ns: int, ingest_ns: int, size: float = 0.0) -> List[Alert]:
        return self._get(symbol).on_tick(price, ts_ns, ingest_ns, size=size)

    def on_quote(
        self,
        symbol: str,
        bid: float,
        ask: float,
        bid_size: float,
        ask_size: float,
        ts_ns: int,
        ingest_ns: int,
    ) -> List[Alert]:
        return self._get(symbol).on_quote(bid, ask, bid_size, ask_size, ts_ns, ingest_ns)

    def latest_quote(self, symbol: str) -> Optional[dict]:
        det = self.detectors.get(symbol)
        if det is None or det.last_ask <= det.last_bid or det.last_bid <= 0:
            return None
        mid = 0.5 * (det.last_bid + det.last_ask)
        spread_bps = (det.last_ask - det.last_bid) / mid * 10000.0
        return {
            "bid": det.last_bid,
            "ask": det.last_ask,
            "bid_size": det.last_bid_size,
            "ask_size": det.last_ask_size,
            "mid": mid,
            "spread_bps": spread_bps,
            "spread_baseline_bps": det.spread_stats.mean,
        }

    def set_z_thresh(self, symbol: str, z_thresh: float) -> bool:
        det = self.detectors.get(symbol)
        if det is None:
            return False
        det.z_thresh = z_thresh
        return True

    def symbols(self) -> list[str]:
        return list(self.detectors.keys())

    def snapshot_buffer(self, symbol: str) -> "tuple[list[int], list[float]]":
        det = self.detectors.get(symbol)
        if det is None:
            return [], []
        ts, prices = det.buf.window_with_ts()
        return ts.tolist(), prices.tolist()
