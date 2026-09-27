"""The image work of a conversion. Needs Pillow; nothing imports this
module until a conversion runs (see __init__).

Per chapter: pages in natural order -> per page, a few at a time in a
thread pool (Pillow releases the GIL while decoding, resizing and
encoding): mode fix -> colour check -> margin crop -> spread split or
rotate -> fit to the screen -> e-ink tone (autocontrast + gamma) -> pad
-> baseline JPEG. Tall webtoon strips go through a streaming splitter
instead, which cuts them at blank gaps near screen height. Finished pages
are handed to the writer in order as they come, so memory holds a few
pages (for a webtoon, the next strip joined to the unsent rows, about two
strips) whatever the length.

Decoding is hardened for pages from anywhere: only the DECODERS formats
are tried, an image over MAX_PIXELS is refused from its header before it
is decoded, a truncated image is an error (not a grey half page), and an
animated image gives its first frame. Every failure is a ConvertError
naming the page.
"""
import io
import math
import os
import struct
import time
import warnings
import zipfile
from array import array
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from itertools import accumulate
from statistics import median

from PIL import Image, ImageChops, ImageFile, ImageOps

from . import WEBTOON_COUNTRIES, ConvertError, Options, Result, source
from .profiles import PROFILES, Profile
from .writers import WRITERS, Page, book_from

# Page analysis. Tuned on synthetic pages; change one only with a test page
# that shows why.
CROP_TOL = 32               # grey levels from the border colour that still count as margin
CROP_MAX_FRAC = 0.15        # at most this much of the page is cut from each side
SPREAD_RATIO = 1.15         # wider than tall by this much: a double page (on a portrait screen)
PANORAMA_RATIO = 1.8        # wider still: rotated, never split
WEBTOON_RATIO = 2.5         # a page this much taller than wide is a strip
WEBTOON_HINT_RATIO = 1.5    # ... or this much, for a series tagged long strip or from WEBTOON_COUNTRIES
WEBTOON_KEEP = 1.2          # a panel up to this many screens tall stays whole on one (shrunk) page
WEBTOON_TWO = 2.0           # ... up to this many becomes two overlapping screens; taller is cut ...
WEBTOON_OVERLAP = 0.04      # ... at screen height, each screen repeating this much of the last
COLOUR_FRAC = 0.005         # e-ink: share of tinted pixels that makes a page colour ...
SCREEN_COLOUR_FRAC = 1e-4   # ... other screens: any tinted spot over about 0.01% of the page
MAX_PIXELS = 64_000_000     # per image: an 800 x 80000 strip; 256 MB decoded as RGB (4 bytes a pixel)

LANCZOS = Image.Resampling.LANCZOS
BOX = Image.Resampling.BOX
DECODERS = ("JPEG", "PNG", "WEBP", "GIF", "BMP", "AVIF")    # Pillow never sniffs any other format
_ORIENTATION = 0x0112                                       # EXIF tag; 5-8 swap width and height

Image.MAX_IMAGE_PIXELS = MAX_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = False
warnings.simplefilter("error", Image.DecompressionBombWarning)   # over MAX_PIXELS: refused, not warned about

# what Pillow raises for a damaged or hostile image (UnidentifiedImageError is an OSError)
_IMAGE_ERRORS = (OSError, SyntaxError, ValueError, EOFError, IndexError, TypeError, struct.error)
_BOMB = (Image.DecompressionBombError, Image.DecompressionBombWarning)


# ---------------------------------------------------------------- reading

def _turned(img: Image.Image) -> bool:
    """Does the EXIF orientation swap width and height? Asked of JPEGs only,
    which carry EXIF in their header: other formats decode the whole image
    to look for it."""
    return img.format in ("JPEG", "MPO") and img.getexif().get(_ORIENTATION, 1) in (5, 6, 7, 8)


def _displayed_size(img: Image.Image) -> tuple[int, int]:
    w, h = img.size
    return (h, w) if _turned(img) else (w, h)


def _too_large(name: str) -> ConvertError:
    return ConvertError(f"{name}: image too large (over {MAX_PIXELS / 1e6:g} megapixels)")


def image_size(z: zipfile.ZipFile, name: str) -> tuple[int, int]:
    """A page's size as shown, from its header only."""
    try:
        if z.getinfo(name).file_size > source.MAX_ENTRY_BYTES:
            raise source.too_big(name, source.MAX_ENTRY_BYTES)
        with z.open(name) as f, Image.open(f, formats=DECODERS) as img:
            w, h = _displayed_size(img)
    except _BOMB:
        raise _too_large(name) from None
    except (*source.ENTRY_ERRORS, *_IMAGE_ERRORS) as e:
        raise ConvertError(f"{name}: unreadable image ({e})") from e
    if w * h > MAX_PIXELS:
        raise _too_large(name)
    return w, h


def load(z: zipfile.ZipFile, name: str, prof: Profile | None = None, grey: bool = False) -> Image.Image:
    """One page decoded as RGB or L, upright, alpha flattened onto white.
    With prof, a JPEG much larger than the screen is decoded at 1/2, 1/4 or
    1/8 scale (see _draft); grey then also decodes it straight to grey."""
    data = source.read_entry(z, name)
    try:
        img = Image.open(io.BytesIO(data), formats=DECODERS)
        w, h = img.size
        if w * h > MAX_PIXELS:
            raise _too_large(name)
        if prof is not None and img.format == "JPEG":
            _draft(img, prof, grey)
        img.load()                              # an animation's first frame
        return _normalise(img)
    except _BOMB:
        raise _too_large(name) from None
    except ConvertError:
        raise
    except _IMAGE_ERRORS as e:
        raise ConvertError(f"{name}: unreadable image ({e})") from e


def _draft(img: Image.Image, prof: Profile, grey: bool) -> None:
    """Let libjpeg decode at a reduced scale when that loses nothing: the
    decoded page must still be at least the screen size after the largest
    crop (CROP_MAX_FRAC off each side), which also covers split halves
    (their height is the page's) and rotated spreads. Pillow picks the
    largest scale that keeps both sides at or above the size asked for."""
    keep = 1 - 2 * CROP_MAX_FRAC
    need_w, need_h = math.ceil(prof.width / keep), math.ceil(prof.height / keep)
    if _turned(img):
        need_w, need_h = need_h, need_w         # asked in the stored orientation
    if img.width >= 2 * need_w and img.height >= 2 * need_h:
        img.draft("L" if grey else None, (need_w, need_h))


def _normalise(img: Image.Image) -> Image.Image:
    ImageOps.exif_transpose(img, in_place=True)
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, (255, 255, 255))
        img.paste(rgba, mask=rgba.getchannel("A"))
    elif img.mode.startswith("I"):              # 16-bit grey: to 8 bits by scaling, not by clipping
        img = img.convert("I").point(lambda v: v / 256).convert("L")
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")                # P, CMYK, 1 ... (no ICC colour management)
    return img


def detect_layout(sizes: list[tuple[int, int]], hints: dict) -> tuple[bool, str]:
    """Webtoon or paged, and why. A webtoon when more than half the pages
    are strips (WEBTOON_RATIO), or when the metadata says so (tagged long
    strip, or from Korea, China or Taiwan) and the pages are tall anyway
    (median WEBTOON_HINT_RATIO): 800x1280 pieces of a strip are not strips
    on their own."""
    n = len(sizes)
    tall = sum(h >= WEBTOON_RATIO * w for w, h in sizes)
    if tall * 2 > n:
        return True, f"{tall} of {n} pages are tall strips"
    ratio = median(h / w for w, h in sizes)
    hint = ("tagged long strip" if hints.get("long_strip")
            else f"from {WEBTOON_COUNTRIES[hints['country']]}" if hints.get("country") in WEBTOON_COUNTRIES else "")
    if hint and ratio >= WEBTOON_HINT_RATIO:
        return True, f"pages {ratio:.1f}x as tall as wide and {hint}"
    return False, f"{tall} of {n} pages are tall strips"


def ordered(pool: ThreadPoolExecutor, fn, items, ahead: int):
    """pool.map that keeps at most `ahead` tasks in flight, so results come
    in order and only a few pages are held at once."""
    pending: deque = deque()
    for item in items:
        pending.append(pool.submit(fn, item))
        if len(pending) > ahead:
            yield pending.popleft().result()
    while pending:
        yield pending.popleft().result()


# ---------------------------------------------------------------- page analysis

_CHROMA = [255 if abs(v - 128) > 12 else 0 for v in range(256)]
_INK = [255 if v > 40 else 0 for v in range(256)]


def _median(hist: list[int]) -> int:
    total = sum(hist)
    return next(v for v, acc in enumerate(accumulate(hist)) if acc * 2 >= total)


def _edges(img: Image.Image) -> list[Image.Image]:
    w, h = img.size
    return [img.crop(b) for b in ((0, 0, w, 1), (0, h - 1, w, h), (0, 0, 1, h), (w - 1, 0, w, h))]


def _edge_hist(img: Image.Image) -> list[int]:
    hist = [0] * 256
    for e in _edges(img):
        hist = [a + b for a, b in zip(hist, e.histogram(), strict=True)]
    return hist


def border_colour(img: Image.Image) -> int | tuple[int, ...]:
    """The median colour of the outermost pixels, band by band."""
    bands = [_median(_edge_hist(img.getchannel(i))) for i in range(len(img.getbands()))]
    return bands[0] if len(bands) == 1 else tuple(bands)


def is_colour(img: Image.Image, prof: Profile) -> bool:
    """Does the page keep its colour? On e-ink only when more than
    COLOUR_FRAC of it is tinted, so a grey page with a small coloured logo
    still gets the e-ink tone; on other screens any tinted spot over
    SCREEN_COLOUR_FRAC (a red sound effect, a drop of blood) keeps it."""
    if img.mode == "L":
        return False
    small = img.reduce(8) if min(img.size) >= 64 else img
    _, cb, cr = small.convert("YCbCr").split()
    tinted = ImageChops.lighter(cb.point(_CHROMA), cr.point(_CHROMA)).histogram()[255]
    return tinted > small.width * small.height * (COLOUR_FRAC if prof.eink else SCREEN_COLOUR_FRAC)


def content_box(img: Image.Image, page_numbers: bool = True) -> tuple[int, int, int, int] | None:
    """Bounding box of everything that differs from the page's border colour
    (the median of the outermost pixels) by more than CROP_TOL; each side
    is cut by at most CROP_MAX_FRAC, so a mostly empty page keeps its
    framing. With page_numbers, a small mark alone in the top or bottom
    margin (a page number, a scanlator's tag) does not stop the crop."""
    g = img.convert("L")
    f = 4 if min(g.size) >= 400 else 1
    small = g.reduce(f) if f > 1 else g        # box-averaging also swallows specks and grain
    bg = _median(_edge_hist(small))
    diff = ImageChops.difference(small, Image.new("L", small.size, bg)).point(
        [255 if v > CROP_TOL else 0 for v in range(256)])
    box = diff.getbbox()
    if not box:
        return None                            # blank page: keep as is
    if page_numbers:
        box = _skip_page_number(diff, box)
    W, H = img.size
    x0 = min(max(0, box[0] - 1) * f, int(W * CROP_MAX_FRAC))
    y0 = min(max(0, box[1] - 1) * f, int(H * CROP_MAX_FRAC))
    x1 = max(min(W, (box[2] + 1) * f), W - int(W * CROP_MAX_FRAC))
    y1 = max(min(H, (box[3] + 1) * f), H - int(H * CROP_MAX_FRAC))
    return (x0, y0, x1, y1) if (x0, y0, x1, y1) != (0, 0, W, H) else None


def _skip_page_number(diff: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    w, h = diff.size
    rows = array("f", diff.crop((0, box[1], w, box[3])).convert("F").resize((1, box[3] - box[1]), BOX).tobytes())
    runs, start = [], None                     # vertical runs of rows with any content
    for y, v in enumerate(rows + array("f", [0])):
        if v > 0 and start is None:
            start = y
        elif v == 0 and start is not None:
            runs.append((box[1] + start, box[1] + y))
            start = None
    if len(runs) < 2:
        return box

    def small(run, gap):
        # under 4% of the page tall, over 1% away from the rest, under a quarter of it wide
        bb = diff.crop((0, run[0], w, run[1])).getbbox()
        return run[1] - run[0] < h * 0.04 and gap > h * 0.01 and bb and bb[2] - bb[0] < w * 0.25

    top, bottom = box[1], box[3]
    if small(runs[-1], runs[-1][0] - runs[-2][1]):
        bottom, runs = runs[-2][1], runs[:-1]
    if len(runs) >= 2 and small(runs[0], runs[1][0] - runs[0][1]):
        top = runs[1][0]
    if (top, bottom) == (box[1], box[3]):
        return box
    inner = diff.crop((0, top, w, bottom)).getbbox()
    return (inner[0], inner[1] + top, inner[2], inner[3] + top) if inner else box


def blank_rows(img: Image.Image) -> list[bool]:
    """Which rows are a gap between panels: at most 2 pixels differ from the row's mean.

    Row by row, so flat colour, vertical gradients and black gutters all count as
    gaps, while the side borders of a framed panel, speech-bubble outlines and
    screentone do not. The outer 2.5% each side is ignored (frame decorations).
    """
    g = img.convert("L")
    pad = g.width // 40
    g = g.crop((pad, 0, g.width - pad, g.height))
    mean = g.resize((1, g.height), BOX).resize(g.size, Image.Resampling.NEAREST)
    off = ImageChops.difference(g, mean).point(_INK).convert("F")
    per_row = array("f", off.resize((1, g.height), BOX).tobytes())
    limit = 2.5 * 255 / g.width
    return [v <= limit for v in per_row]


# ---------------------------------------------------------------- page transforms

def fit(img: Image.Image, prof: Profile, upscale: bool) -> Image.Image:
    scale = min(prof.width / img.width, prof.height / img.height)
    if scale >= 1 and not upscale:
        return img
    size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(size, LANCZOS, reducing_gap=3.0)


def tone(img: Image.Image, prof: Profile, opts: Options, webtoon: bool) -> Image.Image:
    """E-ink only, grey pages only: stretch the contrast (not on a page of
    very low contrast, which was probably meant that way, nor on webtoon
    pages, where it would differ from one cut to the next), then gamma."""
    if img.mode != "L" or not prof.eink:
        return img
    if not webtoon:
        lo, hi = img.getextrema()
        if hi - lo >= 160:
            img = ImageOps.autocontrast(img)
    if opts.gamma != 1.0:
        img = img.point([round(255 * (v / 255) ** opts.gamma) for v in range(256)])
    return img


def pad(img: Image.Image, prof: Profile) -> Image.Image:
    """The page centred on a screen-sized canvas of its own border colour."""
    if img.size == (prof.width, prof.height) or img.width > prof.width or img.height > prof.height:
        return img
    canvas = Image.new(img.mode, (prof.width, prof.height), border_colour(img))
    canvas.paste(img, ((prof.width - img.width) // 2, (prof.height - img.height) // 2))
    return canvas


def encode(img: Image.Image, quality: int, side: str | None = None) -> Page:
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=True, progressive=False)   # baseline: e-readers
    return Page(out.getvalue(), img.width, img.height, img.mode == "L", side)


def finish(img: Image.Image, prof: Profile, opts: Options, webtoon: bool, side: str | None = None) -> Page:
    img = tone(fit(img, prof, opts.upscale), prof, opts, webtoon)
    if opts.pad:
        img = pad(img, prof)
    return encode(img, opts.jpeg_quality, side)


def shows_colour(prof: Profile, opts: Options) -> bool:
    return prof.colour and opts.colour == "auto"


def process_page(img: Image.Image, prof: Profile, opts: Options, rtl: bool, first: bool) -> list[Page]:
    """One source page as one or more output pages: a double page on a
    portrait screen is split at the original fold (right half first when
    reading right to left) and/or rotated, as opts.spreads says; a panorama
    (PANORAMA_RATIO) is only ever rotated."""
    colour = shows_colour(prof, opts) and is_colour(img, prof)
    if not colour:
        img = img.convert("L")
    fold = img.width / 2
    if opts.crop and not (first and colour):   # leave a colour cover alone
        box = content_box(img)
        if box:
            img, fold = img.crop(box), fold - box[0]
    parts: list[tuple[Image.Image, str | None]] = []
    wide = img.width / img.height
    portrait = prof.height > prof.width
    if portrait and wide > SPREAD_RATIO and opts.spreads != "keep":
        split = opts.spreads in ("split", "both") and wide < PANORAMA_RATIO
        if split:
            x = int(min(max(fold, img.width * 0.4), img.width * 0.6))
            left, right = img.crop((0, 0, x, img.height)), img.crop((x, 0, img.width, img.height))
            parts += [(right, "right"), (left, "left")] if rtl else [(left, "left"), (right, "right")]
        if opts.spreads in ("rotate", "both") or not split:
            parts.append((img.rotate(90, expand=True), None))
    else:
        parts.append((img, None))
    return [finish(p, prof, opts, False, side) for p, side in parts]


def strip_width(sizes: list[tuple[int, int]], prof: Profile, opts: Options) -> int:
    """The width of a webtoon's column: the width most of the chapter's
    height has (the median weighted by height), so a narrow title card or
    one wide banner does not resize every strip; never wider than the
    screen, and the screen width with opts.upscale."""
    if opts.upscale:
        return prof.width
    half, acc = sum(h for _, h in sizes) / 2, 0
    for w, h in sorted(sizes):
        acc += h
        if acc >= half:
            return min(w, prof.width)
    return prof.width                           # no sizes: not reached from convert()


def to_width(strip: Image.Image, width: int, upscale: bool) -> Image.Image:
    """A strip at the column width: a wider one shrunk, a narrower one
    enlarged with upscale, otherwise centred on its border colour."""
    if strip.width > width or (upscale and strip.width < width):
        return strip.resize((width, max(1, round(strip.height * width / strip.width))), LANCZOS)
    if strip.width < width:
        canvas = Image.new(strip.mode, (width, strip.height), border_colour(strip))
        canvas.paste(strip, ((width - strip.width) // 2, 0))
        return canvas
    return strip


def webtoon_pages(strips, prof: Profile, opts: Options, width: int):
    """Join strips into one virtual column `width` wide (see strip_width)
    and cut it into screen-shaped pages.

    A page ends at the lowest blank gap within one screen height (ignoring the
    top eighth). A panel taller than the screen is kept whole up to
    WEBTOON_KEEP screens (the page is shrunk to fit), shown as two
    overlapping screens up to WEBTOON_TWO, and beyond that cut at screen
    height with a WEBTOON_OVERLAP overlap. Blank rows at the top of a page
    are trimmed to a small margin. Only the unsent rows (about two screens)
    joined to the next strip are held in memory.
    """
    buf, top = None, 0
    page_h = round(width * prof.height / prof.width)
    two = round(page_h * WEBTOON_TWO)
    margin, min_gap, overlap = page_h // 60, max(4, page_h // 200), round(page_h * WEBTOON_OVERLAP)

    def rows(a: int, b: int) -> list[bool]:
        return blank_rows(buf.crop((0, top + a, width, min(top + b, buf.height))))

    def page(b: int) -> Image.Image:
        return buf.crop((0, top, width, top + b))

    def lowest_gap(blank, lo, hi):
        y = hi
        while y >= lo:
            if blank[y]:
                start = y
                while start > lo and blank[start - 1]:
                    start -= 1
                if y - start + 1 >= min_gap:
                    return start, y
                y = start
            y -= 1
        return None

    def take(final: bool) -> tuple[Image.Image | None, int]:
        """The next page (or None) and how many rows it used up."""
        h = buf.height - top
        blank = rows(0, page_h + 1)
        lead = next((i for i, b in enumerate(blank) if not b), None)
        if lead is None:                                     # nothing but gap
            return None, (len(blank) - margin if h > len(blank) else h)
        if lead > margin:
            return None, lead - margin
        if h <= page_h:                                      # the last page of the chapter
            last = max(i for i, b in enumerate(blank) if not b)
            return page(min(h, last + 1 + margin)), h
        gap = lowest_gap(blank, page_h // 8, page_h)
        if gap:
            cut = min(gap[1] + 1, gap[0] + margin)
            return page(cut), cut
        below = rows(page_h, two + 1)                        # a tall panel: where does it end?
        end = next((page_h + i for i in range(len(below) - min_gap + 1) if all(below[i:i + min_gap])), None)
        if end is None and final and h <= two:
            end = h
        if end is not None and end <= page_h * WEBTOON_KEEP:
            return page(end), end                            # shrink a little rather than cut
        if end is not None:
            return page(page_h), end - page_h                # two overlapping screens
        return page(page_h), page_h - overlap

    for strip in strips:
        strip = to_width(strip, width, opts.upscale)
        if buf is None:
            buf, top = strip, 0
        else:
            # the unsent rows are copied out first, so the column they were
            # cut from is freed before the joined one is made
            rest, buf = buf.crop((0, top, width, buf.height)), None
            mode = rest.mode if rest.mode == strip.mode else "RGB"
            joined = Image.new(mode, (width, rest.height + strip.height))
            joined.paste(rest if rest.mode == mode else rest.convert(mode), (0, 0))
            joined.paste(strip if strip.mode == mode else strip.convert(mode), (0, rest.height))
            buf, top = joined, 0
            del rest, joined
        strip = None                                         # held once, in buf
        while buf.height - top > two + 1:
            out, used = take(final=False)
            top += used
            if out is not None:
                yield out
    while buf is not None and buf.height - top > 0:
        out, used = take(final=True)
        top += used
        if out is not None:
            yield out


def webtoon_page(img: Image.Image, prof: Profile, opts: Options) -> Page:
    if not (shows_colour(prof, opts) and is_colour(img, prof)):
        img = img.convert("L")
    return finish(img, prof, opts, True)


# ---------------------------------------------------------------- one chapter

def _direction(rtl: bool | None, webtoon: bool, info: dict) -> tuple[bool, str]:
    if webtoon:
        return False, "webtoon"
    if rtl is not None:
        return rtl, "as asked"
    if info.get("Manga") == "YesAndRightToLeft":
        return True, "ComicInfo says right to left"
    return False, "no direction given"


def _mtime(f) -> float:
    try:
        return os.fstat(f.fileno()).st_mtime
    except (OSError, ValueError, AttributeError):
        return 0


def convert(src, out, *, options: Options, rtl: bool | None, webtoon: bool | None, hints: dict, meta: dict,
            threads: int, progress, name: str = "", profile: Profile | None = None) -> Result:
    """Convert the CBZ in the open file src into the open file out (see
    convert_chapter for the arguments). profile overrides options.profile
    (tests use small screens)."""
    started = time.monotonic()
    prof = profile or PROFILES[options.profile]
    fmt = options.output_format
    progress = progress or (lambda done, total: None)
    try:
        z = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, EOFError, ValueError) as e:
        raise ConvertError(f"not a readable zip archive ({e})") from e
    with z:
        names = source.page_names(z)
        if not names:
            raise ConvertError("no pages in the archive")
        info = source.read_comicinfo(z)
        sizes = [image_size(z, n) for n in names] if webtoon is not False else []
        if webtoon is None:
            webtoon, layout_why = detect_layout(sizes, hints)
        else:
            layout_why = "as asked"
        rtl, direction_why = _direction(rtl, webtoon, info)
        decided = (f"{'webtoon' if webtoon else 'paged'} ({layout_why}); "
                   f"{'right to left' if rtl else 'left to right'} ({direction_why})")
        stem = os.path.splitext(name)[0]
        book = book_from(meta, info, stem, rtl, webtoon, _mtime(src))
        writer = WRITERS[fmt](out, book, prof)
        total = len(names)
        pool = ThreadPoolExecutor(threads, thread_name_prefix="mangarr-convert")
        try:
            if webtoon:          # the splitting is sequential; encoding runs ahead in the pool
                def strips():
                    for i, n in enumerate(names):
                        yield load(z, n)
                        progress(i + 1, total)

                pages = ordered(pool, lambda img: [webtoon_page(img, prof, options)],
                                webtoon_pages(strips(), prof, options, strip_width(sizes, prof, options)),
                                2 * threads)
            else:
                grey = not shows_colour(prof, options)
                pages = ordered(pool, lambda i: process_page(load(z, names[i], prof, grey), prof, options, rtl,
                                                             i == 0), range(total), 2 * threads)
            for done, group in enumerate(pages, 1):
                for page in group:
                    writer.add(page)
                if not webtoon:
                    progress(done, total)
            if not writer.pages:
                raise ConvertError("every page is blank")
            writer.close()
        except BaseException:
            writer.abort()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    out.flush()
    size = out.seek(0, os.SEEK_END)
    return Result(path=None, format=fmt, profile=prof.key, pages=len(writer.pages), bytes=size,
                  seconds=round(time.monotonic() - started, 3), rtl=rtl, webtoon=webtoon, decided=decided)
