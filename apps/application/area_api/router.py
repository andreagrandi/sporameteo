"""Django Ninja endpoint for one stored estimate and its source facts."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from ninja import Field, Router, Schema, Status
from pyproj import Transformer
from shapely import transform as shapely_transform
from shapely.geometry import mapping
from shapely.wkb import loads as load_wkb

from config.api_schemas import ApiErrorSchema
from estimate_updates.contracts import EstimateRead, EstimateReadStatus
from estimate_updates.models import EstimateUpdate
from estimate_updates.services import facts_for_estimate, read_estimate
from map_api import published
from national_forest.models import ForestBuild, ForestCell
from national_terrain.models import TerrainCell, TerrainSourceTile
from weather_requests.eligibility import weather_cell
from weather_summaries.models import WeatherDaily, WeatherSnapshotDay, WeatherSummary


class CoordinatesSchema(Schema):
    """Return one WGS84 point with explicit coordinate names."""

    latitude: float
    longitude: float


class BoundsSchema(Schema):
    """Return WGS84 square bounds."""

    west: float
    south: float
    east: float
    north: float


class SelectionSchema(Schema):
    """Identify the selected square and the exact estimate publication."""

    square_id: str
    geometry: dict[str, Any]
    bounds: BoundsSchema
    centroid: CoordinatesSchema
    representative_coordinates: CoordinatesSchema
    target_date: date
    assessment_date: date
    days_ahead: int
    update_id: UUID
    run_id: UUID | None = None
    municipality_name: str | None = None


class EstimateSchema(Schema):
    """Describe a stored whole percentage or a machine-readable gap."""

    state: str
    whole_percentage: int | None = None
    unavailable_reason: str | None = None
    calculated_at: datetime | None = None
    method_version: str | None = None
    experimental_unvalidated: bool
    experimental_explanation_key: str
    translation_key: str


class FactPeriodSchema(Schema):
    """Describe the inclusive dates used by one contributing fact."""

    start: date
    end: date
    inclusive: bool


class FactSourceSchema(Schema):
    """Identify the immutable source rows used by one contributing fact."""

    id: str
    ids: list[str]


class FactSchema(Schema):
    """Expose one exact method input or derived method factor."""

    role_key: str
    role: str
    actual_value: float | None
    value: float | None
    unit: str
    period_start: date
    period_end: date
    period: FactPeriodSchema
    source_id: str
    source_ids: list[str]
    source: FactSourceSchema
    completed_day_count: int
    forecast_day_count: int
    method_factor: float | None
    normalized_factor: float | None
    translation_key: str
    quality: str | None = None
    source_metadata: dict = Field(default_factory=dict)


class WeatherPeriodSchema(Schema):
    """Return one completed-day rain period and its provenance."""

    state: str
    value: float | None
    unit: str
    period_start: date
    period_end: date
    inclusive: bool
    completed_day_count: int
    forecast_day_count: int
    source_ids: list[str]
    source_attempt_ids: list[str]
    missing_reason_key: str | None = None


class SelectedWeatherSchema(Schema):
    """Return weather for the requested target day."""

    state: str
    date: date
    rain_mm: float | None
    mean_temperature_c: float | None
    min_temperature_c: float | None
    completed_day_count: int
    forecast_day_count: int
    source_ids: list[str]
    source_attempt_ids: list[str]
    missing_reason_key: str | None = None
    et0_mm: float | None = None
    mean_soil_temperature_c: float | None = None
    mean_soil_moisture: float | None = None
    frost: bool | None = None


class WeatherGapSchema(Schema):
    """Explain one missing or incomplete stored weather variable."""

    date: date
    variable: str
    reason_key: str
    unit: str | None = None
    expected_hours: int | None = None
    present_hours: int | None = None


class WeatherSchema(Schema):
    """Return selected-day weather and three completed-day rain windows."""

    selected_day: SelectedWeatherSchema
    recent_rain_7_days: WeatherPeriodSchema
    recent_rain_14_days: WeatherPeriodSchema
    recent_rain_21_days: WeatherPeriodSchema
    provider: str | None = None
    model: str | None = None
    endpoint: str | None = None
    retrieval_time: datetime | None = None
    source_ids: list[str]
    source_attempt_ids: list[str]
    requested_coordinates: CoordinatesSchema | None = None
    returned_coordinates: CoordinatesSchema | None = None
    reference_elevation_m: float | None = None
    variable_gaps: list[WeatherGapSchema]
    limitation_keys: list[str]


class SourceGapSchema(Schema):
    """Explain one missing source or derived value."""

    reason_key: str
    detail: str | None = None


class SourceTileSchema(Schema):
    """Describe one terrain source tile used by the selected square."""

    tile_name: str
    status: str
    url: str
    release: str
    last_modified: str | None = None
    retrieved_at: datetime | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    crs: str | None = None
    native_pixel_m: float | None = None
    vertical_datum: str | None = None
    credit: str | None = None
    error_detail: str | None = None


class SourceRecordSchema(Schema):
    """Describe one immutable input source and its attribution."""

    name: str
    release: str | None = None
    native_resolution_m: float | None = None
    catalogue_url: str | None = None
    service_url: str | None = None
    url: str | None = None
    licence: str | None = None
    credit: str | None = None
    retrieval_time: datetime | None = None
    retrieval_sha256: str | None = None
    source_metadata: dict[str, Any]
    source_tiles: list[SourceTileSchema]
    gaps: list[SourceGapSchema]


class WeatherCreditSchema(Schema):
    """Return the exact Open-Meteo attribution required by its licence."""

    text: str
    url: str
    licence: str


class WeatherSourceSchema(Schema):
    """Describe the weather source and its immutable retrieval provenance."""

    provider: str | None = None
    model: str | None = None
    endpoint: str | None = None
    retrieval_time: datetime | None = None
    source_ids: list[str]
    source_attempt_ids: list[str]
    requested_coordinates: CoordinatesSchema | None = None
    returned_coordinates: CoordinatesSchema | None = None
    reference_elevation_m: float | None = None
    variable_gaps: list[WeatherGapSchema]
    credit: WeatherCreditSchema


class SourcesSchema(Schema):
    """Return grid, forest, terrain and weather attribution records."""

    grid: SourceRecordSchema
    forest: SourceRecordSchema
    terrain: SourceRecordSchema
    weather: WeatherSourceSchema
    limitation_keys: list[str]


class AreaDetailSchema(Schema):
    """Return one selected square and the frozen data behind its estimate."""

    selection: SelectionSchema
    estimate: EstimateSchema
    facts: list[FactSchema]
    weather: WeatherSchema
    sources: SourcesSchema


router = Router(tags=["areas"])


def _error(*, code: str, message: str, message_key: str, details: dict[str, Any] | None = None):
    """Build one structured API error."""
    return ApiErrorSchema(
        code=code,
        message=message,
        message_key=message_key,
        details=details,
    )


def _classification_error(
    *,
    code: str,
    message: str,
    message_key: str,
    square_id: str,
    target_date: date,
    update_id: UUID | str | None = None,
) -> Status[ApiErrorSchema]:
    """Return one no-update classification response without reading weather."""
    details: dict[str, Any] = {
        "square_id": square_id,
        "target_date": target_date.isoformat(),
    }
    if update_id is not None:
        details["update_id"] = str(update_id)
    return Status(
        409,
        _error(
            code=code,
            message=message,
            message_key=message_key,
            details=details,
        ),
    )


def _configured_square(*, square_id: str) -> ForestCell | None:
    """Find one square in the configured completed forest build."""
    version = str(getattr(settings, "SPORAMETEO_FOREST_BUILD_VERSION", ""))
    if not version:
        return None
    return (
        ForestCell.objects.select_related("grid_build_cell__cell", "chunk")
        .filter(
            build__version=version,
            build__status=ForestBuild.Status.COMPLETED,
            grid_build_cell__cell__cell_id=square_id,
        )
        .first()
    )


def _forest_state_without_update(
    *, square_id: str, target_date: date
) -> Status[ApiErrorSchema] | None:
    """Classify a square when the caller did not select an update UUID."""
    forest_cell = _configured_square(square_id=square_id)
    if forest_cell is None:
        return Status(
            404,
            _error(
                code="square_unknown",
                message="The requested square is not in the configured forest build.",
                message_key="area.square_unknown",
                details={"square_id": square_id},
            ),
        )
    if forest_cell.is_forest is None or forest_cell.forest_share is None:
        return _classification_error(
            code="missing_forest_data",
            message="The configured square has no required forest data.",
            message_key="area.missing_forest_data",
            square_id=square_id,
            target_date=target_date,
        )
    if forest_cell.is_forest is False or forest_cell.forest_share < 0.1:
        return _classification_error(
            code="no_forest",
            message="The configured square has no forest for this estimate.",
            message_key="area.no_forest",
            square_id=square_id,
            target_date=target_date,
        )
    return _classification_error(
        code="outside_prepared_area",
        message="The published run has no estimate for this square.",
        message_key="area.outside_prepared_area",
        square_id=square_id,
        target_date=target_date,
    )


def _square_update_matches_weather_cell(
    *, square_id: str, update: EstimateUpdate, forest_cell: ForestCell
) -> Status[ApiErrorSchema] | None:
    """Reject an eligible square whose selected update covers another weather cell."""
    if forest_cell.is_forest is not True:
        return None
    if forest_cell.weather_latitude is None or forest_cell.weather_longitude is None:
        return None
    expected_cell_key = weather_cell(
        latitude=forest_cell.weather_latitude,
        longitude=forest_cell.weather_longitude,
    ).key
    if expected_cell_key == update.cell_key:
        return None
    return Status(
        409,
        _error(
            code="update_square_mismatch",
            message="The selected update belongs to a different weather cell.",
            message_key="area.update_square_mismatch",
            details={
                "square_id": square_id,
                "update_id": str(update.public_id),
                "expected_cell_key": expected_cell_key,
                "update_cell_key": update.cell_key,
            },
        ),
    )


def _parse_request_date(*, value: str | None) -> date | None:
    """Parse one strict ISO calendar date."""
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.isoformat() == value else None


def _parse_request_uuid(*, value: str | None) -> UUID | None:
    """Parse one UUID query value."""
    try:
        return UUID(value)
    except (TypeError, ValueError):
        return None


def _parse_retrieval_time(*, value: str | datetime) -> datetime | None:
    """Parse a stored source retrieval time into a typed timestamp."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_TO_WGS84 = Transformer.from_crs("EPSG:3035", "EPSG:4326", always_xy=True)


def _wgs84_geometry(*, geometry: Any) -> tuple[dict[str, Any], BoundsSchema, CoordinatesSchema]:
    """Transform a stored EPSG:3035 polygon to WGS84 response values."""
    transformed = shapely_transform(
        load_wkb(bytes(geometry.wkb)),
        _TO_WGS84.transform,
        interleaved=False,
    )
    west, south, east, north = transformed.bounds
    centroid = transformed.centroid
    coordinates = CoordinatesSchema(latitude=float(centroid.y), longitude=float(centroid.x))
    return (
        mapping(transformed),
        BoundsSchema(
            west=float(west),
            south=float(south),
            east=float(east),
            north=float(north),
        ),
        coordinates,
    )


def _serialize_fact(*, fact: Any) -> FactSchema:
    """Convert a frozen forecast fact into the public typed shape."""
    return FactSchema(
        role_key=fact.role_key,
        role=fact.role_key,
        actual_value=fact.original_value,
        value=fact.original_value,
        unit=fact.unit,
        period_start=fact.period_start,
        period_end=fact.period_end,
        period=FactPeriodSchema(
            start=fact.period_start,
            end=fact.period_end,
            inclusive=True,
        ),
        source_id=fact.source_id,
        source_ids=list(fact.source_ids),
        source=FactSourceSchema(id=fact.source_id, ids=list(fact.source_ids)),
        completed_day_count=fact.completed_day_count,
        forecast_day_count=fact.forecast_day_count,
        method_factor=fact.normalized_factor,
        normalized_factor=fact.normalized_factor,
        translation_key=fact.translation_key,
        quality=fact.quality,
        source_metadata=fact.source_metadata,
    )


def _weather_day_map(*, snapshot_id: UUID) -> dict[date, WeatherDaily]:
    """Load only immutable daily rows referenced by one weather snapshot."""
    return {
        link.date: link.daily
        for link in WeatherSnapshotDay.objects.select_related("daily").filter(
            snapshot_id=snapshot_id
        )
    }


def _daily_source_ids(*, rows: tuple[WeatherDaily, ...]) -> tuple[list[str], list[str]]:
    """Return stable source and attempt identifiers in period order."""
    source_ids = list(dict.fromkeys(row.source_id for row in rows))
    attempt_ids = list(dict.fromkeys(str(row.attempt_id) for row in rows))
    return source_ids, attempt_ids


def _recent_period(
    *,
    rows: dict[date, WeatherDaily],
    assessment_date: date,
    days: int,
) -> WeatherPeriodSchema:
    """Calculate one completed-day rain window without future rows."""
    period_start = assessment_date - timedelta(days=days)
    period_end = assessment_date - timedelta(days=1)
    period_rows = tuple(
        rows[period_start + timedelta(days=offset)]
        for offset in range(days)
        if period_start + timedelta(days=offset) in rows
    )
    complete = tuple(
        row
        for row in period_rows
        if row.status == WeatherDaily.Status.COMPLETE and row.rain_mm is not None
    )
    complete_count = len(complete)
    value = sum(float(row.rain_mm) for row in complete) if complete_count == days else None
    source_ids, attempt_ids = _daily_source_ids(rows=period_rows)
    return WeatherPeriodSchema(
        state="available" if value is not None else "unavailable",
        value=round(value, 4) if value is not None else None,
        unit="mm",
        period_start=period_start,
        period_end=period_end,
        inclusive=True,
        completed_day_count=complete_count,
        forecast_day_count=0,
        source_ids=source_ids,
        source_attempt_ids=attempt_ids,
        missing_reason_key=None if value is not None else "weather.recent_rain_missing",
    )


def _variable_gaps(*, rows: tuple[WeatherDaily, ...]) -> list[WeatherGapSchema]:
    """Return recorded variable gaps without inventing a value."""
    gaps: list[WeatherGapSchema] = []
    for row in rows:
        variables = row.metadata.get("variables", {})
        if not isinstance(variables, dict):
            continue
        for variable, details in variables.items():
            if not isinstance(details, dict):
                continue
            reason = details.get("reason")
            if reason in {None, "complete"}:
                continue
            gaps.append(
                WeatherGapSchema(
                    date=row.date,
                    variable=variable,
                    reason_key=reason,
                    unit=details.get("unit"),
                    expected_hours=details.get("expected_hours"),
                    present_hours=details.get("present_hours"),
                )
            )
    return gaps


def _selected_weather(
    *,
    summary: WeatherSummary | None,
    daily: WeatherDaily | None,
    target_date: date,
) -> SelectedWeatherSchema:
    """Serialize the exact stored target-day weather row."""
    metadata = summary.metadata if summary is not None else {}
    target_metadata = metadata.get("target", {})
    if not isinstance(target_metadata, dict):
        target_metadata = {}
    completed_count = target_metadata.get("completed_day_count", 0)
    forecast_count = target_metadata.get("forecast_day_count", 1)
    if not isinstance(completed_count, int):
        completed_count = 0
    if not isinstance(forecast_count, int):
        forecast_count = 1
    rain = daily.rain_mm if daily is not None else None
    temperature = daily.mean_temperature_c if daily is not None else None
    minimum = daily.min_temperature_c if daily is not None else None
    if daily is None and summary is not None:
        rain = summary.target_rain_mm
        temperature = summary.target_mean_temperature_c
        minimum = summary.target_min_temperature_c
    complete = (
        daily is not None
        and daily.status == WeatherDaily.Status.COMPLETE
        and rain is not None
        and temperature is not None
    )
    source_ids = list(summary.metadata.get("target", {}).get("source_ids", [])) if summary else []
    if daily is not None and not source_ids:
        source_ids = [daily.source_id]
    attempt_ids = (
        list(summary.metadata.get("target", {}).get("source_attempt_ids", []))
        if summary
        else []
    )
    if daily is not None and not attempt_ids:
        attempt_ids = [str(daily.attempt_id)]
    return SelectedWeatherSchema(
        state="available" if complete else "unavailable",
        date=target_date,
        rain_mm=rain,
        mean_temperature_c=temperature,
        min_temperature_c=minimum,
        completed_day_count=completed_count,
        forecast_day_count=forecast_count,
        source_ids=source_ids,
        source_attempt_ids=attempt_ids,
        missing_reason_key=None if complete else "weather.selected_day_missing",
        et0_mm=daily.et0_mm if daily is not None and complete else None,
        mean_soil_temperature_c=(
            daily.mean_soil_temperature_c if daily is not None and complete else None
        ),
        mean_soil_moisture=(
            daily.mean_soil_moisture if daily is not None and complete else None
        ),
        frost=summary.frost if summary is not None and complete else None,
    )


def _weather_response(*, update: EstimateUpdate, target_date: date) -> WeatherSchema:
    """Build weather details solely from one update's snapshot."""
    summary = WeatherSummary.objects.filter(
        snapshot_id=update.snapshot_id,
        target_date=target_date,
    ).first()
    rows = _weather_day_map(snapshot_id=update.snapshot_id)
    daily = rows.get(target_date)
    period_rows = tuple(
        row
        for row_date, row in rows.items()
        if update.assessment_date - timedelta(days=21) <= row_date <= target_date
    )
    source_row = daily or (period_rows[0] if period_rows else None)
    requested = returned = None
    provider = model = endpoint = None
    retrieval_time = None
    if source_row is not None:
        provider = source_row.source_provider
        model = source_row.source_model
        endpoint = source_row.source_endpoint
        retrieval_time = source_row.retrieved_at
        requested = CoordinatesSchema(
            latitude=source_row.requested_latitude,
            longitude=source_row.requested_longitude,
        )
        returned = CoordinatesSchema(
            latitude=source_row.returned_latitude,
            longitude=source_row.returned_longitude,
        )
    source_ids = list(update.snapshot.source_ids)
    attempt_ids = list(update.snapshot.source_attempt_ids)
    return WeatherSchema(
        selected_day=_selected_weather(summary=summary, daily=daily, target_date=target_date),
        recent_rain_7_days=_recent_period(
            rows=rows,
            assessment_date=update.assessment_date,
            days=7,
        ),
        recent_rain_14_days=_recent_period(
            rows=rows,
            assessment_date=update.assessment_date,
            days=14,
        ),
        recent_rain_21_days=_recent_period(
            rows=rows,
            assessment_date=update.assessment_date,
            days=21,
        ),
        provider=provider,
        model=model,
        endpoint=endpoint,
        retrieval_time=retrieval_time,
        source_ids=source_ids,
        source_attempt_ids=attempt_ids,
        requested_coordinates=requested,
        returned_coordinates=returned,
        reference_elevation_m=update.snapshot.reference_elevation_m,
        variable_gaps=_variable_gaps(rows=period_rows),
        limitation_keys=["weather.shared_across_several_kilometres"],
    )


def _forest_source(*, forest_cell: ForestCell, update: EstimateUpdate) -> SourceRecordSchema:
    """Serialize forest source facts, including the used chunk checksum."""
    build = update.forest_build
    chunk = forest_cell.chunk
    metadata = dict(build.source_metadata or {})
    metadata.setdefault("source_release", build.source_release)
    metadata.setdefault("source_resolution_m", build.source_resolution_m)
    metadata.setdefault("source_crs", build.source_crs)
    return SourceRecordSchema(
        name=build.source_release,
        release=build.source_release,
        native_resolution_m=float(build.source_resolution_m),
        catalogue_url=build.catalogue_url,
        service_url=build.service_url,
        url=build.service_url,
        licence=build.licence,
        credit=build.credit,
        retrieval_time=_parse_retrieval_time(value=chunk.source_retrieved_at),
        retrieval_sha256=chunk.source_sha256 or None,
        source_metadata=metadata,
        source_tiles=[],
        gaps=(
            [SourceGapSchema(reason_key=forest_cell.missing_reason)]
            if forest_cell.missing_reason
            else []
        ),
    )


def _grid_source(*, update: EstimateUpdate) -> SourceRecordSchema:
    """Serialize the ISTAT source facts stored on the joined grid build."""
    build = update.forest_build.grid_build
    metadata = dict(build.source_metadata or {})

    def source_text(*, key: str) -> str | None:
        value = metadata.get(key)
        return value if isinstance(value, str) and value else None

    return SourceRecordSchema(
        name=source_text(key="dataset") or build.version,
        release=source_text(key="version"),
        catalogue_url=source_text(key="source_page"),
        service_url=source_text(key="source_url"),
        url=source_text(key="license_url"),
        licence=source_text(key="license"),
        credit=source_text(key="credit"),
        retrieval_sha256=source_text(key="archive_sha256"),
        source_metadata=metadata,
        source_tiles=[],
        gaps=[],
    )


def _terrain_source(*, terrain_cell: TerrainCell, update: EstimateUpdate) -> SourceRecordSchema:
    """Serialize terrain build metadata and only tiles used by this square."""
    build = update.terrain_build
    metadata = dict(build.source_metadata or {})
    tiles = list(
        TerrainSourceTile.objects.filter(
            build_id=build.id,
            chunk_links__chunk_id=terrain_cell.chunk_id,
        )
        .distinct()
        .order_by("tile_name")
    )
    tile_data = [
        SourceTileSchema(
            tile_name=tile.tile_name,
            status=tile.status,
            url=tile.url,
            release=tile.release,
            last_modified=tile.last_modified or None,
            retrieved_at=tile.retrieved_at,
            size_bytes=tile.size_bytes,
            sha256=tile.sha256 or None,
            crs=tile.crs or None,
            native_pixel_m=tile.native_pixel_m,
            vertical_datum=tile.vertical_datum or None,
            credit=tile.credit or None,
            error_detail=tile.error_detail or None,
        )
        for tile in tiles
    ]
    release = str(metadata.get("release", "Copernicus DEM GLO-30 public AWS copy, 2021"))
    native_pixel = metadata.get("native_pixel_m", 30.0)
    return SourceRecordSchema(
        name=release,
        release=release,
        native_resolution_m=float(native_pixel) if native_pixel is not None else None,
        catalogue_url=metadata.get("source_dataset"),
        service_url=metadata.get("source_url"),
        url=metadata.get("source_url"),
        licence=metadata.get("credit"),
        credit=metadata.get("credit"),
        retrieval_time=build.completed_at,
        retrieval_sha256=build.source_manifest_fingerprint or None,
        source_metadata=metadata,
        source_tiles=tile_data,
        gaps=(
            [
                SourceGapSchema(
                    reason_key=terrain_cell.source_gap_detail or "terrain.source_gap"
                )
            ]
            if terrain_cell.source_gap
            else []
        ),
    )


def _sources_response(
    *,
    update: EstimateUpdate,
    forest_cell: ForestCell,
    terrain_cell: TerrainCell | None,
    weather: WeatherSchema,
) -> SourcesSchema:
    """Combine exact source records and snapshot weather provenance."""
    terrain = (
        _terrain_source(terrain_cell=terrain_cell, update=update)
        if terrain_cell is not None
        else SourceRecordSchema(
            name=update.terrain_build.version,
            source_metadata=dict(update.terrain_build.source_metadata or {}),
            source_tiles=[],
            gaps=[SourceGapSchema(reason_key="terrain.data_missing")],
        )
    )
    return SourcesSchema(
        grid=_grid_source(update=update),
        forest=_forest_source(forest_cell=forest_cell, update=update),
        terrain=terrain,
        weather=WeatherSourceSchema(
            provider=weather.provider,
            model=weather.model,
            endpoint=weather.endpoint,
            retrieval_time=weather.retrieval_time,
            source_ids=weather.source_ids,
            source_attempt_ids=weather.source_attempt_ids,
            requested_coordinates=weather.requested_coordinates,
            returned_coordinates=weather.returned_coordinates,
            reference_elevation_m=weather.reference_elevation_m,
            variable_gaps=weather.variable_gaps,
            credit=WeatherCreditSchema(
                text="Weather data by Open-Meteo.com",
                url="https://open-meteo.com/",
                licence="CC BY 4.0",
            ),
        ),
        limitation_keys=weather.limitation_keys,
    )


def _detail_response(
    *,
    update: EstimateUpdate,
    forest_cell: ForestCell,
    target_date: date,
    read: EstimateRead,
) -> AreaDetailSchema:
    """Build the successful response from one exact publication."""
    geometry, bounds, centroid = _wgs84_geometry(geometry=forest_cell.grid_build_cell.cell.geometry)
    terrain_cell = TerrainCell.objects.filter(
        build_id=update.terrain_build_id,
        grid_build_cell_id=forest_cell.grid_build_cell_id,
    ).first()
    weather = _weather_response(update=update, target_date=target_date)
    facts: tuple[Any, ...] = ()
    if read.status in {EstimateReadStatus.AVAILABLE, EstimateReadStatus.UNAVAILABLE}:
        try:
            facts = facts_for_estimate(
                update_id=update.public_id,
                forest_cell_id=forest_cell.id,
                target_date=target_date,
            )
        except (TerrainCell.DoesNotExist, WeatherSummary.DoesNotExist):
            facts = ()
    return AreaDetailSchema(
        selection=SelectionSchema(
            square_id=forest_cell.grid_build_cell.cell.cell_id,
            geometry=geometry,
            bounds=bounds,
            centroid=centroid,
            representative_coordinates=centroid,
            target_date=target_date,
            assessment_date=update.assessment_date,
            days_ahead=(target_date - update.assessment_date).days,
            update_id=update.public_id,
            municipality_name=(
                municipality.name
                if (municipality := forest_cell.grid_build_cell.cell.municipality) is not None
                else None
            ),
        ),
        estimate=EstimateSchema(
            state=read.status.value,
            whole_percentage=read.percentage,
            unavailable_reason=read.unavailable_reason,
            calculated_at=read.calculated_at,
            method_version=read.method_version,
            experimental_unvalidated=True,
            experimental_explanation_key="estimate.experimental_unvalidated",
            translation_key=read.message_key,
        ),
        facts=[_serialize_fact(fact=fact) for fact in facts],
        weather=weather,
        sources=_sources_response(
            update=update,
            forest_cell=forest_cell,
            terrain_cell=terrain_cell,
            weather=weather,
        ),
    )


def _with_run_id(*, body: dict[str, Any], update_id: UUID) -> dict[str, Any]:
    """Add the published run that holds the update right now to a stored body."""
    selection = {**body["selection"], "run_id": published.run_id_for_update(update_id=update_id)}
    return {**body, "selection": selection}


def read_area_detail(
    *, square_id: str, target_date: date, update_id: UUID
) -> dict[str, Any] | Status[ApiErrorSchema]:
    """Return the detail body of one square for one date and update, or the API error.

    The endpoint and the landing page both call it, and it keeps each body in the map cache.
    """
    stored = published.cached_detail(
        update_id=update_id,
        square_id=square_id,
        target_date=target_date,
    )
    if stored is not None:
        return _with_run_id(body=stored, update_id=update_id)
    update = (
        EstimateUpdate.objects.select_related(
            "snapshot", "forest_build__grid_build", "terrain_build"
        )
        .filter(public_id=update_id)
        .first()
    )
    if update is None:
        return Status(
            410,
            _error(
                code="update_expired",
                message="The requested estimate update is no longer available.",
                message_key="area.update_expired",
                details={"update_id": str(update_id)},
            ),
        )
    last_date = update.assessment_date + timedelta(days=7)
    if not update.assessment_date <= target_date <= last_date:
        return Status(
            400,
            _error(
                code="invalid_date",
                message="target_date is outside the eight dates in update_id.",
                message_key="area.date_not_in_update",
                details={
                    "assessment_date": update.assessment_date.isoformat(),
                    "last_target_date": last_date.isoformat(),
                },
            ),
        )
    forest_cell = (
        ForestCell.objects.select_related(
            "grid_build_cell__cell__municipality", "chunk"
        )
        .filter(
            build_id=update.forest_build_id,
            grid_build_cell__cell__cell_id=square_id,
        )
        .first()
    )
    if forest_cell is None:
        return Status(
            404,
            _error(
                code="square_unknown",
                message="The requested square is not in this forest build.",
                message_key="area.square_unknown",
                details={"square_id": square_id},
            ),
        )
    if forest_cell.is_forest is None or forest_cell.forest_share is None:
        return _classification_error(
            code="missing_forest_data",
            message="The selected square has no required forest data.",
            message_key="area.missing_forest_data",
            square_id=square_id,
            target_date=target_date,
            update_id=update.public_id,
        )
    if forest_cell.is_forest is False or forest_cell.forest_share < 0.1:
        return _classification_error(
            code="no_forest",
            message="The selected square has no forest for this estimate.",
            message_key="area.no_forest",
            square_id=square_id,
            target_date=target_date,
            update_id=update.public_id,
        )
    mismatch = _square_update_matches_weather_cell(
        square_id=square_id,
        update=update,
        forest_cell=forest_cell,
    )
    if mismatch is not None:
        return mismatch
    read = read_estimate(
        update_id=update.public_id,
        forest_cell_id=forest_cell.id,
        target_date=target_date,
    )
    detail = _detail_response(
        update=update,
        forest_cell=forest_cell,
        target_date=target_date,
        read=read,
    )
    body = detail.model_dump(mode="json")
    published.store_detail(
        update_id=update_id,
        square_id=square_id,
        target_date=target_date,
        body=body,
    )
    return _with_run_id(body=body, update_id=update_id)


@router.get(
    "/areas/{square_id}",
    response={
        200: AreaDetailSchema,
        400: ApiErrorSchema,
        404: ApiErrorSchema,
        409: ApiErrorSchema,
        410: ApiErrorSchema,
    },
    openapi_extra={
        "parameters": [
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
            {
                "in": "query",
                "name": "update_id",
                "required": False,
                "description": "Exact saved update UUID. Omit only to classify the configured square.",
                "schema": {"type": "string", "format": "uuid"},
            },
        ]
    },
)
def area_detail(
    request: HttpRequest,
    square_id: str,
) -> AreaDetailSchema | Status[ApiErrorSchema]:
    """Return one square's exact stored estimate and explanation inputs."""
    target_date = request.GET.get("target_date")
    update_id = request.GET.get("update_id")
    from sporameteo.finds.grid import CELL_SIZE_M, parse_cell_id

    try:
        parse_cell_id(square_id, expected_size_m=CELL_SIZE_M)
    except ValueError as exc:
        return Status(
            400,
            _error(
                code="invalid_square_id",
                message=str(exc),
                message_key="area.invalid_square_id",
            ),
        )
    parsed_date = _parse_request_date(value=target_date)
    if parsed_date is None:
        return Status(
            400,
            _error(
                code="invalid_date",
                message="target_date must use YYYY-MM-DD.",
                message_key="area.invalid_date",
            ),
        )
    if update_id is None:
        classification = _forest_state_without_update(
            square_id=square_id,
            target_date=parsed_date,
        )
        if classification is None:
            raise AssertionError("A square without an update must have a classification.")
        return classification
    parsed_update = _parse_request_uuid(value=update_id)
    if parsed_update is None:
        return Status(
            400,
            _error(
                code="invalid_update_id",
                message="update_id must be a UUID.",
                message_key="area.invalid_update_id",
            ),
        )
    result = read_area_detail(square_id=square_id, target_date=parsed_date, update_id=parsed_update)
    if isinstance(result, Status):
        return result
    return JsonResponse(result)


__all__ = [
    "AreaDetailSchema",
    "area_detail",
    "read_area_detail",
    "router",
]
