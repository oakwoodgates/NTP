"""signal_events: add bar_close

The closing price of the bar at the moment a SignalEvent fires. Lets
dashboard and analysis code compute mark-to-market metrics (unrealized
PnL on the open position, equity curve including unrealized, etc.)
without needing a separate mark-price feed. Lag is one bar interval at
worst — acceptable for an at-a-glance widget refreshed every ~30s.

Nullable — pre-migration rows carry NULL. The dashboard renders
NULL as ``—`` (no unrealized math possible for old rows).

Revision ID: 004
Revises: 003
Create Date: 2026-05-20
"""

import sqlalchemy as sa

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "signal_events",
        sa.Column("bar_close", sa.Numeric(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("signal_events", "bar_close")
