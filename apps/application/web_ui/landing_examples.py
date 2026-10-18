"""Read the real example squares of the landing page from the published run.

The page only reads stored rows and cached bodies. It never starts a weather fetch and it
never calculates an estimate. The finished numbers are kept in the shared map cache, keyed
by the published run, so a new nightly run replaces them without any manual step.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from django.http import HttpRequest, JsonResponse, QueryDict
from django.utils import formats, timezone, translation
from django.utils.translation import gettext

from area_api.router import area_detail
from map_api import published
from map_api.published import GRID_TO_WGS84, MapReadError
from sporameteo.finds.grid import CELL_SIZE_M, parse_cell_id
from weather_on_demand import services as weather_services

from .formatting import format_date, format_decimal, format_short_date, format_weekday
from .translations import labels_for


logger = logging.getLogger(__name__)

HERO_SQUARE = "500m_E4372500_N2337000"
AREA_SQUARES = {
    "apennines": HERO_SQUARE,
    "borgotaro": "500m_E4305000_N2372000",
    "sila": "500m_E4880500_N1827500",
    "fiemme": "500m_E4453500_N2578500",
}
CACHE_KEY = "landing-examples-v1:{run_id}:{first}:{last}"
CACHE_SECONDS = 6 * 60 * 60
MAP_ZOOM = "13"
CENTER_MARGIN_DEGREES = 0.0003


def _centre(*, square_id: str) -> tuple[float, float]:
    """Return the latitude and longitude of the middle of one square."""
    address = parse_cell_id(square_id, expected_size_m=CELL_SIZE_M)
    half = CELL_SIZE_M / 2
    longitude, latitude = GRID_TO_WGS84.transform(address.corner_e + half, address.corner_n + half)
    return latitude, longitude


def _read_square(*, current: published.PublishedMap, square_id: str, target_date: date) -> dict[str, Any]:
    """Read one square for one date through the same cached read the map uses."""
    latitude, longitude = _centre(square_id=square_id)
    margin = CENTER_MARGIN_DEGREES
    try:
        body = published.read_squares(
            published=current,
            target_date=target_date,
            bbox=(longitude - margin, latitude - margin, longitude + margin, latitude + margin),
            max_squares=4,
        )
    except MapReadError:
        return {}
    for feature in json.loads(body.content)["features"]:
        if feature["id"] == square_id:
            return feature["properties"]
    return {}


def _percentage(*, properties: dict[str, Any]) -> int | None:
    """Return the stored whole percentage, or None when the square has no estimate."""
    if properties.get("state") != "available":
        return None
    return properties.get("percentage")


def _read_detail(*, square_id: str, target_date: date, update_id: str) -> dict[str, Any] | None:
    """Read the area details through the existing detail view and its cache."""
    request = HttpRequest()
    request.method = "GET"
    request.GET = QueryDict(urlencode({"target_date": target_date.isoformat(), "update_id": update_id}))
    try:
        response = area_detail(request, square_id=square_id)
    except Exception:
        # The landing page must still render when one detail cannot be built.
        logger.exception("The landing page could not read the details of %s", square_id)
        return None
    if not isinstance(response, JsonResponse) or response.status_code != 200:
        return None
    return json.loads(response.content)


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


def _collect(*, current: published.PublishedMap, first: date, last: date) -> dict[str, Any]:
    """Read the raw numbers of every example square for the first and last date."""
    squares: dict[str, Any] = {}
    for square_id in dict.fromkeys(AREA_SQUARES.values()):
        today_properties = _read_square(current=current, square_id=square_id, target_date=first)
        last_properties = (
            _read_square(current=current, square_id=square_id, target_date=last) if last != first else today_properties
        )
        latitude, longitude = _centre(square_id=square_id)
        squares[square_id] = {
            "first": _percentage(properties=today_properties),
            "last": _percentage(properties=last_properties),
            "latitude": latitude,
            "longitude": longitude,
            "update_id": today_properties.get("update_id"),
        }
    hero = squares[HERO_SQUARE]
    hero["facts"] = None
    if hero["first"] is not None and hero["update_id"]:
        detail = _read_detail(square_id=HERO_SQUARE, target_date=first, update_id=hero["update_id"])
        if detail is not None:
            hero["facts"] = _hero_facts(detail=detail)
    return squares


def _raw_examples(*, current: published.PublishedMap, first: date, last: date) -> dict[str, Any]:
    """Return the cached raw numbers of the run, reading them once per run."""
    key = CACHE_KEY.format(run_id=current.map_id, first=first.isoformat(), last=last.isoformat())
    cache = published.map_cache()
    stored = cache.get(key)
    if stored is not None:
        return stored
    squares = _collect(current=current, first=first, last=last)
    hero = squares[HERO_SQUARE]
    if hero["first"] is None or hero["facts"] is not None:
        cache.set(key, squares, timeout=CACHE_SECONDS)
    return squares


def _map_url(*, square_id: str, target_date: date, latitude: float, longitude: float) -> str:
    """Return the map address that opens one square on one date."""
    query = urlencode(
        {
            "square": square_id,
            "date": target_date.isoformat(),
            "center": f"{latitude:.4f},{longitude:.4f}",
            "zoom": MAP_ZOOM,
            "details": "true",
        }
    )
    return f"/map/?{query}"


def _percent(value: int | None) -> str:
    """Format a whole percentage, or return nothing when there is none."""
    return "" if value is None else f"{value}%"


def _decimal(value: float | None, *, language: str, places: int = 1) -> str:
    """Format a number with the decimal mark of the page language."""
    if value is None:
        return ""
    if places == 1:
        return format_decimal(value, language=language)
    with translation.override(language):
        return formats.number_format(Decimal(str(value)), decimal_pos=places, use_l10n=True)


def _long_weekday(value: date, *, language: str) -> str:
    """Return the full weekday name with a capital letter."""
    with translation.override(language):
        result = formats.date_format(value, "l", use_l10n=True)
    return result[:1].upper() + result[1:]


def _updated_text(*, calculated_at: str | None, language: str) -> str:
    """Return the update time in Europe/Rome, or nothing when it is unknown."""
    if not calculated_at:
        return ""
    moment = timezone.localtime(datetime.fromisoformat(calculated_at))
    with translation.override(language):
        time_text = formats.time_format(moment.time(), "H:i", use_l10n=True)
    return gettext("Updated {date} at {time}").format(
        date=format_date(moment.date(), language=language), time=time_text
    )


def _hero(*, raw: dict[str, Any], first: date, last: date, today: date, language: str) -> dict[str, Any]:
    """Build the hero card values the template shows."""
    square = raw[HERO_SQUARE]
    card: dict[str, Any] = {
        "map_url": _map_url(
            square_id=HERO_SQUARE, target_date=first, latitude=square["latitude"], longitude=square["longitude"]
        ),
        "has_estimate": square["first"] is not None,
        "reason": gettext("There is no estimate for this square on this date."),
    }
    if square["first"] is None:
        return card
    first_label = (
        gettext("Today, {date}").format(date=format_short_date(first, language=language))
        if first == today
        else gettext("{weekday}, {date}").format(
            weekday=_long_weekday(first, language=language), date=format_short_date(first, language=language)
        )
    )
    card.update(
        {
            "first_label": first_label,
            "first_percent": _percent(square["first"]),
            "first_width": square["first"],
        }
    )
    if last != first:
        card.update(
            {
                "has_last": True,
                "last_label": gettext("{weekday}, {date}").format(
                    weekday=_long_weekday(last, language=language),
                    date=format_short_date(last, language=language),
                ),
                "last_percent": _percent(square["last"]),
                "last_width": square["last"],
                "dates_text": gettext("{first} and {last}").format(
                    first=gettext("Today {date}").format(date=format_short_date(first, language=language))
                    if first == today
                    else f"{format_weekday(first, language=language)} {format_short_date(first, language=language)}",
                    last=f"{format_weekday(last, language=language)} {format_short_date(last, language=language)}",
                ),
            }
        )
    facts = square["facts"]
    if facts is None:
        return card
    mean = _decimal(facts["mean_temperature"], language=language)
    minimum = _decimal(facts["min_temperature"], language=language)
    if mean and minimum:
        temperature = gettext("{mean}°C, minimum {minimum}°C").format(mean=mean, minimum=minimum)
    elif mean:
        temperature = f"{mean}°C"
    else:
        temperature = ""
    rain = facts["rain"]
    card.update(
        {
            "has_facts": True,
            "coordinates": gettext("{latitude}° N • {longitude}° E").format(
                latitude=_decimal(facts["latitude"], language=language, places=4),
                longitude=_decimal(facts["longitude"], language=language, places=4),
            ),
            "rain_7": f"{_decimal(rain[7], language=language)} mm" if rain[7] is not None else "",
            "forest": (
                f"{round(facts['forest_share'] * 100)}%" if facts["forest_share"] is not None else ""
            ),
            "temperature": temperature,
            "updated": _updated_text(calculated_at=facts["calculated_at"], language=language),
            "rain_sentence": (
                gettext("At Abetone they are {seven}, {fourteen} and {twenty_one} mm.").format(
                    seven=_decimal(rain[7], language=language),
                    fourteen=_decimal(rain[14], language=language),
                    twenty_one=_decimal(rain[21], language=language),
                )
                if all(rain[days] is not None for days in (7, 14, 21))
                else ""
            ),
        }
    )
    return card


def landing_examples(*, language: str) -> dict[str, Any]:
    """Return the hero card, the four area cards and the older-result notice."""
    today = weather_services.italian_assessment_date()
    current = published.published_map()
    dates = current.available_dates(today=today) if current is not None else []
    if current is None or not dates:
        reason = gettext("No estimate has been published yet.")
        return {
            "hero": {"has_estimate": False, "reason": reason, "map_url": "/map/"},
            "areas": {
                key: {"has_estimate": False, "reason": reason, "map_url": "/map/"} for key in AREA_SQUARES
            },
            "older_notice": "",
        }
    first, last = dates[0], dates[-1]
    raw = _raw_examples(current=current, first=first, last=last)
    areas = {}
    for key, square_id in AREA_SQUARES.items():
        square = raw[square_id]
        areas[key] = {
            "has_estimate": square["first"] is not None,
            "percent": _percent(square["first"]),
            "date_text": format_short_date(first, language=language),
            "reason": gettext("There is no estimate for this square on this date."),
            "map_url": _map_url(
                square_id=square_id, target_date=first, latitude=square["latitude"], longitude=square["longitude"]
            ),
        }
    older_notice = (
        labels_for(language)["detail_old_result"].format(date=format_date(current.newest.assessment_date, language=language))
        if current.newest.assessment_date < today
        else ""
    )
    return {
        "hero": _hero(raw=raw, first=first, last=last, today=today, language=language),
        "areas": areas,
        "older_notice": older_notice,
    }
