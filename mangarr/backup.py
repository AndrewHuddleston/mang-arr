"""Backups of the database, Sonarr-style: taken on a schedule and on demand,
kept under <data>/backups with the oldest pruned, downloadable, and
restorable in place (the live database is replaced through SQLite's
online-backup API, so nothing needs a restart).

Safety rules, because a backup file can come from anywhere (an upload):

- A backup is written under a temporary name and renamed only once it is
  complete and passes an integrity check, so a failed or interrupted backup
  never shows up as a (broken) restore point or pushes good ones out.
- Backups and the database are private to the owner (0600, folder 0700):
  they hold the login password, the API key and notifier tokens.
- A restore never touches the live database until the replacement is ready:
  the file is copied aside first (so pruning cannot delete it mid-restore),
  rebuilt into a fresh database from mang-arr's own schema (only known
  tables and columns are copied, so no triggers, views or extra tables come
  along), upgraded to the current schema, and its stored file paths are
  checked against the library and staging roots. Only then is the live
  database swapped, after a safety backup of it.
- The current login, API key and session settings are kept across a
  restore: rolling data back must not revive old credentials or turn the
  login off.
"""
import contextlib
import fcntl
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.parse

from . import config, db, library

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^mangarr-(\d{8}-\d{6})(?:-(\d+))?\.db$")
KEEP = int(os.environ.get("MANGARR_BACKUPS_KEEP", "7"))
INTERVAL_HOURS = float(os.environ.get("MANGARR_BACKUP_HOURS", "24"))
# Largest backup accepted through the upload form (MB); a bigger live database raises it to twice its size.
UPLOAD_MAX_MB = float(os.environ.get("MANGARR_BACKUP_UPLOAD_MAX_MB", "512"))
# Event history kept (Activity/series pages); older events are pruned before each scheduled backup.
EVENTS_KEEP_DAYS = float(os.environ.get("MANGARR_EVENTS_KEEP_DAYS", "90"))
EVENTS_KEEP_ROWS = int(os.environ.get("MANGARR_EVENTS_KEEP_ROWS", "20000"))

# Security settings a restore keeps from the CURRENT database instead of the
# backup's (a key the current database does not have is removed from the
# restored one too, so an old login cannot come back).
PRESERVED_SETTINGS = ("auth_method", "auth_user", "auth_password", "api_key", "session_epoch", "session_secret",
                      "allowed_hosts")

_TMP_PREFIX = ".tmp-"            # work in progress inside the backups folder (never listed as a backup)
_lock = threading.RLock()        # one backup, prune or restore at a time in this process


class RestoreError(ValueError):
    """A restore was refused or failed; the live database was not changed."""


# What backup, download and restore operations raise when they fail; the web layer reports these to the user.
FAILURES = (ValueError, OSError, sqlite3.Error)


def backup_dir() -> str:
    d = os.path.join(config.DATA_DIR, "backups")
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        if os.stat(d).st_mode & 0o077:
            os.chmod(d, 0o700)
    except OSError as e:
        log.warning("cannot make the backups folder %s private (chmod 700): %s", d, e)
    return d


def _remove(path: str) -> None:
    """Remove a database file with its -wal/-shm/-journal companions."""
    for p in (path, path + "-wal", path + "-shm", path + "-journal"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def _clean_stale_temp(max_age: float = 86400) -> None:
    """Remove leftovers of backups, downloads and restores that a crash interrupted."""
    d = backup_dir()
    for name in os.listdir(d):
        if not name.startswith(_TMP_PREFIX):
            continue
        p = os.path.join(d, name)
        try:
            if time.time() - os.path.getmtime(p) < max_age:
                continue
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
            log.info("removed stale temporary backup file %s", name)
        except OSError as e:
            log.warning("cannot remove stale temporary backup file %s: %s", p, e)


def _ensure_live() -> None:
    """Create (and migrate) the live database when there is none yet."""
    if not os.path.exists(config.DB_PATH):
        with db.connect():
            pass


def _snapshot(dst: str) -> None:
    """Write a consistent copy of the live database into dst (an empty file
    of ours). The live database is opened without migrating it, so this also
    works when it cannot be migrated. The copy uses a rollback journal: one
    self-contained file that reading never adds -wal/-shm files to."""
    _ensure_live()
    src = sqlite3.connect(config.DB_PATH, timeout=30)
    try:
        out = sqlite3.connect(dst)
        try:
            src.backup(out)
            out.execute("PRAGMA journal_mode = DELETE")
        finally:
            out.close()
    finally:
        src.close()


def _integrity(path: str) -> str:
    """'ok', or what is wrong with the SQLite file."""
    try:
        c = sqlite3.connect(_ro_uri(path), uri=True)
        try:
            return c.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            c.close()
    except sqlite3.DatabaseError as e:
        return f"not a SQLite database: {e}"


def _free_name(d: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(d, f"mangarr-{stamp}.db")
    n = 1
    while os.path.exists(path):                    # several in one second
        path = os.path.join(d, f"mangarr-{stamp}-{n}.db")
        n += 1
    return path


def create(reason: str = "manual", prune_after: bool = True) -> str:
    """Write a consistent copy of the live database; returns its path. It is
    written under a temporary name and renamed only once it is complete and
    passes an integrity check, so a failure leaves nothing that looks like a
    backup."""
    with _lock:
        d = backup_dir()
        fd, tmp = tempfile.mkstemp(prefix=_TMP_PREFIX, suffix=".db", dir=d)   # created 0600
        os.close(fd)
        try:
            _snapshot(tmp)
            ok = _integrity(tmp)
            if ok != "ok":
                raise sqlite3.DatabaseError(f"the new backup failed its integrity check: {ok}")
            path = _free_name(d)
            os.replace(tmp, path)
        except BaseException as e:
            _remove(tmp)
            log.error("backup (%s) failed, nothing kept: %s", reason, e)
            raise
        log.info("backup written (%s): %s, %d bytes", reason, path, os.path.getsize(path))
        if prune_after:
            prune()
        return path


def snapshot_for_download() -> tuple[str, str]:
    """(temporary path, download name) of a fresh copy of the live database,
    for a download. It is not a kept backup: nothing is rotated or pruned,
    and the caller deletes the file once it has been sent."""
    fd, tmp = tempfile.mkstemp(prefix=_TMP_PREFIX + "download-", suffix=".db", dir=backup_dir())
    os.close(fd)
    try:
        _snapshot(tmp)
    except BaseException:
        _remove(tmp)
        raise
    return tmp, f"mangarr-{time.strftime('%Y%m%d-%H%M%S')}.db"


def _sort_key(name: str) -> tuple[str, int]:
    m = NAME_RE.match(name)
    return (m.group(1), int(m.group(2) or 0)) if m else ("", 0)


def listing() -> list[dict]:
    """Kept backups, newest first (by the time in the name, then the
    same-second counter, so mangarr-X-10.db sorts after mangarr-X-9.db)."""
    d = backup_dir()
    out = []
    for name in sorted((n for n in os.listdir(d) if NAME_RE.match(n)), key=_sort_key, reverse=True):
        p = os.path.join(d, name)
        try:
            out.append({"name": name, "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
        except OSError:                           # deleted meanwhile
            continue
    return out


def prune(keep: int | None = None, protect: str | None = None) -> int:
    """Delete all but the newest `keep` (default KEEP) backups, never the one named `protect`."""
    keep = KEEP if keep is None else keep
    with _lock:
        old = [b for b in listing()[keep:] if b["name"] != protect]
        for b in old:
            _remove(os.path.join(backup_dir(), b["name"]))
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
    with _lock:
        _remove(path_of(name))
    log.info("backup deleted: %s", name)


def _ro_uri(path: str) -> str:
    # immutable: read without locking and without creating -wal/-shm files next to it
    return f"file:{urllib.parse.quote(os.path.abspath(path))}?mode=ro&immutable=1"


def verify(path: str) -> tuple[bool, str]:
    """Is this file a mang-arr database in good shape?"""
    try:
        src = sqlite3.connect(_ro_uri(path), uri=True)
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
    if version < 1:
        return False, "not a mang-arr database (schema version 0)"
    if version > len(db.MIGRATIONS):
        return False, f"database schema {version} is newer than this version understands ({len(db.MIGRATIONS)})"
    return True, f"schema {version}, {n} series"


def upload_limit() -> int:
    """Largest backup accepted through the upload form, in bytes."""
    try:
        live = os.path.getsize(config.DB_PATH)
    except OSError:
        live = 0
    return int(max(UPLOAD_MAX_MB * 1024 * 1024, 2 * live))


# -- restore ------------------------------------------------------------------

def _stage_copy(source, dst: str, limit: int | None = None) -> None:
    """Copy the file to restore (a path, or an open binary file) to a private
    work file first: the original may be pruned or changed while the restore
    runs, and reading our copy leaves nothing next to the original."""
    if isinstance(source, (str, os.PathLike)):
        shutil.copyfile(source, dst)
        return
    source.seek(0)
    n = 0
    with open(dst, "wb") as out:
        while chunk := source.read(1 << 20):
            n += len(chunk)
            if limit is not None and n > limit:
                raise RestoreError(f"the file is larger than the upload limit ({limit // 1048576} MB)")
            out.write(chunk)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _rebuild(src_path: str, dst_path: str, page_size: int) -> tuple[int, list[str]]:
    """Build a fresh database at dst_path from mang-arr's own migrations and
    copy the known columns of the known tables across from src_path, then
    upgrade it to the current schema. Nothing else in the file (triggers,
    views, extra tables or indexes, altered column definitions) survives.
    Returns (the backup's schema version, notes for the user)."""
    notes: list[str] = []
    con = sqlite3.connect(f"file:{urllib.parse.quote(os.path.abspath(dst_path))}", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute(f"PRAGMA page_size = {int(page_size)}")      # must match the live file for the swap
        con.execute("PRAGMA journal_mode = DELETE")
        con.execute("PRAGMA trusted_schema = OFF")                # the attached file's schema is untrusted
        con.execute("ATTACH DATABASE ? AS src", (_ro_uri(src_path),))
        version = con.execute("PRAGMA src.user_version").fetchone()[0]
        if version < 1:
            raise RestoreError("not a mang-arr database (schema version 0)")
        if version > len(db.MIGRATIONS):
            raise RestoreError(f"database schema {version} is newer than this version understands "
                               f"({len(db.MIGRATIONS)})")
        db.migrate(con, target=version)                           # the schema the backup says it has
        want = [r[0] for r in con.execute("SELECT name FROM main.sqlite_master WHERE type='table'"
                                          " AND name NOT LIKE 'sqlite!_%' ESCAPE '!' ORDER BY rowid")]
        found = {r[0]: (r[1], r[2] or "") for r in con.execute("SELECT name, type, sql FROM src.sqlite_master")}
        skipped = sorted(f"{typ} {name}" for name, (typ, _) in found.items()
                         if not name.startswith("sqlite_")
                         and (typ in ("trigger", "view") or (typ == "table" and name not in want)))
        if skipped:
            log.warning("restore: not carried over from the backup (not part of the schema): %s", ", ".join(skipped))
            notes.append(f"ignored {len(skipped)} object(s) that are not part of mang-arr's schema")
        con.execute("BEGIN")
        for t in want:
            typ, sql = found.get(t, (None, ""))
            if typ != "table" or sql.upper().startswith("CREATE VIRTUAL"):
                raise RestoreError(f"not a mang-arr database: table {t} is missing for schema {version}")
            cols = [r[1] for r in con.execute(f"PRAGMA main.table_info({_q(t)})")]
            have = {r[1] for r in con.execute(f"PRAGMA src.table_info({_q(t)})")}
            missing = [c for c in cols if c not in have]
            if missing:
                raise RestoreError(f"table {t} lacks column(s) {', '.join(missing)} that schema {version} has")
            names = ", ".join(_q(c) for c in cols)
            con.execute(f"INSERT INTO main.{_q(t)} ({names}) SELECT {names} FROM src.{_q(t)}")
        con.commit()
        con.execute("DETACH DATABASE src")
        orphans = 0
        for t in ("series_source", "chapter"):
            orphans += con.execute(f"DELETE FROM {t} WHERE series_id NOT IN (SELECT id FROM series)").rowcount
        if orphans:
            log.warning("restore: dropped %d source/chapter row(s) of series that are not in the backup", orphans)
        con.commit()
        db.migrate(con)                                           # an older backup is upgraded here, not live
        notes += _sanitise_paths(con)
        con.commit()
        return version, notes
    except sqlite3.Error as e:
        con.rollback()
        raise RestoreError(f"the backup cannot be used: {type(e).__name__}: {e}") from e
    finally:
        con.close()


def _sanitise_paths(con) -> list[str]:
    """Stored paths are acted on later (files deleted, links made), so a
    restored database may only point inside the library and staging roots.
    Series folders must be single folder names, one per series; anything
    else is replaced (folders) or cleared (paths: the next import fills
    them in again)."""
    notes = []
    taken: set[str] = set()
    renamed = 0
    for r in con.execute("SELECT id, ref, title, folder FROM series ORDER BY id").fetchall():
        folder = r["folder"]
        if db.valid_folder(folder) and folder not in taken:
            taken.add(folder)
            continue
        new = library.unique_folder(r["title"] or "untitled", taken, r["ref"] or str(r["id"]))
        taken.add(new)
        con.execute("UPDATE series SET folder=? WHERE id=?", (new, r["id"]))
        log.warning("restore: series #%d folder %r is not a plain folder name of its own; now %r",
                    r["id"], folder, new)
        renamed += 1
    if renamed:
        notes.append(f"{renamed} series folder name(s) replaced")
    cleared = 0
    checks = (("chapter", "library_path", config.LIBRARY_ROOT), ("chapter", "staging_path", config.STAGING_ROOT),
              ("series_source", "folder", config.STAGING_ROOT))
    for table, col, root in checks:
        bad = {r[0] for r in con.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL")
               if not library.is_within(r[0], root)}
        for p in sorted(bad):
            log.warning("restore: %s.%s %r is outside %s; cleared", table, col, p, root)
            cleared += con.execute(f"UPDATE {table} SET {col}=NULL WHERE {col}=?", (p,)).rowcount
    # without a folder, the staging folder is derived from the source name and title: keep that inside too
    # (the entry is dropped; the next refresh of the series lists its sources again)
    for r in con.execute("SELECT series_id, manga_id, source_name, title FROM series_source WHERE folder IS NULL"
                         ).fetchall():
        derived = os.path.join(config.STAGING_ROOT, r["source_name"] or "", library.safe_title(r["title"] or ""))
        if not library.is_within(derived, config.STAGING_ROOT):
            log.warning("restore: source entry %r of series #%d would point outside %s; dropped",
                        r["source_name"], r["series_id"], config.STAGING_ROOT)
            con.execute("DELETE FROM series_source WHERE series_id=? AND manga_id=?", (r["series_id"], r["manga_id"]))
            cleared += 1
    if cleared:
        notes.append(f"{cleared} stored path(s) outside the library/staging folders cleared")
    return notes


def _current_settings() -> dict[str, str] | None:
    """The preserved settings rows of the live database, or None when it
    cannot be read."""
    try:
        c = sqlite3.connect(config.DB_PATH, timeout=30)
        try:
            marks = ",".join("?" for _ in PRESERVED_SETTINGS)
            return dict(c.execute(f"SELECT key, value FROM setting WHERE key IN ({marks})", PRESERVED_SETTINGS))
        finally:
            c.close()
    except sqlite3.Error as e:
        log.warning("restore: cannot read the current login/API key settings (%s); the backup's are used", e)
        return None


def _carry_settings(path: str, current: dict[str, str]) -> None:
    """Put the current security settings into the database about to be restored."""
    con = sqlite3.connect(path)
    try:
        for k in PRESERVED_SETTINGS:
            if k in current:
                con.execute("INSERT INTO setting (key, value) VALUES (?, ?)"
                            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, current[k]))
            else:
                con.execute("DELETE FROM setting WHERE key=?", (k,))
        con.commit()
    finally:
        con.close()


@contextlib.contextmanager
def _no_download_run():
    """Hold the download lock (without waiting) while the database is swapped,
    so a download run of another process (the CLI) cannot write into it."""
    try:
        f = open(config.LOCK_PATH, "a")
    except OSError as e:                       # no data folder yet / not ours: no download run of ours holds it
        log.info("restore: cannot open the download lock %s (%s); not waiting for download runs",
                 config.LOCK_PATH, e)
        yield
        return
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RestoreError("a download run is in progress (another mang-arr process); try again later") from e
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _page_size() -> int:
    try:
        c = sqlite3.connect(config.DB_PATH, timeout=30)
        try:
            return c.execute("PRAGMA page_size").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return 4096


def restore(source, keep_auth: bool = True) -> str:
    """Replace the live database with a backup: the path of a file, or an
    open binary file (an upload, limited to upload_limit()). Returns a
    description; raises RestoreError (a ValueError) when the file is refused
    or cannot be used, with the live database unchanged. Jobs must not run
    meanwhile: the web layer runs this on the job runner's own thread."""
    with _lock:
        _ensure_live()
        work = tempfile.mkdtemp(prefix=_TMP_PREFIX + "restore-", dir=backup_dir())
        try:
            staged, fresh = os.path.join(work, "source.db"), os.path.join(work, "restored.db")
            _stage_copy(source, staged, None if isinstance(source, (str, os.PathLike)) else upload_limit())
            ok, msg = verify(staged)
            if not ok:
                raise RestoreError(msg)
            version, notes = _rebuild(staged, fresh, _page_size())
            if keep_auth:
                current = _current_settings()
                if current is None:
                    notes.append("the current login settings were unreadable, so the backup's are in use")
                else:
                    _carry_settings(fresh, current)
                    notes.append("login and API key kept as they are now")
            with _no_download_run():
                try:
                    safety = create("before restore", prune_after=False)
                    live = sqlite3.connect(config.DB_PATH, timeout=30)
                    try:
                        src = sqlite3.connect(fresh)
                        try:
                            src.backup(live)              # one step: other connections see old or new, never half
                        finally:
                            src.close()
                    finally:
                        live.close()
                except sqlite3.Error as e:
                    raise RestoreError(f"the current database could not be saved or replaced ({e}); stop "
                                       f"mang-arr and copy a backup over {config.DB_PATH} by hand") from e
        except RestoreError as e:
            log.warning("restore of %s refused, the database is unchanged: %s",
                        source if isinstance(source, str) else "an uploaded file", e)
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
        db.restrict_permissions(config.DB_PATH)
        from . import settings
        with db.connect() as con:
            settings.refresh(con)                        # the middleware must see the restored settings now
            n = con.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        protect = os.path.basename(source) if isinstance(source, str) else None
        prune(protect=protect)       # only now, and never the file just restored (the next backup rotates it out)
    what = source if isinstance(source, str) else "an uploaded file"
    extra = "; " + "; ".join(notes) if notes else ""
    log.warning("database restored from %s (schema %d, %d series%s); previous state saved as %s",
                what, version, n, extra, safety)
    return (f"restored (schema {version}, {n} series{extra}); the previous database was saved as "
            f"{os.path.basename(safety)}")


# -- housekeeping -------------------------------------------------------------

def prune_history(keep_days: float = EVENTS_KEEP_DAYS, keep_rows: int = EVENTS_KEEP_ROWS) -> int:
    """Trim the event history; compact the file after a large trim."""
    with db.connect() as con:
        n = db.prune_events(con, keep_days, keep_rows)
        if n >= 5000:
            con.execute("VACUUM")
            log.info("database compacted after pruning %d events", n)
    return n


def start_background(interval_hours: float = INTERVAL_HOURS, first_delay: float = 300.0) -> None:
    try:
        d = backup_dir()
    except OSError as e:
        log.error("scheduled backups disabled: cannot create the backups folder: %s", e)
        return

    def loop():
        time.sleep(first_delay)
        while True:
            try:
                _clean_stale_temp()
                last = listing()
                due = not last or time.time() - last[0]["mtime"] > interval_hours * 3600
                if due:
                    try:
                        prune_history()
                    except Exception:
                        log.exception("event history pruning failed")
                    create("scheduled")
            except Exception:
                log.exception("scheduled backup failed")
            time.sleep(600)
    threading.Thread(target=loop, name="mangarr-backups", daemon=True).start()
    log.info("scheduled backups: every %.0fh, keeping %d, in %s", interval_hours, KEEP, d)
