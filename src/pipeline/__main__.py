"""Run scores, prompts, and citations for one or more countries.

Default: `python -m pipeline`. Scores always refresh START_DATE through
yesterday. Citations are incremental when a CSV already exists, unless
`--full` or `--incremental` is passed. Each export also replaces Azure SQL
unless `--skip-db` is set.
"""

from __future__ import annotations

import argparse

from pipeline.combine import combine_category_exports
from pipeline.config import add_country_option, countries_from_args, select_country
from pipeline.db import set_skip_sql
from pipeline.prompts import export as export_prompts
from pipeline.raw_citations import export as export_raw_citations
from pipeline.scores_summarized import export as export_scores_summarized


def main(argv: list[str] | None = None) -> None:
    """Export fact tables and prompts for each selected country."""
    parser = argparse.ArgumentParser(
        description="Export Profound scores, prompts, and citations."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--full",
        action="store_true",
        help="Full citations backfill from START_DATE (not incremental).",
    )
    mode.add_argument(
        "--incremental",
        action="store_true",
        help="Refresh citations from the last date already in the CSV.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="With --full, ignore any citations checkpoint and start over.",
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="Write CSVs only; do not replace Azure SQL tables.",
    )
    add_country_option(parser)
    args = parser.parse_args(argv)
    if args.fresh and not args.full:
        raise SystemExit("--fresh can only be used with --full.")
    set_skip_sql(args.skip_db)
    # None lets raw_citations.export choose incremental vs full from whether the CSV exists.
    citations_mode = None
    if args.full:
        citations_mode = "full"
    elif args.incremental:
        citations_mode = "incremental"
    for country in countries_from_args(args):
        select_country(country.slug)
        print(f"=== {country.name} ({country.slug}) ===", flush=True)
        # Scores first so dim rebuilds after citations still see the latest cube.
        export_scores_summarized()
        export_prompts()
        export_raw_citations(mode=citations_mode, fresh=args.fresh)
    # Category folders are intermediate. Country stays on the region column.
    combine_category_exports()


if __name__ == "__main__":
    main()
