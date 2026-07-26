import time
from collections import Counter, deque
from typing import Deque, Dict

import numpy as np


class RollingStats:
    """Rolling latency percentiles + throughput + per-symbol alert counts.

    - `window_sec` applies to latency/throughput samples
    - `alert_window_sec` applies to per-symbol alert counting (larger is usually
      desired: 1-5 minutes, so a sparse-hour UI still shows meaningful density).
    """

    __slots__ = (
        "window_ns", "latencies_ns", "timestamps_ns", "_alert_count",
        "alert_window_ns", "alert_ts_ns", "alert_symbols", "alert_kinds",
    )

    def __init__(self, window_sec: float = 5.0, alert_window_sec: float = 300.0):
        self.window_ns = int(window_sec * 1e9)
        self.latencies_ns: Deque[int] = deque()
        self.timestamps_ns: Deque[int] = deque()
        self._alert_count = 0
        self.alert_window_ns = int(alert_window_sec * 1e9)
        self.alert_ts_ns: Deque[int] = deque()
        self.alert_symbols: Deque[str] = deque()
        self.alert_kinds: Deque[str] = deque()

    def record_tick(self, latency_ns: int, now_ns: int) -> None:
        self.latencies_ns.append(latency_ns)
        self.timestamps_ns.append(now_ns)
        cutoff = now_ns - self.window_ns
        while self.timestamps_ns and self.timestamps_ns[0] < cutoff:
            self.timestamps_ns.popleft()
            self.latencies_ns.popleft()

    def record_alert(self, symbol: str, kind: str, now_ns: int) -> None:
        self._alert_count += 1
        self.alert_ts_ns.append(now_ns)
        self.alert_symbols.append(symbol)
        self.alert_kinds.append(kind)
        cutoff = now_ns - self.alert_window_ns
        while self.alert_ts_ns and self.alert_ts_ns[0] < cutoff:
            self.alert_ts_ns.popleft()
            self.alert_symbols.popleft()
            self.alert_kinds.popleft()

    def snapshot(self) -> Dict:
        n = len(self.latencies_ns)
        by_symbol = dict(Counter(self.alert_symbols))
        by_kind = dict(Counter(self.alert_kinds))
        if n == 0:
            return {
                "ticks_per_sec": 0.0,
                "p50_us": 0.0,
                "p95_us": 0.0,
                "p99_us": 0.0,
                "window_ticks": 0,
                "alerts_total": self._alert_count,
                "alerts_window_sec": self.alert_window_ns / 1e9,
                "alerts_by_symbol": by_symbol,
                "alerts_by_kind": by_kind,
            }
        arr = np.fromiter(self.latencies_ns, dtype=np.int64, count=n)
        span_ns = self.timestamps_ns[-1] - self.timestamps_ns[0]
        tps = (n / (span_ns / 1e9)) if span_ns > 0 else float(n)
        return {
            "ticks_per_sec": float(tps),
            "p50_us": float(np.percentile(arr, 50) / 1000),
            "p95_us": float(np.percentile(arr, 95) / 1000),
            "p99_us": float(np.percentile(arr, 99) / 1000),
            "window_ticks": n,
            "alerts_total": self._alert_count,
            "alerts_window_sec": self.alert_window_ns / 1e9,
            "alerts_by_symbol": by_symbol,
            "alerts_by_kind": by_kind,
        }
