from datetime import datetime
import pytz
from .models import Session, SessionId

ET_TZ = pytz.timezone("America/New_York")
OR_DURATION_MINS = 30


def get_et_now() -> datetime:
    return datetime.now(ET_TZ)


def get_session() -> Session:
    et = get_et_now()
    t  = et.hour * 60 + et.minute

    boundaries = [
        (240,                    SessionId.CLOSED,    "CLOSED",         "#475569", 0, 99),
        (570,                    SessionId.PREMARKET, "PRE-MARKET",     "#f59e0b", 2, 7),
        (570 + OR_DURATION_MINS, SessionId.OR,        "OPENING RANGE",  "#a78bfa", 3, 7),
        (660,                    SessionId.POWER,     "POWER HOUR",     "#00d97e", 5, 6),
        (720,                    SessionId.MID,       "MID MORNING",    "#3b82f6", 4, 6),
        (840,                    SessionId.DEAD,      "DEAD ZONE",      "#ef4444", 1, 8),
        (930,                    SessionId.AFT,       "AFTERNOON",      "#a78bfa", 3, 6),
        (945,                    SessionId.CLOSE,     "POWER CLOSE",    "#f97316", 3, 7),
        (960,                    SessionId.CLOSE,     "NO NEW TRADES",  "#ef4444", 0, 99),
        (1200,                   SessionId.AH,        "AFTER HOURS",    "#64748b", 1, 99),
    ]

    prev_end = 0
    for end, sid, label, color, quality, threshold in boundaries:
        if t < end:
            remaining = end - t
            return Session(sid, label, color, quality, threshold, remaining)
        prev_end = end

    return Session(SessionId.CLOSED, "CLOSED", "#475569", 0, 99, 0)


def is_or_complete() -> bool:
    et = get_et_now()
    return et.hour * 60 + et.minute >= 570 + OR_DURATION_MINS


def is_market_open() -> bool:
    et = get_et_now()
    t  = et.hour * 60 + et.minute
    return 570 <= t < 960


def is_trading_allowed() -> bool:
    et = get_et_now()
    t  = et.hour * 60 + et.minute
    return 570 <= t < 945


def get_or_start_ts() -> int:
    et = get_et_now()
    start = et.replace(hour=9, minute=30, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def get_or_end_ts() -> int:
    et = get_et_now()
    end = et.replace(hour=10, minute=0, second=0, microsecond=0)
    return int(end.timestamp() * 1000)
