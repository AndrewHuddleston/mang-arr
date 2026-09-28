"""Pure view helpers for the templates: how chapters are grouped into
Sonarr-style "seasons", how a series status reads, how a description is
made plain. No database, no network - unit-testable on plain rows/dicts.
"""
import hashlib
import hmac
import html
import itertools
import re
import secrets
import time
import urllib.parse

from .. import library, model, stuck
from ..resolver import ranges

BLOCK = 20                       # chapters per group when a series has no seasons
# A source can list any number of chapters, and each row is about 2 KB of
# HTML: one series page renders at most PAGE_CHAPTERS rows in at most
# PAGE_GROUPS groups (one chapter per block of 20 is possible), the rest on
# further pages. Real series have up to about 4000 chapters.
PAGE_CHAPTERS = 2000
PAGE_GROUPS = 200
MAX_RANGE_SPANS = 40             # spans in a '1-3, 5, 7-9' tooltip
RANGE_STATUSES = frozenset(("wanted", "failed", "unavailable", "ignored", "junk"))  # the series page's tooltips

STATUS_LABELS = {"RELEASING": "Continuing", "FINISHED": "Ended", "HIATUS": "Hiatus", "CANCELLED": "Cancelled"}
CHAPTER_STATUSES = ("have", "wanted", "failed", "junk", "unavailable", "ignored")
COUNTED = ("have", "wanted", "failed", "unavailable")   # what "listed" means on the index page

# sidebar: (section, icon, [(label, href)]); a section whose first link is
# its own page (Settings, System) has anchors as sub-links
# Settings sub-pages, as in Sonarr: slug -> name, in sidebar order
SETTINGS_PAGES = (("media-management", "Media Management"), ("sources", "Sources"), ("downloading", "Downloading"),
                  ("komga", "Komga"), ("notifications", "Notifications"), ("general", "General"))
NAV = [
    ("Series", "series", [("Add New", "/add"), ("Library Import", "/import"), ("Lists", "/lists")]),
    ("Activity", "activity", [("Queue", "/activity"), ("History", "/activity/history")]),
    ("Wanted", "wanted", [("Missing", "/wanted")]),
    ("Settings", "settings", [(label, f"/settings/{slug}") for slug, label in SETTINGS_PAGES]),
    ("System", "system", [("Status", "/system"), ("Tasks", "/system#tasks"), ("Backups", "/system#backups"),
                          ("Logs", "/system/logs")]),
]
SECTION_HOME = {"Series": "/", "Settings": "/settings/media-management", "System": "/system"}


def status_label(status: str | None) -> str:
    """RELEASING -> Continuing, FINISHED -> Ended ..., None -> Unknown."""
    return STATUS_LABELS.get((status or "").upper(), "Unknown")


def status_class(status: str | None) -> str:
    return status_label(status).lower()


def nav_section(path: str) -> str:
    """Which sidebar section a request path belongs to."""
    if path.startswith("/activity"):
        return "Activity"
    if path.startswith("/wanted"):
        return "Wanted"
    if path.startswith("/settings"):
        return "Settings"
    if path.startswith("/system"):
        return "System"
    return "Series"


def nav_current(path: str) -> str:
    """The sidebar link that is 'on' for a request path."""
    if path.startswith("/series/") or path.startswith("/rename"):
        return "/"
    if path.startswith("/activity/history"):
        return "/activity/history"
    if path.startswith("/system/logs"):
        return "/system/logs"
    for _, _, links in NAV:
        for _, href in links:
            base = href.split("#")[0]
            if base != "/" and path.startswith(base):
                return href
    return "/"


# a tag cannot contain '<': "<[^>]+>" rescans from every '<' of a long run
_TAG = re.compile(r"<[^<>]+>")
_BR = re.compile(r"<br\s*/?>|</p>|</div>", re.I)
MAX_DESCRIPTION = 10_000        # chars rendered; real descriptions are a few thousand at most


def plain_description(text: str | None) -> str:
    """AniList descriptions carry <br> and <i>; make them plain text with
    real line breaks, at most one blank line in a row. The text comes from
    the metadata providers, so it is capped and only linear patterns run on
    it (trailing blanks are stripped per line, not by a regex)."""
    if not text:
        return ""
    t = _BR.sub("\n", text[:MAX_DESCRIPTION])
    t = _TAG.sub("", t)
    t = html.unescape(t)
    t = "\n".join(line.rstrip(" \t") for line in t.split("\n"))
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _val(row, key):
    """row[key] for sqlite rows and dicts, getattr for dataclasses; None when absent."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)


def _block_start(number: float) -> int:
    n = int(number)                      # 12.5 -> 12, so it lands with chapter 12
    if n < 1:
        return 1
    return (n - 1) // BLOCK * BLOCK + 1


def _summary(chapters) -> dict:
    have = sum(1 for c in chapters if _val(c, "status") == "have")
    total = sum(1 for c in chapters if _val(c, "status") in COUNTED)
    return {"have": have, "total": total, "pct": round(100 * have / total) if total else 0,
            "wanted": sum(1 for c in chapters if _val(c, "status") in ("wanted", "failed"))}


def group_chapters(chapters, series_row=None) -> list[dict]:
    """Sonarr-style groups for a series' chapter rows (anything indexable by
    'number', 'status', 'name'). Newest group first, newest chapter first
    inside it; only the newest group is `open`.

    When every chapter that has a name is season-numbered ('S2 - Episode 5'),
    groups are seasons; chapters without a name then go into a trailing
    'Unnumbered' group. Otherwise groups are blocks of 20 by the integer
    part of the number, so 12.5 sits with 12 in 'Chapters 1-20'.

    This is the rule over whole rows. The series page gets the same groups
    from totals (chapter_summary, page_summary), and the tests hold the two
    to the same answer."""
    rows = list(chapters)
    if not rows:
        return []
    seasons: dict[int, tuple[int, float]] = {}
    for c in rows:
        name = _val(c, "name")
        if name:
            s = library.parse_season(name)
            if s is None:                  # one plain name: blocks, and the other names need no parsing
                seasons = {}
                break
            seasons[id(c)] = s
    by_season = bool(seasons)
    groups: dict = {}                      # sort key -> group; keys sort newest first, 'Unnumbered' last
    # (a group's dict is made only when its key is new: this runs once per chapter row)
    if by_season:
        for c in rows:
            s = seasons.get(id(c))
            key = (0, -s[0]) if s else (1, 0)
            if key not in groups:
                groups[key] = {"key": f"season-{s[0]}" if s else "unnumbered",
                               "name": f"Season {s[0]}" if s else "Unnumbered", "chapters": []}
            groups[key]["chapters"].append(c)
    else:
        for c in rows:
            start = _block_start(float(_val(c, "number") or 0))
            key = (0, -start)
            if key not in groups:
                groups[key] = {"key": f"block-{start}", "name": f"Chapters {start}-{start + BLOCK - 1}",
                               "chapters": []}
            groups[key]["chapters"].append(c)
    out = []
    for key in sorted(groups):
        g = groups[key]
        g["chapters"].sort(key=lambda c: float(_val(c, "number") or 0), reverse=True)
        g.update(_summary(g["chapters"]))
        g["open"] = False
        out.append(g)
    newest = next((g for g in out if g["key"] != "unnumbered"), out[0])
    newest["open"] = True
    return out


def chapter_summary(rows, limit: int = MAX_RANGE_SPANS) -> dict:
    """What the series page shows about all of a series' chapters, from one
    pass over (number, status, name) by number (db.chapter_marks) that keeps
    totals, not the rows:

    - counts: counts() of them;
    - runs: {status: short_ranges() text} for RANGE_STATUSES, from the
      first `limit` runs and the number of runs;
    - groups: group_chapters()' groups without their rows, each with its
      `size`; a season's also with its chapter numbers, newest first (a
      season is not one range of numbers), while a block of BLOCK keeps
      only totals. page_summary() fills in one page's rows.

    Names are parsed only until the first plain one: from there on the
    series is in blocks, and what was kept for seasons is dropped."""
    counts = dict.fromkeys(CHAPTER_STATUSES, 0)
    adds = {st: (st == "have", st in COUNTED, st in ("wanted", "failed")) for st in CHAPTER_STATUSES}
    blocks: dict[int, list] = {}             # first number -> [rows, have, counted, wanted]
    seasons: dict | None = {}                # season (None: no name) -> [rows, have, counted, wanted, numbers]
    runs: dict[str, list] = {}               # status -> [first `limit` runs as [first, last], runs, last number]
    for number, status, name in rows:        # (this runs once per chapter row: kept to plain operations)
        add = adds.get(status, (0, 0, 0))
        if status in counts:
            counts[status] += 1
        n = float(number or 0)
        i = int(n)
        start = 1 if i < 1 else (i - 1) // BLOCK * BLOCK + 1          # _block_start()
        b = blocks.get(start)
        if b is None:
            b = blocks[start] = [0, 0, 0, 0]
        b[0] += 1
        b[1] += add[0]
        b[2] += add[1]
        b[3] += add[2]
        if seasons is not None:
            sn = library.parse_season(name) if name else None
            if name and sn is None:
                seasons = None               # one plain name: blocks, and the other names need no parsing
            else:
                g = seasons.get(key := sn[0] if sn else None)
                if g is None:
                    g = seasons[key] = [0, 0, 0, 0, []]
                g[0] += 1
                g[1] += add[0]
                g[2] += add[1]
                g[3] += add[2]
                g[4].append(n)
        if status in RANGE_STATUSES:
            r = runs.get(status)
            if r is None:
                runs[status] = [[[n, n]], 1, n]
            else:
                if n == r[2] + 1 and n == i:                             # resolver.ranges(): the run goes on
                    if r[1] <= limit:
                        r[0][-1][1] = n
                else:
                    r[1] += 1
                    if r[1] <= limit:
                        r[0].append([n, n])
                r[2] = n
    text = {}
    for status, (spans, n_runs, _) in runs.items():
        text[status] = ", ".join(f"{a:g}" if a == b else f"{a:g}-{b:g}" for a, b in spans) + \
            (f", ... ({n_runs - limit} more)" if n_runs > limit else "")
    if seasons and any(k is not None for k in seasons):
        groups = [_totals(f"season-{k}", f"Season {k}", *seasons[k][:4], numbers=seasons[k][4][::-1])
                  for k in sorted((k for k in seasons if k is not None), reverse=True)]
        if None in seasons:
            groups.append(_totals("unnumbered", "Unnumbered", *seasons[None][:4], numbers=seasons[None][4][::-1]))
    else:
        groups = [_totals(f"block-{k}", f"Chapters {k}-{k + BLOCK - 1}", *b) for k, b in
                  sorted(blocks.items(), reverse=True)]
    if groups:
        next((g for g in groups if g["key"] != "unnumbered"), groups[0])["open"] = True
    return {"counts": counts, "runs": text, "groups": groups}


def _totals(key: str, name: str, rows: int, have: int, total: int, wanted: int, numbers=None) -> dict:
    """One of chapter_summary()'s groups: group_chapters()' fields, `size`
    for its rows and no chapters yet."""
    g = {"key": key, "name": name, "chapters": [], "have": have, "total": total,
         "pct": round(100 * have / total) if total else 0, "wanted": wanted, "open": False, "size": rows}
    if numbers is not None:
        g["numbers"] = numbers
    return g


def page_slices(sizes: list[int], page: int = 1, rows: int = PAGE_CHAPTERS,
                max_groups: int = PAGE_GROUPS) -> tuple[list[tuple[int, int, int]], dict]:
    """Which rows of groups of these sizes one page shows: at most `rows`
    rows in at most `max_groups` groups, in order. A group that does not fit
    is split across pages. A page past the last one shows the last. Returns
    ([(group index, first row in it, rows)], info) with info = {page, pages,
    total, first, last}: first/last are row positions (from 1) of the
    page's rows among all `total` rows."""
    page = max(page, 1)
    total = sum(sizes)
    at, used, n_groups, row = 1, 0, 0, 0         # page being filled, its rows and groups, rows before it
    out: list[tuple[int, int, int]] = []
    first = last = 0
    for gi, size in enumerate(sizes):
        start = 0
        while start < size:
            if used >= rows or n_groups >= max_groups:
                at, used, n_groups = at + 1, 0, 0
            take = min(size - start, rows - used)
            if at == page:
                first = first or row + 1
                last = row + take
                out.append((gi, start, take))
            used, n_groups, row, start = used + take, n_groups + 1, row + take, start + take
    if not out and page > at:
        return page_slices(sizes, at, rows, max_groups)
    return out, {"page": page, "pages": at, "total": total, "first": first, "last": last}


def _open_first(out: list[dict]) -> list[dict]:
    if out and not any(g["open"] for g in out):
        out[0]["open"] = True                    # a later page opens its first group
    return out


def page_groups(groups: list[dict], page: int = 1, rows: int = PAGE_CHAPTERS,
                max_groups: int = PAGE_GROUPS) -> tuple[list[dict], dict]:
    """One page of group_chapters()' groups: at most `rows` chapter rows in
    at most `max_groups` groups, in the same order (newest first). A group
    that does not fit is split across pages: the part keeps the whole
    group's header numbers, with `part` set and `size` its full length.
    A page past the last one shows the last. Returns (groups, info) as
    page_slices does."""
    slices, info = page_slices([len(g["chapters"]) for g in groups], page, rows, max_groups)
    out = []
    for gi, start, take in slices:
        g = groups[gi]
        chs = g["chapters"]
        out.append(dict(g, chapters=chs[start:start + take], part=take < len(chs), size=len(chs)))
    return _open_first(out), info


def page_summary(groups: list[dict], page: int, read, rows: int = PAGE_CHAPTERS,
                 max_groups: int = PAGE_GROUPS) -> tuple[list[dict], dict]:
    """page_groups() for chapter_summary()'s groups: read(slices, info) gets
    page_slices()' answer and returns the page's rows in order, which fill
    the groups on the page. For blocks those rows are one range of numbers,
    highest first (db.chapters_newest_first); a season's come by number."""
    slices, info = page_slices([g["size"] for g in groups], page, rows, max_groups)
    got = iter(read(slices, info) if slices else ())
    out = [dict(groups[gi], chapters=list(itertools.islice(got, take)), part=take < groups[gi]["size"])
           for gi, _start, take in slices]
    return _open_first(out), info


def short_ranges(numbers, limit: int = MAX_RANGE_SPANS) -> str:
    """resolver.ranges() cut to `limit` spans ('1-3, 5, ... (12 more)'): a
    tooltip over thousands of scattered chapter numbers stays short."""
    spans = ranges(numbers).split(", ")
    if len(spans) <= limit:
        return ", ".join(spans)
    return ", ".join(spans[:limit]) + f", ... ({len(spans) - limit} more)"


def counts(chapters) -> dict[str, int]:
    """{status: count} for the summary row, every status present."""
    out = dict.fromkeys(CHAPTER_STATUSES, 0)
    for c in chapters:
        st = _val(c, "status")
        if st in out:
            out[st] += 1
    return out


def progress(have: int, listed: int) -> int:
    return round(100 * have / listed) if listed else 0


def progress_kind(status: str | None, monitored, pct: int, downloading: bool = False) -> str:
    """Sonarr's progress-bar colour rule: purple while downloading, green when
    complete and ended, blue when complete and continuing, red when a
    monitored series is missing chapters, orange when an unmonitored one is."""
    if downloading:
        return "purple"
    if pct >= 100:
        return "success" if status_class(status) == "ended" else "primary"
    return "danger" if monitored else "warning"


# chapter status -> (label kind, label text, icon)
CHAPTER_STATUS = {
    "have": ("success", "Downloaded", "downloaded"),
    "wanted": ("danger", "Missing", "missing"),
    "failed": ("danger", "Failed", "warning"),
    "unavailable": ("purple", "Unavailable", "unavailable"),
    "ignored": ("disabled", "Ignored", "ignore"),
    "junk": ("default", "Junk", "junk"),
}


def chapter_status(status: str | None) -> dict:
    kind, text, icon = CHAPTER_STATUS.get(status or "", ("default", status or "unknown", "unknown"))
    return {"kind": kind, "text": text, "icon": icon}


# history event kind -> (icon symbol, icon kind, tooltip)
EVENT_ICONS = {
    "added": ("plus", "success", "Series added"),
    "resolved": ("refresh", "default", "Sources resolved"),
    "downloaded": ("download", "success", "Chapter downloaded"),
    "imported": ("drive", "success", "Chapter imported into the library"),
    "relinked": ("drive", "warning", "Misread file linked again under its real number"),
    "gone": ("drive", "warning", "Library file gone: chapter set back"),
    "failed": ("warning", "danger", "Download failed"),
    "deleted": ("delete", "danger", "Series deleted"),
    "review": ("info", "warning", "Needs a decision"),
    "monitor": ("bookmark", "default", "Monitoring changed"),
    "ignore": ("ignore", "default", "Chapter ignored"),
    "unignore": ("bookmark", "default", "Chapter wanted again"),
    "skip": ("ignore", "default", "Chapter skipped or un-skipped"),
    "list": ("list", "default", "Import list"),
    "renamed": ("edit", "default", "Files renamed"),
}


def event_icon(kind: str | None) -> dict:
    icon, ikind, tip = EVENT_ICONS.get(kind or "", ("unknown", "default", kind or "event"))
    return {"icon": icon, "kind": ikind, "tip": tip}


# -- stuck behind a chapter (stuck.py) -------------------------------------------

SKIP_DISCLAIMER = ("Skipping means this chapter is not downloaded and the chapters after it continue. If it turns out "
                   "to be part of the story you will have a gap. You can undo this: the chapter stays listed as "
                   "skipped and can be un-skipped.")
IMAGES_GONE = ("no working pages", "failed instantly")      # failure reasons that mean its images are gone


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _num(n) -> str:
    return f"{n:g}"


def stuck_note(st) -> dict:
    """The texts of the note on a series stuck behind a chapter (a
    stuck.Stuck): {head: 'Stuck behind chapter 7.2 - 372 chapters waiting.',
    body: 'Only Manganato lists it and its images are gone (failed on every
    try since 2026-09-26).'}. Before a resolve has seen which sites list it
    (a blocker an earlier version left), from its row: its source, and
    whether its last failure said no other source has it."""
    n = _num(st.number)
    head = f"Stuck behind chapter {n} - {st.waiting} chapter{'s' if st.waiting != 1 else ''} waiting."
    gone = any(k in (st.reason or "") for k in IMAGES_GONE)
    what = "its images are gone" if gone else "it could not be downloaded"
    day = (st.failed_since or "")[:10]
    when = (f"failed on every try since {day}" if st.tries > 1 else f"failed on {day}") if day else \
        "failed on every try"
    sites = [k for k in st.names][:4]
    if sites:
        more = len(st.names) - len(sites)
        who = _and(sites + ([f"{more} more"] if more > 0 else []))
        body = f"Only {who} list{'s' if len(st.names) == 1 else ''} it and {what} ({when})."
    elif st.source and "no other source has this chapter" in (st.reason or ""):
        body = f"Only {st.source} lists it and {what} ({when})."
    elif st.source:
        body = f"It failed on {st.source} and every other source that lists it" + \
            (": its images are gone" if gone else "") + f" ({when})."
    else:
        body = f"It failed on every source that lists it ({when})."
    return {"head": head, "body": body}


def auto_skip_clause(st, auto_on: bool) -> str:
    """What the automatic skip does with a blocker (a stuck.Stuck with its
    verdict), for its note: '' when it is off or the verdict does not
    suggest skipping."""
    v = st.verdict
    if not auto_on or v is None or not v.skippable:
        return ""
    why = stuck.why_not_auto(st, v)
    if why == "verdict":
        return "too uncertain to skip automatically"
    if why == "declined":
        return "not skipped automatically: you un-skipped it or wanted it again"
    if why == "waiting":
        return "not skipped automatically: you chose to keep waiting for it"
    if why == "mangadex":
        return ("not skipped automatically before MangaDex's chapter list is checked, which may change this verdict"
                if st.md_wait == "pending" else
                "not skipped automatically while MangaDex does not answer: its chapter list may change this verdict")
    if why == "untried":
        return (f"skipped automatically once a pass has tried it on {_and(st.untried[:3])} too" if st.untried and
                st.names else "skipped automatically once a pass has tried it on every site that lists it")
    if why == "young":
        return ("skipped automatically once passes have judged it so for a day, in a pass in which every site that "
                "lists it answers, unless you keep waiting")
    return ("the next pass in which every site that lists it answers skips it automatically, unless you keep "
            "waiting")


def mangadex_line(st) -> str:
    """The note's line while MangaDex's chapter list is missing from its
    verdict because MangaDex did not answer (a stuck.Stuck), with when it
    is asked again; '' otherwise (a lookup under way has the page's
    "checking MangaDex..." instead)."""
    if st.md_wait != "failed":
        return ""
    when = time.strftime("%H:%M", time.localtime(st.md_retry)) if st.md_retry else ""
    return ("MangaDex did not answer, so its chapter list is not in this verdict yet: it is asked again "
            + (f"after {when}." if when else "later."))


def verdict_label(v, number) -> dict:
    """A verdict (verdict.Verdict) as a small label for the chapter's row:
    {text, kind, tip}."""
    whole = _num(int(number)) if isinstance(number, (int, float)) and number >= 0 else "?"
    text = {"side_story": "Side story?", "covered": f"Covered by {whole}?",
            "rest_of_chapter": f"Rest of {whole}?"}.get(v.kind, "Unknown")
    kind = ("info" if v.confidence == "high" else "default") if v.skippable else \
        "warning" if v.kind == "rest_of_chapter" else "default"
    tip = "\n".join([f"{v.headline} ({v.confidence} confidence)", *v.evidence])
    return {"text": text, "kind": kind, "tip": tip}


def skip_note(k: dict) -> str:
    """The line on a series page for a chapter skipped from a stuck note
    (stuck.skipped): changed since it was skipped, automatically, or by
    you."""
    n = _num(k["number"])
    if k["changed"]:
        names = "; ".join(f'{src}: "{name}"' for src, name in list(k["names"].items())[:3] if name)
        who = "was skipped automatically" if k["how"] == "auto" else "you skipped"
        return (f"Chapter {n}, which {who}, is listed differently now" + (f" ({names})" if names else "") +
                ": it may be a chapter of the story after all.")
    if k["how"] == "auto":
        what = k["verdict"] or "judged skippable"
        return f"Chapter {n} skipped automatically: {what[:1].lower() + what[1:]}"
    return f"Chapter {n} skipped: the chapters after it download without it"


def provider(row_or_series) -> str:
    """Where the metadata came from, like Sonarr's 'network'."""
    if _val(row_or_series, "anilist_id"):
        return "AniList"
    if _val(row_or_series, "mangadex_id"):
        return "MangaDex"
    return "Manual"


def network_line(series) -> str:
    """'AniList · manhwa · Korean' for an add-page result or a series."""
    parts = [provider(series)]
    kind = getattr(series, "kind", None)
    if kind:
        parts.append(kind)
    lang = getattr(series, "language", None)
    if lang:
        parts.append(lang)
    return " · ".join(parts)


def row_kind(row) -> str:
    """manga / manhwa / manhua / webtoon / comic for a plain series row."""
    return model.Series(country=_val(row, "country"), format=_val(row, "format")).kind


def row_language(row) -> str | None:
    return model.Series(country=_val(row, "country"), format=_val(row, "format")).language


def snippet(text: str | None, limit: int = 320) -> str:
    """Plain, single-paragraph excerpt of a description for cards."""
    t = " ".join(plain_description(text).split())
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "…"


def index_stats(rows) -> dict:
    """Numbers for the series index footer, like Sonarr's SeriesIndexFooter."""
    out = {"series": 0, "ended": 0, "continuing": 0, "monitored": 0, "unmonitored": 0, "chapters": 0, "files": 0,
           "wanted": 0}
    for r in rows:
        out["series"] += 1
        if status_class(_val(r, "status")) == "ended":
            out["ended"] += 1
        else:
            out["continuing"] += 1
        if _val(r, "monitored"):
            out["monitored"] += 1
        else:
            out["unmonitored"] += 1
        out["chapters"] += _val(r, "listed") or 0
        out["files"] += _val(r, "have") or 0
        out["wanted"] += _val(r, "wanted") or 0
    return out


_SUWAYOMI_STATE = {"DOWNLOADING": "running", "QUEUED": "queued", "ERROR": "failed", "FINISHED": "done"}


def queue_rows(jobs, squeue: dict | None) -> list[dict]:
    """One list for the Activity queue: mang-arr's jobs and Suwayomi's download
    queue, newest job first, Suwayomi transfers after the active jobs."""
    out = []
    for j in jobs or []:
        d = j.as_dict() if hasattr(j, "as_dict") else dict(j)
        out.append({
            "id": d.get("id"), "client": "mang-arr", "kind": d.get("kind") or "", "title": d.get("title") or "",
            "series_id": d.get("seriesId"), "chapter": "", "status": d.get("status") or "queued",
            "progress": None, "message": d.get("message") or d.get("progress") or "",
            "at": d.get("finishedAt") or d.get("startedAt") or d.get("queuedAt"),
            "cancel": f"/activity/cancel/{d.get('id')}" if d.get("status") in ("queued", "running") else None,
        })
    for x in (squeue or {}).get("items", []):
        out.append({
            "id": None, "client": "Suwayomi", "kind": "download", "title": x.get("manga") or "",
            "series_id": None, "chapter": x.get("chapter") or "",
            "status": _SUWAYOMI_STATE.get(x.get("state"), "queued"),
            "progress": x.get("progress"), "message": f"try {x['tries']}" if x.get("tries") else "",
            "at": None, "cancel": None,
        })
    active = [r for r in out if r["status"] in ("queued", "running")]
    rest = [r for r in out if r["status"] not in ("queued", "running")]
    return active + rest


def install(env) -> None:
    """Register the helpers with a Jinja environment."""
    env.globals.update(NAV=NAV, SECTION_HOME=SECTION_HOME, nav_section=nav_section, nav_current=nav_current,
                       status_label=status_label, status_class=status_class, progress=progress,
                       progress_kind=progress_kind, chapter_status=chapter_status, event_icon=event_icon,
                       provider=provider, network_line=network_line, index_stats=index_stats, human_size=human_size,
                       row_kind=row_kind, row_language=row_language, stuck_note=stuck_note,
                       verdict_label=verdict_label, skip_note=skip_note, auto_skip_clause=auto_skip_clause,
                       mangadex_line=mangadex_line,
                       SKIP_DISCLAIMER=SKIP_DISCLAIMER)
    env.filters["ranges"] = short_ranges
    env.filters["status_label"] = status_label
    env.filters["status_class"] = status_class
    env.filters["plain"] = plain_description
    env.filters["snippet"] = snippet
    env.filters["human_size"] = human_size
    env.filters["path_segment"] = path_segment
    env.filters["css_url"] = css_url


# -- output safety -------------------------------------------------------------

def path_segment(value) -> str:
    """Percent-encode a value for use as ONE URL path segment ('/', '?', '#'
    and '..' included), e.g. a ref in /lists/exclusions/<ref>/delete. Jinja's
    urlencode leaves '/' alone, which lets 'manual:../../series/5' re-route a form."""
    return urllib.parse.quote(str(value), safe="")


def css_url(url) -> str:
    """A URL safe inside style="...url('...')": http(s) only, and the
    characters that could end the CSS string or the url() percent-encoded.
    HTML escaping alone is not enough there (the browser decodes &#39; before
    the CSS parser sees it). Anything else becomes '' (no image)."""
    u = str(url or "").strip()
    if not re.match(r"(?i)^https?://", u):
        return ""
    return re.sub(r"""['"()\\\s<>]""", lambda m: f"%{ord(m.group()):02X}", u)


# Flash messages travel in the redirect URL (?m=...) so the async forms in
# app.js can read them, but only text this process generated is shown: each
# carries an HMAC with a per-process key. A crafted link (?m=Security update:
# re-enter your password at ...) is ignored instead of shown as a system message.
_FLASH_KEY = secrets.token_bytes(32)


def _flash_sig(name: str, msg: str) -> str:
    return hmac.new(_FLASH_KEY, f"{name}\0{msg}".encode(), hashlib.sha256).hexdigest()[:32]


def flash_query(msg: str, name: str = "m") -> str:
    """'m=<text>&ms=<signature>' for a redirect URL."""
    return f"{name}={urllib.parse.quote(msg)}&{name}s={_flash_sig(name, msg)}"


def flash_from(query_params, name: str = "m") -> str | None:
    """The flash text from a request's query, or None when it is missing or
    was not signed by this process."""
    msg = query_params.get(name)
    if not msg:
        return None
    sig = query_params.get(name + "s") or ""
    if hmac.compare_digest(sig.encode("utf-8", "replace"), _flash_sig(name, msg).encode()):
        return msg
    return None


def human_size(n: int | float | None) -> str:
    """1234567 -> '1.2 MB'; 0 -> '0 B'."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def ref_url(row) -> str | None:
    """Where the metadata came from: the AniList or MangaDex page."""
    if _val(row, "anilist_id"):
        return f"https://anilist.co/manga/{_val(row, 'anilist_id')}"
    if _val(row, "mangadex_id"):
        return f"https://mangadex.org/title/{_val(row, 'mangadex_id')}"
    return None
