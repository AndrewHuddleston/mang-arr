"""Notification channels: each builds the right request; failures are isolated; event toggles apply;
sending never blocks the caller; text is cleaned and escaped; SMTP security fails closed; health alerts
are damped. No real network: _post and smtplib are replaced."""
import base64
import email.header
import signal
import smtplib
import threading
import time
import unittest
from unittest import mock

from mangarr import daemon, health, notify, settings


def values(**kw):
    v = dict(settings.DEFAULTS)
    for k in list(v):
        if isinstance(v[k], str):
            v[k] = ""
    v.update(kw)
    return v


class NotifyTest(unittest.TestCase):
    def run_send(self, v, kind="new", force=False):
        calls = []

        def fake_post(url, payload=None, headers=None, data=None, timeout=20):
            calls.append({"url": url, "payload": payload, "headers": headers or {}, "data": data})
            return 200
        with mock.patch.object(notify.settings, "all_values", lambda: v), \
             mock.patch.object(notify, "_post", fake_post):
            res = notify.send_detailed("Title", "Body", kind, force=force)
        return res, calls

    def test_nothing_configured(self):
        res, calls = self.run_send(values())
        self.assertEqual((res, calls), ({}, []))

    def test_each_webhook_style_channel(self):
        v = values(discord_webhook="https://discord/x", slack_webhook="https://slack/x", ntfy_url="https://ntfy.sh/t",
                   ntfy_token="tok", gotify_url="http://gotify/", gotify_token="g", apprise_url="http://apprise/notify/k",
                   webhook_url="http://hook", telegram_token="T", telegram_chat_id="42",
                   notifiarr_api_key="N", notifiarr_channel="123")
        res, calls = self.run_send(v)
        self.assertEqual(set(res), {"discord", "slack", "ntfy", "gotify", "apprise", "webhook", "telegram", "notifiarr"})
        by = {c["url"]: c for c in calls}
        self.assertEqual(by["https://discord/x"]["payload"]["embeds"][0]["title"], "Title")
        self.assertEqual(by["https://ntfy.sh/t"]["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(by["https://ntfy.sh/t"]["data"], b"Body")
        self.assertEqual(by["http://gotify/message"]["headers"]["X-Gotify-Key"], "g")
        self.assertEqual(by["https://api.telegram.org/botT/sendMessage"]["payload"]["chat_id"], "42")
        n = by["https://notifiarr.com/api/v1/notification/passthrough"]
        self.assertEqual((n["headers"]["X-API-Key"], n["payload"]["discord"]["ids"]["channel"]), ("N", 123))
        self.assertEqual(by["http://apprise/notify/k"]["payload"]["type"], "success")

    def test_failure_on_one_channel_does_not_stop_others(self):
        v = values(discord_webhook="https://discord/x", webhook_url="http://hook")

        def fake_post(url, payload=None, headers=None, data=None, timeout=20):
            if "discord" in url:
                raise OSError("down")
            return 200
        with mock.patch.object(notify.settings, "all_values", lambda: v), mock.patch.object(notify, "_post", fake_post):
            res = notify.send_detailed("T", "B", "new")
        self.assertIn("down", res["discord"])
        self.assertIs(res["webhook"], True)

    def test_event_toggles(self):
        v = values(webhook_url="http://hook", notify_on_added=False)
        self.assertEqual(self.run_send(v, "added")[0], {})
        self.assertEqual(self.run_send(v, "added", force=True)[0], {"webhook": True})
        self.assertEqual(self.run_send(v, "new")[0], {"webhook": True})

    def test_configured_channels(self):
        v = values(telegram_token="T")                           # chat id missing: not configured
        self.assertEqual(notify.configured_channels(v), [])
        v = values(telegram_token="T", telegram_chat_id="1", smtp_host="h", smtp_to="a@b")
        self.assertEqual(notify.configured_channels(v), ["telegram", "email"])


    def test_results_follow_channel_order(self):
        v = values(webhook_url="http://hook", discord_webhook="https://discord/x", slack_webhook="https://slack/x")
        res, _ = self.run_send(v)
        self.assertEqual(list(res), ["discord", "slack", "webhook"])


class DeliveryTest(unittest.TestCase):
    """send() queues and returns; channels run in parallel under one overall deadline."""

    def setUp(self):
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def patches(self, v, post):
        return mock.patch.object(notify.settings, "all_values", lambda: v), mock.patch.object(notify, "_post", post)

    def test_send_returns_at_once_and_delivers_in_background(self):
        v = values(webhook_url="http://hook")
        got = []

        def slow_post(url, payload=None, headers=None, data=None, timeout=20):
            self.release.wait(5)
            got.append(payload["title"])
            return 200
        p1, p2 = self.patches(v, slow_post)
        with p1, p2:
            t0 = time.monotonic()
            self.assertTrue(notify.send("T", "B", "new"))
            self.assertLess(time.monotonic() - t0, 0.5)       # the channel is still blocked
            self.assertEqual(got, [])
            self.release.set()
            self.assertTrue(notify.flush(5))
        self.assertEqual(got, ["T"])

    def test_send_skips_queue_when_nothing_to_do(self):
        with mock.patch.object(notify.settings, "all_values", lambda: values()):
            self.assertFalse(notify.send("T", "B", "new"))
        with mock.patch.object(notify.settings, "all_values", lambda: values(webhook_url="http://h",
                                                                               notify_on_added=False)):
            self.assertFalse(notify.send("T", "B", "added"))

    def test_overall_deadline_and_parallel_channels(self):
        v = values(webhook_url="http://hook", discord_webhook="https://discord/x", slack_webhook="https://slack/x")

        def post(url, payload=None, headers=None, data=None, timeout=20):
            if "hook" in url and "discord" not in url:
                self.release.wait(10)                          # a black-holed endpoint
            else:
                time.sleep(0.3)
            return 200
        p1, p2 = self.patches(v, post)
        with p1, p2, mock.patch.object(notify, "DEADLINE", 1.0), self.assertLogs("mangarr.notify", "ERROR"):
            t0 = time.monotonic()
            res = notify.send_detailed("T", "B", "new")
            took = time.monotonic() - t0
        self.assertLess(took, 2.0)
        self.assertIs(res["discord"], True)
        self.assertIs(res["slack"], True)
        self.assertIn("TimeoutError", res["webhook"])

    def test_queue_is_bounded_and_drops_oldest(self):
        v = values(webhook_url="http://hook")
        sent = []

        def post(url, payload=None, headers=None, data=None, timeout=20):
            self.release.wait(5)
            sent.append(payload["title"])
            return 200
        p1, p2 = self.patches(v, post)
        with p1, p2, mock.patch.object(notify, "QUEUE_MAX", 3):
            notify.send("first", "B", "new")
            deadline = time.monotonic() + 2
            while notify._queue and time.monotonic() < deadline:   # the sender picked "first" up and is blocked
                time.sleep(0.01)
            with self.assertLogs("mangarr.notify", "WARNING") as logs:
                for i in range(5):
                    notify.send(f"m{i}", "B", "new")
            self.assertIn("queue full", logs.output[0])
            self.release.set()
            self.assertTrue(notify.flush(5))
        self.assertEqual(sent, ["first", "m2", "m3", "m4"])

    def test_daemon_once_delivers_before_returning(self):
        # `mangarr daemon --once` must not leave delivery to atexit: from Python 3.12 no thread can be
        # started at interpreter shutdown, and the sender needs threads.
        v = values(webhook_url="http://hook")
        got = []

        def slow_post(url, payload=None, headers=None, data=None, timeout=20):
            time.sleep(0.3)
            got.append(payload["title"])
            return 200
        for sig in (signal.SIGTERM, signal.SIGINT):                    # daemon.run installs its own handlers
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        p1, p2 = self.patches(v, slow_post)
        with p1, p2, mock.patch.object(daemon, "Client"), \
             mock.patch.object(daemon, "cycle", lambda c: notify.send("mang-arr: new chapters", "X (+1)", "new")):
            daemon.run(once=True)
            self.assertEqual(got, ["mang-arr: new chapters"])

    def test_per_operation_timeouts_leave_room_for_slow_endpoints(self):
        seen = {}

        def fetch(url, data=None, headers=None, method=None, timeout=0, deadline=None, **kw):
            seen["timeout"] = timeout
            return 200, b""
        with mock.patch.object(notify.settings, "all_values", lambda: values(webhook_url="http://hook")), \
             mock.patch.object(notify.outbound, "fetch", fetch):
            self.assertIs(notify.send_detailed("T", "B", "test", force=True)["webhook"], True)
        self.assertGreaterEqual(seen["timeout"], 20)              # an Apprise fan-out may take a while to answer


class TextTest(unittest.TestCase):
    """Titles are one line, messages capped, markup from untrusted text neutralised."""

    def capture(self, v, title, message, kind="added"):
        calls = []

        def fake_post(url, payload=None, headers=None, data=None, timeout=20):
            calls.append({"url": url, "payload": payload, "headers": headers or {}, "data": data})
            return 200
        with mock.patch.object(notify.settings, "all_values", lambda: v), mock.patch.object(notify, "_post", fake_post):
            res = notify.send_detailed(title, message, kind, force=True)
        return res, {c["url"]: c for c in calls}

    def test_ntfy_title_one_line_and_utf8(self):
        res, by = self.capture(values(ntfy_url="https://ntfy.sh/t"), "Added: Pokémon\r\nX-Evil: 1\u2028ポケモン", "Body")
        title = by["https://ntfy.sh/t"]["headers"]["Title"]
        self.assertIs(res["ntfy"], True)
        self.assertTrue(title.isascii())
        self.assertNotIn("\n", title)
        self.assertNotIn("\r", title)
        self.assertTrue(title.startswith("=?UTF-8?B?"))
        self.assertEqual(base64.b64decode(title[10:-2]).decode(), "Added: Pokémon X-Evil: 1 ポケモン")
        _, by = self.capture(values(ntfy_url="https://ntfy.sh/t"), "Added: Foo", "Body")
        self.assertEqual(by["https://ntfy.sh/t"]["headers"]["Title"], "Added: Foo")     # plain ASCII stays readable

    def test_message_length_capped(self):
        _, by = self.capture(values(webhook_url="http://hook"), "T" * 1000, "x" * 20000)
        p = by["http://hook"]["payload"]
        self.assertEqual((len(p["title"]), len(p["message"])), (notify.MAX_TITLE, notify.MAX_MESSAGE))

    def test_slack_mentions_and_links_escaped(self):
        evil = "Solo <!channel> <https://evil.example/login|read now> & <@U024BE7LH>"
        _, by = self.capture(values(slack_webhook="https://slack/x"), f"Added: {evil}", f"{evil}: <!here>")
        text = by["https://slack/x"]["payload"]["text"]
        self.assertNotIn("<", text)
        self.assertNotIn(">", text)
        self.assertIn("&lt;!channel&gt;", text)
        self.assertIn("&amp;", text)

    def test_discord_and_notifiarr_markdown_neutralised(self):
        evil = "Solo [Chapter 201](https://evil.example/fix) @everyone **x**"
        v = values(discord_webhook="https://discord/x", notifiarr_api_key="N", notifiarr_channel="1")
        _, by = self.capture(v, f"Added: {evil}", evil)
        d = by["https://discord/x"]["payload"]
        self.assertEqual(d["allowed_mentions"], {"parse": []})
        n = by["https://notifiarr.com/api/v1/notification/passthrough"]["payload"]["discord"]["text"]
        for text in (d["embeds"][0]["description"], d["embeds"][0]["title"], n["description"], n["title"]):
            self.assertNotIn("[Chapter 201](", text)
            self.assertIn("\\[Chapter 201\\]\\(https://evil.example/fix\\)", text)
            self.assertNotIn("@everyone", text)
            self.assertIn("\\*\\*x\\*\\*", text)


class FakeSMTP:
    instances: list = []
    greeting = 220

    def __init__(self, *a, **kw):
        self.log = [("init", a, kw)]
        self.sock = None
        FakeSMTP.instances.append(self)

    def connect(self, host, port):
        self.log.append(("connect", host, port))
        return self.greeting, b"mail.example ESMTP"

    def starttls(self, context=None):
        self.log.append(("starttls",))

    def login(self, user, password):
        self.log.append(("login", user))

    def send_message(self, msg):
        self.log.append(("send", msg))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.log.append(("quit",))


class EmailTest(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances = []
        FakeSMTP.greeting = 220

    def send(self, security, title="T"):
        v = values(smtp_host="mail.example", smtp_to="a@b", smtp_user="me", smtp_password="pw", smtp_security=security)
        with mock.patch.object(notify.settings, "all_values", lambda: v), \
             mock.patch.object(notify.smtplib, "SMTP", FakeSMTP), mock.patch.object(notify.smtplib, "SMTP_SSL", FakeSMTP):
            return notify.send_detailed(title, "Body", "test", only="email", force=True)

    def test_unknown_security_fails_closed(self):
        for bad in ("tls", "plain", "start-tls"):
            with self.subTest(bad=bad), self.assertLogs("mangarr.notify", "ERROR"):
                res = self.send(bad)
                self.assertIn("unknown SMTP security", res["email"])
        self.assertEqual(FakeSMTP.instances, [])                          # never connected, never logged in

    def test_known_modes(self):
        self.assertIs(self.send("STARTTLS")["email"], True)              # case does not matter
        steps = [s[0] for s in FakeSMTP.instances[-1].log]
        self.assertEqual(steps, ["init", "connect", "starttls", "login", "send", "quit"])
        self.assertIs(self.send("ssl")["email"], True)
        self.assertNotIn("starttls", [s[0] for s in FakeSMTP.instances[-1].log])
        self.assertEqual(FakeSMTP.instances[-1].log[1], ("connect", "mail.example", 465))
        with self.assertLogs("mangarr.notify", "WARNING") as logs:       # 'none' works, but says so
            self.assertIs(self.send("none")["email"], True)
        self.assertTrue(any("without encryption" in line for line in logs.output))

    def test_rejecting_greeting_is_a_connect_error(self):
        FakeSMTP.greeting = 554
        with self.assertLogs("mangarr.notify", "ERROR"):
            res = self.send("starttls")
        self.assertIn("SMTPConnectError", res["email"])
        self.assertIn("554", res["email"])
        steps = [s[0] for s in FakeSMTP.instances[-1].log]
        self.assertEqual(steps, ["init", "connect", "quit"])            # no STARTTLS, no login, nothing sent

    def test_smtp_timeout_allows_a_greeting_delay(self):
        self.assertIs(self.send("starttls")["email"], True)
        self.assertGreater(FakeSMTP.instances[-1].log[0][2]["timeout"], 20)
        self.assertTrue(issubclass(smtplib.SMTPConnectError, smtplib.SMTPException))

    def test_subject_one_line(self):
        self.assertIs(self.send("starttls", "Added: Foo\r\nBcc: x@evil\x0bBar ポケモン")["email"], True)
        msg = [s for s in FakeSMTP.instances[-1].log if s[0] == "send"][0][1]
        subject = str(email.header.make_header(email.header.decode_header(msg["Subject"])))
        self.assertEqual(subject, "Added: Foo Bcc: x@evil Bar ポケモン")
        self.assertIsNone(msg["Bcc"])


class HealthAlertTest(unittest.TestCase):
    """Alerts key on the check name, need CONFIRM_SECS of failure, clear after CLEAR_SECS clean, and never
    re-page within COOLDOWN_SECS, however often health runs."""

    def setUp(self):
        health._alerts.clear()
        self.addCleanup(health._alerts.clear)
        self.sent = []
        p = mock.patch.object(health.notify, "send", lambda *a, **k: self.sent.append(a) or True)
        p.start()
        self.addCleanup(p.stop)

    def run_at(self, t, *errors):
        out = [health.Check("error", name, detail) for name, detail in errors] or [health.Check("ok", "Suwayomi", "ok")]
        return health._alert(out, now=t)

    def test_flapping_backend_pages_once(self):
        down = ("Suwayomi", "unreachable at http://suwayomi: timed out")
        paged = []
        for i in range(200):                                    # a sample every 5 s, alternating, for 1000 s
            paged += self.run_at(1000 + i * 5, *([down] if i % 2 else []))
        self.assertEqual(paged, [])                             # never failed CONFIRM_SECS in a row
        t = 5000
        for i in range(10):                                     # now it stays down for 150 s
            paged += self.run_at(t + i * 15, down)
        self.assertEqual(paged, ["Suwayomi"])
        for i in range(2000):                                   # then flaps for hours, sampled every second
            paged += self.run_at(t + 200 + i, *([down] if (i // 150) % 2 else []))
        self.assertEqual(paged, ["Suwayomi"])
        self.assertEqual(len(self.sent), 1)

    def test_changing_detail_is_one_problem(self):
        paged = []
        for i, gb in enumerate([1, 2, 1, 0, 2, 1] * 10):
            paged += self.run_at(i * 60, ("Disk", f"{gb} GB free of 4000 GB on the library volume"))
        self.assertEqual(paged, ["Disk"])

    def test_recovered_problem_pages_again_after_cooldown(self):
        down = ("Komga", "http://komga: HTTP 500")
        paged = []
        for t in (0, 60, 130):
            paged += self.run_at(t, down)
        self.assertEqual(paged, ["Komga"])
        for t in range(200, 200 + health.CLEAR_SECS + 120, 60):   # recovered
            paged += self.run_at(t)
        base = 2000                                              # fails again, within the cooldown: logged only
        paged += self.run_at(base, down) + self.run_at(base + 130, down)
        self.assertEqual(paged, ["Komga"])
        paged += self.run_at(base + 200)
        later = health.COOLDOWN_SECS + 5000                     # recovered, and much later a new outage
        for t in range(base + 200, base + 200 + health.CLEAR_SECS + 120, 60):
            paged += self.run_at(t)
        paged += self.run_at(later, down) + self.run_at(later + 130, down)
        self.assertEqual(paged, ["Komga", "Komga"])

    def test_outage_starting_in_cooldown_pages_when_cooldown_ends(self):
        down = ("Suwayomi", "unreachable at http://suwayomi: timed out")
        paged = []
        for t in (0, 60, 120):
            paged += [(t, n) for n in self.run_at(t, down)]
        for t in range(180, 180 + health.CLEAR_SECS + 120, 60):   # recovered
            self.run_at(t)
        with self.assertLogs("mangarr.health", "INFO") as logs:     # a new outage, inside the cooldown: held
            for t in range(3600, 3600 + 3 * 86400, 600):           # and it lasts three days
                paged += [(t, n) for n in self.run_at(t, down)]
        self.assertTrue(any("holding the notification" in line for line in logs.output))
        self.assertEqual([n for _, n in paged], ["Suwayomi", "Suwayomi"])
        self.assertEqual(paged[0][0], 120)
        self.assertGreaterEqual(paged[1][0], 120 + health.COOLDOWN_SECS)
        self.assertLess(paged[1][0], 120 + health.COOLDOWN_SECS + 600)     # at the first run after the cooldown

    def test_held_outage_that_ends_in_cooldown_never_pages(self):
        down = ("Komga", "http://komga: HTTP 500")
        paged = []
        for t in (0, 130):
            paged += self.run_at(t, down)
        for t in range(200, 200 + health.CLEAR_SECS + 120, 60):
            paged += self.run_at(t)
        for t in range(2000, 2400, 60):                           # held
            paged += self.run_at(t, down)
        for t in range(2400, 2400 + health.CLEAR_SECS + 120, 60):   # and over before the cooldown ends
            paged += self.run_at(t)
        for t in range(health.COOLDOWN_SECS, health.COOLDOWN_SECS + 3600, 60):
            paged += self.run_at(t)
        self.assertEqual(paged, ["Komga"])


if __name__ == "__main__":
    unittest.main()
