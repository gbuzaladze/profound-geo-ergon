"""Scheduled Durable Functions entry points for the GEO pipeline."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import azure.durable_functions as df
import azure.functions as func

from pipeline.cloud import run_country
from pipeline.config import COUNTRIES

LOGGER = logging.getLogger(__name__)
TORONTO = ZoneInfo("America/Toronto")
SCHEDULE_HOUR = 6
# Novartis runs on Monday; Tuesday keeps the two pipelines off the same day.
SCHEDULE_WEEKDAY = 1

app = df.DFApp()


def scheduled_instance_id(now: datetime) -> str | None:
    """Return this Tuesday's run ID after 6 AM Toronto."""
    local_now = now.astimezone(TORONTO)
    # Tuesday is weekday 1. Other days never start a run, even if the timer fires.
    if local_now.weekday() != SCHEDULE_WEEKDAY or local_now.hour < SCHEDULE_HOUR:
        return None
    # Durable Functions rejects a duplicate ID, so hourly Tuesday checks still
    # produce one run for that Toronto date.
    return f"geo-weekly-{local_now.date().isoformat()}"


@app.timer_trigger(
    schedule="%PIPELINE_TIMER_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
@app.durable_client_input(client_name="client")
async def scheduled_pipeline(
    timer: func.TimerRequest, client: df.DurableOrchestrationClient
) -> None:
    """Start at most one orchestration each Tuesday in Toronto."""
    instance_id = scheduled_instance_id(datetime.now(TORONTO))
    if instance_id is None:
        return
    existing = await client.get_status(instance_id)
    if existing is not None:
        LOGGER.info(
            "Weekly pipeline already exists instance_id=%s status=%s",
            instance_id,
            existing.runtime_status,
        )
        return
    countries = [country.slug for country in COUNTRIES]
    await client.start_new(
        "pipeline_orchestrator",
        instance_id=instance_id,
        client_input={"countries": countries},
    )
    LOGGER.info(
        "Started weekly pipeline instance_id=%s countries=%s past_due=%s",
        instance_id,
        countries,
        timer.past_due,
    )


@app.orchestration_trigger(context_name="context")
def pipeline_orchestrator(
    context: df.DurableOrchestrationContext,
):
    """Run countries sequentially so they share one Profound API quota."""
    payload = context.get_input() or {}
    countries = payload.get("countries") or []
    # Retry a failed country hourly, matching the API quota recovery window.
    # The fixed delay avoids rapid retries after a Profound rate-limit response.
    retry = df.RetryOptions(
        first_retry_interval_in_milliseconds=3_600_000,
        max_number_of_attempts=3,
        backoff_coefficient=1.0,
        max_retry_interval_in_milliseconds=3_600_000,
    )
    results = []
    for country_slug in countries:
        context.set_custom_status({"country": country_slug, "state": "running"})
        result = yield context.call_activity_with_retry(
            "run_country_activity", retry, country_slug
        )
        results.append(result)
    context.set_custom_status({"state": "completed", "countries": len(results)})
    return results


@app.activity_trigger(input_name="country_slug")
def run_country_activity(country_slug: str) -> dict[str, object]:
    """Execute one SQL-only country pipeline activity."""
    return run_country(country_slug)
