"""Application configuration loaded from environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All config from environment. Never hardcode secrets."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://autotrader:changeme@postgres:5432/autotrader"
    )

    # Redis
    redis_url: str = Field(default="redis://redis:6379/0")

    # Alpaca
    alpaca_api_key: str = Field(default="")
    alpaca_secret_key: str = Field(default="")
    alpaca_base_url: str = Field(
        default="https://paper-api.alpaca.markets"
    )

    # Swyftx
    swyftx_api_key: str = Field(default="")
    swyftx_access_token: str = Field(default="")
    swyftx_refresh_token: str = Field(default="")

    # Trading
    trading_mode: str = Field(default="paper")

    # Risk limits (cents for money, percentages as floats)
    max_position_size_cents: int = Field(default=10000)  # $100.00
    max_daily_drawdown_pct: float = Field(default=5.0)
    max_per_trade_risk_pct: float = Field(default=2.0)
    circuit_breaker_consecutive_losses: int = Field(default=3)
    circuit_breaker_pause_minutes: int = Field(default=60)

    # App
    app_env: str = Field(default="development")
    log_level: str = Field(default="INFO")
    secret_key: str = Field(default="")
    allowed_hosts: str = Field(default="https://trade.printforge.com.au,http://localhost:8000")

    # API authentication key for admin/trade endpoints (set in .env)
    api_key: str = Field(default="")


settings = Settings()
