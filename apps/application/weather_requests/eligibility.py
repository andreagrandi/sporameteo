"""Find the weather cells that a completed national forest build permits."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

from django.db.models import Q


CENTRE_QUANTUM = Decimal("0.01")
TUSCANY_ISTAT_CODE = "09"
ALL_REGIONS = "all"


class WeatherEligibilityError(ValueError):
    """Report an invalid or incomplete forest input."""


@dataclass(frozen=True, order=True)
class WeatherCell:
    """Identify one canonical 0.1 degree cell by its centre."""

    key: str
    latitude: float
    longitude: float

    @property
    def weather_cell_key(self) -> str:
        """Return the stable cell key used by requests and later summaries."""
        return self.key


def _centre_coordinate(value: float | Decimal | str) -> Decimal:
    try:
        coordinate = Decimal(str(value)).quantize(CENTRE_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise WeatherEligibilityError(f"Invalid weather coordinate: {value!r}") from exc
    if coordinate == Decimal("-0.00"):
        coordinate = Decimal("0.00")
    return coordinate


def weather_cell_key(*, latitude: float | Decimal | str, longitude: float | Decimal | str) -> str:
    """Return a key made from the two-decimal weather cell centre."""
    latitude_decimal = _centre_coordinate(latitude)
    longitude_decimal = _centre_coordinate(longitude)
    return f"{latitude_decimal:.2f},{longitude_decimal:.2f}"


canonical_cell_key = weather_cell_key


def weather_cell(*, latitude: float | Decimal | str, longitude: float | Decimal | str) -> WeatherCell:
    """Return the typed weather cell for two canonical centre coordinates."""
    latitude_decimal = _centre_coordinate(latitude)
    longitude_decimal = _centre_coordinate(longitude)
    return WeatherCell(
        key=weather_cell_key(latitude=latitude_decimal, longitude=longitude_decimal),
        latitude=float(latitude_decimal),
        longitude=float(longitude_decimal),
    )


weather_cell_for_coordinates = weather_cell


def _cells_from_rows(*, rows: Iterable[object]) -> tuple[WeatherCell, ...]:
    result: dict[str, WeatherCell] = {}
    for row in rows:
        is_forest = getattr(row, "is_forest", None)
        latitude = getattr(row, "weather_latitude", None)
        longitude = getattr(row, "weather_longitude", None)
        if is_forest is not True:
            raise WeatherEligibilityError("Only forest squares may receive weather.")
        if latitude is None or longitude is None:
            raise WeatherEligibilityError("A forest square is missing its weather cell centre.")
        cell = weather_cell(latitude=latitude, longitude=longitude)
        result[cell.key] = cell
    return tuple(sorted(result.values(), key=lambda value: value.key))


def _cells_from_coordinates(
    *,
    rows: Iterable[tuple[float | Decimal, float | Decimal]],
) -> tuple[WeatherCell, ...]:
    """Build unique cells from the distinct coordinate result."""
    result: dict[str, WeatherCell] = {}
    for latitude, longitude in rows:
        cell = weather_cell(latitude=latitude, longitude=longitude)
        result[cell.key] = cell
    return tuple(sorted(result.values(), key=lambda value: value.key))


def weather_cells_for_forest_build(*, build_version: str) -> tuple[WeatherCell, ...]:
    """Return unique weather cells from one completed forest build."""
    from national_forest.models import ForestBuild, ForestCell

    build = ForestBuild.objects.filter(version=build_version).first()
    if build is None:
        raise WeatherEligibilityError(f"No forest build exists for {build_version!r}.")
    if build.status != ForestBuild.Status.COMPLETED:
        raise WeatherEligibilityError(
            f"Forest build {build_version!r} is {build.status}, not completed."
        )
    forest_cells = ForestCell.objects.filter(build=build, is_forest=True)
    if forest_cells.filter(
        Q(weather_latitude__isnull=True) | Q(weather_longitude__isnull=True)
    ).exists():
        raise WeatherEligibilityError("A forest square is missing its weather cell centre.")
    rows = forest_cells.values_list("weather_latitude", "weather_longitude").distinct().iterator(
        chunk_size=10_000
    )
    return _cells_from_coordinates(rows=rows)


def nightly_run_key(*, value: str) -> str:
    """Return the run key of a region setting: all, or the sorted codes joined by commas."""
    text = value.strip().lower()
    if text == ALL_REGIONS:
        return ALL_REGIONS
    codes = sorted({code.strip() for code in value.split(",") if code.strip()})
    if not codes:
        raise WeatherEligibilityError("Name at least one region code, or all.")
    return ",".join(codes)


def nightly_region_codes(*, value: str, forest_build_version: str) -> tuple[str, ...]:
    """Return the sorted region codes of a setting value or run key, and reject unknown codes."""
    from national_forest.models import ForestBuild
    from national_grid.models import GridRegion

    key = nightly_run_key(value=value)
    build = ForestBuild.objects.select_related("grid_build").filter(version=forest_build_version).first()
    if build is None:
        raise WeatherEligibilityError(f"No forest build exists for {forest_build_version!r}.")
    known = set(GridRegion.objects.filter(build=build.grid_build).values_list("istat_code", flat=True))
    if key == ALL_REGIONS:
        return tuple(sorted(known))
    codes = tuple(key.split(","))
    unknown = [code for code in codes if code not in known]
    if unknown:
        raise WeatherEligibilityError(
            f"Unknown region codes {unknown} for forest build {forest_build_version!r}. "
            f"Known codes are {sorted(known)}."
        )
    return codes


def weather_cells_for_regions(
    *, forest_build_version: str, region_codes: Iterable[str]
) -> tuple[WeatherCell, ...]:
    """Return the unique weather cells of the forest squares of these regions, each counted once."""
    from national_forest.models import ForestBuild, ForestCell

    codes = sorted(set(region_codes))
    if not codes:
        raise WeatherEligibilityError("Name at least one region code.")
    build = ForestBuild.objects.filter(version=forest_build_version).first()
    if build is None:
        raise WeatherEligibilityError(f"No forest build exists for {forest_build_version!r}.")
    if build.status != ForestBuild.Status.COMPLETED:
        raise WeatherEligibilityError(
            f"Forest build {forest_build_version!r} is {build.status}, not completed."
        )
    forest_cells = ForestCell.objects.filter(
        build=build,
        is_forest=True,
        grid_build_cell__owner_region__istat_code__in=codes,
    )
    if forest_cells.filter(
        Q(weather_latitude__isnull=True) | Q(weather_longitude__isnull=True)
    ).exists():
        raise WeatherEligibilityError("A forest square is missing its weather cell centre.")
    rows = forest_cells.values_list("weather_latitude", "weather_longitude").distinct().iterator(
        chunk_size=10_000
    )
    return _cells_from_coordinates(rows=rows)


def weather_cells_for_run_key(*, forest_build_version: str, run_key: str) -> tuple[WeatherCell, ...]:
    """Return the unique weather cells of the regions that a run key names."""
    return weather_cells_for_regions(
        forest_build_version=forest_build_version,
        region_codes=nightly_region_codes(value=run_key, forest_build_version=forest_build_version),
    )


def eligible_weather_cells(*, forest_build_version: str) -> tuple[WeatherCell, ...]:
    """Return weather cells eligible for provider requests."""
    return weather_cells_for_forest_build(build_version=forest_build_version)


def weather_cells_for_forest_cells(*, cells: Iterable[object]) -> tuple[WeatherCell, ...]:
    """Return unique weather cells after validating forest square rows."""
    return _cells_from_rows(rows=cells)
