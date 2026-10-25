"""Find the best scoring areas of the first published date for the landing page.

The ranking reads the cached per-square arrays of the published runs, so a request never
queries the stored results. Only the municipality and region names are read from the grid
tables, and the finished list is kept in the shared map cache.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from uuid import UUID

import numpy
from ninja import Status

from area_api.router import read_area_detail
from map_api import published
from map_api.published import GRID_TO_WGS84, NO_VALUE, _offset
from national_grid.models import GridCell, GridRegion
from sporameteo.finds.grid import CELL_SIZE_M, format_cell_id, parse_cell_id


logger = logging.getLogger(__name__)

TOP_COUNT = 8
NAME_BLOCK = 300
CACHE_KEY = "landing-top-areas-v1:{map_id}:{first}:{last}"
CACHE_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 15 * 60
DAYS_AHEAD = 8


def _centre(*, square_id: str) -> tuple[float, float]:
    """Return the latitude and longitude of the middle of one square."""
    address = parse_cell_id(square_id, expected_size_m=CELL_SIZE_M)
    half = CELL_SIZE_M / 2
    longitude, latitude = GRID_TO_WGS84.transform(address.corner_e + half, address.corner_n + half)
    return latitude, longitude


def _read_detail(*, square_id: str, target_date: date, update_id: str) -> dict[str, Any] | None:
    """Read the area details with the function behind the detail endpoint and its cache."""
    try:
        result = read_area_detail(square_id=square_id, target_date=target_date, update_id=UUID(update_id))
    except Exception:
        # The landing page must still render when one detail cannot be built.
        logger.exception("The landing page could not read the details of %s", square_id)
        return None
    if isinstance(result, Status):
        return None
    return result


def _hero_facts(*, detail: dict[str, Any]) -> dict[str, Any]:
    """Pick the few detail values the hero card shows."""
    weather = detail.get("weather") or {}
    selected = weather.get("selected_day") or {}
    facts = {fact["role_key"]: fact for fact in detail.get("facts") or []}
    rain = {}
    for days in (7, 14, 21):
        period = weather.get(f"recent_rain_{days}_days") or {}
        rain[days] = period.get("value") if period.get("state") == "available" else None
    forest = (facts.get("forest_share") or {}).get("value")
    return {
        "latitude": detail["selection"]["centroid"]["latitude"],
        "longitude": detail["selection"]["centroid"]["longitude"],
        "rain": rain,
        "forest_share": forest,
        "mean_temperature": selected.get("mean_temperature_c"),
        "min_temperature": selected.get("min_temperature_c"),
        "calculated_at": detail["estimate"].get("calculated_at"),
    }


def _candidates(*, current: published.PublishedMap, first: date, last: date) -> dict[str, Any]:
    """Return every square with a score on the first date, best score first.

    Squares with the same score are ordered by their corners, which is the order of the
    square ids.
    """
    parts: dict[str, list[numpy.ndarray]] = {"corner_e": [], "corner_n": [], "first": [], "last": [], "update": []}
    public_ids: list[str] = []
    for run in current.runs.values():
        first_offset = _offset(run=run, target_date=first)
        if not 0 <= first_offset < DAYS_AHEAD:
            continue
        summary = published.run_summary(run=run)
        points = published.overview_points(run=run, summary=summary)
        order = sorted(summary.updates, key=lambda update_id: summary.updates[update_id][1])
        first_values = points.values[:, first_offset].astype(numpy.int64)
        last_offset = _offset(run=run, target_date=last)
        if 0 <= last_offset < DAYS_AHEAD:
            last_values = points.values[:, last_offset].astype(numpy.int64)
        else:
            last_values = numpy.full(len(first_values), NO_VALUE, dtype=numpy.int64)
        keep = first_values != NO_VALUE
        parts["corner_e"].append(points.corner_e[keep].astype(numpy.int64))
        parts["corner_n"].append(points.corner_n[keep].astype(numpy.int64))
        parts["first"].append(first_values[keep])
        parts["last"].append(last_values[keep])
        parts["update"].append(points.update_index[keep].astype(numpy.int64) + len(public_ids))
        public_ids.extend(summary.updates[update_id][0] for update_id in order)
    if not parts["first"]:
        return {"public_ids": public_ids, **{key: numpy.array([], dtype=numpy.int64) for key in parts}}
    joined = {key: numpy.concatenate(value) for key, value in parts.items()}
    ranking = numpy.lexsort((joined["corner_n"], joined["corner_e"], -joined["first"]))
    return {"public_ids": public_ids, **{key: value[ranking] for key, value in joined.items()}}


def _pick_municipalities(*, candidates: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk down the ranking and keep the best square of each municipality."""
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = len(candidates["first"])
    for start in range(0, total, NAME_BLOCK):
        block = range(start, min(start + NAME_BLOCK, total))
        ids = {
            index: format_cell_id(
                corner_e=int(candidates["corner_e"][index]), corner_n=int(candidates["corner_n"][index])
            )
            for index in block
        }
        cells = GridCell.objects.filter(cell_id__in=list(ids.values())).select_related("municipality")
        municipality_of = {cell.cell_id: cell.municipality for cell in cells}
        for index in block:
            municipality = municipality_of.get(ids[index])
            if municipality is None or municipality.istat_code in seen:
                continue
            seen.add(municipality.istat_code)
            chosen.append(
                {
                    "square_id": ids[index],
                    "name": municipality.name,
                    "region_code": municipality.region_code,
                    "first": int(candidates["first"][index]),
                    "last": None if candidates["last"][index] == NO_VALUE else int(candidates["last"][index]),
                    "update_id": candidates["public_ids"][int(candidates["update"][index])],
                }
            )
            if len(chosen) == TOP_COUNT:
                return chosen
    return chosen


def _collect(*, current: published.PublishedMap, first: date, last: date) -> list[dict[str, Any]]:
    """Build the top list with place names, centres and the hero facts of each square."""
    chosen = _pick_municipalities(candidates=_candidates(current=current, first=first, last=last))
    regions = dict(
        GridRegion.objects.filter(
            build_id=current.grid_build_id, istat_code__in={entry["region_code"] for entry in chosen}
        ).values_list("istat_code", "name")
    )
    entries = []
    for entry in chosen:
        region = regions.get(entry.pop("region_code"), "")
        latitude, longitude = _centre(square_id=entry["square_id"])
        detail = _read_detail(
            square_id=entry["square_id"], target_date=first, update_id=entry["update_id"]
        )
        entries.append(
            {
                **entry,
                "title": f"{entry['name']}, {region}" if region else entry["name"],
                "latitude": latitude,
                "longitude": longitude,
                "facts": _hero_facts(detail=detail) if detail is not None else None,
            }
        )
    return entries


def top_areas(*, current: published.PublishedMap, first: date, last: date) -> list[dict[str, Any]]:
    """Return the cached best areas of the first date, reading them once per run."""
    key = CACHE_KEY.format(map_id=current.map_id, first=first.isoformat(), last=last.isoformat())
    cache = published.map_cache()
    stored = cache.get(key)
    if stored is not None:
        return stored
    entries = _collect(current=current, first=first, last=last)
    # A list with a failed detail read is kept for a short time only, so the next tries come soon
    # but a square that keeps failing does not rebuild the whole ranking on every request.
    complete = all(entry["facts"] is not None for entry in entries)
    cache.set(key, entries, timeout=CACHE_SECONDS if complete else RETRY_SECONDS)
    return entries
