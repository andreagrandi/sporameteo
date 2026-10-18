# National weather and estimate refresh assessment

Assessed on 13 October 2026 using the completed local forest build and the
current weather request planner. National counts are read-only measurements.
No national weather fetch or national calculation was run for this assessment.
Local testing remains restricted to Tuscany.

## Recommendation

Fetch weather for every eligible Italian weather cell each night, calculate
all forest-square estimates in the background, and publish a completed version
once. Keep the previous published version available during the run and after
a failed run. A browser keeps its displayed version until a complete replacement
is ready. Do not redraw the overlay as individual weather batches finish.

This is a proposed replacement for the on-demand decision D15. It does not
change D16's weather detail or D17's minimum forest coverage. A national
scheduler and publication switch have not been implemented by this assessment.
The Tuscany implementation is tracked in [epic #96](https://github.com/andreagrandi/sporameteo/issues/96).

## Coverage and provider accounting

The completed build `italy-forest-copernicus-2018-v1` contains:

| Measurement | Italy | Tuscany |
| --- | ---: | ---: |
| Eligible forest squares | 717,104 | 71,031 |
| Distinct weather cells used by those squares | 3,726 | 333 |
| Groups of at most 100 weather cells | 38 | 4 |

Tuscany uses forest squares whose owner region has ISTAT code `09`. Weather
cells can cross a regional border. Count each shared weather cell once when
calculating national usage. Do not sum regional weather-cell counts.

The current planner requests five hourly variables and one model selection.
It uses 30 completed days of history and today plus seven future dates. It
reuses completed historical days, but replaces yesterday's forecast with
completed weather. It does not request weather separately for every 500 m square.

The forest, terrain and host-tree datasets are prepared separately. They are
reused during nightly calculation and do not need a nightly national download.

Open-Meteo's free service permits 600 counted calls per minute, 5,000 per hour,
10,000 per day and 300,000 per month for non-commercial use. Multiple locations
consume multiple counted calls even when sent in one HTTP request. Longer
periods and more variables can also increase the charge against the allowance.
See [Open-Meteo pricing](https://open-meteo.com/en/pricing) and the
[provider's explanation of multiple locations](https://github.com/open-meteo/open-meteo/issues/1295).

Our reservation limits are deliberately lower: 540 per rolling minute,
4,500 per rolling hour, 9,000 per rolling day and 270,000 per rolling 31 days.
Every retry also consumes a reservation. Batching does not evade these limits.

The figures below come from `plan_current_weather_requests`, including its
conservative treatment of the closing Italian midnight. The same counts were
checked for 13 October, 26 October and 30 March 2026 to include both clock changes.

| Work | Counted calls per cell | Italy calls | Italy HTTP requests | Tuscany calls | Tuscany HTTP requests |
| --- | ---: | ---: | ---: | ---: | ---: |
| Initial load | 4 | 14,904 | 152 | 1,332 | 16 |
| One nightly refresh | 2 | 7,452 | 76 | 666 | 8 |
| 31 nightly refreshes | 62 | 231,012 | 2,356 | 20,646 | 248 |

The initial load makes three history requests per cell, covering 13, 13 and
4 days, plus one eight-day forecast request. A normal nightly refresh makes
one request for yesterday and one for the new eight-day forecast. These are
counts for compatible, complete batches without retries or previously missing
history. Actual HTTP request counts can increase when cells have different gaps.

Italy's normal refresh leaves 1,548 counted calls under our daily limit and
38,988 under our rolling monthly limit. A 20 percent retry allowance every night
would fit the daily limit but exceed the monthly limit. Reserve 10 percent for
routine retries instead: 8,197.2 calls per night on average and 254,113.2 over
31 nights. Quota reservations, not these averages, decide whether each retry runs.
A complete second national pass on the same day does not fit.

The provider charge is zero while the project remains eligible for the free
service and stays within these limits. Hosting, storage and backups are separate.
No new hosting price or commercial subscription is assumed here.

## Batches and initial load

Start one request of at most 100 locations every 90 seconds, with only one
provider request in progress. This spends at most 4,000 counted calls per hour
for the current request parts and keeps well below the minute allowance.
The 76 nightly requests occupy about 114 minutes before retries and unusually
slow responses. This is a proposed pacing budget, not a measured completion time.
The durable quota checker must remain authoritative across all workers and endpoints.

Use a night window such as 02:00 to 05:00 Europe/Rome, then measure whether the
last completed day is available at that time in Tuscany. The provider documents
ECMWF IFS updates every six hours without the five-day delay of ERA5. Do not
replace the configured history source or reuse yesterday's forecast merely to
meet a publication deadline. See the
[historical data schedule](https://open-meteo.com/en/docs/historical-weather-api).

The initial national load cannot fit in one daily allowance. With no intervening
refresh it needs at least two days, but early groups then become older while the
remaining groups load. A conservative four-night rollout that also refreshes
every group already loaded is:

| Night | New weather cells | Existing cells refreshed | Counted calls |
| --- | ---: | ---: | ---: |
| 1 | 2,000 | 0 | 8,000 |
| 2 | 1,000 | 2,000 | 8,000 |
| 3 | 500 | 3,000 | 8,000 |
| 4 | 226 | 3,500 | 7,904 |

That rollout spends 31,904 calls. Another 27 complete refreshes bring the first
31-night period to 233,108 calls, below our 270,000 limit before retries.
At 90-second pacing the busiest rollout nights need about two hours.
Respect rolling 24-hour windows when starting the next night, including clock
changes. Any unrelated provider work shares the same allowance.

For local testing, load and refresh only the 333 Tuscany weather cells. Keep
national processing disabled. Do not use the existing visitor limit of 100 new
cells per minute as the scheduled-work budget. The scheduler must use the shared
provider quota reservations and its explicit region scope.

## Estimate calculation and publication

One full assessment writes 717,104 result rows, containing eight daily values
each, or 5,736,832 values. Fetching weather once per weather cell allows all of
its forest squares to share those inputs.

The existing [national benchmark](estimate_updates.md#measured-national-run)
calculated and stored two assessments in 710.3 seconds on an M2 Pro. The stored
estimate tables occupied 131.2 MiB for both assessments. That benchmark used
synthetic weather and method v1. It is evidence that precomputation is practical,
but it is not a measured runtime or storage estimate for the current v4 method
or for a hosted server.

A read-only check of v4 on 13 October used real stored weather for three cells
whose forest squares all belong to Tuscany. It fetched no provider data and
wrote no estimates:

| Weather cell centre | Forest squares | Calculation time |
| --- | ---: | ---: |
| 43.85, 11.05 | 46 | 0.154 seconds |
| 43.65, 11.15 | 298 | 0.610 seconds |
| 43.95, 10.55 | 361 | 0.637 seconds |

The sample calculated eight dates for 705 squares in 1.401 seconds. Scaling
that rate by square count gives about 2.4 minutes for Tuscany and 24 minutes
for Italy, excluding writes. This is a rough projection from three Tuscany
cells with stored habitat, not a national or hosted-server benchmark. Allow a
provisional 30 to 60 minutes for calculation, writes and validation after weather
loading, then verify that allowance with the complete Tuscany run. The proposed
two-hour fetch window plus that allowance fits a three-hour night window when
provider data is available. Do not run a new national benchmark locally.

The current service publishes completed weather cells independently. That is
not a complete national publication boundary. A scheduled implementation needs
a run record that lists the expected cells and their finished estimate updates.
Map, overview and area APIs must read from one published run. Switch the published
run only after validation. If required work fails, keep the previous run and its
actual assessment date. Known source gaps must remain explicit exceptions.

The map should retain its existing overlay while a replacement loads, then
replace the data once. An active session can keep its published version until
the next deliberate refresh. It must not display a sequence of partially
completed national runs or clear the overlay between finished batches.

## Existing documentation that must not drive the new budget

`weather_request_budget` and `weather_updates.md` now describe the 38-day
window: three history pieces of 13, 13 and 4 days plus one forecast of 8 days
for the first load, and yesterday plus one forecast of 8 days for a normal
night. The budget report lists counted calls and HTTP requests as separate
numbers. The nightly refresh command `refresh_weather` follows the same
plan.

## Immediate database changes

The stored-weather read now loads current snapshots, completed fetch identities
and known gaps in three queries regardless of the number of weather cells.
Incomplete fetches and unfinished callbacks still use the existing planning and
recovery path. A cached map response no longer loads the individual estimate rows.

The measured Tuscany view contained 5,114 squares and 24 current weather cells.
Its repeated request fell from 246 queries to six. Under the same Python profiler,
elapsed time fell from 0.773 to 0.486 seconds. The first response in a new process
was 1.273 seconds before and 1.220 seconds after, so this change alone does not
resolve first-view latency. Neither measurement made a provider request.

The focused weather and map suites passed 68 tests. A further check verified
that a cached response does not query the estimate-result table. These changes
do not install a scheduler or remove the existing on-demand wait.
