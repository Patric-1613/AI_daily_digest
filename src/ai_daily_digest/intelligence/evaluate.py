"""Evaluation harness — the four scored metrics from the original project
design: citation validity, unsupported-claim count, duplicate rate,
change recall. Reused across prompt/logic changes so they're comparable
over time. Run against a frozen test set — see run_eval()'s docstring
for what "frozen" means here and its current limits.

Never edit the test set to make a score look better; fix the code or
prompt instead (see intelligence/CLAUDE.md's testing rules).
"""

from __future__ import annotations

import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_daily_digest.intelligence.facts import FactStore
from ai_daily_digest.intelligence.loaders import FixtureLoader, find_repo_root
from ai_daily_digest.intelligence.validate import validate_claim
from ai_daily_digest.shared.ids import new_id
from ai_daily_digest.shared.schemas import (
    Change,
    ClaimValidationStatus,
    Digest,
    ExtractedFact,
    ExtractionMethod,
    Subject,
)
from ai_daily_digest.shared.snapshot_resolver import InMemorySnapshotResolver, SnapshotResolver

# CWD-rooted, not __file__-rooted -- see loaders.py::find_repo_root's
# docstring for why (this had the exact same non-editable-install bug
# FixtureLoader did, for the same reason: __file__ lives in
# site-packages under a non-editable install, nowhere near docs/).
RESULTS_FILE = find_repo_root(Path.cwd()) / "docs" / "eval_results.md"


def citation_validity(
    digest: Digest,
    known_snapshot_ids: set[uuid.UUID],
    *,
    snapshot_resolver: SnapshotResolver | None = None,
) -> float:
    """Share of claims that validate_claim() marks "supported" -- reuses
    the real production check (citation existence, plus content
    grounding when `snapshot_resolver` is supplied) rather than
    re-implementing an existence-only approximation of it, which
    previously let this metric read 100% on claims the real gate would
    have rejected. 1.0 for an empty digest — vacuously true, there are no
    unsupported claims because there are no claims."""
    if not digest.claims:
        return 1.0
    valid = sum(
        1
        for c in digest.claims
        if validate_claim(
            c, known_snapshot_ids, snapshot_resolver=snapshot_resolver
        ).validation_status
        == ClaimValidationStatus.SUPPORTED
    )
    return valid / len(digest.claims)


def unsupported_claim_count(
    digest: Digest,
    known_snapshot_ids: set[uuid.UUID],
    *,
    snapshot_resolver: SnapshotResolver | None = None,
) -> int:
    """The target is zero — this is the number that goes in the report,
    per the original project design. Same real-check reuse as
    citation_validity() above."""
    return sum(
        1
        for c in digest.claims
        if validate_claim(
            c, known_snapshot_ids, snapshot_resolver=snapshot_resolver
        ).validation_status
        != ClaimValidationStatus.SUPPORTED
    )


def _normalise_claim_text(text: str) -> str:
    return " ".join(text.lower().split())


def duplicate_rate(digest: Digest) -> float:
    """Share of claims that repeat an earlier claim's text (normalised)
    in the same digest — the first occurrence isn't a duplicate, only
    the repeats are. 0.0 for an empty digest."""
    if not digest.claims:
        return 0.0
    seen: Counter[str] = Counter()
    duplicates = 0
    for claim in digest.claims:
        key = _normalise_claim_text(claim.text)
        if seen[key] > 0:
            duplicates += 1
        seen[key] += 1
    return duplicates / len(digest.claims)


def _change_key(change: Change) -> tuple[str, str, str]:
    return (change.subject.company, change.subject.product, change.field)


def change_recall(detected_changes: list[Change], expected_changes: list[Change]) -> float:
    """Share of expected (subject, field) changes that show up among the
    detected changes. 1.0 when nothing was expected."""
    if not expected_changes:
        return 1.0
    detected_keys = {_change_key(c) for c in detected_changes}
    expected_keys = {_change_key(c) for c in expected_changes}
    return len(expected_keys & detected_keys) / len(expected_keys)


@dataclass
class EvalResult:
    citation_validity: float | None = None
    unsupported_claims: int | None = None
    duplicate_rate: float | None = None
    change_recall: float | None = None

    def as_table_row(self, label: str) -> str:
        """label identifies the run (e.g. a prompt version, or
        "self-check") so consecutive `make eval` rows in
        docs/eval_results.md stay distinguishable at a glance."""
        cit = f"{self.citation_validity:.0%}" if self.citation_validity is not None else "N/A"
        unsup = str(self.unsupported_claims) if self.unsupported_claims is not None else "N/A"
        dup = f"{self.duplicate_rate:.0%}" if self.duplicate_rate is not None else "N/A"
        rec = f"{self.change_recall:.0%}" if self.change_recall is not None else "N/A"
        return f"| {label} | {cit} | {unsup} | {dup} | {rec} |"


def run_eval(
    digest: Digest,
    detected_changes: list[Change],
    expected_changes: list[Change],
    known_snapshot_ids: set[uuid.UUID],
    *,
    snapshot_resolver: SnapshotResolver | None = None,
) -> EvalResult:
    """digest/detected_changes: what the pipeline actually produced.
    expected_changes: the gold reference for this test set.
    snapshot_resolver: passed straight through to citation_validity()/
    unsupported_claim_count() for the content-grounding check.

    Callers must keep detected_changes and expected_changes genuinely
    independent -- e.g. never derive both from the same collection (see
    _change_detection_case()'s docstring for the concrete bug this class
    of mistake caused, caught in review on PR #99, 2026-09-09).

    NOTE on evaluation against the Milestone-0 fixture pack:
    evaluate_fixture_pack() scores citation_validity/unsupported_claims/
    duplicate_rate against the fixture pack's real recorded digest (change_recall
    is excluded, rendered as N/A). change_recall is scored separately via
    evaluate_change_detection_case() as a dedicated synthetic smoke test.
    The self-check path (run_self_check) remains available as a plumbing test.
    """
    return EvalResult(
        citation_validity=citation_validity(
            digest, known_snapshot_ids, snapshot_resolver=snapshot_resolver
        ),
        unsupported_claims=unsupported_claim_count(
            digest, known_snapshot_ids, snapshot_resolver=snapshot_resolver
        ),
        duplicate_rate=duplicate_rate(digest),
        change_recall=change_recall(detected_changes, expected_changes),
    )


_CD_DETECTED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_CD_OPENAI_GPT4O = Subject(company="OpenAI", product="GPT-4o")
_CD_ANTHROPIC_CLAUDE = Subject(company="Anthropic", product="Claude")


def _cd_fact(field: str, value: str) -> ExtractedFact:
    """One synthetic ExtractedFact for evaluate_change_detection_case() below --
    a fresh snapshot_id/fact id per call, real ADR-0004-required evidence
    fields populated same as production LLM extraction would."""
    return ExtractedFact(
        id=new_id(),
        snapshot_id=new_id(),
        field=field,
        value=value,
        extraction_method=ExtractionMethod.LLM_STRUCTURED_OUTPUT,
        extraction_model="claude-sonnet-5",
        prompt_version="eval-fixture-case-v1",
        quoted_span=f"synthetic eval fixture quote containing {value}",
        confidence=0.9,
    )


@dataclass(frozen=True)
class ChangeDetectionFactInput:
    """One fact observation input in the synthetic change-detection smoke test."""

    subject: Subject
    field: str
    value: str
    source_url: str
    observed_at: datetime
    in_detected: bool = True


DEFAULT_CHANGE_DETECTION_INPUTS: tuple[ChangeDetectionFactInput, ...] = (
    ChangeDetectionFactInput(
        subject=_CD_OPENAI_GPT4O,
        field="context_window_tokens",
        value="128000",
        source_url="https://openai.example.com/gpt-4o-launch",
        observed_at=datetime(2026, 6, 2, tzinfo=UTC),
        in_detected=True,
    ),
    ChangeDetectionFactInput(
        subject=_CD_OPENAI_GPT4O,
        field="context_window_tokens",
        value="256000",
        source_url="https://openai.example.com/gpt-4o-256k",
        observed_at=datetime(2026, 8, 20, tzinfo=UTC),
        in_detected=True,
    ),
    ChangeDetectionFactInput(
        subject=_CD_ANTHROPIC_CLAUDE,
        field="input_price_usd",
        value="3",
        source_url="https://anthropic.example.com/pricing",
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        in_detected=True,
    ),
    ChangeDetectionFactInput(
        subject=_CD_ANTHROPIC_CLAUDE,
        field="input_price_usd",
        value="2",
        source_url="https://anthropic.example.com/pricing-update",
        observed_at=datetime(2026, 8, 15, tzinfo=UTC),
        in_detected=False,
    ),
)


def evaluate_change_detection_case(
    inputs: Sequence[ChangeDetectionFactInput] | None = None,
) -> EvalResult:
    """Run the synthetic change-detection smoke test.

    Replays a controlled sequence of fact observations into two FactStore
    instances:
    - gold_store receives all observations (producing expected_changes).
    - detected_store receives only observations with in_detected=True (producing
      detected_changes).

    Scores change_recall in isolation and returns an EvalResult with citation/
    duplicate metrics left as None (rendered as N/A in the results table).
    """
    case_inputs = inputs if inputs is not None else DEFAULT_CHANGE_DETECTION_INPUTS
    gold_store = FactStore()
    expected_changes: list[Change] = []
    detected_store = FactStore()
    detected_changes: list[Change] = []

    for item in case_inputs:
        fact = _cd_fact(item.field, item.value)
        gold_change = gold_store.update_fact(
            item.subject,
            fact,
            source_url=item.source_url,
            observed_at=item.observed_at,
            change_set_id_factory=new_id,
            detected_at=_CD_DETECTED_AT,
        )
        if gold_change is not None:
            expected_changes.append(gold_change)

        if item.in_detected:
            det_change = detected_store.update_fact(
                item.subject,
                fact,
                source_url=item.source_url,
                observed_at=item.observed_at,
                change_set_id_factory=new_id,
                detected_at=_CD_DETECTED_AT,
            )
            if det_change is not None:
                detected_changes.append(det_change)

    return EvalResult(
        change_recall=change_recall(detected_changes, expected_changes),
    )


def evaluate_fixture_pack(loader: FixtureLoader | None = None) -> EvalResult:
    """Score the Milestone-0 fixture pack from tests/fixtures/contracts/.

    Scores citation_validity, unsupported_claims, and duplicate_rate against
    the fixture pack's real recorded digest (digests[0]) and snapshots.

    Does NOT compute change_recall (left as None, rendered as N/A in the results
    table), mirroring the precedent in evaluate_digest_run() (PR #97) of excluding
    change_recall when no gold-reference changes exist for the digest run.
    """
    if loader is None:
        loader = FixtureLoader()

    items = loader.load_items()
    snapshots = loader.load_snapshots()
    digests = loader.load_digests()

    if not digests:
        raise RuntimeError(
            f"contract fixture pack has no digests ({loader.fixtures_dir / 'digests.json'}); "
            "the eval harness needs at least one digest to score -- repopulate the "
            "fixture pack (see tests/fixtures/contracts/README.md)"
        )

    if not items or not snapshots:
        raise RuntimeError(
            f"contract fixture pack input batch is incomplete ({loader.fixtures_dir}); "
            "the eval harness needs source items and snapshots to score."
        )

    known_snapshot_ids = {s.id for s in snapshots}
    snapshot_resolver = InMemorySnapshotResolver({s.id: s for s in snapshots})
    digest = digests[0]

    return EvalResult(
        citation_validity=citation_validity(
            digest, known_snapshot_ids, snapshot_resolver=snapshot_resolver
        ),
        unsupported_claims=unsupported_claim_count(
            digest, known_snapshot_ids, snapshot_resolver=snapshot_resolver
        ),
        duplicate_rate=duplicate_rate(digest),
        change_recall=None,
    )


def run_self_check(loader: FixtureLoader | None = None) -> EvalResult:
    """Plumbing self-check: scores the draft fixture pack against itself.

    Proves the metrics harness works mechanically, scoring 100% because
    detected_changes matches expected_changes exactly.
    """
    if loader is None:
        loader = FixtureLoader()

    snapshots = loader.load_snapshots()
    change_sets = loader.load_change_sets()
    digests = loader.load_digests()

    if not digests:
        raise RuntimeError(
            f"contract fixture pack has no digests ({loader.fixtures_dir / 'digests.json'}); "
            "the eval self-check needs at least one digest to score -- repopulate the "
            "fixture pack (see tests/fixtures/contracts/README.md)"
        )

    known_snapshot_ids = {s.id for s in snapshots}
    snapshot_resolver = InMemorySnapshotResolver({s.id: s for s in snapshots})
    changes = [change for cs in change_sets for change in cs.changes]
    digest = digests[0]

    return run_eval(
        digest,
        changes,
        changes,
        known_snapshot_ids,
        snapshot_resolver=snapshot_resolver,
    )


def _append_eval_row(label: str, result: EvalResult) -> None:
    timestamp = datetime.now(UTC).isoformat()
    cit = f"{result.citation_validity:.0%}" if result.citation_validity is not None else "N/A"
    unsup = str(result.unsupported_claims) if result.unsupported_claims is not None else "N/A"
    dup = f"{result.duplicate_rate:.0%}" if result.duplicate_rate is not None else "N/A"
    rec = f"{result.change_recall:.0%}" if result.change_recall is not None else "N/A"
    row = f"| {timestamp} | {label} | {cit} | {unsup} | {dup} | {rec} |\n"

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    is_new = not RESULTS_FILE.exists()
    with RESULTS_FILE.open("a", encoding="utf-8") as f:
        if is_new:
            f.write("# Evaluation results\n\n")
            f.write(
                "Every `make eval` run appends one row here — never edit or delete "
                "past rows, only append. See intelligence/evaluate.py's run_eval() "
                'docstring for what "self-check" runs mean vs. a real evaluation.\n\n'
            )
            f.write(
                "| Timestamp (UTC) | Run | Citation validity | Unsupported claims | "
                "Duplicate rate | Change recall |\n"
            )
            f.write("|---|---|---|---|---|---|\n")
        f.write(row)


def main(argv: list[str] | None = None) -> None:
    """`make eval` entrypoint.

    By default, scores the real Milestone-0 fixture pack for citation/unsupported/
    duplicate metrics and logs as "fixture-pack", then scores the synthetic
    change-detection smoke test and logs as "change-detection-smoke-test".

    Pass `--self-check` to run the plumbing self-check instead.
    """
    args = argv if argv is not None else sys.argv[1:]
    is_self_check = "--self-check" in args

    loader = FixtureLoader()
    print("| Run | Citation validity | Unsupported claims | Duplicate rate | Change recall |")
    print("|---|---|---|---|---|")

    if is_self_check:
        result = run_self_check(loader)
        print(result.as_table_row("self-check"))
        _append_eval_row("self-check", result)
    else:
        fp_result = evaluate_fixture_pack(loader)
        print(fp_result.as_table_row("fixture-pack"))
        _append_eval_row("fixture-pack", fp_result)

        cd_result = evaluate_change_detection_case()
        print(cd_result.as_table_row("change-detection-smoke-test"))
        _append_eval_row("change-detection-smoke-test", cd_result)


if __name__ == "__main__":
    main()
