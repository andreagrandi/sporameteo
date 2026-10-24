"""Read the real example squares of the landing page from the published run.

The page only reads stored rows and cached bodies. It never starts a weather fetch and it
never calculates an estimate. The finished numbers are kept in the shared map cache, keyed
by the published run, so a new nightly run replaces them without any manual step.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from django.utils import formats, timezone, translation
from django.utils.translation import gettext

from map_api import published
from map_api.published import MapReadError
from weather_on_demand import services as weather_services

from . import landing_top_areas
from .formatting import format_date, format_decimal, format_short_date, format_weekday
from .translations import labels_for


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


def _read_square(*, current: published.PublishedMap, square_id: str, target_date: date) -> dict[str, Any]:
    """Read one square for one date through the same cached read the map uses."""
    latitude, longitude = landing_top_areas._centre(square_id=square_id)
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



def _collect(*, current: published.PublishedMap, first: date, last: date) -> dict[str, Any]:
    """Read the raw numbers of every example square for the first and last date."""
    squares: dict[str, Any] = {}
    for square_id in dict.fromkeys(AREA_SQUARES.values()):
        today_properties = _read_square(current=current, square_id=square_id, target_date=first)
        last_properties = (
            _read_square(current=current, square_id=square_id, target_date=last) if last != first else today_properties
        )
        latitude, longitude = landing_top_areas._centre(square_id=square_id)
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
        detail = landing_top_areas._read_detail(
            square_id=HERO_SQUARE, target_date=first, update_id=hero["update_id"]
        )
        if detail is not None:
            hero["facts"] = landing_top_areas._hero_facts(detail=detail)
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


def _hero_names(*, title: str, name: str) -> dict[str, str]:
    """Return the hero heading and the text of its map link for one place."""
    return {"title": title, "link_text": gettext("Open {place} on the map").format(place=name)}


def _first_label(*, first: date, today: date, language: str) -> str:
    """Return the label of the first date, such as Today, 24 Oct or Sunday, 25 Oct."""
    if first == today:
        return gettext("Today, {date}").format(date=format_short_date(first, language=language))
    return gettext("{weekday}, {date}").format(
        weekday=_long_weekday(first, language=language), date=format_short_date(first, language=language)
    )


def _hero(*, square: dict[str, Any], first: date, last: date, today: date, language: str) -> dict[str, Any]:
    """Build the hero card values the template shows for one square entry."""
    card: dict[str, Any] = {
        **_hero_names(title=square["title"], name=square["name"]),
        "map_url": _map_url(
            square_id=square["square_id"],
            target_date=first,
            latitude=square["latitude"],
            longitude=square["longitude"],
        ),
        "has_estimate": square["first"] is not None,
        "reason": gettext("There is no estimate for this square on this date."),
    }
    if square["first"] is None:
        return card
    card.update(
        {
            "first_label": _first_label(first=first, today=today, language=language),
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
                gettext("For {place} they are {seven}, {fourteen} and {twenty_one} mm.").format(
                    place=square["name"],
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


def _fallback_hero_square(*, raw: dict[str, Any]) -> dict[str, Any]:
    """Return the fixed Apennines square, used when no top area is available."""
    return {**raw[HERO_SQUARE], "square_id": HERO_SQUARE, "name": "Abetone", "title": gettext("Abetone, Tuscany")}


def _top_rows(*, entries: list[dict[str, Any]], first: date, language: str) -> list[dict[str, Any]]:
    """Build the compact rows of the best areas list."""
    return [
        {
            "rank": rank,
            "title": entry["title"],
            "coordinates": gettext("{latitude}° N • {longitude}° E").format(
                latitude=_decimal(entry["latitude"], language=language, places=4),
                longitude=_decimal(entry["longitude"], language=language, places=4),
            ),
            "percent": _percent(entry["first"]),
            "map_url": _map_url(
                square_id=entry["square_id"],
                target_date=first,
                latitude=entry["latitude"],
                longitude=entry["longitude"],
            ),
        }
        for rank, entry in enumerate(entries, start=1)
    ]


def landing_examples(*, language: str) -> dict[str, Any]:
    """Return the hero card, the best areas, the four area cards and the older-result notice."""
    today = weather_services.italian_assessment_date()
    current = published.published_map()
    dates = current.available_dates(today=today) if current is not None else []
    if current is None or not dates:
        reason = gettext("No estimate has been published yet.")
        return {
            "hero": {
                **_hero_names(title=gettext("Abetone, Tuscany"), name="Abetone"),
                "has_estimate": False,
                "reason": reason,
                "map_url": "/map/",
            },
            "top_areas": None,
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
    entries = landing_top_areas.top_areas(current=current, first=first, last=last)
    hero_square = random.choice(entries) if entries else _fallback_hero_square(raw=raw)
    top_areas = None
    if entries:
        heading = gettext("Best areas today")
        if first != today:
            heading = gettext("Best areas, {day}").format(day=_first_label(first=first, today=today, language=language))
        top_areas = {
            "heading": heading,
            "rows": _top_rows(entries=entries, first=first, language=language),
        }
    return {
        "hero": _hero(square=hero_square, first=first, last=last, today=today, language=language),
        "top_areas": top_areas,
        "areas": areas,
        "older_notice": older_notice,
    }
