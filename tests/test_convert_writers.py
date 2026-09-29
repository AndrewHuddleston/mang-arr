"""The converter's output formats, without Pillow: the EPUB 3 structure
readers rely on (mimetype first and stored, pre-paginated, spine direction,
two-page slots, viewports, cover), the CBZ's ComicInfo, the PDF's cross
references, and metadata from anywhere staying valid XML."""
import io
import re
import time
import unittest
import uuid
import xml.etree.ElementTree as ET
import zipfile

from mangarr.convert import writers
from mangarr.convert.profiles import PROFILES
from mangarr.convert.writers import Book, Page, book_from, spread_sides, xml_text

OPF = {"o": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}


def jpeg(n: int) -> bytes:
    return b"\xff\xd8" + bytes([n % 256]) * 50 + b"\xff\xd9"      # only the writers look at it


def pages(sides="....S..") -> list[Page]:
    """One page per letter: '.' a page, 'S' the halves of a split spread
    in right-to-left order, 's' in left-to-right order."""
    out = []
    for c in sides:
        if c in "Ss":
            halves = ("right", "left") if c == "S" else ("left", "right")
            out += [Page(jpeg(len(out) + i), 400, 600, side=side) for i, side in enumerate(halves)]
        else:
            out.append(Page(jpeg(len(out)), 300 + len(out), 600, grey=len(out) != 0))
    return out


def write(fmt: str, book: Book, profile: str = "generic", items=None) -> bytes:
    f = io.BytesIO()
    w = writers.WRITERS[fmt](f, book, PROFILES[profile])
    for p in pages() if items is None else items:
        w.add(p)
    w.close()
    return f.getvalue()


def opf_of(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read("OEBPS/content.opf").decode()


def book(**kw) -> Book:
    base = {"title": "Series - Chapter 12", "series": "Series", "number": "12", "uuid": str(uuid.UUID(int=7)),
            "modified": 1_700_000_000}
    return Book(**{**base, **kw})


class EpubTests(unittest.TestCase):
    def test_structure(self):
        data = write("epub", book(rtl=True))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            first = z.infolist()[0]
            self.assertEqual((first.filename, first.compress_type, first.extra), ("mimetype", zipfile.ZIP_STORED, b""))
            self.assertEqual(z.read("mimetype"), b"application/epub+zip")
            self.assertIsNone(z.testzip())
            container = ET.fromstring(z.read("META-INF/container.xml"))
            self.assertEqual(container[0][0].get("full-path"), "OEBPS/content.opf")
            opf = z.read("OEBPS/content.opf").decode()
            root = ET.fromstring(opf)
            for name in ("nav.xhtml", "toc.ncx"):
                ET.fromstring(z.read(f"OEBPS/{name}"))
            n = len(pages())
            images = [i for i in z.infolist() if i.filename.startswith("OEBPS/Images/")]
            self.assertEqual({i.compress_type for i in images}, {zipfile.ZIP_STORED})
            for i, p in enumerate(pages(), 1):
                xhtml = z.read(f"OEBPS/Text/p{i:04d}.xhtml").decode()
                ET.fromstring(xhtml)
                self.assertIn(f'content="width={p.width}, height={p.height}"', xhtml)
                self.assertIn(f'<img src="../Images/p{i:04d}.jpg" width="{p.width}" height="{p.height}"', xhtml)
                self.assertEqual(z.read(f"OEBPS/Images/p{i:04d}.jpg"), p.data)
        spine = root.find("o:spine", OPF)
        self.assertEqual(spine.get("page-progression-direction"), "rtl")
        self.assertEqual([r.get("idref") for r in spine], [f"p{i:04d}" for i in range(1, n + 1)])
        manifest = {i.get("id"): i for i in root.find("o:manifest", OPF)}
        self.assertEqual(manifest["img0001"].get("properties"), "cover-image")
        self.assertTrue(all(manifest[r.get("idref")].get("href").startswith("Text/") for r in spine))
        props = {m.get("property"): m.text for m in root.iter(f"{{{OPF['o']}}}meta") if m.get("property")}
        self.assertEqual(props["rendition:layout"], "pre-paginated")
        self.assertEqual(props["rendition:spread"], "landscape")
        self.assertEqual(props["dcterms:modified"], "2023-11-14T22:13:20Z")
        self.assertEqual(props["belongs-to-collection"], "Series")
        self.assertEqual(props["group-position"], "12")
        self.assertIn('<meta name="calibre:series_index" content="12"/>', opf)
        self.assertEqual(root.find("o:metadata/dc:identifier", OPF).text, f"urn:uuid:{uuid.UUID(int=7)}")
        self.assertNotIn("<script", opf)
        self.assertNotIn("http://", opf.replace("http://www.idpf.org", "").replace("http://purl.org", ""))

    def test_spread_slots(self):
        slots = re.findall(r'properties="page-spread-(\w+)"', opf_of(write("epub", book(rtl=True))))
        #  pages: . . . . R L . .  -> the page before the spread closes a pair
        self.assertEqual(slots, ["right", "left", "right", "left", "right", "left", "right", "left"])
        kobo = opf_of(write("kepub", book(rtl=False), profile="kobo-libra"))
        self.assertIn('properties="rendition:page-spread-left"', kobo)
        self.assertNotIn('properties="page-spread-', kobo)
        self.assertIn('page-progression-direction="ltr"', kobo)

    def test_spread_sides(self):
        def slots(pattern, rtl):
            return "".join(s[0] for s in spread_sides(pages(pattern), rtl))
        self.assertEqual(slots("....", False), "lrlr")
        self.assertEqual(slots("....", True), "rlrl")
        self.assertEqual(slots("...S", True), "lrlrl")        # re-paired backwards: the halves keep r, l
        self.assertEqual(slots("..S..S.", True), "rlrlrlrlr")
        self.assertEqual(slots("...s", False), "rlrlr")
        self.assertEqual(slots("s.", False), "lrl")

    def test_webtoon_has_no_spreads(self):
        data = opf_of(write("epub", book(webtoon=True)))
        self.assertIn('<meta property="rendition:spread">none</meta>', data)
        self.assertNotIn("page-spread", data)

    def test_same_input_same_bytes(self):
        self.assertEqual(write("epub", book()), write("epub", book()))
        self.assertEqual(write("pdf", book()), write("pdf", book()))
        self.assertNotEqual(write("epub", book()), write("epub", book(modified=1_700_000_001)))
        with zipfile.ZipFile(io.BytesIO(write("cbz", book(modified=0)))) as z:
            self.assertEqual({i.date_time for i in z.infolist()}, {(1980, 1, 1, 0, 0, 0)})

    def test_hostile_metadata_stays_valid_xml(self):
        evil = "A\x00B\x1b ]]> & <b>x</b></dc:title><script>alert(1)</script> \ud800 ￾" + "t" * 10_000
        b = book_from({"series": evil, "number": 1, "chapter": evil, "authors": [evil, 5, None], "description": evil,
                       "language": '"><x', "uuid": "not-a-uuid"}, {}, "c", rtl=False, webtoon=False)
        self.assertLessEqual(len(b.title), 2 * writers.MAX_TITLE + 20)
        self.assertEqual(b.language, "en")
        self.assertEqual(b.authors, (writers.clean(evil),))
        for fmt in ("epub", "cbz"):
            with zipfile.ZipFile(io.BytesIO(write(fmt, b))) as z:
                for name in z.namelist():
                    if name.endswith((".xml", ".opf", ".xhtml", ".ncx")):
                        root = ET.fromstring(z.read(name))
                        self.assertEqual([e for e in root.iter() if str(e.tag).endswith("script")], [])
                        self.assertEqual([e for e in root.iter() if str(e.tag).endswith("}b") or e.tag == "b"], [])
                if fmt == "epub":
                    title = ET.fromstring(z.read("OEBPS/content.opf")).find("o:metadata/dc:title", OPF).text
                    self.assertTrue(title.startswith("AB ]]> & <b>x</b></dc:title><script>"))
        pdf = write("pdf", b)
        title = re.search(rb"/Title <FEFF([0-9A-F]*)>", pdf)[1]
        self.assertTrue(bytes.fromhex(title.decode()).decode("utf-16-be").startswith("AB ]]>"))


class CbzTests(unittest.TestCase):
    def test_pages_and_comicinfo(self):
        b = book_from({"series": "S & T", "number": 12.0, "chapter": "Go", "authors": ["A", "B"],
                       "description": "Plain."},
                      {"Volume": "3", "Genre": "Action", "Manga": "YesAndRightToLeft", "Series": "ignored"},
                      "Chapter 012.0", rtl=True, webtoon=False)
        with zipfile.ZipFile(io.BytesIO(write("cbz", b))) as z:
            names = z.namelist()
            self.assertEqual(names[:-1], [f"{i:04d}.jpg" for i in range(1, len(pages()) + 1)])
            info = ET.fromstring(z.read("ComicInfo.xml"))
        fields = {e.tag: e.text for e in info}
        self.assertEqual(fields, {"Series": "S & T", "Number": "12", "Title": "Go", "Writer": "A, B",
                                  "Summary": "Plain.", "LanguageISO": "en", "Volume": "3", "Genre": "Action",
                                  "PageCount": str(len(pages())), "Manga": "YesAndRightToLeft"})

    def test_manga_field_follows_the_copy(self):
        def manga(source, rtl):
            b = book_from({}, {"Manga": source} if source else {}, "c", rtl=rtl, webtoon=not rtl)
            with zipfile.ZipFile(io.BytesIO(write("cbz", b))) as z:
                return ET.fromstring(z.read("ComicInfo.xml")).findtext("Manga")
        self.assertEqual(manga("YesAndRightToLeft", True), "YesAndRightToLeft")
        self.assertEqual(manga("YesAndRightToLeft", False), "Yes")   # a webtoon copy reads left to right
        self.assertEqual(manga("No", False), "No")
        self.assertIsNone(manga("", False))
        self.assertIsNone(manga("Unknown", False))


class PdfTests(unittest.TestCase):
    def test_cross_references_and_direction(self):
        items = pages()
        data = write("pdf", book(rtl=True, authors=("Ann",)), items=items)
        self.assertTrue(data.startswith(b"%PDF-1.4\n"))
        self.assertTrue(data.endswith(b"%%EOF\n"))
        at = int(re.search(rb"startxref\n(\d+)\n", data)[1])
        self.assertEqual(data[at:at + 4], b"xref")
        count = int(re.search(rb"xref\n0 (\d+)\n", data)[1])
        entries = re.findall(rb"(\d{10}) 00000 n \n", data[at:])
        self.assertEqual(len(entries), count - 1)
        for num, offset in enumerate(entries, 1):
            self.assertTrue(data[int(offset):].startswith(b"%d 0 obj\n" % num), num)
        self.assertIn(b"/Count %d" % len(items), data)
        self.assertIn(b"/ViewerPreferences << /Direction /R2L >>", data)
        self.assertEqual(data.count(b"/ColorSpace /DeviceRGB"), 1)          # the colour first page
        self.assertEqual(data.count(b"/ColorSpace /DeviceGray"), len(items) - 1)
        for p in items:
            self.assertIn(b"stream\n" + p.data + b"\nendstream", data)
        self.assertIn(b"/Author <FEFF" + "Ann".encode("utf-16-be").hex().upper().encode() + b">", data)
        self.assertIn(b"/Direction /L2R", write("pdf", book(rtl=False)))


class BookTests(unittest.TestCase):
    def test_meta_then_comicinfo_then_file_name(self):
        info = {"Series": "Info Series", "Number": "7", "Title": "Info Title", "Writer": "W", "Penciller": "W",
                "Summary": "Info summary", "LanguageISO": "ja"}
        b = book_from({}, info, "Chapter 007.0", rtl=False, webtoon=False, modified=1234.9)
        self.assertEqual((b.title, b.series, b.number, b.chapter), ("Info Series - Chapter 7: Info Title",
                                                                    "Info Series", "7", "Info Title"))
        self.assertEqual((b.authors, b.description, b.language, b.modified), (("W",), "Info summary", "ja", 1234))
        self.assertEqual(b.uuid, str(uuid.uuid5(uuid.NAMESPACE_URL, "mang-arr:Info Series:7")))
        b = book_from({"series": "Meta", "number": 7.5, "title": "Whole Title", "modified": 99, "language": "en-GB"},
                      info, "x", rtl=False, webtoon=False, modified=5)
        self.assertEqual((b.title, b.series, b.number, b.modified, b.language), ("Whole Title", "Meta", "7.5", 99,
                                                                                 "en-GB"))
        b = book_from({}, {}, "Chapter 012.0", rtl=False, webtoon=False)
        self.assertEqual((b.title, b.series, b.number, b.language), ("Chapter 012.0", "", "", "en"))
        b = book_from({"series": "S"}, {"Title": "Chapter 12"}, "Chapter 012.0", rtl=False, webtoon=False)
        self.assertEqual(b.title, "S - Chapter 012.0: Chapter 12")
        b = book_from({"series": "S", "number": 12}, {"Title": "Chapter 12"}, "x", rtl=False, webtoon=False)
        self.assertEqual(b.title, "S - Chapter 12")            # the name adds nothing
        for bad in (float("nan"), True, "x", 10 ** 30, -5):
            b = book_from({"modified": bad}, {}, "x", rtl=False, webtoon=False, modified=7)
            self.assertIn(b.modified, (7, 0, writers.MAX_MODIFIED))
            time.gmtime(b.modified)

    def test_xml_text(self):
        self.assertEqual(xml_text('a<b>&"c\'\x00\x08\x0b\ud83d'), "a&lt;b&gt;&amp;&quot;c&#x27;")
        self.assertEqual(xml_text("tab\tnew\nline"), "tab\tnew\nline")
        self.assertEqual(xml_text(None), "")
        self.assertEqual(xml_text("🙂 ü"), "🙂 ü")


if __name__ == "__main__":
    unittest.main()
