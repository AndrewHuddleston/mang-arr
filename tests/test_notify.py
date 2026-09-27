"""Notification channels: each builds the right request; failures are isolated; event toggles apply."""
import unittest
from unittest import mock

from mangarr import notify, settings


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


if __name__ == "__main__":
    unittest.main()
