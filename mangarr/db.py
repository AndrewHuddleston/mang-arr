"""SQLite store: which series are tracked, which source entries belong to
them, and the state of every chapter.

A series has an internal id. Most also carry an AniList id; a series added
manually (typed title only, for the Western webtoons AniList lacks) has none.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  anilist_id      INTEGER UNIQUE,
  title           TEXT NOT NULL,
  romaji          TEXT, english TEXT, native TEXT,
  synonyms        TEXT NOT NULL DEFAULT '[]',
  country         TEXT, status TEXT, format TEXT,
  expected        INTEGER,            -- AniList chapter count, if known
  authors         TEXT NOT NULL DEFAULT '[]',
  cover           TEXT,
  monitored       INTEGER NOT NULL DEFAULT 1,
  added_at        TEXT NOT NULL,
  last_resolved   TEXT
);
CREATE TABLE IF NOT EXISTS series_source (
  series_id       INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
  manga_id        INTEGER NOT NULL,   -- Suwayomi's id for this source entry
  source_name     TEXT NOT NULL,
  title           TEXT NOT NULL,
  author          TEXT,
  match_level     INTEGER NOT NULL,
  author_level    INTEGER NOT NULL,
  chapter_count   INTEGER NOT NULL,
  max_chapter     REAL NOT NULL,
  note            TEXT,               -- why it is not used, if it is not
  is_primary      INTEGER NOT NULL DEFAULT 0,
  seen_at         TEXT NOT NULL,
  PRIMARY KEY (series_id, manga_id)
);
CREATE TABLE IF NOT EXISTS chapter (
  series_id       INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
  number          REAL NOT NULL,
  status          TEXT NOT NULL,      -- wanted | have | failed | unavailable
  manga_id        INTEGER,            -- source entry chosen for it
  source_name     TEXT,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (series_id, number)
);
"""


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def connect(path: str = config.DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def upsert_series(con, s) -> int:
    """Insert or refresh a series; returns its internal id."""
    fields = dict(
        title=s.title, romaji=s.romaji, english=s.english, native=s.native,
        synonyms=json.dumps(s.synonyms), country=s.country, status=s.status,
        format=s.format, expected=s.chapters, authors=json.dumps(s.authors),
        cover=s.cover, last_resolved=now())
    if s.anilist_id is not None:
        row = con.execute("SELECT id FROM series WHERE anilist_id=?", (s.anilist_id,)).fetchone()
    else:
        row = con.execute("SELECT id FROM series WHERE anilist_id IS NULL AND lower(title)=lower(?)",
                          (s.title,)).fetchone()
    if row:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE series SET {sets} WHERE id=?", (*fields.values(), row["id"]))
        return row["id"]
    cols = ", ".join(["anilist_id", *fields, "added_at"])
    marks = ", ".join("?" for _ in range(len(fields) + 2))
    cur = con.execute(f"INSERT INTO series ({cols}) VALUES ({marks})",
                      (s.anilist_id, *fields.values(), now()))
    return cur.lastrowid


def save_plan(con, series_id: int, plan, primary_manga_id: int | None) -> None:
    con.execute("DELETE FROM series_source WHERE series_id=?", (series_id,))
    for m in plan.matches:
        con.execute(
            "INSERT INTO series_source (series_id, manga_id, source_name, title, author,"
            " match_level, author_level, chapter_count, max_chapter, note, is_primary, seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (series_id, m.manga_id, m.source.name, m.title, m.author, m.match, m.author_ok,
             len(m.chapters), m.max, m.note or None, int(m.manga_id == primary_manga_id), now()))
    have = plan.have()
    for n, m in plan.assignment.items():
        status = "have" if n in have else "wanted"
        con.execute(
            "INSERT INTO chapter (series_id, number, status, manga_id, source_name, updated_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(series_id, number) DO UPDATE SET status=excluded.status,"
            " manga_id=excluded.manga_id, source_name=excluded.source_name, updated_at=excluded.updated_at",
            (series_id, n, status, m.manga_id, m.source.name, now()))


def record_results(con, series_id: int, results: dict) -> None:
    for n, r in results.items():
        con.execute("UPDATE chapter SET status=?, updated_at=? WHERE series_id=? AND number=?",
                    ("have" if r == "ok" else "failed", now(), series_id, n))


def series_rows(con):
    return con.execute(
        "SELECT s.*, "
        " (SELECT COUNT(*) FROM chapter c WHERE c.series_id=s.id) AS listed,"
        " (SELECT COUNT(*) FROM chapter c WHERE c.series_id=s.id AND c.status='have') AS have,"
        " (SELECT COUNT(*) FROM chapter c WHERE c.series_id=s.id AND c.status IN ('wanted','failed')) AS wanted"
        " FROM series s ORDER BY s.title COLLATE NOCASE").fetchall()
