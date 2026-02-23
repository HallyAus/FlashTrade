"""Shared test fixtures."""

import pytest

from app.services.risk_manager import RiskManager


@pytest.fixture
def risk_manager() -> RiskManager:
    """Fresh risk manager for each test."""
    return RiskManager()
