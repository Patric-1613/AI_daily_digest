"""The closed set of comparable fact fields (docs/API_CONTRACT.md's
`field` on ExtractedFact/Change). Closed on purpose: an open set can't be
compared. Field names follow the contract's own example
("context_window_tokens") — unit-suffixed where the unit isn't obvious.

Still Draft v0.1 alongside the rest of the contract — extend only by team
agreement (shared/, same CODEOWNERS sign-off rule as schemas.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

COMPARABLE_FIELDS: dict[str, str] = {
    # field: human-readable label, used in prompts and rendered UI/email.
    "context_window_tokens": "Context window",
    "input_price_usd": "Input price (USD)",
    "output_price_usd": "Output price (USD)",
    "benchmark_scores": "Named benchmark scores",
    "availability_regions": "Availability regions",
    "licence_terms": "Licence terms",
    "modalities": "Modalities",
}


def field_label(field: str) -> str:
    """The one curated label for a field, used everywhere a field name
    reaches rendered text -- extract_facts.py's prompts,
    draft_claims.py's single-subject claims, compare_subjects.py's
    cross-subject claims -- so a field reads identically no matter which
    code path produced the sentence. Falls back to the raw field key
    (with underscores turned to spaces) only for a field COMPARABLE_FIELDS
    somehow doesn't know about. Lowercased for mid-sentence use ("Context
    window" -> "context window")."""
    label = COMPARABLE_FIELDS.get(field, field.replace("_", " "))
    return label[:1].lower() + label[1:] if label else label


class ComparisonRule(Protocol):
    """What a field needs to support a deterministic cross-subject
    comparison: turn its stored string value into something comparable,
    and say which side is bigger. Deliberately minimal -- see ADR 0005's
    point (f): different fields need genuinely different comparison
    semantics (currency/unit/basis for prices, benchmark name/conditions
    for scores, set comparison for regions/modalities), so only fields
    with an actual ComparisonRule registered in COMPARISON_RULES are
    eligible for comparison at all. A field with no rule is excluded,
    not given a default/guessed one."""

    def parse(self, value: str) -> object:
        """Raises ValueError (or TypeError) for a malformed stored
        value -- callers must treat that as "drop this one candidate",
        never let it abort a whole batch (see ADR 0005's implementation
        issue)."""

    def relation(self, parsed_a: object, parsed_b: object) -> str:
        """One of "lower", "higher", "equal" -- how parsed_a compares to
        parsed_b."""


@dataclass(frozen=True)
class IntegerComparisonRule:
    """Phase 1 of ADR 0005: a field whose value is already an
    unambiguous bare integer string (context_window_tokens).
    Benchmarks/regions/modalities each still need their own
    representation designed first (basis/conditions for benchmark
    scores, set semantics for regions/modalities) -- see ADR 0005 point
    (f) -- so they have no rule here and stay excluded from comparison,
    not guessed at with this one. Prices moved to their own
    PriceComparisonRule below in Phase 2."""

    unit: str

    def parse(self, value: str) -> int:
        return int(value)

    def relation(self, parsed_a: object, parsed_b: object) -> str:
        # Signature matches the ComparisonRule Protocol exactly (object,
        # not int) -- a narrower parameter type here would make this
        # class structurally incompatible with the Protocol under mypy's
        # contravariance check for dict[str, ComparisonRule]. `assert`
        # would narrow the type too, but strips under -O (bandit B101) --
        # an explicit raise doesn't, and still fails loudly if this is
        # ever reached with something this class's own parse() didn't
        # produce (see compare_subjects.py's parse-then-relation
        # pairing).
        if not isinstance(parsed_a, int) or not isinstance(parsed_b, int):
            raise TypeError(f"relation() expected two ints, got {parsed_a!r} and {parsed_b!r}")
        if parsed_a < parsed_b:
            return "lower"
        if parsed_a > parsed_b:
            return "higher"
        return "equal"


@dataclass(frozen=True)
class PriceComparisonRule:
    """Phase 2 of ADR 0005: input_price_usd/output_price_usd's own
    representation -- a plain USD-per-unit numeric string, with an
    optional leading "$" and thousands separators tolerated (the same
    formatting-tolerance grounding.py's numbers_in()/value_supported_by_quote()
    already extend to a disclosed value elsewhere in this codebase, kept
    consistent here). Deliberately still excludes currency conversion,
    a differing basis (per-token vs. per-million-tokens), and any other
    unit mismatch across the two sides being compared -- this rule
    trusts the stored value's basis is already consistent for a given
    field, the same trust IntegerComparisonRule already places in
    context_window_tokens always meaning the same unit (tokens)."""

    unit: str = "USD"

    def parse(self, value: str) -> float:
        cleaned = value.strip().lstrip("$").replace(",", "").strip()
        try:
            return float(cleaned)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Cannot parse price value: {value!r}") from exc

    def relation(self, parsed_a: object, parsed_b: object) -> str:
        # Same reasoning as IntegerComparisonRule.relation's own comment
        # -- object, not float, to match the ComparisonRule Protocol
        # exactly; an explicit raise (not assert) so it still fails
        # loudly under -O and isn't stripped (bandit B101).
        if not isinstance(parsed_a, (int, float)) or not isinstance(parsed_b, (int, float)):
            raise TypeError(f"relation() expected floats/ints, got {parsed_a!r} and {parsed_b!r}")
        if parsed_a < parsed_b:
            return "lower"
        if parsed_a > parsed_b:
            return "higher"
        return "equal"


COMPARISON_RULES: dict[str, ComparisonRule] = {
    "context_window_tokens": IntegerComparisonRule(unit="tokens"),
    "input_price_usd": PriceComparisonRule(unit="USD"),
    "output_price_usd": PriceComparisonRule(unit="USD"),
    # benchmark_scores, availability_regions, modalities, licence_terms:
    # still deliberately absent -- see IntegerComparisonRule's docstring
    # and ADR 0005 point (f); each needs its own representation designed
    # before it can be added here. compare_subjects() drops any
    # candidate naming a field not in this registry, the same way it
    # already drops a field not in COMPARABLE_FIELDS at all.
}
