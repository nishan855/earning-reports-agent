import logging
from langchain_core.tools import tool
from langchain_tavily import TavilySearch
import yfinance as yf

logger = logging.getLogger(__name__)

tavily_basic = TavilySearch(max_results=5, search_depth="basic")
tavily_advanced = TavilySearch(max_results=5, search_depth="advanced")


def search(
    query: str,
    domains: list[str] = [],
    max_results: int = 5,
    search_depth: str = "basic",
) -> list[dict]:
    """
    Wrapper around TavilySearch.
    Returns clean list of {url, content} dicts.
    search_depth: "basic" or "advanced"
    """
    try:
        client = tavily_advanced if search_depth == "advanced" else tavily_basic
        response = client.invoke(query)
        return response.get("results", [])
    except Exception as e:
        logger.error(f"search error: {e}")
        return []


@tool
def get_stock_info(ticker: str) -> dict:
    """Fetches basic stock info — company name, sector, market cap, PE ratio."""
    logger.info(f"Tool called (get_stock_info): {ticker}")
    try:
        info = yf.Ticker(ticker).info
        return {
            "ticker": ticker,
            "company_name": info.get("longName", "Unknown"),
            "sector": info.get("sector", "Unknown"),
            "market_cap": info.get("marketCap", None),
            "pe_ratio": info.get("trailingPE", None),
            "revenue": info.get("totalRevenue", None),
        }
    except Exception as e:
        logger.error(f"Tool: get_stock_info error: {e}")
        return {"ticker": ticker, "error": str(e)}


tools = [search, get_stock_info]
