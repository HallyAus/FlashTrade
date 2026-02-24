# FlashTrade — Implementation Guide

> This file lives inside the codebase. It has implementation-specific shortcuts
> that help Claude Code work efficiently within the code.
> For project-level context, see ../CLAUDE.md

## Project Structure
```
FlashTrade/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app entry point
│   ├── config.py             # Settings from env vars (pydantic-settings)
│   ├── models/               # SQLAlchemy/Pydantic models
│   │   ├── ohlcv.py          # OHLCV candlestick data
│   │   ├── trade.py          # Trade records
│   │   ├── position.py       # Open positions
│   │   └── journal.py        # Trade journal entries
│   ├── services/
│   │   ├── data/             # Data ingestion
│   │   │   ├── yfinance_feed.py
│   │   │   ├── ccxt_feed.py
│   │   │   └── alpaca_feed.py
│   │   ├── strategy/         # Trading strategies
│   │   │   ├── base.py       # Abstract strategy class
│   │   │   ├── momentum.py   # RSI + MACD
│   │   │   └── meanrev.py    # Bollinger Bands
│   │   ├── backtest/         # Backtesting engine (ADR-007)
│   │   │   ├── engine.py     # Walk-forward loop + data loading
│   │   │   ├── broker.py     # Position mgmt, fees, stop-loss sim
│   │   │   ├── metrics.py    # Sharpe, drawdown, win rate, profit factor
│   │   │   └── result.py     # BacktestResult + ClosedTrade dataclasses
│   │   ├── execution/        # Order execution
│   │   │   ├── alpaca_executor.py
│   │   │   ├── swyftx_executor.py
│   │   │   └── paper_executor.py
│   │   ├── risk_manager.py   # ALL trades go through here
│   │   └── alerting.py       # Email/push notifications
│   ├── tasks/                # Celery async tasks
│   │   ├── data_tasks.py     # Scheduled data pulls
│   │   ├── trade_tasks.py    # Strategy evaluation + execution
│   │   └── monitoring_tasks.py
│   ├── api/                  # FastAPI routes
│   │   ├── dashboard.py
│   │   ├── trades.py
│   │   └── admin.py          # Kill switch, config
│   └── dashboard/            # Frontend (Streamlit or React)
├── scripts/
│   ├── ingest.py             # CLI for data ingestion
│   ├── backtest.py           # CLI for backtesting
│   └── trade.py              # CLI for trading (paper/live/kill)
├── tests/
│   ├── test_risk_manager.py  # MUST have >90% coverage
│   ├── test_execution.py     # MUST have >90% coverage
│   └── ...
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── CLAUDE.md                 # (this file)
```

## Quick Commands
```bash
# Run the app
docker compose up -d
docker compose logs -f trader

# Run tests
docker compose exec trader python -m pytest -v
docker compose exec trader python -m pytest --cov=app --cov-report=term-missing

# Backtesting
docker compose exec trader python scripts/backtest.py --strategy momentum --symbol BTC --market crypto --timeframe 1h --days 180
docker compose exec trader python scripts/backtest.py --strategy auto --symbol BHP.AX --market asx --timeframe 1d --days 365

# Linting
black app/ scripts/ tests/
isort app/ scripts/ tests/
mypy app/

# Database
docker compose exec postgres psql -U autotrader -d autotrader

# Beads
bd list --status open
bd next
bd create "task description" --priority P2
```

## Key Patterns

### Every trade goes through RiskManager
```python
# CORRECT
risk_check = risk_manager.evaluate(order)
if risk_check.approved:
    executor.submit(order)

# WRONG — never bypass risk manager
executor.submit(order)  # NO!
```

### All money in cents internally
```python
# CORRECT
position_size_cents = 5000  # $50.00
display_amount = position_size_cents / 100

# WRONG
position_size = 50.00  # floating point money = bugs
```

### UTC everywhere, AEST for display only
```python
from datetime import datetime, timezone
now = datetime.now(timezone.utc)  # CORRECT
# Convert to AEST only in dashboard/API responses
```

### Config from environment, never hardcoded
```python
# CORRECT
from app.config import settings
api_key = settings.alpaca_api_key

# WRONG
api_key = "AKXXXXXXXXX"  # NEVER
```

## Testing Requirements
- `risk_manager.py` — minimum 90% coverage, test ALL edge cases
- `execution/` — minimum 90% coverage, mock all broker APIs
- `strategy/` — backtest results must be reproducible
- All tests must pass before any commit to main

## Dependencies (key packages)
```
fastapi>=0.109.0
uvicorn>=0.27.0
sqlalchemy>=2.0.25
celery>=5.3.6
redis>=5.0.1
yfinance>=0.2.36
ccxt>=4.2.0
alpaca-trade-api>=3.0.0
pandas>=2.2.0
numpy>=1.26.0
pydantic-settings>=2.1.0
httpx>=0.26.0
```
