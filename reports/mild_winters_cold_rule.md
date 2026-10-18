# A cold rule for mild, wet winters

Issue #172.

## Question

The #156 check found that mild, wet winters on Etna and in the Gennargentu
reach 30 or more on many days from December to March, when nobody reports
porcini. Can a rule taken from a published source keep those winters low,
without lowering the days when people found porcini?

## Answer

No. The rule tested here keeps every winter low, but it also cuts the autumn
finds in the southern mountains and the late finds in the Alps. It fails AC4,
so the method stays at v5, as AC8 requires. The app does not change.

- From December to February, no place has a day at 30 or more with the new
  rule. Etna north had 8 to 18 such days a month with v5 and the Gennargentu
  16 to 23.
- 24 of the 61 finds of the 12 months score lower, 17 of them by more than 5
  points. Sila on 24 October 2025 drops from 56 to 11. Val di Fiemme on 30
  September 2026 drops from 47 to 10.
- October and November 2025 lose every day at 50 or more at Sila, Pollino and
  Nebrodi. October is the month with the most finds in all years at Sila and
  Nebrodi.

Run: `reports/runs/method_outside_tuscany/20261018T131731Z-b1f3bf.json`.
Script: `experiments/method_outside_tuscany/run.py`.

## The rule and its source

The rule multiplies the v5 temperature factor by a second factor for the mean
temperature of the 20 days before the target day, after the altitude
correction of each square:

```text
t20 = mean daily temperature of the 20 days before the target day, at the square
cold = exp(-0.10455 * (t20 - 13.7)^2) when t20 is below 13.7 deg C, otherwise 1
temperature factor = v5 temperature factor * cold
```

The numbers come from the Bielefeld *B. edulis* preprint, table S1, the same
row that method v2 used. The study counted porcini in one German beech forest
from 2015 to 2024. It found that fruiting was "largely absent" at 20-day means
of 5 to 10 deg C. The curve gives 0.74 at 12 deg C, 0.24 at 10 deg C, 0.03 at
8 deg C and almost 0 at 5 deg C.

The owner's document named in decision D21 could not be found in the
repository or online, so it gave no rule. No find set any number. The rule was
chosen before the rerun and was not changed after it.

## How the check was done

The script and places are those of #171: 10 places outside Tuscany and the
Pistoia home area of D11. It calculated v5 and the cold rule for every forest
square and every day from 17 October 2025 to 16 October 2026, as an estimate
for the same day. The v5 results match the #171 run.

The weather came from the files saved by #156 and #171. No Open-Meteo, GBIF
or iNaturalist request was made and nothing was written to the database. The
cold rule lives only in the script. The app keeps v5.

Every find day is in `reports/method_outside_tuscany_find_days.csv`, with the
cold factor of the day. The `_v5` and `_cold` columns of
`reports/method_outside_tuscany_days.csv` and
`reports/method_outside_tuscany_months.csv` hold the two methods side by side.

## AC3: December to February

Days at 30 or more, v5 and the cold rule:

| Place | December | January | February |
|---|---|---|---|
| Etna north | 8 and 0 | 8 and 0 | 18 and 0 |
| Gennargentu | 23 and 0 | 16 and 0 | 23 and 0 |
| Pistoia | 26 and 0 | 6 and 0 | 21 and 0 |
| Campo dei Fiori | 0 and 0 | 0 and 0 | 4 and 0 |
| Borgotaro | 0 and 0 | 0 and 0 | 4 and 0 |

The other six places had no day at 30 or more in these months with either
method. March also drops to no day at 30 or more at every place. Pistoia
loses 24 such days, the Gennargentu 24 and Campo dei Fiori 24.

## AC4: find days that drop

Median estimate of the forest squares on the day of each find. The table
lists every find that drops by more than 5 points. Two finds on the same day
appear once, with their count.

| Place | Date | Finds | v5 | Cold rule | Cold factor |
|---|---|---:|---:|---:|---:|
| Sila | 24 October 2025 | 1 | 56 | 11 | 0.12 |
| Sila | 5 November 2025 | 1 | 51 | 13 | 0.18 |
| Val di Fiemme | 30 September 2026 | 2 | 47 | 10 | 0.11 |
| Nebrodi | 28 October 2025 | 1 | 81 | 45 | 0.52 |
| Nebrodi | 2 November 2025 | 1 | 74 | 39 | 0.49 |
| Val di Fiemme | 20 September 2026 | 1 | 66 | 37 | 0.53 |
| Sila | 17 October 2025 | 1 | 37 | 9 | 0.13 |
| Sila | 13 November 2025 | 1 | 22 | 6 | 0.06 |
| Borgotaro | 2 April 2026 | 1 | 18 | 5 | 0.00 |
| Val di Fiemme | 12 September 2026 | 1 | 81 | 71 | 0.86 |
| Val di Fiemme | 6 September 2026 | 2 | 93 | 85 | 0.91 |
| Val di Fiemme | 7 September 2026 | 2 | 93 | 86 | 0.92 |
| Val di Fiemme | 8 September 2026 | 2 | 93 | 86 | 0.92 |

Seven more finds drop by less than 4 points: Sila on 5 October 2026, 2
finds, Val di Fiemme on 2 September 2026, 2 finds, and on 26 July and 8
August, and Etna north on 28 October 2025. The other 37 finds keep the same
score. No find scores higher.

The four autumn 2025 finds at Sila have cold factors of 0.06 to 0.18. The
curve gives these factors to 20-day means of about 8.5 to 9.7 deg C at the
squares.

## AC5: the Alps and Pistoia from April to November

A frozen Alpine January still scores 5: Val di Fiemme has a mean of 5.0 in
January 2026 with both methods. Val di Fiemme still reaches 50 or more on
every day of July and August. September drops from 25 to 17 such days.

Months from April to November that lose days at 50 or more:

| Place | Month | v5 | Cold rule |
|---|---|---:|---:|
| Val di Fiemme | May 2026 | 6 | 0 |
| Val di Fiemme | June 2026 | 26 | 7 |
| Val di Fiemme | September 2026 | 25 | 17 |
| Pistoia | November 2025 | 22 | 19 |
| Pistoia | April 2026 | 11 | 10 |

Other places lose more. October and November 2025 go from 15 and 15 days at
50 or more to none at Nebrodi, from 15 and 7 to none at Pollino and from 8
and 5 to none at Sila. April 2026 loses all its days at 50 or more at
Nebrodi, Pollino, Sila and Valle Gesso.

## AC6: where the check cannot decide

- Pescasseroli has no find nearby in any year. Pollino has 3 and the
  Gennargentu 1, none in the 12 months. At these places the check shows only
  how the rule moves the estimate. The Gennargentu winter result rests on the
  sources alone.
- Nebrodi and Etna north have 3 finds each in the 12 months. The Nebrodi
  drops come from 2 finds.
- The Sila and Val di Fiemme drops are clearer. Sila has 39 finds in all
  years, 21 of them in September and October. Val di Fiemme has 257.

## AC8: the method stays at v5

The rule from the source fails AC4. Find days drop by up to 45 points, and
whole autumn months lose their high days in the southern mountains. The
method stays at v5 and `docs/forecast_method.md` records the rule as tested
and not adopted.

The cold rule cannot tell a mild winter from a cool autumn, because both have
20-day means of 8 to 11 deg C. A rule that separates them needs a source that
describes how porcini respond to the change of season, for example cooling
after a warm summer against warming after a cold winter. The project has no
such source yet.

## Limits

- One year of weather and one weather cell per place.
- The Bielefeld curve comes from one German beech forest and one species.
  Southern oak and chestnut woods with *B. aereus* may fruit in cooler
  conditions than the curve allows.
- Finds within 30 km cover many altitudes, and 405 finds have a blurred
  position.
- The weather cells are about 9 km wide.
