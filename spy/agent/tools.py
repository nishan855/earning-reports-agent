import pytz
from datetime import datetime

ET = pytz.timezone("America/New_York")

LEVEL_CONTEXT = {
    "PDH": "Previous Day High — where sellers defended yesterday. Institutional memory. Strong resistance. Break above = bullish continuation.",
    "PDL": "Previous Day Low — where buyers defended yesterday. Strong support. Break below = bearish continuation.",
    "PDC": "Previous Day Close — gap fill level. Price returning here closes today's gap. Often causes pause or reversal.",
    "VWAP": "Volume Weighted Average Price — institutional benchmark. Above = institutions net long. Below = net short. Most important intraday level.",
    "ORH": "Opening Range High — highest price in first 30 min. Break above = bulls won the morning. Strong momentum signal.",
    "ORL": "Opening Range Low — lowest price in first 30 min. Break below = bears won the morning. Strong momentum signal.",
    "PWH": "Previous Week High — weekly resistance. Institutions plan around weekly levels. Strong reaction zone.",
    "PWL": "Previous Week Low — weekly support. Strong institutional memory.",
    "PM HIGH": "Pre-Market High — resistance from pre-market trading. Relevant on gap days.",
    "PM LOW": "Pre-Market Low — support from pre-market trading. Gap day level.",
    "PD": "Previous day level — institutional memory from yesterday's session.",
    "OR": "Opening range level — defines morning direction.",
    "SWING": "Swing point — recent price structure. Minor level, use as target not trigger.",
}

STRENGTH_CONTEXT = {
    4: "Strong institutional level — PDH/PDL/OR class. High probability reaction.",
    3: "Moderate level — VWAP/weekly. Good reaction but less reliable alone.",
    2: "Minor level — swing point. Use as target not entry trigger.",
    1: "Weak level — noise. Ignore for signals.",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_candles",
            "description": "Get recent closed candles. Use this to identify price action at the level. Look for: rejection wicks, retest holds, breakout candles, momentum, volume spikes. Always call this first when near a level.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timeframe": {"type": "string", "enum": ["1m", "5m", "15m"], "description": "1m for entry timing, 5m for confirmation, 15m for trend"},
                    "count": {"type": "integer", "description": "Number of candles (max 20)", "minimum": 3, "maximum": 20}
                },
                "required": ["timeframe", "count"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_cvd",
            "description": "Get Cumulative Volume Delta history. CVD tells you if buyers or sellers are actually aggressive. Rising CVD = buyers hitting ask. Falling CVD = sellers hitting bid. Check CVD change on the signal candle specifically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer", "description": "Minutes of CVD history (max 30)", "minimum": 5, "maximum": 30}
                },
                "required": ["minutes"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_level_info",
            "description": "Get detailed info about a specific key level. Returns: exact price, strength, type, source, distance from current price.",
            "parameters": {
                "type": "object",
                "properties": {
                    "level_name": {"type": "string", "description": "Level label e.g. PDH, PDL, VWAP, ORH, ORL, PM High, PM Low"}
                },
                "required": ["level_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_all_levels",
            "description": "Get the complete levels map. Returns all key levels above and below price, their strength, type, and distance. Use to find targets and understand support/resistance.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_trend",
            "description": "Get current trend direction. Returns 15m trend (primary bias) and 5m trend (intraday momentum). Also returns VWAP position. Only trade in direction of 15m trend.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_day_character",
            "description": "Get today's market character. Returns: gap size and type, volume vs average, day range, VWAP position, day type (trending/ranging).",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_session",
            "description": "Get current session details. Returns: session name, quality, minutes remaining. Use to set appropriate targets — less time means tighter targets.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_signal_history",
            "description": "Get all signals fired today. Returns: time, direction, level, entry price. Use to avoid repeating same signal.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_rr",
            "description": "Calculate risk/reward for a proposed trade. Must be called before send_signal. Minimum acceptable RR is 2.0.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entry": {"type": "number", "description": "Proposed entry price"},
                    "stop": {"type": "number", "description": "Proposed stop loss price"},
                    "target": {"type": "number", "description": "Proposed target price"}
                },
                "required": ["entry", "stop", "target"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_vwap_story",
            "description": "How price has behaved relative to VWAP throughout the entire day. Not just current position — the full story. A day spent above VWAP = institutions net long. Multiple crosses = choppy, avoid trend trades.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_or_status",
            "description": "Complete Opening Range context. ORH and ORL are the most important morning levels. Returns: OR high/low, whether broken, direction, distance, retest history. Use for morning trades. If OR inside range = choppy day.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_signal",
            "description": "Send the final trading signal. Call ONLY when confident. This triggers browser update immediately. If not confident, send WAIT with explanation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "signal": {"type": "string", "enum": ["LONG", "SHORT", "WAIT"]},
                    "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "entry": {"type": "number", "description": "Exact entry price"},
                    "stop": {"type": "number", "description": "Stop loss price"},
                    "tp1": {"type": "number", "description": "First target — nearest key level"},
                    "tp2": {"type": "number", "description": "Second target — next key level"},
                    "rr": {"type": "number", "description": "Risk reward ratio to TP1"},
                    "pattern": {"type": "string", "description": "Pattern e.g. REJECTION, RETEST, BREAKOUT, STOP_HUNT"},
                    "narrative": {"type": "string", "description": "One sentence story of why this trade makes sense"},
                    "reasoning": {"type": "string", "description": "Full technical reasoning"},
                    "invalidation": {"type": "string", "description": "What kills this setup"},
                    "warnings": {"type": "string", "description": "Any concerns"},
                    "wait_for": {"type": "string", "description": "If WAIT — what to watch for next"}
                },
                "required": ["signal", "confidence", "entry", "stop", "tp1", "tp2", "rr", "pattern", "narrative", "reasoning", "invalidation"]
            }
        }
    }
]


class ToolHandler:
    def __init__(self, engine):
        self.engine = engine

    async def execute(self, tool_name: str, args: dict) -> str:
        handlers = {
            "get_candles":        self._get_candles,
            "get_cvd":            self._get_cvd,
            "get_level_info":     self._get_level_info,
            "get_all_levels":     self._get_all_levels,
            "get_trend":          self._get_trend,
            "get_day_character":  self._get_day_character,
            "get_session":        self._get_session,
            "get_signal_history": self._get_signal_history,
            "calculate_rr":       self._calculate_rr,
            "get_vwap_story":     self._get_vwap_story,
            "get_or_status":      self._get_or_status,
            "send_signal":        self._send_signal,
        }
        handler = handlers.get(tool_name)
        if not handler:
            return f"Unknown tool: {tool_name}"
        try:
            return await handler(args)
        except Exception as e:
            return f"Tool error ({tool_name}): {e}"

    async def _get_candles(self, args: dict) -> str:
        tf = args["timeframe"]
        count = min(args["count"], 20)

        if tf == "1m":
            candles = self.engine.candles.closed_1m[-count:]
        elif tf == "5m":
            candles = self.engine.candles.closed_5m[-count:]
        else:
            candles = self.engine.candles.closed_15m[-count:]

        if not candles:
            return "No candles available"

        all_c = (self.engine.candles.closed_1m if tf == "1m"
                 else self.engine.candles.closed_5m if tf == "5m"
                 else self.engine.candles.closed_15m)
        avg_v = sum(c.v for c in all_c[-21:-1]) / 20 if len(all_c) > 20 else 0

        lines = [f"Last {len(candles)} closed {tf} candles:"]
        lines.append("Time    Open    High    Low     Close   Vol     Wick analysis")
        for c in candles:
            et = datetime.fromtimestamp(c.t / 1000, tz=ET)
            tstr = et.strftime("%H:%M")
            body = abs(c.c - c.o)
            upper = c.h - max(c.o, c.c)
            lower = min(c.o, c.c) - c.l
            ratio = max(upper, lower) / body if body > 0.001 else 0
            color = "GREEN" if c.c >= c.o else "RED"
            vol_r = f"{c.v / avg_v:.1f}x" if avg_v > 0 else "-"
            lines.append(
                f"{tstr}  {c.o:.2f}  {c.h:.2f}  {c.l:.2f}  {c.c:.2f}  {vol_r}  "
                f"body={body:.3f} up_wick={upper:.3f} dn_wick={lower:.3f} wick/body={ratio:.1f} {color}"
            )
        # Volume surge on last candle
        if candles and avg_v > 0:
            last = candles[-1]
            if last.v >= avg_v * 2.0:
                lines.append(f"\nVOLUME SURGE on last candle ({last.v / avg_v:.1f}x avg) — institutional activity")
            elif last.v >= avg_v * 1.5:
                lines.append(f"\nAbove-average volume on last candle ({last.v / avg_v:.1f}x)")
        return "\n".join(lines)

    async def _get_cvd(self, args: dict) -> str:
        minutes = min(args["minutes"], 30)
        history = self.engine.cvd.get_history(minutes)
        current = self.engine.cvd.value
        bias = self.engine.cvd.bias

        if not history:
            return f"CVD: {current:+,.0f} ({bias}) — no minute history yet"

        lines = [f"CVD now: {current:+,.0f} ({bias})", f"Last {len(history)} minutes:"]
        for pt in history:
            arrow = "^" if pt.delta > 0 else "v" if pt.delta < 0 else "-"
            lines.append(f"  {pt.time_et}  {pt.value:+,.0f}  {arrow} {pt.delta:+,.0f} this minute")

        if len(history) >= 2:
            total_change = history[-1].value - history[0].value
            lines.append(f"\nTotal change last {len(history)} min: {total_change:+,.0f}")

        # Divergence detection
        div = self.engine.cvd.detect_divergence(self.engine.candles.closed_1m)
        if div["type"] != "NONE":
            lines.append(f"\nDIVERGENCE: {div['type']} — {div['detail']}")
        else:
            lines.append(f"\nNo divergence detected — CVD confirms price action")
        return "\n".join(lines)

    def _get_atr(self) -> float:
        closed = self.engine.candles.closed_1m
        if len(closed) >= 14:
            return sum(c.h - c.l for c in closed[-14:]) / 14
        return 0.68

    async def _get_level_info(self, args: dict) -> str:
        name = args["level_name"].upper()
        levels = self.engine._current_levels or []

        level = next((lvl for lvl in levels if lvl.label.upper() == name), None)
        if not level:
            return f"Level '{name}' not found. Available: {', '.join(lvl.label for lvl in levels[:15])}"

        current = self.engine.candles.live_price
        distance = abs(current - level.price)
        dist_pct = distance / current * 100 if current > 0 else 0
        atr = self._get_atr()
        atr_mult = distance / atr if atr > 0 else 0

        if atr_mult < 0.5:
            proximity = "very close — immediate reaction zone"
        elif atr_mult < 1.0:
            proximity = "close — reachable this candle"
        elif atr_mult < 2.0:
            proximity = "moderate — reachable in a few candles"
        else:
            proximity = "extended — less likely reached soon"

        description = LEVEL_CONTEXT.get(level.label.upper(), LEVEL_CONTEXT.get(level.source.upper(), "Key price level"))
        strength_desc = STRENGTH_CONTEXT.get(level.strength, "")

        lines = [
            f"Level: {level.label}",
            f"Price: ${level.price:.2f}",
            f"What: {description}",
            f"Strength: {level.strength}/4 — {strength_desc}",
            f"Distance: ${distance:.2f} ({dist_pct:.2f}% | {atr_mult:.1f}x ATR — {proximity})",
        ]

        # Confluence
        confluent = [lvl for lvl in levels if lvl.label != level.label and abs(lvl.price - level.price) <= 0.50]
        if confluent:
            lines.append(f"Confluence: within $0.50 of {', '.join(lvl.label for lvl in confluent)} — STRONGER together")

        # Test history
        level_tests = self.engine._level_tests.get(level.label.upper(), [])
        lines.append(f"Tests today: {len(level_tests)}")

        if level_tests:
            lines.append("Test history:")
            for t in level_tests:
                lines.append(
                    f"  {t.get('time_et', '?')}  {t.get('result', '?')}  "
                    f"H:${t.get('candle_high', 0):.2f} L:${t.get('candle_low', 0):.2f} C:${t.get('candle_close', 0):.2f}"
                )
        else:
            lines.append("First test today — fresh level, strong reaction expected")

        return "\n".join(lines)

    async def _get_all_levels(self, args: dict) -> str:
        levels = self.engine._current_levels or []
        current = self.engine.candles.live_price
        if not levels:
            return "No levels computed yet"

        atr = self._get_atr()
        above = sorted([lvl for lvl in levels if lvl.price > current + 0.10], key=lambda lvl: lvl.price)
        below = sorted([lvl for lvl in levels if lvl.price < current - 0.10], key=lambda lvl: lvl.price, reverse=True)

        lines = [f"Current price: ${current:.2f} | ATR: ${atr:.3f}", "", "ABOVE (resistance):"]
        for lvl in above[:8]:
            dist = lvl.price - current
            atr_mult = dist / atr if atr > 0 else 0
            near = [x.label for x in above[:8] if x.label != lvl.label and abs(x.price - lvl.price) <= 0.50]
            conf = f" *CONFLUENCE({','.join(near)})" if near else ""
            ctx = LEVEL_CONTEXT.get(lvl.label.upper(), "")
            ctx_short = ctx[:50] + "..." if len(ctx) > 50 else ctx
            lines.append(f"  {lvl.label:8} ${lvl.price:.2f}  +${dist:.2f} ({atr_mult:.1f}x ATR)  str={lvl.strength}/4{conf}")
            if ctx_short:
                lines.append(f"           {ctx_short}")

        lines.append("\nBELOW (support):")
        for lvl in below[:8]:
            dist = current - lvl.price
            atr_mult = dist / atr if atr > 0 else 0
            near = [x.label for x in below[:8] if x.label != lvl.label and abs(x.price - lvl.price) <= 0.50]
            conf = f" *CONFLUENCE({','.join(near)})" if near else ""
            ctx = LEVEL_CONTEXT.get(lvl.label.upper(), "")
            ctx_short = ctx[:50] + "..." if len(ctx) > 50 else ctx
            lines.append(f"  {lvl.label:8} ${lvl.price:.2f}  -${dist:.2f} ({atr_mult:.1f}x ATR)  str={lvl.strength}/4{conf}")
            if ctx_short:
                lines.append(f"           {ctx_short}")

        return "\n".join(lines)

    async def _get_trend(self, args: dict) -> str:
        from ..market_utils import detect_trend_with_strength, calc_vwap

        closed_15m = self.engine.candles.closed_15m
        closed_5m = self.engine.candles.closed_5m
        current = self.engine.candles.live_price
        today_1m = self.engine.candles.today_candles_1m

        if len(closed_15m) >= 30:
            t15_dir, t15_count, t15_str = detect_trend_with_strength(closed_15m)
        else:
            t15_dir, t15_count, t15_str = None, 0, "UNKNOWN"
        if len(closed_5m) >= 30:
            t5_dir, t5_count, t5_str = detect_trend_with_strength(closed_5m)
        else:
            t5_dir, t5_count, t5_str = None, 0, "UNKNOWN"

        t15_val = t15_dir.value if t15_dir else "UNKNOWN"
        t5_val = t5_dir.value if t5_dir else "UNKNOWN"
        aligned = t15_val == t5_val and t15_val not in ("RANGING", "UNKNOWN")
        vwap = calc_vwap(today_1m) if today_1m else 0
        v_pos = "ABOVE" if current > vwap else "BELOW"
        v_dist = abs(current - vwap)

        return (
            f"15m trend: {t15_val} ({t15_str}, {t15_count} swings)\n"
            f"5m trend:  {t5_val} ({t5_str}, {t5_count} swings)\n"
            f"Aligned:   {'YES — trade with confidence' if aligned else 'NO — conflicting, be cautious'}\n"
            f"VWAP:      ${vwap:.2f} (price {v_pos} by ${v_dist:.2f})\n"
            f"Bias:      {t15_val if t15_val not in ('RANGING', 'UNKNOWN') else t5_val}"
        )

    async def _get_day_character(self, args: dict) -> str:
        pm = self.engine.pm_data
        c1m = self.engine.candles.closed_1m
        today = self.engine.candles.today_candles_1m

        if not c1m:
            return "No intraday data yet"

        day_high = max(c.h for c in today) if today else 0
        day_low = min(c.l for c in today) if today else 0
        day_range = day_high - day_low
        avg_range = 3.50
        range_pct = day_range / avg_range * 100 if avg_range > 0 else 0
        avg_vol = sum(c.v for c in c1m[-21:-1]) / 20 if len(c1m) > 20 else 0
        last_vol = c1m[-1].v if c1m else 0
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1

        lines = []
        if pm:
            lines += [
                f"Gap: {pm.gap_pct:+.2f}% {pm.gap_type}",
                f"PM High: ${pm.pm_high:.2f}  PM Low: ${pm.pm_low:.2f}",
            ]
            gap_fill = pm.pd_close
            if pm.gap_pct > 0.2 and today:
                filled = min(c.l for c in today) <= gap_fill
                lines.append(f"Gap fill at: ${gap_fill:.2f} ({'FILLED' if filled else 'NOT FILLED — magnet below'})")
                lines.append("Gap up holding = bullish continuation" if not filled else "Gap filled = neutral, watch for direction")
            elif pm.gap_pct < -0.2 and today:
                filled = max(c.h for c in today) >= gap_fill
                lines.append(f"Gap fill at: ${gap_fill:.2f} ({'FILLED' if filled else 'NOT FILLED — magnet above'})")
        lines += [
            f"Day range: ${day_range:.2f} ({range_pct:.0f}% of avg ${avg_range:.2f})",
            f"Day high: ${day_high:.2f}  Day low: ${day_low:.2f}",
            f"Volume trend: {vol_ratio:.1f}x average",
            f"Character: {'EXTENDED' if range_pct > 100 else 'NORMAL' if range_pct > 60 else 'EARLY — room to move'}",
        ]
        return "\n".join(lines)

    async def _get_session(self, args: dict) -> str:
        from ..sessions import get_session
        sess = get_session()
        return (
            f"Session: {sess.label}\n"
            f"Quality: {sess.quality}/5\n"
            f"Minutes remaining: {sess.min_remaining}\n"
            f"Trading allowed: {'YES' if sess.quality > 0 else 'NO'}\n"
            f"Targets: {'Conservative — session ending soon' if sess.min_remaining < 20 else 'Normal targets valid'}"
        )

    async def _get_signal_history(self, args: dict) -> str:
        history = self.engine.signal_history
        if not history:
            return "No signals fired today"
        lines = [f"Signals today ({len(history)}):"]
        for sig in history:
            lines.append(
                f"  {sig.get('time', '?')}  {sig.get('direction', '?'):5}  "
                f"{sig.get('level', '?'):8}  @ ${sig.get('entry', 0):.2f}  "
                f"conf={sig.get('confidence', '?')}"
            )
        return "\n".join(lines)

    async def _calculate_rr(self, args: dict) -> str:
        entry = args["entry"]
        stop = args["stop"]
        target = args["target"]
        risk = abs(entry - stop)
        reward = abs(target - entry)
        if risk <= 0:
            return "Invalid: entry and stop are the same price"
        rr = reward / risk
        return (
            f"Entry:  ${entry:.2f}\n"
            f"Stop:   ${stop:.2f}\n"
            f"Target: ${target:.2f}\n"
            f"Risk:   ${risk:.2f} per share\n"
            f"Reward: ${reward:.2f} per share\n"
            f"RR:     {rr:.2f}:1\n"
            f"Meets minimum 2:1: {'YES' if rr >= 2.0 else 'NO — try a further target'}"
        )

    async def _get_vwap_story(self, _args: dict) -> str:
        today = self.engine.candles.today_candles_1m
        if not today or len(today) < 5:
            return "Not enough intraday data for VWAP story"

        current = self.engine.candles.live_price
        crosses = 0
        above_count = 0
        below_count = 0
        running_vwap_prices = []

        tp_vol_sum = 0.0
        vol_sum = 0.0
        for c in today:
            tp = (c.h + c.l + c.c) / 3
            tp_vol_sum += tp * c.v
            vol_sum += c.v
            vwap_at = tp_vol_sum / vol_sum if vol_sum > 0 else 0
            running_vwap_prices.append(vwap_at)
            if c.c > vwap_at:
                above_count += 1
            else:
                below_count += 1

        for i in range(1, len(today)):
            prev_above = today[i - 1].c > running_vwap_prices[i - 1]
            curr_above = today[i].c > running_vwap_prices[i]
            if prev_above != curr_above:
                crosses += 1

        total = above_count + below_count
        pct_above = above_count / total * 100 if total > 0 else 0
        vwap_now = running_vwap_prices[-1] if running_vwap_prices else 0
        position = "ABOVE" if current > vwap_now else "BELOW"
        dist = abs(current - vwap_now)

        if crosses <= 2 and pct_above > 70:
            character = "STRONG BULL DAY — institutions net long, buy dips to VWAP"
        elif crosses <= 2 and pct_above < 30:
            character = "STRONG BEAR DAY — institutions net short, sell rallies to VWAP"
        elif crosses >= 5:
            character = "CHOPPY — multiple VWAP crosses, avoid trend trades"
        else:
            character = "MIXED — some direction but not clean"

        return (
            f"VWAP: ${vwap_now:.2f}\n"
            f"Price: {position} by ${dist:.2f}\n"
            f"Time above VWAP: {pct_above:.0f}% ({above_count}/{total} bars)\n"
            f"VWAP crosses today: {crosses}\n"
            f"Character: {character}"
        )

    async def _get_or_status(self, _args: dict) -> str:
        or_data = self.engine.or_data
        if not or_data:
            return "Opening Range not yet available"

        if not or_data.complete:
            return (
                f"Opening Range forming: {or_data.bar_count}/30 minutes\n"
                f"Current high: ${or_data.high:.2f}\n"
                f"Current low:  ${or_data.low:.2f}\n"
                f"Locks at 10:00 AM ET"
            )

        price = self.engine.candles.live_price
        or_range = or_data.high - or_data.low

        if price > or_data.high:
            status = "BROKEN BULLISH"
            distance = price - or_data.high
            bias = "LONG bias — bulls won the morning"
        elif price < or_data.low:
            status = "BROKEN BEARISH"
            distance = or_data.low - price
            bias = "SHORT bias — bears won the morning"
        else:
            status = "INSIDE RANGE"
            distance = 0
            bias = "NO BIAS — choppy, avoid trend trades"

        lines = [
            f"ORH: ${or_data.high:.2f}",
            f"ORL: ${or_data.low:.2f}",
            f"Range: ${or_range:.2f}",
            f"Status: {status}",
            f"Bias: {bias}",
        ]
        if distance > 0:
            lines.append(f"Distance from OR: ${distance:.2f}")

        broken = self.engine._broken_levels
        if "ORH" in broken:
            lines.append(f"ORH ${or_data.high:.2f} previously broken — now support")
        if "ORL" in broken:
            lines.append(f"ORL ${or_data.low:.2f} previously broken — now resistance")

        orh_tests = len(self.engine._level_tests.get("ORH", []))
        orl_tests = len(self.engine._level_tests.get("ORL", []))
        if orh_tests:
            lines.append(f"ORH tested {orh_tests}x today")
        if orl_tests:
            lines.append(f"ORL tested {orl_tests}x today")

        return "\n".join(lines)

    async def _send_signal(self, args: dict) -> str:
        return f"Signal sent: {args.get('signal', '?')} @ ${args.get('entry', 0):.2f}"

    async def run_all_tool_tests(self) -> dict:
        results = {}
        for tf in ["1m", "5m", "15m"]:
            try:
                r = await self._get_candles({"timeframe": tf, "count": 5})
                assert len(r) > 0
                results[f"get_candles_{tf}"] = "PASS"
            except Exception as e:
                results[f"get_candles_{tf}"] = f"FAIL: {e}"
        try:
            r = await self._get_cvd({"minutes": 10})
            assert "CVD" in r
            results["get_cvd"] = "PASS"
        except Exception as e:
            results["get_cvd"] = f"FAIL: {e}"
        try:
            r = await self._get_all_levels({})
            assert "ABOVE" in r or "BELOW" in r or "No levels" in r
            results["get_all_levels"] = "PASS"
        except Exception as e:
            results["get_all_levels"] = f"FAIL: {e}"
        try:
            r = await self._get_trend({})
            assert "trend" in r.lower()
            results["get_trend"] = "PASS"
        except Exception as e:
            results["get_trend"] = f"FAIL: {e}"
        try:
            r = await self._get_day_character({})
            assert len(r) > 10
            results["get_day_character"] = "PASS"
        except Exception as e:
            results["get_day_character"] = f"FAIL: {e}"
        try:
            r = await self._get_session({})
            assert "Session" in r
            results["get_session"] = "PASS"
        except Exception as e:
            results["get_session"] = f"FAIL: {e}"
        try:
            r = await self._get_signal_history({})
            assert len(r) > 0
            results["get_signal_history"] = "PASS"
        except Exception as e:
            results["get_signal_history"] = f"FAIL: {e}"
        try:
            r = await self._calculate_rr({"entry": 580.50, "stop": 579.50, "target": 583.00})
            assert "RR" in r
            results["calculate_rr_valid"] = "PASS"
        except Exception as e:
            results["calculate_rr_valid"] = f"FAIL: {e}"
        try:
            r = await self._calculate_rr({"entry": 580.50, "stop": 579.50, "target": 581.00})
            assert "NO" in r
            results["calculate_rr_bad"] = "PASS"
        except Exception as e:
            results["calculate_rr_bad"] = f"FAIL: {e}"
        try:
            r = await self._get_level_info({"level_name": "VWAP"})
            assert "VWAP" in r or "not found" in r
            results["get_level_info"] = "PASS"
        except Exception as e:
            results["get_level_info"] = f"FAIL: {e}"
        try:
            r = await self._get_vwap_story({})
            assert "VWAP" in r or "Not enough" in r
            results["get_vwap_story"] = "PASS"
        except Exception as e:
            results["get_vwap_story"] = f"FAIL: {e}"
        try:
            r = await self._get_or_status({})
            assert len(r) > 0
            results["get_or_status"] = "PASS"
        except Exception as e:
            results["get_or_status"] = f"FAIL: {e}"
        return results
