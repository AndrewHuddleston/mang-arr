"""The files: what Suwayomi wrote, and the clean per-series library.

Suwayomi downloads into <staging>/<Source>/<Series>/<scanlator>_<chapter name>.cbz
and treats that tree as its record of what is downloaded, so it must never
be renamed. The library is a second tree, <library>/<folder>/Chapter 012.0.cbz,
built from hard links, that Komga reads. One folder per tracked series
(the folder name is stored with the series, so two series with the same
title never share one), regardless of how many sources the chapters came from.

Everything under staging is written by Suwayomi and its extensions, so it is
treated as untrusted: symlinks and special files are skipped, archives are
checked with hard caps before anything is linked, and every name mang-arr
creates fits the filesystem's 255-byte limit.
"""
import errno
import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import unicodedata

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


def scan_series_dir(path: str) -> tuple[dict[float, str], list[str]]:
    """{chapter number: file path} for one series folder, plus the files
    whose number could not be read. Duplicate numbers keep the first name.
    Symlinks and anything that is not a regular file are skipped (logged)."""
    found: dict[float, str] = {}
    unparsed: list[str] = []
    try:
        with os.scandir(path) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as e:
        log.error("cannot list %s: %s", path, e)
        return found, unparsed
    for entry in entries:
        name = entry.name
        if not name.lower().endswith(_EXT):
            continue
        full = entry.path
        if not _regular_file(entry):
            log.warning("skipping %s: a symlink or not a regular file", full)
            continue
        n = parse_number(name)
        if n is None:
            unparsed.append(full)
        elif n not in found:
            found[n] = full
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
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    try:
        return os.path.commonpath([real, real_root]) == real_root
    except ValueError:            # mixed absolute/relative, or different drives
        return False


_copy_warned = False
COPIED = 0          # chapters copied because a hard link was impossible
# os.link errors that mean "hard links cannot work here", where a copy is the
# right fallback: another filesystem or mount (EXDEV), links refused
# (EPERM: fs.protected_hardlinks, SMB/FUSE mounts), or the inode is out of links.
_COPY_ERRNOS = (errno.EXDEV, errno.EPERM, errno.EMLINK)


def _same_file(a: str, b: str) -> bool:
    """Is b the very inode a is (a hard link), without following symlinks?"""
    try:
        sa, sb = os.lstat(a), os.lstat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def link_into_library(src_path: str, folder: str, number: float, root: str | None = None,
                      replace: bool = False, label: str | None = None) -> str | None:
    """Hard-link a staged chapter into the library. Copies when the two
    trees are on different filesystems or mounts (logged once). Returns the
    library path, or None when a different file already sits there and
    `replace` is False - the library never overwrites what it did not make.
    The source must be a regular file (never a symlink), a copy is written to
    a temporary name and moved into place, so an interrupted copy never
    leaves a truncated chapter under the real name."""
    global _copy_warned, COPIED
    st = os.lstat(src_path)
    if not stat.S_ISREG(st.st_mode):
        log.error("%s is not a regular file (symlink or special file); not linking it", src_path)
        return None
    d = library_dir(folder, root)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, chapter_filename(number, label))
    if os.path.lexists(dst):
        if _same_file(src_path, dst):
            return dst
        if not replace:
            log.error("%s exists and is not a link to %s; leaving it alone", dst, src_path)
            return None
        os.remove(dst)                                  # a symlink is removed, not followed
    try:
        os.link(src_path, dst, follow_symlinks=False)
    except OSError as e:
        if e.errno not in _COPY_ERRNOS:
            raise
        if not _copy_warned:
            log.warning("cannot hard-link %s -> %s (%s); copying instead. Staging and library are on "
                        "different filesystems or mounts, so every chapter is stored twice", src_path, dst, e)
            _copy_warned = True
        _copy_atomic(src_path, dst)
        COPIED += 1
    return dst


def _copy_atomic(src: str, dst: str) -> None:
    """Copy src to a hidden temporary file next to dst, flush it to disk, then
    move it into place without replacing anything that appeared at dst in the
    meantime. The temporary file is removed on any failure (disk full ...)."""
    fd, tmp = tempfile.mkstemp(prefix=".", suffix=".part", dir=os.path.dirname(dst))
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(src, tmp)
        try:
            os.link(tmp, dst)                       # fails if dst exists: never writes through it
        except OSError as e:
            if e.errno == errno.EEXIST or os.path.lexists(dst):
                raise FileExistsError(errno.EEXIST, "appeared while copying", dst) from e
            os.replace(tmp, dst)                    # no hard links on this filesystem at all
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def scan_library_dir(folder: str, root: str | None = None) -> dict[float, str]:
    d = library_dir(folder, root)
    if not os.path.isdir(d):
        return {}
    found, _ = scan_series_dir(d)
    return found


_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".jxl")

# Limits for one chapter archive, far above any real chapter (a long webtoon
# chapter is a few hundred pages and a few hundred MB). Checked from the zip
# directory before anything is decompressed.
MAX_ENTRIES = 5000
MAX_UNCOMPRESSED = 2 << 30          # 2 GiB, declared total
MAX_RATIO = 100                     # per entry, uncompressed / compressed ...
RATIO_MIN_SIZE = 1 << 20            # ... for entries over 1 MiB (images barely compress)


def verify_archive(path: str) -> tuple[bool, str]:
    """Is this a readable comic archive with at least one image? Returns
    (ok, detail) and never raises: a truncated, corrupt, encrypted or
    oversized file (or a symlink) must not reach the library, and must not
    stop the import of the other chapters either."""
    import zipfile
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return False, "not a regular file"
        if st.st_size < 1024:
            return False, "file is empty"
        # no symlink (O_NOFOLLOW), and never block on a FIFO swapped in after the check
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as f:
            if not zipfile.is_zipfile(f):
                return False, "not a zip archive"
            f.seek(0)
            with zipfile.ZipFile(f) as z:
                infos = z.infolist()
                problem = _archive_limits(infos)
                if problem:
                    log.warning("%s rejected: %s", path, problem)
                    return False, problem
                bad = z.testzip()
                if bad:
                    return False, f"corrupt entry {bad}"
                images = [i.filename for i in infos if i.filename.lower().endswith(_IMAGE_EXT)]
        if not images:
            return False, "no images inside"
        return True, f"{len(images)} pages"
    except Exception as e:      # zlib.error, NotImplementedError, RuntimeError (encrypted), EOFError ...
        return False, f"{type(e).__name__}: {e}"[:300]


def _archive_limits(infos) -> str | None:
    """Why an archive's directory is implausible for a chapter, or None."""
    if len(infos) > MAX_ENTRIES:
        return f"{len(infos)} entries (limit {MAX_ENTRIES})"
    total = sum(i.file_size for i in infos)
    if total > MAX_UNCOMPRESSED:
        return f"{total} bytes uncompressed (limit {MAX_UNCOMPRESSED})"
    for i in infos:
        if i.file_size > RATIO_MIN_SIZE and i.file_size > MAX_RATIO * max(i.compress_size, 1):
            return f"entry {i.filename[:80]!r} expands {i.file_size // max(i.compress_size, 1)}x (limit {MAX_RATIO}x)"
    return None


QUARANTINE_DAYS = 14    # .corrupt files older than this are deleted


def quarantine(path: str) -> str:
    """Move a bad staged file aside (same folder, .corrupt suffix) so Suwayomi
    sees the chapter as not downloaded and it can be fetched again. Its time
    is set to now, so prune_quarantine counts the days from here."""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise OSError(errno.EINVAL, "not a regular file; not quarantined", path)
    dst = path + ".corrupt"
    os.replace(path, dst)
    try:
        os.utime(dst)
    except OSError as e:
        log.debug("could not touch %s: %s", dst, e)
    return dst


def prune_quarantine(path: str, days: float = QUARANTINE_DAYS) -> int:
    """Delete the .corrupt files in one staging folder that were set aside
    more than `days` ago (a good copy has been fetched again, or never will
    be). Only regular files are touched. Returns how many were deleted."""
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        with os.scandir(path) as it:
            old = [e for e in it if e.name.endswith(".corrupt") and _regular_file(e)
                   and e.stat(follow_symlinks=False).st_mtime < cutoff]
    except OSError as e:
        log.debug("cannot list %s for old quarantined files: %s", path, e)
        return 0
    for e in old:
        try:
            os.remove(e.path)
            removed += 1
            log.info("deleted %s: quarantined more than %g days ago", e.path, days)
        except OSError as err:
            log.warning("could not delete old quarantined file %s: %s", e.path, err)
    return removed
