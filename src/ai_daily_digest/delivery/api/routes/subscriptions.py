"""Public subscription routes; raw capabilities stay in JSON POST bodies only."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_daily_digest.delivery.api.client_network import (
    ClientNetworkResolutionError,
    resolve_client_network_identity,
)
from ai_daily_digest.delivery.api.dependencies import get_subscription_service
from ai_daily_digest.delivery.api.errors import INTERNAL_ERROR_CODE, ApiError, ErrorEnvelope
from ai_daily_digest.delivery.subscriptions.repository import normalize_email
from ai_daily_digest.delivery.subscriptions.service import (
    SubscriptionRateLimitError,
    SubscriptionService,
)
from ai_daily_digest.delivery.subscriptions.tokens import InvalidSubscriptionTokenError

router = APIRouter(prefix="/v1/subscriptions", tags=["subscriptions"])

INVALID_ACTION_MESSAGE = "The subscription token is invalid or no longer usable."
GENERIC_REQUEST_MESSAGE = "If the address is eligible, a confirmation email will be sent."


class SubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    consent_to_daily_digest: Literal[True]

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


class SubscriptionTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=256)


class SubscriptionMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str


SubscriptionServiceDependency = Annotated[SubscriptionService, Depends(get_subscription_service)]


def _network(request: Request) -> str:
    return resolve_client_network_identity(request)


def _require_frontend_origin(request: Request) -> None:
    expected = getattr(request.app.state, "frontend_origin", None)
    if expected is None or request.headers.get("origin") != expected:
        raise ApiError(
            status_code=403,
            code="invalid_request_origin",
            message="The request origin is not allowed.",
        )


def _translate_failure(exc: Exception) -> None:
    if isinstance(exc, InvalidSubscriptionTokenError):
        raise ApiError(
            status_code=400,
            code="invalid_subscription_token",
            message=INVALID_ACTION_MESSAGE,
        ) from None
    if isinstance(exc, SubscriptionRateLimitError):
        raise ApiError(
            status_code=429,
            code="subscription_rate_limited",
            message="Too many subscription requests. Please try again later.",
        ) from None
    if isinstance(exc, ClientNetworkResolutionError):
        raise ApiError(
            status_code=500,
            code=INTERNAL_ERROR_CODE,
            message="The subscription request could not be processed.",
        ) from None
    raise exc


@router.post(
    "",
    operation_id="request_subscription",
    status_code=202,
    response_model=SubscriptionMessage,
    responses={
        422: {"model": ErrorEnvelope},
        429: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    },
)
async def request_subscription(
    payload: SubscriptionRequest,
    request: Request,
    service: SubscriptionServiceDependency,
) -> SubscriptionMessage:
    try:
        await service.request_subscription(payload.email, _network(request))
    except (SubscriptionRateLimitError, ClientNetworkResolutionError) as exc:
        _translate_failure(exc)
    return SubscriptionMessage(message=GENERIC_REQUEST_MESSAGE)


@router.post(
    "/confirm",
    operation_id="confirm_subscription",
    response_model=SubscriptionMessage,
    responses={
        400: {"model": ErrorEnvelope},
        403: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        429: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    },
)
async def confirm_subscription(
    payload: SubscriptionTokenRequest,
    request: Request,
    service: SubscriptionServiceDependency,
) -> SubscriptionMessage:
    _require_frontend_origin(request)
    try:
        await service.confirm(payload.token, _network(request))
    except (
        InvalidSubscriptionTokenError,
        SubscriptionRateLimitError,
        ClientNetworkResolutionError,
    ) as exc:
        _translate_failure(exc)
    return SubscriptionMessage(message="Your subscription has been confirmed.")


@router.post(
    "/unsubscribe",
    operation_id="unsubscribe_subscription",
    response_model=SubscriptionMessage,
    responses={
        400: {"model": ErrorEnvelope},
        403: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        429: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    },
)
async def unsubscribe_subscription(
    payload: SubscriptionTokenRequest,
    request: Request,
    service: SubscriptionServiceDependency,
) -> SubscriptionMessage:
    _require_frontend_origin(request)
    try:
        await service.unsubscribe(payload.token, _network(request))
    except (
        InvalidSubscriptionTokenError,
        SubscriptionRateLimitError,
        ClientNetworkResolutionError,
    ) as exc:
        _translate_failure(exc)
    return SubscriptionMessage(message="You have been unsubscribed.")
