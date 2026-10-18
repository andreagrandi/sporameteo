# Weather updates

The application stores weather by 0.1 degree weather cell and Italian assessment date. The assessment date uses `Europe/Rome`, so the date changes at Rome midnight on both daylight saving clock-change days. Weather is prepared ahead by a scheduled run, described below. The map, overview and details read the published Tuscany run and never start a provider request, as described in `docs/api.md`.

The window of a snapshot is 38 dates: the 30 completed Italian days before the assessment date, the assessment date and the following seven dates. Completed days come from the historical source, `SPORAMETEO_WEATHER_HISTORY_ENDPOINT`, which defaults to the Open-Meteo archive. Today and the next seven days come from the forecast source. The planner asks for completed days in pieces of at most 13 days, so a full first load is pieces of 13, 13 and 4 days plus one forecast of 8 days. A normal night asks for yesterday plus one forecast of 8 days.

Completed days are stored once and read from the database before any request is planned. A stored day counts as history only when it came from the historical source, has the status complete or checked known missing, and carries the current `history_version` in its metadata. If several rows exist for one date, the oldest one is used. A forecast row for a date that has passed is never history, so the next night fetches that day from the historical source. The reused rows are attached to the new fetch as completed parts with the reason `stored_history` and no request, and the snapshot is built from them. A day that was reused needs no provider request after a restart.

`WEATHER_HISTORY_VERSION` in `src/sporameteo/weather/policy.py` is the explicit correction switch. Raising it stops the reuse of older rows. The next run fetches the history again and stores new rows. The older rows and the snapshots built from them stay in the database. Cleanup never deletes rows from the historical source.

The new snapshot is written only after every expected date is present or has a checked known gap. A known gap remains a ready snapshot with a null value and its reason. A refused or interrupted request remains pending and never becomes a known gap, and it stores no rows.

Compatible cells share one request. Each request has at most 100 cells and carries its complete date, variable, model, time zone and provider option identity. The #44 quota reservation runs before every network attempt, including a retry. A 429 response keeps the full provider message, the retry time and the counted reservation. A request that completed at Open-Meteo but failed before daily storage may be fetched again only after the service confirms that no ready snapshot exists.

The on-demand service `request_weather` remains in `weather_on_demand/services.py`, but no page request calls it since #100. A caller waits for up to three seconds using a monotonic deadline while the worker continues the durable task. A slow or pending fetch returns the last ready snapshot with its real assessment date, fetch time, source and snapshot ID. A cell with no ready snapshot returns `not_available_yet` with a stable reason code. The service stores only a SHA-256 visitor hash. One hash may start at most 100 newly triggered cell and assessment-date fetches in a rolling 60-second window. Current snapshots, previous readable snapshots and already pending cells do not consume that allowance. A stale cell that needs a new assessment does consume one allowance.

The queue worker owns provider calls. It claims a durable batch under sorted logical advisory locks and stores each successful date part before assembling the member snapshot. A stopped worker moves an abandoned running batch to pending, keeps its #44 quota reservation, and queues it after two minutes or the provider retry time. The weather task uses `run_after` for delayed retries. A permanent provider refusal stays failed until an explicit reopen. The `configure_estimate_callback` hook runs after the ready snapshot transaction commits. It receives `snapshot_id`, `cell_key`, `assessment_date` and `forest_build_version` as keyword values and is optional, so a weather fetch does not import the estimate code. A callback failure is retried from the ready snapshot without another provider request. The `configure_cleanup_callback` adapter receives `cleanup_date` as a keyword value and returns snapshot IDs still protected by estimate updates before weather snapshots are pruned.

Cleanup makes no provider requests. Keep the newest two snapshots per cell and any snapshot IDs supplied by the later estimate service. Delete only unreferenced daily rows older than 30 days before the cleanup date, and never a row from the historical source. A current or previous snapshot reference protects all of its daily rows even when that snapshot is older. Keep #44 request attempts for 31 days, then remove older attempts. The date-keyed cleanup task is idempotent and can be queued with:

```sh
uv run python apps/application/manage.py cleanup_weather
```


The status command reports fetches by assessment date, weighted provider calls
and a separate attempt count. It also reports refused requests with their full
messages, pending cells and the last cleanup:

```sh
uv run python apps/application/manage.py weather_on_demand_status
```

## Scheduled weather refresh

`refresh_tuscany_weather` prepares the 38-day window for every weather cell that holds a forest square of the nightly regions. The setting `SPORAMETEO_NIGHTLY_REGIONS` names them. It is `all` or a list of ISTAT region codes such as `09,08`, and it defaults to `09`, Tuscany. `--region-code` overrides it with the same format, and an unknown code stops the command with an error. A weather cell shared by several forest squares, or by two regions on a border, is counted and stored once. One `ScheduledWeatherRun` covers the whole set. Its `region_code` is `all`, or the sorted codes joined by commas, so a single region keeps the key `09`. The forest build comes from `SPORAMETEO_FOREST_BUILD_VERSION` unless `--forest-build-version` is given. The assessment date is today in Europe/Rome unless `--assessment-date` is given.

Print the plan first. The dry run reads the database only. It writes nothing, makes no network call and prints the mode, the number of cells, the cells that are already ready for the date, the MB the run would read from the files, and the API calls left. The API calls left are the counted calls and HTTP requests of the open fallback batches of the run for that date, so they are 0 when no run or no fallback batch exists:

```sh
uv run python apps/application/manage.py refresh_tuscany_weather --dry-run
```

Start the run without `--dry-run`. The command stores a `ScheduledWeatherRun` for the region and date, queues one task and prints the run ID and status. It plans no API batches:

```sh
uv run python apps/application/manage.py refresh_tuscany_weather
```

The run is in `initial` mode while no cell of the run has a ready snapshot for an earlier date, and in `refresh` mode afterwards.

The worker task reads the Open-Meteo files once for all 38 days of every cell, as described in the next section. The nightly run makes no API call in its normal path. The run keeps the read in `file_read`.

When the read fails, the worker falls back to the API for the whole night. A read fails when a file cannot be reached or when the estimate is over 1 GB. The run then plans the full API run with `models=ecmwf_ifs`, so the fallback uses the same model as the files. It stores `file_read_failed` as the reason and the error text as the full reason, and it keeps both after the fallback run completes, as the record of why the night used the API. The fallback costs 1,332 counted calls for 333 cells in `initial` mode: three history pieces of 13, 13 and 4 days and one forecast of 8 days, which is 4 calls per cell. A normal night costs 666 counted calls: yesterday and one forecast of 8 days, which is 2 calls per cell. Counted calls and HTTP requests are different numbers. With 333 cells the initial load sends 16 HTTP requests and a normal night sends 8, because each piece needs one request per 100 cells.

On the fallback path the queued task takes one batch, runs it, and then queues itself again. It starts the next provider request at least 90 seconds after the previous start, so only one request of the run is in progress. The shared quota reservation still runs before every attempt, with limits of 540 per minute, 4,500 per hour, 9,000 per day and 270,000 per 31 days. When a reservation is refused or the provider asks for a delay, the whole run waits until the retry time. A restart does not repeat completed parts. Running the command again for the same date resumes the same run and creates no second run, batch or request. The worker recovery command also queues a task again for an unfinished run.

A run ends as `completed` when every weather cell of the run has a snapshot for the date. It ends as `failed` with a reason otherwise:

- When the files lack hours for a cell, the cell gets no snapshot and the run fails with the reason `missing_hours`. The full reason gives the number of cells without a snapshot and the first cell with its dates. The run does not use the API for that cell.
- When a fallback batch is refused for good, the run fails with the reason of the provider.

A failed run never deletes or replaces stored rows or snapshots, so the last published estimates stay. Running the command again for a failed run reads the files again when the run has no fallback batch, and only the cells without a snapshot are read. For a failed fallback run, `reopen_weather_fetch` reopens the failed batches first, and the command resumes the run. The run record keeps the planned counted calls and HTTP requests of the fallback, the number of provider attempts and the start and end times. The database size can be read with `pg_database_size` before and after a run.

## Weather from the Open-Meteo files

`store_file_weather` stores the same 38 days of daily rows as the Tuscany refresh, but it reads the public Open-Meteo files in the `openmeteo.s3.amazonaws.com` bucket instead of calling the API. The nightly run uses it as its normal path. The command `store_file_weather` takes the same arguments as `refresh_tuscany_weather`: `--dry-run`, `--assessment-date`, `--region-code`, `--forest-build-version` and `--json`.

The dry run reads the database only. It makes no network call and writes nothing. It prints the number of weather cells, the cells that already have a snapshot for the date, the days to read, the number of chunk files and the estimated download in MB:

```sh
uv run python apps/application/manage.py store_file_weather --dry-run
```

A real read refuses to start when the estimate is over 1 GB. Otherwise it reads the files once for all cells that still need days:

```sh
uv run python apps/application/manage.py store_file_weather
```

The read works in these steps:

- A cell that has a ready snapshot for the date is skipped.
- A day before the assessment date is reused when a stored file row for it exists, was never a forecast and has the current history version. The oldest row wins.
- A day from the assessment date on is reused only when its row came from the newest forecast run in the files. A row that was a forecast is never reused as a past day. The next night the same date is read again as a past day.
- The Italian daily values come from the same aggregation as the API data. A day with any missing hour gets no row. The cell then gets no snapshot, and the read record lists the cell with the reason `missing_hours` and the dates.
- A cell with all 38 days gets a snapshot. The read is done in one pass, so a first load stores 38 days per cell and a later night stores about 9 days per cell.

Each row is named `open-meteo-files` with the model `ecmwf_ifs`. Its metadata holds only that cell: the grid point, the grid height, the height correction, the read ID, the run time and the modification time of the chunk files that hold the day. Days from the assessment date on carry the run time as the forecast issue time. The row has no `response` entry. Rows that the API fallback stores from now on have no `response` entry either, and older rows keep theirs.

Every read leaves a `FileWeatherRead` record with the assessment date, the run time of the files, the start and end times, the bytes and requests used, the cell counts, the missing cells and the modification time of every chunk file. When a file cannot be read, the read ends as `failed` with the error text on the record and no row is written.

## Nightly chain

One cron entry starts the whole night. `refresh_tuscany_weather` runs at 00:30 UTC. The steps follow each other without more cron entries:

1. The command creates the weather run and queues its task. The worker reads the files for all 38 days of every weather cell of the nightly regions, once for the whole set. When the read fails, it falls back to the API with `models=ecmwf_ifs` and records why.
2. When the run reaches `completed`, the worker queues one forecast task for the same date. The run stores the task ID in `forecast_task_id`.
3. The forecast task goes through the regions one after another. Each region gets its own forecast run, which is calculated and published on its own. A region that already has a published run for the date is skipped, so a retry only redoes what is missing.
4. Publishing the run of Tuscany queues the map cache warm-up. The map serves only Tuscany until issue #155, so other regions queue no warm-up.

00:30 UTC is 01:30 in Rome in winter time and 02:30 in summer time, so it is always after midnight in Rome. It is also clear of the book corners jobs at 02:00 and 03:00 UTC. `cleanup_weather` keeps its own entry at 01:30 UTC. Both entries are in the `cron` list of `app.json`, and the VPS clock is UTC. The checks after the chain are described in the next section.

A failed weather run queues no forecast. A failed region keeps its last published run and queues no warm-up, and the other regions still publish. The forecast task records the failure of each region, goes on with the next one, and raises at the end with the failed regions and their reasons, so its task row is marked failed. `run_tuscany_forecast --region-code <code>` for the same date runs one region again by hand. Running `refresh_tuscany_weather` again for the same date resumes the same run and queues no second forecast. If the forecast could not be queued before, for example because the queue was down, the next run of the command queues it and the run keeps the reason `forecast_queue_failure` until then.

The background worker must be running, for example `run_background_worker`, because the command only creates and queues the run.

## Nightly check and failure email

`/health/` does not look at how old the data is, so a separate command checks the night. `check_nightly_update` looks for a published forecast run for today's Rome date in every nightly region. The regions come from `SPORAMETEO_NIGHTLY_REGIONS`, and `--region-code` overrides them. The weather run is found by its run key. The night counts as published only when every region has a published run. It runs three times from the `cron` list of `app.json`:

| UTC time | Command | What it does |
|---|---|---|
| 02:30 | `check_nightly_update --retry` | First retry, no email |
| 03:15 | `check_nightly_update --retry` | Second retry, no email |
| 04:00 | `check_nightly_update` | Email the owner if the run is still not published |

04:00 UTC is 05:00 in Rome in winter time and 06:00 in summer time. The retries avoid the book corners jobs at 02:00 and 03:00 UTC. A normal night reads the files and sends no provider request, so the weather run ends within a few minutes. On the API fallback a normal night sends 8 provider requests at least 90 seconds apart, so the weather run takes about 12 minutes before the forecast starts.

A retry does nothing when the run is published or a step is still working. Otherwise it repeats the step that stopped:

- No weather run for the date: it starts `refresh_tuscany_weather` for the date. This happens when cron or the worker was down at 00:30.
- Weather run pending or running: it calls the same start again, which queues the task again only if the task was lost.
- Weather run failed: it reopens the failed batches of the run, if any, and resumes the run. A run without fallback batches reads the files again. Batches that already finished keep their snapshots and cost no new calls.
- Forecast never queued, or forecast task failed: it queues a new forecast task and stores its ID in `forecast_task_id`. The task skips the regions that are published and reruns only the missing ones.

The provider already retries refusals with status 429, 500, 502, 503 and 504 on its own, with the delay it asks for. The retry passes cover what stops for good: other refusals, storage failures, a forecast that raised, and a worker or cron that was down.

The 04:00 check sends one email when the run is not published. The subject names the date and the reason, which is one of:

- `no_weather_run`: no weather run started, which usually means the worker or cron is down.
- `weather_failed`: the weather run failed. The email holds the full reason of the run, such as the refusal message of the provider or the cell that lacks hours in the files.
- `forecast_failed`: the forecast failed or could not be queued. The email holds the forecast error. With several regions it names the regions that are not published, with the failure reason of each.
- `not_finished`: a step was still pending or running at 04:00.

Run the check by hand for another date with `--assessment-date`:

```sh
uv run python apps/application/manage.py check_nightly_update --retry --assessment-date 2026-10-15
uv run python apps/application/manage.py check_nightly_update --assessment-date 2026-10-15
```

### Email settings

The email goes through Resend with `django-anymail`, as in book corners. Set the key and the recipient on the VPS:

```sh
sudo dokku config:set sporameteo \
  RESEND_API_KEY="<resend-api-key>" \
  NIGHTLY_ALERT_EMAIL="<owner-email-address>" \
  DEFAULT_FROM_EMAIL="SporaMeteo <noreply@sporameteo.com>"
```

Without `RESEND_API_KEY` or `NIGHTLY_ALERT_EMAIL`, the command writes the message to the log at error level and exits normally.

On 15 October 2026 the Resend pricing page listed 3 custom domains, 100 emails a day and 3,000 emails a month on the free plan. One check sends at most one email a day. The sender domain is `sporameteo.com`, to be verified in the same Resend account as book corners. Until the domain is verified, set `DEFAULT_FROM_EMAIL` to an address on the book corners domain.

## Rebuild from the files in production

The file weather of #129 was deployed on 16 October 2026 at 16:42 UTC. The
night of 17 October 2026 was the first one on the file path, and it rebuilt
the production weather from the files. `WEATHER_HISTORY_VERSION` stayed at 1.
The file read reuses only rows from the files, so no older API row could enter
a snapshot.

The weather run `124b8876-f68e-4cc5-bf18-4661446963a0` started at 00:30 UTC
and completed at 00:31 UTC in `refresh` mode. It made 0 provider attempts and
planned 0 API calls. Its file read `a7b44f2f-73f5-437a-add1-8583aabf9d2e` used
the 12:00 UTC run of 16 October, read 31.2 MB in 1,036 requests and wrote
snapshots for all 333 cells with no missing hours. The 333 snapshots hold
12,654 daily rows, 38 per cell, and every row has the file source name. No API
request attempt was stored after the deploy.

The forecast run `ea997a59-e7ad-4074-9de0-1d0891af097a` was published at
00:37 UTC with 333 of 333 cells. The previous run,
`d8fd4377-57ee-40db-ad95-fecb1bcd45e1` for 16 October, used API weather.

No counts were taken before the deploy. After the rebuild the database held
5,328 rows from the API forecast, 10,323 rows from the API archive and 12,654
rows from the files. It also held 999 snapshots, 2 forecast runs and 355,155
estimate results. The API rows stay in the database as decision D15 requires.

## First night for all regions in production

The code of #154 was deployed on 18 October 2026 at 09:33 UTC, with the
migrations `weather_on_demand.0008` and `weather_summaries.0004`. The app then
switched to `SPORAMETEO_FOREST_BUILD_VERSION=regions-all-forest-italy-forest-copernicus-2018-v1`,
`SPORAMETEO_TERRAIN_BUILD_VERSION=regions-all-terrain-italy-terrain-glo30-2021-v2`
and `SPORAMETEO_NIGHTLY_REGIONS=all`. To go back to Tuscany, set the two
`tuscany-...-56` build versions and `SPORAMETEO_NIGHTLY_REGIONS=09` again.

The dry run for 18 October listed 3,726 weather cells, 333 of them already
ready from the Tuscany night, 288.3 MB to read and 0 API calls. The run was
then started by hand with `--assessment-date 2026-10-18`, so it was a first
load of 38 days for 3,393 cells. Tuscany was skipped, because its cells and
its forecast run of that date were already ready.

| Step | VPS time | Peak memory of the worker |
|---|---|---|
| Weather read, 154.4 MB in 4,817 requests | 11.5 min, 09:38:55 to 09:50:28 UTC | 260 MB |
| Estimates of 19 regions in one task | 10.5 min, 09:50:28 to 10:00:57 UTC | 183 MB |
| Map cache warm-up of Tuscany, by hand | 6 s | 111 MB |

The memory figures are the highest `memory.current` of the worker container,
read every 2 seconds. The worker limit is 384 MB. The `peak_memory_mb` of
314.8 MB on the forecast runs is the peak of the whole worker process, the
weather read included. The weather run `79793efe-e471-42b9-b806-ba95ec618af6`
made 0 provider attempts, and no cell lacked hours. All 20 regions have a
published run for 18 October. The regions took 6 to 54 seconds each.
`reports/italy_night_cost.md` expected about 13 minutes and 302 MB for the
first-load read and about 5.8 minutes for the estimates. The 19 runs record
9.4 minutes of runtime in all, and the gaps between regions add about one
minute. The cause of the difference with the report was not measured.

The warm-up ran only for Tuscany, so the 232 MB measured for all Italy in the
report is still to be checked with #155. After the switch a squares request
near Abetone returned 181 available squares of the 18 October run in 0.33 s.

## Live worker check

On 12 October 2026, a map request near Abetone queued the missing weather cell
at 44.15, 10.65. The normal `run_background_worker --batch` process made one
provider request at `2026-10-12T02:08:41.172757Z`. It stored 29 daily rows and
eight summaries, then handed the snapshot to the estimate task. The estimate
task stored 340 forest-square rows.

The next map response returned six available squares. Its selected-area
response used the same saved update UUID. Repeating the request made no
provider call. An Italy-wide view created no weather fetch. The source attempt
was `b4d638f5-fef5-4561-891b-3fcd40a343ac` and the snapshot was
`c98dd3ba-6290-4711-a791-b84113ffed79`.

The ignored local evidence is
`data/processed/map_checks/sporameteo-live-pipeline-verified.json`.
This bounded check used real provider data for one weather cell. The separate
national estimate benchmark used synthetic weather and made no HTTP requests.
