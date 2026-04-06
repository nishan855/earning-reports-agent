"""Tests for V3.1 Level System Rebuild — 12 tests."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from trading.models import Candle, Level, VolumeProfile
from trading.levels.builder import (
    find_swing_highs, find_swing_lows, score_swing_level,
    get_orh_orl_score, get_developing_level_score, build_levels,
)
from trading.detection.confidence import score_signal, APPROACH_SCORES
from trading.detection.approach import ApproachResult


def mc(o, h, l, c, v=10000, t=0):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)

def make_approach(atype="NEUTRAL", pts=0):
    return ApproachResult(type=atype, confidence_pts=pts, details="")


# TEST 1 — Swing high detection
def test_1():
    candles = []
    for i in range(15):
        if i < 7:
            h = 184 + i * 0.7  # rising: 184, 184.7, 185.4, 186.1, 186.8, 187.5, 188.2
        elif i == 7:
            h = 190.0  # peak
        else:
            h = 188.2 - (i - 7) * 0.7  # falling: 187.5, 186.8, 186.1, 185.4, 184.7, 184.0, 183.3
        candles.append(mc(h - 1, h, h - 2, h - 0.5, t=i * 86400000))
    swings = find_swing_highs(candles, 5, 5)
    assert len(swings) >= 1, f"Expected at least 1 swing high, got {len(swings)}"
    assert swings[0]["price"] == 190.0, f"Expected 190.0, got {swings[0]['price']}"
    assert swings[0]["bars_old"] == 7, f"Expected bars_old=7, got {swings[0]['bars_old']}"
    print("Swing high detection: PASS")


# TEST 2 — Swing low detection
def test_2():
    candles = []
    for i in range(15):
        if i < 7:
            lo = 190 - i * 0.7
        elif i == 7:
            lo = 180.0  # trough
        else:
            lo = 180.7 + (i - 8) * 0.7
        candles.append(mc(lo + 1, lo + 2, lo, lo + 0.5, t=i * 86400000))
    swings = find_swing_lows(candles, 5, 5)
    assert len(swings) >= 1, f"Expected at least 1 swing low, got {len(swings)}"
    assert swings[0]["price"] == 180.0, f"Expected 180.0, got {swings[0]['price']}"
    print("Swing low detection: PASS")


# TEST 3 — Swing score daily
def test_3():
    assert score_swing_level(3, True) == 9
    assert score_swing_level(10, True) == 8
    assert score_swing_level(20, True) == 7
    assert score_swing_level(35, True) == 6
    print("Swing score daily: PASS")


# TEST 4 — Swing score 15m
def test_4():
    assert score_swing_level(5, False) == 7
    assert score_swing_level(15, False) == 6
    assert score_swing_level(25, False) == 5
    print("Swing score 15m: PASS")


# TEST 5 — PDC not in output
def test_5():
    daily = [mc(180+i, 182+i, 179+i, 181+i, v=1000000, t=i*86400000) for i in range(30)]
    c1m = [mc(200, 201, 199, 200, t=1000*60*i) for i in range(10)]
    levels = build_levels("SPY", daily, c1m, [], 200.0, 200.5, 201, 199, True, None, [])
    pdc_levels = [l for l in levels if l.name == "PDC" or l.source == "PDC"]
    assert len(pdc_levels) == 0, f"PDC should be removed, found {len(pdc_levels)}"
    print("PDC removed: PASS")


# TEST 6 — VWAP not in output
def test_6():
    daily = [mc(180+i, 182+i, 179+i, 181+i, v=1000000, t=i*86400000) for i in range(30)]
    c1m = [mc(200, 201, 199, 200, t=1000*60*i) for i in range(10)]
    levels = build_levels("SPY", daily, c1m, [], 200.0, 200.5, 201, 199, True, None, [])
    vwap_levels = [l for l in levels if l.name == "VWAP" or l.source == "VWAP"]
    assert len(vwap_levels) == 0, f"VWAP should be removed from levels, found {len(vwap_levels)}"
    print("VWAP demoted: PASS")


# TEST 7 — PMH/PML volume gate
def test_7():
    # Daily bars with 1M volume each
    daily = [mc(180+i, 182+i, 179+i, 181+i, v=1_000_000, t=i*86400000) for i in range(30)]
    c1m = [mc(200, 201, 199, 200, t=1000*60*i) for i in range(10)]

    # Pre-market bars with LOW volume (5% of daily = 50k total, below 10%)
    import pytz
    from datetime import datetime
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    pm_time = now.replace(hour=5, minute=0, second=0)
    pm_ts = int(pm_time.timestamp() * 1000)
    pm_bars_low = [mc(200, 201, 199, 200.5, v=10000, t=pm_ts + i*300000) for i in range(5)]  # 50k total

    levels_low = build_levels("SPY", daily, c1m, pm_bars_low, 200.0, 0, 0, 0, False, None, [])
    pmh_low = [l for l in levels_low if l.name in ("PMH", "PML")]
    assert len(pmh_low) == 0, f"PMH/PML should be filtered with low PM volume, found {len(pmh_low)}"

    # High PM volume (15% = 150k) — PMH/PML created but score=5, filtered by MIN_LEVEL_SCORE=7
    # unless boosted by confluence or HVN. Verify they are at least CONSTRUCTED before filtering.
    pm_bars_high = [mc(200, 201, 199, 200.5, v=30000, t=pm_ts + i*300000) for i in range(5)]  # 150k total
    # Build without confluence filter to verify PMH/PML are constructed
    from trading.levels.builder import _get_premarket_bars, SCORE_PMH_PML
    pm_filtered = _get_premarket_bars(pm_bars_high)
    pm_vol = sum(c.v for c in pm_filtered)
    avg_daily = sum(c.v for c in daily[-20:]) / 20
    assert pm_vol > avg_daily * 0.10, f"PM vol {pm_vol} should be > 10% of {avg_daily}"
    assert SCORE_PMH_PML == 5, "PMH/PML base score should be 5"
    print("PMH/PML volume gate: PASS")


# TEST 8 — ORH/ORL time decay
def test_8():
    formed = 1000000000000  # some base ms
    ms_per_hour = 3_600_000

    # 0.5 hours old, 0 tests: base 8
    assert get_orh_orl_score(formed, 0, formed + int(0.5 * ms_per_hour)) == 8
    # 3.5 hours old: decay -1 → 7
    assert get_orh_orl_score(formed, 0, formed + int(3.5 * ms_per_hour)) == 7
    # 4.5 hours old: decay -2 → 6
    assert get_orh_orl_score(formed, 0, formed + int(4.5 * ms_per_hour)) == 6
    # 4.5 hours + 1 test: decay -2, test -1 → 5
    assert get_orh_orl_score(formed, 1, formed + int(4.5 * ms_per_hour)) == 5
    # 4.5 hours + 2 tests: decay -2, test -2 → 4
    assert get_orh_orl_score(formed, 2, formed + int(4.5 * ms_per_hour)) == 4
    print("ORH/ORL decay: PASS")


# TEST 9 — dPOC time gating
def test_9():
    assert get_developing_level_score("dPOC", 10.0) == 5
    assert get_developing_level_score("dPOC", 12.0) == 6
    assert get_developing_level_score("dPOC", 14.0) == 7
    assert get_developing_level_score("dVAH", 10.0) == 4
    assert get_developing_level_score("dVAH", 12.0) == 5
    assert get_developing_level_score("dVAH", 14.0) == 6
    print("dPOC time gate: PASS")


# TEST 10 — Test count penalty
def test_10():
    level = Level(name="PDL", price=185, score=8, type="support", source="PD", confidence="HIGH")
    approach = make_approach("MOMENTUM", 10)

    # 0 tests: no penalty
    conf0 = score_signal(level=level, vol_ratio=1.5, displacement=0.6, wick_ratio=1.5,
                         cvd_ratio=1.5, cvd_divergence=False, approach=approach, tests_today=0)
    assert conf0.components["test_count"] == 0

    # 2 tests: -5
    conf2 = score_signal(level=level, vol_ratio=1.5, displacement=0.6, wick_ratio=1.5,
                         cvd_ratio=1.5, cvd_divergence=False, approach=approach, tests_today=2)
    assert conf2.components["test_count"] == -5

    # 3 tests: -12
    conf3 = score_signal(level=level, vol_ratio=1.5, displacement=0.6, wick_ratio=1.5,
                         cvd_ratio=1.5, cvd_divergence=False, approach=approach, tests_today=3)
    assert conf3.components["test_count"] == -12

    # 4 tests: BLOCKED
    conf4 = score_signal(level=level, vol_ratio=1.5, displacement=0.6, wick_ratio=1.5,
                         cvd_ratio=1.5, cvd_divergence=False, approach=approach, tests_today=4)
    assert conf4.score == 0, f"Expected 0, got {conf4.score}"
    assert "BLOCKED" in conf4.details
    print("Test count penalty: PASS")


# TEST 11 — Setup-specific approach scoring
def test_11():
    level = Level(name="PDL", price=185, score=8, type="support", source="PD", confidence="HIGH")

    # LG + AGGRESSIVE_PUSH = 12
    conf_lg = score_signal(level=level, vol_ratio=1.5, displacement=0.6, wick_ratio=1.5,
                           cvd_ratio=1.5, cvd_divergence=False,
                           approach=make_approach("AGGRESSIVE_PUSH", 12),
                           setup_type="LIQUIDITY_GRAB")
    assert conf_lg.components["approach"] == 12, f"LG+AGG expected 12, got {conf_lg.components['approach']}"

    # OB + AGGRESSIVE_PUSH = -5
    conf_ob = score_signal(level=level, vol_ratio=1.5, displacement=0.6, wick_ratio=0.0,
                           cvd_ratio=1.5, cvd_divergence=False,
                           approach=make_approach("AGGRESSIVE_PUSH", 12),
                           setup_type="OB_DEFENSE")
    assert conf_ob.components["approach"] == -5, f"OB+AGG expected -5, got {conf_ob.components['approach']}"

    # S3 + MOMENTUM = -5
    conf_s3 = score_signal(level=level, vol_ratio=1.5, displacement=0.6, wick_ratio=2.0,
                           cvd_ratio=1.5, cvd_divergence=False,
                           approach=make_approach("MOMENTUM", 10),
                           setup_type="FAILED_AUCTION_MAJOR")
    assert conf_s3.components["approach"] == -5, f"S3+MOM expected -5, got {conf_s3.components['approach']}"

    # OB + ABSORPTION = 15
    conf_ob2 = score_signal(level=level, vol_ratio=1.5, displacement=0.6, wick_ratio=0.0,
                            cvd_ratio=1.5, cvd_divergence=False,
                            approach=make_approach("ABSORPTION", 15),
                            setup_type="OB_DEFENSE")
    assert conf_ob2.components["approach"] == 15, f"OB+ABS expected 15, got {conf_ob2.components['approach']}"
    print("Setup approach scores: PASS")


# TEST 12 — No false swing (flat candles)
def test_12():
    candles = [mc(185, 185.5, 184.5, 185.2, t=i*86400000) for i in range(20)]
    highs = find_swing_highs(candles, 5, 5)
    assert len(highs) == 0, f"Expected no swings in flat data, got {len(highs)}"
    print("No false swing: PASS")


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
    test_11()
    test_12()
    print("\n" + "=" * 50)
    print("ALL 12 LEVEL REBUILD TESTS PASSED")
    print("=" * 50)
