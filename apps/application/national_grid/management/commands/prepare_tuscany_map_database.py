"""Prepare a separate migrated database with static Tuscany map data."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from national_grid.models import GridBuild
from national_grid.subset import (
    DEFAULT_SOURCE_ALIAS,
    DEFAULT_TARGET_ALIAS,
    SourceBuildError,
    TuscanySubsetError,
    prepare_tuscany_subset,
    validate_existing_tuscany_subset,
)


class Command(BaseCommand):
    """Copy completed national static builds into a Tuscany database."""

    help = "Copy completed grid, forest and terrain records into a Tuscany-only database."

    def add_arguments(self, parser: Any) -> None:
        add_source_arguments(parser)
        parser.add_argument("--grid-build-version")
        parser.add_argument("--forest-build-version")
        parser.add_argument("--terrain-build-version")
        parser.add_argument("--region-code", default="09")
        parser.add_argument(
            "--output-dir",
            type=Path,
            default=Path(settings.BASE_DIR).parents[1] / "data/processed/mvp_forest",
        )
        parser.add_argument(
            "--validate-only",
            action="store_true",
            help="Validate an existing subset without writing to either database.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        source_alias = str(options["source_alias"])
        target_alias = str(options["target_alias"])
        try:
            _configure_source_alias(
                source_alias=source_alias,
                target_alias=target_alias,
                name=options.get("source_db_name"),
                user=options.get("source_db_user"),
                password=options.get("source_db_password"),
                host=options.get("source_db_host"),
                port=options.get("source_db_port"),
            )
            source_grid_version = options.get("source_grid_build_version")
            if not source_grid_version:
                source_grid_version = _latest_grid_version(alias=source_alias)
            source_forest_version = str(options["source_forest_build_version"] or "")
            source_terrain_version = str(options["source_terrain_build_version"] or "")
            if not source_forest_version or not source_terrain_version:
                raise CommandError(
                    "Source forest and terrain build versions are required or must be configured."
                )
            grid_version = options.get("grid_build_version") or (
                f"tuscany-grid-{source_grid_version}-56"
            )
            forest_version = options.get("forest_build_version") or (
                f"tuscany-forest-{source_forest_version}-56"
            )
            terrain_version = options.get("terrain_build_version") or (
                f"tuscany-terrain-{source_terrain_version}-56"
            )

            if options["validate_only"]:
                result = validate_existing_tuscany_subset(
                    alias=target_alias,
                    grid_version=grid_version,
                    forest_version=forest_version,
                    terrain_version=terrain_version,
                    region_code=str(options["region_code"]),
                )
            else:
                result = prepare_tuscany_subset(
                    source_alias=source_alias,
                    target_alias=target_alias,
                    source_grid_version=str(source_grid_version),
                    source_forest_version=source_forest_version,
                    source_terrain_version=source_terrain_version,
                    grid_version=grid_version,
                    forest_version=forest_version,
                    terrain_version=terrain_version,
                    output_dir=Path(options["output_dir"]),
                    region_code=str(options["region_code"]),
                ).as_dict()
        except (TuscanySubsetError, SourceBuildError, OSError, KeyError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))


def add_source_arguments(parser: Any) -> None:
    """Add the options that choose the source database and its builds."""
    parser.add_argument("--source-alias", default=DEFAULT_SOURCE_ALIAS)
    parser.add_argument("--target-alias", default=DEFAULT_TARGET_ALIAS)
    parser.add_argument("--source-db-name")
    parser.add_argument("--source-db-user")
    parser.add_argument("--source-db-password")
    parser.add_argument("--source-db-host")
    parser.add_argument("--source-db-port")
    parser.add_argument("--source-grid-build-version")
    parser.add_argument(
        "--source-forest-build-version",
        default=getattr(settings, "SPORAMETEO_FOREST_BUILD_VERSION", ""),
    )
    parser.add_argument(
        "--source-terrain-build-version",
        default=getattr(settings, "SPORAMETEO_TERRAIN_BUILD_VERSION", ""),
    )


def _configure_source_alias(
    *,
    source_alias: str,
    target_alias: str,
    name: str | None,
    user: str | None,
    password: str | None,
    host: str | None,
    port: str | None,
) -> None:
    if source_alias in connections.databases:
        if not any(value is not None for value in (name, user, password, host, port)):
            return
        base = copy.deepcopy(connections.databases[source_alias])
    else:
        base = copy.deepcopy(connections.databases[target_alias])
    base["NAME"] = name or os.getenv("SPORAMETEO_SOURCE_DB_NAME", "sporameteo")
    base["USER"] = user or os.getenv("SPORAMETEO_SOURCE_DB_USER", base.get("USER"))
    base["PASSWORD"] = password or os.getenv("SPORAMETEO_SOURCE_DB_PASSWORD", base.get("PASSWORD"))
    base["HOST"] = host or os.getenv("SPORAMETEO_SOURCE_DB_HOST", base.get("HOST"))
    base["PORT"] = port or os.getenv("SPORAMETEO_SOURCE_DB_PORT", base.get("PORT"))
    connections.databases[source_alias] = base
    connections[source_alias].close()


def _latest_grid_version(*, alias: str) -> str:
    version = (
        GridBuild.objects.using(alias)
        .filter(status=GridBuild.Status.COMPLETED)
        .order_by("-completed_at", "-created_at")
        .values_list("version", flat=True)
        .first()
    )
    if version is None:
        raise CommandError("The source database has no completed national grid build.")
    return str(version)
