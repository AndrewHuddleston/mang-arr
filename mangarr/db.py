"""SQLite store: which series are tracked, which source entries belong to
them, and the state of every chapter."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
  anilist_id      INTEGER PRIMARY KEY,
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
  anilist_id      INTEGER NOT NULL REFERENCES series(anilist_id) ON DELETE CASCADE,
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
  PRIMARY KEY (anilist_id, manga_id)
);
CREATE TABLE IF NOT EXISTS chapter (
  anilist_id      INTEGER NOT NULL REFERENCES series(anilist_id) ON DELETE CASCADE,
  number          REAL NOT NULL,
  status          TEXT NOT NULL,      -- wanted | have | failed | unavailable
  manga_id        INTEGER,            -- source entry chosen for it
  source_name     TEXT,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (anilist_id, number)
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


def upsert_series(con, s) -> None:
    con.execute(
        "INSERT INTO series (anilist_id, title, romaji, english, native, synonyms, country,"
        " status, format, expected, authors, cover, added_at, last_resolved)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(anilist_id) DO UPDATE SET title=excluded.title, romaji=excluded.romaji,"
        " english=excluded.english, native=excluded.native, synonyms=excluded.synonyms,"
        " country=excluded.country, status=excluded.status, format=excluded.format,"
        " expected=excluded.expected, authors=excluded.authors, cover=excluded.cover,"
        " last_resolved=excluded.last_resolved",
        (s.anilist_id, s.title, s.romaji, s.english, s.native, json.dumps(s.synonyms),
         s.country, s.status, s.format, s.chapters, json.dumps(s.authors), s.cover, now(), now()))


def save_plan(con, plan, primary_manga_id: int | None) -> None:
    aid = plan.series.anilist_id
    con.execute("DELETE FROM series_source WHERE anilist_id=?", (aid,))
    for m in plan.matches:
        con.execute(
            "INSERT INTO series_source (anilist_id, manga_id, source_name, title, author,"
            " match_level, author_level, chapter_count, max_chapter, note, is_primary, seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, m.manga_id, m.source.name, m.title, m.author, m.match, m.author_ok,
             len(m.chapters), m.max, m.note or None, int(m.manga_id == primary_manga_id), now()))
    have = plan.have()
    for n, m in plan.assignment.items():
        status = "have" if n in have else "wanted"
        con.execute(
            "INSERT INTO chapter (anilist_id, number, status, manga_id, source_name, updated_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(anilist_id, number) DO UPDATE SET status=excluded.status,"
            " manga_id=excluded.manga_id, source_name=excluded.source_name, updated_at=excluded.updated_at",
            (aid, n, status, m.manga_id, m.source.name, now()))


def record_results(con, anilist_id: int, results: dict) -> None:
    for n, r in results.items():
        con.execute("UPDATE chapter SET status=?, updated_at=? WHERE anilist_id=? AND number=?",
                    ("have" if r == "ok" else "failed", now(), anilist_id, n))


def series_rows(con):
    return con.execute(
        "SELECT s.*, "
        " (SELECT COUNT(*) FROM chapter c WHERE c.anilist_id=s.anilist_id) AS listed,"
        " (SELECT COUNT(*) FROM chapter c WHERE c.anilist_id=s.anilist_id AND c.status='have') AS have,"
        " (SELECT COUNT(*) FROM chapter c WHERE c.anilist_id=s.anilist_id AND c.status IN ('wanted','failed')) AS wanted"
        " FROM series s ORDER BY s.title COLLATE NOCASE").fetchall()
