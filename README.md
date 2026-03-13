# Quartr — AI-Powered Earnings Intelligence Agent

> Institutional-grade earnings analysis powered by LangGraph, GPT-5, and real-time market data.

Quartr is an agentic AI system that analyzes public company earnings calls, SEC filings, news, and competitor data to generate actionable investment signals with price targets.

![Screenshot](docs/screenshot.png)

## Features

- **10-Node Agentic Pipeline** — LangGraph orchestrates parallel research, synthesis, pattern detection, and signal generation
- **8-Quarter Earnings Analysis** — Deep analysis of earnings call transcripts with sentiment tracking
- **SEC Filing Intelligence** — Extracts risk factors and disclosures from 10-K/10-Q filings
- **Pattern Detection** — Identifies language shifts, credibility scores, and management anomalies
- **Price Targets** — Generates specific price targets with upside/downside calculations
- **BUY/HOLD/SELL Signals** — Confidence-weighted investment signals with detailed reasoning
- **Bloomberg-Style Frontend** — Dark terminal UI with live agentic console, charts, and full reports
- **Production API** — FastAPI with CORS, validation, error handling, and health checks
- **One-Click Deploy** — Dockerfile + Railway.toml for instant cloud deployment

## Local Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- OpenAI API key
- Tavily API key

### Installation

```bash
# Clone the repository
git clone https://github.com/your-username/earning-reports-agent.git
cd earning-reports-agent

# Install dependencies
uv sync

# Configure environment
cp .env.example .env
# Edit .env with your API keys
```

### Run

```bash
# Start the API server
uv run uvicorn main:app --reload

# Open the frontend
open http://localhost:8000/frontend/index.html
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key (GPT-5-mini + GPT-5.4) |
| `TAVILY_API_KEY` | Tavily search API key |

## API Documentation

### `GET /health`

Health check endpoint.

**Response:** `{"status": "UP"}`

### `GET /analyze/{ticker}`

Run full earnings intelligence analysis on a stock ticker.

**Parameters:**
- `ticker` (path) — Stock ticker symbol, 1-6 letters (e.g., `NVDA`, `AAPL`)

**Response Schema:**

```json
{
  "ticker": "NVDA",
  "company": "NVIDIA Corporation",
  "current_quarter": "Q1 2026",
  "current_date": "2026-03-12",
  "signal": "BUY",
  "confidence": 85.0,
  "reasoning": "...",
  "price_target": 180.0,
  "price_target_timeframe": "12 months",
  "upside_downside": 15.5,
  "current_price": 155.8,
  "market_cap": "$3.8T",
  "sector": "Technology",
  "pe_ratio": 45.2,
  "revenue_growth": 122.4,
  "gross_margin": 75.3,
  "analyst_consensus": "Strong Buy",
  "num_analysts": 52,
  "credibility_score": 78.0,
  "sentiment_trajectory": [0.2, 0.4, 0.6, 0.7, 0.8, 0.85, 0.9, 0.92],
  "risks": ["..."],
  "catalysts": ["..."],
  "anomalies": ["..."],
  "language_shifts": [{"quarter": "Q3 2025", "shift": "...", "significance": "high"}],
  "guidance_history": [{"quarter": "Q1 2025", "guided": "...", "actual": "...", "met": true}],
  "report": "# Full markdown report..."
}
```

**Error Responses:**
- `400` — Invalid ticker format
- `404` — Company not found
- `500` — Analysis failed

## Deploy to Railway

1. Push your code to GitHub
2. Go to [railway.app](https://railway.app) and create a new project
3. Connect your GitHub repository
4. Add environment variables: `OPENAI_API_KEY`, `TAVILY_API_KEY`
5. Railway will auto-detect the Dockerfile and deploy
6. Your API will be live at `https://your-app.up.railway.app`

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Agent Framework | LangGraph |
| LLMs | GPT-5-mini (synthesis/reports), GPT-5.4 (analysis/signals) |
| API | FastAPI + Uvicorn |
| Search | Tavily (earnings, SEC, news) |
| Market Data | yfinance |
| Frontend | Vanilla HTML/CSS/JS, Chart.js |
| Deployment | Docker, Railway |

## Architecture

```
User Request (ticker)
       │
       ▼
   ┌─────────┐
   │  Intake  │ ← yfinance company info + market data
   └────┬─────┘
        ▼
   ┌──────────────┐
   │ Research Router│ ← determines research gaps
   └──┬──┬──┬──┬──┘
      │  │  │  │     (parallel)
      ▼  ▼  ▼  ▼
  ┌────┐┌───┐┌────┐┌──────────┐
  │Trans││SEC││News││Competitor│ ← Tavily + yfinance
  └──┬─┘└─┬─┘└─┬──┘└────┬─────┘
     │    │    │        │
     ▼    ▼    ▼        ▼
   ┌──────────────────────┐
   │     Synthesis         │ ← GPT-5-mini merges all data
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  Pattern Detection    │ ← GPT-5.4 (loops if data insufficient)
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │  Signal Generator     │ ← GPT-5.4 BUY/HOLD/SELL + price target
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │   Report Writer       │ ← GPT-5-mini comprehensive report
   └──────────────────────┘
```

## License

MIT
