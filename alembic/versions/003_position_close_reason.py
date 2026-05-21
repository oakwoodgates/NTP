"""positions: add closing_order_id + close_reason

Capture WHY a position closed (protective stop, liquidation mixin,
strategy cross-flip exit, or unknown) so the dashboard can show
operators the failure mode at a glance and downstream analysis can
filter / group by close reason.

Both columns are nullable — historical positions (closed before this
migration) will carry NULL, which the dashboard renders as ``—``.

Revision ID: 003
Revises: 002
Create Date: 2026-05-20
"""

import sqlalchemy as sa

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("closing_order_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "positions",
        sa.Column("close_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions", "close_reason")
    op.drop_column("positions", "closing_order_id")
