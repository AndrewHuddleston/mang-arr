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

send() never blocks its caller: it puts the message on a small bounded queue
that one background thread drains. Every message gets one overall deadline
(DEADLINE) shared by all channels, which run in parallel, and every HTTP call
has a wall-clock limit (see outbound.py), so a dead or trickling endpoint
delays nothing but its own delivery. send_detailed() is the synchronous
form, for the Test buttons, with the same deadline.

Text is cleaned before it goes out: titles become one line (no CR/LF or
other control characters), messages are length-capped, and text from
metadata sites, sources or error messages is escaped for Slack, Discord
and Notifiarr so it cannot ping a whole channel or hide a link.
"""
import atexit
import base64
import collections
import json
import logging
import re
import smtplib
import ssl
import threading
import time
import urllib.parse
from email.message import EmailMessage

from . import config, outbound, settings

log = logging.getLogger(__name__)

EVENTS = {                      # kind -> setting that enables it
    "new": "notify_on_new",
    "added": "notify_on_added",
    "failed": "notify_on_failed",
    "error": "notify_on_failed",
    "health": "notify_on_health",
}
PRIORITY = {"failed": 1, "error": 1, "health": 1}   # louder on services that support it

DEADLINE = 30.0          # seconds for one message, all channels together
REQUEST_TIMEOUT = 10.0   # per socket operation (connect, each read) inside that
QUEUE_MAX = 100          # messages waiting for the background sender; the oldest is dropped beyond this
MAX_TITLE = 250
MAX_MESSAGE = 4000
SMTP_SECURITY = ("starttls", "ssl", "none")

# The deadline of the message being sent on this thread (each channel runs on
# its own thread); channels read it through _deadline().
_local = threading.local()


def _deadline() -> float:
    return getattr(_local, "deadline", None) or time.monotonic() + DEADLINE


def _post(url: str, payload=None, headers: dict | None = None, data: bytes | None = None,
          timeout: float = REQUEST_TIMEOUT) -> int:
    """POST and return the status. Redirects are refused (raised as an
    HTTPError naming the new URL), only http(s) is allowed, and the call
    never runs past the message's deadline."""
    body = data if data is not None else json.dumps(payload).encode()
    h = {"Content-Type": "application/json", "User-Agent": config.USER_AGENT}
    h.update(headers or {})
    status, _ = outbound.fetch(url, body, h, method="POST", timeout=timeout, deadline=_deadline(),
                               max_bytes=64 * 1024, what="the notification URL")
    return status


# -- text cleaning --------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")


def _one_line(s: str, limit: int = MAX_TITLE) -> str:
    """A header-safe title: all whitespace and line breaks become single
    spaces, other control characters go, and it is capped at `limit`."""
    return _CONTROL.sub("", " ".join(str(s).split()))[:limit]


def _clean(s: str, limit: int = MAX_MESSAGE) -> str:
    """A message body: line breaks kept, other control characters removed,
    capped at `limit` characters."""
    s = _CONTROL.sub("", str(s).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " "))
    if len(s) > limit:
        log.info("notification text shortened from %d to %d characters", len(s), limit)
        s = s[:limit - 3] + "..."
    return s


def _slack_escape(s: str) -> str:
    """Slack parses <...> as links and mentions (<!channel>, <@U123>,
    <https://x|text>); escaping & < > is all it needs to show them as text."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_MD_SPECIAL = re.compile(r"([\\*_~`|>#\[\]()<])")


def _discord_escape(s: str) -> str:
    """Neutralise Discord markdown: masked links [text](url), bold, spoilers,
    quotes and headings are shown literally, and @everyone / @here cannot
    ping (a zero-width space after the @; Notifiarr has no allowed_mentions)."""
    s = _MD_SPECIAL.sub(r"\\\1", s)
    return re.sub(r"@(everyone|here)", "@\u200b\\1", s)


# -- channels: each returns None (not configured), or raises on failure -------

def _pushover(v, title, message, kind):
    if not (v["pushover_token"] and v["pushover_user"]):
        return None
    data = urllib.parse.urlencode({"token": v["pushover_token"], "user": v["pushover_user"], "title": title[:250],
                                   "message": message[:1024], "priority": PRIORITY.get(kind, 0)}).encode()
    _, raw = outbound.fetch("https://api.pushover.net/1/messages.json", data,
                            {"User-Agent": config.USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
                            method="POST", timeout=REQUEST_TIMEOUT, deadline=_deadline(), max_bytes=64 * 1024)
    body = json.loads(raw)
    if body.get("status") != 1:
        raise RuntimeError(f"rejected: {body.get('errors') or body}")
    return True


def _discord(v, title, message, kind):
    if not v["discord_webhook"]:
        return None
    color = {"new": 0x27C24C, "added": 0x5D9CEC, "failed": 0xF05050, "error": 0xF05050, "health": 0xFF902B}
    _post(v["discord_webhook"], {"username": "mang-arr", "allowed_mentions": {"parse": []}, "embeds": [
        {"title": _discord_escape(title)[:256], "description": _discord_escape(message)[:4000],
         "color": color.get(kind, 0x9C6BFF)}]})
    return True


def _slack(v, title, message, kind):
    if not v["slack_webhook"]:
        return None
    _post(v["slack_webhook"], {"text": f"*{_slack_escape(title)}*\n{_slack_escape(message)}"[:3900]})
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
    title = _one_line(title)
    if not title.isascii():     # HTTP headers are Latin-1; ntfy decodes RFC 2047 encoded words
        title = "=?UTF-8?B?" + base64.b64encode(title.encode("utf-8")).decode() + "?="
    headers = {"Title": title, "Tags": kind,
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
    # Fail closed: only the three modes the Settings page offers. Anything
    # else ('tls', a typo) used to fall through to a plaintext login.
    security = str(v["smtp_security"] or "starttls").strip().lower()
    if security not in SMTP_SECURITY:
        log.error("email not sent: smtp_security is %r; it must be starttls, ssl or none", v["smtp_security"])
        raise ValueError(f"unknown SMTP security {v['smtp_security']!r}: must be starttls, ssl or none")
    host = str(v["smtp_host"]).strip()
    msg = EmailMessage()
    msg["Subject"] = _one_line(title)
    msg["From"] = v["smtp_from"] or v["smtp_user"] or "mang-arr@localhost"
    msg["To"] = v["smtp_to"]
    msg.set_content(message)
    port = int(v["smtp_port"] or (465 if security == "ssl" else 587))
    ctx = ssl.create_default_context()
    deadline = _deadline()
    timeout = max(0.1, min(REQUEST_TIMEOUT, deadline - time.monotonic()))
    server = smtplib.SMTP_SSL(timeout=timeout, context=ctx) if security == "ssl" else smtplib.SMTP(timeout=timeout)
    server._host = host      # smtplib only sets this (the TLS server name) when given a host in the constructor
    watchdog = outbound.Watchdog(deadline, lambda: server.sock).start()   # a hard stop for the whole exchange
    try:
        server.connect(host, port)
        with server:
            if security == "starttls":
                server.starttls(context=ctx)
            if v["smtp_user"]:
                if security == "none":
                    log.warning("email: logging in to %s without encryption (SMTP security is 'none')", host)
                server.login(v["smtp_user"], v["smtp_password"])
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no complete answer from {host} within the time limit") from e
        raise
    finally:
        watchdog.cancel()
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
                       "text": {"title": _discord_escape(title)[:256], "description": _discord_escape(message)[:4000],
                                "footer": "mang-arr"},
                       "ids": {"channel": channel}}},
          headers={"X-API-Key": v["notifiarr_api_key"]})
    return True


def _apprise(v, title, message, kind):
    if not v["apprise_url"]:
        return None
    ntype = {"failed": "failure", "error": "failure", "health": "warning", "new": "success"}.get(kind, "info")
    _post(v["apprise_url"], {"title": title, "body": message, "type": ntype, "format": "text"})
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
    """Queue a message for every configured channel (or just `only`) and
    return at once; a background thread delivers it (results in the log).
    Events whose notify_on_* setting is off are dropped unless `force`.
    True when it was queued for at least one channel."""
    v = settings.all_values()
    toggle = EVENTS.get(kind)
    if toggle and not force and not v.get(toggle, True):
        log.debug("notification %r dropped: %s is off", title, toggle)
        return False
    if not [k for k in configured_channels(v) if not only or k == only]:
        log.debug("notification (no channel configured): %s - %s", title, message)
        return False
    with _cond:
        if len(_queue) >= QUEUE_MAX:
            dropped = _queue.popleft()
            log.warning("notification queue full (%d waiting): dropped the oldest, %r", QUEUE_MAX, dropped[0])
        _queue.append((title, message, kind, only, force))
        _cond.notify()
    _ensure_sender()
    return True


def send_detailed(title: str, message: str, kind: str = "info", only: str | None = None,
                  force: bool = False) -> dict[str, object]:
    """{channel: True | 'error text'} for every configured channel tried.
    Synchronous (the Test buttons use it) but bounded: channels run in
    parallel and the whole call returns within DEADLINE seconds; a channel
    still going then is reported as timed out."""
    v = settings.all_values()
    toggle = EVENTS.get(kind)
    if toggle and not force and not v.get(toggle, True):
        log.debug("notification %r dropped: %s is off", title, toggle)
        return {}
    title, message = _one_line(title), _clean(message)
    keys = [k for k in CHANNELS if (not only or k == only) and k in configured_channels(v)]
    deadline = time.monotonic() + DEADLINE
    out: dict[str, object] = {}
    lock = threading.Lock()

    def deliver(key: str) -> None:
        _local.deadline = deadline
        name, fn = CHANNELS[key]
        try:
            r = fn(v, title, message, kind)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            with lock:
                if key not in out:
                    out[key] = err
                    log.error("%s notification failed: %s", name, err)
            return
        with lock:
            if r and key not in out:
                out[key] = True
                log.info("%s notification sent: %s", name, title)

    threads = [threading.Thread(target=deliver, args=(k,), name=f"mangarr-notify-{k}", daemon=True) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    with lock:
        for key, t in zip(keys, threads, strict=True):
            if t.is_alive() and key not in out:
                out[key] = f"TimeoutError: no answer within {DEADLINE:.0f} s"
                log.error("%s notification failed: %s", CHANNELS[key][0], out[key])
        result = {k: out[k] for k in keys if k in out}      # in CHANNELS order, whatever finished first
    if not keys:
        log.debug("notification (no channel configured): %s - %s", title, message)
    return result


# -- the background sender --------------------------------------------------------

_queue: collections.deque = collections.deque()
_cond = threading.Condition()
_busy = False
_sender: threading.Thread | None = None


def _ensure_sender() -> None:
    global _sender
    with _cond:
        if _sender is None or not _sender.is_alive():
            _sender = threading.Thread(target=_sender_loop, name="mangarr-notify", daemon=True)
            _sender.start()


def _sender_loop() -> None:
    global _busy
    while True:
        with _cond:
            while not _queue:
                _busy = False
                _cond.notify_all()          # wakes flush()
                _cond.wait()
            item = _queue.popleft()
            _busy = True
        try:
            send_detailed(*item)
        except Exception:                   # never let one message stop the sender
            log.exception("notification %r could not be sent", item[0])


def flush(timeout: float = DEADLINE) -> bool:
    """Wait up to `timeout` seconds for queued messages to go out. True when
    the queue is empty. Called at exit so a one-shot run still notifies."""
    end = time.monotonic() + timeout
    with _cond:
        while _queue or _busy:
            left = end - time.monotonic()
            if left <= 0:
                log.warning("%d notification(s) not sent before shutdown", len(_queue) + int(_busy))
                return False
            _cond.wait(left)
    return True


atexit.register(flush, 10.0)
