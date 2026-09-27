"""The staging tree is written by Suwayomi and its extensions and the names
come from scraped sites: names fit the filesystem, symlinks are never
followed, bad archives are set aside instead of breaking the import, and
one bad chapter never blocks the rest of the series."""
import bz2
import errno
import logging
import os
import random
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import zlib
from unittest import mock

from mangarr import core, db, library
from mangarr.library import (
    chapter_filename,
    fit_name,
    link_into_library,
    prune_quarantine,
    quarantine,
    safe_title,
    scan_series_dir,
    staging_dirs,
    unique_folder,
    verify_archive,
)
from mangarr.model import Series


def make_cbz(path: str, pages: int = 1) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(pages):
            z.writestr(f"{i:03d}.jpg", os.urandom(2000))
    return path


def read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def age(path: str, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t), follow_symlinks=False)


def stored(data: bytes) -> tuple:
    return data, zipfile.ZIP_STORED, zlib.crc32(data), len(data)


def deflated(data: bytes) -> tuple:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush(), zipfile.ZIP_DEFLATED, zlib.crc32(data), len(data)


def build_zip(path: str, entries, zip64: bool = False, count: int | None = None, repeat: int = 1) -> str:
    """Write a zip by hand, for archives zipfile will not write: entries are
    (name, stored bytes, method, crc, size). zip64 adds zip64 end records,
    count overrides the entry count in the end records, and repeat writes
    the central directory that many times over (the same entries again)."""
    local, central = bytearray(), bytearray()
    for name, data, method, crc, size in entries:
        n = name.encode()
        central += struct.pack("<4s6H3L5H2L", b"PK\x01\x02", 20, 20, 0, method, 0, 0x21, crc, len(data), size,
                               len(n), 0, 0, 0, 0, 0, len(local)) + n
        local += struct.pack("<4s5H3L2H", b"PK\x03\x04", 20, 0, method, 0, 0x21, crc, len(data), size, len(n), 0)
        local += n + data
    total = len(entries) * repeat if count is None else count
    with open(path, "wb") as f:
        f.write(local)
        cd_at = f.tell()
        for i in range(0, repeat, 10000):
            f.write(bytes(central) * min(10000, repeat - i))
        cd_size = f.tell() - cd_at
        if zip64:
            rec_at = f.tell()
            f.write(struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, total, total, cd_size, cd_at))
            f.write(struct.pack("<4sLQL", b"PK\x06\x07", 0, rec_at, 1))
            f.write(struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0))
        else:
            f.write(struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, total, total, cd_size, cd_at, 0))
    return path


def hidden_directory(path: str, hidden: int) -> tuple[bytes, bytes]:
    """The evidence file for the end-record race: one stored page, a directory
    of `hidden` entries nothing points at, then a one-entry directory and an
    end record for it. Returns (that end record, one whose directory spans
    both, still claiming one entry); each is the last 22 bytes of the file."""
    page, name = os.urandom(3000), b"0.jpg"
    local = struct.pack("<4s5H3L2H", b"PK\x03\x04", 20, 0, 0, 0, 0x21, zlib.crc32(page), len(page), len(page),
                        len(name), 0) + name + page
    central = struct.pack("<4s6H3L5H2L", b"PK\x01\x02", 20, 20, 0, 0, 0, 0x21, zlib.crc32(page), len(page),
                          len(page), len(name), 0, 0, 0, 0, 0, 0) + name
    with open(path, "wb") as f:
        f.write(local + central * hidden + central)
    small = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, len(central), len(local) + len(central) * hidden, 0)
    big = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, len(central) * (hidden + 1), len(local), 0)
    with open(path, "ab") as f:
        f.write(small)
    return small, big


class NameLengthTest(unittest.TestCase):
    def test_cjk_chapter_label_fits_255_bytes(self):
        for label in ("第" * 85, "第" * 79, "😀" * 70, "x" * 300):
            name = chapter_filename(12.0, label)
            self.assertLessEqual(len(name.encode()), 255, name)
            self.assertTrue(name.startswith("Chapter 012.0 - ") and name.endswith(".cbz"))
        self.assertEqual(chapter_filename(12.0, "第" * 85), chapter_filename(12.0, "第" * 85))   # stable

    def test_fit_name(self):
        self.assertEqual(fit_name("short", keep=".cbz"), "short.cbz")
        a, b = fit_name("é" * 200 + "a"), fit_name("é" * 200 + "b")
        self.assertLessEqual(len(a.encode()), 255)
        self.assertNotEqual(a, b)                                # hash of the full name keeps them apart
        a.encode().decode("utf-8")                               # cut on a character boundary

    def test_long_titles_fit_as_folders(self):
        for title in ("The " + "Very " * 60 + "Long Title", "進撃" * 60):
            folder = unique_folder(title, set(), "anilist:1")
            self.assertLessEqual(len(folder.encode()), 255)
            again = unique_folder(title, {folder}, "anilist:2")
            self.assertLessEqual(len(again.encode()), 255)
            self.assertNotEqual(folder, again)

    def test_really_links_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = make_cbz(os.path.join(tmp, "a.cbz"))
            folder = unique_folder("進撃" * 60, set(), "anilist:1")
            dst = link_into_library(src, folder, 12.0, root=os.path.join(tmp, "lib"), label="第" * 85)
            self.assertTrue(os.path.samefile(src, dst))

    def test_leading_dot_is_not_hidden(self):
        self.assertEqual(safe_title(".hack//Link"), "hack__Link")
        self.assertEqual(safe_title("..."), "untitled")


class UniqueFolderTest(unittest.TestCase):
    def test_case_and_normalisation_insensitive(self):
        self.assertNotEqual(unique_folder("berserk", {"Berserk"}, "anilist:2").casefold(), "berserk")
        nfd = "Pokémon".replace("é", "é")
        self.assertEqual(unique_folder(nfd, set(), "anilist:1"), "Pokémon")        # stored as NFC
        self.assertNotEqual(unique_folder("Pokémon", {nfd}, "anilist:2"), "Pokémon")

    def test_suffixed_name_is_rechecked(self):
        # an AniList 'Re:Zero' and a manual 'Re:Zero' own the two obvious names
        taken = {"Re_Zero", "Re_Zero (manual_Re_Zero)"}
        folder = unique_folder("Re_Zero", taken, "manual:Re_Zero")
        self.assertNotIn(folder, taken)
        self.assertEqual(folder, "Re_Zero (manual_Re_Zero) 2")


class SymlinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.secret = make_cbz(os.path.join(self.root, "outside.cbz"))
        self.staging = os.path.join(self.root, "staging")
        self.series = os.path.join(self.staging, "Src (EN)", "Title")
        os.makedirs(self.series)

    def tearDown(self):
        self.tmp.cleanup()

    def test_symlinked_chapter_is_skipped(self):
        make_cbz(os.path.join(self.series, "Chapter 1.cbz"))
        os.symlink(self.secret, os.path.join(self.series, "Chapter 2.cbz"))
        with self.assertLogs("mangarr.library", "WARNING") as cm:
            found, unparsed = scan_series_dir(self.series)
        self.assertEqual(sorted(found), [1.0])
        self.assertIn("symlink", "\n".join(cm.output))
        self.assertEqual(verify_archive(os.path.join(self.series, "Chapter 2.cbz")), (False, "not a regular file"))
        with self.assertLogs("mangarr.library", "ERROR"):
            self.assertIsNone(link_into_library(os.path.join(self.series, "Chapter 2.cbz"), "T", 2.0,
                                                root=os.path.join(self.root, "lib")))

    def test_symlinked_folders_are_not_adopted(self):
        elsewhere = os.path.join(self.root, "elsewhere")
        os.makedirs(os.path.join(elsewhere, "Series"))
        os.symlink(elsewhere, os.path.join(self.staging, "Evil"))
        os.symlink(elsewhere, os.path.join(self.staging, "Src (EN)", "Linked"))
        with self.assertLogs("mangarr.library", "WARNING"):
            dirs = staging_dirs(self.staging)
        self.assertEqual([(s, n) for s, n, _ in dirs], [("Src (EN)", "Title")])

    def test_symlink_at_library_path_is_not_written_through(self):
        lib = os.path.join(self.root, "lib")
        os.makedirs(os.path.join(lib, "T"))
        target = os.path.join(self.root, "victim.cbz")
        os.symlink(target, os.path.join(lib, "T", "Chapter 001.0.cbz"))      # dangling
        src = make_cbz(os.path.join(self.series, "Chapter 1.cbz"))
        with self.assertLogs("mangarr.library", "ERROR"):
            self.assertIsNone(link_into_library(src, "T", 1.0, root=lib))
        self.assertFalse(os.path.exists(target))

    def test_quarantine_refuses_a_symlink(self):
        link = os.path.join(self.series, "Chapter 3.cbz")
        os.symlink(self.secret, link)
        with self.assertRaises(OSError):
            quarantine(link)
        self.assertTrue(os.path.exists(self.secret))


class VerifyArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_deflate_stream_is_reported_not_raised(self):
        p = os.path.join(self.d, "c.cbz")
        r = random.Random(0)                           # compressible, so deflate uses Huffman codes
        body = " ".join("".join(r.choice("abcdefghij") for _ in range(r.randint(2, 8))) for _ in range(4000))
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("001.jpg", body.encode())
        data = bytearray(read(p))
        start = 30 + len("001.jpg")                    # local header, then the deflate stream
        for i in range(start + 40, start + 80):        # was: zlib.error escaped verify_archive
            data[i] ^= 0xFF
        write(p, bytes(data))
        ok, detail = verify_archive(p)
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_unsupported_method_and_encrypted(self):
        for tweak in ("method", "encrypted"):
            p = make_cbz(os.path.join(self.d, f"{tweak}.cbz"))
            data = bytearray(read(p))
            cd = data.rfind(b"PK\x01\x02")
            if tweak == "method":
                data[cd + 10:cd + 12] = (99).to_bytes(2, "little")
                data[10:12] = (99).to_bytes(2, "little")
            else:
                data[cd + 8] |= 1
                data[6] |= 1
            write(p, bytes(data))
            if tweak == "method":                       # refused from the directory, before zipfile opens it
                with self.assertLogs("mangarr.library", "WARNING"):
                    ok, detail = verify_archive(p)
                self.assertIn("compressed with AES", detail)
            else:
                ok, detail = verify_archive(p)
            self.assertFalse(ok, tweak)

    def test_caps(self):
        many = os.path.join(self.d, "many.cbz")
        with zipfile.ZipFile(many, "w") as z:
            for i in range(library.MAX_ENTRIES + 1):
                z.writestr(f"{i}.jpg", b"")
        with self.assertLogs("mangarr.library", "WARNING"):
            self.assertIn("entries", verify_archive(many)[1])
        bomb = os.path.join(self.d, "bomb.cbz")
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("001.jpg", b"\0" * (4 << 20))
            z.writestr("002.jpg", os.urandom(2000))
        with self.assertLogs("mangarr.library", "WARNING"):
            self.assertIn("expands", verify_archive(bomb)[1])
        with mock.patch.object(library, "MAX_UNCOMPRESSED", 1000), self.assertLogs("mangarr.library", "WARNING"):
            self.assertIn("uncompressed", verify_archive(make_cbz(os.path.join(self.d, "big.cbz")))[1])

    def test_good_archive_still_passes(self):
        self.assertEqual(verify_archive(make_cbz(os.path.join(self.d, "g.cbz"), 3)), (True, "3 pages"))


class ZipBombTest(unittest.TestCase):
    """verify_archive against archives made to cost time and memory (finding
    114): refused from the end records or the directory, before zipfile
    parses the directory or decompresses anything, while real chapters
    (incompressible pages, a ComicInfo.xml) still pass."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.parsed: list = []

    def tearDown(self):
        self.tmp.cleanup()

    def _no_parse(self):
        """zipfile.ZipFile must not even be constructed."""
        return mock.patch.object(zipfile, "ZipFile", side_effect=lambda *a, **k: self.parsed.append(a))

    def test_million_entries_refused_fast_and_small(self):
        p = build_zip(os.path.join(self.d, "million.cbz"), [("0.jpg", *stored(b""))], zip64=True, repeat=1_000_000)
        self.assertGreater(os.path.getsize(p), 50_000_000)             # a real 1,000,000-entry directory
        code = ("import resource, sys, time\n"
                "from mangarr import library\n"
                "base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
                "t = time.monotonic()\n"
                "ok, detail = library.verify_archive(sys.argv[1])\n"
                "grew = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - base\n"
                "print(ok, time.monotonic() - t, grew, detail, sep='|')\n")
        root = os.path.dirname(os.path.dirname(os.path.abspath(library.__file__)))
        out = subprocess.run([sys.executable, "-c", code, p], capture_output=True, text=True, timeout=120,
                             env=dict(os.environ, PYTHONPATH=root))
        ok, secs, grew_kib, detail = out.stdout.strip().split("|", 3)
        self.assertEqual((ok, detail), ("False", "1000000 entries (limit 5000)"), out.stderr)
        self.assertLess(float(secs), 1.0)                       # was 19.6 s
        self.assertLess(int(grew_kib), 16 << 10)                # was 519 MB of peak RSS
        with self._no_parse(), self.assertLogs("mangarr.library", "WARNING"):
            self.assertFalse(verify_archive(p)[0])
        self.assertEqual(self.parsed, [])

    def test_directory_bigger_than_its_end_record_says(self):
        p = build_zip(os.path.join(self.d, "lying.cbz"), [("0.jpg", *stored(os.urandom(2000)))],
                      repeat=library.MAX_ENTRIES + 1, count=3)
        with self._no_parse(), self.assertLogs("mangarr.library", "WARNING"):
            self.assertEqual(verify_archive(p), (False, "more than 5000 entries (limit 5000)"))
        p = build_zip(os.path.join(self.d, "names.cbz"), [("x" * 5000 + ".jpg", *stored(os.urandom(2000)))],
                      repeat=1000)
        with self._no_parse(), self.assertLogs("mangarr.library", "WARNING"):
            self.assertIn("bytes of directory", verify_archive(p)[1])
        self.assertEqual(self.parsed, [])

    def test_zero_pages_refused_before_decompressing(self):
        page = deflated(b"\0" * (1 << 20))
        small = deflated(b"\0" * (250 << 10))                  # under the per-entry ratio floor
        cases = [(2047, page, "uncompressed"),                  # the evidence: 2 GiB, passed after 14 s
                 (900, page, "expands"),                        # under 1 GiB, but each page expands ~1000x
                 (4000, small, "byte file")]                    # small pages: the whole file expands ~800x
        for pages, entry, why in cases:
            p = build_zip(os.path.join(self.d, f"zeros{pages}.cbz"), [(f"{i:04d}.jpg", *entry) for i in range(pages)])
            t = time.monotonic()
            with mock.patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("decompressed")) as opened, \
                    self.assertLogs("mangarr.library", "WARNING"):
                ok, detail = verify_archive(p)
            self.assertFalse(ok, pages)
            self.assertIn(why, detail)
            opened.assert_not_called()
            self.assertLess(time.monotonic() - t, 2.0)

    def test_real_chapters_still_pass(self):
        rnd = random.Random(1)
        info = (b'<?xml version="1.0" encoding="utf-8"?><ComicInfo><Series>Title</Series><Number>12</Number>'
                b"<Summary>" + b"a few words " * 300 + b"</Summary></ComicInfo>")
        for method in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED):
            p = os.path.join(self.d, f"real{method}.cbz")
            with zipfile.ZipFile(p, "w", method) as z:
                for i in range(20):                             # incompressible pages, some over 256 KiB
                    z.writestr(f"{i:03d}.jpg", rnd.randbytes(rnd.randint(20_000, 400_000)))
                z.writestr("ComicInfo.xml", info)
                z.comment = b"scanned by someone"               # the end record is not the last 22 bytes
            self.assertEqual(verify_archive(p), (True, "20 pages"))
        p = build_zip(os.path.join(self.d, "zip64.cbz"),       # zip64 end records, which some tools always write
                      [(f"{i:03d}.png", *deflated(rnd.randbytes(3000))) for i in range(3)]
                      + [("ComicInfo.xml", *deflated(info))], zip64=True)
        self.assertEqual(verify_archive(p), (True, "3 pages"))

    def test_bzip2_and_lzma_are_refused_before_decompressing(self):
        """The evidence: zipfile inflates a whole bzip2 or LZMA read at once, so
        a few KB declared as a 1000-byte page expanded to gigabytes in memory,
        and passed when the CRC matched the first 1000 bytes."""
        zeros = b"\0" * (16 << 20)                             # 16 MiB here; the evidence used 1-2 GiB
        lz = zipfile.LZMACompressor()
        packed = {"bzip2": (bz2.compress(zeros), zipfile.ZIP_BZIP2),
                  "LZMA": (lz.compress(zeros) + lz.flush(), zipfile.ZIP_LZMA)}
        for method, (data, code) in packed.items():
            p = build_zip(os.path.join(self.d, f"{method}.cbz"),
                          [("000.jpg", *stored(os.urandom(3000))),
                           ("001.jpg", data, code, zlib.crc32(zeros[:1000]), 1000)])
            self.assertLess(os.path.getsize(p), 10_000)
            with mock.patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("decompressed")) as opened, \
                    self.assertLogs("mangarr.library", "WARNING"):
                ok, detail = verify_archive(p)
            self.assertEqual((ok, detail), (False, f"entry '001.jpg' is compressed with {method}; "
                                                   "only stored or deflate is accepted"))
            opened.assert_not_called()

    def test_overlapping_entries_are_refused(self):
        # 300 entries reading back one 3000-byte page behind 1 MiB of empty deflate
        # blocks: 300 MiB of inflating, which no size or ratio limit sees
        page = os.urandom(3000)
        data, method, crc, size = deflated(page)
        p = build_zip(os.path.join(self.d, "overlap.cbz"),
                      [("0.jpg", b"\0\0\0\xff\xff" * ((1 << 20) // 5) + data, method, crc, size)], repeat=300)
        with mock.patch.object(zipfile.ZipFile, "open", side_effect=AssertionError("read back")), \
                self.assertLogs("mangarr.library", "WARNING"):
            ok, detail = verify_archive(p)
        self.assertFalse(ok)
        self.assertIn("overlap", detail)

    def _spy_limits(self):
        seen: list = []
        real = library._archive_limits

        def spy(infos, size):
            seen.append(len(infos))
            return real(infos, size)
        return seen, mock.patch.object(library, "_archive_limits", spy)

    def test_end_record_rewritten_after_the_check(self):
        """The evidence repro made deterministic: the end record is switched to
        a hidden 20,001-entry directory right after the directory was checked.
        zipfile parses the directory that was checked, not the new one."""
        p = os.path.join(self.d, "flip.cbz")
        small, big = hidden_directory(p, 20000)
        real = library._directory_limits

        def check_then_rewrite(snap, *end):
            res = real(snap, *end)
            fd = os.open(p, os.O_WRONLY)
            os.pwrite(fd, big, os.path.getsize(p) - len(big))
            os.close(fd)
            return res
        seen, spy = self._spy_limits()
        with spy, mock.patch.object(library, "_directory_limits", check_then_rewrite):
            self.assertEqual(verify_archive(p), (True, "1 pages"))
        self.assertEqual(seen, [1])                             # was: 20001 entries parsed after the check
        self.assertEqual(read(p)[-len(big):], big)              # the file really did change
        with self.assertLogs("mangarr.library", "WARNING"):     # and checked afresh, it is refused
            self.assertEqual(verify_archive(p), (False, "more than 5000 entries (limit 5000)"))

    def test_end_record_flipped_while_checking(self):
        """The evidence repro: a thread writes the end record back and forth
        while verify_archive runs in a loop (it won 70 of 300 attempts)."""
        p = os.path.join(self.d, "flip.cbz")
        small, big = hidden_directory(p, 20000)
        at = os.path.getsize(p) - len(small)
        stop = threading.Event()

        def flip():
            fd = os.open(p, os.O_WRONLY)
            try:
                while not stop.is_set():
                    os.pwrite(fd, big, at)
                    os.pwrite(fd, small, at)
            finally:
                os.close(fd)
        seen, spy = self._spy_limits()
        results = set()
        interval = sys.getswitchinterval()
        t = threading.Thread(target=flip, daemon=True)
        with spy, mock.patch.object(library.log, "warning"):
            sys.setswitchinterval(1e-5)
            t.start()
            try:
                deadline = time.monotonic() + 20
                for _ in range(300):
                    results.add(verify_archive(p))
                    if time.monotonic() > deadline:
                        break
            finally:
                stop.set()
                t.join(10)
                sys.setswitchinterval(interval)
        self.assertTrue(seen)
        self.assertEqual(max(seen), 1, "zipfile parsed a directory that was not checked")
        self.assertIn((True, "1 pages"), results)
        self.assertEqual({detail for ok, detail in results if ok}, {"1 pages"})

    def test_unusual_zip64_layout_is_refused(self):
        p = build_zip(os.path.join(self.d, "z.cbz"), [("0.jpg", *stored(os.urandom(2000)))], zip64=True)
        data = bytearray(read(p))
        loc = data.rfind(b"PK\x06\x07")
        data[loc + 8:loc + 16] = (0).to_bytes(8, "little")     # the locator points somewhere else
        write(p, bytes(data))
        with self._no_parse():
            ok, detail = verify_archive(p)
        self.assertFalse(ok)
        self.assertIn("zip64", detail)

    def test_checking_is_time_bounded(self):
        p = make_cbz(os.path.join(self.d, "g.cbz"), 3)
        with mock.patch.object(library, "VERIFY_SECONDS", -1), self.assertLogs("mangarr.library", "WARNING"):
            ok, detail = verify_archive(p)
        self.assertFalse(ok)
        self.assertIn("longer than", detail)


class CopyFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = make_cbz(os.path.join(self.tmp.name, "a.cbz"), 5)
        self.lib = os.path.join(self.tmp.name, "lib")

    def tearDown(self):
        self.tmp.cleanup()

    def _link_fails(self, err):
        real = os.link

        def fake(src, dst, **kw):
            if not src.startswith(".mangarr-"):         # the staged file, by any name; not the final rename
                raise OSError(err, os.strerror(err), src, None, dst)
            return real(src, dst, **kw)
        return mock.patch.object(library.os, "link", fake)

    def test_other_errors_are_not_copied(self):
        with self._link_fails(errno.ENOSPC), self.assertRaises(OSError) as cm:
            link_into_library(self.src, "T", 1.0, root=self.lib)
        self.assertEqual(os.listdir(os.path.join(self.lib, "T")), [])
        self.assertEqual((cm.exception.filename, cm.exception.filename2),       # the real paths, not temp names
                         (self.src, os.path.join(self.lib, "T", "Chapter 001.0.cbz")))

    def test_cross_device_copies_atomically(self):
        with self._link_fails(errno.EXDEV), self.assertLogs("mangarr.library", "WARNING"):
            library._copy_warned = False
            dst = link_into_library(self.src, "T", 1.0, root=self.lib)
        self.assertEqual(read(dst), read(self.src))
        self.assertEqual(os.listdir(os.path.dirname(dst)), ["Chapter 001.0.cbz"])     # no temp file left

    def test_interrupted_copy_leaves_nothing(self):
        def broken(inp, out, *a):
            out.write(inp.read(100))
            raise OSError(errno.ENOSPC, "No space left on device")
        with self._link_fails(errno.EXDEV), mock.patch.object(library.shutil, "copyfileobj", broken), \
                self.assertRaises(OSError), self.assertLogs("mangarr.library", "WARNING"):
            link_into_library(self.src, "T", 1.0, root=self.lib)
        self.assertEqual(os.listdir(os.path.join(self.lib, "T")), [])


class QuarantinePruneTest(unittest.TestCase):
    def test_old_corrupt_files_are_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            fresh = quarantine(make_cbz(os.path.join(d, "Chapter 1.cbz")))
            old = quarantine(make_cbz(os.path.join(d, "Chapter 2.cbz")))
            age(old, (library.QUARANTINE_DAYS + 1) * 86400)
            keep = make_cbz(os.path.join(d, "Chapter 3.cbz"))
            age(keep, (library.QUARANTINE_DAYS + 1) * 86400)
            with self.assertLogs("mangarr.library", "INFO"):
                self.assertEqual(prune_quarantine(d), 1)
            self.assertTrue(os.path.exists(fresh) and os.path.exists(keep) and not os.path.exists(old))


class ImportBase(unittest.TestCase):
    """A real staging and library tree, and a database, for core.import_series."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.staging, self.lib = os.path.join(t, "staging"), os.path.join(t, "library")
        self.dbpath = os.path.join(t, "t.db")
        self.patches = [mock.patch.object(library.config, "STAGING_ROOT", self.staging),
                        mock.patch.object(library.config, "LIBRARY_ROOT", self.lib),
                        mock.patch.object(core.komga, "scan", lambda: False)]
        for p in self.patches:
            p.start()
        self.folder = os.path.join(self.staging, "Src (EN)", "Title")
        os.makedirs(self.folder)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _series(self, con, source_name="Src (EN)", title="Title", folder=None):
        sid = db.upsert_series(con, Series(anilist_id=1, english="Title"))
        con.execute("INSERT INTO series_source (series_id, manga_id, source_name, title, match_level, author_level,"
                    " chapter_count, max_chapter, folder, seen_at) VALUES (?,?,?,?,0,1,1,1,?,?)",
                    (sid, 7, source_name, title, folder, db.now()))
        return sid

    def _wanted(self, con, sid, *numbers):
        for n in numbers:
            con.execute("INSERT INTO chapter (series_id, number, status, updated_at) VALUES (?,?,'wanted',?)",
                        (sid, n, db.now()))


class ImportTest(ImportBase):
    """core.import_series against a real staging and library tree."""

    def test_one_bad_chapter_does_not_block_the_rest(self):
        for n in (1, 2, 3):
            age(make_cbz(os.path.join(self.folder, f"Chapter {n}.cbz")), 3600)
        with open(os.path.join(self.folder, "Chapter 4.cbz"), "wb") as f:     # corrupt and old
            f.write(b"PK" + b"x" * 3000)
        age(os.path.join(self.folder, "Chapter 4.cbz"), 3600)
        real = library.link_into_library

        def flaky(src, folder, n, **kw):
            if n == 2.0:
                raise OSError(errno.ENAMETOOLONG, "File name too long")
            return real(src, folder, n, **kw)
        with db.connect(self.dbpath) as con, mock.patch.object(library, "link_into_library", flaky), \
                self.assertLogs("mangarr.core", "WARNING"):
            sid = self._series(con)
            for n in (1, 2, 3, 4):
                con.execute("INSERT INTO chapter (series_id, number, status, updated_at) VALUES (?,?,'wanted',?)",
                            (sid, n, db.now()))
            linked = core.import_series(con, sid)
            rows = {r["number"]: r for r in db.chapters(con, sid)}
        self.assertEqual(linked, 2)
        self.assertEqual((rows[1.0]["status"], rows[3.0]["status"]), ("have", "have"))
        self.assertEqual(rows[2.0]["status"], "failed")
        self.assertIn("File name too long", rows[2.0]["reason"])
        self.assertEqual(rows[4.0]["status"], "failed")
        self.assertTrue(os.path.exists(os.path.join(self.folder, "Chapter 4.cbz.corrupt")))

    def test_file_still_being_written_is_left_alone(self):
        p = os.path.join(self.folder, "Chapter 1.cbz")
        with open(p, "wb") as f:
            f.write(b"PK\x03\x04" + b"x" * 3000)        # no central directory yet
        with db.connect(self.dbpath) as con, self.assertLogs("mangarr.core", "INFO") as cm:
            sid = self._series(con)
            self.assertEqual(core.import_series(con, sid), 0)
        self.assertTrue(os.path.exists(p))
        self.assertFalse(os.path.exists(p + ".corrupt"))
        self.assertIn("still being written", "\n".join(cm.output))

    def test_source_name_cannot_leave_staging(self):
        outside = os.path.join(self.tmp.name, "outside", "Title")
        os.makedirs(outside)
        age(make_cbz(os.path.join(outside, "Chapter 1.cbz")), 3600)
        with db.connect(self.dbpath) as con:
            sid = self._series(con, source_name="../outside")
            self.assertEqual(core.series_staging_dirs(con, sid), [])
            con.execute("UPDATE series_source SET folder=?", (outside,))           # e.g. a restored backup
            with self.assertLogs("mangarr.core", "WARNING"):
                self.assertEqual(core.series_staging_dirs(con, sid), [])
                self.assertEqual(core.import_series(con, sid), 0)
        self.assertEqual(os.listdir(outside), ["Chapter 1.cbz"])

    def test_source_name_is_sanitised_like_suwayomi(self):
        folder = os.path.join(self.staging, "Src_ Scans (EN)", "Title")
        os.makedirs(folder)
        with db.connect(self.dbpath) as con:
            sid = self._series(con, source_name="Src: Scans (EN)")
            self.assertEqual([f for _, f, _ in core.series_staging_dirs(con, sid)], [folder])


    def test_bzip2_page_is_set_aside_not_linked(self):
        """The evidence end to end: a 4 KB 'Chapter 1.cbz' holding bzip2 of
        zeros declared as a 1000-byte page was verified and linked."""
        zeros = b"\0" * (16 << 20)
        p = build_zip(os.path.join(self.folder, "Chapter 1.cbz"),
                      [("000.jpg", *stored(os.urandom(3000))),
                       ("001.jpg", bz2.compress(zeros), zipfile.ZIP_BZIP2, zlib.crc32(zeros[:1000]), 1000)])
        age(p, 3600)
        with db.connect(self.dbpath) as con, self.assertLogs("mangarr", "WARNING"):
            sid = self._series(con)
            self._wanted(con, sid, 1)
            self.assertEqual(core.import_series(con, sid), 0)
            row = db.chapters(con, sid)[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("compressed with bzip2", row["reason"])
        self.assertTrue(os.path.exists(p + ".corrupt"))
        self.assertFalse(os.path.exists(os.path.join(self.lib, "Title")) and os.listdir(os.path.join(self.lib, "Title")))

    def test_bad_file_that_cannot_be_set_aside_does_not_block_the_rest(self):
        with open(os.path.join(self.folder, "Chapter 1.cbz"), "wb") as f:     # corrupt and old
            f.write(b"PK" + b"x" * 3000)
        age(os.path.join(self.folder, "Chapter 1.cbz"), 3600)
        age(make_cbz(os.path.join(self.folder, "Chapter 2.cbz")), 3600)

        def refuse(path):
            raise OSError(errno.EACCES, "Permission denied", path)
        with db.connect(self.dbpath) as con, mock.patch.object(library, "quarantine", refuse), \
                self.assertLogs("mangarr.core", "ERROR"):
            sid = self._series(con)
            for n in (1, 2):
                con.execute("INSERT INTO chapter (series_id, number, status, updated_at) VALUES (?,?,'wanted',?)",
                            (sid, n, db.now()))
            self.assertEqual(core.import_series(con, sid), 1)
            rows = {r["number"]: r for r in db.chapters(con, sid)}
        self.assertEqual(rows[1.0]["status"], "failed")
        self.assertIn("not set aside", rows[1.0]["reason"])
        self.assertEqual(rows[2.0]["status"], "have")


class LinkTest(ImportBase):
    """What reaches the library is the open file that was checked, on any
    filesystem, and a failure says which folders were involved."""

    def test_link_does_not_depend_on_inode_numbers(self):
        """sshfs and other FUSE mounts without use_ino, or CIFS with
        noserverino, number each path on its own: a new hard link does not
        show the inode of the open file it was made from."""
        real = library._lstat_at

        def own_numbers(name, dir_fd):
            st = real(name, dir_fd)
            if st is None:
                return None
            fields = list(st)
            fields[1] += 1_000_000                              # st_ino
            return os.stat_result(fields)
        staged = os.path.join(self.folder, "Chapter 1.cbz")
        dst = os.path.join(self.lib, "Title", "Chapter 001.0.cbz")
        for i, proc in enumerate((library._PROC_FD, os.path.join(self.tmp.name, "no-proc"))):     # Linux, elsewhere
            with self.subTest(proc=proc):
                age(make_cbz(staged), 3600)
                with db.connect(os.path.join(self.tmp.name, f"{i}.db")) as con, \
                        mock.patch.object(library, "_lstat_at", own_numbers), \
                        mock.patch.object(library, "_PROC_FD", proc), self.assertLogs("mangarr", "INFO") as cm:
                    sid = self._series(con)
                    self._wanted(con, sid, 1)
                    self.assertEqual(core.import_series(con, sid), 1)      # was: failed, "the staged file was replaced"
                    self.assertEqual(db.chapters(con, sid)[0]["status"], "have")
                self.assertEqual(read(dst), read(staged))
                warned = [r.getMessage() for r in cm.records if r.levelno >= logging.WARNING]
                if proc == library._PROC_FD:
                    self.assertTrue(os.path.samefile(staged, dst))       # the open file itself, hard-linked
                    self.assertEqual(warned, [])
                else:
                    self.assertIn("copying the checked file instead", "\n".join(warned))
                os.remove(dst)

    @unittest.skipIf(os.geteuid() == 0, "root writes through a read-only folder")
    def test_link_failure_names_the_folders(self):
        staged = os.path.join(self.folder, "Chapter 4.cbz")
        age(make_cbz(staged), 3600)
        lib = os.path.join(self.lib, "Title")
        os.makedirs(lib)
        os.chmod(lib, 0o555)                                    # the usual PUID/PGID mistake
        try:
            with db.connect(self.dbpath) as con, self.assertLogs("mangarr.core", "ERROR") as cm:
                sid = self._series(con)
                self._wanted(con, sid, 4)
                self.assertEqual(core.import_series(con, sid), 0)
                row = db.chapters(con, sid)[0]
        finally:
            os.chmod(lib, 0o755)
        where = f"'{staged}' -> '{os.path.join(lib, 'Chapter 004.0.cbz')}'"
        self.assertEqual(row["status"], "failed")
        self.assertIn(f"cannot link Chapter 4.cbz: PermissionError: [Errno 13] Permission denied: {where}",
                      row["reason"])                            # was: 'Chapter 4.cbz' -> '.mangarr-....part'
        self.assertIn(where, "\n".join(cm.output))
        self.assertEqual(os.listdir(lib), [])


class StagingSwapTest(ImportBase):
    """A staging writer swaps the series folder, or a file in it, for a symlink
    while import runs (finding 113). Import works through the folder it
    opened, so nothing outside it is ever checked, set aside or linked, and a
    swap costs at most that one chapter, never the series."""

    def setUp(self):
        super().setUp()
        self.victim = os.path.join(self.lib, "Victim")          # another series' library folder
        os.makedirs(self.victim)
        age(make_cbz(os.path.join(self.victim, "Chapter 001.0.cbz")), 3600)
        bad = os.path.join(self.victim, "Chapter 002.0.cbz")    # would be set aside if it were ever checked
        write(bad, b"PK" + b"x" * 3000)
        age(bad, 3600)
        self.before = self._victim()

    def _victim(self):
        return sorted((e.name, e.inode(), e.stat(follow_symlinks=False).st_size) for e in os.scandir(self.victim))

    def _bad_staged(self, name="Chapter 001.0.cbz"):
        p = os.path.join(self.folder, name)
        write(p, b"PK" + b"x" * 3000)                          # corrupt and old: set aside when checked
        age(p, 3600)
        return p

    def _swap_folder(self):
        os.rename(self.folder, self.folder + ".real")
        os.symlink(self.victim, self.folder)

    def test_folder_swapped_after_the_check(self):
        self._bad_staged()
        real = library.verify_archive

        def verify_then_swap(f):
            res = real(f)
            self._swap_folder()                                 # the evidence repro, made deterministic
            return res
        with db.connect(self.dbpath) as con, mock.patch.object(library, "verify_archive", verify_then_swap), \
                self.assertLogs("mangarr.core", "WARNING"):
            sid = self._series(con)
            self.assertEqual(core.import_series(con, sid), 0)
        self.assertEqual(self._victim(), self.before)
        self.assertTrue(os.path.exists(os.path.join(self.folder + ".real", "Chapter 001.0.cbz.corrupt")))

    def test_folder_swapped_before_it_is_opened(self):
        self._bad_staged()
        real_dirs = core.series_staging_dirs

        def listed_then_swapped(con, sid):
            dirs = real_dirs(con, sid)
            self._swap_folder()
            return dirs
        with db.connect(self.dbpath) as con, mock.patch.object(core, "series_staging_dirs", listed_then_swapped), \
                self.assertLogs("mangarr.core", "WARNING") as cm:
            sid = self._series(con)
            self.assertEqual(core.import_series(con, sid), 0)
        self.assertEqual(self._victim(), self.before)
        self.assertIn("skipping staging folder", "\n".join(cm.output))
        self.assertEqual(os.listdir(self.lib), ["Victim"])

    def test_folder_above_swapped_is_refused(self):
        os.rename(os.path.join(self.staging, "Src (EN)"), os.path.join(self.staging, "moved"))
        os.symlink(os.path.join(self.staging, "moved"), os.path.join(self.staging, "Src (EN)"))
        with self.assertRaises(OSError):
            library.StagingFolder(self.folder)
        with self.assertRaises(OSError):                        # '..' never leaves the root either
            library.StagingFolder(os.path.join(self.staging, "..", "library", "Victim"))

    def test_file_swapped_for_a_symlink_fails_that_chapter_only(self):
        age(make_cbz(os.path.join(self.folder, "Chapter 1.cbz")), 3600)
        age(make_cbz(os.path.join(self.folder, "Chapter 2.cbz")), 3600)
        real_scan = library.StagingFolder.scan

        def scan_then_swap(sf):
            found = real_scan(sf)
            os.remove(os.path.join(self.folder, "Chapter 1.cbz"))
            os.symlink(os.path.join(self.victim, "Chapter 001.0.cbz"), os.path.join(self.folder, "Chapter 1.cbz"))
            return found
        with db.connect(self.dbpath) as con, mock.patch.object(library.StagingFolder, "scan", scan_then_swap), \
                self.assertLogs("mangarr.core", "ERROR"):
            sid = self._series(con)
            self._wanted(con, sid, 1, 2)
            self.assertEqual(core.import_series(con, sid), 1)
            rows = {r["number"]: r for r in db.chapters(con, sid)}
        self.assertEqual(rows[1.0]["status"], "failed")
        self.assertIn("symlink", rows[1.0]["reason"])
        self.assertEqual(rows[2.0]["status"], "have")
        self.assertEqual(self._victim(), self.before)
        self.assertTrue(os.path.islink(os.path.join(self.folder, "Chapter 1.cbz")))     # not set aside either

    def test_file_swapped_after_the_check_links_the_checked_file(self):
        p = os.path.join(self.folder, "Chapter 1.cbz")
        lib = os.path.join(self.lib, "Title")
        real = library.verify_archive
        runs = 0
        for keep_old in (True, False):
            for proc in (library._PROC_FD, os.path.join(self.tmp.name, "no-proc")):      # Linux, and elsewhere
                with self.subTest(proc=proc, keep_old=keep_old):
                    age(make_cbz(p), 3600)
                    checked = read(p)
                    other = make_cbz(os.path.join(self.tmp.name, "other.cbz"))

                    def verify_then_swap(f, keep_old=keep_old, other=other):
                        res = real(f)
                        if keep_old:
                            os.rename(p, p + ".old")
                        os.replace(other, p)                    # a different, unchecked file under the name
                        return res
                    runs += 1
                    with db.connect(os.path.join(self.tmp.name, f"{runs}.db")) as con, \
                            mock.patch.object(library, "verify_archive", verify_then_swap), \
                            mock.patch.object(library, "_PROC_FD", proc), self.assertLogs("mangarr", "INFO") as cm:
                        sid = self._series(con)
                        self._wanted(con, sid, 1)
                        linked = core.import_series(con, sid)
                        status = db.chapters(con, sid)[0]["status"]
                    self.assertEqual([r.getMessage() for r in cm.records if r.levelno >= logging.ERROR], [])
                    if linked:
                        self.assertEqual(status, "have")
                        self.assertEqual(os.listdir(lib), ["Chapter 001.0.cbz"])        # no temp file left
                        self.assertEqual(read(os.path.join(lib, "Chapter 001.0.cbz")), checked)
                        os.remove(os.path.join(lib, "Chapter 001.0.cbz"))
                    else:
                        # the checked file was deleted by the swap: through /proc it cannot be
                        # linked any more, so the chapter waits for the next import
                        self.assertEqual((keep_old, proc, status), (False, library._PROC_FD, "wanted"))
                        self.assertEqual(os.listdir(lib), [])

    def test_file_renamed_away_mid_import_skips_that_chapter(self):
        self._bad_staged("Chapter 1.cbz")
        age(make_cbz(os.path.join(self.folder, "Chapter 2.cbz")), 3600)
        real = library.verify_archive

        def verify_then_rename(f):
            res = real(f)
            if f.name == "Chapter 1.cbz":
                os.rename(f.path, f.path + ".gone")
            return res
        with db.connect(self.dbpath) as con, mock.patch.object(library, "verify_archive", verify_then_rename), \
                self.assertLogs("mangarr.core", "WARNING") as cm:
            sid = self._series(con)
            self._wanted(con, sid, 1, 2)
            self.assertEqual(core.import_series(con, sid), 1)
            rows = {r["number"]: r for r in db.chapters(con, sid)}
        self.assertIn("disappeared", "\n".join(cm.output))
        self.assertEqual((rows[1.0]["status"], rows[2.0]["status"]), ("wanted", "have"))

    def test_is_within_does_not_raise_when_a_link_changes_under_it(self):
        with mock.patch.object(library.os.path, "realpath", side_effect=FileNotFoundError(2, "gone")):
            self.assertFalse(library.is_within(self.folder, self.staging))

    def test_folder_flipped_while_importing_in_a_loop(self):
        """The evidence repro: a thread flips the folder between the real one
        and a symlink into the library while import runs over and over."""
        template = os.path.join(self.tmp.name, "bad.cbz")
        write(template, b"PK" + b"x" * 3000)
        name = "Chapter 001.0.cbz"
        real_fd = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY)     # the real folder, wherever it is
        self.addCleanup(os.close, real_fd)

        def restage():                                          # put the bad file back once it was set aside
            try:
                os.stat(name, dir_fd=real_fd, follow_symlinks=False)
                return
            except FileNotFoundError:
                pass
            try:
                os.unlink(name + ".corrupt", dir_fd=real_fd)
            except FileNotFoundError:
                pass
            os.link(template, name, dst_dir_fd=real_fd)       # a link, not a rewrite: keeps each round cheap
            age(template, 3600)                                 # quarantine touched it
        restage()
        real, link = self.folder + ".real", self.folder + ".link"
        os.symlink(self.victim, link)
        stop = threading.Event()

        def flip():
            while not stop.is_set():
                for a, b in ((self.folder, real), (link, self.folder), (self.folder, link), (real, self.folder)):
                    try:
                        os.rename(a, b)
                    except OSError:
                        pass
        counts = {"opened": 0, "refused": 0}
        real_cls = library.StagingFolder

        class Counting(real_cls):
            def __init__(self, *a, **kw):
                try:
                    super().__init__(*a, **kw)
                except OSError:
                    counts["refused"] += 1
                    raise
                counts["opened"] += 1
        logger = logging.getLogger("mangarr")
        level, interval = logger.level, sys.getswitchinterval()
        errors: list = []
        names = [n for n, _, _ in self.before]
        with db.connect(self.dbpath) as con, mock.patch.object(library, "StagingFolder", Counting):
            sid = self._series(con, folder=self.folder)
            con.commit()
            t = threading.Thread(target=flip, daemon=True)
            logger.setLevel(logging.CRITICAL)                   # thousands of expected warnings
            sys.setswitchinterval(1e-5)                         # switch threads often, so the race is hit
            t.start()
            try:
                for i in range(20000):
                    try:
                        core.import_series(con, sid)
                    except Exception as e:                      # was: FileNotFoundError out of realpath
                        errors.append(f"round {i}: {type(e).__name__}: {e}")
                    if sorted(os.listdir(self.victim)) != names:
                        break
                    restage()
            finally:
                stop.set()
                t.join(10)
                sys.setswitchinterval(interval)
                logger.setLevel(level)
            corrupt = con.execute("SELECT corrupt FROM source_stats").fetchone()
        self.assertFalse(t.is_alive())
        self.assertEqual(self._victim(), self.before, f"library files changed in round {i}")
        self.assertEqual(errors[:3], [])
        self.assertEqual(sorted(os.listdir(self.lib)), ["Victim"])                 # nothing linked from it
        self.assertTrue(corrupt and corrupt[0] > 0 and counts["opened"] > 0, (corrupt, counts))


class AdoptEntriesTest(unittest.TestCase):
    def test_source_display_name_is_keyed_like_its_staging_folder(self):
        # Suwayomi writes "Src: Scans (EN)" downloads to a "Src_ Scans (EN)" folder
        nodes = [{"id": 7, "title": "Title: Two", "downloadCount": 3, "source": {"displayName": "Src: Scans (EN)"}},
                 {"id": 8, "title": "Other", "downloadCount": 0, "source": {"displayName": "Src (EN)"}}]

        class Client:
            def gq(self, query, timeout=None):
                return {"mangas": {"nodes": nodes}}

            def mangas_page(self, offset, limit):         # the paged listing (jobs branch)
                return nodes[offset:offset + limit], False
        entries = core.suwayomi_downloaded_entries(Client())
        self.assertEqual(entries, {("Src_ Scans (EN)", "Title_ Two"): 7})


if __name__ == "__main__":
    unittest.main()
