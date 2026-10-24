"""Limit how many API requests one client can make in a time window."""

import ipaddress
import time

from django.conf import settings
from django.core.cache import caches
from django.http import HttpRequest
from ninja.throttling import BaseThrottle

RATE_LIMIT_SCOPE = "api"

# Cloudflare edge address ranges, copied on 24 October 2026 from
# https://www.cloudflare.com/ips-v4 and https://www.cloudflare.com/ips-v6
CLOUDFLARE_NETWORKS = tuple(
    ipaddress.ip_network(address=network)
    for network in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


def _is_cloudflare_address(*, address: str) -> bool:
    """Tell whether an address lies inside a Cloudflare range.
    An invalid address counts as outside."""
    try:
        parsed = ipaddress.ip_address(address=address)
    except ValueError:
        return False
    return any(parsed in network for network in CLOUDFLARE_NETWORKS if network.version == parsed.version)


def client_address(*, request: HttpRequest) -> str:
    """Return the address of the visitor who sent the request.
    CF-Connecting-IP counts only when the peer is a Cloudflare address."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    peer = forwarded.split(",")[-1].strip() or request.META.get("REMOTE_ADDR") or "unknown"
    connecting = request.META.get("HTTP_CF_CONNECTING_IP", "").strip()
    if connecting and _is_cloudflare_address(address=peer):
        return connecting
    return peer


def _window_seconds() -> int:
    return max(1, int(settings.API_RATE_LIMIT_WINDOW_SECONDS))


def _seconds_left_in_window() -> int:
    window_seconds = _window_seconds()
    return max(1, window_seconds - (int(time.time()) % window_seconds))


def is_api_rate_limited(*, request: HttpRequest, scope: str, max_requests: int) -> tuple[bool, int]:
    """Count this request and tell whether the client is over the limit.
    Also return the seconds until the current window ends."""
    if not settings.API_RATE_LIMIT_ENABLED:
        return False, 0
    window_seconds = _window_seconds()
    allowed_requests = max(1, max_requests)
    now_seconds = int(time.time())
    retry_after_seconds = _seconds_left_in_window()
    window_bucket = now_seconds // window_seconds
    cache_key = f"api-rate:{scope}:{client_address(request=request)}:{window_bucket}"
    cache = caches["rate_limit"]
    if cache.add(cache_key, 1, timeout=window_seconds + 1):
        request_count = 1
    else:
        try:
            request_count = int(cache.incr(cache_key))
        except ValueError:
            cache.set(cache_key, 1, timeout=window_seconds + 1)
            request_count = 1
    return request_count > allowed_requests, retry_after_seconds


class ApiRateThrottle(BaseThrottle):
    """Limit read requests per client address for every API endpoint."""

    def allow_request(self, request: HttpRequest) -> bool:
        limited, _ = is_api_rate_limited(
            request=request,
            scope=RATE_LIMIT_SCOPE,
            max_requests=settings.API_RATE_LIMIT_READ_REQUESTS,
        )
        return not limited

    def wait(self) -> float:
        return float(_seconds_left_in_window())
