"""Failure guards for a long-running collector.

Adapted from oculus's ``risk_manager.py``: the ideas worth keeping were a kill
latch that survives a restart, a staleness check that treats silence as failure,
and a drawdown latch that trips on a loss of confidence rather than a loss of
money. Those are the three ways a six-month run dies quietly, so they are the
three things this module watches.

The important change of meaning: oculus guards a trading account, this guards a
*collector*. "Stale" here means the feed went quiet, and a quiet feed is not a
calm market — it is a wedged websocket or a parked endpoint, and every second it
stays quiet is trades that were never seen. So the guard fails loud and early.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("cse.guards")


class KillSwitch:
    """A persisted latch: once tripped it stays tripped across restarts.

    Deliberately sticky. A collector that quietly restarts itself after a fault
    hides the fault; one that refuses to run until someone looks at it does not.
    """

    def __init__(self, path: str | Path = "data/kill_switch.json"):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._reason: Optional[str] = None
        self._engaged = False
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._engaged = bool(data.get("engaged"))
            self._reason = data.get("reason")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._engaged = False
            self._reason = None

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"engaged": self._engaged, "reason": self._reason, "at": time.time()}),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def engage(self, reason: str) -> None:
        with self._lock:
            if self._engaged:
                return
            self._engaged = True
            self._reason = reason
            self._persist()
            log.error("kill switch engaged: %s", reason)

    def disengage(self) -> None:
        with self._lock:
            self._engaged = False
            self._reason = None
            self._persist()
            log.warning("kill switch disengaged")

    @property
    def engaged(self) -> bool:
        with self._lock:
            return self._engaged

    @property
    def reason(self) -> Optional[str]:
        with self._lock:
            return self._reason

    def to_dict(self) -> dict:
        return {"engaged": self.engaged, "reason": self.reason}


@dataclass
class StalenessGuard:
    """Flags a feed that has gone quiet for too long.

    ``silence`` is measured from the last *event*, not the last successful call:
    an endpoint happily returning "no new transactions" for ten minutes is exactly
    the failure this is meant to catch.
    """

    threshold_s: float = 120.0
    #: Silence is only meaningful once we have heard something at least once;
    #: otherwise startup would immediately look like a fault.
    _last_event: Optional[float] = None
    _events: int = 0

    def note_event(self, when: Optional[float] = None) -> None:
        self._last_event = when if when is not None else time.time()
        self._events += 1

    @property
    def events(self) -> int:
        return self._events

    @property
    def silence_s(self) -> Optional[float]:
        if self._last_event is None:
            return None
        return max(0.0, time.time() - self._last_event)

    def is_stale(self, now: Optional[float] = None) -> bool:
        if self._last_event is None:
            return False
        now = now if now is not None else time.time()
        return (now - self._last_event) > self.threshold_s

    def to_dict(self) -> dict:
        return {
            "threshold_s": self.threshold_s,
            "events": self._events,
            "silence_s": round(self.silence_s, 1) if self.silence_s is not None else None,
            "stale": self.is_stale(),
        }


@dataclass
class DrawdownGuard:
    """Trips when a shadow portfolio gives back too much of its peak."""

    limit_pct: float = 0.5
    peak: float = 0.0
    tripped: bool = False
    tripped_at: Optional[float] = None
    _started: bool = False

    def update(self, equity: float) -> bool:
        if equity > self.peak or not self._started:
            self.peak = equity
            self._started = True
        if self.peak <= 0:
            return False
        dd = (self.peak - equity) / self.peak
        if dd >= self.limit_pct and not self.tripped:
            self.tripped = True
            self.tripped_at = time.time()
        return self.tripped

    def to_dict(self) -> dict:
        return {
            "limit_pct": self.limit_pct,
            "peak": round(self.peak, 4),
            "tripped": self.tripped,
            "tripped_at": self.tripped_at,
        }


@dataclass
class HealthReport:
    """What the runner reports about itself, so silence is never the only signal."""

    kill: dict = field(default_factory=dict)
    feed: dict = field(default_factory=dict)
    queue: dict = field(default_factory=dict)
    rpc: dict = field(default_factory=dict)
    trades_seen: int = 0
    trades_recorded: int = 0
    trades_duplicate: int = 0
    checked_at: float = field(default_factory=time.time)

    @property
    def healthy(self) -> bool:
        if self.kill.get("engaged"):
            return False
        if self.feed.get("stale"):
            return False
        return True

    def to_dict(self) -> dict:
        d = {
            "healthy": self.healthy,
            "kill": self.kill,
            "feed": self.feed,
            "queue": self.queue,
            "rpc": self.rpc,
            "trades_seen": self.trades_seen,
            "trades_recorded": self.trades_recorded,
            "trades_duplicate": self.trades_duplicate,
            "checked_at": self.checked_at,
        }
        return d
