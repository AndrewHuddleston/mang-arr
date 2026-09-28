"""The background worker: every REFRESH_HOURS, re-resolve each monitored
series, download what is missing, link it into the library, and notify."""
import logging
import signal
import time

from . import config, core, db, downloader, limits, notify, relink, stuck
from .suwayomi import BREAKER_SECS, Client, SuwayomiError, SuwayomiUnreachable

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
    downloader.retry_leftovers_now(client)      # queue entries an earlier run could not take back out
    outages = 0                     # consecutive series that failed because Suwayomi is not answering
    for n, r in enumerate(rows, 1):
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
            outages = 0
        except SuwayomiError as e:
            log.error("%s: Suwayomi error: %s", r["title"], e)
            summary["errors"].append(f"{r['title']}: {e}")
            try:
                with db.connect() as con:
                    con.execute("UPDATE series SET last_error=? WHERE id=?", (str(e)[:300], r["id"]))
            except Exception as rec:
                log.warning("%s: could not record the error: %s", r["title"], rec)
            if isinstance(e, SuwayomiUnreachable):
                outages += 1
                if outages >= 2:        # still down after a breaker window: one error, not one per series
                    log.error("Suwayomi is not answering; stopping this cycle after %d of %d series", n, len(rows))
                    summary["errors"].append(f"cycle stopped after {n} of {len(rows)} series: Suwayomi is not "
                                             "answering")
                    break
                log.warning("Suwayomi is not answering; waiting %d s, the cycle stops if it still is", BREAKER_SECS)
                limits.pause(BREAKER_SECS, lambda: _stop)
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
    stuck.fetcher.start()               # MangaDex lookups for the chapters series are stuck behind
    interval_hours = limits.clamp("refresh_hours", interval_hours)
    log.info("worker started: refresh every %.1fh, suwayomi at %s", interval_hours, config.SUWAYOMI_URL)
    while not _stop:
        started = time.monotonic()
        try:
            # once after an upgrade that scheduled it; again before each cycle while Suwayomi did not answer it
            relink.run_if_due(client, should_cancel=lambda: _stop)
        except Exception:
            log.exception("library link check failed; it runs again before the next cycle")
        try:
            cycle(client)
        except Exception:
            log.exception("cycle crashed")
        if once:
            break
        interval_hours = limits.setting("refresh_hours")      # clamped: never NaN, inf or 0
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
