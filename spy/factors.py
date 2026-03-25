from .models import (
    Candle, Level, OpeningRange, PreMarketData, VixData,
    StopHunt, PriceAction, FactorResult, FactorEngineResult,
    TrendDirection, SessionId,
)
from .market_utils import (
    detect_trend, detect_stop_hunt, calc_vwap, calc_atr,
    calc_avg_vol, detect_price_action, MIN_RR,
)
from .levels import find_nearest_levels, calc_rr
from .sessions import get_session, is_trading_allowed


def interpret_vix(vix: float) -> VixData:
    if vix < 15:  return VixData(vix, f"{vix:.1f} CALM",     "#00d97e", True,  1.00, "Low fear — trend trades work best")
    if vix < 20:  return VixData(vix, f"{vix:.1f} NORMAL",   "#3b82f6", True,  1.00, "Normal conditions — standard sizing")
    if vix < 25:  return VixData(vix, f"{vix:.1f} ELEVATED", "#f59e0b", True,  0.75, "Elevated — reduce size 25%")
    if vix < 30:  return VixData(vix, f"{vix:.1f} HIGH",     "#f97316", False, 0.50, "High VIX — reduce size 50%, wider stops")
    return              VixData(vix, f"{vix:.1f} DANGER",    "#ef4444", False, 0.00, "Extreme fear — avoid new longs")


def run_factor_engine(
    closed_1m:  list[Candle],
    closed_5m:  list[Candle],
    closed_15m: list[Candle],
    c_daily:    list[Candle],
    cvd:        float,
    vix_val:    float | None,
    or_data:    OpeningRange | None,
    pm_data:    PreMarketData | None,
    levels:     list[Level],
) -> FactorEngineResult | None:
    if not closed_1m or not closed_5m or not closed_15m:
        return None

    last     = closed_1m[-1]
    prev     = closed_1m[-2] if len(closed_1m) > 1 else last
    sess     = get_session()
    vwap     = calc_vwap(closed_1m)
    atr      = calc_atr(closed_1m)
    avg_vol  = calc_avg_vol(closed_1m)
    vix_data = interpret_vix(vix_val or 18.0)

    d_trend  = detect_trend(c_daily if len(c_daily) >= 30 else closed_15m)
    t15      = detect_trend(closed_15m)
    t5       = detect_trend(closed_5m)
    sh       = detect_stop_hunt(closed_1m)
    res, sup = find_nearest_levels(last.c, levels)
    rr_calc  = calc_rr(last.c, d_trend.value, atr, res, sup)

    bias = (d_trend if d_trend not in (TrendDirection.RANGING, TrendDirection.UNKNOWN)
            else t15 if t15 not in (TrendDirection.RANGING, TrendDirection.UNKNOWN)
            else t5 if t5 != TrendDirection.RANGING
            else TrendDirection.RANGING)

    pa = detect_price_action(last, prev, levels, bias)

    # CONTEXT
    c1_ok = d_trend not in (TrendDirection.RANGING, TrendDirection.UNKNOWN)
    C1 = FactorResult(
        id="c1", layer="CONTEXT", label="DAILY TREND",
        ok=c1_ok, is_bonus=False, weight=2,
        val=d_trend.value,
        color="#00d97e" if d_trend == TrendDirection.BULLISH else "#ff4d6d" if d_trend == TrendDirection.BEARISH else "#f59e0b",
        reason=f"Daily chart {d_trend.value} — institutional bias confirmed, trade with trend" if c1_ok
               else "Daily ranging — no clear institutional direction, higher risk",
        missing=None if c1_ok else "Need clear HH+HL (bullish) or LH+LL (bearish) on daily chart",
    )

    c2_ok = vix_data.tradeable
    C2 = FactorResult(
        id="c2", layer="CONTEXT", label="VIX / FEAR",
        ok=c2_ok, is_bonus=False, weight=1,
        val=vix_data.label, color=vix_data.color,
        reason=vix_data.note,
        missing=f"VIX {vix_data.value:.1f} elevated — reduces signal reliability" if not c2_ok else None,
    )

    c3_ok = t15 == t5 and t15 not in (TrendDirection.RANGING, TrendDirection.UNKNOWN)
    C3 = FactorResult(
        id="c3", layer="CONTEXT", label="INTRADAY BIAS",
        ok=c3_ok, is_bonus=False, weight=2,
        val=f"{t15.value} <- 15m & 5m agree" if c3_ok else f"15m:{t15.value} / 5m:{t5.value}",
        color="#00d97e" if c3_ok and t15 == TrendDirection.BULLISH else "#ff4d6d" if c3_ok else "#f59e0b",
        reason=f"15m and 5m both {t15.value} — intraday confirmed" if c3_ok
               else f"Timeframes conflict: 15m={t15.value} vs 5m={t5.value}",
        missing=None if c3_ok else "Need 15m and 5m to agree on direction",
    )

    # SETUP
    or_broke = (or_data and or_data.complete and (
        (last.c > or_data.high and bias == TrendDirection.BULLISH) or
        (last.c < or_data.low  and bias == TrendDirection.BEARISH)
    ))
    or_inside = or_data and or_data.complete and or_data.low <= last.c <= or_data.high
    s1_ok = not (or_data and or_data.complete) or or_broke

    S1 = FactorResult(
        id="s1", layer="SETUP", label="OPENING RANGE",
        ok=s1_ok, is_bonus=False, weight=3,
        val=("Waiting for 9:30" if not or_data
             else f"Forming ({or_data.bar_count}/30 min)" if not or_data.complete
             else f"BROKE {'HIGH' if bias == TrendDirection.BULLISH else 'LOW'} ${or_data.high if bias == TrendDirection.BULLISH else or_data.low:.2f}" if or_broke
             else f"Inside ${or_data.low:.2f}-${or_data.high:.2f}" if or_inside
             else "Failed to break"),
        color="#00d97e" if or_broke else "#f59e0b" if or_inside or not or_data else "#ff4d6d",
        reason=("OR not complete — waiting for 10:00 ET" if not or_data or not or_data.complete
                else f"SPY broke {'above ORH' if bias == TrendDirection.BULLISH else 'below ORL'} — directional day" if or_broke
                else "Inside OR — choppy, lower probability"),
        missing=(f"Need close {'above ORH' if bias == TrendDirection.BULLISH else 'below ORL'} "
                 f"${or_data.high if bias == TrendDirection.BULLISH else or_data.low:.2f}"
                 if not s1_ok and or_data else None),
    )

    s2_ok = pa is not None
    S2 = FactorResult(
        id="s2", layer="SETUP", label="KEY LEVEL P/A",
        ok=s2_ok, is_bonus=False, weight=2,
        val=(f"{pa.type} @ {pa.level.label} ${pa.level.price:.2f}" if pa
             else f"Approaching {res.label} ${res.price:.2f}" if res else "No level in play"),
        color="#00d97e" if s2_ok else "#475569",
        reason=(f"{pa.type} at {pa.level.label} — price respecting level" if pa
                else "No price action at key level — waiting for trigger"),
        missing=None if s2_ok else "Wait for BREAKOUT/RETEST/REJECTION at OR/PDH/PDL/round number",
    )

    vol_spike = avg_vol > 0 and last.v > avg_vol * 1.5
    cvd_ok    = (cvd > 0 if bias == TrendDirection.BULLISH
                 else cvd < 0 if bias == TrendDirection.BEARISH else False)
    s3_ok = vol_spike and cvd_ok
    S3 = FactorResult(
        id="s3", layer="SETUP", label="VOLUME + CVD",
        ok=s3_ok, is_bonus=False, weight=2,
        val=f"Vol {last.v/avg_vol:.1f}x | CVD {'+' if cvd >= 0 else ''}{int(cvd):,}" if avg_vol > 0 else f"CVD {int(cvd):,}",
        color="#00d97e" if s3_ok else "#f59e0b" if vol_spike or cvd_ok else "#475569",
        reason=(f"{last.v/avg_vol:.1f}x vol spike + {'positive' if cvd > 0 else 'negative'} CVD — institutional conviction" if s3_ok
                else "No institutional backing" if not vol_spike and not cvd_ok
                else "CVD aligned but volume thin" if not vol_spike
                else f"Volume spiked but CVD opposes {bias.value} bias"),
        missing=(f"Need: {'vol >1.5x avg' if not vol_spike else ''}"
                 f"{' + ' if not vol_spike and not cvd_ok else ''}"
                 f"{'positive CVD' if not cvd_ok and bias == TrendDirection.BULLISH else 'negative CVD' if not cvd_ok else ''}"
                 if not s3_ok else None),
    )

    s4_ok = sh is not None
    S4 = FactorResult(
        id="s4", layer="SETUP", label="STOP HUNT",
        ok=s4_ok, is_bonus=True, weight=2,
        val=f"{sh.type} sweep @ ${sh.level:.2f}" if sh else "Not detected",
        color="#a78bfa" if s4_ok else "#1e2533",
        reason=(f"Institutions swept {'retail longs below support' if sh.type == 'BULLISH' else 'retail shorts above resistance'} — highest probability setup" if sh
                else "No stop hunt — standard entry conditions"),
        missing=None,
    )

    # TIMING
    t1_ok = sess.quality >= 3
    T1 = FactorResult(
        id="t1", layer="TIMING", label="SESSION",
        ok=t1_ok, is_bonus=False, weight=2,
        val=sess.label, color=sess.color,
        reason=(f"{sess.label} — {'best session, institutional order flow active' if sess.id == SessionId.POWER else 'adequate session'}" if t1_ok
                else f"{sess.label} — {'dead zone: low volume, avoid' if sess.id == SessionId.DEAD else 'suboptimal session'}"),
        missing=None if t1_ok else "Wait for Power Hour or Afternoon session",
    )

    rr    = rr_calc["rr"]
    t2_ok = rr >= MIN_RR
    T2 = FactorResult(
        id="t2", layer="TIMING", label="RISK/REWARD",
        ok=t2_ok, is_bonus=False, weight=2,
        val=f"{rr}:1 -> {res.label if res and bias == TrendDirection.BULLISH else sup.label if sup else 'target'}" if rr > 0 else "Calculating...",
        color="#00d97e" if rr >= 3 else "#3b82f6" if rr >= MIN_RR else "#ff4d6d",
        reason=(f"{rr}:1 — clear path to next level" if t2_ok
                else f"Only {rr}:1 — {'resistance' if bias == TrendDirection.BULLISH else 'support'} too close" if rr > 0
                else "Cannot compute RR — no clear target level"),
        missing=f"Need >={MIN_RR}:1 RR" if not t2_ok else None,
    )

    # SCORING
    context_score = sum([c1_ok, c2_ok, c3_ok])
    core_setup    = sum([s1_ok, s2_ok, s3_ok])
    timing_score  = sum([t1_ok, t2_ok])
    total_score   = context_score + core_setup + timing_score

    threshold = sess.signal_threshold
    all_ok = (
        is_trading_allowed() and (
            (context_score >= 2 and core_setup >= 2 and timing_score == 2 and total_score >= threshold) or
            (s4_ok and context_score == 3 and core_setup >= 1 and timing_score == 2 and total_score >= threshold - 1)
        )
    )

    return FactorEngineResult(
        factors=[C1, C2, C3, S1, S2, S3, S4, T1, T2],
        context_score=context_score,
        setup_score=core_setup + (1 if s4_ok else 0),
        timing_score=timing_score,
        total_score=total_score,
        all_ok=all_ok,
        bias=bias,
        vwap=vwap, atr=atr,
        rr=rr_calc["rr"], sl=rr_calc["sl"],
        tp1=rr_calc["tp1"], tp2=rr_calc["tp2"],
        near_resistance=res, near_support=sup,
        price_action=pa, stop_hunt=sh,
        or_data=or_data,
        last_price=last.c,
        evaluated_at=last.t,
    )
