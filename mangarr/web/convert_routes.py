"""E-reader conversion in the web UI and the API: the targets on Settings ->
Media Management, the queue on the Activity page, how a series is read on
its page, and the converted copies' downloads (conversions.py does the
work).

app.py registers `router` and calls init() from its lifespan, which starts
the conversion service.
"""
import logging
import os
import stat

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .. import config, conversions, convert, db, library
from ..convert import profiles
from . import views

log = logging.getLogger(__name__)
router = APIRouter()
_app = None                  # the app module, set by init()
BACK = "/settings/media-management"
MEDIA_TYPES = {"epub": "application/epub+zip", "kepub": "application/epub+zip",
               "cbz": "application/vnd.comicbook+zip", "pdf": "application/pdf"}
OPTION_FIELDS = ("spreads", "colour")               # choices
OPTION_FLAGS = ("crop", "upscale", "pad")           # on / off
DIRECTIONS = (("auto", "Auto"), ("rtl", "Right to left"), ("ltr", "Left to right"))
LAYOUTS = (("auto", "Auto"), ("paged", "Pages"), ("webtoon", "Webtoon strip"))


def init(app_module=None) -> None:
    global _app
    if app_module is None:
        from . import app as app_module
    _app = app_module
    conversions.service.start()


def _flash(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse(f"{path}{'&' if '?' in path else '?'}{views.flash_query(msg)}#conversion", 303)


def _on(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def human_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 5400:
        return f"about {seconds / 3600:.0f} h"
    if seconds >= 90:
        return f"about {seconds / 60:.0f} min"
    return f"about {max(seconds, 1)} s"


def page_context(con) -> dict:
    """What the E-reader Conversion section of Settings -> Media Management shows."""
    ok, detail = convert.available()
    per = conversions.counts(con)
    rows = []
    for t in conversions.targets(con):
        prof = profiles.PROFILES.get(t["profile"])
        rows.append({**t, "device": prof.name if prof else t["profile"],
                     "format_label": profiles.FORMAT_LABELS.get(t["format"], t["format"]),
                     "path": conversions.target_dir(t), "counts": per.get(t["id"], {}),
                     "size": views.human_size((per.get(t["id"]) or {}).get("bytes", 0))})
    est = conversions.estimate(con)
    groups: dict[str, list] = {}
    for p in profiles.PROFILES.values():
        groups.setdefault(p.group or "Other", []).append(
            {"key": p.key, "name": p.name, "size": f"{p.width}x{p.height}", "colour": p.colour,
             "formats": list(p.formats), "default_format": p.default_format})
    return {"available": ok, "engine": detail, "root": conversions.root(), "problem": conversions.root_problem(),
            "targets": rows, "max_targets": conversions.MAX_TARGETS, "profile_groups": groups,
            "format_labels": profiles.FORMAT_LABELS, "kindle_note": profiles.KINDLE_NOTE,
            "estimate": {**est, "time": human_time(est["seconds"]), "size": views.human_size(est["bytes"])},
            "default_target": conversions.default_target(), "status": conversions.service.status(con)}


def _target_from(form) -> dict:
    options = {}
    for k in OPTION_FIELDS:
        if form.get(k):
            options[k] = str(form.get(k))
    for k in OPTION_FLAGS:
        if k in form:
            options[k] = _on(form.getlist(k)[-1])
    try:
        if str(form.get("gamma") or "").strip():
            options["gamma"] = float(form.get("gamma"))
        if str(form.get("quality") or "").strip():
            options["quality"] = int(float(form.get("quality")))
    except (TypeError, ValueError):
        raise conversions.TargetError("Contrast and JPEG Quality must be numbers") from None
    d = {"name": form.get("name"), "profile": form.get("profile"), "format": form.get("format"),
         "folder": form.get("folder"), "scope": form.get("scope") or "all", "options": options}
    if "enabled" in form:
        d["enabled"] = _on(form.getlist("enabled")[-1])
    return d


# -- Settings -> Media Management ----------------------------------------------------------------

@router.post("/settings/convert/targets")
async def target_add(request: Request):
    form = await request.form()
    return await run_in_threadpool(_target_add, form)


def _target_add(form) -> RedirectResponse:
    try:
        with db.connect() as con:
            tid = conversions.add_target(con, _target_from(form))
            t = conversions.get_target(con, tid)
    except conversions.TargetError as e:
        return _flash(BACK, f"target not added: {e}")
    conversions.service.reconcile_soon()
    return _flash(BACK, f"target {t['name']} added: " + ("the chapters you have are being converted, new ones as "
                                                         "they arrive" if t["scope"] == "all" else
                                                         "chapters are converted as they arrive from now on"))


@router.post("/settings/convert/targets/{target_id}")
async def target_edit(request: Request, target_id: int):
    form = await request.form()
    return await run_in_threadpool(_target_edit, target_id, form)


def _target_edit(target_id: int, form) -> RedirectResponse:
    try:
        with db.connect() as con:
            t = conversions.update_target(con, target_id, _target_from(form))
    except LookupError:
        raise HTTPException(404, "no such target") from None
    except (conversions.TargetError, OSError) as e:
        return _flash(BACK, f"target not changed: {e}")
    conversions.service.reconcile_soon()
    return _flash(BACK, f"target {t['name']} saved")


@router.post("/settings/convert/targets/{target_id}/delete")
async def target_delete(request: Request, target_id: int):
    form = await request.form()
    return await run_in_threadpool(_target_delete, target_id, _on(form.get("files", "0")))


def _target_delete(target_id: int, files: bool) -> RedirectResponse:
    try:
        with db.connect() as con:
            t = conversions.get_target(con, target_id)
            removed = conversions.delete_target(con, target_id, files)
    except LookupError:
        raise HTTPException(404, "no such target") from None
    return _flash(BACK, f"target {t['name']} removed" + (f" with {removed} file(s)" if files else
                                                         "; its files are kept in " + conversions.target_dir(t)))


@router.post("/settings/convert/targets/{target_id}/queue")
async def target_queue(request: Request, target_id: int):
    form = await request.form()
    again = str(form.get("mode", "missing")) == "all"
    with db.connect() as con:
        if conversions.get_target(con, target_id) is None:
            raise HTTPException(404, "no such target")
        n = conversions.queue_existing(con, target_id, again=again, priority=2)
    conversions.service.reconcile_soon()
    return _flash(BACK, f"{n} chapter(s) queued for conversion" if n else "nothing to queue: every chapter has its "
                                                                          "copy or is waiting for it")


# -- Activity ------------------------------------------------------------------------------------

def activity_context(con) -> dict:
    st = conversions.service.status(con)
    names = {t["id"]: t["name"] for t in conversions.targets(con)}
    failed = [dict(r) | {"target": names.get(r["target_id"], "?")} for r in con.execute(
        "SELECT v.series_id, v.number, v.target_id, v.reason, v.next_try, v.updated_at, s.title FROM conversion v"
        " JOIN series s ON s.id=v.series_id WHERE v.status='failed' ORDER BY v.updated_at DESC LIMIT 50").fetchall()]
    return {"status": st, "failed": failed, "eta": human_time(st["eta_seconds"]) if st["waiting"] else "",
            "any": bool(names)}


@router.post("/activity/conversions/{action}")
def activity_action(action: str):
    with db.connect() as con:
        if action == "pause":
            conversions.service.pause(con)
            msg = "conversion paused"
        elif action == "resume":
            conversions.service.resume(con)
            msg = "conversion goes on"
        elif action == "cancel-current":
            msg = "the running conversion is stopped and queued again" if conversions.service.cancel_current() \
                else "no conversion is running"
        elif action == "retry-failed":
            n = con.execute("UPDATE conversion SET status='pending', tries=0, next_try=NULL, reason=NULL,"
                            " queued_at=?, updated_at=? WHERE status='failed'", (db.now(), db.now())).rowcount
            con.commit()
            conversions.service.reconcile_soon()
            msg = f"{n} failed conversion(s) queued again"
        else:
            raise HTTPException(404)
    return RedirectResponse(f"/activity?{views.flash_query(msg)}#conversions", 303)


# -- a series: how it is read, and its copies ---------------------------------------------------------

def series_context(con, series_row, numbers) -> dict:
    """For the series page: the series' e-reader settings and, per chapter
    shown, its copies {number: [{target, status, url, tip}]}."""
    every = {t["id"]: t for t in conversions.targets(con)}
    copies: dict[float, list] = {}
    if every and numbers:
        marks = ",".join("?" * len(numbers))
        for r in con.execute(f"SELECT * FROM conversion WHERE series_id=? AND number IN ({marks}) ORDER BY target_id",
                             (series_row["id"], *numbers)).fetchall():
            t = every.get(r["target_id"])
            if t is None or r["status"] == "skipped":
                continue
            tip = (f"{r['pages']} pages, {views.human_size(r['bytes'])}, "
                   f"{'webtoon' if r['webtoon'] else 'right to left' if r['rtl'] else 'left to right'}"
                   if r["status"] == "done" else (r["reason"] or r["status"]))
            copies.setdefault(r["number"], []).append({
                "target": t["name"], "format": t["format"].upper(), "status": r["status"], "tip": tip,
                "url": f"/series/{series_row['id']}/chapter/{r['number']}/converted/{t['id']}"
                       if r["status"] == "done" else None})
    rtl, why = conversions.direction(series_row)
    keys = series_row.keys()
    return {"copies": copies, "any_target": bool(every), "directions": DIRECTIONS, "layouts": LAYOUTS,
            "reading_direction": series_row["reading_direction"] if "reading_direction" in keys else "auto",
            "layout": series_row["layout"] if "layout" in keys else "auto",
            "convert_enabled": bool(series_row["convert_enabled"]) if "convert_enabled" in keys else True,
            "auto_direction": f"{'right to left' if rtl else 'left to right'} ({why})"}


def _save_series(series_id: int, values: dict) -> str:
    rd = str(values.get("reading_direction") or "auto")
    lo = str(values.get("layout") or "auto")
    if rd not in dict(DIRECTIONS) or lo not in dict(LAYOUTS):
        raise ValueError("reading direction: auto, rtl or ltr; layout: auto, paged or webtoon")
    on = values.get("convert_enabled")
    with db.connect() as con:
        if db.get_series(con, series_id) is None:
            raise LookupError
        con.execute("UPDATE series SET reading_direction=?, layout=?, convert_enabled=? WHERE id=?",
                    (rd, lo, int(bool(on)), series_id))
        con.commit()
    conversions.service.notify(series_id)       # what was made another way is made again
    return "e-reader settings saved; copies made another way are made again"


@router.post("/series/{series_id}/convert/settings")
async def series_settings(request: Request, series_id: int):
    form = await request.form()
    values = {"reading_direction": form.get("reading_direction"), "layout": form.get("layout"),
              "convert_enabled": _on(form.getlist("convert_enabled")[-1]) if "convert_enabled" in form else True}
    try:
        msg = await run_in_threadpool(_save_series, series_id, values)
    except LookupError:
        raise HTTPException(404, "no such series") from None
    except ValueError as e:
        msg = f"not saved: {e}"
    return RedirectResponse(f"/series/{series_id}?{views.flash_query(msg)}", 303)


@router.post("/series/{series_id}/convert")
async def series_convert(request: Request, series_id: int):
    form = await request.form()
    again = str(form.get("mode", "missing")) == "all"
    with db.connect() as con:
        if db.get_series(con, series_id) is None:
            raise HTTPException(404, "no such series")
        n = conversions.queue_existing(con, series_id=series_id, again=again, priority=0)
        made = conversions.reconcile(con, series_id, priority=0) if conversions.enabled() else {"new": 0}
    conversions.service.reconcile_soon()
    total = n + made["new"]
    msg = f"{total} chapter(s) queued for conversion" if total else "nothing to convert: no target, or every chapter " \
                                                                    "has its copy"
    return RedirectResponse(f"/series/{series_id}?{views.flash_query(msg)}", 303)


@router.get("/series/{series_id}/chapter/{number}/converted/{target_id}")
def converted_file(series_id: int, number: float, target_id: int):
    """Download a converted copy: only a finished one, only a regular file
    inside its target's folder."""
    with db.connect() as con:
        t = conversions.get_target(con, target_id)
        r = con.execute("SELECT status, output_path FROM conversion WHERE series_id=? AND number=? AND target_id=?",
                        (series_id, number, target_id)).fetchone()
    if t is None or r is None or r["status"] != "done" or not r["output_path"]:
        raise HTTPException(404, "no converted copy")
    path = r["output_path"]
    if not library.is_within(path, conversions.target_dir(t)) or not library.is_within(path, config.CONVERTED_ROOT):
        raise HTTPException(404, "no converted copy")
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise HTTPException(404, "no converted copy")
    except OSError:
        raise HTTPException(404, "the converted copy is gone; it is made again at the next check") from None
    return FileResponse(path, filename=os.path.basename(path),
                        media_type=MEDIA_TYPES.get(t["format"], "application/octet-stream"))


# -- API ---------------------------------------------------------------------------------------------

def _target_out(t: dict, per: dict) -> dict:
    return {"id": t["id"], "name": t["name"], "enabled": t["enabled"], "profile": t["profile"],
            "format": t["format"], "folder": t["folder"], "scope": t["scope"], "options": t["options"],
            "path": conversions.target_dir(t), "counts": per.get(t["id"], {})}


@router.get("/api/v1/conversion")
def api_status():
    """The conversion service: whether it can run, what it is converting,
    what waits, and the counts per target."""
    with db.connect() as con:
        st = conversions.service.status(con)
    ok, detail = st.pop("available")
    return {**st, "available": ok, "engine": detail, "outputFolder": conversions.root(),
            "problem": conversions.root_problem()}


@router.get("/api/v1/conversion/target")
def api_targets():
    with db.connect() as con:
        per = conversions.counts(con)
        return [_target_out(t, per) for t in conversions.targets(con)]


@router.get("/api/v1/conversion/profile")
def api_profiles():
    """The reader profiles a target can be made for."""
    return [{"key": p.key, "name": p.name, "width": p.width, "height": p.height, "colour": p.colour,
             "eink": p.eink, "formats": list(p.formats), "defaultFormat": p.default_format, "group": p.group}
            for p in profiles.PROFILES.values()]


@router.post("/api/v1/conversion/target")
def api_target_add(body: dict):
    """Add a target: {"name", "profile", "format", "folder", "scope": "all" |
    "new", "enabled", "options": {...}}; all but name have defaults."""
    try:
        with db.connect() as con:
            tid = conversions.add_target(con, body)
            out = _target_out(conversions.get_target(con, tid), {})
    except conversions.TargetError as e:
        raise HTTPException(400, str(e)) from e
    conversions.service.reconcile_soon()
    return JSONResponse(out, status_code=201)


@router.put("/api/v1/conversion/target/{target_id}")
def api_target_edit(target_id: int, body: dict):
    try:
        with db.connect() as con:
            t = conversions.update_target(con, target_id, body)
            out = _target_out(t, conversions.counts(con))
    except LookupError:
        raise HTTPException(404, "no such target") from None
    except (conversions.TargetError, OSError) as e:
        raise HTTPException(400, str(e)) from e
    conversions.service.reconcile_soon()
    return out


@router.delete("/api/v1/conversion/target/{target_id}")
def api_target_delete(target_id: int, files: bool = False):
    try:
        with db.connect() as con:
            removed = conversions.delete_target(con, target_id, files)
    except LookupError:
        raise HTTPException(404, "no such target") from None
    return {"ok": True, "filesRemoved": removed}


@router.post("/api/v1/conversion/{action}")
def api_action(action: str):
    """pause | resume | cancel-current | retry-failed | reconcile"""
    if action == "reconcile":
        conversions.service.reconcile_soon()
        return {"ok": True}
    if action not in ("pause", "resume", "cancel-current", "retry-failed"):
        raise HTTPException(404)
    activity_action(action)
    return {"ok": True}


@router.post("/api/v1/series/{series_id}/convert")
def api_series_convert(series_id: int, mode: str = "missing"):
    """Queue a series' chapters for conversion now: mode=missing (what has
    no copy yet) or all (everything again)."""
    with db.connect() as con:
        if db.get_series(con, series_id) is None:
            raise HTTPException(404, "no such series")
        n = conversions.queue_existing(con, series_id=series_id, again=(mode == "all"), priority=0)
        made = conversions.reconcile(con, series_id, priority=0) if conversions.enabled() else {"new": 0}
    conversions.service.reconcile_soon()
    return {"queued": n + made["new"]}


@router.put("/api/v1/series/{series_id}/convert")
def api_series_settings(series_id: int, body: dict):
    """How a series is read: {"readingDirection": "auto" | "rtl" | "ltr",
    "layout": "auto" | "paged" | "webtoon", "enabled": true}."""
    try:
        _save_series(series_id, {"reading_direction": body.get("readingDirection"), "layout": body.get("layout"),
                                 "convert_enabled": body.get("enabled", True)})
    except LookupError:
        raise HTTPException(404, "no such series") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}
