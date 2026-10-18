# Experimental porcini forecast method

This document describes the experimental porcini method in
`src/sporameteo/forecast_method.py` and `src/sporameteo/moisture_method.py`.
It calculates one whole percentage for one 500 m square and one Italian local
date. The percentage is an experimental, unvalidated estimate. It is not a
measured probability and it is not a chance that a visitor will find a
mushroom.

New estimate updates use `porcini-experimental-v5`, described in the next two
sections. The code keeps v1 to v4 so saved updates retain their original
percentages and explanations. The sections after them describe v1 to v3.

The event is visible fruiting bodies from the four-species porcini group,
*Boletus edulis*, *B. aereus*, *B. reticulatus* and *B. pinophilus*, somewhere
in the selected 500 m square on the Italian local target date. The event key is
`visible_porcini_fruiting_bodies_in_500m_cell_on_italian_date`.

## Version 4

v4 reads the 30 days before the target date. Each day has rain, mean air
temperature and reference evapotranspiration, which is the water a short,
well watered grass would lose to the air that day. The temperature is moved to
the height of the square at 0.65 deg C per 100 m.

```text
water starts at 40 mm on the first of the 30 days
each day: water = min(80, max(0, 0.95 * water + min(rain, 25) - drying))
v4 drying = 0.5 * evapotranspiration
water factor of a day = min(1, water / 40)
moisture = min(last day's water factor, mean water factor of the last 7 days)
temperature = mean band score of the last 7 days
whole_percentage = ROUND_HALF_UP(5 + 90 * moisture * temperature)
```

The band score is 0 at 0 deg C, 0.1 at 5 deg C, 0.4 at 8 deg C, 1 from 12 to
18 deg C, 0.7 at 22 deg C, 0.3 at 25 deg C and 0 at 30 deg C, with straight
lines between them. Squares with less than a tenth of forest get no estimate,
as decision D17 requires. Host trees are shown but do not change the score.

The temperature bands follow the owner's document named in decision D21. The
other numbers are project choices recorded in
`reports/mvp/pistoia_formula_reassessment.md`: the 40 mm start, the 95
percent kept each day, the 25 mm daily limit, the 80 mm maximum, the half of
the evapotranspiration and the 40 mm full level.

## Version 5

v5 changes one rule of v4. The daily drying can be at most 0.5 mm:

```text
v5 drying = min(0.5 * evapotranspiration, 0.5)
```

| Rule | Value | Source |
| --- | ---: | --- |
| Highest daily drying of the store | 0.5 mm | Paulsen and Weiler, 2026, table 5. |

[Paulsen and Weiler](https://doi.org/10.5194/hess-30-4305-2026) weighed the
top 15 cm of the forest floor, litter and the soil under it, in boxes at
three beech and spruce woods of the Black Forest, 415 to 1,163 m high. From
May 2024 to August 2025 the floor lost 0.51 to 0.54 mm a day on days without
rain at all three sites. The authors summarise it as about 0.5 mm a day. A
fully closed canopy kept the summer rate low, at most 0.16 mm an hour.

v4 lost half the evapotranspiration, which is 2 to 3 mm a day in a southern
summer. At that rate a 10 mm storm was gone in about four days. v5 keeps the v4 rule
when it dries less than 0.5 mm, which happens on days with less than 1 mm of
evapotranspiration, mostly in winter. On those days v5 equals v4. On every
other day v5 dries less, so a v5 score is never lower than v4 for the same
weather.

Limits of the source:

- The boxes held no living roots, so the measure leaves out the water that
  trees draw from the top soil.
- The study covers a wet period in German woods, not a dry southern summer.
- The 95 percent kept each day still drains 2 mm a day from a store of 40 mm.
  That rule is unchanged and remains a project choice.

`reports/water_store_v5.md` compares v4 and v5 at 11 places, using finds only
to check the change. No find set the number.

## Host trees in v3

JRC provides modelled relative occurrence for individual trees on a 1 km grid.
The selected release describes 2006. The loader samples the native pixel at
the centre of each requested 500 m square. Adjacent squares can share a value.
The values do not measure forest cover or identify the dominant tree.

The source set contains beech, chestnut, seven oaks, seven pines, Norway spruce
and silver fir. `GROUP_SPECIES` in `src/sporameteo/host_trees.py` lists all 18
species. Every selected dataset's JRC catalogue title was checked for the
2006 date and C-SMFAv0-3-2 release. The
[source assessment](../reports/mvp/tree_species_coverage.md) records the choice
and limitations. Each local manifest records file checksums, download dates,
source URLs and credit. Zero remains a valid value; the raster's negative
missing marker stays missing. A live check also found -1 pixels outside the
declared mask. The loader treats every negative value as missing, never zero.

For each host group, take the maximum occurrence among its selected species.
A group is missing if any selected species in it is missing. If all five
groups are available, apply this formula before rounding the percentage:

```text
h = max(beech, chestnut, oaks, pines, spruce_fir)
host_multiplier = 0.50 + 0.50 * h
score_v3 = score_v2 * host_multiplier
whole_percentage = ROUND_HALF_UP(5 + 90 * score_v3)
```

The maximum, equal treatment of the five groups, and 0.50 coefficients are
project choices. They are not fitted weights or a probability that any host
is present. Adding probabilities would count overlapping species more than
once. Taking their maximum is a simple experimental rule, not a validated
way to compare differently calibrated tree maps. The selected groups do not
include every possible host. Low scores reduce the estimate without excluding
the square.

If the source is unavailable or any group is missing, v3 uses `score_v2`
without a tree penalty. The `host_tree_factor` fact records a multiplier of
1 and quality `forest_only_missing_host_data`. Known group facts remain
visible; unknown group facts have a null value. Missing tree data are never
converted to zero. Other missing required inputs still make the estimate
unavailable as described below.

With ideal v2 weather and full forest cover, `score_v2` is 1:

| Tree inputs | Multiplier | Percentage |
| --- | ---: | ---: |
| Beech or chestnut equals 1, other groups equal 0 | 1.00 | 95 |
| Highest complete group equals 0.8 | 0.90 | 86 |
| All five groups equal 0 | 0.50 | 50 |
| Any group missing | 1.00, disclosed fallback | 95 |

The fallback can score higher than a complete low-host record. This is an
explicit limitation of keeping the existing estimate when tree evidence is
unavailable. For dry or frozen conditions, the v2 score is already zero and
all these cases remain at 5.

### Host evidence

The references support host associations, not the numerical weights above.
The four porcini species need not use every listed host group equally.

| Group | Published support |
| --- | --- |
| Beech | Beugelsdijk and colleagues list Fagus among B. edulis hosts. |
| Chestnut | The 2023 chestnut-orchard study measures B. edulis and B. reticulatus mycelium. |
| Oaks | Beugelsdijk and colleagues link Quercus with B. aereus, B. reticulatus and B. edulis. |
| Pines | Their study links B. pinophilus mainly with Pinus and also discusses pine-associated B. edulis. |
| Spruce and fir | Their host tables include Picea and Abies within B. edulis and B. pinophilus descriptions. |

Sources are [Beugelsdijk et al., 2008](https://pmc.ncbi.nlm.nih.gov/articles/PMC2865352/)
and the [2023 chestnut study](https://www.frontiersin.org/journals/soil-science/articles/10.3389/fsoil.2023.1159793/full).
Independent Italian observations have not validated this v3 modifier.

### Cache and saved explanations

The setup command downloads files once. Workers sample only requested areas
and store the five group values in `HostTreeCache`, keyed by stable square ID
and manifest fingerprint. There are no provider calls in runtime sampling.
Each estimate update saves its tree inputs and source metadata. The update
fingerprint includes those inputs, so changed tree values create a distinct
update. Area details read saved inputs even if local files or cache rows change.
Queue identities include the method and source version, so installing a source
does not reuse a completed forest-only task for the same weather snapshot.
Old v1 and v2 updates ignore tree data entirely.

## Public records

`NumericInput` is a frozen record with these fields:

| Field | Meaning |
| --- | --- |
| `value` | A finite number, or `None` when the value is missing. |
| `unit` | Exactly `mm`, `deg_c`, `m` or `fraction`, as required by the input. |
| `source_id` | The stable identifier of the source record or build. |
| `period_start`, `period_end` | Inclusive dates covered by the value. |
| `completed_day_count` | Number of completed days in the period. |
| `forecast_day_count` | Number of forecast days in the period. |
| `quality` | Optional source quality text. |
| `semantic` | Temperature records must use the canonical value `temperature_at_reference_altitude`. |

`DailyTemperatures` is a frozen record with the same period, day count, source,
quality and semantic fields. Instead of one value it holds `daily_means` and
`daily_minimums`, one entry for each day of its period. Both are air
temperatures at the Open-Meteo reference altitude, in `deg_c`.

`ForecastInputs` contains `assessment_date`, `target_date`, the numeric inputs
`old_rain`, `recent_rain`, `target_temperature`, `reference_altitude`,
`square_altitude` and `forest_share`, and the daily record
`period_temperatures`, plus optional `host_trees`. v2 and v3 read `period_temperatures` and ignore
`target_temperature`. v1 reads `target_temperature` and ignores
`period_temperatures`.

`calculate_forecast` takes the inputs and a method, which defaults to v3. It
returns a frozen `ForecastResult` with these fields:

| Field | Meaning |
| --- | --- |
| `whole_percentage` | An integer from 5 through 95, or `None` when no estimate is available. |
| `unavailable_reason` | A stable reason key, or `None` for an estimate. |
| `missing_inputs` | Ordered input keys with missing values. |
| `method_version` | The version string, such as `porcini-experimental-v2`. |
| `event_key` | The event described above. |
| `experimental_unvalidated` | Always `True` for this method. |
| `ordered_facts` | Frozen facts produced by the same calculation, in a stable order. |

The calculation does no network or database work. The estimate update service
reads the stored weather, terrain and forest records and passes them in.

For v4 and v5 the service calls `calculate_v4_scores` instead, once per
weather cell and target date. In both versions the moisture part of the score depends only on the
weather of the cell, so it is worked out once. Each square then gets only its
altitude step and the final score. The percentages and missing reasons are
the same as from `calculate_forecast`, without the facts. It is about 9 times
faster on real cells, as `reports/italy_night_cost.md` records. The detail
view still uses `calculate_forecast` for the facts.

## Required input schema

All input periods are inclusive. A `None` record or a record whose `value` is
`None` is missing. A daily temperature record is missing when any daily mean or
daily minimum is `None`. A numeric zero is a valid value.

| Input | Unit | Period | Completed days | Forecast days |
| --- | --- | --- | ---: | ---: |
| `old_rain` | `mm` | Target minus 21 through target minus 8 | 14 | 0 |
| `recent_rain` | `mm` | Target minus 7 through target minus 1 | `7 - days_ahead` | `days_ahead` |
| `period_temperatures` | `deg_c` | Target minus 19 through target | `19 - days_ahead` | `days_ahead + 1` |
| `reference_altitude` | `m` | Assessment date only | 1 | 0 |
| `square_altitude` | `m` | Assessment date only | 1 | 0 |
| `forest_share` | `fraction` | Assessment date only | 1 | 0 |

`days_ahead` is the number of days from the assessment date to the target date.
It must be from zero through seven. Days before the assessment date count as
completed. The assessment date and later days count as forecast, so the target
day is always a forecast day. For example, a target three days after
assessment has 16 completed and 4 forecast days in its temperature period.

The temperature period has 20 days. Each weather snapshot stores the 21 days
before the assessment date and the 7 days after it, so the 20 days ending on
any target date are always inside the snapshot.

Terrain and forest values use the assessment date as their build date. A
regional tree value is not used. v3 accepts the optional JRC `HostTrees` record
with five occurrence values, source ID, year, resolution and source metadata.

The method rejects wrong units, dates in the wrong period, inconsistent day
counts, daily lists of the wrong length, negative rain, non-finite numbers and
forest shares outside zero through one. It accepts finite low and high
altitudes. It rejects temperatures without the canonical reference-altitude
semantic, because it applies the altitude adjustment once itself.

## Weather and forest formula shared with v2

```text
lapse = 0.0065 * (square_altitude - reference_altitude)
corrected_mean = average(daily_means) - lapse
frost_days = number of days with daily_minimum - lapse below 0

d = clamp(old_rain / 60)
r = clamp(recent_rain / 30)
t = exp(-0.10455 * (corrected_mean - 13.7)^2)
    when corrected_mean is 13.7 or lower
t = exp(-ln(2) * ((corrected_mean - 13.7) / (20 - 13.7))^2)
    when corrected_mean is above 13.7
k = clamp(1 - frost_days / 4)
f = clamp((forest_share - 0.10) / 0.90)

score = d * (0.90 + 0.10 * r) * t * k * (0.75 + 0.25 * f)
whole_percentage = ROUND_HALF_UP(5 + 90 * score)
```

`clamp` sets a value below zero to zero and a value above one to one. Raw
invalid inputs are rejected before this step. The implementation uses decimal
round-half-up after the score is calculated, checks that the score is finite
and from zero through one, and never repairs a bad method configuration.

| Parameter | Value | Source or status |
| --- | ---: | --- |
| Older rain saturation | 60 mm | Project choice that maps the Piemonte rain description. |
| Recent rain saturation | 30 mm | Project choice. |
| Recent rain base and weight | 0.90 and 0.10 | Project choice, unchanged from v1. |
| Temperature period | 20 days ending on the target date | Bielefeld preprint, which used a 20-day mean. |
| Temperature peak | 13.7 deg C | Bielefeld preprint, table S1, all sites, 20-day temperature and 26-day rain windows. |
| Cold side of the curve | 0.10455 | Same table row, the quadratic temperature term of the fitted model. |
| Warm side of the curve | Half at 20 deg C | Project choice. 20 deg C is the highest 20-day mean the Bielefeld study observed. |
| Frost threshold | Daily minimum below 0 deg C at the square | Project choice, same threshold as the stored frost flag. |
| Frost days that bring the score to zero | 4 | Project choice. No source gives a number. |
| Temperature lapse rate | 0.0065 deg C per metre | Open-Meteo elevation adjustment assumption. |
| Minimum forest share | 0.10 | Decision D17. |
| Forest base and weight | 0.75 and 0.25 | Project choice, unchanged from v1. |
| Output bounds | 5 through 95 | Project choice for display. |

Only the temperature peak and the cold side of the curve come from a fitted
model. The project fitted nothing itself. No year or area kept aside for the
final benchmark was used to choose any value.

### Why each rule exists

Cold and heat. The temperature factor multiplies the whole score. v1 let
temperature remove at most a quarter of the score, so a frozen January forest
in the Alps and a 34 deg C August day in Puglia both showed 73 percent with
good rain. Porcini do not fruit in either case. In v2 both give 5 percent.

The Bielefeld study modelled daily porcini counts with a quadratic temperature
term on a log scale. Its predicted production relative to the peak is
`exp(-0.10455 * (T - 13.7)^2)`. v2 uses that curve as it is below the peak. It
gives 0.24 at 10 deg C and less than 0.04 at 8 deg C, which matches the
study's finding that fruiting was largely absent at 20-day means of 5 to 10 deg
C, the most common conditions in its seasons.

Above the peak, the same curve gives 0.016 at 20 deg C. v2 does not use it
there, for two reasons. The study observed 20-day means only up to 20 deg C,
in one German beech forest, for *B. edulis* only, from August to November. The
Piemonte material describes *B. reticulatus* and *B. aereus* as warmth-loving
summer species in Italy. The published curve would set almost every July and
August estimate to the floor. v2 instead halves the factor at 20 deg C and
falls slowly after that: 0.22 at 23 deg C, 0.05 at 27 deg C and almost zero at
34 deg C. The half point at 20 deg C is a project choice, not a measured value.

Several days instead of one. The Bielefeld study compared windows from 2 to 35
days and found the best fit with a 20-day mean temperature. One warm day in a
cold month no longer lifts the score, and one hot day in a mild month no longer
drops it.

Frost. No study the project found gives a number of frost days. The Piemonte
material says the first autumn frosts stop *B. reticulatus*, and that
*B. edulis*, *B. aereus* and *B. pinophilus* can be found until the first
frosts and beyond, into late November when leaf litter covers the ground. The
Bielefeld monitoring stopped each year at the first heavy frost. v2 therefore
lets one frost night lower the score without ending it. Each day in the period
whose minimum is below zero at the square removes a quarter of the score, and
four frost days bring it to the floor. The minimum gets the same altitude
adjustment as the mean, so a square 500 m above the weather cell centre counts
frost 3.25 deg C sooner.

No season rule. v1 removed the 15 June to 15 November season in #47 because one
calendar range does not fit four species from the Alps to Sicily. The Piemonte
material describes spring fruiting for some species, and *B. aereus* in
southern oak and cork oak woods fruits into December. With the temperature and
frost rules, a winter in the Alps or a lowland summer heat already gives 5
percent. A fixed season would only remove the mild-winter and early-summer
cases that the sources describe. The project found no source that gives a day
length limit for these species, so v2 has none.

Recent rain. The rain of the last 7 days still changes the score by at most 10
percent. The Piemonte timing and the Bielefeld counts both point to rain 8 to
30 days before fruiting, which the older rain input covers. No source in the
project supports a stronger effect for the last week.

## Contributing facts

`ordered_facts` contains known values even when another required input is
missing. Each fact records the original value, unit, source identifier, source
identifiers for derived values, inclusive dates, completed and forecast day
counts, a normalized factor when one exists, a role key and a translation key.
The v2 order is:

| Role key | Value and factor |
| --- | --- |
| `older_rain` | Original 14-day rain and `d`. |
| `recent_rain` | Original seven-day rain and `r`. |
| `mean_temperature_at_reference` | 20-day mean temperature from the weather provider, with the period's completed and forecast day counts. |
| `reference_altitude` | Provider elevation used as the adjustment reference. |
| `square_altitude` | Terrain elevation of the square. |
| `corrected_mean_temperature` | 20-day mean at the square altitude and `t`. |
| `frost_days` | Number of frost days at the square, unit `days`, and `k`. |
| `forest_share` | Original forest fraction and `f`. |

The two derived facts name the temperature, reference altitude and square
altitude sources. Missing weather facts do not appear. v3 adds `host_beech`,
`host_chestnut`, `host_oaks`, `host_pines`, `host_spruce_fir` and the multiplier
when tree records exist. Group facts describe the source year, with zero
weather-day counts. The multiplier fact always appears, including a disclosed
fallback. The area API includes credit and resolution in `source_metadata`.

## Missing results

| Condition | `whole_percentage` | `unavailable_reason` |
| --- | --- | --- |
| Missing old rain | `None` | `older_rain` |
| Missing recent rain | `None` | `recent_rain` |
| Missing temperature on any day of the period | `None` | `period_temperature` |
| Missing reference altitude | `None` | `reference_altitude` |
| Missing square altitude | `None` | `square_altitude` |
| Missing forest share | `None` | `missing_forest_data` |
| Forest share below 0.10 | `None` | `forest_below_minimum` |

When more than one required value is missing, `missing_inputs` lists all
missing keys in the order of this table, with `forest_share` last. The first
missing key is the unavailable reason unless the forest value is missing or
below the minimum.

## Worked examples

The tests in `tests/app/test_forecast_method.py` check every example below.

The first examples use an assessment and target date of 15 June 2026, reference
altitude 500 m and square altitude 1,000 m. Every daily mean is 16.95 deg C and
every daily minimum is 8 deg C. The adjustment gives a 13.7 deg C mean at the
square and no frost days.

| Older rain | Recent rain | Forest share | Result |
| ---: | ---: | ---: | ---: |
| 60 mm | 30 mm | 1.00 | 95 |
| 0 mm | 30 mm | 1.00 | 5 |
| 60 mm | 30 mm | 0.10 | 73 |
| Missing | 30 mm | 1.00 | Unavailable, `older_rain` |
| 60 mm | 30 mm | 1.00, one daily mean missing | Unavailable, `period_temperature` |

With the ideal row and one daily minimum of 2 deg C at the reference altitude,
that night is -1.25 deg C at the square. The result is 73 and the `frost_days`
fact has value 1 and factor 0.75.

Cold, heat and points on the temperature curve, all with 60 mm older rain, 30
mm recent rain, full forest and no frost unless stated:

| Case | Mean at the square | Factor `t` | Result |
| --- | ---: | ---: | ---: |
| Alps, 20 January 2026, square and reference at 1,500 m, minimums -10 deg C | -5 deg C | 0.00 | 5 |
| Puglia, 5 August 2026, square and reference at 100 m | 34 deg C | 0.00 | 5 |
| Cold, below the observed fruiting range | 5 deg C | 0.00 | 5 |
| Cold side | 10 deg C | 0.24 | 27 |
| Peak | 13.7 deg C | 1.00 | 95 |
| Warm half point | 20 deg C | 0.50 | 50 |

A future example uses assessment date 15 June 2026 and target date 18 June
2026. The older rain period is 28 May through 10 June with 14 completed days.
The recent rain period is 11 through 17 June with four completed and three
forecast days. The temperature period is 30 May through 18 June with 16
completed and 4 forecast days. For older rain 48 mm, recent rain 15 mm, every
daily mean 15 deg C, minimums above zero, both altitudes 500 m and forest share
0.75, the result is 67.

## Version 1

`porcini-experimental-v1` used the target day's mean temperature only, with
the factor `0.75 + 0.25 * clamp(1 - abs(corrected - 13) / 10)`, and had no
frost rule. Its other inputs and parameters match v2. The code keeps it
unchanged and the estimate service uses it to recalculate facts for updates
stored with that version. #66 replaced it because temperature could lower the
score by at most a quarter.

## Source evidence and limits

The Regione Piemonte material describes porcini appearing 12 to 15 days after
at least 30 mm of rain in the relevant earlier period. See the [official
regional resource](https://www.regione.piemonte.it/web/media/14353/download),
the [timing pages](https://www.regione.piemonte.it/web/sites/default/files/media/documenti/2019-11/funghi_tartufi_risorse_del_bosco_pagg_38-52.pdf)
and the [*B. aereus* pages](https://www.regione.piemonte.it/web/sites/default/files/media/documenti/2019-11/funghi_tartufi_risorse_del_bosco_pagg._53-72.pdf).
This supports the delayed rain input. It does not give a national percentage
or a one-degree cooling rule.

The Bielefeld *B. edulis* preprint used one German beech forest from 2015 to
2024, visited 2 to 4 times a week from early August until the first heavy
frost. Fruiting was concentrated at a 20-day mean temperature of 10 to 15 deg
C, with a fitted peak of 13.7 deg C for all sites, after rain over the previous
26 days. The 20-day means of its seasons ranged from 5 to 20 deg C. It does not
calibrate an Italian four-species percentage. The [preprint](https://www.biorxiv.org/content/10.64898/2025.12.12.693895v2)
also found that the cooling rule did not separate porcini days there.

The Piemonte material places *B. edulis* roughly from 300 m to 2,000 m with
tree-line exceptions. It places *B. aereus* around 200 m to 1,000 m, sometimes
to 1,400 m, with autumn and regional spring differences. These ranges do not
support one national altitude exclusion. The method keeps altitude as a
temperature adjustment and forest as a minimum eligibility value only.

The Swiss study reports a shift in autumn fungal timing after 1991. It does not
measure this four-species Italian event. See [Global Change Biology,
10.1111/gcb.12263](https://pubmed.ncbi.nlm.nih.gov/23686649/).

The method follows local decisions D17 and D18 and the limits in [P2](../reports/p2_habitat.md),
[P3](../reports/p3_weather.md) and [P4](../reports/p4_benchmark.md). P4's
rain share is evidence for a useful input, not a 95 percent chance. P4's
failed place learner and P5's synthetic map numbers do not enter this method.
The formula does not use soil, slope, aspect, a cooling rule, a
season rule or visitor finding records as absence evidence.

The [Open-Meteo documentation](https://open-meteo.com/en/docs) describes the
provider's weather-centre elevation and temperature adjustment. The estimate
service passes the reference altitude and square altitude separately so the
method adjusts once.
