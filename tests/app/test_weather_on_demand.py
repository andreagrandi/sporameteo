import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic as real_monotonic, sleep
from urllib.parse import parse_qs, urlsplit

import pytest
from django.conf import settings
from django.db import IntegrityError, OperationalError, close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from django_tasks_db.models import DBTaskResult

import weather_on_demand.services as weather_services
from background_jobs.services import logical_job_locks
from weather_requests.eligibility import weather_cell
from weather_requests.models import WeatherRequest, WeatherRequestAttempt
from weather_requests.provider import request_spec_from_request
from weather_requests.quota import reserve_attempt
from weather_on_demand.models import (
    WeatherCleanupJob,
    WeatherFetch,
    WeatherFetchBatch,
    WeatherFetchPart,
    WeatherVisitorReservation,
    WeatherVisitorWindow,
)
from weather_summaries.models import WeatherDaily, WeatherSnapshot


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANAGE_PATH = PROJECT_ROOT / "apps" / "application" / "manage.py"

from weather_on_demand.services import (
    CellWeatherResult,
    WeatherLookupResult,
    configure_cleanup_callback,
    configure_estimate_callback,
    italian_assessment_date,
    notify_snapshot_ready,
    opaque_visitor_hash,
    weather_fetch_lock_key,
)


class _FakeOpenMeteo:
    def __init__(
        self,
        *,
        rain_values: tuple[float | None, ...] = (1.0,),
        status: int = 200,
        message: str = "",
        elevation: float | None = 200,
        started: Event | None = None,
        release: Event | None = None,
    ) -> None:
        self.rain_values = rain_values
        self.status = status
        self.message = message
        self.elevation = elevation
        self.started = started
        self.release = release
        self.calls: list[dict[str, str]] = []
        self._lock = Lock()

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)

    def __call__(self, *, url: str, timeout: float) -> object:
        query = {
            key: values[0]
            for key, values in parse_qs(urlsplit(url).query).items()
        }
        with self._lock:
            call_number = len(self.calls)
            self.calls.append(query)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(timeout=10)
        if self.status != 200:
            return self.status, {}, self.message.encode("utf-8")

        def query_time(*, date_name: str, hour_name: str) -> datetime:
            hour_value = query[hour_name]
            if "T" in hour_value:
                parsed = datetime.fromisoformat(hour_value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed
            return datetime.fromisoformat(
                f"{query[date_name]}T{int(hour_value):02d}:00:00+00:00"
            )

        start = query_time(date_name="start_date", hour_name="start_hour")
        end = query_time(date_name="end_date", hour_name="end_hour")
        hours = int((end - start).total_seconds() // 3600) + 1
        variables = query["hourly"].split(",")
        units = {
            "precipitation": "mm",
            "temperature_2m": "°C",
            "soil_moisture_0_to_7cm": "m³/m³",
            "soil_temperature_0_to_7cm": "°C",
            "et0_fao_evapotranspiration": "mm",
        }
        rain_value = self.rain_values[min(call_number, len(self.rain_values) - 1)]
        times = [int((start + timedelta(hours=index)).timestamp()) for index in range(hours)]
        latitudes = query["latitude"].split(",")
        longitudes = query["longitude"].split(",")
        return [
            {
                "latitude": float(latitude),
                "longitude": float(longitude),
                "elevation": self.elevation,
                "timezone": "GMT",
                "hourly_units": {variable: units[variable] for variable in variables},
                "hourly": {
                    "time": times,
                    **{
                        variable: [
                            rain_value
                            if variable == "precipitation"
                            else 0.5
                            if variable == "soil_moisture_0_to_7cm"
                            else 10.0
                        ]
                        * hours
                        for variable in variables
                    },
                },
            }
            for latitude, longitude in zip(latitudes, longitudes, strict=True)
        ]


def _local_weather_handler(fake: _FakeOpenMeteo) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = fake(url=f"http://127.0.0.1{self.path}", timeout=60.0)
            if isinstance(payload, tuple):
                status, _headers, body = payload
                body_bytes = body if isinstance(body, bytes) else str(body).encode("utf-8")
                self.send_response(int(status))
            else:
                body_bytes = json.dumps(payload).encode("utf-8")
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _worker_environment(*, endpoint: str) -> dict[str, str]:
    database = settings.DATABASES["default"]
    environment = os.environ.copy()
    environment.update(
        {
            "SPORAMETEO_DB_NAME": str(database["NAME"]),
            "SPORAMETEO_DB_USER": str(database["USER"]),
            "SPORAMETEO_DB_PASSWORD": str(database["PASSWORD"]),
            "SPORAMETEO_DB_HOST": str(database["HOST"]),
            "SPORAMETEO_DB_PORT": str(database["PORT"]),
            "SPORAMETEO_WEATHER_ENDPOINT": endpoint,
            "SPORAMETEO_WEATHER_HISTORY_ENDPOINT": endpoint,
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


@pytest.fixture(autouse=True)
def disable_cross_app_callbacks() -> Iterator[None]:
    """Keep weather tests independent from optional estimate storage."""
    estimate_callback = weather_services._ESTIMATE_CALLBACK
    cleanup_callback = weather_services._CLEANUP_CALLBACK
    configure_estimate_callback(None)
    configure_cleanup_callback(None)
    yield
    configure_estimate_callback(estimate_callback)
    configure_cleanup_callback(cleanup_callback)


def _cell(*, index: int = 0):
    return weather_cell(latitude=44.05 + index * 0.01, longitude=10.85)


def _clock(*, day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _request_weather(
    *,
    cells,
    day: date,
    visitor_id: str = "visitor-1",
    transport: object,
    wait_seconds: float = 3.0,
    forest_build_version: str = "",
):
    close_old_connections()
    try:
        return weather_services.request_weather(
            cells=cells,
            visitor_id=visitor_id,
            assessment_date=day,
            clock=lambda: _clock(day=day),
            transport=transport,
            wait_seconds=wait_seconds,
            forest_build_version=forest_build_version,
        )
    finally:
        close_old_connections()


def _wait_for_fetch(*, cell_key: str, day: date, timeout: float = 15.0) -> WeatherFetch:
    deadline = real_monotonic() + timeout
    while real_monotonic() < deadline:
        fetch = WeatherFetch.objects.get(cell_key=cell_key, assessment_date=day)
        if fetch.status == WeatherFetch.Status.COMPLETED:
            return fetch
        sleep(0.05)
    raise AssertionError(f"Fetch for {cell_key} on {day} did not complete.")


def test_rome_assessment_date_uses_the_local_day_on_both_clock_change_boundaries() -> None:
    assert italian_assessment_date(
        clock=lambda: datetime(2026, 3, 28, 23, 30, tzinfo=UTC)
    ) == date(2026, 3, 29)
    assert italian_assessment_date(
        clock=lambda: datetime(2026, 10, 24, 22, 30, tzinfo=UTC)
    ) == date(2026, 10, 25)


def test_visitor_hash_is_stable_and_does_not_include_the_original_identifier() -> None:
    first = opaque_visitor_hash(visitor_id="visitor-123")

    assert first == opaque_visitor_hash(visitor_id="visitor-123")
    assert len(first) == 64
    assert "visitor-123" not in first
    assert first != opaque_visitor_hash(visitor_id="visitor-456")


def test_weather_cell_lock_key_is_stable_for_one_cell_and_date() -> None:
    first = weather_fetch_lock_key(
        cell_key="44.05,10.85",
        assessment_date=date(2026, 10, 11),
    )

    assert first == weather_fetch_lock_key(
        cell_key="44.05,10.85",
        assessment_date=date(2026, 10, 11),
    )
    assert first != weather_fetch_lock_key(
        cell_key="44.05,10.85",
        assessment_date=date(2026, 10, 12),
    )


def test_lookup_result_exposes_real_snapshot_date_and_fetch_time() -> None:
    fetched = datetime(2026, 10, 11, 14, tzinfo=UTC)
    cell = CellWeatherResult(
        cell_key="44.05,10.85",
        state="previous",
        assessment_date=date(2026, 10, 10),
        fetched_at=fetched,
        source="open-meteo:request",
        snapshot_id="snapshot-1",
        reason_code="provider_unavailable",
        full_reason="provider is temporarily unavailable",
    )
    result = WeatherLookupResult(assessment_date=date(2026, 10, 11), cells=(cell,))

    assert result["44.05,10.85"].assessment_date == date(2026, 10, 10)
    assert result.as_dict()["cells"][0]["fetched_at"] == fetched.isoformat()


def test_estimate_and_cleanup_adapters_have_stable_public_contracts() -> None:
    estimate_calls: list[dict[str, object]] = []
    cleanup_calls: list[date] = []
    configure_estimate_callback(lambda **kwargs: estimate_calls.append(kwargs))
    configure_cleanup_callback(lambda *, cleanup_date: cleanup_calls.append(cleanup_date) or ["protected"])

    notify_snapshot_ready(
        snapshot_id="snapshot-1",
        cell_key="44.05,10.85",
        assessment_date=date(2026, 10, 11),
        forest_build_version="forest-v1",
    )

    assert estimate_calls == [
        {
            "snapshot_id": "snapshot-1",
            "cell_key": "44.05,10.85",
            "assessment_date": date(2026, 10, 11),
            "forest_build_version": "forest-v1",
        }
    ]
    configure_estimate_callback(None)
    configure_cleanup_callback(None)


@pytest.mark.django_db(transaction=True)
def test_ready_snapshot_calls_estimate_hook_after_commit() -> None:
    day = date(2026, 10, 11)
    cell = _cell()
    calls: list[dict[str, object]] = []
    called = Event()

    def callback(**kwargs: object) -> None:
        calls.append(kwargs)
        called.set()

    configure_estimate_callback(callback)

    result = _request_weather(
        cells=(cell,),
        day=day,
        transport=_FakeOpenMeteo(),
        forest_build_version="forest-v1",
    )

    fetch = WeatherFetch.objects.get(cell_key=cell.key, assessment_date=day)
    assert result.cells[0].state == "current"
    # The fetch thread runs the hook just after its commit, which can be after the request returns.
    assert called.wait(timeout=15)
    assert calls == [
        {
            "snapshot_id": str(fetch.snapshot_id),
            "cell_key": cell.key,
            "assessment_date": day,
            "forest_build_version": "forest-v1",
        }
    ]


@pytest.mark.django_db(transaction=True)
def test_failed_estimate_hook_retries_from_ready_snapshot_without_provider_refetch() -> None:
    day = date(2026, 10, 11)
    cell = _cell()
    fake = _FakeOpenMeteo()
    calls: list[dict[str, object]] = []

    def callback(**kwargs: object) -> None:
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("estimate queue unavailable")

    configure_estimate_callback(callback)
    _request_weather(
        cells=(cell,),
        day=day,
        transport=fake,
        forest_build_version="forest-v1",
    )
    # The fetch thread records the failed hook just after its commit, which can be after the request returns.
    deadline = real_monotonic() + 15
    while (
        WeatherFetch.objects.get(cell_key=cell.key, assessment_date=day).estimate_callback_status != "pending"
        and real_monotonic() < deadline
    ):
        sleep(0.05)
    fetch = WeatherFetch.objects.get(cell_key=cell.key, assessment_date=day)
    assert fetch.estimate_callback_status == "pending"
    assert fetch.estimate_callback_reason == "estimate queue unavailable"
    snapshot_id = fetch.snapshot_id

    retry_at = fetch.estimate_callback_retry_at
    assert retry_at is not None
    weather_services.recover_weather_fetches(now=retry_at + timedelta(seconds=1))
    fetch.refresh_from_db()
    assert fetch.estimate_callback_status == "completed"
    assert fetch.snapshot_id == snapshot_id
    assert fake.call_count == 4
    assert len(calls) == 2


@pytest.mark.django_db(transaction=True)
def test_first_multi_cell_request_splits_history_and_forecast_and_repeat_is_cached() -> None:
    day = date(2026, 10, 11)
    cells = (_cell(index=0), _cell(index=1))
    fake = _FakeOpenMeteo()

    first = _request_weather(cells=cells, day=day, transport=fake)
    assert [item.state for item in first.cells] == ["current", "current"]
    assert fake.call_count == 4
    assert WeatherSnapshot.objects.filter(assessment_date=day).count() == 2
    assert WeatherRequestAttempt.objects.count() == 4

    second = _request_weather(
        cells=cells,
        day=day,
        transport=lambda **kwargs: (_ for _ in ()).throw(AssertionError("cache miss")),
    )

    assert [item.state for item in second.cells] == ["current", "current"]
    assert fake.call_count == 4
    assert WeatherRequestAttempt.objects.count() == 4


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("cell_count", [2, 24])
def test_ready_weather_uses_three_queries_regardless_of_cell_count(cell_count: int) -> None:
    day = date(2026, 10, 11)
    cells = tuple(_cell(index=index) for index in range(cell_count))
    for cell in cells:
        snapshot = WeatherSnapshot.objects.create(
            cell_key=cell.key,
            assessment_date=day,
            pipeline_version=weather_services.PIPELINE_VERSION,
            latest_retrieval=_clock(day=day),
            source_ids=["stored-source"],
        )
        WeatherFetch.objects.create(
            cell_key=cell.key,
            assessment_date=day,
            pipeline_version=weather_services.PIPELINE_VERSION,
            status=WeatherFetch.Status.COMPLETED,
            snapshot=snapshot,
            estimate_callback_status="completed",
        )
    with CaptureQueriesContext(connection) as queries:
        result = weather_services.request_weather(cells=cells, assessment_date=day)
    assert len(queries) == 3
    assert len(result.cells) == cell_count
    assert {item.state for item in result.cells} == {"current"}
    assert {item.source for item in result.cells} == {"stored-source"}
    assert WeatherRequestAttempt.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_checked_known_missing_snapshot_is_ready_and_never_refetched() -> None:
    day = date(2026, 10, 11)
    cell = _cell()
    fake = _FakeOpenMeteo(rain_values=(None,))

    first = _request_weather(cells=(cell,), day=day, transport=fake)
    assert first.cells[0].state == "current"
    assert first.cells[0].reason_code == "known_missing"
    assert WeatherSnapshot.objects.filter(cell_key=cell.key, assessment_date=day).exists()

    second = _request_weather(
        cells=(cell,),
        day=day,
        transport=lambda **kwargs: (_ for _ in ()).throw(AssertionError("ready refetch")),
    )
    assert second.cells[0].reason_code == "known_missing"
    assert fake.call_count == 4


@pytest.mark.django_db(transaction=True)
def test_subprocess_worker_uses_real_http_and_duplicate_weather_tasks_are_serialized() -> None:
    day = date(2026, 10, 11)
    fake = _FakeOpenMeteo()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _local_weather_handler(fake))
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/forecast"
    previous_endpoint = os.environ.get("SPORAMETEO_WEATHER_ENDPOINT")
    previous_history_endpoint = os.environ.get("SPORAMETEO_WEATHER_HISTORY_ENDPOINT")
    os.environ["SPORAMETEO_WEATHER_ENDPOINT"] = endpoint
    os.environ["SPORAMETEO_WEATHER_HISTORY_ENDPOINT"] = endpoint
    try:
        result = weather_services.request_weather(
            cells=(_cell(),),
            visitor_id="subprocess-worker",
            assessment_date=day,
            clock=lambda: _clock(day=day),
            wait_seconds=0,
        )
        assert result.cells[0].state == "not_available_yet"
        fetch = WeatherFetch.objects.get(cell_key=_cell().key, assessment_date=day)
        batch = fetch.batch
        assert batch is not None
        assert batch.task_id is not None
        from weather_on_demand.tasks import weather_fetch_task

        duplicate_task = weather_fetch_task.enqueue(batch_id=str(batch.id))
        first = subprocess.Popen(
            _worker_command(worker_id="weather-http-worker-one"),
            cwd=PROJECT_ROOT,
            env=_worker_environment(endpoint=endpoint),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second = subprocess.Popen(
            _worker_command(worker_id="weather-http-worker-two"),
            cwd=PROJECT_ROOT,
            env=_worker_environment(endpoint=endpoint),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        first_output = first.communicate(timeout=30)
        second_output = second.communicate(timeout=30)
        assert first.returncode == 0, first_output[1] or first_output[0]
        assert second.returncode == 0, second_output[1] or second_output[0]
        fetch.refresh_from_db()
        batch.refresh_from_db()
        assert fetch.status == WeatherFetch.Status.COMPLETED
        assert WeatherSnapshot.objects.filter(
            cell_key=_cell().key,
            assessment_date=day,
        ).count() == 1
        assert fake.call_count == 4
        assert DBTaskResult.objects.filter(id=duplicate_task.id).exists()
        assert DBTaskResult.objects.filter(
            task_path="weather_on_demand.tasks.weather_fetch_task",
        ).count() == 5
        assert batch.status == WeatherFetchBatch.Status.COMPLETED
    finally:
        if previous_history_endpoint is None:
            os.environ.pop("SPORAMETEO_WEATHER_HISTORY_ENDPOINT", None)
        else:
            os.environ["SPORAMETEO_WEATHER_HISTORY_ENDPOINT"] = previous_history_endpoint
        if previous_endpoint is None:
            os.environ.pop("SPORAMETEO_WEATHER_ENDPOINT", None)
        else:
            os.environ["SPORAMETEO_WEATHER_ENDPOINT"] = previous_endpoint
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.mark.django_db(transaction=True)
def test_yesterday_request_refreshes_completed_day_separately_from_forecast() -> None:
    first_day = date(2026, 10, 11)
    cell = _cell()
    fake = _FakeOpenMeteo(rain_values=(1.0, 3.0))

    _request_weather(cells=(cell,), day=first_day, transport=fake)
    _request_weather(cells=(cell,), day=first_day + timedelta(days=1), transport=fake)
    _wait_for_fetch(cell_key=cell.key, day=first_day + timedelta(days=1))

    second_request = WeatherRequest.objects.order_by("created_at", "id").last()
    assert second_request is not None
    assert second_request.canonical_spec["local_date_start"] == "2026-10-12"
    assert second_request.canonical_spec["local_date_end"] == "2026-10-19"
    second_spec = request_spec_from_request(request=second_request)
    second_attempt = second_request.attempts.get()
    assert second_attempt.reservation_weight == second_spec.reservation_weight
    assert fake.call_count == 6


@pytest.mark.django_db(transaction=True)
def test_ten_day_stale_snapshot_refreshes_old_forecast_days() -> None:
    first_day = date(2026, 10, 11)
    second_day = first_day + timedelta(days=10)
    cell = _cell()
    fake = _FakeOpenMeteo(rain_values=(1.0, 1.0, 1.0, 1.0, 9.0, 9.0))

    _request_weather(cells=(cell,), day=first_day, transport=fake)
    _request_weather(cells=(cell,), day=second_day, transport=fake)
    _wait_for_fetch(cell_key=cell.key, day=second_day)

    refresh_requests = list(WeatherRequest.objects.order_by("created_at", "id"))[4:]
    assert len(refresh_requests) == 2
    history_request, normal_request = refresh_requests
    assert history_request.canonical_spec["local_date_start"] == "2026-10-11"
    assert history_request.canonical_spec["local_date_end"] == "2026-10-20"
    assert normal_request.canonical_spec["local_date_start"] == "2026-10-21"
    assert normal_request.canonical_spec["local_date_end"] == "2026-10-28"
    previous_forecast_day = first_day + timedelta(days=5)
    snapshots = WeatherSnapshot.objects.filter(cell_key=cell.key).order_by("assessment_date")
    assert snapshots.count() == 2
    assert snapshots[0].days.get(date=previous_forecast_day).daily.rain_mm == 24.0
    assert snapshots[1].days.get(date=previous_forecast_day).daily.rain_mm == 216.0
    fetch = WeatherFetch.objects.get(cell_key=cell.key, assessment_date=second_day)
    assert list(
        WeatherFetchPart.objects.filter(fetch=fetch)
        .order_by("start_date")
        .values_list("start_date", "end_date", "status", "reason_code")
    ) == [
        (date(2026, 9, 21), date(2026, 10, 10), WeatherFetchPart.Status.COMPLETED, "stored_history"),
        (date(2026, 10, 11), date(2026, 10, 20), WeatherFetchPart.Status.COMPLETED, "part_stored"),
        (date(2026, 10, 21), date(2026, 10, 28), WeatherFetchPart.Status.COMPLETED, "part_stored"),
    ]
    assert fake.call_count == 6


@pytest.mark.django_db(transaction=True)
def test_concurrent_clients_share_one_cell_fetch() -> None:
    day = date(2026, 10, 11)
    started = Event()
    release = Event()
    fake = _FakeOpenMeteo(started=started, release=release)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _request_weather,
                cells=(_cell(),),
                day=day,
                visitor_id=f"visitor-{index}",
                transport=fake,
            )
            for index in range(2)
        ]
        assert started.wait(timeout=10)
        sleep(0.2)
        release.set()
        results = [future.result(timeout=15) for future in futures]

    assert fake.call_count == 4
    assert all(result.cells[0].state == "current" for result in results)
    assert WeatherFetch.objects.filter(cell_key=_cell().key, assessment_date=day).count() == 1


@pytest.mark.django_db(transaction=True)
def test_slow_fetch_returns_previous_after_three_seconds_and_finishes_in_background() -> None:
    first_day = date(2026, 10, 11)
    second_day = first_day + timedelta(days=1)
    cell = _cell()
    seed = _FakeOpenMeteo()
    _request_weather(cells=(cell,), day=first_day, transport=seed)
    _wait_for_fetch(cell_key=cell.key, day=first_day)

    started = Event()
    release = Event()
    slow = _FakeOpenMeteo(started=started, release=release)
    started_at = real_monotonic()
    result = _request_weather(
        cells=(cell,),
        day=second_day,
        transport=slow,
        wait_seconds=3.0,
    )
    elapsed = real_monotonic() - started_at

    assert elapsed >= 2.8
    assert result.cells[0].state == "previous"
    assert result.cells[0].assessment_date == first_day
    assert slow.call_count == 1

    release.set()
    deadline = real_monotonic() + 10
    while real_monotonic() < deadline:
        if WeatherSnapshot.objects.filter(cell_key=cell.key, assessment_date=second_day).exists():
            break
        sleep(0.05)
    assert WeatherSnapshot.objects.filter(cell_key=cell.key, assessment_date=second_day).exists()


@pytest.mark.django_db(transaction=True)
def test_provider_down_preserves_previous_date_and_429_is_pending_with_full_message() -> None:
    first_day = date(2026, 10, 11)
    second_day = first_day + timedelta(days=1)
    cell = _cell()
    _request_weather(cells=(cell,), day=first_day, transport=_FakeOpenMeteo())
    _wait_for_fetch(cell_key=cell.key, day=first_day)

    down = _FakeOpenMeteo(status=503, message="Open-Meteo is down")
    result = _request_weather(cells=(cell,), day=second_day, transport=down)

    assert result.cells[0].state == "previous"
    assert result.cells[0].assessment_date == first_day
    assert result.cells[0].reason_code == "provider_unavailable"
    assert down.call_count == 2

    refused_cell = _cell(index=1)
    message = "provider says too many places, preserve every byte"
    refused = _FakeOpenMeteo(status=429, message=message)
    refused_result = _request_weather(
        cells=(refused_cell,),
        day=second_day,
        transport=refused,
    )

    refused_fetch = WeatherFetch.objects.get(
        cell_key=refused_cell.key,
        assessment_date=second_day,
    )
    attempt = WeatherRequestAttempt.objects.filter(
        request=refused_fetch.request,
    ).get()
    assert refused_result.cells[0].state == "not_available_yet"
    assert refused_fetch.reason_code == "provider_refused"
    assert attempt.outcome == WeatherRequestAttempt.Outcome.REFUSED
    assert attempt.full_message == message
    assert weather_services.weather_status(assessment_date=second_day)["refused_requests"][0]["full_message"] == message


@pytest.mark.django_db(transaction=True)
def test_permanent_provider_refusal_stays_failed_until_explicit_reopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = date(2026, 10, 11)
    cell = _cell()
    refused = _FakeOpenMeteo(status=400, message="invalid request must be reopened")
    result = _request_weather(cells=(cell,), day=day, transport=refused)
    fetch = WeatherFetch.objects.get(cell_key=cell.key, assessment_date=day)
    batch = fetch.batch
    assert batch is not None
    assert result.cells[0].state == "not_available_yet"
    assert batch.status == WeatherFetchBatch.Status.FAILED
    assert fetch.status == WeatherFetch.Status.FAILED
    assert fetch.full_reason == "invalid request must be reopened"

    monkeypatch.setattr(weather_services, "_schedule_batch", lambda **kwargs: None)
    reopened = weather_services.reopen_weather_fetch(
        batch_id=batch.id,
        now=_clock(day=day),
    )
    assert reopened.status == WeatherFetchBatch.Status.PENDING
    batch.refresh_from_db()
    fetch.refresh_from_db()
    assert batch.request.status == WeatherRequest.Status.PENDING
    assert fetch.status == WeatherFetch.Status.PENDING
    for part in fetch.parts.select_related("batch").order_by("start_date"):
        assert weather_services.process_weather_fetch_batch(
            batch_id=part.batch_id,
            transport=_FakeOpenMeteo(),
            clock=lambda: _clock(day=day),
        )
    fetch.refresh_from_db()
    assert fetch.status == WeatherFetch.Status.COMPLETED


@pytest.mark.django_db(transaction=True)
def test_multipart_refusal_reopens_all_sibling_batches_and_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_day = date(2026, 10, 11)
    second_day = first_day + timedelta(days=10)
    cell = _cell()
    _request_weather(cells=(cell,), day=first_day, transport=_FakeOpenMeteo())
    _wait_for_fetch(cell_key=cell.key, day=first_day)

    refused = _FakeOpenMeteo(status=400, message="multipart request must be reopened")
    _request_weather(cells=(cell,), day=second_day, transport=refused)
    fetch = WeatherFetch.objects.get(cell_key=cell.key, assessment_date=second_day)
    failed_parts = list(WeatherFetchPart.objects.filter(fetch=fetch, batch__isnull=False))
    assert WeatherFetchPart.objects.filter(
        fetch=fetch, reason_code="stored_history", status=WeatherFetchPart.Status.COMPLETED
    ).count() == 1
    failed_batches = list(
        WeatherFetchBatch.objects.filter(
            parts__fetch=fetch,
        ).distinct()
    )
    assert len(failed_parts) == 2
    assert {part.status for part in failed_parts} == {WeatherFetchPart.Status.FAILED}
    assert len(failed_batches) == 2
    assert {batch.status for batch in failed_batches} == {
        WeatherFetchBatch.Status.FAILED
    }
    assert refused.call_count == 1

    monkeypatch.setattr(weather_services, "_schedule_batch", lambda **kwargs: None)
    reopened = weather_services.reopen_weather_fetch(
        batch_id=failed_batches[0].id,
        now=_clock(day=second_day),
    )
    assert reopened.status == WeatherFetchBatch.Status.PENDING
    assert set(
        WeatherFetchPart.objects.filter(fetch=fetch, batch__isnull=False).values_list("status", flat=True)
    ) == {WeatherFetchPart.Status.PENDING}
    assert set(
        WeatherFetchBatch.objects.filter(
            id__in=[batch.id for batch in failed_batches]
        ).values_list("status", flat=True)
    ) == {WeatherFetchBatch.Status.PENDING}

    success = _FakeOpenMeteo()
    for batch in WeatherFetchBatch.objects.filter(
        id__in=[batch.id for batch in failed_batches]
    ).order_by("id"):
        assert weather_services.process_weather_fetch_batch(
            batch_id=batch.id,
            transport=success,
            clock=lambda: _clock(day=second_day),
        )
    fetch.refresh_from_db()
    assert fetch.status == WeatherFetch.Status.COMPLETED
    assert success.call_count == 2


@pytest.mark.django_db(transaction=True)
def test_quota_refusal_preserves_previous_and_never_calls_provider() -> None:
    day = date(2026, 10, 11)
    cell = _cell()
    clock = _clock(day=day)
    quota_request = WeatherRequest.objects.create(
        request_digest="q" * 64,
        canonical_spec={},
        forest_build_version="quota-test",
    )
    reservation = reserve_attempt(
        request=quota_request,
        reservation_weight=540,
        now=clock,
    )
    assert reservation.attempt is not None
    fake = _FakeOpenMeteo()
    result = _request_weather(cells=(cell,), day=day, transport=fake)

    fetch = WeatherFetch.objects.get(cell_key=cell.key, assessment_date=day)
    assert result.cells[0].state == "not_available_yet"
    assert fetch.reason_code == "quota_exhausted"
    assert fake.call_count == 0


@pytest.mark.django_db(transaction=True)
def test_visitor_can_start_only_one_hundred_new_cells_then_retry_after_window() -> None:
    day = date(2026, 10, 11)
    cells = tuple(_cell(index=index) for index in range(101))
    result = weather_services.request_weather(
        cells=cells,
        visitor_id="limited-visitor",
        assessment_date=day,
        clock=lambda: _clock(day=day),
        wait_seconds=0,
    )

    window = WeatherVisitorWindow.objects.get(
        visitor_hash=opaque_visitor_hash(visitor_id="limited-visitor")
    )
    limited = WeatherFetch.objects.filter(
        assessment_date=day,
        reason_code="visitor_limit",
    )
    limited_fetch = limited.get()
    assert len(result.cells) == 101
    assert window.new_cell_count == 100
    assert limited.count() == 1

    later_clock = _clock(day=day) + timedelta(seconds=61)
    retry_result = weather_services.request_weather(
        cells=(_cell(index=100),),
        visitor_id="limited-visitor",
        assessment_date=day,
        clock=lambda: later_clock,
        wait_seconds=0,
    )
    limited_fetch.refresh_from_db()
    assert retry_result.cells[0].reason_code != "visitor_limit"
    assert limited_fetch.reason_code == "fetch_pending"


@pytest.mark.django_db(transaction=True)
def test_visitor_limit_uses_a_rolling_sixty_second_window() -> None:
    day = date(2026, 10, 11)
    visitor_id = "rolling-visitor"
    first_clock = _clock(day=day)
    weather_services.request_weather(
        cells=(_cell(index=0),),
        visitor_id=visitor_id,
        assessment_date=day,
        clock=lambda: first_clock,
        wait_seconds=0,
    )
    second_clock = first_clock + timedelta(seconds=59)
    weather_services.request_weather(
        cells=tuple(_cell(index=index) for index in range(1, 100)),
        visitor_id=visitor_id,
        assessment_date=day,
        clock=lambda: second_clock,
        wait_seconds=0,
    )
    window = WeatherVisitorWindow.objects.get(
        visitor_hash=opaque_visitor_hash(visitor_id=visitor_id)
    )
    assert window.new_cell_count == 100
    assert WeatherVisitorReservation.objects.filter(
        visitor_hash=window.visitor_hash,
    ).count() == 100

    third_clock = first_clock + timedelta(seconds=60, milliseconds=100)
    weather_services.request_weather(
        cells=tuple(_cell(index=index) for index in range(100, 200)),
        visitor_id=visitor_id,
        assessment_date=day,
        clock=lambda: third_clock,
        wait_seconds=0,
    )
    window.refresh_from_db()
    assert window.new_cell_count == 100
    assert WeatherVisitorReservation.objects.filter(
        visitor_hash=window.visitor_hash,
    ).count() == 100
    assert WeatherFetch.objects.filter(
        assessment_date=day,
        cell_key__in=[_cell(index=index).key for index in range(100, 200)],
        reason_code="visitor_limit",
    ).count() == 99


@pytest.mark.django_db(transaction=True)
def test_request_service_uses_rome_date_on_both_dst_boundaries() -> None:
    cell = _cell()
    spring_clock = lambda: datetime(2026, 3, 28, 23, 30, tzinfo=UTC)
    autumn_clock = lambda: datetime(2026, 10, 24, 22, 30, tzinfo=UTC)

    weather_services.request_weather(
        cells=(cell,),
        visitor_id="dst-spring",
        clock=spring_clock,
        wait_seconds=0,
    )
    weather_services.request_weather(
        cells=(cell,),
        visitor_id="dst-autumn",
        clock=autumn_clock,
        wait_seconds=0,
    )

    assert set(
        WeatherFetch.objects.filter(cell_key=cell.key).values_list(
            "assessment_date", flat=True
        )
    ) == {date(2026, 3, 29), date(2026, 10, 25)}


@pytest.mark.django_db(transaction=True)
def test_cleanup_keeps_two_latest_and_referenced_snapshots_without_provider_calls() -> None:
    cell = _cell()
    base_day = date(2026, 10, 11)
    fake = _FakeOpenMeteo()
    days = tuple(base_day - timedelta(days=offset) for offset in (50, 40, 30, 20, 10, 0))
    for day in days:
        _request_weather(cells=(cell,), day=day, transport=fake)
        _wait_for_fetch(cell_key=cell.key, day=day)

    snapshots = list(WeatherSnapshot.objects.filter(cell_key=cell.key).order_by("assessment_date"))
    assert len(snapshots) == len(days)
    referenced = snapshots[0]
    unreferenced = snapshots[1]
    WeatherFetch.objects.filter(snapshot_id__in=[snapshot.id for snapshot in snapshots[:2]]).delete()
    WeatherFetch.objects.exclude(assessment_date=base_day).update(
        snapshot=None,
        previous_snapshot=None,
    )
    latest_fetch = WeatherFetch.objects.get(cell_key=cell.key, assessment_date=base_day)
    latest_fetch.previous_snapshot = referenced
    latest_fetch.save(update_fields=["previous_snapshot", "updated_at"])
    before_daily_count = WeatherDaily.objects.count()
    with CaptureQueriesContext(connection) as queries:
        cleanup = weather_services.cleanup_weather(
            cleanup_date=base_day,
            protected_snapshot_ids=(str(referenced.id),),
        )

    # The cleanup must not load the JSON fields of every snapshot into memory.
    assert not [
        query["sql"]
        for query in queries
        if query["sql"].startswith("SELECT") and '"weather_summaries_weathersnapshot"."metadata"' in query["sql"]
    ]
    assert cleanup["deleted_snapshot_count"] == 3
    assert WeatherSnapshot.objects.filter(id=referenced.id).exists()
    assert not WeatherSnapshot.objects.filter(id=unreferenced.id).exists()
    assert WeatherSnapshot.objects.filter(cell_key=cell.key).count() == 3
    assert WeatherDaily.objects.count() < before_daily_count
    assert fake.call_count == 14


@pytest.mark.django_db(transaction=True)
def test_cleanup_callback_protects_estimate_snapshot_and_makes_no_http_call() -> None:
    day = date(2026, 10, 11)
    cell = _cell()
    fake = _FakeOpenMeteo()
    _request_weather(cells=(cell,), day=day - timedelta(days=40), transport=fake)
    _wait_for_fetch(cell_key=cell.key, day=day - timedelta(days=40))
    old_snapshot = WeatherSnapshot.objects.get(cell_key=cell.key)
    WeatherFetch.objects.filter(snapshot=old_snapshot).delete()
    configure_cleanup_callback(lambda *, cleanup_date: [str(old_snapshot.id)])
    try:
        cleanup = weather_services.cleanup_weather(
            cleanup_date=day,
            protected_snapshot_ids=(),
        )
    finally:
        configure_cleanup_callback(None)

    assert cleanup["deleted_snapshot_count"] == 0
    assert WeatherSnapshot.objects.filter(id=old_snapshot.id).exists()
    assert fake.call_count == 4


@pytest.mark.django_db(transaction=True)
def test_cleanup_enqueue_is_idempotent_for_one_date() -> None:
    cleanup_date = date(2026, 10, 11)

    first = weather_services.enqueue_cleanup(cleanup_date=cleanup_date)
    second = weather_services.enqueue_cleanup(cleanup_date=cleanup_date)

    assert first.id == second.id
    assert first.task_id is not None
    assert second.task_id == first.task_id
    assert WeatherCleanupJob.objects.filter(cleanup_date=cleanup_date).count() == 1


@pytest.mark.django_db(transaction=True)
def test_recovery_requeues_pending_weather_and_cleanup_tasks_after_claim_gap() -> None:
    day = date(2026, 10, 11)
    cell = _cell()

    _request_weather(
        cells=(cell,),
        day=day,
        transport=None,
        wait_seconds=0,
    )
    batch = WeatherFetchBatch.objects.filter(assessment_date=day).earliest("first_requested_at")
    weather_task_id = batch.task_id
    assert weather_task_id is not None
    DBTaskResult.objects.get(id=weather_task_id).claim(worker_id="dead-weather-worker")

    cleanup = weather_services.enqueue_cleanup(cleanup_date=day)
    cleanup_task_id = cleanup.task_id
    assert cleanup_task_id is not None
    DBTaskResult.objects.get(id=cleanup_task_id).claim(worker_id="dead-cleanup-worker")

    weather_services.recover_weather_fetches(now=_clock(day=day))

    batch.refresh_from_db()
    cleanup.refresh_from_db()
    assert DBTaskResult.objects.get(id=weather_task_id).status == "FAILED"
    assert DBTaskResult.objects.get(id=cleanup_task_id).status == "FAILED"
    assert batch.task_id is not None
    assert batch.task_id != weather_task_id
    assert cleanup.task_id is not None
    assert cleanup.task_id != cleanup_task_id
    assert DBTaskResult.objects.get(id=batch.task_id).status == "READY"
    assert DBTaskResult.objects.get(id=cleanup.task_id).status == "READY"


@pytest.mark.django_db(transaction=True)
def test_cleanup_clears_each_terminal_snapshot_reference_before_deleting_old_chain() -> None:
    cell = _cell()
    cleanup_day = date(2026, 10, 11)
    fake = _FakeOpenMeteo()
    assessment_days = tuple(cleanup_day - timedelta(days=offset) for offset in (50, 49, 48))

    for assessment_day in assessment_days:
        _request_weather(cells=(cell,), day=assessment_day, transport=fake)
        _wait_for_fetch(cell_key=cell.key, day=assessment_day)

    snapshots = list(
        WeatherSnapshot.objects.filter(cell_key=cell.key).order_by("assessment_date")
    )
    assert len(snapshots) == 3
    fetches = list(
        WeatherFetch.objects.filter(cell_key=cell.key).order_by("assessment_date")
    )
    assert fetches[1].previous_snapshot_id == snapshots[0].id
    cleanup = weather_services.cleanup_weather(cleanup_date=cleanup_day)

    assert cleanup["deleted_snapshot_count"] == 1
    assert not WeatherSnapshot.objects.filter(id=snapshots[0].id).exists()
    fetches[1].refresh_from_db()
    assert fetches[1].previous_snapshot_id is None
    assert WeatherSnapshot.objects.filter(cell_key=cell.key).count() == 2
    assert fake.call_count == 8


@pytest.mark.django_db(transaction=True)
def test_storage_conflict_fails_without_requeue_or_second_provider_call() -> None:
    first_day = date(2026, 10, 11)
    second_day = first_day + timedelta(days=1)
    cell = _cell()

    _request_weather(
        cells=(cell,),
        day=first_day,
        transport=_FakeOpenMeteo(elevation=200),
    )
    conflicting_provider = _FakeOpenMeteo(elevation=201)
    _request_weather(
        cells=(cell,),
        day=second_day,
        transport=conflicting_provider,
        wait_seconds=3,
    )

    fetch = WeatherFetch.objects.get(cell_key=cell.key, assessment_date=second_day)
    fetch.refresh_from_db()
    assert fetch.status == WeatherFetch.Status.FAILED
    assert fetch.reason_code == "storage_conflict"
    assert "elevation" in fetch.full_reason
    failed_part = fetch.parts.filter(status=WeatherFetchPart.Status.FAILED).first()
    assert failed_part is not None
    assert failed_part.batch.status == WeatherFetchBatch.Status.FAILED
    request = failed_part.batch.request
    assert request.attempts.count() == 1
    assert conflicting_provider.call_count == 2


@pytest.mark.django_db(transaction=True)
def test_atomic_storage_failure_charges_new_attempt_then_recovers_completed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = date(2026, 10, 11)
    cell = _cell()
    fake = _FakeOpenMeteo()
    original_store = weather_services.store_weather_daily_rows
    failures = 0

    def fail_once(*args, **kwargs):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise OperationalError("storage failed after provider response")
        return original_store(*args, **kwargs)

    monkeypatch.setattr(weather_services, "store_weather_daily_rows", fail_once)
    result = _request_weather(cells=(cell,), day=day, transport=None, wait_seconds=0)
    fetch = WeatherFetch.objects.get(cell_key=cell.key, assessment_date=day)
    batch = fetch.batch
    assert batch is not None
    weather_services.process_weather_fetch_batch(
        batch_id=batch.id, transport=fake, clock=lambda: _clock(day=day),
    )
    batch.refresh_from_db()
    assert result.cells[0].state == "not_available_yet"
    assert WeatherRequestAttempt.objects.filter(request=batch.request).count() == 1
    assert batch.request.status == WeatherRequest.Status.COMPLETED
    assert WeatherSnapshot.objects.filter(cell_key=cell.key, assessment_date=day).count() == 0

    batch.retry_at = None
    batch.status = WeatherFetchBatch.Status.PENDING
    batch.save(update_fields=["retry_at", "status", "updated_at"])
    assert weather_services.process_weather_fetch_batch(
        batch_id=batch.id,
        transport=fake,
        clock=lambda: _clock(day=day),
    )
    assert WeatherRequestAttempt.objects.filter(request=batch.request).count() == 2
    for part in fetch.parts.exclude(batch_id=batch.id).order_by("start_date"):
        weather_services.process_weather_fetch_batch(
            batch_id=part.batch_id, transport=fake, clock=lambda: _clock(day=day),
        )
    assert WeatherSnapshot.objects.filter(cell_key=cell.key, assessment_date=day).count() == 1


@pytest.mark.django_db(transaction=True)
def test_unexpected_storage_exception_fails_without_requeue_or_second_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = date(2026, 10, 11)
    cell = _cell()
    fake = _FakeOpenMeteo()

    def fail_unexpectedly(*args, **kwargs):
        raise IntegrityError("repeatable storage constraint failure")

    monkeypatch.setattr(weather_services, "store_weather_daily_rows", fail_unexpectedly)
    result = _request_weather(cells=(cell,), day=day, transport=None, wait_seconds=0)

    fetch = WeatherFetch.objects.get(cell_key=cell.key, assessment_date=day)
    batch = fetch.batch
    weather_services.process_weather_fetch_batch(
        batch_id=batch.id, transport=fake, clock=lambda: _clock(day=day),
    )
    fetch.refresh_from_db()
    assert result.cells[0].state == "not_available_yet"
    assert fetch.status == WeatherFetch.Status.FAILED
    assert fetch.reason_code == "storage_failure"
    assert "repeatable storage constraint failure" in fetch.full_reason
    assert fetch.batch is not None
    assert fetch.batch.status == WeatherFetchBatch.Status.FAILED
    assert fetch.batch.task_id is None
    assert fetch.request is not None
    assert batch.request.attempts.count() == 1
    assert fake.call_count == 1


@pytest.mark.django_db(transaction=True)
def test_recovery_returns_abandoned_batch_to_pending_and_uses_provider_retry_time() -> None:
    day = date(2026, 10, 11)
    cell = _cell()
    fake = _FakeOpenMeteo()
    weather_services.request_weather(
        cells=(cell,),
        visitor_id="recovery",
        assessment_date=day,
        clock=lambda: _clock(day=day),
        transport=None,
        wait_seconds=0,
    )
    fetch = WeatherFetch.objects.get(cell_key=cell.key, assessment_date=day)
    batch = fetch.batch
    assert batch is not None
    retry_time = _clock(day=day) + timedelta(minutes=5)
    batch.status = WeatherFetchBatch.Status.RUNNING
    batch.started_at = _clock(day=day) - timedelta(minutes=3)
    batch.request.retry_at = retry_time
    batch.request.save(update_fields=["retry_at", "updated_at"])
    batch.save(update_fields=["status", "started_at", "updated_at"])

    recovered = weather_services.recover_weather_fetches(now=_clock(day=day))
    batch.refresh_from_db()

    assert recovered == 1
    assert batch.status == WeatherFetchBatch.Status.PENDING
    assert batch.retry_at == retry_time
    assert batch.task_id is not None


@contextmanager
def _locks_held_by_another_session(*, keys: tuple[str, ...]) -> Iterator[None]:
    locked = Event()
    release = Event()

    def hold_locks() -> None:
        try:
            with logical_job_locks(logical_keys=keys, wait=True):
                locked.set()
                release.wait(timeout=10)
        finally:
            connection.close()

    holder = Thread(target=hold_locks)
    holder.start()
    try:
        assert locked.wait(timeout=10)
        yield
    finally:
        release.set()
        holder.join(timeout=10)


def _running_batch_started_long_ago(*, day: date) -> WeatherFetchBatch:
    weather_services.request_weather(
        cells=(_cell(),),
        visitor_id="lock-recovery",
        assessment_date=day,
        clock=lambda: _clock(day=day),
        transport=None,
        wait_seconds=0,
    )
    batch = WeatherFetch.objects.get(assessment_date=day).batch
    assert batch is not None
    WeatherFetchBatch.objects.filter(id=batch.id).update(
        status=WeatherFetchBatch.Status.RUNNING,
        started_at=_clock(day=day) - timedelta(minutes=3),
    )
    batch.refresh_from_db()
    return batch


@pytest.mark.django_db(transaction=True)
def test_startup_recovery_leaves_a_running_batch_whose_cell_lock_is_held() -> None:
    day = date(2026, 10, 11)
    batch = _running_batch_started_long_ago(day=day)
    fetch = WeatherFetch.objects.get(assessment_date=day)
    cell_key = f"weather-fetch:{fetch.cell_key}:{day.isoformat()}"

    with _locks_held_by_another_session(keys=(cell_key,)):
        recovered = weather_services.recover_weather_fetches(now=_clock(day=day))

    batch.refresh_from_db()
    assert recovered == 0
    assert batch.status == WeatherFetchBatch.Status.RUNNING


@pytest.mark.django_db(transaction=True)
def test_startup_recovery_leaves_a_running_cleanup_whose_lock_is_held() -> None:
    cleanup = weather_services.enqueue_cleanup(cleanup_date=date(2026, 10, 22))
    WeatherCleanupJob.objects.filter(id=cleanup.id).update(
        status=WeatherCleanupJob.Status.RUNNING,
        task_id=None,
        updated_at=datetime(2026, 10, 1, tzinfo=UTC),
    )

    with _locks_held_by_another_session(keys=(cleanup.logical_key,)):
        weather_services.recover_weather_fetches(now=_clock(day=date(2026, 10, 22)))

    cleanup.refresh_from_db()
    assert cleanup.status == WeatherCleanupJob.Status.RUNNING


@pytest.mark.django_db(transaction=True)
def test_startup_recovery_queues_again_a_running_cleanup_when_its_lock_is_free() -> None:
    cleanup = weather_services.enqueue_cleanup(cleanup_date=date(2026, 10, 22))
    WeatherCleanupJob.objects.filter(id=cleanup.id).update(
        status=WeatherCleanupJob.Status.RUNNING,
        task_id=None,
        updated_at=datetime(2026, 10, 1, tzinfo=UTC),
    )

    recovered = weather_services.recover_weather_fetches(now=_clock(day=date(2026, 10, 22)))

    cleanup.refresh_from_db()
    assert recovered == 1
    assert cleanup.status == WeatherCleanupJob.Status.PENDING
    assert cleanup.task_id is not None


def _fetch_with_callback_running_long_ago(*, day: date, calls: list[dict[str, object]]) -> WeatherFetch:
    configure_estimate_callback(lambda **kwargs: calls.append(kwargs))
    _request_weather(
        cells=(_cell(),),
        day=day,
        transport=_FakeOpenMeteo(),
        forest_build_version="forest-v1",
    )
    deadline = real_monotonic() + 15
    while (
        WeatherFetch.objects.get(assessment_date=day).estimate_callback_status != "completed"
        and real_monotonic() < deadline
    ):
        sleep(0.05)
    fetch = WeatherFetch.objects.get(assessment_date=day)
    assert fetch.estimate_callback_status == "completed"
    WeatherFetch.objects.filter(id=fetch.id).update(
        estimate_callback_status="running",
        estimate_callback_started_at=_clock(day=day) - timedelta(hours=1),
    )
    calls.clear()
    return fetch


@pytest.mark.django_db(transaction=True)
def test_startup_recovery_leaves_a_running_estimate_callback_whose_lock_is_held() -> None:
    day = date(2026, 10, 11)
    calls: list[dict[str, object]] = []
    fetch = _fetch_with_callback_running_long_ago(day=day, calls=calls)

    with _locks_held_by_another_session(
        keys=weather_services.estimate_callback_lock_keys(fetch_id=fetch.id)
    ):
        weather_services.recover_weather_fetches(now=_clock(day=day))

    fetch.refresh_from_db()
    assert fetch.estimate_callback_status == "running"
    assert calls == []


@pytest.mark.django_db(transaction=True)
def test_startup_recovery_runs_again_a_running_estimate_callback_when_its_lock_is_free() -> None:
    day = date(2026, 10, 11)
    calls: list[dict[str, object]] = []
    fetch = _fetch_with_callback_running_long_ago(day=day, calls=calls)

    weather_services.recover_weather_fetches(now=_clock(day=day))

    fetch.refresh_from_db()
    assert fetch.estimate_callback_status == "completed"
    assert len(calls) == 1


@pytest.mark.django_db(transaction=True)
def test_startup_recovery_returns_a_running_batch_with_a_running_queue_row_when_locks_are_free() -> None:
    day = date(2026, 10, 11)
    batch = _running_batch_started_long_ago(day=day)
    old_task_id = batch.task_id
    assert old_task_id is not None
    DBTaskResult.objects.get(id=old_task_id).claim(worker_id="test-stopped-worker")

    recovered = weather_services.recover_weather_fetches(now=_clock(day=day))

    batch.refresh_from_db()
    assert recovered == 1
    assert batch.status == WeatherFetchBatch.Status.PENDING
    assert batch.task_id != old_task_id
    assert DBTaskResult.objects.get(id=old_task_id).status == "FAILED"
