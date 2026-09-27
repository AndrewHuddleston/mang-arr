# Contributing

Thanks for looking. mang-arr is small on purpose; please keep it that way.

## Ground rules

- **The core stays stdlib-only.** Everything under `mangarr/` except
  `mangarr/web/` and `mangarr/convert/engine.py` must import nothing
  outside the Python 3.10 standard library. `urllib`, `sqlite3`, `argparse`, `dataclasses` and friends are
  enough; a CLI or daemon install must work with `pip install .` and no
  extras.
- **The web UI is an optional extra.** FastAPI, uvicorn, Jinja2 and
  python-multipart live only in `mangarr/web/` and are installed with
  `pip install .[web]`. `requirements.txt` mirrors that extra for the Docker
  image; keep the two lists identical.
- **E-reader conversion is an optional extra too.** Pillow is imported by
  `mangarr/convert/engine.py` only, and nothing imports that module until a
  conversion runs; the rest of `mangarr/convert/` is stdlib-only, so the
  server can list profiles without Pillow. `pip install .[convert]`;
  `requirements-convert.txt` mirrors the extra.
- **Do not change behaviour in a "cleanup" change.** Refactors and
  behaviour changes go in separate commits so each can be reviewed and
  reverted on its own.
- **Python 3.10+.** Use `X | None`, `list[str]`, `match` if you like; do not
  use anything newer than 3.10.

## Setting up

```sh
git clone https://github.com/AndrewHuddleston/mang-arr
cd mang-arr
python3 -m venv .venv
.venv/bin/pip install -e .[web] ruff
```

`-e` installs the package in place, so edits are live. Nothing in the tests
needs the extras; they are there so `mangarr serve` works too.

## Running the tests

```sh
.venv/bin/python -m unittest discover -s tests
```

The tests are plain `unittest`, need no network, and stub the metadata
providers (`tests/test_metadata.py` shows how). A new matching rule or file
name pattern should come with a case in the relevant test file; the
`cases` dict in `tests/test_library.py` is the place for new chapter file
name shapes seen in the wild.

## Lint

```sh
.venv/bin/ruff check mangarr tests
```

Configuration is in `pyproject.toml` (`[tool.ruff]`): line length 120,
target Python 3.10, rule sets E, F, W, I, B and UP. `ruff check --fix` is
fine for import order and unused imports. There is no formatter step; match
the surrounding style rather than reformatting whole files.

CI (`.github/workflows/ci.yml`) runs ruff, the tests on Python 3.10 and 3.12,
a wheel build, and a Docker build on every push and pull request. Pushes to
`main` and `v*` tags also publish the image to
`ghcr.io/andrewhuddleston/mang-arr`.

## Commit style

- One change per commit, with a subject line under about 70 characters
  written in the imperative ("Probe fractional chapters by page count", not
  "Probed" or "Fixes probing").
- The body explains *why*: which real series or source misbehaved, what the
  wrong outcome was, and why this rule is the fix. The git log of this
  project is its design document.
- Tests and lint pass on every commit.

## Where things live

```
mangarr/
  config.py      every MANGARR_* setting and its default
  settings.py    runtime settings stored in the database: DEFAULTS is the
                 list of keys (sources, scheduling, Komga, notifications,
                 login method / user / password, API key); secrets are
                 never echoed back
  model.py       Series: the one dataclass every module agrees on
  anilist.py     AniList lookup (primary identity database)
  mangadex.py    MangaDex lookup (fallback database)
  metadata.py    picks the one record a typed title means
  matching.py    title normalisation and the strict match rules
  suwayomi.py    Suwayomi GraphQL client (the only module that talks to it)
  resolver.py    search every source, accept matches, build the per-chapter plan
  downloader.py  paced per-source download through Suwayomi, with the
                 per-chapter failure reasons
  library.py     staging tree parsing, the hard-link library, chapter file
                 names (chapter_filename / chapter_label) and the
                 season-episode matching through Suwayomi's chapter list
  db.py          SQLite schema and queries (numbered migrations; chapter
                 statuses and the reason column live here)
  core.py        add / refresh / import / adopt / delete and the per-chapter
                 search (chapter_releases, download_chapter), independent
                 of the caller
  lists.py       import lists: one fetcher per kind (AniList user list,
                 AniList chart, text URL), param validation, storage, sync
  jobs.py        in-process job runner and scheduler for the web UI
  daemon.py      the standalone background worker
  health.py      health checks: live tests of Suwayomi, Komga, AniList,
                 MangaDex, paths, disk, configuration; cached a minute
  backup.py      database backups: scheduled, on demand, listing, pruning,
                 verify and restore through SQLite's online-backup API
  updates.py     daily GitHub release check (banner, /api/v1/system/status)
  komga.py       Komga scan trigger after imports and the connection test
  notify.py      Pushover and webhook notifications
  metrics.py     Prometheus metrics for /metrics (optional prometheus-client)
  convert/       e-reader copies of a chapter (EPUB, KEPUB, CBZ, PDF):
                 profiles (the screens), source (reading the CBZ),
                 writers (the formats), engine (the page work, Pillow)
  logsetup.py    console + rotating file logging (text or JSON lines)
  __main__.py    the CLI
  web/
    app.py         FastAPI app: pages, JSON API, login middleware, jobs
    lists_routes.py  the /lists page, its API and the list sync thread
    views.py       pure template helpers: NAV (the sidebar), chapter
                   grouping (group_chapters), status labels, description
                   cleanup; unit-testable without the app
    templates/     Jinja templates, one per page
    static/        one stylesheet and one script
tests/           unittest suites; no network
```

## Reporting a wrong match

The most useful bug report is a wrong download: which title you added,
which source matched what, and what it should have been. `mangarr resolve
"Title" --debug` prints every hit and why it was accepted or rejected;
paste that.
