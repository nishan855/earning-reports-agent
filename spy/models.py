from dataclasses import dataclass, field
from typing import Optional, Literal
from enum import Enum


class TrendDirection(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGING = "RANGING"
    UNKNOWN = "UNKNOWN"


class SessionId(str, Enum):
    CLOSED    = "CLOSED"
    PREMARKET = "PREMARKET"
    OR        = "OR"
    POWER     = "POWER"
    MID       = "MID"
    DEAD      = "DEAD"
    AFT       = "AFT"
    CLOSE     = "CLOSE"
    AH        = "AH"


@dataclass
class Candle:
    t: int       # timestamp ms
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class Session:
    id: SessionId
    label: str
    color: str
    quality: int          # 0-5, 5 = best
    signal_threshold: int # min score to fire


@dataclass
class Level:
    price: float
    label: str
    type: Literal["resistance", "support", "dynamic", "round", "pivot"]
    strength: int         # 1-4
    source: str


@dataclass
class OpeningRange:
    high: float
    low: float
    complete: bool
    bar_count: int

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass
class PreMarketData:
    pd_high: float
    pd_low: float
    pd_close: float
    pm_high: float
    pm_low: float
    gap_pct: float
    gap_type: Literal["GAP UP", "GAP DOWN", "FLAT"]
    gap_fill: float


@dataclass
class VixData:
    value: float
    label: str
    color: str
    tradeable: bool
    size_multiplier: float
    note: str


@dataclass
class StopHunt:
    type: Literal["BULLISH", "BEARISH"]
    level: float
    wick_size: float


@dataclass
class PriceAction:
    type: Literal["BREAKOUT", "RETEST", "REJECTION"]
    level: Level
    strength: Literal["HIGH", "MEDIUM", "LOW"]


@dataclass
class FactorResult:
    id: str
    layer: Literal["CONTEXT", "SETUP", "TIMING"]
    label: str
    ok: bool
    is_bonus: bool
    val: str
    color: str
    reason: str
    missing: Optional[str]
    weight: int


@dataclass
class FactorEngineResult:
    factors: list[FactorResult]
    context_score: int
    setup_score: int
    timing_score: int
    total_score: int
    all_ok: bool
    bias: TrendDirection
    vwap: float
    atr: float
    rr: float
    sl: float
    tp1: float
    tp2: float
    near_resistance: Optional[Level]
    near_support: Optional[Level]
    price_action: Optional[PriceAction]
    stop_hunt: Optional[StopHunt]
    or_data: Optional[OpeningRange]
    last_price: float
    evaluated_at: int


@dataclass
class Signal:
    direction: Literal["LONG", "SHORT", "SKIP"]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    entry_type: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    rr: float
    narrative: str
    reasoning: str
    invalidation: str
    size_note: str
    key_risk: str
    generated_at: int
    stream_complete: bool = False
