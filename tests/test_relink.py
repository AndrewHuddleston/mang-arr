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

from mangarr import backup, core, db, jobs, komga, library, relink  # noqa: E402
from mangarr.suwayomi import Chapter, QueryError  # noqa: E402

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
            fake = self.build(con)
        before = self.staging_state()
        library_before = {os.path.join(d, f) for d, _, fs in os.walk(self.lib) for f in fs}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(cli, "Client", lambda *a, **k: fake):
            self.assertEqual(cli.cmd_check_links(argparse.Namespace(dry_run=True)), 0)
        text = buf.getvalue()
        self.assertIn("Kamisama Kiss: chapter 2020 is 150: would remove ", text)
        self.assertIn("3 misread link(s) (dry run: nothing changed)", text)
        self.assertFalse(os.listdir(backup.backup_dir()))                  # a dry run writes no backup either
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
                msg = relink.check_links(con, fake).message
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
            # the database was backed up once, before the first change: the backup still has the wrong rows
            (b,) = backup.listing()
            with db.connect(backup.path_of(b["name"])) as old:
                self.assertEqual(old.execute("SELECT status FROM chapter WHERE number=2020").fetchone()[0], "have")
            # a second run finds nothing more to do, and writes no backup
            self.assertEqual(relink.check_links(con, fake).message, "no misread links; 0 chapter(s) imported")
            self.assertEqual(len(backup.listing()), 1)

    def test_without_suwayomi_nothing_is_changed_and_it_runs_again(self):
        """Suwayomi down: no link is removed and no row changed (only the
        import runs); the check stays due, and the next run, with Suwayomi
        back, repairs."""
        with db.connect() as con:
            fake = self.build(con)
            con.execute("INSERT INTO maintenance (name, due_at) VALUES (?, ?)", (relink.TASK, db.now()))
            wrong = self.lib_path(con, self.ids["Kamisama Kiss"], "Chapter 2020.0.cbz")
        fake.down = True
        with self.assertLogs("mangarr", "WARNING") as cm:
            msg = relink.run_if_due(fake)
        # the import links the three files under their real numbers too; the wrong links stay until confirmed
        self.assertEqual(msg, "no misread links; 3 left for a later run (Suwayomi did not answer); 17 chapter(s) "
                              "imported")
        self.assertIn("nothing changed, the check runs again later", "\n".join(cm.output))
        self.assertTrue(os.path.exists(wrong))
        with db.connect() as con:
            self.assertEqual(self.rows(con, self.ids["Kamisama Kiss"])[2020.0]["status"], "have")
            self.assertEqual(db.maintenance_due(con), {relink.TASK})
            self.assertEqual(self.events(con, "relinked"), [])
        self.assertEqual(backup.listing(), [])                             # nothing changed: no backup
        fake.down = False
        with self.assertLogs("mangarr.relink", "WARNING"):
            self.assertEqual(relink.run_if_due(fake), "3 misread link(s) repaired; 0 chapter(s) imported")
        self.assertFalse(os.path.exists(wrong))
        with db.connect() as con:
            self.assertNotIn(2020.0, self.rows(con, self.ids["Kamisama Kiss"]))
            self.assertEqual(db.maintenance_due(con), set())
            self.assertEqual(self.events(con, "relinked")[0], "Chapter 2020 was a misread file (it is chapter 150); "
                                                              "link removed, re-imported as 150")

    def test_a_manual_run_suwayomi_did_not_answer_is_due_again(self):
        """The System task or the CLI run while Suwayomi is down: due again,
        so it runs by itself once Suwayomi answers."""
        with db.connect() as con:
            fake = self.build(con)
            fake.down = True
            with self.assertLogs("mangarr", "WARNING"):
                result = relink.check_links(con, fake)
            relink.finish(con, result)
            self.assertEqual(result.deferred, 3)
            self.assertEqual(db.maintenance_due(con), {relink.TASK})

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
    def one(self, con, number=1.0, name=HANA_91, listed=(1.0,)):
        """Hana-kun with 0.3.0 having linked HANA_91 as `number`; Suwayomi
        lists the staged files and chapters `listed` (not downloaded)."""
        sid = self.series(con, "Hana-kun", 5)
        p = self.source(con, sid, MN, "Himokuzu", 51, [name, "Chapter 9.cbz"])
        self.link_as(con, sid, f"{p}/Chapter 9.cbz", 9.0, MN)
        dst = self.link_as(con, sid, f"{p}/{name}", number, MN)
        con.commit()
        chapters = listing(51, {name: 9.1, "Chapter 9.cbz": 9.0}) + [Chapter(51900 + i, n, f"Chapter {n:g}", None, False)
                                                                     for i, n in enumerate(listed)]
        return sid, f"{p}/{name}", dst, FakeSuwayomi({51: chapters})

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

    def test_a_chapter_on_disk_from_another_source_is_linked_from_its_own_file(self):
        """0.3.0 took the spin-off for chapter 1 while another source has the
        real chapter 1: the spin-off's link goes, and the import links both
        files under their own numbers."""
        with db.connect() as con:
            sid = self.series(con, "Dreaming Freedom", 4)
            spin = f"{NATO}_Chapter 171.01_ Spin-off 1.cbz"
            mn = self.source(con, sid, MN, "Dreaming Freedom", 41, [spin])
            wc = self.source(con, sid, "Weeb Central (EN)", "Dreaming Freedom", 42, ["Chapter 1.cbz"])
            dst = self.link_as(con, sid, f"{mn}/{spin}", 1.0, MN)
            con.commit()
            fake = FakeSuwayomi({41: listing(41, {spin: 171.01}), 42: listing(42, {"Chapter 1.cbz": 1.0})})
            with self.assertLogs("mangarr.relink", "WARNING"):
                self.assertEqual(relink.check_links(con, fake).message, "1 misread link(s) repaired; 2 chapter(s) "
                                                                        "imported")
            rows = self.rows(con, sid)
            self.assertEqual(rows[1.0]["staging_path"], f"{wc}/Chapter 1.cbz")
            self.assertEqual(inode(dst), inode(f"{wc}/Chapter 1.cbz"))
            self.assertEqual(rows[171.01]["staging_path"], f"{mn}/{spin}")
            self.assertEqual(self.events(con, "relinked"), [
                "Chapter 1 was a misread file (it is chapter 171.01); link removed, re-imported as 171.01; chapter 1 "
                "is linked from Chapter 1.cbz now"])

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


class CrossCheckTest(Tree):
    """A link is only removed when 0.3.0's reading of its staging name is the
    number it is linked as, the parser reads another number now, and
    Suwayomi numbers the file as the parser does."""

    def scanlator(self, con, prefix: str, anilist_id: int, numbers=(1, 2, 3, 4, 5)):
        """A series whose chapters `prefix`_Chapter N.cbz 0.3.0 linked as N."""
        sid = self.series(con, f"Series {anilist_id}", anilist_id)
        names = {f"{prefix}_Chapter {n}.cbz": float(n) for n in numbers}
        p = self.source(con, sid, MN, f"Series {anilist_id}", anilist_id, list(names))
        dsts = {n: self.link_as(con, sid, f"{p}/{name}", n, MN) for name, n in names.items()}
        con.commit()
        return sid, p, dsts, listing(anilist_id, names)

    def test_a_marker_glued_to_a_digit_in_the_prefix_is_no_misread(self):
        """0.3.0 read these right, and so does the parser: nothing is
        touched (the previous parser took the prefix's digit for the chapter
        and the check unlinked every correct file)."""
        with db.connect() as con:
            lists = {}
            made = {}
            for i, prefix in enumerate(("Ch4os Scans", "Ep1c TL", "www.chapter1.com", "ep2 fans", "Ch 3 Scans")):
                sid, _, dsts, chapters = self.scanlator(con, prefix, 100 + i)
                lists[100 + i], made[sid] = chapters, dsts
            state = {p: inode(p) for dsts in made.values() for p in dsts.values()}
            self.assertEqual(relink.find_misreads(relink.links(con)), [])
            self.assertEqual(relink.check_links(con, FakeSuwayomi(lists)).message,
                             "no misread links; 0 chapter(s) imported")
            self.assertEqual({p: inode(p) for p in state}, state)
            for sid, dsts in made.items():
                self.assertEqual({n: r["status"] for n, r in self.rows(con, sid).items()},
                                 dict.fromkeys(dsts, "have"))
            self.assertEqual(backup.listing(), [])

    def test_a_parser_that_misreads_is_stopped_by_suwayomi(self):
        """Should the parser read the prefix's digit after all ('Ch4os' as
        chapter 4), Suwayomi's numbers stop the repair: nothing removed."""
        with db.connect() as con:
            sid, _, dsts, chapters = self.scanlator(con, "Ch4os Scans", 100)
            state = {p: inode(p) for p in dsts.values()}
            with mock.patch.object(library, "parse_number", lambda name: 4.0):
                self.assertEqual(sorted(m.link.number for m in relink.find_misreads(relink.links(con))),
                                 [1.0, 2.0, 3.0, 5.0])
                with self.assertLogs("mangarr.relink", "WARNING") as cm:
                    msg = relink.check_links(con, FakeSuwayomi({100: chapters})).message
            self.assertEqual(msg, "no misread links; 4 left as they are (Suwayomi does not confirm the new number); "
                                  "0 chapter(s) imported")
            self.assertIn("reads as chapter 4 now, but Suwayomi numbers it 1; left as it is", "\n".join(cm.output))
            self.assertEqual({p: inode(p) for p in state}, state)
            self.assertEqual({n: r["status"] for n, r in self.rows(con, sid).items()}, dict.fromkeys(dsts, "have"))
            self.assertEqual(backup.listing(), [])

    def test_a_link_suwayomi_matched_is_left_alone(self):
        """0.3.0 read the name as no number and Suwayomi's names made the
        link (3.5); the parser's reading (3) says nothing about it."""
        name = "www.x.com_Chapter 3_ Volume 2 Extra.cbz"
        self.assertIsNone(relink._parse_030(name))
        self.assertEqual(library.parse_number(name), 3.0)
        with db.connect() as con:
            sid = self.series(con, "Extra", 9)
            p = self.source(con, sid, MN, "Extra", 91, ["www.x.com_Chapter 3.cbz", name])
            self.link_as(con, sid, f"{p}/www.x.com_Chapter 3.cbz", 3.0, MN)
            dst = self.link_as(con, sid, f"{p}/{name}", 3.5, MN)
            con.commit()
            fake = FakeSuwayomi({91: listing(91, {"www.x.com_Chapter 3.cbz": 3.0, name: 3.5})})
            self.assertEqual(relink.check_links(con, fake).message, "no misread links; 0 chapter(s) imported")
            self.assertTrue(os.path.exists(dst))
            self.assertEqual(self.rows(con, sid)[3.5]["status"], "have")

    def test_suwayomi_numbering_the_file_otherwise_leaves_it(self):
        """Suwayomi numbers Losers' file 2020 itself, or does not list it, or
        no source entry holds its folder: the link stays."""
        for case in ("numbers it 2020", "does not list it", "no entry"):
            with self.subTest(case=case), db.connect() as con:
                sid = self.series(con, f"Kamisama Kiss {case}", 1)
                p = self.source(con, sid, MD, f"Divine Nanami {case}", 11, [LOSERS])
                dst = self.link_as(con, sid, f"{p}/{LOSERS}", 2020.0, MD)
                if case == "no entry":
                    con.execute("UPDATE series_source SET folder=? WHERE series_id=?", (p + "-elsewhere", sid))
                con.commit()
                chapters = listing(11, {LOSERS: 2020.0}) if case == "numbers it 2020" else \
                    listing(11, {"Losers in eXile_Ch.149.cbz": 149.0})
                with self.assertLogs("mangarr.relink", "WARNING") as cm:
                    result = relink.check_links(con, FakeSuwayomi({11: chapters}))
                self.assertEqual((result.repaired, result.left), ([], 1))
                self.assertIn("left as it is", "\n".join(cm.output))
                self.assertTrue(os.path.exists(dst))
                self.assertEqual(self.rows(con, sid)[2020.0]["status"], "have")
                con.execute("DELETE FROM series WHERE id=?", (sid,))
                os.remove(dst)
                con.commit()

    def test_the_name_suwayomi_writes_matches_despite_its_sanitising(self):
        """number_in: the exact name Suwayomi writes, else the same letters,
        digits and dots; a name two chapters share says nothing."""
        chapters = [Chapter(1, 17.0, "Ch.17 - Maidens 101: A Success!", "Humane Scans", True),
                    Chapter(2, 5.0, "Oneshot", "Team", True), Chapter(3, 6.0, "Oneshot", "Team", True)]
        self.assertEqual(relink.number_in(chapters, HUMANE), 17.0)
        self.assertEqual(relink.number_in(chapters, "Humane Scans_Ch.17 - Maidens 101  A Success.cbz"), 17.0)
        self.assertIsNone(relink.number_in(chapters, "Team_Oneshot.cbz"))
        self.assertIsNone(relink.number_in(chapters, "Humane Scans_Ch.18.cbz"))

    def test_the_reference_reading_is_0_3_0s(self):
        """_parse_030 reads the real names as 0.3.0 did (its misreads too)."""
        for name, old in ((LOSERS, 2020.0), (HUMANE, 101.0), (ANON, 2026.0), (HANA_91, 1.0),
                          (f"{NATO}_Chapter 171.1_ Spin-off 10.cbz", 10.0), (f"{NATO}_Chapter 0_ Volume 10.cbz", None),
                          (f"{NATO}_Chapter 16.1_ (1r0n).cbz", 0.0), ("Ch4os Scans_Chapter 12.cbz", 12.0),
                          ("Official_S2 - Episode 5.cbz", None), ("Unknown_Vol.15 Ch.88.5.cbz", 88.5)):
            with self.subTest(name=name):
                self.assertEqual(relink._parse_030(name), old)


class LeftoversTest(LiveBox):
    """Backlog 12 (b), (c) and (d): what the verification of 0.3.1 left."""

    def test_a_source_entry_that_cannot_be_listed_does_not_put_a_repair_off(self):
        class Stale(FakeSuwayomi):
            def chapters(self, manga_id):
                if manga_id == 12:                  # Kamisama Kiss on Weeb Central: Suwayomi no longer has it
                    self.asked.append(manga_id)
                    raise QueryError("no such manga")
                return super().chapters(manga_id)
        with db.connect() as con:
            fake = self.build(con)
            stale = Stale(fake.lists)
            con.execute("INSERT INTO maintenance (name, due_at) VALUES (?, ?)", (relink.TASK, db.now()))
            con.commit()
        msg = relink.run_if_due(stale)
        self.assertIn("3 misread link(s) repaired", msg)
        self.assertNotIn("left for a later run", msg)
        with db.connect() as con:
            rows = self.rows(con, self.ids["Kamisama Kiss"])
            self.assertNotIn(2020.0, rows)
            self.assertEqual(rows[150.0]["status"], "have")
            self.assertEqual(db.maintenance_due(con), set())        # done: it does not run before every cycle

    def test_suwayomi_not_answering_about_the_files_still_puts_it_off(self):
        with db.connect() as con:
            fake = self.build(con)
            fake.down = True
            result = relink.check_links(con, fake)
            self.assertEqual((result.repaired, result.deferred), ([], 3))
            self.assertIn(2020.0, self.rows(con, self.ids["Kamisama Kiss"]))

    def test_komga_is_asked_to_scan_after_the_last_link_is_removed(self):
        order = []
        real = relink._unlink

        def unlink(m):
            order.append("remove")
            return real(m)
        with db.connect() as con, mock.patch.object(relink, "_unlink", unlink), \
                mock.patch.object(komga, "scan", lambda *a: order.append("scan") or True):
            fake = self.build(con)
            result = relink.check_links(con, fake)
        self.assertGreater(result.imported, 0)          # imports asked for scans of their own, earlier
        self.assertEqual(order.count("remove"), 3)
        self.assertEqual(order[-1], "scan")
        self.assertGreater(len(order) - 1 - order[::-1].index("remove"), -1)
        self.assertLess(max(i for i, x in enumerate(order) if x == "remove"), len(order) - 1)

    def test_a_file_of_another_group_than_the_one_suwayomi_keeps_is_found(self):
        class Groups(FakeSuwayomi):
            """chapters() keeps one chapter per number, as Client.chapters does (the first listed here);
            chapters_all() lists every group's."""

            def chapters_all(self, manga_id):
                return super().chapters(manga_id)

            def chapters(self, manga_id):
                kept = {}
                for c in super().chapters(manga_id):
                    kept.setdefault(c.number, c)
                return list(kept.values())
        with db.connect() as con:
            fake = self.build(con)
            big = [Chapter(11900 + n, float(n), f"Ch.{n}", "Big Group", True) for n in (88.5, 149, 150)]
            lists = dict(fake.lists)
            lists[11] = big + lists[11]             # Big Group's chapter 150 is listed first: the one kept
            groups = Groups(lists)
            self.assertIsNone(relink.number_in(groups.chapters(11), LOSERS))       # what went wrong
            self.assertEqual(relink.number_in(groups.chapters_all(11), LOSERS), 150.0)
            msg = relink.check_links(con, groups).message
            self.assertIn("3 misread link(s) repaired", msg)
            rows = self.rows(con, self.ids["Kamisama Kiss"])
            self.assertNotIn(2020.0, rows)
            self.assertEqual(rows[150.0]["status"], "have")


class GoneFileTest(LiveBox):
    """A 'have' chapter whose library file is gone, and that the import
    cannot link again, goes back to what the plan says."""

    def test_a_wrong_file_removed_by_hand(self):
        with db.connect() as con:
            fake = self.build(con)
            ks = self.ids["Kamisama Kiss"]
            os.remove(self.lib_path(con, ks, "Chapter 2020.0.cbz"))
            with self.assertLogs("mangarr.relink", "WARNING"):
                msg = relink.check_links(con, fake).message
            self.assertEqual(msg, "2 misread link(s) repaired; 1 chapter(s) whose library file was gone set back; "
                                  "17 chapter(s) imported")
            rows = self.rows(con, ks)
            self.assertNotIn(2020.0, rows)
            self.assertEqual((rows[150.0]["status"], os.path.basename(rows[150.0]["staging_path"])), ("have", LOSERS))
            self.assertEqual(self.events(con, "gone"), [
                "Chapter 2020's library file Chapter 2020.0.cbz is gone and no downloaded file could be linked as "
                "chapter 2020; removed: no source lists it"])
            self.assertEqual(len(backup.listing()), 1)

    def test_the_backup_of_before_the_repair_restored(self):
        """The backup the repair wrote is restored: its rows still say 2020,
        101 and 2026 (and the check is due again), whose files are gone."""
        with db.connect() as con:
            fake = self.build(con)
            con.execute("INSERT INTO maintenance (name, due_at) VALUES (?, ?)", (relink.TASK, db.now()))
        with self.assertLogs("mangarr.relink", "WARNING"):
            relink.run_if_due(fake)
        (b,) = backup.listing()
        with self.assertLogs("mangarr.backup", "INFO"):
            backup.restore(backup.path_of(b["name"]))
        with db.connect() as con:
            self.assertEqual(db.maintenance_due(con), {relink.TASK})
            self.assertEqual(self.rows(con, self.ids["Kamisama Kiss"])[2020.0]["status"], "have")
        with self.assertLogs("mangarr.relink", "WARNING"):
            msg = relink.run_if_due(fake)
        self.assertEqual(msg, "no misread links; 3 chapter(s) whose library file was gone set back; 17 chapter(s) "
                              "imported")
        with db.connect() as con:
            for title, wrong, right in (("Kamisama Kiss", 2020.0, 150.0), ("The Dangers in My Heart", 2026.0, 185.5),
                                        ("Ohitorisama ni wa Naremashita node.: Kon'yakusha Houchi-chuu!", 101.0,
                                         17.0)):
                rows = self.rows(con, self.ids[title])
                self.assertNotIn(wrong, rows)
                self.assertEqual(rows[right]["status"], "have")
                self.assertTrue(os.path.exists(rows[right]["library_path"]))
            self.assertEqual(len(self.events(con, "gone")), 3)
            self.assertEqual(db.maintenance_due(con), set())

    def test_a_right_file_removed_is_linked_again(self):
        with db.connect() as con:
            fake = self.build(con)
            ks = self.ids["Kamisama Kiss"]
            lib149 = self.lib_path(con, ks, "Chapter 149.0.cbz")
            os.remove(lib149)
            with self.assertLogs("mangarr.relink", "WARNING"):
                relink.check_links(con, fake)
            self.assertEqual(self.rows(con, ks)[149.0]["status"], "have")
            self.assertTrue(os.path.exists(lib149))
            self.assertEqual(self.events(con, "gone"), [])

    def test_both_files_gone_is_wanted_again(self):
        """Its staging file went too: a source lists 149, so it is wanted."""
        with db.connect() as con:
            fake = self.build(con)
            ks = self.ids["Kamisama Kiss"]
            row = self.rows(con, ks)[149.0]
            os.remove(row["library_path"])
            os.remove(row["staging_path"])
            with self.assertLogs("mangarr.relink", "WARNING"):
                relink.check_links(con, fake)
            row = self.rows(con, ks)[149.0]
            self.assertEqual((row["status"], row["library_path"], row["staging_path"]), ("wanted", None, None))
            self.assertIn("its library file Chapter 149.0.cbz was gone", row["reason"])
            self.assertEqual(self.events(con, "gone"), [
                "Chapter 149's library file Chapter 149.0.cbz is gone and no downloaded file could be linked as "
                "chapter 149; wanted again"])

    def test_a_library_folder_that_is_not_there_is_left_alone(self):
        """Not every file of a series gone at once: a library not mounted."""
        import shutil
        with db.connect() as con:
            fake = self.build(con)
            ks = self.ids["Kamisama Kiss"]
            shutil.rmtree(os.path.join(self.lib, db.get_series(con, ks)["folder"]))
            con.execute("DELETE FROM series_source WHERE series_id=?", (ks,))      # nothing to link it again from
            con.commit()
            with self.assertLogs("mangarr.relink", "WARNING") as cm:
                relink.check_links(con, fake)
            self.assertIn("is the library mounted?", "\n".join(cm.output))
            self.assertEqual({r["status"] for r in self.rows(con, ks).values()}, {"have"})
            self.assertEqual(self.events(con, "gone"), [])


class BackupAndKomgaTest(LiveBox):
    def test_no_backup_no_change(self):
        """The backup before the first change fails: nothing is changed and
        the check stays due."""
        with db.connect() as con:
            fake = self.build(con)
            con.execute("INSERT INTO maintenance (name, due_at) VALUES (?, ?)", (relink.TASK, db.now()))
            before = {p: inode(p) for p in self.library_files()}
            rows = {sid: self.rows(con, sid) for sid in self.ids.values()}
        with mock.patch.object(backup, "create", side_effect=OSError("No space left on device")), \
                self.assertRaises(relink.RepairAborted) as cm:
            relink.run_if_due(fake)
        self.assertIn("nothing changed", str(cm.exception))
        self.assertEqual({p: inode(p) for p in self.library_files()}, before)
        with db.connect() as con:
            self.assertEqual({sid: self.rows(con, sid) for sid in self.ids.values()}, rows)
            self.assertEqual(db.maintenance_due(con), {relink.TASK})

    def library_files(self) -> list[str]:
        return [os.path.join(d, f) for d, _, fs in os.walk(self.lib) for f in fs]

    def test_komga_not_answering_is_asked_once_more(self):
        """At the next import (nothing linked there) or after a while."""
        scans = []
        timers = []

        class Timer:
            def __init__(self, delay, fn):
                timers.append(self)
                self.fn, self.daemon, self.cancelled = fn, False, False

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True
        with db.connect() as con:
            fake = self.build(con)
            with mock.patch.object(komga, "scan", lambda *a: scans.append("scan") and False), \
                    mock.patch.object(komga, "configured", lambda: True), \
                    mock.patch.object(komga.threading, "Timer", Timer), \
                    self.assertLogs("mangarr", "WARNING") as cm:
                relink.check_links(con, fake)
                asked = len(scans)
                self.assertEqual(len(timers), 1)                        # one retry waits, however many scans failed
                self.assertIn("trying once more", "\n".join(cm.output))
                self.assertEqual(core.import_series(con, self.ids["Kamisama Kiss"], fake), 0)
                self.assertEqual(len(scans), asked + 1)                 # the retry, at the next import
                self.assertTrue(timers[0].cancelled)
                core.import_series(con, self.ids["Kamisama Kiss"], fake)
                self.assertEqual(len(scans), asked + 1)                 # only once
                self.assertIsNone(komga.retry_now())

    def test_komga_retry_after_a_while(self):
        scans = []
        with mock.patch.object(komga, "configured", lambda: True), \
                mock.patch.object(komga, "RETRY_SECONDS", 3600):
            with mock.patch.object(komga, "scan", lambda *a: scans.append(1) and False), \
                    self.assertLogs("mangarr.komga", "WARNING"):
                self.assertFalse(komga.scan_retrying())
            with mock.patch.object(komga, "scan", lambda *a: scans.append(1) or True):
                self.assertTrue(komga.retry_now())                      # what the timer calls
                self.assertIsNone(komga.retry_now())
            self.assertEqual(len(scans), 2)


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


    def test_a_check_suwayomi_did_not_answer_is_queued_again_after_a_pass(self):
        with db.connect() as con:
            fake = self.build(con)
        fake.down = True
        job = jobs.Job(1, "check-links", "check library links")
        with mock.patch.object(web, "client", fake), mock.patch.object(web, "with_cancel", lambda c, f: c), \
                self.assertLogs("mangarr", "WARNING"):
            self.assertIn("3 left for a later run (Suwayomi did not answer)", web._job_check_links(job))
        with db.connect() as con:
            self.assertEqual(db.maintenance_due(con), {relink.TASK})
        with mock.patch.object(web, "_refresh_all", lambda job: "pass done"):
            self.assertEqual(web._job_refresh_all(jobs.Job(2, "refresh-all", "all")), "pass done")
        self.assertEqual(self.queued(), ["check-links"])


class WorkerTest(unittest.TestCase):
    def test_the_worker_runs_a_due_check_before_every_cycle(self):
        import signal

        from mangarr import daemon
        calls = []
        handlers = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, handlers[1])
        self.addCleanup(signal.signal, signal.SIGTERM, handlers[0])
        with mock.patch.object(daemon, "cycle", lambda c: calls.append("cycle")), \
                mock.patch.object(relink, "run_if_due", lambda *a, **k: calls.append("check")), \
                mock.patch.object(daemon.stuck.fetcher, "start", lambda: None), \
                mock.patch.object(daemon.notify, "flush", lambda: None), \
                mock.patch.object(daemon.limits, "clamp", lambda k, v: 1.0):
            daemon.run(once=True)
        self.assertEqual(calls, ["check", "cycle"])


class SchemaTest(unittest.TestCase):
    def test_new_database_has_nothing_due(self):
        with tempfile.TemporaryDirectory() as d, db.connect(os.path.join(d, "t.db")) as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
            self.assertEqual(db.maintenance_due(con), set())
            db.maintenance_done(con, relink.TASK)
            self.assertEqual(db.maintenance_due(con), set())


if __name__ == "__main__":
    unittest.main()
