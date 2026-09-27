"""Numeric MANGARR_* environment variables: a typo or an out-of-range value
is logged and replaced, never a crash while the app loads (with `restart:
unless-stopped` that is a container restart loop)."""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from mangarr import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# every numeric variable read at import time, with a typo a user could make
GARBAGE = {"MANGARR_LOCK_WAIT_SECS": "6h", "MANGARR_REFRESH_HOURS": "6h", "MANGARR_FIRST_REFRESH_MIN": "5m",
           "MANGARR_BACKUPS_KEEP": "seven", "MANGARR_BACKUP_HOURS": "1d", "MANGARR_BACKUP_UPLOAD_MAX_MB": "1G",
           "MANGARR_EVENTS_KEEP_DAYS": "90d", "MANGARR_EVENTS_KEEP_ROWS": "100k", "MANGARR_MAX_UPLOAD_MB": "2G"}

try:
    import fastapi  # noqa: F401
except ImportError:                      # web extras not installed
    fastapi = None


class EnvNumberTest(unittest.TestCase):
    def get(self, raw, **kw):
        with mock.patch.dict(os.environ, {"MANGARR_T": raw}):
            return config.env_number("MANGARR_T", kw.pop("default", 90.0), kw.pop("lo", 1), kw.pop("hi", 100), **kw)

    def test_not_a_number_falls_back_to_the_default(self):
        for raw in ("90d", "1G", "100k", "nan", "inf", "-inf", "1,5"):
            with self.subTest(raw=raw), self.assertLogs("mangarr.config", "WARNING") as logs:
                self.assertEqual(self.get(raw), 90.0)
            self.assertIn("MANGARR_T", logs.output[0])
        for raw in ("", "  "):                                  # unset in a compose file: the default, silently
            with mock.patch.object(config.log, "warning") as warn:
                self.assertEqual(self.get(raw), 90.0)
            warn.assert_not_called()
        self.assertEqual(self.get(" 42 "), 42.0)

    def test_out_of_range_is_clamped(self):
        for raw, want in (("0", 1), ("-5", 1), ("1e9", 100)):
            with self.subTest(raw=raw), self.assertLogs("mangarr.config", "WARNING"):
                self.assertEqual(self.get(raw), want)

    def test_warnings_give_the_exact_numbers(self):
        # round 2: %g printed the clamped 1048576 as 1.04858e+06
        with self.assertLogs("mangarr.config", "WARNING") as logs:
            self.assertEqual(self.get("1e30", default=2048, lo=1, hi=1048576, integer=True), 1048576)
        self.assertIn("MANGARR_T='1e30' is outside 1..1048576; using 1048576", logs.output[0])
        with self.assertLogs("mangarr.config", "WARNING") as logs:
            self.assertEqual(self.get("6h", default=21600.5, lo=0, hi=604800), 21600.5)
        self.assertIn("using the default 21600.5", logs.output[0])

    def test_integers(self):
        self.assertEqual(self.get("7", integer=True, default=7), 7)
        self.assertIsInstance(self.get("7.0", integer=True, default=7), int)
        with self.assertLogs("mangarr.config", "WARNING"):
            self.assertEqual(self.get("7.6", integer=True, default=7), 8)
        with self.assertLogs("mangarr.config", "WARNING"):
            self.assertEqual(self.get("0", integer=True, default=7), 1)       # never "keep no backups"

    def test_no_bare_numeric_env_parse_left(self):
        bare = re.compile(r"\b(int|float)\(\s*os\.(environ|getenv)")
        for folder, _, files in os.walk(os.path.join(ROOT, "mangarr")):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(folder, name), encoding="utf-8") as f:
                        self.assertIsNone(bare.search(f.read()), f"{name}: use config.env_number")

    @unittest.skipIf(fastapi is None, "web extras not installed")
    def test_app_loads_with_garbage_in_every_numeric_variable(self):
        # the review's repro: MANGARR_EVENTS_KEEP_DAYS=90d made `import mangarr.web.app` raise ValueError
        code = ("import mangarr.web.app\n"
                "from mangarr import backup, config, downloader\n"
                "from mangarr.web import security\n"
                "print(downloader.LOCK_WAIT_SECS, config.REFRESH_HOURS, config.FIRST_REFRESH_MIN, backup.KEEP,"
                " backup.INTERVAL_HOURS, backup.UPLOAD_MAX_MB, backup.EVENTS_KEEP_DAYS, backup.EVENTS_KEEP_ROWS,"
                " security.BODY_LIMITS['/system/backups/upload'] >> 20)\n")
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if not k.startswith("MANGARR_")}
            env.update(GARBAGE, MANGARR_DATA=tmp, PYTHONPATH=ROOT)
            out = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp, capture_output=True, text=True,
                                 timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["21600", "6.0", "5.0", "7", "24.0", "512.0", "90.0", "100000", "2048"])
        for name, raw in GARBAGE.items():
            self.assertIn(f"{name}={raw!r} is not a number", out.stderr)


if __name__ == "__main__":
    unittest.main()
