"""Live service entrypoint: synthetic (or Polygon) feed → detector → FastAPI.

Run locally:
    python -m app.live

Run in Docker:
    docker compose up hft

Open http://localhost:8080/ for the viewer.
"""
import argparse
import asyncio
import os
import time

import uvicorn

from .env import load_dotenv

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

from .api import make_app
from .batch.dependence_shift import DependenceShiftDetector
from .bus import Bus
from .detector import PerSymbolDetector, Router
from .feed import AlpacaFeedController, Quote, Tick, alpaca_market_feed, synthetic_feed
from .persist import load_config_file
from .recent import RecentAlerts
from .recorder import CsvTickRecorder, record_tap
from .replay import csv_replay_feed
from .sinks.sqlite_sink import SqliteSink
from .sinks.webhook_sink import WebhookSink
from .state import RuntimeState
from .stats import RollingStats


async def _detector_loop(
    feed,
    router: Router,
    state: RuntimeState,
    alert_bus: Bus,
    recent: RecentAlerts,
    rolling: RollingStats,
    app_state=None,   # for /health tick-age tracking; optional
) -> None:
    async for event in feed:
        if not state.active:
            await asyncio.sleep(0.001)
            continue

        if isinstance(event, Tick):
            emitted = router.on_tick(event.symbol, event.price, event.ts_ns, event.ingest_ns, size=event.size)
        elif isinstance(event, Quote):
            emitted = router.on_quote(
                event.symbol, event.bid, event.ask,
                event.bid_size, event.ask_size,
                event.ts_ns, event.ingest_ns,
            )
        else:
            continue

        now_ns = time.perf_counter_ns()
        if app_state is not None:
            app_state.last_tick_ns = now_ns
        rolling.record_tick(now_ns - event.ingest_ns, now_ns)
        for alert in emitted:
            rolling.record_alert(alert.symbol, alert.kind, now_ns)
            recent.add(alert)
            alert_bus.publish(alert)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--source", choices=("synthetic", "csv", "alpaca"), default=os.environ.get("HFT_SOURCE", "synthetic"))
    p.add_argument("--symbols", default=os.environ.get("HFT_SYMBOLS", "AAPL,MSFT,SPY,NVDA"))
    p.add_argument("--tps", type=int, default=int(os.environ.get("HFT_TPS", "5000")))
    p.add_argument("--anomaly-rate", type=float, default=float(os.environ.get("HFT_ANOMALY_RATE", "5e-4")))
    p.add_argument("--csv-path", default=os.environ.get("HFT_CSV_PATH"))
    p.add_argument("--speed", type=float, default=float(os.environ.get("HFT_SPEED", "60")),
                   help="replay speed multiplier; 0=unthrottled, 1=realtime, 60=1min/sec")
    p.add_argument("--loop", action="store_true", help="loop the CSV indefinitely")
    p.add_argument("--record-to", default=os.environ.get("HFT_RECORD_TO"),
                   help="also write every tick to this CSV (replay-compatible)")
    p.add_argument("--host", default=os.environ.get("HFT_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("HFT_PORT", "8080")))
    return p.parse_args()


def _state_from_env() -> RuntimeState:
    """RuntimeState seeded from HFT_* env vars; unset keys fall back to class defaults."""
    defaults = RuntimeState()
    return RuntimeState(
        default_z_thresh=float(os.environ.get("HFT_Z_THRESH", defaults.default_z_thresh)),
        default_cusum_h=float(os.environ.get("HFT_CUSUM_H", defaults.default_cusum_h)),
        default_cusum_k=float(os.environ.get("HFT_CUSUM_K", defaults.default_cusum_k)),
        alpha=float(os.environ.get("HFT_ALPHA", defaults.alpha)),
        session_gap_sec=float(os.environ.get("HFT_SESSION_GAP_SEC", defaults.session_gap_sec)),
        warmup_ticks=int(os.environ.get("HFT_WARMUP_TICKS", defaults.warmup_ticks)),
        spread_z_thresh=float(os.environ.get("HFT_SPREAD_Z_THRESH", defaults.spread_z_thresh)),
        obi_alpha=float(os.environ.get("HFT_OBI_ALPHA", defaults.obi_alpha)),
        obi_extreme_thresh=float(os.environ.get("HFT_OBI_EXTREME_THRESH", defaults.obi_extreme_thresh)),
        obi_rearm_thresh=float(os.environ.get("HFT_OBI_REARM_THRESH", defaults.obi_rearm_thresh)),
        qrate_z_thresh=float(os.environ.get("HFT_QRATE_Z_THRESH", defaults.qrate_z_thresh)),
        qrate_cooldown_sec=float(os.environ.get("HFT_QRATE_COOLDOWN_SEC", defaults.qrate_cooldown_sec)),
        vol_z_thresh=float(os.environ.get("HFT_VOL_Z_THRESH", defaults.vol_z_thresh)),
        micro_z_thresh=float(os.environ.get("HFT_MICRO_Z_THRESH", defaults.micro_z_thresh)),
        burst_window_sec=float(os.environ.get("HFT_BURST_WINDOW_SEC", defaults.burst_window_sec)),
        burst_k_thresh=float(os.environ.get("HFT_BURST_K_THRESH", defaults.burst_k_thresh)),
        burst_baseline_alpha=float(os.environ.get("HFT_BURST_BASELINE_ALPHA", defaults.burst_baseline_alpha)),
        burst_cooldown_sec=float(os.environ.get("HFT_BURST_COOLDOWN_SEC", defaults.burst_cooldown_sec)),
        regime_short=int(os.environ.get("HFT_REGIME_SHORT", defaults.regime_short)),
        regime_ref=int(os.environ.get("HFT_REGIME_REF", defaults.regime_ref)),
        regime_ref_stride=int(os.environ.get("HFT_REGIME_REF_STRIDE", defaults.regime_ref_stride)),
        regime_check_every=int(os.environ.get("HFT_REGIME_CHECK_EVERY", defaults.regime_check_every)),
        regime_ks_thresh=float(os.environ.get("HFT_REGIME_KS_THRESH", defaults.regime_ks_thresh)),
        regime_cooldown_sec=float(os.environ.get("HFT_REGIME_COOLDOWN_SEC", defaults.regime_cooldown_sec)),
        use_quote_mid=os.environ.get("HFT_USE_QUOTE_MID", str(defaults.use_quote_mid)).lower() not in ("0", "false", "no"),
        min_obs=int(os.environ.get("HFT_MIN_OBS", defaults.min_obs)),
    )


def main() -> None:
    load_dotenv()
    args = _parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    state = _state_from_env()
    # Overlay persisted config file if it exists (precedence: env < file < API)
    loaded = load_config_file(state)
    if loaded:
        print("[config] loaded persisted overrides from qpulse_config.json")

    def _factory(sym: str) -> PerSymbolDetector:
        return PerSymbolDetector(
            sym,
            alpha=state.alpha,
            z_thresh=state.default_z_thresh,
            cusum_h=state.default_cusum_h,
            cusum_k=state.default_cusum_k,
            session_gap_sec=state.session_gap_sec,
            warmup_ticks=state.warmup_ticks,
            spread_z_thresh=state.spread_z_thresh,
            obi_alpha=state.obi_alpha,
            obi_extreme_thresh=state.obi_extreme_thresh,
            obi_rearm_thresh=state.obi_rearm_thresh,
            qrate_z_thresh=state.qrate_z_thresh,
            qrate_cooldown_sec=state.qrate_cooldown_sec,
            vol_z_thresh=state.vol_z_thresh,
            micro_z_thresh=state.micro_z_thresh,
            burst_window_sec=state.burst_window_sec,
            burst_k_thresh=state.burst_k_thresh,
            burst_baseline_alpha=state.burst_baseline_alpha,
            burst_cooldown_sec=state.burst_cooldown_sec,
            regime_short=state.regime_short,
            regime_ref=state.regime_ref,
            regime_ref_stride=state.regime_ref_stride,
            regime_check_every=state.regime_check_every,
            regime_ks_thresh=state.regime_ks_thresh,
            regime_cooldown_sec=state.regime_cooldown_sec,
            use_quote_mid=state.use_quote_mid,
            min_obs=state.min_obs,
        )

    router = Router(factory=_factory)
    alert_bus = Bus(maxsize=1000)
    recent = RecentAlerts(capacity=500)
    rolling = RollingStats(window_sec=5.0)

    # Optional sinks
    db_path = os.environ.get("HFT_DB_PATH", "qpulse.db")
    webhook_url = os.environ.get("HFT_WEBHOOK_URL")
    webhook_min_score = float(os.environ.get("HFT_WEBHOOK_MIN_SCORE", "0"))
    webhook_key = os.environ.get("HFT_WEBHOOK_KEY")

    app = make_app(router, state, recent, alert_bus, rolling, db_path=db_path)

    @app.on_event("startup")
    async def _spawn_detector() -> None:
        if args.source == "csv":
            if not args.csv_path:
                raise SystemExit("--csv-path required when --source csv")
            feed = csv_replay_feed(args.csv_path, speed=args.speed, loop=args.loop)
        elif args.source == "alpaca":
            initial_symbols = state.symbols if state.symbols else symbols
            controller = AlpacaFeedController(
                initial_symbols=initial_symbols,
                feed_name=os.environ.get("ALPACA_FEED", "iex"),
                trades=True,
                quotes=os.environ.get("HFT_QUOTES", "true").lower() != "false",
            )
            app.state.feed_controller = controller
            state.symbols = controller.symbols
            state.feed_name = controller.feed_name
            feed = controller.stream()
        else:
            feed = synthetic_feed(symbols, ticks_per_second=args.tps, anomaly_rate=args.anomaly_rate)
        if args.record_to:
            feed = record_tap(feed, CsvTickRecorder(args.record_to))

        # Spawn persistence and webhook sinks before the detector task
        app.state.sqlite_sink = SqliteSink(db_path, alert_bus)
        app.state.sqlite_task = asyncio.create_task(app.state.sqlite_sink.run())
        print(f"[sqlite] persisting alerts → {db_path}")

        if webhook_url:
            # feed_name tells the receiver which data context produced these
            # alerts, so it never labels them with a gate measured elsewhere.
            # For live feeds it is read per batch, because /config/feed can
            # switch crypto↔iex while the service runs.
            if args.source == "csv":
                feed_name = "csv"
            elif args.source == "synthetic":
                feed_name = "synthetic"
            else:
                feed_name = lambda: state.feed_name  # noqa: E731
            app.state.webhook_sink = WebhookSink(
                webhook_url, alert_bus, min_score=webhook_min_score,
                api_key=webhook_key, feed_name=feed_name,
            )
            app.state.webhook_task = asyncio.create_task(app.state.webhook_sink.run())
            auth_note = "authenticated" if webhook_key else "NO KEY SET"
            print(f"[webhook] forwarding alerts → {webhook_url}  (min_score="
                  f"{webhook_min_score}, feed={app.state.webhook_sink.feed_name}, {auth_note})")

        if os.environ.get("HFT_BATCH_ENABLED", "false").lower() in ("1", "true", "yes"):
            univ_env = os.environ.get("HFT_BATCH_UNIVERSE", "")
            univ = [s.strip() for s in univ_env.split(",") if s.strip()] or None
            batch = DependenceShiftDetector(
                bus=alert_bus,
                universe=univ,
                ref_days=int(os.environ.get("HFT_BATCH_REF_DAYS", "252")),
                roll=int(os.environ.get("HFT_BATCH_ROLL", "5")),
                refit_interval_sec=float(os.environ.get("HFT_BATCH_REFIT_SEC", "86400")),
                score_interval_sec=float(os.environ.get("HFT_BATCH_SCORE_SEC", "3600")),
                z_thresh=float(os.environ.get("HFT_BATCH_Z_THRESH", "3.0")),
            )
            app.state.batch_detector = batch
            app.state.batch_task = asyncio.create_task(batch.run())
            print(f"[batch] dependence-shift detector enabled  universe={batch.universe}  "
                  f"z_thresh={batch.z_thresh}")
            print(f"[recorder] writing ticks to {args.record_to}")
        app.state.detector_task = asyncio.create_task(
            _detector_loop(feed, router, state, alert_bus, recent, rolling, app_state=app.state)
        )

    @app.on_event("shutdown")
    async def _stop_detector() -> None:
        for attr in ("detector_task", "sqlite_task", "webhook_task", "batch_task"):
            task = getattr(app.state, attr, None)
            if task:
                task.cancel()
        for attr in ("sqlite_sink", "webhook_sink", "batch_detector"):
            sink = getattr(app.state, attr, None)
            if sink:
                sink.close()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
