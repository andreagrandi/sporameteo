"""Check and prepare the stored inputs of several regions before estimates run."""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from estimate_updates.tuscany_inputs import (
    READY,
    RegionStatus,
    TuscanyReport,
    all_region_codes,
    REGION_COMMANDS,
    prepare_region_inputs,
)


DEFAULT_TERRAIN_CACHE = Path(settings.BASE_DIR).parent.parent / "data/raw/mvp_terrain"


class Command(BaseCommand):
    help = "Report input readiness per region and store host-tree values for every forest square of the regions."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--regions", required=True,
            help="ISTAT region codes separated by commas, such as 09,10, or all for every region of the forest build grid.",
        )
        parser.add_argument("--forest-build-version", default=settings.SPORAMETEO_FOREST_BUILD_VERSION)
        parser.add_argument("--terrain-build-version", default=settings.SPORAMETEO_TERRAIN_BUILD_VERSION)
        parser.add_argument("--dry-run", action="store_true", help="Print the inventory without writing or downloading.")
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
        parser.add_argument(
            "--allow-forest-only", action="store_true",
            help="Accept missing JRC files and let estimates use forest only.",
        )
        parser.add_argument(
            "--skip-source-files", action="store_true",
            help="Do not check the forest source files and terrain tiles. Use this on a server that only runs the app.",
        )
        parser.add_argument("--terrain-cache-dir", type=Path, default=DEFAULT_TERRAIN_CACHE)

    def handle(self, *args, **options) -> None:
        forest_version = options["forest_build_version"]
        region_codes = self._region_codes(value=options["regions"], forest_version=forest_version)
        try:
            report = prepare_region_inputs(
                region_codes=region_codes, forest_version=forest_version,
                terrain_version=options["terrain_build_version"],
                terrain_cache_dir=options["terrain_cache_dir"], dry_run=options["dry_run"],
                allow_forest_only=options["allow_forest_only"],
                check_source_files=not options["skip_source_files"], names=REGION_COMMANDS,
            )
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        if options["json"]:
            self.stdout.write(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        else:
            self._write_text(report=report)
        if options["dry_run"]:
            return
        waiting = [item for item in report.regions if item.state != READY]
        if waiting or report.failures:
            names = ", ".join(f"{item.region_code} ({self._problems(region=item)})" for item in waiting)
            sources = "; ".join(f"{item.source} is {item.state}" for item in report.failures)
            raise CommandError(f"Region inputs are not ready. Regions: {names or 'none'}. Sources: {sources}.")

    def _region_codes(self, *, value: str, forest_version: str) -> list[str]:
        if value.strip() == "all":
            codes = all_region_codes(forest_version=forest_version)
            if not codes:
                raise CommandError(f"No forest build named {forest_version} exists, so the regions cannot be listed.")
            return codes
        codes = sorted({item.strip() for item in value.split(",") if item.strip()})
        if not codes:
            raise CommandError("Give at least one region code with --regions.")
        return codes

    def _problems(self, *, region: RegionStatus) -> str:
        return "; ".join(
            f"{item.source} is {item.state} for {item.squares} squares" + (f", such as {item.sample}" if item.sample else "")
            for item in region.problems
        )

    def _write_text(self, *, report: TuscanyReport) -> None:
        mode = "Dry run" if report.dry_run else "Preparation"
        self.stdout.write(f"{mode} for regions {', '.join(report.region_codes)}")
        for region in report.regions:
            label = f"{region.region_code} {region.name}".strip()
            if region.state == READY:
                self.stdout.write(f"{label}: ready, {region.squares} squares, {region.forest_squares} forest squares")
            else:
                self.stdout.write(f"{label}: not ready, {self._problems(region=region)}")
        for item in report.failures:
            self.stdout.write(f"{item.source} is {item.state}. Action: {item.action}")
        ready = sum(1 for item in report.regions if item.state == READY)
        self.stdout.write(
            f"Totals: regions={len(report.regions)}, ready={ready}, "
            f"squares={sum(item.squares for item in report.regions)}, "
            f"forest_squares={sum(item.forest_squares for item in report.regions)}"
        )
        self.stdout.write(f"Elapsed: {report.elapsed_seconds:.2f} seconds")
