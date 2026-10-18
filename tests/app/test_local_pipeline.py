"""Check the whole nightly chain once: weather, estimates, publication and the readers.

Single steps have their own tests. These tests start with a fake Open-Meteo, run the same
services as the commands, and read the published run through the map, overview and details.
"""

from __future__ import annotations

import json
from hashlib import sha256
from collections.abc import Callable
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.cache import caches
from django.test import Client

import weather_on_demand.services as weather_services
import weather_requests.provider as weather_provider
from estimate_updates.forecast_runs import (
    ForecastRunError,
    process_forecast_run,
    published_forecast_run,
    start_forecast_run,
)
from estimate_updates.models import EstimateResult, ForecastRun, PublishedForecastRun
from map_api import api as map_api
from map_api.published import GRID_TO_WGS84, warm_published_map_cache
from sporameteo.finds.grid import format_cell_id
from national_grid.models import GridRegion
from tests.app.test_forecast_runs import (  # noqa: F401
    _recording_calculator,
    local_host_trees,
    tuscany,
)
from tests.app.test_place_search import _resolve_post
from tests.app.test_scheduled_weather import (  # noqa: F401
    DAY,
    _clock,
    _fill_quota,
    _task_time,
    _Transport,
    isolate_environment,
)
from tests.app.test_weather_on_demand import _FakeOpenMeteo
from weather_on_demand.models import ScheduledWeatherRun
from weather_summaries.models import WeatherSnapshot

pytestmark = pytest.mark.django_db(transaction=True)

NEXT_DAY = DAY + timedelta(days=1)
TERMINAL = {ScheduledWeatherRun.Status.COMPLETED, ScheduledWeatherRun.Status.FAILED}
FOREST_SQUARES = (0, 1, 2)


def _run_weather(
    *,
    pipeline: SimpleNamespace,
    day: date,
    override: Callable[[dict[str, str]], object | None] | None = None,
    inner: Callable[..., object] | None = None,
    patience: timedelta = timedelta(hours=6),
) -> tuple[ScheduledWeatherRun, _Transport]:
    """Drive the night's weather run until it ends or the night is over."""
    clock = _clock(day=day)
    transport = _Transport(clock=clock, override=override, inner=inner)
    run = weather_services.start_scheduled_weather_run(
        assessment_date=day,
        region_code="09",
        forest_build_version=pipeline.tuscany.forest.version,
        clock=clock,
    )
    deadline = clock.now + patience
    for _ in range(300):
        run.refresh_from_db()
        if run.status in TERMINAL or clock.now > deadline:
            break
        weather_services.run_scheduled_weather_step(run_id=run.id, clock=clock, transport=transport)
        run.refresh_from_db()
        if run.status in TERMINAL:
            break
        when = _task_time(run=run)
        if when is not None and when > clock.now:
            clock.now = when
    run.refresh_from_db()
    return run, transport


def _run_forecast(*, day: date, calculator: Callable[..., object] | None = None) -> ForecastRun:
    """Plan, calculate and publish the Tuscany estimates for one date."""
    run = start_forecast_run(assessment_date=day)
    return process_forecast_run(run_id=run.id, calculator=calculator)


def _publish_night(*, pipeline: SimpleNamespace, day: date) -> _Transport:
    """Run weather, estimates, publication and cache warming for one night."""
    weather, transport = _run_weather(pipeline=pipeline, day=day)
    assert weather.status == ScheduledWeatherRun.Status.COMPLETED
    assert _run_forecast(day=day).status == ForecastRun.Status.PUBLISHED
    assert warm_published_map_cache(region_code="09")["squares"] > 0
    return transport


def _forbid_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if a read asks any weather provider for data."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("A read must not call a weather provider.")

    monkeypatch.setattr(weather_provider, "_default_transport", forbidden)
    monkeypatch.setattr(weather_services, "request_weather", forbidden)
    monkeypatch.setattr(weather_services, "fetch_weather_on_demand", forbidden)


@pytest.fixture
def pipeline(tuscany: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:  # noqa: F811
    """Prepare the Tuscany fixture, an empty map cache and a day that the test can move."""
    # The fixture region is one square wide. Widen it so the extent holds every fixture square.
    region = GridRegion.objects.get(build=tuscany.grid, istat_code="09")
    region.geometry = MultiPolygon(
        [Polygon.from_bbox((4_300_000, 2_300_000, 4_302_500, 2_300_500))], srid=3035,
    )
    region.save(update_fields=["geometry"])
    caches["map"].clear()
    map_api._REGION_EXTENTS.clear()
    map_api._coverage_cached.cache_clear()
    state = {"today": DAY}
    monkeypatch.setattr("map_api.api._today", lambda: state["today"])
    west, south = GRID_TO_WGS84.transform(4_299_900, 2_299_900)
    east, north = GRID_TO_WGS84.transform(4_302_600, 2_300_600)
    return SimpleNamespace(
        tuscany=tuscany,
        state=state,
        client=Client(),
        bbox=",".join(str(value) for value in (west, south, east, north)),
        square_ids=[cell.grid_build_cell.cell.cell_id for cell in tuscany.forest_cells],
    )


@pytest.fixture
def night_d(pipeline: SimpleNamespace) -> SimpleNamespace:
    """Publish the run of night D and keep what its provider calls looked like."""
    pipeline.transport = _publish_night(pipeline=pipeline, day=DAY)
    pipeline.run = ForecastRun.objects.get(assessment_date=DAY)
    return pipeline


def _squares(*, pipeline: SimpleNamespace, day: date):
    return pipeline.client.get("/api/v1/squares", {"bbox": pipeline.bbox, "target_date": day.isoformat()})


def _overview(*, pipeline: SimpleNamespace, day: date):
    return pipeline.client.get(
        "/api/v1/estimate-overview", {"bbox": pipeline.bbox, "target_date": day.isoformat()},
    )


def _detail(*, pipeline: SimpleNamespace, square_id: str, day: date, update_id: str | None):
    query = {"target_date": day.isoformat()}
    if update_id is not None:
        query["update_id"] = update_id
    return pipeline.client.get(f"/api/v1/areas/{square_id}", query)


def _square(*, response, square_id: str) -> dict:
    return next(item for item in response.json()["features"] if item["id"] == square_id)["properties"]


def _published_values(*, pipeline: SimpleNamespace, today: date) -> dict[tuple[int, int], dict]:
    """Read map and detail values for each offered date and forest square."""
    pipeline.state["today"] = today
    values = {}
    for offset in range(8):
        day = DAY + timedelta(days=offset)
        if day < today:
            continue
        squares = _squares(pipeline=pipeline, day=day)
        assert squares.status_code == 200
        for index in FOREST_SQUARES:
            properties = _square(response=squares, square_id=pipeline.square_ids[index])
            detail = _detail(
                pipeline=pipeline,
                square_id=pipeline.square_ids[index],
                day=day,
                update_id=properties["update_id"],
            )
            assert detail.status_code == 200
            values[(offset, index)] = {
                "assessment_date": properties["assessment_date"],
                "percentage": properties["percentage"],
                "update_id": properties["update_id"],
                "detail_percentage": detail.json()["estimate"]["whole_percentage"],
            }
    return values


# The whole chain


def test_a_night_stores_weather_publishes_eight_dates_and_warms_the_map(night_d: SimpleNamespace) -> None:
    weather = ScheduledWeatherRun.objects.get(assessment_date=DAY)
    run = night_d.run

    assert weather.status == ScheduledWeatherRun.Status.COMPLETED
    assert WeatherSnapshot.objects.filter(assessment_date=DAY).count() == 2
    assert night_d.transport.count > 0
    assert run.status == ForecastRun.Status.PUBLISHED
    assert run.target_dates == [(DAY + timedelta(days=offset)).isoformat() for offset in range(8)]
    assert PublishedForecastRun.objects.get(region_code="09").current_run_id == run.id
    assert published_forecast_run(region_code="09").run.id == run.id
    results = EstimateResult.objects.filter(update__forecast_run_cells__run=run)
    assert results.count() == 3
    assert warm_published_map_cache(region_code="09")["squares"] > 0


def test_map_overview_and_details_agree_on_square_date_value_update_and_credits(
    night_d: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_providers(monkeypatch)
    calls_before = night_d.transport.count
    info = night_d.client.get("/api/v1/info").json()
    credits = {item["name"]: item for item in info["attributions"]}
    assert info["run"]["assessment_date"] == DAY.isoformat()
    assert credits["Open-Meteo"]["credit"] == "Weather data by Open-Meteo.com"
    assert credits["Copernicus forest"]["credit"] is not None

    for offset in (0, 1, 4, 7):
        day = DAY + timedelta(days=offset)
        squares = _squares(pipeline=night_d, day=day)
        overview = _overview(pipeline=night_d, day=day)
        assert squares.status_code == overview.status_code == 200
        assert squares.json()["assessment_date"] == overview.json()["assessment_date"] == DAY.isoformat()
        assert squares.json()["target_date"] == overview.json()["target_date"] == day.isoformat()
        assert squares.json()["run_id"] == overview.json()["run_id"] == sha256(f"09:{night_d.run.id}".encode()).hexdigest()[:32]
        percentages = []
        for index in FOREST_SQUARES:
            square_id = night_d.square_ids[index]
            properties = _square(response=squares, square_id=square_id)
            detail = _detail(
                pipeline=night_d, square_id=square_id, day=day, update_id=properties["update_id"],
            )
            body = detail.json()
            assert detail.status_code == 200
            assert body["selection"]["square_id"] == square_id
            assert body["selection"]["target_date"] == properties["target_date"] == day.isoformat()
            assert body["selection"]["assessment_date"] == properties["assessment_date"] == DAY.isoformat()
            assert body["selection"]["update_id"] == properties["update_id"]
            assert body["selection"]["run_id"] == str(night_d.run.id)
            assert properties["state"] == body["estimate"]["state"] == "available"
            assert body["estimate"]["whole_percentage"] == properties["percentage"]
            assert body["estimate"]["calculated_at"][:19] == properties["calculated_at"][:19]
            assert body["sources"]["weather"]["retrieval_time"] is not None
            assert body["sources"]["weather"]["credit"]["text"] == "Weather data by Open-Meteo.com"
            percentages.append(properties["percentage"])
        group = overview.json()["features"]
        assert len(group) == 1
        assert group[0]["properties"]["state"] == "available"
        assert group[0]["properties"]["scored_square_count"] == len(percentages)
        assert group[0]["properties"]["percentage"] == round(sum(percentages) / len(percentages))
        assert group[0]["properties"]["assessment_date"] == DAY.isoformat()
    assert night_d.transport.count == calls_before


# A failed night keeps the published run


def _slow(query: dict[str, str]) -> object:
    raise TimeoutError("The provider answered too slowly.")


def _interrupted(query: dict[str, str]) -> object:
    raise ConnectionResetError("The connection was interrupted.")


def _refused(query: dict[str, str]) -> object:
    return 429, {"Retry-After": "600"}, b"daily limit"


@pytest.mark.parametrize(
    "failure",
    ["slow_fetch", "refused_fetch", "interrupted_fetch", "missing_hours", "limit_reached", "failed_calculation"],
)
def test_a_failed_next_night_keeps_the_run_of_the_night_before(
    failure: str, night_d: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _published_values(pipeline=night_d, today=DAY)
    assert {item["assessment_date"] for item in before.values()} == {DAY.isoformat()}
    assert all(isinstance(item["percentage"], int) for item in before.values())
    clock = _clock(day=NEXT_DAY)
    if failure == "limit_reached":
        _fill_quota(window="day", now=clock.now)
    overrides = {"slow_fetch": _slow, "refused_fetch": _refused, "interrupted_fetch": _interrupted}
    inner = None
    if failure == "missing_hours":
        fake = _FakeOpenMeteo()

        def truncated(*, url: str, timeout: float) -> object:
            payload = fake(url=url, timeout=timeout)
            for location in payload:
                location["hourly"] = {key: values[:-5] for key, values in location["hourly"].items()}
            return payload

        inner = truncated

    weather, transport = _run_weather(
        pipeline=night_d, day=NEXT_DAY, override=overrides.get(failure), inner=inner,
    )

    if failure == "failed_calculation":
        assert weather.status == ScheduledWeatherRun.Status.COMPLETED
        snapshots = {item.cell_key: item.id for item in WeatherSnapshot.objects.filter(assessment_date=NEXT_DAY)}
        failed = _run_forecast(
            day=NEXT_DAY,
            calculator=_recording_calculator(seen=[], fail_for={str(snapshots["43.60,11.20"])}),
        )
        assert failed.status == ForecastRun.Status.FAILED
    else:
        assert weather.status != ScheduledWeatherRun.Status.COMPLETED
        assert not WeatherSnapshot.objects.filter(assessment_date=NEXT_DAY).exists()
        if failure == "limit_reached":
            assert transport.count == 0
        with pytest.raises(ForecastRunError, match="no ready weather"):
            start_forecast_run(assessment_date=NEXT_DAY)

    pointer = PublishedForecastRun.objects.get(region_code="09")
    assert pointer.current_run_id == night_d.run.id
    assert not ForecastRun.objects.filter(status=ForecastRun.Status.PUBLISHED).exclude(id=night_d.run.id).exists()
    _forbid_providers(monkeypatch)
    after = _published_values(pipeline=night_d, today=NEXT_DAY)
    assert sorted({offset for offset, _ in after}) == list(range(1, 8))
    assert after == {key: value for key, value in before.items() if key[0] >= 1}
    assert night_d.client.get("/api/v1/info").json()["target_dates"] == [
        (DAY + timedelta(days=offset)).isoformat() for offset in range(1, 8)
    ]
    update_id = next(iter(after.values()))["update_id"]
    for offered in (DAY, DAY + timedelta(days=8)):
        assert _squares(pipeline=night_d, day=offered).json()["code"] == "unsupported_date"
        assert _overview(pipeline=night_d, day=offered).json()["code"] == "unsupported_date"
    too_late = _detail(
        pipeline=night_d, square_id=night_d.square_ids[0], day=DAY + timedelta(days=8), update_id=update_id,
    )
    assert too_late.status_code == 400
    assert too_late.json()["code"] == "invalid_date"


# Squares and places outside the prepared area


def test_squares_outside_the_prepared_area_give_a_reason_and_no_percentage(
    night_d: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_providers(monkeypatch)
    calls_before = night_d.transport.count
    lazio_id, noforest_id = night_d.square_ids[3], night_d.square_ids[4]
    paris_id = format_cell_id(corner_e=3_760_000, corner_n=2_890_000)
    paris_bbox = "2.30,48.80,2.31,48.81"
    day = DAY + timedelta(days=2)

    squares = _squares(pipeline=night_d, day=day)
    lazio = _square(response=squares, square_id=lazio_id)
    noforest = _square(response=squares, square_id=noforest_id)
    overview = _overview(pipeline=night_d, day=day)
    paris_squares = night_d.client.get("/api/v1/squares", {"bbox": paris_bbox, "target_date": day.isoformat()})
    paris_overview = night_d.client.get(
        "/api/v1/estimate-overview", {"bbox": paris_bbox, "target_date": day.isoformat()},
    )
    lazio_detail = _detail(pipeline=night_d, square_id=lazio_id, day=day, update_id=None)
    noforest_detail = _detail(pipeline=night_d, square_id=noforest_id, day=day, update_id=None)
    paris_detail = _detail(pipeline=night_d, square_id=paris_id, day=day, update_id=None)
    paris_point = _resolve_post(client=night_d.client, latitude=48.805, longitude=2.305)

    assert (lazio["state"], lazio["reason"], lazio["percentage"]) == ("unavailable", "outside_prepared_area", None)
    assert lazio["update_id"] is None
    assert (noforest["state"], noforest["reason"], noforest["percentage"]) == ("noforest", "noforest", None)
    assert overview.json()["features"][0]["properties"]["forest_square_count"] == 3
    assert paris_squares.status_code == 400
    assert paris_squares.json()["code"] == "outside_coverage"
    assert paris_overview.status_code == 200
    assert paris_overview.json()["features"] == []
    assert lazio_detail.status_code == 409
    assert lazio_detail.json()["code"] == "outside_prepared_area"
    assert noforest_detail.status_code == 409
    assert noforest_detail.json()["code"] == "no_forest"
    assert paris_detail.status_code == 404
    assert paris_detail.json()["code"] == "square_unknown"
    assert paris_point.status_code == 422
    assert paris_point.json()["code"] == "outside_italy"
    for body in (lazio_detail.json(), noforest_detail.json(), paris_detail.json()):
        assert "estimate" not in body
        assert "whole_percentage" not in json.dumps(body)
    assert night_d.transport.count == calls_before
