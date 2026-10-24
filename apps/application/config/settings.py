"""Django settings for the local SporaMeteo application."""

import importlib.util
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import dj_database_url
from django.core.exceptions import DisallowedHost

BASE_DIR = Path(__file__).resolve().parent.parent


def _rasterio_gdal_library_path() -> str | None:
    """Find Rasterio's GDAL library, which does not load a second Arrow build."""
    module_spec = importlib.util.find_spec("rasterio")
    if module_spec is None or module_spec.origin is None:
        return None
    library_dir = Path(module_spec.origin).parent / ".dylibs"
    candidates = sorted(library_dir.glob("libgdal*.dylib"))
    return str(candidates[0]) if candidates else None


def _env_bool(*, name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(*, name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def _env_list(*, name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if value is None:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


SECRET_KEY = os.getenv(
    "SPORAMETEO_DJANGO_SECRET_KEY",
    "django-insecure-local-development-placeholder",
)
DEBUG = _env_bool(name="SPORAMETEO_DEBUG", default=False)
ALLOWED_HOSTS = _env_list(name="SPORAMETEO_ALLOWED_HOSTS", default=["127.0.0.1", "localhost"])
CSRF_TRUSTED_ORIGINS = _env_list(
    name="SPORAMETEO_CSRF_TRUSTED_ORIGINS",
    default=["http://127.0.0.1:8010", "http://localhost:8010"],
)

# Behind the Dokku proxy, TLS ends at the proxy and it sends X-Forwarded-Proto.
# The flags stay off by default so local development and tests use plain HTTP.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = _env_bool(name="SPORAMETEO_SECURE_SSL_REDIRECT", default=False)
# Dokku checks /health/ over plain HTTP inside the container, so it must not redirect.
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
SESSION_COOKIE_SECURE = _env_bool(name="SPORAMETEO_SESSION_COOKIE_SECURE", default=False)
CSRF_COOKIE_SECURE = _env_bool(name="SPORAMETEO_CSRF_COOKIE_SECURE", default=False)
SECURE_HSTS_SECONDS = _env_int(name="SPORAMETEO_SECURE_HSTS_SECONDS", default=0)

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.gis",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_tasks_db",
    "background_jobs",
    "national_grid",
    "national_terrain",
    "national_forest",
    "weather_requests",
    "weather_summaries",
    "weather_on_demand",
    "estimate_updates",
    "area_api",
    "map_api",
    "place_search",
    "web_ui",
    "config",
]

SPORAMETEO_TERRAIN_BUILD_VERSION = os.getenv(
    "SPORAMETEO_TERRAIN_BUILD_VERSION", "italy-terrain-glo30-2021-v2"
)
SPORAMETEO_FOREST_BUILD_VERSION = os.getenv(
    "SPORAMETEO_FOREST_BUILD_VERSION", "italy-forest-copernicus-2018-v1"
)
# The regions of the nightly chain: all, or ISTAT region codes such as 09,08.
SPORAMETEO_NIGHTLY_REGIONS = os.getenv("SPORAMETEO_NIGHTLY_REGIONS", "09")
SPORAMETEO_PLACE_SEARCH_URL = os.getenv(
    "SPORAMETEO_PLACE_SEARCH_URL", "https://photon.komoot.io/api"
)
SPORAMETEO_PLACE_SEARCH_TIMEOUT = float(os.getenv("SPORAMETEO_PLACE_SEARCH_TIMEOUT", "4"))
SPORAMETEO_PLACE_SEARCH_CACHE_SECONDS = int(
    os.getenv("SPORAMETEO_PLACE_SEARCH_CACHE_SECONDS", "2592000")
)
SPORAMETEO_PLACE_SEARCH_EMPTY_CACHE_SECONDS = int(
    os.getenv("SPORAMETEO_PLACE_SEARCH_EMPTY_CACHE_SECONDS", "900")
)
SPORAMETEO_PLACE_SEARCH_REQUEST_INTERVAL = float(
    os.getenv("SPORAMETEO_PLACE_SEARCH_REQUEST_INTERVAL", "1")
)
SPORAMETEO_PLACE_SEARCH_LIMIT = int(os.getenv("SPORAMETEO_PLACE_SEARCH_LIMIT", "8"))
SPORAMETEO_PLACE_SEARCH_MIN_LENGTH = int(os.getenv("SPORAMETEO_PLACE_SEARCH_MIN_LENGTH", "2"))
SPORAMETEO_PLACE_SEARCH_USER_AGENT = os.getenv(
    "SPORAMETEO_PLACE_SEARCH_USER_AGENT", "SporaMeteo/1.0 place search"
)
SPORAMETEO_JRC_DIRECTORY = os.getenv(
    "SPORAMETEO_JRC_DIRECTORY", str(BASE_DIR.parent.parent / "data/raw/jrc_host_trees")
)

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "sporameteo-api",
    },
    # Completed map data shared by all processes. Entries do not expire, the table is bounded.
    "map": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "sporameteo_map_cache",
        "TIMEOUT": None,
        "OPTIONS": {
            "MAX_ENTRIES": int(os.getenv("SPORAMETEO_MAP_CACHE_MAX_ENTRIES", "50000")),
            "CULL_FREQUENCY": 4,
        },
    },
    # Request counts for the API limit, shared by all processes.
    "rate_limit": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "sporameteo_rate_limit_cache",
        "OPTIONS": {"MAX_ENTRIES": 10000, "CULL_FREQUENCY": 3},
    },
}

API_RATE_LIMIT_ENABLED = _env_bool(name="SPORAMETEO_API_RATE_LIMIT_ENABLED", default=not DEBUG)
API_RATE_LIMIT_WINDOW_SECONDS = _env_int(name="SPORAMETEO_API_RATE_LIMIT_WINDOW_SECONDS", default=60)
API_RATE_LIMIT_READ_REQUESTS = _env_int(name="SPORAMETEO_API_RATE_LIMIT_READ_REQUESTS", default=150)

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "web_ui.middleware.WebUILocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

def database_config(*, environ: Mapping[str, str]) -> dict[str, Any]:
    """Build the default database settings from DATABASE_URL or SPORAMETEO_DB_* values.

    DATABASE_URL wins when it is set, which is how Dokku passes the database.
    """
    database_url = environ.get("DATABASE_URL")
    if database_url:
        config = dj_database_url.parse(
            database_url,
            engine="django.contrib.gis.db.backends.postgis",
            conn_max_age=0,
        )
        config["OPTIONS"] = {**config.get("OPTIONS", {}), "connect_timeout": 5}
        return config
    return {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": environ.get("SPORAMETEO_DB_NAME", "sporameteo"),
        "USER": environ.get("SPORAMETEO_DB_USER", "sporameteo"),
        "PASSWORD": environ.get("SPORAMETEO_DB_PASSWORD", "replace-with-a-local-password"),
        "HOST": environ.get("SPORAMETEO_DB_HOST", "127.0.0.1"),
        "PORT": environ.get("SPORAMETEO_DB_PORT", "5542"),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {"connect_timeout": 5},
    }


DATABASES = {"default": database_config(environ=os.environ)}

TASKS = {
    "default": {
        "BACKEND": "django_tasks_db.backend.DatabaseBackend",
    },
}

LANGUAGE_CODE = "en"
LANGUAGE_COOKIE_NAME = "sporameteo_language"
LANGUAGES = [("en", "English"), ("it", "Italiano")]
TIME_ZONE = "Europe/Rome"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR.parent.parent / "staticfiles"
# Outside debug, collectstatic adds a content fingerprint to each file name, so a deploy
# changes the URL of every changed file and browsers and Cloudflare cannot serve an old copy.
# Debug keeps the plain names because runserver serves files that collectstatic never saw.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedStaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}
STATICFILES_DIRS = [
    BASE_DIR.parent.parent / "tools/map_checks/node_modules/maplibre-gl/dist",
]
LOCALE_PATHS = [BASE_DIR / "web_ui" / "locale", BASE_DIR / "web_ui" / "landing_locale"]
SPORAMETEO_UI_FIXTURE_PAGES = _env_bool(name="SPORAMETEO_UI_FIXTURE_PAGES", default=False)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

GDAL_LIBRARY_PATH = (
    os.getenv("SPORAMETEO_GDAL_LIBRARY_PATH")
    or _rasterio_gdal_library_path()
    or os.getenv("GDAL_LIBRARY_PATH")
)
GEOS_LIBRARY_PATH = os.getenv("GEOS_LIBRARY_PATH")

# Email for the nightly check (Resend). Without a key, emails go to the console.
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "SporaMeteo <noreply@sporameteo.com>")
NIGHTLY_ALERT_EMAIL = os.getenv("NIGHTLY_ALERT_EMAIL", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
if RESEND_API_KEY:
    INSTALLED_APPS += ["anymail"]
    ANYMAIL = {"RESEND_API_KEY": RESEND_API_KEY}
    EMAIL_BACKEND = "anymail.backends.resend.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Error reports to Sentry. Without a DSN, nothing is sent.
SENTRY_DSN = os.getenv("SENTRY_DSN", "")


def sentry_before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Drop DisallowedHost errors, which come from bots that call the server by its IP address.
    They would use up the error quota without pointing at a bug."""
    exc_info = hint.get("exc_info")
    if exc_info and exc_info[0] is DisallowedHost:
        return None
    return event


if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        release=os.getenv("GIT_REV", ""),
        environment="development" if DEBUG else "production",
        send_default_pii=False,
        before_send=sentry_before_send,
    )
