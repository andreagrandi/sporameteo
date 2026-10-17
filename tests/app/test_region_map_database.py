"""Checks for adding region map data to a database that already holds data."""

from __future__ import annotations

import copy
import json
import uuid
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.management import call_command
from django.core.management.base import CommandError

from national_forest.models import ForestBuild, ForestCell, ForestChunk
from national_grid.models import (
    GridBuild,
    GridBuildCell,
    GridCell,
    GridChunk,
    GridRegion,
    Municipality,
)
from national_grid.subset import (
    SourceBuildError,
    TuscanySubsetError,
    prepare_region_subset,
    prepare_tuscany_subset,
)
from national_terrain.models import TerrainBuild, TerrainCell, TerrainChunk


SOURCE_ALIAS = "tuscany_subset_source"
if SOURCE_ALIAS not in settings.DATABASES:
    source_database = copy.deepcopy(settings.DATABASES["default"])
    source_database["NAME"] = f"{source_database['NAME']}_source"
    settings.DATABASES[SOURCE_ALIAS] = source_database

REGION_CELLS = {"09": [1_000, 1_500], "10": [3_000, 3_500], "11": [5_000]}
DATABASES = {"default", SOURCE_ALIAS}


def _square(*, west: int, south: int = 2_000, size: int = 500) -> Polygon:
    return Polygon(
        [
            (west, south),
            (west + size, south),
            (west + size, south + size),
            (west, south + size),
            (west, south),
        ],
        srid=3035,
    )


def _create_source(*, alias: str) -> dict[str, Any]:
    """Create completed source builds with regions 09, 10 and 11."""
    manager = lambda model: model.objects.using(alias)
    total = sum(len(corners) for corners in REGION_CELLS.values())
    grid = manager(GridBuild).create(
        version=f"source-grid-{uuid.uuid4()}",
        fingerprint="d" * 64,
        status=GridBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=total,
    )
    grid_chunk = manager(GridChunk).create(
        build=grid,
        west_e=1_000,
        south_n=2_000,
        east_e=6_000,
        north_n=2_500,
        status=GridChunk.Status.COMPLETED,
        cell_count=total,
    )
    forest = manager(ForestBuild).create(
        version=f"source-forest-{uuid.uuid4()}",
        grid_build=grid,
        fingerprint="e" * 64,
        status=ForestBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        cell_count=total,
        forest_cell_count=total,
    )
    forest_chunk = manager(ForestChunk).create(
        build=forest,
        grid_chunk=grid_chunk,
        west_e=1_000,
        south_n=2_000,
        east_e=6_000,
        north_n=2_500,
        status=ForestChunk.Status.COMPLETED,
        cell_count=total,
        source_sha256="f" * 64,
        source_path="/tmp/source.tif",
        source_metadata={"source": "fixture"},
    )
    terrain = manager(TerrainBuild).create(
        version=f"source-terrain-{uuid.uuid4()}",
        grid_build=grid,
        fingerprint="1" * 64,
        grid_fingerprint=grid.fingerprint,
        status=TerrainBuild.Status.COMPLETED,
        planned_chunk_count=1,
        completed_chunk_count=1,
        result_count=total,
    )
    terrain_chunk = manager(TerrainChunk).create(
        build=terrain,
        grid_chunk=grid_chunk,
        status=TerrainChunk.Status.COMPLETED,
        result_count=total,
    )
    for code, corners in REGION_CELLS.items():
        region = manager(GridRegion).create(
            build=grid,
            istat_code=code,
            name=f"Region {code}",
            geometry=MultiPolygon([_square(west=corners[0])]),
        )
        for west in corners:
            cell = manager(GridCell).create(
                cell_id=f"500m_E{west}_N2000",
                corner_e=west,
                corner_n=2_000,
                geometry=_square(west=west),
            )
            membership = manager(GridBuildCell).create(
                build=grid,
                cell=cell,
                chunk=grid_chunk,
                owner_region=region,
                land_geometry=MultiPolygon([_square(west=west)]),
                land_share=1.0,
            )
            manager(ForestCell).create(
                build=forest,
                grid_build_cell=membership,
                chunk=forest_chunk,
                total_pixel_count=100,
                valid_pixel_count=100,
                forest_pixel_count=80,
                broadleaf_pixel_count=80,
                conifer_pixel_count=0,
                unclassifiable_pixel_count=0,
                outside_pixel_count=0,
                valid_coverage=1.0,
                coverage_quality="high",
                forest_share=0.8,
                broadleaf_share=1.0,
                leaf_type=ForestCell.LeafType.BROADLEAF,
                is_forest=True,
                weather_latitude=43.5,
                weather_longitude=11.2,
            )
            manager(TerrainCell).create(
                build=terrain,
                grid_build_cell=membership,
                chunk=terrain_chunk,
                altitude_m=100.0,
                slope_deg=5.0,
                aspect_deg=10.0,
                altitude_valid_fraction=1.0,
                slope_valid_fraction=1.0,
                aspect_valid_fraction=1.0,
                source_gap=False,
                source_gap_detail="",
            )
    return {"grid": grid, "forest": forest, "terrain": terrain}


def _versions(*, source: dict[str, Any]) -> dict[str, str]:
    return {
        "source_grid_version": source["grid"].version,
        "source_forest_version": source["forest"].version,
        "source_terrain_version": source["terrain"].version,
    }


def _prepare_regions(
    *, source: dict[str, Any], tmp_path: Path, codes: list[str], tag: str = "new", **extra: Any
) -> Any:
    return prepare_region_subset(
        source_alias=SOURCE_ALIAS,
        target_alias="default",
        grid_version=f"{tag}-grid",
        forest_version=f"{tag}-forest",
        terrain_version=f"{tag}-terrain",
        output_dir=tmp_path,
        region_codes=codes,
        **_versions(source=source),
        **extra,
    )


def _target_state() -> dict[str, Any]:
    return {
        "builds": sorted(
            [
                *GridBuild.objects.values_list("version", "cell_count"),
                *ForestBuild.objects.values_list("version", "cell_count"),
                *TerrainBuild.objects.values_list("version", "result_count"),
            ]
        ),
        "cells": sorted(GridCell.objects.values_list("cell_id", "corner_e", "municipality_id")),
        "regions": GridRegion.objects.count(),
        "memberships": GridBuildCell.objects.count(),
        "forest_cells": ForestCell.objects.count(),
        "terrain_cells": TerrainCell.objects.count(),
    }


def _target_with_tuscany(*, source: dict[str, Any], tmp_path: Path) -> None:
    """Fill the target with a Tuscany subset and a municipality on its cells."""
    prepare_tuscany_subset(
        source_alias=SOURCE_ALIAS,
        target_alias="default",
        grid_version="tuscany-grid-test",
        forest_version="tuscany-forest-test",
        terrain_version="tuscany-terrain-test",
        output_dir=tmp_path / "tuscany",
        **_versions(source=source),
    )
    municipality = Municipality.objects.create(
        istat_code="048017",
        name="Fixture",
        region_code="09",
        geometry=MultiPolygon([_square(west=1_000, size=3_000)]),
    )
    GridCell.objects.update(municipality=municipality)
    # Region 10 shares its first cell with the target, and keeps its municipality.
    GridCell.objects.create(
        cell_id="500m_E3000_N2000",
        corner_e=3_000,
        corner_n=2_000,
        geometry=_square(west=3_000),
        municipality=municipality,
    )


@pytest.mark.django_db(transaction=True, databases=DATABASES)
def test_two_regions_are_added_next_to_an_existing_subset(tmp_path: Path) -> None:
    source = _create_source(alias=SOURCE_ALIAS)
    _target_with_tuscany(source=source, tmp_path=tmp_path)
    before = _target_state()
    old_grid = GridBuild.objects.get(version="tuscany-grid-test")

    result = _prepare_regions(source=source, tmp_path=tmp_path, codes=["10", "11"])

    new_grid = GridBuild.objects.get(version="new-grid")
    new_forest = ForestBuild.objects.get(version="new-forest")
    new_terrain = TerrainBuild.objects.get(version="new-terrain")
    memberships = GridBuildCell.objects.filter(build=new_grid)
    assert set(memberships.values_list("cell_id", flat=True)) == {
        "500m_E3000_N2000",
        "500m_E3500_N2000",
        "500m_E5000_N2000",
    }
    assert set(memberships.values_list("owner_region__istat_code", flat=True)) == {"10", "11"}
    assert new_grid.cell_count == 3
    assert new_grid.source_metadata["subset"] == "regions"
    assert new_grid.source_metadata["region_codes"] == ["10", "11"]
    assert ForestCell.objects.filter(build=new_forest).count() == 3
    assert TerrainCell.objects.filter(build=new_terrain).count() == 3
    assert new_forest.cell_count == 3
    assert new_terrain.result_count == 3
    assert result.region_squares == {"10": 2, "11": 1}
    assert result.grid_cells_reused == 1
    assert result.layer_path is not None and Path(result.layer_path).is_file()
    assert new_forest.layer_path == str(result.layer_path)

    old_grid.refresh_from_db()
    assert old_grid.source_metadata["subset"] == "tuscany"
    after = _target_state()
    assert set(before["cells"]) <= set(after["cells"])
    assert len(after["cells"]) == len(before["cells"]) + 2
    assert set(before["builds"]) <= set(after["builds"])
    assert after["regions"] == before["regions"] + 2
    assert after["memberships"] == before["memberships"] + 3
    assert GridBuildCell.objects.filter(build=old_grid).count() == 2


@pytest.mark.django_db(transaction=True, databases=DATABASES)
def test_existing_target_version_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    source = _create_source(alias=SOURCE_ALIAS)
    _target_with_tuscany(source=source, tmp_path=tmp_path)
    before = _target_state()

    with pytest.raises(TuscanySubsetError, match="tuscany-forest-test"):
        prepare_region_subset(
            source_alias=SOURCE_ALIAS,
            target_alias="default",
            grid_version="new-grid",
            forest_version="tuscany-forest-test",
            terrain_version="new-terrain",
            output_dir=tmp_path / "new",
            region_codes=["10"],
            **_versions(source=source),
        )

    assert _target_state() == before
    assert not (tmp_path / "new").exists()


@pytest.mark.django_db(transaction=True, databases=DATABASES)
def test_unknown_and_duplicate_region_codes_are_refused(tmp_path: Path) -> None:
    source = _create_source(alias=SOURCE_ALIAS)

    with pytest.raises(SourceBuildError, match="'99'"):
        _prepare_regions(source=source, tmp_path=tmp_path, codes=["10", "99"])
    with pytest.raises(SourceBuildError, match="more than once"):
        _prepare_regions(source=source, tmp_path=tmp_path, codes=["10", "10"])

    assert GridBuild.objects.count() == 0
    assert GridCell.objects.count() == 0
    assert not (tmp_path / "new-forest").exists()


@pytest.mark.django_db(transaction=True, databases=DATABASES)
def test_cell_with_other_coordinates_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    source = _create_source(alias=SOURCE_ALIAS)
    GridCell.objects.create(
        cell_id="500m_E3500_N2000",
        corner_e=3_000,
        corner_n=2_000,
        geometry=_square(west=3_000),
    )
    before = _target_state()

    with pytest.raises(TuscanySubsetError, match="500m_E3500_N2000"):
        _prepare_regions(source=source, tmp_path=tmp_path, codes=["10", "11"])

    assert _target_state() == before
    assert not (tmp_path / "new-forest").exists()


@pytest.mark.django_db(transaction=True, databases=DATABASES)
def test_failed_copy_removes_the_layer_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _create_source(alias=SOURCE_ALIAS)

    def fail(**kwargs: Any) -> None:
        raise RuntimeError("finish failed")

    monkeypatch.setattr("national_grid.subset._finish_forest_build", fail)

    with pytest.raises(RuntimeError, match="finish failed"):
        _prepare_regions(source=source, tmp_path=tmp_path, codes=["10"])

    assert GridBuild.objects.count() == 0
    assert not (tmp_path / "new-forest").exists()


@pytest.mark.django_db(transaction=True, databases=DATABASES)
def test_all_chooses_every_region_with_default_version_names(tmp_path: Path) -> None:
    source = _create_source(alias=SOURCE_ALIAS)

    output = _run_command(source=source, tmp_path=tmp_path, regions="all")

    grid_version = f"regions-all-grid-{source['grid'].version}"
    assert output["region_codes"] == ["09", "10", "11"]
    assert output["grid_version"] == grid_version
    assert output["forest_version"] == f"regions-all-forest-{source['forest'].version}"
    assert output["terrain_version"] == f"regions-all-terrain-{source['terrain'].version}"
    assert GridBuildCell.objects.filter(build__version=grid_version).count() == 5
    assert output["region_squares"] == {"09": 2, "10": 2, "11": 1}


@pytest.mark.django_db(transaction=True, databases=DATABASES)
def test_chosen_regions_appear_in_default_version_names(tmp_path: Path) -> None:
    source = _create_source(alias=SOURCE_ALIAS)

    output = _run_command(source=source, tmp_path=tmp_path, regions="10,11", dry_run=True)

    assert output["grid_version"] == f"regions-10-11-grid-{source['grid'].version}"
    assert output["forest_version"] == f"regions-10-11-forest-{source['forest'].version}"
    assert output["terrain_version"] == f"regions-10-11-terrain-{source['terrain'].version}"


@pytest.mark.django_db(transaction=True, databases=DATABASES)
def test_dry_run_prints_counts_and_writes_nothing(tmp_path: Path) -> None:
    source = _create_source(alias=SOURCE_ALIAS)
    _target_with_tuscany(source=source, tmp_path=tmp_path)
    before = _target_state()
    source_before = GridBuildCell.objects.using(SOURCE_ALIAS).count()
    files_before = sorted(tmp_path.rglob("*"))

    output = _run_command(source=source, tmp_path=tmp_path, regions="10,11", dry_run=True)

    assert output["dry_run"] is True
    assert output["layer_path"] is None
    assert output["grid_cells_reused"] == 1
    assert output["counts"]["grid_cells"] == 3
    assert output["counts"]["grid_cells_to_create"] == 2
    assert output["counts"]["forest_cells"] == 3
    assert output["counts"]["terrain_cells"] == 3
    assert _target_state() == before
    assert GridBuildCell.objects.using(SOURCE_ALIAS).count() == source_before
    assert sorted(tmp_path.rglob("*")) == files_before


@pytest.mark.django_db(transaction=True, databases=DATABASES)
def test_version_longer_than_120_characters_is_refused(tmp_path: Path) -> None:
    source = _create_source(alias=SOURCE_ALIAS)

    with pytest.raises(CommandError, match="longer than 120"):
        _run_command(
            source=source,
            tmp_path=tmp_path,
            regions="10",
            extra=["--grid-build-version", "g" * 121],
        )


def _run_command(
    *,
    source: dict[str, Any],
    tmp_path: Path,
    regions: str,
    dry_run: bool = False,
    extra: list[str] | None = None,
) -> dict[str, Any]:
    stdout = StringIO()
    arguments = [
        "--regions",
        regions,
        "--source-alias",
        SOURCE_ALIAS,
        "--target-alias",
        "default",
        "--source-grid-build-version",
        source["grid"].version,
        "--source-forest-build-version",
        source["forest"].version,
        "--source-terrain-build-version",
        source["terrain"].version,
        "--output-dir",
        str(tmp_path / "command"),
        *(extra or []),
    ]
    if dry_run:
        arguments.append("--dry-run")
    call_command("prepare_region_map_database", *arguments, stdout=stdout)
    return json.loads(stdout.getvalue())
