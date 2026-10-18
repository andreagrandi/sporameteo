"""Check the per-region input report, the shared host-tree rows and one input set per region."""

from __future__ import annotations

import io
import json

import pytest
from django.contrib.gis.geos import MultiPolygon
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction

from estimate_updates.models import HostTreeCache, TuscanyInputSet
from national_forest.models import ForestCell
from national_grid.models import GridBuildCell, GridCell, GridRegion
from national_terrain.models import TerrainCell
from tests.app.test_tuscany_inputs import _square, requests, tuscany  # noqa: F401


pytestmark = pytest.mark.django_db

SECOND_CORNERS = ((3_000, 1_000), (3_500, 1_000))


@pytest.fixture
def two_regions(tuscany) -> dict:
    """Add region 10 with two forest squares to the Tuscany fixture build."""
    grid, forest, terrain = tuscany["grid"], tuscany["forest"], tuscany["terrain"]
    region = GridRegion.objects.create(
        build=grid, istat_code="10", name="Umbria", geometry=MultiPolygon([_square(west=3_000, south=1_000)]),
    )
    memberships = []
    for west, south in SECOND_CORNERS:
        cell = GridCell.objects.create(
            cell_id=f"500m_E{west}_N{south}-{grid.version}", corner_e=west, corner_n=south,
            geometry=_square(west=west, south=south),
        )
        membership = GridBuildCell.objects.create(
            build=grid, cell=cell, chunk=tuscany["chunk"], owner_region=region,
            land_geometry=MultiPolygon([_square(west=west, south=south)]), land_share=1.0,
        )
        memberships.append(membership)
        ForestCell.objects.create(
            build=forest, grid_build_cell=membership, chunk=tuscany["forest_cells"][0].chunk,
            total_pixel_count=100, valid_pixel_count=100, forest_pixel_count=80,
            broadleaf_pixel_count=80, conifer_pixel_count=0, unclassifiable_pixel_count=0,
            outside_pixel_count=0, valid_coverage=1.0, coverage_quality="high", forest_share=0.8,
            broadleaf_share=1.0, leaf_type=ForestCell.LeafType.BROADLEAF, is_forest=True,
            weather_latitude=43.0, weather_longitude=12.0,
        )
        TerrainCell.objects.create(
            build=terrain, grid_build_cell=membership, chunk=tuscany["terrain"].chunks.first(),
            altitude_m=200.0, slope_deg=5.0, aspect_deg=10.0, altitude_valid_fraction=1.0,
            slope_valid_fraction=1.0, aspect_valid_fraction=1.0,
        )
    grid.cell_count = 5
    grid.save(update_fields=["cell_count"])
    return {**tuscany, "region_two": region, "memberships_two": memberships}


def _run(*arguments: str, fixture: dict) -> str:
    out = io.StringIO()
    call_command("prepare_region_inputs", f"--terrain-cache-dir={fixture['cache_dir']}", *arguments, stdout=out)
    return out.getvalue()


def _report(*arguments: str, fixture: dict) -> dict:
    return json.loads(_run("--json", *arguments, fixture=fixture))


def _region(*, report: dict, code: str) -> dict:
    return next(item for item in report["regions"] if item["region_code"] == code)


def test_two_ready_regions_get_one_input_set_each(two_regions) -> None:
    report = _report("--regions=09,10", fixture=two_regions)
    assert report["ready"] is True
    assert report["region_codes"] == ["09", "10"]
    assert [(item["region_code"], item["state"], item["squares"], item["forest_squares"]) for item in report["regions"]] == [
        ("09", "ready", 3, 2), ("10", "ready", 2, 2),
    ]
    assert _region(report=report, code="10")["name"] == "Umbria"
    assert HostTreeCache.objects.count() == 4
    sets = {item.region_code: item for item in TuscanyInputSet.objects.all()}
    assert set(sets) == {"09", "10"}
    assert sets["09"].eligible_square_count == sets["09"].host_tree_row_count == 2
    assert sets["10"].eligible_square_count == sets["10"].host_tree_row_count == 2
    assert sets["09"].host_tree_source_version == sets["10"].host_tree_source_version


def test_all_uses_every_region_of_the_grid(two_regions) -> None:
    report = _report("--regions=all", "--dry-run", fixture=two_regions)
    assert report["region_codes"] == ["09", "10"]


def test_text_output_has_one_line_per_region_and_totals(two_regions) -> None:
    text = _run("--regions=09,10", fixture=two_regions)
    assert "09 Toscana: ready, 3 squares, 2 forest squares" in text
    assert "10 Umbria: ready, 2 squares, 2 forest squares" in text
    assert "Totals: regions=2, ready=2, squares=5, forest_squares=4" in text


def test_missing_terrain_rows_name_the_region_and_squares(two_regions) -> None:
    _run("--regions=09,10", fixture=two_regions)
    gone = two_regions["memberships_two"][1]
    TerrainCell.objects.filter(grid_build_cell=gone).delete()
    report = _report("--regions=09,10", "--dry-run", fixture=two_regions)
    assert _region(report=report, code="09")["state"] == "ready"
    assert _region(report=report, code="09")["problems"] == []
    broken = _region(report=report, code="10")
    assert broken["state"] == "incomplete"
    assert [(item["source"], item["squares"], item["sample"]) for item in broken["problems"]] == [
        ("terrain", 1, gone.cell_id),
    ]
    text = _run("--regions=09,10", "--dry-run", fixture=two_regions)
    assert f"10 Umbria: not ready, terrain is incomplete for 1 squares, such as {gone.cell_id}" in text
    with pytest.raises(CommandError, match="Regions: 10 .terrain is incomplete for 1 squares"):
        _run("--regions=09,10", fixture=two_regions)


def test_build_level_failure_marks_every_region(two_regions) -> None:
    two_regions["layer_path"].unlink()
    report = _report("--regions=09,10", "--dry-run", fixture=two_regions)
    assert [(item["region_code"], item["problems"][0]["source"], item["problems"][0]["squares"]) for item in report["regions"]] == [
        ("09", "forest", 3), ("10", "forest", 2),
    ]


def test_stored_host_tree_values_are_not_sampled_again(two_regions, requests) -> None:
    _run("--regions=09,10", fixture=two_regions)
    kept = HostTreeCache.objects.filter(cell_id__contains="E1000_N1000").get()
    kept.values = [0.9] * 5
    kept.save(update_fields=["values"])
    before = {item.cell_id: (item.id, item.values) for item in HostTreeCache.objects.exclude(id=kept.id)}
    HostTreeCache.objects.filter(cell_id__contains="E3500_N1000").delete()
    _run("--regions=09,10", fixture=two_regions)
    kept.refresh_from_db()
    assert kept.values == [0.9] * 5
    after = {item.cell_id: (item.id, item.values) for item in HostTreeCache.objects.exclude(id=kept.id)}
    unchanged = {key: value for key, value in before.items() if "E3500_N1000" not in key}
    assert {key: after[key] for key in unchanged} == unchanged
    assert HostTreeCache.objects.count() == 4


def test_grid_region_that_was_not_chosen_is_rejected(two_regions) -> None:
    report = _report("--regions=09", "--dry-run", fixture=two_regions)
    grid = next(item for item in report["sources"] if item["source"] == "administrative_geometry")
    assert grid["state"] == "rejected"
    assert "Regions not chosen: 10" in grid["detail"][0]
    assert _region(report=report, code="09")["problems"][0]["source"] == "administrative_geometry"
    with pytest.raises(CommandError, match="Regions: 09"):
        _run("--regions=09", fixture=two_regions)
    assert HostTreeCache.objects.count() == 0


def test_chosen_region_missing_from_the_grid_is_rejected(two_regions) -> None:
    report = _report("--regions=09,10,11", "--dry-run", fixture=two_regions)
    grid = next(item for item in report["sources"] if item["source"] == "administrative_geometry")
    assert grid["state"] == "rejected"
    assert "missing from the grid: 11" in grid["detail"][0]
    assert _region(report=report, code="11")["squares"] == 0


def test_build_versions_come_from_the_arguments(two_regions, settings) -> None:
    forest, terrain = settings.SPORAMETEO_FOREST_BUILD_VERSION, settings.SPORAMETEO_TERRAIN_BUILD_VERSION
    settings.SPORAMETEO_FOREST_BUILD_VERSION = "other-forest"
    settings.SPORAMETEO_TERRAIN_BUILD_VERSION = "other-terrain"
    report = _report(
        "--regions=09,10", "--dry-run", f"--forest-build-version={forest}", f"--terrain-build-version={terrain}",
        fixture=two_regions,
    )
    assert [item["state"] for item in report["sources"][:3]] == ["ready", "ready", "ready"]
    assert report["sources"][1]["version"] == forest
    assert report["sources"][2]["version"] == terrain


def test_tuscany_command_keeps_one_region_and_its_input_set(tuscany, requests) -> None:
    out = io.StringIO()
    call_command("prepare_region_inputs", "--regions=09", f"--terrain-cache-dir={tuscany['cache_dir']}", "--json", stdout=out)
    report = json.loads(out.getvalue())
    assert report["region_code"] == "09"
    assert [item["region_code"] for item in report["regions"]] == ["09"]
    assert TuscanyInputSet.objects.get().region_code == "09"


def test_input_sets_of_two_regions_share_the_same_versions(two_regions) -> None:
    values = {
        "grid_build_version": "g", "forest_build_version": "f", "terrain_build_version": "t",
        "host_tree_source_version": "h", "eligible_square_count": 1, "host_tree_row_count": 1,
    }
    TuscanyInputSet.objects.create(region_code="09", **values)
    TuscanyInputSet.objects.create(region_code="10", **values)
    with pytest.raises(IntegrityError), transaction.atomic():
        TuscanyInputSet.objects.create(region_code="10", **values)
    assert TuscanyInputSet.objects.count() == 2


def test_action_texts_name_the_region_commands(two_regions) -> None:
    two_regions["layer_path"].unlink()
    report = _report("--regions=09,10", "--dry-run", fixture=two_regions)
    forest = next(item for item in report["sources"] if item["source"] == "forest")
    assert "prepare_region_map_database" in forest["action"]
    assert "prepare_tuscany" not in json.dumps(report)
    trees_text = _run("--regions=09,10", "--dry-run", fixture=two_regions)
    assert "prepare_tuscany" not in trees_text
