import logging
import os
import random
import time
import uuid
from contextlib import nullcontext

from django.db import close_old_connections
from django.db.utils import OperationalError
from django_tasks_db.management.commands.db_worker import Worker as DatabaseWorker
from django_tasks_db.models import DBTaskResult
from django_tasks_db.utils import exclusive_transaction, is_locked_database_exception

from .models import BackgroundJobAttempt
from .services import logical_job_locks, recover_abandoned_tasks


logger = logging.getLogger("django_tasks_db")

# During a deploy the old worker can claim a task and then stop, after the new
# worker has already run its startup recovery.
ABANDONED_TASK_CHECK_SECONDS = 60.0


REPRESENTATIVE_TASK_PATH = "background_jobs.tasks.representative_job_task"
WEATHER_FETCH_TASK_PATH = "weather_on_demand.tasks.weather_fetch_task"
WEATHER_CLEANUP_TASK_PATH = "weather_on_demand.tasks.weather_cleanup_task"
SCHEDULED_WEATHER_TASK_PATH = "weather_on_demand.tasks.scheduled_weather_run_task"
FORECAST_TASK_PATH = "estimate_updates.tasks.forecast_task"


class SporaMeteoWorker(DatabaseWorker):
    """Claim SporaMeteo tasks while holding each logical job lock."""

    def _logical_lock_keys_for_task(self, task_result: DBTaskResult) -> tuple[str, ...]:
        kwargs = task_result.args_kwargs.get("kwargs", {})
        if task_result.task_path == REPRESENTATIVE_TASK_PATH:
            attempt_id = kwargs.get("attempt_id")
            if attempt_id is None:
                return ()
            try:
                parsed_attempt_id = uuid.UUID(str(attempt_id))
            except ValueError:
                return ()
            attempt = (
                BackgroundJobAttempt.objects.select_related("job")
                .filter(id=parsed_attempt_id)
                .first()
            )
            return (attempt.job.logical_key,) if attempt is not None else ()
        if task_result.task_path == WEATHER_FETCH_TASK_PATH:
            batch_id = kwargs.get("batch_id")
            if batch_id is None:
                return ()
            try:
                parsed_batch_id = uuid.UUID(str(batch_id))
            except ValueError:
                return ()
            from weather_on_demand.services import weather_task_lock_keys

            return weather_task_lock_keys(batch_id=parsed_batch_id)
        if task_result.task_path == WEATHER_CLEANUP_TASK_PATH:
            job_id = kwargs.get("job_id")
            if job_id is None:
                return ()
            try:
                parsed_job_id = uuid.UUID(str(job_id))
            except ValueError:
                return ()
            from weather_on_demand.services import weather_cleanup_task_lock_keys

            return weather_cleanup_task_lock_keys(job_id=parsed_job_id)
        if task_result.task_path == SCHEDULED_WEATHER_TASK_PATH:
            run_id = kwargs.get("run_id")
            if run_id is None:
                return ()
            try:
                parsed_run_id = uuid.UUID(str(run_id))
            except ValueError:
                return ()
            from weather_on_demand.services import scheduled_run_task_lock_keys

            return scheduled_run_task_lock_keys(run_id=parsed_run_id)
        if task_result.task_path == FORECAST_TASK_PATH:
            region_code = kwargs.get("region_code")
            assessment_date = kwargs.get("assessment_date")
            if region_code is None or assessment_date is None:
                return ()
            return (f"forecast:{region_code}:{assessment_date}",)
        return ()

    def _logical_key_for_task(self, task_result: DBTaskResult) -> str | None:
        """Return the first lock key for compatibility with worker callers."""
        keys = self._logical_lock_keys_for_task(task_result)
        return keys[0] if keys else None

    def recover_abandoned_tasks(self) -> int:
        """Fail and queue again the tasks of workers that have stopped.

        A failure here is logged and must not stop this worker.
        """
        try:
            recovered = recover_abandoned_tasks(
                lock_keys_for_task=self._logical_lock_keys_for_task,
            )
        except Exception:
            logger.exception("Checking for abandoned tasks failed worker_id=%s", self.worker_id)
            return 0
        if recovered:
            logger.warning(
                "Recovered %d abandoned tasks worker_id=%s",
                recovered,
                self.worker_id,
            )
        return recovered

    def _claim_task(
        self,
        *,
        tasks,
        pause_after_claim: float,
    ) -> tuple[DBTaskResult | None, bool]:
        candidates = list(tasks[:50])
        if not candidates:
            return None, True

        for candidate in candidates:
            logical_keys = self._logical_lock_keys_for_task(candidate)
            lock_context = (
                logical_job_locks(logical_keys=logical_keys, wait=False)
                if logical_keys
                else nullcontext(True)
            )
            with lock_context as acquired:
                if not acquired:
                    continue
                try:
                    with exclusive_transaction(tasks.db):
                        task_result = tasks.filter(id=candidate.id).get_locked()
                        if task_result is not None:
                            task_result.claim(self.worker_id)
                except OperationalError as exc:
                    if is_locked_database_exception(exc):
                        continue
                    raise
                if task_result is not None:
                    if pause_after_claim:
                        time.sleep(pause_after_claim)
                    self.run_task(task_result)
                    return task_result, True

        return None, False

    def run(self) -> None:
        logger.info(
            "Starting worker worker_id=%s queues=%s",
            self.worker_id,
            ",".join(self.queue_names),
        )
        if self.startup_delay and self.interval:
            time.sleep(random.random())

        pause_after_claim = float(
            os.environ.get("SPORAMETEO_TEST_PAUSE_AFTER_TASK_CLAIM", "0")
        )
        next_abandoned_check = time.monotonic() + ABANDONED_TASK_CHECK_SECONDS
        while self.running:
            close_old_connections()
            if time.monotonic() >= next_abandoned_check:
                self.recover_abandoned_tasks()
                next_abandoned_check = time.monotonic() + ABANDONED_TASK_CHECK_SECONDS
            tasks = DBTaskResult.objects.ready().filter(backend_name=self.backend_name)
            if not self.process_all_queues:
                tasks = tasks.filter(queue_name__in=self.queue_names)
            if self.excluded_queue_names:
                tasks = tasks.exclude(queue_name__in=self.excluded_queue_names)

            task_result, retrieved_task_result = self._claim_task(
                tasks=tasks,
                pause_after_claim=pause_after_claim,
            )

            if self.batch and retrieved_task_result and task_result is None:
                logger.info(
                    "No more tasks to run for worker_id=%s - exiting gracefully.",
                    self.worker_id,
                )
                return None

            if self.max_tasks is not None and self._run_tasks >= self.max_tasks:
                logger.info(
                    "Run maximum tasks (%d) on worker=%s - exiting gracefully.",
                    self._run_tasks,
                    self.worker_id,
                )
                return None

            close_old_connections()
            if self.running and task_result is None:
                time.sleep(self.interval)
