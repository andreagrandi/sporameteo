from __future__ import annotations

import uuid
from datetime import date

from django.db import models
from django.db.models import Q

from weather_requests.models import WeatherRequest
from weather_summaries.models import WeatherSnapshot


class ScheduledWeatherRun(models.Model):
    """Track one resumable Tuscany weather preparation for one Italian date."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class Mode(models.TextChoices):
        INITIAL = "initial", "Initial"
        REFRESH = "refresh", "Refresh"

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    region_code = models.CharField(max_length=100)
    assessment_date = models.DateField()
    forest_build_version = models.CharField(max_length=120)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    mode = models.CharField(max_length=20, choices=Mode.choices)
    planned_counted_calls = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    planned_http_requests = models.PositiveIntegerField(default=0)
    provider_attempts = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    next_start_at = models.DateTimeField(null=True, blank=True)
    task_id = models.UUIDField(null=True, blank=True)
    forecast_task_id = models.UUIDField(null=True, blank=True)
    file_read = models.ForeignKey(
        "weather_summaries.FileWeatherRead",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="scheduled_runs",
    )
    reason_code = models.CharField(max_length=80, blank=True, default="")
    full_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-assessment_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["region_code", "assessment_date"],
                name="scheduled_weather_run_region_date_unique",
            ),
        ]


class WeatherFetchBatch(models.Model):
    """Group compatible cell fetches into one provider request."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    logical_key = models.CharField(max_length=255, unique=True)
    assessment_date = models.DateField()
    request = models.ForeignKey(
        WeatherRequest,
        on_delete=models.PROTECT,
        related_name="on_demand_batches",
    )
    canonical_spec = models.JSONField(default=dict)
    scheduled_run = models.ForeignKey(
        ScheduledWeatherRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="batches",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    task_id = models.UUIDField(null=True, blank=True, unique=True)
    retry_at = models.DateTimeField(null=True, blank=True)
    reason_code = models.CharField(max_length=80, blank=True, default="")
    full_reason = models.TextField(blank=True, default="")
    first_requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["first_requested_at", "id"]
        indexes = [
            models.Index(fields=["status", "retry_at"]),
            models.Index(fields=["assessment_date", "status"]),
        ]


class WeatherFetch(models.Model):
    """Track one cell and Italian assessment date until its snapshot is ready."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    cell_key = models.CharField(max_length=120)
    pipeline_version = models.PositiveSmallIntegerField(default=1)
    assessment_date = models.DateField()
    batch = models.ForeignKey(
        WeatherFetchBatch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fetches",
    )
    request = models.ForeignKey(
        WeatherRequest,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="on_demand_fetches",
    )
    previous_snapshot = models.ForeignKey(
        WeatherSnapshot,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="next_weather_fetches",
    )
    snapshot = models.ForeignKey(
        WeatherSnapshot,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="weather_fetches",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    retry_at = models.DateTimeField(null=True, blank=True)
    reason_code = models.CharField(max_length=80, blank=True, default="fetch_pending")
    full_reason = models.TextField(blank=True, default="")
    visitor_hash = models.CharField(max_length=64, blank=True, default="")
    first_requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    estimate_callback_status = models.CharField(
        max_length=20,
        default="pending",
    )
    estimate_callback_retry_at = models.DateTimeField(null=True, blank=True)
    estimate_callback_started_at = models.DateTimeField(null=True, blank=True)
    estimate_callback_attempts = models.PositiveIntegerField(default=0)
    estimate_callback_reason = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["assessment_date", "cell_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["cell_key", "assessment_date", "pipeline_version"],
                name="weather_fetch_cell_assessment_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["assessment_date", "status"]),
            models.Index(fields=["cell_key", "assessment_date"]),
        ]

    @property
    def task_id(self):
        """Return the queue task identifier for this fetch batch."""
        return self.batch.task_id if self.batch_id else None

    @property
    def snapshot_ref(self):
        """Return the ready snapshot referenced by this fetch."""
        return self.snapshot

    @property
    def full_message(self) -> str:
        """Return the complete durable reason for this fetch."""
        return self.full_reason

    @property
    def request_batch(self):
        """Return the durable request batch for this fetch."""
        return self.batch


class WeatherFetchPart(models.Model):
    """Persist one bounded provider date range for one weather fetch."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    fetch = models.ForeignKey(
        WeatherFetch,
        on_delete=models.CASCADE,
        related_name="parts",
    )
    batch = models.ForeignKey(
        WeatherFetchBatch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="parts",
    )
    request = models.ForeignKey(
        WeatherRequest,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="on_demand_fetch_parts",
    )
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    retry_at = models.DateTimeField(null=True, blank=True)
    daily_ids = models.JSONField(default=list)
    source_ids = models.JSONField(default=list)
    source_attempt_ids = models.JSONField(default=list)
    reason_code = models.CharField(max_length=80, blank=True, default="fetch_pending")
    full_reason = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_date", "end_date", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["fetch", "start_date", "end_date"],
                name="weather_fetch_part_fetch_dates_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "retry_at"]),
            models.Index(fields=["batch", "status"]),
        ]

    @property
    def task_id(self):
        """Return the queue task identifier for the linked provider batch."""
        return self.batch.task_id if self.batch_id else None

    @property
    def snapshot_ref(self):
        """Return the final snapshot held by the parent fetch."""
        return self.fetch.snapshot_ref


class WeatherVisitorWindow(models.Model):
    """Count newly requested cells for one opaque visitor hash."""

    visitor_hash = models.CharField(max_length=64, primary_key=True)
    window_started_at = models.DateTimeField()
    new_cell_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(new_cell_count__gte=0),
                name="weather_visitor_new_cell_count_nonnegative",
            ),
        ]


class WeatherVisitorReservation(models.Model):
    """Record one newly triggered cell and date for rolling visitor limits."""

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    visitor_hash = models.CharField(max_length=64)
    cell_key = models.CharField(max_length=120)
    assessment_date = models.DateField()
    reserved_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=["visitor_hash", "reserved_at"]),
        ]


class WeatherCleanupJob(models.Model):
    """Record one idempotent date-keyed weather cleanup task."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"

    id = models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True)
    cleanup_date = models.DateField(unique=True)
    logical_key = models.CharField(max_length=255, unique=True)
    task_id = models.UUIDField(null=True, blank=True, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    retry_at = models.DateTimeField(null=True, blank=True)
    protected_snapshot_ids = models.JSONField(default=list)
    deleted_daily_count = models.PositiveIntegerField(default=0)
    deleted_snapshot_count = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)
    full_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-cleanup_date"]


WeatherFetchRecord = WeatherFetch
WeatherRequestBatch = WeatherFetchBatch
