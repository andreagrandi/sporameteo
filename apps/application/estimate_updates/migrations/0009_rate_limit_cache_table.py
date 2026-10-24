from django.core.management import call_command
from django.db import migrations

RATE_LIMIT_CACHE_TABLE = "sporameteo_rate_limit_cache"


def create_rate_limit_cache_table(apps, schema_editor) -> None:
    """Create the table behind the API request counts when it is missing."""
    call_command(
        "createcachetable",
        RATE_LIMIT_CACHE_TABLE,
        database=schema_editor.connection.alias,
        verbosity=0,
    )


def drop_rate_limit_cache_table(apps, schema_editor) -> None:
    """Remove the table behind the API request counts."""
    quoted = schema_editor.connection.ops.quote_name(RATE_LIMIT_CACHE_TABLE)
    schema_editor.execute(f"DROP TABLE IF EXISTS {quoted}")


class Migration(migrations.Migration):

    dependencies = [
        ("estimate_updates", "0008_input_set_per_region"),
    ]

    operations = [
        migrations.RunPython(create_rate_limit_cache_table, drop_rate_limit_cache_table),
    ]
