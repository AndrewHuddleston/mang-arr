"""install.sh, run for real against fake docker/curl, plus checks of the Dockerfile, CI and dependency files.

The installer is executed the way the README's one-liner does it (`curl ... | bash`: the script on
stdin, no controlling terminal). `docker`, `curl`, `chown`, `sleep` and `python3` are replaced by small
fakes on PATH: fake docker answers `compose port` from the generated compose file, fake curl plays
Suwayomi, Komga and mang-arr from a JSON state file, and every fake logs its argv so the tests can
check that no secret ever appears on a command line.
"""
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "install.sh")
BASH = shutil.which("bash")

GENUINE = "eu.kanade.tachiyomi.extension."

FAKE_DOCKER = r'''
import json, os, re, sys
a = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["docker"] + a) + "\n")
if a[:1] == ["info"]:
    sys.exit(0)
if a[:2] == ["compose", "version"]:
    print("Docker Compose version v2.29.0")
    sys.exit(0)
if a[:1] == ["compose"] and a[1] in ("pull", "up", "stop"):
    sys.exit(0)
if a[:2] == ["compose", "port"]:
    svc, port = a[2], a[3]
    text = open("docker-compose.yml").read()
    m = re.search(r"^  %s:\n(.*?)(?=^  \S|\Z)" % re.escape(svc), text, re.S | re.M)
    pm = m and re.search(r'ports:\n\s+- "?([^"\n]+)"?', m.group(1))
    if not pm:
        sys.exit(1)
    parts = pm.group(1).split(":")
    host, hp, cp = parts if len(parts) == 3 else ["0.0.0.0"] + parts
    if cp != port:
        sys.exit(1)
    print("%s:%s" % (host, hp))
    sys.exit(0)
sys.exit(1)
'''

FAKE_CURL = r'''
import base64, json, os, sys, urllib.parse
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["curl"] + args) + "\n")
method = out = wfmt = url = data = None
fail = False
headers = {}
i = 0
while i < len(args):
    a = args[i]
    if a == "-X":
        method = args[i + 1]; i += 2
    elif a == "-H":
        v = args[i + 1]; i += 2
        for line in (open(v[1:]).read().splitlines() if v.startswith("@") else [v]):
            k, _, val = line.partition(":")
            headers[k.strip().lower()] = val.strip()
    elif a in ("-d", "--data", "--data-binary"):
        v = args[i + 1]; i += 2
        data = sys.stdin.read() if v == "@-" else (open(v[1:]).read() if v.startswith("@") else v)
    elif a == "-u":
        headers["authorization"] = "Basic " + base64.b64encode(args[i + 1].encode()).decode(); i += 2
    elif a == "-o":
        out = args[i + 1]; i += 2
    elif a == "-w":
        wfmt = args[i + 1]; i += 2
    elif a == "--max-time":
        i += 2
    elif a.startswith("-") and not a.startswith("--"):
        fail = fail or "f" in a; i += 1
    else:
        url = a; i += 1
method = method or ("POST" if data is not None else "GET")
path = urllib.parse.urlsplit(url).path
sf = os.environ["FAKE_STATE"]
st = json.load(open(sf))
st.setdefault("events", []).append("%s %s" % (method, path))
K, M, S = st["komga"], st["mangarr"], st["suwayomi"]


def basic_ok():
    h = headers.get("authorization", "")
    if not h.startswith("Basic "):
        return False
    return base64.b64decode(h[6:]).decode() == "%s:%s" % (K.get("email"), K.get("password"))


def respond():
    if path == "/api/graphql":
        if method == "GET":
            return 200, {"data": {"settings": {"downloadAsCbz": True}}}
        try:
            body = json.loads(data)
        except ValueError:
            return 400, {"errors": [{"message": "bad json"}]}
        S.setdefault("requests", []).append(body)
        q, v = body.get("query", ""), body.get("variables") or {}
        for word, n in list(S.get("fail", {}).items()):
            if word in q and n > 0:
                S["fail"][word] = n - 1
                return None, None                       # connection dropped / timed out
        if "updateExtension" in q:
            S.setdefault("installed", []).append(v.get("id"))
            return 200, {"data": {"updateExtension": {"extension": {"pkgName": v.get("id"), "isInstalled": True}}}}
        if "addExtensionStore" in q:
            return 200, {"errors": [{"message": "unknown field addExtensionStore"}]}
        if "extensionRepos:$repos" in q:
            S["repos"] = v.get("repos")
            return 200, {"data": {"setSettings": {"clientMutationId": None}}}
        if "maxSourcesInParallel" in q:
            S["max_parallel"] = int(q.split("maxSourcesInParallel:")[1].split("}")[0])
            return 200, {"data": {"setSettings": {"settings": {"maxSourcesInParallel": S["max_parallel"]}}}}
        if "setSettings" in q:
            S["download_settings"] = True
            return 200, {"data": {"setSettings": {"settings": {"downloadAsCbz": True}}}}
        if "extensionStores" in q:
            return 200, {"errors": [{"message": "unknown field extensionStores"}]}
        if "extensionRepos" in q:
            return 200, {"data": {"settings": {"extensionRepos": S.get("repos", [])}}}
        if "totalCount" in q:
            return 200, {"data": {"extensions": {"totalCount": len(S["catalog"])}}}
        if "nodes" in q:
            return 200, {"data": {"extensions": {"nodes": S["catalog"]}}}
        return 200, {"data": {}}
    if path == "/api/v1/claim":
        if K.get("down"):                               # not answering: slow start, migration
            return None, None
        if method == "GET":
            return 200, {"isClaimed": K["claimed"]}
        if K["claimed"] or K.get("fail_claim"):
            return 400, {"message": "claim failed"}
        K.update(claimed=True, email=headers.get("x-komga-email"), password=headers.get("x-komga-password"))
        return 200, {"email": K["email"]}
    if path == "/api/v2/users/me":
        return (200, {"email": K["email"]}) if basic_ok() else (401, {})
    if path.startswith("/api/v2/users/me/api-keys"):
        if not basic_ok():
            return 401, {}
        keys = K.setdefault("keys", [])
        if method == "GET":
            return 200, [{"id": k["id"], "comment": k["comment"]} for k in keys]
        if method == "DELETE":
            kid = path.rsplit("/", 1)[1]
            K["keys"] = [k for k in keys if k["id"] != kid]
            return 204, ""
        comment = json.loads(data)["comment"]
        if any(k["comment"] == comment for k in keys):
            return 400, {"message": "ERR_1034"}
        K["serial"] = K.get("serial", 0) + 1
        k = {"id": "K%d" % K["serial"], "comment": comment, "key": "KOMGA-KEY-%d-SECRET" % K["serial"]}
        keys.append(k)
        return 200, k
    if path == "/api/v1/libraries":
        if headers.get("x-api-key") not in [k["key"] for k in K.get("keys", [])]:
            return 401, {}
        libs = K.setdefault("libraries", [])
        if method == "POST":
            lib = dict(json.loads(data), id="LIB%d" % (len(libs) + 1))
            libs.append(lib)
            return 200, lib
        return 200, libs
    if path in ("/api/v1/ping", "/api/v1/system/status", "/api/v1/health"):
        if path == "/api/v1/health":
            M.setdefault("health_keys", []).append(headers.get("x-api-key"))
        return 200, {"ok": True}
    if path == "/api/v1/settings":
        s = M["settings"]
        if s.get("auth_user") and headers.get("x-api-key") != s["api_key"]:
            return 401, "authentication required"
        if method == "PUT":
            body = json.loads(data)
            M.setdefault("puts", []).append({"keys": sorted(body), "login_before": bool(s.get("auth_user")),
                                             "api_key_header": headers.get("x-api-key")})
            login_before, key_before = bool(s.get("auth_user")), s.get("api_key")
            s.update(body)
            if not login_before and s.get("auth_user") and s.get("api_key") == key_before:
                # like mang-arr: switching the login on replaces the key anyone could read until then
                s["api_key"] = "MANGARR-API-KEY-%d-ROTATED" % len(M["puts"])
            shown = dict(s)
            s.update(M.pop("tamper", {}))    # another client's change landing right after this PUT
            for k in ("auth_password", "komga_api_key"):
                if shown.get(k):
                    shown[k] = "********"
            return 200, shown
        shown = dict(s)
        for k in ("auth_password", "komga_api_key"):
            if shown.get(k):
                shown[k] = "********"
        return 200, shown
    return 404, {"message": "not found"}


code, body = respond()
json.dump(st, open(sf, "w"))
if code is None:
    sys.stderr.write("curl: (28) Operation timed out\n")
    if wfmt:
        sys.stdout.write("000")
    sys.exit(28)
text = body if isinstance(body, str) else json.dumps(body)
if fail and code >= 400:
    sys.stderr.write("curl: (22) The requested URL returned error: %d\n" % code)
    sys.exit(22)
if out:
    if out != "/dev/null":
        open(out, "w").write(text)
else:
    sys.stdout.write(text)
if wfmt:
    sys.stdout.write(wfmt.replace("%{http_code}", str(code)))
'''

FAKE_LOGGER = r'''
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + "\n")
'''

# a shell wrapper (not python) keeps the many python3 calls cheap; it logs argv as plain text
FAKE_PYTHON = f'printf "%s\\n" "python3 $*" >>"$FAKE_PYLOG"\nexec {sys.executable} "$@"\n'


def catalog(*extra):
    names = {"en.weebcentral": "Weeb Central", "all.mangadex": "MangaDex"}
    return list(extra) + [{"pkgName": GENUINE + suf, "name": n, "lang": "en", "isInstalled": False, "isNsfw": False}
                          for suf, n in names.items()]


@unittest.skipIf(BASH is None, "bash not installed")
class InstallerHarness(unittest.TestCase):
    """A scratch directory with fake tools; run() pipes install.sh into bash like `curl ... | bash`."""

    def setUp(self):
        self.sandbox(self)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    @staticmethod
    def sandbox(obj):
        """Scratch dirs, fake tools and a fresh fake-service state, as attributes of obj."""
        obj.tmp = tempfile.mkdtemp()
        obj.bin = os.path.join(obj.tmp, "bin")
        obj.work = os.path.join(obj.tmp, "work")
        obj.home = os.path.join(obj.tmp, "home")
        for d in (obj.bin, obj.work, obj.home, os.path.join(obj.tmp, "t")):
            os.makedirs(d)
        tools = {"docker": FAKE_DOCKER, "curl": FAKE_CURL, "python3": FAKE_PYTHON,
                 "chown": FAKE_LOGGER, "sleep": FAKE_LOGGER}
        for name, src in tools.items():
            p = os.path.join(obj.bin, name)
            with open(p, "w") as f:
                f.write("#!{}\n{}".format("/bin/sh" if name == "python3" else sys.executable, src))
            os.chmod(p, 0o755)
        obj.log = os.path.join(obj.tmp, "argv.log")
        obj.pylog = os.path.join(obj.tmp, "python-argv.log")
        obj.state_file = os.path.join(obj.tmp, "state.json")
        with open(obj.state_file, "w") as f:
            json.dump({"komga": {"claimed": False},
                       "mangarr": {"settings": {"auth_user": "", "auth_password": "", "komga_api_key": "",
                                                "api_key": "MANGARR-API-KEY-SECRET"}},
                       "suwayomi": {"catalog": catalog(), "repos": []}}, f)

    def state(self):
        with open(self.state_file) as f:
            return json.load(f)

    def save_state(self, st):
        with open(self.state_file, "w") as f:
            json.dump(st, f)

    def run_installer(self, *args, env=None):
        e = {"PATH": self.bin + os.pathsep + "/usr/bin:/bin", "HOME": self.home, "TZ": "UTC",
             "TMPDIR": os.path.join(self.tmp, "t"), "FAKE_LOG": self.log, "FAKE_PYLOG": self.pylog,
             "FAKE_STATE": self.state_file,
             "PUID": str(os.getuid()), "PGID": str(os.getgid()), "LANG": "C.UTF-8"}
        e.update(env or {})
        with open(SCRIPT, "rb") as f:
            script = f.read()
        p = subprocess.run([BASH, "-s", "--"] + list(args), input=script, cwd=self.work, env=e,
                           capture_output=True, timeout=300, start_new_session=True)   # no controlling tty
        return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")

    def argv_log(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def stack(self, *parts):
        return os.path.join(self.work, "mang-arr-stack", *parts)

    def assertInstalled(self, rc, out, err):
        self.assertEqual(rc, 0, f"installer failed:\n{out}\n{err}")


class FreshInstallTest(InstallerHarness):
    """One fresh install through the piped one-liner, then everything it should have done."""

    @classmethod
    def setUpClass(cls):
        cls.sandbox(cls)
        cls.rc, cls.out, cls.err = InstallerHarness.run_installer(cls)
        if cls.rc != 0:
            shutil.rmtree(cls.tmp, True)
            raise AssertionError(f"installer failed:\n{cls.out}\n{cls.err}")
        cls.st = InstallerHarness.state(cls)
        with open(os.path.join(cls.work, "mang-arr-stack", "mangarr-login.txt")) as f:
            m = re.search(r"(?m)^password: (\S+)$", f.read())
        cls.mangarr_password = m.group(1) if m else None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def setUp(self):
        pass                                        # one shared install, made in setUpClass

    def test_piped_run_does_not_read_its_own_source_as_answers(self):
        # finding 41: prompts come from /dev/tty; with none, the defaults are used
        self.assertIn("no terminal", self.out)
        self.assertEqual(self.st["komga"]["email"], "admin@example.com")
        with open(self.stack("komga-admin.txt")) as f:
            saved = f.read()
        self.assertIn("password: {}\n".format(self.st["komga"]["password"]), saved)
        self.assertGreaterEqual(len(self.st["komga"]["password"]), 16)
        self.assertEqual(stat.S_IMODE(os.stat(self.stack("komga-admin.txt")).st_mode), 0o600)
        self.assertNotIn(self.st["komga"]["password"], self.out + self.err)   # not echoed

    def test_no_unclaimed_marker_is_left_once_komga_has_an_admin(self):
        self.assertFalse(os.path.exists(self.stack(".komga-unclaimed")))

    def test_komga_is_claimed_before_any_suwayomi_work(self):
        # finding 40: claim right after Komga answers, not after minutes of Suwayomi setup
        ev = self.st["events"]
        claim = ev.index("POST /api/v1/claim")
        first_gql = next(i for i, e in enumerate(ev) if e == "POST /api/graphql")
        self.assertLess(claim, first_gql)

    def test_suwayomi_and_komga_published_on_loopback_with_log_rotation(self):
        # findings 39 (bind), 103/72 (log rotation)
        with open(self.stack("docker-compose.yml")) as f:
            compose = f.read()
        self.assertIn('- "127.0.0.1:4567:4567"', compose)
        self.assertIn('- "127.0.0.1:25600:25600"', compose)
        self.assertNotRegex(compose, r'- "(0\.0\.0\.0:)?4567:4567"')
        self.assertEqual(compose.count("logging: *logging"), 3)
        self.assertIn('max-size: "10m"', compose)
        self.assertIn('max-file: "3"', compose)
        self.assertIn('- "{}/staging:/home/suwayomi'.format(self.stack("data")), compose)

    def test_mangarr_login_is_turned_on_in_the_same_put_that_stores_the_komga_key(self):
        # finding 9: the admin-scoped Komga key never sits in a mang-arr without a login
        s = self.st["mangarr"]["settings"]
        self.assertEqual(s["auth_user"], "admin")
        self.assertTrue(self.mangarr_password)
        self.assertEqual(s["auth_password"], self.mangarr_password)
        self.assertEqual(s["komga_api_key"], "KOMGA-KEY-1-SECRET")
        self.assertEqual(s["komga_url"], "http://komga:25600")
        self.assertEqual(s["komga_library_id"], "LIB1")
        puts = self.st["mangarr"]["puts"]
        self.assertEqual(len(puts), 1)
        self.assertIn("auth_user", puts[0]["keys"])
        self.assertIn("komga_api_key", puts[0]["keys"])

    def test_mangarr_api_key_read_before_the_login_is_replaced(self):
        # finding 9, second round: the key anyone on the network could read while mang-arr had no login
        # (between `compose up` and the PUT) must not keep working once the installer is done
        s, puts = self.st["mangarr"]["settings"], self.st["mangarr"]["puts"]
        self.assertEqual(puts[0]["api_key_header"], "MANGARR-API-KEY-SECRET")    # read while open, used once
        self.assertFalse(puts[0]["login_before"])
        self.assertNotEqual(s["api_key"], "MANGARR-API-KEY-SECRET")
        self.assertEqual(self.st["mangarr"]["health_keys"], [s["api_key"]])       # later calls: the new key
        self.assertIn("mang-arr replaced its API key", self.out)
        self.assertNotIn(s["api_key"], self.out + self.err)                       # never printed
        # round 2 follow-up: then it checks that mang-arr refuses a request without the key and shows this
        # run's key and user to one with it
        ev = self.st["events"]
        put = ev.index("PUT /api/v1/settings")
        self.assertEqual(ev[put + 1:put + 3], ["GET /api/v1/settings"] * 2)
        self.assertIn("mang-arr refuses requests without its login or API key", self.out)

    def test_mangarr_password_is_saved_privately_and_never_printed(self):
        # second pass, 3: a password shown once and kept nowhere meant a lockout, and without a
        # terminal it went to stdout (cloud-init / CI logs). Now: a 0600 file, like Komga's.
        f = self.stack("mangarr-login.txt")
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        with open(f) as fh:
            self.assertIn("user: admin\n", fh.read())
        self.assertNotIn(self.mangarr_password, self.out + self.err)
        self.assertIn("mangarr-login.txt", self.out)
        for dirpath, _dirs, files in os.walk(self.tmp):
            for name in files:
                if name in ("state.json", "mangarr-login.txt"):   # the fake mang-arr's database; the file
                    continue
                with open(os.path.join(dirpath, name), "rb") as f:
                    self.assertNotIn(self.mangarr_password.encode(), f.read(), os.path.join(dirpath, name))

    def test_secrets_never_appear_in_process_arguments(self):
        # finding 99: passwords and keys travel in 0600 files / the environment, never argv
        secrets = [self.st["komga"]["password"], "KOMGA-KEY-1-SECRET", "MANGARR-API-KEY-SECRET",
                   self.mangarr_password, self.st["mangarr"]["settings"]["api_key"],
                   base64.b64encode(("admin@example.com:" + self.st["komga"]["password"]).encode()).decode()]
        argv = json.dumps(self.argv_log())
        with open(self.pylog) as f:
            argv += f.read()
        self.assertIn("python3 -c", argv)
        for s in secrets:
            self.assertNotIn(s, argv)
        self.assertTrue(any(a[0] == "curl" for a in self.argv_log()))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "t")), [], "temp files left behind")

    def test_graphql_requests_are_json_with_variables(self):
        # finding 100: values travel as variables; the query text never contains catalogue data
        reqs = self.st["suwayomi"]["requests"]
        installs = [r for r in reqs if "updateExtension" in r["query"]]
        self.assertEqual(len(installs), 2)
        for r in installs:
            self.assertNotIn("tachiyomi", r["query"])
        self.assertEqual(sorted(self.st["suwayomi"]["installed"]),
                         [GENUINE + "all.mangadex", GENUINE + "en.weebcentral"])
        self.assertIn("https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.min.json",
                      self.st["suwayomi"]["repos"])

    def test_a_new_suwayomi_downloads_from_three_sources_at_once(self):
        # mang-arr's Download Lanes (3) never exceed Suwayomi's 'max sources in parallel': set it on a new
        # install, in a request of its own so an older Suwayomi cannot undo the CBZ setting with it
        reqs = [r["query"] for r in self.st["suwayomi"]["requests"] if "setSettings" in r["query"]]
        par = [q for q in reqs if "maxSourcesInParallel" in q]
        self.assertEqual(len(par), 1)
        self.assertIn("setSettings(input:{settings:{maxSourcesInParallel:3}})", par[0])
        self.assertTrue(any("downloadAsCbz:true" in q and "maxSourcesInParallel" not in q for q in reqs))
        self.assertEqual(self.st["suwayomi"]["max_parallel"], 3)
        self.assertIn("Suwayomi downloads from up to 3 sources at once", self.out)


class RerunTest(InstallerHarness):
    """finding 96: running the installer again is safe and does not invent a new Komga password."""

    def test_rerun_keeps_everything(self):
        self.assertInstalled(*self.run_installer())
        first = self.state()
        with open(self.stack("komga-admin.txt")) as f:
            saved = f.read()
        with open(self.stack("mangarr-login.txt")) as f:
            mangarr_login = f.read()
        rc, out, err = self.run_installer(env={"SUWAYOMI_BIND": "0.0.0.0"})
        self.assertInstalled(rc, out, err)
        st = self.state()
        self.assertIn("already has an admin", out)
        # second pass, 8: the early summary does not claim ports the existing compose file does not have
        self.assertIn("ports: as published by the existing", out)
        self.assertNotIn("0.0.0.0:4567", out)
        self.assertNotIn("will be reachable from the network", err)
        with open(self.stack("mangarr-login.txt")) as f:
            self.assertEqual(f.read(), mangarr_login)
        self.assertIn("already has a login", err)
        self.assertNotIn("password:", out)                   # no new, never-applied password
        self.assertEqual(st["komga"]["keys"], first["komga"]["keys"])
        self.assertEqual(st["mangarr"]["settings"], first["mangarr"]["settings"])
        with open(self.stack("komga-admin.txt")) as f:
            self.assertEqual(f.read(), saved)

        # with the API key (the one mang-arr made when the first run turned its login on) it looks, sees the
        # Komga key and leaves it alone; the key read before that first login no longer opens anything
        key = first["mangarr"]["settings"]["api_key"]
        rc, out, err = self.run_installer(env={"MANGARR_API_KEY": key})
        self.assertInstalled(rc, out, err)
        self.assertIn("already has a Komga API key", out)
        self.assertNotIn("replaced its API key", out)                    # a login was there: no new key
        self.assertEqual(self.state()["komga"]["keys"], first["komga"]["keys"])
        self.assertEqual(self.state()["mangarr"]["settings"]["api_key"], key)
        self.assertEqual(self.state()["mangarr"]["health_keys"][-1], key)
        rc, out, err = self.run_installer(env={"MANGARR_API_KEY": "MANGARR-API-KEY-SECRET"})
        self.assertNotEqual(rc, 0)
        self.assertIn("rejected MANGARR_API_KEY", err)

    def test_rerun_leaves_suwayomis_parallel_setting_alone(self):
        self.assertInstalled(*self.run_installer())
        self.assertEqual(self.state()["suwayomi"]["max_parallel"], 3)
        with open(self.stack("config", "suwayomi", "server.conf"), "w") as f:     # what Suwayomi writes on start
            f.write("server.maxSourcesInParallel = 5\n")
        st = self.state()
        st["suwayomi"]["max_parallel"] = 5                                          # the user's choice since
        self.save_state(st)
        rc, out, err = self.run_installer()
        self.assertInstalled(rc, out, err)
        st = self.state()
        self.assertEqual(st["suwayomi"]["max_parallel"], 5)
        self.assertEqual(sum("maxSourcesInParallel" in r["query"] for r in st["suwayomi"]["requests"]), 1)
        self.assertNotIn("sources at once", out)
        self.assertEqual(sum("downloadAsCbz:true" in r["query"] for r in st["suwayomi"]["requests"]), 2)

    def test_rerun_replaces_a_komga_key_mangarr_lost(self):
        self.assertInstalled(*self.run_installer())
        st = self.state()
        st["mangarr"]["settings"]["komga_api_key"] = ""
        self.save_state(st)
        key = st["mangarr"]["settings"]["api_key"]
        rc, out, err = self.run_installer(env={"MANGARR_API_KEY": key})
        self.assertInstalled(rc, out, err)
        st = self.state()
        self.assertEqual([k["id"] for k in st["komga"]["keys"]], ["K2"])       # old one removed, one new
        self.assertEqual(st["mangarr"]["settings"]["komga_api_key"], "KOMGA-KEY-2-SECRET")
        last_put = st["mangarr"]["puts"][-1]
        self.assertNotIn("auth_user", last_put["keys"])                         # login left as it was
        self.assertEqual(last_put["api_key_header"], key)
        self.assertEqual(st["mangarr"]["settings"]["api_key"], key)             # ... and so is the key


class MangarrChangedMeanwhileTest(InstallerHarness):
    """Round 2, installer #9: another client on the network changes mang-arr's settings while it has no
    login (its key, or the login itself). The installer must notice after its PUT instead of reporting a
    protected mang-arr that holds the Komga key."""

    def assertStops(self, tamper):
        st = self.state()
        st["mangarr"]["tamper"] = tamper
        self.save_state(st)
        rc, out, err = self.run_installer()
        self.assertNotEqual(rc, 0, out)
        self.assertIn("not the one this install set", err)
        self.assertIn("Delete the Komga API key named 'mang-arr'", err)
        self.assertNotIn("Done.", out)
        self.assertNotIn("ATTACKER-KEY", out + err)

    def test_api_key_changed_after_the_put(self):
        self.assertStops({"api_key": "ATTACKER-KEY"})

    def test_login_switched_off_after_the_put(self):
        self.assertStops({"auth_user": ""})

    def test_other_user_after_the_put(self):
        self.assertStops({"auth_user": "eve"})


class KomgaStopTest(InstallerHarness):
    """Second pass, 1: the EXIT trap stops only a Komga known to have no admin."""

    def rerun(self, **env):
        os.remove(self.log)                                  # this run's commands only
        return self.run_installer(env=env)

    def stopped(self):
        return ["docker", "compose", "stop", "komga"] in self.argv_log()

    def test_claimed_komga_that_does_not_answer_is_left_running(self):
        self.assertInstalled(*self.run_installer())
        st = self.state()
        st["komga"]["down"] = True                           # e.g. migrating its database after an update
        self.save_state(st)
        rc, out, err = self.rerun()
        self.assertNotEqual(rc, 0)
        self.assertIn("did not come up", err)
        self.assertFalse(self.stopped(), err)
        self.assertNotIn("before Komga had an admin", err)

    def test_claimed_komga_without_a_published_port_is_left_running(self):
        self.assertInstalled(*self.run_installer())
        with open(self.stack("docker-compose.yml")) as f:
            compose = f.read()
        compose = compose.replace('    ports:\n      - "127.0.0.1:25600:25600"\n', "")   # behind a reverse proxy
        with open(self.stack("docker-compose.yml"), "w") as f:
            f.write(compose)
        rc, out, err = self.rerun()
        self.assertNotEqual(rc, 0)
        self.assertIn("Komga has no published port", err)
        self.assertFalse(self.stopped(), err)

    def test_a_komga_left_unclaimed_is_remembered_across_runs(self):
        st = self.state()
        st["komga"]["fail_claim"] = True
        self.save_state(st)
        rc, out, err = self.run_installer()
        self.assertNotEqual(rc, 0)
        self.assertTrue(self.stopped())
        self.assertTrue(os.path.exists(self.stack(".komga-unclaimed")))
        # the next run cannot reach it: still treated as unclaimed, so stopped again
        st = self.state()
        st["komga"].update(fail_claim=False, down=True)
        self.save_state(st)
        rc, out, err = self.rerun()
        self.assertNotEqual(rc, 0)
        self.assertTrue(self.stopped())
        # and once it answers, it is claimed with the password saved by the first run
        st = self.state()
        st["komga"]["down"] = False
        self.save_state(st)
        rc, out, err = self.rerun()
        self.assertInstalled(rc, out, err)
        st = self.state()
        self.assertTrue(st["komga"]["claimed"])
        with open(self.stack("komga-admin.txt")) as f:
            self.assertIn("password: {}\n".format(st["komga"]["password"]), f.read())
        self.assertFalse(os.path.exists(self.stack(".komga-unclaimed")))
        self.assertFalse(self.stopped())


class FailureTest(InstallerHarness):
    def test_komga_is_stopped_when_the_claim_fails(self):
        # finding 40: never leave an unclaimed Komga running after an abort
        st = self.state()
        st["komga"]["fail_claim"] = True
        self.save_state(st)
        rc, out, err = self.run_installer()
        self.assertNotEqual(rc, 0)
        self.assertIn(["docker", "compose", "stop", "komga"], self.argv_log())
        self.assertIn("stopping Komga", err)
        self.assertFalse(any("graphql" in e for e in self.state()["events"]))

    def test_transient_suwayomi_failures_do_not_abort(self):
        # finding 40: a dropped poll used to kill the run with a traceback before the Komga claim
        st = self.state()
        st["suwayomi"]["fail"] = {"totalCount": 2, "nodes": 0}
        self.save_state(st)
        rc, out, err = self.run_installer()
        self.assertInstalled(rc, out, err)
        self.assertNotIn("Traceback", err)
        self.assertTrue(self.state()["komga"]["claimed"])


class InputTest(InstallerHarness):
    def assertRefusedBeforeUp(self, env, msg):
        rc, out, err = self.run_installer(env=env)
        self.assertNotEqual(rc, 0, out)
        self.assertIn(msg, err)
        self.assertFalse(any(a[:3] == ["docker", "compose", "up"] for a in self.argv_log()))
        self.assertFalse(self.state()["komga"]["claimed"])

    def test_mangarr_user_with_a_colon_is_refused_up_front(self):
        # second pass, 6: mang-arr refuses it, which used to stop the run at its very last step
        self.assertRefusedBeforeUp({"MANGARR_USER": "a:b"}, "MANGARR_USER 'a:b'")

    def test_short_komga_password_is_refused_before_anything_starts(self):
        # second pass, 2: it used to be rejected only after `up`, leaving Komga to be stopped again
        self.assertRefusedBeforeUp({"KOMGA_PASSWORD": "short"}, "at least 8 characters")

    def test_no_gnu_stat_needed(self):
        # second pass, 7: `stat -c` is GNU-only; a BSD stat used to end the run silently
        with open(os.path.join(self.bin, "stat"), "w") as f:
            f.write("#!/bin/sh\necho 'stat: illegal option -- c' >&2\nexit 1\n")
        os.chmod(os.path.join(self.bin, "stat"), 0o755)
        rc, out, err = self.run_installer()
        self.assertInstalled(rc, out, err)
        self.assertNotIn("illegal option", err)
        self.assertEqual(stat.S_IMODE(os.stat(self.stack("komga-admin.txt")).st_mode), 0o600)


class CatalogueTest(InstallerHarness):
    def test_only_the_exact_package_is_installed_and_names_are_sanitised(self):
        # finding 100: a lookalike or injected pkgName from another repo is not picked up
        st = self.state()
        injected = ('x\\",patch:{install:true}}){clientMutationId}evil:setSettings(input:{settings:'
                    '{extensionRepos:[\\"https://attacker.example/index.min.json\\"]}}){clientMutationId}'
                    'z:updateExtension(input:{id:\\"' + GENUINE + 'en.weebcentral')
        st["suwayomi"]["catalog"] = catalog(
            {"pkgName": "evil.en.weebcentral", "name": "Evil", "isInstalled": False},
            {"pkgName": injected, "name": "Injected", "isInstalled": False},
            {"pkgName": "evil.all.mangadex", "name": "\x1b]0;pwned\x07Evil\x1b[2J", "isInstalled": False},
        )
        self.save_state(st)
        rc, out, err = self.run_installer()
        self.assertInstalled(rc, out, err)
        st = self.state()
        self.assertEqual(sorted(st["suwayomi"]["installed"]), [GENUINE + "all.mangadex", GENUINE + "en.weebcentral"])
        self.assertNotIn("attacker.example", json.dumps(st["suwayomi"].get("repos")))
        self.assertNotIn("\x1b]", out + err)


class DirectoryTest(InstallerHarness):
    def assertRefused(self, *args, msg):
        rc, out, err = self.run_installer(*args)
        self.assertNotEqual(rc, 0, out)
        self.assertIn(msg, err)
        self.assertFalse(any(a[:2] == ["docker", "compose"] and "up" in a for a in self.argv_log()))

    def test_dangerous_folders_are_refused(self):
        # finding 42
        self.assertRefused("--data", "/", msg="itself")
        self.assertRefused("--data", self.home, msg="home directory")
        self.assertRefused("--dir", "/etc/mang-arr", msg="system directory")
        self.assertRefused("--data", "/usr", msg="system directory")
        self.assertRefused("--data", "", msg="needs a folder")
        self.assertRefused("--data", os.path.join(self.tmp, "a:b"), msg="cannot hold")
        self.assertRefused("--data", self.tmp + "/x\ny", msg="cannot hold")
        self.assertFalse(any(a[0] == "chown" for a in self.argv_log()))

    def test_relative_paths_are_resolved_once(self):
        # finding 98: mkdir and the compose file agree on where the data lives
        rc, out, err = self.run_installer("--dir", "stack", "--data", "./manga")
        self.assertInstalled(rc, out, err)
        data = os.path.join(self.work, "manga")
        self.assertTrue(os.path.isdir(os.path.join(data, "staging")))
        with open(os.path.join(self.work, "stack", "docker-compose.yml")) as f:
            compose = f.read()
        self.assertIn(f'- "{data}/staging:/home/suwayomi/', compose)
        self.assertIn(f'- "{data}:/data"', compose)
        self.assertNotIn("stack/data", compose)

    def test_only_created_folders_are_chowned(self):
        # finding 42: an existing tree is never chowned, nothing is chowned recursively
        existing = os.path.join(self.work, "share")
        os.makedirs(os.path.join(existing, "library", "Other App"))
        rc, out, err = self.run_installer("--data", existing, env={"PUID": "4242", "PGID": "4242"})
        self.assertInstalled(rc, out, err)
        chowned = [a for a in self.argv_log() if a[0] == "chown"]
        self.assertTrue(chowned)
        targets = [a[-1] for a in chowned]
        for a in chowned:
            self.assertNotIn("-R", a)
            self.assertEqual(a[-2], "4242:4242")
        self.assertIn(os.path.join(existing, "staging"), targets)
        self.assertNotIn(existing, targets)
        self.assertNotIn(os.path.join(existing, "library"), targets)
        self.assertIn("belongs to uid", err)                # the existing library is reported instead


@unittest.skipIf(BASH is None, "bash not installed")
class FunctionTest(unittest.TestCase):
    """Functions sourced from install.sh (sourcing defines them without running the installer)."""

    def bash(self, code, **env):
        e = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
        e.update(env)
        p = subprocess.run([BASH, "-c", 'source "$0"; ' + code, SCRIPT], env=e, capture_output=True,
                           text=True, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def test_sudo_runs_containers_as_the_invoking_user(self):
        # finding 97
        rc, out, _ = self.bash('pick_ids 0; echo "$PUID:$PGID"', SUDO_UID="1234", SUDO_GID="2345", SUDO_USER="bob")
        self.assertEqual((rc, out.strip().splitlines()[-1]), (0, "1234:2345"))
        rc, _, err = self.bash('pick_ids 0; echo "$PUID:$PGID"')
        self.assertNotEqual(rc, 0)
        self.assertIn("running as root", err)
        rc, out, err = self.bash('pick_ids 0; echo "$PUID:$PGID"', PUID="0", PGID="0")
        self.assertEqual((rc, out.strip()), (0, "0:0"))
        self.assertIn("as root", err)

    def test_komga_login_is_settled_by_one_question(self):
        # second pass, 2: Enter at the password question generates the password right then, so the
        # claim after `up` never waits on a second prompt; a short one is asked again
        # ask_secret runs in $(...): it counts its calls in a file, one line per question
        stubs = ('TTY=1; ask() { printf %s "$2"; }; asked() { wc -l <"$COUNT" | tr -d " "; }; '
                 'ask_secret() { echo >>"$COUNT"; local a=(${ANSWERS}); printf %s "${a[$(( $(asked) - 1 ))]:-}"; }; ')
        with tempfile.TemporaryDirectory() as d:
            count = os.path.join(d, "n")
            open(count, "w").close()
            rc, out, err = self.bash(stubs + 'ask_komga_login; ask_komga_login; '
                                     'echo "$(asked) $KOMGA_GENERATED ${#KOMGA_PASSWORD} $KOMGA_EMAIL"',
                                     ANSWERS="", COUNT=count)
            self.assertEqual((rc, out.split()[-4:]), (0, ["1", "1", "24", "admin@example.com"]), err)
            open(count, "w").close()
            rc, out, err = self.bash(stubs + 'ask_komga_login; echo "$(asked) $KOMGA_GENERATED $KOMGA_PASSWORD"',
                                     ANSWERS="short longenough1", COUNT=count)
            self.assertEqual((rc, out.split()[-3:]), (0, ["2", "0", "longenough1"]), err)

    def test_help_does_not_need_the_script_file(self):
        with open(SCRIPT, "rb") as f:
            p = subprocess.run([BASH, "-s", "--", "--help"], input=f.read(), capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"--data DIR", p.stdout)


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as f:
        return f.read()


class PackagingTest(unittest.TestCase):
    def test_dockerfile_healthcheck_is_liveness_only_and_base_pinned(self):
        # findings 63, 102
        df = read("Dockerfile")
        hc = df[df.index("HEALTHCHECK"):]
        self.assertIn("/api/v1/ping", hc.split("\n\n")[0])
        self.assertNotIn("system/status", hc.split("\n\n")[0])
        self.assertRegex(df, r"(?m)^FROM python:\d+\.\d+\.\d+-slim-\w+@sha256:[0-9a-f]{64}$")

    def test_ci_is_least_privilege_and_pinned(self):
        # findings 95, 101
        ci = read(".github", "workflows", "ci.yml")
        top = ci[:ci.index("\njobs:")]
        self.assertRegex(top, r"permissions:\n  contents: read\n")
        self.assertNotIn("packages: write", top)
        jobs = re.split(r"(?m)^  (?=[a-z-]+:\n)", ci[ci.index("\njobs:"):])[1:]
        for job in jobs:
            name = job.split(":", 1)[0]
            self.assertEqual("packages: write" in job, name == "publish", name)
            for uses in re.findall(r"uses: (\S+)", job):
                self.assertRegex(uses, r"@[0-9a-f]{40}$", uses)
            self.assertEqual(job.count("actions/checkout@"), job.count("persist-credentials: false"), name)
        publish = next(j for j in jobs if j.startswith("publish:"))
        self.assertNotIn("type=gha", publish)
        self.assertIn("latest=false", publish)
        self.assertIn("concurrency:", publish)
        # second pass, 9: the concurrency group does not order runs by commit; :latest moves only when
        # this commit is still main's head
        self.assertIn("steps.head.outputs.newest == 'true'", publish)
        self.assertIn('gh api "repos/$REPO/commits/main"', publish)
        self.assertIn("if: steps.meta.outputs.tags != ''", publish)
        for tool in ("ruff", "build", "httpx"):                 # CI-only tools: exact versions
            self.assertRegex(ci, rf"pip install [^\n]*\b{tool}==\d", tool)
            self.assertNotRegex(ci, rf"pip install [^\n]*\b{tool}(\s|$)", tool)

    def test_dependabot_covers_docker_pip_and_actions(self):
        db = read(".github", "dependabot.yml")
        for eco in ("docker", "pip", "github-actions"):
            self.assertIn(f'package-ecosystem: "{eco}"', db)

    def test_dependency_floors(self):
        # findings 102, 111: floors past known-vulnerable releases, same in both files
        reqs = [ln for ln in read("requirements.txt").splitlines() if ln and not ln.startswith("#")]
        pyproject = read("pyproject.toml")
        web = re.search(r"web = \[(.*?)\]", pyproject, re.S).group(1)
        self.assertEqual(reqs, re.findall(r'"([^"]+)"', web))

        def floor(name):
            spec = next(r for r in reqs if re.match(re.escape(name) + r"\b", r))
            return tuple(int(x) for x in re.search(r">=([\d.]+)", spec).group(1).split("."))
        self.assertGreaterEqual(floor("fastapi"), (0, 132))
        self.assertGreaterEqual(floor("starlette"), (0, 49, 1))
        self.assertGreaterEqual(floor("python-multipart"), (0, 0, 18))
        self.assertGreaterEqual(floor("jinja2"), (3, 1, 6))

    def test_readme_names_the_real_healthcheck(self):
        # second pass, 4: the Monitoring section still said the HEALTHCHECK used /api/v1/system/status
        readme = read("README.md")
        for part in re.split(r"\n\s*\n|\n- ", readme):
            if "HEALTHCHECK" in part:
                self.assertNotIn("system/status", part, part)
        self.assertNotIn("shown once", readme)

    def test_compose_example_binds_loopback_and_rotates_logs(self):
        ex = read("docker-compose.example.yml")
        self.assertIn('"127.0.0.1:4567:4567"', ex)
        self.assertIn('"127.0.0.1:25600:25600"', ex)
        self.assertEqual(ex.count("logging: *logging"), 3)


if __name__ == "__main__":
    unittest.main()
