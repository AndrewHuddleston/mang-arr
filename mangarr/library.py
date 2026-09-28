"""The files: what Suwayomi wrote, and the clean per-series library.

Suwayomi downloads into <staging>/<Source>/<Series>/<scanlator>_<chapter name>.cbz
and treats that tree as its record of what is downloaded, so it must never
be renamed. The library is a second tree, <library>/<folder>/Chapter 012.0.cbz,
built from hard links, that Komga reads. One folder per tracked series
(the folder name is stored with the series, so two series with the same
title never share one), regardless of how many sources the chapters came from.

Everything under staging is written by Suwayomi and its extensions, so it is
treated as untrusted: symlinks and special files are skipped, import works
on each series folder through one open directory (StagingFolder) so a folder
swapped for a symlink mid-import changes nothing, the file linked is the
open file that was checked, archives are checked with hard caps before
zipfile parses or decompresses them (and zipfile parses the very bytes that
were checked), and every name mang-arr creates fits the filesystem's
255-byte limit.
"""
import errno
import logging
import os
import re
import secrets
import shutil
import stat
import struct
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager

from . import config, naming
from .naming import NAME_MAX, fit_name  # noqa: F401  (library.fit_name and NAME_MAX: kept for callers)

log = logging.getLogger(__name__)

_EXT = (".cbz", ".cbr", ".zip")

# Chapter number from a Suwayomi file name. The scanlator prefix (up to the
# first "_") is ignored by matching the chapter keyword anywhere. Keywords
# come from real names on disk: Chapter, Ch., Episode, #, Page, Day, Mission,
# Room, Act, Step, bullet ...
_SEASON = re.compile(r"(?<![A-Za-z])S(\d+)\s*[-–]\s*(?:Episode|Ep\.?|Chapter|Ch\.?)\s*(\d+(?:\.\d+)?)", re.I)
_KEYWORD = re.compile(
    r"(?:\b(?:chapter|chap|ch|episode|ep|page|day|mission|room|act|step|bullet|part|lesson|round|file|case|"
    r"night|stage)\b\.?|#)\s*(\d+(?:\.\d+)?)", re.I)
_VOLUME_ONLY = re.compile(r"(?<![A-Za-z])vol(?:ume)?\.?\s*\d+", re.I)
# the last number in the name. Anchored at the start of a digit run and
# followed only by non-digits: linear, where "(?!.*\d)" rescans the rest of
# the name from every digit.
_LASTNUM = re.compile(r"(?<![\d])(\d+(?:\.\d+)?)\D*$")
MAX_PARSE = 1000        # chars of a name the chapter-number patterns look at


def parse_number(filename: str) -> float | None:
    """Chapter number in a file name, or None when there is none (volume-only
    files are not chapters; season-numbered files such as 'S2 - Episode 5'
    carry no global number - import resolves those through Suwayomi's own
    chapter listing, see suwayomi_name_map)."""
    stem = os.path.splitext(os.path.basename(filename))[0][:MAX_PARSE]
    m = _KEYWORD.search(stem)
    if m and not (_SEASON.search(stem) and _SEASON.search(stem).start() <= m.start()):
        return float(m.group(1))
    if _SEASON.search(stem):
        return None
    if m:
        return float(m.group(1))
    if m:
        return float(m.group(1))
    if _VOLUME_ONLY.search(stem):
        return None
    m = _LASTNUM.search(stem)
    return float(m.group(1)) if m else None


def parse_season(filename: str) -> tuple[int, float] | None:
    name = filename or ""
    if "/" in name or "." in name:          # (without either, the name is its own base name, with no extension)
        name = os.path.splitext(os.path.basename(name))[0]
    m = _SEASON.search(name[:MAX_PARSE])
    return (int(m.group(1)), float(m.group(2))) if m else None


def _regular_file(entry: os.DirEntry) -> bool:
    """A real file, not a symlink, FIFO or device. Staging is written by
    Suwayomi's extensions; a symlink there could pull any file mang-arr can
    read into the library, so it is never followed."""
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False


def _real_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def scan_series_dir(path: str, dir_fd: int | None = None) -> tuple[dict[float, str], list[str]]:
    """{chapter number: file path} for one series folder, plus the files
    whose number could not be read. Duplicate numbers keep the first name.
    Symlinks and anything that is not a regular file are skipped (logged).
    With dir_fd (the folder, already open) the folder is listed through it
    and plain file names come back; path is then only used in log lines."""
    found: dict[float, str] = {}
    unparsed: list[str] = []
    try:
        with os.scandir(path if dir_fd is None else dir_fd) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as e:
        log.error("cannot list %s: %s", path, e)
        return found, unparsed
    for entry in entries:
        name = entry.name
        if not name.lower().endswith(_EXT):
            continue
        full = os.path.join(path, name)
        if not _regular_file(entry):
            log.warning("skipping %s: a symlink or not a regular file", full)
            continue
        key = full if dir_fd is None else name
        n = parse_number(name)
        if n is None:
            unparsed.append(key)
        elif n not in found:
            found[n] = key
    return found, unparsed


def staging_dirs(root: str | None = None) -> list[tuple[str, str, str]]:
    """[(source name, series folder name, path)] for every series folder."""
    root = root or config.STAGING_ROOT
    out = []
    if not os.path.isdir(root):
        log.error("staging root %s does not exist", root)
        return out
    for src in _dir_entries(root):
        if src.name.startswith("."):
            continue
        for series in _dir_entries(src.path):
            if not series.name.startswith("."):
                out.append((src.name, series.name, series.path))
    return out


def _dir_entries(path: str) -> list[os.DirEntry]:
    """Real sub-directories of path, sorted by name; symlinked ones are
    skipped (and logged) so nothing outside the staging tree is adopted."""
    try:
        with os.scandir(path) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as e:
        log.error("cannot list %s: %s", path, e)
        return []
    out = []
    for e in entries:
        if _real_dir(e):
            out.append(e)
        elif e.is_symlink():
            log.warning("skipping %s: a symlink, not a folder Suwayomi wrote", e.path)
    return out


_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_title(title: str) -> str:
    """Folder name for a series title (same rules Suwayomi uses: illegal
    characters become '_', leading and trailing dots and spaces go, so a
    title such as '.hack//Link' does not become a hidden folder)."""
    t = _ILLEGAL.sub("_", title or "")
    return re.sub(r"\s+", " ", t).strip(" .") or "untitled"


def unique_folder(title: str, taken: set[str], suffix: str) -> str:
    """A folder name not already used by another series: the title, or the
    title plus a disambiguating suffix (the series ref), plus a counter if
    even that is taken. Names are NFC, fit NAME_MAX bytes, and are compared
    case- and normalisation-insensitively, so two series never share a
    folder on a case-insensitive mount either. The default series folder
    format ({Series Title}, see naming.py) makes the name."""
    return naming.render(naming.SeriesNames(title), taken=taken, suffix=suffix)


def chapter_filename(number: float, label: str | None = None) -> str:
    """'Chapter 012.0.cbz' - fixed width so Komga sorts 12 before 12.5 and
    before 100, and .5 chapters sort after their integer. Two decimals only
    when the number needs them (5.25). A label (the source's chapter title
    when it says more than the number, e.g. 'S2 - Episode 5') is appended:
    'Chapter 012.0 - S2 - Episode 5.cbz'. The default chapter file format
    (Chapter {Chapter:000.0}{ - Chapter Title}, see naming.py) makes it."""
    return naming.render(None, naming.ChapterInfo(number, label))


def chapter_label(number: float, name: str | None) -> str | None:
    """The part of a source chapter name worth keeping in the file name:
    None for 'Chapter 12' / 'Ch.12' / 'Episode 12', the name otherwise."""
    return naming.chapter_title(number, name) or None


def suwayomi_name_map(chapters) -> dict:
    """Lookup tables so files whose name carries no global chapter number
    (season episodes) can still be matched to a chapter: by the file stem as
    Suwayomi writes it ('Official_S1 - Episode 0') and by (season, episode),
    which survives renames such as 'S1 - Episode 000.0'."""
    out: dict = {}
    for c in chapters:
        stem = f"{c.scanlator}_{c.name}" if c.scanlator else (c.name or "")
        out[safe_title(stem).lower()] = c.number
        se = parse_season(c.name or "")
        if se:
            out[se] = c.number
    return out


def match_unparsed(path: str, names: dict) -> float | None:
    """Chapter number for a file with no global number, via the name map."""
    stem = os.path.splitext(os.path.basename(path))[0]
    n = names.get(stem.lower())
    if n is None:
        se = parse_season(stem)
        if se:
            n = names.get(se)
    return n


def library_dir(folder: str, root: str | None = None) -> str:
    return os.path.join(root or config.LIBRARY_ROOT, folder)


def is_within(path: str, root: str) -> bool:
    """Is `path` inside `root` (or root itself) once symlinks and '..' are
    resolved? Paths stored in the database (which a restored backup can set
    to anything) are only acted on when this holds for their root."""
    if not path or not root:
        return False
    try:
        real_root = os.path.realpath(root)
        real = os.path.realpath(path)
        return os.path.commonpath([real, real_root]) == real_root
    except ValueError:            # mixed absolute/relative, or different drives
        return False
    except OSError:               # a symlink on the way was swapped while it was being resolved
        return False


# -- staging, opened -------------------------------------------------------------
# A path is looked up again on every call, so a staging writer that swaps a
# folder for a symlink between two calls (check, then use) could aim the
# second one anywhere, e.g. at another series' library folder. Import
# therefore opens each series folder once and does everything in it through
# that open folder, by plain file name.

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)


class NotRegularFile(OSError):
    """A staged name that is a symlink, FIFO, device or folder, not a file."""


def _open_dir_below(root: str, path: str) -> int:
    """File descriptor of the folder `path`, which must lie below `root`.
    Every component under the root is opened relative to the one above it
    and never through a symlink, so the folder that comes back is inside the
    root however the tree is changed meanwhile. The root itself may be a
    symlink: it is configuration, not something Suwayomi writes."""
    rel = None
    for base in (os.path.abspath(root), os.path.realpath(root)):
        r = os.path.relpath(os.path.abspath(path), base)
        if r != os.curdir and os.pardir not in r.split(os.sep):
            rel = r
            break
    if rel is None:
        raise OSError(errno.EPERM, f"not a folder inside {root}", path)
    fd = os.open(root, _DIR_FLAGS)
    try:
        for part in rel.split(os.sep):
            try:
                sub = os.open(part, _DIR_FLAGS | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise OSError(e.errno, f"{part!r} is a symlink or not a folder", path) from None
                raise
            os.close(fd)
            fd = sub
    except BaseException:
        os.close(fd)
        raise
    return fd


class StagingFolder:
    """One series folder in staging, opened once (see _open_dir_below).
    Listing, opening, setting aside and linking its files all go through
    this open folder by plain name, so swapping the folder, or one above it,
    for a symlink after it was opened changes nothing: every file acted on
    is in the folder that was checked to be inside the staging root."""

    def __init__(self, path: str, root: str | None = None):
        self.path = path
        self.fd = _open_dir_below(root or config.STAGING_ROOT, path)

    def __enter__(self) -> "StagingFolder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def scan(self) -> tuple[dict[float, str], list[str]]:
        """scan_series_dir, with file names as values."""
        return scan_series_dir(self.path, dir_fd=self.fd)

    def prune_quarantine(self) -> int:
        return prune_quarantine(self.path, QUARANTINE_DAYS, dir_fd=self.fd)

    def open(self, name: str) -> "StagedFile":
        return StagedFile(self.fd, name, os.path.join(self.path, name))


class StagedFile:
    """A chapter file opened in its staging folder, never through a symlink,
    and checked to be a regular file. Verifying, setting aside, linking and
    copying all use this one open file, so what reaches the library is the
    file that was checked, even if its name is swapped meanwhile."""

    def __init__(self, dir_fd: int, name: str, path: str):
        self.dir_fd, self.name, self.path = dir_fd, name, path
        try:
            # O_NONBLOCK: never hang on a FIFO (it is refused just below)
            self.fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise NotRegularFile(e.errno, "a symlink, not a regular file", path) from None
            raise _naming(e, path) from e      # opened by plain name: say which staging folder
        try:
            self.st = os.fstat(self.fd)
            if not stat.S_ISREG(self.st.st_mode):
                raise NotRegularFile(errno.EINVAL, "not a regular file", path)
        except BaseException:
            os.close(self.fd)
            raise

    def __enter__(self) -> "StagedFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def is_same(self, st: os.stat_result | None) -> bool:
        """Is st (of some name) the very file that was opened?"""
        return st is not None and (st.st_dev, st.st_ino) == (self.st.st_dev, self.st.st_ino)

    def reader(self):
        """A binary file object on the open file, from the start. Closing it
        leaves the file open."""
        os.lseek(self.fd, 0, os.SEEK_SET)
        return open(self.fd, "rb", closefd=False)


@contextmanager
def open_staged(path: str) -> Iterator[StagedFile]:
    """A StagedFile for a plain path: its folder is opened as given, the file
    itself never through a symlink. Raises NotRegularFile for a symlink or
    special file."""
    dir_fd = os.open(os.path.dirname(path) or os.curdir, _DIR_FLAGS)
    try:
        with StagedFile(dir_fd, os.path.basename(path), path) as f:
            yield f
    finally:
        os.close(dir_fd)


def _lstat_at(name: str, dir_fd: int) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _unlink_at(name: str, dir_fd: int) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


# -- library -------------------------------------------------------------------

_copy_warned = False
COPIED = 0          # chapters copied because a hard link was impossible
# os.link errors that mean "hard links cannot work here", where a copy is the
# right fallback: another filesystem or mount (EXDEV), links refused
# (EPERM: fs.protected_hardlinks, SMB/FUSE mounts), or the inode is out of links.
_COPY_ERRNOS = (errno.EXDEV, errno.EPERM, errno.EMLINK)


def link_into_library(src: str | StagedFile, folder: str, number: float, root: str | None = None,
                      replace: bool = False, label: str | None = None) -> str | None:
    """Hard-link a staged chapter into the library. Copies when the two
    trees are on different filesystems or mounts (logged once). Returns the
    library path, or None when a different file already sits there and
    `replace` is False - the library never overwrites what it did not make.
    src is an opened StagedFile (import passes the file it verified) or a
    path, which must be a regular file, never a symlink. What is linked or
    copied is that open file, whatever its name points at by now; it goes
    to a hidden temporary name first and only then gets its real name, so
    the library never shows a half-written copy. An OSError names the
    staged file and the library path."""
    global COPIED
    if isinstance(src, str):
        try:
            with open_staged(src) as f:
                return link_into_library(f, folder, number, root, replace, label)
        except NotRegularFile:
            log.error("%s is not a regular file (symlink or special file); not linking it", src)
            return None
    d = library_dir(folder, root)
    os.makedirs(d, exist_ok=True)
    name = chapter_filename(number, label)
    dst = os.path.join(d, name)
    dir_fd = os.open(d, _DIR_FLAGS)
    try:
        there = _lstat_at(name, dir_fd)
        if there is not None:
            if src.is_same(there):
                return dst
            if not replace:
                log.error("%s exists and is not a link to %s; leaving it alone", dst, src.path)
                return None
        tmp = f".mangarr-{secrets.token_hex(8)}.part"
        try:
            copied = _link_or_copy(src, dir_fd, tmp, dst)
            _publish(dir_fd, tmp, name, dst, replace)
        except OSError as e:
            # the calls above use plain and temporary names; say which files it was
            raise _naming(e, src.path, dst) from e
        finally:
            _unlink_at(tmp, dir_fd)
    finally:
        os.close(dir_fd)
    COPIED += copied
    return dst


def _naming(e: OSError, src: str, dst: str | None = None) -> OSError:
    """e (same type, so FileNotFoundError still means the file went away),
    naming the staged file (and where it was going) by full path instead of
    the plain names the call was made with, so the reason on the chapter
    says which folder could not be read or written (the usual PUID/PGID
    mistake)."""
    if e.errno is None:
        return e
    return type(e)(e.errno, e.strerror, src, None, dst)


# Where Linux shows each open file as a link to it. Hard-linking that entry
# (linkat with AT_SYMLINK_FOLLOW) links the open file itself, so no second
# lookup by name is needed and nothing depends on inode numbers, which some
# mounts (FUSE without use_ino, e.g. sshfs; CIFS with noserverino) do not
# keep the same between two names of one file.
_PROC_FD = "/proc/self/fd"


def _link_or_copy(src: StagedFile, dir_fd: int, tmp: str, dst: str) -> bool:
    """Put the opened staged file at `tmp` in the library folder dir_fd: a
    hard link when possible, else a copy read from the open file. Returns
    whether it was copied."""
    global _copy_warned
    try:
        linked = _link_open_file(src, dir_fd, tmp)
    except OSError as e:
        if e.errno not in _COPY_ERRNOS:
            raise
        if not _copy_warned:
            log.warning("cannot hard-link %s -> %s (%s); copying instead. Staging and library are on "
                        "different filesystems or mounts, so every chapter is stored twice", src.path, dst, e)
            _copy_warned = True
        linked = False
    if not linked:
        _copy_into(src, dir_fd, tmp)
    return not linked


def _link_open_file(src: StagedFile, dir_fd: int, tmp: str) -> bool:
    """Hard-link the open staged file as `tmp` in dir_fd. False when that
    cannot be done safely and the caller should copy from the open file
    instead. Without /proc the link can only be made by name, and the name
    may have been swapped since the file was checked; when the new link is
    not (as far as inode numbers tell) the open file it is removed."""
    if os.path.isdir(_PROC_FD):
        os.link(f"{_PROC_FD}/{src.fd}", tmp, dst_dir_fd=dir_fd, follow_symlinks=True)
        return True
    os.link(src.name, tmp, src_dir_fd=src.dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    if src.is_same(_lstat_at(tmp, dir_fd)):
        return True
    os.unlink(tmp, dir_fd=dir_fd)
    log.warning("%s is no longer the file that was checked (replaced meanwhile, or this filesystem does not "
                "keep inode numbers); copying the checked file instead", src.path)
    return False


def _copy_into(src: StagedFile, dir_fd: int, tmp: str) -> None:
    """Copy the open staged file to a new file `tmp` in dir_fd, with its mode
    and times, flushed to disk."""
    out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    with open(out, "wb") as w, src.reader() as r:
        shutil.copyfileobj(r, w, 1 << 20)
        w.flush()
        os.fchmod(w.fileno(), stat.S_IMODE(src.st.st_mode))
        os.utime(w.fileno(), ns=(src.st.st_atime_ns, src.st.st_mtime_ns))
        os.fsync(w.fileno())


def _publish(dir_fd: int, tmp: str, name: str, dst: str, replace: bool) -> None:
    """Give the finished temporary file its real name. Without `replace`,
    never over anything that appeared there meanwhile."""
    if replace:
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)    # a symlink there is replaced, not followed
        return
    try:
        os.link(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except OSError as e:
        if e.errno == errno.EEXIST or _lstat_at(name, dir_fd) is not None:
            raise FileExistsError(errno.EEXIST, "appeared while linking", dst) from e
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)    # no hard links on this filesystem at all


def scan_library_dir(folder: str, root: str | None = None) -> dict[float, str]:
    d = library_dir(folder, root)
    if not os.path.isdir(d):
        return {}
    found, _ = scan_series_dir(d)
    return found


# -- archive checks ------------------------------------------------------------

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".jxl")

# Limits for one chapter archive. Pages are JPEG/PNG/WebP, already compressed,
# so a real chapter stores at about 1:1 and holds a few hundred pages at most.
# The directory limits are read from the end of the file before zipfile
# parses anything, the rest from the parsed directory before anything is
# decompressed.
MAX_ENTRIES = 5000
MAX_DIRECTORY = 4 << 20             # 4 MiB of central directory (5000 entries need well under 1 MiB)
MAX_UNCOMPRESSED = 1 << 30          # 1 GiB, declared total
MAX_RATIO = 20                      # uncompressed / compressed, per entry and for the whole file ...
RATIO_MIN_SIZE = 256 << 10          # ... once over 256 KiB (a small ComicInfo.xml compresses well)
VERIFY_SECONDS = 120                # reading every entry back to check its CRC
MAX_EXTENTS = 4096                  # runs of data a file's holes are counted around (_stored_bytes)
# Stored and deflate are all Suwayomi (and any comic tool) writes, and the
# only methods zipfile decompresses a piece at a time: it inflates a whole
# bzip2 or LZMA read at once whatever size the directory declares, so a few
# KB of bzip2 could take gigabytes of memory before any limit here applies.
_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_METHOD_NAMES = {zipfile.ZIP_BZIP2: "bzip2", zipfile.ZIP_LZMA: "LZMA", 9: "deflate64", 93: "zstandard", 99: "AES"}

_END = struct.Struct("<4s4H2LH")                # end of central directory record
_END64_LOCATOR = struct.Struct("<4sLQL")        # zip64 end of central directory locator
_END64 = struct.Struct("<4sQ2H2L4Q")            # zip64 end of central directory record
_CENTRAL = struct.Struct("<4s4B4HL2L5H2L")      # one central directory header


class _OutOfTime(Exception):
    """Reading the entries back took longer than VERIFY_SECONDS."""


def verify_archive(src: str | StagedFile) -> tuple[bool | None, str]:
    """Is this a readable comic archive with at least one image? Returns
    (ok, detail) and never raises: a truncated, corrupt, encrypted or
    oversized file (or a symlink) must not reach the library, and must not
    stop the import of the other chapters either. ok is None when reading
    the entries back ran out of time (VERIFY_SECONDS, slow or busy storage):
    that says nothing about the file, so try again later. src is an opened
    StagedFile or a path (never opened through a symlink). A directory too
    big for a chapter is refused from the end records alone, before zipfile
    reads it; oversized or over-compressed contents are refused from the
    directory, before anything is decompressed. zipfile is handed the end
    records and directory that were checked (_Snapshot), not the live file,
    which the staging writer could rewrite in between."""
    if isinstance(src, str):
        try:
            with open_staged(src) as f:
                return verify_archive(f)
        except NotRegularFile:
            return False, "not a regular file"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"[:300]
    try:
        size = os.fstat(src.fd).st_size
        if size < 1024:
            return False, "file is empty"
        snap = _Snapshot(src.fd, size)
        end = _end_records(snap)
        if end is None:
            return False, "not a zip archive"
        problem = _directory_limits(snap, *end)
        if problem:
            return _rejected(src.path, problem)
        with zipfile.ZipFile(snap) as z:
            infos = z.infolist()
            problem = _archive_limits(infos, _stored_bytes(src.fd, size))
            if problem:
                return _rejected(src.path, problem)
            bad = _read_entries(z, infos)
            if bad:
                return False, bad
        images = [i.filename for i in infos if i.filename.lower().endswith(_IMAGE_EXT)]
        if not images:
            return False, "no images inside"
        return True, f"{len(images)} pages"
    except _OutOfTime:
        detail = f"checking it took longer than {VERIFY_SECONDS}s (slow or busy storage)"
        log.warning("%s not checked: %s; trying again later", src.path, detail)
        return None, detail
    except Exception as e:      # zlib.error, NotImplementedError, RuntimeError (encrypted), EOFError ...
        return False, f"{type(e).__name__}: {e}"[:300]


def _stored_bytes(fd: int, size: int) -> int:
    """The bytes a file really holds: its size less its holes. A sparse file
    (holes that read back as zeros) is far longer than what it stores, and
    would pass the ratio and overlap limits on its length. The holes are
    asked for (SEEK_DATA/SEEK_HOLE), not worked out from the blocks the file
    takes on disk: a file system that compresses (ZFS, btrfs) keeps a
    legitimate archive in fewer blocks than its length, and it has no holes.
    (ZFS with compression does keep a run of zeros as a hole; the pages of a
    chapter have none worth counting.) Where holes cannot be asked for, the
    size counts. Past MAX_EXTENTS runs of data the rest counts as a hole: a
    downloaded file is one run. The file offset is left as it was."""
    if not hasattr(os, "SEEK_DATA"):
        return size
    try:
        was = os.lseek(fd, 0, os.SEEK_CUR)
    except OSError:
        return size
    at = stored = 0
    try:
        for _ in range(MAX_EXTENTS):
            try:
                data = os.lseek(fd, at, os.SEEK_DATA)
            except OSError as e:
                if e.errno == errno.ENXIO:          # nothing but a hole from `at` to the end
                    break
                raise
            if data >= size:
                break
            hole = min(os.lseek(fd, data, os.SEEK_HOLE), size)
            if hole <= data:                        # a file system answering nonsense: take the size
                return size
            stored, at = stored + hole - data, hole
            if at >= size:
                break
        return stored
    except OSError:                                 # EINVAL and the like: this file system cannot say
        return size
    finally:
        try:
            os.lseek(fd, was, os.SEEK_SET)
        except OSError:
            pass


def _rejected(path: str, problem: str) -> tuple[bool, str]:
    log.warning("%s rejected: %s", path, problem)
    return False, problem


def _pread(fd: int, n: int, at: int) -> bytes:
    """Up to n bytes at offset `at` (fewer only at the end of the file)."""
    parts = []
    while n > 0:
        b = os.pread(fd, min(n, 1 << 20), at)
        if not b:
            break
        parts.append(b)
        n -= len(b)
        at += len(b)
    return b"".join(parts)


class _Snapshot:
    """A read-only file object over an open archive, for zipfile, that keeps
    it to what was checked. Its size is fixed, and every byte from `start`
    to the end (the central directory, the end records and the comment) is
    held in memory from the moment it was first read; only entry data before
    `start` comes from the file. Everything zipfile reads to find and parse
    the directory is therefore what the limits were checked on, even if the
    staging writer rewrites the end record meanwhile."""

    def __init__(self, fd: int, size: int):
        self.fd, self.size, self.pos = fd, size, 0
        self.start, self.held = size, b""
        self.hold_from(max(size - (1 << 16) - _END.size, 0))      # where zipfile looks for the end record

    def hold_from(self, at: int) -> None:
        """Hold every byte from `at` to the end in memory as well."""
        at = max(at, 0)
        if at < self.start:
            more = _pread(self.fd, self.start - at, at)
            if len(more) != self.start - at:
                raise zipfile.BadZipFile("the file got shorter while it was being checked")
            self.held, self.start = more + self.held, at

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        pos = offset + (0, self.pos, self.size)[whence]
        if pos < 0:
            raise OSError(errno.EINVAL, "negative seek position")
        self.pos = pos
        return pos

    def read(self, n: int | None = -1) -> bytes:
        end = self.size if n is None or n < 0 else min(self.pos + n, self.size)
        if end <= self.pos:
            return b""
        out = b""
        if self.pos < self.start:
            want = min(end, self.start) - self.pos
            out = _pread(self.fd, want, self.pos)
            if len(out) < want:                         # the file got shorter: it ends here
                self.pos += len(out)
                return out
        if end > self.start:
            out += self.held[max(self.pos, self.start) - self.start:end - self.start]
        self.pos = end
        return out


def _end_records(snap: _Snapshot) -> tuple[int, int, int] | None:
    """(entries, directory size, directory start) from the end of a zip, or
    None when it has no end record. The records are found the way zipfile
    finds them (the last 22 bytes, else the last signature in the final
    64 KiB, then a zip64 locator right before it), and held in the snapshot
    before they are read."""
    size = snap.size
    tail_at = max(size - (1 << 16) - _END.size, 0)
    snap.seek(tail_at)
    tail = snap.read()
    if len(tail) >= _END.size and tail[-_END.size:][:4] == b"PK\x05\x06" and tail[-2:] == b"\0\0":
        pos = len(tail) - _END.size
    else:
        pos = tail.rfind(b"PK\x05\x06")
        if pos < 0 or pos + _END.size > len(tail):
            return None
    _, disk, cd_disk, _, entries, cd_size, _, _ = _END.unpack_from(tail, pos)
    at = tail_at + pos                          # the directory ends where the end records start
    # zipfile looks for a zip64 locator and record right before the end record
    snap.hold_from(at - _END64_LOCATOR.size - _END64.size)
    if at >= _END64_LOCATOR.size:
        snap.seek(at - _END64_LOCATOR.size)
        sig, loc_disk, rec_at, disks = _END64_LOCATOR.unpack(snap.read(_END64_LOCATOR.size))
        if sig == b"PK\x06\x07":
            # only the plain layout, the record right before its locator: zipfile
            # versions differ on the others, and no chapter archive needs them
            if loc_disk or disks > 1 or rec_at != at - _END64_LOCATOR.size - _END64.size:
                raise zipfile.BadZipFile("unsupported zip64 end records")
            snap.seek(rec_at)
            sig, _, _, _, disk, cd_disk, _, entries, cd_size, _ = _END64.unpack(snap.read(_END64.size))
            if sig != b"PK\x06\x06":
                raise zipfile.BadZipFile("zip64 end of central directory record not found")
            at = rec_at
    if disk or cd_disk:
        raise zipfile.BadZipFile("archives split over several disks are not supported")
    return entries, cd_size, at - cd_size


def _directory_limits(snap: _Snapshot, entries: int, cd_size: int, cd_at: int) -> str | None:
    """Why an archive's directory is too big for a chapter, or None. The
    declared count and size come first; then the directory is held in the
    snapshot and walked header by header without building anything, because
    the count in the end record can understate what zipfile would parse."""
    if entries > MAX_ENTRIES:
        return f"{entries} entries (limit {MAX_ENTRIES})"
    if cd_size > MAX_DIRECTORY:
        return f"{cd_size} bytes of directory (limit {MAX_DIRECTORY})"
    if cd_at < 0:
        raise zipfile.BadZipFile("bad offset for central directory")
    snap.hold_from(cd_at)
    snap.seek(cd_at)
    cd = snap.read(cd_size)
    count = pos = 0
    while pos < cd_size:
        count += 1
        if count > MAX_ENTRIES:
            return f"more than {MAX_ENTRIES} entries (limit {MAX_ENTRIES})"
        if pos + _CENTRAL.size > len(cd):
            raise zipfile.BadZipFile("truncated central directory")
        h = _CENTRAL.unpack_from(cd, pos)
        if h[0] != b"PK\x01\x02":
            raise zipfile.BadZipFile("bad magic number for central directory")
        pos += _CENTRAL.size + h[12] + h[13] + h[14]      # name, extra field, comment
    return None


def _archive_limits(infos, size: int) -> str | None:
    """Why an archive's parsed directory is implausible for a chapter, or
    None. size is what the file stores (_stored_bytes), not its length."""
    if len(infos) > MAX_ENTRIES:
        return f"{len(infos)} entries (limit {MAX_ENTRIES})"
    for i in infos:
        if i.compress_type not in _METHODS:
            method = _METHOD_NAMES.get(i.compress_type, f"method {i.compress_type}")
            return f"entry {i.filename[:80]!r} is compressed with {method}; only stored or deflate is accepted"
    # every entry's data takes its own bytes of the file; more than the file
    # holds means entries overlap, which is how one page is read back 5000
    # times, or that the file is sparse (holes read back as zeros)
    packed = sum(i.compress_size for i in infos)
    if packed > size:
        return f"entries overlap or the file is sparse: they claim {packed} bytes of data from a file that " \
               f"stores {size} bytes"
    total = sum(i.file_size for i in infos)
    if total > MAX_UNCOMPRESSED:
        return f"{total} bytes uncompressed (limit {MAX_UNCOMPRESSED})"
    for i in infos:
        if i.file_size > RATIO_MIN_SIZE and i.file_size > MAX_RATIO * max(i.compress_size, 1):
            return f"entry {i.filename[:80]!r} expands {i.file_size // max(i.compress_size, 1)}x (limit {MAX_RATIO}x)"
    if total > RATIO_MIN_SIZE and total > MAX_RATIO * size:
        return f"{total} bytes uncompressed from a file that stores {size} bytes: {total // size}x " \
               f"(limit {MAX_RATIO}x)"
    return None


def _read_entries(z, infos) -> str | None:
    """zipfile's testzip, bounded: read every entry back (which checks its
    CRC) in 1 MiB pieces, giving up (_OutOfTime) after VERIFY_SECONDS. The
    limits above already cap how much is read and decompressed (stored and
    deflate stop at the declared size, overlapping entries and sparse files
    are refused); this caps the time on a slow disk. Returns what is wrong,
    or None."""
    deadline = time.monotonic() + VERIFY_SECONDS
    for i in infos:
        try:
            with z.open(i) as e:
                while True:
                    if time.monotonic() > deadline:
                        raise _OutOfTime()
                    if not e.read(1 << 20):
                        break
        except zipfile.BadZipFile:
            return f"corrupt entry {i.filename}"
    return None


QUARANTINE_DAYS = 14    # .corrupt files older than this are deleted


def quarantine(src: str | StagedFile) -> str:
    """Move a bad staged file aside (same folder, .corrupt suffix) so Suwayomi
    sees the chapter as not downloaded and it can be fetched again. Its time
    is set to now, so prune_quarantine counts the days from here. The rename
    is made inside the file's own open folder, and only while the name still
    is the file that was opened; a symlink or special file is never moved
    (OSError)."""
    if isinstance(src, str):
        with open_staged(src) as f:
            return quarantine(f)
    dst = src.name + ".corrupt"
    moved = os.path.join(os.path.dirname(src.path), dst)
    try:                                        # by plain names in the open folder: errors say which folder
        same = src.is_same(os.stat(src.name, dir_fd=src.dir_fd, follow_symlinks=False))
        if same:
            os.replace(src.name, dst, src_dir_fd=src.dir_fd, dst_dir_fd=src.dir_fd)
    except OSError as e:
        raise _naming(e, src.path, moved) from e
    if not same:
        raise OSError(errno.EAGAIN, "the file was replaced while it was being checked; not set aside", src.path)
    try:
        os.utime(src.fd)
    except OSError as e:
        log.debug("could not touch %s: %s", moved, e)
    return moved


def prune_quarantine(path: str, days: float = QUARANTINE_DAYS, dir_fd: int | None = None) -> int:
    """Delete the .corrupt files in one staging folder that were set aside
    more than `days` ago (a good copy has been fetched again, or never will
    be). Only regular files are touched. With dir_fd (the folder, already
    open) everything goes through it; path is then only used in log lines.
    Returns how many were deleted."""
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        with os.scandir(path if dir_fd is None else dir_fd) as it:
            old = [e for e in it if e.name.endswith(".corrupt") and _regular_file(e)
                   and e.stat(follow_symlinks=False).st_mtime < cutoff]
    except OSError as e:
        log.debug("cannot list %s for old quarantined files: %s", path, e)
        return 0
    for e in old:
        full = os.path.join(path, e.name)
        try:
            os.remove(full if dir_fd is None else e.name, dir_fd=dir_fd)
            removed += 1
            log.info("deleted %s: quarantined more than %g days ago", full, days)
        except OSError as err:
            log.warning("could not delete old quarantined file %s: %s", full, err)
    return removed
