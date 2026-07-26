import numpy as np


class RingBuffer:
    __slots__ = ("capacity", "prices", "ts_ns", "head", "count")

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.prices = np.zeros(capacity, dtype=np.float64)
        self.ts_ns = np.zeros(capacity, dtype=np.int64)
        self.head = 0
        self.count = 0

    def push(self, price: float, ts_ns: int) -> None:
        self.prices[self.head] = price
        self.ts_ns[self.head] = ts_ns
        self.head = (self.head + 1) % self.capacity
        if self.count < self.capacity:
            self.count += 1

    def latest(self) -> float:
        idx = (self.head - 1) % self.capacity
        return self.prices[idx]

    def window(self) -> np.ndarray:
        # Copy-free view via concatenation when wrapped; here we return a
        # contiguous snapshot only when explicitly needed (detector hot path
        # does not call this).
        if self.count < self.capacity:
            return self.prices[: self.count]
        return np.concatenate((self.prices[self.head:], self.prices[: self.head]))

    def window_with_ts(self) -> "tuple[np.ndarray, np.ndarray]":
        """Chronologically-ordered (ts_ns, prices) snapshot. Not hot-path."""
        if self.count < self.capacity:
            return self.ts_ns[: self.count], self.prices[: self.count]
        ts = np.concatenate((self.ts_ns[self.head:], self.ts_ns[: self.head]))
        pr = np.concatenate((self.prices[self.head:], self.prices[: self.head]))
        return ts, pr
