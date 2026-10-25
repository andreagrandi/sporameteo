"""Django Ninja endpoints for place search and exact point resolution."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from django.conf import settings
from django.contrib.gis.geos import Point
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from ninja import Router, Schema
from pyproj import Transformer

from config.api_schemas import ApiErrorSchema
from national_forest.models import ForestBuild
from national_grid.models import GridBuildCell, GridRegion

from .provider import PlaceSearchError, PlaceSearchThrottled, cached_place_results, search_places


router = Router(tags=["places"])
WGS84_TO_GRID = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
GRID_TO_WGS84 = Transformer.from_crs("EPSG:3035", "EPSG:4326", always_xy=True)


class PlaceResolvePayload(Schema):
    """Accept one exact WGS84 point and its selection source."""

    latitude: float
    longitude: float
    source: Literal["search", "geolocation", "map", "centre", "point"] = "point"


class PlaceErrorSchema(Schema):
    """Describe a place search or resolve error.
    These errors carry no message_key or details."""

    code: str
    message: str


class PlaceBoundsSchema(Schema):
    """Describe the WGS84 bounds of one place."""

    west: float
    south: float
    east: float
    north: float


class PlaceResultSchema(Schema):
    """Describe one Italian place found by name."""

    name: str
    context: str
    latitude: float
    longitude: float
    bounds: PlaceBoundsSchema | None = None
    osm_id: str | None = None


class PlaceSearchResponse(Schema):
    """Describe the places found for one query.
    A short query, a cached_only read and a provider search each add their own optional fields."""

    results: list[PlaceResultSchema]
    cached: bool
    complete: bool | None = None
    minimum_length: int | None = None
    provider: str | None = None
    attribution: str | None = None


class PlacePointSchema(Schema):
    """Describe one WGS84 point."""

    latitude: float
    longitude: float


class PlaceResolveResponse(Schema):
    """Describe the grid square that holds one exact point."""

    status: Literal["available"]
    point: PlacePointSchema
    representative: PlacePointSchema
    square_id: str
    region: str | None
    coverage: Literal["configured"]
    source: Literal["search", "geolocation", "map", "centre", "point"]
    resolved_at: datetime


def _error(*, status: int, code: str, message: str, retry_after: int | None = None) -> JsonResponse:
    response = JsonResponse({"code": code, "message": message}, status=status)
    if retry_after is not None:
        response["Retry-After"] = str(max(1, retry_after))
    return response


@router.get(
    "/places/search",
    response={
        200: PlaceSearchResponse,
        400: PlaceErrorSchema,
        429: PlaceErrorSchema,
        502: PlaceErrorSchema,
        504: PlaceErrorSchema,
    },
)
def places_search(request: HttpRequest) -> HttpResponse:
    """Return selectable Italian Photon results through the same origin."""
    query = (request.GET.get("q") or request.GET.get("query") or "").strip()
    minimum = int(getattr(settings, "SPORAMETEO_PLACE_SEARCH_MIN_LENGTH", 2))
    if len(query) < minimum:
        if request.GET.get("cached_only") == "1":
            return JsonResponse({"results": [], "cached": True, "complete": False, "minimum_length": minimum})
        return JsonResponse({"results": [], "cached": True, "minimum_length": minimum})
    if len(query) > 120:
        return _error(status=400, code="invalid_query", message="The place search query is too long.")
    if request.GET.get("cached_only") == "1":
        results, complete = cached_place_results(
            query=query,
            language=request.GET.get("lang") or getattr(request, "LANGUAGE_CODE", "it"),
        )
        return JsonResponse({"results": results, "cached": True, "complete": complete})
    try:
        results, cached = search_places(
            query=query,
            language=request.GET.get("lang") or getattr(request, "LANGUAGE_CODE", "it"),
        )
    except PlaceSearchThrottled as error:
        return _error(status=429, code=error.code, message=str(error), retry_after=error.retry_after)
    except PlaceSearchError as error:
        status = 504 if error.code == "provider_timeout" else 502
        return _error(status=status, code=error.code, message=str(error))
    return JsonResponse(
        {
            "results": results,
            "cached": cached,
            "provider": "photon",
            "attribution": "OpenStreetMap data via Photon by Komoot",
        }
    )


def _number(value: str | None, *, lower: float, upper: float) -> float | None:
    try:
        number = float(value or "")
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and lower <= number <= upper else None


def _configured_forest_build() -> ForestBuild | None:
    version = str(getattr(settings, "SPORAMETEO_FOREST_BUILD_VERSION", ""))
    if not version:
        return None
    return ForestBuild.objects.select_related("grid_build").filter(
        version=version,
        status=ForestBuild.Status.COMPLETED,
    ).first()


@router.post(
    "/places/resolve",
    response={
        200: PlaceResolveResponse,
        400: PlaceErrorSchema,
        # Ninja rejects a malformed body with the shared error shape and the same status.
        422: PlaceErrorSchema | ApiErrorSchema,
        503: PlaceErrorSchema,
    },
)
def places_resolve(request: HttpRequest, payload: PlaceResolvePayload) -> HttpResponse:
    """Resolve exact WGS84 coordinates to one configured grid cell."""
    latitude = _number(str(payload.latitude), lower=-90, upper=90)
    longitude = _number(str(payload.longitude), lower=-180, upper=180)
    if latitude is None or longitude is None:
        response = _error(
            status=400,
            code="invalid_coordinates",
            message="Latitude and longitude must be finite WGS84 coordinates.",
        )
        response["Cache-Control"] = "no-store"
        return response
    build = _configured_forest_build()
    if build is None:
        response = _error(status=503, code="coverage_unconfigured", message="Map coverage is not configured.")
        response["Cache-Control"] = "no-store"
        return response
    try:
        east, north = WGS84_TO_GRID.transform(longitude, latitude)
    except (ValueError, OverflowError):
        response = _error(status=422, code="outside_italy", message="This point is outside Italy.")
        response["Cache-Control"] = "no-store"
        return response
    if not math.isfinite(east) or not math.isfinite(north):
        response = _error(status=422, code="outside_italy", message="This point is outside Italy.")
        response["Cache-Control"] = "no-store"
        return response
    point = Point(x=east, y=north, srid=3035)
    grid_build = build.grid_build
    region = GridRegion.objects.filter(build=grid_build, geometry__covers=point).order_by("name").first()
    membership = GridBuildCell.objects.select_related("cell").filter(
        build=grid_build,
        land_geometry__covers=point,
    ).order_by("cell__cell_id").first()
    if membership is None:
        code = "outside_italy" if region is None else "outside_configured_coverage"
        message = (
            "This point is outside Italy."
            if code == "outside_italy"
            else "This point is in Italy but outside the configured map coverage."
        )
        response = _error(status=422, code=code, message=message)
        response["Cache-Control"] = "no-store"
        return response
    centroid = membership.cell.geometry.centroid
    centroid_x, centroid_y = GRID_TO_WGS84.transform(centroid.x, centroid.y)
    response = JsonResponse(
        {
            "status": "available",
            "point": {"latitude": latitude, "longitude": longitude},
            "representative": {"latitude": centroid_y, "longitude": centroid_x},
            "square_id": membership.cell.cell_id,
            "region": region.name if region is not None else None,
            "coverage": "configured",
            "source": payload.source,
            "resolved_at": timezone.now().isoformat(),
        }
    )
    response["Cache-Control"] = "no-store"
    return response
