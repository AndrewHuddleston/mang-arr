"""Notifications: Pushover and/or a generic JSON webhook. Both optional;
nothing is sent when neither is configured (Settings page or environment)."""
import json
import logging
import urllib.parse
import urllib.request

from . import settings

log = logging.getLogger(__name__)


def _cfg() -> dict:
    v = settings.all_values()
    return {"token": v["pushover_token"], "user": v["pushover_user"], "webhook": v["webhook_url"]}


def configured() -> bool:
    c = _cfg()
    return bool((c["token"] and c["user"]) or c["webhook"])


def send(title: str, message: str, kind: str = "info", priority: int = 0) -> bool:
    """True when at least one channel accepted it."""
    c = _cfg()
    ok = False
    if c["token"] and c["user"]:
        ok |= _pushover(c, title, message, priority)
    if c["webhook"]:
        ok |= _webhook(c["webhook"], {"title": title, "message": message, "kind": kind})
    if not (c["token"] and c["user"]) and not c["webhook"]:
        log.debug("notification (no channel configured): %s - %s", title, message)
    return ok


def _pushover(c, title, message, priority) -> bool:
    data = urllib.parse.urlencode({
        "token": c["token"], "user": c["user"],
        "title": title[:250], "message": message[:1024], "priority": priority}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(
                "https://api.pushover.net/1/messages.json", data), timeout=20) as r:
            body = json.load(r)
        if body.get("status") != 1:
            log.error("pushover rejected the message: %s", body.get("errors") or body)
            return False
        log.info("pushover sent: %s", title)
        return True
    except Exception as e:
        log.error("pushover failed: %s: %s", type(e).__name__, e)
        return False


def _webhook(url, payload) -> bool:
    try:
        req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status >= 300:
                log.error("webhook %s answered %d", url, r.status)
                return False
        log.info("webhook sent: %s", payload["title"])
        return True
    except Exception as e:
        log.error("webhook %s failed: %s: %s", url, type(e).__name__, e)
        return False
