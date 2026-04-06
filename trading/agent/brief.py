from datetime import datetime, timedelta
import math
import pytz
from ..models import Candle, Level, DayContext
from ..constants import MIN_RR, CUTOFF_HOUR, CUTOFF_MIN, VIX_HARD_BLOCK
from ..context.sim_clock import now_et

ET = pytz.timezone("America/New_York")


def build_brief(
    asset: str, pattern: str, direction: str, level: Level,
    event_candle: Candle, retest_candle: Candle | None,
    cvd_at_break: float, cvd_now: float, cvd_turned: bool, volume_ratio: float,
    day_context: DayContext, vix: float, current_price: float, atr: float,
    nearest_above: list[Level], nearest_below: list[Level],
    session_name: str, session_quality: int, minutes_to_cutoff: int,
    tests_today: int, zone_context: str = "",
    verification_data: str = "", strength: str = "",
    approach_type: str = "", approach_confidence_pts: int = 0,
    cvd_quarantine: bool = False,
    bars_1m: list = None,
    fvg_found: bool = False, fvg_mid: float = 0.0, fvg_bonus: int = 0,
    trend_5m: str = "NEUTRAL", trend_pts: int = 0,
    setup_data: dict | None = None,
) -> str:
    now = now_et()
    now_str = now.strftime("%I:%M %p ET")

    lines = [
        "=" * 52,
        f"TRADING SETUP — {asset}",
        f"{now_str} | {session_name} ({minutes_to_cutoff} min to cutoff)",
        "=" * 52, "",
        "DAY CONTEXT:",
        f"  Bias: {day_context.bias} | Type: {day_context.day_type}",
    ]
    if day_context.gap_pct != 0:
        lines.append(f"  Gap: {day_context.gap_type} {day_context.gap_pct:+.2f}%")

    # Macro status
    from ..data.calendar import is_macro_halt
    macro_halted, macro_reason = is_macro_halt(now)
    lines += [
        "",
        "MACRO STATUS:",
        f"  {'HALTED — ' + macro_reason if macro_halted else 'CLEAR'}",
    ]

    # Earnings status
    from ..data.calendar import is_earnings_within_hold
    earn_blocked, earn_reason = is_earnings_within_hold(asset)
    lines += [
        "",
        "EARNINGS STATUS:",
        f"  {'BLOCKED — ' + earn_reason if earn_blocked else 'CLEAR'}",
    ]

    # CVD quality
    cvd_quality = "⚠ QUARANTINED (unreliable after WS reconnect)" if cvd_quarantine else "LIVE"
    lines += [
        "",
        "CVD STATUS:",
        f"  {cvd_quality}",
    ]

    # 5m trend alignment
    trend_sign = "+" if trend_pts >= 0 else ""
    lines += ["", "5M TREND ALIGNMENT:", f"  Trend:   {trend_5m}", f"  Signal:  {direction}", f"  Score:   {trend_sign}{trend_pts} pts"]
    if trend_pts == 8:
        lines.append("  Meaning: IDEAL — 5m trend drove price INTO the level. Institutions hunted these stops deliberately.")
    elif trend_pts == -10:
        lines.append("  Meaning: CAUTION — 5m trend aligns with signal direction. This looks like continuation not reversal. Verify institutional intent before firing.")
    else:
        lines.append("  Meaning: NEUTRAL — no strong 5m trend. Setup valid but without trend-fed stop cluster.")

    # Approach context
    if approach_type:
        lines += [
            "",
            "APPROACH CONTEXT:",
            f"  Type: {approach_type} | Confidence pts: {approach_confidence_pts:+d}",
        ]

    # VIX + Options — pre-computed so agent doesn't need to call get_options_context
    if vix < 15: vl = "CALM"
    elif vix < 20: vl = "NORMAL"
    elif vix < 25: vl = "ELEVATED"
    elif vix < 30: vl = "HIGH"
    else: vl = "VERY HIGH"

    if vix >= VIX_HARD_BLOCK:
        env, size, inst = "BLOCKED", "NONE", "NO TRADE"
    elif vix < 20: env, size, inst = "NORMAL", "FULL", "ATM outright"
    elif vix < 25: env, size, inst = "ELEVATED", "FULL", "ATM or 1 OTM"
    elif vix < 30: env, size, inst = "HIGH", "HALF", "debit spread"
    else: env, size, inst = "VERY HIGH", "QUARTER", "spread only"

    from ..core.asset_registry import get_config
    cfg = get_config(asset)
    minor = cfg["round_interval_minor"]
    atm_strike = round(round(current_price / minor) * minor, 2) if current_price > 0 else 0

    hour = now.hour + now.minute / 60.0
    from ..context.options_context import get_expiry
    dte, _expiry_str, _ = get_expiry(asset)
    exp_dt = now + timedelta(days=dte)

    daily_move = (current_price * (vix / 100)) / math.sqrt(252) if current_price > 0 and vix > 0 else 0
    prem = daily_move * 0.4

    lines += [
        "", "OPTIONS CONTEXT (pre-computed):",
        f"  VIX: {vix:.1f} ({vl}) | Session: {session_quality}/5",
        f"  Size: {size} | Instrument: {inst}",
        f"  ATM strike: ${atm_strike:.2f} | DTE: {dte} | Expiry: {exp_dt.strftime('%b %d')}",
    ]
    if prem > 0:
        lines.append(f"  Est premium: ${prem*0.8:.2f}-${prem*1.2:.2f}")
        lines.append(f"  Break-even: ${current_price+prem:.2f} (calls) / ${current_price-prem:.2f} (puts)")
    if hour > 14.5:
        lines.append("  WARNING: Late session — theta decay rapid. TP1 only.")
    lines.append("")

    # Setup
    conf = f" CONFLUENCE({', '.join(level.confluence_with)})" if level.confluence_with else ""
    pattern_str = f"{pattern} | Strength: {strength}" if strength else pattern
    lines += [
        "THE SETUP:",
        f"  Pattern:   {pattern_str}",
        f"  Direction: {direction}",
        f"  Level:     {level.name} ${level.price:.2f} (score {level.score}/10){conf}",
        f"  Context:   {level.description}",
        f"  History:   {_test_note(tests_today)}",
    ]
    if zone_context:
        lines.append(f"  Zone:      {zone_context}")
    lines.append("")

    # Setup-specific context
    if setup_data:
        lines.append(_build_setup_section(pattern, setup_data))

    # Event candle
    lines += [
        "EVENT CANDLE:",
        f"  O:{event_candle.o:.2f} H:{event_candle.h:.2f} L:{event_candle.l:.2f} C:{event_candle.c:.2f}",
        f"  Body: ${event_candle.body:.3f} | Volume: {volume_ratio:.1f}x avg",
        "",
    ]

    # 1m trigger context with level price marker
    if bars_1m and len(bars_1m) >= 1:
        trigger_bars = bars_1m[-6:] if len(bars_1m) >= 6 else bars_1m
        lines.append("1M TRIGGER CONTEXT (last 6 bars):")
        lines.append(f"  ---- LEVEL: {level.name} ${level.price:.2f} ----")
        for bar in trigger_bars:
            arrow = "▲" if bar.c > bar.o else "▼" if bar.c < bar.o else "—"
            body = abs(bar.c - bar.o)
            pos = "ABOVE" if bar.c > level.price else "BELOW" if bar.c < level.price else "AT"
            lines.append(f"  {arrow} O:{bar.o:.2f} H:{bar.h:.2f} L:{bar.l:.2f} C:{bar.c:.2f} Vol:{bar.v:,.0f} Body:{body:.2f} [{pos}]")
        trigger = trigger_bars[-1]
        dir_label = "BULLISH (reversal up)" if direction == "BULLISH" else "BEARISH (reversal down)"
        swept = "below" if trigger.l < level.price else "above"
        closed = "above" if trigger.c > level.price else "below"
        lines += [
            "",
            f"TRIGGER: {dir_label}",
            f"  Swept {swept} ${level.price:.2f}, closed back {closed}",
            "",
        ]

    # FVG context
    if fvg_found:
        lines += [
            "FVG DETECTED: YES",
            f"  Midpoint:         ${fvg_mid:.2f}",
            f"  Status:           UNFILLED",
            f"  Confidence bonus: +{fvg_bonus} pts applied",
            f"  Reason:           Clean institutional displacement confirmed.",
            f"                    Sweep candle moved fast enough to leave",
            f"                    a price gap. Institutional orders likely",
            f"                    sitting at the midpoint.",
            f"  Entry:            Place LIMIT order at ${fvg_mid:.2f}",
            f"                    If not filled within 2 bars → enter at market",
            "",
        ]
    else:
        lines += [
            "FVG DETECTED: NO",
            "  Confidence bonus: +0 pts",
            "  Reason:           Sweep occurred but no clean displacement gap.",
            "                    Price moved slowly or gap already filled.",
            f"  Entry:            Market order near level ${level.price:.2f}",
            "",
        ]

    # Retest / confirmation candle
    if retest_candle:
        lines += [
            "CONFIRMATION CANDLE (1m):",
            f"  O:{retest_candle.o:.2f} H:{retest_candle.h:.2f} L:{retest_candle.l:.2f} C:{retest_candle.c:.2f}",
            "",
        ]

    # Levels
    lines.append("NEAREST LEVELS:")
    lines.append("  RESISTANCE:")
    for lv in nearest_above[:4]:
        d = lv.price - current_price
        lines.append(f"    {lv.name:8} ${lv.price:.2f}  +${d:.2f}  score:{lv.score}")
    lines.append(f"  ── ${current_price:.2f} CURRENT ──")
    lines.append("  SUPPORT:")
    for lv in nearest_below[:4]:
        d = current_price - lv.price
        lines.append(f"    {lv.name:8} ${lv.price:.2f}  -${d:.2f}  score:{lv.score}")
    lines.append("")

    # Pre-calc RR — skip levels too close to entry, find meaningful targets
    if direction == "BULLISH":
        entry = current_price
        stop = level.price - atr * 0.5
        risk = abs(entry - stop)
        min_tp_dist = max(atr * 1.0, risk * 2.0)
        tp_list = [l for l in nearest_above if (l.price - entry) >= min_tp_dist]
        if not tp_list:
            tp_list = nearest_above
        if tp_list:
            target_lv = tp_list[0]
            tp2_lv = tp_list[1] if len(tp_list) > 1 else None
            reward = abs(target_lv.price - entry)
            rr = reward / risk if risk > 0 else 0
            lines += [
                "RISK/REWARD (pre-computed):",
                f"  Entry ${entry:.2f} | Stop ${stop:.2f} | Risk ${risk:.2f}",
                f"  TP1 {target_lv.name} ${target_lv.price:.2f} | Reward ${reward:.2f} | RR {rr:.1f}:1",
            ]
            if tp2_lv:
                r2 = abs(tp2_lv.price - entry)
                rr2 = r2 / risk if risk > 0 else 0
                lines.append(f"  TP2 {tp2_lv.name} ${tp2_lv.price:.2f} | Reward ${r2:.2f} | RR {rr2:.1f}:1")
            lines += [f"  Status: {'MEETS {:.0f}:1 MINIMUM'.format(MIN_RR) if rr >= MIN_RR else 'ACCEPTABLE (1.5:1+) for score 8+ confluence' if rr >= 1.5 else 'BELOW 1.5:1 — consider wider target'}", ""]
    elif direction == "BEARISH":
        entry = current_price
        stop = level.price + atr * 0.5
        risk = abs(entry - stop)
        min_tp_dist = max(atr * 1.0, risk * 2.0)
        tp_list = [l for l in nearest_below if (entry - l.price) >= min_tp_dist]
        if not tp_list:
            tp_list = nearest_below
        if tp_list:
            target_lv = tp_list[0]
            tp2_lv = tp_list[1] if len(tp_list) > 1 else None
            reward = abs(entry - target_lv.price)
            rr = reward / risk if risk > 0 else 0
            lines += [
                "RISK/REWARD (pre-computed):",
                f"  Entry ${entry:.2f} | Stop ${stop:.2f} | Risk ${risk:.2f}",
                f"  TP1 {target_lv.name} ${target_lv.price:.2f} | Reward ${reward:.2f} | RR {rr:.1f}:1",
            ]
            if tp2_lv:
                r2 = abs(entry - tp2_lv.price)
                rr2 = r2 / risk if risk > 0 else 0
                lines.append(f"  TP2 {tp2_lv.name} ${tp2_lv.price:.2f} | Reward ${r2:.2f} | RR {rr2:.1f}:1")
            lines += [f"  Status: {'MEETS {:.0f}:1 MINIMUM'.format(MIN_RR) if rr >= MIN_RR else 'ACCEPTABLE (1.5:1+) for score 8+ confluence' if rr >= 1.5 else 'BELOW 1.5:1 — consider wider target'}", ""]

    # Verification data (formerly required verify_setup tool call — now inlined)
    if verification_data:
        lines += [
            "", "=" * 52,
            "VERIFICATION DATA (5m candles + CVD + trend + volume profile)",
            "=" * 52,
            verification_data,
        ]

    # Instructions
    lines += [
        "", "DECISION: Review the Brief and VERIFICATION DATA above, then call send_signal with LONG, SHORT, or WAIT.",
        "Use tools only if you need deeper investigation:",
        "  - get_candles / get_cvd — deeper price action or CVD divergence check",
        "  - get_level_info / get_level_map — deeper level analysis",
        "  - calculate_rr — re-verify with different entry/stop/target",
    ]

    return "\n".join(lines)


def _test_note(tests_today: int) -> str:
    if tests_today == 0: return "First test today — fresh level, strong reaction expected"
    if tests_today == 1: return "Second test — still significant"
    if tests_today == 2: return "Third test — level weakening"
    return "Fourth+ test — expect breakout, not rejection"


def _build_setup_section(pattern: str, data: dict) -> str:
    if not data:
        return ""
    if pattern == "LIQUIDITY_GRAB":
        return _section_liquidity_grab(data)
    elif pattern == "OB_DEFENSE":
        return _section_ob_defense(data)
    elif pattern == "FAILED_AUCTION_VAR":
        return _section_failed_auction_var(data)
    elif pattern == "FAILED_AUCTION_MAJOR":
        return _section_failed_auction_major(data)
    return ""


def _section_liquidity_grab(data: dict) -> str:
    wick_past = data.get("wick_past", 0.0)
    wick_rejection = data.get("wick_rejection", 0.0)
    wick_extreme = data.get("wick_extreme", 0.0)
    cvd_ratio = data.get("cvd_ratio", 0.0)
    fvg_found = data.get("fvg_found", False)
    fvg_mid = data.get("fvg_midpoint", 0.0)
    vol_ratio = data.get("vol_ratio", 0.0)
    conf = data.get("confidence")
    score_val = conf.score if conf else 0
    enrichment = data.get("enrichment", {})
    entry_note = f"FVG FOUND — limit order at ${fvg_mid:.2f}. Wait max 2 bars for fill. If unfilled → market." if fvg_found else "NO FVG — market order at next candle open."
    return (
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "S1 LIQUIDITY GRAB (5m) — SETUP CONTEXT\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "WHAT THIS IS:\n"
        "  A stop-hunt reversal detected on the 5m chart.\n"
        "  Price swept through a key level on a 5m bar,\n"
        "  absorbed stop orders with volume, then closed back.\n"
        "  A 1m momentum candle confirmed the reversal direction.\n"
        "\n"
        "5M SWEEP DATA:\n"
        f"  Wick penetration:  {wick_past:.3f} past level\n"
        f"  Wick rejection:    {wick_rejection:.0%} of bar range (higher = better)\n"
        f"  Wick extreme:      ${wick_extreme:.2f} (stop-loss anchor)\n"
        f"  Volume spike:      {vol_ratio:.1f}x rolling average (gated >= 1.2x)\n"
        f"  CVD turn:          {cvd_ratio:.1f}x rolling average\n"
        f"  Confidence score:  {score_val}/100\n"
        "\n"
        "1M ENRICHMENT:\n"
        f"  Absorption speed:  +{enrichment.get('absorption', 0)} pts\n"
        f"  Volume clustering: +{enrichment.get('vol_cluster', 0)} pts\n"
        f"  CVD micro-turn:    +{enrichment.get('cvd_micro', 0)} pts\n"
        f"  Total enrichment:  +{enrichment.get('total', 0)} pts\n"
        "\n"
        f"ENTRY:\n  {entry_note}\n"
        "\n"
        "CONFIRM BEFORE TRADING:\n"
        "  * Volume spike on sweep bar (already gated >= 1.2x)\n"
        "  * CVD turned ON the sweep candle (ratio >= 2.0x ideal)\n"
        "  * Wick rejection high (>= 50% of bar range)\n"
        "  * 5m trend OPPOSED to signal = ideal reversal condition\n"
        "\n"
        "RED FLAGS — WAIT if:\n"
        "  X CVD ratio < 1.0x (no institutional absorption)\n"
        "  X 5m trend ALIGNED with signal (continuation not reversal)\n"
        "  X Wick < 0.25 ATR (noise, not a real sweep)\n"
        "  X Price already > 0.5% past entry (stale)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )


def _section_ob_defense(data: dict) -> str:
    ob = data.get("ob", {})
    ob_high = ob.get("ob_high", 0.0)
    ob_low = ob.get("ob_low", 0.0)
    ob_mid = ob.get("ob_mid", 0.0)
    ob_vol = ob.get("vol_ratio", 0.0)
    ob_visits = data.get("ob_visits", 0)
    cvd_ratio = data.get("cvd_ratio", 0.0)
    conf = data.get("confidence")
    score_val = conf.score if conf else 0
    visits_note = "FIRST TEST — highest probability" if ob_visits == 0 else f"TEST #{ob_visits + 1} — {'still valid' if ob_visits == 1 else 'caution: weakening'}"
    return (
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "S2 ORDER BLOCK DEFENSE — SETUP CONTEXT\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "WHAT THIS IS:\n"
        "  A CONTINUATION trade. Institutions created an order\n"
        "  block — the last opposite candle before a displacement.\n"
        "  Price has returned to defend that block.\n"
        "  This is NOT a reversal. Trend continues after defense.\n"
        "\n"
        "ORDER BLOCK ZONE:\n"
        f"  High:    ${ob_high:.2f}\n"
        f"  Low:     ${ob_low:.2f}\n"
        f"  Mid:     ${ob_mid:.2f}\n"
        f"  OB Vol:  {ob_vol:.1f}x average (when OB formed)\n"
        f"  Visits:  {visits_note}\n"
        "\n"
        "CVD AT DEFENSE:\n"
        f"  CVD turn: {cvd_ratio:.1f}x rolling average\n"
        f"  Confidence score: {score_val}/100\n"
        "\n"
        "CONFIRM BEFORE TRADING:\n"
        "  * Price touched OB zone and is rejecting\n"
        "  * CVD slope turned at OB (declining -> flat/rising)\n"
        "  * Day type is TREND (check day context above)\n"
        "  * Signal direction matches locked day bias\n"
        "\n"
        "RED FLAGS — WAIT if:\n"
        "  X OB has been visited 2+ times (zone is weakening)\n"
        "  X CVD still declining at OB (no defense visible)\n"
        "  X Day type flipped to RANGE since 10AM lock\n"
        "  X Price closed through OB mid on this bar\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )


def _section_failed_auction_var(data: dict) -> str:
    target = data.get("target", 0.0)
    details = data.get("details", "")
    vol_ratio = data.get("vol_ratio", 0.0)
    cvd_ratio = data.get("cvd_ratio", 0.0)
    conf = data.get("confidence")
    score_val = conf.score if conf else 0
    if "outside VAH" in details or "above VAH" in details:
        var_type = "ABOVE VAH — price was above value area, rejected back inside"
    elif "outside VAL" in details or "below VAL" in details:
        var_type = "BELOW VAL — price was below value area, rejected back inside"
    elif "inside touch" in details:
        var_type = "INSIDE TOUCH — price approached boundary from inside with rejection wick"
    else:
        var_type = details
    return (
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "S3A FAILED AUCTION (VAR) — SETUP CONTEXT\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "WHAT THIS IS:\n"
        "  Price attempted to auction outside the value area\n"
        "  and FAILED. Market rejected the price as unfair\n"
        "  and pushed back inside. Target is always POC —\n"
        "  the most accepted price of the session.\n"
        "\n"
        "AUCTION TYPE:\n"
        f"  {var_type}\n"
        "\n"
        "TARGET:\n"
        f"  POC: ${target:.2f}  <- THIS IS THE ONLY TARGET\n"
        "  Do not use any other target for S3A.\n"
        "  The market is returning to fair value.\n"
        "\n"
        "METRICS:\n"
        f"  Volume:  {vol_ratio:.1f}x (LOW volume = confirmation for S3A)\n"
        f"  CVD:     {cvd_ratio:.1f}x rolling average\n"
        f"  Score:   {score_val}/100\n"
        "\n"
        "CONFIRM BEFORE TRADING:\n"
        "  * Volume IS low (< 0.8x average confirms failed auction)\n"
        "  * Body closed back INSIDE value area\n"
        "  * CVD turned toward POC direction\n"
        "  * POC target gives RR >= 2.5:1\n"
        "\n"
        "RED FLAGS — WAIT if:\n"
        "  X Volume spiking (high volume outside = breakout not failure)\n"
        "  X Body did not close back inside value area\n"
        "  X POC too close — insufficient RR\n"
        "  X Session < 11:00 AM (S3A only valid after 11 AM)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )


def _section_failed_auction_major(data: dict) -> str:
    wick_ratio = data.get("wick_ratio", 0.0)
    vol_ratio = data.get("vol_ratio", 0.0)
    cvd_ratio = data.get("cvd_ratio", 0.0)
    conf = data.get("confidence")
    score_val = conf.score if conf else 0
    approach = data.get("approach")
    approach_type = approach.type if approach else "UNKNOWN"
    return (
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "S3B FAILED AUCTION (MAJOR LEVEL) — SETUP CONTEXT\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "WHAT THIS IS:\n"
        "  A dual-timeframe rejection at a major level.\n"
        "  5m spotter identified the approach (exhaustion/absorption).\n"
        "  1m sniper triggered on the rejection candle.\n"
        "  The level score is >= 8 — widely watched institutional level.\n"
        "\n"
        "REJECTION QUALITY:\n"
        f"  Wick/body ratio: {wick_ratio:.1f}x\n"
        "  (>= 2.0 required, >= 3.0 = strong, >= 4.0 = exceptional)\n"
        f"  Volume:   {vol_ratio:.1f}x average\n"
        f"  CVD turn: {cvd_ratio:.1f}x rolling average\n"
        f"  Approach: {approach_type}\n"
        f"  Score:    {score_val}/100\n"
        "\n"
        "CONFIRM BEFORE TRADING:\n"
        "  * Wick clearly dominates — long wick, small body\n"
        "  * CVD ratio >= 1.0x is supportive (check VERIFICATION DATA)\n"
        "  * Wick/body >= 3.0x with volume >= 1.2x can trade even with weak CVD\n"
        "  * Approach was EXHAUSTION or ABSORPTION (trend ran out)\n"
        "  * Level has not been tested 3+ times today (weakening)\n"
        "\n"
        "RED FLAGS — WAIT if:\n"
        "  X Wick/body < 2.0 (marginal rejection shape)\n"
        "  X CVD ratio < 0.5x AND volume < 1.0x (no participation at all)\n"
        "  X Approach was MOMENTUM (trend too strong to reverse)\n"
        "  X Level tested 3+ times today (losing significance)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
