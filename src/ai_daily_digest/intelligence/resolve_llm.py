"""LLM fallback resolver — runs only on the true residue deterministic
matching (resolve.py) left unresolved (method="no_match"). An item that
deterministically phrase-matched two or more tracked subjects
(method="ambiguous_multi_subject") is never sent here — see resolve.py's
module docstring. Resolves to a Subject (company + product), not an
"Entity" — see shared/schemas.py. See docs/LLM_AGENT_SPECS.md#resolve_llm
for the full contract.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import BaseModel

from ai_daily_digest.intelligence.llm import HAIKU, call_structured
from ai_daily_digest.intelligence.prompt_templates import load_prompt, render
from ai_daily_digest.intelligence.resolve import (
    ResolutionResult,
    SubjectAlias,
    load_alias_table,
    quoted_span_supports_subject,
)
from ai_daily_digest.shared.schemas import Confidence, SourceItem, Subject

logger = logging.getLogger("intelligence.resolve_llm")

CONFIDENCE_THRESHOLD = 0.6
# How much of item_text is actually shown to the model as
# item_body_excerpt (see the prompt render below). The evidence-grounding
# check must truncate to this same length before validating
# supporting_quote against it -- otherwise a hallucinated quote that
# happens to coincide with real body text the model was never shown could
# pass as "grounded" even though the model couldn't have read it there.
BODY_EXCERPT_CHARS = 500


class ResolveLLMResponse(BaseModel):
    company: str | None = None
    product: str | None = None
    new_subject_proposal: str | None = None
    # A short verbatim excerpt from item_body_excerpt/item_title naming
    # the specific proposed product -- required for an existing-subject
    # match to be accepted at all (see the grounding check below).
    # Irrelevant, and not checked, when only new_subject_proposal is set.
    supporting_quote: str | None = None
    # Confidence rejects NaN at parse time -- see shared/schemas.py's
    # comment. Without this, confidence=NaN silently passed the
    # "< CONFIDENCE_THRESHOLD" check below (NaN < 0.6 is False).
    confidence: Confidence


def _format_candidates(subjects: list[Subject]) -> str:
    if not subjects:
        return "(no candidate subjects)"
    return "\n".join(f"- {s.company}: {s.product}" for s in subjects)


def _default_call(system: str, prompt: str) -> ResolveLLMResponse:
    return call_structured(
        model=HAIKU,
        system=system,
        prompt=prompt,
        response_model=ResolveLLMResponse,
    )


def resolve_via_llm(
    item: SourceItem,
    candidate_subjects: list[Subject],
    *,
    item_text: str = "",
    alias_table: list[SubjectAlias] | None = None,
    call_fn: Callable[[str, str], ResolveLLMResponse] | None = None,
) -> ResolutionResult:
    """call_fn is injectable for testing — defaults to the real Anthropic
    call via intelligence/llm.py. alias_table defaults to the checked-in
    shared/aliases.yaml (loaded once by the caller and passed through, the
    same pattern resolve_deterministic() uses) — it's needed here too,
    since the grounding check below reuses the exact same canonical-name/
    alias phrase matching resolve.py applies to the full item text.

    Three independent guardrails, ALL required for an auto-merge:
    confidence below CONFIDENCE_THRESHOLD is never auto-merged; neither is
    a company/product the model proposed that isn't actually one of
    `candidate_subjects`; and neither is a candidate-list member the model
    picked without a `supporting_quote` that (a) is verbatim text from the
    item and (b) actually names that specific product
    (`quoted_span_supports_subject()`, intelligence/resolve.py) — candidate
    membership and self-reported confidence alone are never sufficient.
    All three failure modes are logged for manual review instead, never
    auto-merged (see intelligence/CLAUDE.md).

    The grounding check validates `supporting_quote` against title + the
    same BODY_EXCERPT_CHARS-truncated body excerpt actually rendered into
    the prompt, joined with a newline (`evidence_text`) — not the full,
    untruncated `item_text`. The prompt tells the model it may quote
    either `item_title` or `item_body_excerpt`, so validating against body
    text alone would wrongly reject a genuine title-only quote; validating
    against the full body (past what the model was actually shown) would
    let a hallucinated quote pass by coincidentally matching real text the
    model never saw. The newline (not a space) is deliberate too: it stops
    a quote from manufacturing a phrase that spans the title/body boundary
    (e.g. title ending "...OpenAI's new" + body starting "Codex is
    astounding" must never let "new Codex" verify as one contiguous quote
    — the two fields were never actually contiguous prose)."""
    system, user_template = load_prompt("resolve")
    prompt = render(
        user_template,
        item_title=item.title,
        item_body_excerpt=item_text[:BODY_EXCERPT_CHARS],
        candidate_subjects=_format_candidates(candidate_subjects),
    )

    call = call_fn or _default_call
    response = call(system, prompt)
    resolved_alias_table = alias_table if alias_table is not None else load_alias_table()
    # Same bounded evidence the model was actually shown (title + the
    # truncated body excerpt, not the full body) -- see the docstring
    # note above and BODY_EXCERPT_CHARS's. Validating against the
    # untruncated body would let a hallucinated quote pass as "grounded"
    # merely by coinciding with real text past character 500 that the
    # model never actually saw.
    evidence_text = f"{item.title}\n{item_text[:BODY_EXCERPT_CHARS]}"

    proposed_subject = (
        Subject(company=response.company, product=response.product)
        if response.company and response.product
        else None
    )

    if response.confidence < CONFIDENCE_THRESHOLD:
        logger.warning(
            "llm_resolution_low_confidence item_id=%s proposed_subject=%s "
            "confidence=%s -- flagged for manual review, not auto-merged",
            item.id,
            proposed_subject,
            response.confidence,
        )
        result = ResolutionResult(
            item_id=item.id,
            subject=None,
            method="llm_low_confidence",
            confidence=response.confidence,
            matched_text=response.new_subject_proposal,
            candidate_subjects=candidate_subjects,
        )
    elif proposed_subject is not None and proposed_subject in candidate_subjects:
        if quoted_span_supports_subject(
            proposed_subject, response.supporting_quote, evidence_text, resolved_alias_table
        ):
            result = ResolutionResult(
                item_id=item.id,
                subject=proposed_subject,
                method="llm_resolved",
                confidence=response.confidence,
                matched_text=response.supporting_quote,
            )
        else:
            # High confidence, and the proposed subject IS a real
            # candidate -- but candidate-list membership was never the
            # question. Absent, fabricated (not verbatim in the item's
            # title or body), or company-only evidence (no
            # product-specific phrase in the quote) are all rejected the
            # same way: this is what let the government/policy item
            # false-merge onto GPT-4o merely because GPT-4o was the only
            # OpenAI candidate on offer.
            logger.warning(
                "llm_unsupported_subject_evidence item_id=%s proposed_subject=%s "
                "confidence=%s -- quoted evidence missing, fabricated, or does not "
                "name the proposed product; flagged for manual review, not auto-merged",
                item.id,
                proposed_subject,
                response.confidence,
            )
            result = ResolutionResult(
                item_id=item.id,
                subject=None,
                method="llm_unsupported_subject_evidence",
                confidence=response.confidence,
                matched_text=response.supporting_quote,
                candidate_subjects=candidate_subjects,
            )
    elif proposed_subject is not None:
        # High confidence, but the model proposed a company/product that
        # isn't even one of the candidates it was given -- accepting this
        # would let the model invent a subject out of thin air. Treated
        # the same as a new-subject proposal (flagged, not auto-merged),
        # not silently coerced into one of the real candidates.
        logger.warning(
            "llm_resolution_subject_not_in_candidates item_id=%s proposed_subject=%s "
            "candidates=%s -- flagged for manual review, not auto-merged",
            item.id,
            proposed_subject,
            candidate_subjects,
        )
        result = ResolutionResult(
            item_id=item.id,
            subject=None,
            method="llm_subject_not_in_candidates",
            confidence=response.confidence,
            matched_text=f"{proposed_subject.company}: {proposed_subject.product}",
            candidate_subjects=candidate_subjects,
        )
    else:
        result = ResolutionResult(
            item_id=item.id,
            subject=None,
            method="llm_new_subject_proposal",
            confidence=response.confidence,
            matched_text=response.new_subject_proposal,
        )

    logger.info(
        "resolution item_id=%s subject=%s method=%s confidence=%s",
        result.item_id,
        result.subject,
        result.method,
        result.confidence,
    )
    return result
