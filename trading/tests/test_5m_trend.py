"""Tests for 5m trend alignment scoring."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from trading.models import Candle, Level, DayContext
from trading.detection.metrics import get_5m_trend
from trading.detection.confidence import score_signal
from trading.detection.approach import ApproachResult


def make_candle(o, h, l, c, v=10000, t=0):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def make_approach(atype="NEUTRAL", pts=0):
    return ApproachResult(type=atype, confidence_pts=pts, details="")


# TEST 1 — Bearish trend detection
def test_1():
    bars = [make_candle(100, 101, 99, 99.5),   # down
            make_candle(99.5, 100, 98, 98.5),   # down
            make_candle(98.5, 99, 97, 97.5),    # down
            make_candle(97.5, 98, 96, 96.5)]    # down
    assert get_5m_trend(bars) == "BEARISH"
    print("5m trend bearish: PASS")


# TEST 2 — Bullish trend detection
def test_2():
    bars = [make_candle(96, 97, 95, 97),
            make_candle(97, 98, 96, 98),
            make_candle(98, 99, 97, 99),
            make_candle(99, 100, 98, 100)]
    assert get_5m_trend(bars) == "BULLISH"
    print("5m trend bullish: PASS")


# TEST 3 — Neutral detection
def test_3():
    bars = [make_candle(100, 101, 99, 101),   # up
            make_candle(101, 102, 100, 100),   # down
            make_candle(100, 101, 99, 101),    # up
            make_candle(101, 102, 100, 100)]   # down
    assert get_5m_trend(bars) == "NEUTRAL"
    print("5m trend neutral: PASS")


# TEST 4 — Opposed trend scores +8
def test_4():
    level = Level(name="PDL", price=185, score=8, type="support", source="PD", confidence="HIGH")
    conf = score_signal(
        level=level, vol_ratio=1.5, displacement=0.6, wick_ratio=1.5,
        cvd_ratio=1.5, cvd_divergence=False, approach=make_approach("MOMENTUM", 10),
        trend_5m="BEARISH", signal_dir="BULLISH",
    )
    assert conf.components["trend_5m"] == 8, f"Expected 8, got {conf.components['trend_5m']}"
    print("Opposed trend bonus: PASS")


# TEST 5 — Aligned trend scores -10
def test_5():
    level = Level(name="PDL", price=185, score=8, type="support", source="PD", confidence="HIGH")
    conf = score_signal(
        level=level, vol_ratio=1.5, displacement=0.6, wick_ratio=1.5,
        cvd_ratio=1.5, cvd_divergence=False, approach=make_approach("MOMENTUM", 10),
        trend_5m="BULLISH", signal_dir="BULLISH",
    )
    assert conf.components["trend_5m"] == -10, f"Expected -10, got {conf.components['trend_5m']}"
    print("Aligned trend penalty: PASS")


# TEST 6 — Neutral trend scores 0
def test_6():
    level = Level(name="PDL", price=185, score=8, type="support", source="PD", confidence="HIGH")
    conf = score_signal(
        level=level, vol_ratio=1.5, displacement=0.6, wick_ratio=1.5,
        cvd_ratio=1.5, cvd_divergence=False, approach=make_approach("MOMENTUM", 10),
        trend_5m="NEUTRAL", signal_dir="BULLISH",
    )
    assert conf.components["trend_5m"] == 0, f"Expected 0, got {conf.components['trend_5m']}"
    print("Neutral trend: PASS")


# TEST 7 — Score capped at 100
def test_7():
    level = Level(name="52WH", price=185, score=10, type="resistance", source="52W", confidence="HIGH")
    conf = score_signal(
        level=level, vol_ratio=2.5, displacement=0.9, wick_ratio=3.5,
        cvd_ratio=5.0, cvd_divergence=True, approach=make_approach("ABSORPTION", 15),
        trend_5m="BEARISH", signal_dir="BULLISH",
    )
    assert conf.score == 100, f"Expected 100, got {conf.score}"
    print("Score cap: PASS")


# TEST 8 — Score floor at 0
def test_8():
    level = Level(name="VWAP", price=185, score=4, type="dynamic", source="VWAP", confidence="HIGH")
    conf = score_signal(
        level=level, vol_ratio=0.3, displacement=0.1, wick_ratio=0.2,
        cvd_ratio=0.2, cvd_divergence=False, approach=make_approach("NEUTRAL", 0),
        trend_5m="BULLISH", signal_dir="BULLISH",
    )
    assert conf.score >= 0, f"Expected >= 0, got {conf.score}"
    print("Score floor: PASS")


# TEST 9 — Brief contains trend section (opposed)
def test_9():
    from trading.agent.brief import build_brief
    bars = [make_candle(185, 185.5, 184.8, 185.3, v=5000, t=1000*60*i) for i in range(6)]
    level = Level(name="PDL", price=185, score=8, type="support", source="PD", confidence="HIGH")
    dc = DayContext(asset="SPY", day_type="RANGE", bias="NEUTRAL")
    brief = build_brief(
        asset="SPY", pattern="LIQUIDITY_GRAB", direction="BULLISH",
        level=level, event_candle=bars[-1], retest_candle=None,
        cvd_at_break=0, cvd_now=0, cvd_turned=False, volume_ratio=1.5,
        day_context=dc, vix=20, current_price=185.3, atr=1.5,
        nearest_above=[Level(name="PDH", price=186, score=8, type="resistance", source="PD", confidence="HIGH")],
        nearest_below=[Level(name="ORL", price=184, score=7, type="support", source="OR", confidence="HIGH")],
        session_name="POWER HOUR", session_quality=4, minutes_to_cutoff=120,
        tests_today=0, bars_1m=bars, trend_5m="BEARISH", trend_pts=8,
    )
    assert "5M TREND ALIGNMENT" in brief, "Missing 5M TREND ALIGNMENT"
    assert "IDEAL" in brief, "Missing IDEAL"
    assert "+8" in brief, "Missing +8"
    print("Brief trend section: PASS")


# TEST 10 — Brief caution when aligned
def test_10():
    from trading.agent.brief import build_brief
    bars = [make_candle(185, 185.5, 184.8, 185.3, v=5000, t=1000*60*i) for i in range(6)]
    level = Level(name="PDL", price=185, score=8, type="support", source="PD", confidence="HIGH")
    dc = DayContext(asset="SPY", day_type="RANGE", bias="NEUTRAL")
    brief = build_brief(
        asset="SPY", pattern="LIQUIDITY_GRAB", direction="BULLISH",
        level=level, event_candle=bars[-1], retest_candle=None,
        cvd_at_break=0, cvd_now=0, cvd_turned=False, volume_ratio=1.5,
        day_context=dc, vix=20, current_price=185.3, atr=1.5,
        nearest_above=[Level(name="PDH", price=186, score=8, type="resistance", source="PD", confidence="HIGH")],
        nearest_below=[Level(name="ORL", price=184, score=7, type="support", source="OR", confidence="HIGH")],
        session_name="POWER HOUR", session_quality=4, minutes_to_cutoff=120,
        tests_today=0, bars_1m=bars, trend_5m="BULLISH", trend_pts=-10,
    )
    assert "CAUTION" in brief, "Missing CAUTION"
    assert "-10" in brief, "Missing -10"
    print("Brief caution: PASS")


if __name__ == "__main__":
    test_1()
    test_2()
    test_3()
    test_4()
    test_5()
    test_6()
    test_7()
    test_8()
    test_9()
    test_10()
    print("\n" + "=" * 50)
    print("ALL 10 TREND ALIGNMENT TESTS PASSED")
    print("=" * 50)
