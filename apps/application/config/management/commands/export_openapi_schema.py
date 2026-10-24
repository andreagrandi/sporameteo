import json

from django.core.management.base import BaseCommand

from config.api import api


class Command(BaseCommand):
    help = "Print the OpenAPI schema of the API as JSON."

    def handle(self, **options) -> None:
        schema = api.get_openapi_schema()
        self.stdout.write(json.dumps(schema, indent=2, sort_keys=True))
