"""Evaluation harness — the four scored metrics from the original project
design: citation validity, unsupported-claim count, duplicate rate,
change recall. Reused across prompt/logic changes so they're comparable
over time. Run against a frozen test set — see run_eval()'s docstring
for what "frozen" means here and its current limits.

Never edit the test set to make a score look better; fix the code or
prompt instead (see intelligence/CLAUDE.md's testing rules).
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_daily_digest.intelligence.loaders import FixtureLoader, find_repo_root
from ai_daily_digest.intelligence.validate import validate_claim
from ai_daily_digest.shared.schemas import Change, ClaimValidationStatus, Digest
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
    citation_validity: float
    unsupported_claims: int
    duplicate_rate: float
    change_recall: float

    def as_table_row(self, label: str) -> str:
        """label identifies the run (e.g. a prompt version, or
        "self-check") so consecutive `make eval` rows in
        docs/eval_results.md stay distinguishable at a glance."""
        return (
            f"| {label} | {self.citation_validity:.0%} | {self.unsupported_claims} | "
            f"{self.duplicate_rate:.0%} | {self.change_recall:.0%} |"
        )


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

    NOTE on "frozen test set": there is no real held-out gold test set
    yet — that needs the team's real tests/fixtures/contracts/ pack
    (Milestone 0, not built) and a live pipeline run through
    intelligence/graph.py against it (needs a real ANTHROPIC_API_KEY and
    a deliberate decision to spend on it, not something to do silently).
    Until then, `main()` below runs a self-check: it scores the current
    draft fixture pack against itself, which proves the metrics work
    correctly but is not real evaluation signal — a self-check trivially
    scores close to 100%.
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


def main() -> None:
    """`make eval` entrypoint. See run_eval()'s docstring — this is
    currently a self-check against the draft fixture pack, not a real
    evaluation of pipeline output. Prints a table and appends a labeled,
    timestamped row to docs/eval_results.md."""
    loader = FixtureLoader()
    snapshots = loader.load_snapshots()
    change_sets = loader.load_change_sets()
    digests = loader.load_digests()

    known_snapshot_ids = {s.id for s in snapshots}
    snapshot_resolver = InMemorySnapshotResolver({s.id: s for s in snapshots})
    changes = [change for cs in change_sets for change in cs.changes]
    if not digests:
        # No artificial placeholder date -- a `Digest.digest_date` is a real
        # `datetime.date` now (ADR 0008 section 5.B), and inventing one here
        # would put a meaningless business date into the eval report. The
        # self-check genuinely needs a digest to score; an empty pack is a
        # broken pack, so say so and stop.
        raise RuntimeError(
            f"contract fixture pack has no digests ({loader.fixtures_dir / 'digests.json'}); "
            "the eval self-check needs at least one digest to score -- repopulate the "
            "fixture pack (see tests/fixtures/contracts/README.md)"
        )
    digest = digests[0]

    result = run_eval(
        digest, changes, changes, known_snapshot_ids, snapshot_resolver=snapshot_resolver
    )

    print("| Run | Citation validity | Unsupported claims | Duplicate rate | Change recall |")
    print("|---|---|---|---|---|")
    print(result.as_table_row("self-check (fixture pack vs. itself)"))

    timestamp = datetime.now(UTC).isoformat()
    row = (
        f"| {timestamp} | self-check | {result.citation_validity:.0%} | "
        f"{result.unsupported_claims} | {result.duplicate_rate:.0%} | "
        f"{result.change_recall:.0%} |\n"
    )
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


if __name__ == "__main__":
    main()
