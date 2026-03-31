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

    def __post_init__(self):
        if self.score >= 75:
            self.label = "HIGH"
        elif self.score >= 50:
            self.label = "MEDIUM"
        else:
            self.label = "LOW"


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
    # Sliding scale — even below-average volume gets some points
    if vol_ratio >= 2.0:
        vol = 20
    elif vol_ratio >= 1.5:
        vol = 15
    elif vol_ratio >= 1.2:
        vol = 10
    elif vol_ratio >= 1.0:
        vol = 5
    elif vol_ratio >= 0.7:
        vol = 0   # average-ish — neutral
    else:
        vol = -5  # suspiciously low — penalize

    components["volume"] = vol
    total += vol

    # ── 3. PRICE SIGNATURE (max 20) ──
    # Displacement + wick quality
    if displacement >= 0.8:
        price = 15
    elif displacement >= 0.5:
        price = 10
    elif displacement >= 0.3:
        price = 5
    else:
        price = 0

    # Wick bonus (rejection quality)
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

    # ── 5. APPROACH CONTEXT (max 15) ──
    approach_pts = approach.confidence_pts  # 0-15 from classifier
    components["approach"] = approach_pts
    total += approach_pts

    # ── CLAMP 0-100 ──
    total = max(0, min(100, total))

    # ── THRESHOLD ──
    threshold = 50
    if day_bias == "NEUTRAL":
        threshold = 60  # slightly stricter on neutral days

    label = "HIGH" if total >= 75 else "MEDIUM" if total >= threshold else "LOW"

    details = (
        f"loc={loc} vol={vol} price={price} cvd={cvd_pts} approach={approach_pts} "
        f"total={total} threshold={threshold}"
        + (" [QUARANTINE]" if cvd_quarantine else "")
    )

    return ConfidenceResult(score=total, label=label, components=components, details=details)
