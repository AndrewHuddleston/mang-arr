"""Pure view helpers for the templates: how chapters are grouped into
Sonarr-style "seasons", how a series status reads, how a description is
made plain. No database, no network - unit-testable on plain rows/dicts.
"""
import html
import re

from .. import library

BLOCK = 20                       # chapters per group when a series has no seasons

STATUS_LABELS = {"RELEASING": "Continuing", "FINISHED": "Ended", "HIATUS": "Hiatus", "CANCELLED": "Cancelled"}
CHAPTER_STATUSES = ("have", "wanted", "failed", "junk", "unavailable", "ignored")
COUNTED = ("have", "wanted", "failed", "unavailable")   # what "listed" means on the index page

# sidebar: (section, icon, [(label, href)]); a section whose first link is
# its own page (Settings, System) has anchors as sub-links
NAV = [
    ("Series", "series", [("Series", "/"), ("Add New", "/add"), ("Library Import", "/import"), ("Lists", "/lists")]),
    ("Activity", "activity", [("Queue", "/activity"), ("History", "/activity/history")]),
    ("Wanted", "wanted", [("Missing", "/wanted")]),
    ("Settings", "settings", [("Sources", "/settings#sources"), ("Scheduling", "/settings#scheduling"),
                              ("Komga", "/settings#komga"), ("Notifications", "/settings#notifications"),
                              ("Security", "/settings#security")]),
    ("System", "system", [("Status", "/system"), ("Tasks", "/system#tasks"), ("Backups", "/system#backups"),
                          ("Logs", "/system/logs")]),
]
SECTION_HOME = {"Settings": "/settings", "System": "/system"}


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
    if path.startswith("/series/"):
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


_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<br\s*/?>|</p>|</div>", re.I)


def plain_description(text: str | None) -> str:
    """AniList descriptions carry <br> and <i>; make them plain text with
    real line breaks, at most one blank line in a row."""
    if not text:
        return ""
    t = _BR.sub("\n", text)
    t = _TAG.sub("", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _val(row, key):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


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
    part of the number, so 12.5 sits with 12 in 'Chapters 1-20'."""
    rows = list(chapters)
    if not rows:
        return []
    named = [c for c in rows if _val(c, "name")]
    seasons = {id(c): library.parse_season(_val(c, "name")) for c in named}
    by_season = bool(named) and all(seasons.values())
    groups: dict = {}                      # sort key -> group; keys sort newest first, 'Unnumbered' last
    if by_season:
        for c in rows:
            s = seasons.get(id(c))
            key = (0, -s[0]) if s else (1, 0)
            g = groups.setdefault(key, {"key": f"season-{s[0]}" if s else "unnumbered",
                                        "name": f"Season {s[0]}" if s else "Unnumbered", "chapters": []})
            g["chapters"].append(c)
    else:
        for c in rows:
            start = _block_start(float(_val(c, "number") or 0))
            g = groups.setdefault((0, -start), {"key": f"block-{start}",
                                               "name": f"Chapters {start}-{start + BLOCK - 1}", "chapters": []})
            g["chapters"].append(c)
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


def install(env) -> None:
    """Register the helpers with a Jinja environment."""
    env.globals.update(NAV=NAV, SECTION_HOME=SECTION_HOME, nav_section=nav_section, nav_current=nav_current,
                       status_label=status_label, status_class=status_class, progress=progress)
    env.filters["status_label"] = status_label
    env.filters["status_class"] = status_class
    env.filters["plain"] = plain_description


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
