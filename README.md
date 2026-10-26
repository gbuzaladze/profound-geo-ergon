# Ergon GEO Pipeline

Reusable pipeline: `project.toml` lists one Profound category per entry and
exports the same tables into a folder for each category. Ergon markets are
the `country` column. `region` is the screenshot grouping (APAC, Americas,
Europe) and sits immediately after `country`.

- `data/{slug}/fact_scores_summarized` — daily visibility, share of voice, average position, and positive sentiment, by country and region
- `data/{slug}/fact_raw_citations` — one row per citation from Visibility answers
- `data/{slug}/dim_prompt` — active Visibility prompts in the category
- `data/{slug}/dim_date`, `dim_topic`, `dim_platform`, `dim_country`, `dim_region` — distinct keys from both facts

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
`owned_asset` on an entry when Profound uses a different brand name.
`owned_aliases` are marked `is_owned` and pulled only
when that category tracks the exact name. The entry's `owned_asset` is
always pulled. The API key stays in `.env`. Azure SQL credentials stay there
too; the pipeline writes CSVs under `data/{slug}/` and replaces the same
tables in schema `{slug}` (`americas-uk-au-uae`, `europe-asia`).

## Reuse for another project

1. Copy this folder (or clone it).
2. Edit `project.toml` — at minimum `owned_asset`, `start_date`, and one
   `[[countries]]` entry. Add `owned_aliases` for extra product names that
   should be marked `is_owned` when that category tracks the exact name.
   Add `owned_citation_hosts` for hostnames or domains whose citation
   `category` should be `Owned`. Add `owned_citation_contains` for text that
   should match any hostname or domain, such as `ergon` for
   `ergon.com`.
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
.\.venv\Scripts\python.exe -m pipeline --country americas-uk-au-uae
.\.venv\Scripts\python.exe -m pipeline --country europe-asia
```

```text
data/fact_scores_summarized.csv
data/fact_raw_citations.csv
data/dim_prompt.csv
data/dim_date.csv
data/dim_topic.csv
data/dim_platform.csv
data/dim_country.csv
data/dim_region.csv
```

Category folders are removed after the two Profound categories are concatenated.
Country is the Profound market. Region is APAC, Americas, or Europe.

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
.\.venv\Scripts\python.exe -m pipeline.db --country americas-uk-au-uae
.\.venv\Scripts\python.exe -m pipeline.db --ensure-only
```

Relate both facts many-to-one to `dim_date[date]`, `dim_topic[topic]`,
`dim_platform[platform]`, `dim_country[country]`, and `dim_region[region]`.
Put slicers on the dim tables so one filter applies to both facts.

## fact_scores_summarized

Pulls the Visibility Summarized view with **topic** and Profound's **region**
(stored as country) included
(date x country x region x topic x platform x asset). Ranking uses the last `LOOKBACK_DAYS` (default 30) to pick,
in **each topic**, the top `TOP_N` brands by visibility, by share of voice,
by average position (lower is better), and by positive sentiment, then unions
those lists. Sentiment ranking uses tracked category brands (v2 sentiment is
per-brand). Time series are pulled for the combined set from `START_DATE`
through yesterday. The country's `owned_asset` is always included, even
outside every top N. An `owned_aliases` name is included only when this
category tracks that exact asset, and those names are marked `is_owned`
even when Profound tracks them as separate unowned assets.

Columns: `date`, `country`, `region`, `topic`, `platform`, `asset`, `is_owned`,
`visibility`, `share_of_voice`, `average_position`, `positive_sentiment`.
`region` is APAC, Americas, or Europe from `[regions]` in `project.toml`.

After the pull, every selected asset is zero-filled for every observed
`date` × `country` × `topic` × `platform` (`visibility` and `share_of_voice` = 0 when
absent). `average_position` and `positive_sentiment` stay blank when missing.
Positive sentiment is merged from a second v2 sentiment pull for category
brands and stored as a 0–1 share (same scale as visibility). Other brands in
the visibility union stay blank.

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pipeline.scores_summarized
.\.venv\Scripts\python.exe -m pipeline.scores_summarized --country europe-asia
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

Columns: `date`, `topic`, `platform`, `category`, `mentioned`, `url`,
`hostname`, `domain`, `path`, `author`, `prompt`, `tags`, `country`, `region`.
`prompt` is the Visibility question and sits immediately before `tags`.
Rows already stored before `prompt` was added stay blank until that date
is pulled again. A `--through-latest` citations run re-pulls `START_DATE`
through the latest date already in `dbo.fact_raw_citations` for that
category, so the scan does not extend to yesterday.
`category` is a display label (`earned_media` → `Earned Media`;
`earned_institutions` → `Institutions`). Hostnames or domains listed in
`owned_citation_hosts`, or containing a string in `owned_citation_contains`,
are forced to `Owned` regardless of the API category. When
`citation-domains/citation-domains-{slug}.csv` exists, a matching `domain` overwrites that
category. The file category is stored with Profound's labels (`Institution`
becomes `Institutions`). A domain that is not in the file keeps the Profound
category. `domain` is the
registrable domain from `hostname` via the Public Suffix List (e.g.
`www.ergon.com` → `ergon.com`).

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
a temporary block does not abandon the rest of the shortcodes. Set
`CITATION_AUTHOR_INSTAGRAM_MAX_SECONDS` to cap Instaloader time per country,
cooldowns included; remaining shortcodes keep a blank `author`. The default
`0` means no limit.

Recompute authors on an existing CSV without a Profound pull:

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"
.\.venv\Scripts\python.exe -m pipeline.raw_citations --enrich-authors --country americas-uk-au-uae
.\.venv\Scripts\python.exe -m pipeline.raw_citations --enrich-authors --country europe-asia
```

### Original pull

Backfill from `START_DATE` through yesterday. If the job would exceed
`HOURLY_API_LIMIT` (600), it sleeps until a slot opens, then continues. A
checkpoint is saved after each page so an interrupted run can resume.

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"
.\.venv\Scripts\python.exe -m pipeline.raw_citations --full
.\.venv\Scripts\python.exe -m pipeline.raw_citations --full --country europe-asia
```

Restart a failed backfill with the same command. To discard the checkpoint
and start over:

```powershell
.\.venv\Scripts\python.exe -m pipeline.raw_citations --full --fresh --country americas-uk-au-uae
```

Re-pull the dates already stored, including `prompt`, without adding days
through yesterday:

```powershell
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"
.\.venv\Scripts\python.exe -m pipeline.raw_citations --through-latest
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
