# Matches the interpreter this project was developed on (.venv is 3.12.3).
FROM python:3.12-slim

# ---------------------------------------------------------------------------
# System packages
#
# agent/clock.py runs `ZoneInfo("Asia/Kolkata")` at import time, and the slim
# image ships WITHOUT the IANA timezone database. Without tzdata the import
# raises ZoneInfoNotFoundError and the whole agent dies before it starts.
#
# ffmpeg + fonts-dejavu-core power the burned-in text-overlay feature
# (pipeline/overlay.py uses the drawtext/libfreetype filter). fonts-dejavu
# supplies the TrueType font drawtext needs to render — without a font on
# disk the overlay fails with a clear error instead of silently skipping.
# ---------------------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Deno — the JavaScript runtime yt-dlp needs to download from YouTube.
#
# Since yt-dlp moved to EJS, YouTube's "n" challenge must be solved by an
# external JS runtime. Without one yt-dlp answers "Only images are available
# for download" and every source download dies with "Requested format is not
# available" — which looked like a cookies/bot-check problem but was not.
# yt-dlp looks for `deno` on PATH and enables it by default (>= 2.3.0).
# The official denoland/deno:bin image is empty, so the release binary is
# fetched here instead. Pin DENO_VERSION to keep builds reproducible.
# ---------------------------------------------------------------------------
ARG DENO_VERSION=2.9.8
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
        amd64) deno_arch=x86_64-unknown-linux-gnu ;; \
        arm64) deno_arch=aarch64-unknown-linux-gnu ;; \
        *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    python -c "import io, os, sys, urllib.request, zipfile; \
url = 'https://github.com/denoland/deno/releases/download/v' + sys.argv[1] + '/deno-' + sys.argv[2] + '.zip'; \
data = urllib.request.urlopen(url, timeout=180).read(); \
zipfile.ZipFile(io.BytesIO(data)).extractall('/usr/local/bin'); \
os.chmod('/usr/local/bin/deno', 0o755)" "$DENO_VERSION" "$deno_arch"; \
    deno --version

# ---------------------------------------------------------------------------
# bgutil PO Token provider — mints YouTube Proof-of-Origin tokens for yt-dlp.
#
# YouTube's playability gate on datacenter IPs (like this container on AWS)
# answers "Sign in to confirm you're not a bot" even when a perfectly valid
# signed-in cookie jar is passed; cookies alone stopped being sufficient there
# in 2025. A PO token is the second half of that handshake — requests must
# prove they come from a browser-shaped client, and bgutil generates those
# attestation tokens by running Google's BotGuard challenge.
#
# Decomposed into two pieces, both installed here:
#  * the yt-dlp plugin  -> `bgutil-ytdlp-pot-provider` in requirements.txt
#  * the token provider -> the server/ script below, invoked per download by
#    the plugin under the same Deno runtime installed above. yt-dlp only uses
#    it when features.source_discovery passes YTDLP_POT_SERVER_HOME, which
#    defaults to /opt/bgutil/server inside the image and is never passed on a
#    machine where that directory is absent (local/dev runs).
#
# BGUTIL_VERSION must match the plugin version in requirements.txt.
# ---------------------------------------------------------------------------
ARG BGUTIL_VERSION=2.0.2
RUN set -eu; \
    python -c "import io, sys, tarfile, urllib.request; \
url = 'https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/' + sys.argv[1] + '.tar.gz'; \
data = urllib.request.urlopen(url, timeout=180).read(); \
tarfile.open(fileobj=io.BytesIO(data), mode='r:gz').extractall('/tmp/bgutil')" "$BGUTIL_VERSION"; \
    mv "/tmp/bgutil/bgutil-ytdlp-pot-provider-$BGUTIL_VERSION" /opt/bgutil; \
    rm -rf /tmp/bgutil; \
    chmod -R a+rX /opt/bgutil
RUN set -eu; cd /opt/bgutil/server \
    && deno install --allow-scripts=npm:canvas --frozen \
    && chmod -R a+rX node_modules

# PYTHONUNBUFFERED makes `docker logs -f` show output immediately; otherwise
# Python buffers stdout and the terminal looks frozen until it exits.
#
# AGENT_DB_PATH: sane default for a plain `docker run` (docker-compose.yml
# sets the same value explicitly). Without it the database would fall back to
# ~/.shorts-agent inside the container's writable layer — an empty database
# that vanishes on every rebuild. Still mount /app/db (and /app/data) to keep it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata \
    AGENT_DB_PATH=/app/db/agent.db

WORKDIR /app

# Dependencies on their own layer first: editing a .py file then rebuilding
# reuses this layer instead of re-downloading everything.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Non-root user. The agent holds a Telegram token, a Gemini key and a YouTube
# OAuth refresh token, so it should not run as root inside the container.
# ---------------------------------------------------------------------------
RUN useradd --create-home --uid 10001 appuser

# Pre-create every path the agent writes to. A named volume inherits the
# ownership of the directory it replaces, so doing this here is what lets
# appuser write to the mounted volume later. /app/db holds the SQLite database
# (compose bind-mounts a host folder over it; the image copy is the fallback
# for a bare `docker run` without mounts, so Database.__init__ can create it).
RUN mkdir -p /app/data/videos /app/data/tokens /app/data/logs /app/db /app/auth \
    && chown -R appuser:appuser /app/data /app/db /app/auth

# Application code. .env, auth/*.json and data/ are excluded by .dockerignore
# so no secret ever reaches the image.
COPY --chown=appuser:appuser . .

USER appuser

# No EXPOSE: the bot uses Telegram long polling, so every connection is
# outbound HTTPS and there is no inbound port to publish.
CMD ["python", "main.py"]
