"""Browser checks for the complete visitor journey on a published Tuscany run.

The data comes from fixtures in the test database, which a test server reads.
Nothing is stubbed except the place search provider, which is a remote service.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from django.core.cache import caches
from playwright.sync_api import Browser, Locator, Page, expect

from weather_on_demand.services import italian_assessment_date

from tests.e2e.journey_data import JourneyData, build_journey_data
from tests.e2e.test_favorites import (
    _open_favorites,
    _read_storage,
    _save_selected,
)


# The browser plugin keeps an event loop running in the test thread. Django refuses
# database calls there unless this is set, and the test database setup makes such calls.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

DESKTOP = (1280, 800)
PHONE = (390, 844)
VIEWPORTS = {"desktop": DESKTOP, "phone": PHONE}
ABETONE = {
    "name": "Abetone",
    "context": "Abetone Cutigliano, Toscana",
    "latitude": 44.14,
    "longitude": 10.66,
}
ZOOMED_OUT_URL = "/map/?center=43.7,11.0&zoom=8"
DETAILS = "aside[data-live-details]"
MONTHS = {
    "en": [
        "January", "February", "March", "April", "May", "June", "July",
        "August", "September", "October", "November", "December",
    ],
    "it": [
        "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre",
    ],
}
SHORT_WEEKDAYS = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "it": ["lun", "mar", "mer", "gio", "ven", "sab", "dom"],
}
ACCEPT_LANGUAGE = {"it": "it-IT,it;q=0.9", "en": "en-GB,en;q=0.9"}
LOCALES = {"it": "it-IT", "en": "en-GB"}
GOOGLE_MAPS_PAGE = "<title>Google Maps stand-in</title>"
SQUARES_LAYER = "sporameteo-square-estimates"
SETTLE_SECONDS = 15


@dataclass
class Visitor:
    """One anonymous browser visitor with the console errors seen so far."""

    page: Page
    touch: bool
    errors: list[str] = field(default_factory=list)

    def press(self, locator: Locator) -> None:
        """Tap on a phone and click on a desktop."""
        if self.touch:
            locator.tap()
        else:
            locator.click()


@pytest.fixture
def journey_data(
    settings, tmp_path: Path, transactional_db: None
) -> JourneyData:
    """Store the published run, weather and details that the journey reads."""
    caches["map"].clear()
    data = build_journey_data(
        layer_path=tmp_path / "forest-layer.gz",
        assessment_date=italian_assessment_date(),
    )
    settings.SPORAMETEO_FOREST_BUILD_VERSION = data.forest_build_version
    settings.SPORAMETEO_TERRAIN_BUILD_VERSION = data.terrain_build_version
    # The test server runs with debug off, so the request limit would be on by default.
    settings.API_RATE_LIMIT_ENABLED = False
    return data


@pytest.fixture
def journey_url(journey_data: JourneyData, live_server) -> str:
    """Return the address of the test server, which reads the fixture rows."""
    return live_server.url


@pytest.fixture
def new_visitor(browser: Browser) -> Iterator[Callable[..., Visitor]]:
    """Build visitors in fresh browser contexts and close them afterwards."""
    contexts = []

    def build(*, language: str, device: str) -> Visitor:
        width, height = VIEWPORTS[device]
        touch = device == "phone"
        context = browser.new_context(
            viewport={"width": width, "height": height},
            locale=LOCALES[language],
            extra_http_headers={"Accept-Language": ACCEPT_LANGUAGE[language]},
            has_touch=touch,
            is_mobile=touch,
        )
        contexts.append(context)
        # The real Google Maps site is outside this check, so a stand-in answers.
        context.route(
            "https://www.google.com/maps/**",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=GOOGLE_MAPS_PAGE
            ),
        )
        page = context.new_page()
        visitor = Visitor(page=page, touch=touch)
        page.on(
            "console",
            lambda message: visitor.errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: visitor.errors.append(str(error)))
        return visitor

    yield build
    for context in contexts:
        context.close()


def _api_get(base_url: str, path: str) -> dict[str, object]:
    with urlopen(url=f"{base_url}/api/v1{path}", timeout=20) as response:
        return json.load(response)


def _stub_place_search(page: Page, *, results: list[dict[str, object]]) -> None:
    """Replace the place search, which asks a remote provider, with fixed answers."""
    page.route(
        "**/api/v1/places/search**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"results": results, "cached": False}),
        ),
    )


def _resolve_place(base_url: str) -> dict[str, object]:
    request = Request(
        url=f"{base_url}/api/v1/places/resolve",
        data=json.dumps(
            {"latitude": ABETONE["latitude"], "longitude": ABETONE["longitude"]}
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def _expected_estimate(
    base_url: str, *, cell_id: str, latitude: float, longitude: float, target_date: str
) -> dict[str, object]:
    """Read the stored answer for one square and date straight from the API."""
    bbox = f"{longitude - 0.03},{latitude - 0.03},{longitude + 0.03},{latitude + 0.03}"
    squares = _api_get(base_url, f"/squares?bbox={bbox}&target_date={target_date}")
    properties = next(
        feature["properties"]
        for feature in squares["features"]
        if feature["properties"]["cell_id"] == cell_id
    )
    area = _api_get(
        base_url,
        f"/areas/{cell_id}?target_date={target_date}&update_id={properties['update_id']}",
    )
    assert area["estimate"]["whole_percentage"] == properties["percentage"]
    assert area["selection"]["assessment_date"] == properties["assessment_date"]
    return {
        "percentage": properties["percentage"],
        "assessment_date": properties["assessment_date"],
        "update_id": properties["update_id"],
    }


def _long_date(iso: str, language: str) -> str:
    year, month, day = (int(part) for part in iso.split("-"))
    return f"{day} {MONTHS[language][month - 1]} {year}"


def _short_weekday(iso: str, language: str) -> str:
    return SHORT_WEEKDAYS[language][date.fromisoformat(iso).weekday()]


def _labels(page: Page) -> dict[str, str]:
    return json.loads(page.locator("#ui-labels").text_content())


def _open_real_map(page: Page, base_url: str, *, path: str) -> None:
    page.goto(url=f"{base_url}{path}", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.SporaMeteoMap?.getDiagnostics?.().squareSetCount >= 1",
        timeout=30_000,
    )
    _wait_for_idle_map(page)


def _wait_for_idle_map(page: Page) -> None:
    """Wait until the map has stopped moving and no fresh squares request is waiting."""
    page.wait_for_function(
        """
        () => {
          const exposed = window.SporaMeteoMap;
          const last = exposed?.lastRequest;
          return Boolean(
            last
            && last.generation === exposed.getDiagnostics().generation
            && !exposed.map.isMoving()
          );
        }
        """,
        timeout=SETTLE_SECONDS * 1000,
    )


def _rendered_squares(page: Page) -> dict[str, int]:
    """Wait until the square layer draws something, then count its features."""
    handle = page.wait_for_function(
        """
        (layer) => {
          const map = window.SporaMeteoMap.map;
          // A style switch removes the layer for a moment before the app adds it again.
          const found = map.getLayer(layer) ? map.queryRenderedFeatures({layers: [layer]}) : [];
          if (!found.length) return false;
          return {
            total: found.length,
            overview: found.filter((feature) => feature.properties?.overview).length,
          };
        }
        """,
        arg=SQUARES_LAYER,
        timeout=SETTLE_SECONDS * 1000,
    )
    return handle.json_value()


def _app_state(page: Page) -> dict[str, str]:
    return page.locator("[data-map-app]").evaluate("element => ({...element.dataset})")


def _selected_date(page: Page) -> str:
    return page.locator(".date-btn[aria-pressed='true']").get_attribute("data-date")


def _square_pixel(page: Page, *, latitude: float, longitude: float) -> tuple[float, float]:
    """Return the page position of a map coordinate."""
    box = page.locator("#map").bounding_box()
    assert box
    point = page.evaluate(
        """
        ([lon, lat]) => {
          const point = window.SporaMeteoMap.map.project([lon, lat]);
          return [point.x, point.y];
        }
        """,
        [longitude, latitude],
    )
    return box["x"] + point[0], box["y"] + point[1]


def _press_map(visitor: Visitor, *, latitude: float, longitude: float) -> None:
    x, y = _square_pixel(visitor.page, latitude=latitude, longitude=longitude)
    if visitor.touch:
        visitor.page.touchscreen.tap(x=x, y=y)
    else:
        visitor.page.mouse.click(x=x, y=y)


def _expect_details(
    page: Page, *, language: str, target_date: str, estimate: dict[str, object]
) -> None:
    """Check the details show the API answer for the date, in the page language."""
    labels = _labels(page)
    details = page.locator(DETAILS)
    expect(details.locator(".live-summary strong")).to_have_text(
        f"{estimate['percentage']}%", timeout=SETTLE_SECONDS * 1000
    )
    identity = details.locator(".detail-identity")
    expect(identity.nth(0)).to_have_text(
        f"{labels['map_target']}: {_long_date(target_date, language)}"
    )
    expect(identity.nth(1)).to_have_text(
        f"{labels['assessment_date_label']}: "
        f"{_long_date(str(estimate['assessment_date']), language)}"
    )


def _expect_date_strip(page: Page, *, language: str, dates: list[str], selected: str) -> None:
    labels = _labels(page)
    buttons = page.locator(".date-btn")
    expect(buttons).to_have_count(len(dates))
    for index, iso in enumerate(dates):
        button = buttons.nth(index)
        expect(button).to_have_attribute("data-date", iso)
        expect(button).to_have_attribute("aria-pressed", "true" if iso == selected else "false")
        weekday = labels["today"] if index == 0 else _short_weekday(iso, language)
        expect(button.locator(".d-wd")).to_have_text(weekday)
        expect(button.locator(".d-dm")).to_contain_text(str(int(iso[-2:])))


def _choose_date(visitor: Visitor, iso: str) -> None:
    visitor.press(visitor.page.locator(f".date-btn[data-date='{iso}']"))
    expect(visitor.page.locator("[data-map-app]")).to_have_attribute("data-target-date", iso)
    _wait_for_idle_map(visitor.page)


def _switch_language(visitor: Visitor, *, language: str) -> None:
    """Press the language button, which reloads the page with state from the URL."""
    with visitor.page.expect_navigation(wait_until="domcontentloaded"):
        visitor.press(visitor.page.locator(f"button[data-language='{language}']"))
    visitor.page.wait_for_function(
        "() => window.SporaMeteoMap?.getDiagnostics?.().squareSetCount >= 1",
        timeout=30_000,
    )
    _wait_for_idle_map(visitor.page)
    expect(visitor.page.locator("html")).to_have_attribute("lang", language)


def _open_layers_panel(visitor: Visitor) -> None:
    if not visitor.page.locator("input[name='background'][value='contrast']").is_visible():
        # The desktop box starts folded. The phone opens it as a sheet.
        fold = visitor.page.locator("[data-fold='layers']")
        visitor.press(fold if fold.is_visible() else visitor.page.locator("[data-open-panel='layers']"))
    expect(visitor.page.locator("input[name='background'][value='contrast']")).to_be_visible()


def _decimal_mark(language: str) -> str:
    return "," if language == "it" else "."


def _rain_amount(page: Page, *, language: str) -> float:
    text = page.locator(f"{DETAILS} .detail-rain-value strong").inner_text()
    match = re.fullmatch(r"(\d+)" + re.escape(_decimal_mark(language)) + r"(\d+)\s*mm", text)
    assert match, f"Rain amount {text!r} does not use the {language} decimal mark"
    return float(f"{match.group(1)}.{match.group(2)}")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("device", ["desktop", "phone"])
@pytest.mark.parametrize("language", ["it", "en"])
def test_real_data_journey_from_search_to_favorite_and_language_switch(
    new_visitor: Callable[..., Visitor], journey_url: str, language: str, device: str
) -> None:
    """Walk the whole visitor journey on the published Tuscany run, with no stubs.

    Only the place search is replaced, because its provider is a remote service.
    """
    visitor = new_visitor(language=language, device=device)
    page = visitor.page
    info = _api_get(journey_url, "/info")
    dates: list[str] = info["target_dates"]
    first_date, last_date = dates[0], dates[-1]
    place = _resolve_place(journey_url)
    cell_id = str(place["square_id"])
    assert place["status"] == "available"
    _stub_place_search(page, results=[ABETONE])

    # Zoomed out: area averages only, no single percentage and no details.
    _open_real_map(page, journey_url, path=f"{ZOOMED_OUT_URL}&date={first_date}")
    squares = _rendered_squares(page)
    assert squares["total"] > 0
    assert squares["overview"] == squares["total"]
    expect(page.locator(DETAILS)).to_have_count(0)
    expect(page.locator("#mapmsgs")).to_contain_text(_labels(page)["map_overview"])
    expect(page.locator(".date-btn .d-pct:not(:empty)")).to_have_count(0)
    _expect_date_strip(page, language=language, dates=dates, selected=first_date)

    # Search a Tuscan place and pick it. The map moves there and single squares appear.
    search = page.locator("[data-place-search]")
    search.fill("Abetone")
    option = page.locator("[data-search-results] [role=option]")
    expect(option).to_have_count(1)
    expect(option).to_contain_text(str(ABETONE["context"]))
    visitor.press(option)
    expect(page.locator("[data-map-app]")).to_have_attribute("data-selected-square", cell_id)
    page.wait_for_function(
        """
        (layer) => {
          const found = window.SporaMeteoMap.map.queryRenderedFeatures({layers: [layer]});
          return found.length > 0 && found.every((feature) => !feature.properties?.overview);
        }
        """,
        arg=SQUARES_LAYER,
        timeout=SETTLE_SECONDS * 1000,
    )
    _wait_for_idle_map(page)

    # Close the details, then select the area by pressing it on the map.
    visitor.press(page.locator(f"{DETAILS} .live-details-close"))
    expect(page.locator(DETAILS)).to_have_count(0)
    _press_map(visitor, latitude=float(ABETONE["latitude"]), longitude=float(ABETONE["longitude"]))
    expect(page.locator(DETAILS)).to_be_visible()
    state = _app_state(page)
    assert state["selectedSquare"] == cell_id
    assert state["selectionSource"] == "map"

    # Today: percentage, date and facts match the API answer for this square.
    today_estimate = _expected_estimate(
        journey_url,
        cell_id=cell_id,
        latitude=float(ABETONE["latitude"]),
        longitude=float(ABETONE["longitude"]),
        target_date=first_date,
    )
    _expect_details(page, language=language, target_date=first_date, estimate=today_estimate)
    expect(page.locator(f"{DETAILS} .detail-key-facts .fact-row")).not_to_have_count(0)
    facts = page.locator(f"{DETAILS} .detail-key-facts .fact-value").all_inner_texts()
    mark = re.escape(_decimal_mark(language))
    assert any(re.search(r"\d" + mark + r"\d", fact) for fact in facts)
    other = "." if language == "it" else ","
    assert not any(re.search(r"\d" + re.escape(other) + r"\d", fact) for fact in facts)

    # The navigation link uses the pressed coordinates.
    expected_link = page.evaluate(
        """
        (state) => window.SporaMeteoNavigation.buildGoogleMapsUrl({
          latitude: Number(state.pointLatitude), longitude: Number(state.pointLongitude),
        })
        """,
        _app_state(page),
    )
    assert expected_link
    navigation = page.locator(f"{DETAILS} [data-navigation]")
    expect(navigation).to_have_attribute("data-navigation-url", expected_link)
    with page.expect_popup() as popup_info:
        visitor.press(navigation)
    assert popup_info.value.url == expected_link
    popup_info.value.close()

    # Day seven: the percentage and date follow the chosen date, then come back.
    _choose_date(visitor, last_date)
    last_estimate = _expected_estimate(
        journey_url,
        cell_id=cell_id,
        latitude=float(ABETONE["latitude"]),
        longitude=float(ABETONE["longitude"]),
        target_date=last_date,
    )
    _expect_details(page, language=language, target_date=last_date, estimate=last_estimate)
    _expect_date_strip(page, language=language, dates=dates, selected=last_date)
    _choose_date(visitor, first_date)
    _expect_details(page, language=language, target_date=first_date, estimate=today_estimate)
    _expect_date_strip(page, language=language, dates=dates, selected=first_date)
    _choose_date(visitor, last_date)

    # Switch language with area, date and details open. Everything stays selected.
    other_language = "en" if language == "it" else "it"
    _switch_language(visitor, language=other_language)
    language = other_language
    # After the reload the details reopen once the square's data arrives, so wait for it.
    root = page.locator("[data-map-app]")
    expect(root).to_have_attribute("data-details-open", "true", timeout=SETTLE_SECONDS * 1000)
    state = _app_state(page)
    assert state["selectedSquare"] == cell_id
    assert state["targetDate"] == last_date
    _expect_details(page, language=language, target_date=last_date, estimate=last_estimate)
    _expect_date_strip(page, language=language, dates=dates, selected=last_date)
    _choose_date(visitor, first_date)

    # The short phone panel hides the rain periods, so make it taller first.
    if visitor.touch:
        visitor.press(page.locator(f"{DETAILS} [data-sheet-height]"))
        expect(page.locator(DETAILS)).to_have_attribute("data-size", "half")

    # Recent rain periods: the totals grow with the period and use the language's decimals.
    periods = {}
    for days in (7, 14, 21):
        visitor.press(page.locator(f"{DETAILS} [data-rain-days='{days}']"))
        expect(page.locator(f"{DETAILS} [data-rain-days='{days}']")).to_have_attribute(
            "aria-pressed", "true"
        )
        periods[days] = _rain_amount(page, language=language)
    # Equal totals are fine when all the rain fell in the last week.
    assert periods[7] <= periods[14] <= periods[21]

    # Sources open and list where the numbers come from.
    sources = page.locator(f"{DETAILS} [data-detail-disclosure='sources']")
    visitor.press(sources.locator("summary").first)
    expect(sources).to_have_attribute("open", "")
    expect(sources.locator(".source-card").first).to_be_visible()
    expect(sources.locator(".source-card[data-source-key='weather']")).to_be_visible()

    # Background and opacity.
    _open_layers_panel(visitor)
    visitor.press(page.locator("input[name='background'][value='contrast']"))
    expect(page.locator("input[name='background'][value='contrast']")).to_be_checked()
    page.wait_for_function("() => window.SporaMeteoMap.getDiagnostics().background === 'contrast'")
    page.locator("[data-opacity]").fill("85")
    expect(page.locator("[data-opacity-value]")).to_have_text("85%")
    page.wait_for_function("() => window.SporaMeteoMap.getDiagnostics().opacity === 85")
    assert "background=contrast" in page.url
    assert "opacity=85" in page.url

    # On a phone the open Layers panel covers the taller details panel.
    if visitor.touch:
        page.keyboard.press("Escape")
        expect(page.locator("#layers-panel")).to_be_hidden()

    # Save the area as a favorite and rename it.
    page.evaluate("key => window.localStorage.removeItem(key)", "sporameteo.favorites")
    _save_selected(page, name="Prato dell'Orso")
    stored = _read_storage(page)
    assert [item["name"] for item in stored["items"]] == ["Prato dell'Orso"]
    _open_favorites(page)
    visitor.press(page.locator("[data-favorite-edit]"))
    form = page.locator("form.favorite-form")
    form.locator("[data-favorite-name]").fill("Faggeta di Abetone")
    visitor.press(form.locator("button[type='submit']"))
    expect(page.locator("[data-favorite-id] h3")).to_have_text("Faggeta di Abetone")
    assert _read_storage(page)["items"][0]["name"] == "Faggeta di Abetone"

    # Reload, reopen the favorite and check it shows the current result.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "() => window.SporaMeteoMap?.getDiagnostics?.().squareSetCount >= 1", timeout=30_000
    )
    _open_favorites(page)
    card = page.locator("[data-favorite-id]").filter(has_text="Faggeta di Abetone")
    expect(card).to_have_count(1)
    visitor.press(card.locator("[data-favorite-open]"))
    selected = _selected_date(page)
    current = _expected_estimate(
        journey_url,
        cell_id=cell_id,
        latitude=float(ABETONE["latitude"]),
        longitude=float(ABETONE["longitude"]),
        target_date=selected,
    )
    _expect_details(page, language=language, target_date=selected, estimate=current)
    expect(page.locator(f"{DETAILS} [data-navigation]")).to_have_attribute(
        "data-navigation-url", re.compile(r"^https://www\.google\.com/maps/search/\?api=1&query=")
    )

    assert visitor.errors == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("language", ["it", "en"])
def test_real_data_view_outside_italy_explains_the_gap(
    new_visitor: Callable[..., Visitor], journey_url: str, language: str
) -> None:
    """Show that a view outside every published region says why it has no estimates."""
    visitor = new_visitor(language=language, device="desktop")
    page = visitor.page
    page.goto(url=f"{journey_url}/map/?center=48.85,2.35&zoom=12", wait_until="domcontentloaded")
    expect(page.locator("#mapmsgs")).to_contain_text(_labels(page)["map_outside_coverage"])
    assert page.evaluate("() => window.SporaMeteoMap.getDiagnostics().squareSetCount") == 0
    expect(page.locator(DETAILS)).to_have_count(0)
    assert visitor.errors == []
