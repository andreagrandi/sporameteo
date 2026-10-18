"""Fill the shared map cache for the published runs of the regions."""

from argparse import ArgumentParser

from django.core.management.base import BaseCommand

from map_api.published import published_map, warm_published_map_cache

ALL_REGIONS = "all"


class Command(BaseCommand):
    help = "Build the overview of the published run of each region into the map cache."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--region-code",
            default=ALL_REGIONS,
            help="ISTAT region code of one published region, or all.",
        )

    def handle(self, *args, **options) -> None:
        if options["region_code"] == ALL_REGIONS:
            current = published_map()
            codes = sorted(current.runs) if current is not None else []
        else:
            codes = [options["region_code"]]
        for code in codes:
            counts = warm_published_map_cache(region_code=code)
            self.stdout.write(
                f"Prepared the overview of {counts["squares"]} squares of the published run of region {code}"
            )
        if not codes:
            self.stdout.write("No region has a published run")
