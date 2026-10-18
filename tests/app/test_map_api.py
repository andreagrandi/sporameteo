"""Focused checks for map request validation and the published run states."""

import gzip
import json
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from math import isclose
from pathlib import Path
from uuid import uuid4

import pytest
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.cache import caches
from django.db import connection
from django.test import Client
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from shapely import transform as shapely_transform
from shapely.wkb import loads as load_wkb

from estimate_updates.models import (
    EstimateResult,
    EstimateUpdate,
    ForecastRun,
    ForecastRunCell,
    PublishedForecastRun,
)
from sporameteo.finds.grid import format_cell_id
from map_api.api import (
    MAX_VIEW_HEIGHT_M,
    MAX_VIEW_WIDTH_M,
    GRID_TO_WGS84,
    _coverage,
    _bbox_size_m,
    _parse_bbox,
    _parse_date,
)
from national_forest import layer
from national_forest.models import ForestBuild, ForestCell, ForestChunk
from national_grid.models import GridBuild, GridBuildCell, GridCell, GridChunk, GridRegion
from national_terrain.models import TerrainBuild
from weather_summaries.models import WeatherSnapshot


MAP_DATE = date(2026, 10, 12)


def map_id_of(*runs: ForecastRun) -> str:
    """Return the map identifier that the API derives from the current run of each region."""
    text = "|".join(f"{run.region_code}:{run.id}" for run in sorted(runs, key=lambda run: run.region_code))
    return sha256(text.encode()).hexdigest()[:32]


def publish_run(
    *,
    fixture: dict[str, object],
    updates: dict[str, EstimateUpdate],
    assessment_date: date = MAP_DATE,
    calculation_version: str = "map-calc-1",
    region_code: str = "09",
) -> ForecastRun:
    """Create a published run that holds the given updates and move the pointer to it."""
    run = ForecastRun.objects.create(
        region_code=region_code,
        assessment_date=assessment_date,
        status=ForecastRun.Status.PUBLISHED,
        forest_build=fixture["build"],
        terrain_build=fixture["terrain"],
        method_version="map-test",
        calculation_version=calculation_version,
        target_dates=[(assessment_date + timedelta(days=offset)).isoformat() for offset in range(8)],
        expected_cell_count=len(updates),
        expected_square_count=len(updates),
        published_at=datetime(2026, 10, 12, 12, tzinfo=UTC),
    )
    for cell_key, update in updates.items():
        ForecastRunCell.objects.create(
            run=run,
            cell_key=cell_key,
            snapshot=update.snapshot,
            expected_square_count=1,
            state=ForecastRunCell.State.COMPLETED,
            update=update,
        )
    pointer = PublishedForecastRun.objects.filter(region_code=region_code).first()
    if pointer is None:
        PublishedForecastRun.objects.create(
            region_code=region_code,
            current_run=run,
            previous_run=None,
            published_at=datetime(2026, 10, 12, 12, tzinfo=UTC),
        )
    else:
        pointer.previous_run_id = pointer.current_run_id
        pointer.current_run = run
        pointer.save(update_fields=["previous_run", "current_run"])
    return run


def add_update(
    *,
    fixture: dict[str, object],
    cell_key: str,
    forest_cell: ForestCell,
    values: bytes,
    label: str,
    assessment_date: date = MAP_DATE,
) -> EstimateUpdate:
    """Store one update with its snapshot and one result row."""
    snapshot, _ = WeatherSnapshot.objects.get_or_create(
        cell_key=cell_key,
        assessment_date=assessment_date,
        defaults={
            "latest_retrieval": datetime(2026, 10, 12, 10, tzinfo=UTC),
            "source_ids": [f"map:{label}"],
            "source_attempt_ids": [f"map:{label}-attempt"],
        },
    )
    update = EstimateUpdate.objects.create(
        cell_key=cell_key,
        assessment_date=assessment_date,
        calculated_at=datetime(2026, 10, 12, 9, 30, tzinfo=UTC),
        snapshot=snapshot,
        forest_build=fixture["build"],
        terrain_build=fixture["terrain"],
        method_version=f"map-test-{label}",
        row_count=1,
        input_fingerprint=sha256(f"{label}-{uuid4()}".encode()).hexdigest(),
        region_code="09",
    )
    EstimateResult.objects.create(update=update, forest_cell=forest_cell, values=values)
    return update


def _layer_metadata(*, path: Path, rows: list[dict[str, object]]) -> dict[str, object]:
    """Write one small deterministic forest artifact and return its metadata."""
    artifact = layer.write_forest_layer(rows=rows, output_path=path)
    return {
        "layer_path": str(artifact.path),
        "layer_sha256": artifact.sha256,
        "layer_gzip_bytes": artifact.gzip_bytes,
        "layer_raw_bytes": artifact.raw_bytes,
    }


@pytest.fixture
def map_fixture(tmp_path: Path) -> dict[str, object]:
    """Create a compact national-build fixture with stored map states."""
    caches["map"].clear()
    suffix = uuid4().hex[:10]
    west, south = 4_300_000, 2_300_000
    polygon = Polygon.from_bbox((west, south, west + 2_000, south + 500))
    polygon.srid = 3035
    multipolygon = MultiPolygon(polygon)
    multipolygon.srid = 3035
    grid = GridBuild.objects.create(
        version=f"map-grid-{suffix}",
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
        cell_count=5,
    )
    region = GridRegion.objects.create(
        build=grid,
        istat_code="09",
        name=f"Map region {suffix}",
        geometry=multipolygon,
    )
    grid_chunk = GridChunk.objects.create(
        build=grid,
        west_e=west,
        south_n=south,
        east_e=west + 2_500,
        north_n=south + 500,
        status=GridChunk.Status.COMPLETED,
        cell_count=4,
    )
    memberships: list[GridBuildCell] = []
    for index in range(5):
        corner_e = west + index * 500
        cell_polygon = Polygon.from_bbox((corner_e, south, corner_e + 500, south + 500))
        cell_polygon.srid = 3035
        grid_cell = GridCell.objects.create(
            cell_id=format_cell_id(corner_e=corner_e, corner_n=south),
            corner_e=corner_e,
            corner_n=south,
            geometry=cell_polygon,
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
        version=f"map-forest-{suffix}",
        grid_build=grid,
        fingerprint="b" * 64,
        status=ForestBuild.Status.COMPLETED,
        licence="Fixture forest licence",
        credit="Fixture forest credit",
        catalogue_url="https://example.test/forest-catalogue",
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=5,
        forest_cell_count=2,
        weather_cell_count=2,
    )
    forest_chunk = ForestChunk.objects.create(
        build=forest,
        grid_chunk=grid_chunk,
        west_e=west,
        south_n=south,
        east_e=west + 2_500,
        north_n=south + 500,
        status=ForestChunk.Status.COMPLETED,
        cell_count=5,
    )
    forest_cells = [
        ForestCell.objects.create(
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
        ),
        ForestCell.objects.create(
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
        ),
        ForestCell.objects.create(
            build=forest,
            grid_build_cell=memberships[2],
            chunk=forest_chunk,
            total_pixel_count=100,
            valid_pixel_count=0,
            forest_pixel_count=0,
            broadleaf_pixel_count=0,
            valid_coverage=0.0,
            coverage_quality="unavailable",
            forest_share=None,
            leaf_type=None,
            is_forest=None,
            missing_reason="source_gap",
        ),
        ForestCell.objects.create(
            build=forest,
            grid_build_cell=memberships[3],
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
            weather_latitude=44.1,
            weather_longitude=11.1,
        ),
        ForestCell.objects.create(
            build=forest,
            grid_build_cell=memberships[4],
            chunk=forest_chunk,
            total_pixel_count=100,
            valid_pixel_count=0,
            forest_pixel_count=0,
            broadleaf_pixel_count=0,
            valid_coverage=0.0,
            coverage_quality="unavailable",
            forest_share=None,
            leaf_type=None,
            is_forest=None,
            missing_reason="optional_missing",
        ),
    ]
    rows = [
        {
            "corner_e": west + index * 500,
            "corner_n": south,
            "forest_pixel_count": 100 if index in {0, 3} else 0,
            "valid_pixel_count": 100,
            "is_forest": index != 2,
        }
        for index in range(5)
    ]
    forest_data = _layer_metadata(path=tmp_path / "forest-layer.gz", rows=rows)
    for field, value in forest_data.items():
        setattr(forest, field, value)
    forest.save(update_fields=[*forest_data, "updated_at"])
    terrain = TerrainBuild.objects.create(
        version=f"map-terrain-{suffix}",
        grid_build=grid,
        fingerprint="c" * 64,
        grid_fingerprint=grid.fingerprint,
        source_metadata={
            "release": "Fixture Copernicus terrain",
            "source_url": "https://example.test/terrain-source",
            "source_dataset": "https://example.test/terrain-dataset",
            "credit": "Fixture terrain licence",
        },
        status=TerrainBuild.Status.COMPLETED,
    )
    snapshot = WeatherSnapshot.objects.create(
        cell_key="44.00,11.00",
        assessment_date=MAP_DATE,
        latest_retrieval=datetime(2026, 10, 12, 10, tzinfo=UTC),
        source_ids=["map:test"],
        source_attempt_ids=["map:attempt"],
    )
    old_update = EstimateUpdate.objects.create(
        cell_key=snapshot.cell_key,
        assessment_date=MAP_DATE,
        calculated_at=datetime(2026, 10, 12, 8, tzinfo=UTC),
        snapshot=snapshot,
        forest_build=forest,
        terrain_build=terrain,
        method_version="map-test-old",
        row_count=1,
        input_fingerprint=sha256(f"old-{suffix}".encode()).hexdigest(),
    )
    new_update = EstimateUpdate.objects.create(
        cell_key=snapshot.cell_key,
        assessment_date=MAP_DATE,
        calculated_at=datetime(2026, 10, 12, 9, tzinfo=UTC),
        snapshot=snapshot,
        forest_build=forest,
        terrain_build=terrain,
        method_version="map-test-new",
        row_count=1,
        input_fingerprint=sha256(f"new-{suffix}".encode()).hexdigest(),
    )
    EstimateResult.objects.create(update=old_update, forest_cell=forest_cells[0], values=bytes([11] * 8))
    EstimateResult.objects.create(
        update=new_update,
        forest_cell=forest_cells[0],
        values=bytes([0, 42, 42, 42, 42, 42, 42, 42]),
    )
    return {
        "build": forest,
        "terrain": terrain,
        "cells": forest_cells,
        "snapshot": snapshot,
        "old_update": old_update,
        "new_update": new_update,
        "bbox": "9.73,43.80,9.78,43.83",
    }


def test_bbox_parser_requires_four_finite_wgs84_values() -> None:
    parsed, error = _parse_bbox(value="10,44,10.1,44.1")

    assert error is None
    assert parsed == (10.0, 44.0, 10.1, 44.1)


@pytest.mark.parametrize(
    "value",
    [
        "10,44,10",
        "nan,44,10.1,44.1",
        "10,44,181,44.1",
        "10.1,44,10,44.1",
        "10,45,10.1,44",
    ],
)
def test_bbox_parser_rejects_malformed_nonfinite_out_of_range_and_reversed_values(
    value: str,
) -> None:
    parsed, error = _parse_bbox(value=value)

    assert parsed is None
    assert error is not None
    assert error.status_code == 400
    assert json.loads(error.content)["code"] == "invalid_bbox"


def test_date_parser_requires_exact_iso_date() -> None:
    parsed, error = _parse_date(value="2026-10-12")

    assert error is None
    assert parsed == date(2026, 10, 12)

    parsed, error = _parse_date(value="2026-1-2")
    assert parsed is None
    assert error is not None
    assert json.loads(error.content)["code"] == "invalid_date"


def test_bbox_size_uses_wgs84_geodesic_distance() -> None:
    width_m, height_m = _bbox_size_m(bbox=(10.0, 44.0, 10.5, 44.5))

    assert width_m > 0
    assert height_m > 0
    assert width_m < MAX_VIEW_WIDTH_M
    assert height_m < MAX_VIEW_HEIGHT_M


@pytest.mark.django_db(transaction=True)
def test_coverage_uses_the_transformed_extent_of_stored_cell_geometry() -> None:
    suffix = uuid4().hex[:10]
    west, south = 4_300_000, 2_300_000
    first_geometry = Polygon.from_bbox((west, south, west + 500, south + 500))
    second_geometry = Polygon.from_bbox((west + 20_000, south + 20_000, west + 20_500, south + 20_500))
    first_geometry.srid = 3035
    second_geometry.srid = 3035
    region_geometry = MultiPolygon(first_geometry, second_geometry, srid=3035)
    grid = GridBuild.objects.create(
        version=f"coverage-grid-{suffix}",
        fingerprint="a" * 64,
        status=GridBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=2,
    )
    region = GridRegion.objects.create(
        build=grid,
        istat_code=f"C{suffix}",
        name="Coverage test region",
        geometry=region_geometry,
    )
    chunk = GridChunk.objects.create(
        build=grid,
        west_e=west,
        south_n=south,
        east_e=west + 20_500,
        north_n=south + 20_500,
        status=GridChunk.Status.COMPLETED,
        cell_count=2,
    )
    for geometry in (first_geometry, second_geometry):
        cell = GridCell.objects.create(
            cell_id=format_cell_id(
                corner_e=int(geometry.extent[0]),
                corner_n=int(geometry.extent[1]),
            ),
            corner_e=int(geometry.extent[0]),
            corner_n=int(geometry.extent[1]),
            geometry=geometry,
        )
        GridBuildCell.objects.create(
            build=grid,
            cell=cell,
            chunk=chunk,
            owner_region=region,
            land_geometry=MultiPolygon(geometry, srid=3035),
            land_share=1.0,
        )
    forest = ForestBuild.objects.create(
        version=f"coverage-forest-{suffix}",
        grid_build=grid,
        fingerprint="b" * 64,
        status=ForestBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=2,
    )

    actual = _coverage(build=forest, region_codes=[region.istat_code])
    expected = [float("inf"), float("inf"), float("-inf"), float("-inf")]
    for geometry in (first_geometry, second_geometry):
        transformed = shapely_transform(
            load_wkb(bytes(geometry.wkb)),
            GRID_TO_WGS84.transform,
            interleaved=False,
        )
        geometry_bounds = transformed.bounds
        expected[0] = min(expected[0], geometry_bounds[0])
        expected[1] = min(expected[1], geometry_bounds[1])
        expected[2] = max(expected[2], geometry_bounds[2])
        expected[3] = max(expected[3], geometry_bounds[3])

    assert actual is not None
    assert all(
        isclose(actual_value, expected_value, rel_tol=0.0, abs_tol=1e-7)
        for actual_value, expected_value in zip(
            (actual.west, actual.south, actual.east, actual.north),
            expected,
            strict=True,
        )
    )


@pytest.mark.django_db
def test_registered_squares_route_validates_before_database_reads() -> None:
    response = Client().get(
        "/api/v1/squares",
        headers={"Host": "127.0.0.1"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "missing_parameter"


def test_broad_view_returns_zoom_in_error_without_any_database_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called() -> None:
        raise AssertionError("broad views must not read the published run")

    monkeypatch.setattr("map_api.published.published_map", fail_if_called)
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE)
    response = Client().get(
        "/api/v1/squares?bbox=10,44,11,44.5&target_date=2026-10-12",
        headers={"Host": "127.0.0.1"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "view_too_large"


@pytest.mark.django_db(transaction=True)
def test_info_and_forest_layer_routes_expose_contract_and_revalidate(
    map_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = map_fixture["build"]
    terrain = map_fixture["terrain"]
    assert isinstance(build, ForestBuild)
    assert isinstance(terrain, TerrainBuild)
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE)

    with override_settings(
        SPORAMETEO_FOREST_BUILD_VERSION=build.version,
        SPORAMETEO_TERRAIN_BUILD_VERSION=terrain.version,
    ):
        client = Client()
        info_response = client.get("/api/v1/info")
        assert info_response.status_code == 200
        info_body = info_response.json()
        assert info_body["today"] == MAP_DATE.isoformat()
        assert info_body["target_dates"] == [
            (MAP_DATE + timedelta(days=offset)).isoformat()
            for offset in range(8)
        ]
        assert "assessment_dates" not in info_body
        assert info_body["max_width_km"] == 60
        assert info_body["max_height_km"] == 60
        assert info_body["max_squares"] == 16_000
        assert info_body["forest_build_version"] == build.version
        assert info_body["attributions"] == [
            {
                "name": "ISTAT boundaries",
                "credit": "Fixture ISTAT credit",
                "licence": "Fixture ISTAT licence",
                "url": "https://example.test/istat-license",
            },
            {
                "name": "Copernicus forest",
                "credit": "Fixture forest credit",
                "licence": "Fixture forest licence",
                "url": "https://example.test/forest-catalogue",
            },
            {
                "name": "Copernicus terrain",
                "credit": "Fixture terrain licence",
                "licence": "Fixture terrain licence",
                "url": "https://example.test/terrain-dataset",
            },
            {
                "name": "Open-Meteo",
                "credit": "Weather data by Open-Meteo.com",
                "licence": "CC BY 4.0",
                "url": "https://open-meteo.com/",
            },
        ]

        plain_response = client.get("/api/v1/forest-layer")
        assert plain_response.status_code == 200
        assert plain_response["Cache-Control"] == "no-cache"
        assert plain_response["Vary"] == "Accept-Encoding"
        assert "Content-Encoding" not in plain_response
        plain_body = json.loads(plain_response.content)
        assert plain_body["type"] == "FeatureCollection"

        gzip_response = client.get(
            "/api/v1/forest-layer",
            HTTP_ACCEPT_ENCODING="gzip",
        )
        assert gzip_response.status_code == 200
        assert gzip_response["ETag"] == plain_response["ETag"]
        assert gzip_response["Content-Encoding"] == "gzip"
        assert gzip.decompress(gzip_response.content) == plain_response.content

        conditional_response = client.get(
            "/api/v1/forest-layer",
            HTTP_IF_NONE_MATCH=plain_response["ETag"],
        )
        assert conditional_response.status_code == 304
        assert conditional_response["ETag"] == plain_response["ETag"]
        assert conditional_response["Cache-Control"] == "no-cache"


@pytest.mark.django_db(transaction=True)
def test_info_target_dates_follow_an_explicit_clock_advance(
    map_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    build = map_fixture["build"]
    first_day = date(2026, 10, 12)
    next_day = first_day + timedelta(days=1)
    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=build.version):
        monkeypatch.setattr("map_api.api._today", lambda: first_day)
        first_response = Client().get("/api/v1/info")
        monkeypatch.setattr("map_api.api._today", lambda: next_day)
        next_response = Client().get("/api/v1/info")

    assert first_response.status_code == 200
    assert next_response.status_code == 200
    assert first_response.json()["today"] == first_day.isoformat()
    assert next_response.json()["today"] == next_day.isoformat()
    assert first_response.json()["target_dates"][0] == first_day.isoformat()
    assert next_response.json()["target_dates"][0] == next_day.isoformat()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("terrain_case", ["unfinished", "wrong_grid"])
def test_info_omits_unavailable_configured_terrain_attribution(
    map_fixture: dict[str, object], terrain_case: str
) -> None:
    build = map_fixture["build"]
    terrain = map_fixture["terrain"]
    assert isinstance(build, ForestBuild)
    assert isinstance(terrain, TerrainBuild)
    if terrain_case == "unfinished":
        terrain.status = TerrainBuild.Status.RUNNING
        terrain.save(update_fields=["status", "updated_at"])
    else:
        other_grid = GridBuild.objects.create(
            version=f"other-map-grid-{uuid4().hex[:10]}",
            fingerprint="e" * 64,
            status=GridBuild.Status.COMPLETED,
        )
        terrain.grid_build = other_grid
        terrain.save(update_fields=["grid_build", "updated_at"])

    with override_settings(
        SPORAMETEO_FOREST_BUILD_VERSION=build.version,
        SPORAMETEO_TERRAIN_BUILD_VERSION=terrain.version,
    ):
        response = Client().get("/api/v1/info")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()["attributions"]] == [
        "ISTAT boundaries",
        "Copernicus forest",
        "Open-Meteo",
    ]


@pytest.mark.django_db(transaction=True)
def test_forest_layer_etag_changes_when_configured_completed_build_changes(
    map_fixture: dict[str, object],
    tmp_path: Path,
) -> None:
    build = map_fixture["build"]
    second_build = ForestBuild.objects.create(
        version=f"{build.version}-next",
        grid_build=build.grid_build,
        fingerprint="d" * 64,
        status=ForestBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=5,
        forest_cell_count=1,
        weather_cell_count=1,
    )
    second_data = _layer_metadata(
        path=tmp_path / "second-forest-layer.gz",
        rows=[
            {
                "corner_e": 4_300_000,
                "corner_n": 2_300_000,
                "forest_pixel_count": 75,
                "valid_pixel_count": 100,
                "is_forest": True,
            }
        ],
    )
    for field, value in second_data.items():
        setattr(second_build, field, value)
    second_build.save(update_fields=[*second_data, "updated_at"])

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=build.version):
        first_response = Client().get("/api/v1/forest-layer")
    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=second_build.version):
        second_response = Client().get("/api/v1/forest-layer")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response["ETag"] != second_response["ETag"]
    assert first_response["Cache-Control"] == "no-cache"
    assert second_response["Cache-Control"] == "no-cache"
    assert first_response.content != second_response.content


@pytest.mark.django_db(transaction=True)
def test_forest_layer_returns_stable_error_for_a_hash_mismatch(
    map_fixture: dict[str, object],
) -> None:
    build = map_fixture["build"]
    build.layer_sha256 = "0" * 64
    build.save(update_fields=["layer_sha256", "updated_at"])

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=build.version):
        response = Client().get("/api/v1/forest-layer")

    assert response.status_code == 503
    assert response.json()["code"] == "forest_layer_unavailable"


@pytest.mark.django_db(transaction=True)
def test_squares_route_reads_the_published_run_and_keeps_all_square_states(
    map_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = map_fixture["build"]
    cells = map_fixture["cells"]
    new_update = map_fixture["new_update"]
    run = publish_run(fixture=map_fixture, updates={"44.00,11.00": new_update})
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE)
    url = f"/api/v1/squares?bbox={map_fixture['bbox']}&target_date={MAP_DATE.isoformat()}"

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=build.version):
        client = Client()
        response = client.get(url)
        with CaptureQueriesContext(connection) as cached_queries:
            repeated = client.get(url)

    assert response.status_code == 200
    assert repeated.content == response.content
    assert response["ETag"] == repeated["ETag"]
    assert response["Cache-Control"] == "no-cache"
    assert not any(
        EstimateResult._meta.db_table in query["sql"]
        or ForestCell._meta.db_table in query["sql"]
        for query in cached_queries
    )
    body = response.json()
    assert body["run_id"] == map_id_of(run)
    assert body["assessment_date"] == MAP_DATE.isoformat()
    assert body["target_date"] == MAP_DATE.isoformat()
    ids = [feature["id"] for feature in body["features"]]
    assert body["square_count"] == len(ids) == 5
    assert ids == sorted(ids)
    assert all(feature["geometry"]["type"] == "Polygon" for feature in body["features"])
    by_cell_id = {feature["id"]: feature["properties"] for feature in body["features"]}

    available = by_cell_id[cells[0].grid_build_cell.cell.cell_id]
    assert available["state"] == "available"
    assert available["percentage"] == 0
    assert available["unavailable_reason"] is None
    assert available["target_date"] == MAP_DATE.isoformat()
    assert available["assessment_date"] == MAP_DATE.isoformat()
    assert available["update_id"] == str(new_update.public_id)
    assert available["calculated_at"].startswith("2026-10-12T09:00:00")
    assert available["weather_cell"] == {"latitude": 44.0, "longitude": 11.0}

    no_forest = by_cell_id[cells[1].grid_build_cell.cell.cell_id]
    assert no_forest["state"] == "noforest"
    assert no_forest["percentage"] is None
    assert no_forest["update_id"] is None
    assert no_forest["weather_cell"] is None
    for index in (2, 4):
        missing = by_cell_id[cells[index].grid_build_cell.cell.cell_id]
        assert missing["unavailable_reason"] == "missing_forest_data"
        assert missing["update_id"] is None

    outside = by_cell_id[cells[3].grid_build_cell.cell.cell_id]
    assert outside["state"] == "unavailable"
    assert outside["reason"] == "outside_prepared_area"
    assert outside["unavailable_reason"] == "outside_prepared_area"
    assert outside["message_key"] == "estimate.outside_prepared_area"
    assert outside["update_id"] is None
    assert outside["assessment_date"] is None
    assert outside["weather_cell"] == {"latitude": 44.1, "longitude": 11.1}


@pytest.mark.django_db(transaction=True)
def test_squares_and_overview_return_a_clear_unavailable_error_without_a_published_run(
    map_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE)
    query = {"bbox": map_fixture["bbox"], "target_date": MAP_DATE.isoformat()}

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=map_fixture["build"].version):
        client = Client()
        squares = client.get("/api/v1/squares", query)
        overview = client.get("/api/v1/estimate-overview", query)
        info = client.get("/api/v1/info")

    for response in (squares, overview):
        assert response.status_code == 503
        assert response.json()["code"] == "forecast_unavailable"
        assert response.json()["message_key"] == "map.forecast_unavailable"
    assert info.status_code == 200
    assert info.json()["run"] is None


@pytest.mark.django_db(transaction=True)
def test_squares_route_rejects_dates_the_published_run_does_not_hold(
    map_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_run(fixture=map_fixture, updates={"44.00,11.00": map_fixture["new_update"]})
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE + timedelta(days=2))

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=map_fixture["build"].version):
        client = Client()
        responses = {
            offset: client.get(
                "/api/v1/squares",
                {
                    "bbox": map_fixture["bbox"],
                    "target_date": (MAP_DATE + timedelta(days=offset)).isoformat(),
                },
            )
            for offset in (1, 2, 7, 8)
        }

    assert responses[1].status_code == 400
    assert responses[1].json()["code"] == "unsupported_date"
    assert responses[2].status_code == 200
    assert responses[7].status_code == 200
    assert responses[8].status_code == 400
    assert responses[8].json()["details"]["available_dates"][0] == (
        MAP_DATE + timedelta(days=2)
    ).isoformat()


@pytest.mark.django_db(transaction=True)
def test_squares_route_rejects_a_view_outside_the_tuscany_extent(
    map_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_run(fixture=map_fixture, updates={"44.00,11.00": map_fixture["new_update"]})
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE)

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=map_fixture["build"].version):
        with CaptureQueriesContext(connection) as queries:
            response = Client().get(
                f"/api/v1/squares?bbox=0,0,0.01,0.01&target_date={MAP_DATE.isoformat()}"
            )

    assert response.status_code == 400
    assert response.json()["code"] == "outside_coverage"
    assert response.json()["message"] == "The bbox is outside the regions with published estimates."
    assert not any(
        ForestCell._meta.db_table in query["sql"]
        or EstimateResult._meta.db_table in query["sql"]
        for query in queries
    )


@pytest.mark.django_db(transaction=True)
def test_overview_averages_run_scores_and_marks_a_mixed_group_unavailable(
    map_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE)
    build = map_fixture["build"]
    first = map_fixture["new_update"]
    run = publish_run(fixture=map_fixture, updates={"44.00,11.00": first})
    client = Client()
    query = {"bbox": "9,43,12,45", "target_date": MAP_DATE.isoformat()}

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=build.version):
        only_first = client.get("/api/v1/estimate-overview", query)
        assert only_first.status_code == 200
        assert only_first.json()["run_id"] == map_id_of(run)
        assert only_first.json()["assessment_date"] == MAP_DATE.isoformat()
        # The forest square without a result row is not part of the run, so it is left out.
        feature = only_first.json()["features"][0]["properties"]
        assert feature["state"] == "available"
        assert feature["percentage"] == 0
        assert feature["forest_square_count"] == 1

        second = add_update(
            fixture=map_fixture,
            cell_key="44.10,11.10",
            forest_cell=map_fixture["cells"][3],
            values=bytes([139] + [100] * 7),
            label="overview-second",
        )
        new_run = publish_run(
            fixture=map_fixture,
            updates={"44.00,11.00": first, "44.10,11.10": second},
            calculation_version="map-calc-2",
        )
        mixed = client.get("/api/v1/estimate-overview", query).json()
        assert mixed["run_id"] == map_id_of(new_run)
        feature = mixed["features"][0]["properties"]
        assert feature["state"] == "unavailable"
        assert feature["percentage"] is None
        assert feature["forest_square_count"] == 2
        assert feature["scored_square_count"] == 1
        assert feature["assessment_date"] == MAP_DATE.isoformat()
        query["target_date"] = (MAP_DATE + timedelta(days=1)).isoformat()
        tomorrow = client.get("/api/v1/estimate-overview", query).json()
        assert tomorrow["features"][0]["properties"]["percentage"] == 71
        assert tomorrow["features"][0]["properties"]["overview"] is True
        assert tomorrow["features"][0]["properties"]["calculated_at"].startswith("2026-10-12T09:")
        query["target_date"] = (MAP_DATE + timedelta(days=8)).isoformat()
        assert client.get("/api/v1/estimate-overview", query).status_code == 400


@pytest.mark.django_db(transaction=True)
def test_info_describes_the_published_run_and_the_tuscany_extent(
    map_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = publish_run(fixture=map_fixture, updates={"44.00,11.00": map_fixture["new_update"]})
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE + timedelta(days=3))

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=map_fixture["build"].version):
        body = Client().get("/api/v1/info").json()

    assert body["today"] == (MAP_DATE + timedelta(days=3)).isoformat()
    assert body["run"]["run_id"] == map_id_of(run)
    assert body["run"]["assessment_date"] == MAP_DATE.isoformat()
    assert body["run"]["published_at"].startswith("2026-10-12T12:00:00")
    assert body["run"]["target_dates"] == [
        (MAP_DATE + timedelta(days=offset)).isoformat() for offset in range(8)
    ]
    assert body["target_dates"] == [
        (MAP_DATE + timedelta(days=offset)).isoformat() for offset in range(3, 8)
    ]
    region = GridRegion.objects.get(istat_code="09", build=map_fixture["build"].grid_build)
    expected = shapely_transform(
        load_wkb(bytes(region.geometry.wkb)), GRID_TO_WGS84.transform, interleaved=False,
    ).bounds
    coverage = body["coverage"]
    assert all(
        isclose(coverage[name], value, abs_tol=1e-7)
        for name, value in zip(("west", "south", "east", "north"), expected, strict=True)
    )


@pytest.mark.django_db
@pytest.mark.parametrize("relative", [True, False])
def test_forest_layer_route_reads_relative_and_absolute_stored_paths(
    map_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    relative: bool,
) -> None:
    build = map_fixture["build"]
    terrain = map_fixture["terrain"]
    assert isinstance(build, ForestBuild)
    assert isinstance(terrain, TerrainBuild)
    stored = Path(build.layer_path)
    monkeypatch.setattr(layer, "REPOSITORY_ROOT", stored.parent)
    ForestBuild.objects.filter(id=build.id).update(layer_path=stored.name if relative else str(stored))

    with override_settings(
        SPORAMETEO_FOREST_BUILD_VERSION=build.version,
        SPORAMETEO_TERRAIN_BUILD_VERSION=terrain.version,
    ):
        response = Client().get("/api/v1/forest-layer")

    assert response.status_code == 200


def add_second_region(*, fixture: dict[str, object]) -> None:
    """Give the last two squares to region 12 and give square 3 the weather cell of square 0."""
    cells = fixture["cells"]
    geometry = Polygon.from_bbox((4_301_500, 2_300_000, 4_302_500, 2_300_500))
    geometry.srid = 3035
    region = GridRegion.objects.create(
        build=fixture["build"].grid_build,
        istat_code="12",
        name="Second map region",
        geometry=MultiPolygon(geometry, srid=3035),
    )
    GridBuildCell.objects.filter(
        id__in=[cells[3].grid_build_cell_id, cells[4].grid_build_cell_id],
    ).update(owner_region=region)
    ForestCell.objects.filter(id=cells[3].id).update(weather_latitude=44.0, weather_longitude=11.0)


@pytest.mark.django_db(transaction=True)
def test_a_view_across_two_regions_gives_each_square_the_run_of_its_own_region(
    map_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_second_region(fixture=map_fixture)
    cells = map_fixture["cells"]
    first_update = map_fixture["new_update"]
    second_update = add_update(
        fixture=map_fixture,
        cell_key="44.00,11.00",
        forest_cell=cells[3],
        values=bytes([60] * 8),
        label="second-region",
        assessment_date=MAP_DATE + timedelta(days=1),
    )
    first_run = publish_run(fixture=map_fixture, updates={"44.00,11.00": first_update})
    second_run = publish_run(
        fixture=map_fixture,
        updates={"44.00,11.00": second_update},
        assessment_date=MAP_DATE + timedelta(days=1),
        region_code="12",
    )
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE + timedelta(days=1))
    first_id = cells[0].grid_build_cell.cell.cell_id
    second_id = cells[3].grid_build_cell.cell.cell_id

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=map_fixture["build"].version):
        client = Client()
        shared = client.get(
            "/api/v1/squares",
            {"bbox": map_fixture["bbox"], "target_date": (MAP_DATE + timedelta(days=1)).isoformat()},
        ).json()
        last_day = client.get(
            "/api/v1/squares",
            {"bbox": map_fixture["bbox"], "target_date": (MAP_DATE + timedelta(days=8)).isoformat()},
        ).json()

    assert shared["run_id"] == map_id_of(first_run, second_run)
    assert shared["assessment_date"] == (MAP_DATE + timedelta(days=1)).isoformat()
    assert shared["square_count"] == 5
    by_cell_id = {feature["id"]: feature["properties"] for feature in shared["features"]}
    assert by_cell_id[first_id]["percentage"] == 42
    assert by_cell_id[first_id]["assessment_date"] == MAP_DATE.isoformat()
    assert by_cell_id[first_id]["update_id"] == str(first_update.public_id)
    assert by_cell_id[second_id]["percentage"] == 60
    assert by_cell_id[second_id]["assessment_date"] == (MAP_DATE + timedelta(days=1)).isoformat()
    assert by_cell_id[second_id]["update_id"] == str(second_update.public_id)
    # The first run holds only 8 days from its own date, so it has nothing for day 8.
    by_cell_id = {feature["id"]: feature["properties"] for feature in last_day["features"]}
    assert by_cell_id[first_id]["percentage"] is None
    assert by_cell_id[first_id]["reason"] == "requiredgap"
    assert by_cell_id[second_id]["percentage"] == 60


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("on_another_build", [False, True])
def test_a_square_of_a_region_without_a_usable_run_has_no_percentage(
    map_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch, on_another_build: bool,
) -> None:
    add_second_region(fixture=map_fixture)
    cells = map_fixture["cells"]
    update = map_fixture["new_update"]
    # The square has a stored result, so only its unpublished region can hide it.
    EstimateResult.objects.create(update=update, forest_cell=cells[3], values=bytes([60] * 8))
    publish_run(fixture=map_fixture, updates={"44.00,11.00": update})
    if on_another_build:
        other_build = ForestBuild.objects.create(
            version=f"other-forest-{uuid4().hex[:10]}",
            grid_build=map_fixture["build"].grid_build,
            fingerprint="d" * 64,
            status=ForestBuild.Status.COMPLETED,
            planned_chunk_count=1,
            completed_chunk_count=1,
            cell_count=5,
        )
        publish_run(
            fixture={**map_fixture, "build": other_build},
            updates={"44.00,11.00": update},
            region_code="12",
        )
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE)

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=map_fixture["build"].version):
        body = Client().get(
            "/api/v1/squares",
            {"bbox": map_fixture["bbox"], "target_date": MAP_DATE.isoformat()},
        ).json()

    by_cell_id = {feature["id"]: feature["properties"] for feature in body["features"]}
    assert by_cell_id[cells[0].grid_build_cell.cell.cell_id]["percentage"] == 0
    unpublished = by_cell_id[cells[3].grid_build_cell.cell.cell_id]
    assert unpublished["percentage"] is None
    assert unpublished["reason"] == "outside_prepared_area"
    assert unpublished["update_id"] is None


@pytest.mark.django_db(transaction=True)
def test_info_lists_the_run_of_each_region_and_the_map_id_changes_when_one_publishes(
    map_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_second_region(fixture=map_fixture)
    cells = map_fixture["cells"]
    second_update = add_update(
        fixture=map_fixture,
        cell_key="44.00,11.00",
        forest_cell=cells[3],
        values=bytes([60] * 8),
        label="info-second",
        assessment_date=MAP_DATE + timedelta(days=1),
    )
    first_run = publish_run(fixture=map_fixture, updates={"44.00,11.00": map_fixture["new_update"]})
    second_run = publish_run(
        fixture=map_fixture,
        updates={"44.00,11.00": second_update},
        assessment_date=MAP_DATE + timedelta(days=1),
        region_code="12",
    )
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE)

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=map_fixture["build"].version):
        client = Client()
        body = client.get("/api/v1/info").json()
        newer_run = publish_run(
            fixture=map_fixture,
            updates={"44.00,11.00": second_update},
            assessment_date=MAP_DATE + timedelta(days=1),
            calculation_version="map-calc-2",
            region_code="12",
        )
        after = client.get("/api/v1/info").json()

    assert body["run"]["run_id"] == map_id_of(first_run, second_run)
    assert body["run"]["assessment_date"] == (MAP_DATE + timedelta(days=1)).isoformat()
    assert [item["region_code"] for item in body["runs"]] == ["09", "12"]
    assert [item["run_id"] for item in body["runs"]] == [str(first_run.id), str(second_run.id)]
    assert body["runs"][0]["assessment_date"] == MAP_DATE.isoformat()
    assert body["runs"][1]["assessment_date"] == (MAP_DATE + timedelta(days=1)).isoformat()
    assert after["run"]["run_id"] == map_id_of(first_run, newer_run)
    assert after["run"]["run_id"] != body["run"]["run_id"]
    grid = map_fixture["build"].grid_build
    bounds = [
        shapely_transform(load_wkb(bytes(region.geometry.wkb)), GRID_TO_WGS84.transform, interleaved=False).bounds
        for region in GridRegion.objects.filter(build=grid)
    ]
    expected = (min(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds), max(b[3] for b in bounds))
    assert all(
        isclose(body["coverage"][name], value, abs_tol=1e-7)
        for name, value in zip(("west", "south", "east", "north"), expected, strict=True)
    )


@pytest.mark.django_db(transaction=True)
def test_overview_averages_squares_of_two_regions_with_their_own_dates(
    map_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_second_region(fixture=map_fixture)
    cells = map_fixture["cells"]
    second_update = add_update(
        fixture=map_fixture,
        cell_key="44.00,11.00",
        forest_cell=cells[3],
        values=bytes([60] * 8),
        label="overview-two-regions",
        assessment_date=MAP_DATE + timedelta(days=1),
    )
    first_run = publish_run(fixture=map_fixture, updates={"44.00,11.00": map_fixture["new_update"]})
    second_run = publish_run(
        fixture=map_fixture,
        updates={"44.00,11.00": second_update},
        assessment_date=MAP_DATE + timedelta(days=1),
        region_code="12",
    )
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE + timedelta(days=1))

    with override_settings(SPORAMETEO_FOREST_BUILD_VERSION=map_fixture["build"].version):
        body = Client().get(
            "/api/v1/estimate-overview",
            {"bbox": "9,43,12,45", "target_date": (MAP_DATE + timedelta(days=1)).isoformat()},
        ).json()

    assert body["run_id"] == map_id_of(first_run, second_run)
    assert body["assessment_date"] == (MAP_DATE + timedelta(days=1)).isoformat()
    properties = [feature["properties"] for feature in body["features"]]
    assert len(properties) == 1
    assert properties[0]["forest_square_count"] == 2
    assert properties[0]["scored_square_count"] == 2
    assert properties[0]["percentage"] == 51
    assert properties[0]["assessment_date"] == MAP_DATE.isoformat()
