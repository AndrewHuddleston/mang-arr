"""Strict order and the stuck-chapter bookkeeping under random passes.

A seeded simulator runs core.refresh_series (resolve, save_plan, the
stuck-behind update, import, download, the update after it) pass after pass
over one series on 2-4 made-up sites. The resolve works from the sites'
chapter lists with the real ranking (resolver._assign) and the real junk
check (resolver._prune_junk, with the page-count cache between passes); the
download is the real downloader.download loop, with a stand-in for what
Suwayomi does with one run of chapters (a chapter it downloads is listed as
downloaded from then on), and the import links what arrived unless the
library already has another file under its name (until you move it away)
or checking the file runs out of time (core.not_linked either way).
Between passes the clock moves on by
hours to days (now and then several days) and the sites change: they rename
a fractional chapter (a side story's name, a title of its own, a plain one),
list or drop chapters (now and then one goes from every site), add new ones,
fix a broken copy, and change what their copy of a fractional chapter is: the
chapter at full length, a short placeholder or notice (fixed later, or put up
where the chapter was), or one whose pages the site will not list. In each
pass every site answers, does not answer (plan.unreachable) or its search
misses the series (absent from the plan); now and then Suwayomi itself does
not answer (no resolve, or no page count), stops in the middle of a
download, or the database is restored from an earlier backup. Between
passes the user skips, un-skips, keeps waiting, ignores or wants a chapter
again; sometimes presses Keep waiting while the pass is judging that very
chapter, or un-skips (or wants) an earlier chapter while the download runs.
After every pass, and at every download, the invariants are checked against
a model that keeps what the passes saw:

  I1 listing grace: a chapter some site listed stays listed (not
     'unavailable'; its reason says it is still waited for) until no site
     has listed it for GRACE_RESOLVES resolves in a row and GRACE_DAYS; a
     resolve in which a site that listed it (in the last KEEP_DAYS) did
     not answer or missed the series does not count. After that it is
     'unavailable'.
  I2 strict order: no chapter is downloaded (or even tried) while an
     earlier one that is not on disk, not ignored or skipped, not past its
     grace and not junk exists; also not while you take an earlier one back
     during the download. Junk is a property of a site's copy, judged by the
     page counts the resolve went by: a chapter is junk only when no site
     that lists it has it at full length (min_pages or more), one has a copy
     short of that and every other one a copy it would not count (a copy
     Suwayomi did not count keeps it), and every site that listed it within
     KEEP_DAYS but did not answer had a copy that was not the chapter when it
     last listed it. A chapter Suwayomi has downloaded is on disk, linked
     or not. A copy of a fractional chapter is tried only when the counts
     the resolve went by say it is the chapter (min_pages or more), or its
     site would not count it and never has (I2-short; a count that fails
     is judged by the copy's last count that worked): never one counted short, one whose
     count expired, one never counted (a fallback behind a full best copy)
     or one Suwayomi did not count this time. (A copy its site changed in
     place since a count still kept is not seen before that count is due
     again: the pass goes by the count, as pagecounts.py says.)
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
  I6 no stall: when the first chapter strict order waits for is due and
     one of its copies may be tried (not one that failed and waits for its
     retry, nor one no site lists now), the pass tries a chapter, or you
     took one back during its download; never twice in a row without.
     Chapters on disk, ignored, junk or unavailable are passed, and so is
     one Suwayomi has downloaded that the import could not link.

None of them exempts junk: a junk chapter is still listed, keeps its
evidence and your decisions, and holds strict order unless it is junk as
I2 says. Nothing here reaches the network, Suwayomi (a stand-in answers the
few calls a pass makes), MangaDex (manual series) or a notifier."""
import contextlib
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

from mangarr import config, core, db, downloader, mangadex, resolver, settings, stuck, verdict  # noqa: E402
from mangarr.model import Series  # noqa: E402
from mangarr.resolver import Plan, SourceMatch, _assign  # noqa: E402
from mangarr.suwayomi import Chapter, Source, SuwayomiUnreachable  # noqa: E402

SCENARIOS = 300
PASSES = (6, 12)
GRACE_RESOLVES, GRACE_DAYS = db.LISTING_GRACE_RESOLVES, db.LISTING_GRACE_DAYS    # I1 (3 resolves and 2 days)
KEEP_DAYS = db.LISTING_KEEP_DAYS        # a site's listing counts this long after it was last seen
GONE_DAYS = stuck.GONE_DAYS             # a decision on a chapter past its grace is dropped this long after
HIGH_DAYS = 1                           # I3: HIGH in two resolves at least this far apart
MIN_PAGES = config.MIN_PAGES            # a copy of a fractional chapter with fewer pages is not the chapter
DAY = 86400
T0 = 1_790_000_000                      # 2026-09-21, whole seconds
ODD = 1033                              # every step is whole hours plus this: no two passes are exactly N hours apart
SITES = ("Site A (EN)", "Site B (EN)", "Site C (EN)", "Site D (EN)")
SIDE = ("Side Story 1", "Side Story: Picnic", "Omake", "Afterword")
TITLED = ("Chapter {}: The Duel", "The Reunion", "Chapter {}: Night Market")
PLAIN = ("Chapter {}", None)
PENDING = ("wanted", "failed", "unavailable")
# what a page count of a fractional chapter's copy tells the resolve (Model.observe)
FULL, SHORT, UNKNOWN, UNASKED = "full", "short", "unknown", "unasked"
NOT_IT = {SHORT: "was counted short", UNASKED: "was not counted: Suwayomi did not answer",
          None: "was not counted in this resolve, and no kept count of it holds"}
SHM = "/dev/shm"                        # a memory file system where there is one: no disk syncs


def cid(mid: int, n: float) -> int:
    return mid * 10000 + int(round(n * 10))


class World:
    """What the sites list (and under what name), which copies are broken,
    how many pages each copy of a fractional chapter has, and what Suwayomi
    has downloaded."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.sites = list(SITES[:rng.randint(2, 4)])
        self.mid = {s: i + 1 for i, s in enumerate(self.sites)}
        self.top = rng.randint(4, 7)
        self.fracs = sorted(rng.sample([k + 0.5 for k in range(1, self.top)], rng.randint(1, 2)))
        self.lists: dict[str, dict[float, str | None]] = {s: {} for s in self.sites}
        self.broken: set[tuple[str, float]] = set()
        self.pages: dict[tuple[str, float], int | None] = {}   # a fractional copy's pages; None: not listed
        for s in self.sites:
            for k in range(1, self.top + 1):
                if rng.random() < 0.85:
                    self.add(s, float(k))
            for f in self.fracs:
                if rng.random() < 0.6:
                    self.add(s, f)
        self.delivered: set[float] = set()                     # in staging: Suwayomi downloaded them
        self.downloaded: set[tuple[str, float]] = set()         # ... from these sites (listed as downloaded)
        # the library already has another file under these chapters' names (a restore, a series added again)
        self.foreign: set[float] = set()
        if rng.random() < 0.35:
            self.foreign.add(rng.choice([float(k) for k in range(1, self.top + 1)] + self.fracs))

    def name(self, n: float) -> str | None:
        if n == int(n):
            return f"Chapter {n:g}"
        name = self.rng.choice(self.rng.choice((SIDE, SIDE, TITLED, PLAIN)))
        return name.format(f"{n:g}") if name else None

    def copy_pages(self) -> int | None:
        """A copy of a fractional chapter: the chapter, a placeholder or notice, or pages the site will not list."""
        rng = self.rng
        return rng.choices((rng.randint(MIN_PAGES + 2, 30), rng.randint(1, MIN_PAGES - 2), None), (0.5, 0.35, 0.15))[0]

    def add(self, s: str, n: float) -> None:
        self.lists[s][n] = self.name(n)
        if n != int(n):
            self.pages[(s, n)] = self.copy_pages()
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
                if f in self.lists[s] and rng.random() < 0.1:   # a placeholder fixed, or put up, in place
                    self.pages[(s, f)] = self.copy_pages()
            if rng.random() < 0.03:
                wholes = [n for n in self.lists[s] if n == int(n)]
                if wholes:
                    del self.lists[s][rng.choice(wholes)]
            mine = sorted(b for b in self.broken if b[0] == s)
            if mine and rng.random() < 0.06:
                self.broken.discard(rng.choice(mine))
        for n in sorted(self.foreign):
            if rng.random() < 0.1:                      # you moved the other file away
                self.foreign.discard(n)
        listed = sorted({n for s in self.sites for n in self.lists[s]})
        if listed and rng.random() < 0.04:
            self.foreign.add(rng.choice(listed))
        if listed and rng.random() < 0.06:              # renumbered, or taken down: gone from every site
            n = rng.choice(listed)
            for s in self.sites:
                self.lists[s].pop(n, None)
        if rng.random() < 0.15:
            self.top += 1
            for s in self.sites:
                if rng.random() < 0.8:
                    self.add(s, float(self.top))


def copy_state(pages) -> str:
    """What a page count the resolve went by says of a copy (UNASKED: Suwayomi did not answer)."""
    if pages == UNASKED:
        return UNASKED
    if pages is None:
        return UNKNOWN
    return FULL if pages >= MIN_PAGES else SHORT


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
        # n -> {skip | declined | wait | ignore: the last event before it}: an automatic skip after it is over it
        self.decisions: dict[float, dict[str, int]] = {}
        self.failed: set[float] = set()
        self.had_row: set[float] = set()
        self.failed_on: dict[float, set[str]] = {}
        self.wholes: dict[float, set[str]] = {}
        self.not_it: dict[tuple[str, float], bool] = {}                     # (site, n): its copy was not the chapter
        self.junk: dict[float, bool] = {}                                   # n: junk as I2 says, when last listed
        self.short: set[tuple[str, float]] = set()                          # copies counted short in this resolve

    def observe(self, t: int, world: World, answered: list[str], judged: dict) -> None:
        """A resolve: what the sites that answered list, and the page counts it went by (judged: (site, n) ->
        pages, None when the count failed, UNASKED when Suwayomi did not answer; not there: not counted)."""
        listing: dict[float, dict[str, str | None]] = {}
        for s in answered:
            self.full[s] = dict(world.lists[s])
            for n, name in world.lists[s].items():
                listing.setdefault(n, {})[s] = name
        self.listed_now = set(listing)
        self.short = {k for k, v in judged.items() if copy_state(v) == SHORT}
        for n in set(self.memory) | set(listing):
            mem = {s: v for s, v in self.memory.get(n, {}).items() if t - v[0] <= KEEP_DAYS * DAY}
            if n in listing:
                self.judge_junk(n, listing[n], [s for s in mem if s not in answered], judged)
                mem.update({s: (t, name) for s, name in listing[n].items()})
                self.missed[n], self.last_listed[n] = 0, t
            elif all(s in answered for s in mem):
                self.missed[n] = self.missed.get(n, 0) + 1
            self.memory[n] = mem
            self.complete[n] = all(s in answered for s in mem)

    def judge_junk(self, n: float, sites: dict, silent: list[str], judged: dict) -> None:
        if n == int(n):
            self.junk[n] = False
            return
        states = {s: copy_state(judged[(s, n)]) if (s, n) in judged else None for s in sites}
        every = all(v in (SHORT, UNKNOWN) for v in states.values()) and SHORT in states.values()
        for s, v in states.items():
            self.not_it[(s, n)] = v == SHORT or (v == UNKNOWN and every)
        self.junk[n] = every and all(self.not_it.get((s, n), False) for s in silent)

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

    def decide(self, n: float, what: str, mark: int) -> None:
        """You decided `what` about chapter n after event `mark`."""
        d = self.decisions.setdefault(n, {})
        if what == "skip":
            d.pop("wait", None)
        elif what == "unskip":
            for k in ("skip", "wait", "ignore"):
                d.pop(k, None)
            what = "declined"
        d.setdefault(what, mark)


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
        self.arrived: list[float] = []
        self.outcome: dict[str, str] = {}
        self.outage = False
        self.resolved = False
        self.auto = self.rng.random() < 0.8
        self.last_event = 0
        self.errors: list[str] = []
        self.history: list[str] = []
        self.judged: dict[tuple[str, float], object] = {}
        self.prng = random.Random()             # what happens inside a pass: apart from how the world goes on
        self.take_back = False
        self.took_back = False                  # you took a chapter back during this pass's download
        self.tried = 0                          # chapters tried in this pass
        self.expect: float | None = None        # I6: the chapter this pass was to try (or one after it)
        self.idle = 0                           # passes in a row that tried nothing though one was due
        self.last_judged: dict[tuple[str, float], str] = {}   # (site, n) -> what its last count judged by said
        self.stats: Counter = Counter()

    # -- the stand-ins ------------------------------------------------------------------------------

    def set_in_library(self, manga_id, in_library, retries=3, timeout=60):
        return None

    def chapter_url(self, chapter_id):
        return f"https://site{chapter_id // 10000}.example/chapter/{chapter_id}"

    def copy_of(self, chapter_id: int) -> tuple[str, float]:
        return self.world.sites[chapter_id // 10000 - 1], (chapter_id % 10000) / 10

    def page_count(self, chapter_id):
        """What Suwayomi says a copy's page list has (the resolve counts fractional chapters)."""
        key = self.copy_of(chapter_id)
        self.stats["page counts"] += 1
        if self.last_judged.get(key) == SHORT:
            self.stats["short counts counted again before the copy is used"] += 1
        if self.prng.random() < 0.03:
            self.judged[key] = UNASKED
            self.stats["page counts Suwayomi did not answer"] += 1
            raise SuwayomiUnreachable("Suwayomi at http://sim unreachable: connection refused")
        pages = self.world.pages.get(key)
        self.judged[key] = pages
        return pages

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
            chapters = [Chapter(cid(w.mid[s], n), n, name, None, (s, n) in w.downloaded)
                        for n, name in sorted(w.lists[s].items())]
            matches.append(SourceMatch(src, w.mid[s], "Freedom", None, 0, "Freedom", 1, chapters))
        candidates = _assign(matches)
        plan = Plan(series, matches, [], unreachable, {n: c[0] for n, c in candidates.items()}, candidates=candidates)
        self.judged = {}
        counts = kw.get("counts")
        if counts is not None:                  # the counts the resolve goes by without asking Suwayomi
            lookup = counts.lookup

            def kept(manga_id, ch, min_pages):
                got = lookup(manga_id, ch, min_pages)
                if got[0]:
                    self.judged[self.copy_of(ch.id)] = got[1]
                return got
            counts.lookup = kept
            record = counts.record

            def recorded(manga_id, ch, pages):
                # a count that failed is judged by the copy's last count that worked, if it has one
                # (pagecounts.py): what the resolve went by is what record() answers, not the failure
                got = record(manga_id, ch, pages)
                self.judged[self.copy_of(ch.id)] = got
                return got
            counts.record = recorded
        resolver._prune_junk(client, plan, None, counts, kw.get("settled"))
        self.model.observe(self.t, w, answered, self.judged)
        self.last_judged.update((k, copy_state(v)) for k, v in self.judged.items())
        self.plan = plan
        self.stats["copies left out as not counted"] += sum(len(v) for v in getattr(plan, "uncounted", {}).values())
        self.stats["junk chapters"] += len(plan.junk)
        self.stats["junk as I2 says"] += sum(1 for n in plan.junk if self.model.junk.get(n))
        self.stats["short copies left out of a chapter"] += sum(
            len(v) for n, v in getattr(plan, "short", {}).items() if n not in plan.junk)
        self.stats["fractional chapters with a short copy"] += len({n for _, n in self.model.short})
        return plan

    def import_series(self, con, series_id, client=None, downloaded=None):
        """What arrived is linked, unless the library already has another file under its name, or checking the
        file runs out of time this once (core.not_linked: the next import tries again)."""
        rows = {r["number"]: r for r in db.chapters(con, series_id)}
        linked = 0
        for n in sorted(self.world.delivered):
            r = rows.get(n)
            if r is not None and r["status"] in ("have", "ignored", "junk"):
                continue
            if n in self.world.foreign:
                core.not_linked(con, series_id, n, f"Site: downloaded, not linked: the library already has another "
                                f"file at Chapter {n:g}.cbz, which is never overwritten")
                self.stats["imports that found another file in the library"] += 1
                continue
            if self.prng.random() < 0.1:
                core.not_linked(con, series_id, n, "Site: downloaded, not linked yet: checking it took longer than "
                                "30s (slow or busy storage); checked again at the next import")
                self.stats["imports whose check ran out of time"] += 1
                continue
            db.set_have(con, series_id, n, None, None)
            linked += 1
        con.commit()
        return linked

    def due(self, con, series_id, plan):
        """core._due, and what I6 expects of the download: the first chapter strict order waits for, when it is
        due and one of its copies may be tried."""
        wanted = self.real_due(con, series_id, plan)
        rows = {r["number"]: r for r in db.chapters(con, series_id)}
        have, now_ = plan.have(), db.now()
        self.expect = None
        for k in sorted(rows):
            r = rows[k]
            if r["status"] in ("have", "ignored", "junk", "unavailable") or k in have:
                if k in have and r["status"] in ("wanted", "failed") and any(n > k for n in wanted):
                    self.stats["downloads due past a chapter downloaded but not linked"] += 1
                continue
            if r["status"] == "failed" and r["next_try"] and r["next_try"] > now_:
                break                                   # waits for its retry
            if k not in plan.assignment:
                break                                   # no site lists it now: waited for
            if k != int(k) and not any(copy_state(self.judged[(s, k)]) in (FULL, UNKNOWN)
                                       for s in self.world.sites if (s, k) in self.judged):
                break                                   # no copy known to be it: counted first
            self.expect = k
            break
        return wanted

    def download_source(self, client, manga_id, todo, batch, label, source_name, patient, cancel, report, memo,
                        stop_on_fail=False, throttled=False, warm=False, gone=set):
        """One run of chapters from one source entry, as Suwayomi would do it: each chapter arrives unless its
        copy there is broken. In order, you may take an earlier chapter back first (un-skip or want it)."""
        ok, failed, why = [], [], {}
        for c in todo:
            if stop_on_fail and self.take_back:
                self.take_back_one(c.number)
            if c.number in gone():
                continue
            self.tried += 1
            if stop_on_fail:
                self.check_order(c.number)
            if c.number != int(c.number):
                self.check_copy(source_name, c.number)
            if self.cut_after is not None and len(self.arrived) >= self.cut_after:
                raise SuwayomiUnreachable("Suwayomi at http://sim unreachable: connection refused")
            if (source_name, c.number) in self.world.broken:
                failed.append(c.number)
                why[c.number] = "the source has no working pages for this chapter"
                if stop_on_fail:
                    break
            else:
                ok.append(c.number)
                self.arrived.append(c.number)
                self.world.delivered.add(c.number)
                self.world.downloaded.add((source_name, c.number))
        return ok, failed, why

    def check_copy(self, site: str, n: float) -> None:
        """I2-short, as a copy of fractional chapter n is tried on `site`."""
        key = (site, n)
        state = copy_state(self.judged[key]) if key in self.judged else None
        if state not in (FULL, UNKNOWN):
            self.fail("I2-short", f"ch {n:g} tried on {site}, whose copy {NOT_IT[state]} (it has "
                                  f"{self.world.pages.get(key)} pages)")
            return
        if self.plan.candidates.get(n) and self.plan.candidates[n][0].source.name != site:
            self.stats[f"fallback copies tried, counted {state}"] += 1
        if (self.world.pages.get(key) or MIN_PAGES) < MIN_PAGES:
            self.stats["short copies tried on a count kept from before the site changed them in place"
                       if state == FULL else "short copies tried that their site would not count"] += 1

    def take_back_one(self, n: float) -> None:
        """You un-skip (or want again) a chapter before n while the download runs."""
        with db.connect() as other:
            ignored = [r["number"] for r in db.chapters(other, self.sid) if r["status"] == "ignored" and r["number"] < n]
            if not ignored:
                return                          # perhaps before a later one
            self.take_back = False
            k = self.prng.choice(ignored)
            if k in {s["number"] for s in stuck.skipped(other, self.sid)}:
                done = stuck.unskip(other, self.sid, k)
            else:
                done = db.want_again(other, self.sid, k)
                stuck.forget(other, self.sid, k)
                other.commit()
        if done:
            self.took_back = True
            self.decide(k, "unskip")
            self.stats["chapters taken back while the download ran"] += 1
            self.history.append(f"  (you took {k:g} back while {n:g} was about to download)")

    def record_downloads(self, con, series_id, plan, wanted, results, *a, **k):
        self.model.failed.update(n for n, r in results.items() if r == "failed")
        return self.real_record(con, series_id, plan, wanted, results, *a, **k)

    def judge(self, series, st, chapters, *a, **k):
        if self.in_pass:
            rows = [c if isinstance(c, verdict.Chapter) else verdict.Chapter.from_row(c) for c in chapters]
            if self.model.high_now(series, st.number, rows, self.t):
                self.note_high(st.number)
            if self.race and not self.raced:
                self.raced = True
                with db.connect() as other:
                    if stuck.dismiss(other, self.sid, st.number):
                        self.decide(st.number, "wait")
                        self.history.append(f"  (Keep waiting on {st.number:g} while the pass judged it)")
        return self.real_judge(series, st, chapters, *a, **k)

    def note_high(self, n: float) -> None:
        h = self.model.high.setdefault(n, [])
        if not h or h[-1] != self.t:
            h.append(self.t)

    # -- the scenario ---------------------------------------------------------------------------------

    def decide(self, n: float, what: str) -> None:
        with db.connect() as con:
            mark = con.execute("SELECT COALESCE(MAX(id), 0) FROM event").fetchone()[0]
        self.model.decide(n, what, mark)

    def fail(self, inv: str, text: str) -> None:
        self.found.append((inv, f"seed {self.seed} pass {self.pass_no}: {text}"))

    def set(self, **values) -> None:
        with db.connect() as con:
            settings.set_many(con, values)
        settings._cache.clear()

    def run(self) -> list[tuple[str, str]]:
        self.real_judge, self.real_record, self.real_due = stuck.judge, core.record_downloads, core._due
        patches = [mock.patch.object(config, "DB_PATH", self.path),
                   mock.patch.object(core, "resolve", self.resolve),
                   mock.patch.object(core, "import_series", self.import_series),
                   mock.patch.object(core, "_due", self.due),
                   mock.patch.object(core, "record_downloads", self.record_downloads),
                   mock.patch.object(downloader, "_download_source", self.download_source),
                   mock.patch.object(downloader, "download_lock", lambda *a, **k: contextlib.nullcontext()),
                   mock.patch.object(downloader, "clear_leftovers", lambda *a, **k: True),
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
        self.prng.seed(self.seed * 1000 + self.pass_no)
        self.t += (rng.randint(1, 40) * 3600 if rng.random() < 0.9 else rng.randint(2, 5) * DAY) + ODD
        w.change()
        self.outcome = {s: rng.choices(("ok", "down", "miss"), (0.7, 0.15, 0.15))[0] for s in w.sites}
        self.outage = rng.random() < 0.04
        self.cut_after = rng.randint(0, 3) if rng.random() < 0.05 else None
        self.race, self.raced, self.resolved = rng.random() < 0.15, False, False
        self.take_back, self.arrived = rng.random() < 0.15, []
        self.took_back, self.tried, self.expect = False, 0, None
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
        if self.resolved:
            self.history.append(f"  counts {sorted((k[0][5], k[1], v) for k, v in self.judged.items())}; "
                                f"junk {sorted(self.plan.junk)}")
            self.stats["voided junk"] += len(getattr(self.plan, "voided", {}))
        self.history.append(f"  tried {self.tried}; due first {self.expect}; not linked "
                            f"{sorted(n for n in w.delivered if n in w.foreign)}")
        self.check_progress()
        self.check()
        self.act()
        if rng.random() < 0.06:
            self.backup_or_restore()

    def describe(self) -> str:
        w = self.world
        return "; ".join(f"{s[5]}: " + ", ".join(f"{n:g}" + (f"={nm!r}/{w.pages.get((s, n))}p" if n != int(n) else "")
                                                  for n, nm in sorted(w.lists[s].items()))
                         for s in w.sites)

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
                    self.decide(st.number, what)
                    self.history.append(f"  you: {what} {st.number:g}")
            elif what == "unskip":
                skipped = [k["number"] for k in stuck.skipped(con, self.sid)]
                if skipped:
                    n = rng.choice(skipped)
                    if stuck.unskip(con, self.sid, n):
                        self.decide(n, "unskip")
                        self.history.append(f"  you: un-skip {n:g}")
            elif what == "ignore":
                todo = [n for n, r in rows.items() if r["status"] in ("wanted", "failed")]
                if todo:
                    n = rng.choice(todo)
                    db.set_status(con, self.sid, n, "ignored")        # the chapter's Ignore button
                    con.commit()
                    self.decide(n, "ignore")
                    self.history.append(f"  you: ignore {n:g}")
            elif what == "want":
                todo = [n for n, r in rows.items() if r["status"] == "ignored"]
                if todo:
                    n = rng.choice(todo)
                    db.want_again(con, self.sid, n)                   # the chapter's Want button
                    stuck.forget(con, self.sid, n)
                    con.commit()
                    self.decide(n, "unskip")
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
        self.stats["restores"] += 1
        self.history.append(f"  restore of the backup after pass {at}")

    # -- the invariants -----------------------------------------------------------------------------

    def check_order(self, n: float) -> None:
        """I2, at the moment chapter n is tried (the chapters' states as they are then)."""
        m = self.model
        with db.connect() as con:
            rows = db.chapters_by_number(con, self.sid, [k for k in m.last_listed if k < n])
        for k in sorted(m.last_listed):
            if k >= n:
                break
            r = rows.get(k)
            status = r["status"] if r is not None else None
            if k in self.world.delivered or status in ("have", "ignored") or m.past_grace(k, self.t) or \
                    m.junk.get(k):
                continue
            self.fail("I2", f"ch {n:g} tried while ch {k:g} is {status} and not past its grace "
                            f"(missed {m.missed.get(k, 0)}, last listed {(self.t - m.last_listed[k]) / 3600:.1f} h ago)"
                            + (" nor junk as I2 says" if status == "junk" else ""))
            return

    def check_progress(self) -> None:
        """I6, after a pass."""
        if self.expect is None or self.tried or self.took_back:
            self.idle = 0
            return
        self.idle += 1
        self.stats["passes that tried nothing though a chapter was due"] += 1
        if self.idle >= 2:
            with db.connect() as con:
                r = db.chapters_by_number(con, self.sid, [self.expect])[self.expect]
                later = [(x["number"], x["reason"]) for x in db.chapters(con, self.sid)
                         if x["number"] > self.expect and x["status"] == "wanted"][:1]
            self.fail("I6", f"{self.idle} passes in a row tried nothing, though ch {self.expect:g} was due "
                            f"({r['status']}: {r['reason']!r}; after it {later})")

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
            if r["status"] == "junk":
                self.stats["junk rows after a pass"] += 1
                self.stats["junk rows with evidence or a decision"] += n in evidence or n in choices
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
            self.stats["automatic skips"] += 1
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
            over = sorted(k for k, mark in m.decisions.get(n, {}).items() if mark < e["id"])
            if over:
                self.fail("I3e", f"ch {n:g} skipped automatically over your decision {over}")
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
                    self.fail("I4", f"ch {n:g} ({r['status']}): {s} listed it {(t - seen) / 3600:.1f} h ago as "
                                    f"{name!r}, but its evidence has only {names}")
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
                self.fail("I5", f"ch {n:g}: your skip is gone ({dict(k) if k else None}; it is {r['status']})")
            if "declined" in decs and (k is None or not k["declined"]):
                self.fail("I5", f"ch {n:g}: your un-skip is forgotten ({dict(k) if k else None}; it is {r['status']})")
            if "wait" in decs and (k is None or not k["dismissed"]):
                self.fail("I5", f"ch {n:g}: your Keep waiting is gone ({dict(k) if k else None}; it is "
                                f"{r['status']})")
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
        self.shm = os.path.isdir(SHM) and os.access(SHM, os.W_OK)
        tmp = tempfile.TemporaryDirectory(dir=SHM if self.shm else None)
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

    def run_scenarios(self, seeds) -> tuple[Counter, dict, Counter, int]:
        """(scenarios per invariant broken, the first break of each with its history, what was exercised,
        passes)."""
        counts, first, stats, runs = Counter(), {}, Counter(), 0
        for seed in seeds:
            sim = Sim(seed, self.tmp, self.template)
            self.errors.sim = sim
            found = sim.run()
            runs += sim.pass_no
            stats.update(sim.stats)
            for inv, text in found:
                first.setdefault(inv, (text, "\n".join(sim.history)))
            counts.update({inv for inv, _ in found})
            for f in (sim.path, sim.path + ".bak", sim.path + "-wal", sim.path + "-shm"):
                if os.path.exists(f):
                    os.remove(f)
        return counts, first, stats, runs

    def test_random_passes_keep_the_invariants(self):
        start = time.perf_counter()
        counts, first, stats, runs = self.run_scenarios(range(SCENARIOS))
        took = time.perf_counter() - start
        report = "\n\n".join(f"{inv} ({counts[inv]} scenario(s)): {text}\n{hist}"
                             for inv, (text, hist) in sorted(first.items()))
        self.assertEqual(dict(counts), {}, f"{SCENARIOS} scenarios, {runs} passes, {took:.1f} s\n{report}")
        # the junk paths are all taken: chapters judged junk by every copy, a short copy left out of a chapter
        # another site has in full, junk withheld while a site that may have it did not answer, recounts; and so
        # are the fallback copies (counted before one is tried, a short count counted again once it expired, a copy
        # not counted this time left out) and the downloads due past a chapter Suwayomi has but the import could
        # not link (another file in the library, a check out of time)
        for what, least in (("junk chapters", 100), ("junk as I2 says", 100), ("short copies left out of a chapter", 50),
                            ("voided junk", 5), ("junk rows with evidence or a decision", 20),
                            ("page counts Suwayomi did not answer", 10), ("chapters taken back while the download ran", 10),
                            ("automatic skips", 10), ("restores", 10), ("fallback copies tried, counted full", 20),
                            ("short counts counted again before the copy is used", 50),
                            ("copies left out as not counted", 20), ("imports that found another file in the library", 100),
                            ("imports whose check ran out of time", 50),
                            ("downloads due past a chapter downloaded but not linked", 20)):
            self.assertGreaterEqual(stats[what], least, f"{what}: {dict(stats)}")
        if self.shm:                            # on a disk, the syncs alone can take longer (about 50 s without it)
            self.assertLess(took, 60)


if __name__ == "__main__":
    unittest.main()
