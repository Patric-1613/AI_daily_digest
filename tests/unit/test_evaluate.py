import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from ai_daily_digest.intelligence.evaluate import (
    _CD_ANTHROPIC_CLAUDE,
    _CD_DETECTED_AT,
    _CD_OPENAI_GPT4O,
    EvalResult,
    _cd_fact,
    change_recall,
    citation_validity,
    duplicate_rate,
    evaluate_fixture_pack,
    main,
    run_eval,
    run_self_check,
    unsupported_claim_count,
)
from ai_daily_digest.intelligence.facts import FactStore
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.schemas import (
    Change,
    Digest,
    DigestClaim,
    DigestStatus,
    DocumentSnapshot,
    FactObservation,
    Subject,
)
from ai_daily_digest.shared.snapshot_resolver import InMemorySnapshotResolver
from tests.uuid_samples import (
    CHANGE_1,
    CHANGE_SET_1,
    CLAIM_1,
    CLAIM_2,
    CLAIM_3,
    DIGEST_1,
    ITEM_1,
    SNAPSHOT_1,
    SNAPSHOT_2,
    SNAPSHOT_MISSING,
)

KNOWN = {SNAPSHOT_1, SNAPSHOT_2}

# The orchestrator's injected batch detection time (ADR 0008 section 5.A) --
# .250000 microseconds on purpose, so tests can assert it survives intact.
TE_DETECTED_AT = datetime(2026, 8, 20, 12, 0, 0, 250000, tzinfo=UTC)


def _claim(text: str, citations: list[uuid.UUID], claim_id: uuid.UUID = CLAIM_1) -> DigestClaim:
    return DigestClaim(id=claim_id, text=text, citation_snapshot_ids=citations)


def _snapshot(snap_id: uuid.UUID, text: str) -> DocumentSnapshot:
    return DocumentSnapshot(
        id=snap_id,
        source_item_id=ITEM_1,
        fetched_at=datetime(2026, 8, 20, tzinfo=UTC),
        content_hash=f"sha256:{snap_id}",
        content_text=text,
    )


def _digest(claims: list[DigestClaim]) -> Digest:
    return Digest(
        id=DIGEST_1,
        digest_date=date(2026, 8, 20),
        status=DigestStatus.DRAFT,
        title="Test",
        claims=claims,
    )


def _change(company: str, product: str, field: str, snap_id: uuid.UUID = SNAPSHOT_1) -> Change:
    # change_type="disclosed" (not "changed") -- this helper never sets
    # a previous observation, and Change's own invariant validator only
    # allows previous=None for a genuine first disclosure. change_recall()
    # itself only reads (subject, field), so the exact change_type here
    # is otherwise immaterial to what these tests check.
    return Change(
        id=CHANGE_1,
        change_set_id=CHANGE_SET_1,
        subject=Subject(company=company, product=product),
        field=field,
        change_type="disclosed",
        previous=None,
        current=FactObservation(value="x", snapshot_id=snap_id),
        confidence=0.9,
        detected_at=TE_DETECTED_AT,
    )


# --- citation_validity ---


def test_citation_validity_all_supported() -> None:
    digest = _digest([_claim("A", [SNAPSHOT_1], CLAIM_1), _claim("B", [SNAPSHOT_2], CLAIM_2)])
    assert citation_validity(digest, KNOWN) == 1.0


def test_citation_validity_partial() -> None:
    digest = _digest([_claim("A", [SNAPSHOT_1], CLAIM_1), _claim("B", [SNAPSHOT_MISSING], CLAIM_2)])
    assert citation_validity(digest, KNOWN) == 0.5


def test_citation_validity_empty_digest_is_vacuously_perfect() -> None:
    assert citation_validity(_digest([]), KNOWN) == 1.0


def test_citation_validity_uses_real_content_grounding_when_available() -> None:
    """Per the second review: citation_validity() used to read 100% for
    a claim citing a real, existing snapshot id whose content had
    nothing to do with what the claim asserted -- it now reuses
    validate_claim() directly, so a citation that exists but doesn't
    ground the claim's numbers is NOT counted as valid, the same as the
    real publish-time gate would score it."""
    digest = _digest([_claim("The price increased to 999999.", [SNAPSHOT_1], CLAIM_1)])
    resolver = InMemorySnapshotResolver(
        {SNAPSHOT_1: _snapshot(SNAPSHOT_1, "The price increased to 5.")}
    )
    assert citation_validity(digest, KNOWN, snapshot_resolver=resolver) == 0.0
    assert unsupported_claim_count(digest, KNOWN, snapshot_resolver=resolver) == 1


def test_citation_validity_without_a_snapshot_resolver_stays_existence_only() -> None:
    """Omitting snapshot_resolver keeps the old, weaker existence-only
    behavior -- callers that don't have snapshot content aren't forced to
    provide one."""
    digest = _digest([_claim("The price increased to 999999.", [SNAPSHOT_1], CLAIM_1)])
    assert citation_validity(digest, KNOWN) == 1.0


# --- unsupported_claim_count ---


def test_unsupported_claim_count_counts_missing_and_empty_citations() -> None:
    digest = _digest(
        [
            _claim("A", [SNAPSHOT_1], CLAIM_1),
            _claim("B", [], CLAIM_2),
            _claim("C", [SNAPSHOT_MISSING], CLAIM_3),
        ]
    )
    assert unsupported_claim_count(digest, KNOWN) == 2


# --- duplicate_rate ---


def test_duplicate_rate_no_duplicates() -> None:
    digest = _digest(
        [_claim("Unique A", [SNAPSHOT_1], CLAIM_1), _claim("Unique B", [SNAPSHOT_1], CLAIM_2)]
    )
    assert duplicate_rate(digest) == 0.0


def test_duplicate_rate_detects_repeated_text_case_and_whitespace_insensitive() -> None:
    digest = _digest(
        [
            _claim("GPT-4o now has 256k context", [SNAPSHOT_1], CLAIM_1),
            _claim("  gpt-4o   now has 256k context  ", [SNAPSHOT_1], CLAIM_2),
            _claim("Something else entirely", [SNAPSHOT_1], CLAIM_3),
        ]
    )
    assert duplicate_rate(digest) == 1 / 3


def test_duplicate_rate_empty_digest_is_zero() -> None:
    assert duplicate_rate(_digest([])) == 0.0


# --- change_recall ---


def test_change_recall_full_when_all_expected_are_detected() -> None:
    expected = [_change("OpenAI", "GPT-4o", "context_window_tokens")]
    detected = [_change("OpenAI", "GPT-4o", "context_window_tokens")]
    assert change_recall(detected, expected) == 1.0


def test_change_recall_partial_when_some_missed() -> None:
    expected = [
        _change("OpenAI", "GPT-4o", "context_window_tokens"),
        _change("Anthropic", "Claude", "benchmark_scores"),
    ]
    detected = [_change("OpenAI", "GPT-4o", "context_window_tokens")]
    assert change_recall(detected, expected) == 0.5


def test_change_recall_zero_when_nothing_detected() -> None:
    expected = [_change("OpenAI", "GPT-4o", "context_window_tokens")]
    assert change_recall([], expected) == 0.0


def test_change_recall_vacuous_when_nothing_expected() -> None:
    assert change_recall([], []) == 1.0


# --- run_eval ---


def test_run_eval_combines_all_four_metrics() -> None:
    digest = _digest([_claim("A", [SNAPSHOT_1], CLAIM_1)])
    detected = [_change("OpenAI", "GPT-4o", "context_window_tokens")]
    expected = [_change("OpenAI", "GPT-4o", "context_window_tokens")]
    result: EvalResult = run_eval(digest, detected, expected, KNOWN)
    assert result.citation_validity == 1.0
    assert result.unsupported_claims == 0
    assert result.duplicate_rate == 0.0
    assert result.change_recall == 1.0
    assert "100%" in result.as_table_row("test")


def test_main_fails_loudly_when_the_fixture_pack_has_no_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0008 section 5.B: Digest.digest_date is a real datetime.date now,
    so the old empty-fixture fallback (Digest(digest_date="", ...)) is gone.
    An empty digest fixture pack must produce an explicit, deterministic
    failure naming the file -- never an invented business date."""
    from ai_daily_digest.intelligence.loaders import FixtureLoader

    monkeypatch.setattr(FixtureLoader, "load_digests", lambda self: [])

    with pytest.raises(RuntimeError, match=r"no digests.*digests\.json"):
        main()


def test_evaluate_fixture_pack_produces_non_trivial_scores() -> None:
    """Proves evaluate_fixture_pack() produces non-trivial (not blanket 100%)
    scores, and that change_recall is a REAL, non-circular measurement.

    citation_validity/unsupported_claims/duplicate_rate score the fixture
    pack's real recorded digest (digests[0]) -- no gold-reference issue
    there, these three only ever look at one digest's own claims.

    change_recall scores _change_detection_case()'s independently-produced
    detected vs. expected changes (see that function's docstring for why an
    earlier version of this test, comparing change_sets[0] against
    flatten(change_sets) -- the same file used as both prediction and
    truth -- was rejected in review, 2026-09-09, by both Patric-1613 and
    chamath-wijayasundara on PR #99). 1 of 2 independently-authored expected
    changes is fed into the "detected" replay, so change_recall == 0.5 by
    construction of two separate FactStore batches, not by how any one
    shared file happens to be split.
    """
    result = evaluate_fixture_pack()
    assert result.citation_validity == 1.0
    assert result.unsupported_claims == 0
    assert result.duplicate_rate == 0.0
    assert result.change_recall == pytest.approx(0.5)
    assert "50%" in result.as_table_row("fixture-pack")


def test_change_detection_case_is_order_independent() -> None:
    """Reordering which subject's fact-sequence is replayed first must not
    change the resulting change_recall -- each (subject, field) timeline is
    tracked independently by FactStore regardless of interleaving order.
    This is exactly the class of bug review caught (a score that
    accidentally depended on incidental ordering/structure of the input
    rather than on genuine detection), proven not to recur here."""
    forward_store = FactStore()
    forward_store.update_fact(
        _CD_OPENAI_GPT4O,
        _cd_fact("context_window_tokens", "128000"),
        source_url="https://openai.example.com/a",
        observed_at=datetime(2026, 6, 2, tzinfo=UTC),
        change_set_id_factory=new_id,
        detected_at=_CD_DETECTED_AT,
    )
    forward_openai_change = forward_store.update_fact(
        _CD_OPENAI_GPT4O,
        _cd_fact("context_window_tokens", "256000"),
        source_url="https://openai.example.com/b",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
        change_set_id_factory=new_id,
        detected_at=_CD_DETECTED_AT,
    )
    forward_store.update_fact(
        _CD_ANTHROPIC_CLAUDE,
        _cd_fact("input_price_usd", "3"),
        source_url="https://anthropic.example.com/a",
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        change_set_id_factory=new_id,
        detected_at=_CD_DETECTED_AT,
    )

    # Reversed order: Anthropic's fact-sequence replayed before OpenAI's.
    reversed_store = FactStore()
    reversed_store.update_fact(
        _CD_ANTHROPIC_CLAUDE,
        _cd_fact("input_price_usd", "3"),
        source_url="https://anthropic.example.com/a",
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        change_set_id_factory=new_id,
        detected_at=_CD_DETECTED_AT,
    )
    reversed_store.update_fact(
        _CD_OPENAI_GPT4O,
        _cd_fact("context_window_tokens", "128000"),
        source_url="https://openai.example.com/a",
        observed_at=datetime(2026, 6, 2, tzinfo=UTC),
        change_set_id_factory=new_id,
        detected_at=_CD_DETECTED_AT,
    )
    reversed_openai_change = reversed_store.update_fact(
        _CD_OPENAI_GPT4O,
        _cd_fact("context_window_tokens", "256000"),
        source_url="https://openai.example.com/b",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
        change_set_id_factory=new_id,
        detected_at=_CD_DETECTED_AT,
    )

    assert forward_openai_change is not None
    assert reversed_openai_change is not None

    expected = [forward_openai_change]  # only the OpenAI change is "expected" here
    assert change_recall([forward_openai_change], expected) == change_recall(
        [reversed_openai_change], expected
    )
    assert change_recall([forward_openai_change], expected) == 1.0


def test_run_self_check_preserves_plumbing_check() -> None:
    """The plumbing self-check path remains available and scores 100%."""
    result = run_self_check()
    assert result.citation_validity == 1.0
    assert result.unsupported_claims == 0
    assert result.duplicate_rate == 0.0
    assert result.change_recall == 1.0
    assert "100%" in result.as_table_row("self-check")


def test_main_runs_fixture_pack_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """main() runs against fixture pack by default and logs to docs/eval_results.md."""
    test_results_file = tmp_path / "eval_results.md"
    monkeypatch.setattr("ai_daily_digest.intelligence.evaluate.RESULTS_FILE", test_results_file)

    main([])

    assert test_results_file.exists()
    content = test_results_file.read_text(encoding="utf-8")
    assert "fixture-pack" in content
    assert "50%" in content


def test_main_supports_self_check_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """main(["--self-check"]) runs the plumbing self-check."""
    test_results_file = tmp_path / "eval_results.md"
    monkeypatch.setattr("ai_daily_digest.intelligence.evaluate.RESULTS_FILE", test_results_file)

    main(["--self-check"])

    assert test_results_file.exists()
    content = test_results_file.read_text(encoding="utf-8")
    assert "self-check" in content
    assert "100%" in content


def test_evaluate_fixture_pack_fails_when_input_batch_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evaluate_fixture_pack raises when items or snapshots are missing."""
    from ai_daily_digest.intelligence.loaders import FixtureLoader

    monkeypatch.setattr(FixtureLoader, "load_items", lambda self: [])
    with pytest.raises(RuntimeError, match=r"input batch is incomplete"):
        evaluate_fixture_pack()
