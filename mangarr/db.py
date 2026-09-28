"""SQLite store: which series are tracked, which source entries belong to
them, and the state of every chapter.

Schema changes are applied as numbered migrations against PRAGMA
user_version, so an existing database upgrades in place. Each migration is
applied in one transaction together with its version bump, so a failure
(disk full, a kill mid-upgrade) leaves the previous version intact rather
than half a migration that every later start trips over.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config, library
from .matching import oneline
from .model import Series

log = logging.getLogger(__name__)

MIGRATIONS = [
    # 1: initial schema
    """
    CREATE TABLE series (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      ref             TEXT NOT NULL UNIQUE,   -- anilist:123 | mangadex:uuid | manual:title
      anilist_id      INTEGER UNIQUE,
      mangadex_id     TEXT UNIQUE,
      title           TEXT NOT NULL,
      romaji          TEXT, english TEXT, native TEXT,
      synonyms        TEXT NOT NULL DEFAULT '[]',
      country         TEXT, status TEXT, format TEXT,
      expected        INTEGER,                -- chapter count when the series is finished and known
      authors         TEXT NOT NULL DEFAULT '[]',
      cover           TEXT,
      monitored       INTEGER NOT NULL DEFAULT 1,
      added_at        TEXT NOT NULL,
      last_resolved   TEXT,
      last_error      TEXT
    );
    CREATE TABLE series_source (
      series_id       INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
      manga_id        INTEGER NOT NULL,       -- Suwayomi's id for this source entry
      source_name     TEXT NOT NULL,
      title           TEXT NOT NULL,
      author          TEXT,
      match_level     INTEGER NOT NULL,
      author_level    INTEGER NOT NULL,
      chapter_count   INTEGER NOT NULL,
      max_chapter     REAL NOT NULL,
      note            TEXT,                   -- why it is not used, if it is not
      is_primary      INTEGER NOT NULL DEFAULT 0,
      folder          TEXT,                   -- staging folder, once known
      seen_at         TEXT NOT NULL,
      PRIMARY KEY (series_id, manga_id)
    );
    CREATE TABLE chapter (
      series_id       INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
      number          REAL NOT NULL,
      status          TEXT NOT NULL,          -- wanted | have | failed | junk | unavailable | ignored
      manga_id        INTEGER,                -- source entry chosen for it
      source_name     TEXT,
      staging_path    TEXT,                   -- Suwayomi's file
      library_path    TEXT,                   -- the hard link Komga reads
      pages           INTEGER,
      updated_at      TEXT NOT NULL
    , PRIMARY KEY (series_id, number)
    );
    CREATE TABLE event (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      at              TEXT NOT NULL,
      series_id       INTEGER,
      kind            TEXT NOT NULL,          -- added | resolved | downloaded | imported | failed | review
      message         TEXT NOT NULL
    );
    """,
    # 2: runtime settings (see settings.py)
    """
    CREATE TABLE setting (
      key             TEXT PRIMARY KEY,
      value           TEXT NOT NULL           -- JSON
    );
    """,
    # 3: the library folder belongs to the series, not to its title, so two
    #    same-titled series never share one
    """
    ALTER TABLE series ADD COLUMN folder TEXT;
    """,
    # 4: why a chapter is failed / junk / unavailable, for the UI
    """
    ALTER TABLE chapter ADD COLUMN reason TEXT;
    """,
    # 5: the chapter's title and release date as the source lists them (for the UI)
    """
    ALTER TABLE chapter ADD COLUMN name TEXT;
    ALTER TABLE chapter ADD COLUMN uploaded TEXT;
    """,
    # 6: series description and volume count from the metadata provider (for the UI)
    """
    ALTER TABLE series ADD COLUMN description TEXT;
    ALTER TABLE series ADD COLUMN volumes INTEGER;
    """,
    # 7: import lists (see lists.py) and the refs they must never add back
    """
    CREATE TABLE import_list (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      name            TEXT NOT NULL,
      kind            TEXT NOT NULL,          -- anilist_user | anilist_top | url_text
      params          TEXT NOT NULL DEFAULT '{}',   -- JSON, per kind
      enabled         INTEGER NOT NULL DEFAULT 1,
      download        INTEGER NOT NULL DEFAULT 1,   -- download chapters when adding
      monitored       INTEGER NOT NULL DEFAULT 1,
      sync_hours      REAL NOT NULL DEFAULT 24,
      last_sync       TEXT,
      last_result     TEXT,
      created_at      TEXT NOT NULL
    );
    CREATE TABLE import_list_exclusion (
      ref             TEXT PRIMARY KEY,
      title           TEXT,
      reason          TEXT,
      created_at      TEXT NOT NULL
    );
    """,
    # 8: details for the series page (genres, first publication year, demographic)
    """
    ALTER TABLE series ADD COLUMN genres TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE series ADD COLUMN year INTEGER;
    ALTER TABLE series ADD COLUMN demographic TEXT;
    """,
    # 9: retry schedule for failed chapters (do not hammer a source every pass)
    """
    ALTER TABLE chapter ADD COLUMN tries INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE chapter ADD COLUMN next_try TEXT;
    """,
    # 10: per-source download outcomes, so the ranking learns which sources deliver
    """
    CREATE TABLE source_stats (
      source_name     TEXT PRIMARY KEY,
      ok              INTEGER NOT NULL DEFAULT 0,
      failed          INTEGER NOT NULL DEFAULT 0,
      corrupt         INTEGER NOT NULL DEFAULT 0,
      last_ok         TEXT,
      last_failed     TEXT,
      updated_at      TEXT NOT NULL
    );
    """,
    # 11: rate limiting detected per source (sources are paced automatically after this)
    """
    ALTER TABLE source_stats ADD COLUMN throttled INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE source_stats ADD COLUMN last_throttled TEXT;
    """,
    # 12: per-series event lookups (series page, chapter details) without scanning every event
    """
    CREATE INDEX IF NOT EXISTS event_series ON event(series_id, id);
    """,
    # 13: the AniList id a MangaDex series' record links to, so one series is
    #     not tracked twice, under both references (see duplicates.py)
    """
    ALTER TABLE series ADD COLUMN anilist_link INTEGER;
    """,
    # 14: the page counts of fractional chapters, so a refresh does not count
    #     them all again every pass (see pagecounts.py). Keyed by Suwayomi's
    #     ids, not by series: save_plan rewrites series_source every resolve.
    #     IF NOT EXISTS: should a merge renumber this migration, a database
    #     that already has the table still upgrades.
    """
    CREATE TABLE IF NOT EXISTS page_probe (
      manga_id        INTEGER NOT NULL,       -- Suwayomi's id for the source entry
      chapter_id      INTEGER NOT NULL,       -- Suwayomi's id for the chapter
      number          REAL NOT NULL,          -- the chapter as the source listed it when last counted or tried
      name            TEXT,
      scanlator       TEXT,
      uploaded        TEXT,
      pages           INTEGER,                -- the last count for that listing; NULL: none yet (the tries failed)
      counted_at      TEXT,
      agreed          INTEGER NOT NULL DEFAULT 0,   -- counts in a row of that listing that gave these pages
      tries           INTEGER NOT NULL DEFAULT 0,   -- failed counts since the last one that worked (pages not used)
      next_try        TEXT,                   -- after a failed count: not counted again before this
      PRIMARY KEY (manga_id, chapter_id)
    );
    """,
    # 15: chapters a series is stuck behind (stuck.py): when a chapter's
    #     failures began; per blocker the evidence (what the sites listing it
    #     call it and their whole chapters with a side-story word, where it is
    #     on them, which of them it failed on), dropped once it no longer
    #     blocks; and apart from it what you (or the automatic skip) decided
    #     about it, kept while it is not listed for a while too: only dropped
    #     once it is on disk or gone for good
    """
    ALTER TABLE chapter ADD COLUMN failed_since TEXT;
    CREATE TABLE stuck (
      series_id       INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
      number          REAL NOT NULL,
      names           TEXT NOT NULL DEFAULT '{}',   -- JSON {source name: its title for the chapter there, or null}
      seen            TEXT NOT NULL DEFAULT '{}',   -- JSON {source name: when a resolve last saw it list the chapter}
      wholes          TEXT NOT NULL DEFAULT '{}',   -- JSON {source name: {number: name}}: its whole chapters with a
                                                    --   side-story or extra word in their names (verdict.site_words)
      urls            TEXT NOT NULL DEFAULT '{}',   -- JSON {source name: the chapter's page on that site ('': none)}
      failed_on       TEXT NOT NULL DEFAULT '[]',   -- JSON [source names a download run failed it on]
      updated_at      TEXT NOT NULL,
      PRIMARY KEY (series_id, number)
    );
    CREATE TABLE stuck_choice (
      series_id       INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
      number          REAL NOT NULL,
      skipped         TEXT,                   -- 'manual' | 'auto' once skipped from a stuck note
      skipped_state   TEXT,                   -- the chapter's state when it was skipped (JSON, stuck.state_of)
      verdict         TEXT,                   -- the verdict's headline then
      dismissed       TEXT,                   -- "Keep waiting": the chapter's state then (never skipped automatically)
      declined        TEXT,                   -- when you un-skipped it or wanted it again: never skipped automatically
      gone_since      TEXT,                   -- since when no site lists it (unavailable, junk): dropped after a while
      updated_at      TEXT NOT NULL,
      PRIMARY KEY (series_id, number)
    );
    """,
    # 16: when each chapter was last listed, and by which sites (save_plan): a
    #     chapter no site lists in one resolve is still waited for, and holds
    #     the chapters after it in order, until none has listed it for a grace
    #     period in which every site that had listed it answered (past_grace).
    #     And since when the verdict on a chapter a series is stuck behind is
    #     one the automatic skip may act on (stuck.py): never on the first
    #     resolve that says so
    """
    ALTER TABLE chapter ADD COLUMN listed TEXT;               -- JSON {source name: [when it last listed it, its name]}
    ALTER TABLE chapter ADD COLUMN listed_at TEXT;            -- when a resolve last saw any source list it
    ALTER TABLE chapter ADD COLUMN unlisted INTEGER NOT NULL DEFAULT 0;   -- resolves in a row no source listed it in
    ALTER TABLE chapter ADD COLUMN missed INTEGER NOT NULL DEFAULT 0;     -- ... of them the ones every source that
                                                                          --   had listed it answered in
    ALTER TABLE stuck ADD COLUMN high_since TEXT;
    """,
    # 17: one-time maintenance tasks an upgrade schedules, run once on the
    #     next start (see maintenance_due): the check of the library links
    #     0.3.0 made under misread chapter numbers (relink.py), for a
    #     database that has library links (a new one has nothing to check)
    """
    CREATE TABLE IF NOT EXISTS maintenance (
      name            TEXT PRIMARY KEY,       -- the task (relink.TASK)
      due_at          TEXT NOT NULL,          -- when an upgrade scheduled it
      done_at         TEXT                    -- when it last ran to the end; NULL: still due
    );
    INSERT OR IGNORE INTO maintenance (name, due_at) SELECT 'check-library-links', datetime('now', 'localtime')
      WHERE EXISTS (SELECT 1 FROM chapter WHERE status = 'have' AND library_path IS NOT NULL);
    """,
]


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ago(days: float) -> str:
    """now() as it was `days` ago."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))


def _statements(sql: str) -> list[str]:
    """The statements of a migration script, one per entry (sqlite3 runs one
    statement per execute(); executescript() would commit after each)."""
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete statement in migration: {buf.strip()[:80]!r}")
    return out


_migrate_lock = threading.Lock()      # threads of this process migrate one at a time


def migrate(con: sqlite3.Connection, target: int | None = None) -> None:
    """Bring the database up to `target` (default: the latest schema). Each
    migration runs in its own BEGIN IMMEDIATE transaction that also bumps
    user_version, and the version is re-read after the write lock is taken,
    so concurrent connections (threads or processes) never apply the same
    migration twice and a failure leaves the previous version intact."""
    target = len(MIGRATIONS) if target is None else target
    if con.execute("PRAGMA user_version").fetchone()[0] >= target:
        return                                     # the common case: nothing to do, no lock
    with _migrate_lock:
        con.commit()                               # nothing of the caller's may ride along
        while True:
            con.execute("BEGIN IMMEDIATE")
            version = -1
            try:
                version = con.execute("PRAGMA user_version").fetchone()[0]
                if version >= target:
                    con.commit()
                    return
                i = version + 1
                for stmt in _statements(MIGRATIONS[version]):
                    con.execute(stmt)
                if i == 3:
                    _backfill_folders(con)
                if i == 15:
                    _backfill_failed_since(con)
                if i == 16:
                    _backfill_listing(con)
                con.execute(f"PRAGMA user_version = {i}")
                con.commit()
            except BaseException as e:
                con.rollback()
                log.error("database migration %d failed and was rolled back (schema stays at %d): %s",
                          version + 1, version, e)
                raise
            log.info("database schema migrated to version %d", i)


def _backfill_folders(con) -> None:
    taken: set[str] = set()
    for r in con.execute("SELECT id, ref, title FROM series ORDER BY id").fetchall():
        folder = library.unique_folder(r[2], taken, r[1])
        taken.add(folder)
        con.execute("UPDATE series SET folder=? WHERE id=?", (folder, r[0]))


def _backfill_failed_since(con) -> None:
    """failed_since of the chapters that failed before migration 15: the
    first event naming the chapter among a download's failures ("ch 7.2:
    ...") or as a failed chapter search ("chapter 7.2: ..."), and at the
    latest its last update. Events are pruned after a while, so for an old
    failure this is the oldest one still recorded."""
    for r in con.execute("SELECT series_id, number, updated_at FROM chapter WHERE status='failed'").fetchall():
        n = f"{r[1]:g}"
        first = con.execute("SELECT MIN(at) FROM event WHERE series_id=? AND kind IN ('downloaded','failed') AND"
                            " (message LIKE ? OR message LIKE ? OR message LIKE ?)",
                            (r[0], f"%: ch {n}: %", f"%; ch {n}: %", f"chapter {n}: %")).fetchone()[0]
        con.execute("UPDATE chapter SET failed_since=? WHERE series_id=? AND number=?",
                    (min((x for x in (first, r[2]) if x), default=None), r[0], r[1]))


def _backfill_listing(con) -> None:
    """What migration 16 knows of the chapters' listings: a wanted or failed
    chapter was listed by its source at the last resolve of its series (an
    ignored or junk one at its last update); an unavailable one was listed
    until it became unavailable, and one that did within the last
    UNREACHABLE_KEEP_DAYS (perhaps a single resolve that missed it) gets
    its grace from then (past_grace), an older one none. A chapter a series
    is stuck behind takes the sites and names its stuck row has seen."""
    cutoff = ago(UNREACHABLE_KEEP_DAYS)
    rows = con.execute("SELECT c.series_id, c.number, c.status, c.name, c.source_name, c.updated_at,"
                       " s.last_resolved, k.names, k.seen FROM chapter c JOIN series s ON s.id=c.series_id"
                       " LEFT JOIN stuck k ON k.series_id=c.series_id AND k.number=c.number"
                       " WHERE c.status != 'have'").fetchall()
    for sid, n, status, name, source, updated, resolved, names, seen in rows:
        gone = status == "unavailable"
        when = (updated if gone else resolved if status in ("wanted", "failed") and resolved else updated) or now()
        sites = {source: [when, name]} if isinstance(source, str) and source else {}
        try:
            names, seen = json.loads(names or "{}"), json.loads(seen or "{}")
        except ValueError:
            names, seen = {}, {}
        if isinstance(names, dict) and isinstance(seen, dict):
            sites.update((k, [seen[k] if isinstance(seen.get(k), str) else when, v]) for k, v in names.items()
                         if isinstance(k, str) and (v is None or isinstance(v, str)))
        listed = json.dumps(sites) if sites else None
        missed = (1 if when >= cutoff else LISTING_GRACE_RESOLVES) if gone else 0
        con.execute("UPDATE chapter SET listed=?, listed_at=?, unlisted=?, missed=? WHERE series_id=? AND number=?",
                    (listed, when, int(gone), missed, sid, n))


def maintenance_due(con) -> set[str]:
    """The one-time tasks an upgrade scheduled (migration 17) that have not
    run to the end yet."""
    return {r[0] for r in con.execute("SELECT name FROM maintenance WHERE done_at IS NULL")}


def maintenance_done(con, name: str) -> None:
    """A one-time task (or the same task run by hand) ran to the end."""
    con.execute("INSERT INTO maintenance (name, due_at, done_at) VALUES (?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET done_at=excluded.done_at", (name, now(), now()))


def maintenance_due_again(con, name: str) -> None:
    """A task that could not do all of its work (a service it needs did not
    answer) is due again: the next start or pass runs it (maintenance_due)."""
    con.execute("INSERT INTO maintenance (name, due_at) VALUES (?,?)"
                " ON CONFLICT(name) DO UPDATE SET done_at=NULL", (name, now()))


def valid_folder(folder) -> bool:
    """Is this a usable series folder name: one plain path component (no
    separator, no NUL, not absolute, not '', '.' or '..')? Folders are joined
    onto LIBRARY_ROOT, so anything else would point outside it. This is a
    structural check only (library.safe_title is not idempotent, e.g.
    'Foo ...' -> 'Foo ', so "safe_title(folder) == folder" would reject
    folders mang-arr made itself); callers that delete files also check
    library.is_within on the resolved folder, which catches symlinks."""
    if not isinstance(folder, str) or folder in ("", ".", "..") or "\0" in folder:
        return False
    # '\\' too: safe_title never makes it, and it is a separator on SMB/Windows shares
    return not any(sep in folder for sep in ("/", "\\", os.sep, os.altsep or "/")) and not os.path.isabs(folder)


def restrict_permissions(path: str) -> None:
    """Make a database file (and its -wal/-shm companions) readable by the
    owner only: it holds the login password, the API key and notifier tokens."""
    for p in (path, path + "-wal", path + "-shm"):
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_mode & 0o077:
            try:
                os.chmod(p, 0o600)
            except OSError as e:
                log.warning("cannot make %s private (chmod 600): %s", p, e)


_restricted: set[str] = set()          # database paths already made private in this process


@contextmanager
def connect(path: str | None = None):
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if path not in _restricted and not os.path.exists(path):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))    # a new database starts private
    con = sqlite3.connect(path, timeout=30)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            con.execute("PRAGMA journal_mode = WAL")   # web requests read while a job writes
            con.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.OperationalError:               # another process holds it; next open will switch
            pass
        if path not in _restricted:
            restrict_permissions(path)                 # also tightens a database made by an older version
            _restricted.add(path)
        migrate(con)
    except BaseException:
        con.close()
        raise
    try:
        yield con
        con.commit()
    finally:
        con.close()


EVENT_MESSAGE_MAX = 2000     # characters; a summary listing every gap or source cannot grow the table unbounded


def event(con, kind: str, message: str, series_id: int | None = None) -> None:
    if len(message) > EVENT_MESSAGE_MAX:
        message = message[:EVENT_MESSAGE_MAX - 3] + "..."
    con.execute("INSERT INTO event (at, series_id, kind, message) VALUES (?,?,?,?)",
                (now(), series_id, kind, message))


# Per-pass summaries written for every series on every refresh ("Sources
# resolved", "Needs a decision"). On a large library they are most of the
# table, so the row cap removes these first and the chapter history
# (downloaded, imported, failed, ...) keeps its full keep_days.
ROUTINE_EVENT_KINDS = ("resolved", "review")


def prune_events(con, keep_days: float, keep_rows: int) -> int:
    """Delete events older than keep_days; then, if more than keep_rows are
    left, the oldest routine events and only after those the oldest of any
    kind. Returns how many were deleted. The history is for the UI; without
    this the table (and every backup) grows forever."""
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - keep_days * 86400))
    aged = con.execute("DELETE FROM event WHERE at < ?", (cutoff,)).rowcount
    excess = con.execute("SELECT COUNT(*) FROM event").fetchone()[0] - max(keep_rows, 0)
    routine = other = 0
    if excess > 0:
        marks = ",".join("?" for _ in ROUTINE_EVENT_KINDS)
        routine = con.execute(f"DELETE FROM event WHERE id IN (SELECT id FROM event WHERE kind IN ({marks})"
                              " ORDER BY id LIMIT ?)", (*ROUTINE_EVENT_KINDS, excess)).rowcount
        excess -= routine
    if excess > 0:
        other = con.execute("DELETE FROM event WHERE id IN (SELECT id FROM event ORDER BY id LIMIT ?)",
                            (excess,)).rowcount
    con.commit()
    if aged:
        log.info("event history pruned: %d event(s) older than %g days removed", aged, keep_days)
    if routine or other:
        log.info("event history over %d events: removed the %d oldest routine (resolved/review) event(s)"
                 "%s", keep_rows, routine,
                 f" and, as that was not enough, the {other} oldest other event(s): raise "
                 f"MANGARR_EVENTS_KEEP_ROWS to keep {keep_days:g} days" if other else "")
    return aged + routine + other


# -- series -----------------------------------------------------------------

def upsert_series(con, s: Series) -> int:
    """Insert or refresh a series; returns its internal id. A new series gets
    a library folder that no other series uses."""
    fields = dict(
        anilist_id=s.anilist_id, mangadex_id=s.mangadex_id,
        title=s.title, romaji=s.romaji, english=s.english, native=s.native,
        synonyms=json.dumps(s.synonyms), country=s.country, status=s.status,
        format=s.format, expected=s.chapters, authors=json.dumps(s.authors),
        cover=s.cover, description=(s.description or None), volumes=s.volumes,
        genres=json.dumps(s.genres), year=s.year, demographic=s.demographic, anilist_link=s.anilist_link)
    row = con.execute("SELECT id, folder FROM series WHERE ref=?", (s.ref,)).fetchone()
    if row:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE series SET {sets} WHERE id=?", (*fields.values(), row["id"]))
        if not row["folder"]:
            taken = {r["folder"] for r in con.execute("SELECT folder FROM series WHERE folder IS NOT NULL")}
            con.execute("UPDATE series SET folder=? WHERE id=?",
                        (library.unique_folder(s.title, taken, s.ref), row["id"]))
        return row["id"]
    taken = {r["folder"] for r in con.execute("SELECT folder FROM series WHERE folder IS NOT NULL")}
    fields["folder"] = library.unique_folder(s.title, taken, s.ref)
    cols = ", ".join(["ref", *fields, "added_at"])
    marks = ", ".join("?" for _ in range(len(fields) + 2))
    cur = con.execute(f"INSERT INTO series ({cols}) VALUES ({marks})",
                      (s.ref, *fields.values(), now()))
    return cur.lastrowid


def series_to_model(row) -> Series:
    return Series(
        anilist_id=row["anilist_id"], mangadex_id=row["mangadex_id"],
        romaji=row["romaji"], english=row["english"], native=row["native"],
        synonyms=json.loads(row["synonyms"] or "[]"), format=row["format"],
        country=row["country"], status=row["status"], chapters=row["expected"],
        cover=row["cover"], authors=json.loads(row["authors"] or "[]"),
        description=row["description"] if "description" in row.keys() else None,
        volumes=row["volumes"] if "volumes" in row.keys() else None,
        genres=json.loads(row["genres"] or "[]") if "genres" in row.keys() else [],
        year=row["year"] if "year" in row.keys() else None,
        demographic=row["demographic"] if "demographic" in row.keys() else None,
        anilist_link=row["anilist_link"] if "anilist_link" in row.keys() else None)


def get_series(con, series_id: int):
    return con.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()


def series_size(con, series_id: int) -> tuple[int, int]:
    """(bytes on disk, files) of the chapters this series has in the library."""
    total = files = 0
    for r in con.execute("SELECT library_path FROM chapter WHERE series_id=? AND status='have'", (series_id,)):
        p = r["library_path"]
        if p and library.is_within(p, config.LIBRARY_ROOT):     # never stat paths outside the library
            try:
                total += os.path.getsize(p)
                files += 1
            except OSError:
                pass
    return total, files


def get_series_by_ref(con, ref: str):
    return con.execute("SELECT * FROM series WHERE ref=?", (ref,)).fetchone()


def find_series(con, text: str):
    """Rows whose title contains the text (case-insensitive), or the id."""
    if text.isdigit():
        r = get_series(con, int(text))
        return [r] if r else []
    return con.execute("SELECT * FROM series WHERE title LIKE ? COLLATE NOCASE ORDER BY title",
                       (f"%{text}%",)).fetchall()


def series_rows(con):
    return con.execute(
        "SELECT s.*, "
        " (SELECT COUNT(*) FROM chapter c WHERE c.series_id=s.id AND c.status NOT IN ('junk','ignored')) AS listed,"
        " (SELECT COUNT(*) FROM chapter c WHERE c.series_id=s.id AND c.status='have') AS have,"
        " (SELECT COUNT(*) FROM chapter c WHERE c.series_id=s.id AND c.status IN ('wanted','failed')) AS wanted,"
        " (SELECT source_name FROM series_source ss WHERE ss.series_id=s.id AND ss.is_primary=1) AS primary_source"
        " FROM series s ORDER BY s.title COLLATE NOCASE").fetchall()


def delete_series(con, series_id: int) -> None:
    con.execute("DELETE FROM series WHERE id=?", (series_id,))


def set_monitored(con, series_id: int, monitored: bool) -> None:
    con.execute("UPDATE series SET monitored=? WHERE id=?", (int(monitored), series_id))
    event(con, "monitor", "monitored" if monitored else "unmonitored", series_id)


def chapters_due(con, in_order: bool = False) -> dict[int, int]:
    """Per series id, how many of its chapters a download would fetch now,
    as core._due picks them: wanted ones the last resolve listed, and failed
    ones whose next attempt is due. In order (strict download in order) only
    those before the first chapter that holds the series (holds_in_order):
    the later ones wait for it (core.hold_in_order)."""
    now_, out, held = now(), {}, set()
    for r in con.execute("SELECT series_id, status, next_try, unlisted, missed, listed_at FROM chapter"
                         " WHERE status IN ('wanted','failed','unavailable') ORDER BY series_id, number"):
        sid = r["series_id"]
        if sid in held:
            continue
        if in_order and holds_in_order(r, not r["unlisted"], now_):
            held.add(sid)
        elif r["status"] != "unavailable" and not r["unlisted"] and \
                (r["status"] == "wanted" or not r["next_try"] or r["next_try"] <= now_):
            out[sid] = out.get(sid, 0) + 1
    return out


def holds_in_order(row, listed: bool, now_: str | None = None) -> bool:
    """Whether a chapter (its row) holds the later chapters of its series
    with strict download in order, being neither on disk nor ignored: it
    failed and waits for its next attempt, or no source listed it in the
    last resolve (`listed`) and its grace is not over (past_grace)."""
    if row["status"] not in ("wanted", "failed", "unavailable"):
        return False
    if not listed:
        return not past_grace(row)
    return row["status"] == "failed" and bool(row["next_try"]) and row["next_try"] > (now_ or now())


def wanted_all(con):
    """One row per series with wanted/failed chapters, numbers as a range string."""
    from .resolver import ranges
    rows = con.execute(
        "SELECT s.id, s.title, s.last_resolved,"
        " SUM(c.status='wanted') AS wanted, SUM(c.status='failed') AS failed,"
        " GROUP_CONCAT(c.number) AS nums,"
        " (SELECT reason FROM chapter f WHERE f.series_id=s.id AND f.status='failed'"
        "  ORDER BY f.updated_at DESC LIMIT 1) AS last_reason"
        " FROM series s JOIN chapter c ON c.series_id=s.id AND c.status IN ('wanted','failed')"
        " GROUP BY s.id ORDER BY s.title COLLATE NOCASE").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["wanted"] = (d["wanted"] or 0) + (d["failed"] or 0)
        d["numbers"] = ranges(float(x) for x in (d.pop("nums") or "").split(",") if x)
        out.append(d)
    return out


def events(con, limit: int = 50):
    return con.execute(
        "SELECT e.*, s.title FROM event e LEFT JOIN series s ON s.id=e.series_id"
        " ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()


# -- plan / chapters ---------------------------------------------------------

UNREACHABLE_KEEP_DAYS = 7       # how long a source that cannot be searched keeps its entry (see save_plan)
# A chapter no source lists in a resolve is not taken for gone at once: the
# source may not have answered, or its search missed the series this time.
# It stays wanted (or failed) and holds the chapters after it in strict order
# until no source has listed it for LISTING_GRACE_RESOLVES resolves in a row
# and LISTING_GRACE_DAYS; a resolve in which a source that listed it in the
# last LISTING_KEEP_DAYS did not answer, or did not match the series, does
# not count (past_grace). Only then is it 'unavailable'.
#
# A fractional chapter whose copy on every site is too short to be the
# chapter (resolver._prune_junk) is junk; but not while a site that listed it
# in the last LISTING_KEEP_DAYS, with a copy not known to be short, did not
# answer: that site may still have it at full length (void_junk). It is
# waited for then like a chapter no site listed.
LISTING_GRACE_RESOLVES = 3
LISTING_GRACE_DAYS = 2
LISTING_KEEP_DAYS = UNREACHABLE_KEEP_DAYS
MAX_LISTED = 12                 # sources a chapter's listing keeps
MAX_LISTED_NAME = 200
UNLISTED_NOTE = "not listed by {} in the last check; still waiting for it"


def past_grace(row) -> bool:
    """Whether a chapter (its row) has not been listed for long enough to
    count as gone: LISTING_GRACE_RESOLVES counted resolves in a row and
    LISTING_GRACE_DAYS since a source last listed it."""
    return (row["missed"] or 0) >= LISTING_GRACE_RESOLVES and (row["listed_at"] or "") < ago(LISTING_GRACE_DAYS)


def listed_by(row, days: float = LISTING_KEEP_DAYS) -> dict:
    """{source name: [when it last listed the chapter, its name there]} of
    the sources that listed it within `days`, from its row; most recent
    first. An entry has a third item, True, when that source's copy was not
    the chapter then (short_copy). A row no resolve has written this for
    (one made another way) has its own source, as of its last update."""
    keys = row.keys()
    if row["listed"] is None:
        source = row["source_name"] if "source_name" in keys else None
        when = row["listed_at"] or (row["updated_at"] if "updated_at" in keys else None)
        got = {source: [when, row["name"] if "name" in keys else None]} if source and when else {}
    else:
        try:
            got = json.loads(row["listed"])
        except (TypeError, ValueError):
            return {}
        if not isinstance(got, dict):
            return {}
    cutoff = ago(days)
    out = {k: v for k, v in got.items() if isinstance(k, str) and isinstance(v, list) and len(v) in (2, 3)
           and isinstance(v[0], str) and v[0] >= cutoff and (v[1] is None or isinstance(v[1], str))
           and (len(v) == 2 or v[2] is True)}
    return dict(sorted(out.items(), key=lambda kv: kv[1][0], reverse=True))


def short_copy(entry) -> bool:
    """Whether a listed_by entry says that source's copy was not the chapter
    when it last listed it: counted shorter than min_pages, or not countable
    in a chapter every other copy of which was short (plan.short)."""
    return len(entry) == 3 and entry[2] is True


def unlisted_note(row) -> str:
    """The reason of a chapter no source listed in the last resolve (or only
    as a copy too short to be it) that is still waited for (UNLISTED_NOTE),
    naming the sources that listed it, those with a copy that may be the
    chapter first."""
    got = listed_by(row)
    sites = ([k for k, v in got.items() if not short_copy(v)] or list(got))[:3] or \
        ([row["source_name"]] if row["source_name"] else ["any source"])
    return UNLISTED_NOTE.format(" or ".join(oneline(k, 60) for k in sites))


def with_listing_note(row, reason: str) -> str:
    """`reason` for a chapter (its row), which when no source listed it in
    the last resolve and it is still waited for also says so, as the
    resolve left it (unlisted_note; never the part cut off)."""
    if row["unlisted"] and row["status"] in ("wanted", "failed") and not past_grace(row):
        return _with_note(reason, unlisted_note(row))
    return reason[:300]


def _with_note(reason: str | None, note: str, limit: int = 300) -> str:
    """A failed chapter's reason (without the unlisted note an earlier
    resolve added) and then `note`, within `limit` characters: the note is
    never the part cut off."""
    cut = (reason or "").find("; not listed by ")
    base = (reason or "")[:cut] if cut >= 0 else (reason or "")
    room = limit - len(note) - 2
    return (base if len(base) <= room else base[:max(room - 1, 0)] + "…") + "; " + note


def save_plan(con, series_id: int, plan, primary_manga_id: int | None) -> list[str]:
    """Store a resolve's outcome. Returns the names of sources whose entry
    was dropped because they have not been searchable for too long."""
    # A source that could not be searched this time (plan.unreachable) says
    # nothing about the series: its stored entry (and staging folder) stays,
    # and the chapters only it lists are not marked unavailable. But only for
    # UNREACHABLE_KEEP_DAYS since it last answered (seen_at): a source that
    # fails for good (a permanent block) must not keep them 'wanted' forever.
    unreachable = {src.name for src, _ in getattr(plan, "unreachable", None) or []}
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - UNREACHABLE_KEEP_DAYS * 86400))
    stored = con.execute("SELECT source_name, seen_at FROM series_source WHERE series_id=?", (series_id,)).fetchall()
    down = sorted({r["source_name"] for r in stored if r["source_name"] in unreachable and r["seen_at"] >= cutoff})
    expired = sorted({r["source_name"] for r in stored if r["source_name"] in unreachable} - set(down))
    marks = ",".join("?" * len(down))
    con.execute(f"DELETE FROM series_source WHERE series_id=? AND source_name NOT IN ({marks})", (series_id, *down))
    if down and primary_manga_id is not None:
        con.execute(f"UPDATE series_source SET is_primary=0 WHERE series_id=? AND source_name IN ({marks})",
                    (series_id, *down))
    for m in plan.matches:
        con.execute(
            "INSERT INTO series_source (series_id, manga_id, source_name, title, author,"
            " match_level, author_level, chapter_count, max_chapter, note, is_primary, seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (series_id, m.manga_id, m.source.name, m.title, m.author, m.match, m.author_ok,
             len(m.chapters), m.max, m.note or None, int(m.manga_id == primary_manga_id), now()))
    void_junk(con, series_id, plan)
    rows = {r["number"]: r for r in con.execute(
        "SELECT number, status, library_path, next_try, source_name FROM chapter WHERE series_id=?", (series_id,))}
    keep = {"have", "ignored"}
    for n, m in plan.assignment.items():
        prev = rows.get(n)
        if prev and prev["status"] in keep:
            continue                         # on disk already, or told to ignore; keep as is
        if prev and prev["status"] == "failed" and prev["next_try"] and prev["next_try"] > now():
            continue                         # scheduled for a later attempt; leave it alone
        others = [c.source.name for c in plan.candidates.get(n, []) if c.manga_id != m.manga_id]
        slow = (" (images fetched page by page: slow)" if m.source.page_warm
                else " (rate-limited source: slow)" if m.source.throttled else "")
        reason = (f"available on {m.source.name}" + slow
                  + (f" (also {', '.join(others[:3])})" if others else "")
                  + "; not downloaded yet - waiting for a download pass")
        con.execute(
            "INSERT INTO chapter (series_id, number, status, manga_id, source_name, reason, updated_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(series_id, number) DO UPDATE SET status=excluded.status, reason=excluded.reason,"
            " manga_id=excluded.manga_id, source_name=excluded.source_name, updated_at=excluded.updated_at",
            (series_id, n, "wanted", m.manga_id, m.source.name, reason, now()))
    for n, (m, pages) in plan.junk.items():
        others = [k for k in (getattr(plan, "short", None) or {}).get(n, {}) if k != m.source.name]
        reason = f"{pages} page(s) on {m.source.name}" + (f" (no full copy on {', '.join(others[:3])} either)" if
                                                          others else "") + ": a notice image, not a chapter"
        con.execute(
            "INSERT INTO chapter (series_id, number, status, manga_id, source_name, pages, reason, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(series_id, number) DO UPDATE SET status='junk', pages=excluded.pages,"
            " reason=excluded.reason, updated_at=excluded.updated_at WHERE chapter.status NOT IN ('have','ignored')",
            (series_id, n, "junk", m.manga_id, m.source.name, pages, reason, now()))
    # title and release date from the source that lists each chapter, whatever
    # its status; each source's chapters are indexed once (a lookup per
    # chapter would be quadratic, with the write lock held throughout)
    by_number: dict[int, dict] = {}
    for n, m in plan.assignment.items():
        listed = by_number.get(id(m))
        if listed is None:
            listed = by_number[id(m)] = {}
            for c in m.chapters:
                listed.setdefault(c.number, c)          # the first listed, as before
        ch = listed.get(n)
        if ch and (ch.name or ch.uploaded):
            con.execute("UPDATE chapter SET name=COALESCE(?, name), uploaded=COALESCE(?, uploaded)"
                        " WHERE series_id=? AND number=?", (ch.name, ch.uploaded, series_id, n))
    _save_listing(con, series_id, plan)
    con.execute("UPDATE series SET last_resolved=?, last_error=NULL WHERE id=?", (now(), series_id))
    return expired


def void_junk(con, series_id: int, plan) -> None:
    """The chapters the resolve found junk (no copy it saw is the chapter)
    that a site listed in the last LISTING_KEEP_DAYS with a copy not known
    to be short, which did not answer this time or whose search missed the
    series: it may still have the chapter at full length. Such a chapter is
    not junk this time: it is taken out of plan.junk (plan.voided names the
    sites) and waited for like a chapter no site listed (_save_listing,
    core.hold_in_order), until that site answers or its listing is too old
    to count. One you skipped or ignored too: you may take it back in this
    very pass (stuck.update does, when it changes), and it must then hold
    the chapters after it like any chapter still waited for."""
    junk = getattr(plan, "junk", None)
    if not junk:
        return
    answered = {m.source.name for m in plan.matches if m.usable}
    rows = chapters_by_number(con, series_id, list(junk))
    for n in list(junk):
        r = rows.get(n)
        if r is None or r["status"] == "have":
            continue
        silent = [k for k, v in listed_by(r).items() if k not in answered and not short_copy(v)]
        if silent:
            del junk[n]
            plan.voided[n] = silent
            log.info("ch %g: every copy listed now is short, but %s may still have it; waiting for %s",
                     n, " or ".join(silent[:3]), "it" if len(silent) == 1 else "them")


def _save_listing(con, series_id: int, plan) -> None:
    """Which sources list each chapter that is not on disk (every one that
    answered and lists it, whatever its copy is: a short one is kept with
    its mark, short_copy), and since when none does (see past_grace). A
    chapter no source lists now, or only with copies too short to be it
    while another may still have it (plan.voided), is still wanted (a
    failed one stays failed), its reason says so, until its grace is over;
    then it is unavailable. It comes back when a source lists it again."""
    stamp = now()
    answered = {m.source.name for m in plan.matches if m.usable}
    short = getattr(plan, "short", None) or {}
    voided = getattr(plan, "voided", None) or {}
    listing: dict[float, dict[str, list]] = {}
    index: dict[int, dict] = {}
    whole = plan.listing() if callable(getattr(plan, "listing", None)) else \
        {**{n: list(ms) for n, ms in plan.candidates.items()}, **{n: [m] for n, (m, _) in plan.junk.items()}}
    for n, ms in whole.items():
        for m in ms:
            chapters = index.get(id(m))
            if chapters is None:
                chapters = index[id(m)] = {}
                for c in m.chapters:
                    chapters.setdefault(c.number, c)
            c = chapters.get(n)
            name = oneline(c.name, MAX_LISTED_NAME) if c is not None and isinstance(c.name, str) and c.name else None
            listing.setdefault(n, {}).setdefault(m.source.name, [stamp, name] + ([True] if m.source.name in
                                                                                  short.get(n, ()) else []))
    writes = []
    for r in con.execute("SELECT * FROM chapter WHERE series_id=? AND status != 'have'", (series_id,)).fetchall():
        n, status, reason = r["number"], r["status"], r["reason"]
        kept = listed_by(r)
        now_listing = listing.get(n)
        sites = dict(now_listing or {})
        sites.update((k, v) for k, v in kept.items() if k not in sites)
        held = n in voided
        if now_listing and not held:
            listed_at, unlisted, missed = stamp, 0, 0
        elif held:                          # listed, if only as a copy too short to be it: its grace starts again,
            listed_at, unlisted, missed = stamp, (r["unlisted"] or 0) + 1, 0    # but it is not listed in full
        else:
            listed_at, unlisted = r["listed_at"], (r["unlisted"] or 0) + 1
            # counts toward its grace only when every source that listed it answered, and did not list it
            missed = (r["missed"] or 0) + (not any(k not in answered for k in kept))
        sites = dict(list(sites.items())[:MAX_LISTED])
        if (not now_listing or held) and (status in ("wanted", "failed", "unavailable") or
                                          (held and status == "junk")):
            row = {**dict(r), "listed": json.dumps(sites), "listed_at": listed_at, "missed": missed}
            if past_grace(row):
                status, reason = "unavailable", "no trusted source lists this chapter any more"
            elif status == "failed":
                reason = _with_note(reason, unlisted_note(row))
            else:
                status, reason = "wanted", unlisted_note(row)
        changed = status != r["status"] or reason != r["reason"]
        writes.append((json.dumps(sites), listed_at, unlisted, missed, status, reason, r["updated_at"] if not changed
                       else stamp, series_id, n))
    con.executemany("UPDATE chapter SET listed=?, listed_at=?, unlisted=?, missed=?, status=?, reason=?, updated_at=?"
                    " WHERE series_id=? AND number=?", writes)


def set_have(con, series_id: int, number: float, staging_path: str | None,
             library_path: str | None, source_name: str | None = None, pages: int | None = None) -> None:
    """The chapter is on disk (pages: its page count, when it was counted)."""
    con.execute(
        "INSERT INTO chapter (series_id, number, status, source_name, staging_path, library_path, pages, updated_at)"
        " VALUES (?,?,'have',?,?,?,?,?)"
        " ON CONFLICT(series_id, number) DO UPDATE SET status='have', reason=NULL, failed_since=NULL,"
        " staging_path=COALESCE(excluded.staging_path, chapter.staging_path),"
        " library_path=COALESCE(excluded.library_path, chapter.library_path),"
        " source_name=COALESCE(excluded.source_name, chapter.source_name),"
        " pages=COALESCE(excluded.pages, chapter.pages), updated_at=excluded.updated_at",
        (series_id, number, source_name, staging_path, library_path, pages, now()))


def set_reason(con, series_id: int, number: float, reason: str) -> bool:
    """Say why a chapter is where it is without changing its status (a
    downloaded file that could not be checked in time, or not linked). A
    chapter the library has, one you ignored and a junk one are left alone,
    and so is a reason that says it already. Returns whether a row
    changed."""
    cur = con.execute("UPDATE chapter SET reason=?, updated_at=? WHERE series_id=? AND number=? AND status IN"
                      " ('wanted','failed','unavailable') AND reason IS NOT ?",
                      (reason[:300], now(), series_id, number, reason[:300]))
    return cur.rowcount > 0


RETRY_HOURS = (0, 24, 72, 168)      # after the 1st failure: next pass; then 1 day, 3 days, a week (cap)


def set_status(con, series_id: int, number: float, status: str, reason: str | None = None,
               only_from: tuple[str, ...] | None = None) -> bool:
    """Change a chapter's status with a reason (cleared for have). A failure
    bumps the try count and schedules the next attempt further out each time
    (failed_since keeps when the first of these failures was); success or a
    fresh 'wanted' resets the schedule. With only_from, the row
    changes only if its current status is one of those (so a background job
    never overwrites what the user set meanwhile, e.g. 'ignored'). Returns
    whether a row changed."""
    if status == "have":
        reason = None
    guard, gargs = "", ()
    if only_from:
        guard = f" AND status IN ({','.join('?' * len(only_from))})"
        gargs = tuple(only_from)
    if status == "failed":
        row = con.execute("SELECT tries FROM chapter WHERE series_id=? AND number=?", (series_id, number)).fetchone()
        tries = (row["tries"] if row else 0) + 1
        hours = RETRY_HOURS[min(tries, len(RETRY_HOURS)) - 1]
        next_try = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + hours * 3600))
        if hours:
            reason = f"{reason or 'download failed'} (failed {tries}x; next attempt after {next_try[:16]})"
        stamp = now()
        cur = con.execute("UPDATE chapter SET status='failed', reason=?, tries=?, next_try=?, updated_at=?,"
                          " failed_since=CASE WHEN ? = 1 OR failed_since IS NULL THEN ? ELSE failed_since END"
                          " WHERE series_id=? AND number=?" + guard,
                          ((reason or None) and reason[:300], tries, next_try if hours else None, stamp, tries, stamp,
                           series_id, number, *gargs))
        return cur.rowcount > 0
    reset = ", tries=0, next_try=NULL, failed_since=NULL" if status in ("have", "wanted") else ""
    cur = con.execute(f"UPDATE chapter SET status=?, reason=?, updated_at=?{reset} WHERE series_id=? AND number=?"
                      + guard, (status, (reason or None) and reason[:300], now(), series_id, number, *gargs))
    return cur.rowcount > 0


def want_again(con, series_id: int, number: float, only_from: tuple[str, ...] | None = None) -> bool:
    """Make a chapter (an ignored one: Un-skip, Want) wanted again, as the
    last resolve left it: one no source listed then is still waited for
    (its reason says so), or unavailable once its grace is over
    (past_grace). Returns whether a row changed."""
    row = con.execute("SELECT * FROM chapter WHERE series_id=? AND number=?", (series_id, number)).fetchone()
    if row is None or not row["unlisted"]:
        return set_status(con, series_id, number, "wanted", None, only_from=only_from)
    if past_grace(row):
        return set_status(con, series_id, number, "unavailable", "no trusted source lists this chapter any more",
                          only_from=only_from)
    return set_status(con, series_id, number, "wanted", unlisted_note(row), only_from=only_from)


def chapters(con, series_id: int, limit: int | None = None, offset: int = 0):
    """The series' chapter rows by number; with limit, only that many from offset."""
    if limit is None:
        return con.execute("SELECT * FROM chapter WHERE series_id=? ORDER BY number", (series_id,)).fetchall()
    return con.execute("SELECT * FROM chapter WHERE series_id=? ORDER BY number LIMIT ? OFFSET ?",
                       (series_id, limit, offset)).fetchall()


def chapter_marks(con, series_id: int):
    """(number, status, name) of every chapter row, by number, as plain
    tuples read as they are used (a cursor): the series page keeps only
    totals of them (views.chapter_summary)."""
    cur = con.cursor()
    cur.row_factory = None
    return cur.execute("SELECT number, status, name FROM chapter WHERE series_id=? ORDER BY number", (series_id,))


def chapters_newest_first(con, series_id: int, limit: int, offset: int = 0):
    """`limit` whole chapter rows from `offset`, highest number first."""
    return con.execute("SELECT * FROM chapter WHERE series_id=? ORDER BY number DESC LIMIT ? OFFSET ?",
                       (series_id, limit, offset)).fetchall()


def chapters_by_number(con, series_id: int, numbers) -> dict:
    """{number: whole chapter row} for these chapter numbers, in statements
    of at most 500 (SQLite limits the parameters of one)."""
    numbers, out = list(numbers), {}
    for i in range(0, len(numbers), 500):
        part = numbers[i:i + 500]
        for r in con.execute(f"SELECT * FROM chapter WHERE series_id=? AND number IN ({','.join('?' * len(part))})",
                             (series_id, *part)):
            out[r["number"]] = r
    return out


def chapter_count(con, series_id: int) -> int:
    return con.execute("SELECT COUNT(*) FROM chapter WHERE series_id=?", (series_id,)).fetchone()[0]


def wanted(con, series_id: int) -> list[float]:
    """Chapters to try now: wanted, plus failed ones whose retry time has come."""
    return [r["number"] for r in con.execute(
        "SELECT number FROM chapter WHERE series_id=? AND (status='wanted' OR (status='failed'"
        " AND (next_try IS NULL OR next_try <= ?))) ORDER BY number", (series_id, now()))]


def sources(con, series_id: int):
    return con.execute("SELECT * FROM series_source WHERE series_id=? ORDER BY is_primary DESC, source_name",
                       (series_id,)).fetchall()


# -- source reliability --------------------------------------------------------

def record_source_result(con, source_name: str, result: str) -> None:
    """result: ok | failed | corrupt."""
    col = {"ok": "ok", "failed": "failed", "corrupt": "corrupt"}[result]
    stamp = "last_ok" if result == "ok" else "last_failed"
    con.execute(
        f"INSERT INTO source_stats (source_name, {col}, {stamp}, updated_at) VALUES (?, 1, ?, ?)"
        f" ON CONFLICT(source_name) DO UPDATE SET {col}={col}+1, {stamp}=excluded.{stamp},"
        f" updated_at=excluded.updated_at",
        (source_name, now(), now()))


def source_stats(con) -> dict[str, dict]:
    return {r["source_name"]: dict(r) for r in con.execute("SELECT * FROM source_stats")}


def reliability(con) -> dict[str, float]:
    """{source name: 0..1} - the share of attempted chapters that arrived
    intact, smoothed so a source with two outcomes is not judged yet."""
    out = {}
    for name, r in source_stats(con).items():
        bad = r["failed"] + r["corrupt"]
        out[name] = (r["ok"] + 2) / (r["ok"] + bad + 4)
    return out


AUTO_THROTTLE_DAYS = 14      # a source that rate-limited us is paced for this long after the last time


def record_throttle(con, source_name: str) -> None:
    """The source refused requests (rate limiting): pace it from now on."""
    con.execute(
        "INSERT INTO source_stats (source_name, throttled, last_throttled, updated_at) VALUES (?, 1, ?, ?)"
        " ON CONFLICT(source_name) DO UPDATE SET throttled=throttled+1, last_throttled=excluded.last_throttled,"
        " updated_at=excluded.updated_at", (source_name, now(), now()))


def auto_throttled(con) -> set[str]:
    """Lower-cased names of sources that rate-limited us recently."""
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - AUTO_THROTTLE_DAYS * 86400))
    return {r["source_name"].lower().strip() for r in con.execute(
        "SELECT source_name FROM source_stats WHERE last_throttled IS NOT NULL AND last_throttled >= ?", (cutoff,))}
