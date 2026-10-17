"""Check and prepare the stored geographic inputs for estimates of one or more regions.

Grid, forest and terrain are only checked. Host-tree values are sampled once per source version.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Exists, OuterRef

from sporameteo.host_trees import checksum, prepare_sources, read_manifest, sample_squares
from national_forest import layer
from national_forest.models import ForestBuild, ForestCell, ForestChunk
from national_grid.models import GridBuild, GridBuildCell, GridRegion
from national_grid.subset import TUSCANY_ISTAT_CODE
from national_terrain.models import TerrainBuild, TerrainCell, TerrainSourceTile
from national_terrain.source import raster_path, sha256_path

from .models import HostTreeCache, TuscanyInputSet


REPOSITORY_ROOT = Path(settings.BASE_DIR).parent.parent
SKIPPED_SOURCE_FILES = "Source files were not checked."
BATCH_SIZE = 5_000
SAMPLE_LIMIT = 5
READY = "ready"
MISSING = "missing"
INCOMPLETE = "incomplete"
CORRUPT = "corrupt"
REJECTED = "rejected"
FALLBACK = "fallback"
BLOCKED = "blocked"


@dataclass(frozen=True)
class CommandNames:
    """Name the commands that the action texts tell the reader to run."""

    inputs: str
    database: str


TUSCANY_COMMANDS = CommandNames(inputs="prepare_tuscany_inputs", database="prepare_tuscany_map_database")
REGION_COMMANDS = CommandNames(inputs="prepare_region_inputs", database="prepare_region_map_database")


def _prepare_database(*, names: CommandNames) -> str:
    subset = "Tuscany subset" if names == TUSCANY_COMMANDS else "regional subset"
    return f"Run {names.database} to build the {subset} database. This command does not rebuild it."


@dataclass
class SourceStatus:
    """Describe one source: its state, version, required action and affected area."""

    source: str
    state: str
    version: str
    action: str
    detail: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.state not in {READY, FALLBACK}


@dataclass
class RegionProblem:
    """Name one source that is not ready for a region, how many squares it affects and a sample."""

    source: str
    state: str
    squares: int
    sample: str = ""


@dataclass
class RegionStatus:
    """Hold the squares of one region and the problems that keep it from being ready."""

    region_code: str
    name: str
    squares: int
    forest_squares: int
    problems: list[RegionProblem] = field(default_factory=list)

    @property
    def state(self) -> str:
        return self.problems[0].state if self.problems else READY


@dataclass
class Gap:
    """Count the squares without a value for one region and keep the first cell ids."""

    count: int = 0
    cell_ids: list[str] = field(default_factory=list)

    def add(self, *, cell_id: str) -> None:
        self.count += 1
        if len(self.cell_ids) <= SAMPLE_LIMIT:
            self.cell_ids.append(cell_id)


Gaps = dict[str, Gap]


@dataclass
class TuscanyReport:
    """Hold the inventory of all four sources, the per-region results and the preparation counts."""

    region_code: str
    dry_run: bool
    sources: list[SourceStatus]
    counts: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    region_codes: list[str] = field(default_factory=list)
    regions: list[RegionStatus] = field(default_factory=list)

    @property
    def failures(self) -> list[SourceStatus]:
        return [item for item in self.sources if item.failed]

    @property
    def ready(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "region_code": self.region_code,
            "region_codes": self.region_codes,
            "dry_run": self.dry_run,
            "ready": self.ready,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "counts": self.counts,
            "sources": [asdict(item) for item in self.sources],
            "regions": [{**asdict(item), "state": item.state} for item in self.regions],
        }


def _sample_text(*, cell_ids: list[str], total: int) -> str:
    shown = ", ".join(cell_ids[:SAMPLE_LIMIT])
    return f"{shown} and {total - SAMPLE_LIMIT} more" if total > SAMPLE_LIMIT else shown


def _sample(values: list[str]) -> list[str]:
    return [_sample_text(cell_ids=values, total=len(values))]


def _check_grid(*, forest: ForestBuild | None, region_codes: list[str], names: CommandNames) -> SourceStatus:
    if forest is None:
        return SourceStatus(
            source="administrative_geometry", state=MISSING, version="",
            action="The configured forest build is missing, so the grid cannot be identified. " + _prepare_database(names=names),
        )
    grid = forest.grid_build
    result = SourceStatus(source="administrative_geometry", state=READY, version=grid.version, action="None.")

    def fail(*, state: str, detail: str, action: str = "") -> SourceStatus:
        action = action or _prepare_database(names=names)
        result.state, result.action = state, action
        result.detail.append(detail)
        return result

    if grid.status != GridBuild.Status.COMPLETED:
        return fail(state=INCOMPLETE, detail=f"Grid build {grid.version} is {grid.status}, not completed.")
    chosen = set(region_codes)
    found = set(GridRegion.objects.filter(build=grid).values_list("istat_code", flat=True))
    if found != chosen:
        extra, missing = sorted(found - chosen), sorted(chosen - found)
        parts = [f"The grid has regions {sorted(found)}, but the chosen regions are {sorted(chosen)}."]
        if extra:
            parts.append(f"Regions not chosen: {', '.join(extra)}.")
        if missing:
            parts.append(f"Chosen regions missing from the grid: {', '.join(missing)}.")
        return fail(state=REJECTED, detail=" ".join(parts))
    memberships = GridBuildCell.objects.filter(build=grid)
    outside = memberships.exclude(owner_region__istat_code__in=chosen).count()
    if outside:
        return fail(state=REJECTED, detail=f"{outside} grid squares belong to a region that was not chosen.")
    count = memberships.count()
    result.counts["squares"] = count
    if count != grid.cell_count:
        return fail(state=INCOMPLETE, detail=f"The grid has {count} squares but its build records {grid.cell_count}.")
    return result


def _missing_rows(*, grid: GridBuild, model: type, build: Any) -> tuple[list[str], Gaps]:
    """Return the cell ids without a row in the build, and the same squares grouped by owner region."""
    # NOT EXISTS keeps an anti-join. NOT IN over a million ids rescans the list per square.
    rows = model.objects.filter(build=build, grid_build_cell_id=OuterRef("pk"))
    missing = (
        GridBuildCell.objects.filter(build=grid).exclude(Exists(rows))
        .order_by("cell_id").values_list("cell_id", "owner_region__istat_code")
    )
    cell_ids: list[str] = []
    gaps: Gaps = {}
    for cell_id, code in missing:
        cell_ids.append(cell_id)
        gaps.setdefault(code, Gap()).add(cell_id=cell_id)
    return cell_ids, gaps


def _check_forest(*, names: CommandNames, forest: ForestBuild | None, version: str, grid: SourceStatus, gaps: Gaps, check_source_files: bool = True) -> SourceStatus:
    action = f"Run {names.database}, or build_national_forest for the national build."
    if forest is None:
        return SourceStatus(
            source="forest", state=MISSING, version=version, action=action,
            detail=[f"No forest build named {version} exists."],
        )
    result = SourceStatus(source="forest", state=READY, version=forest.version, action="None.")

    def fail(*, state: str, detail: str) -> SourceStatus:
        result.state, result.action = state, action
        result.detail.append(detail)
        return result

    if forest.status != ForestBuild.Status.COMPLETED:
        return fail(state=INCOMPLETE, detail=f"Forest build {forest.version} is {forest.status}, not completed.")
    if grid.state != READY:
        return fail(state=BLOCKED, detail="The grid used by this forest build is not ready.")
    missing, found = _missing_rows(grid=forest.grid_build, model=ForestCell, build=forest)
    gaps.update(found)
    result.counts["missing_squares"] = len(missing)
    if missing:
        return fail(state=INCOMPLETE, detail=f"{len(missing)} squares have no forest row: " + _sample(missing)[0])
    if not forest.layer_path:
        return fail(state=MISSING, detail="The forest build has no layer file.")
    layer_path = layer.resolve_layer_path(value=forest.layer_path)
    if not layer_path.is_file():
        return fail(state=MISSING, detail=f"Layer file {layer_path} is missing.")
    try:
        found = layer.validate_layer(path=layer_path)["sha256"]
    except ValueError as exc:
        return fail(state=CORRUPT, detail=f"Layer file {layer_path} is damaged: {exc}")
    if found != forest.layer_sha256:
        return fail(state=CORRUPT, detail=f"Layer file {layer_path} changed since the build.")
    if not check_source_files:
        result.detail.append(SKIPPED_SOURCE_FILES)
        return result
    for chunk in ForestChunk.objects.filter(build=forest).exclude(source_path="").order_by("west_e", "south_n"):
        area = f"chunk at E{chunk.west_e} N{chunk.south_n}"
        # Forest builds store cache paths relative to the repository root.
        path = REPOSITORY_ROOT / chunk.source_path
        if not path.is_file():
            return fail(state=MISSING, detail=f"Source file {path} for {area} is missing.")
        if chunk.source_sha256 and sha256_path(path=path) != chunk.source_sha256:
            return fail(state=CORRUPT, detail=f"Source file {path} for {area} changed since the build.")
    return result


def _check_terrain(*, names: CommandNames, terrain: TerrainBuild | None, version: str, grid: SourceStatus, forest: ForestBuild | None, gaps: Gaps, cache_dir: Path, check_source_files: bool = True) -> SourceStatus:
    action = f"Run {names.database}, or build_national_terrain for the national build."
    if terrain is None:
        return SourceStatus(
            source="terrain", state=MISSING, version=version, action=action,
            detail=[f"No terrain build named {version} exists."],
        )
    result = SourceStatus(source="terrain", state=READY, version=terrain.version, action="None.")

    def fail(*, state: str, detail: str) -> SourceStatus:
        result.state, result.action = state, action
        result.detail.append(detail)
        return result

    if terrain.status != TerrainBuild.Status.COMPLETED:
        return fail(state=INCOMPLETE, detail=f"Terrain build {terrain.version} is {terrain.status}, not completed.")
    if grid.state != READY or forest is None or terrain.grid_build_id != forest.grid_build_id:
        return fail(state=BLOCKED, detail="The terrain build does not use the same ready grid as the forest build.")
    missing, found = _missing_rows(grid=terrain.grid_build, model=TerrainCell, build=terrain)
    gaps.update(found)
    result.counts["missing_squares"] = len(missing)
    if missing:
        return fail(state=INCOMPLETE, detail=f"{len(missing)} squares have no terrain row: " + _sample(missing)[0])
    if not check_source_files:
        result.detail.append(SKIPPED_SOURCE_FILES)
        return result
    tiles = TerrainSourceTile.objects.filter(
        build=terrain, status=TerrainSourceTile.Status.AVAILABLE,
    ).order_by("tile_name")
    for tile in tiles:
        path = raster_path(cache_dir=cache_dir, tile_name=tile.tile_name)
        if not path.is_file():
            return fail(state=MISSING, detail=f"Terrain tile {tile.tile_name} is missing at {path}.")
        if tile.sha256 and sha256_path(path=path) != tile.sha256:
            return fail(state=CORRUPT, detail=f"Terrain tile {tile.tile_name} at {path} changed since the build.")
    return result


def _forest_square_counts(*, forest: ForestBuild) -> dict[str, int]:
    rows = (
        ForestCell.objects.filter(build=forest, is_forest=True)
        .values_list("grid_build_cell__owner_region__istat_code").annotate(total=Count("id"))
    )
    return {code: total for code, total in rows}


def _unstored_batches(*, forest: ForestBuild, version: str) -> Iterator[list[tuple[str, float, float, str]]]:
    """Yield forest squares in cell id order, in batches, without the ones that already have host-tree values.
    Each item is the cell id, the centre east and north, and the owner region code."""
    rows = ForestCell.objects.filter(build=forest, is_forest=True).order_by("grid_build_cell__cell__cell_id").values_list(
        "grid_build_cell__cell__cell_id", "grid_build_cell__cell__corner_e", "grid_build_cell__cell__corner_n",
        "grid_build_cell__owner_region__istat_code",
    )

    def unstored(*, batch: list[tuple[str, float, float, str]]) -> list[tuple[str, float, float, str]]:
        stored = set(
            HostTreeCache.objects.filter(source_version=version, cell_id__in=[row[0] for row in batch])
            .values_list("cell_id", flat=True)
        )
        return [row for row in batch if row[0] not in stored]

    batch: list[tuple[str, float, float, str]] = []
    for row in rows.iterator(chunk_size=BATCH_SIZE):
        batch.append(row)
        if len(batch) >= BATCH_SIZE:
            if missing := unstored(batch=batch):
                yield missing
            batch = []
    if batch and (missing := unstored(batch=batch)):
        yield missing


def _check_host_trees(*, names: CommandNames, forest: ForestBuild | None, ready: bool, directory: Path, allow_forest_only: bool, gaps: Gaps) -> tuple[SourceStatus, dict | None]:
    """Return the host-tree status and the manifest if readable."""
    result = SourceStatus(source="host_trees", state=READY, version="", action="None.")
    if forest is None or not ready:
        result.state = BLOCKED
        result.action = "Fix the sources above first."
        return result, None
    result.counts["eligible_squares"] = sum(_forest_square_counts(forest=forest).values())
    problem = ""
    state = MISSING
    manifest = None
    try:
        selected = read_manifest(directory=directory)
        if selected is None:
            problem = f"No JRC manifest in {directory}."
        else:
            manifest, version = selected
            result.version = version
            for record in manifest["files"]:
                path = directory / record["filename"]
                if not path.is_file():
                    problem = f"JRC file {path} is missing."
                    break
                if checksum(path=path) != record["sha256"]:
                    state, problem = CORRUPT, f"JRC file {path} changed since it was downloaded."
                    break
    except (OSError, ValueError, KeyError) as exc:
        state, problem = CORRUPT, f"The JRC manifest in {directory} is not usable: {exc}"
    if problem:
        if allow_forest_only:
            result.state = FALLBACK
            result.action = "Estimates use forest only, without host-tree evidence. Prepare JRC files later to add it."
        else:
            result.state = state
            result.action = (
                f"Run {names.inputs} to download the JRC files." if manifest is None and state == MISSING
                else f"Restore the JRC files or prepare a new JRC directory, then run {names.inputs}."
            )
        result.detail.append(problem)
        return result, None
    missing = 0
    sample: list[str] = []
    for batch in _unstored_batches(forest=forest, version=result.version):
        for cell_id, _, _, code in batch:
            missing += 1
            gaps.setdefault(code, Gap()).add(cell_id=cell_id)
            if len(sample) <= SAMPLE_LIMIT:
                sample.append(cell_id)
    result.counts["missing_squares"] = missing
    if missing:
        result.state = INCOMPLETE
        result.action = f"Run {names.inputs} to sample the missing squares from the local JRC files."
        result.detail.append(f"{missing} squares have no host-tree values: " + _sample_text(cell_ids=sample, total=missing))
    return result, manifest


def _region_statuses(*, region_codes: list[str], forest: ForestBuild | None, sources: list[SourceStatus], gaps: dict[str, Gaps]) -> list[RegionStatus]:
    """Group the source results by region: square counts first, then one problem per failing source."""
    names: dict[str, str] = {}
    squares: dict[str, int] = {}
    forest_squares: dict[str, int] = {}
    if forest is not None:
        grid = forest.grid_build
        names = dict(GridRegion.objects.filter(build=grid, istat_code__in=region_codes).values_list("istat_code", "name"))
        squares = {
            code: total for code, total in GridBuildCell.objects.filter(build=grid)
            .values_list("owner_region__istat_code").annotate(total=Count("id"))
        }
        forest_squares = _forest_square_counts(forest=forest)
    regions = [
        RegionStatus(
            region_code=code, name=names.get(code, ""), squares=squares.get(code, 0),
            forest_squares=forest_squares.get(code, 0),
        )
        for code in region_codes
    ]
    for status in sources:
        if not status.failed or status.state == BLOCKED:
            continue
        source_gaps = gaps.get(status.source, {})
        for region in regions:
            if source_gaps:
                if gap := source_gaps.get(region.region_code):
                    region.problems.append(RegionProblem(
                        source=status.source, state=status.state, squares=gap.count,
                        sample=_sample_text(cell_ids=gap.cell_ids, total=gap.count),
                    ))
            else:
                affected = region.forest_squares if status.source == "host_trees" else region.squares
                region.problems.append(RegionProblem(source=status.source, state=status.state, squares=affected))
    for status in sources:
        if status.state != BLOCKED:
            continue
        for region in regions:
            if not region.problems:
                affected = region.forest_squares if status.source == "host_trees" else region.squares
                region.problems.append(RegionProblem(source=status.source, state=BLOCKED, squares=affected))
    return regions


def build_inventory(
    *, region_codes: list[str], forest_version: str, terrain_version: str, terrain_cache_dir: Path,
    allow_forest_only: bool = False, check_source_files: bool = True, names: CommandNames | None = None,
) -> tuple[TuscanyReport, dict | None]:
    """Check each source for the chosen regions without writing or contacting a provider.
    Also return the JRC manifest if it is usable."""
    forest = ForestBuild.objects.filter(version=forest_version).select_related("grid_build").first()
    terrain = TerrainBuild.objects.filter(version=terrain_version).first()
    gaps: dict[str, Gaps] = {"forest": {}, "terrain": {}, "host_trees": {}}
    names = names or TUSCANY_COMMANDS
    grid = _check_grid(forest=forest, region_codes=region_codes, names=names)
    forest_status = _check_forest(
        names=names,
        forest=forest, version=forest_version, grid=grid, gaps=gaps["forest"], check_source_files=check_source_files,
    )
    terrain_status = _check_terrain(
        names=names,
        terrain=terrain, version=terrain_version, grid=grid, forest=forest, gaps=gaps["terrain"],
        cache_dir=terrain_cache_dir, check_source_files=check_source_files,
    )
    trees, manifest = _check_host_trees(
        names=names,
        forest=forest, ready=grid.state == READY and forest_status.state == READY,
        directory=Path(settings.SPORAMETEO_JRC_DIRECTORY), allow_forest_only=allow_forest_only,
        gaps=gaps["host_trees"],
    )
    sources = [grid, forest_status, terrain_status, trees]
    report = TuscanyReport(
        region_code=",".join(region_codes), dry_run=True, sources=sources, region_codes=list(region_codes),
        regions=_region_statuses(region_codes=region_codes, forest=forest, sources=sources, gaps=gaps),
    )
    return report, manifest


def _store_host_trees(*, manifest: dict, version: str, forest: ForestBuild) -> None:
    """Sample and store the squares without values, one batch at a time. Stored squares are never rewritten."""
    directory = Path(settings.SPORAMETEO_JRC_DIRECTORY)
    for batch in _unstored_batches(forest=forest, version=version):
        centres = {cell_id: (east + 250.0, north + 250.0) for cell_id, east, north, _ in batch}
        sampled = sample_squares(directory=directory, manifest=manifest, version=version, centres=centres)
        HostTreeCache.objects.bulk_create(
            [HostTreeCache(source_version=version, cell_id=key, values=list(value.values)) for key, value in sampled.items()],
            ignore_conflicts=True,
        )


def all_region_codes(*, forest_version: str) -> list[str]:
    """Return the sorted codes of every region in the grid of the forest build. Return none if the build is missing."""
    forest = ForestBuild.objects.filter(version=forest_version).select_related("grid_build").first()
    if forest is None:
        return []
    return sorted(GridRegion.objects.filter(build=forest.grid_build).values_list("istat_code", flat=True))


def prepare_region_inputs(
    *, region_codes: list[str], forest_version: str, terrain_version: str, terrain_cache_dir: Path,
    dry_run: bool = False, allow_forest_only: bool = False, check_source_files: bool = True,
    names: CommandNames | None = None,
) -> TuscanyReport:
    """Report readiness and, unless dry, store host-tree values for every eligible square of the regions.
    Only a missing JRC manifest triggers a download. Older source versions stay untouched."""
    started = time.monotonic()

    def inventory() -> tuple[TuscanyReport, dict | None]:
        report, manifest = build_inventory(
            region_codes=region_codes, forest_version=forest_version, terrain_version=terrain_version,
            terrain_cache_dir=terrain_cache_dir, allow_forest_only=allow_forest_only,
            check_source_files=check_source_files, names=names,
        )
        report.dry_run = dry_run
        return report, manifest

    report, manifest = inventory()
    trees = report.sources[3]
    if not dry_run and all(item.state == READY for item in report.sources[:3]):
        directory = Path(settings.SPORAMETEO_JRC_DIRECTORY)
        if trees.state == MISSING and manifest is None and not trees.version:
            prepare_sources(directory=directory)
            report, manifest = inventory()
            trees = report.sources[3]
        if trees.state in {INCOMPLETE, READY} and manifest is not None:
            forest = ForestBuild.objects.get(version=forest_version)
            if trees.counts.get("missing_squares"):
                _store_host_trees(manifest=manifest, version=trees.version, forest=forest)
            report, _ = inventory()
            trees = report.sources[3]
            if report.ready:
                _record_provenance(report=report, manifest=manifest)
    report.counts = {
        "eligible_squares": trees.counts.get("eligible_squares", 0),
        "missing_host_tree_squares": trees.counts.get("missing_squares", 0),
        "tuscany_squares": report.sources[0].counts.get("squares", 0),
        "input_sets": TuscanyInputSet.objects.count(),
        "host_tree_rows": HostTreeCache.objects.filter(source_version=trees.version).count() if trees.version else 0,
    }
    report.elapsed_seconds = time.monotonic() - started
    return report


def prepare_tuscany_inputs(*, terrain_cache_dir: Path, dry_run: bool = False, allow_forest_only: bool = False, check_source_files: bool = True) -> TuscanyReport:
    """Run the region preparation for Tuscany with the build versions from the settings."""
    return prepare_region_inputs(
        region_codes=[TUSCANY_ISTAT_CODE], forest_version=settings.SPORAMETEO_FOREST_BUILD_VERSION,
        terrain_version=settings.SPORAMETEO_TERRAIN_BUILD_VERSION, terrain_cache_dir=terrain_cache_dir,
        dry_run=dry_run, allow_forest_only=allow_forest_only, check_source_files=check_source_files,
    )


def _record_provenance(*, report: TuscanyReport, manifest: dict) -> None:
    grid, forest, terrain, trees = report.sources
    with transaction.atomic():
        for region in report.regions:
            if region.state != READY:
                continue
            squares = ForestCell.objects.filter(
                build__version=forest.version, is_forest=True,
                grid_build_cell__owner_region__istat_code=region.region_code,
            ).values("grid_build_cell__cell__cell_id")
            TuscanyInputSet.objects.get_or_create(
                region_code=region.region_code, grid_build_version=grid.version,
                forest_build_version=forest.version, terrain_build_version=terrain.version,
                host_tree_source_version=trees.version,
                defaults={
                    "jrc_manifest": manifest,
                    "eligible_square_count": region.forest_squares,
                    "host_tree_row_count": HostTreeCache.objects.filter(
                        source_version=trees.version, cell_id__in=squares,
                    ).count(),
                },
            )
