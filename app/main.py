"""FlashTrade — FastAPI application entry point."""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, dashboard, trades
from app.config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

app = FastAPI(
    title="FlashTrade",
    description="Algorithmic trading system for ASX, US stocks, and crypto",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.app_env == "development" else settings.allowed_hosts.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(dashboard.router)
app.include_router(trades.router)
app.include_router(admin.router)


@app.get("/")
async def root():
    return {
        "name": "FlashTrade",
        "version": "0.1.0",
        "status": "running",
        "trading_mode": settings.trading_mode,
    }
