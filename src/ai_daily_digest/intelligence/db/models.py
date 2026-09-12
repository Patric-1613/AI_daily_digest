"""SQLAlchemy models for intelligence persistence — ADR 0011."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from ai_daily_digest.shared.db.metadata import Base

__all__ = [
    "ChangeModel",
    "ChangeSetModel",
    "CurrentFactModel",
    "DigestClaimCitationModel",
    "DigestClaimModel",
    "DigestModel",
    "ExtractedFactModel",
    "SubjectModel",
]


class SubjectModel(Base):
    """Canonical subject identity registry — ADR 0011 §4."""

    __tablename__ = "subjects"

    company_key: Mapped[str] = mapped_column(Text, primary_key=True)
    product_key: Mapped[str] = mapped_column(Text, primary_key=True)
    company: Mapped[str] = mapped_column(Text, nullable=False)
    product: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExtractedFactModel(Base):
    """Whole-row immutable fact evidence — ADR 0011 §4."""

    __tablename__ = "extracted_facts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_key: Mapped[str] = mapped_column(Text, nullable=False)
    product_key: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    disclosure_status: Mapped[str] = mapped_column(String(32), nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_version: Mapped[int] = mapped_column(Integer, nullable=False)
    quoted_span: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_key", "product_key"],
            ["subjects.company_key", "subjects.product_key"],
            ondelete="RESTRICT",
            name="fk_extracted_facts_subject",
        ),
        UniqueConstraint(
            "snapshot_id",
            "company_key",
            "product_key",
            "field",
            "extraction_version",
            name="uq_extracted_facts_attempt",
        ),
        UniqueConstraint(
            "id",
            "company_key",
            "product_key",
            "field",
            "snapshot_id",
            "observed_at",
            "extraction_version",
            name="uq_extracted_facts_composite_identity",
        ),
        CheckConstraint(
            "disclosure_status IN ('disclosed', 'not_disclosed')",
            name="chk_extracted_facts_disclosure_status",
        ),
        CheckConstraint(
            "extraction_method IN ('deterministic', 'llm_structured_output')",
            name="chk_extracted_facts_extraction_method",
        ),
        CheckConstraint(
            "extraction_version >= 1",
            name="chk_extracted_facts_extraction_version",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="chk_extracted_facts_confidence",
        ),
        Index(
            "idx_extracted_facts_subject_field_chronology",
            "company_key",
            "product_key",
            "field",
            "observed_at",
            "snapshot_id",
            "extraction_version",
            "id",
        ),
        Index("idx_extracted_facts_snapshot_id", "snapshot_id"),
    )


class CurrentFactModel(Base):
    """Current confirmed fact pointers — ADR 0011 §4."""

    __tablename__ = "current_facts"

    company_key: Mapped[str] = mapped_column(Text, primary_key=True)
    product_key: Mapped[str] = mapped_column(Text, primary_key=True)
    field: Mapped[str] = mapped_column(Text, primary_key=True)
    fact_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extraction_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_key", "product_key"],
            ["subjects.company_key", "subjects.product_key"],
            ondelete="RESTRICT",
            name="fk_current_facts_subject",
        ),
        ForeignKeyConstraint(
            [
                "fact_id",
                "company_key",
                "product_key",
                "field",
                "snapshot_id",
                "observed_at",
                "extraction_version",
            ],
            [
                "extracted_facts.id",
                "extracted_facts.company_key",
                "extracted_facts.product_key",
                "extracted_facts.field",
                "extracted_facts.snapshot_id",
                "extracted_facts.observed_at",
                "extracted_facts.extraction_version",
            ],
            ondelete="RESTRICT",
            name="fk_current_facts_extracted_fact_composite",
        ),
        CheckConstraint("extraction_version >= 1", name="chk_current_facts_extraction_version"),
    )


class ChangeSetModel(Base):
    """Aggregate grouping of changes for a subject — ADR 0011 §4."""

    __tablename__ = "change_sets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    company_key: Mapped[str] = mapped_column(Text, nullable=False)
    product_key: Mapped[str] = mapped_column(Text, nullable=False)
    review_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_key", "product_key"],
            ["subjects.company_key", "subjects.product_key"],
            ondelete="RESTRICT",
            name="fk_change_sets_subject",
        ),
        UniqueConstraint("id", "company_key", "product_key", name="uq_change_sets_id_subject"),
    )


class ChangeModel(Base):
    """Field-level observation delta — ADR 0011 §4."""

    __tablename__ = "changes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    change_set_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    company_key: Mapped[str] = mapped_column(Text, nullable=False)
    product_key: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    change_type: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    review_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    previous_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    previous_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_snapshots.id", ondelete="RESTRICT"),
        nullable=True,
    )
    current_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["company_key", "product_key"],
            ["subjects.company_key", "subjects.product_key"],
            ondelete="RESTRICT",
            name="fk_changes_subject",
        ),
        ForeignKeyConstraint(
            ["change_set_id", "company_key", "product_key"],
            ["change_sets.id", "change_sets.company_key", "change_sets.product_key"],
            ondelete="RESTRICT",
            name="fk_changes_change_set",
        ),
        UniqueConstraint("change_set_id", "position", name="uq_changes_change_set_position"),
        CheckConstraint("position >= 0", name="chk_changes_position"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="chk_changes_confidence"),
        Index("idx_changes_pagination", "detected_at", "id"),
        Index("idx_changes_subject_field", "company_key", "product_key", "field"),
        Index("idx_changes_change_set_id", "change_set_id"),
        Index("idx_changes_previous_snapshot_id", "previous_snapshot_id"),
        Index("idx_changes_current_snapshot_id", "current_snapshot_id"),
    )


class DigestModel(Base):
    """Daily published or draft update digests — ADR 0011 §4."""

    __tablename__ = "digests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    digest_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'review', 'published')", name="chk_digests_status"),
        Index(
            "uq_digests_one_published_per_date",
            "digest_date",
            unique=True,
            postgresql_where=(status == "published"),
        ),
        Index(
            "idx_digests_pagination",
            "digest_date",
            "id",
            postgresql_where=(status == "published"),
        ),
    )


class DigestClaimModel(Base):
    """Factual claims within a digest — ADR 0011 §4."""

    __tablename__ = "digest_claims"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    digest_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("digests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    change_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("changes.id", ondelete="SET NULL"),
        nullable=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("digest_id", "position", name="uq_digest_claims_digest_position"),
        CheckConstraint("position >= 0", name="chk_digest_claims_position"),
        CheckConstraint(
            "validation_status IN ('pending', 'supported', 'unsupported')",
            name="chk_digest_claims_validation_status",
        ),
        Index("idx_digest_claims_digest_id", "digest_id"),
        Index("idx_digest_claims_change_id", "change_id"),
    )


class DigestClaimCitationModel(Base):
    """Normalized citations for digest claims — ADR 0011 §4."""

    __tablename__ = "digest_claim_citations"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("digest_claims.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("document_snapshots.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("claim_id", "position", name="uq_digest_claim_citations_position"),
        CheckConstraint("position >= 0", name="chk_digest_claim_citations_position"),
        Index("idx_digest_claim_citations_snapshot_id", "snapshot_id"),
    )
