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
# or, unattended:
YES=1 KOMGA_EMAIL=me@example.com KOMGA_PASSWORD='choose one' bash install.sh --dir /opt/mang-arr --data /srv/manga
```

Requires Docker with Compose v2, `curl` and `python3`. The script:

1. writes `docker-compose.yml` with the three services (the containers run as your user),
2. pulls and starts them and waits until each answers,
3. Suwayomi: saves chapters as CBZ, turns its own auto-download off (mang-arr drives downloads), adds the Keiyoushi extension repository and installs a default set of English sources (Weeb Central, MangaDex, Bato, WEBTOON, MANGA Plus, Mangakakalot.fun, Manganato, Asura Scans, Flame Comics; change with `SOURCES=`),
4. Komga: creates the admin user (or uses yours), an API key for mang-arr, and a library on `/library`,
5. mang-arr: stores the Komga URL, key and library id so it can trigger a rescan after every import.

Re-running it is safe: existing files and objects are kept.

## Manual

### 1. Folders

```sh
mkdir -p /srv/manga/staging /srv/manga/library /opt/stack/config/{suwayomi,komga,mangarr}
chown -R 1000:1000 /srv/manga /opt/stack/config     # the uid the containers run as
```

### 2. docker-compose.yml

See [`docker-compose.example.yml`](../docker-compose.example.yml): the same
three services the installer writes. Points that matter:

- Suwayomi: `DOWNLOAD_AS_CBZ=true`; mount the staging folder at
  `/home/suwayomi/.local/share/Tachidesk/downloads/mangas` (that is the
  folder whose children are source folders).
- Komga: mount the library folder read-only at `/library`.
- mang-arr: mount the parent folder at `/data`, set
  `MANGARR_STAGING=/data/staging`, `MANGARR_LIBRARY=/data/library`,
  `MANGARR_SUWAYOMI_URL=http://suwayomi:4567`; mount a config folder at
  `/config`.

`docker compose up -d`.

### 3. Suwayomi (http://host:4567)

- Settings → Downloads: **Save as CBZ** on; **Auto download new chapters** off
  (mang-arr checks and downloads on its own schedule).
- Settings → Browse → Extension repositories: add
  `https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.min.json`.
- Browse → Extensions: install the English sources you want. Good starting
  set: Weeb Central, MangaDex, Bato (Bbato), WEBTOON, MANGA Plus,
  Mangakakalot.fun, Manganato, Asura Scans, Flame Comics. mang-arr searches
  every installed English source automatically; disable or throttle any of
  them later in mang-arr → Settings → Sources.

Through the API instead of the UI:

```sh
S=http://localhost:4567/api/graphql
curl -s $S -H 'Content-Type: application/json' -d '{"query":"mutation { setSettings(input:{settings:{downloadAsCbz:true, autoDownloadNewChapters:false}}) { clientMutationId } }"}'
curl -s $S -H 'Content-Type: application/json' -d '{"query":"mutation { addExtensionStore(input:{indexUrl:\"https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.min.json\"}) { clientMutationId } }"}'
curl -s $S -H 'Content-Type: application/json' -d '{"query":"mutation { fetchExtensions(input:{}) { extensions { pkgName } } }"}'
curl -s $S -H 'Content-Type: application/json' -d '{"query":"mutation { updateExtension(input:{id:\"eu.kanade.tachiyomi.extension.en.weebcentral\", patch:{install:true}}) { extension { isInstalled } } }"}'
```

### 4. Komga (http://host:25600)

- First visit: create the admin account.
- Add a library: name `Manga`, root `/library`.
- Account menu → **API keys** → create one named `mang-arr`, copy it.

Through the API instead of the UI:

```sh
K=http://localhost:25600
curl -X POST -H 'X-Komga-Email: me@example.com' -H 'X-Komga-Password: secret' $K/api/v1/claim   # first admin
curl -u me@example.com:secret -H 'Content-Type: application/json' -X POST -d '{"comment":"mang-arr"}' $K/api/v2/users/me/api-keys
curl -H "X-API-Key: <key>" -H 'Content-Type: application/json' -X POST -d '{"name":"Manga","root":"/library"}' $K/api/v1/libraries
```

### 5. mang-arr (http://host:6789)

- Settings → Komga: URL `http://komga:25600` (the compose service name),
  the API key, optionally the library id; **Save & test Komga**.
- Settings → Security: set a login. Settings → Notifications: Pushover or a
  webhook.
- Add New to track series, or Library Import if the staging folder already
  holds chapters from an existing Suwayomi.

Or through the API: `PUT /api/v1/settings` with
`{"komga_url": "http://komga:25600", "komga_api_key": "<key>"}`.

### 6. Check

mang-arr → System → Status lists every backend check: Suwayomi (version,
sources), Komga (real API call), AniList and MangaDex reachability, the
paths, hard-link viability and disk space. `GET /api/v1/health` returns the
same as JSON.

## Existing Suwayomi library

Point `MANGARR_STAGING` (or `/data/staging`) at the folder whose children
are Suwayomi's source folders, start mang-arr, open **Library Import**,
scan, adopt. Nothing is moved; the library tree is built from hard links.

## Updating

`docker compose pull && docker compose up -d`. mang-arr shows a banner when
a newer release exists. Database migrations run on start; a backup is taken
daily (System → Backups) and can be restored from the same page.
