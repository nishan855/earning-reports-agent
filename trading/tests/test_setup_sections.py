"""Tests for setup-specific brief sections."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from trading.models import Candle, Level, DayContext
from trading.detection.approach import ApproachResult
from trading.detection.confidence import ConfidenceResult


def make_candle(o, h, l, c, v=10000, t=0):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)

def make_conf(score=65):
    return ConfidenceResult(score=score, label="MEDIUM", components={}, details="")

def make_approach(atype="EXHAUSTION"):
    return ApproachResult(type=atype, confidence_pts=15, details="")

def base_brief_args():
    bars = [make_candle(185, 185.5, 184.8, 185.3, v=5000, t=1000*60*i) for i in range(6)]
    level = Level(name="PDL", price=185, score=8, type="support", source="PD", confidence="HIGH")
    dc = DayContext(asset="SPY", day_type="RANGE", bias="NEUTRAL")
    return dict(
        asset="SPY", direction="BULLISH",
        level=level, event_candle=bars[-1], retest_candle=None,
        cvd_at_break=0, cvd_now=0, cvd_turned=False, volume_ratio=1.5,
        day_context=dc, vix=20.0, current_price=185.3, atr=1.5,
        nearest_above=[Level(name="PDH", price=186, score=8, type="resistance", source="PD", confidence="HIGH")],
        nearest_below=[Level(name="ORL", price=184, score=7, type="support", source="OR", confidence="HIGH")],
        session_name="POWER HOUR", session_quality=4, minutes_to_cutoff=120,
        tests_today=0, bars_1m=bars,
    )


# TEST 1 — S1 section
def test_s1():
    from trading.agent.brief import build_brief
    args = base_brief_args()
    args["pattern"] = "LIQUIDITY_GRAB"
    args["setup_data"] = {
        "wick_past": 0.45,
        "cvd_ratio": 2.3,
        "vol_ratio": 1.8,
        "fvg_found": True,
        "fvg_midpoint": 185.20,
        "confidence": make_conf(72),
    }
    brief = build_brief(**args)
    assert "S1 LIQUIDITY GRAB" in brief, "Missing S1 header"
    assert "stop-hunt reversal" in brief, "Missing setup description"
    assert "0.450" in brief, f"Missing wick_past"
    assert "2.3" in brief, "Missing CVD ratio"
    assert "185.20" in brief, "Missing FVG mid"
    print("S1 section: PASS")


# TEST 2 — S2 section
def test_s2():
    from trading.agent.brief import build_brief
    args = base_brief_args()
    args["pattern"] = "OB_DEFENSE"
    args["setup_data"] = {
        "ob": {"ob_high": 185.50, "ob_low": 185.00, "ob_mid": 185.25, "vol_ratio": 1.8, "formed_at": 0, "candle": None},
        "ob_visits": 0,
        "cvd_ratio": 1.5,
        "confidence": make_conf(68),
    }
    brief = build_brief(**args)
    assert "S2 ORDER BLOCK" in brief, "Missing S2 header"
    assert "CONTINUATION" in brief, "Missing CONTINUATION"
    assert "185.50" in brief, "Missing ob_high"
    assert "185.00" in brief, "Missing ob_low"
    assert "FIRST TEST" in brief, "Missing first test note"
    print("S2 section: PASS")


# TEST 3 — S3A section
def test_s3a():
    from trading.agent.brief import build_brief
    args = base_brief_args()
    args["pattern"] = "FAILED_AUCTION_VAR"
    args["setup_data"] = {
        "target": 184.50,
        "details": "outside VAH target=POC $184.50 vol=0.8x cvd=1.2x conf=62",
        "vol_ratio": 0.8,
        "cvd_ratio": 1.2,
        "confidence": make_conf(62),
    }
    brief = build_brief(**args)
    assert "S3A FAILED AUCTION" in brief, "Missing S3A header"
    assert "POC" in brief, "Missing POC"
    assert "184.50" in brief, "Missing target"
    assert "ONLY TARGET" in brief, "Missing ONLY TARGET"
    assert "ABOVE VAH" in brief or "VAH" in brief, "Missing VAR type"
    print("S3A section: PASS")


# TEST 4 — S3B section
def test_s3b():
    from trading.agent.brief import build_brief
    args = base_brief_args()
    args["pattern"] = "FAILED_AUCTION_MAJOR"
    args["setup_data"] = {
        "wick_ratio": 3.2,
        "vol_ratio": 1.6,
        "cvd_ratio": 2.8,
        "confidence": make_conf(74),
        "approach": make_approach("EXHAUSTION"),
    }
    brief = build_brief(**args)
    assert "S3B FAILED AUCTION" in brief, "Missing S3B header"
    assert "dual-timeframe" in brief, "Missing dual-timeframe"
    assert "3.2" in brief, "Missing wick ratio"
    assert "EXHAUSTION" in brief, "Missing approach type"
    print("S3B section: PASS")


# TEST 5 — Unknown pattern
def test_unknown():
    from trading.agent.brief import _build_setup_section
    result = _build_setup_section("UNKNOWN_PATTERN", {})
    assert result == "", f"Expected empty string, got: {result}"
    print("Unknown pattern: PASS")


# TEST 6 — System prompt
def test_prompt():
    from trading.agent.agent import SYSTEM_PROMPT
    assert "REVERSAL" in SYSTEM_PROMPT, "Missing REVERSAL"
    assert "CONTINUATION" in SYSTEM_PROMPT, "Missing CONTINUATION"
    assert "MEAN REVERSION" in SYSTEM_PROMPT, "Missing MEAN REVERSION"
    assert "S1" in SYSTEM_PROMPT, "Missing S1"
    assert "S2" in SYSTEM_PROMPT, "Missing S2"
    assert "S3" in SYSTEM_PROMPT, "Missing S3"
    print("System prompt: PASS")


if __name__ == "__main__":
    test_s1()
    test_s2()
    test_s3a()
    test_s3b()
    test_unknown()
    test_prompt()
    print("\n" + "=" * 50)
    print("ALL SETUP SECTION TESTS PASSED")
    print("=" * 50)
