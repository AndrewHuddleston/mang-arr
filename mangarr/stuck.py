"""Series stuck behind a chapter, and what to do about one.

With chapters downloaded strictly in order (Settings: Strict download in
order), a chapter that failed on every source listing it holds back the
later chapters of its series: they say "waiting for chapter N"
(downloader.waiting_reason), in the pass that failed it and in every pass
until its next try (core.hold_in_order). blockers() finds these in what the
passes left in the database, earlier ones too. For each blocker this module
keeps what the verdict (verdict.py) needs, and does what the user or the
automatic skip decides:

  * evidence: what every source listing the chapter calls it and where it
    is on their sites (the stuck table, from each resolve's plan: update();
    a site missing from one resolve, which may just not have answered,
    keeps its name for KEEP_DAYS), the sites a download run failed it on
    (note_failures), and the page counts of the chapters it is compared
    with (fill_pages)
  * skip(): the chapter becomes 'ignored', so the later chapters go on at
    the next pass; unskip() makes it wanted again. A skip keeps the
    chapter's state (state_of: its name on each site), and one whose state
    changed is offered back on the series page: a site may have put a real
    chapter where a notice was. A chapter you un-skip (or want again) is
    never skipped automatically after that (declined)
  * dismiss() ("Keep waiting"): the note is folded away and the automatic
    skip leaves the chapter alone until its state changes
  * automatic skip (Settings, off by default): only a blocker whose verdict
    is a HIGH-confidence side_story or covered (Verdict.auto_skip), that
    failed on every site listing it now (a site that has just started
    listing it is tried first), and only once MangaDex's chapter list was
    looked up for it (or cannot be: a manual series); never one you
    declined or keep waiting for (why_not_auto). Logged, recorded as an
    event, shown on the series page and undone like any skip; one whose
    state changes is made wanted again by itself, as its verdict no longer
    holds.

A pass never waits for MangaDex and neither does a page: both judge with
what is cached (verdict.judge(fetch=False)). The Fetcher thread looks
MangaDex up for blockers without an answer, one at a time (mangadex.py
paces and bounds the requests), and counts the pages the verdict compares.
"""
import json
import logging
import math
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from . import config, db, downloader, library, limits, mangadex, settings, verdict
from .matching import oneline
from .model import Series
from .suwayomi import SuwayomiError, SuwayomiUnreachable
from .verdict import Verdict

log = logging.getLogger(__name__)

MAX_NAME = 200          # characters kept of a site's name for the chapter
MAX_SOURCE = 200        # a source name longer than this is not kept (Suwayomi display names are short)
MAX_SOURCES = 12        # sites whose name for a blocker is kept
MAX_URL = 2000
URL_SITES = 3           # sites whose page for a blocker is looked up (the Open buttons)
PAGES_NEAR = 15         # whole chapters nearest to N whose pages are counted for "the usual length"
KEEP_DAYS = db.UNREACHABLE_KEEP_DAYS    # a site no resolve saw list the chapter for this long is dropped
SKIPPABLE = ("wanted", "failed", "unavailable")


@dataclass
class Stuck:
    """A chapter a series is stuck behind."""
    series_id: int
    number: float
    waiting: int                        # later chapters that wait for it
    status: str                         # its chapter row's
    name: str | None
    source: str | None
    reason: str | None
    tries: int
    failed_since: str | None
    names: dict = field(default_factory=dict)   # {source: its name for it there}; {} until a resolve saw it
    urls: dict = field(default_factory=dict)    # {source: its page on that site, '' when there is none}
    failed_on: list = field(default_factory=list)   # the sites a download run failed it on
    dismissed: bool = False             # "Keep waiting", and its state has not changed since
    declined: bool = False              # you un-skipped it or wanted it again: never skipped automatically
    verdict: Verdict | None = None
    checking: bool = False              # MangaDex not looked up for it yet; the Fetcher is on it

    @property
    def untried(self) -> list[str]:
        """The sites listing it that no download run has failed it on yet
        (one that has just started to list it): the next pass tries them."""
        return [k for k in self.names if k not in self.failed_on]

    def as_dict(self) -> dict:
        """For the API."""
        v = self.verdict
        return {"number": self.number, "waiting": self.waiting, "status": self.status, "name": self.name,
                "reason": self.reason, "tries": self.tries, "failedSince": self.failed_since,
                "sources": dict(self.names), "urls": {k: u for k, u in self.urls.items() if u},
                "failedOn": list(self.failed_on), "dismissed": self.dismissed, "declined": self.declined,
                "checkingMangaDex": self.checking,
                "verdict": None if v is None else {"kind": v.kind, "headline": v.headline, "confidence": v.confidence,
                                                   "evidence": list(v.evidence), "skippable": v.skippable,
                                                   "autoSkip": v.auto_skip}}


def _loads(text, default):
    try:
        v = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def state_of(name: str | None, names: dict) -> dict:
    """What "the chapter's state" is for Keep waiting and for a skip: its
    name in its row and on each site that lists it."""
    return {"name": name, "names": dict(names)}


def same_state(then: dict, now: dict) -> bool:
    """Whether a state kept earlier still holds: the same name in its row,
    and every site listing it now lists it under the name it had then. A
    site that no longer lists it is no change (it may only not have
    answered), nor are names on the sites that were not known on one side
    (no resolve had seen the chapter yet); a new site, or a new name on
    one, is."""
    if then.get("name") != now.get("name"):
        return False
    was, now_names = then.get("names") or {}, now.get("names") or {}
    if not was or not now_names:
        return True
    return all(src in was and was[src] == name for src, name in now_names.items())


def safe_url(url) -> str | None:
    """A chapter's page on its site as the extension gives it, when it is a
    plain http(s) link that may be shown as one; else None."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL or any(c.isspace() or ord(c) < 32 or c == "\x7f"
                                                                        for c in url):
        return None
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    return url if u.scheme in ("http", "https") and u.hostname else None


def why_not_auto(st: Stuck, v: Verdict | None) -> str | None:
    """Why the automatic skip leaves this blocker alone, or None when it may
    skip it: 'verdict' (not a HIGH side_story or covered one), 'declined'
    (you un-skipped it or wanted it again), 'waiting' (Keep waiting),
    'untried' (a site listing it has not been tried yet, or no resolve has
    seen which sites list it)."""
    if v is None or not v.auto_skip:
        return "verdict"
    if st.declined:
        return "declined"
    if st.dismissed:
        return "waiting"
    if not st.names or st.untried:
        return "untried"
    return None


# -- finding them ------------------------------------------------------------

def blockers(con, series_id: int | None = None) -> list[Stuck]:
    """Every chapter a series (or this one) is stuck behind now: a failed
    chapter that wanted ones wait for (their reason says so), with strict
    download in order on. By series, then number."""
    if not settings.get("download_in_order"):
        return []
    where, args = (" AND series_id=?", (series_id,)) if series_id is not None else ("", ())
    counts: dict[tuple[int, str], int] = {}
    for r in con.execute("SELECT series_id, reason, COUNT(*) AS n FROM chapter WHERE status='wanted' AND reason LIKE"
                         " 'waiting for chapter %'" + where + " GROUP BY series_id, reason", args):
        token = downloader.waiting_for(r["reason"])
        if token:
            counts[(r["series_id"], token)] = counts.get((r["series_id"], token), 0) + r["n"]
    out = []
    for sid in sorted({sid for sid, _ in counts}):
        failed = {f"{r['number']:g}": r for r in con.execute(
            "SELECT * FROM chapter WHERE series_id=? AND status='failed'", (sid,))}
        stored = {r["number"]: r for r in con.execute("SELECT * FROM stuck WHERE series_id=?", (sid,))}
        for (s2, token), waiting in counts.items():
            row = failed.get(token) if s2 == sid else None
            if row is not None:
                out.append(_stuck(row, waiting, stored.get(row["number"])))
    return sorted(out, key=lambda st: (st.series_id, st.number))


def _stuck(row, waiting: int, stored) -> Stuck:
    st = Stuck(row["series_id"], row["number"], waiting, row["status"], row["name"], row["source_name"],
               row["reason"], row["tries"] or 0, row["failed_since"])
    if stored is not None:
        _stored(st, stored)
    return st


def _stored(st: Stuck, stored) -> None:
    """What the stuck row keeps of the blocker, onto it."""
    st.names = _loads(stored["names"], {})
    st.urls = _loads(stored["urls"], {})
    st.failed_on = [k for k in _loads(stored["failed_on"], []) if isinstance(k, str)]
    st.declined = bool(stored["declined"])
    st.dismissed = bool(stored["dismissed"]) and same_state(_loads(stored["dismissed"], {}),
                                                            state_of(st.name, st.names))


def _chapters(con, ids) -> dict[int, list[verdict.Chapter]]:
    """The chapter rows of these series as the verdict reads them (only the
    columns it needs), by series id; in statements of at most 500 ids."""
    ids = list(ids)
    out: dict[int, list[verdict.Chapter]] = {sid: [] for sid in ids}
    cur = con.cursor()
    cur.row_factory = None                  # plain tuples: a big library has hundreds of thousands of rows
    make = verdict.Chapter
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for sid, number, status, name, source, pages in cur.execute(
                "SELECT series_id, number, status, name, source_name, pages FROM chapter WHERE series_id IN"
                f" ({','.join('?' * len(part))})", part):
            out[sid].append(make(number, status, name, source, pages))
    return out


_ASK = object()


def judge(series: Series, st: Stuck, chapters, md=_ASK) -> Verdict:
    """The verdict on a blocker from the series' chapters (verdict.Chapter,
    or chapter rows), with what is cached of MangaDex only (never waits), or
    with this answer of MangaDex's (md; None: none)."""
    rows = [c if isinstance(c, verdict.Chapter) else verdict.Chapter.from_row(c) for c in chapters]
    blocker = verdict.Blocker(st.number, dict(st.names), st.reason)
    min_pages = int(settings.get("min_pages") or config.MIN_PAGES)
    if md is _ASK:
        return verdict.judge(series, blocker, rows, fetch=False, min_pages=min_pages)
    return verdict.classify(series, blocker, rows, md, min_pages)


def details(con, series_row, fetch: bool = True) -> list[Stuck]:
    """blockers() of one series with their verdicts, for its page and the
    API; with fetch, a blocker MangaDex was not looked up for yet is handed
    to the Fetcher (checking). Reads only."""
    found = blockers(con, series_row["id"])
    if found:
        series = db.series_to_model(series_row)
        rows = _chapters(con, [series_row["id"]])[series_row["id"]]
        for st in found:
            _judge_now(series, st, rows, fetch)
    return found


def overview(con, fetch: bool = True, judged: bool = True) -> dict[int, list[Stuck]]:
    """blockers() of every series by series id, with their verdicts unless
    judged=False (the Wanted page judges only for its Stuck filter: that
    reads every chapter row of every stuck series); fetch as for details().
    Reads only: the stuck series' chapters in one statement, as the verdict
    reads them."""
    out: dict[int, list[Stuck]] = {}
    for st in blockers(con):
        out.setdefault(st.series_id, []).append(st)
    if not out or not judged:
        return out
    series_rows = {sid: r for sid in out if (r := db.get_series(con, sid)) is not None}
    chapters = _chapters(con, list(series_rows))
    for sid, found in out.items():
        row = series_rows.get(sid)
        if row is None:
            continue
        series = db.series_to_model(row)
        for st in found:
            _judge_now(series, st, chapters[sid], fetch)
    return out


def _judge_now(series: Series, st: Stuck, rows, fetch: bool) -> None:
    st.verdict = judge(series, st, rows)
    if fetch and not mangadex.looked_up(series, st.number):
        st.checking = fetcher.request(st.series_id, series, st.number)


def skipped(con, series_id: int) -> list[dict]:
    """Chapters of the series skipped from a stuck note that are still
    skipped: {number, how ('manual' | 'auto'), verdict (headline then),
    changed (its state is not what it was when it was skipped), names (on
    the sites now)}."""
    out = []
    for r in con.execute("SELECT s.*, c.name AS chapter_name FROM stuck s JOIN chapter c ON c.series_id=s.series_id AND"
                         " c.number=s.number WHERE s.series_id=? AND s.skipped IS NOT NULL AND c.status='ignored'"
                         " ORDER BY s.number", (series_id,)):
        names = _loads(r["names"], {})
        then = _loads(r["skipped_state"], {})
        out.append({"number": r["number"], "how": r["skipped"], "verdict": r["verdict"], "names": names,
                    "changed": bool(then) and not same_state(then, state_of(r["chapter_name"], names))})
    return out


# -- what the user (or the automatic skip) does --------------------------------

def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _waits_for(n: float) -> tuple[str, int]:
    """(the start of the reason of a chapter that waits for chapter n, its length)."""
    start = downloader.WAITING_START.format(f"{n:g}")
    return start, len(start)


def skipped_reason(n: float) -> str:
    """The reason of a later chapter that waited for chapter n, once n is skipped."""
    return f"no longer waits for chapter {n:g}, which was skipped: the next pass downloads it"


def skip(con, series_id: int, number: float, how: str = "manual", v: Verdict | None = None,
         waiting: int | None = None) -> bool:
    """Skip a chapter: it becomes 'ignored' (a wanted, failed or unavailable
    one only), with the state it is skipped in kept for the series page,
    and the chapters that waited for it say they no longer do. how:
    'manual' or 'auto'. Returns whether it was skipped. Committed."""
    row = con.execute("SELECT * FROM chapter WHERE series_id=? AND number=?", (series_id, number)).fetchone()
    if row is None or row["status"] not in SKIPPABLE:
        return False
    stored = con.execute("SELECT names FROM stuck WHERE series_id=? AND number=?", (series_id, number)).fetchone()
    names = _loads(stored["names"], {}) if stored is not None else {}
    n = f"{number:g}"
    held = f"it held back {waiting} chapter{'s' if waiting != 1 else ''}" if waiting else ""
    if how == "auto" and v is not None:
        reason = f"skipped automatically: {_lower_first(v.headline)} ({v.confidence} confidence)"
        msg = f"chapter {n} skipped automatically: {_lower_first(v.headline)} ({v.confidence} confidence)"
    else:
        reason = "skipped: the chapters after it download without it (un-skip to want it again)"
        msg = f"chapter {n} skipped" + (f" (verdict: {_lower_first(v.headline)})" if v is not None else "")
    if not db.set_status(con, series_id, number, "ignored", reason, only_from=SKIPPABLE):
        return False
    con.execute("INSERT INTO stuck (series_id, number, names, skipped, skipped_state, verdict, updated_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(series_id, number) DO UPDATE SET skipped=excluded.skipped,"
                " skipped_state=excluded.skipped_state, verdict=excluded.verdict, dismissed=NULL,"
                " updated_at=excluded.updated_at",
                (series_id, number, json.dumps(names), how, json.dumps(state_of(row["name"], names)),
                 v.headline if v is not None else None, db.now()))
    start, size = _waits_for(number)
    con.execute("UPDATE chapter SET reason=?, updated_at=? WHERE series_id=? AND status='wanted' AND"
                " substr(reason, 1, ?)=?", (skipped_reason(number), db.now(), series_id, size, start))
    db.event(con, "skip", msg + (f"; {held}" if held else ""), series_id)
    con.commit()
    return True


def keep_skipped(con, series_id: int, number: float) -> bool:
    """A skipped chapter the sites list differently now stays skipped as it
    is: its state now is the one it is skipped in, so the series page stops
    offering it back. Returns whether it is one. Committed."""
    r = con.execute("SELECT s.names, c.name FROM stuck s JOIN chapter c ON c.series_id=s.series_id AND"
                    " c.number=s.number WHERE s.series_id=? AND s.number=? AND s.skipped IS NOT NULL AND"
                    " c.status='ignored'", (series_id, number)).fetchone()
    if r is None:
        return False
    con.execute("UPDATE stuck SET skipped_state=?, updated_at=? WHERE series_id=? AND number=?",
                (json.dumps(state_of(r["name"], _loads(r["names"], {}))), db.now(), series_id, number))
    db.event(con, "skip", f"chapter {number:g} kept skipped, as it is listed now", series_id)
    con.commit()
    return True


def unskip(con, series_id: int, number: float, why: str | None = None, by_user: bool = True) -> bool:
    """Want a skipped (ignored) chapter again. by_user: you asked for it, so
    the automatic skip leaves the chapter alone from now on (declined); not
    when the automatic skip itself takes one back. Returns whether it was
    ignored. Committed."""
    if not db.set_status(con, series_id, number, "wanted", None, only_from=("ignored",)):
        return False
    forget(con, series_id, number, declined=by_user)
    con.execute("UPDATE chapter SET reason=?, updated_at=? WHERE series_id=? AND status='wanted' AND reason=?",
                ("not downloaded yet - waiting for a download pass", db.now(), series_id, skipped_reason(number)))
    db.event(con, "skip", f"chapter {number:g} un-skipped: wanted again" + (f" ({why})" if why else ""), series_id)
    con.commit()
    return True


def forget(con, series_id: int, number: float, declined: bool = True) -> None:
    """The chapter is wanted again (un-skipped, or its Want button): what
    its skip and Keep waiting kept no longer applies. declined: you wanted
    it, so it is never skipped automatically again (kept until the chapter
    is on disk or gone)."""
    con.execute("UPDATE stuck SET skipped=NULL, skipped_state=NULL, verdict=NULL, dismissed=NULL,"
                " declined=CASE WHEN ? THEN ? ELSE declined END, updated_at=? WHERE series_id=? AND number=?",
                (int(declined), db.now(), db.now(), series_id, number))


def dismiss(con, series_id: int, number: float) -> bool:
    """Keep waiting: fold the note on this blocker away, and keep the
    automatic skip off it, until its state changes. Returns whether the
    series is stuck behind it. Committed."""
    st = next((b for b in blockers(con, series_id) if b.number == number), None)
    if st is None:
        return False
    con.execute("INSERT INTO stuck (series_id, number, names, dismissed, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(series_id, number) DO UPDATE SET dismissed=excluded.dismissed,"
                " updated_at=excluded.updated_at",
                (series_id, number, json.dumps(st.names), json.dumps(state_of(st.name, st.names)), db.now()))
    con.commit()
    return True


# -- after a download run, after a resolve --------------------------------------

def note_failures(con, series_id: int, results: dict, attempts) -> None:
    """After a download run (core.record_downloads, before its commit): the
    sites each chapter that failed for good was tried on and failed
    (attempts: (source name, chapter, 'ok' | 'failed')), added to what its
    stuck row keeps, with strict order on (only then does a failed chapter
    hold its series back). The automatic skip acts only on a chapter that
    failed on every site listing it now."""
    if not settings.get("download_in_order"):
        return
    by_n: dict[float, list[str]] = {}
    for src, n, r in attempts or ():
        if r == "failed" and results.get(n) == "failed" and isinstance(src, str) and 0 < len(src) <= MAX_SOURCE:
            by_n.setdefault(n, [])
            if src not in by_n[n]:
                by_n[n].append(src)
    for n, sites in by_n.items():
        r = con.execute("SELECT failed_on FROM stuck WHERE series_id=? AND number=?", (series_id, n)).fetchone()
        kept = [k for k in _loads(r["failed_on"], []) if isinstance(k, str)] if r is not None else []
        both = list(dict.fromkeys([*kept, *sites]))[:MAX_SOURCES * 2]
        con.execute("INSERT INTO stuck (series_id, number, failed_on, updated_at) VALUES (?,?,?,?)"
                    " ON CONFLICT(series_id, number) DO UPDATE SET failed_on=excluded.failed_on,"
                    " updated_at=excluded.updated_at", (series_id, n, json.dumps(both), db.now()))


def update(con, client, series_id: int, series: Series, plan, found: list[Stuck] | None = None) -> None:
    """Keep the evidence on this series' blockers, skip automatically what
    may be, and have MangaDex looked up for the rest. After a resolve,
    `found` is what blockers() said before save_plan made the failed ones
    wanted again (the pass's download then tries them); after a download
    run it is None: the blockers now. client (None: none) looks up the
    chapters' pages on their sites, outside any transaction. Never raises:
    this bookkeeping must not end a pass."""
    try:
        _update(con, client, series_id, series, plan, blockers(con, series_id) if found is None else found)
    except limits.Cancelled:
        log.debug("%s: stuck-behind bookkeeping cut short by a cancel", oneline(series.title, 80))
    except Exception as e:
        log.warning("%s: could not update the chapters the series is stuck behind: %s: %s",
                    oneline(series.title, 80), type(e).__name__, oneline(e, 200))


def _merge(kept: dict, seen: dict, fresh: dict, now: str) -> tuple[dict, dict]:
    """The names of the sites listing a chapter: this resolve's (fresh,
    best first), then the ones kept of the sites it did not see list it,
    for KEEP_DAYS after one last did (a site that did not answer, or whose
    search missed once, is still one that lists it). With the date each
    was last seen."""
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - KEEP_DAYS * 86400))
    names, when = dict(fresh), {k: now for k in fresh}
    for k, name in kept.items():
        if k in names or len(names) >= MAX_SOURCES:
            continue
        last = seen.get(k) if isinstance(seen.get(k), str) else now     # kept before dates were: from now
        if last >= cutoff:
            names[k], when[k] = name, last
    return names, when


def _update(con, client, series_id: int, series: Series, plan, found: list[Stuck]) -> None:
    stored = {r["number"]: r for r in con.execute("SELECT * FROM stuck WHERE series_id=?", (series_id,))}
    if not found and not stored:
        return                              # the common case: nothing to keep, nothing to judge
    rows = db.chapters_by_number(con, series_id, list(stored))
    candidates = getattr(plan, "candidates", None) or {}
    now = db.now()
    evidence: dict[float, tuple[dict, dict, dict]] = {}      # number -> (names, seen, chapter ids)
    for n in {st.number for st in found} | {n for n, r in stored.items() if r["skipped"]}:
        fresh, ids = _names(candidates.get(n) or [], n)
        if fresh:                           # a plan that no longer lists it keeps what was known
            r = stored.get(n)
            names, seen = _merge(_loads(r["names"], {}) if r is not None else {},
                                 _loads(r["seen"], {}) if r is not None else {}, fresh, now)
            evidence[n] = (names, seen, ids)
    if con.in_transaction:
        con.commit()                        # no write is held across the lookups on Suwayomi
    urls = {}
    for n, (names, _, ids) in evidence.items():
        known = _loads(stored[n]["urls"], {}) if n in stored else {}
        urls[n] = {k: u for k, u in known.items() if k in names}
        for src in list(names)[:URL_SITES]:
            if src not in urls[n] and ids.get(src) is not None and client is not None:
                u = _url(client, ids[src], series)
                if u is not None:           # None: Suwayomi did not answer; asked again next time
                    urls[n][src] = u
    for n, (names, seen, _) in evidence.items():
        con.execute("INSERT INTO stuck (series_id, number, names, seen, urls, updated_at) VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(series_id, number) DO UPDATE SET names=excluded.names, seen=excluded.seen,"
                    " urls=excluded.urls, updated_at=excluded.updated_at",
                    (series_id, n, json.dumps(names), json.dumps(seen), json.dumps(urls[n]), now))
    for n, r in stored.items():
        c = rows.get(n)
        if c is None or c["status"] in ("have", "junk", "unavailable") or (c["status"] == "ignored" and
                                                                           not r["skipped"]):
            con.execute("DELETE FROM stuck WHERE series_id=? AND number=?", (series_id, n))
    con.commit()
    for n, r in stored.items():
        c = rows.get(n)
        if r["skipped"] == "auto" and c is not None and c["status"] == "ignored" and n in evidence and \
                not same_state(_loads(r["skipped_state"], {}), state_of(c["name"], evidence[n][0])):
            unskip(con, series_id, n, "its name on the sites changed since it was skipped automatically", by_user=False)
            log.info("%s: ch %g wanted again: its name on the sites changed since it was skipped automatically",
                     oneline(series.title, 80), n)
    if not found:
        return
    for st in found:
        fill_pages(con, series_id, st.number)
    now_stored = {r["number"]: r for r in con.execute("SELECT * FROM stuck WHERE series_id=?", (series_id,))}
    chapters = _chapters(con, [series_id])[series_id]
    current = {c.number: c for c in chapters}
    auto = bool(settings.get("auto_skip_side_stories") and settings.get("download_in_order"))
    for st in found:
        c = current.get(st.number)
        if c is not None:
            st.status, st.name = c.status, c.name
        if st.number in now_stored:
            _stored(st, now_stored[st.number])
        # one answer of MangaDex's for the check and the verdict: a lookup that lands in between changes neither
        md = mangadex.english_chapters(series, st.number, fetch=False)
        if md is None and not mangadex.looked_up(series, st.number):
            fetcher.request(series_id, series, st.number)
            continue                        # automatic skipping waits for MangaDex's chapter list
        if not auto or st.status not in ("wanted", "failed"):
            continue
        if md is None and not series.manual:
            continue                        # MangaDex could not be asked: tried again later
        v = judge(series, st, chapters, md)
        why = why_not_auto(st, v)
        if why is not None:
            if why != "verdict":
                log.debug("%s: ch %g not skipped automatically (%s): %s", oneline(series.title, 80), st.number,
                          why, _lower_first(v.headline))
            continue
        if skip(con, series_id, st.number, "auto", v, st.waiting):
            log.info("%s: ch %g skipped automatically: %s (%s confidence); %d later chapter(s) waited for it",
                     oneline(series.title, 80), st.number, _lower_first(v.headline), v.confidence, st.waiting)


def _names(candidates, number: float) -> tuple[dict, dict]:
    """({source: its name for the chapter}, {source: its chapter id there})
    from the plan's candidates for it, best first."""
    names: dict[str, str | None] = {}
    ids: dict[str, int | None] = {}
    for m in candidates:
        src = getattr(getattr(m, "source", None), "name", None)
        if not isinstance(src, str) or not src or len(src) > MAX_SOURCE or src in names:
            continue
        ch = next((c for c in getattr(m, "chapters", None) or () if c.number == number), None)
        names[src] = oneline(ch.name, MAX_NAME) if ch is not None and isinstance(ch.name, str) and ch.name else None
        ids[src] = ch.id if ch is not None and isinstance(ch.id, int) else None
        if len(names) >= MAX_SOURCES:
            break
    return names, ids


def _url(client, chapter_id: int, series: Series) -> str | None:
    """The chapter's page on its site; '' when there is none to show (no
    realUrl, one that is not a plain http(s) link, or a Suwayomi that does
    not know the field): kept, and not asked again. None when Suwayomi did
    not answer (unreachable, a timeout, its circuit breaker open): asked
    again at the next update."""
    try:
        url = client.chapter_url(chapter_id)
    except limits.Cancelled:
        raise
    except SuwayomiUnreachable as e:
        log.debug("%s: Suwayomi did not answer for the page of chapter id %d: %s", oneline(series.title, 80),
                  chapter_id, oneline(e, 200))
        return None
    except SuwayomiError as e:              # a GraphQL error: this Suwayomi has no realUrl, or no such chapter
        log.debug("%s: no page on its site for chapter id %d: %s", oneline(series.title, 80), chapter_id,
                  oneline(e, 200))
        return ""
    except Exception as e:                  # a timeout, a dropped connection ...
        log.debug("%s: could not look up the page of chapter id %d: %s: %s", oneline(series.title, 80), chapter_id,
                  type(e).__name__, oneline(e, 200))
        return None
    return safe_url(url) or ""


def fill_pages(con, series_id: int, number: float) -> int:
    """Count the pages the verdict on the blocker `number` compares and no
    import counted (chapters linked before page counts were kept): chapter
    N with its pieces below the blocker, and the PAGES_NEAR whole chapters
    nearest to it. Only library files inside the library are read, and
    only their directories. Returns how many were counted. Committed."""
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not 0 <= number < verdict.MAX_NUMBER:
        return 0                            # also NaN
    b, whole = float(number), float(math.floor(number))
    have = {r["number"]: r for r in con.execute(
        "SELECT number, pages, library_path FROM chapter WHERE series_id=? AND status='have'", (series_id,))}
    pieces = [n for n in have if whole <= n < b]
    near = sorted((n for n in have if n == int(n) and n != whole), key=lambda n: abs(n - whole))[:PAGES_NEAR]
    counted = 0
    for n in dict.fromkeys(pieces + near):
        r = have[n]
        path = r["library_path"]
        if r["pages"] or not path or not library.is_within(path, config.LIBRARY_ROOT):
            continue
        pages = library.page_count(path)
        if pages:
            con.execute("UPDATE chapter SET pages=? WHERE series_id=? AND number=? AND pages IS NULL",
                        (pages, series_id, n))
            counted += 1
    if counted:
        con.commit()
        log.debug("series %d: counted the pages of %d chapter(s) for the verdict on ch %g", series_id, counted, b)
    return counted


# -- MangaDex lookups in the background ------------------------------------------

class Fetcher:
    """MangaDex lookups for stuck chapters, off every pass and page request:
    one thread, one lookup at a time (mangadex.py paces its requests and
    gives up rather than wait long), at most MAX_PENDING waiting (more are
    dropped; the next page view or pass asks again). Each one also counts
    the pages the verdict compares (fill_pages). Started with the web app
    and the daemon (start); until then requests only wait."""

    MAX_PENDING = 64

    def __init__(self, lookup=None):
        self._lookup = lookup or (lambda s, n: mangadex.english_chapters(s, n, fetch=True))
        self._cv = threading.Condition()
        self._pending: dict = {}            # key -> (series id, series, number), oldest first
        self._busy = None                   # the key being looked up
        self._thread: threading.Thread | None = None

    @staticmethod
    def _key(series: Series, number: float) -> tuple:
        return series.ref, math.floor(number)          # mangadex caches per series and whole chapter

    def request(self, series_id: int, series: Series, number: float) -> bool:
        """Have MangaDex looked up for this blocker. True when a lookup for
        it is waiting or running now."""
        if not math.isfinite(number) or series.manual:
            return False
        key = self._key(series, number)
        with self._cv:
            if key == self._busy or key in self._pending:
                return True
            if len(self._pending) >= self.MAX_PENDING:
                log.debug("MangaDex lookups for stuck chapters: %d waiting; not queueing %s ch %g", len(self._pending),
                          oneline(series.title, 80), number)
                return False
            self._pending[key] = (series_id, series, number)
            self._cv.notify_all()
        return True

    def pending(self) -> int:
        with self._cv:
            return len(self._pending) + (self._busy is not None)

    def start(self) -> None:
        with self._cv:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="mangarr-stuck-lookups", daemon=True)
            self._thread.start()
        log.debug("MangaDex lookups for stuck chapters started")

    def wait_idle(self, timeout: float) -> bool:
        """Until nothing is waiting or running (tests)."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._pending and self._busy is None, timeout)

    def _loop(self) -> None:
        while True:
            with self._cv:
                self._cv.wait_for(lambda: bool(self._pending))
                key = next(iter(self._pending))
                series_id, series, number = self._pending.pop(key)
                self._busy = key
            try:
                self._one(series_id, series, number)
            except Exception as e:                  # keep the thread alive for the next one
                log.warning("MangaDex lookup for %s ch %g failed: %s: %s", oneline(series.title, 80), number,
                            type(e).__name__, oneline(e, 200))
            finally:
                with self._cv:
                    self._busy = None
                    self._cv.notify_all()

    def _one(self, series_id: int, series: Series, number: float) -> None:
        try:
            with db.connect() as con:
                fill_pages(con, series_id, number)
        except Exception as e:
            log.warning("%s: could not count the pages around ch %g: %s: %s", oneline(series.title, 80), number,
                        type(e).__name__, oneline(e, 200))
        if not mangadex.looked_up(series, number):
            self._lookup(series, number)
            log.debug("%s ch %g: MangaDex's chapter list looked up for the stuck-behind verdict",
                      oneline(series.title, 80), number)


fetcher = Fetcher()
