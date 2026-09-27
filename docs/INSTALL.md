# Installing the stack

mang-arr needs two other programs:

| | Role | Why |
|---|---|---|
| **Suwayomi** | sources and downloads | hosts the Mihon/Tachiyomi extension ecosystem (hundreds of site scrapers maintained by a community) and fetches chapters as CBZ |
| **Komga** | reader | serves the library to browsers and apps, remembers read progress |
| **mang-arr** | manager | knows what each series is, what it should contain, where to get it, and builds the clean library |

They share one data folder:

```
<data>/staging/<Source>/<Series>/*.cbz    Suwayomi writes here (mang-arr never renames it)
<data>/library/<Series>/Chapter 012.0.cbz mang-arr builds this from hard links; Komga reads it
```

The two trees must be on the same filesystem and under the same Docker
mount, or hard links are impossible and every chapter is stored twice.

## Automatic

```sh
curl -fsSL https://raw.githubusercontent.com/AndrewHuddleston/mang-arr/main/install.sh -o install.sh
bash install.sh                 # asks a couple of questions
```

`curl -fsSL .../install.sh | bash` works too: the questions are read from
the terminal, not from the pipe. With no terminal (cron, cloud-init, CI) or
with `YES=1` the installer asks nothing and uses the defaults.

Unattended, without putting a password on the command line (where `ps` and
your shell history would keep it):

```sh
read -rs KOMGA_PASSWORD && export KOMGA_PASSWORD      # type it, press Enter
YES=1 KOMGA_EMAIL=me@example.com bash install.sh --dir /opt/mang-arr --data /srv/manga
```

Leave `KOMGA_PASSWORD` out (or press Enter at the question) and the
installer generates one and saves it to `<dir>/komga-admin.txt`, readable
only by you. Log in to Komga, change the password, then delete the file.
Every Komga question is asked, and a typed password checked (8 characters or
more), before any container starts, so the Komga admin is created the moment
Komga answers.

Requires Docker with Compose v2, `curl` and `python3`. Run it as the user
who should own the files; if you use `sudo`, the containers still run as the
user who ran `sudo` (`SUDO_UID`), never as root unless you set `PUID=0 PGID=0`
yourself. The script:

1. checks the folders: `--dir` and `--data` may be relative (they are
   resolved against the current directory), but not `/`, your home folder
   itself, or a system folder. It creates what is missing and changes the
   owner only of the folders it created, never of an existing tree;
2. writes `docker-compose.yml` with the three services (the containers run
   as your user, and every container's log is capped at 3 × 10 MB);
3. pulls and starts them, and creates the Komga admin the moment Komga
   answers (if the run stops before that, it stops Komga again so nobody
   else can claim it; a Komga that already has an admin is never stopped);
4. Suwayomi: saves chapters as CBZ, turns its own auto-download off (mang-arr
   drives downloads), adds the Keiyoushi extension repository and installs a
   default set of English sources (Weeb Central, MangaDex, Bato, WEBTOON,
   MANGA Plus, Mangakakalot.fun, Manganato, Asura Scans, Flame Comics; change
   with `SOURCES=`). Only the exact Keiyoushi package of each is installed;
5. Komga: an API key for mang-arr and a library on `/library`;
6. mang-arr: **turns its login on** and stores the Komga URL, key and library
   id, in one step, so the Komga key never sits in a mang-arr anyone on the
   network could change. The user is `admin` (`MANGARR_USER=`, no spaces or
   `:`); the password is generated (or `MANGARR_PASSWORD=`) and saved to
   `<dir>/mangarr-login.txt`, readable only by you. It is never printed, so
   no terminal scrollback, cloud-init or CI log keeps it. Log in, change the
   password under Settings → Security, then delete the file. Lost it? See
   [Forgotten mang-arr password](#forgotten-mang-arr-password).

Passwords and API keys are never passed on a command line: the installer
hands them to `curl` through header and body files in a private temporary
folder that it deletes when it finishes.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `STACK_DIR` / `--dir` | `./mang-arr-stack` | compose file and `config/` |
| `DATA_DIR` / `--data` | `<dir>/data` | `staging/` and `library/` |
| `PUID` / `PGID` | you (under sudo: the user who ran sudo) | user the containers run as |
| `TZ` | system timezone | |
| `MANGARR_PORT` / `SUWAYOMI_PORT` / `KOMGA_PORT` | 6789 / 4567 / 25600 | host ports |
| `SUWAYOMI_BIND` / `KOMGA_BIND` | `127.0.0.1` | address those ports are published on (see below) |
| `KOMGA_EMAIL` / `KOMGA_PASSWORD` | `admin@example.com` / generated | Komga admin to create, or to log in with |
| `MANGARR_USER` / `MANGARR_PASSWORD` | `admin` / generated | the mang-arr login to turn on (a generated password goes to `<dir>/mangarr-login.txt`) |
| `MANGARR_API_KEY` | | only for re-runs once mang-arr has a login (below) |
| `MANGARR_IMAGE` | `ghcr.io/andrewhuddleston/mang-arr:latest` | |
| `CONTAINER_PREFIX` | | prefix for the container names |
| `SOURCES` | the list above | Suwayomi extensions, comma-separated pkgName suffixes |
| `YES=1` | | ask nothing |

### Reaching Suwayomi and Komga from other machines

mang-arr talks to Suwayomi and Komga over the compose network, so the
installer publishes their ports on `127.0.0.1` only. Published Docker ports
bypass host firewalls such as ufw, so anything on `0.0.0.0` is open to your
whole network, and to the internet on a VPS.

- **Suwayomi** has no login, and anyone who can reach it can install
  extensions, which are code it runs. Keep it local and use an SSH tunnel
  for its web UI: `ssh -L 4567:127.0.0.1:4567 you@server`, then open
  <http://localhost:4567> on your own computer. `SUWAYOMI_BIND=0.0.0.0`
  opens it anyway, with a warning.
- **Komga** is the reader, and has a login once its admin exists. To read
  on phones and tablets, answer *yes* when the installer asks, or install
  with `KOMGA_BIND=0.0.0.0`. For an existing stack, change Komga's `ports:`
  line in `docker-compose.yml` from `"127.0.0.1:25600:25600"` to
  `"25600:25600"` and run `docker compose up -d`.

### Running it again

Re-running is safe: existing files, the compose file (the installer reads
the ports from the running stack), the Komga admin, Komga's API key and
mang-arr's settings are kept. What a re-run does and does not do:

- Komga already has an admin: it is not touched. The installer only needs
  the Komga password when it has to create a new API key for mang-arr, and
  reuses `komga-admin.txt` if that file is still there.
- mang-arr already has a login: its settings are left alone unless you pass
  `MANGARR_API_KEY` (Settings → Security in mang-arr). With the key, a
  missing Komga key is replaced (an old `mang-arr` key in Komga is deleted
  first; Komga cannot show an existing key again).
- A Komga that does not answer in time (a slow start, a database migration
  after an image update) or has no published port is left running: only a
  Komga the installer has seen without an admin is stopped when a run fails.
  The installer remembers that in `<dir>/.komga-unclaimed` until the admin
  exists.
- A compose file written by an older installer publishes Suwayomi on every
  interface; the installer warns about that but does not rewrite the file.
  Change the `ports:` line as shown above, and add the `x-logging` block from
  [`docker-compose.example.yml`](../docker-compose.example.yml).

### Forgotten mang-arr password

The password is stored in mang-arr's database; set a new one from inside the
container (it is typed at a prompt, not put on the command line):

```sh
cd <dir>     # the folder with docker-compose.yml
docker compose exec mangarr python -c '
import getpass
from mangarr import db, settings
p = getpass.getpass("new mang-arr password: ")
with db.connect() as con:
    settings.set_many(con, {"auth_password": p})
print("password changed")'
```

The user name is unchanged (`admin` unless you chose another); mang-arr
picks the new password up within a few seconds. To switch the login off
instead, store `{"auth_user": ""}` the same way, then set a new login under
Settings → Security right away.

## Manual

### 1. Folders

```sh
mkdir -p /srv/manga/staging /srv/manga/library /opt/stack/config/{suwayomi,komga,mangarr}
chown 1000:1000 /srv/manga /srv/manga/staging /srv/manga/library /opt/stack/config/*   # the uid the containers run as
```

Change the owner of new, empty folders only; do not `chown -R` a share that
other programs use.

### 2. docker-compose.yml

See [`docker-compose.example.yml`](../docker-compose.example.yml): the same
three services the installer writes. Points that matter:

- Suwayomi: `DOWNLOAD_AS_CBZ=true`; mount the staging folder at
  `/home/suwayomi/.local/share/Tachidesk/downloads/mangas` (that is the
  folder whose children are source folders). Publish its port on
  `127.0.0.1` only (see above).
- Komga: mount the library folder read-only at `/library`. Publish it on
  `127.0.0.1` until its admin account exists.
- mang-arr: mount the parent folder at `/data`, set
  `MANGARR_STAGING=/data/staging`, `MANGARR_LIBRARY=/data/library`,
  `MANGARR_SUWAYOMI_URL=http://suwayomi:4567`; mount a config folder at
  `/config`. If you open mang-arr by a host name other than an IP address,
  `localhost` or a `.lan`/`.local`/`.home.arpa` name, list it in
  `MANGARR_ALLOWED_HOSTS`.
- Every service: the `logging:` block, so Docker caps the container logs.

`docker compose up -d`.

### 3. Suwayomi (http://localhost:4567, or through the SSH tunnel)

- Settings → Downloads: **Save as CBZ** on; **Auto download new chapters** off
  (mang-arr checks and downloads on its own schedule).
- Settings → Browse → Extension repositories: add
  `https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.min.json`.
- Browse → Extensions: install the English sources you want. Good starting
  set: Weeb Central, MangaDex, Bato (Bbato), WEBTOON, MANGA Plus,
  Mangakakalot.fun, Manganato, Asura Scans, Flame Comics. mang-arr searches
  every installed English source automatically; disable or throttle any of
  them later in mang-arr → Settings → Sources.

Through the API instead of the UI (on the Docker host):

```sh
S=http://localhost:4567/api/graphql
curl -s $S -H 'Content-Type: application/json' -d '{"query":"mutation { setSettings(input:{settings:{downloadAsCbz:true, autoDownloadNewChapters:false}}) { clientMutationId } }"}'
curl -s $S -H 'Content-Type: application/json' -d '{"query":"mutation($url: String!) { addExtensionStore(input:{indexUrl:$url}) { clientMutationId } }", "variables":{"url":"https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.min.json"}}'
curl -s $S -H 'Content-Type: application/json' -d '{"query":"mutation { fetchExtensions(input:{}) { extensions { pkgName } } }"}'
curl -s $S -H 'Content-Type: application/json' -d '{"query":"mutation($id: String!) { updateExtension(input:{id:$id, patch:{install:true}}) { extension { isInstalled } } }", "variables":{"id":"eu.kanade.tachiyomi.extension.en.weebcentral"}}'
```

### 4. Komga (http://localhost:25600)

- First visit: create the admin account. Do this right after Komga starts:
  until an admin exists, anyone who can reach Komga can create one.
- Add a library: name `Manga`, root `/library`.
- Account menu → **API keys** → create one named `mang-arr`, copy it.

Through the API instead of the UI, with the password in a file rather than
on the command line:

```sh
K=http://localhost:25600
read -r EMAIL; read -rs PASSWORD                     # type them
(umask 077; printf 'X-Komga-Email: %s\nX-Komga-Password: %s\n' "$EMAIL" "$PASSWORD" > komga.hdr)
curl -X POST -H @komga.hdr $K/api/v1/claim                      # first admin
printf 'Authorization: Basic %s\n' "$(printf '%s:%s' "$EMAIL" "$PASSWORD" | base64 -w0)" > komga.hdr
curl -H @komga.hdr -H 'Content-Type: application/json' -X POST -d '{"comment":"mang-arr"}' $K/api/v2/users/me/api-keys
printf 'X-API-Key: %s\n' '<key>' > komga.hdr
curl -H @komga.hdr -H 'Content-Type: application/json' -X POST -d '{"name":"Manga","root":"/library"}' $K/api/v1/libraries
rm komga.hdr
```

### 5. mang-arr (http://host:6789)

- Settings → Security: **set a login first**, before you store any API key.
  Without one, anyone on your network can change mang-arr's settings.
- Settings → Komga: URL `http://komga:25600` (the compose service name),
  the API key, optionally the library id; **Save & test Komga**.
- Settings → Notifications: Pushover, a webhook, and the others.
- Add New to track series, or Library Import if the staging folder already
  holds chapters from an existing Suwayomi.

Or through the API: `PUT /api/v1/settings` with
`{"komga_url": "http://komga:25600", "komga_api_key": "<key>"}`, sending the
`X-Api-Key` header once the login is on.

### 6. Check

mang-arr → System → Status lists every backend check: Suwayomi (version,
sources), Komga (real API call), AniList and MangaDex reachability, the
paths, hard-link viability and disk space. `GET /api/v1/health` returns the
same as JSON. The Docker health check uses `GET /api/v1/ping`, which only
says the process is alive.

## Existing Suwayomi library

Point `MANGARR_STAGING` (or `/data/staging`) at the folder whose children
are Suwayomi's source folders, start mang-arr, open **Library Import**,
scan, adopt. Nothing is moved; the library tree is built from hard links.

## Updating

`docker compose pull && docker compose up -d`. mang-arr shows a banner when
a newer release exists. Database migrations run on start; a backup is taken
daily (System → Backups) and can be restored from the same page. `:latest`
follows `main` and is rebuilt weekly; pin a release tag
(`ghcr.io/andrewhuddleston/mang-arr:0.1.0`) if you prefer to upgrade by hand.
