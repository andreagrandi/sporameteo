"""Checks that map, overview, date and detail reads come only from the published Tuscany run."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.gis.geos import Polygon
from django.core.cache.backends.db import DatabaseCache
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from estimate_updates.models import EstimateResult, ForecastRun, PublishedForecastRun
from map_api import api as map_api
from map_api import published
from map_api.published import GRID_TO_WGS84
from sporameteo.finds.grid import format_cell_id
from national_forest.models import ForestCell
from national_grid.models import GridBuildCell, GridCell
from tests.app.test_area_api import _publish_run_for, area_fixture  # noqa: F401
from tests.app.test_map_api import (  # noqa: F401
    MAP_DATE,
    add_update,
    map_fixture,
    map_id_of,
    publish_run,
)


pytestmark = pytest.mark.django_db(transaction=True)

CACHE_TABLE = "sporameteo_map_cache"
READ_TABLES = (EstimateResult._meta.db_table, ForestCell._meta.db_table)
BLOCKED_FUNCTIONS = (
    "weather_on_demand.services.request_weather",
    "weather_on_demand.services.fetch_weather_on_demand",
    "weather_requests.provider.fetch_weather",
    "national_forest.source.cache_source_chunk",
    "national_terrain.source.cache_tile",
    "estimate_updates.services.calculate_estimate_update",
    "estimate_updates.services.enqueue_estimate_update",
    "estimate_updates.forecast_runs.calculate_estimate_update",
    "estimate_updates.forecast_runs.process_forecast_run",
)


@pytest.fixture
def no_remote_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test when a page request fetches weather, habitat data or calculates."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("A page request must only read stored rows.")

    for path in BLOCKED_FUNCTIONS:
        monkeypatch.setattr(path, forbidden)


@pytest.fixture
def served(
    map_fixture: dict[str, object],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    settings,
    no_remote_work: None,
) -> SimpleNamespace:
    """Publish one run for the map fixture and serve it on the fixture date."""
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE)
    settings.SPORAMETEO_FOREST_BUILD_VERSION = map_fixture["build"].version
    run = publish_run(fixture=map_fixture, updates={"44.00,11.00": map_fixture["new_update"]})
    return SimpleNamespace(fixture=map_fixture, run=run, client=Client())


def _squares_query(*, bbox: str, day_offset: int = 0) -> dict[str, str]:
    return {"bbox": bbox, "target_date": (MAP_DATE + timedelta(days=day_offset)).isoformat()}


def _square(*, response, cell: ForestCell) -> dict:
    cell_id = cell.grid_build_cell.cell.cell_id
    return next(item for item in response.json()["features"] if item["id"] == cell_id)["properties"]


def _read_tables(queries: CaptureQueriesContext) -> int:
    return sum(1 for query in queries if any(table in query["sql"] for table in READ_TABLES))


def _cache_rows(*, pattern: str = "%") -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {CACHE_TABLE} WHERE cache_key LIKE %s", [pattern])
        return cursor.fetchone()[0]


def _other_process_cache(*, max_entries: int = 50_000, cull_frequency: int = 4) -> DatabaseCache:
    """Return a new handle on the shared table, as a second process or a restart would have."""
    return DatabaseCache(
        CACHE_TABLE,
        {"TIMEOUT": None, "OPTIONS": {"MAX_ENTRIES": max_entries, "CULL_FREQUENCY": cull_frequency}},
    )


@pytest.fixture
def served_detail(
    area_fixture: dict[str, object],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    settings,
    no_remote_work: None,
) -> SimpleNamespace:
    """Publish the run of the area fixture, whose update can be explained in full."""
    update = area_fixture["update"]
    run = _publish_run_for(update=update)
    monkeypatch.setattr("map_api.api._today", lambda: update.assessment_date)
    settings.SPORAMETEO_FOREST_BUILD_VERSION = update.forest_build.version
    return SimpleNamespace(
        update=update,
        run=run,
        square_id=area_fixture["square_id"],
        day=update.assessment_date + timedelta(days=1),
        client=Client(),
    )


def test_prepared_reads_need_no_remote_access_and_agree_across_endpoints(
    served_detail: SimpleNamespace,
) -> None:
    update, day = served_detail.update, served_detail.day
    client = served_detail.client

    detail = client.get(
        f"/api/v1/areas/{served_detail.square_id}",
        {"target_date": day.isoformat(), "update_id": str(update.public_id)},
    )
    bounds = detail.json()["selection"]["bounds"]
    query = {
        "bbox": ",".join(str(bounds[name]) for name in ("west", "south", "east", "north")),
        "target_date": day.isoformat(),
    }
    info = client.get("/api/v1/info")
    squares = client.get("/api/v1/squares", query)
    overview = client.get("/api/v1/estimate-overview", query)

    assert [item.status_code for item in (info, squares, overview, detail)] == [200] * 4
    run_id = str(served_detail.run.id)
    map_id = map_id_of(served_detail.run)
    assert info.json()["run"]["run_id"] == squares.json()["run_id"] == overview.json()["run_id"] == map_id
    assert info.json()["runs"][0]["run_id"] == detail.json()["selection"]["run_id"] == run_id
    assessment_date = update.assessment_date.isoformat()
    assert info.json()["run"]["assessment_date"] == squares.json()["assessment_date"] == assessment_date
    assert overview.json()["assessment_date"] == assessment_date
    assert day.isoformat() in info.json()["target_dates"]
    assert squares.json()["target_date"] == overview.json()["target_date"] == day.isoformat()
    square = next(item for item in squares.json()["features"] if item["id"] == served_detail.square_id)
    properties = square["properties"]
    selection = detail.json()["selection"]
    assert selection["target_date"] == properties["target_date"] == day.isoformat()
    assert selection["square_id"] == square["id"]
    assert selection["update_id"] == properties["update_id"] == str(update.public_id)
    assert selection["assessment_date"] == properties["assessment_date"] == assessment_date
    percentage = detail.json()["estimate"]["whole_percentage"]
    assert percentage == properties["percentage"]
    assert detail.json()["estimate"]["calculated_at"][:19] == properties["calculated_at"][:19]
    overview_properties = overview.json()["features"][0]["properties"]
    assert overview_properties["percentage"] == percentage
    assert overview_properties["assessment_date"] == assessment_date
    assert overview_properties["target_date"] == day.isoformat()


def test_a_restart_serves_the_detail_from_the_shared_cache(served_detail: SimpleNamespace, monkeypatch) -> None:
    url = f"/api/v1/areas/{served_detail.square_id}"
    query = {
        "target_date": served_detail.day.isoformat(),
        "update_id": str(served_detail.update.public_id),
    }
    first = served_detail.client.get(url, query)

    monkeypatch.setattr(published, "map_cache", _other_process_cache)
    with CaptureQueriesContext(connection) as queries:
        second = Client().get(url, query)

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert _read_tables(queries) == 0
    assert len(queries) <= 4


def test_a_missing_publication_or_a_view_outside_tuscany_gives_a_clear_unavailable_answer(
    map_fixture: dict[str, object],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    settings,
    no_remote_work: None,
) -> None:
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE)
    settings.SPORAMETEO_FOREST_BUILD_VERSION = map_fixture["build"].version
    client = Client()
    query = _squares_query(bbox=map_fixture["bbox"])

    before = [client.get("/api/v1/squares", query), client.get("/api/v1/estimate-overview", query)]
    publish_run(fixture=map_fixture, updates={"44.00,11.00": map_fixture["new_update"]})
    outside = client.get("/api/v1/squares", _squares_query(bbox="12.5,41.8,12.6,41.9"))
    overview_outside = client.get(
        "/api/v1/estimate-overview", _squares_query(bbox="12.5,41.8,12.6,41.9"),
    )

    for response in before:
        assert response.status_code == 503
        assert response.json()["code"] == "forecast_unavailable"
        assert response.json()["message_key"] == "map.forecast_unavailable"
    assert outside.status_code == 400
    assert outside.json()["code"] == "outside_coverage"
    assert overview_outside.status_code == 200
    assert overview_outside.json()["features"] == []


def test_a_failed_newer_run_keeps_the_previous_values_and_dates_without_shifting_them(
    served: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = served.fixture
    cell = fixture["cells"][0]
    ForecastRun.objects.create(
        region_code="09",
        assessment_date=MAP_DATE + timedelta(days=1),
        status=ForecastRun.Status.FAILED,
        forest_build=fixture["build"],
        terrain_build=fixture["terrain"],
        method_version="map-test",
        calculation_version="map-calc-failed",
        target_dates=[(MAP_DATE + timedelta(days=1 + offset)).isoformat() for offset in range(8)],
        expected_cell_count=1,
        expected_square_count=1,
        failure_reason="1 weather cells failed.",
    )
    monkeypatch.setattr("map_api.api._today", lambda: MAP_DATE + timedelta(days=2))

    info = served.client.get("/api/v1/info").json()
    values = {}
    for offset in range(2, 8):
        response = served.client.get(
            "/api/v1/squares", _squares_query(bbox=fixture["bbox"], day_offset=offset),
        )
        assert response.status_code == 200
        properties = _square(response=response, cell=cell)
        assert properties["target_date"] == (MAP_DATE + timedelta(days=offset)).isoformat()
        assert properties["assessment_date"] == MAP_DATE.isoformat()
        values[offset] = properties["percentage"]
    too_late = served.client.get(
        "/api/v1/squares", _squares_query(bbox=fixture["bbox"], day_offset=8),
    )

    assert info["run"]["run_id"] == map_id_of(served.run)
    assert info["run"]["assessment_date"] == MAP_DATE.isoformat()
    assert info["target_dates"] == [
        (MAP_DATE + timedelta(days=offset)).isoformat() for offset in range(2, 8)
    ]
    assert values == {offset: 42 for offset in range(2, 8)}
    assert too_late.status_code == 400
    assert too_late.json()["code"] == "unsupported_date"


def test_a_second_process_and_a_restart_reuse_the_entries_of_the_first(
    served: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = served.fixture
    query = _squares_query(bbox=fixture["bbox"])
    first = served.client.get("/api/v1/squares", query)

    monkeypatch.setattr(published, "map_cache", _other_process_cache)
    map_api._REGION_EXTENTS.clear()
    map_api._coverage_cached.cache_clear()
    with CaptureQueriesContext(connection) as queries:
        second = Client().get("/api/v1/squares", query)

    assert first.status_code == second.status_code == 200
    assert second.content == first.content
    assert second["ETag"] == first["ETag"]
    assert _read_tables(queries) == 0


def test_a_small_cache_drops_entries_and_the_database_rebuilds_them(
    served: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = served.fixture
    cell_bounds = served.client.get(
        "/api/v1/squares", _squares_query(bbox=fixture["bbox"]),
    ).json()["features"][0]["geometry"]["coordinates"][0]
    west, south = min(x for x, _ in cell_bounds), min(y for _, y in cell_bounds)
    east, north = max(x for x, _ in cell_bounds), max(y for _, y in cell_bounds)
    small_bbox = f"{west + 1e-5},{south + 1e-5},{east - 1e-5},{north - 1e-5}"
    requests = [
        _squares_query(bbox=bbox, day_offset=offset)
        for bbox in (fixture["bbox"], small_bbox)
        for offset in (0, 1, 2)
    ]
    monkeypatch.setattr(published, "map_cache", lambda: _other_process_cache(max_entries=4, cull_frequency=2))
    published.map_cache().clear()

    first = [served.client.get("/api/v1/squares", query).content for query in requests]
    rows_after_writes = _cache_rows()
    with CaptureQueriesContext(connection) as queries:
        again = [served.client.get("/api/v1/squares", query).content for query in requests]

    assert again == first
    assert rows_after_writes <= 5
    assert _cache_rows() <= 5
    assert _read_tables(queries) > 0


def test_a_new_run_shows_new_data_and_the_old_run_stays_readable_in_the_cache(
    served: SimpleNamespace,
) -> None:
    fixture = served.fixture
    cell = fixture["cells"][0]
    query = _squares_query(bbox=fixture["bbox"], day_offset=1)
    old_run = served.run
    old_response = served.client.get("/api/v1/squares", query)
    old_rows = _cache_rows(pattern=f"%{old_run.id}%")

    newer = add_update(
        fixture=fixture, cell_key="44.00,11.00", forest_cell=cell,
        values=bytes([10] * 8), label="newer-run",
    )
    new_run = publish_run(
        fixture=fixture, updates={"44.00,11.00": newer}, calculation_version="map-calc-2",
    )
    new_response = served.client.get("/api/v1/squares", query)
    PublishedForecastRun.objects.filter(region_code="09").update(current_run=old_run)
    with CaptureQueriesContext(connection) as queries:
        old_again = served.client.get("/api/v1/squares", query)

    assert old_rows > 0
    assert _cache_rows(pattern=f"%{old_run.id}%") == old_rows
    assert new_response.json()["run_id"] == map_id_of(new_run)
    assert _square(response=new_response, cell=cell)["percentage"] == 10
    assert _square(response=new_response, cell=cell)["update_id"] == str(newer.public_id)
    assert _square(response=old_response, cell=cell)["percentage"] == 42
    assert old_again.content == old_response.content
    assert _read_tables(queries) == 0


def test_request_options_never_share_a_cached_body(served: SimpleNamespace) -> None:
    fixture = served.fixture
    cell = fixture["cells"][0]
    full = served.client.get("/api/v1/squares", _squares_query(bbox=fixture["bbox"]))
    ring = full.json()["features"][0]["geometry"]["coordinates"][0]
    west, south = min(x for x, _ in ring), min(y for _, y in ring)
    east, north = max(x for x, _ in ring), max(y for _, y in ring)
    one_square = f"{west + 1e-5},{south + 1e-5},{east - 1e-5},{north - 1e-5}"

    narrow = served.client.get("/api/v1/squares", _squares_query(bbox=one_square))
    next_day = served.client.get("/api/v1/squares", _squares_query(bbox=fixture["bbox"], day_offset=1))
    full_again = served.client.get("/api/v1/squares", _squares_query(bbox=fixture["bbox"]))
    overview = served.client.get("/api/v1/estimate-overview", _squares_query(bbox=fixture["bbox"]))
    wide_overview = served.client.get(
        "/api/v1/estimate-overview", _squares_query(bbox="9,43,12,45"),
    )

    assert full.json()["square_count"] == 5
    assert 0 < narrow.json()["square_count"] < 5
    assert narrow.json()["features"][0]["id"] == cell.grid_build_cell.cell.cell_id
    assert next_day.json()["target_date"] == (MAP_DATE + timedelta(days=1)).isoformat()
    assert _square(response=full, cell=cell)["percentage"] == 0
    assert _square(response=next_day, cell=cell)["percentage"] == 42
    assert full_again.content == full.content
    assert len({item["ETag"] for item in (full, narrow, next_day, overview, wide_overview)}) == 5


def _add_weather_cells(*, fixture: dict[str, object], count: int) -> None:
    """Add forest squares above the fixture squares, each with its own weather cell and update."""
    build = fixture["build"]
    first = fixture["cells"][0]
    membership = first.grid_build_cell
    for index in range(count):
        corner_n = 2_300_500 + index * 500
        polygon = Polygon.from_bbox((4_300_000, corner_n, 4_300_500, corner_n + 500))
        polygon.srid = 3035
        grid_cell = GridCell.objects.create(
            cell_id=format_cell_id(corner_e=4_300_000, corner_n=corner_n),
            corner_e=4_300_000,
            corner_n=corner_n,
            geometry=polygon,
        )
        extra_membership = GridBuildCell.objects.create(
            build=membership.build,
            cell=grid_cell,
            chunk=membership.chunk,
            owner_region=membership.owner_region,
            land_geometry=membership.land_geometry,
            land_share=1.0,
        )
        forest_cell = ForestCell.objects.create(
            build=build,
            grid_build_cell=extra_membership,
            chunk=first.chunk,
            total_pixel_count=100,
            valid_pixel_count=100,
            forest_pixel_count=100,
            broadleaf_pixel_count=100,
            valid_coverage=1.0,
            coverage_quality="high",
            forest_share=1.0,
            broadleaf_share=1.0,
            leaf_type=ForestCell.LeafType.BROADLEAF,
            is_forest=True,
            weather_latitude=45.0 + index / 10,
            weather_longitude=12.0,
        )
        add_update(
            fixture=fixture,
            cell_key=f"{45.0 + index / 10:.2f},12.00",
            forest_cell=forest_cell,
            values=bytes([20 + index] * 8),
            label=f"extra-{index}",
        )


def _run_updates(*, run: ForecastRun) -> dict[str, object]:
    return {cell.cell_key: cell.update for cell in run.cells.select_related("update")}


def test_a_cold_view_runs_the_same_number_of_queries_for_any_number_of_weather_cells(
    served: SimpleNamespace,
) -> None:
    fixture = served.fixture
    query = _squares_query(bbox=fixture["bbox"])
    published.map_cache().clear()
    map_api._REGION_EXTENTS.clear()
    with CaptureQueriesContext(connection) as few:
        few_response = served.client.get("/api/v1/squares", query)

    _add_weather_cells(fixture=fixture, count=4)
    updates = {**_run_updates(run=served.run)}
    for cell in ForestCell.objects.filter(build=fixture["build"], weather_latitude__gte=45.0):
        key = f"{cell.weather_latitude:.2f},{cell.weather_longitude:.2f}"
        updates[key] = EstimateResult.objects.get(forest_cell=cell).update
    publish_run(fixture=fixture, updates=updates, calculation_version="map-calc-many")
    published.map_cache().clear()
    with CaptureQueriesContext(connection) as many:
        many_response = served.client.get("/api/v1/squares", query)

    def update_ids(response) -> set[str]:
        return {
            item["properties"]["update_id"]
            for item in response.json()["features"]
            if item["properties"]["update_id"]
        }

    assert len(update_ids(few_response)) == 1
    assert len(update_ids(many_response)) == 5
    assert len(many) == len(few)
    assert _read_tables(few) == _read_tables(many) == 2


def test_cache_hits_run_no_query_on_estimate_results_or_forest_cells(served: SimpleNamespace) -> None:
    fixture = served.fixture
    query = _squares_query(bbox=fixture["bbox"], day_offset=1)
    overview_query = _squares_query(bbox="9,43,12,45", day_offset=1)
    served.client.get("/api/v1/squares", query)
    served.client.get("/api/v1/estimate-overview", overview_query)

    with CaptureQueriesContext(connection) as squares:
        squares_response = served.client.get("/api/v1/squares", query)
    with CaptureQueriesContext(connection) as overview:
        overview_response = served.client.get("/api/v1/estimate-overview", overview_query)

    assert squares_response.status_code == overview_response.status_code == 200
    for captured in (squares, overview):
        assert _read_tables(captured) == 0
        assert len(captured) <= 3


def test_warming_prepares_each_published_region_and_leaves_tiles_to_the_first_request(
    served: SimpleNamespace,
) -> None:
    fixture = served.fixture
    publish_run(fixture=fixture, updates={"44.00,11.00": fixture["new_update"]}, region_code="10")

    assert published.warm_published_map_cache(region_code="11") == {"squares": 0}
    assert _cache_rows(pattern="%:overview-points:%") == 0
    assert published.warm_published_map_cache(region_code="10")["squares"] > 0
    assert _cache_rows(pattern="%:overview-points:%") == 1
    call_command("warm_published_map_cache", region_code="all")
    # Warming prepares the summary and the overview points. Tiles wait for the first request.
    assert (_cache_rows(pattern="%:static:%"), _cache_rows(pattern="%:results:%")) == (0, 0)
    assert _cache_rows(pattern="%:overview-points:%") == 2
    call_command("warm_published_map_cache", region_code="09")
    longitude, latitude = GRID_TO_WGS84.transform(4_301_250, 2_300_400)
    inside = f"{longitude - 0.0012},{latitude - 0.0009},{longitude + 0.0012},{latitude + 0.0009}"
    response = served.client.get("/api/v1/squares", _squares_query(bbox=inside))

    assert response.status_code == 200
    assert (_cache_rows(pattern="%:static:%"), _cache_rows(pattern="%:results:%")) == (1, 1)
    assert published.warm_published_map_cache(region_code="09")["squares"] > 0


def test_warming_without_a_published_run_does_nothing(
    map_fixture: dict[str, object],  # noqa: F811
) -> None:
    assert published.warm_published_map_cache(region_code="09") == {"squares": 0}
    call_command("warm_published_map_cache")
    assert _cache_rows() == 0


def test_an_overview_miss_with_warm_points_runs_no_forest_or_result_query(served: SimpleNamespace) -> None:
    call_command("warm_published_map_cache")
    assert _cache_rows(pattern="%:overview-points:%") == 1

    with CaptureQueriesContext(connection) as queries:
        response = served.client.get(
            "/api/v1/estimate-overview", _squares_query(bbox="9,43,12,45", day_offset=1),
        )

    assert response.status_code == 200
    assert response.json()["features"][0]["properties"]["percentage"] == 42
    assert _read_tables(queries) == 0
