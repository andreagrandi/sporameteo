"""Check Tuscany input readiness, host-tree preparation and source versioning."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path

import numpy as np
import pytest
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.management import call_command
from django.core.management.base import CommandError
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from estimate_updates.host_trees import saved_host_trees, tree_inputs_for_area
from estimate_updates.models import HostTreeCache, TuscanyInputSet
from sporameteo.host_trees import SPECIES, read_manifest
from national_forest import layer
from national_forest.models import ForestBuild, ForestCell, ForestChunk
from national_grid.models import GridBuild, GridBuildCell, GridCell, GridChunk, GridRegion
from national_terrain.models import (
    TerrainBuild,
    TerrainCell,
    TerrainChunk,
    TerrainChunkSource,
    TerrainSourceTile,
)


pytestmark = pytest.mark.django_db

TILE_NAME = "Copernicus_DSM_COG_10_N43_00_E011_00_DEM"
CORNERS = ((1_000, 1_000), (1_500, 1_000), (2_000, 1_000))


def _square(*, west: int, south: int) -> Polygon:
    return Polygon([(west, south), (west + 500, south), (west + 500, south + 500), (west, south + 500), (west, south)])


def _raster_archive(*, value: float) -> bytes:
    with MemoryFile() as memory:
        with memory.open(
            driver="GTiff", height=2, width=3, count=1, dtype="float32", crs="EPSG:3035",
            transform=from_origin(west=0, north=2000, xsize=1000, ysize=1000), nodata=-9999.0,
        ) as dataset:
            dataset.write(np.full((2, 3), value, dtype="float32"), indexes=1)
        raster = memory.read()
    buffer = io.BytesIO()
    with zipfile.ZipFile(file=buffer, mode="w") as archive:
        archive.writestr(zinfo_or_arcname="source.tif", data=raster)
    return buffer.getvalue()


def _sha(*, path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve fake JRC archives and record every request."""
    calls: list[str] = []
    state = {"value": 0.2}

    def download(*, url: str, timeout: int) -> io.BytesIO:
        calls.append(url)
        return io.BytesIO(initial_bytes=_raster_archive(value=state["value"]))

    download.state = state
    monkeypatch.setattr("urllib.request.urlopen", download)
    return calls


@pytest.fixture
def tuscany(tmp_path: Path, settings, requests) -> dict:
    """Build a small Tuscany grid with forest and terrain rows and their source files."""
    grid = GridBuild.objects.create(
        version=f"grid-{uuid.uuid4()}", fingerprint="d" * 64, status=GridBuild.Status.COMPLETED,
        planned_chunk_count=1, completed_chunk_count=1, cell_count=3,
    )
    region = GridRegion.objects.create(
        build=grid, istat_code="09", name="Toscana", geometry=MultiPolygon([_square(west=1_000, south=1_000)]),
    )
    chunk = GridChunk.objects.create(
        build=grid, west_e=1_000, south_n=1_000, east_e=2_500, north_n=1_500,
        status=GridChunk.Status.COMPLETED, cell_count=3,
    )
    memberships = []
    for west, south in CORNERS:
        cell = GridCell.objects.create(
            cell_id=f"500m_E{west}_N{south}-{grid.version}", corner_e=west, corner_n=south,
            geometry=_square(west=west, south=south),
        )
        memberships.append(GridBuildCell.objects.create(
            build=grid, cell=cell, chunk=chunk, owner_region=region,
            land_geometry=MultiPolygon([_square(west=west, south=south)]), land_share=1.0,
        ))
    layer_path = tmp_path / "forest" / "layer.geojson.gz"
    layer_path.parent.mkdir()
    layer_path.write_bytes(gzip.compress(json.dumps({"type": "FeatureCollection", "features": []}).encode()))
    source_path = tmp_path / "forest" / "chunk.tif"
    source_path.write_bytes(b"forest source")
    forest = ForestBuild.objects.create(
        version=f"forest-{uuid.uuid4()}", grid_build=grid, fingerprint="e" * 64,
        status=ForestBuild.Status.COMPLETED, planned_chunk_count=1, completed_chunk_count=1,
        cell_count=3, forest_cell_count=2, weather_cell_count=1,
        layer_path=str(layer_path), layer_sha256=_sha(path=layer_path),
    )
    forest_chunk = ForestChunk.objects.create(
        build=forest, grid_chunk=chunk, west_e=1_000, south_n=1_000, east_e=2_500, north_n=1_500,
        status=ForestChunk.Status.COMPLETED, cell_count=3, source_path=str(source_path),
        source_sha256=_sha(path=source_path),
    )
    forest_cells = []
    for index, membership in enumerate(memberships):
        forest_cells.append(ForestCell.objects.create(
            build=forest, grid_build_cell=membership, chunk=forest_chunk, total_pixel_count=100,
            valid_pixel_count=100, forest_pixel_count=80 if index < 2 else 0,
            broadleaf_pixel_count=80 if index < 2 else 0, conifer_pixel_count=0,
            unclassifiable_pixel_count=0, outside_pixel_count=0, valid_coverage=1.0,
            coverage_quality="high", forest_share=0.8 if index < 2 else 0.0,
            broadleaf_share=1.0 if index < 2 else 0.0,
            leaf_type=ForestCell.LeafType.BROADLEAF if index < 2 else ForestCell.LeafType.NO_FOREST,
            is_forest=index < 2, weather_latitude=43.5 if index < 2 else None,
            weather_longitude=11.2 if index < 2 else None,
        ))
    cache_dir = tmp_path / "terrain"
    cache_dir.mkdir()
    tile_path = cache_dir / f"{TILE_NAME}.tif"
    tile_path.write_bytes(b"terrain tile")
    terrain = TerrainBuild.objects.create(
        version=f"terrain-{uuid.uuid4()}", grid_build=grid, fingerprint="1" * 64,
        grid_fingerprint=grid.fingerprint, status=TerrainBuild.Status.COMPLETED,
        planned_chunk_count=1, completed_chunk_count=1, result_count=3,
    )
    terrain_chunk = TerrainChunk.objects.create(
        build=terrain, grid_chunk=chunk, status=TerrainChunk.Status.COMPLETED, result_count=3,
    )
    tile = TerrainSourceTile.objects.create(
        build=terrain, tile_name=TILE_NAME, status=TerrainSourceTile.Status.AVAILABLE,
        url="https://example.invalid/tile", release="fixture", credit="fixture", sha256=_sha(path=tile_path),
    )
    TerrainChunkSource.objects.create(chunk=terrain_chunk, tile=tile)
    for membership in memberships:
        TerrainCell.objects.create(
            build=terrain, grid_build_cell=membership, chunk=terrain_chunk, altitude_m=100.0,
            slope_deg=5.0, aspect_deg=10.0, altitude_valid_fraction=1.0, slope_valid_fraction=1.0,
            aspect_valid_fraction=1.0,
        )
    settings.SPORAMETEO_FOREST_BUILD_VERSION = forest.version
    settings.SPORAMETEO_TERRAIN_BUILD_VERSION = terrain.version
    settings.SPORAMETEO_JRC_DIRECTORY = str(tmp_path / "jrc")
    return {
        "grid": grid, "region": region, "chunk": chunk, "forest": forest, "terrain": terrain,
        "memberships": memberships, "forest_cells": forest_cells, "cache_dir": cache_dir,
        "tile_path": tile_path, "layer_path": layer_path, "source_path": source_path,
        "jrc": tmp_path / "jrc",
    }


def _run(*arguments: str, tuscany: dict) -> str:
    out = io.StringIO()
    call_command("prepare_region_inputs", "--regions=09", f"--terrain-cache-dir={tuscany['cache_dir']}", *arguments, stdout=out)
    return out.getvalue()


def _report(*arguments: str, tuscany: dict) -> dict:
    return json.loads(_run("--json", *arguments, tuscany=tuscany))


def _source(*, report: dict, name: str) -> dict:
    return next(item for item in report["sources"] if item["source"] == name)


def test_dry_run_lists_every_source_and_writes_nothing(tuscany, requests) -> None:
    report = _report("--dry-run", tuscany=tuscany)
    assert [item["source"] for item in report["sources"]] == ["administrative_geometry", "forest", "terrain", "host_trees"]
    assert report["region_code"] == "09"
    versions = {item["source"]: item["version"] for item in report["sources"]}
    assert versions["administrative_geometry"] == tuscany["grid"].version
    assert versions["forest"] == tuscany["forest"].version
    assert versions["terrain"] == tuscany["terrain"].version
    for item in report["sources"][:3]:
        assert item["state"] == "ready"
    trees = _source(report=report, name="host_trees")
    assert trees["state"] == "missing"
    assert "prepare_region_inputs" in trees["action"]
    assert all(item["action"] for item in report["sources"])
    assert requests == []
    assert HostTreeCache.objects.count() == 0
    assert TuscanyInputSet.objects.count() == 0
    assert not tuscany["jrc"].exists()


def test_dry_run_text_output_shows_region_state_and_totals(tuscany, requests) -> None:
    _run(tuscany=tuscany)
    text = _run("--dry-run", tuscany=tuscany)
    assert "Dry run for regions 09" in text
    assert "09 Toscana: ready, 3 squares, 2 forest squares" in text
    assert "Totals: regions=1, ready=1, squares=3, forest_squares=2" in text
    assert "Elapsed:" in text


def test_dry_run_text_output_names_the_failed_source_and_its_action(tuscany) -> None:
    text = _run("--dry-run", tuscany=tuscany)
    assert "host_trees is missing. Action: Run prepare_region_inputs" in text


def test_real_run_stores_host_trees_for_every_eligible_square(tuscany, requests) -> None:
    report = _report(tuscany=tuscany)
    assert report["ready"] is True
    assert len(requests) == len(SPECIES)
    _, version = read_manifest(directory=tuscany["jrc"])
    rows = {row.cell_id: row.values for row in HostTreeCache.objects.filter(source_version=version)}
    eligible = {
        cell.grid_build_cell.cell.cell_id for cell in tuscany["forest_cells"] if cell.is_forest
    }
    assert set(rows) == eligible
    assert len(eligible) == 2
    assert all(values == pytest.approx([0.2] * 5) for values in rows.values())
    assert report["counts"]["eligible_squares"] == 2
    assert report["counts"]["host_tree_rows"] == 2
    assert report["elapsed_seconds"] >= 0
    stored = TuscanyInputSet.objects.get()
    assert stored.region_code == "09"
    assert stored.grid_build_version == tuscany["grid"].version
    assert stored.forest_build_version == tuscany["forest"].version
    assert stored.terrain_build_version == tuscany["terrain"].version
    assert stored.host_tree_source_version == version
    assert stored.eligible_square_count == 2
    assert stored.host_tree_row_count == 2
    assert stored.jrc_manifest["source_year"] == 2006


def test_repeat_run_makes_no_request_and_no_duplicate_rows(tuscany, requests, monkeypatch) -> None:
    _run(tuscany=tuscany)

    def refuse(*, url: str, timeout: int) -> None:
        raise AssertionError(f"Unexpected remote request: {url}")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    report = _report(tuscany=tuscany)
    assert report["ready"] is True
    assert HostTreeCache.objects.count() == 2
    assert TuscanyInputSet.objects.count() == 1


def test_small_batches_still_cover_all_squares(tuscany, monkeypatch) -> None:
    monkeypatch.setattr("estimate_updates.region_inputs.BATCH_SIZE", 1)
    _run(tuscany=tuscany)
    assert HostTreeCache.objects.count() == 2


def test_prefilled_rows_serve_runtime_lookup_without_sampling(tuscany, monkeypatch) -> None:
    _run(tuscany=tuscany)

    def refuse(**kwargs) -> dict:
        assert kwargs["centres"] == {}, "Runtime sampling should not be needed"
        return {}

    monkeypatch.setattr("estimate_updates.host_trees.sample_squares", refuse)
    cells = [cell for cell in tuscany["forest_cells"] if cell.is_forest]
    result = tree_inputs_for_area(forest_cells=cells)
    assert set(result["cells"]) == {str(cell.id) for cell in cells}


def test_grid_with_a_membership_outside_tuscany_is_rejected(tuscany, requests) -> None:
    other = GridRegion.objects.create(
        build=tuscany["grid"], istat_code="10", name="Umbria",
        geometry=MultiPolygon([_square(west=9_000, south=1_000)]),
    )
    GridBuildCell.objects.filter(id=tuscany["memberships"][2].id).update(owner_region=other)
    report = _report("--dry-run", tuscany=tuscany)
    grid = _source(report=report, name="administrative_geometry")
    assert grid["state"] == "rejected"
    assert "prepare_region_map_database" in grid["action"]
    assert report["ready"] is False
    with pytest.raises(CommandError, match="administrative_geometry is rejected"):
        _run(tuscany=tuscany)
    assert requests == []
    assert HostTreeCache.objects.count() == 0


def test_missing_forest_row_fails_and_names_the_square(tuscany, requests) -> None:
    removed = tuscany["forest_cells"][2]
    removed.delete()
    with pytest.raises(CommandError, match="forest is incomplete"):
        _run(tuscany=tuscany)
    report = _report("--dry-run", tuscany=tuscany)
    forest = _source(report=report, name="forest")
    assert forest["counts"]["missing_squares"] == 1
    assert tuscany["memberships"][2].cell_id in forest["detail"][0]
    assert requests == []
    assert HostTreeCache.objects.count() == 0


def test_missing_terrain_row_fails_and_names_the_square(tuscany, requests) -> None:
    TerrainCell.objects.filter(grid_build_cell=tuscany["memberships"][0]).delete()
    with pytest.raises(CommandError, match="terrain is incomplete"):
        _run(tuscany=tuscany)
    terrain = _source(report=_report("--dry-run", tuscany=tuscany), name="terrain")
    assert terrain["counts"]["missing_squares"] == 1
    assert tuscany["memberships"][0].cell_id in terrain["detail"][0]
    assert HostTreeCache.objects.count() == 0


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_forest_layer_problem_names_the_file(tuscany, damage) -> None:
    if damage == "missing":
        tuscany["layer_path"].unlink()
    else:
        tuscany["layer_path"].write_bytes(gzip.compress(b'{"type": "FeatureCollection", "features": [], "x": 1}'))
    forest = _source(report=_report("--dry-run", tuscany=tuscany), name="forest")
    assert forest["state"] == ("missing" if damage == "missing" else "corrupt")
    assert str(tuscany["layer_path"]) in forest["detail"][0]
    with pytest.raises(CommandError, match="forest is"):
        _run(tuscany=tuscany)


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_forest_source_file_problem_names_file_and_chunk(tuscany, damage) -> None:
    if damage == "missing":
        tuscany["source_path"].unlink()
    else:
        tuscany["source_path"].write_bytes(b"changed")
    forest = _source(report=_report("--dry-run", tuscany=tuscany), name="forest")
    assert forest["state"] == ("missing" if damage == "missing" else "corrupt")
    assert str(tuscany["source_path"]) in forest["detail"][0]
    assert "chunk at E1000 N1000" in forest["detail"][0]


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_terrain_tile_problem_names_the_tile(tuscany, damage) -> None:
    if damage == "missing":
        tuscany["tile_path"].unlink()
    else:
        tuscany["tile_path"].write_bytes(b"changed")
    terrain = _source(report=_report("--dry-run", tuscany=tuscany), name="terrain")
    assert terrain["state"] == ("missing" if damage == "missing" else "corrupt")
    assert TILE_NAME in terrain["detail"][0]
    with pytest.raises(CommandError, match="terrain is"):
        _run(tuscany=tuscany)


def test_terrain_tile_without_a_file_is_ignored_when_confirmed_missing(tuscany) -> None:
    tuscany["tile_path"].unlink()
    TerrainSourceTile.objects.update(status=TerrainSourceTile.Status.CONFIRMED_MISSING)
    terrain = _source(report=_report("--dry-run", tuscany=tuscany), name="terrain")
    assert terrain["state"] == "ready"


def test_changed_jrc_file_fails_without_download(tuscany, requests) -> None:
    _run(tuscany=tuscany)
    calls = len(requests)
    (tuscany["jrc"] / f"{SPECIES[0]}.tif").write_bytes(b"corrupt")
    report = _report("--dry-run", tuscany=tuscany)
    trees = _source(report=report, name="host_trees")
    assert trees["state"] == "corrupt"
    assert f"{SPECIES[0]}.tif" in trees["detail"][0]
    with pytest.raises(CommandError, match="host_trees is corrupt"):
        _run(tuscany=tuscany)
    assert len(requests) == calls


def test_missing_jrc_raster_fails_and_names_the_file(tuscany) -> None:
    _run(tuscany=tuscany)
    (tuscany["jrc"] / f"{SPECIES[3]}.tif").unlink()
    trees = _source(report=_report("--dry-run", tuscany=tuscany), name="host_trees")
    assert trees["state"] == "missing"
    assert f"{SPECIES[3]}.tif" in trees["detail"][0]


def test_missing_host_tree_rows_are_refilled_for_current_version(tuscany, requests) -> None:
    _run(tuscany=tuscany)
    HostTreeCache.objects.order_by("id").first().delete()
    trees = _source(report=_report("--dry-run", tuscany=tuscany), name="host_trees")
    assert trees["state"] == "incomplete"
    assert trees["counts"]["missing_squares"] == 1
    calls = len(requests)
    _run(tuscany=tuscany)
    assert HostTreeCache.objects.count() == 2
    assert len(requests) == calls
    assert TuscanyInputSet.objects.count() == 1


def test_forest_only_option_reports_fallback_without_downloading(tuscany, requests) -> None:
    report = _report("--allow-forest-only", tuscany=tuscany)
    trees = _source(report=report, name="host_trees")
    assert trees["state"] == "fallback"
    assert "forest only" in trees["action"]
    assert report["ready"] is True
    assert requests == []
    assert HostTreeCache.objects.count() == 0
    assert TuscanyInputSet.objects.count() == 0
    assert not tuscany["jrc"].exists()


def test_forest_only_option_does_not_hide_a_forest_failure(tuscany) -> None:
    tuscany["layer_path"].unlink()
    with pytest.raises(CommandError, match="forest is missing"):
        _run("--allow-forest-only", tuscany=tuscany)


def test_new_jrc_version_adds_rows_and_keeps_saved_estimate_inputs(tuscany, requests, settings, tmp_path) -> None:
    _run(tuscany=tuscany)
    _, old_version = read_manifest(directory=tuscany["jrc"])
    cells = [cell for cell in tuscany["forest_cells"] if cell.is_forest]
    saved = tree_inputs_for_area(forest_cells=cells)
    first_cell = str(cells[0].id)

    requests.clear()
    from urllib.request import urlopen

    urlopen.state["value"] = 0.6
    settings.SPORAMETEO_JRC_DIRECTORY = str(tmp_path / "jrc_new")
    _run(tuscany=tuscany)
    _, new_version = read_manifest(directory=tmp_path / "jrc_new")
    assert new_version != old_version
    assert len(requests) == len(SPECIES)
    assert HostTreeCache.objects.filter(source_version=old_version).count() == 2
    assert HostTreeCache.objects.filter(source_version=new_version).count() == 2
    assert TuscanyInputSet.objects.count() == 2
    assert set(TuscanyInputSet.objects.values_list("host_tree_source_version", flat=True)) == {old_version, new_version}
    latest = tree_inputs_for_area(forest_cells=cells)
    assert latest["source"]["source_id"] == f"jrc:{new_version}"
    assert latest["cells"][first_cell] == pytest.approx([0.6] * 5)
    old = saved_host_trees(tree_inputs=saved, forest_cell_id=cells[0].id)
    assert old.source_id == f"jrc:{old_version}"
    assert old.values == pytest.approx((0.2,) * 5)


def test_skip_source_files_accepts_missing_forest_chunk_and_terrain_tile(tuscany) -> None:
    tuscany["source_path"].unlink()
    tuscany["tile_path"].unlink()
    report = _report("--dry-run", "--skip-source-files", tuscany=tuscany)
    for name in ("forest", "terrain"):
        source = _source(report=report, name=name)
        assert source["state"] == "ready"
        assert source["detail"] == ["Source files were not checked."]
    assert _source(report=_report("--dry-run", tuscany=tuscany), name="forest")["state"] == "missing"
    assert _source(report=_report("--dry-run", tuscany=tuscany), name="terrain")["state"] == "missing"


def test_skip_source_files_works_in_a_real_run(tuscany, requests) -> None:
    tuscany["source_path"].unlink()
    tuscany["tile_path"].unlink()
    report = _report("--skip-source-files", tuscany=tuscany)
    assert _source(report=report, name="forest")["state"] == "ready"
    assert _source(report=report, name="terrain")["state"] == "ready"


def test_skip_source_files_still_checks_the_forest_layer_file(tuscany) -> None:
    tuscany["layer_path"].write_bytes(gzip.compress(b'{"type": "FeatureCollection", "features": [], "x": 1}'))
    forest = _source(report=_report("--dry-run", "--skip-source-files", tuscany=tuscany), name="forest")
    assert forest["state"] == "corrupt"


def test_forest_layer_stored_relative_to_the_repository_is_found(tuscany, monkeypatch) -> None:
    layer_path = tuscany["layer_path"]
    monkeypatch.setattr(layer, "REPOSITORY_ROOT", layer_path.parent)
    ForestBuild.objects.update(layer_path=layer_path.name)
    forest = _source(report=_report("--dry-run", tuscany=tuscany), name="forest")
    assert forest["state"] == "ready"
