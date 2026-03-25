import asyncio
import aiohttp
import json
import time
from .models import Candle


class FinnhubClient:
    BASE = "https://finnhub.io/api/v1"
    WS   = "wss://ws.finnhub.io"
    MAX_CALLS_PER_MIN = 55

    def __init__(self, api_key: str):
        self._key         = api_key
        self._call_times: list[float] = []
        self._ws_task     = None

    async def _rate_limited_get(self, url: str) -> dict:
        now = time.time()
        self._call_times = [t for t in self._call_times if now - t < 60]
        if len(self._call_times) >= self.MAX_CALLS_PER_MIN:
            wait = 60 - (now - self._call_times[0]) + 0.1
            await asyncio.sleep(wait)
        self._call_times.append(time.time())

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 429:
                    await asyncio.sleep(10)
                    async with session.get(url) as retry:
                        return await retry.json()
                return await resp.json()

    async def fetch_bars(
        self,
        symbol: str,
        resolution: str | int,
        from_ts: int,
        to_ts:   int,
    ) -> list[Candle]:
        url = f"{self.BASE}/stock/candle?symbol={symbol}&resolution={resolution}&from={from_ts}&to={to_ts}&token={self._key}"
        try:
            data = await self._rate_limited_get(url)
            if data.get("s") != "ok" or not data.get("t"):
                return []
            return [
                Candle(t=data["t"][i] * 1000, o=data["o"][i], h=data["h"][i],
                       l=data["l"][i], c=data["c"][i], v=data["v"][i])
                for i in range(len(data["t"]))
            ]
        except Exception as e:
            print(f"fetchBars error ({resolution}): {e}")
            return []

    async def fetch_vix(self) -> float | None:
        try:
            data = await self._rate_limited_get(f"{self.BASE}/quote?symbol=VIX&token={self._key}")
            return data.get("c")
        except:
            return None

    async def connect_ws(self, symbol: str, on_trade):
        import websockets
        while True:
            try:
                async with websockets.connect(f"{self.WS}?token={self._key}") as ws:
                    await ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
                    print(f"Finnhub WS connected: {symbol}")
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") != "trade":
                            continue
                        for trade in msg.get("data", []):
                            await on_trade(trade["p"], trade["v"], trade["t"])
            except Exception as e:
                print(f"Finnhub WS error: {e} — reconnecting in 5s")
                await asyncio.sleep(5)
