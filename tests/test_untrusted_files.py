"""The staging tree is written by Suwayomi and its extensions and the names
come from scraped sites: names fit the filesystem, symlinks are never
followed, bad archives are set aside instead of breaking the import, and
one bad chapter never blocks the rest of the series."""
import errno
import os
import random
import tempfile
import time
import unittest
import zipfile
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
            if src == self.src:
                raise OSError(err, os.strerror(err))
            return real(src, dst, **kw)
        return mock.patch.object(library.os, "link", fake)

    def test_other_errors_are_not_copied(self):
        with self._link_fails(errno.ENOSPC), self.assertRaises(OSError):
            link_into_library(self.src, "T", 1.0, root=self.lib)
        self.assertEqual(os.listdir(os.path.join(self.lib, "T")), [])

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


class ImportTest(unittest.TestCase):
    """core.import_series against a real staging and library tree."""

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


class AdoptEntriesTest(unittest.TestCase):
    def test_source_display_name_is_keyed_like_its_staging_folder(self):
        # Suwayomi writes "Src: Scans (EN)" downloads to a "Src_ Scans (EN)" folder
        class Client:
            def gq(self, query, timeout=None):
                return {"mangas": {"nodes": [
                    {"id": 7, "title": "Title: Two", "downloadCount": 3, "source": {"displayName": "Src: Scans (EN)"}},
                    {"id": 8, "title": "Other", "downloadCount": 0, "source": {"displayName": "Src (EN)"}}]}}
        entries = core.suwayomi_downloaded_entries(Client())
        self.assertEqual(entries, {("Src_ Scans (EN)", "Title_ Two"): 7})


if __name__ == "__main__":
    unittest.main()
