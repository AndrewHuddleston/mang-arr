"""Health checks, Sonarr-style: a list of problems and warnings a person
can act on, with a live test of every backend (Suwayomi, Komga, AniList,
MangaDex). Results are cached for a minute so the navigation bar can show
a problem count on every page without hammering the backends."""
import logging
import os
import shutil
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass

from . import config, komga, notify, settings
from .suwayomi import Client, SuwayomiError

log = logging.getLogger(__name__)
CACHE_SECS = 60


@dataclass
class Check:
    level: str        # error | warning | ok
    name: str
    detail: str


_cache: dict = {"at": 0.0, "checks": []}
_lock = threading.Lock()


def _ping(name: str, url: str, timeout: int = 8) -> Check:
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ms = int((time.monotonic() - t0) * 1000)
            if r.status < 400:
                return Check("ok", name, f"reachable ({ms} ms)")
            return Check("warning", name, f"answered HTTP {r.status}")
    except Exception as e:
        return Check("warning", name, f"unreachable: {type(e).__name__}: {e}")


def run(client: Client, force: bool = False) -> list[Check]:
    with _lock:
        if not force and time.monotonic() - _cache["at"] < CACHE_SECS and _cache["checks"]:
            return list(_cache["checks"])
    out: list[Check] = []
    v = settings.all_values()

    # -- Suwayomi (the download engine) --
    try:
        t0 = time.monotonic()
        info = client.gq("{ aboutServer { version } sources { totalCount } }", timeout=8, retries=1)
        sources = client.sources()
        ms = int((time.monotonic() - t0) * 1000)
        version = (info.get("aboutServer") or {}).get("version") or "?"
        out.append(Check("ok", "Suwayomi", f"{version} at {config.SUWAYOMI_URL}, {len(sources)} sources ({ms} ms)"))
        enabled = [s for s in sources if not s.unusable]
        if sources and not enabled:
            out.append(Check("error", "Sources", "every source is disabled in Settings"))
        elif sources and len(enabled) < len(sources):
            out.append(Check("ok", "Sources", f"{len(enabled)} enabled, {len(sources) - len(enabled)} disabled"))
        elif not sources:
            out.append(Check("error", "Sources", "Suwayomi has no English sources installed"))
        slow = [s.name for s in sources if s.throttled and not s.unusable]
        if slow:
            out.append(Check("ok", "Rate-limited sources",
                             f"{', '.join(slow)}: the site limits requests, so chapters only it has download one at "
                             f"a time with pauses and retries (Settings: delay between chapters). Other sources "
                             f"are preferred whenever they list the chapter."))
    except SuwayomiError as e:
        out.append(Check("error", "Suwayomi", f"unreachable at {config.SUWAYOMI_URL}: {e}"))

    # -- Komga (the reader) --
    if not komga.configured():
        out.append(Check("warning", "Komga", "not configured: new chapters appear only at Komga's own scan interval"))
    else:
        ok, msg = komga.test()
        out.append(Check("ok" if ok else "error", "Komga", f"{v['komga_url']}: {msg}"))

    # -- metadata providers --
    out.append(_ping("AniList", config.ANILIST_URL.replace("graphql.anilist.co", "anilist.co")))
    out.append(_ping("MangaDex", config.MANGADEX_URL + "/ping"))

    # -- paths and disk --
    for name, path, need_write in (("Staging", config.STAGING_ROOT, False), ("Library", config.LIBRARY_ROOT, True)):
        if not os.path.isdir(path):
            out.append(Check("error", name, f"path does not exist: {path}"))
        elif need_write and not os.access(path, os.W_OK):
            out.append(Check("error", name, f"not writable: {path}"))
        else:
            out.append(Check("ok", name, path))
    if os.path.isdir(config.STAGING_ROOT) and os.path.isdir(config.LIBRARY_ROOT):
        try:
            if os.stat(config.STAGING_ROOT).st_dev != os.stat(config.LIBRARY_ROOT).st_dev:
                out.append(Check("warning", "Hard links",
                                 "staging and library are on different filesystems: chapters are copied, "
                                 "not linked (twice the disk use)"))
        except OSError:
            pass
        try:
            usage = shutil.disk_usage(config.LIBRARY_ROOT)
            free_gb = usage.free / 1e9
            level = "error" if free_gb < 2 else "warning" if free_gb < 20 else "ok"
            out.append(Check(level, "Disk",
                             f"{free_gb:.0f} GB free of {usage.total / 1e9:.0f} GB on the library volume"))
        except OSError as e:
            out.append(Check("warning", "Disk", f"cannot read usage: {e}"))

    # -- configuration --
    if not notify.configured():
        out.append(Check("warning", "Notifications", "none configured (Settings -> Notifications)"))
    else:
        out.append(Check("ok", "Notifications", ", ".join(notify.CHANNELS[k][0] for k in notify.configured_channels())))
    if not v["auth_user"]:
        out.append(Check("warning", "Security", "no web login set; anyone on the network can use this page"))

    with _lock:
        _cache.update(at=time.monotonic(), checks=list(out))
    for c in out:
        if c.level == "error":
            log.warning("health: %s: %s", c.name, c.detail)
    _alert(out)
    return out


def summary(client: Client) -> dict:
    checks = run(client)
    return {"errors": sum(1 for c in checks if c.level == "error"),
            "warnings": sum(1 for c in checks if c.level == "warning"),
            "checks": [asdict(c) for c in checks]}


def problems(client: Client) -> list[str]:
    return [f"{c.name}: {c.detail}" for c in run(client) if c.level == "error"]


# -- health notifications ------------------------------------------------------
# Alert state is kept apart from the probe results and keyed on the check name
# only (details such as "1 GB free" change from run to run). Every rule is
# about elapsed time, not about how many runs happened, so how often run() is
# called (page views, the nav poller, anyone polling /api/v1/health) does not
# change how often anyone is paged:
#   - a problem pages once it has lasted CONFIRM_SECS with no clean run in
#     between (a single slow answer does not page), and once per incident;
#   - it counts as recovered only after CLEAR_SECS of clean runs (flap damping);
#   - a check never pages more than once per COOLDOWN_SECS.
CONFIRM_SECS = 120
CLEAR_SECS = 600
COOLDOWN_SECS = 6 * 3600

_alerts: dict[str, dict] = {}     # check name -> failing_since, clean_since, active, notified_at (monotonic)
_alert_lock = threading.Lock()


def _alert(out: list[Check], now: float | None = None) -> list[str]:
    """Update the alert state from one run and send a notification for
    problems that just became due. Returns the names paged (for tests)."""
    now = time.monotonic() if now is None else now
    failing = {c.name for c in out if c.level == "error"}
    due: list[str] = []
    with _alert_lock:
        for name in sorted(failing | set(_alerts)):
            a = _alerts.setdefault(name, {"failing_since": None, "clean_since": None, "active": False,
                                          "notified_at": None})
            if name in failing:
                a["clean_since"] = None
                if a["failing_since"] is None:
                    a["failing_since"] = now
                if a["active"] or now - a["failing_since"] < CONFIRM_SECS:
                    continue                          # already paged for this incident, or not confirmed yet
                a["active"] = True
                if a["notified_at"] is not None and now - a["notified_at"] < COOLDOWN_SECS:
                    log.info("health: %s is failing again; not notifying again within %.0f h of the last "
                             "notification", name, COOLDOWN_SECS / 3600)
                    continue
                a["notified_at"] = now
                due.append(name)
            else:
                a["failing_since"] = None
                if a["clean_since"] is None:
                    a["clean_since"] = now
                if a["active"] and now - a["clean_since"] >= CLEAR_SECS:
                    a["active"] = False
                    log.info("health: %s recovered", name)
                if not a["active"] and (a["notified_at"] is None or now - a["notified_at"] >= COOLDOWN_SECS):
                    del _alerts[name]                 # nothing left to remember
    if due:
        log.info("health: notifying about %s", ", ".join(due))
        notify.send("mang-arr: health problem", "\n".join(f"{c.name}: {c.detail}" for c in out
                                                           if c.level == "error"), "health")
    return due
