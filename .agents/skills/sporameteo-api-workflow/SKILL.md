---
name: sporameteo-api-workflow
description: Keep the SporaMeteo website and its /api/v1 API in step. Use when adding or changing a website feature that reads or writes data, when changing an API endpoint, request or response field, error, request limit or OpenAPI schema, or when asked for a website-only change.
---

# SporaMeteo API workflow

The website and a future phone app read the same data. The website must get
that data from the same `/api/v1/` endpoints a phone app would call, so the
app never needs a feature the API lacks.

The API code lives in `apps/application/config/api.py`,
`apps/application/map_api/`, `apps/application/area_api/` and
`apps/application/place_search/`.

## Rules

1. A website feature that reads or writes data gets that data from an
   `/api/v1/` endpoint. The only exceptions are the features that the
   "Website only" table of issue #37 lists, such as F65 and F80.
2. When someone asks for a website-only change, ask whether a phone app needs
   it too before you start.
3. Changes inside version 1 only add things, such as new endpoints and new
   optional fields. Removing or renaming a field, or changing what a field
   means, needs a new version under a new path such as `/api/v2/`.
4. Each API change updates `docs/api.md` and adds an entry to
   `docs/api_changelog.md`.
5. Before finishing, run the API tests in `tests/app/` and the schema test.

## Changelog entries

Add the entry under the version in `version=` of `NinjaAPI` in
`apps/application/config/api.py`. Raise the middle number of that version for
a new endpoint or field, and the last number for a fix that keeps every
field and its meaning. Write one line for each change, in plain words, and
name the route and the field.

## Website calls in the schema test

`tests/app/test_openapi_schema.py` lists every endpoint that
`apps/application/web_ui/static/web_ui/app.js` calls, with the response codes
the schema must declare. Update `WEB_PAGE_CALLS` in the same change when the
website calls a new endpoint or handles a new response code.

## Checks

Run the API tests and the schema test before finishing:

```sh
uv run pytest -n 4 tests/app/test_api_errors.py tests/app/test_api_limits.py \
  tests/app/test_area_api.py tests/app/test_map_api.py \
  tests/app/test_published_forecast_api.py tests/app/test_openapi_schema.py
```

Add any new API test file to this command. Then confirm that `docs/api.md` describes the final behaviour and that
`docs/api_changelog.md` has an entry for the change.
