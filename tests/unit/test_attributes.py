"""Direct tests for shared/attributes.py's ComparisonRule implementations
-- as opposed to test_compare_subjects.py, which covers how a rule is
used inside compare_subjects()'s own guardrails, this is about the rule
classes themselves."""

from decimal import Decimal

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
        ("$5.00", Decimal("5.00")),
        ("5", Decimal("5")),
        ("0.0025", Decimal("0.0025")),
        ("1,200.50", Decimal("1200.50")),
    ],
)
def test_price_rule_parses_well_formed_values(raw: str, expected: Decimal) -> None:
    assert PriceComparisonRule().parse(raw) == expected


@pytest.mark.parametrize("raw", ["free", "undisclosed"])
def test_price_rule_parse_rejects_non_numeric_strings(raw: str) -> None:
    with pytest.raises(ValueError, match="Cannot parse price value"):
        PriceComparisonRule().parse(raw)


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity", "-Infinity"])
def test_price_rule_parse_rejects_non_finite_values(raw: str) -> None:
    with pytest.raises(ValueError, match="Cannot parse price value"):
        PriceComparisonRule().parse(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "$$5.00",  # repeated "$"
        "1e10",  # scientific notation
        "1_000",  # underscores
        "-5",  # negative, not explicitly approved
        "-5.00",
        "5.0.0",  # multiple decimal points
        "$",  # sign with no digits
        "",
    ],
)
def test_price_rule_parse_rejects_malformed_strings(raw: str) -> None:
    with pytest.raises(ValueError, match="Cannot parse price value"):
        PriceComparisonRule().parse(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "1,2",
        "12,34",
        "1,234,56",
        "$1,00",
    ],
)
def test_price_rule_parse_rejects_malformed_comma_grouping(raw: str) -> None:
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
    with pytest.raises(TypeError, match="expected two Decimals"):
        PriceComparisonRule().relation("5", "3")


def test_price_rule_default_unit_is_usd() -> None:
    assert PriceComparisonRule().unit == "USD"


# --- COMPARISON_RULES registry ---


def test_price_fields_are_not_registered_pending_basis_design() -> None:
    """PriceComparisonRule exists and is unit-tested above, but is
    deliberately NOT registered in COMPARISON_RULES yet -- ADR 0005
    point (f) requires a currency/unit/basis design (e.g. reconciling
    per-token vs. per-million-tokens pricing) to be accepted first, and
    that follow-up ADR hasn't been written (review, 2026-09-09)."""
    assert "input_price_usd" not in COMPARISON_RULES
    assert "output_price_usd" not in COMPARISON_RULES


def test_fields_without_a_designed_representation_stay_unregistered() -> None:
    """ADR 0005 point (f): benchmark_scores/availability_regions/
    licence_terms/modalities are deliberately excluded, not guessed at
    -- same reasoning that keeps the price fields unregistered above."""
    for field in ("benchmark_scores", "availability_regions", "licence_terms", "modalities"):
        assert field not in COMPARISON_RULES


def test_context_window_tokens_rule_is_unaffected_by_phase_2() -> None:
    assert isinstance(COMPARISON_RULES["context_window_tokens"], IntegerComparisonRule)
