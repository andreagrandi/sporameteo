"""Check that every command in app.json exists and that the nightly cron runs the weather refresh."""

import json
import re
from pathlib import Path

from django.core.management import get_commands


APP_JSON = Path(__file__).resolve().parents[2] / "app.json"
COMMAND = re.compile(r"manage\.py\s+([\w-]+)")


def _commands(*, lines: list[str]) -> set[str]:
    return {name for line in lines for name in COMMAND.findall(line)}


def test_every_command_in_app_json_exists() -> None:
    config = json.loads(APP_JSON.read_text())
    lines = [item["command"] for item in config["cron"]] + config["scripts"]["dokku"]["predeploy"].splitlines()
    names = _commands(lines=lines)
    assert names
    assert names <= set(get_commands())


def test_the_nightly_cron_entry_runs_the_weather_refresh() -> None:
    config = json.loads(APP_JSON.read_text())
    entries = [item for item in config["cron"] if item["schedule"] == "30 0 * * *"]
    assert len(entries) == 1
    assert _commands(lines=[entries[0]["command"]]) == {"refresh_weather"}
