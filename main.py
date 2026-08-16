"""
Entry point.

Startup deliberately does NOT die when the broker login fails: Railway would
health-check the container, get nothing, and restart it forever. Instead the
app comes up, reports the failure on /health and on the dashboard, and retries
the connection in the background.

Every schedule below is IST. Railway containers run on UTC, so the scheduler is
pinned to Asia/Kolkata explicitly.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from clock import IST, MarketClock, now_naive
from config import cfg
from database import init_db
from runtime import (budget, engine, feed, notifier, options, scanner,
                     state, store, universe)

logger = logging.getLogger("main")
scheduler = AsyncIOScheduler(timezone=IST)


# --------------------------------------------------------------------------- #
async def connect_feed() -> bool:
    if state.feed_connected:
        return True

    # A missing variable will still be missing in five minutes. Retrying it on a
    # timer just fills the log with the same line and hides real errors.
    if state.config_error:
        return False

    try:
        await feed.connect()
        state.feed_connected = True
        state.feed_error = None
        state.config_error = None
        logger.info("[Startup] %s feed connected", feed.name)
        return True
    except Exception as exc:
        state.feed_connected = False
        state.feed_error = str(exc)

        if type(exc).__name__ == "CredentialsError":
            state.config_error = str(exc)
            logger.error(
                "\n"
                "  ---------------------------------------------------------------\n"
                "  FEED NOT CONFIGURED - the scanner is idle, not broken.\n"
                "  %s\n"
                "\n"
                "  Set these in Railway (Service -> Variables), then redeploy.\n"
                "  To bring the dashboard up meanwhile, set FEED_MODE=mock.\n"
                "  Retries are suspended until restart; see /health for status.\n"
                "  ---------------------------------------------------------------",
                exc)
        else:
            logger.error("[Startup] Feed connection failed (will retry): %s", exc)
        return False


async def job_scan():
    if not state.feed_connected and not await connect_feed():
        return
    try:
        created = await scanner.run_cycle()
        state.last_scan_at = now_naive()
        state.scan_count += 1
        state.last_scan_error = None
        if created:
            logger.info("[Scan] %d new signal(s)", len(created))
    except Exception as exc:
        state.last_scan_error = str(exc)
        logger.exception("[Scan] Cycle failed")
        await notifier.send_error("Scan cycle", str(exc))


async def job_manage_positions():
    if not state.feed_connected or not MarketClock.is_market_open():
        return
    try:
        await engine.manage_open_positions(feed)
    except Exception as exc:
        logger.exception("[Manage] Position update failed: %s", exc)


async def job_snapshots():
    if not state.feed_connected or not MarketClock.is_market_open():
        return
    try:
        await scanner.get_index_snapshots()
    except Exception as exc:
        logger.warning("[Snapshot] %s", exc)


async def job_square_off():
    if not state.feed_connected or not MarketClock.is_market_day():
        return
    closed = await engine.force_square_off(feed, reason="15:20 square-off")
    if closed:
        await notifier.send(f"Squared off {len(closed)} position(s) at the bell.")


async def job_refresh_instruments():
    """
    Reload the Kite instruments dump before the open.

    Futures and option contracts roll with every expiry, and the dump is the
    source for index tokens, lot sizes, expiries and the option chain. Kite
    serves it as one call per exchange, so this is cheap.
    """
    if not state.feed_connected:
        return
    loader = getattr(feed, "_load_instruments", None)
    if not loader:
        return
    try:
        await loader()
        logger.info("[Instruments] Reloaded for the session")
    except Exception as exc:
        logger.error("[Instruments] Reload failed: %s", exc)
        await notifier.send_error("Instrument reload", str(exc))


async def job_build_universe():
    """
    Rebuild the tradable stock list before the open. The F&O list and its
    liquidity both move, so this is derived daily rather than hardcoded.
    """
    if not cfg.EQUITY_ENABLED:
        return
    if not state.feed_connected and not await connect_feed():
        return
    try:
        symbols = await universe.build(force=True)
        state.universe_size = len(symbols)
        logger.info("[Universe] %d stocks selected for today", len(symbols))
    except Exception as exc:
        logger.error("[Universe] Build failed: %s", exc)
        await notifier.send_error("Universe build", str(exc))


async def job_premarket_backfill():
    """
    Load history before the open so the first scan of the day isn't competing
    with live trading for the rate limit.
    """
    if not state.feed_connected and not await connect_feed():
        return
    try:
        await store.backfill(scanner.symbols(), cfg.TIMEFRAME, cfg.HISTORY_BARS)
        store.prune(cfg.CANDLE_RETENTION_DAYS)
        logger.info("[Backfill] %s", store.stats())
    except Exception as exc:
        logger.error("[Backfill] Failed: %s", exc)


async def job_sample_iv():
    """
    IV rank is only meaningful with history behind it, and history only builds
    if we sample on quiet days too - not just when a signal fires.
    """
    if not state.feed_connected or not cfg.OPTIONS_ENABLED or not MarketClock.is_market_open():
        return
    try:
        await options.record_atm_iv_for_universe()
    except Exception as exc:
        logger.warning("[IV] Sampling failed: %s", exc)


async def job_manage_stream():
    """
    Own the streaming connection across the session: start it once the market
    opens, restart it if it dies, and shut it down at the close so a dead
    socket isn't left reconnecting all night.
    """
    if not cfg.USE_WEBSOCKET or not state.feed_connected:
        return
    starter = getattr(feed, "start_stream", None)
    if not starter:
        return                      # mock feed - nothing to stream

    if not MarketClock.is_market_open():
        if getattr(feed, "stream", None) and feed.stream.connected:
            feed.stop_stream()
        return

    if feed.stream_healthy():
        return

    if budget.exhausted() and cfg.WS_UNSUBSCRIBE_AT_CAP:
        logger.debug("[WS] Cap reached - not restarting the stream")
        return

    try:
        started = starter(scanner.symbols())
        state.stream_started = bool(started)
        if started:
            logger.info("[WS] Stream up for %d symbol(s)", len(scanner.symbols()))
    except Exception as exc:
        logger.error("[WS] Could not start stream: %s", exc)
        await notifier.send_error("Websocket start", str(exc))


async def job_reconnect():
    """Retries transient failures only - a config error suspends itself."""
    if not state.feed_connected and not state.config_error:
        await connect_feed()


# --------------------------------------------------------------------------- #
def register_jobs():
    # Scans fire 20s after each 5-minute boundary so the candle has closed and
    # the broker has published it.
    scheduler.add_job(job_scan, CronTrigger(minute="*/5", second=20, timezone=IST),
                      id="scan", replace_existing=True, max_instances=1, misfire_grace_time=60)
    scheduler.add_job(job_manage_positions, "interval", minutes=1,
                      id="manage", replace_existing=True, max_instances=1, misfire_grace_time=30)
    scheduler.add_job(job_snapshots, "interval", minutes=1,
                      id="snapshots", replace_existing=True, max_instances=1, misfire_grace_time=30)
    scheduler.add_job(job_square_off, CronTrigger(day_of_week="mon-fri", hour=15, minute=20, timezone=IST),
                      id="square_off", replace_existing=True)
    scheduler.add_job(job_refresh_instruments,
                      CronTrigger(day_of_week="mon-fri", hour=8, minute=15, timezone=IST),
                      id="instruments", replace_existing=True)
    scheduler.add_job(job_build_universe,
                      CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone=IST),
                      id="universe", replace_existing=True)
    scheduler.add_job(job_premarket_backfill,
                      CronTrigger(day_of_week="mon-fri", hour=8, minute=45, timezone=IST),
                      id="backfill", replace_existing=True)
    scheduler.add_job(job_sample_iv, "interval", minutes=cfg.IV_SAMPLE_MINUTES,
                      id="iv_sample", replace_existing=True, max_instances=1)
    scheduler.add_job(job_manage_stream, "interval", minutes=1,
                      id="stream", replace_existing=True, max_instances=1)
    scheduler.add_job(job_reconnect, "interval", minutes=5,
                      id="reconnect", replace_existing=True, max_instances=1)


async def warm_up() -> None:
    """
    Everything slow, moved off the startup path.

    Connecting to Kite pulls four instrument dumps of several MB, and the first
    universe build quotes a few hundred symbols. Doing that inside the lifespan
    blocks uvicorn from accepting requests, so Railway's health check times out,
    the deploy is marked failed, and the *previous* image keeps serving traffic.
    The symptom is not a crash - it is "nothing I upload ever takes effect".

    The web server now binds first and this runs behind it. /health reports
    warmed_up=false while it works.
    """
    try:
        await connect_feed()
        if cfg.EQUITY_ENABLED and state.feed_connected and not universe.load():
            await job_build_universe()      # cold start - don't wait for tomorrow
        if cfg.RUN_SCHEDULER:
            await job_manage_stream()       # boot mid-session: don't wait a minute
    except Exception as exc:
        state.feed_error = str(exc)
        logger.exception("[Startup] Warm-up failed: %s", exc)
    finally:
        state.warmed_up = True
        logger.info("[Startup] Warm-up complete (feed connected: %s)", state.feed_connected)


@asynccontextmanager
async def lifespan(_app):
    state.started_at = now_naive()
    init_db()

    if cfg.RUN_SCHEDULER:
        register_jobs()
        scheduler.start()
    else:
        logger.info("[Startup] RUN_SCHEDULER=false - web only, a worker owns the jobs")

    # Fire and forget: the health check must never wait on the broker.
    warm = asyncio.create_task(warm_up())
    logger.info("[Startup] %s | feed=%s | timeframe=%s | market=%s",
                "Index Squeeze Scanner", cfg.FEED_MODE, cfg.TIMEFRAME, MarketClock.session_label())
    if cfg.FEED_MODE == "mock":
        logger.warning("[Startup] FEED_MODE=mock - every number on the dashboard is synthetic")
    try:
        yield
    finally:
        warm.cancel()
        if getattr(feed, "stop_stream", None):
            feed.stop_stream()
        scheduler.shutdown(wait=False)
        logger.info("[Shutdown] Scheduler stopped")


# dashboard.app is imported after runtime so the singletons exist first.
from dashboard import app  # noqa: E402

app.router.lifespan_context = lifespan


async def run_worker() -> None:
    """
    Headless mode: scheduler only, no HTTP server.

    Railway can run this as a second service pointed at the same database, so
    the scanning process carries no web-rendering overhead and a dashboard
    request can never slow a scan. Both processes are safe to run together -
    set RUN_SCHEDULER=false on the web service so jobs fire in exactly one.
    """
    state.started_at = now_naive()
    init_db()
    register_jobs()
    scheduler.start()
    asyncio.create_task(warm_up())      # same reason as the web service
    logger.info("[Worker] Scanning only - no dashboard. Cap %d signals/day",
                cfg.MAX_SIGNALS_PER_DAY)

    # A minimal health server, for two reasons. Railway's healthcheck is
    # configured once in railway.json and applies to every service, so a worker
    # with no HTTP listener fails its check and restart-loops forever. And it
    # makes the scanning process observable - whether it is alive, when it last
    # scanned, how much signal budget is left - without reading logs.
    from fastapi import FastAPI

    health_app = FastAPI(title="Scanner worker", docs_url=None, redoc_url=None)

    @health_app.get("/health")
    async def worker_health():
        return {
            "status": "ok",
            "role": "worker",
            "feed": {"mode": cfg.FEED_MODE, "connected": state.feed_connected,
                     "error": state.feed_error},
            "market": MarketClock.session_label(),
            "scheduler_running": scheduler.running,
            "jobs": [j.id for j in scheduler.get_jobs()],
            "scans": state.scan_count,
            "last_scan_at": state.last_scan_at.isoformat() if state.last_scan_at else None,
            "last_scan_error": state.last_scan_error,
            "budget": budget.check(),
            "universe": state.universe_size,
            "stream": getattr(getattr(feed, "stream", None), "status", lambda: None)(),
            "server_time_ist": now_naive().isoformat(timespec="seconds"),
        }

    server = uvicorn.Server(uvicorn.Config(
        health_app, host="0.0.0.0", port=cfg.PORT, log_level="warning"))
    try:
        await server.serve()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.shutdown(wait=False)
        logger.info("[Worker] Stopped")


if __name__ == "__main__":
    import sys
    if "--worker" in sys.argv or os.getenv("RUN_MODE", "").lower() == "worker":
        asyncio.run(run_worker())
    else:
        uvicorn.run("main:app", host="0.0.0.0", port=cfg.PORT, log_level=cfg.LOG_LEVEL.lower())
