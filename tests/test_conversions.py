"""E-reader conversion around the engine (conversions.py): targets, the
queue and what reconcile makes of the library, one conversion in a process
of its own and what each way of failing leads to, converted copies that
follow a rename, and the pages and API. Everything happens in a temporary
folder. The tests that convert for real need Pillow; the rest replace the
worker process with a small script."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mangarr import conversions, convert, core, db, health, library, renamer, settings  # noqa: E402
from mangarr.model import Series  # noqa: E402

PILLOW = convert.available()[0]
if PILLOW:
    import convert_fixtures as fx  # noqa: E402

try:
    from fastapi.testclient import TestClient

    from mangarr.web import app as web
except (ImportError, RuntimeError):      # web extras not installed
    TestClient = web = None


def fake_worker(code: int, answer: dict | None = None, sleep: float = 0.0, write: bytes = b"book"):
    """A stand-in for the worker process: writes `write` to the job's
    output, says `answer` and ends with `code` after `sleep` seconds."""
    def spawn(job: dict):
        script = ("import json, sys, time\n"
                  f"time.sleep({sleep})\n"
                  f"open({job['out']!r}, 'wb').write({write!r})\n"
                  f"print(json.dumps({answer or {}!r}))\n"
                  f"sys.exit({code})\n")
        spawn.jobs.append(job)
        return subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, text=True)
    spawn.jobs = []
    return spawn


OK = {"ok": True, "format": "epub", "profile": "generic", "pages": 3, "bytes": 4, "seconds": 0.1, "rtl": True,
      "webtoon": False, "decided": "paged", "engine": 1}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.library = os.path.join(tmp.name, "library")
        self.staging = os.path.join(tmp.name, "staging")
        self.out = os.path.join(tmp.name, "converted")
        for d in (self.library, self.staging):
            os.makedirs(d)
        for p in (mock.patch("mangarr.config.DATA_DIR", self.root),
                  mock.patch("mangarr.config.DB_PATH", os.path.join(tmp.name, "t.db")),
                  mock.patch("mangarr.config.LOCK_PATH", os.path.join(tmp.name, "lock")),
                  mock.patch("mangarr.config.LIBRARY_ROOT", self.library),
                  mock.patch("mangarr.config.STAGING_ROOT", self.staging),
                  mock.patch("mangarr.config.CONVERTED_ROOT", self.out),
                  mock.patch.object(core.komga, "scan_retrying", lambda *a, **k: False),
                  mock.patch.object(conversions.service, "notify", lambda sid: None)):
            p.start()
            self.addCleanup(p.stop)
        settings._cache.clear()
        self.addCleanup(settings._cache.clear)
        with db.connect() as con:
            settings.set_many(con, {"convert_enabled": True})
        settings._cache.clear()

    def series(self, title="Test Series", numbers=(1.0, 2.0), country="JP", anilist_id=1, real=False) -> int:
        with db.connect() as con:
            sid = db.upsert_series(con, Series(anilist_id=anilist_id, english=title, country=country,
                                               authors=["Someone"], description="A <b>story</b>."))
            d = library.library_dir(db.get_series(con, sid)["folder"])
            os.makedirs(d, exist_ok=True)
            for n in numbers:
                path = os.path.join(d, library.chapter_filename(n, None))
                if real:
                    fx.manga_cbz(path)
                else:
                    with zipfile.ZipFile(path, "w") as z:
                        z.writestr("001.jpg", b"not an image")
                db.set_have(con, sid, n, path, path, "Source")
            db._backfill_file_titles(con)
        return sid

    def target(self, **kw) -> int:
        with db.connect() as con:
            return conversions.add_target(con, {"name": "Any reader", "scope": "all", **kw})

    def rows(self) -> dict:
        with db.connect() as con:
            return {(r["series_id"], r["number"], r["target_id"]): dict(r)
                    for r in con.execute("SELECT * FROM conversion")}

    def work(self, spawn=None) -> list[str]:
        """Reconcile and run the queue to its end."""
        out = []
        with db.connect() as con, mock.patch.object(conversions, "_spawn", spawn):
            conversions.reconcile(con)
            while (r := conversions.claim_next(con)) is not None:
                out.append(conversions.run_one(con, r))
                if out[-1] in ("full", "stopped"):      # the chapter waits again: the service stops here too
                    break
        return out

    def files(self) -> list[str]:
        return sorted(os.path.relpath(os.path.join(d, f), self.out) for d, _, ff in os.walk(self.out) for f in ff)


class TargetTest(Base):
    def test_what_a_target_may_be(self):
        ok = conversions.validate_target({"name": "  My   Kobo ", "profile": "kobo-libra-colour"})
        self.assertEqual((ok["name"], ok["format"], ok["folder"], ok["scope"], ok["enabled"]),
                         ("My Kobo", "kepub", "kepub", "all", True))
        for bad, why in (({"name": ""}, "Name"), ({"name": "x" * 61}, "Name"), ({"name": "a\x00b"}, "Name"),
                         ({"name": "x", "profile": "kindle"}, "Device"),
                         ({"name": "x", "format": "kepub"}, "Format"),            # not offered for generic
                         ({"name": "x", "format": "mobi"}, "Format"),
                         ({"name": "x", "folder": ".."}, "Folder"), ({"name": "x", "folder": "a/b"}, "Folder"),
                         ({"name": "x", "folder": ".hidden"}, "Folder"), ({"name": "x", "folder": "/abs"}, "Folder"),
                         ({"name": "x", "scope": "some"}, "Existing"),
                         ({"name": "x", "options": {"gamma": 9}}, "gamma"),
                         ({"name": "x", "options": {"nope": 1}}, "unknown option"),
                         ({"name": "x", "options": {"quality": 10}}, "quality"), ("x", "fields")):
            with self.subTest(bad=bad), self.assertRaisesRegex(conversions.TargetError, why):
                conversions.validate_target(bad)
        with self.assertRaisesRegex(conversions.TargetError, "another target"):
            conversions.validate_target({"name": "x", "folder": "EPUB"}, taken=["epub"])

    def test_at_most_eight_and_no_shared_folder(self):
        for k in range(conversions.MAX_TARGETS):
            self.target(name=f"T{k}", folder=f"f{k}")
        with self.assertRaisesRegex(conversions.TargetError, "at most 8"):
            self.target(name="one more", folder="f9")
        with db.connect() as con, self.assertRaisesRegex(conversions.TargetError, "another target"):
            conversions.update_target(con, 1, {"folder": "F1"})

    def test_the_output_folder_must_be_outside_the_library(self):
        self.assertIsNone(conversions.root_problem())
        with mock.patch("mangarr.config.CONVERTED_ROOT", os.path.join(self.library, "epub")):
            self.assertIn("inside the library folder", conversions.root_problem())
            self.series()
            self.target()
            self.assertEqual(self.work(fake_worker(0, OK)), [])                  # nothing is even queued
        with mock.patch("mangarr.config.CONVERTED_ROOT", self.root):
            self.assertIn("holds the library folder", conversions.root_problem())

    def test_the_default_target_is_an_epub_for_any_reader(self):
        t = conversions.validate_target(conversions.default_target())
        self.assertEqual((t["name"], t["profile"], t["format"], t["folder"], t["scope"]),
                         ("Generic EPUB (any reader)", "generic", "epub", "epub", "new"))


class QueueTest(Base):
    def test_existing_chapters_all_or_only_new(self):
        sid = self.series()
        every = self.target(name="All", folder="all", scope="all")
        new = self.target(name="New", folder="new", scope="new")
        self.assertEqual({k[2]: v["status"] for k, v in self.rows().items()}, {new: "skipped"})
        self.assertEqual(self.work(fake_worker(0, OK)), ["done", "done"])
        got = self.rows()
        self.assertEqual({(k[1], k[2]): v["status"] for k, v in got.items()},
                         {(1.0, every): "done", (2.0, every): "done", (1.0, new): "skipped", (2.0, new): "skipped"})
        self.assertEqual(self.files(), ["all/Test Series/Test Series - Chapter 001.0.epub",
                                        "all/Test Series/Test Series - Chapter 002.0.epub"])
        # a chapter that arrives later is converted for both
        with db.connect() as con:
            d = library.library_dir(db.get_series(con, sid)["folder"])
            path = os.path.join(d, "Chapter 003.0.cbz")
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("1.jpg", b"x")
            db.set_have(con, sid, 3.0, path, path, "Source")
        self.assertEqual(self.work(fake_worker(0, OK)), ["done", "done"])
        # Convert existing chapters takes the held ones
        with db.connect() as con:
            self.assertEqual(conversions.queue_existing(con, new), 2)
        self.assertEqual(self.work(fake_worker(0, OK)), ["done", "done"])
        self.assertEqual(len(self.files()), 6)

    def test_the_most_urgent_first(self):
        self.series(numbers=(1.0, 2.0, 3.0))
        tid = self.target()
        with db.connect() as con:
            conversions.reconcile(con)
            con.execute("UPDATE conversion SET priority=0 WHERE number=3")
            con.execute("UPDATE conversion SET priority=1 WHERE number=2")
            con.commit()
            order = []
            while (r := conversions.claim_next(con)) is not None:
                order.append(r["number"])
                self.assertIsNone(conversions.claim_next(con) if False else None)
            self.assertEqual(order, [3.0, 2.0, 1.0])
            self.assertEqual(conversions.release_stale(con, all_running=True), 3)
            con.execute("UPDATE convert_target SET enabled=0 WHERE id=?", (tid,))
            con.commit()
            self.assertIsNone(conversions.claim_next(con))                      # a disabled target gets no work

    def test_what_the_worker_is_told(self):
        self.series(country="KR")
        self.target(options={"crop": False})
        spawn = fake_worker(0, OK)
        self.work(spawn)
        job = spawn.jobs[0]
        self.assertEqual((job["rtl"], job["webtoon"], job["hints"]), (False, None, {"long_strip": None,
                                                                                  "country": "KR"}))
        self.assertEqual(job["meta"], {"series": "Test Series", "number": 1.0, "chapter": "", "authors": ["Someone"],
                                       "description": "A story ."})
        self.assertEqual((job["options"]["crop"], job["options"]["profile"], job["threads"], job["memory_mb"]),
                         (False, "generic", 2, 1536))
        self.assertTrue(library.is_within(job["out"], self.out))
        self.assertTrue(os.path.basename(job["out"]).startswith(".mangarr-"))
        row = next(iter(self.rows().values()))
        self.assertIn("direction: Korea", row["decided"])

    def test_how_a_series_is_read(self):
        def s(**kw):
            return {"reading_direction": "auto", "layout": "auto", "country": None, "format": None, **kw}

        class Row(dict):
            pass
        self.assertEqual(conversions.direction(Row(s(country="JP"))), (True, "Japan"))
        self.assertEqual(conversions.direction(Row(s(country="kr"))), (False, "Korea"))
        self.assertEqual(conversions.direction(Row(s(country="US")))[0], False)
        self.assertEqual(conversions.direction(Row(s()))[0], True)               # unknown: like a manga
        self.assertEqual(conversions.direction(Row(s(country="JP", reading_direction="ltr"))),
                         (False, "set on this series"))
        self.assertEqual(conversions.layout(Row(s()))[0], None)
        self.assertEqual(conversions.layout(Row(s(layout="webtoon")))[0], True)
        self.assertEqual(conversions.layout(Row(s(layout="paged")))[0], False)
        self.assertEqual(conversions.layout(Row(s(format="WEBTOON", country="KR")))[1],
                         {"long_strip": True, "country": "KR"})

    def test_failures(self):
        self.series(numbers=(1.0,))
        self.target()
        key = (1, 1.0, 1)
        for code, answer, status, again in ((2, {"ok": False, "error": "003.jpg: truncated"}, "failed", False),
                                            (1, {"ok": False, "error": "odd"}, "failed", True),
                                            (1, {}, "failed", True)):
            with self.subTest(code=code):
                with db.connect() as con:
                    con.execute("DELETE FROM conversion")
                    con.commit()
                self.assertEqual(self.work(fake_worker(code, answer)), ["failed"])
                row = self.rows()[key]
                self.assertEqual((row["status"], bool(row["next_try"]), row["tries"]), (status, again, 1))
                self.assertTrue(row["reason"])
                self.assertEqual(self.files(), [])                               # nothing is left behind
        # the disk is full: the chapter waits, the caller pauses
        with db.connect() as con:
            con.execute("DELETE FROM conversion")
            con.commit()
        self.assertEqual(self.work(fake_worker(4, {"ok": False, "error": "no space"})), ["full"])
        self.assertEqual((self.rows()[key]["status"], self.files()), ("pending", []))

    def test_out_of_memory_is_tried_once_more_with_one_thread(self):
        self.series(numbers=(1.0,))
        self.target()
        spawn = fake_worker(3, {"ok": False, "error": "out of memory"})
        self.assertEqual(self.work(spawn), ["failed"])
        self.assertEqual([j["threads"] for j in spawn.jobs], [2, 1])

    def test_a_failure_is_tried_again_when_it_is_due_and_gives_up_in_the_end(self):
        self.series(numbers=(1.0,))
        self.target()
        key = (1, 1.0, 1)
        for tries in range(1, len(conversions.RETRY_MINUTES) + 2):
            self.assertEqual(self.work(fake_worker(1, {"ok": False, "error": "odd"})), ["failed"])
            row = self.rows()[key]
            self.assertEqual(row["tries"], tries)
            if tries <= len(conversions.RETRY_MINUTES):
                self.assertTrue(row["next_try"])
                self.assertEqual(self.work(fake_worker(0, OK)), [])              # not due yet
                with db.connect() as con:
                    con.execute("UPDATE conversion SET next_try='2000-01-01 00:00:00'")
                    con.commit()
        self.assertIsNone(self.rows()[key]["next_try"])                          # it stays failed
        with db.connect() as con:
            self.assertEqual(conversions.queue_existing(con, 1), 1)              # until you ask
        self.assertEqual(self.work(fake_worker(0, OK)), ["done"])

    def test_one_that_takes_too_long_is_stopped(self):
        self.series(numbers=(1.0,))
        self.target()
        t0 = time.monotonic()
        with mock.patch.object(conversions.limits, "setting",
                               lambda k: 0.01 if k == "convert_timeout_minutes" else conversions.limits.clamp(
                                   k, settings.DEFAULTS[k])):
            self.assertEqual(self.work(fake_worker(0, OK, sleep=30)), ["failed"])
        self.assertLess(time.monotonic() - t0, 20)
        self.assertIn("took longer", self.rows()[(1, 1.0, 1)]["reason"])
        self.assertEqual(self.files(), [])

    def test_a_stop_queues_it_again(self):
        self.series(numbers=(1.0,))
        self.target()
        with db.connect() as con, mock.patch.object(conversions, "_spawn", fake_worker(0, OK, sleep=30)):
            conversions.reconcile(con)
            r = conversions.claim_next(con)
            self.assertEqual(conversions.run_one(con, r, stop=lambda: True), "stopped")
        self.assertEqual((self.rows()[(1, 1.0, 1)]["status"], self.files()), ("pending", []))

    def test_what_changed_is_made_again(self):
        sid = self.series()
        tid = self.target()
        self.work(fake_worker(0, OK))
        with db.connect() as con:
            self.assertEqual(sum(conversions.reconcile(con).values()), 0)        # nothing to do
            path = con.execute("SELECT library_path FROM chapter WHERE number=1").fetchone()[0]
        with zipfile.ZipFile(path, "w") as z:                                  # the library file was replaced
            z.writestr("1.jpg", b"another")
        with db.connect() as con:
            self.assertEqual(conversions.reconcile(con)["changed"], 1)
            self.assertEqual(self.rows()[(sid, 1.0, tid)]["priority"], 1)
        self.work(fake_worker(0, OK))
        with db.connect() as con:                                              # its options
            conversions.update_target(con, tid, {"options": {"crop": False}})
            self.assertEqual(conversions.reconcile(con)["changed"], 2)
        self.work(fake_worker(0, OK))
        with db.connect() as con:                                              # how the series is read
            con.execute("UPDATE series SET reading_direction='ltr' WHERE id=?", (sid,))
            self.assertEqual(conversions.reconcile(con)["changed"], 2)
        self.work(fake_worker(0, OK))
        os.unlink(os.path.join(self.out, self.files()[0]))                     # a copy is gone
        with db.connect() as con:
            self.assertEqual(conversions.reconcile(con)["changed"], 1)

    def test_a_chapter_that_left_the_library_takes_its_copy_along(self):
        sid = self.series()
        self.target()
        self.work(fake_worker(0, OK))
        stray = os.path.join(self.out, "epub", "Test Series", "mine.epub")
        with open(stray, "w") as f:
            f.write("not mang-arr's")
        with db.connect() as con:
            con.execute("UPDATE chapter SET status='wanted', library_path=NULL WHERE series_id=? AND number=1", (sid,))
            self.assertEqual(conversions.reconcile(con)["removed"], 1)
        self.assertEqual(self.files(), ["epub/Test Series/Test Series - Chapter 002.0.epub",
                                        "epub/Test Series/mine.epub"])          # a file it did not make stays
        self.assertEqual(list(self.rows()), [(sid, 2.0, 1)])

    def test_only_recorded_files_inside_the_target_are_removed(self):
        sid = self.series(numbers=(1.0,))
        tid = self.target()
        self.work(fake_worker(0, OK))
        outside = os.path.join(self.root, "precious.txt")
        with open(outside, "w") as f:
            f.write("keep")
        with db.connect() as con:
            con.execute("UPDATE conversion SET output_path=?", (outside,))      # as a restored backup could say
            con.commit()
            self.assertEqual(conversions.remove_series_outputs(con, sid), 0)
            self.assertEqual(conversions.delete_target(con, tid, delete_files=True), 0)
        self.assertTrue(os.path.exists(outside))

    def test_deleting_a_series_with_its_files_removes_its_copies(self):
        sid = self.series()
        other = self.series("Other", anilist_id=2)
        self.target()
        self.work(fake_worker(0, OK))
        with db.connect() as con:
            core.delete_series(con, mock.Mock(), sid, delete_library=True)
        self.assertEqual(self.files(), ["epub/Other/Other - Chapter 001.0.epub", "epub/Other/Other - Chapter 002.0.epub"])
        self.assertEqual({k[0] for k in self.rows()}, {other})

    def test_a_series_that_is_not_converted(self):
        sid = self.series()
        self.target()
        with db.connect() as con:
            con.execute("UPDATE series SET convert_enabled=0 WHERE id=?", (sid,))
        self.assertEqual(self.work(fake_worker(0, OK)), [])

    def test_switched_off_nothing_new_is_queued(self):
        self.series()
        self.target()
        with db.connect() as con:
            settings.set_many(con, {"convert_enabled": False})
        settings._cache.clear()
        self.assertEqual(self.work(fake_worker(0, OK)), [])

    def test_a_target_folder_renamed_takes_its_copies_along(self):
        self.series(numbers=(1.0,))
        tid = self.target()
        self.work(fake_worker(0, OK))
        with db.connect() as con:
            conversions.update_target(con, tid, {"folder": "books"})
            self.assertEqual(sum(conversions.reconcile(con).values()), 0)
        self.assertEqual(self.files(), ["books/Test Series/Test Series - Chapter 001.0.epub"])
        self.assertTrue(os.path.exists(self.rows()[(1, 1.0, tid)]["output_path"]))
        os.makedirs(os.path.join(self.out, "taken"))
        with db.connect() as con, self.assertRaisesRegex(conversions.TargetError, "already in the output folder"):
            conversions.update_target(con, tid, {"folder": "taken"})


class RenameTest(Base):
    """User decision 2: converted copies are renamed with their library files."""

    def test_copies_follow_a_rename_and_its_undo(self):
        sid = self.series()
        self.target()
        self.work(fake_worker(0, OK))
        before = self.files()
        with db.connect() as con:
            p = renamer.plan(con, sid, {"chapter_file_format": "Ch {Chapter:000}",
                                        "series_folder_format": "{Series Title} ({Year})"}, check_komga=False)
            con.execute("UPDATE series SET year=2020 WHERE id=?", (sid,))
            con.commit()
            p = renamer.plan(con, sid, {"chapter_file_format": "Ch {Chapter:000}",
                                        "series_folder_format": "{Series Title} ({Year})"}, check_komga=False)
        out = renamer.apply(p, confirmed=True)
        self.assertEqual(out["renamed"], 3)                                     # two files and the folder
        self.assertEqual(self.files(), ["epub/Test Series (2020)/Test Series (2020) - Ch 001.epub",
                                        "epub/Test Series (2020)/Test Series (2020) - Ch 002.epub"])
        rows = self.rows()
        for r in rows.values():
            self.assertTrue(os.path.exists(r["output_path"]), r["output_path"])
            self.assertTrue(os.path.exists(r["source_path"]), r["source_path"])
        with db.connect() as con:
            self.assertEqual(sum(conversions.reconcile(con).values()), 0)        # nothing is converted again
        renamer.undo(out["run_id"], confirmed=True)
        self.assertEqual(self.files(), before)
        with db.connect() as con:
            self.assertEqual(sum(conversions.reconcile(con).values()), 0)

    def test_a_copy_is_never_renamed_over_another_file(self):
        sid = self.series(numbers=(1.0,))
        self.target()
        self.work(fake_worker(0, OK))
        taken = os.path.join(self.out, "epub", "Test Series", "Test Series - Ch 001.epub")
        with open(taken, "w") as f:
            f.write("someone else's")
        with db.connect() as con:
            p = renamer.plan(con, sid, {"chapter_file_format": "Ch {Chapter:000}"}, check_komga=False)
        self.assertEqual(renamer.apply(p, confirmed=True)["renamed"], 1)        # the library file is renamed
        with open(taken) as f:
            self.assertEqual(f.read(), "someone else's")
        self.assertIn("epub/Test Series/Test Series - Chapter 001.0.epub", self.files())


@unittest.skipUnless(PILLOW, "Pillow is not installed")
class RealConversionTest(Base):
    def test_a_chapter_becomes_an_epub_that_carries_its_series(self):
        sid = self.series(numbers=(12.5,), real=True)
        self.target()
        self.assertEqual(self.work(), ["done"])
        self.assertEqual(self.files(), ["epub/Test Series/Test Series - Chapter 012.5.epub"])
        row = self.rows()[(sid, 12.5, 1)]
        self.assertEqual((row["status"], row["rtl"], row["webtoon"], row["engine"]), ("done", 1, 0,
                                                                                    convert.ENGINE_VERSION))
        self.assertGreater(row["pages"], 0)
        self.assertEqual(row["bytes"], os.path.getsize(row["output_path"]))
        with zipfile.ZipFile(row["output_path"]) as z:
            self.assertEqual(z.namelist()[0], "mimetype")
            opf = z.read(next(n for n in z.namelist() if n.endswith(".opf"))).decode()
        for needle in ("<dc:title>Test Series - Chapter 12.5", "<dc:creator>Someone</dc:creator>",
                       '<meta property="belongs-to-collection" id="series">Test Series</meta>',
                       '<meta name="calibre:series" content="Test Series"/>',
                       '<meta name="calibre:series_index" content="12.5"/>', 'page-progression-direction="rtl"'):
            self.assertIn(needle, opf)
        self.assertEqual([f for f in os.listdir(os.path.dirname(row["output_path"])) if f.startswith(".")], [])

    def test_a_chapter_that_cannot_be_converted_fails_for_good(self):
        sid = self.series(numbers=(1.0,))                                       # its page is not an image
        self.target()
        self.assertEqual(self.work(), ["failed"])
        row = self.rows()[(sid, 1.0, 1)]
        self.assertEqual((row["status"], row["next_try"]), ("failed", None))
        self.assertIn("001.jpg", row["reason"])
        self.assertEqual(self.files(), [])

    def test_the_worker_gets_nothing_of_the_servers_environment(self):
        with mock.patch.dict(os.environ, {"MANGARR_SECRET_THING": "hunter2"}):
            p = conversions._start_worker({"src": "/nonexistent", "out": "/nonexistent"})
        out = p.stdout.read()
        p.wait(60)
        p.stdout.close()
        p.stderr.close()
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["ok"], False)
        self.assertNotEqual(p.returncode, 0)
        env = dict(x.split("=", 1) for x in subprocess.run(
            ["env"], capture_output=True, text=True, env={"PATH": os.defpath}).stdout.splitlines() if "=" in x)
        self.assertNotIn("MANGARR_SECRET_THING", env)


@unittest.skipIf(web is None, "web extras not installed")
class PagesTest(Base):
    def setUp(self):
        super().setUp()
        for p in (mock.patch("mangarr.web.app.client.sources", lambda *a, **k: []),
                  mock.patch("mangarr.web.app.client.gq", lambda q, **kw: {"downloadStatus": {"state": "STOPPED",
                                                                                              "queue": []}}),
                  mock.patch.object(health, "_ping", lambda name, url, timeout=8: health.Check("ok", name, "stub")),
                  mock.patch.dict(health._cache, {"at": 0.0, "checks": []}),
                  mock.patch.object(conversions.service, "reconcile_soon", lambda: None),
                  mock.patch("mangarr.notify.send", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)

    @staticmethod
    def flash(r) -> str:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["location"]).query)["m"][0]

    def test_switching_it_on_adds_the_epub_target_for_any_reader(self):
        with db.connect() as con:
            settings.set_many(con, {"convert_enabled": False})
        settings._cache.clear()
        self.series()
        html = self.client.get("/settings/media-management").text
        for needle in ('id="conversion"', 'name="convert_enabled"', "No targets yet", 'id="target-new"',
                       "Send to Kindle", 'name="convert_threads"', "Apple Books"):
            self.assertIn(needle, html)
        r = self.client.post("/settings", data={"page": "media-management", "convert_enabled": ["0", "1"]},
                             follow_redirects=False)
        self.assertIn("Generic EPUB (any reader) was added", self.flash(r))
        got = self.client.get("/api/v1/conversion/target").json()
        self.assertEqual([(t["name"], t["format"], t["folder"], t["scope"]) for t in got],
                         [("Generic EPUB (any reader)", "epub", "epub", "new")])
        self.assertEqual(got[0]["counts"]["skipped"], 2)                         # nothing you have is converted yet
        html = self.client.get("/settings/media-management").text
        self.assertIn("Convert existing chapters", html)
        self.assertIn('action="/settings/convert/targets/1/delete"', html)
        r = self.client.post("/settings/convert/targets/1/queue", data={"mode": "missing"}, follow_redirects=False)
        self.assertEqual(self.flash(r), "2 chapter(s) queued for conversion")
        # saving the page again adds no second target
        self.client.post("/settings", data={"page": "media-management", "convert_enabled": ["0", "1"]})
        self.assertEqual(len(self.client.get("/api/v1/conversion/target").json()), 1)

    def test_targets_from_the_page_and_the_api(self):
        r = self.client.post("/settings/convert/targets", data={
            "name": "Kobo", "profile": "kobo-libra-colour", "format": "kepub", "folder": "kobo", "scope": "all",
            "enabled": ["0", "1"], "crop": ["0"], "spreads": "rotate", "gamma": "1.2", "quality": ""},
            follow_redirects=False)
        self.assertIn("target Kobo added", self.flash(r))
        t = self.client.get("/api/v1/conversion/target").json()[0]
        self.assertEqual((t["profile"], t["format"], t["folder"], t["options"]["crop"], t["options"]["spreads"],
                          t["options"]["gamma"], t["options"]["quality"]),
                         ("kobo-libra-colour", "kepub", "kobo", False, "rotate", 1.2, None))
        r = self.client.post("/settings/convert/targets", data={"name": "Bad", "folder": "../x"},
                             follow_redirects=False)
        self.assertIn("target not added: Folder", self.flash(r))
        r = self.client.post("/api/v1/conversion/target", json={"name": "Api", "folder": "kobo"})
        self.assertEqual((r.status_code, "another target" in r.text), (400, True))
        r = self.client.post("/api/v1/conversion/target", json={"name": "Api", "format": "pdf"})
        self.assertEqual((r.status_code, r.json()["folder"]), (201, "pdf"))
        r = self.client.put(f"/api/v1/conversion/target/{r.json()['id']}", json={"enabled": False})
        self.assertEqual((r.status_code, r.json()["enabled"]), (200, False))
        self.assertEqual(self.client.put("/api/v1/conversion/target/99", json={}).status_code, 404)
        self.assertEqual(self.client.delete("/api/v1/conversion/target/2").json(), {"ok": True, "filesRemoved": 0})
        self.assertEqual(len(self.client.get("/api/v1/conversion/profile").json()), len(convert.profiles.PROFILES))

    def test_the_series_page_the_download_and_the_activity_page(self):
        sid = self.series()
        tid = self.target()
        self.assertNotIn("copy-link", self.client.get(f"/series/{sid}").text)
        self.work(fake_worker(0, OK))
        html = self.client.get(f"/series/{sid}").text
        for needle in ('id="ereader"', "copy-link", f'href="/series/{sid}/chapter/1.0/converted/{tid}"',
                       "Auto is right to left (Japan)", 'name="reading_direction"'):
            self.assertIn(needle, html)
        r = self.client.get(f"/series/{sid}/chapter/1.0/converted/{tid}")
        self.assertEqual((r.status_code, r.headers["content-type"], r.content), (200, "application/epub+zip", b"book"))
        self.assertIn("Test%20Series%20-%20Chapter%20001.0.epub", r.headers["content-disposition"])
        self.assertEqual(self.client.get(f"/series/{sid}/chapter/9.0/converted/{tid}").status_code, 404)
        self.assertEqual(self.client.get(f"/series/{sid}/chapter/1.0/converted/99").status_code, 404)
        # a path from outside the target's folder is never served
        secret = os.path.join(self.root, "t.db")
        with db.connect() as con:
            con.execute("UPDATE conversion SET output_path=? WHERE number=2", (secret,))
        self.assertEqual(self.client.get(f"/series/{sid}/chapter/2.0/converted/{tid}").status_code, 404)
        html = self.client.get("/activity").text
        for needle in ('id="conversions"', "E-reader conversions: 0 waiting, 2 done, 0 failed",
                       'action="/activity/conversions/pause"'):
            self.assertIn(needle, html)
        self.assertEqual(self.client.get("/api/v1/conversion").json()["done"], 2)

    def test_how_a_series_is_read_is_saved_and_copies_are_made_again(self):
        sid = self.series()
        self.target()
        self.work(fake_worker(0, OK))
        r = self.client.post(f"/series/{sid}/convert/settings", data={
            "reading_direction": "ltr", "layout": "webtoon", "convert_enabled": ["0", "1"]}, follow_redirects=False)
        self.assertIn("e-reader settings saved", self.flash(r))
        with db.connect() as con:
            s = db.get_series(con, sid)
            self.assertEqual((s["reading_direction"], s["layout"], s["convert_enabled"]), ("ltr", "webtoon", 1))
            self.assertEqual(conversions.reconcile(con)["changed"], 2)
        r = self.client.post(f"/series/{sid}/convert/settings", data={"reading_direction": "sideways"},
                             follow_redirects=False)
        self.assertIn("not saved", self.flash(r))
        self.assertEqual(self.client.put(f"/api/v1/series/{sid}/convert", json={"layout": "nope"}).status_code, 400)
        self.assertEqual(self.client.put("/api/v1/series/99/convert", json={}).status_code, 404)
        self.work(fake_worker(0, OK))
        self.assertEqual(self.client.post(f"/api/v1/series/{sid}/convert?mode=missing").json(), {"queued": 0})
        self.assertEqual(self.client.post(f"/api/v1/series/{sid}/convert?mode=all").json(), {"queued": 2})

    def test_pause_and_resume(self):
        self.target()
        self.client.post("/activity/conversions/pause")
        settings._cache.clear()
        self.assertTrue(settings.get("convert_paused"))
        self.assertIn("paused", self.client.get("/api/v1/conversion").json()["paused"].__repr__().lower() + "paused")
        self.assertEqual(self.client.post("/api/v1/conversion/resume").json(), {"ok": True})
        settings._cache.clear()
        self.assertFalse(settings.get("convert_paused"))
        self.assertEqual(self.client.post("/api/v1/conversion/nope").status_code, 404)
        # never from the settings form or the settings API
        r = self.client.put("/api/v1/settings", json={"convert_paused": True})
        self.assertEqual(r.status_code, 400)

    def test_health_says_when_it_cannot_run(self):
        self.assertEqual([c.level for c in health._conversion()], ["warning"])       # on, no target
        self.target()
        with mock.patch.object(convert, "available", lambda: (False, "Pillow is not installed: pip ...")):
            c = health._conversion()
        self.assertEqual((c[0].level, "nothing is converted" in c[0].detail), ("error", True))
        with mock.patch("mangarr.config.CONVERTED_ROOT", os.path.join(self.library, "x")):
            self.assertEqual(health._conversion()[0].level, "error")
        with db.connect() as con:
            settings.set_many(con, {"convert_enabled": False})
        settings._cache.clear()
        self.assertEqual(health._conversion(), [])


if __name__ == "__main__":
    unittest.main()
