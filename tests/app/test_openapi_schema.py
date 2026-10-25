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
    ("get", "/api/v1/places/search"): {"200", "400", "429", "502", "504"},
    ("post", "/api/v1/places/resolve"): {"200", "400", "422", "503"},
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

    assert info["version"] == "1.1.1"
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


def test_top_areas_path_lists_its_fields_and_the_unavailable_error() -> None:
    schema = _served_schema()
    operation = schema["paths"]["/api/v1/top-areas"]["get"]

    ok_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    response_schema = schema["components"]["schemas"][ok_ref.rsplit("/", 1)[1]]
    area_ref = response_schema["properties"]["areas"]["items"]["$ref"]
    area_schema = schema["components"]["schemas"][area_ref.rsplit("/", 1)[1]]
    assert set(response_schema["properties"]) == {"target_date", "run_id", "areas"}
    assert set(area_schema["properties"]) == {
        "rank", "square_id", "update_id", "municipality", "region", "latitude", "longitude", "percentage",
    }
    assert "503" in operation["responses"]


def _component(*, schema: dict, ref: str) -> dict:
    """Return the component schema that one $ref points to."""
    return schema["components"]["schemas"][ref.rsplit("/", 1)[1]]


def _ok_schema(*, schema: dict, method: str, path: str) -> dict:
    """Return the component schema of the 200 answer of one operation."""
    operation = schema["paths"][path][method]
    ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    return _component(schema=schema, ref=ref)


def test_place_search_path_lists_its_fields() -> None:
    schema = _served_schema()

    response_schema = _ok_schema(schema=schema, method="get", path="/api/v1/places/search")
    result_schema = _component(schema=schema, ref=response_schema["properties"]["results"]["items"]["$ref"])
    assert set(response_schema["properties"]) == {
        "results", "cached", "complete", "minimum_length", "provider", "attribution",
    }
    assert set(response_schema["required"]) == {"results", "cached"}
    assert set(result_schema["properties"]) == {"name", "context", "latitude", "longitude", "bounds", "osm_id"}


def test_place_resolve_path_lists_its_fields() -> None:
    schema = _served_schema()

    response_schema = _ok_schema(schema=schema, method="post", path="/api/v1/places/resolve")
    assert set(response_schema["properties"]) == {
        "status", "point", "representative", "square_id", "region", "coverage", "source", "resolved_at",
    }


def test_estimate_overview_path_lists_its_fields() -> None:
    schema = _served_schema()

    response_schema = _ok_schema(schema=schema, method="get", path="/api/v1/estimate-overview")
    feature_schema = _component(schema=schema, ref=response_schema["properties"]["features"]["items"]["$ref"])
    properties_schema = _component(schema=schema, ref=feature_schema["properties"]["properties"]["$ref"])
    assert set(response_schema["properties"]) == {
        "type", "run_id", "assessment_date", "target_date", "area_size_m", "features",
    }
    assert set(properties_schema["properties"]) == {
        "overview", "target_date", "state", "percentage", "assessment_date", "calculated_at",
        "forest_square_count", "scored_square_count",
    }


def test_export_command_prints_the_schema_as_json() -> None:
    out = StringIO()

    call_command("export_openapi_schema", stdout=out)

    printed = json.loads(out.getvalue())
    assert printed == _served_schema()
    validate(printed)
