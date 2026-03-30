import time
import pytz
from datetime import datetime
from ..constants import VIX_HARD_BLOCK, VIX_REDUCE_HALF, VIX_REDUCE_QUARTER, MIN_LEVEL_SCORE, GLOBAL_PAUSE_SEC, ASSET_COOLDOWN_SEC, MAX_SIGNALS_PER_ASSET
from ..context.session import is_signal_allowed
from ..context.sim_clock import now_et

ET = pytz.timezone("America/New_York")


class GateSystem:
    """Hard mechanical gates only. Judgment calls (VIX sizing, dead zone,
    counter-trend, session quality) are left to the LLM agent."""

    def __init__(self):
        self._last_signal_time: float = 0.0
        self._asset_last_signal: dict = {}
        self._daily_counts: dict = {}
        self._reset_date: str = ""
        self.sim_mode: bool = False

    def _check_daily_reset(self):
        today = now_et().strftime("%Y-%m-%d")
        if today != self._reset_date:
            self._daily_counts = {}
            self._reset_date = today
            self._last_signal_time = 0.0
            self._asset_last_signal = {}

    def check_all(self, asset: str, level_score: int, volume_ratio: float, vix: float, direction: str, day_bias: str) -> tuple[bool, str]:
        self._check_daily_reset()

        # Signal hours — hard rule (agent can't override clock)
        if not self.sim_mode and not is_signal_allowed():
            return False, "Outside signal hours (10am-3:30pm)"

        # VIX hard block — only at extreme (35+), agent handles 25-35
        if vix >= VIX_HARD_BLOCK:
            return False, f"VIX {vix:.1f} >= {VIX_HARD_BLOCK} hard block"

        # Level score minimum — already pre-filtered in detection
        if level_score < MIN_LEVEL_SCORE:
            return False, f"Level score {level_score} < minimum {MIN_LEVEL_SCORE}"

        # Global pause between signals — mechanical cooldown
        elapsed = time.time() - self._last_signal_time
        if elapsed < GLOBAL_PAUSE_SEC:
            return False, f"Global pause: {int(GLOBAL_PAUSE_SEC - elapsed)}s remaining"

        # Per-asset cooldown
        elapsed_asset = time.time() - self._asset_last_signal.get(asset, 0)
        if elapsed_asset < ASSET_COOLDOWN_SEC:
            return False, f"{asset} cooldown: {int(ASSET_COOLDOWN_SEC - elapsed_asset)}s remaining"

        # Daily budget per asset
        count = self._daily_counts.get(asset, 0)
        if count >= MAX_SIGNALS_PER_ASSET:
            return False, f"{asset} budget exhausted ({count}/{MAX_SIGNALS_PER_ASSET})"

        # Everything else (VIX sizing, dead zone caution, counter-trend,
        # session quality, volume judgment) → agent decides
        return True, ""

    def record_signal(self, asset: str):
        self._check_daily_reset()
        now = time.time()
        self._last_signal_time = now
        self._asset_last_signal[asset] = now
        self._daily_counts[asset] = self._daily_counts.get(asset, 0) + 1

    def get_size_modifier(self, vix: float) -> str:
        if vix >= VIX_HARD_BLOCK:
            return "SKIP"
        if vix >= VIX_REDUCE_QUARTER:
            return "QUARTER"
        if vix >= VIX_REDUCE_HALF:
            return "HALF"
        return "FULL"

    def get_status(self, asset: str) -> dict:
        self._check_daily_reset()
        return {
            "global_pause_remaining": max(0, GLOBAL_PAUSE_SEC - (time.time() - self._last_signal_time)),
            "asset_cooldown_remaining": max(0, ASSET_COOLDOWN_SEC - (time.time() - self._asset_last_signal.get(asset, 0))),
            "signals_today": self._daily_counts.get(asset, 0),
            "budget_remaining": MAX_SIGNALS_PER_ASSET - self._daily_counts.get(asset, 0),
        }
