"""Notifications: Pushover and/or a generic JSON webhook. Both optional;
nothing is sent when neither is configured."""
import json
import logging
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger(__name__)


def configured() -> bool:
    return bool((config.PUSHOVER_TOKEN and config.PUSHOVER_USER) or config.WEBHOOK_URL)


def send(title: str, message: str, kind: str = "info", priority: int = 0) -> None:
    if config.PUSHOVER_TOKEN and config.PUSHOVER_USER:
        _pushover(title, message, priority)
    if config.WEBHOOK_URL:
        _webhook({"title": title, "message": message, "kind": kind})
    if not configured():
        log.debug("notification (not configured): %s - %s", title, message)


def _pushover(title, message, priority):
    data = urllib.parse.urlencode({
        "token": config.PUSHOVER_TOKEN, "user": config.PUSHOVER_USER,
        "title": title[:250], "message": message[:1024], "priority": priority}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(
                "https://api.pushover.net/1/messages.json", data), timeout=20):
            pass
        log.info("pushover sent: %s", title)
    except Exception as e:
        log.error("pushover failed: %s", e)


def _webhook(payload):
    try:
        req = urllib.request.Request(config.WEBHOOK_URL, json.dumps(payload).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20):
            pass
        log.info("webhook sent: %s", payload["title"])
    except Exception as e:
        log.error("webhook failed: %s", e)
