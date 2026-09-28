"""The rename preview (renamer.plan): what each chapter's file would be
called under the naming formats, what stops a rename (a name two chapters
or another file would share), names that sort out of order, and what Komga
would do with reading progress. It changes nothing: no file, no database
row. Komga is faked (fake_komga.py): nothing reaches a real one."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_komga import FakeKomga  # noqa: E402

from mangarr import db, komga, library, naming, renamer, settings  # noqa: E402
from mangarr.model import Series  # noqa: E402


class PlanBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.library = os.path.join(tmp.name, "library")
        self.staging = os.path.join(tmp.name, "staging")
        os.makedirs(self.library)
        self.komga = FakeKomga()
        for p in (mock.patch("mangarr.config.DB_PATH", os.path.join(tmp.name, "t.db")),
                  mock.patch("mangarr.config.LIBRARY_ROOT", self.library),
                  mock.patch("mangarr.config.STAGING_ROOT", self.staging),
                  mock.patch.object(komga, "_call", self.komga.call)):
            p.start()
            self.addCleanup(p.stop)
        settings._cache.clear()
        self.addCleanup(settings._cache.clear)

    def add_series(self, chapters, anilist_id=97994, english="Yakuza Fiancé: Raise wa Tanin ga Ii",
                   romaji="Raise wa Tanin ga Ii", year=2017) -> int:
        """chapters: (number, the source's name now, the label the file got) - or a file name as the
        third item, for a file named some other way. Each file is a hard link to a staged file."""
        with db.connect() as con:
            sid = db.upsert_series(con, Series(anilist_id=anilist_id, english=english, romaji=romaji, year=year))
            folder = db.get_series(con, sid)["folder"]
            d = library.library_dir(folder)
            os.makedirs(d, exist_ok=True)
            os.makedirs(self.staging, exist_ok=True)
            for n, name, label in chapters:
                file = label if isinstance(label, str) and label.endswith(".cbz") else library.chapter_filename(n, label)
                staged = os.path.join(self.staging, f"{anilist_id}-{n}.cbz")
                with open(staged, "wb") as f:
                    f.write(f"chapter {n}".encode())
                os.link(staged, os.path.join(d, file))
                db.set_have(con, sid, n, staged, os.path.join(d, file), "Source")
                con.execute("UPDATE chapter SET name=? WHERE series_id=? AND number=?", (name, sid, n))
        return sid

    def plan(self, sid, formats=None, **kw):
        with db.connect() as con:
            return renamer.plan(con, sid, formats, **kw)

    def folder(self, sid) -> str:
        with db.connect() as con:
            return library.library_dir(db.get_series(con, sid)["folder"])

    def names(self, p) -> dict:
        return {c["number"]: c["new_name"] for c in p["chapters"]}

    def komga_on(self, library_id="LIB1"):
        with db.connect() as con:
            settings.set_many(con, {"komga_url": "http://komga:25600", "komga_api_key": "k",
                                    "komga_library_id": library_id})


# the library as it is: files linked before a title was known, titles the source has edited
# since, long titles cut to fit, NFD, and numbers from 0.99 to 1000
MIXED = [
    (0.99, None, None),
    (1.0, "Chapter 1", None),
    (12.0, "Vol.1 chapter 12", None),                                   # title learned after the file was linked
    (12.25, "Chapter 12.25: Side", "Chapter 12.25: Side"),
    (12.5, "Chapter 12.5: Extra: The Daily Life", "Chapter 12.5: Extra: The Daily Life"),
    (13.0, "The New Title", "The Old Title"),                           # edited at the source since
    (14.0, "第" * 90, "第" * 90),                                          # cut to fit 255 bytes, with a hash tag
    (15.0, "a" * 79 + " b", "a" * 79 + " b"),                            # cut at 80, ending in a space
    (16.0, "Café au lait", "Café au lait"),                   # NFD, kept so
    (131.0, "S2 - Episode 5", "S2 - Episode 5"),
    (1000.0, None, None),
]


class DefaultsTest(PlanBase):
    def test_upgrading_renames_nothing(self):
        sid = self.add_series(MIXED)
        p = self.plan(sid)
        self.assertEqual(len(p["chapters"]), len(MIXED))
        self.assertEqual([c["skip"] for c in p["chapters"]], [None] * len(MIXED))
        self.assertEqual([c["new_name"] for c in p["chapters"]], [c["old_name"] for c in p["chapters"]])
        self.assertEqual(p["renames"], 0)
        self.assertEqual((p["collisions"], p["warnings"], p["komga"]), ([], [], None))
        self.assertEqual(p["folder"], {"old": "Yakuza Fiancé_ Raise wa Tanin ga Ii",
                                       "new": "Yakuza Fiancé_ Raise wa Tanin ga Ii", "changed": False, "blocked": None})
        self.assertEqual(self.komga.calls, [])                  # nothing to rename: Komga is not asked
        c = {x["number"]: x for x in p["chapters"]}
        self.assertEqual(c[12.0]["title"], None)
        self.assertEqual(c[13.0]["title"], "The Old Title")
        self.assertEqual(c[12.5]["title"], "Extra_ The Daily Life")
        self.assertEqual({x["title_from"] for x in p["chapters"]}, {"file"})
        self.assertRegex(c[14.0]["old_name"], r"~[0-9a-f]{8}\.cbz$")
        self.assertEqual(json.loads(json.dumps(p)), p)          # ready for the API as it is

    def test_latest_titles(self):
        sid = self.add_series(MIXED)
        p = self.plan(sid, use_latest_titles=True)
        changed = {c["number"]: c["new_name"] for c in p["chapters"] if c["changed"]}
        self.assertEqual(changed, {12.0: "Chapter 012.0 - Vol.1 chapter 12.cbz",
                                   13.0: "Chapter 013.0 - The New Title.cbz"})
        self.assertEqual(p["renames"], 2)
        self.assertEqual({c["number"] for c in p["chapters"] if c["title_from"] != "source"}, {0.99, 1000.0})  # no name
        c13 = next(c for c in p["chapters"] if c["number"] == 13.0)
        self.assertEqual(c13["new_path"], os.path.join(self.folder(sid), "Chapter 013.0 - The New Title.cbz"))
        self.assertEqual(c13["old_path"], os.path.join(self.folder(sid), "Chapter 013.0 - The Old Title.cbz"))

    def test_latest_titles_keep_a_title_no_source_name_replaces(self):
        sid = self.add_series([(3.0, "Chapter 3: The Storm", "Chapter 3: The Storm")])
        with db.connect() as con:
            con.execute("UPDATE chapter SET name=NULL WHERE series_id=?", (sid,))
        p = self.plan(sid, use_latest_titles=True)
        self.assertEqual((self.names(p), p["chapters"][0]["title_from"]), ({3.0: "Chapter 003.0 - The Storm.cbz"}, "file"))

    def test_nothing_is_written(self):
        sid = self.add_series(MIXED)
        before_files, before_rows = self.snapshot(), self.rows()
        with db.connect() as con:
            changes = con.total_changes
            fmt = {"series_folder_format": "{Series Romaji}{ (Year)}", "colon_replacement": "dash",
                   "chapter_file_format": "{Series Title} - Chapter {Chapter:000}{ - Chapter Title}"}
            p = renamer.plan(con, sid, fmt, use_latest_titles=True)
            self.assertEqual(con.total_changes, changes)
            self.assertFalse(con.in_transaction)
        self.assertEqual(p["renames"], len(MIXED))
        self.assertTrue(p["folder"]["changed"])
        self.assertEqual(self.snapshot(), before_files)
        self.assertEqual(self.rows(), before_rows)

    def snapshot(self):
        out = {}
        for top, dirs, files in os.walk(self.root):
            for n in dirs + files:
                p = os.path.join(top, n)
                st = os.lstat(p)
                data = b""
                if os.path.isfile(p) and not p.endswith((".db", "-wal", "-shm")):
                    with open(p, "rb") as f:
                        data = f.read()
                out[p] = (st.st_ino, st.st_nlink, st.st_mode, st.st_mtime_ns, hashlib.sha1(data).hexdigest())
        return out

    def rows(self):
        with db.connect() as con:
            return ([tuple(r) for r in con.execute("SELECT * FROM chapter ORDER BY number")],
                    [tuple(r) for r in con.execute("SELECT * FROM series")])

    def test_unknown_series_and_bad_formats(self):
        sid = self.add_series(MIXED[:2])
        with self.assertRaises(LookupError):
            self.plan(sid + 1)
        with self.assertRaises(naming.FormatError) as e:
            self.plan(sid, {"chapter_file_format": "{Series Title}"})
        self.assertIn("must contain {Chapter}", e.exception.messages[0])

    def test_options_object(self):
        sid = self.add_series(MIXED[:3])
        p = self.plan(sid, naming.Options(chapter_file_format="Ch {Chapter:000.0}{ - Chapter Title}"))
        self.assertEqual(self.names(p)[1.0], "Ch 001.0.cbz")


class FormatChangeTest(PlanBase):
    def test_new_chapter_format(self):
        sid = self.add_series(MIXED)
        p = self.plan(sid, {"chapter_file_format": "Chapter {Chapter:000}{ - Chapter Title}"})
        n = self.names(p)
        self.assertEqual(n[1.0], "Chapter 001.cbz")
        self.assertEqual(n[12.0], "Chapter 012.cbz")                      # the title stays out, as in the file
        self.assertEqual(n[12.5], "Chapter 012.5 - Extra_ The Daily Life.cbz")
        self.assertEqual(n[13.0], "Chapter 013 - The Old Title.cbz")
        self.assertEqual(n[0.99], "Chapter 000.99.cbz")
        self.assertEqual(p["renames"], len(MIXED) - 3)                      # 0.99, 12.25 and 12.5 keep theirs
        self.assertEqual(p["collisions"], [])

    def test_raw_title_follows_a_new_colon_replacement(self):
        # the file's title is what the source's name still gives, so its ':' is rendered again; an edited
        # title only has the file's '_'
        sid = self.add_series([(12.5, "Chapter 12.5: Extra: The Daily Life", "Chapter 12.5: Extra: The Daily Life"),
                               (13.0, "Now: Another", "Before: This")])
        n = self.names(self.plan(sid, {"colon_replacement": "dash"}))
        self.assertEqual(n[12.5], "Chapter 012.5 - Extra- The Daily Life.cbz")
        self.assertEqual(n[13.0], "Chapter 013.0 - Before_ This.cbz")

    def test_title_options(self):
        sid = self.add_series([(12.0, "Vol.3 chapter 13", "Vol.3 chapter 13"),
                               (14.0, "A rather long chapter title", "A rather long chapter title")])
        n = self.names(self.plan(sid, {"drop_number_only_titles": True, "chapter_title_max_chars": 8}))
        self.assertEqual(n, {12.0: "Chapter 012.0.cbz", 14.0: "Chapter 014.0 - A rather.cbz"})

    def test_a_file_already_renamed_to_the_chosen_format(self):
        fmt = {"chapter_file_format": "{Chapter:000} - {Chapter Title}"}
        sid = self.add_series([(3.0, "Chapter 3: The Storm", "003 - The Storm.cbz")])
        p = self.plan(sid, fmt)
        self.assertEqual((p["renames"], p["chapters"][0]["title_from"], p["warnings"]), (0, "file", []))

    def test_a_name_no_format_made_takes_the_source_title(self):
        sid = self.add_series([(3.0, "Chapter 3: The Storm", "Old style 3.cbz")])
        p = self.plan(sid)
        self.assertEqual(self.names(p), {3.0: "Chapter 003.0 - The Storm.cbz"})
        self.assertEqual(p["chapters"][0]["title_from"], "source")
        self.assertEqual(p["warnings"][0]["kind"], "title")
        self.assertIn("(chapter 3)", p["warnings"][0]["message"])

    def test_sort_order_warning(self):
        sid = self.add_series([(12.0, None, None), (12.5, None, None), (13.0, None, None)])
        p = self.plan(sid, {"chapter_file_format": "Chapter {Chapter:000}"})
        self.assertEqual(p["warnings"], [{"kind": "sort", "message": (
            "Readers and file browsers that sort by plain file name would put Chapter 012.5.cbz before "
            "Chapter 012.cbz. Komga goes by the chapter number.")}])
        self.assertEqual(self.plan(sid)["warnings"], [])

    def test_sort_order_that_was_already_wrong_is_not_blamed_on_the_rename(self):
        sid = self.add_series([(999.0, None, None), (1000.0, None, None)])     # 1000.0 sorts first today
        self.assertEqual(self.plan(sid, {"chapter_file_format": "Chapter {Chapter:000.00}"})["warnings"], [])
        p = self.plan(sid, {"chapter_file_format": "Chapter {Chapter:0000.0}"})
        self.assertEqual((p["warnings"], self.names(p)[999.0]), ([], "Chapter 0999.0.cbz"))


class FolderTest(PlanBase):
    def test_new_folder_format(self):
        sid = self.add_series(MIXED[:3])
        p = self.plan(sid, {"series_folder_format": "{Series Romaji}{ (Year)}"})
        self.assertEqual(p["folder"], {"old": "Yakuza Fiancé_ Raise wa Tanin ga Ii",
                                       "new": "Raise wa Tanin ga Ii (2017)", "changed": True, "blocked": None})
        self.assertEqual(p["renames"], 0)                                # the files keep their names ...
        self.assertEqual(p["chapters"][1]["new_path"],                   # ... in the renamed folder
                         os.path.join(self.library, "Raise wa Tanin ga Ii (2017)", "Chapter 001.0.cbz"))
        self.assertEqual(p["komga"]["state"], "not_configured")           # a folder rename is checked too

    def test_a_folder_that_is_there_blocks_the_folder_rename(self):
        sid = self.add_series(MIXED[:2])
        os.makedirs(os.path.join(self.library, "raise wa tanin ga ii (2017)"))    # not a series of mang-arr's
        p = self.plan(sid, {"series_folder_format": "{Series Romaji}{ (Year)}",
                            "chapter_file_format": "Chapter {Chapter:000}"})
        self.assertEqual(p["folder"]["blocked"], "a folder named 'raise wa tanin ga ii (2017)' is already in the library")
        self.assertEqual(p["collisions"][0]["name"], "Raise wa Tanin ga Ii (2017)")
        self.assertEqual(p["chapters"][1]["new_path"], os.path.join(self.folder(sid), "Chapter 001.cbz"))

    def test_another_series_folder_is_never_taken(self):
        self.add_series([], anilist_id=1, english="Raise wa Tanin ga Ii (2017)", romaji=None, year=None)
        sid = self.add_series(MIXED[:1])
        p = self.plan(sid, {"series_folder_format": "{Series Romaji}{ (Year)}"})
        self.assertEqual(p["folder"]["new"], "Raise wa Tanin ga Ii (2017) (anilist_97994)")

    def test_a_suffixed_folder_is_kept(self):
        a = self.add_series(MIXED[:1], anilist_id=1, english="Wind Breaker", romaji=None, year=None)
        b = self.add_series(MIXED[:1], anilist_id=2, english="Wind Breaker", romaji=None, year=None)
        self.assertTrue(self.folder(b).endswith("Wind Breaker (anilist_2)"))
        with db.connect() as con:
            db.delete_series(con, a)                              # the plain name is free now
        self.assertFalse(self.plan(b)["folder"]["changed"])

    def test_a_series_without_a_folder(self):
        sid = self.add_series(MIXED[:1])
        with db.connect() as con:
            con.execute("UPDATE series SET folder=NULL WHERE id=?", (sid,))
        p = self.plan(sid)
        self.assertEqual((p["folder"]["blocked"], p["chapters"]), ("this series has no usable library folder", []))


class CollisionTest(PlanBase):
    def test_two_chapters_one_name(self):
        # 12.001 was linked under a name of its own; without decimals both are Chapter 012
        sid = self.add_series([(12.0, None, None), (12.001, None, "Chapter 012.001.cbz")])
        p = self.plan(sid, {"chapter_file_format": "Chapter {Chapter:000}"})
        self.assertEqual([c["skip"] is not None for c in p["chapters"]], [True, True])
        self.assertEqual(p["collisions"], [{"name": "Chapter 012.cbz", "numbers": [12.0, 12.001],
                                            "message": "Chapters 12, 12.001 would all be named Chapter 012.cbz"}])
        self.assertEqual(p["renames"], 0)

    def test_a_chapter_that_has_the_name_keeps_it(self):
        sid = self.add_series([(12.0, None, None), (12.001, None, "Chapter 012.001.cbz")])
        p = self.plan(sid)                                    # 12.001 would become Chapter 012.0 too
        c = {x["number"]: x for x in p["chapters"]}
        self.assertIsNone(c[12.0]["skip"])
        self.assertEqual(c[12.001]["skip"], "another chapter would get the same name (chapter 12)")
        self.assertEqual(p["renames"], 0)

    def test_a_file_mang_arr_did_not_make(self):
        sid = self.add_series([(12.0, None, None), (13.0, None, None)])
        open(os.path.join(self.folder(sid), "chapter 012.cbz"), "w").close()     # another case: still taken
        p = self.plan(sid, {"chapter_file_format": "Chapter {Chapter:000}"})
        c = {x["number"]: x for x in p["chapters"]}
        self.assertEqual(c[12.0]["skip"], "'chapter 012.cbz' is already in the folder (mang-arr did not make it); "
                                          "not overwritten")
        self.assertIsNone(c[13.0]["skip"])
        self.assertEqual((p["renames"], p["collisions"][0]["numbers"]), (1, [12.0]))

    def test_a_name_that_is_being_freed_is_not_a_collision(self):
        # chapter 2's file carries the name chapter 1 gets; chapter 2 moves away first
        sid = self.add_series([(1.0, None, None), (2.0, None, "Chapter 001.cbz")])
        p = self.plan(sid, {"chapter_file_format": "Chapter {Chapter:000}"})
        self.assertEqual(self.names(p), {1.0: "Chapter 001.cbz", 2.0: "Chapter 002.cbz"})
        self.assertEqual((p["renames"], p["collisions"]), (2, []))

    def test_a_skipped_chapter_keeps_its_name_taken(self):
        sid = self.add_series([(1.0, None, None), (2.0, None, "Chapter 001.cbz")])
        open(os.path.join(self.folder(sid), "Chapter 002.cbz"), "w").close()
        p = self.plan(sid, {"chapter_file_format": "Chapter {Chapter:000}"})
        c = {x["number"]: x for x in p["chapters"]}
        self.assertIn("'Chapter 002.cbz' is already in the folder", c[2.0]["skip"])
        self.assertEqual(c[1.0]["skip"], "chapter 2 keeps that name")
        self.assertEqual((p["renames"], len(p["collisions"])), (0, 2))

    def test_a_change_of_case_only(self):
        sid = self.add_series([(12.0, None, "chapter 012.0.cbz")])
        p = self.plan(sid)
        self.assertEqual((p["chapters"][0]["new_name"], p["chapters"][0]["skip"]), ("Chapter 012.0.cbz", None))
        self.assertEqual((p["renames"], p["collisions"]), (1, []))


class SkipTest(PlanBase):
    def test_files_that_are_left_alone(self):
        sid = self.add_series([(1.0, None, None), (2.0, None, None), (3.0, None, None), (4.0, None, None),
                               (5.0, None, None)])
        d = self.folder(sid)
        os.remove(os.path.join(d, "Chapter 002.0.cbz"))
        os.remove(os.path.join(d, "Chapter 003.0.cbz"))
        os.symlink("/etc/passwd", os.path.join(d, "Chapter 003.0.cbz"))
        elsewhere = os.path.join(self.root, "elsewhere")
        os.makedirs(elsewhere)
        with db.connect() as con:
            con.execute("UPDATE chapter SET library_path=NULL WHERE series_id=? AND number=4", (sid,))
            con.execute("UPDATE chapter SET library_path=? WHERE series_id=? AND number=5",
                        (os.path.join(elsewhere, "Chapter 005.0.cbz"), sid))
        p = self.plan(sid, {"chapter_file_format": "Chapter {Chapter:000}"})
        skip = {c["number"]: c["skip"] for c in p["chapters"]}
        self.assertEqual(skip, {1.0: None, 2.0: "the file is missing",
                                3.0: "not a regular file (a symlink or a folder); left as it is",
                                4.0: "no library file is recorded for this chapter",
                                5.0: "the file is not in the series' library folder; left as it is"})
        self.assertEqual(p["renames"], 1)
        self.assertTrue(os.path.islink(os.path.join(d, "Chapter 003.0.cbz")))

    def test_a_folder_that_is_a_symlink_is_not_listed(self):
        sid = self.add_series([(1.0, None, None)])
        d = self.folder(sid)
        os.rename(d, d + " real")
        os.symlink(d + " real", d)
        p = self.plan(sid, {"chapter_file_format": "Chapter {Chapter:000}"})
        self.assertIn("not a real folder", p["chapters"][0]["skip"])
        self.assertEqual((p["renames"], p["komga"]), (0, None))


class KomgaTest(PlanBase):
    """What the preview says about reading progress (user decision 5: a rename without Komga, or with
    its file hashing off, is allowed after an explicit confirmation)."""
    FORMAT = {"chapter_file_format": "Chapter {Chapter:000}{ - Chapter Title}"}

    def setUp(self):
        super().setUp()
        self.sid = self.add_series([(1.0, None, None), (2.0, "Chapter 2: Two", "Chapter 2: Two")])
        self.folder_name = os.path.basename(self.folder(self.sid))

    def check(self):
        return self.plan(self.sid, self.FORMAT)["komga"]

    def test_no_komga(self):
        k = self.check()
        self.assertEqual(k, {"state": "not_configured", "needs_confirmation": True,
                             "message": "No Komga configured: reading progress in your reader app may be lost."})
        self.assertEqual(self.komga.calls, [])

    def test_hashing_on_and_every_book_hashed(self):
        self.komga_on()
        self.komga.add_series("S1", self.folder_name, ["Chapter 001.0.cbz", "Chapter 002.0 - Two.cbz"])
        k = self.check()
        self.assertEqual((k["state"], k["needs_confirmation"]), ("ok", False))
        self.assertTrue(k["message"].startswith("Komga will keep reading progress: file hashing is on for "
                                                "'Manga (mang-arr)' and all 2 books"), k["message"])

    def test_hashing_off(self):
        self.komga_on()
        self.komga.libraries["LIB1"]["hashFiles"] = False
        self.komga.add_series("S1", self.folder_name, ["Chapter 001.0.cbz"], hashed=False)
        k = self.check()
        self.assertEqual((k["state"], k["needs_confirmation"]), ("hashing_off", True))
        self.assertTrue(k["message"].startswith("Komga file hashing is off for the library 'Manga (mang-arr)': "
                                                "reading progress may be lost."))
        self.assertIn("Compute hash for files", k["message"])

    def test_books_not_hashed_yet(self):
        self.komga_on()
        self.komga.add_series("S1", self.folder_name, ["Chapter 001.0.cbz", "Chapter 002.0 - Two.cbz"],
                              hashed=[True, False])
        k = self.check()
        self.assertEqual((k["state"], k["needs_confirmation"]), ("not_hashed_yet", True))
        self.assertIn("has not hashed 1 of 2 books", k["message"])

    def test_series_not_in_komga_yet(self):
        self.komga_on()
        k = self.check()
        self.assertEqual((k["state"], k["needs_confirmation"]), ("not_in_komga", False))

    def test_library_found_through_the_series(self):
        self.komga_on(library_id="")
        self.komga.add_library("LIB2", "Manga (mang-arr)", hash_files=False, root="/library")
        self.komga.add_series("S1", self.folder_name, ["Chapter 001.0.cbz"], library="LIB2")
        self.assertEqual(self.check()["state"], "hashing_off")
        self.assertIn(("GET", "/api/v1/libraries/LIB2"), self.komga.calls)

    def test_same_folder_in_two_libraries(self):
        self.komga_on(library_id="")
        self.komga.add_library("OLD", "Manga", root="/manga")
        self.komga.add_series("S1", self.folder_name, ["a.cbz"], library="OLD", root="/manga")
        self.komga.add_series("S2", self.folder_name, ["Chapter 001.0.cbz"])
        k = self.check()
        self.assertEqual((k["state"], k["needs_confirmation"]), ("ambiguous", True))
        self.komga_on(library_id="LIB1")
        settings._cache.clear()
        self.assertEqual(self.check()["state"], "ok")

    def test_komga_unreachable(self):
        self.komga_on()
        self.komga.fail = urllib.error.URLError("connection refused")
        k = self.check()
        self.assertEqual((k["state"], k["needs_confirmation"]), ("error", True))
        self.assertIn("Could not check Komga (URLError", k["message"])
        self.assertIn("reading progress may be lost", k["message"])

    def test_unknown_library(self):
        self.komga_on(library_id="GONE")
        self.komga.add_series("S1", self.folder_name, ["Chapter 001.0.cbz"], library="GONE")
        k = self.check()
        self.assertEqual((k["state"], k["needs_confirmation"]), ("error", True))
        self.assertIn("HTTP Error 404", k["message"])


if __name__ == "__main__":
    unittest.main()
