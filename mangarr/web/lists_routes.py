"""Import Lists: the /lists page, its JSON API, and the thread that syncs
lists on their schedule. Sync runs as a "list-sync" job on the app's runner;
every new series becomes an ordinary add job.

app.py registers `router` and calls init() from its lifespan; init() takes
the app module (defaults to mangarr.web.app, imported lazily so this module
can be imported by app.py at load time) for its runner, page() and add-job
helpers.
"""
import logging
import threading
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import db, jobs, lists, model
from . import views

log = logging.getLogger(__name__)
router = APIRouter()
_app = None                  # the app module, set by init()
CHECK_EVERY = 600            # seconds between "is a list due?" checks
FIRST_CHECK = 120            # let the app come up first
MIN_SYNC_HOURS = 1.0         # a list is synced at most this often (each sync can add and download series)


def init(app_module=None) -> None:
    global _app
    if app_module is None:
        from . import app as app_module
    _app = app_module
    threading.Thread(target=_loop, name="mangarr-lists", daemon=True).start()
    log.info("import list scheduler started: checks every %d min", CHECK_EVERY // 60)


def exclude_deleted(con, series_row) -> None:
    """Called by the series delete form: keep this series off every list."""
    lists.add_exclusion(con, series_row["ref"], series_row["title"], "deleted by user")


# -- jobs ---------------------------------------------------------------------

def _submit_add(series: model.Series, download: bool, monitored: bool, list_name: str) -> jobs.Job | str:
    """Queue the add job for one series a list yielded. Same checks as the
    Add page; an unmonitored list unmonitors the series once it exists."""
    with db.connect() as con:
        if db.get_series_by_ref(con, series.ref):
            return "already tracked"
    for j in _app.runner.jobs():
        if j.kind == "add" and j.title == series.title and j.status in ("queued", "running"):
            return f"already queued as job #{j.id}"
    inner = _app._job_add(series, download)

    def run(job: jobs.Job):
        out = inner(job)
        if job.series_id:
            with db.connect() as con:
                if not monitored:
                    db.set_monitored(con, job.series_id, False)
                db.event(con, "added", f"added by import list {list_name}", job.series_id)
        return out
    return _app.runner.submit("add", series.title, run)


def _job_sync(list_id: int):
    def run(job: jobs.Job):
        with db.connect() as con:
            row = lists.get_list(con, list_id)
            if not row:
                return "list no longer exists"
            job.title = row["name"]

            def submit(series, download, monitored):
                r = _submit_add(series, download, monitored, row["name"])
                if isinstance(r, str):
                    log.debug("list %s: %s: %s", row["name"], series.title, r)
            return lists.sync(con, row, submit, should_cancel=lambda: job.cancel)
    return run


def _queue_sync(row) -> jobs.Job | str:
    for j in _app.runner.jobs():
        if j.kind == "list-sync" and j.title == row["name"] and j.status in ("queued", "running"):
            return f"already {j.status} as job #{j.id}"
    return _app.runner.submit("list-sync", row["name"], _job_sync(row["id"]))


def _sync_due() -> None:
    with db.connect() as con:
        rows = lists.all_lists(con)
    for r in rows:
        try:
            if lists.is_due(r):
                log.info("list %s is due (last sync %s)", r["name"], r["last_sync"] or "never")
                _queue_sync(r)
        except Exception as e:
            log.error("list %s: could not queue sync: %s: %s", r["name"], type(e).__name__, e)
            try:
                with db.connect() as con:
                    lists.mark_synced(con, r["id"], f"error: {type(e).__name__}: {e}"[:300])
            except Exception as e2:
                log.error("list %s: could not record the error either: %s", r["name"], e2)


def _loop() -> None:
    time.sleep(FIRST_CHECK)
    while True:
        try:
            _sync_due()
        except Exception as e:                     # DB unavailable etc.; try again next tick
            log.error("import list scheduler: %s: %s", type(e).__name__, e)
        time.sleep(CHECK_EVERY)


# -- page ---------------------------------------------------------------------

def _flash(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/lists?{views.flash_query(msg)}", 303)


def _sync_hours(value) -> float:
    """The sync interval, raised to MIN_SYNC_HOURS (with a log line) when shorter."""
    hours = float(value or 24)
    if 0 < hours < MIN_SYNC_HOURS:
        log.warning("import list sync interval %g h raised to the minimum of %g h", hours, MIN_SYNC_HOURS)
        return MIN_SYNC_HOURS
    return hours


def _busy() -> set[str]:
    return {j.title for j in _app.runner.jobs() if j.kind == "list-sync" and j.status in ("queued", "running")}


@router.get("/lists")
def lists_page(request: Request):
    with db.connect() as con:
        rows = [lists.row_dict(r) for r in lists.all_lists(con)]
        excl = lists.exclusions(con)
    return _app.page(request, "lists.html", rows=rows, exclusions=excl, kinds=lists.KINDS,
                     statuses=lists.USER_STATUSES, sorts=lists.TOP_SORTS, countries=lists.COUNTRIES,
                     busy=_busy(), cap=lists.MAX_ADDS)


@router.post("/lists/add")
async def lists_add(request: Request):
    form = await request.form()
    return await run_in_threadpool(_lists_add, form)       # SQLite work off the event loop


def _lists_add(form) -> RedirectResponse:
    kind = str(form.get("kind", ""))
    raw = {k: form.get(k) for k in ("username", "sort", "limit", "country", "min_chapters", "url")}
    raw["statuses"] = form.getlist("statuses")
    try:
        params = lists.validate_params(kind, raw)
        sync_hours = _sync_hours(form.get("sync_hours"))
        with db.connect() as con:
            list_id = lists.add_list(con, str(form.get("name", "")), kind, params, download=form.get("download") == "1",
                                     monitored=form.get("monitored") == "1", sync_hours=sync_hours)
            row = lists.get_list(con, list_id)
    except ValueError as e:
        return _flash(str(e))
    if form.get("sync_now") == "1":
        _queue_sync(row)
        return _flash(f"list added and sync queued: {row['name']}")
    return _flash(f"list added: {row['name']}")


@router.post("/lists/{list_id}/sync")
def lists_sync(list_id: int):
    with db.connect() as con:
        row = lists.get_list(con, list_id)
    if not row:
        raise HTTPException(404, "no such list")
    r = _queue_sync(row)
    return _flash(f"{row['name']}: {r}" if isinstance(r, str) else f"{row['name']}: sync queued as job #{r.id}")


@router.post("/lists/{list_id}/toggle")
def lists_toggle(list_id: int):
    with db.connect() as con:
        row = lists.get_list(con, list_id)
        if not row:
            raise HTTPException(404, "no such list")
        lists.set_enabled(con, list_id, not row["enabled"])
    return _flash(f"{row['name']}: {'disabled' if row['enabled'] else 'enabled'}")


@router.post("/lists/{list_id}/delete")
def lists_delete(list_id: int):
    with db.connect() as con:
        row = lists.get_list(con, list_id)
        if not row:
            raise HTTPException(404, "no such list")
        lists.delete_list(con, list_id)
    log.info("import list deleted: %s", row["name"])
    return _flash(f"deleted list {row['name']}")


@router.post("/lists/exclusions/add")
def exclusions_add(ref: str = Form(...), title: str = Form(""), reason: str = Form("")):
    try:
        with db.connect() as con:
            lists.add_exclusion(con, ref, title, reason or "added by hand")
    except ValueError as e:
        return _flash(str(e))
    return _flash(f"excluded {ref.strip()}")


@router.post("/lists/exclusions/{ref:path}/delete")
def exclusions_delete(ref: str):
    with db.connect() as con:
        lists.remove_exclusion(con, ref)
    return _flash(f"exclusion removed: {ref}")


# -- api ----------------------------------------------------------------------

class ListBody(BaseModel):
    name: str = ""
    kind: str
    params: dict = {}
    enabled: bool = True
    download: bool = True
    monitored: bool = True
    syncHours: float = 24
    syncNow: bool = False


class ExclusionBody(BaseModel):
    ref: str
    title: str = ""
    reason: str = ""


@router.get("/api/v1/importlist")
def api_lists():
    with db.connect() as con:
        return [lists.row_dict(r) for r in lists.all_lists(con)]


@router.post("/api/v1/importlist")
def api_lists_add(body: ListBody):
    try:
        params = lists.validate_params(body.kind, body.params)
        with db.connect() as con:
            list_id = lists.add_list(con, body.name, body.kind, params, enabled=body.enabled,
                                     download=body.download, monitored=body.monitored,
                                     sync_hours=_sync_hours(body.syncHours))
            row = lists.get_list(con, list_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if body.syncNow:
        _queue_sync(row)
    return lists.row_dict(row)


@router.post("/api/v1/importlist/{list_id}/sync")
def api_lists_sync(list_id: int):
    with db.connect() as con:
        row = lists.get_list(con, list_id)
    if not row:
        raise HTTPException(404, "no such list")
    r = _queue_sync(row)
    if isinstance(r, str):
        raise HTTPException(409, f"{row['name']}: {r}")
    return r.as_dict()


@router.delete("/api/v1/importlist/{list_id}")
def api_lists_delete(list_id: int):
    with db.connect() as con:
        if not lists.get_list(con, list_id):
            raise HTTPException(404, "no such list")
        lists.delete_list(con, list_id)
    return {"ok": True}


@router.get("/api/v1/importlistexclusion")
def api_exclusions():
    with db.connect() as con:
        return [dict(r) for r in lists.exclusions(con)]


@router.post("/api/v1/importlistexclusion")
def api_exclusions_add(body: ExclusionBody):
    try:
        with db.connect() as con:
            lists.add_exclusion(con, body.ref, body.title, body.reason or "added via API")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "ref": body.ref.strip()}


@router.delete("/api/v1/importlistexclusion")
def api_exclusions_delete(ref: str):
    with db.connect() as con:
        lists.remove_exclusion(con, ref)
    return {"ok": True}
