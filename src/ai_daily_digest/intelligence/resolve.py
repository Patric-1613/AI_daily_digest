"""Deterministic subject resolution — checked first, cheap, auditable.
Resolves a SourceItem to a Subject (company + product, see
shared/schemas.py) — the real contract has no "Entity", so this is what
"Classify relevance and entities" (docs/ARCHITECTURE.md's intelligence
workflow diagram) actually does.

Only true residue (no phrase match at all) goes to resolve_llm.py. A
false merge here is worse than a miss, so matching requires a whole
normalised alias/name to appear as a phrase in the item text — not a
loose substring check. When the item text instead names *two or more*
already-tracked subjects by phrase (e.g. an article covering both Codex
and ChatGPT), that is never sent to the LLM to adjudicate either —
picking one would be exactly the kind of confidence-scored false merge
this project treats as worse than a miss. See method="ambiguous_multi_subject".

A bare product name is sometimes also an ordinary word (e.g. "Codex" is
also a bound manuscript) that can appear in text with nothing to do with
the tracked product at all. Matching on the bare product name alone
additionally requires something else to corroborate the subject's
company — its publisher, or the company's own name appearing anywhere in
the text (see _bare_match_corroborated()) — a company+product phrase or a
reviewed alias needs no such corroboration, since either already names
the company (or was human-reviewed) directly.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ai_daily_digest.intelligence.facts import normalise_name
from ai_daily_digest.shared.schemas import SourceItem, Subject

logger = logging.getLogger("intelligence.resolve")

ALIASES_PATH = Path(__file__).resolve().parents[1] / "shared" / "aliases.yaml"

# Trusted product-specific feeds that uniquely identify a single product by definition.
# Checked before text matching to resolve version-only releases without mixing generic
# source/publisher metadata into the alias text haystack.
TRUSTED_SOURCE_SUBJECTS: dict[str, Subject] = {
    "langchain_pypi": Subject(company="LangChain", product="LangChain"),
    "langgraph_pypi": Subject(company="LangChain", product="LangGraph"),
}


@dataclass
class SubjectAlias:
    subject: Subject
    aliases: list[str]  # normalised


@dataclass
class ResolutionResult:
    item_id: uuid.UUID
    subject: Subject | None
    # "alias_match" | "no_match" | "ambiguous_multi_subject" |
    # "llm_resolved" | "llm_low_confidence" | "llm_new_subject_proposal" |
    # "llm_subject_not_in_candidates" | "llm_unsupported_subject_evidence"
    # (the last five are set by resolve_llm.py, not this module)
    method: str
    confidence: float
    matched_text: str | None = None
    candidate_subjects: list[Subject] = field(default_factory=list)


def load_alias_table(path: Path = ALIASES_PATH) -> list[SubjectAlias]:
    """Missing file -> empty table, not an error: a fresh checkout before
    the team has populated shared/aliases.yaml should still run, just
    with deterministic matching finding fewer subjects than it eventually
    will."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    table: list[SubjectAlias] = []
    for entry in data.get("subjects") or []:
        subject = Subject(company=entry["company"], product=entry["product"])
        table.append(
            SubjectAlias(
                subject=subject, aliases=[normalise_name(a) for a in entry.get("aliases", [])]
            )
        )
    return table


def _index_alias_table(alias_table: list[SubjectAlias]) -> dict[Subject, list[str]]:
    """Group aliases by subject once per resolve_deterministic() call,
    rather than the alias table being linearly re-scanned inside
    _candidate_strings() for every subject — the difference between one
    O(n) pass and an O(subjects * aliases) one as the alias table grows."""
    index: dict[Subject, list[str]] = {}
    for entry in alias_table:
        index.setdefault(entry.subject, []).extend(entry.aliases)
    return index


def _candidate_strings(subject: Subject, alias_index: dict[Subject, list[str]]) -> set[str]:
    """The full candidate phrase set for `subject`: its bare product name,
    the "company product" phrase, and any reviewed aliases. Used by
    quoted_span_supports_subject() (an already-verified, verbatim quote is
    its own strong evidence). resolve_deterministic()'s own matching loop
    below deliberately does NOT use this directly -- see
    _company_candidate_strings()/_bare_product_name() and the provenance
    gate for why the bare product name needs an extra check there."""
    strings = {
        normalise_name(subject.product),
        normalise_name(f"{subject.company} {subject.product}"),
    }
    strings |= set(alias_index.get(subject, ()))
    # Drop only single-character candidates (a bare "x" is unsafe to
    # match on) -- 2 characters is deliberately allowed through: real
    # product names are this short (OpenAI's "o1"/"o3"/"o4-mini" family),
    # and _contains_phrase() still requires it as a whole, space-bounded
    # token, not a loose substring, so "o1" won't match inside "o100" etc.
    return {s for s in strings if len(s) >= 2}


def _company_candidate_strings(subject: Subject, alias_index: dict[Subject, list[str]]) -> set[str]:
    """Candidate phrases strong enough for resolve_deterministic() to
    accept on their own, with no further check: the "company product"
    phrase (it already names the company inline) and reviewed aliases
    (human-vetted -- see aliases.yaml's own docstring). Deliberately
    excludes the bare product name alone -- see _bare_product_name() and
    the provenance gate in resolve_deterministic()."""
    strings = {normalise_name(f"{subject.company} {subject.product}")}
    strings |= set(alias_index.get(subject, ()))
    return {s for s in strings if len(s) >= 2}


def _bare_product_name(subject: Subject) -> str | None:
    """The product name alone, normalised -- deliberately the *weakest*
    candidate phrase. An ordinary word that happens to equal a tracked
    product name (e.g. "Codex" is also an ordinary English noun for a
    bound manuscript) can appear throughout completely unrelated text
    published by someone with no connection to the company at all.
    resolve_deterministic() requires a match on this one specifically --
    never the stronger phrases in _company_candidate_strings() -- to be
    corroborated separately, via _bare_match_corroborated(). Returns None
    for a product name too short to safely match at all (see
    _candidate_strings()'s comment)."""
    name = normalise_name(subject.product)
    return name if len(name) >= 2 else None


def _bare_match_corroborated(item: SourceItem, subject: Subject, haystack_normalised: str) -> bool:
    """Whether something *other than the bare product-name match itself*
    corroborates that this item is really about `subject`'s company --
    required before resolve_deterministic() accepts a match on
    _bare_product_name() alone (see its docstring for why). Either of two
    signals is enough:

    1. The item's own publisher IS that company -- an official channel
       doesn't need to repeat its own name (e.g. OpenAI's own news feed
       covering Codex, ChatGPT, or GPT-Live-1 rarely says "OpenAI" again
       in the headline or excerpt).
    2. The company's name appears anywhere in the item text, even if not
       adjacent to the product name -- third-party/editorial coverage
       that explicitly names the company (e.g. "OpenAI boosts GPT-4o's
       context window", where "OpenAI" and "GPT-4o" aren't a contiguous
       phrase but the article is still clearly about that company).

    Deliberately reuses SourceItem.publisher and the same haystack/phrase
    check as everywhere else in this module, rather than a new lookup
    table or per-product keyword list -- general and uniform across every
    subject's bare product name, not special-cased to any one product. An
    article naming neither the publisher nor the company anywhere (e.g. a
    museum newsletter using "codex" in its ordinary sense) satisfies
    neither and fails closed."""
    if normalise_name(item.publisher) == normalise_name(subject.company):
        return True
    company = normalise_name(subject.company)
    return len(company) >= 2 and _contains_phrase(haystack_normalised, company)


def _contains_phrase(haystack_normalised: str, phrase_normalised: str) -> bool:
    return f" {phrase_normalised} " in f" {haystack_normalised} "


def resolve_deterministic(
    item: SourceItem,
    known_subjects: list[Subject],
    alias_table: list[SubjectAlias] | None = None,
    *,
    item_text: str = "",
) -> ResolutionResult:
    """item_text is the item's title plus its snapshot's content_text —
    SourceItem itself carries no body (see shared/schemas.py), so callers
    must pass the relevant DocumentSnapshot's text explicitly."""
    # Product-specific trusted sources resolve directly by source_id to prevent
    # missing version-only releases while keeping publisher/source metadata out
    # of general alias text matching (preventing false merges).
    if item.source_id in TRUSTED_SOURCE_SUBJECTS:
        subject = TRUSTED_SOURCE_SUBJECTS[item.source_id]
        result = ResolutionResult(
            item_id=item.id,
            subject=subject,
            method="alias_match",
            confidence=0.95,
            matched_text=item.source_id,
        )
        logger.info(
            "resolution item_id=%s subject=%s method=%s confidence=%s",
            result.item_id,
            result.subject,
            result.method,
            result.confidence,
        )
        return result

    alias_table = alias_table if alias_table is not None else load_alias_table()
    alias_index = _index_alias_table(alias_table)
    # dict.fromkeys dedupes while preserving order (known subjects first,
    # then any alias-table-only subjects) in one O(n) pass, rather than
    # an `in` check against a growing list for every alias-table entry.
    all_subjects = list(dict.fromkeys([*known_subjects, *alias_index.keys()]))
    haystack = normalise_name(f"{item.title} {item_text}")

    matches: list[tuple[Subject, str]] = []
    for subject in all_subjects:
        matched_text = next(
            (
                candidate
                for candidate in _company_candidate_strings(subject, alias_index)
                if _contains_phrase(haystack, candidate)
            ),
            None,
        )
        if matched_text is None:
            # Fall back to the bare product name only with corroboration
            # from something other than the match itself -- see
            # _bare_product_name()'s and _bare_match_corroborated()'s
            # docstrings for why this one candidate needs an extra check
            # the stronger ones above don't.
            bare = _bare_product_name(subject)
            if (
                bare is not None
                and _contains_phrase(haystack, bare)
                and _bare_match_corroborated(item, subject, haystack)
            ):
                matched_text = bare
        if matched_text is not None:
            matches.append((subject, matched_text))

    if len(matches) == 1:
        subject, matched_text = matches[0]
        result = ResolutionResult(
            item_id=item.id,
            subject=subject,
            method="alias_match",
            confidence=0.95,
            matched_text=matched_text,
        )
    elif len(matches) == 0:
        result = ResolutionResult(
            item_id=item.id,
            subject=None,
            method="no_match",
            confidence=0.0,
            candidate_subjects=all_subjects,
        )
    else:
        # More than one already-tracked subject matched by explicit
        # phrase — e.g. an item naming both Codex and ChatGPT. A false
        # merge is worse than a miss, so this never auto-picks one *and*
        # is never handed to the LLM to adjudicate (the schema only
        # supports one Subject per item, so there is nothing safe for an
        # LLM guess to do here but silently collapse two real subjects
        # into one). Terminal: candidates are preserved on the result for
        # a human to review; see resolve.py's module docstring.
        result = ResolutionResult(
            item_id=item.id,
            subject=None,
            method="ambiguous_multi_subject",
            confidence=0.0,
            candidate_subjects=[s for s, _ in matches],
        )

    logger.info(
        "resolution item_id=%s subject=%s method=%s confidence=%s",
        result.item_id,
        result.subject,
        result.method,
        result.confidence,
    )
    return result


def quoted_span_supports_subject(
    subject: Subject,
    quoted_span: str | None,
    item_text: str,
    alias_table: list[SubjectAlias],
) -> bool:
    """Whether `quoted_span` is real, verbatim evidence for `subject`.

    Two independent conditions, both required:

    1. `quoted_span` is a verbatim (non-normalised) substring of
       `item_text` -- so a model can't invent supporting text that was
       never actually in the item.
    2. After normalisation, `quoted_span` itself contains one of
       `subject`'s candidate phrases (its canonical product name,
       "company product", or a reviewed alias) as a whole, space-bounded
       phrase -- the same rule resolve_deterministic() already applies to
       the full item text, just applied to the narrower quoted span.

    A quote that's real but only names the company ("OpenAI announced...")
    fails condition 2, because none of `_candidate_strings()`'s phrases is
    the bare company name -- this is a general grounding rule, not a
    denylist of "policy"/"partnership"/etc. keywords.

    Condition 1 requires a word-boundary-respecting match, not a bare
    Python `in` substring check -- otherwise a short quote like "GPT-4o"
    could technically match inside an unrelated longer word (e.g.
    "GPT-4oXtra") that only happens to start with the same characters,
    letting normalising the *isolated* quote manufacture a phrase match
    that was never actually present as a real word in the source text.

    Used by resolve_llm.py so an existing-subject match is never accepted
    on the model's self-reported confidence and candidate-list membership
    alone (see intelligence/CLAUDE.md's false-merge rule)."""
    if not quoted_span:
        return False
    if not re.search(rf"(?<!\w){re.escape(quoted_span)}(?!\w)", item_text):
        return False
    alias_index = _index_alias_table(alias_table)
    candidates = _candidate_strings(subject, alias_index)
    haystack = normalise_name(quoted_span)
    return any(_contains_phrase(haystack, candidate) for candidate in candidates)
