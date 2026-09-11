"""Subscription persistence lifecycle from ADRs 0012 and 0013.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("normalized_email", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("consent_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("consented_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("confirmed_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("unsubscribed_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("suppressed_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("suppression_reason", sa.String(32)),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("normalized_email", name="uq_subscriptions_normalized_email"),
        sa.CheckConstraint(
            "status IN ('pending','confirmed','unsubscribed','suppressed')",
            name="ck_subscriptions_status",
        ),
        sa.CheckConstraint("consent_generation >= 1", name="ck_subscriptions_generation"),
        sa.CheckConstraint(
            "suppression_reason IS NULL OR suppression_reason IN ('hard_bounce','abuse_complaint','administrative')",
            name="ck_subscriptions_suppression_reason",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND confirmed_at IS NULL AND unsubscribed_at IS NULL AND suppressed_at IS NULL AND suppression_reason IS NULL) OR "
            "(status = 'confirmed' AND confirmed_at IS NOT NULL AND unsubscribed_at IS NULL AND suppressed_at IS NULL AND suppression_reason IS NULL) OR "
            "(status = 'unsubscribed' AND confirmed_at IS NOT NULL AND unsubscribed_at IS NOT NULL AND suppressed_at IS NULL AND suppression_reason IS NULL) OR "
            "(status = 'suppressed' AND suppressed_at IS NOT NULL AND suppression_reason IS NOT NULL)",
            name="ck_subscriptions_state_timestamps",
        ),
    )
    op.create_table(
        "subscription_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("key_id", sa.String(32), nullable=False),
        sa.Column("consent_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("used_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint("token_digest", name="uq_subscription_tokens_token_digest"),
        sa.CheckConstraint(
            "purpose IN ('confirm_subscription','unsubscribe')",
            name="ck_subscription_tokens_purpose",
        ),
        sa.CheckConstraint("consent_generation >= 1", name="ck_subscription_tokens_generation"),
        sa.CheckConstraint(
            "(purpose = 'confirm_subscription' AND expires_at IS NOT NULL) OR (purpose = 'unsubscribe' AND expires_at IS NULL)",
            name="ck_subscription_tokens_expiry",
        ),
    )
    op.create_index(
        "ix_subscription_tokens_subscription_generation",
        "subscription_tokens",
        ["subscription_id", "consent_generation"],
    )
    op.create_table(
        "subscription_consent_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("consent_generation", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("occurred_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("suppression_reason", sa.String(32)),
        sa.UniqueConstraint(
            "subscription_id",
            "consent_generation",
            "event_type",
            name="uq_subscription_consent_event",
        ),
        sa.CheckConstraint("consent_generation >= 1", name="ck_sub_consent_generation"),
        sa.CheckConstraint(
            "event_type IN ('consent_requested','confirmed','unsubscribed','suppressed')",
            name="ck_sub_consent_type",
        ),
        sa.CheckConstraint(
            "(event_type = 'suppressed' AND suppression_reason IN ('hard_bounce','abuse_complaint','administrative')) OR (event_type <> 'suppressed' AND suppression_reason IS NULL)",
            name="ck_sub_consent_reason",
        ),
    )
    op.create_table(
        "subscription_rate_limits",
        sa.Column("scope", sa.String(48), primary_key=True),
        sa.Column("identity_digest", sa.String(64), primary_key=True),
        sa.Column("window_started_at", postgresql.TIMESTAMP(timezone=True), primary_key=True),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("request_count >= 1", name="ck_subscription_rate_limits_count"),
    )
    op.create_index(
        "ix_subscription_rate_limits_expiry", "subscription_rate_limits", ["expires_at"]
    )
    op.execute("""
        CREATE TRIGGER subscription_consent_events_block_update
        BEFORE UPDATE OR DELETE ON subscription_consent_events
        FOR EACH ROW EXECUTE FUNCTION reject_row_mutation();
        CREATE TRIGGER subscription_consent_events_block_truncate
        BEFORE TRUNCATE ON subscription_consent_events
        FOR EACH STATEMENT EXECUTE FUNCTION reject_table_truncate();
    """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER subscription_consent_events_block_truncate ON subscription_consent_events"
    )
    op.execute(
        "DROP TRIGGER subscription_consent_events_block_update ON subscription_consent_events"
    )
    op.drop_index("ix_subscription_rate_limits_expiry", table_name="subscription_rate_limits")
    op.drop_table("subscription_rate_limits")
    op.drop_table("subscription_consent_events")
    op.drop_index(
        "ix_subscription_tokens_subscription_generation", table_name="subscription_tokens"
    )
    op.drop_table("subscription_tokens")
    op.drop_table("subscriptions")
