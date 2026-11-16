#!/bin/sh
set -e
cd /app

# The bot and audio processor keep PID files / a single-instance lock and refuse to start if the
# recorded PID looks alive. After a container restart those PIDs are meaningless (and can collide
# with unrelated processes), so clear them. Safe: only one container runs this folder.
rm -f .gondor_streaming.lock \
      request_queue/.audio_processor.pid \
      request_queue/.processor_heartbeat

if [ -z "$NEW_DISCORD_TOKEN" ]; then
    echo "NEW_DISCORD_TOKEN is not set. Put it in .env (see .env.example) or pass it with -e." >&2
    exit 1
fi

mkdir -p "${GONDOR_STATE_DIR:-/data}" playlists

# exec: the bot becomes the foreground process so signals from `docker stop` reach it.
exec python streaming.py
