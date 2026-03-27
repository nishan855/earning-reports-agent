from .sessions import get_et_now
from .models import CVDPoint


class CVDEngine:
    def __init__(self):
        self._cvd: float = 0.0
        self._last_price: float | None = None
        self._session_date: str = ""
        self._history: list[CVDPoint] = []
        self._minute_cvd: float = 0.0
        self._current_minute: str = ""

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

        self._last_price = price
        return self._cvd

    def reset(self):
        self._cvd = 0.0
        self._last_price = None
        self._history = []
        self._minute_cvd = 0.0
        self._current_minute = ""

    def get_history(self, minutes: int = 30) -> list[CVDPoint]:
        return self._history[-minutes:]

    @property
    def value(self) -> float:
        return self._cvd

    @property
    def bias(self) -> str:
        if self._cvd > 500:
            return "BUYERS"
        elif self._cvd < -500:
            return "SELLERS"
        return "NEUTRAL"
