from .sessions import get_et_now


class CVDEngine:
    def __init__(self):
        self._cvd:        float = 0.0
        self._last_price: float | None = None
        self._session_date: str = ""

    def process_trade(self, price: float, volume: float) -> float:
        today = get_et_now().strftime("%Y-%m-%d")
        if today != self._session_date:
            self.reset()
            self._session_date = today

        if self._last_price is not None:
            if price > self._last_price:
                self._cvd += volume
            elif price < self._last_price:
                self._cvd -= volume

        self._last_price = price
        return self._cvd

    def reset(self):
        self._cvd        = 0.0
        self._last_price = None

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
