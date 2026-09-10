"""Tests for the shared retry/validation wrapper every LLM call site
depends on. Every other test file that touches this behavior explicitly
defers coverage here (see e.g. test_resolve_llm.py's module docstring) --
this is that file. `_client()` is monkeypatched with a fake Anthropic
client so nothing here needs a real API key or network access.

The fake updated to implement .parse() returning an object shaped like the
real SDK response (.parsed_output, .stop_reason, .stop_details) instead of
only .create() -- call_structured() was rewritten to use .parse() exclusively
after the Day-7 staging rehearsal confirmed that .create()+json.loads() fails
on any non-pure-JSON response (prose wrapper, markdown fence, empty text).
"""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from ai_daily_digest.intelligence import llm


class _Response(BaseModel):
    value: str


# ---------------------------------------------------------------------------
# SDK response fakes
# ---------------------------------------------------------------------------


class _FakeStopDetails:
    """Shaped like anthropic.types.RefusalStopDetails."""

    def __init__(self, category: str | None = "general_harms") -> None:
        self.category = category
        self.explanation: str | None = None  # never logged; tested for non-exposure


class _FakeParseResponse:
    """Shaped like anthropic.types.ParsedMessage[T].

    .parsed_output  -- the already-validated Pydantic model instance, or None
    .stop_reason    -- "end_turn", "refusal", "max_tokens", etc.
    .stop_details   -- Optional[_FakeStopDetails] (only set when stop_reason=="refusal")
    """

    def __init__(
        self,
        parsed_output: BaseModel | None,
        stop_reason: str = "end_turn",
        stop_details: _FakeStopDetails | None = None,
    ) -> None:
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class _FakeMessages:
    """Queues up canned _FakeParseResponse or Exception objects, one per call to .parse()."""

    def __init__(self, responses: list[_FakeParseResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def parse(self, **_kwargs: object) -> _FakeParseResponse:
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, responses: list[_FakeParseResponse | Exception]) -> None:
        self.messages = _FakeMessages(responses)


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_FakeParseResponse | Exception],
) -> _FakeClient:
    fake = _FakeClient(responses)
    monkeypatch.setattr(llm, "_client", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Helper to build a successful parse response
# ---------------------------------------------------------------------------


def _ok(value: str = "ok") -> _FakeParseResponse:
    return _FakeParseResponse(parsed_output=_Response(value=value))


def _empty() -> _FakeParseResponse:
    return _FakeParseResponse(parsed_output=None, stop_reason="end_turn")


def _refusal(category: str = "general_harms") -> _FakeParseResponse:
    return _FakeParseResponse(
        parsed_output=None,
        stop_reason="refusal",
        stop_details=_FakeStopDetails(category=category),
    )


# ---------------------------------------------------------------------------
# API error fakes
# ---------------------------------------------------------------------------


class _FakeAPIError(Exception):
    """Base for fake Anthropic API errors."""


class _FakeRateLimitError(_FakeAPIError):
    pass


class _FakeAPIConnectionError(_FakeAPIError):
    pass


class _FakeAPIStatusError(_FakeAPIError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _ErrorMessages:
    """Raises a canned exception on .parse() calls."""

    def __init__(self, exceptions: list[Exception]) -> None:
        self._exceptions = list(exceptions)
        self.calls = 0

    def parse(self, **_kwargs: object) -> _FakeParseResponse:
        self.calls += 1
        raise self._exceptions.pop(0)


class _ErrorClient:
    def __init__(self, exceptions: list[Exception]) -> None:
        self.messages = _ErrorMessages(exceptions)


def _patch_error_client(
    monkeypatch: pytest.MonkeyPatch, exceptions: list[Exception]
) -> _ErrorClient:
    fake = _ErrorClient(exceptions)
    monkeypatch.setattr(llm, "_client", lambda: fake)
    # Also patch the error types so the except branches resolve correctly
    import anthropic as _ant

    monkeypatch.setattr(_ant, "RateLimitError", _FakeRateLimitError)
    monkeypatch.setattr(_ant, "APIConnectionError", _FakeAPIConnectionError)
    monkeypatch.setattr(_ant, "APIStatusError", _FakeAPIStatusError)
    return fake


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_succeeds_on_first_valid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(monkeypatch, [_ok("ok")])
    result = llm.call_structured(
        model=llm.HAIKU, system="sys", prompt="p", response_model=_Response
    )
    assert result.value == "ok"
    assert fake.messages.calls == 1


# ---------------------------------------------------------------------------
# Empty output (parsed_output is None)
# ---------------------------------------------------------------------------


def test_retries_once_on_empty_output_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(monkeypatch, [_empty(), _ok()])
    result = llm.call_structured(
        model=llm.HAIKU, system="sys", prompt="p", response_model=_Response
    )
    assert result.value == "ok"
    assert fake.messages.calls == 2


def test_fails_closed_after_two_empty_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(monkeypatch, [_empty(), _empty()])
    with pytest.raises(llm.StructuredCallFailedError):
        llm.call_structured(model=llm.HAIKU, system="sys", prompt="p", response_model=_Response)
    assert fake.messages.calls == 2


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


def test_retries_once_on_refusal_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(monkeypatch, [_refusal(), _ok()])
    result = llm.call_structured(
        model=llm.HAIKU, system="sys", prompt="p", response_model=_Response
    )
    assert result.value == "ok"
    assert fake.messages.calls == 2


def test_fails_closed_after_two_refusals(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_client(monkeypatch, [_refusal(), _refusal()])
    with pytest.raises(llm.StructuredCallFailedError):
        llm.call_structured(model=llm.HAIKU, system="sys", prompt="p", response_model=_Response)
    assert fake.messages.calls == 2


def test_refusal_log_does_not_contain_explanation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """stop_details.explanation may echo prompt content — must never appear in logs."""
    # Inject an explanation to confirm it's suppressed
    details = _FakeStopDetails(category="general_harms")
    details.explanation = "SECRET-EXPLANATION-TEXT"
    refusal_response = _FakeParseResponse(
        parsed_output=None, stop_reason="refusal", stop_details=details
    )
    _patch_client(monkeypatch, [refusal_response, _ok()])
    with caplog.at_level(logging.WARNING):
        llm.call_structured(model=llm.HAIKU, system="sys", prompt="p", response_model=_Response)
    assert "SECRET-EXPLANATION-TEXT" not in caplog.text
    assert "general_harms" in caplog.text  # category IS safe to log


# ---------------------------------------------------------------------------
# Pydantic validation errors (ValidationError raised by messages.parse())
# ---------------------------------------------------------------------------


def _make_json_invalid_error() -> ValidationError:
    try:
        TypeAdapter(_Response).validate_json("not valid json at all {")
    except ValidationError as err:
        return err
    raise AssertionError("unreachable")


def _make_schema_invalid_error(sensitive_payload: str = "") -> ValidationError:
    try:
        TypeAdapter(_Response).validate_json(f'{{"wrong_key": "{sensitive_payload}"}}')
    except ValidationError as err:
        return err
    raise AssertionError("unreachable")


def test_validation_error_json_invalid_retries_once_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When messages.parse() raises a JSON-invalid ValidationError on attempt 1,
    call_structured retries once with the feedback prompt and succeeds on attempt 2."""
    fake = _patch_client(monkeypatch, [_make_json_invalid_error(), _ok("recovered")])
    result = llm.call_structured(
        model=llm.HAIKU, system="sys", prompt="p", response_model=_Response
    )
    assert result.value == "recovered"
    assert fake.messages.calls == 2


def test_validation_error_schema_invalid_retries_once_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When messages.parse() raises a schema ValidationError on attempt 1,
    call_structured retries once and succeeds on attempt 2."""
    fake = _patch_client(monkeypatch, [_make_schema_invalid_error(), _ok("recovered")])
    result = llm.call_structured(
        model=llm.HAIKU, system="sys", prompt="p", response_model=_Response
    )
    assert result.value == "recovered"
    assert fake.messages.calls == 2


def test_validation_error_twice_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two consecutive ValidationErrors from messages.parse() fail closed."""
    fake = _patch_client(monkeypatch, [_make_schema_invalid_error(), _make_schema_invalid_error()])
    with pytest.raises(llm.StructuredCallFailedError, match="Validation failed twice"):
        llm.call_structured(model=llm.HAIKU, system="sys", prompt="p", response_model=_Response)
    assert fake.messages.calls == 2


def test_validation_error_log_does_not_leak_raw_input_or_str_exc(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Per security review: ValidationError.__str__() embeds raw invalid inputs verbatim.
    call_structured must log only sanitized errors without str(exc) or invalid inputs."""
    sensitive_text = "SECRET-ARTICLE-TEXT-12345"
    error = _make_schema_invalid_error(sensitive_payload=sensitive_text)
    _patch_client(monkeypatch, [error, _ok()])

    with caplog.at_level(logging.WARNING):
        result = llm.call_structured(
            model=llm.HAIKU, system="sys", prompt="p", response_model=_Response
        )

    assert result.value == "ok"
    assert "llm_validation_failed" in caplog.text
    assert sensitive_text not in caplog.text


# ---------------------------------------------------------------------------
# API errors
# ---------------------------------------------------------------------------


def test_rate_limit_error_retries_once_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First call raises RateLimitError, second succeeds
    class _BothMessages:
        def __init__(self) -> None:
            self.calls = 0

        def parse(self, **_kwargs: object) -> _FakeParseResponse:
            self.calls += 1
            if self.calls == 1:
                raise _FakeRateLimitError("429")
            return _ok()

    import anthropic as _ant

    fake_client = type("C", (), {"messages": _BothMessages()})()
    monkeypatch.setattr(llm, "_client", lambda: fake_client)
    monkeypatch.setattr(_ant, "RateLimitError", _FakeRateLimitError)
    monkeypatch.setattr(_ant, "APIConnectionError", _FakeAPIConnectionError)
    monkeypatch.setattr(_ant, "APIStatusError", _FakeAPIStatusError)

    result = llm.call_structured(
        model=llm.HAIKU, system="sys", prompt="p", response_model=_Response
    )
    assert result.value == "ok"
    assert fake_client.messages.calls == 2


def test_rate_limit_error_twice_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_error_client(
        monkeypatch, [_FakeRateLimitError("429"), _FakeRateLimitError("429")]
    )
    with pytest.raises(llm.StructuredCallFailedError):
        llm.call_structured(model=llm.HAIKU, system="sys", prompt="p", response_model=_Response)
    assert fake.messages.calls == 2


def test_api_connection_error_retries_once_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BothMessages:
        def __init__(self) -> None:
            self.calls = 0

        def parse(self, **_kwargs: object) -> _FakeParseResponse:
            self.calls += 1
            if self.calls == 1:
                raise _FakeAPIConnectionError("network error")
            return _ok()

    import anthropic as _ant

    fake_client = type("C", (), {"messages": _BothMessages()})()
    monkeypatch.setattr(llm, "_client", lambda: fake_client)
    monkeypatch.setattr(_ant, "RateLimitError", _FakeRateLimitError)
    monkeypatch.setattr(_ant, "APIConnectionError", _FakeAPIConnectionError)
    monkeypatch.setattr(_ant, "APIStatusError", _FakeAPIStatusError)

    result = llm.call_structured(
        model=llm.HAIKU, system="sys", prompt="p", response_model=_Response
    )
    assert result.value == "ok"
    assert fake_client.messages.calls == 2


def test_api_status_500_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BothMessages:
        def __init__(self) -> None:
            self.calls = 0

        def parse(self, **_kwargs: object) -> _FakeParseResponse:
            self.calls += 1
            if self.calls == 1:
                raise _FakeAPIStatusError(500)
            return _ok()

    import anthropic as _ant

    fake_client = type("C", (), {"messages": _BothMessages()})()
    monkeypatch.setattr(llm, "_client", lambda: fake_client)
    monkeypatch.setattr(_ant, "RateLimitError", _FakeRateLimitError)
    monkeypatch.setattr(_ant, "APIConnectionError", _FakeAPIConnectionError)
    monkeypatch.setattr(_ant, "APIStatusError", _FakeAPIStatusError)

    result = llm.call_structured(
        model=llm.HAIKU, system="sys", prompt="p", response_model=_Response
    )
    assert result.value == "ok"


def test_api_status_4xx_fails_closed_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx (not 429) is a client-error — must fail immediately, no retry consumed."""
    fake = _patch_error_client(monkeypatch, [_FakeAPIStatusError(403)])
    with pytest.raises(llm.StructuredCallFailedError):
        llm.call_structured(model=llm.HAIKU, system="sys", prompt="p", response_model=_Response)
    # Only 1 call: fail-closed immediately, did not retry
    assert fake.messages.calls == 1


def test_api_status_400_fails_closed_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_error_client(monkeypatch, [_FakeAPIStatusError(400)])
    with pytest.raises(llm.StructuredCallFailedError):
        llm.call_structured(model=llm.HAIKU, system="sys", prompt="p", response_model=_Response)
    assert fake.messages.calls == 1


# ---------------------------------------------------------------------------
# No raw response content ever logged
# ---------------------------------------------------------------------------


def test_empty_output_log_does_not_contain_response_content(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """None parsed_output — log must contain no model-response content."""
    _patch_client(monkeypatch, [_empty(), _ok()])
    with caplog.at_level(logging.WARNING):
        llm.call_structured(model=llm.HAIKU, system="sys", prompt="p", response_model=_Response)
    # Only safe structural fields (attempt, model) may appear — no content
    assert "attempt=0" in caplog.text
    assert "llm_empty_output" in caplog.text


# ---------------------------------------------------------------------------
# Infrastructure / misc (preserved from original test suite)
# ---------------------------------------------------------------------------


def test_missing_api_key_raises_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm._client.cache_clear()  # a client cached by an earlier test must not mask this
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        llm._client()


def test_client_is_cached_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per review: _client() previously built a new anthropic.Anthropic()
    (and its own connection pool) on every call_structured() call.
    Verified via object identity, not just "it doesn't raise" -- a bug
    here wouldn't otherwise be observable from the outside."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    llm._client.cache_clear()
    try:
        first = llm._client()
        second = llm._client()
        assert first is second
    finally:
        llm._client.cache_clear()  # don't leak a cached client into other tests
