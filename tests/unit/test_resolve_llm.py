"""Tests the plumbing (prompt rendering, confidence gating, result
shaping) with an injected fake call_fn — no network/API key needed. The
real call_structured() path is exercised by intelligence/llm.py's own
tests, not here."""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ai_daily_digest.intelligence.prompt_templates import load_prompt, render
from ai_daily_digest.intelligence.resolve_llm import (
    BODY_EXCERPT_CHARS,
    ResolveLLMResponse,
    resolve_via_llm,
)
from ai_daily_digest.shared.schemas import SourceItem, Subject

TRL_ITEM_TEST = uuid.UUID("01a01e2f-3770-7bc0-967a-19297e60ec0c")


def _item(title: str = "Some ambiguous headline") -> SourceItem:
    return SourceItem(
        id=TRL_ITEM_TEST,
        dedupe_key=f"sha256:{TRL_ITEM_TEST}",
        source_id="test-source",
        publisher="Test Publisher",
        title=title,
        canonical_url="https://example.com/a",  # type: ignore[arg-type]
        first_fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def _candidates() -> list[Subject]:
    return [Subject(company="OpenAI", product="GPT-4o")]


def test_prompt_loads_and_renders() -> None:
    system, user_template = load_prompt("resolve")
    assert "Output JSON only" in system
    rendered = render(
        user_template,
        item_title="Test Title",
        item_body_excerpt="Test body",
        candidate_subjects="- OpenAI: GPT-4o",
    )
    assert "Test Title" in rendered
    assert "{{item_title}}" not in rendered


def test_high_confidence_resolution() -> None:
    """The accept path now also requires a supporting_quote that is (a)
    verbatim item text and (b) actually names the proposed product --
    candidate-list membership and confidence alone are no longer enough
    (see test_high_confidence_with_unsupported_evidence_is_not_auto_merged
    for the case this guards against)."""
    item_text = "OpenAI's GPT-4o now ships a new safety evaluation suite."

    def fake_call(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(
            company="OpenAI",
            product="GPT-4o",
            supporting_quote="GPT-4o now ships a new safety evaluation suite",
            confidence=0.9,
        )

    result = resolve_via_llm(
        _item(), _candidates(), item_text=item_text, alias_table=[], call_fn=fake_call
    )
    assert result.subject == Subject(company="OpenAI", product="GPT-4o")
    assert result.method == "llm_resolved"


def test_high_confidence_with_unsupported_evidence_is_not_auto_merged() -> None:
    """The exact rehearsal bug: high confidence, and the proposed subject
    IS a real candidate (it's the only OpenAI product in the catalogue at
    the time), but nothing in the item text actually names it. Candidate
    membership and confidence must never be sufficient on their own."""

    def fake_call(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(company="OpenAI", product="GPT-4o", confidence=0.85)

    result = resolve_via_llm(
        _item("Expanding AI access and cyber defense for governments"),
        _candidates(),
        item_text="OpenAI and GSA will offer eligible governments expanded cyber defense support.",
        alias_table=[],
        call_fn=fake_call,
    )
    assert result.subject is None
    assert result.method == "llm_unsupported_subject_evidence"
    assert result.candidate_subjects == _candidates()


def test_high_confidence_with_fabricated_evidence_is_not_auto_merged() -> None:
    """A supporting_quote that isn't actually present in the item text at
    all -- fabricated outright, not merely company-only -- must be
    rejected the same way."""

    def fake_call(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(
            company="OpenAI",
            product="GPT-4o",
            supporting_quote="GPT-4o headlines this government partnership",
            confidence=0.9,
        )

    result = resolve_via_llm(
        _item("Expanding AI access and cyber defense for governments"),
        _candidates(),
        item_text="OpenAI and GSA will offer eligible governments expanded cyber defense support.",
        alias_table=[],
        call_fn=fake_call,
    )
    assert result.subject is None
    assert result.method == "llm_unsupported_subject_evidence"


def test_title_only_supporting_quote_is_accepted() -> None:
    """The prompt tells the model it may quote either item_title or
    item_body_excerpt (see prompts/resolve.txt) -- a genuine quote naming
    the product in the title alone, with no mention anywhere in the body,
    must be accepted. Validating against body text only would wrongly
    reject this."""

    def fake_call(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(
            company="OpenAI",
            product="GPT-4o",
            supporting_quote="GPT-4o gets a major update",
            confidence=0.9,
        )

    result = resolve_via_llm(
        _item("GPT-4o gets a major update"),
        _candidates(),
        item_text="The team shipped several improvements this quarter.",
        alias_table=[],
        call_fn=fake_call,
    )
    assert result.subject == Subject(company="OpenAI", product="GPT-4o")
    assert result.method == "llm_resolved"


def test_fabricated_quote_absent_from_both_title_and_body_is_rejected() -> None:
    """A quote that appears in neither the title nor the body -- not a
    boundary-crossing trick, just plainly not present anywhere -- must be
    rejected."""

    def fake_call(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(
            company="OpenAI",
            product="GPT-4o",
            supporting_quote="GPT-4o was mentioned nowhere in this item",
            confidence=0.9,
        )

    result = resolve_via_llm(
        _item("A totally unrelated headline"),
        _candidates(),
        item_text="Nothing about any tracked product here.",
        alias_table=[],
        call_fn=fake_call,
    )
    assert result.subject is None
    assert result.method == "llm_unsupported_subject_evidence"


def test_supporting_quote_cannot_cross_title_body_boundary() -> None:
    """Title and body are two separate fields, never contiguous prose --
    a quote must not be accepted merely because concatenating the tail of
    the title with the head of the body happens to spell it out. Title
    ends "...OpenAI's new", body starts "GPT-4o is here..."; naively
    space-joining them would make "new GPT-4o is here" a real substring,
    but the two fields were never actually written as one sentence."""

    def fake_call(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(
            company="OpenAI",
            product="GPT-4o",
            supporting_quote="new GPT-4o is here",
            confidence=0.9,
        )

    result = resolve_via_llm(
        _item("OpenAI's new"),
        _candidates(),
        item_text="GPT-4o is here to stay.",
        alias_table=[],
        call_fn=fake_call,
    )
    assert result.subject is None
    assert result.method == "llm_unsupported_subject_evidence"


def test_supporting_quote_past_the_body_excerpt_limit_is_rejected() -> None:
    """The prompt only ever shows the model item_text[:BODY_EXCERPT_CHARS]
    (see resolve_via_llm's item_body_excerpt render) -- evidence validation
    must be bounded to that exact same excerpt, not the full item_text.
    Otherwise a hallucinated supporting_quote could be "verified" merely
    by coincidentally matching real body text beyond character 500 that
    the model was never actually shown and could not have read. Padding
    puts the real "GPT-4o is here" phrase strictly after the cutoff; the
    fake call returns it verbatim (a real, non-fabricated phrase in the
    full body) but it must still be rejected because it falls outside the
    bounded evidence the model was given."""
    padding = "x" * BODY_EXCERPT_CHARS
    item_text = f"{padding} GPT-4o is here to stay."
    assert item_text[:BODY_EXCERPT_CHARS] == padding  # phrase starts strictly after the cutoff

    def fake_call(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(
            company="OpenAI",
            product="GPT-4o",
            supporting_quote="GPT-4o is here",
            confidence=0.9,
        )

    result = resolve_via_llm(
        _item("Unrelated headline with no product mention"),
        _candidates(),
        item_text=item_text,
        alias_table=[],
        call_fn=fake_call,
    )
    assert result.subject is None
    assert result.method == "llm_unsupported_subject_evidence"


def test_government_policy_item_rejects_gpt4o_high_confidence_resolution() -> None:
    """Regression test for the actual rehearsal failure, using the real
    item title and RSS description (fetched verbatim from
    https://openai.com/news/rss.xml during the follow-up review). A fake
    LLM response proposes the catalogue-valid OpenAI/GPT-4o at high
    confidence with a real, verbatim, but company-only supporting_quote
    -- exactly what happened in the rehearsal. This must be rejected with
    method="llm_unsupported_subject_evidence", not by simulating low
    confidence or returning no_match -- proving the grounding check itself
    is what stops the false merge, not some other guardrail."""
    title = (
        "Expanding AI access and cyber defense for federal, state, local, and tribal governments"
    )
    description = (
        "OpenAI and GSA will offer eligible federal, state, local, and tribal "
        "governments $0 license fees, 50% off usage, and expanded cyber defense "
        "support."
    )

    def fake_call(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(
            company="OpenAI",
            product="GPT-4o",
            supporting_quote=description,
            confidence=0.85,
        )

    result = resolve_via_llm(
        _item(title),
        _candidates(),
        item_text=f"{title} {description}",
        alias_table=[],
        call_fn=fake_call,
    )
    assert result.subject is None
    assert result.method == "llm_unsupported_subject_evidence"


def test_low_confidence_is_never_auto_merged() -> None:
    """Even if the model proposes a subject, a low confidence score must
    not resolve it — this is the guardrail against confident-sounding
    wrong merges."""

    def fake_call(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(company="OpenAI", product="GPT-4o", confidence=0.3)

    result = resolve_via_llm(_item(), _candidates(), call_fn=fake_call)
    assert result.subject is None
    assert result.method == "llm_low_confidence"


def test_high_confidence_subject_not_in_candidates_is_not_auto_merged() -> None:
    """Adversarial case per the review: high confidence alone must not be
    enough to accept a company/product the model was never actually given
    as a candidate -- a false merge to an invented subject is exactly the
    failure mode this project's "false merge worse than a miss" rule
    exists to prevent."""

    def fake_call(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(company="Mistral", product="Le Chat", confidence=0.95)

    result = resolve_via_llm(_item(), _candidates(), call_fn=fake_call)
    assert result.subject is None
    assert result.method == "llm_subject_not_in_candidates"
    assert result.candidate_subjects == _candidates()


def test_nan_confidence_is_rejected_at_parse_time_not_silently_accepted() -> None:
    """Same NaN-bypass case as test_extract_facts.py's -- confidence=NaN
    silently passed "< CONFIDENCE_THRESHOLD" here too before the
    Confidence type existed."""
    with pytest.raises(ValidationError):
        ResolveLLMResponse(company="OpenAI", product="GPT-4o", confidence=float("nan"))


def test_new_subject_proposal_with_no_existing_match() -> None:
    def fake_call(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(new_subject_proposal="Mistral: Le Chat", confidence=0.8)

    result = resolve_via_llm(_item(), _candidates(), call_fn=fake_call)
    assert result.subject is None
    assert result.method == "llm_new_subject_proposal"
    assert result.matched_text == "Mistral: Le Chat"
