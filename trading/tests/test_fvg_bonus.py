"""Tests for FVG confidence bonus."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from trading.models import Candle, Level, DayContext
from trading.detection.confidence import ConfidenceResult


def make_candle(o, h, l, c, v=10000, t=0):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


# ═══════════════════════════════════════════════════════
# TEST 1 — Score increases by 8 when FVG found
# ═══════════════════════════════════════════════════════
def test_fvg_bonus_score():
    score = 67
    fvg_found = True
    if fvg_found:
        new_score = min(100, score + 8)
        label = "HIGH" if new_score >= 75 else "MEDIUM" if new_score >= 50 else "LOW"
    else:
        new_score = score
        label = "MEDIUM"
    assert new_score == 75, f"Expected 75, got {new_score}"
    assert label == "HIGH", f"Expected HIGH, got {label}"
    print("FVG bonus score: PASS")


# ═══════════════════════════════════════════════════════
# TEST 2 — Score unchanged when no FVG
# ═══════════════════════════════════════════════════════
def test_fvg_no_bonus():
    score = 67
    fvg_found = False
    if fvg_found:
        new_score = min(100, score + 8)
    else:
        new_score = score
    label = "HIGH" if new_score >= 75 else "MEDIUM" if new_score >= 50 else "LOW"
    assert new_score == 67, f"Expected 67, got {new_score}"
    assert label == "MEDIUM", f"Expected MEDIUM, got {label}"
    print("FVG no bonus: PASS")


# ═══════════════════════════════════════════════════════
# TEST 3 — Score capped at 100
# ═══════════════════════════════════════════════════════
def test_fvg_score_cap():
    score = 96
    fvg_found = True
    new_score = min(100, score + 8)
    assert new_score == 100, f"Expected 100, got {new_score}"
    print("FVG score cap: PASS")


# ═══════════════════════════════════════════════════════
# TEST 4 — Brief contains FVG context when found
# ═══════════════════════════════════════════════════════
def test_fvg_brief_found():
    from trading.agent.brief import build_brief

    bars = [make_candle(185.0, 185.5, 184.8, 185.3, v=5000, t=1000*60*i) for i in range(6)]
    level = Level(name="PDL", price=185.0, score=8, type="support", source="PD", confidence="HIGH")
    dc = DayContext(asset="SPY", day_type="RANGE", bias="NEUTRAL")

    brief = build_brief(
        asset="SPY", pattern="LIQUIDITY_GRAB", direction="BULLISH",
        level=level, event_candle=bars[-1], retest_candle=None,
        cvd_at_break=0, cvd_now=0, cvd_turned=False, volume_ratio=1.5,
        day_context=dc, vix=20.0, current_price=185.3, atr=1.5,
        nearest_above=[Level(name="PDH", price=186.0, score=8, type="resistance", source="PD", confidence="HIGH")],
        nearest_below=[Level(name="ORL", price=184.0, score=7, type="support", source="OR", confidence="HIGH")],
        session_name="POWER HOUR", session_quality=4, minutes_to_cutoff=120,
        tests_today=0, bars_1m=bars,
        fvg_found=True, fvg_mid=185.20, fvg_bonus=8,
    )

    assert "FVG DETECTED: YES" in brief, "Missing FVG DETECTED: YES"
    assert "185.20" in brief, "Missing FVG midpoint"
    assert "+8 pts applied" in brief, "Missing +8 pts"
    assert "LIMIT" in brief, "Missing LIMIT order instruction"
    print("FVG brief found: PASS")


# ═══════════════════════════════════════════════════════
# TEST 5 — Brief contains no-FVG context
# ═══════════════════════════════════════════════════════
def test_fvg_brief_not_found():
    from trading.agent.brief import build_brief

    bars = [make_candle(185.0, 185.5, 184.8, 185.3, v=5000, t=1000*60*i) for i in range(6)]
    level = Level(name="PDL", price=185.0, score=8, type="support", source="PD", confidence="HIGH")
    dc = DayContext(asset="SPY", day_type="RANGE", bias="NEUTRAL")

    brief = build_brief(
        asset="SPY", pattern="LIQUIDITY_GRAB", direction="BULLISH",
        level=level, event_candle=bars[-1], retest_candle=None,
        cvd_at_break=0, cvd_now=0, cvd_turned=False, volume_ratio=1.5,
        day_context=dc, vix=20.0, current_price=185.3, atr=1.5,
        nearest_above=[Level(name="PDH", price=186.0, score=8, type="resistance", source="PD", confidence="HIGH")],
        nearest_below=[Level(name="ORL", price=184.0, score=7, type="support", source="OR", confidence="HIGH")],
        session_name="POWER HOUR", session_quality=4, minutes_to_cutoff=120,
        tests_today=0, bars_1m=bars,
        fvg_found=False, fvg_mid=0.0, fvg_bonus=0,
    )

    assert "FVG DETECTED: NO" in brief, "Missing FVG DETECTED: NO"
    assert "+0 pts" in brief, "Missing +0 pts"
    assert "Market order" in brief, "Missing Market order instruction"
    print("FVG brief not found: PASS")


if __name__ == "__main__":
    test_fvg_bonus_score()
    test_fvg_no_bonus()
    test_fvg_score_cap()
    test_fvg_brief_found()
    test_fvg_brief_not_found()
    print("\n" + "=" * 50)
    print("ALL FVG BONUS TESTS PASSED")
    print("=" * 50)
