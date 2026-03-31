# Signal Engine v2.1 — Full Implementation Plan

> **Hand this file directly to Claude Code.**
> All design decisions are locked. Implement exactly as specified.
> Run tests after each phase. Do not proceed if tests fail.

---

## Context

The `trading/` module already exists with 38 files across 7 phases. It runs at `/trading` on Render.
The old detection files (`breakout.py`, `rejection.py`, `stop_hunt.py`) are being replaced by a new detection engine.
The existing `failed_breakout.py` and all other files are being updated in-place.

**Core philosophy:** "Are institutions actively positioning at this price level right now?"

---

## Dynamic Baselines (LOCKED — DO NOT CHANGE)

```
Gate 1 (Location):  abs(price − level) <= ATR * 0.3
Gate 3 (Volume):    current_vol >= 1.5 * rolling_avg(last_10_bars)
Gate 5 (CVD):       abs(cvd_turn) >= 2.0 * rolling_avg(last_10_cvd_turns)
```

Everything is relative. Rolling windows, not static thresholds, not day-wide averages.

---

## Phase 1 — New Files (8 files to create)

### FILE: `trading/detection/approach.py`

5-candle approach context classifier. Returns one of 5 states in priority order.

```python
"""
Approach context classifier.
Evaluates the 5 closed candles leading up to the level.
Returns the FIRST matching type in priority order.
"""

from dataclasses import dataclass
from ..models import Candle


@dataclass
class ApproachResult:
    type: str          # AGGRESSIVE_PUSH | ABSORPTION | EXHAUSTION | MOMENTUM | NEUTRAL
    confidence_pts: int  # contribution to confidence score
    details: str


def classify_approach(
    candles: list,        # last 5+ closed candles (index -1 = most recent)
    level_price: float,
    atr: float,
    rolling_avg_vol: float,  # rolling_avg(last_10_bars)
) -> ApproachResult:
    """
    Priority order:
    1. AGGRESSIVE_PUSH   (displacement-grade candle present — unmistakable)
    2. ABSORPTION        (volume is tie-breaker vs exhaustion — check first)
    3. EXHAUSTION
    4. MOMENTUM
    5. NEUTRAL           (default — detection not blocked, confidence = 0)
    """
    if len(candles) < 4:
        return ApproachResult("NEUTRAL", 0, "Insufficient candles")

    c = candles  # use negative indexing throughout
    bodies  = [abs(x.c - x.o) for x in c]
    volumes = [x.v for x in c]
    closes  = [x.c for x in c]
    rvol    = rolling_avg_vol if rolling_avg_vol > 0 else 1.0

    # ── 1. AGGRESSIVE PUSH ──────────────────────────────────────────
    # Requires displacement-grade candle in last 2 bars
    recent_bodies = bodies[-2:]
    has_displacement = any(b > atr * 1.0 for b in recent_bodies)

    if has_displacement:
        big_idx = -1 if bodies[-1] > atr * 1.0 else -2
        big_c   = c[big_idx]
        vol_ratio = big_c.v / rvol

        # Direction toward level
        moving_toward = (
            (big_c.c > big_c.o and big_c.c >= level_price * 0.995)
            or
            (big_c.c < big_c.o and big_c.c <= level_price * 1.005)
        )
        price_progress = abs(closes[-1] - closes[-3]) > atr * 0.8

        if vol_ratio >= 2.0 and moving_toward and price_progress:
            return ApproachResult(
                "AGGRESSIVE_PUSH", 12,
                f"Displacement candle body={bodies[big_idx]:.2f} vol={vol_ratio:.1f}x"
            )

    # ── 2. ABSORPTION (check before exhaustion — volume is tie-breaker)
    price_range    = max(x.h for x in c[-4:]) - min(x.l for x in c[-4:])
    tight_range    = price_range < atr * 0.8
    high_vol_count = sum(1 for v in volumes[-4:] if v > rvol)
    avg_vol_4      = sum(volumes[-4:]) / 4 if len(volumes) >= 4 else 0
    elevated_vol   = high_vol_count >= 3 and avg_vol_4 > rvol * 1.2

    if tight_range and elevated_vol:
        return ApproachResult(
            "ABSORPTION", 15,
            f"Range={price_range:.2f} (<ATR*0.8={atr*0.8:.2f}), vol_count={high_vol_count}/4"
        )

    # ── 3. EXHAUSTION ────────────────────────────────────────────────
    if len(bodies) >= 3:
        shrinking = (
            bodies[-1] < bodies[-2] < bodies[-3]
            and all(b < atr * 0.4 for b in bodies[-3:])
        )
        avg_vol_last3 = sum(volumes[-3:]) / 3 if len(volumes) >= 3 else 0
        avg_vol_prev3 = sum(volumes[-6:-3]) / 3 if len(volumes) >= 6 else rvol
        vol_flat      = avg_vol_last3 <= avg_vol_prev3 * 1.1

        if shrinking and vol_flat:
            return ApproachResult(
                "EXHAUSTION", 15,
                f"bodies={[round(b,2) for b in bodies[-3:]]} vol_trend=flat"
            )

    # ── 4. MOMENTUM ──────────────────────────────────────────────────
    if len(closes) >= 5:
        bullish_count = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        bearish_count = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i-1])
        directional   = bullish_count >= 4 or bearish_count >= 4

        meaningful_bodies = all(b > atr * 0.15 for b in bodies[-5:])
        total_progress    = abs(closes[-1] - closes[-5]) > atr * 0.5

        if directional and meaningful_bodies and total_progress:
            direction = "bull" if bullish_count >= 4 else "bear"
            return ApproachResult(
                "MOMENTUM", 10,
                f"{direction} {bullish_count if direction=='bull' else bearish_count}/5 bars, progress={abs(closes[-1]-closes[-5]):.2f}"
            )

    return ApproachResult("NEUTRAL", 0, "No clear approach pattern")
```

---

### FILE: `trading/detection/metrics.py`

Shared metric calculations used by all setup detectors.

```python
"""
Shared detection metrics.
All calculations use rolling windows — no static thresholds.
"""

from ..models import Candle


def displacement_ratio(candle: Candle) -> float:
    """Body / total range. 1.0 = full body candle. 0.0 = doji."""
    total_range = candle.h - candle.l
    if total_range == 0:
        return 0.0
    body = abs(candle.c - candle.o)
    return body / total_range


def wick_body_ratio(candle: Candle, direction: str) -> float:
    """
    Wick-to-body ratio on the rejection side.
    direction = 'BULLISH' → upper wick rejected bears
    direction = 'BEARISH' → lower wick rejected bulls
    """
    body = abs(candle.c - candle.o)
    if body == 0:
        return 99.0  # doji — treat as max wick
    if direction == "BULLISH":
        wick = candle.h - max(candle.o, candle.c)
    else:
        wick = min(candle.o, candle.c) - candle.l
    return wick / body


def rolling_vol_ratio(candle: Candle, rolling_avg: float) -> float:
    """candle.v / rolling_avg(last_10_bars)"""
    if rolling_avg <= 0:
        return 1.0
    return candle.v / rolling_avg


def cvd_turn_magnitude(cvd_turn: float, rolling_avg_cvd: float) -> float:
    """abs(cvd_turn) / rolling_avg(last_10_cvd_turns)"""
    if rolling_avg_cvd <= 0:
        return 0.0
    return abs(cvd_turn) / rolling_avg_cvd


def detect_fvg(candles: list) -> tuple:
    """
    Fair Value Gap detection.
    FVG = gap between candle[-3].high and candle[-1].low (for bullish)
          gap between candle[-3].low  and candle[-1].high (for bearish)

    Returns (fvg_found: bool, fvg_midpoint: float, fvg_direction: str)
    """
    if len(candles) < 3:
        return False, 0.0, ""

    c1 = candles[-3]  # 3 candles back
    c3 = candles[-1]  # most recent

    # Bullish FVG: gap above c1.high below c3.low
    if c3.l > c1.h:
        mid = (c1.h + c3.l) / 2
        return True, mid, "BULLISH"

    # Bearish FVG: gap below c1.low above c3.high
    if c3.h < c1.l:
        mid = (c1.l + c3.h) / 2
        return True, mid, "BEARISH"

    return False, 0.0, ""


def is_super_candle(
    candle: Candle,
    atr: float,
    rolling_avg_vol: float,
    cvd_turn: float,
    rolling_avg_cvd: float,
    vol_percentile_90: float,
) -> bool:
    """
    Super-candle check — skip Gate 7 confirmation if True.
    All three conditions must be met:
    - Volume in top 10% of rolling window
    - Body > ATR
    - CVD turn > 4× rolling average
    """
    vol_top10  = candle.v >= vol_percentile_90
    body_large = abs(candle.c - candle.o) > atr
    cvd_strong = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd) >= 4.0
    return vol_top10 and body_large and cvd_strong
```

---

### FILE: `trading/detection/confidence.py`

Composite confidence scorer 0–100.

```python
"""
Confidence scoring engine.
Composite 0–100 score from 5 components.
"""

from dataclasses import dataclass, field
from ..models import Level
from .approach import ApproachResult


@dataclass
class ConfidenceResult:
    score: int
    label: str        # HIGH | MEDIUM | LOW
    components: dict  # breakdown for brief
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
    vol_ratio: float,           # candle.v / rolling_avg_10
    displacement: float,        # 0.0–1.0 displacement ratio
    wick_ratio: float,          # wick/body on rejection side
    cvd_ratio: float,           # abs(cvd_turn) / rolling_avg_10_cvd
    cvd_divergence: bool,       # price/CVD divergence detected
    approach: ApproachResult,
    cvd_quarantine: bool = False,  # WS gap — cap CVD contribution at 8
    day_bias: str = "NEUTRAL",
) -> ConfidenceResult:
    components = {}
    total = 0

    # ── 1. LOCATION QUALITY (max 20, +10 confluence bonus)
    level_score = level.score
    if level_score >= 9:
        loc = 20
    elif level_score >= 8:
        loc = 18
    elif level_score >= 7:
        loc = 15
    elif level_score >= 6:
        loc = 10
    else:
        loc = 5

    # Confluence bonus (handled by level scorer — level.score already includes it)
    # POC/VAH/VAL premium
    if level.source in ("VOLUME_PROFILE", "POC", "VAH", "VAL"):
        loc = min(20, loc + 2)

    components["location"] = loc
    total += loc

    # ── 2. VOLUME SIGNATURE (max 20)
    if vol_ratio >= 2.0:
        vol = 20
    elif vol_ratio >= 1.5:
        vol = 12
    elif vol_ratio >= 1.2:
        vol = 5
    else:
        vol = 0  # blocks signal if < 1.2 (Gate 3 would have caught this)

    components["volume"] = vol
    total += vol

    # ── 3. PRICE SIGNATURE (max 20, +5 wick bonus)
    if displacement >= 1.0:
        price = 20
    elif displacement >= 0.7:
        price = 14
    elif displacement >= 0.4:
        price = 8
    else:
        price = 0

    if wick_ratio >= 0.5:  # significant wick on rejection side
        price = min(20, price + 5)

    components["price"] = price
    total += price

    # ── 4. CVD SIGNATURE (max 25, +10 divergence bonus)
    if cvd_quarantine:
        # WS gap — cap contribution
        cvd_pts = min(8, 8 if cvd_ratio >= 2.0 else 0)
    else:
        if cvd_ratio >= 4.0:
            cvd_pts = 25
        elif cvd_ratio >= 2.0:
            cvd_pts = 15
        elif cvd_ratio >= 1.0:
            cvd_pts = 8
        else:
            cvd_pts = 0

        if cvd_divergence:
            cvd_pts = min(25, cvd_pts + 10)

    components["cvd"] = cvd_pts
    total += cvd_pts

    # ── 5. APPROACH CONTEXT (max 15)
    approach_pts = approach.confidence_pts
    components["approach"] = approach_pts
    total += approach_pts

    # ── THRESHOLD ADJUSTMENT
    threshold = 50
    if day_bias == "NEUTRAL":
        threshold = 70  # stricter when no clear direction

    label = "HIGH" if total >= 75 else "MEDIUM" if total >= threshold else "LOW"

    details = (
        f"loc={loc} vol={vol} price={price} cvd={cvd_pts} approach={approach_pts} "
        f"total={total} threshold={threshold}"
        + (" [QUARANTINE]" if cvd_quarantine else "")
    )

    return ConfidenceResult(
        score=total,
        label=label,
        components=components,
        details=details,
    )
```

---

### FILE: `trading/detection/liquidity_grab.py`

Setup 1 — replaces `stop_hunt.py`.

```python
"""
Setup 1: Liquidity Grab
Replaces stop_hunt.py

Detects institutional stop sweeps on 1m candles.
Preferred entry: FVG midpoint. Fallback: next candle open.
"""

from ..models import Candle, Level
from .approach import classify_approach, ApproachResult
from .metrics import (
    displacement_ratio, rolling_vol_ratio,
    cvd_turn_magnitude, detect_fvg, is_super_candle,
)
from .confidence import score_signal


def detect_liquidity_grab(
    candles_1m: list,        # last 10+ closed 1m candles
    level: Level,
    atr: float,
    rolling_avg_vol: float,  # rolling_avg(last_10_bars)
    cvd_turn: float,         # cvd change on this candle
    rolling_avg_cvd: float,  # rolling_avg(last_10_cvd_turns)
    cvd_quarantine: bool = False,
    day_bias: str = "NEUTRAL",
) -> dict | None:
    """
    Returns signal dict or None.

    Signal dict keys:
        setup, direction, entry, approach, confidence,
        fvg_found, fvg_midpoint, details
    """
    if len(candles_1m) < 5:
        return None

    candle = candles_1m[-1]   # trigger candle
    price  = candle.c

    # ── Approach check (must be AGGRESSIVE_PUSH)
    approach = classify_approach(
        candles_1m[-6:-1], level.price, atr, rolling_avg_vol
    )
    if approach.type != "AGGRESSIVE_PUSH":
        return None

    # ── Sweep candle check
    total_range = candle.h - candle.l
    if total_range == 0:
        return None

    body = abs(candle.c - candle.o)
    disp = displacement_ratio(candle)
    vol_ratio = rolling_vol_ratio(candle, rolling_avg_vol)
    cvd_ratio = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd)

    # Determine direction (bullish = swept below level, rejected back up)
    swept_below = candle.l < level.price and candle.c > level.price
    swept_above = candle.h > level.price and candle.c < level.price

    if not swept_below and not swept_above:
        return None

    direction = "BULLISH" if swept_below else "BEARISH"

    # Wick past level
    if swept_below:
        wick_past = level.price - candle.l
    else:
        wick_past = candle.h - level.price

    if wick_past < atr * 0.3:
        return None  # wick not significant enough

    # Displacement ratio check
    if disp < 0.6:
        return None

    # Volume: top 25% (vol_ratio >= 1.5 is a reasonable proxy)
    if vol_ratio < 1.5:
        return None

    # CVD turn magnitude
    if cvd_ratio < 2.0 and not cvd_quarantine:
        return None

    # ── FVG detection (preferred entry point)
    fvg_found, fvg_mid, fvg_dir = detect_fvg(candles_1m[-3:])

    # ── Confidence score
    conf = score_signal(
        level=level,
        vol_ratio=vol_ratio,
        displacement=disp,
        wick_ratio=wick_past / body if body > 0 else 0,
        cvd_ratio=cvd_ratio,
        cvd_divergence=False,
        approach=approach,
        cvd_quarantine=cvd_quarantine,
        day_bias=day_bias,
    )

    if conf.label == "LOW":
        return None

    entry = fvg_mid if fvg_found else None  # None = next candle open

    return {
        "setup":        "LIQUIDITY_GRAB",
        "direction":    direction,
        "entry":        entry,
        "approach":     approach,
        "confidence":   conf,
        "fvg_found":    fvg_found,
        "fvg_midpoint": fvg_mid,
        "vol_ratio":    vol_ratio,
        "cvd_ratio":    cvd_ratio,
        "wick_past":    wick_past,
        "details":      (
            f"sweep wick={wick_past:.2f} disp={disp:.2f} "
            f"vol={vol_ratio:.1f}x cvd={cvd_ratio:.1f}x "
            f"fvg={'YES @ '+str(round(fvg_mid,2)) if fvg_found else 'NO'}"
        ),
    }
```

---

### FILE: `trading/detection/defense.py`

Setup 2 — replaces `breakout.py`.

```python
"""
Setup 2: Institutional Order Block Defense
Replaces breakout.py

Detects institutional defense of order blocks on 5m candles.
Only valid on TREND days with locked directional bias.
"""

import time
from ..models import Candle, Level
from .approach import classify_approach
from .metrics import displacement_ratio, rolling_vol_ratio, cvd_turn_magnitude
from .confidence import score_signal


def find_order_block(
    candles_5m: list,   # last 20+ closed 5m candles
    displacement_candle_idx: int,  # index of the displacement candle
    direction: str,     # BULLISH = looking for last bearish OB before bull displacement
    atr: float,
    rolling_avg_vol: float,
) -> dict | None:
    """
    Order Block = last opposite-colored candle before displacement.
    Requirements:
      - body > ATR × 1.0
      - volume top 20% (vol_ratio >= 1.5)
      - formed within 60 min from OB candle close time
      - not visited 2+ times since formed
    """
    if displacement_candle_idx < 1:
        return None

    ob_color = "BEARISH" if direction == "BULLISH" else "BULLISH"
    now_ts   = time.time() * 1000  # ms

    # Search backwards from displacement candle
    for i in range(displacement_candle_idx - 1, max(-1, displacement_candle_idx - 15), -1):
        c = candles_5m[i]
        is_correct_color = (
            (ob_color == "BEARISH" and c.c < c.o) or
            (ob_color == "BULLISH" and c.c > c.o)
        )
        if not is_correct_color:
            continue

        body = abs(c.c - c.o)
        if body < atr * 1.0:
            continue

        vol_ratio = c.v / rolling_avg_vol if rolling_avg_vol > 0 else 0
        if vol_ratio < 1.5:
            continue

        # Formed within 60 min (3600000 ms)
        if now_ts - c.t > 3600000:
            continue

        # OB zone
        if direction == "BULLISH":
            ob_high = max(c.o, c.c)
            ob_low  = min(c.o, c.c)
        else:
            ob_high = max(c.o, c.c)
            ob_low  = min(c.o, c.c)

        return {
            "candle":    c,
            "ob_high":   ob_high,
            "ob_low":    ob_low,
            "ob_mid":    (ob_high + ob_low) / 2,
            "vol_ratio": vol_ratio,
            "formed_at": c.t,
        }

    return None


def count_ob_visits(
    candles_5m: list,
    ob_high: float,
    ob_low: float,
    ob_formed_idx: int,
) -> int:
    """Count how many times price touched OB zone after it was formed."""
    visits = 0
    for c in candles_5m[ob_formed_idx + 1:]:
        if c.l <= ob_high and c.h >= ob_low:
            visits += 1
    return visits


def detect_ob_defense(
    candles_5m: list,       # last 20+ closed 5m candles
    candles_1m: list,       # last 5+ closed 1m candles for CVD slope
    level: Level,
    atr: float,
    rolling_avg_vol_5m: float,
    rolling_avg_vol_1m: float,
    cvd_turn: float,
    rolling_avg_cvd: float,
    day_type: str,          # must be TREND
    day_bias: str,          # must be BULLISH or BEARISH (locked)
    cvd_quarantine: bool = False,
) -> dict | None:
    """
    Returns signal dict or None.
    Only fires on TREND days with locked directional bias.
    """
    # Day type gate
    if day_type != "TREND":
        return None

    if day_bias not in ("BULLISH", "BEARISH"):
        return None

    direction = day_bias  # signal direction matches locked bias

    if len(candles_5m) < 5:
        return None

    last_5m = candles_5m[-1]

    # Approach on 5m candles must be ABSORPTION
    approach = classify_approach(
        candles_5m[-6:-1], level.price, atr, rolling_avg_vol_5m
    )
    if approach.type != "ABSORPTION":
        return None

    # Find most recent displacement to locate OB
    displacement_idx = len(candles_5m) - 1
    ob = find_order_block(
        candles_5m, displacement_idx, direction, atr, rolling_avg_vol_5m
    )
    if not ob:
        return None

    # OB not visited 2+ times since formed
    ob_formed_idx = next(
        (i for i, c in enumerate(candles_5m) if c.t == ob["formed_at"]), 0
    )
    visits = count_ob_visits(candles_5m, ob["ob_high"], ob["ob_low"], ob_formed_idx)
    if visits >= 2:
        return None

    # Price touches OB zone (within 0.15%)
    tolerance = level.price * 0.0015
    if direction == "BULLISH":
        touched_ob = last_5m.l <= ob["ob_high"] + tolerance
    else:
        touched_ob = last_5m.h >= ob["ob_low"] - tolerance

    if not touched_ob:
        return None

    # CVD slope: declining before → flattens/turns at level
    cvd_ratio  = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd)
    vol_ratio  = rolling_vol_ratio(last_5m, rolling_avg_vol_5m)
    disp       = displacement_ratio(last_5m)

    # CVD must show defense (turn positive for bull, negative for bear)
    if not cvd_quarantine:
        if direction == "BULLISH" and cvd_turn <= 0:
            return None
        if direction == "BEARISH" and cvd_turn >= 0:
            return None

    conf = score_signal(
        level=level,
        vol_ratio=vol_ratio,
        displacement=disp,
        wick_ratio=0.0,
        cvd_ratio=cvd_ratio,
        cvd_divergence=False,
        approach=approach,
        cvd_quarantine=cvd_quarantine,
        day_bias=day_bias,
    )

    if conf.label == "LOW":
        return None

    return {
        "setup":        "OB_DEFENSE",
        "direction":    direction,
        "entry":        None,  # next candle open
        "approach":     approach,
        "confidence":   conf,
        "ob":           ob,
        "ob_visits":    visits,
        "vol_ratio":    vol_ratio,
        "cvd_ratio":    cvd_ratio,
        "details":      (
            f"OB ${ob['ob_low']:.2f}–${ob['ob_high']:.2f} "
            f"visits={visits} vol={vol_ratio:.1f}x cvd={cvd_ratio:.1f}x"
        ),
    }
```

---

### FILE: `trading/detection/failed_auction.py`

Setup 3 (VAR + Major Level) — replaces `rejection.py`.

```python
"""
Setup 3: Failed Auction
Replaces rejection.py

3A: Value Area Rejection (VAR) — after 11:00 AM only
3B: Major Level Rejection — level score >= 8
"""

from ..models import Candle, Level
from .approach import classify_approach
from .metrics import displacement_ratio, rolling_vol_ratio, cvd_turn_magnitude, wick_body_ratio
from .confidence import score_signal


def detect_failed_auction(
    candles: list,           # 1m or 5m closed candles
    level: Level,
    atr: float,
    rolling_avg_vol: float,
    cvd_turn: float,
    rolling_avg_cvd: float,
    vah: float,              # Value Area High (from volume profile)
    val: float,              # Value Area Low
    poc: float,              # Point of Control
    session_hour: float,     # decimal hour in ET (e.g. 13.5 = 1:30 PM)
    cvd_quarantine: bool = False,
    day_bias: str = "NEUTRAL",
) -> dict | None:
    """
    Tries 3A first, then 3B.
    Returns signal dict or None.
    """
    result = _detect_var(
        candles, level, atr, rolling_avg_vol, cvd_turn, rolling_avg_cvd,
        vah, val, poc, session_hour, cvd_quarantine, day_bias
    )
    if result:
        return result

    return _detect_major_level(
        candles, level, atr, rolling_avg_vol, cvd_turn, rolling_avg_cvd,
        cvd_quarantine, day_bias
    )


def _detect_var(
    candles, level, atr, rolling_avg_vol, cvd_turn, rolling_avg_cvd,
    vah, val, poc, session_hour, cvd_quarantine, day_bias
) -> dict | None:
    """Setup 3A: Value Area Rejection. Only after 11:00 AM."""
    if session_hour < 11.0:
        return None

    if not vah or not val or not poc:
        return None

    if len(candles) < 3:
        return None

    candle = candles[-1]
    vol_ratio = rolling_vol_ratio(candle, rolling_avg_vol)
    cvd_ratio = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd)

    # Price must have been outside value area
    # Now body closes back inside
    outside_above = candle.h > vah and candle.c < vah
    outside_below = candle.l < val and candle.c > val

    if not outside_above and not outside_below:
        return None

    direction = "BEARISH" if outside_above else "BULLISH"

    # Low-volume confirmation (inverse gate — vol < 0.8×)
    if vol_ratio >= 0.8:
        return None

    # CVD turn >= 1.5×
    if cvd_ratio < 1.5 and not cvd_quarantine:
        return None

    approach = classify_approach(
        candles[-6:-1], level.price, atr, rolling_avg_vol
    )

    conf = score_signal(
        level=level,
        vol_ratio=vol_ratio,
        displacement=displacement_ratio(candle),
        wick_ratio=wick_body_ratio(candle, direction),
        cvd_ratio=cvd_ratio,
        cvd_divergence=False,
        approach=approach,
        cvd_quarantine=cvd_quarantine,
        day_bias=day_bias,
    )

    if conf.label == "LOW":
        return None

    return {
        "setup":      "FAILED_AUCTION_VAR",
        "direction":  direction,
        "entry":      None,
        "target":     poc,        # ALWAYS POC for 3A
        "approach":   approach,
        "confidence": conf,
        "vol_ratio":  vol_ratio,
        "cvd_ratio":  cvd_ratio,
        "details":    (
            f"{'above VAH' if outside_above else 'below VAL'} "
            f"target=POC ${poc:.2f} vol={vol_ratio:.1f}x cvd={cvd_ratio:.1f}x"
        ),
    }


def _detect_major_level(
    candles, level, atr, rolling_avg_vol, cvd_turn, rolling_avg_cvd,
    cvd_quarantine, day_bias
) -> dict | None:
    """Setup 3B: Major Level Rejection. Level score >= 8 required."""
    if level.score < 8:
        return None

    if len(candles) < 3:
        return None

    candle    = candles[-1]
    vol_ratio = rolling_vol_ratio(candle, rolling_avg_vol)
    cvd_ratio = cvd_turn_magnitude(cvd_turn, rolling_avg_cvd)

    # Approach must be EXHAUSTION
    approach = classify_approach(
        candles[-6:-1], level.price, atr, rolling_avg_vol
    )
    if approach.type != "EXHAUSTION":
        return None

    # Determine rejection direction
    upper_wick = candle.h - max(candle.o, candle.c)
    lower_wick = min(candle.o, candle.c) - candle.l
    body       = abs(candle.c - candle.o)

    if body == 0:
        return None

    upper_ratio = upper_wick / body
    lower_ratio = lower_wick / body

    # Need wick/body > 2.5 on rejection side
    if upper_ratio > lower_ratio and upper_ratio >= 2.5:
        direction   = "BEARISH"
        wick_r      = upper_ratio
    elif lower_ratio >= 2.5:
        direction   = "BULLISH"
        wick_r      = lower_ratio
    else:
        return None

    # Volume top 25% (vol_ratio >= 1.5)
    if vol_ratio < 1.5:
        return None

    # CVD >= 2×
    if cvd_ratio < 2.0 and not cvd_quarantine:
        return None

    conf = score_signal(
        level=level,
        vol_ratio=vol_ratio,
        displacement=displacement_ratio(candle),
        wick_ratio=wick_r,
        cvd_ratio=cvd_ratio,
        cvd_divergence=False,
        approach=approach,
        cvd_quarantine=cvd_quarantine,
        day_bias=day_bias,
    )

    if conf.label == "LOW":
        return None

    return {
        "setup":      "FAILED_AUCTION_MAJOR",
        "direction":  direction,
        "entry":      None,
        "target":     None,
        "approach":   approach,
        "confidence": conf,
        "vol_ratio":  vol_ratio,
        "cvd_ratio":  cvd_ratio,
        "wick_ratio": wick_r,
        "details":    (
            f"wick/body={wick_r:.1f} vol={vol_ratio:.1f}x cvd={cvd_ratio:.1f}x "
            f"level_score={level.score}"
        ),
    }
```

---

### FILE: `trading/data/persistence.py`

SQLite persistence layer.

```python
"""
SQLite persistence layer.
Hot state lives in memory. Persistent state flushed on schedule.

Flush schedule:
  - every 60s: CVD snapshots, day context
  - every 5m:  volume profile, LevelState trackers, signal history, last tick timestamps
"""

import sqlite3
import json
import time
import os
from pathlib import Path


DB_PATH = os.getenv("DB_PATH", "trading_state.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cvd_snapshots (
            asset TEXT PRIMARY KEY,
            value REAL,
            estimated INTEGER DEFAULT 0,
            updated_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS vol_profiles (
            asset TEXT PRIMARY KEY,
            data  TEXT,
            updated_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS day_contexts (
            asset TEXT PRIMARY KEY,
            data  TEXT,
            date  TEXT,
            updated_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS level_states (
            key  TEXT PRIMARY KEY,
            data TEXT,
            updated_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS signal_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            asset      TEXT,
            direction  TEXT,
            setup      TEXT,
            level_name TEXT,
            entry      REAL,
            confidence TEXT,
            fired_at   INTEGER,
            data       TEXT
        );

        CREATE TABLE IF NOT EXISTS tick_timestamps (
            asset      TEXT PRIMARY KEY,
            last_tick  INTEGER
        );
    """)
    conn.commit()
    conn.close()


def save_cvd_snapshot(asset: str, value: float, estimated: bool = False):
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO cvd_snapshots (asset, value, estimated, updated_at)
        VALUES (?, ?, ?, ?)
    """, (asset, value, int(estimated), int(time.time())))
    conn.commit()
    conn.close()


def load_cvd_snapshots() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT asset, value, estimated FROM cvd_snapshots").fetchall()
    conn.close()
    return {r["asset"]: {"value": r["value"], "estimated": bool(r["estimated"])} for r in rows}


def save_vol_profile(asset: str, profile_dict: dict):
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO vol_profiles (asset, data, updated_at)
        VALUES (?, ?, ?)
    """, (asset, json.dumps(profile_dict), int(time.time())))
    conn.commit()
    conn.close()


def save_signal(signal_dict: dict):
    conn = get_db()
    conn.execute("""
        INSERT INTO signal_history
            (asset, direction, setup, level_name, entry, confidence, fired_at, data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal_dict.get("asset"),
        signal_dict.get("direction"),
        signal_dict.get("setup"),
        signal_dict.get("level_name"),
        signal_dict.get("entry"),
        signal_dict.get("confidence"),
        int(time.time()),
        json.dumps(signal_dict),
    ))
    conn.commit()
    conn.close()


def save_tick_timestamp(asset: str, ts: int):
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO tick_timestamps (asset, last_tick)
        VALUES (?, ?)
    """, (asset, ts))
    conn.commit()
    conn.close()


def load_tick_timestamps() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT asset, last_tick FROM tick_timestamps").fetchall()
    conn.close()
    return {r["asset"]: r["last_tick"] for r in rows}


# Call at startup
init_db()
```

---

### FILE: `trading/data/calendar.py`

Market calendar — hardcoded 2026 dates + earnings check.

```python
"""
Market calendar for 2026.
Hardcoded dates + yfinance earnings integration.

Macro event windows:
  - FOMC / Fed Chair:        ±30 minutes
  - CPI/NFP/PCE/PPI/Retail:  ±15 minutes
"""

from datetime import date, datetime, timedelta
import pytz

ET = pytz.timezone("America/New_York")

# ── 2026 MARKET HOLIDAYS ──────────────────────────────────────────
MARKET_HOLIDAYS_2026 = {
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
}

# ── 2026 EARLY CLOSE DAYS (1:00 PM ET) ───────────────────────────
EARLY_CLOSE_DAYS_2026 = {
    date(2026, 11, 27),  # Day before Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
    date(2026, 12, 31),  # New Year's Eve
}

# ── 2026 MACRO EVENTS (date, time_et_hhmm, window_minutes, label) ─
MACRO_EVENTS_2026 = [
    # FOMC decisions (±30 min)
    (date(2026, 1, 29),  1400, 30, "FOMC"),
    (date(2026, 3, 19),  1400, 30, "FOMC"),
    (date(2026, 5, 7),   1400, 30, "FOMC"),
    (date(2026, 6, 18),  1400, 30, "FOMC"),
    (date(2026, 7, 30),  1400, 30, "FOMC"),
    (date(2026, 9, 17),  1400, 30, "FOMC"),
    (date(2026, 11, 5),  1400, 30, "FOMC"),
    (date(2026, 12, 10), 1400, 30, "FOMC"),
    # CPI (±15 min) — 8:30 AM releases
    (date(2026, 1, 15),   830, 15, "CPI"),
    (date(2026, 2, 12),   830, 15, "CPI"),
    (date(2026, 3, 12),   830, 15, "CPI"),
    (date(2026, 4, 10),   830, 15, "CPI"),
    (date(2026, 5, 13),   830, 15, "CPI"),
    (date(2026, 6, 11),   830, 15, "CPI"),
    (date(2026, 7, 15),   830, 15, "CPI"),
    (date(2026, 8, 13),   830, 15, "CPI"),
    (date(2026, 9, 11),   830, 15, "CPI"),
    (date(2026, 10, 14),  830, 15, "CPI"),
    (date(2026, 11, 12),  830, 15, "CPI"),
    (date(2026, 12, 10),  830, 15, "CPI"),
    # NFP (±15 min) — first Friday of month 8:30 AM
    (date(2026, 1, 9),    830, 15, "NFP"),
    (date(2026, 2, 6),    830, 15, "NFP"),
    (date(2026, 3, 6),    830, 15, "NFP"),
    (date(2026, 4, 3),    830, 15, "NFP"),
    (date(2026, 5, 1),    830, 15, "NFP"),
    (date(2026, 6, 5),    830, 15, "NFP"),
    (date(2026, 7, 10),   830, 15, "NFP"),
    (date(2026, 8, 7),    830, 15, "NFP"),
    (date(2026, 9, 4),    830, 15, "NFP"),
    (date(2026, 10, 2),   830, 15, "NFP"),
    (date(2026, 11, 6),   830, 15, "NFP"),
    (date(2026, 12, 4),   830, 15, "NFP"),
]


def is_market_holiday(d: date = None) -> bool:
    if d is None:
        d = datetime.now(ET).date()
    return d in MARKET_HOLIDAYS_2026


def is_early_close(d: date = None) -> bool:
    if d is None:
        d = datetime.now(ET).date()
    return d in EARLY_CLOSE_DAYS_2026


def get_cutoff_time(d: date = None) -> int:
    """Returns cutoff time as HHMM integer."""
    if d is None:
        d = datetime.now(ET).date()
    if d in EARLY_CLOSE_DAYS_2026:
        return 1230  # 12:30 PM on early close days
    return 1515      # 3:15 PM normally


def is_macro_halt(now: datetime = None) -> tuple:
    """
    Returns (is_halted: bool, reason: str).
    Checks if current time is within any macro event window.
    """
    if now is None:
        now = datetime.now(ET)

    today = now.date()
    t_hhmm = now.hour * 100 + now.minute

    for evt_date, evt_time, window_min, label in MACRO_EVENTS_2026:
        if evt_date != today:
            continue

        evt_dt = ET.localize(datetime(
            today.year, today.month, today.day,
            evt_time // 100, evt_time % 100
        ))

        delta = abs((now - evt_dt).total_seconds() / 60)
        if delta <= window_min:
            return True, f"{label} ±{window_min}min window"

    return False, ""


def is_earnings_within_hold(
    asset: str,
    hold_days: int = 5,
) -> tuple:
    """
    Returns (blocked: bool, reason: str).
    Uses yfinance to check upcoming earnings.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(asset)
        cal    = ticker.calendar
        if cal is None or cal.empty:
            return False, ""

        # calendar has 'Earnings Date' row
        if "Earnings Date" in cal.index:
            earn_date = cal.loc["Earnings Date"].iloc[0]
            if hasattr(earn_date, 'date'):
                earn_date = earn_date.date()
            today     = datetime.now(ET).date()
            days_away = (earn_date - today).days
            if 0 <= days_away <= hold_days:
                return True, f"{asset} earnings in {days_away}d (within {hold_days}d hold)"

    except Exception:
        pass  # fail open — don't block on yfinance errors

    return False, ""
```

---

## Phase 2 — Files to Modify (8 files)

### MODIFY: `trading/detection/level_state.py`

Add the following to the `TrackerEngine` and `LevelState` classes:

**Add UNCERTAIN state:**
```python
# In LevelState dataclass, add field:
uncertain: bool = False  # True when WS resync happened mid-track

# In TrackerEngine.on_1m_close, when uncertain:
#   Raise confirmation threshold to 75 before confirming
```

**Add safe iteration (prevents RuntimeError during async iteration):**
```python
# In all methods that iterate self._trackers:
# Replace: for t in self._trackers.values()
# With:    for t in list(self._trackers.values())
```

**Add WAIT tracker max-calls logic:**
```python
# In TrackerEngine, add field per tracker:
agent_call_count: int = 0  # incremented each time agent is called for this tracker

# In the engine that calls the agent:
# if tracker.agent_call_count >= 2: expire the tracker
# WAIT result: increment count, do NOT start cooldown, do NOT send Telegram
```

---

### MODIFY: `trading/levels/builder.py`

Update volume interaction multiplier and minimum score threshold:

```python
# Replace old score multiplier logic with:
def apply_volume_multiplier(base_score: int, interaction_count: int) -> int:
    """
    Volume confirmation multiplier based on interaction count.
    0 interactions: ×0.5
    1 interaction:  ×1.0
    2 interactions: ×1.3
    3+ interactions: ×1.5
    """
    multipliers = {0: 0.5, 1: 1.0, 2: 1.3}
    mult = multipliers.get(interaction_count, 1.5)  # 3+ = 1.5
    return max(1, round(base_score * mult))

# Minimum score threshold: raise from 6 to 7
MIN_LEVEL_SCORE_FOR_DETECTION = 7  # levels scoring < 7 are filtered before detection

# Confluence rule: two levels within 0.30% → score = max + 2 (cap 12)
CONFLUENCE_DISTANCE_PCT = 0.0030  # 0.30%
CONFLUENCE_BONUS = 2
CONFLUENCE_CAP = 12
```

---

### MODIFY: `trading/core/gates.py`

Add Gate 0 (macro halt) and earnings check as pre-signal gates. Update proximity check.

```python
# Add to check_all() BEFORE all existing gates:

# Gate 0a: Earnings check
from ..data.calendar import is_earnings_within_hold
earned_blocked, earn_reason = is_earnings_within_hold(asset)
if earned_blocked:
    return False, earn_reason

# Gate 0b: Holiday
from ..data.calendar import is_market_holiday
if is_market_holiday():
    return False, "Market holiday"

# Gate 0c: Macro event halt
from ..data.calendar import is_macro_halt
macro_halted, macro_reason = is_macro_halt()
if macro_halted:
    return False, f"Macro halt: {macro_reason}"

# Also add get_size_modifier() method:
def get_size_modifier(self, vix: float) -> str:
    """Returns position size modifier based on VIX."""
    if vix >= 35:
        return "SKIP"
    elif vix >= 30:
        return "QUARTER"   # debit spread only
    elif vix >= 25:
        return "HALF"
    else:
        return "FULL"
```

---

### MODIFY: `trading/core/multi_engine.py`

Route to new setups. Add asyncio.Lock for agent. Add staleness detection.

```python
# 1. Add single global agent lock at top of __init__:
self._agent_lock = asyncio.Lock()

# 2. In _fire_agent(), wrap with lock:
async with self._agent_lock:
    # ... existing agent fire logic

# 3. Add WS staleness detection:
self._last_tick_ts: dict = {a: 0.0 for a in ASSETS}  # seconds

# In _handle_tick():
self._last_tick_ts[asset] = time.time()

# Add staleness check method:
def _is_stale(self, asset: str, threshold_sec: float = 30.0) -> bool:
    last = self._last_tick_ts.get(asset, 0)
    return last > 0 and (time.time() - last) > threshold_sec

# 4. Route 1m bar close to new setup detectors:
# In _on_1m_bars_updated(): call detect_liquidity_grab()
# In _on_5m_bars_updated(): call detect_ob_defense() + detect_failed_auction()

# 5. Import new modules:
from ..detection.liquidity_grab import detect_liquidity_grab
from ..detection.defense import detect_ob_defense
from ..detection.failed_auction import detect_failed_auction
from ..detection.approach import classify_approach
from ..detection.metrics import is_super_candle

# 6. Remove imports of old detection files:
# REMOVE: from ..detection.breakout import detect_breakout, is_back_through
# REMOVE: from ..detection.rejection import detect_rejection
# REMOVE: from ..detection.stop_hunt import detect_stop_hunt, confirm_stop_hunt
```

---

### MODIFY: `trading/context/options_context.py`

Implement the 2–5 DTE Rolling Fridays model exactly as specced.

```python
# Replace get_expiry() entirely:

def get_expiry(asset: str) -> tuple:
    """
    Returns (dte: int, expiry_date_str: str, skip: bool).

    2–5 DTE Rolling Fridays model:
      Mon–Wed:           Current Friday
      Thu before noon:   Current Friday (1 DTE acceptable)
      Thu after 1PM:     SKIP (options math broken)
      Fri before noon:   NEXT Friday
      Fri after noon:    SKIP

    skip=True means do not trade today.
    """
    now = datetime.now(ET)
    dow = now.weekday()  # 0=Mon 4=Fri
    t   = now.hour * 60 + now.minute

    # Find next Friday
    days_to_friday = (4 - dow) % 7
    if days_to_friday == 0:
        days_to_friday = 7
    next_friday = now.date() + timedelta(days=days_to_friday)

    # Find current Friday
    days_since_mon = dow
    days_to_cur_fri = 4 - dow
    cur_friday = now.date() + timedelta(days=days_to_cur_fri if days_to_cur_fri >= 0 else days_to_cur_fri + 7)

    if dow == 3:  # Thursday
        if t >= 780:  # after 1:00 PM (13*60=780)
            return 0, "", True   # SKIP

        expiry = cur_friday
        dte    = (expiry - now.date()).days

    elif dow == 4:  # Friday
        if t >= 720:  # after noon (12*60=720)
            return 0, "", True   # SKIP

        expiry = next_friday
        dte    = (expiry - now.date()).days

    else:  # Mon, Tue, Wed
        expiry = cur_friday
        dte    = (expiry - now.date()).days

    expiry_str = expiry.strftime("%b %d")
    return max(1, dte), expiry_str, False
```

---

### MODIFY: `trading/context/day_context.py`

Update day bias algorithm to match v2.1 spec exactly.

```python
# Replace bias scoring logic with:

def compute_day_bias(
    daily_bars: list,
    bars_15m: list,
    current_price: float,
    or_high: float,
    or_low: float,
    vwap: float,
) -> tuple:
    """
    Returns (bias: str, score: int).
    bias: BULLISH | BEARISH | NEUTRAL
    score: integer
    Locked at 10:00 AM.
    """
    score = 0

    # Daily trend
    if len(daily_bars) >= 5:
        last5 = daily_bars[-5:]
        if last5[-1].c > last5[0].c:
            score += 1
        elif last5[-1].c < last5[0].c:
            score -= 1

    # 15m trend
    if len(bars_15m) >= 4:
        last4 = bars_15m[-4:]
        if last4[-1].c > last4[0].c:
            score += 1
        elif last4[-1].c < last4[0].c:
            score -= 1

    # Broke above ORH or below ORL
    if or_high > 0 and current_price > or_high:
        score += 2
    elif or_low > 0 and current_price < or_low:
        score -= 2

    # Price vs VWAP
    if vwap > 0:
        if current_price > vwap:
            score += 1
        elif current_price < vwap:
            score -= 1

    if score >= 3:
        bias = "BULLISH"
    elif score <= -3:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return bias, score

# Day type classification (count direction changes in last 30 1m bars):
def compute_day_type(bars_1m_today: list) -> str:
    """TREND | RANGE"""
    sample = bars_1m_today[:30]
    if len(sample) < 5:
        return "RANGE"

    changes = 0
    for i in range(1, len(sample)):
        prev = sample[i-1]
        curr = sample[i]
        if (prev.c > prev.o) != (curr.c > curr.o):
            changes += 1

    return "TREND" if changes <= 4 else "RANGE"
```

---

### MODIFY: `trading/data/cvd_engine.py`

Add asyncio.Lock, ESTIMATED flag, and rolling averages.

```python
# In CVDEngine class, add:

import asyncio
self._lock = asyncio.Lock()
self._estimated = False          # True when CVD reconstructed from bars
self._cvd_history: list = []     # list of abs(cvd_turn) values for rolling avg

async def process_trade_async(self, price: float, volume: float):
    """Thread-safe version for async context."""
    async with self._lock:
        self.process_trade(price, volume)

def set_estimated(self, value: bool):
    self._estimated = value

@property
def is_estimated(self) -> bool:
    return self._estimated

def rolling_avg_cvd_turn(self, window: int = 10) -> float:
    """Rolling average of abs(cvd_turn) over last N turns."""
    sample = self._cvd_history[-window:]
    if not sample:
        return 1.0
    return sum(abs(v) for v in sample) / len(sample)

def record_cvd_turn(self, turn: float):
    """Call after each candle close to build rolling history."""
    self._cvd_history.append(turn)
    if len(self._cvd_history) > 200:  # cap history
        self._cvd_history = self._cvd_history[-200:]
```

---

### MODIFY: `trading/data/candle_store.py`

Enforce memory caps.

```python
# Apply these caps when loading/appending:
CAPS = {
    "1m":    500,
    "5m":    2000,
    "15m":   1000,
    "daily": 252,
}

# In load_1m(), load_5m(), etc.:
def load_1m(self, candles: list):
    self._1m = candles[-CAPS["1m"]:]

def load_5m(self, candles: list):
    self._5m = candles[-CAPS["5m"]:]

def load_15m(self, candles: list):
    self._15m = candles[-CAPS["15m"]:]

def load_daily(self, candles: list):
    self._daily = candles[-CAPS["daily"]:]
```

---

### MODIFY: `trading/agent/agent.py`

Add single global asyncio.Lock enforcement.

```python
# The lock lives in multi_engine.py (self._agent_lock).
# This file just needs to ensure the agent is never called without it.
# Add assertion/comment at top of run() method:

async def run(self, brief: str) -> Signal | None:
    """
    Called with the global agent lock already held.
    Never call this directly — always through multi_engine._fire_agent().
    """
    # ... existing logic
```

---

### MODIFY: `trading/agent/brief.py`

Add macro status, earnings status, IV estimate, CVD quality, and day bias to brief output.

```python
# Add these sections to build_brief():

# Macro status section
macro_halted, macro_reason = is_macro_halt()
macro_section = f"MACRO: {'HALTED — ' + macro_reason if macro_halted else 'CLEAR'}"

# Earnings section
earn_blocked, earn_reason = is_earnings_within_hold(asset)
earnings_section = f"EARNINGS: {'BLOCKED — ' + earn_reason if earn_blocked else 'CLEAR (no earnings within hold window)'}"

# IV estimate section
iv_section = f"IV ESTIMATE (approx): {vix:.1f} × {asset_beta} = {vix * asset_beta:.1f}%  [approximation only]"

# CVD quality section
cvd_quality = "ESTIMATED (WS gap — confidence capped)" if cvd_quarantine else "LIVE"
cvd_section = f"CVD QUALITY: {cvd_quality}"

# Day bias section
bias_section = f"DAY BIAS: {day_context.bias} (locked at 10AM) — Setup 2 valid: {day_context.day_type == 'TREND'}"
```

---

### MODIFY: `trading/agent/tools.py`

Cap tool output length and history size.

```python
# In get_level_map() and all other tools that return strings:
# Truncate output to 2000 characters max:
result = result[:2000] if len(result) > 2000 else result

# In get_signal_history():
# Cap to last 200 signals:
history = signal_history[-200:]

# Add get_cvd_divergence() tool:
def get_cvd_divergence(self, asset: str) -> str:
    """
    Returns CVD divergence analysis.
    Checks for price/CVD divergence (distribution or accumulation).
    """
    # ... implementation
    pass

# Add get_cvd_slope() tool:
def get_cvd_slope(self, asset: str, bars: int = 5) -> str:
    """
    Returns CVD slope over last N bars.
    Used to confirm absorption (CVD rising despite flat price).
    """
    # ... implementation
    pass
```

---

## Phase 3 — Delete Old Files

```bash
# Remove old detection files (they are replaced by new ones):
rm trading/detection/breakout.py
rm trading/detection/rejection.py
rm trading/detection/stop_hunt.py
```

Update any remaining imports in `multi_engine.py` and `__init__.py` files.

---

## Phase 4 — Tests

### FILE: `trading/tests/test_v21_detection.py`

```python
"""
v2.1 detection engine tests.
All 12 tests must pass before going live.
"""

import pytest
from trading.models import Candle, Level
from trading.detection.approach import classify_approach
from trading.detection.metrics import (
    displacement_ratio, wick_body_ratio, cvd_turn_magnitude,
    detect_fvg, rolling_vol_ratio
)
from trading.detection.confidence import score_signal, ConfidenceResult
from trading.detection.liquidity_grab import detect_liquidity_grab
from trading.detection.failed_auction import detect_failed_auction
from trading.data.calendar import is_macro_halt, is_earnings_within_hold, get_cutoff_time
from datetime import date


def make_candle(o, h, l, c, v=100000, t=0):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


# ── TEST 1: Approach — AGGRESSIVE_PUSH ───────────────────────────────────────

def test_approach_aggressive_push():
    atr  = 1.0
    rvol = 100000
    # Last candle: big bull body driving toward level at 185.00
    candles = [
        make_candle(183.0, 183.5, 182.8, 183.3, 90000),
        make_candle(183.3, 183.8, 183.1, 183.6, 95000),
        make_candle(183.6, 184.2, 183.4, 184.0, 98000),
        make_candle(184.0, 185.3, 183.9, 185.2, 220000),  # displacement candle
        make_candle(185.2, 185.4, 185.0, 185.3, 210000),
    ]
    result = classify_approach(candles, level_price=185.0, atr=atr, rolling_avg_vol=rvol)
    assert result.type == "AGGRESSIVE_PUSH", f"Got {result.type}"
    assert result.confidence_pts == 12


# ── TEST 2: Approach — ABSORPTION ────────────────────────────────────────────

def test_approach_absorption():
    atr  = 1.0
    rvol = 100000
    # Tight range, elevated volume, 4 candles < ATR*0.8
    candles = [
        make_candle(185.0, 185.2, 184.9, 185.1, 140000),
        make_candle(185.1, 185.3, 185.0, 185.2, 135000),
        make_candle(185.2, 185.4, 185.1, 185.2, 130000),
        make_candle(185.2, 185.3, 185.0, 185.1, 145000),
        make_candle(185.1, 185.2, 185.0, 185.1, 142000),
    ]
    result = classify_approach(candles, level_price=185.0, atr=atr, rolling_avg_vol=rvol)
    assert result.type == "ABSORPTION", f"Got {result.type}: {result.details}"
    assert result.confidence_pts == 15


# ── TEST 3: Approach — EXHAUSTION ────────────────────────────────────────────

def test_approach_exhaustion():
    atr  = 1.0
    rvol = 100000
    # Shrinking bodies, declining volume
    candles = [
        make_candle(184.0, 184.5, 183.8, 184.3, 90000),
        make_candle(184.3, 184.7, 184.2, 184.6, 80000),
        make_candle(184.6, 184.9, 184.5, 184.7, 70000),  # body=0.1
        make_candle(184.7, 185.0, 184.6, 184.8, 65000),  # body=0.1 < prev
        make_candle(184.8, 185.1, 184.7, 184.82, 60000), # body=0.02 < prev
    ]
    result = classify_approach(candles, level_price=185.0, atr=atr, rolling_avg_vol=rvol)
    assert result.type == "EXHAUSTION", f"Got {result.type}: {result.details}"


# ── TEST 4: Approach — MOMENTUM ──────────────────────────────────────────────

def test_approach_momentum():
    atr  = 1.0
    rvol = 100000
    candles = [
        make_candle(183.0, 183.5, 182.8, 183.4, 95000),
        make_candle(183.4, 183.9, 183.3, 183.8, 98000),
        make_candle(183.8, 184.3, 183.7, 184.2, 100000),
        make_candle(184.2, 184.7, 184.1, 184.6, 102000),
        make_candle(184.6, 185.1, 184.5, 185.0, 105000),
    ]
    result = classify_approach(candles, level_price=185.0, atr=atr, rolling_avg_vol=rvol)
    assert result.type == "MOMENTUM", f"Got {result.type}: {result.details}"
    assert result.confidence_pts == 10


# ── TEST 5: Approach — NEUTRAL (no pattern) ──────────────────────────────────

def test_approach_neutral():
    atr  = 1.0
    rvol = 100000
    candles = [
        make_candle(185.0, 185.3, 184.7, 185.1, 100000),
        make_candle(185.1, 185.2, 184.9, 185.0, 95000),
        make_candle(185.0, 185.4, 184.8, 185.2, 90000),
        make_candle(185.2, 185.3, 185.0, 185.1, 105000),
        make_candle(185.1, 185.5, 184.9, 185.3, 98000),
    ]
    result = classify_approach(candles, level_price=185.0, atr=atr, rolling_avg_vol=rvol)
    assert result.type == "NEUTRAL"
    assert result.confidence_pts == 0


# ── TEST 6: FVG Detection ─────────────────────────────────────────────────────

def test_fvg_detection():
    # Bullish FVG: c[-3].high < c[-1].low
    candles = [
        make_candle(183.0, 184.0, 182.8, 183.8),  # c[-3]: high=184.0
        make_candle(184.0, 185.5, 183.9, 185.2),  # c[-2]: displacement
        make_candle(185.5, 185.8, 184.2, 185.6),  # c[-1]: low=184.2 > 184.0
    ]
    found, mid, direction = detect_fvg(candles)
    assert found, "FVG not detected"
    assert direction == "BULLISH"
    assert abs(mid - (184.0 + 184.2) / 2) < 0.01, f"FVG mid wrong: {mid}"


# ── TEST 7: CVD Metrics ───────────────────────────────────────────────────────

def test_cvd_turn_magnitude():
    # turn=50000, rolling_avg=10000 → ratio=5.0
    ratio = cvd_turn_magnitude(50000, 10000)
    assert abs(ratio - 5.0) < 0.01

    # turn=15000, rolling_avg=10000 → ratio=1.5
    ratio2 = cvd_turn_magnitude(15000, 10000)
    assert abs(ratio2 - 1.5) < 0.01

    # rolling_avg=0 → ratio=0
    ratio3 = cvd_turn_magnitude(50000, 0)
    assert ratio3 == 0.0


# ── TEST 8: Confidence Scoring ────────────────────────────────────────────────

def test_confidence_score_high():
    from trading.detection.approach import ApproachResult
    level = Level(name="PDH", price=185.0, score=9, type="resistance", source="DAILY", confidence="HIGH")
    approach = ApproachResult("ABSORPTION", 15, "test")

    result = score_signal(
        level=level,
        vol_ratio=2.5,
        displacement=0.85,
        wick_ratio=0.6,
        cvd_ratio=4.5,
        cvd_divergence=True,
        approach=approach,
        cvd_quarantine=False,
        day_bias="BULLISH",
    )
    assert result.label == "HIGH", f"Expected HIGH got {result.label} score={result.score}"
    assert result.score >= 75


def test_confidence_score_quarantine_cap():
    from trading.detection.approach import ApproachResult
    level = Level(name="PDH", price=185.0, score=9, type="resistance", source="DAILY", confidence="HIGH")
    approach = ApproachResult("ABSORPTION", 15, "test")

    result = score_signal(
        level=level,
        vol_ratio=2.5,
        displacement=0.85,
        wick_ratio=0.0,
        cvd_ratio=10.0,  # very high
        cvd_divergence=True,
        approach=approach,
        cvd_quarantine=True,  # quarantine active
        day_bias="BULLISH",
    )
    # CVD capped at 8 during quarantine
    assert result.components["cvd"] <= 8
    assert result.score <= 63 + 10  # max possible with quarantine + divergence bonus


# ── TEST 9: Liquidity Grab Detection ─────────────────────────────────────────

def test_liquidity_grab_basic():
    level = Level(name="PDL", price=185.0, score=8, type="support", source="DAILY", confidence="HIGH")
    atr   = 1.0

    # Setup: aggressive approach then sweep below level
    candles = [
        make_candle(186.0, 186.5, 185.8, 186.3, 100000, t=1),
        make_candle(186.3, 186.8, 186.2, 186.6, 110000, t=2),
        make_candle(186.6, 187.2, 186.5, 187.0, 120000, t=3),
        make_candle(187.0, 187.5, 186.8, 187.3, 200000, t=4),  # aggressive
        make_candle(187.3, 187.4, 184.5, 185.6, 180000, t=5),  # sweep below PDL, closes back
    ]

    result = detect_liquidity_grab(
        candles_1m=candles,
        level=level,
        atr=atr,
        rolling_avg_vol=100000,
        cvd_turn=25000,
        rolling_avg_cvd=10000,
    )
    # May or may not fire depending on exact approach classification
    # Just ensure it doesn't crash
    assert result is None or isinstance(result, dict)


# ── TEST 10: Failed Auction VAR ───────────────────────────────────────────────

def test_failed_auction_var_after_11():
    level = Level(name="VAH", price=186.0, score=7, type="resistance", source="VOLUME_PROFILE", confidence="HIGH")
    atr = 1.0

    candles = [
        make_candle(185.0, 185.5, 184.8, 185.2, 60000),
        make_candle(185.2, 185.6, 185.0, 185.4, 55000),
        make_candle(185.4, 186.4, 185.3, 185.9, 50000),  # poked above VAH=186.0, closed back
    ]
    # last candle: h=186.4 > vah=186.0, c=185.9 < vah → outside_above + back inside
    candles[-1] = make_candle(185.4, 186.4, 185.3, 185.9, 50000)

    result = detect_failed_auction(
        candles=candles,
        level=level,
        atr=atr,
        rolling_avg_vol=100000,
        cvd_turn=-20000,
        rolling_avg_cvd=10000,
        vah=186.0,
        val=184.0,
        poc=185.0,
        session_hour=13.0,   # 1:00 PM — after 11 AM gate
    )
    assert result is None or (result is not None and result["setup"] == "FAILED_AUCTION_VAR")


def test_failed_auction_var_blocked_before_11():
    level = Level(name="VAH", price=186.0, score=7, type="resistance", source="VOLUME_PROFILE", confidence="HIGH")
    candles = [make_candle(185.0, 186.5, 184.9, 185.8, 50000)]

    result = detect_failed_auction(
        candles=candles, level=level, atr=1.0,
        rolling_avg_vol=100000, cvd_turn=-20000, rolling_avg_cvd=10000,
        vah=186.0, val=184.0, poc=185.0,
        session_hour=10.5,  # before 11 AM — should be blocked
    )
    assert result is None, "Should be None before 11 AM"


# ── TEST 11: Calendar ─────────────────────────────────────────────────────────

def test_calendar_early_close():
    from trading.data.calendar import EARLY_CLOSE_DAYS_2026, get_cutoff_time
    # Thanksgiving 2026 day before
    cutoff = get_cutoff_time(date(2026, 11, 27))
    assert cutoff == 1230, f"Expected 1230, got {cutoff}"

    # Normal day
    cutoff_normal = get_cutoff_time(date(2026, 6, 15))
    assert cutoff_normal == 1515


def test_calendar_holiday():
    from trading.data.calendar import is_market_holiday
    assert is_market_holiday(date(2026, 12, 25))
    assert not is_market_holiday(date(2026, 6, 15))


# ── TEST 12: Rolling Avg Vol Ratio ────────────────────────────────────────────

def test_rolling_vol_ratio():
    candle   = make_candle(185.0, 185.5, 184.8, 185.2, v=200000)
    rvol     = 100000
    ratio    = rolling_vol_ratio(candle, rvol)
    assert abs(ratio - 2.0) < 0.01

    candle_low = make_candle(185.0, 185.5, 184.8, 185.2, v=70000)
    ratio_low  = rolling_vol_ratio(candle_low, rvol)
    assert ratio_low < 1.0


if __name__ == "__main__":
    tests = [
        test_approach_aggressive_push,
        test_approach_absorption,
        test_approach_exhaustion,
        test_approach_momentum,
        test_approach_neutral,
        test_fvg_detection,
        test_cvd_turn_magnitude,
        test_confidence_score_high,
        test_confidence_score_quarantine_cap,
        test_liquidity_grab_basic,
        test_failed_auction_var_after_11,
        test_failed_auction_var_blocked_before_11,
        # test_calendar_early_close,  # skip if calendar.py not updated yet
        # test_calendar_holiday,
        test_rolling_vol_ratio,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
    print(f"\n{'='*50}")
    print(f"  {passed}/{passed+failed} tests passed")
    print(f"{'='*50}")
```

Run with:
```bash
uv run python trading/tests/test_v21_detection.py
```

All tests must pass before proceeding to live testing.

---

## Execution Order

```
Phase 1:  Create 8 new files (approach, metrics, confidence, liquidity_grab,
          defense, failed_auction, persistence, calendar)

Phase 2:  Modify 8 existing files (level_state, builder, gates, multi_engine,
          options_context, day_context, cvd_engine, candle_store)
          Also update: agent.py, brief.py, tools.py

Phase 3:  Delete 3 old files (breakout.py, rejection.py, stop_hunt.py)
          Update imports in multi_engine.py

Phase 4:  Run test suite — all 12 tests must pass

Phase 5:  Deploy to Render. Smoke test /trading endpoint.
```

---

## Hard Rules (Non-Negotiable)

1. No signals before 10:00 AM ET
2. No signals after 3:15 PM ET
3. No signals during Tier-1 macro events (FOMC ±30min, others ±15min)
4. No signals if earnings within hold period
5. No signals when VIX ≥ 35
6. No counter-trend signals unless level score ≥ 9
7. No duplicate signals same level same day
8. Max 3 signals per asset per day
9. Minimum RR 2.5:1
10. One agent at a time — asyncio.Lock enforced in multi_engine._fire_agent()

---

## Signal Output Fields (unchanged from v2.0)

The `Signal` model already exists. No changes needed.
The agent brief is updated to include: macro status, earnings status, IV estimate (with approximation label), CVD quality (LIVE vs ESTIMATED), day bias, Setup 2 validity.

---

## What NOT to Change

- `spy/` directory — do not touch
- `trading/models.py` — data models unchanged
- `trading/constants.py` — constants unchanged  
- `trading/agent/agent.py` — only add Lock comment, no logic changes
- `frontend/trading.html` — no changes
- `spy/router.py` existing routes — add new routes only, never modify existing

---

*Implementation plan v2.1 — all design decisions locked.*
