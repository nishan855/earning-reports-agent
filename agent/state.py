from typing import Optional

from pydantic import BaseModel


class AgentState(BaseModel):
    ticker: str = ""
    company_name: str = ""
    current_date: str = ""
    current_quarter: str = ""

    iteration_count: int = 0
    max_iterations: int = 1
    data_sufficient: bool = False
    research_gaps: list[str] = []

    transcripts: Optional[list[dict]] = None
    sec_data: Optional[list[dict]] = None
    news_data: Optional[list[dict]] = None
    competitor_data: Optional[list[dict]] = None

    synthesis: Optional[str] = None
    guidance_history: Optional[list[dict]] = None
    language_shifts: Optional[list[dict]] = None
    sentiment_trajectory: Optional[list[float]] = None
    credibility_score: Optional[float] = None
    anomalies: Optional[list[str]] = None

    signal: Optional[str] = None
    confidence: Optional[float] = None
    reasoning: Optional[str] = None
    risks: Optional[list[str]] = None
    catalysts: Optional[list[str]] = None

    price_target: Optional[float] = None
    price_target_timeframe: Optional[str] = None
    upside_downside: Optional[float] = None

    current_price: Optional[float] = None
    fifty_two_week_high: Optional[float] = None
    fifty_two_week_low: Optional[float] = None
    market_cap: Optional[str] = None
    sector: Optional[str] = None
    pe_ratio: Optional[float] = None
    forward_pe: Optional[float] = None
    trailing_eps: Optional[float] = None
    forward_eps: Optional[float] = None
    revenue_growth: Optional[float] = None
    gross_margin: Optional[float] = None
    analyst_target_price: Optional[float] = None
    analyst_consensus: Optional[str] = None
    num_analysts: Optional[int] = None
    short_interest: Optional[float] = None
    short_percent_float: Optional[float] = None
    insider_ownership: Optional[float] = None
    next_earnings_date: Optional[str] = None
    quarterly_revenue: Optional[list[dict]] = None
    quarterly_eps: Optional[list[dict]] = None

    report: Optional[str] = None

    errors: list[str] = []
