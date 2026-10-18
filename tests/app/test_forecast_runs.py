"""Check Tuscany forecast runs: the plan, calculation, validation, publication and cleanup."""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import ProtectedError
from django_tasks_db.models import DBTaskResult

import weather_on_demand.services as weather_services
import weather_requests.provider as weather_provider
from estimate_updates.contracts import UNAVAILABLE_REASON_CODES, decode_result_bytes
from estimate_updates.forecast_runs import (
    ForecastRunError,
    ForecastRunValidationError,
    plan_forecast_run,
    process_forecast_run,
    publish_forecast_run,
    published_forecast_run,
    start_forecast_run,
    validate_forecast_run,
)
from estimate_updates.models import (
    EstimateResult,
    EstimateUpdate,
    ForecastRun,
    ForecastRunCell,
    PublishedForecastRun,
    TuscanyInputSet,
)
from estimate_updates.services import (
    calculate_and_publish_estimates,
    calculate_estimate_update,
    cleanup_estimate_updates,
    publish_estimate_update,
)
from estimate_updates.tasks import tuscany_forecast_task
from sporameteo.finds.grid import format_cell_id
from sporameteo.forecast_method import CURRENT_METHOD, HostTrees, calculate_forecast
from sporameteo.weather.policy import PIPELINE_VERSION
from national_forest.models import ForestBuild, ForestCell, ForestChunk
from national_grid.models import GridBuild, GridBuildCell, GridCell, GridChunk, GridRegion
from national_terrain.models import TerrainBuild, TerrainCell, TerrainChunk
from tests.app.test_estimate_updates import _create_followup_snapshot
from weather_requests.models import WeatherRequestAttempt
from weather_summaries.models import WeatherDaily, WeatherSnapshot, WeatherSnapshotDay

pytestmark = pytest.mark.django_db(transaction=True)

DAY = date(2026, 10, 11)
FIRST_CELL = "43.50,11.20"
SECOND_CELL = "43.60,11.20"
TREE_VERSION = "a" * 64


def _square(*, west: int, south: int) -> Polygon:
    return Polygon(
        [(west, south), (west + 500, south), (west + 500, south + 500), (west, south + 500), (west, south)]
    )


def _snapshot(*, cell_key: str, day: date) -> WeatherSnapshot:
    source = SimpleNamespace(
        cell_key=cell_key,
        pipeline_version=PIPELINE_VERSION,
        reference_elevation_m=200.0,
        latest_retrieval=datetime(2026, 10, 11, 10, tzinfo=UTC),
    )
    return _create_followup_snapshot(source_snapshot=source, assessment_date=day)


def _weather(*, day: date) -> dict[str, WeatherSnapshot]:
    return {key: _snapshot(cell_key=key, day=day) for key in (FIRST_CELL, SECOND_CELL)}


@pytest.fixture(autouse=True)
def block_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if any code path asks a weather provider for data."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Estimate runs must not call a weather provider.")

    monkeypatch.setattr(weather_provider, "_default_transport", forbidden)
    monkeypatch.setattr(weather_services, "request_weather", forbidden)


@pytest.fixture(autouse=True)
def local_host_trees(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve host-tree values from a fake local source instead of JRC files."""
    source = {"source_year": 2006, "resolution_m": 1000}
    monkeypatch.setattr("estimate_updates.host_trees.read_manifest", lambda **kwargs: (source, TREE_VERSION))
    monkeypatch.setattr(
        "estimate_updates.host_trees.sample_squares",
        lambda **kwargs: {
            key: HostTrees(
                values=(0.8, 0.0, None, 0.0, 0.0),
                source_id=f"jrc:{TREE_VERSION}",
                source_year=2006,
                resolution_m=1000,
            )
            for key in kwargs["centres"]
        },
    )


@pytest.fixture
def tuscany(settings) -> SimpleNamespace:
    """Build two Tuscany weather cells, a Lazio square in the first one and a non-forest square."""
    suffix = uuid.uuid4().hex[:10]
    west, south = 4_300_000, 2_300_000
    grid = GridBuild.objects.create(
        version=f"grid-{suffix}",
        fingerprint="d" * 64,
        status=GridBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=5,
    )
    tuscan_region = GridRegion.objects.create(
        build=grid, istat_code="09", name="Toscana", geometry=MultiPolygon([_square(west=west, south=south)], srid=3035),
    )
    lazio_region = GridRegion.objects.create(
        build=grid, istat_code="12", name="Lazio", geometry=MultiPolygon([_square(west=west + 5_000, south=south)], srid=3035),
    )
    grid_chunk = GridChunk.objects.create(
        build=grid, west_e=west, south_n=south, east_e=west + 2_500, north_n=south + 500,
        status=GridChunk.Status.COMPLETED, cell_count=5,
    )
    forest = ForestBuild.objects.create(
        version=f"forest-{suffix}",
        grid_build=grid,
        fingerprint="e" * 64,
        status=ForestBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=5,
        forest_cell_count=4,
    )
    forest_chunk = ForestChunk.objects.create(
        build=forest, grid_chunk=grid_chunk, west_e=west, south_n=south, east_e=west + 2_500,
        north_n=south + 500, status=ForestChunk.Status.COMPLETED, cell_count=5,
    )
    terrain = TerrainBuild.objects.create(
        version=f"terrain-{suffix}",
        grid_build=grid,
        algorithm_version="national-terrain-v2",
        fingerprint="c" * 64,
        grid_fingerprint=grid.fingerprint,
        status=TerrainBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        result_count=5,
    )
    terrain_chunk = TerrainChunk.objects.create(
        build=terrain, grid_chunk=grid_chunk, status=TerrainChunk.Status.COMPLETED, result_count=5,
    )
    squares = (
        (tuscan_region, True, 43.5, 11.2),
        (tuscan_region, True, 43.5, 11.2),
        (tuscan_region, True, 43.6, 11.2),
        (lazio_region, True, 43.5, 11.2),
        (tuscan_region, False, None, None),
    )
    forest_cells = []
    for index, (region, is_forest, latitude, longitude) in enumerate(squares):
        corner_e = west + index * 500
        grid_cell = GridCell.objects.create(
            cell_id=format_cell_id(corner_e=corner_e, corner_n=south),
            corner_e=corner_e,
            corner_n=south,
            geometry=_square(west=corner_e, south=south),
        )
        membership = GridBuildCell.objects.create(
            build=grid, cell=grid_cell, chunk=grid_chunk, owner_region=region,
            land_geometry=MultiPolygon([_square(west=corner_e, south=south)], srid=3035), land_share=1.0,
        )
        forest_cells.append(
            ForestCell.objects.create(
                build=forest,
                grid_build_cell=membership,
                chunk=forest_chunk,
                total_pixel_count=100,
                valid_pixel_count=100,
                forest_pixel_count=80 if is_forest else 0,
                broadleaf_pixel_count=80 if is_forest else 0,
                valid_coverage=1.0,
                coverage_quality="high",
                forest_share=0.8 if is_forest else 0.0,
                broadleaf_share=1.0 if is_forest else 0.0,
                leaf_type=ForestCell.LeafType.BROADLEAF if is_forest else ForestCell.LeafType.NO_FOREST,
                is_forest=is_forest,
                weather_latitude=latitude,
                weather_longitude=longitude,
            )
        )
        TerrainCell.objects.create(
            build=terrain, grid_build_cell=membership, chunk=terrain_chunk, altitude_m=500.0, slope_deg=0.0,
            aspect_deg=None, altitude_valid_fraction=1.0, slope_valid_fraction=1.0, aspect_valid_fraction=1.0,
            altitude_quality="high", slope_quality="high", aspect_quality="high",
        )
    input_set = TuscanyInputSet.objects.create(
        region_code="09",
        grid_build_version=grid.version,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
        host_tree_source_version=TREE_VERSION,
        eligible_square_count=4,
        host_tree_row_count=4,
    )
    settings.SPORAMETEO_FOREST_BUILD_VERSION = forest.version
    settings.SPORAMETEO_TERRAIN_BUILD_VERSION = terrain.version
    return SimpleNamespace(
        grid=grid, forest=forest, terrain=terrain, input_set=input_set, forest_cells=forest_cells,
    )


def _publish_day(*, day: date) -> ForecastRun:
    """Store weather for one date and run the whole Tuscany forecast."""
    _weather(day=day)
    return process_forecast_run(run_id=start_forecast_run(assessment_date=day).id)


def _recording_calculator(
    *, seen: list[str], fail_for: set[str] | None = None, watch: Callable[[], None] | None = None,
) -> Callable[..., object]:
    """Wrap the real formula and record the weather snapshot of each calculation."""

    def calculator(*, inputs: object) -> object:
        snapshot_id = inputs.daily_weather_source_id.rsplit(":", maxsplit=1)[1]
        seen.append(snapshot_id)
        if watch is not None:
            watch()
        if fail_for and snapshot_id in fail_for:
            raise ValueError("The calculator failed on purpose.")
        return calculate_forecast(inputs=inputs)

    return calculator


# AC1: the plan lists the expected set before calculation


def test_plan_lists_only_tuscany_cells_and_squares_with_dates_and_versions(
    tuscany: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _weather(day=DAY)

    def forbidden(**kwargs: object) -> None:
        raise AssertionError("A plan must not calculate anything.")

    monkeypatch.setattr("estimate_updates.forecast_runs.calculate_estimate_update", forbidden)

    plan = plan_forecast_run(assessment_date=DAY)

    assert [(cell.cell_key, cell.square_count) for cell in plan.cells] == [(FIRST_CELL, 2), (SECOND_CELL, 1)]
    assert plan.square_count == 3
    assert all(cell.snapshot_id is not None for cell in plan.cells)
    assert plan.target_dates == tuple(DAY + timedelta(days=offset) for offset in range(8))
    assert plan.grid_build_version == tuscany.grid.version
    assert plan.forest_build_version == tuscany.forest.version
    assert plan.terrain_build_version == tuscany.terrain.version
    assert plan.host_tree_source_version == TREE_VERSION
    assert plan.method_version == CURRENT_METHOD.version
    assert plan.input_set_id == tuscany.input_set.id
    assert ForecastRun.objects.count() == 0
    assert EstimateUpdate.objects.count() == 0
    assert plan.as_dict()["squares_total"] == 3


def test_run_records_the_matching_input_set_and_tree_source(tuscany: SimpleNamespace) -> None:
    run = _publish_day(day=DAY)

    assert run.input_set_id == tuscany.input_set.id
    assert run.host_tree_source_version == TREE_VERSION


def test_run_without_an_input_set_row_still_runs_with_host_trees(tuscany: SimpleNamespace) -> None:
    tuscany.input_set.delete()
    _weather(day=DAY)
    assert plan_forecast_run(assessment_date=DAY).input_set_id is None

    run = process_forecast_run(run_id=start_forecast_run(assessment_date=DAY).id)

    assert run.status == ForecastRun.Status.PUBLISHED
    assert run.input_set_id is None
    assert run.host_tree_source_version == TREE_VERSION
    assert EstimateUpdate.objects.get(cell_key=FIRST_CELL).tree_inputs["source"]["source_id"] == f"jrc:{TREE_VERSION}"


def test_run_without_jrc_files_uses_forest_only_and_requires_no_tree_values(
    tuscany: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("estimate_updates.host_trees.read_manifest", lambda **kwargs: None)
    _weather(day=DAY)

    run = process_forecast_run(run_id=start_forecast_run(assessment_date=DAY).id)

    assert run.status == ForecastRun.Status.PUBLISHED
    assert run.input_set_id is None
    assert run.host_tree_source_version == ""
    assert all(update.tree_inputs == {} for update in EstimateUpdate.objects.all())
    assert validate_forecast_run(run=run) == 0


def test_jrc_files_that_appear_after_the_plan_fail_the_cells_as_incompatible(
    tuscany: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {"source_year": 2006, "resolution_m": 1000}
    monkeypatch.setattr("estimate_updates.host_trees.read_manifest", lambda **kwargs: None)
    _weather(day=DAY)
    run = start_forecast_run(assessment_date=DAY)
    assert run.host_tree_source_version == ""
    monkeypatch.setattr("estimate_updates.host_trees.read_manifest", lambda **kwargs: (source, TREE_VERSION))

    run = process_forecast_run(run_id=run.id)

    assert run.status == ForecastRun.Status.FAILED
    assert run.completed_at is None
    assert set(run.cells.values_list("state", flat=True)) == {ForecastRunCell.State.FAILED}
    assert "did not match the source" in run.cells.first().failure_reason
    assert not PublishedForecastRun.objects.exists()


def test_start_refuses_when_a_cell_has_no_ready_weather(tuscany: SimpleNamespace) -> None:
    _snapshot(cell_key=FIRST_CELL, day=DAY)

    with pytest.raises(ForecastRunError, match="1 of 2 weather cells"):
        start_forecast_run(assessment_date=DAY)

    assert ForecastRun.objects.count() == 0


# AC2: calculation with provider access blocked, Tuscany only


def test_run_stores_eight_values_for_every_tuscany_square_and_no_other_square(tuscany: SimpleNamespace) -> None:
    run = _publish_day(day=DAY)

    assert run.status == ForecastRun.Status.PUBLISHED
    assert run.expected_cell_count == run.completed_cell_count == 2
    assert run.expected_square_count == 3
    updates = {update.cell_key: update for update in EstimateUpdate.objects.all()}
    assert set(updates) == {FIRST_CELL, SECOND_CELL}
    assert {update.region_code for update in updates.values()} == {"09"}
    assert {update.row_count for update in updates.values()} == {2, 1}
    lazio_square = tuscany.forest_cells[3]
    non_forest_square = tuscany.forest_cells[4]
    stored = EstimateResult.objects.values_list("forest_cell_id", flat=True)
    assert sorted(stored) == sorted(cell.id for cell in tuscany.forest_cells[:3])
    assert lazio_square.id not in stored
    assert non_forest_square.id not in stored
    for result in EstimateResult.objects.all():
        values = decode_result_bytes(values=result.values)
        assert len(values) == 8
        assert all(isinstance(value, int) or value in UNAVAILABLE_REASON_CODES for value in values)
    assert WeatherRequestAttempt.objects.count() == 0


def test_run_uses_the_current_formula_unchanged(tuscany: SimpleNamespace) -> None:
    snapshots = _weather(day=DAY)
    seen: list[str] = []
    run = start_forecast_run(assessment_date=DAY)

    process_forecast_run(run_id=run.id, calculator=_recording_calculator(seen=seen))

    assert set(seen) == {str(snapshot.id) for snapshot in snapshots.values()}
    assert len(seen) == 3 * 8
    direct = calculate_estimate_update(
        snapshot_id=snapshots[FIRST_CELL].id,
        cell_key=FIRST_CELL,
        assessment_date=DAY,
        forest_build_version=tuscany.forest.version,
        terrain_build_version=tuscany.terrain.version,
        region_code="09",
    )
    stored = EstimateUpdate.objects.get(cell_key=FIRST_CELL)
    assert stored.input_fingerprint == direct.input_fingerprint
    assert {row.forest_cell_id: bytes(row.values) for row in direct.rows} == {
        result.forest_cell_id: bytes(result.values) for result in stored.results.all()
    }


@pytest.mark.parametrize("region_code", ["99", "", "9"])
def test_unknown_regions_are_rejected_by_plan_and_start(
    tuscany: SimpleNamespace, region_code: str,
) -> None:
    _weather(day=DAY)

    with pytest.raises(ForecastRunError):
        plan_forecast_run(assessment_date=DAY, region_code=region_code)
    with pytest.raises(ForecastRunError):
        start_forecast_run(assessment_date=DAY, region_code=region_code)
    assert ForecastRun.objects.count() == 0
    assert EstimateUpdate.objects.count() == 0


def test_a_cell_without_host_tree_values_fails_instead_of_using_forest_only(
    tuscany: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _weather(day=DAY)
    run = start_forecast_run(assessment_date=DAY)
    monkeypatch.setattr("estimate_updates.services.tree_inputs_for_area", lambda **kwargs: {})

    run = process_forecast_run(run_id=run.id)

    assert run.status == ForecastRun.Status.FAILED
    assert not PublishedForecastRun.objects.exists()
    assert "did not match the source" in run.cells.first().failure_reason


# AC3: readers see one complete run


def test_readers_see_the_previous_run_until_the_next_run_is_published(tuscany: SimpleNamespace) -> None:
    first = _publish_day(day=DAY)
    before = published_forecast_run(region_code="09")
    assert before.run.id == first.id
    assert set(before.updates) == {FIRST_CELL, SECOND_CELL}
    _weather(day=DAY + timedelta(days=1))
    second = start_forecast_run(assessment_date=DAY + timedelta(days=1))
    observed: list[uuid.UUID] = []

    def watch() -> None:
        observed.append(published_forecast_run(region_code="09").run.id)

    process_forecast_run(
        run_id=second.id, calculator=_recording_calculator(seen=[], watch=watch),
    )

    assert observed
    assert set(observed) == {first.id}
    after = published_forecast_run(region_code="09")
    assert after.run.id == second.id
    assert set(after.updates.values()) == set(
        EstimateUpdate.objects.filter(assessment_date=DAY + timedelta(days=1)).values_list("public_id", flat=True)
    )
    assert after.updates != before.updates
    pointer = PublishedForecastRun.objects.get(region_code="09")
    assert pointer.previous_run_id == first.id


def test_published_reader_returns_nothing_before_the_first_publication(tuscany: SimpleNamespace) -> None:
    assert published_forecast_run(region_code="09") is None


# AC4: failure, restart and unchanged published reference


def test_failed_run_keeps_the_published_run_and_a_restart_finishes_only_unfinished_cells(
    tuscany: SimpleNamespace,
) -> None:
    first = _publish_day(day=DAY)
    pointer = PublishedForecastRun.objects.get(region_code="09")
    first_updates = set(EstimateUpdate.objects.values_list("id", flat=True))
    next_day = DAY + timedelta(days=1)
    snapshots = _weather(day=next_day)
    second = start_forecast_run(assessment_date=next_day)

    failed = process_forecast_run(
        run_id=second.id,
        calculator=_recording_calculator(seen=[], fail_for={str(snapshots[SECOND_CELL].id)}),
    )

    assert failed.status == ForecastRun.Status.FAILED
    assert failed.completed_cell_count == 1
    assert SECOND_CELL in failed.failure_reason
    assert dict(failed.cells.values_list("cell_key", "state")) == {
        FIRST_CELL: ForecastRunCell.State.COMPLETED,
        SECOND_CELL: ForecastRunCell.State.FAILED,
    }
    same_pointer = PublishedForecastRun.objects.get(region_code="09")
    assert (same_pointer.current_run_id, same_pointer.published_at) == (first.id, pointer.published_at)
    assert same_pointer.previous_run_id is None
    first.refresh_from_db()
    assert first.status == ForecastRun.Status.PUBLISHED
    assert first.assessment_date == DAY
    assert first_updates <= set(EstimateUpdate.objects.values_list("id", flat=True))
    assert published_forecast_run(region_code="09").run.id == first.id

    resumed = start_forecast_run(assessment_date=next_day)
    assert resumed.id == second.id
    assert resumed.status == ForecastRun.Status.RUNNING
    seen: list[str] = []
    done = process_forecast_run(run_id=resumed.id, calculator=_recording_calculator(seen=seen))

    assert set(seen) == {str(snapshots[SECOND_CELL].id)}
    assert done.status == ForecastRun.Status.PUBLISHED
    assert PublishedForecastRun.objects.get(region_code="09").current_run_id == second.id
    assert EstimateUpdate.objects.filter(assessment_date=next_day).count() == 2
    assert EstimateUpdate.objects.count() == 4


def test_restart_after_an_update_was_stored_but_not_recorded_creates_no_duplicate(
    tuscany: SimpleNamespace,
) -> None:
    snapshots = _weather(day=DAY)
    run = start_forecast_run(assessment_date=DAY)
    stored = publish_estimate_update(
        prepared=calculate_estimate_update(
            snapshot_id=snapshots[FIRST_CELL].id,
            cell_key=FIRST_CELL,
            assessment_date=DAY,
            forest_build_version=tuscany.forest.version,
            terrain_build_version=tuscany.terrain.version,
            region_code="09",
        )
    ).update
    assert run.cells.get(cell_key=FIRST_CELL).state == ForecastRunCell.State.WAITING

    done = process_forecast_run(run_id=run.id)

    assert done.status == ForecastRun.Status.PUBLISHED
    assert done.cells.get(cell_key=FIRST_CELL).update_id == stored.id
    assert EstimateUpdate.objects.filter(cell_key=FIRST_CELL).count() == 1
    assert EstimateUpdate.objects.count() == 2
    assert EstimateResult.objects.filter(update=stored).count() == 2


def test_processing_a_published_run_again_changes_nothing(tuscany: SimpleNamespace) -> None:
    run = _publish_day(day=DAY)
    published_at = PublishedForecastRun.objects.get().published_at

    again = process_forecast_run(run_id=run.id)

    assert again.status == ForecastRun.Status.PUBLISHED
    assert PublishedForecastRun.objects.get().published_at == published_at
    assert EstimateUpdate.objects.count() == 2


def test_run_records_runtime_and_peak_memory(tuscany: SimpleNamespace) -> None:
    run = _publish_day(day=DAY)

    assert run.runtime_seconds is not None and run.runtime_seconds > 0
    assert run.peak_memory_mb is not None and run.peak_memory_mb > 0
    assert run.started_at is not None and run.completed_at is not None and run.published_at is not None


# AC5: validation


def _tamper_missing_cell(run: ForecastRun, tuscany: SimpleNamespace) -> None:
    run.cells.filter(cell_key=SECOND_CELL).delete()


def _tamper_unfinished_cell(run: ForecastRun, tuscany: SimpleNamespace) -> None:
    run.cells.filter(cell_key=SECOND_CELL).update(state=ForecastRunCell.State.WAITING)


def _tamper_assessment_date(run: ForecastRun, tuscany: SimpleNamespace) -> None:
    EstimateUpdate.objects.filter(cell_key=SECOND_CELL).update(assessment_date=DAY - timedelta(days=1))


def _tamper_snapshot_date(run: ForecastRun, tuscany: SimpleNamespace) -> None:
    other = _snapshot(cell_key=SECOND_CELL, day=DAY - timedelta(days=1))
    EstimateUpdate.objects.filter(cell_key=SECOND_CELL).update(snapshot=other)


def _tamper_forest_build(run: ForecastRun, tuscany: SimpleNamespace) -> None:
    other = ForestBuild.objects.create(
        version="other-forest", grid_build=tuscany.grid, fingerprint="1" * 64,
        status=ForestBuild.Status.COMPLETED, planned_chunk_count=1, completed_chunk_count=1,
    )
    EstimateUpdate.objects.filter(cell_key=SECOND_CELL).update(forest_build=other)


def _tamper_terrain_build(run: ForecastRun, tuscany: SimpleNamespace) -> None:
    other = TerrainBuild.objects.create(
        version="other-terrain", grid_build=tuscany.grid, algorithm_version="national-terrain-v2",
        fingerprint="2" * 64, grid_fingerprint=tuscany.grid.fingerprint, status=TerrainBuild.Status.COMPLETED,
    )
    EstimateUpdate.objects.filter(cell_key=SECOND_CELL).update(terrain_build=other)


def _tamper_method_version(run: ForecastRun, tuscany: SimpleNamespace) -> None:
    EstimateUpdate.objects.filter(cell_key=SECOND_CELL).update(method_version="older-method")


def _tamper_tree_source(run: ForecastRun, tuscany: SimpleNamespace) -> None:
    update = EstimateUpdate.objects.get(cell_key=SECOND_CELL)
    changed = {**update.tree_inputs, "source": {**update.tree_inputs["source"], "source_id": "jrc:other"}}
    EstimateUpdate.objects.filter(id=update.id).update(tree_inputs=changed)


def _tamper_region(run: ForecastRun, tuscany: SimpleNamespace) -> None:
    EstimateUpdate.objects.filter(cell_key=SECOND_CELL).update(region_code="")


def _tamper_row_count(run: ForecastRun, tuscany: SimpleNamespace) -> None:
    EstimateUpdate.objects.filter(cell_key=FIRST_CELL).update(row_count=1)


def _tamper_result_rows(run: ForecastRun, tuscany: SimpleNamespace) -> None:
    update = EstimateUpdate.objects.get(cell_key=FIRST_CELL)
    EstimateResult.objects.filter(update=update, forest_cell=tuscany.forest_cells[0]).delete()


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (_tamper_missing_cell, "do not match the Tuscany weather cells"),
        (_tamper_unfinished_cell, "no completed update"),
        (_tamper_assessment_date, "another assessment date"),
        (_tamper_snapshot_date, "another date or snapshot"),
        (_tamper_forest_build, "another forest build"),
        (_tamper_terrain_build, "another terrain build"),
        (_tamper_method_version, "another method version"),
        (_tamper_tree_source, "another host-tree source"),
        (_tamper_region, "another region"),
        (_tamper_row_count, "does not cover the Tuscany squares"),
        (_tamper_result_rows, "missing result rows"),
    ],
)
def test_validation_rejects_a_run_that_does_not_match_its_inputs(
    tuscany: SimpleNamespace, tamper: Callable[..., None], message: str,
) -> None:
    run = _publish_day(day=DAY)
    assert validate_forecast_run(run=run) == 0
    tamper(run, tuscany)

    with pytest.raises(ForecastRunValidationError, match=message):
        validate_forecast_run(run=ForecastRun.objects.get(id=run.id))


def test_publish_refuses_an_invalid_run_and_leaves_the_pointer_alone(tuscany: SimpleNamespace) -> None:
    first = _publish_day(day=DAY)
    pointer = PublishedForecastRun.objects.get(region_code="09")
    before = (pointer.current_run_id, pointer.previous_run_id, pointer.published_at)
    next_day = DAY + timedelta(days=1)
    _weather(day=next_day)
    second = start_forecast_run(assessment_date=next_day)
    process_forecast_run(run_id=second.id)
    ForecastRun.objects.filter(id=second.id).update(status=ForecastRun.Status.RUNNING, published_at=None)
    PublishedForecastRun.objects.filter(id=pointer.id).update(
        current_run=first, previous_run=None, published_at=before[2],
    )
    EstimateUpdate.objects.filter(assessment_date=next_day).update(method_version="older-method")

    with pytest.raises(ForecastRunValidationError, match="another method version"):
        publish_forecast_run(run_id=second.id)

    pointer.refresh_from_db()
    assert (pointer.current_run_id, pointer.previous_run_id, pointer.published_at) == before
    second.refresh_from_db()
    assert second.status == ForecastRun.Status.RUNNING
    assert published_forecast_run(region_code="09").run.id == first.id


def test_publishing_queues_the_map_cache_warming_once_after_the_commit(
    tuscany: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        "estimate_updates.tasks.warm_published_map_cache_task",
        SimpleNamespace(enqueue=lambda **kwargs: calls.append(kwargs)),
    )

    run = _publish_day(day=DAY)

    assert run.status == ForecastRun.Status.PUBLISHED
    assert calls == [{"region_code": "09"}]


def test_a_failing_queue_does_not_undo_the_publication(
    tuscany: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_queue(**kwargs: str) -> None:
        raise RuntimeError("queue is down")

    monkeypatch.setattr(
        "estimate_updates.tasks.warm_published_map_cache_task",
        SimpleNamespace(enqueue=broken_queue),
    )

    run = _publish_day(day=DAY)

    assert run.status == ForecastRun.Status.PUBLISHED
    assert published_forecast_run(region_code="09").run.id == run.id


def test_forecast_task_publishes_the_run_and_queues_one_warm_up_only_once(
    tuscany: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        "estimate_updates.tasks.warm_published_map_cache_task",
        SimpleNamespace(enqueue=lambda **kwargs: calls.append(kwargs)),
    )
    _weather(day=DAY)

    first = tuscany_forecast_task.call(region_code="09", assessment_date=DAY.isoformat())
    second = tuscany_forecast_task.call(region_code="09", assessment_date=DAY.isoformat())

    assert first == second == ForecastRun.Status.PUBLISHED
    assert published_forecast_run(region_code="09").run.assessment_date == DAY
    assert calls == [{"region_code": "09"}]


def test_a_failing_forecast_task_keeps_the_published_run_and_queues_no_warm_up(
    tuscany: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _publish_day(day=DAY)
    pointer = PublishedForecastRun.objects.get(region_code="09")
    before = (pointer.current_run_id, pointer.previous_run_id, pointer.published_at)
    _weather(day=DAY + timedelta(days=1))
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        "estimate_updates.tasks.warm_published_map_cache_task",
        SimpleNamespace(enqueue=lambda **kwargs: calls.append(kwargs)),
    )

    def broken(**kwargs: object) -> None:
        raise ValueError("The calculation failed on purpose.")

    monkeypatch.setattr("estimate_updates.forecast_runs.calculate_estimate_update", broken)

    with pytest.raises(ForecastRunError, match="failed on purpose"):
        tuscany_forecast_task.call(
            region_code="09", assessment_date=(DAY + timedelta(days=1)).isoformat(),
        )

    pointer.refresh_from_db()
    assert (pointer.current_run_id, pointer.previous_run_id, pointer.published_at) == before
    assert pointer.current_run_id == first.id
    assert calls == []


def test_the_forecast_callback_queues_the_task_with_an_iso_date(tuscany: SimpleNamespace) -> None:
    task_id = weather_services._FORECAST_CALLBACK(region_code="09", assessment_date=DAY)

    queued = DBTaskResult.objects.get(id=task_id)
    assert queued.task_path == "estimate_updates.tasks.tuscany_forecast_task"
    assert queued.args_kwargs["kwargs"] == {"region_code": "09", "assessment_date": DAY.isoformat()}


def test_publish_refuses_a_run_older_than_the_published_run(tuscany: SimpleNamespace) -> None:
    older = _publish_day(day=DAY)
    newer = _publish_day(day=DAY + timedelta(days=1))

    with pytest.raises(ForecastRunError, match="newer"):
        publish_forecast_run(run_id=older.id)

    assert PublishedForecastRun.objects.get().current_run_id == newer.id


def test_recorded_source_gap_is_accepted_and_counted(tuscany: SimpleNamespace) -> None:
    snapshots = _weather(day=DAY)
    day = WeatherSnapshotDay.objects.filter(snapshot=snapshots[SECOND_CELL]).order_by("date").first()
    WeatherDaily.objects.filter(id=day.daily_id).update(status=WeatherDaily.Status.CHECKED_KNOWN_MISSING)

    run = process_forecast_run(run_id=start_forecast_run(assessment_date=DAY).id)

    assert run.status == ForecastRun.Status.PUBLISHED
    assert run.source_gap_cell_count == 1
    assert dict(run.cells.values_list("cell_key", "source_gap_days")) == {FIRST_CELL: 0, SECOND_CELL: 1}


def test_a_run_with_unfinished_cells_is_never_complete(tuscany: SimpleNamespace) -> None:
    _weather(day=DAY)
    run = start_forecast_run(assessment_date=DAY)

    with pytest.raises(ForecastRunValidationError, match="no completed update"):
        publish_forecast_run(run_id=run.id)

    assert not PublishedForecastRun.objects.exists()


# AC6: retention


def test_cleanup_keeps_current_and_previous_runs_with_their_inputs(tuscany: SimpleNamespace) -> None:
    oldest = _publish_day(day=DAY - timedelta(days=5))
    previous = _publish_day(day=DAY - timedelta(days=1))
    current = _publish_day(day=DAY)
    for offset in (1, 2):
        extra = _weather(day=DAY + timedelta(days=offset))
        for key, snapshot in extra.items():
            calculate_and_publish_estimates(
                snapshot_id=snapshot.id,
                cell_key=key,
                assessment_date=snapshot.assessment_date,
                forest_build_version=tuscany.forest.version,
                terrain_build_version=tuscany.terrain.version,
            )
    retained_updates = set(
        ForecastRunCell.objects.filter(run__in=[previous, current]).values_list("update_id", flat=True)
    )
    retained_snapshots = set(
        ForecastRunCell.objects.filter(run__in=[previous, current]).values_list("snapshot_id", flat=True)
    )
    oldest_updates = set(ForecastRunCell.objects.filter(run=oldest).values_list("update_id", flat=True))

    protected = cleanup_estimate_updates(cleanup_date=DAY + timedelta(days=2))
    weather_services.cleanup_weather(cleanup_date=DAY + timedelta(days=2))

    assert not ForecastRun.objects.filter(id=oldest.id).exists()
    assert set(ForecastRun.objects.values_list("id", flat=True)) == {previous.id, current.id}
    assert PublishedForecastRun.objects.get().previous_run_id == previous.id
    assert retained_updates <= set(EstimateUpdate.objects.values_list("id", flat=True))
    assert not oldest_updates & set(EstimateUpdate.objects.values_list("id", flat=True))
    assert {str(value) for value in retained_snapshots} <= set(protected)
    assert retained_snapshots <= set(WeatherSnapshot.objects.values_list("id", flat=True))
    kept_results = EstimateResult.objects.filter(update_id__in=retained_updates).count()
    assert kept_results == 2 * 3
    assert published_forecast_run(region_code="09").run.id == current.id
    assert validate_forecast_run(run=ForecastRun.objects.get(id=current.id)) == 0
    assert validate_forecast_run(run=ForecastRun.objects.get(id=previous.id)) == 0


def test_cleanup_keeps_a_run_that_is_still_running_and_a_failed_run_for_today(tuscany: SimpleNamespace) -> None:
    _publish_day(day=DAY - timedelta(days=3))
    _weather(day=DAY - timedelta(days=2))
    running = start_forecast_run(assessment_date=DAY - timedelta(days=2))

    cleanup_estimate_updates(cleanup_date=DAY)

    assert ForecastRun.objects.filter(id=running.id).exists()


def test_retained_run_protects_its_input_set_and_builds(tuscany: SimpleNamespace) -> None:
    _publish_day(day=DAY)

    for protected in (tuscany.input_set, tuscany.forest, tuscany.terrain):
        with pytest.raises(ProtectedError):
            protected.delete()
    assert TuscanyInputSet.objects.filter(id=tuscany.input_set.id).exists()
    assert ForestBuild.objects.filter(id=tuscany.forest.id).exists()
    assert TerrainBuild.objects.filter(id=tuscany.terrain.id).exists()


# Command


def test_dry_run_command_prints_the_plan_and_writes_nothing(tuscany: SimpleNamespace) -> None:
    _weather(day=DAY)
    out = io.StringIO()

    call_command("run_tuscany_forecast", "--assessment-date", DAY.isoformat(), "--dry-run", stdout=out)

    text = out.getvalue()
    assert "no data written" in text
    assert "Forest squares: 3" in text
    assert ForecastRun.objects.count() == 0
    assert EstimateUpdate.objects.count() == 0
    assert PublishedForecastRun.objects.count() == 0

    out = io.StringIO()
    call_command("run_tuscany_forecast", "--assessment-date", DAY.isoformat(), "--dry-run", "--json", stdout=out)
    assert json.loads(out.getvalue())["cells_total"] == 2


def test_command_runs_and_publishes_then_reports_the_run(tuscany: SimpleNamespace) -> None:
    _weather(day=DAY)
    out = io.StringIO()

    call_command("run_tuscany_forecast", "--assessment-date", DAY.isoformat(), "--json", stdout=out)

    report = json.loads(out.getvalue())
    run = ForecastRun.objects.get()
    assert report["run_id"] == str(run.id)
    assert report["status"] == "published"
    assert report["cells"] == report["completed_cells"] == 2
    assert report["squares"] == report["result_rows"] == 3
    assert report["current_run_id"] == str(run.id)
    assert report["previous_run_id"] == ""
    assert report["runtime_seconds"] > 0
    assert report["peak_memory_mb"] > 0
    assert report["estimate_storage_bytes"] > 0


def test_command_rejects_unknown_regions_and_missing_weather(tuscany: SimpleNamespace) -> None:
    _weather(day=DAY)

    with pytest.raises(CommandError, match="not in grid build"):
        call_command("run_tuscany_forecast", "--assessment-date", DAY.isoformat(), "--region-code", "99", "--dry-run")
    with pytest.raises(CommandError, match="not in grid build"):
        call_command("run_tuscany_forecast", "--assessment-date", DAY.isoformat(), "--region-code", "99")
    with pytest.raises(CommandError, match="no ready weather"):
        call_command("run_tuscany_forecast", "--assessment-date", (DAY + timedelta(days=1)).isoformat())
    assert ForecastRun.objects.count() == 0


# Several regions in one night


def _count_warm_ups(*, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        "estimate_updates.tasks.warm_published_map_cache_task",
        SimpleNamespace(enqueue=lambda **kwargs: calls.append(kwargs)),
    )
    return calls


def test_forecast_task_publishes_every_region_and_a_rerun_skips_them(
    tuscany: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _count_warm_ups(monkeypatch=monkeypatch)
    _weather(day=DAY)

    tuscany_forecast_task.call(region_code="12,09", assessment_date=DAY.isoformat())

    assert published_forecast_run(region_code="09").run.assessment_date == DAY
    assert published_forecast_run(region_code="12").run.assessment_date == DAY
    assert sorted(call["region_code"] for call in calls) == ["09", "12"]
    run_ids = set(ForecastRun.objects.values_list("id", flat=True))

    def forbidden(**kwargs: object) -> None:
        raise AssertionError("A published region must be skipped.")

    monkeypatch.setattr("estimate_updates.forecast_runs.start_forecast_run", forbidden)
    tuscany_forecast_task.call(region_code="09,12", assessment_date=DAY.isoformat())

    assert set(ForecastRun.objects.values_list("id", flat=True)) == run_ids


def test_a_failed_region_keeps_its_last_run_and_the_other_region_publishes(
    tuscany: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _count_warm_ups(monkeypatch=monkeypatch)
    _weather(day=DAY)
    tuscany_forecast_task.call(region_code="09,12", assessment_date=DAY.isoformat())
    lazio_before = PublishedForecastRun.objects.get(region_code="12").current_run_id
    next_day = DAY + timedelta(days=1)
    _weather(day=next_day)
    real = calculate_estimate_update

    def fail_for_lazio(**kwargs: object) -> object:
        if kwargs["region_code"] == "12":
            raise ValueError("The Lazio calculation failed on purpose.")
        return real(**kwargs)

    monkeypatch.setattr("estimate_updates.forecast_runs.calculate_estimate_update", fail_for_lazio)

    with pytest.raises(ForecastRunError, match="1 of 2 regions failed. 12: .*failed on purpose"):
        tuscany_forecast_task.call(region_code="09,12", assessment_date=next_day.isoformat())

    assert published_forecast_run(region_code="09").run.assessment_date == next_day
    assert PublishedForecastRun.objects.get(region_code="12").current_run_id == lazio_before
    assert ForecastRun.objects.get(region_code="12", assessment_date=next_day).status == ForecastRun.Status.FAILED

    monkeypatch.setattr("estimate_updates.forecast_runs.calculate_estimate_update", real)
    tuscany_forecast_task.call(region_code="09,12", assessment_date=next_day.isoformat())

    assert published_forecast_run(region_code="12").run.assessment_date == next_day


def test_publishing_another_region_queues_the_map_warm_up_of_that_region(
    tuscany: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _count_warm_ups(monkeypatch=monkeypatch)
    _weather(day=DAY)

    run = process_forecast_run(run_id=start_forecast_run(assessment_date=DAY, region_code="12").id)

    assert run.status == ForecastRun.Status.PUBLISHED
    assert calls == [{"region_code": "12"}]


def test_a_forecast_run_rejects_a_region_that_is_not_in_the_grid(tuscany: SimpleNamespace) -> None:
    _weather(day=DAY)

    with pytest.raises(ForecastRunError, match="not in grid build"):
        start_forecast_run(assessment_date=DAY, region_code="99")
