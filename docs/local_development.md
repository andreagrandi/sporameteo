# Local development

This guide runs the SporaMeteo Django application on the laptop and a PostGIS
database in Docker. It needs no public domain, hosting account or paid service.

## Database image and volume

Compose and GitHub Actions use the pinned `imresamu/postgis` community image.
It is a free experimental image with native `linux/arm64` and `linux/amd64`
support. The image contains PostgreSQL 17.6 and PostGIS 3.5.3. The previous
`postgis/postgis:17-3.5` image used PostgreSQL 17.5 and PostGIS 3.5.2 and ran
through AMD64 emulation on Apple Silicon.

Docker must choose the native image on Apple Silicon. Remove any
`DOCKER_DEFAULT_PLATFORM=linux/amd64` setting from `.envrc` and run
`unset DOCKER_DEFAULT_PLATFORM` after loading the environment.

The service now uses the `sporameteo_pgdata_native` Compose volume. With the
default project name, Docker names it
`sporameteo_sporameteo_pgdata_native`. The old physical volume
`sporameteo_sporameteo_pgdata` remains available. Compose does not copy data
between these volumes.

## Clean setup

Run these commands from the repository root. Install `uv`, Docker with Compose,
Homebrew and `direnv` first. Start Docker and enable the direnv hook for your
shell. The lockfile uses Python 3.14, which `uv sync` installs if needed.

Install the native libraries that GeoDjango uses on macOS:

```sh
brew install gdal geos proj libpq gettext
```

Copy the environment template only when `.envrc` does not exist. This keeps an
existing file and its GBIF settings unchanged:

```sh
if [ ! -e .envrc ]; then cp .envrc.example .envrc; fi
```

If `.envrc` already exists, add the local application settings from
`.envrc.example` without replacing the existing GBIF settings. Set
`SPORAMETEO_DJANGO_SECRET_KEY` and `SPORAMETEO_DB_PASSWORD` to local values.
Keep the database name, user and port at their defaults for the commands below.
Add the Homebrew GEOS library path to `.envrc`:

```sh
export GEOS_LIBRARY_PATH="$(brew --prefix geos)/lib/libgeos_c.dylib"
```

On macOS, Django uses the GDAL library bundled with the locked Rasterio wheel.
Homebrew GDAL loads another Arrow library, which conflicts with PyArrow during
Parquet reads and writes. The application therefore prefers the bundled copy
over `GDAL_LIBRARY_PATH`. Use `SPORAMETEO_GDAL_LIBRARY_PATH` only to select a
compatible custom build. On systems without the bundled library, Django uses
`GDAL_LIBRARY_PATH` or its normal native-library discovery.

Load the environment and install the locked Python environment:

```sh
direnv allow
unset DOCKER_DEFAULT_PLATFORM
uv sync --locked
```

Check and start the database. Compose publishes it only on the laptop loopback
address, so it does not listen on the local network:

```sh
docker compose config --quiet
docker compose up --wait database
docker compose ps
docker compose port database 5432
```

The last command reports `127.0.0.1:5542` with the default settings. Run the
Django migrations and start the application on its fixed local address:

```sh
uv run python apps/application/manage.py migrate
uv run python apps/application/manage.py runserver 127.0.0.1:8010
```

`runserver` serves the CSS, JavaScript and logo only when
`SPORAMETEO_DEBUG=true`. With debug off, the pages load without styles and the
map stays blank. The production image serves static files with WhiteNoise, see
the section on the production image below.

In another terminal, check the API:

```sh
curl --fail-with-body --silent --show-error http://127.0.0.1:8010/api/v1/health
```

The available response has these fields:

```json
{"application":"available","database":"available"}
```

The endpoint returns HTTP 503 and this body when PostgreSQL is unavailable:

```json
{"application":"available","database":"unavailable"}
```

It never includes database settings or connection error details.

## Production image

The `Dockerfile` builds the image that Dokku runs. It builds the CSS, copies the
MapLibre files, compiles the translations and collects the static files. The
`Procfile` holds the web and worker commands.

Build the image and run the web process against the local database. A container
reaches the laptop through `host.docker.internal`. The image reads the database
from `DATABASE_URL`, which wins over the `SPORAMETEO_DB_*` values:

```sh
docker build -t sporameteo:local .
docker run --rm -p 8020:8000 \
  -e DATABASE_URL="postgis://${SPORAMETEO_DB_USER}:${SPORAMETEO_DB_PASSWORD}@host.docker.internal:${SPORAMETEO_DB_PORT}/${SPORAMETEO_DB_NAME}" \
  -e SPORAMETEO_ALLOWED_HOSTS=localhost,127.0.0.1 \
  sporameteo:local
```

The image starts gunicorn with 2 workers on port 8000, or on `PORT` when it is
set.

Then open `http://localhost:8020/`. The HTTPS settings stay off unless you set
`SPORAMETEO_SECURE_SSL_REDIRECT`, `SPORAMETEO_SESSION_COOKIE_SECURE`,
`SPORAMETEO_CSRF_COOKIE_SECURE` or `SPORAMETEO_SECURE_HSTS_SECONDS`. On Linux, the
image uses the system GDAL library, and Parquet reads and writes work after
Django loads it.

## Database checks

Run the focused application checks while the database is healthy:

```sh
uv run pytest tests/app/test_application_setup.py
```

The checks query `PostGIS_Version`, store and read a temporary WGS84 point,
check the database UTC session, and verify both health responses. They create
no product tables. Product dates use `Europe/Rome`, while stored event
timestamps use UTC.

To check the unavailable and recovery responses manually, stop and start the
database while the Django server remains running:

```sh
docker compose stop database
curl --silent --show-error --include http://127.0.0.1:8010/api/v1/health
docker compose start database
docker compose up --wait database
curl --fail-with-body --silent --show-error http://127.0.0.1:8010/api/v1/health
```

The named `sporameteo_pgdata_native` volume keeps database contents across service
restarts. Compare a migration row before and after recreating the container:

```sh
docker compose exec database psql \
  -U "${SPORAMETEO_DB_USER:-sporameteo}" \
  -d "${SPORAMETEO_DB_NAME:-sporameteo}" \
  -c "SELECT id, app, name, applied FROM django_migrations ORDER BY id LIMIT 1;"
docker compose down
docker compose up --wait database
docker compose exec database psql \
  -U "${SPORAMETEO_DB_USER:-sporameteo}" \
  -d "${SPORAMETEO_DB_NAME:-sporameteo}" \
  -c "SELECT id, app, name, applied FROM django_migrations ORDER BY id LIMIT 1;"
```

`docker compose down` removes the container but keeps the named volume.
`docker compose down --volumes` removes the local database contents and is only
needed for a deliberate reset.

## Optional copy of existing development data

The new volume starts empty. Run migrations and rebuild the local data for a
fresh development setup. There is no user-data migration required.
The old physical volume `sporameteo_sporameteo_pgdata` stays in place.
The optional commands below keep existing development and benchmark data
without rebuilding it. Stop application writers while taking the dumps.
These commands use the default Compose project name.

When the old database container still exists, record it before the first new
`docker compose up`:

```sh
set -eu
OLD_CONTAINER="$(docker compose ps -q database)"
test -n "$OLD_CONTAINER"
test "$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' "$OLD_CONTAINER")" = sporameteo_sporameteo_pgdata
```

Create custom dumps for the databases to keep. Add another dump command for
each database that the new volume must contain:

```sh
set -eu
MIGRATION_DIR=/tmp/sporameteo-db-migration
mkdir -p "$MIGRATION_DIR"
docker exec "$OLD_CONTAINER" pg_dump \
  --format=custom --no-owner --no-acl \
  --username="${SPORAMETEO_DB_USER:-sporameteo}" \
  --dbname=sporameteo \
  > "$MIGRATION_DIR/sporameteo.dump"
docker exec "$OLD_CONTAINER" pg_dump \
  --format=custom --no-owner --no-acl \
  --username="${SPORAMETEO_DB_USER:-sporameteo}" \
  --dbname=sporameteo_estimate_benchmark_20261012 \
  > "$MIGRATION_DIR/sporameteo_estimate_benchmark_20261012.dump"
```

Stop the old service, start the new volume, restore the application dump into
a new database made from `template0`, then restore the benchmark dump into a
separate database:

```sh
set -eu
docker compose stop database
docker compose up --wait database
docker compose exec -T database createdb \
  --username="${SPORAMETEO_DB_USER:-sporameteo}" \
  --template=template0 \
  sporameteo_native
docker compose exec -T database pg_restore \
  --no-owner --no-acl --exit-on-error \
  --username="${SPORAMETEO_DB_USER:-sporameteo}" \
  --dbname=sporameteo_native \
  < "$MIGRATION_DIR/sporameteo.dump"
docker compose exec -T database createdb \
  --username="${SPORAMETEO_DB_USER:-sporameteo}" \
  --template=template0 \
  sporameteo_estimate_benchmark_20261012
docker compose exec -T database pg_restore \
  --no-owner --no-acl --exit-on-error \
  --username="${SPORAMETEO_DB_USER:-sporameteo}" \
  --dbname=sporameteo_estimate_benchmark_20261012 \
  < "$MIGRATION_DIR/sporameteo_estimate_benchmark_20261012.dump"
export SPORAMETEO_DB_NAME=sporameteo_native
uv run python apps/application/manage.py migrate --noinput
```

The new Compose volume keeps its initialized `sporameteo` database. The
restored application database uses `sporameteo_native`, so no initialized
database or PostGIS extension is removed. Set `SPORAMETEO_DB_NAME=sporameteo_native`
in `.envrc` as well when the restored database should be the default for later
shells. The dump options restore data without depending on source ownership
or access-control rows. The old database remains available on its old volume.

If the old container was already replaced, stop the new service and start a
temporary old-image container on the preserved physical volume:

```sh
docker compose stop database
docker run --rm --detach --name sporameteo-old-volume-recovery \
  --platform linux/amd64 \
  --mount source=sporameteo_sporameteo_pgdata,target=/var/lib/postgresql/data \
  --publish 127.0.0.1:5543:5432 \
  --env POSTGRES_DB="${SPORAMETEO_DB_NAME:-sporameteo}" \
  --env POSTGRES_USER="${SPORAMETEO_DB_USER:-sporameteo}" \
  --env POSTGRES_PASSWORD="${SPORAMETEO_DB_PASSWORD:-replace-with-a-local-password}" \
  postgis/postgis:17-3.5
until docker exec sporameteo-old-volume-recovery pg_isready \
  --username="${SPORAMETEO_DB_USER:-sporameteo}" --dbname=postgres; do sleep 1; done
OLD_CONTAINER=sporameteo-old-volume-recovery
```

Run the dump block above after setting `OLD_CONTAINER`, then run the restore
block. Stop the temporary container after the restore with
`docker stop sporameteo-old-volume-recovery`. The `--rm` option removes that
temporary container only. It does not remove either database volume.

If the benchmark database does not exist in the old volume, skip its dump and
restore commands. Keep the application dump and restore commands.

If the old data is disposable, use the new empty volume instead:

```sh
docker compose up --wait database
uv run python apps/application/manage.py migrate --noinput
```

Then rerun the documented [national grid](#national-grid),
[national terrain](#national-terrain) and [national forest](#national-forest-build)
builds. Recreate the disposable estimate database with the
[local benchmark](estimate_updates.md#local-benchmark) command.

## Checks

Run these checks from the repository root. The commands use the locked Python
environment, so run `uv sync --locked` before the first check. The investigation
checks need GDAL, GEOS and PROJ. The Clean setup section lists the macOS
packages. On Linux, install `gdal-bin`, `libgdal-dev`, `libgeos-dev` and
`libproj-dev` with the system package manager.

Network access is needed to install uv and Python, resolve PyPI packages, fetch
the pre-commit hook repositories and pull the PostGIS image. The checks do not
use that network access for provider calls.

### File checks without a database

These commands match the file checks job in GitHub Actions:

```sh
uv sync --locked
uv run pre-commit run --all-files
uv run pre-commit run gitleaks-history --hook-stage manual --all-files
```

The normal gitleaks hook scans files during the usual staged-file checks. The
manual hook scans repository history, so run it explicitly when that check is
needed. The `pytest-socket` guard blocks Python socket calls made while pytest
is running outside `127.0.0.1`, `127.0.1.1` and `::1`. It does not cover module
initialisation, native geographic libraries or subprocesses. Provider fixtures
must patch the function that opens the external request and return a small
fixed or generated answer. Worker tests use the local database, and the demo
API test uses a loopback server. These checks do not need weather, observation
or hosting credentials and do not download national data.

### Application and worker checks with PostGIS

The application checks require the local PostGIS service and the native
geographic libraries. Start the service, check the configuration and
migrations, apply migrations, then run the application and worker tests:

```sh
docker compose up --wait database
uv run python apps/application/manage.py check
uv run python apps/application/manage.py makemigrations --check --dry-run
uv run python apps/application/manage.py migrate --noinput
uv run pytest tests/app
```

The first command starts or reuses the local service, while the `pytest-django`
plugin creates and drops a fresh test database for each run. Do not add
`--reuse-db`.
The configured database user needs permission to create databases. PostGIS is
required. A missing service or native geographic library fails these checks
instead of skipping geographic behavior or using SQLite. The Django task
backend remains the database backend in these tests.

These commands are local checks. GitHub Actions runs the same three jobs on
pull requests and pushes to `master`. A local pass does not report a remote CI
pass. Check the workflow result after an authorized publication.

Provider tests use fixed or generated responses. Later national acceptance with
real weather and observation downloads belongs to #61 and needs its own data
and credentials. The web UI serves `/` and `/map/`. Fixture pages are disabled
by default and are enabled with `SPORAMETEO_UI_FIXTURE_PAGES=true` for browser
checks.

### Web UI checks

```sh
uv sync --locked
npm ci
npm run build:css
git diff --exit-code -- apps/application/web_ui/static/web_ui/app.css apps/application/web_ui/static/web_ui/landing.css
msgfmt --check apps/application/web_ui/locale/it/LC_MESSAGES/django.po
msgfmt --check apps/application/web_ui/locale/en/LC_MESSAGES/django.po
msgfmt --check apps/application/web_ui/landing_locale/it/LC_MESSAGES/django.po
uv run python apps/application/manage.py compilemessages --ignore .venv --locale it --locale en
uv run playwright install chromium webkit
uv run pytest tests/e2e
```

The e2e fixture starts Django on a free loopback port with
`SPORAMETEO_UI_FIXTURE_PAGES=true`. Set `SPORAMETEO_BASE_URL` to reuse an
existing local server. The fixture routes contain labelled sample states and
values for checks only. The normal map shell does not copy mockup data into the
product page. The e2e server is a subprocess and does not use pytest-django's
test database, so running it with the full suite does not create a second test
database.

Pull requests and pushes to `master` run the browser checks in chromium only.
CI splits them into three groups that run at the same time on separate
machines, each with four parallel workers. CI has no WebKit run.
`SPORAMETEO_E2E_SHARD=2/3` runs the second of three groups locally. A checksum
of each test id picks its group.

Run the checks in parallel locally with `uv run pytest tests/e2e -n 4`. This
uses the browsers from the pytest settings in `pyproject.toml`, which are
chromium and webkit. Each worker starts its own Django server.

To look for unstable tests, repeat them with `--count`:

```sh
uv run pytest -n 6 --count=20 \
  tests/e2e/test_favorites.py::test_favorites_missing_grid_cell_keeps_saved_location_without_neighbor \
  tests/e2e/test_map_controls.py::test_failed_squares_request_offers_retry_without_losing_the_selected_date \
  tests/e2e/test_area_details.py::test_area_detail_renders_host_source_provenance_and_quality \
  tests/e2e/test_map_controls.py::test_selected_square_area_failure_can_retry_without_cached_unavailable_result \
  tests/e2e/test_place_search.py::test_touch_place_selection_renders_square_and_preserves_day \
  tests/e2e/test_map_controls.py::test_map_renders_eight_calendar_dates_and_preserves_an_older_target
```

CI machines are slower than a laptop. Set `SPORAMETEO_E2E_CPU_SLOWDOWN` to a
number such as `4` to make chromium run that many times slower and reproduce CI
timing. Webkit ignores it.

### Full local suite

After the two focused groups pass with PostGIS running, run the combined suite:

```sh
uv run pytest
```

## Phone access through Tailscale

The application works on the laptop without Tailscale. Tailscale Serve gives a
phone on the owner's private Tailscale network an HTTPS address, which browsers
need for location access. It does not publish the application to the internet.

Install and connect Tailscale on the laptop and phone using the same network.
The commands below also need `jq`. Find this laptop's certificate hostname and
check whether another application already uses Serve:

```sh
TAILSCALE_HOST="$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')"
printf '%s\n' "$TAILSCALE_HOST"
tailscale serve status
```

Tailscale publishes the certificate name, and therefore the network name, in
public certificate logs. See the [Tailscale HTTPS documentation](https://tailscale.com/docs/how-to/set-up-https-certificates).
Set the hostname in `.envrc` before starting or restarting Django:

```sh
export SPORAMETEO_ALLOWED_HOSTS=127.0.0.1,localhost,laptop.tailnet-name.ts.net
export SPORAMETEO_CSRF_TRUSTED_ORIGINS=https://laptop.tailnet-name.ts.net
```

Replace the example hostname with the value from `tailscale status`. Reload the
environment and restart `runserver`, then configure private HTTPS forwarding:

```sh
direnv allow
tailscale serve --bg --https=443 http://127.0.0.1:8010
tailscale serve status
```

If Serve asks you to enable HTTPS, follow its setup link before continuing.
If another application uses HTTPS port 443, choose port 8443 and add `:8443`
to the trusted origin and phone URL. Use the same port in the Serve shutdown
command. Keep the hostname without a port in `SPORAMETEO_ALLOWED_HOSTS`.

On the phone, open this URL while it is connected to the same Tailscale
network:

```text
https://laptop.tailnet-name.ts.net/api/v1/health
```

Replace the hostname with the value from `TAILSCALE_HOST`. The phone should
receive the available JSON response. Do not enable Tailscale Funnel, which
opens the service to the internet. Turn Serve off with the matching command
when the check is complete:

```sh
tailscale serve --https=443 off
```

## Background jobs

Django stores queued work in PostgreSQL through `django-tasks-db`. Run the
worker in another terminal after applying migrations:

```sh
uv run python apps/application/manage.py migrate
uv run python apps/application/manage.py run_background_worker
```

Enqueueing a job returns control to the caller. The worker reads the persisted
input and performs the work in its own process. Normal development settings
always use the database queue. Tests that need immediate execution select
that backend explicitly.

Each logical job has a stable key and a persisted input ID. The queue receives
only an attempt ID. Do not put geographic datasets or file contents in queue
arguments. A retry creates a new attempt and retains the original failure,
start time and end time. Completed jobs do not run again when a worker restarts.

The worker takes a PostgreSQL lock before claiming a queued job and holds it
until it records the result. Starting another worker cannot take that job's
protected work while the lock is held.
Worker startup checks interrupted attempts through the same lock. It leaves
work owned by a live worker alone and records abandoned work as failed.
Interrupted work requires an explicit retry.

### Enqueue and inspect a job

Create a small persisted input. This example doubles 21 without downloading
anything:

```sh
INPUT_ID="$(uv run python apps/application/manage.py shell --verbosity 0 -c \
  'from background_jobs.services import create_representative_input; print(create_representative_input(value=21).id)')"
uv run python apps/application/manage.py enqueue_representative_job "$INPUT_ID" \
  --logical-key local-worker-check
uv run python apps/application/manage.py background_job_status local-worker-check
```

The enqueue command prints the job, attempt and queue task IDs. With the worker
stopped, the state remains `waiting`. Start the worker in a separate terminal,
or process the current queue once:

```sh
uv run python apps/application/manage.py run_background_worker --batch
uv run python apps/application/manage.py background_job_status local-worker-check
```

The state becomes `completed`. Repeating the enqueue command with the same
input and logical key returns the same attempt. It does not repeat the work.
Use a new logical key for another independent run.

### Failure, retry and restart

Create an input whose first attempt fails deliberately:

```sh
FAILURE_INPUT_ID="$(uv run python apps/application/manage.py shell --verbosity 0 -c \
  'from background_jobs.services import create_representative_input; print(create_representative_input(value=3, fail_first_attempt=True).id)')"
uv run python apps/application/manage.py enqueue_representative_job "$FAILURE_INPUT_ID" \
  --logical-key local-retry-check
uv run python apps/application/manage.py run_background_worker --batch
uv run python apps/application/manage.py background_job_status local-retry-check
```

Copy the printed failed `attempt_id` into the command below:

```sh
FAILED_ATTEMPT_ID=replace-with-the-failed-attempt-id
uv run python apps/application/manage.py retry_background_job "$FAILED_ATTEMPT_ID"
uv run python apps/application/manage.py run_background_worker --batch
uv run python apps/application/manage.py background_job_status local-retry-check
uv run python apps/application/manage.py background_job_status "$FAILED_ATTEMPT_ID"
```

The new attempt completes with a result of 6. The old attempt still shows its
failure reason and timestamps. Retrying a waiting, running, completed or older
attempt fails rather than creating overlapping work.

Stop a worker with `Ctrl+C` and let its current task finish. Queued tasks stay
in PostgreSQL. Start `run_background_worker` again to resume them. If a worker
was killed before finishing, startup records its abandoned attempt as failed
with `Worker stopped before the job completed.` Inspect it and use the retry
command when it is safe to repeat the work. A failed job does not stop the
worker from processing the next queued job.

### Daily schedule on macOS

This example uses a user LaunchAgent at 03:00 every day. The Mac system
timezone must be `Europe/Rome`. Check it before installing the schedule:

```sh
readlink /etc/localtime
command -v direnv
command -v uv
```

The timezone path must end with `/Europe/Rome`. The schedule follows Italy's
summer and winter clock changes. Use the paths printed by `command -v` in the
example below. Replace `/path/to/sporameteo` and `replace-with-input-uuid` with
the repository path and an input ID created without forced failure. Store this
file as `~/Library/LaunchAgents/it.sporameteo.daily-job.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>it.sporameteo.daily-job</string>
  <key>WorkingDirectory</key><string>/path/to/sporameteo</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/direnv</string>
    <string>exec</string><string>/path/to/sporameteo</string>
    <string>/opt/homebrew/bin/uv</string><string>run</string>
    <string>python</string><string>apps/application/manage.py</string>
    <string>enqueue_representative_job</string><string>replace-with-input-uuid</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/tmp/sporameteo-daily-job.log</string>
  <key>StandardErrorPath</key><string>/tmp/sporameteo-daily-job-error.log</string>
</dict>
</plist>
```

Allow the repository's `.envrc` through direnv before installing the schedule.
Validate, load and manually trigger the LaunchAgent:

```sh
plutil -lint ~/Library/LaunchAgents/it.sporameteo.daily-job.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/it.sporameteo.daily-job.plist
launchctl kickstart "gui/$(id -u)/it.sporameteo.daily-job"
```

This schedules enqueueing only. PostgreSQL must be running when it fires.
The worker must run to process queued work. The schedule runs as the logged-in
user and does not start Docker or keep the laptop awake.

If the laptop sleeps through 03:00, launchd invokes the command once after it
wakes. Several missed sleep-time invocations become one invocation using the
current Italian date. If the laptop is powered off, the run is missed until
the next scheduled time. These rules follow Apple's
[timed job documentation](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html).

Run a missed date explicitly after waking or starting the laptop:

```sh
uv run python apps/application/manage.py enqueue_representative_job "$INPUT_ID" \
  --job-date 2026-10-11
uv run python apps/application/manage.py run_background_worker --batch
```

Replace the example date with the missed date. The default key is
`representative:<input UUID>:<Italian date>`, so repeating that invocation does
not repeat a completed logical job. Disable the schedule with:

```sh
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/it.sporameteo.daily-job.plist
```

Issue #46 can follow the same pattern with its own job name and persisted
input ID. Include the assessment date and any input version in the logical key
when they identify different work. It owns the weather and cleanup rules.
This representative job does not implement them.

## National grid

The application uses the detailed ISTAT boundaries for 1 January 2026. Keep
the original archive outside Git and verify its checksum before building:

```sh
mkdir -p data/raw/mvp_boundaries
curl --fail --location --output data/raw/mvp_boundaries/Limiti01012026.zip \
  https://www.istat.it/storage/cartografia/confini_amministrativi/non_generalizzati/2026/Limiti01012026.zip
shasum -a 256 data/raw/mvp_boundaries/Limiti01012026.zip
uv run python apps/application/manage.py migrate --noinput
uv run python apps/application/manage.py build_national_grid \
  data/raw/mvp_boundaries/Limiti01012026.zip \
  --build-version italy-500m-istat-2026-01-01-v1 \
  --expected-sha256 a9075f8d839dcb2b409099703da7bdd37cc214f8967fa7372b32305875ad046f \
  --downloaded-on 2026-10-12 \
  --chunk-size-m 25000
```

Use your actual download date. Reuse an existing archive when its checksum
matches. If ISTAT changes the archive, verify the new source and use a new
build version rather than relabelling it as this build.

The command processes bounded chunks and stores progress in PostGIS. Repeat
the same command after an interruption to resume unfinished chunks. Lookups
require a completed build. Repeating a completed build reuses its cells.

Every included square has some Italian land, even if its centre lies at sea.
The stored square remains 500 m across. Its Italian land geometry and share
are separate values. The [grid coverage report](../reports/mvp/grid_coverage.md)
records the measured national and regional counts, island checks and credits.

## National terrain

After the national grid has completed, build its terrain values:

```sh
uv run python apps/application/manage.py build_national_terrain \
  --grid-build-version italy-500m-istat-2026-01-01-v1 \
  --build-version italy-terrain-glo30-2021-v2 \
  --cache-dir data/raw/mvp_terrain
```

Repeat the same command to resume unfinished chunks. Completed chunks retain
their values and source references. `--max-chunks 1` limits a trial run to one
unfinished chunk. Use a new version when source or processing inputs change.
The [terrain coverage report](../reports/mvp/terrain_coverage.md) records the
national measurements, source limitations and credit.

## Local JRC host-tree setup

Run the migration and prepare the local files before starting the worker:

```sh
uv run python apps/application/manage.py migrate
uv run python apps/application/manage.py prepare_host_trees
```

The default directory is `data/raw/jrc_host_trees`. Set
`SPORAMETEO_JRC_DIRECTORY` for a different persistent location. API and worker
processes must use the same setting. `--directory` changes the setup command's
destination only, so set the environment variable too when using that option.

Setup downloads 18 JRC archives once, checks their raster format and writes a
manifest only when the complete set is ready. A repeat verifies checksums and
reuses valid files. After an interrupted download, repeat the command. A
changed cached file causes an error. Prepare a new directory for a deliberate
source replacement and restart the worker with the new setting.

Workers read only requested square centres and cache their five host-group
values in PostgreSQL. Neither map requests nor workers download JRC files.
Missing or damaged files cause a logged forest-only fallback for uncached
areas. Details disclose missing tree evidence. Already cached areas can still
use their saved values. Each published update retains its own tree inputs.

The local check on 12 October 2026 downloaded and verified the 18 archives in
65.52 seconds. They occupy 315,002,972 bytes; extracted rasters occupy
358,709,516 bytes. Repeating setup took 0.55 seconds without downloads.
Reading 504 Tuscany square centres took 0.575 seconds including initial
checksum checks and 0.365 seconds on a repeat in the same process.
These are laptop measurements, not hosted service targets.

The hosted one-time bootstrap is tracked separately in [#88](https://github.com/andreagrandi/sporameteo/issues/88).

## Tuscany input readiness

Build the Tuscany subset database first with `prepare_region_map_database --regions 09`.
Then run `load_municipalities` on it. The command reads the `Com01012026`
folder of `data/raw/mvp_boundaries/Limiti01012026.zip`, stores the 273 Tuscan
municipalities and names the municipality of each square by largest overlap.
It prints a JSON summary and a second run changes nothing. Run it before the
app serves area details. Finished details are cached without an expiry, so a
detail cached earlier keeps the plain heading until its cache entry is removed.

```sh
SPORAMETEO_DB_NAME=sporameteo_tuscany_56 \
uv run python apps/application/manage.py load_municipalities
```

Then check and prepare every stored input for the Tuscany map:

```sh
uv run python apps/application/manage.py prepare_region_inputs --regions 09 --dry-run
uv run python apps/application/manage.py prepare_region_inputs --regions 09
```

The report lists four sources: `administrative_geometry`, `forest`, `terrain`
and `host_trees`. Each entry shows its state, source version, the action to
take and the affected area, such as the number of squares without a row or the
file that is missing or changed. The command only accepts a grid that holds
ISTAT region 09 and nothing else.

The dry run changes nothing and makes no remote request. A real run never
rebuilds grid, forest or terrain data. If one of them fails, it stops and
names the build command to run. When they are ready, it downloads the JRC
files only if no manifest exists, then stores host-tree values for every forest
square that has none under the current JRC version. Older versions keep their
rows. A repeat run makes no request and adds no rows. It also records the
verified source versions in `TuscanyInputSet`.

Options:

- `--json` prints the report as JSON.
- `--allow-forest-only` accepts missing or damaged JRC files. The report shows
  state `fallback`, estimates use forest only and nothing is downloaded.
- `--terrain-cache-dir` sets the folder with the terrain tiles, default
  `data/raw/mvp_terrain`.
- `--skip-source-files` skips the checksum checks of the forest source files
  and terrain tiles. The app does not read them while it runs, so a server
  that only runs the app does not hold them. The report adds
  `Source files were not checked.` to the forest and terrain entries.

The command exits with an error when a source is not ready. The dry run always
exits normally, so read its `ready` field in the JSON output.

[Hosting data](hosting_data.md) lists every table and file the VPS needs and
the commands that build the Tuscany-only dump.

## Shutdown

Stop Django and the worker with `Ctrl+C` in their terminals. Let the worker
finish its current job before exiting. Turn off this application's Tailscale
Serve route if it is active, then stop PostGIS:

```sh
docker compose down
```

The database volume remains available for the next `docker compose up --wait database`.

## Directory map

```text
apps/application/manage.py       Django command entry point
apps/application/config/          settings, API, URLs and server entry points
apps/application/background_jobs/ queued jobs, attempts and operator commands
apps/application/national_grid/   versioned national cells and build commands
apps/api/                         P5 synthetic API experiment
apps/web/                         P5 static demonstration
src/sporameteo/                    experiment package and CLI
tests/app/                        Django and PostGIS foundation checks
docker-compose.yml                local PostGIS service only
```

## Book Corners references

The application follows [Book Corners](https://github.com/andreagrandi/book-corners)
patterns from `config/settings.py`, `config/urls.py`, `config/api.py` and
`docker-compose.yml` for environment settings, the PostGIS backend, API routing
and database health checks. SporaMeteo adds an explicit database availability
response to its health endpoint.

Book Corners installs geographic libraries in its `Dockerfile`. SporaMeteo
uses host libraries because Django runs on the host through `uv`. On macOS,
Rasterio supplies GDAL and Homebrew supplies GEOS.
It keeps one lockfile for the application and experiments. Only PostGIS runs
in Compose. The pinned image selects ARM64 on Apple Silicon and AMD64 on
x86-64 hosts without architecture emulation.

Book Corners discovers dynamically assigned ports through `scripts/local_env.py`.
SporaMeteo uses fixed ports 8010 and 5542, and `docker compose port` reports the
database mapping. Change `SPORAMETEO_DB_PORT` before starting Compose if 5542 is
occupied. Choose another `runserver` port if 8010 is occupied, then update the
local trusted origins and Tailscale forwarding target to match.

The worker follows Book Corners `libraries/tasks.py` and `Procfile` by accepting
persisted input IDs and running a separate database worker. SporaMeteo adds
logical job keys, attempt history and interruption recovery. The worker uses
the same host environment as Django.

The checks follow Book Corners `.github/workflows/ci.yml` and `noxfile.py` for
the PostGIS service, native geographic libraries, migration checks and tests.
SporaMeteo runs the commands directly through `uv` and keeps its existing
pre-commit hooks. It adds a Git history secret scan and restricts Python test
connections to loopback addresses. It omits Book Corners deployment steps and
its frontend build because this application has no frontend yet.

The application stays separate from the existing P5 directories. This setup
has no product data tables, account system, frontend build, deployment job or
hosting credentials.

## National forest build

After the national grid completes, build the forest inputs with:

```sh
uv run python apps/application/manage.py build_national_forest \
  --grid-build-version italy-500m-istat-2026-01-01-v1 \
  --build-version italy-forest-copernicus-2018-v1 \
  --cache-dir data/raw/mvp_forest \
  --output-dir data/processed/mvp_forest \
  --max-source-chunk-m 40000
```

Repeat the same command after an interruption. The version fixes the grid,
source release and chunk layout. A completed repeat reuses the stored rows
and source metadata. The national measurements and source gaps are in
`reports/mvp/habitat_coverage.md`.

## Recorded walkthrough

On 2026-10-11, the locked environment ran on macOS with Python 3.14.8 and
Django 6.1.2. Compose started PostGIS on port 5542 and Django applied its
migrations. The focused checks passed against PostGIS, including the
geometry write and read with coordinate system 4326 intact. The full suite
passed after selecting Rasterio's GDAL library. An additional Parquet
regression check passed in the same process as GeoDjango.

The live health endpoint returned HTTP 200 locally. Removing the database
container changed the response to HTTP 503 while the application remained
available. Recreating the container restored HTTP 200 and retained the same
migration row, including its original timestamp. Private HTTPS through
Tailscale Serve returned HTTP 200 with a valid
certificate. The owner confirmed the requested phone health check worked.

The worker walkthrough ran on the same date. With no worker running, enqueueing
returned a waiting attempt. A separate worker completed it with a result of 42.
A deliberately failed attempt retained its reason and timestamps after an
explicit retry completed with a result of 6. Repeating a completed logical key
returned the original attempt. A manual enqueue for 2026-10-10 also completed.
The LaunchAgent example passed plist parsing and calendar checks. No persistent
LaunchAgent was installed during this walkthrough.
