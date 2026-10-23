# Estimate updates

An estimate update is one complete publication for one weather cell and one
Italian assessment date. `EstimateUpdate.public_id` is the UUID exposed to map
and detail reads. The database also keeps an internal bigint primary key for
joins. The UUID never changes when the same immutable input tuple is retried.

The tuple is the weather cell, assessment date, exact `WeatherSnapshot`, exact
completed `ForestBuild`, exact completed `TerrainBuild`, and frozen method
version. The forest and terrain builds must point to the same completed grid.
The weather snapshot cell and date must match the update. The terrain build is
read from the explicit `SPORAMETEO_TERRAIN_BUILD_VERSION` setting. The service
does not select an arbitrary latest terrain build.

`EstimateResult` has no surrogate row ID. Its composite primary key is the
update and forest square. Its `values` field contains exactly eight bytes for
assessment day through day seven. Bytes from 0 through 100 are whole
percentages. Bytes from 128 through 137 are documented unavailable reasons:

| Byte | Reason |
| ---: | --- |
| 128 | `older_rain` |
| 129 | `recent_rain` |
| 130 | `target_temperature` |
| 131 | `reference_altitude` |
| 132 | `square_altitude` |
| 133 | `missing_forest_data` |
| 134 | `forest_below_minimum` |
| 135 | `weather_summary_missing` |
| 136 | `terrain_data_missing` |
| 137 | `period_temperature` |

Byte 130 comes only from updates stored with `porcini-experimental-v1`. Byte
137 comes only from v2 updates.

Other bytes are rejected. A forest square without at least ten percent forest
remains in `ForestCell` and receives no result row. An eligible square receives
one result row containing all eight dates. The service loads summaries and
habitat rows in batches, calculates with the current frozen method,
`porcini-experimental-v2`, and validates every expected row before it inserts
an update or result. v2 reads the daily mean and minimum temperature of the 20
days ending on each target date from the snapshot's daily weather rows. The
update stores its method version. An area detail rebuilds facts with the
method named by that version, so an update stored with v1 keeps its v1
percentages and facts after v2 is released.

Forest squares are selected by the exact two-decimal weather-cell centre in
SQL. The estimate updates migration adds a partial covering index for the
completed forest build, forest flag and two centre coordinates. The national
loop therefore reads the scalar square fields for one cell instead of loading
the full forest build for each calculation.

Validation rejects missing or duplicate square rows, mixed method versions,
nonfinite values, noninteger percentages, values outside 0 through 100, and
unknown unavailable reasons. The write runs in one database transaction under
a PostgreSQL advisory lock for the weather cell. It rechecks snapshot and
build identities under that lock. A repeat input returns the original UUID.
An older assessment cannot replace a newer assessment. A failed calculation or
write leaves the previous complete update and its source references unchanged.

The worker callback from #46 only creates an `EstimateTask` and queues the
separate database task. The task makes no weather request. A failed task can be
retried independently. The immutable input fingerprint prevents duplicate
pending tasks for the same snapshot, builds, cell and assessment date.

Reads require the exact update UUID supplied by the caller. They never select a
newer update as a fallback. The shared read contract returns these states:

| State | Meaning |
| --- | --- |
| `available` | The selected byte is a whole percentage. |
| `unavailable` | The selected byte is a documented reason code. |
| `noforest` | The square is in `ForestCell` but is below the forest threshold. |
| `noresultyet` | The exact update exists but has no eligible result row. |
| `unknown` | The square does not belong to the exact update's forest build. |
| `expired` | The exact update UUID has been deleted or never existed. |

Facts for an area detail are rebuilt from the update's exact snapshot and the
update's exact forest and terrain builds. No latest-input lookup is used. This
keeps the explanation and percentage on the same inputs and named method.

Nightly cleanup keeps every update for the two newest distinct assessment dates
for each weather cell. This keeps same-day rebuilds when they use a different
forest build. It deletes older updates and their result rows in one transaction
and deletes terminal estimate tasks that point to those old snapshots. Active
waiting or running tasks remain and their snapshot IDs are returned with the
retained update references. Weather cleanup uses those IDs before deleting old
snapshots.

The weather callback records a waiting task before queue dispatch. If queue
dispatch fails, the task keeps its failure reason and a later callback or
worker startup recovery reserves it again. Startup recovery also returns
interrupted running tasks to waiting without making a weather request.
It does this only when no worker holds the task lock, and the 60 second check
does the same.

Each task records the method and tree source it was queued with. A deploy that
changes either leaves old tasks in the queue. The worker and startup recovery
close each of them as failed, with the reason that a newer method replaced it,
and raise no error. Startup recovery then queues tasks with the new method for
the two newest weather days of each cell.

## Local benchmark

Run the benchmark only against a disposable PostgreSQL database whose name
contains `estimate_benchmark`:

```sh
uv run python apps/application/manage.py benchmark_estimate_updates \
  --forest-build-version <forest-build> \
  --terrain-build-version <terrain-build> \
  --assessment-date 2026-10-11 \
  --cell <latitude,longitude>
```

Use `--national` instead of `--cell` for every real national forest weather
cell. The command creates clearly labelled
`synthetic-benchmark-29dweather` snapshots, uses the real completed forest,
terrain and grid rows, runs two assessments per cell, and records elapsed time,
row counts, database bytes before and after, heap, index and total bytes for
the update, result and task tables, and the separate forest-cell lookup index.
The synthetic weather is explicitly unusable as a forecast. The command makes no
HTTP requests and reports measured storage without extrapolating to a target.

### Measured national run

The local run on 12 October 2026 used an Apple M2 Pro with 32 GiB of memory,
PostgreSQL 17 and PostGIS 3.5 in Docker, Python 3.14.8 and the frozen
`porcini-experimental-v1` calculation. The disposable database was
`sporameteo_estimate_benchmark_20261012`. It contained these real habitat builds:

- Grid `italy-500m-istat-2026-01-01-v1`.
- Terrain `italy-terrain-glo30-2021-v2`.
- Forest `italy-forest-copernicus-2018-v1`.

The weather was synthetic and labelled `synthetic-benchmark-29dweather`.
These records measure calculation and storage cost. They are not product
forecasts or evidence of prediction accuracy. The run made no HTTP request.

| Measurement | Result |
| --- | ---: |
| Forest squares | 717,104 |
| Distinct weather cells | 3,726 |
| Assessments per cell | 2, dated 12 and 13 October 2026 |
| Complete updates | 7,452 |
| Result rows | 1,434,208 |
| Stored daily estimates | 11,473,664 |
| National calculation and write time | 710.3282 seconds |
| Whole command elapsed time | 715.71 seconds |
| Peak process resident memory | 478,658,560 bytes, 456.5 MiB |
| One cell at 44.15, 10.65, 340 squares and two assessments | 0.3155 seconds |
| Fastest and slowest two-assessment cell | 0.0238 and 1.1741 seconds |

The national run reused the two updates from that one-cell measurement.
It calculated the remaining 7,450 updates. The audit checked two rows per
forest square, eight bytes per row, and agreement between every update's
declared row count and its stored rows.

| Stored table or index | Bytes including indexes |
| --- | ---: |
| Estimate results | 131,702,784 |
| Estimate update metadata | 5,840,896 |
| Empty durable task table and its indexes | 73,728 |
| All estimate tables | 137,617,408 |
| Additional forest lookup index | 55,418,880 |

The estimate tables use 131.2 MiB, about 31 percent above the provisional
100 MiB estimate. This is the measured cost of retaining current and previous
national results. The separate 52.9 MiB index speeds up finding forest squares
within a weather cell. It belongs to the habitat table and is not hidden in
the estimate total. The whole cloned database occupied 2,496,416,915 bytes at
the audit, including habitat and synthetic weather records.

The first benchmark command reported 137,543,680 bytes because it counted
updates and results but omitted the empty task table. The table audit above
includes it. The command now reports all three tables and the separate forest
lookup index.

The reproducible national command is:

```sh
SPORAMETEO_DB_NAME=sporameteo_estimate_benchmark_20261012 \
  /usr/bin/time -l uv run python apps/application/manage.py benchmark_estimate_updates \
  --forest-build-version italy-forest-copernicus-2018-v1 \
  --terrain-build-version italy-terrain-glo30-2021-v2 \
  --assessment-date 2026-10-12 --national
```

The focused retention tests create consecutive assessments, same-day rebuilds
and terminal tasks without manually removing their references. Cleanup keeps
every update from the newest two dates and active tasks, removes older results
and terminal task references, and makes the deleted update UUID return the
expired state.

## Live worker check

The separate live check on 12 October 2026 fetched weather for one real cell
at 44.15, 10.65. The normal worker stored update
`5aac5767-92d1-4bd5-bc30-44fca00ced81` with 340 forest-square rows and eight
dates each. A map square and its selected-area response returned the same
saved percentage and update UUID. Repeating the map read made no provider
call. [The API report](api.md#live-request-through-the-worker) records the
request bounds, retrieval time and source attempt. This check uses live
weather and is separate from the synthetic performance measurements.
