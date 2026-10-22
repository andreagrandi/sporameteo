"""Check that today's forecast runs of the nightly regions are published, and retry the step that stopped."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from django_tasks_db.models import DBTaskResult, get_date_max

from weather_on_demand.models import ScheduledWeatherRun, WeatherFetchBatch
from weather_on_demand.services import reopen_weather_fetch, start_scheduled_weather_run
from weather_requests.eligibility import ALL_REGIONS, nightly_region_codes, nightly_run_key

from .models import ForecastRun
from .tasks import queue_forecast

logger = logging.getLogger(__name__)

PUBLISHED = "published"
NO_WEATHER_RUN = "no_weather_run"
WEATHER_FAILED = "weather_failed"
FORECAST_FAILED = "forecast_failed"
NOT_FINISHED = "not_finished"
WORKER_DOWN = "worker_down"

_TRACEBACK_LINES = 8
# A due task that waits longer than this means no worker is taking tasks.
WORKER_WAIT_LIMIT = timedelta(minutes=30)


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


def _waiting_since(*, task: DBTaskResult) -> datetime:
    if task.run_after == get_date_max():
        return task.enqueued_at
    return max(task.enqueued_at, task.run_after)


def _stopped_worker_message(*, now: datetime) -> str | None:
    """Say how long the oldest due task has waited, or None when no due task waited past the limit."""
    # A task with a later run_after is not due yet, so it does not count as waiting.
    due = DBTaskResult.objects.filter(Q(run_after=get_date_max()) | Q(run_after__lte=now), status="READY").only(
        "enqueued_at", "run_after"
    )
    if not due:
        return None
    oldest = min(_waiting_since(task=task) for task in due)
    waited = now - oldest
    if waited <= WORKER_WAIT_LIMIT:
        return None
    hours, rest = divmod(int(waited.total_seconds()) // 60, 60)
    last_start = DBTaskResult.objects.aggregate(last=Max("started_at"))["last"]
    started = f"A task last started at {last_start:%Y-%m-%d %H:%M} UTC." if last_start else "No task has ever started."
    return (
        f"No worker is taking tasks. The oldest due task has waited {hours} hours and {rest} minutes, "
        f"since {oldest:%Y-%m-%d %H:%M} UTC. {started}"
    )


def check_nightly_update(*, assessment_date: date, region_code: str | None = None) -> NightlyCheckResult:
    """Read the stored run states and say whether every region has a published forecast run.

    The reason code names the step that stopped when it is not, or worker_down when a due task
    waited past WORKER_WAIT_LIMIT. The default regions come from SPORAMETEO_NIGHTLY_REGIONS.
    """
    result = _check_steps(assessment_date=assessment_date, region_code=region_code)
    if result.is_published:
        return result
    stopped_worker = _stopped_worker_message(now=timezone.now())
    if stopped_worker is None:
        return result
    return NightlyCheckResult(reason=WORKER_DOWN, message=f"{stopped_worker}\n{result.message}")


def _check_steps(*, assessment_date: date, region_code: str | None) -> NightlyCheckResult:
    """Say which step stopped for the night, without looking at the task queue."""
    region_code = nightly_run_key(value=region_code or settings.SPORAMETEO_NIGHTLY_REGIONS)
    codes = _region_codes(run_key=region_code)
    unpublished = _unpublished_regions(codes=codes, assessment_date=assessment_date)
    if not unpublished:
        return NightlyCheckResult(reason=PUBLISHED, message=f"The {assessment_date} forecast run is published.")
    result = _stopped_step(assessment_date=assessment_date, region_code=region_code, unpublished=unpublished)
    # The forecast failure message already names the regions.
    if len(codes) > 1 and result.reason != FORECAST_FAILED:
        return NightlyCheckResult(
            reason=result.reason,
            message=f"{result.message}\nRegions not published: {', '.join(unpublished)}.",
        )
    return result


def _stopped_step(*, assessment_date: date, region_code: str, unpublished: tuple[str, ...]) -> NightlyCheckResult:
    """Find the step that stopped for a night with at least one unpublished region."""
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
        run.forecast_task_id = queue_forecast(region_code=region_code, assessment_date=assessment_date)
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
    result = _check_steps(assessment_date=assessment_date, region_code=region_code)
    if result.is_published:
        logger.info("Nightly retry for %s: the run is published, nothing to retry.", assessment_date)
        return result
    stopped_worker = _stopped_worker_message(now=timezone.now())
    if stopped_worker is not None:
        # The retry below still runs, so the step resumes once a worker is back.
        logger.warning(
            "Nightly retry for %s: %s A retry cannot help until a worker runs.", assessment_date, stopped_worker
        )

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
    if stopped_worker is not None:
        return NightlyCheckResult(reason=WORKER_DOWN, message=f"{stopped_worker}\n{result.message}")
    return result
