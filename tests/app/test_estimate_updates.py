"""Focused checks for compact estimate updates and exact read states."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
import math
from threading import Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.conf import settings
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.db import close_old_connections

from sporameteo.finds.grid import format_cell_id
from sporameteo.forecast_method import ForecastResult, calculate_forecast
from national_forest.models import ForestBuild, ForestCell, ForestChunk
from national_grid.models import GridBuild, GridBuildCell, GridCell, GridChunk, GridRegion
from national_terrain.models import TerrainBuild, TerrainCell, TerrainChunk
from weather_summaries.models import (
    WeatherDaily,
    WeatherSnapshot,
    WeatherSnapshotDay,
    WeatherSummary,
)

from estimate_updates.contracts import (
    EstimateReadStatus,
    decode_result_bytes,
    decode_result_days,
    encode_result_bytes,
)
from estimate_updates.models import EstimateResult, EstimateTask, EstimateUpdate
from estimate_updates.services import (
    EstimateValidationError,
    PreparedEstimateRow,
    advisory_lock_key,
    input_fingerprint,
    calculate_and_publish_estimates,
    calculate_estimate_update,
    cleanup_estimate_updates,
    enqueue_estimate_update,
    facts_for_estimate,
    publish_estimate_update,
    read_estimate,
    recover_estimate_tasks,
    recover_estimate_handoffs,
    retry_estimate_task,
    validate_prepared_rows,
)


@pytest.mark.django_db(transaction=True)
def test_tree_cache_reads_only_requested_squares_and_reuses_values(
    complete_estimate_fixture, monkeypatch,
) -> None:
    from estimate_updates.host_trees import tree_inputs_for_area
    from estimate_updates.models import HostTreeCache
    from sporameteo.forecast_method import HostTrees

    forest_cell = complete_estimate_fixture["forest_cell"]
    source = {"source_year": 2006, "resolution_m": 1000}
    monkeypatch.setattr("estimate_updates.host_trees.read_manifest", lambda **kwargs: (source, "a" * 64))
    calls = []

    def sample(**kwargs):
        if kwargs["centres"]:
            calls.append(kwargs["centres"])
        return {key: HostTrees(values=(0.8, 0.0, None, 0.0, 0.0), source_id="jrc:test", source_year=2006, resolution_m=1000)
                for key in kwargs["centres"]}

    monkeypatch.setattr("estimate_updates.host_trees.sample_squares", sample)
    first = tree_inputs_for_area(forest_cells=[forest_cell])
    second = tree_inputs_for_area(forest_cells=[forest_cell])
    assert first == second
    assert first["cells"][str(forest_cell.id)] == [0.8, 0.0, None, 0.0, 0.0]
    assert len(calls) == 1
    assert len(calls[0]) == 1
    assert HostTreeCache.objects.count() == 1
    monkeypatch.setattr("estimate_updates.host_trees.read_manifest", lambda **kwargs: (source, "b" * 64))
    third = tree_inputs_for_area(forest_cells=[forest_cell])
    assert third["source"]["source_id"] != first["source"]["source_id"]
    assert HostTreeCache.objects.count() == 2
    assert len(calls) == 2


@pytest.mark.django_db(transaction=True)
def test_tree_source_failure_uses_disclosed_fallback_without_caching_zero(
    complete_estimate_fixture, monkeypatch,
) -> None:
    from estimate_updates.host_trees import tree_inputs_for_area
    from estimate_updates.models import HostTreeCache

    monkeypatch.setattr("estimate_updates.host_trees.read_manifest", lambda **kwargs: ({}, "a" * 64))

    def fail(**kwargs):
        raise OSError("local file unavailable")

    monkeypatch.setattr("estimate_updates.host_trees.sample_squares", fail)
    result = tree_inputs_for_area(forest_cells=[complete_estimate_fixture["forest_cell"]])
    assert result == {}
    assert HostTreeCache.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_new_tree_source_queues_a_new_task_for_the_same_weather(complete_estimate_fixture, monkeypatch, settings) -> None:
    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    settings.SPORAMETEO_TERRAIN_BUILD_VERSION = complete_estimate_fixture["terrain"].version
    monkeypatch.setattr("estimate_updates.services._schedule_estimate_task", lambda **kwargs: False)
    monkeypatch.setattr("estimate_updates.services.calculation_version", lambda: "a" * 64)
    kwargs = dict(snapshot_id=snapshot.id, cell_key=snapshot.cell_key,
                  assessment_date=snapshot.assessment_date, forest_build_version=forest.version)
    first = enqueue_estimate_update(**kwargs)
    assert enqueue_estimate_update(**kwargs).id == first.id
    monkeypatch.setattr("estimate_updates.services.calculation_version", lambda: "b" * 64)
    second = enqueue_estimate_update(**kwargs)
    assert first.id != second.id
    assert first.calculation_version != second.calculation_version


def test_result_bytes_store_eight_percentages_and_unavailable_reasons() -> None:
    values = encode_result_bytes(
        percentages=[0, 5, 95, None, 42, None, 100, 1],
        unavailable_reasons=[
            None,
            None,
            None,
            "older_rain",
            None,
            "square_altitude",
            None,
            None,
        ],
    )

    assert len(values) == 8
    assert decode_result_bytes(values=values) == (
        0,
        5,
        95,
        "older_rain",
        42,
        "square_altitude",
        100,
        1,
    )
    assert decode_result_days(values=values, assessment_date=date(2026, 10, 11))[3].target_date == date(
        2026, 10, 14
    )


def test_result_bytes_reject_wrong_length_unknown_reason_and_invalid_percentage() -> None:
    with pytest.raises(ValueError, match="exactly eight"):
        encode_result_bytes(percentages=[1], unavailable_reasons=[None])
    with pytest.raises(ValueError, match="Unknown estimate unavailable reason"):
        encode_result_bytes(percentages=[None] * 8, unavailable_reasons=["unknown"] * 8)
    with pytest.raises(ValueError, match="whole integers"):
        encode_result_bytes(percentages=[1.5] + [None] * 7, unavailable_reasons=[None] * 8)
    with pytest.raises(ValueError, match="Unknown estimate result byte"):
        decode_result_bytes(values=bytes([127] * 8))


def test_validation_rejects_missing_duplicate_and_malformed_rows() -> None:
    valid = PreparedEstimateRow(forest_cell_id=1, values=bytes([50] * 8))
    with pytest.raises(EstimateValidationError, match="Missing"):
        validate_prepared_rows(rows=[valid], expected_forest_cell_ids=[1, 2])
    with pytest.raises(EstimateValidationError, match="duplicate"):
        validate_prepared_rows(rows=[valid, valid], expected_forest_cell_ids=[1])
    with pytest.raises(EstimateValidationError, match="Unknown estimate result byte"):
        validate_prepared_rows(
            rows=[PreparedEstimateRow(forest_cell_id=1, values=bytes([127] * 8))],
            expected_forest_cell_ids=[1],
        )


def test_update_input_fingerprint_is_repeatable_and_changes_with_each_identity() -> None:
    snapshot_id = uuid4()
    forest_build_id = uuid4()
    terrain_build_id = uuid4()
    first = input_fingerprint(
        cell_key="44.00,11.00",
        assessment_date=date(2026, 10, 11),
        snapshot_id=snapshot_id,
        forest_build_id=forest_build_id,
        terrain_build_id=terrain_build_id,
        method_version="porcini-experimental-v1",
    )

    assert first == input_fingerprint(
        cell_key="44.00,11.00",
        assessment_date=date(2026, 10, 11),
        snapshot_id=snapshot_id,
        forest_build_id=forest_build_id,
        terrain_build_id=terrain_build_id,
        method_version="porcini-experimental-v1",
    )
    assert first != input_fingerprint(
        cell_key="44.00,11.00",
        assessment_date=date(2026, 10, 12),
        snapshot_id=snapshot_id,
        forest_build_id=forest_build_id,
        terrain_build_id=terrain_build_id,
        method_version="porcini-experimental-v1",
    )


@pytest.mark.django_db(transaction=True)
def test_result_storage_uses_update_and_forest_cell_composite_primary_key() -> None:
    assert EstimateResult._meta.pk.field_names == ("update_id", "forest_cell_id")
    assert EstimateResult._meta.get_field("values").max_length == 8
    assert "id" not in {field.name for field in EstimateResult._meta.fields}


def test_advisory_lock_key_is_stable_and_cell_specific() -> None:
    assert advisory_lock_key(cell_key="44.00,11.00") == advisory_lock_key(cell_key="44.00,11.00")
    assert advisory_lock_key(cell_key="44.00,11.00") != advisory_lock_key(cell_key="45.00,11.00")


def test_exact_missing_update_returns_expired_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyQuery:
        def first(self):
            return None

    monkeypatch.setattr(
        "estimate_updates.services.EstimateUpdate.objects",
        SimpleNamespace(filter=lambda **kwargs: EmptyQuery()),
    )

    result = read_estimate(
        update_id=uuid4(),
        forest_cell_id=42,
        target_date=date(2026, 10, 11),
    )

    assert result.status is EstimateReadStatus.EXPIRED
    assert result.update_id is None


@pytest.mark.django_db(transaction=True)
def test_read_estimate_marks_missing_forest_data_unavailable(
    complete_estimate_fixture: dict[str, object],
) -> None:
    assessment_snapshot = complete_estimate_fixture["snapshot"]
    source_forest = complete_estimate_fixture["forest"]
    source_cell = complete_estimate_fixture["forest_cell"]
    source_terrain = complete_estimate_fixture["terrain"]
    assert isinstance(assessment_snapshot, WeatherSnapshot)
    assert isinstance(source_forest, ForestBuild)
    assert isinstance(source_cell, ForestCell)
    assert isinstance(source_terrain, TerrainBuild)
    suffix = uuid4().hex[:10]
    forest = ForestBuild.objects.create(
        version=f"test-forest-missing-{suffix}",
        grid_build=source_forest.grid_build,
        fingerprint="d" * 64,
        status=ForestBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=1,
        forest_cell_count=0,
    )
    chunk = ForestChunk.objects.create(
        build=forest,
        grid_chunk=source_cell.chunk.grid_chunk,
        west_e=source_cell.chunk.west_e,
        south_n=source_cell.chunk.south_n,
        east_e=source_cell.chunk.east_e,
        north_n=source_cell.chunk.north_n,
        status=ForestChunk.Status.COMPLETED,
        cell_count=1,
    )
    missing_cell = ForestCell.objects.create(
        build=forest,
        grid_build_cell=source_cell.grid_build_cell,
        chunk=chunk,
        total_pixel_count=100,
        valid_pixel_count=0,
        forest_pixel_count=0,
        broadleaf_pixel_count=0,
        valid_coverage=0.0,
        coverage_quality="unavailable",
        forest_share=None,
        broadleaf_share=None,
        leaf_type=None,
        is_forest=None,
    )
    update = EstimateUpdate.objects.create(
        cell_key=assessment_snapshot.cell_key,
        assessment_date=assessment_snapshot.assessment_date,
        calculated_at=datetime(2026, 10, 11, 11, tzinfo=UTC),
        snapshot=assessment_snapshot,
        forest_build=forest,
        terrain_build=source_terrain,
        method_version="porcini-experimental-v1",
        row_count=0,
        input_fingerprint=input_fingerprint(
            cell_key=assessment_snapshot.cell_key,
            assessment_date=assessment_snapshot.assessment_date,
            snapshot_id=assessment_snapshot.id,
            forest_build_id=forest.id,
            terrain_build_id=source_terrain.id,
            method_version="porcini-experimental-v1",
        ),
    )

    result = read_estimate(
        update_id=update.public_id,
        forest_cell_id=missing_cell.id,
        target_date=assessment_snapshot.assessment_date,
    )

    assert result.status is EstimateReadStatus.UNAVAILABLE
    assert result.unavailable_reason == "missing_forest_data"
    assert result.message_key == "estimate.unavailable"


@pytest.fixture
def complete_estimate_fixture() -> dict[str, object]:
    """Create one eligible and one nonforest square with frozen inputs."""
    suffix = uuid4().hex[:10]
    west, south = 4_300_000, 2_300_000
    polygon = Polygon.from_bbox((west, south, west + 1_000, south + 500))
    multipolygon = MultiPolygon(polygon, srid=3035)
    grid = GridBuild.objects.create(
        version=f"test-grid-{suffix}",
        fingerprint="a" * 64,
        status=GridBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=2,
    )
    region = GridRegion.objects.create(
        build=grid,
        istat_code=f"R{suffix}",
        name=f"Test region {suffix}",
        geometry=multipolygon,
    )
    grid_chunk = GridChunk.objects.create(
        build=grid,
        west_e=west,
        south_n=south,
        east_e=west + 1_000,
        north_n=south + 500,
        status=GridChunk.Status.COMPLETED,
        cell_count=2,
    )
    memberships: list[GridBuildCell] = []
    for index in range(2):
        corner_e = west + index * 500
        grid_cell = GridCell.objects.create(
            cell_id=format_cell_id(corner_e=corner_e, corner_n=south),
            corner_e=corner_e,
            corner_n=south,
            geometry=Polygon.from_bbox((corner_e, south, corner_e + 500, south + 500)),
        )
        memberships.append(
            GridBuildCell.objects.create(
                build=grid,
                cell=grid_cell,
                chunk=grid_chunk,
                owner_region=region,
                land_geometry=multipolygon,
                land_share=1.0,
            )
        )
    forest = ForestBuild.objects.create(
        version=f"test-forest-{suffix}",
        grid_build=grid,
        fingerprint="b" * 64,
        status=ForestBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=2,
        forest_cell_count=1,
    )
    forest_chunk = ForestChunk.objects.create(
        build=forest,
        grid_chunk=grid_chunk,
        west_e=west,
        south_n=south,
        east_e=west + 1_000,
        north_n=south + 500,
        status=ForestChunk.Status.COMPLETED,
        cell_count=2,
    )
    forest_cell = ForestCell.objects.create(
        build=forest,
        grid_build_cell=memberships[0],
        chunk=forest_chunk,
        total_pixel_count=100,
        valid_pixel_count=100,
        forest_pixel_count=100,
        broadleaf_pixel_count=100,
        valid_coverage=1.0,
        coverage_quality="high",
        forest_share=1.0,
        broadleaf_share=1.0,
        leaf_type=ForestCell.LeafType.BROADLEAF,
        is_forest=True,
        weather_latitude=44.0,
        weather_longitude=11.0,
    )
    noforest_cell = ForestCell.objects.create(
        build=forest,
        grid_build_cell=memberships[1],
        chunk=forest_chunk,
        total_pixel_count=100,
        valid_pixel_count=100,
        forest_pixel_count=0,
        broadleaf_pixel_count=0,
        valid_coverage=1.0,
        coverage_quality="high",
        forest_share=0.0,
        leaf_type=ForestCell.LeafType.NO_FOREST,
        is_forest=False,
    )
    terrain = TerrainBuild.objects.create(
        version=f"test-terrain-{suffix}",
        grid_build=grid,
        algorithm_version="national-terrain-v2",
        fingerprint="c" * 64,
        grid_fingerprint=grid.fingerprint,
        status=TerrainBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        result_count=2,
    )
    terrain_chunk = TerrainChunk.objects.create(
        build=terrain,
        grid_chunk=grid_chunk,
        status=TerrainChunk.Status.COMPLETED,
        result_count=2,
    )
    for membership in memberships:
        TerrainCell.objects.create(
            build=terrain,
            grid_build_cell=membership,
            chunk=terrain_chunk,
            altitude_m=500.0,
            slope_deg=0.0,
            aspect_deg=None,
            altitude_valid_fraction=1.0,
            slope_valid_fraction=1.0,
            aspect_valid_fraction=1.0,
            altitude_quality="high",
            slope_quality="high",
            aspect_quality="high",
        )
    assessment = date(2026, 10, 11)
    snapshot = WeatherSnapshot.objects.create(
        cell_key="44.00,11.00",
        pipeline_version=2,
        assessment_date=assessment,
        reference_elevation_m=500.0,
        latest_retrieval=datetime(2026, 10, 11, 10, tzinfo=UTC),
        source_ids=["benchmark:weather"],
        source_attempt_ids=["benchmark:attempt"],
        metadata={"benchmark": "test"},
    )
    summaries = []
    for days_ahead in range(8):
        target = assessment.fromordinal(assessment.toordinal() + days_ahead)
        summaries.append(
            WeatherSummary(
                snapshot=snapshot,
                target_date=target,
                days_ahead=days_ahead,
                old_rain_mm=60.0,
                recent_rain_mm=30.0,
                target_mean_temperature_c=16.25,
                metadata={
                    "old_rain": {
                        "period_start": (target.fromordinal(target.toordinal() - 21)).isoformat(),
                        "period_end": (target.fromordinal(target.toordinal() - 8)).isoformat(),
                        "completed_day_count": 14,
                        "forecast_day_count": 0,
                        "source_ids": ["benchmark:old"],
                    },
                    "recent_rain": {
                        "period_start": (target.fromordinal(target.toordinal() - 7)).isoformat(),
                        "period_end": (target.fromordinal(target.toordinal() - 1)).isoformat(),
                        "completed_day_count": 7 - days_ahead,
                        "forecast_day_count": days_ahead,
                        "source_ids": ["benchmark:recent"],
                    },
                    "target": {
                        "period_start": target.isoformat(),
                        "period_end": target.isoformat(),
                        "completed_day_count": 0,
                        "forecast_day_count": 1,
                        "source_ids": ["benchmark:target"],
                    },
                },
            )
        )
    WeatherSummary.objects.bulk_create(summaries)
    _add_daily_temperatures(snapshot=snapshot)
    return {
        "snapshot": snapshot,
        "forest": forest,
        "terrain": terrain,
        "forest_cell": forest_cell,
        "noforest_cell": noforest_cell,
    }


def _add_daily_temperatures(
    *,
    snapshot: WeatherSnapshot,
    missing_dates: tuple[date, ...] = (),
    frost_dates: tuple[date, ...] = (),
) -> None:
    """Link daily temperature rows to a snapshot, from 21 days before through 7 days after."""
    attempt_id = uuid4()
    rows = []
    for offset in range(-30, 8):
        current = snapshot.assessment_date + timedelta(days=offset)
        if current in missing_dates:
            continue
        rows.append(
            WeatherDaily(
                cell_key=snapshot.cell_key,
                date=current,
                source_provider="Open-Meteo",
                source_model="test",
                source_endpoint="https://example.test/weather",
                source_id="benchmark:weather",
                request_id="request-1",
                attempt_id=attempt_id,
                retrieved_at=datetime(2026, 10, 11, 10, tzinfo=UTC),
                requested_latitude=44.0,
                requested_longitude=11.0,
                returned_latitude=44.0,
                returned_longitude=11.0,
                reference_elevation_m=500.0,
                rain_mm=5.0,
                et0_mm=2.0,
                mean_temperature_c=2.0 if current in frost_dates else 16.25,
                min_temperature_c=-1.0 if current in frost_dates else 8.0,
                status=WeatherDaily.Status.COMPLETE,
            )
        )
    WeatherDaily.objects.bulk_create(rows)
    WeatherSnapshotDay.objects.bulk_create(
        [WeatherSnapshotDay(snapshot=snapshot, date=row.date, daily=row) for row in rows]
    )


def _create_followup_snapshot(
    *,
    source_snapshot: WeatherSnapshot,
    assessment_date: date,
    frost_dates: tuple[date, ...] = (),
) -> WeatherSnapshot:
    """Create a second frozen weather assessment with the same input shape."""
    snapshot = WeatherSnapshot.objects.create(
        cell_key=source_snapshot.cell_key,
        pipeline_version=source_snapshot.pipeline_version,
        assessment_date=assessment_date,
        reference_elevation_m=source_snapshot.reference_elevation_m,
        latest_retrieval=source_snapshot.latest_retrieval,
        source_ids=[f"benchmark:weather:{assessment_date.isoformat()}"],
        source_attempt_ids=[f"benchmark:attempt:{assessment_date.isoformat()}"],
        metadata={"benchmark": "test", "assessment_date": assessment_date.isoformat()},
    )
    summaries = []
    for days_ahead in range(8):
        target = assessment_date.fromordinal(assessment_date.toordinal() + days_ahead)
        summaries.append(
            WeatherSummary(
                snapshot=snapshot,
                target_date=target,
                days_ahead=days_ahead,
                old_rain_mm=60.0,
                recent_rain_mm=30.0,
                target_mean_temperature_c=16.25,
                metadata={
                    "old_rain": {
                        "period_start": target.fromordinal(target.toordinal() - 21).isoformat(),
                        "period_end": target.fromordinal(target.toordinal() - 8).isoformat(),
                        "completed_day_count": 14,
                        "forecast_day_count": 0,
                        "source_ids": ["benchmark:old"],
                    },
                    "recent_rain": {
                        "period_start": target.fromordinal(target.toordinal() - 7).isoformat(),
                        "period_end": target.fromordinal(target.toordinal() - 1).isoformat(),
                        "completed_day_count": 7 - days_ahead,
                        "forecast_day_count": days_ahead,
                        "source_ids": ["benchmark:recent"],
                    },
                    "target": {
                        "period_start": target.isoformat(),
                        "period_end": target.isoformat(),
                        "completed_day_count": 0,
                        "forecast_day_count": 1,
                        "source_ids": ["benchmark:target"],
                    },
                },
            )
        )
    WeatherSummary.objects.bulk_create(summaries)
    _add_daily_temperatures(snapshot=snapshot, frost_dates=frost_dates)
    return snapshot


@pytest.mark.django_db(transaction=True)
def test_default_method_stores_v5_with_thirty_preceding_weather_days(
    complete_estimate_fixture: dict[str, object],
) -> None:
    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    terrain = complete_estimate_fixture["terrain"]
    calculated_inputs = []

    def capturing(*, inputs):
        calculated_inputs.append(inputs)
        return calculate_forecast(inputs=inputs)

    prepared = calculate_estimate_update(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
        calculator=capturing,
    )
    default_prepared = calculate_estimate_update(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )

    assert prepared.method_version == "porcini-experimental-v5"
    assert default_prepared.method_version == "porcini-experimental-v5"
    assert len(calculated_inputs) == 8
    for days_ahead, inputs in enumerate(calculated_inputs):
        assert len(inputs.daily_weather) == 30
        assert inputs.daily_weather[0].day == inputs.target_date - timedelta(days=30)
        assert inputs.daily_weather[-1].day == inputs.target_date - timedelta(days=1)
        assert all(day.et0_mm is not None for day in inputs.daily_weather)
        assert sum(day.day >= inputs.assessment_date for day in inputs.daily_weather) == days_ahead
        record = inputs.period_temperatures
        assert len(record.daily_means) == 20
        assert len(record.daily_minimums) == 20
        assert record.period_end == inputs.target_date
        assert record.period_start == inputs.target_date - timedelta(days=19)
        assert record.completed_day_count == 19 - days_ahead
        assert record.forecast_day_count == days_ahead + 1
        assert record.unit == "deg_c"
        assert None not in record.daily_means
    values = decode_result_bytes(values=default_prepared.rows[0].values)
    assert all(isinstance(value, int) for value in values)


@pytest.mark.django_db(transaction=True)
def test_fast_cell_path_stores_the_same_rows_as_the_square_by_square_path(
    complete_estimate_fixture: dict[str, object],
) -> None:
    snapshot = complete_estimate_fixture["snapshot"]
    arguments = {
        "snapshot_id": snapshot.id,
        "cell_key": snapshot.cell_key,
        "assessment_date": snapshot.assessment_date,
        "forest_build_version": complete_estimate_fixture["forest"].version,
        "terrain_build_version": complete_estimate_fixture["terrain"].version,
    }
    slow = calculate_estimate_update(**arguments, calculator=lambda inputs: calculate_forecast(inputs=inputs))
    fast = calculate_estimate_update(**arguments)
    assert [(row.forest_cell_id, bytes(row.values)) for row in fast.rows] == [
        (row.forest_cell_id, bytes(row.values)) for row in slow.rows
    ]
    assert fast.input_fingerprint == slow.input_fingerprint
    assert fast.tree_inputs == slow.tree_inputs


@pytest.mark.django_db(transaction=True)
def test_fast_cell_path_rejects_invalid_host_tree_values(
    complete_estimate_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sporameteo.forecast_method import HostTrees

    monkeypatch.setattr(
        "estimate_updates.services.saved_host_trees",
        lambda **kwargs: HostTrees(values=(2.0, 0.0, 0.0, 0.0, 0.0), source_id="test", source_year=2020, resolution_m=1000),
    )
    snapshot = complete_estimate_fixture["snapshot"]
    with pytest.raises(EstimateValidationError):
        calculate_estimate_update(
            snapshot_id=snapshot.id, cell_key=snapshot.cell_key,
            assessment_date=snapshot.assessment_date,
            forest_build_version=complete_estimate_fixture["forest"].version,
            terrain_build_version=complete_estimate_fixture["terrain"].version,
        )


@pytest.mark.django_db(transaction=True)
def test_current_method_rejects_legacy_weather_without_changing_saved_rows(
    complete_estimate_fixture: dict[str, object],
) -> None:
    snapshot = complete_estimate_fixture["snapshot"]
    WeatherSnapshot.objects.filter(id=snapshot.id).update(pipeline_version=1)
    with pytest.raises(EstimateValidationError, match="30-day weather snapshot"):
        calculate_estimate_update(
            snapshot_id=snapshot.id, cell_key=snapshot.cell_key,
            assessment_date=snapshot.assessment_date,
            forest_build_version=complete_estimate_fixture["forest"].version,
            terrain_build_version=complete_estimate_fixture["terrain"].version,
        )
    assert EstimateUpdate.objects.count() == 0
    assert snapshot.days.count() == 38


@pytest.mark.django_db(transaction=True)
def test_frost_day_in_the_period_lowers_the_percentage(
    complete_estimate_fixture: dict[str, object],
) -> None:
    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    terrain = complete_estimate_fixture["terrain"]
    assessment = date(2026, 10, 12)
    frost_snapshot = _create_followup_snapshot(
        source_snapshot=snapshot,
        assessment_date=assessment,
        frost_dates=(assessment - timedelta(days=3), assessment - timedelta(days=4)),
    )

    def percentage_on_first_day(*, current: WeatherSnapshot) -> int:
        prepared = calculate_estimate_update(
            snapshot_id=current.id,
            cell_key=current.cell_key,
            assessment_date=current.assessment_date,
            forest_build_version=forest.version,
            terrain_build_version=terrain.version,
        )
        value = decode_result_bytes(values=prepared.rows[0].values)[0]
        assert isinstance(value, int)
        return value

    assert percentage_on_first_day(current=frost_snapshot) < percentage_on_first_day(current=snapshot)


@pytest.mark.django_db(transaction=True)
def test_missing_daily_temperature_gives_period_temperature_reason(
    complete_estimate_fixture: dict[str, object],
) -> None:
    source = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    terrain = complete_estimate_fixture["terrain"]
    snapshot = _create_followup_snapshot(source_snapshot=source, assessment_date=date(2026, 10, 12))
    missing = snapshot.assessment_date - timedelta(days=5)
    WeatherSnapshotDay.objects.filter(snapshot=snapshot, date=missing).delete()

    prepared = calculate_estimate_update(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )

    decoded = decode_result_bytes(values=prepared.rows[0].values)
    assert decoded == ("weather_history",) * 8
    assert encode_result_bytes(
        percentages=[None] * 8,
        unavailable_reasons=list(decoded),
    ) == prepared.rows[0].values


@pytest.mark.django_db(transaction=True)
def test_calculation_publishes_one_eight_day_row_and_omits_nonforest(
    complete_estimate_fixture: dict[str, object],
) -> None:
    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    terrain = complete_estimate_fixture["terrain"]
    prepared = calculate_estimate_update(
        snapshot_id=snapshot.id,
        cell_key="44.00,11.00",
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )
    assert prepared.row_count == 1
    assert len(prepared.rows) == 1
    assert len(prepared.rows[0].values) == 8
    published = publish_estimate_update(prepared=prepared)
    repeated = calculate_and_publish_estimates(
        snapshot_id=snapshot.id,
        cell_key="44.00,11.00",
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )

    assert published.created is True
    assert repeated.created is False
    assert repeated.update.public_id == published.update.public_id
    assert published.update.results.count() == 1
    assert not published.update.results.filter(
        forest_cell_id=complete_estimate_fixture["noforest_cell"].id
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_missing_terrain_input_keeps_exact_terrain_build_source(
    complete_estimate_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from estimate_updates import services

    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    terrain = complete_estimate_fixture["terrain"]
    forest_cell = complete_estimate_fixture["forest_cell"]
    TerrainCell.objects.filter(
        build=terrain,
        grid_build_cell_id=forest_cell.grid_build_cell_id,
    ).delete()
    calculated_inputs = []

    def calculating(*, inputs):
        calculated_inputs.append(inputs)
        return calculate_forecast(inputs=inputs)

    prepared = calculate_estimate_update(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
        calculator=calculating,
    )
    assert calculated_inputs
    assert calculated_inputs[0].square_altitude.source_id == f"terrain-build:{terrain.id}"

    published = publish_estimate_update(prepared=prepared)
    facts_inputs = []
    original_calculate_forecast = services.calculate_forecast

    def facts_calculating(*, inputs, method):
        facts_inputs.append(inputs)
        return original_calculate_forecast(inputs=inputs, method=method)

    monkeypatch.setattr(services, "calculate_forecast", facts_calculating)
    facts_for_estimate(
        update_id=published.update.public_id,
        forest_cell_id=forest_cell.id,
        target_date=snapshot.assessment_date,
    )

    assert facts_inputs
    assert facts_inputs[0].square_altitude.source_id == f"terrain-build:{terrain.id}"


@pytest.mark.django_db(transaction=True)
def test_concurrent_publication_returns_one_public_update(
    complete_estimate_fixture: dict[str, object],
) -> None:
    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    terrain = complete_estimate_fixture["terrain"]
    prepared = calculate_estimate_update(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )

    def publish_once() -> tuple[bool, str]:
        close_old_connections()
        try:
            result = publish_estimate_update(prepared=prepared)
            return result.created, str(result.update.public_id)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: publish_once(), range(2)))

    assert sorted(created for created, _ in results) == [False, True]
    assert len({public_id for _, public_id in results}) == 1
    assert EstimateUpdate.objects.filter(cell_key=snapshot.cell_key).count() == 1


@pytest.mark.django_db(transaction=True)
def test_older_completion_does_not_replace_newer_publication(
    complete_estimate_fixture: dict[str, object],
) -> None:
    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    terrain = complete_estimate_fixture["terrain"]
    newer_snapshot = _create_followup_snapshot(
        source_snapshot=snapshot,
        assessment_date=snapshot.assessment_date.fromordinal(snapshot.assessment_date.toordinal() + 1),
    )
    older = calculate_estimate_update(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )
    newer = calculate_estimate_update(
        snapshot_id=newer_snapshot.id,
        cell_key=newer_snapshot.cell_key,
        assessment_date=newer_snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )

    published_newer = publish_estimate_update(prepared=newer)
    skipped_older = publish_estimate_update(prepared=older)

    assert published_newer.created is True
    assert skipped_older.created is False
    assert skipped_older.skipped_older is True
    assert skipped_older.update.public_id == published_newer.update.public_id
    assert EstimateUpdate.objects.filter(cell_key=snapshot.cell_key).count() == 1


@pytest.mark.django_db(transaction=True)
def test_failed_publication_rolls_back_and_preserves_previous_update(
    complete_estimate_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    terrain = complete_estimate_fixture["terrain"]
    published = calculate_and_publish_estimates(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )
    newer_snapshot = _create_followup_snapshot(
        source_snapshot=snapshot,
        assessment_date=snapshot.assessment_date.fromordinal(snapshot.assessment_date.toordinal() + 1),
    )
    prepared = calculate_estimate_update(
        snapshot_id=newer_snapshot.id,
        cell_key=newer_snapshot.cell_key,
        assessment_date=newer_snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )

    def fail_bulk_create(*args, **kwargs):
        raise RuntimeError("simulated result insert failure")

    monkeypatch.setattr(EstimateResult.objects, "bulk_create", fail_bulk_create)
    with pytest.raises(RuntimeError, match="simulated result insert failure"):
        publish_estimate_update(prepared=prepared)

    assert EstimateUpdate.objects.count() == 1
    assert EstimateUpdate.objects.get().public_id == published.update.public_id
    assert published.update.results.count() == 1


@pytest.mark.django_db(transaction=True)
def test_cleanup_keeps_two_snapshots_and_expires_exact_old_uuid(
    complete_estimate_fixture: dict[str, object],
) -> None:
    from estimate_updates.services import cleanup_estimate_updates

    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    assessment_dates = [
        snapshot.assessment_date.fromordinal(snapshot.assessment_date.toordinal() + offset)
        for offset in range(3)
    ]
    snapshots = [snapshot]
    snapshots.extend(
        _create_followup_snapshot(source_snapshot=snapshot, assessment_date=assessment_date)
        for assessment_date in assessment_dates[1:]
    )
    publications = [
        calculate_and_publish_estimates(
            snapshot_id=current_snapshot.id,
            cell_key=current_snapshot.cell_key,
            assessment_date=current_snapshot.assessment_date,
            forest_build_version=forest.version,
            terrain_build_version=complete_estimate_fixture["terrain"].version,
        )
        for current_snapshot in snapshots
    ]

    retained = cleanup_estimate_updates(cleanup_date=assessment_dates[-1])

    assert set(retained) == {str(snapshots[1].id), str(snapshots[2].id)}
    assert not EstimateUpdate.objects.filter(id=publications[0].update.id).exists()
    assert EstimateResult.objects.filter(update_id=publications[0].update.id).count() == 0
    assert set(EstimateUpdate.objects.values_list("snapshot_id", flat=True)) == {
        snapshots[1].id,
        snapshots[2].id,
    }
    expired = read_estimate(
        update_id=publications[0].update.public_id,
        forest_cell_id=complete_estimate_fixture["forest_cell"].id,
        target_date=assessment_dates[0],
    )
    assert expired.status is EstimateReadStatus.EXPIRED
    assert expired.update_id is None


@pytest.mark.django_db(transaction=True)
def test_weather_ready_callback_hands_off_one_durable_task(
    complete_estimate_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from estimate_updates import services
    from weather_on_demand.services import notify_snapshot_ready

    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    queued: list[object] = []
    queue_lock = Lock()

    def fake_queue(*, task_id):
        with queue_lock:
            queued.append(task_id)
        EstimateTask.objects.filter(id=task_id).update(task_id=uuid4())

    monkeypatch.setattr(services, "_queue_estimate_task", fake_queue)
    notify_snapshot_ready(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
    )

    task = EstimateTask.objects.get()
    assert task.snapshot_id == snapshot.id
    assert task.cell_key == snapshot.cell_key
    assert task.assessment_date == snapshot.assessment_date
    assert task.forest_build_version == forest.version
    assert task.task_id is not None
    assert queued == [task.id]


@pytest.mark.django_db(transaction=True)
def test_concurrent_weather_callbacks_deduplicate_task(
    complete_estimate_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from estimate_updates import services

    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    queued: list[object] = []
    queue_lock = Lock()

    def fake_queue(*, task_id):
        with queue_lock:
            queued.append(task_id)
        EstimateTask.objects.filter(id=task_id).update(task_id=uuid4())

    monkeypatch.setattr(services, "_queue_estimate_task", fake_queue)

    def enqueue_once() -> object:
        close_old_connections()
        try:
            return services.enqueue_estimate_update(
                snapshot_id=snapshot.id,
                cell_key=snapshot.cell_key,
                assessment_date=snapshot.assessment_date,
                forest_build_version=forest.version,
            ).id
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        task_ids = list(executor.map(lambda _: enqueue_once(), range(2)))

    assert task_ids[0] == task_ids[1]
    assert EstimateTask.objects.count() == 1
    assert len(queued) == 1


@pytest.mark.django_db(transaction=True)
def test_failed_queue_dispatch_leaves_waiting_task_for_later_handoff(
    complete_estimate_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from estimate_updates import services, tasks

    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    queued_id = uuid4()

    class FailingTask:
        def enqueue(self, *, job_id: str):
            raise RuntimeError("queue database is unavailable")

    class WorkingTask:
        def enqueue(self, *, job_id: str):
            return SimpleNamespace(id=queued_id)

    monkeypatch.setattr(tasks, "estimate_update_task", FailingTask())
    first = enqueue_estimate_update(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
    )

    assert first.state == EstimateTask.State.WAITING
    assert first.task_id is None
    assert "queue database is unavailable" in first.failure_reason

    monkeypatch.setattr(tasks, "estimate_update_task", WorkingTask())
    second = enqueue_estimate_update(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
    )

    assert second.id == first.id
    assert second.state == EstimateTask.State.WAITING
    assert second.task_id == queued_id
    assert second.failure_reason.endswith("queue database is unavailable")
    assert services._task_backend_status(task_id=queued_id) is None


@pytest.mark.django_db(transaction=True)
def test_retry_preserves_failure_reason_and_queues_again(
    complete_estimate_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from estimate_updates import services

    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    queued: list[object] = []

    def fake_queue(*, task_id):
        queued.append(task_id)
        EstimateTask.objects.filter(id=task_id).update(task_id=uuid4())

    monkeypatch.setattr(services, "_queue_estimate_task", fake_queue)
    task = EstimateTask.objects.create(
        snapshot=snapshot,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version="test-terrain-version",
        input_fingerprint="d" * 64,
        state=EstimateTask.State.FAILED,
        failure_reason="calculation failed: missing terrain",
    )

    retried = retry_estimate_task(task_id=task.id)

    assert retried.state == EstimateTask.State.WAITING
    assert retried.failure_reason == "calculation failed: missing terrain"
    assert retried.task_id is not None
    assert queued == [task.id]


@pytest.mark.django_db(transaction=True)
def test_startup_recovery_requeues_interrupted_and_terminal_tasks(
    complete_estimate_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from estimate_updates import services

    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    queued: list[object] = []
    statuses = {uuid4(): "FAILED"}
    terminal_task_id = next(iter(statuses))
    terminal_snapshot = _create_followup_snapshot(
        source_snapshot=snapshot,
        assessment_date=snapshot.assessment_date.fromordinal(snapshot.assessment_date.toordinal() + 1),
    )

    interrupted = EstimateTask.objects.create(
        snapshot=snapshot,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version="test-terrain-version",
        input_fingerprint="e" * 64,
        state=EstimateTask.State.RUNNING,
        queue_token=uuid4(),
        failure_reason="worker stopped",
    )
    terminal = EstimateTask.objects.create(
        snapshot=terminal_snapshot,
        cell_key=terminal_snapshot.cell_key,
        assessment_date=terminal_snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version="test-terrain-version",
        input_fingerprint="f" * 64,
        task_id=terminal_task_id,
        state=EstimateTask.State.WAITING,
    )

    monkeypatch.setattr(services, "_task_backend_status", lambda *, task_id: statuses.get(task_id))

    def fake_queue(*, task_id):
        queued.append(task_id)
        EstimateTask.objects.filter(id=task_id).update(task_id=uuid4())

    monkeypatch.setattr(services, "_queue_estimate_task", fake_queue)
    recovered = recover_estimate_tasks()

    assert recovered == 2
    interrupted.refresh_from_db()
    terminal.refresh_from_db()
    assert interrupted.state == EstimateTask.State.WAITING
    assert "Worker interrupted" in interrupted.failure_reason
    assert terminal.state == EstimateTask.State.WAITING
    assert len(queued) == 2
    assert set(queued) == {interrupted.id, terminal.id}


def _task_with_calculation_version(
    *, fixture: dict[str, object], state: str, calculation_version: str, fingerprint: str,
) -> EstimateTask:
    snapshot = fixture["snapshot"]
    return EstimateTask.objects.create(
        snapshot=snapshot,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=fixture["forest"].version,
        # The terrain version differs per task, because each input tuple may hold one task only.
        terrain_build_version=f"test-terrain-{fingerprint[:8]}",
        calculation_version=calculation_version,
        input_fingerprint=fingerprint,
        state=state,
    )


@pytest.mark.django_db(transaction=True)
def test_task_queued_with_an_older_method_closes_without_error_or_publication(
    complete_estimate_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from estimate_updates import services
    from estimate_updates.tasks import estimate_update_task

    monkeypatch.setattr(services, "calculation_version", lambda: "b" * 64)

    def fail(**kwargs):
        raise AssertionError("A replaced task must not calculate.")

    monkeypatch.setattr(services, "calculate_and_publish_estimates", fail)
    task_row = _task_with_calculation_version(
        fixture=complete_estimate_fixture, state=EstimateTask.State.WAITING,
        calculation_version="a" * 64, fingerprint="1" * 64,
    )

    assert estimate_update_task.call(job_id=str(task_row.id)) == ""
    task_row.refresh_from_db()
    assert task_row.state == EstimateTask.State.FAILED
    assert task_row.failure_reason == services.REPLACED_TASK_REASON
    assert task_row.attempt_count == 0
    assert task_row.ended_at is not None
    assert EstimateUpdate.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_startup_recovery_closes_tasks_of_an_older_method_without_queueing_them(
    complete_estimate_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from estimate_updates import services

    monkeypatch.setattr(services, "calculation_version", lambda: "b" * 64)
    monkeypatch.setattr(services, "_task_backend_status", lambda *, task_id: None)
    queued: list[object] = []
    monkeypatch.setattr(services, "_queue_estimate_task", lambda *, task_id: queued.append(task_id))
    old_waiting = _task_with_calculation_version(
        fixture=complete_estimate_fixture, state=EstimateTask.State.WAITING,
        calculation_version="a" * 64, fingerprint="2" * 64,
    )
    old_running = _task_with_calculation_version(
        fixture=complete_estimate_fixture, state=EstimateTask.State.RUNNING,
        calculation_version="a" * 64, fingerprint="3" * 64,
    )
    current = _task_with_calculation_version(
        fixture=complete_estimate_fixture, state=EstimateTask.State.WAITING,
        calculation_version="b" * 64, fingerprint="4" * 64,
    )

    recover_estimate_tasks()

    for task_row in (old_waiting, old_running):
        task_row.refresh_from_db()
        assert task_row.state == EstimateTask.State.FAILED
        assert task_row.failure_reason == services.REPLACED_TASK_REASON
    current.refresh_from_db()
    assert current.state == EstimateTask.State.WAITING
    assert current.id in queued
    assert old_waiting.id not in queued
    assert old_running.id not in queued


@pytest.mark.django_db(transaction=True)
def test_startup_recovery_recreates_missing_ready_weather_handoff(
    complete_estimate_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from estimate_updates import services

    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    queued: list[object] = []
    monkeypatch.setattr(settings, "SPORAMETEO_FOREST_BUILD_VERSION", forest.version)

    def fake_queue(*, task_id):
        queued.append(task_id)
        EstimateTask.objects.filter(id=task_id).update(task_id=uuid4())

    monkeypatch.setattr(services, "_queue_estimate_task", fake_queue)

    recovered = recover_estimate_handoffs()

    task = EstimateTask.objects.get(snapshot=snapshot)
    assert recovered == 1
    assert task.state == EstimateTask.State.WAITING
    assert task.task_id is not None
    assert queued == [task.id]


@pytest.mark.django_db(transaction=True)
def test_cleanup_removes_terminal_old_tasks_and_protects_active_snapshot(
    complete_estimate_fixture: dict[str, object],
) -> None:
    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    terrain = complete_estimate_fixture["terrain"]
    newer_snapshot = _create_followup_snapshot(
        source_snapshot=snapshot,
        assessment_date=snapshot.assessment_date.fromordinal(snapshot.assessment_date.toordinal() + 1),
    )
    newest_snapshot = _create_followup_snapshot(
        source_snapshot=snapshot,
        assessment_date=snapshot.assessment_date.fromordinal(snapshot.assessment_date.toordinal() + 2),
    )
    oldest = calculate_and_publish_estimates(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )
    calculate_and_publish_estimates(
        snapshot_id=newer_snapshot.id,
        cell_key=newer_snapshot.cell_key,
        assessment_date=newer_snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )
    calculate_and_publish_estimates(
        snapshot_id=newest_snapshot.id,
        cell_key=newest_snapshot.cell_key,
        assessment_date=newest_snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )
    terminal = EstimateTask.objects.create(
        snapshot=snapshot,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
        published_update=oldest.update,
        input_fingerprint="1" * 64,
        state=EstimateTask.State.COMPLETED,
    )
    active = EstimateTask.objects.create(
        snapshot=snapshot,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version="active-terrain-version",
        input_fingerprint="2" * 64,
        state=EstimateTask.State.WAITING,
    )

    retained = cleanup_estimate_updates(cleanup_date=newest_snapshot.assessment_date)

    assert not EstimateUpdate.objects.filter(id=oldest.update.id).exists()
    assert not EstimateTask.objects.filter(id=terminal.id).exists()
    assert EstimateTask.objects.filter(id=active.id).exists()
    assert str(snapshot.id) in retained


@pytest.mark.django_db(transaction=True)
def test_cleanup_keeps_all_updates_for_two_newest_assessment_dates(
    complete_estimate_fixture: dict[str, object],
) -> None:
    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    forest_cell = complete_estimate_fixture["forest_cell"]
    terrain = complete_estimate_fixture["terrain"]
    assert isinstance(snapshot, WeatherSnapshot)
    assert isinstance(forest, ForestBuild)
    assert isinstance(forest_cell, ForestCell)
    assert isinstance(terrain, TerrainBuild)
    current_date = snapshot.assessment_date
    previous_date = current_date.fromordinal(current_date.toordinal() - 1)
    older_date = current_date.fromordinal(current_date.toordinal() - 2)
    previous_snapshot = _create_followup_snapshot(
        source_snapshot=snapshot,
        assessment_date=previous_date,
    )
    older_snapshot = _create_followup_snapshot(
        source_snapshot=snapshot,
        assessment_date=older_date,
    )
    suffix = uuid4().hex[:10]
    rebuilt_forest = ForestBuild.objects.create(
        version=f"test-forest-rebuild-{suffix}",
        grid_build=forest.grid_build,
        fingerprint="9" * 64,
        status=ForestBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=1,
        forest_cell_count=1,
    )
    rebuilt_chunk = ForestChunk.objects.create(
        build=rebuilt_forest,
        grid_chunk=forest_cell.chunk.grid_chunk,
        west_e=forest_cell.chunk.west_e,
        south_n=forest_cell.chunk.south_n,
        east_e=forest_cell.chunk.east_e,
        north_n=forest_cell.chunk.north_n,
        status=ForestChunk.Status.COMPLETED,
        cell_count=1,
    )
    rebuilt_cell = ForestCell.objects.create(
        build=rebuilt_forest,
        grid_build_cell=forest_cell.grid_build_cell,
        chunk=rebuilt_chunk,
        total_pixel_count=forest_cell.total_pixel_count,
        valid_pixel_count=forest_cell.valid_pixel_count,
        forest_pixel_count=forest_cell.forest_pixel_count,
        broadleaf_pixel_count=forest_cell.broadleaf_pixel_count,
        valid_coverage=forest_cell.valid_coverage,
        coverage_quality=forest_cell.coverage_quality,
        forest_share=forest_cell.forest_share,
        broadleaf_share=forest_cell.broadleaf_share,
        leaf_type=forest_cell.leaf_type,
        is_forest=forest_cell.is_forest,
        weather_latitude=forest_cell.weather_latitude,
        weather_longitude=forest_cell.weather_longitude,
    )

    oldest = calculate_and_publish_estimates(
        snapshot_id=older_snapshot.id,
        cell_key=older_snapshot.cell_key,
        assessment_date=older_snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )
    previous = calculate_and_publish_estimates(
        snapshot_id=previous_snapshot.id,
        cell_key=previous_snapshot.cell_key,
        assessment_date=previous_snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )
    current = calculate_and_publish_estimates(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
    )
    same_day_rebuild = calculate_and_publish_estimates(
        snapshot_id=snapshot.id,
        cell_key=snapshot.cell_key,
        assessment_date=snapshot.assessment_date,
        forest_build_version=rebuilt_forest.version,
        terrain_build_version=terrain.version,
    )

    retained = cleanup_estimate_updates(cleanup_date=current_date)

    assert set(retained) == {str(previous_snapshot.id), str(snapshot.id)}
    assert current.created is True
    assert same_day_rebuild.created is True
    assert current.update.public_id != same_day_rebuild.update.public_id
    assert EstimateUpdate.objects.filter(cell_key=snapshot.cell_key).count() == 3
    assert set(EstimateUpdate.objects.values_list("id", flat=True)) == {
        previous.update.id,
        current.update.id,
        same_day_rebuild.update.id,
    }
    assert not EstimateUpdate.objects.filter(id=oldest.update.id).exists()
    expired = read_estimate(
        update_id=oldest.update.public_id,
        forest_cell_id=forest_cell.id,
        target_date=older_date,
    )
    assert expired.status is EstimateReadStatus.EXPIRED
    assert EstimateUpdate.objects.filter(assessment_date=previous_date).exists()
    assert EstimateUpdate.objects.filter(assessment_date=current_date).count() == 2
    assert rebuilt_cell.build_id == same_day_rebuild.update.forest_build_id


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failure", ["mixed_method", "nonfinite", "unknown_reason"])
def test_calculation_rejects_invalid_rows_before_publication(
    complete_estimate_fixture: dict[str, object],
    failure: str,
) -> None:
    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    terrain = complete_estimate_fixture["terrain"]

    def invalid_calculator(*, inputs):
        result = calculate_forecast(inputs=inputs)
        if failure == "mixed_method":
            return replace(result, method_version="porcini-experimental-other")
        if failure == "nonfinite":
            return replace(result, whole_percentage=math.inf)
        return replace(result, whole_percentage=None, unavailable_reason="made_up")

    with pytest.raises(EstimateValidationError):
        calculate_estimate_update(
            snapshot_id=snapshot.id,
            cell_key="44.00,11.00",
            assessment_date=snapshot.assessment_date,
            forest_build_version=forest.version,
            terrain_build_version=terrain.version,
            calculator=invalid_calculator,
        )
    assert not EstimateUpdate.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_weather_callback_creates_one_durable_estimate_task_for_same_input(
    complete_estimate_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from estimate_updates import services

    snapshot = complete_estimate_fixture["snapshot"]
    forest = complete_estimate_fixture["forest"]
    queued: list[object] = []

    def fake_queue(*, task_id):
        queued.append(task_id)
        EstimateTask.objects.filter(id=task_id).update(task_id=uuid4())

    monkeypatch.setattr(services, "_queue_estimate_task", fake_queue)

    first = services.enqueue_estimate_update(
        snapshot_id=snapshot.id,
        cell_key="44.00,11.00",
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
    )
    second = services.enqueue_estimate_update(
        snapshot_id=snapshot.id,
        cell_key="44.00,11.00",
        assessment_date=snapshot.assessment_date,
        forest_build_version=forest.version,
    )

    assert first.id == second.id
    assert EstimateTask.objects.count() == 1
    assert queued == [first.id]
