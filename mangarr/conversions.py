"""E-reader copies of library chapters: the targets, the queue and the
service that works through it (Settings -> Media Management -> E-reader
Conversion).

A target is what to make and where it goes: a reader profile, a format
(EPUB, Kobo KEPUB, CBZ, PDF) and a folder of its own under CONVERTED_ROOT.
Every chapter the library has gets one copy per enabled target:

    <CONVERTED_ROOT>/<target folder>/<series folder>/<series folder> - <chapter file name>.epub

one folder per series, and the series is in every file name and in every
book's metadata, so reader apps that ignore folders still group the
chapters. The library is only read. Conversion is off until it is switched
on and a target exists, and it needs Pillow (convert.available).

The queue is the table `conversion`: a row per chapter and target.
reconcile() makes the rows the library calls for and notices what changed
(a file replaced, options changed, an output gone); the service thread
takes the rows that are due, most urgent first, and converts each in a
process of its own (convert/worker.py) with a memory limit and a time
limit, so a bad page cannot take the server down. It is a thread of its
own, not the job runner: imports and refreshes never wait behind a back
catalogue being converted.

Nothing outside CONVERTED_ROOT is ever written or removed, only files this
module recorded are removed, and a file it did not make is never
overwritten by a rename (follow_file, follow_folder: converted copies are
renamed together with their library files).
"""
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict

from . import config, convert, db, library, limits, metrics, naming, settings
from .convert import profiles

log = logging.getLogger(__name__)

MAX_TARGETS = 8
MAX_NAME = 60
SCOPES = ("all", "new")
RETRY_MINUTES = (10, 60, 360, 1440)     # after the 1st, 2nd ... failure that may pass; then it stays failed
IDLE_SECS = 60.0                        # the service looks at the queue at least this often
FIRST_RECONCILE_SECS = 60.0             # after a start
RECONCILE_EVERY_SECS = 24 * 3600.0
NO_PROGRESS_SECS = 120.0                # a conversion that says nothing for this long is stopped
STALE_EXTRA_SECS = 300.0
KILL_WAIT_SECS = 5.0
MAX_OUTPUT_MB = 4096
CPU_SECS_PER_CHAPTER = 3.0              # measured: about 3 CPU seconds for a 20 page chapter
PART_PREFIX, PART_SUFFIX = ".mangarr-", ".part"
EXISTED_BEFORE = "existed before this target was added (Convert existing chapters makes a copy)"
_TAG = re.compile(r"<[^<>]+>")

_spawn: Callable | None = None          # tests replace the worker process (see _start_worker)


class TargetError(ValueError):
    """A target that cannot be saved; the message says why, in plain words."""


# -- folders and names ---------------------------------------------------------------------------

def root() -> str:
    return config.CONVERTED_ROOT


def root_problem() -> str | None:
    """Why the output folder cannot be used (None: it can): it must not be
    inside the library or staging, nor hold them."""
    r = root()
    for name, other in (("library", config.LIBRARY_ROOT), ("staging", config.STAGING_ROOT)):
        if library.is_within(r, other):
            return (f"the output folder {r} is inside the {name} folder: Komga would show every copy as a second "
                    "book. Set MANGARR_CONVERTED to a folder outside it")
        if library.is_within(other, r):
            return f"the output folder {r} holds the {name} folder. Set MANGARR_CONVERTED to a folder of its own"
    return None


def valid_folder(name) -> bool:
    """One plain, visible path component that mang-arr's own names leave as it is."""
    return (db.valid_folder(name) and not name.startswith(".") and "/" not in name and os.sep not in name
            and name == library.safe_title(name) and len(name.encode("utf-8")) <= 100)


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "copies"


def target_dir(target) -> str:
    return os.path.join(root(), target["folder"])


def output_name(series_folder: str, library_path: str, fmt: str) -> str:
    """'<series folder> - <chapter file name>' with the format's extension,
    cut to fit a file name."""
    stem = os.path.splitext(os.path.basename(library_path))[0]
    ext = convert.FORMATS[fmt]
    return naming.fit_name(f"{series_folder} - {stem}", keep=ext)


def output_path(target, series_folder: str, library_path: str) -> str:
    return os.path.join(target_dir(target), series_folder, output_name(series_folder, library_path, target["format"]))


def _ours(path: str | None, target) -> bool:
    """Is this a path inside the target's folder that may be acted on?"""
    return bool(path) and library.is_within(path, target_dir(target)) and library.is_within(path, root()) \
        and root_problem() is None


def signature(path: str) -> str | None:
    """'inode:size:mtime_ns' of a regular file, None when it is not one."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return f"{st.st_ino}:{st.st_size}:{st.st_mtime_ns}" if stat.S_ISREG(st.st_mode) else None


# -- targets -------------------------------------------------------------------------------------

def targets(con, enabled_only: bool = False) -> list[dict]:
    rows = con.execute("SELECT * FROM convert_target" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id")
    return [_target(r) for r in rows.fetchall()]


def get_target(con, target_id: int) -> dict | None:
    r = con.execute("SELECT * FROM convert_target WHERE id=?", (int(target_id),)).fetchone()
    return _target(r) if r else None


def _target(r) -> dict:
    d = dict(r)
    try:
        d["options"] = json.loads(d["options"] or "{}")
    except ValueError:
        d["options"] = {}
    if not isinstance(d["options"], dict):
        d["options"] = {}
    d["enabled"] = bool(d["enabled"])
    return d


def engine_options(target: dict) -> convert.Options:
    """The engine's Options of a target (ValueError when what is stored
    cannot be used)."""
    return convert.Options.from_dict({**target["options"], "profile": target["profile"], "format": target["format"]})


def validate_target(d: dict, taken: list[str] = ()) -> dict:
    """A target as it is stored, from a form or JSON. TargetError with the
    reason when it cannot be used. taken: the folders of the other targets."""
    if not isinstance(d, dict):
        raise TargetError("a target is a set of fields")
    name = re.sub(r"\s+", " ", str(d.get("name") or "")).strip()
    if not name or len(name) > MAX_NAME or any(ord(c) < 32 for c in name):
        raise TargetError(f"Name: 1 to {MAX_NAME} characters")
    profile = str(d.get("profile") or profiles.DEFAULT_PROFILE)
    if profile not in profiles.PROFILES:
        raise TargetError("Device: choose one from the list")
    prof = profiles.PROFILES[profile]
    fmt = str(d.get("format") or prof.default_format)
    if fmt not in prof.formats:
        raise TargetError(f"Format: {prof.name} offers {', '.join(prof.formats)}")
    folder = str(d.get("folder") or "").strip() or fmt
    if not valid_folder(folder):
        raise TargetError("Folder: one plain folder name (no /, not starting with a dot)")
    if naming.folder_key(folder) in {naming.folder_key(t) for t in taken}:
        raise TargetError(f"Folder: {folder!r} is the folder of another target")
    scope = str(d.get("scope") or "all")
    if scope not in SCOPES:
        raise TargetError("Existing Chapters: all or new")
    options = d.get("options") or {}
    if not isinstance(options, dict):
        raise TargetError("options must be a set of fields")
    options = {k: v for k, v in options.items() if k not in ("profile", "format")}
    try:
        checked = convert.Options.from_dict({**options, "profile": profile, "format": fmt})
    except (ValueError, TypeError) as e:
        raise TargetError(str(e)) from e
    stored = {k: v for k, v in asdict(checked).items() if k not in ("profile", "format")}
    return {"name": name, "profile": profile, "format": fmt, "folder": folder, "scope": scope,
            "enabled": d.get("enabled") not in (False, 0, "0", "false", "off", ""), "options": stored}


def default_target() -> dict:
    """What turning conversion on makes when there is no target yet."""
    prof = profiles.PROFILES[profiles.DEFAULT_PROFILE]
    return {"name": prof.name, "profile": prof.key, "format": prof.default_format, "folder": prof.default_format,
            "scope": "new", "enabled": True, "options": {}}


def add_target(con, d: dict) -> int:
    """Store a new target. With scope 'new' the chapters the library has
    now are marked as not to be converted (Convert existing takes that
    back); with 'all' they are queued by the next reconcile."""
    have = targets(con)
    if len(have) >= MAX_TARGETS:
        raise TargetError(f"at most {MAX_TARGETS} targets")
    t = validate_target(d, [x["folder"] for x in have])
    cur = con.execute("INSERT INTO convert_target (name, enabled, profile, format, folder, options, scope, created_at)"
                      " VALUES (?,?,?,?,?,?,?,?)", (t["name"], int(t["enabled"]), t["profile"], t["format"],
                                                    t["folder"], json.dumps(t["options"]), t["scope"], db.now()))
    tid = cur.lastrowid
    if t["scope"] == "new":
        con.execute("INSERT OR IGNORE INTO conversion (series_id, number, target_id, status, priority, reason,"
                    " updated_at) SELECT series_id, number, ?, 'skipped', 2, ?, ? FROM chapter WHERE status='have'"
                    " AND library_path IS NOT NULL", (tid, EXISTED_BEFORE, db.now()))
    con.commit()
    log.info("conversion target #%d added: %s (%s, %s) in %s, %s chapters", tid, t["name"], t["profile"],
             t["format"], t["folder"], "existing and new" if t["scope"] == "all" else "only new")
    return tid


def update_target(con, target_id: int, d: dict) -> dict:
    """Change a target. Its folder can be renamed (the copies move with it,
    never over an existing folder); changed options re-convert what was
    made, at the next reconcile."""
    old = get_target(con, target_id)
    if old is None:
        raise LookupError("no such target")
    t = validate_target({**old, **d}, [x["folder"] for x in targets(con) if x["id"] != old["id"]])
    if (t["profile"], t["format"]) != (old["profile"], old["format"]) and t["format"] != old["format"]:
        pass                                                # another extension: reconcile makes the copies again
    if t["folder"] != old["folder"]:
        src, dst = target_dir(old), os.path.join(root(), t["folder"])
        if os.path.lexists(dst):
            raise TargetError(f"Folder: {t['folder']!r} is already in the output folder")
        if os.path.isdir(src) and not os.path.islink(src) and library.is_within(src, root()):
            os.rename(src, dst)
        for r in con.execute("SELECT series_id, number, output_path FROM conversion WHERE target_id=? AND"
                             " output_path IS NOT NULL", (old["id"],)).fetchall():
            if library.is_within(r["output_path"], src) or r["output_path"].startswith(src + os.sep):
                con.execute("UPDATE conversion SET output_path=? WHERE series_id=? AND number=? AND target_id=?",
                            (dst + r["output_path"][len(src):], r["series_id"], r["number"], old["id"]))
    con.execute("UPDATE convert_target SET name=?, enabled=?, profile=?, format=?, folder=?, options=?, scope=?"
                " WHERE id=?", (t["name"], int(t["enabled"]), t["profile"], t["format"], t["folder"],
                                json.dumps(t["options"]), t["scope"], old["id"]))
    con.commit()
    return get_target(con, old["id"])


def delete_target(con, target_id: int, delete_files: bool = False) -> int:
    """Remove a target; with delete_files its copies too (only the files it
    recorded). Returns how many files were removed."""
    t = get_target(con, target_id)
    if t is None:
        raise LookupError("no such target")
    removed = 0
    if delete_files:
        for r in con.execute("SELECT output_path FROM conversion WHERE target_id=? AND output_path IS NOT NULL",
                             (t["id"],)).fetchall():
            removed += _remove_output(r["output_path"], t)
        _prune_empty(t)
    con.execute("DELETE FROM convert_target WHERE id=?", (t["id"],))
    con.execute("DELETE FROM conversion WHERE target_id=?", (t["id"],))
    con.commit()
    log.info("conversion target #%d (%s) removed%s", t["id"], t["name"],
             f" with {removed} file(s)" if delete_files else "; its files are kept")
    return removed


def _remove_output(path: str | None, target) -> int:
    """Remove one recorded copy: only a regular file inside the target's folder."""
    if not _ours(path, target):
        return 0
    try:
        if stat.S_ISREG(os.lstat(path).st_mode):
            os.unlink(path)
            return 1
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("could not remove the converted copy %s: %s", path, e)
    return 0


def _prune_empty(target) -> None:
    """Series folders of a target that are empty now, and the target folder
    when it is; nothing that still holds a file."""
    top = target_dir(target)
    if not (os.path.isdir(top) and not os.path.islink(top) and library.is_within(top, root())):
        return
    try:
        for name in os.listdir(top):
            d = os.path.join(top, name)
            if os.path.isdir(d) and not os.path.islink(d) and not os.listdir(d):
                os.rmdir(d)
        if not os.listdir(top):
            os.rmdir(top)
    except OSError as e:
        log.debug("could not tidy %s: %s", top, e)


def queue_existing(con, target_id: int | None = None, series_id: int | None = None, again: bool = False,
                   priority: int = 0) -> int:
    """Convert existing chapters: the ones held back (a target for new
    chapters only, a failure that gave up) are queued; with `again`, what
    was made is made again too. Returns how many rows were queued."""
    where, args = ["status IN ('skipped','failed'" + (",'done'" if again else "") + ")"], []
    if target_id is not None:
        where.append("target_id=?")
        args.append(int(target_id))
    if series_id is not None:
        where.append("series_id=?")
        args.append(int(series_id))
    cur = con.execute("UPDATE conversion SET status='pending', priority=?, reason=NULL, tries=0, next_try=NULL,"
                      " queued_at=?, updated_at=? WHERE " + " AND ".join(where),
                      (int(priority), db.now(), db.now(), *args))
    con.commit()
    return cur.rowcount


# -- how a series is read ------------------------------------------------------------------------

def direction(series) -> tuple[bool, str]:
    """(right to left?, why) for a paged series; a webtoon always reads
    left to right, whatever this says (the engine sees to that)."""
    chosen = (series["reading_direction"] if "reading_direction" in series.keys() else "auto") or "auto"
    if chosen in ("rtl", "ltr"):
        return chosen == "rtl", "set on this series"
    country = (series["country"] or "").upper()
    if country == "JP":
        return True, "Japan"
    if country:
        return False, {"KR": "Korea", "CN": "China", "TW": "Taiwan"}.get(country, country)
    return True, "unknown origin: read like a manga; change it on the series"


def layout(series) -> tuple[bool | None, dict]:
    """(webtoon: True, False or None to decide from the pages, the hints for that)."""
    chosen = (series["layout"] if "layout" in series.keys() else "auto") or "auto"
    hints = {"long_strip": True if (series["format"] or "").upper() == "WEBTOON" else None,
             "country": (series["country"] or "")[:3]}
    return {"paged": False, "webtoon": True}.get(chosen), hints


def _plain(text) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", str(text or ""))).strip()[:2000]


def meta_for(series, chapter) -> dict:
    """What goes into a book's metadata: the series (so reader apps group
    its chapters), the chapter's number and name, the authors."""
    try:
        authors = json.loads(series["authors"] or "[]")
    except (ValueError, TypeError):
        authors = []
    return {"series": series["title"], "number": chapter["number"], "chapter": chapter["name"] or "",
            "authors": [str(a) for a in authors if a][:5] if isinstance(authors, list) else [],
            "description": _plain(series["description"])}


def options_hash(target: dict, series) -> str:
    """What the output depends on besides the source file: the target's
    options and how the series is read."""
    rd = series["reading_direction"] if "reading_direction" in series.keys() else "auto"
    lo = series["layout"] if "layout" in series.keys() else "auto"
    text = json.dumps([target["profile"], target["format"], target["options"], rd, lo], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# -- the queue -----------------------------------------------------------------------------------

def enabled() -> bool:
    return bool(settings.get("convert_enabled"))


def reconcile(con, series_id: int | None = None, priority: int = 2) -> dict:
    """Make the queue agree with the library, for one series or all of
    them: a row for every chapter on disk and enabled target; what was made
    from a file that was replaced, or with other options, or is gone, is
    queued again; a copy whose library file was only renamed is renamed; a
    failure whose next try is due is queued; the copy of a chapter the
    library no longer has is removed with its row. Returns the counts."""
    out = {"new": 0, "changed": 0, "renamed": 0, "removed": 0, "retry": 0}
    if root_problem():
        return out
    now = db.now()
    every = {t["id"]: t for t in targets(con)}
    active = [t for t in every.values() if t["enabled"]] if enabled() else []
    where, args = ("WHERE s.id=?", (int(series_id),)) if series_id is not None else ("", ())
    series = {r["id"]: r for r in con.execute(f"SELECT s.* FROM series s {where}", args).fetchall()}
    chapters: dict[tuple[int, float], dict] = {}
    for r in con.execute("SELECT c.series_id, c.number, c.library_path FROM chapter c JOIN series s ON s.id=c.series_id"
                         f" {where}{' AND' if where else ' WHERE'} c.status='have' AND c.library_path IS NOT NULL",
                         args).fetchall():
        if library.is_within(r["library_path"], config.LIBRARY_ROOT):
            chapters[(r["series_id"], r["number"])] = r
    rows = con.execute("SELECT * FROM conversion" + (" WHERE series_id=?" if series_id is not None else ""),
                       args).fetchall()
    seen = set()
    for r in rows:
        key = (r["series_id"], r["number"])
        t = every.get(r["target_id"])
        seen.add((*key, r["target_id"]))
        ch = chapters.get(key)
        if t is None or ch is None:                         # the chapter is gone from the library
            if r["status"] != "running":
                out["removed"] += _remove_output(r["output_path"], t) if t else 0
                con.execute("DELETE FROM conversion WHERE series_id=? AND number=? AND target_id=?",
                            (*key, r["target_id"]))
            continue
        s = series[r["series_id"]]
        if r["status"] == "failed" and r["next_try"] and r["next_try"] <= now and t["enabled"] and active:
            _requeue(con, r, 2)
            out["retry"] += 1
        elif r["status"] == "done":
            sig = signature(ch["library_path"])
            want = output_path(t, s["folder"], ch["library_path"])
            if not r["output_path"] or not os.path.lexists(r["output_path"]):
                if t["enabled"] and active and s["convert_enabled"]:
                    _requeue(con, r, 2)
                    out["changed"] += 1
            elif sig != r["source_sig"] or options_hash(t, s) != r["options_hash"]:
                if t["enabled"] and active and s["convert_enabled"]:
                    _requeue(con, r, 1 if sig != r["source_sig"] else 2)
                    out["changed"] += 1
            elif r["source_path"] != ch["library_path"] or r["output_path"] != want:
                if _move_output(con, r, t, want, ch["library_path"]):
                    out["renamed"] += 1
    for t in active:
        for key in chapters:
            if (*key, t["id"]) in seen or not series[key[0]]["convert_enabled"] \
                    or not db.valid_folder(series[key[0]]["folder"]):
                continue
            con.execute("INSERT OR IGNORE INTO conversion (series_id, number, target_id, status, priority,"
                        " queued_at, updated_at) VALUES (?,?,?,'pending',?,?,?)", (*key, t["id"], priority, now, now))
            out["new"] += 1
    con.commit()
    if any(out.values()):
        log.info("conversion queue%s: %d new, %d changed, %d renamed, %d removed, %d to try again",
                 f" (series #{series_id})" if series_id is not None else "", out["new"], out["changed"],
                 out["renamed"], out["removed"], out["retry"])
    return out


def _requeue(con, r, priority: int) -> None:
    con.execute("UPDATE conversion SET status='pending', priority=?, reason=NULL, next_try=NULL, queued_at=?,"
                " updated_at=? WHERE series_id=? AND number=? AND target_id=?",
                (priority, db.now(), db.now(), r["series_id"], r["number"], r["target_id"]))


def _move_output(con, r, target, want: str, library_path: str) -> bool:
    """A copy follows its library file's new name (or its series folder's):
    one rename inside the target's folder, never over another file. When
    that cannot be done the copy stays where it is and the row still knows
    it."""
    old = r["output_path"]
    if old != want:
        if not (_ours(old, target) and _ours(want, target)) or os.path.lexists(want):
            con.execute("UPDATE conversion SET source_path=?, updated_at=? WHERE series_id=? AND number=? AND"
                        " target_id=?", (library_path, db.now(), r["series_id"], r["number"], r["target_id"]))
            return False
        try:
            os.makedirs(os.path.dirname(want), exist_ok=True)
            os.rename(old, want)
        except OSError as e:
            log.warning("the converted copy %s could not be renamed to %s: %s", old, want, e)
            return False
    con.execute("UPDATE conversion SET source_path=?, output_path=?, updated_at=? WHERE series_id=? AND number=? AND"
                " target_id=?", (library_path, want, db.now(), r["series_id"], r["number"], r["target_id"]))
    return True


def follow_file(con, series_id: int, number: float, new_library_path: str) -> int:
    """A library file was renamed (renamer.py): its converted copies get
    the matching name. Returns how many were renamed. Never raises."""
    n = 0
    try:
        s = db.get_series(con, series_id)
        for r in con.execute("SELECT * FROM conversion WHERE series_id=? AND number=? AND status='done'",
                             (series_id, number)).fetchall():
            t = get_target(con, r["target_id"])
            if s is None or t is None or not db.valid_folder(s["folder"]):
                continue
            n += _move_output(con, r, t, output_path(t, s["folder"], new_library_path), new_library_path)
    except Exception as e:
        log.warning("converted copies of series #%s ch %s could not follow the rename: %s: %s", series_id, number,
                    type(e).__name__, e)
    return n


def follow_folder(con, series_id: int, old_folder: str, new_folder: str) -> int:
    """A series folder was renamed: the series' folder in every target is
    renamed too (never over an existing one), and the copies in it get
    their new names. Returns how many copies moved. Never raises."""
    n = 0
    try:
        if not (db.valid_folder(old_folder) and db.valid_folder(new_folder)):
            return 0
        for t in targets(con):
            src, dst = os.path.join(target_dir(t), old_folder), os.path.join(target_dir(t), new_folder)
            if os.path.isdir(src) and not os.path.islink(src) and _ours(src, t) and not os.path.lexists(dst):
                os.rename(src, dst)
                for r in con.execute("SELECT * FROM conversion WHERE series_id=? AND target_id=? AND output_path IS"
                                     " NOT NULL", (series_id, t["id"])).fetchall():
                    if os.path.dirname(r["output_path"]) == src:
                        con.execute("UPDATE conversion SET output_path=? WHERE series_id=? AND number=? AND"
                                    " target_id=?", (os.path.join(dst, os.path.basename(r["output_path"])),
                                                     series_id, r["number"], t["id"]))
        for r in con.execute("SELECT v.*, c.library_path AS now_path FROM conversion v JOIN chapter c ON"
                             " c.series_id=v.series_id AND c.number=v.number WHERE v.series_id=? AND v.status='done'",
                             (series_id,)).fetchall():
            t = get_target(con, r["target_id"])
            if t is not None and r["now_path"]:
                n += _move_output(con, r, t, output_path(t, new_folder, r["now_path"]), r["now_path"])
    except Exception as e:
        log.warning("converted copies of series #%s could not follow the folder's rename: %s: %s", series_id,
                    type(e).__name__, e)
    return n


def remove_series_outputs(con, series_id: int) -> int:
    """The copies of a series that is deleted with its library files."""
    removed = 0
    every = {t["id"]: t for t in targets(con)}
    for r in con.execute("SELECT target_id, output_path FROM conversion WHERE series_id=?", (series_id,)).fetchall():
        t = every.get(r["target_id"])
        if t is not None:
            removed += _remove_output(r["output_path"], t)
    for t in every.values():
        _prune_empty(t)
    return removed


def claim_next(con):
    """Take the most urgent row that is due, atomically: two processes
    never convert the same chapter."""
    while True:
        r = con.execute("SELECT v.series_id, v.number, v.target_id FROM conversion v JOIN convert_target t ON"
                        " t.id=v.target_id WHERE v.status='pending' AND t.enabled=1 ORDER BY v.priority, v.queued_at,"
                        " v.series_id, v.number LIMIT 1").fetchone()
        if r is None:
            return None
        cur = con.execute("UPDATE conversion SET status='running', claimed_at=?, updated_at=? WHERE series_id=? AND"
                          " number=? AND target_id=? AND status='pending'",
                          (db.now(), db.now(), r["series_id"], r["number"], r["target_id"]))
        con.commit()
        if cur.rowcount:
            return con.execute("SELECT * FROM conversion WHERE series_id=? AND number=? AND target_id=?",
                               (r["series_id"], r["number"], r["target_id"])).fetchone()


def release_stale(con, all_running: bool = False) -> int:
    """Rows left running by a process that is gone are queued again."""
    secs = limits.setting("convert_timeout_minutes") * 60 + STALE_EXTRA_SECS
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - secs))
    cur = con.execute("UPDATE conversion SET status='pending', claimed_at=NULL, updated_at=? WHERE status='running'"
                      + ("" if all_running else " AND (claimed_at IS NULL OR claimed_at < ?)"),
                      (db.now(),) if all_running else (db.now(), cutoff))
    con.commit()
    return cur.rowcount


def _set(con, r, **fields) -> None:
    fields["updated_at"] = db.now()
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE conversion SET {cols} WHERE series_id=? AND number=? AND target_id=?",
                (*fields.values(), r["series_id"], r["number"], r["target_id"]))
    con.commit()


def _fail(con, r, why: str, permanent: bool = False) -> None:
    tries = (r["tries"] or 0) + 1
    gave_up = permanent or tries > len(RETRY_MINUTES)
    next_try = None if gave_up else time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + RETRY_MINUTES[tries - 1] * 60))
    _set(con, r, status="failed", reason=why[:500], tries=tries, next_try=next_try, claimed_at=None)


def counts(con) -> dict[int, dict]:
    """{target id: {"done", "pending", "running", "failed", "skipped", "bytes"}}"""
    out: dict[int, dict] = {}
    for r in con.execute("SELECT target_id, status, COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM conversion"
                         " GROUP BY target_id, status"):
        d = out.setdefault(r["target_id"], {"done": 0, "pending": 0, "running": 0, "failed": 0, "skipped": 0,
                                            "bytes": 0})
        d[r["status"]] = d.get(r["status"], 0) + r["n"]
        if r["status"] == "done":
            d["bytes"] += r["b"]
    return out


def estimate(con) -> dict:
    """What converting every chapter the library has would take, for one
    target: {"chapters", "seconds", "bytes"}. The time is CPU time at about
    3 seconds a chapter; the size is at most about the library's own."""
    n = con.execute("SELECT COUNT(*) FROM chapter WHERE status='have' AND library_path IS NOT NULL").fetchone()[0]
    try:
        size = shutil.disk_usage(config.LIBRARY_ROOT)
        used = _tree_size_hint(con) or 0
    except OSError:
        size, used = None, 0
    return {"chapters": n, "seconds": int(n * CPU_SECS_PER_CHAPTER), "bytes": used,
            "free": size.free if size else None}


def _tree_size_hint(con) -> int:
    """The library's size, from a sample of its files (a full walk of
    thousands of files would make the Settings page slow)."""
    rows = con.execute("SELECT library_path FROM chapter WHERE status='have' AND library_path IS NOT NULL ORDER BY"
                       " series_id, number").fetchall()
    if not rows:
        return 0
    step = max(1, len(rows) // 200)
    sizes = []
    for r in rows[::step]:
        if library.is_within(r["library_path"], config.LIBRARY_ROOT):
            try:
                sizes.append(os.path.getsize(r["library_path"]))
            except OSError:
                pass
    return int(sum(sizes) / len(sizes) * len(rows)) if sizes else 0


# -- one conversion ------------------------------------------------------------------------------

def _start_worker(job: dict):
    """The worker process for one conversion, with nothing of the server's
    environment but what it needs to start."""
    if _spawn is not None:
        return _spawn(job)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONPATH": here, "PYTHONDONTWRITEBYTECODE": "1",
           "MALLOC_ARENA_MAX": "2", "OMP_NUM_THREADS": "1"}
    p = subprocess.Popen([sys.executable, "-m", "mangarr.convert.worker"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
                         cwd=here, env=env, text=True)
    p.stdin.write(json.dumps(job))
    p.stdin.close()
    return p


def _kill(p) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.send_signal(sig)
            except (ProcessLookupError, OSError):
                return
        try:
            p.wait(KILL_WAIT_SECS)
            return
        except subprocess.TimeoutExpired:
            continue


def _run_worker(job: dict, stop: Callable[[], bool], said: Callable[[int, int], None]) -> tuple[int | None, dict, str]:
    """Run the worker to its end: (exit code or None when it was stopped,
    its last answer, why it was stopped or what it wrote to stderr)."""
    p = _start_worker(job)
    answer: dict = {}
    last = [time.monotonic()]
    errors: list[str] = []

    def read_out():
        for line in p.stdout:
            last[0] = time.monotonic()
            try:
                d = json.loads(line[:65536])
            except ValueError:
                continue
            if isinstance(d, dict) and "progress" in d:
                try:
                    said(int(d["progress"][0]), int(d["progress"][1]))
                except (TypeError, ValueError, IndexError):
                    pass
            elif isinstance(d, dict):
                answer.update(d)

    def read_err():
        for line in p.stderr:
            errors.append(line[:500])
            del errors[:-20]
    readers = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    for t in readers:
        t.start()
    started = time.monotonic()
    limit = limits.setting("convert_timeout_minutes") * 60
    why = ""
    while p.poll() is None:
        now = time.monotonic()
        if stop():
            why = "stopped"
        elif now - started > limit:
            why = f"took longer than {limit / 60:g} min"
        elif now - last[0] > NO_PROGRESS_SECS:
            why = f"made no progress for {NO_PROGRESS_SECS:.0f} s"
        if why:
            _kill(p)
            break
        time.sleep(0.2)
    for t in readers:
        t.join(2)
    return (None if why else p.returncode), answer, why or "".join(errors)[-1000:]


def run_one(con, r, stop: Callable[[], bool] | None = None, said: Callable[[int, int], None] | None = None,
            threads: int | None = None) -> str:
    """Convert one claimed row. Returns what came of it: done, failed,
    skipped, stopped (queued again) or full (the disk: the caller pauses)."""
    stop = stop or (lambda: False)
    said = said or (lambda done, total: None)
    t = get_target(con, r["target_id"])
    s = db.get_series(con, r["series_id"])
    ch = con.execute("SELECT * FROM chapter WHERE series_id=? AND number=?", (r["series_id"], r["number"])).fetchone()
    if t is None or s is None or ch is None or ch["status"] != "have" or not ch["library_path"]:
        con.execute("DELETE FROM conversion WHERE series_id=? AND number=? AND target_id=?",
                    (r["series_id"], r["number"], r["target_id"]))
        con.commit()
        return "skipped"
    src = ch["library_path"]
    sig = signature(src)
    problem = root_problem()
    if problem or not library.is_within(src, config.LIBRARY_ROOT) or sig is None or not db.valid_folder(s["folder"]) \
            or not valid_folder(t["folder"]):
        _fail(con, r, problem or "the library file is not a file inside the library", permanent=True)
        return "failed"
    try:
        options = engine_options(t)
    except (ValueError, TypeError) as e:
        _fail(con, r, f"the target's options cannot be used: {e}", permanent=True)
        return "failed"
    dst = output_path(t, s["folder"], src)
    out_dir = os.path.dirname(dst)
    tmp = os.path.join(out_dir, f"{PART_PREFIX}{secrets.token_hex(8)}{PART_SUFFIX}")
    try:
        os.makedirs(out_dir, exist_ok=True)
        if not _ours(out_dir, t) or os.path.islink(out_dir):
            raise OSError("the series folder in the output folder is not a plain folder inside it")
        fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666)
        os.close(fd)
    except OSError as e:
        _fail(con, r, f"the output folder cannot be written: {e.strerror or e}")
        return "failed"
    rtl, why_rtl = direction(s)
    webtoon, hints = layout(s)
    n_threads = int(threads or limits.setting("convert_threads"))
    job = {"src": src, "out": tmp, "options": options.to_dict(), "rtl": rtl, "webtoon": webtoon, "hints": hints,
           "meta": meta_for(s, ch), "threads": n_threads, "memory_mb": int(limits.setting("convert_memory_mb")),
           "max_output_mb": MAX_OUTPUT_MB,
           "cpu_secs": int(limits.setting("convert_timeout_minutes") * 60 * n_threads + 60)}
    log.debug("%s ch %g: converting for %s", s["title"], r["number"], t["name"])
    t0 = time.monotonic()
    try:
        code, answer, detail = _run_worker(job, stop, said)
    except OSError as e:
        code, answer, detail = 1, {}, f"the converter could not be started: {e}"
    took = time.monotonic() - t0
    fmt = t["format"]
    if code == 0 and answer.get("ok"):
        try:
            os.replace(tmp, dst)
        except OSError as e:
            _unlink(tmp)
            _fail(con, r, f"the copy could not be put in place: {e.strerror or e}")
            return "failed"
        old = r["output_path"]
        if old and old != dst:
            _remove_output(old, t)
        size = answer.get("bytes") or 0
        _set(con, r, status="done", reason=None, tries=0, next_try=None, claimed_at=None, source_path=src,
             source_sig=sig, options_hash=options_hash(t, s), engine=answer.get("engine"), output_path=dst,
             pages=answer.get("pages"), bytes=size, seconds=round(took, 2), rtl=int(bool(answer.get("rtl"))),
             webtoon=int(bool(answer.get("webtoon"))), decided=f"{answer.get('decided') or ''}; direction: {why_rtl}")
        metrics.record_conversion(fmt, "done", took, size)
        log.info("%s ch %g: %s for %s, %s pages, %.1f MB in %.1f s -> %s", s["title"], r["number"], fmt, t["name"],
                 answer.get("pages"), size / 1e6, took, dst)
        return "done"
    _unlink(tmp)
    error = str(answer.get("error") or detail or "the converter ended without an answer")[:400]
    if code is None:                                    # stopped by us
        if detail == "stopped":
            _set(con, r, status="pending", claimed_at=None)
            return "stopped"
        metrics.record_conversion(fmt, "timeout", took, 0)
        _fail(con, r, f"the conversion {detail}")
        return "failed"
    if code == 4:
        _set(con, r, status="pending", claimed_at=None)
        metrics.record_conversion(fmt, "failed", took, 0)
        log.warning("%s ch %g: %s", s["title"], r["number"], error)
        return "full"
    if code == 3 and n_threads > 1:                     # out of memory: once more, one page at a time
        log.info("%s ch %g: out of memory with %d threads; trying with one", s["title"], r["number"], n_threads)
        return run_one(con, r, stop, said, threads=1)
    metrics.record_conversion(fmt, "memory" if code == 3 else "failed", took, 0)
    _fail(con, r, error, permanent=(code == 2))
    log.warning("%s ch %g: not converted for %s: %s", s["title"], r["number"], t["name"], error)
    return "failed"


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def clean_parts(con) -> int:
    """Unfinished files a stopped conversion left behind, older than a day."""
    n = 0
    cutoff = time.time() - 86400
    for t in targets(con):
        top = target_dir(t)
        if not (os.path.isdir(top) and library.is_within(top, root())):
            continue
        for d, _dirs, files in os.walk(top):
            for f in files:
                p = os.path.join(d, f)
                if f.startswith(PART_PREFIX) and f.endswith(PART_SUFFIX):
                    try:
                        if os.lstat(p).st_mtime < cutoff:
                            os.unlink(p)
                            n += 1
                    except OSError:
                        pass
    return n


# -- the service ---------------------------------------------------------------------------------

class Service:
    """The thread that works through the queue (see the module docstring)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop = False
        self._cancel = False
        self._dirty: set[int] = set()
        self._full = False
        self._last_full = 0.0
        self.current: dict | None = None
        self.held: str | None = None                # why nothing is converted right now
        self.recent: list[float] = []               # seconds the last conversions took

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._loop, name="mangarr-convert", daemon=True)
            self._thread.start()
        log.info("e-reader conversion service started (output folder %s)", root())

    def stop(self, wait: float = 10.0) -> None:
        self._stop = True
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(wait)

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def notify(self, series_id: int) -> None:
        """New chapters of this series are in the library. Thread-safe, never raises."""
        try:
            with self._lock:
                self._dirty.add(int(series_id))
            self._wake.set()
        except Exception as e:
            log.debug("conversion notify failed: %s", e)

    def reconcile_soon(self) -> None:
        with self._lock:
            self._full = True
        self._wake.set()

    def cancel_current(self) -> bool:
        if self.current is None:
            return False
        self._cancel = True
        return True

    def pause(self, con) -> None:
        settings.set_many(con, {"convert_paused": True}, internal=True)
        self._cancel = True
        self._wake.set()

    def resume(self, con) -> None:
        settings.set_many(con, {"convert_paused": False}, internal=True)
        self._wake.set()

    def status(self, con) -> dict:
        per = counts(con)
        waiting = sum(d["pending"] for d in per.values())
        avg = sum(self.recent) / len(self.recent) if self.recent else CPU_SECS_PER_CHAPTER
        return {"enabled": enabled(), "available": convert.available(), "alive": self.alive(),
                "paused": bool(settings.get("convert_paused")), "held": self.held, "current": self.current,
                "waiting": waiting, "failed": sum(d["failed"] for d in per.values()),
                "done": sum(d["done"] for d in per.values()), "eta_seconds": int(waiting * avg), "targets": per}

    # -- the loop --

    def _halted(self) -> bool:
        return self._stop or self._cancel

    def _loop(self) -> None:
        first = time.monotonic() + FIRST_RECONCILE_SECS
        try:
            with db.connect() as con:
                release_stale(con, all_running=True)
        except Exception as e:
            log.warning("conversion service: could not read the queue at start: %s", e)
        while not self._stop:
            self._wake.wait(IDLE_SECS)
            self._wake.clear()
            if self._stop:
                break
            try:
                self._turn(first)
            except Exception:
                log.exception("conversion service: a turn failed; trying again in a while")
                time.sleep(5)

    def _turn(self, first: float) -> None:
        self.held = None
        if not enabled():
            self.held = "conversion is switched off"
            return
        ok, detail = convert.available()
        if not ok:
            self.held = detail
            return
        problem = root_problem()
        if problem:
            self.held = problem
            return
        now = time.monotonic()
        with db.connect() as con:
            if not targets(con, enabled_only=True):
                self.held = "there is no target"
                return
            with self._lock:
                dirty, self._dirty = self._dirty, set()
                full = self._full or (now >= first and now - self._last_full > RECONCILE_EVERY_SECS)
                self._full = False
            if full:
                self._last_full = now
                release_stale(con)
                clean_parts(con)
                reconcile(con)
            else:
                for sid in dirty:
                    reconcile(con, sid, priority=1)
            while not self._stop:
                if settings.get("convert_paused"):
                    self.held = "paused by you"
                    return
                low = self._low_on_space()
                if low:
                    self.held = low
                    return
                r = claim_next(con)
                if r is None:
                    return
                self._cancel = False
                s = db.get_series(con, r["series_id"])
                t = get_target(con, r["target_id"])
                self.current = {"series_id": r["series_id"], "title": s["title"] if s else "", "number": r["number"],
                                "target": t["name"] if t else "", "page": 0, "pages": 0, "since": time.time()}

                def said(done: int, total: int) -> None:
                    cur = self.current
                    if cur is not None:
                        cur["page"], cur["pages"] = done, total
                t0 = time.monotonic()
                try:
                    what = run_one(con, r, self._halted, said)
                finally:
                    self.current = None
                if what == "done":
                    self.recent = (self.recent + [time.monotonic() - t0])[-20:]
                elif what == "full":
                    self.held = "the output folder is full; conversion goes on when there is space again"
                    return
                with self._lock:
                    if self._dirty or self._full:
                        self._wake.set()
                        return

    @staticmethod
    def _low_on_space() -> str | None:
        need = limits.setting("convert_min_free_gb") * (1 << 30)
        try:
            os.makedirs(root(), exist_ok=True)
            free = shutil.disk_usage(root()).free
        except OSError as e:
            return f"the output folder {root()} cannot be used: {e.strerror or e}"
        if free < need:
            return (f"paused: {free / (1 << 30):.1f} GB free in {root()}, less than the {need / (1 << 30):g} GB "
                    "set in Settings")
        return None


service = Service()


def run_until_empty(stop: Callable[[], bool] | None = None, said: Callable[[str], None] | None = None) -> dict:
    """Reconcile, then convert in the foreground until nothing is due (the
    command line). The claim is atomic, so it is safe next to the service."""
    out = {"done": 0, "failed": 0}
    stop = stop or (lambda: False)
    with db.connect() as con:
        reconcile(con)
        while not stop():
            r = claim_next(con)
            if r is None:
                break
            what = run_one(con, r, stop)
            if what in out:
                out[what] += 1
            if said:
                said(f"series #{r['series_id']} ch {r['number']:g}: {what}")
            if what in ("full", "stopped"):
                break
    return out
