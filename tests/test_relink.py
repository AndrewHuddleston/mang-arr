"""Chapters 0.3.0 linked under a misread number, and files it never
imported: the one-time link check (relink.py). The trees are real
(temporary) staging and library folders with fake chapter files named like
the ones on the live box on 2026-09-28; Suwayomi and Komga are fakes."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_import_reasons import MN, NATO, FakeSuwayomi, Tree, inode, make_cbz  # noqa: E402

from mangarr import db, jobs, library, relink  # noqa: E402
from mangarr.suwayomi import Chapter  # noqa: E402

try:
    from fastapi.testclient import TestClient

    from mangarr.web import app as web
except (ImportError, RuntimeError):      # web extras not installed
    TestClient = web = None

MD, WC = "MangaDex (EN)", "Weeb Central (EN)"
SPIN_OFFS = {f"{NATO}_Chapter 171.{i:02d}_ Spin-off {i}.cbz": float(f"171.{i:02d}")
             for i in (1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13)}
SPIN_OFFS[f"{NATO}_Chapter 171.1_ Spin-off 10.cbz"] = 171.1
HANA_91 = f"{NATO}_Chapter 9.1_ Hana-kun’s Obsession is a Powerful Poison - Part 1.cbz"
LOSERS = "Losers in eXile_Ch.150 - Hana to Yume March 2020 Special.cbz"
HUMANE = "Humane Scans_Ch.17 - Maidens 101_ A Success_.cbz"
ANON = "_a_nonymous_Ch.185.5 - Twitter Extra - Valentine's Day 2026.cbz"


def listing(manga_id: int, names: dict, downloaded=True) -> list[Chapter]:
    """Suwayomi's chapters for staged file names {file name: number}: the
    scanlator and chapter name it made each name from ('<scanlator>_<name>',
    ':' and '?' made '_')."""
    out = []
    for i, (name, n) in enumerate(sorted(names.items(), key=lambda kv: kv[1])):
        stem = os.path.splitext(name)[0]
        scanlator, _, chapter = stem.partition("_") if not stem.startswith("_a_nonymous_") else \
            ("/a/nonymous", "", stem[len("_a_nonymous_"):])
        out.append(Chapter(manga_id * 1000 + i, n, chapter, scanlator, downloaded))
    return out


class LiveBox(Tree):
    """The live box on 2026-09-28, in small: the three chapters linked under a
    misread number, Dreaming Freedom's 13 spin-offs never imported (read as
    chapters 1-13, which were on disk), Hana-kun's 9.1 likewise, and links
    that were right all along (Nanoni's chapter 0, matched through
    Suwayomi's names, among them)."""

    def build(self, con):
        # Kamisama Kiss: its MangaDex entry is "Divine Nanami", its Weeb Central one "Kamisama Kiss"
        ks = self.series(con, "Kamisama Kiss", 1)
        md = self.source(con, ks, MD, "Divine Nanami", 11, ["Unknown_Vol.15 Ch.88.5.cbz", LOSERS])
        wc = self.source(con, ks, WC, "Kamisama Kiss", 12, ["Chapter 149.cbz"])
        self.link_as(con, ks, f"{md}/Unknown_Vol.15 Ch.88.5.cbz", 88.5, MD)
        self.link_as(con, ks, f"{wc}/Chapter 149.cbz", 149.0, WC)
        self.link_as(con, ks, f"{md}/{LOSERS}", 2020.0, MD)
        # Ohitorisama ...: MangaDex calls it "Perfectly Fine on My Own, ..."
        oh = self.series(con, "Ohitorisama ni wa Naremashita node.: Kon'yakusha Houchi-chuu!", 2)
        p = self.source(con, oh, MD, "Perfectly Fine on My Own, so My Fiancé Can Twist in the Wind", 21,
                        ["Humane Scans_Ch.16.cbz", HUMANE])
        self.link_as(con, oh, f"{p}/Humane Scans_Ch.16.cbz", 16.0, MD)
        self.link_as(con, oh, f"{p}/{HUMANE}", 101.0, MD)
        # The Dangers in My Heart
        dh = self.series(con, "The Dangers in My Heart", 3)
        p = self.source(con, dh, MD, "The Dangers in My Heart", 31, ["_a_nonymous_Ch.185.4 - Twitter Extra.cbz", ANON])
        self.link_as(con, dh, f"{p}/_a_nonymous_Ch.185.4 - Twitter Extra.cbz", 185.4, MD)
        self.link_as(con, dh, f"{p}/{ANON}", 2026.0, MD)
        # Dreaming Freedom: chapters 1-13 and 171 linked, the spin-offs never
        df = self.series(con, "Dreaming Freedom", 4)
        whole = [f"{NATO}_Chapter {n}.cbz" for n in (*range(1, 14), 171)]
        p = self.source(con, df, MN, "Dreaming Freedom", 41, whole + list(SPIN_OFFS))
        for name in whole:
            self.link_as(con, df, f"{p}/{name}", library.parse_number(name), MN)
        for n in SPIN_OFFS.values():            # as save_plan left them: wanted, waiting for a pass that never came
            con.execute("INSERT INTO chapter (series_id, number, status, manga_id, source_name, reason, updated_at)"
                        " VALUES (?,?,'wanted',41,?,?,?)", (df, n, MN, f"available on {MN}; not downloaded yet - "
                                                            "waiting for a download pass", db.now()))
        # Hana-kun: 9.1 read as 1, which was on disk
        hk = self.series(con, "Hana-kun Can’t Live without Me", 5)
        p = self.source(con, hk, MN, "Himokuzu Hana-kun wa Shinitagari", 51, ["Chapter 1.cbz", "Chapter 9.cbz",
                                                                             HANA_91])
        self.link_as(con, hk, f"{p}/Chapter 1.cbz", 1.0, MN)
        self.link_as(con, hk, f"{p}/Chapter 9.cbz", 9.0, MN)
        # Nanoni: chapter 0 matched through Suwayomi's names (0.3.0 read the name as none)
        nn = self.series(con, "And Yet, You Are So Sweet", 6)
        p = self.source(con, nn, MN, "Nanoni, Chigira-kun ga Amasugiru", 61,
                        [f"{NATO}_Chapter 0_ Volume 10.cbz", f"{NATO}_Chapter 48.5.cbz"])
        self.link_as(con, nn, f"{p}/{NATO}_Chapter 0_ Volume 10.cbz", 0.0, MN, label="Volume 10")
        self.link_as(con, nn, f"{p}/{NATO}_Chapter 48.5.cbz", 48.5, MN)
        con.commit()
        return FakeSuwayomi({
            11: listing(11, {"Unknown_Vol.15 Ch.88.5.cbz": 88.5, LOSERS: 150.0}),
            12: listing(12, {f"Chapter {n}.cbz": float(n) for n in range(1, 150)}),
            21: listing(21, {f"Humane Scans_Ch.{n}.cbz": float(n) for n in range(1, 17)} | {HUMANE: 17.0}),
            31: listing(31, {"_a_nonymous_Ch.185.4 - Twitter Extra.cbz": 185.4, ANON: 185.5}),
            41: listing(41, {name: library.parse_number(name) for name in whole} | SPIN_OFFS),
            51: listing(51, {"Chapter 1.cbz": 1.0, "Chapter 9.cbz": 9.0, HANA_91: 9.1}),
            61: listing(61, {f"{NATO}_Chapter 0_ Volume 10.cbz": 0.0, f"{NATO}_Chapter 48.5.cbz": 48.5}),
        })



class LiveBoxTest(LiveBox):
    def test_the_plan_names_exactly_the_three_wrong_links(self):
        with db.connect() as con:
            self.build(con)
            found = relink.find_misreads(relink.links(con))
        self.assertEqual(sorted((m.link.number, m.actual, os.path.basename(m.link.staging_path)) for m in found),
                         [(101.0, 17.0, HUMANE), (2020.0, 150.0, LOSERS), (2026.0, 185.5, ANON)])

    def test_cli_dry_run_changes_nothing(self):
        import argparse
        import contextlib
        import io

        from mangarr import __main__ as cli
        with db.connect() as con:
            self.build(con)
        before = self.staging_state()
        library_before = {os.path.join(d, f) for d, _, fs in os.walk(self.lib) for f in fs}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.cmd_check_links(argparse.Namespace(dry_run=True)), 0)
        text = buf.getvalue()
        self.assertIn("Kamisama Kiss: chapter 2020 is 150: would remove ", text)
        self.assertIn("3 misread link(s) (dry run: nothing changed)", text)
        self.assertEqual(self.staging_state(), before)
        self.assertEqual({os.path.join(d, f) for d, _, fs in os.walk(self.lib) for f in fs}, library_before)
        with db.connect() as con:
            self.assertIn(2020.0, self.rows(con, self.ids["Kamisama Kiss"]))

    def test_repair_and_import(self):
        with db.connect() as con:
            fake = self.build(con)
            before = self.staging_state()
            untouched = {p: inode(p) for p in (
                self.lib_path(con, self.ids["Kamisama Kiss"], "Chapter 149.0.cbz"),
                self.lib_path(con, self.ids["The Dangers in My Heart"], "Chapter 185.4.cbz"),
                self.lib_path(con, self.ids["And Yet, You Are So Sweet"], "Chapter 000.0 - Volume 10.cbz"),
                self.lib_path(con, self.ids["Hana-kun Can’t Live without Me"], "Chapter 001.0.cbz"),
                *(self.lib_path(con, self.ids["Dreaming Freedom"], f"Chapter {n:05.1f}.cbz") for n in range(1, 14)))}
            with self.assertLogs("mangarr.relink", "WARNING") as cm:
                msg = relink.check_links(con, fake)
            self.assertEqual(msg, "3 misread link(s) repaired; 17 chapter(s) imported")
            # the three wrong links are gone, their files linked under their real numbers, the bogus rows deleted
            for title, wrong, right, name in (("Kamisama Kiss", 2020.0, 150.0, LOSERS),
                                              ("Ohitorisama ni wa Naremashita node._ Kon'yakusha Houchi-chuu!", 101.0,
                                               17.0, HUMANE),
                                              ("The Dangers in My Heart", 2026.0, 185.5, ANON)):
                sid = next(v for k, v in self.ids.items() if library.safe_title(k) == title)
                rows = self.rows(con, sid)
                self.assertNotIn(wrong, rows)
                self.assertEqual(rows[right]["status"], "have")
                self.assertEqual(os.path.basename(rows[right]["staging_path"]), name)
                self.assertFalse(os.path.exists(self.lib_path(con, sid, library.chapter_filename(wrong))))
                self.assertEqual(inode(rows[right]["library_path"]), inode(rows[right]["staging_path"]))
                self.assertEqual(rows[right]["library_path"], self.lib_path(con, sid, library.chapter_filename(right)))
            # the spin-offs and Hana-kun's 9.1 are linked at last
            df = self.rows(con, self.ids["Dreaming Freedom"])
            for name, n in SPIN_OFFS.items():
                self.assertEqual((df[n]["status"], os.path.basename(df[n]["staging_path"])), ("have", name))
                self.assertEqual(inode(df[n]["library_path"]), inode(df[n]["staging_path"]))
            self.assertEqual(sorted(os.listdir(os.path.join(self.lib, "Dreaming Freedom")))[-14:],
                             ["Chapter 171.0.cbz", "Chapter 171.01.cbz", "Chapter 171.02.cbz", "Chapter 171.03.cbz",
                              "Chapter 171.04.cbz", "Chapter 171.05.cbz", "Chapter 171.06.cbz", "Chapter 171.07.cbz",
                              "Chapter 171.08.cbz", "Chapter 171.09.cbz", "Chapter 171.1.cbz", "Chapter 171.11.cbz",
                              "Chapter 171.12.cbz", "Chapter 171.13.cbz"])
            hk = self.rows(con, self.ids["Hana-kun Can’t Live without Me"])
            self.assertEqual((hk[9.1]["status"], os.path.basename(hk[9.1]["staging_path"])), ("have", HANA_91))
            # what was right stays exactly as it was, and staging is never touched
            self.assertEqual({p: inode(p) for p in untouched}, untouched)
            self.assertEqual(self.staging_state(), before)
            self.assertEqual(self.events(con, "relinked"), [
                "Chapter 2020 was a misread file (it is chapter 150); link removed, re-imported as 150",
                "Chapter 101 was a misread file (it is chapter 17); link removed, re-imported as 17",
                "Chapter 2026 was a misread file (it is chapter 185.5); link removed, re-imported as 185.5"])
            self.assertEqual(len([m for m in cm.output if "was a misread file" in m]), 3)
            self.assertTrue(self.scans)                                     # Komga was asked to scan
            # a second run finds nothing more to do
            self.assertEqual(relink.check_links(con, fake), "no misread links; 0 chapter(s) imported")

    def test_without_suwayomi_the_rows_decide(self):
        """Suwayomi down: the file names alone repair; a row no resolve ever
        listed (2020: made by the import alone) is deleted."""
        with db.connect() as con:
            fake = self.build(con)
            fake.down = True
            with self.assertLogs("mangarr", "WARNING"):
                msg = relink.check_links(con, fake)
            self.assertTrue(msg.startswith("3 misread link(s) repaired"), msg)
            self.assertNotIn(2020.0, self.rows(con, self.ids["Kamisama Kiss"]))
            self.assertEqual(self.rows(con, self.ids["Kamisama Kiss"])[150.0]["status"], "have")

    def test_once_after_the_upgrade(self):
        """Migration 17 schedules the check for a database with library links;
        it runs once and is not due again. A new database has nothing due."""
        with db.connect() as con:
            self.assertEqual(db.maintenance_due(con), set())
            fake = self.build(con)
            con.execute("DROP TABLE maintenance")
            con.execute("PRAGMA user_version = 16")
            con.commit()
        with db.connect() as con:                                   # the upgrade
            self.assertEqual(db.maintenance_due(con), {relink.TASK})
        with self.assertLogs("mangarr.relink", "WARNING"):
            self.assertTrue(relink.run_if_due(fake).startswith("3 misread link(s) repaired"))
        with db.connect() as con:
            self.assertEqual(db.maintenance_due(con), set())
        self.assertIsNone(relink.run_if_due(fake))


class RepairRulesTest(Tree):
    def one(self, con, number=1.0, name=HANA_91, listed=(1.0, 9.0, 9.1)):
        """Hana-kun with 0.3.0 having linked HANA_91 as `number`."""
        sid = self.series(con, "Hana-kun", 5)
        p = self.source(con, sid, MN, "Himokuzu", 51, [name, "Chapter 9.cbz"])
        self.link_as(con, sid, f"{p}/Chapter 9.cbz", 9.0, MN)
        dst = self.link_as(con, sid, f"{p}/{name}", number, MN)
        con.commit()
        return sid, f"{p}/{name}", dst, FakeSuwayomi({51: [Chapter(51000 + i, n, f"Chapter {n:g}", None, n != 1.0)
                                                           for i, n in enumerate(listed)]})

    def test_a_listed_number_is_wanted_again(self):
        with db.connect() as con:
            sid, staged, dst, fake = self.one(con)
            with self.assertLogs("mangarr.relink", "WARNING"):
                relink.check_links(con, fake)
            rows = self.rows(con, sid)
            self.assertFalse(os.path.exists(dst))
            self.assertEqual((rows[1.0]["status"], rows[1.0]["library_path"], rows[1.0]["staging_path"]),
                             ("wanted", None, None))
            self.assertIn("its file was chapter 9.1", rows[1.0]["reason"])
            self.assertEqual((rows[9.1]["status"], rows[9.1]["staging_path"]), ("have", staged))
            self.assertEqual(self.events(con, "relinked"), [
                "Chapter 1 was a misread file (it is chapter 9.1); link removed, re-imported as 9.1; chapter 1 is "
                "wanted again"])

    def test_only_links_mang_arr_made_are_removed(self):
        """A copy (another inode), a symlink, a file outside the series' library
        folder, a staging path outside the staging root: left alone."""
        cases = {}
        with db.connect() as con:
            sid, staged, dst, fake = self.one(con)
            os.remove(dst)                                   # a copy: same bytes, another file
            with open(staged, "rb") as r, open(dst, "wb") as w:
                w.write(r.read())
            cases["copy"] = dst
            other = self.series(con, "Other", 9)
            o = os.path.join(self.lib, db.get_series(con, other)["folder"])
            os.makedirs(o)
            p = self.source(con, other, MN, "Other", 91, ["www.x_Ch.3 - 2020.cbz"])
            elsewhere = os.path.join(self.lib, "Kamisama Kiss", "Chapter 2020.0.cbz")     # another series' folder
            os.makedirs(os.path.dirname(elsewhere))
            os.link(f"{p}/www.x_Ch.3 - 2020.cbz", elsewhere)
            db.set_have(con, other, 2020.0, f"{p}/www.x_Ch.3 - 2020.cbz", elsewhere, MN)
            cases["outside the folder"] = elsewhere
            link = os.path.join(o, "Chapter 2019.0.cbz")
            os.symlink(f"{p}/www.x_Ch.3 - 2020.cbz", link)
            db.set_have(con, other, 2019.0, f"{p}/www.x_Ch.3 - 2020.cbz", link, MN)
            cases["symlink"] = link
            outside = os.path.join(os.path.dirname(self.staging), "elsewhere_Ch.4 - 2018.cbz")
            make_cbz(outside)
            lib2018 = os.path.join(o, "Chapter 2018.0.cbz")
            os.link(outside, lib2018)
            db.set_have(con, other, 2018.0, outside, lib2018, MN)
            cases["staging outside the root"] = lib2018
            con.commit()
            state = {k: os.lstat(v) for k, v in cases.items()}
            with self.assertLogs("mangarr", "WARNING"):
                self.assertEqual(relink.find_misreads(relink.links(con)), [])
                relink.check_links(con, fake)
            for k, v in cases.items():
                with self.subTest(case=k):
                    self.assertEqual(os.lstat(v).st_ino, state[k].st_ino)
            self.assertEqual(self.rows(con, sid)[1.0]["status"], "have")
            self.assertEqual({n: r["status"] for n, r in self.rows(con, other).items() if n > 2000},
                             {2018.0: "have", 2019.0: "have", 2020.0: "have"})

    def test_a_name_suwayomi_matched_is_left_alone(self):
        """A file whose name reads as no number (a season episode, linked
        through Suwayomi's names) says nothing about its link."""
        with db.connect() as con:
            sid = self.series(con, "Wind Breaker", 7)
            p = self.source(con, sid, "Weeb Central (EN)", "Wind Breaker", 71, ["S2 - Episode 5.cbz"])
            dst = self.link_as(con, sid, f"{p}/S2 - Episode 5.cbz", 131.0, "Weeb Central (EN)", label="S2 - Episode 5")
            con.commit()
            self.assertEqual(relink.find_misreads(relink.links(con)), [])
            self.assertTrue(os.path.exists(dst))


@unittest.skipIf(web is None, "web extras not installed")
class WebTaskTest(LiveBox):
    """The System task, the API command and the run once after the upgrade:
    a job on the runner (a fresh one, not started: nothing runs by itself)."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(web, "runner", jobs.Runner())
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(web.app)

    def queued(self) -> list[str]:
        return [j.kind for j in web.runner.jobs() if j.status == "queued"]

    def test_system_task_and_command_queue_one_check(self):
        r = self.client.post("/system/check-links", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.client.post("/api/v1/command", json={"name": "CheckLibraryLinks"})
        self.assertEqual(self.queued(), ["check-links"])

    def test_queued_at_start_when_due_and_done_once_run(self):
        web._queue_maintenance()
        self.assertEqual(self.queued(), [])                             # a new database: nothing to check
        with db.connect() as con:
            fake = self.build(con)
            con.execute("INSERT INTO maintenance (name, due_at) VALUES (?, ?)", (relink.TASK, db.now()))
        web._queue_maintenance()
        self.assertEqual(self.queued(), ["check-links"])
        job = jobs.Job(1, "check-links", "check library links")
        with mock.patch.object(web, "client", fake), mock.patch.object(web, "with_cancel", lambda c, f: c), \
                self.assertLogs("mangarr.relink", "WARNING"):
            self.assertEqual(web._job_check_links(job), "3 misread link(s) repaired; 17 chapter(s) imported")
        with db.connect() as con:
            self.assertEqual(db.maintenance_due(con), set())


class SchemaTest(unittest.TestCase):
    def test_new_database_has_nothing_due(self):
        with tempfile.TemporaryDirectory() as d, db.connect(os.path.join(d, "t.db")) as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
            self.assertEqual(db.maintenance_due(con), set())
            db.maintenance_done(con, relink.TASK)
            self.assertEqual(db.maintenance_due(con), set())


if __name__ == "__main__":
    unittest.main()
