# Hosting

SporaMeteo runs as its own Dokku app on the Hetzner VPS that also hosts book
corners. The setup copies the book corners method from its `HOSTING.md` and
`DEPLOYMENT.md`. The VPS has 2 ARM CPUs, 3.7 GB of memory and 25 GB of free
disk. Before this setup it had about 1.9 GB of free memory and no swap.

Every SporaMeteo resource on the VPS has its own name: the app `sporameteo`,
the database service `sporameteo-db` and the storage folder
`/var/lib/dokku/data/storage/sporameteo`. No step changes a book corners app,
service, folder or Cloudflare zone.

Run the server commands as the `deploy` user over
`ssh deploy@vps.bookcorners.org` until `vps.sporameteo.com` resolves. After
each step, check that book corners still answers:

```sh
curl -s -o /dev/null -w "%{http_code}\n" https://www.bookcorners.org/health/
```

The answer must be `200`.

## Swap

The VPS has no swap, so a memory spike in either app ends with the kernel
killing a process. The owner approved a 2 GB swap file. A low `swappiness`
keeps the kernel from moving memory to disk while free memory remains.

```sh
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab
echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-swappiness.conf
sudo sysctl --system
free -h
```

`free -h` must show `Swap: 2.0Gi`. Run it again after the next reboot to check
that the line in `/etc/fstab` works.

## App and database

The database uses the same PostGIS image and tag as `book-corners-db`, so the
VPS keeps only one copy of the image. Read the tag from the existing service
first:

```sh
sudo dokku postgres:info book-corners-db --version
```

Pass that value to `--image-version`. In October 2026 it was
`17-3.6-bookworm`. The `deploy` user may run only `dokku` without a password,
and `sudo` drops the `POSTGRES_IMAGE` variables that the book corners docs use,
so the flags replace them. The swap and folder steps need the sudo password.

```sh
sudo dokku apps:create sporameteo
sudo dokku postgres:create sporameteo-db \
  --image imresamu/postgis --image-version "<tag of book-corners-db>"
sudo dokku postgres:link sporameteo-db sporameteo
sudo dokku postgres:info sporameteo-db
sudo dokku postgres:info book-corners-db
```

The two `postgres:info` outputs must show the same version and two different
container IDs. The link sets `DATABASE_URL`, which `config/settings.py` reads
before the `SPORAMETEO_DB_*` variables.

## Memory limits

The measured peaks are 142 MB for the estimate step and 160 MB for the map
cache warm-up. Both run in the `worker` process. The `web` process runs two
gunicorn workers, and each one loads Django and GDAL.

| Process | Limit | Reason |
|---|---|---|
| `web` | 512 MB | Two gunicorn workers and the gunicorn main process. |
| `worker` | 384 MB | More than twice the 160 MB warm-up peak. |

```sh
sudo dokku resource:limit --memory 512m --process-type web sporameteo
sudo dokku resource:limit --memory 384m --process-type worker sporameteo
sudo dokku resource:report sporameteo
```

The Dokku docs describe limits per process type and do not say whether the
`app.json` cron tasks get them. After the first night, check the real use with
`sudo docker stats --no-stream` and update this table.

## Data files

The app reads 37 JRC host tree files and the Tuscany forest layer file at
runtime. `.dockerignore` leaves `data/` out of the image, so both live in
storage folders on the host and a deploy does not remove them. The container
runs as root, so the folders need no change of owner.

```sh
sudo mkdir -p /var/lib/dokku/data/storage/sporameteo/jrc_host_trees
sudo dokku storage:mount sporameteo \
  /var/lib/dokku/data/storage/sporameteo/jrc_host_trees:/app/data/jrc_host_trees
sudo dokku storage:mount sporameteo \
  /var/lib/dokku/data/storage/sporameteo/mvp_forest:/app/data/processed/mvp_forest
```

Docker creates the `mvp_forest` folder on the host at the next start.

## Restore the Tuscany data

`docs/hosting_data.md` describes the dump and lists the files to copy. The
`deploy` user can run `dokku` without a password but not other `sudo`
commands. The steps below therefore write to the storage folders from inside a
`dokku run` container. `dokku run` does not pass standard input to the
container, so the files go through a staging folder that is mounted for one
command.

Stop the app and empty the database. The first deploy has already created
empty tables, and the restore needs an empty schema:

```sh
sudo dokku ps:stop sporameteo
echo "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" \
  | sudo dokku postgres:connect sporameteo-db
```

Stream the dump from the laptop, so no copy of it is written on the VPS:

```sh
ssh deploy@vps.sporameteo.com "sudo dokku postgres:import sporameteo-db" \
  < data/processed/hosting/sporameteo_tuscany_vps.dump
```

Copy the 37 JRC files and the forest layer file from the laptop to a staging
folder. The `_rpp.zip` archives stay on the laptop:

```sh
ssh deploy@vps.sporameteo.com "mkdir -p ~/sporameteo_staging/jrc \
  ~/sporameteo_staging/forest/tuscany-forest-italy-forest-copernicus-2018-v1-56"
scp data/raw/jrc_host_trees/*.tif data/raw/jrc_host_trees/*.json \
  deploy@vps.sporameteo.com:sporameteo_staging/jrc/
scp data/processed/mvp_forest/tuscany-forest-italy-forest-copernicus-2018-v1-56/forest_layer_2000m.geojson.gz \
  deploy@vps.sporameteo.com:sporameteo_staging/forest/tuscany-forest-italy-forest-copernicus-2018-v1-56/
```

On the VPS, copy the files into the storage folders, remove the staging folder,
apply any migration newer than the dump and start the app:

```sh
sudo dokku storage:mount sporameteo /home/deploy/sporameteo_staging:/staging
sudo dokku run --no-tty sporameteo sh -c \
  "cp -r /staging/jrc/. /app/data/jrc_host_trees/ && cp -r /staging/forest/. /app/data/processed/mvp_forest/"
sudo dokku storage:unmount sporameteo /home/deploy/sporameteo_staging:/staging
rm -rf ~/sporameteo_staging
sudo dokku run --no-tty sporameteo python apps/application/manage.py migrate --noinput
sudo dokku run --no-tty sporameteo python apps/application/manage.py createcachetable
sudo dokku ps:start sporameteo
```

Check that every source is ready. The source files behind the forest and
terrain builds stay on the laptop, so the check skips them:

```sh
sudo dokku run --no-tty sporameteo python apps/application/manage.py \
  prepare_region_inputs --regions 09 --dry-run --json --skip-source-files
```

All four sources must report `ready`, and the `host_trees` version must be
`d8998582df944b32f134ef85beb51380e255e4702d6714ebb01b5bd3029ddd0a`.

The October 2026 dump was built before the rename and holds an empty
`micometeo_map_cache` table. The app uses `sporameteo_map_cache`, which
`createcachetable` creates. Drop the old table:

```sh
echo "DROP TABLE micometeo_map_cache;" | sudo dokku postgres:connect sporameteo-db
```

## Load the inputs of other regions

Issue #153 adds the grid, forest, terrain and host tree values of all 20
regions next to the Tuscany data. The app keeps reading the Tuscany builds
until `SPORAMETEO_FOREST_BUILD_VERSION` and `SPORAMETEO_TERRAIN_BUILD_VERSION`
change. The load only adds rows, so the app keeps running.

The grid, forest and terrain values come from the local national database
`sporameteo_native`. The copy runs on the laptop and writes to production
through an SSH tunnel. The host tree values are sampled on the VPS, where the
JRC files are.

Run every step outside the night tasks, which run from 00:30 to 02:30 UTC. The
published run must not change between the check before and the check after.

Deploy the commit with migration `estimate_updates.0008` first, and check out
the same commit on the laptop.

### Check before the load

`tools/region_load/tuscany_check.sql` prints fingerprints of the Tuscany
builds, squares, forest, terrain, input set and host tree values, of the
published runs and of the results of the published Tuscany run, and then the
database size in bytes:

```sh
ssh deploy@vps.sporameteo.com "sudo dokku postgres:connect sporameteo-db" \
  < tools/region_load/tuscany_check.sql > tuscany_before.txt
```

### Copy the grid, forest and terrain values

Read the address and password of the database, then open a tunnel to it. The
tunnel stays in the foreground, so use a second terminal for the next steps:

```sh
ssh deploy@vps.sporameteo.com "sudo dokku postgres:enter sporameteo-db hostname -i"
ssh deploy@vps.sporameteo.com "sudo dokku postgres:info sporameteo-db --dsn"
ssh -N -L 15432:<internal ip>:5432 deploy@vps.sporameteo.com
```

In the second terminal, point `DATABASE_URL` at the tunnel. It is the DSN
with `127.0.0.1:15432` in place of the host and port. The source options name
the local national database. Pass all five of them, because any source option
left out takes its value from `DATABASE_URL`, which is production:

```sh
export DATABASE_URL="postgres://postgres:<password>@127.0.0.1:15432/sporameteo_db"
uv run python apps/application/manage.py prepare_region_map_database \
  --regions all --dry-run \
  --source-db-name sporameteo_native --source-db-user sporameteo \
  --source-db-password "<local password>" \
  --source-db-host 127.0.0.1 --source-db-port 5542 \
  --source-forest-build-version italy-forest-copernicus-2018-v1 \
  --source-terrain-build-version italy-terrain-glo30-2021-v2
```

The dry run lists the 20 regions and their squares and writes nothing. Run
the same command without `--dry-run` for the copy. It creates
`regions-all-grid-italy-500m-istat-2026-01-01-v1`,
`regions-all-forest-italy-forest-copernicus-2018-v1` and
`regions-all-terrain-italy-terrain-glo30-2021-v2`, and writes the forest layer
file to `data/processed/mvp_forest/regions-all-forest-italy-forest-copernicus-2018-v1/`.
The copy is one transaction. If the tunnel drops, nothing is stored and the
layer file is removed, so the command can run again.

On the laptop, against a local copy of the database, the copy took 7 minutes
51 seconds and used 1.27 GB of memory on the laptop. It copies the 1,217,473
squares of Italy and reuses the outlines of the 92,707 Tuscany squares that
are already stored. The time through the tunnel also
depends on the upload speed of the laptop and was not measured. Close the
tunnel when the copy ends.

Copy the layer file to the VPS through a staging folder, as for Tuscany:

```sh
ssh deploy@vps.sporameteo.com "mkdir -p \
  ~/sporameteo_staging/forest/regions-all-forest-italy-forest-copernicus-2018-v1"
scp data/processed/mvp_forest/regions-all-forest-italy-forest-copernicus-2018-v1/forest_layer_2000m.geojson.gz \
  deploy@vps.sporameteo.com:sporameteo_staging/forest/regions-all-forest-italy-forest-copernicus-2018-v1/
```

On the VPS:

```sh
sudo dokku storage:mount sporameteo /home/deploy/sporameteo_staging:/staging
sudo dokku run --no-tty sporameteo sh -c \
  "cp -r /staging/forest/. /app/data/processed/mvp_forest/"
sudo dokku storage:unmount sporameteo /home/deploy/sporameteo_staging:/staging
rm -rf ~/sporameteo_staging
```

### Sample the host tree values

On the VPS, the dry run reports each region as ready or names the missing
source and the squares it affects. The real run samples the JRC files for the
forest squares that have no stored values and records one input set per
region:

```sh
sudo dokku run --no-tty sporameteo python apps/application/manage.py \
  prepare_region_inputs --regions all --dry-run --skip-source-files \
  --forest-build-version regions-all-forest-italy-forest-copernicus-2018-v1 \
  --terrain-build-version regions-all-terrain-italy-terrain-glo30-2021-v2
sudo dokku run --no-tty sporameteo python apps/application/manage.py \
  prepare_region_inputs --regions all --skip-source-files \
  --forest-build-version regions-all-forest-italy-forest-copernicus-2018-v1 \
  --terrain-build-version regions-all-terrain-italy-terrain-glo30-2021-v2
```

All 20 regions must report `ready`. The Tuscany values are already stored, so
the run samples 646,073 of the 717,104 forest squares.

On the laptop, inside the app image limited to 2 CPUs and 384 MB without
swap, the run took 850 seconds. The memory that cannot be freed peaked at
260 MiB, and the process peaked at 337 MiB, file pages included. Nothing was
killed. The VPS is 1.38 times slower, see `reports/italy_night_cost.md`, so the
run takes about 850 times 1.38, about 20 minutes.

### Check after the load

Three checks follow. The first covers all of Italy. The dry run checks that
every square of the 20 regions has a grid membership, forest and terrain
values, and host tree values for every forest square:

```sh
sudo dokku run --no-tty sporameteo python apps/application/manage.py \
  prepare_region_inputs --regions all --dry-run --skip-source-files \
  --forest-build-version regions-all-forest-italy-forest-copernicus-2018-v1 \
  --terrain-build-version regions-all-terrain-italy-terrain-glo30-2021-v2
```

All 20 regions must report `ready`, with 1,217,473 squares and 717,104 forest
squares in the totals.

The dry run only checks that the values exist. The second check compares
them with the national database. `tools/region_load/region_values_check.sql`
prints one line per region with the region code, name and a fingerprint of
its outline, the number of squares, and fingerprints of the square, forest and
terrain values. Row ids and timestamps are left out, because the copy gives
them new values. On the laptop, run it on the national builds:

```sh
(printf '%s\n' "\\set grid 'italy-500m-istat-2026-01-01-v1'" \
  "\\set forest 'italy-forest-copernicus-2018-v1'" \
  "\\set terrain 'italy-terrain-glo30-2021-v2'"; \
  cat tools/region_load/region_values_check.sql) \
  | docker compose exec -T database psql -U sporameteo -d sporameteo_native \
  > region_values_national.txt
```

Then run it on the copied builds in production and compare the two files:

```sh
(printf '%s\n' "\\set grid 'regions-all-grid-italy-500m-istat-2026-01-01-v1'" \
  "\\set forest 'regions-all-forest-italy-forest-copernicus-2018-v1'" \
  "\\set terrain 'regions-all-terrain-italy-terrain-glo30-2021-v2'"; \
  cat tools/region_load/region_values_check.sql) \
  | ssh deploy@vps.sporameteo.com "sudo dokku postgres:connect sporameteo-db" \
  > region_values_production.txt
diff region_values_national.txt region_values_production.txt
```

The diff must be empty, and each file must have 20 lines. A line that
differs names the region whose values changed in the copy.

On the laptop, the check took 13 seconds on `sporameteo_native` and 15
seconds on a local copy of the loaded database. The two printed the same 20
lines. A changed forest share of one Molise square in a scratch copy changed
only the forest fingerprint of the Molise line.

The third check covers Tuscany, which the site serves today. Its data and its
published run must not change:

```sh
ssh deploy@vps.sporameteo.com "sudo dokku postgres:connect sporameteo-db" \
  < tools/region_load/tuscany_check.sql > tuscany_after.txt
diff tuscany_before.txt tuscany_after.txt
```

Only the `size` line may differ. Record both sizes in the setup log.

On the local copy, the database grew from 189,820,051 to 2,415,438,995 bytes.
The largest new tables were the square memberships at 714 MB, the forest
values at 461 MB, the squares at 412 MB and the terrain values at 383 MB.

## Environment

Generate the secret key on the laptop:

```sh
uv run python -c "import secrets; print(secrets.token_urlsafe(50))"
```

```sh
sudo dokku config:set --no-restart sporameteo \
  SPORAMETEO_DJANGO_SECRET_KEY="<generated key>" \
  SPORAMETEO_ALLOWED_HOSTS="www.sporameteo.com,sporameteo.com" \
  SPORAMETEO_CSRF_TRUSTED_ORIGINS="https://www.sporameteo.com,https://sporameteo.com" \
  SPORAMETEO_SECURE_SSL_REDIRECT="true" \
  SPORAMETEO_SESSION_COOKIE_SECURE="true" \
  SPORAMETEO_CSRF_COOKIE_SECURE="true" \
  SPORAMETEO_SECURE_HSTS_SECONDS="31536000" \
  SPORAMETEO_FOREST_BUILD_VERSION="tuscany-forest-italy-forest-copernicus-2018-v1-56" \
  SPORAMETEO_TERRAIN_BUILD_VERSION="tuscany-terrain-italy-terrain-glo30-2021-v2-56" \
  SPORAMETEO_NIGHTLY_REGIONS="09" \
  SPORAMETEO_JRC_DIRECTORY="/app/data/jrc_host_trees" \
  RESEND_API_KEY="<Resend key>" \
  NIGHTLY_ALERT_EMAIL="<owner address>"
```

The two apps share one Resend account. The app sends from
`noreply@sporameteo.com`, so `sporameteo.com` must be a verified domain in
Resend. The build versions match the Tuscany dump in `docs/hosting_data.md`.
`SPORAMETEO_NIGHTLY_REGIONS` names the regions of the nightly chain, `all` or
codes such as `09,08`. It defaults to `09`, as `docs/weather_updates.md` explains.
`--no-restart` avoids errors before the first deploy.

## Domain and HTTPS

Cloudflare holds the `sporameteo.com` zone. It needs three A records, all
pointing at the VPS address:

| Record | Proxy |
|---|---|
| `sporameteo.com` | Proxied |
| `www.sporameteo.com` | Proxied |
| `vps.sporameteo.com` | DNS only |

`vps.sporameteo.com` skips Cloudflare, so SSH and `git push` reach the VPS.
Add a Cloudflare redirect rule that sends `sporameteo.com/*` to
`https://www.sporameteo.com/${1}` with status 301.

```sh
sudo dokku domains:set sporameteo www.sporameteo.com sporameteo.com
```

After the first deploy, map port 80 and start the worker. The `Dockerfile`
exposes port 8000, so without `ports:set` nginx listens on 8000 instead of 80.
Dokku starts only the `web` process unless told otherwise.

```sh
sudo dokku ports:set sporameteo http:80:8000
sudo dokku ps:scale sporameteo worker=1
```

Then enable HTTPS with the same notification email as book corners:

```sh
sudo dokku letsencrypt:set sporameteo email \
  "$(sudo dokku letsencrypt:report book-corners --letsencrypt-email)"
sudo dokku letsencrypt:enable sporameteo
```

The Let's Encrypt plugin and its renewal cron job already exist for book
corners. `letsencrypt:enable` adds the `https:443:8000` mapping. Then set the
Cloudflare SSL mode for the zone to Full Strict, and turn on Always Use HTTPS.

## Deploys from CI

The `deploy` job in `.github/workflows/ci.yml` runs after all checks pass on a
push to `master`. It asks Dokku which commit it runs and pushes only when one
of these paths changed since that commit: `apps/`, `src/`, `assets/`,
`pyproject.toml`, `uv.lock`, `package.json`, `package-lock.json`, the CSS build
configs, the map library package files under `tools/map_checks/`, `Dockerfile`,
`Procfile`, `app.json` and the workflow itself. A push that touches only
`reports/` or `docs/` does not deploy.

The job needs one repository secret, `DOKKU_SSH_PRIVATE_KEY`. Create a key pair
for it and give the public key to Dokku:

```sh
ssh-keygen -t ed25519 -N "" -f ~/.ssh/sporameteo_deploy
cat ~/.ssh/sporameteo_deploy.pub | ssh deploy@vps.sporameteo.com \
  "sudo dokku ssh-keys:add sporameteo-ci"
gh secret set DOKKU_SSH_PRIVATE_KEY < ~/.ssh/sporameteo_deploy
```

For a manual deploy from the laptop:

```sh
git remote add dokku dokku@vps.sporameteo.com:sporameteo
git push dokku master
```

## Image size

On 23 October 2026 the image built on the laptop from commit `4399ea2` with
the `Dockerfile` of that commit took 2.39 GB, or 2,390,274,670 bytes. The
laptop builds for ARM, like the VPS. The VPS reported 3.2 GB for
`dokku/sporameteo`. The `deploy` user cannot read the Docker images on the VPS
without the `sudo` password, so the reason for the gap is still unknown.

`docker history` gives the size of each step of the final stage:

| Step | Size |
|---|---|
| Debian trixie and Python 3.14.8 from `python:3.14.8-slim-trixie` | 145 MB |
| `apt-get install` of `gettext`, `libgdal-dev`, `libgeos-dev` and `libproj-dev` | 736 MB |
| Copy of the `uv` binary | 37.5 MB |
| Copy of `pyproject.toml`, `uv.lock` and `README.md` | 0.3 MB |
| `uv sync --frozen --no-dev --no-install-project` | 1.42 GB |
| Copy of `src` and `apps` | 1.9 MB |
| `uv sync --frozen --no-dev` | 1.1 MB |
| Copy of the built CSS | 0.1 MB |
| Copy of the MapLibre files | 14.1 MB |
| `compilemessages` and `collectstatic` | 34.8 MB |

The `apt-get` step adds 259 packages to the base image. Their declared
installed size is 786 MB. The 87 packages whose name ends in `-dev` account for
453 MB of it. The largest are `libboost1.83-dev` with 161 MB, `libicu-dev` with
51 MB, `liblapack-dev` with 23 MB and `libstdc++-14-dev` with 23 MB. The header
files under `/usr/include` take 207 MB. Nothing in the image compiles C code,
because `uv sync` installs only prebuilt wheels.

The `uv sync` step is 1.42 GB because it keeps two copies of every package.
`/app/.venv` takes 710 MB. The download cache in `/root/.cache/uv` takes
711 MB, and the app never reads it.

The 10 largest Python packages in `/app/.venv`, counted from the files each
package lists as installed:

| Package | Version | Size |
|---|---|---|
| pyarrow | 26.0.0 | 154 MB |
| rasterio | 1.5.2 | 124 MB |
| scipy | 1.18.1 | 122 MB |
| numpy | 2.5.3 | 54 MB |
| scikit-learn | 1.9.1 | 40 MB |
| pyproj | 3.8.0 | 37 MB |
| matplotlib | 3.11.2 | 27 MB |
| psycopg-binary | 3.3.6 | 25 MB |
| fonttools | 4.66.1 | 24 MB |
| Django | 6.1.2 | 24 MB |

`rasterio` carries its own copy of GDAL in `rasterio.libs`, 66 MB of its
124 MB. `pyproj` also carries its own PROJ.

A test image swapped the three `-dev` packages for the runtime libraries
`libgdal36`, `libgeos-c1t64` and `libproj25`, which are the packages the
`-dev` ones depend on. Its `apt-get` step took 211 MB instead of 736 MB, and
the whole image took 1.87 GB instead of 2.39 GB. The expected saving is
525 MB. `/usr/include` was empty. Inside that image, Django loaded GDAL 3.10.3
and GEOS 3.13.1 and moved a point from WGS 84 to the European grid,
EPSG:3035, with PROJ. `compilemessages` and `collectstatic` ran during the
build.

Issue #192 made both changes in the `Dockerfile`. It installs the three
runtime libraries, and both `uv sync` lines run with `--no-cache`. Built on the
laptop on 23 October 2026 from commit `b995b4a` plus this change, the image
took 1.16 GB, or 1,156,446,307 bytes, instead of 2.39 GB. The `apt-get`
step took 211 MB, and the first `uv sync` step took 710 MB. The image has no
`/root/.cache` folder and no package whose name ends in `-dev`.

## Checks

```sh
curl -s -o /dev/null -w "%{http_code}\n" https://www.sporameteo.com/health/
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" https://sporameteo.com/
curl -s -o /dev/null -w "%{http_code}\n" https://www.bookcorners.org/health/
```

The first and last must print `200`. The second must print `301` and
`https://www.sporameteo.com/`.

## Monitoring

Sentry collects errors from pages and from the worker. The app starts Sentry
only when `SENTRY_DSN` is set. A failed background job reaches Sentry because
Django logs it at error level. Errors with a `DisallowedHost` exception are
dropped, because they come from bots that call the server by its IP address.
Performance tracing stays off.

The project `sporameteo` sits in the book corners Sentry organization. That
organization uses the sponsored plan for open source projects. On 15 October
2026 the plan page asked for a project under a permissive licence such as MIT
or Apache. SporaMeteo uses the MIT licence and the repository is public. The
organization's subscription page shows the Sponsored Business plan with 5
million errors per month and a pay-as-you-go limit of $0, so errors over the
quota are dropped and not billed.

```sh
sudo dokku config:set sporameteo SENTRY_DSN="<DSN from the Sentry project>"
```

To send a test error:

```sh
sudo dokku run sporameteo python apps/application/manage.py shell -c \
  "import sentry_sdk; sentry_sdk.capture_exception(RuntimeError('SporaMeteo Sentry test')); sentry_sdk.flush()"
```

UptimeRobot calls `https://www.sporameteo.com/health/` every 5 minutes and
sends an email when the page does not answer `200`. It sends HEAD requests,
so the health page answers HEAD as well as GET. On 15 October 2026 the
UptimeRobot terms, updated 24 June 2026, said: "UptimeRobot is available for
any use, including commercial and business use." The pricing page listed 50
monitors and a 5 minute interval on the free plan. Book corners used 1 of the
50 monitors, so a second site fits.

To check the alert, stop the web process for one check and start it again:

```sh
sudo dokku ps:scale sporameteo web=0
sudo dokku ps:scale sporameteo web=1
```

## Move to another host

The book corners VPS is a temporary home. These steps move SporaMeteo to a new
VPS. `OLD` and `NEW` stand for the addresses of the two hosts. Parts of steps
3 to 5 were rehearsed on 16 October 2026, as recorded at the end of this
section.

1. Set up the new VPS with the sections above, from "Swap" to "Domain and
   HTTPS", but leave the DNS records and Let's Encrypt for step 6. Copy the
   values of `sudo dokku config:show sporameteo` from the old host. Add the CI
   deploy key with `dokku ssh-keys:add sporameteo-ci`, using
   `~/.ssh/sporameteo_deploy.pub` from the laptop. Deploy once from the laptop
   with `git push dokku@NEW:sporameteo master`.
2. Choose a time outside the night, so not between 00:30 and 04:00 UTC. Stop the
   old app, so that no run writes to the old database after the export:

   ```sh
   ssh deploy@OLD "sudo dokku ps:stop sporameteo"
   ```

3. Empty the new database as in "Restore the Tuscany data", then stream the
   database from the old host to the new one through the laptop:

   ```sh
   ssh deploy@OLD "sudo dokku postgres:export sporameteo-db" \
     | ssh deploy@NEW "sudo dokku postgres:import sporameteo-db"
   ```

4. Copy the data files. They are owned by root and readable only by root,
   so `rsync` as `deploy` fails with "Permission denied". Stream them out of a
   container as one archive instead:

   ```sh
   ssh deploy@OLD "sudo dokku run --no-tty sporameteo \
     tar -C /app/data -cf - jrc_host_trees processed/mvp_forest" \
     > /tmp/sporameteo_files.tar
   ssh deploy@NEW "mkdir -p ~/sporameteo_staging"
   scp /tmp/sporameteo_files.tar deploy@NEW:sporameteo_staging/
   ```

   On the new host, unpack the archive into the storage folders through a
   staging mount, as in "Restore the Tuscany data", then start the app:

   ```sh
   sudo dokku storage:mount sporameteo /home/deploy/sporameteo_staging:/staging
   sudo dokku run --no-tty sporameteo tar -C /app/data -xf /staging/sporameteo_files.tar
   sudo dokku storage:unmount sporameteo /home/deploy/sporameteo_staging:/staging
   rm -rf ~/sporameteo_staging
   sudo dokku run --no-tty sporameteo python apps/application/manage.py migrate --noinput
   sudo dokku ps:start sporameteo
   ```

5. Check the copy. The row count of every table must match the old database.
   Run this on both hosts and compare the output:

   ```sh
   echo "SELECT table_name, (xpath('/row/c/text()', query_to_xml(format(
     'SELECT count(*) AS c FROM %I.%I', table_schema, table_name),
     false, true, '')))[1]::text FROM information_schema.tables
     WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
     ORDER BY table_name;" | sudo dokku postgres:connect sporameteo-db
   ```

   The checksums of the 38 data files must match the old host. Compare the
   output of this command on both hosts:

   ```sh
   sudo dokku run --no-tty sporameteo sh -c \
     "cd /app/data && find jrc_host_trees processed/mvp_forest -type f -print0 \
     | sort -z | xargs -0 sha256sum" | LC_ALL=C sort -k2
   ```

   The container and the host sort file names differently, so sort both lists
   with `LC_ALL=C sort -k2` before comparing. Then run `prepare_region_inputs --regions 09`
   as at the end of "Restore the Tuscany data".
6. In Cloudflare, point `sporameteo.com`, `www.sporameteo.com` and
   `vps.sporameteo.com` to the new address. Then enable Let's Encrypt on the
   new host as in "Domain and HTTPS". The CI deploy target stays
   `vps.sporameteo.com`, and the deploy job reads the host key again on every
   run, so no secret changes.
7. Run the checks in "Checks", and `check_nightly_update` the next morning.
   Remove the old app and database only after one night has published on the
   new host.

### Rehearsal on 16 October 2026

The rehearsal ran the export and import of step 3, the archive of step 4 and
the checks of step 5 on the current VPS, into a scratch database
service `sporameteo-db-rehearsal` and a scratch folder in the home of
`deploy`. The app kept running. The export went to a file on the VPS instead
of through the laptop, and the import read that file. The unpacking through a
staging mount was not rehearsed. It is the same mount that restored the data
on 15 October 2026.

| Step | Time | Size |
|---|---|---|
| Export of `sporameteo-db`, 389 MB on disk | 18 seconds | 96.9 MB dump |
| Creation of the scratch service, same `imresamu/postgis:17-3.6-bookworm` image | 10 seconds | |
| Import into the scratch service | 32 seconds | |
| Data files streamed out as one archive and unpacked | 20 seconds | 359 MB, 38 files |

All 56 tables had the same row count in both databases, among them 15,651
daily weather rows, 666 weather snapshots and 284,124 estimate results. The
37 JRC files and the forest layer file had the same SHA-256 checksums as the
originals. The first copy with `rsync` failed with "Permission denied", which
is why step 4 uses an archive.

Afterwards `postgres:destroy` removed the scratch service and the scratch
folder was deleted. Disk use went from 23,000,178,688 to 23,002,730,496 bytes.
The 2.5 MB left over is mostly the `busybox` image that `postgres:destroy`
downloads for its cleanup.

## Setup log

| Step | Date | Result |
|---|---|---|
| Swap | 15 October 2026 | 2 GB swap file in `/etc/fstab`, `swappiness` 10. |
| App and database | 15 October 2026 | `sporameteo-db` and `book-corners-db` both run `imresamu/postgis:17-3.6-bookworm`, in different containers. |
| Memory limits | 15 October 2026 | `web` 512 MB, `worker` 384 MB. |
| JRC storage | 15 October 2026 | Folder mounted at `/app/data/jrc_host_trees`, still empty. |
| Environment | 15 October 2026 | All variables set. `RESEND_API_KEY` and `NIGHTLY_ALERT_EMAIL` copied from the book corners `RESEND_API_KEY` and `ADMIN_NOTIFICATION_EMAIL`. `sporameteo.com` verified in Resend. Domains set. |
| DNS and redirect | 15 October 2026 | Wildcard redirect rule `https://sporameteo.com/*` to `https://www.sporameteo.com/${1}`, 301. The path and query string carry over. |
| First deploy | 15 October 2026 | `f953ade` pushed from the laptop. Migrations and both health checks passed. Port 80 mapped, one worker started. |
| Let's Encrypt | 15 October 2026 | Certificate valid until 13 January 2027. The VPS answers `/health/` with 200 and a valid certificate for both names. |
| Cloudflare Full Strict | 15 October 2026 | Set by the owner. `/health/` answers 200 through Cloudflare, and plain `http` redirects to `https`. |
| CI deploy key | 15 October 2026 | Dokku key `sporameteo-ci`, private key in the `DOKKU_SSH_PRIVATE_KEY` secret. `git ls-remote` with it returns the deployed commit. |
| Tuscany data | 15 October 2026 | Dump restored with the deployed commit `d25c2de`. Row counts match `docs/hosting_data.md`, except that `django_migrations` has 64 rows because `migrate` added `weather_on_demand.0006`. The dry run and a real run of the Tuscany input preparation with `--skip-source-files`, now `prepare_region_inputs --regions 09`, both report `ready` for all four sources. The real run took 2.3 seconds and left 71,031 host tree rows and 1 input set. `host_tree_source_version()` matches the laptop. `/api/v1/forest-layer` answers 200. The database takes 184 MB. The JRC files take 358,725,015 bytes and the forest layer file 208,655 bytes. Used disk went from 52% to 54% of 38 GB. No dump, staging folder or `_rpp.zip` file is left on the VPS. `DROP TABLE` removed the empty `micometeo_map_cache` table that the dump brought from before the rename, so the database has 55 tables. |
| First weather load | 15 October 2026 | The dry run planned 2026-10-15 in `initial` mode: 333 cells, 1,332 counted calls and 16 HTTP requests. The real run `f6557df6-8743-43d1-aad9-e8b01812c6b6` started at 19:21:22 UTC and ended `completed` at 19:44:23 UTC. `weather_on_demand_status` reports 1,332 counted calls, 16 attempts and no refused request. The chain queued the forecast without a manual command. It ran from 19:46:51 to 19:52:32 UTC and published run `fdde1518-478f-49e6-8ccf-9e6f098b098b`. The map cache warm-up ran from 19:52:32 to 19:52:42 UTC and left 602 cache rows. The 333 snapshots also queued 333 per-cell estimate tasks, which delayed the forecast start by about 2 minutes. `/api/v1/squares` returns available estimates for all eight dates near Abetone, in the Casentino, on Monte Amiata and in Lunigiana. The only unavailable squares have no forest. Area details name the municipality, for example Coreglia Antelminelli, Pratovecchio Stia, Arcidosso and Tresana. The database went from 184 MB, 192,416,915 bytes, to 301 MB, 315,804,819 bytes. |
| Move rehearsal | 16 October 2026 | Export, import and file copy rehearsed into `sporameteo-db-rehearsal` and a scratch folder, then removed. Row counts of all 56 tables and checksums of all 38 data files matched. See "Move to another host". |
| Region inputs | 18 October 2026 | Production ran `de09904` with `estimate_updates.0008` applied. `dokku postgres:info --internal-ip` printed nothing, so the tunnel used the address that `dokku postgres:enter sporameteo-db hostname -i` printed, 172.17.0.4. The dry run of `prepare_region_map_database` listed 20 regions, 1,217,473 squares, 717,104 forest squares and 92,707 reused Tuscany squares. The copy through the tunnel ran from about 07:09 to 07:25 UTC and committed. The forest layer file is 3,377,975 bytes, and its checksum on the VPS matches the laptop. `prepare_region_inputs` sampled 646,073 forest squares in 1,685 seconds, from 07:31 UTC, and the whole `dokku run` took 40 minutes 30 seconds. The dry run after the load reports 20 regions `ready`, 1,217,473 squares and 717,104 forest squares. The Tuscany check printed the same lines before and after, except size, which went from 493,137,043 to 2,718,485,651 bytes. |
| Region values check | 18 October 2026 | `tools/region_load/region_values_check.sql` printed the same 20 lines on production and on `sporameteo_native`. It took 2 minutes 55 seconds on the VPS and 13 seconds on the laptop. |
