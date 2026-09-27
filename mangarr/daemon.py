"""The background worker: every REFRESH_HOURS, re-resolve each monitored
series, download what is missing, link it into the library, and notify."""
import logging
import signal
import time

from . import config, core, db, notify, settings
from .suwayomi import Client, SuwayomiError

log = logging.getLogger(__name__)

_stop = False


def _handle_stop(signum, frame):
    global _stop
    log.info("signal %d received, finishing the current series then stopping", signum)
    _stop = True


def cycle(client: Client) -> dict:
    """One pass over every monitored series. Returns a summary."""
    summary = {"series": 0, "downloaded": 0, "failed": 0, "imported": 0, "errors": [], "new": []}
    with db.connect() as con:
        rows = [r for r in db.series_rows(con) if r["monitored"]]
    log.info("cycle start: %d monitored series", len(rows))
    for r in rows:
        if _stop:
            break
        summary["series"] += 1
        try:
            with db.connect() as con:
                out = core.refresh_series(con, client, r["id"], download=True)
            summary["downloaded"] += out.downloaded
            summary["failed"] += out.failed
            summary["imported"] += out.imported
            if out.imported:
                summary["new"].append(f"{r['title']} (+{out.imported})")
        except SuwayomiError as e:
            log.error("%s: Suwayomi error: %s", r["title"], e)
            summary["errors"].append(f"{r['title']}: {e}")
            with db.connect() as con:
                con.execute("UPDATE series SET last_error=? WHERE id=?", (str(e)[:300], r["id"]))
        except Exception as e:                      # keep the loop alive
            log.exception("%s: unexpected error", r["title"])
            summary["errors"].append(f"{r['title']}: {e}")
    log.info("cycle done: %d series, %d downloaded, %d failed, %d imported, %d errors",
             summary["series"], summary["downloaded"], summary["failed"], summary["imported"],
             len(summary["errors"]))
    if summary["new"]:
        notify.send("mang-arr: new chapters",
                    "\n".join(summary["new"][:20]) + ("\n..." if len(summary["new"]) > 20 else ""), "new")
    if summary["errors"]:
        notify.send("mang-arr: errors", "\n".join(summary["errors"][:10]), "error", priority=0)
    return summary


def run(interval_hours: float = config.REFRESH_HOURS, once: bool = False) -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    client = Client()
    log.info("worker started: refresh every %.1fh, suwayomi at %s", interval_hours, config.SUWAYOMI_URL)
    while not _stop:
        started = time.monotonic()
        try:
            cycle(client)
        except Exception:
            log.exception("cycle crashed")
        if once:
            break
        try:
            interval_hours = float(settings.get("refresh_hours")) or interval_hours
        except Exception as e:
            log.debug("could not read refresh_hours: %s", e)
        sleep_for = max(60.0, interval_hours * 3600 - (time.monotonic() - started))
        log.info("next cycle in %.0f min", sleep_for / 60)
        for _ in range(int(sleep_for)):
            if _stop:
                break
            time.sleep(1)
    # Deliver what the last cycle queued while threads can still be started
    # (Python 3.12+ refuses new threads during interpreter shutdown, when the
    # atexit fallback would run), so `daemon --once` still notifies.
    notify.flush()
    log.info("worker stopped")
