"""Deterministic subject resolution — checked first, cheap, auditable.
Resolves a SourceItem to a Subject (company + product, see
shared/schemas.py) — the real contract has no "Entity", so this is what
"Classify relevance and entities" (docs/ARCHITECTURE.md's intelligence
workflow diagram) actually does.

Only the residue this can't confidently resolve (no match, or more than
one match) goes to resolve_llm.py. A false merge here is worse than a
miss, so matching requires a whole normalised alias/name to appear as a
phrase in the item text — not a loose substring check.
"""

from __future__ import annotations

import logging
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
    method: str  # "alias_match" | "no_match" | "ambiguous"
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
        for candidate in _candidate_strings(subject, alias_index):
            if _contains_phrase(haystack, candidate):
                matches.append((subject, candidate))
                break  # one match per subject is enough

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
        # More than one subject matched — ambiguous. A false merge is
        # worse than a miss, so this never auto-picks one; it goes to the
        # LLM residue with the specific candidates that matched.
        result = ResolutionResult(
            item_id=item.id,
            subject=None,
            method="ambiguous",
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
