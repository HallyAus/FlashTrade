"""Create initial tables: ohlcv, trades, positions, journal.

Revision ID: 001
Revises: None
Create Date: 2026-02-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ohlcv",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("timeframe", sa.String(5), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.BigInteger(), nullable=False),
        sa.Column("high", sa.BigInteger(), nullable=False),
        sa.Column("low", sa.BigInteger(), nullable=False),
        sa.Column("close", sa.BigInteger(), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ohlcv_symbol_tf_ts", "ohlcv", ["symbol", "timeframe", "timestamp"], unique=True)
    op.create_index("ix_ohlcv_market", "ohlcv", ["market"])

    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("order_type", sa.String(10), nullable=False),
        sa.Column("quantity_cents", sa.BigInteger(), nullable=False),
        sa.Column("price_cents", sa.BigInteger(), nullable=False),
        sa.Column("stop_loss_cents", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("strategy", sa.String(50), nullable=False),
        sa.Column("broker_order_id", sa.String(100), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("side", sa.String(5), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("entry_price_cents", sa.BigInteger(), nullable=False),
        sa.Column("current_price_cents", sa.BigInteger(), nullable=False),
        sa.Column("stop_loss_cents", sa.BigInteger(), nullable=False),
        sa.Column("strategy", sa.String(50), nullable=False),
        sa.Column("unrealized_pnl_cents", sa.BigInteger(), default=0),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol"),
    )

    op.create_table(
        "journal",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("strategy", sa.String(50), nullable=False),
        sa.Column("signal_data", sa.Text(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("outcome_cents", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("journal")
    op.drop_table("positions")
    op.drop_table("trades")
    op.drop_index("ix_ohlcv_market", table_name="ohlcv")
    op.drop_index("ix_ohlcv_symbol_tf_ts", table_name="ohlcv")
    op.drop_table("ohlcv")
