"""Calculate, validate, publish and read complete estimate updates."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from sporameteo.forecast_method import (
    CURRENT_METHOD,
    METHODS_BY_VERSION,
    TEMPERATURE_PERIOD_DAYS,
    DailyTemperatures,
    ForecastFact,
    ForecastInputs,
    ForecastMethodConfig,
    ForecastMethodV2Config,
    ForecastMethodV3Config,
    ForecastMethodV4Config,
    ForecastResult,
    NumericInput,
    HostTrees,
    calculate_forecast,
    calculate_v4_scores,
)
from national_forest.models import ForestBuild, ForestCell
from sporameteo.moisture_method import WeatherDay
from sporameteo.weather.policy import PIPELINE_VERSION
from national_terrain.models import TerrainBuild, TerrainCell
from weather_requests.eligibility import canonical_cell_key
from weather_summaries.models import WeatherSnapshot, WeatherSnapshotDay, WeatherSummary

from .contracts import (
    EstimateRead,
    EstimateReadStatus,
    UNAVAILABLE_REASON_CODES,
    decode_result_bytes,
    encode_result_bytes,
)
from .models import (
    EstimateResult,
    EstimateTask,
    EstimateUpdate,
    ForecastRun,
    ForecastRunCell,
    PublishedForecastRun,
)
from .host_trees import calculation_version, saved_host_trees, tree_inputs_for_area


METHOD_VERSION = CURRENT_METHOD.version
DEFAULT_BATCH_SIZE = 1_000
STALE_ESTIMATE_TASK_AGE = timedelta(minutes=30)
TERRAIN_BUILD_SETTING = "SPORAMETEO_TERRAIN_BUILD_VERSION"
FOREST_BUILD_SETTING = "SPORAMETEO_FOREST_BUILD_VERSION"
REPLACED_TASK_REASON = "Replaced: the method or tree source changed after this task was queued."

logger = logging.getLogger(__name__)


class EstimateUpdateError(RuntimeError):
    """Report invalid estimate inputs or an incomplete publication."""


class EstimateValidationError(EstimateUpdateError, ValueError):
    """Report rows that fail complete-update validation."""


@dataclass(frozen=True)
class PreparedEstimateRow:
    """Hold one validated forest square and its eight encoded values."""

    forest_cell_id: int
    values: bytes


@dataclass(frozen=True)
class PreparedEstimateUpdate:
    """Hold all rows and immutable identities before database publication."""

    cell_key: str
    assessment_date: date
    snapshot_id: uuid.UUID
    forest_build_id: uuid.UUID
    terrain_build_id: uuid.UUID
    method_version: str
    calculated_at: datetime
    rows: tuple[PreparedEstimateRow, ...]
    input_fingerprint: str
    tree_inputs: dict = field(default_factory=dict)
    tree_fingerprint: str = ""
    region_code: str = ""

    @property
    def row_count(self) -> int:
        """Return the number of eligible forest squares."""
        return len(self.rows)


@dataclass(frozen=True)
class PublishedEstimate:
    """Describe one newly published or already existing update."""

    update: EstimateUpdate
    created: bool
    skipped_older: bool = False


def input_fingerprint(
    *,
    cell_key: str,
    assessment_date: date,
    snapshot_id: uuid.UUID,
    forest_build_id: uuid.UUID,
    terrain_build_id: uuid.UUID,
    method_version: str,
    tree_fingerprint: str = "",
    region_code: str = "",
) -> str:
    """Return the stable identity for one immutable input tuple."""
    value = "|".join(
        (
            cell_key,
            assessment_date.isoformat(),
            str(snapshot_id),
            str(forest_build_id),
            str(terrain_build_id),
            method_version,
        )
    )
    if tree_fingerprint:
        value += "|" + tree_fingerprint
    if region_code:
        value += "|region:" + region_code
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def advisory_lock_key(*, cell_key: str) -> int:
    """Return a stable signed PostgreSQL advisory-lock value."""
    return int.from_bytes(
        hashlib.blake2b(
            f"estimate-update:{cell_key}".encode("utf-8"),
            digest_size=8,
        ).digest(),
        byteorder="big",
        signed=True,
    )


@contextmanager
def estimate_cell_lock(*, cell_key: str) -> Iterator[None]:
    """Serialize publications for one weather cell."""
    if connection.vendor != "postgresql":
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [advisory_lock_key(cell_key=cell_key)])
    yield


def _terrain_build_version() -> str:
    value = getattr(settings, TERRAIN_BUILD_SETTING, "")
    if not isinstance(value, str) or not value.strip():
        raise EstimateUpdateError(
            f"{TERRAIN_BUILD_SETTING} must name the completed terrain build."
        )
    return value.strip()


def _require_date(value: date, *, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise EstimateValidationError(f"{name} must be a date.")
    return value


def _source_id(*, metadata: Mapping[str, Any], snapshot_id: uuid.UUID, group: str) -> str:
    values = metadata.get(group, {})
    source_ids = values.get("source_ids", []) if isinstance(values, Mapping) else []
    if source_ids:
        return str(source_ids[0])
    return f"weather-snapshot:{snapshot_id}"


def _period_metadata(
    *,
    summary: WeatherSummary,
    group: str,
    fallback_start: date,
    fallback_end: date,
) -> tuple[date, date, int, int]:
    metadata = summary.metadata.get(group, {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    try:
        period_start = date.fromisoformat(str(metadata.get("period_start", fallback_start)))
        period_end = date.fromisoformat(str(metadata.get("period_end", fallback_end)))
    except ValueError as exc:
        raise EstimateValidationError(f"Weather summary {group} has an invalid period.") from exc
    completed = metadata.get("completed_day_count", 0)
    forecast = metadata.get("forecast_day_count", 0)
    if isinstance(completed, bool) or not isinstance(completed, int):
        raise EstimateValidationError(f"Weather summary {group} has a noninteger completed count.")
    if isinstance(forecast, bool) or not isinstance(forecast, int):
        raise EstimateValidationError(f"Weather summary {group} has a noninteger forecast count.")
    if group == "old_rain":
        expected_completed, expected_forecast = 14, 0
    elif group == "recent_rain":
        days_ahead = summary.days_ahead
        expected_completed, expected_forecast = 7 - days_ahead, days_ahead
    else:
        expected_completed, expected_forecast = 0, 1
    if (completed, forecast) != (expected_completed, expected_forecast):
        raise EstimateValidationError(
            f"Weather summary {group} has the wrong day composition."
        )
    if period_end < period_start:
        raise EstimateValidationError(f"Weather summary {group} period is reversed.")
    return period_start, period_end, completed, forecast


def _numeric_input(
    *,
    value: float | None,
    unit: str,
    source_id: str,
    period_start: date,
    period_end: date,
    completed_day_count: int,
    forecast_day_count: int,
    quality: str | None = None,
    semantic: str | None = None,
) -> NumericInput:
    """Create one method input while preserving the source reference."""
    return NumericInput(
        value=value,
        unit=unit,
        source_id=source_id,
        period_start=period_start,
        period_end=period_end,
        completed_day_count=completed_day_count,
        forecast_day_count=forecast_day_count,
        quality=quality,
        semantic=semantic,
    )


def _finite_or_none(*, value: float | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _snapshot_daily_rows(*, snapshot: WeatherSnapshot) -> dict[date, Any]:
    """Load the daily weather rows linked to one snapshot, keyed by date."""
    return {
        day.date: day.daily
        for day in WeatherSnapshotDay.objects.filter(snapshot_id=snapshot.id).select_related("daily")
    }


def _period_temperatures(
    *,
    snapshot: WeatherSnapshot,
    target_date: date,
    daily_rows: Mapping[date, Any],
) -> DailyTemperatures:
    """Build the 20 daily means and minimums that end on the target date."""
    dates = [
        target_date - timedelta(days=TEMPERATURE_PERIOD_DAYS - 1 - offset)
        for offset in range(TEMPERATURE_PERIOD_DAYS)
    ]
    means: list[float | None] = []
    minimums: list[float | None] = []
    for day in dates:
        row = daily_rows.get(day)
        means.append(None if row is None else _finite_or_none(value=row.mean_temperature_c))
        minimums.append(None if row is None else _finite_or_none(value=row.min_temperature_c))
    completed = sum(1 for day in dates if day < snapshot.assessment_date)
    return DailyTemperatures(
        daily_means=tuple(means),
        daily_minimums=tuple(minimums),
        unit="deg_c",
        source_id=f"weather-snapshot-days:{snapshot.id}",
        period_start=dates[0],
        period_end=dates[-1],
        completed_day_count=completed,
        forecast_day_count=len(dates) - completed,
        semantic="temperature_at_reference_altitude",
    )


def _forecast_inputs(
    *,
    snapshot: WeatherSnapshot,
    summary: WeatherSummary,
    forest_cell: ForestCell,
    terrain_cell: TerrainCell | None,
    terrain_build_id: uuid.UUID,
    daily_rows: Mapping[date, Any],
    host_trees: HostTrees | None = None,
) -> ForecastInputs:
    assessment_date = snapshot.assessment_date
    target_date = summary.target_date
    old_start, old_end, old_completed, old_forecast = _period_metadata(
        summary=summary,
        group="old_rain",
        fallback_start=target_date - timedelta(days=21),
        fallback_end=target_date - timedelta(days=8),
    )
    recent_start, recent_end, recent_completed, recent_forecast = _period_metadata(
        summary=summary,
        group="recent_rain",
        fallback_start=target_date - timedelta(days=7),
        fallback_end=target_date - timedelta(days=1),
    )
    target_start, target_end, target_completed, target_forecast = _period_metadata(
        summary=summary,
        group="target",
        fallback_start=target_date,
        fallback_end=target_date,
    )
    source_id = f"weather-snapshot:{snapshot.id}"
    old_source = _source_id(metadata=summary.metadata, snapshot_id=snapshot.id, group="old_rain")
    recent_source = _source_id(
        metadata=summary.metadata,
        snapshot_id=snapshot.id,
        group="recent_rain",
    )
    target_source = _source_id(metadata=summary.metadata, snapshot_id=snapshot.id, group="target")
    return ForecastInputs(
        assessment_date=assessment_date,
        target_date=target_date,
        old_rain=_numeric_input(
            value=summary.old_rain_mm,
            unit="mm",
            source_id=old_source,
            period_start=old_start,
            period_end=old_end,
            completed_day_count=old_completed,
            forecast_day_count=old_forecast,
        ),
        recent_rain=_numeric_input(
            value=summary.recent_rain_mm,
            unit="mm",
            source_id=recent_source,
            period_start=recent_start,
            period_end=recent_end,
            completed_day_count=recent_completed,
            forecast_day_count=recent_forecast,
        ),
        target_temperature=_numeric_input(
            value=summary.target_mean_temperature_c,
            unit="deg_c",
            source_id=target_source,
            period_start=target_start,
            period_end=target_end,
            completed_day_count=target_completed,
            forecast_day_count=target_forecast,
            semantic="temperature_at_reference_altitude",
        ),
        period_temperatures=_period_temperatures(
            snapshot=snapshot,
            target_date=target_date,
            daily_rows=daily_rows,
        ),
        reference_altitude=_numeric_input(
            value=snapshot.reference_elevation_m,
            unit="m",
            source_id=source_id,
            period_start=assessment_date,
            period_end=assessment_date,
            completed_day_count=1,
            forecast_day_count=0,
        ),
        square_altitude=_numeric_input(
            value=None if terrain_cell is None else terrain_cell.altitude_m,
            unit="m",
            source_id=(
                f"terrain-cell:{terrain_cell.id}"
                if terrain_cell is not None
                else f"terrain-build:{terrain_build_id}"
            ),
            period_start=assessment_date,
            period_end=assessment_date,
            completed_day_count=1,
            forecast_day_count=0,
        ),
        host_trees=host_trees,
        daily_weather=tuple(
            WeatherDay(day=day, rain_mm=daily_rows[day].rain_mm,
                       temperature_c=daily_rows[day].mean_temperature_c,
                       et0_mm=daily_rows[day].et0_mm)
            for day in (target_date - timedelta(days=offset) for offset in range(30, 0, -1))
            if day in daily_rows
        ),
        daily_weather_source_id=f"weather-snapshot-days:{snapshot.id}",
        forest_share=_numeric_input(
            value=forest_cell.forest_share,
            unit="fraction",
            source_id=f"forest-cell:{forest_cell.id}",
            period_start=assessment_date,
            period_end=assessment_date,
            completed_day_count=1,
            forecast_day_count=0,
        ),
    )


def _summary_map(*, snapshot: WeatherSnapshot) -> dict[date, WeatherSummary]:
    summaries = list(snapshot.summaries.order_by("days_ahead", "target_date"))
    expected_dates = tuple(
        snapshot.assessment_date + timedelta(days=offset) for offset in range(8)
    )
    actual_dates = tuple(summary.target_date for summary in summaries)
    if actual_dates != expected_dates:
        raise EstimateValidationError(
            "A weather snapshot must contain exactly eight ordered target summaries."
        )
    return {summary.target_date: summary for summary in summaries}


def _forest_cells_for_weather_cell(
    *,
    forest_build: ForestBuild,
    cell_key: str,
    batch_size: int,
    region_code: str = "",
) -> list[ForestCell]:
    try:
        latitude_text, longitude_text = cell_key.split(",", maxsplit=1)
        latitude = Decimal(latitude_text)
        longitude = Decimal(longitude_text)
    except (InvalidOperation, ValueError) as exc:
        raise EstimateValidationError(f"Invalid weather cell key: {cell_key!r}.") from exc
    if canonical_cell_key(latitude=latitude, longitude=longitude) != cell_key:
        raise EstimateValidationError(f"Invalid weather cell key: {cell_key!r}.")
    squares = ForestCell.objects.filter(
        build_id=forest_build.id,
        is_forest=True,
        weather_latitude=float(latitude),
        weather_longitude=float(longitude),
    )
    if region_code:
        squares = squares.filter(grid_build_cell__owner_region__istat_code=region_code)
    return list(
        squares
        .only(
            "id",
            "build_id",
            "grid_build_cell_id",
            "forest_share",
            "weather_latitude",
            "weather_longitude",
        )
        .order_by("id")
        .iterator(chunk_size=batch_size)
    )


def validate_prepared_rows(
    *,
    rows: Sequence[PreparedEstimateRow],
    expected_forest_cell_ids: Iterable[int],
) -> tuple[PreparedEstimateRow, ...]:
    """Reject missing, duplicate, malformed or unexpected result rows."""
    expected = set(expected_forest_cell_ids)
    seen: set[int] = set()
    for row in rows:
        if row.forest_cell_id in seen:
            raise EstimateValidationError("Estimate rows contain a duplicate forest square.")
        seen.add(row.forest_cell_id)
        if len(row.values) != 8:
            raise EstimateValidationError("Estimate rows must contain eight bytes.")
        try:
            decode_result_bytes(values=row.values)
        except ValueError as exc:
            raise EstimateValidationError(str(exc)) from exc
    if seen != expected:
        missing = expected - seen
        extra = seen - expected
        raise EstimateValidationError(
            f"Estimate rows do not match expected forest squares. Missing {sorted(missing)}, "
            f"unexpected {sorted(extra)}."
        )
    return tuple(rows)


def _accept_outcome(
    *,
    percentage: int | None,
    reason: str | None,
    values: list[int | None],
    reasons: list[str | None],
) -> None:
    """Check one calculated percentage or unavailable reason and append it."""
    if percentage is None:
        if reason not in UNAVAILABLE_REASON_CODES:
            raise EstimateValidationError(f"Unknown estimate unavailable reason: {reason!r}.")
        values.append(None)
        reasons.append(reason)
        return
    if isinstance(percentage, bool) or not isinstance(percentage, int):
        raise EstimateValidationError("Estimate percentages must be whole integers.")
    if not math.isfinite(float(percentage)) or not 0 <= percentage <= 100:
        raise EstimateValidationError(
            "Estimate percentages must be finite and from zero through one hundred."
        )
    values.append(percentage)
    reasons.append(None)


def _v4_rows(
    *,
    snapshot: WeatherSnapshot,
    summaries: Mapping[date, WeatherSummary],
    forest_cells: Sequence[ForestCell],
    terrain_cells: Mapping[int, TerrainCell],
    terrain_build_id: uuid.UUID,
    daily_rows: Mapping[date, Any],
    tree_inputs: dict[str, Any],
    method: ForecastMethodV4Config,
) -> list[PreparedEstimateRow]:
    """Calculate all squares of a cell with the shared weather work done once per target date."""
    if not forest_cells:
        return []
    altitudes = [
        None if (terrain := terrain_cells.get(row.grid_build_cell_id)) is None else terrain.altitude_m
        for row in forest_cells
    ]
    shares = [row.forest_share for row in forest_cells]
    trees = [saved_host_trees(tree_inputs=tree_inputs, forest_cell_id=row.id) for row in forest_cells]
    values: list[list[int | None]] = [[] for _ in forest_cells]
    reasons: list[list[str | None]] = [[] for _ in forest_cells]
    for target_date in sorted(summaries):
        shared = _forecast_inputs(
            snapshot=snapshot,
            summary=summaries[target_date],
            forest_cell=forest_cells[0],
            terrain_cell=terrain_cells.get(forest_cells[0].grid_build_cell_id),
            terrain_build_id=terrain_build_id,
            daily_rows=daily_rows,
            host_trees=trees[0],
        )
        try:
            outcomes = calculate_v4_scores(
                inputs=shared, forest_shares=shares, altitudes=altitudes, host_trees=trees, method=method,
            )
        except (TypeError, ValueError) as exc:
            raise EstimateValidationError(str(exc)) from exc
        for index, (percentage, reason) in enumerate(outcomes):
            _accept_outcome(percentage=percentage, reason=reason, values=values[index], reasons=reasons[index])
    return [
        PreparedEstimateRow(
            forest_cell_id=row.id,
            values=encode_result_bytes(percentages=values[index], unavailable_reasons=reasons[index]),
        )
        for index, row in enumerate(forest_cells)
    ]


def calculate_estimate_update(
    *,
    snapshot_id: uuid.UUID | str,
    cell_key: str,
    assessment_date: date,
    forest_build_version: str,
    terrain_build_version: str | None = None,
    calculator: Callable[..., ForecastResult] | None = None,
    calculated_at: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    method: ForecastMethodConfig | ForecastMethodV2Config = CURRENT_METHOD,
    region_code: str = "",
) -> PreparedEstimateUpdate:
    """Calculate all eligible squares and validate rows before any insert."""
    assessment_date = _require_date(assessment_date, name="assessment_date")
    terrain_version = terrain_build_version or _terrain_build_version()
    if batch_size <= 0:
        raise ValueError("batch_size must be positive.")
    snapshot = WeatherSnapshot.objects.get(
        id=snapshot_id,
        cell_key=cell_key,
        assessment_date=assessment_date,
        status=WeatherSnapshot.Status.READY,
    )
    if isinstance(method, ForecastMethodV4Config) and snapshot.pipeline_version != PIPELINE_VERSION:
        raise EstimateValidationError("The current method requires a new 30-day weather snapshot.")
    forest_build = ForestBuild.objects.get(version=forest_build_version)
    terrain_build = TerrainBuild.objects.get(version=terrain_version)
    if forest_build.status != ForestBuild.Status.COMPLETED:
        raise EstimateUpdateError("The forest build must be completed.")
    if terrain_build.status != TerrainBuild.Status.COMPLETED:
        raise EstimateUpdateError("The terrain build must be completed.")
    if forest_build.grid_build_id != terrain_build.grid_build_id:
        raise EstimateUpdateError("Forest and terrain builds must use the same grid build.")
    summaries = _summary_map(snapshot=snapshot)
    daily_rows = _snapshot_daily_rows(snapshot=snapshot)
    forest_cells = _forest_cells_for_weather_cell(
        forest_build=forest_build,
        cell_key=cell_key,
        batch_size=batch_size,
        region_code=region_code,
    )
    grid_cell_ids = [row.grid_build_cell_id for row in forest_cells]
    terrain_cells = {
        row.grid_build_cell_id: row
        for row in TerrainCell.objects.filter(
            build=terrain_build,
            grid_build_cell_id__in=grid_cell_ids,
        )
        .only("id", "build_id", "grid_build_cell_id", "altitude_m")
        .order_by("id")
        .iterator(chunk_size=batch_size)
    }
    tree_inputs = tree_inputs_for_area(forest_cells=forest_cells) if isinstance(method, ForecastMethodV3Config) else {}
    tree_fingerprint = (
        hashlib.sha256(json.dumps(tree_inputs, sort_keys=True).encode()).hexdigest()
        if isinstance(method, ForecastMethodV3Config) else ""
    )
    expected_ids = [row.id for row in forest_cells]
    prepared_rows: list[PreparedEstimateRow] = []
    if calculator is None and isinstance(method, ForecastMethodV4Config):
        prepared_rows = _v4_rows(
            snapshot=snapshot,
            summaries=summaries,
            forest_cells=forest_cells,
            terrain_cells=terrain_cells,
            terrain_build_id=terrain_build.id,
            daily_rows=daily_rows,
            tree_inputs=tree_inputs,
            method=method,
        )
        forest_cells = []
    for forest_cell in forest_cells:
        terrain_cell = terrain_cells.get(forest_cell.grid_build_cell_id)
        values: list[int | None] = []
        reasons: list[str | None] = []
        for target_date in sorted(summaries):
            inputs = _forecast_inputs(
                snapshot=snapshot,
                summary=summaries[target_date],
                forest_cell=forest_cell,
                terrain_cell=terrain_cell,
                terrain_build_id=terrain_build.id,
                daily_rows=daily_rows,
                host_trees=saved_host_trees(tree_inputs=tree_inputs, forest_cell_id=forest_cell.id),
            )
            try:
                if calculator is None:
                    result = calculate_forecast(inputs=inputs, method=method)
                else:
                    result = calculator(inputs=inputs)
            except (TypeError, ValueError) as exc:
                raise EstimateValidationError(str(exc)) from exc
            if result.method_version != method.version:
                raise EstimateValidationError("Estimate rows use mixed method versions.")
            _accept_outcome(
                percentage=result.whole_percentage,
                reason=result.unavailable_reason,
                values=values,
                reasons=reasons,
            )
        encoded = encode_result_bytes(percentages=values, unavailable_reasons=reasons)
        prepared_rows.append(PreparedEstimateRow(forest_cell_id=forest_cell.id, values=encoded))
    rows = validate_prepared_rows(rows=prepared_rows, expected_forest_cell_ids=expected_ids)
    when = calculated_at or timezone.now()
    if timezone.is_naive(when):
        when = timezone.make_aware(when)
    fingerprint = input_fingerprint(
        cell_key=cell_key,
        assessment_date=assessment_date,
        snapshot_id=snapshot.id,
        forest_build_id=forest_build.id,
        terrain_build_id=terrain_build.id,
        method_version=method.version,
        tree_fingerprint=tree_fingerprint,
        region_code=region_code,
    )
    return PreparedEstimateUpdate(
        cell_key=cell_key,
        assessment_date=assessment_date,
        snapshot_id=snapshot.id,
        forest_build_id=forest_build.id,
        terrain_build_id=terrain_build.id,
        method_version=method.version,
        calculated_at=when,
        rows=rows,
        input_fingerprint=fingerprint,
        tree_inputs=tree_inputs,
        tree_fingerprint=tree_fingerprint,
        region_code=region_code,
    )


def publish_estimate_update(*, prepared: PreparedEstimateUpdate) -> PublishedEstimate:
    """Publish all validated rows in one transaction for one weather cell."""
    with transaction.atomic():
        with estimate_cell_lock(cell_key=prepared.cell_key):
            snapshot = WeatherSnapshot.objects.select_for_update().get(id=prepared.snapshot_id)
            if snapshot.cell_key != prepared.cell_key or snapshot.assessment_date != prepared.assessment_date:
                raise EstimateUpdateError("The weather snapshot identity changed before publication.")
            forest_build = ForestBuild.objects.select_for_update().get(id=prepared.forest_build_id)
            terrain_build = TerrainBuild.objects.select_for_update().get(id=prepared.terrain_build_id)
            if forest_build.status != ForestBuild.Status.COMPLETED:
                raise EstimateUpdateError("The forest build is no longer completed.")
            if terrain_build.status != TerrainBuild.Status.COMPLETED:
                raise EstimateUpdateError("The terrain build is no longer completed.")
            if forest_build.grid_build_id != terrain_build.grid_build_id:
                raise EstimateUpdateError("Forest and terrain builds must use the same grid build.")
            repeated = EstimateUpdate.objects.filter(
                input_fingerprint=prepared.input_fingerprint,
            ).first()
            if repeated is not None:
                return PublishedEstimate(update=repeated, created=False)
            newer = (
                EstimateUpdate.objects.filter(
                    cell_key=prepared.cell_key,
                    region_code=prepared.region_code,
                )
                .order_by("-assessment_date", "-calculated_at", "-id")
                .first()
            )
            if newer is not None and newer.assessment_date > prepared.assessment_date:
                return PublishedEstimate(update=newer, created=False, skipped_older=True)
            update = EstimateUpdate.objects.create(
                public_id=uuid.uuid4(),
                cell_key=prepared.cell_key,
                assessment_date=prepared.assessment_date,
                calculated_at=prepared.calculated_at,
                snapshot=snapshot,
                forest_build=forest_build,
                terrain_build=terrain_build,
                method_version=prepared.method_version,
                row_count=prepared.row_count,
                input_fingerprint=prepared.input_fingerprint,
                tree_inputs=prepared.tree_inputs,
                tree_fingerprint=prepared.tree_fingerprint,
                region_code=prepared.region_code,
            )
            EstimateResult.objects.bulk_create(
                [
                    EstimateResult(
                        update=update,
                        forest_cell_id=row.forest_cell_id,
                        values=row.values,
                    )
                    for row in prepared.rows
                ],
                batch_size=DEFAULT_BATCH_SIZE,
            )
            return PublishedEstimate(update=update, created=True)


def calculate_and_publish_estimates(**kwargs: Any) -> PublishedEstimate:
    """Calculate and publish one complete update."""
    return publish_estimate_update(prepared=calculate_estimate_update(**kwargs))


def _task_input_fingerprint(
    *,
    snapshot_id: uuid.UUID,
    cell_key: str,
    assessment_date: date,
    forest_build_version: str,
    terrain_build_version: str,
    calculation: str,
) -> str:
    return hashlib.sha256(
        "|".join(
            (
                str(snapshot_id),
                cell_key,
                assessment_date.isoformat(),
                forest_build_version,
                terrain_build_version,
                calculation,
            )
        ).encode("utf-8")
    ).hexdigest()


def _task_backend_status(*, task_id: uuid.UUID) -> str | None:
    """Return the durable queue status for one task result, if it exists."""
    from django_tasks_db.models import DBTaskResult

    return (
        DBTaskResult.objects.filter(id=task_id)
        .values_list("status", flat=True)
        .first()
    )


def _schedule_estimate_task(*, task_row: EstimateTask) -> bool:
    """Reserve one waiting task and queue it after the transaction commits."""
    if task_row.state != EstimateTask.State.WAITING or task_row.queue_token is not None:
        return False
    task_row.queue_token = uuid.uuid4()
    task_row.save(update_fields=["queue_token", "updated_at"])
    transaction.on_commit(lambda task_id=task_row.id: _queue_estimate_task(task_id=task_id))
    return True


def _queue_estimate_task(*, task_id: uuid.UUID) -> None:
    from .tasks import estimate_update_task

    with transaction.atomic():
        task_row = EstimateTask.objects.select_for_update().filter(id=task_id).first()
        if task_row is None:
            return
        if task_row.state != EstimateTask.State.WAITING or task_row.queue_token is None:
            return
        try:
            with transaction.atomic():
                result = estimate_update_task.enqueue(job_id=str(task_id))
        except Exception as exc:
            reason = f"Queue dispatch failed: {type(exc).__name__}: {exc}"
            task_row.queue_token = None
            task_row.failure_reason = (
                f"{task_row.failure_reason}\n{reason}"
                if task_row.failure_reason
                else reason
            )
            task_row.save(update_fields=["queue_token", "failure_reason", "updated_at"])
            return
        task_row.task_id = uuid.UUID(str(result.id))
        task_row.queue_token = None
        task_row.save(update_fields=["task_id", "queue_token", "updated_at"])


def enqueue_estimate_update(
    *,
    snapshot_id: uuid.UUID | str,
    cell_key: str,
    assessment_date: date,
    forest_build_version: str,
) -> EstimateTask:
    """Persist and queue one estimate task after weather commits."""
    terrain_build_version = _terrain_build_version()
    calculation = calculation_version()
    parsed_snapshot_id = uuid.UUID(str(snapshot_id))
    assessment_date = _require_date(assessment_date, name="assessment_date")
    fingerprint = _task_input_fingerprint(
        snapshot_id=parsed_snapshot_id,
        cell_key=cell_key,
        assessment_date=assessment_date,
        forest_build_version=forest_build_version,
        terrain_build_version=terrain_build_version,
        calculation=calculation,
    )
    with transaction.atomic():
        try:
            task_row = EstimateTask.objects.select_for_update().get(
                input_fingerprint=fingerprint,
            )
        except EstimateTask.DoesNotExist:
            try:
                with transaction.atomic():
                    task_row = EstimateTask.objects.create(
                        snapshot_id=parsed_snapshot_id,
                        cell_key=cell_key,
                        assessment_date=assessment_date,
                        forest_build_version=forest_build_version,
                        terrain_build_version=terrain_build_version,
                        calculation_version=calculation,
                        input_fingerprint=fingerprint,
                    )
            except IntegrityError:
                task_row = EstimateTask.objects.select_for_update().get(
                    input_fingerprint=fingerprint,
                )
        if task_row.state == EstimateTask.State.FAILED:
            task_row.task_id = None
            task_row.queue_token = None
            task_row.state = EstimateTask.State.WAITING
            task_row.save(update_fields=["task_id", "queue_token", "state", "updated_at"])
            _schedule_estimate_task(task_row=task_row)
        elif task_row.state == EstimateTask.State.WAITING and task_row.queue_token is None:
            backend_status = (
                _task_backend_status(task_id=task_row.task_id)
                if task_row.task_id is not None
                else None
            )
            if task_row.task_id is None or backend_status not in {"READY", "RUNNING"}:
                task_row.task_id = None
                task_row.save(update_fields=["task_id", "updated_at"])
            else:
                backend_status = "pending"
            if backend_status != "pending":
                _schedule_estimate_task(task_row=task_row)
    task_row.refresh_from_db()
    return task_row


def _is_replaced(*, task_row: EstimateTask) -> bool:
    return bool(task_row.calculation_version) and task_row.calculation_version != calculation_version()


def _close_replaced_task(*, task_row: EstimateTask) -> None:
    # A deploy that changes the method leaves tasks of the old method in the queue.
    # They must not publish, and closing them is routine, so nothing is raised.
    task_row.state = EstimateTask.State.FAILED
    task_row.task_id = None
    task_row.queue_token = None
    task_row.failure_reason = REPLACED_TASK_REASON
    task_row.ended_at = timezone.now()
    task_row.save(update_fields=["state", "task_id", "queue_token", "failure_reason", "ended_at", "updated_at"])
    logger.info("Closed estimate task %s queued with an older method or tree source.", task_row.id)


def process_estimate_task(*, task_id: uuid.UUID | str) -> PublishedEstimate | None:
    """Run one durable estimate task without making weather requests.

    A task queued with an older method or tree source is closed and returns None."""
    parsed_task_id = uuid.UUID(str(task_id))
    with transaction.atomic():
        task_row = EstimateTask.objects.select_for_update().get(id=parsed_task_id)
        if task_row.state == EstimateTask.State.COMPLETED:
            if task_row.published_update_id is None:
                raise EstimateUpdateError("A completed estimate task has no published update.")
            update = EstimateUpdate.objects.get(id=task_row.published_update_id)
            return PublishedEstimate(update=update, created=False)
        if task_row.state == EstimateTask.State.RUNNING:
            raise EstimateUpdateError("The estimate task is already running.")
        if _is_replaced(task_row=task_row):
            _close_replaced_task(task_row=task_row)
            return None
        task_row.state = EstimateTask.State.RUNNING
        task_row.attempt_count += 1
        task_row.started_at = timezone.now()
        task_row.save(update_fields=["state", "attempt_count", "started_at", "updated_at"])
    try:
        published = calculate_and_publish_estimates(
            snapshot_id=task_row.snapshot_id,
            cell_key=task_row.cell_key,
            assessment_date=task_row.assessment_date,
            forest_build_version=task_row.forest_build_version,
            terrain_build_version=task_row.terrain_build_version,
        )
    except Exception as exc:
        with transaction.atomic():
            EstimateTask.objects.filter(id=task_row.id).update(
                state=EstimateTask.State.FAILED,
                failure_reason=f"{type(exc).__name__}: {exc}",
                ended_at=timezone.now(),
                updated_at=timezone.now(),
            )
        raise
    with transaction.atomic():
        EstimateTask.objects.filter(id=task_row.id).update(
            state=EstimateTask.State.COMPLETED,
            published_update_id=published.update.id,
            ended_at=timezone.now(),
            failure_reason="",
            updated_at=timezone.now(),
        )
    return published


def retry_estimate_task(*, task_id: uuid.UUID | str) -> EstimateTask:
    """Queue an explicit retry for one failed estimate task."""
    parsed_task_id = uuid.UUID(str(task_id))
    with transaction.atomic():
        task_row = EstimateTask.objects.select_for_update().get(id=parsed_task_id)
        if task_row.state != EstimateTask.State.FAILED:
            raise EstimateUpdateError("Only a failed estimate task can be retried.")
        task_row.state = EstimateTask.State.WAITING
        task_row.task_id = None
        task_row.queue_token = None
        task_row.save(update_fields=["state", "task_id", "queue_token", "updated_at"])
        _schedule_estimate_task(task_row=task_row)
    task_row.refresh_from_db()
    return task_row


def recover_estimate_tasks() -> int:
    """Requeue estimate tasks stranded by a failed callback or worker restart."""
    recovered = 0
    candidates = EstimateTask.objects.filter(
        state__in=[EstimateTask.State.WAITING, EstimateTask.State.RUNNING],
    ).values_list("id", flat=True)
    for task_id in candidates.iterator(chunk_size=DEFAULT_BATCH_SIZE):
        with transaction.atomic():
            task_row = EstimateTask.objects.select_for_update().get(id=task_id)
            if task_row.state == EstimateTask.State.COMPLETED:
                continue
            if task_row.queue_token is not None:
                task_row.queue_token = None
                task_row.save(update_fields=["queue_token", "updated_at"])
            backend_status = (
                _task_backend_status(task_id=task_row.task_id)
                if task_row.task_id is not None
                else None
            )
            running_recently = (
                task_row.started_at is not None
                and task_row.started_at > timezone.now() - STALE_ESTIMATE_TASK_AGE
            )
            if backend_status == "RUNNING" and running_recently:
                continue
            if _is_replaced(task_row=task_row):
                _close_replaced_task(task_row=task_row)
                continue
            if backend_status == "READY":
                if task_row.state == EstimateTask.State.RUNNING:
                    task_row.state = EstimateTask.State.WAITING
                    task_row.save(update_fields=["state", "updated_at"])
                    recovered += 1
                continue
            if task_row.state == EstimateTask.State.RUNNING:
                task_row.state = EstimateTask.State.WAITING
                task_row.failure_reason = (
                    f"{task_row.failure_reason}\nWorker interrupted before estimate completion."
                    if task_row.failure_reason
                    else "Worker interrupted before estimate completion."
                )
            task_row.task_id = None
            task_row.queue_token = None
            task_row.save(
                update_fields=["state", "task_id", "queue_token", "failure_reason", "updated_at"]
            )
            if _schedule_estimate_task(task_row=task_row):
                recovered += 1
    return recovered + recover_estimate_handoffs()


def recover_estimate_handoffs() -> int:
    """Recreate missing estimate tasks for the latest ready weather inputs."""
    forest_build_version = getattr(settings, FOREST_BUILD_SETTING, "")
    if not isinstance(forest_build_version, str) or not forest_build_version.strip():
        return 0
    if not ForestBuild.objects.filter(version=forest_build_version).exists():
        return 0
    selected: dict[str, int] = {}
    recovered = 0
    snapshots = WeatherSnapshot.objects.filter(
        status=WeatherSnapshot.Status.READY,
        pipeline_version=PIPELINE_VERSION,
    ).order_by("cell_key", "-assessment_date", "-created_at")
    for snapshot in snapshots.iterator(chunk_size=DEFAULT_BATCH_SIZE):
        count = selected.get(snapshot.cell_key, 0)
        if count >= 2:
            continue
        selected[snapshot.cell_key] = count + 1
        task_row = enqueue_estimate_update(
            snapshot_id=snapshot.id,
            cell_key=snapshot.cell_key,
            assessment_date=snapshot.assessment_date,
            forest_build_version=forest_build_version,
        )
        if task_row.state == EstimateTask.State.WAITING:
            recovered += 1
    return recovered


def cleanup_estimate_updates(*, cleanup_date: date | None = None) -> tuple[str, ...]:
    """Keep updates for two distinct dates per cell and return protected snapshots."""
    target_date = cleanup_date or timezone.localdate()
    _require_date(target_date, name="cleanup_date")
    retained_snapshot_ids: set[str] = set()
    with transaction.atomic():
        published = PublishedForecastRun.objects.values_list("current_run_id", "previous_run_id")
        in_use = {run_id for pair in published for run_id in pair if run_id is not None}
        ForecastRun.objects.exclude(id__in=in_use).exclude(
            status=ForecastRun.Status.RUNNING,
        ).filter(assessment_date__lt=target_date).delete()
        run_update_ids = set(
            ForecastRunCell.objects.exclude(update_id=None).values_list("update_id", flat=True)
        )
        retained_snapshot_ids.update(
            str(value) for value in ForecastRunCell.objects.values_list("snapshot_id", flat=True)
        )
        # Read only the columns needed here. Whole rows with tree_inputs filled more than 1 GB
        # of worker memory once all 20 regions were updated every night.
        updates = list(
            EstimateUpdate.objects.select_for_update()
            .order_by("cell_key", "-assessment_date", "-calculated_at", "-id")
            .values_list("id", "cell_key", "assessment_date", "snapshot_id")
        )
        kept_dates: dict[str, list[date]] = {}
        deletable: list[int] = []
        for update_id, cell_key, assessment_date, snapshot_id in updates:
            if update_id in run_update_ids:
                retained_snapshot_ids.add(str(snapshot_id))
                continue
            if assessment_date > target_date:
                retained_snapshot_ids.add(str(snapshot_id))
                continue
            dates = kept_dates.setdefault(cell_key, [])
            if assessment_date in dates:
                retained_snapshot_ids.add(str(snapshot_id))
            elif len(dates) < 2:
                dates.append(assessment_date)
                retained_snapshot_ids.add(str(snapshot_id))
            else:
                deletable.append(update_id)
        if deletable:
            # The delete loads each row to follow cascades, so it reads only the id.
            EstimateUpdate.objects.filter(id__in=deletable).only("id").delete()
        active_snapshot_ids = set(
            EstimateTask.objects.filter(
                state__in=[EstimateTask.State.WAITING, EstimateTask.State.RUNNING],
            ).values_list("snapshot_id", flat=True)
        )
        retained_update_snapshot_ids = {
            uuid.UUID(snapshot_id) for snapshot_id in retained_snapshot_ids
        }
        retained_snapshot_ids.update(str(snapshot_id) for snapshot_id in active_snapshot_ids)
        EstimateTask.objects.filter(
            state__in=[EstimateTask.State.COMPLETED, EstimateTask.State.FAILED],
            assessment_date__lt=target_date,
        ).exclude(
            snapshot_id__in=retained_update_snapshot_ids,
        ).delete()
    return tuple(sorted(retained_snapshot_ids))


def _expired(*, forest_cell_id: int) -> EstimateRead:
    return EstimateRead(
        status=EstimateReadStatus.EXPIRED,
        forest_cell_id=forest_cell_id,
        message_key="estimate.update_expired",
    )


def _forest_cell_for_read(*, forest_cell_id: int, update: EstimateUpdate) -> ForestCell | None:
    return ForestCell.objects.filter(
        id=forest_cell_id,
        build_id=update.forest_build_id,
    ).first()


def read_estimate(
    *,
    update_id: uuid.UUID | str,
    forest_cell_id: int,
    target_date: date,
) -> EstimateRead:
    """Read one exact update without falling back to a newer publication."""
    _require_date(target_date, name="target_date")
    try:
        parsed_update_id = uuid.UUID(str(update_id))
    except (TypeError, ValueError):
        return _expired(forest_cell_id=forest_cell_id)
    update = EstimateUpdate.objects.filter(public_id=parsed_update_id).first()
    if update is None:
        return _expired(forest_cell_id=forest_cell_id)
    offset = (target_date - update.assessment_date).days
    if offset < 0 or offset > 7:
        raise ValueError("target_date must be from assessment day through day seven.")
    forest_cell = _forest_cell_for_read(forest_cell_id=forest_cell_id, update=update)
    if forest_cell is None:
        return EstimateRead(
            status=EstimateReadStatus.UNKNOWN,
            forest_cell_id=forest_cell_id,
            update_id=update.public_id,
            assessment_date=update.assessment_date,
            message_key="estimate.square_unknown",
        )
    if forest_cell.is_forest is None or forest_cell.forest_share is None:
        return EstimateRead(
            status=EstimateReadStatus.UNAVAILABLE,
            forest_cell_id=forest_cell_id,
            target_date=target_date,
            assessment_date=update.assessment_date,
            update_id=update.public_id,
            calculated_at=update.calculated_at,
            method_version=update.method_version,
            unavailable_reason="missing_forest_data",
            message_key="estimate.unavailable",
        )
    if forest_cell.is_forest is False or forest_cell.forest_share < 0.1:
        return EstimateRead(
            status=EstimateReadStatus.NO_FOREST,
            forest_cell_id=forest_cell_id,
            target_date=target_date,
            assessment_date=update.assessment_date,
            update_id=update.public_id,
            calculated_at=update.calculated_at,
            method_version=update.method_version,
            message_key="estimate.no_forest",
        )
    row = EstimateResult.objects.filter(update_id=update.id, forest_cell_id=forest_cell_id).first()
    if row is None:
        return EstimateRead(
            status=EstimateReadStatus.NO_RESULT_YET,
            forest_cell_id=forest_cell_id,
            target_date=target_date,
            assessment_date=update.assessment_date,
            update_id=update.public_id,
            calculated_at=update.calculated_at,
            method_version=update.method_version,
            message_key="estimate.result_not_ready",
        )
    decoded = decode_result_bytes(values=row.values)
    value = decoded[offset]
    return EstimateRead(
        status=(
            EstimateReadStatus.AVAILABLE
            if isinstance(value, int)
            else EstimateReadStatus.UNAVAILABLE
        ),
        forest_cell_id=forest_cell_id,
        target_date=target_date,
        assessment_date=update.assessment_date,
        update_id=update.public_id,
        percentage=value if isinstance(value, int) else None,
        unavailable_reason=value if isinstance(value, str) else None,
        calculated_at=update.calculated_at,
        method_version=update.method_version,
        message_key=(
            "estimate.available" if isinstance(value, int) else "estimate.unavailable"
        ),
    )


read_estimate_result = read_estimate
lookup_estimate = read_estimate


def facts_for_estimate(*, update_id: uuid.UUID | str, forest_cell_id: int, target_date: date) -> tuple[ForecastFact, ...]:
    """Return facts recalculated with the update's own method version and inputs."""
    update = EstimateUpdate.objects.select_related(
        "snapshot",
        "forest_build",
        "terrain_build",
    ).get(public_id=uuid.UUID(str(update_id)))
    method = METHODS_BY_VERSION.get(update.method_version)
    if method is None:
        raise EstimateUpdateError(
            f"Update {update.public_id} uses unknown method version {update.method_version!r}."
        )
    forest_cell = ForestCell.objects.get(id=forest_cell_id, build_id=update.forest_build_id)
    terrain_cell = TerrainCell.objects.filter(
        build_id=update.terrain_build_id,
        grid_build_cell_id=forest_cell.grid_build_cell_id,
    ).first()
    summary = WeatherSummary.objects.get(snapshot_id=update.snapshot_id, target_date=target_date)
    result = calculate_forecast(
        inputs=_forecast_inputs(
            snapshot=update.snapshot,
            summary=summary,
            forest_cell=forest_cell,
            terrain_cell=terrain_cell,
            terrain_build_id=update.terrain_build_id,
            daily_rows=_snapshot_daily_rows(snapshot=update.snapshot),
            host_trees=saved_host_trees(tree_inputs=update.tree_inputs, forest_cell_id=forest_cell.id)
            if isinstance(method, ForecastMethodV3Config) else None,
        ),
        method=method,
    )
    return result.ordered_facts
