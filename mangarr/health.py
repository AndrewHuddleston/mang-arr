"""Health checks, Sonarr-style: a list of problems and warnings a person
can act on, with a live test of every backend (Suwayomi, Komga, AniList,
MangaDex). Results are cached for a minute so the navigation bar can show
a problem count on every page without hammering the backends.

Only one run happens at a time (single flight), in a background thread:
callers get the cached result at once when there is one (a stale one
triggers the re-check), otherwise they wait at most DEADLINE seconds, so a
hung backend or a hung disk mount can never pile up request threads. A
run still going after DEADLINE is an error for every caller, so a hang
shows up (and /api/v1/health fails) instead of the last good result being
served forever. A forced run (System page) re-checks at most every
FORCE_MIN_SECS."""
import logging
import os
import shutil
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

from . import config, komga, notify, settings
from .suwayomi import Client, SuwayomiError

log = logging.getLogger(__name__)
CACHE_SECS = 60
FORCE_MIN_SECS = 15      # force=True re-checks at most this often, whoever asks
DEADLINE = 25            # seconds a caller waits for a run; the run itself carries on in the background


@dataclass
class Check:
    level: str        # error | warning | ok
    name: str
    detail: str


_cache: dict = {"at": 0.0, "checks": []}
_lock = threading.Lock()
_running: threading.Event | None = None      # set when the run in progress finishes
_started = 0.0                               # when the run in progress started (monotonic)
_hang_logged: threading.Event | None = None  # the run already reported as hung (log it once, not per caller)


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


def run(client: Client, force: bool = False, wait: float = DEADLINE, note_timeout: bool = True) -> list[Check]:
    """The checks: cached, re-checked by one background run at a time.
    force wants a fresh result (at most FORCE_MIN_SECS old) and waits for it;
    otherwise a stale result is returned at once while the re-check runs.
    With nothing cached, waits up to `wait` s; if the run is still going
    then, returns what is cached plus (note_timeout) an error saying so.
    A run going for longer than DEADLINE is reported as an error to every
    caller (cached or not, note_timeout or not): a hung check must not
    leave monitoring reading the last good result forever."""
    global _running, _started
    with _lock:
        now = time.monotonic()
        have = bool(_cache["checks"])
        hung = _running is not None and now - _started > DEADLINE
        if have and not hung and now - _cache["at"] < (FORCE_MIN_SECS if force else CACHE_SECS):
            return list(_cache["checks"])
        done = _running
        if done is None:                     # single flight: start the one run
            done = _running = threading.Event()
            _started = now
            threading.Thread(target=_background, args=(client, done), name="mangarr-health", daemon=True).start()
    finished = done.is_set() if (have and not force) else done.wait(wait)   # stale while revalidating: no wait
    with _lock:
        checks = list(_cache["checks"])
        running_for = time.monotonic() - _started if _running is done and not done.is_set() else 0.0
    if finished or running_for <= 0:
        return checks
    if running_for > DEADLINE or (note_timeout and not (have and not force)):
        _note_hang(done, running_for)
        checks.append(Check("error", "Health checks", f"not finished after {running_for:.1f} s: a backend or a disk "
                                                      "mount is not answering (the check carries on in the "
                                                      "background)"))
    return checks


def _note_hang(done: threading.Event, secs: float) -> None:
    """Log a slow or hung run once per run, not once per caller."""
    global _hang_logged
    with _lock:
        if _hang_logged is done:
            return
        _hang_logged = done
    log.warning("health: checks still running after %.1f s: a backend or a disk mount is not answering", secs)


def _background(client: Client, done: threading.Event) -> None:
    global _running
    try:
        _compute(client)
    except Exception as e:                   # never leave callers without an answer
        log.exception("health: check run failed")
        with _lock:
            _cache.update(at=time.monotonic(), checks=[Check("error", "Health checks", f"{type(e).__name__}: {e}")])
    finally:
        with _lock:
            _running = None
        done.set()


def _no_userinfo(url: str) -> str:
    """A URL for a message: without user:password@."""
    try:
        u = urllib.parse.urlsplit(url)
        if u.username is None and u.password is None:
            return url
        host = u.hostname or ""
        if u.port:
            host = f"{host}:{u.port}"
        return urllib.parse.urlunsplit((u.scheme, host, u.path, u.query, u.fragment))
    except ValueError:
        return "(unparseable URL)"


def _compute(client: Client) -> list[Check]:
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
        out.append(Check("ok" if ok else "error", "Komga", f"{_no_userinfo(v['komga_url'])}: {msg}"))

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
    elif not v["auth_password"]:
        out.append(Check("error", "Security", "a login username is set without a password: nobody can sign in "
                                              "with a password (set one, or clear the username)"))

    with _lock:
        _cache.update(at=time.monotonic(), checks=list(out))
        errors = {f"{c.name}: {c.detail.split(':')[0]}" for c in out if c.level == "error"}
        new = errors - _cache.get("errors", set())
        _cache["errors"] = errors
    for c in out:
        if c.level == "error":
            log.warning("health: %s: %s", c.name, c.detail)
    if new:
        notify.send("mang-arr: health problem", "\n".join(f"{c.name}: {c.detail}" for c in out
                                                           if c.level == "error"), "health")
    return out


def summary(client: Client, wait: float = 5) -> dict:
    """Counts for the navigation bar and the status poller: never waits
    long (a first run that is slow shows up on the next poll)."""
    checks = run(client, wait=wait, note_timeout=False)
    return {"errors": sum(1 for c in checks if c.level == "error"),
            "warnings": sum(1 for c in checks if c.level == "warning"),
            "checks": [asdict(c) for c in checks]}


def problems(client: Client) -> list[str]:
    return [f"{c.name}: {c.detail}" for c in run(client) if c.level == "error"]
