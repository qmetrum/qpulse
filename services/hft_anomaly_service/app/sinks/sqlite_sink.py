"""SQLite WAL alert persistence (Phase 2.1).

Writes are batched (every `batch_size` alerts or `flush_sec`, whichever first)
and delegated to a thread executor so the detector loop is never blocked.
Reads are served by the /alerts/history endpoint through `query_history`.
"""
import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

from ..bus import Bus
from ..detector import Alert


_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ns  INTEGER NOT NULL,
    symbol TEXT    NOT NULL,
    kind   TEXT    NOT NULL,
    price  REAL    NOT NULL,
    score  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_symbol_ts ON alerts (symbol, ts_ns);
CREATE INDEX IF NOT EXISTS idx_alerts_kind_ts   ON alerts (kind, ts_ns);
"""


def _open_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL is already crash-safe
    return conn


def init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = _open_conn(path)
    try:
        conn.executescript(_SCHEMA)
        # Migration: add details_json column if missing
        cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
        if "details_json" not in cols:
            conn.execute("ALTER TABLE alerts ADD COLUMN details_json TEXT")
    finally:
        conn.close()


class SqliteSink:
    __slots__ = ("path", "bus", "batch_size", "flush_sec", "_queue", "_conn", "_stop")

    def __init__(self, path: str, bus: Bus, batch_size: int = 100, flush_sec: float = 1.0):
        self.path = path
        self.bus = bus
        self.batch_size = batch_size
        self.flush_sec = flush_sec
        init_db(path)
        self._conn = _open_conn(path)
        self._queue = bus.subscribe()
        self._stop = False

    async def run(self) -> None:
        batch: List[Alert] = []
        last_flush = time.perf_counter()
        loop = asyncio.get_running_loop()

        while not self._stop:
            timeout = max(0.01, self.flush_sec - (time.perf_counter() - last_flush))
            try:
                alert = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                batch.append(alert)
            except asyncio.TimeoutError:
                pass

            now = time.perf_counter()
            if batch and (len(batch) >= self.batch_size or (now - last_flush) >= self.flush_sec):
                to_write = batch
                batch = []
                last_flush = now
                await loop.run_in_executor(None, self._flush_sync, to_write)

    def _flush_sync(self, batch: List[Alert]) -> None:
        rows = [
            (a.ts_ns, a.symbol, a.kind, a.price, a.score,
             json.dumps(a.details) if a.details else None)
            for a in batch
        ]
        try:
            self._conn.execute("BEGIN")
            self._conn.executemany(
                "INSERT INTO alerts (ts_ns, symbol, kind, price, score, details_json) "
                "VALUES (?,?,?,?,?,?)",
                rows,
            )
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def close(self) -> None:
        self._stop = True
        self.bus.unsubscribe(self._queue)
        try:
            self._conn.close()
        except Exception:
            pass


def query_history(
    path: str,
    symbol: Optional[str] = None,
    kind: Optional[str] = None,
    since: Optional[int] = None,
    until: Optional[int] = None,
    limit: int = 100,
) -> list[dict]:
    """Return most-recent-first alerts matching filters."""
    clauses: list[str] = []
    params: list = []
    if symbol:
        clauses.append("symbol = ?"); params.append(symbol)
    if kind:
        clauses.append("kind = ?"); params.append(kind)
    if since is not None:
        clauses.append("ts_ns >= ?"); params.append(since)
    if until is not None:
        clauses.append("ts_ns <= ?"); params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        f"SELECT ts_ns, symbol, kind, price, score, details_json "
        f"FROM alerts {where} ORDER BY ts_ns DESC LIMIT ?"
    )
    params.append(limit)

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        details = None
        if r[5]:
            try:
                details = json.loads(r[5])
            except (ValueError, TypeError):
                details = None
        out.append({
            "ts_ns": r[0], "symbol": r[1], "kind": r[2],
            "price": r[3], "score": r[4], "details": details,
        })
    return out
