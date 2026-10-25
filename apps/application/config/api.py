"""Django Ninja endpoints for the local application."""

import logging
import math
from typing import Any, Literal

import sentry_sdk
from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from ninja import NinjaAPI, Schema, Status
from ninja.errors import HttpError, Throttled, ValidationError

from area_api.router import router as area_router
from config.api_limits import ApiRateThrottle
from config.api_schemas import ApiErrorSchema
from config.health import database_is_available
from map_api.api import router as map_router
from place_search.api import router as place_search_router


class HealthResponse(Schema):
    """Report whether the application and database can answer requests."""

    application: Literal["available"]
    database: Literal["available", "unavailable"]


logger = logging.getLogger(__name__)

api = NinjaAPI(
    title="SporaMeteo API",
    version="1.1.0",
    description=(
        "Mushroom growing conditions for each map square in Italy, "
        "based on recent weather and forest cover."
    ),
    throttle=[ApiRateThrottle()],
)


def _error_response(
    *,
    request: HttpRequest,
    status: int,
    code: str,
    message: str,
    message_key: str,
    details: dict[str, Any] | None = None,
) -> HttpResponse:
    """Return one error in the shared shape that every API endpoint uses.
    A phone app then reads a single error format."""
    error = ApiErrorSchema(code=code, message=message, message_key=message_key, details=details)
    return api.create_response(request, error.model_dump(), status=status)


@api.exception_handler(ValidationError)
def handle_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
    """Return 422 with the field errors when a query or body value is invalid."""
    return _error_response(
        request=request,
        status=422,
        code="invalid_request",
        message="The request contains invalid values.",
        message_key="api.invalid_request",
        details={"errors": exc.errors},
    )


@api.exception_handler(Http404)
def handle_not_found(request: HttpRequest, exc: Http404) -> HttpResponse:
    """Return 404 when an endpoint raises Http404."""
    return _error_response(
        request=request,
        status=404,
        code="not_found",
        message="Not found.",
        message_key="api.not_found",
    )


@api.exception_handler(Throttled)
def handle_throttled(request: HttpRequest, exc: Throttled) -> HttpResponse:
    """Return 429 with the seconds to wait in the body.
    Ninja adds the same number as the Retry-After header."""
    retry_after = max(1, math.ceil(exc.wait)) if exc.wait is not None else 1
    return _error_response(
        request=request,
        status=429,
        code="too_many_requests",
        message="Too many requests. Try again later.",
        message_key="api.too_many_requests",
        details={"retry_after": retry_after},
    )


@api.exception_handler(HttpError)
def handle_http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
    """Return the shared shape and keep the status code of the raised HttpError."""
    return _error_response(
        request=request,
        status=exc.status_code,
        code="http_error",
        message=str(exc),
        message_key="api.http_error",
    )


@api.exception_handler(Exception)
def handle_internal_error(request: HttpRequest, exc: Exception) -> HttpResponse:
    """Log an unhandled error, send it to Sentry and return 500.
    The real message stays hidden unless DEBUG is on."""
    logger.exception("Unhandled API error.", exc_info=exc)
    sentry_sdk.capture_exception(exc)
    message = str(exc) if settings.DEBUG else "Internal server error."
    return _error_response(
        request=request,
        status=500,
        code="internal_error",
        message=message,
        message_key="api.internal_error",
    )


@api.get("/health", throttle=[], response={200: HealthResponse, 503: HealthResponse})
def health(request: HttpRequest) -> HealthResponse | Status[HealthResponse]:
    """Return application availability and a database connection check."""
    if not database_is_available():
        return Status(503, HealthResponse(application="available", database="unavailable"))
    return HealthResponse(application="available", database="available")


api.add_router("", area_router)
api.add_router("", map_router)
api.add_router("", place_search_router)
