"""Backups of the database, Sonarr-style: taken on a schedule and on demand,
kept under <data>/backups with the oldest pruned, downloadable, and
restorable in place (the live database is replaced through SQLite's
online-backup API, so nothing needs a restart)."""
import logging
import os
import re
import sqlite3
import threading
import time

from . import config, db

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^mangarr-\d{8}-\d{6}(?:-\d+)?\.db$")
KEEP = int(os.environ.get("MANGARR_BACKUPS_KEEP", "7"))
INTERVAL_HOURS = float(os.environ.get("MANGARR_BACKUP_HOURS", "24"))


def backup_dir() -> str:
    d = os.path.join(config.DATA_DIR, "backups")
    os.makedirs(d, exist_ok=True)
    return d


def create(reason: str = "manual") -> str:
    """Write a consistent copy of the live database; returns its path."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(backup_dir(), f"mangarr-{stamp}.db")
    n = 1
    while os.path.exists(path):                    # several in one second
        path = os.path.join(backup_dir(), f"mangarr-{stamp}-{n}.db")
        n += 1
    with db.connect() as con:
        dst = sqlite3.connect(path)
        try:
            con.backup(dst)
        finally:
            dst.close()
    log.info("backup written (%s): %s, %d bytes", reason, path, os.path.getsize(path))
    prune()
    return path


def listing() -> list[dict]:
    out = []
    for name in sorted(os.listdir(backup_dir()), reverse=True):
        if NAME_RE.match(name):
            p = os.path.join(backup_dir(), name)
            out.append({"name": name, "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
    return out


def prune(keep: int = KEEP) -> int:
    old = listing()[keep:]
    for b in old:
        os.remove(os.path.join(backup_dir(), b["name"]))
        log.info("backup pruned: %s", b["name"])
    return len(old)


def path_of(name: str) -> str:
    """Validated path of a named backup (names are our own pattern only)."""
    if not NAME_RE.match(name):
        raise ValueError(f"not a backup name: {name!r}")
    p = os.path.join(backup_dir(), name)
    if not os.path.isfile(p):
        raise FileNotFoundError(name)
    return p


def delete(name: str) -> None:
    os.remove(path_of(name))
    log.info("backup deleted: %s", name)


def verify(path: str) -> tuple[bool, str]:
    """Is this file a mang-arr database in good shape?"""
    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ok = src.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = src.execute("PRAGMA user_version").fetchone()[0]
            n = src.execute("SELECT COUNT(*) FROM series").fetchone()[0] if "series" in tables else 0
        finally:
            src.close()
    except sqlite3.DatabaseError as e:
        return False, f"not a SQLite database: {e}"
    if ok != "ok":
        return False, f"integrity check failed: {ok}"
    if "series" not in tables or "chapter" not in tables:
        return False, "not a mang-arr database (no series/chapter tables)"
    if version > len(db.MIGRATIONS):
        return False, f"database schema {version} is newer than this version understands ({len(db.MIGRATIONS)})"
    return True, f"schema {version}, {n} series"


def restore(path: str) -> str:
    """Replace the live database with the given file (after a safety backup
    of the current one). Returns a description. The caller must make sure
    no job is running."""
    ok, msg = verify(path)
    if not ok:
        raise ValueError(msg)
    safety = create("before restore")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with db.connect() as con:
            src.backup(con)
            db.migrate(con)                       # an older backup is upgraded in place
    finally:
        src.close()
    log.warning("database restored from %s (%s); previous state saved as %s", path, msg, safety)
    return f"restored ({msg}); the previous database was saved as {os.path.basename(safety)}"


def start_background(interval_hours: float = INTERVAL_HOURS, first_delay: float = 300.0) -> None:
    def loop():
        time.sleep(first_delay)
        while True:
            try:
                last = listing()
                due = not last or time.time() - last[0]["mtime"] > interval_hours * 3600
                if due:
                    create("scheduled")
            except Exception:
                log.exception("scheduled backup failed")
            time.sleep(600)
    threading.Thread(target=loop, name="mangarr-backups", daemon=True).start()
    log.info("scheduled backups: every %.0fh, keeping %d, in %s", interval_hours, KEEP, backup_dir())
