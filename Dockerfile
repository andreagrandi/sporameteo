FROM node:26.11.1-alpine AS css-builder

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY postcss.config.cjs tailwind.landing.config.cjs ./
COPY assets ./assets
COPY apps/application/web_ui/templates ./apps/application/web_ui/templates
RUN npm run build:css

FROM node:26.11.1-alpine AS map-library

WORKDIR /app/tools/map_checks

COPY tools/map_checks/package.json tools/map_checks/package-lock.json ./
RUN npm ci --ignore-scripts

FROM python:3.14.8-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    gettext \
    libgdal36 \
    libgeos-c1t64 \
    libproj25 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY src ./src
COPY apps ./apps
RUN uv sync --frozen --no-dev --no-cache

COPY --from=css-builder /app/apps/application/web_ui/static/web_ui/ ./apps/application/web_ui/static/web_ui/
COPY --from=map-library /app/tools/map_checks/node_modules/maplibre-gl/dist ./tools/map_checks/node_modules/maplibre-gl/dist

# compilemessages searches the working directory, so run it inside the Django project only.
WORKDIR /app/apps/application
RUN python manage.py compilemessages \
    && python manage.py collectstatic --noinput
WORKDIR /app

EXPOSE 8000

CMD ["sh", "-c", "gunicorn --chdir apps/application config.wsgi:application --workers 2 --bind 0.0.0.0:${PORT:-8000}"]
