from collections import deque
from typing import Deque, Iterable, List

from .detector import Alert


class RecentAlerts:
    """Bounded in-memory history for `/alerts/recent` queries."""

    __slots__ = ("_dq",)

    def __init__(self, capacity: int = 500):
        self._dq: Deque[Alert] = deque(maxlen=capacity)

    def add(self, a: Alert) -> None:
        self._dq.append(a)

    def snapshot(self, limit: int | None = None) -> List[Alert]:
        if limit is None or limit >= len(self._dq):
            return list(self._dq)
        # last `limit` items
        return list(self._dq)[-limit:]

    def __iter__(self) -> Iterable[Alert]:
        return iter(self._dq)
