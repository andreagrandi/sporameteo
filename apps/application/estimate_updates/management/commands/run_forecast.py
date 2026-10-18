"""Plan or run the estimates of one region for one date and publish the finished run."""

import json
from argparse import ArgumentParser
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from estimate_updates.forecast_runs import (
    ForecastRunError,
    forecast_run_report,
    plan_forecast_run,
    process_forecast_run,
    start_forecast_run,
)
from weather_on_demand.services import italian_assessment_date
from weather_requests.eligibility import TUSCANY_ISTAT_CODE


class Command(BaseCommand):
    help = "Print the estimate plan of one region, or calculate all estimates and publish the finished run."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Print the plan without writing.")
        parser.add_argument(
            "--assessment-date",
            type=date.fromisoformat,
            default=None,
            help="Italian date to calculate, default today in Europe/Rome.",
        )
        parser.add_argument("--region-code", default=TUSCANY_ISTAT_CODE, help="ISTAT region code of one region.")
        parser.add_argument("--json", action="store_true", help="Print the result as JSON.")

    def handle(self, *args, **options) -> None:
        assessment_date = options["assessment_date"] or italian_assessment_date()
        region_code = options["region_code"]
        try:
            if options["dry_run"]:
                plan = plan_forecast_run(assessment_date=assessment_date, region_code=region_code)
                self._write_plan(plan=plan.as_dict(), as_json=options["json"])
                return
            run = start_forecast_run(assessment_date=assessment_date, region_code=region_code)
            run = process_forecast_run(run_id=run.id)
            report = forecast_run_report(run=run)
        except ForecastRunError as exc:
            raise CommandError(str(exc)) from exc
        self._write_report(report=report, as_json=options["json"])
        if report["status"] == "failed":
            raise CommandError(f"The run failed and the published run is unchanged. {report['failure_reason']}")

    def _write_plan(self, *, plan: dict, as_json: bool) -> None:
        if as_json:
            self.stdout.write(json.dumps(plan, indent=2, sort_keys=True))
            return
        self.stdout.write(f"Dry run for {plan['assessment_date']}, no data written")
        self.stdout.write(f"Target dates: {plan['target_dates'][0]} through {plan['target_dates'][-1]}")
        self.stdout.write(
            f"Weather cells: {plan['cells_total']}, with ready weather: {plan['cells_with_weather']}"
        )
        self.stdout.write(f"Forest squares: {plan['squares_total']}")
        self.stdout.write(
            f"Grid {plan['grid_build_version']}, forest {plan['forest_build_version']}, "
            f"terrain {plan['terrain_build_version']}"
        )
        self.stdout.write(f"Host-tree source: {plan['host_tree_source_version']}")
        self.stdout.write(f"Method: {plan['method_version']}")

    def _write_report(self, *, report: dict, as_json: bool) -> None:
        if as_json:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
            return
        self.stdout.write(f"Run {report['run_id']} is {report['status']} for {report['assessment_date']}")
        self.stdout.write(f"Weather cells: {report['completed_cells']} of {report['cells']} completed")
        self.stdout.write(f"Forest squares: {report['squares']}, result rows: {report['result_rows']}")
        self.stdout.write(f"Cells with recorded weather gaps: {report['source_gap_cells']}")
        self.stdout.write(f"Runtime: {report['runtime_seconds']} seconds")
        self.stdout.write(f"Peak memory: {report['peak_memory_mb']} MB")
        self.stdout.write(f"Estimate storage: {report['estimate_storage_bytes']} bytes")
        self.stdout.write(f"Published run: {report['current_run_id'] or 'none'}")
        self.stdout.write(f"Previous run: {report['previous_run_id'] or 'none'}")
