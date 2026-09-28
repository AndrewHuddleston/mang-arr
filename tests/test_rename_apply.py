"""Renaming library files for real (renamer.apply, repair, undo): nothing is
lost, overwritten or left behind, staging is never touched, a hard link stays
the same file, the database and the disk agree after every interruption, and
an undo gives every name back. Everything happens in a temporary folder;
Komga is faked (fake_komga.py)."""
import contextlib
import os
import random
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_renamer import MIXED, PlanBase  # noqa: E402

from mangarr import backup, core, db, jobs, komga, library, naming, renamer, serieslock, settings  # noqa: E402
from mangarr.model import Series  # noqa: E402

NO_DECIMALS = {"chapter_file_format": "Chapter {Chapter:000}{ - Chapter Title}"}
WITH_YEAR = {"chapter_file_format": "{Series Romaji} - Ch. {Chapter:0000.0}{ - Chapter Title} [{Year}]",
             "colon_replacement": "smart"}
ROMAJI_FOLDER = {"series_folder_format": "{Series Romaji}{ (Year)}"}


class Crash(BaseException):
    """A kill: nothing after it runs, not even the clean-up of an except Exception."""


class ApplyBase(PlanBase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch("mangarr.config.DATA_DIR", self.root),
                  mock.patch("mangarr.config.LOCK_PATH", os.path.join(self.root, "download.lock")),
                  mock.patch.object(komga, "scan_retrying", self.scan)):
            p.start()
            self.addCleanup(p.stop)
        self.scans = 0

    @contextlib.contextmanager
    def at(self, **where):
        """Like subTest, but the first failure ends the test (every later step would fail on its leftovers)."""
        try:
            yield
        except Exception as e:
            raise AssertionError(f"{where}: {type(e).__name__}: {e}") from e

    def scan(self, *a) -> bool:
        self.scans += 1
        return True

    def add(self, chapters=MIXED, titles: bool = True, **kw) -> int:
        """A series as add_series makes it, its file titles on record as the migration leaves them."""
        sid = self.add_series(chapters, **kw)
        if titles:
            with db.connect() as con:
                db._backfill_file_titles(con)
        return sid

    def apply(self, sid, formats=None, confirmed=True, **kw) -> dict:
        return renamer.apply(self.plan(sid, formats, **kw), confirmed=confirmed)

    def snapshot(self) -> dict:
        """What must never change by itself: every file of the library and of staging, by inode."""
        out = {"library": {}, "staging": {}}
        for key, top in (("library", self.library), ("staging", self.staging)):
            for d, _dirs, files in os.walk(top):
                for f in files:
                    st = os.lstat(os.path.join(d, f))
                    where = os.path.relpath(os.path.join(d, f), top)
                    out[key][where] = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_nlink)
        return out

    def rows(self, sid) -> dict:
        with db.connect() as con:
            return {r["number"]: (r["library_path"], r["file_title"], r["staging_path"]) for r in con.execute(
                "SELECT number, library_path, file_title, staging_path FROM chapter WHERE series_id=?", (sid,))}

    def chapter_inodes(self, sid) -> dict:
        return {n: os.lstat(p).st_ino for n, (p, _, _) in self.rows(sid).items()}

    def check(self, before: dict, inodes: dict, *sids) -> None:
        """The invariants: no file lost, added, overwritten or left under a temporary name; staging as it
        was; every chapter's row points at its own file; nothing of the journal is left open."""
        now = self.snapshot()
        self.assertEqual(now["staging"], before["staging"])
        self.assertEqual(sorted(now["library"].values()), sorted(before["library"].values()))
        self.assertEqual([p for p in now["library"] if renamer.TMP_PREFIX in p], [])
        self.assertEqual([n for n in os.listdir(self.library) if n.startswith(renamer.TMP_PREFIX)], [])
        for sid in sids:
            for n, (path, _title, _staged) in self.rows(sid).items():
                st = os.lstat(path)
                self.assertEqual(st.st_ino, inodes[sid][n], (n, path))
                self.assertTrue(library.is_within(path, self.library))
                self.assertEqual(os.path.dirname(path), self.folder(sid))
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM rename_log WHERE state='planned'").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM rename_run WHERE state='running'").fetchone()[0], 0)

    def ring(self) -> int:
        with db.connect() as con:
            sid = db.upsert_series(con, Series(anilist_id=9, english="ring", romaji="Ring", year=2001))
            d = library.library_dir(db.get_series(con, sid)["folder"])
            os.makedirs(d, exist_ok=True)
            os.makedirs(self.staging, exist_ok=True)
            names = {1.0: "Chapter 002.0.cbz", 2.0: "Chapter 001.0.cbz", 3.0: "chapter 003.0.cbz",
                     4.0: "Chapter 004.0 - Old.cbz"}
            for n, name in names.items():
                staged = os.path.join(self.staging, f"ring-{n}.cbz")
                with open(staged, "wb") as f:
                    f.write(f"ring {n}".encode())
                os.link(staged, os.path.join(d, name))
                db.set_have(con, sid, n, staged, os.path.join(d, name), "Source",
                            file_title="Old" if n == 4 else "")
        return sid

    def names(self, sid) -> list:          # noqa: D102 (PlanBase.names is for a preview; here: the folder)
        return sorted(os.listdir(self.folder(sid)))


class ApplyTest(ApplyBase):
    def test_the_defaults_rename_nothing_and_write_nothing(self):
        sid = self.add()
        before, rows = self.snapshot(), self.rows(sid)
        out = renamer.apply(self.plan(sid))
        self.assertEqual((out["run_id"], out["renamed"], out["message"]), (None, 0, "nothing to rename"))
        self.assertEqual((self.snapshot(), self.rows(sid)), (before, rows))
        self.assertFalse(os.path.isdir(os.path.join(self.root, "backups")))
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM rename_run").fetchone()[0], 0)
        self.assertEqual(self.scans, 0)

    def test_files_are_renamed_and_the_rows_follow(self):
        sid = self.add()
        before, inodes, rows = self.snapshot(), self.chapter_inodes(sid), self.rows(sid)
        p = self.plan(sid, NO_DECIMALS)
        out = renamer.apply(p, confirmed=True)
        self.assertEqual(out["state"], "done")
        self.assertEqual(out["renamed"], p["renames"])
        self.assertEqual(out["skipped"], 0)
        self.assertEqual(out["series"][0]["problems"], [])
        self.check(before, {sid: inodes}, sid)
        after = self.rows(sid)
        self.assertEqual({n: v[0] for n, v in after.items()}, {c["number"]: c["new_path"] for c in p["chapters"]})
        self.assertEqual({n: v[1:] for n, v in after.items()}, {n: v[1:] for n, v in rows.items()})   # titles kept
        self.assertIn("Chapter 012.cbz", self.names(sid))
        self.assertIn("Chapter 012.5 - Extra_ The Daily Life.cbz", self.names(sid))
        self.assertEqual(self.plan(sid, NO_DECIMALS)["renames"], 0)                  # a second preview: nothing
        self.assertEqual(renamer.apply(self.plan(sid, NO_DECIMALS))["run_id"], None)

    def test_a_hard_link_stays_the_same_file_and_staging_is_not_touched(self):
        sid = self.add()
        staged = {n: os.lstat(v[2]) for n, v in self.rows(sid).items()}
        listing = sorted(os.listdir(self.staging))
        self.apply(sid, WITH_YEAR)
        for n, (path, _, staging_path) in self.rows(sid).items():
            st, was = os.lstat(path), staged[n]
            self.assertEqual((st.st_ino, st.st_dev, st.st_nlink), (was.st_ino, was.st_dev, 2))
            now = os.lstat(staging_path)
            self.assertEqual((now.st_ino, now.st_mtime_ns, now.st_ctime_ns > 0), (was.st_ino, was.st_mtime_ns, True))
        self.assertEqual(sorted(os.listdir(self.staging)), listing)

    def test_a_copy_is_renamed_like_a_link(self):
        sid = self.add([(1.0, "Chapter 1", None)])
        path = self.rows(sid)[1.0][0]
        os.remove(path)
        with open(path, "wb") as f:                 # a library on another file system: a copy, one link
            f.write(b"a copy")
        with db.connect() as con:
            con.execute("UPDATE rename_log SET size=0 WHERE 0")      # (the table is there)
        ino = os.lstat(path).st_ino
        self.apply(sid, NO_DECIMALS)
        new = self.rows(sid)[1.0][0]
        self.assertEqual(os.path.basename(new), "Chapter 001.cbz")
        self.assertEqual((os.lstat(new).st_ino, os.lstat(new).st_nlink), (ino, 1))

    def test_the_journal_the_backup_and_the_event(self):
        sid = self.add([(1.0, "Chapter 1", None), (2.0, "Chapter 2: Two", "Chapter 2: Two")])
        out = self.apply(sid, NO_DECIMALS)
        self.assertTrue(os.path.isfile(out["backup"]))
        self.assertEqual(os.path.dirname(out["backup"]), os.path.join(self.root, "backups"))
        with db.connect(out["backup"]) as old:      # written before anything was renamed
            self.assertEqual(old.execute("SELECT COUNT(*) FROM rename_run").fetchone()[0], 0)
            self.assertTrue(old.execute("SELECT library_path FROM chapter WHERE number=1").fetchone()[0]
                            .endswith("Chapter 001.0.cbz"))
        with db.connect() as con:
            run = renamer.runs(con)[0]
            self.assertEqual((run["id"], run["state"], run["renamed"], run["skipped"], run["can_undo"]),
                             (out["run_id"], "done", 2, 0, True))
            self.assertEqual(run["backup"], out["backup"])
            steps = renamer.steps(con, out["run_id"])
            self.assertEqual([(s["kind"], s["number"], os.path.basename(s["old_path"]),
                               os.path.basename(s["new_path"]), s["state"]) for s in steps],
                             [("file", 1.0, "Chapter 001.0.cbz", "Chapter 001.cbz", "done"),
                              ("file", 2.0, "Chapter 002.0 - Two.cbz", "Chapter 002 - Two.cbz", "done")])
            self.assertEqual({s["series_id"] for s in steps}, {sid})
            self.assertTrue(all(s["size"] and s["mtime_ns"] for s in steps))
            events = [e["message"] for e in db.events(con) if e["kind"] == "renamed"]
            self.assertEqual(len(events), 1)
            self.assertIn("2 file(s)", events[0])
            self.assertEqual(renamer.runs(con, series_id=sid + 1), [])

    def test_the_journal_is_on_disk_before_the_first_file_is_touched(self):
        sid = self.add([(1.0, "Chapter 1", None), (2.0, "Chapter 2", None)])
        before = self.names(sid)
        seen = {}

        def at(name):
            if name == "journal":
                other = db.connect()                # another connection: only what is committed
                with other as con:
                    seen["rows"] = [r["state"] for r in con.execute("SELECT state FROM rename_log")]
                    seen["sync"] = con.execute("PRAGMA synchronous").fetchone()[0]
                seen["names"] = self.names(sid)
        with mock.patch.object(renamer, "_checkpoint", at), \
                mock.patch.object(renamer, "_durable", wraps=renamer._durable) as durable:
            self.apply(sid, NO_DECIMALS)
        self.assertEqual(seen["rows"], ["planned", "planned"])
        self.assertEqual(seen["names"], before)
        durable.assert_called_once()

    def test_the_journal_connection_syncs_every_commit(self):
        with db.connect() as con:
            self.assertEqual(con.execute("PRAGMA synchronous").fetchone()[0], 1)     # NORMAL
            renamer._durable(con)
            self.assertEqual(con.execute("PRAGMA synchronous").fetchone()[0], 2)     # FULL

    def test_the_series_folder_is_renamed_last_and_every_path_follows(self):
        sid = self.add()
        before, inodes = self.snapshot(), self.chapter_inodes(sid)
        order = []
        with mock.patch.object(renamer, "_checkpoint", order.append):
            out = self.apply(sid, {**NO_DECIMALS, **ROMAJI_FOLDER})
        self.assertEqual(out["series"][0]["folder"], {"old": "Yakuza Fiancé_ Raise wa Tanin ga Ii",
                                                      "new": "Raise wa Tanin ga Ii (2017)", "state": "done"})
        self.assertEqual(self.folder(sid), os.path.join(self.library, "Raise wa Tanin ga Ii (2017)"))
        self.assertEqual(os.listdir(self.library), ["Raise wa Tanin ga Ii (2017)"])
        self.assertGreater(order.index("folder-renamed"), max(i for i, n in enumerate(order) if n == "recorded"))
        self.check(before, {sid: inodes}, sid)
        with db.connect() as con:
            last = renamer.steps(con, out["run_id"])[-1]
        self.assertEqual((last["kind"], last["number"], last["state"]), ("folder", None, "done"))

    def test_the_folder_alone(self):
        sid = self.add()
        before, inodes = self.snapshot(), self.chapter_inodes(sid)
        out = self.apply(sid, ROMAJI_FOLDER)
        self.assertEqual((out["renamed"], out["series"][0]["renamed"]), (1, 0))
        self.check(before, {sid: inodes}, sid)
        self.assertEqual(os.path.basename(self.folder(sid)), "Raise wa Tanin ga Ii (2017)")

    def test_a_folder_that_is_there_already_is_never_merged_into(self):
        sid = self.add()
        os.makedirs(os.path.join(self.library, "raise wa tanin ga ii (2017)"))       # another case: still taken
        p = self.plan(sid, ROMAJI_FOLDER)
        self.assertTrue(p["folder"]["blocked"])
        p["folder"]["blocked"] = None                       # a preview from before the folder appeared
        before, rows = self.snapshot(), self.rows(sid)
        out = renamer.apply(p, confirmed=True)
        self.assertEqual(out["renamed"], 0)
        self.assertEqual((self.snapshot(), self.rows(sid)), (before, rows))

    def test_the_folder_step_is_checked_again(self):
        sid = self.add()
        p = self.plan(sid, ROMAJI_FOLDER)
        root = os.open(self.library, os.O_RDONLY)
        self.addCleanup(os.close, root)
        with db.connect() as con:
            def why(new, old=p["folder"]["old"]):
                return renamer._check_folder(con, sid, root, {"old_path": os.path.join(self.library, old),
                                                              "new_path": os.path.join(self.library, new)})
            self.assertIsNone(why(p["folder"]["new"]))
            os.makedirs(os.path.join(self.library, "TAKEN"))
            self.assertIn("already in the library", why("taken"))
            self.assertIn("can be used", why(".."))
            self.assertIn("can be used", why(renamer.TMP_PREFIX + "1.tmp"))
            self.assertEqual(why("x", old="Another"), renamer.CHANGED_SINCE)
            self.assertIn("not a folder directly in the library",
                          renamer._check_folder(con, sid, root, {"old_path": p["folder"]["old"],
                                                                 "new_path": "/etc/x"}))
            other = db.upsert_series(con, Series(anilist_id=5, english="Other"))
            con.execute("UPDATE series SET folder='Wanted' WHERE id=?", (other,))
            self.assertIn("another series", why("wanted"))
            os.rename(os.path.join(self.library, p["folder"]["old"]), os.path.join(self.library, "moved"))
            os.symlink("moved", os.path.join(self.library, p["folder"]["old"]))
            self.assertIn("not a real folder", why("fine"))

    def test_titles_survive_every_change_of_format(self):
        """User decision 1, through formats the file names of which neither the default nor the chosen
        format can read back: the titles on record are kept."""
        sid = self.add()
        rows = self.rows(sid)
        for fmt in (WITH_YEAR, {"chapter_file_format": "{Chapter:00}{ (Chapter Title)}"}, NO_DECIMALS,
                    {"chapter_file_format": "Ch {Chapter}{ Chapter Title}", "colon_replacement": "dash"}, None):
            p = self.plan(sid, fmt)
            self.assertEqual([c["skip"] for c in p["chapters"]], [None] * len(MIXED), fmt)
            self.assertEqual({c["title_from"] for c in p["chapters"]}, {"file"})
            renamer.apply(p, confirmed=True)
            self.assertEqual(self.plan(sid, fmt)["renames"], 0)
        self.assertEqual(self.rows(sid), rows)              # back at the default format: every name as it was

    def test_a_new_colon_replacement_applies_to_a_kept_title(self):
        sid = self.add([(12.5, "Chapter 12.5: Extra: The Daily Life", "Chapter 12.5: Extra: The Daily Life")])
        self.assertEqual(self.rows(sid)[12.5][1], "Extra: The Daily Life")       # the source's own spelling
        self.apply(sid, {"colon_replacement": "smart"})
        self.assertEqual(self.names(sid), ["Chapter 012.5 - Extra - The Daily Life.cbz"])
        self.assertEqual(self.rows(sid)[12.5][1], "Extra: The Daily Life")

    def test_the_latest_titles_only_when_asked_for(self):
        sid = self.add()
        self.assertEqual(self.plan(sid)["renames"], 0)
        p = self.plan(sid, use_latest_titles=True)
        self.assertEqual(sorted(c["number"] for c in p["chapters"] if c["changed"]), [12.0, 13.0])
        renamer.apply(p, confirmed=True)
        rows = self.rows(sid)
        self.assertEqual(os.path.basename(rows[13.0][0]), "Chapter 013.0 - The New Title.cbz")
        self.assertEqual((rows[13.0][1], rows[12.0][1]), ("The New Title", "Vol.1 chapter 12"))
        self.assertEqual(self.plan(sid)["renames"], 0)      # and they are the titles that are kept from now on

    def test_only_the_selected_chapters(self):
        sid = self.add([(1.0, None, None), (2.0, None, None), (3.0, None, None)])
        p = self.plan(sid, NO_DECIMALS, only=[1.0, 3])
        self.assertEqual([c["skip"] for c in p["chapters"]], [None, renamer.NOT_SELECTED, None])
        self.assertEqual((p["renames"], p["only"]), (2, [1.0, 3.0]))
        out = renamer.apply(p, confirmed=True)
        self.assertEqual(out["renamed"], 2)
        self.assertEqual(self.names(sid), ["Chapter 001.cbz", "Chapter 002.0.cbz", "Chapter 003.cbz"])

    def test_never_over_another_file(self):
        sid = self.add([(1.0, None, None), (2.0, None, None)])
        d = self.folder(sid)
        p = self.plan(sid, NO_DECIMALS)
        with open(os.path.join(d, "chapter 001.CBZ"), "wb") as f:     # appeared since; another case, still taken
            f.write(b"not ours")
        out = renamer.apply(p, confirmed=True)
        self.assertEqual((out["renamed"], out["skipped"]), (1, 1))
        self.assertEqual(self.names(sid), ["Chapter 001.0.cbz", "Chapter 002.cbz", "chapter 001.CBZ"])
        with open(os.path.join(d, "chapter 001.CBZ"), "rb") as f:
            self.assertEqual(f.read(), b"not ours")
        self.assertIn("not overwritten", out["series"][0]["skipped"][0]["reason"])

    def test_the_rename_itself_never_replaces(self):
        d = os.path.join(self.root, "x")
        os.makedirs(d)
        for name in ("a", "b"):
            with open(os.path.join(d, name), "w") as f:
                f.write(name)
        fd = os.open(d, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        for fallback in (False, True):
            with mock.patch.object(renamer, "_renameat2", None if fallback else renamer._renameat2):
                with self.assertRaises(FileExistsError):
                    renamer._rename(fd, "a", "b")
                self.assertEqual(sorted(os.listdir(d)), ["a", "b"])
                renamer._rename(fd, "a", "c")
                renamer._rename(fd, "c", "a")
        with open(os.path.join(d, "b")) as f:
            self.assertEqual(f.read(), "b")
        with mock.patch.object(renamer, "_renameat2", lambda *a: -1), \
                mock.patch.object(renamer.ctypes, "get_errno", lambda: 38):      # ENOSYS: an old kernel
            renamer._rename(fd, "a", "d")
        self.assertEqual(sorted(os.listdir(d)), ["b", "d"])

    def test_a_file_step_is_checked_again(self):
        sid = self.add([(1.0, None, None), (2.0, None, None)])
        d = self.folder(sid)
        fd = os.open(d, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        old = os.path.join(d, "Chapter 001.0.cbz")
        with db.connect() as con:
            def why(new, old=old, number=1.0):
                return renamer._check_file(con, sid, fd, d, {"old_path": old, "new_path": new, "number": number})
            self.assertIsNone(why(os.path.join(d, "x.cbz")))
            self.assertIn("not in the series' library folder", why(os.path.join(self.library, "x.cbz")))
            self.assertIn("not in the series' library folder", why(os.path.join(d, "..", "x.cbz")))
            self.assertIn("not in the series' library folder", why(os.path.join(d, "sub", "x.cbz")))
            self.assertIn("can be used", why(os.path.join(d, renamer.TMP_PREFIX + "9.tmp")))
            self.assertIn("can be used", why(os.path.join(d, "x" * 300)))
            self.assertEqual(why(os.path.join(d, "x.cbz"), number=2.0), renamer.CHANGED_SINCE)
            self.assertEqual(why(os.path.join(d, "x.cbz"), number=7.0), renamer.CHANGED_SINCE)
            os.remove(old)
            self.assertEqual(why(os.path.join(d, "x.cbz")), "the file is missing")
            os.symlink("Chapter 002.0.cbz", old)
            self.assertIn("not a regular file", why(os.path.join(d, "x.cbz")))

    def test_a_library_file_inside_staging_is_left_alone(self):
        sid = self.add([(1.0, None, None)])
        with mock.patch("mangarr.config.STAGING_ROOT", self.root):       # the library inside the staging folder
            p = self.plan(sid, NO_DECIMALS)
            self.assertEqual((p["renames"], p["chapters"][0]["skip"]), (0, renamer.IN_STAGING))
            self.assertEqual(renamer.apply(p)["run_id"], None)
        self.assertEqual(self.names(sid), ["Chapter 001.0.cbz"])

    def hand_named(self, names: dict, **kw) -> int:
        """A series whose files have these names ({number: file name}), no title in any."""
        sid = self.add([(n, None, name) for n, name in names.items()], titles=False, **kw)
        with db.connect() as con:
            con.execute("UPDATE chapter SET file_title='' WHERE series_id=?", (sid,))
        return sid

    def test_names_in_a_chain_and_in_a_ring(self):
        """2 has the name 1 gets and 3 the name 2 gets (a chain); 4 and 5 have each other's (a ring):
        nothing is overwritten and nothing skipped."""
        sid = self.hand_named({1.0: "Chapter 000.5.cbz", 2.0: "Chapter 001.0.cbz", 3.0: "Chapter 002.0.cbz",
                               4.0: "Chapter 005.0.cbz", 5.0: "Chapter 004.0.cbz", 6.0: "Chapter 006.0.cbz"})
        before, inodes = self.snapshot(), self.chapter_inodes(sid)
        p = self.plan(sid)
        self.assertEqual((p["renames"], p["collisions"]), (5, []))
        seen = []
        with mock.patch.object(renamer, "_checkpoint", seen.append):
            out = renamer.apply(p, confirmed=True)
        self.assertEqual((out["renamed"], out["skipped"]), (5, 0))
        self.assertEqual(seen.count("parked"), 1)           # one of the ring, for a moment; the chain needs none
        self.check(before, {sid: inodes}, sid)
        self.assertEqual(self.names(sid), [f"Chapter 00{i}.0.cbz" for i in range(1, 7)])
        self.assertEqual({n: os.path.basename(v[0]) for n, v in self.rows(sid).items()},
                         {float(i): f"Chapter 00{i}.0.cbz" for i in range(1, 7)})

    def test_a_change_of_case_only(self):
        sid = self.hand_named({1.0: "chapter 001.0.CBZ".replace(".CBZ", ".cbz"), 2.0: "Chapter 002.0.cbz"})
        before, inodes = self.snapshot(), self.chapter_inodes(sid)
        seen = []
        with mock.patch.object(renamer, "_checkpoint", seen.append):
            out = self.apply(sid)
        self.assertEqual((out["renamed"], seen.count("parked")), (1, 1))
        self.check(before, {sid: inodes}, sid)
        self.assertEqual(self.names(sid), ["Chapter 001.0.cbz", "Chapter 002.0.cbz"])

    def test_a_folder_whose_case_changes(self):
        sid = self.add(english="berserk", romaji="Berserk")
        before, inodes = self.snapshot(), self.chapter_inodes(sid)
        out = self.apply(sid, {"series_folder_format": "{Series Romaji}"})
        self.assertEqual(out["series"][0]["folder"], {"old": "berserk", "new": "Berserk", "state": "done"})
        self.assertEqual(os.listdir(self.library), ["Berserk"])
        self.check(before, {sid: inodes}, sid)

    def test_what_changed_since_the_preview_is_left_alone(self):
        sid = self.add([(1.0, None, None), (2.0, None, None), (3.0, None, None)])
        p = self.plan(sid, NO_DECIMALS)
        d = self.folder(sid)
        os.remove(os.path.join(d, "Chapter 002.0.cbz"))                      # gone
        with db.connect() as con:                                            # linked again under another name
            os.rename(os.path.join(d, "Chapter 003.0.cbz"), os.path.join(d, "Chapter 003.0 - New.cbz"))
            con.execute("UPDATE chapter SET library_path=?, file_title='New' WHERE series_id=? AND number=3",
                        (os.path.join(d, "Chapter 003.0 - New.cbz"), sid))
        out = renamer.apply(p, confirmed=True)
        self.assertEqual(out["renamed"], 1)
        self.assertEqual(self.names(sid), ["Chapter 001.cbz", "Chapter 003.0 - New.cbz"])
        reasons = {s["number"]: s["reason"] for s in out["series"][0]["skipped"]}
        self.assertEqual(reasons, {2.0: "the file is missing", 3.0: renamer.CHANGED_SINCE})
        self.assertEqual(out["skipped"], 2)

    def test_a_preview_made_with_other_formats_than_the_settings(self):
        """The rename is what was previewed, whatever the settings are by now."""
        sid = self.add([(1.0, None, None)])
        p = self.plan(sid, NO_DECIMALS)
        with db.connect() as con:
            settings.set_many(con, {"chapter_file_format": "{Chapter}"})
        renamer.apply(p, confirmed=True)
        self.assertEqual(self.names(sid), ["Chapter 001.cbz"])

    def test_what_is_not_a_preview_is_refused(self):
        sid = self.add([(1.0, None, None)])
        p = self.plan(sid, NO_DECIMALS)
        for bad in ({}, {"series_id": sid}, [p, "x"], {**p, "formats": {"chapter_file_format": "{Nope}"}},
                    {**p, "formats": {"chapter_file_format": "../{Chapter}"}}, {**p, "formats": 5}):
            with self.assertRaises(renamer.RenameError):
                renamer.apply(bad, confirmed=True)
        tampered = {**p, "chapters": [{**p["chapters"][0], "new_path": "/etc/passwd", "new_name": "../../x"}]}
        out = renamer.apply(tampered, confirmed=True)
        self.assertEqual((out["run_id"], self.names(sid)), (None, ["Chapter 001.0.cbz"]))
        gone = {**p, "series_id": sid + 50}
        self.assertEqual(renamer.apply(gone, confirmed=True)["run_id"], None)

    def test_several_series_in_one_rename(self):
        a = self.add([(1.0, None, None)])
        b = self.add([(1.0, None, None), (2.0, None, None)], anilist_id=2, english="Berserk", romaji="Berserk")
        c = self.add([(1.0, None, None)], anilist_id=3, english="Same", romaji="Same")
        self.komga_on()
        before = self.snapshot()
        inodes = {s: self.chapter_inodes(s) for s in (a, b, c)}
        plans = [self.plan(s, NO_DECIMALS) for s in (a, b, c)]
        said = []
        out = renamer.apply(plans, confirmed=True, progress=said.append)
        self.assertEqual((out["renamed"], len(out["series"])), (4, 3))
        self.assertEqual(said[1], "renaming 2 of 3: Berserk")
        self.check(before, inodes, a, b, c)
        self.assertEqual(self.scans, 1)                     # one scan for the whole rename
        with db.connect() as con:
            self.assertEqual(len(renamer.runs(con)), 1)
            self.assertEqual(len([e for e in db.events(con) if e["kind"] == "renamed"]), 3)

    def test_a_cancel_stops_before_the_next_series(self):
        a = self.add([(1.0, None, None)])
        b = self.add([(1.0, None, None)], anilist_id=2, english="Berserk", romaji="Berserk")
        plans = [self.plan(s, NO_DECIMALS) for s in (a, b)]
        done = []
        out = renamer.apply(plans, confirmed=True, progress=done.append, should_cancel=lambda: bool(done))
        self.assertEqual((out["state"], out["renamed"]), ("cancelled", 1))
        self.assertEqual((self.names(a), self.names(b)), (["Chapter 001.cbz"], ["Chapter 001.0.cbz"]))
        with db.connect() as con:
            self.assertEqual(renamer.runs(con)[0]["state"], "cancelled")

    def test_the_backup_comes_first(self):
        sid = self.add([(1.0, None, None)])
        before, rows = self.snapshot(), self.rows(sid)
        with mock.patch.object(backup, "create", side_effect=OSError("disk full")) as create:
            with self.assertRaisesRegex(renamer.RenameError, "backup before it failed.*nothing was renamed"):
                self.apply(sid, NO_DECIMALS)
        create.assert_called_once_with("before rename")
        self.assertEqual((self.snapshot(), self.rows(sid)), (before, rows))
        with db.connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM rename_run").fetchone()[0], 0)

    def test_an_error_in_one_series_does_not_stop_the_others(self):
        a = self.add([(1.0, None, None), (2.0, None, None)])
        b = self.add([(1.0, None, None)], anilist_id=2, english="Berserk", romaji="Berserk")
        before = self.snapshot()
        inodes = {s: self.chapter_inodes(s) for s in (a, b)}
        real, calls = renamer._record_file, []

        def record(con, row):
            calls.append(row["number"])
            if len(calls) == 2:                 # the second file of the first series: renamed, not recorded
                raise RuntimeError("boom")
            real(con, row)
        with mock.patch.object(renamer, "_record_file", record), self.assertLogs("mangarr.renamer", "ERROR"):
            out = renamer.apply([self.plan(s, NO_DECIMALS) for s in (a, b)], confirmed=True)
        self.assertEqual(out["renamed"], 3)                 # settled from the disk: the rename had happened
        self.assertEqual(out["series"][0]["problems"], ["RuntimeError: boom"])
        self.check(before, inodes, a, b)
        self.assertEqual(self.names(a), ["Chapter 001.cbz", "Chapter 002.cbz"])

    def test_a_file_that_cannot_be_renamed(self):
        sid = self.add([(1.0, None, None), (2.0, None, None)])
        before, inodes = self.snapshot(), self.chapter_inodes(sid)
        real = renamer._rename

        def rename(fd, old, new):
            if old == "Chapter 001.0.cbz":
                raise PermissionError(13, "Permission denied")
            real(fd, old, new)
        with mock.patch.object(renamer, "_rename", rename):
            out = self.apply(sid, NO_DECIMALS)
        self.assertEqual((out["renamed"], out["skipped"]), (1, 1))
        self.assertEqual(out["series"][0]["skipped"][0]["reason"], "could not be renamed (Permission denied)")
        self.check(before, {sid: inodes}, sid)
        with db.connect() as con:
            self.assertEqual([s["state"] for s in renamer.steps(con, out["run_id"])], ["failed", "done"])


class KomgaTest(ApplyBase):
    def setUp(self):
        super().setUp()
        self.sid = self.add([(1.0, None, None), (2.0, None, None)])
        self.folder_name = os.path.basename(self.folder(self.sid))

    def in_komga(self, **kw):
        self.komga_on()
        self.komga.add_series("S1", self.folder_name, ["Chapter 001.0.cbz", "Chapter 002.0.cbz"], **kw)

    def test_without_komga_the_rename_must_be_confirmed(self):
        p = self.plan(self.sid, NO_DECIMALS)
        self.assertEqual(p["komga"]["state"], "not_configured")
        with self.assertRaises(renamer.NeedsConfirmation) as e:
            renamer.apply(p)
        self.assertIn("reading progress in your reader app may be lost", str(e.exception))
        self.assertEqual([(s["series_id"], s["state"]) for s in e.exception.series], [(self.sid, "not_configured")])
        self.assertEqual(self.names(self.sid), ["Chapter 001.0.cbz", "Chapter 002.0.cbz"])
        self.assertFalse(os.path.isdir(os.path.join(self.root, "backups")))         # nothing had started
        out = renamer.apply(p, confirmed=True)
        self.assertEqual((out["renamed"], out["komga_scan"], self.scans), (2, None, 0))

    def test_with_komga_hashing_no_confirmation_and_one_scan(self):
        self.in_komga()
        p = self.plan(self.sid, NO_DECIMALS)
        self.assertEqual(p["komga"]["needs_confirmation"], False)
        out = renamer.apply(p)
        self.assertEqual((out["renamed"], out["komga_scan"], self.scans), (2, True, 1))

    def test_komga_is_asked_again_when_the_rename_starts(self):
        self.in_komga()
        p = self.plan(self.sid, NO_DECIMALS)
        self.komga.libraries["LIB1"]["hashFiles"] = False           # switched off since the preview
        with self.assertRaises(renamer.NeedsConfirmation) as e:
            renamer.apply(p)
        self.assertEqual(e.exception.series[0]["state"], "hashing_off")
        self.assertEqual(renamer.apply(p, confirmed=True)["renamed"], 2)

    def test_hashing_off_books_not_hashed_and_a_renamed_folder_need_it(self):
        self.in_komga(hashed=[True, False])
        with self.assertRaises(renamer.NeedsConfirmation):
            self.apply(self.sid, NO_DECIMALS, confirmed=False)
        self.komga.books["S1"][1]["fileHash"] = "h"
        with self.assertRaises(renamer.NeedsConfirmation) as e:
            self.apply(self.sid, ROMAJI_FOLDER, confirmed=False)
        self.assertEqual(e.exception.series[0]["state"], "folder_unverified")
        self.assertEqual(os.listdir(self.library), [self.folder_name])

    def test_what_komga_kept(self):
        self.in_komga()
        out = self.apply(self.sid, NO_DECIMALS, confirmed=False)
        with db.connect() as con:
            run = con.execute("SELECT komga FROM rename_run WHERE id=?", (out["run_id"],)).fetchone()
            self.assertIn('"books": ["S1-0", "S1-1"]', run["komga"])
            kept = renamer.komga_kept(con, out["run_id"])
            self.assertEqual(kept, [{"series_id": self.sid, "title": "Yakuza Fiancé: Raise wa Tanin ga Ii",
                                     "before": 2, "kept": 2,
                                     "message": "Komga kept 2 of 2 books (reading progress intact)"}])
            self.komga.books["S1"][1]["id"] = "new"             # Komga made a new book of one
            kept = renamer.komga_kept(con, out["run_id"])
            self.assertEqual((kept[0]["kept"], kept[0]["message"][:24]), (1, "Komga kept 1 of 2 books;"))
            self.komga.fail = OSError("down")
            self.assertEqual(renamer.komga_kept(con, out["run_id"])[0]["kept"], None)
            with self.assertRaises(LookupError):
                renamer.komga_kept(con, 99)

    def test_submit_asks_before_anything_is_queued(self):
        runner = mock.Mock()
        p = self.plan(self.sid, NO_DECIMALS)
        with self.assertRaises(renamer.NeedsConfirmation):
            renamer.submit(runner, p)
        runner.submit.assert_not_called()
        renamer.submit(runner, p, confirmed=True)
        self.assertEqual(runner.submit.call_args.args[:2], ("rename", "rename files: Yakuza Fiancé: Raise wa "
                                                                      "Tanin ga Ii"))
        self.assertEqual(runner.submit.call_args.kwargs, {"series_id": self.sid, "key": "rename"})


class CrashTest(ApplyBase):
    """A kill at every step boundary, then the start-up repair."""

    def crash_at(self, n: int, fn):
        """fn() with a kill at the n-th checkpoint: (the checkpoint's name, or None when fn got through)."""
        count = [0]
        hit = []

        def at(name):
            count[0] += 1
            if count[0] == n:
                hit.append(name)
                raise Crash(name)
        with mock.patch.object(renamer, "_checkpoint", at):
            try:
                fn()
            except Crash:
                pass
        return hit[0] if hit else None

    def every_step(self, build, formats, expect_folder: str | None = None, undo: bool = False):
        """Build the series, kill the rename (or the undo of a finished one) at step n, repair, check
        the invariants, do the rest, undo; for every n until the rename gets through."""
        seen = []
        for n in range(1, 200):
            with self.at(step=n):
                for name in os.listdir(self.library):
                    self.fail(f"library not empty: {name}")
                sid = build()
                before, inodes, rows = self.snapshot(), self.chapter_inodes(sid), self.rows(sid)
                folder = self.folder(sid)
                target = self.plan(sid, formats)
                if undo:
                    first = renamer.apply(target, confirmed=True)
                    hit = self.crash_at(n, lambda first=first: renamer.undo(first["run_id"], confirmed=True))
                else:
                    hit = self.crash_at(n, lambda target=target: renamer.apply(target, confirmed=True))
                seen.append(hit)
                with db.connect() as con:
                    open_runs = con.execute("SELECT COUNT(*) FROM rename_run WHERE state='running'").fetchone()[0]
                self.assertEqual(open_runs, 1 if hit and hit not in ("finished",) else 0, hit)
                answer = renamer.repair()
                self.assertEqual(answer["runs"], open_runs)
                self.check(before, {sid: inodes}, sid)
                self.assertEqual(renamer.repair(), {"runs": 0, "finished": 0, "put_back": 0, "left": 0})
                # from wherever the kill left it: rename (again) to the formats, and undo all of it
                renamer.apply(self.plan(sid, formats), confirmed=True)
                self.check(before, {sid: inodes}, sid)
                after = self.rows(sid)
                self.assertEqual({k: os.path.basename(v[0]) for k, v in after.items()},
                                 {c["number"]: c["new_name"] for c in target["chapters"]})
                self.assertEqual({k: v[1] for k, v in after.items()},
                                 {c["number"]: c["file_title"] for c in target["chapters"]})
                if expect_folder:
                    self.assertEqual(os.listdir(self.library), [expect_folder])
                self.assertEqual(self.plan(sid, formats)["renames"], 0)
                renamer.apply(self.plan(sid), confirmed=True)       # and back, by the default formats
                if not expect_folder:
                    self.assertEqual(self.rows(sid), rows)
                    self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.folder(sid), folder)
                with db.connect() as con:
                    core.delete_series(con, mock.Mock(), sid, delete_library=True)
                for f in os.listdir(self.staging):
                    os.remove(os.path.join(self.staging, f))
            if hit is None:
                return seen
        self.fail("the rename never got through")

    def test_a_kill_at_every_step_of_a_rename(self):
        seen = self.every_step(lambda: self.add(MIXED[:4]), NO_DECIMALS)
        self.assertEqual(seen[:4], ["run", "journal", "before-rename", "renamed"])
        self.assertEqual(seen[-3:], ["before-finish", "finished", None])
        self.assertEqual(seen.count("recorded"), 2)         # 0.99 and 12.25 keep their names

    def test_a_kill_at_every_step_with_the_folder_renamed(self):
        seen = self.every_step(lambda: self.add(MIXED[:3]), {**NO_DECIMALS, **ROMAJI_FOLDER},
                               expect_folder="Raise wa Tanin ga Ii (2017)")
        self.assertEqual(seen[-6:], ["before-folder-rename", "folder-renamed", "folder-recorded", "before-finish",
                                     "finished", None])

    def test_a_kill_at_every_step_of_a_ring_a_case_change_and_a_folder_case_change(self):
        formats = {"series_folder_format": "{Series Romaji}"}
        target = None
        for n in range(1, 200):
            with self.at(step=n):
                sid = self.ring()
                before, inodes = self.snapshot(), self.chapter_inodes(sid)
                target = target or self.plan(sid, formats)
                self.assertEqual((target["renames"], target["folder"]["new"]), (3, "Ring"))
                hit = self.crash_at(n, lambda sid=sid: renamer.apply(self.plan(sid, formats), confirmed=True))
                renamer.repair()
                self.check(before, {sid: inodes}, sid)
                self.assertEqual(renamer.apply(self.plan(sid, formats), confirmed=True)["skipped"], 0)
                self.check(before, {sid: inodes}, sid)
                self.assertEqual(os.listdir(self.library), ["Ring"])
                self.assertEqual(self.names(sid), ["Chapter 001.0.cbz", "Chapter 002.0.cbz", "Chapter 003.0.cbz",
                                                   "Chapter 004.0 - Old.cbz"])
                with db.connect() as con:
                    core.delete_series(con, mock.Mock(), sid, delete_library=True)
                for f in os.listdir(self.staging):
                    os.remove(os.path.join(self.staging, f))
            if hit is None:
                return
        self.fail("the rename never got through")

    def test_a_kill_at_every_step_of_an_undo(self):
        seen = self.every_step(lambda: self.add(MIXED[:3]), {**NO_DECIMALS, **ROMAJI_FOLDER},
                               expect_folder="Raise wa Tanin ga Ii (2017)", undo=True)
        self.assertEqual(seen[:5], ["run", "journal", "before-folder-rename", "folder-renamed", "folder-recorded"])

    def test_a_file_caught_under_its_temporary_name(self):
        """Its new name is free: it gets it. Taken meanwhile: it gets its old name back."""
        for taken in (False, True):
            with self.subTest(taken=taken):
                sid = self.ring()
                before, inodes = self.snapshot(), self.chapter_inodes(sid)
                self.assertEqual(self.crash_at(4, lambda sid=sid: renamer.apply(self.plan(sid), confirmed=True)),
                                 "parked")
                d = self.folder(sid)
                tmp = [n for n in os.listdir(d) if n.startswith(renamer.TMP_PREFIX)]
                self.assertEqual(len(tmp), 1)
                with db.connect() as con:
                    row = con.execute("SELECT * FROM rename_log WHERE id=?",
                                      (int(tmp[0][len(renamer.TMP_PREFIX):-4]),)).fetchone()
                if taken:
                    with open(row["new_path"], "wb") as f:
                        f.write(b"someone else's")
                answer = renamer.repair()
                self.assertEqual((answer["finished"], answer["put_back"]), (0, 1) if taken else (1, 0))
                if taken:
                    os.remove(row["new_path"])
                    with db.connect() as con:
                        self.assertEqual(renamer.steps(con, row["run_id"])[row["id"] - 1]["detail"],
                                         renamer.PUT_BACK)
                self.check(before, {sid: inodes}, sid)
                with db.connect() as con:
                    core.delete_series(con, mock.Mock(), sid, delete_library=True)
                    con.execute("DELETE FROM rename_log")
                    con.execute("DELETE FROM sqlite_sequence WHERE name='rename_log'")
                for f in os.listdir(self.staging):
                    os.remove(os.path.join(self.staging, f))

    def test_another_file_under_the_new_name_is_not_taken_for_the_renamed_one(self):
        sid = self.add([(1.0, None, None)])
        self.assertEqual(self.crash_at(3, lambda: self.apply(sid, NO_DECIMALS)), "before-rename")
        d = self.folder(sid)
        os.rename(os.path.join(d, "Chapter 001.0.cbz"), os.path.join(d, "elsewhere"))
        with open(os.path.join(d, "Chapter 001.cbz"), "wb") as f:       # not the file the step was planned for
            f.write(b"another file, another size")
        self.assertEqual(renamer.repair(), {"runs": 1, "finished": 0, "put_back": 0, "left": 1})
        self.assertTrue(self.rows(sid)[1.0][0].endswith("Chapter 001.0.cbz"))

    def test_a_journal_with_paths_outside_the_library_moves_nothing(self):
        sid = self.add([(1.0, None, None)])
        victim = os.path.join(self.root, "victim")
        os.makedirs(victim)
        with open(os.path.join(victim, "a.cbz"), "wb") as f:
            f.write(b"x")
        with db.connect() as con:
            con.execute("INSERT INTO rename_run (id, started_at, state) VALUES (1, ?, 'running')", (db.now(),))
            for kind, old, new in (("file", os.path.join(victim, "a.cbz"), os.path.join(victim, "b.cbz")),
                                   ("file", os.path.join(self.staging, "97994-1.0.cbz"),
                                    os.path.join(self.staging, "x.cbz")),
                                   ("file", os.path.join(self.library, "a.cbz"), os.path.join(self.library, "b")),
                                   ("folder", victim, os.path.join(self.root, "victim2")),
                                   ("folder", self.folder(sid), os.path.join(self.library, "..")),
                                   ("converted", os.path.join(victim, "a.cbz"), os.path.join(victim, "c.cbz"))):
                con.execute("INSERT INTO rename_log (run_id, series_id, number, kind, old_path, new_path, state, at)"
                            " VALUES (1,?,1,?,?,?,'planned',?)", (sid, kind, old, new, db.now()))
        before = self.snapshot()
        with mock.patch.object(renamer, "_rename") as rename:
            answer = renamer.repair()
        rename.assert_not_called()
        self.assertEqual((answer["runs"], answer["finished"], answer["left"]), (1, 0, 6))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(os.listdir(victim), ["a.cbz"])
        with db.connect() as con:
            self.assertEqual([s["state"] for s in renamer.steps(con, 1)],
                             ["failed", "failed", "failed", "failed", "failed", "skipped"])

    def test_the_repair_does_not_run_into_a_running_rename(self):
        sid = self.add([(1.0, None, None)])
        self.crash_at(3, lambda: self.apply(sid, NO_DECIMALS))
        with serieslock.rename_run():                       # a rename of another process, still at work
            self.assertIsNone(renamer.repair())
            with self.assertRaisesRegex(renamer.RenameError, "not started"), \
                    mock.patch.object(renamer, "RUN_WAIT_SECS", 0.0):
                self.apply(sid, NO_DECIMALS)
        self.assertEqual(renamer.repair()["runs"], 1)

    def test_a_series_in_use_is_settled_at_a_later_start(self):
        sid = self.add([(1.0, None, None)])
        self.assertEqual(self.crash_at(4, lambda: self.apply(sid, NO_DECIMALS)), "renamed")
        held, go = threading.Event(), threading.Event()

        def importing():
            with serieslock.hold(sid):
                held.set()
                go.wait(10)
        t = threading.Thread(target=importing)
        t.start()
        held.wait(10)
        with mock.patch.object(renamer, "SERIES_WAIT_SECS", 0.0), self.assertLogs("mangarr.renamer", "WARNING"):
            self.assertEqual(renamer.repair()["runs"], 0)
        go.set()
        t.join()
        self.assertEqual(renamer.repair(), {"runs": 1, "finished": 1, "put_back": 0, "left": 0})
        self.assertTrue(self.rows(sid)[1.0][0].endswith("Chapter 001.cbz"))

    def test_the_next_rename_settles_an_interrupted_one_first(self):
        sid = self.add([(1.0, None, None), (2.0, None, None)])
        before, inodes = self.snapshot(), self.chapter_inodes(sid)
        self.assertEqual(self.crash_at(4, lambda: self.apply(sid, NO_DECIMALS)), "renamed")
        out = self.apply(sid, NO_DECIMALS)                  # no start in between
        self.assertEqual(out["renamed"], 1)
        self.check(before, {sid: inodes}, sid)
        with db.connect() as con:
            self.assertEqual([(r["state"], r["renamed"], r["skipped"]) for r in renamer.runs(con)],
                             [("done", 1, 0), ("interrupted", 1, 1)])

    def test_the_repair_never_raises_and_asks_for_the_scan(self):
        sid = self.add([(1.0, None, None)])
        self.komga_on()
        self.crash_at(4, lambda: self.apply(sid, NO_DECIMALS))
        with mock.patch.object(renamer, "_repair_locked", side_effect=RuntimeError("x")), \
                self.assertLogs("mangarr.renamer", "ERROR"):
            self.assertIsNone(renamer.repair())
        self.assertEqual(self.scans, 0)
        renamer.repair()
        self.assertEqual(self.scans, 1)
        with db.connect() as con:
            self.assertIn("interrupted rename", [e["message"] for e in db.events(con)
                                                 if e["kind"] == "renamed"][0])


class UndoTest(ApplyBase):
    def test_an_undo_gives_every_name_and_title_back(self):
        sid = self.add()
        self.komga_on()
        before, inodes, rows = self.snapshot(), self.chapter_inodes(sid), self.rows(sid)
        folder = self.folder(sid)
        first = self.apply(sid, {**WITH_YEAR, **ROMAJI_FOLDER}, use_latest_titles=True)
        self.assertNotEqual(self.rows(sid), rows)
        out = renamer.undo(first["run_id"], confirmed=True)
        self.assertEqual((out["state"], out["renamed"], out["skipped"]), ("done", first["renamed"], 0))
        self.assertEqual((self.snapshot(), self.rows(sid), self.folder(sid)), (before, rows, folder))
        self.check(before, {sid: inodes}, sid)
        self.assertEqual(self.scans, 2)
        with db.connect() as con:
            new, old = renamer.runs(con)
            self.assertEqual((new["undo_of"], old["undone_by"]), (old["id"], new["id"]))
            self.assertEqual((old["can_undo"], old["undo_reason"]), (False, "that rename has been undone already"))
            self.assertEqual((new["can_undo"], new["undo_reason"]),
                             (False, "that is an undo itself; rename again instead"))
            self.assertTrue(os.path.isfile(new["backup"]))
        for run in (first["run_id"], out["run_id"], 99):
            with self.assertRaises(renamer.RenameError):
                renamer.undo(run, confirmed=True)
        self.assertEqual(self.rows(sid), rows)

    def test_an_undo_of_a_ring(self):
        sid = self.ring()
        before, rows = self.snapshot(), self.rows(sid)
        first = self.apply(sid, rename_folder=False)
        self.assertEqual(first["renamed"], 3)
        renamer.undo(first["run_id"], confirmed=True)
        self.assertEqual((self.snapshot(), self.rows(sid)), (before, rows))

    def test_what_changed_since_is_left_alone(self):
        sid = self.add([(1.0, None, None), (2.0, None, None), (3.0, None, None), (4.0, None, None)])
        first = self.apply(sid, NO_DECIMALS)
        d = self.folder(sid)
        os.remove(os.path.join(d, "Chapter 001.cbz"))                        # deleted since
        with open(os.path.join(d, "Chapter 002.0.cbz"), "wb") as f:          # its old name is taken
            f.write(b"not ours")
        self.apply(sid, {"chapter_file_format": "{Chapter:0}"}, only=[3.0])  # renamed again since
        out = renamer.undo(first["run_id"], confirmed=True)
        self.assertEqual((out["renamed"], out["skipped"]), (1, 3))
        self.assertEqual({s["number"]: s["reason"] for s in out["series"][0]["skipped"]},
                         {1.0: "the file is missing", 3.0: renamer.CHANGED_SINCE,
                          2.0: "'Chapter 002.0.cbz' is already in the folder; not overwritten"})
        self.assertEqual(self.names(sid), ["3.cbz", "Chapter 002.0.cbz", "Chapter 002.cbz", "Chapter 004.0.cbz"])
        with open(os.path.join(d, "Chapter 002.0.cbz"), "rb") as f:
            self.assertEqual(f.read(), b"not ours")

    def test_when_a_rename_can_be_undone(self):
        sid = self.add([(1.0, None, None)])
        out = self.apply(sid, NO_DECIMALS)
        with db.connect() as con:
            con.execute("UPDATE rename_run SET finished_at=?", (db.ago(renamer.UNDO_DAYS + 0.1),))
        with self.assertRaisesRegex(renamer.RenameError, "more than 7 days old"):
            renamer.undo(out["run_id"], confirmed=True)
        with db.connect() as con:
            con.execute("UPDATE rename_run SET finished_at=?, state='running'", (db.now(),))
            run = con.execute("SELECT * FROM rename_run").fetchone()
            self.assertEqual(renamer.can_undo(run), (False, "that rename has not finished"))
            con.execute("UPDATE rename_run SET state='done', renamed=0")
            run = con.execute("SELECT * FROM rename_run").fetchone()
            self.assertEqual(renamer.can_undo(run)[0], False)
            self.assertEqual(renamer.can_undo(None), (False, "there is no such rename"))
        self.assertEqual(self.names(sid), ["Chapter 001.cbz"])

    def test_an_interrupted_rename_can_be_undone(self):
        sid = self.add([(1.0, None, None), (2.0, None, None)])
        before, rows = self.snapshot(), self.rows(sid)
        with mock.patch.object(renamer, "_checkpoint", side_effect=[None, None, None, None, None, Crash()]):
            with self.assertRaises(Crash):
                self.apply(sid, NO_DECIMALS)
        renamer.repair()
        self.assertEqual(self.names(sid), ["Chapter 001.cbz", "Chapter 002.0.cbz"])
        with db.connect() as con:
            run = renamer.runs(con)[0]
        self.assertEqual((run["state"], run["can_undo"]), ("interrupted", True))
        renamer.undo(run["id"], confirmed=True)
        self.assertEqual((self.snapshot(), self.rows(sid)), (before, rows))

    def test_an_undo_must_be_confirmed_like_a_rename(self):
        sid = self.add([(1.0, None, None)])
        out = self.apply(sid, NO_DECIMALS)
        with self.assertRaises(renamer.NeedsConfirmation):
            renamer.undo(out["run_id"])
        self.assertEqual(self.names(sid), ["Chapter 001.cbz"])
        self.komga_on()
        self.komga.add_series("S1", os.path.basename(self.folder(sid)), ["Chapter 001.cbz"])
        self.assertEqual(renamer.undo(out["run_id"])["renamed"], 1)


class LockTest(ApplyBase):
    def test_a_series_in_use_is_not_renamed(self):
        a = self.add([(1.0, None, None)])
        b = self.add([(1.0, None, None)], anilist_id=2, english="Berserk", romaji="Berserk")
        held, go = threading.Event(), threading.Event()

        def importing():
            with serieslock.hold(a):
                held.set()
                go.wait(20)
        t = threading.Thread(target=importing)
        t.start()
        held.wait(10)
        try:
            with mock.patch.object(renamer, "SERIES_WAIT_SECS", 0.3), self.assertLogs("mangarr.renamer", "WARNING"):
                out = renamer.apply([self.plan(s, NO_DECIMALS) for s in (a, b)], confirmed=True)
        finally:
            go.set()
            t.join()
        self.assertEqual((out["renamed"], out["skipped"]), (1, 1))
        self.assertIn("an import, download or delete of this series is running",
                      out["series"][0]["skipped"][0]["reason"])
        self.assertEqual((self.names(a), self.names(b)), (["Chapter 001.0.cbz"], ["Chapter 001.cbz"]))

    def test_import_download_and_delete_wait_for_a_rename(self):
        sid = self.add([(1.0, None, None)])
        order = []

        def during(name):
            if name == "renamed" and not order:
                order.append("renaming")
                threads = [threading.Thread(target=run) for run in (importing, deleting, downloading)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(0.4)                     # they wait ...
                order.append("still renaming")
                self.threads = threads

        def importing():
            with db.connect() as con:
                core.import_series(con, sid)
            order.append("imported")

        def downloading():
            with serieslock.hold(sid, shared=True):
                order.append("downloaded")

        def deleting():
            with db.connect() as con, mock.patch.object(core, "DELETE_WAIT_SECS", 0.1):
                try:
                    core.delete_series(con, mock.Mock(), sid, delete_library=True)
                    order.append("deleted")
                except serieslock.Busy:
                    order.append("delete refused")
        with mock.patch.object(renamer, "_checkpoint", during):
            out = self.apply(sid, NO_DECIMALS)
        for t in self.threads:
            t.join(10)
        self.assertEqual(out["renamed"], 1)
        self.assertEqual(order[:3], ["renaming", "delete refused", "still renaming"])
        self.assertEqual(sorted(order[3:]), ["downloaded", "imported"])
        self.assertEqual(self.names(sid), ["Chapter 001.cbz"])

    def test_the_series_lock(self):
        with serieslock.hold(7):
            self.assertEqual(serieslock.held(7), "exclusive")
            with serieslock.hold(7), serieslock.hold(7, shared=True):     # again, by the thread that has it
                pass
            self.assertEqual(serieslock.held(7), "exclusive")
            with serieslock.hold(8, shared=True):                         # another series
                with self.assertRaisesRegex(RuntimeError, "exclusive lock was asked for"):
                    with serieslock.hold(8):
                        pass
            got = []

            def other(shared):
                try:
                    with serieslock.hold(7, shared=shared, wait_secs=0.0):
                        got.append("got it")
                except serieslock.Busy as e:
                    got.append(type(e).__name__)
            for shared in (False, True):
                t = threading.Thread(target=other, args=(shared,))
                t.start()
                t.join()
            self.assertEqual(got, ["Busy", "Busy"])
        self.assertIsNone(serieslock.held(7))
        with serieslock.hold(7, shared=True):
            t = threading.Thread(target=other, args=(True,))              # downloads do not wait for each other
            t.start()
            t.join()
            t = threading.Thread(target=other, args=(False,))
            t.start()
            t.join()
        self.assertEqual(got[2:], ["got it", "Busy"])
        with self.assertRaises(serieslock.Cancelled):
            with serieslock.hold(7):
                t = threading.Thread(target=lambda: got.append(self.cancelled_wait()))
                t.start()
                t.join()
                raise got[-1]
        self.assertEqual(sorted(os.listdir(serieslock.lock_dir())), ["series-7.lock", "series-8.lock"])
        self.assertEqual(os.path.dirname(serieslock.lock_dir()), self.root)

    def cancelled_wait(self):
        try:
            with serieslock.hold(7, should_cancel=lambda: True):
                return None
        except serieslock.Cancelled as e:
            return e

    def test_without_a_lock_file_an_import_goes_on_and_a_rename_does_not(self):
        sid = self.add([(1.0, None, None)])
        with mock.patch.object(serieslock, "lock_dir", lambda: os.path.join(self.root, "t.db", "no")), \
                mock.patch.object(serieslock, "_warned", False):
            with self.assertLogs("mangarr.serieslock", "WARNING"):
                with db.connect() as con:
                    core.import_series(con, sid)
            with self.assertLogs("mangarr.renamer", "WARNING"):
                out = self.apply(sid, NO_DECIMALS)
        self.assertEqual((out["renamed"], self.names(sid)), (0, ["Chapter 001.0.cbz"]))


class JobTest(ApplyBase):
    def test_a_rename_is_a_job_of_the_runner(self):
        sid = self.add([(1.0, None, None), (2.0, None, None)])
        runner = jobs.Runner()
        runner.start()
        seen = {}

        def at(name):
            if name == "journal":
                seen["pending"] = runner.pending_for(sid)
        with mock.patch.object(renamer, "_checkpoint", at):
            job = renamer.submit(runner, self.plan(sid, NO_DECIMALS), confirmed=True)
            for _ in range(200):
                if job.status not in jobs.ACTIVE:
                    break
                threading.Event().wait(0.05)
        self.assertEqual((job.kind, job.status, job.series_id), ("rename", "done", sid))
        self.assertEqual(job.message, "2 file(s) and folder(s) renamed in 1 series")
        self.assertTrue(seen["pending"])                    # the series page offers no delete meanwhile
        self.assertEqual(self.names(sid), ["Chapter 001.cbz", "Chapter 002.cbz"])
        with db.connect() as con:
            run = renamer.runs(con)[0]["id"]
        job = runner.submit("rename-undo", "undo", renamer.undo_job(run, confirmed=True))
        for _ in range(200):
            if job.status not in jobs.ACTIVE:
                break
            threading.Event().wait(0.05)
        self.assertEqual((job.status, job.message), ("done", "2 file(s) and folder(s) renamed in 1 series"))
        self.assertEqual(self.names(sid), ["Chapter 001.0.cbz", "Chapter 002.0.cbz"])

    def test_a_refused_rename_is_a_failed_job_that_says_why(self):
        sid = self.add([(1.0, None, None)])
        job = mock.Mock(cancel=False)
        with self.assertRaisesRegex(renamer.NeedsConfirmation, "may be lost"):
            renamer.job(self.plan(sid, NO_DECIMALS))(job)
        self.assertEqual(job.active_series_ids, frozenset({sid}))


class RestoreTest(ApplyBase):
    def test_a_backup_from_before_a_rename(self):
        """The restored rows name files that have other names by now: they follow the journal, and the
        rename can still be undone."""
        sid = self.add()
        before, inodes, rows = self.snapshot(), self.chapter_inodes(sid), self.rows(sid)
        first = self.apply(sid, {**NO_DECIMALS, **ROMAJI_FOLDER}, use_latest_titles=True)
        renamed = self.rows(sid)
        with self.assertLogs("mangarr", "WARNING"):
            msg = backup.restore(first["backup"])
        self.assertIn("follow the renames done since the backup", msg)
        self.assertEqual(self.rows(sid), renamed)
        self.assertEqual(os.path.basename(self.folder(sid)), "Raise wa Tanin ga Ii (2017)")
        self.check(before, {sid: inodes}, sid)
        with db.connect() as con:
            self.assertEqual(core.import_series(con, sid), 0)       # nothing is linked a second time
            self.assertEqual(renamer.runs(con)[0]["can_undo"], True)
        self.check(before, {sid: inodes}, sid)
        renamer.undo(first["run_id"], confirmed=True)
        self.assertEqual((self.snapshot(), self.rows(sid)), (before, rows))

    def test_a_backup_from_between_a_rename_and_its_undo(self):
        sid = self.add()
        rows = self.rows(sid)
        first = self.apply(sid, NO_DECIMALS)
        between = backup.create("test")
        renamer.undo(first["run_id"], confirmed=True)
        with self.assertLogs("mangarr", "WARNING"):
            backup.restore(between)
        self.assertEqual(self.rows(sid), rows)

    def test_the_rows_only_follow_to_files_that_are_there(self):
        sid = self.add([(1.0, None, None), (2.0, None, None)])
        first = self.apply(sid, NO_DECIMALS)
        d = self.folder(sid)
        os.rename(os.path.join(d, "Chapter 001.cbz"), os.path.join(d, "moved by hand.cbz"))
        os.remove(os.path.join(d, "Chapter 002.cbz"))
        os.symlink("/etc/passwd", os.path.join(d, "Chapter 002.cbz"))
        with self.assertLogs("mangarr", "WARNING"):
            backup.restore(first["backup"])
        self.assertEqual([os.path.basename(v[0]) for v in self.rows(sid).values()],
                         ["Chapter 001.0.cbz", "Chapter 002.0.cbz"])

    def test_no_restore_while_files_are_renamed(self):
        sid = self.add([(1.0, None, None)])
        saved = backup.create("test")
        with serieslock.rename_run():
            with self.assertRaisesRegex(backup.RestoreError, "files are being renamed"), \
                    self.assertLogs("mangarr.backup", "WARNING"):
                backup.restore(saved)
        self.assertEqual(self.plan(sid)["renames"], 0)

    def test_a_restored_journal_stays_inside_the_library(self):
        self.add([(1.0, None, None)])
        with db.connect() as con:
            con.execute("INSERT INTO rename_run (id, started_at, state) VALUES (1, ?, 'running')", (db.now(),))
            for old, new in (("/etc/passwd", "/etc/x"), (os.path.join(self.library, "a"), "/tmp/x"),
                             (os.path.join(self.library, "a", "b"), os.path.join(self.library, "a", "c"))):
                con.execute("INSERT INTO rename_log (run_id, series_id, number, kind, old_path, new_path, state, at)"
                            " VALUES (1,1,1,'file',?,?,'planned',?)", (old, new, db.now()))
        crafted = backup.create("test")
        with db.connect() as con:
            con.execute("DELETE FROM rename_log")
        with mock.patch.object(backup, "_carry_rename_journal", lambda path: False), \
                self.assertLogs("mangarr", "WARNING"):
            msg = backup.restore(crafted)
        self.assertIn("2 rename journal row(s) outside the library folder dropped", msg)
        with db.connect() as con:
            self.assertEqual([r["old_path"] for r in con.execute("SELECT old_path FROM rename_log")],
                             [os.path.join(self.library, "a", "b")])


class ImportTest(ApplyBase):
    """New files and folders are named by the settings; existing ones keep their names."""

    def staged_series(self, names, **kw) -> int:
        with db.connect() as con:
            sid = db.upsert_series(con, Series(anilist_id=7, english="Re:Zero", romaji="Re Zero", year=2014, **kw))
            d = os.path.join(self.staging, "Source (EN)", "Re_Zero")
            os.makedirs(d, exist_ok=True)
            con.execute("INSERT INTO series_source (series_id, manga_id, source_name, title, match_level,"
                        " author_level, chapter_count, max_chapter, is_primary, folder, seen_at)"
                        " VALUES (?,1,'Source (EN)','Re:Zero',3,1,3,3,1,?,?)", (sid, d, db.now()))
            for n, name in names.items():
                con.execute("INSERT INTO chapter (series_id, number, status, name, updated_at)"
                            " VALUES (?,?,'wanted',?,?)", (sid, n, name, db.now()))
        return sid

    def stage(self, n) -> None:
        import zipfile
        d = os.path.join(self.staging, "Source (EN)", "Re_Zero")
        path = os.path.join(d, f"Scans_Chapter {n:g}.cbz")
        with zipfile.ZipFile(path, "w") as z:
            for i in range(10):
                z.writestr(f"{i:03}.jpg", os.urandom(300))
        os.utime(path, (1, 1))

    def test_new_files_and_folders_take_the_formats(self):
        with db.connect() as con:
            settings.set_many(con, {"series_folder_format": "{Series Romaji}{ (Year)}", "colon_replacement": "smart",
                                    "chapter_file_format": "{Series Title} {Chapter:00}{ - Chapter Title}"})
        sid = self.staged_series({1.0: "Chapter 1: The End: Part 1", 2.5: "Chapter 2.5"})
        self.assertEqual(os.path.basename(self.folder(sid)), "Re Zero (2014)")
        for n in (1, 2.5):
            self.stage(n)
        with db.connect() as con:
            self.assertEqual(core.import_series(con, sid), 2)
        self.assertEqual(self.names(sid), ["Re-Zero 01 - The End - Part 1.cbz", "Re-Zero 02.5.cbz"])
        rows = self.rows(sid)
        self.assertEqual((rows[1.0][1], rows[2.5][1]), ("The End: Part 1", ""))
        self.assertEqual(self.plan(sid, settings.naming_options())["renames"], 0)
        with db.connect() as con:                           # a change of format renames nothing by itself
            settings.set_many(con, {"chapter_file_format": "Ch {Chapter}"})
            con.execute("INSERT INTO chapter (series_id, number, status, name, updated_at) VALUES (?,3,'wanted',"
                        "'Chapter 3', ?)", (sid, db.now()))
            con.commit()
            self.stage(3)
            self.assertEqual(core.import_series(con, sid), 1)
        self.assertEqual(self.names(sid), ["Ch 3.cbz", "Re-Zero 01 - The End - Part 1.cbz", "Re-Zero 02.5.cbz"])
        p = self.plan(sid, settings.naming_options())
        self.assertEqual(p["renames"], 2)
        renamer.apply(p, confirmed=True)
        self.assertEqual(self.names(sid), ["Ch 1.cbz", "Ch 2.5.cbz", "Ch 3.cbz"])
        self.assertEqual(self.rows(sid)[1.0][1], "The End: Part 1")          # kept for the next format

    def test_stored_settings_that_cannot_be_used_never_stop_an_import(self):
        sid = self.staged_series({1.0: "Chapter 1"})
        with db.connect() as con:
            con.execute("INSERT INTO setting (key, value) VALUES ('chapter_file_format', '\"{Nope}/../x\"'),"
                        " ('chapter_title_max_chars', '\"many\"')")
            con.commit()
            settings.refresh(con)
            self.stage(1)
            with mock.patch.object(settings, "_naming_warned", set()), \
                    self.assertLogs("mangarr.settings", "ERROR") as logs:
                self.assertEqual(core.import_series(con, sid), 1)
                other = db.upsert_series(con, Series(anilist_id=8, english="Other"))
            self.assertEqual(len(logs.output), 1)               # said once
            self.assertEqual(db.get_series(con, other)["folder"], "Other")
            options, errors = settings.checked_naming_options(con)
            self.assertTrue(any(e.startswith("Chapter File Format") for e in errors), errors)
            self.assertTrue(any(e.startswith("Chapter Title Length") for e in errors), errors)
            with self.assertRaises(naming.FormatError):
                renamer.plan(con, sid, settings.stored(con))
        self.assertEqual(self.names(sid), ["Chapter 001.0.cbz"])


class RandomTest(ApplyBase):
    """Random series, formats and kills: the invariants hold, and the way back is exact."""
    FORMATS = ("Chapter {Chapter:000.0}{ - Chapter Title}", "Chapter {Chapter:000}{ - Chapter Title}",
               "{Chapter:0}{ Chapter Title}", "{Series Title} - {Chapter:0000.00}{ (Chapter Title)} [{Year}]",
               "{Chapter Title - }c{Chapter}", "ch {Chapter:00.0}", "CH {Chapter:00.0}",
               "{Series Romaji} {Chapter:000}{ - Chapter Title}{ - Chapter Title}")
    FOLDERS = ("{Series Title}", "{Series Romaji}{ (Year)}", "{series title}", "[{Year}] {Series English}")
    TITLES = (None, "Chapter {n}", "Chapter {n}: The Storm", "Vol.1 Ch. {n}", "Extra: What? <now>", "第" * 90,
              "a" * 79 + " b", "The storm", "THE STORM", "x/y\\z", "...", "Ep. {n} - End.", "{n}")

    def test_random_renames_kills_and_undos(self):
        rng = random.Random(20260928)
        for round_ in range(60):
            with self.at(round=round_):
                numbers = sorted(rng.sample([0, 0.5, 1, 2, 2.5, 3, 10, 10.25, 99, 100, 1000], rng.randint(1, 7)))
                chapters = []
                for n in numbers:
                    label = rng.choice(self.TITLES)
                    label = label and label.format(n=f"{n:g}")
                    chapters.append((float(n), rng.choice((label, rng.choice(self.TITLES))), label))
                sid = self.add(chapters, anilist_id=1000 + round_, english=f"Ser: ies {round_}?",
                               romaji=f"ser: ies {round_}?", year=rng.choice((None, 2001)))
                before, inodes, rows = self.snapshot(), self.chapter_inodes(sid), self.rows(sid)
                folder = self.folder(sid)
                runs = []
                for _ in range(rng.randint(1, 4)):
                    formats = {"chapter_file_format": rng.choice(self.FORMATS),
                               "series_folder_format": rng.choice(self.FOLDERS),
                               "colon_replacement": rng.choice(list(naming.COLON_MODES)),
                               "replace_illegal_characters": rng.random() < 0.5,
                               "chapter_title_max_chars": rng.choice((5, 80, 255)),
                               "drop_number_only_titles": rng.random() < 0.3}
                    p = self.plan(sid, formats, use_latest_titles=rng.random() < 0.3)
                    kill = rng.choice((0, 0, 0, rng.randint(1, 30)))
                    count = [0]

                    def at(name, kill=kill, count=count):
                        count[0] += 1
                        if count[0] == kill:
                            raise Crash(name)
                    with mock.patch.object(renamer, "_checkpoint", at):
                        try:
                            out = renamer.apply(p, confirmed=True)
                            if out["run_id"]:
                                runs.append(out["run_id"])
                                done = {c["number"] for c in p["chapters"] if c["changed"] and not c["skip"]}
                                self.assertEqual(out["series"][0]["renamed"], len(done))
                                self.assertEqual(out["skipped"], 0, out)
                        except Crash:
                            pass
                    if rng.random() < 0.5 or count[0] >= kill > 0:
                        renamer.repair()
                        with db.connect() as con:
                            runs = [r["id"] for r in reversed(renamer.runs(con)) if r["can_undo"]
                                    and r["id"] in runs + [con.execute("SELECT MAX(id) FROM rename_run"
                                                                       ).fetchone()[0]]]
                        self.check(before, {sid: inodes}, sid)
                renamer.repair()
                self.check(before, {sid: inodes}, sid)
                for run in reversed(runs):                  # every rename undone, newest first: as it was
                    out = renamer.undo(run, confirmed=True)
                    self.assertEqual(out["skipped"], 0, out)
                    self.check(before, {sid: inodes}, sid)
                self.assertEqual((self.snapshot(), self.rows(sid), self.folder(sid)), (before, rows, folder))


if __name__ == "__main__":
    unittest.main()
