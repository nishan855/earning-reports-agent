from ..models import Candle, Level
from ..constants import REJECTION_VOL_MIN


def detect_rejection(
    candle: Candle, level: Level, avg_volume: float,
    cvd_at_open: float, cvd_at_close: float,
    atr: float = 0.5,
) -> tuple[bool, str, str]:
    body = candle.body
    upper_wick = candle.upper_wick
    lower_wick = candle.lower_wick
    total_range = candle.h - candle.l

    if atr <= 0 or avg_volume <= 0:
        return False, "", ""

    # Rejection uses RANGE check instead of body check (allows Doji rejections)
    # The candle must have meaningful total size — 50% of ATR
    if total_range < atr * 0.5:
        return False, "", ""

    if candle.v / avg_volume < REJECTION_VOL_MIN:
        return False, "", ""

    cvd_change = cvd_at_close - cvd_at_open
    if abs(cvd_change) < avg_volume * 0.003:
        return False, "", ""

    # Bearish rejection at resistance — ATR-based proximity
    touched_high = candle.h >= level.price - (atr * 0.2)
    closed_below = candle.c < level.price
    wick_ratio_u = upper_wick / body if body > 0.001 else (10.0 if upper_wick > 0 else 0)
    if touched_high and closed_below and wick_ratio_u >= 1.5 and cvd_change < 0:
        # Close distance band: filter noise and breakdowns
        close_dist = level.price - candle.c
        if close_dist < atr * 0.1:
            return False, "", ""  # too close — market noise, not conviction
        if close_dist > atr * 1.0:
            return False, "", ""  # too far — this is a breakdown, not a rejection
        strength = "STRONG" if wick_ratio_u >= 2.5 else "MODERATE"
        return True, "BEARISH", strength

    # Bullish rejection at support — ATR-based proximity
    touched_low = candle.l <= level.price + (atr * 0.2)
    closed_above = candle.c > level.price
    wick_ratio_l = lower_wick / body if body > 0.001 else (10.0 if lower_wick > 0 else 0)
    if touched_low and closed_above and wick_ratio_l >= 1.5 and cvd_change > 0:
        close_dist = candle.c - level.price
        if close_dist < atr * 0.1:
            return False, "", ""
        if close_dist > atr * 1.0:
            return False, "", ""
        strength = "STRONG" if wick_ratio_l >= 2.5 else "MODERATE"
        return True, "BULLISH", strength

    return False, "", ""
