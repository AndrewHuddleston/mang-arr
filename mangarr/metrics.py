"""Prometheus metrics at /metrics (optional: needs prometheus-client).

    mangarr_series_total                 tracked series
    mangarr_chapters{status=...}         chapters by status (have/wanted/failed/junk)
    mangarr_downloads_total{source,result}
    mangarr_jobs_total{kind,status}
    mangarr_suwayomi_up                  1 when the API answers
    mangarr_last_refresh_timestamp       unix time of the last completed refresh-all
    mangarr_download_lanes               download lanes of the running pass (0: none)
    mangarr_download_lanes_busy          lanes downloading right now
    mangarr_series_waiting_for_lane      series resolved and waiting for a lane
    mangarr_lane_seconds_total{source}   time lanes spent on each source (rate() near 1: the bottleneck)
    mangarr_download_unstarted_total{source}
                                         chunks Suwayomi did not start within 30 min (its queue busy)
    mangarr_page_fetches_total{source,result}
                                         page requests on page-by-page sources (ok/busy/timeout/gone/error)
    mangarr_page_warmups_total{source,result}
                                         chapters fetched page by page (ok/partial/refused/deadline/no_pages/cancelled)
    mangarr_page_warm_downloads_total{source,result}
                                         downloads after that (ok/rewarm: page cache cleared/failed_after_warm)
    mangarr_page_delay_seconds{source}   current spacing of page requests
"""
import logging

log = logging.getLogger(__name__)

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
    AVAILABLE = True
except ImportError:                       # metrics are optional
    AVAILABLE = False

if AVAILABLE:
    SERIES = Gauge("mangarr_series_total", "tracked series")
    CHAPTERS = Gauge("mangarr_chapters", "chapters by status", ["status"])
    DOWNLOADS = Counter("mangarr_downloads_total", "chapter downloads", ["source", "result"])
    JOBS = Counter("mangarr_jobs_total", "jobs by outcome", ["kind", "status"])
    SUWAYOMI_UP = Gauge("mangarr_suwayomi_up", "Suwayomi API reachable")
    LAST_REFRESH = Gauge("mangarr_last_refresh_timestamp", "last completed refresh-all")
    LANES = Gauge("mangarr_download_lanes", "download lanes of the running pass")
    LANES_BUSY = Gauge("mangarr_download_lanes_busy", "download lanes downloading right now")
    LANE_WAITING = Gauge("mangarr_series_waiting_for_lane", "series waiting for a download lane")
    LANE_SECONDS = Counter("mangarr_lane_seconds_total", "time download lanes spent on each source", ["source"])
    UNSTARTED = Counter("mangarr_download_unstarted_total", "download chunks Suwayomi did not start in time",
                        ["source"])
    PAGE_FETCHES = Counter("mangarr_page_fetches_total", "page requests on page-by-page sources",
                           ["source", "result"])
    PAGE_WARMUPS = Counter("mangarr_page_warmups_total", "chapters fetched page by page", ["source", "result"])
    PAGE_WARM_DOWNLOADS = Counter("mangarr_page_warm_downloads_total", "downloads after a page-by-page fetch",
                                  ["source", "result"])
    PAGE_DELAY = Gauge("mangarr_page_delay_seconds", "spacing of page requests on a page-by-page source",
                       ["source"])


def record_download(source: str, result: str) -> None:
    if AVAILABLE:
        DOWNLOADS.labels(source=source, result=result).inc()


def record_lanes(lanes: int, busy: int, waiting: int) -> None:
    if AVAILABLE:
        LANES.set(lanes)
        LANES_BUSY.set(busy)
        LANE_WAITING.set(waiting)


def record_lane_time(source: str, secs: float) -> None:
    if AVAILABLE and secs > 0:
        LANE_SECONDS.labels(source=source).inc(secs)


def record_unstarted(source: str) -> None:
    if AVAILABLE:
        UNSTARTED.labels(source=source).inc()


def record_page(source: str, result: str) -> None:
    if AVAILABLE:
        PAGE_FETCHES.labels(source=source, result=result).inc()


def record_warm(source: str, result: str) -> None:
    if AVAILABLE:
        PAGE_WARMUPS.labels(source=source, result=result).inc()


def record_warm_download(source: str, result: str) -> None:
    if AVAILABLE:
        PAGE_WARM_DOWNLOADS.labels(source=source, result=result).inc()


def set_page_delay(source: str, secs: float) -> None:
    if AVAILABLE:
        PAGE_DELAY.labels(source=source).set(secs)


def record_job(kind: str, status: str) -> None:
    if AVAILABLE:
        JOBS.labels(kind=kind, status=status).inc()


def record_refresh_done(ts: float) -> None:
    if AVAILABLE:
        LAST_REFRESH.set(ts)


def render(con, suwayomi_ok: bool) -> tuple[bytes, str]:
    """Update the gauges from the database and render the exposition."""
    if not AVAILABLE:
        return b"prometheus-client is not installed\n", "text/plain"
    SERIES.set(con.execute("SELECT COUNT(*) FROM series").fetchone()[0])
    for status in ("have", "wanted", "failed", "junk", "unavailable"):
        CHAPTERS.labels(status=status).set(
            con.execute("SELECT COUNT(*) FROM chapter WHERE status=?", (status,)).fetchone()[0])
    SUWAYOMI_UP.set(1 if suwayomi_ok else 0)
    return generate_latest(), CONTENT_TYPE_LATEST
