"""Focused checks for selected-square detail responses."""

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
import uuid

import pytest
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.cache import caches
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.test import Client

from config.api import api
from estimate_updates.contracts import encode_result_bytes
from estimate_updates.models import (
    EstimateResult,
    EstimateUpdate,
    ForecastRun,
    ForecastRunCell,
    PublishedForecastRun,
)
from estimate_updates.services import input_fingerprint
from map_api.api import router as map_router
from sporameteo.finds.grid import format_cell_id
from national_forest.models import ForestBuild, ForestCell, ForestChunk
from national_grid.models import GridBuild, GridBuildCell, GridCell, GridChunk, GridRegion, Municipality
from national_terrain.models import (
    TerrainBuild,
    TerrainCell,
    TerrainChunk,
    TerrainChunkSource,
    TerrainSourceTile,
)
from area_api.router import router
from weather_summaries.models import WeatherDaily, WeatherSnapshot, WeatherSnapshotDay, WeatherSummary


@pytest.fixture(scope="module", autouse=True)
def register_area_router() -> None:
    """Register the area router while the shared API module is under construction."""
    if not any(existing_router is router for _, existing_router, *_ in api._router_registrations):
        api.add_router("", router)
    if not any(existing_router is map_router for _, existing_router, *_ in api._router_registrations):
        api.add_router("", map_router)


@pytest.fixture
def area_fixture() -> dict[str, object]:
    """Create one frozen update, source manifest and weather snapshot."""
    suffix = uuid.uuid4().hex[:10]
    assessment = date(2026, 10, 11)
    west, south = 4_300_000, 2_300_000
    square_id = format_cell_id(corner_e=west, corner_n=south)
    geometry = Polygon.from_bbox((west, south, west + 500, south + 500))
    multipolygon = MultiPolygon(geometry, srid=3035)
    grid = GridBuild.objects.create(
        version=f"area-grid-{suffix}",
        fingerprint="a" * 64,
        status=GridBuild.Status.COMPLETED,
        source_metadata={
            "dataset": "Fixture ISTAT boundaries",
            "version": "fixture-2026",
            "archive_sha256": "1" * 64,
            "source_url": "https://example.test/istat-source",
            "source_page": "https://example.test/istat-page",
            "license": "Fixture ISTAT licence",
            "license_url": "https://example.test/istat-license",
            "credit": "Fixture ISTAT credit",
        },
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=1,
    )
    region = GridRegion.objects.create(
        build=grid,
        istat_code="09",
        name=f"Area region {suffix}",
        geometry=multipolygon,
    )
    grid_chunk = GridChunk.objects.create(
        build=grid,
        west_e=west,
        south_n=south,
        east_e=west + 500,
        north_n=south + 500,
        status=GridChunk.Status.COMPLETED,
        cell_count=1,
    )
    grid_cell = GridCell.objects.create(
        cell_id=square_id,
        corner_e=west,
        corner_n=south,
        geometry=geometry,
    )
    membership = GridBuildCell.objects.create(
        build=grid,
        cell=grid_cell,
        chunk=grid_chunk,
        owner_region=region,
        land_geometry=multipolygon,
        land_share=1.0,
    )
    forest = ForestBuild.objects.create(
        version=f"area-forest-{suffix}",
        grid_build=grid,
        fingerprint="b" * 64,
        status=ForestBuild.Status.COMPLETED,
        source_release="HRL Forest Type 2018",
        source_metadata={"catalogue": "forest-catalogue", "native_resolution_m": 10},
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=1,
        forest_cell_count=1,
    )
    forest_chunk = ForestChunk.objects.create(
        build=forest,
        grid_chunk=grid_chunk,
        west_e=west,
        south_n=south,
        east_e=west + 500,
        north_n=south + 500,
        status=ForestChunk.Status.COMPLETED,
        cell_count=1,
        source_sha256="d" * 64,
        source_retrieved_at="2026-10-10T10:00:00+00:00",
    )
    forest_cell = ForestCell.objects.create(
        build=forest,
        grid_build_cell=membership,
        chunk=forest_chunk,
        total_pixel_count=100,
        valid_pixel_count=100,
        forest_pixel_count=100,
        broadleaf_pixel_count=100,
        valid_coverage=1.0,
        coverage_quality="high",
        forest_share=0.8,
        broadleaf_share=0.8,
        leaf_type=ForestCell.LeafType.BROADLEAF,
        is_forest=True,
        weather_latitude=44.0,
        weather_longitude=11.0,
    )
    terrain = TerrainBuild.objects.create(
        version=f"area-terrain-{suffix}",
        grid_build=grid,
        fingerprint="c" * 64,
        grid_fingerprint=grid.fingerprint,
        source_manifest_fingerprint="e" * 64,
        source_metadata={
            "release": "Copernicus DEM GLO-30 public AWS copy, 2021",
            "native_pixel_m": 30.0,
            "source_url": "https://registry.opendata.aws/copernicus-dem/",
            "source_dataset": "https://example.test/cop-dem",
            "credit": "WorldDEM licence and no-liability notice",
        },
        status=TerrainBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        result_count=1,
    )
    terrain_chunk = TerrainChunk.objects.create(
        build=terrain,
        grid_chunk=grid_chunk,
        status=TerrainChunk.Status.COMPLETED,
        result_count=1,
    )
    terrain_cell = TerrainCell.objects.create(
        build=terrain,
        grid_build_cell=membership,
        chunk=terrain_chunk,
        altitude_m=500.0,
        slope_deg=4.0,
        aspect_deg=90.0,
        altitude_valid_fraction=1.0,
        slope_valid_fraction=1.0,
        aspect_valid_fraction=1.0,
        altitude_quality="high",
        slope_quality="high",
        aspect_quality="high",
    )
    terrain_tile = TerrainSourceTile.objects.create(
        build=terrain,
        tile_name="N44_00_E011_00",
        status=TerrainSourceTile.Status.AVAILABLE,
        url="https://example.test/dem.tif",
        release="Copernicus DEM GLO-30 public AWS copy, 2021",
        retrieved_at=datetime(2026, 10, 9, 12, tzinfo=UTC),
        size_bytes=123,
        sha256="f" * 64,
        crs="EPSG:4326",
        native_pixel_m=30.0,
        vertical_datum="EGM2008",
        credit="WorldDEM licence and no-liability notice",
    )
    TerrainChunkSource.objects.create(chunk=terrain_chunk, tile=terrain_tile)
    snapshot = WeatherSnapshot.objects.create(
        cell_key="44.00,11.00",
        assessment_date=assessment,
        reference_elevation_m=500.0,
        latest_retrieval=datetime(2026, 10, 11, 10, tzinfo=UTC),
        source_ids=["weather:source"],
        source_attempt_ids=["weather:attempt"],
        metadata={"provider": "Open-Meteo"},
    )
    daily_rows: list[WeatherDaily] = []
    for offset in range(-21, 8):
        current = assessment + timedelta(days=offset)
        daily_rows.append(
            WeatherDaily(
                cell_key=snapshot.cell_key,
                date=current,
                source_provider="Open-Meteo",
                source_model="ecmwf_ifs04",
                source_endpoint="https://example.test/weather",
                source_id="weather:source",
                request_id="request-1",
                attempt_id=uuid.uuid4(),
                retrieved_at=datetime(2026, 10, 11, 10, tzinfo=UTC),
                requested_latitude=44.0,
                requested_longitude=11.0,
                returned_latitude=44.01,
                returned_longitude=11.01,
                reference_elevation_m=500.0,
                rain_mm=1000.0 if offset > 0 else 0.0 if offset == 0 else 2.0,
                mean_temperature_c=15.0,
                min_temperature_c=8.0,
                status=WeatherDaily.Status.COMPLETE,
                metadata={
                    "source": {"value_kind": "modelled provider values"},
                    "variables": {
                        "precipitation": {"unit": "mm", "reason": "complete"},
                        "temperature": {"unit": "deg_c", "reason": "complete"},
                        "et0": {"unit": "mm", "reason": "optional_variable_absent"},
                    },
                },
            )
        )
    WeatherDaily.objects.bulk_create(daily_rows)
    daily_rows = list(WeatherDaily.objects.filter(cell_key=snapshot.cell_key).order_by("date"))
    WeatherSnapshotDay.objects.bulk_create(
        [WeatherSnapshotDay(snapshot=snapshot, date=row.date, daily=row) for row in daily_rows]
    )
    summaries: list[WeatherSummary] = []
    for days_ahead in range(8):
        target = assessment + timedelta(days=days_ahead)
        summaries.append(
            WeatherSummary(
                snapshot=snapshot,
                target_date=target,
                days_ahead=days_ahead,
                visitor_rain_7_mm=14.0,
                visitor_rain_14_mm=28.0,
                visitor_rain_21_mm=42.0,
                old_rain_mm=28.0,
                recent_rain_mm=14.0,
                target_rain_mm=1000.0 if days_ahead else 0.0,
                target_mean_temperature_c=15.0,
                target_min_temperature_c=8.0,
                metadata={
                    "old_rain": {
                        "period_start": (target - timedelta(days=21)).isoformat(),
                        "period_end": (target - timedelta(days=8)).isoformat(),
                        "completed_day_count": 14,
                        "forecast_day_count": 0,
                        "source_ids": ["weather:source"],
                    },
                    "recent_rain": {
                        "period_start": (target - timedelta(days=7)).isoformat(),
                        "period_end": (target - timedelta(days=1)).isoformat(),
                        "completed_day_count": 7 - days_ahead,
                        "forecast_day_count": days_ahead,
                        "source_ids": ["weather:source"],
                    },
                    "target": {
                        "period_start": target.isoformat(),
                        "period_end": target.isoformat(),
                        "completed_day_count": 0,
                        "forecast_day_count": 1,
                        "source_ids": ["weather:source"],
                        "source_attempt_ids": ["weather:attempt"],
                    },
                },
            )
        )
    WeatherSummary.objects.bulk_create(summaries)
    update = EstimateUpdate.objects.create(
        cell_key=snapshot.cell_key,
        assessment_date=assessment,
        calculated_at=datetime(2026, 10, 11, 11, tzinfo=UTC),
        snapshot=snapshot,
        forest_build=forest,
        terrain_build=terrain,
        method_version="porcini-experimental-v1",
        row_count=1,
        input_fingerprint=input_fingerprint(
            cell_key=snapshot.cell_key,
            assessment_date=assessment,
            snapshot_id=snapshot.id,
            forest_build_id=forest.id,
            terrain_build_id=terrain.id,
            method_version="porcini-experimental-v1",
        ),
    )
    EstimateResult.objects.create(
        update=update,
        forest_cell=forest_cell,
        values=encode_result_bytes(
            percentages=[52, 53, 54, 55, 56, 57, 58, 59],
            unavailable_reasons=[None] * 8,
        ),
    )
    return {
        "square_id": square_id,
        "assessment": assessment,
        "forest_cell": forest_cell,
        "update": update,
        "snapshot": snapshot,
        "terrain_cell": terrain_cell,
    }


def _add_noforest_cell(*, area_fixture: dict[str, object]) -> str:
    """Add a second stable square with an explicit no-forest result."""
    forest_cell = area_fixture["forest_cell"]
    assert isinstance(forest_cell, ForestCell)
    membership = forest_cell.grid_build_cell
    west = membership.cell.corner_e + 1_000
    south = membership.cell.corner_n
    cell_id = format_cell_id(corner_e=west, corner_n=south)
    geometry = Polygon.from_bbox((west, south, west + 500, south + 500))
    grid_cell = GridCell.objects.create(
        cell_id=cell_id,
        corner_e=west,
        corner_n=south,
        geometry=geometry,
    )
    second_membership = GridBuildCell.objects.create(
        build=membership.build,
        cell=grid_cell,
        chunk=membership.chunk,
        owner_region=membership.owner_region,
        land_geometry=MultiPolygon(geometry, srid=3035),
        land_share=1.0,
    )
    ForestCell.objects.create(
        build=forest_cell.build,
        grid_build_cell=second_membership,
        chunk=forest_cell.chunk,
        total_pixel_count=100,
        valid_pixel_count=100,
        forest_pixel_count=0,
        broadleaf_pixel_count=0,
        valid_coverage=1.0,
        coverage_quality="high",
        forest_share=0.0,
        broadleaf_share=0.0,
        leaf_type=ForestCell.LeafType.NO_FOREST,
        is_forest=False,
    )
    return cell_id


def _add_missing_forest_cell(*, area_fixture: dict[str, object]) -> str:
    """Add a second stable square without required forest source data."""
    forest_cell = area_fixture["forest_cell"]
    assert isinstance(forest_cell, ForestCell)
    membership = forest_cell.grid_build_cell
    west = membership.cell.corner_e + 500
    south = membership.cell.corner_n
    cell_id = format_cell_id(corner_e=west, corner_n=south)
    geometry = Polygon.from_bbox((west, south, west + 500, south + 500))
    grid_cell = GridCell.objects.create(
        cell_id=cell_id,
        corner_e=west,
        corner_n=south,
        geometry=geometry,
    )
    second_membership = GridBuildCell.objects.create(
        build=membership.build,
        cell=grid_cell,
        chunk=membership.chunk,
        owner_region=membership.owner_region,
        land_geometry=MultiPolygon(geometry, srid=3035),
        land_share=1.0,
    )
    ForestCell.objects.create(
        build=forest_cell.build,
        grid_build_cell=second_membership,
        chunk=forest_cell.chunk,
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
        missing_reason="missing_forest_data",
    )
    return cell_id


def _create_changed_forest_update(
    *,
    area_fixture: dict[str, object],
    percentages: list[int | None] | None = None,
    unavailable_reasons: list[str | None] | None = None,
) -> tuple[EstimateUpdate, ForestCell]:
    """Create a new immutable forest input and publication for the same snapshot."""
    old_update = area_fixture["update"]
    old_cell = area_fixture["forest_cell"]
    assert isinstance(old_update, EstimateUpdate)
    assert isinstance(old_cell, ForestCell)
    old_build = old_cell.build
    suffix = uuid.uuid4().hex[:10]
    forest = ForestBuild.objects.create(
        version=f"area-forest-changed-{suffix}",
        grid_build=old_build.grid_build,
        fingerprint="7" * 64,
        status=ForestBuild.Status.COMPLETED,
        source_release=old_build.source_release,
        source_metadata=dict(old_build.source_metadata),
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=1,
        forest_cell_count=1,
    )
    chunk = ForestChunk.objects.create(
        build=forest,
        grid_chunk=old_cell.chunk.grid_chunk,
        west_e=old_cell.chunk.west_e,
        south_n=old_cell.chunk.south_n,
        east_e=old_cell.chunk.east_e,
        north_n=old_cell.chunk.north_n,
        status=ForestChunk.Status.COMPLETED,
        cell_count=1,
        source_sha256="8" * 64,
        source_retrieved_at="2026-10-11T10:00:00+00:00",
    )
    changed_cell = ForestCell.objects.create(
        build=forest,
        grid_build_cell=old_cell.grid_build_cell,
        chunk=chunk,
        total_pixel_count=100,
        valid_pixel_count=100,
        forest_pixel_count=100,
        broadleaf_pixel_count=100,
        valid_coverage=1.0,
        coverage_quality="high",
        forest_share=0.5,
        broadleaf_share=0.5,
        leaf_type=ForestCell.LeafType.BROADLEAF,
        is_forest=True,
        weather_latitude=44.0,
        weather_longitude=11.0,
    )
    update = EstimateUpdate.objects.create(
        cell_key=old_update.cell_key,
        assessment_date=old_update.assessment_date,
        calculated_at=old_update.calculated_at + timedelta(minutes=2),
        snapshot=old_update.snapshot,
        forest_build=forest,
        terrain_build=old_update.terrain_build,
        method_version=old_update.method_version,
        row_count=1,
        input_fingerprint=input_fingerprint(
            cell_key=old_update.cell_key,
            assessment_date=old_update.assessment_date,
            snapshot_id=old_update.snapshot_id,
            forest_build_id=forest.id,
            terrain_build_id=old_update.terrain_build_id,
            method_version=old_update.method_version,
        ),
    )
    EstimateResult.objects.create(
        update=update,
        forest_cell=changed_cell,
        values=encode_result_bytes(
            percentages=percentages or [62, 63, 64, 65, 66, 67, 68, 69],
            unavailable_reasons=unavailable_reasons or [None] * 8,
        ),
    )
    return update, changed_cell


def _create_frost_update(*, area_fixture: dict[str, object]) -> EstimateUpdate:
    """Create a later immutable snapshot whose supported frost derivation is true."""
    old_update = area_fixture["update"]
    old_snapshot = area_fixture["snapshot"]
    old_cell = area_fixture["forest_cell"]
    assert isinstance(old_update, EstimateUpdate)
    assert isinstance(old_snapshot, WeatherSnapshot)
    assert isinstance(old_cell, ForestCell)
    assessment = old_snapshot.assessment_date + timedelta(days=1)
    snapshot = WeatherSnapshot.objects.create(
        cell_key=old_snapshot.cell_key,
        assessment_date=assessment,
        reference_elevation_m=old_snapshot.reference_elevation_m,
        latest_retrieval=old_snapshot.latest_retrieval,
        source_ids=["weather:frost-source"],
        source_attempt_ids=["weather:frost-attempt"],
        metadata={"provider": "Open-Meteo"},
    )
    rows: list[WeatherDaily] = []
    for offset in range(-21, 8):
        current = assessment + timedelta(days=offset)
        rows.append(
            WeatherDaily(
                cell_key=snapshot.cell_key,
                date=current,
                source_provider="Open-Meteo",
                source_model="ecmwf_ifs04",
                source_endpoint="https://example.test/weather",
                source_id="weather:frost-source",
                request_id="frost-request",
                attempt_id=uuid.uuid4(),
                retrieved_at=snapshot.latest_retrieval,
                requested_latitude=44.0,
                requested_longitude=11.0,
                returned_latitude=44.01,
                returned_longitude=11.01,
                reference_elevation_m=500.0,
                rain_mm=1000.0 if offset > 0 else 0.0 if offset == 0 else 2.0,
                mean_temperature_c=15.0,
                min_temperature_c=-1.0 if offset == 0 else 8.0,
                status=WeatherDaily.Status.COMPLETE,
                metadata={
                    "source": {"value_kind": "modelled provider values"},
                    "variables": {
                        "precipitation": {"unit": "mm", "reason": "complete"},
                        "temperature": {"unit": "deg_c", "reason": "complete"},
                    },
                },
            )
        )
    WeatherDaily.objects.bulk_create(rows)
    rows = list(WeatherDaily.objects.filter(cell_key=snapshot.cell_key).order_by("-created_at")[:29])
    rows_by_date = {row.date: row for row in rows}
    WeatherSnapshotDay.objects.bulk_create(
        [
            WeatherSnapshotDay(snapshot=snapshot, date=current, daily=rows_by_date[current])
            for current in (assessment + timedelta(days=offset) for offset in range(-21, 8))
        ]
    )
    summaries: list[WeatherSummary] = []
    for days_ahead in range(8):
        target = assessment + timedelta(days=days_ahead)
        summaries.append(
            WeatherSummary(
                snapshot=snapshot,
                target_date=target,
                days_ahead=days_ahead,
                visitor_rain_7_mm=14.0,
                visitor_rain_14_mm=28.0,
                visitor_rain_21_mm=42.0,
                old_rain_mm=28.0,
                recent_rain_mm=14.0,
                target_rain_mm=1000.0 if days_ahead else 0.0,
                target_mean_temperature_c=15.0,
                target_min_temperature_c=-1.0 if days_ahead == 0 else 8.0,
                frost=days_ahead == 0,
                metadata={
                    "old_rain": {
                        "period_start": (target - timedelta(days=21)).isoformat(),
                        "period_end": (target - timedelta(days=8)).isoformat(),
                        "completed_day_count": 14,
                        "forecast_day_count": 0,
                        "source_ids": ["weather:frost-source"],
                    },
                    "recent_rain": {
                        "period_start": (target - timedelta(days=7)).isoformat(),
                        "period_end": (target - timedelta(days=1)).isoformat(),
                        "completed_day_count": 7 - days_ahead,
                        "forecast_day_count": days_ahead,
                        "source_ids": ["weather:frost-source"],
                    },
                    "target": {
                        "period_start": target.isoformat(),
                        "period_end": target.isoformat(),
                        "completed_day_count": 0,
                        "forecast_day_count": 1,
                        "source_ids": ["weather:frost-source"],
                        "source_attempt_ids": ["weather:frost-attempt"],
                    },
                },
            )
        )
    WeatherSummary.objects.bulk_create(summaries)
    update = EstimateUpdate.objects.create(
        cell_key=old_update.cell_key,
        assessment_date=assessment,
        calculated_at=old_update.calculated_at + timedelta(days=1),
        snapshot=snapshot,
        forest_build=old_update.forest_build,
        terrain_build=old_update.terrain_build,
        method_version=old_update.method_version,
        row_count=1,
        input_fingerprint=input_fingerprint(
            cell_key=old_update.cell_key,
            assessment_date=assessment,
            snapshot_id=snapshot.id,
            forest_build_id=old_update.forest_build_id,
            terrain_build_id=old_update.terrain_build_id,
            method_version=old_update.method_version,
        ),
    )
    EstimateResult.objects.create(
        update=update,
        forest_cell=old_cell,
        values=encode_result_bytes(
            percentages=[52, 53, 54, 55, 56, 57, 58, 59],
            unavailable_reasons=[None] * 8,
        ),
    )
    return update


@pytest.mark.django_db(transaction=True)
def test_area_detail_uses_exact_update_and_exposes_frozen_facts(area_fixture: dict[str, object]) -> None:
    update = area_fixture["update"]
    square_id = area_fixture["square_id"]
    response = Client().get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-12", "update_id": str(update.public_id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["selection"]["square_id"] == square_id
    assert body["selection"]["target_date"] == "2026-10-12"
    assert body["selection"]["assessment_date"] == "2026-10-11"
    assert body["selection"]["days_ahead"] == 1
    assert body["selection"]["update_id"] == str(update.public_id)
    assert body["estimate"]["state"] == "available"
    assert body["estimate"]["whole_percentage"] == 53
    assert body["estimate"]["experimental_unvalidated"] is True
    assert body["estimate"]["experimental_explanation_key"] == "estimate.experimental_unvalidated"
    assert "reliability" not in body["estimate"]
    assert body["weather"]["selected_day"]["date"] == "2026-10-12"
    assert body["weather"]["selected_day"]["rain_mm"] == 1000.0
    assert body["weather"]["recent_rain_7_days"]["value"] == 14.0
    assert body["weather"]["recent_rain_14_days"]["value"] == 28.0
    assert body["weather"]["recent_rain_21_days"]["value"] == 42.0
    assert body["weather"]["recent_rain_7_days"]["forecast_day_count"] == 0
    assert body["weather"]["recent_rain_14_days"]["forecast_day_count"] == 0
    assert body["weather"]["recent_rain_21_days"]["forecast_day_count"] == 0
    assert body["weather"]["recent_rain_7_days"]["period_start"] == "2026-10-04"
    assert body["weather"]["recent_rain_7_days"]["period_end"] == "2026-10-10"
    assert body["weather"]["recent_rain_14_days"]["period_start"] == "2026-09-27"
    assert body["weather"]["recent_rain_14_days"]["period_end"] == "2026-10-10"
    assert body["weather"]["recent_rain_21_days"]["period_start"] == "2026-09-20"
    assert body["weather"]["recent_rain_21_days"]["period_end"] == "2026-10-10"
    assert body["weather"]["provider"] == "Open-Meteo"
    assert body["weather"]["model"] == "ecmwf_ifs04"
    assert body["weather"]["endpoint"] == "https://example.test/weather"
    assert body["weather"]["retrieval_time"] == "2026-10-11T10:00:00Z"
    assert body["weather"]["source_ids"] == ["weather:source"]
    assert body["weather"]["source_attempt_ids"] == ["weather:attempt"]
    assert body["weather"]["requested_coordinates"] == {"latitude": 44.0, "longitude": 11.0}
    assert body["weather"]["returned_coordinates"] == {"latitude": 44.01, "longitude": 11.01}
    assert body["weather"]["reference_elevation_m"] == 500.0
    assert body["sources"]["grid"] == {
        "name": "Fixture ISTAT boundaries",
        "release": "fixture-2026",
        "native_resolution_m": None,
        "catalogue_url": "https://example.test/istat-page",
        "service_url": "https://example.test/istat-source",
        "url": "https://example.test/istat-license",
        "licence": "Fixture ISTAT licence",
        "credit": "Fixture ISTAT credit",
        "retrieval_time": None,
        "retrieval_sha256": "1" * 64,
        "source_metadata": {
            "dataset": "Fixture ISTAT boundaries",
            "version": "fixture-2026",
            "archive_sha256": "1" * 64,
            "source_url": "https://example.test/istat-source",
            "source_page": "https://example.test/istat-page",
            "license": "Fixture ISTAT licence",
            "license_url": "https://example.test/istat-license",
            "credit": "Fixture ISTAT credit",
        },
        "source_tiles": [],
        "gaps": [],
    }
    assert body["sources"]["forest"]["retrieval_sha256"] == "d" * 64
    assert body["sources"]["forest"]["release"] == "HRL Forest Type 2018"
    assert body["sources"]["forest"]["native_resolution_m"] == 10.0
    assert body["sources"]["forest"]["catalogue_url"]
    assert "Copernicus" in body["sources"]["forest"]["licence"]
    assert "European Union" in body["sources"]["forest"]["credit"]
    assert body["sources"]["terrain"]["source_tiles"][0]["sha256"] == "f" * 64
    assert body["sources"]["forest"]["retrieval_time"] == "2026-10-10T10:00:00Z"
    assert body["sources"]["terrain"]["source_tiles"][0]["retrieved_at"] == "2026-10-09T12:00:00Z"
    assert "WorldDEM licence and no-liability notice" in body["sources"]["terrain"]["source_tiles"][0]["credit"]
    assert body["sources"]["weather"]["credit"] == {
        "text": "Weather data by Open-Meteo.com",
        "url": "https://open-meteo.com/",
        "licence": "CC BY 4.0",
    }
    assert body["weather"]["variable_gaps"]
    assert any(gap["reason_key"] == "optional_variable_absent" for gap in body["weather"]["variable_gaps"])
    facts = {fact["role_key"]: fact for fact in body["facts"]}
    assert set(facts) == {
        "older_rain",
        "recent_rain",
        "target_temperature_at_reference",
        "reference_altitude",
        "square_altitude",
        "corrected_temperature",
        "forest_share",
    }
    assert facts["older_rain"]["actual_value"] == 28.0
    assert facts["older_rain"]["unit"] == "mm"
    assert facts["older_rain"]["period_start"] == "2026-09-21"
    assert facts["older_rain"]["period_end"] == "2026-10-04"
    assert facts["older_rain"]["completed_day_count"] == 14
    assert facts["older_rain"]["forecast_day_count"] == 0
    assert facts["recent_rain"]["actual_value"] == 14.0
    assert facts["recent_rain"]["period_start"] == "2026-10-05"
    assert facts["recent_rain"]["period_end"] == "2026-10-11"
    assert facts["recent_rain"]["completed_day_count"] == 6
    assert facts["recent_rain"]["forecast_day_count"] == 1
    assert facts["target_temperature_at_reference"]["actual_value"] == 15.0
    assert facts["reference_altitude"]["actual_value"] == 500.0
    assert facts["square_altitude"]["actual_value"] == 500.0
    assert facts["forest_share"]["actual_value"] == 0.8
    assert {fact["translation_key"] for fact in facts.values()}


@pytest.mark.django_db(transaction=True)
def test_area_detail_names_the_municipality_only_when_one_is_stored(
    area_fixture: dict[str, object],
) -> None:
    update = area_fixture["update"]
    square_id = area_fixture["square_id"]
    query = {"target_date": "2026-10-12", "update_id": str(update.public_id)}

    without = Client().get(f"/api/v1/areas/{square_id}", query)
    caches["map"].clear()
    cell = GridCell.objects.get(cell_id=square_id)
    cell.municipality = Municipality.objects.create(
        istat_code="047007",
        name="Marliana",
        region_code="09",
        geometry=MultiPolygon(cell.geometry, srid=3035),
    )
    cell.save()
    with CaptureQueriesContext(connection) as queries:
        named = Client().get(f"/api/v1/areas/{square_id}", query)

    assert without.status_code == named.status_code == 200
    assert without.json()["selection"]["municipality_name"] is None
    assert named.json()["selection"]["municipality_name"] == "Marliana"
    # The name comes with the square query, not from an extra query on the municipality table.
    assert not any(query["sql"].startswith('SELECT "national_grid_municipality"') for query in queries)


@pytest.mark.django_db(transaction=True)
def test_area_detail_preserves_old_update_after_new_publication(area_fixture: dict[str, object]) -> None:
    update = area_fixture["update"]
    square_id = area_fixture["square_id"]
    client = Client()
    old_response = client.get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-11", "update_id": str(update.public_id)},
    )
    EstimateUpdate.objects.create(
        cell_key=update.cell_key,
        assessment_date=update.assessment_date,
        calculated_at=update.calculated_at + timedelta(minutes=1),
        snapshot=update.snapshot,
        forest_build=update.forest_build,
        terrain_build=update.terrain_build,
        method_version="porcini-experimental-v1-new",
        row_count=1,
        input_fingerprint="9" * 64,
    )
    retained_response = client.get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-11", "update_id": str(update.public_id)},
    )

    assert old_response.json()["estimate"] == retained_response.json()["estimate"]
    assert old_response.json()["facts"] == retained_response.json()["facts"]


@pytest.mark.django_db(transaction=True)
def test_area_detail_reports_a_deleted_update_as_expired(area_fixture: dict[str, object]) -> None:
    update = area_fixture["update"]
    square_id = area_fixture["square_id"]
    update_id = str(update.public_id)
    update.delete()

    response = Client().get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-11", "update_id": update_id},
    )

    assert response.status_code == 410
    assert response.json() == {
        "code": "update_expired",
        "message": "The requested estimate update is no longer available.",
        "message_key": "area.update_expired",
        "details": {"update_id": update_id},
    }


@pytest.mark.django_db(transaction=True)
def test_area_detail_facts_change_only_in_the_new_immutable_input_update(
    area_fixture: dict[str, object],
) -> None:
    old_update = area_fixture["update"]
    square_id = area_fixture["square_id"]
    new_update, _ = _create_changed_forest_update(area_fixture=area_fixture)
    client = Client()
    old_body = client.get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-11", "update_id": str(old_update.public_id)},
    ).json()
    new_body = client.get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-11", "update_id": str(new_update.public_id)},
    ).json()

    old_forest = next(fact for fact in old_body["facts"] if fact["role_key"] == "forest_share")
    new_forest = next(fact for fact in new_body["facts"] if fact["role_key"] == "forest_share")
    assert old_forest["actual_value"] == 0.8
    assert new_forest["actual_value"] == 0.5
    assert old_forest["source_id"] != new_forest["source_id"]
    assert old_body["selection"]["update_id"] != new_body["selection"]["update_id"]


@pytest.mark.django_db(transaction=True)
def test_area_detail_distinguishes_zero_rain_from_a_required_gap_and_optional_gap(
    area_fixture: dict[str, object],
) -> None:
    update = area_fixture["update"]
    square_id = area_fixture["square_id"]
    gap_update, _ = _create_changed_forest_update(
        area_fixture=area_fixture,
        percentages=[52, None, 54, 55, 56, 57, 58, 59],
        unavailable_reasons=[None, "recent_rain", None, None, None, None, None, None],
    )

    client = Client()
    zero_body = client.get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-11", "update_id": str(update.public_id)},
    ).json()
    gap_body = client.get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-12", "update_id": str(gap_update.public_id)},
    ).json()

    assert zero_body["estimate"]["state"] == "available"
    assert zero_body["estimate"]["whole_percentage"] == 52
    assert zero_body["weather"]["selected_day"]["rain_mm"] == 0.0
    assert zero_body["weather"]["selected_day"]["state"] == "available"
    assert gap_body["estimate"]["state"] == "unavailable"
    assert gap_body["estimate"]["whole_percentage"] is None
    assert gap_body["estimate"]["unavailable_reason"] == "recent_rain"
    assert gap_body["weather"]["recent_rain_7_days"]["value"] == 14.0
    assert gap_body["weather"]["selected_day"]["rain_mm"] == 1000.0
    assert gap_body["weather"]["selected_day"]["et0_mm"] is None


@pytest.mark.django_db(transaction=True)
def test_area_detail_returns_noforest_state_for_a_square_in_the_exact_build(
    area_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    noforest_id = _add_noforest_cell(area_fixture=area_fixture)
    update = area_fixture["update"]

    def fail_weather_data(*args: object, **kwargs: object) -> object:
        raise AssertionError("a no-forest square must not load weather")

    monkeypatch.setattr("area_api.router._weather_response", fail_weather_data)
    response = Client().get(
        f"/api/v1/areas/{noforest_id}",
        {"target_date": "2026-10-11", "update_id": str(update.public_id)},
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "no_forest",
        "message": "The selected square has no forest for this estimate.",
        "message_key": "area.no_forest",
        "details": {
            "square_id": noforest_id,
            "target_date": "2026-10-11",
            "update_id": str(update.public_id),
        },
    }


@pytest.mark.django_db(transaction=True)
def test_area_detail_without_update_classifies_configured_square_without_weather(
    area_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    noforest_id = _add_noforest_cell(area_fixture=area_fixture)
    missing_id = _add_missing_forest_cell(area_fixture=area_fixture)
    square_id = area_fixture["square_id"]

    def fail_transport(*args: object, **kwargs: object) -> None:
        raise AssertionError("classification must not request weather")

    monkeypatch.setattr("area_api.router._weather_response", fail_transport)
    monkeypatch.setattr("weather_on_demand.services.request_weather", fail_transport)
    monkeypatch.setattr("weather_requests.provider.request_weather", fail_transport, raising=False)
    monkeypatch.setattr("weather_requests.provider.fetch_weather", fail_transport, raising=False)
    client = Client()
    with override_settings(
        SPORAMETEO_FOREST_BUILD_VERSION=area_fixture["forest_cell"].build.version
    ):
        eligible = client.get(
            f"/api/v1/areas/{square_id}",
            {"target_date": "2026-10-12"},
        )
        noforest = client.get(
            f"/api/v1/areas/{noforest_id}",
            {"target_date": "2026-10-12"},
        )
        missing = client.get(
            f"/api/v1/areas/{missing_id}",
            {"target_date": "2026-10-12"},
        )
        exact_missing = client.get(
            f"/api/v1/areas/{missing_id}",
            {
                "target_date": "2026-10-12",
                "update_id": str(area_fixture["update"].public_id),
            },
        )
        unknown = client.get(
            "/api/v1/areas/500m_E4301500_N2300000",
            {"target_date": "2026-10-12"},
        )

    assert eligible.status_code == 409
    assert eligible.json()["code"] == "outside_prepared_area"
    assert eligible.json()["message_key"] == "area.outside_prepared_area"
    assert eligible.json()["details"] == {
        "square_id": square_id,
        "target_date": "2026-10-12",
    }
    assert noforest.status_code == 409
    assert noforest.json()["code"] == "no_forest"
    assert noforest.json()["message_key"] == "area.no_forest"
    assert missing.status_code == 409
    assert missing.json()["code"] == "missing_forest_data"
    assert missing.json()["message_key"] == "area.missing_forest_data"
    assert exact_missing.status_code == 409
    assert exact_missing.json() == {
        "code": "missing_forest_data",
        "message": "The selected square has no required forest data.",
        "message_key": "area.missing_forest_data",
        "details": {
            "square_id": missing_id,
            "target_date": "2026-10-12",
            "update_id": str(area_fixture["update"].public_id),
        },
    }
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "square_unknown"


@pytest.mark.django_db(transaction=True)
def test_area_detail_rejects_an_update_for_a_different_weather_cell(
    area_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    update = area_fixture["update"]
    snapshot = area_fixture["snapshot"]
    forest_cell = area_fixture["forest_cell"]
    terrain_cell = area_fixture["terrain_cell"]
    assert isinstance(update, EstimateUpdate)
    assert isinstance(snapshot, WeatherSnapshot)
    assert isinstance(forest_cell, ForestCell)
    distant_update = EstimateUpdate.objects.create(
        cell_key="45.00,12.00",
        assessment_date=update.assessment_date,
        calculated_at=update.calculated_at + timedelta(minutes=1),
        snapshot=snapshot,
        forest_build=forest_cell.build,
        terrain_build=terrain_cell.build,
        method_version=update.method_version,
        row_count=1,
        input_fingerprint=input_fingerprint(
            cell_key="45.00,12.00",
            assessment_date=update.assessment_date,
            snapshot_id=snapshot.id,
            forest_build_id=forest_cell.build.id,
            terrain_build_id=terrain_cell.build.id,
            method_version=update.method_version,
        ),
    )

    def fail_weather_data(*args: object, **kwargs: object) -> object:
        raise AssertionError("a mismatched update must not load weather")

    monkeypatch.setattr("area_api.router._weather_response", fail_weather_data)
    response = Client().get(
        f"/api/v1/areas/{area_fixture['square_id']}",
        {
            "target_date": "2026-10-12",
            "update_id": str(distant_update.public_id),
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "update_square_mismatch",
        "message": "The selected update belongs to a different weather cell.",
        "message_key": "area.update_square_mismatch",
        "details": {
            "square_id": area_fixture["square_id"],
            "update_id": str(distant_update.public_id),
            "expected_cell_key": update.cell_key,
            "update_cell_key": "45.00,12.00",
        },
    }


def test_api_openapi_marks_map_and_area_query_contracts() -> None:
    schema = api.get_openapi_schema()
    map_parameters = {
        parameter["name"]: parameter
        for parameter in schema["paths"]["/api/v1/squares"]["get"]["parameters"]
    }
    area_parameters = {
        parameter["name"]: parameter
        for parameter in schema["paths"]["/api/v1/areas/{square_id}"]["get"]["parameters"]
    }

    assert map_parameters["bbox"]["required"] is True
    assert map_parameters["target_date"]["required"] is True
    assert map_parameters["target_date"]["schema"]["format"] == "date"
    assert area_parameters["target_date"]["required"] is True
    assert area_parameters["target_date"]["schema"]["format"] == "date"
    assert area_parameters["update_id"]["required"] is False
    assert area_parameters["update_id"]["schema"]["format"] == "uuid"


@pytest.mark.django_db(transaction=True)
def test_area_detail_exposes_supported_frost_without_inventing_hail_or_drying(
    area_fixture: dict[str, object],
) -> None:
    update = _create_frost_update(area_fixture=area_fixture)
    square_id = area_fixture["square_id"]
    response = Client().get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-12", "update_id": str(update.public_id)},
    )

    assert response.status_code == 200
    body = response.json()
    selected = body["weather"]["selected_day"]
    assert selected["frost"] is True
    assert selected["et0_mm"] is None
    assert selected["mean_soil_temperature_c"] is None
    assert selected["mean_soil_moisture"] is None
    assert "hail" not in selected
    assert "drying" not in selected


def _other_update(*, update: EstimateUpdate, label: str) -> EstimateUpdate:
    """Store a second update for the same weather cell so that another run can hold it."""
    return EstimateUpdate.objects.create(
        cell_key=update.cell_key,
        assessment_date=update.assessment_date,
        calculated_at=update.calculated_at + timedelta(hours=1),
        snapshot=update.snapshot,
        forest_build=update.forest_build,
        terrain_build=update.terrain_build,
        method_version=f"area-other-{label}",
        row_count=1,
        input_fingerprint=sha256(f"area-other-{label}".encode()).hexdigest(),
    )


def _publish_run_for(
    *, update: EstimateUpdate, calculation_version: str = "area-calc-1", region_code: str = "09",
) -> ForecastRun:
    """Publish a run that holds the update and move the pointer to it."""
    run = ForecastRun.objects.create(
        region_code=region_code,
        assessment_date=update.assessment_date,
        status=ForecastRun.Status.PUBLISHED,
        forest_build=update.forest_build,
        terrain_build=update.terrain_build,
        method_version=update.method_version,
        calculation_version=calculation_version,
        target_dates=[
            (update.assessment_date + timedelta(days=offset)).isoformat() for offset in range(8)
        ],
        expected_cell_count=1,
        expected_square_count=1,
        published_at=update.calculated_at,
    )
    ForecastRunCell.objects.create(
        run=run,
        cell_key=update.cell_key,
        snapshot=update.snapshot,
        expected_square_count=1,
        state=ForecastRunCell.State.COMPLETED,
        update=update,
    )
    pointer = PublishedForecastRun.objects.filter(region_code=region_code).first()
    if pointer is None:
        PublishedForecastRun.objects.create(
            region_code=region_code, current_run=run, previous_run=None, published_at=update.calculated_at,
        )
    else:
        pointer.previous_run_id = pointer.current_run_id
        pointer.current_run = run
        pointer.save(update_fields=["previous_run", "current_run"])
    return run


@pytest.mark.django_db(transaction=True)
def test_area_detail_percentage_matches_real_map_square_for_same_update(
    area_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    update = area_fixture["update"]
    snapshot = area_fixture["snapshot"]
    square_id = area_fixture["square_id"]
    assert isinstance(update, EstimateUpdate)
    assert isinstance(snapshot, WeatherSnapshot)
    monkeypatch.setattr("map_api.api._today", lambda: date(2026, 10, 12))
    run = _publish_run_for(update=update)
    detail = Client().get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-12", "update_id": str(update.public_id)},
    )
    detail_body = detail.json()
    bounds = detail_body["selection"]["bounds"]

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=update.forest_build.version):
        map_response = Client().get(
            "/api/v1/squares",
            {
                "bbox": ",".join(
                    str(bounds[key]) for key in ("west", "south", "east", "north")
                ),
                "target_date": "2026-10-12",
            },
        )

    assert detail.status_code == 200
    assert map_response.status_code == 200
    feature = map_response.json()["features"][0]
    assert feature["id"] == square_id
    assert feature["properties"]["update_id"] == str(update.public_id)
    assert feature["properties"]["percentage"] == detail_body["estimate"]["whole_percentage"]
    assert map_response.json()["run_id"] == sha256(f"09:{run.id}".encode()).hexdigest()[:32]
    assert detail_body["selection"]["run_id"] == str(run.id)
    assert detail_body["selection"]["target_date"] == feature["properties"]["target_date"]


@pytest.mark.django_db(transaction=True)
def test_area_detail_names_the_current_or_previous_run_and_null_for_older_updates(
    area_fixture: dict[str, object],
) -> None:
    update = area_fixture["update"]
    query = {"target_date": "2026-10-11", "update_id": str(update.public_id)}
    url = f"/api/v1/areas/{area_fixture['square_id']}"

    unpublished = Client().get(url, query).json()["selection"]["run_id"]
    first = _publish_run_for(update=update, calculation_version="area-calc-1")
    as_current = Client().get(url, query).json()["selection"]["run_id"]
    _publish_run_for(update=_other_update(update=update, label="b"), calculation_version="area-calc-2")
    as_previous = Client().get(url, query).json()["selection"]["run_id"]
    _publish_run_for(update=_other_update(update=update, label="c"), calculation_version="area-calc-3")
    as_older = Client().get(url, query).json()["selection"]["run_id"]

    assert unpublished is None
    assert as_current == str(first.id)
    assert as_previous == str(first.id)
    assert as_older is None


@pytest.mark.django_db(transaction=True)
def test_area_detail_names_the_run_of_a_region_other_than_tuscany(
    area_fixture: dict[str, object],
) -> None:
    update = area_fixture["update"]
    query = {"target_date": "2026-10-11", "update_id": str(update.public_id)}
    run = _publish_run_for(update=update, region_code="12")

    selection = Client().get(f"/api/v1/areas/{area_fixture['square_id']}", query).json()["selection"]

    assert selection["run_id"] == str(run.id)


@pytest.mark.django_db(transaction=True)
def test_area_detail_repeated_open_is_served_from_the_shared_cache(
    area_fixture: dict[str, object],
) -> None:
    update = area_fixture["update"]
    query = {"target_date": "2026-10-11", "update_id": str(update.public_id)}
    url = f"/api/v1/areas/{area_fixture['square_id']}"
    client = Client()

    first = client.get(url, query)
    with CaptureQueriesContext(connection) as queries:
        second = client.get(url, query)
    caches["map"].clear()
    with CaptureQueriesContext(connection) as rebuilt:
        third = client.get(url, query)

    assert first.status_code == second.status_code == third.status_code == 200
    assert second.json() == first.json() == third.json()
    assert len(queries) < 5
    assert not any(EstimateResult._meta.db_table in query["sql"] for query in queries)
    assert len(rebuilt) > len(queries)


@pytest.mark.django_db(transaction=True)
def test_area_detail_rejects_invalid_inputs_unknown_square_and_expired_update(
    area_fixture: dict[str, object],
) -> None:
    square_id = area_fixture["square_id"]
    update = area_fixture["update"]
    client = Client()

    invalid_square = client.get(
        "/api/v1/areas/1000m_E4300000_N2300000",
        {"target_date": "2026-10-11", "update_id": str(update.public_id)},
    )
    invalid_update = client.get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-11", "update_id": "bad"},
    )
    unknown_square = client.get(
        "/api/v1/areas/500m_E4300500_N2300000",
        {"target_date": "2026-10-11", "update_id": str(update.public_id)},
    )
    expired = client.get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-11", "update_id": str(uuid.uuid4())},
    )

    assert invalid_square.status_code == 400
    assert invalid_square.json()["code"] == "invalid_square_id"
    assert invalid_update.status_code == 400
    assert invalid_update.json()["code"] == "invalid_update_id"
    assert unknown_square.status_code == 404
    assert unknown_square.json()["code"] == "square_unknown"
    assert expired.status_code == 410
    assert expired.json()["code"] == "update_expired"


@pytest.mark.django_db(transaction=True)
def test_area_detail_returns_distinct_no_result_and_date_errors(area_fixture: dict[str, object]) -> None:
    update = area_fixture["update"]
    square_id = area_fixture["square_id"]
    EstimateResult.objects.filter(update=update).delete()
    client = Client()
    no_result = client.get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-11", "update_id": str(update.public_id)},
    )
    invalid_date = client.get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-19", "update_id": str(update.public_id)},
    )

    assert no_result.status_code == 200
    assert no_result.json()["estimate"]["state"] == "noresultyet"
    assert no_result.json()["estimate"]["whole_percentage"] is None
    assert invalid_date.status_code == 400
    assert invalid_date.json()["code"] == "invalid_date"


@pytest.mark.django_db(transaction=True)
def test_area_detail_never_calls_weather_transport(area_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    update = area_fixture["update"]
    square_id = area_fixture["square_id"]

    def fail_transport(*args: object, **kwargs: object) -> None:
        raise AssertionError("area detail must not fetch weather")

    monkeypatch.setattr("weather_on_demand.services.request_weather", fail_transport)
    monkeypatch.setattr("weather_requests.provider.fetch_weather", fail_transport)
    monkeypatch.setattr("weather_requests.provider.WeatherProvider.fetch", fail_transport)
    response = Client().get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-11", "update_id": str(update.public_id)},
    )

    assert response.status_code == 200
    assert response.json()["estimate"]["whole_percentage"] == 52


@pytest.mark.django_db(transaction=True)
def test_area_detail_keeps_v1_update_on_v1_facts_and_serves_v2_facts_for_v2_update(
    area_fixture: dict[str, object],
) -> None:
    v1_update = area_fixture["update"]
    forest_cell = area_fixture["forest_cell"]
    square_id = area_fixture["square_id"]
    v2_version = "porcini-experimental-v2"
    v2_update = EstimateUpdate.objects.create(
        cell_key=v1_update.cell_key,
        assessment_date=v1_update.assessment_date,
        calculated_at=v1_update.calculated_at + timedelta(minutes=1),
        snapshot=v1_update.snapshot,
        forest_build=v1_update.forest_build,
        terrain_build=v1_update.terrain_build,
        method_version=v2_version,
        row_count=1,
        input_fingerprint=input_fingerprint(
            cell_key=v1_update.cell_key,
            assessment_date=v1_update.assessment_date,
            snapshot_id=v1_update.snapshot_id,
            forest_build_id=v1_update.forest_build_id,
            terrain_build_id=v1_update.terrain_build_id,
            method_version=v2_version,
        ),
    )
    EstimateResult.objects.create(
        update=v2_update,
        forest_cell=forest_cell,
        values=encode_result_bytes(
            percentages=[61, 62, 63, 64, 65, 66, 67, 68],
            unavailable_reasons=[None] * 8,
        ),
    )
    client = Client()

    v1_body = client.get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-12", "update_id": str(v1_update.public_id)},
    ).json()
    v2_body = client.get(
        f"/api/v1/areas/{square_id}",
        {"target_date": "2026-10-12", "update_id": str(v2_update.public_id)},
    ).json()

    assert v1_body["estimate"]["whole_percentage"] == 53
    assert v1_body["estimate"]["method_version"] == "porcini-experimental-v1"
    v1_facts = {fact["role_key"] for fact in v1_body["facts"]}
    assert "target_temperature_at_reference" in v1_facts
    assert "corrected_temperature" in v1_facts
    assert "frost_days" not in v1_facts
    assert v2_body["estimate"]["whole_percentage"] == 62
    assert v2_body["estimate"]["method_version"] == v2_version
    v2_facts = {fact["role_key"]: fact for fact in v2_body["facts"]}
    assert "mean_temperature_at_reference" in v2_facts
    assert "corrected_mean_temperature" in v2_facts
    assert "frost_days" in v2_facts
    assert "target_temperature_at_reference" not in v2_facts
    period = v2_facts["mean_temperature_at_reference"]
    assert period["period_start"] == "2026-09-23"
    assert period["period_end"] == "2026-10-12"
    assert period["completed_day_count"] == 18
    assert period["forecast_day_count"] == 2


@pytest.mark.django_db(transaction=True)
def test_area_tree_facts_use_saved_inputs_after_the_source_changes(area_fixture, monkeypatch) -> None:
    from estimate_updates.services import calculate_and_publish_estimates
    from sporameteo.forecast_method import METHOD_V3

    old = area_fixture["update"]
    cell = area_fixture["forest_cell"]
    square_id = area_fixture["square_id"]
    inputs = {
        "source": {"source_id": "jrc:first", "source_year": 2006, "resolution_m": 1000,
                   "metadata": {"credit": "JRC", "licence": "EC reuse notice"}},
        "cells": {str(cell.id): [0.8, 0.1, 0.2, 0.0, 0.0]},
    }
    monkeypatch.setattr("estimate_updates.services.tree_inputs_for_area", lambda **kwargs: inputs)
    kwargs = dict(method=METHOD_V3, snapshot_id=old.snapshot_id, cell_key=old.cell_key, assessment_date=old.assessment_date,
                  forest_build_version=old.forest_build.version, terrain_build_version=old.terrain_build.version)
    first = calculate_and_publish_estimates(**kwargs)
    assert first.created is True
    assert calculate_and_publish_estimates(**kwargs).created is False
    inputs = {**inputs, "cells": {str(cell.id): [0.0] * 5}}
    second = calculate_and_publish_estimates(**kwargs)
    assert second.created is True
    assert second.update.id != first.update.id

    def detail(update):
        response = Client().get(f"/api/v1/areas/{square_id}",
                                {"target_date": "2026-10-12", "update_id": str(update.public_id)})
        assert response.status_code == 200
        return response.json()

    original = detail(first.update)
    changed = detail(second.update)
    facts = {fact["role_key"]: fact for fact in original["facts"]}
    assert facts["host_beech"]["actual_value"] == 0.8
    assert facts["host_beech"]["source_id"] == "jrc:first"
    assert facts["host_beech"]["period_start"] == "2006-01-01"
    assert facts["host_beech"]["source_metadata"]["resolution_m"] == 1000
    assert facts["host_tree_factor"]["actual_value"] == 0.9
    assert original["estimate"]["whole_percentage"] > changed["estimate"]["whole_percentage"]
    assert detail(old)["estimate"]["whole_percentage"] == 53
    assert all(not fact["role_key"].startswith("host_") for fact in detail(old)["facts"])
