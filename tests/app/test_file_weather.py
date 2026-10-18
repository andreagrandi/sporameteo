"""Checks for reading weather from Open-Meteo files, using small local files instead of the bucket."""

import json
import math
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import numpy as np
import omfiles
import pytest
from django.core.management import call_command
from weather_requests.eligibility import WeatherCell
from weather_summaries.file_weather import store_file_weather
from weather_summaries.models import FileWeatherRead, WeatherDaily, WeatherSnapshot

from sporameteo.weather import bucket_files
from sporameteo.weather.bucket_files import (
    CHUNK_HOURS,
    DEM_PATH,
    DOMAIN_PATH,
    READ_VARIABLES,
    chunk_path,
    estimate_read_bytes,
    local_window_hours,
    match_cells,
    point_coordinates,
    read_cell_hours,
    read_newest_run,
    surrounding_points,
)
from sporameteo.weather.evapotranspiration import hourly_reference_evapotranspiration, top_of_atmosphere_radiation

DAY = date(2026, 10, 10)
CELLS = {"cell-a": (43.5, 11.0), "cell-b": (44.0, 10.5)}
GRID_HEIGHTS = {"cell-a": 500.0, "cell-b": 600.0}
PLACE_HEIGHTS = {"cell-a": 500.0, "cell-b": 100.0}
VALUES = {
    "precipitation": 0.5,
    "temperature_2m": 20.0,
    "soil_moisture_0_to_7cm": 0.3,
    "soil_temperature_0_to_7cm": 12.0,
    "dew_point_2m": 10.0,
    "shortwave_radiation": 300.0,
    "wind_u_component_10m": 3.0,
    "wind_v_component_10m": 4.0,
}
RUN_TIME = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)


class SmallArray:
    """Map the global point and column indexes of the real grid onto a small local array."""

    def __init__(self, *, reader: Any, point_index: dict[int, int] | None, dem_index: dict[tuple[int, int], int] | None) -> None:
        self.reader = reader
        self.point_index = point_index
        self.dem_index = dem_index

    def read_array(self, ranges: tuple[slice, ...]) -> np.ndarray:
        if self.dem_index is not None:
            row, column = ranges[0].start, ranges[1].start
            return self.reader.read_array((slice(self.dem_index[(row, column)], self.dem_index[(row, column)] + 1), slice(0, 1)))
        assert self.point_index is not None
        points = range(ranges[-2].start, ranges[-2].stop) if len(ranges) == 3 else range(ranges[1].start, ranges[1].stop)
        full = self.reader.read_array(tuple(slice(None) for _ in ranges))
        if len(ranges) == 3:
            return np.stack([full[:, self.point_index[point], :] for point in points], axis=1)
        return np.stack([full[:, self.point_index[point]] for point in points], axis=1)


class LocalSource:
    """Read small .om files from a folder and answer like the bucket source."""

    def __init__(self, *, root: Path, point_index: dict[int, int], dem_index: dict[tuple[int, int], int]) -> None:
        self.root = root
        self.point_index = point_index
        self.dem_index = dem_index
        self.opened: list[str] = []

    def open_om(self, *, path: str) -> SmallArray:
        self.opened.append(path)
        reader = omfiles.OmFileReader(str(self.root / path))
        if path.startswith(DEM_PATH):
            return SmallArray(reader=reader, point_index=None, dem_index=self.dem_index)
        return SmallArray(reader=reader, point_index=self.point_index, dem_index=None)

    def read_json(self, *, path: str) -> dict[str, Any]:
        return json.loads((self.root / path).read_text())

    def modified(self, *, path: str) -> datetime:
        return datetime.fromtimestamp((self.root / path).stat().st_mtime, tz=UTC)

    def bytes_read(self) -> int:
        return 0

    def request_count(self) -> int:
        return len(self.opened)


def write_om(*, path: Path, data: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = omfiles.OmFileWriter(str(path))
    variable = writer.write_array(data.astype(np.float32), chunks=[min(size, 4) for size in data.shape], compression="fpx_xor_2d")
    writer.close(variable)


def build_source(
    *,
    root: Path,
    first_day: date = DAY,
    last_day: date = DAY,
    run_time: datetime = RUN_TIME,
    missing_temperature_hour: int | None = None,
    cells: dict[str, tuple[float, float]] | None = None,
    missing_temperature_cell: str = "cell-a",
) -> LocalSource:
    """Write the static files and the chunk files for the test cells, the two default ones unless given."""
    cells = cells or CELLS
    surrounding = {key: surrounding_points(latitude=lat, longitude=lon) for key, (lat, lon) in cells.items()}
    points = {key: found[0] for key, found in surrounding.items()}
    ordered = sorted({point for group in points.values() for point in group})
    point_index = {point: index for index, point in enumerate(ordered)}
    heights = np.zeros((1, len(ordered)))
    for key, group in points.items():
        for point in group:
            heights[0, point_index[point]] = GRID_HEIGHTS.get(key, 500.0)
    write_om(path=root / DOMAIN_PATH / "static" / "HSURF.om", data=heights)
    dem_index: dict[tuple[int, int], int] = {}
    for key, (lat, lon) in cells.items():
        whole_degree, row, column = bucket_files.dem_position(latitude=lat, longitude=lon)
        dem_index[(row, column)] = len(dem_index)
        write_om(path=root / DEM_PATH / f"lat_{whole_degree}.om", data=np.full((len(CELLS), 1), PLACE_HEIGHTS.get(key, 100.0)))
    (root / DOMAIN_PATH / "static" / "meta.json").write_text(json.dumps({"last_run_initialisation_time": int(run_time.timestamp())}))
    first_hour, end_hour = local_window_hours(first_day=first_day, last_day=last_day)
    for chunk in range(first_hour // CHUNK_HOURS, end_hour // CHUNK_HOURS + 1):
        for variable in READ_VARIABLES:
            data = np.full((1, len(ordered), CHUNK_HOURS), VALUES[variable])
            local_hour = None if missing_temperature_hour is None else missing_temperature_hour - chunk * CHUNK_HOURS
            if variable == "temperature_2m" and local_hour is not None and 0 <= local_hour < CHUNK_HOURS:
                data[0, point_index[surrounding[missing_temperature_cell][0][surrounding[missing_temperature_cell][2]]], local_hour] = np.nan
            write_om(path=root / chunk_path(variable=variable, chunk=chunk), data=data)
    return LocalSource(root=root, point_index=point_index, dem_index=dem_index)


def cell_positions() -> dict[str, tuple[float, float]]:
    return dict(CELLS)


def test_two_cells_read_for_one_day(tmp_path: Path) -> None:
    source = build_source(root=tmp_path)
    matches = match_cells(source=source, cells=cell_positions())
    result = read_cell_hours(source=source, matches=matches, first_day=DAY, last_day=DAY)

    assert set(result.cells) == {"cell-a", "cell-b"}
    for hours in result.cells.values():
        assert len(hours.timestamps) == 25
        assert hours.precipitation == (0.5,) * 25
        assert hours.soil_moisture[0] == 0.30000001192092896
        assert all(value is not None and value >= 0 for value in hours.et0)


def test_height_correction_applies_to_temperature_and_soil_temperature(tmp_path: Path) -> None:
    source = build_source(root=tmp_path)
    matches = match_cells(source=source, cells=cell_positions())
    result = read_cell_hours(source=source, matches=matches, first_day=DAY, last_day=DAY)

    assert matches["cell-a"].correction == 0.0
    assert math.isclose(matches["cell-b"].correction, (600.0 - 100.0) * 0.0065, rel_tol=1e-5)
    assert result.cells["cell-a"].temperature[0] == 20.0
    assert math.isclose(result.cells["cell-b"].temperature[0], 20.0 + 3.25, rel_tol=1e-5)
    assert math.isclose(result.cells["cell-b"].soil_temperature[0], 12.0 + 3.25, rel_tol=1e-5)
    assert result.cells["cell-b"].precipitation[0] == 0.5


def test_wind_speed_comes_from_both_components(tmp_path: Path) -> None:
    source = build_source(root=tmp_path)
    matches = match_cells(source=source, cells={"cell-a": CELLS["cell-a"]})
    hours = read_cell_hours(source=source, matches=matches, first_day=DAY, last_day=DAY).cells["cell-a"]

    hour_end = datetime.fromtimestamp(hours.timestamps[12], tz=UTC)
    latitude, longitude = point_coordinates(point=matches["cell-a"].point)
    expected = hourly_reference_evapotranspiration(
        temperature_c=20.0,
        dew_point_c=10.0,
        shortwave_radiation_w_m2=300.0,
        wind_speed_10m_m_s=5.0,
        elevation_m=500.0,
        top_of_atmosphere_w_m2=top_of_atmosphere_radiation(latitude=latitude, longitude=longitude, hour_end=hour_end),
    )
    assert math.isclose(hours.et0[12], expected, rel_tol=1e-5)


def test_missing_hour_becomes_missing_in_temperature_and_evapotranspiration(tmp_path: Path) -> None:
    first_hour, _ = local_window_hours(first_day=DAY, last_day=DAY)
    source = build_source(root=tmp_path, missing_temperature_hour=first_hour + 5)
    matches = match_cells(source=source, cells=cell_positions())
    result = read_cell_hours(source=source, matches=matches, first_day=DAY, last_day=DAY)

    assert result.cells["cell-a"].temperature[5] is None
    assert result.cells["cell-a"].et0[5] is None
    assert result.cells["cell-a"].temperature[4] == 20.0
    assert result.cells["cell-b"].temperature[5] is not None


def test_modification_time_comes_back_for_every_chunk_file(tmp_path: Path) -> None:
    source = build_source(root=tmp_path)
    matches = match_cells(source=source, cells=cell_positions())
    result = read_cell_hours(source=source, matches=matches, first_day=DAY, last_day=DAY)

    first_hour, _ = local_window_hours(first_day=DAY, last_day=DAY)
    expected = chunk_path(variable="temperature_2m", chunk=first_hour // CHUNK_HOURS)
    assert expected in result.file_modified
    assert len(result.file_modified) >= len(READ_VARIABLES)
    assert result.file_modified[expected] == datetime.fromtimestamp((tmp_path / expected).stat().st_mtime, tz=UTC)


def test_newest_run_time_comes_from_meta_file(tmp_path: Path) -> None:
    source = build_source(root=tmp_path)

    assert read_newest_run(source=source) == RUN_TIME


def test_estimate_read_bytes_grows_with_cells_and_chunk_files() -> None:
    short = estimate_read_bytes(cell_count=333, first_day=date(2026, 10, 9), last_day=date(2026, 10, 10))
    long = estimate_read_bytes(cell_count=333, first_day=date(2026, 9, 1), last_day=date(2026, 10, 17))

    assert short > 0
    assert long >= short
    assert estimate_read_bytes(cell_count=0, first_day=DAY, last_day=DAY) == 0


ASSESSMENT = date(2026, 10, 10)
WEATHER_CELLS = [WeatherCell(key=key, latitude=lat, longitude=lon) for key, (lat, lon) in CELLS.items()]


def store(*, source: LocalSource, assessment_date: date = ASSESSMENT) -> FileWeatherRead:
    return store_file_weather(cells=WEATHER_CELLS, assessment_date=assessment_date, region_code="09", source=source)


def window_source(*, root: Path, missing_temperature_hour: int | None = None) -> LocalSource:
    """Write files for the 38 days of ASSESSMENT and the one more day that the next date needs."""
    return build_source(
        root=root,
        first_day=ASSESSMENT - timedelta(days=30),
        last_day=ASSESSMENT + timedelta(days=8),
        missing_temperature_hour=missing_temperature_hour,
    )


@pytest.mark.django_db
def test_first_read_stores_38_days_and_a_snapshot_for_each_cell(tmp_path: Path) -> None:
    read = store(source=window_source(root=tmp_path))

    assert read.status == "complete"
    assert (read.cells_total, read.cells_written, read.cells_already_ready, read.missing) == (2, 2, 0, {})
    assert WeatherDaily.objects.filter(cell_key="cell-a").count() == 38
    assert WeatherDaily.objects.filter(cell_key="cell-b").count() == 38
    assert WeatherSnapshot.objects.count() == 2
    row = WeatherDaily.objects.get(cell_key="cell-b", date=ASSESSMENT)
    assert row.rain_mm == 12.0
    assert math.isclose(row.mean_temperature_c, 23.25, rel_tol=1e-5)
    assert row.reference_elevation_m == 100.0


@pytest.mark.django_db
def test_rows_name_the_file_source_and_the_newest_chunk_file_time(tmp_path: Path) -> None:
    read = store(source=window_source(root=tmp_path))

    row = WeatherDaily.objects.get(cell_key="cell-a", date=ASSESSMENT)
    day_start_hour, day_end_hour = local_window_hours(first_day=ASSESSMENT, last_day=ASSESSMENT)
    paths = bucket_files.chunk_paths(first_hour=day_start_hour, last_hour=day_end_hour)
    newest = max(datetime.fromtimestamp((tmp_path / path).stat().st_mtime, tz=UTC) for path in paths)
    assert (row.source_provider, row.source_model, row.source_id) == ("open-meteo-files", "ecmwf_ifs", "open-meteo-files:ecmwf_ifs")
    assert row.source_endpoint == "https://openmeteo.s3.amazonaws.com/data/ecmwf_ifs"
    assert row.request_id == f"files:{read.id}"
    assert row.attempt_id == read.id
    assert row.publication_time == newest
    assert set(read.file_modified) >= set(paths)


@pytest.mark.django_db
def test_second_read_for_the_same_date_writes_nothing(tmp_path: Path) -> None:
    source = window_source(root=tmp_path)
    store(source=source)
    second = store(source=source)

    assert WeatherDaily.objects.count() == 76
    assert (second.cells_written, second.cells_already_ready) == (0, 2)
    assert second.status == "complete"


@pytest.mark.django_db
def test_missing_hour_stores_no_snapshot_and_names_the_reason(tmp_path: Path) -> None:
    bad_day = ASSESSMENT + timedelta(days=3)
    first_hour, _ = local_window_hours(first_day=bad_day, last_day=bad_day)
    read = store(source=window_source(root=tmp_path, missing_temperature_hour=first_hour + 5))

    assert read.cells_written == 1
    assert read.missing == {"cell-a": {"reason": "missing_hours", "dates": [bad_day.isoformat()]}}
    assert not WeatherDaily.objects.filter(cell_key="cell-a", date=bad_day).exists()
    assert WeatherDaily.objects.filter(cell_key="cell-a").count() == 37
    assert not WeatherSnapshot.objects.filter(cell_key="cell-a").exists()
    assert WeatherSnapshot.objects.filter(cell_key="cell-b").exists()


@pytest.mark.django_db
def test_next_date_with_a_new_run_writes_nine_days_per_cell(tmp_path: Path) -> None:
    source = window_source(root=tmp_path)
    store(source=source)
    newer_run = RUN_TIME + timedelta(days=1)
    (tmp_path / DOMAIN_PATH / "static" / "meta.json").write_text(json.dumps({"last_run_initialisation_time": int(newer_run.timestamp())}))

    read = store(source=source, assessment_date=ASSESSMENT + timedelta(days=1))

    assert read.run_time == newer_run
    assert (read.cells_written, read.missing) == (2, {})
    assert WeatherDaily.objects.count() == 76 + 18
    assert WeatherDaily.objects.filter(cell_key="cell-a", date=ASSESSMENT).count() == 2
    past = WeatherDaily.objects.get(cell_key="cell-a", date=ASSESSMENT, forecast_issue_time=None)
    assert past.metadata["history_version"] == 1


@pytest.mark.django_db
def test_forecast_rows_carry_the_run_time_and_past_rows_do_not(tmp_path: Path) -> None:
    store(source=window_source(root=tmp_path))

    forecast = WeatherDaily.objects.filter(cell_key="cell-a", date__gte=ASSESSMENT)
    past = WeatherDaily.objects.filter(cell_key="cell-a", date__lt=ASSESSMENT)
    assert forecast.count() == 8
    assert {row.forecast_issue_time for row in forecast} == {RUN_TIME}
    assert {row.forecast_issue_time for row in past} == {None}


@pytest.mark.django_db
def test_row_metadata_holds_only_this_cell(tmp_path: Path) -> None:
    store(source=window_source(root=tmp_path))

    metadata = WeatherDaily.objects.get(cell_key="cell-b", date=ASSESSMENT).metadata
    assert "response" not in metadata
    assert set(metadata["location"]) == {"grid_point", "grid_latitude", "grid_longitude", "grid_height_m", "height_correction_c"}
    assert math.isclose(metadata["location"]["height_correction_c"], 3.25, rel_tol=1e-5)
    assert metadata["source"]["value_kind"] == "modelled file values"
    assert metadata["source"]["run_time"] == RUN_TIME.isoformat()
    assert all(path.startswith(DOMAIN_PATH) for path in metadata["source"]["file_modified"])


@pytest.mark.django_db
def test_failed_chunk_file_ends_the_read_as_failed_and_writes_no_row(tmp_path: Path) -> None:
    class BrokenSource(LocalSource):
        def open_om(self, *, path: str) -> SmallArray:
            if "/temperature_2m/" in path:
                raise OSError("chunk file unreachable")
            return super().open_om(path=path)

    working = window_source(root=tmp_path)
    read = store(source=BrokenSource(root=tmp_path, point_index=working.point_index, dem_index=working.dem_index))

    assert read.status == "failed"
    assert "chunk file unreachable" in read.error
    assert WeatherDaily.objects.count() == 0
    assert WeatherSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_dry_run_prints_megabytes_and_cell_count_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("weather_on_demand.management.commands.store_file_weather.weather_cells_for_run_key", lambda **kwargs: WEATHER_CELLS)
    output = StringIO()

    call_command("store_file_weather", "--dry-run", "--assessment-date=2026-10-10", "--forest-build-version=test", stdout=output)

    text = output.getvalue()
    assert "Weather cells: 2, already ready for the date: 0" in text
    assert "Days to read: 2026-09-10 through 2026-10-17" in text
    assert "Estimated download: " in text and " MB" in text
    assert FileWeatherRead.objects.count() == 0
    assert WeatherDaily.objects.count() == 0


@pytest.mark.django_db
def test_a_read_in_small_blocks_stores_the_same_rows_as_one_block(tmp_path: Path) -> None:
    from django.db import transaction

    source = window_source(root=tmp_path)
    fields = ("cell_key", "date", "rain_mm", "mean_temperature_c", "min_temperature_c", "et0_mm", "mean_soil_temperature_c",
              "mean_soil_moisture", "reference_elevation_m", "forecast_issue_time", "status")

    def stored() -> list[tuple[Any, ...]]:
        return list(WeatherDaily.objects.order_by("cell_key", "date").values_list(*fields))

    savepoint = transaction.savepoint_create()
    in_blocks = store_file_weather(
        cells=WEATHER_CELLS, assessment_date=ASSESSMENT, region_code="09", source=source, block_size=1
    )
    rows_in_blocks = stored()
    snapshots_in_blocks = WeatherSnapshot.objects.count()
    transaction.savepoint_rollback(savepoint)
    in_one_block = store(source=source)

    assert (in_blocks.cells_written, in_blocks.missing) == (in_one_block.cells_written, in_one_block.missing) == (2, {})
    assert len(rows_in_blocks) == 76
    assert rows_in_blocks == stored()
    assert snapshots_in_blocks == WeatherSnapshot.objects.count() == 2
