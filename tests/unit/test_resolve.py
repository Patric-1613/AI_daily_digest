import uuid
from datetime import UTC, datetime

from ai_daily_digest.intelligence.loaders import FixtureLoader
from ai_daily_digest.intelligence.resolve import (
    SubjectAlias,
    load_alias_table,
    quoted_span_supports_subject,
    resolve_deterministic,
)
from ai_daily_digest.shared.schemas import SourceItem, Subject

# The fixture pack's known subjects (see tests/fixtures/contracts/README.md)
KNOWN_SUBJECTS = [
    Subject(company="OpenAI", product="GPT-4o"),
    Subject(company="Anthropic", product="Claude"),
]

TR_ITEM_UNRELATED = uuid.UUID("01a01e2f-23e8-78c0-bb25-d9ea47beb168")
TR_ITEM_AMB = uuid.UUID("01a01e2f-27d0-7780-af0c-cf29b575940f")
TR_ITEM_O1 = uuid.UUID("01a01e2f-2bb8-7191-8561-23188072a595")
TR_ITEM_O100 = uuid.UUID("01a01e2f-2fa0-7290-a1ff-1f0214d73621")
TR_ITEM_X = uuid.UUID("01a01e2f-3388-77b0-a5b7-cc071d465b25")


def _item(item_id: uuid.UUID, title: str, publisher: str = "Test Publisher") -> SourceItem:
    return SourceItem(
        id=item_id,
        dedupe_key=f"sha256:{item_id}",
        source_id="test-source",
        publisher=publisher,
        title=title,
        canonical_url="https://example.com/a",  # type: ignore[arg-type]
        first_fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def test_all_fixture_items_resolve_with_zero_false_merges() -> None:
    """A false merge is worse than a miss, so this is checked explicitly
    per item against the fixture pack's real content, not just counted."""
    loader = FixtureLoader()
    items = loader.load_items()
    alias_table = load_alias_table()

    # The fixture pack's real, frozen UUID v7 ids (see
    # tests/fixtures/contracts/README.md) -- items 1-3 are OpenAI/GPT-4o,
    # item 4 is Anthropic/Claude.
    expected_by_publisher_hint = {
        uuid.UUID("01a01e6a-a260-7f03-9481-0c52e1f35714"): Subject(
            company="OpenAI", product="GPT-4o"
        ),
        uuid.UUID("01a01ef8-8a80-7271-a956-b9f73c508585"): Subject(
            company="OpenAI", product="GPT-4o"
        ),
        uuid.UUID("019e8798-d240-77d2-b10f-c6ab78299ae1"): Subject(
            company="OpenAI", product="GPT-4o"
        ),
        uuid.UUID("01a01a5b-82c0-72c1-aff1-dce965ea56ff"): Subject(
            company="Anthropic", product="Claude"
        ),
    }

    for item in items:
        text = loader.snapshot_text(item.latest_snapshot_id) if item.latest_snapshot_id else ""
        result = resolve_deterministic(item, KNOWN_SUBJECTS, alias_table, item_text=text)
        expected = expected_by_publisher_hint.get(item.id)
        if expected is not None:
            assert result.subject == expected, (
                f"{item.id} resolved to {result.subject!r}, expected {expected!r}"
            )


def test_unrelated_item_does_not_match() -> None:
    alias_table = load_alias_table()
    item = _item(TR_ITEM_UNRELATED, "Local bakery wins regional award")
    result = resolve_deterministic(
        item,
        KNOWN_SUBJECTS,
        alias_table,
        item_text="A bakery in town has won an award for its sourdough.",
    )
    assert result.subject is None
    assert result.method == "no_match"


def test_ambiguous_when_two_subjects_both_match() -> None:
    shared = Subject(company="Shared Co", product="Shared Product")
    other = Subject(company="Other Co", product="Shared Product")
    # Reviewed aliases (not the bare product name) so this test exercises
    # ambiguity detection alone, independent of the bare-product-name
    # provenance gate (see test_medieval_codex... below for that).
    alias_table = [
        SubjectAlias(subject=shared, aliases=["shared product"]),
        SubjectAlias(subject=other, aliases=["shared product"]),
    ]
    item = _item(TR_ITEM_AMB, "Shared Product gets an update")
    result = resolve_deterministic(
        item,
        [shared, other],
        alias_table=alias_table,
        item_text="Details about Shared Product follow.",
    )
    assert result.subject is None
    assert result.method == "ambiguous_multi_subject"
    assert set(result.candidate_subjects) == {shared, other}


def test_short_two_character_product_names_still_match() -> None:
    """Real OpenAI models are literally named "o1"/"o3" -- a 2-character
    candidate must still be matchable as a whole, space-bounded token."""
    subject = Subject(company="OpenAI", product="o1")
    item = _item(TR_ITEM_O1, "o1 launches today")
    result = resolve_deterministic(
        item, [subject], alias_table=[], item_text="OpenAI's o1 model is now generally available."
    )
    assert result.subject == subject


def test_short_product_name_does_not_match_as_a_substring_of_a_longer_word() -> None:
    """ "o1" must not match inside "o100" or similar -- word-boundary
    matching, not a loose substring check."""
    subject = Subject(company="OpenAI", product="o1")
    item = _item(TR_ITEM_O100, "o100 launches today")
    result = resolve_deterministic(
        item,
        [subject],
        alias_table=[],
        item_text="A completely different product called o100 was announced.",
    )
    assert result.subject is None


def test_no_match_returns_all_known_subjects_as_candidates_for_llm_fallback() -> None:
    item = _item(TR_ITEM_X, "Totally unrelated headline")
    result = resolve_deterministic(
        item, KNOWN_SUBJECTS, alias_table=[], item_text="Nothing about tracked subjects here."
    )
    assert result.method == "no_match"
    assert set(result.candidate_subjects) == set(KNOWN_SUBJECTS)


def test_langchain_pypi_resolves_to_canonical_subject() -> None:
    item = SourceItem(
        id=uuid.UUID("01a01e2f-4110-7aa0-8b10-123456789abc"),
        dedupe_key="sha256:langchain-9.9.0",
        source_id="langchain_pypi",
        publisher="Python Package Index",
        title="9.9.0",
        canonical_url="https://pypi.org/project/langchain/9.9.0/",  # type: ignore[arg-type]
        first_fetched_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text="Release notes for 9.9.0 with performance improvements and bug fixes.",
    )
    assert result.subject == Subject(company="LangChain", product="LangChain")
    assert result.method == "alias_match"
    assert result.confidence == 0.95
    assert result.matched_text == "langchain_pypi"


def test_langgraph_pypi_resolves_to_canonical_subject() -> None:
    item = SourceItem(
        id=uuid.UUID("01a01e2f-4220-7bb0-8c20-abcdef123456"),
        dedupe_key="sha256:langgraph-0.2.1",
        source_id="langgraph_pypi",
        publisher="Python Package Index",
        title="0.2.1",
        canonical_url="https://pypi.org/project/langgraph/0.2.1/",  # type: ignore[arg-type]
        first_fetched_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text="Release notes for 0.2.1 with bug fixes and improvements.",
    )
    assert result.subject == Subject(company="LangChain", product="LangGraph")
    assert result.method == "alias_match"
    assert result.confidence == 0.95
    assert result.matched_text == "langgraph_pypi"


def test_generic_publisher_does_not_force_product_match() -> None:
    """A generic company-level publisher must not create a product classification
    when neither the title nor the body contains product-identifying evidence."""
    item = SourceItem(
        id=uuid.UUID("01a01e2f-4440-7dd0-8e40-112233445566"),
        dedupe_key="sha256:generic-post-1",
        source_id="generic_company_blog",
        publisher="LangChain",
        title="Observability platform update",
        canonical_url="https://blog.example.com/observability",  # type: ignore[arg-type]
        first_fetched_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text="General infrastructure changes and monitoring dashboard updates.",
    )
    assert result.subject is None
    assert result.method == "no_match"
    assert result.confidence == 0.0


def test_generic_source_id_does_not_force_product_match() -> None:
    """A generic company source ID (e.g. langchain_changelog) must not force a product
    match without explicit product evidence in the text."""
    item = SourceItem(
        id=uuid.UUID("01a01e2f-4550-7ee0-8f50-667788990011"),
        dedupe_key="sha256:generic-changelog-1",
        source_id="langchain_changelog",
        publisher="Python Package Index",
        title="Quarterly ecosystem recap",
        canonical_url="https://example.com/changelog/recap",  # type: ignore[arg-type]
        first_fetched_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text="Overview of ecosystem events, contributor highlights, and community calls.",
    )
    assert result.subject is None
    assert result.method == "no_match"
    assert result.confidence == 0.0


def test_unknown_source_item_fails_closed() -> None:
    item = SourceItem(
        id=uuid.UUID("01a01e2f-4330-7cc0-8d30-fedcba654321"),
        dedupe_key="sha256:unknown-tool-1.0.0",
        source_id="unknown_tool_pypi",
        publisher="Python Package Index",
        title="1.0.0",
        canonical_url="https://pypi.org/project/unknown-tool/1.0.0/",  # type: ignore[arg-type]
        first_fetched_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text="Release 1.0.0 of an uncatalogued third-party library.",
    )
    assert result.subject is None
    assert result.method == "no_match"
    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# Regression tests using the five real OpenAI RSS entries from the
# rehearsal (titles/descriptions fetched verbatim from
# https://openai.com/news/rss.xml during the follow-up review; see the
# PR description for the exact fetch). Each SUBJECT_CHATGPT/CODEX/etc.
# below is only registered by adding OpenAI/ChatGPT, OpenAI/Codex,
# OpenAI/GPT-Live-1, and OpenAI/GPT-6 Astra to shared/aliases.yaml.
# ---------------------------------------------------------------------------

SUBJECT_CHATGPT = Subject(company="OpenAI", product="ChatGPT")
SUBJECT_CODEX = Subject(company="OpenAI", product="Codex")
SUBJECT_GPT_LIVE_1 = Subject(company="OpenAI", product="GPT-Live-1")
SUBJECT_GPT_6_ASTRA = Subject(company="OpenAI", product="GPT-6 Astra")

TR_ITEM_CODEX_CHATGPT = uuid.UUID("01a01e2f-5001-7000-8000-000000000001")
TR_ITEM_DATA_AGENT = uuid.UUID("01a01e2f-5002-7000-8000-000000000002")
TR_ITEM_FIN_SERVICES = uuid.UUID("01a01e2f-5003-7000-8000-000000000003")
TR_ITEM_GOV_POLICY = uuid.UUID("01a01e2f-5004-7000-8000-000000000004")
TR_ITEM_GPT_LIVE_1 = uuid.UUID("01a01e2f-5005-7000-8000-000000000005")


def test_codex_and_chatgpt_item_is_ambiguous_multi_subject() -> None:
    """Item 1: "How a researcher uses Codex and ChatGPT to search for new
    antimicrobial molecules" explicitly names two tracked products in one
    item. The schema allows only one Subject per item, so this must stay
    unresolved with both candidates preserved -- never silently collapsed
    onto either one by the LLM. publisher="OpenAI" reflects the real
    item's actual source (openai_news) and is also load-bearing here:
    neither "Codex" nor "ChatGPT" appears with the company name in a
    single phrase or as a reviewed alias, so each is a bare-product-name
    match that needs the publisher (or a company-name mention in the
    text, absent here) to corroborate it -- see
    resolve.py::_bare_match_corroborated()."""
    item = _item(
        TR_ITEM_CODEX_CHATGPT,
        "How a researcher uses Codex and ChatGPT to search for new antimicrobial molecules",
        publisher="OpenAI",
    )
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text=(
            "César de la Fuente's lab uses Codex and ChatGPT to search living and "
            "extinct genomes for antimicrobial candidates to fight drug-resistant "
            "infections."
        ),
    )
    assert result.subject is None
    assert result.method == "ambiguous_multi_subject"
    assert set(result.candidate_subjects) == {SUBJECT_CHATGPT, SUBJECT_CODEX}


def test_data_agent_item_resolves_to_chatgpt_product_family() -> None:
    """Item 2: "Now everyone can put data to work" -- the RSS description
    is "Meet the Data agent in ChatGPT Work. Connect company data,
    uncover insights, and build interactive dashboards with AI using
    natural language." "Data agent" and "ChatGPT Work" are a feature and
    a tier, not tracked subjects (per this PR's scope), but the
    description does contain the bare canonical phrase "ChatGPT" as a
    whole word ("...in ChatGPT Work...") -- so this resolves to the
    OpenAI/ChatGPT product family via ordinary deterministic phrase
    matching, the same as any other item that happens to mention a tier
    name alongside the bare product name. This is a documented outcome of
    the "no feature/tier subjects yet" scope decision, not a special
    case in the code. publisher="OpenAI" corroborates the bare "ChatGPT"
    match (see resolve.py::_bare_match_corroborated()) -- the real item's
    actual source (openai_news)."""
    item = _item(TR_ITEM_DATA_AGENT, "Now everyone can put data to work", publisher="OpenAI")
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text=(
            "Meet the Data agent in ChatGPT Work. Connect company data, uncover "
            "insights, and build interactive dashboards with AI using natural "
            "language."
        ),
    )
    assert result.subject == SUBJECT_CHATGPT
    assert result.method == "alias_match"


def test_financial_services_item_is_ambiguous_multi_subject() -> None:
    """Item 3: "Introducing ChatGPT for Financial Services" -- the RSS
    description is "Introducing ChatGPT for Financial Services, combining
    built-in financial data and GPT-6 Astra for research, modeling, and
    client-ready materials." Both "ChatGPT" and "GPT-6 Astra" are
    tracked, whole-phrase matches, so this must stay unresolved with both
    candidates preserved -- "Financial Services" itself is an offering
    name, not a subject (per this PR's scope), and is not what drives the
    ambiguity here. publisher="OpenAI" corroborates both bare-name matches
    (see resolve.py::_bare_match_corroborated()) -- the real item's actual
    source (openai_news)."""
    item = _item(
        TR_ITEM_FIN_SERVICES, "Introducing ChatGPT for Financial Services", publisher="OpenAI"
    )
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text=(
            "Introducing ChatGPT for Financial Services, combining built-in "
            "financial data and GPT-6 Astra for research, modeling, and "
            "client-ready materials."
        ),
    )
    assert result.subject is None
    assert result.method == "ambiguous_multi_subject"
    assert set(result.candidate_subjects) == {SUBJECT_CHATGPT, SUBJECT_GPT_6_ASTRA}


def test_government_policy_item_has_no_deterministic_match() -> None:
    """Item 4: the government/cyber-defense item names no tracked product
    at all -- title "Expanding AI access and cyber defense for federal,
    state, local, and tribal governments", description "OpenAI and GSA
    will offer eligible federal, state, local, and tribal governments $0
    license fees, 50% off usage, and expanded cyber defense support."
    Deterministic matching correctly finds nothing (not even "OpenAI"
    alone triggers a match -- see _candidate_strings(), which never
    includes a bare company name). This is the item that previously
    false-merged onto GPT-4o via the LLM fallback; the fallback's own
    grounding guard is tested in test_resolve_llm.py."""
    item = _item(
        TR_ITEM_GOV_POLICY,
        "Expanding AI access and cyber defense for federal, state, local, and tribal governments",
        publisher="OpenAI",
    )
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text=(
            "OpenAI and GSA will offer eligible federal, state, local, and tribal "
            "governments $0 license fees, 50% off usage, and expanded cyber "
            "defense support."
        ),
    )
    assert result.subject is None
    assert result.method == "no_match"


def test_gpt_live_1_item_resolves_deterministically() -> None:
    """Item 5: "Build more natural voice experiences with GPT\u2011Live\u20111 in
    the API" -- fetched verbatim, the real title/description use a
    Unicode non-breaking hyphen (U+2011), not a plain ASCII "-". A single,
    unambiguous exact match against the canonical OpenAI/GPT-Live-1 entry
    added to aliases.yaml, no alias needed. publisher="OpenAI" corroborates
    the bare-name match (see resolve.py::_bare_match_corroborated()) -- the
    real item's actual source (openai_news)."""
    item = _item(
        TR_ITEM_GPT_LIVE_1,
        "Build more natural voice experiences with GPT\u2011Live\u20111 in the API",
        publisher="OpenAI",
    )
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text=(
            "GPT\u2011Live\u20111 brings natural, full-duplex voice conversations "
            "to the API, with stronger instruction following, custom voices, and "
            "telephony support."
        ),
    )
    assert result.subject == SUBJECT_GPT_LIVE_1
    assert result.method == "alias_match"


def test_gpt_live_1_punctuation_variants_all_match() -> None:
    """normalise_name() strips any non-word/non-space character, so the
    canonical "GPT-Live-1" entry (plain ASCII hyphens) must match all
    three real-world punctuation variants without needing a single added
    alias: a non-breaking hyphen (the real RSS feed's own rendering), a
    plain ASCII hyphen, and bare spaces."""
    alias_table = load_alias_table()
    variants = [
        "GPT\u2011Live\u20111",  # non-breaking hyphens, as published
        "GPT-Live-1",  # plain ASCII hyphens
        "GPT Live 1",  # spaces only
    ]
    for index, variant in enumerate(variants):
        item = _item(
            uuid.UUID(f"01a01e2f-5006-7000-8000-00000000000{index}"),
            f"Build more natural voice experiences with {variant} in the API",
            publisher="OpenAI",
        )
        result = resolve_deterministic(
            item,
            [],
            alias_table=alias_table,
            item_text=f"{variant} brings natural, full-duplex voice conversations to the API.",
        )
        assert result.subject == SUBJECT_GPT_LIVE_1, f"variant {variant!r} failed to match"
        assert result.method == "alias_match"


# ---------------------------------------------------------------------------
# Codex generic-word false-positive: "Codex" is also an ordinary English
# noun (a bound manuscript), so the bare canonical name alone is not
# enough to accept a match -- see resolve.py::_bare_match_corroborated().
# ---------------------------------------------------------------------------

TR_ITEM_MEDIEVAL_CODEX = uuid.UUID("01a01e2f-5009-7000-8000-000000000009")
TR_ITEM_OPENAI_CODEX = uuid.UUID("01a01e2f-500a-7000-8000-00000000000a")
TR_ITEM_OPENAI_NO_CODEX = uuid.UUID("01a01e2f-500b-7000-8000-00000000000b")


def test_medieval_codex_article_does_not_resolve_to_openai_codex() -> None:
    """An unrelated/editorial article using "codex" in its ordinary sense
    (a bound manuscript), published by something with no connection to
    OpenAI and never mentioning OpenAI anywhere in the text either, must
    fail closed -- not resolve to the tracked OpenAI/Codex subject purely
    because the bare word matches."""
    item = _item(
        TR_ITEM_MEDIEVAL_CODEX,
        "A medieval codex was discovered",
        publisher="City History Museum",
    )
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text="Researchers are preserving the ancient codex.",
    )
    assert result.subject is None
    assert result.method == "no_match"


def test_openai_published_codex_mention_resolves_to_openai_codex() -> None:
    """The corroborating case: an explicit OpenAI product announcement
    mentioning Codex resolves normally -- the item's own publisher (the
    real item's actual source, openai_news) corroborates the bare-name
    match."""
    item = _item(TR_ITEM_OPENAI_CODEX, "Codex gets new capabilities", publisher="OpenAI")
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text="OpenAI is rolling out new capabilities for Codex today.",
    )
    assert result.subject == SUBJECT_CODEX
    assert result.method == "alias_match"


def test_openai_article_without_codex_mention_is_not_forced_to_codex() -> None:
    """A genuine OpenAI-published item that never mentions Codex at all
    must not be forced onto it merely because the publisher matches --
    provenance corroborates a textual match, it never substitutes for
    one."""
    item = _item(
        TR_ITEM_OPENAI_NO_CODEX, "OpenAI announces a new safety initiative", publisher="OpenAI"
    )
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text="The company outlined new safety research commitments today.",
    )
    assert result.subject is None
    assert result.method == "no_match"


def test_third_party_report_naming_the_company_elsewhere_in_text_still_resolves() -> None:
    """A third-party outlet's own coverage, mentioning the company
    somewhere in the text even though it isn't adjacent to the product
    name, still corroborates a bare-name match -- provenance isn't
    limited to "the publisher equals the company" (see
    resolve.py::_bare_match_corroborated()). This mirrors
    tests/fixtures/contracts's own TechDesk/GPT-4o fixture item."""
    item = _item(
        TR_ITEM_OPENAI_CODEX,
        "A researcher's toolkit",
        publisher="TechDesk",
    )
    result = resolve_deterministic(
        item,
        [],
        alias_table=load_alias_table(),
        item_text="OpenAI's Codex is being used by researchers in a new toolkit.",
    )
    assert result.subject == SUBJECT_CODEX
    assert result.method == "alias_match"


# ---------------------------------------------------------------------------
# quoted_span_supports_subject() -- the deterministic grounding check
# resolve_llm.py relies on before accepting an existing-subject match.
# ---------------------------------------------------------------------------


def test_quoted_span_supports_subject_rejects_missing_quote() -> None:
    assert not quoted_span_supports_subject(
        Subject(company="OpenAI", product="GPT-4o"),
        None,
        item_text="Anything at all.",
        alias_table=[],
    )


def test_quoted_span_supports_subject_rejects_fabricated_span() -> None:
    """A quote that simply isn't present in the item text at all --
    whether hallucinated outright or lightly paraphrased -- must be
    rejected on the verbatim-substring check alone, before the phrase
    check even runs."""
    assert not quoted_span_supports_subject(
        Subject(company="OpenAI", product="GPT-4o"),
        "GPT-4o powers this new government partnership",
        item_text="OpenAI announced a new partnership with a federal agency today.",
        alias_table=[],
    )


def test_quoted_span_supports_subject_rejects_mid_word_embedded_quote() -> None:
    """A quote that is technically a raw character substring of item_text
    but only because it's embedded inside a different, longer word (e.g.
    "GPT-4o" inside "GPT-4oXtra") must be rejected -- a bare `in` check
    would wrongly accept this since it ignores word boundaries."""
    assert not quoted_span_supports_subject(
        Subject(company="OpenAI", product="GPT-4o"),
        "GPT-4o",
        item_text="This release note describes GPT-4oXtra's new features.",
        alias_table=[],
    )


def test_quoted_span_supports_subject_rejects_company_only_evidence() -> None:
    """A real, verbatim quote that names only the company -- exactly the
    government/policy rehearsal case -- must be rejected. This is the
    general grounding rule doing its job, not a keyword denylist for
    "government"/"policy"/"partnership"."""
    item_text = "OpenAI announced a new partnership with a federal agency today."
    assert not quoted_span_supports_subject(
        Subject(company="OpenAI", product="GPT-4o"),
        "OpenAI announced a new partnership with a federal agency today.",
        item_text=item_text,
        alias_table=[],
    )


def test_quoted_span_supports_subject_accepts_valid_grounded_quote() -> None:
    """The positive case: a verbatim quote that both appears in the item
    text and names the specific proposed product."""
    item_text = "OpenAI's GPT-4o now supports a 256,000 token context window."
    assert quoted_span_supports_subject(
        Subject(company="OpenAI", product="GPT-4o"),
        "GPT-4o now supports a 256,000 token context window",
        item_text=item_text,
        alias_table=[],
    )


def test_quoted_span_supports_subject_accepts_reviewed_alias() -> None:
    """The phrase check must accept a reviewed alias, not only the bare
    canonical product name."""
    subject = Subject(company="OpenAI", product="GPT-4o")
    alias_table = load_alias_table()
    item_text = "The gpt4o rollout is now complete for all API customers."
    assert quoted_span_supports_subject(
        subject,
        "The gpt4o rollout is now complete",
        item_text=item_text,
        alias_table=alias_table,
    )
