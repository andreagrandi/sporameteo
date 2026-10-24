"""Checks the API request limit for one client address."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.cache import caches
from django.db import connection
from django.test import Client, RequestFactory

from config.api_limits import client_address

pytestmark = pytest.mark.django_db

LIMIT = 2
CLOUDFLARE_PEER = "162.158.1.1"


@pytest.fixture(autouse=True)
def small_limit(settings):
    settings.API_RATE_LIMIT_ENABLED = True
    settings.API_RATE_LIMIT_WINDOW_SECONDS = 3600
    settings.API_RATE_LIMIT_READ_REQUESTS = LIMIT
    caches["rate_limit"].clear()
    yield
    caches["rate_limit"].clear()


def _get(*, path: str = "/api/v1/info", **meta: str):
    return Client().get(path=path, **meta)


def test_request_over_the_limit_returns_429_with_retry_after() -> None:
    statuses = [_get().status_code for _ in range(LIMIT)]
    response = _get()

    assert 429 not in statuses
    body = response.json()
    assert response.status_code == 429
    assert body["code"] == "too_many_requests"
    assert body["message_key"] == "api.too_many_requests"
    retry_after = body["details"]["retry_after"]
    assert isinstance(retry_after, int)
    assert 1 <= retry_after <= 3600
    assert response["Retry-After"] == str(retry_after)


def test_health_is_never_limited() -> None:
    for _ in range(LIMIT + 3):
        _get()

    assert _get(path="/api/v1/health").status_code == 200
    assert _get().status_code == 429


def test_cloudflare_visitors_with_different_connecting_ips_count_separately() -> None:
    for _ in range(LIMIT):
        _get(HTTP_X_FORWARDED_FOR=CLOUDFLARE_PEER, HTTP_CF_CONNECTING_IP="203.0.113.1")

    over = _get(HTTP_X_FORWARDED_FOR=CLOUDFLARE_PEER, HTTP_CF_CONNECTING_IP="203.0.113.1")
    other = _get(HTTP_X_FORWARDED_FOR=CLOUDFLARE_PEER, HTTP_CF_CONNECTING_IP="203.0.113.2")

    assert over.status_code == 429
    assert other.status_code != 429


def test_different_forwarded_addresses_count_separately() -> None:
    for _ in range(LIMIT):
        _get(HTTP_X_FORWARDED_FOR="198.51.100.1")

    assert _get(HTTP_X_FORWARDED_FOR="198.51.100.1").status_code == 429
    assert _get(HTTP_X_FORWARDED_FOR="198.51.100.2").status_code != 429


def test_connecting_ip_from_a_peer_outside_cloudflare_is_ignored() -> None:
    for number in range(LIMIT):
        _get(HTTP_X_FORWARDED_FOR="198.51.100.1", HTTP_CF_CONNECTING_IP=f"203.0.113.{number}")

    response = _get(HTTP_X_FORWARDED_FOR="198.51.100.1", HTTP_CF_CONNECTING_IP="203.0.113.99")

    assert response.status_code == 429


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"HTTP_X_FORWARDED_FOR": "1.1.1.1, 198.51.100.7"}, "198.51.100.7"),
        ({"REMOTE_ADDR": "198.51.100.8"}, "198.51.100.8"),
        ({"HTTP_X_FORWARDED_FOR": CLOUDFLARE_PEER, "HTTP_CF_CONNECTING_IP": "203.0.113.5"}, "203.0.113.5"),
        ({"HTTP_X_FORWARDED_FOR": "2606:4700::1", "HTTP_CF_CONNECTING_IP": "203.0.113.5"}, "203.0.113.5"),
        ({"HTTP_X_FORWARDED_FOR": "198.51.100.7", "HTTP_CF_CONNECTING_IP": "203.0.113.5"}, "198.51.100.7"),
        ({"HTTP_X_FORWARDED_FOR": "not-an-address", "HTTP_CF_CONNECTING_IP": "203.0.113.5"}, "not-an-address"),
        ({"HTTP_X_FORWARDED_FOR": CLOUDFLARE_PEER}, CLOUDFLARE_PEER),
        ({"REMOTE_ADDR": ""}, "unknown"),
    ],
)
def test_client_address_picks_the_visitor_address(meta: dict[str, str], expected: str) -> None:
    request = RequestFactory().get(path="/", **meta)

    assert client_address(request=request) == expected


def test_limiter_uses_the_shared_database_cache() -> None:
    _get()

    cache = caches["rate_limit"]
    assert cache.__class__.__name__ == "DatabaseCache"
    assert any(key.startswith("api-rate:api:") for key in _stored_keys())


def _stored_keys() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT cache_key FROM sporameteo_rate_limit_cache")
        return [row[0].split(":", 2)[-1] for row in cursor.fetchall()]


def test_nothing_is_counted_when_the_limit_is_off(settings) -> None:
    settings.API_RATE_LIMIT_ENABLED = False

    statuses = [_get().status_code for _ in range(LIMIT + 3)]

    assert 429 not in statuses
    assert _stored_keys() == []


def test_limit_is_off_by_default_when_debug_is_on() -> None:
    code = "from django.conf import settings; print(settings.API_RATE_LIMIT_ENABLED)"
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "PYTHONPATH": str(Path(__file__).parents[2] / "apps/application"),
        "SPORAMETEO_DEBUG": "1",
    }
    env.pop("SPORAMETEO_API_RATE_LIMIT_ENABLED", None)

    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)

    assert result.stdout.strip() == "False"
