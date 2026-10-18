"""Check v4 rule behavior without treating it as biological validation.

Earlier method versions remain available for saved results.
"""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from tools.forecast_checks.moisture_candidate import CandidateResult, WeatherDay, calculate_candidate
from sporameteo.forecast_method import CURRENT_METHOD, METHOD_V4, METHOD_V5


TARGET = date(2026, 10, 12)


def weather(*, rain: tuple[float, ...], temperature: float = 15, et0: float = 2) -> tuple[WeatherDay, ...]:
    return tuple(WeatherDay(day=TARGET - timedelta(days=30 - index), rain_mm=amount,
                            temperature_c=temperature, et0_mm=et0)
                 for index, amount in enumerate(rain))


def score(*, days: tuple[WeatherDay, ...]) -> CandidateResult:
    return calculate_candidate(target_date=TARGET, days=days, forest_share=0.624)


def test_recent_wet_spell_survives_dry_older_window() -> None:
    dry = score(days=weather(rain=(0,) * 30))
    wet = score(days=weather(rain=(0,) * 23 + (15,) * 5 + (0,) * 2))
    assert dry.score == 5
    assert wet.score > dry.score
    assert wet.moisture_factor > 0.5


def test_distributed_rain_beats_single_storm_under_runoff_assumption() -> None:
    distributed = score(days=weather(rain=(0,) * 23 + (15,) * 5 + (0,) * 2))
    storm = score(days=weather(rain=(0,) * 23 + (75,) + (0,) * 6))
    assert distributed.rain_totals_mm == storm.rain_totals_mm
    assert distributed.score > storm.score


def test_moisture_persists_after_rain_and_drying_reduces_it() -> None:
    rain = (0,) * 16 + (15,) * 7 + (0,) * 7
    mild = score(days=weather(rain=rain, et0=1))
    drying = score(days=weather(rain=rain, et0=8))
    assert mild.rain_totals_mm[0] == 0
    assert mild.score > 5
    assert drying.score < mild.score


def test_temperature_bands_allow_cool_and_warm_fruiting() -> None:
    rain = (5,) * 30
    values = [score(days=weather(rain=rain, temperature=temp)).score for temp in (5, 10, 15, 20, 28)]
    assert values[0] < values[1] < values[2]
    assert values[2] > values[3] > values[4]
    assert values[1] > 5
    assert values[3] > 5


@pytest.mark.parametrize("field", ["rain_mm", "temperature_c", "et0_mm"])
def test_missing_weather_is_unavailable(field: str) -> None:
    days = list(weather(rain=(5,) * 30))
    days[10] = replace(days[10], **{field: None})
    result = score(days=tuple(days))
    assert result.score is None
    assert result.unavailable_reason == f"missing_{field}"


@pytest.mark.parametrize("change", ["short", "duplicate", "target_day", "reversed"])
def test_incomplete_or_misdated_history_is_rejected(change: str) -> None:
    days = weather(rain=(5,) * 30)
    if change == "short":
        days = days[1:]
    elif change == "duplicate":
        days = days[:-1] + (days[-2],)
    elif change == "target_day":
        days = days[:-1] + (replace(days[-1], day=TARGET),)
    else:
        days = tuple(reversed(days))
    with pytest.raises(ValueError, match="30 consecutive"):
        score(days=days)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_invalid_rain_is_rejected(value: float) -> None:
    days = weather(rain=(5,) * 30)
    with pytest.raises(ValueError):
        score(days=days[:-1] + (replace(days[-1], rain_mm=value),))


def test_initial_moisture_uncertainty_is_reported_internally() -> None:
    result = score(days=weather(rain=(0,) * 30, et0=0))
    low, high = result.initial_moisture_score_bounds
    assert low < result.score < high


def test_forest_eligibility_and_current_method_are_preserved() -> None:
    days = weather(rain=(5,) * 30)
    result = calculate_candidate(target_date=TARGET, days=days, forest_share=0.09)
    assert result.score is None
    assert result.unavailable_reason == "below_forest_threshold"
    assert calculate_candidate(target_date=TARGET, days=days, forest_share=0.1).score is not None
    assert CURRENT_METHOD == METHOD_V5


def score_v5(*, days: tuple[WeatherDay, ...]) -> CandidateResult:
    return calculate_candidate(
        target_date=TARGET, days=days, forest_share=0.624, maximum_drying_mm=METHOD_V5.maximum_drying_mm,
    )


def test_v5_keeps_more_water_than_v4_when_et0_is_high() -> None:
    days = weather(rain=(0,) * 16 + (15,) * 7 + (0,) * 7, temperature=15, et0=6)
    v4 = score(days=days)
    v5 = score_v5(days=days)
    assert v5.retained_water_mm > v4.retained_water_mm
    assert v5.score > v4.score


def test_v5_equals_v4_when_the_drying_cap_never_binds() -> None:
    days = weather(rain=(0,) * 16 + (15,) * 7 + (0,) * 7, et0=1)
    assert score_v5(days=days) == score(days=days)


@pytest.mark.parametrize("et0", [0, 0.5, 1, 3, 8])
@pytest.mark.parametrize("rain", [(0,) * 30, (5,) * 30, (0,) * 23 + (15,) * 5 + (0,) * 2])
def test_v5_never_scores_below_v4_on_the_same_weather(et0: float, rain: tuple[float, ...]) -> None:
    days = weather(rain=rain, et0=et0)
    assert score_v5(days=days).score >= score(days=days).score
