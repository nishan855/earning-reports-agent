import asyncio
import json
import time
import pytz
import yfinance as yf
from ..constants import ASSETS, YFINANCE_STAGGER_SEC, VALIDATE_INTERVAL_SEC, YF_MAX_CONCURRENT
from ..models import Candle

ET = pytz.timezone("America/New_York")


class DataFeed:
    def __init__(self, finnhub_key: str, on_tick, on_bars, on_vix, on_reconnect=None):
        self._key = finnhub_key
        self._on_tick = on_tick
        self._on_bars = on_bars
        self._on_vix = on_vix
        self._on_reconnect = on_reconnect
        self._running = False
        self._startup_done = False
        import os
        self._sim_mode = os.environ.get("FINNHUB_SIM", "").strip() == "1"
        # WS disconnect tracking
        self._ws_disconnect_at: float = 0.0
        self._ws_disconnects: int = 0
        # Semaphore to cap concurrent yfinance calls
        self._yf_semaphore = asyncio.Semaphore(YF_MAX_CONCURRENT)

    async def start(self):
        self._running = True
        await asyncio.gather(
            self._run_websocket(),
            self._run_bar_poller(),
            self._run_vix_poller(),
        )

    async def stop(self):
        self._running = False

    # ── WebSocket: real-time ticks with reconnect tracking ────────

    async def _run_websocket(self):
        import os
        import websockets
        from datetime import datetime

        # Connect to fake Finnhub server in simulation mode
        sim_mode = os.environ.get("FINNHUB_SIM", "").strip() == "1"
        sim_port = os.environ.get("FINNHUB_SIM_PORT", "8765")
        if sim_mode:
            url = f"ws://localhost:{sim_port}"
            print(f"[DataFeed] SIM MODE — connecting to {url}")
        else:
            url = f"wss://ws.finnhub.io?token={self._key}"

        backoff = 5
        while self._running:
            # Don't connect outside extended market hours (skip in sim mode)
            if not sim_mode:
                now_et = datetime.now(ET)
                t_min = now_et.hour * 60 + now_et.minute
                if t_min < 240 or t_min >= 1215:
                    # Outside extended hours — sleep until 4:00 AM ET
                    if t_min >= 1215:
                        wake_min = (24 * 60 - t_min) + 240
                    else:
                        wake_min = 240 - t_min
                    wake_sec = max(wake_min * 60, 60)
                    print(f"[DataFeed] Outside market hours — sleeping {wake_min}m until 4:00 AM ET")
                    self._ws_disconnect_at = 0.0
                    await asyncio.sleep(wake_sec)
                    continue

            try:
                async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
                    # On reconnect during market hours: fire callback with gap window
                    if self._ws_disconnect_at > 0 and self._on_reconnect:
                        gap_start = self._ws_disconnect_at
                        gap_end = time.time()
                        self._ws_disconnect_at = 0.0
                        try:
                            await self._on_reconnect(gap_start, gap_end)
                        except Exception as e:
                            print(f"[DataFeed] Reconnect handler error: {e}")

                    print(f"[DataFeed] WS connected — subscribing {len(ASSETS)} assets")
                    for asset in ASSETS:
                        await ws.send(json.dumps({"type": "subscribe", "symbol": asset}))
                    backoff = 5
                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(message)
                            if data.get("type") == "trade":
                                for trade in data.get("data", []):
                                    symbol = trade.get("s", "")
                                    if symbol in ASSETS:
                                        await self._on_tick(symbol, float(trade["p"]), float(trade["v"]), int(trade["t"]))
                        except Exception as e:
                            print(f"[DataFeed] Parse error: {e}")
            except Exception as e:
                # In sim mode, always track disconnects; in live mode, only during market hours
                if sim_mode:
                    if self._ws_disconnect_at == 0.0:
                        self._ws_disconnect_at = time.time()
                        self._ws_disconnects += 1
                    print(f"[DataFeed] WS error: {e} — reconnecting in {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 120)
                else:
                    now_et = datetime.now(ET)
                    t_min = now_et.hour * 60 + now_et.minute
                    if 240 <= t_min < 1215:
                        if self._ws_disconnect_at == 0.0:
                            self._ws_disconnect_at = time.time()
                            self._ws_disconnects += 1
                        print(f"[DataFeed] WS error: {e} — reconnecting in {backoff}s")
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 120)
                    else:
                        continue

    # ── Bar poller: startup full fetch, then validate-only ────────

    async def _run_bar_poller(self):
        # Phase 1: Startup — fetch all timeframes for all assets (one time)
        await self._startup_fetch()
        self._startup_done = True
        print(f"[DataFeed] Startup fetch complete — switching to validation mode ({VALIDATE_INTERVAL_SEC}s interval)")

        # Phase 2: Ongoing — fetch ONLY 1m bars for validation every 5 min
        loop = asyncio.get_event_loop()
        while self._running:
            await asyncio.sleep(VALIDATE_INTERVAL_SEC)
            # Skip validation outside market hours (unless sim mode)
            if not self._sim_mode:
                from datetime import datetime
                now_et = datetime.now(ET)
                t_min = now_et.hour * 60 + now_et.minute
                if t_min < 570 or t_min >= 960:
                    continue
            for asset in ASSETS:
                if not self._running:
                    break
                try:
                    async with self._yf_semaphore:
                        bars = await loop.run_in_executor(None, self._fetch_1m_sync, asset)
                    if bars:
                        await self._on_bars(asset, "1m", bars)
                except Exception as e:
                    print(f"[DataFeed] Validation fetch {asset}: {e}")
                await asyncio.sleep(YFINANCE_STAGGER_SEC)

    async def _startup_fetch(self):
        """Fetch all 4 timeframes for all assets at startup."""
        await asyncio.sleep(3)
        loop = asyncio.get_event_loop()
        for asset in ASSETS:
            if not self._running:
                break
            try:
                async with self._yf_semaphore:
                    bars = await loop.run_in_executor(None, self._fetch_bars_sync, asset)
                if bars:
                    for tf, candles in bars.items():
                        if candles:
                            await self._on_bars(asset, tf, candles)
            except Exception as e:
                print(f"[DataFeed] Startup fetch error {asset}: {e}")
            await asyncio.sleep(YFINANCE_STAGGER_SEC)

    # ── Backfill: fetch 1m bars for a gap window after disconnect ─

    async def backfill_gap(self, asset: str, gap_start: float, gap_end: float):
        """Fetch 1m bars to fill a WS disconnect gap.
        Note: yfinance has ~15min delay, so recent gaps may return empty.
        The heartbeat Doji bars keep the time-series unbroken in that case."""
        loop = asyncio.get_event_loop()
        try:
            async with self._yf_semaphore:
                bars = await loop.run_in_executor(None, self._fetch_1m_sync, asset)
            if not bars:
                return
            # Filter to gap window
            gap_start_ms = int(gap_start * 1000)
            gap_end_ms = int(gap_end * 1000)
            gap_bars = [c for c in bars if gap_start_ms <= c.t <= gap_end_ms]
            if gap_bars:
                await self._on_bars(asset, "1m_backfill", gap_bars)
            # Empty gap_bars is expected for short/recent disconnects — not an error
        except Exception as e:
            print(f"[DataFeed] Backfill {asset}: {e}")

    # ── Sync fetch methods (run in thread pool) ───────────────────

    def _fetch_bars_sync(self, asset: str) -> dict:
        """Fetch all 4 timeframes — used at startup only."""
        ticker = yf.Ticker(asset)
        result = {}
        periods = {"1m": ("1d", "1m"), "5m": ("5d", "5m"), "15m": ("30d", "15m"), "1d": ("2y", "1d")}
        for tf, (period, interval) in periods.items():
            try:
                df = ticker.history(period=period, interval=interval, prepost=True)
                if df.empty:
                    result[tf] = []
                    continue
                candles = []
                for ts, row in df.iterrows():
                    try:
                        c = Candle(t=int(ts.timestamp() * 1000), o=float(row["Open"]), h=float(row["High"]),
                                   l=float(row["Low"]), c=float(row["Close"]), v=float(row["Volume"]))
                        if c.c > 0:
                            candles.append(c)
                    except Exception:
                        pass
                result[tf] = candles
            except Exception as e:
                print(f"[DataFeed] {asset} {tf} error: {e}")
                result[tf] = []
        return result

    def _fetch_1m_sync(self, asset: str) -> list[Candle]:
        """Fetch only 1m bars — used for validation and backfill."""
        try:
            ticker = yf.Ticker(asset)
            df = ticker.history(period="1d", interval="1m", prepost=True)
            if df.empty:
                return []
            candles = []
            for ts, row in df.iterrows():
                try:
                    c = Candle(t=int(ts.timestamp() * 1000), o=float(row["Open"]), h=float(row["High"]),
                               l=float(row["Low"]), c=float(row["Close"]), v=float(row["Volume"]))
                    if c.c > 0:
                        candles.append(c)
                except Exception:
                    pass
            return candles
        except Exception as e:
            print(f"[DataFeed] {asset} 1m fetch error: {e}")
            return []

    # ── VIX poller (60s interval) ─────────────────────────────────

    async def _run_vix_poller(self):
        loop = asyncio.get_event_loop()
        while self._running:
            # Only poll VIX during market hours (unless sim mode)
            from datetime import datetime
            now_et = datetime.now(ET)
            t_min = now_et.hour * 60 + now_et.minute
            if self._sim_mode or (570 <= t_min < 960):
                try:
                    vix = await loop.run_in_executor(None, self._fetch_vix_sync)
                    if vix and 5 < vix < 100:
                        await self._on_vix(vix)
                except Exception as e:
                    print(f"[DataFeed] VIX error: {e}")
            await asyncio.sleep(60)

    def _fetch_vix_sync(self) -> float | None:
        try:
            ticker = yf.Ticker("^VIX")
            hist = ticker.history(period="1d", interval="1m")
            if hist.empty:
                return None
            val = float(hist["Close"].iloc[-1])
            return val if val < 100 else None
        except Exception:
            return None
