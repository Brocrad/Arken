# syntax=docker/dockerfile:1

# Python 3.12 on purpose: streaming.py imports `audioop`, which was removed in Python 3.13.

# ---- build stage: needs git (discord.py is installed from GitHub) ----
FROM python:3.12-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt


# ---- runtime stage ----
FROM python:3.12-slim

# ffmpeg: audio playback. tini: proper PID 1 (signal forwarding, reaps ffmpeg/yt-dlp children).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. Override the uid/gid at build time if your host user isn't 1000.
ARG UID=1000
ARG GID=1000
RUN groupadd --gid ${GID} arken \
    && useradd --uid ${UID} --gid ${GID} --create-home --shell /usr/sbin/nologin arken

# venv is owned by the bot user so `!deps update` / ARKEN_AUTO_UPDATE_DEPS can pip-upgrade yt-dlp
# (those upgrades live until the container is recreated; rebuild the image to make them permanent).
COPY --from=builder --chown=arken:arken /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    ARKEN_STATE_DIR=/data

WORKDIR /app
# Bot, audio processor and caches all use paths relative to /app, so /app must be writable.
# /data holds persistent state (mount a volume there); /app/playlists is mounted separately.
RUN mkdir -p /app /data /app/playlists \
    && chown -R arken:arken /app /data

COPY --chown=arken:arken *.py requirements.txt entrypoint.sh /app/
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

USER arken

# The audio processor writes a heartbeat every 2s; unhealthy if it stopped for a minute.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import time,pathlib; t=float(pathlib.Path('request_queue/.processor_heartbeat').read_text()); raise SystemExit(0 if time.time()-t < 60 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
