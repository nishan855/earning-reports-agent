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


def get_expiry(asset: str, signal_hour: float, confidence: str = "MEDIUM") -> tuple[int, str]:
    now = now_et()
    dow = now.weekday()
    daily_exp = has_daily_expiry(asset)
    if signal_hour < 11.0:
        dte = 0 if (dow == 4 and daily_exp) else (1 if daily_exp else 2)
    elif signal_hour < 13.0:
        dte = 1 if daily_exp else 2
    elif signal_hour < 14.5:
        dte = 0 if daily_exp else 1
    else:
        dte = 0
    if confidence == "HIGH" and dte < 2:
        dte = min(dte + 1, 3)
    expiry_dt = now + timedelta(days=dte)
    return dte, expiry_dt.strftime("%b %d")


def estimate_premium(price: float, vix: float) -> tuple[float, float]:
    daily_move = (price * (vix / 100)) / math.sqrt(252)
    mid = daily_move * 0.4
    return round(mid * 0.8, 2), round(mid * 1.2, 2)
