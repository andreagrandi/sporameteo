import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.http import JsonResponse
from django.test import Client, override_settings
from django.urls import path
from django_tasks_db.models import DBTaskResult

from background_jobs.models import (
    BackgroundJobAttempt,
    RepresentativeInput,
    RepresentativeResult,
)
from background_jobs.services import (
    create_representative_input,
    enqueue_representative_job,
    logical_job_lock,
    recover_interrupted_jobs,
    retry_background_job,
)
from background_jobs import worker as worker_module
from background_jobs.tasks import representative_job_task
from background_jobs.worker import SporaMeteoWorker
from estimate_updates.tasks import forecast_task
from weather_on_demand.models import WeatherCleanupJob
from weather_on_demand.services import enqueue_cleanup


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANAGE_PATH = PROJECT_ROOT / "apps" / "application" / "manage.py"


def _worker_environment() -> dict[str, str]:
    database = settings.DATABASES["default"]
    environment = os.environ.copy()
    environment.update(
        {
            "SPORAMETEO_DB_NAME": str(database["NAME"]),
            "SPORAMETEO_DB_USER": str(database["USER"]),
            "SPORAMETEO_DB_PASSWORD": str(database["PASSWORD"]),
            "SPORAMETEO_DB_HOST": str(database["HOST"]),
            "SPORAMETEO_DB_PORT": str(database["PORT"]),
            "PYTHONPATH": os.pathsep.join(
                [
                    str(PROJECT_ROOT / "apps" / "application"),
                    str(PROJECT_ROOT / "src"),
                    str(PROJECT_ROOT / "apps" / "api"),
                    environment.get("PYTHONPATH", ""),
                ]
            ),
        }
    )
    return environment


def _run_worker(*, worker_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(MANAGE_PATH),
            "run_background_worker",
            "--batch",
            "--interval",
            "0",
            "--no-startup-delay",
            "--worker-id",
            worker_id,
        ],
        cwd=PROJECT_ROOT,
        env=_worker_environment(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _worker_command(*, worker_id: str) -> list[str]:
    return [
        sys.executable,
        str(MANAGE_PATH),
        "run_background_worker",
        "--batch",
        "--interval",
        "0",
        "--no-startup-delay",
        "--worker-id",
        worker_id,
    ]


def _wait_for_state(*, attempt_id: uuid.UUID, state: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        attempt = BackgroundJobAttempt.objects.get(id=attempt_id)
        if attempt.state == state:
            return
        time.sleep(0.05)
    raise AssertionError(f"Attempt {attempt_id} did not reach {state}.")


def _wait_for_claim_before_application_start(
    *,
    attempt_id: uuid.UUID,
    task_id: uuid.UUID,
    timeout: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        attempt = BackgroundJobAttempt.objects.get(id=attempt_id)
        task_result = DBTaskResult.objects.get(id=task_id)
        if task_result.status == "RUNNING" and attempt.state == "waiting":
            return
        time.sleep(0.05)
    raise AssertionError("Worker did not pause after claiming the queue task.")


@pytest.fixture
def representative_input() -> RepresentativeInput:
    return create_representative_input(value=7)


@pytest.mark.django_db(transaction=True)
def test_enqueue_returns_before_worker_completes_and_worker_persists_result(
    representative_input: RepresentativeInput,
) -> None:
    logical_key = f"test:enqueue:{representative_input.id}"

    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=logical_key,
    )

    assert attempt.state == BackgroundJobAttempt.State.WAITING
    assert attempt.task_id is not None
    assert not RepresentativeResult.objects.filter(job_id=attempt.job_id).exists()
    assert DBTaskResult.objects.get(id=attempt.task_id).status == "READY"

    result = _run_worker(worker_id="test-enqueue-worker")

    assert result.returncode == 0, result.stderr or result.stdout
    attempt.refresh_from_db()
    assert attempt.state == BackgroundJobAttempt.State.COMPLETED
    assert attempt.started_at is not None
    assert attempt.ended_at is not None
    assert RepresentativeResult.objects.get(job_id=attempt.job_id).result_value == 14


@pytest.mark.django_db(transaction=True)
def test_failed_attempt_requires_explicit_retry_and_preserves_failure(
    representative_input: RepresentativeInput,
) -> None:
    representative_input.fail_first_attempt = True
    representative_input.save(update_fields=["fail_first_attempt"])
    logical_key = f"test:retry:{representative_input.id}"

    failed_attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=logical_key,
    )
    failed_worker = _run_worker(worker_id="test-retry-failure-worker")

    assert failed_worker.returncode == 0, failed_worker.stderr or failed_worker.stdout
    failed_attempt.refresh_from_db()
    assert failed_attempt.state == BackgroundJobAttempt.State.FAILED
    assert "Forced failure" in failed_attempt.failure_reason
    original_started_at = failed_attempt.started_at
    original_ended_at = failed_attempt.ended_at

    retry_attempt = retry_background_job(attempt_id=failed_attempt.id)
    retry_worker = _run_worker(worker_id="test-retry-success-worker")

    assert retry_worker.returncode == 0, retry_worker.stderr or retry_worker.stdout
    retry_attempt.refresh_from_db()
    failed_attempt.refresh_from_db()
    assert retry_attempt.state == BackgroundJobAttempt.State.COMPLETED
    assert retry_attempt.retry_of_id == failed_attempt.id
    assert failed_attempt.state == BackgroundJobAttempt.State.FAILED
    assert failed_attempt.failure_reason
    assert failed_attempt.started_at == original_started_at
    assert failed_attempt.ended_at == original_ended_at


@pytest.mark.django_db(transaction=True)
def test_batch_worker_continues_after_failed_attempt(
    representative_input: RepresentativeInput,
) -> None:
    representative_input.fail_first_attempt = True
    representative_input.save(update_fields=["fail_first_attempt"])
    failed_attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=f"test:batch-failure:{representative_input.id}",
    )
    next_input = create_representative_input(value=13)
    next_attempt = enqueue_representative_job(
        input_id=next_input.id,
        logical_key=f"test:batch-after-failure:{next_input.id}",
    )

    worker = _run_worker(worker_id="test-batch-failure-worker")

    assert worker.returncode == 0, worker.stderr or worker.stdout
    failed_attempt.refresh_from_db()
    next_attempt.refresh_from_db()
    assert failed_attempt.state == BackgroundJobAttempt.State.FAILED
    assert "Forced failure" in failed_attempt.failure_reason
    assert next_attempt.state == BackgroundJobAttempt.State.COMPLETED


@pytest.mark.django_db(transaction=True)
def test_two_workers_cannot_execute_duplicate_tasks_for_one_logical_job(
    representative_input: RepresentativeInput,
) -> None:
    representative_input.delay_seconds = 0.5
    representative_input.save(update_fields=["delay_seconds"])
    logical_key = f"test:duplicate:{representative_input.id}"
    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=logical_key,
    )
    duplicate_task = representative_job_task.enqueue(attempt_id=str(attempt.id))

    first = subprocess.Popen(
        _worker_command(worker_id="test-duplicate-worker-one"),
        cwd=PROJECT_ROOT,
        env=_worker_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        _worker_command(worker_id="test-duplicate-worker-two"),
        cwd=PROJECT_ROOT,
        env=_worker_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        first_output = first.communicate(timeout=20)
        second_output = second.communicate(timeout=20)
    finally:
        for worker in (first, second):
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=10)

    assert first.returncode == 0, first_output[1] or first_output[0]
    assert second.returncode == 0, second_output[1] or second_output[0]
    attempt.refresh_from_db()
    assert attempt.state == BackgroundJobAttempt.State.COMPLETED
    assert BackgroundJobAttempt.objects.filter(job_id=attempt.job_id).count() == 1
    assert RepresentativeResult.objects.filter(job_id=attempt.job_id).count() == 1
    assert DBTaskResult.objects.get(id=attempt.task_id).status == "SUCCESSFUL"
    assert DBTaskResult.objects.get(id=duplicate_task.id).status == "SUCCESSFUL"


@pytest.mark.django_db(transaction=True)
def test_enqueue_of_running_logical_job_returns_without_waiting_for_work(
    representative_input: RepresentativeInput,
) -> None:
    representative_input.delay_seconds = 1.0
    representative_input.save(update_fields=["delay_seconds"])
    logical_key = f"test:running-enqueue:{representative_input.id}"
    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=logical_key,
    )
    child = subprocess.Popen(
        _worker_command(worker_id="test-running-enqueue-worker"),
        cwd=PROJECT_ROOT,
        env=_worker_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_state(attempt_id=attempt.id, state="running")
        started = time.monotonic()
        returned_attempt = enqueue_representative_job(
            input_id=representative_input.id,
            logical_key=logical_key,
        )
        elapsed = time.monotonic() - started
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=20)

    assert returned_attempt.id == attempt.id
    assert elapsed < 0.5


@pytest.mark.django_db(transaction=True)
def test_recovery_does_not_fail_task_claimed_by_live_worker_before_task_entry(
    representative_input: RepresentativeInput,
) -> None:
    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=f"test:claim-gap:{representative_input.id}",
    )
    assert attempt.task_id is not None
    environment = _worker_environment()
    environment["SPORAMETEO_TEST_PAUSE_AFTER_TASK_CLAIM"] = "2"
    child = subprocess.Popen(
        _worker_command(worker_id="test-claim-gap-worker"),
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_claim_before_application_start(
            attempt_id=attempt.id,
            task_id=attempt.task_id,
        )
        assert recover_interrupted_jobs() == 0
        attempt.refresh_from_db()
        assert attempt.state == BackgroundJobAttempt.State.WAITING
    finally:
        try:
            child_output = child.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child_output = child.communicate(timeout=10)

    assert child.returncode == 0, child_output[1] or child_output[0]
    attempt.refresh_from_db()
    assert attempt.state == BackgroundJobAttempt.State.COMPLETED


@pytest.mark.django_db(transaction=True)
def test_recovery_fails_hard_kill_after_queue_claim_before_task_entry(
    representative_input: RepresentativeInput,
) -> None:
    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=f"test:claim-kill:{representative_input.id}",
    )
    assert attempt.task_id is not None
    environment = _worker_environment()
    environment["SPORAMETEO_TEST_PAUSE_AFTER_TASK_CLAIM"] = "5"
    child = subprocess.Popen(
        _worker_command(worker_id="test-claim-kill-worker"),
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_claim_before_application_start(
            attempt_id=attempt.id,
            task_id=attempt.task_id,
        )
        child.kill()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    assert recover_interrupted_jobs() == 1
    attempt.refresh_from_db()
    assert attempt.state == BackgroundJobAttempt.State.FAILED
    assert attempt.failure_reason == "Worker stopped before the job completed."
    assert DBTaskResult.objects.get(id=attempt.task_id).status == "FAILED"


@pytest.mark.django_db(transaction=True)
def test_recovery_marks_interrupted_attempt_failed_and_worker_processes_next_job(
    representative_input: RepresentativeInput,
) -> None:
    representative_input.delay_seconds = 2.0
    representative_input.save(update_fields=["delay_seconds"])
    interrupted = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=f"test:interrupt:{representative_input.id}",
    )
    child = subprocess.Popen(
        _worker_command(worker_id="test-interrupted-worker"),
        cwd=PROJECT_ROOT,
        env=_worker_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_state(attempt_id=interrupted.id, state="running")
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    recovered = recover_interrupted_jobs()

    assert recovered == 1
    interrupted.refresh_from_db()
    assert interrupted.state == BackgroundJobAttempt.State.FAILED
    assert interrupted.failure_reason == "Worker stopped before the job completed."
    assert not RepresentativeResult.objects.filter(job_id=interrupted.job_id).exists()
    assert DBTaskResult.objects.get(id=interrupted.task_id).status == "FAILED"

    next_input = create_representative_input(value=9)
    next_attempt = enqueue_representative_job(
        input_id=next_input.id,
        logical_key=f"test:after-interrupt:{next_input.id}",
    )
    result = _run_worker(worker_id="test-after-interrupt-worker")

    assert result.returncode == 0, result.stderr or result.stdout
    next_attempt.refresh_from_db()
    assert next_attempt.state == BackgroundJobAttempt.State.COMPLETED


@pytest.mark.django_db(transaction=True)
def test_completed_job_is_not_rerun_by_a_new_worker(
    representative_input: RepresentativeInput,
) -> None:
    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=f"test:completed:{representative_input.id}",
    )
    first = _run_worker(worker_id="test-completed-worker-one")
    assert first.returncode == 0, first.stderr or first.stdout
    attempt.refresh_from_db()
    started_at = attempt.started_at

    second = _run_worker(worker_id="test-completed-worker-two")

    assert second.returncode == 0, second.stderr or second.stdout
    attempt.refresh_from_db()
    assert attempt.state == BackgroundJobAttempt.State.COMPLETED
    assert attempt.started_at == started_at
    assert DBTaskResult.objects.get(id=attempt.task_id).status == "SUCCESSFUL"


@pytest.mark.django_db(transaction=True)
def test_startup_recovery_does_not_steal_a_live_worker_job(
    representative_input: RepresentativeInput,
) -> None:
    representative_input.delay_seconds = 0.7
    representative_input.save(update_fields=["delay_seconds"])
    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=f"test:live-recovery:{representative_input.id}",
    )
    child = subprocess.Popen(
        _worker_command(worker_id="test-live-recovery-worker"),
        cwd=PROJECT_ROOT,
        env=_worker_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_state(attempt_id=attempt.id, state="running")
        assert recover_interrupted_jobs() == 0
        attempt.refresh_from_db()
        assert attempt.state == BackgroundJobAttempt.State.RUNNING
    finally:
        try:
            child_output = child.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child_output = child.communicate(timeout=10)
    assert child.returncode == 0, child_output[1] or child_output[0]
    attempt.refresh_from_db()
    assert attempt.state == BackgroundJobAttempt.State.COMPLETED


@pytest.mark.django_db(transaction=True)
def test_recovery_reconciles_completed_application_attempt_with_running_queue_row(
    representative_input: RepresentativeInput,
) -> None:
    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=f"test:reconcile:{representative_input.id}",
    )
    task_result = DBTaskResult.objects.get(id=attempt.task_id)
    task_result.claim(worker_id="test-reconcile-worker")
    representative_job_task.func(attempt_id=str(attempt.id))
    attempt.refresh_from_db()
    assert attempt.state == BackgroundJobAttempt.State.COMPLETED
    assert task_result.status == "RUNNING"

    assert recover_interrupted_jobs() == 1

    task_result.refresh_from_db()
    assert task_result.status == "SUCCESSFUL"


def _current_worker() -> SporaMeteoWorker:
    return SporaMeteoWorker(
        queue_names=["default"],
        interval=0,
        batch=True,
        backend_name="default",
        startup_delay=False,
        max_tasks=None,
        worker_id="test-current-worker",
        excluded_queue_names=[],
    )


@pytest.mark.django_db(transaction=True)
def test_running_worker_fails_attempt_left_by_a_stopped_worker(
    representative_input: RepresentativeInput,
) -> None:
    representative_input.delay_seconds = 2.0
    representative_input.save(update_fields=["delay_seconds"])
    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=f"test:abandoned:{representative_input.id}",
    )
    current_worker = _current_worker()
    child = subprocess.Popen(
        _worker_command(worker_id="test-stopped-worker"),
        cwd=PROJECT_ROOT,
        env=_worker_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_state(attempt_id=attempt.id, state="running")
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    recovered = current_worker.recover_abandoned_tasks()

    assert recovered == 1
    attempt.refresh_from_db()
    assert attempt.state == BackgroundJobAttempt.State.FAILED
    assert attempt.failure_reason == "Worker stopped before the job completed."
    assert DBTaskResult.objects.get(id=attempt.task_id).status == "FAILED"
    retry = retry_background_job(attempt_id=attempt.id)
    assert retry.state == BackgroundJobAttempt.State.WAITING
    assert DBTaskResult.objects.get(id=retry.task_id).status == "READY"


@pytest.mark.django_db(transaction=True)
def test_running_worker_leaves_alone_an_attempt_whose_worker_is_working(
    representative_input: RepresentativeInput,
) -> None:
    representative_input.delay_seconds = 0.7
    representative_input.save(update_fields=["delay_seconds"])
    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=f"test:live-abandoned:{representative_input.id}",
    )
    child = subprocess.Popen(
        _worker_command(worker_id="test-live-worker"),
        cwd=PROJECT_ROOT,
        env=_worker_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_state(attempt_id=attempt.id, state="running")
        assert _current_worker().recover_abandoned_tasks() == 0
        attempt.refresh_from_db()
        assert attempt.state == BackgroundJobAttempt.State.RUNNING
        assert DBTaskResult.objects.get(id=attempt.task_id).status == "RUNNING"
    finally:
        try:
            child_output = child.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child_output = child.communicate(timeout=10)
    assert child.returncode == 0, child_output[1] or child_output[0]
    attempt.refresh_from_db()
    assert attempt.state == BackgroundJobAttempt.State.COMPLETED


@pytest.mark.django_db(transaction=True)
def test_running_worker_queues_again_a_cleanup_left_by_a_stopped_worker() -> None:
    cleanup = enqueue_cleanup(cleanup_date=date(2026, 10, 22))
    stopped_task_id = cleanup.task_id
    assert stopped_task_id is not None
    DBTaskResult.objects.get(id=stopped_task_id).claim(worker_id="test-stopped-worker")
    WeatherCleanupJob.objects.filter(id=cleanup.id).update(
        status=WeatherCleanupJob.Status.RUNNING,
    )

    recovered = _current_worker().recover_abandoned_tasks()

    assert recovered == 1
    cleanup.refresh_from_db()
    assert cleanup.status == WeatherCleanupJob.Status.PENDING
    assert cleanup.task_id is not None
    assert cleanup.task_id != stopped_task_id
    assert DBTaskResult.objects.get(id=stopped_task_id).status == "FAILED"
    assert DBTaskResult.objects.get(id=cleanup.task_id).status == "READY"


@pytest.mark.django_db(transaction=True)
def test_running_worker_leaves_alone_a_cleanup_whose_worker_holds_the_lock() -> None:
    cleanup = enqueue_cleanup(cleanup_date=date(2026, 10, 22))
    live_task_id = cleanup.task_id
    assert live_task_id is not None
    DBTaskResult.objects.get(id=live_task_id).claim(worker_id="test-live-worker")
    WeatherCleanupJob.objects.filter(id=cleanup.id).update(
        status=WeatherCleanupJob.Status.RUNNING,
    )
    locked = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        try:
            with logical_job_lock(logical_key=cleanup.logical_key, wait=True):
                locked.set()
                release.wait(timeout=10)
        finally:
            connection.close()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert locked.wait(timeout=10)
        assert _current_worker().recover_abandoned_tasks() == 0
    finally:
        release.set()
        holder.join(timeout=10)

    cleanup.refresh_from_db()
    assert cleanup.status == WeatherCleanupJob.Status.RUNNING
    assert cleanup.task_id == live_task_id
    assert DBTaskResult.objects.get(id=live_task_id).status == "RUNNING"


@pytest.mark.django_db(transaction=True)
def test_running_worker_fails_other_queue_tasks_only_when_their_lock_is_free() -> None:
    stopped = forecast_task.enqueue(region_code="09", assessment_date="2026-10-21")
    live = forecast_task.enqueue(region_code="09", assessment_date="2026-10-22")
    DBTaskResult.objects.get(id=stopped.id).claim(worker_id="test-stopped-worker")
    DBTaskResult.objects.get(id=live.id).claim(worker_id="test-live-worker")
    locked = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        try:
            with logical_job_lock(logical_key="forecast:09:2026-10-22", wait=True):
                locked.set()
                release.wait(timeout=10)
        finally:
            connection.close()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    try:
        assert locked.wait(timeout=10)
        assert _current_worker().recover_abandoned_tasks() == 1
    finally:
        release.set()
        holder.join(timeout=10)

    assert DBTaskResult.objects.get(id=stopped.id).status == "FAILED"
    assert DBTaskResult.objects.get(id=live.id).status == "RUNNING"


@pytest.mark.django_db(transaction=True)
def test_worker_loop_checks_for_abandoned_tasks_without_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(worker_module, "ABANDONED_TASK_CHECK_SECONDS", 0.0)
    monkeypatch.setattr(
        SporaMeteoWorker,
        "recover_abandoned_tasks",
        lambda self: calls.append(self.worker_id) or 0,
    )

    _current_worker().run()

    assert calls == ["test-current-worker"]


@pytest.mark.django_db(transaction=True)
def test_result_persistence_failure_marks_application_attempt_failed(
    representative_input: RepresentativeInput,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=f"test:result-failure:{representative_input.id}",
    )

    def fail_result_persistence(*, attempt_id: uuid.UUID, result_value: int) -> int:
        raise RuntimeError("result persistence failed")

    monkeypatch.setattr(
        "background_jobs.tasks._mark_attempt_completed",
        fail_result_persistence,
    )

    with pytest.raises(RuntimeError, match="result persistence failed"):
        representative_job_task.func(attempt_id=str(attempt.id))

    attempt.refresh_from_db()
    assert attempt.state == BackgroundJobAttempt.State.FAILED
    assert "result persistence failed" in attempt.failure_reason
    assert not RepresentativeResult.objects.filter(job_id=attempt.job_id).exists()


@pytest.mark.django_db(transaction=True)
def test_background_job_status_command_prints_attempt_history(
    representative_input: RepresentativeInput,
) -> None:
    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=f"test:status:{representative_input.id}",
    )
    output = io.StringIO()

    call_command("background_job_status", str(attempt.id), stdout=output)

    text = output.getvalue()
    assert f"attempt_id={attempt.id}" in text
    assert "state=waiting" in text
    assert f"task_id={attempt.task_id}" in text


@pytest.mark.django_db(transaction=True)
@override_settings(
    TASKS={
        "default": {
            "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
        }
    }
)
def test_explicit_immediate_backend_override_runs_task_inline(
    representative_input: RepresentativeInput,
) -> None:
    assert (
        settings.TASKS["default"]["BACKEND"]
        == "django.tasks.backends.immediate.ImmediateBackend"
    )

    attempt = enqueue_representative_job(
        input_id=representative_input.id,
        logical_key=f"test:immediate:{representative_input.id}",
    )

    attempt.refresh_from_db()
    assert attempt.state == BackgroundJobAttempt.State.COMPLETED
    assert attempt.task_id is None


def _test_enqueue_view(request):
    input_id = uuid.UUID(request.GET["input_id"])
    attempt = enqueue_representative_job(
        input_id=input_id,
        logical_key=f"test:http:{input_id}",
    )
    return JsonResponse({"attempt_id": str(attempt.id), "state": attempt.state})


urlpatterns = [path("test-enqueue/", _test_enqueue_view)]


@pytest.mark.django_db(transaction=True)
@override_settings(ROOT_URLCONF="tests.app.test_background_jobs")
def test_request_path_queues_without_running_representative_work(
    representative_input: RepresentativeInput,
) -> None:
    query = urlencode({"input_id": representative_input.id})

    response = Client().get(f"/test-enqueue/?{query}")

    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["state"] == BackgroundJobAttempt.State.WAITING
    attempt = BackgroundJobAttempt.objects.get(id=body["attempt_id"])
    assert not RepresentativeResult.objects.filter(job_id=attempt.job_id).exists()
    assert DBTaskResult.objects.get(id=attempt.task_id).status == "READY"
