"""Check today's forecast runs, retry it, or email the owner when it is missing."""

import logging
from argparse import ArgumentParser
from datetime import date

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from estimate_updates.nightly_check import check_nightly_update, retry_nightly_update
from weather_on_demand.services import italian_assessment_date

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Retry the stopped nightly step with --retry. Without it, email the owner if the run is not published."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--retry", action="store_true", help="Retry the step that stopped and send no email.")
        parser.add_argument(
            "--assessment-date",
            type=date.fromisoformat,
            default=None,
            help="Italian date to check, default today in Europe/Rome.",
        )
        parser.add_argument(
            "--region-code",
            default=None,
            help="Region codes such as 09,08, or all. Default from SPORAMETEO_NIGHTLY_REGIONS.",
        )

    def handle(self, *args, **options) -> None:
        assessment_date = options["assessment_date"] or italian_assessment_date()
        region_code = options["region_code"]
        if options["retry"]:
            result = retry_nightly_update(assessment_date=assessment_date, region_code=region_code)
            self.stdout.write(f"Retry for {assessment_date}: {result.reason}")
            return
        result = check_nightly_update(assessment_date=assessment_date, region_code=region_code)
        if result.is_published:
            self.stdout.write(f"The {assessment_date} forecast run is published.")
            return
        body = (
            f"{result.message}\n\n"
            "The retry passes did not fix it. Check the worker and the logs.\n"
        )
        if not settings.RESEND_API_KEY or not settings.NIGHTLY_ALERT_EMAIL:
            logger.error("Nightly update not published (%s). No email sent: %s", result.reason, result.message)
            self.stdout.write(f"Not published ({result.reason}). Email is not configured, the message is in the log.")
            return
        send_mail(
            subject=f"SporaMeteo nightly update missing for {assessment_date}: {result.reason}",
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[settings.NIGHTLY_ALERT_EMAIL],
        )
        self.stdout.write(f"Not published ({result.reason}). Email sent.")
