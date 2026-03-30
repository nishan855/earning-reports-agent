from ..models import Candle, Level
from ..constants import STOP_HUNT_VOL_MIN


def detect_stop_hunt(
    candle: Candle, level: Level, avg_volume: float, cvd_change: float,
    atr: float = 0.5,
) -> tuple[bool, str]:
    body = candle.body
    upper_wick = candle.upper_wick
    lower_wick = candle.lower_wick

    if atr <= 0 or avg_volume <= 0:
        return False, ""
    # Body must be meaningful — 15% of ATR
    if body < atr * 0.15:
        return False, ""
    if candle.v < avg_volume * STOP_HUNT_VOL_MIN:
        return False, ""

    # CVD PARADOX FIX:
    # On a bullish stop hunt, the sweep candle absorbs triggered stop-losses
    # (market sells), so its CVD is naturally NEGATIVE even on a bullish reclaim.
    # We accept either:
    #   a) CVD divergence: heavy selling absorbed (cvd_change < -threshold AND close > level)
    #   b) Neutral CVD: the sweep candle's CVD is inconclusive — defer to confirmation candle
    # We do NOT require cvd_change > 0 on the sweep candle itself.
    cvd_threshold = avg_volume * 0.003

    # Bullish stop hunt below support
    if candle.l < level.price and candle.c > level.price and lower_wick > body:
        # Accept if: selling was absorbed (negative CVD = stops triggered) OR CVD is meaningfully positive
        if cvd_change < -cvd_threshold or cvd_change > cvd_threshold:
            return True, "BULLISH"

    # Bearish stop hunt above resistance
    if candle.h > level.price and candle.c < level.price and upper_wick > body:
        # Accept if: buying was absorbed (positive CVD = stops triggered) OR CVD is meaningfully negative
        if cvd_change > cvd_threshold or cvd_change < -cvd_threshold:
            return True, "BEARISH"

    return False, ""


def confirm_stop_hunt(
    confirm_candle: Candle, direction: str, level: Level,
    cvd_now: float, cvd_1min_ago: float,
) -> bool:
    """Confirmation candle must hold reclaim side AND CVD must align with direction.
    This is where we require CVD to confirm — not on the sweep candle."""
    if direction == "BULLISH":
        # Price holds above level AND CVD is now positive (buyers stepping in)
        return confirm_candle.c > level.price and cvd_now > cvd_1min_ago
    if direction == "BEARISH":
        # Price holds below level AND CVD is now negative (sellers stepping in)
        return confirm_candle.c < level.price and cvd_now < cvd_1min_ago
    return False
