"""Prometheus metrics at /metrics (optional: needs prometheus-client).

    mangarr_series_total                 tracked series
    mangarr_chapters{status=...}         chapters by status (have/wanted/failed/junk)
    mangarr_downloads_total{source,result}
    mangarr_jobs_total{kind,status}
    mangarr_suwayomi_up                  1 when the API answers
    mangarr_last_refresh_timestamp       unix time of the last completed refresh-all
"""
import logging

log = logging.getLogger(__name__)

try:
    from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest)
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


def record_download(source: str, result: str) -> None:
    if AVAILABLE:
        DOWNLOADS.labels(source=source, result=result).inc()


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
