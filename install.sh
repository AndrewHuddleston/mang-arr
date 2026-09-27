#!/usr/bin/env bash
# mang-arr stack installer: Suwayomi (sources + downloads) + Komga (reader) + mang-arr (manager),
# brought up with Docker Compose and wired together.
#
#   curl -fsSL https://raw.githubusercontent.com/AndrewHuddleston/mang-arr/main/install.sh | bash
#   # or: bash install.sh [--dir DIR] [--data DIR] [--yes]
#
# Questions are read from the terminal (/dev/tty), never from stdin, so the piped form works.
# Without a terminal (cron, cloud-init, CI) or with YES=1 the defaults are used without asking.
#
# Environment overrides (all optional):
#   STACK_DIR      where docker-compose.yml and the config folders go   (default ./mang-arr-stack)
#   DATA_DIR       where chapters are stored: <DATA_DIR>/staging (Suwayomi) and <DATA_DIR>/library (Komga)
#                  (default <STACK_DIR>/data; both trees MUST live under this one folder for hard links)
#   PUID / PGID    user the containers run as     (default: you; under sudo, the user who ran sudo)
#   TZ             timezone                                              (default: system or UTC)
#   MANGARR_PORT / SUWAYOMI_PORT / KOMGA_PORT                            (default 6789 / 4567 / 25600)
#   SUWAYOMI_BIND / KOMGA_BIND  address their port is published on (default 127.0.0.1: this machine
#                  only; mang-arr reaches both over the compose network). 0.0.0.0 = every interface.
#   KOMGA_EMAIL / KOMGA_PASSWORD   Komga admin to create (or to log in with, if Komga is already set up).
#                  A generated password is saved to <STACK_DIR>/komga-admin.txt (mode 0600).
#   MANGARR_USER / MANGARR_PASSWORD  the login the installer turns on in mang-arr (the user name
#                  cannot contain ':'; default admin / generated). A generated password is saved to
#                  <STACK_DIR>/mangarr-login.txt (mode 0600), never printed.
#   MANGARR_API_KEY  mang-arr's API key; only needed to re-run against a mang-arr that already has a login
#   MANGARR_IMAGE  image for mang-arr                                    (default ghcr.io/andrewhuddleston/mang-arr:latest)
#   CONTAINER_PREFIX  prefix for the three container names (default none)
#   SOURCES        Suwayomi extensions to install, comma-separated pkgName suffixes (see DEFAULT_SOURCES)
#   YES=1          no prompts
#
# Everything runs inside main(), called on the last line: a download cut short runs nothing.
set -euo pipefail

# Suwayomi extensions installed by default (Keiyoushi pkgName suffixes): a mix of
# official platforms and the aggregators that carry the widest English catalogue.
DEFAULT_SOURCES="en.weebcentral,all.mangadex,en.bbato,all.webtoons,all.mangaplus,en.mangakakalotfun,en.manganelo,en.asurascans,en.flamecomics"
EXT_REPO="https://raw.githubusercontent.com/keiyoushi/extensions/repo/index.min.json"
EXT_PREFIX="eu.kanade.tachiyomi.extension."   # only this exact package name is installed for a suffix
KOMGA_KEY_COMMENT="mang-arr"
MASK="********"                               # how mang-arr's API shows a stored secret

TTY=0            # 1 when questions can be asked on /dev/tty
WORK=""          # private temp dir (0700) for request bodies and header files that carry secrets
KOMGA_PENDING=0  # 1 once Komga is known to have no admin (the EXIT trap stops it then); see komga_unclaimed
KOMGA_MARK=""    # <STACK_DIR>/.komga-unclaimed: remembers that across runs
KOMGA_GENERATED=0  # 1 when this run generated the Komga password (not yet saved or applied)
PUBLISHED_ON=""  # host address of the port host_url last looked up

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m  %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERR\033[0m  %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
mang-arr stack installer: Suwayomi + Komga + mang-arr with Docker Compose, wired together.

  bash install.sh [--dir DIR] [--data DIR] [--yes]
  curl -fsSL https://raw.githubusercontent.com/AndrewHuddleston/mang-arr/main/install.sh | bash

  --dir DIR    where docker-compose.yml and config/ go (default ./mang-arr-stack)
  --data DIR   chapter storage: DIR/staging and DIR/library (default <dir>/data)
  --yes, -y    ask nothing; use the defaults and environment variables

Environment variables are listed at the top of this script and in docs/INSTALL.md.
EOF
}

need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required"; }

# ------------------------------------------------------------------ terminal questions
# Under `curl ... | bash` stdin IS this script, so a plain `read` would eat its next lines as
# answers. Every question goes to and comes from /dev/tty; without one the defaults are used.
open_tty() {
  if [[ "$YES" != 1 ]] && { true </dev/tty; } 2>/dev/null && { true >/dev/tty; } 2>/dev/null; then
    TTY=1
  else
    TTY=0
    [[ "$YES" == 1 ]] || say "no terminal to ask questions on: using the defaults (as with YES=1)"
    YES=1
  fi
}

ask() {  # ask <prompt> <default>: the answer, or the default, on stdout
  local a=""
  if [[ $TTY == 1 ]]; then
    printf '%s' "$1" >/dev/tty
    IFS= read -r a </dev/tty || a=""
  fi
  printf '%s' "${a:-$2}"
}

ask_secret() {  # ask_secret <prompt>: typed twice, not echoed; empty when there is no terminal
  local a="" b=""
  [[ $TTY == 1 ]] || return 0
  while :; do
    printf '%s' "$1" >/dev/tty; IFS= read -r -s a </dev/tty || a=""; printf '\n' >/dev/tty
    [[ -n "$a" ]] || return 0
    printf 'again: ' >/dev/tty; IFS= read -r -s b </dev/tty || b=""; printf '\n' >/dev/tty
    [[ "$a" != "$b" ]] || break
    printf 'the two entries differ; try again\n' >/dev/tty
  done
  printf '%s' "$a"
}

# ------------------------------------------------------------------ input checks
abspath() { python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }  # realpath -m is GNU-only
# owner <path>: "uid:gid" of a file ("?:?" when it cannot be read). `stat -c` is GNU-only (BSD/macOS
# use `stat -f`), and a failing command substitution would end the run silently under set -e.
owner() { python3 -c 'import os,sys;s=os.stat(sys.argv[1]);print("%d:%d"%(s.st_uid,s.st_gid))' "$1" 2>/dev/null || echo '?:?'; }

target_home() {  # home folder of the user who ran sudo, if any
  [[ -n "${SUDO_USER:-}" ]] || return 0
  getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true
}

# check_dir <label> <path> <varname>: refuse paths that are dangerous to create, chown or mount, and
# store the absolute, normalised path (symlinks resolved) in <varname>. Relative paths are resolved
# against the current directory here, so mkdir and the compose file agree on where they are.
check_dir() {
  local label=$1 p=$2 abs home d
  [[ -n "$p" ]] || die "$label is empty"
  # ':' splits a compose volume spec, '$' is compose interpolation, quotes/backslashes/newlines break YAML
  if [[ "$p" == *[:\"\\\$]* || "$p" == *$'\n'* || "$p" == *$'\r'* || "$p" == *$'\t'* ]]; then
    die "$label '$p' contains a character docker-compose.yml cannot hold (: \" \\ \$ tab or newline)"
  fi
  abs=$(abspath "$p") || die "cannot resolve $label '$p'"
  for d in / /home /mnt /media /opt /root /srv /tmp /var /var/lib /usr/local /Users /Volumes; do
    [[ "$abs" != "$d" ]] || die "$label '$p' is $abs itself; use a dedicated folder inside it, e.g. ${d%/}/mang-arr"
  done
  for d in /bin /boot /dev /etc /lib /lib32 /lib64 /libx32 /proc /run /sbin /sys /usr; do
    if [[ "$abs" == "$d" || "$abs" == "$d"/* ]] && [[ "$abs" != /usr/local/* ]]; then
      die "$label '$p' is inside the system directory $d; choose another folder"
    fi
  done
  for home in "${HOME:-}" "$(target_home)"; do
    [[ -n "$home" ]] || continue
    [[ "$abs" != "$(abspath "$home")" ]] || die "$label '$p' is a home directory itself; use a folder inside it, e.g. $home/mang-arr"
  done
  printf -v "$3" '%s' "$abs"
}

# pick_ids <euid>: the uid:gid the containers run as. Under sudo `id -u` is 0, which would run every
# container (Suwayomi executes third-party extension code) as root and leave root-owned files behind.
# Use the user who ran sudo instead, and refuse root unless it was asked for explicitly.
pick_ids() {
  if [[ -z "${PUID:-}" ]]; then
    if [[ "$1" == 0 ]]; then
      [[ -n "${SUDO_UID:-}" && "$SUDO_UID" != 0 ]] \
        || die "running as root: set PUID and PGID to the user the containers should run as (PUID=0 PGID=0 only if you really want them to run as root)"
      PUID=$SUDO_UID
      PGID=${PGID:-${SUDO_GID:-$SUDO_UID}}
      say "running under sudo: the containers will run as ${SUDO_USER:-uid $PUID} ($PUID:$PGID), not as root"
    else
      PUID=$(id -u)
    fi
  fi
  PGID=${PGID:-$(id -g "$PUID" 2>/dev/null || printf '%s' "$PUID")}
  [[ "$PUID" =~ ^[0-9]+$ && "$PGID" =~ ^[0-9]+$ ]] || die "PUID/PGID must be numeric ids (got '$PUID:$PGID')"
  [[ "$PUID" != 0 ]] || warn "PUID=0: all three containers will run as root"
}

check_inputs() {
  local v
  for v in MANGARR_PORT SUWAYOMI_PORT KOMGA_PORT; do
    if [[ ! "${!v}" =~ ^[0-9]{1,5}$ ]] || (( ${!v} < 1 || ${!v} > 65535 )); then die "$v must be a port number (got '${!v}')"; fi
  done
  for v in SUWAYOMI_BIND KOMGA_BIND; do
    [[ "${!v}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "$v must be an IPv4 address such as 127.0.0.1 or 0.0.0.0 (got '${!v}')"
  done
  if [[ ! "$TZ" =~ ^[A-Za-z0-9_+/-]+$ ]]; then warn "TZ '$TZ' is not a timezone name; using UTC"; TZ=UTC; fi
  [[ "$CONTAINER_PREFIX" =~ ^[A-Za-z0-9_.-]*$ ]] || die "CONTAINER_PREFIX may only hold letters, digits, '_', '.' and '-'"
  [[ "$MANGARR_IMAGE" =~ ^[A-Za-z0-9._/:@-]+$ ]] || die "MANGARR_IMAGE '$MANGARR_IMAGE' is not an image reference"
  [[ "$SOURCES" =~ ^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*(,[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*)*$ ]] \
    || die "SOURCES must be comma-separated pkgName suffixes such as en.weebcentral,all.mangadex"
  # mang-arr refuses a user name with ':' (basic auth splits user:password there): say so now, not
  # at the very last step after everything else is set up
  [[ "$MANGARR_USER" =~ ^[^[:space:][:cntrl:]:]+$ ]] \
    || die "MANGARR_USER '$MANGARR_USER' must not be empty or contain spaces, control characters or ':'"
}

valid_email() { [[ "$1" =~ ^[^@[:space:][:cntrl:]]+@[^@[:space:][:cntrl:]]+\.[^@[:space:][:cntrl:]]+$ ]]; }

# ------------------------------------------------------------------ folders and compose file
# make_dirs: create the folders, and chown only the ones this run created, never recursively: --data
# may point at an existing share that other programs use. Existing folders are reported, not changed.
make_dirs() {
  local d uid created=()
  for d in "$STACK_DIR" "$STACK_DIR/config" "$STACK_DIR/config/mangarr" "$STACK_DIR/config/suwayomi" \
           "$STACK_DIR/config/komga" "$DATA_DIR" "$DATA_DIR/staging" "$DATA_DIR/library"; do
    [[ ! -d "$d" ]] || continue
    mkdir -p -- "$d" || die "cannot create $d"
    created+=("$d")
  done
  for d in "${created[@]}"; do
    [[ "$(owner "$d")" != "$PUID:$PGID" ]] || continue
    chown -- "$PUID:$PGID" "$d" || warn "could not chown $d to $PUID:$PGID; the containers run as that user and must be able to write it"
  done
  for d in "$STACK_DIR/config/mangarr" "$STACK_DIR/config/suwayomi" "$STACK_DIR/config/komga" \
           "$DATA_DIR/staging" "$DATA_DIR/library"; do
    uid=$(owner "$d"); uid=${uid%%:*}
    [[ "$uid" == "$PUID" ]] \
      || warn "$d belongs to uid $uid but the containers run as $PUID; if they cannot write it: sudo chown -R $PUID:$PGID '$d'"
  done
  (( ${#created[@]} == 0 )) || ok "created ${#created[@]} folder(s) in $STACK_DIR and $DATA_DIR"
}

render_compose() {  # the generated docker-compose.yml on stdout (every value was checked by check_*)
  cat <<EOF
# Generated by mang-arr install.sh. Three services, one data tree:
#   $DATA_DIR/staging  <- Suwayomi writes chapters here (never renamed)
#   $DATA_DIR/library  <- mang-arr builds the per-series hard-link tree here; Komga reads it
# mang-arr reaches Suwayomi and Komga over the compose network. Their own ports are published on
# $SUWAYOMI_BIND (Suwayomi) and $KOMGA_BIND (Komga); 127.0.0.1 means this machine only. Suwayomi has no
# login and runs extension code: keep it off the network (docs/INSTALL.md explains the options).

# Docker keeps container output forever unless told otherwise: cap it for every service.
x-logging: &logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"

services:
  suwayomi:
    image: ghcr.io/suwayomi/suwayomi-server:stable
    container_name: "${CONTAINER_PREFIX}suwayomi"
    user: "$PUID:$PGID"
    environment:
      - "TZ=$TZ"
      - DOWNLOAD_AS_CBZ=true
    volumes:
      - "./config/suwayomi:/home/suwayomi/.local/share/Tachidesk"
      - "$DATA_DIR/staging:/home/suwayomi/.local/share/Tachidesk/downloads/mangas"
    ports:
      - "$SUWAYOMI_BIND:$SUWAYOMI_PORT:4567"
    logging: *logging
    restart: unless-stopped

  komga:
    image: gotson/komga:latest
    container_name: "${CONTAINER_PREFIX}komga"
    user: "$PUID:$PGID"
    environment:
      - "TZ=$TZ"
    volumes:
      - "./config/komga:/config"
      - "$DATA_DIR/library:/library:ro"
    ports:
      - "$KOMGA_BIND:$KOMGA_PORT:25600"
    logging: *logging
    restart: unless-stopped

  mangarr:
    image: "$MANGARR_IMAGE"
    container_name: "${CONTAINER_PREFIX}mangarr"
    user: "$PUID:$PGID"
    depends_on:
      - suwayomi
    environment:
      - "TZ=$TZ"
      - MANGARR_SUWAYOMI_URL=http://suwayomi:4567
      - MANGARR_STAGING=/data/staging
      - MANGARR_LIBRARY=/data/library
      # host names you open mang-arr by, other than IPs, localhost, single-label names and *.lan/.local/.home.arpa:
      # - MANGARR_ALLOWED_HOSTS=manga.example.com
    volumes:
      - "./config/mangarr:/config"
      - "$DATA_DIR:/data"
    ports:
      - "$MANGARR_PORT:6789"
    logging: *logging
    restart: unless-stopped
EOF
}

write_compose() {
  local f="$STACK_DIR/docker-compose.yml"
  if [[ -f "$f" ]]; then
    warn "$f exists; keeping it (delete it to regenerate). Ports are read from the running stack."
    return 0
  fi
  render_compose >"$f.tmp.$$"          # write, then rename: never a half-written compose file
  mv -f -- "$f.tmp.$$" "$f"
  [[ "$(owner "$f")" == "$PUID:"* ]] || chown -- "$PUID:$PGID" "$f" || true
  ok "wrote $f"
}

compose() { (cd "$STACK_DIR" && docker compose "$@"); }

# host_url <varname> <service> <container-port>: store http://127.0.0.1:<published port> in <varname>,
# asked from the running stack, so a re-run with an existing compose file (maybe with other ports)
# talks to the right place. Sets PUBLISHED_ON to the host address the port is bound to.
host_url() {
  local out
  out=$(compose port "$2" "$3" 2>/dev/null | head -n 1) || true
  [[ "${out##*:}" =~ ^[0-9]+$ ]] || return 1
  PUBLISHED_ON=${out%:*}
  printf -v "$1" 'http://127.0.0.1:%s' "${out##*:}"
}

# ------------------------------------------------------------------ HTTP helpers
# Secrets (passwords, API keys) never go on a command line, where any local user can read them in
# `ps` or /proc/<pid>/cmdline: headers go in files read by `curl -H @file`, bodies in files read by
# `--data-binary @file`, all inside $WORK (mode 0700). JSON is built with python's json.dumps, with
# secret values handed over in the environment.

hdr() {  # hdr <name> <header line>...: write a header file, print its path
  local f="$WORK/$1.hdr"
  printf '%s\n' "${@:2}" >"$f"
  printf '%s' "$f"
}

# save_secret <file> <title> <hint> <line>...: a generated login, in a file only its owner can read
# (mode 0600, owned by PUID), so it is never printed where logs, CI output or scrollback keep it.
save_secret() {
  ( umask 077                        # inside the subshell, before the file is created
    { printf '# %s on %s.\n# %s\n' "$2" "$(date '+%Y-%m-%d %H:%M')" "$3"
      printf '%s\n' "${@:4}"; } >"$1.tmp.$$" )
  mv -f -- "$1.tmp.$$" "$1"
  [[ "$(owner "$1")" == "$PUID:"* ]] || chown -- "$PUID:$PGID" "$1" || true
}

# req <method> <url> [header-file] [json-body-file]: response body -> $WORK/resp, HTTP status -> stdout
# ("000" when nothing answered).
req() {
  local args=(-sS --max-time 60 -o "$WORK/resp" -w '%{http_code}' -X "$1")
  [[ -z "${3:-}" ]] || args+=(-H "@$3")
  [[ -z "${4:-}" ]] || args+=(-H 'Content-Type: application/json' --data-binary "@$4")
  : >"$WORK/resp"
  curl "${args[@]}" "$2" 2>"$WORK/curl.err" || true
}

resp() { cat "$WORK/resp"; }

jsonget() {  # jsonget <dotted.path> < json: the value ("" when missing); status 1 on bad JSON
  python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
for k in sys.argv[1].split("."):
    if isinstance(d, list):
        d = d[int(k)] if k.isdigit() and int(k) < len(d) else None
    elif isinstance(d, dict):
        d = d.get(k)
    else:
        d = None
    if d is None:
        break
print("" if d is None else (json.dumps(d) if isinstance(d, (dict, list)) else d))' "$1"
}

jvars() {  # jvars name value ...: a JSON object of string values (GraphQL variables)
  python3 -c 'import json,sys;a=sys.argv[1:];print(json.dumps(dict(zip(a[::2], a[1::2]))))' "$@"
}

# gql <query> [variables-json]: POST to Suwayomi's GraphQL API; the response on stdout. Fails on an
# HTTP error and on a GraphQL "errors" answer (sent with HTTP 200). Values always travel as GraphQL
# variables, never pasted into the query text, so a hostile catalogue entry cannot add a mutation.
gql() {
  local vars='{}'
  [[ $# -lt 2 ]] || vars=$2
  python3 -c 'import json,sys;print(json.dumps({"query": sys.argv[1], "variables": json.loads(sys.argv[2])}))' \
    "$1" "$vars" >"$WORK/gql.json"
  curl -fsS --max-time 60 -H 'Content-Type: application/json' --data-binary "@$WORK/gql.json" \
    -o "$WORK/gql.out" "$SUWA/api/graphql" || return 1
  python3 -c 'import json,sys
t = open(sys.argv[1]).read()
try:
    bad = bool(json.loads(t).get("errors"))
except (ValueError, AttributeError):
    bad = True
sys.stdout.write(t)
sys.exit(1 if bad else 0)' "$WORK/gql.out"
}

wait_http() {  # wait_http <label> <tries> <url>...: until one of the urls answers 2xx
  local i u
  for (( i = 0; i < $2; i++ )); do
    for u in "${@:3}"; do curl -fsS -o /dev/null --max-time 5 "$u" 2>/dev/null && return 0; done
    sleep 2
  done
  die "$1 did not come up at $3 within about $(( $2 * 2 ))s (docker compose logs $1)"
}

cleanup() {
  local rc=$?
  # Only a Komga known to have no admin is stopped. One that merely did not answer (a slow start, a
  # database migration after an image update, no published port) may be claimed and in use.
  if [[ $KOMGA_PENDING == 1 ]] && ! komga_claimed; then
    warn "the installer stopped before Komga had an admin account; stopping Komga so nobody else can claim it"
    compose stop komga >/dev/null 2>&1 || warn "could not stop Komga: run 'docker compose stop komga' in $STACK_DIR"
    warn "fix the problem above and run the installer again"
  elif [[ $KOMGA_PENDING == 1 ]]; then
    komga_has_admin
  fi
  [[ -z "$WORK" ]] || rm -rf -- "$WORK"
  return "$rc"
}

# ------------------------------------------------------------------ Komga
KOMGA_SECRET_FILE=""
KOMGA_KEY=""; KOMGA_LIB=""

komga_claimed() {  # true when Komga answers and already has an admin
  local url=""
  host_url url komga 25600 || return 1
  [[ "$(curl -fsS --max-time 5 "$url/api/v1/claim" 2>/dev/null | jsonget isClaimed 2>/dev/null)" =~ ^[Tt]rue$ ]]
}

komga_fresh() {  # true when this stack's Komga has never run (its config folder is empty or missing)
  [[ -z "$(ls -A -- "$STACK_DIR/config/komga" 2>/dev/null)" ]]
}

# komga_unclaimed / komga_has_admin: what the installer knows about Komga's admin. The marker file
# carries "no admin yet" to the next run, which then treats Komga as unclaimed even if it cannot
# reach it; without the marker, a Komga that does not answer is left alone.
komga_unclaimed() { KOMGA_PENDING=1; : >"$KOMGA_MARK" 2>/dev/null || true; }
komga_has_admin() { KOMGA_PENDING=0; rm -f -- "$KOMGA_MARK"; }

load_komga_file() {  # reuse the login a previous run generated, unless one was given explicitly
  [[ -f "$KOMGA_SECRET_FILE" ]] || return 0
  [[ -n "${KOMGA_EMAIL:-}" ]] || KOMGA_EMAIL=$(sed -n 's/^email: //p' "$KOMGA_SECRET_FILE" | head -n 1)
  [[ -n "${KOMGA_PASSWORD:-}" ]] || KOMGA_PASSWORD=$(sed -n 's/^password: //p' "$KOMGA_SECRET_FILE" | head -n 1)
}

# For a new Komga the admin is asked for (and checked, and generated on Enter) before anything
# starts, so the claim goes out the moment Komga answers without waiting on anyone at the keyboard
# (until then, whoever reaches Komga first can make themselves its admin). Once KOMGA_PASSWORD is
# set, calling it again asks nothing.
ask_komga_login() {
  [[ -n "${KOMGA_EMAIL:-}" ]] || KOMGA_EMAIL=$(ask "Komga admin email [admin@example.com]: " admin@example.com)
  valid_email "$KOMGA_EMAIL" || die "'$KOMGA_EMAIL' is not an email address (Komga requires one)"
  while [[ -z "${KOMGA_PASSWORD:-}" && $TTY == 1 ]]; do
    KOMGA_PASSWORD=$(ask_secret "Komga admin password (min 8 characters; Enter = generate one): ")
    [[ -n "$KOMGA_PASSWORD" ]] || break
    if (( ${#KOMGA_PASSWORD} < 8 )); then
      { printf 'too short: at least 8 characters, or Enter to generate one\n' >/dev/tty; } 2>/dev/null || true
      KOMGA_PASSWORD=""
    fi
  done
  if [[ -z "${KOMGA_PASSWORD:-}" ]]; then
    KOMGA_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')
    KOMGA_GENERATED=1          # saved to $KOMGA_SECRET_FILE right before the claim uses it
  fi
  (( ${#KOMGA_PASSWORD} >= 8 )) || die "the Komga admin password (KOMGA_PASSWORD) must have at least 8 characters"
  [[ "$KOMGA_PASSWORD" != *[$'\r\n']* ]] || die "the Komga admin password must be a single line"
}

claim_komga() {
  local st f
  st=$(curl -fsS --max-time 10 "$KOMGA/api/v1/claim" | jsonget isClaimed) || die "Komga did not say whether it has an admin"
  if [[ "$st" =~ ^[Tt]rue$ ]]; then
    komga_has_admin
    if [[ $KOMGA_GENERATED == 1 ]]; then     # generated for a claim that is not needed: not its password
      KOMGA_PASSWORD=""; KOMGA_GENERATED=0
    fi
    ok "Komga already has an admin"
    return 0
  fi
  komga_unclaimed
  ask_komga_login                             # asks nothing when it already ran before `up`
  if [[ $KOMGA_GENERATED == 1 ]]; then
    # Komga has no self-service password reset: keep the generated password in a file only its
    # owner can read, instead of printing it where logs and scrollback would keep it.
    save_secret "$KOMGA_SECRET_FILE" "Komga admin created by mang-arr install.sh" \
      "Log in, change the password, then delete this file." "email: $KOMGA_EMAIL" "password: $KOMGA_PASSWORD"
    ok "generated a Komga admin password: it is in $KOMGA_SECRET_FILE (readable only by its owner)"
  fi
  f=$(hdr claim "X-Komga-Email: $KOMGA_EMAIL" "X-Komga-Password: $KOMGA_PASSWORD")
  st=$(req POST "$KOMGA/api/v1/claim" "$f")
  rm -f -- "$f"
  [[ "$st" == 2* ]] || die "could not create the Komga admin user (HTTP $st: $(resp | head -c 300))"
  komga_has_admin
  ok "Komga admin user created ($KOMGA_EMAIL)"
}

# komga_key_and_library: a new API key named 'mang-arr' and the library on /library, for mang-arr.
# Sets KOMGA_KEY and KOMGA_LIB; returns 1 after a warning when that is not possible.
komga_key_and_library() {
  local auth keyhdr st ids id
  if [[ -z "${KOMGA_PASSWORD:-}" ]]; then
    [[ -n "${KOMGA_EMAIL:-}" ]] || KOMGA_EMAIL=$(ask "Komga admin email: " "")
    KOMGA_PASSWORD=$(ask_secret "Komga password for ${KOMGA_EMAIL:-the admin} (Enter = skip the Komga key): ")
  fi
  if [[ -z "${KOMGA_EMAIL:-}" || -z "${KOMGA_PASSWORD:-}" ]]; then
    warn "no Komga login (set KOMGA_EMAIL and KOMGA_PASSWORD): not creating a Komga API key for mang-arr"
    return 1
  fi
  [[ "$KOMGA_EMAIL$KOMGA_PASSWORD" != *[$'\r\n']* ]] || die "the Komga login must be a single line"
  # basic auth assembled with the printf builtin, so the password never shows up in argv
  auth=$(hdr komga-login "Authorization: Basic $(printf '%s:%s' "$KOMGA_EMAIL" "$KOMGA_PASSWORD" | base64 | tr -d '\n')")
  st=$(req GET "$KOMGA/api/v2/users/me" "$auth")
  if [[ "$st" != 2* ]]; then
    warn "Komga login failed for $KOMGA_EMAIL (HTTP $st): not creating a Komga API key for mang-arr"
    return 1
  fi
  # Komga refuses a second key with the same comment and cannot show an existing key again. mang-arr
  # does not have ours (that is why we are here), so an old 'mang-arr' key is useless: replace it.
  st=$(req GET "$KOMGA/api/v2/users/me/api-keys" "$auth")
  if [[ "$st" == 2* ]]; then
    ids=$(resp | python3 -c 'import json,sys
for k in json.load(sys.stdin):
    if k.get("comment") == sys.argv[1] and str(k.get("id", "")).isalnum():
        print(k["id"])' "$KOMGA_KEY_COMMENT" 2>/dev/null) || ids=""
    for id in $ids; do
      st=$(req DELETE "$KOMGA/api/v2/users/me/api-keys/$id" "$auth")
      if [[ "$st" == 2* ]]; then ok "removed the old '$KOMGA_KEY_COMMENT' Komga API key (mang-arr did not have it)"
      else warn "could not remove the old '$KOMGA_KEY_COMMENT' Komga API key (HTTP $st)"; fi
    done
  fi
  python3 -c 'import json,sys;print(json.dumps({"comment": sys.argv[1]}))' "$KOMGA_KEY_COMMENT" >"$WORK/komga-key.json"
  st=$(req POST "$KOMGA/api/v2/users/me/api-keys" "$auth" "$WORK/komga-key.json")
  rm -f -- "$auth"
  [[ "$st" == 2* ]] || { warn "Komga did not create an API key (HTTP $st: $(resp | head -c 300))"; return 1; }
  KOMGA_KEY=$(resp | jsonget key) || KOMGA_KEY=""
  [[ -n "$KOMGA_KEY" ]] || { warn "Komga did not return an API key"; return 1; }
  ok "Komga API key for mang-arr created"

  keyhdr=$(hdr komga-key "X-API-Key: $KOMGA_KEY")
  KOMGA_LIB=""
  st=$(req GET "$KOMGA/api/v1/libraries" "$keyhdr")
  if [[ "$st" == 2* ]]; then
    KOMGA_LIB=$(resp | python3 -c 'import json,sys
print(next((l["id"] for l in json.load(sys.stdin) if str(l.get("root", "")).rstrip("/") == "/library"), ""))' 2>/dev/null) || KOMGA_LIB=""
  fi
  if [[ -n "$KOMGA_LIB" ]]; then
    ok "Komga library on /library exists ($KOMGA_LIB)"
  else
    python3 -c 'import json;print(json.dumps({"name": "Manga", "root": "/library", "scanOnStartup": True, "hashFiles": True}))' >"$WORK/lib.json"
    st=$(req POST "$KOMGA/api/v1/libraries" "$keyhdr" "$WORK/lib.json")
    KOMGA_LIB=$(resp | jsonget id 2>/dev/null) || KOMGA_LIB=""
    if [[ "$st" == 2* && -n "$KOMGA_LIB" ]]; then ok "Komga library 'Manga' created on /library ($KOMGA_LIB)"
    else warn "could not create the Komga library on /library (HTTP $st); add it in Komga"; KOMGA_LIB=""; fi
  fi
  rm -f -- "$keyhdr"
}

# ------------------------------------------------------------------ Suwayomi
configure_suwayomi() {
  local repos total=0 catalog suf pkg state name i
  say "configuring Suwayomi"
  if gql 'mutation { setSettings(input:{settings:{downloadAsCbz:true, autoDownloadNewChapters:false}}) { settings { downloadAsCbz } } }' >/dev/null; then
    ok "chapters saved as CBZ; Suwayomi's own auto-download off (mang-arr drives downloads)"
  else
    warn "could not change Suwayomi's download settings: turn Save as CBZ on and auto-download off in its UI"
  fi
  repos=$( (gql '{ extensionStores { indexUrl } }' 2>/dev/null || true; gql '{ settings { extensionRepos } }' 2>/dev/null || true) | tr -d '\n')
  if [[ "$repos" != *keiyoushi* ]]; then
    # older Suwayomi has no addExtensionStore: append to extensionRepos instead, keeping the others
    # shellcheck disable=SC2016  # $url and $repos are GraphQL variables, not shell ones
    if gql 'mutation($url: String!) { addExtensionStore(input:{indexUrl:$url}) { clientMutationId } }' \
         "$(jvars url "$EXT_REPO")" >/dev/null 2>&1 \
       || gql 'mutation($repos: [String!]!) { setSettings(input:{settings:{extensionRepos:$repos}}) { clientMutationId } }' \
         "$(gql '{ settings { extensionRepos } }' 2>/dev/null | python3 -c 'import json,sys
try:
    r = json.load(sys.stdin)["data"]["settings"]["extensionRepos"] or []
except (ValueError, KeyError, TypeError):
    r = []
print(json.dumps({"repos": r + [sys.argv[1]]}))' "$EXT_REPO")" >/dev/null; then
      ok "extension repository added (Keiyoushi, $EXT_REPO)"
    else
      warn "could not add the extension repository: add $EXT_REPO in Suwayomi's settings"
    fi
  fi
  gql 'mutation { fetchExtensions(input:{}) { extensions { pkgName } } }' >/dev/null 2>&1 || true
  # The index is fetched in the background: wait until the catalogue is populated. A failed or
  # garbled poll is just another try, never the end of the install.
  for (( i = 0; i < 30; i++ )); do
    total=$(gql '{ extensions { totalCount } }' 2>/dev/null | jsonget data.extensions.totalCount 2>/dev/null) || total=0
    [[ "$total" =~ ^[0-9]+$ ]] || total=0
    (( total == 0 )) || break
    sleep 2
  done
  if (( total == 0 )); then
    warn "extension catalogue is empty: install sources in Suwayomi's UI (docs/INSTALL.md, section 3)"
    return 0
  fi
  ok "extension catalogue: $total extensions"
  catalog=$(gql '{ extensions { nodes { pkgName name lang isInstalled isNsfw } } }' 2>/dev/null) || catalog=""
  # One line per requested suffix: suffix<TAB>pkgName<TAB>state<TAB>name. Only the exact Keiyoushi
  # package name counts (a lookalike such as evil.en.weebcentral from another repo does not), and names
  # lose control characters before they reach the terminal. A requested source is installed even when
  # the catalogue's loose NSFW flag is set.
  printf '%s' "$catalog" | python3 -c '
import json, re, sys
prefix, wanted = sys.argv[1], [s for s in sys.argv[2].split(",") if s]
try:
    nodes = json.load(sys.stdin)["data"]["extensions"]["nodes"] or []
except (ValueError, KeyError, TypeError):
    nodes = []
by_pkg = {n.get("pkgName"): n for n in nodes if isinstance(n, dict)}
for suf in wanted:
    e = by_pkg.get(prefix + suf)
    if e is None:
        print(suf, "", "", "", sep="\t")
    else:
        name = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(e.get("name") or suf))[:80]
        print(suf, e["pkgName"], "installed" if e.get("isInstalled") else "missing", name, sep="\t")
' "$EXT_PREFIX" "$SOURCES" >"$WORK/wanted.tsv" || : >"$WORK/wanted.tsv"
  while IFS=$'\t' read -r suf pkg state name; do
    if [[ -z "$pkg" ]]; then warn "extension '$suf' not in the catalogue; skipped"; continue; fi
    if [[ "$state" == installed ]]; then ok "$name already installed"; continue; fi
    # shellcheck disable=SC2016  # $id is a GraphQL variable
    if gql 'mutation($id: String!) { updateExtension(input:{id:$id, patch:{install:true}}) { extension { pkgName isInstalled } } }' \
         "$(jvars id "$pkg")" >/dev/null 2>&1; then
      ok "installed $name"
    else
      warn "could not install $name ($pkg)"
    fi
  done <"$WORK/wanted.tsv"
}

# ------------------------------------------------------------------ mang-arr
MA_HDR=""; MA_OK=0; MA_LOGIN_SET=0; MA_HAS_KOMGA=0; MA_NEW_PASSWORD=""
MANGARR_SECRET_FILE=""

probe_mangarr() {  # may we change mang-arr's settings, and what does it have already?
  local st key
  [[ -z "${MANGARR_API_KEY:-}" ]] || MA_HDR=$(hdr mangarr "X-Api-Key: $MANGARR_API_KEY")
  st=$(req GET "$MANGARR/api/v1/settings" "$MA_HDR")
  case "$st" in
    2*)
      MA_OK=1
      [[ -z "$(resp | jsonget auth_user)" ]] || MA_LOGIN_SET=1
      [[ -z "$(resp | jsonget komga_api_key)" ]] || MA_HAS_KOMGA=1
      key=$(resp | jsonget api_key) || key=""
      if [[ -z "$MA_HDR" && -n "$key" && "$key" != "$MASK" ]]; then MA_HDR=$(hdr mangarr "X-Api-Key: $key"); fi
      ;;
    401|403)
      [[ -z "${MANGARR_API_KEY:-}" ]] || die "mang-arr rejected MANGARR_API_KEY (HTTP $st)"
      warn "mang-arr already has a login: leaving its settings alone. To let the installer store a Komga key"
      warn "in it, run it again with MANGARR_API_KEY=<the key from mang-arr -> Settings -> Security>"
      ;;
    *) die "mang-arr's settings API answered HTTP $st: $(resp | head -c 300)" ;;
  esac
}

# One PUT turns the login on and stores the Komga key, so the key never sits in a mang-arr that anyone
# on the network could reconfigure (and point at their own "Komga") or back up without logging in.
configure_mangarr() {
  local st user="" pass=""
  if [[ $MA_LOGIN_SET == 0 ]]; then
    user=$MANGARR_USER
    pass=${MANGARR_PASSWORD:-}
    if [[ -z "$pass" ]]; then
      pass=$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))'); MA_NEW_PASSWORD=$pass
      # saved before mang-arr gets it, so a run cut short after the PUT cannot lose it
      save_secret "$MANGARR_SECRET_FILE" "mang-arr login turned on by install.sh" \
        "Change the password in mang-arr -> Settings -> Security; forgotten: docs/INSTALL.md, 'Forgotten mang-arr password'." \
        "user: $user" "password: $pass"
    fi
  fi
  if [[ -z "$user" && -z "$KOMGA_KEY" ]]; then ok "mang-arr needs no changes"; return 0; fi
  MA_USER="$user" MA_PASS="$pass" MA_KKEY="$KOMGA_KEY" MA_KLIB="$KOMGA_LIB" python3 -c 'import json, os
e = os.environ
b = {}
if e["MA_USER"]:
    b.update(auth_user=e["MA_USER"], auth_password=e["MA_PASS"])
if e["MA_KKEY"]:
    b.update(komga_url="http://komga:25600", komga_api_key=e["MA_KKEY"], komga_library_id=e["MA_KLIB"])
print(json.dumps(b))' >"$WORK/mangarr.json"
  st=$(req PUT "$MANGARR/api/v1/settings" "$MA_HDR" "$WORK/mangarr.json")
  rm -f -- "$WORK/mangarr.json"
  if [[ "$st" != 2* ]]; then
    # a 4xx is a refusal (nothing stored): drop the password. After a timeout or a 5xx it may have
    # been applied, so the file stays rather than lock the user out.
    [[ -z "$MA_NEW_PASSWORD" || "$st" != 4* ]] || rm -f -- "$MANGARR_SECRET_FILE"
    die "mang-arr did not accept its settings (HTTP $st: $(resp | head -c 300))"
  fi
  [[ -z "$user" ]] || ok "mang-arr login turned on (user $user)"
  [[ -z "$KOMGA_KEY" ]] || ok "mang-arr knows Komga (scan after every import)"
}

# ------------------------------------------------------------------ main
main() {
  STACK_DIR="${STACK_DIR:-$PWD/mang-arr-stack}"
  DATA_DIR="${DATA_DIR:-}"
  TZ="${TZ:-$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || echo UTC)}"
  MANGARR_PORT="${MANGARR_PORT:-6789}"
  SUWAYOMI_PORT="${SUWAYOMI_PORT:-4567}"
  KOMGA_PORT="${KOMGA_PORT:-25600}"
  SUWAYOMI_BIND="${SUWAYOMI_BIND:-127.0.0.1}"
  KOMGA_BIND="${KOMGA_BIND:-}"
  MANGARR_IMAGE="${MANGARR_IMAGE:-ghcr.io/andrewhuddleston/mang-arr:latest}"
  MANGARR_USER="${MANGARR_USER:-admin}"
  CONTAINER_PREFIX="${CONTAINER_PREFIX:-}"
  YES="${YES:-0}"
  SOURCES="${SOURCES:-$DEFAULT_SOURCES}"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dir)  [[ $# -ge 2 && -n "$2" ]] || die "--dir needs a folder"; STACK_DIR="$2"; shift 2 ;;
      --data) [[ $# -ge 2 && -n "$2" ]] || die "--data needs a folder"; DATA_DIR="$2"; shift 2 ;;
      --yes|-y) YES=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
  done
  # Nothing here reads stdin: under `curl | bash` it holds the rest of this script. Detach it so no
  # child process can swallow script text either.
  exec </dev/null

  need curl; need python3
  check_dir "--dir/STACK_DIR" "$STACK_DIR" STACK_DIR
  check_dir "--data/DATA_DIR" "${DATA_DIR:-$STACK_DIR/data}" DATA_DIR
  pick_ids "$EUID"
  open_tty
  KOMGA_SECRET_FILE="$STACK_DIR/komga-admin.txt"
  MANGARR_SECRET_FILE="$STACK_DIR/mangarr-login.txt"
  KOMGA_MARK="$STACK_DIR/.komga-unclaimed"

  need docker
  docker compose version >/dev/null 2>&1 || die "docker compose (v2) is required"
  docker info >/dev/null 2>&1 || die "docker is not running or you cannot access it (try: sudo usermod -aG docker \$USER, then log in again)"

  if [[ -z "$KOMGA_BIND" ]]; then
    KOMGA_BIND=127.0.0.1
    if [[ ! -f "$STACK_DIR/docker-compose.yml" ]] \
       && [[ "$(ask "Let phones, tablets and other computers on your network read from Komga? [y/N] " n)" =~ ^[Yy] ]]; then
      KOMGA_BIND=0.0.0.0
    fi
  fi
  check_inputs

  say "mang-arr stack -> $STACK_DIR   data -> $DATA_DIR   user $PUID:$PGID   tz $TZ"
  if [[ -f "$STACK_DIR/docker-compose.yml" ]]; then    # a re-run: the existing file decides, not the defaults
    say "ports: as published by the existing $STACK_DIR/docker-compose.yml (listed at the end)"
  else
    say "ports: mang-arr $MANGARR_PORT (network, with a login), Suwayomi $SUWAYOMI_BIND:$SUWAYOMI_PORT, Komga $KOMGA_BIND:$KOMGA_PORT"
    [[ "$SUWAYOMI_BIND" == 127.* ]] \
      || warn "Suwayomi will be reachable from the network with no login: anyone who reaches it can install extensions (code) in it"
  fi
  if [[ "$YES" != 1 && ! "$(ask "continue? [Y/n] " y)" =~ ^[Yy] ]]; then exit 1; fi

  # Komga certainly has no admin when this run creates the stack and its config folder is empty, or
  # when an earlier run left the marker. Then the login is settled now, before `up`, and the EXIT trap
  # stops Komga should the run end before the claim. Otherwise that is decided only after Komga says
  # it has no admin: a claimed Komga that is slow to answer is never stopped.
  local komga_new=0
  if { [[ ! -f "$STACK_DIR/docker-compose.yml" ]] && komga_fresh; } || [[ -f "$KOMGA_MARK" ]]; then komga_new=1; fi
  load_komga_file
  [[ $komga_new == 0 ]] || ask_komga_login

  WORK=$(mktemp -d "${TMPDIR:-/tmp}/mang-arr-install.XXXXXX")
  chmod 700 "$WORK"
  trap cleanup EXIT
  trap 'exit 130' INT TERM

  # ---------------------------------------------------------------- files
  make_dirs
  write_compose

  # ---------------------------------------------------------------- up
  say "pulling images and starting containers"
  compose pull -q --ignore-pull-failures || warn "some images could not be pulled; using local copies if present"
  [[ $komga_new == 0 ]] || komga_unclaimed
  compose up -d

  # Komga first, claimed the moment it answers: until then whoever reaches it first becomes its admin.
  host_url KOMGA komga 25600 || die "Komga has no published port in $STACK_DIR/docker-compose.yml; the installer needs one"
  local komga_on=$PUBLISHED_ON
  wait_http komga 90 "$KOMGA/api/v1/claim"; ok "Komga is up"
  claim_komga

  SUWA=""; PUBLISHED_ON=""
  host_url SUWA suwayomi 4567 || SUWA=""
  local suwa_on=$PUBLISHED_ON
  host_url MANGARR mangarr 6789 || die "mang-arr has no published port in $STACK_DIR/docker-compose.yml"
  if [[ -n "$SUWA" ]]; then
    wait_http suwayomi 90 "$SUWA/api/graphql?query=%7Bsettings%7BdownloadAsCbz%7D%7D"; ok "Suwayomi is up"
    [[ "$suwa_on" == 127.* ]] \
      || warn "Suwayomi is published on $suwa_on (every interface) with no login; change its ports: line to \"127.0.0.1:${SUWA##*:}:4567\" (docs/INSTALL.md)"
  fi
  wait_http mangarr 60 "$MANGARR/api/v1/ping" "$MANGARR/api/v1/system/status"; ok "mang-arr is up"

  if [[ -n "$SUWA" ]]; then configure_suwayomi
  else warn "Suwayomi has no published port: configure it by hand (docs/INSTALL.md, section 3)"; fi

  say "configuring Komga and mang-arr"
  probe_mangarr
  if [[ $MA_OK == 1 && $MA_HAS_KOMGA == 1 ]]; then
    ok "mang-arr already has a Komga API key; not creating another"
  elif [[ $MA_OK == 1 ]]; then
    komga_key_and_library || warn "add a Komga API key yourself later: mang-arr -> Settings -> Komga"
  fi
  [[ $MA_OK == 0 ]] || configure_mangarr
  if [[ "$(curl -fsS --max-time 60 "$MANGARR/api/v1/health" 2>/dev/null | jsonget ok 2>/dev/null)" =~ ^[Tt]rue$ ]]; then
    ok "mang-arr health: ok"
  else
    warn "mang-arr health reports problems: see System -> Status in mang-arr"
  fi

  local ip where_s where_k khost
  ip=$(hostname -I 2>/dev/null | awk '{print $1}') || ip=""
  where_s="this machine only; from elsewhere: ssh -L ${SUWA##*:}:127.0.0.1:${SUWA##*:} <this host>"
  [[ "$suwa_on" == 127.* ]] || where_s="reachable from the network, no login"
  [[ -n "$SUWA" ]] || where_s="no published port"
  khost=localhost; where_k="this machine only; docs/INSTALL.md shows how to read from other devices"
  if [[ "$komga_on" != 127.* ]]; then khost=${ip:-localhost}; where_k="login: ${KOMGA_EMAIL:-your Komga admin}"; fi
  cat <<EOF

$(printf '\033[1;32mDone.\033[0m')
  mang-arr   http://${ip:-localhost}:${MANGARR##*:}   (Add New, Library Import, Settings)
  Suwayomi   http://localhost:${SUWA##*:}   ($where_s)
  Komga      http://$khost:${KOMGA##*:}   ($where_k)

Next:
  * open mang-arr -> Add New, or Library Import if $DATA_DIR/staging already holds chapters
  * Settings -> Notifications to hear about new chapters and problems
  * everything lives in $STACK_DIR (compose file, config/) and $DATA_DIR (chapters)
EOF
  [[ ! -f "$KOMGA_SECRET_FILE" ]] || printf '  * the generated Komga admin password is in %s; change it in Komga, then delete that file\n' "$KOMGA_SECRET_FILE"
  # the generated mang-arr password is never printed (cloud-init and CI logs would keep it)
  [[ ! -f "$MANGARR_SECRET_FILE" ]] || printf '  * the generated mang-arr login is in %s (readable only by its owner); change the password in Settings -> Security, then delete that file\n' "$MANGARR_SECRET_FILE"
}

# Sourced (the tests do that): define the functions only. Executed or piped: install.
if (return 0 2>/dev/null); then return 0; fi
main "$@"
