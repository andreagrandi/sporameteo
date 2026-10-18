# Application read API

The Django application serves anonymous reads under `/api/v1`. The generated
schema is at `/api/v1/openapi.json` and the interactive reference is at
`/api/v1/docs`. Run the application with the completed national grid, terrain
and forest builds, the weather migrations and the estimate migrations.

These routes return stored product data. The percentage is experimental and
unvalidated. It does not measure a visitor's chance of finding a mushroom.

| Route | Purpose |
| --- | --- |
| `GET /info` | Today's Italian date, the published map, the run of each region, the dates, the coverage of the published regions and map limits. |
| `GET /forest-layer` | The simplified national forest polygons, with no estimates or weather request. |
| `GET /squares?bbox=west,south,east,north&target_date=YYYY-MM-DD` | Squares and stored estimates of the published regions for the visible map. |
| `GET /estimate-overview?bbox=west,south,east,north&target_date=YYYY-MM-DD` | Average scores of the published regions in larger areas, for views wider than the square limit. |
| `GET /areas/{square_id}?target_date=YYYY-MM-DD&update_id=UUID` | One exact saved estimate and the inputs behind it. Omit the UUID only to classify a square with no selected update. |

Coordinates use WGS84 longitude and latitude. Square IDs identify the national
500 m grid. Dates use Europe/Rome. The map accepts the dates of the published
run from today onwards. Details accept the eight original dates of the
requested update, including a retained previous assessment.

## Published runs

The map, overview and details read the published run of every region, prepared
at night by `run_tuscany_forecast`. A page request never fetches weather,
never downloads habitat data and never calculates an estimate. The run
pointers are read from the database on every request, and a pointer changes
only when a new run of its region is published. A region whose run was made
from another forest build than the configured one counts as not published,
because its results point at the squares of the other build. A failed or
incomplete run leaves the previous run of its region on the map with its real
assessment date. A value for 16 October always comes from the day of the run
that is 16 October, so a day-old run is never moved forward.

Each forest square uses the run of the region that owns it. A weather cell on
a border has a different update in each region, so a square never takes the
update of its neighbour region. The regions can have different assessment
dates. The `map_id` is a short identifier derived from the region codes and
the run IDs of all published regions. It changes whenever any region
publishes, and the responses return it as `run_id`.

Finished responses and the data behind them are kept in the `map` cache, a
database table shared by every process. Its entries do not expire. They are
keyed by `map_id` or run ID, target date, view or 10 km tile, and forest
build, so a new run uses new keys and never mixes with old entries.
`SPORAMETEO_MAP_CACHE_MAX_ENTRIES` bounds the table, 50,000 entries by default.
An evicted entry is rebuilt from the database. After each publication a
background task prepares the run summary and the overview points of the
published region, and `manage.py warm_published_map_cache` does the same by
hand. The command takes `--region-code` with one published region code, or
`all`, its default, which warms every published region in turn. The square and
result tiles of a 10 km area are built by the first request that needs them,
which takes 7 to 16 ms per tile.

## Metadata and forest layer

```sh
curl http://127.0.0.1:8010/api/v1/info
curl --compressed http://127.0.0.1:8010/api/v1/forest-layer
```

The metadata supplies `today`, `target_dates`, `run`, `coverage`,
`forest_build_version`, `max_width_km`, `max_height_km`, `max_squares` and
`forest_layer`. `run` describes the whole map. Its `run_id` is the `map_id`,
its `assessment_date` and eight `target_dates` are those of the newest run,
and its `published_at` is the latest publication. It is `null` when no run is
published. `runs` lists the run of each published region, sorted by region
code, with `region_code`, `run_id`, `assessment_date` and `published_at`. It
is empty when no run is published. The top-level `target_dates` are the dates
of the newest run from today onwards, so a run published yesterday offers
seven dates. Without a run they are today and the next seven days. The layer descriptor contains its URL, SHA-256 and compressed
and uncompressed sizes. The `attributions` list contains the ISTAT boundaries,
Copernicus forest, Copernicus terrain when its configured completed build uses
the same grid, and Open-Meteo. Each item has `name`, `credit`, `licence` and
`url`. The website shows each `credit` and `licence` and links source text to
`url`. Missing values are `null` when an older build did not store that field.
Coverage is the bounding box of all published regions of the grid. Without a
published run it is the bounding box of the Tuscany region, or the national
grid's bounding box when the grid has no Tuscany region. It is not a promise
that every point inside the box has estimates.

The forest layer is the immutable artifact recorded by the completed forest
build. Its only per-polygon value is `forest_share`. The endpoint checks its
checksum and sizes. The info and forest-layer endpoints do not call the weather
provider.

## Map squares

```sh
curl --compressed \
  'http://127.0.0.1:8010/api/v1/squares?bbox=10.60,44.10,10.70,44.20&target_date=2026-10-12'
```

Replace the date with one returned by `/info`. A successful response is a
GeoJSON `FeatureCollection` with `run_id`, `assessment_date`, `target_date`,
`square_count` and `features`. The top-level `run_id` is the `map_id` and the
top-level `assessment_date` is the one of the newest run.
Every feature has its square ID, WGS84 polygon and these properties. This is
an illustrative available-result excerpt:

```json
{
  "state": "available",
  "percentage": 65,
  "unavailable_reason": null,
  "reason": null,
  "target_date": "2026-10-12",
  "assessment_date": "2026-10-12",
  "update_id": "550e8400-e29b-41d4-a716-446655440000",
  "calculated_at": "2026-10-12T08:30:00Z",
  "message_key": "estimate.available",
  "weather_cell": {
    "latitude": 44.0,
    "longitude": 11.0
  }
}
```

The response also includes `cell_id`. Copy the feature ID, target date and
update ID together when opening details. Every square carries the assessment
date of the run of its own region, and a view across a regional border can
hold two dates. A target date outside the eight days of a region's run gives
that region's squares no percentage. Each weather cell has its own update ID
and calculation time.
`weather_cell` contains the stored weather cell centre for the forest square.
It is `null` when either coordinate is missing. Use these coordinates directly
instead of deriving a centre from the displayed polygon.

| State | Percentage | Meaning |
| --- | --- | --- |
| `available` | Whole integer, including a valid zero | The stored result is available. |
| `noforest` | `null` | The square has less than ten percent forest. |
| `unavailable` | `null` | A required input is missing, or the square is outside the prepared area. The reason identifies it. |

For example, no forest has `state: "noforest"` and `reason: "noforest"`.
A missing old-rain input has `state: "unavailable"` and
`reason: "older_rain"`. A forest square whose region has no usable published
run, such as a region that has not published yet, has `state: "unavailable"`,
`reason: "outside_prepared_area"` and
`message_key: "estimate.outside_prepared_area"`. None of these means zero
percent. The current method normally produces values from 5 through 95. The
storage and API contracts still preserve a valid stored zero.

The provisional view limit is 60 km on each axis and 16,000 squares. Bounds
must be finite numbers in coordinate range, with west before east and south
before north. A broader view returns HTTP 400 before any database read:

```json
{
  "code": "view_too_large",
  "message": "Zoom in for estimates before requesting this view.",
  "message_key": "map.zoom_in_for_estimates",
  "details": {
    "width_m": 90000,
    "height_m": 70000,
    "max_width_m": 60000,
    "max_height_m": 60000
  }
}
```

A view that touches the extent of no published region returns 400
`outside_coverage` without reading squares. Without a published run, `/squares` and
`/estimate-overview` return 503 `forecast_unavailable`.

A permitted view reads its squares from cached 10 km tiles. Missing tiles are
loaded together with one forest square query and one stored result query. The
finished body is cached by run, date and bounds, so a repeated view reads one
cache entry. Responses support gzip. Map and forest-layer responses use
`Cache-Control: no-cache`, so a browser must revalidate them. Their ETags
change with the run, date and bounds, or with the artifact. Full weather histories and contributing facts belong to details
and do not repeat for every polygon.

The overview keeps its points per region run, so the warm-up memory stays per
region. A request combines the points of the runs whose extent meets the view.
It takes each run's value at that run's own day offset, and it clips the view
to the extent of all published regions. The regions do not overlap, because
each run holds only the squares of its own region. The top-level `run_id` is
the `map_id` and the top-level `assessment_date` is the one of the newest run.
The `assessment_date` of an area is the oldest date among the runs of its
scored squares, and its `calculated_at` is the oldest calculation time.

## Selected-area details

```sh
curl 'http://127.0.0.1:8010/api/v1/areas/500m_E4370000_N2330000?target_date=2026-10-12&update_id=550e8400-e29b-41d4-a716-446655440000'
```

Use a real square ID and update UUID from `/squares`. The example UUID above
is illustrative. The endpoint never fetches weather or substitutes the newest
update for the requested one.

The selected square must belong to the update's weather cell. A UUID from
another area returns `update_square_mismatch` and attaches no weather data.
For map squares with a null update ID, omit that query value. The endpoint
checks the configured forest build and returns a structured state error for
no forest, missing forest data or an estimate that is not available yet.
It never invents an update or silently chooses the newest one.

| Response group | Stored content |
| --- | --- |
| `selection` | Square ID, polygon, bounds, centroid, representative coordinates, target date, assessment date, days ahead, update UUID, `run_id`, the current or previous published run that holds the update, or `null`, and `municipality_name`, the municipality of the square, or `null`. |
| `estimate` | State, `whole_percentage`, unavailable reason, calculation time, method version, `experimental_unvalidated`, availability translation key and `experimental_explanation_key`. |
| `facts` | Actual method inputs and derived factors, units, inclusive periods, source IDs, completed and forecast day counts and translation keys. |
| `weather` | Selected-day values, rain totals for the previous 7, 14 and 21 completed days, source metadata, missing values and limitations. |
| `sources` | Exact ISTAT grid, forest, terrain and weather source records, release dates, retrieval dates, checksums, real source detail, credits and gaps. |

`selection.municipality_name` comes from the ISTAT municipality borders, the
folder `Com01012026` of the same archive as the region borders, under CC BY
4.0. The command `load_municipalities` matches each square once to the
municipality with the largest overlap and stores the match with the square. The
name is `null` when no municipality is stored for the square. Finished details
are cached by update, square and date without an expiry. The cache key changed
with this field, so details cached before it are rebuilt. A detail cached
before `load_municipalities` ran keeps a `null` name until its entry is removed.

`selection.representative_coordinates` identifies the selected square. Use it
for favorites and navigation. Do not replace it with the current map centre.

Map `properties.percentage` and detail `estimate.whole_percentage` refer to
the same stored byte. The detail rebuilds facts from that update's exact
weather snapshot and habitat builds. A later update cannot change a retained
previous update's explanation. The finished detail is kept in the `map` cache
by update UUID, square and date, so the explanation of one square is built
once. The percentage itself is never recalculated.

A contributing fact has `role_key`, `actual_value`, `unit`, `period_start`,
`period_end`, `source_id`, `source_ids`, `completed_day_count`,
`forecast_day_count`, `method_factor` and `translation_key`. The schema also
exposes equivalent `role`, `value`, `period`, `source` and `normalized_factor`
fields. Product text can translate complete sentences from the keys and
values. Source names and required attribution remain intact.
The experimental explanation key is `estimate.experimental_unvalidated`.
The generated schema types timestamps as `date-time` and defines the nested
fact periods, source identities, gaps, terrain tiles and weather attribution.

Weather attribution includes the exact credit `Weather data by Open-Meteo.com`,
the link `https://open-meteo.com/` and `CC BY 4.0`. The area page reads ISTAT
attribution for the requested update's grid from `sources.grid.credit`,
`sources.grid.licence` and `sources.grid.url`. It reads
forest attribution from `sources.forest.credit`, `sources.forest.licence` and
`sources.forest.catalogue_url`. It reads terrain attribution from
`sources.terrain.credit`, `sources.terrain.licence` and
`sources.terrain.catalogue_url`. That terrain link is the recorded dataset
link, while `sources.terrain.service_url` retains the recorded source link when
one exists. It reads weather attribution from `sources.weather.credit.text`,
`sources.weather.credit.licence` and `sources.weather.credit.url`. The website
shows the applicable credits with the data. Forest and terrain records retain
their dataset-specific attribution and source dates. A missing terrain cell
still returns its source record and gap, while `/info` omits terrain when
the configured terrain build is absent, unfinished or uses another grid.

For an assessment on 12 October, the recent seven-day period is 5 through
11 October. The 14-day period starts on 28 September and the 21-day period on
21 September. None contains rain forecast for 12 October or later. Each
period reports both boundaries, its unit, missing reason and source attempts.

Optional soil and evaporation values remain null when unavailable. Frost
appears only when the stored minimum temperature supports its derivation.
There is no invented hail value, numerical prediction confidence or accuracy
label based only on days ahead. The limitation keys explain that multiple
500 m squares can share weather covering several kilometres.

## Errors

Errors contain `code`, `message`, `message_key` and `details`. Clients should
translate `message_key` with the supplied values. The readable English message
also helps inspect an API response.

| HTTP status | Code | Meaning |
| --- | --- | --- |
| 400 | `missing_parameter`, `invalid_bbox`, `invalid_date`, `invalid_update_id`, `invalid_square_id` | Malformed or missing input. |
| 400 | `unsupported_date` | The map target is not a date of the published run from today onwards. `details.available_dates` lists the accepted dates. |
| 400 | `view_too_large`, `square_limit_exceeded` | Zoom in before requesting estimates. |
| 400 | `outside_coverage` | The view is outside the regions with published estimates or holds no grid square. |
| 404 | `square_unknown` | The square does not belong to the requested update's forest build. |
| 409 | `update_square_mismatch` | The square and update refer to different weather cells. |
| 409 | `no_forest`, `missing_forest_data`, `outside_prepared_area` | The square has no selected saved estimate. No unrelated weather is attached. |
| 410 | `update_expired` | The requested update UUID is no longer available. Refresh the map. |
| 503 | `forest_build_unavailable`, `forest_layer_unavailable` | The configured completed build or checked artifact is unavailable. |
| 503 | `forecast_unavailable` | No region has a published run yet. |

A deleted update returns this shape, with the requested UUID in `details`:

```json
{
  "code": "update_expired",
  "message": "The requested estimate update is no longer available.",
  "message_key": "area.update_expired",
  "details": {"update_id": "550e8400-e29b-41d4-a716-446655440000"}
}
```

## Largest-view measurements

### Live request through the worker

On 12 October 2026, the view `10.653,44.137,10.661,44.143` near Abetone
returned six pending squares. The normal database worker then made one
Open-Meteo request for the weather cell centred at 44.15, 10.65. It stored
29 daily rows, eight summaries and estimates for all 340 forest squares of
that weather cell.

The next map response returned six available squares. Details for
`500m_E4373000_N2336500` matched its saved map value of 5 and update
`5aac5767-92d1-4bd5-bc30-44fca00ced81`. The weather retrieval time was
`2026-10-12T02:08:41.172757Z`, with attempt
`b4d638f5-fef5-4561-891b-3fcd40a343ac`. Open-Meteo returned model-grid
coordinates 44.149982, 10.645727 and reference elevation 1,319 m.

A repeated map request and the detail request made no further provider call.
An Italy-wide request returned `view_too_large` and created no weather fetch.
This checks the live data path and identity agreement, not prediction accuracy.
The ignored local evidence is `data/processed/map_checks/sporameteo-live-pipeline-verified.json`.

Repeat the live check from the repository root with the local application
database. Use the current Italian date in `--assessment-date` for a fresh
provider check. The queue command makes the normal API request and leaves
provider work for the background worker. Queue mode refuses past dates. Verify
mode accepts a retained historical date:

```sh
live_assessment_date="$(uv run python -c 'from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo("Europe/Rome")).date())')"
uv run python tools/map_checks/check_live_pipeline.py queue \
  --database-name sporameteo \
  --assessment-date "$live_assessment_date" \
  --bbox "10.653,44.137,10.661,44.143" \
  --output data/processed/map_checks/sporameteo-live-pipeline-queued.json
uv run python apps/application/manage.py run_background_worker \
  --batch --no-startup-delay --no-reload --max-tasks 10
```

After the worker has stored the snapshot and estimate, verify the exact saved
records without making another provider request:

```sh
uv run python tools/map_checks/check_live_pipeline.py verify \
  --database-name sporameteo \
  --assessment-date "$live_assessment_date" \
  --bbox "10.653,44.137,10.661,44.143" \
  --output data/processed/map_checks/sporameteo-live-pipeline-verified.json
```

Queue mode reports the map response and request counters. It does not claim
that the worker ran. Verify mode reads saved weather and checks its 29 daily
rows, eight summaries, estimate rows and agreement between map and detail.
It also checks repeated reads and the broad-view guard without fetching weather.

To inspect the retained historical evidence from 12 October 2026 without
queueing work, run verify with its fixed assessment date:

```sh
uv run python tools/map_checks/check_live_pipeline.py verify \
  --database-name sporameteo \
  --assessment-date 2026-10-12 \
  --bbox "10.653,44.137,10.661,44.143" \
  --output data/processed/map_checks/sporameteo-live-pipeline-verified-20261012.json
```

### National response and desktop drawing costs

The 12 October 2026 local measurement used the national habitat builds and
the disposable database from [the estimate benchmark](estimate_updates.md).
Its percentages came from labelled synthetic benchmark weather. A stored
snapshot lookup replaced weather acquisition, so this measures database
reads, polygon conversion, JSON and compression without provider latency.
It made no weather HTTP request.

The laptop was an Apple M2 Pro with 32 GiB of memory. Each view measured
about 59.4 by 59.2 km. Each request made five database queries. The first
read populated the response cache, followed by two cached reads.

| View | Squares | Forest squares | Raw bytes | Gzip bytes | First read | Cached reads |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Northern Apennines | 14,286 | 13,356 | 9,260,493 | 819,559 | 2.1085 s | 1.0526 and 1.1008 s |
| Alps | 14,261 | 12,511 | 9,196,599 | 830,451 | 1.6551 s | 1.0818 and 1.1194 s |
| Calabria | 13,145 | 11,013 | 8,432,366 | 759,762 | 1.6254 s | 1.0831 and 1.0047 s |

Gzip length can differ by a few bytes because Django adds random padding.
The broad Italy view returned `view_too_large` with zero weather calls.
The 16,000-square limit allows these near-60-km national views. The physical
phone measurements below check that limit on an iPhone 15. A desktop browser
with a phone-sized window is not a physical-phone acceptance check.

The same saved responses were drawn with MapLibre GL JS 4.7.1 in a visible
Chrome 154.0.8037.98 window, 1,440 by 900 CSS pixels at two device pixels per
CSS pixel. The measurement used an isolated persistent browser profile and
an empty background map. It timed `addSource` through the first `idle` event
after adding the fill layer. It excludes background tiles and network transfer
from a deployed server.

| View | Three drawing times |
| --- | --- |
| Northern Apennines | 334.1, 313.4 and 312.2 ms |
| Alps | 309.2, 316.9 and 312.5 ms |
| Calabria | 309.7, 312.0 and 312.4 ms |

These desktop results are separate from the physical-phone measurements
recorded below for [#64](https://github.com/andreagrandi/sporameteo/issues/64).
Any closer starting view needs the owner's agreement, as required by #49.

### Native PostGIS image repeat

On 12 October 2026, the same saved benchmark data was measured again after
moving it to the native image and volume. A second run used the previous
PostGIS image with AMD64 emulation against the original benchmark database.
Both runs made five database queries per view, made no weather HTTP requests
and decoded to exactly the same GeoJSON on the same Apple M2 Pro laptop.
The broad Italy request returned `view_too_large` with zero weather calls in
both runs.

| View | Raw bytes | Native gzip bytes | Native read times | AMD64 emulated read times |
| --- | ---: | --- | --- | --- |
| Northern Apennines | 9,260,493 | 819,599 / 819,556 / 819,577 | 1.6214 / 0.9208 / 0.9741 s | 2.7762 / 1.1423 / 1.2033 s |
| Alps | 9,196,599 | 830,403 / 830,413 / 830,454 | 1.4087 / 1.0249 / 0.9969 s | 1.6005 / 1.2187 / 1.2027 s |
| Calabria | 8,432,534 | 759,743 / 759,741 / 759,693 | 1.2677 / 0.8957 / 0.8429 s | 1.4887 / 1.0995 / 1.0233 s |

Each time list contains the first read followed by two cached reads. The
native run used `imresamu/postgis:17-3.5-bookworm` at digest
`sha256:35fe7dceda62bfe7d2965b0ba78d915ff9f901b5e0466d633ecc58421b5b5210`.
It contains PostgreSQL 17.6 and PostGIS 3.5.3. The emulated run used the
previous PostgreSQL 17.5 and PostGIS 3.5.2 image. Native cached reads were
19.2 percent shorter for Northern Apennines, 16.5 percent shorter for the Alps
and 18.1 percent shorter for Calabria when comparing the average of the two
cached reads. These results include the image and database-version change, so
they describe this repeat setup rather than isolating CPU architecture. The
historical table records 8,432,366 raw bytes for Calabria. Both current runs
recorded 8,432,534 bytes.

The native report is
`data/processed/map_checks/issue73-native/sporameteo-map-measurements.json`.
Its SHA-256 is
`0c2ff6fd8e113447c5912cc6090fd0fdc1d1b282f4ddcbf2d2bac447a7334109`.
Repeat the native measurement with the disposable database and output path. Set
`SPORAMETEO_DB_PORT` to the port of that database when it is not the default:

```sh
SPORAMETEO_DB_PORT="${SPORAMETEO_DB_PORT:-5542}" \
  uv run python tools/map_checks/measure_responses.py \
  --database-name sporameteo_estimate_benchmark_20261012 \
  --assessment-date 2026-10-13 \
  --output-dir data/processed/map_checks/issue73-native
```

The repeatable measurement tools and page are under `tools/map_checks/`.
The ignored local files under `data/processed/map_checks/` contain the three
saved responses and the two measurement reports. To repeat the response size
and timing measurement from the repository root, use the disposable benchmark
database and completed national grid, terrain and forest data described in
[the estimate benchmark](estimate_updates.md). Those builds and benchmark
rows are local inputs and are not included in a clean checkout:

```sh
uv run python tools/map_checks/measure_responses.py \
  --database-name sporameteo_estimate_benchmark_20261012 \
  --assessment-date 2026-10-13 \
  --output-dir data/processed/map_checks
```

The script replaces weather lookup with saved snapshots for the chosen date,
so it makes no provider request. To repeat the visible desktop drawing
measurement, install the pinned Node packages and matching Chromium binary:

```sh
npm --prefix tools/map_checks ci
npm --prefix tools/map_checks exec -- playwright install chromium
node tools/map_checks/measure_drawing.cjs \
  --data-dir data/processed/map_checks \
  --output data/processed/map_checks/sporameteo-map-drawing-measurements.json \
  --profile-dir data/processed/map_checks/browser-profile
```

The historical measurement used MapLibre 4.7.1. The local dependency now pins
6.4.1, which fixes the critical attribution security advisory
GHSA-jrc7-96c5-q579. New runs use the updated renderer and must record their
own timings. They do not reproduce the historical library version.

The drawing script starts a bounded local server for the page, MapLibre assets
and three named GeoJSON files. It uses a visible 1,440 by 900 browser window,
two device pixels per CSS pixel and a persistent profile. The report records
the browser and library versions, viewport, headed state and
`physical_phone: false`. For a fresh dataset, request the documented views
from the application and save each GeoJSON response before running the drawing
measurement.

The response and drawing tables above quote the 12 October 2026 run. New
commands write ignored reports under `data/processed/map_checks/`; they do not
replace those recorded numbers.

For API contract checks, run:

```sh
uv run pytest tests/app/test_map_api.py tests/app/test_area_api.py
```

The fixtures cover saved values, unchanged previous updates, expired updates,
rain periods, missing required and optional inputs, source credits, broad
views, invalid bounds, date limits and cache identities. They replace external
weather and never depend on a live provider.

### Physical-phone measurement page

Start the local measurement page with the saved national responses:

```sh
uv run python tools/phone_map/serve.py --host 0.0.0.0 --port 8769 \
  --data-dir data/processed/map_checks
```

Open the laptop's local network address on a phone using the same Wi-Fi.
Enter the phone model, operating-system and browser versions, and connection
conditions. Keep the page visible and tap `Run measurements`. It runs three
trials for each of the Northern Apennines, Alps and Calabria views, then sends
the report to `data/processed/phone_map_checks/` on the laptop.

The report separates data loading and JSON parsing, polygon drawing, and the
total time from starting the data request until the map is drawn. It records
the actual compressed body size, transfer size, viewport and device pixel
ratio. These totals exclude loading the page and MapLibre from its CDN. The
server compresses the saved responses itself, so its compressed size can
differ from the application response. The map has an empty background and
uses the same MapLibre version and synthetic weather as the desktop check.
It loads a fixed saved area. Panning outside that area shows no more squares.
Loading the newly visible area as the visitor pans belongs to the application
map in #56, not this measurement page.

The physical-phone checkbox records the operator's device declaration.
Desktop verification must clear it. A report marked as a desktop test or with
the page hidden during measurement does not satisfy #64. Stop the server with
Ctrl+C after collecting the phone results. It serves only the measurement
page, manifest and three named responses.

### Physical-phone results

On 12 October 2026 at 08:13 CEST, the owner ran all nine checks on a physical
iPhone 15. The owner reported iOS 27. The browser identified itself as Safari
27.0.1. The phone and M2 Pro laptop used the same Wi-Fi. The page stayed visible
throughout the run. The page loaded MapLibre GL JS 4.7.1 from its pinned CDN URL.

The viewport was 393 CSS pixels wide and changed from 657 to 749 pixels high
as the browser controls moved. The map canvas remained 393 by 555 CSS pixels,
with a device pixel ratio of three. The views and their square counts match
the largest-view desktop measurements above.

| View | Gzip bytes | Three drawing times | Three times including data loading |
| --- | ---: | --- | --- |
| Northern Apennines | 805,201 | 1,128, 1,215 and 1,249 ms | 1,341, 1,402 and 1,538 ms |
| Alps | 814,980 | 1,842, 1,880 and 1,974 ms | 2,022, 2,052 and 2,197 ms |
| Calabria | 745,813 | 1,009, 1,205 and 1,095 ms | 1,179, 1,368 and 1,257 ms |

Each reported encoded body size matched the server's gzip length. The browser
reported 300 additional transfer bytes per response. Drawing time runs from
adding the GeoJSON source to the first MapLibre idle event. The last column
starts before fetching the response and includes JSON parsing, map creation
and drawing. Neither column measures the initial HTML and library download,
background tiles, a public server or a mobile data connection.

The owner confirmed that panning felt usable and observed the fixed-area
boundary. Keep the 60 km per-axis limit and the 16,000-square cap. No limit
change is needed from this check. This result covers this phone, browser and
connection. The largest saved view contains 14,286 squares; 16,000 remains
the request guard, not a claim that this run rendered exactly 16,000 squares.
The website still needs to load adjacent views as the map moves.

The complete local report is
`data/processed/phone_map_checks/069e21c7e60146bc9fe809946797895b.json`.
Its SHA-256 is
`805e6a8842506cd75f67b780815b3f4d75d057c389ae488983b96333626b3107`.
The original report omitted the library-version property. The pinned script
URL identifies version 4.7.1, and the measurement page now records it explicitly.

### National forest measurement

For #69, start the same page with the completed forest artifact:

```sh
uv run python tools/phone_map/serve.py --host 0.0.0.0 --port 8769 \
  --forest-layer data/processed/mvp_forest/italy-forest-copernicus-2018-v1/forest_layer_2000m.geojson.gz
```

This option needs no saved estimate responses. The server sends the existing
gzip file unchanged and records its checksum and sizes in the manifest.
The page draws all 74,047 forest polygons across Italy. Colour represents
`forest_share`; there are no estimates or weather calls. It runs three trials
with a fresh map and an uncached download each time. The original nine-trial
check remains available when `--forest-layer` is omitted.

Open the laptop's address on the same Wi-Fi, enter the device details and tap
`Run measurements`. Keep the page visible. Once it finishes, pan and zoom
around Italy and report whether the map feels usable. The page saves all
three results in `data/processed/phone_map_checks/`.

Each trial records these times in milliseconds:

- `download_ms` covers the request through receiving and decoding the full
  response text, including gzip decompression.
- `reading_ms` covers `JSON.parse` on that text.
- `source_to_idle_ms` covers adding the forest source and drawing it until
  MapLibre reports that the map is idle.
- `fetch_to_drawn_ms` covers the whole request through drawing, including
  calculating bounds and creating the map between reading and drawing.

The sum of the first three times excludes map setup. Use `fetch_to_drawn_ms`
for the issue's three-second threshold. The check excludes the initial page
and library downloads, background tiles and application-server work.

On 12 October 2026 at 15:08 CEST, the owner ran the three checks on a physical
iPhone 15 and reported iOS 27. The browser identified itself as Safari 27.0.1.
The phone and laptop used the same Wi-Fi. The page stayed visible throughout
the run. MapLibre GL JS 4.7.1 drew the layer in a 393 by 555 CSS pixel map,
with a device pixel ratio of three.

| Trial | Gzip bytes | Download | Reading JSON | Drawing | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 3,377,975 | 191 ms | 58 ms | 4,321 ms | 4,858 ms |
| 2 | 3,377,975 | 185 ms | 56 ms | 4,610 ms | 5,127 ms |
| 3 | 3,377,975 | 315 ms | 58 ms | 4,559 ms | 5,202 ms |

Each encoded body size matched the original gzip file. Every run used all
74,047 polygons and 22,169,307 uncompressed bytes. The owner described the
initial load as a bit slow but found it acceptable for the whole Italy map.
All three initial loads exceeded the three-second target in #69. Drawing
accounted for most of the time.

The owner expects normal use to start zoomed in around the user's area.
This check measures the national overview and does not establish the speed
of that local starting view. Keep the forest artifact unchanged for now and
judge normal loading in the application with that local view. The national
three-second target remains unmet. A smaller viewport alone does not avoid
downloading and reading the national file if the application still requests it.

The complete local report is
`data/processed/phone_map_checks/4cc56f4f76e54c399c28b10347087699.json`.
Its SHA-256 is
`4c253d04efe5984378cf9fb8796fa00184266272c8605fafbbc6c32014e70a2b`.
