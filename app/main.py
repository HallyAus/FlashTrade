"""FlashTrade — FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import admin, dashboard, trades
from app.config import settings
from app.database import engine

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: verify DB connection. Shutdown: dispose engine."""
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("Database connected successfully")
    except Exception as e:
        logger.error("Database connection failed: %s", e)
        raise
    yield
    await engine.dispose()
    logger.info("Database engine disposed")


app = FastAPI(
    title="FlashTrade",
    description="Algorithmic trading system for ASX, US stocks, and crypto",
    version="0.7.0",
    lifespan=lifespan,
)

# CORS: restrict in production, allow localhost in development
_allowed_origins = (
    ["http://localhost:8000", "http://localhost:3000"]
    if settings.app_env == "development"
    else settings.allowed_hosts.split(",")
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
)

app.include_router(dashboard.router)
app.include_router(trades.router)
app.include_router(admin.router)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def root():
    """Serve the dashboard UI."""
    return FileResponse(str(static_dir / "index.html"))


@app.get("/api")
async def api_root():
    return {
        "name": "FlashTrade",
        "version": "0.7.0",
        "status": "running",
        "trading_mode": settings.trading_mode,
    }
