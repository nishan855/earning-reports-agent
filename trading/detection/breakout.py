from ..models import Candle, Level
from ..constants import BREAKOUT_VOL_MIN


def detect_breakout(
    candle: Candle, prev_candle: Candle, level: Level,
    avg_volume: float, cvd_at_open: float, cvd_at_close: float,
    atr: float = 0.5,
) -> tuple[bool, str]:
    body = candle.body
    if atr <= 0 or avg_volume <= 0:
        return False, ""
    # Body must be meaningful — 15% of ATR
    if body < atr * 0.15:
        return False, ""
    if candle.v / avg_volume < BREAKOUT_VOL_MIN:
        return False, ""

    cvd_change = cvd_at_close - cvd_at_open
    # CVD magnitude must be meaningful (>= 0.3% of avg volume)
    if abs(cvd_change) < avg_volume * 0.003:
        return False, ""

    # Exhaustion cap: reject breakouts that have already over-extended past the level
    max_extension = atr * 1.5

    # Bullish breakout: price crosses above level with buyer CVD
    if (prev_candle.c < level.price or candle.o < level.price) and candle.c > level.price and cvd_change > 0:
        if (candle.c - level.price) > max_extension:
            return False, ""  # over-extended — likely exhaustion, not conviction
        return True, "BULLISH"

    # Bearish breakout: price crosses below level with seller CVD
    if (prev_candle.c > level.price or candle.o > level.price) and candle.c < level.price and cvd_change < 0:
        if (level.price - candle.c) > max_extension:
            return False, ""  # over-extended
        return True, "BEARISH"

    return False, ""


def is_back_through(candle: Candle, level: Level, direction: str) -> bool:
    if direction == "BULLISH":
        return candle.c < level.price
    if direction == "BEARISH":
        return candle.c > level.price
    return False
