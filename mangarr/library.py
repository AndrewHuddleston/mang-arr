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
swapped for a symlink mid-import changes nothing, archives are checked with
hard caps before zipfile parses or decompresses them, and every name
mang-arr creates fits the filesystem's 255-byte limit.
"""
import errno
import hashlib
import logging
import os
import re
import secrets
import shutil
import stat
import struct
import time
import unicodedata
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager

from . import config

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
    m = _SEASON.search(os.path.splitext(os.path.basename(filename or ""))[0][:MAX_PARSE])
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


NAME_MAX = 255          # bytes in one file or folder name (ext4, btrfs, xfs, zfs ...)


def fit_name(name: str, max_bytes: int = NAME_MAX, keep: str = "") -> str:
    """name (plus the fixed ending `keep`, e.g. '.cbz') cut to at most
    max_bytes of UTF-8, on a character boundary. A cut name gets a short
    hash of the full name, so two long names that share a prefix stay
    distinct and the same name always gives the same result."""
    full = name + keep
    if len(full.encode("utf-8")) <= max_bytes:
        return full
    tag = "~" + hashlib.sha1(full.encode("utf-8")).hexdigest()[:8]
    room = max_bytes - len((tag + keep).encode("utf-8"))
    cut = name.encode("utf-8")[:max(room, 0)].decode("utf-8", "ignore").rstrip(" .")
    log.debug("name longer than %d bytes, shortened: %r -> %r", max_bytes, full[:80], cut[:40] + "..." + tag)
    return cut + tag + keep


def _folder_key(name: str) -> str:
    """How a filesystem may see a folder name: case-insensitive (macOS,
    Windows, SMB mounts) and normalisation-insensitive (NFC vs NFD)."""
    return unicodedata.normalize("NFC", name).casefold()


def unique_folder(title: str, taken: set[str], suffix: str) -> str:
    """A folder name not already used by another series: the title, or the
    title plus a disambiguating suffix (the series ref), plus a counter if
    even that is taken. Names are NFC, fit NAME_MAX bytes, and are compared
    case- and normalisation-insensitively, so two series never share a
    folder on a case-insensitive mount either."""
    keys = {_folder_key(t) for t in taken if t}
    base = unicodedata.normalize("NFC", safe_title(title))
    for i in range(1000):
        cand = base if i == 0 else f"{base} ({suffix})" if i == 1 else f"{base} ({suffix}) {i}"
        cand = fit_name(unicodedata.normalize("NFC", safe_title(cand)))
        if _folder_key(cand) not in keys:
            if cand != base:
                log.info("library folder %r is taken or too long; using %r", base[:120], cand)
            return cand
    raise ValueError(f"no free library folder name for {title[:120]!r}")


def chapter_filename(number: float, label: str | None = None) -> str:
    """'Chapter 012.0.cbz' - fixed width so Komga sorts 12 before 12.5 and
    before 100, and .5 chapters sort after their integer. Two decimals only
    when the number needs them (5.25). A label (the source's chapter title
    when it says more than the number, e.g. 'S2 - Episode 5') is appended:
    'Chapter 012.0 - S2 - Episode 5.cbz'."""
    base = f"Chapter {number:06.2f}" if round(number, 1) != round(number, 2) else f"Chapter {number:05.1f}"
    label = chapter_label(number, label)
    return fit_name(f"{base} - {label}", keep=".cbz") if label else f"{base}.cbz"


def chapter_label(number: float, name: str | None) -> str | None:
    """The part of a source chapter name worth keeping in the file name:
    None for 'Chapter 12' / 'Ch.12' / 'Episode 12', the name otherwise."""
    if not name:
        return None
    # capped and whitespace-collapsed first, so the patterns below (anchored,
    # no nested quantifiers) stay cheap on a hostile source's chapter name
    n = re.sub(r"\s+", " ", name[:MAX_PARSE]).strip()
    plain = _PLAIN_NAME.fullmatch(n)
    if plain and float(plain.group(1)) == number:
        return None
    lead = _LEAD_NAME.match(n)
    if lead and float(lead.group(1)) == number:
        n = lead.group(2).strip()
    return safe_title(n)[:80]


# 'Chapter 12', 'Ch.012:' ... (float() drops leading zeros, so no 0* here:
# 0* followed by \d+ backtracks quadratically on a long run of zeros)
_PLAIN_NAME = re.compile(r"(?:chapter|chap|ch|episode|ep|#)?\.? ?(\d+(?:\.\d+)?) ?[:.\-]? ?", re.I)
_LEAD_NAME = re.compile(r"(?:chapter|chap|ch|episode|ep|#)?\.? ?(\d+(?:\.\d+)?) ?[:.\-–] ?(.+)$", re.I)


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
            raise
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
    path, which must be a regular file, never a symlink. The link or copy is
    made under a hidden temporary name, checked to be the very file that
    was opened, and only then given its real name, so the library never
    shows a half-written copy or a file swapped in after the check."""
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
        finally:
            _unlink_at(tmp, dir_fd)
    finally:
        os.close(dir_fd)
    COPIED += copied
    return dst


def _link_or_copy(src: StagedFile, dir_fd: int, tmp: str, dst: str) -> bool:
    """Put the opened staged file at `tmp` in the library folder dir_fd: a
    hard link when possible, else a copy read from the open file. Returns
    whether it was copied. A hard link can only be made by name, so it is
    checked to be the file that was opened (the name may have been swapped)."""
    global _copy_warned
    try:
        os.link(src.name, tmp, src_dir_fd=src.dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except OSError as e:
        if e.errno not in _COPY_ERRNOS:
            raise
        if not _copy_warned:
            log.warning("cannot hard-link %s -> %s (%s); copying instead. Staging and library are on "
                        "different filesystems or mounts, so every chapter is stored twice", src.path, dst, e)
            _copy_warned = True
        _copy_into(src, dir_fd, tmp)
        return True
    if not src.is_same(_lstat_at(tmp, dir_fd)):
        raise OSError(errno.EAGAIN, "the staged file was replaced while it was being imported", src.path)
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

_END = struct.Struct("<4s4H2LH")                # end of central directory record
_END64_LOCATOR = struct.Struct("<4sLQL")        # zip64 end of central directory locator
_END64 = struct.Struct("<4sQ2H2L4Q")            # zip64 end of central directory record
_CENTRAL = struct.Struct("<4s4B4HL2L5H2L")      # one central directory header


def verify_archive(src: str | StagedFile) -> tuple[bool, str]:
    """Is this a readable comic archive with at least one image? Returns
    (ok, detail) and never raises: a truncated, corrupt, encrypted or
    oversized file (or a symlink) must not reach the library, and must not
    stop the import of the other chapters either. src is an opened
    StagedFile or a path (never opened through a symlink). A directory too
    big for a chapter is refused from the end records alone, before zipfile
    reads it; oversized or over-compressed contents are refused from the
    directory, before anything is decompressed."""
    if isinstance(src, str):
        try:
            with open_staged(src) as f:
                return verify_archive(f)
        except NotRegularFile:
            return False, "not a regular file"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"[:300]
    try:
        size = src.st.st_size
        if size < 1024:
            return False, "file is empty"
        with src.reader() as f:
            end = _end_records(f, size)
            if end is None:
                return False, "not a zip archive"
            problem = _directory_limits(f, *end)
            if problem:
                return _rejected(src.path, problem)
            f.seek(0)
            with zipfile.ZipFile(f) as z:
                infos = z.infolist()
                problem = _archive_limits(infos, size)
                if problem:
                    return _rejected(src.path, problem)
                bad = _read_entries(z, infos, src.path)
                if bad:
                    return False, bad
        images = [i.filename for i in infos if i.filename.lower().endswith(_IMAGE_EXT)]
        if not images:
            return False, "no images inside"
        return True, f"{len(images)} pages"
    except Exception as e:      # zlib.error, NotImplementedError, RuntimeError (encrypted), EOFError ...
        return False, f"{type(e).__name__}: {e}"[:300]


def _rejected(path: str, problem: str) -> tuple[bool, str]:
    log.warning("%s rejected: %s", path, problem)
    return False, problem


def _end_records(f, size: int) -> tuple[int, int, int] | None:
    """(entries, directory size, directory start) from the end of a zip, or
    None when it has no end record. The records are found the way zipfile
    finds them (the last 22 bytes, else the last signature in the final
    64 KiB, then a zip64 locator right before it), reading only the tail."""
    tail_at = max(size - (1 << 16) - _END.size, 0)
    f.seek(tail_at)
    tail = f.read()
    if len(tail) >= _END.size and tail[-_END.size:][:4] == b"PK\x05\x06" and tail[-2:] == b"\0\0":
        pos = len(tail) - _END.size
    else:
        pos = tail.rfind(b"PK\x05\x06")
        if pos < 0 or pos + _END.size > len(tail):
            return None
    _, disk, cd_disk, _, entries, cd_size, _, _ = _END.unpack_from(tail, pos)
    at = tail_at + pos                          # the directory ends where the end records start
    if at >= _END64_LOCATOR.size:
        f.seek(at - _END64_LOCATOR.size)
        sig, loc_disk, rec_at, disks = _END64_LOCATOR.unpack(f.read(_END64_LOCATOR.size))
        if sig == b"PK\x06\x07":
            # only the plain layout, the record right before its locator: zipfile
            # versions differ on the others, and no chapter archive needs them
            if loc_disk or disks > 1 or rec_at != at - _END64_LOCATOR.size - _END64.size:
                raise zipfile.BadZipFile("unsupported zip64 end records")
            f.seek(rec_at)
            sig, _, _, _, disk, cd_disk, _, entries, cd_size, _ = _END64.unpack(f.read(_END64.size))
            if sig != b"PK\x06\x06":
                raise zipfile.BadZipFile("zip64 end of central directory record not found")
            at = rec_at
    if disk or cd_disk:
        raise zipfile.BadZipFile("archives split over several disks are not supported")
    return entries, cd_size, at - cd_size


def _directory_limits(f, entries: int, cd_size: int, cd_at: int) -> str | None:
    """Why an archive's directory is too big for a chapter, or None. The
    declared count and size come first; then the directory is walked header
    by header without building anything, because the count in the end record
    can understate what zipfile would parse."""
    if entries > MAX_ENTRIES:
        return f"{entries} entries (limit {MAX_ENTRIES})"
    if cd_size > MAX_DIRECTORY:
        return f"{cd_size} bytes of directory (limit {MAX_DIRECTORY})"
    if cd_at < 0:
        raise zipfile.BadZipFile("bad offset for central directory")
    f.seek(cd_at)
    cd = f.read(cd_size)
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
    """Why an archive's parsed directory is implausible for a chapter, or None."""
    if len(infos) > MAX_ENTRIES:
        return f"{len(infos)} entries (limit {MAX_ENTRIES})"
    total = sum(i.file_size for i in infos)
    if total > MAX_UNCOMPRESSED:
        return f"{total} bytes uncompressed (limit {MAX_UNCOMPRESSED})"
    for i in infos:
        if i.file_size > RATIO_MIN_SIZE and i.file_size > MAX_RATIO * max(i.compress_size, 1):
            return f"entry {i.filename[:80]!r} expands {i.file_size // max(i.compress_size, 1)}x (limit {MAX_RATIO}x)"
    # entries can share compressed bytes (overlapping entries), so the file's
    # own size is what the archive as a whole can honestly expand from
    if total > RATIO_MIN_SIZE and total > MAX_RATIO * size:
        return f"{total} bytes uncompressed from a {size} byte file: {total // size}x (limit {MAX_RATIO}x)"
    return None


def _read_entries(z, infos, path: str) -> str | None:
    """zipfile's testzip, bounded: read every entry back (which checks its
    CRC) in 1 MiB pieces and give up after VERIFY_SECONDS. The limits above
    already cap how much can be decompressed; this caps the time on a slow
    disk. Returns what is wrong, or None."""
    deadline = time.monotonic() + VERIFY_SECONDS
    for i in infos:
        try:
            with z.open(i) as e:
                while e.read(1 << 20):
                    if time.monotonic() > deadline:
                        problem = f"checking it took longer than {VERIFY_SECONDS}s"
                        log.warning("%s rejected: %s", path, problem)
                        return problem
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
    if not src.is_same(os.stat(src.name, dir_fd=src.dir_fd, follow_symlinks=False)):
        raise OSError(errno.EAGAIN, "the file was replaced while it was being checked; not set aside", src.path)
    dst = src.name + ".corrupt"
    os.replace(src.name, dst, src_dir_fd=src.dir_fd, dst_dir_fd=src.dir_fd)
    try:
        os.utime(src.fd)
    except OSError as e:
        log.debug("could not touch %s: %s", dst, e)
    return os.path.join(os.path.dirname(src.path), dst)


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
