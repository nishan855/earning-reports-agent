from datetime import datetime, timedelta
import math
import pytz
from ..constants import ASSETS, MIN_RR, MAX_SIGNALS_PER_ASSET, VIX_HARD_BLOCK, CUTOFF_HOUR, CUTOFF_MIN
from ..models import Candle, Level, Signal
from ..context.sim_clock import now_et

ET = pytz.timezone("America/New_York")


class ToolHandler:
    def __init__(self, candle_store, cvd_engine, level_store, vol_profiles, day_contexts, signal_history, tracker_engine, vix_value, current_setup):
        self._candles = candle_store
        self._cvd = cvd_engine
        self._levels = level_store
        self._vol_profiles = vol_profiles
        self._day_contexts = day_contexts
        self._signal_history = signal_history
        self._tracker = tracker_engine
        self._vix = vix_value
        self._setup = current_setup
        self._last_signal = None

    def verify_setup(self, asset: str) -> str:
        """Single call that returns candles + CVD + trend for independent verification."""
        if asset not in ASSETS:
            return f"Unknown asset: {asset}"
        parts = []

        # 5m candles
        parts.append(self.get_candles(asset, "5m", 10))

        # CVD
        parts.append(self.get_cvd(asset))

        # Trend
        parts.append(self.get_trend(asset))

        # Volume profile summary
        vp = self._vol_profiles.get(asset)
        if vp:
            price = self._candles.get(asset).live_price
            pos = "ABOVE" if price > vp.poc else "BELOW"
            va = "INSIDE VA" if vp.val <= price <= vp.vah else "ABOVE VA" if price > vp.vah else "BELOW VA"
            parts.append(f"VOL PROFILE — {asset}: POC ${vp.poc:.2f} ({pos}), {va}, VAH ${vp.vah:.2f}, VAL ${vp.val:.2f}")

        result = "\n\n".join(parts)
        return result[:2000] if len(result) > 2000 else result

    def get_candles(self, asset: str, timeframe: str, count: int = 10) -> str:
        if asset not in ASSETS:
            return f"Unknown asset: {asset}"
        store = self._candles.get(asset)
        if timeframe == "1m": bars = store.closed_1m[-count:]
        elif timeframe == "5m": bars = store.closed_5m[-count:]
        elif timeframe == "15m": bars = store.closed_15m[-count:]
        elif timeframe == "daily": bars = store.c_daily[-count:]
        else: return f"Unknown timeframe: {timeframe}"
        if not bars:
            return f"No {timeframe} bars for {asset}"
        avg_v = sum(c.v for c in bars) / len(bars) if bars else 0
        lines = [f"{asset} {timeframe} (last {len(bars)}):"]
        for c in bars:
            dt = datetime.fromtimestamp(c.t / 1000, tz=ET)
            color = "GREEN" if c.is_bullish else "RED"
            vr = f"{c.v/avg_v:.1f}x" if avg_v > 0 else "-"
            wr = f"{c.wick_body_ratio:.1f}x" if c.body > 0.01 else "DOJI"
            lines.append(f"  {dt.strftime('%H:%M')} {color:5} O:{c.o:.2f} H:{c.h:.2f} L:{c.l:.2f} C:{c.c:.2f} vol:{vr} wick/body:{wr}")
        result = "\n".join(lines)
        return result[:2000] if len(result) > 2000 else result

    def get_cvd(self, asset: str, minutes: int = 15) -> str:
        if asset not in ASSETS:
            return f"Unknown asset: {asset}"
        eng = self._cvd.get(asset)
        hist = eng.get_history(minutes)
        rolling_avg = eng.rolling_avg_cvd_turn(10)

        # Bias classification
        bias = eng.bias
        if bias == "BUYERS":
            bias_label = "BUYERS_AGGRESSIVE" if rolling_avg > 0 and abs(eng.value) > rolling_avg * 2 else "BUYERS"
        elif bias == "SELLERS":
            bias_label = "SELLERS_AGGRESSIVE" if rolling_avg > 0 and abs(eng.value) > rolling_avg * 2 else "SELLERS"
        else:
            bias_label = "NEUTRAL"

        # Direction from recent history
        if len(hist) >= 3:
            direction = "RISING" if hist[-1].value > hist[-3].value else "FALLING" if hist[-1].value < hist[-3].value else "FLAT"
            turned_this_bar = len(hist) >= 2 and (
                (hist[-1].delta > 0 and hist[-2].delta < 0) or
                (hist[-1].delta < 0 and hist[-2].delta > 0)
            )
            if turned_this_bar:
                direction += " (turned this bar)"
        else:
            direction = "INSUFFICIENT DATA"

        # Current bar ratio vs rolling average
        last_turn = abs(hist[-1].delta) if hist else 0
        ratio = last_turn / rolling_avg if rolling_avg > 0 else 0

        lines = [
            f"{asset} CVD:",
            f"  CVD RATIO:     {ratio:.1f}× rolling average",
            f"  CVD BIAS:      {bias_label}",
            f"  CVD DIRECTION: {direction}",
        ]

        if eng.is_estimated:
            lines.append(f"  ⚠ CVD QUARANTINED — data unreliable after WS reconnect")

        # Divergence detection
        store = self._candles.get(asset)
        if store and store.closed_1m:
            div = eng.detect_divergence(store.closed_1m)
            if div["type"] != "NONE":
                lines.append(f"  DIVERGENCE: {div['type']} — {div['detail']}")
            else:
                lines.append(f"  No divergence — CVD confirms price")
        result = "\n".join(lines)
        return result[:2000] if len(result) > 2000 else result

    def get_setup_context(self, asset: str) -> str:
        s = self._setup
        if not s or s.get("asset") != asset:
            return f"No active setup for {asset}"
        lines = [f"SETUP — {asset}", f"  Pattern: {s.get('pattern')}", f"  Direction: {s.get('direction')}", f"  Level: {s.get('level_name')} ${s.get('level_price',0):.2f}", f"  Score: {s.get('level_score',0)}/10"]
        ec = s.get("event_candle")
        if ec:
            lines.append(f"  Candle: O:{ec.o:.2f} H:{ec.h:.2f} L:{ec.l:.2f} C:{ec.c:.2f}")
            lines.append(f"  Volume: {s.get('volume_ratio',0):.1f}x avg")
        result = "\n".join(lines)
        return result[:2000] if len(result) > 2000 else result

    def get_level_info(self, asset: str, level_name: str) -> str:
        levels = self._levels.get(asset, [])
        level = next((l for l in levels if l.name == level_name), None)
        if not level:
            return f"Level {level_name} not found for {asset}. Available: {', '.join(l.name for l in levels[:10])}"
        lines = [f"LEVEL — {asset} {level.name}", f"  Price: ${level.price:.2f}", f"  Score: {level.score}/10", f"  Type: {level.type} | Source: {level.source}", f"  {level.description}"]
        if level.confluence_with:
            lines.append(f"  CONFLUENCE: {', '.join(level.confluence_with)} — score boosted")
        lines.append(f"  Tests today: {level.tests_today}" + (" (fresh — strong reaction expected)" if level.tests_today == 0 else ""))
        result = "\n".join(lines)
        return result[:2000] if len(result) > 2000 else result

    def get_level_map(self, asset: str) -> str:
        levels = self._levels.get(asset, [])
        if not levels:
            return f"No levels for {asset}"
        store = self._candles.get(asset)
        price = store.live_price
        atr = _calc_atr(store.closed_1m[-20:]) if store.closed_1m else 1.0
        above = sorted([l for l in levels if l.price > price], key=lambda l: l.price)[:8]
        below = sorted([l for l in levels if l.price <= price], key=lambda l: l.price, reverse=True)[:8]
        lines = [f"LEVELS — {asset} @ ${price:.2f} ATR ${atr:.2f}", "", "  RESISTANCE:"]
        for l in above:
            d = l.price - price
            conf = " *CONF" if l.confluence_with else ""
            lines.append(f"    {l.name:8} ${l.price:.2f}  +${d:.2f} ({d/atr:.1f}x ATR) score:{l.score}{conf}")
        lines.append(f"\n  ── ${price:.2f} PRICE ──\n  SUPPORT:")
        for l in below:
            d = price - l.price
            conf = " *CONF" if l.confluence_with else ""
            lines.append(f"    {l.name:8} ${l.price:.2f}  -${d:.2f} ({d/atr:.1f}x ATR) score:{l.score}{conf}")
        result = "\n".join(lines)
        return result[:2000] if len(result) > 2000 else result

    def get_volume_profile(self, asset: str) -> str:
        vp = self._vol_profiles.get(asset)
        if not vp:
            return f"Volume profile not computed for {asset}"
        price = self._candles.get(asset).live_price
        pos = "ABOVE" if price > vp.poc else "BELOW"
        va = "INSIDE VALUE AREA" if vp.val <= price <= vp.vah else "ABOVE VA" if price > vp.vah else "BELOW VA"
        lines = [f"VOLUME PROFILE — {asset}", f"  POC: ${vp.poc:.2f} (strongest magnet)", f"  VAH: ${vp.vah:.2f} | VAL: ${vp.val:.2f}", f"  Price {pos} POC, {va}"]
        if vp.hvn_list:
            lines.append("  HVN targets: " + ", ".join(f"${h:.2f}" for h in vp.hvn_list[:3]))
        result = "\n".join(lines)
        return result[:2000] if len(result) > 2000 else result

    def get_trend(self, asset: str) -> str:
        store = self._candles.get(asset)
        daily = store.c_daily
        b15 = store.closed_15m
        lines = [f"TREND — {asset}"]
        if len(daily) >= 5:
            h = [c.h for c in daily[-5:]]; l = [c.l for c in daily[-5:]]
            if h[-1] > h[-3] and l[-1] > l[-3]: lines.append("  Daily: BULLISH")
            elif h[-1] < h[-3] and l[-1] < l[-3]: lines.append("  Daily: BEARISH")
            else: lines.append("  Daily: NEUTRAL")
        if len(b15) >= 6:
            if b15[-1].c > b15[0].c and b15[-1].l > b15[1].l: lines.append("  15m: BULLISH")
            elif b15[-1].c < b15[0].c and b15[-1].h < b15[1].h: lines.append("  15m: BEARISH")
            else: lines.append("  15m: NEUTRAL")
        result = "\n".join(lines)
        return result[:2000] if len(result) > 2000 else result

    def get_day_context(self, asset: str) -> str:
        dc = self._day_contexts.get(asset)
        if not dc:
            return f"No day context for {asset}"
        lines = [f"DAY — {asset}", f"  Type: {dc.day_type} | Bias: {dc.bias}", f"  Gap: {dc.gap_type} {dc.gap_pct:+.2f}% | Filled: {dc.gap_filled}"]
        if dc.or_complete:
            lines.append(f"  OR: ${dc.or_high:.2f} / ${dc.or_low:.2f}")
        if dc.relative_str != 0:
            lines.append(f"  Relative strength: {dc.relative_str:+.1f}%")
        result = "\n".join(lines)
        return result[:2000] if len(result) > 2000 else result

    def get_options_context(self, asset: str) -> str:
        vix = self._vix
        now = now_et()
        price = self._candles.get(asset).live_price if self._candles.get(asset) else 0
        if vix >= VIX_HARD_BLOCK:
            return f"VIX {vix:.1f} — HARD BLOCK. No signals allowed."
        if vix < 20: env, size, inst = "NORMAL", "FULL", "ATM outright"
        elif vix < 25: env, size, inst = "ELEVATED", "FULL", "ATM or 1 OTM"
        elif vix < 30: env, size, inst = "HIGH", "HALF", "debit spread"
        else: env, size, inst = "VERY HIGH", "QUARTER", "spread only"

        from ..core.asset_registry import get_config, has_daily_expiry
        cfg = get_config(asset)
        minor = cfg["round_interval_minor"]
        atm = round(round(price / minor) * minor, 2) if price > 0 else 0

        hour = now.hour + now.minute / 60.0
        daily_exp = has_daily_expiry(asset)
        if hour < 11: dte = 0 if daily_exp and now.weekday() == 4 else 1
        elif hour < 13: dte = 1 if daily_exp else 2
        elif hour < 14.5: dte = 0 if daily_exp else 1
        else: dte = 0
        exp_dt = now + timedelta(days=dte)

        daily_move = (price * (vix / 100)) / math.sqrt(252) if price > 0 and vix > 0 else 0
        prem = daily_move * 0.4

        lines = [f"OPTIONS — {asset}", f"  VIX: {vix:.1f} ({env})", f"  Size: {size} | Instrument: {inst}",
                 f"  ATM strike: ${atm:.2f}", f"  DTE: {dte} | Expiry: {exp_dt.strftime('%b %d')}"]
        if prem > 0:
            lines.append(f"  Est premium: ${prem*0.8:.2f}-${prem*1.2:.2f}")
            lines.append(f"  Break-even: ${price+prem:.2f} (calls) / ${price-prem:.2f} (puts)")
        if hour > 14.5:
            lines.append("  WARNING: Late session — theta decay rapid. TP1 only.")
        result = "\n".join(lines)
        return result[:2000] if len(result) > 2000 else result

    def get_session(self) -> str:
        now = now_et()
        t = now.hour * 60 + now.minute
        if t < 600: name, q = "OR FORMING", 0
        elif t < 660: name, q = "POWER HOUR", 5
        elif t < 720: name, q = "MID MORNING", 4
        elif t < 840: name, q = "DEAD ZONE", 1
        elif t < 930: name, q = "AFTERNOON", 4
        elif t < 945: name, q = "POWER CLOSE", 3
        elif t < 960: name, q = "HARD CUTOFF", 0
        else: name, q = "CLOSED", 0
        cutoff_t = CUTOFF_HOUR * 60 + CUTOFF_MIN
        return f"SESSION: {name} | Quality: {q}/5 | To cutoff: {max(0, cutoff_t - t)} min | To close: {max(0, 960 - t)} min"

    def get_signal_history(self, asset: str) -> str:
        sigs = self._signal_history if asset == "ALL" else [s for s in self._signal_history if s.asset == asset]
        sigs = sigs[-200:]
        if not sigs:
            return f"No signals today for {asset}"
        lines = [f"SIGNALS — {asset}"]
        for s in sigs:
            lines.append(f"  {s.fired_at} {s.asset} {s.direction} {s.pattern} @ ${s.entry:.2f} conf:{s.confidence}")
        count = len([s for s in sigs if s.direction in ("LONG", "SHORT")])
        lines.append(f"  Budget: {count}/{MAX_SIGNALS_PER_ASSET} used")
        result = "\n".join(lines)
        return result[:2000] if len(result) > 2000 else result

    def calculate_rr(self, entry: float, stop: float, target: float) -> str:
        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk <= 0:
            return "Invalid: risk is zero."
        rr = reward / risk
        ok = rr >= MIN_RR
        lines = [f"RR: {rr:.1f}:1 | Entry ${entry:.2f} Stop ${stop:.2f} Target ${target:.2f}", f"Risk ${risk:.2f} Reward ${reward:.2f}"]
        if ok:
            lines.append(f"ACCEPTABLE — meets {MIN_RR}:1 minimum")
        else:
            direction = 1 if target > entry else -1
            min_target = entry + risk * MIN_RR * direction
            lines.append(f"INSUFFICIENT — need {MIN_RR}:1. Min target: ${min_target:.2f}")
        result = "\n".join(lines)
        return result[:2000] if len(result) > 2000 else result

    def send_signal(self, signal: str, confidence: int = 0, narrative: str = "", reasoning: str = "", invalidation: str = "", wait_for: str = "",
                    asset: str = "", setup_type: str = "", entry: float = 0, stop: float = 0, tp1: float = 0, tp2: float = 0, rr: float = 0,
                    option_type: str = "", strike: float = 0, expiry_date: str = "", dte: int = 0, size: str = "", est_premium_lo: float = 0,
                    est_premium_hi: float = 0, breakeven: float = 0, instrument: str = "", warnings: str = "") -> str:
        now = now_et()
        # Auto-fill from setup context — agent only needs to provide decision fields
        s = self._setup
        asset = asset or s.get("asset", "")
        setup_type = setup_type or s.get("pattern", "")
        # Confidence: normalize to both percentage (int) and label (str)
        # Agent may send int (0-100) or legacy string ("HIGH"/"MEDIUM"/"LOW")
        if isinstance(confidence, str):
            conf_pct = {"HIGH": 90, "MEDIUM": 65, "LOW": 30}.get(confidence, 50)
            conf_label = confidence if confidence in ("HIGH", "MEDIUM", "LOW") else "MEDIUM"
        elif isinstance(confidence, (int, float)) and confidence > 0:
            conf_pct = int(confidence)
            conf_label = "HIGH" if conf_pct >= 80 else "MEDIUM" if conf_pct >= 50 else "LOW"
        else:
            conf_pct = 50
            conf_label = "MEDIUM"
        # Auto-fill trade fields from pre-computed brief context if agent didn't override
        entry = entry or s.get("entry", 0)
        stop = stop or s.get("stop", 0)
        tp1 = tp1 or s.get("tp1", 0)
        tp2 = tp2 or s.get("tp2", 0)
        rr = rr or s.get("rr", 0)
        option_type = option_type or s.get("option_type", "")
        strike = strike or s.get("strike", 0)
        expiry_date = expiry_date or s.get("expiry_date", "")
        dte = dte or s.get("dte", 0)
        size = size or s.get("size", "FULL")
        est_premium_lo = est_premium_lo or s.get("est_premium_lo", 0)
        est_premium_hi = est_premium_hi or s.get("est_premium_hi", 0)
        breakeven = breakeven or s.get("breakeven", 0)
        instrument = instrument or s.get("instrument", "")
        approach_type = s.get("approach_type", "")
        sig = Signal(
            asset=asset, direction=signal, confidence=conf_label, confidence_pct=conf_pct,
            approach_type=approach_type, pattern=setup_type,
            level_name=s.get("level_name", ""), level_price=s.get("level_price", 0),
            entry=entry, stop=stop, tp1=tp1, tp2=tp2, rr=rr, option_type=option_type, strike=strike,
            expiry_date=expiry_date, dte=dte, size=size, est_premium_lo=est_premium_lo,
            est_premium_hi=est_premium_hi, breakeven=breakeven, instrument=instrument,
            narrative=narrative, reasoning=reasoning, invalidation=invalidation,
            warnings=warnings, wait_for=wait_for, fired_at=now.strftime("%H:%M:%S"),
            session=s.get("session", ""), vix_at_signal=self._vix,
        )
        self._signal_history.append(sig)
        if len(self._signal_history) > 200:
            self._signal_history = self._signal_history[-200:]
        self._last_signal = sig
        if signal in ("LONG", "SHORT"):
            return f"Signal: {asset} {signal} {confidence} @ ${entry:.2f} Stop ${stop:.2f} TP1 ${tp1:.2f} RR {rr:.1f}:1 | {option_type} ${strike:.2f} exp {expiry_date}"
        return f"WAIT: {asset} — {wait_for}"

    def get_last_signal(self) -> Signal | None:
        return self._last_signal


def _calc_atr(candles: list, period: int = 14) -> float:
    if len(candles) < 2:
        return 1.0
    trs = []
    for i in range(1, min(period + 1, len(candles))):
        tr = max(candles[i].h - candles[i].l, abs(candles[i].h - candles[i-1].c), abs(candles[i].l - candles[i-1].c))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 1.0


TOOL_DEFINITIONS = [
    # Primary tool — final decision (trade fields auto-filled from Brief context)
    {"type": "function", "function": {"name": "send_signal", "description": "Your verdict. Trade fields (entry/stop/options) are auto-filled from the Brief. You provide only the decision.", "parameters": {"type": "object", "properties": {"signal": {"type": "string", "enum": ["LONG","SHORT","WAIT"]}, "confidence": {"type": "integer", "description": "0-100. 80+=HIGH, 50-79=MEDIUM, <50=LOW"}, "narrative": {"type": "string", "description": "1-2 sentence thesis"}, "reasoning": {"type": "string", "description": "What the tape showed you (LONG/SHORT) or why you passed (WAIT)"}, "invalidation": {"type": "string", "description": "Exact exit condition, e.g. '1m close below POC $639.50'. Use 'N/A' for WAIT."}, "wait_for": {"type": "string", "description": "WAIT only: exact condition to re-engage"}}, "required": ["signal","confidence","narrative","reasoning","invalidation"]}}},
    # Deep-dive tools — use only if the Brief + Verification Data raises questions
    {"type": "function", "function": {"name": "get_candles", "description": "Get candles for a specific timeframe (1m/5m/15m/daily).", "parameters": {"type": "object", "properties": {"asset": {"type": "string", "enum": ASSETS}, "timeframe": {"type": "string", "enum": ["1m","5m","15m","daily"]}, "count": {"type": "integer", "default": 10}}, "required": ["asset","timeframe"]}}},
    {"type": "function", "function": {"name": "get_cvd", "description": "CVD ratio (vs rolling average), bias (BUYERS/SELLERS/NEUTRAL), direction (RISING/FALLING/FLAT), turn detection, and divergence check. No raw values — ratios only.", "parameters": {"type": "object", "properties": {"asset": {"type": "string", "enum": ASSETS}, "minutes": {"type": "integer", "default": 15}}, "required": ["asset"]}}},
    {"type": "function", "function": {"name": "get_level_info", "description": "Deep dive on a specific level — score, history, confluence.", "parameters": {"type": "object", "properties": {"asset": {"type": "string", "enum": ASSETS}, "level_name": {"type": "string"}}, "required": ["asset","level_name"]}}},
    {"type": "function", "function": {"name": "get_level_map", "description": "All levels above/below price with ATR distances.", "parameters": {"type": "object", "properties": {"asset": {"type": "string", "enum": ASSETS}}, "required": ["asset"]}}},
    {"type": "function", "function": {"name": "calculate_rr", "description": "Recalculate RR with different entry/stop/target.", "parameters": {"type": "object", "properties": {"entry": {"type": "number"}, "stop": {"type": "number"}, "target": {"type": "number"}}, "required": ["entry","stop","target"]}}},
    {"type": "function", "function": {"name": "get_signal_history", "description": "Signals fired today + budget check.", "parameters": {"type": "object", "properties": {"asset": {"type": "string"}}, "required": ["asset"]}}},
]
