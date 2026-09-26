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
  be added by exact title (`--manual`).
- **Strict matching on every source.** Every Suwayomi source is searched with
  every known title; a hit is accepted only when its title equals one of them
  after normalisation. Anthologies, promos and spin-offs are rejected and
  listed, so you can see why.
- **Per-chapter tracking.** The wanted list is the union of chapter numbers
  across every accepted source minus what is on disk. A series split across
  three sites is still complete. Sources whose chapter count is far off are
  distrusted; fractional "chapters" that are a two-page notice are dropped.
- **Paced downloads through Suwayomi**, one source at a time, with batch
  sizes that shrink and back off when a source throttles.
- **A clean library for Komga.** One folder per series, `Chapter 012.0.cbz`,
  built from hard links so Suwayomi's own download tree is never touched and
  nothing is stored twice.
- **Adopt an existing library.** Everything Suwayomi already downloaded is
  identified from its folder name, registered, linked, and then topped up.
- **Web UI and JSON API** with a background job runner and scheduler, a
  wanted list, an activity page with Suwayomi's live download queue, and a
  System page with configuration, source health and a log tail.
- **CLI** for everything the UI does, plus `resolve`, a dry run that shows
  where every chapter would come from before you commit.
- **Notifications** via Pushover and/or a generic JSON webhook when new
  chapters land or a refresh fails.
- **Stdlib-only core.** Python 3.10+, SQLite, `urllib`. The web UI is an
  optional extra.

## Installation

### Docker Compose (recommended)

Images are published to `ghcr.io/andrewhuddleston/mang-arr` (`latest` from
`main`, and one tag per release).

1. Copy [`docker-compose.example.yml`](docker-compose.example.yml) to
   `docker-compose.yml`. It contains an optional `suwayomi` service; delete
   it if you already run Suwayomi and point `MANGARR_SUWAYOMI_URL` at yours.
2. Edit the two host paths. mang-arr needs three mounts:

   | Container path | What | Access |
   |---|---|---|
   | `/config` | database, download lock and log | read/write |
   | `/staging` | Suwayomi's download tree: the folder whose children are source folders (`.../downloads/mangas` in a default Suwayomi install), laid out `<Source>/<Series>/*.cbz` | read |
   | `/library` | the per-series tree mang-arr builds for Komga | read/write |

   **`/staging` and `/library` must be on the same filesystem** (the same
   host disk or dataset). The library is made of hard links; on different
   filesystems mang-arr silently falls back to copying every chapter.
3. Make sure Suwayomi saves chapters as CBZ (Settings → Downloads → *Save
   as CBZ*, or `DOWNLOAD_AS_CBZ=true` on the container). mang-arr only looks
   at `.cbz`, `.cbr` and `.zip` files.
4. `docker compose up -d`, then open <http://localhost:6789>.

The container runs as `1000:1000` in the example; `chown` the config and
library folders to match, and make the download tree readable by that user.

The web UI has **no authentication**. Keep it on your LAN or behind a reverse
proxy that adds some.

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
export MANGARR_STAGING=/path/to/suwayomi/downloads/mangas
export MANGARR_LIBRARY=/path/to/manga-library

.venv/bin/mangarr serve              # web UI + API + scheduler on :6789
.venv/bin/mangarr status             # or use the CLI directly
```

`mangarr daemon` runs the scheduled refresh without the web UI, for a
systemd service or a cron-style setup where the CLI is all you need.

## Configuration

Everything is an environment variable read by `mangarr/config.py`. The
Docker image presets the paths marked with an asterisk to the container
mounts (`/config`, `/staging`, `/library`, `/config/mangarr.log`,
`http://suwayomi:4567`).

| Variable | Default | Meaning |
|---|---|---|
| `MANGARR_SUWAYOMI_URL` * | `http://localhost:4567` | Base URL of the Suwayomi server. mang-arr talks to `<url>/api/graphql`. |
| `MANGARR_DATA` * | `/var/lib/mangarr` | Data directory: the database, the download lock and (for `serve`) the log file live here. |
| `MANGARR_DB` | `$MANGARR_DATA/mangarr.db` | SQLite database path. |
| `MANGARR_LOCK` | `$MANGARR_DATA/download.lock` | Lock file so two download runs (worker + CLI) never clear each other's Suwayomi queue. |
| `MANGARR_STAGING` * | `/mnt/movie_silo/books/manga` | Suwayomi's download tree, `<Source>/<Series>/*.cbz`. Read only; never renamed. |
| `MANGARR_LIBRARY` * | `/mnt/movie_silo/books/library` | The per-series hard-link tree Komga reads, `<Series>/Chapter 012.0.cbz`. |
| `MANGARR_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `MANGARR_LOG_FILE` * | unset (`serve`: `$MANGARR_DATA/mangarr.log`) | Also log to this file, rotated at 10 MB, five kept. The System page tails it. |
| `MANGARR_REFRESH_HOURS` | `6` | The scheduler re-checks every monitored series this often. |
| `MANGARR_FIRST_REFRESH_MIN` | `5` | Minutes after start-up before the first scheduled refresh. |
| `MANGARR_PUSHOVER_TOKEN` | unset | Pushover application token. Notifications are sent only when both Pushover values are set. |
| `MANGARR_PUSHOVER_USER` | unset | Pushover user key. |
| `MANGARR_WEBHOOK_URL` | unset | A URL to POST `{"title", "message", "kind"}` JSON to on every notification. |
| `MANGARR_UNUSABLE_SOURCES` | `comick (unoriginal) (en),mangakakalot (en),readcomiconline (en)` | Comma-separated Suwayomi source names (case-insensitive) that are searched but never assigned chapters: they list series but cannot deliver images, or rate-limit into uselessness. |
| `MANGARR_THROTTLED_SOURCES` | `manganato (en)` | Sources that work but throttle: they lose every close call and get single-chapter batches, but chapters nobody else has are still taken from them. |

Source names are Suwayomi's display names as shown on the System page, e.g.
`Weeb Central (EN)`.

The remaining knobs (`DISAGREE`, the length ratio above which a source is
distrusted; `MIN_PAGES`, below which a fractional chapter counts as junk;
`BATCH_DEFAULT` / `BATCH_THROTTLED`) are constants in `config.py`.

## Usage

### Web UI

| Page | What it does |
|---|---|
| **Series** (`/`) | Every tracked series with have / listed / wanted counts, status, primary source and when it was last checked. Filter box on top. |
| **Series detail** (`/series/{id}`) | Sources that matched and why some are not used, chapter ranges by status (have, wanted, failed, junk), history. Buttons: *Search & download missing*, *Re-check sources only*, *Monitor / Unmonitor*, *Delete* (optionally with the library files; Suwayomi's files are never deleted). |
| **Add** (`/add`) | Type a title; get AniList / MangaDex candidates with covers and chapter counts. Pick one, or add by exact title with aliases when no database has it. Adding is queued as a job. |
| **Wanted** (`/wanted`) | Every series with missing or failed chapters and which numbers. |
| **Activity** (`/activity`) | The job queue (add, refresh, refresh-all) with progress and results, a *Refresh all now* button, cancel, and Suwayomi's own download queue. |
| **System** (`/system`) | Version, uptime, Suwayomi reachability, next scheduled refresh, notification test, every source with its unusable / throttled flag, the effective configuration and the last 200 log lines. |

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

#### `mangarr refresh [SERIES] [--no-download]`

Re-resolves one tracked series (title fragment or id) or all of them,
downloads new chapters and links them. This is what the scheduler runs.

#### `mangarr import [SERIES]`

Links whatever is in the staging tree for one or all tracked series into the
library without touching the network. Idempotent.

#### `mangarr adopt [--only FRAGMENT] [--dry-run]`

Registers what Suwayomi already downloaded; see the next section.

#### `mangarr status`

One line per tracked series: id, title, have / listed / wanted counts,
status, primary source.

#### `mangarr show SERIES`

One series in detail: identity, every matched source with its note, chapter
ranges by status, and the last eight events.

#### `mangarr daemon [--interval HOURS] [--once]`

The background worker without the web UI: a refresh of every monitored
series every `--interval` hours (default `MANGARR_REFRESH_HOURS`), with
notifications. `--once` runs one cycle and exits (for cron or a systemd
timer). Stops cleanly on SIGTERM after the current series.

#### `mangarr serve [--host HOST] [--port PORT]`

The web UI, JSON API, job runner and scheduler in one process. Defaults to
`0.0.0.0:6789`. Needs the `web` extra.

### Adopting an existing Suwayomi library

If Suwayomi has been downloading for a while, do not re-add everything by
hand:

```sh
mangarr adopt --dry-run          # look first
```

Every `<Source>/<Series>` folder in the staging tree is looked up by its
folder name (Suwayomi's sanitised title; `:` and `?` become `_`, which the
matcher understands). Folders with exactly one exact database match are
listed as identified; the rest are printed as `REVIEW` with their
candidates. Then:

```sh
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

Point a Komga library at the mang-arr library tree (`/library` in the
container, `MANGARR_LIBRARY` otherwise). Komga sees one series per folder
and one book per `Chapter NNN.N.cbz`; the zero-padded names sort 12 before
12.5 before 100 without any Komga-side tweaking. Mount it read-only in
Komga. Because the files are hard links, deleting a series with its library
files in mang-arr removes only Komga's view; Suwayomi's copies are untouched.

Komga's periodic scan picks new chapters up on its own; there is no
mang-arr → Komga notification yet (see Roadmap).

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
them, every chapter's status and paths, an event log). Long operations run
in a single worker thread because Suwayomi has one download queue; mang-arr
clears and refills that queue while it downloads, so do not queue manual
downloads in Suwayomi at the same time.

```
mangarr/
  config.py      every MANGARR_* setting
  model.py       Series: the one dataclass every module agrees on
  anilist.py     AniList lookup (series identity)
  mangadex.py    MangaDex lookup (fallback identity)
  metadata.py    picks the one record a typed title means
  matching.py    title normalisation + strict match rules
  suwayomi.py    Suwayomi GraphQL client
  resolver.py    search every source, accept matches, build per-chapter plan
  downloader.py  paced per-source download through Suwayomi
  library.py     staging tree parsing, hard-link library
  db.py          SQLite: series, sources, chapters, events
  core.py        add / refresh / import / adopt
  jobs.py        job runner + scheduler (web)
  daemon.py      standalone background worker
  notify.py      Pushover / webhook
  logsetup.py    logging
  __main__.py    CLI
  web/           FastAPI app, templates, stylesheet (optional extra)
```

## Logging

Every module logs through `logging`, to the console (stderr) and optionally
a rotating file.

| Level | What you see |
|---|---|
| `DEBUG` | Every API request with timing, every search hit and why it was accepted or rejected, every page-count probe. |
| `INFO` | What the app decided and did: sources matched, chapters planned, downloads, imports, notifications, job start/finish. |
| `WARNING` | Degraded but handled: a source unreachable, a source distrusted, a chapter that failed and stays wanted, throttling back-off. |
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
- The System page shows the last 200 lines; `docker compose logs mangarr`
  shows the same stream.

A wrong match is best debugged with `mangarr --debug resolve "Title"`: it
prints every hit on every source and the exact reason each was accepted or
rejected.

## API

The web process exposes a JSON API under `/api/v1/`, used by the pages'
live updates and usable from scripts. Interactive docs (Swagger UI) are at
**`/api/docs`**.

| Method and path | Purpose |
|---|---|
| `GET /api/v1/system/status` | version, uptime, the running job, next scheduled refresh (also the Docker health check) |
| `GET /api/v1/series` | every tracked series with counts |
| `GET /api/v1/series/{id}` | one series with its sources and chapters |
| `POST /api/v1/series` | add: `{"ref": "anilist:123", "download": true}` or `{"ref": "manual", "title": "...", "aliases": [...]}`; returns the job |
| `POST /api/v1/series/{id}/refresh?download=true` | queue a refresh; returns the job |
| `DELETE /api/v1/series/{id}?files=false` | stop tracking, optionally delete library files |
| `GET /api/v1/lookup?term=...` | AniList / MangaDex candidates for a title |
| `GET /api/v1/wanted` | series with missing chapters |
| `GET /api/v1/queue` | mang-arr's jobs and Suwayomi's download queue |
| `POST /api/v1/command` | `{"name": "RefreshAll"}` |
| `GET /api/v1/log?lines=200` | tail of the log file |

There is no authentication or API key; see the note under Installation.

## Roadmap

- Trigger a Komga library scan after an import instead of waiting for
  Komga's schedule.
- Season-numbered webtoons (`S2 - Episode 5`): adopt and track them as one
  series with a running chapter number.
- Per-series source overrides (pin or exclude a source for one series) from
  the UI.
- Refresh a tracked series' metadata (status, expected chapter count) from
  AniList on refresh, not only on add.
- Authentication for the web UI, or at least an API key.
- Tests for the resolver and downloader against a recorded Suwayomi.

## License

[MIT](LICENSE). Copyright 2026 Andrew Huddleston.
