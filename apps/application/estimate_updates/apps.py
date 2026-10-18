from django.apps import AppConfig


class EstimateUpdatesConfig(AppConfig):
    """Register estimate storage and connect the weather worker hooks."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "estimate_updates"

    def ready(self) -> None:
        """Connect weather callbacks after all applications are loaded."""
        from weather_on_demand.services import (
            configure_cleanup_callback,
            configure_estimate_callback,
            configure_forecast_callback,
        )

        from .services import cleanup_estimate_updates, enqueue_estimate_update
        from .tasks import queue_forecast

        configure_estimate_callback(enqueue_estimate_update)
        configure_cleanup_callback(cleanup_estimate_updates)
        configure_forecast_callback(queue_forecast)
