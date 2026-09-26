"""Logging: console plus an optional rotating file, level from the CLI or
MANGARR_LOG_LEVEL. Every module logs through logging.getLogger(__name__).

    DEBUG   every API request with timing, every search hit and why it was
            accepted or rejected, every page-count probe
    INFO    what the app decided and did: sources matched, chapters planned,
            downloads, imports, notifications
    WARNING something degraded but handled: a source unreachable, a source
            distrusted, a chapter that failed and stays wanted
    ERROR   an operation did not complete

MANGARR_LOG_JSON=1 writes one JSON object per line (for Loki, Promtail,
Vector ...) instead of the human format.
"""
import json
import logging
import logging.handlers
import os
import sys
import time

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        if record.threadName not in ("MainThread", None):
            d["thread"] = record.threadName
        return json.dumps(d, ensure_ascii=False)


def setup(level: str | None = None, file: str | None = None, console: bool = True,
          json_lines: bool | None = None) -> None:
    level_name = (level or os.environ.get("MANGARR_LOG_LEVEL") or "INFO").upper()
    file = file or os.environ.get("MANGARR_LOG_FILE")
    if json_lines is None:
        json_lines = os.environ.get("MANGARR_LOG_JSON", "").lower() in ("1", "true", "yes")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name, logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = JsonFormatter() if json_lines else logging.Formatter(FORMAT, DATEFMT)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        root.addHandler(ch)
    if file:
        os.makedirs(os.path.dirname(file) or ".", exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(file, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    # third-party noise stays quiet unless we are debugging
    if level_name != "DEBUG":
        for name in ("urllib3", "asyncio", "uvicorn.access"):
            logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger(__name__).debug("logging ready: level=%s file=%s json=%s", level_name, file, json_lines)
