"""The converter's standard-library side, without Pillow: the server can
list profiles and ask whether conversion is available without Pillow ever
being imported; options are checked before anything runs; the archive
reader orders pages, caps entries and refuses ComicInfo it should not
parse; and the engine's own tests are skipped only when Pillow is
missing."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

import mangarr
import mangarr.convert as convert
from mangarr.convert import FORMATS, ConvertError, Options, available, check_hints, convert_chapter, source
from mangarr.convert.profiles import DEFAULT_PROFILE, KINDLE_NOTE, PROFILES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(mangarr.__file__)))


def run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": ROOT})


def engine_tests(first: str, name: str = "tests.test_convert_engine") -> subprocess.CompletedProcess:
    """The engine's tests run by dotted name, as `python -m unittest` does
    (tests/ is not on sys.path), after the line `first`."""
    return run(f"import sys, unittest\n{first}\nunittest.main(module=None, argv=['test', '-v', {name!r}])")


def cbz(entries) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
    return zipfile.ZipFile(buf)


class ImportTests(unittest.TestCase):
    def test_the_standard_library_modules_never_import_pillow(self):
        r = run("import sys, mangarr.convert, mangarr.convert.profiles, mangarr.convert.source, "
                "mangarr.convert.writers\n"
                "ok, detail = mangarr.convert.available()\n"
                "print('PIL' in sys.modules, [m for m in sys.modules if m.startswith('mangarr.convert.engine')])")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False []")

    def test_without_pillow(self):
        # sys.modules["PIL"] = None makes every import of PIL fail, as if it were not installed
        r = run("import io, sys\nsys.modules['PIL'] = None\n"
                "from mangarr.convert import available, convert_chapter, Options\n"
                "from mangarr.convert.writers import WRITERS, Book, Page\n"
                "from mangarr.convert.profiles import PROFILES\n"
                "print(available())\n"
                "f = io.BytesIO(); w = WRITERS['epub'](f, Book('t'), PROFILES['generic'])\n"
                "w.add(Page(b'x', 1, 1)); w.close(); print(len(f.getvalue()) > 0)\n"
                "try:\n    convert_chapter(io.BytesIO(), io.BytesIO(), Options())\n"
                "except RuntimeError as e:\n    print('refused:', e)\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], "(False, \"Pillow is not installed: pip install 'mang-arr[convert]'\")")
        self.assertEqual(lines[1], "True")
        self.assertEqual(lines[2], "refused: Pillow is not installed: pip install 'mang-arr[convert]'")

    def test_the_engine_tests_are_skipped_without_pillow(self):
        r = engine_tests("sys.modules['PIL'] = None")
        self.assertEqual(r.returncode, 0, r.stderr)
        ran = int(re.search(r"Ran (\d+) tests", r.stderr)[1])
        self.assertGreater(ran, 20)
        self.assertIn(f"OK (skipped={ran})", r.stderr)
        self.assertEqual(r.stderr.count("skipped 'Pillow is not installed (the convert extra)'"), ran)

    @unittest.skipIf(importlib.util.find_spec("PIL") is None, "Pillow is not installed (the convert extra)")
    def test_with_pillow_the_engine_tests_run_or_fail(self):
        r = engine_tests("", "tests.test_convert_engine.HostileInputTests.test_comicinfo_with_a_doctype_is_ignored")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Ran 1 test", r.stderr)
        self.assertNotIn("skipped", r.stderr)
        # an engine that cannot be imported is an error, not a run of skips
        r = engine_tests("sys.modules['mangarr.convert.engine'] = None")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAILED (errors=1)", r.stderr)
        self.assertIn("mangarr.convert.engine", r.stderr)
        self.assertNotIn("skipped", r.stderr)

    def test_available(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            self.assertEqual(available(), (False, "Pillow is not installed: pip install 'mang-arr[convert]'"))
        with mock.patch("importlib.util.find_spec", return_value=object()), \
                mock.patch("importlib.metadata.version", return_value="11.3.0"):
            ok, detail = available()
            self.assertFalse(ok)
            self.assertIn("Pillow 11.3.0 is older than 12.3", detail)
        with mock.patch("importlib.util.find_spec", return_value=object()), \
                mock.patch("importlib.metadata.version", return_value="12.10.0.dev1"):
            self.assertEqual(available(), (True, "Pillow 12.10.0.dev1"))
        with mock.patch("importlib.util.find_spec", return_value=object()), \
                mock.patch("importlib.metadata.version", side_effect=convert.importlib.metadata.PackageNotFoundError):
            self.assertFalse(available()[0])


class OptionTests(unittest.TestCase):
    def test_defaults_are_the_generic_epub(self):
        o = Options()
        self.assertEqual((o.profile, o.output_format, o.jpeg_quality), ("generic", "epub", 90))
        self.assertEqual(Options(profile="kobo-libra").output_format, "kepub")
        self.assertEqual(Options(profile="kobo-libra", quality=70).jpeg_quality, 70)
        self.assertEqual(Options.from_dict(o.to_dict()), o)
        self.assertEqual(hash(Options(gamma=1.2)), hash(Options.from_dict({"gamma": 1.2})))

    def test_bad_options_are_refused_in_plain_words(self):
        cases = [({"profile": "kindle-paperwhite"}, "unknown profile"),
                 ({"profile": "generic", "format": "kepub"}, "not offered for Generic EPUB"),
                 ({"format": "mobi"}, "not offered"),
                 ({"spreads": "fold"}, "spreads must be one of split, rotate, both, keep"),
                 ({"colour": "sepia"}, "colour must be"),
                 ({"crop": "yes"}, "crop must be true or false"),
                 ({"pad": 1}, "pad must be"),
                 ({"gamma": 0.4}, "gamma must be between 0.5 and 3.0"),
                 ({"gamma": float("nan")}, "gamma"),
                 ({"gamma": True}, "gamma"),
                 ({"quality": 96}, "quality must be a whole number from 50 to 95"),
                 ({"quality": 80.0}, "quality"),
                 ({"quality": True}, "quality")]
        for d, needle in cases:
            with self.subTest(d), self.assertRaises(ValueError) as cm:
                Options.from_dict(d)
            self.assertIn(needle, str(cm.exception))
        for bad in ({"threads": 2}, {"../x": 1}, []):
            with self.subTest(bad), self.assertRaises(ValueError):
                Options.from_dict(bad)

    def test_hints(self):
        self.assertEqual(check_hints(None), {"long_strip": None, "country": ""})
        self.assertEqual(check_hints({"country": "kr", "long_strip": True}), {"long_strip": True, "country": "KR"})
        for bad in ({"long_strip": "yes"}, {"country": 5}, {"country": "Korea"}, {"tags": []}, ["KR"]):
            with self.subTest(bad), self.assertRaises(ValueError):
                check_hints(bad)

    def test_arguments_are_checked_before_anything_is_read_or_written(self):
        with tempfile.TemporaryDirectory(prefix="mangarr-convert-") as d:
            dst = os.path.join(d, "out.epub")
            for kw in ({"rtl": "yes"}, {"webtoon": 1}, {"threads": 0}, {"threads": 9}, {"threads": True},
                       {"meta": []}, {"hints": {"country": "Japan"}}, {"options": {"profile": "generic"}}):
                with self.subTest(kw), self.assertRaises(ValueError):
                    convert_chapter(os.path.join(d, "missing.cbz"), dst, **kw)
            with mock.patch.object(convert, "available", return_value=(False, "no Pillow")), \
                    self.assertRaisesRegex(RuntimeError, "no Pillow"):
                convert_chapter(os.path.join(d, "missing.cbz"), dst)
            self.assertEqual(os.listdir(d), [])


class ProfileTests(unittest.TestCase):
    def test_table(self):
        self.assertEqual(DEFAULT_PROFILE, "generic")
        generic = PROFILES["generic"]
        self.assertEqual(generic.name, "Generic EPUB (any reader)")
        self.assertTrue(generic.colour)
        self.assertFalse(generic.eink)
        self.assertEqual(generic.default_format, "epub")
        for p in PROFILES.values():
            with self.subTest(p.key):
                self.assertTrue(set(p.formats) <= set(FORMATS))
                self.assertIn(p.default_format, p.formats)
                self.assertEqual("kepub" in p.formats, p.vendor == "kobo")
                self.assertGreater(p.height, p.width)
                self.assertTrue(50 <= p.quality <= 95)
                self.assertNotIn("kindle", p.key)
        self.assertIn("Send to Kindle", KINDLE_NOTE)


class SourceTests(unittest.TestCase):
    def test_pages_in_natural_order_without_junk(self):
        z = cbz([(n, b"x") for n in ("p10.jpg", "p2.JPG", "p1.jpeg", "__MACOSX/p1.jpg", ".hidden.jpg",
                                     "sub/.DS_Store", "notes.txt", "ComicInfo.xml", "v1/c10/p1.png",
                                     "v1/c2/p1.webp", "v1/.thumbs/p1.png", "cover.avif", "dir/")])
        self.assertEqual(source.page_names(z), ["cover.avif", "p1.jpeg", "p2.JPG", "p10.jpg", "v1/c2/p1.webp",
                                                "v1/c10/p1.png"])

    def test_page_names_with_odd_digits(self):
        long = "9" * 5000 + ".jpg"                              # past int()'s 4300-digit limit
        z = cbz([(n, b"x") for n in ("1\u00b22.jpg", long, "\u0663.jpg", "10.jpg", "001.jpg", "2.jpg")])
        self.assertEqual(source.page_names(z), ["001.jpg", "1\u00b22.jpg", "2.jpg", "10.jpg", long, "\u0663.jpg"])

    def test_entry_caps(self):
        z = cbz([("a.jpg", b"x" * 100)])
        self.assertEqual(source.read_entry(z, "a.jpg"), b"x" * 100)
        with self.assertRaisesRegex(ConvertError, "a.jpg: larger than"):
            source.read_entry(z, "a.jpg", cap=99)
        with mock.patch.object(source, "MAX_ENTRIES", 3), self.assertRaisesRegex(ConvertError, "4 entries"):
            source.page_names(cbz([(f"{i}.jpg", b"") for i in range(4)]))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as w:
            w.writestr("b.jpg", b"y" * 5000)
        data = bytearray(buf.getvalue())
        data[40:44] = b"\x00\x00\x00\x00"                       # damage the deflate stream
        with self.assertRaisesRegex(ConvertError, "b.jpg: cannot be read from the archive"):
            source.read_entry(zipfile.ZipFile(io.BytesIO(bytes(data))), "b.jpg")

    def test_comicinfo(self):
        def info(text, name="ComicInfo.xml"):
            return source.read_comicinfo(cbz([(name, text)]))
        good = "<ComicInfo><Series> S &amp; T </Series><Pages><Page Image='0'/></Pages><Number>3</Number></ComicInfo>"
        self.assertEqual(info(good), {"Series": "S & T", "Number": "3"})
        self.assertEqual(info(good, "comicinfo.XML"), {"Series": "S & T", "Number": "3"})
        self.assertEqual(info(good, "sub/ComicInfo.xml"), {})
        self.assertEqual(info("\ufeff" + good), {"Series": "S & T", "Number": "3"})
        self.assertEqual(info(good.encode("utf-16")), {})
        self.assertEqual(info('<?xml version="1.0"?><!DOCTYPE ComicInfo><ComicInfo><Series>x</Series></ComicInfo>'), {})
        self.assertEqual(info('<!ENTITY a "b"><ComicInfo/>'), {})
        self.assertEqual(info("<ComicInfo><Series>x</Series>"), {})
        self.assertEqual(info("<ComicInfo><Summary>" + "x" * source.MAX_COMICINFO + "</Summary></ComicInfo>"), {})
        self.assertEqual(source.read_comicinfo(cbz([("001.jpg", b"")])), {})


if __name__ == "__main__":
    unittest.main()
