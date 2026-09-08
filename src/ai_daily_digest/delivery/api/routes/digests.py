"""Published digest feed endpoint."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from ai_daily_digest.delivery.api.dependencies import get_cursor_codec, get_digest_feed_repository
from ai_daily_digest.delivery.api.errors import ErrorEnvelope, error_response
from ai_daily_digest.delivery.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    CursorCodec,
    FilterInput,
    FilterValidationError,
    InvalidCursorError,
    Page,
    build_cursor_payload,
    canonicalize_filters,
    validate_half_open_range,
)
from ai_daily_digest.delivery.api.schemas import DigestSummary
from ai_daily_digest.shared.repositories import DigestFeedFilter, DigestFeedRepository
from ai_daily_digest.shared.schemas import DigestStatus

DIGESTS_RESOURCE = "digests"
DIGESTS_SORT = "digest_date:desc,id:desc"

router = APIRouter(prefix="/v1", tags=["digests"])


@router.get(
    "/digests",
    summary="List published digests",
    operation_id="get_digests",
    response_model=Page[DigestSummary],
    responses={
        200: {"description": "A cursor-paginated page of published digests."},
        400: {"model": ErrorEnvelope, "description": "Invalid pagination cursor."},
        422: {"model": ErrorEnvelope, "description": "Invalid query parameter or date range."},
    },
)
async def get_digests(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    request: Request,
    cursor_codec: Annotated[CursorCodec, Depends(get_cursor_codec)],
    repository: Annotated[DigestFeedRepository, Depends(get_digest_feed_repository)],
    limit: Annotated[int, Query(ge=MIN_LIMIT, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(description="Opaque continuation cursor.")] = None,
    date_from: Annotated[
        date | None, Query(description="Inclusive digest-date lower bound.")
    ] = None,
    date_to: Annotated[date | None, Query(description="Exclusive digest-date upper bound.")] = None,
) -> Response | Page[DigestSummary]:
    """Return published digests ordered by ``(digest_date DESC, id DESC)``."""
    try:
        date_range = validate_half_open_range(date_from, date_to)
        raw_filters: dict[str, FilterInput] = {"published_only": True}
        if date_range.lower is not None:
            raw_filters["date_from"] = date_range.lower
        if date_range.upper is not None:
            raw_filters["date_to"] = date_range.upper
        canonical_filters = canonicalize_filters(
            resource=DIGESTS_RESOURCE,
            sort=DIGESTS_SORT,
            filters=raw_filters,
        )
    except FilterValidationError:
        return error_response(
            request,
            status_code=422,
            code="validation_error",
            message="Invalid digest date range.",
        )

    after: tuple[date, uuid.UUID] | None = None
    if cursor is not None:
        try:
            payload = cursor_codec.decode(cursor, filters=canonical_filters)
            after = (date.fromisoformat(payload.k.t), payload.k.id)
        except (InvalidCursorError, ValueError):
            return error_response(
                request,
                status_code=400,
                code="invalid_cursor",
                message="The pagination cursor is invalid for this request.",
            )

    items = await repository.list_digests(
        feed_filter=DigestFeedFilter(date_from=date_range.lower, date_to=date_range.upper),
        after=after,
        limit=limit,
    )
    if any(item.status is not DigestStatus.PUBLISHED for item in items):
        raise RuntimeError("DigestFeedRepository returned an unpublished digest")
    has_more = len(items) > limit
    returned_items = items[:limit]
    next_cursor: str | None = None
    if has_more and returned_items:
        last_item = returned_items[-1]
        next_cursor = cursor_codec.encode(
            build_cursor_payload(
                filters=canonical_filters,
                last_sort_value=last_item.digest_date,
                last_id=last_item.id,
            )
        )

    summaries = [
        DigestSummary(
            id=item.id,
            digest_date=item.digest_date,
            status=item.status,
            title=item.title,
        )
        for item in returned_items
    ]
    return Page[DigestSummary](items=summaries, next_cursor=next_cursor)
