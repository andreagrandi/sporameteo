"""Add the static map data of chosen regions to a database that already holds data."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from national_grid.models import GridBuild, GridRegion
from national_grid.subset import (
    DEFAULT_SOURCE_ALIAS,
    DEFAULT_TARGET_ALIAS,
    SourceBuildError,
    TuscanySubsetError,
    prepare_region_subset,
)


MAX_VERSION_LENGTH = 120


class Command(BaseCommand):
    """Copy completed national static builds of chosen regions into the target database."""

    help = (
        "Copy grid, forest and terrain records of chosen regions into a database that "
        "already holds data. Existing rows are never changed."
    )

    def add_arguments(self, parser: Any) -> None:
        add_source_arguments(parser)
        parser.add_argument(
            "--regions",
            required=True,
            help="Comma separated ISTAT region codes such as 01,02, or all.",
        )
        parser.add_argument("--grid-build-version")
        parser.add_argument("--forest-build-version")
        parser.add_argument("--terrain-build-version")
        parser.add_argument(
            "--output-dir",
            type=Path,
            default=Path(settings.BASE_DIR).parents[1] / "data/processed/mvp_forest",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Check the inputs and print the counts without writing anything.",
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
            region_codes, chose_all = _parse_regions(
                value=str(options["regions"]),
                alias=source_alias,
                grid_version=str(source_grid_version),
            )
            suffix = "all" if chose_all else "-".join(region_codes)
            versions = {
                "grid": options.get("grid_build_version")
                or f"regions-{suffix}-grid-{source_grid_version}",
                "forest": options.get("forest_build_version")
                or f"regions-{suffix}-forest-{source_forest_version}",
                "terrain": options.get("terrain_build_version")
                or f"regions-{suffix}-terrain-{source_terrain_version}",
            }
            for kind, version in versions.items():
                if len(version) > MAX_VERSION_LENGTH:
                    raise CommandError(
                        f"The {kind} build version is longer than {MAX_VERSION_LENGTH} "
                        "characters. Pass a shorter one."
                    )
            result = prepare_region_subset(
                source_alias=source_alias,
                target_alias=target_alias,
                source_grid_version=str(source_grid_version),
                source_forest_version=source_forest_version,
                source_terrain_version=source_terrain_version,
                grid_version=versions["grid"],
                forest_version=versions["forest"],
                terrain_version=versions["terrain"],
                output_dir=Path(options["output_dir"]),
                region_codes=region_codes,
                dry_run=bool(options["dry_run"]),
            ).as_dict()
        except (TuscanySubsetError, SourceBuildError, OSError, KeyError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))


def _parse_regions(*, value: str, alias: str, grid_version: str) -> tuple[list[str], bool]:
    codes = [code.strip() for code in value.split(",") if code.strip()]
    if not codes:
        raise CommandError("--regions needs at least one region code or all.")
    if "all" in codes and codes != ["all"]:
        raise CommandError("Use all on its own, not in a list of codes.")
    try:
        grid = GridBuild.objects.using(alias).get(version=grid_version)
    except GridBuild.DoesNotExist as exc:
        raise CommandError("Requested source build does not exist.") from exc
    all_codes = sorted(
        GridRegion.objects.using(alias).filter(build=grid).values_list("istat_code", flat=True)
    )
    if codes == ["all"]:
        if not all_codes:
            raise CommandError("The source grid build has no regions.")
        return all_codes, True
    return codes, sorted(codes) == all_codes


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
