# A slower drying water store in method v5

Issue #171.

## Question

The #156 check found summer and early autumn finds at Sila and Valle Gesso on
days when the v4 water store was empty. Can a rule taken from a published
source make the store answer to summer rain, without raising months that are
out of season?

## Answer

Yes. v5 caps the daily drying of the store at 0.5 mm, the rate Paulsen and
Weiler measured on the forest floor of beech and spruce woods. The app now
uses v5.

- All 10 find days at Sila and all 3 at Valle Gesso score higher. Sila's
  summer and October finds went from 5 to 9 to 27 to 50. Valle Gesso's June
  finds went from 11 to 16 to 68 to 75.
- No find day at any of the 11 places scores lower than with v4.
- From December to February, one month at one place crosses 50 more often:
  Etna north in February, on 1 day instead of none.
- March rises at 7 places. With the owner's agreement, March counts as
  possible spring season, so these rises do not fail the check. They are
  listed below.

Run: `reports/runs/method_outside_tuscany/20261018T121738Z-88de99.json`.
Script: `experiments/method_outside_tuscany/run.py`.

## What the weather cells recorded

The Sila weather cell recorded no heavy summer storm. From June to September
2026 its wettest day had 13 mm, so the 25 mm daily limit of the store never
applied. The wettest spell brought 31 mm from 23 June to 3 July. The
provider's own topsoil moisture stayed between 0.01 and 0.1 the whole summer.

A store that loses half the evapotranspiration each day loses 2 to 3 mm a day
there in summer. Rains of 3 to 9 mm were gone within a few days, before the
7-day mean of the water factor could rise. Removing the 25 mm limit
changed no find day at Sila or Valle Gesso. Only a slower drying store lifted
them.

## The change and its source

```text
v4 drying = 0.5 * evapotranspiration
v5 drying = min(0.5 * evapotranspiration, 0.5)
```

Paulsen and Weiler, 2026, Hydrology and Earth System Sciences 30, 4305 to
4319, [doi 10.5194/hess-30-4305-2026](https://doi.org/10.5194/hess-30-4305-2026).
They weighed boxes filled with the top 15 cm of the forest floor at three
beech and spruce sites in the Black Forest, at 415, 841 and 1,163 m. From May
2024 to August 2025 their table 5 gives 0.51, 0.54 and 0.53 mm a day on days
without rain. The text summarises this as about 0.5 mm a day.

v5 equals v4 on days with less than 1 mm of evapotranspiration. On every
other day it dries less, so a v5 score is never lower than a v4 score for the
same weather. `docs/forecast_method.md` gives the full rule and the limits
of the source.

No find set this number. The number was chosen from the paper before the
rerun and was not changed after it.

## How the check was done

The script is the one from #156. It calculated v4 and v5 for every forest
square and every day from 17 October 2025 to 16 October 2026, as an estimate
for the same day. The 10 places and the finds within 30 km are those of #156.
Pistoia was added as the home area of D11, at the weather cell 43.95 N,
10.85 E, with 315 forest squares and 32 finds nearby in all years.

The v4 results of the 10 original places match the #156 run on all 3,650
days. The weather of the 10 cells came from the files saved by #156. Only the
Pistoia cell was read again from the Open-Meteo files, with 810 requests and
1.35 MB in 35 seconds. No Open-Meteo API call was made and nothing was written
to the database.

Every find day was in `reports/method_outside_tuscany_find_days.csv`, with
the v5 columns next to the v4 ones in `reports/method_outside_tuscany_days.csv`
and `reports/method_outside_tuscany_months.csv`. The #172 rerun replaced the
v4 columns. The files of this report are those of commit d896d82.

## AC2 and AC3: scores on the days of nearby finds

Median estimate of the forest squares on the day of each find in the 12
months.

| Place | Finds | Higher with v5 | Same | Lower | v4 on the find days | v5 on the find days |
|---|---:|---:|---:|---:|---|---|
| Val di Fiemme | 28 | 17 | 11 | 0 | 35 to 94 | 47 to 95 |
| Campo dei Fiori | 6 | 5 | 1 | 0 | 37 to 88 | 67 to 94 |
| Valle Gesso | 3 | 3 | 0 | 0 | 11 to 16 | 68 to 75 |
| Borgotaro | 4 | 4 | 0 | 0 | 12 to 36 | 18 to 63 |
| Pescasseroli | 0 | | | | | |
| Pollino | 0 | | | | | |
| Sila | 10 | 10 | 0 | 0 | 5 to 43 | 22 to 56 |
| Nebrodi | 3 | 3 | 0 | 0 | 8 to 65 | 70 to 81 |
| Etna north | 3 | 2 | 1 | 0 | 24 to 93 | 72 to 93 |
| Gennargentu | 0 | | | | | |
| Pistoia | 4 | 3 | 1 | 0 | 20 to 89 | 46 to 89 |

The Sila find days in detail:

| Date | v4 | v5 |
|---|---:|---:|
| 17 October 2025 | 19 | 37 |
| 24 October 2025 | 43 | 56 |
| 5 November 2025 | 42 | 51 |
| 13 November 2025 | 19 | 22 |
| 21 June 2026 | 5 | 28 |
| 5 July 2026, 2 finds | 9 | 50 |
| 18 July 2026 | 5 | 27 |
| 5 October 2026, 2 finds | 5 | 44 |

The summer finds at Sila still score below 50 on two of four days. The cell
recorded little rain, so a store fed by that rain can only partly fill.

## AC4 and AC5: months out of season

The sources of the method describe fruiting from spring to the first frosts
and *B. aereus* into December. None describes January or February. For this
check the owner set December to February as the months to watch. March counts
as possible spring, because spring 2026 came early at Pistoia.

From December 2025 to February 2026, across 11 places and 33 months:

- 32 months have the same number of days at 30 or more and at 50 or more.
- Etna north, February: 1 day at 50 or more instead of none. Its days at 30
  or more stay at 18.
- The largest rise of a monthly mean is 1.5 points, Etna north in December.

Val di Fiemme stays at 5.0 to 5.1 from December to February with both
versions. April rises from 7.4 to 9.0 on average, with no day at 30 or more.
Pistoia keeps the same counts from December to February: 26, 6 and 21 days at
30 or more, and 6, 0 and 2 at 50 or more.

March rises with v5, because evapotranspiration there is above 1 mm a day:

| Place | Days at 30 or more, v4 and v5 | Days at 50 or more, v4 and v5 |
|---|---|---|
| Pistoia | 19 and 24 | 6 and 13 |
| Campo dei Fiori | 16 and 24 | 0 and 0 |
| Etna north | 15 and 16 | 0 and 3 |
| Nebrodi | 5 and 7 | 0 and 0 |
| Borgotaro | 4 and 6 | 0 and 0 |
| Pollino | 0 and 2 | 0 and 0 |

The Pistoia March days at 50 or more had daily means of 9.4 to 11 deg C. v4
already scored them 42 to 61. The temperature rule gives 8 to 10 deg C days
a factor of 0.4 to 0.7, which the #156 report flagged as the second rule to
change. Issue #172 tested that change, see `reports/mild_winters_cold_rule.md`.

Pistoia rises in season too. April goes from 0 to 11 days at 50 or more,
June from 7 to 17 and September from 8 to 18. There are 4 finds near Pistoia
in the 12 months, too few to say whether these rises are right.

## AC6: where the check cannot decide

- Pescasseroli has no find nearby in any year. Pollino has 3 and the
  Gennargentu 1, none of them in the 12 months. At these three places the
  check shows only how v5 moves the estimate, not whether it is right.
  Pescasseroli rises from no day at 50 or more to 28 days in June 2026.
- Nebrodi, Etna north and Pistoia have 3 or 4 finds each in the 12 months.
  One summer find at Nebrodi and one at Etna north moved from 8 and 24 to 70
  and 72. These are single days.

## AC8

The change from the source passes the checks of AC4 and AC5 as set above, so
the method changes to v5.

## Limits

- One year of weather. The Sila cell recorded a dry summer in 2026, and a
  wetter year could give a different picture.
- The source measured German woods in a wet period, in boxes without living
  roots. Tree roots also dry the top soil, so the real drying in a southern
  summer is likely faster than 0.5 mm a day.
- The 95 percent kept each day, the 40 mm full level and the other store
  numbers are unchanged project choices.
- Weather cells are about 9 km wide and miss local storms. v5 cannot count
  rain that the cell did not record.
- Finds within 30 km cover many altitudes, and 405 finds have a blurred
  position.
