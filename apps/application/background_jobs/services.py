import hashlib
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import date

from django.apps import apps
from django.db import IntegrityError, connection, transaction
from django_tasks_db.models import DBTaskResult
from django.utils import timezone

from .models import (
    BackgroundJob,
    BackgroundJobAttempt,
    RepresentativeInput,
    RepresentativeResult,
)


JOB_NAME = "representative"
INTERRUPTED_FAILURE_REASON = "Worker stopped before the job completed."


class BackgroundJobError(RuntimeError):
    """Base error for representative background job operations."""


class FailedBackgroundJobError(BackgroundJobError):
    """Report that a failed attempt needs an explicit retry."""


class ActiveBackgroundJobError(BackgroundJobError):
    """Report that another worker owns the logical job lock."""


class StaleBackgroundTaskError(BackgroundJobError):
    """Report that a queue task points to a terminal application attempt."""


class ControlledRepresentativeFailure(BackgroundJobError):
    """Provide the deliberate failure used by the representative checks."""


def advisory_lock_key(*, logical_key: str) -> int:
    """Return a stable signed PostgreSQL advisory-lock key."""

    digest = hashlib.blake2b(
        logical_key.encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@contextmanager
def logical_job_locks(*, logical_keys: Iterable[str], wait: bool) -> Iterator[bool]:
    """Hold sorted PostgreSQL session locks for one task's logical keys."""

    keys = tuple(sorted(set(logical_keys)))
    if any(not key or len(key) > 255 for key in keys):
        raise ValueError("Logical task lock keys must be non-empty and at most 255 characters.")
    lock_ids = [advisory_lock_key(logical_key=key) for key in keys]
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
            with connection.cursor() as cursor:
                for lock_id in reversed(acquired):
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
            yield False
            return
        yield True
    finally:
        if acquired and len(acquired) == len(lock_ids):
            with connection.cursor() as cursor:
                for lock_id in reversed(acquired):
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])


@contextmanager
def logical_job_lock(*, logical_key: str, wait: bool) -> Iterator[bool]:
    """Hold one PostgreSQL session lock for a logical job."""

    with logical_job_locks(logical_keys=[logical_key], wait=wait) as acquired:
        yield acquired


def create_representative_input(
    *,
    value: int = 1,
    delay_seconds: float = 0.0,
    fail_first_attempt: bool = False,
) -> RepresentativeInput:
    """Create a bounded input for the representative background job."""

    representative_input = RepresentativeInput(
        value=value,
        delay_seconds=delay_seconds,
        fail_first_attempt=fail_first_attempt,
    )
    representative_input.full_clean()
    representative_input.save()
    return representative_input


def default_logical_key(*, input_id: uuid.UUID, job_date: date) -> str:
    """Build the documented representative logical-job key."""

    return f"{JOB_NAME}:{input_id}:{job_date.isoformat()}"


def _queue_attempt(*, attempt_id: uuid.UUID) -> None:
    from .tasks import representative_job_task

    try:
        task_result = representative_job_task.enqueue(attempt_id=str(attempt_id))
        try:
            task_id = uuid.UUID(str(task_result.id))
        except ValueError:
            task_id = None
        BackgroundJobAttempt.objects.filter(id=attempt_id).update(
            task_id=task_id,
        )
    except Exception as exc:
        reason = _failure_reason(exc)
        BackgroundJobAttempt.objects.filter(id=attempt_id).update(
            state=BackgroundJobAttempt.State.FAILED,
            ended_at=timezone.now(),
            failure_reason=reason,
        )
        raise


def _get_or_create_job(
    *,
    logical_key: str,
    representative_input: RepresentativeInput,
) -> BackgroundJob:
    job = (
        BackgroundJob.objects.select_for_update()
        .filter(logical_key=logical_key)
        .first()
    )
    if job is None:
        try:
            with transaction.atomic():
                BackgroundJob.objects.create(
                    logical_key=logical_key,
                    job_name=JOB_NAME,
                    input_id=representative_input.id,
                )
        except IntegrityError:
            pass
        job = BackgroundJob.objects.select_for_update().get(logical_key=logical_key)
    return job


def enqueue_representative_job(
    *,
    input_id: uuid.UUID,
    logical_key: str,
) -> BackgroundJobAttempt:
    """Create or return one queued representative attempt."""

    with transaction.atomic():
        representative_input = RepresentativeInput.objects.get(id=input_id)
        job = _get_or_create_job(
            logical_key=logical_key,
            representative_input=representative_input,
        )
        if job.input_id != representative_input.id:
            raise BackgroundJobError(
                "The logical key already belongs to another input."
            )

        latest_attempt = job.attempts.order_by("-attempt_number").first()
        if latest_attempt is not None:
            if latest_attempt.state in {
                BackgroundJobAttempt.State.WAITING,
                BackgroundJobAttempt.State.RUNNING,
                BackgroundJobAttempt.State.COMPLETED,
            }:
                return latest_attempt
            raise FailedBackgroundJobError(
                f"Attempt {latest_attempt.id} failed and requires an explicit retry."
            )

        attempt = BackgroundJobAttempt.objects.create(
            job=job,
            attempt_number=1,
        )
        _queue_attempt(attempt_id=attempt.id)

    attempt.refresh_from_db()
    return attempt


def retry_background_job(*, attempt_id: uuid.UUID) -> BackgroundJobAttempt:
    """Queue one explicit retry for a failed representative attempt."""

    with transaction.atomic():
        failed_attempt = (
            BackgroundJobAttempt.objects.select_for_update()
            .select_related("job")
            .get(id=attempt_id)
        )
        if failed_attempt.state != BackgroundJobAttempt.State.FAILED:
            raise BackgroundJobError("Only a failed attempt can be retried.")

        job = BackgroundJob.objects.select_for_update().get(id=failed_attempt.job_id)
        latest_attempt = job.attempts.order_by("-attempt_number").first()
        if latest_attempt is None or latest_attempt.id != failed_attempt.id:
            raise BackgroundJobError("The requested attempt is not the latest attempt.")

        attempt = BackgroundJobAttempt.objects.create(
            job=job,
            attempt_number=failed_attempt.attempt_number + 1,
            retry_of=failed_attempt,
        )
        _queue_attempt(attempt_id=attempt.id)

    attempt.refresh_from_db()
    return attempt


def _failure_reason(exc: BaseException) -> str:
    message = str(exc).strip()
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _mark_attempt_failed(*, attempt_id: uuid.UUID, reason: str) -> None:
    with transaction.atomic():
        attempt = BackgroundJobAttempt.objects.select_for_update().get(id=attempt_id)
        if attempt.state == BackgroundJobAttempt.State.RUNNING:
            attempt.state = BackgroundJobAttempt.State.FAILED
            attempt.ended_at = timezone.now()
            attempt.failure_reason = reason
            attempt.save(update_fields=["state", "ended_at", "failure_reason"])


def _mark_attempt_completed(*, attempt_id: uuid.UUID, result_value: int) -> int:
    with transaction.atomic():
        attempt = (
            BackgroundJobAttempt.objects.select_for_update()
            .select_related("job")
            .get(id=attempt_id)
        )
        if attempt.state == BackgroundJobAttempt.State.COMPLETED:
            return RepresentativeResult.objects.get(job_id=attempt.job_id).result_value
        if attempt.state != BackgroundJobAttempt.State.RUNNING:
            raise StaleBackgroundTaskError(
                f"Attempt {attempt.id} is {attempt.state} and cannot complete."
            )

        result, _ = RepresentativeResult.objects.get_or_create(
            job_id=attempt.job_id,
            defaults={"result_value": result_value},
        )
        if result.result_value != result_value:
            raise BackgroundJobError("The logical job already has a different result.")
        attempt.state = BackgroundJobAttempt.State.COMPLETED
        attempt.ended_at = timezone.now()
        attempt.save(update_fields=["state", "ended_at"])
        return result.result_value


def _representative_result(*, attempt: BackgroundJobAttempt) -> RepresentativeResult | None:
    return RepresentativeResult.objects.filter(job_id=attempt.job_id).first()


def _backend_failure_reason(*, task_result: DBTaskResult) -> str:
    traceback = (task_result.traceback or "").strip()
    if traceback:
        return traceback[-4000:]
    if task_result.exception_class_path:
        return task_result.exception_class_path
    return "Database task failed."


def _set_backend_failed(*, task_result: DBTaskResult, reason: str) -> None:
    if task_result.status == "FAILED":
        return
    task_result.set_failed(BackgroundJobError(reason))


def _reconcile_terminal_attempt(
    *,
    attempt: BackgroundJobAttempt,
    task_result: DBTaskResult,
) -> None:
    result = _representative_result(attempt=attempt)
    if attempt.state == BackgroundJobAttempt.State.COMPLETED:
        if result is None:
            attempt.state = BackgroundJobAttempt.State.FAILED
            attempt.ended_at = timezone.now()
            attempt.failure_reason = "Completed attempt has no persisted result."
            attempt.save(update_fields=["state", "ended_at", "failure_reason"])
            _set_backend_failed(task_result=task_result, reason=attempt.failure_reason)
        elif task_result.status in {"READY", "RUNNING"}:
            task_result.set_successful(result.result_value)
        return

    if attempt.state == BackgroundJobAttempt.State.FAILED:
        if task_result.status not in {"FAILED", "SUCCESSFUL"}:
            _set_backend_failed(
                task_result=task_result,
                reason=attempt.failure_reason or "The application attempt failed.",
            )


def recover_interrupted_jobs() -> int:
    """Reconcile or fail SporaMeteo attempts left by a stopped worker."""

    recovered = recover_interrupted_attempts(
        states=[
            BackgroundJobAttempt.State.WAITING,
            BackgroundJobAttempt.State.RUNNING,
            BackgroundJobAttempt.State.COMPLETED,
            BackgroundJobAttempt.State.FAILED,
        ]
    )
    if apps.is_installed("weather_on_demand"):
        from weather_on_demand.services import recover_weather_fetches

        recovered += recover_weather_fetches()
    if apps.is_installed("estimate_updates"):
        from estimate_updates.services import recover_estimate_tasks

        recovered += recover_estimate_tasks()
    return recovered


def recover_abandoned_tasks(
    *,
    lock_keys_for_task: Callable[[DBTaskResult], tuple[str, ...]],
) -> int:
    """Fail tasks whose worker has stopped and queue their work again.

    Safe while other workers run, because it only acts when the task's locks are free.
    """

    recovered = recover_interrupted_attempts(
        states=[
            BackgroundJobAttempt.State.WAITING,
            BackgroundJobAttempt.State.RUNNING,
        ]
    )
    if apps.is_installed("weather_on_demand"):
        from weather_on_demand.services import recover_abandoned_weather_tasks

        recovered += recover_abandoned_weather_tasks()
    if apps.is_installed("estimate_updates"):
        from estimate_updates.services import recover_abandoned_estimate_tasks

        recovered += recover_abandoned_estimate_tasks()
    # Other task types only get their queue row failed here, their own rows
    # recover when the worker next starts.
    recovered += fail_abandoned_queue_tasks(lock_keys_for_task=lock_keys_for_task)
    return recovered


def fail_abandoned_queue_tasks(
    *,
    lock_keys_for_task: Callable[[DBTaskResult], tuple[str, ...]],
) -> int:
    """Fail running queue rows whose logical locks no worker holds.

    A worker holds these locks for the whole task, and PostgreSQL drops them when it stops.
    """

    failed = 0
    candidates = DBTaskResult.objects.filter(status="RUNNING").order_by("started_at")
    for candidate in candidates:
        logical_keys = lock_keys_for_task(candidate)
        if not logical_keys:
            continue
        with logical_job_locks(logical_keys=logical_keys, wait=False) as acquired:
            if not acquired:
                continue
            with transaction.atomic():
                task_result = (
                    DBTaskResult.objects.select_for_update()
                    .filter(id=candidate.id, status="RUNNING")
                    .first()
                )
                if task_result is None:
                    continue
                task_result.set_failed(BackgroundJobError(INTERRUPTED_FAILURE_REASON))
                failed += 1
    return failed


def recover_interrupted_attempts(*, states: Iterable[str]) -> int:
    """Reconcile or fail representative attempts in the given states.

    It skips every attempt whose logical lock a live worker holds.
    """

    recovered = 0
    attempts = (
        BackgroundJobAttempt.objects.filter(state__in=list(states))
        .select_related("job")
        .order_by("queued_at")
    )
    for candidate in attempts:
        with logical_job_lock(logical_key=candidate.job.logical_key, wait=False) as acquired:
            if not acquired:
                continue

            with transaction.atomic():
                attempt = (
                    BackgroundJobAttempt.objects.select_for_update()
                    .select_related("job")
                    .get(id=candidate.id)
                )
                task_result = (
                    DBTaskResult.objects.select_for_update()
                    .filter(id=attempt.task_id)
                    .first()
                )
                if task_result is None:
                    if attempt.state in {
                        BackgroundJobAttempt.State.COMPLETED,
                        BackgroundJobAttempt.State.FAILED,
                    }:
                        continue
                    reason = "Worker stopped before the queue task was created."
                    attempt.state = BackgroundJobAttempt.State.FAILED
                    attempt.ended_at = timezone.now()
                    attempt.failure_reason = reason
                    attempt.save(update_fields=["state", "ended_at", "failure_reason"])
                    recovered += 1
                    continue

                if attempt.state in {
                    BackgroundJobAttempt.State.COMPLETED,
                    BackgroundJobAttempt.State.FAILED,
                }:
                    if task_result.status in {"READY", "RUNNING"}:
                        _reconcile_terminal_attempt(
                            attempt=attempt,
                            task_result=task_result,
                        )
                        recovered += 1
                    continue

                if task_result.status == "READY":
                    if attempt.state == BackgroundJobAttempt.State.RUNNING:
                        reason = INTERRUPTED_FAILURE_REASON
                        attempt.state = BackgroundJobAttempt.State.FAILED
                        attempt.ended_at = timezone.now()
                        attempt.failure_reason = reason
                        attempt.save(
                            update_fields=["state", "ended_at", "failure_reason"]
                        )
                        _set_backend_failed(task_result=task_result, reason=reason)
                        recovered += 1
                    continue

                if task_result.status == "RUNNING":
                    reason = INTERRUPTED_FAILURE_REASON
                    attempt.state = BackgroundJobAttempt.State.FAILED
                    attempt.ended_at = timezone.now()
                    attempt.failure_reason = reason
                    attempt.save(update_fields=["state", "ended_at", "failure_reason"])
                    _set_backend_failed(task_result=task_result, reason=reason)
                    recovered += 1
                    continue

                if task_result.status == "SUCCESSFUL":
                    result = _representative_result(attempt=attempt)
                    if result is None:
                        reason = "Queue task succeeded without a persisted result."
                        attempt.state = BackgroundJobAttempt.State.FAILED
                        attempt.ended_at = timezone.now()
                        attempt.failure_reason = reason
                        attempt.save(
                            update_fields=["state", "ended_at", "failure_reason"]
                        )
                        _set_backend_failed(task_result=task_result, reason=reason)
                    else:
                        attempt.state = BackgroundJobAttempt.State.COMPLETED
                        attempt.ended_at = attempt.ended_at or timezone.now()
                        attempt.save(update_fields=["state", "ended_at"])
                    recovered += 1
                    continue

                if task_result.status == "FAILED":
                    reason = _backend_failure_reason(task_result=task_result)
                    attempt.state = BackgroundJobAttempt.State.FAILED
                    attempt.ended_at = attempt.ended_at or timezone.now()
                    attempt.failure_reason = attempt.failure_reason or reason
                    attempt.save(update_fields=["state", "ended_at", "failure_reason"])
                    recovered += 1
    return recovered
