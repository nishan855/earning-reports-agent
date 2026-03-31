"""Fake Finnhub WebSocket server.

Publishes historical tick data in the exact same JSON format as Finnhub,
so the real DataFeed connects to it and the entire pipeline is tested end-to-end:
  WS connect → subscribe → parse ticks → process_tick → 1m bars → 5m/15m aggregation
  → heartbeat → detection → agent → signals

Usage:
  # Replay all of Friday at 10x speed (~39 min for full 6.5hr day)
  python -m trading.sim.fake_finnhub --pace 10 --day 0

  # Replay Friday at real-time pace (6.5 hours)
  python -m trading.sim.fake_finnhub --realtime --day 0

  # Replay 1 hour of Friday fast (custom tick speed)
  python -m trading.sim.fake_finnhub --speed 0.01 --minutes 60 --day 0

  # Then start the trading engine with FINNHUB_SIM=1
  FINNHUB_SIM=1 uvicorn main:app --port 8000
"""

import asyncio
import json
import time
import random
import argparse
import pytz
import yfinance as yf
from datetime import datetime

ET = pytz.timezone("America/New_York")
ASSETS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "MSFT", "META", "AMZN", "JPM", "XLE", "GLD", "BA"]


def load_replay_data(day_offset: int = 0, minutes: int = 0) -> dict:
    """Load 1m bars from yfinance. If minutes=0, load the full day."""
    label = f"{minutes} minutes" if minutes else "full day"
    print(f"[FakeFinnhub] Loading {label} of data (day_offset={day_offset})...")
    data = {}
    for asset in ASSETS:
        try:
            ticker = yf.Ticker(asset)
            df = ticker.history(period="5d", interval="1m")
            bars = []
            for ts, row in df.iterrows():
                try:
                    o, h, l, c, v = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"]), float(row["Volume"])
                    if c > 0 and h >= l:
                        bars.append({"t": int(ts.timestamp() * 1000), "o": round(o, 2), "h": round(h, 2),
                                     "l": round(l, 2), "c": round(c, 2), "v": v})
                except Exception:
                    pass

            # Filter market hours (9:30-16:00)
            mkt = [b for b in bars
                   if 570 <= datetime.fromtimestamp(b["t"] / 1000, tz=ET).hour * 60 +
                              datetime.fromtimestamp(b["t"] / 1000, tz=ET).minute < 960]

            # Group by day
            days = {}
            for b in mkt:
                d = datetime.fromtimestamp(b["t"] / 1000, tz=ET).strftime("%Y-%m-%d")
                days.setdefault(d, []).append(b)

            sorted_days = sorted(days.keys(), reverse=True)
            pick = min(day_offset, len(sorted_days) - 1)
            if sorted_days:
                day = sorted_days[pick]
                all_bars = days[day]
                data[asset] = all_bars[:minutes] if minutes else all_bars
                print(f"  {asset}: {len(data[asset])} bars from {day}")
            else:
                data[asset] = []
        except Exception as e:
            print(f"  {asset}: error — {e}")
            data[asset] = []
    return data


def bars_to_ticks(bar: dict, asset: str) -> list[dict]:
    """Generate ~10 synthetic ticks from a 1m bar in Finnhub trade format."""
    o, h, l, c, v = bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]
    base_ts = bar["t"]
    vol = max(v / 10, 1)
    ticks = []

    # Tick 1: open
    ticks.append({"s": asset, "p": o, "v": vol, "t": base_ts})

    # Ticks 2-4: random walk between extremes
    for i in range(3):
        price = round(random.uniform(l, h), 2)
        ticks.append({"s": asset, "p": price, "v": vol, "t": base_ts + (i + 1) * 5000})

    # Tick 5: low
    ticks.append({"s": asset, "p": l, "v": vol * 1.5, "t": base_ts + 25000})

    # Tick 6: high
    ticks.append({"s": asset, "p": h, "v": vol * 1.5, "t": base_ts + 30000})

    # Ticks 7-9: drift toward close
    for i in range(3):
        frac = (i + 1) / 4
        price = h * (1 - frac) + c * frac if c >= o else l * (1 - frac) + c * frac
        noise = random.uniform(-0.01, 0.01) * abs(h - l)
        price = round(max(l, min(h, price + noise)), 2)
        ticks.append({"s": asset, "p": price, "v": vol, "t": base_ts + 35000 + i * 5000})

    # Tick 10: close
    ticks.append({"s": asset, "p": c, "v": vol, "t": base_ts + 55000})

    return ticks


async def run_server(port: int = 8765, speed: float = 0.0, pace: float = 0.0,
                     realtime: bool = False, day_offset: int = 0, minutes: int = 0):
    """Run fake Finnhub WebSocket server.

    Timing modes (pick one):
      --realtime     : 1 minute of bars = 60 seconds wall clock (full day = 6.5 hours)
      --pace N       : 1 minute of bars = 60/N seconds (--pace 10 = full day in ~39 min)
      --speed S      : S seconds between each individual tick (--speed 0.01 = fast blast)
    """
    import websockets

    replay_data = load_replay_data(day_offset, minutes)
    max_bars = max(len(bars) for bars in replay_data.values()) if replay_data else 0
    ticks_per_bar = 10 * len(ASSETS)  # ~80 ticks per minute
    total_ticks = max_bars * ticks_per_bar

    # Calculate timing
    if realtime:
        pace = 1.0
    if pace > 0:
        # Pace mode: spread ticks evenly across the scaled minute
        sec_per_minute = 60.0 / pace
        tick_delay = sec_per_minute / ticks_per_bar
        est_sec = int(max_bars * sec_per_minute)
        mode_label = f"{'REALTIME' if realtime else f'{pace}x pace'} ({sec_per_minute:.1f}s per bar)"
    elif speed > 0:
        tick_delay = speed
        est_sec = int(total_ticks * speed)
        mode_label = f"fixed {speed}s per tick"
    else:
        tick_delay = 0
        est_sec = 0
        mode_label = "max speed (no delay)"

    est_min = est_sec / 60

    # Find the date being replayed
    spy_bars = replay_data.get("SPY", [])
    replay_date = ""
    first_time = ""
    last_time = ""
    if spy_bars:
        replay_date = datetime.fromtimestamp(spy_bars[0]["t"] / 1000, tz=ET).strftime("%A %Y-%m-%d")
        first_time = datetime.fromtimestamp(spy_bars[0]["t"] / 1000, tz=ET).strftime("%H:%M")
        last_time = datetime.fromtimestamp(spy_bars[-1]["t"] / 1000, tz=ET).strftime("%H:%M")

    print(f"\n{'='*60}")
    print(f"  FAKE FINNHUB SERVER")
    print(f"  Replaying: {replay_date} ({first_time} - {last_time} ET)")
    print(f"  Bars: {max_bars} | Ticks: ~{total_ticks}")
    print(f"  Mode: {mode_label}")
    print(f"  Est runtime: {est_min:.1f} min ({est_sec}s)")
    print(f"  Listening: ws://localhost:{port}")
    print(f"{'='*60}\n")

    clients = set()
    subscribed = {}
    started = asyncio.Event()

    async def handler(ws):
        clients.add(ws)
        subscribed[ws] = set()
        print(f"[FakeFinnhub] Client connected ({len(clients)} total)")
        try:
            async for message in ws:
                try:
                    msg = json.loads(message)
                    if msg.get("type") == "subscribe":
                        sym = msg.get("symbol", "")
                        subscribed[ws].add(sym)
                        # Start once at least one client subscribes all assets
                        if len(subscribed[ws]) >= len(ASSETS):
                            started.set()
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            clients.discard(ws)
            subscribed.pop(ws, None)
            print(f"[FakeFinnhub] Client disconnected ({len(clients)} remaining)")

    async def publisher():
        await started.wait()
        print(f"[FakeFinnhub] Subscriptions complete — starting tick stream\n")
        await asyncio.sleep(0.5)

        tick_count = 0
        sim_start = time.time()

        for i in range(max_bars):
            bar_start = time.time()

            # Generate ticks for this minute
            minute_ticks = []
            for asset in ASSETS:
                bars = replay_data.get(asset, [])
                if i >= len(bars):
                    continue
                minute_ticks.extend(bars_to_ticks(bars[i], asset))

            # Shuffle for realistic interleaving
            random.shuffle(minute_ticks)

            # Publish ticks
            for tick in minute_ticks:
                msg = json.dumps({"type": "trade", "data": [tick]})
                dead = set()
                for ws in clients:
                    if tick["s"] in subscribed.get(ws, set()):
                        try:
                            await ws.send(msg)
                        except Exception:
                            dead.add(ws)
                clients.difference_update(dead)
                tick_count += 1

                if tick_delay > 0:
                    await asyncio.sleep(tick_delay)

            # Progress log
            bar_time = ""
            if i < len(spy_bars):
                bar_time = datetime.fromtimestamp(spy_bars[i]["t"] / 1000, tz=ET).strftime("%H:%M")
            elapsed = time.time() - sim_start
            if i % 5 == 0:
                print(f"  {bar_time} | bar {i+1}/{max_bars} | {tick_count} ticks | elapsed {elapsed:.0f}s")

        elapsed = time.time() - sim_start
        print(f"\n{'='*60}")
        print(f"  SIMULATION COMPLETE")
        print(f"  {tick_count} ticks over {max_bars} bars in {elapsed:.1f}s")
        print(f"  Server staying alive — Ctrl+C to stop")
        print(f"{'='*60}\n")

    async with websockets.serve(handler, "localhost", port, ping_interval=30, ping_timeout=10):
        await asyncio.gather(
            publisher(),
            asyncio.Future(),
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fake Finnhub WebSocket server for testing")
    parser.add_argument("--port", type=int, default=8765, help="WebSocket port (default 8765)")
    parser.add_argument("--speed", type=float, default=0.0, help="Fixed seconds between each tick")
    parser.add_argument("--pace", type=float, default=0.0, help="Speed multiplier (10 = 10x real-time)")
    parser.add_argument("--realtime", action="store_true", help="1:1 real-time pace (6.5hr full day)")
    parser.add_argument("--day", type=int, default=0, help="Day offset (0=most recent, 1=day before)")
    parser.add_argument("--minutes", type=int, default=0, help="Minutes to replay (0=full day)")
    args = parser.parse_args()

    if not args.speed and not args.pace and not args.realtime:
        args.pace = 10  # default: 10x speed

    asyncio.run(run_server(
        port=args.port, speed=args.speed, pace=args.pace,
        realtime=args.realtime, day_offset=args.day, minutes=args.minutes,
    ))
