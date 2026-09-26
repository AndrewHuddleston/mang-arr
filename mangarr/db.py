"""SQLite store: which series are tracked, which source entries belong to
them, and the state of every chapter.

Schema changes are applied as numbered migrations against PRAGMA
user_version, so an existing database upgrades in place.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from . import config, library
from .model import Series

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
]


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def migrate(con: sqlite3.Connection) -> None:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        con.executescript(sql)
        con.execute(f"PRAGMA user_version = {i}")
        if i == 3:
            _backfill_folders(con)
    con.commit()


def _backfill_folders(con) -> None:
    taken: set[str] = set()
    for r in con.execute("SELECT id, ref, title FROM series ORDER BY id").fetchall():
        folder = library.unique_folder(r["title"], taken, r["ref"])
        taken.add(folder)
        con.execute("UPDATE series SET folder=? WHERE id=?", (folder, r["id"]))


@contextmanager
def connect(path: str | None = None):
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        con.execute("PRAGMA journal_mode = WAL")   # web requests read while a job writes
        con.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError:               # another process holds it; next open will switch
        pass
    migrate(con)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def event(con, kind: str, message: str, series_id: int | None = None) -> None:
    con.execute("INSERT INTO event (at, series_id, kind, message) VALUES (?,?,?,?)",
                (now(), series_id, kind, message))


# -- series -----------------------------------------------------------------

def upsert_series(con, s: Series) -> int:
    """Insert or refresh a series; returns its internal id. A new series gets
    a library folder that no other series uses."""
    fields = dict(
        anilist_id=s.anilist_id, mangadex_id=s.mangadex_id,
        title=s.title, romaji=s.romaji, english=s.english, native=s.native,
        synonyms=json.dumps(s.synonyms), country=s.country, status=s.status,
        format=s.format, expected=s.chapters, authors=json.dumps(s.authors),
        cover=s.cover, description=(s.description or None), volumes=s.volumes)
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
        volumes=row["volumes"] if "volumes" in row.keys() else None)


def get_series(con, series_id: int):
    return con.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()


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

def save_plan(con, series_id: int, plan, primary_manga_id: int | None) -> None:
    con.execute("DELETE FROM series_source WHERE series_id=?", (series_id,))
    for m in plan.matches:
        con.execute(
            "INSERT INTO series_source (series_id, manga_id, source_name, title, author,"
            " match_level, author_level, chapter_count, max_chapter, note, is_primary, seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (series_id, m.manga_id, m.source.name, m.title, m.author, m.match, m.author_ok,
             len(m.chapters), m.max, m.note or None, int(m.manga_id == primary_manga_id), now()))
    rows = {r["number"]: r for r in con.execute(
        "SELECT number, status, library_path FROM chapter WHERE series_id=?", (series_id,))}
    keep = {"have", "ignored"}
    for n, m in plan.assignment.items():
        prev = rows.get(n)
        if prev and prev["status"] in keep:
            continue                         # on disk already, or told to ignore; keep as is
        con.execute(
            "INSERT INTO chapter (series_id, number, status, manga_id, source_name, reason, updated_at)"
            " VALUES (?,?,?,?,?,NULL,?)"
            " ON CONFLICT(series_id, number) DO UPDATE SET status=excluded.status, reason=NULL,"
            " manga_id=excluded.manga_id, source_name=excluded.source_name, updated_at=excluded.updated_at",
            (series_id, n, "wanted", m.manga_id, m.source.name, now()))
    for n, (m, pages) in plan.junk.items():
        reason = f"{pages} page(s) on {m.source.name}: a notice image, not a chapter"
        con.execute(
            "INSERT INTO chapter (series_id, number, status, manga_id, source_name, pages, reason, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(series_id, number) DO UPDATE SET status='junk', pages=excluded.pages,"
            " reason=excluded.reason, updated_at=excluded.updated_at WHERE chapter.status NOT IN ('have','ignored')",
            (series_id, n, "junk", m.manga_id, m.source.name, pages, reason, now()))
    # title and release date from the source that lists each chapter, whatever its status
    for n, m in plan.assignment.items():
        ch = next((c for c in m.chapters if c.number == n), None)
        if ch and (ch.name or ch.uploaded):
            con.execute("UPDATE chapter SET name=COALESCE(?, name), uploaded=COALESCE(?, uploaded)"
                        " WHERE series_id=? AND number=?", (ch.name, ch.uploaded, series_id, n))
    # a chapter that was wanted but that no trusted source lists any more is
    # not wanted, it is unavailable - it comes back if a source lists it again
    still = set(plan.assignment) | set(plan.junk)
    for n, prev in rows.items():
        if prev["status"] in ("wanted", "failed") and n not in still:
            con.execute("UPDATE chapter SET status='unavailable', reason=?, updated_at=?"
                        " WHERE series_id=? AND number=?",
                        ("no trusted source lists this chapter any more", now(), series_id, n))
    con.execute("UPDATE series SET last_resolved=?, last_error=NULL WHERE id=?", (now(), series_id))


def set_have(con, series_id: int, number: float, staging_path: str | None,
             library_path: str | None, source_name: str | None = None) -> None:
    con.execute(
        "INSERT INTO chapter (series_id, number, status, source_name, staging_path, library_path, updated_at)"
        " VALUES (?,?,'have',?,?,?,?)"
        " ON CONFLICT(series_id, number) DO UPDATE SET status='have',"
        " staging_path=COALESCE(excluded.staging_path, chapter.staging_path),"
        " library_path=COALESCE(excluded.library_path, chapter.library_path),"
        " source_name=COALESCE(excluded.source_name, chapter.source_name), updated_at=excluded.updated_at",
        (series_id, number, source_name, staging_path, library_path, now()))


def set_status(con, series_id: int, number: float, status: str, reason: str | None = None) -> None:
    """Change a chapter's status; the reason is kept for failed / junk /
    unavailable / ignored and cleared for have / wanted."""
    if status in ("have", "wanted"):
        reason = None
    con.execute("UPDATE chapter SET status=?, reason=?, updated_at=? WHERE series_id=? AND number=?",
                (status, (reason or None) and reason[:300], now(), series_id, number))


def chapters(con, series_id: int):
    return con.execute("SELECT * FROM chapter WHERE series_id=? ORDER BY number", (series_id,)).fetchall()


def wanted(con, series_id: int) -> list[float]:
    return [r["number"] for r in con.execute(
        "SELECT number FROM chapter WHERE series_id=? AND status IN ('wanted','failed') ORDER BY number",
        (series_id,))]


def sources(con, series_id: int):
    return con.execute("SELECT * FROM series_source WHERE series_id=? ORDER BY is_primary DESC, source_name",
                       (series_id,)).fetchall()
