"""Copy completed national builds for chosen regions into another database."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.apps import apps
from django.db import connections, transaction
from django.db.models import Model

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


TUSCANY_ISTAT_CODE = "09"
DEFAULT_SOURCE_ALIAS = "source"
DEFAULT_TARGET_ALIAS = "default"
COPY_BATCH_SIZE = 2_000
_APPLICATION_APPS = {
    "area_api",
    "background_jobs",
    "django_tasks_database",
    "estimate_updates",
    "map_api",
    "national_forest",
    "national_grid",
    "national_terrain",
    "weather_on_demand",
    "weather_requests",
    "weather_summaries",
    "web_ui",
}


class TuscanySubsetError(RuntimeError):
    """Report an unsafe or incomplete region subset copy."""


RegionSubsetError = TuscanySubsetError


class DestinationNotEmptyError(TuscanySubsetError):
    """Report that a target database already contains application rows."""


class SourceBuildError(TuscanySubsetError):
    """Report missing or unfinished source builds."""


@dataclass(frozen=True)
class RegionSubsetResult:
    """Return copied counts and the generated layer artifact for a list of regions."""

    source_grid_version: str
    source_forest_version: str
    source_terrain_version: str
    grid_version: str
    forest_version: str
    terrain_version: str
    region_codes: list[str]
    region_names: dict[str, str]
    region_squares: dict[str, int]
    grid_cells_reused: int
    counts: dict[str, int]
    layer_path: Path | None
    layer_sha256: str | None
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-safe preparation details."""
        return {
            "source_grid_version": self.source_grid_version,
            "source_forest_version": self.source_forest_version,
            "source_terrain_version": self.source_terrain_version,
            "grid_version": self.grid_version,
            "forest_version": self.forest_version,
            "terrain_version": self.terrain_version,
            "region_codes": list(self.region_codes),
            "region_names": dict(self.region_names),
            "region_squares": dict(self.region_squares),
            "grid_cells_reused": self.grid_cells_reused,
            "counts": dict(self.counts),
            "dry_run": self.dry_run,
            "layer_path": None if self.layer_path is None else str(self.layer_path),
            "layer_sha256": self.layer_sha256,
        }


@dataclass(frozen=True)
class TuscanySubsetResult:
    """Return copied counts and the generated layer artifact."""

    source_grid_version: str
    source_forest_version: str
    source_terrain_version: str
    grid_version: str
    forest_version: str
    terrain_version: str
    region_code: str
    region_name: str
    counts: dict[str, int]
    layer_path: Path
    layer_sha256: str

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-safe preparation details."""
        return {
            "source_grid_version": self.source_grid_version,
            "source_forest_version": self.source_forest_version,
            "source_terrain_version": self.source_terrain_version,
            "grid_version": self.grid_version,
            "forest_version": self.forest_version,
            "terrain_version": self.terrain_version,
            "region_code": self.region_code,
            "region_name": self.region_name,
            "counts": dict(self.counts),
            "layer_path": str(self.layer_path),
            "layer_sha256": self.layer_sha256,
        }


def prepare_tuscany_subset(
    *,
    source_alias: str,
    target_alias: str,
    source_grid_version: str,
    source_forest_version: str,
    source_terrain_version: str,
    grid_version: str,
    forest_version: str,
    terrain_version: str,
    output_dir: Path,
    region_code: str = TUSCANY_ISTAT_CODE,
) -> TuscanySubsetResult:
    """Copy completed static records for one Italian region into an empty database.

    Exclude weather and estimates so the normal worker can populate them later.
    """
    result = _prepare_subset(
        source_alias=source_alias,
        target_alias=target_alias,
        source_grid_version=source_grid_version,
        source_forest_version=source_forest_version,
        source_terrain_version=source_terrain_version,
        grid_version=grid_version,
        forest_version=forest_version,
        terrain_version=terrain_version,
        output_dir=output_dir,
        region_codes=[region_code],
        allow_non_empty_target=False,
        dry_run=False,
    )
    assert result.layer_path is not None and result.layer_sha256 is not None
    return TuscanySubsetResult(
        source_grid_version=result.source_grid_version,
        source_forest_version=result.source_forest_version,
        source_terrain_version=result.source_terrain_version,
        grid_version=result.grid_version,
        forest_version=result.forest_version,
        terrain_version=result.terrain_version,
        region_code=region_code,
        region_name=result.region_names[region_code],
        counts=result.counts,
        layer_path=result.layer_path,
        layer_sha256=result.layer_sha256,
    )


def prepare_region_subset(
    *,
    source_alias: str,
    target_alias: str,
    source_grid_version: str,
    source_forest_version: str,
    source_terrain_version: str,
    grid_version: str,
    forest_version: str,
    terrain_version: str,
    output_dir: Path,
    region_codes: list[str],
    dry_run: bool = False,
) -> RegionSubsetResult:
    """Add the static records of several regions to a database that already holds data.

    Never change an existing row. Grid cells that the target already holds are reused.
    """
    return _prepare_subset(
        source_alias=source_alias,
        target_alias=target_alias,
        source_grid_version=source_grid_version,
        source_forest_version=source_forest_version,
        source_terrain_version=source_terrain_version,
        grid_version=grid_version,
        forest_version=forest_version,
        terrain_version=terrain_version,
        output_dir=output_dir,
        region_codes=region_codes,
        allow_non_empty_target=True,
        dry_run=dry_run,
    )


def _prepare_subset(
    *,
    source_alias: str,
    target_alias: str,
    source_grid_version: str,
    source_forest_version: str,
    source_terrain_version: str,
    grid_version: str,
    forest_version: str,
    terrain_version: str,
    output_dir: Path,
    region_codes: list[str],
    allow_non_empty_target: bool,
    dry_run: bool,
) -> RegionSubsetResult:
    _ensure_distinct_databases(source_alias=source_alias, target_alias=target_alias)
    if len({grid_version, forest_version, terrain_version}) != 3:
        raise TuscanySubsetError("Subset grid, forest and terrain versions must be distinct.")
    if allow_non_empty_target:
        _ensure_new_versions(
            alias=target_alias,
            grid_version=grid_version,
            forest_version=forest_version,
            terrain_version=terrain_version,
        )
    else:
        _ensure_empty_target(alias=target_alias)
    source_grid, source_forest, source_terrain, source_regions = _load_source_builds(
        alias=source_alias,
        source_grid_version=source_grid_version,
        source_forest_version=source_forest_version,
        source_terrain_version=source_terrain_version,
        region_codes=region_codes,
    )
    source_region_ids = [region.id for region in source_regions]
    memberships = _source_membership_keys(
        alias=source_alias,
        build_id=source_grid.id,
        region_ids=source_region_ids,
    )
    code_by_region_id = {region.id: region.istat_code for region in source_regions}
    region_squares = {region.istat_code: 0 for region in source_regions}
    for row in memberships:
        region_squares[code_by_region_id[row["owner_region_id"]]] += 1
    _validate_habitat_memberships(
        alias=source_alias,
        model=ForestCell,
        build_id=source_forest.id,
        grid_build_id=source_grid.id,
        expected_membership_ids={row["id"] for row in memberships},
        label="forest",
        owner_region_ids=source_region_ids,
    )
    _validate_habitat_memberships(
        alias=source_alias,
        model=TerrainCell,
        build_id=source_terrain.id,
        grid_build_id=source_grid.id,
        expected_membership_ids={row["id"] for row in memberships},
        label="terrain",
        owner_region_ids=source_region_ids,
    )
    cell_ids = {row["cell_id"] for row in memberships}
    source_chunk_ids = {row["chunk_id"] for row in memberships}
    region_names = {region.istat_code: region.name for region in source_regions}
    if len(region_codes) == 1 and not allow_non_empty_target:
        label: dict[str, Any] = {
            "subset": "tuscany",
            "subset_region_code": region_codes[0],
            "subset_region_name": region_names[region_codes[0]],
        }
    else:
        label = {"subset": "regions", "region_codes": list(region_codes)}
    fingerprint_key = ",".join(region_codes)
    result_fields: dict[str, Any] = {
        "source_grid_version": source_grid.version,
        "source_forest_version": source_forest.version,
        "source_terrain_version": source_terrain.version,
        "grid_version": grid_version,
        "forest_version": forest_version,
        "terrain_version": terrain_version,
        "region_codes": list(region_codes),
        "region_names": region_names,
        "region_squares": region_squares,
    }

    if dry_run:
        grid_cells_reused = _copy_cells(
            alias=target_alias, source_alias=source_alias, cell_ids=cell_ids, write=False
        )
        forest_cells = ForestCell.objects.using(source_alias).filter(
            build=source_forest, grid_build_cell__owner_region_id__in=source_region_ids
        )
        return RegionSubsetResult(
            **result_fields,
            grid_cells_reused=grid_cells_reused,
            counts={
                "grid_regions": len(source_regions),
                "grid_chunks": len(source_chunk_ids),
                "grid_cells": len(memberships),
                "grid_cells_to_create": len(cell_ids) - grid_cells_reused,
                "forest_chunks": ForestChunk.objects.using(source_alias)
                .filter(build=source_forest, grid_chunk_id__in=source_chunk_ids)
                .count(),
                "forest_cells": forest_cells.count(),
                "forest_cells_with_forest": forest_cells.filter(is_forest=True).count(),
                "terrain_chunks": TerrainChunk.objects.using(source_alias)
                .filter(build=source_terrain, grid_chunk_id__in=source_chunk_ids)
                .count(),
                "terrain_cells": TerrainCell.objects.using(source_alias)
                .filter(
                    build=source_terrain, grid_build_cell__owner_region_id__in=source_region_ids
                )
                .count(),
                "estimate_updates": 0,
                "weather_rows": 0,
            },
            layer_path=None,
            layer_sha256=None,
            dry_run=True,
        )

    output_path = Path(output_dir) / forest_version / "forest_layer_2000m.geojson.gz"
    layer_written = False
    try:
        with transaction.atomic(using=target_alias):
            grid_cells_reused = _copy_cells(
                alias=target_alias, source_alias=source_alias, cell_ids=cell_ids, write=True
            )
            target_grid = _copy_grid_build(
                source=source_grid,
                alias=target_alias,
                version=grid_version,
                label=label,
                fingerprint_key=fingerprint_key,
            )
            region_id_map = _copy_regions(
                sources=source_regions, target_build=target_grid, alias=target_alias
            )
            grid_chunk_map = _copy_grid_chunks(
                alias=target_alias,
                source_alias=source_alias,
                source_build_id=source_grid.id,
                target_build=target_grid,
                chunk_ids=source_chunk_ids,
                membership_rows=memberships,
            )
            membership_id_map = _copy_memberships(
                alias=target_alias,
                source_alias=source_alias,
                source_build_id=source_grid.id,
                target_build=target_grid,
                memberships=memberships,
                grid_chunk_map=grid_chunk_map,
                region_id_map=region_id_map,
            )
            _finish_grid_build(
                build=target_grid,
                alias=target_alias,
                chunk_count=len(grid_chunk_map),
                cell_count=len(membership_id_map),
            )

            target_forest = _copy_forest_build(
                source=source_forest,
                alias=target_alias,
                version=forest_version,
                target_grid=target_grid,
                label=label,
                fingerprint_key=fingerprint_key,
            )
            forest_chunk_map = _copy_forest_chunks(
                alias=target_alias,
                source_alias=source_alias,
                source_build_id=source_forest.id,
                target_build=target_forest,
                grid_chunk_map=grid_chunk_map,
            )
            forest_cell_count, forest_count, weather_count, forest_gap_count = _copy_forest_cells(
                alias=target_alias,
                source_alias=source_alias,
                source_build_id=source_forest.id,
                target_build=target_forest,
                membership_id_map=membership_id_map,
                forest_chunk_map=forest_chunk_map,
                source_region_ids=source_region_ids,
            )

            target_terrain = _copy_terrain_build(
                source=source_terrain,
                alias=target_alias,
                version=terrain_version,
                target_grid=target_grid,
                label=label,
                fingerprint_key=fingerprint_key,
            )
            terrain_chunk_map = _copy_terrain_chunks(
                alias=target_alias,
                source_alias=source_alias,
                source_build_id=source_terrain.id,
                target_build=target_terrain,
                grid_chunk_map=grid_chunk_map,
            )
            terrain_tile_map = _copy_terrain_tiles(
                alias=target_alias,
                source_alias=source_alias,
                source_build_id=source_terrain.id,
                target_build=target_terrain,
                terrain_chunk_map=terrain_chunk_map,
            )
            _copy_terrain_chunk_sources(
                alias=target_alias,
                source_alias=source_alias,
                terrain_chunk_map=terrain_chunk_map,
                terrain_tile_map=terrain_tile_map,
            )
            terrain_cell_count, terrain_gap_count = _copy_terrain_cells(
                alias=target_alias,
                source_alias=source_alias,
                source_build_id=source_terrain.id,
                target_build=target_terrain,
                membership_id_map=membership_id_map,
                terrain_chunk_map=terrain_chunk_map,
                source_region_ids=source_region_ids,
            )
            target_membership_ids = set(membership_id_map.values())
            _validate_habitat_memberships(
                alias=target_alias,
                model=ForestCell,
                build_id=target_forest.id,
                grid_build_id=target_grid.id,
                expected_membership_ids=target_membership_ids,
                label="target forest",
            )
            _validate_habitat_memberships(
                alias=target_alias,
                model=TerrainCell,
                build_id=target_terrain.id,
                grid_build_id=target_grid.id,
                expected_membership_ids=target_membership_ids,
                label="target terrain",
            )

            artifact = layer.write_forest_layer(
                rows=layer.rows_from_cells(
                    cells=ForestCell.objects.using(target_alias).filter(build=target_forest)
                ),
                output_path=output_path,
            )
            layer_written = True
            _finish_forest_build(
                build=target_forest,
                alias=target_alias,
                cell_count=forest_cell_count,
                forest_count=forest_count,
                weather_count=weather_count,
                gap_count=forest_gap_count,
                chunk_count=len(forest_chunk_map),
                layer_path=artifact.path,
                layer_sha256=artifact.sha256,
                layer_raw_bytes=artifact.raw_bytes,
                layer_gzip_bytes=artifact.gzip_bytes,
            )
            _finish_terrain_build(
                build=target_terrain,
                alias=target_alias,
                cell_count=terrain_cell_count,
                gap_count=terrain_gap_count,
                chunk_count=len(terrain_chunk_map),
            )
    except BaseException:
        # The layer file is written before the transaction commits. Remove it so a
        # rolled back copy does not leave a file that no build row points to.
        if layer_written:
            _remove_layer_file(path=output_path)
        raise

    return RegionSubsetResult(
        **result_fields,
        grid_cells_reused=grid_cells_reused,
        counts={
            "grid_regions": len(region_id_map),
            "grid_chunks": len(grid_chunk_map),
            "grid_cells": len(membership_id_map),
            "forest_chunks": len(forest_chunk_map),
            "forest_cells": forest_cell_count,
            "forest_cells_with_forest": forest_count,
            "forest_weather_cells": weather_count,
            "forest_source_gaps": forest_gap_count,
            "terrain_chunks": len(terrain_chunk_map),
            "terrain_cells": terrain_cell_count,
            "terrain_source_tiles": len(terrain_tile_map),
            "terrain_source_gaps": terrain_gap_count,
            "estimate_updates": 0,
            "weather_rows": 0,
        },
        layer_path=artifact.path,
        layer_sha256=artifact.sha256,
    )


def _remove_layer_file(*, path: Path) -> None:
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _ensure_new_versions(
    *, alias: str, grid_version: str, forest_version: str, terrain_version: str
) -> None:
    for model, version in (
        (GridBuild, grid_version),
        (ForestBuild, forest_version),
        (TerrainBuild, terrain_version),
    ):
        if model.objects.using(alias).filter(version=version).exists():
            raise TuscanySubsetError(
                f"Target database already has a {model._meta.label} with version {version!r}."
            )


def _ensure_distinct_databases(*, source_alias: str, target_alias: str) -> None:
    if source_alias == target_alias:
        raise TuscanySubsetError("Source and target database aliases must be different.")
    source = connections[source_alias].settings_dict
    target = connections[target_alias].settings_dict
    keys = ("ENGINE", "NAME", "HOST", "PORT", "USER")
    if all(source.get(key) == target.get(key) for key in keys):
        raise TuscanySubsetError("Source and target point to the same database.")


def _ensure_empty_target(*, alias: str) -> None:
    for model in apps.get_models():
        if model._meta.app_label not in _APPLICATION_APPS or model._meta.proxy:
            continue
        if model.objects.using(alias).exists():
            raise DestinationNotEmptyError(
                f"Target database contains rows in {model._meta.label}."
            )


def _load_source_builds(
    *,
    alias: str,
    source_grid_version: str,
    source_forest_version: str,
    source_terrain_version: str,
    region_codes: list[str],
) -> tuple[GridBuild, ForestBuild, TerrainBuild, list[GridRegion]]:
    try:
        grid = GridBuild.objects.using(alias).get(version=source_grid_version)
        forest = ForestBuild.objects.using(alias).get(version=source_forest_version)
        terrain = TerrainBuild.objects.using(alias).get(version=source_terrain_version)
    except (GridBuild.DoesNotExist, ForestBuild.DoesNotExist, TerrainBuild.DoesNotExist) as exc:
        raise SourceBuildError("Requested source build does not exist.") from exc
    if grid.status != GridBuild.Status.COMPLETED:
        raise SourceBuildError(f"Source grid build {grid.version!r} is not completed.")
    if forest.status != ForestBuild.Status.COMPLETED:
        raise SourceBuildError(f"Source forest build {forest.version!r} is not completed.")
    if terrain.status != TerrainBuild.Status.COMPLETED:
        raise SourceBuildError(f"Source terrain build {terrain.version!r} is not completed.")
    if forest.grid_build_id != grid.id or terrain.grid_build_id != grid.id:
        raise SourceBuildError("Source forest and terrain builds do not use the source grid build.")
    if not region_codes:
        raise SourceBuildError("At least one region code is required.")
    duplicates = sorted({code for code in region_codes if region_codes.count(code) > 1})
    if duplicates:
        raise SourceBuildError(f"Region codes are listed more than once: {duplicates}.")
    found = {
        region.istat_code: region
        for region in GridRegion.objects.using(alias).filter(
            build=grid, istat_code__in=region_codes
        )
    }
    for code in region_codes:
        if code not in found:
            raise SourceBuildError(f"Source grid has no region with ISTAT code {code!r}.")
    return grid, forest, terrain, [found[code] for code in region_codes]


def _source_membership_keys(
    *,
    alias: str,
    build_id: Any,
    region_id: Any = None,
    region_ids: list[Any] | None = None,
) -> list[dict[str, Any]]:
    selected = list(region_ids or [])
    if region_id is not None:
        selected.append(region_id)
    return list(
        GridBuildCell.objects.using(alias)
        .filter(build_id=build_id, owner_region_id__in=selected)
        .order_by("id")
        .values("id", "cell_id", "chunk_id", "owner_region_id")
    )


def _validate_habitat_memberships(
    *,
    alias: str,
    model: type[Model],
    build_id: Any,
    grid_build_id: Any,
    expected_membership_ids: set[Any],
    label: str,
    owner_region_ids: list[Any] | None = None,
) -> None:
    queryset = model.objects.using(alias).filter(build_id=build_id)
    if queryset.exclude(grid_build_cell__build_id=grid_build_id).exists():
        raise SourceBuildError(f"The {label} rows contain a membership from another grid build.")
    if owner_region_ids is not None:
        queryset = queryset.filter(grid_build_cell__owner_region_id__in=owner_region_ids)
    actual_membership_ids = set(queryset.values_list("grid_build_cell_id", flat=True))
    if actual_membership_ids != expected_membership_ids:
        missing = len(expected_membership_ids - actual_membership_ids)
        extra = len(actual_membership_ids - expected_membership_ids)
        raise SourceBuildError(
            f"The {label} rows do not exactly cover the selected grid memberships "
            f"(missing={missing}, extra={extra})."
        )


def _copy_fields(instance: Model, *, excluded: set[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in instance._meta.concrete_fields:
        if field.name in excluded or field.primary_key or field.auto_created:
            continue
        if getattr(field, "auto_now_add", False) or getattr(field, "auto_now", False):
            continue
        value = getattr(instance, field.attname)
        values[field.name] = copy.deepcopy(value)
    return values


def _copy_grid_build(
    *, source: GridBuild, alias: str, version: str, label: dict[str, Any], fingerprint_key: str
) -> GridBuild:
    metadata = copy.deepcopy(source.source_metadata)
    metadata.update(
        {
            **label,
            "source_build_version": source.version,
            "source_build_id": str(source.id),
        }
    )
    return GridBuild.objects.using(alias).create(
        **_copy_fields(
            source,
            excluded={
                "version",
                "fingerprint",
                "status",
                "source_metadata",
                "planned_chunk_count",
                "completed_chunk_count",
                "cell_count",
                "started_at",
                "completed_at",
            },
        ),
        version=version,
        fingerprint=_subset_fingerprint(source.fingerprint, version, fingerprint_key),
        status=GridBuild.Status.RUNNING,
        source_metadata=metadata,
        planned_chunk_count=0,
        completed_chunk_count=0,
        cell_count=0,
        started_at=source.started_at,
        completed_at=source.completed_at,
    )


def _copy_regions(
    *, sources: list[GridRegion], target_build: GridBuild, alias: str
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for source in sources:
        target = GridRegion.objects.using(alias).create(
            build=target_build,
            istat_code=source.istat_code,
            name=source.name,
            geometry=copy.deepcopy(source.geometry),
        )
        result[source.id] = target.id
    return result


def _copy_cells(*, alias: str, source_alias: str, cell_ids: set[str], write: bool) -> int:
    """Create the cells the target lacks and return how many it already held.

    Raise if an existing cell has other coordinates than the source.
    """
    if not cell_ids:
        raise SourceBuildError("The selected regions have no grid memberships.")
    ordered = sorted(cell_ids)
    reused = 0
    for start in range(0, len(ordered), COPY_BATCH_SIZE):
        batch_ids = ordered[start : start + COPY_BATCH_SIZE]
        existing = {
            cell_id: (corner_e, corner_n)
            for cell_id, corner_e, corner_n in GridCell.objects.using(alias)
            .filter(cell_id__in=batch_ids)
            .values_list("cell_id", "corner_e", "corner_n")
        }
        batch: list[GridCell] = []
        for source in GridCell.objects.using(source_alias).filter(cell_id__in=batch_ids):
            if source.cell_id in existing:
                if existing[source.cell_id] != (source.corner_e, source.corner_n):
                    raise TuscanySubsetError(
                        f"Target cell {source.cell_id!r} has other coordinates than the source."
                    )
                reused += 1
                continue
            batch.append(
                GridCell(
                    cell_id=source.cell_id,
                    corner_e=source.corner_e,
                    corner_n=source.corner_n,
                    geometry=copy.deepcopy(source.geometry),
                )
            )
        if write and batch:
            GridCell.objects.using(alias).bulk_create(batch)
    return reused


def _copy_grid_chunks(
    *,
    alias: str,
    source_alias: str,
    source_build_id: Any,
    target_build: GridBuild,
    chunk_ids: set[Any],
    membership_rows: list[dict[str, Any]],
) -> dict[Any, Any]:
    counts: dict[Any, int] = {}
    for row in membership_rows:
        counts[row["chunk_id"]] = counts.get(row["chunk_id"], 0) + 1
    result: dict[Any, Any] = {}
    for source in (
        GridChunk.objects.using(source_alias)
        .filter(build_id=source_build_id, id__in=chunk_ids)
        .order_by("south_n", "west_e")
    ):
        target = GridChunk.objects.using(alias).create(
            build=target_build,
            west_e=source.west_e,
            south_n=source.south_n,
            east_e=source.east_e,
            north_n=source.north_n,
            status=source.status,
            cell_count=counts.get(source.id, 0),
            started_at=source.started_at,
            completed_at=source.completed_at,
        )
        result[source.id] = target.id
    if set(result) != chunk_ids:
        raise SourceBuildError("The source grid has a missing chunk for a selected membership.")
    return result


def _copy_memberships(
    *,
    alias: str,
    source_alias: str,
    source_build_id: Any,
    target_build: GridBuild,
    memberships: list[dict[str, Any]],
    grid_chunk_map: dict[Any, Any],
    region_id_map: dict[Any, Any],
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    batch: list[GridBuildCell] = []
    batch_source_ids: list[Any] = []
    for source in (
        GridBuildCell.objects.using(source_alias)
        .filter(build_id=source_build_id, owner_region_id__in=list(region_id_map))
        .order_by("id")
        .iterator(chunk_size=COPY_BATCH_SIZE)
    ):
        batch.append(
            GridBuildCell(
                build=target_build,
                cell_id=source.cell_id,
                chunk_id=grid_chunk_map[source.chunk_id],
                owner_region_id=region_id_map[source.owner_region_id],
                land_geometry=copy.deepcopy(source.land_geometry),
                land_share=source.land_share,
            )
        )
        batch_source_ids.append(source.id)
        if len(batch) >= COPY_BATCH_SIZE:
            created = GridBuildCell.objects.using(alias).bulk_create(batch)
            result.update(zip(batch_source_ids, (item.id for item in created), strict=True))
            batch.clear()
            batch_source_ids.clear()
    if batch:
        created = GridBuildCell.objects.using(alias).bulk_create(batch)
        result.update(zip(batch_source_ids, (item.id for item in created), strict=True))
    if len(result) != len(memberships):
        raise SourceBuildError("The source membership copy did not preserve every selected cell.")
    return result


def _finish_grid_build(*, build: GridBuild, alias: str, chunk_count: int, cell_count: int) -> None:
    GridBuild.objects.using(alias).filter(id=build.id).update(
        planned_chunk_count=chunk_count,
        completed_chunk_count=chunk_count,
        cell_count=cell_count,
        status=GridBuild.Status.COMPLETED,
        completed_at=build.completed_at,
    )


def _copy_forest_build(
    *,
    source: ForestBuild,
    alias: str,
    version: str,
    target_grid: GridBuild,
    label: dict[str, Any],
    fingerprint_key: str,
) -> ForestBuild:
    metadata = copy.deepcopy(source.source_metadata)
    metadata.update(
        {
            **label,
            "source_build_version": source.version,
            "source_build_id": str(source.id),
        }
    )
    return ForestBuild.objects.using(alias).create(
        **_copy_fields(
            source,
            excluded={
                "version",
                "grid_build",
                "fingerprint",
                "status",
                "source_metadata",
                "planned_chunk_count",
                "completed_chunk_count",
                "failed_chunk_count",
                "cell_count",
                "forest_cell_count",
                "weather_cell_count",
                "source_gap_count",
                "started_at",
                "layer_path",
                "layer_sha256",
                "layer_gzip_bytes",
                "layer_raw_bytes",
                "completed_at",
            },
        ),
        version=version,
        grid_build=target_grid,
        fingerprint=_subset_fingerprint(source.fingerprint, version, fingerprint_key),
        status=ForestBuild.Status.RUNNING,
        source_metadata=metadata,
        planned_chunk_count=0,
        completed_chunk_count=0,
        failed_chunk_count=0,
        cell_count=0,
        forest_cell_count=0,
        weather_cell_count=0,
        layer_path="",
        layer_sha256="",
        layer_gzip_bytes=None,
        layer_raw_bytes=None,
        started_at=source.started_at,
        completed_at=source.completed_at,
    )


def _copy_forest_chunks(
    *,
    alias: str,
    source_alias: str,
    source_build_id: Any,
    target_build: ForestBuild,
    grid_chunk_map: dict[Any, Any],
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for source in (
        ForestChunk.objects.using(source_alias)
        .filter(build_id=source_build_id, grid_chunk_id__in=set(grid_chunk_map))
        .order_by("south_n", "west_e")
    ):
        values = _copy_fields(source, excluded={"build", "grid_chunk"})
        target = ForestChunk.objects.using(alias).create(
            **values,
            build=target_build,
            grid_chunk_id=grid_chunk_map[source.grid_chunk_id],
        )
        result[source.id] = target.id
    return result


def _copy_forest_cells(
    *,
    alias: str,
    source_alias: str,
    source_build_id: Any,
    target_build: ForestBuild,
    membership_id_map: dict[Any, Any],
    forest_chunk_map: dict[Any, Any],
    source_region_ids: list[Any],
) -> tuple[int, int, int, int]:
    count = forest_count = weather_count = gap_count = 0
    chunk_counts: dict[Any, int] = {}
    weather_keys: set[tuple[float, float]] = set()
    batch: list[ForestCell] = []
    for source in (
        ForestCell.objects.using(source_alias)
        .filter(
            build_id=source_build_id, grid_build_cell__owner_region_id__in=source_region_ids
        )
        .order_by("id")
        .iterator(chunk_size=COPY_BATCH_SIZE)
    ):
        values = _copy_fields(source, excluded={"build", "grid_build_cell", "chunk"})
        batch.append(
            ForestCell(
                **values,
                build=target_build,
                grid_build_cell_id=membership_id_map[source.grid_build_cell_id],
                chunk_id=forest_chunk_map[source.chunk_id],
            )
        )
        count += 1
        chunk_counts[source.chunk_id] = chunk_counts.get(source.chunk_id, 0) + 1
        forest_count += int(source.is_forest is True)
        gap_count += int(source.valid_coverage < 1.0)
        if source.weather_latitude is not None and source.weather_longitude is not None:
            weather_keys.add((source.weather_latitude, source.weather_longitude))
        if len(batch) >= COPY_BATCH_SIZE:
            ForestCell.objects.using(alias).bulk_create(batch)
            batch.clear()
    if batch:
        ForestCell.objects.using(alias).bulk_create(batch)
    for chunk_id, chunk_count in chunk_counts.items():
        ForestChunk.objects.using(alias).filter(id=forest_chunk_map[chunk_id]).update(
            cell_count=chunk_count
        )
    weather_count = len(weather_keys)
    return count, forest_count, weather_count, gap_count


def _finish_forest_build(
    *,
    build: ForestBuild,
    alias: str,
    cell_count: int,
    forest_count: int,
    weather_count: int,
    gap_count: int,
    chunk_count: int,
    layer_path: Path,
    layer_sha256: str,
    layer_raw_bytes: int,
    layer_gzip_bytes: int,
) -> None:
    ForestBuild.objects.using(alias).filter(id=build.id).update(
        planned_chunk_count=chunk_count,
        completed_chunk_count=chunk_count,
        cell_count=cell_count,
        forest_cell_count=forest_count,
        weather_cell_count=weather_count,
        source_gap_count=gap_count,
        layer_path=layer.stored_layer_path(path=layer_path),
        layer_sha256=layer_sha256,
        layer_raw_bytes=layer_raw_bytes,
        layer_gzip_bytes=layer_gzip_bytes,
        status=ForestBuild.Status.COMPLETED,
        completed_at=build.completed_at,
    )


def _copy_terrain_build(
    *,
    source: TerrainBuild,
    alias: str,
    version: str,
    target_grid: GridBuild,
    label: dict[str, Any],
    fingerprint_key: str,
) -> TerrainBuild:
    metadata = copy.deepcopy(source.source_metadata)
    metadata.update(
        {
            **label,
            "source_build_version": source.version,
            "source_build_id": str(source.id),
        }
    )
    return TerrainBuild.objects.using(alias).create(
        **_copy_fields(
            source,
            excluded={
                "version",
                "grid_build",
                "fingerprint",
                "grid_fingerprint",
                "status",
                "source_metadata",
                "planned_chunk_count",
                "completed_chunk_count",
                "result_count",
                "source_gap_count",
                "started_at",
                "completed_at",
            },
        ),
        version=version,
        grid_build=target_grid,
        fingerprint=_subset_fingerprint(source.fingerprint, version, fingerprint_key),
        grid_fingerprint=target_grid.fingerprint,
        status=TerrainBuild.Status.RUNNING,
        source_metadata=metadata,
        planned_chunk_count=0,
        completed_chunk_count=0,
        result_count=0,
        source_gap_count=0,
        started_at=source.started_at,
        completed_at=source.completed_at,
    )


def _copy_terrain_chunks(
    *,
    alias: str,
    source_alias: str,
    source_build_id: Any,
    target_build: TerrainBuild,
    grid_chunk_map: dict[Any, Any],
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for source in (
        TerrainChunk.objects.using(source_alias)
        .filter(build_id=source_build_id, grid_chunk_id__in=set(grid_chunk_map))
        .order_by("id")
    ):
        values = _copy_fields(source, excluded={"build", "grid_chunk"})
        target = TerrainChunk.objects.using(alias).create(
            **values,
            build=target_build,
            grid_chunk_id=grid_chunk_map[source.grid_chunk_id],
        )
        result[source.id] = target.id
    return result


def _copy_terrain_tiles(
    *,
    alias: str,
    source_alias: str,
    source_build_id: Any,
    target_build: TerrainBuild,
    terrain_chunk_map: dict[Any, Any],
) -> dict[Any, Any]:
    source_tile_ids = set(
        TerrainChunkSource.objects.using(source_alias)
        .filter(chunk_id__in=set(terrain_chunk_map))
        .values_list("tile_id", flat=True)
    )
    result: dict[Any, Any] = {}
    for source in (
        TerrainSourceTile.objects.using(source_alias)
        .filter(build_id=source_build_id, id__in=source_tile_ids)
        .order_by("tile_name")
    ):
        values = _copy_fields(source, excluded={"build"})
        target = TerrainSourceTile.objects.using(alias).create(**values, build=target_build)
        result[source.id] = target.id
    if source_tile_ids != set(result):
        raise SourceBuildError("The terrain source tile links are incomplete.")
    return result


def _copy_terrain_chunk_sources(
    *,
    alias: str,
    source_alias: str,
    terrain_chunk_map: dict[Any, Any],
    terrain_tile_map: dict[Any, Any],
) -> None:
    batch: list[TerrainChunkSource] = []
    for source in (
        TerrainChunkSource.objects.using(source_alias)
        .filter(chunk_id__in=set(terrain_chunk_map))
        .order_by("id")
        .iterator(chunk_size=COPY_BATCH_SIZE)
    ):
        if source.tile_id not in terrain_tile_map:
            raise SourceBuildError("The terrain source tile link is outside the selected build.")
        batch.append(
            TerrainChunkSource(
                chunk_id=terrain_chunk_map[source.chunk_id],
                tile_id=terrain_tile_map[source.tile_id],
            )
        )
        if len(batch) >= COPY_BATCH_SIZE:
            TerrainChunkSource.objects.using(alias).bulk_create(batch)
            batch.clear()
    if batch:
        TerrainChunkSource.objects.using(alias).bulk_create(batch)


def _copy_terrain_cells(
    *,
    alias: str,
    source_alias: str,
    source_build_id: Any,
    target_build: TerrainBuild,
    membership_id_map: dict[Any, Any],
    terrain_chunk_map: dict[Any, Any],
    source_region_ids: list[Any],
) -> tuple[int, int]:
    count = gap_count = 0
    chunk_counts: dict[Any, int] = {}
    batch: list[TerrainCell] = []
    for source in (
        TerrainCell.objects.using(source_alias)
        .filter(
            build_id=source_build_id, grid_build_cell__owner_region_id__in=source_region_ids
        )
        .order_by("id")
        .iterator(chunk_size=COPY_BATCH_SIZE)
    ):
        values = _copy_fields(source, excluded={"build", "grid_build_cell", "chunk"})
        batch.append(
            TerrainCell(
                **values,
                build=target_build,
                grid_build_cell_id=membership_id_map[source.grid_build_cell_id],
                chunk_id=terrain_chunk_map[source.chunk_id],
            )
        )
        count += 1
        chunk_counts[source.chunk_id] = chunk_counts.get(source.chunk_id, 0) + 1
        gap_count += int(source.source_gap)
        if len(batch) >= COPY_BATCH_SIZE:
            TerrainCell.objects.using(alias).bulk_create(batch)
            batch.clear()
    if batch:
        TerrainCell.objects.using(alias).bulk_create(batch)
    for chunk_id, chunk_count in chunk_counts.items():
        TerrainChunk.objects.using(alias).filter(id=terrain_chunk_map[chunk_id]).update(
            result_count=chunk_count
        )
    return count, gap_count


def _finish_terrain_build(
    *, build: TerrainBuild, alias: str, cell_count: int, gap_count: int, chunk_count: int
) -> None:
    TerrainBuild.objects.using(alias).filter(id=build.id).update(
        planned_chunk_count=chunk_count,
        completed_chunk_count=chunk_count,
        result_count=cell_count,
        source_gap_count=gap_count,
        status=TerrainBuild.Status.COMPLETED,
        completed_at=build.completed_at,
    )


def _subset_fingerprint(source_fingerprint: str, version: str, region_code: str) -> str:
    payload = {
        "source_fingerprint": source_fingerprint,
        "subset_version": version,
        "region_code": region_code,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
