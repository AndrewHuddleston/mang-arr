"""The converter end to end on generated chapters: page order, reading
direction, spreads, crop, fit, colour and tone, webtoon strips, the
metadata that reaches the book, byte-identical output, and hostile pages
that must fail with the page's name and leave nothing behind. Needs Pillow
(the convert extra); skipped without it."""
import hashlib
import io
import os
import re
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from itertools import pairwise
from unittest import mock

try:
    import convert_fixtures as fx
    from PIL import Image, ImageChops, ImageFile, JpegImagePlugin, PngImagePlugin

    from mangarr.convert import engine
except ImportError:             # the convert extra is not installed
    Image = None

from mangarr.convert import ConvertError, Options, convert_chapter, profiles

OPF_NS = {"o": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}


def jpegs(path: str) -> list:
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if n.endswith(".jpg"))
        return [Image.open(io.BytesIO(z.read(n))) for n in names]


def darkness(img) -> float:
    g = img.convert("L")
    return 255 - sum(i * v for i, v in enumerate(g.histogram())) / (g.width * g.height)


def sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        if Image is None:
            self.skipTest("Pillow is not installed (the convert extra)")
        patcher = mock.patch.dict(profiles.PROFILES, fx.PROFILES)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory(prefix="mangarr-convert-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.out = os.path.join(self.tmp, "out")
        os.mkdir(self.out)

    def convert(self, src, name, **kw):
        opts = {k: kw.pop(k) for k in list(kw) if k in Options.__dataclass_fields__}
        return convert_chapter(src, os.path.join(self.out, name), Options(**opts), **kw)


@unittest.skipIf(Image is None, "Pillow is not installed (the convert extra)")
class MangaTests(Base):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory(prefix="mangarr-convert-")
        cls.manga = fx.manga_cbz(os.path.join(cls.dir.name, "Chapter 012.0.cbz"))

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_right_to_left_splits_the_spread_right_half_first(self):
        r = self.convert(self.manga, "rtl.cbz", profile="test-grey", format="cbz")
        pages = jpegs(r.path)
        self.assertEqual((r.pages, len(pages)), (fx.PAGES + 1, fx.PAGES + 1))
        self.assertTrue(r.rtl)
        self.assertIn("ComicInfo says right to left", r.decided)
        right, left = pages[fx.SPREAD - 1], pages[fx.SPREAD]
        self.assertLess(darkness(right), darkness(left))       # the black block is on the left half
        self.assertTrue(all(p.width <= 300 and p.height <= 420 for p in pages))
        self.assertTrue(all(p.mode == "L" and p.format == "JPEG" for p in pages))
        with zipfile.ZipFile(r.path) as z:
            info = ET.fromstring(z.read("ComicInfo.xml"))
            self.assertEqual([i.compress_type for i in z.infolist() if i.filename.endswith(".jpg")],
                             [zipfile.ZIP_STORED] * r.pages)
        self.assertEqual(info.findtext("PageCount"), str(fx.PAGES + 1))
        self.assertEqual(info.findtext("Manga"), "YesAndRightToLeft")
        self.assertEqual(info.findtext("Series"), "Test Manga & Friends")
        self.assertEqual(info.findtext("Number"), "12")
        self.assertEqual(info.findtext("Title"), "The Spread")
        # margins and the lone page number cropped: narrower than the source's 600x900
        self.assertLess(pages[1].width / pages[1].height, 600 / 900 - 0.01)

    def test_left_to_right_splits_the_spread_left_half_first(self):
        r = self.convert(self.manga, "ltr.cbz", profile="test-grey", format="cbz", rtl=False)
        pages = jpegs(r.path)
        self.assertFalse(r.rtl)
        self.assertIn("left to right (as asked)", r.decided)
        self.assertGreater(darkness(pages[fx.SPREAD - 1]), darkness(pages[fx.SPREAD]))

    def test_spread_modes(self):
        for mode, count in (("rotate", fx.PAGES), ("both", fx.PAGES + 2), ("keep", fx.PAGES)):
            with self.subTest(mode):
                r = self.convert(self.manga, f"{mode}.cbz", profile="test-grey", format="cbz", spreads=mode)
                pages = jpegs(r.path)
                self.assertEqual(r.pages, count)
                if mode == "rotate":
                    self.assertGreater(pages[fx.SPREAD - 1].height, pages[fx.SPREAD - 1].width)
                elif mode == "keep":
                    self.assertGreater(pages[fx.SPREAD - 1].width, pages[fx.SPREAD - 1].height)
                else:                                          # both halves, then the whole page turned
                    self.assertGreater(pages[fx.SPREAD + 1].height, pages[fx.SPREAD + 1].width)

    def test_a_panorama_is_rotated_not_split(self):
        page = fx.manga_page(1800, 900, 1, fx.random.Random(1))
        wide = fx.pages_cbz(os.path.join(self.tmp, "wide.cbz"), [("001.png", fx.encode(page, "png"))])
        r = self.convert(wide, "pano.cbz", profile="test-grey", format="cbz")
        self.assertEqual(r.pages, 1)
        self.assertGreater(jpegs(r.path)[0].height, jpegs(r.path)[0].width)

    def test_kepub_structure(self):
        r = self.convert(self.manga, "Test - Chapter 012.kepub.epub", profile="test-colour")
        self.assertEqual(r.format, "kepub")
        self.assertTrue(r.path.endswith(".kepub.epub"))
        with zipfile.ZipFile(r.path) as z:
            first = z.infolist()[0]
            self.assertEqual((first.filename, first.compress_type), ("mimetype", zipfile.ZIP_STORED))
            opf = z.read("OEBPS/content.opf").decode()
            root = ET.fromstring(opf)
            spine = root.find("o:spine", OPF_NS)
            self.assertEqual(spine.get("page-progression-direction"), "rtl")
            self.assertEqual(len(spine), r.pages)
            for n in range(1, r.pages + 1):
                page = z.read(f"OEBPS/Text/p{n:04d}.xhtml").decode()
                img = Image.open(io.BytesIO(z.read(f"OEBPS/Images/p{n:04d}.jpg")))
                m = re.search(r'content="width=(\d+), height=(\d+)"', page)
                self.assertEqual((int(m[1]), int(m[2])), img.size)
                self.assertEqual(img.mode, "RGB" if n == 1 else "L")   # colour cover kept, grey pages grey
        sides = re.findall(r'properties="rendition:page-spread-(\w+)"', opf)
        self.assertEqual(len(sides), r.pages)
        self.assertEqual(sides[fx.SPREAD - 1:fx.SPREAD + 1], ["right", "left"])
        self.assertEqual(sides[fx.SPREAD - 2], "left")          # the page before a spread closes a pair
        self.assertTrue(all(a != b for a, b in pairwise(sides)))
        self.assertIn('properties="cover-image"', opf)

    def test_generic_keeps_colour_and_skips_the_eink_tone(self):
        plain = self.convert(self.manga, "a.epub", profile="generic")
        dark = self.convert(self.manga, "b.epub", profile="generic", gamma=2.0)
        self.assertEqual(sha(plain.path), sha(dark.path))       # no gamma or autocontrast off e-ink
        pages = jpegs(plain.path)
        self.assertEqual(pages[0].mode, "RGB")
        self.assertEqual(pages[1].mode, "L")                     # a grey page is stored grey, which looks the same
        self.assertTrue(all(800 < max(p.size) <= 900 for p in pages))       # cropped, neither shrunk nor enlarged
        with zipfile.ZipFile(plain.path) as z:
            opf = z.read("OEBPS/content.opf").decode()
        self.assertIn('properties="page-spread-', opf)           # no Kobo prefix
        self.assertNotIn("rendition:page-spread", opf)
        eink = self.convert(self.manga, "c.epub", profile="test-grey")
        eink_dark = self.convert(self.manga, "d.epub", profile="test-grey", gamma=2.0)
        self.assertNotEqual(sha(eink.path), sha(eink_dark.path))
        self.assertGreater(darkness(jpegs(eink_dark.path)[1]), darkness(jpegs(eink.path)[1]))

    def test_colour_grey_makes_every_page_grey(self):
        r = self.convert(self.manga, "grey.epub", profile="test-colour", format="epub", colour="grey")
        self.assertTrue(all(p.mode == "L" for p in jpegs(r.path)))

    def test_output_is_byte_identical_across_runs_and_thread_counts(self):
        a = self.convert(self.manga, "a.kepub.epub", profile="test-colour", threads=1)
        b = self.convert(self.manga, "b.kepub.epub", profile="test-colour", threads=4)
        c = self.convert(self.manga, "c.kepub.epub", profile="test-colour", threads=4)
        self.assertEqual(sha(a.path), sha(b.path))
        self.assertEqual(sha(b.path), sha(c.path))

    def test_pdf(self):
        r = self.convert(self.manga, "x.pdf", profile="test-grey", format="pdf")
        with open(r.path, "rb") as f:
            data = f.read()
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"/Count %d" % r.pages, data)
        self.assertIn(b"/Direction /R2L", data)
        self.assertEqual(data.count(b"/Filter /DCTDecode"), r.pages)
        at = int(data[data.rindex(b"startxref") + 10:].split()[0])
        self.assertEqual(data[at:at + 4], b"xref")

    def test_pad_gives_every_page_the_screen_size(self):
        r = self.convert(self.manga, "pad.cbz", profile="test-grey", format="cbz", pad=True)
        pages = jpegs(r.path)
        self.assertEqual({p.size for p in pages}, {(300, 420)})
        corner = pages[1].convert("L").crop((0, 0, 4, 4))
        self.assertGreater(min(corner.tobytes()), 230)          # filled with the white border

    def test_meta_from_the_caller_wins_over_comicinfo(self):
        uid = "7d9b8a3e-4b8c-4a57-9f55-2f3c6a1d0e11"
        r = self.convert(self.manga, "m.epub", profile="test-grey", format="epub",
                         meta={"series": "Kaguya-sama", "number": 12.5, "chapter": "A Talk", "authors": ["A", "B"],
                               "description": "Plain text.", "uuid": uid, "modified": 1_700_000_000})
        with zipfile.ZipFile(r.path) as z:
            root = ET.fromstring(z.read("OEBPS/content.opf"))
            dates = {i.date_time for i in z.infolist()}
        md = root.find("o:metadata", OPF_NS)
        self.assertEqual(md.findtext("dc:title", namespaces=OPF_NS), "Kaguya-sama - Chapter 12.5: A Talk")
        self.assertEqual(md.findtext("dc:identifier", namespaces=OPF_NS), f"urn:uuid:{uid}")
        self.assertEqual([e.text for e in md.findall("dc:creator", OPF_NS)], ["A", "B"])
        self.assertEqual(md.findtext("dc:description", namespaces=OPF_NS), "Plain text.")
        props = {m.get("property"): m.text for m in md.findall("o:meta", OPF_NS) if m.get("property")}
        self.assertEqual(props["dcterms:modified"], "2023-11-14T22:13:20Z")
        self.assertEqual(props["group-position"], "12.5")
        self.assertEqual(dates, {time.gmtime(1_700_000_000)[:6]})

    def test_progress_is_reported_per_source_page_and_can_cancel(self):
        seen = []
        self.convert(self.manga, "p.cbz", profile="test-grey", format="cbz", progress=lambda i, n: seen.append((i, n)))
        self.assertEqual(seen, [(i, fx.PAGES) for i in range(1, fx.PAGES + 1)])

        def cancel(i, n):
            if i == 3:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.convert(self.manga, "c.cbz", profile="test-grey", format="cbz", progress=cancel)
        self.assertEqual(sorted(os.listdir(self.out)), ["p.cbz"])       # no partial file, no temp file

    def test_layout_is_decided_from_the_headers_alone(self):
        decodes = []

        def load(img):
            decodes.append(bool(img.tile))                      # load() again on a loaded image is free
            return ImageFile.ImageFile.load(img)
        with mock.patch.object(PngImagePlugin.PngImageFile, "load", autospec=True, side_effect=load):
            self.convert(self.manga, "h.cbz", profile="test-grey", format="cbz")
        self.assertEqual(sum(decodes), 3)                       # each PNG page decoded once, when converted

    def test_quality(self):
        low = self.convert(self.manga, "low.cbz", profile="test-grey", format="cbz", quality=50)
        high = self.convert(self.manga, "high.cbz", profile="test-grey", format="cbz", quality=95)
        self.assertLess(low.bytes * 1.3, high.bytes)
        self.assertEqual(high.bytes, os.path.getsize(high.path))

    def test_an_open_file_in_and_out(self):
        buf = io.BytesIO()
        with open(self.manga, "rb") as src:
            r = convert_chapter(src, buf, Options(profile="test-grey", format="cbz"))
        self.assertIsNone(r.path)
        self.assertEqual(r.bytes, len(buf.getvalue()))
        with zipfile.ZipFile(buf) as z:
            self.assertIsNone(z.testzip())


@unittest.skipIf(Image is None, "Pillow is not installed (the convert extra)")
class WebtoonTests(Base):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory(prefix="mangarr-convert-")
        cls.webtoon = fx.webtoon_cbz(os.path.join(cls.dir.name, "Chapter 003.0.cbz"))

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_strips_are_detected_and_cut_into_screens(self):
        r = self.convert(self.webtoon, "w.cbz", profile="test-wide", format="cbz", rtl=True)
        self.assertTrue(r.webtoon)
        self.assertFalse(r.rtl)                                 # a webtoon always reads left to right
        self.assertEqual(r.decided, "webtoon (2 of 2 pages are tall strips); left to right (webtoon)")
        pages = jpegs(r.path)
        page_h = round(400 * 640 / 480)
        self.assertEqual({p.width for p in pages}, {400})
        self.assertTrue(all(p.height <= page_h * engine.WEBTOON_KEEP + 1 for p in pages))
        self.assertGreater(len(pages), 10)
        white = Image.new("L", (400, 1), 255)
        self.assertTrue(all(ImageChops.difference(p.convert("L"), white.resize(p.size)).getbbox() for p in pages))

    def test_epub_of_a_webtoon_has_no_spreads(self):
        r = self.convert(self.webtoon, "w.epub", profile="test-wide")
        with zipfile.ZipFile(r.path) as z:
            opf = z.read("OEBPS/content.opf").decode()
        self.assertIn('<meta property="rendition:spread">none</meta>', opf)
        self.assertNotIn("page-spread-", opf)
        self.assertIn('page-progression-direction="ltr"', opf)

    def test_layout_can_be_forced(self):
        r = self.convert(self.webtoon, "p.cbz", profile="test-wide", format="cbz", webtoon=False)
        self.assertFalse(r.webtoon)
        self.assertEqual(r.pages, 2)
        self.assertIn("paged (as asked)", r.decided)

    def test_pieces_of_a_strip_need_a_hint(self):
        strip = fx.webtoon_strips(400, 640 * 6, 1)[0]
        pieces = [(f"{i + 1:02d}.jpg", fx.encode(strip.crop((0, i * 640, 400, (i + 1) * 640)), "jpg"))
                  for i in range(6)]
        src = fx.pages_cbz(os.path.join(self.tmp, "pieces.cbz"), pieces)
        cases = ({}, {"long_strip": True}, {"country": "KR"}, {"country": "jp"}, {"long_strip": False, "country": "TW"})
        for hints, webtoon, why in zip(cases, (False, True, True, False, True),
                                       ("0 of 6 pages are tall strips", "tagged long strip", "from Korea",
                                        "0 of 6 pages", "from Taiwan"), strict=True):
            with self.subTest(hints):
                r = self.convert(src, "h.cbz", profile="test-wide", format="cbz", hints=hints)
                self.assertEqual(r.webtoon, webtoon)
                self.assertIn(why, r.decided)

    def test_mixed_strips(self):
        s1, s2 = fx.webtoon_strips(400, 3000, 2)
        strips = [s1, s2.resize((600, 4500)).convert("L"), Image.new("RGB", (400, 3000), "white"),
                  s1.crop((0, 0, 400, 1200)), Image.new("L", (320, 100), 255)]
        src = fx.webtoon_cbz(os.path.join(self.tmp, "mixed.cbz"), strips)
        r = self.convert(src, "m.cbz", profile="test-wide", format="cbz")
        pages = jpegs(r.path)
        self.assertTrue(r.webtoon)
        self.assertEqual({p.width for p in pages}, {400})
        self.assertTrue(all(ImageChops.difference(p.convert("L"), Image.new("L", p.size, 255)).getbbox() for p in pages))


@unittest.skipIf(Image is None, "Pillow is not installed (the convert extra)")
class HostileInputTests(Base):
    def page(self, w=600, h=900, fmt="jpg", **kw):
        return fx.encode(Image.new("L", (w, h), 200), fmt, **kw)

    def assertFails(self, src, needle, **kw):
        with self.assertRaises(ConvertError) as cm:
            self.convert(src, "x.kepub.epub", profile="test-colour", **kw)
        self.assertIn(needle, str(cm.exception))
        self.assertEqual(os.listdir(self.out), [])              # nothing left behind
        return cm.exception

    def test_truncated_pages_name_the_page(self):
        rnd = fx.random.Random(2)
        pages = {n: fx.encode(fx.manga_page(600, 900, i, rnd), n.rsplit(".", 1)[1])
                 for i, n in enumerate(("001.jpg", "002.webp", "003.jpg"), 1)}
        for victim, cut in (("002.webp", 500), ("003.jpg", 3000)):
            with self.subTest(victim):
                bad = fx.pages_cbz(os.path.join(self.tmp, "bad.cbz"),
                                   [(n, data[:cut] if n == victim else data) for n, data in pages.items()])
                for webtoon in (None, False):
                    self.assertFails(bad, victim, webtoon=webtoon)

    def test_a_decompression_bomb_is_refused_from_its_header(self):
        src = fx.pages_cbz(os.path.join(self.tmp, "bomb.cbz"), [("001.png", fx.png_header(20000, 20000))])
        started = time.monotonic()
        for webtoon in (None, False, True):
            self.assertFails(src, "001.png: image too large", webtoon=webtoon)
        self.assertLess(time.monotonic() - started, 2)
        with mock.patch.object(engine, "MAX_PIXELS", 500 * 800):     # our own cap, not only Pillow's
            src = fx.pages_cbz(os.path.join(self.tmp, "big.cbz"), [("001.jpg", self.page(600, 900))])
            self.assertFails(src, "001.jpg: image too large", webtoon=False)

    def test_archive_limits(self):
        src = fx.pages_cbz(os.path.join(self.tmp, "many.cbz"), [(f"{i:05d}.jpg", b"") for i in range(5001)])
        self.assertFails(src, "5001 entries")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("001.jpg", self.page())
            z.writestr("002.jpg", self.page())
        src = os.path.join(self.tmp, "d64.cbz")
        with open(src, "wb") as f:
            f.write(fx.with_method(buf.getvalue(), "002.jpg", 9))
        self.assertFails(src, "002.jpg: cannot be read from the archive", webtoon=False)
        with open(src, "wb") as f:
            f.write(b"not a zip at all" * 100)
        self.assertFails(src, "not a readable zip archive")
        src = fx.pages_cbz(os.path.join(self.tmp, "none.cbz"), [("notes.txt", b"hi"), ("__MACOSX/._001.jpg", b"")])
        self.assertFails(src, "no pages in the archive")
        src = fx.pages_cbz(os.path.join(self.tmp, "big.cbz"), [("001.jpg", self.page())])
        with mock.patch("mangarr.convert.source.MAX_ENTRY_BYTES", 1000):
            self.assertFails(src, "001.jpg: larger than")
        src = fx.pages_cbz(os.path.join(self.tmp, "jxl.cbz"), [("001.jpg", self.page()), ("002.jxl", b"\xff\x0a")])
        self.assertFails(src, "002.jxl: unreadable image", webtoon=False)

    def test_comicinfo_with_a_doctype_is_ignored(self):
        info = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa">]>'
                "<ComicInfo><Series>&a;</Series><Manga>YesAndRightToLeft</Manga></ComicInfo>")
        src = fx.pages_cbz(os.path.join(self.tmp, "Chapter 7.cbz"), [("001.jpg", self.page())], info)
        r = self.convert(src, "d.epub", profile="test-grey")
        self.assertFalse(r.rtl)
        self.assertIn("no direction given", r.decided)
        with zipfile.ZipFile(r.path) as z:
            title = ET.fromstring(z.read("OEBPS/content.opf")).find("o:metadata/dc:title", OPF_NS).text
        self.assertEqual(title, "Chapter 7")

    def test_odd_image_modes(self):
        grey16 = Image.new("I;16", (600, 900), 30000)
        rotated = Image.new("L", (900, 600), 90)
        exif = rotated.getexif()
        exif[0x0112] = 6                                        # shown turned 90 degrees
        rgba = Image.new("RGBA", (600, 900), (0, 0, 0, 0))
        frames = [Image.new("L", (600, 900), v) for v in (40, 250)]
        gif = io.BytesIO()
        frames[0].save(gif, "GIF", save_all=True, append_images=frames[1:])
        src = fx.pages_cbz(os.path.join(self.tmp, "modes.cbz"),
                           [("1.png", fx.encode(grey16, "png")), ("2.jpg", fx.encode(rotated, "jpg", exif=exif.tobytes())),
                            ("3.png", fx.encode(rgba, "png")), ("4.gif", gif.getvalue())])
        r = self.convert(src, "m.cbz", profile="test-grey", format="cbz", crop=False, webtoon=False)
        pages = jpegs(r.path)
        means = [255 - darkness(p) for p in pages]
        self.assertAlmostEqual(means[0], 30000 / 256, delta=3)   # 16 bits scaled down, not clipped to white
        self.assertGreater(pages[1].height, pages[1].width)     # upright
        self.assertGreater(means[2], 250)                        # transparency on white
        self.assertLess(means[3], 50)                            # the first frame


@unittest.skipIf(Image is None, "Pillow is not installed (the convert extra)")
class DraftTests(Base):
    def test_large_jpegs_decode_at_a_reduced_scale_with_the_same_result(self):
        rnd = fx.random.Random(5)
        pages = [(f"{n}.jpg", fx.encode(fx.manga_page(1400, 2000, n, rnd), "jpg", quality=90)) for n in (1, 2)]
        pages.append(("3.jpg", fx.encode(fx.manga_page(2800, 2000, 3, rnd), "jpg", quality=90)))
        src = fx.pages_cbz(os.path.join(self.tmp, "hires.cbz"), pages)
        real = JpegImagePlugin.JpegImageFile.draft
        with mock.patch.object(JpegImagePlugin.JpegImageFile, "draft", autospec=True, side_effect=real) as spy:
            fast = self.convert(src, "fast.cbz", profile="test-grey", format="cbz")
        self.assertEqual(spy.call_count, 3)
        with mock.patch.object(engine, "_draft"):
            full = self.convert(src, "full.cbz", profile="test-grey", format="cbz")
        a, b = jpegs(fast.path), jpegs(full.path)
        self.assertEqual(len(a), 4)
        for x, y in zip(a, b, strict=True):
            # the crop is found on the smaller image, a few source pixels coarser
            self.assertLessEqual(abs(x.width - y.width), y.width * 0.02 + 1)
            self.assertLessEqual(abs(x.height - y.height), 2)
            self.assertAlmostEqual(darkness(x), darkness(y), delta=1.5)
        with mock.patch.object(JpegImagePlugin.JpegImageFile, "draft", autospec=True, side_effect=real) as spy:
            self.convert(src, "generic.cbz", profile="generic", format="cbz")
        self.assertEqual(spy.call_count, 0)                     # not twice the screen after the largest crop


if __name__ == "__main__":
    unittest.main()
