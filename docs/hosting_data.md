# Hosting data

This page lists the data the VPS needs. The laptop database
`sporameteo_native` holds the national build, about 2.2 GB, plus laptop weather
and estimates. The VPS receives a Tuscany-only dump instead. Every table that
`python manage.py migrate` creates appears below with one label, so a table
cannot go missing without notice.

## Tuscany-only dump

The dump was built on 15 October 2026 from `master` at commit `965bfd2` plus
the changes that store the forest layer path relative to the repository and
add the `--skip-source-files` option of the Tuscany input preparation, now
`prepare_region_inputs --regions 09`.

- Source database: `sporameteo_tuscany_vps`, 221 MB on the laptop.
- Dump file: `data/processed/hosting/sporameteo_tuscany_vps.dump`. Git ignores
  `data/processed/`.
- Dump size: 19,420,724 bytes, about 19 MB.
- SHA-256: `83a61760f76cc47985f55b975f2eab8fb924690a86f0978c10bc41bb0de3f012`.
- Grid: `tuscany-grid-italy-500m-istat-2026-01-01-v1-56`, 92,707 squares, all
  in ISTAT region 09.
- Forest: `tuscany-forest-italy-forest-copernicus-2018-v1-56`.
- Terrain: `tuscany-terrain-italy-terrain-glo30-2021-v2-56`.
- JRC host trees: manifest SHA-256
  `d8998582df944b32f134ef85beb51380e255e4702d6714ebb01b5bd3029ddd0a`.

### Build the dump again

Run these commands from the repository root with the database running. The
national database `sporameteo_native` must hold the completed national grid,
forest and terrain builds. The JRC files must already be in
`data/raw/jrc_host_trees`, so the last preparation step downloads nothing.

```sh
set -eu
docker compose exec -T database createdb -U sporameteo sporameteo_tuscany_vps
export SPORAMETEO_DB_NAME=sporameteo_tuscany_vps
export SPORAMETEO_SOURCE_DB_NAME=sporameteo_native
uv run python apps/application/manage.py migrate --noinput
uv run python apps/application/manage.py prepare_region_map_database --regions 09 \
  --grid-build-version tuscany-grid-italy-500m-istat-2026-01-01-v1-56 \
  --forest-build-version tuscany-forest-italy-forest-copernicus-2018-v1-56 \
  --terrain-build-version tuscany-terrain-italy-terrain-glo30-2021-v2-56
uv run python apps/application/manage.py load_municipalities
export SPORAMETEO_FOREST_BUILD_VERSION=tuscany-forest-italy-forest-copernicus-2018-v1-56
export SPORAMETEO_TERRAIN_BUILD_VERSION=tuscany-terrain-italy-terrain-glo30-2021-v2-56
uv run python apps/application/manage.py prepare_region_inputs --regions 09 --json
mkdir -p data/processed/hosting
docker compose exec -T database pg_dump \
  --format=custom --no-owner --no-acl \
  -U sporameteo -d sporameteo_tuscany_vps \
  > data/processed/hosting/sporameteo_tuscany_vps.dump
```

On the laptop, the Tuscany map database preparation took 41 seconds, the
Tuscany input preparation took 44 seconds to sample host trees for 71,031
squares, and `pg_dump` took 2 seconds. Those two commands are now
`prepare_region_map_database --regions 09` and
`prepare_region_inputs --regions 09`. The map database command also rewrote
the Tuscany forest layer file. The new file had the same SHA-256 as the old
one.

### Restore check

The dump was restored into a new empty database made from `template0`:

```sh
docker compose exec -T database createdb -U sporameteo \
  --template=template0 sporameteo_tuscany_restore_check
docker compose exec -T database pg_restore \
  --no-owner --no-acl --exit-on-error \
  -U sporameteo -d sporameteo_tuscany_restore_check \
  < data/processed/hosting/sporameteo_tuscany_vps.dump
```

The restore took 3 seconds. The check then ran from a copy of `apps/` in
another folder, laid out the way the VPS will be. The copy held the forest
layer file and the 37 JRC files the app reads. It held no forest source files
and no terrain tiles:

```sh
SPORAMETEO_DB_NAME=sporameteo_tuscany_restore_check \
SPORAMETEO_FOREST_BUILD_VERSION=tuscany-forest-italy-forest-copernicus-2018-v1-56 \
SPORAMETEO_TERRAIN_BUILD_VERSION=tuscany-terrain-italy-terrain-glo30-2021-v2-56 \
SPORAMETEO_JRC_DIRECTORY=/path/to/jrc \
python apps/application/manage.py prepare_region_inputs --regions 09 \
  --dry-run --json --skip-source-files
```

The dry run took 0.6 seconds and reported `ready` for
`administrative_geometry`, `forest`, `terrain` and `host_trees`. The forest
and terrain entries carry the detail `Source files were not checked.` It ran
with the HTTP and HTTPS proxy variables pointed at a closed local port and
made no download. Without `--skip-source-files`, the same run reports
`forest` and `terrain` as `missing` and names the first absent file.

`/api/v1/forest-layer` answered 200 from the same copy, so the relative layer
path works outside the laptop repository.

Queries on the restored copy found:

- one grid region, with ISTAT code 09,
- no square owned by another region and no square without a region,
- no stored grid square outside the Tuscany build,
- no weather, estimate or map cache rows.

## Tables

The labels mean:

- Restored: the rows come from the dump.
- Fetched again: the VPS fills the table from the weather provider.
- Built by the app: the app or its worker fills the table while it runs.
- Left empty: nothing in the hosted app writes to the table.

`migrate` creates 55 tables. The row counts come from
`sporameteo_tuscany_vps` before the dump.

### Restored

| Table | Rows |
|---|---|
| `national_grid_gridbuild` | 1 |
| `national_grid_gridchunk` | 71 |
| `national_grid_gridregion` | 1 |
| `national_grid_gridcell` | 92,707 |
| `national_grid_gridbuildcell` | 92,707 |
| `national_grid_municipality` | 273 |
| `national_forest_forestbuild` | 1 |
| `national_forest_forestchunk` | 71 |
| `national_forest_forestcell` | 92,707 |
| `national_terrain_terrainbuild` | 1 |
| `national_terrain_terraincell` | 92,707 |
| `national_terrain_terrainchunk` | 71 |
| `national_terrain_terrainchunksource` | 116 |
| `national_terrain_terrainsourcetile` | 11 |
| `estimate_updates_hosttreecache` | 71,031 |
| `estimate_updates_tuscanyinputset` | 1 |
| `django_migrations` | 63 |
| `django_content_type` | 49 |
| `auth_permission` | 196 |
| `spatial_ref_sys` | 8,500 |

`spatial_ref_sys` belongs to PostGIS. The dump creates the PostGIS extension,
and the extension fills this table.

### Fetched again on the VPS

| Table | Rows |
|---|---|
| `weather_on_demand_scheduledweatherrun` | 0 |
| `weather_on_demand_weatherfetch` | 0 |
| `weather_on_demand_weatherfetchbatch` | 0 |
| `weather_on_demand_weatherfetchpart` | 0 |
| `weather_requests_weatherrequest` | 0 |
| `weather_requests_weatherrequestattempt` | 0 |
| `weather_requests_weatherrequestmember` | 0 |
| `weather_summaries_weatherdaily` | 0 |
| `weather_summaries_weathersnapshot` | 0 |
| `weather_summaries_weathersnapshotday` | 0 |
| `weather_summaries_weathersummary` | 0 |

### Built by the app

| Table | Rows |
|---|---|
| `estimate_updates_estimateresult` | 0 |
| `estimate_updates_estimatetask` | 0 |
| `estimate_updates_estimateupdate` | 0 |
| `estimate_updates_forecastrun` | 0 |
| `estimate_updates_forecastruncell` | 0 |
| `estimate_updates_publishedforecastrun` | 0 |
| `sporameteo_map_cache` | 0 |
| `background_jobs_backgroundjob` | 0 |
| `background_jobs_backgroundjobattempt` | 0 |
| `django_tasks_database_dbtaskresult` | 0 |
| `place_search_placesearchcache` | 0 |
| `place_search_placesearchthrottle` | 0 |
| `weather_on_demand_weathercleanupjob` | 0 |
| `weather_on_demand_weathervisitorwindow` | 0 |
| `weather_on_demand_weathervisitorreservation` | 0 |
| `weather_requests_weatherquotalock` | 0 |
| `django_session` | 0 |

The place search cache stores Photon answers as visitors search. The weather
visitor, cleanup and quota tables hold the app's own limits and bookkeeping,
not provider data.

### Left empty

| Table | Rows |
|---|---|
| `auth_user` | 0 |
| `auth_group` | 0 |
| `auth_group_permissions` | 0 |
| `auth_user_groups` | 0 |
| `auth_user_user_permissions` | 0 |
| `background_jobs_representativeinput` | 0 |
| `background_jobs_representativeresult` | 0 |

The app has no accounts and no admin site. The representative job tables
belong to the worker example in `docs/local_development.md`.

## Files

### JRC files the app reads

Copy these files from `data/raw/jrc_host_trees` to the folder that
`SPORAMETEO_JRC_DIRECTORY` names on the VPS:

- `manifest.json`,
- the `.tif` file of each of the 18 species,
- the `.json` file of each of the 18 species.

The 37 files take 358,725,015 bytes, about 359 MB. The 18 `_rpp.zip` archives
take 315,002,972 bytes. They are only used while preparing and stay off the
VPS.

### Forest layer file

The map reads the Tuscany forest layer through `/api/v1/forest-layer`. Copy
this 208,655 byte file to the same place under the repository on the VPS. The
image leaves out `data/`, so on the VPS the file sits in the `mvp_forest`
storage folder that `docs/hosting.md` mounts:

```text
data/processed/mvp_forest/tuscany-forest-italy-forest-copernicus-2018-v1-56/forest_layer_2000m.geojson.gz
```

The forest build in the dump stores this path relative to the repository
root, so the repository can live in any folder. Without the file, the forest
layer request answers 503.

### Files that stay on the laptop

`prepare_region_inputs --regions 09` normally checks the checksums of the source files
behind the forest and terrain builds. The app does not read them while it
runs, so they stay off the VPS:

- 71 forest source files under `data/raw/mvp_forest`, 456,054,362 bytes.
- 11 terrain tiles under `data/raw/mvp_terrain`, 353,867,033 bytes.

The full check runs on the laptop before the dump. On the VPS, run it with
`--skip-source-files`. It still checks every database row, the forest layer
file and the JRC files.
