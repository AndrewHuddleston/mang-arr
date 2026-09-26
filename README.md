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
  be added by exact title (`--manual`). Status, chapter and volume counts,
  synonyms, description, genres, year and demographic are re-read from the
  provider on every refresh.
- **Strict matching on every source.** Every Suwayomi source is searched with
  every known title; a hit is accepted only when its title equals one of them
  after normalisation. Anthologies, promos and spin-offs are rejected and
  listed, so you can see why.
- **Per-chapter tracking.** The wanted list is the union of chapter numbers
  across every accepted source minus what is on disk. A series split across
  three sites is still complete. Sources whose chapter count is far off are
  distrusted; fractional "chapters" that are a two-page notice are dropped.
  Every chapter that is not on disk carries a reason (why it is wanted,
  why it failed, why it is unavailable or junk). Chapters you do not want
  can be ignored one by one.
- **Per-chapter search.** *Search* fetches one chapter from the best trusted
  source; *Manual* shows what every source entry has for that chapter and
  lets you pick one, including entries the automatic search would not use.
- **Paced downloads through Suwayomi**, one source at a time, with batch
  sizes that shrink and back off when a source throttles. A chapter that
  fails on one source is retried on the next source that lists it.
- **A clean library for Komga.** One folder per series, `Chapter 012.0.cbz`
  (with the source's chapter title appended when it says more than the
  number), built from hard links so Suwayomi's own download tree is never
  touched and nothing is stored twice. Season-numbered webtoons
  (`S2 - Episode 5`) are matched to Suwayomi's running chapter numbers.
  Komga can be asked to rescan after every import.
- **Adopt an existing library.** Everything Suwayomi already downloaded is
  identified from its folder name, registered, linked, and then topped up;
  from the Library Import page or the CLI.
- **Import lists**, as in Sonarr: an AniList user's list, an AniList chart,
  or a text file of titles at a URL, synced on a schedule; exclusions keep
  deleted series from coming back.
- **Web UI and JSON API.** A Sonarr-style sidebar layout: series as posters
  or a table, a series page with details and grouped chapters, a wanted
  list, a queue with Suwayomi's live downloads, history, settings, and a
  System section with health checks, scheduled tasks, backups and logs.
  Optional login (login page or HTTP basic), with an API key for scripts.
- **Health checks** on every page: Suwayomi, Komga, AniList, MangaDex, the
  two paths, hard-link viability, disk space and configuration gaps, with a
  problem count in the top bar and `/api/v1/health` for monitoring.
- **Backups** of the database, daily and on demand, with download, restore
  and upload.
- **Update check** against GitHub releases, shown as a banner.
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
   | `/config` | database, backups, download lock and log | read/write |
   | `/data` | one folder that holds **both** Suwayomi's download tree and the library mang-arr builds; `MANGARR_STAGING` and `MANGARR_LIBRARY` point inside it (defaults `/data/staging` and `/data/library`) | read/write (the staging tree is only read) |

   **Staging and library must be under the same bind mount.** The library is
   made of hard links, and `link(2)` fails across mount points even when
   both sides are on the same disk. Do not mount the download tree and the
   library as two separate volumes. When a link is impossible mang-arr
   falls back to copying the chapter; the System page shows how many were
   copied and the health check warns about it.

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

The Docker `HEALTHCHECK` calls `/api/v1/system/status`, which only says the
process is up. That is deliberate: `/api/v1/health` answers 503 when
Suwayomi is down or a path is missing, and a restart would not fix either.

Authentication is optional and off until you set a username and password
under Settings → Security (a login page by default, or the browser's basic
auth prompt). Scripts use the API key shown on the same page instead.
`/api/v1/health`, `/api/v1/system/status` and `/metrics` stay open so
health checks and scrapers work without credentials.

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
would refresh the same series. Scheduled backups, the update check and
import list syncs run only under `serve`.

## Configuration

There are two layers.

**Environment variables** (read by `mangarr/config.py`, and by `backup.py`
for the two backup ones) set paths, the Suwayomi URL, logging, backups, and
the defaults for everything else. The Docker image presets the ones marked
with an asterisk to the container mounts (`/config`, `/data/staging`,
`/data/library`, `/config/mangarr.log`, `http://suwayomi:4567`).

| Variable | Default | Meaning |
|---|---|---|
| `MANGARR_SUWAYOMI_URL` * | `http://localhost:4567` | Base URL of the Suwayomi server. mang-arr talks to `<url>/api/graphql`. |
| `MANGARR_DATA` * | `/var/lib/mangarr` | Data directory: the database, the `backups/` folder, the download lock and (for `serve`) the log file live here. |
| `MANGARR_DB` | `$MANGARR_DATA/mangarr.db` | SQLite database path. |
| `MANGARR_LOCK` | `$MANGARR_DATA/download.lock` | Lock file; only one download run (web worker or CLI) exists at a time, the other waits. |
| `MANGARR_STAGING` * | `$MANGARR_DATA/staging` | Suwayomi's download tree, `<Source>/<Series>/*.cbz`. Read only; never renamed. |
| `MANGARR_LIBRARY` * | `$MANGARR_DATA/library` | The per-series hard-link tree Komga reads, `<Series>/Chapter 012.0.cbz`. Same mount as staging. |
| `MANGARR_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `MANGARR_LOG_FILE` * | unset (`serve`: `$MANGARR_DATA/mangarr.log`) | Also log to this file, rotated at 10 MB, five kept. The Logs page tails it. |
| `MANGARR_LOG_JSON` | unset | `1` writes one JSON object per log line (`ts`, `level`, `logger`, `msg`, `exc`, `thread`) instead of the human format, for Loki/Promtail/Vector. |
| `MANGARR_REFRESH_HOURS` | `6` | Default for the refresh interval (see Settings). |
| `MANGARR_FIRST_REFRESH_MIN` | `5` | Minutes after start-up before the first scheduled refresh. |
| `MANGARR_BACKUP_HOURS` | `24` | Hours between scheduled database backups (`serve` only). |
| `MANGARR_BACKUPS_KEEP` | `7` | How many backups to keep in `$MANGARR_DATA/backups`; the oldest are pruned after every backup. |
| `MANGARR_PUSHOVER_TOKEN` | unset | Default Pushover application token. Notifications are sent only when both Pushover values are set. |
| `MANGARR_PUSHOVER_USER` | unset | Default Pushover user key. |
| `MANGARR_WEBHOOK_URL` | unset | Default URL to POST `{"title", "message", "kind"}` JSON to on every notification. |
| `MANGARR_UNUSABLE_SOURCES` | `comick (unoriginal) (en),mangakakalot (en),readcomiconline (en)` | Default set of disabled sources: comma-separated Suwayomi source names (case-insensitive) that are searched but never assigned chapters, because they list series but cannot deliver images, or rate-limit into uselessness. |
| `MANGARR_THROTTLED_SOURCES` | `manganato (en)` | Default set of throttled sources: they work but throttle, so they lose every close call and get single-chapter batches; chapters nobody else has are still taken from them. |

Source names are Suwayomi's display names as shown on the System page, e.g.
`Weeb Central (EN)`.

**Runtime settings** live in the database and are edited on the Settings
page (`/settings`; the sidebar's Settings entries are anchors into it:
Sources, Scheduling, Komga, Notifications, Security). The environment
variables above are only their initial values; a saved setting wins and
applies to the next job.

| Setting | Notes |
|---|---|
| Sources: disabled / throttled | One row per Suwayomi source, two checkboxes. Defaults from `MANGARR_UNUSABLE_SOURCES` and `MANGARR_THROTTLED_SOURCES`. |
| Refresh every (hours) | The scheduler and the daemon pick a change up within seconds. |
| Minimum pages for a fractional chapter | A `12.5` with fewer pages than this is treated as a notice image and marked junk. Default 8. |
| Komga URL, API key, library id | When URL and key are set, every import that linked at least one chapter asks Komga to scan (the given library, or all of them). The key comes from Komga's account menu → API keys. *Save & test Komga* lists the libraries it can see; the health check calls the same API. |
| Pushover token / user, webhook URL | Notification channels; *Save & send test notification* checks them. |
| Login method | *login page* (a form and a signed session cookie that lasts 30 days, with a *Sign out* button in the top bar) or *browser prompt* (HTTP basic auth). Only active once a username is set. |
| Web username / password | The login for the UI and API. Empty username means no login; the health check then warns that anyone on the network can use the page. |
| API key | Generated on first start and shown in the clear (it is not a secret field). When a login is set, a request carrying it as an `X-Api-Key` header or `?apikey=` query parameter is accepted without a session or basic auth. Edit it to rotate it; rotating it, or changing the password, also signs every browser out. |

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

The layout follows Sonarr: a sidebar on the left with the sections
**Series** (Series, Add New, Library Import, Lists), **Activity** (Queue,
History), **Wanted** (Missing), **Settings** (anchors into the one settings
page) and **System** (Status, Tasks, Backups, Logs). The top bar shows the
running job and the health summary (*healthy*, *N warnings* or *N
problems*, linking to System → Status), and updates every five seconds.
An update banner appears at the top of every page when a newer release is
published.

#### Screens

| Page | One line |
|---|---|
| **Series** (`/`) | Every tracked series as posters or a table; search, sort, filter, monitored toggle, status and have/listed progress. |
| **Series detail** (`/series/{id}`) | Cover, description, a details panel, per-status counts, chapters in groups with a reason and actions per chapter, the sources that matched, recent history. |
| **Add New** (`/add`) | Type a title; pick an AniList / MangaDex candidate or add by exact title with aliases. |
| **Library Import** (`/import`) | Scan Suwayomi's download tree and adopt what is already there. |
| **Lists** (`/lists`) | Import lists (AniList user list, AniList chart, text file at a URL) and the exclusion list. |
| **Queue** (`/activity`) | mang-arr's jobs with progress, results and cancel, and Suwayomi's own download queue. |
| **History** (`/activity/history`) | The last 200 events: adds, resolves, downloads, imports, failures, monitor and ignore changes. |
| **Missing** (`/wanted`) | Every series with wanted or failed chapters, which numbers, and the last failure reason. |
| **Settings** (`/settings`) | Runtime settings: sources, scheduling, Komga, notifications, security. |
| **Status** (`/system`) | Version, uptime, health checks, scheduled tasks, backups, sources, effective configuration. |
| **Logs** (`/system/logs`) | The log file, filtered by level, auto-refreshing. |
| **Sign in** (`/login`) | The login form, when the login method is *login page*. |

#### Series

Every tracked series, as a poster grid or a table (the toggle on the right;
the choice, the sort and the filter are remembered in the browser). The
search box filters by title (and romaji / native title); *Sort* offers
title, status, progress, wanted count, last checked and added, with a
direction button; *Filter* offers all, monitored, unmonitored, wanted,
continuing and ended. Each entry shows the cover, the status as Sonarr
words (*Continuing* for a releasing series, *Ended* for a finished one,
*Hiatus*, *Cancelled*, *Unknown*), the primary source, the number of
wanted chapters, and a progress bar reading *have/listed*, where *listed*
is every chapter a trusted source lists that you have not ignored and that
is not junk. The bookmark icon toggles monitoring without leaving the page.

#### Series detail

The top shows the cover, alternative titles, badges (status, country,
format, chapters and volumes expected), the authors and the description
(clipped, with a *more* button). Buttons: *Search missing* (re-resolve the
sources and download what is wanted), *Re-check sources only* (re-resolve
without downloading), *Monitor* / *Unmonitor*, and *Delete* with two
checkboxes: *also delete library folder* (only the links mang-arr made;
Suwayomi's files are never deleted) and *exclude from import lists*. Delete
is refused while a job for the series is queued or running.

The details panel, in the style of Radarr's, lists the library path, the
status, size on disk (bytes and files of what is linked), the type (manga,
manhwa, manhua, one-shot or comic, from the country of origin and format),
original language, author and artist, genres, year, demographic, chapters
(have / listed, and the expected count when the provider knows it),
volumes, reading direction (right to left for Japanese manga that is not a
webtoon), the primary source with the entry title it matched, the metadata
reference as a link to the AniList or MangaDex page, and when the series
was added and last checked.

Below that, one pill per status with the count and the chapter ranges, and
(when something failed) a *Why chapters failed* card with the reason per
chapter.

Chapters are grouped the way Sonarr groups episodes into seasons. A series
whose named chapters are all season-numbered (`S2 - Episode 5`) gets one
group per season, plus an *Unnumbered* group for chapters with no name.
Any other series gets blocks of twenty by the integer part of the number
(*Chapters 1-20*, *Chapters 21-40*, ...; `12.5` sits with `12`). Groups
are newest first and only the newest is open; each shows a have/total
progress bar and the wanted count; *Expand all* / *Collapse all* on the
right. Inside a group, one row per chapter, newest first: number, the
title as the source lists it, the release date, the status, the source
the chapter was or will be taken from, the reason (or the library file
name for a chapter on disk), and the actions:

- **Search** (wanted, failed, unavailable or ignored chapters): queue a
  *chapter* job that tries the trusted source entries in order (primary
  first) and downloads the chapter from the first one that lists it and
  delivers it; the chapter is linked into the library straight away.
- **Manual**: open a panel listing every source entry of the series - the
  ones the automatic search would not use too - with whether it lists the
  chapter, the chapter title and scanlator there, whether Suwayomi already
  has it downloaded, and a *Download* button per entry. Downloading from an
  entry with a note (author differs, too long, disabled source) overrides
  that note for this one chapter.
- **ignore** / **want**: take a chapter off the wanted list, or put an
  ignored one back.

Then the sources table (every entry that matched, its chapter count and
highest number, the primary marked with a star, and the note saying why an
entry is not used) and the last fifteen events for the series.

#### Chapter statuses and reasons

Every chapter a trusted source lists gets a row with one of these statuses.
Every status except *have* comes with a reason, shown in the chapter table:

| Status | Meaning | Reason texts |
|---|---|---|
| `have` | On disk and linked into the library. The reason column shows the library file name instead. | - |
| `wanted` | Listed by a trusted source, not on disk yet. Downloaded by the next *Search missing*, *Search all wanted now* or scheduled refresh. | *available on Weeb Central (also MangaDex, Bato); not downloaded yet - waiting for a download pass*; *not attempted: the download pass was cancelled or interrupted before this chapter*. A chapter you just took back with *want* shows *waiting for a download pass*. |
| `failed` | The last download pass tried and could not get it. It is retried on every refresh: the re-resolve puts it back to wanted, then the download runs again. | One entry per source tried, joined with `;`: *Manganato: Suwayomi reported an error on every try, gave up after 300s of backoff*; *MangaDex: download made no progress, gave up after 60s of backoff*; *Bato: Suwayomi finished the batch without this chapter (download error)*; *does not list it* (from a per-chapter search). Prefixed *failed on every source:* when the pass ran out of fallbacks; suffixed *(no other source has this chapter)* when there was only one. *no enabled source lists this chapter* when the only entries that have it are disabled. |
| `unavailable` | It was wanted or failed, but no trusted source lists it any more (the source dropped it, or the entry was distrusted). It becomes wanted again as soon as a source lists it. | *no trusted source lists this chapter any more* |
| `junk` | A fractional chapter with fewer pages than *Minimum pages for a fractional chapter*: a notice or an ad. Never downloaded; a file for it in staging is not imported. | *3 page(s) on Manganato: a notice image, not a chapter* |
| `ignored` | You pressed *ignore*. Kept across refreshes; nothing is downloaded until you press *want*. | - |

The Missing page counts wanted and failed chapters together, and the
index's *wanted* number is the same sum.

#### Add New

Type a title; get AniList / MangaDex candidates with covers, status, format,
country and chapter counts, and a mark on the ones already tracked. A
single exact match is highlighted. Pick one, or add by exact title with
aliases when no database has it. Adding is queued as a job.

#### Library Import

*Scan staging folders* walks Suwayomi's download tree in a background job.
Folders with exactly one exact database match are listed as identified and
adopted with one tick; the rest get a drop-down of candidates, an as-is (no
metadata) option, or skip. Folders whose files carry no chapter number are
flagged (*+N unnumbered*, *season-numbered*). *Adopt selected* registers
the series, links their chapters into the library and leaves the rest to
the next refresh. See *Adopting an existing Suwayomi library* below.

#### Lists

Import lists, as in Sonarr: an AniList user's manga list (chosen statuses;
default current and planning), an AniList chart (trending / popularity /
score / favourites, top 1-100, optional country JP / KR / CN and minimum
chapter count; manga format only, no adult titles), or a text file at a
URL with one title per line (`#` comments; an `anilist:123` /
`mangadex:uuid` reference works too). Each list has its own sync interval
(default 24 h), a download flag and a monitored flag, and can be disabled.
Lists are checked every ten minutes and synced when due, or by *Sync now*;
every series a sync yields that is not tracked yet is queued as an ordinary
add job (at most 25 per sync; the next sync continues), with the list's
download / monitored flags. Titles from a text list without a single exact
database match are reported as *needs review* in the list's result, not
added. **Exclusions** are references a list must never add: tick *exclude
from import lists* when deleting a series, or add one by reference.
Deleting a list keeps the series it added.

#### Queue and History

The Queue shows the job list (kinds: `add`, `refresh`, `refresh-all`,
`search-wanted`, `chapter`, `adopt-scan`, `adopt`, `list-sync`) with
progress, results, and a *cancel* button per queued or running job, a
*Refresh all now* button, and Suwayomi's own download queue with per-item
progress; it reloads every ten seconds. History is the last 200 events
across all series.

#### Missing

Every series with wanted or failed chapters: the count, the chapter ranges,
how many failed, the most recent failure reason and when the series was
last checked. *Search all wanted now* queues a `search-wanted` job, a
download pass over all of them.

#### System

**Status** shows version, start time, Suwayomi reachability, the next
scheduled refresh, whether notifications (with a *send test* button),
Komga scan and Prometheus are on, and how many chapters were copied
instead of linked, if any. Then:

- **Health**: the same checks the top bar counts, each with a level and a
  detail line. See Monitoring.
- **Tasks**: the three scheduled tasks (refresh all monitored series,
  update check, database backup) with their interval, next run and a *run
  now* button, plus the latest release found by the update check.
- **Backups**: the kept backups with size and age, a download link each,
  *restore* and *delete* buttons, *Back up now*, and *Restore from file*
  for an uploaded `.db`. See Backups.
- **Sources**: every Suwayomi source with its ok / throttled / unusable flag.
- **Configuration**: the effective `MANGARR_*` values.

**Logs** (`/system/logs`) shows the last 500 lines of the log file (up to
5000 with `?lines=`) with a minimum-level filter, auto-refresh every five
seconds and follow.

### Backups

`serve` writes a consistent copy of the database (SQLite's online backup
API, so it is safe while jobs run) to `$MANGARR_DATA/backups/mangarr-<date>-<time>.db`
every `MANGARR_BACKUP_HOURS` hours (default 24; the first one five minutes
after start), keeping the newest `MANGARR_BACKUPS_KEEP` (default 7). *Back
up now* on the System page and `POST /api/v1/system/backup` take one on
demand; `GET /system/backup` takes one and downloads it.

Restore, from a kept backup or an uploaded file, first checks that the file
is a SQLite database that passes `PRAGMA integrity_check`, has the
`series` and `chapter` tables, and is not from a newer schema than the
running version understands; then it takes a safety backup of the current
database (named in the confirmation message), replaces the live database
in place and applies any migrations the backup is missing. No restart is
needed. A restore is refused while any job is queued or running; cancel it
on the Queue page first.

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
library without touching the network, except to ask Suwayomi for the
chapter list when a file carries no chapter number (see *Season-numbered
webtoons*). Idempotent. Triggers a Komga scan when Komga is configured and
something was linked.

#### `mangarr adopt [--only FRAGMENT] [--dry-run]`

Registers what Suwayomi already downloaded; `--only` limits it to folders
whose name contains the fragment; `--dry-run` only prints what would
happen. See the next section.

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

The web UI, JSON API, job runner, scheduler, backup and update threads in
one process. Defaults to `0.0.0.0:6789`. Needs the `web` extra.

### Adopting an existing Suwayomi library

If Suwayomi has been downloading for a while, do not re-add everything by
hand. Every `<Source>/<Series>` folder in the staging tree is looked up by
its folder name (Suwayomi's sanitised title; `:` and `?` become `_`, which
the matcher understands). Folders with exactly one exact database match are
*identified*; the rest need a choice from their candidate list.

**Web:** open **Library Import**, press *Scan staging folders*, wait for
the job, then tick the identified rows you want, pick a candidate (or
as-is, or skip) for each of the others, and press *Adopt selected*. The
adopt job registers the series and links their chapters; run *Refresh all
now* on the Queue page afterwards to fetch what is missing.

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
files without a readable number are reported and, unless Suwayomi's
chapter list can place them (next section), skipped.

Note that a refresh keeps only one Suwayomi entry per series (the primary
source) in Suwayomi's own library, so Suwayomi's update checks one entry
rather than five copies. The downloaded files stay where they are.

### Season-numbered webtoons and chapter titles

Some sources number webtoons per season: the files are named
`S1 - Episode 12`, `S2 - Episode 5` and carry no global chapter number.
Suwayomi's running chapter number for such an entry is canonical: `S2 -
Episode 5` might be chapter 131. At import, a file whose name has no
chapter number is matched to a chapter through Suwayomi's own chapter list
for that source entry, by the file stem as Suwayomi writes it or by
(season, episode), and is tracked under that number. The series page then
groups those chapters by season.

The library file name carries the source's chapter title whenever it says
more than the number: `Chapter 131.0 - S2 - Episode 5.cbz`, or `Chapter
012.0 - The Storm.cbz` for a chapter the source calls "Chapter 12: The
Storm". A title that is just the number in another form (`Ch.12`, `Episode
12`) adds nothing, so the file stays `Chapter 012.0.cbz`. Titles are
sanitised and cut at 80 characters.

### Komga

Point a Komga library at the mang-arr library tree (`/data/library` in the
container, `MANGARR_LIBRARY` otherwise). Komga sees one series per folder
and one book per `Chapter NNN.N[ - title].cbz`; the zero-padded names sort
12 before 12.5 before 100 without any Komga-side tweaking. Mount it
read-only in Komga. Because the files are hard links, deleting a series
with its library files in mang-arr removes only Komga's view; Suwayomi's
copies are untouched.

Komga's periodic scan picks new chapters up on its own. To get them sooner,
put Komga's URL and an API key on the Settings page: mang-arr then asks
Komga to scan (one library, or all) after every import that linked
something.

## Monitoring

- **Health checks** run on every page load and API status call (cached for
  a minute; the System page forces a fresh run). Each is *ok*, a *warning*
  or an *error*:

  | Check | Error when | Warning when |
  |---|---|---|
  | Suwayomi | the GraphQL API does not answer | - (ok shows version, source count and latency) |
  | Sources | Suwayomi has no English sources, or every source is disabled in Settings | - |
  | Komga | the configured URL / API key fails a real `GET /api/v1/libraries` | not configured (new chapters appear only at Komga's own scan interval) |
  | AniList, MangaDex | - | the site is unreachable or answers an error |
  | Staging, Library | the path does not exist, or the library is not writable | - |
  | Hard links | - | staging and library are on different filesystems (chapters are copied) |
  | Disk | less than 2 GB free on the library volume | less than 20 GB free |
  | Notifications | - | neither Pushover nor a webhook is configured |
  | Security | - | no web login is set |

- **`GET /api/v1/health`** returns `{"ok": true, "problems": [],
  "warnings": [...], "version": "..."}` with 200, or 503 and the problems
  when any check is an error. Use it for alerting, not for restarting the
  container: the Docker `HEALTHCHECK` deliberately uses
  `/api/v1/system/status`, which only proves the process is alive.
- **`GET /api/v1/system/status`** includes `health: {errors, warnings}`
  and `update: {current, latest, url, updateAvailable, checkedAt, error}`.
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

- **Update check**: once a day `serve` asks GitHub for the latest release
  (first check a minute after start; a failed check is retried hourly and
  shown on the System page). A newer version shows a banner on every page
  with a link to the release notes, and `updateAvailable: true` in
  `/api/v1/system/status`. *Run now* is on System → Tasks.
- **`MANGARR_LOG_JSON=1`** switches both the console and the log file to one
  JSON object per line, for Loki/Promtail, Vector and similar.

`/metrics`, `/api/v1/health` and `/api/v1/system/status` are exempt from
the login.

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
them, every chapter's status, reason, title, release date and paths, an
event log, import lists, settings). Long operations run in a single worker
thread because Suwayomi has one download queue.

A refresh does three things in order. It re-reads the series from AniList
or MangaDex (status, chapter and volume counts, synonyms, description,
genres, year, demographic; the stored record is kept when the provider is
down). It re-resolves the sources, and rewrites every chapter row that is
not *have* or *ignored*: chapters a trusted source lists become *wanted*
with a reason naming the source, fractional chapters that probe as a few
pages become *junk*, chapters no trusted source lists any more become
*unavailable*, and each chapter's title and release date are taken from
the source that lists it. Then it downloads what is wanted and links the
results.

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
`failed`, with the reason from every source tried. A file lock
(`MANGARR_LOCK`) makes sure only one download run exists at a time across
the web worker and the CLI; a second one waits. A per-chapter search or
manual download is the same machinery for one chapter from one entry.

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
  library.py     staging tree parsing, hard-link library, file names
  db.py          SQLite: series, sources, chapters, events, lists, settings
  core.py        add / refresh / import / adopt / per-chapter download
  lists.py       import lists: fetchers, params, sync
  jobs.py        job runner + scheduler (web)
  daemon.py      standalone background worker
  health.py      health checks (System page, top bar, /api/v1/health)
  backup.py      scheduled and on-demand database backups, restore
  updates.py     GitHub release check
  komga.py       Komga scan trigger and connection test
  notify.py      Pushover / webhook
  metrics.py     Prometheus metrics
  logsetup.py    logging (text or JSON lines)
  __main__.py    CLI
  web/           FastAPI app, list routes, view helpers, templates,
                 stylesheet and script (optional extra)
```

## Logging

Every module logs through `logging`, to the console (stderr) and optionally
a rotating file.

| Level | What you see |
|---|---|
| `DEBUG` | Every API request with timing, every search hit and why it was accepted or rejected, every page-count probe. |
| `INFO` | What the app decided and did: sources matched, chapters planned, downloads, imports, notifications, job start/finish, settings changes, logins, backups. |
| `WARNING` | Degraded but handled: a source unreachable, a source distrusted, a chapter that failed and stays wanted, throttling back-off, a failed health check, an unauthorised request or failed login. |
| `ERROR` | An operation did not complete. |

- `--debug` on any command is shorthand for `--log-level DEBUG`; `--quiet`
  drops the console output and prints only results.
- `MANGARR_LOG_LEVEL` sets the default level; `--log-level` overrides it per
  run.
- `MANGARR_LOG_FILE` (or `--log-file`) adds a file handler: 10 MB per file,
  five rotations kept. `mangarr serve` always writes one, defaulting to
  `$MANGARR_DATA/mangarr.log`, because the Logs page and
  `GET /api/v1/log` tail it. The Docker image sets it to
  `/config/mangarr.log`.
- `MANGARR_LOG_JSON=1` writes JSON lines instead of the text format.
- The Logs page shows the last 500 lines; `docker compose logs mangarr`
  shows the same stream.

A wrong match is best debugged with `mangarr --debug resolve "Title"`: it
prints every hit on every source and the exact reason each was accepted or
rejected.

## API

The web process exposes a JSON API under `/api/v1/`, used by the pages'
live updates and usable from scripts. Interactive docs (Swagger UI) are at
**`/api/docs`**. When a web login is set in Settings it applies to the API
as well, except for the three endpoints listed under Monitoring; send the
API key from Settings → Security as an `X-Api-Key` header (or `?apikey=`)
instead of a session or basic auth:

```sh
curl -H "X-Api-Key: $KEY" http://localhost:6789/api/v1/wanted
```

| Method and path | Purpose |
|---|---|
| `GET /api/v1/health` | 200 `{"ok": true, "problems": [], "warnings": [...], "version"}` or 503 with the problems; see Monitoring |
| `GET /api/v1/system/status` | version, uptime, the running job, next scheduled refresh, `update` (release check) and `health` (error and warning counts); polled by the top bar |
| `GET /api/v1/system/backup` | the kept backups: `[{"name", "size", "mtime"}]` |
| `POST /api/v1/system/backup` | take a backup now; returns `{"name", "size"}` |
| `GET /api/v1/series` | every tracked series with counts |
| `GET /api/v1/series/{id}` | one series with its sources and chapters (each chapter with `status`, `reason`, `name`, `uploaded`, `source_name`, paths) |
| `POST /api/v1/series` | add. Body: `{"ref": "anilist:123", "download": true}` or `{"ref": "mangadex:<uuid>"}` or `{"ref": "manual", "title": "...", "aliases": ["..."]}`; `download` defaults to true. Returns the job. 400 on a bad reference, 409 when the series is already tracked or already queued. |
| `POST /api/v1/series/{id}/refresh?download=true` | queue a refresh; returns the job; 409 if one is already queued for the series |
| `DELETE /api/v1/series/{id}?files=false` | stop tracking, optionally delete the library folder; 409 while a job for the series runs |
| `GET /api/v1/series/{id}/chapter/{n}/releases` | manual search: one object per source entry of the series: `{"source", "mangaId", "title", "note", "listed", "chapterId", "name", "scanlator", "uploaded", "downloaded", "usable"}`, or `{"source", "mangaId", "title", "note", "error"}` when Suwayomi could not list that entry. `usable` is false when the entry has a note or does not list the chapter. |
| `POST /api/v1/series/{id}/chapter/{n}/search?manga_id=` | queue a `chapter` job: without `manga_id`, the automatic search (best trusted entry that lists it); with it, that entry regardless of its note. Returns the job. |
| `POST /series/{id}/chapter/{n}/search` | the same automatic search as a form route (redirects to the series page) |
| `POST /series/{id}/chapter/{n}/download` | form route with field `manga_id`: download from that entry (the *Download* button in the Manual panel) |
| `POST /series/{id}/chapter/{n}/ignore` | mark a chapter ignored (form route; redirects) |
| `POST /series/{id}/chapter/{n}/unignore` | make an ignored chapter wanted again |
| `GET /api/v1/importlist` | import lists with their params, last sync and result |
| `POST /api/v1/importlist` | add a list. Body: `{"name", "kind": "anilist_user" \| "anilist_top" \| "url_text", "params": {...}, "enabled": true, "download": true, "monitored": true, "syncHours": 24, "syncNow": false}`; params per kind: `{"username", "statuses": ["CURRENT", "PLANNING"]}`, `{"sort": "TRENDING_DESC", "limit": 50, "country": "KR", "min_chapters": 0}`, `{"url"}`. 400 on bad params. |
| `POST /api/v1/importlist/{id}/sync` | queue a sync; returns the job; 409 if one is already queued |
| `DELETE /api/v1/importlist/{id}` | remove a list (series it added stay tracked) |
| `GET` / `POST` / `DELETE /api/v1/importlistexclusion` | list, add (`{"ref", "title", "reason"}`) or remove (`?ref=`) an exclusion |
| `GET /api/v1/lookup?term=...` | AniList / MangaDex candidates for a title |
| `GET /api/v1/wanted` | series with missing chapters |
| `GET /api/v1/queue` | mang-arr's jobs and Suwayomi's download queue |
| `POST /api/v1/command` | `{"name": "RefreshAll"}` (refresh every monitored series; returns the existing job if one is already queued) or `{"name": "SearchWanted"}` (download-only pass over series with wanted chapters). 400 for any other name. |
| `GET /api/v1/log?lines=200` | tail of the log file (`lines` capped at 5000) |
| `GET /metrics` | Prometheus exposition; see Monitoring |
| `GET /system/backup` | take a backup now and download it (`mangarr-<date>-<time>.db`) |
| `GET /system/backups/{name}` | download a kept backup |

Jobs are returned as `{"id", "kind", "title", "seriesId", "status",
"queuedAt", "startedAt", "finishedAt", "progress", "message"}`; poll
`/api/v1/queue` to follow one.

Without a web login the API is open; keep it on your LAN or behind a
reverse proxy in that case.

## Roadmap

- Languages other than English: today only Suwayomi's English (`en`/`all`)
  sources are searched and every chapter is the English release. Planned: a
  per-series language setting, searching that language's sources, and a
  library layout that can hold more than one language of the same series.
- Sync read direction (webtoon vs. manga) to Komga.

## License

[MIT](LICENSE). Copyright 2026 Andrew Huddleston.
