"""Link digest claims to originating changes (ADR 0014 / Issue #122).

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "digest_claims",
        sa.Column(
            "change_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("changes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_digest_claims_change_id",
        "digest_claims",
        ["change_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_digest_claims_change_id", table_name="digest_claims")
    op.drop_column("digest_claims", "change_id")
