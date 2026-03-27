import asyncio
import time
import datetime
import os
import pytz
from dataclasses import dataclass, field
from typing import Any

from .candles import CandleStore
from .cvd import CVDEngine
from .factors import interpret_vix
from .levels import build_levels, compute_opening_range
from .finnhub import FinnhubClient
from .sessions import get_session, is_trading_allowed
from .models import Signal, PreMarketData, Level
from .market_utils import calc_avg_vol

_ET = pytz.timezone("America/New_York")


def _is_premarket_ts(ts_ms: int) -> bool:
    et = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=_ET)
    t = et.hour * 60 + et.minute
    return 240 <= t < 570


def _get_et_time() -> str:
    return datetime.datetime.now(_ET).strftime("%H:%M:%S ET")


@dataclass
class SPYState:
    live_price: float = 0.0
    cvd: float = 0.0
    cvd_bias: str = "NEUTRAL"
    session_label: str = "CLOSED"
    session_color: str = "#475569"
    vix: float | None = None
    vix_label: str = ""
    vix_color: str = "#475569"
    signal: Any = None
    stream_text: str = ""
    or_high: float = 0.0
    or_low: float = 0.0
    or_complete: bool = False
    levels: list = field(default_factory=list)
    pm_data: Any = None
    agent_status: str = "idle"
    agent_level: str = ""
    c1m: list = field(default_factory=list)
    c5m: list = field(default_factory=list)
    c15m: list = field(default_factory=list)
    log: list = field(default_factory=list)
    connected_to_finnhub: bool = False


class SPYEngine:
    SYMBOL = "SPY"

    def __init__(self):
        self._finnhub_key = os.environ.get("FINNHUB_KEY", "")
        self._openai_key = os.environ.get("OPENAI_KEY", "")
        self._telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

        self.candles = CandleStore()
        self.cvd = CVDEngine()
        self.finnhub = FinnhubClient(self._finnhub_key)
        self.state = SPYState()
        self.pm_data: PreMarketData | None = None
        self.vix_val: float | None = None
        self.model = "gpt-5.4"
        self.cooldown_secs = 300

        self._clients: set = set()
        self._log_buf: list[dict] = []

        # Agent state
        self._last_signal_time = 0.0
        self._last_level_times: dict[str, float] = {}
        self._generating = False
        self._current_level_name = ""
        self._current_levels: list[Level] = []
        self._level_tests: dict[str, list[dict]] = {}
        self.or_data = None
        self.signal_history: list[dict] = []

        self._last_5m_count = 0
        self._broken_levels: set[str] = set()
        self.candles.on_candle_close(self._on_candle_close)

    async def start(self):
        if not self._finnhub_key:
            self._log("FINNHUB_KEY not set in .env", "error")
            return
        if not self._openai_key:
            self._log("OPENAI_KEY not set in .env", "error")
            return

        self._log("SPY Engine starting...")
        self._log(f"LLM model: {self.model}", "info")
        await self._load_initial_data()
        asyncio.create_task(self._run_finnhub_ws())
        asyncio.create_task(self._run_vix_poller())
        asyncio.create_task(self._run_bar_poller())

    async def _load_initial_data(self):
        self._log("Loading historical bars...")
        c_daily, c15m, c5m, c1m = await asyncio.gather(
            self.finnhub.fetch_bars(self.SYMBOL, "D"),
            self.finnhub.fetch_bars(self.SYMBOL, "15"),
            self.finnhub.fetch_bars(self.SYMBOL, "5"),
            self.finnhub.fetch_bars(self.SYMBOL, "1"),
        )
        self.candles.load(c1m, c5m, c15m, c_daily)
        self._log(f"Bars: {len(c1m)}x1m  {len(c5m)}x5m  {len(c15m)}x15m  {len(c_daily)}xdaily", "success")

        if len(c_daily) >= 2:
            prev = c_daily[-2]
            today = c_daily[-1]
            gap = (today.o - prev.c) / prev.c * 100
            pm_bars = [c for c in c5m if _is_premarket_ts(c.t)]
            pm_h = max(c.h for c in pm_bars) if pm_bars else today.o
            pm_l = min(c.l for c in pm_bars) if pm_bars else today.o
            self.pm_data = PreMarketData(
                pd_high=prev.h, pd_low=prev.l, pd_close=prev.c,
                pm_high=pm_h, pm_low=pm_l,
                gap_pct=round(gap, 2),
                gap_type="GAP UP" if gap > 0.2 else "GAP DOWN" if gap < -0.2 else "FLAT",
                gap_fill=prev.c,
            )
            self._log(f"Pre-market: {self.pm_data.gap_type} {gap:+.2f}% | PDH ${prev.h:.2f} PDL ${prev.l:.2f} | PM bars: {len(pm_bars)}", "success")

        self.vix_val = await self.finnhub.fetch_vix()
        self._update_vix_state()
        if self.vix_val:
            self._log(f"VIX: {self.vix_val:.1f} ({self.state.vix_label})", "info")

        # Initial level computation
        await self._on_candle_close()

    # ── Data pollers ──────────────────────────────────────

    async def _run_finnhub_ws(self):
        async def on_trade(price: float, volume: float, ts: int):
            self.cvd.process_trade(price, volume)
            self.candles.update_live(price, volume, ts)
            self.state.live_price = price
            self.state.cvd = self.cvd.value
            self.state.cvd_bias = self.cvd.bias
            await self._broadcast({"type": "tick", "price": price, "cvd": self.cvd.value})

        self.state.connected_to_finnhub = True
        await self.finnhub.connect_ws(self.SYMBOL, on_trade)

    def _update_vix_state(self):
        if self.vix_val is not None and self.vix_val > 0:
            vd = interpret_vix(self.vix_val)
            self.state.vix = self.vix_val
            self.state.vix_label = vd.label
            self.state.vix_color = vd.color

    async def _run_vix_poller(self):
        while True:
            await asyncio.sleep(30)
            vix = await self.finnhub.fetch_vix()
            if vix:
                self.vix_val = vix
                self._update_vix_state()

    async def _run_bar_poller(self):
        while True:
            await asyncio.sleep(60)
            try:
                c1m, c5m, c15m = await asyncio.gather(
                    self.finnhub.fetch_bars(self.SYMBOL, "1"),
                    self.finnhub.fetch_bars(self.SYMBOL, "5"),
                    self.finnhub.fetch_bars(self.SYMBOL, "15"),
                )
                if c1m:
                    live = self.candles.c1m[-1] if self.candles.c1m else None
                    self.candles.c1m = c1m[:-1] + ([live] if live else [c1m[-1]])
                if c5m:
                    live5 = self.candles.c5m[-1] if self.candles.c5m else None
                    self.candles.c5m = c5m[:-1] + ([live5] if live5 else [c5m[-1]])
                if c15m:
                    live15 = self.candles.c15m[-1] if self.candles.c15m else None
                    self.candles.c15m = c15m[:-1] + ([live15] if live15 else [c15m[-1]])
            except Exception as e:
                self._log(f"Bar poll error: {e}", "error")

    # ── Candle close → proximity gate → agent ─────────────

    def _is_price_near_level(self, price: float, levels: list[Level]) -> tuple[bool, Level | None]:
        nearest = None
        nearest_dist = float("inf")
        for lvl in levels:
            if lvl.price <= 0:
                continue
            dist_pct = abs(price - lvl.price) / price
            if dist_pct <= 0.005:
                if dist_pct < nearest_dist:
                    nearest_dist = dist_pct
                    nearest = lvl
        return (nearest is not None), nearest

    def _is_rejection_candle(self, candle, level: Level) -> bool:
        body = abs(candle.c - candle.o)
        upper_wick = candle.h - max(candle.o, candle.c)
        lower_wick = min(candle.o, candle.c) - candle.l

        if body < 0.02:
            return False

        touched_above = (
            candle.h >= level.price
            and abs(candle.h - level.price) / level.price < 0.003
        )
        if touched_above and upper_wick >= body * 2.0 and candle.c < level.price:
            return True

        touched_below = (
            candle.l <= level.price
            and abs(candle.l - level.price) / level.price < 0.003
        )
        if touched_below and lower_wick >= body * 2.0 and candle.c > level.price:
            return True

        return False

    def _is_stop_hunt_candle(self, candle, level: Level) -> bool:
        body = abs(candle.c - candle.o)
        upper_wick = candle.h - max(candle.o, candle.c)
        lower_wick = min(candle.o, candle.c) - candle.l
        if body < 0.02:
            return False
        # Bullish stop hunt: wick swept below support, closed above
        if level.type in ("support", "pivot", "dynamic"):
            if (candle.l < level.price
                    and candle.c > level.price
                    and candle.c > candle.o
                    and lower_wick >= body * 2.5):
                return True
        # Bearish stop hunt: wick swept above resistance, closed below
        if level.type in ("resistance", "pivot", "dynamic"):
            if (candle.h > level.price
                    and candle.c < level.price
                    and candle.c < candle.o
                    and upper_wick >= body * 2.5):
                return True
        return False

    def _is_retest_candle(self, candle, level: Level) -> bool:
        if level.label not in self._broken_levels:
            return False
        body = abs(candle.c - candle.o)
        if body < 0.02:
            return False
        dist_pct = abs(candle.c - level.price) / level.price if level.price > 0 else 1
        if dist_pct > 0.002:
            return False
        lower_wick = min(candle.o, candle.c) - candle.l
        upper_wick = candle.h - max(candle.o, candle.c)
        # Bullish retest: price came back to broken resistance (now support), held above
        if candle.c > level.price and candle.l <= level.price + 0.05 and lower_wick >= body:
            return True
        # Bearish retest: price came back to broken support (now resistance), held below
        if candle.c < level.price and candle.h >= level.price - 0.05 and upper_wick >= body:
            return True
        return False

    def _check_and_record_breakout(self, candle_5m, level: Level) -> bool:
        prev_5m = self.candles.closed_5m[-2] if len(self.candles.closed_5m) >= 2 else None
        if not prev_5m:
            return False
        # Bullish breakout: prev closed below, now closed above
        if prev_5m.c < level.price and candle_5m.c > level.price and candle_5m.c > candle_5m.o:
            self._broken_levels.add(level.label)
            return True
        # Bearish breakout: prev closed above, now closed below
        if prev_5m.c > level.price and candle_5m.c < level.price and candle_5m.c < candle_5m.o:
            self._broken_levels.add(level.label)
            return True
        return False

    def _record_level_test(self, level_name: str, candle, result: str):
        key = level_name.upper()
        if key not in self._level_tests:
            self._level_tests[key] = []
        self._level_tests[key].append({
            "time_et": _get_et_time(),
            "result": result,
            "candle_high": candle.h,
            "candle_low": candle.l,
            "candle_close": candle.c,
        })
        if len(self._level_tests[key]) > 10:
            self._level_tests[key] = self._level_tests[key][-10:]

    def _gates_pass(self, level: Level, current_volume: float, avg_volume: float) -> tuple[bool, str]:
        if not is_trading_allowed():
            return False, "Outside trading hours"
        sess = get_session()
        if sess.quality == 0:
            return False, f"{sess.label} — signals disabled"
        if time.time() - self._last_signal_time < self.cooldown_secs:
            remaining = int(self.cooldown_secs - (time.time() - self._last_signal_time))
            return False, f"Cooldown: {remaining}s remaining"
        last_lvl_time = self._last_level_times.get(level.label, 0)
        if time.time() - last_lvl_time < 600:
            remaining = int(600 - (time.time() - last_lvl_time))
            return False, f"{level.label} analyzed {remaining}s ago"
        if avg_volume > 0 and current_volume < avg_volume * 0.5:
            return False, "Volume too low"
        if self._generating:
            return False, "Agent already running"
        return True, ""

    async def _on_candle_close(self):
        sess = get_session()
        or_data = compute_opening_range(self.candles.c1m)
        levels = build_levels(
            self.candles.closed_1m,
            self.candles.closed_5m,
            self.candles.c_daily,
            self.pm_data,
            or_data,
        )
        self._current_levels = levels
        self.or_data = or_data
        self.state.levels = [self._serialize_level(l) for l in levels]
        self.state.or_high = or_data.high if or_data else 0.0
        self.state.or_low = or_data.low if or_data else 0.0
        self.state.or_complete = or_data.complete if or_data else False
        self.state.session_label = sess.label
        self.state.session_color = sess.color

        price = self.candles.live_price
        if not price:
            return

        await self._broadcast({"type": "state", "data": self._get_push_payload()})

        if not self.candles.closed_1m:
            return

        last_1m = self.candles.closed_1m[-1]
        avg_v = calc_avg_vol(self.candles.closed_1m)

        # Priority 1: Stop hunt scan
        # Priority 2: Rejection scan
        # Priority 3: Retest scan
        # Only one fires per candle close.
        for pattern, checker in [
            ("STOP_HUNT", self._is_stop_hunt_candle),
            ("REJECTION", self._is_rejection_candle),
            ("RETEST", self._is_retest_candle),
        ]:
            triggered = False
            for level in self._current_levels:
                if level.price <= 0:
                    continue
                if not checker(last_1m, level):
                    continue
                if pattern != "RETEST" and avg_v > 0 and last_1m.v < avg_v * 0.8:
                    continue
                passed, reason = self._gates_pass(level, last_1m.v, avg_v)
                if not passed:
                    self._log(f"{pattern} gate: {reason}")
                    continue

                self._log(f"1m {pattern} at {level.label} ${level.price:.2f}", "success")
                self._record_level_test(level.label, last_1m, pattern)
                self._fire_agent(level, pattern)
                triggered = True
                break
            if triggered:
                break

        # Check for 5m candle close
        cur_5m_count = len(self.candles.closed_5m)
        if cur_5m_count > self._last_5m_count and cur_5m_count > 0:
            self._last_5m_count = cur_5m_count
            await self._on_5m_candle_close()

    async def _on_5m_candle_close(self):
        if not self.candles.closed_5m or self._generating:
            return

        last_5m = self.candles.closed_5m[-1]
        avg_v = calc_avg_vol(self.candles.closed_5m)

        for level in self._current_levels:
            if level.price <= 0:
                continue

            # Check breakout first
            if self._check_and_record_breakout(last_5m, level):
                passed, reason = self._gates_pass(level, last_5m.v, avg_v)
                if not passed:
                    self._log(f"BREAKOUT gate: {reason}")
                    continue
                self._log(f"5m BREAKOUT at {level.label} ${level.price:.2f}", "success")
                self._record_level_test(level.label, last_5m, "BREAKOUT")
                self._fire_agent(level, "BREAKOUT")
                return

            # Check touch (wick or body within 0.3% of level)
            dist_hi = abs(last_5m.h - level.price) / level.price if level.price > 0 else 1
            dist_lo = abs(last_5m.l - level.price) / level.price if level.price > 0 else 1
            if min(dist_hi, dist_lo) <= 0.003:
                passed, reason = self._gates_pass(level, last_5m.v, avg_v)
                if not passed:
                    continue
                self._log(f"5m TOUCH at {level.label} ${level.price:.2f}", "info")
                self._record_level_test(level.label, last_5m, "TOUCH")
                self._fire_agent(level, "TOUCH")
                return

    def _fire_agent(self, level: Level, pattern: str):
        self._generating = True
        self._current_level_name = level.label
        self._last_level_times[level.label] = time.time()
        self.state.agent_status = "running"
        self.state.agent_level = level.label
        self.state.stream_text = ""
        asyncio.create_task(self._broadcast({
            "type": "agent_start",
            "pattern": pattern,
            "level": level.label,
            "levelPrice": level.price,
            "time": _get_et_time(),
        }))
        asyncio.create_task(self._run_agent(self.candles.live_price, level, pattern))

    # ── Agent runner ──────────────────────────────────────

    async def _run_agent(self, price: float, level: Level, trigger_pattern: str):
        from .agent.agent import run_agent

        pattern_descriptions = {
            "STOP_HUNT": "STOP HUNT detected — 1m candle swept past level then reversed. Institutions hunted retail stops. Highest probability reversal setup.",
            "REJECTION": "REJECTION detected — 1m candle pushed into level with large wick, body closed on opposite side. Sellers/buyers defending the level.",
            "RETEST": "RETEST detected — previously broken level being retested as new S/R. Price returned to level and held. Classic continuation entry.",
            "BREAKOUT": "BREAKOUT detected — 5m candle closed beyond the level. Directional move confirmed on 5m. Look for retest or continuation.",
            "TOUCH": "LEVEL TOUCH detected — 5m candle interacted with level. Investigate for rejection or breakout.",
        }
        pattern_desc = pattern_descriptions.get(trigger_pattern, trigger_pattern)

        test_history = self._level_tests.get(level.label, [])
        if test_history:
            history_str = f"Tests at {level.label} today ({len(test_history)} total):\n"
            for t in test_history:
                history_str += (
                    f"  {t.get('time_et', '?')} {t.get('result', '?')} "
                    f"H:{t.get('candle_high', 0):.2f} L:{t.get('candle_low', 0):.2f} C:{t.get('candle_close', 0):.2f}\n"
                )
        else:
            history_str = f"First test of {level.label} today — fresh level.\n"

        initial_message = (
            f"SPY ${price:.2f} | {level.label} ${level.price:.2f} "
            f"({level.type}, strength {level.strength}/4) | {_get_et_time()}\n\n"
            f"TRIGGER: {pattern_desc}\n\n"
            f"{history_str}\n"
            f"Use your tools to investigate.\nDecide: LONG, SHORT, or WAIT."
        )

        try:
            async def on_token(token: str):
                self.state.stream_text += token
                await self._broadcast({"type": "signal_token", "token": token})

            async def on_complete(signal_args):
                self._generating = False
                self.state.stream_text = ""
                self.state.agent_status = "idle"

                if signal_args:
                    sig = self._build_signal(signal_args, level)
                    self.state.signal = self._serialize_signal(sig)

                    if sig.direction != "WAIT":
                        self._last_signal_time = time.time()
                        await self._send_telegram(signal_args)
                        self._log(f"Signal: {sig.direction} @ ${sig.entry:.2f} | RR {sig.rr}:1 | {sig.confidence}", "success")
                    else:
                        self._log(f"WAIT: {sig.wait_for or sig.narrative}", "info")

                    self.signal_history.append({
                        "time": _get_et_time(),
                        "direction": sig.direction,
                        "level": level.label,
                        "entry": sig.entry,
                        "confidence": sig.confidence,
                    })
                    if len(self.signal_history) > 20:
                        self.signal_history = self.signal_history[-20:]

                    await self._broadcast({"type": "signal_complete", "signal": self.state.signal})
                else:
                    self._log("Agent finished without signal", "info")
                    await self._broadcast({
                        "type": "signal_complete",
                        "signal": {
                            "direction": "WAIT",
                            "narrative": "Agent completed analysis without a signal",
                            "wait_for": "Next candle close near a key level",
                            "reasoning": "",
                            "confidence": "LOW",
                            "pattern": trigger_pattern,
                            "firedAt": _get_et_time(),
                        },
                    })

            async def on_tool_call(name: str, status: str, args: dict, result: str):
                await self._broadcast({
                    "type": "tool_call", "name": name, "status": status,
                    "args": args, "result": result, "count": len(self.signal_history),
                })

            async def on_error(err: str):
                self._generating = False
                self.state.agent_status = "idle"
                self._log(f"Agent error: {err}", "error")
                await self._broadcast({"type": "signal_error", "error": err})

            await run_agent(
                engine=self,
                initial_message=initial_message,
                openai_key=self._openai_key,
                model=self.model,
                reasoning="high",
                on_token=on_token,
                on_tool_call=on_tool_call,
                on_complete=on_complete,
                on_error=on_error,
            )
        except Exception as e:
            self._generating = False
            self.state.agent_status = "idle"
            self._log(f"Agent exception: {e}", "error")

    def _build_signal(self, args: dict, level: Level) -> Signal:
        return Signal(
            direction=args.get("signal", "WAIT"),
            confidence=args.get("confidence", "LOW"),
            entry=float(args.get("entry", 0)),
            stop=float(args.get("stop", 0)),
            tp1=float(args.get("tp1", 0)),
            tp2=float(args.get("tp2", 0)),
            rr=float(args.get("rr", 0)),
            pattern=args.get("pattern", ""),
            narrative=args.get("narrative", ""),
            reasoning=args.get("reasoning", ""),
            invalidation=args.get("invalidation", ""),
            warnings=args.get("warnings", ""),
            wait_for=args.get("wait_for", ""),
            fired_at=_get_et_time(),
            level_name=level.label,
        )

    # ── Telegram ──────────────────────────────────────────

    async def _send_telegram(self, signal_args: dict):
        direction = signal_args.get("signal", "?")
        confidence = signal_args.get("confidence", "?")
        entry = signal_args.get("entry", 0)
        stop = signal_args.get("stop", 0)
        tp1 = signal_args.get("tp1", 0)
        tp2 = signal_args.get("tp2", 0)
        rr = signal_args.get("rr", 0)
        narrative = signal_args.get("narrative", "")
        invalid = signal_args.get("invalidation", "")

        msg = (
            f"{'🟢' if direction == 'LONG' else '🔴'} SPY {direction} — {confidence}\n"
            f"{_get_et_time()}\n\n"
            f"Entry:  ${entry:.2f}\nStop:   ${stop:.2f}\n"
            f"TP1:    ${tp1:.2f}\nTP2:    ${tp2:.2f}\n"
            f"RR:     {rr:.1f}:1\n\n"
            f"{narrative}\n\nExit if: {invalid}"
        )

        if not self._telegram_token or not self._telegram_chat_id:
            self._log("Telegram not configured — skipping", "warn")
            return

        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{self._telegram_token}/sendMessage"
            async with aiohttp.ClientSession() as s:
                await s.post(url, json={"chat_id": self._telegram_chat_id, "text": msg})
            self._log("Telegram sent", "success")
        except Exception as e:
            self._log(f"Telegram error: {e}", "error")

    # ── Client management ─────────────────────────────────

    async def add_client(self, ws):
        self._clients.add(ws)
        await ws.send_json({"type": "state", "data": self._get_push_payload()})
        self._log(f"Browser connected ({len(self._clients)} clients)")

    async def remove_client(self, ws):
        self._clients.discard(ws)

    async def _broadcast(self, msg: dict):
        if not self._clients:
            return
        dead = set()
        for client in self._clients:
            try:
                await client.send_json(msg)
            except Exception:
                dead.add(client)
        self._clients -= dead

    # ── Logging ───────────────────────────────────────────

    def _log(self, msg: str, level: str = "info"):
        entry = {"msg": msg, "level": level, "time": datetime.datetime.now().strftime("%H:%M:%S")}
        self._log_buf.append(entry)
        if len(self._log_buf) > 50:
            self._log_buf = self._log_buf[-50:]
        self.state.log = self._log_buf[-10:]
        print(f"[SPY] [{level.upper()}] {msg}")

    # ── Serializers ───────────────────────────────────────

    def _serialize_level(self, l: Level) -> dict:
        return {"price": l.price, "label": l.label, "type": l.type, "strength": l.strength, "source": l.source}

    def _serialize_signal(self, s: Signal) -> dict:
        return {
            "direction": s.direction, "confidence": s.confidence,
            "entry": s.entry, "stop": s.stop, "tp1": s.tp1, "tp2": s.tp2, "rr": s.rr,
            "pattern": s.pattern, "narrative": s.narrative, "reasoning": s.reasoning,
            "invalidation": s.invalidation, "warnings": s.warnings, "wait_for": s.wait_for,
            "firedAt": s.fired_at, "levelName": s.level_name,
        }

    def _get_push_payload(self) -> dict:
        return {
            "livePrice": self.state.live_price,
            "cvd": self.state.cvd,
            "cvdBias": self.state.cvd_bias,
            "session": {"label": self.state.session_label, "color": self.state.session_color},
            "vix": self.state.vix,
            "vixLabel": self.state.vix_label,
            "vixColor": self.state.vix_color,
            "signal": self.state.signal,
            "streaming": self.state.agent_status == "running",
            "streamText": self.state.stream_text,
            "agentStatus": self.state.agent_status,
            "agentLevel": self.state.agent_level,
            "orHigh": self.state.or_high,
            "orLow": self.state.or_low,
            "orComplete": self.state.or_complete,
            "levels": self.state.levels,
            "pmData": self.pm_data.__dict__ if self.pm_data else None,
            "c1m": [c.__dict__ for c in self.candles.c1m[-100:]],
            "c5m": [c.__dict__ for c in self.candles.c5m[-100:]],
            "c15m": [c.__dict__ for c in self.candles.c15m[-100:]],
            "log": self.state.log,
            "connected": self.state.connected_to_finnhub,
        }


_engine: SPYEngine | None = None

def get_engine() -> SPYEngine:
    global _engine
    if _engine is None:
        _engine = SPYEngine()
    return _engine
