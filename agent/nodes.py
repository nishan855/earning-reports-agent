import asyncio
from datetime import datetime
import logging

import httpx
import pandas as pd

from agent.state import AgentState
import yfinance as yf
from agent.tools import search
from agent.llm import llm_mini, llm
from langchain_core.messages import HumanMessage


async def async_search(**kwargs) -> list[dict]:
    return await asyncio.to_thread(search, **kwargs)


logger = logging.getLogger(__name__)


def get_quarter_from_date(date: datetime) -> str:
    month: int = date.month
    year: int = date.year
    if month <= 3:
        return f"Q1 {year}"
    elif month <= 6:
        return f"Q2 {year}"
    elif month <= 9:
        return f"Q3 {year}"
    else:
        return f"Q4 {year}"


def get_last_8_quarters(current_quarter: str) -> list[str]:
    quarter_map = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
    reverse_map = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}

    parts = current_quarter.split(" ")
    current_q = quarter_map[parts[0]]
    current_y = int(parts[1])

    quarters = []
    q, y = current_q, current_y

    for _ in range(8):
        q -= 1
        if q == 0:
            q = 4
            y -= 1
        quarters.append(f"{reverse_map[q]} {y}")

    return quarters


def _format_transcripts(transcripts: list[dict] | None) -> str:
    if not transcripts:
        return "No transcripts available"
    return "\n\n".join([f"[{t['quarter']}] {t['content'][:500]}" for t in transcripts])


def _format_list(data: list[dict] | None) -> str:
    if not data:
        return "No data available"
    return "\n\n".join(
        [
            f"- {item.get('content', item.get('company_name', str(item)))[:300]}"
            for item in data
        ]
    )


async def intake_node(state: AgentState) -> dict:
    logger.info(f"intake_node started | ticker: {state.ticker}")

    ticker: str = state.ticker.upper().strip()

    try:
        info = await asyncio.to_thread(lambda: yf.Ticker(ticker=ticker).info)
        if not info or "longName" not in info:
            logger.error(f"Invalid ticker: {ticker}")
            return {
                "ticker": ticker,
                "company_name": "Unknown",
                "errors": [f"Invalid ticker {ticker}"],
            }
    except Exception as e:
        logger.error(f"yfinance error: {e}")
        return {
            "ticker": ticker,
            "company_name": "Unknown",
            "errors": [f"Failed to fetch info: {e}"],
        }

    now: datetime = datetime.now()
    current_date = now.strftime("%Y-%m-%d")
    current_quarter = get_quarter_from_date(now)

    current_price = info.get("currentPrice") or info.get("regularMarketPrice")
    fifty_two_week_high = info.get("fiftyTwoWeekHigh")
    fifty_two_week_low = info.get("fiftyTwoWeekLow")

    raw_cap = info.get("marketCap")
    if raw_cap:
        if raw_cap >= 1_000_000_000_000:
            market_cap = f"${raw_cap / 1_000_000_000_000:.1f}T"
        elif raw_cap >= 1_000_000_000:
            market_cap = f"${raw_cap / 1_000_000_000:.1f}B"
        elif raw_cap >= 1_000_000:
            market_cap = f"${raw_cap / 1_000_000:.1f}M"
        else:
            market_cap = f"${raw_cap:,.0f}"
    else:
        market_cap = None

    sector = info.get("sectorDisp")
    pe_ratio = info.get("trailingPE")
    forward_pe = info.get("forwardPE")
    trailing_eps = info.get("trailingEps")
    forward_eps = info.get("forwardEps")
    revenue_growth_raw = info.get("revenueGrowth")
    revenue_growth = round(revenue_growth_raw * 100, 2) if revenue_growth_raw is not None else None
    gross_margin_raw = info.get("grossMargins")
    gross_margin = round(gross_margin_raw * 100, 2) if gross_margin_raw is not None else None
    analyst_target_price = info.get("targetMeanPrice")
    rec_key = info.get("recommendationKey")
    analyst_consensus = rec_key.replace("_", " ").title() if rec_key else None
    num_analysts = info.get("numberOfAnalystOpinions")

    short_interest_raw = info.get("sharesShort")
    shares_float = info.get("floatShares")
    short_interest = short_interest_raw
    short_percent_float = None
    if short_interest_raw and shares_float and shares_float > 0:
        short_percent_float = round((short_interest_raw / shares_float) * 100, 2)

    insider_raw = info.get("heldPercentInsiders")
    insider_ownership = round(insider_raw * 100, 2) if insider_raw is not None else None

    yf_ticker = yf.Ticker(ticker)

    async def _fetch_calendar():
        try:
            cal = await asyncio.to_thread(lambda: yf_ticker.calendar)
            if cal is not None:
                if isinstance(cal, dict) and "Earnings Date" in cal:
                    dates = cal["Earnings Date"]
                    if dates:
                        return dates[0].strftime("%B %d, %Y")
                elif isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.columns:
                    return cal["Earnings Date"].iloc[0].strftime("%B %d, %Y")
        except Exception:
            pass
        return None

    async def _fetch_quarterly_revenue():
        result = []
        try:
            qf = await asyncio.to_thread(lambda: yf_ticker.quarterly_financials)
            if qf is not None and not qf.empty:
                rev_row = None
                for label in ["Total Revenue", "Revenue"]:
                    if label in qf.index:
                        rev_row = qf.loc[label]
                        break
                if rev_row is not None:
                    for col in list(rev_row.index)[:8]:
                        val = rev_row[col]
                        if val is not None and not (isinstance(val, float) and val != val):
                            quarter_label = get_quarter_from_date(col.to_pydatetime())
                            result.append({"quarter": quarter_label, "revenue": float(val)})
                    result.reverse()
        except Exception as e:
            logger.warning(f"Failed to fetch quarterly revenue: {e}")
        return result

    async def _fetch_quarterly_eps():
        result = []
        try:
            ed = await asyncio.to_thread(lambda: yf_ticker.get_earnings_dates(limit=20))
            if ed is not None and not ed.empty:
                valid = ed.dropna(subset=["Reported EPS", "EPS Estimate"])
                for idx in list(valid.index)[:8]:
                    row = valid.loc[idx]
                    quarter_label = get_quarter_from_date(idx.to_pydatetime())
                    result.append({
                        "quarter": quarter_label,
                        "actual": float(row["Reported EPS"]),
                        "estimate": float(row["EPS Estimate"]),
                    })
                result.reverse()
        except Exception as e:
            logger.warning(f"Failed to fetch quarterly EPS: {e}")
        return result

    next_earnings_date, quarterly_revenue, quarterly_eps = await asyncio.gather(
        _fetch_calendar(), _fetch_quarterly_revenue(), _fetch_quarterly_eps()
    )

    logger.info(f"intake_node completed | {ticker} | {current_quarter}")

    return {
        "ticker": ticker,
        "company_name": info["longName"],
        "current_date": current_date,
        "current_quarter": current_quarter,
        "iteration_count": 0,
        "data_sufficient": False,
        "current_price": current_price,
        "fifty_two_week_high": round(fifty_two_week_high, 2) if fifty_two_week_high is not None else None,
        "fifty_two_week_low": round(fifty_two_week_low, 2) if fifty_two_week_low is not None else None,
        "market_cap": market_cap,
        "sector": sector,
        "pe_ratio": round(pe_ratio, 2) if pe_ratio is not None else None,
        "forward_pe": round(forward_pe, 2) if forward_pe is not None else None,
        "trailing_eps": round(trailing_eps, 2) if trailing_eps is not None else None,
        "forward_eps": round(forward_eps, 2) if forward_eps is not None else None,
        "revenue_growth": revenue_growth,
        "gross_margin": gross_margin,
        "analyst_target_price": round(analyst_target_price, 2) if analyst_target_price is not None else None,
        "analyst_consensus": analyst_consensus,
        "num_analysts": num_analysts,
        "short_interest": short_interest,
        "short_percent_float": short_percent_float,
        "insider_ownership": insider_ownership,
        "next_earnings_date": next_earnings_date,
        "quarterly_revenue": quarterly_revenue if quarterly_revenue else None,
        "quarterly_eps": quarterly_eps if quarterly_eps else None,
        "errors": [],
    }


async def research_router_node(state: AgentState) -> dict:
    logger.info(f"research_router_node invoked — iter {state.iteration_count}")
    if state.iteration_count == 0:
        gaps = ["transcripts", "sec", "news", "competitors"]
        logger.info(f"First pass — queueing all {gaps}")
        return {"research_gaps": gaps}
    logger.info(f"Pass {state.iteration_count} — gaps: {state.research_gaps}")
    return {"research_gaps": state.research_gaps}


async def transcript_node(state: AgentState) -> dict:
    logger.info("transcript_node started")

    if "transcripts" not in state.research_gaps:
        logger.info("transcript_node skipped — not in gaps")
        return {}

    quarters = get_last_8_quarters(state.current_quarter)
    logger.info(f"Fetching transcripts for: {quarters}")

    batch_queries = [
        (quarters[0:3], f"{state.company_name} earnings call transcript {quarters[0]} {quarters[1]} {quarters[2]}"),
        (quarters[3:6], f"{state.company_name} quarterly earnings transcript {quarters[3]} {quarters[4]} {quarters[5]}"),
        (quarters[6:8], f"{state.company_name} earnings call results {quarters[6]} {quarters[7]}"),
    ]

    transcript_domains = ["fool.com", "seekingalpha.com", "alphastreet.com", "gurufocus.com"]

    async def fetch_batch(batch_quarters: list[str], query: str):
        try:
            results = await async_search(
                query=query,
                domains=transcript_domains,
                max_results=5,
                search_depth="advanced",
            )
            transcripts = []
            if results:
                for r in results:
                    matched_quarter = None
                    content_lower = r["content"].lower()
                    for q in batch_quarters:
                        if q.lower() in content_lower:
                            matched_quarter = q
                            break
                    if not matched_quarter:
                        matched_quarter = batch_quarters[0]
                    transcripts.append({
                        "quarter": matched_quarter,
                        "content": r["content"],
                        "url": r["url"],
                    })
            return transcripts
        except Exception as e:
            logger.error(f"transcript_node batch error: {e}")
            return []

    batch_results = await asyncio.gather(*[fetch_batch(bq, q) for bq, q in batch_queries])

    transcripts = []
    seen_urls = set()
    for batch in batch_results:
        for t in batch:
            if t["url"] not in seen_urls:
                seen_urls.add(t["url"])
                transcripts.append(t)

    logger.info(f"transcript_node completed | found {len(transcripts)} transcripts (3 batch searches)")
    return {"transcripts": transcripts}


async def sec_node(state: AgentState) -> dict:
    logger.info("sec_node started")

    if "sec" not in state.research_gaps:
        logger.info("sec_node skipped — not in gaps")
        return {}

    SEC_HEADERS = {"User-Agent": "QernAgent contact@example.com"}

    ITEM_DESCRIPTIONS = {
        "1.01": "Entry into Material Agreement",
        "1.02": "Termination of Material Agreement",
        "2.01": "Acquisition/Disposition of Assets",
        "2.02": "Results of Operations (Earnings)",
        "2.05": "Costs for Exit/Disposal Activities",
        "2.06": "Material Impairments",
        "3.01": "Delisting Notification",
        "4.01": "Change in Accountant",
        "4.02": "Non-Reliance on Prior Financials",
        "5.02": "Officer Departure/Appointment",
        "5.03": "Amendments to Articles/Bylaws",
        "5.07": "Shareholder Vote Results",
        "7.01": "Regulation FD Disclosure",
        "8.01": "Other Events",
        "9.01": "Financial Statements and Exhibits",
    }

    sec_data = []

    async def _fetch_edgar():
        try:
            r = await asyncio.to_thread(
                lambda: httpx.get(
                    "https://www.sec.gov/files/company_tickers.json",
                    headers=SEC_HEADERS,
                    timeout=10,
                )
            )
            cik = None
            for entry in r.json().values():
                if entry["ticker"].upper() == state.ticker.upper():
                    cik = str(entry["cik_str"]).zfill(10)
                    break
            if not cik:
                logger.warning(f"SEC: CIK not found for {state.ticker}")
                return []

            r2 = await asyncio.to_thread(
                lambda: httpx.get(
                    f"https://data.sec.gov/submissions/CIK{cik}.json",
                    headers=SEC_HEADERS,
                    timeout=10,
                )
            )
            recent = r2.json()["filings"]["recent"]

            filings = []
            form4_count = 0
            form4_recent_dates = []

            for i in range(len(recent["form"])):
                form = recent["form"][i]
                date = recent["filingDate"][i]
                accession = recent["accessionNumber"][i].replace("-", "")

                if form in ("10-K", "10-Q"):
                    doc = recent["primaryDocument"][i]
                    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{doc}"
                    filings.append({
                        "type": form,
                        "date": date,
                        "url": url,
                        "content": f"{form} filed on {date}",
                    })

                elif form == "8-K":
                    items_raw = recent["items"][i]
                    item_labels = []
                    if items_raw:
                        for code in items_raw.split(","):
                            label = ITEM_DESCRIPTIONS.get(code.strip(), code.strip())
                            item_labels.append(label)
                    doc = recent["primaryDocument"][i]
                    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{doc}"
                    filings.append({
                        "type": "8-K",
                        "date": date,
                        "items": item_labels,
                        "url": url,
                        "content": f"8-K filed on {date}: {', '.join(item_labels)}",
                    })

                elif form in ("4", "3"):
                    form4_count += 1
                    if len(form4_recent_dates) < 5:
                        form4_recent_dates.append(date)

                if len(filings) >= 10 and form4_count >= 5:
                    break

            if form4_count > 0:
                filings.append({
                    "type": "Insider Activity",
                    "content": (
                        f"{form4_count} insider transaction filings (Form 3/4) found. "
                        f"Most recent: {', '.join(form4_recent_dates)}"
                    ),
                    "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=40",
                })

            return filings
        except Exception as e:
            logger.error(f"EDGAR API error: {e}")
            return []

    async def _fetch_risk_factors():
        try:
            results = await async_search(
                query=f"{state.company_name} {state.ticker} SEC filing risk factors material weaknesses {state.current_quarter}",
                domains=["sec.gov"],
                search_depth="advanced",
            )
            if results:
                return [{
                    "type": "Risk Factors (Search)",
                    "content": results[0]["content"],
                    "url": results[0]["url"],
                }]
        except Exception as e:
            logger.error(f"SEC risk factor search error: {e}")
        return []

    edgar_results, risk_results = await asyncio.gather(
        _fetch_edgar(), _fetch_risk_factors()
    )
    sec_data = edgar_results + risk_results

    logger.info(f"sec_node completed | {len(sec_data)} items (EDGAR + search)")
    return {"sec_data": sec_data}


async def news_node(state: AgentState) -> dict:
    logger.info("news_node started")
    if "news" not in state.research_gaps:
        logger.info("news_node skipped — not in gaps")
        return {}
    queries = [
        f"{state.company_name} {state.ticker} latest news earnings {state.current_quarter}",
        f"{state.company_name} analyst report outlook {state.current_quarter}",
    ]

    async def fetch_one(query: str):
        try:
            return await async_search(
                query=query,
                domains=[
                    "reuters.com",
                    "bloomberg.com",
                    "cnbc.com",
                    "wsj.com",
                    "seekingalpha.com",
                ],
            )
        except Exception as e:
            logger.error(f"news_node error: {e}")
            return []

    all_results = await asyncio.gather(*[fetch_one(q) for q in queries])

    news_data = []
    seen_urls = set()
    for query, results in zip(queries, all_results):
        for result in results:
            if result["url"] not in seen_urls:
                seen_urls.add(result["url"])
                news_data.append(
                    {
                        "title": result.get("title", ""),
                        "content": result["content"],
                        "url": result["url"],
                        "query": query,
                    }
                )

    logger.info(f"news_node completed | found {len(news_data)} articles")
    return {"news_data": news_data}


async def competitor_node(state: AgentState) -> dict:
    logger.info("competitor_node started")

    if "competitors" not in state.research_gaps:
        logger.info("competitor_node skipped — not in gaps")
        return {}

    sector = state.sector or ""
    query = f"{state.company_name} top competitors {sector} {state.current_quarter}"

    results = await async_search(
        query=query,
        domains=["investopedia.com", "reuters.com", "bloomberg.com"],
    )

    competitor_data = []
    if results:
        for result in results[:3]:
            competitor_data.append(
                {
                    "source": "news",
                    "content": result["content"],
                    "url": result["url"],
                }
            )
        logger.info(f"Found {len(competitor_data)} competitor articles")
    else:
        logger.warning("No competitor articles found")

    competitor_data.append(
        {
            "source": "yfinance",
            "ticker": state.ticker,
            "company_name": state.company_name,
            "sector": sector,
            "market_cap": state.market_cap,
            "revenue_growth": state.revenue_growth,
            "gross_margins": state.gross_margin,
        }
    )

    logger.info(f"competitor_node completed | {len(competitor_data)} records")
    return {"competitor_data": competitor_data}


async def synthesis_node(state: AgentState) -> dict:
    logger.info("synthesis_node — passing through (merged into pattern_detection)")
    return {}


async def pattern_detection_node(state: AgentState) -> dict:
    logger.info("pattern_detection_node started")

    research_gaps = []

    if not state.transcripts or len(state.transcripts) < 4:
        logger.warning(f"Transcripts insufficient: {len(state.transcripts or [])}/8")
        research_gaps.append("transcripts")

    if not state.sec_data or len(state.sec_data) == 0:
        logger.warning("SEC data missing")
        research_gaps.append("sec")

    if not state.news_data or len(state.news_data) == 0:
        logger.warning("News data missing")
        research_gaps.append("news")

    if not state.competitor_data or len(state.competitor_data) == 0:
        logger.warning("Competitor data missing")
        research_gaps.append("competitors")

    if research_gaps and state.iteration_count < state.max_iterations:
        logger.info(f"Data insufficient — gaps: {research_gaps}")
        return {
            "research_gaps": research_gaps,
            "data_sufficient": False,
            "iteration_count": state.iteration_count + 1,
        }

    logger.info("Data sufficient — running combined synthesis + pattern + signal analysis")

    raw_data = f"""
=== EARNINGS TRANSCRIPTS ===
{_format_transcripts(state.transcripts)}

=== SEC FILINGS ===
{_format_list(state.sec_data)}

=== NEWS & ANALYST COVERAGE ===
{_format_list(state.news_data)}

=== COMPETITIVE LANDSCAPE ===
{_format_list(state.competitor_data)}
"""

    prompt = f"""
You are a senior investment analyst at a top hedge fund analyzing {state.company_name} ({state.ticker}).

Company: {state.company_name} ({state.ticker})
Quarter: {state.current_quarter}
Current Price: ${state.current_price}
Market Cap: {state.market_cap}
Sector: {state.sector}
PE Ratio: {state.pe_ratio}
Revenue Growth: {state.revenue_growth}%
Gross Margin: {state.gross_margin}%

{raw_data}

Perform THREE tasks in a SINGLE response:

TASK 1 — SYNTHESIS: Merge all raw data above into key insights. Remove noise, highlight financial metrics, note contradictions.
TASK 2 — PATTERN ANALYSIS: Analyze management credibility, sentiment trends, language shifts, anomalies, and guidance accuracy.
TASK 3 — SIGNAL GENERATION: Based on your analysis AND the financial data, generate a BUY/HOLD/SELL signal with price target.

Respond ONLY in this exact JSON format:
{{
    "synthesis": "<3-4 paragraph unified summary of all research data>",
    "credibility_score": <float 0-100>,
    "language_shifts": [
        {{"quarter": "<quarter>", "shift": "<description>", "significance": "<high/medium/low>"}}
    ],
    "sentiment_trajectory": [<float -1.0 to 1.0 per quarter, oldest to newest>],
    "anomalies": ["<anomaly 1>", "<anomaly 2>"],
    "guidance_history": [
        {{"quarter": "<quarter>", "guided": "<amount>", "actual": "<amount>", "met": <true/false>}}
    ],
    "signal": "<BUY|HOLD|SELL>",
    "confidence": <float 0-100>,
    "reasoning": "<4-5 sentence reasoning covering: core thesis, key financial metrics, risk/reward balance, catalyst timeline, confidence explanation>",
    "risks": ["<risk 1>", "<risk 2>", "<risk 3>"],
    "catalysts": ["<catalyst 1>", "<catalyst 2>", "<catalyst 3>"],
    "price_target": <float dollar price target>,
    "price_target_timeframe": "12 months",
    "upside_downside": <float percentage from current price ${state.current_price} to target. Positive = upside, negative = downside>
}}
"""

    try:
        import json

        response = await llm.ainvoke([HumanMessage(content=prompt)])
        raw = str(response.content).strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw)
        logger.info(
            f"Combined analysis complete | credibility: {result.get('credibility_score')} | signal: {result.get('signal')} | confidence: {result.get('confidence')}"
        )

        return {
            "data_sufficient": True,
            "research_gaps": [],
            "synthesis": result.get("synthesis", ""),
            "credibility_score": result.get("credibility_score"),
            "language_shifts": result.get("language_shifts", []),
            "sentiment_trajectory": result.get("sentiment_trajectory", []),
            "anomalies": result.get("anomalies", []),
            "guidance_history": result.get("guidance_history", []),
            "signal": result.get("signal"),
            "confidence": result.get("confidence"),
            "reasoning": result.get("reasoning"),
            "risks": result.get("risks", []),
            "catalysts": result.get("catalysts", []),
            "price_target": result.get("price_target"),
            "price_target_timeframe": result.get("price_target_timeframe", "12 months"),
            "upside_downside": result.get("upside_downside"),
        }

    except Exception as e:
        logger.error(f"pattern_detection_node error: {e}")
        return {
            "data_sufficient": True,
            "research_gaps": [],
            "signal": "HOLD",
            "confidence": 0.0,
            "reasoning": "Analysis failed",
            "errors": state.errors + [str(e)],
        }


async def signal_generator_node(state: AgentState) -> dict:
    logger.info("signal_generator_node — passing through (combined with pattern_detection)")
    return {}


async def report_writer_node(state: AgentState) -> dict:
    logger.info("report_writer_node started")

    upside_str = f"+{state.upside_downside}%" if state.upside_downside and state.upside_downside > 0 else f"{state.upside_downside}%"

    prompt = f"""
You are an elite financial report writer producing institutional-grade research.
Write a comprehensive, data-rich earnings intelligence report.

Use ONLY this data — do not invent anything:

Company: {state.company_name} ({state.ticker})
Quarter: {state.current_quarter}
Date: {state.current_date}
Sector: {state.sector}

Signal: {state.signal}
Confidence: {state.confidence}/100
Reasoning: {state.reasoning}
Price Target: ${state.price_target}
Price Target Timeframe: {state.price_target_timeframe}
Upside/Downside: {upside_str}

Current Price: ${state.current_price}
Market Cap: {state.market_cap}
PE Ratio: {state.pe_ratio}
Revenue Growth: {state.revenue_growth}%
Gross Margin: {state.gross_margin}%
Analyst Consensus: {state.analyst_consensus}
Number of Analysts: {state.num_analysts}

Credibility Score: {state.credibility_score}/100
Sentiment Trajectory: {state.sentiment_trajectory}

Language Shifts:
{state.language_shifts}

Risks:
{chr(10).join([f"- {r}" for r in (state.risks or [])])}

Catalysts:
{chr(10).join([f"- {c}" for c in (state.catalysts or [])])}

Anomalies:
{chr(10).join([f"- {a}" for a in (state.anomalies or [])])}

Guidance History:
{state.guidance_history}

Research Synthesis:
{state.synthesis}

Write the report in clean markdown with EXACTLY these sections:

# {state.company_name} ({state.ticker}) — Earnings Intelligence Report
**Signal: {state.signal} | Confidence: {state.confidence}% | Price Target: ${state.price_target} ({upside_str})**
*Generated: {state.current_date} | Quarter: {state.current_quarter} | Powered by Qern*

## Executive Summary
3-4 paragraphs covering the investment thesis, key financials, and overall outlook.

## Signal & Price Target
| Metric | Value |
|--------|-------|
| Signal | {state.signal} |
| Confidence | {state.confidence}% |
| Price Target | ${state.price_target} |
| Timeframe | {state.price_target_timeframe} |
| Upside/Downside | {upside_str} |

## Key Financials
| Metric | Value |
|--------|-------|
| Current Price | ${state.current_price} |
| Market Cap | {state.market_cap} |
| PE Ratio | {state.pe_ratio} |
| Revenue Growth | {state.revenue_growth}% |
| Gross Margin | {state.gross_margin}% |
| Analyst Consensus | {state.analyst_consensus} |
| Number of Analysts | {state.num_analysts} |

## Earnings Momentum (8 Quarters)
Narrative analysis of last 8 quarters of earnings calls. Quarter by quarter highlights. Revenue trajectory. Guidance accuracy.

## Pattern Analysis
- Credibility Score: {state.credibility_score}/100 — with explanation
- Sentiment Trajectory: narrative describing the trend from {state.sentiment_trajectory}
- Language Shifts: bullet points of significant changes from {state.language_shifts}
- Management Communication style assessment

## Competitive Position
How the company stacks up against competitors. Market share narrative. Competitive moats.

## Risk Factors
For each risk: description, severity (High/Medium/Low), likelihood, and potential impact on thesis.

## Catalysts
For each catalyst: description, timeline, potential upside impact.

## Anomalies — What Management Isn't Saying
For each anomaly: context explaining why it matters to investors.

## SEC Filing Insights
Key findings from 10-K/10-Q analysis. Risk factors buried in filings. Notable disclosures not mentioned on earnings calls.

## Guidance Track Record
| Quarter | Guided | Actual | Beat/Miss | % Variance |
Build this table from the guidance_history data.
Then a credibility assessment paragraph.

## Investment Conclusion
Strong concluding paragraph with clear recommendation, price target justification, and key monitoring points.

---
*Disclaimer: This report is AI-generated for informational purposes only. Not financial advice. Always do your own research.*
"""

    try:
        response = await llm_mini.ainvoke([HumanMessage(content=prompt)])
        report = (
            response.content
            if isinstance(response.content, str)
            else response.content[0]
        )
        logger.info("report_writer_node completed")
        return {"report": report}

    except Exception as e:
        logger.error(f"report_writer_node error: {e}")
        return {
            "report": "Report generation failed",
            "errors": state.errors + [str(e)],
        }
