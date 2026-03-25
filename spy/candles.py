import asyncio
from .models import Candle
from .sessions import get_or_start_ts, get_or_end_ts
from typing import Callable


class CandleStore:
    def __init__(self):
        self.c1m:     list[Candle] = []
        self.c5m:     list[Candle] = []
        self.c15m:    list[Candle] = []
        self.c_daily: list[Candle] = []
        self._last_eval_ts: int = 0
        self._on_close_callbacks: list[Callable] = []

    def load(self, c1m, c5m, c15m, c_daily):
        self.c1m     = c1m
        self.c5m     = c5m
        self.c15m    = c15m
        self.c_daily = c_daily

    def update_live(self, price: float, volume: float, ts: int):
        if not self.c1m:
            return

        current_min_ts = (ts // 60_000) * 60_000
        last = self.c1m[-1]
        last_min_ts = (last.t // 60_000) * 60_000

        if current_min_ts > last_min_ts:
            new_candle = Candle(
                t=current_min_ts,
                o=price, h=price, l=price, c=price, v=volume
            )
            self.c1m.append(new_candle)

            if len(self.c1m) > 500:
                self.c1m = self.c1m[-500:]

            closed_candle = self.c1m[-2]
            if closed_candle.t != self._last_eval_ts:
                self._last_eval_ts = closed_candle.t
                asyncio.create_task(self._fire_close_callbacks())
        else:
            last.c  = price
            last.h  = max(last.h, price)
            last.l  = min(last.l, price)
            last.v += volume
            self.c1m[-1] = last

    def on_candle_close(self, callback: Callable):
        self._on_close_callbacks.append(callback)

    async def _fire_close_callbacks(self):
        for cb in self._on_close_callbacks:
            try:
                await cb()
            except Exception as e:
                print(f"Candle close callback error: {e}")

    @property
    def closed_1m(self) -> list[Candle]:
        return self.c1m[:-1] if len(self.c1m) > 1 else []

    @property
    def closed_5m(self) -> list[Candle]:
        return self.c5m[:-1] if len(self.c5m) > 1 else []

    @property
    def closed_15m(self) -> list[Candle]:
        return self.c15m[:-1] if len(self.c15m) > 1 else []

    @property
    def live_price(self) -> float:
        return self.c1m[-1].c if self.c1m else 0.0

    @property
    def today_candles_1m(self) -> list[Candle]:
        or_start = get_or_start_ts()
        return [c for c in self.closed_1m if c.t >= or_start]

    @property
    def or_candles(self) -> list[Candle]:
        start = get_or_start_ts()
        end   = get_or_end_ts()
        return [c for c in self.c1m if start <= c.t < end]
