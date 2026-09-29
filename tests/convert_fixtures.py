"""Chapters generated for the converter tests (need Pillow): a small manga
chapter with a colour cover, margins, a page number and one double page; a
webtoon of two strips with white gaps, a black gutter and a panel several
screens tall; and hostile archives. Everything comes from a fixed seed, so
a failure reproduces."""
import io
import random
import struct
import zipfile
import zlib

from PIL import Image, ImageDraw

from mangarr.convert.profiles import Profile

# Small screens, so every generated page is shrunk and the tests stay fast.
GREY = Profile("test-grey", "Test grey", 300, 420, colour=False, eink=True, vendor="",
               default_format="epub", formats=("epub", "cbz", "pdf"))
COLOUR = Profile("test-colour", "Test colour", 300, 420, colour=True, eink=True, vendor="kobo",
                 default_format="kepub", formats=("kepub", "epub", "cbz", "pdf"))
WIDE = Profile("test-wide", "Test wide", 480, 640, colour=True, eink=True, vendor="",
               default_format="epub", formats=("epub", "cbz", "pdf"))
PROFILES = {p.key: p for p in (GREY, COLOUR, WIDE)}

SPREAD = 5          # the double page of the manga chapter (1-based)
PAGES = 8
MARGIN = 30

COMICINFO = """<?xml version="1.0" encoding="utf-8"?>
<ComicInfo>
  <Series>{series}</Series>
  <Number>{number}</Number>
  <Title>{title}</Title>
  <Writer>Test Mangaka</Writer>
  <LanguageISO>en</LanguageISO>
  <Manga>{manga}</Manga>
</ComicInfo>
"""


def encode(img: Image.Image, fmt: str, **kw) -> bytes:
    b = io.BytesIO()
    img.save(b, {"jpg": "JPEG", "png": "PNG", "webp": "WEBP", "gif": "GIF"}[fmt], **kw)
    return b.getvalue()


def panel(draw: ImageDraw.ImageDraw, box, rnd: random.Random, colour: bool = False) -> None:
    x0, y0, x1, y1 = box
    draw.rectangle(box, outline=0, width=4)
    for _ in range(rnd.randint(6, 12)):
        a = (rnd.randint(x0, x1), rnd.randint(y0, y1))
        b = (rnd.randint(x0, x1), rnd.randint(y0, y1))
        fill = (rnd.randint(0, 255), rnd.randint(0, 120), rnd.randint(0, 255)) if colour else 0
        draw.line([a, b], fill=fill, width=rnd.randint(2, 4))


def manga_page(w: int, h: int, n: int, rnd: random.Random, colour: bool = False) -> Image.Image:
    """Two rows of panels inside a uniform white margin, the page number
    alone in the bottom margin. A double page gets a solid black block on
    its left half only, so the tests can tell the halves apart."""
    img = Image.new("RGB" if colour else "L", (w, h), (255, 255, 255) if colour else 255)
    d = ImageDraw.Draw(img)
    inner = h - 2 * MARGIN
    for row in range(2):
        y = MARGIN + row * inner // 2
        panel(d, (MARGIN, y + 4, w - MARGIN, y + inner // 2 - 4), rnd, colour)
    if w > h:
        d.rectangle((MARGIN + 20, MARGIN + 20, w // 2 - 40, h - MARGIN - 20), fill=0)
    d.text((w // 2 - 4, h - MARGIN // 2 - 6), str(n), fill=0)
    return img


def manga_cbz(path: str, comicinfo: str | None = None, spread_width: int = 1200) -> str:
    """8 pages of 600x900 (page 1 a colour cover, page SPREAD a double page),
    JPEG/PNG/WebP mixed, stored in reverse order, with a ComicInfo.xml
    saying right to left."""
    rnd = random.Random(12)
    fmts = ("jpg", "png", "webp")
    entries = []
    for n in range(1, PAGES + 1):
        img = manga_page(spread_width if n == SPREAD else 600, 900, n, rnd, colour=n == 1)
        fmt = fmts[n % 3]
        entries.append((f"{n:03d}.{fmt}", encode(img, fmt, **({"quality": 90} if fmt != "png" else {}))))
    if comicinfo is None:
        comicinfo = COMICINFO.format(series="Test Manga &amp; Friends", number="12", title="The Spread",
                                     manga="YesAndRightToLeft")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for name, data in reversed(entries):
            z.writestr(name, data)
        z.writestr("ComicInfo.xml", comicinfo)
    return path


def webtoon_strips(width: int = 400, height: int = 6000, count: int = 2) -> list[Image.Image]:
    """Colour strips of framed panels 300-700 px tall with 60-200 px white
    gaps, one black gutter and one 1500 px panel; panels cross the joins."""
    rnd = random.Random(3)
    total = Image.new("RGB", (width, height * count), (255, 255, 255))
    d = ImageDraw.Draw(total)
    y, k = 80, 0
    while y < height * count - 400:
        ph = min(1500 if k == 4 else rnd.randint(300, 700), height * count - 100 - y)
        panel(d, (20, y, width - 20, y + ph), rnd, colour=True)
        y += ph
        gap = rnd.randint(60, 200)
        if k == 7:
            d.rectangle((0, y, width, y + gap), fill=(0, 0, 0))
        y += gap
        k += 1
    return [total.crop((0, i * height, width, (i + 1) * height)) for i in range(count)]


def webtoon_cbz(path: str, strips: list[Image.Image] | None = None) -> str:
    strips = webtoon_strips() if strips is None else strips
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for i, strip in enumerate(strips, 1):
            fmt = "png" if strip.mode == "L" and i % 2 else "jpg"
            z.writestr(f"{i}.{fmt}", encode(strip, fmt))
        z.writestr("ComicInfo.xml", COMICINFO.format(series="Test Webtoon", number="3", title="Strips", manga="Yes"))
    return path


def pages_cbz(path: str, images: list[tuple[str, bytes]], comicinfo: str | None = None) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for name, data in images:
            z.writestr(name, data)
        if comicinfo is not None:
            z.writestr("ComicInfo.xml", comicinfo)
    return path


def png_header(width: int, height: int) -> bytes:
    """A PNG that claims width x height but holds a few bytes: a
    decompression bomb's header, without the memory to make a real one."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 64))
            + chunk(b"IEND", b""))


def with_method(data: bytes, name: str, method: int) -> bytes:
    """A zip whose entry `name` claims another compression method (9 is
    deflate64), patched in both its local and central headers."""
    buf = bytearray(data)
    for sig, at in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
        start = 0
        while (i := buf.find(sig, start)) >= 0:
            header = 30 if sig == b"PK\x03\x04" else 46
            name_len = struct.unpack_from("<H", buf, i + (26 if header == 30 else 28))[0]
            if bytes(buf[i + header:i + header + name_len]) == name.encode():
                struct.pack_into("<H", buf, i + at, method)
            start = i + 4
    return bytes(buf)
