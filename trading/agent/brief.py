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

    from ..core.asset_registry import get_config, has_daily_expiry
    cfg = get_config(asset)
    minor = cfg["round_interval_minor"]
    atm_strike = round(round(current_price / minor) * minor, 2) if current_price > 0 else 0

    hour = now.hour + now.minute / 60.0
    daily_exp = has_daily_expiry(asset)
    if hour < 11: dte = 0 if daily_exp and now.weekday() == 4 else 1
    elif hour < 13: dte = 1 if daily_exp else 2
    elif hour < 14.5: dte = 0 if daily_exp else 1
    else: dte = 0
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

    # Event candle
    lines += [
        "EVENT CANDLE:",
        f"  O:{event_candle.o:.2f} H:{event_candle.h:.2f} L:{event_candle.l:.2f} C:{event_candle.c:.2f}",
        f"  Body: ${event_candle.body:.3f} | Volume: {volume_ratio:.1f}x avg | CVD: {cvd_at_break:+,.0f}",
        "",
    ]

    # Retest candle
    if retest_candle:
        lines += [
            "RETEST CANDLE (1m):",
            f"  O:{retest_candle.o:.2f} H:{retest_candle.h:.2f} L:{retest_candle.l:.2f} C:{retest_candle.c:.2f}",
            f"  CVD at retest: {cvd_now:+,.0f} | CVD turned: {cvd_turned}",
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
