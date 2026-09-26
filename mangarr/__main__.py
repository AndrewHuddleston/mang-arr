"""Command line: search, resolve (dry run), add, refresh, import, adopt, status, show, daemon."""
import argparse
import logging
import os
import signal
import sys

from . import config, core, daemon, db, logsetup, metadata, model
from .model import Series
from .resolver import Plan, primary, ranges, resolve
from .suwayomi import Client, SuwayomiError

log = logging.getLogger("mangarr.cli")


def out(msg: str = "") -> None:
    print(msg, flush=True)


# -- identity ----------------------------------------------------------------

def pick_series(a) -> Series | None:
    if getattr(a, "manual", False):
        return model.manual(a.query, *(a.alias or []))
    if getattr(a, "anilist", None):
        return _or_complain(metadata.by_ref(f"anilist:{a.anilist}"), f"AniList {a.anilist}")
    if getattr(a, "mangadex", None):
        return _or_complain(metadata.by_ref(f"mangadex:{a.mangadex}"), f"MangaDex {a.mangadex}")
    pick, cands = metadata.lookup(a.query)
    if pick:
        return pick
    if not cands:
        out(f"no database has {a.query!r}. Add it by exact title with --manual.")
        return None
    out(f"no single exact match for {a.query!r}; choose one with --anilist ID or --mangadex UUID,"
        " or use --manual:\n")
    print_candidates(cands)
    return None


def _or_complain(s, what):
    if not s:
        out(f"nothing found for {what}")
    return s


def print_candidates(cands: list[Series]) -> None:
    for s in cands:
        out(f"  {s.ref:<46} {s.title[:40]:<40} {s.format or '?':<8} {s.country or '?':<3}"
            f" {s.status or '?':<9} {str(s.chapters or '?'):>4} ch")
        alts = [t for t in s.titles if t != s.title][:3]
        if alts:
            out(f"      aka: {' | '.join(alts)}")


def describe(series: Series) -> None:
    if series.manual:
        out(f"{series.title}  [manual: no metadata, exact title only]")
    else:
        out(f"{series.title}  [{series.ref}]  {series.format} {series.country}"
            f"  {series.status}  {series.chapters or '?'} chapters")
    if series.authors:
        out(f"  by {', '.join(series.authors[:3])}")
    out(f"  titles: {' | '.join(series.titles[:6])}")
    out()


def show_plan(plan: Plan) -> None:
    out()
    if plan.rejected:
        shown = plan.rejected[:6]
        for r in shown:
            out(f"  rejected  {r.source.name:<24} {r.title[:40]!r:<42} ({r.reason})")
        if len(plan.rejected) > 6:
            out(f"  ... {len(plan.rejected) - 6} more rejected (--debug lists them)")
        out()
    if not plan.assignment:
        out("  no usable source has this series.")
        return
    per: dict[str, list[float]] = {}
    for n, m in plan.assignment.items():
        per.setdefault(m.source.name, []).append(n)
    for name in sorted(per):
        out(f"  {name:<26} provides {len(per[name]):>4}: {ranges(per[name])}")
    if plan.junk:
        junk = sorted(plan.junk)
        srcs = sorted(set(m.source.name for m, _ in plan.junk.values()))
        out(f"  {'junk, skipped':<26} {len(junk):>13}: {ranges(junk)}  ({', '.join(srcs)}; "
            f"1-{max(p for _, p in plan.junk.values())} pages each)")
    wanted = plan.wanted()
    out()
    out(f"  chapters listed: {len(plan.chapters)}  on disk: {len(plan.have())}"
        f"  wanted: {len(wanted)}  gaps nobody has: {ranges(plan.gaps()) if plan.gaps() else 'none'}")
    if plan.series.chapters and plan.series.status == "FINISHED":
        top = max(plan.chapters)
        if top < plan.series.chapters - 0.5:
            out(f"  ! sources stop at {top:g} but the series has {plan.series.chapters}")
    p = primary(plan)
    if p:
        out(f"  primary (kept in Suwayomi library for updates): {p.source.name}")


# -- commands ----------------------------------------------------------------

def cmd_search(a):
    _, cands = metadata.lookup(a.query)
    if not cands:
        out("nothing on AniList or MangaDex.")
        return 1
    print_candidates(cands)
    return 0


def cmd_resolve(a):
    series = pick_series(a)
    if not series:
        return 1
    describe(series)
    plan = resolve(Client(), series)
    show_plan(plan)
    return 0


def cmd_add(a):
    series = pick_series(a)
    if not series:
        return 1
    describe(series)
    client = Client()
    with db.connect() as con:
        o = core.add_series(con, client, series, download=not a.no_download)
    show_plan(o.plan)
    if not primary(o.plan):
        return 1
    out(f"\ntracking {series.title} as #{o.series_id}; {o.imported} chapter(s) in the library")
    if o.results:
        out(f"FINISHED: {o.downloaded} downloaded, {o.failed} failed")
    return 0


def _find(con, text):
    rows = db.find_series(con, text)
    if not rows:
        out(f"no tracked series matches {text!r}")
        return None
    if len(rows) > 1:
        out("several match; use the id:")
        for r in rows:
            out(f"  {r['id']:>4}  {r['title']}")
        return None
    return rows[0]


def cmd_refresh(a):
    client = Client()
    with db.connect() as con:
        if a.series:
            rows = [_find(con, a.series)]
        else:
            rows = [r for r in db.series_rows(con) if r["monitored"] or a.all]
        rows = [r for r in rows if r]
        if not rows:
            return 1
        failures = 0
        for r in rows:
            out(f"\n=== {r['title']}")
            try:
                o = core.refresh_series(con, client, r["id"], download=not a.no_download)
            except core.Gone as e:
                out(f"  skipped: {e}")
                continue
            except SuwayomiError as e:
                failures += 1
                log.error("%s: %s", r["title"], e)
                out(f"  error: {e}")
                continue
            show_plan(o.plan)
            if o.results:
                out(f"  downloaded {o.downloaded}, failed {o.failed}, imported {o.imported}")
    return 1 if failures else 0


def cmd_import(a):
    with db.connect() as con:
        rows = [_find(con, a.series)] if a.series else db.series_rows(con)
        for r in rows:
            if r:
                n = core.import_series(con, r["id"])
                out(f"  {r['title']}: {n} newly linked")
    return 0


def cmd_adopt(a):
    client = Client()
    out("scanning staged series folders ...")
    items = core.plan_adopt(client, only=a.only)
    ok = [i for i in items if i.series]
    review = [i for i in items if not i.series]
    out(f"\n{len(items)} folders: {len(ok)} identified, {len(review)} need review")
    for it in review:
        out(f"\n  REVIEW  {it.source} / {it.folder_name}  ({len(it.numbers)} ch)")
        print_candidates(it.candidates[:4])
        out("      adopt it with:  mangarr add --anilist ID   (or --mangadex UUID / --manual \"Title\")")
    if a.dry_run:
        return 0
    with db.connect() as con:
        ids, n_ch = core.apply_adopt(con, ok)
    out(f"\nadopted {len(ids)} series, {n_ch} chapters. Next: mangarr import (library links),"
        " mangarr refresh (find missing chapters).")
    return 0


def cmd_status(a):
    with db.connect() as con:
        rows = db.series_rows(con)
    if not rows:
        out("no series tracked yet.")
        return 0
    out(f"{'ID':>4} {'SERIES':<44} {'HAVE':>5} {'LISTED':>6} {'WANTED':>6}  {'STATUS':<9} PRIMARY")
    for r in rows:
        out(f"{r['id']:>4} {r['title'][:44]:<44} {r['have']:>5} {r['listed']:>6} {r['wanted']:>6}"
            f"  {(r['status'] or '')[:9]:<9} {r['primary_source'] or '-'}")
    return 0


def cmd_show(a):
    with db.connect() as con:
        r = _find(con, a.series)
        if not r:
            return 1
        describe(db.series_to_model(r))
        for s in db.sources(con, r["id"]):
            out(f"  {'*' if s['is_primary'] else ' '} {s['source_name']:<26} {s['title'][:34]:<34}"
                f" {s['chapter_count']:>4} ch  {s['note'] or ''}")
        chs = db.chapters(con, r["id"])
        by = {}
        for c in chs:
            by.setdefault(c["status"], []).append(c["number"])
        out()
        for st in ("have", "wanted", "failed", "junk", "unavailable"):
            if by.get(st):
                out(f"  {st:<8} {len(by[st]):>4}: {ranges(by[st])}")
        out()
        for e in con.execute("SELECT at, kind, message FROM event WHERE series_id=? ORDER BY id DESC LIMIT 8",
                             (r["id"],)):
            out(f"  {e['at']}  {e['kind']:<10} {e['message']}")
    return 0


def cmd_daemon(a):
    daemon.run(a.interval or config.REFRESH_HOURS, once=a.once)
    return 0


def cmd_serve(a):
    try:
        import uvicorn
    except ImportError:
        out("the web UI needs the extras: pip install -r requirements.txt")
        return 1
    log.info("serving on http://%s:%d (data %s, staging %s, library %s)", a.host, a.port,
             config.DATA_DIR, config.STAGING_ROOT, config.LIBRARY_ROOT)
    uvicorn.run("mangarr.web.app:app", host=a.host, port=a.port, log_config=None, access_log=False)
    return 0


def main(argv=None):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)      # `| head` should not traceback
    p = argparse.ArgumentParser(prog="mangarr", description="Sonarr for manga.")
    p.add_argument("--debug", action="store_true", help="log every request and decision")
    p.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR (default INFO or $MANGARR_LOG_LEVEL)")
    p.add_argument("--log-file", help="also log to this file (rotated; default $MANGARR_LOG_FILE)")
    p.add_argument("--quiet", action="store_true", help="no progress on the console, only results")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="database candidates for a title")
    s.add_argument("query")
    s.set_defaults(fn=cmd_search)

    for name, fn, help_ in (("resolve", cmd_resolve, "dry run: where each chapter would come from"),
                            ("add", cmd_add, "track a series and download what is missing")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("query", nargs="?", default="")
        s.add_argument("--anilist", type=int, help="AniList id, when the title is ambiguous")
        s.add_argument("--mangadex", help="MangaDex uuid")
        s.add_argument("--manual", action="store_true",
                       help="no metadata lookup; the typed title is the series")
        s.add_argument("--alias", action="append", metavar="TITLE",
                       help="with --manual: another exact title sources may use (repeatable)")
        if name == "add":
            s.add_argument("--no-download", action="store_true")
        s.set_defaults(fn=fn)

    s = sub.add_parser("refresh", help="re-resolve tracked series and fetch new chapters")
    s.add_argument("series", nargs="?", help="title fragment or id (default: every monitored series)")
    s.add_argument("--no-download", action="store_true")
    s.add_argument("--all", action="store_true", help="include unmonitored series")
    s.set_defaults(fn=cmd_refresh)

    s = sub.add_parser("import", help="link downloaded chapters into the library")
    s.add_argument("series", nargs="?")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("adopt", help="register what Suwayomi already downloaded")
    s.add_argument("--only", help="folder name fragment")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_adopt)

    s = sub.add_parser("status", help="tracked series")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("show", help="one series in detail")
    s.add_argument("series")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("daemon", help="background worker: refresh every N hours")
    s.add_argument("--interval", type=float, metavar="HOURS")
    s.add_argument("--once", action="store_true", help="one cycle, then exit")
    s.set_defaults(fn=cmd_daemon)

    s = sub.add_parser("serve", help="web UI + API + background worker")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=6789)
    s.set_defaults(fn=cmd_serve)

    a = p.parse_args(argv)
    log_file = a.log_file or config.LOG_FILE
    if a.cmd == "serve" and not log_file:          # the System page tails this
        log_file = os.path.join(config.DATA_DIR, "mangarr.log")
        config.LOG_FILE = log_file
    try:
        logsetup.setup("DEBUG" if a.debug else a.log_level, log_file, console=not a.quiet)
    except ValueError as e:
        p.error(str(e))
    if a.cmd in ("resolve", "add") and not a.query and not a.anilist and not a.mangadex:
        p.error("give a title, --anilist ID or --mangadex UUID")
    try:
        return a.fn(a) or 0
    except KeyboardInterrupt:
        out("\ninterrupted")
        return 130
    except (SuwayomiError, metadata.LookupError_, RuntimeError, OSError, ValueError) as e:
        log.error("%s: %s", type(e).__name__, e)
        out(f"error: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
