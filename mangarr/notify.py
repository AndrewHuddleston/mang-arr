"""Notifications to the services people actually use. Every channel is
optional and independent: a message goes to each configured one, and a
failure on one never stops the others.

    Pushover   token + user key
    Discord    channel webhook URL
    Slack      incoming-webhook URL
    Telegram   bot token + chat id
    ntfy       topic URL (ntfy.sh or self-hosted) + optional access token
    Gotify     server URL + application token
    Email      SMTP host/port/user/password/from/to, STARTTLS / SSL / plain
    Notifiarr  API key + Discord channel id (passthrough integration)
    Apprise    an Apprise API notify URL (covers ~100 more services)
    Webhook    any URL; POSTs {title, message, kind} as JSON

Which events notify is chosen in Settings (notify_on_*): new chapters,
series added, failed downloads, health problems. Tests always send.
"""
import json
import logging
import smtplib
import ssl
import urllib.parse
import urllib.request
from email.message import EmailMessage

from . import config, settings

log = logging.getLogger(__name__)

EVENTS = {                      # kind -> setting that enables it
    "new": "notify_on_new",
    "added": "notify_on_added",
    "failed": "notify_on_failed",
    "error": "notify_on_failed",
    "health": "notify_on_health",
}
PRIORITY = {"failed": 1, "error": 1, "health": 1}   # louder on services that support it


def _post(url: str, payload=None, headers: dict | None = None, data: bytes | None = None, timeout: int = 20) -> int:
    body = data if data is not None else json.dumps(payload).encode()
    h = {"Content-Type": "application/json", "User-Agent": config.USER_AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, body, h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


# -- channels: each returns None (not configured), or raises on failure -------

def _pushover(v, title, message, kind):
    if not (v["pushover_token"] and v["pushover_user"]):
        return None
    data = urllib.parse.urlencode({"token": v["pushover_token"], "user": v["pushover_user"], "title": title[:250],
                                   "message": message[:1024], "priority": PRIORITY.get(kind, 0)}).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data)
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.load(r)
    if body.get("status") != 1:
        raise RuntimeError(f"rejected: {body.get('errors') or body}")
    return True


def _discord(v, title, message, kind):
    if not v["discord_webhook"]:
        return None
    color = {"new": 0x27C24C, "added": 0x5D9CEC, "failed": 0xF05050, "error": 0xF05050, "health": 0xFF902B}
    _post(v["discord_webhook"], {"username": "mang-arr", "embeds": [
        {"title": title[:256], "description": message[:4000], "color": color.get(kind, 0x9C6BFF)}]})
    return True


def _slack(v, title, message, kind):
    if not v["slack_webhook"]:
        return None
    _post(v["slack_webhook"], {"text": f"*{title}*\n{message}"[:3900]})
    return True


def _telegram(v, title, message, kind):
    if not (v["telegram_token"] and v["telegram_chat_id"]):
        return None
    url = f"https://api.telegram.org/bot{v['telegram_token']}/sendMessage"
    _post(url, {"chat_id": v["telegram_chat_id"], "text": f"{title}\n\n{message}"[:4000],
                "disable_web_page_preview": True})
    return True


def _ntfy(v, title, message, kind):
    if not v["ntfy_url"]:
        return None
    headers = {"Title": title[:250].encode("ascii", "replace").decode(), "Tags": kind,
               "Priority": "high" if PRIORITY.get(kind) else "default", "Content-Type": "text/plain; charset=utf-8"}
    if v["ntfy_token"]:
        headers["Authorization"] = f"Bearer {v['ntfy_token']}"
    _post(v["ntfy_url"], headers=headers, data=message.encode("utf-8"))
    return True


def _gotify(v, title, message, kind):
    if not (v["gotify_url"] and v["gotify_token"]):
        return None
    url = v["gotify_url"].rstrip("/") + "/message"
    _post(url, {"title": title, "message": message, "priority": 8 if PRIORITY.get(kind) else 5},
          headers={"X-Gotify-Key": v["gotify_token"]})
    return True


def _email(v, title, message, kind):
    if not (v["smtp_host"] and v["smtp_to"]):
        return None
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = v["smtp_from"] or v["smtp_user"] or "mang-arr@localhost"
    msg["To"] = v["smtp_to"]
    msg.set_content(message)
    port = int(v["smtp_port"] or (465 if v["smtp_security"] == "ssl" else 587))
    ctx = ssl.create_default_context()
    if v["smtp_security"] == "ssl":
        server = smtplib.SMTP_SSL(v["smtp_host"], port, timeout=30, context=ctx)
    else:
        server = smtplib.SMTP(v["smtp_host"], port, timeout=30)
    with server:
        if v["smtp_security"] == "starttls":
            server.starttls(context=ctx)
        if v["smtp_user"]:
            server.login(v["smtp_user"], v["smtp_password"])
        server.send_message(msg)
    return True


def _notifiarr(v, title, message, kind):
    if not (v["notifiarr_api_key"] and v["notifiarr_channel"]):
        return None
    color = {"new": "27C24C", "added": "5D9CEC", "failed": "F05050", "error": "F05050", "health": "FF902B"}
    try:
        channel = int(v["notifiarr_channel"])
    except ValueError as e:
        raise ValueError("the Discord channel id must be a number") from e
    _post("https://notifiarr.com/api/v1/notification/passthrough",
          {"notification": {"update": False, "name": "mang-arr", "event": kind},
           "discord": {"color": color.get(kind, "9C6BFF"),
                       "text": {"title": title[:256], "description": message[:4000], "footer": "mang-arr"},
                       "ids": {"channel": channel}}},
          headers={"X-API-Key": v["notifiarr_api_key"]})
    return True


def _apprise(v, title, message, kind):
    if not v["apprise_url"]:
        return None
    ntype = {"failed": "failure", "error": "failure", "health": "warning", "new": "success"}.get(kind, "info")
    _post(v["apprise_url"], {"title": title, "body": message, "type": ntype})
    return True


def _webhook(v, title, message, kind):
    if not v["webhook_url"]:
        return None
    _post(v["webhook_url"], {"title": title, "message": message, "kind": kind})
    return True


CHANNELS = {
    "pushover": ("Pushover", _pushover),
    "discord": ("Discord", _discord),
    "slack": ("Slack", _slack),
    "telegram": ("Telegram", _telegram),
    "ntfy": ("ntfy", _ntfy),
    "gotify": ("Gotify", _gotify),
    "email": ("Email", _email),
    "notifiarr": ("Notifiarr", _notifiarr),
    "apprise": ("Apprise", _apprise),
    "webhook": ("Webhook", _webhook),
}


def configured_channels(values: dict | None = None) -> list[str]:
    v = values or settings.all_values()
    required = {"pushover": ("pushover_token", "pushover_user"), "discord": ("discord_webhook",),
                "slack": ("slack_webhook",), "telegram": ("telegram_token", "telegram_chat_id"),
                "ntfy": ("ntfy_url",), "gotify": ("gotify_url", "gotify_token"), "email": ("smtp_host", "smtp_to"),
                "notifiarr": ("notifiarr_api_key", "notifiarr_channel"), "apprise": ("apprise_url",),
                "webhook": ("webhook_url",)}
    return [k for k, keys in required.items() if all(v.get(x) for x in keys)]


def configured() -> bool:
    return bool(configured_channels())


def send(title: str, message: str, kind: str = "info", priority: int = 0, only: str | None = None,
         force: bool = False) -> bool:
    """Send to every configured channel (or just `only`). Events whose
    notify_on_* setting is off are dropped unless `force` (tests). True when
    at least one channel accepted it."""
    return any(r is True for r in send_detailed(title, message, kind, only, force).values())


def send_detailed(title: str, message: str, kind: str = "info", only: str | None = None,
                  force: bool = False) -> dict[str, object]:
    """{channel: True | 'error text'} for every configured channel tried."""
    v = settings.all_values()
    toggle = EVENTS.get(kind)
    if toggle and not force and not v.get(toggle, True):
        log.debug("notification %r dropped: %s is off", title, toggle)
        return {}
    out: dict[str, object] = {}
    for key, (name, fn) in CHANNELS.items():
        if only and key != only:
            continue
        try:
            r = fn(v, title, message, kind)
        except Exception as e:
            out[key] = f"{type(e).__name__}: {e}"
            log.error("%s notification failed: %s", name, out[key])
            continue
        if r:
            out[key] = True
            log.info("%s notification sent: %s", name, title)
    if not out:
        log.debug("notification (no channel configured): %s - %s", title, message)
    return out
