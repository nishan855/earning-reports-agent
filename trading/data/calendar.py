"""
Market calendar for 2026.
Hardcoded dates + yfinance earnings integration.

Macro event windows:
  - FOMC / Fed Chair:        ±30 minutes
  - CPI/NFP/PCE/PPI/Retail:  ±15 minutes
"""

from datetime import date, datetime, timedelta
import pytz

ET = pytz.timezone("America/New_York")

# ── 2026 MARKET HOLIDAYS ──────────────────────────────────────────
MARKET_HOLIDAYS_2026 = {
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
}

# ── 2026 EARLY CLOSE DAYS (1:00 PM ET) ───────────────────────────
EARLY_CLOSE_DAYS_2026 = {
    date(2026, 11, 27),  # Day before Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
    date(2026, 12, 31),  # New Year's Eve
}

# ── 2026 MACRO EVENTS (date, time_et_hhmm, window_minutes, label) ─
MACRO_EVENTS_2026 = [
    # FOMC decisions (±30 min)
    (date(2026, 1, 29),  1400, 30, "FOMC"),
    (date(2026, 3, 19),  1400, 30, "FOMC"),
    (date(2026, 5, 7),   1400, 30, "FOMC"),
    (date(2026, 6, 18),  1400, 30, "FOMC"),
    (date(2026, 7, 30),  1400, 30, "FOMC"),
    (date(2026, 9, 17),  1400, 30, "FOMC"),
    (date(2026, 11, 5),  1400, 30, "FOMC"),
    (date(2026, 12, 10), 1400, 30, "FOMC"),
    # CPI (±15 min) — 8:30 AM releases
    (date(2026, 1, 15),   830, 15, "CPI"),
    (date(2026, 2, 12),   830, 15, "CPI"),
    (date(2026, 3, 12),   830, 15, "CPI"),
    (date(2026, 4, 10),   830, 15, "CPI"),
    (date(2026, 5, 13),   830, 15, "CPI"),
    (date(2026, 6, 11),   830, 15, "CPI"),
    (date(2026, 7, 15),   830, 15, "CPI"),
    (date(2026, 8, 13),   830, 15, "CPI"),
    (date(2026, 9, 11),   830, 15, "CPI"),
    (date(2026, 10, 14),  830, 15, "CPI"),
    (date(2026, 11, 12),  830, 15, "CPI"),
    (date(2026, 12, 10),  830, 15, "CPI"),
    # NFP (±15 min) — first Friday of month 8:30 AM
    (date(2026, 1, 9),    830, 15, "NFP"),
    (date(2026, 2, 6),    830, 15, "NFP"),
    (date(2026, 3, 6),    830, 15, "NFP"),
    (date(2026, 4, 3),    830, 15, "NFP"),
    (date(2026, 5, 1),    830, 15, "NFP"),
    (date(2026, 6, 5),    830, 15, "NFP"),
    (date(2026, 7, 10),   830, 15, "NFP"),
    (date(2026, 8, 7),    830, 15, "NFP"),
    (date(2026, 9, 4),    830, 15, "NFP"),
    (date(2026, 10, 2),   830, 15, "NFP"),
    (date(2026, 11, 6),   830, 15, "NFP"),
    (date(2026, 12, 4),   830, 15, "NFP"),
]


def is_market_holiday(d: date = None) -> bool:
    if d is None:
        d = datetime.now(ET).date()
    return d in MARKET_HOLIDAYS_2026


def is_early_close(d: date = None) -> bool:
    if d is None:
        d = datetime.now(ET).date()
    return d in EARLY_CLOSE_DAYS_2026


def get_cutoff_time(d: date = None) -> int:
    """Returns cutoff time as HHMM integer."""
    if d is None:
        d = datetime.now(ET).date()
    if d in EARLY_CLOSE_DAYS_2026:
        return 1230  # 12:30 PM on early close days
    return 1515      # 3:15 PM normally


def is_macro_halt(now: datetime = None) -> tuple:
    """
    Returns (is_halted: bool, reason: str).
    Checks if current time is within any macro event window.
    """
    if now is None:
        now = datetime.now(ET)

    today = now.date()
    t_hhmm = now.hour * 100 + now.minute

    for evt_date, evt_time, window_min, label in MACRO_EVENTS_2026:
        if evt_date != today:
            continue

        evt_dt = ET.localize(datetime(
            today.year, today.month, today.day,
            evt_time // 100, evt_time % 100
        ))

        delta = abs((now - evt_dt).total_seconds() / 60)
        if delta <= window_min:
            return True, f"{label} ±{window_min}min window"

    return False, ""


def is_earnings_within_hold(
    asset: str,
    hold_days: int = 5,
) -> tuple:
    """
    Returns (blocked: bool, reason: str).
    Uses yfinance to check upcoming earnings.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(asset)
        cal    = ticker.calendar
        if cal is None or cal.empty:
            return False, ""

        # calendar has 'Earnings Date' row
        if "Earnings Date" in cal.index:
            earn_date = cal.loc["Earnings Date"].iloc[0]
            if hasattr(earn_date, 'date'):
                earn_date = earn_date.date()
            today     = datetime.now(ET).date()
            days_away = (earn_date - today).days
            if 0 <= days_away <= hold_days:
                return True, f"{asset} earnings in {days_away}d (within {hold_days}d hold)"

    except Exception:
        pass  # fail open — don't block on yfinance errors

    return False, ""
