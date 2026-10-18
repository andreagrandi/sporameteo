"""Focused checks for the experimental porcini forecast method."""

from dataclasses import replace
from datetime import date, timedelta
import math

import pytest

from sporameteo.forecast_method import (
    EVENT_KEY,
    METHOD_V1,
    METHOD_V2,
    METHOD_V3,
    METHOD_V4,
    METHOD_V5,
    HostTrees,
    DailyTemperatures,
    ForecastInputs,
    NumericInput,
    calculate_forecast as calculate_versioned_forecast,
)
from sporameteo.moisture_method import WeatherDay


ASSESSMENT = date(2026, 6, 15)


def test_current_method_uses_recent_rain_without_the_v3_older_rain_veto() -> None:
    days = tuple(
        WeatherDay(day=ASSESSMENT - timedelta(days=offset),
                   rain_mm=10.0 if offset <= 7 else 0.0,
                   temperature_c=16.25, et0_mm=2.0)
        for offset in range(30, 0, -1)
    )
    inputs = replace(make_inputs(old_rain=0.0, recent_rain=70.0),
                     daily_weather=days, daily_weather_source_id="test:30-days")
    current = calculate_versioned_forecast(inputs=inputs)
    legacy = calculate_versioned_forecast(inputs=inputs, method=METHOD_V3)
    assert current.method_version == METHOD_V5.version
    assert current.whole_percentage > 5
    assert legacy.whole_percentage == 5
    assert current.experimental_unvalidated is True
    rain = next(fact for fact in current.ordered_facts if fact.role_key == "rain_30_days")
    assert rain.original_value == 70.0
    assert rain.completed_day_count == 30
    assert rain.forecast_day_count == 0


def test_current_method_requires_et0_instead_of_assuming_no_drying() -> None:
    days = tuple(
        WeatherDay(day=ASSESSMENT - timedelta(days=offset), rain_mm=5.0,
                   temperature_c=16.0, et0_mm=None if offset == 10 else 2.0)
        for offset in range(30, 0, -1)
    )
    result = calculate_versioned_forecast(inputs=replace(
        make_inputs(), daily_weather=days, daily_weather_source_id="test:30-days",
    ))
    assert result.whole_percentage is None
    assert result.unavailable_reason == "missing_et0_mm"


def calculate_forecast(*, inputs: ForecastInputs, method=METHOD_V3):
    """Keep legacy regression examples on their original method version."""
    return calculate_versioned_forecast(inputs=inputs, method=method)


def make_input(
    *,
    role: str,
    value: float | None,
    assessment: date = ASSESSMENT,
    target: date | None = None,
    unit: str | None = None,
    source_id: str | None = None,
    semantic: str | None = None,
) -> NumericInput:
    target_date = target or assessment
    days_ahead = (target_date - assessment).days
    if role == "old_rain":
        period_start = target_date - timedelta(days=21)
        period_end = target_date - timedelta(days=8)
        completed = 14
        forecast = 0
        default_unit = "mm"
    elif role == "recent_rain":
        period_start = target_date - timedelta(days=7)
        period_end = target_date - timedelta(days=1)
        completed = 7 - days_ahead
        forecast = days_ahead
        default_unit = "mm"
    elif role == "target_temperature":
        period_start = target_date
        period_end = target_date
        completed = 0
        forecast = 1
        default_unit = "deg_c"
    else:
        period_start = assessment
        period_end = assessment
        completed = 1
        forecast = 0
        default_unit = "m" if role in {"reference_altitude", "square_altitude"} else "fraction"
    if role == "target_temperature" and semantic is None:
        semantic = "temperature_at_reference_altitude"
    return NumericInput(
        value=value,
        unit=unit or default_unit,
        source_id=source_id or f"test:{role}",
        period_start=period_start,
        period_end=period_end,
        completed_day_count=completed,
        forecast_day_count=forecast,
        semantic=semantic,
    )


def make_period_temperatures(
    *,
    assessment: date = ASSESSMENT,
    days_ahead: int = 0,
    daily_means: tuple[float | None, ...] | None = None,
    daily_minimums: tuple[float | None, ...] | None = None,
    mean: float = 16.95,
    minimum: float = 8.0,
    semantic: str = "temperature_at_reference_altitude",
    period_start: date | None = None,
) -> DailyTemperatures:
    target = assessment + timedelta(days=days_ahead)
    return DailyTemperatures(
        daily_means=daily_means if daily_means is not None else (mean,) * 20,
        daily_minimums=daily_minimums if daily_minimums is not None else (minimum,) * 20,
        unit="deg_c",
        source_id="test:period_temperature",
        period_start=period_start or target - timedelta(days=19),
        period_end=target,
        completed_day_count=19 - days_ahead,
        forecast_day_count=days_ahead + 1,
        semantic=semantic,
    )


def make_inputs(
    *,
    assessment: date = ASSESSMENT,
    days_ahead: int = 0,
    old_rain: float | None = 60.0,
    recent_rain: float | None = 30.0,
    target_temperature: float | None = 16.25,
    reference_altitude: float | None = 500.0,
    square_altitude: float | None = 1000.0,
    forest_share: float | None = 1.0,
    period_temperatures: DailyTemperatures | None | str = "default",
) -> ForecastInputs:
    target = assessment + timedelta(days=days_ahead)
    if period_temperatures == "default":
        period_temperatures = make_period_temperatures(assessment=assessment, days_ahead=days_ahead)
    return ForecastInputs(
        period_temperatures=period_temperatures,
        assessment_date=assessment,
        target_date=target,
        old_rain=None
        if old_rain is None
        else make_input(role="old_rain", value=old_rain, assessment=assessment, target=target),
        recent_rain=None
        if recent_rain is None
        else make_input(role="recent_rain", value=recent_rain, assessment=assessment, target=target),
        target_temperature=None
        if target_temperature is None
        else make_input(
            role="target_temperature",
            value=target_temperature,
            assessment=assessment,
            target=target,
        ),
        reference_altitude=None
        if reference_altitude is None
        else make_input(
            role="reference_altitude",
            value=reference_altitude,
            assessment=assessment,
            target=target,
        ),
        square_altitude=None
        if square_altitude is None
        else make_input(
            role="square_altitude",
            value=square_altitude,
            assessment=assessment,
            target=target,
        ),
        forest_share=None
        if forest_share is None
        else make_input(
            role="forest_share",
            value=forest_share,
            assessment=assessment,
            target=target,
        ),
    )


def test_fixed_examples_return_documented_percentages_and_facts() -> None:
    ideal = calculate_forecast(method=METHOD_V1, inputs=make_inputs())
    dry = calculate_forecast(method=METHOD_V1, inputs=make_inputs(old_rain=0.0))
    low_forest = calculate_forecast(method=METHOD_V1, inputs=make_inputs(forest_share=0.1))

    assert ideal.whole_percentage == 95
    assert dry.whole_percentage == 5
    assert low_forest.whole_percentage == 73
    assert ideal.unavailable_reason is None
    assert ideal.experimental_unvalidated is True
    assert ideal.method_version == METHOD_V1.version
    assert ideal.event_key == EVENT_KEY
    assert tuple(fact.role_key for fact in ideal.ordered_facts) == (
        "older_rain",
        "recent_rain",
        "target_temperature_at_reference",
        "reference_altitude",
        "square_altitude",
        "corrected_temperature",
        "forest_share",
    )
    assert ideal.ordered_facts[0].normalized_factor == 1.0
    assert ideal.ordered_facts[5].original_value == 13.0
    assert ideal.ordered_facts[5].source_ids == (
        "test:target_temperature",
        "test:reference_altitude",
        "test:square_altitude",
    )
    assert ideal == calculate_forecast(method=METHOD_V1, inputs=make_inputs())


def test_missing_required_input_is_unavailable_and_keeps_known_facts() -> None:
    result = calculate_forecast(method=METHOD_V1, inputs=make_inputs(old_rain=None))

    assert result.whole_percentage is None
    assert result.unavailable_reason == "older_rain"
    assert result.missing_inputs == ("older_rain",)
    assert "older_rain" not in {fact.role_key for fact in result.ordered_facts}
    assert "forest_share" in {fact.role_key for fact in result.ordered_facts}


def test_missing_forest_and_low_forest_are_distinct() -> None:
    missing = calculate_forecast(method=METHOD_V1, inputs=make_inputs(forest_share=None))
    low = calculate_forecast(method=METHOD_V1, inputs=make_inputs(forest_share=0.099999))

    assert missing.unavailable_reason == "missing_forest_data"
    assert missing.missing_inputs == ("forest_share",)
    assert low.unavailable_reason == "forest_below_minimum"
    assert low.missing_inputs == ()


@pytest.mark.parametrize(
    ("role", "unit"),
    [
        ("old_rain", "deg_c"),
        ("recent_rain", "m"),
        ("target_temperature", "mm"),
        ("reference_altitude", "fraction"),
        ("square_altitude", "mm"),
        ("forest_share", "mm"),
    ],
)
def test_wrong_units_raise_value_error(role: str, unit: str) -> None:
    inputs = make_inputs()
    replacement = make_input(role=role, value=1.0, unit=unit)
    inputs = replace(inputs, **{role: replacement})

    with pytest.raises(ValueError, match="must use"):
        calculate_forecast(method=METHOD_V1, inputs=inputs)


def test_nonfinite_negative_and_out_of_range_values_raise() -> None:
    with pytest.raises(ValueError, match="finite"):
        calculate_forecast(method=METHOD_V1, inputs=make_inputs(old_rain=math.inf))
    with pytest.raises(ValueError, match="must not be negative"):
        calculate_forecast(method=METHOD_V1, inputs=make_inputs(recent_rain=-1.0))
    with pytest.raises(ValueError, match="from zero to one"):
        calculate_forecast(method=METHOD_V1, inputs=make_inputs(forest_share=1.1))


def test_future_target_requires_exact_period_composition() -> None:
    assessment = date(2028, 2, 29)
    result = calculate_forecast(method=METHOD_V1, inputs=make_inputs(assessment=assessment, days_ahead=7))

    recent = next(fact for fact in result.ordered_facts if fact.role_key == "recent_rain")
    target = next(
        fact
        for fact in result.ordered_facts
        if fact.role_key == "target_temperature_at_reference"
    )
    assert result.whole_percentage == 95
    assert recent.period_start == date(2028, 2, 29)
    assert recent.period_end == date(2028, 3, 6)
    assert recent.completed_day_count == 0
    assert recent.forecast_day_count == 7
    assert target.period_start == date(2028, 3, 7)
    assert target.completed_day_count == 0
    assert target.forecast_day_count == 1


@pytest.mark.parametrize("days_ahead", range(8))
def test_every_supported_days_ahead_value_has_matching_recent_composition(
    days_ahead: int,
) -> None:
    result = calculate_forecast(method=METHOD_V1, inputs=make_inputs(days_ahead=days_ahead))

    recent = next(fact for fact in result.ordered_facts if fact.role_key == "recent_rain")
    assert recent.completed_day_count == 7 - days_ahead
    assert recent.forecast_day_count == days_ahead


def test_target_temperature_is_adjusted_once_from_reference_to_square() -> None:
    result = calculate_forecast(method=METHOD_V1, inputs=make_inputs(target_temperature=16.25))
    corrected = next(
        fact for fact in result.ordered_facts if fact.role_key == "corrected_temperature"
    )

    assert corrected.original_value == pytest.approx(13.0)
    assert corrected.normalized_factor == 1.0

    adjusted = make_input(
        role="target_temperature",
        value=13.0,
        semantic="temperature_at_square_altitude",
    )
    with pytest.raises(ValueError, match="reference altitude"):
        calculate_forecast(method=METHOD_V1, inputs=replace(make_inputs(), target_temperature=adjusted))

    missing_semantic = replace(
        make_inputs().target_temperature,
        value=None,
        semantic=None,
    )
    with pytest.raises(ValueError, match="reference altitude"):
        calculate_forecast(method=METHOD_V1, inputs=replace(make_inputs(), target_temperature=missing_semantic))


def test_calendar_boundaries_and_altitudes_have_no_hard_exclusions() -> None:
    june = calculate_forecast(method=METHOD_V1, inputs=make_inputs(assessment=date(2026, 6, 15)))
    november = calculate_forecast(method=METHOD_V1, inputs=make_inputs(assessment=date(2026, 11, 15)))
    high = calculate_forecast(method=METHOD_V1, inputs=make_inputs(square_altitude=2500.0))
    low = calculate_forecast(method=METHOD_V1, inputs=make_inputs(square_altitude=-20.0))

    assert june.whole_percentage == november.whole_percentage == 95
    assert high.whole_percentage is not None
    assert low.whole_percentage is not None


def test_round_half_up_and_invalid_method_configuration_are_explicit() -> None:
    low_forest = calculate_forecast(method=METHOD_V1, inputs=make_inputs(forest_share=0.1))
    assert low_forest.whole_percentage == 73

    invalid_frozen_value = replace(METHOD_V1, old_rain_saturation_mm=120.0)
    with pytest.raises(ValueError, match="parameters are frozen"):
        calculate_forecast(inputs=make_inputs(), method=invalid_frozen_value)
    unsupported = replace(METHOD_V1, version="porcini-experimental-v3")
    with pytest.raises(ValueError, match="unsupported method version"):
        calculate_forecast(inputs=make_inputs(), method=unsupported)


def test_documented_future_example_keeps_values_and_period_facts() -> None:
    result = calculate_forecast(
        method=METHOD_V1,
        inputs=make_inputs(
            assessment=date(2026, 6, 15),
            days_ahead=3,
            old_rain=48.0,
            recent_rain=15.0,
            target_temperature=15.0,
            reference_altitude=500.0,
            square_altitude=500.0,
            forest_share=0.75,
        )
    )

    assert result.whole_percentage == 65
    old_rain = next(fact for fact in result.ordered_facts if fact.role_key == "older_rain")
    recent_rain = next(fact for fact in result.ordered_facts if fact.role_key == "recent_rain")
    target_temperature = next(
        fact
        for fact in result.ordered_facts
        if fact.role_key == "target_temperature_at_reference"
    )
    assert old_rain.original_value == 48.0
    assert old_rain.period_start == date(2026, 5, 28)
    assert old_rain.period_end == date(2026, 6, 10)
    assert old_rain.completed_day_count == 14
    assert old_rain.forecast_day_count == 0
    assert recent_rain.original_value == 15.0
    assert recent_rain.period_start == date(2026, 6, 11)
    assert recent_rain.period_end == date(2026, 6, 17)
    assert recent_rain.completed_day_count == 4
    assert recent_rain.forecast_day_count == 3
    assert target_temperature.period_start == date(2026, 6, 18)
    assert target_temperature.period_end == date(2026, 6, 18)
    assert target_temperature.completed_day_count == 0
    assert target_temperature.forecast_day_count == 1


def test_changed_input_changes_its_fact_and_normalized_factor() -> None:
    result = calculate_forecast(method=METHOD_V1, inputs=make_inputs(old_rain=30.0))
    fact = next(fact for fact in result.ordered_facts if fact.role_key == "older_rain")

    assert result.whole_percentage == 50
    assert fact.original_value == 30.0
    assert fact.normalized_factor == 0.5


def fact_by_role(*, result, role_key: str):
    return next(fact for fact in result.ordered_facts if fact.role_key == role_key)


def test_v2_fixed_examples_return_expected_percentages_and_facts() -> None:
    ideal = calculate_forecast(method=METHOD_V2, inputs=make_inputs())
    dry = calculate_forecast(method=METHOD_V2, inputs=make_inputs(old_rain=0.0))
    low_forest = calculate_forecast(method=METHOD_V2, inputs=make_inputs(forest_share=0.1))
    missing_old_rain = calculate_forecast(method=METHOD_V2, inputs=make_inputs(old_rain=None))

    assert ideal.whole_percentage == 95
    assert dry.whole_percentage == 5
    assert low_forest.whole_percentage == 73
    assert missing_old_rain.whole_percentage is None
    assert missing_old_rain.unavailable_reason == "older_rain"
    assert missing_old_rain.missing_inputs == ("older_rain",)
    assert ideal.method_version == "porcini-experimental-v2"
    assert tuple(fact.role_key for fact in ideal.ordered_facts) == (
        "older_rain",
        "recent_rain",
        "mean_temperature_at_reference",
        "reference_altitude",
        "square_altitude",
        "corrected_mean_temperature",
        "frost_days",
        "forest_share",
    )
    mean = fact_by_role(result=ideal, role_key="mean_temperature_at_reference")
    corrected = fact_by_role(result=ideal, role_key="corrected_mean_temperature")
    frost = fact_by_role(result=ideal, role_key="frost_days")
    assert mean.original_value == pytest.approx(16.95)
    assert mean.unit == "deg_c"
    assert mean.normalized_factor is None
    assert mean.translation_key == "forecast.fact.mean_temperature_at_reference"
    assert corrected.original_value == pytest.approx(13.7)
    assert corrected.normalized_factor == pytest.approx(1.0)
    assert corrected.source_id == "derived:corrected_mean_temperature"
    assert corrected.quality == "derived"
    assert corrected.source_ids == (
        "test:period_temperature",
        "test:reference_altitude",
        "test:square_altitude",
    )
    assert frost.original_value == 0.0
    assert frost.unit == "days"
    assert frost.normalized_factor == 1.0
    assert frost.source_id == "derived:frost_days"
    assert frost.translation_key == "forecast.fact.frost_days"
    assert frost.period_start == date(2026, 5, 27)


def test_v3_ignores_target_temperature() -> None:
    default = calculate_forecast(inputs=make_inputs())
    without_target = calculate_forecast(inputs=make_inputs(target_temperature=None))

    assert default.method_version == "porcini-experimental-v3"
    assert without_target == default


@pytest.mark.parametrize("group_index", range(5))
def test_v3_host_group_lowers_penalty_without_changing_weather(group_index: int) -> None:
    empty = HostTrees(values=(0.0,) * 5, source_id="jrc:test", source_year=2006, resolution_m=1000)
    values = [0.0] * 5
    values[group_index] = 1.0
    wooded = replace(empty, values=tuple(values))
    inputs = make_inputs()
    low = calculate_forecast(inputs=replace(inputs, host_trees=empty))
    high = calculate_forecast(inputs=replace(inputs, host_trees=wooded))
    assert low.whole_percentage == 50
    assert high.whole_percentage == 95
    assert low.method_version == METHOD_V3.version
    assert low.ordered_facts != high.ordered_facts
    assert calculate_forecast(inputs=replace(inputs, host_trees=empty), method=METHOD_V2).whole_percentage == 95


def test_v3_missing_and_partial_hosts_keep_v2_percentage_and_disclose_fallback() -> None:
    inputs = make_inputs()
    partial = HostTrees(values=(0.0, None, 0.0, 0.0, 0.0), source_id="jrc:test", source_year=2006, resolution_m=1000)
    for trees in (None, partial):
        result = calculate_forecast(inputs=replace(inputs, host_trees=trees))
        assert result.whole_percentage == calculate_forecast(inputs=inputs, method=METHOD_V2).whole_percentage
        assert result.ordered_facts[-1].quality == "forest_only_missing_host_data"
        assert result.ordered_facts[-1].normalized_factor == 1.0
    result = calculate_forecast(inputs=replace(inputs, host_trees=partial))
    chestnut = next(fact for fact in result.ordered_facts if fact.role_key == "host_chestnut")
    assert chestnut.original_value is None
    assert chestnut.period_start == date(2006, 1, 1)
    assert chestnut.source_id == "jrc:test"


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf"), True])
def test_v3_rejects_invalid_host_values(value: float) -> None:
    trees = HostTrees(values=(value, 0.0, 0.0, 0.0, 0.0), source_id="jrc:test", source_year=2006, resolution_m=1000)
    with pytest.raises(ValueError, match="host occurrence"):
        calculate_forecast(inputs=replace(make_inputs(), host_trees=trees))


def test_v2_missing_period_temperature_is_unavailable_and_keeps_known_facts() -> None:
    result = calculate_forecast(inputs=make_inputs(period_temperatures=None))
    partial = calculate_forecast(
        inputs=make_inputs(
            period_temperatures=make_period_temperatures(
                daily_means=(16.95,) * 19 + (None,),
            )
        )
    )

    assert result.whole_percentage is None
    assert result.unavailable_reason == "period_temperature"
    assert result.missing_inputs == ("period_temperature",)
    roles = {fact.role_key for fact in result.ordered_facts}
    assert {"older_rain", "forest_share", "reference_altitude"} <= roles
    assert "mean_temperature_at_reference" not in roles
    assert "frost_days" not in roles
    assert partial.unavailable_reason == "period_temperature"


def test_v2_missing_order_and_reason_precedence() -> None:
    result = calculate_forecast(
        inputs=make_inputs(
            old_rain=None,
            period_temperatures=None,
            reference_altitude=None,
            square_altitude=None,
        )
    )
    no_forest = calculate_forecast(inputs=make_inputs(period_temperatures=None, forest_share=None))

    assert result.unavailable_reason == "older_rain"
    assert result.missing_inputs == (
        "older_rain",
        "period_temperature",
        "reference_altitude",
        "square_altitude",
    )
    assert no_forest.unavailable_reason == "missing_forest_data"


def test_v2_future_example_keeps_values_and_period_facts() -> None:
    result = calculate_forecast(
        inputs=make_inputs(
            assessment=date(2026, 6, 15),
            days_ahead=3,
            old_rain=48.0,
            recent_rain=15.0,
            reference_altitude=500.0,
            square_altitude=500.0,
            forest_share=0.75,
            period_temperatures=make_period_temperatures(
                assessment=date(2026, 6, 15), days_ahead=3, mean=15.0, minimum=8.0
            ),
        )
    )

    mean = fact_by_role(result=result, role_key="mean_temperature_at_reference")
    assert result.whole_percentage == 67
    assert mean.period_start == date(2026, 5, 30)
    assert mean.period_end == date(2026, 6, 18)
    assert mean.completed_day_count == 16
    assert mean.forecast_day_count == 4


def test_v2_frozen_alps_period_gives_minimum_percentage() -> None:
    result = calculate_forecast(
        inputs=make_inputs(
            assessment=date(2026, 1, 20),
            reference_altitude=1500.0,
            square_altitude=1500.0,
            period_temperatures=make_period_temperatures(
                assessment=date(2026, 1, 20), mean=-5.0, minimum=-10.0
            ),
        )
    )

    assert result.whole_percentage is not None
    assert result.whole_percentage <= 15
    assert result.whole_percentage == 5


def test_v2_puglia_heat_period_gives_minimum_percentage() -> None:
    result = calculate_forecast(
        inputs=make_inputs(
            assessment=date(2026, 8, 5),
            reference_altitude=100.0,
            square_altitude=100.0,
            period_temperatures=make_period_temperatures(
                assessment=date(2026, 8, 5), mean=34.0, minimum=24.0
            ),
        )
    )

    assert result.whole_percentage is not None
    assert result.whole_percentage <= 15
    assert result.whole_percentage == 5


def test_v2_one_frost_day_lowers_the_percentage() -> None:
    minimums = (8.0,) * 19 + (2.0,)
    result = calculate_forecast(
        inputs=make_inputs(
            period_temperatures=make_period_temperatures(daily_minimums=minimums)
        )
    )

    frost = fact_by_role(result=result, role_key="frost_days")
    assert result.whole_percentage == 73
    assert frost.original_value == 1.0
    assert frost.normalized_factor == 0.75


def test_v2_frost_lapse_applies_to_the_minimum_but_not_at_equal_altitude() -> None:
    minimums = (8.0,) * 19 + (2.0,)
    same_altitude = calculate_forecast(
        inputs=make_inputs(
            square_altitude=500.0,
            period_temperatures=make_period_temperatures(mean=13.7, daily_minimums=minimums),
        )
    )
    higher_square = calculate_forecast(
        inputs=make_inputs(
            period_temperatures=make_period_temperatures(daily_minimums=minimums)
        )
    )

    assert fact_by_role(result=same_altitude, role_key="frost_days").original_value == 0.0
    assert fact_by_role(result=higher_square, role_key="frost_days").original_value == 1.0
    assert same_altitude.whole_percentage == 95
    assert higher_square.whole_percentage == 73


@pytest.mark.parametrize(
    ("corrected_mean", "expected_factor", "expected"),
    [(13.7, 1.0, 95), (10.0, 0.239, 27), (20.0, 0.5, 50), (5.0, 0.000366, 5), (34.0, 0.000749, 5)],
)
def test_v2_temperature_curve_peaks_at_13_7_and_halves_at_20(
    corrected_mean: float, expected_factor: float, expected: int
) -> None:
    result = calculate_forecast(
        inputs=make_inputs(
            period_temperatures=make_period_temperatures(mean=corrected_mean + 3.25)
        )
    )

    corrected = fact_by_role(result=result, role_key="corrected_mean_temperature")
    assert corrected.normalized_factor == pytest.approx(expected_factor, abs=1e-4)
    assert result.whole_percentage == expected


@pytest.mark.parametrize("days_ahead", range(8))
def test_v2_every_days_ahead_value_has_matching_temperature_composition(
    days_ahead: int,
) -> None:
    result = calculate_forecast(inputs=make_inputs(days_ahead=days_ahead))

    mean = fact_by_role(result=result, role_key="mean_temperature_at_reference")
    assert mean.completed_day_count == 19 - days_ahead
    assert mean.forecast_day_count == days_ahead + 1


def test_v2_invalid_temperature_records_raise_value_error() -> None:
    with pytest.raises(ValueError, match="20 entries"):
        calculate_forecast(
            inputs=make_inputs(
                period_temperatures=make_period_temperatures(daily_means=(16.95,) * 19)
            )
        )
    with pytest.raises(ValueError, match="reference altitude"):
        calculate_forecast(
            inputs=make_inputs(
                period_temperatures=make_period_temperatures(semantic="temperature_at_square_altitude")
            )
        )
    with pytest.raises(ValueError, match="wrong period"):
        calculate_forecast(
            inputs=make_inputs(
                period_temperatures=make_period_temperatures(period_start=date(2026, 5, 1))
            )
        )
    with pytest.raises(ValueError, match="finite"):
        calculate_forecast(
            inputs=make_inputs(
                period_temperatures=make_period_temperatures(daily_minimums=(math.nan,) * 20)
            )
        )
    wrong_unit = replace(make_period_temperatures(), unit="mm")
    with pytest.raises(ValueError, match="must use deg_c"):
        calculate_forecast(inputs=make_inputs(period_temperatures=wrong_unit))


def _thirty_days(*, temperatures=None, skip: int | None = None, missing: tuple[int, str] | None = None, rain: float = 4.0):
    days = []
    for index, offset in enumerate(range(30, 0, -1)):
        values = {"rain_mm": rain + index % 3, "temperature_c": 12.0, "et0_mm": 2.0}
        if temperatures is not None and index >= 23:
            values["temperature_c"] = temperatures[index - 23]
        if missing is not None and missing[0] == index:
            values[missing[1]] = None
        days.append(WeatherDay(day=ASSESSMENT - timedelta(days=offset), **values))
    return tuple(days[1:] if skip is not None else days)


def _fast_and_slow(
    *, base: ForecastInputs, forest_shares: list, altitudes: list, method=METHOD_V4,
) -> tuple[list, list]:
    from sporameteo.forecast_method import calculate_v4_scores

    fast = calculate_v4_scores(inputs=base, forest_shares=forest_shares, altitudes=altitudes, method=method)
    slow = []
    for forest, altitude in zip(forest_shares, altitudes, strict=True):
        result = calculate_versioned_forecast(inputs=replace(
            base,
            forest_share=None if forest is None else make_input(role="forest_share", value=forest),
            square_altitude=None if altitude is None else make_input(role="square_altitude", value=altitude),
        ), method=method)
        slow.append((result.whole_percentage, result.unavailable_reason))
    return fast, slow


@pytest.mark.parametrize("method", [METHOD_V4, METHOD_V5])
@pytest.mark.parametrize("reference_altitude", [500.0, None])
@pytest.mark.parametrize(
    "weather",
    [
        _thirty_days(),
        _thirty_days(missing=(3, "rain_mm")),
        _thirty_days(missing=(10, "temperature_c")),
        _thirty_days(missing=(29, "et0_mm")),
        _thirty_days(skip=1),
        _thirty_days(temperatures=(0.0, 5.0, 8.0, 12.0, 18.0, 22.0, 25.0)),
        _thirty_days(temperatures=(30.0, 29.0, 6.5, 3.0, 0.1, -4.0, 20.0)),
    ],
)
def test_fast_v4_scores_equal_the_full_calculation_square_by_square(weather, reference_altitude, method) -> None:
    forests = [None, 0.05, 0.1, 0.5, 1.0]
    altitudes = [None, -20.0, 0.0, 200.0, 500.0, 1500.0]
    pairs = [(forest, altitude) for forest in forests for altitude in altitudes]
    base = replace(
        make_inputs(reference_altitude=reference_altitude),
        daily_weather=weather,
        daily_weather_source_id="test:30-days",
    )
    fast, slow = _fast_and_slow(base=base, forest_shares=[p[0] for p in pairs], altitudes=[p[1] for p in pairs],
                              method=method)
    assert fast == slow


@pytest.mark.parametrize(
    ("weather", "forest", "altitude"),
    [
        (_thirty_days(), 1.5, 500.0),
        (_thirty_days(), 0.5, math.nan),
        (_thirty_days(), 0.5, True),
        (_thirty_days(rain=-1.0), 0.5, 500.0),
        (_thirty_days(missing=(3, "rain_mm"))[:2] + (WeatherDay(day=ASSESSMENT, rain_mm=1.0, temperature_c=1.0, et0_mm=1.0),) * 28, 0.5, 500.0),
    ],
)
def test_fast_v4_scores_raise_where_the_full_calculation_raises(weather, forest, altitude) -> None:
    from sporameteo.forecast_method import calculate_v4_scores

    base = replace(make_inputs(), daily_weather=weather, daily_weather_source_id="test:30-days")
    with pytest.raises(ValueError):
        calculate_versioned_forecast(inputs=replace(
            base,
            forest_share=make_input(role="forest_share", value=forest),
            square_altitude=make_input(role="square_altitude", value=altitude),
        ))
    with pytest.raises(ValueError):
        calculate_v4_scores(inputs=base, forest_shares=[0.5, forest], altitudes=[500.0, altitude])


def test_v5_result_carries_the_v5_version_and_v4_keeps_its_own() -> None:
    inputs = replace(make_inputs(old_rain=0.0, recent_rain=70.0),
                     daily_weather=_thirty_days(), daily_weather_source_id="test:30-days")
    assert calculate_versioned_forecast(inputs=inputs, method=METHOD_V5).method_version == "porcini-experimental-v5"
    assert calculate_versioned_forecast(inputs=inputs, method=METHOD_V4).method_version == "porcini-experimental-v4"


def test_modified_v5_parameters_are_rejected_as_frozen() -> None:
    with pytest.raises(ValueError, match="frozen"):
        calculate_versioned_forecast(inputs=make_inputs(), method=replace(METHOD_V5, maximum_drying_mm=1.0))
