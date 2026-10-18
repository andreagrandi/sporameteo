"""Plan or run a read of the Open-Meteo files that stores 38 days of weather for the chosen regions."""

import json
from argparse import ArgumentParser
from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from weather_requests.eligibility import WeatherEligibilityError, nightly_run_key, weather_cells_for_run_key
from weather_summaries.file_weather import plan_file_weather, store_file_weather

from ...services import italian_assessment_date


class Command(BaseCommand):
    help = "Print the plan for reading the Open-Meteo files, or read them and store 38 days of weather."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Print the plan without writing or downloading.")
        parser.add_argument(
            "--assessment-date",
            type=date.fromisoformat,
            default=None,
            help="Italian date to prepare, default today in Europe/Rome.",
        )
        parser.add_argument(
            "--region-code",
            default=str(settings.SPORAMETEO_NIGHTLY_REGIONS),
            help="Region codes such as 09,08, or all. Default from SPORAMETEO_NIGHTLY_REGIONS.",
        )
        parser.add_argument(
            "--forest-build-version",
            default=str(getattr(settings, "SPORAMETEO_FOREST_BUILD_VERSION", "")),
            help="Forest build that lists the weather cells, default from the app setting.",
        )
        parser.add_argument("--json", action="store_true", help="Print the result as JSON.")

    def handle(self, *args, **options) -> None:
        assessment_date = options["assessment_date"] or italian_assessment_date()
        version = options["forest_build_version"]
        if not version:
            raise CommandError("Set SPORAMETEO_FOREST_BUILD_VERSION or pass --forest-build-version.")
        try:
            region_code = nightly_run_key(value=options["region_code"])
            cells = weather_cells_for_run_key(forest_build_version=version, run_key=region_code)
            plan = plan_file_weather(cells=cells, assessment_date=assessment_date)
            if options["dry_run"]:
                self._write_plan(plan=plan.as_dict(), as_json=options["json"])
                return
            read = store_file_weather(cells=cells, assessment_date=assessment_date, region_code=region_code)
        except (WeatherEligibilityError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        summary = {
            "read_id": str(read.id),
            "status": read.status,
            "assessment_date": read.assessment_date.isoformat(),
            "cells_total": read.cells_total,
            "cells_written": read.cells_written,
            "cells_already_ready": read.cells_already_ready,
            "cells_missing": len(read.missing),
            "megabytes_read": round(read.bytes_read / 1_000_000, 1),
            "http_requests": read.http_requests,
            "error": read.error,
        }
        if options["json"]:
            self.stdout.write(json.dumps(summary, indent=2, sort_keys=True))
        else:
            self.stdout.write(" ".join(f"{name}={value}" for name, value in summary.items() if value != ""))
        if read.status == "failed":
            raise CommandError(f"The read failed: {read.error}")

    def _write_plan(self, *, plan: dict, as_json: bool) -> None:
        if as_json:
            self.stdout.write(json.dumps(plan, indent=2, sort_keys=True))
            return
        self.stdout.write(f"Dry run for {plan['assessment_date']}, no data written, nothing downloaded")
        self.stdout.write(f"Weather cells: {plan['cells_total']}, already ready for the date: {plan['cells_already_ready']}")
        self.stdout.write(f"Cells to write: {plan['cells_to_write']}")
        if plan["first_day"] is None:
            self.stdout.write("Days to read: none")
        else:
            self.stdout.write(f"Days to read: {plan['first_day']} through {plan['last_day']}")
        self.stdout.write(f"Chunk files: {plan['chunk_files']}")
        self.stdout.write(f"Estimated download: {plan['estimated_megabytes']} MB")
