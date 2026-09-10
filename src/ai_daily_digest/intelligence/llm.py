"""Single wrapper around the Anthropic SDK for all of intelligence's direct
LLM calls. Multi-step workflows (retrieval + extraction + generation) are
expected to move to LangGraph per docs/adr — see intelligence/CLAUDE.md —
but every LangGraph node that calls the model still goes through
call_structured() here, so retries, logging, and model choice stay in one
place instead of scattered across nodes.

Requires: anthropic, pydantic>=2, python-dotenv
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    # Only for the _client() return-type annotation below -- the runtime
    # import stays inside _client() so this module remains importable
    # without the anthropic package installed (see its docstring).
    import anthropic

logger = logging.getLogger("intelligence.llm")

# Model ids — keep in sync with intelligence/CLAUDE.md's model choice table.
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"


class StructuredCallFailedError(Exception):
    """Raised when the model fails to produce schema-valid output twice.
    Callers must not silently fall back to prose — catch this and either
    skip the item (resolution) or fail the run loudly (generation)."""


@functools.lru_cache(maxsize=1)
def _client() -> anthropic.Anthropic:
    # Cached: this previously built a brand new anthropic.Anthropic()
    # (and its own connection pool) on every call_structured() call.
    # lru_cache only caches a *successful* return -- it never caches a
    # raised exception, so a missing API key still raises fresh on every
    # call rather than getting stuck behind a cached failure. The cached
    # client does persist for the process's lifetime, so it won't pick
    # up an API key that's rotated at runtime without a process restart
    # -- an accepted tradeoff of caching a singleton client, same as any
    # other cached connection/client object.
    #
    # Imported lazily so this module can be importable (e.g. by tests
    # that only check prompt files exist) without the anthropic package
    # installed.
    import anthropic  # pylint: disable=import-outside-toplevel

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — copy .env.example to .env and fill it in.")
    return anthropic.Anthropic(api_key=api_key)


def call_structured[T: BaseModel](  # pylint: disable=too-many-arguments,too-many-branches
    # All keyword-only: model/system/prompt/response_model are the call
    # itself, max_tokens is provider-call tuning -- every LLM call site in
    # intelligence/ goes through this one function (see module docstring)
    # specifically so that knob lives in one place instead of being
    # duplicated per call site.
    #
    # PEP 695 generic syntax (requires Python >=3.12, this project's
    # target -- see pyproject.toml's requires-python). An earlier version
    # of this function used `TypeVar` instead because the machine it was
    # written on only had Python 3.11 installed; verified and switched to
    # this syntax once run against a real 3.12 interpreter (`uv`'s
    # managed toolchain, via `uv run --no-editable`).
    *,
    model: str,
    system: str,
    prompt: str,
    response_model: type[T],
    max_tokens: int = 2048,
) -> T:
    """Call the model via client.messages.parse(), validate the parsed output
    against `response_model`. On failure (refusal, empty output, validation
    error), retry once. On a second failure, raise StructuredCallFailedError
    — never return unvalidated data.

    Switched from client.messages.create() + manual json.loads() to
    client.messages.parse(output_format=response_model) to eliminate the
    class of JSONDecodeError failures observed in the Day-7 staging
    rehearsal: any non-pure-JSON response (prose wrapper, markdown fence,
    empty text) caused a hard crash rather than entering the retry loop.
    client.messages.parse() handles JSON extraction internally and surfaces
    the parsed Pydantic model directly via response.parsed_output.

    Retry Policy & Error Ownership:
      - Transport / Network Errors (RateLimitError, APIConnectionError, APIStatusError):
        The Anthropic client handles transport-level retries internally with
        exponential backoff and jitter (default max_retries=2). An error escaping
        from client.messages.parse() indicates transport attempts are exhausted;
        call_structured fails closed immediately without an extra wrapper retry
        to prevent multiplying HTTP attempts.
      - Content / Semantic Errors (ValidationError, refusal, empty parsed_output):
        Handled by call_structured's 2-attempt loop. On attempt 0, a feedback
        reminder is appended to the prompt before resending to the model.

    Privacy & Exception Chaining:
      - Terminal StructuredCallFailedError exceptions are raised `from None`
        so that caller exception logging (e.g. logger.exception()) cannot
        render underlying ValidationError.__str__() or provider payloads,
        which could contain un-sanitized input text or scraped content.

    No `temperature` parameter: sampling controls (temperature/top_p/
    top_k) are removed on the current-generation models this file's
    HAIKU/SONNET/OPUS constants point at (they return 400 if sent) --
    verified against the real installed SDK, not assumed. An earlier
    version of this function accepted temperature=0.0 hoping for
    deterministic output; that call would have failed against the real
    API the first time it ran live (every test here uses an injected
    fake, so nothing caught it). Reproducibility now comes from this
    function's own schema-validation retry loop and from each call
    site's own grounding checks (extract_facts.py, compare_subjects.py,
    ...), not from a sampling knob.
    """
    import anthropic as _anthropic  # pylint: disable=import-outside-toplevel

    client = _client()

    for attempt in range(2):
        # Never log the raw prompt/system text -- repo-root AGENTS.md:
        # "Never place subscriber email addresses, credentials, or raw
        # prompts in logs." Collected page content flows into the prompt
        # (extract_facts, compare_subjects, ...), so logging it verbatim
        # would put scraped article text into whatever log retention
        # system picks this up. A short content hash is enough to
        # correlate "this attempt, this prompt" across log lines without
        # persisting the content itself.
        prompt_fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        logger.info(
            "llm_call attempt=%s model=%s prompt_chars=%s prompt_fingerprint=%s",
            attempt,
            model,
            len(prompt),
            prompt_fingerprint,
        )

        # --- API call with per-error-type handling ---
        try:
            response = client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=response_model,
            )
        except _anthropic.RateLimitError:
            # 429 — Rate limit / quota burst. SDK internal retries with backoff
            # and jitter are already exhausted; fail closed immediately.
            logger.error(
                "llm_api_error attempt=%s error_type=RateLimitError model=%s",
                attempt,
                model,
            )
            raise StructuredCallFailedError(
                f"RateLimitError (rate limit exhausted) for {response_model.__name__}."
            ) from None
        except _anthropic.APIConnectionError:
            # Network connection fault. SDK internal retries already exhausted;
            # fail closed immediately.
            logger.error(
                "llm_api_error attempt=%s error_type=APIConnectionError model=%s",
                attempt,
                model,
            )
            raise StructuredCallFailedError(
                f"APIConnectionError (network failure) for {response_model.__name__}."
            ) from None
        except _anthropic.APIStatusError as exc:
            # HTTP status error (5xx server error or 4xx client error).
            # 5xx errors were already retried internally by the SDK. Fail closed.
            logger.error(
                "llm_api_error attempt=%s error_type=APIStatusError status=%s model=%s",
                attempt,
                exc.status_code,
                model,
            )
            raise StructuredCallFailedError(
                f"APIStatusError {exc.status_code} for {response_model.__name__}."
            ) from None
        except ValidationError as exc:
            # Under anthropic==1.3.0, messages.parse() calls TypeAdapter.validate_json()
            # internally, raising ValidationError on invalid JSON or schema violations.
            # Never log str(exc) directly: ValidationError.__str__() embeds invalid
            # input values verbatim, which could leak scraped content or secrets into logs.
            # Only sanitized error location/type metadata is safe.
            safe_errors = [
                {"loc": err.get("loc"), "type": err.get("type")}
                for err in exc.errors(
                    include_input=False,
                    include_context=False,
                    include_url=False,
                )
            ]
            logger.warning(
                "llm_validation_failed attempt=%s model=%s errors=%s",
                attempt,
                model,
                safe_errors,
            )
            if attempt == 0:
                prompt = (
                    f"{prompt}\n\n"
                    f"Your previous response failed schema validation. "
                    f"Return ONLY valid JSON matching the required schema, nothing else."
                )
                continue
            raise StructuredCallFailedError(
                f"Validation failed twice for {response_model.__name__}."
            ) from None

        # --- Refusal check (before trusting parsed_output) ---
        if response.stop_reason == "refusal":
            # Log only the category (a closed enum), never the explanation
            # text -- stop_details.explanation is human-readable prose that
            # the API docs note "is not guaranteed to be stable" and may
            # echo prompt content in some refusal categories. Only the
            # category is safe to log per AGENTS.md's "never log raw
            # prompts/content" constraint.
            category = (
                response.stop_details.category if response.stop_details is not None else "unknown"
            )
            logger.warning(
                "llm_refusal attempt=%s model=%s refusal_category=%s",
                attempt,
                model,
                category,
            )
            if attempt == 0:
                prompt = (
                    f"{prompt}\n\n"
                    f"Your previous response was refused. "
                    f"Return ONLY valid JSON matching the required schema, nothing else."
                )
                continue
            raise StructuredCallFailedError(
                f"Model refused twice for {response_model.__name__}."
            ) from None

        # --- Empty / None parsed_output ---
        parsed = response.parsed_output
        if parsed is None:
            logger.warning(
                "llm_empty_output attempt=%s model=%s",
                attempt,
                model,
            )
            if attempt == 0:
                prompt = (
                    f"{prompt}\n\n"
                    f"Your previous response produced no parseable output. "
                    f"Return ONLY valid JSON matching the required schema, nothing else."
                )
                continue
            raise StructuredCallFailedError(
                f"Model produced no parseable output after 2 attempts "
                f"for {response_model.__name__}."
            ) from None

        return parsed

    # Unreachable in normal control flow (the loop always returns or raises),
    # but keeps mypy and static analysis happy without a bare `return None`.
    raise StructuredCallFailedError(
        f"Model failed to produce valid {response_model.__name__} after 2 attempts."
    ) from None
