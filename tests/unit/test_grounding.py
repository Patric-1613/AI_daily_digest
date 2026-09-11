"""Direct unit tests for the shared grounding primitives -- the
adversarial cases here are what extract_facts.py, compare_subjects.py,
and validate.py all depend on to catch a fabricated value hiding behind
a real-looking citation or quote."""

from ai_daily_digest.intelligence.grounding import numbers_in, value_supported_by_quote


def test_numbers_in_strips_comma_and_currency_formatting() -> None:
    assert numbers_in("The price is $1,234.50 per month") == {"1234.50"}


def test_numbers_in_finds_every_distinct_number() -> None:
    assert numbers_in("increased from 128000 to 256000") == {"128000", "256000"}


def test_numbers_in_ignores_digits_embedded_in_a_product_name() -> None:
    """The "4" in "GPT-4o" is part of a name, not an asserted number --
    if it were picked up, a well-grounded comparison mentioning "GPT-4o"
    alongside a real number could be wrongly rejected as fabricated."""
    assert numbers_in("OpenAI's GPT-4o has a 256,000-token context window") == {"256000"}
    assert numbers_in("OpenAI's o1 model") == set()


def test_numbers_in_ignores_bare_digit_product_name_with_no_trailing_letter() -> None:
    """Per review: "GPT-4" (unlike "GPT-4o") has nothing letter-like
    directly after its digit, so the trailing-letter exclusion alone
    doesn't catch it -- the preceding "letter-hyphen" pattern must be
    excluded too, or a claim mentioning "GPT-4" picks up a spurious "4"
    that can make an otherwise well-grounded claim wrongly fail its
    content check if the cited evidence spells the name differently
    (e.g. "GPT4", no hyphen -- see
    test_claim_and_evidence_agree_on_a_bare_digit_product_name below for
    that exact shape)."""
    assert numbers_in("GPT-4 costs $5 per month") == {"5"}


def test_claim_and_evidence_agree_on_a_bare_digit_product_name() -> None:
    """End-to-end shape of the bug: a claim mentioning "GPT-4" and its
    cited evidence spelling the same product "GPT4" (no hyphen) must
    still be recognized as mutually grounded -- before the fix, the
    claim's spurious "4" (from "GPT-4") wasn't matched by the evidence's
    numbers (which correctly never had a stray "4" from "GPT4", since
    that digit sits directly against the letter "T"), so the subset
    check validate.py/compare_subjects.py both rely on would have failed
    a claim that was actually fully grounded."""
    claim_numbers = numbers_in("GPT-4's price is 5.")
    evidence_numbers = numbers_in("GPT4 costs 5 dollars.")
    assert claim_numbers <= evidence_numbers


def test_numbers_in_ignores_compound_decimal_product_names() -> None:
    """Fixes issue #7: a compound decimal-shaped name like "GPT-4.5"
    was leaking a spurious "5" -- the earlier lookbehind-only fix
    excluded a match from starting at "4" (preceded by "T-"), but
    couldn't stop the regex engine from then retrying and matching "5"
    on its own, since nothing about "5" alone looks name-adjacent from
    its own position. Fixed via whole-span exclusion (see
    _COMPOUND_PRODUCT_NAME_RE) -- verified across the exact names named
    in the issue."""
    assert numbers_in("The new GPT-4.5 model launched today.") == set()
    assert numbers_in("Claude-3.5 is Anthropic's latest model.") == set()
    assert numbers_in("Gemini-1.5 supports a huge context window.") == set()


def test_numbers_in_still_finds_real_numbers_alongside_a_compound_decimal_name() -> None:
    """The exact shape from issue #7's acceptance criteria: a real price
    sitting right next to a compound decimal product name must still be
    extracted correctly -- the fix must not overreach into excluding
    legitimate numbers just because a name-shaped token is nearby."""
    assert numbers_in("GPT-4.5 costs $5.50 per 1M tokens") == {"5.50"}
    assert numbers_in("Claude-3.5 scored 71.2 on the benchmark") == {"71.2"}


def test_numbers_in_ignores_bare_decimal_with_no_letters_attached() -> None:
    """A plain decimal with nothing letter-like touching it (no name to
    confuse it with) must always be treated as a real, asserted number."""
    assert numbers_in("The discount rate is 0.5 this quarter.") == {"0.5"}


def test_numbers_in_empty_when_no_digits() -> None:
    # Not "Apache-2.0" -- a digit run preceded by "<letter>-" is excluded
    # (the same "GPT-4"/"GPT-4.5" shape), so "Apache-2.0" doesn't
    # contribute a number here either; using "MIT" keeps this test about
    # the true no-digits-at-all case either way.
    assert numbers_in("MIT licence, no numbers here") == set()


def test_value_supported_when_number_matches_despite_comma_formatting() -> None:
    assert value_supported_by_quote("256000", "increased to 256,000 tokens") is True


def test_value_not_supported_when_number_embedded_in_a_larger_number() -> None:
    """value "20" must not be considered supported by a quote that only
    contains "120" -- a naive substring check would get this wrong."""
    assert value_supported_by_quote("20", "the price is $120 per month") is False


def test_value_not_supported_when_number_never_appears_in_quote() -> None:
    """The exact fabrication case: a real, grounded quote (it does appear
    in the source -- extract_facts.py checks that separately) that simply
    doesn't say the value the model claims it does."""
    assert value_supported_by_quote("999999", "increased to 256,000 tokens") is False


def test_value_supported_for_exact_non_numeric_phrase() -> None:
    assert value_supported_by_quote("Apache-2.0", "now licensed under Apache-2.0 terms") is True


def test_value_not_supported_for_unrelated_non_numeric_phrase() -> None:
    assert value_supported_by_quote("MIT", "now licensed under Apache-2.0 terms") is False


def test_value_supported_when_number_has_metric_multiplier() -> None:
    assert value_supported_by_quote("100000", "introducing a 100K context window") is True
    assert value_supported_by_quote("200000", "now supports 200k context window") is True
    assert value_supported_by_quote("1000000", "scaling up to 1M tokens") is True


def test_empty_value_is_never_supported() -> None:
    assert value_supported_by_quote("", "any quote at all") is False
