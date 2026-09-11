"""Tests the compiled graph end-to-end with injected fake call_fns for
every LLM call site — no network/API key needed. This exercises the
wiring (state passing, conditional routing) on top of logic that's
already covered by each node's own unit tests."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from ai_daily_digest.intelligence.extract_facts import FactCandidate, FactExtractionResponse
from ai_daily_digest.intelligence.facts import FactStore
from ai_daily_digest.intelligence.graph import build_graph
from ai_daily_digest.intelligence.resolve import SubjectAlias
from ai_daily_digest.intelligence.resolve_llm import ResolveLLMResponse
from ai_daily_digest.shared.schemas import DocumentSnapshot, SourceItem, Subject

OPENAI_GPT4O = Subject(company="OpenAI", product="GPT-4o")

# The orchestrator's injected batch detection time (ADR 0008 section 5.A) --
# .250000 microseconds on purpose, so tests can assert it survives intact.
TG_DETECTED_AT = datetime(2026, 8, 20, 12, 0, 0, 250000, tzinfo=UTC)

TG_ITEM_LAUNCH = uuid.UUID("019e85d8-3680-7023-bb24-6d440e85eb11")
TG_ITEM_256K = uuid.UUID("01a01cae-9a80-78a2-9a8a-c4d249e93d11")
TG_ITEM_AMBIGUOUS = uuid.UUID("01a01788-3e80-76a1-9574-dc0d398c4c47")
TG_ITEM_LECHAT = uuid.UUID("01a01cae-9e68-7622-a0fa-5c439040e356")
TG_ITEM_UNKNOWN = uuid.UUID("01a01788-4268-73b0-81e3-7a871f8f99fb")
TG_SNAP_LAUNCH = uuid.UUID("019e85d8-3e50-7571-8a64-e2d24fc1234e")
TG_SNAP_256K = uuid.UUID("01a01cae-a250-7dd2-b02c-5fbb7b3d3f7f")
TG_SNAP_AMB = uuid.UUID("01a01788-4650-72d3-8dbb-b65bf1c69df4")
TG_SNAP_LECHAT = uuid.UUID("01a01cae-a638-7773-9890-d0fa83b3c72c")
TG_SNAP_UNKNOWN = uuid.UUID("01a01788-4a38-7a81-8bc9-e52f5ba7a84d")


def _item(
    item_id: uuid.UUID, title: str, canonical_url: str = "https://openai.com/a"
) -> SourceItem:
    return SourceItem(
        id=item_id,
        dedupe_key=f"sha256:{item_id}",
        source_id="openai_news",
        publisher="OpenAI",
        title=title,
        canonical_url=canonical_url,  # type: ignore[arg-type]
        first_fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def _snapshot(
    snap_id: uuid.UUID, item_id: uuid.UUID, text: str, fetched_at: datetime
) -> DocumentSnapshot:
    return DocumentSnapshot(
        id=snap_id,
        source_item_id=item_id,
        fetched_at=fetched_at,
        content_hash=f"sha256:{snap_id}",
        content_text=text,
    )


def _extraction_fake(
    field: str, value: str, quoted_span: str, confidence: float = 0.95
) -> Callable[[str, str], FactExtractionResponse]:
    def fake_call(system: str, prompt: str) -> FactExtractionResponse:
        return FactExtractionResponse(
            facts=[
                FactCandidate(
                    field=field, value=value, quoted_span=quoted_span, confidence=confidence
                )
            ]
        )

    return fake_call


def test_first_observation_produces_no_claims() -> None:
    store = FactStore()
    store.register_subject(OPENAI_GPT4O)  # already tracked -- deterministic matching needs this
    graph = build_graph(
        store,
        {},
        batch_detected_at=TG_DETECTED_AT,
        alias_table=[],
        extract_call_fn=_extraction_fake(
            "context_window_tokens", "128000", "128,000 token context window"
        ),
    )
    item = _item(TG_ITEM_LAUNCH, "Introducing GPT-4o")
    snapshot = _snapshot(
        TG_SNAP_LAUNCH,
        TG_ITEM_LAUNCH,
        "OpenAI is launching GPT-4o with a 128,000 token context window.",
        datetime(2026, 6, 2, tzinfo=UTC),
    )
    result = graph.invoke({"item": item, "snapshot": snapshot})

    assert result["subject"] == OPENAI_GPT4O
    assert result["claims"] == []  # first observation, not a change
    current = store.get_current_fact(OPENAI_GPT4O, "context_window_tokens")
    assert current is not None
    assert current.value == "128000"


def test_changed_value_flows_through_to_a_supported_claim() -> None:
    store = FactStore()
    store.register_subject(OPENAI_GPT4O)  # already tracked -- deterministic matching needs this
    change_set_ids: dict[Subject, uuid.UUID] = {}
    launch_graph = build_graph(
        store,
        change_set_ids,
        batch_detected_at=TG_DETECTED_AT,
        alias_table=[],
        extract_call_fn=_extraction_fake(
            "context_window_tokens", "128000", "128,000 token context window"
        ),
    )
    launch_item = _item(TG_ITEM_LAUNCH, "Introducing GPT-4o")
    launch_snapshot = _snapshot(
        TG_SNAP_LAUNCH,
        TG_ITEM_LAUNCH,
        "OpenAI is launching GPT-4o with a 128,000 token context window.",
        datetime(2026, 6, 2, tzinfo=UTC),
    )
    launch_graph.invoke({"item": launch_item, "snapshot": launch_snapshot})

    update_graph = build_graph(
        store,
        change_set_ids,
        batch_detected_at=TG_DETECTED_AT,
        alias_table=[],
        extract_call_fn=_extraction_fake(
            "context_window_tokens", "256000", "increased to 256,000 tokens"
        ),
    )
    update_item = _item(TG_ITEM_256K, "GPT-4o now supports a 256k token context window")
    update_snapshot = _snapshot(
        TG_SNAP_256K,
        TG_ITEM_256K,
        "OpenAI today announced GPT-4o's context window has been increased to 256,000 tokens.",
        datetime(2026, 8, 20, tzinfo=UTC),
    )
    result = update_graph.invoke({"item": update_item, "snapshot": update_snapshot})

    assert result["subject"] == OPENAI_GPT4O
    assert len(result["changes"]) == 1
    assert result["changes"][0].previous is not None
    assert result["changes"][0].previous.value == "128000"
    assert result["changes"][0].current.value == "256000"
    assert len(result["claims"]) == 1
    claim = result["claims"][0]
    assert "256000" in claim.text
    assert claim.validation_status == "supported"
    assert set(claim.citation_snapshot_ids) == {TG_SNAP_LAUNCH, TG_SNAP_256K}


def test_change_confidence_reflects_the_extracted_facts_confidence() -> None:
    """Per review: compare() previously never passed
    confidence=fact.confidence to store.update_fact(), so every real
    Change silently got update_fact()'s default confidence=1.0
    regardless of the actual extraction confidence."""
    store = FactStore()
    store.register_subject(OPENAI_GPT4O)
    change_set_ids: dict[Subject, uuid.UUID] = {}
    launch_graph = build_graph(
        store,
        change_set_ids,
        batch_detected_at=TG_DETECTED_AT,
        alias_table=[],
        extract_call_fn=_extraction_fake(
            "context_window_tokens", "128000", "128,000 token context window", confidence=0.95
        ),
    )
    launch_graph.invoke(
        {
            "item": _item(TG_ITEM_LAUNCH, "Introducing GPT-4o"),
            "snapshot": _snapshot(
                TG_SNAP_LAUNCH,
                TG_ITEM_LAUNCH,
                "OpenAI is launching GPT-4o with a 128,000 token context window.",
                datetime(2026, 6, 2, tzinfo=UTC),
            ),
        }
    )

    update_graph = build_graph(
        store,
        change_set_ids,
        batch_detected_at=TG_DETECTED_AT,
        alias_table=[],
        extract_call_fn=_extraction_fake(
            "context_window_tokens", "256000", "increased to 256,000 tokens", confidence=0.72
        ),
    )
    result = update_graph.invoke(
        {
            "item": _item(TG_ITEM_256K, "GPT-4o now supports a 256k token context window"),
            "snapshot": _snapshot(
                TG_SNAP_256K,
                TG_ITEM_256K,
                "OpenAI today announced GPT-4o's context window has been increased to 256,000 "
                "tokens.",
                datetime(2026, 8, 20, tzinfo=UTC),
            ),
        }
    )

    assert len(result["changes"]) == 1
    assert result["changes"][0].confidence == 0.72  # not the update_fact() default of 1.0


def test_subject_is_registered_even_when_no_facts_are_accepted() -> None:
    """Per review: register_subject() was previously only called
    implicitly inside FactStore.update_fact(), so a subject that
    resolves successfully but ends up with zero accepted facts (e.g.
    everything the model reported failed a grounding check) never got
    registered -- known_subjects() would silently omit a subject the
    pipeline actually saw and resolved. Uses a subject known only via
    the alias table (never previously registered in the store) so
    registration during this graph run is the only thing that could add
    it -- unlike the other tests here, which pre-register their subject
    in the store before invoking, which would mask this exact bug."""
    store = FactStore()
    mistral_le_chat = Subject(company="Mistral", product="Le Chat")
    alias_table = [SubjectAlias(subject=mistral_le_chat, aliases=["le chat"])]

    graph = build_graph(
        store,
        {},
        batch_detected_at=TG_DETECTED_AT,
        alias_table=alias_table,
        extract_call_fn=lambda system, prompt: FactExtractionResponse(facts=[]),
    )
    item = _item(TG_ITEM_LECHAT, "Le Chat gets an update", canonical_url="https://mistral.ai/a")
    snapshot = _snapshot(
        TG_SNAP_LECHAT,
        TG_ITEM_LECHAT,
        "Mistral's Le Chat received a minor update today.",
        datetime(2026, 8, 20, tzinfo=UTC),
    )
    result = graph.invoke({"item": item, "snapshot": snapshot})

    assert result["subject"] == mistral_le_chat
    assert result["facts"] == []
    assert mistral_le_chat in store.known_subjects()


def test_llm_fallback_rejects_high_confidence_match_with_unsupported_evidence() -> None:
    """Routing/plumbing test for the LLM fallback path, using the outcome
    that's actually reachable once the grounding check is in place: a
    high-confidence, catalogue-valid proposal with no textual evidence
    naming the specific product is routed to the LLM (true residue, no
    deterministic phrase match at all) and then rejected there, never
    resolved. See test_resolve_llm.py for direct unit coverage of the
    accept path itself, and resolve.py's module docstring for why a
    genuinely *grounded* existing-subject match can never legitimately
    reach this residue in the first place -- deterministic matching
    already scans the same text with the same phrase rule, so if the
    evidence were real, it would have matched there first."""
    store = FactStore()
    store.register_subject(OPENAI_GPT4O)  # known, but not findable by phrase match below

    def resolve_fake(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(
            company="OpenAI",
            product="GPT-4o",
            supporting_quote="OpenAI released a new research paper today",
            confidence=0.9,
        )

    graph = build_graph(
        store,
        {},
        batch_detected_at=TG_DETECTED_AT,
        alias_table=[],
        resolve_llm_call_fn=resolve_fake,
        extract_call_fn=_extraction_fake("benchmark_scores", "71.2", "scored 71.2"),
    )
    item = _item(TG_ITEM_AMBIGUOUS, "New model scores 71.2 on ReasonBench")
    snapshot = _snapshot(
        TG_SNAP_AMB,
        TG_ITEM_AMBIGUOUS,
        "OpenAI released a new research paper today.",
        datetime(2026, 8, 19, tzinfo=UTC),
    )
    result = graph.invoke({"item": item, "snapshot": snapshot})

    assert result["subject"] is None
    assert result["resolution"].method == "llm_unsupported_subject_evidence"
    assert result.get("facts", []) == []
    assert result.get("claims", []) == []


def test_multi_subject_item_never_reaches_the_llm_fallback() -> None:
    """Orchestration regression: an item deterministically phrase-matching
    two tracked subjects (Codex and ChatGPT) must end the run right after
    classify_deterministic -- route_after_classify must never route it to
    classify_llm. Asserted here by making the injected resolve_llm_call_fn
    raise if it's ever invoked at all, not just by checking the final
    subject/method -- a bug that routed to the LLM but then happened to
    reject its answer would otherwise pass silently."""
    store = FactStore()
    chatgpt = Subject(company="OpenAI", product="ChatGPT")
    codex = Subject(company="OpenAI", product="Codex")
    alias_table = [
        SubjectAlias(subject=chatgpt, aliases=[]),
        SubjectAlias(subject=codex, aliases=[]),
    ]

    def resolve_llm_must_not_be_called(system: str, prompt: str) -> ResolveLLMResponse:
        raise AssertionError(
            "resolve_via_llm must never be called for an ambiguous_multi_subject item"
        )

    graph = build_graph(
        store,
        {},
        batch_detected_at=TG_DETECTED_AT,
        alias_table=alias_table,
        resolve_llm_call_fn=resolve_llm_must_not_be_called,
    )
    item = _item(
        TG_ITEM_UNKNOWN,
        "How a researcher uses Codex and ChatGPT to search for new antimicrobial molecules",
    )
    snapshot = _snapshot(
        TG_SNAP_UNKNOWN,
        TG_ITEM_UNKNOWN,
        "The lab uses Codex and ChatGPT to search genomes for antimicrobial candidates.",
        datetime(2026, 8, 19, tzinfo=UTC),
    )
    result = graph.invoke({"item": item, "snapshot": snapshot})

    assert result["subject"] is None
    assert result["resolution"].method == "ambiguous_multi_subject"
    assert set(result["resolution"].candidate_subjects) == {chatgpt, codex}
    assert result.get("facts", []) == []
    assert result.get("claims", []) == []


def test_unresolvable_item_ends_early_with_no_facts_or_claims() -> None:
    store = FactStore()

    def resolve_fake(system: str, prompt: str) -> ResolveLLMResponse:
        return ResolveLLMResponse(confidence=0.9)  # no company/product proposed

    graph = build_graph(
        store,
        {},
        batch_detected_at=TG_DETECTED_AT,
        alias_table=[],
        resolve_llm_call_fn=resolve_fake,
    )
    item = _item(TG_ITEM_UNKNOWN, "Completely unrelated headline")
    snapshot = _snapshot(
        TG_SNAP_UNKNOWN,
        TG_ITEM_UNKNOWN,
        "Nothing about a tracked subject here.",
        datetime(2026, 8, 19, tzinfo=UTC),
    )
    result = graph.invoke({"item": item, "snapshot": snapshot})

    assert result["subject"] is None
    assert result.get("facts", []) == []
    assert result.get("claims", []) == []


def test_produced_change_carries_the_injected_batch_detected_at() -> None:
    """ADR 0008 section 5.A: every Change the graph produces is stamped
    with exactly the caller-injected batch_detected_at, normalized to
    UTC with microseconds intact -- no graph node reads the wall clock."""
    store = FactStore()
    store.register_subject(OPENAI_GPT4O)
    change_set_ids: dict[Subject, uuid.UUID] = {}
    injected = datetime(2026, 8, 20, 6, 30, 0, 987654, tzinfo=UTC)

    launch_graph = build_graph(
        store,
        change_set_ids,
        batch_detected_at=injected,
        alias_table=[],
        extract_call_fn=_extraction_fake(
            "context_window_tokens", "128000", "128,000 token context window"
        ),
    )
    launch_graph.invoke(
        {
            "item": _item(TG_ITEM_LAUNCH, "Introducing GPT-4o"),
            "snapshot": _snapshot(
                TG_SNAP_LAUNCH,
                TG_ITEM_LAUNCH,
                "OpenAI is launching GPT-4o with a 128,000 token context window.",
                datetime(2026, 6, 2, tzinfo=UTC),
            ),
        }
    )

    update_graph = build_graph(
        store,
        change_set_ids,
        batch_detected_at=injected,
        alias_table=[],
        extract_call_fn=_extraction_fake("context_window_tokens", "256000", "to 256,000 tokens"),
    )
    result = update_graph.invoke(
        {
            "item": _item(TG_ITEM_256K, "GPT-4o now supports a 256k token context window"),
            "snapshot": _snapshot(
                TG_SNAP_256K,
                TG_ITEM_256K,
                "OpenAI today increased GPT-4o's context window to 256,000 tokens.",
                datetime(2026, 8, 20, tzinfo=UTC),
            ),
        }
    )

    assert len(result["changes"]) == 1
    assert result["changes"][0].detected_at == injected
    assert result["changes"][0].detected_at.microsecond == 987654
