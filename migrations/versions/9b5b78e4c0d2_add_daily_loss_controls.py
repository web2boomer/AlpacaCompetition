"""add daily loss validation controls

Revision ID: 9b5b78e4c0d2
Revises: fdebf92c8f1c
Create Date: 2026-09-01 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b5b78e4c0d2"
down_revision: str | None = "fdebf92c8f1c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_loss_controls",
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("confirmation_count", sa.Integer(), nullable=False),
        sa.Column("first_breach_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_loss", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("defined_loss_envelope", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("quote_quality_passed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=80), nullable=False),
        sa.PrimaryKeyConstraint("session_date"),
    )


def downgrade() -> None:
    op.drop_table("daily_loss_controls")
