"""Pure view helpers for the templates: how chapters are grouped into
Sonarr-style "seasons", how a series status reads, how a description is
made plain. No database, no network - unit-testable on plain rows/dicts.
"""
import hashlib
import hmac
import html
import re
import secrets
import urllib.parse

from .. import library, model

BLOCK = 20                       # chapters per group when a series has no seasons

STATUS_LABELS = {"RELEASING": "Continuing", "FINISHED": "Ended", "HIATUS": "Hiatus", "CANCELLED": "Cancelled"}
CHAPTER_STATUSES = ("have", "wanted", "failed", "junk", "unavailable", "ignored")
COUNTED = ("have", "wanted", "failed", "unavailable")   # what "listed" means on the index page

# sidebar: (section, icon, [(label, href)]); a section whose first link is
# its own page (Settings, System) has anchors as sub-links
NAV = [
    ("Series", "series", [("Add New", "/add"), ("Library Import", "/import"), ("Lists", "/lists")]),
    ("Activity", "activity", [("Queue", "/activity"), ("History", "/activity/history")]),
    ("Wanted", "wanted", [("Missing", "/wanted")]),
    ("Settings", "settings", [("Sources", "/settings#sources"), ("Scheduling", "/settings#scheduling"),
                              ("Komga", "/settings#komga"), ("Notifications", "/settings#notifications"),
                              ("Security", "/settings#security")]),
    ("System", "system", [("Status", "/system"), ("Tasks", "/system#tasks"), ("Backups", "/system#backups"),
                          ("Logs", "/system/logs")]),
]
SECTION_HOME = {"Series": "/", "Settings": "/settings", "System": "/system"}


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
    "failed": ("warning", "danger", "Download failed"),
    "deleted": ("delete", "danger", "Series deleted"),
    "review": ("info", "warning", "Needs a decision"),
    "monitor": ("bookmark", "default", "Monitoring changed"),
    "ignore": ("ignore", "default", "Chapter ignored"),
    "unignore": ("bookmark", "default", "Chapter wanted again"),
    "list": ("list", "default", "Import list"),
}


def event_icon(kind: str | None) -> dict:
    icon, ikind, tip = EVENT_ICONS.get(kind or "", ("unknown", "default", kind or "event"))
    return {"icon": icon, "kind": ikind, "tip": tip}


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
                       row_kind=row_kind, row_language=row_language)
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
