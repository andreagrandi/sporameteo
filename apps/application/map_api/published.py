"""Read the published runs of every region for the map and keep derived map data in a shared cache.

Page requests only read stored rows. They never fetch weather, download habitat data or
calculate estimates. Square lists, run results and finished response bodies live in the
database cache alias "map", so every process and every restart sees the same entries.
An evicted entry is rebuilt from the database.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, NamedTuple
from uuid import UUID

import numpy
import shapely
from django.conf import settings
from django.contrib.gis.db.models import Extent
from django.contrib.gis.db.models.functions import Transform
from django.contrib.gis.geos import Polygon as GeosPolygon
from django.core.cache import caches
from django.db.models import Q
from pyproj import Transformer
from shapely.geometry import Polygon

from estimate_updates.contracts import decode_result_bytes
from estimate_updates.models import (
    EstimateResult,
    ForecastRunCell,
    PublishedForecastRun,
)
from sporameteo.finds.grid import CELL_SIZE_M
from national_forest.models import ForestCell
from national_grid.models import GridRegion
from weather_requests.eligibility import weather_cell


WGS84_TO_GRID = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
GRID_TO_WGS84 = Transformer.from_crs("EPSG:3035", "EPSG:4326", always_xy=True)

CACHE_ALIAS = "map"
CACHE_VERSION = "map-v2"
TILE_SIZE_M = 10_000
MIN_FOREST_SHARE = 0.1
NO_VALUE = 255

Tile = tuple[int, int]
Bbox = tuple[float, float, float, float]


class MapReadError(Exception):
    """Carry one stable public error for a map read that cannot be answered."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        message_key: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.message_key = message_key
        self.details = details


@dataclass(frozen=True)
class PublishedRun:
    """Describe the run that readers use for one region, read from one pointer row."""

    region_code: str
    run_id: str
    assessment_date: date
    published_at: datetime
    target_dates: tuple[date, ...]
    method_version: str
    forest_build_id: Any
    forest_build_version: str
    grid_build_id: Any
    previous_run_id: str | None

    def available_dates(self, *, today: date) -> list[date]:
        """Return the run dates that are not in the past, without shifting any of them."""
        return [value for value in self.target_dates if value >= today]


@dataclass(frozen=True)
class PublishedMap:
    """Hold the current run of each published region that uses the configured forest build."""

    runs: dict[str, PublishedRun]
    forest_build_id: Any
    forest_build_version: str
    grid_build_id: Any
    map_id: str

    @property
    def newest(self) -> PublishedRun:
        """Return the run with the latest assessment date."""
        return max(self.runs.values(), key=lambda run: (run.assessment_date, run.region_code))

    @property
    def latest_published_at(self) -> datetime:
        """Return the time of the most recent publication."""
        return max(run.published_at for run in self.runs.values())

    def available_dates(self, *, today: date) -> list[date]:
        """Return the dates of the newest run that are not in the past."""
        return self.newest.available_dates(today=today)


@dataclass(frozen=True)
class RunSummary:
    """Hold the update of each weather cell of one run and the extent of its region."""

    assessment_date: str
    method_version: str
    cell_updates: dict[str, int]
    updates: dict[int, tuple[str, str]]
    extent: tuple[float, float, float, float] | None


class StaticSquare(NamedTuple):
    """Hold the fixed facts of one square of a forest build."""

    region_code: str
    cell_id: str
    forest_cell_id: int
    corner_e: int
    corner_n: int
    is_forest: bool | None
    forest_share: float | None
    weather_key: str | None
    weather_latitude: float | None
    weather_longitude: float | None
    geometry: dict[str, Any]


@dataclass(frozen=True)
class CachedBody:
    """Hold one finished JSON response body and the cache key that identifies it."""

    content: bytes
    key: str


def map_cache() -> Any:
    """Return the shared database cache used for completed map data."""
    return caches[CACHE_ALIAS]


def _published_run(*, pointer: PublishedForecastRun) -> PublishedRun:
    run = pointer.current_run
    return PublishedRun(
        region_code=pointer.region_code,
        run_id=str(run.id),
        assessment_date=run.assessment_date,
        published_at=pointer.published_at,
        target_dates=tuple(date.fromisoformat(value) for value in run.target_dates),
        method_version=run.method_version,
        forest_build_id=run.forest_build_id,
        forest_build_version=run.forest_build.version,
        grid_build_id=run.forest_build.grid_build_id,
        previous_run_id=str(pointer.previous_run_id) if pointer.previous_run_id else None,
    )


def _map_id(*, runs: dict[str, PublishedRun]) -> str:
    text = "|".join(f"{code}:{runs[code].run_id}" for code in sorted(runs))
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def published_map() -> PublishedMap | None:
    """Read the pointer of every region with one query and keep the runs of the configured build."""
    version = str(getattr(settings, "SPORAMETEO_FOREST_BUILD_VERSION", ""))
    pointers = PublishedForecastRun.objects.select_related("current_run__forest_build").order_by(
        "region_code",
    )
    runs = {
        pointer.region_code: _published_run(pointer=pointer)
        for pointer in pointers
        if pointer.current_run.forest_build.version == version
    }
    if not runs:
        return None
    first = next(iter(runs.values()))
    return PublishedMap(
        runs=runs,
        forest_build_id=first.forest_build_id,
        forest_build_version=first.forest_build_version,
        grid_build_id=first.grid_build_id,
        map_id=_map_id(runs=runs),
    )


def map_info(*, published: PublishedMap) -> dict[str, Any]:
    """Return the public description of the map run and of each published region run."""
    newest = published.newest
    return {
        "run_id": published.map_id,
        "assessment_date": newest.assessment_date.isoformat(),
        "published_at": published.latest_published_at.isoformat(),
        "target_dates": [value.isoformat() for value in newest.target_dates],
    }


def regions_info(*, published: PublishedMap) -> list[dict[str, Any]]:
    """Return the run of each published region, sorted by region code."""
    return [
        {
            "region_code": code,
            "run_id": run.run_id,
            "assessment_date": run.assessment_date.isoformat(),
            "published_at": run.published_at.isoformat(),
        }
        for code, run in sorted(published.runs.items())
    ]


def region_extent(*, grid_build_id: Any, region_codes: list[str]) -> Bbox | None:
    """Return the WGS84 extent of the geometries of the given regions of one grid build."""
    extent = (
        GridRegion.objects.filter(build_id=grid_build_id, istat_code__in=region_codes)
        .annotate(wgs84_geometry=Transform("geometry", srid=4326))
        .aggregate(wgs84_extent=Extent("wgs84_geometry"))
        .get("wgs84_extent")
    )
    if extent is None:
        return None
    west, south, east, north = extent
    return float(west), float(south), float(east), float(north)


def _cached_extent(*, grid_build_id: Any, region_code: str) -> Bbox | None:
    """Return the cached extent of one region, read from the database when it is missing."""
    cache = map_cache()
    key = f"{CACHE_VERSION}:extent:{grid_build_id}:{region_code}"
    extent = cache.get(key)
    if extent is None:
        extent = region_extent(grid_build_id=grid_build_id, region_codes=[region_code])
        if extent is not None:
            cache.set(key, extent, timeout=None)
    return extent


def _meets(*, extent: Bbox, bbox: Bbox) -> bool:
    west, south, east, north = bbox
    ext_west, ext_south, ext_east, ext_north = extent
    return not (west > ext_east or east < ext_west or south > ext_north or north < ext_south)


def _summary_key(*, run: PublishedRun) -> str:
    return f"{CACHE_VERSION}:summary:{run.run_id}"


def _build_summary(*, run: PublishedRun) -> RunSummary:
    rows = (
        ForecastRunCell.objects.filter(run_id=run.run_id)
        .exclude(update_id=None)
        .values_list("cell_key", "update_id", "update__public_id", "update__calculated_at")
    )
    cell_updates: dict[str, int] = {}
    updates: dict[int, tuple[str, str]] = {}
    for cell_key, update_id, public_id, calculated_at in rows:
        cell_updates[cell_key] = update_id
        updates[update_id] = (str(public_id), calculated_at.isoformat())
    return RunSummary(
        assessment_date=run.assessment_date.isoformat(),
        method_version=run.method_version,
        cell_updates=cell_updates,
        updates=updates,
        extent=_cached_extent(grid_build_id=run.grid_build_id, region_code=run.region_code),
    )


def run_summary(*, run: PublishedRun) -> RunSummary:
    """Return the cached summary of a run, built from the database when it is missing."""
    cache = map_cache()
    key = _summary_key(run=run)
    summary = cache.get(key)
    if summary is None:
        summary = _build_summary(run=run)
        cache.set(key, summary, timeout=None)
    return summary


def _static_key(*, published: PublishedMap, tile: Tile) -> str:
    return f"{CACHE_VERSION}:static:{published.forest_build_version}:{tile[0]}:{tile[1]}"


def _run_tile_key(*, run: PublishedRun, tile: Tile) -> str:
    return f"{CACHE_VERSION}:results:{run.run_id}:{tile[0]}:{tile[1]}"


def _tile_of(*, corner_e: int, corner_n: int) -> Tile:
    return corner_e // TILE_SIZE_M, corner_n // TILE_SIZE_M


def _tiles_in(*, bounds: tuple[float, float, float, float]) -> list[Tile]:
    west, south, east, north = bounds
    return [
        (tile_e, tile_n)
        for tile_e in range(math.floor(west / TILE_SIZE_M), math.floor(east / TILE_SIZE_M) + 1)
        for tile_n in range(math.floor(south / TILE_SIZE_M), math.floor(north / TILE_SIZE_M) + 1)
    ]


def _square_geometries(*, corners: list[tuple[int, int]]) -> list[dict[str, Any]]:
    """Transform each square corner pair into a WGS84 polygon without reading geometries."""
    size = CELL_SIZE_M
    xs: list[float] = []
    ys: list[float] = []
    for corner_e, corner_n in corners:
        xs.extend([corner_e, corner_e, corner_e + size, corner_e + size, corner_e])
        ys.extend([corner_n, corner_n + size, corner_n + size, corner_n, corner_n])
    longitudes, latitudes = GRID_TO_WGS84.transform(xs, ys)
    geometries: list[dict[str, Any]] = []
    for index in range(len(corners)):
        start = index * 5
        ring = [[longitudes[start + step], latitudes[start + step]] for step in range(5)]
        geometries.append({"type": "Polygon", "coordinates": [ring]})
    return geometries


def _weather_key(*, latitude: float | None, longitude: float | None) -> str | None:
    if latitude is None or longitude is None:
        return None
    return weather_cell(latitude=latitude, longitude=longitude).key


def _load_static_tiles(
    *, published: PublishedMap, tiles: list[Tile],
) -> dict[Tile, tuple[StaticSquare, ...]]:
    """Read the squares of the missing tiles with one forest cell query."""
    wanted = set(tiles)
    tile_e = [tile[0] for tile in tiles]
    tile_n = [tile[1] for tile in tiles]
    area = GeosPolygon.from_bbox(
        (
            min(tile_e) * TILE_SIZE_M,
            min(tile_n) * TILE_SIZE_M,
            (max(tile_e) + 1) * TILE_SIZE_M,
            (max(tile_n) + 1) * TILE_SIZE_M,
        )
    )
    area.srid = 3035
    rows = (
        ForestCell.objects.filter(
            build_id=published.forest_build_id,
            grid_build_cell__cell__geometry__within=area,
        )
        .order_by()
        .values_list(
            "grid_build_cell__owner_region__istat_code",
            "id",
            "grid_build_cell__cell__cell_id",
            "grid_build_cell__cell__corner_e",
            "grid_build_cell__cell__corner_n",
            "is_forest",
            "forest_share",
            "weather_latitude",
            "weather_longitude",
        )
    )
    kept = [row for row in rows if _tile_of(corner_e=row[3], corner_n=row[4]) in wanted]
    geometries = _square_geometries(corners=[(row[3], row[4]) for row in kept])
    squares: dict[Tile, list[StaticSquare]] = {tile: [] for tile in tiles}
    for row, geometry in zip(kept, geometries, strict=True):
        region_code, forest_cell_id, cell_id, corner_e, corner_n, is_forest, share, latitude, longitude = row
        squares[_tile_of(corner_e=corner_e, corner_n=corner_n)].append(
            StaticSquare(
                region_code=region_code,
                cell_id=cell_id,
                forest_cell_id=forest_cell_id,
                corner_e=corner_e,
                corner_n=corner_n,
                is_forest=is_forest,
                forest_share=share,
                weather_key=(
                    _weather_key(latitude=latitude, longitude=longitude)
                    if is_forest is True
                    else None
                ),
                weather_latitude=latitude,
                weather_longitude=longitude,
                geometry=geometry,
            )
        )
    return {tile: tuple(values) for tile, values in squares.items()}


def needs_result(*, square: StaticSquare) -> bool:
    """Return whether the square is a forest square that a run can hold a result for."""
    return (
        square.is_forest is True
        and square.forest_share is not None
        and square.forest_share >= MIN_FOREST_SHARE
    )


def _load_run_tiles(
    *,
    region_code: str,
    summary: RunSummary,
    static: dict[Tile, tuple[StaticSquare, ...]],
    tiles: list[Tile],
) -> dict[Tile, dict[int, tuple[int, tuple[int | str, ...] | None]]]:
    """Read the stored results of the squares of one region in the missing tiles with one query."""
    by_update: dict[int, list[int]] = {}
    tile_of_cell: dict[int, Tile] = {}
    for tile in tiles:
        for square in static[tile]:
            # A border weather cell has a different update in each region.
            if square.region_code != region_code:
                continue
            if not needs_result(square=square) or square.weather_key is None:
                continue
            update_id = summary.cell_updates.get(square.weather_key)
            if update_id is None:
                continue
            by_update.setdefault(update_id, []).append(square.forest_cell_id)
            tile_of_cell[square.forest_cell_id] = tile
    results: dict[Tile, dict[int, tuple[int, tuple[int | str, ...] | None]]] = {
        tile: {} for tile in tiles
    }
    if not by_update:
        return results
    condition = Q()
    for update_id, forest_cell_ids in by_update.items():
        condition |= Q(update_id=update_id, forest_cell_id__in=forest_cell_ids)
    rows = EstimateResult.objects.filter(condition).values_list(
        "update_id", "forest_cell_id", "values",
    )
    for update_id, forest_cell_id, values in rows:
        try:
            decoded: tuple[int | str, ...] | None = decode_result_bytes(values=values)
        except ValueError:
            decoded = None
        results[tile_of_cell[forest_cell_id]][forest_cell_id] = (update_id, decoded)
    return results


def _cached_tiles(
    *,
    tiles: list[Tile],
    key_for: Callable[[Tile], str],
    load_missing: Callable[[list[Tile]], dict[Tile, Any]],
) -> dict[Tile, Any]:
    """Read tiles with one get_many, load the missing ones together and store them."""
    cache = map_cache()
    keys = {key_for(tile): tile for tile in tiles}
    found = cache.get_many(list(keys))
    values: dict[Tile, Any] = {keys[key]: value for key, value in found.items()}
    missing = [tile for tile in tiles if tile not in values]
    if missing:
        loaded = load_missing(missing)
        cache.set_many({key_for(tile): loaded[tile] for tile in missing}, timeout=None)
        values.update(loaded)
    return values


def static_tiles(
    *, published: PublishedMap, tiles: list[Tile],
) -> dict[Tile, tuple[StaticSquare, ...]]:
    """Return the squares of each tile, loading only the tiles that are not cached."""
    return _cached_tiles(
        tiles=tiles,
        key_for=lambda tile: _static_key(published=published, tile=tile),
        load_missing=lambda missing: _load_static_tiles(published=published, tiles=missing),
    )


def run_tiles(
    *,
    run: PublishedRun,
    summary: RunSummary,
    static: dict[Tile, tuple[StaticSquare, ...]],
    tiles: list[Tile],
) -> dict[Tile, dict[int, tuple[int, tuple[int | str, ...] | None]]]:
    """Return the stored results of each tile, loading only the tiles that are not cached."""
    return _cached_tiles(
        tiles=tiles,
        key_for=lambda tile: _run_tile_key(run=run, tile=tile),
        load_missing=lambda missing: _load_run_tiles(
            region_code=run.region_code, summary=summary, static=static, tiles=missing,
        ),
    )


def _normalized_bbox(*, bbox: Bbox) -> Bbox:
    west, south, east, north = (round(value, 6) for value in bbox)
    return west, south, east, north


def _bbox_polygon(*, bbox: Bbox) -> Polygon:
    """Transform a WGS84 box into a polygon in the grid CRS."""
    west, south, east, north = bbox
    corners = [
        WGS84_TO_GRID.transform(west, south),
        WGS84_TO_GRID.transform(east, south),
        WGS84_TO_GRID.transform(east, north),
        WGS84_TO_GRID.transform(west, north),
    ]
    return Polygon(corners)


def _select_squares(
    *, static: dict[Tile, tuple[StaticSquare, ...]], area: Polygon,
) -> list[tuple[Tile, StaticSquare]]:
    """Return the squares that intersect the area, sorted by cell ID."""
    pairs = [(tile, square) for tile, squares in static.items() for square in squares]
    if not pairs:
        return []
    corner_e = numpy.array([square.corner_e for _, square in pairs], dtype=float)
    corner_n = numpy.array([square.corner_n for _, square in pairs], dtype=float)
    boxes = shapely.box(corner_e, corner_n, corner_e + CELL_SIZE_M, corner_n + CELL_SIZE_M)
    shapely.prepare(area)
    mask = shapely.intersects(boxes, area)
    chosen = [pair for pair, keep in zip(pairs, mask, strict=True) if keep]
    return sorted(chosen, key=lambda pair: pair[1].cell_id)


def _body_key(*, kind: str, map_id: str, target_date: date, bbox: Bbox, extra: str = "") -> str:
    text = ",".join(format(value, ".6f") for value in bbox)
    digest = hashlib.sha256(f"{text}|{extra}".encode()).hexdigest()
    return f"{CACHE_VERSION}:{kind}:{map_id}:{target_date.isoformat()}:{digest}"


def cached_body(*, key: str) -> CachedBody | None:
    """Return a stored finished response body, or None when the cache has no such entry."""
    content = map_cache().get(key)
    return CachedBody(content=content, key=key) if content is not None else None


def _store_body(*, key: str, body: dict[str, Any]) -> CachedBody:
    content = json.dumps(body, separators=(",", ":")).encode()
    map_cache().set(key, content, timeout=None)
    return CachedBody(content=content, key=key)


def _square_properties(
    *,
    square: StaticSquare,
    target_date: date,
    state: str,
    percentage: int | None,
    reason: str | None,
    message_key: str,
    assessment_date: str | None = None,
    update_id: str | None = None,
    calculated_at: str | None = None,
) -> dict[str, Any]:
    """Return the public properties of one square."""
    has_weather = square.weather_latitude is not None and square.weather_longitude is not None
    return {
        "cell_id": square.cell_id,
        "state": state,
        "percentage": percentage,
        "unavailable_reason": reason,
        "reason": reason,
        "target_date": target_date.isoformat(),
        "assessment_date": assessment_date,
        "update_id": update_id,
        "calculated_at": calculated_at,
        "message_key": message_key,
        "weather_cell": (
            {"latitude": square.weather_latitude, "longitude": square.weather_longitude}
            if has_weather
            else None
        ),
    }


def _properties(
    *,
    square: StaticSquare,
    result: tuple[int, tuple[int | str, ...] | None] | None,
    summary: RunSummary | None,
    target_date: date,
    offset: int,
) -> dict[str, Any]:
    """Resolve one square against the stored run results."""
    plain = {"square": square, "target_date": target_date}
    if square.is_forest is None or square.forest_share is None:
        return _square_properties(
            **plain, state="unavailable", percentage=None, reason="missing_forest_data",
            message_key="estimate.missing_forest",
        )
    if not needs_result(square=square):
        return _square_properties(
            **plain, state="noforest", percentage=None, reason="noforest",
            message_key="estimate.no_forest",
        )
    if result is None or summary is None:
        return _square_properties(
            **plain, state="unavailable", percentage=None, reason="outside_prepared_area",
            message_key="estimate.outside_prepared_area",
        )
    update_id, values = result
    public_id, calculated_at = summary.updates[update_id]
    known = {
        "assessment_date": summary.assessment_date,
        "update_id": public_id,
        "calculated_at": calculated_at,
    }
    value = values[offset] if values is not None and 0 <= offset < len(values) else None
    if value is None:
        return _square_properties(
            **plain, **known, state="unavailable", percentage=None, reason="requiredgap",
            message_key="estimate.required_gap",
        )
    if isinstance(value, int):
        return _square_properties(
            **plain, **known, state="available", percentage=value, reason=None,
            message_key="estimate.available",
        )
    return _square_properties(
        **plain, **known, state="unavailable", percentage=None, reason=value,
        message_key="estimate.unavailable",
    )


def _check_extent(*, published: PublishedMap, bbox: Bbox) -> None:
    """Raise when the bbox touches the extent of no published region."""
    extents = [
        extent
        for code in published.runs
        if (extent := _cached_extent(grid_build_id=published.grid_build_id, region_code=code))
        is not None
    ]
    if extents and not any(_meets(extent=extent, bbox=bbox) for extent in extents):
        raise _outside_coverage()


def _outside_coverage() -> MapReadError:
    return MapReadError(
        status=400,
        code="outside_coverage",
        message="The bbox is outside the regions with published estimates.",
        message_key="map.outside_coverage",
    )


def day_offset(*, run: PublishedRun, target_date: date) -> int:
    return (target_date - run.assessment_date).days


def read_squares(
    *, published: PublishedMap, target_date: date, bbox: Bbox, max_squares: int,
) -> CachedBody:
    """Return the squares of one view for one date, each from the run of its own region."""
    bbox = _normalized_bbox(bbox=bbox)
    key = _body_key(kind="squares", map_id=published.map_id, target_date=target_date, bbox=bbox)
    stored = cached_body(key=key)
    if stored is not None:
        return stored
    _check_extent(published=published, bbox=bbox)
    area = _bbox_polygon(bbox=bbox)
    tiles = _tiles_in(bounds=area.bounds)
    static = static_tiles(published=published, tiles=tiles)
    selected = _select_squares(static=static, area=area)
    if not selected:
        raise _outside_coverage()
    if len(selected) > max_squares:
        raise MapReadError(
            status=400,
            code="square_limit_exceeded",
            message="Zoom in for estimates before requesting this many squares.",
            message_key="map.square_limit_exceeded",
            details={"square_count": len(selected), "max_squares": max_squares},
        )
    summaries: dict[str, RunSummary] = {}
    results: dict[str, dict[Tile, dict[int, tuple[int, tuple[int | str, ...] | None]]]] = {}
    for code in sorted({square.region_code for _, square in selected} & set(published.runs)):
        run = published.runs[code]
        used = sorted({tile for tile, square in selected if square.region_code == code})
        summaries[code] = run_summary(run=run)
        results[code] = run_tiles(run=run, summary=summaries[code], static=static, tiles=used)
    features = [
        {
            "type": "Feature",
            "id": square.cell_id,
            "geometry": square.geometry,
            "properties": _properties(
                square=square,
                result=results.get(square.region_code, {}).get(tile, {}).get(square.forest_cell_id),
                summary=summaries.get(square.region_code),
                target_date=target_date,
                offset=day_offset(run=published.runs[square.region_code], target_date=target_date)
                if square.region_code in published.runs
                else 0,
            ),
        }
        for tile, square in selected
    ]
    body = {
        "type": "FeatureCollection",
        "run_id": published.map_id,
        "assessment_date": published.newest.assessment_date.isoformat(),
        "target_date": target_date.isoformat(),
        "square_count": len(features),
        "features": features,
    }
    return _store_body(key=key, body=body)


def overview_key(*, published: PublishedMap, target_date: date, bbox: Bbox, size: int) -> str:
    """Return the cache key of one overview response."""
    return _body_key(
        kind="overview2", map_id=published.map_id, target_date=target_date,
        bbox=_normalized_bbox(bbox=bbox), extra=str(size),
    )


def _union_extent(*, published: PublishedMap) -> Bbox | None:
    extents = [
        extent
        for code in published.runs
        if (extent := _cached_extent(grid_build_id=published.grid_build_id, region_code=code))
        is not None
    ]
    if not extents:
        return None
    wests, souths, easts, norths = zip(*extents, strict=True)
    return min(wests), min(souths), max(easts), max(norths)


class _Columns(NamedTuple):
    """Hold the forest squares of one run inside a view, one entry per square."""

    corner_e: numpy.ndarray
    corner_n: numpy.ndarray
    values: numpy.ndarray
    update_index: numpy.ndarray
    calculated_at: list[str]
    assessment_day: int


def _run_columns(
    *, published: PublishedMap, run: PublishedRun, target_date: date, area: Polygon,
) -> _Columns | None:
    """Read the squares of one run inside the area, at the day offset of that run."""
    extent = _cached_extent(grid_build_id=published.grid_build_id, region_code=run.region_code)
    if extent is not None and not area.intersects(_bbox_polygon(bbox=extent)):
        return None
    points = overview_points(run=run, summary=run_summary(run=run))
    centre_e = points.corner_e + CELL_SIZE_M / 2
    centre_n = points.corner_n + CELL_SIZE_M / 2
    inside = shapely.contains_xy(area, centre_e, centre_n)
    offset = day_offset(run=run, target_date=target_date)
    if 0 <= offset < points.values.shape[1]:
        values = points.values[inside, offset].astype(numpy.int64)
    else:
        values = numpy.full(int(inside.sum()), NO_VALUE, dtype=numpy.int64)
    return _Columns(
        corner_e=points.corner_e[inside].astype(numpy.int64),
        corner_n=points.corner_n[inside].astype(numpy.int64),
        values=values,
        update_index=points.update_index[inside].astype(numpy.int64),
        calculated_at=points.calculated_at,
        assessment_day=run.assessment_date.toordinal(),
    )


def read_overview(
    *, published: PublishedMap, target_date: date, bbox: Bbox, size: int,
) -> CachedBody:
    """Average the stored scores of forest squares of every region into areas of size metres."""
    bbox = _normalized_bbox(bbox=bbox)
    key = overview_key(published=published, target_date=target_date, bbox=bbox, size=size)
    stored = cached_body(key=key)
    if stored is not None:
        return stored
    base = {
        "type": "FeatureCollection",
        "run_id": published.map_id,
        "assessment_date": published.newest.assessment_date.isoformat(),
        "target_date": target_date.isoformat(),
        "area_size_m": size,
    }
    clipped = bbox
    union = _union_extent(published=published)
    if union is not None:
        west, south, east, north = bbox
        ext_west, ext_south, ext_east, ext_north = union
        clipped = (max(west, ext_west), max(south, ext_south), min(east, ext_east), min(north, ext_north))
        if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
            return CachedBody(
                content=json.dumps({**base, "features": []}, separators=(",", ":")).encode(),
                key=key,
            )
    area = _bbox_polygon(bbox=clipped)
    parts = [
        part
        for run in published.runs.values()
        if (part := _run_columns(published=published, run=run, target_date=target_date, area=area))
        is not None
    ]
    stamps = sorted({stamp for part in parts for stamp in part.calculated_at})
    rank_of = {stamp: rank for rank, stamp in enumerate(stamps)}
    corner_e = numpy.concatenate([part.corner_e for part in parts] or [numpy.zeros(0, dtype=numpy.int64)])
    corner_n = numpy.concatenate([part.corner_n for part in parts] or [numpy.zeros(0, dtype=numpy.int64)])
    values = numpy.concatenate([part.values for part in parts] or [numpy.zeros(0, dtype=numpy.int64)])
    update_rank = numpy.concatenate(
        [
            numpy.array([rank_of[stamp] for stamp in part.calculated_at], dtype=numpy.int64)[
                part.update_index
            ]
            for part in parts
        ]
        or [numpy.zeros(0, dtype=numpy.int64)]
    )
    assessment_day = numpy.concatenate(
        [numpy.full(len(part.values), part.assessment_day, dtype=numpy.int64) for part in parts]
        or [numpy.zeros(0, dtype=numpy.int64)]
    )
    scored = values != NO_VALUE
    cell_e = corner_e // size
    cell_n = corner_n // size
    keys, group_of = numpy.unique(cell_e * 1_000_000 + cell_n, return_inverse=True)
    counts = numpy.bincount(group_of, minlength=len(keys))
    scored_counts = numpy.bincount(group_of, weights=scored, minlength=len(keys))
    sums = numpy.bincount(group_of, weights=numpy.where(scored, values, 0), minlength=len(keys))
    highest = numpy.iinfo(numpy.int64).max
    oldest = numpy.full(len(keys), highest)
    numpy.minimum.at(oldest, group_of[scored], update_rank[scored])
    oldest_day = numpy.full(len(keys), highest)
    numpy.minimum.at(oldest_day, group_of[scored], assessment_day[scored])
    features = []
    for index, group_key in enumerate(keys):
        east = int(group_key // 1_000_000) * size
        north = int(group_key % 1_000_000) * size
        count = int(counts[index])
        scored_count = int(scored_counts[index])
        # A partly missing area must not look like a fully assessed area.
        complete = scored_count == count
        ring = [
            list(GRID_TO_WGS84.transform(x, y))
            for x, y in (
                (east, north), (east + size, north), (east + size, north + size),
                (east, north + size), (east, north),
            )
        ]
        features.append(
            {
                "type": "Feature",
                "id": f"overview-{east}-{north}",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {
                    "overview": True,
                    "target_date": target_date.isoformat(),
                    "state": "available" if complete else "unavailable",
                    "percentage": round(float(sums[index]) / scored_count) if complete else None,
                    "assessment_date": (
                        date.fromordinal(int(oldest_day[index])).isoformat() if scored_count else None
                    ),
                    "calculated_at": stamps[int(oldest[index])] if scored_count else None,
                    "forest_square_count": count,
                    "scored_square_count": scored_count,
                },
            }
        )
    return _store_body(key=key, body={**base, "features": features})


@dataclass(frozen=True)
class OverviewPoints:
    """Hold one row per forest square of a run, as compact arrays."""

    corner_e: numpy.ndarray
    corner_n: numpy.ndarray
    values: numpy.ndarray
    update_index: numpy.ndarray
    calculated_at: list[str]


def _points_key(*, run: PublishedRun) -> str:
    return f"{CACHE_VERSION}:overview-points:{run.run_id}"


def _build_overview_points(*, summary: RunSummary) -> bytes:
    """Read every forest square result of the run with one query and pack it."""
    order = sorted(summary.updates, key=lambda update_id: summary.updates[update_id][1])
    index_of = {update_id: index for index, update_id in enumerate(order)}
    rows = EstimateResult.objects.filter(
        update_id__in=list(summary.updates),
        forest_cell__forest_share__gte=MIN_FOREST_SHARE,
    ).values_list(
        "update_id",
        "forest_cell__grid_build_cell__cell__corner_e",
        "forest_cell__grid_build_cell__cell__corner_n",
        "values",
    )
    # The arrays are filled as the rows stream in. They grow in blocks if the table gains rows after the count.
    capacity = rows.count()
    update_index = numpy.zeros(capacity, dtype=numpy.int16)
    corner_e = numpy.zeros(capacity, dtype=numpy.int32)
    corner_n = numpy.zeros(capacity, dtype=numpy.int32)
    values = numpy.full((capacity, 8), NO_VALUE, dtype=numpy.uint8)
    filled = 0
    for update_id, east, north, raw in rows.iterator(chunk_size=20_000):
        if filled == capacity:
            capacity += 20_000
            update_index = numpy.resize(update_index, capacity)
            corner_e = numpy.resize(corner_e, capacity)
            corner_n = numpy.resize(corner_n, capacity)
            values = numpy.concatenate([values, numpy.full((20_000, 8), NO_VALUE, dtype=numpy.uint8)])
        try:
            values[filled] = [value if isinstance(value, int) else NO_VALUE for value in decode_result_bytes(values=raw)]
        except ValueError:
            pass
        update_index[filled] = index_of[update_id]
        corner_e[filled] = east
        corner_n[filled] = north
        filled += 1
    buffer = io.BytesIO()
    numpy.savez_compressed(
        buffer,
        corner_e=corner_e[:filled],
        corner_n=corner_n[:filled],
        values=values[:filled],
        update_index=update_index[:filled],
        calculated_at=numpy.array([summary.updates[update_id][1] for update_id in order], dtype=str),
    )
    return buffer.getvalue()


def overview_points(*, run: PublishedRun, summary: RunSummary) -> OverviewPoints:
    """Return the compact per-square data of a run, built from the database when missing."""
    cache = map_cache()
    key = _points_key(run=run)
    packed = cache.get(key)
    if packed is None:
        packed = _build_overview_points(summary=summary)
        cache.set(key, packed, timeout=None)
    with numpy.load(io.BytesIO(packed)) as data:
        return OverviewPoints(
            corner_e=data["corner_e"],
            corner_n=data["corner_n"],
            values=data["values"],
            update_index=data["update_index"],
            calculated_at=[str(value) for value in data["calculated_at"]],
        )


def detail_key(*, update_id: UUID, square_id: str, target_date: date) -> str:
    """Return the cache key of one area detail body."""
    # Version 2 adds the municipality name, so bodies stored before it are not reused.
    return f"{CACHE_VERSION}:detail-v2:{update_id}:{square_id}:{target_date.isoformat()}"


def cached_detail(*, update_id: UUID, square_id: str, target_date: date) -> dict[str, Any] | None:
    """Return a stored area detail body, or None when the cache has no such entry."""
    return map_cache().get(detail_key(update_id=update_id, square_id=square_id, target_date=target_date))


def store_detail(
    *, update_id: UUID, square_id: str, target_date: date, body: dict[str, Any],
) -> None:
    """Store one finished area detail body."""
    map_cache().set(
        detail_key(update_id=update_id, square_id=square_id, target_date=target_date),
        body,
        timeout=None,
    )


def run_id_for_update(*, update_id: UUID) -> str | None:
    """Return the published run, current or previous, whose cells hold the update."""
    run_ids = [
        value
        for pointer in PublishedForecastRun.objects.order_by("region_code").values_list(
            "current_run_id", "previous_run_id",
        )
        for value in pointer
        if value is not None
    ]
    if not run_ids:
        return None
    found = set(
        ForecastRunCell.objects.filter(update__public_id=update_id, run_id__in=run_ids)
        .values_list("run_id", flat=True)
    )
    for run_id in run_ids:
        if run_id in found:
            return str(run_id)
    return None


def warm_published_map_cache(*, region_code: str) -> dict[str, int]:
    """Build the run summary and the overview points of the published run of one region.

    Square and result tiles are built by the first request that needs them.
    """
    current = published_map()
    run = current.runs.get(region_code) if current is not None else None
    if run is None:
        return {"squares": 0}
    summary = run_summary(run=run)
    points = overview_points(run=run, summary=summary)
    return {"squares": len(points.corner_e)}
