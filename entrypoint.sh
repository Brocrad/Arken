#!/bin/sh
set -e
cd /app

# The bot and audio processor keep PID files / a single-instance lock and refuse to start if the
# recorded PID looks alive. After a container restart those PIDs are meaningless (and can collide
# with unrelated processes), so clear them. Safe: only one container runs this folder.
# Per-token locks when ARKEN_TOKEN_ID is set; also clear the legacy single-instance names.
rm -f .arken_streaming.lock .arken_streaming_*.lock \
      .gondor_streaming.lock .gondor_streaming_*.lock \
      request_queue/.audio_processor.pid \
      request_queue/.processor_heartbeat

if [ -z "$NEW_DISCORD_TOKEN" ]; then
    echo "NEW_DISCORD_TOKEN is not set. Put it in .env (see .env.example) or pass it with -e." >&2
    exit 1
fi

STATE="${ARKEN_STATE_DIR:-${GONDOR_STATE_DIR:-/data}}"
mkdir -p "$STATE" "$STATE/metrics" playlists

# exec: the bot becomes the foreground process so signals from `docker stop` reach it.
exec python streaming.py