"""
Confidence scoring engine (V3.0 — Weighted Factor Model).

All factors contribute points on a sliding scale.
No binary kills — weak factors subtract, strong factors add.
Total 0-100. Minimum 50 to pass to agent.
"""

from dataclasses import dataclass
from ..models import Level
from .approach import ApproachResult


@dataclass
class ConfidenceResult:
    score: int
    label: str
    components: dict
    details: str
    threshold: int = 50

    def __post_init__(self):
        if self.score >= 75:
            self.label = "HIGH"
        elif self.score >= 50:
            self.label = "MEDIUM"
        else:
            self.label = "LOW"


APPROACH_SCORES = {
    "LIQUIDITY_GRAB": {"AGGRESSIVE_PUSH": 12, "MOMENTUM": 10, "ABSORPTION": 8, "EXHAUSTION": 6, "NEUTRAL": 0},
    "OB_DEFENSE": {"ABSORPTION": 15, "EXHAUSTION": 10, "MOMENTUM": 0, "NEUTRAL": 0, "AGGRESSIVE_PUSH": -5},
    "FAILED_AUCTION_VAR": {"EXHAUSTION": 15, "ABSORPTION": 12, "NEUTRAL": 0, "MOMENTUM": -5, "AGGRESSIVE_PUSH": -8},
    "FAILED_AUCTION_MAJOR": {"EXHAUSTION": 15, "ABSORPTION": 12, "NEUTRAL": 0, "MOMENTUM": -5, "AGGRESSIVE_PUSH": -8},
}


def score_signal(
    level: Level,
    vol_ratio: float,
    displacement: float,
    wick_ratio: float,
    cvd_ratio: float,
    cvd_divergence: bool,
    approach: ApproachResult,
    cvd_quarantine: bool = False,
    day_bias: str = "NEUTRAL",
    trend_5m: str = "NEUTRAL",
    signal_dir: str = "",
    setup_type: str = "LIQUIDITY_GRAB",
    tests_today: int = 0,
) -> ConfidenceResult:
    components = {}
    total = 0

    # ── 1. LOCATION QUALITY (max 25) ──
    # Higher score levels = more institutional memory
    if level.score >= 10:
        loc = 25
    elif level.score >= 9:
        loc = 22
    elif level.score >= 8:
        loc = 18
    elif level.score >= 7:
        loc = 14
    else:
        loc = 8

    # Confluence bonus for volume-sourced levels (developing and prior day)
    if level.source in ("VOLUME", "PD_VOLUME", "VOLUME_PROFILE"):
        loc = min(25, loc + 3)

    components["location"] = loc
    total += loc

    # ── 2. VOLUME SIGNATURE (max 20, min -5) ──
    if setup_type == "FAILED_AUCTION_VAR":
        # S3A: LOW volume = GOOD (failed auction = nobody wants to trade outside value)
        if vol_ratio <= 0.5:
            vol = 15  # very low — strong failed auction signal
        elif vol_ratio <= 0.8:
            vol = 10  # low — confirms rejection
        elif vol_ratio <= 1.2:
            vol = 5   # average — neutral
        elif vol_ratio <= 1.5:
            vol = 0   # elevated — less clear
        else:
            vol = -5  # high volume outside value = possible breakout, not failure
    else:
        # Standard: high volume = good (stops triggering, institutional participation)
        if vol_ratio >= 2.0:
            vol = 20
        elif vol_ratio >= 1.5:
            vol = 15
        elif vol_ratio >= 1.2:
            vol = 10
        elif vol_ratio >= 1.0:
            vol = 5
        elif vol_ratio >= 0.7:
            vol = 0
        else:
            vol = -5

    components["volume"] = vol
    total += vol

    # ── 3. PRICE SIGNATURE (max 20) ──
    if setup_type == "LIQUIDITY_GRAB_5M":
        # V4.0: INVERTED for sweeps — high wick rejection = good
        # displacement here IS wick_rejection (wick_past / bar_range)
        if displacement >= 0.7:
            price = 15  # mostly wick, tiny body = strong rejection
        elif displacement >= 0.5:
            price = 10
        elif displacement >= 0.3:
            price = 5
        else:
            price = 0   # too much body = breakout shape, not rejection
        # No additive wick bonus — wick IS the primary metric for sweeps
    else:
        # Standard displacement + wick bonus for other setups
        if displacement >= 0.8:
            price = 15
        elif displacement >= 0.5:
            price = 10
        elif displacement >= 0.3:
            price = 5
        else:
            price = 0
        if wick_ratio >= 3.0:
            price = min(20, price + 8)
        elif wick_ratio >= 2.0:
            price = min(20, price + 5)
        elif wick_ratio >= 1.0:
            price = min(20, price + 2)

    components["price"] = price
    total += price

    # ── 4. CVD SIGNATURE (max 25, min -5) ──
    if cvd_quarantine:
        cvd_pts = min(8, 5 if cvd_ratio >= 1.0 else 0)
    else:
        if cvd_ratio >= 4.0:
            cvd_pts = 25
        elif cvd_ratio >= 2.0:
            cvd_pts = 18
        elif cvd_ratio >= 1.0:
            cvd_pts = 10
        elif cvd_ratio >= 0.5:
            cvd_pts = 3
        else:
            cvd_pts = -5  # CVD opposing — penalize

        if cvd_divergence:
            cvd_pts = min(25, cvd_pts + 10)

    components["cvd"] = cvd_pts
    total += cvd_pts

    # ── 5. APPROACH CONTEXT (setup-specific, Section 7) ──
    approach_table = APPROACH_SCORES.get(setup_type, APPROACH_SCORES["LIQUIDITY_GRAB"])
    approach_pts = approach_table.get(approach.type if approach else "NEUTRAL", 0)
    components["approach"] = approach_pts
    total += approach_pts

    # ── 6. 5M TREND ALIGNMENT (max +8, min -10) ──
    # For LG: opposed trend = good (drove price into level), aligned = suspicious
    trend_pts = 0
    if trend_5m != "NEUTRAL" and signal_dir:
        opposed = (
            (trend_5m == "BEARISH" and signal_dir == "BULLISH")
            or (trend_5m == "BULLISH" and signal_dir == "BEARISH")
        )
        aligned = (
            (trend_5m == "BULLISH" and signal_dir == "BULLISH")
            or (trend_5m == "BEARISH" and signal_dir == "BEARISH")
        )
        if opposed:
            trend_pts = 8
        elif aligned:
            trend_pts = -10
    components["trend_5m"] = trend_pts
    total += trend_pts

    # ── 7. TEST COUNT PENALTY (Section 6) ──
    test_penalty = 0
    if tests_today == 2:
        test_penalty = -5
    elif tests_today == 3:
        test_penalty = -12
    elif tests_today >= 4:
        test_penalty = -20
    components["test_count"] = test_penalty
    total += test_penalty

    # Hard gate: 4+ tests blocks signal entirely
    if tests_today >= 4:
        return ConfidenceResult(score=0, label="LOW", components=components, details=f"BLOCKED: level tested {tests_today}x today")

    # ── CLAMP 0-100 ──
    total = max(0, min(100, total))

    # ── THRESHOLD ──
    threshold = 50
    if day_bias == "NEUTRAL":
        threshold = 60  # slightly stricter on neutral days

    label = "HIGH" if total >= 75 else "MEDIUM" if total >= threshold else "LOW"

    details = (
        f"loc={loc} vol={vol} price={price} cvd={cvd_pts} approach={approach_pts} trend={trend_pts} "
        f"total={total} threshold={threshold}"
        + (" [QUARANTINE]" if cvd_quarantine else "")
    )

    return ConfidenceResult(score=total, label=label, components=components, details=details, threshold=threshold)
