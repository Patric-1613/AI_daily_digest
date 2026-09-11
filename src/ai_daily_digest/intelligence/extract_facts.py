"""Fact extraction — the "Extract typed facts with citations" step in
docs/ARCHITECTURE.md's intelligence workflow diagram. Turns one
DocumentSnapshot's text into zero or more ExtractedFact records against
the closed field list (shared/attributes.py). See
docs/LLM_AGENT_SPECS.md#extract_facts for the full contract.

Six guardrails enforced in code, not just requested in the prompt:
  1. quoted_span must actually appear in the snapshot text (grounding
     check) -- a model that paraphrases instead of quoting produces a
     fact that gets silently dropped, not silently stored. Applies
     regardless of disclosure_status -- an explicit non-disclosure
     statement needs a real citation exactly the same as a value does
     (ADR 0006).
  2. for a DISCLOSED candidate, value must actually be supported by
     quoted_span itself -- a real, grounded quote can still have an
     invented value attached to it (e.g. quoting a real sentence but
     reporting a different number than it states); check #1 alone can't
     catch that, see grounding.py. For a NOT_DISCLOSED candidate, quote
     existence (#1) alone is not semantic support either -- see #6 below,
     _quote_supports_non_disclosure's own real check for this case.
  3. field must be in the closed list -- an invented field is dropped.
  4. a quote shared across two different fields' candidates in the SAME
     extraction response is ambiguous evidence -- e.g. "Input costs 5
     and output costs 15" doesn't prove which number is input_price_usd
     and which is output_price_usd. Per review: this is CONTAINMENT, not
     a complete attribution fix -- see _cross_contaminated_indices'
     own docstring for its documented limits (it can't catch a mix-up
     when the sibling fact is omitted entirely, and can false-positive
     on coincidental shared digits). The real fix needs character-offset
     citations, not a re-quoted substring -- see
     docs/DESIGN_PROPOSAL_comparison_and_grounding.md point (e), not yet
     built. Until then, BOTH candidates sharing ambiguous evidence are
     dropped rather than guessing which one is right.
  5. disclosure_status="not_disclosed" and a real value are mutually
     exclusive, and disclosure_status="disclosed" (the default) requires
     a real value -- enforced at the model level on both FactCandidate
     here and ExtractedFact itself (shared/schemas.py), per ADR 0006, so
     a malformed candidate is rejected before it ever reaches the checks
     above, not silently coerced into one state or the other. `value`
     has no default on either model -- a candidate/fact that omits it
     entirely is rejected too, never silently treated as one state or
     the other.
  6. a NOT_DISCLOSED candidate's quote must actually SUPPORT a
     non-disclosure claim about THAT field, not merely exist in the text
     -- see _quote_supports_non_disclosure's own docstring: no real
     number anywhere in the quote; the SAME CLAUSE (split on sentence
     punctuation AND compound-sentence joins -- a comma+conjunction, a
     bare contrast conjunction, or a dash, not just a period) must carry
     both an approved withholding phrase and a concept pattern matching
     the candidate's own field; and that clause must not ALSO name a
     different field's concept family -- e.g. neither "The model
     features a large context window. Pricing details have not been
     released." (two sentences) nor "Benchmark scores are strong and
     pricing has not been announced" (one compound clause naming two
     unrelated concepts) may support a non-disclosure claim for the
     field whose concept happens to appear first. WITHIN the pricing
     family specifically, an input/output qualifier must also match the
     candidate's own price field -- "Output pricing has not been
     announced" must not support input_price_usd just because the two
     price fields share one family (see
     _pricing_qualifiers_support_field). Per review: quote existence
     alone conflates "this text is real" with "this text means what the
     candidate claims it means" -- the same gap check #2 already closes
     for a disclosed value's number, now closed for a non-disclosure
     claim too.

The accepted quoted_span and confidence are kept on the resulting
ExtractedFact (not discarded) so the evidence a fact was built from can
still be audited later, not just at extraction time -- see
docs/adr/0004-extracted-fact-keeps-evidence.md.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from pydantic import BaseModel, Field, field_validator, model_validator

from ai_daily_digest.intelligence.facts import normalise_name
from ai_daily_digest.intelligence.grounding import numbers_in, value_supported_by_quote
from ai_daily_digest.intelligence.llm import SONNET, call_structured
from ai_daily_digest.intelligence.prompt_templates import load_prompt, render
from ai_daily_digest.shared.attributes import COMPARABLE_FIELDS
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.schemas import (
    Confidence,
    DisclosureStatus,
    DocumentSnapshot,
    ExtractedFact,
    ExtractionMethod,
    Subject,
)

logger = logging.getLogger("intelligence.extract_facts")

CONFIDENCE_THRESHOLD = 0.6
PROMPT_VERSION = "extract-facts-v1"

# Explicit, reviewed closed set of context-window aliases justified by model output.
# Preserves closed-schema design: unrelated, near-miss, or novel fields remain unmapped
# and are rejected fail-closed.
_CANONICAL_FIELD_ALIASES: dict[str, str] = {
    "context_window": "context_window_tokens",
    "Context window": "context_window_tokens",
    "context window": "context_window_tokens",
    "context-window": "context_window_tokens",
}

# Approved explicit-withholding wording -- deliberately narrow (per
# review): vague absence ("we tested multiple models") must NOT match,
# only a phrase that actually says the fact is being withheld. Case-
# insensitive. Includes an optional "been" ("has not yet BEEN
# announced") -- without it, the pattern rejects its own worked examples
# ("pricing has not been announced"); verified against every
# acceptance-test phrase before relying on it. "available"/"public" are
# NOT bare verbs here (unlike an earlier revision) -- "is not available"
# alone is a plain service/feature-availability statement (e.g. "The
# model is not available in Europe"), not a non-disclosure claim; only
# counted when it follows one of a small set of nouns that actually name
# a withheld FACT ("pricing/details/... are not available").
_WITHHOLDING_PHRASE_RE = re.compile(
    r"\b(?:"
    r"not\s+(?:yet\s+)?(?:been\s+)?"
    r"(?:disclosed|announced|published|revealed|released|stated|detailed|shared|provided)"
    r"|withheld|unannounced|undisclosed|tbd|to\s+be\s+announced"
    r"|(?:details|information|pricing|scores|terms)\s+(?:are|is)\s+not\s+"
    r"(?:yet\s+)?(?:public|available)"
    r")\b",
    re.IGNORECASE,
)

# Which broader concept FAMILY each comparable field belongs to.
# input_price_usd/output_price_usd are deliberately the same family --
# "input"/"output" are optional qualifiers, never independent evidence
# of pricing on their own (per review) -- a quote needs an actual
# pricing concept (price/cost/rate/fee/... or "$") regardless of which
# of the two price fields the candidate names, and a clause naming both
# ("Input and output pricing have not been announced") must never be
# treated as cross-family-contaminated just because it mentions both.
# Within the pricing family specifically, input/output qualifiers DO
# still matter for a second, narrower check -- see
# _pricing_qualifiers_support_field below -- to stop an input-only
# non-disclosure quote ("Output pricing has not been announced")
# supporting the WRONG price field.
_FIELD_TO_FAMILY: dict[str, str] = {
    "input_price_usd": "pricing",
    "output_price_usd": "pricing",
    "context_window_tokens": "context_window",
    "benchmark_scores": "benchmarks",
    "availability_regions": "regions",
    "licence_terms": "licence",
    "modalities": "modalities",
}

# One concept pattern per family -- catches a real withholding statement
# about one field being misattributed to another (e.g. a pricing
# non-disclosure statement reported against context_window_tokens).
# Whole-word/whole-phrase boundaries throughout: a loose substring match
# would let e.g. "rate" match inside "corporate", a single word like
# "limit" or "token" stand in for the whole context_window concept (a
# "rate limit" is not a context window), or "terms" alone match an
# unrelated "payment terms" sentence for licence. Matched against the
# raw quote (case-insensitive), not normalise_name()'d, so a symbol like
# "$" survives -- normalise_name() strips punctuation entirely; "$" has
# no natural word boundary of its own, so it's its own alternative
# rather than wrapped in `\b`.
_FAMILY_CONCEPT_PATTERNS: dict[str, re.Pattern[str]] = {
    "pricing": re.compile(
        r"\b(?:price|pricing|cost|costs|rate|rates|fee|fees|dollar|dollars|cent|cents"
        r"|currency|currencies|usd)\b|\$",
        re.IGNORECASE,
    ),
    # Never "limit" or "token" alone -- "Rate limits have not been
    # announced" must not read as a context-window non-disclosure.
    "context_window": re.compile(
        r"\b(?:context\s+(?:window|length|size|limit|capacity)"
        r"|token\s+(?:window|limit|capacity)"
        r"|(?:max|maximum)\s+context)\b",
        re.IGNORECASE,
    ),
    # Never "result" or "score" alone.
    "benchmarks": re.compile(
        r"\b(?:benchmark(?:s)?(?:\s+(?:score|scores|result|results|eval|evals"
        r"|evaluation|evaluations))?)\b",
        re.IGNORECASE,
    ),
    # Never bare "available"/"availability" alone.
    "regions": re.compile(
        r"\b(?:region|regions|country|countries|geographic\s+availability"
        r"|geographies|geography|regional\s+availability)\b",
        re.IGNORECASE,
    ),
    # Never "terms" alone -- avoids matching an unrelated "payment terms".
    "licence": re.compile(r"\b(?:licen[sc]e|licen[sc]ing)(?:\s+terms)?\b", re.IGNORECASE),
    "modalities": re.compile(
        r"\b(?:modalit(?:y|ies)|multimodal|input\s+(?:types?|formats?)"
        r"|output\s+(?:types?|formats?))\b",
        re.IGNORECASE,
    ),
}

# Splits a quote into clause/sentence segments -- see
# _quote_supports_non_disclosure's own docstring for why a single
# combined check across the whole quote isn't enough. Per review: plain
# sentence punctuation (`. ! ? ; \n`) alone missed a COMPOUND sentence
# joining two unrelated clauses with a comma+conjunction, a bare
# contrast conjunction, or a dash -- "The model has a large context
# window, but pricing details have not been released" is one sentence,
# no terminal punctuation between its two halves, yet they're about
# different facts. Also splits on "--"/"—" (an em dash, not a hyphen --
# a single ASCII "-" as in "GPT-4o" does NOT match either alternative,
# verified deliberately so a compound product name is never mistaken
# for a clause boundary).
_CLAUSE_SPLIT_RE = re.compile(
    r"[.!?;\n]+"
    r"|,\s*(?:but|and|while|whereas|although|however|yet)\b"
    r"|\b(?:but|while|whereas|although|however)\b"
    r"|--|—",
    re.IGNORECASE,
)

# Directional qualifiers within the pricing family -- a clause already
# confirmed to be a pricing-family non-disclosure statement (per
# _quote_supports_non_disclosure's main check) can still name the WRONG
# price field: "Output pricing has not been announced" is real pricing
# non-disclosure, but it says nothing about input_price_usd. Restricted
# to the whole words "input"/"output" themselves -- per review, broader
# synonyms (prompt/ingress for input; completion/generation/egress for
# output) caused false co-occurrences: "image generation" or "prompt
# caching" are common qualifiers on an OUTPUT-pricing statement's own
# clause that have nothing to do with which price direction is meant
# ("Output pricing for prompt caching has not been announced" is about
# output pricing, but the old pattern would have read "prompt" in it as
# an INPUT qualifier too, making has_input and has_output both true and
# silently letting it pass for input_price_usd as well).
_INPUT_QUALIFIER_RE = re.compile(r"\binput\b", re.IGNORECASE)
_OUTPUT_QUALIFIER_RE = re.compile(r"\boutput\b", re.IGNORECASE)


def _pricing_qualifiers_support_field(field: str, clause: str) -> bool:
    """Within the pricing family, ensure directional qualifiers match
    the field:
      - Input-only wording ("Input pricing...") supports ONLY
        input_price_usd.
      - Output-only wording ("Output pricing...") supports ONLY
        output_price_usd.
      - Both qualifiers present ("Input and output pricing...") support
        BOTH input_price_usd and output_price_usd -- the same siblings-
        share-a-family reasoning _FIELD_TO_FAMILY's own comment
        describes for the family-match check itself.
      - General, unqualified pricing wording ("Pricing has not been
        announced", no "input"/"output" at all) supports BOTH -- a
        candidate is not required to specify a qualifier that the
        source text itself never mentioned."""
    has_input = bool(_INPUT_QUALIFIER_RE.search(clause))
    has_output = bool(_OUTPUT_QUALIFIER_RE.search(clause))
    if (has_input and has_output) or (not has_input and not has_output):
        return True
    if has_input:
        return field == "input_price_usd"
    if has_output:
        return field == "output_price_usd"
    return True


class FactCandidate(BaseModel):
    """disclosure_status/value: ADR 0006's "unknown" vs. "not disclosed"
    distinction, mirrored from ExtractedFact (shared/schemas.py) here so
    a malformed candidate (e.g. not_disclosed with a value attached)
    fails call_structured's own validate -> retry-once -> fail-loudly
    loop, the same protection FactCandidate.confidence already gets from
    the shared Confidence type -- not just caught later in
    extract_facts()'s own post-processing. `value` has no default, same
    reasoning as ExtractedFact's own field -- the model's structured
    response must explicitly say `null` for a not_disclosed candidate,
    never silently omit the key."""

    field: str = Field(
        description=(
            "Exact canonical snake_case field key from the closed list: "
            "'context_window_tokens', 'input_price_usd', 'output_price_usd', "
            "'benchmark_scores', 'availability_regions', 'licence_terms', 'modalities'."
        )
    )
    value: str | None
    disclosure_status: DisclosureStatus = DisclosureStatus.DISCLOSED
    quoted_span: str
    # Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    # -- rejects confidence=NaN at parse time (see shared/schemas.py's
    # comment): NaN silently passed every "< CONFIDENCE_THRESHOLD" check
    # below before this was added, since every comparison with NaN is
    # False. A malformed response now fails call_structured's own
    # validation instead, triggering its retry-once-then-fail-loudly path.
    confidence: Confidence

    @field_validator("field", mode="before")
    @classmethod
    def _normalize_field(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        return _CANONICAL_FIELD_ALIASES.get(v.strip(), v)

    @model_validator(mode="after")
    def _require_consistent_disclosure_state(self) -> FactCandidate:
        """Same contradiction ExtractedFact's own validator rejects
        (shared/schemas.py) -- catching it here too, on the raw model
        response, means a malformed candidate triggers call_structured's
        retry-with-the-validation-error loop instead of silently
        reaching this module's post-processing only to be dropped
        without the model ever getting a chance to correct itself."""
        if self.disclosure_status == DisclosureStatus.NOT_DISCLOSED and self.value is not None:
            raise ValueError(
                "a candidate with disclosure_status='not_disclosed' must not also report a value"
            )
        if self.disclosure_status == DisclosureStatus.DISCLOSED and not self.value:
            raise ValueError("a candidate with disclosure_status='disclosed' must report a value")
        return self


class FactExtractionResponse(BaseModel):
    facts: list[FactCandidate] = Field(default_factory=list)


def _format_fields() -> str:
    return "\n".join(f"- {key}: {label}" for key, label in COMPARABLE_FIELDS.items())


def _cross_contaminated_indices(candidates: list[FactCandidate]) -> set[int]:
    """Indices of candidates whose quoted_span also contains a DIFFERENT
    field's candidate's value, from the same extraction response --
    exactly the "Input costs 5 and output costs 15" case reproduced in
    review: input_price_usd=5's quote also contains "15"
    (output_price_usd's own value), so we can't be confident "5" is
    really input's and not a misattribution. Deliberately narrower than
    "quote contains more than one number" -- that would also flag the
    legitimate, already-tested "increased from 128,000 to 256,000
    tokens" pattern (two numbers, one field, no sibling candidate
    involved), which this does not.

    KNOWN LIMITS (documented per review, not silently assumed complete
    -- this is containment, not a full attribution fix; see
    docs/DESIGN_PROPOSAL_comparison_and_grounding.md point (e) for the
    real fix, character-offset citations, not yet built):
      1. Only catches contamination when BOTH the correct and the
         confused candidate appear in the SAME extraction response. If
         the model returns the wrong number for a field and simply
         omits the sibling fact that would have exposed the mix-up
         (e.g. only reports input_price_usd=15, never mentions
         output_price_usd at all), there is no sibling value to compare
         against and this guard cannot catch it.
      2. Two candidates whose values coincidentally share a digit
         sequence for unrelated reasons (not an actual mix-up) will
         still be flagged and both dropped -- a safe direction to be
         wrong in (an extra fact goes to nothing rather than a wrong
         fact goes to press), but a real, accepted false-positive rate,
         not zero-cost caution."""
    ambiguous: set[int] = set()
    for i, candidate in enumerate(candidates):
        quote_numbers = numbers_in(candidate.quoted_span)
        for other in candidates:
            if other.field == candidate.field:
                continue
            # A not_disclosed candidate has no value to be confused with
            # -- nothing to compare against, so it can never make ANOTHER
            # candidate ambiguous (though it can still itself be flagged
            # by a sibling's real value appearing in its own quote, via
            # the outer loop above).
            if other.value is None:
                continue
            other_value_numbers = numbers_in(other.value)
            if other_value_numbers and other_value_numbers <= quote_numbers:
                ambiguous.add(i)
                break
    return ambiguous


def _conflicting_same_field_indices(candidates: list[FactCandidate]) -> set[int]:
    """Indices of candidates that contradict another candidate for the SAME
    canonical field in the same extraction response (e.g. one candidate claims
    value '100000' and another claims '200000', or disclosed vs not_disclosed).
    Fails closed by dropping all contradictory candidates for that field."""
    conflicting: set[int] = set()
    for i, a in enumerate(candidates):
        for j, b in enumerate(candidates):
            if i >= j:
                continue
            if a.field == b.field and (
                a.value != b.value or a.disclosure_status != b.disclosure_status
            ):
                conflicting.add(i)
                conflicting.add(j)
    return conflicting


def _quote_supports_non_disclosure(field: str, quote: str) -> bool:
    """Deterministic semantic-support check for a not_disclosed
    candidate, per review: the quote actually appearing in the snapshot
    text (checked separately -- the same grounding check every candidate
    goes through) proves the quote is real, not that it SUPPORTS "this
    specific field is being withheld". Mirrors what
    grounding.py::value_supported_by_quote() does for a disclosed
    value's number -- a real, grounded quote can still be attached to
    the wrong claim.

    1. The quote must not contain a real number anywhere -- a quote that
       states an actual value ("$5 per million tokens") is a disclosed
       fact mislabeled not_disclosed, not a genuine non-disclosure;
       reject rather than guess which label is right.
    2. At least one CLAUSE of the quote (split on sentence punctuation
       AND compound-sentence joins -- see _CLAUSE_SPLIT_RE's own comment
       for why a period alone isn't enough) must contain an approved
       explicit-withholding phrase (_WITHHOLDING_PHRASE_RE), AND that
       same clause's set of matching concept FAMILIES
       (_FAMILY_CONCEPT_PATTERNS) must be EXACTLY the candidate's own
       family -- not a superset, not a different one, not empty.
       Checking the field concept against the whole quote independently
       of the withholding phrase (rather than the same clause) would
       accept e.g. "The model features a large context window. Pricing
       details have not been released." for context_window_tokens -- the
       withholding phrase and the field concept are both present
       SOMEWHERE in the quote, but not about the same fact; that must be
       rejected. Requiring the matching-family set to be exactly one
       family (not just "contains the candidate's own") is what rejects
       e.g. "Benchmark scores are strong and pricing has not been
       announced" for benchmark_scores -- that clause's own concept
       AND a withholding phrase are both present, but the SAME clause
       ALSO names pricing, so which fact is actually being withheld is
       ambiguous; it must not count as support for either. A field with
       no registered family fails closed too. Same-family siblings
       (input_price_usd/output_price_usd) never trigger this against
       each other -- they map to the one "pricing" family, so a clause
       naming both still has a matching-family set of exactly
       `{"pricing"}`.
    3. WITHIN the pricing family specifically, a clause that names the
       WRONG price field is still rejected --
       _pricing_qualifiers_support_field's own docstring for the exact
       rule -- "Output pricing has not been announced" is real pricing
       non-disclosure, but must not support input_price_usd just
       because both fields share one family and one concept pattern."""
    if numbers_in(quote):
        return False
    target_family = _FIELD_TO_FAMILY.get(field)
    if target_family is None:
        return False
    for clause in _CLAUSE_SPLIT_RE.split(quote):
        if not _WITHHOLDING_PHRASE_RE.search(clause):
            continue
        matching_families = {
            family for family, pattern in _FAMILY_CONCEPT_PATTERNS.items() if pattern.search(clause)
        }
        if matching_families == {target_family}:
            if target_family == "pricing" and not _pricing_qualifiers_support_field(field, clause):
                continue
            return True
    return False


def _default_call(system: str, prompt: str) -> FactExtractionResponse:
    return call_structured(
        model=SONNET,
        system=system,
        prompt=prompt,
        response_model=FactExtractionResponse,
    )


def _render_extract_prompt(subject: Subject, snapshot: DocumentSnapshot) -> tuple[str, str]:
    system, user_template = load_prompt("extract_facts")
    prompt = render(
        user_template,
        subject_company=subject.company,
        subject_product=subject.product,
        snapshot_id=str(snapshot.id),
        comparable_fields=_format_fields(),
        snapshot_text=snapshot.content_text or "",
    )
    return system, prompt


def extract_facts(
    subject: Subject,
    snapshot: DocumentSnapshot,
    *,
    call_fn: Callable[[str, str], FactExtractionResponse] | None = None,
) -> list[ExtractedFact]:
    """call_fn is injectable for testing — defaults to the real Anthropic
    call via intelligence/llm.py."""
    response = (call_fn or _default_call)(*_render_extract_prompt(subject, snapshot))

    haystack = normalise_name(snapshot.content_text or "")
    ambiguous_indices = _cross_contaminated_indices(response.facts)
    conflicting_indices = _conflicting_same_field_indices(response.facts)
    seen_facts: set[tuple[str, str | None, DisclosureStatus]] = set()
    facts: list[ExtractedFact] = []
    for i, candidate in enumerate(response.facts):
        if i in conflicting_indices:
            logger.warning(
                "extraction_rejected reason=conflicting_same_field snapshot_id=%s "
                "field=%s value=%r",
                snapshot.id,
                candidate.field,
                candidate.value,
            )
            continue
        if i in ambiguous_indices:
            logger.warning(
                "extraction_rejected reason=ambiguous_multi_field_quote snapshot_id=%s "
                "field=%s value=%r quoted_span=%r",
                snapshot.id,
                candidate.field,
                candidate.value,
                candidate.quoted_span,
            )
            continue
        if candidate.field not in COMPARABLE_FIELDS:
            logger.warning(
                "extraction_rejected reason=unknown_field snapshot_id=%s field=%s",
                snapshot.id,
                candidate.field,
            )
            continue
        if candidate.confidence < CONFIDENCE_THRESHOLD:
            logger.warning(
                "extraction_rejected reason=low_confidence snapshot_id=%s field=%s confidence=%s",
                snapshot.id,
                candidate.field,
                candidate.confidence,
            )
            continue
        # normalise_name("") == "" and "" is a substring of everything,
        # so an empty/punctuation-only quoted_span would otherwise sail
        # straight through this check -- reject it explicitly rather than
        # relying on the substring test alone.
        normalised_span = normalise_name(candidate.quoted_span)
        if not normalised_span or normalised_span not in haystack:
            logger.warning(
                "extraction_rejected reason=ungrounded_span snapshot_id=%s field=%s quoted_span=%r",
                snapshot.id,
                candidate.field,
                candidate.quoted_span,
            )
            continue
        if candidate.value is not None:
            # The quote itself is real (checked above), but that doesn't
            # mean the *value* the model reported actually came from it
            # -- a model can quote a real sentence and still attach a
            # fabricated number to it. This is the check that catches
            # that specific case.
            if not value_supported_by_quote(candidate.value, candidate.quoted_span):
                logger.warning(
                    "extraction_rejected reason=value_not_in_quote snapshot_id=%s field=%s "
                    "value=%r quoted_span=%r",
                    snapshot.id,
                    candidate.field,
                    candidate.value,
                    candidate.quoted_span,
                )
                continue
        else:
            # not_disclosed candidate -- no value to check support for,
            # but per review, quote EXISTENCE (checked above) is not the
            # same as quote SUPPORT for a non-disclosure claim about
            # THIS field: it must actually say so explicitly, about the
            # right field, and not itself contain a real value. See
            # _quote_supports_non_disclosure's own docstring.
            if not _quote_supports_non_disclosure(candidate.field, candidate.quoted_span):
                logger.warning(
                    "extraction_rejected reason=non_disclosure_not_supported "
                    "snapshot_id=%s field=%s quoted_span=%r",
                    snapshot.id,
                    candidate.field,
                    candidate.quoted_span,
                )
                continue

        fact_signature = (candidate.field, candidate.value, candidate.disclosure_status)
        if fact_signature in seen_facts:
            logger.info(
                "extraction_duplicate_deduplicated snapshot_id=%s field=%s value=%r",
                snapshot.id,
                candidate.field,
                candidate.value,
            )
            continue
        seen_facts.add(fact_signature)

        facts.append(
            ExtractedFact(
                id=new_id(),
                snapshot_id=snapshot.id,
                field=candidate.field,
                value=candidate.value,
                disclosure_status=candidate.disclosure_status,
                extraction_method=ExtractionMethod.LLM_STRUCTURED_OUTPUT,
                extraction_model=SONNET,
                prompt_version=PROMPT_VERSION,
                quoted_span=candidate.quoted_span,
                confidence=candidate.confidence,
            )
        )
        logger.info(
            "extraction_accepted snapshot_id=%s field=%s disclosure_status=%s confidence=%s",
            snapshot.id,
            candidate.field,
            candidate.disclosure_status,
            candidate.confidence,
        )

    return facts
