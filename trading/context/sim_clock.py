"""Centralized clock for the trading system.

In live mode: returns datetime.now(ET) — real wall clock.
In sim mode: returns the timestamp of the last processed tick.

Usage:
    from ..context.sim_clock import now_et

    dt = now_et()  # returns datetime in ET timezone
"""

import os
import pytz
from datetime import datetime

ET = pytz.timezone("America/New_York")

_sim_mode = os.environ.get("FINNHUB_SIM", "").strip() == "1"
_sim_time: datetime | None = None


def now_et() -> datetime:
    """Get current time in ET — real clock or simulated."""
    if _sim_mode and _sim_time is not None:
        return _sim_time
    return datetime.now(ET)


def set_sim_time(ts_ms: int):
    """Update the simulated clock from a tick timestamp (milliseconds)."""
    global _sim_time
    _sim_time = datetime.fromtimestamp(ts_ms / 1000, tz=ET)


def is_sim() -> bool:
    return _sim_mode
