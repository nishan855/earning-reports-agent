import asyncio
import time
import datetime
import os
from dataclasses import dataclass, field
from typing import Any

from .candles  import CandleStore
from .cvd      import CVDEngine
from .factors  import run_factor_engine
from .signals  import SignalEngine
from .levels   import build_levels, compute_opening_range
from .finnhub  import FinnhubClient
from .sessions import get_session, is_trading_allowed
from .models   import FactorEngineResult, Signal, PreMarketData


@dataclass
class SPYState:
    live_price:      float         = 0.0
    cvd:             float         = 0.0
    cvd_bias:        str           = "NEUTRAL"
    session_label:   str           = "CLOSED"
    session_color:   str           = "#475569"
    vix:             float | None  = None
    vix_label:       str           = ""
    vix_color:       str           = "#475569"
    factor_engine:   Any           = None
    signal:          Any           = None
    signal_streaming:bool          = False
    stream_text:     str           = ""
    or_high:         float         = 0.0
    or_low:          float         = 0.0
    or_complete:     bool          = False
    levels:          list          = field(default_factory=list)
    pm_data:         Any           = None
    c1m:             list          = field(default_factory=list)
    c5m:             list          = field(default_factory=list)
    c15m:            list          = field(default_factory=list)
    log:             list          = field(default_factory=list)
    connected_to_finnhub: bool     = False


class SPYEngine:
    SYMBOL = "SPY"

    def __init__(self):
        self._finnhub_key = os.environ.get("FINNHUB_KEY", "")
        self._openai_key  = os.environ.get("OPENAI_KEY", "")

        self.candles      = CandleStore()
        self.cvd          = CVDEngine()
        self.signals      = SignalEngine(self._openai_key)
        self.finnhub      = FinnhubClient(self._finnhub_key)
        self.state        = SPYState()
        self.pm_data:     PreMarketData | None = None
        self.vix_val:     float | None = None

        self._clients:    set = set()
        self._log_buf:    list[dict] = []

        self.candles.on_candle_close(self._on_candle_close)

    async def start(self):
        if not self._finnhub_key:
            self._log("FINNHUB_KEY not set in .env", "error")
            return
        if not self._openai_key:
            self._log("OPENAI_KEY not set in .env", "error")
            return

        self._log("SPY Engine starting...")
        await self._load_initial_data()
        asyncio.create_task(self._run_finnhub_ws())
        asyncio.create_task(self._run_vix_poller())
        asyncio.create_task(self._run_bar_poller())

    async def _load_initial_data(self):
        now = int(time.time())

        self._log("Loading historical bars...")

        # Batch 1
        c_daily, c15m = await asyncio.gather(
            self.finnhub.fetch_bars(self.SYMBOL, "D",  now - 86400*90,  now),
            self.finnhub.fetch_bars(self.SYMBOL, "15", now - 86400*5,   now),
        )
        await asyncio.sleep(1)

        # Batch 2
        c5m, c1m = await asyncio.gather(
            self.finnhub.fetch_bars(self.SYMBOL, "5",  now - 86400*3,   now),
            self.finnhub.fetch_bars(self.SYMBOL, "1",  now - 3600*6,    now),
        )
        await asyncio.sleep(1)

        # Batch 3
        pm5m = await self.finnhub.fetch_bars(self.SYMBOL, "5", now - 3600*10, now)

        self.candles.load(c1m, c5m, c15m, c_daily)
        self._log(f"Bars: {len(c1m)}x1m  {len(c5m)}x5m  {len(c15m)}x15m  {len(c_daily)}xdaily", "success")

        if len(c_daily) >= 2:
            prev  = c_daily[-2]
            today = c_daily[-1]
            gap   = (today.o - prev.c) / prev.c * 100
            pm_h  = max(c.h for c in pm5m) if pm5m else today.o
            pm_l  = min(c.l for c in pm5m) if pm5m else today.o
            self.pm_data = PreMarketData(
                pd_high=prev.h, pd_low=prev.l, pd_close=prev.c,
                pm_high=pm_h, pm_low=pm_l,
                gap_pct=round(gap, 2),
                gap_type="GAP UP" if gap > 0.2 else "GAP DOWN" if gap < -0.2 else "FLAT",
                gap_fill=prev.c,
            )
            self._log(f"Pre-market: {self.pm_data.gap_type} {gap:+.2f}% | PDH ${prev.h:.2f} PDL ${prev.l:.2f}", "success")

        self.vix_val = await self.finnhub.fetch_vix()
        if self.vix_val:
            self._log(f"VIX: {self.vix_val:.1f}", "info")

        await self._on_candle_close()

    async def _run_finnhub_ws(self):
        async def on_trade(price: float, volume: float, ts: int):
            self.cvd.process_trade(price, volume)
            self.candles.update_live(price, volume, ts)
            self.state.live_price = price
            self.state.cvd        = self.cvd.value
            self.state.cvd_bias   = self.cvd.bias
            await self._broadcast({"type": "tick", "price": price, "cvd": self.cvd.value})

        self.state.connected_to_finnhub = True
        await self.finnhub.connect_ws(self.SYMBOL, on_trade)

    async def _run_vix_poller(self):
        while True:
            await asyncio.sleep(30)
            vix = await self.finnhub.fetch_vix()
            if vix:
                self.vix_val   = vix
                self.state.vix = vix

    async def _run_bar_poller(self):
        while True:
            await asyncio.sleep(60)
            now = int(time.time())
            try:
                c1m, c5m, c15m = await asyncio.gather(
                    self.finnhub.fetch_bars(self.SYMBOL, "1",  now - 3600*4,  now),
                    self.finnhub.fetch_bars(self.SYMBOL, "5",  now - 86400*2, now),
                    self.finnhub.fetch_bars(self.SYMBOL, "15", now - 86400*3, now),
                )
                if c1m: self.candles.c1m  = c1m
                if c5m: self.candles.c5m  = c5m
                if c15m: self.candles.c15m = c15m
            except Exception as e:
                self._log(f"Bar poll error: {e}", "error")

    async def _on_candle_close(self):
        sess    = get_session()
        or_data = compute_opening_range(self.candles.c1m)
        levels  = build_levels(
            self.candles.closed_1m,
            self.candles.closed_5m,
            self.candles.c_daily,
            self.pm_data,
            or_data,
        )

        engine = run_factor_engine(
            closed_1m  = self.candles.closed_1m,
            closed_5m  = self.candles.closed_5m,
            closed_15m = self.candles.closed_15m,
            c_daily    = self.candles.c_daily,
            cvd        = self.cvd.value,
            vix_val    = self.vix_val,
            or_data    = or_data,
            pm_data    = self.pm_data,
            levels     = levels,
        )

        if not engine:
            return

        self._log(f"Candle close — Score {engine.total_score}/9 | Bias {engine.bias.value} | {'SIGNAL CONDITIONS MET' if engine.all_ok else f'{9-engine.total_score} away'}")

        self.state.factor_engine = self._serialize_engine(engine)
        self.state.levels        = [self._serialize_level(l) for l in levels]
        self.state.or_high       = or_data.high if or_data else 0.0
        self.state.or_low        = or_data.low  if or_data else 0.0
        self.state.or_complete   = or_data.complete if or_data else False
        self.state.session_label = sess.label
        self.state.session_color = sess.color

        await self._broadcast({"type": "state", "data": self._get_push_payload()})

        if engine.all_ok and is_trading_allowed():
            asyncio.create_task(self._generate_signal(engine))

    async def _generate_signal(self, engine):
        self._log("Conditions met -> GPT-5.4 analyzing...", "success")
        self.state.signal_streaming = True
        self.state.stream_text      = ""

        async def on_token(token: str):
            self.state.stream_text += token
            await self._broadcast({"type": "signal_token", "token": token})

        async def on_complete(sig: Signal):
            self.state.signal           = self._serialize_signal(sig)
            self.state.signal_streaming = False
            self.state.stream_text      = ""
            await self._broadcast({"type": "signal_complete", "signal": self.state.signal})
            self._log(f"Signal: {sig.direction} @ ${sig.entry:.2f} | RR {sig.rr}:1 | {sig.confidence}", "success")

        async def on_error(err: str):
            self.state.signal_streaming = False
            self._log(f"Signal error: {err}", "error")
            await self._broadcast({"type": "signal_error", "error": err})

        await self.signals.generate(engine, self.pm_data, on_token, on_complete, on_error)

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
            except:
                dead.add(client)
        self._clients -= dead

    def _log(self, msg: str, level: str = "info"):
        entry = {
            "msg":   msg,
            "level": level,
            "time":  datetime.datetime.now().strftime("%H:%M:%S"),
        }
        self._log_buf.append(entry)
        if len(self._log_buf) > 50:
            self._log_buf = self._log_buf[-50:]
        self.state.log = self._log_buf[-10:]
        print(f"[SPY] [{level.upper()}] {msg}")

    def _serialize_engine(self, e) -> dict:
        return {
            "factors":       [{"id":f.id,"layer":f.layer,"label":f.label,"ok":f.ok,"isBonus":f.is_bonus,"val":f.val,"color":f.color,"reason":f.reason,"missing":f.missing} for f in e.factors],
            "contextScore":  e.context_score,
            "setupScore":    e.setup_score,
            "timingScore":   e.timing_score,
            "totalScore":    e.total_score,
            "allOk":         e.all_ok,
            "bias":          e.bias.value,
            "vwap":          e.vwap,
            "atr":           e.atr,
            "rr":            e.rr,
            "sl":            e.sl,
            "tp1":           e.tp1,
            "tp2":           e.tp2,
            "lastPrice":     e.last_price,
            "orData":        {"high":e.or_data.high,"low":e.or_data.low,"complete":e.or_data.complete} if e.or_data else None,
            "priceAction":   {"type":e.price_action.type,"level":e.price_action.level.label,"price":e.price_action.level.price} if e.price_action else None,
            "stopHunt":      {"type":e.stop_hunt.type,"level":e.stop_hunt.level} if e.stop_hunt else None,
        }

    def _serialize_level(self, l) -> dict:
        return {"price":l.price,"label":l.label,"type":l.type,"strength":l.strength,"source":l.source}

    def _serialize_signal(self, s: Signal) -> dict:
        return {
            "direction":s.direction,"confidence":s.confidence,"entryType":s.entry_type,
            "entry":s.entry,"stopLoss":s.stop_loss,"tp1":s.tp1,"tp2":s.tp2,"rr":s.rr,
            "narrative":s.narrative,"reasoning":s.reasoning,"invalidation":s.invalidation,
            "sizeNote":s.size_note,"keyRisk":s.key_risk,"streamComplete":s.stream_complete,
        }

    def _get_push_payload(self) -> dict:
        return {
            "livePrice":    self.state.live_price,
            "cvd":          self.state.cvd,
            "cvdBias":      self.state.cvd_bias,
            "session":      {"label":self.state.session_label,"color":self.state.session_color},
            "vix":          self.state.vix,
            "vixLabel":     self.state.vix_label,
            "factorEngine": self.state.factor_engine,
            "signal":       self.state.signal,
            "streaming":    self.state.signal_streaming,
            "streamText":   self.state.stream_text,
            "orHigh":       self.state.or_high,
            "orLow":        self.state.or_low,
            "orComplete":   self.state.or_complete,
            "levels":       self.state.levels,
            "pmData":       self.pm_data.__dict__ if self.pm_data else None,
            "c1m":          [c.__dict__ for c in self.candles.c1m[-100:]],
            "c5m":          [c.__dict__ for c in self.candles.c5m[-100:]],
            "c15m":         [c.__dict__ for c in self.candles.c15m[-100:]],
            "log":          self.state.log,
            "connected":    self.state.connected_to_finnhub,
        }


_engine: SPYEngine | None = None

def get_engine() -> SPYEngine:
    global _engine
    if _engine is None:
        _engine = SPYEngine()
    return _engine
