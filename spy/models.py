from dataclasses import dataclass
from typing import Literal
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
    min_remaining: int = 0


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
class Signal:
    direction: Literal["LONG", "SHORT", "WAIT"]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    entry: float
    stop: float
    tp1: float
    tp2: float
    rr: float
    pattern: str
    narrative: str
    reasoning: str
    invalidation: str
    warnings: str = ""
    wait_for: str = ""
    fired_at: str = ""
    level_name: str = ""


@dataclass
class CVDPoint:
    time_et: str
    value: float
    delta: float
