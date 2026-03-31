import asyncio

from ..models import CVDPoint, Candle
from ..context.sim_clock import now_et
import pytz
from datetime import datetime

ET = pytz.timezone("America/New_York")


class AssetCVDEngine:
    """Bar-based CVD engine.

    Uses 1m bar close vs open to classify volume:
      close > open → bar volume added as "buy"
      close < open → bar volume subtracted as "sell"
      close == open → ignored

    This produces meaningful CVD with any data source (ticks or bars)
    unlike tick-comparison CVD which requires dense, non-batched tick data.
    """
    def __init__(self, asset: str):
        self.asset = asset
        self._cvd: float = 0.0
        self._session_date: str = ""
        self._history: list[CVDPoint] = []
        self._minute_cvd: float = 0.0
        self._current_minute: str = ""
        self._total_volume: float = 0.0
        # Track per-bar accumulation for bar-based CVD
        self._bar_open: float = 0.0
        self._bar_volume: float = 0.0
        self._bar_close: float = 0.0
        self._last_price: float = 0.0
        # v2.1 additions
        self._lock = asyncio.Lock()
        self._estimated: bool = False  # True when CVD reconstructed from bars
        self._cvd_history: list = []   # list of abs(cvd_turn) values for rolling avg

    def process_trade(self, price: float, volume: float) -> float:
        """Process a tick — accumulates volume for the current bar.
        CVD is updated when a new minute starts (bar closes)."""
        now = now_et()
        today = now.strftime("%Y-%m-%d")
        t_min = now.hour * 60 + now.minute

        # Reset at 9:30 AM ET
        if today != self._session_date and t_min >= 570:
            self.reset()
            self._session_date = today
        elif today == self._session_date and t_min == 570 and self._total_volume > 0:
            if self._current_minute and self._current_minute < "09:30":
                self.reset()
                self._session_date = today

        minute_key = now.strftime("%H:%M")
        if minute_key != self._current_minute:
            # New minute — close the previous bar and calculate CVD delta
            if self._current_minute and self._bar_volume > 0:
                # Bar-based CVD: close > open = buy volume, close < open = sell volume
                if self._bar_close > self._bar_open:
                    self._cvd += self._bar_volume
                    delta = self._bar_volume
                elif self._bar_close < self._bar_open:
                    self._cvd -= self._bar_volume
                    delta = -self._bar_volume
                else:
                    delta = 0

                self._history.append(CVDPoint(
                    time_et=self._current_minute,
                    value=self._cvd,
                    delta=delta,
                ))
                if len(self._history) > 60:
                    self._history = self._history[-60:]

            self._minute_cvd = self._cvd
            self._current_minute = minute_key
            # Start new bar
            self._bar_open = price
            self._bar_volume = 0.0
            self._bar_close = price

        # Accumulate volume and track close price for this bar
        self._bar_volume += volume
        self._bar_close = price
        self._last_price = price
        self._total_volume += volume
        return self._cvd

    def process_bar(self, bar) -> float:
        """Process a complete 1m bar directly (for backtesting).
        Avoids the tick-by-tick path entirely."""
        now = now_et()
        today = now.strftime("%Y-%m-%d")
        t_min = now.hour * 60 + now.minute

        if today != self._session_date and t_min >= 570:
            self.reset()
            self._session_date = today

        if bar.c > bar.o:
            self._cvd += bar.v
            delta = bar.v
        elif bar.c < bar.o:
            self._cvd -= bar.v
            delta = -bar.v
        else:
            delta = 0

        minute_key = now.strftime("%H:%M")
        self._history.append(CVDPoint(
            time_et=minute_key,
            value=self._cvd,
            delta=delta,
        ))
        if len(self._history) > 60:
            self._history = self._history[-60:]

        self._minute_cvd = self._cvd
        self._current_minute = minute_key
        self._total_volume += bar.v
        self._last_price = bar.c
        return self._cvd

    async def process_trade_async(self, price: float, volume: float):
        """Thread-safe version for async context."""
        async with self._lock:
            self.process_trade(price, volume)

    def set_estimated(self, value: bool):
        self._estimated = value

    @property
    def is_estimated(self) -> bool:
        return self._estimated

    def rolling_avg_cvd_turn(self, window: int = 10) -> float:
        """Rolling average of abs(cvd_turn) over last N turns."""
        sample = self._cvd_history[-window:]
        if not sample:
            return 1.0
        return sum(abs(v) for v in sample) / len(sample)

    def record_cvd_turn(self, turn: float):
        """Call after each candle close to build rolling history."""
        self._cvd_history.append(turn)
        if len(self._cvd_history) > 200:  # cap history
            self._cvd_history = self._cvd_history[-200:]

    def reset(self):
        self._cvd = 0.0
        self._session_date = ""
        self._history = []
        self._minute_cvd = 0.0
        self._current_minute = ""
        self._total_volume = 0.0
        self._bar_open = 0.0
        self._bar_volume = 0.0
        self._bar_close = 0.0
        self._last_price = 0.0
        self._estimated = False
        self._cvd_history = []

    def get_history(self, minutes: int = 30) -> list[CVDPoint]:
        return self._history[-minutes:]

    def detect_divergence(self, candles_1m: list[Candle], lookback: int = 10) -> dict:
        if len(candles_1m) < lookback or len(self._history) < lookback:
            return {"type": "NONE", "detail": "Not enough data"}
        recent_candles = candles_1m[-lookback:]
        recent_cvd = self._history[-lookback:]
        price_highs = [c.h for c in recent_candles]
        price_lows = [c.l for c in recent_candles]
        cvd_vals = [pt.value for pt in recent_cvd]
        mid = lookback // 2
        if max(price_highs[mid:]) > max(price_highs[:mid]) and max(cvd_vals[mid:]) < max(cvd_vals[:mid]):
            return {"type": "BEARISH_DIVERGENCE", "detail": "Price higher high but CVD lower — fake rally"}
        if min(price_lows[mid:]) < min(price_lows[:mid]) and min(cvd_vals[mid:]) > min(cvd_vals[:mid]):
            return {"type": "BULLISH_DIVERGENCE", "detail": "Price lower low but CVD higher — fake selloff"}
        return {"type": "NONE", "detail": "No divergence"}

    @property
    def value(self) -> float:
        return self._cvd

    @property
    def value_1min_ago(self) -> float:
        return self._history[-1].value if self._history else 0.0

    @property
    def value_5min_ago(self) -> float:
        return self._history[-5].value if len(self._history) >= 5 else (self._history[0].value if self._history else 0.0)

    @property
    def bias(self) -> str:
        threshold = max(self._total_volume * 0.005, 500)
        if self._cvd > threshold:
            return "BUYERS"
        elif self._cvd < -threshold:
            return "SELLERS"
        return "NEUTRAL"


class MultiCVDEngine:
    def __init__(self):
        from ..constants import ASSETS
        self._engines = {asset: AssetCVDEngine(asset) for asset in ASSETS}

    def get(self, asset: str) -> AssetCVDEngine:
        return self._engines[asset]

    def process_trade(self, asset: str, price: float, volume: float) -> float:
        if asset in self._engines:
            return self._engines[asset].process_trade(price, volume)
        return 0.0

    def reset_all(self):
        for engine in self._engines.values():
            engine.reset()

    def value(self, asset: str) -> float:
        return self._engines[asset].value

    def bias(self, asset: str) -> str:
        return self._engines[asset].bias
