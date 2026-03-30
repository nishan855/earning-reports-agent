from ..models import Candle, LevelState


def detect_failed_retest(retest_candle: Candle, tracker: LevelState, atr: float = 0.5) -> bool:
    """Check if a retest candle failed — closed back through the level with conviction."""
    level = tracker.level_price
    direction = tracker.direction
    # ATR-based proximity for "touched the level"
    proximity = atr * 0.2
    # Conviction buffer: must breach level by 10% ATR to count as failed
    # Prevents 1-cent noise from triggering false failures
    conviction_buffer = atr * 0.1

    if direction == "BULLISH":
        touched = retest_candle.l <= level + proximity
        failed = retest_candle.c < (level - conviction_buffer)
        return touched and failed
    if direction == "BEARISH":
        touched = retest_candle.h >= level - proximity
        failed = retest_candle.c > (level + conviction_buffer)
        return touched and failed
    return False


def get_reverse_direction(direction: str) -> str:
    if direction == "BULLISH":
        return "BEARISH"
    if direction == "BEARISH":
        return "BULLISH"
    return ""


def confirm_failed_breakout(
    confirm_candle: Candle, level: LevelState,
    cvd_now: float, cvd_1min_ago: float,
) -> bool:
    reverse = get_reverse_direction(level.direction)
    if reverse == "BEARISH":
        return confirm_candle.c < level.level_price and cvd_now < cvd_1min_ago
    if reverse == "BULLISH":
        return confirm_candle.c > level.level_price and cvd_now > cvd_1min_ago
    return False


def build_failed_breakout_context(
    tracker: LevelState, fail_candle: Candle,
    confirm_candle: Candle, cvd_value: float,
) -> dict:
    reverse_dir = get_reverse_direction(tracker.direction)
    return {
        "pattern": "FAILED_BREAKOUT",
        "original_break": tracker.direction,
        "reverse_dir": reverse_dir,
        "break_level": tracker.level_price,
        "break_level_name": tracker.level_name,
        "fail_candle": {"open": fail_candle.o, "high": fail_candle.h, "low": fail_candle.l, "close": fail_candle.c},
        "confirm_candle": {"open": confirm_candle.o, "high": confirm_candle.h, "low": confirm_candle.l, "close": confirm_candle.c},
        "cvd_at_confirm": cvd_value,
        "trapped_direction": tracker.direction,
        "description": (
            f"Breakout {tracker.direction} at {tracker.level_name} ${tracker.level_price:.2f} failed. "
            f"Retest closed wrong side. Trapped {tracker.direction.lower()} traders will exit. "
            f"Reverse {reverse_dir} signal."
        ),
    }
