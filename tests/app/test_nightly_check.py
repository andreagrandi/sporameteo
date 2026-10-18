"""Check the nightly check: the reason found, the single email and the retry of the stopped step."""

from __future__ import annotations

import logging

import pytest
from django.core import mail
from django.core.management import call_command
from django_tasks_db.models import DBTaskResult

import weather_on_demand.services as weather_services
from estimate_updates.nightly_check import (
    FORECAST_FAILED,
    NO_WEATHER_RUN,
    NOT_FINISHED,
    PUBLISHED,
    WEATHER_FAILED,
    check_nightly_update,
    retry_nightly_update,
)
from estimate_updates.forecast_runs import process_forecast_run, start_forecast_run
from estimate_updates.tasks import queue_tuscany_forecast
from tests.app.test_forecast_runs import (  # noqa: F401
    _publish_day,
    block_providers,
    local_host_trees,
    tuscany,
)
from tests.app.test_scheduled_weather import (  # noqa: F401
    DAY,
    _clock,
    _drive,
    _start,
    _Transport,
    forecast_calls,
    forest,
    isolate_environment,
)
from weather_on_demand.models import ScheduledWeatherRun, WeatherFetchBatch

pytestmark = pytest.mark.django_db(transaction=True)

PROVIDER_MESSAGE = "Daily API request limit exceeded, please try again tomorrow."


@pytest.fixture
def email_settings(settings) -> None:
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.RESEND_API_KEY = "test"
    settings.NIGHTLY_ALERT_EMAIL = "owner@example.com"


def _run_check() -> None:
    call_command("check_nightly_update", assessment_date=DAY)


def _failed_weather_run(*, forest: dict[str, str]) -> ScheduledWeatherRun:
    clock = _clock()
    refusing = _Transport(clock=clock, override=lambda query: (400, {}, PROVIDER_MESSAGE.encode()))
    run = _drive(run=_start(forest=forest, clock=clock), clock=clock, transport=refusing)
    assert run.status == ScheduledWeatherRun.Status.FAILED
    return run


def _failed_forecast_run(*, forest: dict[str, str]) -> ScheduledWeatherRun:
    clock = _clock()
    run = _drive(run=_start(forest=forest, clock=clock), clock=clock, transport=_Transport(clock=clock))
    task_id = queue_tuscany_forecast(region_code="09", assessment_date=DAY)
    DBTaskResult.objects.get(id=task_id).set_failed(RuntimeError("The forecast broke on purpose."))
    run.forecast_task_id = task_id
    run.save(update_fields=["forecast_task_id"])
    return run


def test_no_email_when_the_run_is_published(tuscany, email_settings) -> None:
    _publish_day(day=DAY)

    _run_check()

    assert check_nightly_update(assessment_date=DAY).reason == PUBLISHED
    assert mail.outbox == []


def test_email_names_a_missing_weather_run(email_settings) -> None:
    _run_check()

    assert len(mail.outbox) == 1
    assert NO_WEATHER_RUN in mail.outbox[0].subject
    assert str(DAY) in mail.outbox[0].subject
    assert "worker or the cron job is probably down" in mail.outbox[0].body
    assert mail.outbox[0].to == ["owner@example.com"]


def test_email_holds_the_full_provider_message_of_a_failed_weather_run(forest, email_settings) -> None:
    run = _failed_weather_run(forest=forest)

    _run_check()

    assert len(mail.outbox) == 1
    assert WEATHER_FAILED in mail.outbox[0].subject
    assert run.reason_code in mail.outbox[0].body
    assert PROVIDER_MESSAGE in mail.outbox[0].body


def test_email_holds_the_error_of_a_failed_forecast_task(forest, forecast_calls, email_settings) -> None:
    _failed_forecast_run(forest=forest)

    _run_check()

    assert len(mail.outbox) == 1
    assert FORECAST_FAILED in mail.outbox[0].subject
    assert "The forecast broke on purpose." in mail.outbox[0].body


def test_without_a_resend_key_the_message_goes_to_the_log(email_settings, settings, caplog) -> None:
    settings.RESEND_API_KEY = ""

    with caplog.at_level(logging.ERROR):
        _run_check()

    assert mail.outbox == []
    assert "No weather run was started" in caplog.text


def test_retry_reopens_failed_batches_and_sets_the_run_back_to_pending(forest, forecast_calls, email_settings) -> None:
    run = _failed_weather_run(forest=forest)
    assert WeatherFetchBatch.objects.filter(scheduled_run=run, status=WeatherFetchBatch.Status.FAILED).exists()

    call_command("check_nightly_update", "--retry", assessment_date=DAY)

    run.refresh_from_db()
    assert run.status == ScheduledWeatherRun.Status.PENDING
    assert not WeatherFetchBatch.objects.filter(scheduled_run=run, status=WeatherFetchBatch.Status.FAILED).exists()
    assert mail.outbox == []


def test_retry_queues_a_new_forecast_task_after_a_failed_one(forest, forecast_calls) -> None:
    run = _failed_forecast_run(forest=forest)
    old_task_id = run.forecast_task_id

    result = retry_nightly_update(assessment_date=DAY)

    run.refresh_from_db()
    assert result.reason == FORECAST_FAILED
    assert run.forecast_task_id not in {None, old_task_id}
    assert DBTaskResult.objects.get(id=run.forecast_task_id).status == "READY"

    new_task_id = run.forecast_task_id
    assert retry_nightly_update(assessment_date=DAY).reason == NOT_FINISHED
    run.refresh_from_db()
    assert run.forecast_task_id == new_task_id


def test_retry_does_nothing_when_the_run_is_published(tuscany) -> None:
    _publish_day(day=DAY)
    tasks_before = DBTaskResult.objects.count()

    result = retry_nightly_update(assessment_date=DAY)

    assert result.reason == PUBLISHED
    assert DBTaskResult.objects.count() == tasks_before
    assert not ScheduledWeatherRun.objects.exists()


# Several regions in one night


def test_two_regions_are_published_only_when_both_are(tuscany) -> None:
    _publish_day(day=DAY)

    assert check_nightly_update(assessment_date=DAY, region_code="09,12").reason == NO_WEATHER_RUN

    process_forecast_run(run_id=start_forecast_run(assessment_date=DAY, region_code="12").id)

    assert check_nightly_update(assessment_date=DAY, region_code="12,09").reason == PUBLISHED


def test_the_message_names_the_region_that_failed_and_its_reason(tuscany) -> None:
    _publish_day(day=DAY)
    ScheduledWeatherRun.objects.create(
        region_code="09,12",
        assessment_date=DAY,
        forest_build_version=tuscany.forest.version,
        status=ScheduledWeatherRun.Status.COMPLETED,
        mode=ScheduledWeatherRun.Mode.INITIAL,
        reason_code="ready",
    )

    def broken(*, inputs: object) -> object:
        raise ValueError("The Lazio calculation failed on purpose.")

    run = start_forecast_run(assessment_date=DAY, region_code="12")
    process_forecast_run(run_id=run.id, calculator=broken)
    result = check_nightly_update(assessment_date=DAY, region_code="09,12")

    assert result.reason == FORECAST_FAILED
    assert "Regions not published: 12." in result.message
    assert "Region 12 forecast run reason" in result.message
    assert "failed on purpose" in result.message


def test_the_default_regions_come_from_the_setting(tuscany, settings) -> None:
    _publish_day(day=DAY)
    settings.SPORAMETEO_NIGHTLY_REGIONS = "09,12"

    assert check_nightly_update(assessment_date=DAY).reason == NO_WEATHER_RUN

    settings.SPORAMETEO_NIGHTLY_REGIONS = "09"

    assert check_nightly_update(assessment_date=DAY).reason == PUBLISHED
