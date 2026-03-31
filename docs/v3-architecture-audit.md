# Signal Engine V3.0 — Spec vs Implementation Audit

**What was planned vs what was actually built — with exact logic, thresholds, and formulas.**

---

## APPROACH CONTEXT CLASSIFIER

### Planned (v2.1 Spec)
```
AGGRESSIVE_PUSH:
  body > ATR × 1.0
  vol_ratio >= 2.0×
  price_progress > ATR × 0.8
  → 12 confidence points

ABSORPTION:
  range < ATR × 0.8
  3 of 4 bars have vol > rolling_avg
  avg_vol(last 4) > rolling_avg × 1.2
  → 15 points

EXHAUSTION:
  bodies[-1] < bodies[-2] < bodies[-3]
  all 3 bodies < ATR × 0.4
  avg_vol(last 3) <= avg_vol(prev 3) × 1.1
  → 15 points

MOMENTUM:
  4 of 5 bars same direction
  all bodies > ATR × 0.15
  total_progress > ATR × 0.5
  → 10 points
```

### Built (approach.py)
```
AGGRESSIVE_PUSH:
  body > ATR × 0.6          ← was 1.0, loosened 40%
  vol_ratio >= 1.5×         ← was 2.0, loosened 25%
  price_progress > ATR × 0.5 ← was 0.8, loosened 37%
  → 12 points (same)

ABSORPTION:
  range < ATR × 1.2         ← was 0.8, loosened 50%
  2 of 4 bars vol > avg × 0.8 ← was 3/4 bars > avg
  avg_vol(4) > avg × 1.0    ← was 1.2, loosened
  → 15 points (same)

EXHAUSTION:
  bodies shrinking (same)
  all 3 < ATR × 0.5         ← was 0.4, loosened 25%
  vol_flat <= avg × 1.3     ← was 1.1, loosened
  → 15 points (same)

MOMENTUM:
  3 of 5 bars same direction ← was 4/5, loosened
  meaningful_bodies check    ← REMOVED entirely
  total_progress > ATR × 0.3 ← was 0.5, loosened 40%
  → 10 points (same)
```

### Why It Changed
Spec thresholds produced 91% NEUTRAL classification on real 1m data. SPY Friday morning: only 3/113 windows matched AGGRESSIVE_PUSH with spec values. After relaxing, NEUTRAL dropped to 30%.

---

## S1: LIQUIDITY GRAB

### Planned (v2.1 Spec) — Sequential Boolean Gates
```python
# All must pass (AND chain):
approach.type == "AGGRESSIVE_PUSH"     # Gate 1
displacement_ratio >= 0.6              # Gate 2
vol_ratio >= 1.5                       # Gate 3
cvd_ratio >= 2.0 (if not quarantined)  # Gate 4
wick_past >= ATR × 0.3                 # Gate 5
→ Then score with confidence scorer
→ score >= 50 to pass
```

### Built (liquidity_grab.py) — V3.0 Trigger & Grade
```python
# Phase 1 TRIGGER (only 2 conditions):
swept_below = candle.l < level.price AND candle.c > level.price  # physical sweep
wick_past >= ATR × 0.1                                          # minimum noise filter

# Phase 2 GRADE (everything else scored):
approach = classify_approach(...)  # calculated, contributes 0-15 pts
vol_ratio → scored 0-20 pts       # was: hard gate >= 1.5
cvd_ratio → scored 0-25 pts       # was: hard gate >= 2.0
displacement → scored 0-20 pts    # was: hard gate >= 0.6

# Phase 3 GATE:
confidence.score >= 50
```

### Delta
| Check | Spec | Built | Change |
|-------|------|-------|--------|
| Approach gate | AGGRESSIVE_PUSH required | Classified, scored 0-15 pts | REMOVED as gate |
| Displacement | >= 0.6 hard gate | Scored in confidence | REMOVED as gate |
| Volume | >= 1.5 hard gate | Scored -5 to +20 pts | REMOVED as gate |
| CVD | >= 2.0 hard gate | Scored -5 to +25 pts | REMOVED as gate |
| Wick depth | >= ATR × 0.3 | >= ATR × 0.1 | LOOSENED 67% |
| Final gate | Score >= 50 | Score >= 50 | SAME |

### Why
The AND chain of 5 independent gates produced 0 signals across 5 days. Probability math: 0.10 × 0.20 × 0.30 × 0.15 × 0.05 = 0.000045 per check. With ~9600 checks/morning, expected detections = 0.4/day.

---

## S2: OB DEFENSE

### Planned (v2.1 Spec)
```python
day_type == "TREND"                    # Hard requirement
day_bias in ("BULLISH", "BEARISH")     # Locked direction
approach.type == "ABSORPTION"          # On 5m candles
find_order_block(): body > ATR × 1.0, vol top 20%
OB not visited 2+ times
tolerance = level.price × 0.0015      # 0.15% proximity
CVD must show defense (turn positive for bull)
```

### Built (defense.py)
```python
day_type == "TREND"                    # Same
day_bias in ("BULLISH", "BEARISH")     # Same
approach.type == "ABSORPTION"          # Same
find_order_block(): body > ATR × 1.0, vol_ratio >= 1.5  # Same
OB not visited 2+ times               # Same
proximity = atr × 0.2                  # CHANGED from 0.15%
CVD must show defense                  # Same
```

### Delta
| Check | Spec | Built | Change |
|-------|------|-------|--------|
| Proximity | level.price × 0.0015 (0.15%) | ATR × 0.2 | DIFFERENT formula |
| Day type | TREND only | TREND only (updates every 5m now) | SAME + improvement |
| Everything else | — | — | MATCH |

### Why S2 Rarely Fires
- Day type classifier marks most days as RANGE (~80%)
- ABSORPTION approach requires tight range + elevated volume — rare on 1m bars
- Combined: S2 fires maybe 1-2 times per week on real data

---

## S3A: FAILED AUCTION (VAR)

### Planned (v2.1 Spec)
```python
session_hour >= 11.0                   # After 11 AM only
# Classic: price outside VAH/VAL, closes back inside
outside_above = candle.h > vah AND candle.c < vah
outside_below = candle.l < val AND candle.c > val
vol_ratio < 0.8                        # LOW volume confirmation (inverse)
cvd_ratio >= 1.5                       # CVD turn
target = POC                           # Always POC
# Called inside level loop for every level
```

### Built (failed_auction.py)
```python
session_hour >= 11.0                   # Same
# Classic: same checks                 # Same
# ADDED: Inside-out touch (v2.2)
inside_touch_vah = candle.h >= vah - proximity AND candle.c < vah
    AND bearish close AND upper_wick > body × 1.5
# Volume: classic uses < 0.8, inside-out uses >= 1.0
# CVD + volume + approach → scored, not gated (V3.0)
target = POC                           # Same
# Called OUTSIDE level loop (v3.0 fix) — uses real VAH/VAL only
```

### Delta
| Check | Spec | Built | Change |
|-------|------|-------|--------|
| Trigger scope | Outside-in only | + inside-out touch | EXPANDED |
| Volume gate | < 0.8 hard gate | Scored in confidence | REMOVED as gate (V3.0) |
| CVD gate | >= 1.5 hard gate | Scored in confidence | REMOVED as gate (V3.0) |
| Loop position | Inside level loop | OUTSIDE level loop | FIXED (was matching 52WH) |

---

## S3B: FAILED AUCTION (MAJOR)

### Planned (v2.1 Spec)
```python
level.score >= 8                       # Required
approach.type == "EXHAUSTION"          # On 5m candles
wick/body >= 2.5                       # Strict rejection shape
vol_ratio >= 1.5                       # Volume top 25%
cvd_ratio >= 2.0                       # CVD turn
# Evaluated on 5m candles
```

### Built (failed_auction.py) — V2.3 + V2.4 + V3.0
```python
level.score >= 8                       # Same
approach in (EXHAUSTION, ABSORPTION, MOMENTUM)  # EXPANDED
wick/body >= 2.0                       # LOOSENED from 2.5
vol_ratio >= 1.0                       # LOOSENED from 1.5 (v2.4)
cvd_ratio → scored, not gated          # REMOVED as gate (V3.0)
# 5m SPOTTER (approach) + 1m SNIPER (trigger) — v2.3 change
```

### Delta
| Check | Spec | Built | Change |
|-------|------|-------|--------|
| Approach types | EXHAUSTION only | + ABSORPTION + MOMENTUM | EXPANDED |
| Wick ratio | >= 2.5 | >= 2.0 | LOOSENED 20% |
| Volume | >= 1.5× hard gate | >= 1.0× (v2.4) → scored (V3.0) | LOOSENED then removed |
| CVD | >= 2.0 hard gate | Scored in confidence | REMOVED as gate |
| Timeframe | 5m bars | 5m spotter + 1m sniper | IMPROVED precision |

---

## CONFIDENCE SCORING

### Planned (v2.1 Spec)
```
LOCATION:   5-20 pts, +2 bonus for POC/VAH/VAL (max 20)
VOLUME:     0-20 pts, no penalties
PRICE:      0-20 pts, +5 wick bonus (max 20)
CVD:        0-25 pts, +10 divergence (max 25), quarantine cap 8
APPROACH:   0-15 pts from classifier
THRESHOLD:  50 (NEUTRAL day: 70)
```

### Built (confidence.py — V3.0 Weighted Factor Model)
```
LOCATION:   8-25 pts, +3 bonus for volume sources (max 25)    ← EXPANDED
VOLUME:     -5 to +20 pts, penalties for weak volume           ← PENALTIES ADDED
PRICE:      0-20 pts, multi-tier wick bonus (+2/+5/+8)        ← RESTRUCTURED
CVD:        -5 to +25 pts, penalties for weak CVD, +10 div    ← PENALTIES ADDED
APPROACH:   0-15 pts from classifier                           ← SAME
THRESHOLD:  50 (NEUTRAL day: 60)                               ← LOOSENED from 70
```

### Exact Scoring Comparison

**Location Quality:**
| Level Score | Spec Points | Built Points |
|-------------|-------------|--------------|
| >= 10 | N/A | 25 |
| >= 9 | 20 | 22 |
| >= 8 | 18 | 18 |
| >= 7 | 15 | 14 |
| >= 6 | 10 | 8 |
| < 6 | 5 | 8 |
| POC/VAH/VAL bonus | +2 (max 20) | +3 (max 25) |

**Volume Signature:**
| Vol Ratio | Spec Points | Built Points |
|-----------|-------------|--------------|
| >= 2.0× | 20 | 20 |
| >= 1.5× | 12 | 15 |
| >= 1.2× | 5 | 10 |
| >= 1.0× | 0 | 5 |
| >= 0.7× | 0 | 0 |
| < 0.7× | 0 | -5 |

**CVD Signature:**
| CVD Ratio | Spec Points | Built Points |
|-----------|-------------|--------------|
| >= 4.0× | 25 | 25 |
| >= 2.0× | 15 | 18 |
| >= 1.0× | 8 | 10 |
| >= 0.5× | 0 | 3 |
| < 0.5× | 0 | -5 |
| Quarantine threshold | >= 2.0 → 8pts | >= 1.0 → 5pts |

---

## HARD RULES (10)

| # | Rule | Spec | Built | Match |
|---|------|------|-------|-------|
| 1 | No signals before 10:00 AM | Hard gate | `is_signal_allowed()` | MATCH |
| 2 | No signals after 3:15 PM | Hard gate | `CUTOFF_MIN=15` | MATCH |
| 3 | No signals during macro events | Hard gate | `is_macro_halt()` | MATCH |
| 4 | No signals if earnings within hold | Hard gate | `is_earnings_within_hold()` | MATCH |
| 5 | VIX >= 35 hard block | Hard gate | `VIX_HARD_BLOCK=35` | MATCH |
| 6 | No counter-trend unless score >= 9 | Hard gate | Agent judgment | SOFTENED |
| 7 | No duplicate same level/day | Hard gate | `tracker._locked` | MATCH |
| 8 | Max 3 signals per asset per day | Hard gate | `MAX_SIGNALS=3` | MATCH |
| 9 | Min RR 2.5:1 | Hard gate | `MIN_RR=2.5` (agent evaluates) | SOFTENED |
| 10 | One agent at a time | `asyncio.Lock` | `_agent_lock` | MATCH |

---

## OPTIONS EXECUTION

| Feature | Spec | Built | Match |
|---------|------|-------|-------|
| Mon-Wed → current Friday | 4-2 DTE | `get_expiry()` | MATCH |
| Thu-Fri → next Friday | 8-7 DTE | `get_expiry()` | MATCH |
| No skip days | Never skip | `skip=False` always | MATCH |
| ATM strikes | Always | `get_strike()` | MATCH |
| VIX < 25 → FULL size | Yes | `get_options_env()` | MATCH |
| VIX 25-30 → HALF | Yes | Yes | MATCH |
| VIX 30-35 → QUARTER + spread | Yes | Yes | MATCH |
| VIX >= 35 → SKIP | Yes | Yes | MATCH |
| Max hold 5 days | Required | NOT BUILT | MISSING |
| Day 3 exit if TP1 not hit | Required | NOT BUILT | MISSING |
| Spread check (ask ≤ est+$0.20) | Required | NOT BUILT | MISSING |
| Position tracking | Required | NOT BUILT | MISSING |

---

## WHAT'S NOT IN SPEC BUT WAS BUILT

| Feature | File | Notes |
|---------|------|-------|
| BREAKOUT_RETEST detector | `level_state.py` | Full v2.0 retest tracking still active |
| Continuation breakout | Removed | Was added then removed per spec |
| Fake Finnhub server | `sim/fake_finnhub.py` | Realistic tick replay for testing |
| Sim clock (`now_et()`) | `context/sim_clock.py` | Allows historical replay |
| Data health tracking | `models.py:DataHealth` | HEALTHY/DEGRADED/STALE per asset |
| WS reconnect + backfill | `data_feed.py`, `candle_store.py` | yfinance gap fill |
| Heartbeat (ghost bar) | `candle_store.py:350` | Force-close bars on time boundaries |
| Signal history API | `main.py:/trading/signals` | Full signal log with filtering |
| Health API | `main.py:/trading/health` | Per-asset data quality |

---

## VERSION EVOLUTION

| Version | What Changed | Why |
|---------|-------------|-----|
| v2.1 | Original spec | Full redesign from v2.0 |
| v2.2 | Proximity model, MOMENTUM for S3B | Pixel-perfect pierce too strict |
| v2.3 | S3B → 5m spotter / 1m sniper | 5m bars washed out wick ratios |
| v2.4 | S3B volume → 1.0× | Wick + volume anti-correlated on 1m |
| V3.0 | Boolean gates → Weighted scoring | AND chain = 0 signals mathematically |

---

## SUMMARY

The system is architecturally the same as the spec — same 4 setups, same approach classifier, same confidence scoring, same gate sequence, same options model. But the **implementation thresholds are systematically looser** than the spec across every detection gate:

- Approach classifier: all thresholds relaxed 25-50%
- S1: 3 of 5 Boolean gates removed, moved to scoring
- S3B: wick ratio and volume loosened, approach types expanded
- Confidence scoring: penalties added for weak factors, thresholds lowered
- Result: more candidates reach the scorer, quality differentiated by points not by binary kill

This was a deliberate architectural pivot (V3.0) after proving that the spec's AND chain produced 0 signals on real market data. The spec was mathematically sound but calibrated for idealized conditions that don't occur on Finnhub free-tier 1m bars.
