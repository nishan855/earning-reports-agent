import math
import pytz
from datetime import datetime, timedelta
from ..core.asset_registry import get_config, has_daily_expiry
from .sim_clock import now_et

ET = pytz.timezone("America/New_York")


def get_options_env(vix: float) -> dict:
    if vix < 15: return {"label": "CALM", "size": "FULL", "instrument": "ATM outright"}
    if vix < 20: return {"label": "NORMAL", "size": "FULL", "instrument": "ATM outright"}
    if vix < 25: return {"label": "ELEVATED", "size": "FULL", "instrument": "ATM or 1 OTM"}
    if vix < 30: return {"label": "HIGH", "size": "HALF", "instrument": "debit spread"}
    if vix < 35: return {"label": "VERY HIGH", "size": "QUARTER", "instrument": "spread only"}
    return {"label": "EXTREME", "size": "SKIP", "instrument": "none"}


def get_strike(asset: str, price: float, direction: str, vix: float) -> float:
    cfg = get_config(asset)
    minor = cfg["round_interval_minor"]
    atm = round(round(price / minor) * minor, 2)
    if vix < 23:
        return atm
    return atm + minor if direction == "BULLISH" else atm - minor


def get_expiry(asset: str, signal_hour: float = 0, confidence: str = "MEDIUM") -> tuple:
    """
    Returns (dte: int, expiry_date_str: str, skip: bool).

    Rolling Fridays model — NO skip days:
      Monday:     Current Friday  (4 DTE)
      Tuesday:    Current Friday  (3 DTE)
      Wednesday:  Current Friday  (2 DTE)
      Thursday:   NEXT Friday     (8 DTE)
      Friday:     NEXT Friday     (7 DTE)

    Thursday and Friday shift forward one week to maintain 2-5 DTE minimum
    and avoid 0-1 DTE gamma/theta traps. skip is always False.
    """
    now = now_et()
    dow = now.weekday()  # 0=Mon 4=Fri

    # Current week's Friday
    days_to_cur_fri = 4 - dow
    cur_friday = now.date() + timedelta(days=days_to_cur_fri)

    # Next week's Friday
    next_friday = cur_friday + timedelta(days=7)

    if dow <= 2:  # Mon (0), Tue (1), Wed (2) → current Friday
        expiry = cur_friday
    else:  # Thu (3), Fri (4) → next Friday
        expiry = next_friday

    dte = (expiry - now.date()).days
    expiry_str = expiry.strftime("%b %d")
    return dte, expiry_str, False


def estimate_premium(price: float, vix: float) -> tuple[float, float]:
    daily_move = (price * (vix / 100)) / math.sqrt(252)
    mid = daily_move * 0.4
    return round(mid * 0.8, 2), round(mid * 1.2, 2)
