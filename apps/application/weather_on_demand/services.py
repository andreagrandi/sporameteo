from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.apps import apps
from django.db import (
    DatabaseError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    connection,
    transaction,
)
from django.db.models import Count, Q, Sum
from django_tasks_db.models import DBTaskResult
from django.utils import timezone

from sporameteo.weather.bucket_files import BucketSource, WeatherFileSource
from sporameteo.weather.openmeteo import FORECAST_URL
from sporameteo.weather.policy import (
    FORECAST_DAYS,
    HISTORY_DAYS,
    PIPELINE_VERSION,
    history_endpoint,
)
from weather_requests.eligibility import (
    TUSCANY_ISTAT_CODE,
    WeatherCell,
    nightly_run_key,
    weather_cells_for_run_key,
)
from weather_requests.planning import (
    DEFAULT_MODEL,
    MAX_LOCATIONS,
    combine_request_specs,
    plan_current_weather_requests,
    request_compatibility_key,
)
from weather_requests.provider import (
    PendingRequestError,
    PermanentProviderError,
    ProviderResult,
    ResponseValidationError,
    WeatherProvider,
    get_or_create_request,
    request_spec_from_request,
)
from weather_requests.models import WeatherRequest, WeatherRequestAttempt
from weather_summaries.file_weather import SOURCE_MODEL as FILE_WEATHER_MODEL
from weather_summaries.file_weather import plan_file_weather, store_file_weather
from weather_summaries.models import FileWeatherRead, WeatherDaily, WeatherSnapshot, WeatherSnapshotDay
from weather_summaries.provider_adapter import normalized_provider_responses
from weather_summaries.services import (
    WeatherDailyConflictError,
    build_weather_snapshot,
    store_weather_daily_rows,
    store_weather_response,
    stored_history_rows,
)

from .models import (
    ScheduledWeatherRun,
    WeatherCleanupJob,
    WeatherFetch,
    WeatherFetchBatch,
    WeatherFetchPart,
    WeatherVisitorReservation,
    WeatherVisitorWindow,
)


ROME = ZoneInfo("Europe/Rome")
VISITOR_WINDOW = timedelta(seconds=60)
VISITOR_CELL_LIMIT = 100
DEFAULT_WAIT_SECONDS = 3.0
RETRY_DELAY = timedelta(minutes=2)
WEATHER_FETCH_TASK_PATH = "weather_on_demand.tasks.weather_fetch_task"
WEATHER_CLEANUP_TASK_PATH = "weather_on_demand.tasks.weather_cleanup_task"
SCHEDULED_START_GAP = timedelta(seconds=90)
SCHEDULED_RUNNING_RECHECK = timedelta(seconds=60)
SCHEDULED_SOURCE = "tuscany_schedule"
_ESTIMATE_CALLBACK: Callable[..., Any] | None = None
_CLEANUP_CALLBACK: Callable[..., Any] | None = None
_FORECAST_CALLBACK: Callable[..., Any] | None = None


def _file_source() -> WeatherFileSource:
    """Return the Open-Meteo file bucket that the nightly run reads, so tests can replace it."""
    return BucketSource()


class WeatherOnDemandError(RuntimeError):
    """Report an invalid or incomplete on-demand weather operation."""


@dataclass(frozen=True)
class CellWeatherResult:
    """Describe one cell using current or previous dated weather."""

    cell_key: str
    state: str
    assessment_date: date | None
    fetched_at: datetime | None
    source: str | None
    snapshot_id: str | None
    reason_code: str
    full_reason: str

    @property
    def status(self) -> str:
        """Return the public state name."""
        return self.state

    def as_dict(self) -> dict[str, Any]:
        """Return the result in the API response shape."""
        return {
            "cell_key": self.cell_key,
            "state": self.state,
            "status": self.state,
            "assessment_date": (
                self.assessment_date.isoformat() if self.assessment_date else None
            ),
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "source": self.source,
            "snapshot_id": self.snapshot_id,
            "reason_code": self.reason_code,
            "full_reason": self.full_reason,
        }


@dataclass(frozen=True)
class WeatherLookupResult:
    """Return one dated result for every requested weather cell."""

    assessment_date: date
    cells: tuple[CellWeatherResult, ...]

    @property
    def by_cell(self) -> dict[str, CellWeatherResult]:
        """Return results indexed by weather cell key."""
        return {item.cell_key: item for item in self.cells}

    def __getitem__(self, cell_key: str) -> CellWeatherResult:
        return self.by_cell[cell_key]

    def as_dict(self) -> dict[str, Any]:
        """Return the public response shape."""
        return {
            "assessment_date": self.assessment_date.isoformat(),
            "cells": [item.as_dict() for item in self.cells],
        }


def _clock_now(clock: Callable[[], datetime] | None = None) -> datetime:
    value = clock() if clock is not None else timezone.now()
    if not isinstance(value, datetime):
        raise TypeError("The weather clock must return a datetime.")
    if timezone.is_naive(value):
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def italian_assessment_date(*, clock: Callable[[], datetime] | None = None) -> date:
    """Return today's date at the Europe/Rome boundary."""
    return _clock_now(clock).astimezone(ROME).date()


def weather_window(*, assessment_date: date) -> tuple[date, date]:
    """Return the first and last date of the 38-day weather window."""
    return (
        assessment_date - timedelta(days=HISTORY_DAYS),
        assessment_date + timedelta(days=FORECAST_DAYS),
    )


def opaque_visitor_hash(*, visitor_id: str | bytes | None) -> str:
    """Hash a visitor identifier without storing its original value."""
    value = "anonymous" if visitor_id is None else str(visitor_id)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def configure_estimate_callback(callback: Callable[..., Any] | None) -> None:
    """Set the optional callback invoked after a snapshot commits."""
    global _ESTIMATE_CALLBACK
    _ESTIMATE_CALLBACK = callback


set_estimate_callback = configure_estimate_callback


def configure_cleanup_callback(callback: Callable[..., Any] | None) -> None:
    """Set the optional estimate-retention callback used by cleanup."""
    global _CLEANUP_CALLBACK
    _CLEANUP_CALLBACK = callback


set_cleanup_callback = configure_cleanup_callback


def configure_forecast_callback(callback: Callable[..., Any] | None) -> None:
    """Set the optional callback that queues the forecast task for a completed run."""
    global _FORECAST_CALLBACK
    _FORECAST_CALLBACK = callback


def _notify_snapshot_ready(
    *,
    snapshot_id: uuid.UUID | str,
    cell_key: str,
    assessment_date: date,
    forest_build_version: str,
) -> None:
    callback = _ESTIMATE_CALLBACK
    if callback is None:
        return
    try:
        callback(
            snapshot_id=str(snapshot_id),
            cell_key=cell_key,
            assessment_date=assessment_date,
            forest_build_version=forest_build_version,
        )
    except Exception:
        return


def _dispatch_estimate_callback(*, fetch_id: uuid.UUID) -> None:
    """Run the optional estimate callback once, under its own lock.

    It returns at once if another session is already running the callback.
    """
    if _ESTIMATE_CALLBACK is None:
        return
    from background_jobs.services import logical_job_locks

    with logical_job_locks(
        logical_keys=estimate_callback_lock_keys(fetch_id=fetch_id),
        wait=False,
    ) as acquired:
        if not acquired:
            return
        _run_estimate_callback(fetch_id=fetch_id)


def _run_estimate_callback(*, fetch_id: uuid.UUID) -> None:
    """Run the optional estimate callback and retain failures for retry."""
    callback = _ESTIMATE_CALLBACK
    if callback is None:
        return
    with transaction.atomic():
        fetch = (
            WeatherFetch.objects.select_for_update()
            .filter(id=fetch_id, status=WeatherFetch.Status.COMPLETED, snapshot__isnull=False)
            .first()
        )
        if fetch is None or fetch.estimate_callback_status in {"completed", "running"}:
            return
        now = timezone.now()
        fetch.estimate_callback_status = "running"
        fetch.estimate_callback_started_at = now
        fetch.estimate_callback_attempts += 1
        fetch.save(
            update_fields=[
                "estimate_callback_status",
                "estimate_callback_started_at",
                "estimate_callback_attempts",
                "updated_at",
            ]
        )
    request = WeatherRequest.objects.filter(id=fetch.request_id).first()
    try:
        result = callback(
            snapshot_id=str(fetch.snapshot_id),
            cell_key=fetch.cell_key,
            assessment_date=fetch.assessment_date,
            forest_build_version=(request.forest_build_version if request else ""),
        )
        if (
            result is not None
            and getattr(result, "state", None) == "waiting"
            and getattr(result, "task_id", None) is None
        ):
            raise RuntimeError("The estimate callback returned a waiting task without a queue ID.")
    except Exception as exc:
        WeatherFetch.objects.filter(id=fetch.id).update(
            estimate_callback_status="pending",
            estimate_callback_retry_at=now + RETRY_DELAY,
            estimate_callback_started_at=None,
            estimate_callback_reason=str(exc),
            updated_at=timezone.now(),
        )
        return
    WeatherFetch.objects.filter(id=fetch.id).update(
        estimate_callback_status="completed",
        estimate_callback_retry_at=None,
        estimate_callback_started_at=None,
        estimate_callback_reason="",
        updated_at=timezone.now(),
    )


def notify_snapshot_ready(
    *,
    snapshot_id: uuid.UUID | str,
    cell_key: str,
    assessment_date: date,
    forest_build_version: str = "",
) -> None:
    """Invoke the configured estimate adapter after snapshot storage."""
    _notify_snapshot_ready(
        snapshot_id=snapshot_id,
        cell_key=cell_key,
        assessment_date=assessment_date,
        forest_build_version=forest_build_version,
    )


def weather_fetch_lock_key(*, cell_key: str, assessment_date: date) -> int:
    """Return a signed advisory lock key for one cell and Italian date."""
    value = f"weather-fetch:{cell_key}:{assessment_date.isoformat()}"
    return int.from_bytes(
        hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )


def weather_task_lock_keys(*, batch_id: uuid.UUID | str) -> tuple[str, ...]:
    """Return every advisory key claimed by one weather queue task."""
    parsed_batch_id = uuid.UUID(str(batch_id))
    batch = (
        WeatherFetchBatch.objects.filter(id=parsed_batch_id)
        .prefetch_related("fetches", "parts__fetch")
        .first()
    )
    if batch is None:
        return ()
    fetches = {fetch.id: fetch for fetch in batch.fetches.all()}
    fetches.update({part.fetch.id: part.fetch for part in batch.parts.all()})
    return tuple(
        [batch.logical_key]
        + [
            f"weather-fetch:{fetch.cell_key}:{fetch.assessment_date.isoformat()}"
            for fetch in sorted(fetches.values(), key=lambda item: item.cell_key)
        ]
    )


def estimate_callback_lock_keys(*, fetch_id: uuid.UUID | str) -> tuple[str, ...]:
    """Return the advisory key held while one fetch's estimate callback runs."""
    return (f"estimate-callback:{fetch_id}",)


def weather_cleanup_task_lock_keys(*, job_id: uuid.UUID | str) -> tuple[str, ...]:
    """Return the advisory key claimed by one cleanup queue task."""
    job = WeatherCleanupJob.objects.filter(id=uuid.UUID(str(job_id))).first()
    return (job.logical_key,) if job is not None else ()


@contextmanager
def weather_cell_locks(
    *,
    keys: Iterable[tuple[str, date]],
    wait: bool,
) -> Iterator[bool]:
    """Hold sorted PostgreSQL advisory locks for weather cell dates."""
    values = sorted({(str(cell), day) for cell, day in keys})
    if not values:
        yield True
        return
    lock_ids = [
        weather_fetch_lock_key(cell_key=cell, assessment_date=day)
        for cell, day in values
    ]
    acquired: list[int] = []
    try:
        with connection.cursor() as cursor:
            for lock_id in lock_ids:
                if wait:
                    cursor.execute("SELECT pg_advisory_lock(%s)", [lock_id])
                    locked = True
                else:
                    cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
                    locked = bool(cursor.fetchone()[0])
                if not locked:
                    break
                acquired.append(lock_id)
        if len(acquired) != len(lock_ids):
            if acquired:
                with connection.cursor() as cursor:
                    for lock_id in reversed(acquired):
                        cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
            yield False
            return
        yield True
    except DatabaseError:
        if connection.vendor != "postgresql":
            yield True
            return
        raise
    finally:
        if acquired and len(acquired) == len(lock_ids):
            with connection.cursor() as cursor:
                for lock_id in reversed(acquired):
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])


def _previous_snapshot(*, cell_key: str, assessment_date: date) -> WeatherSnapshot | None:
    return (
        WeatherSnapshot.objects.filter(
            cell_key=cell_key,
            assessment_date__lt=assessment_date,
            status=WeatherSnapshot.Status.READY,
            pipeline_version=PIPELINE_VERSION,
        )
        .order_by("-assessment_date", "-created_at")
        .first()
    )


def _snapshot_for_date(*, cell_key: str, assessment_date: date) -> WeatherSnapshot | None:
    return WeatherSnapshot.objects.filter(
        cell_key=cell_key,
        assessment_date=assessment_date,
        status=WeatherSnapshot.Status.READY,
        pipeline_version=PIPELINE_VERSION,
    ).first()


def _stored_dates(*, snapshot: WeatherSnapshot | None) -> set[date]:
    if snapshot is None:
        return set()
    return set(snapshot.days.values_list("date", flat=True))


def _ensure_visitor_window(*, visitor_hash: str, now: datetime) -> WeatherVisitorWindow:
    try:
        return WeatherVisitorWindow.objects.select_for_update().get(
            visitor_hash=visitor_hash
        )
    except WeatherVisitorWindow.DoesNotExist:
        try:
            WeatherVisitorWindow.objects.create(
                visitor_hash=visitor_hash,
                window_started_at=now,
                new_cell_count=0,
            )
        except IntegrityError:
            pass
        return WeatherVisitorWindow.objects.select_for_update().get(
            visitor_hash=visitor_hash
        )


def _reserve_visitor_slots(
    *,
    visitor_hash: str,
    cell_keys: Sequence[str],
    assessment_date: date,
    now: datetime,
) -> set[str]:
    if not cell_keys:
        return set()
    with transaction.atomic():
        window = _ensure_visitor_window(visitor_hash=visitor_hash, now=now)
        cutoff = now - VISITOR_WINDOW
        WeatherVisitorReservation.objects.filter(
            visitor_hash=visitor_hash,
            reserved_at__lte=cutoff,
        ).delete()
        active_count = WeatherVisitorReservation.objects.filter(
            visitor_hash=visitor_hash,
            reserved_at__gt=cutoff,
        ).count()
        available = max(0, VISITOR_CELL_LIMIT - active_count)
        accepted_keys = set(sorted(cell_keys)[:available])
        WeatherVisitorReservation.objects.bulk_create(
            [
                WeatherVisitorReservation(
                    visitor_hash=visitor_hash,
                    cell_key=cell_key,
                    assessment_date=assessment_date,
                    reserved_at=now,
                )
                for cell_key in sorted(accepted_keys)
            ]
        )
        active_reservations = WeatherVisitorReservation.objects.filter(
            visitor_hash=visitor_hash,
            reserved_at__gt=cutoff,
        )
        active_count = active_reservations.count()
        oldest = active_reservations.order_by("reserved_at").values_list(
            "reserved_at", flat=True
        ).first()
        window.window_started_at = oldest or now
        window.new_cell_count = active_count
        window.save(update_fields=["window_started_at", "new_cell_count", "updated_at"])
        return accepted_keys


def _ensure_fetch(
    *,
    cell_key: str,
    assessment_date: date,
    previous_snapshot: WeatherSnapshot | None,
    visitor_hash: str,
    now: datetime,
) -> tuple[WeatherFetch, bool]:
    fetch = (
        WeatherFetch.objects.select_for_update()
        .filter(cell_key=cell_key, assessment_date=assessment_date, pipeline_version=PIPELINE_VERSION)
        .first()
    )
    if fetch is None:
        try:
            with transaction.atomic():
                fetch = WeatherFetch.objects.create(
                    pipeline_version=PIPELINE_VERSION,
                    cell_key=cell_key,
                    assessment_date=assessment_date,
                    previous_snapshot=previous_snapshot,
                    visitor_hash=visitor_hash,
                    reason_code="fetch_pending",
                    first_requested_at=now,
                )
            return fetch, True
        except IntegrityError:
            fetch = WeatherFetch.objects.select_for_update().get(
                pipeline_version=PIPELINE_VERSION,
                cell_key=cell_key,
                assessment_date=assessment_date,
            )
    return fetch, False


def _mark_visitor_limited(
    *,
    fetch: WeatherFetch,
    visitor_hash: str,
    now: datetime,
) -> None:
    fetch.status = WeatherFetch.Status.PENDING
    fetch.reason_code = "visitor_limit"
    fetch.full_reason = "This visitor reached the limit of 100 new weather cells in 60 seconds."
    fetch.retry_at = now + VISITOR_WINDOW
    fetch.visitor_hash = visitor_hash
    fetch.save(
        update_fields=[
            "status",
            "reason_code",
            "full_reason",
            "retry_at",
            "visitor_hash",
            "updated_at",
        ]
    )


def _completed_dates(
    *,
    cell_key: str,
    assessment_date: date,
    fetch_id: uuid.UUID | None,
) -> tuple[dict[date, WeatherDaily], set[date]]:
    """Return stored history rows and every date that needs no provider request.

    The dry run and the real plan both call this, so they agree on what is missing.
    """
    history = stored_history_rows(cell_key=cell_key, assessment_date=assessment_date)
    completed = set(history)
    if fetch_id is not None:
        completed.update(_completed_part_dates(fetch_id=fetch_id))
    return history, completed


def _completed_part_dates(*, fetch_id: uuid.UUID) -> set[date]:
    row_ids = {
        str(row_id)
        for part_ids in WeatherFetchPart.objects.filter(
            fetch_id=fetch_id,
            status=WeatherFetchPart.Status.COMPLETED,
        ).values_list("daily_ids", flat=True)
        for row_id in (part_ids or [])
    }
    if not row_ids:
        return set()
    return set(WeatherDaily.objects.filter(id__in=row_ids).values_list("date", flat=True))


def _contiguous_ranges(*, days: Iterable[date]) -> list[tuple[date, date]]:
    ranges: list[tuple[date, date]] = []
    for day in sorted(set(days)):
        if ranges and ranges[-1][1] + timedelta(days=1) == day:
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


def _attach_stored_history(
    *,
    fetch: WeatherFetch,
    history: Mapping[date, WeatherDaily],
    now: datetime,
) -> None:
    """Attach stored history rows to a fetch as completed parts without a request."""
    covered = _completed_part_dates(fetch_id=fetch.id)
    for start, end in _contiguous_ranges(days=set(history) - covered):
        rows = [history[start + timedelta(days=offset)] for offset in range((end - start).days + 1)]
        WeatherFetchPart.objects.get_or_create(
            fetch=fetch,
            start_date=start,
            end_date=end,
            defaults={
                "status": WeatherFetchPart.Status.COMPLETED,
                "daily_ids": [str(row.id) for row in rows],
                "source_ids": list(dict.fromkeys(row.source_id for row in rows)),
                "source_attempt_ids": list(dict.fromkeys(str(row.attempt_id) for row in rows)),
                "reason_code": "stored_history",
                "completed_at": now,
            },
        )


def _plan_batches(
    *,
    cells: Sequence[WeatherCell],
    assessment_date: date,
    visitor_hash: str | None,
    now: datetime,
    model: str,
    forest_build_version: str,
    options: Mapping[str, str] | None,
    scheduled_run: ScheduledWeatherRun | None = None,
) -> tuple[WeatherFetchBatch, ...]:
    """Plan provider batches for the cells, or for a scheduled run when no visitor is given."""
    cells_by_key = {cell.key: cell for cell in cells}
    available: list[tuple[WeatherCell, WeatherFetch, Any]] = []
    pending_batches: dict[uuid.UUID, WeatherFetchBatch] = {}
    endpoint = os.environ.get("SPORAMETEO_WEATHER_ENDPOINT", FORECAST_URL)
    fetch_visitor = visitor_hash or ""
    source = "weather_on_demand" if visitor_hash is not None else SCHEDULED_SOURCE
    with transaction.atomic():
        fetches: dict[str, WeatherFetch] = {}
        trigger_keys: set[str] = set()
        for cell in sorted(cells, key=lambda item: item.key):
            current_snapshot = _snapshot_for_date(
                cell_key=cell.key,
                assessment_date=assessment_date,
            )
            if current_snapshot is not None:
                with weather_cell_locks(
                    keys=[(cell.key, assessment_date)],
                    wait=True,
                ):
                    fetch, _created = _ensure_fetch(
                        cell_key=cell.key,
                        assessment_date=assessment_date,
                        previous_snapshot=None,
                        visitor_hash=fetch_visitor,
                        now=now,
                    )
                if (
                    fetch.status != WeatherFetch.Status.COMPLETED
                    or fetch.snapshot_id != current_snapshot.id
                ):
                    fetch.status = WeatherFetch.Status.COMPLETED
                    fetch.snapshot = current_snapshot
                    fetch.fetched_at = current_snapshot.latest_retrieval
                    fetch.completed_at = now
                    fetch.reason_code = "ready"
                    fetch.full_reason = ""
                    fetch.save(
                        update_fields=[
                            "status",
                            "snapshot",
                            "fetched_at",
                            "completed_at",
                            "reason_code",
                            "full_reason",
                            "updated_at",
                        ]
                    )
                transaction.on_commit(
                    lambda fetch_id=fetch.id: _dispatch_estimate_callback(fetch_id=fetch_id)
                )
                continue
            previous = _previous_snapshot(
                cell_key=cell.key,
                assessment_date=assessment_date,
            )
            with weather_cell_locks(
                keys=[(cell.key, assessment_date)],
                wait=False,
            ) as acquired:
                if not acquired:
                    continue
                fetch, _created = _ensure_fetch(
                    cell_key=cell.key,
                    assessment_date=assessment_date,
                    previous_snapshot=previous,
                    visitor_hash=fetch_visitor,
                    now=now,
                )
            fetches[cell.key] = fetch
            if fetch.status == WeatherFetch.Status.COMPLETED and fetch.snapshot_id:
                continue
            if fetch.status == WeatherFetch.Status.FAILED:
                continue
            active_parts = list(
                WeatherFetchPart.objects.filter(
                    fetch=fetch,
                    status__in=[
                        WeatherFetchPart.Status.PENDING,
                        WeatherFetchPart.Status.RUNNING,
                    ],
                    batch__status__in=[
                        WeatherFetchBatch.Status.PENDING,
                        WeatherFetchBatch.Status.RUNNING,
                    ],
                ).select_related("batch")
            )
            for part in active_parts:
                if part.batch_id is not None:
                    if scheduled_run is not None and part.batch.scheduled_run_id is None:
                        part.batch.scheduled_run = scheduled_run
                        part.batch.save(update_fields=["scheduled_run", "updated_at"])
                    pending_batches[part.batch_id] = part.batch
            if active_parts:
                continue
            if (
                visitor_hash is not None
                and fetch.reason_code == "visitor_limit"
                and fetch.visitor_hash == visitor_hash
            ):
                if fetch.retry_at is None or fetch.retry_at > now:
                    continue
            if fetch.status == WeatherFetch.Status.RUNNING:
                continue
            if (
                fetch.retry_at is None
                or fetch.retry_at <= now
                or (visitor_hash is None and fetch.reason_code == "visitor_limit")
            ):
                trigger_keys.add(cell.key)

        accepted_keys = (
            _reserve_visitor_slots(
                visitor_hash=visitor_hash,
                cell_keys=tuple(trigger_keys),
                assessment_date=assessment_date,
                now=now,
            )
            if visitor_hash is not None
            else set(trigger_keys)
        )
        for cell_key, fetch in fetches.items():
            if fetch.batch_id is not None or fetch.status == WeatherFetch.Status.RUNNING:
                continue
            if (
                visitor_hash is not None
                and fetch.reason_code == "visitor_limit"
                and fetch.visitor_hash == visitor_hash
                and (fetch.retry_at is None or fetch.retry_at > now)
            ):
                continue
            is_new = cell_key in trigger_keys
            if not is_new:
                continue
            if is_new and cell_key not in accepted_keys:
                _mark_visitor_limited(fetch=fetch, visitor_hash=fetch_visitor, now=now)
                continue
            history, completed_dates = _completed_dates(
                cell_key=fetch.cell_key,
                assessment_date=assessment_date,
                fetch_id=fetch.id,
            )
            _attach_stored_history(fetch=fetch, history=history, now=now)
            specs = plan_current_weather_requests(
                locations=[cells_by_key[cell_key]],
                assessment_date=assessment_date,
                completed_dates=completed_dates,
                model=model,
                endpoint=endpoint,
                history_endpoint=history_endpoint(),
                options=options,
                forest_build_version=forest_build_version,
            )
            for spec in specs:
                requested_dates = {
                    spec.start_date + timedelta(days=offset)
                    for offset in range((spec.end_date - spec.start_date).days + 1)
                }
                if requested_dates.issubset(completed_dates):
                    continue
                available.append((cells_by_key[cell_key], fetch, spec))

        groups: dict[tuple[Any, ...], list[tuple[WeatherCell, WeatherFetch, Any]]] = defaultdict(list)
        for item in available:
            groups[request_compatibility_key(spec=item[2])].append(item)
        for group in groups.values():
            for offset in range(0, len(group), MAX_LOCATIONS):
                chunk = group[offset : offset + MAX_LOCATIONS]
                combined = combine_request_specs(specs=[item[2] for item in chunk])
                request = get_or_create_request(
                    spec=combined,
                    provenance={"source": source, "assessment_date": assessment_date.isoformat()},
                )
                logical_key = f"weather-fetch:{assessment_date.isoformat()}:{combined.digest}"
                batch, _created = WeatherFetchBatch.objects.get_or_create(
                    logical_key=logical_key,
                    defaults={
                        "assessment_date": assessment_date,
                        "request": request,
                        "canonical_spec": combined.as_dict(),
                        "scheduled_run": scheduled_run,
                    },
                )
                if scheduled_run is not None and batch.scheduled_run_id is None:
                    batch.scheduled_run = scheduled_run
                    batch.save(update_fields=["scheduled_run", "updated_at"])
                if batch.request_id != request.id:
                    raise WeatherOnDemandError("A weather batch logical key has another request.")
                for _cell, fetch, _spec in chunk:
                    if fetch.batch_id is None:
                        fetch.batch = batch
                    fetch.request = request
                    fetch.status = WeatherFetch.Status.PENDING
                    fetch.reason_code = "fetch_pending"
                    fetch.full_reason = ""
                    fetch.retry_at = None
                    fetch.save(
                        update_fields=[
                            "batch",
                            "request",
                            "status",
                            "reason_code",
                            "full_reason",
                            "retry_at",
                            "updated_at",
                        ]
                    )
                    part, _part_created = WeatherFetchPart.objects.get_or_create(
                        fetch=fetch,
                        start_date=combined.start_date,
                        end_date=combined.end_date,
                        defaults={
                            "batch": batch,
                            "request": request,
                            "reason_code": "fetch_pending",
                        },
                    )
                    if part.batch_id != batch.id or part.request_id != request.id:
                        raise WeatherOnDemandError(
                            "A weather fetch part has another durable batch or request."
                        )
                    if part.status in {
                        WeatherFetchPart.Status.FAILED,
                        WeatherFetchPart.Status.PENDING,
                    }:
                        part.status = WeatherFetchPart.Status.PENDING
                        part.retry_at = None
                        part.reason_code = "fetch_pending"
                        part.full_reason = ""
                        part.save(
                            update_fields=[
                                "status",
                                "retry_at",
                                "reason_code",
                                "full_reason",
                                "updated_at",
                            ]
                        )
                pending_batches[batch.id] = batch
    return tuple(pending_batches.values())


def _enqueue_batch(*, batch_id: uuid.UUID, run_after: datetime | None = None) -> uuid.UUID | None:
    from .tasks import weather_fetch_task

    try:
        task = weather_fetch_task.using(run_after=run_after) if run_after else weather_fetch_task
        with transaction.atomic():
            batch = WeatherFetchBatch.objects.select_for_update().get(id=batch_id)
            if _queue_task_is_active(task_id=batch.task_id):
                return batch.task_id
            task_result = task.enqueue(batch_id=str(batch_id))
            task_id = uuid.UUID(str(task_result.id))
            batch.task_id = task_id
            batch.save(update_fields=["task_id", "updated_at"])
            return task_id
    except Exception as exc:
        WeatherFetchBatch.objects.filter(id=batch_id).update(
            task_id=None,
            status=WeatherFetchBatch.Status.PENDING,
            reason_code="queue_failure",
            full_reason=str(exc),
            retry_at=timezone.now() + RETRY_DELAY,
            updated_at=timezone.now(),
        )
        return None


def _schedule_batch(*, batch: WeatherFetchBatch, run_after: datetime | None = None) -> None:
    if batch.scheduled_run_id is not None:
        # The scheduled run task starts its batches one at a time.
        return
    if run_after is None and batch.retry_at is not None and batch.retry_at > timezone.now():
        run_after = batch.retry_at
    if _queue_task_is_active(task_id=batch.task_id):
        return
    transaction.on_commit(
        lambda: _enqueue_batch(batch_id=batch.id, run_after=run_after)
    )


def _queue_task_is_active(
    *,
    task_id: uuid.UUID | None,
    logical_keys: Iterable[str] = (),
) -> bool:
    """Return whether a queued task is live or still waiting to run."""
    if task_id is None:
        return False
    task_result = DBTaskResult.objects.filter(id=task_id).first()
    if task_result is None or task_result.status not in {"READY", "RUNNING"}:
        return False
    if task_result.status == "READY":
        return True
    keys = tuple(logical_keys)
    if not keys:
        return True
    from background_jobs.services import logical_job_locks

    with logical_job_locks(logical_keys=keys, wait=False) as acquired:
        if not acquired:
            return True
        _fail_running_queue_task(task_id=task_id)
    return False


def _fail_running_queue_task(*, task_id: uuid.UUID) -> None:
    """Mark a queue row left running by a stopped worker as failed."""
    with transaction.atomic():
        current = DBTaskResult.objects.select_for_update().filter(id=task_id).first()
        if current is not None and current.status == "RUNNING":
            current.set_failed(RuntimeError("The worker stopped before the task completed."))


def _start_injected_batch(
    *,
    batch_id: uuid.UUID,
    transport: Callable[..., Any] | None,
    clock: Callable[[], datetime] | None,
    provider: WeatherProvider | None,
) -> None:
    def run() -> None:
        from django.db import close_old_connections

        close_old_connections()
        try:
            batch = WeatherFetchBatch.objects.get(id=batch_id)
            part_keys = list(
                batch.parts.values_list("fetch__cell_key", "fetch__assessment_date")
            )
            if not part_keys:
                part_keys = list(
                    batch.fetches.values_list("cell_key", "assessment_date")
                )
            with weather_cell_locks(keys=part_keys, wait=True):
                process_weather_fetch_batch(
                    batch_id=batch_id,
                    transport=transport,
                    clock=clock,
                    provider=provider,
                )
        finally:
            close_old_connections()

    threading.Thread(target=run, name="weather-on-demand-test-fetch", daemon=True).start()


def _claim_batch(*, batch_id: uuid.UUID, now: datetime) -> WeatherFetchBatch | None:
    with transaction.atomic():
        batch = (
            WeatherFetchBatch.objects.select_for_update()
            .select_related("request")
            .filter(id=batch_id)
            .first()
        )
        if batch is None or batch.status in {
            WeatherFetchBatch.Status.COMPLETED,
            WeatherFetchBatch.Status.FAILED,
        }:
            return None
        if batch.status == WeatherFetchBatch.Status.RUNNING:
            return None
        if batch.retry_at is not None and batch.retry_at > now:
            return None
        batch.status = WeatherFetchBatch.Status.RUNNING
        batch.started_at = now
        batch.reason_code = "fetch_running"
        batch.full_reason = ""
        batch.save(
            update_fields=[
                "status",
                "started_at",
                "reason_code",
                "full_reason",
                "updated_at",
            ]
        )
        batch.fetches.filter(status=WeatherFetch.Status.PENDING).update(
            status=WeatherFetch.Status.RUNNING,
            started_at=now,
            reason_code="fetch_running",
            full_reason="",
            updated_at=now,
        )
        WeatherFetch.objects.filter(
            id__in=batch.parts.values_list("fetch_id", flat=True),
            status=WeatherFetch.Status.PENDING,
        ).update(
            status=WeatherFetch.Status.RUNNING,
            started_at=now,
            reason_code="fetch_running",
            full_reason="",
            updated_at=now,
        )
        batch.parts.filter(status=WeatherFetchPart.Status.PENDING).update(
            status=WeatherFetchPart.Status.RUNNING,
            started_at=now,
            reason_code="fetch_running",
            full_reason="",
            updated_at=now,
        )
        return batch


def _retry_reason(*, error: BaseException, status: int | None = None) -> str:
    if status == 429:
        return "provider_refused"
    if isinstance(error, PendingRequestError):
        return "provider_unavailable"
    if isinstance(error, (PermanentProviderError, ResponseValidationError)):
        return "provider_refused"
    return "storage_recovery"


def _is_transient_storage_error(*, error: BaseException) -> bool:
    """Return whether an unexpected storage error can be retried safely."""
    return isinstance(error, (OperationalError, InterfaceError, InterruptedError, TimeoutError))


def _pending_reason(*, batch: WeatherFetchBatch, error: PendingRequestError) -> str:
    """Map a pending provider result to a stable visitor-facing reason."""
    if "quota" in str(error).lower():
        return "quota_exhausted"
    attempt = (
        WeatherRequestAttempt.objects.filter(request_id=batch.request_id)
        .order_by("-attempt_number")
        .first()
    )
    if attempt is not None and attempt.http_status == 429:
        return "provider_refused"
    return "provider_unavailable"


def _batch_pending(
    *,
    batch_id: uuid.UUID,
    reason_code: str,
    full_reason: str,
    retry_at: datetime,
    now: datetime,
) -> None:
    with transaction.atomic():
        batch = WeatherFetchBatch.objects.select_for_update().get(id=batch_id)
        batch.status = WeatherFetchBatch.Status.PENDING
        batch.task_id = None
        batch.retry_at = retry_at
        batch.reason_code = reason_code
        batch.full_reason = full_reason
        batch.save(
            update_fields=[
                "status",
                "task_id",
                "retry_at",
                "reason_code",
                "full_reason",
                "updated_at",
            ]
        )
        batch.fetches.filter(status=WeatherFetch.Status.RUNNING).update(
            status=WeatherFetch.Status.PENDING,
            retry_at=retry_at,
            reason_code=reason_code,
            full_reason=full_reason,
            updated_at=now,
        )
        WeatherFetch.objects.filter(
            id__in=batch.parts.values_list("fetch_id", flat=True),
            status=WeatherFetch.Status.RUNNING,
        ).update(
            status=WeatherFetch.Status.PENDING,
            retry_at=retry_at,
            reason_code=reason_code,
            full_reason=full_reason,
            updated_at=now,
        )
        batch.parts.filter(status=WeatherFetchPart.Status.RUNNING).update(
            status=WeatherFetchPart.Status.PENDING,
            retry_at=retry_at,
            reason_code=reason_code,
            full_reason=full_reason,
            updated_at=now,
        )
    _schedule_batch(batch=batch, run_after=retry_at)


def _related_weather_scope(
    *,
    batch_id: uuid.UUID,
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Return batches and fetches connected through multipart siblings."""
    batch_ids = {batch_id}
    fetch_ids: set[uuid.UUID] = set()
    while True:
        previous_batch_ids = set(batch_ids)
        previous_fetch_ids = set(fetch_ids)
        fetch_ids.update(
            WeatherFetch.objects.filter(batch_id__in=batch_ids).values_list("id", flat=True)
        )
        fetch_ids.update(
            WeatherFetchPart.objects.filter(batch_id__in=batch_ids).values_list(
                "fetch_id", flat=True
            )
        )
        batch_ids.update(
            WeatherFetch.objects.filter(
                id__in=fetch_ids,
                batch_id__isnull=False,
            ).values_list("batch_id", flat=True)
        )
        batch_ids.update(
            WeatherFetchPart.objects.filter(
                fetch_id__in=fetch_ids,
                batch_id__isnull=False,
            ).values_list("batch_id", flat=True)
        )
        if batch_ids == previous_batch_ids and fetch_ids == previous_fetch_ids:
            return batch_ids, fetch_ids


def _batch_failed(
    *,
    batch_id: uuid.UUID,
    reason_code: str,
    full_reason: str,
    now: datetime,
) -> None:
    """Stop a batch after a permanent provider or validation failure."""
    with transaction.atomic():
        batch = WeatherFetchBatch.objects.get(id=batch_id)
        batch_ids, fetch_ids = _related_weather_scope(batch_id=batch.id)
        locked_batches = list(
            WeatherFetchBatch.objects.select_for_update()
            .filter(id__in=batch_ids)
            .order_by("id")
        )
        if not any(locked.id == batch_id for locked in locked_batches):
            return
        list(
            WeatherFetch.objects.select_for_update()
            .filter(id__in=fetch_ids)
            .order_by("id")
        )
        list(
            WeatherFetchPart.objects.select_for_update()
            .filter(fetch_id__in=fetch_ids)
            .order_by("id")
        )
        WeatherFetchBatch.objects.filter(
            id__in=batch_ids,
            status__in=[WeatherFetchBatch.Status.PENDING, WeatherFetchBatch.Status.RUNNING],
        ).update(
            status=WeatherFetchBatch.Status.FAILED,
            task_id=None,
            retry_at=None,
            reason_code=reason_code,
            full_reason=full_reason,
            updated_at=now,
        )
        WeatherFetch.objects.filter(
            id__in=fetch_ids,
            status__in=[WeatherFetch.Status.PENDING, WeatherFetch.Status.RUNNING],
        ).update(
            status=WeatherFetch.Status.FAILED,
            retry_at=None,
            reason_code=reason_code,
            full_reason=full_reason,
            updated_at=now,
        )
        WeatherFetchPart.objects.filter(
            fetch_id__in=fetch_ids,
            status__in=[WeatherFetchPart.Status.PENDING, WeatherFetchPart.Status.RUNNING],
        ).update(
            status=WeatherFetchPart.Status.FAILED,
            retry_at=None,
            reason_code=reason_code,
            full_reason=full_reason,
            updated_at=now,
        )


def reopen_weather_fetch(*, batch_id: uuid.UUID | str, now: datetime | None = None) -> WeatherFetchBatch:
    """Explicitly reopen one permanently failed provider batch."""
    current = _clock_now(lambda: now) if now is not None else _clock_now()
    parsed_batch_id = uuid.UUID(str(batch_id))
    reopened_batch_ids: set[uuid.UUID] = set()
    with transaction.atomic():
        batch = WeatherFetchBatch.objects.get(id=parsed_batch_id)
        if batch.status != WeatherFetchBatch.Status.FAILED:
            raise WeatherOnDemandError("Only a failed weather batch can be reopened.")
        batch_ids, fetch_ids = _related_weather_scope(batch_id=batch.id)
        locked_batches = list(
            WeatherFetchBatch.objects.select_for_update()
            .filter(id__in=batch_ids)
            .order_by("id")
        )
        batch = next(
            (locked for locked in locked_batches if locked.id == parsed_batch_id),
            None,
        )
        if batch is None or batch.status != WeatherFetchBatch.Status.FAILED:
            raise WeatherOnDemandError("Only a failed weather batch can be reopened.")
        list(
            WeatherFetch.objects.select_for_update()
            .filter(id__in=fetch_ids)
            .order_by("id")
        )
        list(
            WeatherFetchPart.objects.select_for_update()
            .filter(fetch_id__in=fetch_ids)
            .order_by("id")
        )
        request_ids = WeatherFetchBatch.objects.filter(
            id__in=batch_ids,
        ).values_list("request_id", flat=True)
        WeatherRequest.objects.filter(
            id__in=request_ids,
            status=WeatherRequest.Status.FAILED,
        ).update(
            status=WeatherRequest.Status.PENDING,
            retry_at=current,
            failed_at=None,
            latest_provider_message="",
            updated_at=current,
        )
        reopened_batch_ids.update(
            WeatherFetchBatch.objects.filter(
                id__in=batch_ids,
                status__in=[
                    WeatherFetchBatch.Status.FAILED,
                    WeatherFetchBatch.Status.PENDING,
                    WeatherFetchBatch.Status.RUNNING,
                ],
            ).values_list("id", flat=True)
        )
        WeatherFetchBatch.objects.filter(id__in=reopened_batch_ids).update(
            status=WeatherFetchBatch.Status.PENDING,
            task_id=None,
            retry_at=current,
            reason_code="fetch_reopened",
            full_reason="The failed weather batch was explicitly reopened.",
            updated_at=current,
        )
        WeatherFetch.objects.filter(
            id__in=fetch_ids,
            status__in=[
                WeatherFetch.Status.FAILED,
                WeatherFetch.Status.PENDING,
                WeatherFetch.Status.RUNNING,
            ],
        ).update(
            status=WeatherFetch.Status.PENDING,
            retry_at=current,
            reason_code="fetch_reopened",
            full_reason="The failed weather batch was explicitly reopened.",
            updated_at=current,
        )
        WeatherFetchPart.objects.filter(
            fetch_id__in=fetch_ids,
            status__in=[
                WeatherFetchPart.Status.FAILED,
                WeatherFetchPart.Status.PENDING,
                WeatherFetchPart.Status.RUNNING,
            ],
        ).update(
            status=WeatherFetchPart.Status.PENDING,
            retry_at=current,
            reason_code="fetch_reopened",
            full_reason="The failed weather batch was explicitly reopened.",
            updated_at=current,
        )
    for reopened_batch in WeatherFetchBatch.objects.filter(id__in=reopened_batch_ids):
        _schedule_batch(batch=reopened_batch, run_after=current)
    batch = WeatherFetchBatch.objects.get(id=parsed_batch_id)
    batch.refresh_from_db()
    return batch


def _store_batch(
    *,
    batch_id: uuid.UUID,
    result: ProviderResult,
    now: datetime,
) -> None:
    responses = normalized_provider_responses(result=result)
    ready_fetch_ids: list[uuid.UUID] = []
    with transaction.atomic():
        batch = (
            WeatherFetchBatch.objects.select_for_update()
            .select_related("request")
            .get(id=batch_id)
        )
        parts = list(
            batch.parts.select_for_update()
            .select_related("fetch")
            .order_by("fetch__cell_key", "start_date", "end_date")
        )
        if not parts:
            fetches = list(batch.fetches.select_for_update().order_by("cell_key"))
            if len(fetches) != len(responses):
                raise WeatherOnDemandError("The provider answer did not match the fetch batch.")
            for fetch, response in zip(fetches, responses, strict=True):
                stored = store_weather_response(
                    response=response,
                    assessment_date=fetch.assessment_date,
                    previous_snapshot=fetch.previous_snapshot,
                    start=result.spec.start_date,
                    end=result.spec.end_date,
                )
                if stored.snapshot is None:
                    raise WeatherOnDemandError(
                        f"The provider answer did not complete the weather snapshot for {fetch.cell_key}."
                    )
                fetch.status = WeatherFetch.Status.COMPLETED
                fetch.snapshot = stored.snapshot
                fetch.fetched_at = stored.snapshot.latest_retrieval
                fetch.completed_at = now
                fetch.retry_at = None
                fetch.reason_code = "ready"
                fetch.full_reason = ""
                fetch.save(
                    update_fields=[
                        "status",
                        "snapshot",
                        "fetched_at",
                        "completed_at",
                        "retry_at",
                        "reason_code",
                        "full_reason",
                        "updated_at",
                    ]
                )
                ready_fetch_ids.append(fetch.id)
        elif len(parts) != len(responses):
            raise WeatherOnDemandError("The provider answer did not match the fetch batch.")
        else:
            responses_by_cell = {
                response.location.cell_key: response for response in responses
            }
            for part in parts:
                response = responses_by_cell.get(part.fetch.cell_key)
                if response is None:
                    raise WeatherOnDemandError(
                        f"The provider answer omitted {part.fetch.cell_key}."
                    )
                if (
                    response.requested_start != part.start_date
                    or response.requested_end != part.end_date
                ):
                    raise WeatherOnDemandError(
                        f"The provider answer range did not match part {part.id}."
                    )
                if len(parts) == 1 and part.fetch.previous_snapshot_id is None and part.fetch.pipeline_version == 1:
                    stored_result = store_weather_response(
                        response=response,
                        assessment_date=part.fetch.assessment_date,
                        previous_snapshot=None,
                        start=part.start_date,
                        end=part.end_date,
                    )
                    rows = stored_result.daily
                else:
                    rows = store_weather_daily_rows(
                        response=response,
                        start=part.start_date,
                        end=part.end_date,
                    )
                part.daily_ids = [str(row.id) for row in rows]
                part.source_ids = list(dict.fromkeys(row.source_id for row in rows))
                part.source_attempt_ids = list(
                    dict.fromkeys(str(row.attempt_id) for row in rows)
                )
                part.status = WeatherFetchPart.Status.COMPLETED
                part.retry_at = None
                part.completed_at = now
                part.reason_code = "part_stored"
                part.full_reason = ""
                part.save(
                    update_fields=[
                        "daily_ids",
                        "source_ids",
                        "source_attempt_ids",
                        "status",
                        "retry_at",
                        "completed_at",
                        "reason_code",
                        "full_reason",
                        "updated_at",
                    ]
                )

            fetches = {
                fetch.id: fetch
                for fetch in WeatherFetch.objects.select_for_update().filter(
                    id__in={part.fetch_id for part in parts}
                )
            }
            for fetch in fetches.values():
                all_parts = list(
                    WeatherFetchPart.objects.filter(fetch=fetch).order_by("start_date", "end_date")
                )
                if not all_parts or any(
                    part.status != WeatherFetchPart.Status.COMPLETED for part in all_parts
                ):
                    continue
                daily_ids = [
                    row_id
                    for part in all_parts
                    for row_id in (part.daily_ids or [])
                ]
                first_row = (
                    WeatherDaily.objects.filter(id__in=daily_ids, cell_key=fetch.cell_key)
                    .order_by("date", "id")
                    .first()
                )
                if first_row is None:
                    raise WeatherOnDemandError(
                        f"Weather parts for {fetch.cell_key} have no stored daily rows."
                    )
                stored = build_weather_snapshot(
                    pipeline_version=fetch.pipeline_version,
                    cell_key=fetch.cell_key,
                    assessment_date=fetch.assessment_date,
                    reference_elevation_m=first_row.reference_elevation_m,
                    new_daily_row_ids=daily_ids,
                    previous_snapshot=fetch.previous_snapshot,
                )
                if stored.snapshot is None:
                    fetch.status = WeatherFetch.Status.PENDING
                    fetch.retry_at = None
                    fetch.reason_code = "snapshot_incomplete"
                    fetch.full_reason = (
                        "Stored weather parts do not yet cover the full snapshot."
                    )
                    fetch.save(
                        update_fields=[
                            "status",
                            "retry_at",
                            "reason_code",
                            "full_reason",
                            "updated_at",
                        ]
                    )
                    continue
                fetch.status = WeatherFetch.Status.COMPLETED
                fetch.snapshot = stored.snapshot
                fetch.fetched_at = stored.snapshot.latest_retrieval
                fetch.completed_at = now
                fetch.retry_at = None
                fetch.reason_code = "ready"
                fetch.full_reason = ""
                fetch.save(
                    update_fields=[
                        "status",
                        "snapshot",
                        "fetched_at",
                        "completed_at",
                        "retry_at",
                        "reason_code",
                        "full_reason",
                        "updated_at",
                    ]
                )
                ready_fetch_ids.append(fetch.id)
        batch.status = WeatherFetchBatch.Status.COMPLETED
        batch.retry_at = None
        batch.completed_at = now
        batch.reason_code = "ready"
        batch.full_reason = ""
        batch.save(
            update_fields=[
                "status",
                "retry_at",
                "completed_at",
                "reason_code",
                "full_reason",
                "updated_at",
            ]
        )
        for fetch_id in ready_fetch_ids:
            transaction.on_commit(
                lambda fetch_id=fetch_id: _dispatch_estimate_callback(fetch_id=fetch_id)
            )


def process_weather_fetch_batch(
    *,
    batch_id: uuid.UUID | str,
    transport: Callable[..., Any] | None = None,
    clock: Callable[[], datetime] | None = None,
    provider: WeatherProvider | None = None,
) -> bool:
    """Run one claimed batch and atomically store every cell snapshot."""
    parsed_batch_id = uuid.UUID(str(batch_id))
    now = _clock_now(clock)
    batch = _claim_batch(batch_id=parsed_batch_id, now=now)
    if batch is None:
        return False
    request = batch.request
    spec = request_spec_from_request(request=request)
    fetches = list(
        WeatherFetch.objects.filter(
            id__in=batch.parts.values_list("fetch_id", flat=True)
        ).select_related("snapshot")
        if batch.parts.exists()
        else batch.fetches.select_related("snapshot").order_by("cell_key")
    )
    has_no_snapshot = all(fetch.snapshot_id is None for fetch in fetches)
    client = provider or WeatherProvider(transport=transport, clock=clock)
    try:
        result = client.fetch(
            spec=spec,
            provenance={"weather_fetch_batch_id": str(batch.id)},
            allow_completed_refetch=request.status == WeatherRequest.Status.COMPLETED and has_no_snapshot,
        )
        _store_batch(batch_id=parsed_batch_id, result=result, now=_clock_now(clock))
        return True
    except PendingRequestError as exc:
        retry_at = exc.retry_at or (_clock_now(clock) + RETRY_DELAY)
        _batch_pending(
            batch_id=parsed_batch_id,
            reason_code=_pending_reason(batch=batch, error=exc),
            full_reason=str(exc),
            retry_at=retry_at,
            now=_clock_now(clock),
        )
        return False
    except (
        PermanentProviderError,
        ResponseValidationError,
        WeatherDailyConflictError,
        WeatherOnDemandError,
    ) as exc:
        _batch_failed(
            batch_id=parsed_batch_id,
            reason_code=(
                "storage_conflict"
                if isinstance(exc, (WeatherDailyConflictError, WeatherOnDemandError))
                else "provider_refused"
            ),
            full_reason=str(exc),
            now=_clock_now(clock),
        )
        return False
    except Exception as exc:
        if not _is_transient_storage_error(error=exc):
            _batch_failed(
                batch_id=parsed_batch_id,
                reason_code="storage_failure",
                full_reason=str(exc),
                now=_clock_now(clock),
            )
            return False
        retry_at = _clock_now(clock) + RETRY_DELAY
        _batch_pending(
            batch_id=parsed_batch_id,
            reason_code=_retry_reason(error=exc),
            full_reason=str(exc),
            retry_at=retry_at,
            now=_clock_now(clock),
        )
        return False


@dataclass(frozen=True)
class ScheduledPartPlan:
    """Describe one date range that a scheduled run asks the provider for."""

    kind: str
    start_date: date
    end_date: date
    cells: int
    http_requests: int
    counted_calls: Decimal

    def as_dict(self) -> dict[str, Any]:
        """Return the part in a JSON-safe shape."""
        return {
            "kind": self.kind,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "cells": self.cells,
            "http_requests": self.http_requests,
            "counted_calls": float(self.counted_calls),
        }


@dataclass(frozen=True)
class ScheduledWeatherPlan:
    """Describe what one scheduled run would request, without writing anything."""

    assessment_date: date
    cells_total: int
    cells_ready: int
    mode: str
    counted_calls: Decimal
    http_requests: int
    parts: tuple[ScheduledPartPlan, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return the plan in a JSON-safe shape."""
        first_day, last_day = weather_window(assessment_date=self.assessment_date)
        return {
            "assessment_date": self.assessment_date.isoformat(),
            "window_start": first_day.isoformat(),
            "window_end": last_day.isoformat(),
            "cells_total": self.cells_total,
            "cells_ready": self.cells_ready,
            "mode": self.mode,
            "counted_calls": float(self.counted_calls),
            "http_requests": self.http_requests,
            "parts": [part.as_dict() for part in self.parts],
        }


def _scheduled_mode(*, cell_keys: Sequence[str], assessment_date: date) -> str:
    """Return initial when no cell has a ready snapshot for an earlier date."""
    has_earlier = WeatherSnapshot.objects.filter(
        cell_key__in=cell_keys,
        assessment_date__lt=assessment_date,
        status=WeatherSnapshot.Status.READY,
        pipeline_version=PIPELINE_VERSION,
    ).exists()
    return (
        ScheduledWeatherRun.Mode.REFRESH if has_earlier else ScheduledWeatherRun.Mode.INITIAL
    ).value


def plan_scheduled_weather(
    *,
    cells: Sequence[WeatherCell],
    assessment_date: date,
    model: str = DEFAULT_MODEL,
    options: Mapping[str, str] | None = None,
    forest_build_version: str = "",
) -> ScheduledWeatherPlan:
    """Read the database and return what a scheduled run would request.

    The plan makes no writes and no provider calls.
    """
    cell_keys = sorted({cell.key for cell in cells})
    ready_keys = set(
        WeatherSnapshot.objects.filter(
            cell_key__in=cell_keys,
            assessment_date=assessment_date,
            status=WeatherSnapshot.Status.READY,
            pipeline_version=PIPELINE_VERSION,
        ).values_list("cell_key", flat=True)
    )
    endpoint = os.environ.get("SPORAMETEO_WEATHER_ENDPOINT", FORECAST_URL)
    groups: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
    for cell in sorted({cell.key: cell for cell in cells}.values(), key=lambda item: item.key):
        if cell.key in ready_keys:
            continue
        fetch_id = (
            WeatherFetch.objects.filter(
                cell_key=cell.key,
                assessment_date=assessment_date,
                pipeline_version=PIPELINE_VERSION,
            )
            .values_list("id", flat=True)
            .first()
        )
        _history, completed_dates = _completed_dates(
            cell_key=cell.key,
            assessment_date=assessment_date,
            fetch_id=fetch_id,
        )
        specs = plan_current_weather_requests(
            locations=[cell],
            assessment_date=assessment_date,
            completed_dates=completed_dates,
            model=model,
            endpoint=endpoint,
            history_endpoint=history_endpoint(),
            options=options,
            forest_build_version=forest_build_version,
        )
        for spec in specs:
            requested_dates = {
                spec.start_date + timedelta(days=offset)
                for offset in range((spec.end_date - spec.start_date).days + 1)
            }
            if not requested_dates.issubset(completed_dates):
                groups[request_compatibility_key(spec=spec)].append(spec)
    parts = tuple(
        ScheduledPartPlan(
            kind="history" if group[0].end_date < assessment_date else "forecast",
            start_date=group[0].start_date,
            end_date=group[0].end_date,
            cells=len(group),
            http_requests=-(-len(group) // MAX_LOCATIONS),
            counted_calls=sum((spec.reservation_weight for spec in group), Decimal("0")),
        )
        for group in sorted(groups.values(), key=lambda items: (items[0].start_date, items[0].end_date))
    )
    return ScheduledWeatherPlan(
        assessment_date=assessment_date,
        cells_total=len(cell_keys),
        cells_ready=len(ready_keys),
        mode=_scheduled_mode(cell_keys=cell_keys, assessment_date=assessment_date),
        counted_calls=sum((part.counted_calls for part in parts), Decimal("0")),
        http_requests=sum(part.http_requests for part in parts),
        parts=parts,
    )


@dataclass(frozen=True)
class NightlyWeatherPlan:
    """What the nightly run would read from the files and what it still owes the API."""

    assessment_date: date
    cells_total: int
    cells_ready: int
    mode: str
    file_megabytes: float
    api_counted_calls_left: Decimal
    api_http_requests_left: int

    def as_dict(self) -> dict[str, Any]:
        """Return the plan in a JSON-safe shape."""
        first_day, last_day = weather_window(assessment_date=self.assessment_date)
        return {
            "assessment_date": self.assessment_date.isoformat(),
            "window_start": first_day.isoformat(),
            "window_end": last_day.isoformat(),
            "cells_total": self.cells_total,
            "cells_ready": self.cells_ready,
            "mode": self.mode,
            "file_megabytes": self.file_megabytes,
            "api_counted_calls_left": float(self.api_counted_calls_left),
            "api_http_requests_left": self.api_http_requests_left,
        }


def plan_nightly_weather(
    *,
    cells: Sequence[WeatherCell],
    assessment_date: date,
    region_code: str = TUSCANY_ISTAT_CODE,
) -> NightlyWeatherPlan:
    """Return the file size to read and the API calls left, from the database only.

    The API calls left are those of the open fallback batches of the run, so they are 0 without one.
    """
    cell_keys = sorted({cell.key for cell in cells})
    ready = WeatherSnapshot.objects.filter(
        cell_key__in=cell_keys,
        assessment_date=assessment_date,
        status=WeatherSnapshot.Status.READY,
        pipeline_version=PIPELINE_VERSION,
    ).count()
    open_batches = WeatherFetchBatch.objects.filter(
        scheduled_run__region_code=region_code,
        scheduled_run__assessment_date=assessment_date,
        status__in=[WeatherFetchBatch.Status.PENDING, WeatherFetchBatch.Status.RUNNING],
    ).select_related("request")
    counted_calls = Decimal("0")
    http_requests = 0
    for batch in open_batches:
        counted_calls += request_spec_from_request(request=batch.request).reservation_weight
        http_requests += 1
    return NightlyWeatherPlan(
        assessment_date=assessment_date,
        cells_total=len(cell_keys),
        cells_ready=ready,
        mode=_scheduled_mode(cell_keys=cell_keys, assessment_date=assessment_date),
        file_megabytes=plan_file_weather(cells=cells, assessment_date=assessment_date).as_dict()["estimated_megabytes"],
        api_counted_calls_left=counted_calls,
        api_http_requests_left=http_requests,
    )


def scheduled_run_task_lock_keys(*, run_id: uuid.UUID | str) -> tuple[str, ...]:
    """Return the advisory key claimed by one scheduled run queue task."""
    run = ScheduledWeatherRun.objects.filter(id=uuid.UUID(str(run_id))).first()
    if run is None:
        return ()
    return (f"scheduled-weather-run:{run.region_code}:{run.assessment_date.isoformat()}",)


def _enqueue_run(
    *,
    run_id: uuid.UUID,
    run_after: datetime | None = None,
    own_task: bool = False,
) -> uuid.UUID | None:
    """Queue the next task of a run, unless a live task already exists.

    The task that calls this from inside the worker passes own_task, because its
    own queue row is still running at that moment.
    """
    from .tasks import scheduled_weather_run_task

    try:
        task = (
            scheduled_weather_run_task.using(run_after=run_after)
            if run_after
            else scheduled_weather_run_task
        )
        with transaction.atomic():
            run = ScheduledWeatherRun.objects.select_for_update().get(id=run_id)
            if not own_task and _queue_task_is_active(
                task_id=run.task_id,
                logical_keys=scheduled_run_task_lock_keys(run_id=run_id),
            ):
                return run.task_id
            task_result = task.enqueue(run_id=str(run_id))
            run.task_id = uuid.UUID(str(task_result.id))
            run.save(update_fields=["task_id", "updated_at"])
            return run.task_id
    except Exception as exc:
        ScheduledWeatherRun.objects.filter(id=run_id).update(
            task_id=None,
            reason_code="queue_failure",
            full_reason=str(exc),
            updated_at=timezone.now(),
        )
        return None


def start_scheduled_weather_run(
    *,
    assessment_date: date,
    region_code: str = TUSCANY_ISTAT_CODE,
    forest_build_version: str,
    clock: Callable[[], datetime] | None = None,
) -> ScheduledWeatherRun:
    """Create or resume the run for one date and queue its task once.

    The worker reads the files, and a run with fallback batches keeps planning them.
    """
    region_code = nightly_run_key(value=region_code)
    cells = weather_cells_for_run_key(
        forest_build_version=forest_build_version,
        run_key=region_code,
    )
    now = _clock_now(clock)
    with transaction.atomic():
        run, created = ScheduledWeatherRun.objects.select_for_update().get_or_create(
            region_code=region_code,
            assessment_date=assessment_date,
            defaults={
                "forest_build_version": forest_build_version,
                "mode": _scheduled_mode(cell_keys=[cell.key for cell in cells], assessment_date=assessment_date),
            },
        )
        if run.status == ScheduledWeatherRun.Status.COMPLETED:
            if run.forecast_task_id is None:
                return _queue_forecast(run_id=run.id)
            return run
        has_batches = run.batches.exists()
        if run.status == ScheduledWeatherRun.Status.FAILED:
            run.status = ScheduledWeatherRun.Status.PENDING
            run.completed_at = None
            run.reason_code = ""
            run.full_reason = ""
            if not has_batches:
                # A retry reads the files again, so the cells that lacked hours get another try.
                run.file_read = None
            run.save(
                update_fields=["status", "completed_at", "reason_code", "full_reason", "file_read", "updated_at"]
            )
        if has_batches:
            _plan_batches(
                cells=cells,
                assessment_date=assessment_date,
                visitor_hash=None,
                now=now,
                model=FILE_WEATHER_MODEL,
                forest_build_version=forest_build_version,
                options=None,
                scheduled_run=run,
            )
    _enqueue_run(run_id=run.id)
    run.refresh_from_db()
    return run


def _read_scheduled_files(
    *,
    run: ScheduledWeatherRun,
    cells: Sequence[WeatherCell],
    file_source: WeatherFileSource | None,
    now: datetime,
) -> ScheduledWeatherRun:
    """Read the 38 days from the files and store the read on the run.

    A failed read keeps its error on the run as the record of why the API is used.
    """
    try:
        read = store_file_weather(
            cells=cells,
            assessment_date=run.assessment_date,
            region_code=run.region_code,
            source=file_source or _file_source(),
        )
    except ValueError as exc:
        read = FileWeatherRead.objects.create(
            assessment_date=run.assessment_date,
            region_code=run.region_code,
            started_at=now,
            finished_at=now,
            cells_total=len(cells),
            status=FileWeatherRead.Status.FAILED,
            error=str(exc),
        )
    updates: dict[str, Any] = {"file_read": read, "updated_at": now}
    if read.status == FileWeatherRead.Status.FAILED:
        updates |= {"reason_code": "file_read_failed", "full_reason": read.error}
    ScheduledWeatherRun.objects.filter(id=run.id).update(**updates)
    run.refresh_from_db()
    return run


def _plan_file_fallback(
    *,
    run: ScheduledWeatherRun,
    cells: Sequence[WeatherCell],
    now: datetime,
) -> None:
    """Plan the full API run for a run whose file read failed, unless it is planned already."""
    if run.batches.exists():
        return
    fallback = plan_scheduled_weather(
        cells=cells,
        assessment_date=run.assessment_date,
        model=FILE_WEATHER_MODEL,
        forest_build_version=run.forest_build_version,
    )
    ScheduledWeatherRun.objects.filter(id=run.id).update(
        planned_counted_calls=fallback.counted_calls,
        planned_http_requests=fallback.http_requests,
        updated_at=now,
    )
    _plan_batches(
        cells=cells,
        assessment_date=run.assessment_date,
        visitor_hash=None,
        now=now,
        model=FILE_WEATHER_MODEL,
        forest_build_version=run.forest_build_version,
        options=None,
        scheduled_run=run,
    )


def _scheduled_batch_lock_keys(*, batch: WeatherFetchBatch) -> list[tuple[str, date]]:
    keys = list(batch.parts.values_list("fetch__cell_key", "fetch__assessment_date"))
    return keys or list(batch.fetches.values_list("cell_key", "assessment_date"))


def _missing_from_files(*, run: ScheduledWeatherRun, cell_keys: Sequence[str]) -> dict[str, Any]:
    """Return the problems that a complete file read reported for these cells, by sorted cell key."""
    read = run.file_read
    if read is None or read.status != FileWeatherRead.Status.COMPLETE:
        return {}
    return {key: read.missing[key] for key in sorted(cell_keys) if key in read.missing}


def _finish_scheduled_run(*, run: ScheduledWeatherRun, now: datetime) -> ScheduledWeatherRun:
    """Mark a run completed when every weather cell of its regions has a snapshot, otherwise failed."""
    try:
        cells = weather_cells_for_run_key(
            forest_build_version=run.forest_build_version,
            run_key=run.region_code,
        )
    except ValueError as exc:
        status, reason_code, full_reason = ScheduledWeatherRun.Status.FAILED, "scope_error", str(exc)
    else:
        keys = {cell.key for cell in cells}
        ready = set(
            WeatherSnapshot.objects.filter(
                cell_key__in=keys,
                assessment_date=run.assessment_date,
                status=WeatherSnapshot.Status.READY,
                pipeline_version=PIPELINE_VERSION,
            ).values_list("cell_key", flat=True)
        )
        if ready >= keys:
            status, reason_code, full_reason = ScheduledWeatherRun.Status.COMPLETED, "ready", ""
            if run.file_read is not None and run.file_read.status == FileWeatherRead.Status.FAILED:
                # Keep the record of why this night used the API.
                reason_code, full_reason = "file_read_failed", run.file_read.error
        elif missing := _missing_from_files(run=run, cell_keys=sorted(keys - ready)):
            first_key, problem = next(iter(missing.items()))
            dates = ", ".join(problem.get("dates", []))
            status = ScheduledWeatherRun.Status.FAILED
            reason_code = str(problem.get("reason", "missing_hours"))
            full_reason = (
                f"{len(keys - ready)} of {len(keys)} weather cells have no snapshot. "
                f"The first is {first_key}"
                + (f", with no complete day for {dates}." if dates else ".")
                + (f" {problem['error']}" if problem.get("error") else "")
            )
        else:
            blocked = (
                WeatherFetch.objects.filter(
                    cell_key__in=keys - ready,
                    assessment_date=run.assessment_date,
                    pipeline_version=PIPELINE_VERSION,
                )
                .exclude(reason_code="fetch_pending")
                .order_by("cell_key")
                .first()
            )
            status = ScheduledWeatherRun.Status.FAILED
            reason_code = blocked.reason_code if blocked is not None else "not_planned"
            full_reason = (
                f"{len(keys - ready)} of {len(keys)} weather cells have no snapshot. "
                + (blocked.full_reason if blocked is not None else "")
            ).strip()
    with transaction.atomic():
        locked = ScheduledWeatherRun.objects.select_for_update().get(id=run.id)
        locked.status = status
        locked.completed_at = now
        locked.task_id = None
        locked.reason_code = reason_code
        locked.full_reason = full_reason
        locked.save(
            update_fields=[
                "status",
                "completed_at",
                "task_id",
                "reason_code",
                "full_reason",
                "updated_at",
            ]
        )
        if status == ScheduledWeatherRun.Status.COMPLETED:
            return _queue_forecast(run_id=locked.id)
    return locked


def _queue_forecast(*, run_id: uuid.UUID) -> ScheduledWeatherRun:
    """Queue the forecast task once for a completed run and store its task ID."""
    callback = _FORECAST_CALLBACK
    with transaction.atomic():
        run = ScheduledWeatherRun.objects.select_for_update().get(id=run_id)
        if (
            callback is None
            or run.status != ScheduledWeatherRun.Status.COMPLETED
            or run.forecast_task_id is not None
        ):
            return run
        try:
            with transaction.atomic():
                run.forecast_task_id = callback(
                    region_code=run.region_code,
                    assessment_date=run.assessment_date,
                )
        except Exception as exc:
            run.reason_code = "forecast_queue_failure"
            run.full_reason = str(exc)
        else:
            if run.reason_code == "forecast_queue_failure":
                run.reason_code = "ready"
                run.full_reason = ""
        run.save(update_fields=["forecast_task_id", "reason_code", "full_reason", "updated_at"])
    return run


def run_scheduled_weather_step(
    *,
    run_id: uuid.UUID | str,
    transport: Callable[..., Any] | None = None,
    clock: Callable[[], datetime] | None = None,
    provider: WeatherProvider | None = None,
    file_source: WeatherFileSource | None = None,
) -> ScheduledWeatherRun:
    """Read the files once, then start at most one fallback request and queue the next step."""
    parsed_run_id = uuid.UUID(str(run_id))
    now = _clock_now(clock)
    run = ScheduledWeatherRun.objects.get(id=parsed_run_id)
    if run.status in {ScheduledWeatherRun.Status.COMPLETED, ScheduledWeatherRun.Status.FAILED}:
        return run
    ScheduledWeatherRun.objects.filter(id=run.id).update(
        status=ScheduledWeatherRun.Status.RUNNING,
        started_at=run.started_at or now,
        updated_at=now,
    )
    run.refresh_from_db()
    if run.file_read_id is None or run.file_read.status == FileWeatherRead.Status.FAILED:
        try:
            cells = weather_cells_for_run_key(
                forest_build_version=run.forest_build_version,
                run_key=run.region_code,
            )
        except ValueError:
            return _finish_scheduled_run(run=run, now=now)
        if run.file_read_id is None:
            run = _read_scheduled_files(run=run, cells=cells, file_source=file_source, now=now)
        if run.file_read.status == FileWeatherRead.Status.FAILED:
            _plan_file_fallback(run=run, cells=cells, now=now)
    if run.next_start_at is not None and run.next_start_at > now:
        _enqueue_run(run_id=run.id, run_after=run.next_start_at, own_task=True)
        run.refresh_from_db()
        return run
    batch = (
        run.batches.filter(status=WeatherFetchBatch.Status.PENDING)
        .filter(Q(retry_at__isnull=True) | Q(retry_at__lte=now))
        .order_by("logical_key")
        .first()
    )
    if batch is not None:
        attempts_before = WeatherRequestAttempt.objects.filter(request_id=batch.request_id).count()
        with weather_cell_locks(keys=_scheduled_batch_lock_keys(batch=batch), wait=True):
            process_weather_fetch_batch(
                batch_id=batch.id,
                transport=transport,
                clock=clock,
                provider=provider,
            )
        attempts = (
            WeatherRequestAttempt.objects.filter(request_id=batch.request_id).count()
            - attempts_before
        )
        next_start_at = now + SCHEDULED_START_GAP if attempts > 0 else None
        batch.refresh_from_db()
        if batch.status == WeatherFetchBatch.Status.PENDING and batch.retry_at is not None:
            # A quota block or a provider retry time holds the whole run, not only this batch.
            next_start_at = max(next_start_at or batch.retry_at, batch.retry_at)
        if attempts > 0 or next_start_at is not None:
            ScheduledWeatherRun.objects.filter(id=run.id).update(
                provider_attempts=run.provider_attempts + attempts,
                next_start_at=next_start_at or run.next_start_at,
                updated_at=now,
            )
    current = _clock_now(clock)
    run.refresh_from_db()
    open_batches = list(
        run.batches.filter(
            status__in=[WeatherFetchBatch.Status.PENDING, WeatherFetchBatch.Status.RUNNING]
        ).order_by("logical_key")
    )
    if not open_batches:
        return _finish_scheduled_run(run=run, now=current)
    ready_times = [
        current + SCHEDULED_RUNNING_RECHECK
        if open_batch.status == WeatherFetchBatch.Status.RUNNING
        else (open_batch.retry_at or current)
        for open_batch in open_batches
    ]
    run_after = max(min(ready_times), run.next_start_at or current)
    waiting = next(
        (item for item in open_batches if item.retry_at is not None and item.retry_at > current),
        None,
    )
    file_failed = run.file_read is not None and run.file_read.status == FileWeatherRead.Status.FAILED
    if waiting is not None:
        reason_code, full_reason = waiting.reason_code, waiting.full_reason
    elif file_failed:
        reason_code, full_reason = "file_read_failed", run.file_read.error
    else:
        reason_code, full_reason = "", ""
    ScheduledWeatherRun.objects.filter(id=run.id).update(
        reason_code=reason_code,
        full_reason=full_reason,
        updated_at=current,
    )
    _enqueue_run(
        run_id=run.id,
        run_after=run_after if run_after > current else None,
        own_task=True,
    )
    run.refresh_from_db()
    return run


def _recover_scheduled_runs(*, now: datetime) -> int:
    """Queue a task again for every unfinished run that has no live task."""
    recovered = 0
    for run in ScheduledWeatherRun.objects.filter(
        status__in=[ScheduledWeatherRun.Status.PENDING, ScheduledWeatherRun.Status.RUNNING]
    ).order_by("assessment_date"):
        if _queue_task_is_active(
            task_id=run.task_id,
            logical_keys=scheduled_run_task_lock_keys(run_id=run.id),
        ):
            continue
        run_after = run.next_start_at if run.next_start_at and run.next_start_at > now else None
        _enqueue_run(run_id=run.id, run_after=run_after, own_task=True)
        recovered += 1
    return recovered


def _cell_result(
    *,
    cell_key: str,
    assessment_date: date,
) -> CellWeatherResult:
    current = _snapshot_for_date(cell_key=cell_key, assessment_date=assessment_date)
    fetch = WeatherFetch.objects.filter(
        cell_key=cell_key,
        assessment_date=assessment_date,
        pipeline_version=PIPELINE_VERSION,
    ).first()
    if current is not None:
        source = current.source_ids[0] if current.source_ids else None
        from weather_summaries.models import WeatherDaily

        known_missing = current.days.filter(
            daily__status=WeatherDaily.Status.CHECKED_KNOWN_MISSING
        ).exists()
        return CellWeatherResult(
            cell_key=cell_key,
            state="current",
            assessment_date=current.assessment_date,
            fetched_at=current.latest_retrieval,
            source=source,
            snapshot_id=str(current.id),
            reason_code="known_missing" if known_missing else "ready",
            full_reason=(
                "The provider returned a checked known gap for one or more daily values."
                if known_missing
                else ""
            ),
        )
    previous = _previous_snapshot(cell_key=cell_key, assessment_date=assessment_date)
    if previous is not None:
        source = previous.source_ids[0] if previous.source_ids else None
        reason_code = fetch.reason_code if fetch is not None else "fetch_pending"
        full_reason = fetch.full_reason if fetch is not None else "The current assessment is still pending."
        return CellWeatherResult(
            cell_key=cell_key,
            state="previous",
            assessment_date=previous.assessment_date,
            fetched_at=previous.latest_retrieval,
            source=source,
            snapshot_id=str(previous.id),
            reason_code=reason_code,
            full_reason=full_reason,
        )
    reason_code = fetch.reason_code if fetch is not None else "fetch_pending"
    full_reason = fetch.full_reason if fetch is not None else "The first weather fetch has not completed."
    return CellWeatherResult(
        cell_key=cell_key,
        state="not_available_yet",
        assessment_date=None,
        fetched_at=None,
        source=None,
        snapshot_id=None,
        reason_code=reason_code,
        full_reason=full_reason,
    )


def _ready_weather_results(
    *, cells: Sequence[WeatherCell], assessment_date: date,
) -> WeatherLookupResult | None:
    """Read completed cells together without reopening their fetch planning."""
    keys = {cell.key for cell in cells}
    snapshots = {
        row.cell_key: row
        for row in WeatherSnapshot.objects.filter(
            cell_key__in=keys,
            assessment_date=assessment_date,
            pipeline_version=PIPELINE_VERSION,
            status=WeatherSnapshot.Status.READY,
        )
    }
    if snapshots.keys() != keys:
        return None
    fetches = WeatherFetch.objects.filter(
        cell_key__in=keys,
        assessment_date=assessment_date,
        pipeline_version=PIPELINE_VERSION,
        status=WeatherFetch.Status.COMPLETED,
    )
    if _ESTIMATE_CALLBACK is not None:
        fetches = fetches.filter(estimate_callback_status__in=["completed", "running"])
    completed = dict(fetches.values_list("cell_key", "snapshot_id"))
    if any(completed.get(key) != snapshot.id for key, snapshot in snapshots.items()):
        return None
    known_missing = set(WeatherSnapshotDay.objects.filter(
        snapshot_id__in=[snapshot.id for snapshot in snapshots.values()],
        daily__status=WeatherDaily.Status.CHECKED_KNOWN_MISSING,
    ).values_list("snapshot_id", flat=True))
    return WeatherLookupResult(
        assessment_date=assessment_date,
        cells=tuple(
            CellWeatherResult(
                cell_key=cell.key,
                state="current",
                assessment_date=assessment_date,
                fetched_at=snapshots[cell.key].latest_retrieval,
                source=next(iter(snapshots[cell.key].source_ids), None),
                snapshot_id=str(snapshots[cell.key].id),
                reason_code="known_missing" if snapshots[cell.key].id in known_missing else "ready",
                full_reason=(
                    "The provider returned a checked known gap for one or more daily values."
                    if snapshots[cell.key].id in known_missing else ""
                ),
            )
            for cell in sorted(cells, key=lambda item: item.key)
        ),
    )


def request_weather(
    *,
    cells: Sequence[WeatherCell],
    visitor_id: str | bytes | None = None,
    assessment_date: date | None = None,
    clock: Callable[[], datetime] | None = None,
    transport: Callable[..., Any] | None = None,
    provider: WeatherProvider | None = None,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    monotonic: Callable[[], float] | None = None,
    model: str = DEFAULT_MODEL,
    forest_build_version: str = "",
    options: Mapping[str, str] | None = None,
) -> WeatherLookupResult:
    """Plan, queue and briefly wait for weather cells requested by a visitor."""
    if not cells:
        raise ValueError("At least one weather cell is required.")
    target_date = assessment_date or italian_assessment_date(clock=clock)
    if not isinstance(target_date, date) or isinstance(target_date, datetime):
        raise TypeError("assessment_date must be a date.")
    ready = _ready_weather_results(cells=cells, assessment_date=target_date)
    if ready is not None:
        return ready
    visitor_hash = opaque_visitor_hash(visitor_id=visitor_id)
    now = _clock_now(clock)
    batches = _plan_batches(
        cells=cells,
        assessment_date=target_date,
        visitor_hash=visitor_hash,
        now=now,
        model=model,
        forest_build_version=forest_build_version,
        options=options,
    )
    inline = transport is not None or provider is not None
    for batch in batches:
        if inline:
            _start_injected_batch(
                batch_id=batch.id,
                transport=transport,
                clock=clock,
                provider=provider,
            )
        else:
            _schedule_batch(batch=batch)
    monotonic_clock = monotonic or time.monotonic
    deadline = monotonic_clock() + max(0.0, wait_seconds)
    while monotonic_clock() < deadline:
        results = tuple(
            _cell_result(cell_key=cell.key, assessment_date=target_date)
            for cell in sorted(cells, key=lambda item: item.key)
        )
        if all(item.state == "current" for item in results):
            return WeatherLookupResult(assessment_date=target_date, cells=results)
        time.sleep(0.05)
    results = tuple(
        _cell_result(cell_key=cell.key, assessment_date=target_date)
        for cell in sorted(cells, key=lambda item: item.key)
    )
    return WeatherLookupResult(assessment_date=target_date, cells=results)


def fetch_weather_on_demand(**kwargs: Any) -> WeatherLookupResult:
    """Return weather through the public on-demand service."""
    return request_weather(**kwargs)


def _recover_estimate_callbacks(*, now: datetime) -> None:
    """Retry ready fetch callbacks without repeating provider work."""
    if _ESTIMATE_CALLBACK is None:
        return
    fetches = WeatherFetch.objects.filter(
        status=WeatherFetch.Status.COMPLETED,
        snapshot__isnull=False,
        pipeline_version=PIPELINE_VERSION,
    ).filter(
        estimate_callback_status__in=["pending", "running"],
    )
    from background_jobs.services import logical_job_locks

    for fetch in fetches:
        if fetch.estimate_callback_status == "running":
            with logical_job_locks(
                logical_keys=estimate_callback_lock_keys(fetch_id=fetch.id),
                wait=False,
            ) as acquired:
                if not acquired:
                    continue
                WeatherFetch.objects.filter(
                    id=fetch.id,
                    estimate_callback_status="running",
                ).update(
                    estimate_callback_status="pending",
                    estimate_callback_started_at=None,
                    updated_at=now,
                )
        if (
            fetch.estimate_callback_retry_at is not None
            and fetch.estimate_callback_retry_at > now
        ):
            continue
        _dispatch_estimate_callback(fetch_id=fetch.id)


def recover_weather_fetches(*, now: datetime | None = None) -> int:
    """Return batches and cleanup jobs left by a stopped worker to pending queue work.

    It decides by the task's locks, not by age, so a live worker keeps its work.
    """
    current = _clock_now(lambda: now) if now is not None else _clock_now()
    recovered = _recover_running_batches(now=current)
    _recover_pending_batches(now=current)
    recovered += _recover_running_cleanup_jobs(now=current)
    for job in WeatherCleanupJob.objects.filter(
        status=WeatherCleanupJob.Status.PENDING,
    ).order_by("cleanup_date"):
        if job.task_id is not None and _queue_task_is_active(
            task_id=job.task_id,
            logical_keys=weather_cleanup_task_lock_keys(job_id=job.id),
        ):
            continue
        if job.task_id is not None:
            with transaction.atomic():
                current_job = WeatherCleanupJob.objects.select_for_update().get(id=job.id)
                if current_job.task_id == job.task_id:
                    current_job.task_id = None
                    current_job.full_reason = (
                        "The worker stopped after claiming the weather cleanup task."
                    )
                    current_job.save(update_fields=["task_id", "full_reason", "updated_at"])
                    job = current_job
                else:
                    job = current_job
        if job.retry_at is not None and job.retry_at > current:
            continue
        _schedule_cleanup(job=job, run_after=max(current, job.retry_at or current))
    recovered += _recover_scheduled_runs(now=current)
    _recover_estimate_callbacks(now=current)
    return recovered


def recover_abandoned_weather_tasks(*, now: datetime | None = None) -> int:
    """Queue again batches, cleanup jobs and scheduled runs whose worker has stopped.

    It decides by the task's locks, not by age, so it can run next to a live worker.
    """
    current = _clock_now(lambda: now) if now is not None else _clock_now()
    recovered = _recover_running_batches(now=current)
    _recover_pending_batches(now=current)
    recovered += _recover_running_cleanup_jobs(now=current)
    recovered += _recover_scheduled_runs(now=current)
    return recovered


def _recover_running_batches(*, now: datetime) -> int:
    """Return running batches whose locks no worker holds to pending and queue them."""
    from background_jobs.services import logical_job_locks

    recovered = 0
    for candidate in WeatherFetchBatch.objects.filter(
        status=WeatherFetchBatch.Status.RUNNING,
    ).order_by("started_at"):
        logical_keys = weather_task_lock_keys(batch_id=candidate.id)
        with logical_job_locks(logical_keys=logical_keys, wait=False) as acquired:
            if not acquired:
                continue
            if candidate.task_id is not None:
                _fail_running_queue_task(task_id=candidate.task_id)
            batch = _return_batch_to_pending(batch_id=candidate.id, now=now)
        if batch is None:
            continue
        _schedule_batch(batch=batch, run_after=batch.retry_at)
        recovered += 1
    return recovered


def _return_batch_to_pending(
    *,
    batch_id: uuid.UUID,
    now: datetime,
) -> WeatherFetchBatch | None:
    """Mark a running batch left by a stopped worker as waiting to run again."""
    with transaction.atomic():
        batch = (
            WeatherFetchBatch.objects.select_for_update()
            .select_related("request")
            .filter(id=batch_id)
            .first()
        )
        if batch is None or batch.status != WeatherFetchBatch.Status.RUNNING:
            return None
        retry_at = max(
            now + RETRY_DELAY,
            batch.retry_at or now + RETRY_DELAY,
            batch.request.retry_at or now + RETRY_DELAY,
        )
        batch.status = WeatherFetchBatch.Status.PENDING
        batch.task_id = None
        batch.retry_at = retry_at
        batch.reason_code = "worker_interrupted"
        batch.full_reason = "The worker stopped before weather storage completed."
        batch.save(
            update_fields=[
                "status",
                "task_id",
                "retry_at",
                "reason_code",
                "full_reason",
                "updated_at",
            ]
        )
        batch.fetches.filter(status=WeatherFetch.Status.RUNNING).update(
            status=WeatherFetch.Status.PENDING,
            retry_at=retry_at,
            reason_code="worker_interrupted",
            full_reason=batch.full_reason,
            updated_at=now,
        )
        WeatherFetch.objects.filter(
            id__in=batch.parts.values_list("fetch_id", flat=True),
            status=WeatherFetch.Status.RUNNING,
        ).update(
            status=WeatherFetch.Status.PENDING,
            retry_at=retry_at,
            reason_code="worker_interrupted",
            full_reason=batch.full_reason,
            updated_at=now,
        )
        batch.parts.filter(status=WeatherFetchPart.Status.RUNNING).update(
            status=WeatherFetchPart.Status.PENDING,
            retry_at=retry_at,
            reason_code="worker_interrupted",
            full_reason=batch.full_reason,
            updated_at=now,
        )
    return batch


def _recover_pending_batches(*, now: datetime) -> None:
    """Queue pending batches that have no live queue task."""
    for batch in WeatherFetchBatch.objects.filter(
        status=WeatherFetchBatch.Status.PENDING,
    ).order_by("first_requested_at"):
        if batch.task_id is not None and _queue_task_is_active(
            task_id=batch.task_id,
            logical_keys=weather_task_lock_keys(batch_id=batch.id),
        ):
            continue
        if batch.task_id is not None:
            with transaction.atomic():
                current_batch = WeatherFetchBatch.objects.select_for_update().get(id=batch.id)
                if current_batch.task_id == batch.task_id:
                    current_batch.task_id = None
                    current_batch.reason_code = "worker_claim_lost"
                    current_batch.full_reason = (
                        "The worker stopped after claiming the weather task."
                    )
                    current_batch.save(
                        update_fields=["task_id", "reason_code", "full_reason", "updated_at"]
                    )
                    batch = current_batch
                else:
                    batch = current_batch
        if batch.retry_at is not None and batch.retry_at > now:
            continue
        _schedule_batch(batch=batch, run_after=max(now, batch.retry_at or now))


def _recover_running_cleanup_jobs(*, now: datetime) -> int:
    """Return running cleanup jobs whose lock no worker holds to pending and queue them."""
    from background_jobs.services import logical_job_locks

    recovered = 0
    for candidate in WeatherCleanupJob.objects.filter(
        status=WeatherCleanupJob.Status.RUNNING,
    ).order_by("updated_at"):
        logical_keys = weather_cleanup_task_lock_keys(job_id=candidate.id)
        with logical_job_locks(logical_keys=logical_keys, wait=False) as acquired:
            if not acquired:
                continue
            if candidate.task_id is not None and _queue_task_is_active(
                task_id=candidate.task_id,
                logical_keys=logical_keys,
            ):
                continue
            with transaction.atomic():
                job = WeatherCleanupJob.objects.select_for_update().get(id=candidate.id)
                if (
                    job.status != WeatherCleanupJob.Status.RUNNING
                    or job.task_id != candidate.task_id
                ):
                    continue
                retry_at = _return_cleanup_to_pending(job=job, now=now)
                _schedule_cleanup(job=job, run_after=retry_at)
        recovered += 1
    return recovered


def _return_cleanup_to_pending(*, job: WeatherCleanupJob, now: datetime) -> datetime:
    """Mark a cleanup job left by a stopped worker as waiting to run again."""
    retry_at = max(now + RETRY_DELAY, job.retry_at or now + RETRY_DELAY)
    job.status = WeatherCleanupJob.Status.PENDING
    job.task_id = None
    job.retry_at = retry_at
    job.full_reason = "The worker stopped before weather cleanup completed."
    job.save(
        update_fields=[
            "status",
            "task_id",
            "retry_at",
            "full_reason",
            "updated_at",
        ]
    )
    return retry_at


def cleanup_weather(
    *,
    cleanup_date: date,
    protected_snapshot_ids: Iterable[str] = (),
) -> dict[str, int | str]:
    """Delete unreferenced old weather rows without provider access."""
    protected = {str(value) for value in protected_snapshot_ids}
    if _CLEANUP_CALLBACK is not None:
        protected.update(
            str(value)
            for value in (_CLEANUP_CALLBACK(cleanup_date=cleanup_date) or ())
        )
    with transaction.atomic():
        # Read only the columns needed here, so memory does not grow with the JSON fields.
        snapshots = list(
            WeatherSnapshot.objects.order_by("cell_key", "-assessment_date", "-created_at")
            .values_list("id", "cell_key", "assessment_date")
        )
        keep: set[str] = set(protected)
        kept_per_cell: dict[str, int] = defaultdict(int)
        for snapshot_id, cell_key, _assessment_date in snapshots:
            if kept_per_cell[cell_key] < 2:
                keep.add(str(snapshot_id))
                kept_per_cell[cell_key] += 1
        latest_fetch_by_cell: dict[str, tuple[uuid.UUID | None, uuid.UUID | None]] = {}
        for cell_key, snapshot_id, previous_snapshot_id in WeatherFetch.objects.order_by(
            "cell_key", "-assessment_date", "-updated_at"
        ).values_list("cell_key", "snapshot_id", "previous_snapshot_id"):
            latest_fetch_by_cell.setdefault(cell_key, (snapshot_id, previous_snapshot_id))
        keep.update(
            str(value)
            for pair in latest_fetch_by_cell.values()
            for value in pair
            if value is not None
        )
        active_fetches = WeatherFetch.objects.filter(
            status__in=[WeatherFetch.Status.PENDING, WeatherFetch.Status.RUNNING]
        )
        keep.update(
            str(value)
            for value in active_fetches.values_list("snapshot_id", flat=True)
            if value is not None
        )
        keep.update(
            str(value)
            for value in active_fetches.values_list("previous_snapshot_id", flat=True)
            if value is not None
        )
        if _ESTIMATE_CALLBACK is not None:
            keep.update(
                str(value)
                for value in WeatherFetch.objects.filter(
                    status=WeatherFetch.Status.COMPLETED,
                ).exclude(estimate_callback_status="completed").values_list(
                    "snapshot_id", flat=True
                )
                if value is not None
            )
        protected_daily_ids = {
            str(row_id)
            for part_ids in WeatherFetchPart.objects.filter(
                fetch__status__in=[WeatherFetch.Status.PENDING, WeatherFetch.Status.RUNNING],
            ).values_list("daily_ids", flat=True)
            for row_id in (part_ids or [])
        }
        if apps.is_installed("estimate_updates"):
            from estimate_updates.models import EstimateTask

            keep.update(
                str(value)
                for value in EstimateTask.objects.values_list("snapshot_id", flat=True)
            )
        terminal_fetches = WeatherFetch.objects.filter(
            status__in=[WeatherFetch.Status.COMPLETED, WeatherFetch.Status.FAILED]
        )
        terminal_fetches.filter(snapshot_id__isnull=False).exclude(
            snapshot_id__in=keep
        ).update(snapshot=None, updated_at=timezone.now())
        terminal_fetches.filter(previous_snapshot_id__isnull=False).exclude(
            previous_snapshot_id__in=keep
        ).update(previous_snapshot=None, updated_at=timezone.now())
        deletable = [
            snapshot_id
            for snapshot_id, _cell_key, assessment_date in snapshots
            if str(snapshot_id) not in keep
            and assessment_date < cleanup_date
        ]
        # The delete loads each row to follow cascades, so it reads only the id.
        snapshots_to_delete = WeatherSnapshot.objects.filter(id__in=deletable).only("id")
        deleted_snapshots = snapshots_to_delete.count()
        snapshots_to_delete.delete()
        cutoff = cleanup_date - timedelta(days=HISTORY_DAYS)
        from weather_summaries.models import WeatherDaily

        deleted_daily_count = (
            WeatherDaily.objects.filter(
                date__lt=cutoff,
                snapshot_days__isnull=True,
            )
            .exclude(id__in=protected_daily_ids)
            .exclude(source_endpoint=history_endpoint())
            .delete()[0]
        )
        from weather_requests.models import WeatherRequestAttempt

        attempt_cutoff = datetime.combine(
            cleanup_date - timedelta(days=31),
            datetime.min.time(),
            tzinfo=UTC,
        )
        deleted_attempt_count = WeatherRequestAttempt.objects.filter(
            reserved_at__lt=attempt_cutoff,
        ).delete()[0]
        WeatherVisitorReservation.objects.filter(
            reserved_at__lt=timezone.now() - VISITOR_WINDOW,
        ).delete()
    return {
        "cleanup_date": cleanup_date.isoformat(),
        "deleted_snapshot_count": deleted_snapshots,
        "deleted_daily_count": deleted_daily_count,
        "deleted_attempt_count": deleted_attempt_count,
    }


def enqueue_cleanup(
    *,
    cleanup_date: date | None = None,
    protected_snapshot_ids: Iterable[str] = (),
) -> WeatherCleanupJob:
    """Create one date-keyed cleanup task and queue it after commit."""
    target = cleanup_date or italian_assessment_date()
    logical_key = f"weather-cleanup:{target.isoformat()}"
    requested_protected = [str(value) for value in protected_snapshot_ids]
    with transaction.atomic():
        job, _created = WeatherCleanupJob.objects.get_or_create(
            cleanup_date=target,
            defaults={
                "logical_key": logical_key,
                "protected_snapshot_ids": requested_protected,
            },
        )
        if requested_protected and job.status != WeatherCleanupJob.Status.COMPLETED:
            job.protected_snapshot_ids = list(
                dict.fromkeys([*job.protected_snapshot_ids, *requested_protected])
            )
            job.save(update_fields=["protected_snapshot_ids", "updated_at"])
        if job.status != WeatherCleanupJob.Status.COMPLETED and job.task_id is None:
            transaction.on_commit(
                lambda job_id=job.id, retry_at=job.retry_at: _enqueue_cleanup(
                    job_id=job_id,
                    run_after=retry_at,
                )
            )
    job.refresh_from_db()
    return job


def _enqueue_cleanup(
    *,
    job_id: uuid.UUID,
    run_after: datetime | None = None,
) -> uuid.UUID | None:
    from .tasks import weather_cleanup_task

    try:
        task = weather_cleanup_task.using(run_after=run_after) if run_after else weather_cleanup_task
        with transaction.atomic():
            job = WeatherCleanupJob.objects.select_for_update().get(id=job_id)
            if _queue_task_is_active(task_id=job.task_id):
                return job.task_id
            task_result = task.enqueue(job_id=str(job_id))
            task_id = uuid.UUID(str(task_result.id))
            job.task_id = task_id
            job.save(update_fields=["task_id", "updated_at"])
            return task_id
    except Exception as exc:
        WeatherCleanupJob.objects.filter(id=job_id).update(
            task_id=None,
            full_reason=str(exc),
            updated_at=timezone.now(),
        )
        return None


def _schedule_cleanup(*, job: WeatherCleanupJob, run_after: datetime | None = None) -> None:
    """Queue cleanup after its row is committed, once it is due."""
    if run_after is None and job.retry_at is not None and job.retry_at > timezone.now():
        run_after = job.retry_at
    if _queue_task_is_active(task_id=job.task_id):
        return
    transaction.on_commit(
        lambda: _enqueue_cleanup(job_id=job.id, run_after=run_after)
    )


def run_cleanup_job(*, job_id: uuid.UUID | str) -> dict[str, int | str]:
    """Run one queued cleanup job without network access."""
    with transaction.atomic():
        job = WeatherCleanupJob.objects.select_for_update().get(id=job_id)
        if job.status == WeatherCleanupJob.Status.COMPLETED:
            return {
                "cleanup_date": job.cleanup_date.isoformat(),
                "deleted_snapshot_count": job.deleted_snapshot_count,
                "deleted_daily_count": job.deleted_daily_count,
            }
        job.status = WeatherCleanupJob.Status.RUNNING
        job.save(update_fields=["status", "updated_at"])
    try:
        result = cleanup_weather(
            cleanup_date=job.cleanup_date,
            protected_snapshot_ids=job.protected_snapshot_ids,
        )
    except Exception as exc:
        WeatherCleanupJob.objects.filter(id=job.id).update(
            status=WeatherCleanupJob.Status.PENDING,
            task_id=None,
            retry_at=timezone.now() + RETRY_DELAY,
            full_reason=str(exc),
            updated_at=timezone.now(),
        )
        job.refresh_from_db()
        _schedule_cleanup(job=job, run_after=job.retry_at)
        raise
    WeatherCleanupJob.objects.filter(id=job.id).update(
        status=WeatherCleanupJob.Status.COMPLETED,
        deleted_snapshot_count=int(result["deleted_snapshot_count"]),
        deleted_daily_count=int(result["deleted_daily_count"]),
        retry_at=None,
        completed_at=timezone.now(),
        full_reason="",
        updated_at=timezone.now(),
    )
    return result


def weather_status(*, assessment_date: date | None = None) -> dict[str, Any]:
    """Return durable fetch, quota and cleanup status without network access."""
    target = assessment_date or italian_assessment_date()
    fetches = WeatherFetch.objects.all()
    per_day: dict[str, dict[str, int]] = {}
    for row in fetches.values("assessment_date", "status").annotate(count=Count("id")):
        key = row["assessment_date"].isoformat()
        per_day.setdefault(key, {})[row["status"]] = row["count"]
    from weather_requests.models import WeatherRequestAttempt

    attempts = WeatherRequestAttempt.objects.all()
    counted_calls = attempts.aggregate(total=Sum("reservation_weight"))["total"]
    refusals = list(
        attempts.filter(outcome=WeatherRequestAttempt.Outcome.REFUSED)
        .order_by("-reserved_at")
        .values("id", "request_id", "http_status", "full_message", "reserved_at")[:100]
    )
    for item in refusals:
        item["id"] = str(item["id"])
        item["request_id"] = str(item["request_id"])
        item["reserved_at"] = item["reserved_at"].isoformat()
    last_cleanup = WeatherCleanupJob.objects.order_by("-cleanup_date").first()
    return {
        "assessment_date": target.isoformat(),
        "fetches_per_day": per_day,
        "counted_calls": str(counted_calls or "0"),
        "attempt_count": attempts.count(),
        "refused_requests": refusals,
        "pending_cells": list(
            fetches.filter(status=WeatherFetch.Status.PENDING).values_list("cell_key", flat=True)
        ),
        "last_cleanup": (
            {
                "cleanup_date": last_cleanup.cleanup_date.isoformat(),
                "status": last_cleanup.status,
            }
            if last_cleanup
            else None
        ),
    }


__all__ = [
    "CellWeatherResult",
    "DEFAULT_WAIT_SECONDS",
    "SCHEDULED_START_GAP",
    "ScheduledPartPlan",
    "ScheduledWeatherPlan",
    "WeatherLookupResult",
    "WeatherOnDemandError",
    "cleanup_weather",
    "configure_cleanup_callback",
    "configure_estimate_callback",
    "configure_forecast_callback",
    "enqueue_cleanup",
    "fetch_weather_on_demand",
    "italian_assessment_date",
    "opaque_visitor_hash",
    "notify_snapshot_ready",
    "process_weather_fetch_batch",
    "recover_weather_fetches",
    "request_weather",
    "plan_nightly_weather",
    "plan_scheduled_weather",
    "reopen_weather_fetch",
    "run_cleanup_job",
    "run_scheduled_weather_step",
    "start_scheduled_weather_run",
    "weather_window",
    "set_estimate_callback",
    "set_cleanup_callback",
    "weather_status",
]
