"""
Single place where the feed, scanner, engine and notifier are constructed, so
main.py and dashboard.py share one instance each instead of importing each
other in a circle.
"""
import logging
from datetime import datetime

from clock import IST

from config import cfg
from candle_store import CandleStore
from feed_mock import build_feed
from notifier import Notifier
from options_layer import OptionsLayer
from scanner import IndexScanner
from signal_budget import SignalBudget
from universe import UniverseBuilder
from signal_engine import SignalEngine

# Containers run UTC, every market rule here runs IST. Logging the container
# clock means a line stamped 09:45 is really 15:15 IST - actively misleading
# when you are trying to work out whether a scan fired during market hours.
logging.Formatter.converter = lambda *args: datetime.now(IST).timetuple()
logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    format="%(asctime)s IST %(levelname)-7s %(name)-16s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

feed = build_feed()
notifier = Notifier()
options = OptionsLayer(feed)
engine = SignalEngine(notifier=notifier, options=options)
store = CandleStore(feed)
universe = UniverseBuilder(feed)


def _on_budget_exhausted():
    """Cap reached - stop paying for a feed there is nothing left to act on."""
    stream = getattr(feed, "stream", None)
    if stream and cfg.WS_UNSUBSCRIBE_AT_CAP:
        stream.unsubscribe()


budget = SignalBudget(on_exhausted=_on_budget_exhausted)
scanner = IndexScanner(feed, engine=engine, store=store, budget=budget, universe=universe)


class AppState:
    """Health and status surface for the dashboard."""
    feed_connected = False
    feed_error = None
    started_at = None
    last_scan_at = None
    last_scan_error = None
    scan_count = 0
    universe_size = 0
    stream_started = False
    config_error = None
    warmed_up = False


state = AppState()
