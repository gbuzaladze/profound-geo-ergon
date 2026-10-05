# Novartis GEO Pipeline (Canada, Mexico, Brazil)

Reusable pipeline: `project.toml` lists one Profound category per country and
exports the same tables into a folder for each market:

- `data/{country}/fact_scores_summarized` — daily visibility, share of voice, average position, and positive sentiment
- `data/{country}/fact_raw_citations` — one row per citation from Visibility answers
- `data/{country}/dim_prompt` — active Visibility prompts in the category
- `data/{country}/dim_date`, `dim_topic`, `dim_platform` — distinct keys from both facts

## Requirements

- Python 3.12
- A Profound Enterprise API key with REST API access

## Setup

Create `.env` from `.env.example`, set `PROFOUND_API_KEY`, fill in the
`AZURE_SQL_*` values, and install the locked dependencies:

```powershell
uv sync
```

Shared settings live under `[project]` in `project.toml` (`owned_asset`,
`owned_aliases`, `owned_citation_hosts`, `owned_citation_contains`,
`start_date`). Each market is a
`[[countries]]` table with `slug`, `name`, and `category_id`. Set
`owned_asset` on a country when Profound uses a local brand name
(`Novartis - Mexico`). `owned_aliases` are marked `is_owned` and pulled only
when that category tracks the exact name. The country's `owned_asset` is
always pulled. The API key stays in `.env`. Azure SQL credentials stay there
too; the pipeline writes CSVs under `data/{slug}/` and replaces the same
tables in schema `{slug}` (`canada`, `mexico`, `brazil`).

## Scheduled Azure Function

The Azure Durable Function is a separate SQL-only adapter around the same
pipeline logic. It never creates data CSVs. Each daily run:

1. refreshes the complete scores and prompt tables;
2. reads the latest citation date from Azure SQL;
3. pulls citations from that date through yesterday;
4. combines the new window with older SQL rows;
5. atomically replaces the complete citation table; and
6. rebuilds dimensions from both SQL fact tables.

The timer checks hourly and starts one deterministic run per Toronto calendar
day after 6:00 AM. This keeps the schedule correct across daylight-saving
changes and catches up after a temporary outage. Countries run sequentially
to respect the shared Profound API quota.

The existing CLI commands are unchanged and continue writing CSVs. Azure
Functions uses managed identity for SQL; local CLI runs can continue using
`AZURE_SQL_USERNAME` and `AZURE_SQL_PASSWORD`.

Before the first cloud run, load complete citation history into every country
schema using the CLI. The Function deliberately refuses to increment an empty
citation table.

Deployment assets are in `infra/` and use Azure Developer CLI:

```powershell
azd auth login
azd env new production
azd env set AZURE_LOCATION canadacentral
azd env set AZURE_SQL_SERVER your-server.database.windows.net
azd env set AZURE_SQL_DATABASE your-database
azd provision
```

After provisioning, add `PROFOUND_API_KEY` as the `profound-api-key` secret in
the provisioned Key Vault. In Azure SQL, create a contained Entra user for the
provisioned Function identity and grant it read, write, and required schema DDL
permissions. Then validate and deploy with `azd deploy`. Do not place the API
key or SQL password in Bicep parameters or Function App settings.

## Reuse for another project

1. Copy this folder (or clone it).
2. Edit `project.toml` — at minimum `owned_asset`, `start_date`, and one
   `[[countries]]` entry. Add `owned_aliases` for extra product names that
   should be marked `is_owned` when that category tracks the exact name.
   Add `owned_citation_hosts` for hostnames or domains whose citation
   `category` should be `Owned`. Add `owned_citation_contains` for text that
   should match any hostname or domain, such as `novartis` for
   `novartis.com.br` and `novartis.com.mx`.
3. Set `PROFOUND_API_KEY` and the `AZURE_SQL_*` values in `.env`.
   Allow your client IP on the Azure SQL firewall.
4. Delete `data/` if it still has exports from a previous project.
5. Run a full export (`python -m pipeline`). `fact_raw_citations` starts
   from `start_date` when no CSV exists yet in that country's folder.

Both exports scan through yesterday. Today's date is never requested or kept,
because Profound data for the pull day can still be incomplete. After the
first `fact_raw_citations` backfill, daily runs only refresh new dates.
Dimension CSVs are rebuilt after each fact export.

## Export all countries

Daily run. Each country writes into `data/{slug}/` and replaces the same
tables in Azure SQL schema `{slug}`. `fact_raw_citations` is incremental
per country if that folder already has a CSV.

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"
.\.venv\Scripts\python.exe -m pipeline
```

Fresh citations backfill from `START_DATE` (not incremental):

```powershell
.\.venv\Scripts\python.exe -m pipeline --full --fresh
```

One market only:

```powershell
.\.venv\Scripts\python.exe -m pipeline --country canada
.\.venv\Scripts\python.exe -m pipeline --country mexico --country brazil
```

```text
data/canada/fact_scores_summarized.csv
data/canada/fact_raw_citations.csv
data/canada/dim_prompt.csv
data/canada/dim_date.csv
data/canada/dim_topic.csv
data/canada/dim_platform.csv
data/mexico/...
data/brazil/...

Azure SQL schemas canada / mexico / brazil
  fact_scores_summarized
  fact_raw_citations
  dim_prompt
  dim_date
  dim_topic
  dim_platform
```

CSV-only (skip Azure SQL):

```powershell
.\.venv\Scripts\python.exe -m pipeline --skip-db
```

Load existing CSVs into Azure SQL without calling Profound. Creates the
country schemas and tables if they do not exist, then replaces each table
from `data/{slug}/*.csv`. Citation files are large; the first load can take
several minutes per country.

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"
.\.venv\Scripts\python.exe -m pipeline.db
.\.venv\Scripts\python.exe -m pipeline.db --country canada
.\.venv\Scripts\python.exe -m pipeline.db --ensure-only
```

Relate both facts many-to-one to `dim_date[date]`, `dim_topic[topic]`, and
`dim_platform[platform]`. Put slicers on the dim tables so one filter applies
to both facts.

## fact_scores_summarized

Pulls the Visibility Summarized view with **topic** included (date x topic x
platform x asset). Ranking uses the last `LOOKBACK_DAYS` (default 30) to pick,
in **each topic**, the top `TOP_N` brands by visibility, by share of voice,
by average position (lower is better), and by positive sentiment, then unions
those lists. Sentiment ranking uses tracked category brands (v2 sentiment is
per-brand). Time series are pulled for the combined set from `START_DATE`
through yesterday. The country's `owned_asset` is always included, even
outside every top N. An `owned_aliases` name is included only when this
category tracks that exact asset, and those names are marked `is_owned`
even when Profound tracks them as separate unowned assets.

Columns: `date`, `topic`, `platform`, `asset`, `is_owned`, `visibility`,
`share_of_voice`, `average_position`, `positive_sentiment`.

After the pull, every selected asset is zero-filled for every observed
`date` × `topic` × `platform` (`visibility` and `share_of_voice` = 0 when
absent). `average_position` and `positive_sentiment` stay blank when missing.
Positive sentiment is merged from a second v2 sentiment pull for category
brands and stored as a 0–1 share (same scale as visibility). Other brands in
the visibility union stay blank.

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pipeline.scores_summarized
.\.venv\Scripts\python.exe -m pipeline.scores_summarized --country mexico
```

## dim_prompt

Lists active Visibility prompts in the category (text, topic, tags, regions,
platforms).

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pipeline.prompts
```

## fact_raw_citations

Pages through V2 Visibility answers and flattens citations to the Citations UI
columns.

Columns: `date`, `topic`, `platform`, `category`, `subcategory`, `pag`,
`mentioned`, `url`, `hostname`, `domain`, `path`, `author`, `tags`, `region`.
`category` is a display label (`earned_media` → `Earned Media`;
`earned_institutions` → `Institutions`). Hostnames or domains listed in
`owned_citation_hosts`, or containing a string in `owned_citation_contains`,
are forced to `Owned` regardless of the API category. When
`citation-domains/citation-domains-{slug}.csv` exists, a matching `domain` overwrites that
category and sets `subcategory` and `pag` (`TRUE` or `FALSE`). The file
category is stored with Profound's labels (`Institution` becomes
`Institutions`). A domain that
is not in the file keeps the Profound category, with `subcategory` and `pag`
blank. Canada and Mexico use the same columns; their files are added when
those lists exist. `domain` is the
registrable domain from `hostname` via the Public Suffix List (e.g.
`us.kisqali.com` → `kisqali.com`, `bccancer.bc.ca` → `bccancer.bc.ca`).

`author` is filled for Reddit, YouTube, and Instagram citation URLs:

- Reddit: `r/subreddit` from `/r/{subreddit}/...`
- YouTube: `@handle` from `/@`, `/c/`, or `/user/` paths; video URLs
  (`watch`, `shorts`, `youtu.be`) use YouTube oEmbed for the channel
- Instagram: `@handle` from profile URLs and `/{user}/p/` or `/{user}/reel/`
  paths. `/p/{shortcode}/` and `/reel/{shortcode}/` use Instaloader (same
  approach as the portable Instagram enricher). Optional
  `INSTAGRAM_SESSION_FILE` plus `INSTAGRAM_LOGIN_USERNAME` enable the
  authenticated fallback when anonymous requests are blocked.

Live Instagram and YouTube lookups run for rows still missing `author` after
URL parsing. An incremental pull looks up only the refreshed dates. A
shortcode or video resolved in that window is copied onto older rows that
cite the same URL. `--enrich-authors` looks up the whole file. Lookups are
cached by shortcode/video id within a run, so incremental SQL rows that
already have `author` are left alone. Set
`CITATION_AUTHOR_LIVE_LOOKUP=0` to skip network lookups, or
`CITATION_AUTHOR_INSTAGRAM_LIVE_LOOKUP=0` to skip only Instaloader shortcode
lookups. After three consecutive Instagram failures the job cools down
(`CITATION_AUTHOR_INSTAGRAM_COOLDOWN_SECONDS`, default 300) and continues, so
a temporary block does not abandon the rest of the shortcodes.

Recompute authors on an existing CSV without a Profound pull:

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"
.\.venv\Scripts\python.exe -m pipeline.raw_citations --enrich-authors --country mexico
.\.venv\Scripts\python.exe -m pipeline.raw_citations --enrich-authors --country brazil
```

### Original pull

Backfill from `START_DATE` through yesterday. If the job would exceed
`HOURLY_API_LIMIT` (600), it sleeps until a slot opens, then continues. A
checkpoint is saved after each page so an interrupted run can resume.

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"
.\.venv\Scripts\python.exe -m pipeline.raw_citations --full
.\.venv\Scripts\python.exe -m pipeline.raw_citations --full --country brazil
```

Restart a failed backfill with the same command. To discard the checkpoint
and start over:

```powershell
.\.venv\Scripts\python.exe -m pipeline.raw_citations --full --fresh --country canada
```

### Incremental pull

Re-pulls from the last date already in `data/{country}/fact_raw_citations.csv`
through yesterday, replaces that date window, and keeps older rows. This is
the default when the CSV already exists.

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"
.\.venv\Scripts\python.exe -m pipeline.raw_citations --incremental
```

## References

- [Profound REST API](https://docs.tryprofound.com/rest-api/introduction)
- [Profound Python SDK](https://pypi.org/project/profound/)
