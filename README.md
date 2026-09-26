# mang-arr

Sonarr for manga: add a series once, get every chapter, keep getting new ones.

It sits on top of [Suwayomi](https://github.com/Suwayomi/Suwayomi-Server)
(which does the actual downloading from ~hundreds of sources) and
[Komga](https://komga.org) (which serves the library), and adds the layer both
lack: knowing what a series *is* and what it *should contain*.

## Why this exists

Every off-the-shelf "manga *arr" fails on a real library for the same reasons:

| Problem | What goes wrong |
|---|---|
| Sources name series differently | "Hitting Rewind with You" matches a different series on MangaDex; only the romaji title *Okuremashite Seishun* finds it everywhere |
| Fuzzy matching picks look-alikes | "Let's Play" → *Let's Play a Bunch!* (2 ch); "Gekkan Shoujo Nozaki-kun" → the *Anthology* |
| One source per series | Coverage is split: Weeb Central has 1-9, Manganato 10-18, MangaDex 19-20 |
| Series-level tracking | Gaps hide in the middle (bonus .5 chapters); "complete" means "everything the chosen source had" |
| Existing tools track volumes | A library of `Chapter N.cbz` files reads as 0% owned |

## Features

- **Identity from AniList (MangaDex as fallback).** A series is a database
  record with every title it is known by, not a string you typed. Ambiguous
  titles are shown as candidates, never guessed; series no database has can
  be added by exact title (`--manual`). Status, chapter count and synonyms
  are re-read from the provider on every refresh.
- **Strict matching on every source.** Every Suwayomi source is searched with
  every known title; a hit is accepted only when its title equals one of them
  after normalisation. Anthologies, promos and spin-offs are rejected and
  listed, so you can see why.
- **Per-chapter tracking.** The wanted list is the union of chapter numbers
  across every accepted source minus what is on disk. A series split across
  three sites is still complete. Sources whose chapter count is far off are
  distrusted; fractional "chapters" that are a two-page notice are dropped.
  Chapters you do not want can be ignored one by one.
- **Paced downloads through Suwayomi**, one source at a time, with batch
  sizes that shrink and back off when a source throttles. A chapter that
  fails on one source is retried on the next source that lists it.
- **A clean library for Komga.** One folder per series, `Chapter 012.0.cbz`,
  built from hard links so Suwayomi's own download tree is never touched and
  nothing is stored twice. Komga can be asked to rescan after every import.
- **Adopt an existing library.** Everything Suwayomi already downloaded is
  identified from its folder name, registered, linked, and then topped up;
  from the Import page or the CLI.
- **Web UI and JSON API** with a background job runner and scheduler, a
  wanted list, an activity page with Suwayomi's live download queue, a
  Settings page, and a System page with configuration, source health and a
  log tail. Optional HTTP basic auth, with an API key for scripts.
- **CLI** for everything the UI does, plus `resolve`, a dry run that shows
  where every chapter would come from before you commit.
- **Notifications** via Pushover and/or a generic JSON webhook when new
  chapters land or a refresh fails.
- **Monitoring**: Prometheus metrics at `/metrics`, a health endpoint that
  says what is wrong, and optional JSON log lines.
- **Stdlib-only core.** Python 3.10+, SQLite, `urllib`. The web UI is an
  optional extra.

## Installation

### Docker Compose (recommended)

Images are published to `ghcr.io/andrewhuddleston/mang-arr` (`latest` from
`main`, and one tag per release).

1. Copy [`docker-compose.example.yml`](docker-compose.example.yml) to
   `docker-compose.yml`. It contains an optional `suwayomi` service; delete
   it if you already run Suwayomi and point `MANGARR_SUWAYOMI_URL` at yours.
2. Edit the host paths. mang-arr needs two mounts:

   | Container path | What | Access |
   |---|---|---|
   | `/config` | database, download lock and log | read/write |
   | `/data` | one folder that holds **both** Suwayomi's download tree and the library mang-arr builds; `MANGARR_STAGING` and `MANGARR_LIBRARY` point inside it (defaults `/data/staging` and `/data/library`) | read/write (the staging tree is only read) |

   **Staging and library must be under the same bind mount.** The library is
   made of hard links, and `link(2)` fails across mount points even when
   both sides are on the same disk. Do not mount the download tree and the
   library as two separate volumes. When a link is impossible mang-arr
   falls back to copying the chapter and the System page shows how many were
   copied.

   In the example the host folder `/data/manga` is mounted as `/data`;
   Suwayomi writes to `/data/manga/downloads` (so its tree is
   `/data/downloads/mangas` inside the mang-arr container, the folder whose
   children are source folders, laid out `<Source>/<Series>/*.cbz`) and the
   library goes to `/data/manga/library`. `MANGARR_STAGING` and
   `MANGARR_LIBRARY` are set accordingly.
3. Make sure Suwayomi saves chapters as CBZ (Settings → Downloads → *Save
   as CBZ*, or `DOWNLOAD_AS_CBZ=true` on the container). mang-arr only looks
   at `.cbz`, `.cbr` and `.zip` files.
4. `docker compose up -d`, then open <http://localhost:6789>.

The image runs as user `1000:1000` by default (`USER` in the Dockerfile;
override with `user: "PUID:PGID"` in compose). `chown` the config folder and
the library folder to that user and make the download tree readable by it.
The simplest arrangement is to run Suwayomi as the same user.

Authentication is optional and off until you set a username and password on
the Settings page (HTTP basic auth). Scripts can use the API key shown on
the same page instead. `/api/v1/health`, `/api/v1/system/status` and
`/metrics` stay open so health checks and scrapers work without
credentials.

### Plain Python

Python 3.10 or newer. The core has no dependencies; the web UI needs the
`web` extra.

```sh
git clone https://github.com/AndrewHuddleston/mang-arr
cd mang-arr
python3 -m venv .venv
.venv/bin/pip install -e .[web]      # or: pip install -e .   for CLI/daemon only

export MANGARR_DATA=~/.local/share/mangarr
export MANGARR_SUWAYOMI_URL=http://localhost:4567
export MANGARR_STAGING=/path/to/manga/downloads/mangas   # Suwayomi's tree
export MANGARR_LIBRARY=/path/to/manga/library            # same mount as staging

.venv/bin/mangarr serve              # web UI + API + scheduler on :6789
.venv/bin/mangarr status             # or use the CLI directly
```

`mangarr daemon` runs the scheduled refresh without the web UI, for a
systemd service or a cron-style setup where the CLI is all you need. Run
either `serve` or `daemon`, not both: each has its own scheduler and the two
would refresh the same series.

## Configuration

There are two layers.

**Environment variables** (read by `mangarr/config.py`) set paths, the
Suwayomi URL, logging, and the defaults for everything else. The Docker
image presets the ones marked with an asterisk to the container mounts
(`/config`, `/data/staging`, `/data/library`, `/config/mangarr.log`,
`http://suwayomi:4567`).

| Variable | Default | Meaning |
|---|---|---|
| `MANGARR_SUWAYOMI_URL` * | `http://localhost:4567` | Base URL of the Suwayomi server. mang-arr talks to `<url>/api/graphql`. |
| `MANGARR_DATA` * | `/var/lib/mangarr` | Data directory: the database, the download lock and (for `serve`) the log file live here. |
| `MANGARR_DB` | `$MANGARR_DATA/mangarr.db` | SQLite database path. |
| `MANGARR_LOCK` | `$MANGARR_DATA/download.lock` | Lock file; only one download run (web worker or CLI) exists at a time, the other waits. |
| `MANGARR_STAGING` * | `$MANGARR_DATA/staging` | Suwayomi's download tree, `<Source>/<Series>/*.cbz`. Read only; never renamed. |
| `MANGARR_LIBRARY` * | `$MANGARR_DATA/library` | The per-series hard-link tree Komga reads, `<Series>/Chapter 012.0.cbz`. Same mount as staging. |
| `MANGARR_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `MANGARR_LOG_FILE` * | unset (`serve`: `$MANGARR_DATA/mangarr.log`) | Also log to this file, rotated at 10 MB, five kept. The System page tails it. |
| `MANGARR_LOG_JSON` | unset | `1` writes one JSON object per log line (`ts`, `level`, `logger`, `msg`, `exc`, `thread`) instead of the human format, for Loki/Promtail/Vector. |
| `MANGARR_REFRESH_HOURS` | `6` | Default for the refresh interval (see Settings). |
| `MANGARR_FIRST_REFRESH_MIN` | `5` | Minutes after start-up before the first scheduled refresh. |
| `MANGARR_PUSHOVER_TOKEN` | unset | Default Pushover application token. Notifications are sent only when both Pushover values are set. |
| `MANGARR_PUSHOVER_USER` | unset | Default Pushover user key. |
| `MANGARR_WEBHOOK_URL` | unset | Default URL to POST `{"title", "message", "kind"}` JSON to on every notification. |
| `MANGARR_UNUSABLE_SOURCES` | `comick (unoriginal) (en),mangakakalot (en),readcomiconline (en)` | Default set of disabled sources: comma-separated Suwayomi source names (case-insensitive) that are searched but never assigned chapters, because they list series but cannot deliver images, or rate-limit into uselessness. |
| `MANGARR_THROTTLED_SOURCES` | `manganato (en)` | Default set of throttled sources: they work but throttle, so they lose every close call and get single-chapter batches; chapters nobody else has are still taken from them. |

Source names are Suwayomi's display names as shown on the System page, e.g.
`Weeb Central (EN)`.

**Runtime settings** live in the database and are edited on the Settings
page (`/settings`). The environment variables above are only their initial
values; a saved setting wins and applies to the next job.

| Setting | Notes |
|---|---|
| Sources: disabled / throttled | One row per Suwayomi source, two checkboxes. Defaults from `MANGARR_UNUSABLE_SOURCES` and `MANGARR_THROTTLED_SOURCES`. |
| Refresh every (hours) | The scheduler and the daemon pick a change up within seconds. |
| Minimum pages for a fractional chapter | A `12.5` with fewer pages than this is treated as a notice image and marked junk. Default 8. |
| Komga URL, API key, library id | When URL and key are set, every import that linked at least one chapter asks Komga to scan (the given library, or all of them). The key comes from Komga's account menu → API keys. *Save & test Komga* lists the libraries it can see. |
| Pushover token / user, webhook URL | Notification channels; *Save & send test notification* checks them. |
| Web username / password | HTTP basic auth for the UI and API. Empty username means no login. |
| API key | Generated on first start and shown in the clear (it is not a secret field). When a login is set, a request carrying it as an `X-Api-Key` header or `?apikey=` query parameter is accepted without basic auth. Edit it to rotate it. |

Secrets (the Komga API key, Pushover values, password) are never shown again once
saved: the field is blank with a "set - leave blank to keep" placeholder.
Submitting it blank keeps the current value; submitting a single space
clears it.

The remaining knobs are constants in `config.py`: `DISAGREE` (a source
whose highest chapter is more than 1.5× what the series should have is
distrusted), `BATCH_DEFAULT` / `BATCH_THROTTLED` (4 and 1 chapters queued
at a time), and `BACKOFF_MAX` / `BACKOFF_MAX_WITH_FALLBACK` (how long a
failing source is retried with doubling waits before it is dropped for the
run: 300 s, or 60 s when another source can supply the same chapters).

## Usage

### Web UI

| Page | What it does |
|---|---|
| **Series** (`/`) | Every tracked series with have / listed / wanted counts, status, primary source and when it was last checked. Filter box on top. |
| **Series detail** (`/series/{id}`) | Sources that matched and why some are not used, chapter ranges by status (have, wanted, failed, junk, unavailable), history. Buttons: *Search & download missing*, *Re-check sources only*, *Monitor / Unmonitor*, *Delete* (optionally with the library folder; Suwayomi's files are never deleted). Delete is refused while a job for the series is queued or running. The chapter list has an *ignore* button per wanted / failed / unavailable chapter and a *want* button to take an ignored one back. |
| **Add** (`/add`) | Type a title; get AniList / MangaDex candidates with covers and chapter counts. Pick one, or add by exact title with aliases when no database has it. Adding is queued as a job. |
| **Import** (`/import`) | *Scan staging folders* walks Suwayomi's download tree in a background job. Folders with exactly one exact database match are listed as identified and adopted with one click; the rest get a drop-down of candidates, an as-is (no metadata) option, or skip. *Adopt selected* registers the series, links their chapters into the library and leaves the rest to the next refresh. |
| **Lists** (`/lists`) | Import lists, as in Sonarr: an AniList user's manga list (chosen statuses), an AniList chart (trending / popular / score / favourites, top N, optional country and minimum chapter count; manga format only, no adult titles), or a text file at a URL with one title per line (`#` comments; an `anilist:123` / `mangadex:uuid` reference works too). Each list is synced on its own interval and by *Sync now*; every series it yields that is not tracked yet is queued as an ordinary add job (at most 25 per sync; the next sync continues), with the list's download / monitored flags. Titles from a text list without a single exact database match are reported as *needs review*, not added. **Exclusions** are references a list must never add: tick *exclude from import lists* when deleting a series, or add one by reference. |
| **Wanted** (`/wanted`) | Every series with missing or failed chapters and which numbers. *Search all wanted now* queues a download-only pass over all of them. |
| **Activity** (`/activity`) | The job list (add, refresh, refresh-all, search-wanted, adopt-scan, adopt, list-sync) with progress and results and a *cancel* button per queued or running job, a *Refresh all now* button, Suwayomi's own download queue, and recent history. |
| **Settings** (`/settings`) | Runtime settings; see Configuration. |
| **System** (`/system`) | Version, uptime, Suwayomi reachability, next scheduled refresh, whether notifications / Komga scan / Prometheus are on, chapters copied instead of linked (if any), every source with its unusable / throttled flag, the effective configuration, the last 200 log lines, and a *Download database backup* link (`/system/backup`, a consistent SQLite online-backup copy). |

The navigation bar shows the running job and updates every five seconds.

### CLI

```
usage: mangarr [-h] [--debug] [--log-level LOG_LEVEL] [--log-file LOG_FILE]
               [--quiet]
               {search,resolve,add,refresh,import,adopt,status,show,daemon,serve}
               ...

Sonarr for manga.

positional arguments:
  {search,resolve,add,refresh,import,adopt,status,show,daemon,serve}
    search              database candidates for a title
    resolve             dry run: where each chapter would come from
    add                 track a series and download what is missing
    refresh             re-resolve tracked series and fetch new chapters
    import              link downloaded chapters into the library
    adopt               register what Suwayomi already downloaded
    status              tracked series
    show                one series in detail
    daemon              background worker: refresh every N hours
    serve               web UI + API + background worker

options:
  -h, --help            show this help message and exit
  --debug               log every request and decision
  --log-level LOG_LEVEL
                        DEBUG, INFO, WARNING, ERROR (default INFO or
                        $MANGARR_LOG_LEVEL)
  --log-file LOG_FILE   also log to this file (rotated; default
                        $MANGARR_LOG_FILE)
  --quiet               no progress on the console, only results
```

The global options go before the subcommand: `mangarr --debug resolve "Title"`.

#### `mangarr search QUERY`

Lists AniList and MangaDex candidates for a title: reference (`anilist:123`
or `mangadex:uuid`), title, format, country, status, chapter count and
alternative titles. Use the reference with `add --anilist` / `--mangadex`.

#### `mangarr resolve [QUERY] [--anilist ID | --mangadex UUID | --manual [--alias TITLE ...]]`

Dry run. Identifies the series, searches every source, and prints which
sources matched (and which hits were rejected and why), which chapters each
source would provide, junk chapters skipped, what is already on disk, what
is wanted, gaps no source has, and which source would be kept as primary.
Nothing is written.

```
  --anilist ANILIST    AniList id, when the title is ambiguous
  --mangadex MANGADEX  MangaDex uuid
  --manual             no metadata lookup; the typed title is the series
  --alias TITLE        with --manual: another exact title sources may use
                       (repeatable)
```

#### `mangarr add [QUERY] [same options as resolve] [--no-download]`

Everything `resolve` does, then tracks the series, links anything already
downloaded into the library, downloads what is wanted and links that too.
`--no-download` tracks and links but leaves the download to the next
refresh. Exits non-zero when no usable source has the series.

```sh
mangarr add "Okuremashite Seishun"
mangarr add --anilist 175717
mangarr add --manual "Some Webtoon" --alias "Some Webtoon (Official)"
```

#### `mangarr refresh [SERIES] [--no-download] [--all]`

```
positional arguments:
  series         title fragment or id (default: every monitored series)

options:
  --no-download
  --all          include unmonitored series
```

Re-resolves one tracked series or every monitored one (`--all` includes
unmonitored series too), refreshes its metadata, downloads new chapters
and links them. This is what the scheduler runs. If the web worker is
downloading at the same time, the CLI waits for the download lock.

#### `mangarr import [SERIES]`

Links whatever is in the staging tree for one or all tracked series into the
library without touching the network. Idempotent. Triggers a Komga scan
when Komga is configured and something was linked.

#### `mangarr adopt [--only FRAGMENT] [--dry-run]`

Registers what Suwayomi already downloaded; `--only` limits it to folders
whose name contains the fragment. See the next section.

#### `mangarr status`

One line per tracked series: id, title, have / listed / wanted counts,
status, primary source.

#### `mangarr show SERIES`

One series in detail: identity, every matched source with its note, chapter
ranges by status, and the last eight events.

#### `mangarr daemon [--interval HOURS] [--once]`

The background worker without the web UI: a refresh of every monitored
series every `--interval` hours (default: the *Refresh every* setting, or
`MANGARR_REFRESH_HOURS`), with notifications. `--once` runs one cycle and
exits (for cron or a systemd timer). Stops cleanly on SIGTERM after the
current series. Do not run it next to `serve`, which has its own scheduler.

#### `mangarr serve [--host HOST] [--port PORT]`

The web UI, JSON API, job runner and scheduler in one process. Defaults to
`0.0.0.0:6789`. Needs the `web` extra.

### Adopting an existing Suwayomi library

If Suwayomi has been downloading for a while, do not re-add everything by
hand. Every `<Source>/<Series>` folder in the staging tree is looked up by
its folder name (Suwayomi's sanitised title; `:` and `?` become `_`, which
the matcher understands). Folders with exactly one exact database match are
*identified*; the rest need a choice from their candidate list.

**Web:** open **Import**, press *Scan staging folders*, wait for the job,
then tick the identified rows you want, pick a candidate (or as-is, or
skip) for each of the others, and press *Adopt selected*. The adopt job
registers the series and links their chapters; run *Refresh all now* on the
Activity page afterwards to fetch what is missing.

**CLI:**

```sh
mangarr adopt --dry-run          # look first: identified rows and REVIEW rows with candidates
mangarr adopt                    # register the identified folders
mangarr add --anilist 12345      # one command per REVIEW folder, using the
                                 # candidate list (or --mangadex / --manual)
mangarr import                   # hard-link every staged chapter into the library
mangarr refresh                  # find and fetch what is missing
```

Folders of the same series under different sources merge into one tracked
series. Chapter numbers are parsed from the file names (`Chapter 12`,
`Ch.12.5`, `Episode 12`, `#12`, and a dozen other shapes seen in the wild);
files without a readable number are reported and skipped. Season-numbered
webtoons (`S2 - Episode 5`) are not handled yet.

Note that a refresh keeps only one Suwayomi entry per series (the primary
source) in Suwayomi's own library, so Suwayomi's update checks one entry
rather than five copies. The downloaded files stay where they are.

### Komga

Point a Komga library at the mang-arr library tree (`/data/library` in the
container, `MANGARR_LIBRARY` otherwise). Komga sees one series per folder
and one book per `Chapter NNN.N.cbz`; the zero-padded names sort 12 before
12.5 before 100 without any Komga-side tweaking. Mount it read-only in
Komga. Because the files are hard links, deleting a series with its library
files in mang-arr removes only Komga's view; Suwayomi's copies are untouched.

Komga's periodic scan picks new chapters up on its own. To get them sooner,
put Komga's URL and an API key on the Settings page: mang-arr then asks
Komga to scan (one library, or all) after every import that linked
something.

## Monitoring

- **`GET /metrics`** is a Prometheus exposition (needs `prometheus-client`,
  included in the `web` extra and the Docker image; otherwise the endpoint
  says so in plain text). Gauges are refreshed from the database on each
  scrape.

  | Metric | Labels | Meaning |
  |---|---|---|
  | `mangarr_series_total` | | tracked series |
  | `mangarr_chapters` | `status` (have, wanted, failed, junk, unavailable) | chapters by status |
  | `mangarr_downloads_total` | `source`, `result` (ok, failed) | chapter download attempts |
  | `mangarr_jobs_total` | `kind`, `status` | jobs by outcome |
  | `mangarr_suwayomi_up` | | 1 when the Suwayomi API answered on this scrape |
  | `mangarr_last_refresh_timestamp` | | unix time of the last completed refresh-all |

- **`GET /api/v1/health`** returns `{"ok": true, "problems": [],
  "version": "..."}` with 200, or 503 and a list of problems when Suwayomi
  does not answer, the staging or library path is missing, or the library
  is not writable. The Docker `HEALTHCHECK` uses it.
- **`MANGARR_LOG_JSON=1`** switches both the console and the log file to one
  JSON object per line, for Loki/Promtail, Vector and similar.

`/metrics`, `/api/v1/health` and `/api/v1/system/status` are exempt from
basic auth.

## How it works

1. **Identity comes from AniList.** A series is an AniList ID plus *all* its
   titles (romaji, English, native, synonyms), its country, status and expected
   chapter count. This is the TVDB. MangaDex fills in the Western webtoons
   AniList lacks.
2. **Every source is searched with every title.** A hit is only accepted when
   its title matches one of AniList's titles exactly (after normalising
   punctuation). Prefix and substring matches are rejected - that is how
   anthologies and promos win. Author names are compared as a second check.
3. **Chapters are tracked per chapter, not per series.** The wanted list is the
   union of chapter numbers across every accepted source, minus what is on disk.
4. **Each chapter picks its own source**, preferring healthy, unthrottled ones,
   so a series split across three sites is still complete.
5. **Suwayomi downloads**, paced per source with backoff, so one throttled site
   does not stall the rest.
6. **The library is hard links.** Suwayomi's tree is its record of what it has
   downloaded and is never renamed; the Komga tree is built beside it, one
   folder per series regardless of how many sources the chapters came from.

State lives in one SQLite database (series, the source entries that matched
them, every chapter's status and paths, an event log, settings). Long
operations run in a single worker thread because Suwayomi has one download
queue.

The downloader works one source at a time. It queues a batch of its own
chapter ids in Suwayomi, watches them, and dequeues them if it gives up;
it never clears Suwayomi's queue, so your own manual downloads and
Suwayomi's library updates are left alone. Waiting is progress-aware: a
batch is abandoned only when none of its chapters has moved for ten
minutes (or after three hours in total), so a slow 150-page webtoon chapter
is not mistaken for a dead source. Within a source the batch size shrinks
to one and backs off when it errors and grows back when downloads succeed.
A chapter that fails on its first source is retried on the next source
that lists it; a source that fails everything it was asked for is dropped
for the rest of the run; only chapters no source could deliver end up
`failed`. A file lock (`MANGARR_LOCK`) makes sure only one download run
exists at a time across the web worker and the CLI; a second one waits.

```
mangarr/
  config.py      every MANGARR_* setting and its default
  settings.py    runtime settings stored in the database (Settings page)
  model.py       Series: the one dataclass every module agrees on
  anilist.py     AniList lookup (series identity)
  mangadex.py    MangaDex lookup (fallback identity)
  metadata.py    picks the one record a typed title means
  matching.py    title normalisation + strict match rules
  suwayomi.py    Suwayomi GraphQL client
  resolver.py    search every source, accept matches, build per-chapter plan
  downloader.py  paced per-source download through Suwayomi, with fallback
  library.py     staging tree parsing, hard-link library
  db.py          SQLite: series, sources, chapters, events, settings
  core.py        add / refresh / import / adopt
  jobs.py        job runner + scheduler (web)
  daemon.py      standalone background worker
  komga.py       Komga scan trigger
  notify.py      Pushover / webhook
  metrics.py     Prometheus metrics
  logsetup.py    logging (text or JSON lines)
  __main__.py    CLI
  web/           FastAPI app, templates, stylesheet (optional extra)
```

## Logging

Every module logs through `logging`, to the console (stderr) and optionally
a rotating file.

| Level | What you see |
|---|---|
| `DEBUG` | Every API request with timing, every search hit and why it was accepted or rejected, every page-count probe. |
| `INFO` | What the app decided and did: sources matched, chapters planned, downloads, imports, notifications, job start/finish, settings changes. |
| `WARNING` | Degraded but handled: a source unreachable, a source distrusted, a chapter that failed and stays wanted, throttling back-off, an unauthorised request. |
| `ERROR` | An operation did not complete. |

- `--debug` on any command is shorthand for `--log-level DEBUG`; `--quiet`
  drops the console output and prints only results.
- `MANGARR_LOG_LEVEL` sets the default level; `--log-level` overrides it per
  run.
- `MANGARR_LOG_FILE` (or `--log-file`) adds a file handler: 10 MB per file,
  five rotations kept. `mangarr serve` always writes one, defaulting to
  `$MANGARR_DATA/mangarr.log`, because the System page and
  `GET /api/v1/log` tail it. The Docker image sets it to
  `/config/mangarr.log`.
- `MANGARR_LOG_JSON=1` writes JSON lines instead of the text format.
- The System page shows the last 200 lines; `docker compose logs mangarr`
  shows the same stream.

A wrong match is best debugged with `mangarr --debug resolve "Title"`: it
prints every hit on every source and the exact reason each was accepted or
rejected.

## API

The web process exposes a JSON API under `/api/v1/`, used by the pages'
live updates and usable from scripts. Interactive docs (Swagger UI) are at
**`/api/docs`**. When a web login is set in Settings it applies to the API
as well, except for the three endpoints listed under Monitoring; send the
API key from the Settings page as an `X-Api-Key` header (or `?apikey=`)
instead of basic auth:

```sh
curl -H "X-Api-Key: $KEY" http://localhost:6789/api/v1/wanted
```

| Method and path | Purpose |
|---|---|
| `GET /api/v1/health` | 200 `{"ok": true, "problems": [], "version"}` or 503 with the problems (Suwayomi down, paths missing, library not writable); the Docker health check |
| `GET /api/v1/system/status` | version, uptime, the running job, next scheduled refresh; polled by the navigation bar |
| `GET /api/v1/series` | every tracked series with counts |
| `GET /api/v1/series/{id}` | one series with its sources and chapters |
| `POST /api/v1/series` | add. Body: `{"ref": "anilist:123", "download": true}` or `{"ref": "mangadex:<uuid>"}` or `{"ref": "manual", "title": "...", "aliases": ["..."]}`; `download` defaults to true. Returns the job. 400 on a bad reference, 409 when the series is already tracked or already queued. |
| `POST /api/v1/series/{id}/refresh?download=true` | queue a refresh; returns the job; 409 if one is already queued for the series |
| `DELETE /api/v1/series/{id}?files=false` | stop tracking, optionally delete the library folder; 409 while a job for the series runs |
| `POST /series/{id}/chapter/{number}/ignore` | mark a chapter ignored (form route used by the series page; redirects) |
| `POST /series/{id}/chapter/{number}/unignore` | make an ignored chapter wanted again |
| `GET /api/v1/importlist` | import lists with their params, last sync and result |
| `POST /api/v1/importlist` | add a list. Body: `{"name", "kind": "anilist_user" \| "anilist_top" \| "url_text", "params": {...}, "download": true, "monitored": true, "syncHours": 24, "syncNow": false}`; params per kind: `{"username", "statuses": ["CURRENT", "PLANNING"]}`, `{"sort": "TRENDING_DESC", "limit": 50, "country": "KR", "min_chapters": 0}`, `{"url"}`. 400 on bad params. |
| `POST /api/v1/importlist/{id}/sync` | queue a sync; returns the job; 409 if one is already queued |
| `DELETE /api/v1/importlist/{id}` | remove a list (series it added stay tracked) |
| `GET` / `POST` / `DELETE /api/v1/importlistexclusion` | list, add (`{"ref", "title", "reason"}`) or remove (`?ref=`) an exclusion |
| `GET /api/v1/lookup?term=...` | AniList / MangaDex candidates for a title |
| `GET /api/v1/wanted` | series with missing chapters |
| `GET /api/v1/queue` | mang-arr's jobs and Suwayomi's download queue |
| `POST /api/v1/command` | `{"name": "RefreshAll"}` (refresh every monitored series; returns the existing job if one is already queued) or `{"name": "SearchWanted"}` (download-only pass over series with wanted chapters). 400 for any other name. |
| `GET /api/v1/log?lines=200` | tail of the log file (`lines` capped at 5000) |
| `GET /metrics` | Prometheus exposition; see Monitoring |
| `GET /system/backup` | download a consistent copy of the SQLite database (`mangarr-backup-<timestamp>.db`) |

Jobs are returned as `{"id", "kind", "title", "seriesId", "status",
"queuedAt", "startedAt", "finishedAt", "progress", "message"}`; poll
`/api/v1/queue` to follow one.

Without a web login the API is open; keep it on your LAN or behind a
reverse proxy in that case.

## Roadmap

- Interactive per-chapter search: pick the source for one chapter by hand.
- Languages other than English: today only Suwayomi's English (`en`/`all`)
  sources are searched and every chapter is the English release. Planned: a
  per-series language setting, searching that language's sources, and a
  library layout that can hold more than one language of the same series.
- Sync read direction (webtoon vs. manga) to Komga.

## License

[MIT](LICENSE). Copyright 2026 Andrew Huddleston.
