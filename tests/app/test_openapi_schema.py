"""Checks that the OpenAPI schema stays valid and lists what the web page calls."""

import json
import re
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from openapi_spec_validator import validate

from config.api import api


APP_JS = Path(__file__).resolve().parents[2] / "apps/application/web_ui/static/web_ui/app.js"

# Every API call in app.js, with the response codes the schema must declare.
# Update this list when app.js calls a new endpoint.
WEB_PAGE_CALLS = {
    ("get", "/api/v1/places/search"): {"200"},
    ("post", "/api/v1/places/resolve"): {"200"},
    ("get", "/api/v1/areas/{square_id}"): {"200", "400", "404", "409", "410"},
    ("get", "/api/v1/forest-layer"): {"200", "503"},
    ("get", "/api/v1/estimate-overview"): {"200", "400", "503"},
    ("get", "/api/v1/squares"): {"200", "400", "503"},
    ("get", "/api/v1/info"): {"200", "503"},
}


def _paths_in_app_js() -> set[str]:
    """Return each /api/v1 path that app.js names, with ${...} parts replaced by a marker.
    The "estimate-overview" or "squares" choice becomes two paths."""
    source = APP_JS.read_text(encoding="utf-8")
    paths = set()
    for match in re.finditer(r'`(/api/v1/[^`?]*)|"(/api/v1/[^"?]*)"', source):
        path = match.group(1) or match.group(2)
        if path.startswith("/api/v1/${overview "):
            paths.update({"/api/v1/estimate-overview", "/api/v1/squares"})
            continue
        paths.add(re.sub(r"\$\{[^}]*\}", "{param}", path))
    return paths


def _served_schema() -> dict:
    """Return the schema as /api/v1/openapi.json serves it.
    Ninja keys response codes by int, and JSON turns them into strings."""
    return json.loads(json.dumps(api.get_openapi_schema()))


def test_schema_is_valid_openapi() -> None:
    validate(_served_schema())


def test_schema_reports_version_and_description() -> None:
    info = api.get_openapi_schema()["info"]

    assert info["version"] == "1.0.0"
    assert info["description"] == (
        "Mushroom growing conditions for each map square in Italy, "
        "based on recent weather and forest cover."
    )


def test_list_covers_every_path_in_app_js() -> None:
    listed = {re.sub(r"\{[^}]*\}", "{param}", path) for _, path in WEB_PAGE_CALLS}

    assert _paths_in_app_js() == listed


def test_schema_has_every_web_page_call_with_its_response_codes() -> None:
    paths = _served_schema()["paths"]

    for (method, path), codes in WEB_PAGE_CALLS.items():
        assert path in paths, path
        assert method in paths[path], f"{method} {path}"
        assert set(paths[path][method]["responses"]) == codes, f"{method} {path}"


def test_export_command_prints_the_schema_as_json() -> None:
    out = StringIO()

    call_command("export_openapi_schema", stdout=out)

    printed = json.loads(out.getvalue())
    assert printed == _served_schema()
    validate(printed)
