"""SQL-only execution path used by the scheduled Azure Function."""

from __future__ import annotations

import logging
from time import perf_counter

from pipeline.config import select_country
from pipeline.db import rebuild_dimensions_from_sql
from pipeline.prompts import export as export_prompts
from pipeline.raw_citations import export as export_raw_citations
from pipeline.scores_summarized import export as export_scores

LOGGER = logging.getLogger(__name__)


def run_country(country_slug: str) -> dict[str, object]:
    """Run one country without creating CSVs or other local data files."""
    started = perf_counter()
    country = select_country(country_slug, create_data_dir=False)
    LOGGER.info("Starting SQL-only pipeline for country=%s", country.slug)
    try:
        # Suppress per-export dimension rebuilds so dimensions are refreshed
        # once, after both fact tables contain their latest data.
        scores = export_scores(csv_output=False, rebuild_dimensions=False)
        prompts = export_prompts(csv_output=False)
        citations = export_raw_citations(
            mode="incremental",
            csv_output=False,
            state_source="sql",
            rebuild_dimensions=False,
        )
        # Build shared slicer dimensions from the final scores and citations.
        rebuild_dimensions_from_sql()
    except SystemExit as error:
        raise RuntimeError(str(error)) from error

    result = {
        "country": country.slug,
        "seconds": round(perf_counter() - started, 2),
        "scores": scores,
        "prompts": prompts,
        "citations": citations,
    }
    LOGGER.info(
        "Completed SQL-only pipeline country=%s seconds=%.2f",
        country.slug,
        result["seconds"],
    )
    return result
