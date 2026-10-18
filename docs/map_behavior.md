# Map behavior

Issue #56 connects the map controls to the stored-data API. The product scope
comes from [the product requirements](product_prd.md), D14 through D20 in
[the decisions](decisions.md), and [the selected design](design/decision.md).
[The API contract](api.md) defines dates, result states and request limits.

## Provider and renderer

The map uses OpenStreetMap data from OpenFreeMap. It does not use the standard
OpenStreetMap tile endpoint. [The P6 assessment](../reports/p6_cost_licence_portability.md)
assessed OpenFreeMap and a locally hosted Protomaps file for this use.

Book Corners creates one Leaflet map and debounces viewport requests in
`libraries/templates/libraries/map.html`. Its background uses raster tiles from
the standard OpenStreetMap endpoint. SporaMeteo keeps the single-map and delayed
request pattern, but D14 requires a different provider.

[OpenFreeMap's integration guide](https://openfreemap.org/quick_start/)
uses MapLibre for its vector tiles. Its Leaflet example requires Leaflet,
MapLibre and a bridge between them. SporaMeteo uses MapLibre directly so that
one renderer draws the background, forest and estimates.

The existing device measurements use MapLibre GL JS 4.7.1. On a physical
iPhone 15, the estimate measurement page drew up to 14,286 squares in about
1.2 to 2.2 seconds including data loading. That page had no background map.
The national forest measurement drew 74,047 polygons in 4.8 to 5.2 seconds.
It missed the three-second target. The owner accepted that initial delay for
the national overview. [The measurement record](api.md#physical-phone-results)
describes the device and limits of those checks.

These measurements do not prove that the complete application passes the
sustained physical iPhone Safari check in #62.

## Controls and result state

The light background starts from OpenFreeMap's Liberty style. The contrast
background starts from Positron. The client adjusts land-cover colors to
parchment or gray so that green background forests do not change the meaning
of the estimate colors. The pinned renderer is MapLibre GL JS 6.4.1.
This version fixes the third-party attribution security flaw in
[GHSA-jrc7-96c5-q579](https://github.com/advisories/GHSA-jrc7-96c5-q579).
The older physical-device measurements above used 4.7.1 and do not validate
the updated renderer.

Changing the background calls `setStyle` on the existing map. After the style
loads, the controller restores its data sources and layers. The date,
selected square, visibility and opacity remain in shared URL state.
The footer keeps the three map-provider credits visible. An expandable
control contains the full input-data credits and licence links in a bounded,
scrollable panel.
The application serves the pinned renderer from its local static files.
Install it with `npm ci --prefix tools/map_checks` before starting Django.

The client fetches and checks a background style before replacing the working
style. A failed request offers retry. A later background choice cancels the
earlier request.

The selected weather outline uses the API's stored `weather_cell` centre and
extends 0.05 degrees on each side. The client does not infer weather geometry
from a map polygon. Squares without a stored weather cell have no outline.

The client reads today's eight dates and the view limits from `/api/v1/info`.
Individual-square requests have limits of 60 km on either axis and 16,000
squares. Broad views request `/api/v1/estimate-overview`, which averages
stored forest-square scores in larger areas for the selected date. It uses
the same colors and never requests new weather. Partly missing areas remain
marked as missing. Selecting an overview area zooms into individual squares.

Viewport changes wait 280 milliseconds before requesting squares. Each
request has an abort signal and a sequence number. Movement hides the previous
estimate layers but keeps the selected area's panel and date percentages open,
even when that area leaves the viewport. Changing the date clears the old
selected result. Only the current response can restore estimate layers after
rendering. Delayed overview responses
also check the current sequence number.

Missing estimates use a gray hatch. Squares below the forest threshold have
no fill and a dotted gray outline. A stored zero remains an available
percentage. Estimate colors use fixed stops at 0, 60, 65, 80 and 90, from
warm white through amber and teal to purple and dark purple. The
renderer uses layer opacity rather than the mockup's per-layer CSS multiply
blend, because MapLibre draws its layers in one canvas.

Pending weather results poll twice at 2.5-second intervals, then offer a
manual retry. A mixed view containing available, pending and no-forest
squares still reports unfinished work. Provider refusal, exhausted allowance
and visitor limits have separate messages. Older results keep their original
target and assessment dates. A changed selected update clears the old
selection rather than combining it with a new result.
The date card shows the assessment date in a compact heading and can collapse.
Calculation times remain in the details panel. Internal update UUIDs do not
appear in that panel. The older-result warning uses only the older results.

Selecting a square loads its eight date percentages through the area API.
Each request names the same square and stored update. Dates beyond an older
update's forecast remain unavailable. A bounded cache avoids repeating
successful requests for the same square and update.

## External destination handoff

The selected-area action opens a Google Maps URL with the validated latitude
and longitude. The URL uses the documented HTTPS search format, so an Android
or iOS device can hand it to an installed Google Maps app. Desktop browsers
and devices without the app use the same URL in the browser. See [Google's
Maps URL documentation](https://developers.google.com/maps/documentation/urls/get-started).

A point selected from the map or place search keeps its exact coordinates when
the map moves or the date changes. A grid selection waits for the current
area response, then uses that response's representative point. The action
stays disabled while that point is missing or does not match the selected
square, date and update. The illustrative fixture keeps the action disabled
because its coordinates are examples rather than a verified destination.

The control opens the external URL only after activation. It does not request
the visitor's location and does not start a route, record a journey, or claim
that the point is a parking place, entrance or safe path.

On 13 October 2026, a headed Chromium desktop check selected a grid area at
the map centre and opened Google Maps at `44.131200,10.641300`. A headed WebKit
phone-sized check in Italian selected a search point and opened Google Maps at
`44.130500,10.640500`. Both checks moved the map after selection and retained
the selected destination. The browser fallback reached the Google Maps place
page after its consent screen. Neither check started a journey. These checks
used controlled local map and API responses. They did not test native app
opening on a physical Android or iOS device.

## Test scope

Automated browser checks use controlled API responses to exercise failures,
delays, old updates and the ends of the percentage scale. Those responses are
test fixtures, not stored product estimates.

Install the pinned browser renderer before running the checks:

```sh
npm ci --prefix tools/map_checks
uv run playwright install chromium webkit
uv run pytest tests/e2e/test_map_controls.py
```

The tests serve MapLibre 6.4.1 from that local dependency directory. They
replace external style and tile responses, so provider availability does not
decide the test outcome. The CI browser job installs the same locked files.

The manual session for #56 uses an isolated Tuscany database. Its static
inputs come from the existing real grid, forest and terrain builds. Weather
and percentages must come through the normal worker. The source national
database stays unchanged by subset preparation. A Tuscany test does not
establish national performance or complete national acceptance.

The local database is `sporameteo_tuscany_56`. Preparation copied 92,707 grid
memberships owned by ISTAT region `09`, Toscana. It copied the same number of
forest and terrain rows, 71 grid chunks and 11 terrain source tiles. Of those
squares, 71,031 meet the forest threshold and share 333 weather cells.
Preparation copied no weather, estimates or queued work. Validation passed
before the worker started. The source still contained 92,707 Tuscany
memberships after the copy.
An additional join check found no missing forest or terrain membership in
the live subset. The preparation command now compares
the exact membership sets, so matching incomplete row counts cannot pass.

The source builds are `italy-500m-istat-2026-01-01-v1`,
`italy-forest-copernicus-2018-v1` and `italy-terrain-glo30-2021-v2`.
The subset versions add the prefixes `tuscany-grid-`, `tuscany-forest-` and
`tuscany-terrain-`, respectively, and the suffix `-56`.
The subset forest artifact is 208,655 compressed bytes and 1,795,869
uncompressed bytes. Its SHA-256 is
`b97c36dd0fb70544ef4e0e24c07e2772074bd9be537243df7feebe3dce6fa170`.

Create an empty database and apply migrations before preparing another local
copy. The region command does not refuse a database that already holds rows, so
start from a new empty migrated database:

```sh
SPORAMETEO_DB_NAME=sporameteo_tuscany_56 \
SPORAMETEO_SOURCE_DB_NAME=sporameteo \
uv run python apps/application/manage.py prepare_region_map_database --regions 09 \
  --grid-build-version tuscany-grid-italy-500m-istat-2026-01-01-v1-56 \
  --forest-build-version tuscany-forest-italy-forest-copernicus-2018-v1-56 \
  --terrain-build-version tuscany-terrain-italy-terrain-glo30-2021-v2-56 \
  --source-grid-build-version italy-500m-istat-2026-01-01-v1 \
  --source-forest-build-version italy-forest-copernicus-2018-v1 \
  --source-terrain-build-version italy-terrain-glo30-2021-v2
```

Then store the municipality of each square. The command also works on a
populated database and a second run changes nothing:

```sh
SPORAMETEO_DB_NAME=sporameteo_tuscany_56 \
uv run python apps/application/manage.py load_municipalities
```

Run it again after every new subset copy, because the copy does not carry
municipalities. Run it before the app serves area details. Finished details
are cached by update, square and date without an expiry, so a detail cached
earlier keeps the plain heading until its cache entry is removed, for example
when the map cache is cleared.

Set the database name and both subset habitat versions in the application
and worker environments.

The live check on 12 October 2026 requested the Abetone bounds
`10.64,44.12,10.68,44.15` for the same Italian date. The first response
contained 59 pending squares and one square without enough forest. After
the normal worker completed, the same request returned 59 available estimates
and the same no-forest square. The database held one weather request attempt,
one ready weather snapshot and one published update with 152 square rows.
The update covers the shared weather cell, so it contains more squares than
the requested view. One returned square, `500m_E4372000_N2334500`, showed 5
percent from update `8602a59f-872a-4667-89e0-da85a503fdbb`, calculated at
18:23:39 UTC. This is a software-path check, not a check of prediction accuracy.

## Headed Chrome measurements

The first live session on 12 October 2026 used installed Google Chrome,
MapLibre GL JS 4.7.1 and a 1440 by 900 desktop viewport. The background came
from OpenFreeMap. The database contained real Tuscany inputs. Measurements
below include response decoding but exclude the 280 millisecond debounce.

| Request | Features | Decoded bytes | Response time |
| --- | ---: | ---: | ---: |
| First narrow view | 669 | 472,451 | 383 ms |
| Moved narrow view | 850 | 599,602 | 2,070 ms |
| Wider narrow view | 2,116 | 1,482,397 | 1,705 ms |
| Resized narrow view | 1,465 | 1,035,128 | 499 ms |
| Tuscany forest overview | 5,970 | 1,795,869 | 112 ms |

The first narrow request reached a visible, idle estimate layer after
995 milliseconds. The session retained the same map instance and seven
registered application handlers after repeated movement. The console recorded
one missing `/favicon.ico` response and no JavaScript exception. The document
had no horizontal overflow at this desktop size.

The user reported slow zooming during this session. Only 10 of the 333 Tuscany
weather cells had ready weather and estimates at that point. The remaining
323 had never been requested. Static grid, forest and terrain coverage was
complete. These measurements therefore do not establish acceptable zoom
performance or complete Tuscany weather readiness.

After that report, the remaining 323 weather cells were requested through
the normal weather service in batches of 100, 100, 100 and 23, with pauses
between batches to respect the visitor limit. All 333 weather snapshots,
fetches and estimate tasks completed. The 333 published updates contain
71,031 square results for all 71,031 eligible forest squares. The final check
found zero missing eligible squares and zero provider failures.

The updated session used MapLibre 6.4.1 at the same desktop size. Four cycles
changed the date, moved between zoom levels 12 and 10, and switched between
the real Liberty and Positron backgrounds. The sequence took 10.1 seconds.
It sent four square requests and one forest request. The square responses
contained 668 to 682 features, used 471,541 to 481,570 decoded bytes, and took
259 to 320 milliseconds. The forest response took 144 milliseconds.

Each background switch restored the custom layers. The session retained the
same map instance, eight registered application handlers, and the same actual
listener counts before and after the sequence. The animation-frame probe
recorded no gap above 50 milliseconds during those cycles. These short desktop
checks do not replace #62's sustained physical-phone acceptance.

At a 390 by 844 viewport, the live map had no horizontal document overflow.
A narrow request returned 63 squares in 151 milliseconds. Selecting a real
square showed eight stored percentages and the Italian experimental notice.
After converting rendered features to plain GeoJSON, selection produced no
console or page error. Both the selected-square and weather-area sources
contained rendered features. Changing to the contrast background preserved
those outlines, the selected square, all eight percentages and 45 percent
opacity. Hiding and showing the overlay also preserved that state.

The initial manual browser used a fixed 900-pixel page height inside a
792-pixel native window. That clipped the visible date controls even though
the DOM measurements fit the emulated viewport. The browser was reopened
without viewport emulation. Its actual content area measured 1,352 by 647
pixels, and all eight date buttons ended at pixel 586, inside the window.
Future manual handoffs must use the native window size.

After the compact metadata and credit repairs, the user's native window
measured 1,352 by 703 pixels. The date card was 133 pixels high, all eight
buttons ended at pixel 642, and the closed footer was 36 pixels high. The
unused older-result warning had `display: none`. The document had no
horizontal overflow. The calculation summary showed the actual earliest and
latest calculation times rather than one entry per weather cell.

The user's screenshot also showed a nearly uniform pale overlay. The visible
available squares all contained a stored 5 percent estimate for that date.
The estimate layer was visible at 60 percent opacity. The approved warm-white
end of the scale therefore showed little variation in that view. The client
must not invent stronger values to create color variation.

## Validation record

Checkpoint on 12 October: the latest full-suite run reached the final browser
checks but reported repeated WebKit failures. It was interrupted while a map
initialization check waited for the renderer. This run did not pass. Earlier
successful runs below are historical evidence, not a green result for this
checkpoint. UI acceptance remains open in #56 while formula investigation #91
takes priority.
The failing WebKit background-control case passed when run alone on a fresh
test server. This does not establish the cause of the combined-run failures.

The full Python suite passed 506 checks before the review repairs. After the
repairs, the exact map browser suite passed 54 checks across Chromium and
WebKit. The layout and language suite passed 100 checks. Four focused live
renderer checks also passed after restoring the sample pages' original script
loading order. The web UI application suite passed 26 checks.

A sustained mouse drag with a selected area reproduced a panel resize that
stopped movement after its first few pixels. The panel now stays open after
panning and keeps the map width stable. The regression check verifies movement while
the mouse stays pressed. It also checks that the header close button stays
inside the viewport and closes the panel. The live header wraps long square
IDs and places coordinates below them so they cannot push the button outside
the panel.

The later panning repair passed 12 focused drag, selection and date-change
checks in Chromium and WebKit. Six drag and control checks passed after adding
the weather-area label. The map uses one container resize observer and skips
resize calls when its dimensions have not changed.

The API weather-cell repair passed 35 API checks. The Tuscany preparation
command passed seven checks, including exact membership validation and
rejection of missing forest or terrain rows. CSS generation, JavaScript
syntax, Django checks and `git diff --check` passed. The map dependency audit
reported zero known vulnerabilities. These are local results, not remote CI
or physical-phone acceptance.

## Later work

- #57 adds place search and current location.
- #58 adds the complete selected-area explanations, weather and source details.
- #59 adds favorites stored in the current browser.
- #62 covers sustained use on physical phones, including iPhone Safari.
