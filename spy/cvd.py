from .sessions import get_et_now
from .models import CVDPoint, Candle


class CVDEngine:
    def __init__(self):
        self._cvd: float = 0.0
        self._last_price: float | None = None
        self._session_date: str = ""
        self._history: list[CVDPoint] = []
        self._minute_cvd: float = 0.0
        self._current_minute: str = ""
        self._total_volume: float = 0.0

    def process_trade(self, price: float, volume: float) -> float:
        now = get_et_now()
        today = now.strftime("%Y-%m-%d")
        if today != self._session_date:
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
        # Check if price made higher high but CVD made lower high
        if max(price_highs[mid:]) > max(price_highs[:mid]) and max(cvd_vals[mid:]) < max(cvd_vals[:mid]):
            return {"type": "BEARISH_DIVERGENCE", "detail": "Price higher high but CVD lower — fake rally, sellers absorbing"}
        # Check if price made lower low but CVD made higher low
        if min(price_lows[mid:]) < min(price_lows[:mid]) and min(cvd_vals[mid:]) > min(cvd_vals[:mid]):
            return {"type": "BULLISH_DIVERGENCE", "detail": "Price lower low but CVD higher — fake selloff, buyers absorbing"}
        return {"type": "NONE", "detail": "No divergence"}

    @property
    def value(self) -> float:
        return self._cvd

    @property
    def bias(self) -> str:
        threshold = max(self._total_volume * 0.005, 500)
        if self._cvd > threshold:
            return "BUYERS"
        elif self._cvd < -threshold:
            return "SELLERS"
        return "NEUTRAL"
