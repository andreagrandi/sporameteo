"""Build the database rows that the visitor journey reads.

The rows follow the shapes in tests/app/test_area_api.py and tests/app/test_map_api.py.
The block is a 5 by 5 group of 500 metre squares around Abetone, all forested.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from django.contrib.gis.geos import MultiPolygon, Polygon

from estimate_updates.contracts import encode_result_bytes
from estimate_updates.models import (
    EstimateResult,
    EstimateUpdate,
    ForecastRun,
    ForecastRunCell,
    PublishedForecastRun,
)
from estimate_updates.services import input_fingerprint
from map_api.published import WGS84_TO_GRID
from national_forest import layer
from national_forest.models import ForestBuild, ForestCell, ForestChunk
from national_grid.models import (
    GridBuild,
    GridBuildCell,
    GridCell,
    GridChunk,
    GridRegion,
    Municipality,
)
from national_terrain.models import (
    TerrainBuild,
    TerrainCell,
    TerrainChunk,
    TerrainChunkSource,
    TerrainSourceTile,
)
from sporameteo.finds.grid import CELL_SIZE_M, format_cell_id
from weather_summaries.models import (
    WeatherDaily,
    WeatherSnapshot,
    WeatherSnapshotDay,
    WeatherSummary,
)

ABETONE_LATITUDE = 44.14
ABETONE_LONGITUDE = 10.66
BLOCK_RADIUS = 2
WEATHER_CELL_KEY = "44.00,11.00"
METHOD_VERSION = "porcini-experimental-v1"


@dataclass(frozen=True)
class JourneyData:
    """Name the builds and the square that the journey expects."""

    forest_build_version: str
    terrain_build_version: str
    square_id: str
    assessment_date: date


def _abetone_corner() -> tuple[int, int]:
    """Return the lower-left corner of the 500 metre square that holds Abetone."""
    easting, northing = WGS84_TO_GRID.transform(ABETONE_LONGITUDE, ABETONE_LATITUDE)
    return (
        int(easting // CELL_SIZE_M) * CELL_SIZE_M,
        int(northing // CELL_SIZE_M) * CELL_SIZE_M,
    )


def _polygon(*, west: int, south: int, size: int) -> Polygon:
    polygon = Polygon.from_bbox((west, south, west + size, south + size))
    polygon.srid = 3035
    return polygon


def build_journey_data(*, layer_path: Path, assessment_date: date) -> JourneyData:
    """Create a published Tuscany run with full details for a block of squares."""
    suffix = uuid.uuid4().hex[:10]
    centre_e, centre_n = _abetone_corner()
    corners = [
        (centre_e + step_e * CELL_SIZE_M, centre_n + step_n * CELL_SIZE_M)
        for step_e in range(-BLOCK_RADIUS, BLOCK_RADIUS + 1)
        for step_n in range(-BLOCK_RADIUS, BLOCK_RADIUS + 1)
    ]
    west = centre_e - BLOCK_RADIUS * CELL_SIZE_M
    south = centre_n - BLOCK_RADIUS * CELL_SIZE_M
    block_size = (2 * BLOCK_RADIUS + 1) * CELL_SIZE_M
    # The region reaches past the block so that every square lies inside it.
    region_polygon = _polygon(west=west - 5_000, south=south - 5_000, size=block_size + 10_000)
    region_geometry = MultiPolygon(region_polygon, srid=3035)
    square_id = format_cell_id(corner_e=centre_e, corner_n=centre_n)

    grid = GridBuild.objects.create(
        version=f"journey-grid-{suffix}",
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
        cell_count=len(corners),
    )
    region = GridRegion.objects.create(
        build=grid,
        istat_code="09",
        name=f"Journey region {suffix}",
        geometry=region_geometry,
    )
    municipality = Municipality.objects.create(
        istat_code=f"j{suffix[:5]}",
        name="Abetone Cutigliano",
        region_code="09",
        geometry=region_geometry,
    )
    grid_chunk = GridChunk.objects.create(
        build=grid,
        west_e=west,
        south_n=south,
        east_e=west + block_size,
        north_n=south + block_size,
        status=GridChunk.Status.COMPLETED,
        cell_count=len(corners),
    )
    forest = ForestBuild.objects.create(
        version=f"journey-forest-{suffix}",
        grid_build=grid,
        fingerprint="b" * 64,
        status=ForestBuild.Status.COMPLETED,
        source_release="HRL Forest Type 2018",
        source_metadata={"catalogue": "forest-catalogue", "native_resolution_m": 10},
        licence="Fixture forest licence",
        credit="Fixture forest credit",
        catalogue_url="https://example.test/forest-catalogue",
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=len(corners),
        forest_cell_count=len(corners),
        weather_cell_count=1,
    )
    forest_chunk = ForestChunk.objects.create(
        build=forest,
        grid_chunk=grid_chunk,
        west_e=west,
        south_n=south,
        east_e=west + block_size,
        north_n=south + block_size,
        status=ForestChunk.Status.COMPLETED,
        cell_count=len(corners),
        source_sha256="d" * 64,
        source_retrieved_at="2026-10-10T10:00:00+00:00",
    )
    artifact = layer.write_forest_layer(
        rows=[
            {
                "corner_e": corner_e,
                "corner_n": corner_n,
                "forest_pixel_count": 100,
                "valid_pixel_count": 100,
                "is_forest": True,
            }
            for corner_e, corner_n in corners
        ],
        output_path=layer_path,
    )
    forest.layer_path = str(artifact.path)
    forest.layer_sha256 = artifact.sha256
    forest.layer_gzip_bytes = artifact.gzip_bytes
    forest.layer_raw_bytes = artifact.raw_bytes
    forest.save(update_fields=["layer_path", "layer_sha256", "layer_gzip_bytes", "layer_raw_bytes", "updated_at"])
    terrain = TerrainBuild.objects.create(
        version=f"journey-terrain-{suffix}",
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
        result_count=len(corners),
    )
    terrain_chunk = TerrainChunk.objects.create(
        build=terrain,
        grid_chunk=grid_chunk,
        status=TerrainChunk.Status.COMPLETED,
        result_count=len(corners),
    )
    terrain_tile = TerrainSourceTile.objects.create(
        build=terrain,
        tile_name="N44_00_E010_00",
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

    forest_cells: list[ForestCell] = []
    for corner_e, corner_n in corners:
        square = _polygon(west=corner_e, south=corner_n, size=CELL_SIZE_M)
        grid_cell = GridCell.objects.create(
            cell_id=format_cell_id(corner_e=corner_e, corner_n=corner_n),
            corner_e=corner_e,
            corner_n=corner_n,
            geometry=square,
            municipality=municipality,
        )
        membership = GridBuildCell.objects.create(
            build=grid,
            cell=grid_cell,
            chunk=grid_chunk,
            owner_region=region,
            land_geometry=MultiPolygon(square, srid=3035),
            land_share=1.0,
        )
        forest_cells.append(
            ForestCell.objects.create(
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
        )
        TerrainCell.objects.create(
            build=terrain,
            grid_build_cell=membership,
            chunk=terrain_chunk,
            altitude_m=1250.5,
            slope_deg=4.5,
            aspect_deg=90.0,
            altitude_valid_fraction=1.0,
            slope_valid_fraction=1.0,
            aspect_valid_fraction=1.0,
            altitude_quality="high",
            slope_quality="high",
            aspect_quality="high",
        )

    snapshot = _create_weather(assessment_date=assessment_date)
    update = EstimateUpdate.objects.create(
        cell_key=snapshot.cell_key,
        assessment_date=assessment_date,
        calculated_at=datetime.now(tz=UTC),
        snapshot=snapshot,
        forest_build=forest,
        terrain_build=terrain,
        method_version=METHOD_VERSION,
        row_count=len(forest_cells),
        input_fingerprint=input_fingerprint(
            cell_key=snapshot.cell_key,
            assessment_date=assessment_date,
            snapshot_id=snapshot.id,
            forest_build_id=forest.id,
            terrain_build_id=terrain.id,
            method_version=METHOD_VERSION,
        ),
        region_code="09",
    )
    values = encode_result_bytes(
        percentages=[52, 53, 54, 55, 56, 57, 58, 59],
        unavailable_reasons=[None] * 8,
    )
    EstimateResult.objects.bulk_create(
        [EstimateResult(update=update, forest_cell=cell, values=values) for cell in forest_cells]
    )
    run = ForecastRun.objects.create(
        region_code="09",
        assessment_date=assessment_date,
        status=ForecastRun.Status.PUBLISHED,
        forest_build=forest,
        terrain_build=terrain,
        method_version="journey-test",
        calculation_version="journey-calc-1",
        target_dates=[(assessment_date + timedelta(days=offset)).isoformat() for offset in range(8)],
        expected_cell_count=1,
        expected_square_count=len(forest_cells),
        published_at=datetime.now(tz=UTC),
    )
    ForecastRunCell.objects.create(
        run=run,
        cell_key=snapshot.cell_key,
        snapshot=snapshot,
        expected_square_count=len(forest_cells),
        state=ForecastRunCell.State.COMPLETED,
        update=update,
    )
    PublishedForecastRun.objects.create(
        region_code="09",
        current_run=run,
        previous_run=None,
        published_at=datetime.now(tz=UTC),
    )
    return JourneyData(
        forest_build_version=forest.version,
        terrain_build_version=terrain.version,
        square_id=square_id,
        assessment_date=assessment_date,
    )


def _create_weather(*, assessment_date: date) -> WeatherSnapshot:
    """Store the weather days and the eight summaries for the weather cell."""
    snapshot = WeatherSnapshot.objects.create(
        cell_key=WEATHER_CELL_KEY,
        assessment_date=assessment_date,
        reference_elevation_m=500.0,
        latest_retrieval=datetime.now(tz=UTC),
        source_ids=["weather:source"],
        source_attempt_ids=["weather:attempt"],
        metadata={"provider": "Open-Meteo"},
    )
    retrieved_at = datetime.now(tz=UTC)
    WeatherDaily.objects.bulk_create(
        [
            WeatherDaily(
                cell_key=snapshot.cell_key,
                date=assessment_date + timedelta(days=offset),
                source_provider="Open-Meteo",
                source_model="ecmwf_ifs04",
                source_endpoint="https://example.test/weather",
                source_id="weather:source",
                request_id="request-1",
                attempt_id=uuid.uuid4(),
                retrieved_at=retrieved_at,
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
            for offset in range(-21, 8)
        ]
    )
    rows = list(WeatherDaily.objects.filter(cell_key=snapshot.cell_key).order_by("date"))
    WeatherSnapshotDay.objects.bulk_create(
        [WeatherSnapshotDay(snapshot=snapshot, date=row.date, daily=row) for row in rows]
    )
    summaries: list[WeatherSummary] = []
    for days_ahead in range(8):
        target = assessment_date + timedelta(days=days_ahead)
        summaries.append(
            WeatherSummary(
                snapshot=snapshot,
                target_date=target,
                days_ahead=days_ahead,
                visitor_rain_7_mm=14.5,
                visitor_rain_14_mm=28.5,
                visitor_rain_21_mm=42.5,
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
    return snapshot
