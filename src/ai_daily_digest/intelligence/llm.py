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

from pydantic import BaseModel

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
    the parsed Pydantic model directly via response.parsed_output, so the
    only failure mode this function needs to handle is a None parsed_output
    (model produced no parseable output), a refusal (stop_reason=="refusal"),
    or a Pydantic ValidationError if .parse() surfaces one.

    API error handling (new — previously absent from call_structured):
      - RateLimitError (429): retried once on attempt 0, fails closed on
        attempt 1. Rationale: a transient quota burst often clears within
        seconds; a single retry is a cheap safeguard. Two consecutive 429s
        most likely indicate a sustained quota problem that cannot be
        resolved by retrying more aggressively here.
      - APIConnectionError: retried once (same reasoning as RateLimitError —
        transient network hiccup). Two failures fail closed.
      - APIStatusError >= 500 (InternalServerError, OverloadedError, etc.):
        retried once. Server errors are inherently transient and a single
        retry is appropriate.
      - APIStatusError 4xx (other than 429, which is caught first as
        RateLimitError): fails closed immediately without consuming a retry.
        A 4xx (bad request, auth failure, invalid model, etc.) indicates a
        caller-side configuration error that will not be fixed by retrying.

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
        except _anthropic.RateLimitError as exc:
            # 429 — transient quota burst; retry once, then fail closed.
            logger.warning(
                "llm_api_error attempt=%s error_type=RateLimitError model=%s",
                attempt,
                model,
            )
            if attempt == 0:
                continue
            raise StructuredCallFailedError(
                f"Model returned RateLimitError twice for {response_model.__name__}."
            ) from exc
        except _anthropic.APIConnectionError as exc:
            # Transient network error; retry once, then fail closed.
            logger.warning(
                "llm_api_error attempt=%s error_type=APIConnectionError model=%s",
                attempt,
                model,
            )
            if attempt == 0:
                continue
            raise StructuredCallFailedError(
                f"APIConnectionError persisted after retry for {response_model.__name__}."
            ) from exc
        except _anthropic.APIStatusError as exc:
            # 4xx (not 429, which is caught above as RateLimitError) → fail
            # closed immediately: caller-side configuration error that a
            # retry cannot fix. 5xx → retry once (transient server fault).
            if exc.status_code is not None and exc.status_code >= 500:
                logger.warning(
                    "llm_api_error attempt=%s error_type=APIStatusError status=%s model=%s",
                    attempt,
                    exc.status_code,
                    model,
                )
                if attempt == 0:
                    continue
                raise StructuredCallFailedError(
                    f"APIStatusError {exc.status_code} persisted after retry "
                    f"for {response_model.__name__}."
                ) from exc
            # 4xx other than 429 — fail closed immediately, no retry.
            logger.error(
                "llm_api_error attempt=%s error_type=APIStatusError status=%s model=%s "
                "(client-side error, not retrying)",
                attempt,
                exc.status_code,
                model,
            )
            raise StructuredCallFailedError(
                f"APIStatusError {exc.status_code} (client error) for {response_model.__name__}."
            ) from exc

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
            raise StructuredCallFailedError(f"Model refused twice for {response_model.__name__}.")

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
            )

        # parsed_output is set and is already a validated instance of
        # response_model (client.messages.parse() applies Pydantic
        # validation internally). If the SDK surfaces a ValidationError
        # (empirically confirmed: it does NOT re-raise one — it returns
        # parsed_output=None instead, caught above), that path is still
        # safe: None triggers the empty-output retry above.
        return parsed

    # Unreachable in normal control flow (the loop always returns or raises),
    # but keeps mypy and static analysis happy without a bare `return None`.
    raise StructuredCallFailedError(
        f"Model failed to produce valid {response_model.__name__} after 2 attempts."
    )
