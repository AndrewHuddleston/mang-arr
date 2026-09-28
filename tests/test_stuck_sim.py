"""Strict order and the stuck-chapter bookkeeping under random passes.

A seeded simulator runs core.refresh_series (resolve, save_plan, the
stuck-behind update, import, download, the update after it) pass after pass
over one series on 2-4 made-up sites. Between passes the clock moves on by
hours to days and the sites change: they rename a fractional chapter (a
side story's name, a title of its own, a plain one), list or drop chapters
(now and then one goes from every site), add new ones, fix a broken copy. In each pass every site answers, does not
answer (plan.unreachable) or its search misses the series (absent from the
plan); now and then Suwayomi itself does not answer (no resolve), stops in
the middle of a download, or the database is restored from an earlier
backup. Between passes the user skips, un-skips, keeps waiting, ignores or
wants a chapter again, and sometimes presses Keep waiting while the pass is
judging that very chapter. After every pass, and at every download, the
invariants are checked against a model that keeps what the passes saw:

  I1 listing grace: a chapter some site listed stays listed (not
     'unavailable'; its reason says it is still waited for) until no site
     has listed it for GRACE_RESOLVES resolves in a row and GRACE_DAYS; a
     resolve in which a site that listed it (in the last KEEP_DAYS) did
     not answer or missed the series does not count. After that it is
     'unavailable'.
  I2 strict order: no chapter is downloaded (or even tried) while an
     earlier one that is not on disk, not ignored or skipped and not past
     its grace exists.
  I3 automatic skip: only with strict order and the automatic skip on, a
     chapter that blocked (failed on every site), whose verdict was HIGH in
     two resolves at least a day apart, in the latest of which every site
     that listed it within KEEP_DAYS answered and matched the series, and
     with no decision of yours on it (also one made while the pass judged
     it).
  I4 evidence: once a chapter has a stuck row, it keeps every site that
     listed it within KEEP_DAYS with its name, and never loses a site it
     failed on or a site's whole chapters, until the chapter is on disk or
     past its grace.
  I5 decisions: your skip, un-skip, Keep waiting and ignore stay as you
     left them (dropped only once the chapter is on disk, or no site has
     listed it for GRACE_DAYS + GONE_DAYS).

Nothing here reaches the network, Suwayomi (a stand-in answers the few
calls a pass makes), MangaDex (manual series) or a notifier."""
import copy
import json
import logging
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from collections import Counter
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mangarr import config, core, db, downloader, mangadex, settings, stuck, verdict  # noqa: E402
from mangarr.model import Series  # noqa: E402
from mangarr.resolver import Plan, SourceMatch, _assign  # noqa: E402
from mangarr.suwayomi import Chapter, Source, SuwayomiUnreachable  # noqa: E402

SCENARIOS = 300
PASSES = (6, 12)
GRACE_RESOLVES, GRACE_DAYS = db.LISTING_GRACE_RESOLVES, db.LISTING_GRACE_DAYS    # I1 (3 resolves and 2 days)
KEEP_DAYS = db.LISTING_KEEP_DAYS        # a site's listing counts this long after it was last seen
GONE_DAYS = stuck.GONE_DAYS             # a decision on a chapter past its grace is dropped this long after
HIGH_DAYS = 1                           # I3: HIGH in two resolves at least this far apart
DAY = 86400
T0 = 1_790_000_000                      # 2026-09-21, whole seconds
ODD = 1033                              # every step is whole hours plus this: no two passes are exactly N hours apart
SITES = ("Site A (EN)", "Site B (EN)", "Site C (EN)", "Site D (EN)")
SIDE = ("Side Story 1", "Side Story: Picnic", "Omake", "Afterword")
TITLED = ("Chapter {}: The Duel", "The Reunion", "Chapter {}: Night Market")
PLAIN = ("Chapter {}", None)
PENDING = ("wanted", "failed", "unavailable")


def cid(mid: int, n: float) -> int:
    return mid * 10000 + int(round(n * 10))


class World:
    """What the sites list (and under what name), which copies are broken,
    and what Suwayomi has downloaded."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.sites = list(SITES[:rng.randint(2, 4)])
        self.mid = {s: i + 1 for i, s in enumerate(self.sites)}
        self.top = rng.randint(4, 7)
        self.fracs = sorted(rng.sample([k + 0.5 for k in range(1, self.top)], rng.randint(1, 2)))
        self.lists: dict[str, dict[float, str | None]] = {s: {} for s in self.sites}
        self.broken: set[tuple[str, float]] = set()
        for s in self.sites:
            for k in range(1, self.top + 1):
                if rng.random() < 0.85:
                    self.add(s, float(k))
            for f in self.fracs:
                if rng.random() < 0.6:
                    self.add(s, f)
        self.delivered: set[float] = set()

    def name(self, n: float) -> str | None:
        if n == int(n):
            return f"Chapter {n:g}"
        name = self.rng.choice(self.rng.choice((SIDE, SIDE, TITLED, PLAIN)))
        return name.format(f"{n:g}") if name else None

    def add(self, s: str, n: float) -> None:
        self.lists[s][n] = self.name(n)
        if self.rng.random() < (0.6 if n != int(n) else 0.08):
            self.broken.add((s, n))

    def change(self) -> None:
        rng = self.rng
        for s in self.sites:
            for f in self.fracs:
                if rng.random() < 0.08:
                    if f in self.lists[s] and rng.random() < 0.3:
                        del self.lists[s][f]
                    elif f in self.lists[s]:
                        self.lists[s][f] = self.name(f)
                    else:
                        self.add(s, f)
            if rng.random() < 0.03:
                wholes = [n for n in self.lists[s] if n == int(n)]
                if wholes:
                    del self.lists[s][rng.choice(wholes)]
            mine = sorted(b for b in self.broken if b[0] == s)
            if mine and rng.random() < 0.06:
                self.broken.discard(rng.choice(mine))
        listed = sorted({n for s in self.sites for n in self.lists[s]})
        if listed and rng.random() < 0.06:              # renumbered, or taken down: gone from every site
            n = rng.choice(listed)
            for s in self.sites:
                self.lists[s].pop(n, None)
        if rng.random() < 0.15:
            self.top += 1
            for s in self.sites:
                if rng.random() < 0.8:
                    self.add(s, float(self.top))


class Model:
    """What the passes saw, as the invariants need it."""

    def __init__(self):
        self.memory: dict[float, dict[str, tuple[int, str | None]]] = {}   # n -> {site: (last seen, name)}
        self.last_listed: dict[float, int] = {}
        self.missed: dict[float, int] = {}
        self.listed_now: set[float] = set()
        self.complete: dict[float, bool] = {}
        self.full: dict[str, dict[float, str | None]] = {}                  # site -> its last listing seen
        self.high: dict[float, list[int]] = {}                             # n -> resolves with an auto_skip verdict
        self.decisions: dict[float, set[str]] = {}                          # n -> skip | declined | wait | ignore
        self.failed: set[float] = set()
        self.had_row: set[float] = set()
        self.failed_on: dict[float, set[str]] = {}
        self.wholes: dict[float, set[str]] = {}

    def observe(self, t: int, world: World, answered: list[str]) -> None:
        listing: dict[float, dict[str, str | None]] = {}
        for s in answered:
            self.full[s] = dict(world.lists[s])
            for n, name in world.lists[s].items():
                listing.setdefault(n, {})[s] = name
        self.listed_now = set(listing)
        for n in set(self.memory) | set(listing):
            mem = {s: v for s, v in self.memory.get(n, {}).items() if t - v[0] <= KEEP_DAYS * DAY}
            if n in listing:
                mem.update({s: (t, name) for s, name in listing[n].items()})
                self.missed[n], self.last_listed[n] = 0, t
            elif all(s in answered for s in mem):
                self.missed[n] = self.missed.get(n, 0) + 1
            self.memory[n] = mem
            self.complete[n] = all(s in answered for s in mem)

    def past_grace(self, n: float, t: int) -> bool:
        return n not in self.listed_now and self.missed.get(n, 0) >= GRACE_RESOLVES and \
            t - self.last_listed[n] > GRACE_DAYS * DAY

    def names(self, n: float, t: int) -> dict:
        return {s: v[1] for s, v in self.memory.get(n, {}).items() if t - v[0] <= KEEP_DAYS * DAY}

    def high_now(self, series: Series, n: float, rows, t: int) -> bool:
        names = self.names(n, t)
        if not names or n == int(n):
            return False
        wholes = {s: {f"{k:g}": w for k, w in verdict.site_words(list(self.full.get(s, {}).items()), series,
                                                                  n).items()} for s in names}
        min_pages = int(settings.get("min_pages") or config.MIN_PAGES)
        return verdict.classify(series, verdict.Blocker(n, names, None, wholes), rows, None, min_pages).auto_skip

    def decide(self, n: float, what: str) -> None:
        d = self.decisions.setdefault(n, set())
        if what == "skip":
            d.discard("wait")
            d.add("skip")
        elif what == "unskip":
            d -= {"skip", "wait", "ignore"}
            d.add("declined")
        else:
            d.add(what)


class Violations(AssertionError):
    pass


class Sim:
    """One scenario: a world, a model, a database, and the passes."""

    def __init__(self, seed: int, tmp: str, template: str):
        self.seed = seed
        self.rng = random.Random(seed)
        self.world = World(self.rng)
        self.model = Model()
        self.t = T0
        self.series = Series(english="Freedom")
        self.path = os.path.join(tmp, f"s{seed}.db")
        shutil.copyfile(template, self.path)
        self.snapshot: tuple | None = None
        self.found: list[tuple[str, str]] = []
        self.pass_no = 0
        self.in_pass = False
        self.race = False
        self.raced = False
        self.cut_after: int | None = None
        self.outcome: dict[str, str] = {}
        self.outage = False
        self.resolved = False
        self.auto = self.rng.random() < 0.8
        self.last_event = 0
        self.errors: list[str] = []
        self.history: list[str] = []

    # -- the stand-ins ------------------------------------------------------------------------------

    def set_in_library(self, manga_id, in_library, retries=3, timeout=60):
        return None

    def chapter_url(self, chapter_id):
        return f"https://site{chapter_id // 10000}.example/chapter/{chapter_id}"

    def resolve(self, client, series, **kw):
        if self.outage:
            raise SuwayomiUnreachable("Suwayomi at http://sim unreachable: connection refused")
        self.resolved = True
        w, matches, unreachable, answered = self.world, [], [], []
        for s in w.sites:
            o, src = self.outcome[s], Source(s, s, "en")
            if o == "down":
                unreachable.append((src, "timed out"))
                continue
            if o == "miss":
                continue
            answered.append(s)
            chapters = [Chapter(cid(w.mid[s], n), n, name, None, False) for n, name in sorted(w.lists[s].items())]
            matches.append(SourceMatch(src, w.mid[s], "Freedom", None, 0, "Freedom", 1, chapters))
        self.model.observe(self.t, w, answered)
        candidates = _assign(matches)
        return Plan(series, matches, [], unreachable, {n: c[0] for n, c in candidates.items()},
                    candidates=candidates)

    def import_series(self, con, series_id, client=None):
        rows = {r["number"]: r for r in db.chapters(con, series_id)}
        linked = 0
        for n in sorted(self.world.delivered):
            r = rows.get(n)
            if r is None or r["status"] not in ("have", "ignored"):
                db.set_have(con, series_id, n, None, None)
                linked += 1
        con.commit()
        return linked

    def download(self, client, plan, only=None, should_cancel=None, reasons=None, progress=None, throttled=None,
                 in_order=None, dropped=None, attempts=None):
        reasons = reasons if reasons is not None else {}
        wanted = set(plan.wanted()) if only is None else set(only)
        in_order = bool(settings.get("download_in_order")) if in_order is None else in_order
        steps = downloader.SeriesSteps(plan, wanted, in_order, plan.series.title, reasons)
        with db.connect() as con:
            rows = {r["number"]: r for r in db.chapters(con, self.sid)}
        arrived: list[float] = []
        try:
            while True:
                skip = dropped() if dropped else set()
                keys = steps.wants(skip)
                if not keys:
                    break
                run = steps.take(keys[0], skip)
                if run is None:
                    continue
                ok, failed, why = [], [], {}
                for c in run.todo:
                    if c.number in skip:
                        continue
                    if in_order:
                        self.check_order(c.number, rows, arrived)
                    if self.cut_after is not None and len(arrived) >= self.cut_after:
                        raise SuwayomiUnreachable("Suwayomi at http://sim unreachable: connection refused")
                    if (run.match.source.name, c.number) in self.world.broken:
                        failed.append(c.number)
                        why[c.number] = "the source has no working pages for this chapter"
                        if run.in_order:
                            break
                    else:
                        ok.append(c.number)
                        arrived.append(c.number)
                        self.world.delivered.add(c.number)
                steps.record(run, ok, failed, why)
        finally:
            if attempts is not None:
                attempts.extend(steps.attempts)
        self.model.failed.update(n for n, r in steps.results.items() if r == "failed")
        return steps.results

    def judge(self, series, st, chapters, *a, **k):
        if self.in_pass:
            rows = [c if isinstance(c, verdict.Chapter) else verdict.Chapter.from_row(c) for c in chapters]
            if self.model.high_now(series, st.number, rows, self.t):
                self.note_high(st.number)
            if self.race and not self.raced:
                self.raced = True
                with db.connect() as other:
                    if stuck.dismiss(other, self.sid, st.number):
                        self.model.decide(st.number, "wait")
                        self.history.append(f"  (Keep waiting on {st.number:g} while the pass judged it)")
        return self.real_judge(series, st, chapters, *a, **k)

    def note_high(self, n: float) -> None:
        h = self.model.high.setdefault(n, [])
        if not h or h[-1] != self.t:
            h.append(self.t)

    # -- the scenario ---------------------------------------------------------------------------------

    def fail(self, inv: str, text: str) -> None:
        self.found.append((inv, f"seed {self.seed} pass {self.pass_no}: {text}"))

    def set(self, **values) -> None:
        with db.connect() as con:
            settings.set_many(con, values)
        settings._cache.clear()

    def run(self) -> list[tuple[str, str]]:
        self.real_judge = stuck.judge
        patches = [mock.patch.object(config, "DB_PATH", self.path),
                   mock.patch.object(core, "resolve", self.resolve),
                   mock.patch.object(core, "import_series", self.import_series),
                   mock.patch.object(downloader, "download", self.download),
                   mock.patch.object(stuck, "judge", self.judge),
                   mock.patch.object(time, "time", lambda: float(self.t)),
                   mock.patch.object(db, "now", lambda: time.strftime("%Y-%m-%d %H:%M:%S",
                                                                        time.localtime(self.t)))]
        for p in patches:
            p.start()
        keeper = sqlite3.connect(self.path)     # open throughout: no checkpoint (and sync) at every close
        keeper.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        try:
            settings._cache.clear()
            self.set(download_in_order=True, auto_skip_side_stories=self.auto)
            with db.connect() as con:
                self.sid = db.upsert_series(con, self.series)
            for _ in range(self.rng.randint(*PASSES)):
                self.one_pass()
        finally:
            keeper.close()
            for p in reversed(patches):
                p.stop()
            settings._cache.clear()
        return self.found

    def one_pass(self) -> None:
        rng, w = self.rng, self.world
        self.pass_no += 1
        self.t += rng.randint(1, 40) * 3600 + ODD
        w.change()
        self.outcome = {s: rng.choices(("ok", "down", "miss"), (0.7, 0.15, 0.15))[0] for s in w.sites}
        self.outage = rng.random() < 0.04
        self.cut_after = rng.randint(0, 3) if rng.random() < 0.05 else None
        self.race, self.raced, self.resolved = rng.random() < 0.15, False, False
        if rng.random() < 0.05:
            self.auto = not self.auto
            self.set(auto_skip_side_stories=self.auto)
        self.history.append(f"pass {self.pass_no} at +{(self.t - T0) / 3600:.1f} h: "
                            + ("Suwayomi down" if self.outage else ", ".join(f"{s[5]}={o}" for s, o in
                                                                             self.outcome.items()))
                            + f"; lists {self.describe()}; broken {sorted(w.broken)}")
        self.in_pass = True
        try:
            with db.connect() as con:
                core.refresh_series(con, self, self.sid, download=True)
        except SuwayomiUnreachable:
            pass
        finally:
            self.in_pass = False
        self.check()
        self.act()
        if rng.random() < 0.06:
            self.backup_or_restore()

    def describe(self) -> str:
        return "; ".join(f"{s[5]}: " + ", ".join(f"{n:g}" + (f"={nm!r}" if n != int(n) else "")
                                                  for n, nm in sorted(self.world.lists[s].items()))
                         for s in self.world.sites)

    def act(self) -> None:
        """What the user does between passes."""
        rng = self.rng
        if rng.random() >= 0.4:
            return
        with db.connect() as con:
            row = db.get_series(con, self.sid)
            blocked = stuck.details(con, row, fetch=False)
            rows = {r["number"]: r for r in db.chapters(con, self.sid)}
            what = rng.choice(("skip", "wait", "unskip", "ignore", "want"))
            if what in ("skip", "wait") and blocked:
                st = rng.choice(blocked)
                done = stuck.skip(con, self.sid, st.number, "manual", st.verdict, st.waiting) if what == "skip" \
                    else stuck.dismiss(con, self.sid, st.number)
                if done:
                    self.model.decide(st.number, what)
                    self.history.append(f"  you: {what} {st.number:g}")
            elif what == "unskip":
                skipped = [k["number"] for k in stuck.skipped(con, self.sid)]
                if skipped:
                    n = rng.choice(skipped)
                    if stuck.unskip(con, self.sid, n):
                        self.model.decide(n, "unskip")
                        self.history.append(f"  you: un-skip {n:g}")
            elif what == "ignore":
                todo = [n for n, r in rows.items() if r["status"] in ("wanted", "failed")]
                if todo:
                    n = rng.choice(todo)
                    db.set_status(con, self.sid, n, "ignored")        # the chapter's Ignore button
                    con.commit()
                    self.model.decide(n, "ignore")
                    self.history.append(f"  you: ignore {n:g}")
            elif what == "want":
                todo = [n for n, r in rows.items() if r["status"] == "ignored"]
                if todo:
                    n = rng.choice(todo)
                    db.want_again(con, self.sid, n)                   # the chapter's Want button
                    stuck.forget(con, self.sid, n)
                    con.commit()
                    self.model.decide(n, "unskip")
                    self.history.append(f"  you: want {n:g}")

    def backup_or_restore(self) -> None:
        snap = self.path + ".bak"
        if self.snapshot is None:
            with db.connect() as con, sqlite3.connect(snap) as out:
                con.backup(out)
            out.close()
            self.snapshot = (copy.deepcopy(self.model), self.pass_no)
            self.history.append("  backup")
            return
        model, at = self.snapshot
        with sqlite3.connect(snap) as src, sqlite3.connect(self.path) as dst:
            src.backup(dst)
        src.close()
        dst.close()
        self.model = copy.deepcopy(model)
        self.snapshot = None
        with db.connect() as con:
            self.last_event = con.execute("SELECT COALESCE(MAX(id), 0) FROM event").fetchone()[0]
        self.set(download_in_order=True, auto_skip_side_stories=self.auto)     # the settings as they are now
        self.history.append(f"  restore of the backup after pass {at}")

    # -- the invariants -----------------------------------------------------------------------------

    def check_order(self, n: float, rows: dict, arrived: list) -> None:
        """I2, at the moment chapter n is tried."""
        m = self.model
        for k in sorted(m.last_listed):
            if k >= n:
                break
            r = rows.get(k)
            status = r["status"] if r is not None else None
            if k in arrived or status in ("have", "ignored", "junk") or m.past_grace(k, self.t):
                continue
            self.fail("I2", f"ch {n:g} tried while ch {k:g} is {status} and not past its grace "
                            f"(missed {m.missed.get(k, 0)}, last listed {(self.t - m.last_listed[k]) / 3600:.1f} h ago)")
            return

    def check(self) -> None:
        m, t = self.model, self.t
        with db.connect() as con:
            rows = {r["number"]: r for r in db.chapters(con, self.sid)}
            evidence = {r["number"]: r for r in con.execute("SELECT * FROM stuck WHERE series_id=?", (self.sid,))}
            choices = {r["number"]: r for r in con.execute("SELECT * FROM stuck_choice WHERE series_id=?",
                                                           (self.sid,))}
            events = con.execute("SELECT id, kind, message FROM event WHERE series_id=? AND id>? ORDER BY id",
                                 (self.sid, self.last_event)).fetchall()
            chapters = stuck._chapters(con, [self.sid])[self.sid]
        if events:
            self.last_event = events[-1]["id"]
        for n, r in rows.items():
            if r["status"] == "failed":
                m.failed.add(n)
        for n in m.last_listed:
            if n != int(n) and (r := rows.get(n)) is not None and r["status"] != "have" and \
                    m.high_now(self.series, n, chapters, t):
                self.note_high(n)
        # decisions end once the chapter is on disk, or long after the last site listed it
        for n in list(m.decisions):
            r = rows.get(n)
            if (r is not None and r["status"] == "have") or \
                    (n in m.last_listed and t - m.last_listed[n] > (GRACE_DAYS + GONE_DAYS) * DAY - 3600):
                del m.decisions[n]
        # I3: every automatic skip of this pass
        for e in events:
            words = e["message"].split(" ", 3)
            if e["kind"] != "skip" or words[0] != "chapter" or words[2:3] != ["skipped"] or \
                    not words[3].startswith("automatically"):
                continue
            n = float(words[1])
            high = m.high.get(n, [])
            if not self.auto:
                self.fail("I3a", f"ch {n:g} skipped automatically with the automatic skip off")
            if n not in m.failed:
                self.fail("I3b", f"ch {n:g} skipped automatically but it never failed (did not block)")
            if t not in high or not any(t - h >= HIGH_DAYS * DAY for h in high):
                self.fail("I3c", f"ch {n:g} skipped automatically without a HIGH verdict in two resolves a day apart"
                                f" (HIGH at {[(h - T0) / 3600 for h in high]} h, now {(t - T0) / 3600:.1f} h; "
                                f"names seen {m.names(n, t)})")
            if not m.complete.get(n, False):
                self.fail("I3d", f"ch {n:g} skipped automatically while a site that listed it did not answer or "
                                f"missed the series (listed by {sorted(m.memory.get(n, {}))}, "
                                f"this pass {self.outcome})")
            if m.decisions.get(n):
                self.fail("I3e", f"ch {n:g} skipped automatically over your decision {sorted(m.decisions[n])}")
        # I1
        for n in sorted(m.last_listed):
            r = rows.get(n)
            if r is None:
                self.fail("I1", f"ch {n:g} was listed but has no row")
                continue
            if r["status"] not in PENDING:
                continue
            if n in m.listed_now:
                if r["status"] == "unavailable":
                    self.fail("I1", f"ch {n:g} is listed but unavailable")
            elif not m.past_grace(n, t):
                if r["status"] == "unavailable":
                    self.fail("I1", f"ch {n:g} unavailable within its grace (missed {m.missed.get(n, 0)} counted "
                                    f"resolve(s), last listed {(t - m.last_listed[n]) / 3600:.1f} h ago by "
                                    f"{sorted(m.memory.get(n, {}))})")
                elif "still waiting for it" not in (r["reason"] or ""):
                    self.fail("I1", f"ch {n:g} not listed in the last check, but its reason does not say it is "
                                    f"still waited for: {r['reason']!r}")
            elif r["status"] != "unavailable" and self.resolved:     # a resolve marks it (a pass without one cannot)
                self.fail("I1", f"ch {n:g} past its grace but still {r['status']}")
        # I4
        for n in evidence:
            m.had_row.add(n)
        for n in sorted(m.had_row):
            r = rows.get(n)
            if r is None or r["status"] == "have" or n not in m.last_listed or m.past_grace(n, t):
                m.had_row.discard(n)
                m.failed_on.pop(n, None)
                m.wholes.pop(n, None)
                continue
            e = evidence.get(n)
            if e is None:
                self.fail("I4", f"ch {n:g} ({r['status']}) lost its stuck evidence within its grace")
                m.had_row.discard(n)
                continue
            names = json.loads(e["names"])
            for s, (seen, name) in m.memory.get(n, {}).items():
                if t - seen > KEEP_DAYS * DAY:
                    continue
                if s not in names:
                    self.fail("I4", f"ch {n:g}: {s} listed it {(t - seen) / 3600:.1f} h ago as {name!r}, but its "
                                    f"evidence has only {names}")
                elif names[s] != name:
                    self.fail("I4", f"ch {n:g}: {s} calls it {name!r}, its evidence says {names[s]!r}")
            failed_on, wholes = set(json.loads(e["failed_on"])), set(json.loads(e["wholes"]))
            if not m.failed_on.get(n, set()) <= failed_on:
                self.fail("I4", f"ch {n:g} forgot a site it failed on: {sorted(m.failed_on[n])} -> {sorted(failed_on)}")
            lost = {s for s in m.wholes.get(n, set()) - wholes if s in m.names(n, t)}
            if lost:
                self.fail("I4", f"ch {n:g} forgot the whole chapters of {sorted(lost)}")
            m.failed_on[n], m.wholes[n] = failed_on, wholes
        # I5
        for n, decs in m.decisions.items():
            r, k = rows.get(n), choices.get(n)
            if r is None:
                continue
            if ("skip" in decs or "ignore" in decs) and r["status"] != "ignored":
                self.fail("I5", f"ch {n:g}: you {'skipped' if 'skip' in decs else 'ignored'} it, now it is "
                                f"{r['status']}")
            if "skip" in decs and (k is None or k["skipped"] != "manual"):
                self.fail("I5", f"ch {n:g}: your skip is gone ({dict(k) if k else None})")
            if "declined" in decs and (k is None or not k["declined"]):
                self.fail("I5", f"ch {n:g}: your un-skip is forgotten ({dict(k) if k else None})")
            if "wait" in decs and (k is None or not k["dismissed"]):
                self.fail("I5", f"ch {n:g}: your Keep waiting is gone ({dict(k) if k else None})")
        for text in self.errors:
            self.fail("error", text)
        self.errors.clear()


class _Errors(logging.Handler):
    """The warnings of the stuck-behind bookkeeping (it never raises: a failure is only logged)."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.sim: Sim | None = None

    def emit(self, record):
        if self.sim is not None and "could not" in record.getMessage():
            self.sim.errors.append(record.getMessage()[:300])


class PassSimulatorTest(unittest.TestCase):
    def setUp(self):
        shm = "/dev/shm"                            # a memory file system where there is one: no disk syncs
        tmp = tempfile.TemporaryDirectory(dir=shm if os.path.isdir(shm) and os.access(shm, os.W_OK) else None)
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        for p in (mock.patch.object(config, "DB_PATH", os.path.join(self.tmp, "none.db")),   # each Sim has its own
                  mock.patch.object(config, "LIBRARY_ROOT", os.path.join(self.tmp, "library")),
                  mock.patch.object(config, "STAGING_ROOT", os.path.join(self.tmp, "staging")),
                  mock.patch.object(config, "LOCK_PATH", os.path.join(self.tmp, "lock")),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("a test tried to reach the network")),
                  mock.patch("socket.create_connection", side_effect=OSError("network disabled in tests")),
                  mock.patch("mangarr.notify.send", return_value=None),
                  mock.patch("mangarr.notify.send_detailed", return_value={}),
                  mock.patch.object(core.komga, "scan", lambda *a, **k: False),
                  mock.patch.object(mangadex, "_request", side_effect=AssertionError("a test asked MangaDex")),
                  mock.patch.object(stuck, "fetcher", stuck.Fetcher())):
            p.start()
            self.addCleanup(p.stop)
        os.makedirs(config.LIBRARY_ROOT)
        os.makedirs(config.STAGING_ROOT)
        self.template = os.path.join(self.tmp, "template.db")
        with db.connect(self.template):
            pass
        quiet = logging.getLogger("mangarr")
        noisy = logging.getLogger("mangarr.stuck")
        self.errors = _Errors()
        levels = (quiet.level, noisy.level, noisy.propagate)
        quiet.setLevel(logging.CRITICAL)
        noisy.setLevel(logging.WARNING)
        noisy.propagate = False
        noisy.addHandler(self.errors)

        def restore():
            noisy.removeHandler(self.errors)
            quiet.setLevel(levels[0])
            noisy.setLevel(levels[1])
            noisy.propagate = levels[2]
        self.addCleanup(restore)
        settings._cache.clear()
        self.addCleanup(settings._cache.clear)

    def test_random_passes_keep_the_invariants(self):
        start = time.perf_counter()
        counts, first, runs = Counter(), {}, 0
        for seed in range(SCENARIOS):
            sim = Sim(seed, self.tmp, self.template)
            self.errors.sim = sim
            found = sim.run()
            runs += sim.pass_no
            for inv, text in found:
                first.setdefault(inv, (text, "\n".join(sim.history)))
            counts.update({inv for inv, _ in found})
            for f in (sim.path, sim.path + ".bak", sim.path + "-wal", sim.path + "-shm"):
                if os.path.exists(f):
                    os.remove(f)
        took = time.perf_counter() - start
        report = "\n\n".join(f"{inv} ({counts[inv]} scenario(s)): {text}\n{hist}"
                             for inv, (text, hist) in sorted(first.items()))
        self.assertEqual(dict(counts), {}, f"{SCENARIOS} scenarios, {runs} passes, {took:.1f} s\n{report}")
        self.assertLess(took, 60)


if __name__ == "__main__":
    unittest.main()
