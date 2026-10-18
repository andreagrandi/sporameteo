from __future__ import annotations

import logging
import uuid
from datetime import date

from django.conf import settings
from django.tasks import task

from .services import process_estimate_task

logger = logging.getLogger(__name__)


@task
def estimate_update_task(*, job_id: str) -> str:
    """Run one durable estimate calculation without weather access.

    A task replaced by a newer method returns an empty string."""
    result = process_estimate_task(task_id=job_id)
    return str(result.update.public_id) if result is not None else ""


@task
def warm_published_map_cache_task(*, region_code: str) -> int:
    """Prepare the overview of the published run in the shared map cache without any remote access."""
    from map_api.published import warm_published_map_cache

    return warm_published_map_cache(region_code=region_code)["squares"]


@task
def forecast_task(*, region_code: str, assessment_date: str) -> str:
    """Calculate and publish the forecast run of each region of the run key for one date.
    A region with a published run is skipped, and a failed region does not stop the others.
    The task raises after the last region when any failed, so the task row shows the failure."""
    from weather_requests.eligibility import nightly_region_codes

    from .forecast_runs import ForecastRunError, process_forecast_run, start_forecast_run
    from .models import ForecastRun
    from .services import FOREST_BUILD_SETTING

    day = date.fromisoformat(assessment_date)
    codes = nightly_region_codes(
        value=region_code,
        forest_build_version=str(getattr(settings, FOREST_BUILD_SETTING, "")),
    )
    failures: list[str] = []
    for code in codes:
        if ForecastRun.objects.filter(
            region_code=code, assessment_date=day, status=ForecastRun.Status.PUBLISHED
        ).exists():
            continue
        try:
            run = start_forecast_run(assessment_date=day, region_code=code)
            run = process_forecast_run(run_id=run.id)
        except Exception as exc:
            logger.exception("The forecast run of region %s failed.", code)
            failures.append(f"{code}: {type(exc).__name__}: {exc}")
            continue
        if run.status == ForecastRun.Status.FAILED:
            failures.append(f"{code}: the forecast run {run.id} failed. {run.failure_reason}")
    if failures:
        raise ForecastRunError(f"{len(failures)} of {len(codes)} regions failed. " + " | ".join(failures))
    return "published"


def queue_forecast(*, region_code: str, assessment_date: date) -> uuid.UUID:
    """Queue the forecast task for a date and return the task ID."""
    result = forecast_task.enqueue(
        region_code=region_code,
        assessment_date=assessment_date.isoformat(),
    )
    return uuid.UUID(str(result.id))
