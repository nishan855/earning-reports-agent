import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
import yfinance as yf
import aiohttp
from .models import Candle


_yf_pool = ThreadPoolExecutor(max_workers=2)


def _yf_fetch_bars(symbol: str, period: str, interval: str) -> list[Candle]:
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if df.empty:
            return []
        if hasattr(df.columns, 'droplevel'):
            df.columns = df.columns.droplevel(1)
        candles = []
        for idx, row in df.iterrows():
            ts = int(idx.timestamp() * 1000)
            candles.append(Candle(
                t=ts, o=float(row["Open"]), h=float(row["High"]),
                l=float(row["Low"]), c=float(row["Close"]),
                v=float(row.get("Volume", 0)),
            ))
        return candles
    except Exception as e:
        print(f"yfinance error ({interval}): {e}")
        return []


class FinnhubClient:
    BASE = "https://finnhub.io/api/v1"
    WS   = "wss://ws.finnhub.io"

    def __init__(self, api_key: str):
        self._key = api_key

    async def fetch_bars(self, symbol: str, resolution: str) -> list[Candle]:
        period_map = {
            "1":  ("5d",  "1m"),
            "5":  ("10d", "5m"),
            "15": ("30d", "15m"),
            "D":  ("6mo", "1d"),
        }
        period, interval = period_map.get(str(resolution), ("5d", "1m"))
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_yf_pool, _yf_fetch_bars, symbol, period, interval)

    async def fetch_vix(self) -> float | None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.BASE}/quote?symbol=VIX&token={self._key}") as resp:
                    data = await resp.json()
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
