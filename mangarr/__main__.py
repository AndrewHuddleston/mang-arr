"""Command line: search, resolve (dry run), add, status."""
import argparse
import signal
import sys

from . import anilist, db, downloader
from .matching import AUTHOR_AGREE, AUTHOR_DIFFER, EXACT
from .resolver import Plan, primary, ranges, resolve
from .suwayomi import Client


def log(msg: str = "") -> None:
    print(msg, flush=True)


def pick_series(a) -> anilist.Series | None:
    if a.manual:
        return anilist.manual(a.query, *(a.alias or []))
    if a.anilist:
        s = anilist.by_id(a.anilist)
        if not s:
            log(f"no AniList entry with id {a.anilist}")
        return s
    cands = anilist.search(a.query)
    if not cands:
        log(f"AniList has nothing for {a.query!r}")
        return None
    best = cands[0]
    if len(cands) > 1 and _ambiguous(cands, a.query):
        log(f"several AniList entries could be {a.query!r}; pick one with --anilist ID:\n")
        _print_candidates(cands)
        return None
    return best


def _ambiguous(cands, query) -> bool:
    from .matching import query_score
    s0 = min(query_score(t, query) for t in cands[0].titles)
    s1 = min(query_score(t, query) for t in cands[1].titles)
    return s0 > 0 and s0 == s1


def _print_candidates(cands):
    for s in cands:
        log(f"  {s.anilist_id:>7}  {s.title[:44]:<44} {s.format or '?':<8} {s.country or '?'}"
            f"  {s.status or '?':<9} {str(s.chapters or '?'):>4} ch")
        alts = [t for t in s.titles if t != s.title][:3]
        if alts:
            log(f"           aka: {' | '.join(alts)}")


def describe(series: anilist.Series) -> None:
    if series.anilist_id is None:
        log(f"{series.title}  [manual: no metadata, exact title only]")
    else:
        log(f"{series.title}  [AniList {series.anilist_id}]  {series.format} {series.country}"
            f"  {series.status}  {series.chapters or '?'} chapters")
    if series.authors:
        log(f"  by {', '.join(series.authors[:3])}")
    log(f"  titles: {' | '.join(series.titles[:6])}")
    log()


def show_plan(plan: Plan) -> None:
    log()
    used = sorted(set(m.source.name for m in plan.assignment.values()))
    for r in plan.rejected[:12]:
        log(f"  rejected  {r.source.name:<24} {r.title[:40]!r:<42} ({r.reason})")
    if len(plan.rejected) > 12:
        log(f"  ... {len(plan.rejected) - 12} more rejected")
    log()
    if not plan.assignment:
        log("  no usable source has this series.")
        return
    per = {}
    for n, m in plan.assignment.items():
        per.setdefault(m.source.name, []).append(n)
    for name in used:
        log(f"  {name:<26} provides {len(per[name]):>4}: {ranges(per[name])}")
    if plan.junk:
        junk = sorted(plan.junk)
        srcs = sorted(set(m.source.name for m, _ in plan.junk.values()))
        log(f"  {'junk, skipped':<26} {len(junk):>13}: {ranges(junk)}  ({', '.join(srcs)}; 1-{max(p for _, p in plan.junk.values())} pages each)")
    wanted = plan.wanted()
    log()
    log(f"  chapters listed: {len(plan.chapters)}  on disk: {len(plan.have())}"
        f"  wanted: {len(wanted)}  gaps nobody has: {ranges(plan.gaps()) if plan.gaps() else 'none'}")
    if plan.series.chapters and plan.series.status == "FINISHED":
        top = max(plan.chapters)
        if top < plan.series.chapters - 0.5:
            log(f"  ! sources stop at {top:g} but AniList says the series has {plan.series.chapters}")
    p = primary(plan)
    if p:
        log(f"  primary (kept in Suwayomi library for updates): {p.source.name}")


def cmd_search(a):
    cands = anilist.search(a.query)
    if not cands:
        log("nothing on AniList."); return 1
    _print_candidates(cands)
    return 0


def cmd_resolve(a):
    series = pick_series(a)
    if not series:
        return 1
    describe(series)
    client = Client()
    plan = resolve(client, series, log=log)
    show_plan(plan)
    return 0


def cmd_add(a):
    series = pick_series(a)
    if not series:
        return 1
    describe(series)
    client = Client()
    plan = resolve(client, series, log=log)
    show_plan(plan)
    p = primary(plan)
    if not p:
        return 1
    with db.connect() as con:
        series_id = db.upsert_series(con, series)
        db.save_plan(con, series_id, plan, p.manga_id)
    for m in plan.matches:
        client.set_in_library(m.manga_id, m.manga_id == p.manga_id)
    log(f"\ntracking {series.title}; {p.source.name} entry is in the Suwayomi library.")
    if a.no_download:
        return 0
    wanted = plan.wanted()
    if not wanted:
        log("nothing to download."); return 0
    log(f"\ndownloading {len(wanted)} chapter(s):")
    results = downloader.download(client, plan, log=log)
    with db.connect() as con:
        db.record_results(con, series_id, results)
    ok = sum(1 for r in results.values() if r == "ok")
    log(f"\nFINISHED: {ok} downloaded, {len(results) - ok} failed")
    return 0


def cmd_status(a):
    with db.connect() as con:
        rows = db.series_rows(con)
    if not rows:
        log("no series tracked yet."); return 0
    log(f"{'SERIES':<44} {'HAVE':>5} {'LISTED':>6} {'WANTED':>6}  STATUS")
    for r in rows:
        log(f"{r['title'][:44]:<44} {r['have']:>5} {r['listed']:>6} {r['wanted']:>6}  {r['status'] or ''}")
    return 0


def main(argv=None):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)      # `| head` should not traceback
    p = argparse.ArgumentParser(prog="mangarr", description="Sonarr for manga.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="AniList candidates for a title")
    s.add_argument("query"); s.set_defaults(fn=cmd_search)
    for name, fn, help_ in (("resolve", cmd_resolve, "dry run: where each chapter would come from"),
                            ("add", cmd_add, "track a series and download what is missing")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("query", nargs="?", default="")
        s.add_argument("--anilist", type=int, help="AniList id, when the title is ambiguous")
        s.add_argument("--manual", action="store_true",
                       help="no metadata lookup; the typed title is the series (Western webtoons)")
        s.add_argument("--alias", action="append", metavar="TITLE",
                       help="with --manual: another exact title sources may use (repeatable)")
        if name == "add":
            s.add_argument("--no-download", action="store_true")
        s.set_defaults(fn=fn)
    s = sub.add_parser("status", help="tracked series"); s.set_defaults(fn=cmd_status)
    a = p.parse_args(argv)
    if a.cmd in ("resolve", "add") and not a.query and not a.anilist:
        p.error("give a title or --anilist ID")
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
