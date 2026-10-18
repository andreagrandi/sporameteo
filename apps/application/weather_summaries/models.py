"""Database records for immutable weather days and derived summaries."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import models


class ImmutableRecordMixin:
    """Reject changes to stored weather evidence after its first save."""

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding and type(self).objects.filter(pk=self.pk).exists():
            original = type(self).objects.get(pk=self.pk)
            changed = [
                field.name
                for field in self._meta.concrete_fields
                if field.name != "id" and getattr(original, field.name) != getattr(self, field.name)
            ]
            if changed:
                raise ValueError(f"{type(self).__name__} records are immutable: {changed[0]}")
        super().save(*args, **kwargs)


class WeatherDaily(ImmutableRecordMixin, models.Model):
    """Store one provider attempt's values for one cell and Italian date."""

    class Status(models.TextChoices):
        COMPLETE = "complete", "Complete"
        CHECKED_KNOWN_MISSING = "checked_known_missing", "Checked known missing"

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    cell_key = models.CharField(max_length=120)
    date = models.DateField()
    source_provider = models.CharField(max_length=120)
    source_model = models.CharField(max_length=120)
    source_endpoint = models.URLField(max_length=600)
    source_id = models.CharField(max_length=240)
    request_id = models.CharField(max_length=240)
    attempt_id = models.UUIDField()
    retrieved_at = models.DateTimeField()
    forecast_issue_time = models.DateTimeField(null=True, blank=True)
    publication_time = models.DateTimeField(null=True, blank=True)
    requested_latitude = models.FloatField()
    requested_longitude = models.FloatField()
    returned_latitude = models.FloatField()
    returned_longitude = models.FloatField()
    reference_elevation_m = models.FloatField(null=True, blank=True)
    timezone = models.CharField(max_length=80, default="Europe/Rome")
    rain_mm = models.FloatField(null=True, blank=True)
    mean_temperature_c = models.FloatField(null=True, blank=True)
    min_temperature_c = models.FloatField(null=True, blank=True)
    et0_mm = models.FloatField(null=True, blank=True)
    mean_soil_temperature_c = models.FloatField(null=True, blank=True)
    mean_soil_moisture = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=40, choices=Status.choices)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["attempt_id", "cell_key", "date"],
                name="weather_daily_attempt_cell_date_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["cell_key", "date"], name="weather_daily_cell_date_idx"),
        ]

    @property
    def source_attempt_id(self):
        """Return the attempt UUID under the explicit provenance name."""
        return self.attempt_id

    @property
    def provider(self):
        """Return the provider name under the contract-facing name."""
        return self.source_provider

    @property
    def attempt_uuid(self):
        """Return the attempt UUID under the contract-facing name."""
        return self.attempt_id


class WeatherSnapshot(ImmutableRecordMixin, models.Model):
    """Freeze the 38 daily rows used by one assessment."""

    class Status(models.TextChoices):
        READY = "ready", "Ready"

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    cell_key = models.CharField(max_length=120)
    pipeline_version = models.PositiveSmallIntegerField(default=1)
    assessment_date = models.DateField()
    reference_elevation_m = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    latest_retrieval = models.DateTimeField()
    source_ids = models.JSONField(default=list)
    source_attempt_ids = models.JSONField(default=list)
    metadata = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.READY)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cell_key", "assessment_date", "pipeline_version"],
                name="weather_snapshot_cell_assessment_unique",
            ),
        ]


class WeatherSnapshotDay(models.Model):
    """Protect each daily input referenced by a ready snapshot."""

    id = models.BigAutoField(primary_key=True)
    snapshot = models.ForeignKey(
        WeatherSnapshot,
        on_delete=models.CASCADE,
        related_name="days",
    )
    date = models.DateField()
    daily = models.ForeignKey(
        WeatherDaily,
        on_delete=models.PROTECT,
        related_name="snapshot_days",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["snapshot", "date"],
                name="weather_snapshot_day_snapshot_date_unique",
            ),
        ]


class WeatherSummary(ImmutableRecordMixin, models.Model):
    """Store visitor and method windows for one frozen target date."""

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    snapshot = models.ForeignKey(
        WeatherSnapshot,
        on_delete=models.CASCADE,
        related_name="summaries",
    )
    target_date = models.DateField()
    days_ahead = models.PositiveSmallIntegerField()
    visitor_rain_7_mm = models.FloatField(null=True, blank=True)
    visitor_rain_14_mm = models.FloatField(null=True, blank=True)
    visitor_rain_21_mm = models.FloatField(null=True, blank=True)
    old_rain_mm = models.FloatField(null=True, blank=True)
    recent_rain_mm = models.FloatField(null=True, blank=True)
    target_rain_mm = models.FloatField(null=True, blank=True)
    target_mean_temperature_c = models.FloatField(null=True, blank=True)
    target_min_temperature_c = models.FloatField(null=True, blank=True)
    target_et0_mm = models.FloatField(null=True, blank=True)
    target_mean_soil_temperature_c = models.FloatField(null=True, blank=True)
    target_mean_soil_moisture = models.FloatField(null=True, blank=True)
    frost = models.BooleanField(null=True, blank=True)
    hail = models.BooleanField(null=True, blank=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["snapshot", "target_date"],
                name="weather_summary_snapshot_target_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(days_ahead__gte=0) & models.Q(days_ahead__lte=7),
                name="weather_summary_days_ahead_range",
            ),
        ]


class FileWeatherRead(models.Model):
    """Record one read of the Open-Meteo files for one assessment date."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    assessment_date = models.DateField()
    region_code = models.CharField(max_length=100)
    run_time = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    bytes_read = models.BigIntegerField(default=0)
    http_requests = models.IntegerField(default=0)
    cells_total = models.IntegerField(default=0)
    cells_written = models.IntegerField(default=0)
    cells_already_ready = models.IntegerField(default=0)
    missing = models.JSONField(default=dict)
    file_modified = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    error = models.TextField(blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["assessment_date", "started_at"], name="file_weather_read_date_idx"),
        ]
