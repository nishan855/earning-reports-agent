import asyncio
import time
import pytz
from datetime import datetime

from ..constants import ASSETS, BACKFILL_MAX_GAP_SEC, HEALTH_STALE_SEC, HEALTH_DEGRADED_BARS, MIN_LEVEL_SCORE
from ..models import Candle, Level, DayContext, DataHealth
from ..context.sim_clock import now_et, set_sim_time
from ..core.gates import GateSystem
from ..data.candle_store import MultiCandleStore
from ..data.cvd_engine import MultiCVDEngine
from ..data.data_feed import DataFeed
from ..levels.builder import build_levels, calc_vwap, filter_today_bars
from ..levels.volume_profile import compute_prior_day_profile
from ..levels.volume_profile import compute_volume_profile
from ..levels.zones import detect_zones
from ..detection.level_state import TrackerEngine
from ..detection.liquidity_grab import detect_liquidity_grab
from ..detection.defense import detect_ob_defense
from ..detection.approach import classify_approach
from ..detection.metrics import is_super_candle
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
        self._last_tick_time: dict = {a: 0.0 for a in ASSETS}
        self._tracker = TrackerEngine()
        self._gates = GateSystem()
        self._agent_lock = asyncio.Lock()
        self._last_tick_ts: dict = {a: 0.0 for a in ASSETS}  # seconds
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
        self._last_vwap: dict = {}
        self._prior_day_vp: dict = {}  # V3.1: prior day volume profile per asset
        self._pending_confirm: dict = {a: None for a in ASSETS}  # V3.3: confirmation candle queue
        # Register 1m/5m close callbacks on candle store
        for asset in ASSETS:
            self._candles.on_1m_close(asset, self.on_1m_close)
            self._candles.on_5m_close(asset, self.on_5m_close)

    async def start(self):
        self._log("MultiEngine starting — 8 assets")
        if self._sim_mode:
            await self._preload_prior_day_bars()
        else:
            self._candles.start_heartbeat()
        await asyncio.gather(
            self._feed.start(),
            self._broadcast_state_loop(),
        )

    async def _preload_prior_day_bars(self):
        """Pre-load prior day 1m bars from yfinance so prior day VP works in sim mode."""
        import yfinance as yf
        self._log("Loading prior day 1m bars for V3.1 volume profile...")
        for asset in ASSETS:
            try:
                ticker = yf.Ticker(asset)
                df = await asyncio.to_thread(lambda: ticker.history(period="5d", interval="1m"))
                if df.empty:
                    continue
                bars = []
                for ts, row in df.iterrows():
                    lo = float(row["Low"])
                    o, h, c, v = float(row["Open"]), float(row["High"]), float(row["Close"]), float(row["Volume"])
                    if c > 0 and h >= lo:
                        bars.append(Candle(t=int(ts.timestamp() * 1000), o=round(o, 2), h=round(h, 2),
                                           l=round(lo, 2), c=round(c, 2), v=v))
                if bars:
                    store = self._candles.get(asset)
                    store.closed_1m = bars  # seed with multi-day history
                    self._log(f"{asset}: pre-loaded {len(bars)} 1m bars for prior day VP")
            except Exception as e:
                self._log(f"{asset}: prior day load failed — {e}", "error")

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
                    for idx, row in df.iterrows():
                        try:
                            ts_ms = int(idx.timestamp() * 1000) if hasattr(idx, 'timestamp') else 0  # type: ignore[union-attr]
                            c = Candle(t=ts_ms, o=round(float(row["Open"]),2),
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
        max_bars = max(len(self._candles.get(a)._sim_replay) for a in ASSETS)

        for i in range(max_bars):
            for asset in ASSETS:
                store = self._candles.get(asset)
                replay = store._sim_replay
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
                replay = store._sim_replay
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

                            # S3A: Value Area Rejection (OUTSIDE level loop)
                            # V3.1: Check both prior day VP and developing VP
                            vp = self._vol_profiles.get(asset)
                            pdvp = self._prior_day_vp.get(asset)
                            sim_hour = et.hour + et.minute / 60.0
                            s3a_sim_checks = []
                            if pdvp and pdvp.vah and pdvp.val and pdvp.poc:
                                s3a_sim_checks.append((pdvp.vah, pdvp.val, pdvp.poc, Level(name="pdVAH", price=pdvp.vah, score=9, type="resistance", source="PD_VOLUME", confidence="HIGH"), "prior day"))
                            if vp and vp.vah and vp.val and vp.poc:
                                s3a_sim_checks.append((vp.vah, vp.val, vp.poc, Level(name="dVAH", price=vp.vah, score=7, type="resistance", source="VOLUME", confidence="HIGH"), "developing"))

                            if not self._investigating[asset] and s3a_sim_checks and sim_hour >= 11.0:
                                from ..detection.failed_auction import _detect_var
                                for s_vah, s_val, s_poc, var_level, vp_lbl in s3a_sim_checks:
                                    if self._investigating[asset]:
                                        break
                                    try:
                                        result = _detect_var(
                                            store.closed_5m, var_level, atr, avg_5m, cvd_val, 1.0,
                                            s_vah, s_val, s_poc, sim_hour, False, bias,
                                        )
                                        if result:
                                            fa_dir = result.get("direction", "NEUTRAL")
                                            vol_ratio = c5_last.v / avg_5m if avg_5m > 0 else 1.0
                                            passed, _ = self._gates.check_all(asset, var_level.score, vol_ratio, self._vix, fa_dir, bias)
                                            if passed:
                                                self._log(f"SIM DETECT: {asset} FAILED_AUCTION_VAR {fa_dir} at {vp_lbl} VAH/VAL")
                                                await self._fire_agent(asset, "FAILED_AUCTION_VAR", fa_dir, var_level, c5_last, vol_ratio, cvd_val)
                                                break
                                    except Exception:
                                        pass

                            # Level loop: S2 (OB Defense) + S3B (Major Level Rejection)
                            for level in self._levels[asset]:
                                if level.score < MIN_LEVEL_SCORE or self._investigating[asset]:
                                    continue
                                # Setup 2: OB Defense
                                try:
                                    dc = self._day_contexts.get(asset)
                                    sim_day_type = dc.day_type if dc else "RANGE"
                                    result = detect_ob_defense(
                                        store.closed_5m, store.closed_1m, level, atr, avg_5m,
                                        self._avg_vol(asset, "1m"), cvd_val, 1.0, sim_day_type, bias,
                                    )
                                    if result:
                                        ob_dir = result.get("direction", "NEUTRAL")
                                        vol_ratio = c5_last.v / avg_5m if avg_5m > 0 else 1.0
                                        passed, _ = self._gates.check_all(asset, level.score, vol_ratio, self._vix, ob_dir, bias)
                                        if passed:
                                            self._log(f"SIM DETECT: {asset} OB_DEFENSE {ob_dir} at {level.name} ${level.price:.2f}")
                                            await self._fire_agent(asset, "OB_DEFENSE", ob_dir, level, c5_last, vol_ratio, cvd_val)
                                            break
                                except Exception:
                                    pass
                                # Setup 3B: Major Level Rejection only (S3A handled above)
                                try:
                                    if level.score >= 8:
                                        from ..detection.failed_auction import _detect_major_level
                                        result = _detect_major_level(
                                            store.closed_5m, level, atr, avg_5m,
                                            cvd_val, 1.0, False, bias,
                                            candles_1m=store.closed_1m[-10:],
                                        )
                                        if result:
                                            fa_dir = result.get("direction", "NEUTRAL")
                                            vol_ratio = c5_last.v / avg_5m if avg_5m > 0 else 1.0
                                            passed, _ = self._gates.check_all(asset, level.score, vol_ratio, self._vix, fa_dir, bias)
                                            if passed:
                                                self._log(f"SIM DETECT: {asset} FAILED_AUCTION_MAJOR {fa_dir} at {level.name} ${level.price:.2f}")
                                                await self._fire_agent(asset, "FAILED_AUCTION_MAJOR", fa_dir, level, c5_last, vol_ratio, cvd_val)
                                                break
                                except Exception:
                                    pass

                        # 1m detection (liquidity grab)
                        for level in self._levels[asset]:
                            if level.score < MIN_LEVEL_SCORE or self._investigating[asset]:
                                continue
                            try:
                                cvd_change = cvd_eng.value - (cvd_eng.value_1min_ago if cvd_eng._history else 0)
                                result = detect_liquidity_grab(
                                    bars_so_far[-10:], level, atr, avg_vol, cvd_change, 1.0,
                                )
                                if result:
                                    grab_dir = result.get("direction", "NEUTRAL")
                                    vol_ratio = last.v / avg_vol if avg_vol > 0 else 1.0
                                    passed, _ = self._gates.check_all(asset, level.score, vol_ratio, self._vix, grab_dir, bias)
                                    if passed:
                                        self._log(f"SIM DETECT: {asset} LIQUIDITY_GRAB {grab_dir} at {level.name} ${level.price:.2f}")
                                        await self._fire_agent(asset, "LIQUIDITY_GRAB", grab_dir, level, last, vol_ratio, cvd_change)
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
                for idx, row in dfd.iterrows():
                    try:
                        ts_ms = int(idx.timestamp() * 1000) if hasattr(idx, 'timestamp') else 0  # type: ignore[union-attr]
                        c = Candle(t=ts_ms, o=round(float(row["Open"]),2),
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
                for idx, row in df1m.iterrows():
                    try:
                        ts_ms = int(idx.timestamp() * 1000) if hasattr(idx, 'timestamp') else 0  # type: ignore[union-attr]
                        c = Candle(t=ts_ms, o=round(float(row["Open"]),2),
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

    def _is_stale(self, asset: str, threshold_sec: float = 30.0) -> bool:
        last = self._last_tick_ts.get(asset, 0)
        return last > 0 and (time.time() - last) > threshold_sec

    async def _handle_tick(self, asset, price, volume, ts_ms):
        self._last_tick_time[asset] = time.time()
        self._last_tick_ts[asset] = time.time()
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
            self._cvd.get(asset).set_estimated(True)  # CVD is unreliable after reconnect

    def _is_cvd_quarantined(self, asset: str) -> bool:
        return self._cvd.get(asset).is_estimated

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
        # Record CVD turn for rolling average
        cvd_1m_turn = cvd_now - cvd_prev
        self._cvd.get(asset).record_cvd_turn(cvd_1m_turn)
        avg_vol = self._avg_vol(asset, "1m")
        atr = self._calc_atr(asset)

        self._update_or(asset, bars)

        # Lightweight VWAP update on 1m (fast — no sorting/filtering)
        today_bars = filter_today_bars(bars)
        if today_bars:
            from ..levels.builder import calc_vwap
            self._last_vwap = {asset: calc_vwap(today_bars)}

        # Heavy compute (levels + volume profile) moved to on_5m_close

        # V3.3: Confirmation candle — check pending signals from previous bar
        await self._check_pending_confirmation(asset, last)

        if not self._investigating[asset]:
            await self._check_liquidity_grabs(asset, last, cvd_now, cvd_prev, avg_vol)

        # S3B 1m Sniper: check for rejection on 1m candle using 5m approach context
        if not self._investigating[asset]:
            await self._check_s3b_sniper(asset, bars, last, cvd_now, cvd_prev, avg_vol, atr)

    async def _check_s3b_sniper(self, asset, bars_1m, candle, cvd_now, cvd_prev, avg_vol_1m, atr):
        """S3B 1m sniper — uses 5m approach context, triggers on 1m rejection candle."""
        store = self._candles.get(asset)
        bars_5m = store.closed_5m
        if len(bars_5m) < 3:
            return

        dc = self._day_contexts.get(asset)
        bias = dc.bias if dc else "NEUTRAL"
        cvd_eng = self._cvd.get(asset)
        cvd_turn = cvd_now - cvd_prev
        rolling_avg_cvd = cvd_eng.rolling_avg_cvd_turn(10)
        cvd_quarantine = self._is_cvd_quarantined(asset)

        # S3B only — S3A is handled in on_5m_close outside the level loop
        from ..detection.failed_auction import _detect_major_level
        for level in self._levels[asset]:
            if level.score < 8 or self._tracker.is_locked(asset, level.name):
                continue
            try:
                result = _detect_major_level(
                    bars_5m, level, atr, avg_vol_1m, cvd_turn, rolling_avg_cvd,
                    cvd_quarantine, bias, candles_1m=bars_1m[-10:],
                )
                if result:
                    fa_dir = result.get("direction", "NEUTRAL")
                    vol_ratio = candle.v / avg_vol_1m if avg_vol_1m > 0 else 1.0
                    passed, _ = self._gates.check_all(asset, level.score, vol_ratio, self._vix, fa_dir, bias)
                    if passed:
                        approach = result.get("approach")
                        self._log(f"{asset} FAILED_AUCTION_MAJOR {fa_dir} at {level.name} ${level.price:.2f}")
                        self._queue_pending(asset, {
                            "pattern": "FAILED_AUCTION_MAJOR", "direction": fa_dir,
                            "level": level, "candle": candle,
                            "vol_ratio": vol_ratio, "cvd_change": cvd_turn,
                            "approach_type": approach.type if approach else "",
                            "approach_confidence_pts": approach.confidence_pts if approach else 0,
                            "cvd_quarantine": cvd_quarantine,
                        })
                        break
            except Exception:
                pass

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

        # Heavy compute on 5m close
        await self._update_levels(asset)
        today_bars = filter_today_bars(store.closed_1m)
        if len(today_bars) >= 5:
            atr = self._calc_atr(asset)
            vp = compute_volume_profile(asset, today_bars, atr=atr)
            if vp:
                self._vol_profiles[asset] = vp

        # Re-assess day type and bias on every 5m close (not just at OR lock)
        if self._or_locked[asset]:
            self._assess_day(asset)

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

        atr = self._calc_atr(asset)
        avg_vol_1m = self._avg_vol(asset, "1m")
        bars_1m = store.closed_1m
        cvd_eng = self._cvd.get(asset)
        cvd_turn = cvd_close - cvd_open
        rolling_avg_cvd = cvd_eng.rolling_avg_cvd_turn(10)
        day_type = dc.day_type if dc else "RANGE"

        # Volume profile values for failed_auction
        vp = self._vol_profiles.get(asset)
        vah = vp.vah if vp else 0.0
        val_price = vp.val if vp else 0.0
        poc = vp.poc if vp else 0.0
        now_hour = now_et().hour + now_et().minute / 60.0

        cvd_quarantine = self._is_cvd_quarantined(asset)

        # ── S3A: Value Area Rejection (OUTSIDE level loop — uses VAH/VAL only) ──
        # V3.1: Check both prior day VP (preferred) and today's developing VP
        s3a_checks = []
        pdvp = self._prior_day_vp.get(asset)
        if pdvp and pdvp.vah and pdvp.val and pdvp.poc:
            pd_level = next((lv for lv in self._levels[asset] if lv.name in ("pdVAH", "pdVAL", "pdPOC")), None)
            if not pd_level:
                pd_level = Level(name="pdVAH", price=pdvp.vah, score=9, type="resistance", source="PD_VOLUME", confidence="HIGH")
            s3a_checks.append((pdvp.vah, pdvp.val, pdvp.poc, pd_level, "prior day"))
        if vah and val_price and poc:
            d_level = next((lv for lv in self._levels[asset] if lv.name in ("dVAH", "dVAL", "dPOC")), None)
            if not d_level:
                d_level = Level(name="dVAH", price=vah, score=7, type="resistance", source="VOLUME", confidence="HIGH")
            s3a_checks.append((vah, val_price, poc, d_level, "developing"))

        if not self._investigating[asset] and now_hour >= 11.0:
            from ..detection.failed_auction import _detect_var
            for s3a_vah, s3a_val, s3a_poc, var_level, vp_label in s3a_checks:
                if self._investigating[asset]:
                    break
                try:
                    result = _detect_var(
                        bars, var_level, atr, avg_vol, cvd_turn, rolling_avg_cvd,
                        s3a_vah, s3a_val, s3a_poc, now_hour, cvd_quarantine, bias,
                    )
                    if result:
                        fa_dir = result.get("direction", "NEUTRAL")
                        vol_ratio = last.v / avg_vol if avg_vol > 0 else 1.0
                        passed, _ = self._gates.check_all(asset, var_level.score, vol_ratio, self._vix, fa_dir, bias)
                        if passed:
                            approach = result.get("approach")
                            self._log(f"{asset} FAILED_AUCTION_VAR {fa_dir} at {vp_label} VAH/VAL target=POC ${s3a_poc:.2f}")
                            self._queue_pending(asset, {
                                "pattern": "FAILED_AUCTION_VAR", "direction": fa_dir,
                                "level": var_level, "candle": last,
                                "vol_ratio": vol_ratio, "cvd_change": cvd_turn,
                                "approach_type": approach.type if approach else "",
                                "approach_confidence_pts": approach.confidence_pts if approach else 0,
                                "cvd_quarantine": cvd_quarantine,
                            })
                            break
                except Exception:
                    pass

        # ── Level loop: S2 (OB Defense) + S3B (Major Level Rejection) ──
        for level in self._levels[asset]:
            if level.score < MIN_LEVEL_SCORE or self._tracker.is_locked(asset, level.name):
                continue
            if self._investigating[asset]:
                break

            # Setup 2: OB Defense
            try:
                result = detect_ob_defense(
                    bars, bars_1m, level, atr, avg_vol, avg_vol_1m,
                    cvd_turn, rolling_avg_cvd, day_type, bias,
                    cvd_quarantine=cvd_quarantine,
                )
                if result:
                    ob_dir = result.get("direction", "NEUTRAL")
                    vol_ratio = last.v / avg_vol if avg_vol > 0 else 1.0
                    passed, reason = self._gates.check_all(asset, level.score, vol_ratio, self._vix, ob_dir, bias)
                    if passed:
                        approach = result.get("approach")
                        self._log(f"{asset} OB_DEFENSE {ob_dir} at {level.name} ${level.price:.2f}")
                        await self._fire_agent(
                            asset, "OB_DEFENSE", ob_dir, level, last, vol_ratio, cvd_turn,
                            approach_type=approach.type if approach else "",
                            approach_confidence_pts=approach.confidence_pts if approach else 0,
                            cvd_quarantine=cvd_quarantine,
                        )
                        break
            except Exception:
                pass

            # Setup 3B: Major Level Rejection (S3B only — S3A moved out of loop)
            if level.score >= 8:
                try:
                    from ..detection.failed_auction import _detect_major_level
                    result = _detect_major_level(
                        bars, level, atr, avg_vol_1m, cvd_turn, rolling_avg_cvd,
                        cvd_quarantine, bias, candles_1m=bars_1m[-10:],
                    )
                    if result:
                        fa_dir = result.get("direction", "NEUTRAL")
                        vol_ratio = last.v / avg_vol if avg_vol > 0 else 1.0
                        passed, reason = self._gates.check_all(asset, level.score, vol_ratio, self._vix, fa_dir, bias)
                        if passed:
                            approach = result.get("approach")
                            self._log(f"{asset} FAILED_AUCTION_MAJOR {fa_dir} at {level.name} ${level.price:.2f}")
                            self._queue_pending(asset, {
                                "pattern": "FAILED_AUCTION_MAJOR", "direction": fa_dir,
                                "level": level, "candle": last,
                                "vol_ratio": vol_ratio, "cvd_change": cvd_turn,
                                "approach_type": approach.type if approach else "",
                                "approach_confidence_pts": approach.confidence_pts if approach else 0,
                                "cvd_quarantine": cvd_quarantine,
                            })
                            break
                except Exception:
                    pass

    async def _check_liquidity_grabs(self, asset, candle, cvd_now, cvd_prev, avg_vol):
        dc = self._day_contexts.get(asset)
        bias = dc.bias if dc else "NEUTRAL"
        atr = self._calc_atr(asset)
        store = self._candles.get(asset)
        bars_1m = store.closed_1m[-10:]  # last 10 1m bars for liquidity grab detection
        cvd_quarantine = self._is_cvd_quarantined(asset)
        for level in self._levels[asset]:
            if level.score < MIN_LEVEL_SCORE or self._tracker.is_locked(asset, level.name):
                continue
            cvd_change = cvd_now - cvd_prev
            cvd_eng = self._cvd.get(asset)
            rolling_avg_cvd = cvd_eng.rolling_avg_cvd_turn(10)
            try:
                result = detect_liquidity_grab(
                    bars_1m, level, atr, avg_vol, cvd_change, rolling_avg_cvd,
                    cvd_quarantine=cvd_quarantine, day_bias=bias,
                )
                if result:
                    grab_dir = result.get("direction", "NEUTRAL")
                    vol_ratio = candle.v / avg_vol if avg_vol > 0 else 1.0
                    passed, _ = self._gates.check_all(asset, level.score, vol_ratio, self._vix, grab_dir, bias)
                    if passed:
                        self._log(f"{asset} LIQUIDITY_GRAB {grab_dir} at {level.name} ${level.price:.2f}")
                        approach = result.get("approach")
                        self._queue_pending(asset, {
                            "pattern": "LIQUIDITY_GRAB", "direction": grab_dir,
                            "level": level, "candle": candle,
                            "vol_ratio": vol_ratio, "cvd_change": cvd_change,
                            "approach_type": approach.type if approach else "",
                            "approach_confidence_pts": approach.confidence_pts if approach else 0,
                            "cvd_quarantine": cvd_quarantine,
                        })
                        break
            except Exception:
                pass

    def _queue_pending(self, asset: str, pending: dict):
        """V3.3: Queue a reversal detection for confirmation on the next 1m bar."""
        self._pending_confirm[asset] = pending
        self._log(f"{asset} PENDING: {pending['pattern']} {pending['direction']} at {pending['level'].name} — awaiting confirmation candle")

    async def _check_pending_confirmation(self, asset: str, candle):
        """V3.3: Check if the confirmation candle validates the pending reversal."""
        pending = self._pending_confirm.get(asset)
        if not pending:
            return
        self._pending_confirm[asset] = None  # clear regardless of outcome

        if self._investigating[asset]:
            self._log(f"{asset} PENDING DROPPED: {pending['pattern']} — asset busy")
            return

        direction = pending["direction"]
        level = pending["level"]
        trigger_candle = pending["candle"]

        # Confirmation: next candle must close in the reversal direction
        # and must NOT break back through the level (invalidation)
        if direction == "BULLISH":
            confirmed = candle.c > candle.o  # bullish close
            invalidated = candle.c < level.price  # closed back below level
        else:
            confirmed = candle.c < candle.o  # bearish close
            invalidated = candle.c > level.price  # closed back above level

        if invalidated:
            self._log(f"{asset} CONFIRM FAILED: {pending['pattern']} {direction} at {level.name} — price broke back through level")
            return

        if not confirmed:
            self._log(f"{asset} CONFIRM FAILED: {pending['pattern']} {direction} at {level.name} — no directional follow-through")
            return

        self._log(f"{asset} CONFIRMED: {pending['pattern']} {direction} at {level.name} — firing agent")
        self._tracker._locked.add(self._tracker._key(asset, level.name))
        await self._fire_agent(
            asset, pending["pattern"], direction, level,
            trigger_candle, pending["vol_ratio"], pending["cvd_change"],
            approach_type=pending.get("approach_type", ""),
            approach_confidence_pts=pending.get("approach_confidence_pts", 0),
            cvd_quarantine=pending.get("cvd_quarantine", False),
        )

    async def _fire_agent(self, asset, pattern, direction, level, candle, vol_ratio, cvd_change, retest_candle=None, cvd_at_retest=0.0, cvd_turned=False, strength="", approach_type="", approach_confidence_pts=0, cvd_quarantine=False):
        if self._investigating[asset]:
            return
        self._investigating[asset] = True
        signal_born = time.time()
        try:
            await self._agent_lock.acquire()

            # V3.3 Signal TTL — drop stale signals that waited too long on the lock
            wait_delta = time.time() - signal_born
            if wait_delta > 20.0:
                self._log(f"{asset} STALE_SIGNAL_SKIPPED: {pattern} {direction} at {level.name} — waited {wait_delta:.1f}s in queue", "warning")
                return

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
                approach_type=approach_type,
                approach_confidence_pts=approach_confidence_pts,
                cvd_quarantine=cvd_quarantine,
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
            from ..core.asset_registry import get_config
            cfg = get_config(asset)
            minor = cfg["round_interval_minor"]
            import math
            atm = round(round(price / minor) * minor, 2) if price > 0 else 0
            from ..context.options_context import get_expiry
            s_dte, s_expiry, _ = get_expiry(asset)
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
                              on_complete=lambda sig: self._on_agent_complete(asset, level, sig),
                              on_tool_call=self._on_tool_call,
                              on_token=self._on_token,
                              on_error=self._make_agent_error_handler(asset)),
                    timeout=70.0,
                )
            except asyncio.TimeoutError:
                self._log(f"{asset} AGENT TIMEOUT after 70s — unlocking asset", "error")

        except Exception as e:
            self._log(f"{asset} agent error: {e}", "error")
        finally:
            if self._agent_lock.locked():
                self._agent_lock.release()
            self._investigating[asset] = False

    def _make_agent_error_handler(self, asset):
        async def _handler(err):
            self._log(f"{asset} AGENT ERROR: {err}", "error")
        return _handler

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
            self._tracker.reset_daily()
            self._log(f"{asset} OR locked H:${self._or_high[asset]:.2f} L:${self._or_low[asset]:.2f}")
            self._assess_day(asset)

    def _is_or_bar(self, c):
        dt = datetime.fromtimestamp(c.t / 1000, tz=ET)
        t = dt.hour * 60 + dt.minute
        return 570 <= t < 600

    def _assess_day(self, asset):
        store = self._candles.get(asset)
        today_bars = filter_today_bars(store.closed_1m)
        atr = self._calc_atr(asset)
        vwap = calc_vwap(today_bars)
        pdvp = self._prior_day_vp.get(asset)
        pd_vah = pdvp.vah if pdvp else 0.0
        pd_val = pdvp.val if pdvp else 0.0
        dc = assess_day_context(
            asset, store.c_daily, store.closed_15m, today_bars,
            self._or_high[asset], self._or_low[asset], store.live_price,
            atr=atr, pd_vah=pd_vah, pd_val=pd_val, vwap=vwap,
        )
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

        # V3.1: Compute prior day VP once (cached — immutable for the session)
        if asset not in self._prior_day_vp and len(store.closed_1m) > 30:
            atr = self._calc_atr(asset)
            pdvp = compute_prior_day_profile(asset, store.closed_1m, atr=atr)
            if pdvp:
                self._prior_day_vp[asset] = pdvp
                self._log(f"{asset} V3.1: prior day VP computed — pdPOC=${pdvp.poc:.2f} pdVAH=${pdvp.vah:.2f} pdVAL=${pdvp.val:.2f}")

        self._levels[asset] = build_levels(
            asset, store.c_daily, today, store.closed_5m, price, vwap,
            self._or_high[asset], self._or_low[asset], self._or_locked[asset],
            self._vol_profiles.get(asset), self._zones.get(asset, []),
            gap_pct=gap_pct,
            prior_day_vp=self._prior_day_vp.get(asset),
        )

    def _find_level(self, asset, name):
        return next((l for l in self._levels[asset] if l.name == name), None)

    def _avg_vol(self, asset, tf):
        """Rolling average of last 10 bars (excluding current bar).
        Per v2.1 spec: rolling_avg(last_10_bars) — local window, not session-wide."""
        store = self._candles.get(asset)
        bars = store.closed_1m if tf == "1m" else store.closed_5m
        if not bars: return 0.0
        prior = bars[:-1] if len(bars) > 1 else bars
        s = prior[-10:]
        if not s: return 0.0
        return sum(c.v for c in s) / len(s)

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
