FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    MANGARR_DATA=/config \
    MANGARR_LOG_FILE=/config/mangarr.log \
    MANGARR_STAGING=/staging \
    MANGARR_LIBRARY=/library \
    MANGARR_SUWAYOMI_URL=http://suwayomi:4567

WORKDIR /app
COPY requirements.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir -r requirements.txt
COPY mangarr ./mangarr
RUN pip install --no-cache-dir --no-deps .

# /config: database + log; /staging: Suwayomi's download tree (read);
# /library: the per-series hard-link tree (write; must be on the same
# filesystem as /staging for links, otherwise files are copied)
VOLUME ["/config", "/staging", "/library"]
EXPOSE 6789
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:6789/api/v1/system/status',timeout=4)" || exit 1

CMD ["mangarr", "serve", "--host", "0.0.0.0", "--port", "6789"]
