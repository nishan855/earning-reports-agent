import pytz
from datetime import datetime
from ..models import Session, SessionId
from ..constants import CUTOFF_HOUR, CUTOFF_MIN, OR_LOCK_HOUR, OR_LOCK_MIN
from .sim_clock import now_et

ET = pytz.timezone("America/New_York")


def get_current_session() -> Session:
    now = now_et()
    t = now.hour * 60 + now.minute
    if t < 570: return Session(id=SessionId.PREMARKET, label="PRE-MARKET", quality=0, color="#475569", min_remaining=0)
    if t < 600: return Session(id=SessionId.OR, label="OR FORMING", quality=0, color="#f59e0b", min_remaining=600 - t)
    if t < 660: return Session(id=SessionId.POWER, label="POWER HOUR", quality=5, color="#00c97e", min_remaining=660 - t)
    if t < 720: return Session(id=SessionId.MID, label="MID MORNING", quality=4, color="#60a5fa", min_remaining=720 - t)
    if t < 840: return Session(id=SessionId.DEAD, label="DEAD ZONE", quality=1, color="#ff4060", min_remaining=840 - t)
    if t < 930: return Session(id=SessionId.AFT, label="AFTERNOON", quality=4, color="#a78bfa", min_remaining=930 - t)
    if t < 945: return Session(id=SessionId.CLOSE, label="POWER CLOSE", quality=3, color="#f59e0b", min_remaining=945 - t)
    if t < 960: return Session(id=SessionId.CUTOFF, label="HARD CUTOFF", quality=0, color="#ff4060", min_remaining=0)
    return Session(id=SessionId.AH, label="AFTER HOURS", quality=0, color="#475569", min_remaining=0)


def is_trading_hours() -> bool:
    t = now_et().hour * 60 + now_et().minute
    return 570 <= t < 960


def is_signal_allowed() -> bool:
    t = now_et().hour * 60 + now_et().minute
    return 600 <= t < CUTOFF_HOUR * 60 + CUTOFF_MIN


def is_or_complete() -> bool:
    now = now_et()
    return now.hour > OR_LOCK_HOUR or (now.hour == OR_LOCK_HOUR and now.minute >= OR_LOCK_MIN)


def minutes_to_cutoff() -> int:
    t = now_et().hour * 60 + now_et().minute
    return max(0, CUTOFF_HOUR * 60 + CUTOFF_MIN - t)
