"""Checks that every API failure returns the shared error shape."""

import json
from unittest.mock import patch

from django.http import Http404
from django.test import Client, RequestFactory, override_settings
from ninja.errors import HttpError

from config.api import api


def _raise_through_api(*, exc: Exception) -> tuple[int, dict]:
    """Pass one exception to the API handlers and return the status and body.
    This reaches the same handlers that a failing endpoint would reach."""
    request = RequestFactory().get(path="/api/v1/example")
    response = api.on_exception(request=request, exc=exc)
    return response.status_code, json.loads(response.content)


def test_invalid_body_value_returns_invalid_request_with_field_errors() -> None:
    response = Client().post(
        path="/api/v1/places/resolve",
        data=json.dumps({"latitude": "not-a-number", "longitude": 10}),
        content_type="application/json",
    )

    body = response.json()
    assert response.status_code == 422
    assert body["code"] == "invalid_request"
    assert body["message_key"] == "api.invalid_request"
    assert body["message"] == "The request contains invalid values."
    assert "detail" not in body
    locations = [error["loc"] for error in body["details"]["errors"]]
    assert ["body", "payload", "latitude"] in locations


def test_http404_returns_not_found() -> None:
    status, body = _raise_through_api(exc=Http404("Missing square."))

    assert status == 404
    assert body == {
        "code": "not_found",
        "message": "Not found.",
        "message_key": "api.not_found",
        "details": None,
    }


def test_http_error_keeps_its_status_code() -> None:
    status, body = _raise_through_api(exc=HttpError(status_code=403, message="Forbidden here."))

    assert status == 403
    assert body == {
        "code": "http_error",
        "message": "Forbidden here.",
        "message_key": "api.http_error",
        "details": None,
    }


@override_settings(DEBUG=False)
def test_other_error_hides_its_message_and_goes_to_sentry() -> None:
    error = RuntimeError("database password is wrong")

    with patch("config.api.sentry_sdk.capture_exception") as capture:
        status, body = _raise_through_api(exc=error)

    assert status == 500
    assert body == {
        "code": "internal_error",
        "message": "Internal server error.",
        "message_key": "api.internal_error",
        "details": None,
    }
    capture.assert_called_once_with(error)


@override_settings(DEBUG=True)
def test_other_error_shows_its_message_when_debug_is_on() -> None:
    with patch("config.api.sentry_sdk.capture_exception"):
        status, body = _raise_through_api(exc=RuntimeError("database password is wrong"))

    assert status == 500
    assert body["code"] == "internal_error"
    assert body["message"] == "database password is wrong"
