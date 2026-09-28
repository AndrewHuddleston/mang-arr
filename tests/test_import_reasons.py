"""What the import says about a chapter Suwayomi reports downloaded that no
staged file is read as: 0.3.0 left such chapters "waiting for a download
pass" while every pass had nothing to download (Dreaming Freedom
171.01-171.13). The trees are real (temporary) staging and library folders
with fake chapter files; Suwayomi and Komga are fakes. Tree and the fakes
are shared with test_relink."""
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from mangarr import config, core, db, komga, library, settings
from mangarr.model import Series
from mangarr.resolver import Plan, SourceMatch
from mangarr.suwayomi import Chapter, Source, SuwayomiUnreachable

MN, NATO = "Manganato (EN)", "www.natomanga.com"


def make_cbz(path: str) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("000.jpg", os.urandom(2000))
    return path


def inode(path: str) -> tuple[int, int]:
    st = os.lstat(path)
    return st.st_dev, st.st_ino


class FakeSuwayomi:
    """Suwayomi's cached chapter lists: {manga id: [Chapter]}; `down` makes
    every call fail."""

    def __init__(self, lists: dict | None = None):
        self.lists, self.down, self.asked = dict(lists or {}), False, []

    def chapters(self, manga_id):
        self.asked.append(manga_id)
        if self.down:
            raise SuwayomiUnreachable("Suwayomi at http://fake unreachable: connection refused")
        return list(self.lists.get(manga_id, []))


class Tree(unittest.TestCase):
    """A temporary database, staging and library, Komga faked."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.staging, self.lib = os.path.join(tmp.name, "staging"), os.path.join(tmp.name, "library")
        self.scans = []
        for p in (mock.patch.object(config, "DATA_DIR", os.path.join(tmp.name, "data")),     # backups go there
                  mock.patch.object(config, "DB_PATH", os.path.join(tmp.name, "t.db")),
                  mock.patch.object(config, "STAGING_ROOT", self.staging),
                  mock.patch.object(config, "LIBRARY_ROOT", self.lib),
                  mock.patch.object(komga, "scan", lambda *a: self.scans.append(a) or True)):
            p.start()
            self.addCleanup(p.stop)
        settings._cache.clear()
        self.addCleanup(settings._cache.clear)
        self.addCleanup(komga.take_retry)          # no Komga retry left waiting for the next test
        self.ids = {}

    def series(self, con, title: str, anilist_id: int) -> int:
        sid = db.upsert_series(con, Series(anilist_id=anilist_id, english=title))
        self.ids[title] = sid
        return sid

    def source(self, con, sid: int, source: str, folder: str, manga_id: int, files) -> str:
        """A source entry of the series and its staging folder with these files."""
        path = os.path.join(self.staging, source, folder)
        os.makedirs(path, exist_ok=True)
        con.execute("INSERT INTO series_source (series_id, manga_id, source_name, title, match_level, author_level,"
                    " chapter_count, max_chapter, folder, seen_at) VALUES (?,?,?,?,0,1,1,1,?,?)",
                    (sid, manga_id, source, folder, path, db.now()))
        for name in files:
            make_cbz(os.path.join(path, name))
        return path

    def link_as(self, con, sid: int, staged: str, number: float, source: str, label: str | None = None) -> str:
        """Link a staged file under `number`, as 0.3.0's import did."""
        folder = db.get_series(con, sid)["folder"]
        dst = library.link_into_library(staged, folder, number, label=label)
        db.set_have(con, sid, number, staged, dst, source)
        return dst

    def lib_path(self, con, sid: int, name: str) -> str:
        return os.path.join(self.lib, db.get_series(con, sid)["folder"], name)

    def rows(self, con, sid: int) -> dict:
        return {r["number"]: dict(r) for r in db.chapters(con, sid)}

    def events(self, con, kind: str) -> list[str]:
        return [r["message"] for r in con.execute("SELECT message FROM event WHERE kind=? ORDER BY id", (kind,))]

    def staging_state(self) -> dict:
        out = {}
        for d, _, files in os.walk(self.staging):
            for f in files:
                p = os.path.join(d, f)
                out[p] = inode(p)
        return out


class MissingFileReasonTest(Tree):
    """A chapter Suwayomi reports downloaded that no staged file is read as
    says why it is not in the library, instead of waiting for a download
    pass that has nothing to download."""

    def setup_series(self, con, files, statuses=None):
        sid = self.series(con, "Dreaming Freedom", 4)
        self.source(con, sid, MN, "Dreaming Freedom", 41, files)
        for n, st in (statuses or {171.01: "wanted"}).items():
            con.execute("INSERT INTO chapter (series_id, number, status, manga_id, source_name, reason, updated_at)"
                        " VALUES (?,?,?,41,?,?,?)", (sid, n, st, MN, f"available on {MN}; not downloaded yet - "
                                                    "waiting for a download pass", db.now()))
        con.commit()
        return sid

    def reason(self, con, sid, n=171.01):
        return self.rows(con, sid)[n]["reason"]

    def test_an_unreadable_file(self):
        with db.connect() as con:
            sid = self.setup_series(con, [f"{NATO}_Spin-off one.cbz"])
            with self.assertLogs("mangarr.core", "WARNING"):
                core.import_series(con, sid, FakeSuwayomi({41: []}), downloaded={171.01: MN})
            self.assertEqual(self.reason(con, sid), f"{MN}: downloaded as {NATO}_Spin-off one.cbz but its chapter "
                                                    "number could not be read; rename it or report the name")

    def test_several_unreadable_files(self):
        with db.connect() as con:
            sid = self.setup_series(con, [f"{NATO}_Spin-off one.cbz", f"{NATO}_Spin-off two.cbz"])
            with self.assertLogs("mangarr.core", "WARNING"):
                core.import_series(con, sid, FakeSuwayomi({41: []}), downloaded={171.01: MN})
            self.assertEqual(self.reason(con, sid), f"{MN}: downloaded, but its file is one of 2 whose chapter number "
                                                    f"could not be read ({NATO}_Spin-off one.cbz, {NATO}_Spin-off "
                                                    "two.cbz); rename it or report the name")

    def test_files_read_as_a_number_another_file_has(self):
        """0.3.0's Dreaming Freedom: the spin-off files read as chapters on disk."""
        with db.connect() as con:
            sid = self.setup_series(con, [f"{NATO}_Chapter 1.cbz", "Other_Chapter 1 - Spin-off 1.cbz"])
            with self.assertLogs("mangarr.core", "WARNING"):
                core.import_series(con, sid, FakeSuwayomi({41: []}), downloaded={171.01: MN})
            self.assertEqual(self.reason(con, sid), f"{MN}: reported downloaded, but no file reads as chapter 171.01; "
                                                    f"{NATO}_Chapter 1.cbz read as chapter 1, which Other_Chapter 1 - "
                                                    "Spin-off 1.cbz already is: rename it or report the name")

    def test_no_file_at_all_and_a_failed_one_is_wanted_again(self):
        with db.connect() as con:
            sid = self.setup_series(con, [], {171.01: "wanted", 171.02: "failed"})
            with self.assertLogs("mangarr.core", "WARNING") as cm:
                core.import_series(con, sid, None, downloaded={171.01: MN, 171.02: MN})
            rows = self.rows(con, sid)
            self.assertEqual(rows[171.02]["status"], "wanted")
            for n in (171.01, 171.02):
                self.assertEqual(rows[n]["reason"], f"{MN}: reported downloaded, but no file in its download folder "
                                                    f"reads as chapter {n:g}; delete the download in Suwayomi to "
                                                    "fetch it again")
            self.assertIn("171.01, 171.02", "\n".join(cm.output))
            with self.assertNoLogs("mangarr.core", "WARNING"):          # said once, not every pass
                core.import_series(con, sid, None, downloaded={171.01: MN, 171.02: MN})

    def test_nothing_is_said_while_the_names_could_not_be_asked(self):
        with db.connect() as con:
            sid = self.setup_series(con, [f"{NATO}_Spin-off one.cbz"])
            fake = FakeSuwayomi()
            fake.down = True
            with self.assertLogs("mangarr.core", "WARNING"):
                core.import_series(con, sid, fake, downloaded={171.01: MN})
            self.assertIn("waiting for a download pass", self.reason(con, sid))

    def test_a_linked_chapter_or_one_you_skipped_is_left_alone(self):
        with db.connect() as con:
            sid = self.setup_series(con, [f"{NATO}_Chapter 171.01_ Spin-off 1.cbz"], {171.01: "wanted",
                                                                                     171.02: "ignored"})
            core.import_series(con, sid, None, downloaded={171.01: MN, 171.02: MN})
            rows = self.rows(con, sid)
            self.assertEqual(rows[171.01]["status"], "have")
            self.assertEqual((rows[171.02]["status"], rows[171.02]["reason"]),
                             ("ignored", f"available on {MN}; not downloaded yet - waiting for a download pass"))

    def test_a_file_suwayomi_matched_to_a_number_another_file_has(self):
        """A season episode Suwayomi numbers 5, next to the file read as 5:
        it is said to be a duplicate, not unreadable."""
        wc = "Weeb Central (EN)"
        with db.connect() as con:
            sid = self.series(con, "Wind Breaker", 7)
            self.source(con, sid, wc, "Wind Breaker", 71, ["Official_Chapter 5.cbz", "Official_S1 - Episode 5.cbz"])
            con.execute("INSERT INTO chapter (series_id, number, status, updated_at) VALUES (?, 7, 'wanted', ?)",
                        (sid, db.now()))
            con.commit()
            fake = FakeSuwayomi({71: [Chapter(1, 5.0, "Chapter 5", "Official", True),
                                      Chapter(2, 5.0, "S1 - Episode 5", "Official", True),
                                      Chapter(3, 7.0, "Chapter 7", "Official", True)]})
            with self.assertLogs("mangarr.core", "WARNING"):
                core.import_series(con, sid, fake, downloaded={7.0: wc})
            self.assertEqual(self.reason(con, sid, 7.0), f"{wc}: reported downloaded, but no file reads as chapter 7; "
                                                         "Official_S1 - Episode 5.cbz read as chapter 5, which "
                                                         "Official_Chapter 5.cbz already is: rename it or report the "
                                                         "name")

    def test_several_files_read_as_numbers_other_files_have(self):
        with db.connect() as con:
            sid = self.setup_series(con, [f"{NATO}_Chapter 1.cbz", "Other_Chapter 1 - Spin-off 1.cbz",
                                          f"{NATO}_Chapter 2.cbz", "Other_Chapter 2 - Spin-off 2.cbz"])
            with self.assertLogs("mangarr.core", "WARNING"):
                core.import_series(con, sid, FakeSuwayomi({41: []}), downloaded={171.01: MN})
            self.assertEqual(self.reason(con, sid), f"{MN}: reported downloaded, but no file reads as chapter 171.01; "
                                                    f"2 files read as a chapter another file already is ({NATO}_"
                                                    "Chapter 1.cbz read as chapter 1, which Other_Chapter 1 - Spin-off"
                                                    " 1.cbz already is, ...): rename them or report the names")

    def test_a_refresh_says_it(self):
        """Through add_series: the plan's Suwayomi-downloaded chapters reach the import."""
        with db.connect() as con:
            sid = self.setup_series(con, [f"{NATO}_Chapter 171.cbz"])
            series = db.series_to_model(db.get_series(con, sid))
            chapters = [Chapter(41001, 171.0, "Chapter 171", NATO, True),
                        Chapter(41002, 171.01, "Chapter 171.01: Spin-off 1", NATO, True)]
            match = SourceMatch(Source("41", MN, "en"), 41, "Dreaming Freedom", None, 0, "Dreaming Freedom", 1,
                                chapters)
            plan = Plan(series, [match], [], [], {171.0: match, 171.01: match},
                        candidates={171.0: [match], 171.01: [match]})
            fake = FakeSuwayomi({41: chapters})
            with mock.patch.object(core, "resolve", lambda *a, **k: plan), \
                    mock.patch.object(core, "_set_library_entries", lambda *a, **k: None), \
                    self.assertLogs("mangarr.core", "WARNING"):
                core.add_series(con, fake, series, download=False, series_id=sid)
            rows = self.rows(con, sid)
            self.assertEqual(rows[171.0]["status"], "have")
            self.assertEqual(rows[171.01]["status"], "wanted")
            self.assertTrue(rows[171.01]["reason"].startswith(f"{MN}: reported downloaded, but no file in its "
                                                              "download folder reads as chapter 171.01"),
                            rows[171.01]["reason"])


if __name__ == "__main__":
    unittest.main()
