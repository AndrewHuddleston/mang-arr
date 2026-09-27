# Base image pinned to an exact release and digest, so every build starts from the same layers;
# Dependabot (.github/dependabot.yml) proposes the updates, and CI rebuilds the image weekly.
FROM python:3.14.6-slim-trixie@sha256:7bec7ddcddeff7975d6ba9b4be7dd6f6b2f55e7491539145e2978f7f97ce9144

# One data mount holds both trees so hard links work (link(2) fails across
# separate mounts even on the same filesystem):
#   /config            database, lock, log
#   /data/staging      Suwayomi's download tree (<Source>/<Series>/*.cbz), read
#   /data/library      the per-series tree built for Komga, written
ENV PYTHONUNBUFFERED=1 \
    MANGARR_DATA=/config \
    MANGARR_LOG_FILE=/config/mangarr.log \
    MANGARR_STAGING=/data/staging \
    MANGARR_LIBRARY=/data/library \
    MANGARR_SUWAYOMI_URL=http://suwayomi:4567

WORKDIR /app
COPY requirements.txt pyproject.toml README.md LICENSE ./
RUN pip install --no-cache-dir -r requirements.txt
COPY mangarr ./mangarr
RUN pip install --no-cache-dir --no-deps . \
 && mkdir -p /config /data && chown 1000:1000 /config /data

VOLUME ["/config", "/data"]
EXPOSE 6789
# Liveness only: /api/v1/ping answers without touching the database, the network or the health
# checks, so a slow Suwayomi, Komga, AniList or MangaDex never marks the container unhealthy.
# /api/v1/health (503 on config problems) is for monitoring, not for restarting the container.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:6789/api/v1/ping',timeout=4)" || exit 1

# run as an unprivileged user; compose can override with user: "PUID:PGID"
USER 1000:1000
CMD ["python", "-m", "mangarr", "serve", "--host", "0.0.0.0", "--port", "6789"]
