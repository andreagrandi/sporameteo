"""Check that today's forecast runs of the nightly regions are published, and retry the step that stopped."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.db import transaction
from django_tasks_db.models import DBTaskResult

from weather_on_demand.models import ScheduledWeatherRun, WeatherFetchBatch
from weather_on_demand.services import reopen_weather_fetch, start_scheduled_weather_run
from weather_requests.eligibility import ALL_REGIONS, nightly_region_codes, nightly_run_key

from .models import ForecastRun
from .tasks import queue_tuscany_forecast

logger = logging.getLogger(__name__)

PUBLISHED = "published"
NO_WEATHER_RUN = "no_weather_run"
WEATHER_FAILED = "weather_failed"
FORECAST_FAILED = "forecast_failed"
NOT_FINISHED = "not_finished"

_TRACEBACK_LINES = 8


@dataclass(frozen=True)
class NightlyCheckResult:
    """Hold the reason code and the plain message for one date."""

    reason: str
    message: str

    @property
    def is_published(self) -> bool:
        return self.reason == PUBLISHED


def _task_row(*, run: ScheduledWeatherRun) -> DBTaskResult | None:
    if run.forecast_task_id is None:
        return None
    return DBTaskResult.objects.filter(id=run.forecast_task_id).first()


def _region_codes(*, run_key: str) -> tuple[str, ...]:
    if run_key == ALL_REGIONS:
        return nightly_region_codes(value=run_key, forest_build_version=settings.SPORAMETEO_FOREST_BUILD_VERSION)
    return tuple(run_key.split(","))


def _forecast_failure_message(
    *,
    run: ScheduledWeatherRun,
    task: DBTaskResult | None,
    failed: list[ForecastRun],
    unpublished: tuple[str, ...],
) -> str:
    parts = ["The weather was ready, but the forecast run failed."]
    several = "," in run.region_code or run.region_code == ALL_REGIONS
    if several:
        parts.append(f"Regions not published: {', '.join(unpublished)}.")
    if run.reason_code == "forecast_queue_failure":
        parts.append(f"The forecast task could not be queued: {run.full_reason}")
    for item in failed:
        if item.failure_reason:
            label = f"Region {item.region_code} forecast run reason" if several else "Forecast run reason"
            parts.append(f"{label}: {item.failure_reason}")
    if task is not None and task.status == "FAILED":
        tail = "\n".join(str(task.traceback or "").strip().splitlines()[-_TRACEBACK_LINES:])
        parts.append(f"Task error: {task.exception_class_path}\n{tail}".rstrip())
    return "\n".join(parts)


def _unpublished_regions(*, codes: tuple[str, ...], assessment_date: date) -> tuple[str, ...]:
    published = set(
        ForecastRun.objects.filter(
            region_code__in=codes,
            assessment_date=assessment_date,
            status=ForecastRun.Status.PUBLISHED,
        ).values_list("region_code", flat=True)
    )
    return tuple(code for code in codes if code not in published)


def check_nightly_update(*, assessment_date: date, region_code: str | None = None) -> NightlyCheckResult:
    """Read the stored run states and say whether every region has a published forecast run.

    The reason code names the step that stopped when it is not. The default regions come
    from SPORAMETEO_NIGHTLY_REGIONS, and the weather run is found by their run key.
    """
    region_code = nightly_run_key(value=region_code or settings.SPORAMETEO_NIGHTLY_REGIONS)
    codes = _region_codes(run_key=region_code)
    unpublished = _unpublished_regions(codes=codes, assessment_date=assessment_date)
    if not unpublished:
        return NightlyCheckResult(reason=PUBLISHED, message=f"The {assessment_date} forecast run is published.")
    run = ScheduledWeatherRun.objects.filter(region_code=region_code, assessment_date=assessment_date).first()
    if run is None:
        return NightlyCheckResult(
            reason=NO_WEATHER_RUN,
            message=(
                f"No weather run was started for {assessment_date}. "
                "The worker or the cron job is probably down."
            ),
        )
    if run.status == ScheduledWeatherRun.Status.FAILED:
        return NightlyCheckResult(
            reason=WEATHER_FAILED,
            message=(
                f"The weather run for {assessment_date} failed.\n"
                f"Reason code: {run.reason_code}\n"
                f"Full reason: {run.full_reason}"
            ),
        )
    if run.status != ScheduledWeatherRun.Status.COMPLETED:
        return NightlyCheckResult(
            reason=NOT_FINISHED,
            message=f"The weather run for {assessment_date} is still {run.status}.",
        )
    task = _task_row(run=run)
    # A retried task is still waiting while the earlier failed ForecastRun row exists.
    if task is not None and task.status in {"READY", "RUNNING"}:
        return NightlyCheckResult(
            reason=NOT_FINISHED,
            message=f"The weather run is completed and the forecast task is still {task.status}.",
        )
    failed = list(
        ForecastRun.objects.filter(
            region_code__in=unpublished,
            assessment_date=assessment_date,
            status=ForecastRun.Status.FAILED,
        ).order_by("region_code", "created_at")
    )
    if (
        run.reason_code == "forecast_queue_failure"
        or (task is not None and task.status == "FAILED")
        or failed
    ):
        return NightlyCheckResult(
            reason=FORECAST_FAILED,
            message=_forecast_failure_message(run=run, task=task, failed=failed, unpublished=unpublished),
        )
    return NightlyCheckResult(
        reason=NOT_FINISHED,
        message=f"The weather run is completed, but the {assessment_date} forecast run is not published yet.",
    )


def _requeue_forecast(*, run_id: uuid.UUID, assessment_date: date, region_code: str) -> None:
    """Queue a new forecast task for a completed run and store its ID."""
    with transaction.atomic():
        run = ScheduledWeatherRun.objects.select_for_update().get(id=run_id)
        run.forecast_task_id = queue_tuscany_forecast(region_code=region_code, assessment_date=assessment_date)
        if run.reason_code == "forecast_queue_failure":
            run.reason_code = "ready"
            run.full_reason = ""
        run.save(update_fields=["forecast_task_id", "reason_code", "full_reason", "updated_at"])


def retry_nightly_update(
    *,
    assessment_date: date,
    region_code: str | None = None,
) -> NightlyCheckResult:
    """Retry the step that stopped and return the state found before the retry.

    It never sends an email.
    """
    region_code = nightly_run_key(value=region_code or settings.SPORAMETEO_NIGHTLY_REGIONS)
    result = check_nightly_update(assessment_date=assessment_date, region_code=region_code)
    if result.is_published:
        logger.info("Nightly retry for %s: the run is published, nothing to retry.", assessment_date)
        return result

    def start() -> None:
        start_scheduled_weather_run(
            assessment_date=assessment_date,
            region_code=region_code,
            forest_build_version=settings.SPORAMETEO_FOREST_BUILD_VERSION,
        )

    run = ScheduledWeatherRun.objects.filter(region_code=region_code, assessment_date=assessment_date).first()
    if result.reason == NO_WEATHER_RUN:
        start()
        action = "started the weather run"
    elif result.reason == WEATHER_FAILED:
        reopened = 0
        failed_ids = list(
            WeatherFetchBatch.objects.filter(
                scheduled_run=run,
                status=WeatherFetchBatch.Status.FAILED,
            ).values_list("id", flat=True)
        )
        for batch_id in failed_ids:
            # Reopening one batch also reopens its sibling batches, so check the status again.
            if WeatherFetchBatch.objects.get(id=batch_id).status == WeatherFetchBatch.Status.FAILED:
                reopen_weather_fetch(batch_id=batch_id)
                reopened += 1
        start()
        action = f"reopened {reopened} failed weather batches and resumed the weather run"
    elif result.reason == FORECAST_FAILED:
        _requeue_forecast(run_id=run.id, assessment_date=assessment_date, region_code=region_code)
        action = "queued a new forecast task"
    elif run.status != ScheduledWeatherRun.Status.COMPLETED:
        start()
        action = "resumed the weather run"
    elif run.forecast_task_id is None:
        start()
        action = "queued the missing forecast task"
    else:
        action = "nothing, the forecast task is still running"
    logger.info("Nightly retry for %s (%s): %s.", assessment_date, result.reason, action)
    return result
