"""Command line: search, resolve (dry run), add, refresh, import, adopt, status, show."""
import argparse
import signal
import sys

from . import core, db, metadata, model
from .model import Series
from .resolver import Plan, primary, ranges, resolve
from .suwayomi import Client


def log(msg: str = "") -> None:
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
        log(f"no database has {a.query!r}. Add it by exact title with --manual.")
        return None
    log(f"no single exact match for {a.query!r}; choose one with --anilist ID or --mangadex UUID,"
        " or use --manual:\n")
    print_candidates(cands)
    return None


def _or_complain(s, what):
    if not s:
        log(f"nothing found for {what}")
    return s


def print_candidates(cands: list[Series]) -> None:
    for s in cands:
        log(f"  {s.ref:<46} {s.title[:40]:<40} {s.format or '?':<8} {s.country or '?':<3}"
            f" {s.status or '?':<9} {str(s.chapters or '?'):>4} ch")
        alts = [t for t in s.titles if t != s.title][:3]
        if alts:
            log(f"      aka: {' | '.join(alts)}")


def describe(series: Series) -> None:
    if series.manual:
        log(f"{series.title}  [manual: no metadata, exact title only]")
    else:
        log(f"{series.title}  [{series.ref}]  {series.format} {series.country}"
            f"  {series.status}  {series.chapters or '?'} chapters")
    if series.authors:
        log(f"  by {', '.join(series.authors[:3])}")
    log(f"  titles: {' | '.join(series.titles[:6])}")
    log()


def show_plan(plan: Plan) -> None:
    log()
    for r in plan.rejected[:8]:
        log(f"  rejected  {r.source.name:<24} {r.title[:40]!r:<42} ({r.reason})")
    if len(plan.rejected) > 8:
        log(f"  ... {len(plan.rejected) - 8} more rejected")
    log()
    if not plan.assignment:
        log("  no usable source has this series.")
        return
    per: dict[str, list[float]] = {}
    for n, m in plan.assignment.items():
        per.setdefault(m.source.name, []).append(n)
    for name in sorted(per):
        log(f"  {name:<26} provides {len(per[name]):>4}: {ranges(per[name])}")
    if plan.junk:
        junk = sorted(plan.junk)
        srcs = sorted(set(m.source.name for m, _ in plan.junk.values()))
        log(f"  {'junk, skipped':<26} {len(junk):>13}: {ranges(junk)}  ({', '.join(srcs)}; "
            f"1-{max(p for _, p in plan.junk.values())} pages each)")
    wanted = plan.wanted()
    log()
    log(f"  chapters listed: {len(plan.chapters)}  on disk: {len(plan.have())}"
        f"  wanted: {len(wanted)}  gaps nobody has: {ranges(plan.gaps()) if plan.gaps() else 'none'}")
    if plan.series.chapters and plan.series.status == "FINISHED":
        top = max(plan.chapters)
        if top < plan.series.chapters - 0.5:
            log(f"  ! sources stop at {top:g} but the series has {plan.series.chapters}")
    p = primary(plan)
    if p:
        log(f"  primary (kept in Suwayomi library for updates): {p.source.name}")


# -- commands ----------------------------------------------------------------

def cmd_search(a):
    _, cands = metadata.lookup(a.query)
    if not cands:
        log("nothing on AniList or MangaDex."); return 1
    print_candidates(cands)
    return 0


def cmd_resolve(a):
    series = pick_series(a)
    if not series:
        return 1
    describe(series)
    plan = resolve(Client(), series, log=log)
    show_plan(plan)
    return 0


def cmd_add(a):
    series = pick_series(a)
    if not series:
        return 1
    describe(series)
    client = Client()
    with db.connect() as con:
        series_id, plan, results = core.add_series(con, client, series, log=log,
                                                   download=not a.no_download)
    show_plan(plan)
    if not primary(plan):
        return 1
    log(f"\ntracking {series.title} as #{series_id}")
    if results:
        ok = sum(1 for r in results.values() if r == "ok")
        log(f"FINISHED: {ok} downloaded, {len(results) - ok} failed")
    return 0


def _find(con, text):
    rows = db.find_series(con, text)
    if not rows:
        log(f"no tracked series matches {text!r}"); return None
    if len(rows) > 1:
        log("several match; use the id:")
        for r in rows:
            log(f"  {r['id']:>4}  {r['title']}")
        return None
    return rows[0]


def cmd_refresh(a):
    client = Client()
    with db.connect() as con:
        rows = [_find(con, a.series)] if a.series else db.series_rows(con)
        rows = [r for r in rows if r]
        if not rows:
            return 1
        for r in rows:
            log(f"\n=== {r['title']}")
            plan, results = core.refresh_series(con, client, r["id"], log=log, download=not a.no_download)
            show_plan(plan)
    return 0


def cmd_import(a):
    with db.connect() as con:
        rows = [_find(con, a.series)] if a.series else db.series_rows(con)
        for r in rows:
            if r:
                n = core.import_series(con, r["id"], log=log)
                log(f"  {r['title']}: {n} in library")
    return 0


def cmd_adopt(a):
    client = Client()
    log("scanning staged series folders ...")
    items = core.plan_adopt(client, log=log, only=a.only)
    ok = [i for i in items if i.series]
    review = [i for i in items if not i.series]
    log(f"\n{len(items)} folders: {len(ok)} identified, {len(review)} need review")
    for it in review:
        log(f"\n  REVIEW  {it.source} / {it.folder_name}  ({len(it.numbers)} ch)")
        print_candidates(it.candidates[:4])
        log("      adopt it with:  mangarr add --anilist ID   (or --mangadex UUID / --manual \"Title\")")
    if a.dry_run:
        return 0
    with db.connect() as con:
        n_series, n_ch = core.apply_adopt(con, ok, log=log)
    log(f"\nadopted {n_series} series, {n_ch} chapters. Next: mangarr import (library links),"
        " mangarr refresh (find missing chapters).")
    return 0


def cmd_status(a):
    with db.connect() as con:
        rows = db.series_rows(con)
    if not rows:
        log("no series tracked yet."); return 0
    log(f"{'ID':>4} {'SERIES':<44} {'HAVE':>5} {'LISTED':>6} {'WANTED':>6}  {'STATUS':<9} PRIMARY")
    for r in rows:
        log(f"{r['id']:>4} {r['title'][:44]:<44} {r['have']:>5} {r['listed']:>6} {r['wanted']:>6}"
            f"  {(r['status'] or '')[:9]:<9} {r['primary_source'] or '-'}")
    return 0


def cmd_show(a):
    with db.connect() as con:
        r = _find(con, a.series)
        if not r:
            return 1
        describe(db.series_to_model(r))
        for s in db.sources(con, r["id"]):
            log(f"  {'*' if s['is_primary'] else ' '} {s['source_name']:<26} {s['title'][:34]:<34}"
                f" {s['chapter_count']:>4} ch  {s['note'] or ''}")
        chs = db.chapters(con, r["id"])
        by = {}
        for c in chs:
            by.setdefault(c["status"], []).append(c["number"])
        log()
        for st in ("have", "wanted", "failed", "junk", "unavailable"):
            if by.get(st):
                log(f"  {st:<8} {len(by[st]):>4}: {ranges(by[st])}")
    return 0


def main(argv=None):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)      # `| head` should not traceback
    p = argparse.ArgumentParser(prog="mangarr", description="Sonarr for manga.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="database candidates for a title")
    s.add_argument("query"); s.set_defaults(fn=cmd_search)

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
    s.add_argument("series", nargs="?", help="title fragment or id (default: all)")
    s.add_argument("--no-download", action="store_true"); s.set_defaults(fn=cmd_refresh)

    s = sub.add_parser("import", help="link downloaded chapters into the library")
    s.add_argument("series", nargs="?"); s.set_defaults(fn=cmd_import)

    s = sub.add_parser("adopt", help="register what Suwayomi already downloaded")
    s.add_argument("--only", help="folder name fragment")
    s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_adopt)

    s = sub.add_parser("status", help="tracked series"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("show", help="one series in detail")
    s.add_argument("series"); s.set_defaults(fn=cmd_show)

    a = p.parse_args(argv)
    if a.cmd in ("resolve", "add") and not a.query and not a.anilist and not a.mangadex:
        p.error("give a title, --anilist ID or --mangadex UUID")
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
