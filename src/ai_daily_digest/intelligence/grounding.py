"""Shared evidence-grounding primitives, used by every place in
intelligence/ that has to answer "is this specific value actually backed
by this specific piece of text" — extract_facts.py (value vs. quoted
span), compare_subjects.py (claim prose vs. the fact-table rows it's
allowed to cite), and validate.py (claim text vs. cited snapshot
content).

Deliberately narrow and deterministic, per docs/ARCHITECTURE.md's
"prefer deterministic code" rule: this is NOT a semantic entailment or
contradiction checker (that would need an LLM, and isn't built). What it
does check, precisely: does every *number* asserted in some text also
appear in some reference text, using formatting-tolerant, word/digit-
boundary-safe matching. That's a real, meaningful check for this
project's fields (context_window_tokens, prices, benchmark_scores are
all numeric) but it is a floor, not a ceiling — a claim could still be
numerically grounded and prose-wise wrong in some other way (e.g. it
could swap which subject a real, cited number belongs to, if nothing
else catches that first). Callers layer this on top of their own
subject/field/citation-ownership checks, not instead of them.
"""

from __future__ import annotations

import re

from ai_daily_digest.intelligence.facts import normalise_name

# Strips thousands separators and currency/percent symbols WITHOUT
# turning them into word-boundary spaces (unlike normalise_name's
# punctuation strip) -- "256,000" and "256000" must collapse to the same
# digit run, not split into two tokens ("256", "000").
_NUMBER_FORMATTING_RE = re.compile(r"[,$%]")

# A bare token of digits (optionally with one decimal point) -- what
# counts as "a number" for grounding purposes here. Excludes a digit run
# immediately touching a letter on either side, with nothing between them
# ("o1", the "4" in "GPT-4o") -- a name where a letter is glued directly
# onto a digit. Hyphen-connected names ("GPT-4", "GPT-4o", "GPT-4.5") are
# NOT handled here -- see _COMPOUND_PRODUCT_NAME_RE below for why they
# need a different mechanism.
_NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?(?![A-Za-z])")

# A whole "<letters>-<digits>[.<digits>][<letters>]" token -- e.g.
# "GPT-4", "GPT-4o", "GPT-4.5", "Claude-3.5", "Gemini-1.5". Every digit
# run inside a match of this pattern is part of a product name/version,
# never an asserted number, regardless of where within the token it
# falls -- matched and excluded as a WHOLE SPAN in numbers_in() below,
# rather than via a boundary lookaround anchored to each individual digit
# run's own position.
#
# That span-based approach replaces an earlier lookbehind-only fix
# (`(?<![A-Za-z]-)`) that excluded a match from STARTING at "4" in
# "GPT-4.5", but couldn't stop the regex engine from then retrying and
# matching "5" on its own -- nothing about "5", looked at from its own
# position, is adjacent to a letter or a hyphen. Python's `re` module
# only supports fixed-width lookbehind, so there's no way to ask "does
# some letter-hyphen sequence appear an arbitrary distance back from
# here" from inside a single number-matching regex; matching the whole
# name+version token as its own span sidesteps that limitation entirely
# instead of chasing further boundary special cases.
#
# Deliberately requires the LETTERS to come first, before the hyphen: a
# number followed by a hyphenated word ("256,000-token context window")
# is a real, legitimate, already-tested number and must not match this
# pattern -- digits, not letters, precede its hyphen.
_COMPOUND_PRODUCT_NAME_RE = re.compile(r"[A-Za-z]+-\d+(?:\.\d+)?[A-Za-z]*")
_MULTIPLIER_TOKEN_RE = re.compile(r"(?<![A-Za-z])(\d+(?:\.\d+)?)\s*([kKmM])(?![A-Za-z])")


def numbers_in(text: str) -> set[str]:
    """Every standalone numeric token in `text`, with formatting
    differences (commas, currency/percent symbols) normalised away so
    "$256,000" and "256000" are the same token. Digits that are really
    part of a product name/version are not numbers for this purpose --
    glued directly to a letter ("o1"), or hyphen-connected with letters
    on either side ("GPT-4", "GPT-4o", "GPT-4.5") -- see
    _NUMBER_TOKEN_RE and _COMPOUND_PRODUCT_NAME_RE. Used to compare "what
    numbers does this claim assert" against "what numbers does this
    evidence actually contain"."""
    stripped = _NUMBER_FORMATTING_RE.sub("", text)
    excluded_spans = [match.span() for match in _COMPOUND_PRODUCT_NAME_RE.finditer(stripped)]
    numbers: set[str] = set()
    for match in _NUMBER_TOKEN_RE.finditer(stripped):
        if any(start <= match.start() and match.end() <= end for start, end in excluded_spans):
            continue
        numbers.add(match.group())
    return numbers


def _multiplier_numbers_in(text: str) -> set[str]:
    stripped = _NUMBER_FORMATTING_RE.sub("", text)
    excluded_spans = [match.span() for match in _COMPOUND_PRODUCT_NAME_RE.finditer(stripped)]
    multiplier_nums: set[str] = set()
    for match in _MULTIPLIER_TOKEN_RE.finditer(stripped):
        if any(start <= match.start() and match.end() <= end for start, end in excluded_spans):
            continue
        val_str, suffix = match.group(1), match.group(2).lower()
        multiplier = 1_000 if suffix == "k" else 1_000_000
        try:
            num = float(val_str) * multiplier
            multiplier_nums.add(str(int(num)) if num.is_integer() else str(num))
        except (ValueError, OverflowError):
            pass
    return multiplier_nums


def value_supported_by_quote(value: str, quoted_span: str) -> bool:
    """True if `value` is actually backed by `quoted_span` -- catches a
    model that quotes a real sentence from the source but attaches a
    different, invented value to it (the quote is grounded per
    extract_facts.py's existing check; the value inside it might not
    be). Two independent checks, either is sufficient:

    1. Non-numeric / exact-phrase match: `value` appears as a whole,
       space-bounded token/phrase inside the quote (word-boundary, not a
       loose substring -- mirrors resolve.py's _contains_phrase). Covers
       fields like licence_terms ("Apache-2.0") and modalities.
    2. Numeric match: `value`'s digit run appears in the quote's digit
       run, tolerant of comma/currency/percent formatting differences,
       but NOT as a substring of a *larger* number (value "20" must not
       match inside quote text containing "120").
    """
    normalised_value = normalise_name(value)
    normalised_quote = normalise_name(quoted_span)
    if normalised_value and f" {normalised_value} " in f" {normalised_quote} ":
        return True

    value_numbers = numbers_in(value) | _multiplier_numbers_in(value)
    if not value_numbers:
        return False
    quote_numbers = numbers_in(quoted_span) | _multiplier_numbers_in(quoted_span)
    return value_numbers <= quote_numbers
