"""The web UI and JSON API (FastAPI). Long operations go through the job
runner; requests only read the database and submit jobs.

Pages:   /  /series/{id}  /add  /import  /lists  /wanted  /activity  /activity/history
         /settings  /system  /system/logs
API:     /api/v1/...  (mirrors the pages; used by the pages' live updates)
"""
import asyncio
import dataclasses
import logging
import math
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import (
    __version__,
    backup,
    config,
    core,
    db,
    downloader,
    duplicates,
    health,
    jobs,
    komga,
    lanes,
    library,
    limits,
    metadata,
    metrics,
    model,
    notify,
    settings,
    stuck,
    updates,
)
from ..suwayomi import BREAKER_SECS, Client, SuwayomiError, SuwayomiUnreachable, with_cancel
from . import lists_routes, security, uploads, views

log = logging.getLogger(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))

runner = jobs.Runner()
health.watch_jobs(runner)                    # a running job that stops moving is a health warning
scheduler: jobs.Scheduler | None = None
client = Client()
STARTED = time.time()
PING_PATH = "/api/v1/ping"                   # liveness: no DB, no network, no login, no Host check
OPEN_PATHS = ("/api/v1/health", "/api/v1/system/status", "/metrics", PING_PATH)  # no login: monitoring + nav poller
UI_SUWAYOMI_TIMEOUT = 10                     # seconds; request handlers never wait on Suwayomi longer (one try)


class _UIClient:
    """The Suwayomi client as request handlers see it: the same client, but
    every call made through it has a short timeout and a single try. The job
    defaults (180 s x 3 tries with 5 s pauses) would let a hung Suwayomi pin a
    web worker thread for about 9 minutes per page load."""

    def __init__(self, base: Client, timeout: int):
        self._base, self._timeout = base, timeout

    def gq(self, query: str, variables: dict | None = None, timeout: int | None = None, retries: int = 1) -> dict:
        t = min(timeout or self._timeout, self._timeout)
        return self._base.gq(query, variables=variables, timeout=t, retries=1)

    def __getattr__(self, name):
        attr = getattr(self._base, name)
        func = getattr(attr, "__func__", None)
        if func is not None and getattr(type(self._base), name, None) is func:
            return func.__get__(self)            # a Client method: run it here so it calls our gq
        return attr                              # plain attributes (and test doubles) as they are


ui_client = _UIClient(client, UI_SUWAYOMI_TIMEOUT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    if not runner._thread.is_alive():
        runner.start()
        scheduler = jobs.Scheduler(runner, _job_refresh_all)
        scheduler.start()
    try:
        _startup_security()
    except Exception as e:
        log.error("could not open the database at %s: %s", config.DB_PATH, e)
    updates.start_background()
    backup.start_background()
    stuck.fetcher.start()                    # MangaDex lookups for the chapters series are stuck behind
    lists_routes.init()
    log.info("mang-arr %s web started (staging %s, library %s)", __version__, config.STAGING_ROOT,
             config.LIBRARY_ROOT)
    yield
    log.info("web shutting down")


def _startup_security() -> None:
    with db.connect() as con:
        if os.environ.get("MANGARR_RESET_LOGIN", "").strip().lower() in ("1", "true", "yes"):
            settings.reset_login(con)            # locked out: the operator restarts with MANGARR_RESET_LOGIN=1
        settings.ensure_security(con)            # API key, session secret, legacy clear-text password -> hash


app = FastAPI(title="mang-arr", version=__version__, docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.filters["ago"] = lambda ts: _ago(ts)
views.install(templates.env)
app.include_router(lists_routes.router)


def _ago(ts) -> str:
    if not ts:
        return "-"
    if isinstance(ts, str):
        try:
            ts = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return ts
    d = int(time.time() - ts)
    future = d < 0
    d = abs(d)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= size:
            return f"in {d // size}{unit}" if future else f"{d // size}{unit} ago"
    return f"in {d}s" if future else f"{d}s ago"


def _flash(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse(f"{path}{'&' if '?' in path else '?'}{views.flash_query(msg)}", 303)


def _series_url(series_id: int, page_no: int = 1) -> str:
    """The series page, on the page of chapter rows an action came from (the
    forms there carry ?page=N), so working through page 2 stays on page 2."""
    return f"/series/{series_id}" + (f"?page={page_no}" if page_no > 1 else "")


# -- jobs ---------------------------------------------------------------------

def _record_error(series_id: int, e: Exception) -> None:
    with db.connect() as con:
        con.execute("UPDATE series SET last_error=? WHERE id=?", (f"{type(e).__name__}: {e}"[:300], series_id))
        db.event(con, "failed", f"{type(e).__name__}: {e}"[:300], series_id)


def _job_refresh(series_id: int, download: bool):
    def run(job: jobs.Job):
        try:
            with db.connect() as con:
                o = core.refresh_series(con, client, series_id, download=download,
                                        should_cancel=lambda: job.cancel,
                                        progress=lambda m: setattr(job, "progress", m))
        except core.Gone as e:
            return str(e)
        except limits.Cancelled:
            raise                                  # asked to stop: not an error of the series
        except Exception as e:
            _record_error(series_id, e)
            raise
        return (f"{len(o.plan.chapters)} listed, {o.downloaded} downloaded, {o.failed} failed,"
                f" {o.imported} imported")
    return run


def _job_add(series: model.Series, download: bool, monitored: bool = True):
    def run(job: jobs.Job):
        with db.connect() as con:
            try:
                o = core.add_series(con, client, series, download=download and monitored,
                                    should_cancel=lambda: job.cancel, progress=lambda m: setattr(job, "progress", m))
            except duplicates.AlreadyTracked as e:     # added under its other reference since this was queued
                return str(e)
            if not monitored:
                db.set_monitored(con, o.series_id, False)
        job.series_id = o.series_id
        notify.send(f"Added: {series.title}", f"{len(o.plan.chapters)} chapters listed, {o.downloaded} downloaded",
                    "added")
        return f"{len(o.plan.chapters)} listed, {o.downloaded} downloaded, {o.imported} imported" + \
            ("" if monitored else " (unmonitored)")
    return run


# a series with these statuses gets no new chapters: once complete it is re-checked only now and then
COMPLETE_STATUSES = ("FINISHED", "CANCELLED")


def plan_pass(rows, due: dict | None = None) -> tuple[list, list]:
    """Order a refresh pass so the download lanes get work within its first
    minute: series with chapters due now first (`due`: series id -> wanted
    chapters and failed ones whose next attempt is due, db.chapters_due;
    without it, any wanted or failed chapter counts), then continuing
    (RELEASING) series, then the rest, each group in the order given.
    Unmonitored series are left out. A finished or cancelled series that is
    complete (_complete: chapters on disk, nothing wanted or failed, no
    error) and was checked less than recheck_finished_days ago is in none
    of these: those come back separately as (row, days until its next
    check), for the end of the pass (_run_pass skips them unless their
    status changed). Returns (rows, skippable)."""
    days = limits.clamp("recheck_finished_days", settings.get("recheck_finished_days") or 0)
    now_ = time.time()
    groups: tuple[list, list, list] = ([], [], [])
    skippable = []
    for r in rows:
        if not r["monitored"]:
            continue
        n_due = due.get(r["id"], 0) if due is not None else r["wanted"]
        if days and _complete(r) and r["last_resolved"]:
            try:
                left = time.mktime(time.strptime(r["last_resolved"], "%Y-%m-%d %H:%M:%S")) + days * 86400 - now_
            except ValueError:
                left = 0
            if left > 0:
                skippable.append((r, max(1, math.ceil(left / 86400))))
                log.debug("pass order: %s last, complete and %s (skipped unless its status changed)",
                          r["title"], r["status"].lower())
                continue
        k, why = (0, f"{n_due} chapter(s) due") if n_due else (1, "continuing") if r["status"] == "RELEASING" \
            else (2, "nothing due")
        log.debug("pass order: %s in group %d (%s)", r["title"], k + 1, why)
        groups[k].append(r)
    return groups[0] + groups[1] + groups[2], skippable


def _complete(r) -> bool:
    """A finished or cancelled series with chapters on disk and nothing
    wanted or failed (failed ones keep their retry schedule), whose last
    check went through: not one no source matched, nor one whose chapters
    all went unavailable."""
    return r["status"] in COMPLETE_STATUSES and not r["wanted"] and bool(r["have"]) and not r["last_error"]


def _skip_text(r, days_left: int) -> str:
    """The item text of a complete finished series the pass skips."""
    status = "cancelled" if r["status"] == "CANCELLED" else "finished"
    gone = (r["listed"] or 0) - (r["have"] or 0)            # nothing wanted or failed: unavailable ones
    what = f"{status}, nothing to download ({gone} chapter(s) no source lists)" if gone > 0 else \
        f"complete and {status}"
    return f"skipped: {what}; next check in about {days_left} day(s)"


class PassStopped(SuwayomiUnreachable):
    """A pass gave up because Suwayomi stopped answering. Carries what the
    pass got done before that, (done, downloaded, imported, errors), so the
    job can still report and notify about it."""

    def __init__(self, msg: str, counts: tuple[int, int, int, int]):
        super().__init__(msg)
        self.counts = counts


def _run_pass(job: jobs.Job, rows, label: str, skippable=()) -> tuple[int, int, int, int]:
    """Refresh every series in rows, keeping job.items current so the
    Activity page shows the whole pass: what is queued, being resolved,
    waiting for a download lane or downloading, and what happened to each.
    Returns (done, downloaded, imported, errors).

    `skippable` (plan_pass: complete finished series, with the days until
    their next check) come last. Once the pass gets to them their status is
    asked from AniList / MangaDex in one go (no source is searched): one
    that is still finished or cancelled, with the same chapter count, is
    skipped; one that is not (it goes on after all) is refreshed like the
    others.

    Series are resolved one after the other on this thread. The chapters due
    are downloaded by download lanes (lanes.LanePool: up to Download Lanes
    sites at once, one series per site) while the next series is resolved.

    When Suwayomi itself stops answering, the pass waits one breaker window
    and goes on; if Suwayomi is still down it stops with one clear error
    (PassStopped, with the counts so far) instead of timing out on every
    series. One outage seen by the resolve step and several lanes at once
    counts once (lanes.Outages)."""
    later = {r["id"]: (r, left) for r, left in skippable}
    rows = list(rows) + [r for r, _ in skippable]
    job.items = [{"series_id": r["id"], "title": r["title"], "state": "queued", "result": ""} for r in rows]
    status_now: dict | None = None                 # what the providers say of the skippable ones now
    downloader.retry_leftovers_now(client)         # queue entries an earlier run could not take back out
    outages = lanes.Outages()
    pool: lanes.LanePool | None = None
    downloaded = imported = errors = 0
    reached = 0                                    # series whose resolve was started
    busy: downloader.LockBusy | None = None

    def cancel() -> bool:
        return job.cancel

    def say(text: str) -> None:
        if pool is None:
            job.progress = text
        else:
            pool.resolving(text)

    def not_checked(rest: list, why: str) -> None:
        """The series the pass does not get to: the complete finished ones
        are skipped as planned (the stop did not cut them: they would have
        been, unless their status changed, and the next pass asks again)."""
        for it in rest:
            if it["series_id"] in later:
                r, left = later[it["series_id"]]
                it["state"], it["result"] = "skipped", _skip_text(r, left)
            else:
                it["state"], it["result"] = "cancelled", why
    try:
        for i, (r, item) in enumerate(zip(rows, job.items, strict=True), 1):
            head = f"{i}/{len(rows)}: {r['title']}"
            if (left := outages.hold_left()) and not job.cancel:
                say(f"{head} - Suwayomi is not answering; waiting {left:.0f} s before going on")
                limits.pause(left, cancel)
                outages.served()
            if job.cancel:
                done = sum(1 for it in job.items if it["state"] in lanes.FINISHED)
                say(f"cancelled after {done} of {len(rows)}")
                not_checked(job.items[i - 1:], "pass cancelled")
                break
            if outages.stopped or (pool is not None and pool.cancelled()):
                not_checked(job.items[i - 1:], "pass stopped: " + ("Suwayomi is not answering" if outages.stopped
                                                                  else pool.stop_why))
                break
            reached = i
            if r["id"] in later:
                if status_now is None:
                    say(f"{head} - checking whether {len(later)} complete finished series go on")
                    try:
                        status_now = _statuses_now([row for row, _ in later.values()], cancel)
                    except limits.Cancelled:        # the check above ends the pass
                        item["state"], item["result"] = "cancelled", "pass cancelled"
                        continue
                if not _goes_on(r, status_now.get(r["ref"])):
                    item["state"], item["result"] = "skipped", _skip_text(r, later[r["id"]][1])
                    continue
            say(head)
            item["state"], item["result"] = "running", "checking sources"
            job.active_series_id = r["id"]         # pending_for() sees the series this pass is on

            def prog(m, head=head, item=item):
                item["result"] = m
                say(f"{head} - {m}")
            try:
                with db.connect() as con:
                    o = core.refresh_series(con, client, r["id"], download=False, should_cancel=cancel,
                                            progress=prog)
                    downloaded += o.downloaded
                    imported += o.imported
                    due = core.downloads_due(con, o)
                    if not due:
                        item["state"], item["result"] = core.describe_outcome(con, r["id"], o)
                outages.ok()
                if due:
                    if pool is None:
                        n, cap = lanes.effective_lanes(with_cancel(client, cancel))
                        fresh = lanes.LanePool(client, job, n, label, outages, cap=cap,
                                               on_error=lambda sid, e: _record_error(sid, e))
                        fresh.start()              # not one lane could be started: this series fails, the next tries
                        pool = fresh
                    pool.submit(i, r["id"], r["title"], item, o.plan, due, progress=prog)
            except core.Gone:
                item["state"], item["result"] = "cancelled", "series was deleted"
                continue
            except limits.Cancelled:               # not an error of the series; the check above ends the pass
                item["state"], item["result"] = "cancelled", "pass cancelled"
                continue
            except lanes.PoolStopped as e:          # the lanes stopped while it was handed over: the same
                item["state"], item["result"] = "cancelled", "pass cancelled" if job.cancel else f"pass stopped: {e}"
                continue
            except downloader.LockBusy as e:
                errors += 1
                item["state"], item["result"] = "error", f"{type(e).__name__}: {e}"[:300]
                not_checked(job.items[i:], "pass stopped: another download run holds the download lock")
                busy = e
                break
            except Exception as e:
                errors += 1
                item["state"], item["result"] = "error", f"{type(e).__name__}: {e}"[:300]
                log.error("%s: %s: %s: %s", label, r["title"], type(e).__name__, e)
                try:                               # bookkeeping must never end the pass
                    _record_error(r["id"], e)
                except Exception as rec:
                    log.warning("%s: could not record the error for %s: %s: %s", label, r["title"],
                                type(rec).__name__, rec)
                if isinstance(e, SuwayomiUnreachable):
                    if outages.report(e) == "stop":
                        not_checked(job.items[i:], "pass stopped: Suwayomi is not answering")
                        if pool is not None:
                            pool.stop("Suwayomi is not answering")
                        break
                    say(f"{head} - Suwayomi is not answering; waiting {BREAKER_SECS} s before going on")
                    log.warning("%s: Suwayomi is not answering; waiting %d s, the pass stops if it still is",
                                label, BREAKER_SECS)
            finally:
                job.active_series_id = None
        if pool is not None and not outages.stopped and busy is None:
            pool.close()
            pool.join()
    finally:
        if pool is not None:
            pool.shutdown()                        # waits for every lane: nothing of the pass writes after it
    if pool is not None:
        d, im, er = pool.counts()
        downloaded, imported, errors = downloaded + d, imported + im, errors + er
    done = sum(1 for it in job.items if it["state"] in lanes.FINISHED and it["state"] != "skipped")
    if busy is not None:
        raise busy
    if outages.stopped:                            # by the resolve step or by the lanes
        left = sum(1 for it in job.items if it["state"] == "cancelled")
        log.error("%s: Suwayomi is not answering; stopping the pass after %d of %d series (%d left)",
                  label, reached, len(rows), left)
        raise PassStopped(_pass_stopped_text(job.items, reached, outages.why), (done, downloaded, imported, errors))
    return done, downloaded, imported, errors


def _statuses_now(rows, cancel) -> dict:
    """metadata.statuses for these series rows, by reference; {} when it
    cannot be had (then every one of them is skipped as planned). A cancel
    cuts a slow provider short (limits.Cancelled)."""
    try:
        return limits.interruptible(lambda: metadata.statuses([r["ref"] for r in rows]), cancel)
    except limits.Cancelled:
        raise
    except Exception as e:
        log.warning("could not check whether complete finished series go on: %s: %s", type(e).__name__, e)
        return {}


def _goes_on(r, now: tuple | None) -> bool:
    """A complete finished series is refreshed after all: its provider has
    it continuing (or on hiatus) now, or with another chapter count."""
    if now is None:
        return False
    status, chapters = now
    changed = status not in COMPLETE_STATUSES or (chapters is not None and chapters != r["expected"])
    if changed:
        log.info("%s: now %s with %s chapter(s) (was %s, %s); checking it in this pass after all", r["title"],
                 status, chapters, r["status"], r["expected"])
    return changed


_CUT = re.compile(r"pass (?:cancelled|stopped: .*?)(?: after (\d+) chapter\(s\) downloaded)?\Z", re.S)


def _pass_stopped_text(items: list, reached: int, why: str) -> str:
    """The message of a pass Suwayomi stopped: how far the resolve got, the
    series it never checked, and the checked ones the stop cut (the download
    lanes stop with it): before any of their chapters arrived, or after some
    did. A series that finished, failed or was deleted meanwhile is none of
    these, nor is a complete finished one it did not get to (skipped as
    planned: not_checked in _run_pass)."""
    parts = []
    later = items[reached:]
    if unchecked := sum(1 for it in later if it["state"] != "skipped"):
        parts.append(f"{unchecked} not checked")
    if skipped := len(later) - unchecked:
        parts.append(f"{skipped} complete finished series skipped")
    cut = [m for it in items[:reached] if it["state"] == "cancelled" and (m := _CUT.match(it.get("result") or ""))]
    if none := sum(1 for m in cut if not m.group(1)):
        parts.append(f"{none} not downloaded")
    if partly := len(cut) - none:
        parts.append(f"{partly} downloaded in part")
    return (f"pass stopped after {reached} of {len(items)} series" + "".join(f", {p}" for p in parts)
            + f": Suwayomi is not answering ({why})")


def _job_refresh_all(job: jobs.Job):
    with db.connect() as con:
        duplicates.read_links(con)                 # the AniList links of MangaDex series, skipped ones too
        due = db.chapters_due(con, bool(settings.get("download_in_order")))
        rows, skippable = plan_pass(db.series_rows(con), due)
    log.info("refresh pass: %d series (%d with chapters due first, then %d continuing), and %d complete finished "
             "series last, skipped unless their status changed", len(rows), sum(1 for r in rows if due.get(r["id"])),
             sum(1 for r in rows if not due.get(r["id"]) and r["status"] == "RELEASING"), len(skippable))
    stopped = None
    try:
        done, downloaded, imported, errors = _run_pass(job, rows, "refresh-all", skippable)
    except PassStopped as e:        # Suwayomi went away: still notify about what the pass did before that
        stopped, (done, downloaded, imported, errors) = e, e.counts
    skipped = sum(1 for i in job.items if i["state"] == "skipped")
    msg = f"{done} series, {downloaded} downloaded, {imported} imported, {errors} errors" + \
        (f", {skipped} complete finished series skipped" if skipped else "")
    if imported:
        new = [f"{i['title']}: {i['result']}" for i in job.items if i["state"] == "done" and "downloaded" in i["result"]
               and not i["result"].startswith("0 downloaded")]
        notify.send(f"mang-arr: {imported} new chapter(s)", "\n".join(new[:15]) or msg, "new")
    bad = [f"{i['title']}: {i['result']}" for i in job.items if i["state"] in ("failed", "error")]
    if bad:
        notify.send(f"mang-arr: {len(bad)} series with failed downloads", "\n".join(bad[:10]), "failed")
    if stopped:
        raise stopped               # the job still ends failed, with the one clear error
    return msg


def _job_refresh_metadata(job: jobs.Job):
    """Metadata only, every series: fast, no source searching."""
    with db.connect() as con:
        rows = db.series_rows(con)
    ok = errors = 0
    for i, r in enumerate(rows, 1):
        if job.cancel:
            break
        job.progress = f"{i}/{len(rows)}: {r['title']}"
        try:
            with db.connect() as con:
                core.refresh_metadata(con, r["id"])
            ok += 1
        except core.Gone:
            continue
        except Exception as e:
            errors += 1
            log.warning("metadata refresh: %s: %s: %s", r["title"], type(e).__name__, e)
    return f"{ok} series refreshed, {errors} failed"


def _job_search_wanted(job: jobs.Job):
    """Download-only pass over every series with wanted chapters."""
    with db.connect() as con:
        rows = db.wanted_all(con)
    done, downloaded, imported, errors = _run_pass(job, rows, "search wanted")
    return f"{len(rows)} series searched, {downloaded} chapters downloaded, {errors} errors"


# -- middleware / errors -------------------------------------------------------
#
# Every request passes, in order:
#   1. the body limit (security.BodyLimit): bodies over MAX_BODY (BODY_LIMITS per path) get 413
#   2. settings: if they have never been readable, 503 (the login state is unknown: fail closed)
#   3. the Host check: only IPs, localhost, LAN names and allowed_hosts (DNS rebinding)
#   4. the CSRF check: a POST/PUT/PATCH/DELETE is refused (403) when the browser's
#      Sec-Fetch-Site is anything but same-origin/none, or, without it (plain HTTP, older
#      browsers), when its Origin (else Referer) is not this server's scheme://host:port;
#      requests with none of these headers (curl, scripts) go on to the normal login check,
#      and a valid X-Api-Key header skips this check
#   5. the login check (when a login is set): X-Api-Key header, ?apikey= (GET under /api/
#      only), the session cookie, or Basic credentials (auth_method basic only); password
#      attempts are throttled per client address, each one reserved before its check
#      (security.check_password)
# and every response gets the security headers (CSP: scripts only from /static).
# Handlers that change settings or hand out secrets check the sign-in again against the
# values they act on (security.still_authorized): it may have changed since step 5.
# The helpers live in security.py.

@app.middleware("http")
async def authentication(request: Request, call_next):
    """Optional login (Settings -> Security): 'forms' shows a login page and
    keeps a signed session cookie; 'basic' uses the browser prompt. The API
    key works with either. Monitoring endpoints and static files stay open.
    Also the Host and CSRF checks; see the comment at the top of this section."""
    path = request.url.path
    if path == PING_PATH:
        return await call_next(request)
    # the settings cache is read inline; only a stale one (every TTL) costs a database read, off the event loop
    v = await run_in_threadpool(settings.all_values) if settings.stale() else settings.all_values()
    ip = security.client_ip(request)
    if not settings.available() and not path.startswith("/static/"):
        return Response("mang-arr cannot read its settings database, so it does not know whether a login is "
                        "required: refusing requests until it can (see the log)\n", 503, media_type="text/plain")

    host = request.headers.get("host", "")
    if not security.host_allowed(host, security.allowed_hosts(v)):
        name = security.hostname(host)          # what to add: the name alone (ports are never compared)
        if security.log_budget.allow(ip):
            log.warning("refused request for host %s from %s: not an allowed host name (DNS rebinding protection;"
                        " add %s to MANGARR_ALLOWED_HOSTS or Settings -> Security)", security.clip(host), ip,
                        security.clip(name) if name else "nothing: it is not a host name")
        if not name:
            return Response(f"mang-arr: the Host header {security.clip(host)} is not a host name.\n", 400,
                            media_type="text/plain")
        return Response(f"mang-arr: the host name {security.clip(name)} is not allowed. Open mang-arr by IP address, "
                        f"localhost or a LAN name, or add {security.clip(name)} (the name alone, without a port) to "
                        "MANGARR_ALLOWED_HOSTS or Settings -> Security -> Allowed Host Names.\n", 400,
                        media_type="text/plain")

    via = "apikey" if security.key_ok(v, request.headers.get("x-api-key")) else ""
    if request.method not in security.SAFE_METHODS and not via:
        ok, why = security.same_origin(request)
        if not ok:
            if security.log_budget.allow(ip):
                log.warning("refused cross-site %s %s from %s: %s (Host %s)",
                            request.method, security.clip(path, 200), ip, why, security.clip(host))
            return Response("mang-arr: cross-site request refused (it did not come from mang-arr's own pages: "
                            "another site, or another port on this host). Behind a reverse proxy, pass the "
                            "original Host header with its port ($http_host in nginx) or send X-Forwarded-Host / "
                            "X-Forwarded-Port, and X-Forwarded-Proto when the proxy serves HTTPS.\n", 403,
                            media_type="text/plain")

    if not v["auth_user"]:
        request.state.authed, request.state.via = True, "open"       # no login configured: everyone is admin
    else:
        if not via:
            q = request.query_params.get("apikey")
            if q and request.method in ("GET", "HEAD") and path.startswith("/api/") and security.key_ok(v, q):
                via = "apikey"
        if not via and security.session_ok(v, request.cookies.get(security.SESSION_COOKIE)):
            via = "session"
        if not via and v["auth_method"] == "basic":
            creds = security.basic_credentials(request.headers.get("authorization", ""))
            if creds:
                ok, wait = await security.check_password(v, ip, *creds)
                if wait:
                    return security.too_many(wait, "logins from this address")
                if ok:
                    via = "basic"
                    request.state.login = security.login_of(v)      # for security.still_authorized
                elif security.log_budget.allow(ip):
                    blocked = security.throttle.retry_after(ip)
                    log.warning("failed basic auth for %s from %s%s", security.clip(creds[0]), ip,
                                f"; further attempts refused for {blocked} s" if blocked else "")
        request.state.authed, request.state.via = bool(via), via
        if not via and not (path in OPEN_PATHS or path.startswith(("/static/", "/login", "/logout"))):
            if security.log_budget.allow(ip):
                log.warning("unauthenticated request to %s from %s", security.clip(path, 200), ip)
            wants_html = "text/html" in request.headers.get("accept", "") and not path.startswith("/api/")
            if v["auth_method"] == "forms" and wants_html:
                return RedirectResponse(f"/login?next={urllib.parse.quote(str(request.url.path))}", 303)
            prompt = {"WWW-Authenticate": 'Basic realm="mang-arr"'} if v["auth_method"] == "basic" else None
            return Response("authentication required", 401, headers=prompt)   # forms mode: no Basic prompt
    response = await call_next(request)
    security.security_headers(path, response)
    if via == "basic" and v.get("session_secret"):
        # Basic credentials checked once: from now on this browser is signed in by its session cookie
        # (checked before Basic), so other clients' failed passwords from a shared address cannot lock it out
        security.set_session(request, response, v, persistent=False)
    return response


app.add_middleware(security.BodyLimit)


def _login_form(request: Request, target: str, error: str | None, status: int = 200, headers=None):
    return templates.TemplateResponse(request, "login.html", {"request": request, "next": target,
                                                              "version": __version__, "error": error},
                                      status_code=status, headers=headers)


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    v = settings.all_values()
    target = security.safe_next(next)
    if not v["auth_user"] or security.session_ok(v, request.cookies.get(security.SESSION_COOKIE)):
        return RedirectResponse(target, 303)
    return _login_form(request, target, _no_password_note(v))


def _no_password_note(v: dict) -> str | None:
    """A login left without a password (an older version allowed it, or its
    value is unreadable) cannot be used: say how to get back in."""
    if v["auth_user"] and not v["auth_password"]:
        return ("A username is set without a password, so nobody can sign in. Restart mang-arr once with "
                "MANGARR_RESET_LOGIN=1 to clear the login, or set a password through the API with the X-Api-Key "
                "header.")
    return None


@app.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    """Async on purpose: the failure delay is an asyncio sleep, so a flood of
    bad logins holds no worker threads; the hash check runs in the pool. The
    attempt is reserved before the check (security.check_password), so
    parallel guesses count at once."""
    ip, target = security.client_ip(request), security.safe_next(next)
    v = await run_in_threadpool(settings.all_values) if settings.stale() else settings.all_values()
    ok, wait = await security.check_password(v, ip, username, password)
    if wait:
        if security.log_budget.allow(ip):
            log.warning("login from %s refused: too many failures, blocked for %d s more", ip, wait)
        return _login_form(request, target, f"too many failed sign-ins: try again in {wait} s", 429,
                           {"Retry-After": str(wait)})
    if not ok:
        blocked = security.throttle.retry_after(ip)             # the failure was counted when it was reserved
        if security.log_budget.allow(ip):
            log.warning("failed login for %s from %s%s", security.clip(username), ip,
                        f"; further attempts refused for {blocked} s" if blocked else "")
        await asyncio.sleep(1)                   # slow down guessing (without holding a thread)
        return _login_form(request, target, _no_password_note(v) or "wrong username or password", 401)
    if not v.get("session_secret"):              # startup could not create it (DB was unavailable then)
        v = await run_in_threadpool(_ensure_security)
    resp = RedirectResponse(target, 303)
    security.set_session(request, resp, v)
    log.info("login: %s from %s", security.clip(username), ip)
    return resp


def _ensure_security() -> dict:
    with db.connect() as con:
        settings.ensure_security(con)
    return settings.all_values()


def _revoke_session(v: dict, cookie: str | None) -> None:
    """Sign out the session this cookie belongs to, server side (a copied
    cookie stops working too)."""
    s = security.parse_session(v, cookie)
    if not s:
        return
    now = time.time()
    keep = [r for r in v.get("revoked_sessions") or [] if str(r).split(":", 1)[-1].isdigit()
            and int(str(r).split(":", 1)[-1]) > now]
    with db.connect() as con:
        if len(keep) >= security.MAX_REVOKED:
            settings.logout_everywhere(con)
        else:
            settings.set_many(con, {"revoked_sessions": [*keep, f"{s[0]}:{s[1]}"]}, internal=True)


@app.post("/logout")
def logout(request: Request):
    try:
        _revoke_session(settings.all_values(), request.cookies.get(security.SESSION_COOKIE))
    except Exception as e:
        log.error("logout: could not record the signed-out session: %s: %s", type(e).__name__, e)
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(security.SESSION_COOKIE)
    return resp


@app.exception_handler(jobs.QueueFull)
async def _queue_full(request: Request, exc: jobs.QueueFull):
    """Too many jobs waiting: 429, as JSON for the API and as a message on the Activity page otherwise."""
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": str(exc)}, status_code=429)
    return Response(f"mang-arr: job queue is full: {exc}", 429, media_type="text/plain")


@app.post("/settings/logout-all")
def logout_all(request: Request):
    try:
        with db.connect() as con:
            settings.logout_everywhere(con, still_allowed=_still(request))
    except settings.NotAllowed:
        return _sign_in_lost(request)
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(security.SESSION_COOKIE)
    return resp


@app.post("/settings/api-key/regenerate")
def api_key_regenerate(request: Request):
    try:
        with db.connect() as con:
            settings.rotate_api_key(con, still_allowed=_still(request))
    except settings.NotAllowed:
        return _sign_in_lost(request)
    log.info("API key regenerated; every session signed out")
    resp = _flash("/settings", "new API key generated: update every script that used the old one")
    _reissue_session(request, resp)
    return resp


def _still(request: Request):
    """still_allowed for settings.write / backup.restore: this request's
    sign-in, checked again against the values stored when it acts."""
    return lambda v: security.still_authorized(request, v)


def _sign_in_lost(request: Request) -> Response:
    """The answer when a request's sign-in no longer holds by the time its
    handler acts (see security.still_authorized): nothing was done."""
    ip = security.client_ip(request)
    if security.log_budget.allow(ip):
        log.warning("refused %s %s from %s: its sign-in no longer holds (a login was switched on, or the API key, "
                    "password or sessions changed, while it waited); nothing done", request.method,
                    security.clip(request.url.path, 200), ip)
    return Response("mang-arr: not signed in any more (a login was switched on, or the API key, password or "
                    "sessions changed, while this request waited); nothing was done. Sign in and try again.\n",
                    401, media_type="text/plain")


def _reissue_session(request: Request, resp: Response) -> None:
    """After a change that signs every session out (API key, password), keep
    the browser that made the change signed in with a fresh cookie."""
    v = settings.all_values()
    if v["auth_user"] and getattr(request.state, "via", "") == "session" and v["auth_method"] == "forms":
        security.set_session(request, resp, v)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, security.clip(request.url.path, 200))
    # details only for a signed-in caller, never on the open endpoints
    if getattr(request.state, "authed", False) and request.url.path not in OPEN_PATHS:
        detail = f"{type(exc).__name__}: {exc}"
    else:
        detail = "internal error"
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": detail}, status_code=500)
    return Response(f"mang-arr: {detail}\n(see the log on the System page)", 500, media_type="text/plain")


@app.get(PING_PATH)
async def api_ping():
    """Liveness: answers as long as the event loop runs. No database, no
    network, no health checks, no login (the Docker HEALTHCHECK uses it)."""
    return {"ok": True}


# -- pages --------------------------------------------------------------------

def page(request: Request, name: str, **ctx):
    h = health.summary(ui_client)                    # cached; never waits long on a backend
    v = settings.all_values()
    ctx.update(request=request, version=__version__, current=runner.current,
               flash=views.flash_from(request.query_params), update=updates.status(),
               health_errors=h["errors"], health_warnings=h["warnings"],
               logged_in=bool(v["auth_user"] and v["auth_method"] == "forms"))
    return templates.TemplateResponse(request, name, ctx)


@app.get("/")
def index(request: Request, q: str = ""):
    with db.connect() as con:
        rows = db.series_rows(con)
    if q:
        rows = [r for r in rows if q.lower() in r["title"].lower()]
    return page(request, "index.html", rows=rows, q=q, library_root=config.LIBRARY_ROOT)


@app.get("/series/{series_id}")
def series_page(request: Request, series_id: int, page_no: int = Query(1, alias="page")):
    """The series details page. The counts cover every chapter; the chapter
    rows are paged (views.page_slices), so a source listing tens of
    thousands of chapters cannot make one response huge.

    The counts, the tooltips' runs of numbers and the groups' totals need
    every row: one pass reads number, status and name of each as a plain
    tuple and keeps totals (views.chapter_summary; a season keeps its
    chapter numbers too), and whole rows are read for the page shown only.
    That pass still grows with the rows stored. Measured with 100,000 rows:
    about 0.7 s and 2 MB for a series in blocks (was 1 s and 26 MB), about
    1.7 s and 5 MB for one in seasons, whose names are parsed (was about
    2 s and 45 MB); rendering the page's 2000 rows adds about 0.8 s either
    way. Going below that would need the totals kept in the database and
    updated with every chapter write, the runs of numbers included: more
    machinery than a count real series stay far below is worth."""
    with db.connect() as con:
        r = db.get_series(con, series_id)
        if not r:
            raise HTTPException(404, "no such series")
        srcs = db.sources(con, series_id)
        if not con.in_transaction:
            con.execute("BEGIN")                    # one snapshot: the totals agree with the rows shown
        summary = views.chapter_summary(db.chapter_marks(con, series_id))

        def read(slices, info):
            if slices and "numbers" not in summary["groups"][slices[0][0]]:     # blocks: one range by number
                return db.chapters_newest_first(con, series_id, info["last"] - info["first"] + 1, info["first"] - 1)
            numbers = [n for gi, start, take in slices for n in summary["groups"][gi]["numbers"][start:start + take]]
            whole = db.chapters_by_number(con, series_id, numbers)
            return [whole[n] for n in numbers if n in whole]
        groups, paging = views.page_summary(summary["groups"], page_no, read)
        events = con.execute("SELECT * FROM event WHERE series_id=? ORDER BY id DESC LIMIT 15",
                             (series_id,)).fetchall()
        size_fn = getattr(db, "series_size", None)          # lands on main; (bytes, files)
        size_bytes, size_files = size_fn(con, series_id) if size_fn else (0, 0)
        # the chapters it is stuck behind, with their verdicts; a link from another site looks nothing up
        blocked = stuck.details(con, r, fetch=not _cross_site(request))
        skips = stuck.skipped(con, series_id)
    marked = {st.number for st in blocked} | {k["number"] for k in skips}
    for g in groups:                                # their rows (verdict label, Un-skip) are shown, not folded
        if any(c["number"] in marked for c in g["chapters"]):
            g["open"] = True
    if paging["pages"] > 1:
        log.debug("series %d: %d chapter rows, showing page %d of %d", series_id, paging["total"], paging["page"],
                  paging["pages"])
    return page(request, "series.html", s=r, series=db.series_to_model(r), sources=srcs,
                runs=summary["runs"], events=events, busy=runner.pending_for(series_id),
                groups=groups, paging=paging, counts=summary["counts"],
                page_q=f"?page={paging['page']}" if paging["page"] > 1 else "",     # actions come back to this page
                description=views.plain_description(r["description"]),
                library_path=library.library_dir(r["folder"] or ""), size_bytes=size_bytes, size_files=size_files,
                size_human=views.human_size(size_bytes), ref_url=views.ref_url(r), stuck=blocked,
                stuck_at={st.number: st for st in blocked}, skipped={k["number"]: k for k in skips},
                skip_notes=skips,
                auto_skip=bool(settings.get("auto_skip_side_stories") and settings.get("download_in_order")))


@app.post("/series/{series_id}/refresh")
def series_refresh(series_id: int, download: str = Form("1"), page_no: int = Query(1, alias="page")):
    with db.connect() as con:
        r = db.get_series(con, series_id)
    if not r:
        raise HTTPException(404)
    if runner.pending_for(series_id):
        return _flash(_series_url(series_id, page_no), "a job for this series is already queued")
    runner.submit("refresh", r["title"], _job_refresh(series_id, download == "1"), series_id)
    return _flash(_series_url(series_id, page_no), "refresh queued")


@app.post("/series/{series_id}/monitor")
def series_monitor(series_id: int, monitored: str = Form("1"), page_no: int = Query(1, alias="page")):
    with db.connect() as con:
        if not db.get_series(con, series_id):
            raise HTTPException(404)
        db.set_monitored(con, series_id, monitored == "1")
    return RedirectResponse(_series_url(series_id, page_no), 303)


def _job_chapter(series_id: int, number: float, manga_id: int | None):
    def run(job: jobs.Job):
        with db.connect() as con:
            return core.download_chapter(con, client, series_id, number, manga_id, should_cancel=lambda: job.cancel,
                                         progress=lambda m: setattr(job, "progress", m))
    return run


def _chapter_key(series_id: int, number: float, manga_id: int | None) -> str:
    """One queued/running job per (series, chapter, source entry)."""
    return f"chapter:{series_id}:{number:g}:{manga_id or 'auto'}"


@app.post("/series/{series_id}/chapter/{number}/search")
def chapter_search(series_id: int, number: float, page_no: int = Query(1, alias="page")):
    with db.connect() as con:
        r = db.get_series(con, series_id)
    if not r:
        raise HTTPException(404)
    runner.submit("chapter", f"{r['title']} ch {number:g}", _job_chapter(series_id, number, None), series_id,
                  key=_chapter_key(series_id, number, None))
    return _flash(_series_url(series_id, page_no), f"search for chapter {number:g} queued")


@app.post("/series/{series_id}/chapter/{number}/download")
def chapter_download(series_id: int, number: float, manga_id: int = Form(...),
                     page_no: int = Query(1, alias="page")):
    with db.connect() as con:
        r = db.get_series(con, series_id)
        src = next((x for x in db.sources(con, series_id) if x["manga_id"] == manga_id), None)
    if not r:
        raise HTTPException(404)
    if not src:
        return _flash(_series_url(series_id, page_no), "that source entry does not belong to this series")
    runner.submit("chapter", f"{r['title']} ch {number:g} from {src['source_name']}",
                  _job_chapter(series_id, number, manga_id), series_id, key=_chapter_key(series_id, number, manga_id))
    return _flash(_series_url(series_id, page_no), f"download of chapter {number:g} from {src['source_name']} queued")


@app.get("/api/v1/series/{series_id}/chapter/{number}")
def api_chapter(series_id: int, number: float):
    """One chapter: its row, file size, and the history events that mention it."""
    with db.connect() as con:
        r = db.get_series(con, series_id)
        if not r:
            raise HTTPException(404)
        c = con.execute("SELECT * FROM chapter WHERE series_id=? AND number=?", (series_id, number)).fetchone()
        if not c:
            raise HTTPException(404, "no such chapter")
        needle = f"ch {number:g}"
        events = [dict(e) for e in con.execute(
            "SELECT at, kind, message FROM event WHERE series_id=? AND (message LIKE ? OR message LIKE ?)"
            " ORDER BY id DESC LIMIT 20", (series_id, f"%{needle}:%", f"%chapter {number:g}%"))]
    d = dict(c)
    d["size"] = os.path.getsize(c["library_path"]) if c["library_path"] and os.path.exists(c["library_path"]) else None
    d["events"] = events
    return d


@app.get("/api/v1/series/{series_id}/chapter/{number}/releases")
def api_chapter_releases(series_id: int, number: float):
    with db.connect() as con:
        if not db.get_series(con, series_id):
            raise HTTPException(404)
        return core.chapter_releases(con, ui_client, series_id, number)    # short Suwayomi timeouts


@app.post("/api/v1/series/{series_id}/chapter/{number}/search")
def api_chapter_search(series_id: int, number: float, manga_id: int | None = None):
    with db.connect() as con:
        r = db.get_series(con, series_id)
    if not r:
        raise HTTPException(404)
    return runner.submit("chapter", f"{r['title']} ch {number:g}", _job_chapter(series_id, number, manga_id),
                         series_id, key=_chapter_key(series_id, number, manga_id)).as_dict()


@app.post("/series/{series_id}/chapter/{number}/ignore")
def chapter_ignore(series_id: int, number: float, page_no: int = Query(1, alias="page")):
    with db.connect() as con:
        db.set_status(con, series_id, number, "ignored")
        db.event(con, "ignore", f"chapter {number:g} ignored", series_id)
    return RedirectResponse(_series_url(series_id, page_no), 303)


@app.post("/series/{series_id}/chapter/{number}/unignore")
def chapter_unignore(series_id: int, number: float, page_no: int = Query(1, alias="page")):
    with db.connect() as con:
        db.set_status(con, series_id, number, "wanted")
        stuck.forget(con, series_id, number)          # a skip from a stuck note no longer applies; never automatic
        db.event(con, "ignore", f"chapter {number:g} wanted again", series_id)
    return RedirectResponse(_series_url(series_id, page_no), 303)


# -- stuck behind a chapter (stuck.py): Skip it, Un-skip, Keep waiting ------------

def _stuck_verdict(con, series_id: int, number: float):
    """The verdict on the chapter the series is stuck behind, if it is one
    (kept with a skip, for the series page and the event)."""
    r = db.get_series(con, series_id)
    st = next((b for b in stuck.details(con, r) if b.number == number), None) if r else None
    return (st.verdict, st.waiting) if st else (None, None)


def _skip(series_id: int, number: float) -> tuple[bool, str]:
    with db.connect() as con:
        r = db.get_series(con, series_id)
        if not r:
            raise HTTPException(404, "no such series")
        c = con.execute("SELECT status FROM chapter WHERE series_id=? AND number=?", (series_id, number)).fetchone()
        if not c:
            raise HTTPException(404, "no such chapter")
        v, waiting = _stuck_verdict(con, series_id, number)
        if stuck.skip(con, series_id, number, "manual", v, waiting):
            log.info("%s: ch %g skipped from the series page", r["title"], number)
            return True, (f"chapter {number:g} skipped: the chapters after it are downloaded by the next pass "
                          "(or Search Missing now)")
    return False, f"chapter {number:g} is {c['status']}: only a wanted, failed or unavailable chapter can be skipped"


def _unskip(series_id: int, number: float) -> tuple[bool, str]:
    with db.connect() as con:
        r = db.get_series(con, series_id)
        if not r:
            raise HTTPException(404, "no such series")
        c = con.execute("SELECT status FROM chapter WHERE series_id=? AND number=?", (series_id, number)).fetchone()
        if not c:
            raise HTTPException(404, "no such chapter")
        if stuck.unskip(con, series_id, number):
            log.info("%s: ch %g un-skipped from the series page", r["title"], number)
            return True, f"chapter {number:g} un-skipped: it is wanted again"
    return False, f"chapter {number:g} is {c['status']}, not skipped"


def _keep_waiting(series_id: int, number: float) -> tuple[bool, str]:
    with db.connect() as con:
        if not db.get_series(con, series_id):
            raise HTTPException(404, "no such series")
        if stuck.dismiss(con, series_id, number):
            return True, (f"the series keeps waiting for chapter {number:g}: its note is folded, and it is not "
                          "skipped automatically, until the chapter changes (another title, or another site lists it)")
    return False, f"the series is not stuck behind chapter {number:g}"


def _keep_skipped(series_id: int, number: float) -> tuple[bool, str]:
    with db.connect() as con:
        if not db.get_series(con, series_id):
            raise HTTPException(404, "no such series")
        if stuck.keep_skipped(con, series_id, number):
            return True, f"chapter {number:g} stays skipped as it is listed now"
    return False, f"chapter {number:g} is not a skipped chapter"


@app.post("/series/{series_id}/chapter/{number}/skip")
def chapter_skip(series_id: int, number: float, page_no: int = Query(1, alias="page")):
    return _flash(_series_url(series_id, page_no), _skip(series_id, number)[1])


@app.post("/series/{series_id}/chapter/{number}/unskip")
def chapter_unskip(series_id: int, number: float, page_no: int = Query(1, alias="page")):
    return _flash(_series_url(series_id, page_no), _unskip(series_id, number)[1])


@app.post("/series/{series_id}/chapter/{number}/keep-waiting")
def chapter_keep_waiting(series_id: int, number: float, page_no: int = Query(1, alias="page")):
    return _flash(_series_url(series_id, page_no), _keep_waiting(series_id, number)[1])


@app.post("/series/{series_id}/chapter/{number}/keep-skipped")
def chapter_keep_skipped(series_id: int, number: float, page_no: int = Query(1, alias="page")):
    return _flash(_series_url(series_id, page_no), _keep_skipped(series_id, number)[1])


@app.post("/api/v1/series/{series_id}/chapter/{number}/skip")
def api_chapter_skip(series_id: int, number: float):
    """Skip a chapter (it becomes ignored; the chapters after it go on). 409
    when it is not wanted, failed or unavailable."""
    ok, msg = _skip(series_id, number)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "number": number, "status": "ignored", "message": msg}


@app.post("/api/v1/series/{series_id}/chapter/{number}/unskip")
def api_chapter_unskip(series_id: int, number: float):
    """Want a skipped (ignored) chapter again. 409 when it is not ignored."""
    ok, msg = _unskip(series_id, number)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "number": number, "status": "wanted", "message": msg}


@app.post("/api/v1/series/{series_id}/chapter/{number}/keep-waiting")
def api_chapter_keep_waiting(series_id: int, number: float):
    """Fold the stuck note on this chapter away, and keep the automatic skip
    off it, until it changes. 409 when the series is not stuck behind it."""
    ok, msg = _keep_waiting(series_id, number)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "number": number, "message": msg}


@app.post("/api/v1/series/{series_id}/chapter/{number}/keep-skipped")
def api_chapter_keep_skipped(series_id: int, number: float):
    """A skipped chapter the sites list differently now stays skipped as it
    is listed now (the series page stops offering it back). 409 when it is
    not one skipped from a stuck note."""
    ok, msg = _keep_skipped(series_id, number)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "number": number, "status": "ignored", "message": msg}


@app.post("/series/{series_id}/delete")
def series_delete(series_id: int, files: str = Form("0"), exclude: str = Form("0")):
    with db.connect() as con:
        r = db.get_series(con, series_id)
        if not r:
            raise HTTPException(404)
        if runner.pending_for(series_id):
            return _flash(f"/series/{series_id}", "cannot delete while a job for this series is running")
        if exclude == "1":
            lists_routes.exclude_deleted(con, r)
            con.commit()                   # never hold a write across delete_series' Suwayomi calls
        core.delete_series(con, client, series_id, delete_library=(files == "1"))
    return _flash("/", f"deleted {r['title']}")


def _cross_site(request: Request) -> bool:
    """A GET a browser made for another site's page (an <img>, a link):
    it must not trigger outbound work (lookups at AniList/MangaDex)."""
    return request.headers.get("sec-fetch-site") in ("cross-site", "same-site")


@app.get("/add")
def add_page(request: Request, term: str = ""):
    pick, cands, error = None, [], None
    if term and _cross_site(request):
        log.info("add page: not looking up %s for a request started by another site", security.clip(term))
        error = "search not run: the link came from another site. Press Search to run it."
    elif term:
        try:
            pick, cands = metadata.lookup(term)
        except Exception as e:                        # both providers down
            error = f"lookup failed: {type(e).__name__}: {e}"
            log.error("add page: %s", error)
        if pick and pick.ref not in [c.ref for c in cands]:
            cands.insert(0, pick)
    with db.connect() as con:
        tracked = duplicates.tracked_ids(con, cands)      # under their own reference or the other one
    return page(request, "add.html", term=term, pick=pick, cands=cands, tracked=tracked, error=error,
                library_root=config.LIBRARY_ROOT)


def _series_from_ref(ref: str, title: str = "", aliases: list[str] | None = None) -> model.Series:
    """Build the Series to add; raises ValueError with a user-facing message."""
    if ref == "manual":
        if not title.strip():
            raise ValueError("a title is required")
        return model.manual(title, *(aliases or []))
    if not model.valid_ref(ref):
        raise ValueError(f"not a series reference: {ref!r}")
    try:
        s = metadata.by_ref(ref)
    except metadata.LookupError_ as e:
        raise ValueError(str(e)) from e
    if not s or s.title == "?":
        raise ValueError(f"nothing found for {ref}")
    return s


def _queue_add(series: model.Series, download: bool, monitored: bool = True) -> jobs.Job | str:
    with db.connect() as con:
        why = duplicates.refusal(con, series)
        if why:
            return why
    key = duplicates.add_key(series)          # one add per series, under either reference
    for j in runner.jobs():
        if j.kind == "add" and (j.title == series.title or j.key == key) and j.status in ("queued", "running"):
            return f"already queued as job #{j.id}"
    return runner.submit("add", series.title, _job_add(series, download, monitored), key=key)


@app.post("/add")
def add_submit(ref: str = Form(...), download: str = Form("1"), title: str = Form(""), alias: str = Form(""),
               monitored: str = Form("1")):
    try:
        series = _series_from_ref(ref, title, [a for a in alias.split("|") if a.strip()])
    except ValueError as e:
        return _flash("/add", str(e))
    job = _queue_add(series, download == "1", monitored in ("1", "true", "on"))
    if isinstance(job, str):
        return _flash("/add", f"{series.title}: {job}")
    return _flash("/activity", f"{series.title} queued as job #{job.id}")


# gen counts finished scans: the Import form carries it, so a submit made
# against an older scan (the list changed since) is refused
_adopt_scan: dict = {"items": None, "job": None, "gen": 0}


def _job_adopt_scan(job: jobs.Job):
    items = core.plan_adopt(client, progress=lambda m: setattr(job, "progress", m), should_cancel=lambda: job.cancel)
    with db.connect() as con:
        duplicates.mark_tracked(con, items)
    _adopt_scan["items"] = items
    _adopt_scan["gen"] += 1
    n_ok = sum(1 for i in items if i.series and not i.tracked)
    n_failed = sum(1 for i in items if i.lookup_error)
    return f"{len(items)} folders, {n_ok} identified, {sum(1 for i in items if not i.series)} need a choice" + \
        (f" ({n_failed} not looked up: AniList and MangaDex could not be reached; scan again)" if n_failed else "")


@app.get("/import")
def import_page(request: Request):
    items = _adopt_scan["items"]
    job = _adopt_scan["job"]
    scanning = bool(job and job.status in ("queued", "running"))
    ctx = dict(items=items, scanning=scanning, staging=config.STAGING_ROOT, gen=_adopt_scan["gen"])
    if items is not None:
        ctx.update(n_ok=sum(1 for i in items if i.series and not i.tracked),
                   n_review=sum(1 for i in items if not i.series),
                   n_tracked=sum(1 for i in items if i.tracked))
    return page(request, "import.html", **ctx)


@app.post("/import/scan")
def import_scan():
    job = _adopt_scan["job"]
    if not (job and job.status in ("queued", "running")):
        _adopt_scan["job"] = runner.submit("adopt-scan", "staging folders", _job_adopt_scan)
    return RedirectResponse("/import", 303)


@app.post("/import/apply")
async def import_apply(request: Request):
    """Adopt what the Import form selected. Fields are keyed by each folder's
    stable key (AdoptItem.key), not its position, and the form's scan
    generation must match the current scan: a rescan in between may have
    added or removed folders, and a choice must never land on another one."""
    items = _adopt_scan["items"]
    if items is None:
        return _flash("/import", "scan first")
    form = await request.form()
    if str(form.get("gen", "")) != str(_adopt_scan["gen"]):
        log.warning("import: form from scan %s submitted, current scan is %s; refused", form.get("gen"),
                    _adopt_scan["gen"])
        return _flash("/import", "the folder list changed since this page was loaded; check it and import again")
    picks = {it.key: (form.get(f"adopt_{it.key}"), str(form.get(f"choice_{it.key}", "skip"))) for it in items}

    def choose() -> list:
        """Metadata lookups (AniList/MangaDex) happen here, off the event loop."""
        chosen = []
        for it in items:
            if it.tracked:
                continue
            adopt, choice = picks[it.key]
            if it.series:
                if adopt == "1":
                    chosen.append(it)
                continue
            if choice == "skip":
                continue
            try:
                series = _series_from_ref("manual" if choice == "manual" else choice, it.folder_name)
            except ValueError as e:
                log.error("import: %s for %s: %s", choice, it.folder_name, e)
                continue
            chosen.append(dataclasses.replace(it, series=series))    # a copy: the shared scan result stays as scanned
        return chosen

    from starlette.concurrency import run_in_threadpool
    chosen = await run_in_threadpool(choose)
    if not chosen:
        return _flash("/import", "nothing selected")

    runner.submit("adopt", f"{len(chosen)} folder(s)", _job_adopt(chosen))
    return _flash("/activity", "adopt queued")


def _job_adopt(chosen: list):
    """Register the chosen folders, then link each series' files, saying
    which one it is on; a cancel stops between series (the next refresh of
    the others links theirs)."""
    def run(job: jobs.Job):
        with db.connect() as con:
            ids, n_ch = core.apply_adopt(con, chosen)
        _adopt_scan["items"] = None                # adopted: the scan is out of date, whatever happens next
        linked = 0
        for i, sid in enumerate(ids, 1):
            if job.cancel:
                return (f"adopted {len(ids)} series ({n_ch} chapters), linked {linked} files; cancelled before "
                        f"linking the other {len(ids) - i + 1} (their next refresh links them)")
            job.progress = f"linking the files of series {i} of {len(ids)}"
            with db.connect() as con:
                linked += core.import_series(con, sid, with_cancel(client, lambda: job.cancel))
        return f"adopted {len(ids)} series ({n_ch} chapters), linked {linked} files"
    return run


@app.get("/wanted")
def wanted_page(request: Request, filter: str = "all"):
    with db.connect() as con:
        rows = db.wanted_all(con)
        shown = filter if filter in ("all", "failed", "clean", "stuck") else "all"
        # every stuck series; judged (which reads all their chapters) only for the Stuck filter
        blocked = stuck.overview(con, fetch=not _cross_site(request), judged=shown == "stuck")
    return page(request, "wanted.html", rows=rows, stuck=blocked, filter=shown)


@app.post("/wanted/search")
def wanted_search():
    for j in runner.jobs():
        if j.kind in ("search-wanted", "refresh-all") and j.status in ("queued", "running"):
            return _flash("/activity", f"a {j.kind} job is already {j.status}")
    runner.submit("search-wanted", "every series with wanted chapters", _job_search_wanted, key="search-wanted")
    return _flash("/activity", "search queued")


@app.get("/activity")
def activity_page(request: Request):
    jobs_, squeue = runner.jobs()[:50], _suwayomi_queue()
    passes = [j for j in jobs_ if j.items]
    current = next((j for j in passes if j.status == "running"), passes[0] if passes else None)
    return page(request, "activity.html", jobs=jobs_, squeue=squeue, queue=views.queue_rows(jobs_, squeue),
                pass_job=current)


@app.get("/activity/history")
def history_page(request: Request):
    with db.connect() as con:
        events = db.events(con, 200)
    return page(request, "history.html", events=events)


@app.post("/activity/refresh-all")
def activity_refresh_all():
    if scheduler is None:
        return _flash("/activity", "scheduler not started yet")
    scheduler.trigger()
    return _flash("/activity", "refresh-all queued")


@app.post("/activity/cancel/{job_id}")
def activity_cancel(job_id: int):
    runner.cancel(job_id)
    return RedirectResponse("/activity", 303)


@app.get("/system")
def system_page(request: Request):
    try:
        sources = ui_client.sources()
        suwayomi_ok = True
    except SuwayomiError as e:
        sources, suwayomi_ok = [], False
        log.error("system page: suwayomi unreachable: %s", e)
    backups = backup.listing()
    tasks = [
        {"name": "Refresh all monitored series", "every": f"{settings.get('refresh_hours')} h",
         "next": scheduler.next_at if scheduler else None, "action": "/activity/refresh-all"},
        {"name": "Update check", "every": "24 h", "action": "/system/update-check",
         "next": (updates.status()["checkedAt"] or time.time()) + updates.INTERVAL},
        {"name": "Refresh metadata (AniList/MangaDex, no source search)", "every": "with each refresh",
         "action": "/system/metadata-refresh", "next": None},
        {"name": "Database backup", "every": f"{backup.INTERVAL_HOURS:g} h (keep {backup.KEEP})",
         "action": "/system/backups/create",
         "next": (backups[0]["mtime"] + backup.INTERVAL_HOURS * 3600) if backups else None},
    ]
    return page(request, "system.html", sources=sources, suwayomi_ok=suwayomi_ok, cfg=_config_view(),
                uptime=_ago(STARTED), notify_ok=notify.configured(),
                komga_ok=komga.configured(), metrics_ok=metrics.AVAILABLE, copied=library.COPIED,
                next_refresh=(scheduler.next_at if scheduler else None),
                checks=health.run(ui_client, force=True), tasks=tasks, backups=backups)


@app.get("/system/logs")
def system_logs_page(request: Request, lines: int = 500):
    return page(request, "logs.html", log_lines=_tail_log(max(1, min(lines, 5000))), log_file=config.LOG_FILE)


@app.post("/system/metadata-refresh")
def system_metadata_refresh():
    runner.submit("metadata", "every series", _job_refresh_metadata, key="metadata")
    return _flash("/activity", "metadata refresh queued")


@app.post("/system/update-check")
def system_update_check():
    s = updates.check(force=True)
    if s["error"]:
        return _flash("/system", f"update check failed: {s['error']}")
    return _flash("/system", f"latest release: {s['latest'] or 'none published'} (running {s['current']})")


@app.get("/system/backup")
def system_backup_download(request: Request):
    """Download a fresh copy of the database. A GET must not change anything
    (links, prefetch and cross-site navigations make GETs), so this streams a
    temporary copy that is deleted once sent: no kept backup is written and
    none is pruned. Kept backups are made only by POST. The copy goes out
    only if the request's sign-in holds for the settings in the copy itself:
    one let in while there was no login must not carry away the API key and
    Komga key a login switched on meanwhile brought."""
    from starlette.background import BackgroundTask
    try:
        p, name = backup.snapshot_for_download()
    except backup.FAILURES as e:
        log.error("database download failed: %s", e)
        raise HTTPException(500, f"cannot copy the database: {e}") from e
    try:
        allowed = security.still_authorized(request, backup.stored_settings(p))
    except backup.FAILURES as e:
        _remove_quietly(p)
        log.error("database download failed: cannot read the copy's settings: %s", e)
        raise HTTPException(500, f"cannot read the copy of the database: {e}") from e
    if not allowed:
        _remove_quietly(p)
        return _sign_in_lost(request)
    return FileResponse(p, filename=name, media_type="application/x-sqlite3",
                        background=BackgroundTask(_remove_quietly, p))


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError as e:
        log.warning("cannot remove temporary download %s: %s", path, e)


@app.post("/system/backups/create")
def system_backups_create():
    try:
        p = backup.create("manual")
    except backup.FAILURES as e:
        return _flash("/system", f"backup failed: {e}")
    return _flash("/system", f"backup written: {os.path.basename(p)}")


@app.get("/system/backups/{name}")
def system_backups_download(request: Request, name: str):
    try:
        p = backup.path_of(name)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404, str(e)) from e
    if not security.still_authorized(request, settings.all_values()):
        return _sign_in_lost(request)           # a backup made after a login was switched on holds its keys
    return FileResponse(p, filename=name, media_type="application/x-sqlite3")


@app.post("/system/backups/{name}/delete")
def system_backups_delete(name: str):
    try:
        backup.delete(name)
    except (ValueError, FileNotFoundError) as e:
        return _flash("/system", str(e))
    return _flash("/system", f"deleted {name}")


def _restore_guard() -> str | None:
    if runner.current or any(j.status in ("queued", "running") for j in runner.jobs()):
        return "cannot restore while a job is queued or running (cancel it on the Activity page first)"
    return None


RESTORE_START_WAIT = 10.0     # seconds a restore waits for the job worker before giving up


def _restore_exclusive(title: str, fn) -> str:
    """Run a restore on the job runner's own worker thread and return its
    message. The worker runs one job at a time, so no job can run while the
    database is swapped, and anything submitted meanwhile waits behind the
    restore (checking only before the restore left a window in which a
    scheduled refresh could start and write into the restored database).
    Raises backup.RestoreError when refused."""
    if (why := _restore_guard()):
        raise backup.RestoreError(why)
    if not runner._thread.is_alive():               # no worker (not started): nothing can run alongside
        return fn()
    state = {"phase": "waiting", "result": None, "error": None}
    lock, started, done = threading.Lock(), threading.Event(), threading.Event()

    def run(job: jobs.Job):
        with lock:
            if state["phase"] != "waiting":
                return "skipped: the restore request stopped waiting"
            state["phase"] = "running"
        job.progress = "replacing the database with the backup (a restore cannot be cancelled)"
        started.set()
        try:
            state["result"] = fn()
            return state["result"]
        except Exception as e:
            state["error"] = e
            raise
        finally:
            done.set()

    runner.submit("restore", title, run)
    if not started.wait(RESTORE_START_WAIT):
        with lock:
            if state["phase"] == "waiting":
                state["phase"] = "abandoned"
                log.warning("restore of %s refused: another job started first", title)
                raise backup.RestoreError("another job started first; try again when the Activity queue is empty")
    done.wait()
    if state["error"] is not None:
        raise state["error"]
    return state["result"]


@app.post("/system/backups/{name}/restore")
def system_backups_restore(request: Request, name: str):
    try:
        path = backup.path_of(name)
        msg = _restore_exclusive(name, lambda: backup.restore(path, still_allowed=_still(request)))
    except backup.FAILURES as e:
        return _flash("/system", f"restore refused: {e}")
    return _flash("/system", msg)


# One restore upload at a time: each one holds up to backup.upload_limit() on the data volume while it
# arrives, so parallel (or stalled) uploads could fill the disk that Suwayomi, Komga and the database share.
# uploads.receive cuts off one that stalls or crawls, so nobody can keep the slot for long.
_upload_slot = threading.Lock()


async def _upload_answer(body: uploads.Body, msg: str) -> Response:
    """The flash redirect for an upload, sent once the rest of a small body
    has been read (uploads.Body.drain), so a browser still sending gets the
    message. A bigger one is not read: the connection is closed after the
    answer instead."""
    resp = _flash("/system", msg)
    if not await body.drain():
        resp.headers["Connection"] = "close"
    return resp


@app.post("/system/backups/upload")
async def system_backups_upload(request: Request):
    body = uploads.Body(request)
    if (why := _restore_guard()):
        return await _upload_answer(body, why)
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        return await _upload_answer(body, "choose a .db file to restore")
    if not _upload_slot.acquire(blocking=False):
        log.warning("backup upload from %s refused: another upload is still in progress", security.client_ip(request))
        return await _upload_answer(body, "restore refused: another backup upload is in progress; try again when "
                                          "it has finished")
    spool = None
    try:
        # size and free-space checks, then the file part streams into the backups folder (not /tmp)
        spool = await run_in_threadpool(backup.UploadSpool, max(body.declared, 0))
        name = await uploads.receive(body, spool)
        if not name:
            msg = "choose a .db file to restore"
        else:
            # blocking work (checks, swap) runs off the event loop, on the job worker; the file is moved, not copied
            msg = await run_in_threadpool(_restore_exclusive, f"uploaded {name}",
                                          lambda: backup.restore(spool, still_allowed=_still(request)))
    except uploads.Disconnected:
        log.info("backup upload cancelled: the client went away after %d bytes", spool.size if spool else 0)
        return Response(status_code=400)
    except uploads.UploadError as e:
        log.warning("backup upload refused after %d bytes: %s", spool.size if spool else 0, e)
        msg = f"restore refused: {e}"
    except backup.FAILURES as e:
        msg = f"restore refused: {e}"
    finally:
        if spool is not None:
            spool.discard()                       # a partial or refused upload (restore() moved a used one away)
        _upload_slot.release()
    return await _upload_answer(body, msg)       # the slot is free while the rest of a refused body is read


@app.get("/api/v1/system/backup")
def api_backups():
    return backup.listing()


@app.post("/api/v1/system/backup")
def api_backups_create():
    p = backup.create("api")
    return {"name": os.path.basename(p), "size": os.path.getsize(p)}


@app.post("/system/notify-test")
def system_notify_test():
    res = notify.send_detailed("mang-arr test", "If you can read this, notifications work.", "test", force=True)
    if not res:
        return _flash("/system", "no notification channel configured")
    parts = [f"{notify.CHANNELS[k][0]}: {'sent' if r is True else r}" for k, r in res.items()]
    return _flash("/system", "; ".join(parts))


@app.get("/settings")
def settings_page(request: Request):
    try:
        sources = ui_client.sources()
    except SuwayomiError as e:
        sources = []
        log.error("settings page: suwayomi unreachable: %s", e)
    # Suwayomi's 'max sources in parallel', next to Download Lanes; not asked when it just failed to answer
    cap = ui_client.max_sources_in_parallel() if sources else None
    with db.connect() as con:
        stats = db.source_stats(con)
    komga_ok, _, komga_test = (views.flash_from(request.query_params, "komga_test") or "").partition(":")
    values = settings.all_values()
    return page(request, "settings.html", v=settings.masked(values), bad_urls=settings.invalid_urls(values),
                sources=sources, komga_test=komga_test, komga_ok=(komga_ok == "1"), stats=stats,
                auto_days=db.AUTO_THROTTLE_DAYS, suwayomi_cap=cap, lanes=int(limits.setting("download_lanes")))


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()                    # async (bounded by security.BodyLimit); the rest blocks: in the pool
    return await run_in_threadpool(_settings_save, request, form)


def _settings_save(request: Request, form) -> Response:
    values = {}
    for key in settings.DEFAULTS:
        if key in ("unusable_sources", "throttled_sources", "page_warm_sources", "api_key") \
                or key in settings.INTERNAL_KEYS:
            continue                           # handled below / detected / Regenerate button / never from a form
        if isinstance(settings.DEFAULTS[key], list):
            if key in form:
                values[key] = ",".join(str(x) for x in form.getlist(key))
        elif key in form:
            values[key] = form[key]
    if form.get("sources_listed") == "1":              # the Sources table was on the page
        enabled = {str(x).lower().strip() for x in form.getlist("enabled_sources")}
        listed = {str(x).lower().strip() for x in form.getlist("listed_sources")}
        values["unusable_sources"] = sorted(listed - enabled)
        # "one by one": the ticks for the sources on the page; entries for sources not installed now are kept
        warm = {str(x).lower().strip() for x in form.getlist("warm_sources")}
        stored = set(settings.all_values()["page_warm_sources"])
        values["page_warm_sources"] = sorted((stored - listed) | (warm & listed))
    epoch = settings.all_values()["session_epoch"]
    try:
        with db.connect() as con:
            notices = settings.set_many(con, values, still_allowed=_still(request))
    except settings.NotAllowed:
        return _sign_in_lost(request)
    except (ValueError, KeyError) as e:
        log.warning("settings not saved: %s", e)
        return _flash("/settings", f"invalid value, nothing saved: {e}")
    except sqlite3.OperationalError as e:
        log.error("settings not saved: %s", e)
        return _flash("/settings", f"could not save (database busy?): {e}")
    action = str(form.get("action", ""))
    resp = _settings_action(action, "; ".join(notices))
    if settings.all_values()["session_epoch"] != epoch:
        _reissue_session(request, resp)                # changed the password: keep this browser signed in
    return resp


def _settings_action(action: str, notice: str) -> Response:
    extra = f" ({notice})" if notice else ""
    if action == "test-komga":
        ok, msg = komga.test()
        return RedirectResponse(f"/settings?{views.flash_query(f'{int(ok)}:{msg}{extra}', 'komga_test')}", 303)
    if action == "test-notify" or action.startswith("test-notify-"):
        only = action[len("test-notify-"):] if action.startswith("test-notify-") else None
        res = notify.send_detailed("mang-arr test", "If you can read this, notifications work.", "test", only=only,
                                   force=True)
        if not res:
            return _flash("/settings", "nothing to test: fill in that channel first" + extra)
        parts = [f"{notify.CHANNELS[k][0]}: {'sent' if r is True else r}" for k, r in res.items()]
        return _flash("/settings", "; ".join(parts) + extra)
    if action == "suwayomi-parallel":
        # the one place mang-arr changes Suwayomi's own 'max sources in parallel': a button the user presses
        n = int(limits.setting("download_lanes"))
        try:
            m = ui_client.set_max_sources_in_parallel(n)
        except (SuwayomiError, KeyError, TypeError, ValueError) as e:
            why = str(e) if isinstance(e, SuwayomiError) else f"unexpected answer ({type(e).__name__})"
            log.warning("could not set Suwayomi's max sources in parallel to %d: %s", n, why)
            return _flash("/settings", f"could not change Suwayomi's setting: {why}{extra}")
        log.info("Suwayomi's max sources in parallel set to %d from the Settings page", m)
        return _flash("/settings", f"Suwayomi now downloads from up to {m} sources at once{extra}")
    return _flash("/settings", "saved" + extra)


@app.get("/metrics")
def metrics_endpoint():
    with db.connect() as con:
        body, ctype = metrics.render(con, _suwayomi_up())
    return Response(body, media_type=ctype)


@app.get("/api/v1/health")
def api_health(request: Request):
    """Open for monitoring. Served from the health cache (at most one
    background re-check a minute, whoever asks), so anonymous callers cannot
    make mang-arr hammer its backends. With a login set, anonymous callers
    get only the names of failing checks, not their details (internal URLs,
    paths, error text)."""
    checks = health.run(ui_client)
    detail = getattr(request.state, "authed", False)
    problems = [f"{c.name}: {c.detail}" if detail else c.name for c in checks if c.level == "error"]
    warnings = [f"{c.name}: {c.detail}" if detail else c.name for c in checks if c.level == "warning"]
    status = 200 if not problems else 503
    return JSONResponse({"ok": not problems, "problems": problems, "warnings": warnings,
                         "version": __version__}, status_code=status)


# -- api ----------------------------------------------------------------------

class AddBody(BaseModel):
    ref: str = Field(description="anilist:ID, mangadex:UUID, manual:Title, or 'manual' with title")
    title: str = ""
    aliases: list[str] = []
    download: bool = True
    monitored: bool = True


@app.get("/api/v1/system/status")
def api_status(request: Request):
    h = health.summary(ui_client)
    if not getattr(request.state, "authed", False):    # login set, anonymous caller: no titles, no job details
        cur = runner.current
        return {"version": __version__, "uptime": int(time.time() - STARTED),
                "job": {"kind": cur.kind, "status": cur.status} if cur else None,
                "health": {"errors": h["errors"], "warnings": h["warnings"]}}
    return {"version": __version__, "uptime": int(time.time() - STARTED),
            "job": runner.current.as_dict() if runner.current else None,
            "nextRefresh": scheduler.next_at if scheduler else None, "update": updates.status(),
            "health": {"errors": h["errors"], "warnings": h["warnings"]}}


@app.get("/api/v1/series")
def api_series():
    with db.connect() as con:
        return [dict(r) for r in db.series_rows(con)]


API_CHAPTERS = 5000        # chapters per GET /api/v1/series/{id} (default and maximum); real series fit in one


@app.get("/api/v1/series/{series_id}")
def api_series_one(request: Request, series_id: int, limit: int = API_CHAPTERS, offset: int = 0):
    """One series with its sources and chapters (by number). The chapters
    come in pages of at most API_CHAPTERS; chapterTotal, limit and offset
    say where this page is, so a client pages on with offset=offset+limit.
    An offset past the end is the end (an empty page): any integer the
    caller sends stays within what SQLite can bind. `stuck` and `skipped`:
    the chapters it is stuck behind with their verdicts (stuck.Stuck), and
    the ones skipped from such a note."""
    limit = max(1, min(limit, API_CHAPTERS))
    with db.connect() as con:
        r = db.get_series(con, series_id)
        if not r:
            raise HTTPException(404)
        total = db.chapter_count(con, series_id)
        offset = min(max(0, offset), total)
        return {"series": dict(r), "sources": [dict(s) for s in db.sources(con, series_id)],
                "chapters": [dict(c) for c in db.chapters(con, series_id, limit=limit, offset=offset)],
                "chapterTotal": total, "limit": limit, "offset": offset,
                "stuck": [st.as_dict() for st in stuck.details(con, r, fetch=not _cross_site(request))],
                "skipped": stuck.skipped(con, series_id)}


@app.post("/api/v1/series")
def api_series_add(body: AddBody):
    try:
        series = _series_from_ref(body.ref, body.title, body.aliases)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    job = _queue_add(series, body.download, body.monitored)
    if isinstance(job, str):
        raise HTTPException(409, f"{series.title}: {job}")
    return job.as_dict()


@app.post("/api/v1/series/{series_id}/refresh")
def api_series_refresh(series_id: int, download: bool = True):
    with db.connect() as con:
        r = db.get_series(con, series_id)
    if not r:
        raise HTTPException(404)
    if runner.pending_for(series_id):
        raise HTTPException(409, "a job for this series is already queued")
    return runner.submit("refresh", r["title"], _job_refresh(series_id, download), series_id).as_dict()


@app.delete("/api/v1/series/{series_id}")
def api_series_delete(series_id: int, files: bool = False):
    with db.connect() as con:
        if not db.get_series(con, series_id):
            raise HTTPException(404)
        if runner.pending_for(series_id):
            raise HTTPException(409, "a job for this series is running")
        core.delete_series(con, client, series_id, delete_library=files)
    return {"ok": True}


@app.get("/api/v1/lookup")
def api_lookup(request: Request, term: str):
    if _cross_site(request):
        log.info("lookup of %s refused: request started by another site", security.clip(term))
        raise HTTPException(403, "cross-site lookup refused")
    try:
        pick, cands = metadata.lookup(term)
    except Exception as e:
        raise HTTPException(502, f"lookup failed: {type(e).__name__}: {e}") from e
    return {"pick": pick.ref if pick else None,
            "candidates": [{"ref": c.ref, "title": c.title, "titles": c.titles, "format": c.format,
                            "country": c.country, "status": c.status, "chapters": c.chapters,
                            "cover": c.cover} for c in cands]}


@app.get("/api/v1/wanted")
def api_wanted():
    with db.connect() as con:
        return [dict(r) for r in db.wanted_all(con)]


@app.get("/api/v1/queue")
def api_queue():
    return {"jobs": [j.as_dict() for j in runner.jobs()[:50]], "suwayomi": _suwayomi_queue()}


@app.post("/api/v1/command")
def api_command(body: dict):
    name = body.get("name")
    if name == "RefreshAll":
        if scheduler is None:
            raise HTTPException(503, "scheduler not started")
        return scheduler.trigger().as_dict()
    if name == "SearchWanted":
        j = runner.active("search-wanted", "refresh-all")
        return (j or runner.submit("search-wanted", "every series with wanted chapters", _job_search_wanted,
                                   key="search-wanted")).as_dict()
    if name == "RefreshMetadata":
        return runner.submit("metadata", "every series", _job_refresh_metadata, key="metadata").as_dict()
    raise HTTPException(400, f"unknown command {name!r}; known: RefreshAll, SearchWanted, RefreshMetadata")


def _settings_out(v: dict) -> dict:
    """Settings with secrets masked except the API key itself, so scripts and
    the installer can read it: only ever for values the caller's sign-in was
    checked against (security.still_authorized), so a request let in while
    there was no login never sees the key a login switched on since brought."""
    out = settings.masked(v)
    out["api_key"] = v["api_key"]
    return out


@app.get("/api/v1/settings")
def api_settings_get(request: Request):
    """Runtime settings with secrets masked (see _settings_out for api_key)."""
    v = settings.all_values()
    if not security.still_authorized(request, v):
        return _sign_in_lost(request)
    return _settings_out(v)


@app.put("/api/v1/settings")
def api_settings_put(request: Request, body: dict):
    """Set runtime settings: {key: value}. Lists take arrays or comma-separated
    strings; a secret given as the mask keeps its value. Unknown keys -> 400.
    A secret cleared because its destination changed is named in the
    X-Mangarr-Notice header (and the log), and so is a new API key when this
    call switched the login on: the response then carries the new key (the
    values as this call left them), and the key the call was made with
    (readable while there was no login) no longer works. A call whose sign-in
    no longer holds when it is stored (a login switched on, or the key
    changed, while it waited) gets 401 and stores nothing."""
    try:
        with db.connect() as con:
            notices, after = settings.write(con, body, still_allowed=_still(request))
    except settings.NotAllowed:
        return _sign_in_lost(request)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"invalid setting: {e}") from e
    headers = {"X-Mangarr-Notice": "; ".join(notices)} if notices else None
    return JSONResponse(_settings_out(after), headers=headers)


@app.get("/api/v1/log")
def api_log(lines: int = 200):
    return JSONResponse({"lines": _tail_log(max(1, min(lines, 5000)))})


# -- helpers ------------------------------------------------------------------

def _suwayomi_up() -> bool:
    try:
        client.gq("{ sources { totalCount } }", timeout=5, retries=1)
        return True
    except SuwayomiError:
        return False


def _suwayomi_queue() -> dict:
    try:
        d = ui_client.gq("{ downloadStatus { state queue { state progress tries"
                      " manga { title } chapter { name } } } }",
                      timeout=20, retries=1)["downloadStatus"]
        return {"state": d["state"], "items": [
            {"manga": x["manga"]["title"], "chapter": x["chapter"]["name"], "state": x["state"],
             "progress": round((x["progress"] or 0) * 100), "tries": x["tries"]} for x in d["queue"][:20]],
            "count": len(d["queue"])}
    except SuwayomiError as e:
        return {"state": "UNREACHABLE", "items": [], "count": 0, "error": str(e)}


def _config_view() -> list[tuple[str, str]]:
    keep = ("SUWAYOMI_URL", "DATA_DIR", "DB_PATH", "STAGING_ROOT", "LIBRARY_ROOT", "REFRESH_HOURS",
            "LOG_LEVEL", "LOG_FILE")
    return [(k, str(getattr(config, k))) for k in keep]


def _tail_log(n: int) -> list[str]:
    path = config.LOG_FILE
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return list(deque(f, maxlen=n))
    except OSError as e:
        return [f"cannot read {path}: {e}"]
