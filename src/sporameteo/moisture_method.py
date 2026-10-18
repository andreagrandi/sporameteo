"""Calculate the v4 experimental score from 30 preceding weather days.

The coefficients are provisional choices documented in the reassessment report.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import math


VERSION = "porcini-experimental-v4"
HISTORY_DAYS = 30


@dataclass(frozen=True)
class WeatherDay:
    """Keep dated daily inputs in explicit units.

Temperature must already refer to the square altitude.
"""

    day: date
    rain_mm: float | None
    temperature_c: float | None
    et0_mm: float | None


@dataclass(frozen=True)
class CandidateResult:
    """Return one score and the internal evidence needed to audit it.

The initial-moisture bounds are diagnostic values, not public outputs.
"""

    score: int | None
    unavailable_reason: str | None
    rain_totals_mm: tuple[float, ...] = ()
    temperature_factor: float | None = None
    moisture_factor: float | None = None
    retained_water_mm: float | None = None
    initial_moisture_score_bounds: tuple[int, int] | None = None
    version: str = VERSION


def temperature_response(*, temperature_c: float) -> float:
    """Use broad temperature bands from the owner's working document.

Interpolation and the outer endpoints are provisional choices.
"""
    knots = ((0.0, 0.0), (5.0, 0.1), (8.0, 0.4), (12.0, 1.0),
             (18.0, 1.0), (22.0, 0.7), (25.0, 0.3), (30.0, 0.0))
    if not math.isfinite(temperature_c):
        raise ValueError("Temperature must be finite.")
    if temperature_c <= knots[0][0] or temperature_c >= knots[-1][0]:
        return 0.0
    for (left, left_value), (right, right_value) in zip(knots, knots[1:]):
        if temperature_c <= right:
            return left_value + (right_value - left_value) * (temperature_c - left) / (right - left)
    raise AssertionError("Temperature interval was not found.")


def _moisture(
    *, days: tuple[WeatherDay, ...], initial_mm: float, maximum_drying_mm: float | None = None,
) -> tuple[float, float]:
    water = initial_mm
    factors = []
    for day in days:
        drying = 0.5 * day.et0_mm
        if maximum_drying_mm is not None:
            # Paulsen and Weiler 2026, HESS 30, 4305-4319, Table 5, https://doi.org/10.5194/hess-30-4305-2026:
            # the forest floor loses about 0.5 mm per rain-free day, so high et0 must not dry it faster.
            drying = min(drying, maximum_drying_mm)
        # The cap tests runoff sensitivity without pretending daily rain reveals storm intensity.
        water = min(80.0, max(0.0, 0.95 * water + min(day.rain_mm, 25.0) - drying))
        factors.append(min(1.0, water / 40.0))
    # Recent persistence allows a gradual response and avoids a fixed emergence date.
    return min(factors[-1], sum(factors[-7:]) / 7), water


def _score(*, moisture: float, temperature: float) -> int:
    value = Decimal(str(5.0 + 90.0 * moisture * temperature))
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_candidate(
    *, target_date: date, days: tuple[WeatherDay, ...], forest_share: float | None,
    maximum_drying_mm: float | None = None,
) -> CandidateResult:
    """Require 30 complete preceding dates and return an experimental score.

Missing weather remains unavailable and never becomes zero rainfall.
"""
    if forest_share is None:
        return CandidateResult(score=None, unavailable_reason="missing_forest")
    if isinstance(forest_share, bool) or not math.isfinite(forest_share) or not 0 <= forest_share <= 1:
        raise ValueError("Forest share must be a finite fraction.")
    if forest_share < 0.1:
        return CandidateResult(score=None, unavailable_reason="below_forest_threshold")
    expected = tuple(target_date - timedelta(days=offset) for offset in range(30, 0, -1))
    if tuple(day.day for day in days) != expected:
        raise ValueError("Exactly 30 consecutive dates before the target are required.")
    for day in days:
        for name in ("rain_mm", "temperature_c", "et0_mm"):
            value = getattr(day, name)
            if value is None:
                return CandidateResult(score=None, unavailable_reason=f"missing_{name}")
            if isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite.")
            if name != "temperature_c" and value < 0:
                raise ValueError(f"{name} cannot be negative.")
    temperature = sum(temperature_response(temperature_c=day.temperature_c) for day in days[-7:]) / 7
    moisture, water = _moisture(days=days, initial_mm=40.0, maximum_drying_mm=maximum_drying_mm)
    dry, _ = _moisture(days=days, initial_mm=0.0, maximum_drying_mm=maximum_drying_mm)
    wet, _ = _moisture(days=days, initial_mm=80.0, maximum_drying_mm=maximum_drying_mm)
    return CandidateResult(
        score=_score(moisture=moisture, temperature=temperature),
        unavailable_reason=None,
        rain_totals_mm=tuple(sum(day.rain_mm for day in days[-window:]) for window in (7, 14, 21, 30)),
        temperature_factor=temperature,
        moisture_factor=moisture,
        retained_water_mm=water,
        initial_moisture_score_bounds=(
            _score(moisture=dry, temperature=temperature),
            _score(moisture=wet, temperature=temperature),
        ),
    )
