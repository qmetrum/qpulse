import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .bus import Bus
from .detector import Alert, Router
from .narrative import render as render_narrative, severity as alert_severity
from .persist import save_config_file
from .recent import RecentAlerts
from .sinks.sqlite_sink import query_history
from .state import RuntimeState
from .stats import RollingStats


def _alert_to_dict(a: Alert) -> dict:
    return {
        "symbol": a.symbol,
        "ts_ns": a.ts_ns,
        "price": a.price,
        "kind": a.kind,
        "score": a.score,
        "details": a.details,
        "severity": alert_severity(a),
        "narrative": render_narrative(a),
    }


class ThresholdUpdate(BaseModel):
    symbol: str
    z_thresh: float


class SymbolsUpdate(BaseModel):
    add: Optional[list[str]] = None
    remove: Optional[list[str]] = None


class FeedSwitch(BaseModel):
    feed_name: str          # iex | sip | crypto
    symbols: Optional[list[str]] = None  # initial symbols on the new feed


# Paths that bypass auth (for liveness probes, static UI, viewer root).
_AUTH_EXEMPT_PREFIXES = ("/health", "/ui", "/favicon")


def make_app(
    router: Router,
    state: RuntimeState,
    recent: RecentAlerts,
    alert_bus: Bus,
    rolling: RollingStats,
    stats_interval_sec: float = 0.5,
    db_path: Optional[str] = None,
) -> FastAPI:
    app = FastAPI(title="HFT Anomaly Stream API")
    stats_bus = Bus(maxsize=100)

    # Track feed liveness / uptime for /health
    app.state.started_at = time.time()
    app.state.last_tick_ns = 0
    app.state.detector_task = None
    app.state.db_path = db_path

    # ---- Auth middleware (no-op if HFT_API_TOKEN unset) ----
    @app.middleware("http")
    async def auth_middleware(request, call_next):
        expected = os.environ.get("HFT_API_TOKEN")
        path = request.url.path
        if not expected or path == "/" or any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != expected:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    async def _stats_broadcaster() -> None:
        while True:
            msg = rolling.snapshot()
            msg["active"] = state.active
            msg["symbols"] = router.symbols()
            stats_bus.publish(msg)
            await asyncio.sleep(stats_interval_sec)

    @app.on_event("startup")
    async def _startup() -> None:
        app.state.stats_task = asyncio.create_task(_stats_broadcaster())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task = getattr(app.state, "stats_task", None)
        if task:
            task.cancel()

    # ---------- HTTP control ----------

    @app.get("/health")
    def health() -> dict:
        now_ns = time.perf_counter_ns()
        last_ns = app.state.last_tick_ns
        tick_age_sec = (now_ns - last_ns) / 1e9 if last_ns else None
        det_task = app.state.detector_task
        detector_alive = bool(det_task and not det_task.done())
        # Socket state and data flow are different questions: a live equities
        # feed is legitimately silent overnight, while a stale crypto feed is
        # broken. Report both instead of inferring one from the other.
        controller = getattr(app.state, "feed_controller", None)
        feed = controller.health if controller is not None else None
        feed_connected = tick_age_sec is not None and tick_age_sec < 30.0
        uptime_sec = time.time() - app.state.started_at
        # Grace period so a service that is still connecting does not look
        # broken; after it, having never received a tick is a real fault.
        grace_sec = max(60.0, (feed or {}).get("stale_sec", 0.0))
        if not detector_alive:
            status = "down"
        elif feed is not None and not feed["connected"]:
            status = "reconnecting"
        elif last_ns == 0:
            # Never received data. Previously fell through to "ok" because tick
            # age was None — a service that has never worked reported healthy.
            status = "starting" if uptime_sec < grace_sec else "degraded"
        elif tick_age_sec is not None and tick_age_sec > 30.0:
            status = "degraded"
        else:
            status = "ok"
        out = {
            "status": status,
            "active": state.active,
            "detector_alive": detector_alive,
            "feed_connected": feed_connected,
            "last_tick_ns": last_ns,
            "tick_age_sec": tick_age_sec,
            "symbols_active": len(router.symbols()),
            "uptime_sec": uptime_sec,
        }
        if feed is not None:
            out["feed"] = feed
        return out

    @app.get("/config")
    def get_config() -> dict:
        return {
            "active": state.active,
            "default_z_thresh": state.default_z_thresh,
            "default_cusum_h": state.default_cusum_h,
            "default_cusum_k": state.default_cusum_k,
            "alpha": state.alpha,
            "session_gap_sec": state.session_gap_sec,
            "warmup_ticks": state.warmup_ticks,
            "spread_z_thresh": state.spread_z_thresh,
            "obi_alpha": state.obi_alpha,
            "obi_extreme_thresh": state.obi_extreme_thresh,
            "obi_rearm_thresh": state.obi_rearm_thresh,
            "qrate_z_thresh": state.qrate_z_thresh,
            "qrate_cooldown_sec": state.qrate_cooldown_sec,
            "vol_z_thresh": state.vol_z_thresh,
            "micro_z_thresh": state.micro_z_thresh,
            "burst_window_sec": state.burst_window_sec,
            "burst_k_thresh": state.burst_k_thresh,
            "burst_baseline_alpha": state.burst_baseline_alpha,
            "burst_cooldown_sec": state.burst_cooldown_sec,
            "regime_short": state.regime_short,
            "regime_ref": state.regime_ref,
            "regime_ref_stride": state.regime_ref_stride,
            "regime_check_every": state.regime_check_every,
            "regime_ks_thresh": state.regime_ks_thresh,
            "regime_cooldown_sec": state.regime_cooldown_sec,
            "use_quote_mid": state.use_quote_mid,
            "min_obs": state.min_obs,
            "z_thresh_overrides": state.z_thresh_overrides,
            "symbols": state.symbols if state.symbols else router.symbols(),
            "feed_name": state.feed_name,
            "batch": getattr(app.state, "batch_detector", None).snapshot()
                      if getattr(app.state, "batch_detector", None) else None,
        }

    @app.post("/config/active")
    def set_active(body: dict) -> dict:
        state.active = bool(body.get("active", True))
        save_config_file(state)
        return {"active": state.active}

    @app.post("/config/threshold")
    def set_threshold(upd: ThresholdUpdate) -> dict:
        state.z_thresh_overrides[upd.symbol] = upd.z_thresh
        applied = router.set_z_thresh(upd.symbol, upd.z_thresh)
        save_config_file(state)
        return {"symbol": upd.symbol, "z_thresh": upd.z_thresh, "applied_to_live_detector": applied}

    @app.post("/config/symbols")
    async def update_symbols(upd: SymbolsUpdate):
        controller = getattr(app.state, "feed_controller", None)
        if controller is None:
            return JSONResponse(
                {"error": "runtime symbol mutation only supported on alpaca source"},
                status_code=400,
            )
        # Validate format per active feed
        for s in (upd.add or []):
            if controller.is_crypto and "/" not in s:
                return JSONResponse(
                    {"error": f"current feed is 'crypto' - symbols need slash format (e.g. BTC/USD). "
                              f"Got '{s}'. To track equities, switch the feed to 'iex' or 'sip' first."},
                    status_code=400,
                )
            if not controller.is_crypto and "/" in s:
                return JSONResponse(
                    {"error": f"current feed is '{controller.feed_name}' - equity symbols use plain tickers (e.g. AAPL). "
                              f"Got '{s}'. To track crypto, switch the feed to 'crypto' first."},
                    status_code=400,
                )
        added = []
        removed = []
        if upd.add:
            cleaned = [s.strip().upper() for s in upd.add if s.strip()]
            added = await controller.add(cleaned)
        if upd.remove:
            cleaned = [s.strip().upper() for s in upd.remove if s.strip()]
            removed = await controller.remove(cleaned)
        state.symbols = controller.symbols
        save_config_file(state)
        return {"current": state.symbols, "added": added, "removed": removed}

    @app.post("/config/feed")
    async def switch_feed(upd: FeedSwitch):
        controller = getattr(app.state, "feed_controller", None)
        if controller is None:
            return JSONResponse(
                {"error": "feed switching only supported on alpaca source"},
                status_code=400,
            )
        valid = {"iex", "sip", "crypto"}
        if upd.feed_name not in valid:
            return JSONResponse(
                {"error": f"feed_name must be one of {sorted(valid)}; got '{upd.feed_name}'"},
                status_code=400,
            )
        # Validate any provided initial symbols against the new feed type
        new_is_crypto = upd.feed_name == "crypto"
        for s in (upd.symbols or []):
            if new_is_crypto and "/" not in s:
                return JSONResponse(
                    {"error": f"crypto feed needs slash format (e.g. BTC/USD); got '{s}'"},
                    status_code=400,
                )
            if not new_is_crypto and "/" in s:
                return JSONResponse(
                    {"error": f"equity feed uses plain tickers (e.g. AAPL); got '{s}'"},
                    status_code=400,
                )
        cleaned = [s.strip().upper() for s in (upd.symbols or []) if s.strip()]
        await controller.switch_feed(upd.feed_name, new_symbols=cleaned)
        state.feed_name = controller.feed_name
        state.symbols = controller.symbols
        save_config_file(state)
        warning = None
        if upd.feed_name in ("iex", "sip"):
            warning = "IEX/SIP only stream during US market hours (09:30–16:00 ET, Mon–Fri). Outside that window the connection succeeds but no data flows."
        return {
            "feed_name": state.feed_name,
            "symbols": state.symbols,
            "note": warning,
        }

    @app.get("/alerts/recent")
    def get_recent(limit: int = 100) -> list[dict]:
        return [_alert_to_dict(a) for a in recent.snapshot(limit)]

    @app.get("/alerts/history")
    def get_history(
        symbol: Optional[str] = None,
        kind: Optional[str] = None,
        since: Optional[int] = None,
        until: Optional[int] = None,
        limit: int = 100,
    ) -> list[dict]:
        if not app.state.db_path:
            return []
        rows = query_history(app.state.db_path, symbol, kind, since, until, limit)
        # Decorate with narrative + severity (re-rendered from stored details)
        for r in rows:
            tmp = Alert(
                symbol=r["symbol"], ts_ns=r["ts_ns"], price=r["price"],
                kind=r["kind"], score=r["score"], ingest_ns=0,
                details=r.get("details"),
            )
            r["narrative"] = render_narrative(tmp)
            r["severity"] = alert_severity(tmp)
        return rows

    @app.get("/context")
    def get_context(symbol: str, ts_ns: int | None = None) -> dict:
        ts, prices = router.snapshot_buffer(symbol)
        return {
            "symbol": symbol,
            "ts_ns": ts,
            "prices": prices,
            "alert_ts_ns": ts_ns,
            "quote": router.latest_quote(symbol),
        }

    # ---------- WebSocket streams ----------

    @app.websocket("/ws/alerts")
    async def ws_alerts(ws: WebSocket) -> None:
        # Manual auth for WS - middleware doesn't apply to WS handshakes
        expected = os.environ.get("HFT_API_TOKEN")
        if expected:
            got = ws.query_params.get("token") or ws.headers.get("authorization", "").removeprefix("Bearer ")
            if got != expected:
                await ws.close(code=1008)
                return
        await ws.accept()
        q = alert_bus.subscribe()
        try:
            while True:
                alert: Alert = await q.get()
                await ws.send_text(json.dumps(_alert_to_dict(alert)))
        except WebSocketDisconnect:
            pass
        finally:
            alert_bus.unsubscribe(q)

    @app.websocket("/ws/stats")
    async def ws_stats(ws: WebSocket) -> None:
        expected = os.environ.get("HFT_API_TOKEN")
        if expected:
            got = ws.query_params.get("token") or ws.headers.get("authorization", "").removeprefix("Bearer ")
            if got != expected:
                await ws.close(code=1008)
                return
        await ws.accept()
        q = stats_bus.subscribe()
        try:
            while True:
                msg = await q.get()
                await ws.send_text(json.dumps(msg))
        except WebSocketDisconnect:
            pass
        finally:
            stats_bus.unsubscribe(q)

    # ---------- Static viewer ----------

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/ui", StaticFiles(directory=str(static_dir), html=True), name="ui")

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    return app
