# API changelog

This file records every change to the `/api/v1/` API. Each version heading
matches `version=` of `NinjaAPI` in `apps/application/config/api.py`.
`docs/api.md` describes the current behaviour.

## 1.1.1

The answers stay the same. The API description now lists their fields:

- `GET /api/v1/places/search` declares `PlaceSearchResponse` with `results`,
  `cached`, `complete`, `minimum_length`, `provider` and `attribution`. Each
  entry of `results` has `name`, `context`, `latitude`, `longitude`, `bounds`
  and `osm_id`. The errors 400, 429, 502 and 504 declare `PlaceErrorSchema`
  with `code` and `message`.
- `POST /api/v1/places/resolve` declares `PlaceResolveResponse` with
  `status`, `point`, `representative`, `square_id`, `region`, `coverage`,
  `source` and `resolved_at`. The errors 400 and 503 declare
  `PlaceErrorSchema`. The error 422 declares `PlaceErrorSchema` for a point
  outside the map and `ApiErrorSchema` for an invalid body.
- `GET /api/v1/estimate-overview` declares `OverviewResponse` with `type`,
  `run_id`, `assessment_date`, `target_date`, `area_size_m` and `features`.
  Each feature has `overview`, `target_date`, `state`, `percentage`,
  `assessment_date`, `calculated_at`, `forest_square_count` and
  `scored_square_count` in `properties`.

## 1.1.0

- `GET /api/v1/top-areas` returns the best scoring municipalities of the
  first available date. It takes no input. It returns `target_date`, `run_id`
  and `areas`, a list of up to 8 entries with `rank`, `square_id`,
  `update_id`, `municipality`, `region`, `latitude`, `longitude` and
  `percentage`.

## 1.0.0

The first version. It has these endpoints:

- `GET /api/v1/health` reports whether the application and its database
  answer. Request limits do not apply to it.
- `GET /api/v1/info` returns today's Italian date, the published map, the run
  of each region, the dates the map offers, the coverage and the map limits.
- `GET /api/v1/forest-layer` returns the simplified national forest polygons.
- `GET /api/v1/squares` returns the squares and stored estimates of the
  published regions inside the visible map, for one date.
- `GET /api/v1/estimate-overview` returns average scores in larger areas, for
  views wider than the square limit.
- `GET /api/v1/areas/{square_id}` returns one saved estimate and the inputs
  behind it.
- `GET /api/v1/places/search` finds Italian places by name.
- `POST /api/v1/places/resolve` finds the grid square that holds exact
  coordinates, or says that the point is outside Italy or outside the
  prepared area.
- `GET /api/v1/openapi.json` returns the generated schema, and
  `GET /api/v1/docs` shows the interactive reference.

Every failure returns the same error shape. Each client address has a
request limit, and a client over the limit gets HTTP 429.
