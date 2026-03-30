from ..models import Level
from ..constants import CONFLUENCE_BOOST, MIN_LEVEL_SCORE

# Source priority for tiebreaking when two levels have the same score
# Higher = more institutional significance, wins the dedup
SOURCE_PRIORITY = {
    "52W": 10, "MONTHLY": 9, "WEEKLY": 8, "PD": 7, "OR": 6,
    "ZONE": 5, "VOLUME": 4, "PM": 3, "VWAP": 2,
}


def apply_confluence(levels: list[Level]) -> list[Level]:
    if len(levels) < 2:
        return levels
    levels = _deduplicate(levels, threshold_pct=0.001)
    for i, lvl in enumerate(levels):
        nearby = [
            other.name for j, other in enumerate(levels)
            if j != i and lvl.price > 0
            and abs(other.price - lvl.price) / lvl.price <= 0.003
        ]
        if nearby:
            lvl.confluence_with = nearby
            lvl.score = min(12, lvl.score + CONFLUENCE_BOOST)
    # Filter out below minimum score
    levels = [l for l in levels if l.score >= MIN_LEVEL_SCORE]
    return levels


def _deduplicate(levels: list[Level], threshold_pct: float) -> list[Level]:
    # Deterministic sort: score first, then source priority for tiebreaking
    sorted_levels = sorted(levels, key=lambda l: (l.score, SOURCE_PRIORITY.get(l.source, 0)), reverse=True)
    result = []
    for lvl in sorted_levels:
        too_close = any(
            abs(existing.price - lvl.price) / lvl.price <= threshold_pct
            for existing in result
            if lvl.price > 0
        )
        if not too_close:
            result.append(lvl)
    return result


def score_level_by_test_count(level: Level, tests_today: int) -> int:
    if tests_today <= 1:
        return level.score
    elif tests_today == 2:
        return level.score - 1
    elif tests_today == 3:
        return level.score - 2
    else:
        return max(3, level.score - 3)
