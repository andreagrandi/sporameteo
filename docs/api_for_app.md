# What the API must serve for a phone app

This report lists every page and feature of the website on 2026-10-24. For
each feature it names where the data comes from, says whether a phone app
would need it, and names the endpoint to add when one is missing. It was
checked against `apps/application/config/urls.py`, the templates in
`apps/application/web_ui/templates/web_ui/` and the browser code in
`apps/application/web_ui/static/web_ui/app.js` and `favorites.js`.

This report adds no endpoints. The endpoints themselves are described in
[api.md](api.md).

## Pages

`apps/application/config/urls.py` serves these routes:

| Route | View | Phone app needs it | Reason |
|---|---|---|---|
| `/` | `web_ui.views.landing` | Partly | The app needs the best areas list, which is built on the server, but not the rest of the landing page. |
| `/map/` | `web_ui.views.map_page` | Yes | The map is the main feature, and the browser already reads all its data from `/api/v1/`. |
| `/map/fixture/<fixture>/` | `web_ui.views.map_page` | No | These pages hold fixed test data and are off unless `SPORAMETEO_UI_FIXTURE_PAGES` is set. |
| `/privacy/` | `web_ui.views.privacy` | No endpoint | An app store listing needs a privacy policy link, and the app can open this web page. |
| `/health/` | `config.health.health` | No | Dokku and the uptime monitor use it, not visitors. |
| `/api/v1/` | `config.api.api` | Yes | This is the API a phone app would call. |

## API endpoints today

| Endpoint | Function | Inputs | Output |
|---|---|---|---|
| `GET /api/v1/health` | `config.api.health` | none | `HealthResponse` |
| `GET /api/v1/info` | `map_api.api.info` | none | `InfoResponse`: today, available dates, published run, coverage, limits, forest layer and attributions |
| `GET /api/v1/squares` | `map_api.api.squares` | `bbox`, `target_date` | `SquaresResponse`: one feature per 500 m square with its score |
| `GET /api/v1/estimate-overview` | `map_api.api.estimate_overview` | `bbox`, `target_date` | Average scores of larger areas, with no response schema |
| `GET /api/v1/forest-layer` | `map_api.api.forest_layer` | none | The gzip forest overview layer, with no response schema |
| `GET /api/v1/areas/{square_id}` | `area_api.router.area_detail` | `target_date`, optional `update_id` | `AreaDetailSchema`: score, weather, facts and sources of one square |
| `GET /api/v1/places/search` | `place_search.api.places_search` | `q`, `lang`, optional `cached_only` | Place results, with no response schema |
| `POST /api/v1/places/resolve` | `place_search.api.places_resolve` | `latitude`, `longitude`, `source` | The square that holds the point, with no response schema |

## Map features

| Feature | Data source | Phone app needs it | Reason |
|---|---|---|---|
| Available dates and the daily refresh | `GET /api/v1/info`, read at `app.js:3335` and again at `app.js:3310` | Yes | The app must know which 8 dates have scores and when a new day starts. |
| Scores of the squares in view | `GET /api/v1/squares`, read at `app.js:3201` | Yes | This is the main map content. |
| Average scores when zoomed out | `GET /api/v1/estimate-overview`, read at `app.js:3201` | Yes | The app needs a readable map of all Italy before the user zooms in. |
| Forest overlay | `GET /api/v1/forest-layer`, read at `app.js:3056` | Yes | The zoomed out map shows only forests. |
| Background map | OpenFreeMap tiles from `tiles.openfreemap.org`, set at `app.js:1304` | Yes, no endpoint | The tiles come from OpenFreeMap, not from SporaMeteo. |
| Area details panel | `GET /api/v1/areas/{square_id}`, read at `app.js:2856` | Yes | It explains the score with weather, forest and terrain facts. |
| Scores of one square for each of the 8 dates | `GET /api/v1/areas/{square_id}` once per date, read at `app.js:1633` | Yes | The details panel shows how the score changes over the week. |
| Older result notice | `run.assessment_date` and `today` from `GET /api/v1/info` | Yes | The user must know when the shown scores were calculated on an earlier day. |
| Place search | `GET /api/v1/places/search`, read at `app.js:498` and `app.js:514` | Yes | Users find a forest by its place name. |
| Choose a search result or a map point | `POST /api/v1/places/resolve`, sent at `app.js:3515` | Yes | The app must turn a point into a square id. |
| Locate me | The device location, then `POST /api/v1/places/resolve` | Yes | Users check the forest where they stand, and the phone gives the location. |
| Favorites | `localStorage` key `sporameteo.favorites`, in `favorites.js` | Yes, no endpoint | The app can keep favorites on the phone, as the website keeps them in the browser. |
| Legend, layers and opacity | Markup in `map.html` and state in `app.js` | Yes, no endpoint | The app draws its own controls. |
| Copy page link | The current `/map/` address, copied at `app.js:1236` | Yes, no endpoint | The app can share the same `/map/?square=...&date=...` web address. |
| Map position in the address | Query values read at `app.js:148` and written at `app.js:650` | No | The app keeps its own screen state. |
| Language switch | Cookie `sporameteo_language` and a page reload, at `app.js:79` | Yes, no endpoint | The app ships its own Italian and English text and passes `lang` to place search. Errors carry a `message_key` the app can translate. |

## Landing page features

`web_ui.landing_examples.landing_examples` builds all of these on the server.
The browser code does not run on the landing page.

| Feature | Data source | Phone app needs it | Reason |
|---|---|---|---|
| Best areas today, up to 8 municipalities | `web_ui.landing_top_areas.top_areas`, built on the server | Yes | The app has no way to get this list today, and it is a quick start for users who have no place in mind. |
| Hero card of one area | One random entry of the best areas, with facts read from `read_area_detail` | No | It is a teaser for the map, and the app can open the details panel for a best area. |
| Four fixed example areas | `map_api.published.read_squares` for fixed squares in `AREA_SQUARES` | No | They show what the map does to a new visitor. |
| Text sections on the method, the factors and foraging ethics | Translated text in `landing.html` | No | The app can ship this text or link to the website. |
| Older result notice | `current.newest.assessment_date` compared with today | Yes, already served | `GET /api/v1/info` holds the same dates. |

## Endpoint to add

### Best areas

`GET /api/v1/top-areas`

Inputs: none. The list always covers the first available date, as the landing
page does today.

Output fields:

- `target_date`: the date of the scores, the first entry of `target_dates` in `/info`.
- `run_id`: the published run the list comes from.
- `areas`: up to 8 entries, best score first, one per municipality. Each entry has:
  - `rank`: 1 to 8.
  - `square_id`: the best scoring square of the municipality.
  - `update_id`: the update to pass to `/api/v1/areas/{square_id}`.
  - `municipality`: the municipality name.
  - `region`: the region name, or null.
  - `latitude` and `longitude`: the middle of the square.
  - `percentage`: the whole score of the square on `target_date`.

With no published run, the endpoint returns 503 with code
`forecast_unavailable`, as `/squares` and `/estimate-overview` do. The endpoint
leaves out the
weather facts that the hero card shows. The app reads them from
`/api/v1/areas/{square_id}` when the user opens an area.

## Sign-in

App sign-in can wait until accounts exist. SporaMeteo has no accounts: F77 in
[product_prd.md](product_prd.md) defers accounts and sign-in, and F30 defers
syncing personal records. Favorites stay on the device. Every endpoint is a
public read, except `/places/resolve`, which is an anonymous POST that stores
nothing about the user.

Book Corners `users/api.py` shows what to copy when accounts come. It uses
`ninja_jwt` to hand out a short-lived access token and a long-lived refresh
token. `/auth/login` and `/auth/register` return the pair, `/auth/refresh`
returns a new access token, and `/auth/social` trades an Apple or Google
identity token from the phone for the same pair. `/auth/me` returns the
profile. That work belongs with the account tickets, not with these endpoints.

## Functions shared by the website and the API

| Feature | Shared function | Status |
|---|---|---|
| Scores of squares | `map_api.published.read_squares` | Shared. The `/squares` endpoint and the landing example cards both call it. |
| Available dates | `map_api.published.PublishedMap.available_dates` | Shared. `/info` and `landing_examples` both call it with `weather_on_demand.services.italian_assessment_date`. |
| Area details | `area_api.router.read_area_detail` | Shared. The `/areas/{square_id}` endpoint and `landing_top_areas._read_detail` both call it. It returns the detail body or the API error. |
| Best areas | `web_ui.landing_top_areas.top_areas` today | Only the landing page calls it. It lives in `web_ui` and imports the private `_offset` from `map_api.published`. It should move to `map_api` so the new endpoint and the landing page both call it. |

The map page renders 8 dates on the server with `web_ui.views._dates`, from
`timezone.localdate`. The browser then replaces them with the dates from
`/api/v1/info`. A phone app only uses `/info`, so this does not need a shared
function.

## Follow-up tickets

Under #203, website:

- #207, size S. Add a plain function in `area_api` that returns the detail
  body of one square for one date and update. Make `area_detail` and
  `landing_top_areas._read_detail` call it, so the landing page stops
  building a fake request and parsing JSON.
- #208, size M. Move the best areas ranking from
  `web_ui/landing_top_areas.py` to `map_api`, give `_offset` a public name,
  and keep the landing page showing the same areas and scores. It depends on
  #207.

Under #204, endpoints:

- #209, size M. Add `GET /api/v1/top-areas` with the fields above. It calls
  the function from #208. It adds the endpoint to `docs/api.md`, starts
  `docs/api_changelog.md` and adds it to the schema test. It depends on
  #208.
- #210, size S. Add response schemas to `/places/search`, `/places/resolve`
  and `/estimate-overview`, so the published API description lists their
  output fields for app code. The JSON they return stays the same.

## Not covered

The details panel asks `/api/v1/areas/{square_id}` once for the chosen date
and once more for each of the other dates, so a new square costs up to 9
requests. The browser keeps the answers for the last 12 squares. All endpoints
share one count of 150 requests per minute for each client address, as
`docs/api.md` describes. A person who opens about 16 new squares in one minute
reaches the limit, and moving the map lowers that number. This report does not
propose a change, because few people open squares that fast and no app exists
yet. If the limit starts to block real users, one `/api/v1/areas/{square_id}`
request could return the scores of all 8 dates.

Phone carriers often send many phones through one shared address. Those phones
would share one count. Telling them apart needs a key for each app install,
which belongs with app sign-in.
