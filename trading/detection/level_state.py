import time
from ..models import Candle, LevelState, TrackerStatus
from ..constants import MAX_RETEST_CANDLES, RETEST_TIMEOUT_SEC, RETEST_PROXIMITY, RETEST_VOL_MIN
from ..context.sim_clock import is_sim


class TrackerEngine:
    def __init__(self):
        self._trackers: dict[str, LevelState] = {}
        self._locked: set[str] = set()

    def _key(self, asset: str, level_name: str) -> str:
        return f"{asset}_{level_name}"

    def start(self, asset: str, level_name: str, level_price: float, level_score: int,
              direction: str, break_candle: Candle, cvd_at_break: float, volume_ratio: float) -> bool:
        key = self._key(asset, level_name)
        if key in self._locked:
            return False
        self._trackers[key] = LevelState(
            asset=asset, level_name=level_name, level_price=level_price, level_score=level_score,
            direction=direction, break_candle=break_candle, break_time=time.time(),
            cvd_at_break=cvd_at_break, volume_ratio=volume_ratio,
            status=TrackerStatus.WATCHING.value, expires_at=time.time() + RETEST_TIMEOUT_SEC,
        )
        return True

    def on_1m_close(self, asset: str, candle: Candle, cvd_now: float, cvd_1min_ago: float,
                    avg_volume: float, current_price: float, atr: float) -> tuple[list[LevelState], list[LevelState]]:
        confirmed = []
        failed = []
        to_remove = []

        for key, tracker in list(self._trackers.items()):
            if tracker.asset != asset:
                continue
            tracker.candles_watched.append(candle)

            # Time-based expiry (skip in sim mode — wall clock doesn't match bar timestamps)
            if not is_sim() and time.time() > tracker.expires_at:
                tracker.status = TrackerStatus.EXPIRED.value
                to_remove.append(key)
                continue
            # Candle-based expiry (works in both live and sim)
            if len(tracker.candles_watched) > MAX_RETEST_CANDLES:
                tracker.status = TrackerStatus.EXPIRED.value
                to_remove.append(key)
                continue
            dist = abs(current_price - tracker.level_price)
            if dist > atr * 1.5:
                tracker.status = TrackerStatus.EXPIRED.value
                to_remove.append(key)
                continue

            result = self._check_retest(tracker, candle, cvd_now, cvd_1min_ago, avg_volume, atr)
            if result == "CONFIRMED":
                tracker.status = TrackerStatus.CONFIRMED.value
                tracker.retest_candle = candle
                tracker.cvd_at_retest = cvd_now
                confirmed.append(tracker)
                to_remove.append(key)
                self._locked.add(key)
            elif result == "FAILED":
                tracker.status = TrackerStatus.FAILED.value
                failed.append(tracker)
                to_remove.append(key)

        for key in to_remove:
            self._trackers.pop(key, None)
        return confirmed, failed

    def _check_retest(self, tracker: LevelState, candle: Candle, cvd_now: float,
                      cvd_1min_ago: float, avg_volume: float, atr: float = 0.5) -> str:
        level = tracker.level_price
        direction = tracker.direction
        proximity = atr * 0.2

        # CVD check removed — CVD mean-reverts naturally during pullbacks.
        # The LLM agent evaluates CVD quality. Mechanical system confirms structure only.

        if direction == "BULLISH":
            if candle.l > level + proximity:
                return "WATCHING"  # hasn't retested yet
            if candle.c <= level:
                return "FAILED"  # closed back through
            # Volume: minimum 0.5x avg, ceiling at 1.5x breakout volume
            # Allows continuation interest (similar vol) but blocks new breakout attempts (2x+ vol)
            if avg_volume > 0:
                if candle.v < avg_volume * 0.5:
                    return "WATCHING"
                if candle.v >= tracker.break_candle.v * 1.5:
                    return "WATCHING"
            return "CONFIRMED"
        elif direction == "BEARISH":
            if candle.h < level - proximity:
                return "WATCHING"
            if candle.c >= level:
                return "FAILED"
            if avg_volume > 0:
                if candle.v < avg_volume * 0.5:
                    return "WATCHING"
                if candle.v >= tracker.break_candle.v * 1.5:
                    return "WATCHING"
            return "CONFIRMED"
        return "WATCHING"

    def invalidate_on_5m_reversal(self, asset: str, level_name: str) -> bool:
        key = self._key(asset, level_name)
        if key in self._trackers:
            self._trackers[key].status = TrackerStatus.FAILED.value
            self._trackers.pop(key, None)
            return True
        return False

    def is_locked(self, asset: str, level_name: str) -> bool:
        return self._key(asset, level_name) in self._locked

    def active_for_asset(self, asset: str) -> list[LevelState]:
        return [t for t in self._trackers.values()
                if t.asset == asset and t.status == TrackerStatus.WATCHING.value]

    def reset_daily(self):
        self._trackers.clear()
        self._locked.clear()
