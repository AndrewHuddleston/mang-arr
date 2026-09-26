"""The web UI and JSON API (FastAPI). Long operations go through the job
runner; requests only read the database and submit jobs.

Pages:   /  /series/{id}  /add  /wanted  /activity  /system
API:     /api/v1/...  (mirrors the pages; used by the pages' live updates)
"""
import base64
import logging
import os
import secrets
import time
from collections import deque

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, config, core, db, jobs, komga, metadata, metrics, model, notify, settings
from ..resolver import ranges
from ..suwayomi import Client, SuwayomiError

log = logging.getLogger(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="mang-arr", version=__version__, docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.filters["ranges"] = ranges
templates.env.filters["ago"] = lambda ts: _ago(ts)

runner = jobs.Runner()
scheduler: jobs.Scheduler | None = None
client = Client()
STARTED = time.time()


def _ago(ts) -> str:
    if not ts:
        return "-"
    if isinstance(ts, str):
        try:
            ts = time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return ts
    d = int(time.time() - ts)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= size:
            return f"{d // size}{unit} ago"
    return f"{d}s ago"


# -- jobs ---------------------------------------------------------------------

def _job_refresh(series_id: int, download: bool):
    def run(job: jobs.Job):
        with db.connect() as con:
            o = core.refresh_series(con, client, series_id, download=download)
        return f"{len(o.plan.chapters)} listed, {o.downloaded} downloaded, {o.failed} failed, {o.imported} imported"
    return run


def _job_add(series: model.Series, download: bool):
    def run(job: jobs.Job):
        with db.connect() as con:
            o = core.add_series(con, client, series, download=download)
        job.series_id = o.series_id
        return f"{len(o.plan.chapters)} listed, {o.downloaded} downloaded, {o.imported} imported"
    return run


def _job_refresh_all(job: jobs.Job):
    with db.connect() as con:
        rows = [r for r in db.series_rows(con) if r["monitored"]]
    done = downloaded = imported = errors = 0
    for i, r in enumerate(rows, 1):
        if job.cancel:
            job.progress = f"cancelled after {done} of {len(rows)}"
            break
        job.progress = f"{i}/{len(rows)}: {r['title']}"
        try:
            with db.connect() as con:
                o = core.refresh_series(con, client, r["id"], download=True)
            downloaded += o.downloaded
            imported += o.imported
        except Exception as e:
            errors += 1
            log.error("refresh-all: %s: %s: %s", r["title"], type(e).__name__, e)
            with db.connect() as con:
                con.execute("UPDATE series SET last_error=? WHERE id=?", (f"{type(e).__name__}: {e}"[:300], r["id"]))
        done += 1
    msg = f"{done} series, {downloaded} downloaded, {imported} imported, {errors} errors"
    if imported:
        notify.send("mang-arr: new chapters", msg, "new")
    return msg


def start_background() -> None:
    global scheduler
    runner.start()
    scheduler = jobs.Scheduler(runner, _job_refresh_all)
    scheduler.start()


@app.on_event("startup")
def _startup():
    if not runner._thread.is_alive():
        start_background()
    log.info("mang-arr %s web started", __version__)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional HTTP basic auth (Settings -> Security). /api/v1/health and
    /metrics stay open so monitoring works."""
    v = settings.all_values()
    user, password = v["auth_user"], v["auth_password"]
    if user and request.url.path not in ("/api/v1/health", "/metrics"):
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                given = base64.b64decode(header[6:]).decode("utf-8", "replace")
                u, _, p = given.partition(":")
                ok = secrets.compare_digest(u, user) and secrets.compare_digest(p, password)
            except Exception:
                ok = False
        if not ok:
            log.warning("unauthorised request to %s from %s", request.url.path,
                        request.client.host if request.client else "?")
            return Response("authentication required", 401, headers={"WWW-Authenticate": 'Basic realm="mang-arr"'})
    return await call_next(request)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
    return Response(f"mang-arr: {type(exc).__name__}: {exc}\n(see the log on the System page)",
                    500, media_type="text/plain")


# -- pages --------------------------------------------------------------------

def page(request: Request, name: str, **ctx):
    ctx.update(request=request, version=__version__, current=runner.current, flash=request.query_params.get("m"))
    return templates.TemplateResponse(request, name, ctx)


@app.get("/")
def index(request: Request, q: str = ""):
    with db.connect() as con:
        rows = db.series_rows(con)
    if q:
        rows = [r for r in rows if q.lower() in r["title"].lower()]
    return page(request, "index.html", rows=rows, q=q)


@app.get("/series/{series_id}")
def series_page(request: Request, series_id: int):
    with db.connect() as con:
        r = db.get_series(con, series_id)
        if not r:
            raise HTTPException(404, "no such series")
        srcs = db.sources(con, series_id)
        chs = db.chapters(con, series_id)
        events = con.execute("SELECT * FROM event WHERE series_id=? ORDER BY id DESC LIMIT 15", (series_id,)).fetchall()
    by: dict[str, list] = {}
    for c in chs:
        by.setdefault(c["status"], []).append(c["number"])
    return page(request, "series.html", s=r, series=db.series_to_model(r), sources=srcs, chapters=chs,
                by=by, events=events, busy=runner.pending_for(series_id))


@app.post("/series/{series_id}/refresh")
def series_refresh(series_id: int, download: str = Form("1")):
    with db.connect() as con:
        r = db.get_series(con, series_id)
    if not r:
        raise HTTPException(404)
    runner.submit("refresh", r["title"], _job_refresh(series_id, download == "1"), series_id)
    return RedirectResponse(f"/series/{series_id}?m=refresh+queued", 303)


@app.post("/series/{series_id}/monitor")
def series_monitor(series_id: int, monitored: str = Form("1")):
    with db.connect() as con:
        db.set_monitored(con, series_id, monitored == "1")
    return RedirectResponse(f"/series/{series_id}", 303)


@app.post("/series/{series_id}/delete")
def series_delete(series_id: int, files: str = Form("0")):
    with db.connect() as con:
        r = db.get_series(con, series_id)
        if not r:
            raise HTTPException(404)
        core.delete_series(con, client, series_id, delete_library=(files == "1"))
    return RedirectResponse(f"/?m=deleted+{r['title']}", 303)


@app.get("/add")
def add_page(request: Request, term: str = ""):
    pick, cands = (None, [])
    if term:
        pick, cands = metadata.lookup(term)
        if pick and pick.ref not in [c.ref for c in cands]:
            cands.insert(0, pick)
    tracked = set()
    with db.connect() as con:
        tracked = {r["ref"] for r in con.execute("SELECT ref FROM series")}
    return page(request, "add.html", term=term, pick=pick, cands=cands, tracked=tracked)


@app.post("/add")
def add_submit(ref: str = Form(...), download: str = Form("1"), title: str = Form(""), alias: str = Form("")):
    if ref == "manual":
        if not title.strip():
            return RedirectResponse("/add?m=title+required", 303)
        series = model.manual(title, *[a for a in alias.split("|") if a.strip()])
    else:
        series = metadata.by_ref(ref)
        if not series:
            return RedirectResponse(f"/add?m=unknown+{ref}", 303)
    job = runner.submit("add", series.title, _job_add(series, download == "1"))
    return RedirectResponse(f"/activity?m=add+queued+as+job+{job.id}", 303)


_adopt_scan: dict = {"items": None, "job": None}


def _job_adopt_scan(job: jobs.Job):
    items = core.plan_adopt(client)
    with db.connect() as con:
        tracked = {r["ref"] for r in con.execute("SELECT ref FROM series")}
    for it in items:
        it.tracked = bool(it.series and it.series.ref in tracked)
    _adopt_scan["items"] = items
    n_ok = sum(1 for i in items if i.series and not i.tracked)
    return f"{len(items)} folders, {n_ok} identified, {sum(1 for i in items if not i.series)} need a choice"


@app.get("/import")
def import_page(request: Request):
    items = _adopt_scan["items"]
    job = _adopt_scan["job"]
    scanning = bool(job and job.status in ("queued", "running"))
    ctx = dict(items=items, scanning=scanning, staging=config.STAGING_ROOT)
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
    items = _adopt_scan["items"]
    if items is None:
        return RedirectResponse("/import?m=scan+first", 303)
    form = await request.form()
    chosen = []
    for i, it in enumerate(items):
        if it.tracked:
            continue
        if it.series:
            if form.get(f"adopt_{i}") == "1":
                chosen.append(it)
            continue
        choice = form.get(f"choice_{i}", "skip")
        if choice == "skip":
            continue
        if choice == "manual":
            it.series = model.manual(it.folder_name)
        else:
            try:
                it.series = metadata.by_ref(choice)
            except Exception as e:
                log.error("import: could not load %s for %s: %s", choice, it.folder_name, e)
                continue
        if it.series:
            chosen.append(it)
    if not chosen:
        return RedirectResponse("/import?m=nothing+selected", 303)

    def run(job: jobs.Job):
        with db.connect() as con:
            n_series, n_ch = core.apply_adopt(con, chosen)
            ids = [r["id"] for r in con.execute("SELECT id FROM series ORDER BY id DESC LIMIT ?", (n_series,))]
        linked = 0
        for sid in ids:
            with db.connect() as con:
                linked += core.import_series(con, sid)
        _adopt_scan["items"] = None
        return f"adopted {n_series} series ({n_ch} chapters), linked {linked} files"

    runner.submit("adopt", f"{len(chosen)} folder(s)", run)
    return RedirectResponse("/activity?m=adopt+queued", 303)


@app.get("/wanted")
def wanted_page(request: Request):
    with db.connect() as con:
        rows = db.wanted_all(con)
    return page(request, "wanted.html", rows=rows)


@app.get("/activity")
def activity_page(request: Request):
    with db.connect() as con:
        events = db.events(con, 40)
    return page(request, "activity.html", jobs=runner.jobs()[:50], events=events, squeue=_suwayomi_queue())


@app.post("/activity/refresh-all")
def activity_refresh_all():
    scheduler.trigger()
    return RedirectResponse("/activity?m=refresh-all+queued", 303)


@app.post("/activity/cancel/{job_id}")
def activity_cancel(job_id: int):
    runner.cancel(job_id)
    return RedirectResponse("/activity", 303)


@app.get("/system")
def system_page(request: Request):
    try:
        sources = client.sources()
        suwayomi_ok = True
    except SuwayomiError as e:
        sources, suwayomi_ok = [], False
        log.error("system page: suwayomi unreachable: %s", e)
    return page(request, "system.html", sources=sources, suwayomi_ok=suwayomi_ok, cfg=_config_view(),
                log_lines=_tail_log(200), uptime=_ago(STARTED), notify_ok=notify.configured(),
                komga_ok=komga.configured(), metrics_ok=metrics.AVAILABLE,
                next_refresh=(scheduler.next_at if scheduler else None))


@app.post("/system/notify-test")
def system_notify_test():
    ok = notify.send("mang-arr", "test notification", "test")
    return RedirectResponse("/system?m=" + ("notification+sent" if ok else "notification+failed+(see+log)"), 303)


@app.get("/settings")
def settings_page(request: Request, komga_test: str = "", komga_ok: str = ""):
    try:
        sources = client.sources()
    except SuwayomiError as e:
        sources = []
        log.error("settings page: suwayomi unreachable: %s", e)
    return page(request, "settings.html", v=settings.all_values(), sources=sources,
                komga_test=komga_test, komga_ok=(komga_ok == "1"))


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    values = {}
    for key in settings.DEFAULTS:
        if isinstance(settings.DEFAULTS[key], list):
            values[key] = form.getlist(key)
        elif key in form:
            values[key] = form[key]
    try:
        with db.connect() as con:
            settings.set_many(con, values)
    except (ValueError, KeyError) as e:
        return RedirectResponse(f"/settings?m=invalid+value:+{e}", 303)
    action = form.get("action", "")
    if action == "test-komga":
        ok, msg = komga.test()
        return RedirectResponse(f"/settings?komga_test={msg}&komga_ok={int(ok)}", 303)
    if action == "test-notify":
        ok = notify.send("mang-arr", "test notification", "test")
        return RedirectResponse("/settings?m=" + ("notification+sent" if ok else "notification+failed+(see+log)"), 303)
    return RedirectResponse("/settings?m=saved", 303)


@app.get("/metrics")
def metrics_endpoint():
    try:
        client.gq("{ sources { totalCount } }", timeout=5, retries=1)
        up = True
    except SuwayomiError:
        up = False
    with db.connect() as con:
        body, ctype = metrics.render(con, up)
    return Response(body, media_type=ctype)


@app.get("/api/v1/health")
def api_health():
    problems = []
    try:
        client.gq("{ sources { totalCount } }", timeout=5, retries=1)
    except SuwayomiError as e:
        problems.append(f"suwayomi: {e}")
    for name, path in (("staging", config.STAGING_ROOT), ("library", config.LIBRARY_ROOT)):
        if not os.path.isdir(path):
            problems.append(f"{name} path missing: {path}")
    if not os.access(config.LIBRARY_ROOT, os.W_OK):
        problems.append(f"library not writable: {config.LIBRARY_ROOT}")
    status = 200 if not problems else 503
    return JSONResponse({"ok": not problems, "problems": problems, "version": __version__}, status_code=status)


# -- api ----------------------------------------------------------------------

@app.get("/api/v1/system/status")
def api_status():
    return {"version": __version__, "uptime": int(time.time() - STARTED), "job": runner.current.as_dict()
            if runner.current else None, "nextRefresh": scheduler.next_at if scheduler else None}


@app.get("/api/v1/series")
def api_series():
    with db.connect() as con:
        return [dict(r) for r in db.series_rows(con)]


@app.get("/api/v1/series/{series_id}")
def api_series_one(series_id: int):
    with db.connect() as con:
        r = db.get_series(con, series_id)
        if not r:
            raise HTTPException(404)
        return {"series": dict(r), "sources": [dict(s) for s in db.sources(con, series_id)],
                "chapters": [dict(c) for c in db.chapters(con, series_id)]}


@app.post("/api/v1/series")
def api_series_add(body: dict):
    ref, download = body.get("ref"), body.get("download", True)
    if ref == "manual":
        series = model.manual(body.get("title", ""), *body.get("aliases", []))
    else:
        series = metadata.by_ref(ref)
    if not series or not series.title.strip() or series.title == "?":
        raise HTTPException(400, "unknown ref or empty title")
    return runner.submit("add", series.title, _job_add(series, bool(download))).as_dict()


@app.post("/api/v1/series/{series_id}/refresh")
def api_series_refresh(series_id: int, download: bool = True):
    with db.connect() as con:
        r = db.get_series(con, series_id)
    if not r:
        raise HTTPException(404)
    return runner.submit("refresh", r["title"], _job_refresh(series_id, download), series_id).as_dict()


@app.delete("/api/v1/series/{series_id}")
def api_series_delete(series_id: int, files: bool = False):
    with db.connect() as con:
        if not db.get_series(con, series_id):
            raise HTTPException(404)
        core.delete_series(con, client, series_id, delete_library=files)
    return {"ok": True}


@app.get("/api/v1/lookup")
def api_lookup(term: str):
    pick, cands = metadata.lookup(term)
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
        return scheduler.trigger().as_dict()
    raise HTTPException(400, f"unknown command {name!r}")


@app.get("/api/v1/log")
def api_log(lines: int = 200):
    return JSONResponse({"lines": _tail_log(lines)})


# -- helpers ------------------------------------------------------------------

def _suwayomi_queue() -> dict:
    try:
        d = client.gq("{ downloadStatus { state queue { state progress tries manga { title } chapter { name } } } }",
                      timeout=20, retries=1)["downloadStatus"]
        return {"state": d["state"], "items": [
            {"manga": x["manga"]["title"], "chapter": x["chapter"]["name"], "state": x["state"],
             "progress": round((x["progress"] or 0) * 100), "tries": x["tries"]} for x in d["queue"][:20]],
            "count": len(d["queue"])}
    except SuwayomiError as e:
        return {"state": "UNREACHABLE", "items": [], "count": 0, "error": str(e)}


def _config_view() -> list[tuple[str, str]]:
    keep = ("SUWAYOMI_URL", "DATA_DIR", "DB_PATH", "STAGING_ROOT", "LIBRARY_ROOT", "REFRESH_HOURS",
            "UNUSABLE_SOURCES", "THROTTLED_SOURCES", "MIN_PAGES", "DISAGREE", "LOG_LEVEL", "LOG_FILE")
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
