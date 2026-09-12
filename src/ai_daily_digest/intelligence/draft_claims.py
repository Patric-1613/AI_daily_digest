"""Deterministic claim drafting — the "Draft digest claims" step for
mechanical, single-field changes. docs/ARCHITECTURE.md: "Prefer
deterministic code for URL normalization, hashing, dates, numeric
comparison, citation validation, and deduplication. Use an LLM ... only
where deterministic rules are insufficient." A field going from one value
to another is exactly the case deterministic code should own; free-form
prose comparisons across *different* subjects are a separate, model-
backed step (not built yet — see docs/LLM_AGENT_SPECS.md).
"""

from __future__ import annotations

from ai_daily_digest.intelligence.facts import change_snapshot_ids
from ai_daily_digest.shared.attributes import field_label as _field_label_for
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.schemas import (
    Change,
    ClaimValidationStatus,
    DigestClaim,
    FactObservation,
)


def _require_previous(change: Change) -> FactObservation:
    """Narrows change.previous from Optional to a real FactObservation.
    Change._require_valid_change_shape (validate_change_shape(),
    shared/schemas.py) already guarantees `previous` is grounded for
    every change_type draft_change_claim() calls this from -- everything
    except "disclosed", which can legitimately have previous=None (a
    first-ever disclosure) and never calls this. An explicit raise, not
    `assert`: this codebase avoids bare `assert` in shipped code (it's
    stripped under -O, and bandit's B101 flags it) even for a "this can't
    actually happen" invariant -- the raise is what makes that invariant
    a real, always-enforced check, not a check only in unoptimized runs,
    while still narrowing the type for every caller below."""
    if change.previous is None:
        raise AssertionError(
            "unreachable: validate_change_shape() guarantees change.previous is set "
            f"for change_type={change.change_type!r}"
        )
    return change.previous


def draft_change_claim(change: Change) -> DigestClaim:
    """One DigestClaim per Change. validation_status starts "pending" —
    intelligence/validate.py is what's allowed to mark it supported."""
    subject_name = f"{change.subject.company}'s {change.subject.product}"
    label = _field_label_for(change.field)

    if change.change_type == "not_disclosed":
        previous = _require_previous(change)
        text = f"{subject_name}'s {label} is no longer disclosed (previously {previous.value})."
    elif change.change_type == "disclosed":
        text = f"{subject_name}'s {label} is now disclosed as {change.current.value}."
    elif change.change_type == "increased":
        previous = _require_previous(change)
        text = (
            f"{subject_name}'s {label} increased to {change.current.value}, "
            f"up from {previous.value}."
        )
    elif change.change_type == "decreased":
        previous = _require_previous(change)
        text = (
            f"{subject_name}'s {label} decreased to {change.current.value}, "
            f"down from {previous.value}."
        )
    else:
        previous = _require_previous(change)
        text = f"{subject_name}'s {label} changed from {previous.value} to {change.current.value}."

    current_id, previous_id = change_snapshot_ids(change)
    citation_ids = [current_id] if current_id else []
    if previous_id and previous_id not in citation_ids:
        citation_ids.append(previous_id)

    return DigestClaim(
        id=new_id(),
        change_id=change.id,
        text=text,
        citation_snapshot_ids=citation_ids,
        validation_status=ClaimValidationStatus.PENDING,
    )
