"""Calculate every estimate of one region for one date and publish the finished run as a whole.

A run lists its weather cells and input versions first. Readers see the previous
published run until the next run passes validation and replaces the pointer.
"""

from __future__ import annotations

import logging
import resource
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import Count
from django.utils import timezone

from sporameteo.forecast_method import CURRENT_METHOD, ForecastResult
from sporameteo.weather.policy import PIPELINE_VERSION
from national_forest.models import ForestBuild, ForestCell
from national_grid.models import GridRegion
from national_terrain.models import TerrainBuild
from weather_requests.eligibility import (
    TUSCANY_ISTAT_CODE,
    WeatherEligibilityError,
    weather_cells_for_regions,
    weather_cell,
)
from weather_summaries.models import WeatherDaily, WeatherSnapshot, WeatherSnapshotDay

from .host_trees import calculation_version, host_tree_source_version
from .models import (
    EstimateResult,
    EstimateUpdate,
    ForecastRun,
    ForecastRunCell,
    PublishedForecastRun,
    TuscanyInputSet,
)
from .services import (
    FOREST_BUILD_SETTING,
    EstimateUpdateError,
    _terrain_build_version,
    calculate_estimate_update,
    publish_estimate_update,
)


logger = logging.getLogger(__name__)

TARGET_DAY_COUNT = 8
PROBLEM_LIMIT = 5


class ForecastRunError(EstimateUpdateError):
    """Report a run that cannot start, finish or be published."""


class ForecastRunValidationError(ForecastRunError, ValueError):
    """Report a finished run that fails coverage, date or input checks."""


@dataclass(frozen=True)
class PlannedCell:
    """Describe one weather cell of the plan and the weather snapshot it will use."""

    cell_key: str
    square_count: int
    snapshot_id: uuid.UUID | None


@dataclass(frozen=True)
class ForecastRunPlan:
    """Hold everything a run needs to know before any calculation starts."""

    region_code: str
    assessment_date: date
    target_dates: tuple[date, ...]
    grid_build_version: str
    forest_build_version: str
    terrain_build_version: str
    host_tree_source_version: str
    method_version: str
    calculation_version: str
    input_set_id: int | None
    cells: tuple[PlannedCell, ...] = field(default_factory=tuple)

    @property
    def square_count(self) -> int:
        """Return the number of forest squares across all cells."""
        return sum(cell.square_count for cell in self.cells)

    @property
    def cells_without_snapshot(self) -> tuple[str, ...]:
        """Return the cell keys that have no ready weather for the date."""
        return tuple(cell.cell_key for cell in self.cells if cell.snapshot_id is None)

    def as_dict(self) -> dict[str, Any]:
        """Return the plan as plain values for printing."""
        return {
            "region_code": self.region_code,
            "assessment_date": self.assessment_date.isoformat(),
            "target_dates": [value.isoformat() for value in self.target_dates],
            "grid_build_version": self.grid_build_version,
            "forest_build_version": self.forest_build_version,
            "terrain_build_version": self.terrain_build_version,
            "host_tree_source_version": self.host_tree_source_version,
            "method_version": self.method_version,
            "calculation_version": self.calculation_version,
            "cells_total": len(self.cells),
            "cells_with_weather": len(self.cells) - len(self.cells_without_snapshot),
            "squares_total": self.square_count,
            "cells": [
                {"cell_key": cell.cell_key, "squares": cell.square_count}
                for cell in self.cells
            ],
        }


@dataclass(frozen=True)
class PublishedRunRead:
    """Hold the current run of a region and its update ids by weather cell."""

    run: ForecastRun
    updates: dict[str, uuid.UUID]


def _require_region(*, forest_build: ForestBuild, region_code: str) -> None:
    if not GridRegion.objects.filter(build_id=forest_build.grid_build_id, istat_code=region_code).exists():
        raise ForecastRunError(
            f"Region code {region_code!r} is not in grid build {forest_build.grid_build.version!r}."
        )


def _square_counts(*, forest_build: ForestBuild, region_code: str) -> dict[str, int]:
    """Return the forest squares of each weather cell, keyed by cell key."""
    rows = (
        ForestCell.objects.filter(
            build=forest_build,
            is_forest=True,
            grid_build_cell__owner_region__istat_code=region_code,
        )
        .order_by()
        .values_list("weather_latitude", "weather_longitude")
        .annotate(squares=Count("id"))
    )
    counts: dict[str, int] = {}
    for latitude, longitude, squares in rows:
        key = weather_cell(latitude=latitude, longitude=longitude).key
        counts[key] = counts.get(key, 0) + squares
    return counts


def _target_dates(*, assessment_date: date) -> tuple[date, ...]:
    return tuple(assessment_date + timedelta(days=offset) for offset in range(TARGET_DAY_COUNT))


def _builds() -> tuple[ForestBuild, TerrainBuild]:
    forest_version = getattr(settings, FOREST_BUILD_SETTING, "")
    if not isinstance(forest_version, str) or not forest_version.strip():
        raise ForecastRunError(f"{FOREST_BUILD_SETTING} must name the completed forest build.")
    forest = ForestBuild.objects.select_related("grid_build").filter(version=forest_version).first()
    if forest is None:
        raise ForecastRunError(f"No forest build exists for {forest_version!r}.")
    terrain_version = _terrain_build_version()
    terrain = TerrainBuild.objects.filter(version=terrain_version).first()
    if terrain is None:
        raise ForecastRunError(f"No terrain build exists for {terrain_version!r}.")
    return forest, terrain


def _input_set(*, forest: ForestBuild, terrain: TerrainBuild, region_code: str, trees: str) -> TuscanyInputSet | None:
    if not trees:
        return None
    return TuscanyInputSet.objects.filter(
        region_code=region_code,
        grid_build_version=forest.grid_build.version,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
        host_tree_source_version=trees,
    ).first()


def _plan(*, assessment_date: date, region_code: str) -> tuple[ForecastRunPlan, ForestBuild, TerrainBuild, TuscanyInputSet | None]:
    forest, terrain = _builds()
    _require_region(forest_build=forest, region_code=region_code)
    trees = host_tree_source_version()
    input_set = _input_set(forest=forest, terrain=terrain, region_code=region_code, trees=trees)
    try:
        weather_cells = weather_cells_for_regions(
            forest_build_version=forest.version,
            region_codes=[region_code],
        )
    except WeatherEligibilityError as exc:
        raise ForecastRunError(str(exc)) from exc
    counts = _square_counts(forest_build=forest, region_code=region_code)
    snapshots = dict(
        WeatherSnapshot.objects.filter(
            cell_key__in=[cell.key for cell in weather_cells],
            assessment_date=assessment_date,
            pipeline_version=PIPELINE_VERSION,
            status=WeatherSnapshot.Status.READY,
        ).values_list("cell_key", "id")
    )
    plan = ForecastRunPlan(
        region_code=region_code,
        assessment_date=assessment_date,
        target_dates=_target_dates(assessment_date=assessment_date),
        grid_build_version=forest.grid_build.version,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
        host_tree_source_version=trees,
        method_version=CURRENT_METHOD.version,
        calculation_version=calculation_version(),
        input_set_id=input_set.id if input_set else None,
        cells=tuple(
            PlannedCell(
                cell_key=cell.key,
                square_count=counts.get(cell.key, 0),
                snapshot_id=snapshots.get(cell.key),
            )
            for cell in weather_cells
        ),
    )
    return plan, forest, terrain, input_set


def plan_forecast_run(
    *, assessment_date: date, region_code: str = TUSCANY_ISTAT_CODE,
) -> ForecastRunPlan:
    """List the cells, squares, dates and input versions of a run without writing."""
    return _plan(assessment_date=assessment_date, region_code=region_code)[0]


def start_forecast_run(
    *, assessment_date: date, region_code: str = TUSCANY_ISTAT_CODE,
) -> ForecastRun:
    """Create the run and its cells, or return the unpublished run with the same inputs."""
    plan, forest, terrain, input_set = _plan(
        assessment_date=assessment_date,
        region_code=region_code,
    )
    missing = plan.cells_without_snapshot
    if missing:
        raise ForecastRunError(
            f"{len(missing)} of {len(plan.cells)} weather cells have no ready weather for "
            f"{assessment_date.isoformat()}. Run refresh_weather first."
        )
    with transaction.atomic():
        run, created = ForecastRun.objects.select_for_update().get_or_create(
            region_code=region_code,
            assessment_date=assessment_date,
            forest_build=forest,
            terrain_build=terrain,
            calculation_version=plan.calculation_version,
            defaults={
                "input_set": input_set,
                "host_tree_source_version": plan.host_tree_source_version,
                "method_version": plan.method_version,
                "target_dates": [value.isoformat() for value in plan.target_dates],
                "expected_cell_count": len(plan.cells),
                "expected_square_count": plan.square_count,
            },
        )
        if created:
            ForecastRunCell.objects.bulk_create(
                [
                    ForecastRunCell(
                        run=run,
                        cell_key=cell.cell_key,
                        snapshot_id=cell.snapshot_id,
                        expected_square_count=cell.square_count,
                    )
                    for cell in plan.cells
                ]
            )
        elif run.status == ForecastRun.Status.FAILED:
            run.status = ForecastRun.Status.RUNNING
            run.failure_reason = ""
            run.save(update_fields=["status", "failure_reason"])
    return run


def _peak_memory_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes and Linux reports kilobytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _source_id(*, update_inputs: dict[str, Any]) -> str:
    source = update_inputs.get("source") if isinstance(update_inputs, dict) else None
    return str(source.get("source_id", "")) if isinstance(source, dict) else ""


def _tree_source_matches(*, run: ForecastRun, tree_inputs: dict[str, Any]) -> bool:
    """Accept the run's JRC source, or forest only values when the run has no JRC source."""
    if not run.host_tree_source_version:
        return tree_inputs == {}
    return _source_id(update_inputs=tree_inputs) == f"jrc:{run.host_tree_source_version}"


def _process_cell(
    *,
    run: ForecastRun,
    cell: ForecastRunCell,
    calculator: Callable[..., ForecastResult] | None,
) -> None:
    prepared = calculate_estimate_update(
        snapshot_id=cell.snapshot_id,
        cell_key=cell.cell_key,
        assessment_date=run.assessment_date,
        forest_build_version=run.forest_build.version,
        terrain_build_version=run.terrain_build.version,
        calculator=calculator,
        region_code=run.region_code,
    )
    if not _tree_source_matches(run=run, tree_inputs=prepared.tree_inputs):
        raise ForecastRunError(
            "Host-tree values did not match the source of the run "
            f"({run.host_tree_source_version or 'forest only'})."
        )
    published = publish_estimate_update(prepared=prepared)
    if published.skipped_older:
        raise ForecastRunError("A newer estimate for this cell already exists.")
    gap_days = WeatherSnapshotDay.objects.filter(
        snapshot_id=cell.snapshot_id,
        daily__status=WeatherDaily.Status.CHECKED_KNOWN_MISSING,
    ).count()
    ForecastRunCell.objects.filter(id=cell.id).update(
        state=ForecastRunCell.State.COMPLETED,
        update=published.update,
        source_gap_days=gap_days,
        failure_reason="",
    )


def process_forecast_run(
    *,
    run_id: uuid.UUID | str,
    calculator: Callable[..., ForecastResult] | None = None,
) -> ForecastRun:
    """Calculate the unfinished cells of a run and publish it when all are complete."""
    started = time.monotonic()
    run = ForecastRun.objects.select_related("forest_build", "terrain_build").get(
        id=uuid.UUID(str(run_id)),
    )
    if run.status == ForecastRun.Status.PUBLISHED:
        return run
    ForecastRun.objects.filter(id=run.id).update(
        status=ForecastRun.Status.RUNNING,
        failure_reason="",
        started_at=run.started_at or timezone.now(),
    )
    pending = (
        run.cells.exclude(state=ForecastRunCell.State.COMPLETED)
        .order_by("cell_key")
    )
    for cell in pending:
        try:
            _process_cell(run=run, cell=cell, calculator=calculator)
        except Exception as exc:
            ForecastRunCell.objects.filter(id=cell.id).update(
                state=ForecastRunCell.State.FAILED,
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
    completed = run.cells.filter(state=ForecastRunCell.State.COMPLETED).count()
    failed = list(
        run.cells.filter(state=ForecastRunCell.State.FAILED)
        .order_by("cell_key")
        .values_list("cell_key", "failure_reason")
    )
    status = ForecastRun.Status.COMPLETED
    reason = ""
    if failed:
        status = ForecastRun.Status.FAILED
        shown = "; ".join(f"{key}: {text}" for key, text in failed[:PROBLEM_LIMIT])
        reason = f"{len(failed)} weather cells failed. {shown}"
    ForecastRun.objects.filter(id=run.id).update(
        status=status,
        failure_reason=reason,
        completed_cell_count=completed,
        completed_at=timezone.now() if not failed else None,
        runtime_seconds=(run.runtime_seconds or 0.0) + (time.monotonic() - started),
        peak_memory_mb=max(run.peak_memory_mb or 0.0, _peak_memory_mb()),
    )
    if not failed:
        try:
            publish_forecast_run(run_id=run.id)
        except ForecastRunError as exc:
            ForecastRun.objects.filter(id=run.id).update(
                status=ForecastRun.Status.FAILED,
                failure_reason=str(exc),
                completed_at=None,
            )
    return ForecastRun.objects.get(id=run.id)


def validate_forecast_run(*, run: ForecastRun) -> int:
    """Check coverage, dates and input identities and return the cells with source gaps."""
    _require_region(forest_build=run.forest_build, region_code=run.region_code)
    problems: list[str] = []
    expected_dates = [value.isoformat() for value in _target_dates(assessment_date=run.assessment_date)]
    if run.target_dates != expected_dates:
        problems.append("The run target dates are not the assessment date and the seven days after.")
    try:
        current = {
            cell.key
            for cell in weather_cells_for_regions(
                forest_build_version=run.forest_build.version,
                region_codes=[run.region_code],
            )
        }
    except WeatherEligibilityError as exc:
        raise ForecastRunValidationError(str(exc)) from exc
    counts = _square_counts(forest_build=run.forest_build, region_code=run.region_code)
    cells = list(run.cells.select_related("update", "snapshot"))
    keys = {cell.cell_key for cell in cells}
    if keys != current:
        problems.append(
            f"The run cells do not match the Tuscany weather cells: missing {sorted(current - keys)}, "
            f"unexpected {sorted(keys - current)}."
        )
    result_counts = dict(
        EstimateResult.objects.filter(update_id__in=[cell.update_id for cell in cells if cell.update_id])
        .order_by()
        .values_list("update_id")
        .annotate(rows=Count("forest_cell_id"))
    )
    for cell in sorted(cells, key=lambda value: value.cell_key):
        update = cell.update
        label = f"Cell {cell.cell_key}"
        if cell.state != ForecastRunCell.State.COMPLETED or update is None:
            problems.append(f"{label} has no completed update.")
            continue
        expected_squares = counts.get(cell.cell_key, 0)
        checks = (
            (update.cell_key == cell.cell_key, "update belongs to another weather cell"),
            (update.assessment_date == run.assessment_date, "update has another assessment date"),
            (
                cell.snapshot.assessment_date == run.assessment_date
                and update.snapshot_id == cell.snapshot_id,
                "update uses weather from another date or snapshot",
            ),
            (update.forest_build_id == run.forest_build_id, "update uses another forest build"),
            (update.terrain_build_id == run.terrain_build_id, "update uses another terrain build"),
            (update.method_version == run.method_version, "update uses another method version"),
            (update.region_code == run.region_code, "update has another region"),
            (
                _tree_source_matches(run=run, tree_inputs=update.tree_inputs),
                "update uses another host-tree source",
            ),
            (
                cell.expected_square_count == expected_squares == update.row_count,
                "update does not cover the Tuscany squares of the cell",
            ),
            (
                result_counts.get(update.id, 0) == update.row_count,
                "update is missing result rows",
            ),
        )
        problems.extend(f"{label}: {text}." for passed, text in checks if not passed)
    if problems:
        shown = " ".join(problems[:PROBLEM_LIMIT])
        raise ForecastRunValidationError(
            f"The run is not valid, {len(problems)} problems. {shown}"
        )
    return (
        WeatherSnapshotDay.objects.filter(
            snapshot_id__in=[cell.snapshot_id for cell in cells],
            daily__status=WeatherDaily.Status.CHECKED_KNOWN_MISSING,
        )
        .values("snapshot_id")
        .distinct()
        .count()
    )


def publish_forecast_run(*, run_id: uuid.UUID | str) -> ForecastRun:
    """Validate a run and make it the published run of its region in one transaction."""
    with transaction.atomic():
        run = ForecastRun.objects.select_for_update().select_related(
            "forest_build", "terrain_build",
        ).get(id=uuid.UUID(str(run_id)))
        pointer = PublishedForecastRun.objects.select_for_update().filter(
            region_code=run.region_code,
        ).select_related("current_run").first()
        gap_cells = validate_forecast_run(run=run)
        now = timezone.now()
        if pointer is None:
            try:
                with transaction.atomic():
                    PublishedForecastRun.objects.create(
                        region_code=run.region_code,
                        current_run=run,
                        previous_run=None,
                        published_at=now,
                    )
            except IntegrityError as exc:
                raise ForecastRunError(
                    "Another run was published at the same time. Run again."
                ) from exc
        elif pointer.current_run_id != run.id:
            if run.assessment_date < pointer.current_run.assessment_date:
                raise ForecastRunError(
                    f"The published run is for {pointer.current_run.assessment_date.isoformat()}, "
                    f"which is newer than {run.assessment_date.isoformat()}."
                )
            pointer.previous_run_id = pointer.current_run_id
            pointer.current_run = run
            pointer.published_at = now
            pointer.save(update_fields=["previous_run", "current_run", "published_at"])
        ForecastRun.objects.filter(id=run.id).update(
            status=ForecastRun.Status.PUBLISHED,
            failure_reason="",
            completed_cell_count=run.cells.filter(state=ForecastRunCell.State.COMPLETED).count(),
            source_gap_cell_count=gap_cells,
            completed_at=run.completed_at or now,
            published_at=now,
        )
        transaction.on_commit(lambda: _queue_map_cache_warming(region_code=run.region_code))
    return ForecastRun.objects.get(id=run.id)


def _queue_map_cache_warming(*, region_code: str) -> None:
    """Queue the cache warming task, and keep the publication when queueing fails."""
    from .tasks import warm_published_map_cache_task

    try:
        warm_published_map_cache_task.enqueue(region_code=region_code)
    except Exception:
        logger.warning("Could not queue the map cache warming task.", exc_info=True)


def published_forecast_run(*, region_code: str) -> PublishedRunRead | None:
    """Return the current run and its update ids, read from one pointer value."""
    pointer = PublishedForecastRun.objects.select_related("current_run").filter(
        region_code=region_code,
    ).first()
    if pointer is None:
        return None
    updates = dict(
        ForecastRunCell.objects.filter(run_id=pointer.current_run_id)
        .exclude(update_id=None)
        .values_list("cell_key", "update__public_id")
    )
    return PublishedRunRead(run=pointer.current_run, updates=updates)


def forecast_run_report(*, run: ForecastRun) -> dict[str, Any]:
    """Return the size, runtime, memory and database use of a run for printing."""
    run.refresh_from_db()
    pointer = PublishedForecastRun.objects.filter(region_code=run.region_code).first()
    rows = EstimateResult.objects.filter(update__forecast_run_cells__run=run).count()
    storage_bytes = 0
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            for model in (EstimateUpdate, EstimateResult):
                cursor.execute("SELECT pg_total_relation_size(%s)", [model._meta.db_table])
                storage_bytes += cursor.fetchone()[0]
    return {
        "run_id": str(run.id),
        "status": run.status,
        "assessment_date": run.assessment_date.isoformat(),
        "cells": run.expected_cell_count,
        "completed_cells": run.completed_cell_count,
        "squares": run.expected_square_count,
        "result_rows": rows,
        "source_gap_cells": run.source_gap_cell_count,
        "runtime_seconds": round(run.runtime_seconds or 0.0, 2),
        "peak_memory_mb": round(run.peak_memory_mb or 0.0, 1),
        "estimate_storage_bytes": storage_bytes,
        "failure_reason": run.failure_reason,
        "current_run_id": str(pointer.current_run_id) if pointer else "",
        "previous_run_id": str(pointer.previous_run_id) if pointer and pointer.previous_run_id else "",
    }
