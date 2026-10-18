"""Method outside Tuscany: the daily v5 and the cold candidate estimates at 11 forest places, from the Open-Meteo files alone.

The script reads public Open-Meteo files and the local database read-only. It calls no weather API and writes nothing to the database.
"""

import csv
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any

import numpy as np

from sporameteo.context import RunContext
from sporameteo.finds import gbif, inaturalist
from sporameteo.finds.cleaning import mark_duplicates
from sporameteo.weather import bucket_files

ROOT = Path(__file__).resolve().parent.parent.parent
REPORTS_DIR = ROOT / "reports"
TIMEOUT_SECONDS = 60
HOURLY_VARIABLES = ("precipitation", "temperature", "soil_moisture", "soil_temperature", "et0")
GROUPS = ("beech", "chestnut", "oaks", "pines", "spruce_fir")
db = SimpleNamespace()

PLACE_COLUMNS = [
    "place",
    "region",
    "region_of_squares",
    "cell_key",
    "distance_km",
    "squares",
    "place_height_m",
    "grid_point_height_m",
    "height_correction_c",
    "altitude_min_m",
    "altitude_median_m",
    "altitude_max_m",
    "forest_type",
    "top_species_shares",
    "days_without_weather",
    "days_without_estimate",
]


FIND_COLUMNS = (
    "place",
    "find_date",
    "estimate_median_v5",
    "estimate_median_cold",
    "factor_retained_water_v5",
    "factor_retained_water_cold",
    "factor_temperature_7_days_v5",
    "factor_temperature_7_days_cold",
    "factor_mean_temperature_20_days_cold",
    "rain_mm_on_the_day",
    "rain_mm_in_the_30_days_before",
)


def start_django() -> None:
    """Start Django and make every query of this session read-only."""
    application = ROOT / "apps" / "application"
    if str(application) not in sys.path:
        sys.path.insert(0, str(application))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    import django

    django.setup()
    from django.conf import settings as django_settings
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")

    from estimate_updates import services as estimate_services
    from estimate_updates.host_trees import saved_host_trees
    from estimate_updates.models import EstimateUpdate
    from national_forest.models import ForestBuild, ForestCell
    from national_grid.models import GridBuildCell
    from national_terrain.models import TerrainCell
    from sporameteo import host_trees
    from sporameteo.moisture_method import _moisture, _score, temperature_response
    from sporameteo.forecast_method import METHOD_V2, METHOD_V5, calculate_forecast
    from sporameteo.weather.policy import PIPELINE_VERSION
    from weather_requests.eligibility import weather_cell
    from weather_summaries import services as summary_services
    from weather_summaries.aggregation import aggregate_hourly
    from weather_summaries.models import WeatherSnapshot

    db.connection = connection
    db.settings = django_settings
    db.estimate_services = estimate_services
    db.summary_services = summary_services
    db.saved_host_trees = saved_host_trees
    db.EstimateUpdate = EstimateUpdate
    db.ForestBuild = ForestBuild
    db.ForestCell = ForestCell
    db.GridBuildCell = GridBuildCell
    db.TerrainCell = TerrainCell
    db.host_trees = host_trees
    db.METHOD_V5 = METHOD_V5
    db.METHOD_V2 = METHOD_V2
    db._moisture = _moisture
    db._score = _score
    db.temperature_response = temperature_response
    db.calculate_forecast = calculate_forecast
    db.PIPELINE_VERSION = PIPELINE_VERSION
    db.weather_cell = weather_cell
    db.aggregate_hourly = aggregate_hourly
    db.WeatherSnapshot = WeatherSnapshot


def assert_read_only() -> None:
    """Fail if the database session is not read-only."""
    with db.connection.cursor() as cursor:
        cursor.execute("SHOW transaction_read_only")
        if cursor.fetchone()[0] != "on":
            raise RuntimeError("The database session is not read-only.")


def distance_km(*, latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    """Return the great-circle distance between two points in kilometres."""
    phi_a, phi_b = math.radians(latitude_a), math.radians(latitude_b)
    delta = math.radians(longitude_b - longitude_a)
    haversine = math.sin((phi_b - phi_a) / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(haversine))


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Places and their forest squares
# ---------------------------------------------------------------------------


def choose_places(*, settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Pick for each place the weather cell with forest squares that lies nearest to the given point."""
    forest_build = db.ForestBuild.objects.get(version=settings["forest_build_version"])
    radius = settings["search_radius_deg"]
    places: list[dict[str, Any]] = []
    for place in settings["places"]:
        candidates = (
            db.ForestCell.objects.filter(
                build_id=forest_build.id,
                is_forest=True,
                weather_latitude__range=(place["latitude"] - radius, place["latitude"] + radius),
                weather_longitude__range=(place["longitude"] - radius, place["longitude"] + radius),
            )
            .values_list("weather_latitude", "weather_longitude")
            .distinct()
        )
        nearest = min(
            candidates,
            key=lambda point: distance_km(
                latitude_a=place["latitude"], longitude_a=place["longitude"], latitude_b=point[0], longitude_b=point[1]
            ),
        )
        cell = db.weather_cell(latitude=Decimal(str(nearest[0])), longitude=Decimal(str(nearest[1])))
        squares = db.estimate_services._forest_cells_for_weather_cell(forest_build=forest_build, cell_key=cell.key, batch_size=1000)
        squares = list(
            db.ForestCell.objects.filter(id__in=[square.id for square in squares])
            .select_related("grid_build_cell__owner_region", "grid_build_cell__cell")
            .order_by("id")
        )
        places.append(
            {
                **place,
                "cell_key": cell.key,
                "cell_latitude": cell.latitude,
                "cell_longitude": cell.longitude,
                "distance_km": round(
                    distance_km(
                        latitude_a=place["latitude"],
                        longitude_a=place["longitude"],
                        latitude_b=cell.latitude,
                        longitude_b=cell.longitude,
                    ),
                    1,
                ),
                "squares": squares,
            }
        )
        print(f"{place['name']}: cell {cell.key}, {places[-1]['distance_km']} km away, {len(squares)} squares", flush=True)
    return places


def terrain_build_id() -> Any:
    """Return the one terrain build that the published Tuscany updates use."""
    ids = set(db.EstimateUpdate.objects.values_list("terrain_build_id", flat=True).distinct())
    if len(ids) != 1:
        raise RuntimeError(f"Expected one terrain build in the estimate updates, found {len(ids)}.")
    return ids.pop()


def load_square_inputs(*, place: dict[str, Any], terrain_id: Any) -> list[tuple[Any, Any, Any]]:
    """Return each forest square with its terrain cell and host trees, read from local files only."""
    squares = place["squares"]
    terrain_cells = {
        row.grid_build_cell_id: row
        for row in db.TerrainCell.objects.filter(
            build_id=terrain_id, grid_build_cell_id__in=[square.grid_build_cell_id for square in squares]
        ).only("id", "build_id", "grid_build_cell_id", "altitude_m")
    }
    directory = Path(db.settings.SPORAMETEO_JRC_DIRECTORY)
    selected = db.host_trees.read_manifest(directory=directory)
    tree_inputs: dict[str, Any] = {}
    if selected is not None:
        manifest, version = selected
        centres = {
            square.grid_build_cell.cell.cell_id: (square.grid_build_cell.cell.corner_e + 250.0, square.grid_build_cell.cell.corner_n + 250.0)
            for square in squares
        }
        sampled = db.host_trees.sample_squares(directory=directory, manifest=manifest, version=version, centres=centres)
        tree_inputs = {
            "source": {
                "source_id": f"jrc:{version}",
                "source_year": manifest["source_year"],
                "resolution_m": manifest["resolution_m"],
                "metadata": manifest,
            },
            "cells": {str(square.id): list(sampled[square.grid_build_cell.cell.cell_id].values) for square in squares},
        }
    return [
        (
            square,
            terrain_cells.get(square.grid_build_cell_id),
            db.saved_host_trees(tree_inputs=tree_inputs, forest_cell_id=square.id),
        )
        for square in squares
    ]


def describe_squares(*, inputs: list[tuple[Any, Any, Any]]) -> dict[str, Any]:
    """Return the altitude range, the region split and the host tree shares of a place."""
    altitudes = [terrain.altitude_m for _, terrain, _ in inputs if terrain is not None and terrain.altitude_m is not None]
    regions = Counter(square.grid_build_cell.owner_region.name if square.grid_build_cell.owner_region else "none" for square, _, _ in inputs)
    shares: dict[str, float] = {}
    for index, group in enumerate(GROUPS):
        values = [trees.values[index] for _, _, trees in inputs if trees is not None and trees.values[index] is not None]
        if values:
            shares[group] = sum(values) / len(values)
    ranked = sorted(shares.items(), key=lambda item: -item[1])
    return {
        "altitude_min_m": round(min(altitudes)) if altitudes else None,
        "altitude_median_m": round(median(altitudes)) if altitudes else None,
        "altitude_max_m": round(max(altitudes)) if altitudes else None,
        "region_of_squares": "; ".join(f"{name} {count}" for name, count in regions.most_common()),
        "forest_type": ranked[0][0] if ranked else "unknown",
        "top_species_shares": "; ".join(f"{name} {share:.2f}" for name, share in ranked[:3]),
    }


# ---------------------------------------------------------------------------
# Weather from the bucket files, read once and kept on disk
# ---------------------------------------------------------------------------


def probe_first_day(*, first_day: date, last_day: date, totals: dict[str, int]) -> date:
    """Return the first Rome day from which every needed chunk file exists, without reading any data."""
    first_hour, end_hour = bucket_files.local_window_hours(first_day=first_day, last_day=last_day)
    for chunk in range(first_hour // bucket_files.CHUNK_HOURS, end_hour // bucket_files.CHUNK_HOURS + 1):
        reader = bucket_files.BucketFile(read_ahead_bytes=0)
        try:
            for variable in bucket_files.READ_VARIABLES:
                reader.size(bucket_files.chunk_path(variable=variable, chunk=chunk))
        except RuntimeError as error:
            if "404" not in str(error) and "403" not in str(error):
                raise
            continue
        finally:
            totals["requests"] += reader.requests
        start = datetime.fromtimestamp(chunk * bucket_files.CHUNK_HOURS * 3600, tz=UTC).astimezone(bucket_files.ROME).date()
        # A chunk starts at a UTC hour, so its first Rome day is partly outside it.
        return max(first_day, start + timedelta(days=1))
    raise RuntimeError("No chunk file exists for the weather window.")


def save_weather(*, directory: Path, hours: bucket_files.HoursRead, matches: dict[str, bucket_files.Match], info: dict[str, Any]) -> None:
    arrays: dict[str, np.ndarray] = {}
    keys = list(hours.cells)
    for index, key in enumerate(keys):
        cell = hours.cells[key]
        arrays["timestamps"] = np.array(cell.timestamps, dtype=np.int64)
        for name in HOURLY_VARIABLES:
            arrays[f"c{index}_{name}"] = np.array([np.nan if value is None else value for value in getattr(cell, name)])
    np.savez_compressed(directory / "hours.npz", **arrays)
    meta = {
        **info,
        "cell_keys": keys,
        "matches": {key: asdict(match) for key, match in matches.items()},
        "file_modified": {path: moment.isoformat() for path, moment in hours.file_modified.items()},
    }
    (directory / "hours_meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")


def load_weather(*, directory: Path) -> tuple[bucket_files.HoursRead, dict[str, bucket_files.Match], dict[str, Any]]:
    meta = json.loads((directory / "hours_meta.json").read_text(encoding="utf-8"))
    stored = np.load(directory / "hours.npz")
    timestamps = tuple(int(value) for value in stored["timestamps"])
    cells: dict[str, bucket_files.CellHours] = {}
    for index, key in enumerate(meta["cell_keys"]):
        series = {name: tuple(None if math.isnan(value) else float(value) for value in stored[f"c{index}_{name}"]) for name in HOURLY_VARIABLES}
        cells[key] = bucket_files.CellHours(
            timestamps=timestamps,
            precipitation=series["precipitation"],
            temperature=series["temperature"],
            soil_moisture=series["soil_moisture"],
            soil_temperature=series["soil_temperature"],
            et0=series["et0"],
        )
    matches = {key: bucket_files.Match(**values) for key, values in meta["matches"].items()}
    modified = {path: datetime.fromisoformat(text) for path, text in meta["file_modified"].items()}
    return bucket_files.HoursRead(cells=cells, file_modified=modified), matches, meta


def hour_ranges(*, hours: bucket_files.HoursRead) -> dict[str, Any]:
    """Return, for each cell, the first and last hour that holds a temperature and the number of hours with a gap."""
    result: dict[str, Any] = {}
    for key, cell in hours.cells.items():
        present = [stamp for stamp, value in zip(cell.timestamps, cell.temperature, strict=True) if value is not None]
        result[key] = {
            "first_hour_with_temperature": datetime.fromtimestamp(present[0], tz=UTC).isoformat() if present else None,
            "last_hour_with_temperature": datetime.fromtimestamp(present[-1], tz=UTC).isoformat() if present else None,
            "hours_in_window": len(cell.timestamps),
            "hours_with_any_gap": sum(
                any(getattr(cell, name)[index] is None for name in HOURLY_VARIABLES) for index in range(len(cell.timestamps))
            ),
        }
    return result


def add_missing_cells(
    *,
    settings: dict[str, Any],
    missing: list[dict[str, Any]],
    weather_dir: Path,
    hours: bucket_files.HoursRead,
    matches: dict[str, bucket_files.Match],
    meta: dict[str, Any],
    last_day: date,
) -> tuple[bucket_files.HoursRead, dict[str, bucket_files.Match], dict[str, Any]]:
    """Read only the cells that are not saved yet, with the saved days, and save them together with the old ones."""
    started = time.monotonic()
    saved_first_day = date.fromisoformat(meta["first_day"])
    cells = {place["cell_key"]: (place["cell_latitude"], place["cell_longitude"]) for place in missing}
    print(f"Reading {len(cells)} new cell(s) from the bucket: {', '.join(sorted(cells))}", flush=True)
    source = bucket_files.BucketSource(read_ahead_bytes=settings["read_ahead_bytes"])
    new_matches = bucket_files.match_cells(source=source, cells=cells)
    new_hours = bucket_files.read_cell_hours(
        source=source, matches=new_matches, first_day=saved_first_day, last_day=last_day, threads=settings["threads"]
    )
    saved_timestamps = next(iter(hours.cells.values())).timestamps
    for key, cell in new_hours.cells.items():
        if cell.timestamps != saved_timestamps:
            raise RuntimeError(f"The hours of the new cell {key} differ from the saved hours. Move the saved weather away to read all cells again.")
    extra_requests = source.request_count()
    extra_megabytes = round(source.bytes_read() / 1e6, 2)
    merged = bucket_files.HoursRead(
        cells={**hours.cells, **new_hours.cells}, file_modified={**hours.file_modified, **new_hours.file_modified}
    )
    merged_matches = {**matches, **new_matches}
    meta = {
        **meta,
        "http_requests": meta["http_requests"] + extra_requests,
        "megabytes": round(meta["megabytes"] + extra_megabytes, 2),
        "extra_cells_read": [
            *meta.get("extra_cells_read", []),
            {"cells": sorted(cells), "http_requests": extra_requests, "megabytes": extra_megabytes, "read_at": now_utc(),
             "seconds": round(time.monotonic() - started, 1)}
        ],
        "http_requests_this_run": extra_requests,
        "hour_ranges": {**meta["hour_ranges"], **hour_ranges(hours=new_hours)},
        "from_cache": False,
    }
    save_weather(directory=weather_dir, hours=merged, matches=merged_matches, info={k: v for k, v in meta.items() if k not in ("cell_keys", "matches", "file_modified")})
    return merged, merged_matches, meta


def get_weather(
    *, settings: dict[str, Any], places: list[dict[str, Any]], weather_dir: Path, first_day: date, last_day: date
) -> tuple[bucket_files.HoursRead, dict[str, bucket_files.Match], dict[str, Any]]:
    """Return the hourly weather of every place cell, from the saved files when they match, otherwise from one bucket read."""
    keys = sorted({place["cell_key"] for place in places})
    meta_path = weather_dir / "hours_meta.json"
    if meta_path.is_file() and (weather_dir / "hours.npz").is_file():
        hours, matches, meta = load_weather(directory=weather_dir)
        saved_keys = set(meta["cell_keys"])
        if saved_keys <= set(keys) and meta["last_day"] == last_day.isoformat() and meta["requested_first_day"] == first_day.isoformat():
            missing = [place for place in places if place["cell_key"] not in saved_keys]
            if not missing:
                meta["from_cache"] = True
                meta["http_requests_this_run"] = 0
                print(f"Weather taken from {weather_dir}, no bucket request", flush=True)
                return hours, matches, meta
            return add_missing_cells(
                settings=settings, missing=missing, weather_dir=weather_dir, hours=hours, matches=matches, meta=meta, last_day=last_day
            )
        raise RuntimeError(f"The saved weather in {weather_dir} is for other cells or days. Move it away to read the files again.")

    started = time.monotonic()
    totals = {"requests": 0}
    weather_first = probe_first_day(first_day=first_day, last_day=last_day, totals=totals)
    hour_window = bucket_files.local_window_hours(first_day=weather_first, last_day=last_day)
    files = len(bucket_files.chunk_paths(first_hour=hour_window[0], last_hour=hour_window[1]))
    estimated = len(keys) * files * (settings["read_ahead_bytes"] + 4096)
    print(f"Weather window {weather_first} to {last_day}, {files} chunk files, estimated {estimated / 1e6:.1f} MB", flush=True)
    if estimated > settings["max_read_megabytes"] * 1e6:
        raise RuntimeError(f"The read would download about {estimated / 1e6:.0f} MB, over the limit of {settings['max_read_megabytes']} MB.")

    source = bucket_files.BucketSource(read_ahead_bytes=settings["read_ahead_bytes"])
    matches = bucket_files.match_cells(source=source, cells={place["cell_key"]: (place["cell_latitude"], place["cell_longitude"]) for place in places})
    matched_seconds = time.monotonic() - started
    hours = bucket_files.read_cell_hours(source=source, matches=matches, first_day=weather_first, last_day=last_day, threads=settings["threads"])
    info = {
        "requested_first_day": first_day.isoformat(),
        "first_day": weather_first.isoformat(),
        "last_day": last_day.isoformat(),
        "read_ahead_bytes": settings["read_ahead_bytes"],
        "chunk_files": files,
        "estimated_megabytes": round(estimated / 1e6, 1),
        "http_requests": source.request_count() + totals["requests"],
        "megabytes": round(source.bytes_read() / 1e6, 2),
        "seconds_match_cells": round(matched_seconds, 1),
        "seconds_total": round(time.monotonic() - started, 1),
        "read_at": now_utc(),
        "hour_ranges": hour_ranges(hours=hours),
        "from_cache": False,
    }
    weather_dir.mkdir(parents=True, exist_ok=True)
    save_weather(directory=weather_dir, hours=hours, matches=matches, info=info)
    return hours, matches, info


def daily_rows(*, hours: bucket_files.CellHours, first_day: date, last_day: date, key: str) -> dict[date, Any]:
    """Build in-memory stand-ins for the daily rows of one cell with the app's own aggregation."""
    days = db.aggregate_hourly(
        hourly_timestamps=hours.timestamps,
        variables={name: getattr(hours, name) for name in HOURLY_VARIABLES},
        start=first_day,
        end=last_day,
    )
    return {
        day.date: SimpleNamespace(
            id=f"files:{key}:{day.date.isoformat()}",
            source_id="files:method_outside_tuscany",
            attempt_id="files",
            date=day.date,
            rain_mm=day.rain_mm,
            mean_temperature_c=day.mean_temperature_c,
            min_temperature_c=day.min_temperature_c,
            et0_mm=day.et0_mm,
            mean_soil_temperature_c=day.mean_soil_temperature_c,
            mean_soil_moisture=day.mean_soil_moisture,
            status=day.status,
            metadata=day.metadata,
        )
        for day in days
    }


def is_complete(*, row: Any) -> bool:
    return None not in (row.rain_mm, row.mean_temperature_c, row.et0_mm, row.mean_soil_temperature_c, row.mean_soil_moisture)


# ---------------------------------------------------------------------------
# Finds from GBIF and iNaturalist
# ---------------------------------------------------------------------------


def fetch_gbif_pages(*, settings: dict[str, Any], finds_dir: Path, counts: dict[str, int]) -> list[Path]:
    """Save every page of the GBIF occurrence search, reusing pages already saved."""
    paths: list[Path] = []
    offset = 0
    while True:
        path = finds_dir / f"gbif_{len(paths) + 1:03d}.json"
        if path.exists():
            body = path.read_bytes()
        else:
            query = [
                ("country", settings["gbif_country"]),
                ("hasCoordinate", "true"),
                ("limit", settings["gbif_page_size"]),
                ("offset", offset),
                *[("taxonKey", key) for key in settings["gbif_taxon_keys"]],
            ]
            request = urllib.request.Request(
                f"{gbif.API_URL}/occurrence/search?{urllib.parse.urlencode(query)}", headers={"User-Agent": gbif.USER_AGENT}
            )
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as reply:
                body = reply.read()
            path.write_bytes(body)
            counts["requests"] += 1
            counts["bytes"] += len(body)
            time.sleep(settings["pause_seconds"])
        paths.append(path)
        page = json.loads(body)
        if page["endOfRecords"]:
            return paths
        offset += settings["gbif_page_size"]


def gbif_row(*, occurrence: dict[str, Any]) -> dict[str, str]:
    """Turn one search answer into the text row that the GBIF download parser reads."""
    row = {key: "" if value is None else str(value) for key, value in occurrence.items() if not isinstance(value, (list, dict))}
    row["issue"] = ";".join(occurrence.get("issues") or [])
    return row


def load_finds(*, settings: dict[str, Any], finds_dir: Path, context: RunContext) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Download or reload both sources, drop repeated finds and return those with a full date and a position."""
    finds_dir.mkdir(parents=True, exist_ok=True)
    gbif_counts = {"requests": 0, "bytes": 0}
    saved_before = {path.name for path in finds_dir.iterdir()}
    gbif_paths = fetch_gbif_pages(settings=settings, finds_dir=finds_dir, counts=gbif_counts)
    parse_settings = {"comparison_countries": [], "pistoia_gid": "", "inat_taxon_ids": settings["inat_taxon_ids"], "inat_pistoia_place_id": 0}
    retrieved_at = now_utc()
    gbif_records = [
        gbif.parse_row(row=gbif_row(occurrence=occurrence), settings=parse_settings, retrieved_at=retrieved_at)
        for path in gbif_paths
        for occurrence in json.loads(path.read_text(encoding="utf-8"))["results"]
    ]
    inat_paths = inaturalist.download_place(settings=parse_settings, place_name="italy", place_id=settings["inat_place_id"], raw_dir=finds_dir)
    inat_records = inaturalist.parse_pages(paths=inat_paths, area="home", settings=parse_settings, retrieved_at=retrieved_at)
    new_inat_pages = [path for path in inat_paths if path.name not in saved_before]
    records = inat_records + gbif_records
    mark_duplicates(records=records, max_distance_m=settings["duplicate_distance_m"])
    kept = []
    dropped = Counter()
    for record in records:
        position = (record["latitude"], record["longitude"]) if record["latitude"] is not None else (record["published_latitude"], record["published_longitude"])
        if record["duplicate_rule"]:
            dropped[record["duplicate_rule"]] += 1
        elif record["basis_of_record"] in settings["not_find_kinds"]:
            dropped["not_a_find"] += 1
        elif record["date_precision"] != "day" or not record["event_date"] or record["event_date"] < settings["earliest_find_date"]:
            dropped["no_full_date"] += 1
        elif position[0] is None:
            dropped["no_position"] += 1
        else:
            kept.append({**record, "find_latitude": position[0], "find_longitude": position[1]})
    info = {
        "gbif_records": len(gbif_records),
        "inat_records": len(inat_records),
        "kept": len(kept),
        "kept_with_blurred_position": sum(1 for record in kept if record["coordinates_obscured"]),
        "dropped": dict(dropped),
        "gbif_requests": gbif_counts["requests"],
        "gbif_megabytes": round(gbif_counts["bytes"] / 1e6, 2),
        "inat_requests": len(new_inat_pages),
        "inat_pages": len(inat_paths),
    }
    context.add_source(
        name="GBIF occurrence search, porcini in Italy",
        url=f"{gbif.API_URL}/occurrence/search",
        licence=json.dumps(Counter(record["licence"] for record in gbif_records)),
        version="v1",
        downloaded_at=retrieved_at,
        query={"country": settings["gbif_country"], "taxonKey": settings["gbif_taxon_keys"], "pages": len(gbif_paths), "requests_this_run": gbif_counts["requests"]},
    )
    context.add_source(
        name="iNaturalist observations, porcini in Italy",
        url=inaturalist.API_URL,
        licence=json.dumps(Counter(record["licence"] for record in inat_records)),
        version="v1",
        downloaded_at=retrieved_at,
        query={"place_id": settings["inat_place_id"], "taxon_id": settings["inat_taxon_ids"], "pages": len(inat_paths), "requests_this_run": len(new_inat_pages)},
    )
    for path in [*gbif_paths, *inat_paths]:
        context.add_input(path)
    return kept, info


# ---------------------------------------------------------------------------
# Estimates
# ---------------------------------------------------------------------------


def cold_candidate(*, forecast_inputs: Any) -> tuple[int, float]:
    """Return the v5 score and the cold factor of the mean temperature of the 20 days before the target.

    The temperature factor of v5 is multiplied by the cold factor, and everything else stays as in v5.
    """
    method = db.METHOD_V5
    adjustment = method.temperature_lapse_rate_c_per_m * (forecast_inputs.square_altitude.value - forecast_inputs.reference_altitude.value)
    days = tuple(replace(day, temperature_c=day.temperature_c - adjustment) for day in forecast_inputs.daily_weather)
    temperatures = [day.temperature_c for day in days]
    week = sum(db.temperature_response(temperature_c=value) for value in temperatures[-7:]) / 7
    # Bielefeld B. edulis preprint, table S1: the 20-day mean temperature has a quadratic cold penalty,
    # so a warm last week cannot hide a cold fortnight before it. Peak and curvature are those of v2.
    mean_20 = sum(temperatures[-20:]) / 20
    peak, curvature = db.METHOD_V2.temperature_peak_c, db.METHOD_V2.cold_curve_coefficient
    cold = math.exp(-curvature * (mean_20 - peak) ** 2) if mean_20 < peak else 1.0
    moisture = db._moisture(days=days, initial_mm=40.0, maximum_drying_mm=method.maximum_drying_mm)[0]
    return db._score(moisture=moisture, temperature=week * cold), cold


def estimate_place(
    *,
    place: dict[str, Any],
    inputs: list[tuple[Any, Any, Any]],
    rows: dict[date, Any],
    elevation: float | None,
    terrain_id: Any,
    target_days: list[date],
) -> list[dict[str, Any]]:
    """Calculate the v5 and the cold candidate estimates of every square for every target day, as on that day with zero days ahead."""
    results: list[dict[str, Any]] = []
    for day in target_days:
        snapshot = db.WeatherSnapshot(
            cell_key=place["cell_key"], assessment_date=day, reference_elevation_m=elevation, pipeline_version=db.PIPELINE_VERSION
        )
        known = {key: row for key, row in rows.items() if key <= day}
        summary = db.summary_services._make_summary(snapshot=snapshot, assessment_date=day, target_date=day, rows=known)
        percentages_v5: list[int] = []
        percentages_cold: list[int] = []
        factors_v5: dict[str, list[float]] = defaultdict(list)
        factors_cold: dict[str, list[float]] = defaultdict(list)
        for forest_cell, terrain_cell, host_trees in inputs:
            forecast_inputs = db.estimate_services._forecast_inputs(
                snapshot=snapshot,
                summary=summary,
                forest_cell=forest_cell,
                terrain_cell=terrain_cell,
                terrain_build_id=terrain_id,
                daily_rows=known,
                host_trees=host_trees,
            )
            forecast = db.calculate_forecast(inputs=forecast_inputs, method=db.METHOD_V5)
            if forecast.whole_percentage is None:
                continue
            percentages_v5.append(forecast.whole_percentage)
            cold_score, cold = cold_candidate(forecast_inputs=forecast_inputs)
            percentages_cold.append(cold_score)
            # v5 shows host trees as facts but leaves them out of the score, so only scored factors are kept.
            for fact in forecast.ordered_facts:
                if fact.normalized_factor is not None:
                    factors_v5[fact.role_key].append(fact.normalized_factor)
                    factors_cold[fact.role_key].append(fact.normalized_factor)
                    if fact.role_key == "corrected_mean_temperature":
                        factors_cold["mean_temperature_20_days"].append(cold)
        results.append(
            {
                "date": day,
                "percentages_v5": percentages_v5,
                "percentages_cold": percentages_cold,
                "factors_v5": {role: float(median(values)) for role, values in factors_v5.items()},
                "factors_cold": {role: float(median(values)) for role, values in factors_cold.items()},
            }
        )
    return results


def write_csv(*, rows: list[dict[str, Any]], columns: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def rounded(value: float | None, digits: int = 2) -> float | str:
    return "" if value is None else round(float(value), digits)


def month_label(*, day: date) -> str:
    return f"{day.year}-{day.month:02d}"


def run(context: RunContext) -> None:
    """Estimate every day of the last 12 months at 11 places and write the four tables."""
    settings = context.config
    started = time.monotonic()
    first_day = date.fromisoformat(settings["first_day"])
    last_day = date.fromisoformat(settings["last_day"])
    summary: dict[str, Any] = {"phases_seconds": {}}
    summary_path = context.output_dir / "summary.json"

    def save_summary() -> None:
        summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    start_django()
    assert_read_only()

    phase = time.monotonic()
    places = choose_places(settings=settings)
    terrain_id = terrain_build_id()
    summary["terrain_build_id"] = str(terrain_id)
    place_inputs = {place["name"]: load_square_inputs(place=place, terrain_id=terrain_id) for place in places}
    summary["phases_seconds"]["places"] = round(time.monotonic() - phase, 1)
    save_summary()

    phase = time.monotonic()
    weather_dir = context.raw_dir
    hours, matches, weather_info = get_weather(settings=settings, places=places, weather_dir=weather_dir, first_day=first_day - timedelta(days=settings["history_days"]), last_day=last_day)
    weather_first = date.fromisoformat(weather_info["first_day"])
    # A target day needs the history days before it.
    target_first = max(first_day, weather_first + timedelta(days=settings["history_days"]))
    target_days = [target_first + timedelta(days=offset) for offset in range((last_day - target_first).days + 1)]
    summary["weather"] = {key: value for key, value in weather_info.items() if key not in ("cell_keys", "matches", "file_modified")}
    summary["target_days"] = {"first": target_first.isoformat(), "last": last_day.isoformat(), "count": len(target_days)}
    summary["phases_seconds"]["weather"] = round(time.monotonic() - phase, 1)
    save_summary()
    context.add_source(
        name="Open-Meteo open data on AWS, ecmwf_ifs chunk files",
        url=f"https://{bucket_files.BUCKET_HOST}/{bucket_files.DOMAIN_PATH}",
        licence="CC BY 4.0",
        version=f"files read {weather_info['read_at']}",
        downloaded_at=weather_info["read_at"],
        query={
            "variables": list(bucket_files.READ_VARIABLES),
            "weather_first_day": weather_info["first_day"],
            "weather_last_day": weather_info["last_day"],
            "read_ahead_bytes": weather_info["read_ahead_bytes"],
            "chunk_files": weather_info["chunk_files"],
            "http_requests_of_the_read": weather_info["http_requests"],
            "megabytes_of_the_read": weather_info["megabytes"],
            "seconds_of_the_read": weather_info["seconds_total"],
            "http_requests_this_run": weather_info.get("http_requests_this_run", 0 if weather_info["from_cache"] else weather_info["http_requests"]),
            "hour_ranges": weather_info["hour_ranges"],
        },
    )
    for name in ("hours.npz", "hours_meta.json"):
        context.add_input(weather_dir / name)

    phase = time.monotonic()
    finds, finds_info = load_finds(settings=settings, finds_dir=context.raw_dir / "finds", context=context)
    summary["finds"] = finds_info
    summary["phases_seconds"]["finds"] = round(time.monotonic() - phase, 1)
    save_summary()
    print(f"Finds: {finds_info['kept']} kept of {finds_info['gbif_records']} GBIF and {finds_info['inat_records']} iNaturalist records", flush=True)

    place_rows: list[dict[str, Any]] = []
    day_rows: list[dict[str, Any]] = []
    month_rows: list[dict[str, Any]] = []
    roles_v5: list[str] = []
    roles_cold: list[str] = []
    per_place_days: dict[str, list[dict[str, Any]]] = {}
    per_place_rows: dict[str, dict[date, Any]] = {}
    phase = time.monotonic()
    for place in places:
        key = place["cell_key"]
        rows = daily_rows(hours=hours.cells[key], first_day=weather_first, last_day=last_day, key=key)
        per_place_rows[place["name"]] = rows
        match = matches[key]
        elevation = None if math.isnan(match.place_height) else match.place_height
        place_started = time.monotonic()
        results = estimate_place(
            place=place, inputs=place_inputs[place["name"]], rows=rows, elevation=elevation, terrain_id=terrain_id, target_days=target_days
        )
        print(f"{place['name']}: {len(target_days)} days estimated in {time.monotonic() - place_started:.0f} s", flush=True)
        for result in results:
            for role in result["factors_v5"]:
                if role not in roles_v5:
                    roles_v5.append(role)
            for role in result["factors_cold"]:
                if role not in roles_cold:
                    roles_cold.append(role)
        incomplete_weather = [day for day in target_days if not is_complete(row=rows[day])]
        no_estimate = [result["date"] for result in results if not result["percentages_cold"]]
        description = describe_squares(inputs=place_inputs[place["name"]])
        place_rows.append(
            {
                "place": place["name"],
                "region": place["region"],
                "region_of_squares": description["region_of_squares"],
                "cell_key": key,
                "distance_km": place["distance_km"],
                "squares": len(place["squares"]),
                "place_height_m": rounded(elevation, 0),
                "grid_point_height_m": rounded(match.point_height, 0),
                "height_correction_c": rounded(match.correction, 2),
                "altitude_min_m": description["altitude_min_m"],
                "altitude_median_m": description["altitude_median_m"],
                "altitude_max_m": description["altitude_max_m"],
                "forest_type": description["forest_type"],
                "top_species_shares": description["top_species_shares"],
                "days_without_weather": len(incomplete_weather),
                "days_without_estimate": len(no_estimate),
            }
        )
        summary.setdefault("days_without_weather", {})[place["name"]] = [day.isoformat() for day in incomplete_weather]
        days: list[dict[str, Any]] = []
        for result in results:
            row = rows[result["date"]]
            values_v5 = result["percentages_v5"]
            values_cold = result["percentages_cold"]
            if len(values_v5) != len(values_cold):
                raise RuntimeError(f"v5 and the cold candidate estimate a different number of squares at {place['name']} on {result['date']}.")
            days.append(
                {
                    "place": place["name"],
                    "date": result["date"].isoformat(),
                    "squares_with_estimate": len(values_cold),
                    "estimate_median_v5": rounded(float(np.median(values_v5)), 1) if values_v5 else "",
                    "estimate_p90_v5": rounded(float(np.percentile(values_v5, 90)), 1) if values_v5 else "",
                    "estimate_max_v5": max(values_v5) if values_v5 else "",
                    "estimate_median_cold": rounded(float(np.median(values_cold)), 1) if values_cold else "",
                    "estimate_p90_cold": rounded(float(np.percentile(values_cold, 90)), 1) if values_cold else "",
                    "estimate_max_cold": max(values_cold) if values_cold else "",
                    "_factors_v5": result["factors_v5"],
                    "_factors_cold": result["factors_cold"],
                    "mean_temperature_c": rounded(row.mean_temperature_c),
                    "min_temperature_c": rounded(row.min_temperature_c),
                    "rain_mm": rounded(row.rain_mm),
                    "soil_moisture": rounded(row.mean_soil_moisture, 3),
                }
            )
        per_place_days[place["name"]] = days
        save_summary()
    summary["phases_seconds"]["estimates"] = round(time.monotonic() - phase, 1)

    radius = settings["find_radius_km"]
    find_rows: list[dict[str, Any]] = []
    for place in places:
        nearby = [
            record
            for record in finds
            if distance_km(
                latitude_a=place["cell_latitude"],
                longitude_a=place["cell_longitude"],
                latitude_b=record["find_latitude"],
                longitude_b=record["find_longitude"],
            )
            <= radius
        ]
        in_window = Counter(record["event_date"][:7] for record in nearby if target_days[0].isoformat() <= record["event_date"] <= last_day.isoformat())
        by_month_of_year = Counter(int(record["event_date"][5:7]) for record in nearby)
        summary.setdefault("finds_nearby", {})[place["name"]] = len(nearby)
        place_days = {day["date"]: day for day in per_place_days[place["name"]]}
        place_weather = per_place_rows[place["name"]]
        for record in sorted(nearby, key=lambda item: item["event_date"]):
            found_on = record["event_date"]
            if found_on not in place_days:
                continue
            day = place_days[found_on]
            found_date = date.fromisoformat(found_on)
            before = [place_weather[found_date - timedelta(days=offset)].rain_mm for offset in range(1, 31) if found_date - timedelta(days=offset) in place_weather]
            find_rows.append(
                {
                    "place": place["name"],
                    "find_date": found_on,
                    "estimate_median_v5": day["estimate_median_v5"],
                    "estimate_median_cold": day["estimate_median_cold"],
                    "factor_retained_water_v5": rounded(day["_factors_v5"].get("retained_water"), 3),
                    "factor_retained_water_cold": rounded(day["_factors_cold"].get("retained_water"), 3),
                    "factor_temperature_7_days_v5": rounded(day["_factors_v5"].get("corrected_mean_temperature"), 3),
                    "factor_temperature_7_days_cold": rounded(day["_factors_cold"].get("corrected_mean_temperature"), 3),
                    "factor_mean_temperature_20_days_cold": rounded(day["_factors_cold"].get("mean_temperature_20_days"), 3),
                    "rain_mm_on_the_day": day["rain_mm"],
                    "rain_mm_in_the_30_days_before": rounded(sum(value for value in before if value is not None), 1) if len(before) == 30 else "",
                }
            )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for day in per_place_days[place["name"]]:
            grouped[day["date"][:7]].append(day)
            day_rows.append(
                {
                    **{k: v for k, v in day.items() if not k.startswith("_")},
                    **{f"factor_{role}_v5": rounded(day["_factors_v5"].get(role), 3) for role in roles_v5},
                    **{f"factor_{role}_cold": rounded(day["_factors_cold"].get(role), 3) for role in roles_cold},
                }
            )
        for label, days in grouped.items():
            medians_v5 = [day["estimate_median_v5"] for day in days if day["estimate_median_v5"] != ""]
            medians_cold = [day["estimate_median_cold"] for day in days if day["estimate_median_cold"] != ""]
            temperatures = [day["mean_temperature_c"] for day in days if day["mean_temperature_c"] != ""]
            minimums = [day["min_temperature_c"] for day in days if day["min_temperature_c"] != ""]
            rain = [day["rain_mm"] for day in days if day["rain_mm"] != ""]
            month_rows.append(
                {
                    "place": place["name"],
                    "month": label,
                    "days": len(days),
                    "days_with_estimate": len(medians_cold),
                    "mean_daily_median_estimate_v5": rounded(sum(medians_v5) / len(medians_v5), 1) if medians_v5 else "",
                    "days_median_50_or_more_v5": sum(1 for value in medians_v5 if value >= 50),
                    "days_median_30_or_more_v5": sum(1 for value in medians_v5 if value >= 30),
                    "mean_daily_median_estimate_cold": rounded(sum(medians_cold) / len(medians_cold), 1) if medians_cold else "",
                    "days_median_50_or_more_cold": sum(1 for value in medians_cold if value >= 50),
                    "days_median_30_or_more_cold": sum(1 for value in medians_cold if value >= 30),
                    "mean_temperature_c": rounded(sum(temperatures) / len(temperatures), 1) if temperatures else "",
                    "frost_days": sum(1 for value in minimums if value < 0),
                    "rain_mm": rounded(sum(rain), 0) if rain else "",
                    "finds_nearby_in_this_month": in_window.get(label, 0),
                    "finds_nearby_in_all_years_for_this_month_of_the_year": by_month_of_year.get(int(label[5:]), 0),
                }
            )
    day_columns = [
        "place",
        "date",
        "squares_with_estimate",
        "estimate_median_v5",
        "estimate_p90_v5",
        "estimate_max_v5",
        "estimate_median_cold",
        "estimate_p90_cold",
        "estimate_max_cold",
        *[f"factor_{role}_v5" for role in roles_v5],
        *[f"factor_{role}_cold" for role in roles_cold],
        "mean_temperature_c",
        "min_temperature_c",
        "rain_mm",
        "soil_moisture",
    ]
    month_columns = list(month_rows[0])
    find_path = REPORTS_DIR / "method_outside_tuscany_find_days.csv"
    write_csv(rows=find_rows, columns=list(FIND_COLUMNS), path=find_path)
    context.add_output(find_path)
    for name, rows_out, columns in (
        ("places", place_rows, PLACE_COLUMNS),
        ("days", day_rows, day_columns),
        ("months", month_rows, month_columns),
    ):
        path = REPORTS_DIR / f"method_outside_tuscany_{name}.csv"
        write_csv(rows=rows_out, columns=columns, path=path)
        context.add_output(path)

    summary["seconds_total"] = round(time.monotonic() - started, 1)
    save_summary()
    context.add_output(summary_path)
    assert_read_only()
