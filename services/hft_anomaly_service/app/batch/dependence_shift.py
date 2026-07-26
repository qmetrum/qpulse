"""Cross-asset dependence-shift detector (Qpulse batch path).

Fits a t-copula on trailing `ref_days` daily log-returns of a configured
asset universe, computes a null distribution of rolling-`roll`-day log-
likelihoods on that reference window, then every `score_interval_sec`
scores the most-recent rolling window against the null. If |z| exceeds
`z_thresh`, emits a `dependence_shift` alert to the same bus as the
hot-path detectors - so it flows to WS / SQLite / webhook uniformly.

Refits once per `refit_interval_sec` (default 24h). Pulls data via
yfinance each refit. Fully opt-in: set `HFT_BATCH_ENABLED=true`.

Method choice (t-copula) comes from the validated gate:
see gate/results/verdict.md.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

from ..bus import Bus
from ..detector import Alert
from .copulas import TCopula


DEFAULT_UNIVERSE = ["XLK", "XLF", "XLE", "XLV", "SPY", "TLT", "GLD", "HYG"]


def _fetch_daily_returns(tickers: List[str], lookback_days: int) -> pd.DataFrame:
    import yfinance as yf
    start = (pd.Timestamp.utcnow() - pd.Timedelta(days=int(lookback_days * 1.6) + 60)).strftime("%Y-%m-%d")
    raw = yf.download(
        tickers=" ".join(tickers),
        start=start, auto_adjust=True, progress=False,
        group_by="ticker", threads=True,
    )
    if isinstance(raw.columns, pd.MultiIndex):
        closes = {}
        for t in tickers:
            if t in raw.columns.get_level_values(0):
                sub = raw[t]
                if "Close" in sub.columns:
                    closes[t] = sub["Close"]
        prices = pd.DataFrame(closes)
    else:
        prices = raw[["Close"]].rename(columns={"Close": tickers[0]})
    prices = prices.reindex(columns=tickers).ffill(limit=2).dropna()
    returns = np.log(prices / prices.shift(1)).dropna(how="all")
    return returns.iloc[-lookback_days:] if len(returns) > lookback_days else returns


class DependenceShiftDetector:
    __slots__ = (
        "bus", "universe", "ref_days", "roll", "refit_interval_sec",
        "score_interval_sec", "z_thresh",
        "_copula", "_null_mu", "_null_sd", "_last_refit", "_last_score",
        "_recent_returns", "_stop", "_alerts_emitted",
    )

    def __init__(
        self,
        bus: Bus,
        universe: Optional[List[str]] = None,
        ref_days: int = 252,
        roll: int = 5,
        refit_interval_sec: float = 86400.0,   # 1 day
        score_interval_sec: float = 3600.0,    # 1 hour
        z_thresh: float = 3.0,
    ):
        self.bus = bus
        self.universe = universe or DEFAULT_UNIVERSE
        self.ref_days = ref_days
        self.roll = roll
        self.refit_interval_sec = refit_interval_sec
        self.score_interval_sec = score_interval_sec
        self.z_thresh = z_thresh
        self._copula: Optional[TCopula] = None
        self._null_mu = 0.0
        self._null_sd = 1.0
        self._last_refit = 0.0
        self._last_score = 0.0
        self._recent_returns: Optional[pd.DataFrame] = None
        self._stop = False
        self._alerts_emitted = 0

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._refit)
        self._last_refit = time.time()
        await self._score()
        self._last_score = time.time()

        while not self._stop:
            now = time.time()
            if now - self._last_refit >= self.refit_interval_sec:
                await loop.run_in_executor(None, self._refit)
                self._last_refit = now
            if now - self._last_score >= self.score_interval_sec:
                await self._score()
                self._last_score = now
            await asyncio.sleep(60.0)

    def _refit(self) -> None:
        """Pull fresh returns, fit t-copula, precompute null distribution."""
        try:
            returns = _fetch_daily_returns(self.universe, lookback_days=self.ref_days + self.roll * 3)
        except Exception as e:
            print(f"[batch] refit: fetch failed: {type(e).__name__}: {e}")
            return
        if len(returns) < self.ref_days:
            print(f"[batch] refit: only {len(returns)} days of data, need {self.ref_days}")
            return

        ref = returns.iloc[: self.ref_days].values
        self._recent_returns = returns
        try:
            cop = TCopula()
            cop.fit(ref)
            ref_ll = cop.log_prob(ref)
            rolling = np.array([ref_ll[i : i + self.roll].sum()
                                for i in range(len(ref_ll) - self.roll + 1)])
            mu, sd = float(rolling.mean()), float(rolling.std(ddof=1))
            if sd < 1e-12:
                print("[batch] refit: null distribution has zero std, skipping")
                return
            self._copula = cop
            self._null_mu = mu
            self._null_sd = sd
            print(f"[batch] refit ok  universe={self.universe}  "
                  f"df={cop.df}  null_mu={mu:.2f}  null_sd={sd:.2f}")
        except Exception as e:
            print(f"[batch] refit: fit failed: {type(e).__name__}: {e}")

    async def _score(self) -> None:
        if self._copula is None or self._recent_returns is None:
            return
        loop = asyncio.get_running_loop()

        def _compute():
            # Score the most recent `roll` days under the fitted copula
            last = self._recent_returns.iloc[-self.roll:].values
            if len(last) < self.roll:
                return None
            ll_per_obs = self._copula.log_prob(last)
            rolling_ll = float(ll_per_obs.sum())
            z = (rolling_ll - self._null_mu) / max(self._null_sd, 1e-12)
            return rolling_ll, z

        res = await loop.run_in_executor(None, _compute)
        if res is None:
            return
        rolling_ll, z = res

        if abs(z) >= self.z_thresh:
            latest_date = self._recent_returns.index[-1]
            ts_ns = int(pd.Timestamp(latest_date).timestamp() * 1e9)
            alert = Alert(
                symbol=",".join(self.universe),
                ts_ns=ts_ns,
                price=rolling_ll,
                kind="dependence_shift",
                score=z,
                ingest_ns=time.perf_counter_ns(),
            )
            self.bus.publish(alert)
            self._alerts_emitted += 1
            print(f"[batch] dependence_shift emitted  z={z:+.2f}  LL={rolling_ll:.2f}  "
                  f"null μ/σ={self._null_mu:.1f}/{self._null_sd:.2f}")

    def snapshot(self) -> dict:
        return {
            "universe": self.universe,
            "ref_days": self.ref_days,
            "roll": self.roll,
            "z_thresh": self.z_thresh,
            "null_mu": self._null_mu,
            "null_sd": self._null_sd,
            "fitted": self._copula is not None,
            "df": getattr(self._copula, "df", None) if self._copula else None,
            "alerts_emitted": self._alerts_emitted,
            "last_refit_sec_ago": time.time() - self._last_refit if self._last_refit else None,
            "last_score_sec_ago": time.time() - self._last_score if self._last_score else None,
        }

    def close(self) -> None:
        self._stop = True
