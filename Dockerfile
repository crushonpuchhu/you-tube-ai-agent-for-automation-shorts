# Matches the interpreter this project was developed on (.venv is 3.12.3).
FROM python:3.12-slim

# ---------------------------------------------------------------------------
# System packages
#
# agent/clock.py runs `ZoneInfo("Asia/Kolkata")` at import time, and the slim
# image ships WITHOUT the IANA timezone database. Without tzdata the import
# raises ZoneInfoNotFoundError and the whole agent dies before it starts.
# ---------------------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# PYTHONUNBUFFERED makes `docker logs -f` show output immediately; otherwise
# Python buffers stdout and the terminal looks frozen until it exits.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata

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
# appuser write to the mounted volume later.
RUN mkdir -p /app/data/videos /app/data/tokens /app/data/logs /app/auth \
    && chown -R appuser:appuser /app/data /app/auth

# Application code. .env, auth/*.json and data/ are excluded by .dockerignore
# so no secret ever reaches the image.
COPY --chown=appuser:appuser . .

USER appuser

# No EXPOSE: the bot uses Telegram long polling, so every connection is
# outbound HTTPS and there is no inbound port to publish.
CMD ["python", "main.py"]
