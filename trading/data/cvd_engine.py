from ..models import CVDPoint, Candle
from ..context.sim_clock import now_et
import pytz
from datetime import datetime

ET = pytz.timezone("America/New_York")


class AssetCVDEngine:
    def __init__(self, asset: str):
        self.asset = asset
        self._cvd: float = 0.0
        self._last_price: float | None = None
        self._session_date: str = ""
        self._history: list[CVDPoint] = []
        self._minute_cvd: float = 0.0
        self._current_minute: str = ""
        self._total_volume: float = 0.0

    def process_trade(self, price: float, volume: float) -> float:
        now = now_et()
        today = now.strftime("%Y-%m-%d")
        t_min = now.hour * 60 + now.minute
        # Reset at 9:30 AM ET (market open), not midnight
        # This prevents pre-market volume from skewing the regular session baseline
        if today != self._session_date and t_min >= 570:
            self.reset()
            self._session_date = today
        elif today == self._session_date and t_min == 570 and self._total_volume > 0:
            # Crossed into 9:30 on same day — reset pre-market accumulation
            if self._current_minute and self._current_minute < "09:30":
                self.reset()
                self._session_date = today

        minute_key = now.strftime("%H:%M")
        if minute_key != self._current_minute:
            if self._current_minute:
                self._history.append(CVDPoint(
                    time_et=self._current_minute,
                    value=self._cvd,
                    delta=self._cvd - self._minute_cvd,
                ))
                if len(self._history) > 60:
                    self._history = self._history[-60:]
            self._minute_cvd = self._cvd
            self._current_minute = minute_key

        if self._last_price is not None:
            if price > self._last_price:
                self._cvd += volume
            elif price < self._last_price:
                self._cvd -= volume
        self._total_volume += volume
        self._last_price = price
        return self._cvd

    def reset(self):
        self._cvd = 0.0
        self._last_price = None
        self._history = []
        self._minute_cvd = 0.0
        self._current_minute = ""
        self._total_volume = 0.0

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
