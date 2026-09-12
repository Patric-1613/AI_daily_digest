import uuid
from datetime import UTC, datetime

from ai_daily_digest.intelligence.draft_claims import draft_change_claim
from ai_daily_digest.shared.attributes import COMPARABLE_FIELDS
from ai_daily_digest.shared.schemas import Change, FactObservation, Subject
from tests.uuid_samples import CHANGE_1, CHANGE_SET_1

TDC_SNAP_CURRENT = uuid.UUID("01a01751-5000-7cb1-a364-e57f1103160f")
TDC_SNAP_PREV = uuid.UUID("019e85a1-4800-7840-bb4d-261cc66dbf1d")

# The orchestrator's injected batch detection time (ADR 0008 section 5.A) --
# .250000 microseconds on purpose, so tests can assert it survives intact.
TDC_DETECTED_AT = datetime(2026, 8, 20, 12, 0, 0, 250000, tzinfo=UTC)


def _subject() -> Subject:
    return Subject(company="OpenAI", product="GPT-4o")


def test_first_disclosure_phrasing_and_single_citation() -> None:
    change = Change(
        id=CHANGE_1,
        change_set_id=CHANGE_SET_1,
        subject=_subject(),
        field="benchmark_scores",
        change_type="disclosed",
        previous=None,
        current=FactObservation(
            value="71.2",
            observed_at=datetime(2026, 8, 19, tzinfo=UTC),
            snapshot_id=TDC_SNAP_CURRENT,
            source_url="https://openai.com/a",  # type: ignore[arg-type]
        ),
        confidence=0.9,
        detected_at=TDC_DETECTED_AT,
    )
    claim = draft_change_claim(change)
    assert "now disclosed as 71.2" in claim.text
    assert claim.citation_snapshot_ids == [TDC_SNAP_CURRENT]
    assert claim.validation_status == "pending"
    assert claim.change_id == CHANGE_1


def test_draft_change_claim_sets_change_id() -> None:
    change = Change(
        id=CHANGE_1,
        change_set_id=CHANGE_SET_1,
        subject=_subject(),
        field="benchmark_scores",
        change_type="disclosed",
        previous=None,
        current=FactObservation(
            value="71.2",
            snapshot_id=TDC_SNAP_CURRENT,
        ),
        confidence=0.9,
        detected_at=TDC_DETECTED_AT,
    )
    claim = draft_change_claim(change)
    assert claim.change_id == CHANGE_1


def test_increased_phrasing_cites_both_snapshots() -> None:
    change = Change(
        id=CHANGE_1,
        change_set_id=CHANGE_SET_1,
        subject=_subject(),
        field="context_window_tokens",
        change_type="increased",
        previous=FactObservation(
            value="128000",
            observed_at=datetime(2026, 6, 2, tzinfo=UTC),
            snapshot_id=TDC_SNAP_PREV,
            source_url="https://openai.com/launch",  # type: ignore[arg-type]
        ),
        current=FactObservation(
            value="256000",
            observed_at=datetime(2026, 8, 20, tzinfo=UTC),
            snapshot_id=TDC_SNAP_CURRENT,
            source_url="https://openai.com/a",  # type: ignore[arg-type]
        ),
        confidence=0.98,
        detected_at=TDC_DETECTED_AT,
    )
    claim = draft_change_claim(change)
    assert "increased to 256000" in claim.text
    assert "up from 128000" in claim.text
    assert set(claim.citation_snapshot_ids) == {TDC_SNAP_CURRENT, TDC_SNAP_PREV}


def test_decreased_phrasing() -> None:
    change = Change(
        id=CHANGE_1,
        change_set_id=CHANGE_SET_1,
        subject=_subject(),
        field="input_price_usd",
        change_type="decreased",
        previous=FactObservation(value="10", snapshot_id=TDC_SNAP_PREV),
        current=FactObservation(value="5", snapshot_id=TDC_SNAP_CURRENT),
        confidence=0.9,
        detected_at=TDC_DETECTED_AT,
    )
    claim = draft_change_claim(change)
    assert "decreased to 5" in claim.text
    assert "down from 10" in claim.text


def test_generic_change_type_falls_back_to_neutral_phrasing() -> None:
    change = Change(
        id=CHANGE_1,
        change_set_id=CHANGE_SET_1,
        subject=_subject(),
        field="licence_terms",
        change_type="changed",
        previous=FactObservation(value="MIT", snapshot_id=TDC_SNAP_PREV),
        current=FactObservation(value="Apache-2.0", snapshot_id=TDC_SNAP_CURRENT),
        confidence=0.8,
        detected_at=TDC_DETECTED_AT,
    )
    claim = draft_change_claim(change)
    assert "changed from MIT to Apache-2.0" in claim.text


def test_field_label_matches_the_same_curated_label_compare_subjects_uses() -> None:
    """A field must read identically whether it appears in a drafted
    change claim or a compare_subjects comparison claim -- both need to
    come from the same COMPARABLE_FIELDS source, not two different
    label-generation strategies."""
    change = Change(
        id=CHANGE_1,
        change_set_id=CHANGE_SET_1,
        subject=_subject(),
        field="context_window_tokens",
        change_type="increased",
        previous=FactObservation(value="128000", snapshot_id=TDC_SNAP_PREV),
        current=FactObservation(value="256000", snapshot_id=TDC_SNAP_CURRENT),
        confidence=0.9,
        detected_at=TDC_DETECTED_AT,
    )
    claim = draft_change_claim(change)
    expected_label = COMPARABLE_FIELDS["context_window_tokens"].lower()
    assert expected_label in claim.text
    assert "context window tokens" not in claim.text  # the old raw-field-key wording


def test_transition_not_disclosed_to_disclosed_phrasing_and_dual_citations() -> None:
    """A transition from previously not disclosed (value=None, snapshot_id present)
    to disclosed (value present, snapshot_id present) cites both snapshots."""
    change = Change(
        id=CHANGE_1,
        change_set_id=CHANGE_SET_1,
        subject=_subject(),
        field="context_window_tokens",
        change_type="disclosed",
        previous=FactObservation(value=None, snapshot_id=TDC_SNAP_PREV),
        current=FactObservation(value="200000", snapshot_id=TDC_SNAP_CURRENT),
        confidence=0.95,
        detected_at=TDC_DETECTED_AT,
    )
    claim = draft_change_claim(change)
    assert "now disclosed as 200000" in claim.text
    assert claim.citation_snapshot_ids == [TDC_SNAP_CURRENT, TDC_SNAP_PREV]


def test_transition_disclosed_to_not_disclosed_phrasing_and_dual_citations() -> None:
    """A transition from previously disclosed to not disclosed cites both snapshots
    and references the previous value in the claim text."""
    change = Change(
        id=CHANGE_1,
        change_set_id=CHANGE_SET_1,
        subject=_subject(),
        field="input_price_usd",
        change_type="not_disclosed",
        previous=FactObservation(value="$5.00", snapshot_id=TDC_SNAP_PREV),
        current=FactObservation(value=None, snapshot_id=TDC_SNAP_CURRENT),
        confidence=0.9,
        detected_at=TDC_DETECTED_AT,
    )
    claim = draft_change_claim(change)
    assert "is no longer disclosed (previously $5.00)" in claim.text
    assert claim.citation_snapshot_ids == [TDC_SNAP_CURRENT, TDC_SNAP_PREV]
