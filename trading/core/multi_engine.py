import asyncio
import time
import pytz
from datetime import datetime

from ..constants import ASSETS, BACKFILL_MAX_GAP_SEC, HEALTH_STALE_SEC, HEALTH_DEGRADED_BARS
from ..models import Candle, Level, DayContext, DataHealth
from ..context.sim_clock import now_et, set_sim_time
from ..core.gates import GateSystem
from ..data.candle_store import MultiCandleStore
from ..data.cvd_engine import MultiCVDEngine
from ..data.data_feed import DataFeed
from ..levels.builder import build_levels, calc_vwap, filter_today_bars
from ..levels.volume_profile import compute_volume_profile
from ..levels.zones import detect_zones
from ..detection.level_state import TrackerEngine
from ..detection.breakout import detect_breakout, is_back_through
from ..detection.rejection import detect_rejection
from ..detection.stop_hunt import detect_stop_hunt, confirm_stop_hunt
from ..detection.failed_breakout import detect_failed_retest, confirm_failed_breakout, get_reverse_direction
from ..context.session import get_current_session, is_trading_hours, is_or_complete, minutes_to_cutoff
from ..context.day_context import assess_day_context
from ..agent.tools import ToolHandler
from ..agent.brief import build_brief

ET = pytz.timezone("America/New_York")


class MultiEngine:
    def __init__(self, finnhub_key: str, openai_key: str, on_signal=None, on_tick=None, on_state=None, on_tool_call=None, on_token=None):
        import os
        self._openai_key = openai_key
        self._sim_mode: bool = os.environ.get("FINNHUB_SIM", "").strip() == "1"
        self._candles = MultiCandleStore()
        self._cvd = MultiCVDEngine()
        self._feed = DataFeed(finnhub_key, self._handle_tick, self._handle_bars, self._handle_vix,
                              on_reconnect=self._handle_reconnect)

        self._levels: dict = {a: [] for a in ASSETS}
        self._vol_profiles: dict = {}
        self._day_contexts: dict = {}
        self._zones: dict = {a: [] for a in ASSETS}
        self._signal_history: list = []
        self._vix: float = 20.0
        self._or_high: dict = {a: 0.0 for a in ASSETS}
        self._or_low: dict = {a: 0.0 for a in ASSETS}
        self._or_locked: dict = {a: False for a in ASSETS}
        self._investigating: dict = {a: False for a in ASSETS}
        self._pending_stop_hunts: dict = {}
        self._locked_stop_hunt_levels: dict = {a: set() for a in ASSETS}
        self._pending_rejections: dict = {}
        self._locked_rejection_levels: dict = {a: set() for a in ASSETS}
        self._last_tick_time: dict = {a: 0.0 for a in ASSETS}
        self._tracker = TrackerEngine()
        self._gates = GateSystem()
        if self._sim_mode:
            self._gates.sim_mode = True
        self._on_signal = on_signal
        self._on_tick = on_tick
        self._on_state = on_state
        self._on_tool_call = on_tool_call
        self._on_token = on_token
        # Data health per asset
        self._health: dict[str, DataHealth] = {a: DataHealth(asset=a) for a in ASSETS}
        self._ws_disconnect_count: int = 0
        # Register 1m/5m close callbacks on candle store
        for asset in ASSETS:
            self._candles.on_1m_close(asset, self.on_1m_close)
            self._candles.on_5m_close(asset, self.on_5m_close)

    async def start(self):
        self._log("MultiEngine starting — 8 assets")
        if not self._sim_mode:
            self._candles.start_heartbeat()
        await asyncio.gather(
            self._feed.start(),
            self._broadcast_state_loop(),
        )

    async def start_simulation(self):
        """Start in simulation mode — replay historical data, skip live feed."""
        self._log("MultiEngine starting — SIMULATION MODE")
        self._sim_mode = True
        await self._broadcast_state_loop()

    async def run_simulation(self, speed: float = 0.05, day_offset: int = 0):
        """Replay historical 1m data through the full pipeline at accelerated speed.
        speed = seconds between each simulated 1m candle.
        day_offset = 0 for most recent day, 1 for day before, etc."""
        import yfinance as yf
        self._sim_mode = True
        self._gates.sim_mode = True
        self._log(f"SIMULATION: Loading data (speed={speed}s/candle, day_offset={day_offset})")

        # Load real bars + daily for all assets
        for asset in ASSETS:
            try:
                ticker = yf.Ticker(asset)
                df1m = ticker.history(period="5d", interval="1m")
                df5m = ticker.history(period="5d", interval="5m")
                df15m = ticker.history(period="30d", interval="15m")
                dfd = ticker.history(period="2y", interval="1d")

                def to_candles(df):
                    out = []
                    for ts, row in df.iterrows():
                        try:
                            c = Candle(t=int(ts.timestamp()*1000), o=round(float(row["Open"]),2),
                                       h=round(float(row["High"]),2), l=round(float(row["Low"]),2),
                                       c=round(float(row["Close"]),2), v=float(row["Volume"]))
                            if c.c > 0 and c.h >= c.l:
                                out.append(c)
                        except Exception:
                            pass
                    return out

                daily = to_candles(dfd)
                c5m = to_candles(df5m)
                c15m = to_candles(df15m)

                store = self._candles.get(asset)
                store.load_daily(daily)
                store.load_5m(c5m)
                store.load_15m(c15m)
                await self._handle_bars(asset, "1d", daily)

                # Get all market-hours 1m bars, grouped by day
                all_1m = to_candles(df1m)
                mkt_1m = [c for c in all_1m
                          if 570 <= datetime.fromtimestamp(c.t/1000, tz=ET).hour * 60 +
                                   datetime.fromtimestamp(c.t/1000, tz=ET).minute < 960]
                # Group by day and pick the requested offset
                days_map = {}
                for c in mkt_1m:
                    d = datetime.fromtimestamp(c.t/1000, tz=ET).strftime("%Y-%m-%d")
                    days_map.setdefault(d, []).append(c)
                sorted_days = sorted(days_map.keys(), reverse=True)
                pick_idx = min(day_offset, len(sorted_days) - 1)
                if sorted_days:
                    target_day = sorted_days[pick_idx]
                    day_bars = days_map[target_day]
                    store._sim_replay = day_bars
                    self._log(f"{asset}: {len(day_bars)} bars for replay ({target_day}), {len(daily)} daily")
                else:
                    store._sim_replay = []
            except Exception as e:
                self._log(f"{asset} sim load error: {e}", "error")
            await asyncio.sleep(0.3)

        # Set VIX — use 20.0 for simulation (normal conditions)
        # Real VIX on weekends/after hours is stale and misleading
        self._vix = 20.0
        self._log(f"VIX: {self._vix:.1f} (simulated normal)")

        # Replay each candle through the engine
        self._log("SIMULATION: Starting replay...")
        max_bars = max(len(self._candles.get(a)._sim_replay) for a in ASSETS if hasattr(self._candles.get(a), '_sim_replay'))

        for i in range(max_bars):
            for asset in ASSETS:
                store = self._candles.get(asset)
                replay = getattr(store, '_sim_replay', [])
                if i >= len(replay):
                    continue
                bar = replay[i]

                # Simulate ticks from this candle: open, high/low, close
                ts = bar.t
                vol_chunk = bar.v / 4
                self._last_tick_time[asset] = time.time()

                # Tick 1: open
                self._cvd.process_trade(asset, bar.o, vol_chunk)
                # Tick 2: high or low (depending on bullish/bearish)
                if bar.c >= bar.o:
                    self._cvd.process_trade(asset, bar.l, vol_chunk)
                    self._cvd.process_trade(asset, bar.h, vol_chunk)
                else:
                    self._cvd.process_trade(asset, bar.h, vol_chunk)
                    self._cvd.process_trade(asset, bar.l, vol_chunk)
                # Tick 4: close
                self._cvd.process_trade(asset, bar.c, vol_chunk)

                # Load bars up to this point
                bars_so_far = replay[:i+1]
                store.c1m = bars_so_far + [Candle(t=bar.t+60000, o=bar.c, h=bar.c, l=bar.c, c=bar.c, v=0)]

                # Rebuild ALL 5m bars from 1m data (not incremental)
                sim_5m = []
                for k in range(0, len(bars_so_far) - 4, 5):
                    chunk = bars_so_far[k:k+5]
                    sim_5m.append(Candle(t=chunk[0].t, o=chunk[0].o, h=max(c.h for c in chunk),
                                         l=min(c.l for c in chunk), c=chunk[-1].c, v=sum(c.v for c in chunk)))
                # Add live candle for partial 5m
                sim_5m.append(Candle(t=bar.t, o=bar.c, h=bar.c, l=bar.c, c=bar.c, v=0))
                store.c5m = sim_5m

                # Broadcast tick
                if self._on_tick:
                    await self._on_tick({"type": "tick", "asset": asset, "price": bar.c, "cvd": self._cvd.value(asset)})

            # Run detection on each 1m close
            for asset in ASSETS:
                store = self._candles.get(asset)
                replay = getattr(store, '_sim_replay', [])
                if i >= len(replay):
                    continue
                bar = replay[i]
                et = datetime.fromtimestamp(bar.t / 1000, tz=ET)
                t_min = et.hour * 60 + et.minute

                # OR tracking
                if 570 <= t_min < 600:
                    or_bars = [c for c in replay[:i+1] if 570 <= datetime.fromtimestamp(c.t/1000, tz=ET).hour * 60 + datetime.fromtimestamp(c.t/1000, tz=ET).minute < 600]
                    if or_bars:
                        self._or_high[asset] = max(c.h for c in or_bars)
                        self._or_low[asset] = min(c.l for c in or_bars)
                elif t_min >= 600 and not self._or_locked[asset]:
                    self._or_locked[asset] = True
                    self._log(f"{asset} OR locked H:${self._or_high[asset]:.2f} L:${self._or_low[asset]:.2f}")
                    self._assess_day(asset)

                # Update levels
                await self._update_levels(asset)

                # Run detection after OR
                if t_min >= 600 and self._or_locked[asset]:
                    bars_1m = store.closed_1m
                    if len(bars_1m) >= 2:
                        last = bars_1m[-1]
                        prev = bars_1m[-2]
                        cvd_eng = self._cvd.get(asset)
                        avg_vol = self._avg_vol(asset, "1m")
                        atr = self._calc_atr(asset)
                        dc = self._day_contexts.get(asset)
                        bias = dc.bias if dc else "NEUTRAL"

                        # 5m detection (breakout + rejection) — runs every 5 bars
                        bars_so_far = replay[:i+1]
                        is_5m_close = len(bars_so_far) >= 5 and len(bars_so_far) % 5 == 0
                        if is_5m_close and len(store.closed_5m) >= 2:
                            c5_last = store.closed_5m[-1]
                            c5_prev = store.closed_5m[-2]
                            avg_5m = self._avg_vol(asset, "5m")
                            cvd_val = cvd_eng.value

                            for level in self._levels[asset]:
                                if level.score < 6 or self._investigating[asset]:
                                    continue
                                try:
                                    is_bo, bo_dir = detect_breakout(c5_last, c5_prev, level, avg_5m, 0, cvd_val, atr)
                                    if is_bo:
                                        vol_ratio = c5_last.v / avg_5m if avg_5m > 0 else 1.0
                                        passed, _ = self._gates.check_all(asset, level.score, vol_ratio, self._vix, bo_dir, bias)
                                        if passed:
                                            self._log(f"SIM DETECT: {asset} BREAKOUT {bo_dir} at {level.name} ${level.price:.2f}")
                                            await self._fire_agent(asset, "BREAKOUT_RETEST", bo_dir, level, c5_last, vol_ratio, cvd_val)
                                            break
                                except Exception:
                                    pass
                                try:
                                    is_rej, rej_dir, _ = detect_rejection(c5_last, level, avg_5m, 0, cvd_val, atr)
                                    if is_rej and level.score >= 8:
                                        vol_ratio = c5_last.v / avg_5m if avg_5m > 0 else 1.0
                                        passed, _ = self._gates.check_all(asset, level.score, vol_ratio, self._vix, rej_dir, bias)
                                        if passed:
                                            self._log(f"SIM DETECT: {asset} REJECTION {rej_dir} at {level.name} ${level.price:.2f}")
                                            await self._fire_agent(asset, "REJECTION", rej_dir, level, c5_last, vol_ratio, cvd_val)
                                            break
                                except Exception:
                                    pass

                        # 1m detection (stop hunt)
                        for level in self._levels[asset]:
                            if level.score < 6 or self._investigating[asset]:
                                continue
                            try:
                                cvd_change = cvd_eng.value - (cvd_eng.value_1min_ago if cvd_eng._history else 0)
                                is_hunt, hunt_dir = detect_stop_hunt(last, level, avg_vol, cvd_change, atr)
                                if is_hunt:
                                    vol_ratio = last.v / avg_vol if avg_vol > 0 else 1.0
                                    passed, _ = self._gates.check_all(asset, level.score, vol_ratio, self._vix, hunt_dir, bias)
                                    if passed:
                                        self._log(f"SIM DETECT: {asset} STOP_HUNT {hunt_dir} at {level.name} ${level.price:.2f}")
                                        await self._fire_agent(asset, "STOP_HUNT", hunt_dir, level, last, vol_ratio, cvd_change)
                                        break
                            except Exception:
                                pass

            # Broadcast state every 10 bars
            if i % 10 == 0 and self._on_state:
                assets_state = [self.get_asset_state(a) for a in ASSETS]
                await self._on_state({"type": "state", "vix": self._vix, "session": f"SIM bar {i+1}/{max_bars}", "assets": assets_state})
                # Send detail for active assets
                for asset in ASSETS:
                    try:
                        detail = self.get_asset_state(asset, include_bars=True)
                        await self._on_state({"type": "asset_detail", "asset": asset, "data": detail})
                    except Exception:
                        pass

            # Log progress
            if i % 20 == 0:
                self._log(f"SIM: bar {i+1}/{max_bars}")

            await asyncio.sleep(speed)

        self._log(f"SIMULATION COMPLETE — replayed {max_bars} bars")
        # Final state broadcast
        if self._on_state:
            assets_state = [self.get_asset_state(a) for a in ASSETS]
            await self._on_state({"type": "state", "vix": self._vix, "session": "SIM COMPLETE", "assets": assets_state})
            for asset in ASSETS:
                detail = self.get_asset_state(asset, include_bars=True)
                await self._on_state({"type": "asset_detail", "asset": asset, "data": detail})

    async def run_tick_simulation(self, speed: float = 0.01, day_offset: int = 0, minutes: int = 60):
        """Simulate Finnhub WebSocket ticks through the REAL tick pipeline.
        Unlike run_simulation which manually builds bars, this feeds synthetic ticks
        through _handle_tick → process_tick → 5m/15m aggregation → callbacks → detection.
        Tests the entire new data pipeline end-to-end.

        speed = seconds between each synthetic tick (0.01 = ~6 min for 1 hour)
        day_offset = 0 for most recent day, 1 for day before
        minutes = how many minutes of market data to replay (default 60 = 1 hour)
        """
        import yfinance as yf
        import random
        self._sim_mode = True
        self._gates.sim_mode = True
        self._log(f"TICK SIM: Loading data (speed={speed}s/tick, day_offset={day_offset}, minutes={minutes})")

        # Load daily bars for levels/zones (same as regular sim)
        for asset in ASSETS:
            try:
                ticker = yf.Ticker(asset)
                dfd = ticker.history(period="2y", interval="1d")
                daily = []
                for ts, row in dfd.iterrows():
                    try:
                        c = Candle(t=int(ts.timestamp()*1000), o=round(float(row["Open"]),2),
                                   h=round(float(row["High"]),2), l=round(float(row["Low"]),2),
                                   c=round(float(row["Close"]),2), v=float(row["Volume"]))
                        if c.c > 0 and c.h >= c.l:
                            daily.append(c)
                    except Exception:
                        pass
                store = self._candles.get(asset)
                store.load_daily(daily)
                await self._handle_bars(asset, "1d", daily)
                self._log(f"{asset}: {len(daily)} daily bars loaded")
            except Exception as e:
                self._log(f"{asset} daily load error: {e}", "error")
            await asyncio.sleep(0.2)

        # Load 1m bars to generate ticks from
        replay_data = {}
        for asset in ASSETS:
            try:
                ticker = yf.Ticker(asset)
                df1m = ticker.history(period="5d", interval="1m")
                all_1m = []
                for ts, row in df1m.iterrows():
                    try:
                        c = Candle(t=int(ts.timestamp()*1000), o=round(float(row["Open"]),2),
                                   h=round(float(row["High"]),2), l=round(float(row["Low"]),2),
                                   c=round(float(row["Close"]),2), v=float(row["Volume"]))
                        if c.c > 0 and c.h >= c.l:
                            all_1m.append(c)
                    except Exception:
                        pass
                # Filter market hours
                mkt_1m = [c for c in all_1m
                          if 570 <= datetime.fromtimestamp(c.t/1000, tz=ET).hour * 60 +
                                   datetime.fromtimestamp(c.t/1000, tz=ET).minute < 960]
                # Group by day
                days_map = {}
                for c in mkt_1m:
                    d = datetime.fromtimestamp(c.t/1000, tz=ET).strftime("%Y-%m-%d")
                    days_map.setdefault(d, []).append(c)
                sorted_days = sorted(days_map.keys(), reverse=True)
                pick_idx = min(day_offset, len(sorted_days) - 1)
                if sorted_days:
                    target_day = sorted_days[pick_idx]
                    day_bars = days_map[target_day][:minutes]  # limit to requested minutes
                    replay_data[asset] = day_bars
                    self._log(f"{asset}: {len(day_bars)} 1m bars → generating ticks ({target_day})")
                else:
                    replay_data[asset] = []
            except Exception as e:
                self._log(f"{asset} 1m load error: {e}", "error")
                replay_data[asset] = []
            await asyncio.sleep(0.2)

        self._vix = 20.0
        self._log(f"VIX: {self._vix:.1f} (simulated)")

        # NOTE: Do NOT start heartbeat in tick sim — ticks use historical timestamps
        # and the heartbeat uses real clock. They would fight each other.
        # The tick stream itself closes bars via process_tick timestamp math.

        # Generate synthetic ticks from 1m bars and feed through _handle_tick
        max_bars = max(len(bars) for bars in replay_data.values()) if replay_data else 0
        self._log(f"TICK SIM: Starting — {max_bars} bars, ~{max_bars * 10} ticks across 8 assets")

        total_ticks = 0
        for i in range(max_bars):
            for asset in ASSETS:
                bars = replay_data.get(asset, [])
                if i >= len(bars):
                    continue
                bar = bars[i]

                # Generate ~10 synthetic ticks per 1m bar
                # Realistic path: open → wander → high/low extremes → close
                ticks = []
                vol_per_tick = bar.v / 10 if bar.v > 0 else 100

                # Tick 1: open
                ticks.append((bar.o, vol_per_tick))

                # Ticks 2-4: random walk between open and extremes
                mid = (bar.o + bar.c) / 2
                for _ in range(3):
                    noise = random.uniform(bar.l, bar.h)
                    ticks.append((round(noise, 2), vol_per_tick))

                # Tick 5: low point
                ticks.append((bar.l, vol_per_tick * 1.5))

                # Tick 6: high point
                ticks.append((bar.h, vol_per_tick * 1.5))

                # Ticks 7-9: drift toward close
                for j in range(3):
                    frac = (j + 1) / 4
                    drift = bar.h * (1 - frac) + bar.c * frac if bar.c >= bar.o else bar.l * (1 - frac) + bar.c * frac
                    noise = drift + random.uniform(-0.02, 0.02) * abs(bar.h - bar.l)
                    noise = max(bar.l, min(bar.h, noise))
                    ticks.append((round(noise, 2), vol_per_tick))

                # Tick 10: close
                ticks.append((bar.c, vol_per_tick))

                # Feed each tick through the real pipeline
                for tick_idx, (price, vol) in enumerate(ticks):
                    # Spread tick timestamps across the minute
                    tick_ts = bar.t + int(tick_idx * 5500)  # ~5.5s apart within the minute
                    await self._handle_tick(asset, price, vol, tick_ts)
                    total_ticks += 1

                    # Small delay between ticks for realism
                    if speed > 0:
                        await asyncio.sleep(speed)

            # Broadcast state every 5 bars
            if i % 5 == 0 and self._on_state:
                bar_ref = replay_data.get("SPY", [{}])
                bar_time = ""
                if i < len(bar_ref):
                    bar_time = datetime.fromtimestamp(bar_ref[i].t / 1000, tz=ET).strftime("%H:%M")
                assets_state = [self.get_asset_state(a) for a in ASSETS]
                await self._on_state({
                    "type": "state", "vix": self._vix,
                    "session": f"TICK SIM {bar_time} bar {i+1}/{max_bars}",
                    "assets": assets_state,
                })
                for asset in ASSETS:
                    try:
                        detail = self.get_asset_state(asset, include_bars=True)
                        await self._on_state({"type": "asset_detail", "asset": asset, "data": detail})
                    except Exception:
                        pass

            if i % 10 == 0:
                store = self._candles.get("SPY")
                self._log(f"TICK SIM: bar {i+1}/{max_bars} | ticks:{total_ticks} | SPY 1m:{len(store.closed_1m)} 5m:{len(store.c5m_live)} 15m:{len(store.c15m_live)}")

        self._candles.stop()
        self._log(f"TICK SIM COMPLETE — {total_ticks} ticks, {max_bars} bars")
        self._log(f"  SPY: {len(self._candles.get('SPY').closed_1m)} 1m, {len(self._candles.get('SPY').c5m_live)} 5m, {len(self._candles.get('SPY').c15m_live)} 15m bars built from ticks")

        # Final broadcast
        if self._on_state:
            assets_state = [self.get_asset_state(a) for a in ASSETS]
            await self._on_state({"type": "state", "vix": self._vix, "session": "TICK SIM COMPLETE", "assets": assets_state})
            for asset in ASSETS:
                detail = self.get_asset_state(asset, include_bars=True)
                await self._on_state({"type": "asset_detail", "asset": asset, "data": detail})

    async def _broadcast_state_loop(self):
        cycle = 0
        while True:
            await asyncio.sleep(5)
            if self._on_state:
                try:
                    # Every 5s: send prices/cvd/bias for all assets
                    assets_state = [self.get_asset_state(a) for a in ASSETS]
                    await self._on_state({
                        "type": "state",
                        "vix": self._vix,
                        "session": self._get_session_label(),
                        "assets": assets_state,
                    })
                    # Every 15s: send bars + levels for each asset (rotated)
                    cycle += 1
                    if cycle % 3 == 0:
                        idx = (cycle // 3) % len(ASSETS)
                        asset = ASSETS[idx]
                        detail = self.get_asset_state(asset, include_bars=True)
                        await self._on_state({"type": "asset_detail", "asset": asset, "data": detail})
                except Exception:
                    pass

    def _get_session_label(self) -> str:
        from ..context.session import get_current_session
        return get_current_session().label

    def _log(self, msg: str, level: str = "info"):
        print(f"[{now_et().strftime('%H:%M:%S')}] [{level.upper()}] {msg}")

    async def _handle_tick(self, asset, price, volume, ts_ms):
        self._last_tick_time[asset] = time.time()
        self._health[asset].last_tick_at = time.time()
        # In sim mode, advance the simulated clock from tick timestamps
        if self._sim_mode:
            set_sim_time(ts_ms)
        self._cvd.process_trade(asset, price, volume)
        self._candles.process_tick(asset, price, volume, ts_ms)
        if self._on_tick:
            await self._on_tick({"type": "tick", "asset": asset, "price": price, "cvd": self._cvd.value(asset)})

    async def _handle_bars(self, asset, timeframe, candles):
        store = self._candles.get(asset)
        if timeframe == "1m":
            if self._sim_mode:
                # In sim mode, skip 1m/5m/15m yfinance bars — ticks build them from scratch
                # Only update levels from daily data
                pass
            elif not store.c1m:
                store.load_1m(candles)
            else:
                report = await store.validate_bars(candles)
                if report.get("problems", 0) > 0:
                    self._log(f"{asset} validation: {report['problems']} issues in {report['checked']} bars")
                await store.merge_backfill(candles)
            await self._update_levels(asset)
        elif timeframe == "1m_backfill":
            await store.merge_backfill(candles)
            self._log(f"{asset} backfilled {len(candles)} bars from gap")
        elif timeframe == "5m":
            if not self._sim_mode and not store.c5m_live:
                store.load_5m(candles)
        elif timeframe == "15m":
            if not self._sim_mode and not store.c15m_live:
                store.load_15m(candles)
        elif timeframe == "1d":
            store.load_daily(candles)
            price = store.live_price or (candles[-1].c if candles else 0)
            self._zones[asset] = detect_zones(candles, price)
            self._log(f"{asset}: {len(candles)} daily bars, {len(self._zones[asset])} zones")
            await self._update_levels(asset)

    async def _handle_vix(self, vix):
        self._vix = vix

    async def _handle_reconnect(self, gap_start: float, gap_end: float):
        """Called by DataFeed when WS reconnects after a disconnect.
        This fires once per WS reconnect — increment disconnect count once,
        not per-asset (it's a shared connection)."""
        gap_sec = gap_end - gap_start
        self._log(f"WS reconnected after {gap_sec:.0f}s gap")
        # Increment once per reconnect event, track on all assets equally
        for asset in ASSETS:
            if gap_sec <= BACKFILL_MAX_GAP_SEC:
                await self._feed.backfill_gap(asset, gap_start, gap_end)
            else:
                self._health[asset].status = "DEGRADED"
                self._log(f"{asset} gap too large ({gap_sec:.0f}s), marked DEGRADED")
        # Track disconnect count globally (same WS serves all assets)
        self._ws_disconnect_count += 1
        for asset in ASSETS:
            self._health[asset].ws_disconnects = self._ws_disconnect_count

    async def on_1m_close(self, asset: str):
        if not self._sim_mode and not is_trading_hours():
            return
        # Health-aware staleness check (skip in sim mode — ticks are historical)
        if not self._sim_mode and self._last_tick_time.get(asset, 0) > 0 and time.time() - self._last_tick_time[asset] > HEALTH_STALE_SEC:
            self._health[asset].status = "STALE"
            self._log(f"{asset} STALE ({int(time.time() - self._last_tick_time[asset])}s no ticks) — skipping detection")
            return
        h = self._health[asset]
        h.status = "DEGRADED" if h.bars_backfilled >= HEALTH_DEGRADED_BARS else "HEALTHY"
        store = self._candles.get(asset)
        bars = store.closed_1m
        if len(bars) < 2:
            return

        last = bars[-1]
        cvd_now = self._cvd.value(asset)
        cvd_prev = self._cvd.get(asset).value_1min_ago
        avg_vol = self._avg_vol(asset, "1m")
        atr = self._calc_atr(asset)
        price = store.live_price

        self._update_or(asset, bars)

        # Lightweight VWAP update on 1m (fast — no sorting/filtering)
        today_bars = filter_today_bars(bars)
        if today_bars:
            from ..levels.builder import calc_vwap
            self._last_vwap = {asset: calc_vwap(today_bars)}

        # Heavy compute (levels + volume profile) moved to on_5m_close

        if not self._investigating[asset]:
            confirmed, failed = self._tracker.on_1m_close(asset, last, cvd_now, cvd_prev, avg_vol, price, atr)
            for t in confirmed:
                level = self._find_level(asset, t.level_name)
                if level:
                    await self._fire_agent(asset, "BREAKOUT_RETEST", t.direction, level, t.break_candle, t.volume_ratio, t.cvd_at_break, t.retest_candle, t.cvd_at_retest, True)

            # Failed breakout → reverse signal (trapped traders exiting)
            for t in failed:
                if self._investigating[asset]:
                    break
                # Verify conviction: the fail candle must breach level by ATR × 0.1
                if not detect_failed_retest(last, t, atr):
                    continue  # Limbo state — not enough conviction to short/long the reverse
                # Confirm with CVD aligning to the reverse direction
                if confirm_failed_breakout(last, t, cvd_now, cvd_prev):
                    reverse_dir = get_reverse_direction(t.direction)
                    level = self._find_level(asset, t.level_name)
                    if level:
                        self._log(f"{asset} FAILED BREAKOUT at {t.level_name} ${t.level_price:.2f} → {reverse_dir}")
                        await self._fire_agent(asset, "FAILED_BREAKOUT", reverse_dir, level, last, t.volume_ratio, cvd_now - cvd_prev)

        if not self._investigating[asset]:
            await self._check_stop_hunts(asset, last, cvd_now, cvd_prev, avg_vol)

        await self._confirm_pending(asset, last, cvd_now, cvd_prev)
        await self._confirm_pending_rejections(asset, last)

    async def _confirm_pending_rejections(self, asset: str, candle):
        """1-bar confirmation for pending rejections. Price must hold on rejection side."""
        to_remove = []
        for key, p in self._pending_rejections.items():
            if p["asset"] != asset:
                continue
            p["candles_seen"] += 1

            level = p["level"]
            direction = p["direction"]

            # Check if price held on rejection side
            if direction == "BEARISH":
                if candle.c >= level.price:
                    # Price crossed back above — rejection failed
                    to_remove.append(key)
                    continue
                # Confirmed: still below level
                confirmed = True
            elif direction == "BULLISH":
                if candle.c <= level.price:
                    # Price crossed back below — rejection failed
                    to_remove.append(key)
                    continue
                confirmed = True
            else:
                to_remove.append(key)
                continue

            if confirmed:
                self._locked_rejection_levels[asset].add(level.name)
                to_remove.append(key)
                if not self._investigating[asset]:
                    self._log(f"{asset} REJECTION CONFIRMED {direction} ({p['strength']}) at {level.name} ${level.price:.2f}")
                    await self._fire_agent(
                        asset, "REJECTION", direction, level,
                        p["candle"], p["vol_ratio"], p["cvd_change"],
                        strength=p["strength"],
                    )
                continue

            # Expire after 2 candles with no confirmation
            if p["candles_seen"] >= 2:
                to_remove.append(key)

        for k in to_remove:
            self._pending_rejections.pop(k, None)

    async def on_5m_close(self, asset: str):
        if not self._sim_mode and not is_trading_hours():
            return
        if self._investigating[asset]:
            return
        if not self._sim_mode and self._last_tick_time.get(asset, 0) > 0 and time.time() - self._last_tick_time[asset] > 30:
            return
        store = self._candles.get(asset)
        bars = store.closed_5m
        if len(bars) < 2:
            return

        # Heavy compute on 5m close (moved from 1m to reduce CPU thrashing)
        await self._update_levels(asset)
        today_bars = filter_today_bars(store.closed_1m)
        if len(today_bars) >= 5:
            atr = self._calc_atr(asset)
            vp = compute_volume_profile(asset, today_bars, atr=atr)
            if vp:
                self._vol_profiles[asset] = vp

        if asset == "SPY":
            self._log(f"SPY 5m_close: {len(bars)} bars, last c=${bars[-1].c:.2f}, levels={len(self._levels.get(asset, []))}")

        last = bars[-1]
        prev = bars[-2]
        cvd_eng = self._cvd.get(asset)
        cvd_open = cvd_eng.value_5min_ago
        cvd_close = cvd_eng.value
        avg_vol = self._avg_vol(asset, "5m")
        dc = self._day_contexts.get(asset)
        bias = dc.bias if dc else "NEUTRAL"

        for level in self._levels[asset]:
            if level.score < 6 or self._tracker.is_locked(asset, level.name):
                continue

            is_bo, bo_dir = detect_breakout(last, prev, level, avg_vol, cvd_open, cvd_close)
            if is_bo and asset == "SPY":
                self._log(f"SPY BREAKOUT DETECTED {bo_dir} at {level.name} ${level.price:.2f}")
            if is_bo:
                vol_ratio = last.v / avg_vol if avg_vol > 0 else 1.0
                passed, reason = self._gates.check_all(asset, level.score, vol_ratio, self._vix, bo_dir, bias)
                if not passed:
                    continue
                self._tracker.start(asset, level.name, level.price, level.score, bo_dir, last, cvd_close, vol_ratio)
                self._log(f"{asset} BREAKOUT {bo_dir} at {level.name} ${level.price:.2f}")
                continue

            # Rejection: score >= 8 check BEFORE detection (CPU optimization)
            if level.score < 8:
                continue
            # Dedup: skip levels already fired today
            if level.name in self._locked_rejection_levels[asset]:
                continue
            atr = self._calc_atr(asset)
            is_rej, rej_dir, strength = detect_rejection(last, level, avg_vol, cvd_open, cvd_close, atr)
            if is_rej:
                vol_ratio = last.v / avg_vol if avg_vol > 0 else 1.0
                passed, reason = self._gates.check_all(asset, level.score, vol_ratio, self._vix, rej_dir, bias)
                if not passed:
                    continue
                # Queue for 1m confirmation instead of firing immediately
                self._pending_rejections[f"{asset}_{level.name}"] = {
                    "asset": asset, "level": level, "direction": rej_dir,
                    "candle": last, "vol_ratio": vol_ratio, "strength": strength,
                    "cvd_change": cvd_close - cvd_open, "candles_seen": 0,
                }
                self._log(f"{asset} REJECTION {rej_dir} ({strength}) at {level.name} ${level.price:.2f} — pending confirmation")
                break

        # Continuation breakout: price above/below level for 3+ consecutive 5m bars
        # Catches breakouts missed by one-shot detection (volume/CVD just under threshold)
        if not self._investigating[asset] and len(bars) >= 4:
            for level in self._levels[asset]:
                if level.score < MIN_LEVEL_SCORE:
                    continue
                if self._tracker.is_locked(asset, level.name):
                    continue
                if level.name in self._locked_stop_hunt_levels.get(asset, set()):
                    continue
                if level.name in self._locked_rejection_levels.get(asset, set()):
                    continue
                recent = bars[-4:]
                all_above = all(b.c > level.price for b in recent)
                all_below = all(b.c < level.price for b in recent)
                if not all_above and not all_below:
                    continue
                if last.v < avg_vol * 1.0:
                    continue
                cont_dir = "BULLISH" if all_above else "BEARISH"
                vol_ratio = last.v / avg_vol if avg_vol > 0 else 1.0
                passed, _ = self._gates.check_all(asset, level.score, vol_ratio, self._vix, cont_dir, bias)
                if not passed:
                    continue
                self._tracker._locked.add(self._tracker._key(asset, level.name))
                self._log(f"{asset} BREAKOUT_CONTINUATION {cont_dir} at {level.name} ${level.price:.2f}")
                await self._fire_agent(asset, "BREAKOUT_CONTINUATION", cont_dir, level, last, vol_ratio, cvd_close - cvd_open)
                break

    async def _check_stop_hunts(self, asset, candle, cvd_now, cvd_prev, avg_vol):
        dc = self._day_contexts.get(asset)
        bias = dc.bias if dc else "NEUTRAL"
        for level in self._levels[asset]:
            if level.score < 6 or self._tracker.is_locked(asset, level.name):
                continue
            # Stop hunt dedup: skip levels already fired today
            if level.name in self._locked_stop_hunt_levels[asset]:
                continue
            cvd_change = cvd_now - cvd_prev
            is_hunt, hunt_dir = detect_stop_hunt(candle, level, avg_vol, cvd_change)
            if is_hunt:
                vol_ratio = candle.v / avg_vol if avg_vol > 0 else 1.0
                passed, _ = self._gates.check_all(asset, level.score, vol_ratio, self._vix, hunt_dir, bias)
                if passed:
                    self._pending_stop_hunts[f"{asset}_{level.name}"] = {
                        "asset": asset, "level": level, "direction": hunt_dir,
                        "candle": candle, "vol_ratio": vol_ratio,
                        "candles_seen": 0, "max_candles": 2,
                    }

    async def _confirm_pending(self, asset, candle, cvd_now, cvd_prev):
        to_remove = []
        for key, p in self._pending_stop_hunts.items():
            if p["asset"] != asset:
                continue
            # Track candle closes instead of wall clock (fixes 90s clock mismatch)
            p["candles_seen"] = p.get("candles_seen", 0) + 1
            if p["candles_seen"] > p.get("max_candles", 2):
                to_remove.append(key)
                continue
            if confirm_stop_hunt(candle, p["direction"], p["level"], cvd_now, cvd_prev):
                await self._fire_agent(asset, "STOP_HUNT", p["direction"], p["level"], p["candle"], p["vol_ratio"], cvd_now - cvd_prev)
                # Lock this level for the day — prevent re-firing on same level
                self._locked_stop_hunt_levels[asset].add(p["level"].name)
                to_remove.append(key)
        for k in to_remove:
            self._pending_stop_hunts.pop(k, None)

    async def _fire_agent(self, asset, pattern, direction, level, candle, vol_ratio, cvd_change, retest_candle=None, cvd_at_retest=0.0, cvd_turned=False, strength=""):
        if self._investigating[asset]:
            return
        self._investigating[asset] = True
        try:
            store = self._candles.get(asset)
            price = store.live_price
            atr = self._calc_atr(asset)
            session = get_current_session()
            dc = self._day_contexts.get(asset, DayContext(asset=asset))
            levels = self._levels[asset]
            above = sorted([l for l in levels if l.price > price], key=lambda l: l.price)[:4]
            below = sorted([l for l in levels if l.price <= price], key=lambda l: l.price, reverse=True)[:4]

            # Build verification data inline (saves 1 LLM round-trip vs verify_setup tool call)
            handler_for_verify = ToolHandler(self._candles, self._cvd, self._levels, self._vol_profiles,
                                             self._day_contexts, self._signal_history, self._tracker, self._vix,
                                             {"asset": asset})
            verification_data = handler_for_verify.verify_setup(asset)

            brief = build_brief(
                asset=asset, pattern=pattern, direction=direction, level=level,
                event_candle=candle, retest_candle=retest_candle,
                cvd_at_break=cvd_change, cvd_now=self._cvd.value(asset),
                cvd_turned=cvd_turned, volume_ratio=vol_ratio,
                day_context=dc, vix=self._vix, current_price=price, atr=atr,
                nearest_above=above, nearest_below=below,
                session_name=session.label, session_quality=session.quality,
                minutes_to_cutoff=minutes_to_cutoff(), tests_today=level.tests_today,
                verification_data=verification_data, strength=strength,
            )

            # Pre-compute trade fields so send_signal auto-fills them
            # TP1 must be at least 1× ATR away from entry to avoid targeting noise levels
            # Skip levels that are too close and find the first meaningful target
            if direction == "BULLISH":
                s_entry = price
                s_stop = level.price - atr * 0.5
                s_risk = abs(s_entry - s_stop)
                min_tp_dist = max(atr * 1.0, s_risk * 2.0)  # at least 1 ATR or 2:1 RR
                tp_candidates = [l for l in above if (l.price - s_entry) >= min_tp_dist]
                if not tp_candidates:
                    tp_candidates = above  # fallback to nearest if nothing far enough
                s_tp1 = tp_candidates[0].price if tp_candidates else s_entry + atr * 2
                s_tp2 = tp_candidates[1].price if len(tp_candidates) > 1 else 0
            elif direction == "BEARISH":
                s_entry = price
                s_stop = level.price + atr * 0.5
                s_risk = abs(s_entry - s_stop)
                min_tp_dist = max(atr * 1.0, s_risk * 2.0)
                tp_candidates = [l for l in below if (s_entry - l.price) >= min_tp_dist]
                if not tp_candidates:
                    tp_candidates = below
                s_tp1 = tp_candidates[0].price if tp_candidates else s_entry - atr * 2
                s_tp2 = tp_candidates[1].price if len(tp_candidates) > 1 else 0
            else:
                s_entry, s_stop, s_tp1, s_tp2 = price, 0, 0, 0
                s_risk = 0
            s_rr = abs(s_tp1 - s_entry) / s_risk if s_risk > 0 else 0

            # Options pre-compute
            from ..context.options_context import get_options_env
            opts_env = get_options_env(self._vix)
            from ..core.asset_registry import get_config, has_daily_expiry
            cfg = get_config(asset)
            minor = cfg["round_interval_minor"]
            import math
            from datetime import timedelta
            atm = round(round(price / minor) * minor, 2) if price > 0 else 0
            now = now_et()
            hour = now.hour + now.minute / 60.0
            daily_exp = has_daily_expiry(asset)
            if hour < 11: s_dte = 0 if daily_exp and now.weekday() == 4 else 1
            elif hour < 13: s_dte = 1 if daily_exp else 2
            elif hour < 14.5: s_dte = 0 if daily_exp else 1
            else: s_dte = 0
            s_expiry = (now + timedelta(days=s_dte)).strftime("%b %d")
            daily_move = (price * (self._vix / 100)) / math.sqrt(252) if price > 0 and self._vix > 0 else 0
            prem = daily_move * 0.4
            s_opt_type = "CALL" if direction == "BULLISH" else "PUT"

            setup = {"asset": asset, "pattern": pattern, "direction": direction, "level_name": level.name,
                     "level_price": level.price, "level_score": level.score, "session": session.label,
                     "event_candle": candle, "volume_ratio": vol_ratio, "cvd_change": cvd_change,
                     "entry": round(s_entry, 2), "stop": round(s_stop, 2), "tp1": round(s_tp1, 2),
                     "tp2": round(s_tp2, 2), "rr": round(s_rr, 1),
                     "option_type": s_opt_type, "strike": atm, "expiry_date": s_expiry,
                     "dte": s_dte, "size": opts_env.get("size", "FULL"),
                     "est_premium_lo": round(prem * 0.8, 2), "est_premium_hi": round(prem * 1.2, 2),
                     "breakeven": round(price + prem if direction == "BULLISH" else price - prem, 2),
                     "instrument": opts_env.get("instrument", "ATM outright")}

            handler = ToolHandler(self._candles, self._cvd, self._levels, self._vol_profiles,
                                  self._day_contexts, self._signal_history, self._tracker, self._vix, setup)

            self._log(f"{asset} AGENT: {pattern} {direction} at {level.name} ${level.price:.2f}")

            from ..agent.agent import run_agent
            try:
                await asyncio.wait_for(
                    run_agent(handler, brief, self._openai_key,
                              on_complete=lambda sig: self._on_agent_complete(asset, level, sig)),
                    timeout=70.0,
                )
            except asyncio.TimeoutError:
                self._log(f"{asset} AGENT TIMEOUT after 70s — unlocking asset", "error")

        except Exception as e:
            self._log(f"{asset} agent error: {e}", "error")
        finally:
            self._investigating[asset] = False

    async def _on_agent_complete(self, asset, level, signal):
        if not signal:
            return
        # Broadcast ALL signals (including WAIT) for the signals log
        if self._on_signal:
            await self._on_signal(signal)
        # Only record LONG/SHORT in gates/budget and apply stale check
        if signal.direction in ("LONG", "SHORT"):
            current = self._candles.get(asset).live_price
            if current > 0 and signal.entry > 0:
                drift = abs(signal.entry - current) / current
                if drift > 0.005:
                    self._log(f"{asset} signal DISCARDED — entry ${signal.entry:.2f} stale (current ${current:.2f}, drift {drift:.1%})")
                    return
            h = self._health.get(asset)
            if h and h.status == "DEGRADED":
                signal.warnings = (signal.warnings or "") + f" [DATA DEGRADED: {h.bars_backfilled} backfilled bars]"
            self._gates.record_signal(asset)
            level.tests_today += 1
            self._log(f"{asset} {signal.direction} @ ${signal.entry:.2f} RR {signal.rr:.1f}:1")
        else:
            self._log(f"{asset} {signal.direction} — {signal.narrative[:80] if signal.narrative else 'no narrative'}")

    def _update_or(self, asset, bars_1m):
        if self._or_locked[asset]:
            return
        or_bars = [c for c in bars_1m if self._is_or_bar(c)]
        if or_bars:
            self._or_high[asset] = max(c.h for c in or_bars)
            self._or_low[asset] = min(c.l for c in or_bars)
        # In sim mode, check OR completion from bar timestamps (not wall clock)
        if self._sim_mode:
            if bars_1m and or_bars:
                last_bar_dt = datetime.fromtimestamp(bars_1m[-1].t / 1000, tz=ET)
                last_bar_min = last_bar_dt.hour * 60 + last_bar_dt.minute
                or_done = last_bar_min >= 600  # 10:00 AM — only after we have OR bars
            else:
                or_done = False
        else:
            or_done = is_or_complete()
        if or_done and not self._or_locked[asset]:
            self._or_locked[asset] = True
            self._log(f"{asset} OR locked H:${self._or_high[asset]:.2f} L:${self._or_low[asset]:.2f}")
            self._assess_day(asset)

    def _is_or_bar(self, c):
        dt = datetime.fromtimestamp(c.t / 1000, tz=ET)
        t = dt.hour * 60 + dt.minute
        return 570 <= t < 600

    def _assess_day(self, asset):
        store = self._candles.get(asset)
        dc = assess_day_context(asset, store.c_daily, store.closed_15m, filter_today_bars(store.closed_1m),
                                self._or_high[asset], self._or_low[asset], store.live_price)
        self._day_contexts[asset] = dc
        self._log(f"{asset} day: {dc.day_type} {dc.bias}")

    async def _update_levels(self, asset):
        store = self._candles.get(asset)
        price = store.live_price
        if not price or not store.c_daily:
            return
        today = filter_today_bars(store.closed_1m)
        vwap = calc_vwap(today)
        dc = self._day_contexts.get(asset)
        gap_pct = dc.gap_pct if dc else 0.0
        self._levels[asset] = build_levels(
            asset, store.c_daily, today, store.closed_5m, price, vwap,
            self._or_high[asset], self._or_low[asset], self._or_locked[asset],
            self._vol_profiles.get(asset), self._zones.get(asset, []),
            gap_pct=gap_pct,
        )

    def _find_level(self, asset, name):
        return next((l for l in self._levels[asset] if l.name == name), None)

    def _avg_vol(self, asset, tf):
        store = self._candles.get(asset)
        bars = store.closed_1m if tf == "1m" else store.closed_5m
        if not bars: return 0.0
        # Exclude the current bar (don't let a spike dilute its own comparison)
        prior = bars[:-1] if len(bars) > 1 else bars
        # Exclude OR bars (first 30 1m / first 6 5m)
        or_cutoff = 30 if tf == "1m" else 6
        non_or = prior[or_cutoff:] if len(prior) > or_cutoff else prior
        s = non_or[-20:]
        if not s: return 0.0
        # Median — resistant to spikes, stable with small samples
        vols = sorted(c.v for c in s)
        mid = len(vols) // 2
        return vols[mid] if len(vols) % 2 else (vols[mid - 1] + vols[mid]) / 2

    def _calc_atr(self, asset, period=14):
        bars = self._candles.get(asset).closed_1m[-(period+1):]
        if len(bars) < 2: return 1.0
        trs = [max(bars[i].h - bars[i].l, abs(bars[i].h - bars[i-1].c), abs(bars[i].l - bars[i-1].c)) for i in range(1, len(bars))]
        return sum(trs) / len(trs) if trs else 1.0

    def get_asset_state(self, asset, include_bars=False):
        store = self._candles.get(asset)
        dc = self._day_contexts.get(asset)
        gates = self._gates.get_status(asset)
        h = self._health.get(asset)
        state = {
            "asset": asset, "price": store.live_price, "cvd": self._cvd.value(asset),
            "cvd_bias": self._cvd.bias(asset), "vix": self._vix,
            "bias": dc.bias if dc else "N/A", "day_type": dc.day_type if dc else "N/A",
            "or_high": self._or_high[asset], "or_low": self._or_low[asset],
            "or_locked": self._or_locked[asset], "investigating": self._investigating[asset],
            "signals_today": gates["signals_today"], "budget_remaining": gates["budget_remaining"],
            "session": get_current_session().label,
            "data_health": h.status if h else "UNKNOWN",
            "bars_backfilled": h.bars_backfilled if h else 0,
        }
        if include_bars:
            state["c1m"] = [{"t":c.t,"o":c.o,"h":c.h,"l":c.l,"c":c.c,"v":c.v} for c in store.c1m[-100:]]
            state["c5m"] = [{"t":c.t,"o":c.o,"h":c.h,"l":c.l,"c":c.c,"v":c.v} for c in store.c5m[-100:]]
            state["c15m"] = [{"t":c.t,"o":c.o,"h":c.h,"l":c.l,"c":c.c,"v":c.v} for c in store.c15m[-100:]]
            state["levels"] = [{"name":l.name,"price":l.price,"score":l.score,"type":l.type,"source":l.source} for l in self._levels.get(asset, [])]
        return state
