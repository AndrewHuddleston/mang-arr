"""E-reader copies of library chapters: a verified CBZ in, a copy sized for
a reader out, as fixed-layout EPUB 3, Kobo KEPUB, CBZ or PDF.

Optional: the image work needs Pillow (pip install 'mang-arr[convert]'),
and only engine.py imports it. This module, profiles, source and writers
are standard library only, so the server can list profiles and say whether
conversion is available without loading an image decoder; Pillow is loaded
the first time convert_chapter() runs.

    ok, detail = available()               # (True, "Pillow 12.3.0") without importing it
    result = convert_chapter("Chapter 012.0.cbz", "out/Series - Chapter 012.0.epub",
                             Options(profile="generic"), rtl=True, meta={"series": "Series", "number": 12})

Options are what a conversion target sets (profile, format and the page
treatment) and are all that goes into the output besides the source file,
rtl/webtoon, the hints and meta, so equal inputs give byte-identical files.
Memory stays at a few pages whatever the chapter length.
"""
import importlib.metadata
import importlib.util
import math
import os
import re
import secrets
from dataclasses import asdict, dataclass, fields, replace

from .profiles import DEFAULT_PROFILE, PROFILES

ENGINE_VERSION = 1          # raised when the same input would give different output
FORMATS = {"kepub": ".kepub.epub", "epub": ".epub", "cbz": ".cbz", "pdf": ".pdf"}
PILLOW_MIN = (12, 3)        # the floor of the convert extra (pyproject.toml, requirements-convert.txt)
INSTALL_HINT = "pip install 'mang-arr[convert]'"

SPREADS = ("split", "rotate", "both", "keep")
COLOUR = ("auto", "grey")
WEBTOON_COUNTRIES = {"KR": "Korea", "CN": "China", "TW": "Taiwan"}
MAX_THREADS = 8


class ConvertError(Exception):
    """The chapter cannot be converted as it is (a bad page, no pages); the
    message names the page when there is one. Trying again will not help
    until the file changes."""


def available() -> tuple[bool, str]:
    """Can this install convert? (ok, detail), without importing Pillow."""
    if importlib.util.find_spec("PIL") is None:
        return False, f"Pillow is not installed: {INSTALL_HINT}"
    try:
        version = importlib.metadata.version("Pillow")
    except importlib.metadata.PackageNotFoundError:
        return False, f"a PIL module is installed but not as the Pillow package: {INSTALL_HINT}"
    have = tuple(int(n) for n in re.findall(r"\d+", version)[:2])
    if have < PILLOW_MIN:
        floor = ".".join(map(str, PILLOW_MIN))
        return False, f"Pillow {version} is older than {floor}, the oldest tested: pip install -U 'mang-arr[convert]'"
    return True, f"Pillow {version}"


def _check(ok: bool, what: str) -> None:
    if not ok:
        raise ValueError(what)


def _is_bool(v) -> bool:
    return isinstance(v, bool)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


@dataclass(frozen=True)
class Options:
    """What a conversion target sets. Checked on creation (ValueError with a
    plain message), so an Options that exists is one the engine accepts."""
    profile: str = DEFAULT_PROFILE
    format: str | None = None       # epub | kepub | cbz | pdf; None: the profile's default
    spreads: str = "split"          # double pages on a portrait screen: split | rotate | both | keep
    crop: bool = True               # cut uniform margins (and a lone page number in one)
    gamma: float = 1.0              # e-ink grey pages only: above 1 darkens mid-tones
    quality: int | None = None      # JPEG quality 50-95; None: the profile's
    upscale: bool = False           # enlarge pages smaller than the screen
    colour: str = "auto"            # auto: colour pages stay colour where the profile shows colour; grey: never
    pad: bool = False               # every page exactly the screen size, filled with its border colour

    def __post_init__(self):
        _check(self.profile in PROFILES, f"unknown profile {self.profile!r}")
        prof = PROFILES[self.profile]
        _check(self.format is None or self.format in prof.formats,
               f"format {self.format!r} is not offered for {prof.name} (use {', '.join(prof.formats)})")
        _check(self.spreads in SPREADS, f"spreads must be one of {', '.join(SPREADS)}")
        _check(self.colour in COLOUR, f"colour must be one of {', '.join(COLOUR)}")
        for name in ("crop", "upscale", "pad"):
            _check(_is_bool(getattr(self, name)), f"{name} must be true or false")
        _check(_is_number(self.gamma) and 0.5 <= self.gamma <= 3.0, "gamma must be between 0.5 and 3.0")
        _check(self.quality is None or (isinstance(self.quality, int) and not _is_bool(self.quality)
                                        and 50 <= self.quality <= 95), "quality must be a whole number from 50 to 95")

    @classmethod
    def from_dict(cls, d: dict) -> "Options":
        """From stored or posted JSON; unknown keys are refused, not ignored."""
        _check(isinstance(d, dict), "options must be an object")
        unknown = set(d) - {f.name for f in fields(cls)}
        _check(not unknown, f"unknown option(s): {', '.join(sorted(map(str, unknown)))}")
        return cls(**d)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def output_format(self) -> str:
        return self.format or PROFILES[self.profile].default_format

    @property
    def jpeg_quality(self) -> int:
        return self.quality or PROFILES[self.profile].quality


@dataclass(frozen=True)
class Result:
    path: str | None        # the file written, None when the caller passed an open file
    format: str
    profile: str
    pages: int
    bytes: int
    seconds: float
    rtl: bool
    webtoon: bool
    decided: str            # why that layout and direction, in plain words
    engine: int = ENGINE_VERSION


def check_hints(hints) -> dict:
    """Layout hints from the series' metadata: long_strip (MangaDex/AniList
    tag, None when unknown) and country (AniList countryOfOrigin, "" when
    unknown)."""
    hints = {} if hints is None else hints
    _check(isinstance(hints, dict) and set(hints) <= {"long_strip", "country"},
           "hints are long_strip and country only")
    long_strip = hints.get("long_strip")
    country = hints.get("country") or ""
    _check(long_strip is None or _is_bool(long_strip), "long_strip must be true, false or unknown")
    _check(isinstance(country, str) and len(country) <= 3, "country must be a short country code")
    return {"long_strip": long_strip, "country": country.upper()}


def convert_chapter(src, dst, options: Options | None = None, *, rtl: bool | None = None,
                    webtoon: bool | None = None, hints: dict | None = None, meta: dict | None = None,
                    threads: int = 2, progress=None) -> Result:
    """Convert one chapter.

    src: a CBZ path or an open binary file (the library file, already
    verified). dst: the output path, or an open binary file to write into.
    With a path, the copy is written to a temporary name in the same folder
    and renamed over dst only once complete and synced (with the umask's
    permissions); on any failure nothing is left behind. With a file, the
    caller does all of that.

    rtl: None reads ComicInfo's <Manga>YesAndRightToLeft</Manga>, otherwise
    left to right. webtoon: None decides from the page shapes and hints
    (see engine.detect_layout). A webtoon always reads left to right.
    meta: see writers.book_from. threads: pages worked on at once.
    progress(done, total) is called after each source page; an exception it
    raises stops the conversion (that is how a caller cancels).

    Raises ConvertError for a chapter that cannot be converted, ValueError
    for bad arguments, RuntimeError when Pillow is missing, and OSError for
    read or write failures.
    """
    options = options or Options()
    _check(isinstance(options, Options), "options must be an Options")
    _check(rtl is None or _is_bool(rtl), "rtl must be true, false or None")
    _check(webtoon is None or _is_bool(webtoon), "webtoon must be true, false or None")
    _check(meta is None or isinstance(meta, dict), "meta must be a dict")
    _check(isinstance(threads, int) and not _is_bool(threads) and 1 <= threads <= MAX_THREADS,
           f"threads must be from 1 to {MAX_THREADS}")
    hints = check_hints(hints)
    ok, detail = available()
    if not ok:
        raise RuntimeError(detail)
    from . import engine

    args = {"options": options, "rtl": rtl, "webtoon": webtoon, "hints": hints, "meta": dict(meta or {}),
            "threads": threads, "progress": progress}
    if isinstance(src, (str, os.PathLike)):
        with open(src, "rb") as f:
            return _convert_to(engine, f, os.path.basename(os.fspath(src)), dst, args)
    name = getattr(src, "name", "")              # an int for a file opened from a descriptor
    return _convert_to(engine, src, os.path.basename(name) if isinstance(name, str) else "", dst, args)


def _convert_to(engine, src, name: str, dst, args: dict) -> Result:
    if not isinstance(dst, (str, os.PathLike)):
        return engine.convert(src, dst, name=name, **args)
    dst = os.path.abspath(dst)
    tmp = os.path.join(os.path.dirname(dst), f".mangarr-{secrets.token_hex(8)}.part")
    fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                 0o666)
    try:
        with open(fd, "w+b") as out:
            result = engine.convert(src, out, name=name, **args)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return replace(result, path=dst)
