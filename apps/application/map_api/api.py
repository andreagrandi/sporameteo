"""Typed map metadata, forest layer and published run read endpoints."""

from __future__ import annotations

import gzip
import hashlib
import math
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from django.conf import settings
from django.contrib.gis.db.models import Extent
from django.contrib.gis.db.models.functions import Transform
from django.http import HttpRequest, HttpResponse, JsonResponse
from ninja import Router, Schema
from pyproj import Geod

from config.api_schemas import ApiErrorSchema
from map_api import published, top_areas
from map_api.published import GRID_TO_WGS84, WGS84_TO_GRID, MapReadError
from national_forest.layer import resolve_layer_path, validate_layer
from national_forest.models import ForestBuild
from national_grid.models import GridBuildCell
from national_terrain.models import TerrainBuild
from weather_on_demand import services as weather_services
from weather_requests.eligibility import TUSCANY_ISTAT_CODE


GEOD = Geod(ellps="WGS84")
MAX_VIEW_WIDTH_M = 60_000.0
MAX_VIEW_HEIGHT_M = 60_000.0
MAX_SQUARES = 16_000
MAX_DAYS_AHEAD = 7

router = Router(tags=["map"])


class CoverageSchema(Schema):
    """Describe the configured grid coverage in WGS84."""

    west: float
    south: float
    east: float
    north: float


class LayerInfoSchema(Schema):
    """Describe the immutable forest overview artifact."""

    url: str
    sha256: str
    gzip_bytes: int | None = None
    raw_bytes: int | None = None


class AttributionSchema(Schema):
    """Describe one map data source and its attribution."""

    name: str
    credit: str | None = None
    licence: str | None = None
    url: str | None = None


class WeatherCellSchema(Schema):
    """Describe the stored weather cell centre for one square."""

    latitude: float
    longitude: float


class RunSchema(Schema):
    """Describe the published map that every map read comes from, as one identifier."""

    run_id: str
    assessment_date: date
    published_at: datetime
    target_dates: list[date]


class RegionRunSchema(Schema):
    """Describe the published run of one region."""

    region_code: str
    run_id: UUID
    assessment_date: date
    published_at: datetime


class InfoResponse(Schema):
    """Describe dates, coverage and limits for map reads."""

    today: date
    target_dates: list[date]
    run: RunSchema | None
    runs: list[RegionRunSchema]
    coverage: CoverageSchema | None
    forest_build_version: str
    max_width_km: float
    max_height_km: float
    max_squares: int
    forest_layer: LayerInfoSchema
    attributions: list[AttributionSchema]


class SquarePropertiesSchema(Schema):
    """Describe one stored square result."""

    cell_id: str
    state: str
    percentage: int | None
    unavailable_reason: str | None
    reason: str | None
    target_date: date
    assessment_date: date | None
    update_id: UUID | None
    calculated_at: datetime | None
    message_key: str
    weather_cell: WeatherCellSchema | None


class SquareFeatureSchema(Schema):
    """Describe one GeoJSON square feature."""

    type: Literal["Feature"]
    id: str
    geometry: dict[str, Any]
    properties: SquarePropertiesSchema


class SquaresResponse(Schema):
    """Describe one GeoJSON square collection."""

    type: Literal["FeatureCollection"]
    run_id: str
    assessment_date: date
    target_date: date
    square_count: int
    features: list[SquareFeatureSchema]


class TopAreaSchema(Schema):
    """Describe one of the best scoring areas."""

    rank: int
    square_id: str
    update_id: str
    municipality: str
    region: str | None
    latitude: float
    longitude: float
    percentage: int


class TopAreasResponse(Schema):
    """Describe the best areas of the first available date."""

    target_date: date
    run_id: str
    areas: list[TopAreaSchema]


def _today() -> date:
    """Return today's date at the Europe/Rome boundary."""
    return weather_services.italian_assessment_date()


def _error_response(
    *,
    status: int,
    code: str,
    message: str,
    message_key: str,
    details: dict[str, Any] | None = None,
) -> JsonResponse:
    """Build one stable JSON error response."""
    return JsonResponse(
        {
            "code": code,
            "message": message,
            "message_key": message_key,
            "details": details,
        },
        status=status,
    )


def _configured_build() -> ForestBuild | None:
    """Return the configured completed forest build."""
    version = str(getattr(settings, "SPORAMETEO_FOREST_BUILD_VERSION", ""))
    if not version:
        return None
    return (
        ForestBuild.objects.select_related("grid_build")
        .filter(
            version=version,
            status=ForestBuild.Status.COMPLETED,
        )
        .first()
    )


def _configured_terrain(*, grid_build_id: UUID) -> TerrainBuild | None:
    """Return the configured completed terrain build for one grid."""
    version = str(getattr(settings, "SPORAMETEO_TERRAIN_BUILD_VERSION", ""))
    if not version:
        return None
    return TerrainBuild.objects.filter(
        version=version,
        status=TerrainBuild.Status.COMPLETED,
        grid_build_id=grid_build_id,
    ).first()


def _source_text(*, value: object) -> str | None:
    """Return one non-empty stored source value."""
    return value if isinstance(value, str) and value else None


def _attributions(*, build: ForestBuild) -> list[AttributionSchema]:
    """Return ordered attributions from the configured build records."""
    grid_metadata = dict(build.grid_build.source_metadata or {})
    attributions = [
        AttributionSchema(
            name="ISTAT boundaries",
            credit=_source_text(value=grid_metadata.get("credit")),
            licence=_source_text(value=grid_metadata.get("license")),
            url=_source_text(value=grid_metadata.get("license_url")),
        ),
        AttributionSchema(
            name="Copernicus forest",
            credit=build.credit or None,
            licence=build.licence or None,
            url=build.catalogue_url or None,
        ),
    ]
    terrain = _configured_terrain(grid_build_id=build.grid_build_id)
    if terrain is not None:
        terrain_metadata = dict(terrain.source_metadata or {})
        terrain_credit = _source_text(value=terrain_metadata.get("credit"))
        attributions.append(
            AttributionSchema(
                name="Copernicus terrain",
                credit=terrain_credit,
                licence=terrain_credit,
                url=_source_text(
                    value=terrain_metadata.get("source_dataset")
                    or terrain_metadata.get("source_url")
                ),
            )
        )
    attributions.append(
        AttributionSchema(
            name="Open-Meteo",
            credit="Weather data by Open-Meteo.com",
            licence="CC BY 4.0",
            url="https://open-meteo.com/",
        )
    )
    return attributions


def _build_error(*, status: int = 503) -> JsonResponse:
    """Return the stable response for unavailable configured data."""
    return _error_response(
        status=status,
        code="forest_build_unavailable",
        message="The configured completed forest build is not available.",
        message_key="map.forest_build_unavailable",
    )


def _forecast_unavailable() -> JsonResponse:
    """Return the stable response for a map read before any run is published."""
    return _error_response(
        status=503,
        code="forecast_unavailable",
        message="No prepared forecast is published yet.",
        message_key="map.forecast_unavailable",
    )


def _unsupported_date(*, available_dates: list[date]) -> JsonResponse:
    """Return the stable response for a date that the published run does not hold."""
    return _error_response(
        status=400,
        code="unsupported_date",
        message="The target_date is not one of the available dates.",
        message_key="map.unsupported_date",
        details={"available_dates": [value.isoformat() for value in available_dates]},
    )


def _parse_date(value: str | None) -> tuple[date | None, JsonResponse | None]:
    if not value:
        return None, _error_response(
            status=400,
            code="missing_parameter",
            message="The parameter target_date is required.",
            message_key="map.target_date_required",
            details={"parameter": "target_date"},
        )
    if len(value) != 10:
        return None, _error_response(
            status=400,
            code="invalid_date",
            message="The parameter target_date must look like 2026-10-12.",
            message_key="map.invalid_date",
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None, _error_response(
            status=400,
            code="invalid_date",
            message="The parameter target_date is not a real date.",
            message_key="map.invalid_date",
        )
    if parsed.isoformat() != value:
        return None, _error_response(
            status=400,
            code="invalid_date",
            message="The parameter target_date must look like 2026-10-12.",
            message_key="map.invalid_date",
        )
    return parsed, None


def _parse_bbox(
    value: str | None,
) -> tuple[tuple[float, float, float, float] | None, JsonResponse | None]:
    message = "The parameter bbox must be four finite WGS84 numbers: west,south,east,north."
    if not value:
        return None, _error_response(
            status=400,
            code="missing_parameter",
            message="The parameter bbox is required.",
            message_key="map.bbox_required",
            details={"parameter": "bbox"},
        )
    parts = value.split(",")
    if len(parts) != 4:
        return None, _error_response(
            status=400,
            code="invalid_bbox",
            message=message,
            message_key="map.invalid_bbox",
        )
    try:
        parsed = tuple(float(part.strip()) for part in parts)
    except (TypeError, ValueError):
        return None, _error_response(
            status=400,
            code="invalid_bbox",
            message=message,
            message_key="map.invalid_bbox",
        )
    west, south, east, north = parsed
    if (
        not all(math.isfinite(number) for number in parsed)
        or not -180 <= west <= 180
        or not -180 <= east <= 180
        or not -90 <= south <= 90
        or not -90 <= north <= 90
    ):
        return None, _error_response(
            status=400,
            code="invalid_bbox",
            message="The bbox coordinates must be finite WGS84 values within longitude and latitude bounds.",
            message_key="map.invalid_bbox",
        )
    if west >= east or south >= north:
        return None, _error_response(
            status=400,
            code="invalid_bbox",
            message="The bbox needs west smaller than east and south smaller than north.",
            message_key="map.invalid_bbox_order",
        )
    return (west, south, east, north), None


def _bbox_size_m(*, bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    west, south, east, north = bbox
    _, _, width = GEOD.inv(west, south, east, south)
    _, _, height = GEOD.inv(west, south, west, north)
    return abs(width), abs(height)


@lru_cache(maxsize=32)
def _coverage_cached(*, grid_build_id: str, forest_fingerprint: str) -> CoverageSchema | None:
    """Return one immutable grid extent in WGS84 from its stored geometries."""
    extent = (
        GridBuildCell.objects.filter(build_id=grid_build_id)
        .annotate(wgs84_geometry=Transform("cell__geometry", srid=4326))
        .aggregate(wgs84_extent=Extent("wgs84_geometry"))
        .get("wgs84_extent")
    )
    del forest_fingerprint
    if extent is None:
        return None
    west, south, east, north = extent
    return CoverageSchema(
        west=float(west),
        south=float(south),
        east=float(east),
        north=float(north),
    )


_REGION_EXTENTS: dict[tuple[str, tuple[str, ...]], CoverageSchema] = {}


def _coverage(*, build: ForestBuild, region_codes: list[str]) -> CoverageSchema | None:
    """Return the union extent of the regions when it exists, else the grid extent."""
    grid_build_id = str(build.grid_build_id)
    cache_key = (grid_build_id, tuple(region_codes))
    cached = _REGION_EXTENTS.get(cache_key)
    if cached is not None:
        return cached
    extent = published.region_extent(grid_build_id=build.grid_build_id, region_codes=region_codes)
    if extent is not None:
        west, south, east, north = extent
        cached = CoverageSchema(west=west, south=south, east=east, north=north)
        _REGION_EXTENTS[cache_key] = cached
        return cached
    return _coverage_cached(
        grid_build_id=grid_build_id,
        forest_fingerprint=build.fingerprint,
    )


def _layer_info(*, build: ForestBuild) -> LayerInfoSchema:
    return LayerInfoSchema(
        url="/api/v1/forest-layer",
        sha256=build.layer_sha256,
        gzip_bytes=build.layer_gzip_bytes,
        raw_bytes=build.layer_raw_bytes,
    )


def _body_response(*, body: published.CachedBody) -> HttpResponse:
    """Build a response from a finished JSON body that is identified by its cache key."""
    response = HttpResponse(body.content, content_type="application/json")
    response["Cache-Control"] = "no-cache"
    response["Vary"] = "Accept-Encoding"
    response["ETag"] = f'"{hashlib.sha256(body.key.encode("utf-8")).hexdigest()}"'
    return response


def _read_error_response(*, error: MapReadError) -> JsonResponse:
    return _error_response(
        status=error.status,
        code=error.code,
        message=error.message,
        message_key=error.message_key,
        details=error.details,
    )


@router.get("/info", response={200: InfoResponse, 503: ApiErrorSchema})
def info(request: HttpRequest) -> InfoResponse | HttpResponse:
    """Return the dates, coverage and limits for configured map data."""
    del request
    build = _configured_build()
    if build is None:
        return _build_error()
    today = _today()
    current = published.published_map()
    if current is None:
        dates = [today + timedelta(days=offset) for offset in range(MAX_DAYS_AHEAD + 1)]
        region_codes = [TUSCANY_ISTAT_CODE]
    else:
        dates = current.available_dates(today=today)
        region_codes = sorted(current.runs)
    coverage = _coverage(build=build, region_codes=region_codes)
    return {
        "today": today.isoformat(),
        "target_dates": [value.isoformat() for value in dates],
        "run": published.map_info(published=current) if current is not None else None,
        "runs": published.regions_info(published=current) if current is not None else [],
        "coverage": coverage.dict() if coverage is not None else None,
        "forest_build_version": build.version,
        "max_width_km": MAX_VIEW_WIDTH_M / 1_000,
        "max_height_km": MAX_VIEW_HEIGHT_M / 1_000,
        "max_squares": MAX_SQUARES,
        "forest_layer": _layer_info(build=build).dict(),
        "attributions": [attribution.dict() for attribution in _attributions(build=build)],
    }


def _layer_error() -> JsonResponse:
    """Return the stable response for an unavailable forest layer."""
    return _error_response(
        status=503,
        code="forest_layer_unavailable",
        message="The configured forest layer artifact is not available.",
        message_key="map.forest_layer_unavailable",
    )


@router.get("/estimate-overview", response={200: Any, 400: ApiErrorSchema, 503: ApiErrorSchema})
def estimate_overview(request: HttpRequest) -> HttpResponse:
    """Average stored forest-square scores of the published run into bounded overview areas."""
    bbox, error = _parse_bbox(request.GET.get("bbox"))
    if error is not None or bbox is None:
        return error
    target, error = _parse_date(request.GET.get("target_date"))
    if error is not None or target is None:
        return error
    current = published.published_map()
    if current is None:
        return _forecast_unavailable()
    available_dates = current.available_dates(today=_today())
    if target not in available_dates:
        return _unsupported_date(available_dates=available_dates)
    width, height = _bbox_size_m(bbox=bbox)
    size = max(2000, math.ceil(max(width, height) / 80 / 1000) * 1000)
    body = published.read_overview(published=current, target_date=target, bbox=bbox, size=size)
    return _body_response(body=body)


def _read_layer(*, build: ForestBuild) -> tuple[bytes, str] | JsonResponse:
    """Read and verify the immutable forest layer artifact."""
    if not build.layer_path or not build.layer_sha256:
        return _layer_error()
    path = resolve_layer_path(value=build.layer_path)
    try:
        compressed = path.read_bytes()
        digest = hashlib.sha256(compressed).hexdigest()
        if digest != build.layer_sha256:
            raise ValueError("The forest layer hash does not match its build record.")
        validate_layer(path=path)
        raw = gzip.decompress(compressed)
        if build.layer_gzip_bytes is not None and len(compressed) != build.layer_gzip_bytes:
            raise ValueError("The forest layer size does not match its build record.")
        if build.layer_raw_bytes is not None and len(raw) != build.layer_raw_bytes:
            raise ValueError("The forest layer raw size does not match its build record.")
    except (OSError, EOFError, ValueError, gzip.BadGzipFile):
        return _layer_error()
    return compressed, digest


@router.get("/forest-layer", response={200: Any, 503: ApiErrorSchema})
def forest_layer(request: HttpRequest) -> HttpResponse:
    """Return the verified immutable gzip forest overview layer."""
    build = _configured_build()
    if build is None:
        return _build_error()
    layer = _read_layer(build=build)
    if isinstance(layer, JsonResponse):
        return layer
    compressed, digest = layer
    accepts_gzip = "gzip" in request.META.get("HTTP_ACCEPT_ENCODING", "").lower()
    etag = f'"{digest}"'
    if request.META.get("HTTP_IF_NONE_MATCH") == etag:
        response = HttpResponse(status=304)
        response["ETag"] = etag
        response["Cache-Control"] = "no-cache"
        response["Vary"] = "Accept-Encoding"
        return response
    content = compressed if accepts_gzip else gzip.decompress(compressed)
    response = HttpResponse(content=content, content_type="application/geo+json")
    response["ETag"] = etag
    response["Cache-Control"] = "no-cache"
    response["Vary"] = "Accept-Encoding"
    if accepts_gzip:
        response["Content-Encoding"] = "gzip"
    return response


@router.get(
    "/squares",
    response={200: SquaresResponse, 400: ApiErrorSchema, 503: ApiErrorSchema},
    openapi_extra={
        "parameters": [
            {
                "in": "query",
                "name": "bbox",
                "required": True,
                "description": "WGS84 west,south,east,north bounds.",
                "schema": {"type": "string", "minLength": 1},
            },
            {
                "in": "query",
                "name": "target_date",
                "required": True,
                "description": "Italian calendar date in YYYY-MM-DD form.",
                "schema": {
                    "type": "string",
                    "format": "date",
                    "pattern": r"^\d{4}-\d{2}-\d{2}$",
                },
            },
        ]
    },
)
def squares(request: HttpRequest) -> HttpResponse:
    """Return the stored values of the published run for the requested map view."""
    parsed_bbox, bbox_error = _parse_bbox(request.GET.get("bbox"))
    if bbox_error is not None or parsed_bbox is None:
        return bbox_error
    parsed_date, date_error = _parse_date(request.GET.get("target_date"))
    if date_error is not None or parsed_date is None:
        return date_error
    width_m, height_m = _bbox_size_m(bbox=parsed_bbox)
    if width_m > MAX_VIEW_WIDTH_M or height_m > MAX_VIEW_HEIGHT_M:
        return _error_response(
            status=400,
            code="view_too_large",
            message="Zoom in for estimates before requesting this view.",
            message_key="map.zoom_in_for_estimates",
            details={
                "width_m": round(width_m, 2),
                "height_m": round(height_m, 2),
                "max_width_m": MAX_VIEW_WIDTH_M,
                "max_height_m": MAX_VIEW_HEIGHT_M,
            },
        )
    current = published.published_map()
    if current is None:
        return _forecast_unavailable()
    available_dates = current.available_dates(today=_today())
    if parsed_date not in available_dates:
        return _unsupported_date(available_dates=available_dates)
    try:
        body = published.read_squares(
            published=current,
            target_date=parsed_date,
            bbox=parsed_bbox,
            max_squares=MAX_SQUARES,
        )
    except MapReadError as error:
        return _read_error_response(error=error)
    return _body_response(body=body)


@router.get("/top-areas", response={200: TopAreasResponse, 503: ApiErrorSchema})
def top_areas_endpoint(request: HttpRequest) -> dict[str, Any] | JsonResponse:
    """Return the best scoring municipalities of the first available date."""
    del request
    current = published.published_map()
    dates = current.available_dates(today=_today()) if current is not None else []
    if current is None or not dates:
        return _forecast_unavailable()
    first, last = dates[0], dates[-1]
    entries = top_areas.top_areas(current=current, first=first, last=last)
    return {
        "target_date": first,
        "run_id": current.map_id,
        "areas": [
            {
                "rank": rank,
                "square_id": entry["square_id"],
                "update_id": entry["update_id"],
                "municipality": entry["name"],
                "region": entry["region"],
                "latitude": entry["latitude"],
                "longitude": entry["longitude"],
                "percentage": entry["first"],
            }
            for rank, entry in enumerate(entries, start=1)
        ],
    }


__all__ = [
    "GRID_TO_WGS84",
    "WGS84_TO_GRID",
    "InfoResponse",
    "SquaresResponse",
    "TopAreasResponse",
    "forest_layer",
    "info",
    "router",
    "squares",
    "top_areas_endpoint",
]
