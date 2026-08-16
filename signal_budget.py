"""
Strict daily signal cap.

The requirement is a hard ceiling of 5 alerts a day, and once it's reached the
stream should unsubscribe rather than keep burning the connection.

The count is read from the database, not from an in-process counter. A counter
in memory resets on every Railway redeploy, so a container that restarts at
noon would happily fire five more signals. The ledger is the only source of
truth that survives a restart.
"""
import logging
from typing import Callable, Dict, List, Optional

from clock import now_naive, today_start
from config import cfg
from database import Signal, session_scope

logger = logging.getLogger(__name__)


class SignalBudget:
    def __init__(self, on_exhausted: Optional[Callable] = None):
        self.on_exhausted = on_exhausted
        self._fired_hook = False

    # ------------------------------------------------------------------ #
    @staticmethod
    def used_today() -> int:
        with session_scope() as db:
            return db.query(Signal).filter(Signal.timestamp >= today_start()).count()

    def remaining(self) -> int:
        return max(0, cfg.MAX_SIGNALS_PER_DAY - self.used_today())

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> Dict:
        used = self.used_today()
        cap = cfg.MAX_SIGNALS_PER_DAY
        return {
            "used": used,
            "cap": cap,
            "remaining": max(0, cap - used),
            "exhausted": used >= cap,
            "date": now_naive().date().isoformat(),
        }

    # ------------------------------------------------------------------ #
    def allow(self, count: int = 1) -> bool:
        """Ask before creating a signal. Never negotiates the cap."""
        return self.remaining() >= count

    def take(self, candidates: List) -> List:
        """
        Trim a batch of candidate setups to what the budget allows, best first.

        A scan can surface more setups than remaining budget. Taking them in
        arrival order would spend the last slot on whichever symbol happened to
        be scanned first, so they're ranked by composite score.
        """
        remaining = self.remaining()
        if remaining <= 0:
            return []
        if len(candidates) <= remaining:
            return candidates
        ranked = sorted(candidates, key=lambda c: -(c.get("composite_score") or 0))
        dropped = [c["symbol"] for c in ranked[remaining:]]
        logger.info("[Budget] %d slot(s) left, keeping the top %d by score; deferred: %s",
                    remaining, remaining, ", ".join(dropped))
        return ranked[:remaining]

    # ------------------------------------------------------------------ #
    def notify_if_exhausted(self) -> bool:
        """
        Fire the exhaustion hook once per day. Called after signals are saved.
        Returns True the first time the cap is reached.
        """
        if not self.exhausted():
            self._fired_hook = False        # new day, or a signal was removed
            return False
        if self._fired_hook:
            return False
        self._fired_hook = True
        logger.info("[Budget] Daily cap of %d reached - no further signals today",
                    cfg.MAX_SIGNALS_PER_DAY)
        if self.on_exhausted:
            try:
                self.on_exhausted()
            except Exception as exc:
                logger.error("[Budget] Exhaustion hook failed: %s", exc)
        return True

    def reset_for_new_day(self) -> None:
        self._fired_hook = False
