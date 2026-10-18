from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e.map_helpers import click_square
from tests.e2e.test_map_controls import (
    TARGET_DATES,
    _api_requests,
    _area_response,
    _feature,
    _info_response,
    _install_map_routes,
    _squares_response,
)


VIEWPORTS = {"desktop": (1440, 900), "phone": (390, 844)}
RECORD_SET_DATA = """
() => {
  const source = window.SporaMeteoMap.map.getSource('squares');
  const original = source.setData.bind(source);
  window.__setDataSizes = [];
  source.setData = (data) => {
    window.__setDataSizes.push(data?.features?.length ?? 0);
    return original(data);
  };
}
"""
HOLD_SQUARES = """
() => {
  const original = window.fetch.bind(window);
  window.__heldSquares = [];
  window.__holdSquares = true;
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    if (!window.__holdSquares || !url.includes('/api/v1/squares')) return original(input, init);
    // The held promise ignores the abort signal, so a superseded answer can still arrive.
    return new Promise((resolve) => window.__heldSquares.push({url, resolve}));
  };
}
"""


def _squares_for(target_date: str, *, run_id: str = "run-1") -> dict[str, object]:
    response = _squares_response(target_date)
    response["run_id"] = run_id
    response["assessment_date"] = "2026-10-12"
    for feature in response["features"]:
        properties = feature["properties"]
        if properties["update_id"]:
            properties["update_id"] = f"update-{run_id}"
        if run_id == "run-2" and properties["cell_id"] == "cell-zero":
            properties["percentage"] = 40
    return response


def _info(
    *,
    run_id: str | None = "run-1",
    today: str = TARGET_DATES[0],
    target_dates: tuple[str, ...] = TARGET_DATES,
    assessment_date: str = TARGET_DATES[0],
) -> dict[str, object]:
    info = _info_response()
    info["today"] = today
    info["target_dates"] = list(target_dates)
    info["run"] = (
        {
            "run_id": run_id,
            "assessment_date": assessment_date,
            "published_at": f"{assessment_date}T03:00:00Z",
            "target_dates": [value for value in target_dates if value >= today],
        }
        if run_id
        else None
    )
    return info


def _json(route: Route, body: dict[str, object], *, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def _open(
    page: Page,
    base_url: str,
    *,
    viewport: str = "desktop",
    current: dict[str, object] | None = None,
) -> dict[str, object]:
    """Open the map with one published run that tests can replace through the returned dict."""
    current = current if current is not None else {"info": _info(), "run_id": "run-1"}
    requests = _install_map_routes(page)
    page.route("**/api/v1/info**", lambda route: _json(route, current["info"]))

    def squares_route(route: Route) -> None:
        target_date = route.request.url.split("target_date=")[1].split("&")[0]
        build = current.get("squares_for", _squares_for)
        _json(route, build(target_date, run_id=str(current["run_id"])))

    page.route("**/api/v1/squares**", squares_route)
    width, height = VIEWPORTS[viewport]
    page.set_viewport_size({"width": width, "height": height})
    page.goto(
        url=f"{base_url}/map/?center=44.13,10.65&zoom=12&date={TARGET_DATES[0]}",
        wait_until="domcontentloaded",
    )
    page.wait_for_function(
        "() => window.SporaMeteoMap?.getDiagnostics().squareSetCount >= 1", timeout=15_000
    )
    page.wait_for_function(
        """
        () => window.SporaMeteoMap.map.getLayoutProperty('sporameteo-square-estimates', 'visibility') === 'visible'
        """,
        timeout=15_000,
    )
    first_date = current["info"]["target_dates"][0]
    page.locator(f".date-btn[data-date='{first_date}']").click(force=True)
    page.wait_for_function(
        "(date) => window.SporaMeteoMap.getDiagnostics().displayedDate === date",
        arg=first_date,
        timeout=15_000,
    )
    current["requests"] = requests
    return current


def _squares_state(page: Page) -> dict[str, object]:
    return page.evaluate(
        """
        () => {
          const map = window.SporaMeteoMap.map;
          return {
            visibility: map.getLayoutProperty('sporameteo-square-estimates', 'visibility'),
            rendered: map.queryRenderedFeatures({layers: ['sporameteo-square-estimates']}).length,
            setDataSizes: window.__setDataSizes,
            diagnostics: window.SporaMeteoMap.getDiagnostics(),
          };
        }
        """
    )


def _release(page: Page, *, index: int, body: dict[str, object]) -> None:
    page.evaluate(
        """
        ([index, body]) => window.__heldSquares[index].resolve(
          new Response(JSON.stringify(body), {status: 200, headers: {'Content-Type': 'application/json'}})
        )
        """,
        [index, body],
    )


def _wait_for_held(page: Page, *, count: int) -> None:
    page.wait_for_function(
        "(count) => (window.__heldSquares || []).length >= count", arg=count, timeout=10_000
    )


@pytest.mark.parametrize("viewport", ["desktop", "phone"])
def test_new_date_keeps_the_overlay_on_screen_and_swaps_once(
    page: Page, base_url: str, viewport: str
) -> None:
    _open(page, base_url, viewport=viewport)
    page.evaluate(RECORD_SET_DATA)
    page.evaluate(HOLD_SQUARES)
    page.locator(".date-btn[data-date='2026-10-16']").click(force=True)
    _wait_for_held(page, count=1)

    waiting = _squares_state(page)
    assert waiting["visibility"] == "visible"
    assert waiting["rendered"] > 0
    assert waiting["setDataSizes"] == []
    expect(page.locator("[data-map-app]")).to_have_attribute("data-map-stale", "true")
    assert waiting["diagnostics"]["displayedDate"] == TARGET_DATES[0]

    _release(page, index=0, body=_squares_for("2026-10-16"))
    page.wait_for_function(
        "() => window.SporaMeteoMap.getDiagnostics().displayedDate === '2026-10-16'", timeout=10_000
    )
    arrived = _squares_state(page)
    assert arrived["setDataSizes"] == [5]
    assert arrived["visibility"] == "visible"
    expect(page.locator("[data-map-app]")).not_to_have_attribute("data-map-stale", "true")


@pytest.mark.parametrize("viewport", ["desktop", "phone"])
def test_moved_view_keeps_the_overlay_on_screen_and_swaps_once(
    page: Page, base_url: str, viewport: str
) -> None:
    _open(page, base_url, viewport=viewport)
    page.evaluate(RECORD_SET_DATA)
    page.evaluate(HOLD_SQUARES)
    page.evaluate("() => window.SporaMeteoMap.map.jumpTo({center: [10.652, 44.131], zoom: 12.2})")
    _wait_for_held(page, count=1)

    waiting = _squares_state(page)
    assert waiting["visibility"] == "visible"
    assert waiting["setDataSizes"] == []

    page.evaluate("() => { window.__holdSquares = false; }")
    _release(page, index=0, body=_squares_for(TARGET_DATES[0]))
    page.wait_for_function("() => window.__setDataSizes.length === 1", timeout=10_000)
    assert _squares_state(page)["setDataSizes"] == [5]


def test_superseded_slow_response_arriving_last_is_ignored(page: Page, base_url: str) -> None:
    _open(page, base_url)
    page.evaluate(RECORD_SET_DATA)
    page.evaluate(HOLD_SQUARES)
    page.locator(".date-btn[data-date='2026-10-16']").click()
    _wait_for_held(page, count=1)
    page.locator(".date-btn[data-date='2026-10-17']").click()
    _wait_for_held(page, count=2)

    _release(page, index=1, body=_squares_for("2026-10-17"))
    page.wait_for_function(
        "() => window.SporaMeteoMap.getDiagnostics().displayedDate === '2026-10-17'", timeout=10_000
    )
    _release(page, index=0, body=_squares_for("2026-10-16"))
    page.wait_for_timeout(400)

    state = _squares_state(page)
    assert state["setDataSizes"] == [5]
    assert state["diagnostics"]["displayedDate"] == "2026-10-17"
    expect(page.locator("[data-map-app]")).to_have_attribute("data-target-date", "2026-10-17")


def test_response_for_another_date_or_run_is_dropped(page: Page, base_url: str) -> None:
    _open(page, base_url)
    page.evaluate(RECORD_SET_DATA)
    page.evaluate(HOLD_SQUARES)
    page.locator(".date-btn[data-date='2026-10-16']").click()
    _wait_for_held(page, count=1)
    _release(page, index=0, body=_squares_for("2026-10-15"))
    page.wait_for_timeout(400)
    assert _squares_state(page)["setDataSizes"] == []

    page.locator(".date-btn[data-date='2026-10-17']").click()
    _wait_for_held(page, count=2)
    _release(page, index=1, body=_squares_for("2026-10-17", run_id="run-other"))
    page.wait_for_timeout(400)
    state = _squares_state(page)
    assert state["setDataSizes"] == []
    assert state["diagnostics"]["displayedRunId"] == "run-1"


@pytest.mark.parametrize("viewport", ["desktop", "phone"])
def test_new_run_from_the_info_refresh_swaps_the_data_once(
    page: Page, base_url: str, viewport: str
) -> None:
    page.clock.install()
    current = _open(page, base_url, viewport=viewport)
    page.evaluate(RECORD_SET_DATA)
    assert _squares_state(page)["diagnostics"]["displayedRunId"] == "run-1"

    current["info"] = _info(run_id="run-2")
    current["run_id"] = "run-2"
    page.clock.run_for(900_000)
    page.wait_for_function(
        "() => window.SporaMeteoMap.getDiagnostics().displayedRunId === 'run-2'", timeout=10_000
    )
    page.wait_for_timeout(500)
    state = _squares_state(page)
    assert state["setDataSizes"] == [5]
    assert state["visibility"] == "visible"
    assert state["rendered"] > 0


def test_run_change_reloads_open_details_once_without_a_loading_state(
    page: Page, base_url: str
) -> None:
    page.clock.install()
    current = _open(page, base_url)
    area_requests: list[dict[str, list[str]]] = []

    def area_route(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        area_requests.append(query)
        update_id = query.get("update_id", [""])[0] or f"update-{current['run_id']}"
        body = _area_response(cell_id="cell-zero", target_date=TARGET_DATES[0], update_id=update_id)
        body["estimate"]["whole_percentage"] = 40 if update_id == "update-run-2" else 0
        _json(route, body)

    page.route("**/api/v1/areas/**", area_route)
    click_square(page, lng_lat=(10.64, 44.13), cell_id="cell-zero")
    details = page.locator("aside[data-live-details]")
    expect(details).to_contain_text("0%")
    area_requests.clear()
    page.evaluate(
        """
        () => {
          window.__sawLoading = false;
          new MutationObserver(() => {
            const panel = document.querySelector('aside[data-live-details]');
            if (!panel || panel.textContent.includes('Loading this area')) window.__sawLoading = true;
          }).observe(document.body, {subtree: true, childList: true, characterData: true});
        }
        """
    )

    current["info"] = _info(run_id="run-2")
    current["run_id"] = "run-2"
    page.clock.run_for(900_000)
    expect(details).to_contain_text("40%")
    page.wait_for_timeout(500)
    assert page.evaluate("() => window.__sawLoading") is False
    # Each date of the strip loads once for the selected square, and the open details load once more.
    assert {tuple(query["update_id"]) for query in area_requests} == {("update-run-2",)}
    per_date = [query["target_date"][0] for query in area_requests]
    assert sorted(per_date) == sorted([*TARGET_DATES, TARGET_DATES[0]])


def _squares_of_two_regions(target_date: str, *, run_id: str) -> dict[str, object]:
    """Give cell-zero to region 09 and cell-hundred to region 12, each with its own assessment date."""
    response = _squares_for(target_date, run_id=run_id)
    for feature in response["features"]:
        if feature["properties"]["cell_id"] == "cell-hundred":
            feature["properties"]["assessment_date"] = "2026-10-13"
    return response


def test_squares_of_two_regions_show_together_and_a_region_run_keeps_the_details(
    page: Page, base_url: str
) -> None:
    info = _info(run_id="map-id")
    info["runs"] = [
        {"region_code": "09", "run_id": "run-09", "assessment_date": "2026-10-12", "published_at": "2026-10-12T03:00:00Z"},
        {"region_code": "12", "run_id": "run-12", "assessment_date": "2026-10-13", "published_at": "2026-10-13T03:00:00Z"},
    ]
    _open(page, base_url, current={"info": info, "run_id": "map-id", "squares_for": _squares_of_two_regions})
    area_requests: list[str] = []

    def area_route(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        area_requests.append(query["target_date"][0])
        body = _area_response(
            cell_id="cell-hundred", target_date=query["target_date"][0], update_id=query.get("update_id", [""])[0],
        )
        # A detail names the run of its own region, which is not the run ID of the whole map.
        body["selection"]["run_id"] = "run-12"
        _json(route, body)

    page.route("**/api/v1/areas/**", area_route)
    shown = page.evaluate(
        """
        () => {
          const map = window.SporaMeteoMap.map;
          const ids = ['cell-zero', 'cell-hundred'];
          return Object.fromEntries(ids.map((id) => {
            const features = map.queryRenderedFeatures({layers: ['sporameteo-square-estimates']})
              .filter((feature) => feature.properties?.cell_id === id);
            return [id, features.map((feature) => [feature.properties.percentage, feature.properties.assessment_date])];
          }));
        }
        """
    )
    assert shown["cell-zero"][0] == [0, "2026-10-12"]
    assert shown["cell-hundred"][0] == [100, "2026-10-13"]

    click_square(page, lng_lat=(10.645, 44.13), cell_id="cell-hundred")
    expect(page.locator("aside[data-live-details]")).to_contain_text("100%")
    page.wait_for_timeout(500)
    count_after_open = len(area_requests)
    page.wait_for_timeout(500)
    assert len(area_requests) == count_after_open


def test_unchanged_run_in_the_info_refresh_keeps_the_data(page: Page, base_url: str) -> None:
    page.clock.install()
    current = _open(page, base_url)
    page.evaluate(RECORD_SET_DATA)
    before = len(_api_requests(current["requests"], "/squares"))
    page.clock.run_for(900_000)
    page.wait_for_timeout(500)
    assert _squares_state(page)["setDataSizes"] == []
    assert len(_api_requests(current["requests"], "/squares")) == before


@pytest.mark.parametrize("viewport", ["desktop", "phone"])
def test_old_run_shows_no_run_note_and_no_centre_button(
    page: Page, base_url: str, viewport: str
) -> None:
    info = _info(
        today="2026-10-12",
        target_dates=TARGET_DATES[1:],
        assessment_date="2026-10-11",
    )
    _open(page, base_url, viewport=viewport, current={"info": info, "run_id": "run-1"})

    first = page.locator(".date-btn").first
    expect(first).to_have_attribute("data-date", TARGET_DATES[1])
    expect(first.locator(".d-wd")).not_to_have_text("Today")
    expect(page.locator("#mapmsgs")).not_to_contain_text("Estimates from")
    expect(page.locator("[data-live-state='older']")).to_have_count(0)
    expect(page.locator("[data-live-state='error']")).to_have_count(0)
    expect(page.locator("#centre-btn")).to_have_count(0)
    expect(page.locator(".maptop [data-center-select]")).to_have_count(0)


def test_today_label_marks_the_date_equal_to_today(page: Page, base_url: str) -> None:
    info = _info(today=TARGET_DATES[1], target_dates=TARGET_DATES[1:])
    _open(page, base_url, current={"info": info, "run_id": "run-1"})
    expect(page.locator(".date-btn").first.locator(".d-wd")).to_have_text("Today")
    expect(page.locator(".date-btn").nth(1).locator(".d-wd")).not_to_have_text("Today")


def test_missing_run_shows_a_not_ready_message_with_retry(page: Page, base_url: str) -> None:
    _install_map_routes(page)
    page.route("**/api/v1/info**", lambda route: _json(route, _info(run_id=None)))
    page.route(
        "**/api/v1/squares**",
        lambda route: _json(
            route,
            {
                "code": "forecast_unavailable",
                "message": "No forecast run is published yet.",
                "message_key": "estimate.forecast_unavailable",
                "details": {},
            },
            status=503,
        ),
    )
    page.goto(url=f"{base_url}/map/?center=44.13,10.65&zoom=12", wait_until="domcontentloaded")
    error = page.locator("[data-live-state='error']")
    expect(error).to_contain_text("Estimates are not ready yet. Try again later.")
    expect(error.locator("button")).to_be_visible()


def test_square_outside_the_prepared_area_is_named_in_the_details(
    page: Page, base_url: str
) -> None:
    current = {"info": _info(), "run_id": "run-1"}
    _open(page, base_url, current=current)

    def squares_route(route: Route) -> None:
        body = _squares_for(TARGET_DATES[0])
        body["features"] = [
            _feature(
                cell_id="cell-outside",
                center_x=10.64,
                center_y=44.13,
                state="unavailable",
                percentage=None,
                reason="outside_prepared_area",
                update_id=None,
            )
        ]
        _json(route, body)

    def area_route(route: Route) -> None:
        body = _area_response(cell_id="cell-zero", target_date=TARGET_DATES[0], update_id="")
        body["selection"]["square_id"] = "cell-outside"
        body["estimate"].update(
            {
                "state": "unavailable",
                "whole_percentage": None,
                "unavailable_reason": "outside_prepared_area",
            }
        )
        _json(route, body)

    page.route("**/api/v1/squares**", squares_route)
    page.route("**/api/v1/areas/**", area_route)
    page.evaluate("() => window.SporaMeteoMap.map.jumpTo({center: [10.651, 44.131], zoom: 12.1})")
    click_square(
        page,
        lng_lat=(10.64, 44.13),
        cell_id="cell-outside",
        layer="sporameteo-square-missing",
    )
    expect(page.locator("aside[data-live-details]")).to_contain_text("no published estimate")
