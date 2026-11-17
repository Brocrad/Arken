"""Per-guild FFmpeg decode profiles (replaces one global ffmpeg_options)."""
from __future__ import annotations

from copy import deepcopy

DEFAULT_REGION = "us"

FFMPEG_PRESETS: dict[str, dict[str, str]] = {
    "us": {
        "before_options": "",
        "options": "-vn -f s16le -ar 48000 -ac 2 -acodec pcm_s16le -loglevel warning",
    },
    "eu": {
        "before_options": "",
        "options": "-vn -f s16le -ar 44100 -ac 2 -acodec pcm_s16le -loglevel warning",
    },
    "hq": {
        "before_options": "",
        "options": "-vn -f s16le -ar 48000 -ac 2 -acodec pcm_s16le -loglevel warning -af volume=1.2",
    },
    "voice": {
        "before_options": "",
        "options": "-vn -f s16le -ar 48000 -ac 1 -acodec pcm_s16le -loglevel warning",
    },
}

REGION_LABELS = {
    "us": "US/English (48kHz stereo)",
    "eu": "European (44.1kHz stereo)",
    "hq": "High quality (48kHz stereo, +volume)",
    "voice": "Voice optimized (48kHz mono)",
}


def normalize_region(name: str | None) -> str:
    if not name:
        return DEFAULT_REGION
    key = str(name).strip().lower()
    return key if key in FFMPEG_PRESETS else DEFAULT_REGION


def ffmpeg_options_for_region(region: str | None) -> dict[str, str]:
    return deepcopy(FFMPEG_PRESETS[normalize_region(region)])


def ffmpeg_options_for_guild(guild_id, guild_setting) -> dict[str, str]:
    """guild_setting(guild_id, key, default=...) from streaming."""
    reg = guild_setting(guild_id, "ffmpeg_region", DEFAULT_REGION) if guild_id else DEFAULT_REGION
    return ffmpeg_options_for_region(reg)