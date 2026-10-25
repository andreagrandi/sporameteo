"""Browser checks for place search and deliberate location use."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Browser, Page, Route, expect

from tests.e2e.map_helpers import wait_for_map_settled
from tests.e2e.test_map_controls import (
    TARGET_DATES,
    _squares_response,
    _install_map_routes,
    _wait_for_map_settled,
    _wait_for_square_request,
)


def _open_fixture(page: Page, base_url: str, *, language: str = "en") -> None:
    page.set_default_timeout(10_000)
    page.set_extra_http_headers(
        headers={"Accept-Language": "it-IT,it;q=0.9" if language == "it" else "en-GB,en;q=0.9"}
    )
    page.goto(url=f"{base_url}/map/fixture/ready/", wait_until="domcontentloaded")


def _open_controlled_map(
    page: Page,
    base_url: str,
    *,
    language: str = "en",
    viewport: tuple[int, int] = (1440, 900),
    omit_selected_when_far: bool = False,
    resolve_code: str | None = None,
    delay_map_load: bool = False,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    requests = _install_map_routes(page)
    resolve_requests: list[dict[str, object]] = []

    if omit_selected_when_far:
        def squares_filter(route: Route) -> None:
            query = parse_qs(urlparse(route.request.url).query)
            bbox = query.get("bbox", [""])[0].split(",")
            if len(bbox) == 4 and float(bbox[0]) > 11:
                target_date = query.get("target_date", [TARGET_DATES[0]])[0]
                body = _squares_response(target_date)
                body["features"] = [
                    feature
                    for feature in body["features"]
                    if feature["properties"]["cell_id"] != "cell-zero"
                ]
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(body),
                )
                return
            route.fallback()

        page.route("**/api/v1/squares**", squares_filter)

    def search_route(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"results":['
                '{"name":"Pistoia","context":"Pistoia, Toscana",'
                '"latitude":44.1305,"longitude":10.6405}'
                '],"cached":false}'
            ),
        )

    def resolve_route(route: Route) -> None:
        payload = route.request.post_data_json
        assert isinstance(payload, dict)
        resolve_requests.append({**payload, "_url": route.request.url})
        latitude = float(payload["latitude"])
        longitude = float(payload["longitude"])
        if resolve_code:
            route.fulfill(
                status=422,
                content_type="application/json",
                body=json.dumps({"code": resolve_code}),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "status": "ok",
                    "point": {"latitude": latitude, "longitude": longitude},
                    "representative": {"latitude": 44.13, "longitude": 10.64},
                    "square_id": "cell-zero",
                    "region": "Pistoia",
                }
            ),
        )

    page.route("**/api/v1/places/search**", search_route)
    page.route("**/api/v1/places/resolve**", resolve_route)
    if delay_map_load:
        page.add_init_script(
            """
            window.__releaseMapLoad = null;
            window.__constructedMap = null;
            let maplibreValue = null;
            Object.defineProperty(window, 'maplibregl', {
              configurable: true,
              get: () => maplibreValue,
              set: (value) => {
                if (!value?.Map || value.Map.__sporameteoDelayed) {
                  maplibreValue = value;
                  return;
                }
                const OriginalMap = value.Map;
                class DelayedMap extends OriginalMap {
                  constructor(...args) {
                    super(...args);
                    this.__sporameteoConstructed = true;
                    window.__constructedMap = this;
                  }

                  once(event, listener) {
                    if (event === 'load' && this.__sporameteoConstructed) {
                      window.__releaseMapLoad = () => {
                        window.__releaseMapLoad = null;
                        listener.call(this, {type: 'load', target: this});
                      };
                      return this;
                    }
                    return super.once(event, listener);
                  }
                }
                Object.defineProperty(DelayedMap, '__sporameteoDelayed', {value: true});
                maplibreValue = {...value, Map: DelayedMap};
              },
            });
            """
        )
    page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    page.set_extra_http_headers(
        {"Accept-Language": "it-IT,it;q=0.9" if language == "it" else "en-GB,en;q=0.9"}
    )
    page.goto(
        url=f"{base_url}/map/?center=44.14,10.65&zoom=12&date={TARGET_DATES[0]}",
        wait_until="domcontentloaded",
    )
    if delay_map_load:
        page.wait_for_function(
            "() => Boolean(window.__constructedMap && window.__releaseMapLoad)",
            timeout=15_000,
        )
        return requests, resolve_requests
    page.wait_for_function(
        """
        () => Boolean(
          window.SporaMeteoMap?.map &&
          document.querySelector('#map canvas.maplibregl-canvas')
        )
        """,
        timeout=15_000,
    )
    page.wait_for_function(
        "() => Boolean(window.SporaMeteoMap?.map?.isStyleLoaded?.())",
        timeout=15_000,
    )
    page.wait_for_function(
        "() => Boolean(window.SporaMeteoMap?.map?.getSource?.('squares'))",
        timeout=15_000,
    )
    first_date = page.locator(f".date-btn[data-date='{TARGET_DATES[0]}']")
    first_date.click()
    _wait_for_square_request(page, requests, TARGET_DATES[0])
    _wait_for_map_settled(page, TARGET_DATES[0])
    return requests, resolve_requests


@pytest.mark.parametrize("source", ["search", "geolocation"])
def test_destination_selected_before_map_ready_is_retained(
    page: Page, base_url: str, source: str
) -> None:
    if source == "geolocation":
        page.add_init_script(
            """
            navigator.geolocation.getCurrentPosition = (success) => {
              success({coords: {latitude: 44.1307, longitude: 10.6407}});
            };
            """
        )
    _open_controlled_map(page, base_url, delay_map_load=True)
    root = page.locator("[data-map-app]")
    if source == "search":
        search = page.locator("[data-place-search]")
        search.fill("Pistoia")
        expect(page.locator("[data-search-results] [role=option]")).to_have_count(1)
        search.press("ArrowDown")
        search.press("Enter")
    else:
        page.locator("[data-location]").click()

    expect(root).to_have_attribute("data-selected-square", "")
    expect(root).to_have_attribute("data-selection-source", source)
    page.evaluate("() => window.__releaseMapLoad?.()")
    page.wait_for_function(
        "() => document.querySelector('[data-map-app]').dataset.selectedSquare === 'cell-zero'",
        timeout=15_000,
    )
    expect(root).to_have_attribute("data-selection-source", source)
    expected_latitude = "44.1305" if source == "search" else "44.1307"
    expect(root).to_have_attribute("data-point-latitude", expected_latitude)


def test_shared_cache_and_repeated_search_skip_provider_requests(page: Page, base_url: str) -> None:
    _open_fixture(page, base_url)
    requests: list[str] = []
    result = {"name": "Femminamorta", "context": "Marliana, Pistoia", "latitude": 43.97, "longitude": 10.79}

    def cached_response(route: Route) -> None:
        requests.append(route.request.url)
        assert "cached_only=1" in route.request.url
        route.fulfill(json={"results": [result], "complete": True, "cached": True})

    page.route("**/api/v1/places/search**", cached_response)
    search = page.locator("[data-place-search]")
    search.fill("Femminamorta")
    expect(page.locator(".result-name")).to_have_text("Femminamorta")
    assert len(requests) == 1
    search.fill("")
    search.fill("  FEMMINAMORTA  ")
    assert page.locator(".result-name").inner_text() == "Femminamorta"
    page.wait_for_timeout(300)
    assert len(requests) == 1


def test_cached_prefix_stays_selectable_when_fresh_request_fails(page: Page, base_url: str) -> None:
    _open_fixture(page, base_url)
    result = {"name": "Femminamorta", "context": "Marliana, Pistoia", "latitude": 43.97, "longitude": 10.79}

    def search_response(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        if "cached_only" in query:
            route.fulfill(json={"results": [result], "complete": query["q"][0].casefold() == "femminamorta"})
        else:
            route.fulfill(status=504, json={"code": "provider_timeout"})

    page.route("**/api/v1/places/search**", search_response)
    search = page.locator("[data-place-search]")
    search.fill("Femminamorta")
    expect(page.locator(".result-name")).to_have_text("Femminamorta")
    search.fill("Femmina")
    assert page.locator(".result-name").inner_text() == "Femminamorta"
    expect(page.locator("[data-search-status]")).to_contain_text("Place search took too long")
    expect(page.locator(".result-name")).to_have_text("Femminamorta")
    search.press("ArrowDown")
    search.press("Enter")
    assert page.evaluate("window.__sporameteoPendingDestination.latitude") == 43.97


def test_highlighted_preview_result_stays_selected_when_fresh_results_arrive(
    page: Page, base_url: str
) -> None:
    _open_fixture(page, base_url)
    result = {"name": "Pistoia", "context": "Pistoia, Toscana", "latitude": 44.1305, "longitude": 10.6405}
    held_routes: list[Route] = []

    def search_response(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        if "cached_only" in query:
            route.fulfill(json={"results": [result], "complete": False})
        else:
            held_routes.append(route)

    page.route("**/api/v1/places/search**", search_response)
    search = page.locator("[data-place-search]")
    search.fill("Pistoia")
    option = page.locator("[data-search-results] [role=option]")
    expect(option).to_have_count(1)
    search.press("ArrowDown")
    expect(option).to_have_attribute("aria-selected", "true")
    for _ in range(100):
        if held_routes:
            break
        page.wait_for_timeout(50)
    assert len(held_routes) == 1
    held_routes[0].fulfill(
        json={"results": [{**result, "context": "Pistoia, Toscana, Italia"}], "cached": False}
    )
    expect(option).to_contain_text("Pistoia, Toscana, Italia")
    expect(option).to_have_attribute("aria-selected", "true")
    search.press("Enter")
    assert page.evaluate("() => window.__sporameteoPendingDestination?.source") == "search"


def test_search_combobox_supports_context_and_keyboard_selection(page: Page, base_url: str) -> None:
    _open_fixture(page, base_url)
    page.route(
        "**/api/v1/places/search**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"results":['
                '{"name":"Pistoia","context":"Pistoia, Toscana","latitude":43.93,"longitude":10.92},'
                '{"name":"Pistoia","context":"Pistoia, Abruzzo","latitude":42.01,"longitude":13.98}'
                '],"cached":false}'
            ),
        ),
    )

    search = page.locator("[data-place-search]")
    expect(search).to_have_attribute("role", "combobox")
    search.fill("Pistoia")
    expect(page.locator("[data-search-results] [role=option]")).to_have_count(2)
    expect(page.locator("[data-search-results]")).to_contain_text("Pistoia, Abruzzo")
    expect(page.locator("[data-search-results] .search-attribution a[href='https://photon.komoot.io/']")).to_be_visible()
    expect(page.locator("[data-search-results] .search-attribution a[href='https://www.openstreetmap.org/copyright']")).to_be_visible()
    search.press("ArrowDown")
    expect(page.locator("[data-search-results] [role=option]").first).to_have_attribute(
        "aria-selected", "true"
    )
    search.press("Enter")
    expect(search).to_have_value("Pistoia")
    assert page.evaluate("() => window.__sporameteoPendingDestination?.source") == "search"


def test_reselecting_same_place_fetches_after_noop_map_move(page: Page, base_url: str) -> None:
    _open_controlled_map(page, base_url)
    search = page.locator("[data-place-search]")
    search.fill("Pistoia")
    expect(page.locator("[data-search-results] [role=option]")).to_have_count(1)
    search.press("ArrowDown")
    search.press("Enter")
    root = page.locator("[data-map-app]")
    page.wait_for_function(
        "() => document.querySelector('[data-map-app]').dataset.selectedSquare === 'cell-zero'",
        timeout=10_000,
    )
    page.wait_for_function("() => !window.SporaMeteoMap.map.isMoving()")
    page.evaluate(
        """
        () => {
          const map = window.SporaMeteoMap.map;
          window.__noopEaseToCalls = 0;
          map.easeTo = () => {
            window.__noopEaseToCalls += 1;
            map.__sporameteoNoopMoving = true;
            window.setTimeout(() => {
              map.__sporameteoNoopMoving = false;
              map.fire('moveend');
            }, 0);
          };
          map.isMoving = () => Boolean(map.__sporameteoNoopMoving);
        }
        """
    )
    requests_before = page.evaluate("() => window.SporaMeteoMap.getDiagnostics().requestCount")
    search.fill("Pistoia")
    expect(page.locator("[data-search-results] [role=option]")).to_have_count(1)
    search.press("ArrowDown")
    search.press("Enter")
    page.wait_for_function(
        "(before) => window.SporaMeteoMap.getDiagnostics().requestCount > before",
        arg=requests_before,
        timeout=10_000,
    )
    page.wait_for_function(
        "() => document.querySelector('[data-map-app]').dataset.selectedSquare === 'cell-zero'",
        timeout=10_000,
    )
    assert page.evaluate("() => window.__noopEaseToCalls") == 1
    expect(page.locator("aside[data-live-details]")).to_be_visible()
    expect(root).to_have_attribute("data-selection-source", "search")


@pytest.mark.parametrize("language", ["en", "it"])
def test_search_no_results_is_localized_and_manual_map_remains_available(
    page: Page, base_url: str, language: str
) -> None:
    _open_fixture(page, base_url, language=language)
    page.route(
        "**/api/v1/places/search**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"results":[],"cached":false}',
        ),
    )

    page.locator("[data-place-search]").fill("zzzz")
    expect(page.locator("[data-search-results] .result-empty")).to_be_visible()
    expect(page.locator("#map")).to_be_visible()
    expected = "No Italian places found." if language == "en" else "Nessun luogo italiano trovato."
    expect(page.locator("[data-search-status]")).to_contain_text(expected)


@pytest.mark.parametrize("language", ["en", "it"])
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "invalid_query"),
        (429, "throttled"),
        (504, "provider_timeout"),
        (503, "provider_unavailable"),
    ],
)
def test_search_provider_errors_show_one_localized_message(
    page: Page, base_url: str, language: str, status: int, code: str
) -> None:
    _open_fixture(page, base_url, language=language)
    page.route(
        "**/api/v1/places/search**",
        lambda route: route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps({"code": code}),
        ),
    )

    page.locator("[data-place-search]").fill("Pistoia")
    message = page.locator("[data-search-results] .result-error")
    expect(message).to_be_visible()
    expect(page.locator("[data-search-results] .result-empty")).to_have_count(0)
    expected = {
        "invalid_query": "Place search is unavailable. Try again or move the map."
        if language == "en"
        else "La ricerca dei luoghi non è disponibile. Riprova o sposta la mappa.",
        "throttled": "Place search is busy" if language == "en" else "La ricerca dei luoghi è occupata",
        "provider_timeout": "Place search took too long" if language == "en" else "La ricerca dei luoghi è troppo lenta",
        "provider_unavailable": "Place search is unavailable" if language == "en" else "La ricerca dei luoghi non è disponibile",
    }[code]
    expect(message).to_contain_text(expected)


def test_delayed_old_search_response_cannot_reopen_dismissed_results(
    page: Page, base_url: str
) -> None:
    page.add_init_script(
        """
        window.__releaseOldPlaceSearch = null;
        const originalFetch = window.fetch;
        window.fetch = (input, init) => {
          const url = new URL(String(input), window.location.href);
          if (url.pathname.endsWith('/api/v1/places/search') && url.searchParams.get('q') === 'Pistoia') {
            return new Promise((resolve) => {
              window.__releaseOldPlaceSearch = () => resolve(new Response(
                JSON.stringify({results: [{name: 'Pistoia', context: 'Toscana', latitude: 44.13, longitude: 10.64}]}),
                {status: 200, headers: {'Content-Type': 'application/json'}}
              ));
            });
          }
          return originalFetch(input, init);
        };
        """
    )
    _open_fixture(page, base_url)
    search = page.locator("[data-place-search]")
    search.fill("Pistoia")
    page.wait_for_function("() => typeof window.__releaseOldPlaceSearch === 'function'")
    page.evaluate("() => document.body.dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    expect(page.locator("[data-search-results]")).to_be_hidden()
    page.evaluate("() => window.__releaseOldPlaceSearch?.()")
    page.wait_for_timeout(300)
    expect(page.locator("[data-search-results]")).to_be_hidden()


def test_editing_search_clears_old_active_result_before_selection(
    page: Page, base_url: str
) -> None:
    _open_fixture(page, base_url)

    def search_route(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query).get("q", [""])[0]
        result = {
            "Pistoia": {"name": "Pistoia", "context": "Toscana", "latitude": 44.13, "longitude": 10.64},
            "Abetone": {"name": "Abetone", "context": "Pistoia", "latitude": 44.14, "longitude": 10.66},
        }.get(query)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"results": [result] if result else []}),
        )

    page.route("**/api/v1/places/search**", search_route)
    search = page.locator("[data-place-search]")
    search.fill("Pistoia")
    expect(page.locator("[data-search-results] [role=option]")).to_have_count(1)
    search.press("ArrowDown")
    search.fill("Abetone")
    expect(page.locator("[data-search-results] [role=option]")).to_have_count(1)
    expect(page.locator("[data-search-results] [role=option]").first).to_contain_text("Abetone")
    search.press("ArrowDown")
    search.press("Enter")
    pending = page.evaluate("() => window.__sporameteoPendingDestination")
    assert pending["source"] == "search"
    assert pending["latitude"] == pytest.approx(44.14)


def test_location_is_called_only_after_button_press_and_keeps_search_available(
    page: Page, base_url: str
) -> None:
    page.add_init_script(
        """
        window.__locationCalls = 0;
        navigator.geolocation.getCurrentPosition = (success) => {
          window.__locationCalls += 1;
          success({coords: {latitude: 43.93, longitude: 10.92}});
        };
        """
    )
    _open_fixture(page, base_url)

    assert page.evaluate("() => window.__locationCalls") == 0
    page.locator("[data-location]").click()
    expect(page.locator("[data-search-status]")).to_contain_text("Use my location")
    assert page.evaluate("() => window.__locationCalls") == 1
    expect(page.locator("[data-place-search]")).to_be_visible()
    expect(page.locator("#map")).to_be_visible()


def test_search_selection_keeps_exact_point_through_date_map_and_centre_changes(
    page: Page, base_url: str
) -> None:
    requests, resolve_requests = _open_controlled_map(page, base_url)
    page.evaluate(
        """
        () => {
          window.__selectionEvents = [];
          window.addEventListener('sporameteo:selectionchange', (event) => {
            window.__selectionEvents.push(event.detail);
          });
        }
        """
    )
    page.locator(".date-btn[data-date='2026-10-15']").click()
    _wait_for_square_request(page, requests, "2026-10-15")

    search = page.locator("[data-place-search]")
    search.fill("Pistoia")
    expect(page.locator("[data-search-results] [role=option]")).to_have_count(1)
    search.press("ArrowDown")
    search.press("Enter")
    page.wait_for_function(
        "() => document.querySelector('[data-map-app]').dataset.selectedSquare === 'cell-zero'",
        timeout=10_000,
    )
    expect(page.locator("aside[data-live-details]")).to_be_visible()
    root = page.locator("[data-map-app]")
    expect(root).to_have_attribute("data-target-date", "2026-10-15")
    expect(root).to_have_attribute("data-point-latitude", "44.1305")
    expect(root).to_have_attribute("data-point-longitude", "10.6405")
    expect(root).to_have_attribute("data-selection-source", "search")
    assert resolve_requests[-1]["source"] == "search"

    search_event = page.evaluate(
        """
        () => window.__selectionEvents.findLast((event) => event.square === 'cell-zero')
        """
    )
    assert search_event["point"] == {"latitude": 44.1305, "longitude": 10.6405}
    assert search_event["representative"] == {"latitude": 44.13, "longitude": 10.64}
    assert search_event["destination"] == search_event["point"]
    assert search_event["source"] == "search"

    previous_squares = len([request for request in requests if request["path"].endswith("/squares")])
    page.locator(".date-btn[data-date='2026-10-12']").click()
    for _ in range(100):
        square_requests = [request for request in requests if request["path"].endswith("/squares")]
        if len(square_requests) > previous_squares and any(
            request["query"].get("target_date") == ["2026-10-12"]
            for request in square_requests[previous_squares:]
        ):
            break
        page.wait_for_timeout(100)
    else:
        pytest.fail("The map did not request the changed target date")
    expect(root).to_have_attribute("data-target-date", "2026-10-12")
    expect(root).to_have_attribute("data-selection-source", "search")
    expect(root).to_have_attribute("data-point-latitude", "44.1305")
    expect(root).to_have_attribute("data-point-longitude", "10.6405")

    wait_for_map_settled(page)
    page.wait_for_function(
        """
        () => window.SporaMeteoMap.map.queryRenderedFeatures(
          window.SporaMeteoMap.map.project([10.6415, 44.131]),
          {layers: ['sporameteo-square-estimates']}
        ).some((feature) => feature.properties?.cell_id === 'cell-zero')
        """
    )
    map_box = page.locator("#map").bounding_box()
    map_point = page.evaluate(
        """
        () => {
          const point = window.SporaMeteoMap.map.project([10.6415, 44.131]);
          return {x: point.x, y: point.y};
        }
        """
    )
    assert map_box
    page.mouse.click(x=map_box["x"] + map_point["x"], y=map_box["y"] + map_point["y"])
    expect(root).to_have_attribute("data-selection-source", "map")
    assert float(root.get_attribute("data-point-longitude") or "nan") == pytest.approx(
        10.6415, abs=0.0003
    )
    assert float(root.get_attribute("data-point-latitude") or "nan") == pytest.approx(
        44.131, abs=0.0003
    )
    assert page.evaluate(
        "() => window.__selectionEvents.findLast((event) => event.square === 'cell-zero')"
    )["source"] == "map"

    previous_squares = len([request for request in requests if request["path"].endswith("/squares")])
    page.evaluate(
        "() => window.SporaMeteoMap.map.jumpTo({center: [10.641, 44.131], zoom: 12})"
    )
    page.wait_for_function("() => !window.SporaMeteoMap.map.isMoving()")
    for _ in range(100):
        square_requests = [request for request in requests if request["path"].endswith("/squares")]
        if len(square_requests) > previous_squares:
            break
        page.wait_for_timeout(100)
    else:
        pytest.fail("The map did not request squares after panning")
    expect(root).to_have_attribute("data-selection-source", "map")
    assert float(root.get_attribute("data-point-longitude") or "nan") == pytest.approx(
        10.6415, abs=0.0003
    )
    page.wait_for_function(
        """
        () => window.SporaMeteoMap.map.queryRenderedFeatures(
          window.SporaMeteoMap.map.project([10.641, 44.131]),
          {layers: ['sporameteo-square-estimates']}
        ).some((feature) => feature.properties?.cell_id === 'cell-zero')
        """
    )

    page.evaluate("() => window.SporaMeteoMap.centerSelection()")
    expect(root).to_have_attribute("data-selection-source", "centre")
    expect(root).to_have_attribute("data-point-latitude", "")
    expect(root).to_have_attribute("data-point-longitude", "")
    assert float(root.get_attribute("data-representative-latitude") or "nan") == pytest.approx(
        44.13, abs=0.0003
    )
    assert float(root.get_attribute("data-representative-longitude") or "nan") == pytest.approx(
        10.64, abs=0.0003
    )
    centre_event = page.evaluate(
        """
        () => window.__selectionEvents.findLast((event) => event.source === 'centre')
        """
    )
    assert centre_event["point"] is None
    assert centre_event["representative"]["latitude"] == pytest.approx(44.13, abs=0.0003)
    assert centre_event["representative"]["longitude"] == pytest.approx(10.64, abs=0.0003)
    assert centre_event["destination"] == centre_event["representative"]


@pytest.mark.parametrize("language", ["en", "it"])
@pytest.mark.parametrize("code", [1, 2, 3])
def test_location_error_is_localized_on_the_real_map(
    page: Page, base_url: str, language: str, code: int
) -> None:
    page.add_init_script(
        """
        navigator.geolocation.getCurrentPosition = (_success, failure) => {
          failure({code: CODE});
        };
        """.replace("CODE", str(code))
    )
    _open_controlled_map(page, base_url, language=language)
    page.locator("[data-location]").click()
    expected = {
        ("en", 1): "Location access was declined.",
        ("it", 1): "L'accesso alla posizione è stato rifiutato.",
        ("en", 2): "Your location is unavailable.",
        ("it", 2): "La tua posizione non è disponibile.",
        ("en", 3): "Your location took too long",
        ("it", 3): "La tua posizione è lenta.",
    }[(language, code)]
    expect(page.locator("[data-search-status]")).to_contain_text(expected)
    expect(page.locator("[data-place-search]")).to_be_visible()
    expect(page.locator("#map")).to_be_visible()


@pytest.mark.parametrize("language", ["en", "it"])
def test_unsupported_location_reports_error_without_removing_search(
    page: Page, base_url: str, language: str
) -> None:
    page.add_init_script(
        """
        Object.defineProperty(navigator, 'geolocation', {value: undefined, configurable: true});
        """
    )
    _open_controlled_map(page, base_url, language=language)
    page.locator("[data-location]").click()
    expected = {
        "en": "This browser cannot provide a location.",
        "it": "Questo browser non può fornire una posizione.",
    }[language]
    expect(page.locator("[data-search-status]")).to_contain_text(expected)
    expect(page.locator("[data-place-search]")).to_be_visible()


@pytest.mark.parametrize("language", ["en", "it"])
@pytest.mark.parametrize("resolve_code", ["outside_italy", "outside_configured_coverage"])
def test_outside_resolution_keeps_point_without_selecting_square(
    page: Page, base_url: str, language: str, resolve_code: str
) -> None:
    _open_controlled_map(page, base_url, language=language, resolve_code=resolve_code)
    search = page.locator("[data-place-search]")
    search.fill("Pistoia")
    expect(page.locator("[data-search-results] [role=option]")).to_have_count(1)
    search.press("ArrowDown")
    search.press("Enter")
    root = page.locator("[data-map-app]")
    expect(root).to_have_attribute("data-selection-source", "search")
    expect(root).to_have_attribute("data-point-latitude", "44.1305")
    expect(root).to_have_attribute("data-selected-square", "")
    expected = {
        ("en", "outside_italy"): "outside Italy",
        ("it", "outside_italy"): "fuori dall'Italia",
        ("en", "outside_configured_coverage"): "no published estimate",
        ("it", "outside_configured_coverage"): "stima pubblicata",
    }[(language, resolve_code)]
    expect(page.locator("#mapmsgs")).to_contain_text(expected)


def test_stale_location_callbacks_cannot_replace_newer_search_or_map_selection(
    page: Page, base_url: str
) -> None:
    page.add_init_script(
        """
        window.__pendingLocation = null;
        navigator.geolocation.getCurrentPosition = (success, failure) => {
          window.__pendingLocation = {success, failure};
        };
        """
    )
    _open_controlled_map(page, base_url)
    page.locator("[data-location]").click()
    page.wait_for_function("() => window.__pendingLocation?.success")
    search = page.locator("[data-place-search]")
    search.fill("Pistoia")
    expect(page.locator("[data-search-results] [role=option]")).to_have_count(1)
    search.press("ArrowDown")
    search.press("Enter")
    root = page.locator("[data-map-app]")
    page.wait_for_function(
        "() => document.querySelector('[data-map-app]').dataset.selectedSquare === 'cell-zero'",
        timeout=10_000,
    )
    page.evaluate(
        "() => window.__pendingLocation.success({coords: {latitude: 44.2, longitude: 10.7}})"
    )
    expect(root).to_have_attribute("data-selection-source", "search")
    expect(root).to_have_attribute("data-point-latitude", "44.1305")

    # The details panel opens when its data arrives and makes the map narrower,
    # which moves the square. Read its position only after the map has resized.
    page.wait_for_function(
        """
        () => {
          const map = window.SporaMeteoMap.map;
          return !!document.querySelector('aside[data-live-details]')
            && map.getCanvas().clientWidth === map.getContainer().clientWidth;
        }
        """,
        timeout=10_000,
    )
    wait_for_map_settled(page)
    page.wait_for_function(
        """
        () => window.SporaMeteoMap.map.queryRenderedFeatures(
          window.SporaMeteoMap.map.project([10.6415, 44.131]),
          {layers: ['sporameteo-square-estimates']}
        ).some((feature) => feature.properties?.cell_id === 'cell-zero')
        """
    )
    map_box = page.locator("#map").bounding_box()
    map_point = page.evaluate(
        """
        () => {
          const point = window.SporaMeteoMap.map.project([10.6415, 44.131]);
          return {x: point.x, y: point.y};
        }
        """
    )
    assert map_box
    page.mouse.click(x=map_box["x"] + map_point["x"], y=map_box["y"] + map_point["y"])
    expect(root).to_have_attribute("data-selection-source", "map")
    page.evaluate("() => window.__pendingLocation.failure({code: 1})")
    expect(root).to_have_attribute("data-selection-source", "map")
    expect(page.locator("[data-location]")).not_to_have_attribute("aria-busy", "true")


def test_offscreen_destination_survives_pan_and_date_change(
    page: Page, base_url: str
) -> None:
    _, resolve_requests = _open_controlled_map(
        page,
        base_url,
        omit_selected_when_far=True,
    )
    search = page.locator("[data-place-search]")
    search.fill("Pistoia")
    expect(page.locator("[data-search-results] [role=option]")).to_have_count(1)
    search.press("ArrowDown")
    search.press("Enter")
    root = page.locator("[data-map-app]")
    # Wait until the squares for Pistoia open the details, so the pan below
    # always starts from the same state on slow and fast browsers.
    page.wait_for_function(
        """
        () => document.querySelector('[data-map-app]').dataset.selectedSquare === 'cell-zero'
          && !!document.querySelector('aside[data-live-details]')
        """,
        timeout=10_000,
    )
    page.wait_for_function("() => !window.SporaMeteoMap.map.isMoving()")

    with page.expect_response(
        lambda response: "/api/v1/squares" in response.url
        and float(parse_qs(urlparse(response.url).query)["bbox"][0].split(",")[0]) > 11,
        timeout=10_000,
    ):
        page.evaluate(
            "() => window.SporaMeteoMap.map.jumpTo({center: [11.5, 43.5], zoom: 12})"
        )
    page.wait_for_function("() => !window.SporaMeteoMap.map.isMoving()")
    # A selected area keeps its open details when it leaves the view.
    page.wait_for_function(
        """
        () => document.querySelector('[data-map-app]').dataset.selectedSquare === 'cell-zero'
          && !!document.querySelector('aside[data-live-details]')
        """,
        timeout=10_000,
    )
    page.locator(".date-btn[data-date='2026-10-15']").click()
    page.wait_for_function(
        "() => document.querySelector('[data-map-app]').dataset.targetDate === '2026-10-15'",
        timeout=10_000,
    )
    page.wait_for_function(
        "() => document.querySelector('[data-map-app]').dataset.selectedSquare === 'cell-zero'",
        timeout=10_000,
    )
    expect(root).to_have_attribute("data-point-latitude", "44.1305")
    expect(root).to_have_attribute("data-point-longitude", "10.6405")
    expect(root).to_have_attribute("data-selection-source", "search")
    assert resolve_requests[-1]["source"] == "search"


@pytest.mark.parametrize("language", ["en", "it"])
def test_touch_place_selection_renders_square_and_preserves_day(
    browser: Browser, base_url: str, language: str
) -> None:
    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True,
        is_mobile=True,
    )
    page = context.new_page()
    try:
        requests, resolve_requests = _open_controlled_map(
            page,
            base_url,
            language=language,
            viewport=(390, 844),
        )
        page.evaluate(
            """
            () => {
              window.__selectionEvents = [];
              window.addEventListener('sporameteo:selectionchange', (event) => {
                window.__selectionEvents.push(event.detail);
              });
            }
            """
        )
        search = page.locator("[data-place-search]")
        search.fill("Pistoia")
        option = page.locator("[data-search-results] [role=option]").first
        expect(option).to_be_visible()
        option.tap()
        root = page.locator("[data-map-app]")
        page.wait_for_function(
            "() => document.querySelector('[data-map-app]').dataset.selectedSquare === 'cell-zero'",
            timeout=10_000,
        )
        page.wait_for_function("() => !window.SporaMeteoMap.map.isMoving()")
        page.wait_for_function(
            """
            () => window.SporaMeteoMap.map.queryRenderedFeatures(
              window.SporaMeteoMap.map.project([10.6415, 44.131]),
              {layers: ['sporameteo-square-estimates']}
            ).some((feature) => feature.properties?.cell_id === 'cell-zero')
            """
        )
        expect(root).to_have_attribute("data-target-date", "2026-10-12")
        expect(root).to_have_attribute("data-selection-source", "search")
        expect(root).to_have_attribute("data-point-latitude", "44.1305")
        expect(root).to_have_attribute("data-point-longitude", "10.6405")
        event = page.evaluate(
            "() => window.__selectionEvents.findLast((item) => item.square === 'cell-zero')"
        )
        assert event["source"] == "search"
        assert event["point"] == {"latitude": 44.1305, "longitude": 10.6405}
        assert event["destination"] == event["point"]
        assert resolve_requests[-1]["source"] == "search"

        page.keyboard.press("Escape")

        map_box = page.locator("#map").bounding_box()
        map_point = page.evaluate(
            """
            () => {
              const point = window.SporaMeteoMap.map.project([10.6415, 44.131]);
              return {x: point.x, y: point.y};
            }
            """
        )
        assert map_box
        page.touchscreen.tap(
            x=map_box["x"] + map_point["x"],
            y=map_box["y"] + map_point["y"],
        )
        expect(root).to_have_attribute("data-selection-source", "map")
        assert float(root.get_attribute("data-point-latitude") or "nan") == pytest.approx(
            44.131, abs=0.0003
        )
        assert float(root.get_attribute("data-point-longitude") or "nan") == pytest.approx(
            10.6415, abs=0.0003
        )
        map_event = page.evaluate(
            "() => window.__selectionEvents.findLast((item) => item.source === 'map')"
        )
        assert map_event["square"] == "cell-zero"
        assert map_event["source"] == "map"
        assert map_event["point"]["latitude"] == pytest.approx(44.131, abs=0.0003)
        assert map_event["point"]["longitude"] == pytest.approx(10.6415, abs=0.0003)
        assert map_event["destination"] == map_event["point"]

        date_button = page.locator(".date-btn[data-date='2026-10-15']")
        date_box = date_button.bounding_box()
        assert date_box
        page.touchscreen.tap(
            x=date_box["x"] + date_box["width"] / 2,
            y=date_box["y"] + date_box["height"] / 2,
        )
        page.wait_for_function(
            "() => document.querySelector('[data-map-app]').dataset.targetDate === '2026-10-15'",
            timeout=10_000,
        )
        expect(root).to_have_attribute("data-selected-square", "cell-zero")
        expect(root).to_have_attribute("data-selection-source", "map")
        assert float(root.get_attribute("data-point-latitude") or "nan") == pytest.approx(
            44.131, abs=0.0003
        )
        assert requests
    finally:
        context.close()


@pytest.mark.parametrize("viewport", [(390, 844), (1440, 900)])
def test_location_selection_uses_exact_point_on_mobile_and_desktop(
    page: Page, base_url: str, viewport: tuple[int, int]
) -> None:
    page.add_init_script(
        """
        navigator.geolocation.getCurrentPosition = (success) => {
          success({coords: {latitude: 44.1307, longitude: 10.6407}});
        };
        """
    )
    requests, resolve_requests = _open_controlled_map(page, base_url, viewport=viewport)
    page.locator("[data-location]").click()
    root = page.locator("[data-map-app]")
    page.wait_for_function(
        "() => document.querySelector('[data-map-app]').dataset.selectedSquare === 'cell-zero'",
        timeout=10_000,
    )
    expect(root).to_have_attribute("data-target-date", "2026-10-12")
    expect(root).to_have_attribute("data-selection-source", "geolocation")
    assert float(root.get_attribute("data-point-latitude") or "nan") == pytest.approx(
        44.1307
    )
    assert float(root.get_attribute("data-point-longitude") or "nan") == pytest.approx(
        10.6407
    )
    assert resolve_requests[-1]["source"] == "geolocation"
    assert requests


def test_geolocation_state_stays_out_of_url_and_restores_after_language_change(
    page: Page, base_url: str
) -> None:
    page.add_init_script(
        """
        navigator.geolocation.getCurrentPosition = (success) => {
          success({coords: {latitude: 44.1307, longitude: 10.6407}});
        };
        """
    )
    _, resolve_requests = _open_controlled_map(page, base_url)
    page.locator("[data-location]").click()
    root = page.locator("[data-map-app]")
    page.wait_for_function(
        "() => document.querySelector('[data-map-app]').dataset.selectedSquare === 'cell-zero'",
        timeout=10_000,
    )
    query = parse_qs(urlparse(page.url).query)
    forbidden = {
        "square",
        "center",
        "zoom",
        "point_latitude",
        "point_longitude",
        "representative_latitude",
        "representative_longitude",
        "point_source",
    }
    assert forbidden.isdisjoint(query)
    assert page.evaluate("() => JSON.parse(sessionStorage.getItem('sporameteo.currentGeolocation')).source") == "geolocation"
    assert resolve_requests[-1]["source"] == "geolocation"
    assert not parse_qs(urlparse(str(resolve_requests[-1]["_url"])).query)

    page.locator("button[data-language='it']").click()
    page.wait_for_function(
        "() => document.querySelector('[data-map-app]').dataset.selectionSource === 'geolocation'",
        timeout=15_000,
    )
    expect(root).to_have_attribute("data-point-latitude", "44.1307")
    expect(root).to_have_attribute("data-point-longitude", "10.6407")

    search = page.locator("[data-place-search]")
    search.fill("Pistoia")
    expect(page.locator("[data-search-results] [role=option]")).to_have_count(1)
    search.press("ArrowDown")
    search.press("Enter")
    page.wait_for_function(
        "() => document.querySelector('[data-map-app]').dataset.selectionSource === 'search'",
        timeout=10_000,
    )
    assert page.evaluate("() => sessionStorage.getItem('sporameteo.currentGeolocation')") is None
