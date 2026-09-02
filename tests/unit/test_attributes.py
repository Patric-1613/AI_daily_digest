"""Direct tests for shared/attributes.py's ComparisonRule implementations
-- as opposed to test_compare_subjects.py, which covers how a rule is
used inside compare_subjects()'s own guardrails, this is about the rule
classes themselves."""

import pytest

from ai_daily_digest.shared.attributes import (
    COMPARISON_RULES,
    IntegerComparisonRule,
    PriceComparisonRule,
)

# --- PriceComparisonRule (ADR 0005 Phase 2) ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$5.00", 5.0),
        ("5", 5.0),
        ("0.0025", 0.0025),
        ("1,200.50", 1200.50),
    ],
)
def test_price_rule_parses_well_formed_values(raw: str, expected: float) -> None:
    assert PriceComparisonRule().parse(raw) == expected


@pytest.mark.parametrize("raw", ["free", "undisclosed"])
def test_price_rule_parse_rejects_non_numeric_strings(raw: str) -> None:
    with pytest.raises(ValueError, match="Cannot parse price value"):
        PriceComparisonRule().parse(raw)


def test_price_rule_relation_lower() -> None:
    rule = PriceComparisonRule()
    assert rule.relation(rule.parse("3"), rule.parse("5")) == "lower"


def test_price_rule_relation_higher() -> None:
    rule = PriceComparisonRule()
    assert rule.relation(rule.parse("5"), rule.parse("3")) == "higher"


def test_price_rule_relation_equal() -> None:
    rule = PriceComparisonRule()
    assert rule.relation(rule.parse("5"), rule.parse("5.00")) == "equal"


def test_price_rule_relation_rejects_non_numeric_input() -> None:
    """relation() is only ever meant to be called with parse()'s own
    output -- a caller passing something else (e.g. the raw string
    directly, skipping parse()) fails loudly rather than comparing
    nonsense."""
    with pytest.raises(TypeError, match="expected floats/ints"):
        PriceComparisonRule().relation("5", "3")


def test_price_rule_default_unit_is_usd() -> None:
    assert PriceComparisonRule().unit == "USD"


# --- COMPARISON_RULES registry (Phase 2) ---


def test_both_price_fields_are_registered() -> None:
    assert isinstance(COMPARISON_RULES["input_price_usd"], PriceComparisonRule)
    assert isinstance(COMPARISON_RULES["output_price_usd"], PriceComparisonRule)


def test_fields_without_a_designed_representation_stay_unregistered() -> None:
    """ADR 0005 point (f): benchmark_scores/availability_regions/
    licence_terms/modalities are deliberately excluded, not guessed at
    -- unaffected by Phase 2's addition of the two price fields."""
    for field in ("benchmark_scores", "availability_regions", "licence_terms", "modalities"):
        assert field not in COMPARISON_RULES


def test_context_window_tokens_rule_is_unaffected_by_phase_2() -> None:
    assert isinstance(COMPARISON_RULES["context_window_tokens"], IntegerComparisonRule)
