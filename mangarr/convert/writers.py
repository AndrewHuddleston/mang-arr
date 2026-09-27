"""The output formats: fixed-layout EPUB 3 (also Kobo's KEPUB, which is the
same file under a .kepub.epub name), CBZ and PDF. Standard library only, so
the formats can be tested without Pillow.

A writer is handed an open binary file and the finished pages one at a
time; it keeps only each page's size, so memory does not grow with the
chapter. close() completes the container and leaves the file open: where
it is written, synced and renamed is the caller's business. Every
metadata string goes through xml_text() (or UTF-16 for the PDF title), and
the output depends only on the pages and the Book, so the same input gives
the same bytes.
"""
import html
import math
import re
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass

from .profiles import Profile

MAX_TITLE = 500                 # characters of a title, series or author name kept
MAX_DESCRIPTION = 10_000
MAX_AUTHORS = 20

# Characters XML 1.0 does not allow at all (C0 controls except tab and line
# breaks, lone surrogates, U+FFFE/U+FFFF); escaping cannot make them valid.
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8}){0,3}")


def clean(s, limit: int = MAX_TITLE) -> str:
    """s as a one-line-safe string for metadata: characters XML cannot hold
    removed, cut to limit characters. Anything that is not a string is ''."""
    if not isinstance(s, str):
        return ""
    return _NOT_XML.sub("", s[:limit]).strip()


def xml_text(s) -> str:
    """s for XML text or an attribute value: clean(), then escaped."""
    return html.escape(clean(s, MAX_DESCRIPTION), quote=True)


@dataclass(frozen=True)
class Page:
    data: bytes                 # a baseline JPEG
    width: int
    height: int
    grey: bool = True
    side: str | None = None     # "left" | "right" for the halves of a split spread


@dataclass(frozen=True)
class Book:
    title: str
    series: str = ""
    number: str = ""            # as shown: "12", "12.5"
    chapter: str = ""           # the chapter's own name, if it has one
    authors: tuple[str, ...] = ()
    description: str = ""       # plain text
    language: str = "en"
    uuid: str = ""
    modified: int = 0           # unix time: dcterms:modified and every zip entry's date
    rtl: bool = False
    webtoon: bool = False
    comicinfo: tuple[tuple[str, str], ...] = ()   # other ComicInfo.xml fields a CBZ keeps


# ComicInfo.xml fields carried over into a converted CBZ; the ones the
# Book has (Series, Number, Title, Writer, Summary, LanguageISO) come from it.
KEPT_COMICINFO = ("Volume", "Penciller", "Inker", "Colorist", "Translator", "Publisher", "Genre", "Tags",
                  "Web", "Year", "Month", "Day", "AgeRating", "Manga")
MAX_MODIFIED = 4354819199       # the end of 2107, the last date a zip entry can carry


def _number(v) -> str:
    if isinstance(v, bool):
        return ""
    if isinstance(v, (int, float)):
        return f"{v:g}" if math.isfinite(v) else ""
    return clean(v, 20)


def book_from(meta: dict, comicinfo: dict[str, str], name: str, rtl: bool, webtoon: bool,
              modified: float = 0) -> Book:
    """The Book for a chapter: what the caller knows (meta) first, then the
    archive's ComicInfo.xml, then the file name.

    meta keys, all optional: series, number, chapter (the chapter's own
    name), title (the whole book title; default "<series> - Chapter <number>:
    <chapter>"), authors (list), description (plain text: HTML is shown as
    text, never as markup), language, uuid, modified (unix time; default the
    source file's). The uuid defaults to a uuid5 of series and number, so a
    re-conversion keeps the book's identity on the reader."""
    info = comicinfo or {}
    series = clean(meta.get("series")) or clean(info.get("Series"))
    number = _number(meta.get("number")) or clean(info.get("Number"), 20)
    chapter = clean(meta.get("chapter")) or clean(info.get("Title"))
    name = clean(name)
    title = clean(meta.get("title"))
    if not title:
        if series and number:
            title = f"{series} - Chapter {number}"
        elif series and name:
            title = f"{series} - {name}"
        else:
            title = name or series or "Chapter"
        if chapter and chapter.casefold() not in title.casefold():
            title += f": {chapter}"
    authors = meta.get("authors")
    if isinstance(authors, (list, tuple)):
        authors = [clean(a) for a in authors]
    else:
        authors = [clean(info.get("Writer")), clean(info.get("Penciller"))]
    authors = tuple(dict.fromkeys(a for a in authors if a))[:MAX_AUTHORS]
    description = clean(meta.get("description"), MAX_DESCRIPTION) or clean(info.get("Summary"), MAX_DESCRIPTION)
    language = clean(meta.get("language"), 30) or clean(info.get("LanguageISO"), 30)
    if not _LANGUAGE.fullmatch(language):
        language = "en"
    ident = meta.get("uuid")
    try:
        ident = str(uuid.UUID(str(ident))) if ident else ""
    except ValueError:
        ident = ""
    ident = ident or str(uuid.uuid5(uuid.NAMESPACE_URL, f"mang-arr:{series or name}:{number}"))
    when = meta.get("modified", modified)
    if isinstance(when, bool) or not isinstance(when, (int, float)) or not math.isfinite(when):
        when = modified
    kept = tuple((k, clean(info[k], MAX_DESCRIPTION)) for k in KEPT_COMICINFO if clean(info.get(k)))
    return Book(title=title, series=series, number=number, chapter=chapter, authors=authors,
                description=description, language=language, uuid=ident, modified=min(max(int(when), 0), MAX_MODIFIED),
                rtl=rtl, webtoon=webtoon, comicinfo=kept)


def _series_index(number: str) -> str:
    try:
        v = float(number)
    except ValueError:
        return ""
    return f"{v:g}" if math.isfinite(v) else ""


def _zip_time(t: int) -> tuple:
    return time.gmtime(min(max(t, 315532800), MAX_MODIFIED))[:6]     # zip dates start in 1980


def _zinfo(name: str, when: tuple, compress: bool) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=when)
    zi.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    zi.create_system = 3                        # the same bytes on every platform
    zi.external_attr = 0o644 << 16
    return zi


class Writer:
    def __init__(self, f, book: Book, profile: Profile):
        self.f, self.book, self.profile = f, book, profile
        self.pages: list[Page] = []             # sizes and sides only; the data is already written
        self.when = _zip_time(book.modified)

    def add(self, page: Page) -> None:
        self.pages.append(Page(b"", page.width, page.height, page.grey, page.side))

    def close(self) -> None:
        pass

    def abort(self) -> None:
        """After a failure: release the zip writer without raising (its
        __del__ would otherwise write to the file later). The file itself
        is the caller's to delete."""
        z = getattr(self, "z", None)
        if z is not None:
            try:
                z.close()
            except Exception:
                pass


class CbzWriter(Writer):
    """Stored JPEGs 0001.jpg ... and a ComicInfo.xml describing the copy."""

    def __init__(self, *a):
        super().__init__(*a)
        self.z = zipfile.ZipFile(self.f, "w")

    def add(self, page: Page) -> None:
        super().add(page)
        self.z.writestr(_zinfo(f"{len(self.pages):04d}.jpg", self.when, False), page.data)

    def close(self) -> None:
        b = self.book
        kept = dict(b.comicinfo)
        # the direction of this copy; a source that said right to left is
        # still manga when the copy reads left to right (a webtoon)
        manga = kept.pop("Manga", "")
        manga = "YesAndRightToLeft" if b.rtl else "Yes" if manga.startswith("Yes") else "No" if manga == "No" else ""
        info = ET.Element("ComicInfo")
        fields = [("Series", b.series), ("Number", b.number), ("Title", b.chapter), ("Writer", ", ".join(b.authors)),
                  ("Summary", b.description), ("LanguageISO", b.language), *kept.items(),
                  ("PageCount", str(len(self.pages))), ("Manga", manga)]
        for tag, value in fields:
            if value:
                ET.SubElement(info, tag).text = value
        self.z.writestr(_zinfo("ComicInfo.xml", self.when, True),
                        ET.tostring(info, encoding="utf-8", xml_declaration=True))
        self.z.close()


def spread_sides(pages: list[Page], rtl: bool) -> list[str]:
    """Left/right slot of every page for two-page (landscape) display.

    Pages alternate, starting on the reading side; the halves of a split
    spread keep their own sides, so the pages before one are re-paired
    backwards and the page just before it closes a pair (KCC's rule, see
    CREDITS.md).
    """
    first, second = ("right", "left") if rtl else ("left", "right")
    sides, nxt = [], first
    for p in pages:
        if p.side:
            sides.append(p.side)
            nxt = first
        else:
            sides.append(nxt)
            nxt = second if nxt == first else first
    seen, nxt = False, second
    for i in range(len(pages) - 1, -1, -1):
        if pages[i].side:
            seen, nxt = True, second
        elif seen:
            sides[i] = nxt
            nxt = first if nxt == second else second
    return sides


_XHTML_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
               '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n')


class EpubWriter(Writer):
    """Fixed-layout EPUB 3: one XHTML page per image with the viewport at the
    image's size, the first page as the cover, and the reading direction on
    the spine. No scripts, no remote resources, one fixed stylesheet."""

    CSS = "@page{margin:0}html,body{margin:0;padding:0}img{display:block;margin:0 auto}\n"

    def __init__(self, *a):
        super().__init__(*a)
        self.z = zipfile.ZipFile(self.f, "w")
        # the mimetype comes first and uncompressed, so readers can sniff it
        self.z.writestr(_zinfo("mimetype", self.when, False), "application/epub+zip")
        self.z.writestr(_zinfo("META-INF/container.xml", self.when, True),
                        '<?xml version="1.0" encoding="UTF-8"?>\n<container version="1.0" '
                        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                        '</rootfiles></container>\n')
        self.z.writestr(_zinfo("OEBPS/style.css", self.when, True), self.CSS)

    def add(self, page: Page) -> None:
        super().add(page)
        n = len(self.pages)
        self.z.writestr(_zinfo(f"OEBPS/Images/p{n:04d}.jpg", self.when, False), page.data)
        self.z.writestr(_zinfo(f"OEBPS/Text/p{n:04d}.xhtml", self.when, True), (
            f'{_XHTML_HEAD}<head><title>Page {n}</title><meta name="viewport" content="width={page.width}, '
            f'height={page.height}"/><link rel="stylesheet" type="text/css" href="../style.css"/></head>\n'
            f'<body><img src="../Images/p{n:04d}.jpg" width="{page.width}" height="{page.height}" '
            f'alt="Page {n}"/></body>\n</html>\n'))

    def close(self) -> None:
        b, esc = self.book, xml_text
        t, lang, uid = esc(b.title), esc(b.language), f"urn:uuid:{b.uuid}"
        nav = (f'{_XHTML_HEAD}<head><title>{t}</title></head><body><nav epub:type="toc" id="toc"><ol>'
               f'<li><a href="Text/p0001.xhtml">{t}</a></li></ol></nav></body></html>\n')
        ncx = ('<?xml version="1.0" encoding="UTF-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" '
               f'version="2005-1"><head><meta name="dtb:uid" content="{uid}"/></head>'
               f'<docTitle><text>{t}</text></docTitle><navMap><navPoint id="n1" playOrder="1">'
               f'<navLabel><text>{t}</text></navLabel><content src="Text/p0001.xhtml"/></navPoint>'
               '</navMap></ncx>\n')
        modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(b.modified, 0)))
        meta = [f'<dc:identifier id="uid">{uid}</dc:identifier>',
                f"<dc:title>{t}</dc:title>", f"<dc:language>{lang}</dc:language>",
                f'<meta property="dcterms:modified">{modified}</meta>',
                '<meta property="rendition:layout">pre-paginated</meta>',
                '<meta property="rendition:orientation">auto</meta>',
                f'<meta property="rendition:spread">{"none" if b.webtoon else "landscape"}</meta>',
                '<meta name="cover" content="img0001"/>']
        meta += [f"<dc:creator>{esc(a)}</dc:creator>" for a in b.authors]
        if b.description:
            meta.append(f"<dc:description>{esc(b.description)}</dc:description>")
        if b.series:
            # EPUB 3 collections, and calibre's names for the same (KOReader, calibre, Kobo read these)
            meta += [f'<meta property="belongs-to-collection" id="series">{esc(b.series)}</meta>',
                     '<meta refines="#series" property="collection-type">series</meta>',
                     f'<meta name="calibre:series" content="{esc(b.series)}"/>']
            index = _series_index(b.number)
            if index:
                meta += [f'<meta refines="#series" property="group-position">{index}</meta>',
                         f'<meta name="calibre:series_index" content="{index}"/>']
        items = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                 '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                 '<item id="css" href="style.css" media-type="text/css"/>']
        spine = []
        prefix = "rendition:" if self.profile.vendor == "kobo" else ""
        sides = [None] * len(self.pages) if b.webtoon else spread_sides(self.pages, b.rtl)
        for n, side in enumerate(sides, 1):
            cover = ' properties="cover-image"' if n == 1 else ""
            items += [f'<item id="p{n:04d}" href="Text/p{n:04d}.xhtml" media-type="application/xhtml+xml"/>',
                      f'<item id="img{n:04d}" href="Images/p{n:04d}.jpg" media-type="image/jpeg"{cover}/>']
            prop = f' properties="{prefix}page-spread-{side}"' if side else ""
            spine.append(f'<itemref idref="p{n:04d}"{prop}/>')
        nl = "\n"
        opf = ('<?xml version="1.0" encoding="UTF-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" '
               f'version="3.0" unique-identifier="uid" xml:lang="{lang}">'
               f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n{nl.join(meta)}\n</metadata>\n'
               f'<manifest>\n{nl.join(items)}\n</manifest>\n'
               f'<spine toc="ncx" page-progression-direction="{"rtl" if b.rtl else "ltr"}">\n'
               f'{nl.join(spine)}\n</spine>\n</package>\n')
        for name, body in (("nav.xhtml", nav), ("toc.ncx", ncx), ("content.opf", opf)):
            self.z.writestr(_zinfo(f"OEBPS/{name}", self.when, True), body)
        self.z.close()


def _pdf_text(s: str) -> bytes:
    # a PDF text string in UTF-16BE with its byte order mark, as hex: nothing in it needs escaping
    return b"<FEFF" + clean(s).encode("utf-16-be").hex().upper().encode() + b">"


class PdfWriter(Writer):
    """Minimal PDF 1.4 that embeds each JPEG as it is (DCTDecode): no second
    lossy encode, and written as it goes. One page per image at 1 px = 1 pt,
    with the reading direction as a viewer preference."""

    def __init__(self, *a):
        super().__init__(*a)
        self.offsets: dict[int, int] = {}
        self.kids: list[int] = []
        self.f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    def obj(self, num: int, body: bytes, stream: bytes | None = None) -> None:
        self.offsets[num] = self.f.tell()
        self.f.write(b"%d 0 obj\n" % num + body)
        if stream is not None:
            self.f.write(b"\nstream\n" + stream + b"\nendstream")
        self.f.write(b"\nendobj\n")

    def add(self, page: Page) -> None:
        super().add(page)
        base = 3 + 3 * (len(self.pages) - 1)     # 1 catalog, 2 page tree, then page, content, image
        w, h = page.width, page.height
        draw = b"q %d 0 0 %d 0 0 cm /Im0 Do Q" % (w, h)
        self.obj(base + 2, b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /%s "
                 b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>"
                 % (w, h, b"DeviceGray" if page.grey else b"DeviceRGB", len(page.data)), page.data)
        self.obj(base + 1, b"<< /Length %d >>" % len(draw), draw)
        self.obj(base, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
                 b"/Resources << /XObject << /Im0 %d 0 R >> >> >>" % (w, h, base + 1, base + 2))
        self.kids.append(base)

    def close(self) -> None:
        b = self.book
        info = 3 + 3 * len(self.pages)
        self.obj(2, b"<< /Type /Pages /Count %d /Kids [%s] >>"
                 % (len(self.kids), b" ".join(b"%d 0 R" % k for k in self.kids)))
        self.obj(1, b"<< /Type /Catalog /Pages 2 0 R /ViewerPreferences << /Direction /%s >> >>"
                 % (b"R2L" if b.rtl else b"L2R"))
        fields = b"/Title %s" % _pdf_text(b.title)
        if b.authors:
            fields += b" /Author %s" % _pdf_text(", ".join(b.authors))
        self.obj(info, b"<< %s /Producer (mang-arr) >>" % fields)
        xref = self.f.tell()
        self.f.write(b"xref\n0 %d\n0000000000 65535 f \n" % (info + 1))
        for n in range(1, info + 1):
            self.f.write(b"%010d 00000 n \n" % self.offsets[n])
        self.f.write(b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                     % (info + 1, info, xref))


WRITERS = {"epub": EpubWriter, "kepub": EpubWriter, "cbz": CbzWriter, "pdf": PdfWriter}
