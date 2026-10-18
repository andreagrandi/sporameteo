"""One phase of the Italy night, run inside the app container.

The orchestrator in run.py starts one process per phase. The last line printed on stdout is one JSON object with the result.
"""

import argparse
import json
import os
import resource
import sys
import time
import tracemalloc
import traceback
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

# In the container the project is in /app. On the laptop the project is two folders above this file.
APPLICATION = Path("/app/apps/application") if Path("/app/apps/application").is_dir() else Path(__file__).resolve().parents[2] / "apps" / "application"
CGROUP_PEAK = Path("/sys/fs/cgroup/memory.peak")
ITALY_REGION_CODE = "IT"
TUSCANY_CODE = "09"
PROGRESS_EVERY_CELLS = 100


def peak_memory_mb() -> dict[str, float | None]:
    """Return the peak memory of this process, and the peak of its container when the system shows it."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes and Linux reports kilobytes.
    process_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    container_mb = None
    try:
        container_mb = round(int(CGROUP_PEAK.read_text(encoding="utf-8").strip()) / (1024 * 1024), 1)
    except (OSError, ValueError):
        pass
    return {"process_mb": round(process_mb, 1), "container_mb": container_mb}


def process_mb() -> float:
    return peak_memory_mb()["process_mb"]


def start_django() -> None:
    if str(APPLICATION) not in sys.path:
        sys.path.insert(0, str(APPLICATION))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    import django

    django.setup()


def square_counts(*, forest_build: Any, region_code: str) -> dict[str, int]:
    """Return the forest squares of each weather cell, for one region or for all Italy when the code is empty."""
    from django.db.models import Count

    from national_forest.models import ForestCell
    from weather_requests.eligibility import weather_cell

    squares = ForestCell.objects.filter(build=forest_build, is_forest=True)
    if region_code:
        squares = squares.filter(grid_build_cell__owner_region__istat_code=region_code)
    rows = squares.order_by().values_list("weather_latitude", "weather_longitude").annotate(squares=Count("id"))
    counts: dict[str, int] = {}
    for latitude, longitude, count in rows:
        key = weather_cell(latitude=latitude, longitude=longitude).key
        counts[key] = counts.get(key, 0) + count
    return counts


def choose_cells(*, region: str, forest_build_version: str, limit: int) -> list[Any]:
    """Return the weather cells of the region sorted by key, cut to the limit when it is above zero."""
    from weather_requests.eligibility import weather_cells_for_forest_build, weather_cells_for_regions

    if region == "all":
        cells = weather_cells_for_forest_build(build_version=forest_build_version)
    else:
        cells = weather_cells_for_regions(forest_build_version=forest_build_version, region_codes=[region])
    ordered = sorted(cells, key=lambda cell: cell.key)
    return ordered[:limit] if limit > 0 else ordered


def run_weather(*, args: argparse.Namespace) -> dict[str, Any]:
    """Read the weather files for the cells and store the days and snapshots, as the nightly weather read does."""
    from weather_summaries.file_weather import plan_file_weather, store_file_weather

    assessment_date = date.fromisoformat(args.assessment_date)
    cells = choose_cells(region=args.region, forest_build_version=args.forest_build_version, limit=args.limit_cells)
    print(f"Weather cells: {len(cells)}", flush=True)
    plan = plan_file_weather(cells=cells, assessment_date=assessment_date)
    plan_data = {**plan.as_dict(), "estimated_bytes": plan.estimated_bytes}
    print(f"Plan: {json.dumps(plan_data)}", flush=True)
    started = time.monotonic()
    result: dict[str, Any] = {"cells": len(cells), "plan": plan_data}
    try:
        read = store_file_weather(cells=cells, assessment_date=assessment_date, region_code=ITALY_REGION_CODE)
    except ValueError as error:
        result.update({"status": "refused", "error": str(error), "seconds": round(time.monotonic() - started, 1)})
        return result
    result.update(
        {
            "status": read.status,
            "error": read.error,
            "seconds": round(time.monotonic() - started, 1),
            "bytes_read": read.bytes_read,
            "http_requests": read.http_requests,
            "cells_written": read.cells_written,
            "cells_already_ready": read.cells_already_ready,
            "cells_missing": len(read.missing),
            "missing_examples": dict(list(read.missing.items())[:5]),
            "run_time": read.run_time.isoformat() if read.run_time else None,
        }
    )
    return result


def run_estimates(*, args: argparse.Namespace) -> dict[str, Any]:
    """Calculate and store the estimates of every cell in cell key order, as one forecast run does, for any region."""
    from django.utils import timezone

    from estimate_updates.forecast_runs import _builds, _target_dates, _tree_source_matches
    from estimate_updates.host_trees import calculation_version, host_tree_source_version
    from estimate_updates.models import EstimateResult, ForecastRun, ForecastRunCell
    from estimate_updates.services import calculate_estimate_update, publish_estimate_update
    from sporameteo.forecast_method import CURRENT_METHOD
    from sporameteo.weather.policy import PIPELINE_VERSION
    from weather_summaries.models import WeatherSnapshot

    assessment_date = date.fromisoformat(args.assessment_date)
    forest, terrain = _builds()
    cells = choose_cells(region=args.region, forest_build_version=forest.version, limit=args.limit_cells)
    area_code = "" if args.region == "all" else args.region
    counts = square_counts(forest_build=forest, region_code=area_code)
    snapshots = dict(
        WeatherSnapshot.objects.filter(
            cell_key__in=[cell.key for cell in cells],
            assessment_date=assessment_date,
            pipeline_version=PIPELINE_VERSION,
            status=WeatherSnapshot.Status.READY,
        ).values_list("cell_key", "id")
    )
    planned = [cell for cell in cells if cell.key in snapshots]
    without_weather = [cell.key for cell in cells if cell.key not in snapshots]
    print(f"Cells {len(cells)}, with weather {len(planned)}, squares {sum(counts.get(cell.key, 0) for cell in planned)}", flush=True)

    run = ForecastRun.objects.create(
        region_code=ITALY_REGION_CODE if args.region == "all" else args.region,
        assessment_date=assessment_date,
        forest_build=forest,
        terrain_build=terrain,
        calculation_version=calculation_version(),
        host_tree_source_version=host_tree_source_version(),
        method_version=CURRENT_METHOD.version,
        target_dates=[value.isoformat() for value in _target_dates(assessment_date=assessment_date)],
        expected_cell_count=len(planned),
        expected_square_count=sum(counts.get(cell.key, 0) for cell in planned),
        started_at=timezone.now(),
    )
    ForecastRunCell.objects.bulk_create(
        [
            ForecastRunCell(
                run=run, cell_key=cell.key, snapshot_id=snapshots[cell.key], expected_square_count=counts.get(cell.key, 0)
            )
            for cell in planned
        ]
    )

    rows_path = Path(args.output_dir) / "cells" / f"{args.label}.jsonl"
    rows_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    squares = 0
    failures: list[dict[str, str]] = []
    peak = 0.0
    peak_cell = ""
    with rows_path.open("w", encoding="utf-8") as rows_file:
        for number, cell in enumerate(run.cells.order_by("cell_key"), start=1):
            cell_started = time.monotonic()
            state = "completed"
            error = ""
            row_count = 0
            try:
                prepared = calculate_estimate_update(
                    snapshot_id=cell.snapshot_id,
                    cell_key=cell.cell_key,
                    assessment_date=assessment_date,
                    forest_build_version=forest.version,
                    terrain_build_version=terrain.version,
                    region_code=area_code,
                )
                if not _tree_source_matches(run=run, tree_inputs=prepared.tree_inputs):
                    raise RuntimeError("Host tree values did not match the source of the run.")
                published = publish_estimate_update(prepared=prepared)
                if published.skipped_older:
                    raise RuntimeError("A newer estimate for this cell already exists.")
                row_count = prepared.row_count
                squares += row_count
                ForecastRunCell.objects.filter(id=cell.id).update(
                    state=ForecastRunCell.State.COMPLETED, update=published.update, failure_reason=""
                )
            except Exception as exc:
                state = "failed"
                error = f"{type(exc).__name__}: {exc}"
                ForecastRunCell.objects.filter(id=cell.id).update(state=ForecastRunCell.State.FAILED, failure_reason=error)
                failures.append({"cell_key": cell.cell_key, "error": error})
            memory = process_mb()
            if memory > peak:
                peak, peak_cell = memory, cell.cell_key
            rows_file.write(
                json.dumps(
                    {
                        "cell_key": cell.cell_key,
                        "squares": row_count,
                        "expected_squares": cell.expected_square_count,
                        "seconds": round(time.monotonic() - cell_started, 3),
                        "process_mb": memory,
                        "state": state,
                        "error": error,
                    }
                )
                + "\n"
            )
            rows_file.flush()
            if number % PROGRESS_EVERY_CELLS == 0:
                print(f"{number} cells, {squares} squares, {time.monotonic() - started:.0f} s, {memory} MB", flush=True)

    seconds = time.monotonic() - started
    memory = peak_memory_mb()
    completed = run.cells.filter(state=ForecastRunCell.State.COMPLETED).count()
    ForecastRun.objects.filter(id=run.id).update(
        status=ForecastRun.Status.FAILED if failures else ForecastRun.Status.COMPLETED,
        failure_reason=f"{len(failures)} weather cells failed." if failures else "",
        completed_cell_count=completed,
        completed_at=None if failures else timezone.now(),
        runtime_seconds=seconds,
        peak_memory_mb=memory["process_mb"],
    )
    return {
        "status": "failed" if failures else "completed",
        "run_id": str(run.id),
        "region": args.region,
        "label": args.label,
        "cells": len(planned),
        "cells_completed": completed,
        "cells_without_weather": len(without_weather),
        "cells_without_weather_examples": without_weather[:5],
        "squares": squares,
        "result_rows": EstimateResult.objects.filter(update__forecast_run_cells__run=run).count(),
        "seconds": round(seconds, 1),
        "peak_memory": memory,
        "peak_reached_at_cell": peak_cell,
        "failures": len(failures),
        "failure_examples": failures[:5],
        "cells_file": str(rows_path),
    }


def newest_completed_run(*, region: str) -> Any:
    """Describe the newest completed forecast run of the region as the run that readers use, or return None."""
    from estimate_updates.models import ForecastRun
    from map_api.published import PublishedRun

    region_code = ITALY_REGION_CODE if region == "all" else region
    forest_run = (
        ForecastRun.objects.select_related("forest_build")
        .filter(region_code=region_code, status=ForecastRun.Status.COMPLETED)
        .order_by("-created_at")
        .first()
    )
    if forest_run is None:
        return None
    return PublishedRun(
        run_id=str(forest_run.id),
        assessment_date=forest_run.assessment_date,
        published_at=datetime.now(tz=UTC),
        target_dates=tuple(date.fromisoformat(value) for value in forest_run.target_dates),
        method_version=forest_run.method_version,
        forest_build_id=forest_run.forest_build_id,
        forest_build_version=forest_run.forest_build.version,
        grid_build_id=forest_run.forest_build.grid_build_id,
        previous_run_id=None,
    )


def run_warmup(*, args: argparse.Namespace) -> dict[str, Any]:
    """Prepare the run summary and the overview points for the newest completed run, as the warm-up after publishing does."""
    from django.db import connection

    from map_api import published

    run = newest_completed_run(region=args.region)
    if run is None:
        return {"status": "no_run", "error": f"No completed run for region {args.region}."}
    table = published.map_cache()._table
    with connection.cursor() as cursor:
        if args.mode == "cold":
            cursor.execute(f"TRUNCATE {table}")
        else:
            # Static tiles are keyed by forest build, so they survive from one night to the next.
            cursor.execute(f"DELETE FROM {table} WHERE cache_key NOT LIKE %s", ["%:static:%"])
        cursor.execute(f"SELECT count(*) FROM {table}")
        entries_before = cursor.fetchone()[0]

    stages: dict[str, dict[str, float]] = {}

    def mark(stage: str) -> None:
        """Record the process peak so far, and the Python allocations since the last mark when tracing."""
        stages[stage] = {"process_peak_mb": process_mb()}
        if args.trace_memory:
            current, peak = tracemalloc.get_traced_memory()
            stages[stage].update({"python_current_mb": round(current / 1e6, 1), "python_peak_since_last_mark_mb": round(peak / 1e6, 1)})
            tracemalloc.reset_peak()

    if args.trace_memory:
        tracemalloc.start()
    mark("start")
    started = time.monotonic()
    summary = published.run_summary(run=run)
    summary_seconds = time.monotonic() - started
    mark("after_run_summary")
    points = published.overview_points(run=run, summary=summary)
    seconds = time.monotonic() - started
    mark("after_overview_points")
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {table}")
        entries_after = cursor.fetchone()[0]
    return {
        "status": "completed",
        "mode": args.mode,
        "region": args.region,
        "run_id": run.run_id,
        "squares": len(points.corner_e),
        "seconds": round(seconds, 1),
        "seconds_summary": round(summary_seconds, 1),
        "seconds_overview": round(seconds - summary_seconds, 1),
        "memory_stages": stages,
        "cache_entries_before": entries_before,
        "cache_entries_after": entries_after,
        "peak_memory": peak_memory_mb(),
    }


def run_requests(*, args: argparse.Namespace) -> dict[str, Any]:
    """Time the first and second call of the map reads after a warm-up, with no tile in the cache."""
    from django.db import connection

    from map_api import published
    from map_api.api import MAX_SQUARES

    run = newest_completed_run(region=args.region)
    if run is None:
        return {"status": "no_run", "error": f"No completed run for region {args.region}."}
    table = published.map_cache()._table
    with connection.cursor() as cursor:
        # A cold start: keep only what the warm-up prepares.
        cursor.execute(f"DELETE FROM {table} WHERE cache_key NOT LIKE %s AND cache_key NOT LIKE %s", ["%:overview-points:%", "%:summary:%"])
    summary = published.run_summary(run=run)
    published.overview_points(run=run, summary=summary)
    # About 15 km wide and 30 km high, a phone held upright.
    views = {
        "tuscany_phone": (11.11, 43.31, 11.29, 43.58),
        "piemonte_phone": (7.10, 44.97, 7.29, 45.24),
    }
    results: dict[str, Any] = {}
    for name, bbox in views.items():
        calls = []
        for _ in range(2):
            started = time.monotonic()
            try:
                body = published.read_squares(run=run, target_date=run.assessment_date, bbox=bbox, max_squares=MAX_SQUARES)
                outcome = f"{len(body.content)} bytes"
            except published.MapReadError as error:
                outcome = f"{error.code}"
            calls.append({"seconds": round(time.monotonic() - started, 4), "outcome": outcome})
            # The response body cache would hide the tile work in the second call, which is the point of the comparison.
        results[name] = calls
    # Where the squares read refuses a region, time the tile work it would do.
    tiles = published._tiles_in(bounds=published._bbox_polygon(bbox=views["piemonte_phone"]).bounds)
    tile_calls = []
    for _ in range(2):
        started = time.monotonic()
        static = published.static_tiles(run=run, tiles=tiles)
        published.run_tiles(run=run, summary=summary, static=static, tiles=tiles)
        tile_calls.append(round(time.monotonic() - started, 4))
    results["piemonte_tiles_only"] = {"tiles": len(tiles), "seconds_first_and_second": tile_calls}
    # The whole of Italy zoomed out, as the app asks for it.
    overview = []
    for _ in range(2):
        started = time.monotonic()
        body = published.read_overview(run=run, target_date=run.assessment_date, bbox=(6.6, 35.5, 18.8, 47.1), size=17000)
        overview.append({"seconds": round(time.monotonic() - started, 4), "bytes": len(body.content)})
    results["italy_overview"] = overview
    return {"status": "completed", "views": results, "peak_memory": peak_memory_mb()}


def run_rewind(*, args: argparse.Namespace) -> dict[str, Any]:
    """Delete the snapshots, daily rows and file reads of one assessment date, so its night can be read again."""
    from django.db.models import F

    from weather_summaries.models import FileWeatherRead, WeatherDaily, WeatherSnapshot

    assessment_date = date.fromisoformat(args.assessment_date)
    read_ids = list(FileWeatherRead.objects.filter(assessment_date=assessment_date).values_list("id", flat=True))
    snapshots = WeatherSnapshot.objects.filter(assessment_date=assessment_date).delete()[0]
    daily = WeatherDaily.objects.filter(attempt_id__in=read_ids).delete()[0]
    reads = FileWeatherRead.objects.filter(id__in=read_ids).delete()[0]
    # A real night finds forecast days of the run before, which the newest run replaces. Move the stored forecast days
    # of the previous night back one day, so this night reads them again as it would with a new run.
    previous_start = datetime(assessment_date.year, assessment_date.month, assessment_date.day, tzinfo=UTC) - timedelta(days=1)
    aged = WeatherDaily.objects.filter(forecast_issue_time__gte=previous_start).update(
        forecast_issue_time=F("forecast_issue_time") - timedelta(days=1)
    )
    return {
        "status": "completed",
        "deleted_snapshot_objects": snapshots,
        "deleted_daily_rows": daily,
        "deleted_reads": reads,
        "forecast_rows_marked_as_older_run": aged,
    }


def run_compare(*, args: argparse.Namespace) -> dict[str, Any]:
    """Calculate cells with the square by square path and the cell path and compare the rows, without writing any estimate."""
    import random

    from estimate_updates.forecast_runs import _builds
    from estimate_updates.contracts import decode_result_bytes
    from estimate_updates.services import calculate_estimate_update
    from sporameteo.forecast_method import calculate_forecast
    from sporameteo.weather.policy import PIPELINE_VERSION
    from weather_requests.eligibility import weather_cells_for_regions
    from weather_summaries.models import WeatherSnapshot

    assessment_date = date.fromisoformat(args.assessment_date)
    forest, terrain = _builds()
    snapshots = dict(
        WeatherSnapshot.objects.filter(
            assessment_date=assessment_date, pipeline_version=PIPELINE_VERSION, status=WeatherSnapshot.Status.READY
        ).values_list("cell_key", "id")
    )
    tuscany = sorted(cell.key for cell in weather_cells_for_regions(forest_build_version=forest.version, region_codes=[TUSCANY_CODE]))
    others = sorted(key for key in snapshots if key not in set(tuscany))
    chosen = [(key, TUSCANY_CODE) for key in tuscany if key in snapshots]
    chosen += [(key, "") for key in sorted(random.Random(args.seed).sample(others, min(args.random_cells, len(others))))]
    print(f"Comparing {len(chosen)} cells, {len(tuscany)} in Tuscany", flush=True)

    def calculate(*, key: str, region_code: str, old: bool) -> tuple[Any, float]:
        started = time.monotonic()
        prepared = calculate_estimate_update(
            snapshot_id=snapshots[key],
            cell_key=key,
            assessment_date=assessment_date,
            forest_build_version=forest.version,
            terrain_build_version=terrain.version,
            calculator=(lambda inputs: calculate_forecast(inputs=inputs)) if old else None,
            region_code=region_code,
        )
        return prepared, time.monotonic() - started

    seconds_old = seconds_new = 0.0
    differing_days = reason_mismatches = largest_difference = 0
    squares = 0
    mismatches: list[dict[str, Any]] = []
    for number, (key, region_code) in enumerate(chosen, start=1):
        # The first call fills the host tree cache, so both timed calls below read the same cached values.
        calculate(key=key, region_code=region_code, old=False)
        old, took_old = calculate(key=key, region_code=region_code, old=True)
        new, took_new = calculate(key=key, region_code=region_code, old=False)
        seconds_old += took_old
        seconds_new += took_new
        squares += len(old.rows)
        problem = None
        if [row.forest_cell_id for row in old.rows] != [row.forest_cell_id for row in new.rows]:
            problem = "different forest cells"
        else:
            first = None
            for a, b in zip(old.rows, new.rows, strict=True):
                if bytes(a.values) == bytes(b.values):
                    continue
                first = first or a.forest_cell_id
                for before, after in zip(decode_result_bytes(values=a.values), decode_result_bytes(values=b.values), strict=True):
                    if before == after:
                        continue
                    differing_days += 1
                    if isinstance(before, int) and isinstance(after, int):
                        largest_difference = max(largest_difference, abs(before - after))
                    else:
                        reason_mismatches += 1
            if first is not None:
                problem = f"first differing square {first}"
            elif old.tree_inputs != new.tree_inputs:
                problem = "different tree_inputs"
            elif old.input_fingerprint != new.input_fingerprint:
                problem = "different input fingerprint"
        if problem:
            mismatches.append({"cell_key": key, "problem": problem})
        if number % PROGRESS_EVERY_CELLS == 0:
            print(f"{number} cells, old {seconds_old:.1f} s, new {seconds_new:.1f} s, mismatches {len(mismatches)}", flush=True)
    return {
        "status": "completed" if not mismatches else "mismatch",
        "cells": len(chosen),
        "cells_tuscany": len([1 for _, code in chosen if code]),
        "squares": squares,
        "seconds_old": round(seconds_old, 2),
        "seconds_new": round(seconds_new, 2),
        "seconds_per_cell_old": round(seconds_old / len(chosen), 4),
        "seconds_per_cell_new": round(seconds_new / len(chosen), 4),
        "square_days": squares * 8,
        "differing_square_days": differing_days,
        "share_differing": round(differing_days / (squares * 8), 6),
        "largest_difference_points": largest_difference,
        "unavailable_reason_mismatches": reason_mismatches,
        "mismatches": len(mismatches),
        "mismatch_examples": mismatches[:10],
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["weather", "estimates", "warmup", "compare", "rewind", "requests"])
    parser.add_argument("--assessment-date", required=True)
    parser.add_argument("--forest-build-version", default="")
    parser.add_argument("--region", default="all")
    parser.add_argument("--label", default="run")
    parser.add_argument("--mode", choices=["cold", "night"], default="cold")
    parser.add_argument("--limit-cells", type=int, default=0)
    parser.add_argument("--limit-tiles", type=int, default=0)
    parser.add_argument("--output-dir", default="/output")
    parser.add_argument("--trace-memory", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--random-cells", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    try:
        start_django()
        handlers = {"weather": run_weather, "estimates": run_estimates, "warmup": run_warmup, "compare": run_compare, "rewind": run_rewind, "requests": run_requests}
        result = handlers[args.phase](args=args)
    except Exception as exc:
        traceback.print_exc()
        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "peak_memory": peak_memory_mb()}
        print(json.dumps({"phase": args.phase, **result}), flush=True)
        return 1
    result.setdefault("peak_memory", peak_memory_mb())
    print(json.dumps({"phase": args.phase, **result}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
