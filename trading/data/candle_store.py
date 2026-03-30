import asyncio
import time
from datetime import datetime
from typing import Callable

import pytz

from ..models import Candle, DataHealth
from ..constants import (
    PRICE_DIVERGENCE_THRESHOLD, VOL_DIVERGENCE_THRESHOLD,
    YF_DELAY_MINUTES, YF_VALIDATE_WINDOW, HEARTBEAT_SEC,
)

ET = pytz.timezone("America/New_York")


class AssetCandleStore:
    def __init__(self, asset: str):
        self.asset = asset
        # Tick-built bars (primary — real-time)
        self.c1m: list[Candle] = []
        self.c5m_live: list[Candle] = []
        self.c15m_live: list[Candle] = []
        # yfinance bars (fallback — startup only)
        self._c5m_yf: list[Candle] = []
        self._c15m_yf: list[Candle] = []
        # Daily always from yfinance
        self.c_daily: list[Candle] = []
        # Callback state
        self._last_eval_ts: int = 0
        self._last_5m_ts: int = 0
        self._on_close_callbacks: list[Callable] = []
        self._on_5m_callbacks: list[Callable] = []
        self._pending_5m_fire: bool = False
        self._pending_15m_fire: bool = False
        # Health
        self._health = DataHealth(asset=asset)

    # ── Properties: c5m / c15m with fallback ──────────────────────

    @property
    def c5m(self) -> list[Candle]:
        return self.c5m_live if self.c5m_live else self._c5m_yf

    @c5m.setter
    def c5m(self, value):
        # Simulation mode writes directly
        self.c5m_live = value

    @property
    def c15m(self) -> list[Candle]:
        return self.c15m_live if self.c15m_live else self._c15m_yf

    @c15m.setter
    def c15m(self, value):
        self.c15m_live = value

    @property
    def closed_1m(self) -> list[Candle]:
        return self.c1m[:-1] if len(self.c1m) > 1 else []

    @property
    def closed_5m(self) -> list[Candle]:
        if self.c5m_live:
            return self.c5m_live  # all live-aggregated bars are already closed
        return self._c5m_yf[:-1] if len(self._c5m_yf) > 1 else []

    @property
    def closed_15m(self) -> list[Candle]:
        if self.c15m_live:
            return self.c15m_live
        return self._c15m_yf[:-1] if len(self._c15m_yf) > 1 else []

    @property
    def live_price(self) -> float:
        return self.c1m[-1].c if self.c1m else 0.0

    @property
    def daily_bars(self) -> list[Candle]:
        return self.c_daily

    @property
    def health(self) -> DataHealth:
        return self._health

    # ── Tick processing (synchronous — called from event loop) ────

    def process_tick(self, price: float, volume: float, ts: int):
        if not self.c1m:
            self.c1m.append(Candle(t=(ts // 60_000) * 60_000, o=price, h=price, l=price, c=price, v=volume))
            return

        current_min_ts = (ts // 60_000) * 60_000
        last = self.c1m[-1]
        last_min_ts = (last.t // 60_000) * 60_000

        if current_min_ts > last_min_ts:
            # New minute — close the previous bar
            new_candle = Candle(t=current_min_ts, o=price, h=price, l=price, c=price, v=volume)
            self.c1m.append(new_candle)
            if len(self.c1m) > 500:
                self.c1m = self.c1m[-500:]
            closed_candle = self.c1m[-2]
            if closed_candle.t != self._last_eval_ts:
                self._last_eval_ts = closed_candle.t
                try:
                    asyncio.create_task(self._fire_callbacks(self._on_close_callbacks))
                except RuntimeError:
                    pass
            # Check 5m/15m aggregation
            self._check_5m_aggregation()
            if self._pending_5m_fire:
                try:
                    asyncio.create_task(self._fire_callbacks(self._on_5m_callbacks))
                except RuntimeError:
                    pass
                self._pending_5m_fire = False
            self._check_15m_aggregation()
        else:
            # Same minute — update live candle
            last.c = price
            last.h = max(last.h, price)
            last.l = min(last.l, price)
            last.v += volume
            self.c1m[-1] = last

    # ── 5m / 15m aggregation from tick-built 1m bars ─────────────

    def _check_5m_aggregation(self):
        closed = self.closed_1m
        if len(closed) < 5:
            return
        last = closed[-1]
        dt = datetime.fromtimestamp(last.t / 1000, tz=ET)
        t_min = dt.hour * 60 + dt.minute
        # Market hours only (9:30 - 16:00)
        if not (570 <= t_min < 960):
            return
        # 5m boundary: the bar at :X4, :X9, :X4... closes a 5m window
        if (dt.minute + 1) % 5 != 0:
            return
        # Collect bars in this 5m window
        window_start_ts = last.t - (4 * 60_000)
        chunk = [c for c in closed if window_start_ts <= c.t <= last.t]
        if len(chunk) < 3:
            return
        bar_5m = Candle(
            t=chunk[0].t, o=chunk[0].o,
            h=max(c.h for c in chunk), l=min(c.l for c in chunk),
            c=chunk[-1].c, v=sum(c.v for c in chunk),
        )
        self.c5m_live.append(bar_5m)
        if len(self.c5m_live) > 2000:
            self.c5m_live = self.c5m_live[-2000:]
        self._pending_5m_fire = True

    def _check_15m_aggregation(self):
        closed = self.closed_1m
        if len(closed) < 15:
            return
        last = closed[-1]
        dt = datetime.fromtimestamp(last.t / 1000, tz=ET)
        t_min = dt.hour * 60 + dt.minute
        if not (570 <= t_min < 960):
            return
        if (dt.minute + 1) % 15 != 0:
            return
        window_start_ts = last.t - (14 * 60_000)
        chunk = [c for c in closed if window_start_ts <= c.t <= last.t]
        if len(chunk) < 5:
            return
        bar_15m = Candle(
            t=chunk[0].t, o=chunk[0].o,
            h=max(c.h for c in chunk), l=min(c.l for c in chunk),
            c=chunk[-1].c, v=sum(c.v for c in chunk),
        )
        self.c15m_live.append(bar_15m)
        if len(self.c15m_live) > 1000:
            self.c15m_live = self.c15m_live[-1000:]
        self._pending_15m_fire = True

    # ── Load from yfinance (startup / fallback) ──────────────────

    def load(self, c1m, c5m, c15m, c_daily):
        self.c1m = c1m
        self._c5m_yf = c5m
        self._c15m_yf = c15m
        self.c_daily = c_daily

    def load_1m(self, candles):
        if not candles:
            return
        live = self.c1m[-1] if self.c1m else None
        self.c1m = candles[:-1] + ([live] if live else [candles[-1]] if candles else [])

    def load_5m(self, candles):
        if not candles:
            return
        live = self._c5m_yf[-1] if self._c5m_yf else None
        self._c5m_yf = candles[:-1] + ([live] if live else [candles[-1]] if candles else [])
        if len(self._c5m_yf) > 2000:
            self._c5m_yf = self._c5m_yf[-2000:]

    def load_15m(self, candles):
        if not candles:
            return
        self._c15m_yf = candles[-1000:] if len(candles) > 1000 else candles

    def load_daily(self, candles):
        if not candles:
            return
        self.c_daily = candles[-252:] if len(candles) > 252 else candles

    # ── Backfill: merge yfinance bars into gaps (async, threaded) ─

    async def merge_backfill(self, yf_candles: list[Candle]):
        if not yf_candles:
            return
        result = await asyncio.to_thread(self._merge_backfill_sync, yf_candles)
        if result:
            self.c1m = result["c1m"]
            self.c5m_live = result["c5m"]
            self.c15m_live = result["c15m"]
            self._health.bars_backfilled += result["filled"]

    def _merge_backfill_sync(self, yf_candles: list[Candle]) -> dict | None:
        """CPU-heavy — runs in thread pool, not on event loop."""
        tick_ts = {c.t for c in self.c1m}
        new_bars = [yfc for yfc in yf_candles if yfc.t not in tick_ts and yfc.c > 0]
        if not new_bars:
            return None
        merged = sorted(self.c1m + new_bars, key=lambda c: c.t)
        if len(merged) > 500:
            merged = merged[-500:]
        c5m = _aggregate_bars_sync(merged, 5)
        c15m = _aggregate_bars_sync(merged, 15)
        return {"c1m": merged, "c5m": c5m, "c15m": c15m, "filled": len(new_bars)}

    # ── Validation: compare tick-built vs yfinance (async, threaded)

    async def validate_bars(self, yf_candles: list[Candle]) -> dict:
        return await asyncio.to_thread(self._validate_bars_sync, yf_candles)

    def _validate_bars_sync(self, yf_candles: list[Candle]) -> dict:
        """Compares tick-built bars to yfinance in the T-20 to T-15 min window."""
        if not yf_candles or not self.c1m:
            return {"status": "NO_DATA", "issues": [], "checked": 0, "problems": 0}

        now_ms = int(time.time() * 1000)
        window_end = now_ms - (YF_DELAY_MINUTES * 60_000)
        window_start = now_ms - ((YF_DELAY_MINUTES + YF_VALIDATE_WINDOW) * 60_000)

        yf_window = [c for c in yf_candles if window_start <= c.t <= window_end]
        tick_map = {c.t: c for c in self.c1m}

        issues = []
        for yfc in yf_window:
            tick_bar = tick_map.get(yfc.t)
            if not tick_bar:
                issues.append(f"MISSING bar at {yfc.t}")
                continue
            if yfc.c > 0:
                if abs(tick_bar.c - yfc.c) / yfc.c > PRICE_DIVERGENCE_THRESHOLD:
                    issues.append(f"CLOSE diverge: tick={tick_bar.c:.2f} yf={yfc.c:.2f}")
            if yfc.v > 0:
                if abs(tick_bar.v - yfc.v) / yfc.v > VOL_DIVERGENCE_THRESHOLD:
                    issues.append(f"VOL diverge: tick={tick_bar.v:.0f} yf={yfc.v:.0f}")

        self._health.last_validated_at = time.time()
        return {"issues": issues, "checked": len(yf_window), "problems": len(issues)}

    # ── Callbacks ─────────────────────────────────────────────────

    def on_close(self, callback: Callable):
        self._on_close_callbacks.append(callback)

    def on_5m_close(self, callback: Callable):
        self._on_5m_callbacks.append(callback)

    async def _fire_callbacks(self, callbacks):
        for cb in callbacks:
            try:
                await cb(self.asset)
            except Exception as e:
                print(f"[{self.asset}] Callback error: {e}")


# ── Module-level helper (used by both merge_backfill and heartbeat) ─

def _aggregate_bars_sync(bars_1m: list[Candle], interval_min: int) -> list[Candle]:
    """Group 1m bars into N-minute bars. Runs in thread or sync context."""
    groups: list[dict] = []
    for c in bars_1m:
        dt = datetime.fromtimestamp(c.t / 1000, tz=ET)
        bucket = c.t - (dt.minute % interval_min) * 60_000
        if groups and groups[-1]["t"] == bucket:
            groups[-1]["bars"].append(c)
        else:
            groups.append({"t": bucket, "bars": [c]})
    return [
        Candle(
            t=g["t"], o=g["bars"][0].o,
            h=max(b.h for b in g["bars"]),
            l=min(b.l for b in g["bars"]),
            c=g["bars"][-1].c,
            v=sum(b.v for b in g["bars"]),
        )
        for g in groups if g["bars"]
    ]


# ── MultiCandleStore: manages all assets + heartbeat ────────────

class MultiCandleStore:
    def __init__(self):
        from ..constants import ASSETS
        self._stores = {asset: AssetCandleStore(asset) for asset in ASSETS}
        self._running = False
        self._heartbeat_task = None

    def get(self, asset: str) -> AssetCandleStore:
        return self._stores[asset]

    def process_tick(self, asset: str, price: float, volume: float, ts_ms: int):
        if asset in self._stores:
            self._stores[asset].process_tick(price, volume, ts_ms)

    def on_1m_close(self, asset: str, callback):
        self._stores[asset].on_close(callback)

    def on_5m_close(self, asset: str, callback):
        self._stores[asset].on_5m_close(callback)

    def live_price(self, asset: str) -> float:
        return self._stores[asset].live_price

    # ── Heartbeat: force-close bars on time boundaries ────────────

    def start_heartbeat(self):
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat())

    def stop(self):
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

    async def _heartbeat(self):
        """1-second heartbeat — force-close bars and insert Doji if no ticks arrived."""
        while self._running:
            await asyncio.sleep(HEARTBEAT_SEC)
            now_ms = int(time.time() * 1000)
            current_min_ts = (now_ms // 60_000) * 60_000

            for asset, store in self._stores.items():
                if not store.c1m:
                    continue
                last_bar_ts = store.c1m[-1].t
                if current_min_ts <= last_bar_ts:
                    continue

                # Minute boundary crossed — force-close the current bar
                last = store.c1m[-1]
                # Insert Doji at last known price for the new minute
                doji = Candle(
                    t=current_min_ts, o=last.c, h=last.c,
                    l=last.c, c=last.c, v=0,
                )
                store.c1m.append(doji)
                if len(store.c1m) > 500:
                    store.c1m = store.c1m[-500:]

                # Fire 1m close callback for the now-closed bar
                closed = store.c1m[-2]
                if closed.t != store._last_eval_ts:
                    store._last_eval_ts = closed.t
                    await store._fire_callbacks(store._on_close_callbacks)

                # Check 5m/15m aggregation
                store._check_5m_aggregation()
                if store._pending_5m_fire:
                    await store._fire_callbacks(store._on_5m_callbacks)
                    store._pending_5m_fire = False
                store._check_15m_aggregation()

    # ── Delegate async methods ────────────────────────────────────

    async def merge_backfill(self, asset: str, candles: list[Candle]):
        if asset in self._stores:
            await self._stores[asset].merge_backfill(candles)

    async def validate_bars(self, asset: str, candles: list[Candle]) -> dict:
        if asset in self._stores:
            return await self._stores[asset].validate_bars(candles)
        return {"status": "UNKNOWN", "issues": [], "checked": 0, "problems": 0}
