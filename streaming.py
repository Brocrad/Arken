#!/usr/bin/env python3
"""
Discord Audio Streaming Bot
A lightweight bot for streaming audio files to Discord voice channels
"""

import os
import sys
import json
import subprocess
import atexit
import time
import uuid
import requests
import shutil
import re
import random
import threading
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

# Load .env before discord so ARKEN_* (or legacy GONDOR_*) affects the voice stack.
try:
    load_dotenv()
except Exception:
    print("WARNING: Skipping .env file due to encoding issues")

# Windows default console encoding (e.g. cp1252) cannot encode emoji in print(); fix before any emoji output.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import discord
from discord.ext import commands

import io
import vapor_style as vapor  # vaporwave/breakcore status text
import donut  # spinning ASCII donut shown while summoning (text fallback)
import gif_art  # animated GIF badges + summon donut (pure stdlib, generated locally)
import math
from array import array
from collections import deque, OrderedDict
from types import SimpleNamespace

import arken_env as aenv
import arken_extras as extras  # effects, sleep timer, stats, likes, lyrics, game helpers (pure logic)
import arken_tenancy as tenancy  # multi-server isolation foundation (guild-scoped personalization)
import arken_metrics as metrics  # Prometheus textfile snapshots for Grafana later
try:
    import audioop  # volume maths for fades (discord.py needs it for volume anyway)
except ImportError:  # pragma: no cover
    audioop = None

# Optional: force IDENTIFY max_dave=0 (classic modes only). Do NOT enable by default — many guilds require DAVE;
# a mismatch causes Discord to drop voice while discord.py reconnects → join/leave loop. Set explicitly in .env if needed.
_use_dave = aenv.truthy("VOICE_USE_DAVE")
_no_dave = aenv.truthy("VOICE_NO_DAVE")
if _no_dave and not _use_dave:

    def _arken_legacy_max_dave_protocol_version(_self) -> int:
        return 0

    discord.voice_state.VoiceConnectionState.max_dave_protocol_version = property(
        _arken_legacy_max_dave_protocol_version
    )
    print(
        "🔧 ARKEN_VOICE_NO_DAVE: voice IDENTIFY uses max_dave=0. "
        "If the bot joins/leaves in a loop, remove this from .env."
    )

# Audio streaming utilities

_LEGACY_FFMPEG = r"C:\Users\Jasper\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg.Essentials_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0-essentials_build\bin\ffmpeg.exe"
_ffmpeg_cached = None


def get_ffmpeg_path():
    """Locate FFmpeg portably: ARKEN_FFMPEG env var -> PATH -> a winget install -> legacy path."""
    global _ffmpeg_cached
    if _ffmpeg_cached:
        return _ffmpeg_cached
    found = None
    env = aenv.get("FFMPEG", "").strip().strip('"')
    if env and os.path.isfile(env):
        found = env
    if not found:
        found = shutil.which("ffmpeg")
    if not found:
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            import glob as _glob
            hits = sorted(_glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "**", "bin", "ffmpeg.exe"), recursive=True))
            if hits:
                found = hits[-1]
    if not found and os.path.isfile(_LEGACY_FFMPEG):
        found = _LEGACY_FFMPEG
    _ffmpeg_cached = found or "ffmpeg"
    return _ffmpeg_cached

# FFmpeg options for audio playback - Optimized for Discord and AAC files
ffmpeg_options = {
    'before_options': '',
    'options': '-vn -f s16le -ar 48000 -ac 2 -acodec pcm_s16le -loglevel warning'
}

# Bot setup - completely generic
# Enable ALL necessary intents explicitly
intents = discord.Intents.default()
intents.message_content = True  # Required to read message content
intents.voice_states = True      # Required for voice channel operations
intents.guild_messages = True    # Required to receive messages in servers
intents.guilds = True            # Required to see guilds/servers
intents.members = False          # Not needed, but can enable if needed

print(f"🔧 Intents configured:")
print(f"   message_content: {intents.message_content}")
print(f"   voice_states: {intents.voice_states}")
print(f"   guild_messages: {intents.guild_messages}")
print(f"   guilds: {intents.guilds}")

bot = commands.Bot(command_prefix='!', intents=intents, description="Arken Discord music bot")


class ArkenHelpCommand(commands.HelpCommand):
    """Readable !help — same guide as !commands; !help <cmd> shows one-line detail."""

    def __init__(self):
        super().__init__(
            command_attrs={"help": "Command guide (same as !commands)"},
            no_category="Commands",
        )

    async def send_bot_help(self, mapping):
        embed = _build_command_guide_embed(self.context.author)
        await self.get_destination().send(embed=embed)

    async def send_command_help(self, command):
        text = command.help or command.brief or "No description."
        embed = discord.Embed(
            title=f"!{command.qualified_name}",
            description=text,
            color=0x7e57c2,
        )
        if command.usage:
            embed.add_field(name="Usage", value=f"`!{command.qualified_name} {command.usage}`", inline=False)
        if command.aliases:
            embed.add_field(
                name="Aliases",
                value=" · ".join(f"`!{a}`" for a in command.aliases),
                inline=False,
            )
        embed.set_footer(text="Full guide: !help · !commands")
        await self.get_destination().send(embed=embed)

    async def send_group_help(self, group):
        await self.send_command_help(group)


bot.help_command = ArkenHelpCommand()

if aenv.get("VOICE_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"):
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger("discord.voice_state").setLevel(logging.DEBUG)
    logging.getLogger("discord.gateway").setLevel(logging.DEBUG)
    print("🔧 ARKEN_VOICE_DEBUG: verbose discord voice logging enabled")

print(f"🔧 discord.py version: {discord.__version__}")
if not aenv.truthy("VOICE_RECONNECT"):
    print("🔧 Voice: auto-reconnect after errors is OFF (stops 4006 loops). Set ARKEN_VOICE_RECONNECT=1 to restore.")

# Directories for communication with background service
MEDIA_DIR = Path("media_library")
QUEUE_DIR = Path("request_queue")
MEDIA_DIR.mkdir(exist_ok=True)
MEDIA_EXTS = (".webm", ".m4a", ".mp4", ".opus", ".mp3", ".ogg")
QUEUE_DIR.mkdir(exist_ok=True)


def _state_file(name: str, default: Path | None = None) -> Path:
    """Where a small persistent JSON state file lives.

    If ARKEN_STATE_DIR is set (e.g. /data in Docker) the file goes there, so one mounted volume holds
    all of it. Otherwise behaviour is unchanged: `default`, or `name` relative to the working directory.
    """
    d = aenv.get("STATE_DIR", "").strip()
    if d:
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        return p / name
    return default if default is not None else Path(name)


def _resolve_state_json(primary: str, legacy: str) -> Path:
    """Prefer Arken state filenames; use legacy names only if they already exist on disk."""
    root = Path(__file__).resolve().parent
    primary_p = _state_file(primary, root / primary)
    legacy_p = _state_file(legacy, root / legacy)
    if primary_p.is_file():
        return primary_p
    if legacy_p.is_file():
        return legacy_p
    return primary_p


# Background audio processor (audio_processor_service.py) — spawned automatically unless already running
_audio_processor_child: subprocess.Popen | None = None


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return str(pid) in (r.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _sanitize_user_query(q: str) -> str:
    """Strip invisible Unicode that breaks Windows / yt-dlp (e.g. ZWSP after !play)."""
    if not q:
        return q
    for ch in (
        "\u200b",
        "\u200c",
        "\u200d",
        "\ufeff",
        "\u2060",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
    ):
        q = q.replace(ch, "")
    return q.strip()


VOICE_LATENCY_GRACE = 30.0  # seconds after a join during which "no heartbeat yet" is normal


def _mark_voice_joined(vc) -> None:
    """Remember when we joined: discord.py reports latency=inf until the first voice heartbeat is acked."""
    try:
        vc._arken_joined_at = time.monotonic()
    except Exception:
        pass


def _voice_session_usable(vc: discord.VoiceClient | None) -> bool:
    """True only if voice socket looks live (avoids 'connected' after 4006)."""
    if vc is None or not vc.is_connected():
        return False
    try:
        if vc.latency != float("inf"):
            return True
    except Exception:
        return True
    # Connected but no heartbeat ack yet. Right after joining that is normal (the first voice
    # heartbeat can take ~10+ s), so don't report a healthy fresh join as broken.
    joined = getattr(vc, "_arken_joined_at", None)
    return joined is not None and (time.monotonic() - joined) < VOICE_LATENCY_GRACE


def _ensure_single_streaming_instance() -> None:
    """Exclusive lock — two streaming.py processes on the same token cause 4006 + duplicate messages.

    Multi-token pools run one process per ARKEN_TOKEN_ID; each uses its own lock file.
    """
    if aenv.truthy("ALLOW_MULTI_INSTANCE"):
        return
    token_tag = (aenv.get("TOKEN_ID") or "").strip()
    lock_name = f".arken_streaming_{token_tag}.lock" if token_tag else ".arken_streaming.lock"
    lock_path = _project_root() / lock_name
    pid_bytes = str(os.getpid()).encode("ascii")

    def try_create_lock() -> bool:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, pid_bytes)
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            return False

    if try_create_lock():
        def release() -> None:
            try:
                if not lock_path.is_file():
                    return
                cur = lock_path.read_text(encoding="ascii", errors="ignore").strip()
                if cur == str(os.getpid()):
                    lock_path.unlink(missing_ok=True)
            except OSError:
                pass

        atexit.register(release)
        return

    try:
        raw = lock_path.read_text(encoding="ascii", errors="ignore").strip()
        old = int(raw) if raw else 0
        if old and _pid_exists(old):
            print("❌ Another streaming.py is already running (same folder).")
            print("   Two bots on one token → voice 4006 and duplicate replies. Close the other process.")
            print(f"   Locked by PID {old}. Remove {lock_path.name} only if that process is dead.")
            sys.exit(3)
    except (ValueError, OSError):
        pass

    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass

    if not try_create_lock():
        print(f"❌ Could not create {lock_path.name}. Exit.")
        sys.exit(3)

    def release2() -> None:
        try:
            if lock_path.is_file():
                cur = lock_path.read_text(encoding="ascii", errors="ignore").strip()
                if cur == str(os.getpid()):
                    lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(release2)


def _audio_processor_already_running() -> bool:
    pid_file = _project_root() / "request_queue" / ".audio_processor.pid"
    if not pid_file.exists():
        return False
    try:
        return _pid_exists(int(pid_file.read_text().strip()))
    except (ValueError, OSError):
        return False


def _kill_audio_processor_from_pidfile() -> None:
    pid_file = _project_root() / "request_queue" / ".audio_processor.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
    except Exception as e:
        print(f"⚠️ Could not stop old audio processor (PID {pid}): {e}")
    time.sleep(1.0)
    try:
        pid_file.unlink()
    except OSError:
        pass


def _ensure_audio_processor_subprocess() -> None:
    """Start queue consumer unless ARKEN_SKIP_AUDIO_PROCESSOR=1 or another instance is alive."""
    if aenv.get("SKIP_AUDIO_PROCESSOR", "").strip() in ("1", "true", "yes"):
        print("⚠️ Skipping audio processor spawn (ARKEN_SKIP_AUDIO_PROCESSOR)")
        return
    if _audio_processor_already_running():
        pid_file = _project_root() / "request_queue" / ".audio_processor.pid"
        try:
            just_started = (time.time() - pid_file.stat().st_mtime) < 15
        except OSError:
            just_started = False
        if just_started or _processor_heartbeat_age() < 15:
            print("🎵 Audio processor already running (shared queue)")
            return
        # Alive but silent: an older build without the heartbeat, or hung
        print("♻️ Audio processor has no heartbeat (old version or hung) — restarting it")
        _kill_audio_processor_from_pidfile()
    global _audio_processor_child
    script = _project_root() / "audio_processor_service.py"
    print("🎵 Starting background audio processor...")
    _audio_processor_child = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(_project_root()),
    )
    time.sleep(1.5)
    if _audio_processor_child.poll() is not None:
        if _audio_processor_child.returncode == 0:
            print("🎵 Audio processor singleton: another worker is active")
        else:
            print(f"❌ Audio processor exited immediately (code {_audio_processor_child.returncode})")
    else:
        print("✅ Audio processor running")


def _terminate_audio_processor_child() -> None:
    global _audio_processor_child
    if _audio_processor_child is None or _audio_processor_child.poll() is not None:
        return
    try:
        _audio_processor_child.terminate()
        _audio_processor_child.wait(timeout=8)
    except Exception:
        try:
            _audio_processor_child.kill()
        except Exception:
            pass


atexit.register(_terminate_audio_processor_child)

# Track current playback
current_sources = {}

# Playlist root. Each Discord server's playlists live under playlists/<guild_id>/ only.
PLAYLIST_DIR = Path("playlists")
PLAYLIST_DIR.mkdir(exist_ok=True)


def _guild_playlists(guild_id) -> Path:
    """playlists/<guild_id>/ — never the shared root."""
    return tenancy.playlist_dir(PLAYLIST_DIR, guild_id)

# Playlist queue management with smart features
playlist_queues = {}  # {guild_id: {"playlist_name": str, "songs": list, "original_songs": list, "current_index": int, "shuffle": bool, "loop_mode": str, "total_songs": int}}

# Smart queue system for individual !play songs
song_queues = {}  # {guild_id: [{"title": str, "file_path": str, "duration": int, "requested_by": User, "cached": bool}, ...]}
# Next after_playing() after voice_client.stop() must continue the playlist (not !play song_queues)
playlist_force_after_stop = set()
# play_next_playlist_song stops the current track itself: that stop's after_playing must not advance again
suppress_after_playing = set()
# guilds currently inside play_next_playlist_song (drops duplicate concurrent advances)
_playlist_advancing = set()
_playlist_advance_pending = set()
notification_channels = {}  # {guild_id: channel} - tracks where to send music notifications

# Voice channel state management
VOICE_STATE_FILE = _state_file("voice_state.json")
PLAYBACK_STATE_FILE = _state_file("playback_state.json")

def set_notification_channel(guild_id, channel):
    """Set the notification channel for a guild"""
    notification_channels[guild_id] = channel
    try:
        cid = getattr(channel, "id", None)
        if cid and guild_setting(guild_id, "notify_channel") != cid:
            set_guild_setting(guild_id, "notify_channel", cid)
    except NameError:
        pass  # settings helpers aren't defined yet (very early call)

def get_notification_channel(guild_id):
    """Get the notification channel for a guild"""
    return notification_channels.get(guild_id)

def save_voice_state():
    """Save current voice channel connections to file"""
    try:
        voice_state = {}
        
        for guild in bot.guilds:
            if guild.voice_client and guild.voice_client.channel:
                voice_state[str(guild.id)] = {
                    'channel_id': guild.voice_client.channel.id,
                    'channel_name': guild.voice_client.channel.name,
                    'guild_name': guild.name,
                    'saved_time': time.time()
                }
        
        with open(VOICE_STATE_FILE, 'w') as f:
            json.dump(voice_state, f, indent=2)
        
        print(f"💾 Saved voice state for {len(voice_state)} guilds")
        return True
        
    except Exception as e:
        print(f"⚠️ Error saving voice state: {e}")
        return False

def load_voice_state():
    """Load saved voice channel connections"""
    try:
        if VOICE_STATE_FILE.exists():
            with open(VOICE_STATE_FILE, 'r') as f:
                voice_state = json.load(f)
            
            # Clean up old state files (older than 1 hour)
            current_time = time.time()
            cleaned_state = {}
            
            for guild_id, state in voice_state.items():
                if current_time - state.get('saved_time', 0) < 3600:  # 1 hour
                    cleaned_state[guild_id] = state
            
            print(f"📂 Loaded voice state for {len(cleaned_state)} guilds")
            return cleaned_state
        
        return {}
        
    except Exception as e:
        print(f"⚠️ Error loading voice state: {e}")
        return {}

def clear_voice_state():
    """Clear saved voice state file"""
    try:
        if VOICE_STATE_FILE.exists():
            VOICE_STATE_FILE.unlink()
            print("🧹 Cleared voice state file")
    except Exception as e:
        print(f"⚠️ Error clearing voice state: {e}")

_voice_connect_locks: dict[int, asyncio.Lock] = {}


def _voice_connect_lock(guild_id: int) -> asyncio.Lock:
    lock = _voice_connect_locks.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _voice_connect_locks[guild_id] = lock
    return lock


async def _await_voice_connected(voice_client: discord.VoiceClient, timeout: float) -> bool:
    """Poll until connected — avoids VoiceClient.wait_until_connected() (threading wait blocks the event loop)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if voice_client.is_connected():
            return True
        await asyncio.sleep(0.05)
    return voice_client.is_connected()


async def connect_to_voice_channel(
    guild: discord.Guild,
    channel: discord.VoiceChannel | discord.StageChannel,
    *,
    timeout: float = 30.0,
) -> discord.VoiceClient:
    """
    One serialized voice join per guild: move_to when possible, otherwise force-disconnect
    and reconnect. Reduces stale sessions / voice WebSocket 4006 from overlapping handshakes
    (e.g. !summon racing with on_ready rejoin).
    """
    async with _voice_connect_lock(guild.id):
        vc = guild.voice_client

        if vc is not None:
            same_ch = vc.channel == channel
            if same_ch and _voice_session_usable(vc):
                return vc

            if _voice_session_usable(vc) and not same_ch:
                try:
                    await asyncio.wait_for(vc.move_to(channel), timeout=timeout)
                    moved = guild.voice_client
                    if moved and await _await_voice_connected(moved, min(timeout, 20.0)):
                        _mark_voice_joined(moved)
                        return moved
                except Exception as e:
                    print(f"⚠️ move_to failed: {type(e).__name__}: {e}")

            try:
                vc.stop()
            except Exception:
                pass
            try:
                await vc.disconnect(force=True)
            except Exception as e:
                print(f"⚠️ voice disconnect(force=True): {e}")
            await asyncio.sleep(1.0)

        connect_timeout = min(max(timeout, 10.0), 90.0)
        voice_reconnect = aenv.truthy("VOICE_RECONNECT")
        new_vc = await asyncio.wait_for(
            channel.connect(
                timeout=connect_timeout,
                reconnect=voice_reconnect,
                self_deaf=False,
                self_mute=False,
            ),
            timeout=connect_timeout + 20.0,
        )
        if not await _await_voice_connected(new_vc, min(timeout, 30.0)):
            try:
                await new_vc.disconnect(force=True)
            except Exception:
                pass
            raise discord.ClientException(
                "Voice connection did not reach a stable connected state in time."
            )
        _mark_voice_joined(new_vc)
        return new_vc

async def rejoin_voice_channels():
    """Rejoin voice channels after restart"""
    voice_state = load_voice_state()
    
    if not voice_state:
        print("📢 No voice channels to rejoin")
        return
    
    rejoined_count = 0
    
    for guild_id_str, state in voice_state.items():
        try:
            guild_id = int(guild_id_str)
            guild = bot.get_guild(guild_id)
            
            if not guild:
                print(f"⚠️ Guild {state['guild_name']} not found, skipping")
                continue

            if not guild_served_here(guild_id):
                print(f"⚠️ Guild {guild.name} not served by this token, skipping rejoin")
                continue
            
            # Find the voice channel
            channel = guild.get_channel(state['channel_id'])
            
            if not channel:
                print(f"⚠️ Voice channel {state['channel_name']} not found in {guild.name}")
                continue

            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                print(f"⚠️ Channel {channel} is not a voice/stage channel in {guild.name}")
                continue
            
            # Check permissions
            if not channel.permissions_for(guild.me).connect:
                print(f"❌ No permission to connect to {channel.name} in {guild.name}")
                continue
            
            await connect_to_voice_channel(guild, channel, timeout=60.0)
            print(f"🔊 Rejoined voice channel: {channel.name} in {guild.name}")
            rejoined_count += 1
            
            # Small delay between connections
            await asyncio.sleep(1)
            
        except Exception as e:
            print(f"❌ Error rejoining {state.get('channel_name', 'unknown')} in {state.get('guild_name', 'unknown')}: {e}")
    
    if rejoined_count > 0:
        print(f"✅ Successfully rejoined {rejoined_count} voice channels")
        # Clear the state file after successful rejoin
        clear_voice_state()
    else:
        print("📢 No voice channels were rejoined")

def save_playback_state():
    """Save current playback state and queues"""
    try:
        playback_state = {}
        
        for guild in bot.guilds:
            guild_id = guild.id
            
            # Check if bot is in voice channel and something is playing
            if guild.voice_client and (guild.voice_client.is_playing() or guild.voice_client.is_paused()):
                current_source = current_sources.get(guild_id)
                
                if current_source and hasattr(current_source, 'title'):
                    guild_state = {
                        'voice_channel_id': guild.voice_client.channel.id,
                        'voice_channel_name': guild.voice_client.channel.name,
                        'guild_name': guild.name,
                        'current_song': {
                            'title': current_source.title,
                            'file_path': current_source.file_path,
                            'duration': current_source.duration
                        },
                        'was_playing': guild.voice_client.is_playing(),
                        'was_paused': guild.voice_client.is_paused(),
                        'saved_time': time.time()
                    }
                    
                    # Save song queue if it exists
                    if guild_id in song_queues and song_queues[guild_id]:
                        guild_state['song_queue'] = []
                        for song in song_queues[guild_id]:
                            # Convert User object to ID for JSON serialization
                            song_data = song.copy()
                            if hasattr(song_data.get('requested_by'), 'id'):
                                song_data['requested_by_id'] = song_data['requested_by'].id
                                song_data['requested_by_name'] = song_data['requested_by'].name
                                del song_data['requested_by']
                            guild_state['song_queue'].append(song_data)
                    
                    # Save playlist state if active
                    if guild_id in playlist_queues:
                        playlist_state = playlist_queues[guild_id].copy()
                        # Convert any non-serializable objects
                        guild_state['playlist_state'] = playlist_state
                    
                    # Save notification channel
                    if guild_id in notification_channels:
                        guild_state['notification_channel_id'] = notification_channels[guild_id].id
                    
                    playback_state[str(guild_id)] = guild_state
        
        if playback_state:
            with open(PLAYBACK_STATE_FILE, 'w') as f:
                json.dump(playback_state, f, indent=2)
            
            print(f"💾 Saved playback state for {len(playback_state)} guilds")
            return True
        else:
            print("📢 No active playback to save")
            return False
        
    except Exception as e:
        print(f"⚠️ Error saving playback state: {e}")
        return False

def load_playback_state():
    """Load saved playback state"""
    try:
        if PLAYBACK_STATE_FILE.exists():
            with open(PLAYBACK_STATE_FILE, 'r') as f:
                playback_state = json.load(f)
            
            # Clean up old state files (older than 1 hour)
            current_time = time.time()
            cleaned_state = {}
            
            for guild_id, state in playback_state.items():
                if current_time - state.get('saved_time', 0) < 3600:  # 1 hour
                    cleaned_state[guild_id] = state
            
            print(f"📂 Loaded playback state for {len(cleaned_state)} guilds")
            return cleaned_state
        
        return {}
        
    except Exception as e:
        print(f"⚠️ Error loading playback state: {e}")
        return {}

def clear_playback_state():
    """Clear saved playback state file"""
    try:
        if PLAYBACK_STATE_FILE.exists():
            PLAYBACK_STATE_FILE.unlink()
            print("🧹 Cleared playback state file")
    except Exception as e:
        print(f"⚠️ Error clearing playback state: {e}")

async def restore_playback_state():
    """Restore playback state after restart"""
    playback_state = load_playback_state()
    
    if not playback_state:
        print("📢 No playback state to restore")
        return
    
    restored_count = 0
    
    for guild_id_str, state in playback_state.items():
        try:
            guild_id = int(guild_id_str)
            guild = bot.get_guild(guild_id)
            
            if not guild:
                print(f"⚠️ Guild {state['guild_name']} not found, skipping playback restore")
                continue
            
            # Check if bot is in voice channel (should be from auto-rejoin)
            if not guild.voice_client:
                print(f"⚠️ Not in voice channel for {guild.name}, skipping playback restore")
                continue
            
            voice_client = guild.voice_client
            current_song = state.get('current_song')
            
            if not current_song or not current_song.get('file_path'):
                print(f"⚠️ No valid current song for {guild.name}")
                continue
            
            # Check if the file still exists
            if not Path(current_song['file_path']).exists():
                print(f"⚠️ Song file no longer exists: {current_song['title']}")
                continue
            
            print(f"🎵 Restoring playback in {guild.name}: {current_song['title']}")
            
            # Restore notification channel
            if state.get('notification_channel_id'):
                channel = guild.get_channel(state['notification_channel_id'])
                if channel:
                    set_notification_channel(guild_id, channel)
            
            # Restore song queue
            if state.get('song_queue'):
                restored_queue = []
                for song_data in state['song_queue']:
                    # Restore User object from ID
                    if 'requested_by_id' in song_data:
                        try:
                            user = await bot.fetch_user(song_data['requested_by_id'])
                            song_data['requested_by'] = user
                            del song_data['requested_by_id']
                            del song_data['requested_by_name']
                        except:
                            # If user can't be fetched, create a dummy user reference
                            song_data['requested_by'] = type('User', (), {
                                'id': song_data.get('requested_by_id', 0),
                                'name': song_data.get('requested_by_name', 'Unknown'),
                                'mention': f"<@{song_data.get('requested_by_id', 0)}>"
                            })()
                    
                    restored_queue.append(song_data)
                
                song_queues[guild_id] = restored_queue
                print(f"📋 Restored {len(restored_queue)} songs in queue")
            
            # Restore playlist state
            if state.get('playlist_state'):
                playlist_queues[guild_id] = state['playlist_state']
                print(f"🎵 Restored playlist state")
                _restore_stream_session(guild_id)
            
            # Create audio source and resume playback
            try:
                file_path = current_song['file_path']
                audio_source = make_audio_source(guild_id, file_path, title=current_song['title'],
                                                 duration=current_song.get('duration', 0),
                                                 video_id=current_song.get('video_id'))
                
                current_sources[guild_id] = audio_source
                
                # Start playing
                voice_client.play(audio_source, after=lambda e: after_playing(e, guild_id))
                
                # Pause if it was paused before reboot
                if state.get('was_paused', False):
                    voice_client.pause()
                    print(f"⏸️ Resumed in paused state")
                
                print(f"✅ Restored playback: {current_song['title']}")
                restored_count += 1
                
                # Send notification about restoration
                notification_channel = get_notification_channel(guild_id)
                if notification_channel:
                    embed = discord.Embed(
                        title="🔄 Playback Restored",
                        description=f"**{current_song['title']}**",
                        color=0x00ff00
                    )
                    
                    embed.add_field(
                        name="Status",
                        value="▶️ Playing" if state.get('was_playing') else "⏸️ Paused",
                        inline=True
                    )
                    
                    if state.get('song_queue'):
                        embed.add_field(
                            name="Queue",
                            value=f"📋 {len(state['song_queue'])} songs restored",
                            inline=True
                        )
                    
                    embed.set_footer(text="Restored after bot restart")
                    
                    try:
                        await notification_channel.send(embed=embed)
                    except:
                        pass  # Don't fail if notification can't be sent
                
                # Small delay between restorations
                await asyncio.sleep(1)
                
            except Exception as e:
                print(f"❌ Error restoring playback for {current_song['title']}: {e}")
                continue
                
        except Exception as e:
            print(f"❌ Error restoring playback for guild {state.get('guild_name', 'unknown')}: {e}")
    
    if restored_count > 0:
        print(f"✅ Successfully restored playback for {restored_count} guilds")
        # Clear the state file after successful restoration
        clear_playback_state()
    else:
        print("📢 No playback was restored")

# ============================================================================
# 🛡️ ACCESS CONTROL — three layers
#
#   1. Staff (user_whitelist.json)     global elevation tiers:
#        owner     manage staff/guilds/runtime + diagnose + DJ in every server
#        operator  manage guilds/runtime + diagnose — NOT cross-server DJ
#      Only owner gets global_control. Playback in a friend’s server follows THAT server’s
#      openplay / DJ role / Manage Server — operators are not elevated there.
#   2. Server policy (per guild)       openplay (default ON) · DJ role · Manage Server
#   3. Registry (allowed_guilds.json)  which Discord servers may use Arken at all
#
#   Command bands inside an approved server:
#        user          summon, queue, like, history, stats, dashboard, voteskip, …
#        dj / control  play, skip, volume, … (openplay OR dj role OR Manage Server OR owner)
#        server admin  !openplay, !djrole (Discord Manage Server, or staff owner)
#        staff         !guilds, !reboot, !whitelist, … (capability-gated)
# ============================================================================

WHITELIST_FILE = _state_file("user_whitelist.json")
STAFF = tenancy.StaffDirectory(WHITELIST_FILE)


def _bootstrap_staff_from_env() -> int:
    """Ensure ARKEN_BOOTSTRAP_OWNERS ids are owners (adds missing ids only; persists to STATE_DIR)."""
    raw = aenv.get("BOOTSTRAP_OWNERS", "").strip()
    if not raw:
        return 0
    added = 0
    for part in re.split(r"[\s,;]+", raw):
        part = part.strip()
        if not part.isdigit():
            continue
        uid = int(part)
        if STAFF.is_staff(uid):
            continue
        try:
            STAFF.set_role(uid, "owner")
            added += 1
            print(f"🛡️ Bootstrap owner added: {uid}")
        except Exception as e:
            print(f"⚠️ Could not bootstrap owner {uid}: {e}")
    return added


_bootstrap_staff_from_env()
GUILD_REGISTRY = tenancy.GuildRegistry(_state_file("allowed_guilds.json"))
LEAVE_UNAPPROVED = aenv.get("LEAVE_UNAPPROVED", "").strip().lower() in (
    "1", "true", "yes", "on",
)

# Multi-token pool: sticky guild → bot-token assignment (see arken_tenancy.TokenPool).
# Single-bot installs leave ARKEN_TOKEN_POOL unset and behave exactly as before.
TOKEN_ID = (aenv.get("TOKEN_ID") or "").strip() or None
_pool_ids = tenancy.parse_token_pool_env(aenv.get("TOKEN_POOL"))
if not _pool_ids and TOKEN_ID:
    _pool_ids = [TOKEN_ID]


TOKEN_POOL = tenancy.TokenPool(
    _state_file("token_pool.json"),
    token_ids=_pool_ids,
    max_guilds_per_token=aenv.int_value("MAX_GUILDS_PER_TOKEN", tenancy.DEFAULT_MAX_GUILDS_PER_TOKEN),
    weight_guild=aenv.float_value("POOL_WEIGHT_GUILD", tenancy.DEFAULT_WEIGHT_GUILD),
    weight_active=aenv.float_value("POOL_WEIGHT_ACTIVE", tenancy.DEFAULT_WEIGHT_ACTIVE),
    weight_members=aenv.float_value("POOL_WEIGHT_MEMBERS", tenancy.DEFAULT_WEIGHT_MEMBERS),
)
if TOKEN_POOL.enabled() and not TOKEN_ID:
    print(
        "⚠️ ARKEN_TOKEN_POOL is set but ARKEN_TOKEN_ID is missing — "
        "this process will not serve any assigned guilds until TOKEN_ID is set."
    )

_METRICS_ENABLED = aenv.get("METRICS", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
_METRICS_WRITER = None
if _METRICS_ENABLED:
    _metrics_dir = aenv.get("STATE_DIR", "").strip() or str(_project_root())
    _METRICS_WRITER = metrics.MetricsWriter(
        metrics.default_metrics_path(_metrics_dir, TOKEN_ID)
    )


def load_whitelist():
    """Staff user ids (any role). Kept for older call sites / sync helpers."""
    return STAFF.ids()


def save_whitelist(authorized_users):
    """Replace staff list; unknown ids become owners. Prefer STAFF.set_role / remove."""
    keep = {int(u) for u in authorized_users}
    for uid in list(STAFF.ids()):
        if uid not in keep:
            STAFF.remove(uid)
    for uid in keep:
        if STAFF.role_of(uid) is None:
            STAFF.set_role(uid, "owner")
    return True


def staff_role(user_id) -> str | None:
    return STAFF.role_of(user_id)


def staff_has(user_id, capability: str) -> bool:
    return STAFF.has(user_id, capability)


def is_owner(user_id) -> bool:
    """Staff role 'owner' (highest tier). Not the same as Discord server admin."""
    return staff_role(user_id) == "owner"


def is_authorized(user_id) -> bool:
    """Any staff member (owner / operator)."""
    return STAFF.is_staff(user_id)


def guild_is_approved(guild_id) -> bool:
    """Whether this Discord server may use Arken. Until the registry exists, allow (seeded on ready)."""
    if not GUILD_REGISTRY.initialized:
        return True
    return GUILD_REGISTRY.is_approved(guild_id)


def guild_served_here(guild_id) -> bool:
    """Approved for Arken AND assigned to this process's token (when the pool is enabled)."""
    if not guild_is_approved(guild_id):
        return False
    if not TOKEN_POOL.enabled():
        return True
    if not TOKEN_ID:
        return False
    return TOKEN_POOL.token_of(guild_id) == TOKEN_ID


async def _sync_seat_nickname(guild) -> bool:
    """Set this bot's *per-server* nickname to the full seat (Token · Location).

    One Discord bot (city-token) sits in up to 5 guilds. Each guild gets a different nickname
    so members see e.g. "Osgiliath · East Bank" in the member list — not bare "Osgiliath".
    Requires Change Nickname (or Manage Nicknames) for the bot. No-op if pool is off.
    """
    if guild is None or not TOKEN_POOL.enabled():
        return False
    if TOKEN_ID and TOKEN_POOL.token_of(guild.id) not in (None, TOKEN_ID):
        return False
    seat = TOKEN_POOL.seat_of(guild.id)
    if not seat:
        return False
    # Discord nicknames max out at 32 characters.
    nick = seat if len(seat) <= 32 else (seat[:31] + "…")
    me = guild.me
    if me is None:
        return False
    current = me.nick or me.display_name
    if current == nick:
        return True
    try:
        await me.edit(nick=nick, reason=f"Arken seat: {seat}")
        print(f"🛡️ Nickname in {guild.name} → {nick}")
        return True
    except Exception as e:
        print(
            f"⚠️ Could not set seat nickname in {guild.name} to '{nick}': {e} "
            f"(need Change Nickname permission)"
        )
        return False


def _guild_member_count(guild) -> int:
    try:
        n = getattr(guild, "member_count", None)
        if n is not None:
            return max(0, int(n))
    except Exception:
        pass
    try:
        return max(0, len(guild.members))
    except Exception:
        return 0


async def _pool_claim_from_invite(guild) -> bool:
    """Lock a guild to this OAuth bot (ARKEN_TOKEN_ID) when it joins via invite.

    The Discord application used in the invite URL is the source of truth for scaling:
    Gondor invite → gondor seat; Osgiliath invite → osgiliath. Returns False if this process left.
    """
    if guild is None or not TOKEN_POOL.enabled() or not TOKEN_ID:
        return True
    members = _guild_member_count(guild)
    owner = TOKEN_POOL.token_of(guild.id)
    if owner is not None and owner != TOKEN_ID:
        city = tenancy.display_token_name(owner)
        here = tenancy.display_token_name(TOKEN_ID)
        print(
            f"🛡️ {guild.name} ({guild.id}) is assigned to **{owner}** ({city}) "
            f"but joined via **{TOKEN_ID}** ({here}) — leaving"
        )
        try:
            await guild.leave()
        except Exception as e:
            print(f"⚠️ Could not leave {guild.name}: {e}")
        return False
    try:
        TOKEN_POOL.assign(
            guild.id,
            token_id=TOKEN_ID,
            members=members,
            name=guild.name,
            source="invite",
        )
        seat = TOKEN_POOL.seat_of(guild.id)
        print(f"🛡️ Invite claim {guild.name} ({guild.id}) → {seat or TOKEN_ID}")
        if guild_is_approved(guild.id):
            await _sync_seat_nickname(guild)
        return True
    except (tenancy.PoolFullError, tenancy.CallsignExhaustedError) as e:
        print(f"⚠️ Cannot claim {guild.name} on {TOKEN_ID}: {e}")
        try:
            await guild.leave()
        except Exception:
            pass
        return False


def _count_active_players() -> int:
    n = 0
    for g in bot.guilds:
        if not guild_served_here(g.id):
            continue
        vc = g.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            n += 1
    return n


def _write_pool_metrics() -> None:
    if not _METRICS_WRITER:
        return
    try:
        snap = TOKEN_POOL.snapshot() if TOKEN_POOL.enabled() else {
            "enabled": False,
            "max_guilds_per_token": tenancy.DEFAULT_MAX_GUILDS_PER_TOKEN,
            "token_count": 0,
            "assigned_guilds": 0,
            "tokens": [],
        }
        _METRICS_WRITER.write(metrics.render_pool_prometheus(snap, token_id=TOKEN_ID))
    except Exception as e:
        print(f"⚠️ metrics write failed: {e}")


async def _pool_stats_loop():
    """Refresh per-token active players + member counts; emit Prometheus textfile."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            if TOKEN_POOL.enabled() and TOKEN_ID:
                dirty = False
                for g in bot.guilds:
                    if TOKEN_POOL.token_of(g.id) == TOKEN_ID:
                        dirty = (
                            TOKEN_POOL.update_guild_members(
                                g.id, _guild_member_count(g), name=g.name, persist=False
                            )
                            or dirty
                        )
                        await _sync_seat_nickname(g)
                TOKEN_POOL.set_runtime(
                    TOKEN_ID, active_players=_count_active_players(), persist=False
                )
                if dirty:
                    TOKEN_POOL.touch()
                else:
                    # Still refresh runtime timestamp occasionally for other processes.
                    TOKEN_POOL.touch()
            _write_pool_metrics()
        except Exception as e:
            print(f"⚠️ pool stats loop: {e}")
        await asyncio.sleep(30)


def openplay_enabled(guild_id) -> bool:
    """Open play defaults ON so a newly approved server works without setup."""
    return bool(guild_setting(guild_id, "openplay", tenancy.DEFAULT_OPENPLAY))


def _member_is_admin(member) -> bool:
    try:
        return bool(member.guild_permissions.manage_guild or member.guild_permissions.administrator)
    except Exception:
        return False


def _member_has_dj(member) -> bool:
    role_id = guild_setting(member.guild.id, "dj_role")
    if not role_id:
        return False
    try:
        rid = int(role_id)
    except (TypeError, ValueError):
        return False
    return any(r.id == rid for r in getattr(member, "roles", []))


def member_can_control(member) -> bool:
    """DJ / playback controls in this server.

    Cross-server elevation is owner-only. Operators are not DJs in friends' servers unless
    that server's own openplay / DJ role / Manage Server allows them.
    """
    if member is None or getattr(member, "guild", None) is None:
        return False
    return tenancy.can_control(
        global_control=is_owner(member.id),
        is_admin=_member_is_admin(member),
        has_dj_role=_member_has_dj(member),
        open_play=openplay_enabled(member.guild.id),
    )


def member_can_server_admin(member) -> bool:
    """Change this server's Arken policy (!openplay, !djrole)."""
    if member is None or getattr(member, "guild", None) is None:
        return False
    return tenancy.can_server_admin(
        is_staff_owner=is_owner(member.id),
        is_guild_admin=_member_is_admin(member),
    )


async def _deny_unapproved(ctx) -> bool:
    """True if the command should stop (guild missing, not approved, or not on this token)."""
    if ctx.guild is None:
        await ctx.send("❌ This command only works inside a Discord server.")
        return True
    if not guild_is_approved(ctx.guild.id):
        st = GUILD_REGISTRY.status(ctx.guild.id)
        await ctx.send(
            f"❌ This server is **{st}**. Staff must approve it with "
            f"`!allowguild {ctx.guild.id}` before Arken will work here."
        )
        return True
    if not guild_served_here(ctx.guild.id):
        seat = TOKEN_POOL.seat_of(ctx.guild.id) if TOKEN_POOL.enabled() else None
        tip = f" It is seated as **{seat}**." if seat else ""
        await ctx.send(
            f"❌ This server is approved but not served by this bot instance.{tip}"
        )
        return True
    return False


def require_staff(*capabilities: str):
    """Decorator: staff member with ALL listed capabilities (default: any staff)."""
    import functools
    needed = tuple(capabilities)

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            ctx = args[0] if args else None
            if not ctx or not hasattr(ctx, "author"):
                return await func(*args, **kwargs)
            uid = ctx.author.id
            if not STAFF.is_staff(uid):
                print(f"🚫 Staff denied: {ctx.author} ({uid}) tried {getattr(ctx.command, 'name', '?')}")
                hint = ""
                if not STAFF.ids():
                    hint = (
                        " Staff list is **empty** on this instance — put owners in "
                        f"`{WHITELIST_FILE}` or set `ARKEN_BOOTSTRAP_OWNERS` in `.env` and restart."
                    )
                else:
                    hint = f" Your Discord id is `{uid}`; staff file is `{WHITELIST_FILE}`."
                await ctx.send(f"❌ **Access denied.** Arken staff only.{hint}")
                return
            missing = [c for c in needed if not staff_has(uid, c)]
            if missing:
                role = staff_role(uid) or "?"
                print(f"🚫 Capability denied ({missing}): {ctx.author} role={role}")
                if missing == ("manage_staff",):
                    extra = " Only **owners** can change the staff whitelist (`!whitelist`)."
                else:
                    extra = f" Need: {', '.join(missing)}."
                await ctx.send(
                    f"❌ **Access denied.** Your staff role (`{role}`) cannot do that.{extra}"
                )
                return
            return await func(*args, **kwargs)

        return wrapper

    return decorator


def require_owner():
    """Decorator: staff role owner only (manage_staff and everything else)."""
    return require_staff("manage_staff")


def require_server_admin():
    """Decorator: Discord Manage Server (or staff owner) in an approved guild."""
    import functools

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            ctx = args[0] if args else None
            if not ctx or not hasattr(ctx, "author"):
                return await func(*args, **kwargs)
            if await _deny_unapproved(ctx):
                return
            if not member_can_server_admin(ctx.author):
                await ctx.send("❌ **Access denied.** Need Manage Server (or be an Arken owner).")
                return
            return await func(*args, **kwargs)

        return wrapper

    return decorator


def require_control():
    """Decorator: DJ / playback — openplay (default on) | DJ role | server admin | staff global_control."""
    import functools

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            ctx = args[0] if args else None
            if not ctx or not hasattr(ctx, "author"):
                return await func(*args, **kwargs)
            if await _deny_unapproved(ctx):
                return
            if not member_can_control(ctx.author):
                print(f"🚫 Control denied: {ctx.author} ({ctx.author.id}) tried {getattr(ctx.command, 'name', '?')}")
                await ctx.send(
                    "❌ **Access denied.** Need the DJ role, Manage Server, "
                    "`!openplay on`, or be an Arken owner."
                )
                return
            return await func(*args, **kwargs)

        return wrapper

    return decorator


def require_user():
    """Decorator: any member in an approved guild (listening / personal commands)."""
    import functools

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            ctx = args[0] if args else None
            if not ctx or not hasattr(ctx, "author"):
                return await func(*args, **kwargs)
            if await _deny_unapproved(ctx):
                return
            return await func(*args, **kwargs)

        return wrapper

    return decorator


def require_authorization():
    """Alias of require_control (DJ / playback)."""
    return require_control()


_staff_counts = ", ".join(
    f"{r}={sum(1 for m in STAFF.all().values() if m.get('role') == r)}"
    for r in tenancy.STAFF_ROLE_ORDER
)
print(f"🛡️ Staff file: {WHITELIST_FILE} (exists={WHITELIST_FILE.is_file()})")
print(f"🛡️ Staff loaded: {len(STAFF.ids())} ({_staff_counts})")
if not STAFF.ids():
    print(
        "⚠️ No staff in user_whitelist.json — admin commands (!guilds, !whitelist, …) will deny everyone. "
        "Copy data/user_whitelist.json from your PC (sync_state) or set ARKEN_BOOTSTRAP_OWNERS in .env."
    )
print(
    "🛡️ Guild registry: "
    + (
        f"ready ({len(GUILD_REGISTRY.all())} servers)"
        if GUILD_REGISTRY.initialized
        else "will seed on first ready"
    )
)

NP_REACTIVE = aenv.flag("NP_REACTIVE")  # equalizer bars follow the real audio spectrum
_SPECTRUM_FRAMES = 3                            # 3 x 20ms of audio per analysis
_SAMPLE_RATE = 48000
_FFT_N = 2048
_HANN = {}
_TWIDDLES = {}


def _hann(n):
    w = _HANN.get(n)
    if w is None:
        w = _HANN[n] = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]
    return w


def _fft(a):
    """In-place radix-2 FFT of a power-of-two list of complex numbers (pure Python, ~10ms for 2048)."""
    n = len(a)
    tw = _TWIDDLES.get(n)
    if tw is None:
        tw = _TWIDDLES[n] = [complex(math.cos(-2 * math.pi * k / n), math.sin(-2 * math.pi * k / n)) for k in range(n // 2)]
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        half = length >> 1
        step = n // length
        for start in range(0, n, length):
            for k in range(half):
                u = a[start + k]
                v = a[start + k + half] * tw[k * step]
                a[start + k] = u + v
                a[start + k + half] = u - v
        length <<= 1


class SimpleAudioSource(discord.PCMVolumeTransformer):
    """Simple audio source that just streams local files"""
    def __init__(self, source, *, title="Unknown", duration=0, file_path=None, start_offset=0.0, volume=None,
                 speed=1.0, requester=None, video_id=None, fade=0.0, game=False):
        super().__init__(source, volume=0.8 if volume is None else volume)
        self.title = title
        self.speed = float(speed or 1.0)    # playback speed from the sound effect (1.0 = normal)
        self.requester = requester          # display name of whoever asked for the track
        self.video_id = video_id
        self.fade = float(fade or 0.0)      # seconds of fade in/out at the track's start and end
        self.master = 1.0                   # extra gain (the sleep timer fades this to 0)
        self.game = bool(game)              # a guess-the-song clip: hidden from presence, stats and autoplay
        self.duration = duration
        self.file_path = file_path  # Track the file path for cleanup
        self.start_offset = float(start_offset or 0.0)  # where in the file this source began (after a seek)
        self._frames_read = 0
        self._tail = deque(maxlen=_SPECTRUM_FRAMES) if NP_REACTIVE else None  # last few PCM frames
        self._peak_db = None

    def read(self):
        data = super().read()
        if data:
            self._frames_read += 1
            if audioop is not None and (self.fade > 0 or self.master < 1.0):
                gain = self.master
                if self.fade > 0:
                    played = None if self.start_offset > 0 else self._frames_read * 0.02
                    remaining = max(0.0, (self.duration - self.elapsed) / self.speed) if self.duration else None
                    gain *= extras.fade_gain(played, remaining, self.fade)
                if gain < 0.999:
                    data = audioop.mul(data, 2, max(0.0, gain))
            if self._tail is not None:
                self._tail.append(data)
        return data

    @property
    def elapsed(self) -> float:
        """Position in the track (seconds): where this source started + audio actually played. The voice
        client pulls one 20ms frame per tick and stops pulling while paused, so this stays correct across
        pauses (and counts no buffering)."""
        return self.start_offset + self._frames_read * 0.02 * self.speed

    def spectrum(self, bands=14, f_lo=50.0, f_hi=12000.0):
        """
        Levels (0..1, one per log-spaced band) of the audio that was just played, or None if unknown.
        FFT over the last ~43ms, volume-compensated, with slow auto-gain so quiet and loud songs both
        use the full height.
        """
        if self._tail is None:
            return None
        try:
            frames = tuple(self._tail)
            if len(frames) < _SPECTRUM_FRAMES:
                return None
            raw = array("h")
            raw.frombytes(b"".join(frames))
            if sys.byteorder != "little":
                raw.byteswap()
            left, right = raw[0::2], raw[1::2]
            if len(left) < _FFT_N:
                return None
            lo = len(left) - _FFT_N
            win = _hann(_FFT_N)
            x = [complex((a + b) * 0.5 * w) for a, b, w in zip(left[lo:], right[lo:], win)]
            _fft(x)
            vol = max(self.volume, 0.05)
            bin_hz = _SAMPLE_RATE / _FFT_N
            edge = (f_hi / f_lo) ** (1.0 / bands)
            dbs = []
            for k in range(bands):
                b0 = max(1, int(f_lo * edge ** k / bin_hz))
                b1 = max(b0 + 1, int(f_lo * edge ** (k + 1) / bin_hz))
                peak_mag = max(abs(x[b]) for b in range(b0, min(b1, _FFT_N // 2)))
                mag = 4.0 * peak_mag / _FFT_N / vol  # ~ sample amplitude of the strongest tone in the band
                dbs.append(20.0 * math.log10(mag + 1e-9) + 1.2 * k)  # +tilt: music has far less energy up high
            top = max(dbs)
            if top < -20.0:  # digital silence
                return [0.0] * bands
            peak = top if self._peak_db is None else max(top, self._peak_db - 1.5)
            self._peak_db = peak
            span = 34.0
            return [max(0.0, min(1.0, (d - (peak - span)) / span)) for d in dbs]
        except Exception:
            return None

def queue_cleanup_request(file_path, keep_recent=True):
    """Signal background service to clean up a specific file.
    keep_recent=True parks it in the processor's small replay cache (instant replays / 'previous')
    before it is deleted; False deletes it immediately."""
    try:
        cleanup_id = str(uuid.uuid4())[:8]

        cleanup_request = {
            'type': 'cleanup',
            'file_path': file_path,
            'cleanup_id': cleanup_id,
            'timestamp': time.time(),
            'keep_recent': keep_recent,
        }

        _write_json_atomic(QUEUE_DIR / f"cleanup_{cleanup_id}.json", cleanup_request)

        print(f"🧹 Queued cleanup for: {os.path.basename(file_path)}")

    except Exception as e:
        print(f"⚠️ Failed to queue cleanup: {e}")

# Song queue management functions
def add_to_song_queue(guild_id, song_info):
    """Add a song to the guild's queue"""
    if guild_id not in song_queues:
        song_queues[guild_id] = []
    song_queues[guild_id].append(song_info)
    print(f"📋 Added '{song_info['title']}' to queue. Position: {len(song_queues[guild_id])}")

def get_next_queued_song(guild_id):
    """Get the next song from the queue"""
    if guild_id in song_queues and song_queues[guild_id]:
        return song_queues[guild_id].pop(0)
    return None

def clear_song_queue(guild_id):
    """Clear all songs from the queue"""
    if guild_id in song_queues:
        count = len(song_queues[guild_id])
        song_queues[guild_id].clear()
        return count
    return 0

def get_queue_info(guild_id):
    """Get information about the current queue"""
    if guild_id not in song_queues:
        return []
    return song_queues[guild_id]

async def play_next_queued_song(guild_id):
    """Play the next song in the queue"""
    guild = bot.get_guild(guild_id)
    if not guild or not guild.voice_client:
        return
    
    voice_client = guild.voice_client
    next_song = get_next_queued_song(guild_id)
    
    if not next_song:
        print(f"📋 Queue empty for guild {guild_id}")
        return
    
    print(f"🎵 Playing next queued song: {next_song['title']}")
    
    try:
        # Create audio source
        audio_path = str(next_song['file_path']).replace('\\', '/')
        audio_source = make_audio_source(guild_id, next_song['file_path'], title=next_song['title'],
                                         duration=next_song['duration'],
                                         requester=_who(next_song.get('requested_by')),
                                         video_id=next_song.get('video_id'))
        
        current_sources[guild_id] = audio_source
        
        # Play the song
        voice_client.play(audio_source, after=lambda e: after_playing(e, guild_id))
        
        # Send notification to the channel where music commands are being used
        # Get the channel from the last command context for this guild
        notification_channel = get_notification_channel(guild_id)
        if notification_channel:
            embed = discord.Embed(
                title=vapor.title("now playing from queue"),
                color=0x9c27b0
            )
            embed.add_field(
                name="Requested by", 
                value=next_song['requested_by'].mention, 
                inline=True
            )
            embed.add_field(
                name="Queue Position", 
                value=f"📊 Was #{1} in queue", 
                inline=True
            )
            if next_song['duration']:
                duration_str = f"{next_song['duration']//60}:{next_song['duration']%60:02d}"
                embed.add_field(name="Duration", value=f"⏱️ {duration_str}", inline=True)
            
            remaining = len(song_queues.get(guild_id, []))
            if remaining > 0:
                embed.set_footer(text=f"{remaining} songs remaining in queue")
            
            try:
                await send_now_playing(notification_channel, embed, audio_source, guild_id,
                                       next_song['title'], next_song['duration'],
                                       cover_path=next_song.get('cover_path'))
            except Exception:
                print(f"⚠️ Could not send notification to channel {notification_channel.name}")
            
    except Exception as e:
        print(f"❌ Error playing queued song: {e}")
        # Try to play the next song in queue
        await play_next_queued_song(guild_id)

def after_playing(error, guild_id):
    """Callback after audio finishes - handles cleanup and playlist continuation"""
    if error:
        print(f"💥 Playback error: {error}")
    else:
        print(f"✅ Finished streaming")
    
    # Get the source and signal cleanup
    source = current_sources.get(guild_id)
    if source and hasattr(source, 'file_path') and source.file_path:
        # For playlist files, don't clean up (they're permanent)
        # For temp files, signal cleanup
        if not str(source.file_path).startswith('playlists'):
            queue_cleanup_request(source.file_path)
    
    current_sources.pop(guild_id, None)

    stopped_on_purpose = guild_id in _no_autoplay   # !stop / !kill / sleep timer: don't start anything new
    _no_autoplay.discard(guild_id)
    if getattr(source, "game", False):
        return  # a guess-the-song clip ended: the game loop moves on by itself
    
    # Programmatic stop from play_next_playlist_song (replacing the track): don't advance twice
    if guild_id in suppress_after_playing:
        suppress_after_playing.discard(guild_id)
        return

    # A streamed-playlist import is still fetching its first track: it starts playback itself
    _q = playlist_queues.get(guild_id)
    if _q and _q.get("starting"):
        return

    _sleep = _sleep_state.get(guild_id)
    if _sleep and _sleep.get("mode") == "end" and not stopped_on_purpose:
        playlist_force_after_stop.discard(guild_id)
        asyncio.run_coroutine_threadsafe(_sleep_finish(guild_id, "end of song"), bot.loop)
        return

    if guild_id in playlist_force_after_stop:
        playlist_force_after_stop.discard(guild_id)
        if PlaylistManager.get_playlist_status(guild_id)["status"] == "active":
            asyncio.run_coroutine_threadsafe(
                play_next_playlist_song(guild_id),
                bot.loop,
            )
        return
    
    # Check for queued songs first (from !play commands)
    if guild_id in song_queues and song_queues[guild_id]:
        print(f"📋 Found {len(song_queues[guild_id])} songs in queue")
        # Schedule the next queued song
        asyncio.run_coroutine_threadsafe(
            play_next_queued_song(guild_id), 
            bot.loop
        )
        return
    
    # If no queued songs, check if there's a playlist active and play next song
    playlist_status = PlaylistManager.get_playlist_status(guild_id)
    if playlist_status["status"] == "active":
        # Schedule the next song in the bot's event loop
        asyncio.run_coroutine_threadsafe(
            play_next_playlist_song(guild_id), 
            bot.loop
        )
    elif source is not None and not stopped_on_purpose and autoplay_enabled(guild_id):
        # Nothing queued, no playlist: autoplay radio picks a similar song
        asyncio.run_coroutine_threadsafe(autoplay_next(guild_id, source), bot.loop)
    elif source is not None and not stopped_on_purpose:
        session_clear(guild_id)   # it all played through: nothing left to resume

async def play_next_playlist_song(guild_id):
    """Advance the active playlist (duplicate concurrent advances are dropped)."""
    if guild_id in _playlist_advancing:
        # Don't run two advances at once, but don't lose the request either: a track may have
        # been skipped/stopped while the previous advance was still finishing (double-skip).
        _playlist_advance_pending.add(guild_id)
        print(f"⏭️ Playlist advance already in progress for guild {guild_id} - queued behind it")
        return
    _playlist_advancing.add(guild_id)
    try:
        while True:
            _playlist_advance_pending.discard(guild_id)
            await _play_next_playlist_song_inner(guild_id)
            if guild_id not in _playlist_advance_pending:
                break
            # A request arrived meanwhile: honour it only if the previous advance left nothing playing
            guild = bot.get_guild(guild_id)
            vc = guild.voice_client if guild else None
            if not vc or vc.is_playing() or vc.is_paused() or guild_id not in playlist_queues:
                break
    finally:
        _playlist_advancing.discard(guild_id)
        _playlist_advance_pending.discard(guild_id)

async def _play_next_playlist_song_inner(guild_id):
    """Play the next song in the active playlist"""
    try:
        print(f"🎵 Attempting to play next playlist song for guild {guild_id}")
        
        # Get next song from playlist
        result = PlaylistManager.get_next_playlist_song(guild_id)
        print(f"🔍 Next song result: {result.get('status')}")
        
        if result["status"] == "finished":
            session_clear(guild_id)
            # Playlist completed
            guild = bot.get_guild(guild_id)
            if guild and guild.voice_client:
                channels = [channel for channel in guild.text_channels if channel.permissions_for(guild.me).send_messages]
                if channels:
                    embed = discord.Embed(
                        title="🎵 Playlist Completed!",
                        description=f"Finished playing playlist: **{result.get('playlist_name', 'Unknown')}**",
                        color=0x4caf50
                    )
                    rain = await _scene_file(
                        ("rain", None), style_for(guild_id), guild_id=guild_id
                    ) if BADGES_ENABLED else None
                    if rain is not None:
                        embed.set_image(url=f"attachment://{rain.filename}")
                        await channels[0].send(embed=embed, file=rain)
                    else:
                        await channels[0].send(embed=embed)
            return
        
        if result["status"] != "success":
            print(f"❌ Failed to get next playlist song: {result['message']}")
            return
        
        # Get song info
        song = result["song"]

        # Streamed (YouTube-imported) entry: make sure its file is downloaded before playing it.
        # Skipping past the prefetch buffer lands here too (it's fetched/promoted to the foreground lane).
        if "status" in song and song.get("video_id"):
            stream_queue = playlist_queues.get(guild_id)
            ok = await _stream_wait_for_entry(guild_id, stream_queue, song)
            if stream_queue is not None:
                stream_queue["starting"] = False
            if playlist_queues.get(guild_id) is not stream_queue or stream_queue is None:
                return  # session was stopped/replaced while we were waiting
            if not ok:
                print(f"⏭️ Skipping unavailable track: {song.get('title')}")
                await _play_next_playlist_song_inner(guild_id)
                return

        audio_file = Path(song["audio_file"])
        
        print(f"🔍 Playing song: {song.get('title', 'Unknown')}")
        print(f"🔍 Audio file path: {audio_file}")
        print(f"🔍 File exists: {audio_file.exists()}")
        
        if not audio_file.exists():
            print(f"⚠️ Playlist file missing: {audio_file}")
            print(f"❌ File not found, skipping to next song")
            # Try to continue with next song
            await _play_next_playlist_song_inner(guild_id)
            return
        
        # Get guild and voice client
        guild = bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            print(f"⚠️ No voice client for guild {guild_id}")
            return
        
        voice_client = guild.voice_client
        
        # Stop current playback if any
        if voice_client.is_playing() or voice_client.is_paused():
            suppress_after_playing.add(guild_id)
            voice_client.stop()
            await asyncio.sleep(0.5)
        
        # Start playing the next song using global ffmpeg_options
        
        print(f"🔍 Creating FFmpeg source for next song: {str(audio_file)}")
        
        try:
            # Ensure consistent path format for FFmpeg
            audio_path = str(audio_file).replace('\\', '/')
            print(f"🔍 FFmpeg path (normalized): {audio_path}")
            
            # Simple path normalization for FFmpeg
            
            # Use FFmpegPCMAudio - Discord.py handles Opus encoding internally
            audio_source = make_audio_source(guild_id, audio_file, title=song.get("title", "Unknown"),
                                             duration=song.get("duration", 0), video_id=song.get("video_id"))
            print(f"✅ Next song audio source created")
            
            current_sources[guild_id] = audio_source
            
            print(f"🔍 Voice client status before next song: connected={voice_client.is_connected()}, playing={voice_client.is_playing()}")
            voice_client.play(audio_source, after=lambda e: after_playing(e, guild_id))
            print(f"✅ Started next song playback!")
            
            # Check if playback actually started
            await asyncio.sleep(1)
            print(f"🔍 Next song after 1 second - Voice client playing: {voice_client.is_playing()}")
            
        except Exception as e:
            print(f"❌ Error creating next song audio source: {e}")
            return
        
        # Send notification to the channel where music commands are being used
        notification_channel = get_notification_channel(guild_id)
        if notification_channel:
            embed = discord.Embed(
                title=vapor.title("now playing from playlist"),
                color=0x9c27b0
            )
            embed.add_field(
                name="Playlist", 
                value=f"🎵 {result['playlist_name']}", 
                inline=True
            )
            embed.add_field(
                name="Progress", 
                value=f"📊 {result['position']}/{result['total']}", 
                inline=True
            )
            if song.get('duration'):
                duration_str = f"{song['duration']//60}:{song['duration']%60:02d}"
                embed.add_field(name="Duration", value=f"⏱️ {duration_str}", inline=True)
            
            try:
                await send_now_playing(notification_channel, embed, audio_source, guild_id,
                                       song.get('title', 'Unknown'), song.get('duration', 0),
                                       cover_path=song.get('cover_path'))
            except Exception:
                print(f"⚠️ Could not send playlist notification to channel {notification_channel.name}")
        
    except Exception as e:
        print(f"❌ Error playing next playlist song: {e}")

class PlaylistManager:
    """Air-gapped playlist management - only references local files"""
    
    @staticmethod
    def create_playlist(guild_id, name):
        """Create a new empty playlist in this server's folder only."""
        try:
            # Sanitize playlist name
            safe_name = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).strip()
            if not safe_name:
                return {"status": "error", "message": "Invalid playlist name"}
            
            playlist_file = _guild_playlists(guild_id) / f"{safe_name}.json"
            
            if playlist_file.exists():
                return {"status": "error", "message": f"Playlist '{safe_name}' already exists"}
            
            playlist_data = {
                "name": safe_name,
                "created": time.time(),
                "files": [],  # List of local file paths only
                "total_duration": 0
            }
            
            with open(playlist_file, 'w') as f:
                json.dump(playlist_data, f, indent=2)
            
            return {"status": "success", "message": f"Created playlist '{safe_name}'"}
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to create playlist: {str(e)}"}
    
    @staticmethod
    def list_playlists(guild_id):
        """List playlists belonging to this Discord server only."""
        try:
            playlists = []
            for playlist_file in _guild_playlists(guild_id).glob("*.json"):
                try:
                    with open(playlist_file, 'r') as f:
                        data = json.load(f)
                    
                    playlists.append({
                        "name": data.get("name", playlist_file.stem),
                        "files": len(data.get("files", [])),
                        "duration": data.get("total_duration", 0)
                    })
                except Exception:
                    continue
            
            return {"status": "success", "playlists": playlists}
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to list playlists: {str(e)}"}
    
    @staticmethod
    def get_available_media():
        """Get list of available local media files (air-gap safe)"""
        try:
            media_files = []
            
            for webm_file in sorted(p for p in MEDIA_DIR.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS):
                try:
                    # Get metadata if available
                    json_file = webm_file.with_suffix('.json')
                    title = "Unknown"
                    duration = 0
                    
                    if json_file.exists():
                        with open(json_file, 'r') as f:
                            metadata = json.load(f)
                        title = metadata.get('title', 'Unknown')
                        duration = metadata.get('duration', 0)
                    
                    media_files.append({
                        "file_path": str(webm_file),
                        "filename": webm_file.name,
                        "title": title,
                        "duration": duration
                    })
                except Exception:
                    continue
            
            return {"status": "success", "files": media_files}
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to get media files: {str(e)}"}
    
    @staticmethod
    def add_song_to_playlist(guild_id, playlist_name, current_file_path, song_title, duration=0):
        """Add currently playing song to a playlist in this server only (air-gap safe)"""
        try:
            gdir = _guild_playlists(guild_id)
            # Validate playlist exists
            playlist_file = gdir / f"{playlist_name}.json"
            if not playlist_file.exists():
                return {"status": "error", "message": f"Playlist '{playlist_name}' not found"}
            
            # Validate current file exists
            current_path = Path(current_file_path)
            if not current_path.exists():
                return {"status": "error", "message": "Current audio file not found"}
            
            # Create playlist-specific directory for permanent storage
            playlist_media_dir = gdir / playlist_name
            playlist_media_dir.mkdir(exist_ok=True)
            
            # Generate unique filename for playlist storage
            timestamp = int(time.time())
            file_id = str(uuid.uuid4())[:8]
            safe_title = "".join(c for c in song_title if c.isalnum() or c in (' ', '-', '_')).strip()[:30]
            if not safe_title:
                safe_title = "Unknown"
            
            # Create permanent filenames
            audio_filename = f"{safe_title}_{timestamp}_{file_id}{current_path.suffix or '.webm'}"
            metadata_filename = f"{safe_title}_{timestamp}_{file_id}.json"
            
            permanent_audio_path = playlist_media_dir / audio_filename
            permanent_metadata_path = playlist_media_dir / metadata_filename
            
            # Copy audio file to permanent location
            shutil.copy2(current_path, permanent_audio_path)
            
            # Copy metadata file if it exists
            metadata_source = current_path.with_suffix('.json')
            if metadata_source.exists():
                shutil.copy2(metadata_source, permanent_metadata_path)
            else:
                # Create basic metadata
                metadata = {
                    "title": song_title,
                    "duration": duration,
                    "added_to_playlist": time.time(),
                    "original_file": str(current_path.name)
                }
                with open(permanent_metadata_path, 'w') as f:
                    json.dump(metadata, f, indent=2)
            
            # Update playlist JSON
            with open(playlist_file, 'r') as f:
                playlist_data = json.load(f)
            
            # Store portable paths under playlists/<guild_id>/… (never another server's folder)
            audio_rel = tenancy.scope_playlist_path(
                str(Path("playlists") / str(guild_id) / playlist_name / audio_filename), guild_id
            )
            meta_rel = tenancy.scope_playlist_path(
                str(Path("playlists") / str(guild_id) / playlist_name / metadata_filename), guild_id
            )
            song_entry = {
                "title": song_title,
                "audio_file": audio_rel,
                "metadata_file": meta_rel,
                "duration": duration,
                "added": time.time()
            }
            
            playlist_data["files"].append(song_entry)
            playlist_data["total_duration"] += duration
            
            # Save updated playlist
            with open(playlist_file, 'w') as f:
                json.dump(playlist_data, f, indent=2)
            
            return {
                "status": "success", 
                "message": f"Added '{song_title}' to playlist '{playlist_name}'",
                "permanent_file": audio_rel
            }
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to add song to playlist: {str(e)}"}
    
    @staticmethod
    def get_playlist_contents(guild_id, playlist_name):
        """Get contents of a playlist in this server only."""
        try:
            playlist_file = _guild_playlists(guild_id) / f"{playlist_name}.json"
            if not playlist_file.exists():
                return {"status": "error", "message": f"Playlist '{playlist_name}' not found"}
            
            with open(playlist_file, 'r') as f:
                playlist_data = json.load(f)
            
            return {"status": "success", "playlist": playlist_data}
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to get playlist contents: {str(e)}"}
    
    @staticmethod
    def clean_playlist(guild_id, playlist_name):
        """Remove missing files from playlist JSON"""
        try:
            result = PlaylistManager.get_playlist_contents(guild_id, playlist_name)
            if result["status"] != "success":
                return result
            
            playlist = result["playlist"]
            songs = playlist.get("files", [])
            original_count = len(songs)
            
            # Filter out missing files
            valid_songs = []
            removed_songs = []
            for song in songs:
                audio_file = Path(song.get("audio_file", ""))
                if audio_file.exists():
                    valid_songs.append(song)
                else:
                    removed_songs.append(song.get("title", "Unknown"))
                    print(f"⚠️ Removing missing file from playlist: {audio_file}")
            
            if len(valid_songs) != original_count:
                # Update the playlist JSON
                playlist["files"] = valid_songs
                playlist["total_duration"] = sum(song.get("duration", 0) for song in valid_songs)
                
                # Save updated playlist
                playlist_file = _guild_playlists(guild_id) / f"{playlist_name}.json"
                with open(playlist_file, 'w', encoding='utf-8') as f:
                    json.dump(playlist, f, indent=2, ensure_ascii=False)
                
                return {
                    "status": "success",
                    "message": f"Cleaned playlist '{playlist_name}': removed {original_count - len(valid_songs)} missing files",
                    "removed_songs": removed_songs,
                    "remaining_songs": len(valid_songs)
                }
            else:
                return {"status": "success", "message": f"Playlist '{playlist_name}' is already clean"}
                
        except Exception as e:
            return {"status": "error", "message": f"Failed to clean playlist: {str(e)}"}

    @staticmethod
    def start_playlist_playback(playlist_name, guild_id):
        """Start playing a playlist (air-gap safe - only local files)"""
        try:
            # First clean the playlist to remove any missing files
            clean_result = PlaylistManager.clean_playlist(guild_id, playlist_name)
            if clean_result["status"] == "success" and "removed" in clean_result["message"]:
                print(f"🧹 {clean_result['message']}")
            
            result = PlaylistManager.get_playlist_contents(guild_id, playlist_name)
            if result["status"] != "success":
                return result
            
            playlist = result["playlist"]
            songs = playlist.get("files", [])
            
            if not songs:
                return {"status": "error", "message": f"Playlist '{playlist_name}' is empty"}
            
            # Verify all playlist files exist (should be clean now)
            valid_songs = []
            for song in songs:
                audio_file = Path(song.get("audio_file", ""))
                
                print(f"🔍 Checking playlist file: {audio_file}")
                print(f"🔍 File exists: {audio_file.exists()}")
                
                if audio_file.exists():
                    valid_songs.append(song)
                else:
                    print(f"⚠️ Playlist file missing: {audio_file}")
            
            if not valid_songs:
                return {"status": "error", "message": f"No valid files found in playlist '{playlist_name}'"}
            
            # Set up playlist queue with smart features
            playlist_queues[guild_id] = {
                "guild_id": guild_id,
                "playlist_name": playlist_name,
                "songs": valid_songs.copy(),  # Current playing order
                "original_songs": valid_songs.copy(),  # Original order for un-shuffle
                "current_index": 0,
                "total_songs": len(valid_songs),
                "shuffle": False,  # Shuffle mode off by default
                "loop_mode": "off"  # Loop modes: "off", "single", "all"
            }
            
            return {
                "status": "success", 
                "message": f"Started playlist '{playlist_name}' with {len(valid_songs)} songs",
                "first_song": valid_songs[0]
            }
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to start playlist: {str(e)}"}
    
    @staticmethod
    def get_next_playlist_song(guild_id):
        """Get the next song in the current playlist with loop support"""
        try:
            if guild_id not in playlist_queues:
                return {"status": "error", "message": "No playlist is currently active"}
            
            queue = playlist_queues[guild_id]
            current_index = queue["current_index"]
            songs = queue["songs"]
            loop_mode = queue.get("loop_mode", "off")
            
            print(f"🔍 Playlist state: index={current_index}, songs={len(songs)}, loop={loop_mode}")
            
            # Handle single song loop
            if loop_mode == "single":
                # Stay on current song (don't increment index)
                if current_index > 0:
                    current_index = current_index - 1  # Go back to current song
                    queue["current_index"] = current_index
                current_song = songs[current_index] if current_index < len(songs) else songs[0]
                queue["current_index"] += 1
            elif current_index >= len(songs):
                # End of playlist reached
                if loop_mode == "all":
                    # Loop back to beginning
                    queue["current_index"] = 0
                    current_song = songs[0]
                    queue["current_index"] += 1
                elif len(songs) == 1:
                    # Special case: single song playlist, loop it by default
                    queue["current_index"] = 0
                    current_song = songs[0]
                    queue["current_index"] += 1
                    print("🔄 Single song playlist - looping by default")
                else:
                    # Playlist finished
                    playlist_queues.pop(guild_id, None)
                    return {"status": "finished", "message": "Playlist completed"}
            else:
                # Normal progression
                current_song = songs[current_index]
                queue["current_index"] += 1
            
            return {
                "status": "success",
                "song": current_song,
                "position": queue["current_index"],  # Use the updated index for position
                "total": queue["total_songs"],
                "playlist_name": queue["playlist_name"]
            }
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to get next song: {str(e)}"}
    
    @staticmethod
    def stop_playlist(guild_id):
        """Stop the current playlist"""
        if guild_id in playlist_queues:
            playlist_name = playlist_queues[guild_id]["playlist_name"]
            playlist_queues.pop(guild_id, None)
            return {"status": "success", "message": f"Stopped playlist '{playlist_name}'"}
        return {"status": "error", "message": "No playlist is currently active"}
    
    @staticmethod
    def get_playlist_status(guild_id):
        """Get current playlist status"""
        if guild_id not in playlist_queues:
            return {"status": "inactive"}
        
        queue = playlist_queues[guild_id]
        return {
            "status": "active",
            "playlist_name": queue["playlist_name"],
            "current_position": queue["current_index"],
            "total_songs": queue["total_songs"],
            "remaining": queue["total_songs"] - queue["current_index"],
            "shuffle": queue.get("shuffle", False),
            "loop_mode": queue.get("loop_mode", "off")
        }
    
    @staticmethod
    def shuffle_playlist(guild_id):
        """Toggle shuffle mode for current playlist"""
        try:
            if guild_id not in playlist_queues:
                return {"status": "error", "message": "No playlist is currently active"}
            
            queue = playlist_queues[guild_id]
            current_shuffle = queue.get("shuffle", False)
            
            if not current_shuffle:
                # Enable shuffle
                import random
                current_song_index = queue["current_index"] - 1  # Current song (already played)
                remaining_songs = queue["songs"][queue["current_index"]:]  # Unplayed songs
                
                # Shuffle only the remaining songs
                random.shuffle(remaining_songs)
                
                # Reconstruct the playlist: played songs + shuffled remaining
                queue["songs"] = queue["songs"][:queue["current_index"]] + remaining_songs
                queue["shuffle"] = True
                
                return {"status": "success", "message": "🔀 Shuffle enabled! Remaining songs randomized"}
            else:
                # Disable shuffle - restore original order
                current_song_index = queue["current_index"] - 1
                original_songs = queue["original_songs"]
                
                # Find where we are in the original playlist
                if current_song_index >= 0:
                    current_song = queue["songs"][current_song_index]
                    # Find this song in original order
                    try:
                        original_index = next(i for i, song in enumerate(original_songs) 
                                            if song.get("audio_file") == current_song.get("audio_file"))
                        queue["current_index"] = original_index + 1
                    except StopIteration:
                        # Fallback if not found
                        queue["current_index"] = 0
                
                queue["songs"] = original_songs.copy()
                queue["shuffle"] = False
                
                return {"status": "success", "message": "🔄 Shuffle disabled! Restored original order"}
                
        except Exception as e:
            return {"status": "error", "message": f"Failed to toggle shuffle: {str(e)}"}
    
    @staticmethod
    def set_loop_mode(guild_id, mode):
        """Set loop mode for current playlist"""
        try:
            if guild_id not in playlist_queues:
                return {"status": "error", "message": "No playlist is currently active"}
            
            valid_modes = ["off", "single", "all"]
            if mode not in valid_modes:
                return {"status": "error", "message": f"Invalid loop mode. Use: {', '.join(valid_modes)}"}
            
            queue = playlist_queues[guild_id]
            queue["loop_mode"] = mode
            
            mode_emojis = {"off": "⏹️", "single": "🔂", "all": "🔁"}
            mode_names = {"off": "Off", "single": "Single Song", "all": "All Songs"}
            
            return {
                "status": "success", 
                "message": f"{mode_emojis[mode]} Loop mode set to: {mode_names[mode]}"
            }
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to set loop mode: {str(e)}"}
    
    @staticmethod
    def skip_song(guild_id, direction="next"):
        """Skip to next or previous song in playlist"""
        try:
            if guild_id not in playlist_queues:
                return {"status": "error", "message": "No playlist is currently active"}
            
            queue = playlist_queues[guild_id]
            current_index = queue["current_index"]
            songs = queue["songs"]
            
            if direction == "next":
                if len(songs) == 1:
                    # Single song playlist - restart the same song
                    queue["current_index"] = 0
                    return {"status": "success", "message": "⏭️ Restarting single song in playlist"}
                elif current_index >= len(songs):
                    # Multi-song playlist at end - loop back to start
                    queue["current_index"] = 0
                    return {"status": "success", "message": "⏭️ Looping back to first song"}
                else:
                    # Normal skip is handled by normal progression
                    return {"status": "success", "message": "⏭️ Skipping to next song"}
            
            elif direction == "previous":
                if current_index <= 1:
                    # Go to first song
                    queue["current_index"] = 0
                    return {
                        "status": "success", 
                        "message": "⏮️ Going to first song",
                        "song": songs[0]
                    }
                else:
                    # Go back 2 positions (since current_index is already +1 from current song)
                    queue["current_index"] = max(0, current_index - 2)
                    return {
                        "status": "success", 
                        "message": "⏮️ Going to previous song",
                        "song": songs[queue["current_index"]]
                    }
            
        except Exception as e:
            return {"status": "error", "message": f"Failed to skip song: {str(e)}"}

def queue_cleanup_all_request():
    """Signal background service to clean up all media files on startup/shutdown"""
    try:
        cleanup_id = str(uuid.uuid4())[:8]
        
        cleanup_request = {
            'type': 'cleanup_all',
            'cleanup_id': cleanup_id,
            'timestamp': time.time(),
            'reason': 'startup_cleanup'
        }
        
        cleanup_file = QUEUE_DIR / f"cleanup_all_{cleanup_id}.json"
        with open(cleanup_file, 'w') as f:
            json.dump(cleanup_request, f, indent=2)
        
        print(f"🧹 Queued full cleanup on startup")
        
    except Exception as e:
        print(f"⚠️ Failed to queue startup cleanup: {e}")

@bot.event
async def on_ready():
    print(f"✅ {bot.user.name} is online!")
    print(f"📁 Connected to {len(bot.guilds)} servers")
    if TOKEN_POOL.enabled():
        print(
            f"🛡️ Token pool: id={TOKEN_ID or '?'} · "
            f"{len(TOKEN_POOL.token_ids)} token(s) · "
            f"cap {TOKEN_POOL.max_guilds_per_token} guilds/token"
        )

    # First run: approve every server we're already in so existing setups keep working.
    seeded = GUILD_REGISTRY.seed([(g.id, g.name) for g in bot.guilds])
    if seeded:
        print(f"🛡️ Guild registry seeded with {seeded} existing server(s)")
    for guild in list(bot.guilds):
        status = GUILD_REGISTRY.seen(guild.id, guild.name)
        if status != "approved":
            print(f"⚠️ Server not approved ({status}): {guild.name} ({guild.id})")
            if TOKEN_POOL.enabled() and TOKEN_ID:
                owner = TOKEN_POOL.token_of(guild.id)
                if owner is not None and owner != TOKEN_ID:
                    print(f"🛡️ {guild.name} pending but reserved for token {owner} — leaving")
                    try:
                        await guild.leave()
                    except Exception:
                        pass
                    continue
                if not LEAVE_UNAPPROVED:
                    await _pool_claim_from_invite(guild)
            if LEAVE_UNAPPROVED:
                try:
                    await guild.leave()
                    print(f"👋 Left unapproved server {guild.name} (ARKEN_LEAVE_UNAPPROVED=1)")
                except Exception as e:
                    print(f"⚠️ Could not leave {guild.name}: {e}")
            continue

        if TOKEN_POOL.enabled() and TOKEN_ID:
            if not await _pool_claim_from_invite(guild):
                continue
            TOKEN_POOL.update_guild_members(
                guild.id, _guild_member_count(guild), name=guild.name
            )

    # Check intents status
    print("\n🔍 INTENT STATUS CHECK:")
    print(f"   Message Content Intent: {bot.intents.message_content}")
    print(f"   Voice States Intent: {bot.intents.voice_states}")
    print(f"   Guild Messages Intent: {bot.intents.guild_messages}")
    
    if not bot.intents.message_content:
        print("\n⚠️ WARNING: MESSAGE CONTENT INTENT IS DISABLED!")
        print("⚠️ The bot cannot read message content!")
        print("⚠️ Go to Discord Developer Portal → Your Bot → Privileged Gateway Intents")
        print("⚠️ Enable 'MESSAGE CONTENT INTENT' and restart the bot!")
    else:
        print("✅ Message Content Intent is enabled")
    
    # List connected servers
    if bot.guilds:
        print(f"\n📋 Connected to {len(bot.guilds)} server(s):")
        for guild in bot.guilds:
            st = GUILD_REGISTRY.status(guild.id)
            seat = TOKEN_POOL.seat_of(guild.id) if TOKEN_POOL.enabled() else None
            seat_s = f" · {seat}" if seat else ""
            print(f"   • {guild.name} (ID: {guild.id}) [{st}{seat_s}]")
            # Check bot permissions in each server
            perms = guild.me.guild_permissions
            print(f"     Permissions: Read Messages={perms.read_messages}, Send Messages={perms.send_messages}")
    
    print("\n🎮 Audio streaming bot ready!")
    print("📁 File streaming service online")
    print("💡 Try typing '!commands' to test if the bot can see messages")

    if not getattr(bot, "_arken_pool_stats_started", False):
        bot._arken_pool_stats_started = True
        bot.loop.create_task(_pool_stats_loop())
    
    # Auto-rejoin voice channels after restart (approved servers only)
    print("\n🔊 Checking for voice channels to rejoin...")
    await rejoin_voice_channels()
    
    # Restore playback state after rejoining channels
    print("🎵 Checking for playback state to restore...")
    await restore_playback_state()
    
    # Queue cleanup of leftover cache only when nothing is playing anywhere
    playback_state = load_playback_state()
    if not playback_state and not any(
        vc and (vc.is_playing() or vc.is_paused()) for g in bot.guilds for vc in (g.voice_client,)
    ):
        queue_cleanup_all_request()
    else:
        print("🎵 Skipping cleanup — playback state or active voice exists")

@bot.event
async def on_disconnect():
    """Handle Discord disconnection. Do not wipe media_library — other guilds may still need files."""
    print("🔌 Bot disconnected from Discord")

@bot.event
async def on_guild_join(guild):
    """New invite: lock pool city to this bot; pending until staff approve."""
    status = GUILD_REGISTRY.seen(guild.id, guild.name)
    print(f"📥 Joined {guild.name} ({guild.id}) — status={status}")

    if TOKEN_POOL.enabled() and TOKEN_ID:
        if not await _pool_claim_from_invite(guild):
            return
    notice = (
        f"Arken joined **{guild.name}** but this server is **{status}**. "
        f"An owner must run `!allowguild {guild.id}` before music works here."
    )
    print(f"🛡️ {notice}")
    # Try to tell the system channel; ignore failures (missing perms).
    try:
        if guild.system_channel is not None:
            await guild.system_channel.send(
                f"👋 {notice}"
            )
    except Exception:
        pass
    if LEAVE_UNAPPROVED or status == "revoked":
        try:
            await guild.leave()
            print(f"👋 Left {guild.name} (unapproved/revoked)")
        except Exception as e:
            print(f"⚠️ Could not leave {guild.name}: {e}")


@bot.event
async def on_guild_remove(guild):
    print(f"📤 Removed from {guild.name} ({guild.id})")


@bot.event
async def on_voice_state_update(member, before, after):
    """Track voice state changes"""
    if member == bot.user:
        if before.channel != after.channel:
            if after.channel:
                print(f"🔊 Bot joined voice channel: {after.channel.name}")
            elif before.channel:
                print(f"👋 Bot left voice channel: {before.channel.name}")

@bot.event
async def on_command_error(ctx, error):
    """Handle command errors"""
    if isinstance(error, commands.CommandNotFound):
        # Ignore unknown commands silently
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing required argument: {error.param.name}")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Command on cooldown. Try again in {error.retry_after:.1f} seconds.")
    else:
        print(f"❌ Error in command {ctx.command.name if ctx.command else 'unknown'}: {type(error).__name__}: {error}")
        import traceback
        traceback.print_exc()
        await ctx.send(f"❌ An error occurred: {str(error)}")

@bot.event
async def on_message(message):
    """Log all messages that start with command prefix for debugging"""
    if message.author.bot:
        # Do not run commands on bot messages (avoids edge cases; duplicate instances are the real dual-reply cause).
        return
    
    # Log ALL messages for debugging (not just commands)
    guild_name = message.guild.name if message.guild else 'DM'
    channel_name = message.channel.name if hasattr(message.channel, 'name') else str(message.channel.type)
    channel_type = message.channel.type.name if hasattr(message.channel, 'type') else 'unknown'
    
    print(f"📨 Message received: '{message.content[:100] if message.content else '(empty)'}'")
    print(f"   From: {message.author} ({message.author.id})")
    print(f"   In: {guild_name} → {channel_name} (type: {channel_type})")
    print(f"   Content type: {type(message.content)}, Length: {len(message.content) if message.content else 0}")
    
    # Log messages with command prefix
    if message.content and message.content.startswith('!'):
        print(f"🎯 Processing command: '{message.content}'")
    
    # Process commands normally - THIS IS CRITICAL
    try:
        await bot.process_commands(message)
        if message.content and message.content.startswith('!'):
            print(f"✅ Command processed: '{message.content}'")
    except Exception as e:
        print(f"❌ Error processing command: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        # Try to send error to user
        try:
            await message.channel.send(f"❌ Error processing command: {str(e)}")
        except:
            pass

# ============================================================================
# ⚡ REQUEST PIPELINE — atomic queue files, heartbeat liveness (no fixed timeout),
#    live progress → Discord
# ============================================================================

def _write_json_atomic(path, data):
    """Write JSON via temp file + rename so the processor never reads a half-written file."""
    path = Path(path)
    tmp = path.with_name("." + path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02 * (attempt + 1))  # Windows: reader momentarily holds the target
    os.replace(tmp, path)


def queue_audio_request(query, lane="foreground"):
    """Queue a download request. lane='background' is used for playlist batch prefetch."""
    request_id = str(uuid.uuid4())[:8]
    request_data = {
        'query': query,
        'request_id': request_id,
        'timestamp': time.time(),
        'lane': lane,
    }
    _write_json_atomic(QUEUE_DIR / f"request_{request_id}.json", request_data)
    return request_id


def queue_playlist_info_request(url, max_items=None):
    """Ask the processor to flat-extract a YouTube playlist (ids/titles/durations only)."""
    request_id = str(uuid.uuid4())[:8]
    data = {
        'type': 'playlist_info',
        'url': url,
        'request_id': request_id,
        'timestamp': time.time(),
        'lane': 'foreground',
    }
    if max_items:
        data['max_items'] = int(max_items)
    _write_json_atomic(QUEUE_DIR / f"request_{request_id}.json", data)
    return request_id


def cancel_queued_request(request_id):
    """Cancel a request that the processor has NOT claimed yet. True if it was cancelled."""
    try:
        (QUEUE_DIR / f"request_{request_id}.json").unlink()
        return True
    except (FileNotFoundError, PermissionError):
        return False


def check_processing_result(request_id):
    """Check if background service has processed the request"""
    result_file = QUEUE_DIR / f"result_{request_id}.json"

    if result_file.exists():
        try:
            with open(result_file, 'r', encoding='utf-8') as f:
                result = json.load(f)

            # Clean up result + progress files
            result_file.unlink()
            try:
                (QUEUE_DIR / f"progress_{request_id}.json").unlink()
            except OSError:
                pass
            return result
        except Exception as e:
            # Most likely caught mid-replace on Windows; the next poll will succeed
            print(f"⚠️ Result not readable yet ({request_id}): {e}")
            return None

    return None


def read_progress(request_id):
    """Latest progress beacon for a request (or None)."""
    try:
        with open(QUEUE_DIR / f"progress_{request_id}.json", 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _processor_heartbeat_age():
    """Seconds since the audio processor last beat (inf if never / unreadable)."""
    try:
        t = float((QUEUE_DIR / ".processor_heartbeat").read_text().strip())
        return max(0.0, time.time() - t)
    except Exception:
        return float('inf')


def _queue_ahead(request_id):
    """How many other foreground requests are waiting in the queue."""
    n = 0
    for rf in QUEUE_DIR.glob("request_*.json"):
        if rf.name == f"request_{request_id}.json":
            continue
        try:
            with open(rf, 'r', encoding='utf-8') as f:
                if json.load(f).get('lane', 'foreground') != 'background':
                    n += 1
        except Exception:
            continue
    return n


async def wait_for_request(request_id, on_progress=None, poll=0.25):
    """
    Wait for a processor result with NO fixed deadline.
    Instead of timing out, we watch the processor's heartbeat: if it stops beating we
    restart it once (queued request files survive) and only give up if it stays dead.
    """
    stale_since = None
    restarted = False
    while True:
        result = check_processing_result(request_id)
        if result is not None:
            return result

        if on_progress is not None:
            try:
                await on_progress(read_progress(request_id))
            except Exception as e:
                print(f"⚠️ progress callback error: {e}")

        if _processor_heartbeat_age() < 20:
            stale_since = None
        else:
            if stale_since is None:
                stale_since = time.time()
            if time.time() - stale_since > 5:
                if not restarted:
                    restarted = True
                    print("⚠️ Audio processor heartbeat lost — restarting it")
                    await asyncio.to_thread(_ensure_audio_processor_subprocess)
                    stale_since = time.time() + 20  # grace while it boots
                else:
                    return {
                        'status': 'error',
                        'message': 'The audio processor is not responding. Try again in a moment.',
                    }
        await asyncio.sleep(poll)


def _format_progress(prog):
    """Progress beacon -> vaporwave status line (None if no beacon yet)."""
    if not prog:
        return None
    stage = prog.get("stage")
    if stage == "downloading":
        return vapor.downloading(prog.get("percent"), prog.get("speed"), prog.get("eta"), prog.get("downloaded"))
    if stage == "finishing":
        return vapor.finishing()
    return vapor.extracting()


# ----------------------------------------------------------------------------
# 🏷️ GIF BADGES — status headers ("NOW PLAYING", "DOWNLOADING", ...) are animated GIFs sent as their
#    own message directly ABOVE the text message they label.
#    vapor.* status builders return a StatusText: the body without a header plus `.badge` (the label).
#    BadgeHeader.sync() keeps that header message in step and only re-uploads when the badge CHANGES,
#    so progress edits stay text-only and cheap. If a GIF can't be built, the text header is used.
#    Disable with ARKEN_GIFS=0.
# ----------------------------------------------------------------------------

BADGES_ENABLED = vapor.BADGES

if BADGES_ENABLED:
    threading.Thread(target=gif_art.prewarm, daemon=True, name="gif-prewarm").start()


async def _scene_file(scene, style=None, *, guild_id=None):
    """discord.File for a terminal-scene GIF ('crash' with its error text, 'shutdown', 'rain'), or None."""
    kind, text = scene
    theme = guild_gif_theme(guild_id)
    try:
        data = await asyncio.to_thread(
            gif_art.terminal_bytes, kind, gif_art.resolve_style(style), text, theme
        )
        return discord.File(io.BytesIO(data), filename=f"{kind}.gif")
    except Exception as e:
        print(f"⚠️ terminal GIF unavailable ({kind}): {e}")
        return None


async def _badge_file(label, style=None, cover=None, theme=None):
    """discord.File for a badge GIF, or None if it can't be built."""
    theme = theme or gif_art.GuildTheme.default()
    try:
        data = await asyncio.to_thread(gif_art.header_bytes, label, style, cover, theme)
        return discord.File(io.BytesIO(data), filename=gif_art.slug(label) + ".gif")
    except Exception as e:
        print(f"⚠️ badge GIF unavailable ({label}): {e}")
        return None


class BadgeHeader:
    """
    The animated badge as its OWN message, sent directly above the text message it labels.
    (Discord always draws attachments below a message's text, so a GIF can't sit above text inside
    a single message; consecutive messages from the bot stack together instead.)
    sync() keeps the header matching a status text and only re-uploads when the badge, the server's
    style, or the cover art CHANGES.
    """

    def __init__(self, style=None):
        self.msg = None
        self.badge = None
        self.style = style  # None -> follow the server's !style setting
        self.cover = None   # gif_art.Cover (pixel-art thumbnail) shown on the left of the badge
        self._key = None
        self._dead = False  # the header message was deleted by someone: stop trying, use text headers

    async def sync(self, dest, text):
        """Make the header match `text`; returns the body for the message below it."""
        badge = getattr(text, "badge", None) if BADGES_ENABLED else None
        scene = getattr(text, "scene", None) if badge else None
        if not badge:
            await self.remove()  # plain text (or GIFs off): no header
            return None if text is None else str(text)
        fallback = getattr(text, "fallback", None) or str(text)  # same text WITH a text header
        if self._dead:
            return fallback
        gid = _dest_guild_id(dest)
        theme = guild_gif_theme(gid)
        style = gif_art.effective_style(self.style or style_for(gid), self.cover)
        theme_key = theme.cache_key()
        key = (scene or badge, style, self.cover.key if self.cover is not None else None, theme_key)
        if key == self._key and self.msg is not None:
            return str(text)
        file = await (
            _scene_file(scene, style, guild_id=gid)
            if scene
            else _badge_file(badge, style, self.cover, theme)
        )
        if file is None:
            return _vapor_fallback_branded(fallback, theme.badge_brand())
        try:
            if self.msg is None:
                self.msg = await dest.send(file=file)
            else:
                await self.msg.edit(attachments=[file])
            self.badge, self._key = badge, key
            return str(text)
        except discord.NotFound:
            self._dead = True
            self.msg = self.badge = self._key = None
        except discord.HTTPException as e:
            print(f"⚠️ badge header failed: {e}")
        return fallback

    async def remove(self):
        msg, self.msg, self.badge, self._key = self.msg, None, None, None
        if msg is not None:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass


async def send_status(dest, text):
    """dest.send() for a status text: badge header first, then the text. Returns (message, header)."""
    header = BadgeHeader()
    body = await header.sync(dest, text)
    msg = await dest.send(content=body)
    return msg, header


class StatusMessage:
    """One Discord message edited in place (rate-limited); re-sends if it was deleted."""
    MIN_INTERVAL = 1.0  # ~Discord's sustained edit limit (5 per 5s per channel); discord.py queues any excess

    def __init__(self, ctx, initial_text):
        self.ctx = ctx
        self.initial = initial_text
        self.msg = None
        self._last_text = initial_text
        self._last_edit = 0.0
        self._dead = False
        self._header = BadgeHeader()  # the GIF badge message above this one (shared with trackers)

    async def start(self):
        self.msg, self._header = await send_status(self.ctx, self.initial)
        self._last_edit = time.time()

    async def update(self, text):
        """Cosmetic update: skipped if unchanged or too soon."""
        if self._dead or self.msg is None or text == self._last_text:
            return
        now = time.time()
        if now - self._last_edit < self.MIN_INTERVAL:
            return
        try:
            body = await self._header.sync(self.ctx, text)
            await self.msg.edit(content=body)
            self._last_text = text
            self._last_edit = now
        except discord.NotFound:
            self._dead = True
        except discord.HTTPException:
            pass

    async def edit(self, content=None, embed=None):
        """Final edit (always applied). If the message was deleted, send a fresh one."""
        if not self._dead and self.msg is not None:
            try:
                body = await self._header.sync(self.ctx, content)
                await self.msg.edit(content=body, embed=embed)
                self._last_text = content
                return
            except discord.NotFound:
                self._dead = True
            except discord.HTTPException as e:
                print(f"⚠️ status edit failed: {e}")
        if content is not None or embed is not None:
            await self._header.remove()
            self._header = BadgeHeader()
            body = await self._header.sync(self.ctx, content)
            self.msg = await self.ctx.send(content=body, embed=embed)  # fresh message replaces the deleted one
            self._dead = False

    def progress_callback(self, request_id):
        t0 = time.time()

        async def cb(prog):
            text = _format_progress(prog)
            if text is None:
                if time.time() - t0 < 1.0:
                    return
                text = vapor.queued(_queue_ahead(request_id))
            await self.update(text)

        return cb


# ============================================================================
# 📥 YOUTUBE PLAYLIST IMPORT — rolling batch prefetch
#
#   • Import = flat-extract the playlist, build an in-memory (and restart-safe) session
#   • First track is fetched in the foreground lane so playback starts ASAP
#   • A background poller keeps a window of downloaded tracks ahead of the playhead:
#       when ready-ahead <= LOW_WATER and nothing is downloading -> request next BATCH
#   • Default = delete after play (replay cache holds the last few);
#     `keep` moves each finished track into playlists/<name>/ permanently
# ============================================================================

STREAM_BATCH_SIZE = max(1, int(aenv.get("BATCH_SIZE", "5")))
STREAM_LOW_WATER = max(0, int(aenv.get("LOW_WATER", "2")))
MAX_IMPORT_ITEMS = max(1, int(aenv.get("MAX_PLAYLIST_ITEMS", "200")))

stream_tasks = {}          # guild_id -> (asyncio.Task, queue)
stream_status_msgs = {}    # guild_id -> StatusMessage
_bg_tasks = set()          # keep strong refs to fire-and-forget tasks

_PLAYLIST_URL_RE = re.compile(
    r"(?:youtube\.com|music\.youtube\.com)/playlist\?(?:[^\s]*&)?list=", re.I
)


def _is_playlist_url(text):
    """True for dedicated playlist pages (watch?v=..&list=.. still plays just that video)."""
    return bool(_PLAYLIST_URL_RE.search(text or ""))


def _spawn(coro):
    task = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def _make_stream_entry(e):
    return {
        "title": e["title"],
        "video_id": e["id"],
        "url": e["url"],
        "duration": int(e.get("duration") or 0),
        "status": "pending",      # pending | downloading | ready | failed
        "audio_file": None,
        "metadata_file": None,
        "request_id": None,
        "lane": None,
        "attempts": 0,
        "submitted_at": 0,
        "added": time.time(),
    }


def _entry_file_ok(entry):
    f = entry.get("audio_file")
    return bool(f) and os.path.exists(f)


def _stream_submit(entry, lane):
    entry["request_id"] = queue_audio_request(entry["url"], lane=lane)
    entry["lane"] = lane
    entry["status"] = "downloading"
    entry["attempts"] = entry.get("attempts", 0) + 1
    entry["submitted_at"] = time.time()


def _stream_promote(entry):
    """We're waiting on this entry right now: if its background request is still unclaimed, re-queue as foreground."""
    if entry.get("status") != "downloading" or entry.get("lane") != "background":
        return
    rid = entry.get("request_id")
    if rid and cancel_queued_request(rid):
        _stream_submit(entry, "foreground")


def _stream_entry_failed(stream, entry, message):
    print(f"⚠️ Stream entry failed ({entry.get('title')}): {message}")
    entry["error"] = message
    if entry.get("attempts", 0) < 2:
        entry["status"] = "pending"      # one automatic retry
    else:
        entry["status"] = "failed"
        stream["failed"] = stream.get("failed", 0) + 1


def _stream_keep_entry(queue, entry):
    """`keep` mode: move the finished file into playlists/<guild_id>/<name>/ for this server only."""
    stream = queue["stream"]
    name = stream["name"]
    guild_id = queue["guild_id"]
    pdir = _guild_playlists(guild_id) / name
    pdir.mkdir(exist_ok=True)

    src = Path(entry["audio_file"])
    safe_title = "".join(c for c in entry["title"] if c.isalnum() or c in (' ', '-', '_')).strip()[:30] or "Unknown"
    stem = f"{safe_title}_{int(time.time())}_{str(uuid.uuid4())[:8]}"
    dest = pdir / f"{stem}{src.suffix or '.webm'}"
    meta_dest = pdir / f"{stem}.json"

    shutil.move(str(src), str(dest))
    side = src.with_suffix('.json')
    if side.exists():
        shutil.move(str(side), str(meta_dest))
    else:
        with open(meta_dest, 'w', encoding='utf-8') as f:
            json.dump({"title": entry["title"], "duration": entry["duration"], "video_id": entry["video_id"]}, f, indent=2)

    audio_rel = tenancy.scope_playlist_path(
        str(Path("playlists") / str(guild_id) / name / dest.name), guild_id
    )
    meta_rel = tenancy.scope_playlist_path(
        str(Path("playlists") / str(guild_id) / name / meta_dest.name), guild_id
    )
    entry["audio_file"] = audio_rel
    entry["metadata_file"] = meta_rel

    playlist_file = _guild_playlists(guild_id) / f"{name}.json"
    if playlist_file.exists():
        with open(playlist_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {"name": name, "created": time.time(), "files": [], "total_duration": 0}
    data["files"].append({
        "title": entry["title"],
        "audio_file": audio_rel,
        "metadata_file": meta_rel,
        "duration": entry["duration"],
        "added": time.time(),
        "video_id": entry["video_id"],
    })
    data["total_duration"] = sum(s.get("duration", 0) for s in data["files"])
    with open(playlist_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _stream_prefill_from_playlist(queue):
    """`keep` mode: tracks already saved in the permanent playlist need no download."""
    stream = queue["stream"]
    playlist_file = _guild_playlists(queue["guild_id"]) / f"{stream['name']}.json"
    if not playlist_file.exists():
        return
    try:
        with open(playlist_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return
    known = {
        s["video_id"]: s for s in data.get("files", [])
        if s.get("video_id") and os.path.exists(s.get("audio_file", ""))
    }
    for e in queue["songs"]:
        hit = known.get(e["video_id"])
        if hit:
            e["audio_file"] = hit["audio_file"]
            e["metadata_file"] = hit.get("metadata_file")
            e["status"] = "ready"
            stream["fetched"] = stream.get("fetched", 0) + 1


def _stream_harvest(queue):
    """Collect finished downloads, retry failures, detect lost requests."""
    stream = queue["stream"]
    now = time.time()
    for e in queue["songs"]:
        st = e.get("status")
        if st == "downloading":
            rid = e.get("request_id")
            res = check_processing_result(rid) if rid else None
            if res is not None:
                fp = res.get("file_path")
                if res.get("status") == "success" and fp and os.path.exists(fp):
                    e["audio_file"] = fp
                    e["cover_path"] = res.get("cover_path")
                    if res.get("duration"):
                        e["duration"] = int(res["duration"])
                    e["status"] = "ready"
                    e["error"] = None
                    stream["fetched"] = stream.get("fetched", 0) + 1
                    if stream.get("keep"):
                        try:
                            _stream_keep_entry(queue, e)
                        except Exception as err:
                            print(f"⚠️ keep-move failed for {e.get('title')}: {err}")
                else:
                    _stream_entry_failed(stream, e, res.get("message", "download failed"))
            else:
                prog = read_progress(rid) if rid else None
                ref = (prog or {}).get("updated") or e.get("submitted_at") or now
                still_queued = bool(rid) and (QUEUE_DIR / f"request_{rid}.json").exists()
                if not still_queued and now - ref > 120:
                    _stream_entry_failed(stream, e, "download request was lost")
        elif st == "ready" and not _entry_file_ok(e):
            # File gone (replay cache evicted it / cleanup) — it will be re-fetched if needed again
            e["status"] = "pending"
            e["audio_file"] = None


def _stream_upcoming(queue):
    songs = queue["songs"]
    idx = queue["current_index"]
    ups = list(songs[idx:])
    if queue.get("loop_mode") == "all":
        ups += songs[:max(0, idx - 1)]
    return ups


def _stream_refill(queue):
    """Watermark rule: when few ready tracks remain ahead and nothing is downloading, request the next batch."""
    ups = _stream_upcoming(queue)
    if not ups:
        return
    ready_ahead = sum(1 for e in ups if e["status"] == "ready" and _entry_file_ok(e))
    inflight = sum(1 for e in ups if e["status"] == "downloading")
    if inflight == 0 and ready_ahead <= STREAM_LOW_WATER:
        batch = [e for e in ups if e["status"] == "pending"][:STREAM_BATCH_SIZE]
        for e in batch:
            _stream_submit(e, "background")
        if batch:
            print(f"📥 Requested next batch: {len(batch)} tracks (ready ahead was {ready_ahead})")


def _stream_status_text(queue):
    stream = queue["stream"]
    total = queue["total_songs"]
    fetched = stream.get("fetched", 0)
    name = str(stream.get("display", queue["playlist_name"]))
    skipped = stream.get("failed", 0)
    note = stream.get("note")
    active = next((e for e in queue["songs"] if e["status"] == "downloading"), None)
    if active:
        prog = read_progress(active["request_id"]) if active.get("request_id") else None
        pct = prog.get("percent") if prog and prog.get("stage") == "downloading" else None
        return vapor.import_status(name, fetched, total, pct, skipped=skipped, note=note)
    return vapor.import_idle(name, fetched, total, skipped=skipped, note=note)


async def _stream_update_status(queue, status_msg):
    if status_msg is None:
        return
    await status_msg.update(_stream_status_text(queue))


async def _kick_playlist(guild_id):
    """Start the first track once any in-flight advance (e.g. an old session) has wound down."""
    for _ in range(100):
        if guild_id not in _playlist_advancing:
            break
        await asyncio.sleep(0.1)
    await play_next_playlist_song(guild_id)


def _stream_teardown(queue):
    """Session over: cancel unclaimed requests, delete never-played downloads (unless `keep`)."""
    stream = queue.get("stream") or {}
    keep = stream.get("keep")
    inflight = []
    for e in queue["songs"]:
        if e.get("status") == "downloading" and e.get("request_id"):
            if not cancel_queued_request(e["request_id"]):
                inflight.append(e["request_id"])  # already claimed; clean its file when it lands
    if not keep:
        for e in queue["songs"][queue.get("current_index", 0):]:
            if e.get("status") == "ready" and _entry_file_ok(e) and not str(e["audio_file"]).startswith("playlists"):
                queue_cleanup_request(e["audio_file"], keep_recent=False)
    if inflight and not keep:
        try:
            _spawn(_stream_drain(inflight))
        except RuntimeError:
            pass


async def _stream_drain(request_ids):
    """Delete files from downloads that were already running when a session ended."""
    deadline = time.time() + 120
    pending = set(request_ids)
    while pending and time.time() < deadline:
        for rid in list(pending):
            res = check_processing_result(rid)
            if res is not None:
                pending.discard(rid)
                if res.get("status") == "success" and res.get("file_path"):
                    queue_cleanup_request(res["file_path"], keep_recent=False)
        await asyncio.sleep(1)


async def _stream_poller(guild_id, queue, status_msg):
    """Per-session background task: harvest results, refill batches, keep status fresh."""
    last_proc_check = 0.0
    no_vc_since = None
    try:
        while playlist_queues.get(guild_id) is queue:
            _stream_harvest(queue)
            _stream_refill(queue)
            await _stream_update_status(queue, status_msg)

            now = time.time()
            if now - last_proc_check > 30:
                last_proc_check = now
                if _processor_heartbeat_age() > 30:
                    print("⚠️ Stream poller: processor heartbeat lost — restarting it")
                    await asyncio.to_thread(_ensure_audio_processor_subprocess)

            # Bot was disconnected from voice and nobody is coming back: stop prefetching
            guild = bot.get_guild(guild_id)
            if not guild or not guild.voice_client:
                no_vc_since = no_vc_since or now
                if now - no_vc_since > 120:
                    print("🛑 Stream poller: no voice connection for 2 min — ending playlist session")
                    PlaylistManager.stop_playlist(guild_id)
                    break
            else:
                no_vc_since = None

            await asyncio.sleep(0.4)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        import traceback
        print(f"❌ Stream poller crashed: {e}")
        traceback.print_exc()
    finally:
        _stream_teardown(queue)
        cur = stream_tasks.get(guild_id)
        if cur and cur[0] is asyncio.current_task():
            stream_tasks.pop(guild_id, None)
            stream_status_msgs.pop(guild_id, None)


def _stream_ensure_poller(guild_id):
    q = playlist_queues.get(guild_id)
    if not q or not q.get("stream"):
        return
    cur = stream_tasks.get(guild_id)
    if cur and not cur[0].done() and cur[1] is q:
        return
    task = asyncio.get_running_loop().create_task(
        _stream_poller(guild_id, q, stream_status_msgs.get(guild_id))
    )
    stream_tasks[guild_id] = (task, q)


async def _stream_wait_for_entry(guild_id, queue, entry):
    """
    Wait (async) until `entry` has a playable local file. Returns False if it can't be
    fetched (caller skips it) or the session ended. Skipping past the buffer lands here:
    a pending entry is requested in the foreground lane, a background one is promoted.
    """
    _stream_ensure_poller(guild_id)
    t0 = time.time()
    notice = None
    notice_header = None
    try:
        while True:
            if playlist_queues.get(guild_id) is not queue:
                return False
            st = entry.get("status")
            if st == "ready" and _entry_file_ok(entry):
                return True
            if st == "failed":
                return False
            if st == "ready":
                entry["status"] = st = "pending"
                entry["audio_file"] = None
            if st == "pending":
                _stream_submit(entry, "foreground")
            elif st == "downloading":
                _stream_promote(entry)

            if notice is None and time.time() - t0 > 1.5:
                ch = get_notification_channel(guild_id)
                if ch:
                    try:
                        notice, notice_header = await send_status(ch, vapor.buffering(entry.get("title", "?")))
                    except Exception:
                        notice = False
                else:
                    notice = False
            await asyncio.sleep(0.25)
    finally:
        if notice:
            try:
                await notice.delete()
            except Exception:
                pass
        if notice_header is not None:
            await notice_header.remove()


async def _begin_playlist_import(ctx, url, keep=False, shuffle=False, custom_name=None, preloaded_info=None):
    """Shared by `!playlist import` and `!play <playlist url>`."""
    guild_id = ctx.guild.id
    set_notification_channel(guild_id, ctx.channel)

    voice_client = ctx.guild.voice_client
    if not voice_client or not _voice_session_usable(voice_client):
        await ctx.send("❌ I'm not connected to a voice channel! Use `!summon` first.")
        return
    if not url.lower().startswith(("http://", "https://")):
        await ctx.send("❌ Usage: `!playlist import <youtube playlist url> [keep] [shuffle] [name]`")
        return

    status = StatusMessage(ctx, vapor.reading_playlist())
    await status.start()

    if preloaded_info is not None:
        info = preloaded_info      # e.g. !likes play: the entries are already known
    else:
        request_id = queue_playlist_info_request(url, MAX_IMPORT_ITEMS)
        info = await wait_for_request(request_id, on_progress=status.progress_callback(request_id))
    if info.get("status") != "success":
        await status.edit(content=vapor.error_label(info.get("message", "Unknown error")))
        return

    entries = [_make_stream_entry(e) for e in info["entries"]]
    yt_title = info.get("title") or "YouTube Playlist"
    safe_name = "".join(c for c in (custom_name or yt_title) if c.isalnum() or c in (' ', '-', '_')).strip()[:60]
    if keep and not safe_name:
        safe_name = f"import_{int(time.time())}"

    queue = {
        "guild_id": guild_id,
        "playlist_name": safe_name or yt_title,
        "songs": list(entries),
        "original_songs": list(entries),   # same dict objects -> status updates are shared
        "current_index": 0,
        "total_songs": len(entries),
        "shuffle": False,
        "loop_mode": "off",
        "starting": True,                  # after_playing must not advance until entry 0 is playing
        "stream": {
            "keep": bool(keep),
            "name": safe_name,
            "display": yt_title,
            "source_url": url,
            "fetched": 0,
            "failed": 0,
        },
    }
    if shuffle:
        random.shuffle(queue["songs"])     # decide the order up front so batches follow it
        queue["shuffle"] = True
    if keep:
        _stream_prefill_from_playlist(queue)

    # Replacing an old session: its poller notices and tears itself down
    playlist_queues[guild_id] = queue
    stream_status_msgs[guild_id] = status

    # First track: foreground lane (playback starts ASAP). The poller then batches the rest.
    first = next((e for e in queue["songs"] if e["status"] == "pending"), None)
    if first is not None and queue["songs"][0] is first:
        _stream_submit(first, "foreground")
    _stream_ensure_poller(guild_id)
    _spawn(_kick_playlist(guild_id))

    mode = "keep" if keep else "delete after play"
    extra = f" ～ first {len(entries)} of {info['total']}" if info.get("truncated") else ""
    queue["stream"]["note"] = f"{mode}{extra}"
    await status.edit(content=_stream_status_text(queue))
    try:
        card = vapor.playlist_card(yt_title, len(entries), sum(int(e.get("duration") or 0) for e in entries),
                                   shuffled=bool(shuffle), keep=bool(keep))
        await reply_note(ctx, card, lifetime=max(NOTE_SECONDS * 2, 0) if NOTE_SECONDS else 0)
    except Exception as e:
        print(f"⚠️ playlist card failed: {e}")


def _restore_stream_session(guild_id):
    """After a restart: re-link shared entry dicts, reset interrupted downloads, resume prefetching."""
    q = playlist_queues.get(guild_id)
    if not q or not q.get("stream"):
        return
    by_id = {e["video_id"]: e for e in q["songs"]}
    q["original_songs"] = [by_id.get(e["video_id"], e) for e in q.get("original_songs", [])]
    for e in q["songs"]:
        if e.get("status") == "downloading":
            e["status"] = "pending"
            e["request_id"] = None
    q["starting"] = False
    _stream_ensure_poller(guild_id)


# ============================================================================
# 🍩 SUMMON ANIMATION — spinning ASCII donut (donut.c) while Arken connects to voice
#    One message, edited every ~0.3s (Discord throttles to ~1/s after a short burst; frames are
#    picked by elapsed time so the spin speed stays constant even when throttled). Frames are
#    pre-rendered once in a background thread. Disable with ARKEN_DONUT=0.
# ============================================================================

DONUT_ENABLED = aenv.get("DONUT", "1").strip().lower() not in ("0", "false", "no", "off")
DONUT_MIN_SECONDS = float(aenv.get("DONUT_SECONDS", "4"))  # play at least this long
DONUT_FRAME_INTERVAL = float(aenv.get("DONUT_INTERVAL", "0.3"))  # seconds per frame (Discord allows ~5 edits/5s, so ~1/s sustained)

if DONUT_ENABLED:
    threading.Thread(target=donut.get_frames, daemon=True, name="donut-prewarm").start()


def _donut_message(frame_text, caption):
    return "```\n" + caption + "\n" + frame_text + "\n```"


class DonutAnimation:
    """Edits one message with successive donut frames until stop() is called."""

    def __init__(self, message, frames, caption, min_seconds=DONUT_MIN_SECONDS):
        self.message = message
        self.frames = frames
        self.caption = caption
        self.min_seconds = min_seconds
        self._t0 = time.time()
        self._stop_event = asyncio.Event()
        self._task = None
        self._stopped = False
        self._dead = False  # message was deleted

    def start(self):
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self):
        last = 0  # frame 0 is already on screen
        while not self._stop_event.is_set():
            # Wait until the next absolute tick (not a fixed sleep), so edit latency doesn't stretch the interval.
            elapsed = time.time() - self._t0
            wait = (int(elapsed / DONUT_FRAME_INTERVAL) + 1) * DONUT_FRAME_INTERVAL - elapsed
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=max(wait, 0.01))
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            # Pick the frame from elapsed time, not from an edit counter: if Discord's rate limit
            # delays an edit, we skip ahead instead of slowing the spin down.
            idx = int((time.time() - self._t0) / DONUT_FRAME_INTERVAL) % len(self.frames)
            if idx == last:
                continue
            last = idx
            try:
                await self.message.edit(content=_donut_message(self.frames[idx], self.caption))
            except discord.NotFound:
                self._dead = True
                return
            except discord.HTTPException:
                pass  # rate-limited / transient: just skip this frame

    async def stop(self, final_content=None, respect_min=True):
        """Finish the animation and replace the message with final_content. Safe to call twice."""
        if self._stopped:
            return
        self._stopped = True
        if respect_min:
            remaining = self.min_seconds - (time.time() - self._t0)
            if remaining > 0:
                await asyncio.sleep(remaining)  # let the donut keep spinning a little
        self._stop_event.set()
        if self._task is not None:
            try:
                await self._task  # lets an in-flight edit finish, so it can't land after our final edit
            except Exception:
                pass
        if final_content is not None and not self._dead:
            try:
                await self.message.edit(content=final_content)
            except discord.HTTPException as e:
                print(f"⚠️ summon final edit failed: {e}")


class GifSummon:
    """
    The summon animation as an animated GIF attachment: Discord plays it by itself (smooth, no edit
    rate limit), so all we do is remove it again when the connection attempt is over.
    Same interface as DonutAnimation (stop()).
    """

    def __init__(self, message, min_seconds=DONUT_MIN_SECONDS):
        self.message = message
        self.min_seconds = min_seconds
        self._t0 = time.time()
        self._stopped = False

    async def stop(self, final_content=None, respect_min=True):
        if self._stopped:
            return
        self._stopped = True
        if respect_min:
            remaining = self.min_seconds - (time.time() - self._t0)
            if remaining > 0:
                await asyncio.sleep(remaining)  # let the donut spin for a moment
        if final_content is not None:
            try:
                await self.message.edit(content=final_content, attachments=[])  # drop the GIF
            except discord.HTTPException as e:
                print(f"⚠️ summon final edit failed: {e}")


async def _start_summon_animation(ctx, channel_name):
    """Send the spinning donut (GIF, or text frames as a fallback). Returns (message, animation or None)."""
    if not DONUT_ENABLED:
        return await ctx.send(f"🎵 Connecting to **{channel_name}**..."), None
    gid = _dest_guild_id(ctx)
    theme = guild_gif_theme(gid)
    if BADGES_ENABLED:
        try:
            data = await asyncio.to_thread(
                gif_art.donut_bytes, gif_art.resolve_style(style_for(gid)), theme
            )
            msg = await ctx.send(file=discord.File(io.BytesIO(data), filename="summoning.gif"))
            return msg, GifSummon(msg)
        except Exception as e:
            print(f"⚠️ donut GIF unavailable, using text donut: {e}")
    frames = await asyncio.to_thread(donut.get_frames)
    # Caption must stay within the donut's width, or Discord wraps the code block and scrambles it.
    caption = f"~ summoning {theme.summon} ~".center(donut.WIDTH).rstrip()
    msg = await ctx.send(_donut_message(frames[0], caption))
    anim = DonutAnimation(msg, frames, caption)
    anim.start()
    return msg, anim


async def _finish_summon(msg, anim, content):
    if anim is not None:
        await anim.stop(content)
    else:
        await msg.edit(content=content)


# ============================================================================
# ⚙️ SETTINGS, SHORT NOTES, PLAYBACK CONTROLS
#    arken_settings.json keeps per-server `!style` and volume. Notes are short replies with an animated
#    badge that delete themselves after ARKEN_NOTE_SECONDS (0 = keep). The control helpers are shared by
#    the chat commands and the now-playing buttons so both behave identically.
# ============================================================================

SETTINGS_FILE = _resolve_state_json("arken_settings.json", "gondor_settings.json")
NOTE_SECONDS = max(0.0, float(aenv.get("NOTE_SECONDS", "12")))
PRESENCE_ENABLED = aenv.flag("PRESENCE")
NP_COUNTDOWN = aenv.flag("NP_COUNTDOWN")
NP_BUTTONS_ENABLED = aenv.flag("NP_BUTTONS")
DEFAULT_VOLUME = 0.8
SEEK_STEP = 10  # seconds for !forward / !rewind and the skip buttons
SCRUB_STEPS = 20  # jump points offered in the scrub dropdown (every 100/SCRUB_STEPS % of the track)

_settings = {}


def _load_settings():
    global _settings
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _settings = data if isinstance(data, dict) else {}
    except FileNotFoundError:
        _settings = {}
    except Exception as e:
        print(f"⚠️ Could not read {SETTINGS_FILE.name}: {e}")
        _settings = {}


_load_settings()


def guild_setting(guild_id, key, default=None):
    return (_settings.get(str(guild_id)) or {}).get(key, default)


def set_guild_setting(guild_id, key, value):
    _settings.setdefault(str(guild_id), {})[key] = value
    try:
        _write_json_atomic(SETTINGS_FILE, _settings)
    except Exception as e:
        print(f"⚠️ Could not save {SETTINGS_FILE.name}: {e}")


def _dest_guild_id(dest):
    guild = getattr(dest, "guild", None) or getattr(getattr(dest, "channel", None), "guild", None)
    return getattr(guild, "id", None)


def style_for(guild_id):
    if not guild_id:
        return gif_art.DEFAULT_STYLE
    pref = guild_setting(guild_id, "style")
    return "auto" if pref == "auto" else gif_art.resolve_style(pref)   # "auto": colours follow the cover art


def _vapor_fallback_branded(fallback: str | None, brand: str) -> str:
    """Prefix text fallbacks with the guild seat when GIF generation fails."""
    if not fallback or not brand:
        return fallback or ""
    tag = f"**{brand}**"
    if fallback.startswith("```"):
        nl = fallback.find("\n")
        if nl != -1:
            return fallback[: nl + 1] + f"{tag}\n" + fallback[nl + 1 :]
    return f"{tag}\n{fallback}"


def guild_gif_theme(guild_id) -> gif_art.GuildTheme:
    """Terminal GIF chrome for this server (city seat when assigned, else guild slug)."""
    if not guild_id:
        return gif_art.GuildTheme.default()
    if TOKEN_POOL.enabled():
        tid = TOKEN_POOL.token_of(guild_id)
        cs = TOKEN_POOL.callsign_of(guild_id)
        if tid and cs:
            return gif_art.GuildTheme.from_seat(TOKEN_POOL.seat_of(guild_id), cs, tid)
    guild = bot.get_guild(int(guild_id))
    if guild and guild.name:
        return gif_art.GuildTheme.from_guild_name(guild.name)
    return gif_art.GuildTheme.default()


def load_cover(path):
    """Pixel-art cover from a processor-made cover_*.rgb file, or None."""
    if not path:
        return None
    try:
        data = Path(path).read_bytes()
        if len(data) != gif_art.COVER_W * gif_art.COVER_H * 3:
            return None
        return gif_art.cover_from_rgb(data, gif_art.COVER_W, gif_art.COVER_H, Path(path).stem)
    except Exception:
        return None


def get_volume(guild_id):
    src = current_sources.get(guild_id)
    if src is not None and hasattr(src, "volume"):
        return src.volume
    return guild_setting(guild_id, "volume", DEFAULT_VOLUME)


def set_volume(guild_id, percent):
    pct = max(0, min(200, int(round(percent))))
    set_guild_setting(guild_id, "volume", pct / 100)
    src = current_sources.get(guild_id)
    if src is not None and hasattr(src, "volume"):
        src.volume = pct / 100
    return pct


def toggle_mute(guild_id):
    """Mute (remembering the level) or restore the level from before the mute. Returns (muted, percent)."""
    current = get_volume(guild_id)
    if current > 0:
        set_guild_setting(guild_id, "premute", current)
        set_volume(guild_id, 0)
        return True, 0
    previous = guild_setting(guild_id, "premute") or DEFAULT_VOLUME
    return False, set_volume(guild_id, previous * 100)


def apply_saved_volume(guild_id, source):
    saved = guild_setting(guild_id, "volume")
    if saved is not None and hasattr(source, "volume"):
        source.volume = float(saved)


async def _delete_later(messages, delay):
    await asyncio.sleep(delay)
    for m in messages:
        if m is not None:
            try:
                await m.delete()
            except discord.HTTPException:
                pass


async def reply_note(dest, text, lifetime=None):
    """Send a short reply (badge GIF above, framed text below) that removes itself after `lifetime` seconds."""
    header = BadgeHeader()
    body = await header.sync(dest, text)
    msg = await dest.send(content=body)
    life = NOTE_SECONDS if lifetime is None else lifetime
    if life > 0:
        _spawn(_delete_later([header.msg, msg], life))
    return msg


_presence_name = object()


async def update_presence():
    """Bot profile status. Song titles only when in exactly one server — otherwise they would leak."""
    global _presence_name
    if not PRESENCE_ENABLED:
        return
    titles = []
    for src in list(current_sources.values()):
        t = getattr(src, "title", None)
        if t and not getattr(src, "game", False):  # guessing-game clips must not give the answer away
            titles.append(str(t))
    # Profile status is global on the Discord account; never show another server's song.
    shown = tenancy.presence_title(len(bot.guilds), titles)
    if shown is None and titles:
        name = tenancy.NEUTRAL_PRESENCE
    else:
        name = shown
    if name == _presence_name:
        return
    _presence_name = name
    try:
        activity = discord.Activity(type=discord.ActivityType.listening, name=name) if name else None
        await bot.change_presence(activity=activity)
    except Exception as e:
        print(f"⚠️ presence update failed: {e}")


def _voice_client_for(guild_id):
    guild = bot.get_guild(guild_id)
    return guild.voice_client if guild else None


def pause_current(guild_id):
    vc = _voice_client_for(guild_id)
    if vc and vc.is_playing():
        vc.pause()
        return True, "Paused"
    return False, "Nothing playing"


def resume_current(guild_id):
    vc = _voice_client_for(guild_id)
    if vc and vc.is_paused():
        vc.resume()
        return True, "Resumed"
    return False, "Nothing paused"


def skip_current(guild_id):
    """Skip the current track (playlist-aware). Returns (ok, message)."""
    vc = _voice_client_for(guild_id)
    if not vc or not vc.is_connected():
        return False, "I'm not connected to a voice channel."
    if not (vc.is_playing() or vc.is_paused()):
        return False, "Nothing playing"
    if PlaylistManager.get_playlist_status(guild_id)["status"] == "active":
        result = PlaylistManager.skip_song(guild_id, "next")
        if result["status"] != "success":
            return False, result["message"]
        playlist_force_after_stop.add(guild_id)
        vc.stop()
        return True, result["message"]
    if song_queues.get(guild_id):
        vc.stop()
        return True, "Skipped — playing next in queue"
    vc.stop()
    return True, "Stopped"


def stop_current(guild_id):
    """Stop playback and end any active playlist. Returns (ok, message)."""
    # End the playlist session FIRST: stopping the voice client fires after_playing, which would
    # otherwise advance to the next playlist track. (For YouTube imports this also cancels the
    # prefetch and deletes downloaded-but-unplayed files.)
    try:
        snapshot_session(guild_id)   # remember what was playing so the dashboard can resume it later
    except Exception as e:
        print(f"⚠️ session snapshot failed: {e}")
    stopped_playlist = None
    playlist_note = ""
    pl_status = PlaylistManager.get_playlist_status(guild_id)
    if pl_status["status"] == "active":
        stopped_playlist = PlaylistManager.stop_playlist(guild_id)
        playlist_note = f" (ended playlist **{pl_status['playlist_name']}**)"

    vc = _voice_client_for(guild_id)
    if not vc:
        return False, "Not connected"
    if vc.is_playing() or vc.is_paused():
        # Queue cleanup of any active files before stopping (never playlist files)
        source = current_sources.get(guild_id)
        if source and hasattr(source, 'file_path') and source.file_path:
            if not str(source.file_path).startswith('playlists'):
                queue_cleanup_request(source.file_path)

        _no_autoplay.add(guild_id)
        vc.stop()
        current_sources.pop(guild_id, None)

        # Also clear the song queue when stopping
        queue_count = clear_song_queue(guild_id)
        if queue_count > 0:
            return True, f"⏹️ **Stopped** (cleared {queue_count} queued songs){playlist_note}"
        return True, f"⏹️ **Stopped**{playlist_note}"
    if stopped_playlist:
        # Nothing audible (e.g. still buffering the next track) but a playlist was active
        clear_song_queue(guild_id)
        return True, f"⏹️ **Stopped**{playlist_note}"
    return False, "Nothing playing"


def _parse_clock(text):
    """'90', '1:30', '1:02:03', '90s', '1m30s', '1h2m3s' -> seconds."""
    text = text.strip().lower().replace(" ", "")
    if re.fullmatch(r"\d+(?::\d{1,2}){0,2}", text):
        total = 0
        for part in text.split(":"):
            total = total * 60 + int(part)
        return total
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", text)
    if m and any(m.groups()):
        h, mi, sec = (int(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + sec
    raise ValueError(f"can't read '{text}' as a time")


def parse_seek(spec):
    """'1:30' -> ('to', 90); '+30' / '-15' -> ('by', +30 / -15)."""
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("give a time like `1:30`, `+30` or `-15`")
    if spec[0] in "+-":
        secs = _parse_clock(spec[1:])
        return "by", secs if spec[0] == "+" else -secs
    return "to", _parse_clock(spec)


def seek_current(guild_id, *, to=None, by=None):
    """
    Jump within the playing track by restarting ffmpeg at the new position and swapping it into the
    running voice player (so playback never stops and nothing gets cleaned up).
    Returns (True, (from_s, to_s, duration)) or (False, error text).
    """
    vc = _voice_client_for(guild_id)
    old = current_sources.get(guild_id)
    if not vc or not vc.is_connected() or old is None or not (vc.is_playing() or vc.is_paused()):
        return False, "Nothing playing"
    path = getattr(old, "file_path", None)
    if not path or not os.path.exists(path) or not hasattr(old, "start_offset"):
        return False, "Can't seek this track"
    if getattr(old, "game", False):
        return False, "No seeking during the guessing game"
    frm = old.elapsed
    duration = old.duration or 0
    target = float(to) if to is not None else frm + float(by or 0)
    target = max(0.0, target)
    if duration:
        target = min(target, max(0.0, duration - 2))
    was_paused = vc.is_paused()
    new = make_audio_source(guild_id, path, title=old.title, duration=old.duration,
                            requester=getattr(old, "requester", None), video_id=getattr(old, "video_id", None),
                            start_offset=target, volume=old.volume)
    new.master = getattr(old, "master", 1.0)
    current_sources[guild_id] = new
    tracker = _np_trackers.get(guild_id)
    if tracker is not None:
        tracker.source = new
        tracker.force = True
    vc.source = new  # swap into the running player (this briefly un-pauses it)
    if was_paused:
        vc.pause()
    # Retire the old ffmpeg a moment later: the player thread may be mid-read on it right now.
    asyncio.get_running_loop().call_later(1.0, old.cleanup)
    return True, (frm, target, duration)


# ============================================================================
# 🎚️ LIVE NOW-PLAYING — the equalizer bar in the "now playing" message tracks the song's real position
#    One tracker per guild. Position comes from SimpleAudioSource.elapsed (audio frames actually
#    played, so pauses are handled). The message is edited every NP_INTERVAL seconds; Discord allows
#    ~5 edits / 5s per channel, so 3s leaves plenty of room for the bot's other messages.
#    Disable with ARKEN_NP_LIVE=0 (messages then stay static, as before).
# ============================================================================

NP_LIVE_ENABLED = aenv.get("NP_LIVE", "1").strip().lower() not in ("0", "false", "no", "off")
NP_INTERVAL = max(1.0, float(aenv.get("NP_INTERVAL", "3")))
NP_TICK = 0.5  # how often we check whether the track ended (so the final edit is prompt)

_np_trackers = {}  # guild_id -> NowPlayingTracker


class JumpModal(discord.ui.Modal, title="Jump to a time"):
    """Pop-up asking for an exact position ('1:30', '90', '+30', '-15')."""

    def __init__(self, guild_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.time_input = discord.ui.TextInput(label="Time  (1:30 · 90 · +30 · -15)", placeholder="1:30",
                                               max_length=12, required=True)
        self.add_item(self.time_input)

    async def on_submit(self, interaction):
        guild = interaction.guild or bot.get_guild(self.guild_id)
        member = guild.get_member(interaction.user.id) if guild else None
        if member is None or not member_can_control(member):
            await interaction.response.send_message("❌ **Access denied.** This needs DJ / admin / openplay.",
                                                    ephemeral=True, delete_after=6)
            return
        try:
            mode, secs = parse_seek(self.time_input.value)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True, delete_after=8)
            return
        ok, info = seek_current(self.guild_id, **{mode: secs})
        if ok:
            await interaction.response.defer()
        else:
            await interaction.response.send_message(f"❌ {info}", ephemeral=True, delete_after=6)


def scrub_points(duration, steps=None):
    """Evenly spaced (seconds, percent) jump targets across a track: 0%, 5%, 10% ... 95%."""
    steps = steps or SCRUB_STEPS
    pts, seen = [], set()
    for k in range(steps):
        secs = int(duration * k / steps)
        if secs not in seen:
            seen.add(secs)
            pts.append((secs, int(round(100 * k / steps))))
    return pts


class NowPlayingView(discord.ui.View):
    """
    Controls under the now-playing message:
      row 1: rewind 10s · pause/resume · skip · forward 10s
      row 2: stop · mute (shows the state) · "jump to exact time" pop-up
      row 3: volume dropdown (0-200%); its label shows the current volume
      row 4: scrub dropdown with numbered jump points; its label shows the live position
    At most 4 buttons per row, because narrow windows / phones wrap a 5th one onto its own line.
    """

    def __init__(self, guild_id, duration=0):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.duration = duration or 0
        sec, pri, dan = discord.ButtonStyle.secondary, discord.ButtonStyle.primary, discord.ButtonStyle.danger

        def add(emoji, callback, style=sec, row=0, label=None):
            b = discord.ui.Button(emoji=emoji, label=label, style=style, row=row)
            b.callback = callback
            self.add_item(b)
            return b

        add("⏪", self._rewind)  # emoji-only buttons, max 4 per row
        self.pause_btn = add("⏸️", self._pause, pri)
        add("⏭️", self._skip)
        add("⏩", self._forward)
        add("⏹️", self._stop, dan, row=1)
        self.mute_btn = add("🔊", self._mute, row=1)
        add("🎯", self._jump, row=1)
        add("🗳️", self._vote, row=1)
        vol_options = [
            discord.SelectOption(
                label="0%  ·  mute" if pct == 0 else f"{pct}%" + ("  ·  boost" if pct > 100 else ""),
                value=str(pct),
                description="▮" * (min(pct, 100) // 10) + "▯" * (10 - min(pct, 100) // 10))
            for pct in range(0, 201, 10)
        ]
        self.volume_sel = discord.ui.Select(placeholder="🔊 Volume 80%", options=vol_options, row=2)
        self.volume_sel.callback = self._volume_pick
        self.add_item(self.volume_sel)
        self.scrub = None
        if self.duration > 0:
            options = [
                discord.SelectOption(
                    label=f"{vapor._fmt_clock(secs)}  ·  {pct}%", value=str(secs),
                    description="▰" * (pct // 10) + "▱" * (10 - pct // 10))
                for secs, pct in scrub_points(self.duration)
            ]
            self.scrub = discord.ui.Select(placeholder=f"⏱ 0:00 / {vapor._fmt_clock(self.duration)} — scrub to…",
                                           options=options, row=3)
            self.scrub.callback = self._scrub
            self.add_item(self.scrub)

    def sync(self, paused, elapsed=None, volume=None):
        self.pause_btn.emoji = "▶️" if paused else "⏸️"
        if volume is not None:
            pct = int(round(volume * 100))
            self.volume_sel.placeholder = "🔇 Muted" if pct == 0 else f"🔊 Volume {pct}%"
            self.mute_btn.emoji = "🔇" if pct == 0 else "🔊"
            self.mute_btn.style = discord.ButtonStyle.danger if pct == 0 else discord.ButtonStyle.secondary
        if self.scrub is not None and elapsed is not None:
            now = vapor._fmt_clock(min(elapsed, self.duration))
            self.scrub.placeholder = f"⏱ {now} / {vapor._fmt_clock(self.duration)} — scrub to…"

    async def _scrub(self, interaction):
        if not await self._guard(interaction):
            return
        ok, info = seek_current(self.guild_id, to=float(self.scrub.values[0]))
        await self._done(interaction, ok, info)

    async def _jump(self, interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(JumpModal(self.guild_id))

    async def _guard(self, interaction, need_auth=True):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("❌ These buttons belong to another server.", ephemeral=True)
            return False
        if need_auth:
            guild = interaction.guild or bot.get_guild(self.guild_id)
            member = guild.get_member(interaction.user.id) if guild else None
            if member is None or not member_can_control(member):
                print(f"🚫 Unauthorized button press: {interaction.user} ({interaction.user.id})")
                await interaction.response.send_message(
                    "❌ **Access denied.** Need Manage Server, the DJ role, openplay, or be a bot owner.",
                    ephemeral=True, delete_after=6,
                )
                return False
        return True

    async def _done(self, interaction, ok, text):
        if ok:
            await interaction.response.defer()  # the tracker refreshes the message itself
        else:
            await interaction.response.send_message(f"❌ {text}", ephemeral=True, delete_after=6)

    async def _pause(self, interaction):
        if not await self._guard(interaction, need_auth=False):
            return
        vc = _voice_client_for(self.guild_id)
        ok, text = resume_current(self.guild_id) if (vc and vc.is_paused()) else pause_current(self.guild_id)
        await self._done(interaction, ok, text)

    async def _skip(self, interaction):
        if not await self._guard(interaction):
            return
        set_notification_channel(self.guild_id, interaction.channel)
        ok, text = skip_current(self.guild_id)
        await self._done(interaction, ok, text)

    async def _stop(self, interaction):
        if not await self._guard(interaction):
            return
        ok, text = stop_current(self.guild_id)
        await self._done(interaction, ok, text)

    async def _seek_by(self, interaction, delta):
        if not await self._guard(interaction):
            return
        ok, info = seek_current(self.guild_id, by=delta)
        await self._done(interaction, ok, info)

    async def _rewind(self, interaction):
        await self._seek_by(interaction, -SEEK_STEP)

    async def _forward(self, interaction):
        await self._seek_by(interaction, SEEK_STEP)

    def _refresh_now(self):
        tracker = _np_trackers.get(self.guild_id)
        if tracker is not None:
            tracker.force = True  # redraw the label right away

    async def _volume_pick(self, interaction):
        if not await self._guard(interaction):
            return
        set_volume(self.guild_id, float(self.volume_sel.values[0]))
        self._refresh_now()
        await interaction.response.defer()

    async def _vote(self, interaction):
        if not await self._guard(interaction, need_auth=False):
            return
        ok, text, passed = cast_vote(self.guild_id, interaction.user)
        if not ok:
            await interaction.response.send_message(f"❌ {text}", ephemeral=True, delete_after=6)
            return
        await interaction.response.defer()
        set_notification_channel(self.guild_id, interaction.channel)
        await reply_note(interaction.channel, vapor.note("skipped" if passed else "voted",
                                                         _safe_name(interaction.user.display_name), text))

    async def _mute(self, interaction):
        if not await self._guard(interaction):
            return
        toggle_mute(self.guild_id)
        self._refresh_now()
        await interaction.response.defer()


class NowPlayingTracker:
    """Keeps one message's progress bar in step with one audio source until that source stops."""

    def __init__(self, guild_id, source, message, embed, header, title, duration, cached=False):
        self.guild_id = guild_id
        self.source = source
        self.message = message  # a real discord.Message (never a StatusMessage: that would re-send if deleted)
        self.embed = embed      # set when the frame lives in this embed's description
        self.header = header    # BadgeHeader: the GIF badge message sitting above `message`
        self.title = title
        self.duration = duration or 0
        self.cached = cached
        self.view = NowPlayingView(guild_id, duration) if NP_BUTTONS_ENABLED else None
        self.force = False      # set to make the next tick edit immediately (e.g. after a seek)
        self._stop_event = asyncio.Event()
        self._task = None
        self._dead = False
        self.lyrics = None        # None: not looked up yet · False: none found · else [(seconds, text)]
        self._lyric_task = None
        self._start_pos = getattr(source, "start_offset", 0.0)

    def _ensure_lyrics(self):
        if self.lyrics is not None or self._lyric_task is not None:
            return
        self._lyric_task = _spawn(self._load_lyrics())

    async def _load_lyrics(self):
        entry = await fetch_lyrics(self.title, self.duration)
        self.lyrics = entry["lines"] if (entry and entry.get("lines")) else False
        self.force = True

    def _lyric_row(self):
        """None when live lyrics are off, otherwise the current line ('' = a gap in the song)."""
        if not lyrics_enabled(self.guild_id):
            return None
        self._ensure_lyrics()
        if self.lyrics is None:
            return "finding lyrics…"
        if self.lyrics is False:
            return "no synced lyrics for this track"
        return extras.lyric_at(self.lyrics, self.source.elapsed + 0.8)[1]

    def _meta(self):
        bits = []
        who = getattr(self.source, "requester", None)
        if who:
            bits.append(f"🎧 {_safe_name(who)}")
        fx = guild_setting(self.guild_id, "effect", "off")
        if fx and fx != "off" and not getattr(self.source, "game", False):
            bits.append("🎛 " + fx.replace("speed:", "speed x"))
        return " · ".join(bits) or None

    def _interval(self):
        return min(NP_INTERVAL, 2.0) if (self.lyrics and lyrics_enabled(self.guild_id)) else NP_INTERVAL

    async def _record_play(self):
        src = self.source
        if getattr(src, "game", False) or not self.title:
            return
        seconds = max(0.0, getattr(src, "elapsed", 0.0) - self._start_pos)
        try:
            await asyncio.to_thread(extras.record_play, DATA, self.guild_id, self.title,
                                    getattr(src, "video_id", None), getattr(src, "requester", None), seconds)
        except Exception as e:
            print(f"⚠️ could not record play: {e}")

    def _text(self, paused=False, ended=False):
        levels = ends_at = None
        if not (paused or ended):
            if NP_REACTIVE and hasattr(self.source, "spectrum"):
                levels = self.source.spectrum(vapor.NP_BAR_WIDTH)
            if NP_COUNTDOWN and self.duration:
                ends_at = time.time() + max(0.0, self.duration - self.source.elapsed) / (getattr(self.source, "speed", 1.0) or 1.0)
        return vapor.now_playing_live(self.title, self.source.elapsed, self.duration, cached=self.cached,
                                      paused=paused, ended=ended, levels=levels, ends_at=ends_at,
                                      hint=f"⏪⏩ {SEEK_STEP}s" if self.view is not None else None,
                                      lyric=None if ended else self._lyric_row(), meta=self._meta())

    def _is_paused(self):
        vc = _voice_client_for(self.guild_id)
        return bool(vc and vc.is_paused())

    async def _edit(self, text, final=False):
        try:
            body = await self.header.sync(getattr(self.message, "channel", None), text)
            kwargs = {}
            if self.view is not None:
                if final:
                    kwargs["view"] = None  # buttons go away with the track
                    self.view.stop()
                else:
                    self.view.sync(self._is_paused(), self.source.elapsed, get_volume(self.guild_id))
                    kwargs["view"] = self.view
            if self.embed is not None:
                self.embed.description = body
                await self.message.edit(embed=self.embed, **kwargs)
            else:
                await self.message.edit(content=body, **kwargs)
        except discord.NotFound:
            self._dead = True  # message was deleted: stop bothering
        except discord.HTTPException:
            pass  # rate limit / transient: the next tick fixes it

    def start(self):
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self):
        last_edit = time.time()
        shown_paused = False
        if self.view is not None:
            await self._edit(self._text())  # attaches the buttons
            last_edit = time.time()
        while not self._stop_event.is_set() and not self._dead:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=NP_TICK)
                break
            except asyncio.TimeoutError:
                pass
            if current_sources.get(self.guild_id) is not self.source:
                break  # finished, skipped, or stopped
            paused = self._is_paused()
            forced, self.force = self.force, False
            if not forced:
                if paused:
                    if shown_paused:
                        continue  # nothing is moving: don't spend edits on a frozen bar
                elif time.time() - last_edit < self._interval() and not shown_paused:
                    continue
            shown_paused = paused
            last_edit = time.time()
            await self._edit(self._text(paused=paused))
        if not self._dead:
            await self._edit(self._text(ended=True), final=True)  # final, static state
        await self._record_play()
        await update_presence()

    async def stop(self):
        """End tracking now (the final edit is made by the loop). Safe to call repeatedly."""
        self._stop_event.set()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass


async def track_now_playing(guild_id, message, title, duration=0, cached=False, embed=None, header=None):
    """
    Start live progress for the track now playing in this guild. `message` is the (already sent)
    now-playing message; pass `embed` when the frame lives in that embed's description, otherwise
    the message content is edited. `header` is the BadgeHeader sitting above that message.
    Replaces (and finishes) any previous tracker for the guild.
    """
    old = _np_trackers.pop(guild_id, None)
    if old is not None:
        await old.stop()
    source = current_sources.get(guild_id)
    if source is not None:
        apply_saved_volume(guild_id, source)
    await update_presence()
    if not NP_LIVE_ENABLED or message is None or source is None or not hasattr(source, "elapsed"):
        return None
    tracker = NowPlayingTracker(guild_id, source, message, embed, header if header is not None else BadgeHeader(),
                                title, duration, cached)
    _np_trackers[guild_id] = tracker
    tracker.start()
    return tracker


async def send_now_playing(channel, embed, source, guild_id, title, duration=0, cached=False, cover_path=None):
    """Send the GIF badge, then the now-playing embed right under it, and start its live progress."""
    text = now_playing_text(source, title, duration, cached)
    header = BadgeHeader()
    header.cover = load_cover(cover_path)
    if BADGES_ENABLED:
        embed.color = discord.Color(gif_art.style_embed_color(gif_art.effective_style(style_for(guild_id), header.cover)))
    embed.description = await header.sync(channel, text)
    msg = await channel.send(embed=embed)
    await track_now_playing(guild_id, msg, title, duration, cached, embed=embed, header=header)
    return msg


def now_playing_text(source, title, duration, cached=False):
    """First frame of the now-playing message (0:00 and an empty bar, or the live one if disabled)."""
    if NP_LIVE_ENABLED:
        return vapor.now_playing_live(title, getattr(source, "elapsed", 0), duration, cached=cached)
    duration_str = f" • {duration//60}:{duration%60:02d}" if duration else ""
    return vapor.now_playing(title, duration_str, cached)


@bot.command(name="test", help="Test if bot is responding (staff: diagnose)")
@require_staff("diagnose")
async def test(ctx):
    """Simple test command to verify bot is working"""
    print(f"🧪 TEST COMMAND RECEIVED from {ctx.author} ({ctx.author.id}) in {ctx.guild.name}")
    await ctx.send("✅ **Bot is working!** I can see your commands. Try `!summon` now.")

@bot.command(name="summon", help="Summon the bot to your voice channel")
@require_user()
async def summon(ctx):
    print(f"📥 Received !summon command from {ctx.author} ({ctx.author.id}) in {ctx.guild.name}")

    if not ctx.author.voice or not ctx.author.voice.channel:
        print(f"❌ User {ctx.author} is not in a voice channel")
        await ctx.send("❌ You need to be in a voice channel to summon me!")
        return

    channel = ctx.author.voice.channel
    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        await ctx.send("❌ Join a voice or stage channel first.")
        return

    voice_client = ctx.guild.voice_client
    print(f"🎯 Target channel: {channel.name} (ID: {channel.id})")

    # Check permissions first
    permissions = channel.permissions_for(ctx.guild.me)
    print(f"🔐 Permissions check - Connect: {permissions.connect}, Speak: {permissions.speak}")
    
    if not permissions.connect:
        await ctx.send(f"❌ I don't have permission to connect to **{channel.name}**!\nPlease check my permissions in the server settings.")
        print(f"❌ Missing 'Connect' permission for {channel.name} in {ctx.guild.name}")
        return
    
    if not permissions.speak:
        await ctx.send(f"⚠️ I can connect but won't be able to play audio (missing 'Speak' permission)")
        print(f"⚠️ Missing 'Speak' permission for {channel.name} in {ctx.guild.name}")

    if (
        voice_client
        and voice_client.channel == channel
        and _voice_session_usable(voice_client)
    ):
        await ctx.send(f"🔊 I'm already connected to **{channel.name}**!")
        return

    anim = None
    try:
        connecting_msg, anim = await _start_summon_animation(ctx, channel.name)
        print(f"🔄 Attempting to connect to voice channel: {channel.name} (ID: {channel.id}) in {ctx.guild.name}")
        print(f"   Channel type: {type(channel).__name__}")
        print(f"   Guild ID: {ctx.guild.id}")
        print(f"   Bot user: {ctx.guild.me.name} (ID: {ctx.guild.me.id})")
        
        if ctx.guild.voice_client:
            print(f"   Existing voice client found: {ctx.guild.voice_client}")
            print(f"   Existing connection status: {ctx.guild.voice_client.is_connected()}")
        
        print(f"   Starting connection attempt (serialized per guild)...")
        try:
            voice_client = await connect_to_voice_channel(
                ctx.guild, channel, timeout=40.0
            )
            print(f"   Connection completed, got voice_client: {voice_client}")
        except asyncio.TimeoutError as timeout_err:
            print(f"❌ Connection timeout: {timeout_err}")
            raise
        except Exception as connect_err:
            print(f"❌ Error during connect_to_voice_channel: {type(connect_err).__name__}: {connect_err}")
            raise
        
        print(f"   Verifying connection...")
        print(f"   voice_client.is_connected(): {voice_client.is_connected() if voice_client else 'N/A'}")
        print(f"   voice_client.channel: {voice_client.channel if voice_client else 'N/A'}")
        
        if voice_client:
            await _await_voice_connected(voice_client, 5.0)
        await asyncio.sleep(0.5)
        
        is_connected = _voice_session_usable(voice_client) if voice_client else False
        print(f"   After short wait - is_connected: {is_connected}")
        
        if voice_client and is_connected:
            await _finish_summon(connecting_msg, anim, f"🔊 **Connected** to **{channel.name}**!")
            if DASHBOARD_ON_SUMMON:
                try:
                    set_notification_channel(ctx.guild.id, ctx.channel)
                    await post_dashboard(ctx)
                except Exception as e:
                    print(f"⚠️ dashboard failed: {e}")
            print(f"✅ Successfully connected to {channel.name} in {ctx.guild.name}")
            print(f"   Voice client channel: {voice_client.channel.name if voice_client.channel else 'None'}")
        else:
            error_details = f"Voice client exists: {voice_client is not None}, Connected: {is_connected}"
            await _finish_summon(connecting_msg, anim, f"⚠️ Connection issue: {error_details}\n💡 Try `!diagnose` to check status.")
            print(f"⚠️ Connection to {channel.name} failed verification: {error_details}")
            if voice_client:
                print(f"   Voice client state: {voice_client}")
                print(f"   Voice client endpoint: {getattr(voice_client, 'endpoint', 'N/A')}")
            
    except asyncio.TimeoutError:
        error_msg = "❌ Connection timeout! This might be a network/firewall issue.\n💡 Try:\n• Temporarily disabling Windows Firewall\n• Checking your internet connection\n• Using `!nettest` to diagnose"
        try:
            await ctx.send(error_msg)
        except:
            pass
        print(f"❌ Connection timeout to {channel.name} in {ctx.guild.name}")
        import traceback
        traceback.print_exc()
        
    except discord.errors.ClientException as e:
        error_msg = f"❌ Discord client error: {str(e)}\n💡 The bot might already be connected elsewhere or there's a connection issue."
        try:
            await ctx.send(error_msg)
        except:
            pass
        print(f"❌ Discord client error connecting to {channel.name}: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        
    except discord.errors.PrivilegedIntentsRequired as e:
        error_msg = "❌ Missing required intents! Please enable 'MESSAGE CONTENT INTENT' in the Discord Developer Portal."
        try:
            await ctx.send(error_msg)
        except:
            pass
        print(f"❌ Missing privileged intents: {e}")
        import traceback
        traceback.print_exc()
        
    except Exception as e:
        error_msg = f"❌ Failed to connect: {str(e)}\n💡 Error type: {type(e).__name__}\n💡 Try `!diagnose` for more info"
        try:
            await ctx.send(error_msg)
        except:
            pass
        print(f"❌ Unexpected error connecting to {channel.name} in {ctx.guild.name}: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    finally:
        # Any failure path above: stop the donut right away and don't leave it frozen mid-spin
        if anim is not None:
            await anim.stop("❌ **Connection failed**", respect_min=False)

@bot.command(name="play", help="Stream audio content")
@require_authorization()
async def play(ctx, *, query: str):
    """Stream audio content from query"""
    query = _sanitize_user_query(query)
    if not query:
        await ctx.send("❌ Add a song name or link after `!play`.")
        return

    # Dedicated playlist page -> import it (rolling batch download, deleted after play)
    if _is_playlist_url(query):
        await _begin_playlist_import(ctx, query)
        return

    # Set notification channel to where the command was issued
    set_notification_channel(ctx.guild.id, ctx.channel)

    if ctx.guild.id in _games:
        await ctx.send("🎲 A guessing game is running. `!guess stop` ends it first.")
        return
    if not getattr(ctx, "autoplay", False):
        _autoplay_count.pop(ctx.guild.id, None)   # a person asked for something: autoplay's streak restarts

    voice_client = ctx.guild.voice_client
    if not voice_client or not _voice_session_usable(voice_client):
        await ctx.send("❌ I'm not connected to a voice channel! Use `!summon` first.")
        return

    try:
        # Process audio request — no fixed deadline: live progress + processor-heartbeat liveness
        processing_msg = StatusMessage(ctx, vapor.searching())
        await processing_msg.start()

        print(f"📝 Queuing request: {query}")
        request_id = queue_audio_request(query)
        result = await wait_for_request(request_id, on_progress=processing_msg.progress_callback(request_id))

        if result['status'] != 'success':
            await processing_msg.edit(content=vapor.error_label(result.get('message', 'Unknown error')))
            return

        # Get file path from result
        file_path = result['file_path']
        title = result.get('title', 'Unknown')
        duration = result.get('duration', 0)
        cached = result.get('cached', False)
        cover_path = result.get('cover_path')

        if not os.path.exists(file_path):
            await processing_msg.edit(content=vapor.error_label("File not found"))
            return
        
        # Check if something is already playing - if so, queue this song
        voice_client = ctx.guild.voice_client
        if voice_client.is_playing() or voice_client.is_paused():
            # Add to queue instead of interrupting current playback
            song_info = {
                'title': title,
                'file_path': file_path,
                'duration': duration,
                'requested_by': ctx.author,
                'cached': cached,
                'cover_path': cover_path,
                'video_id': result.get('video_id'),
            }
            add_to_song_queue(ctx.guild.id, song_info)
            
            # Show queue position
            queue_position = len(song_queues[ctx.guild.id])
            duration_str = f" • {duration//60}:{duration%60:02d}" if duration else ""
            cached_str = " (cached)" if cached else ""
            
            queue_info = get_queue_info(ctx.guild.id)
            detail = f"#{queue_position} in queue" + (f" · {duration//60}:{duration%60:02d}" if duration else "") + (" · cached" if cached else "")
            lines = [title, detail, f"requested by {ctx.author.display_name}"]
            if len(queue_info) > 1:
                lines.append(f"{len(queue_info)} songs waiting")
            await processing_msg.edit(content=vapor.note("added to queue", *lines))
            return
        
        # Stream the audio file (nothing is currently playing)
        print(f"🎵 Streaming file: {os.path.basename(file_path)}")
        
        # Use FFmpegPCMAudio - Discord.py handles Opus encoding internally
        print(f"🔍 FFmpeg options being used: {ffmpeg_options}")
        print(f"🔍 File path for FFmpeg: {file_path}")
        print(f"🔍 File exists: {os.path.exists(file_path)}")
        print(f"🔍 File size: {os.path.getsize(file_path) if os.path.exists(file_path) else 'N/A'} bytes")
        
        
        audio_source = make_audio_source(ctx.guild.id, file_path, title=title, duration=duration,
                                         requester=_who(ctx.author), video_id=result.get('video_id'))
        print(f"✅ Created audio source")
        
        # Track for cleanup
        current_sources[ctx.guild.id] = audio_source
        
        # Start streaming
        voice_client.play(audio_source, after=lambda e: after_playing(e, ctx.guild.id))
        
        # Verify playback started
        await asyncio.sleep(0.5)
        if voice_client.is_playing():
            processing_msg._header.cover = load_cover(cover_path)  # pixel-art thumbnail on the badge
            await processing_msg.edit(content=now_playing_text(audio_source, title, duration, cached))
            await track_now_playing(ctx.guild.id, processing_msg.msg, title, duration, cached,
                                    header=processing_msg._header)
            print(f"✅ Successfully streaming: {title}")
        else:
            await processing_msg.edit(content=vapor.error_label("failed to start streaming"))
            print(f"❌ Streaming failed for: {title}")
            
    except Exception as e:
        print(f"💥 Error in stream command: {e}")
        try:
            await ctx.send(f"❌ **Streaming error:** {str(e)}")
        except:
            pass

@bot.command(name="pause", help="Pause audio stream")
async def pause(ctx):
    """Pause current stream"""
    ok, text = pause_current(ctx.guild.id)
    if ok:
        src = current_sources.get(ctx.guild.id)
        await reply_note(ctx, vapor.note("paused", getattr(src, "title", "") or "playback paused"))
    else:
        await ctx.send(f"❌ {text}")

@bot.command(name="resume", help="Resume audio stream")
async def resume(ctx):
    """Resume stream"""
    ok, text = resume_current(ctx.guild.id)
    if ok:
        src = current_sources.get(ctx.guild.id)
        await reply_note(ctx, vapor.note("resumed", getattr(src, "title", "") or "playback resumed"))
    else:
        await ctx.send(f"❌ {text}")

@bot.command(name="clearqueue", help="Clear the song queue")
@require_authorization()
async def clear_queue(ctx):
    """Clear all songs from the queue"""
    count = clear_song_queue(ctx.guild.id)
    
    if count == 0:
        await ctx.send("📋 The queue is already empty!")
    else:
        embed = discord.Embed(
            title="🗑️ Queue Cleared",
            description=f"Removed {count} songs from the queue",
            color=0xff9900
        )
        await ctx.send(embed=embed)

@bot.command(name="skip", help="Skip to next track (playlist or !play queue)")
@require_authorization()
async def skip_track(ctx):
    """Skip current track; continues active playlist or advances !play queue."""
    set_notification_channel(ctx.guild.id, ctx.channel)
    ok, text = skip_current(ctx.guild.id)
    if ok:
        await reply_note(ctx, vapor.note("skipped", text.replace("**", "")))
    else:
        await ctx.send(f"❌ {text}")

@bot.command(name="stop", help="Stop audio stream")
async def stop(ctx):
    """Stop current stream (and end any active playlist so it doesn't advance to the next track)"""
    ok, text = stop_current(ctx.guild.id)
    await ctx.send(text if ok else f"❌ {text}")

@bot.command(name="volume", aliases=["vol"], help="Show or set the volume (0-200, or +10 / -10)")
@require_authorization()
async def volume_cmd(ctx, level: str = None):
    """!volume -> show, !volume 60 -> set, !volume +10 / -10 -> nudge"""
    gid = ctx.guild.id
    if level is None:
        await reply_note(ctx, vapor.volume_note(int(round(get_volume(gid) * 100))))
        return
    txt = level.strip().rstrip("%")
    try:
        value = float(txt)
    except ValueError:
        await ctx.send("❌ Usage: `!volume <0-200>` or `!volume +10` / `!volume -10`")
        return
    current = get_volume(gid) * 100
    pct = set_volume(gid, current + value if txt[:1] in "+-" else value)
    await reply_note(ctx, vapor.volume_note(pct))

@bot.command(name="mute", aliases=["unmute"], help="Mute / unmute the bot (remembers the volume)")
@require_authorization()
async def mute_cmd(ctx):
    muted, pct = toggle_mute(ctx.guild.id)
    await reply_note(ctx, vapor.volume_note(pct))
    tracker = _np_trackers.get(ctx.guild.id)
    if tracker is not None:
        tracker.force = True

async def _seek_reply(ctx, ok, info):
    if ok:
        frm, to, dur = info
        await reply_note(ctx, vapor.seek_note(frm, to, dur))
    else:
        await ctx.send(f"❌ {info}")

@bot.command(name="seek", help="Jump to a time in the current song (1:30, 90, +30, -15)")
@require_authorization()
async def seek_cmd(ctx, *, position: str = None):
    try:
        mode, secs = parse_seek(position)
    except ValueError as e:
        await ctx.send(f"❌ {e}. Examples: `!seek 1:30`, `!seek +30`, `!seek -15`")
        return
    ok, info = seek_current(ctx.guild.id, **{mode: secs})
    await _seek_reply(ctx, ok, info)

@bot.command(name="forward", aliases=["ff"], help="Skip ahead in the current song (default 10s)")
@require_authorization()
async def forward_cmd(ctx, seconds: str = None):
    try:
        secs = _parse_clock(seconds) if seconds else SEEK_STEP
    except ValueError as e:
        await ctx.send(f"❌ {e}")
        return
    ok, info = seek_current(ctx.guild.id, by=secs)
    await _seek_reply(ctx, ok, info)

@bot.command(name="rewind", aliases=["rw"], help="Go back in the current song (default 10s)")
@require_authorization()
async def rewind_cmd(ctx, seconds: str = None):
    try:
        secs = _parse_clock(seconds) if seconds else SEEK_STEP
    except ValueError as e:
        await ctx.send(f"❌ {e}")
        return
    ok, info = seek_current(ctx.guild.id, by=-secs)
    await _seek_reply(ctx, ok, info)

@bot.command(name="style", help="Change the look of the animated badges (vaporwave, matrix, sunset, synthwave, auto)")
@require_authorization()
async def style_cmd(ctx, name: str = None):
    names = gif_art.style_names() + ("auto",)   # "auto": colours follow each song's cover art; seat labels theme terminal GIFs
    current = style_for(ctx.guild.id)
    if name is None:
        await reply_note(ctx, vapor.note("style", f"current: {current}", "options: " + ", ".join(names)))
        return
    choice = name.strip().lower()
    if choice not in names:
        await ctx.send("❌ Unknown style. Options: " + ", ".join(f"`{n}`" for n in names))
        return
    set_guild_setting(ctx.guild.id, "style", choice)
    if choice != "auto":
        asyncio.get_running_loop().run_in_executor(None, gif_art.prewarm, choice)  # build its GIFs in the background
    await reply_note(ctx, vapor.note("style", f"style set to {choice}",
                                     *(["colours follow each song's cover art"] if choice == "auto" else [])))

@bot.command(name="kill", help="Disconnect the bot from voice channel")
async def kill(ctx):
    """Disconnect from voice channel"""
    if ctx.guild.voice_client:
        try:
            snapshot_session(ctx.guild.id)
        except Exception as e:
            print(f"⚠️ session snapshot failed: {e}")
        # End any playlist first so stopping playback can't advance it (and prefetching stops)
        PlaylistManager.stop_playlist(ctx.guild.id)

        # Queue cleanup of any active files before disconnecting
        source = current_sources.get(ctx.guild.id)
        if source and hasattr(source, 'file_path') and source.file_path:
            if not str(source.file_path).startswith('playlists'):
                queue_cleanup_request(source.file_path)

        # Stop any playback
        _sleep_cancel(ctx.guild.id)
        _autoplay_count.pop(ctx.guild.id, None)
        game = _games.get(ctx.guild.id)
        if game is not None:
            game.stopping = True
            game.skip.set()
            game.answered.set()
        if ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused():
            _no_autoplay.add(ctx.guild.id)
            ctx.guild.voice_client.stop()

        current_sources.pop(ctx.guild.id, None)
        await ctx.guild.voice_client.disconnect()
        scene = await _scene_file(
            ("shutdown", None), style_for(ctx.guild.id), guild_id=ctx.guild.id
        ) if BADGES_ENABLED else None
        if scene is not None:
            await ctx.send("👋 **Disconnected**", file=scene)
        else:
            await ctx.send("👋 **Disconnected**")
    else:
        await ctx.send("❌ Not connected")

def _build_command_guide_embed(member: discord.Member) -> discord.Embed:
    """Shared embed for !help and !commands."""
    embed = discord.Embed(
        title="🎵 Arken — command guide",
        description=(
            "**Start here:** `!summon` in a voice channel, then play music.\n"
            "DJ commands need **open play** (default on), the server's DJ role, or Manage Server.\n"
            "Detail for one command: `!help play`"
        ),
        color=0x7e57c2,
    )

    embed.add_field(
        name="Voice",
        value="`!summon` · join your channel\n`!kill` · leave voice",
        inline=True,
    )
    embed.add_field(
        name="Playback (DJ)",
        value=(
            "`!play` · search or URL\n"
            "`!pause` · `!resume` · `!skip` · `!stop`\n"
            "`!seek` · `!forward` · `!rewind`\n"
            "`!volume` · `!mute` · `!style`"
        ),
        inline=True,
    )
    embed.add_field(
        name="Queue",
        value="`!queue` · list / manage\n`!clearqueue` · `!voteskip`",
        inline=True,
    )

    embed.add_field(
        name="Playlists (DJ)",
        value=(
            "`!playlist` · full playlist help\n"
            "`!playlist create` · `!playlist play` · shuffle / loop\n"
            "`!playlist prev` · skip with `!skip`"
        ),
        inline=False,
    )

    embed.add_field(
        name="Everyone",
        value=(
            "`!like` · `!likes` · `!history` · `!stats` · `!dashboard`\n"
            "`!queue` · `!voteskip`"
        ),
        inline=True,
    )
    embed.add_field(
        name="Extras",
        value="`!effect` · `!fade` · `!sleep` · `!lyrics`\n`!autoplay` · `!guess`",
        inline=True,
    )

    if member_can_server_admin(member):
        embed.add_field(
            name="Server policy",
            value="`!openplay on|off` · `!djrole @Role | off`",
            inline=False,
        )

    role = staff_role(member.id)
    if role:
        staff_lines = [
            "**Guilds:** `!guilds` · `!allowguild` · `!leaveguild` · `!tokens`",
            "**Ops:** `!reboot` · `!deps` · `!rejoin`",
            "**Debug:** `!diagnose` · `!nettest` · `!region` · `!test`",
        ]
        if role == "owner":
            staff_lines.insert(1, "**Staff (owners only):** `!whitelist` — add/remove staff")
        embed.add_field(
            name=f"Staff ({role})",
            value="\n".join(staff_lines),
            inline=False,
        )

    if role == "owner":
        footer = "Arken owner — full staff capabilities in every server"
    elif role == "operator":
        footer = f"Staff role: {role} · manage guilds/runtime per capability"
    elif member_can_server_admin(member):
        footer = "You can change open play and DJ role for this server"
    elif member_can_control(member):
        footer = "You can use DJ controls in this server"
    else:
        footer = "Need DJ access? Ask for the DJ role or enable open play"
    embed.set_footer(text=f"{footer} · !help · !commands")
    return embed


@bot.command(name="commands", help="Show available commands (same as !help)")
async def show_commands(ctx):
    await ctx.send(embed=_build_command_guide_embed(ctx.author))

@bot.command(name="diagnose", help="Run diagnostics (staff: diagnose)")
@require_staff("diagnose")
async def diagnose(ctx):
    """Run diagnostic tests for the bot system"""
    
    embed = discord.Embed(title="🔧 Bot Diagnostics", color=0x00ff00)
    
    # Check FFmpeg
    try:
        import subprocess
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            ffmpeg_version = result.stdout.split('\n')[0]
            embed.add_field(name="FFmpeg", value=f"✅ {ffmpeg_version[:50]}...", inline=False)
        else:
            embed.add_field(name="FFmpeg", value="❌ FFmpeg not working properly", inline=False)
    except FileNotFoundError:
        embed.add_field(name="FFmpeg", value="❌ FFmpeg not installed or not in PATH", inline=False)
    except subprocess.TimeoutExpired:
        embed.add_field(name="FFmpeg", value="❌ FFmpeg timeout", inline=False)
    except Exception as e:
        embed.add_field(name="FFmpeg", value=f"❌ FFmpeg error: {str(e)}", inline=False)
    
    # Check ytdlp/yt-dlp
    try:
        result = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            ytdlp_version = result.stdout.strip()
            embed.add_field(name="yt-dlp", value=f"✅ Version {ytdlp_version}", inline=False)
        else:
            embed.add_field(name="yt-dlp", value="❌ yt-dlp not working properly", inline=False)
    except FileNotFoundError:
        embed.add_field(name="yt-dlp", value="❌ yt-dlp not installed or not in PATH", inline=False)
    except subprocess.TimeoutExpired:
        embed.add_field(name="yt-dlp", value="❌ yt-dlp timeout", inline=False)
    except Exception as e:
        embed.add_field(name="yt-dlp", value=f"❌ yt-dlp error: {str(e)}", inline=False)    

    # Check voice client status
    if ctx.guild.voice_client:
        if ctx.guild.voice_client.is_connected():
            embed.add_field(name="Voice Connection", value=f"✅ Connected to {ctx.guild.voice_client.channel.name}", inline=False)
        else:
            embed.add_field(name="Voice Connection", value="⚠️ Voice client exists but not connected", inline=False)
    else:
        embed.add_field(name="Voice Connection", value="❌ Not connected to voice", inline=False)
    
    # Check permissions
    permissions = ctx.guild.me.guild_permissions
    voice_perms = []
    if permissions.connect:
        voice_perms.append("✅ Connect")
    else:
        voice_perms.append("❌ Connect")
        
    if permissions.speak:
        voice_perms.append("✅ Speak")
    else:
        voice_perms.append("❌ Speak")
        
    if permissions.use_voice_activation:
        voice_perms.append("✅ Voice Activity")
    else:
        voice_perms.append("❌ Voice Activity")
    
    embed.add_field(name="Voice Permissions", value="\n".join(voice_perms), inline=False)
    
    # Check if user is in voice
    if ctx.author.voice:
        embed.add_field(name="User Voice Status", value=f"✅ In {ctx.author.voice.channel.name}", inline=False)
    else:
        embed.add_field(name="User Voice Status", value="❌ Not in voice channel", inline=False)
    
    # Check file system components
    queue_status = "✅ Ready" if QUEUE_DIR.exists() else "❌ Missing"
    embed.add_field(name="Queue System", value=queue_status, inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name="nettest", help="Test network connectivity (staff: diagnose)")
@require_staff("diagnose")
async def nettest(ctx):
    """Test network connectivity for the bot system"""
    
    embed = discord.Embed(title="🌐 Network Connectivity Test", color=0xff9900)
    
    # Test DNS resolution for Discord voice servers
    try:
        import socket
        import requests
        # Test DNS resolution for common Discord voice endpoints
        test_hosts = [
            "discord.gg",
            "gateway.discord.gg", 
            "media.discordapp.net"
        ]
        
        dns_results = []
        for host in test_hosts:
            try:
                ip = socket.gethostbyname(host)
                dns_results.append(f"✅ {host} → {ip}")
            except socket.gaierror:
                dns_results.append(f"❌ {host} → DNS Failed")
        
        embed.add_field(name="DNS Resolution", value="\n".join(dns_results), inline=False)
        
    except Exception as e:
        embed.add_field(name="DNS Resolution", value=f"❌ DNS test failed: {str(e)}", inline=False)
    
    # Test if we can reach Discord's API
    try:
        response = requests.get("https://discord.com/api/v9/gateway", timeout=5)
        if response.status_code == 200:
            embed.add_field(name="Discord API", value="✅ Reachable", inline=False)
        else:
            embed.add_field(name="Discord API", value=f"⚠️ Status: {response.status_code}", inline=False)
    except Exception as api_e:
        embed.add_field(name="Discord API", value=f"❌ Unreachable: {str(api_e)}", inline=False)
    
    # Check Windows Firewall status (if possible)
    try:
        import subprocess
        result = subprocess.run(['netsh', 'advfirewall', 'show', 'allprofiles', 'state'], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            if "ON" in result.stdout:
                embed.add_field(name="Windows Firewall", value="🟡 Enabled (may block UDP)", inline=False)
            else:
                embed.add_field(name="Windows Firewall", value="✅ Disabled", inline=False)
        else:
            embed.add_field(name="Windows Firewall", value="❓ Cannot determine status", inline=False)
    except Exception:
        embed.add_field(name="Windows Firewall", value="❓ Cannot check", inline=False)
    
    # System status checks - perform real checks for services and tools
    try:
        status_lines = []

        # 1) Background processing worker: send a lightweight "ping" request and wait briefly for a result file
        try:
            ping_id = str(uuid.uuid4())[:8]
            ping_request = {
                "type": "status_ping",
                "request_id": ping_id,
                "timestamp": time.time()
            }
            ping_file = QUEUE_DIR / f"request_{ping_id}.json"
            with open(ping_file, "w", encoding="utf-8") as f:
                json.dump(ping_request, f)
            background_status = "❌ No response"
            # Wait up to 5 seconds for a result_{id}.json to be created by the background processor
            waited = 0
            while waited < 5:
                await asyncio.sleep(1)
                waited += 1
                res = check_processing_result(ping_id)
                if res:
                    background_status = "✅ Background worker responded"
                    # If worker returns diagnostic info include short message
                    if isinstance(res, dict) and res.get("status"):
                        background_status += f" ({res.get('status')})"
                    break
        except Exception as be:
            background_status = f"❌ Ping error: {str(be)}"

        status_lines.append(f"🔁 Background worker: {background_status}")

        # 2) FFmpeg: check configured path and run -version
        try:
            ffmpeg_path = get_ffmpeg_path()
            ffmpeg_ok = False
            ffmpeg_msg = ""
            try:
                proc = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, timeout=5)
                if proc.returncode == 0 and proc.stdout:
                    ffmpeg_ok = True
                    ffmpeg_msg = proc.stdout.splitlines()[0]
                else:
                    # Try generic ffmpeg in PATH as fallback
                    proc2 = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
                    if proc2.returncode == 0 and proc2.stdout:
                        ffmpeg_ok = True
                        ffmpeg_msg = proc2.stdout.splitlines()[0]
            except FileNotFoundError:
                # try ffmpeg in PATH
                proc2 = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
                if proc2.returncode == 0 and proc2.stdout:
                    ffmpeg_ok = True
                    ffmpeg_msg = proc2.stdout.splitlines()[0]
            except Exception as e:
                ffmpeg_msg = f"Error: {e}"

            status_lines.append(f"🎛️ FFmpeg: {'✅ ' + ffmpeg_msg if ffmpeg_ok else '❌ Not available (' + str(ffmpeg_msg) + ')'}")
        except Exception as fe:
            status_lines.append(f"🎛️ FFmpeg: ❌ Check error ({fe})")

        # 3) yt-dlp availability
        try:
            proc = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                status_lines.append(f"📥 yt-dlp: ✅ Version {proc.stdout.strip()}")
            else:
                status_lines.append("📥 yt-dlp: ❌ Not responding properly")
        except FileNotFoundError:
            status_lines.append("📥 yt-dlp: ❌ Not installed or not in PATH")
        except Exception as ye:
            status_lines.append(f"📥 yt-dlp: ❌ Error ({ye})")

        # 4) Queue directory writable/readable
        try:
            test_file = QUEUE_DIR / f"health_{str(uuid.uuid4())[:8]}.tmp"
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("ok")
            test_file.unlink()
            status_lines.append(f"📂 Queue dir ({QUEUE_DIR}): ✅ Read/Write")
        except Exception as qe:
            status_lines.append(f"📂 Queue dir ({QUEUE_DIR}): ❌ Not writable ({qe})")

        # 5) Media directory writable/readable
        try:
            test_file = MEDIA_DIR / f"health_{str(uuid.uuid4())[:8]}.tmp"
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("ok")
            test_file.unlink()
            status_lines.append(f"📁 Media dir ({MEDIA_DIR}): ✅ Read/Write")
        except Exception as me:
            status_lines.append(f"📁 Media dir ({MEDIA_DIR}): ❌ Not writable ({me})")

        # 6) Disk space (free)
        try:
            usage = shutil.disk_usage(str(Path.cwd()))
            free_mb = usage.free // (1024 * 1024)
            status_lines.append(f"💾 Disk free: {free_mb} MB")
        except Exception as de:
            status_lines.append(f"💾 Disk: ❌ Error ({de})")

        # 7) Bot event loop
        try:
            loop_running = getattr(bot.loop, "is_running", lambda: False)()
            status_lines.append(f"🔌 Bot loop: {'✅ Running' if loop_running else '❌ Not running'}")
        except Exception as le:
            status_lines.append(f"🔌 Bot loop: ❌ Error ({le})")

        # Aggregate into embed
        embed.add_field(
            name="🔒 System Status Checks",
            value="\n".join(status_lines),
            inline=False
        )
    except Exception as e:
        embed.add_field(
            name="🔒 System Status",
            value=f"❌ Failed to run system checks: {e}",
            inline=False
        )
    
    # Voice connection troubleshooting tips
    tips = [
        "🔧 **Try `!summon` to reconnect to voice**",
        "🔄 **Use `!kill` then `!summon` if connection issues**", 
        "📡 **Check your internet connection**",
        "🛡️ **Temporarily disable Windows Firewall if blocked**"
    ]
    
    embed.add_field(name="Troubleshooting Tips", value="\n".join(tips), inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name="playlist", help="Manage playlists")
@require_authorization()
async def playlist_cmd(ctx, action=None, *, args=None):
    """Air-gap safe playlist management - only uses local files"""
    
    # Set notification channel for playlist notifications
    if action in ["play", "next", "prev", "shuffle", "loop", "import"]:
        set_notification_channel(ctx.guild.id, ctx.channel)
    
    if not action:
        embed = discord.Embed(
            title="🎵 Playlist Commands",
            description="Manage your local file playlists",
            color=0x9c27b0
        )
        
        embed.add_field(
            name="📋 Basic Commands",
            value=(
                "`!playlist create <name>` - Create new playlist\n"
                "`!playlist list` - Show all playlists\n"
                "`!playlist add <name>` - Add currently playing song\n"
                "`!playlist show <name>` - Show playlist contents\n"
                "`!playlist clean <name>` - Remove missing files from playlist\n"
                "`!playlist import <youtube url> [keep] [shuffle] [name]` - Import a YouTube playlist (deleted after play unless `keep`)\n"
                "`!playlist media` - Show available local media files"
            ),
            inline=False
        )
        
        embed.add_field(
            name="🎮 Playback Commands",
            value=(
                "`!playlist play <name>` - Play entire playlist\n"
                "`!playlist stop` - Stop current playlist\n"
                "`!playlist skip` - Skip to next song\n"
                "`!playlist prev` - Go to previous song\n"
                "`!playlist status` - Show current playlist status"
            ),
            inline=False
        )
        
        embed.add_field(
            name="🎛️ Smart Controls",
            value=(
                "`!playlist shuffle` - Toggle shuffle mode 🔀\n"
                "`!playlist loop <off/single/all>` - Set loop mode\n"
                "• `off` - No looping ⏹️\n"
                "• `single` - Loop current song 🔂\n"
                "• `all` - Loop entire playlist 🔁"
            ),
            inline=False
        )
        
        embed.add_field(
            name="🔒 Local Storage",
            value="Playlists only reference local media files\nAll playlist management is done locally for fast access",
            inline=False
        )
        
        embed.set_footer(text="Step 1: Basic playlist creation • More features coming")
        await ctx.send(embed=embed)
        return
    
    if action.lower() == "create":
        if not args:
            await ctx.send("❌ Usage: `!playlist create <playlist name>`")
            return
        
        result = PlaylistManager.create_playlist(ctx.guild.id, args)
        
        if result["status"] == "success":
            await ctx.send(f"✅ {result['message']}")
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "list":
        result = PlaylistManager.list_playlists(ctx.guild.id)
        
        if result["status"] == "success":
            playlists = result["playlists"]
            
            if not playlists:
                await ctx.send("📋 No playlists found. Create one with `!playlist create <name>`")
                return
            
            embed = discord.Embed(
                title="📋 Your Playlists",
                color=0x9c27b0
            )
            
            for playlist in playlists:
                duration_str = f"{playlist['duration']//60}:{playlist['duration']%60:02d}" if playlist['duration'] else "0:00"
                embed.add_field(
                    name=f"🎵 {playlist['name']}",
                    value=f"{playlist['files']} files • {duration_str}",
                    inline=True
                )
            
            embed.set_footer(text="Use !playlist show <name> to view playlist contents")
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "add":
        if not args:
            await ctx.send("❌ Usage: `!playlist add <playlist name>`")
            return
        
        # Check if something is currently playing
        voice_client = ctx.guild.voice_client
        if not voice_client or not voice_client.is_playing():
            await ctx.send("❌ Nothing is currently playing! Use `!play <song>` first.")
            return
        
        # Get current playing source
        current_source = current_sources.get(ctx.guild.id)
        if not current_source or not hasattr(current_source, 'file_path') or not current_source.file_path:
            await ctx.send("❌ No file information available for current song.")
            return
        
        # Add song to playlist
        playlist_name = args.strip()
        song_title = getattr(current_source, 'title', 'Unknown')
        duration = getattr(current_source, 'duration', 0)
        
        # Add a small delay to ensure file copying completes before any cleanup
        await asyncio.sleep(0.5)
        
        result = PlaylistManager.add_song_to_playlist(
            ctx.guild.id,
            playlist_name, 
            current_source.file_path, 
            song_title, 
            duration
        )
        print(f"🔍 Add song result: {result}")
        
        if result["status"] == "success":
            embed = discord.Embed(
                title="✅ Song Added to Playlist!",
                description=f"**{song_title}** has been permanently saved",
                color=0x4caf50
            )
            embed.add_field(name="Playlist", value=f"🎵 {playlist_name}", inline=True)
            embed.add_field(name="Duration", value=f"⏱️ {duration//60}:{duration%60:02d}" if duration else "Unknown", inline=True)
            embed.add_field(name="Status", value="🔒 Protected from cleanup", inline=True)
            embed.set_footer(text="Use !playlist show to view playlist contents")
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "play":
        if not args:
            await ctx.send("❌ Usage: `!playlist play <playlist name>`")
            return
        
        # Check if bot is in voice channel
        if not ctx.guild.voice_client:
            await ctx.send("❌ I need to be summoned first! Use `!summon`")
            return
        
        playlist_name = args.strip()
        
        # Start playlist playback
        result = PlaylistManager.start_playlist_playback(playlist_name, ctx.guild.id)
        print(f"🔍 Playlist start result: {result}")
        
        if result["status"] == "success":
            first_song = result["first_song"]
            
            # Play the first song
            audio_file = Path(first_song["audio_file"])
            
            print(f"🔍 First song: {first_song.get('title', 'Unknown')}")
            print(f"🔍 First song path: {audio_file}")
            print(f"🔍 First song exists: {audio_file.exists()}")
            
            if not audio_file.exists():
                await ctx.send(f"❌ First playlist file not found: {audio_file.name}")
                return
            
            voice_client = ctx.guild.voice_client
            
            # Stop current playback
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
                await asyncio.sleep(0.5)
            
            # Start playing first song using global ffmpeg_options
            
            print(f"🔍 About to create FFmpeg source with path: {str(audio_file)}")
            print(f"🔍 FFmpeg options: {ffmpeg_options}")
            
            try:
                # Ensure consistent path format for FFmpeg
                audio_path = str(audio_file).replace('\\', '/')
                print(f"🔍 FFmpeg path (normalized): {audio_path}")
                
                # Simple path normalization for FFmpeg
                
                # Use FFmpegPCMAudio with explicit FFmpeg path - Discord.py handles Opus encoding internally
                audio_source = make_audio_source(ctx.guild.id, audio_file, title=first_song.get("title", "Unknown"),
                                                 duration=first_song.get("duration", 0),
                                                 video_id=first_song.get("video_id"))
                print(f"✅ Created audio source for playlist")
                print(f"✅ Audio source created successfully")
                
                current_sources[ctx.guild.id] = audio_source
                
                print(f"🔍 Voice client connected: {voice_client.is_connected()}")
                print(f"🔍 Voice client playing: {voice_client.is_playing()}")
                print(f"🔍 Voice client paused: {voice_client.is_paused()}")
                
                voice_client.play(audio_source, after=lambda e: after_playing(e, ctx.guild.id))
                print(f"✅ Started playback!")
                
                # First song is songs[0]; next call to get_next_playlist_song must return songs[1]
                if ctx.guild.id in playlist_queues:
                    playlist_queues[ctx.guild.id]["current_index"] = 1
                
                # Check if playback actually started
                await asyncio.sleep(1)
                print(f"🔍 After 1 second - Voice client playing: {voice_client.is_playing()}")
                
            except Exception as e:
                print(f"❌ Error creating audio source: {e}")
                await ctx.send(f"❌ Failed to create audio source: {str(e)}")
                return
            
            # Send confirmation
            embed = discord.Embed(
                title="🎵 Started Playlist Playback!",
                color=0x9c27b0
            )
            embed.add_field(name="Playlist", value=f"🎵 {playlist_name}", inline=True)
            embed.add_field(name="Total Songs", value=f"📊 {result['message'].split()[-2]}", inline=True)
            if first_song.get('duration'):
                duration_str = f"{first_song['duration']//60}:{first_song['duration']%60:02d}"
                embed.add_field(name="Duration", value=f"⏱️ {duration_str}", inline=True)
            
            embed.set_footer(text="Songs will play automatically • Use !playlist stop to end")
            await send_now_playing(ctx, embed, audio_source, ctx.guild.id,
                                   first_song.get('title', 'Unknown'), first_song.get('duration', 0))
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "import":
        if not args:
            await ctx.send("❌ Usage: `!playlist import <youtube playlist url> [keep] [shuffle] [name]`\n"
                           "Tracks are deleted after they play unless you add `keep`.")
            return
        tokens = args.split()
        import_url = tokens[0]
        rest = tokens[1:]
        keep_flag = any(t.lower() == "keep" for t in rest)
        shuffle_flag = any(t.lower() == "shuffle" for t in rest)
        import_name = " ".join(t for t in rest if t.lower() not in ("keep", "shuffle")).strip() or None
        await _begin_playlist_import(ctx, import_url, keep=keep_flag, shuffle=shuffle_flag, custom_name=import_name)

    elif action.lower() == "stop":
        result = PlaylistManager.stop_playlist(ctx.guild.id)
        
        if result["status"] == "success":
            # Stop current playback
            if ctx.guild.voice_client and (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused()):
                ctx.guild.voice_client.stop()
            
            embed = discord.Embed(
                title="⏹️ Playlist Stopped",
                description=result["message"],
                color=0xff5722
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "clean":
        if not args:
            await ctx.send("❌ Usage: `!playlist clean <playlist name>`")
            return
        
        playlist_name = args.strip()
        result = PlaylistManager.clean_playlist(ctx.guild.id, playlist_name)
        
        if result["status"] == "success":
            if "removed" in result["message"]:
                # Files were removed
                embed = discord.Embed(
                    title="🧹 Playlist Cleaned",
                    description=result["message"],
                    color=0xff9900
                )
                if result.get("removed_songs"):
                    removed_list = "\n".join([f"• {song}" for song in result["removed_songs"][:10]])
                    if len(result["removed_songs"]) > 10:
                        removed_list += f"\n... and {len(result['removed_songs']) - 10} more"
                    embed.add_field(
                        name="Removed Songs",
                        value=removed_list,
                        inline=False
                    )
                embed.add_field(
                    name="Remaining Songs",
                    value=f"{result['remaining_songs']} songs",
                    inline=True
                )
                await ctx.send(embed=embed)
            else:
                # No files needed to be removed
                await ctx.send(f"✅ {result['message']}")
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "show":
        if not args:
            await ctx.send("❌ Usage: `!playlist show <playlist name>`")
            return
        
        result = PlaylistManager.get_playlist_contents(ctx.guild.id, args.strip())
        
        if result["status"] == "success":
            playlist = result["playlist"]
            files = playlist.get("files", [])
            
            if not files:
                await ctx.send(f"📋 Playlist '{args}' is empty. Use `!playlist add {args}` while playing a song to add it!")
                return
            
            embed = discord.Embed(
                title=f"📋 Playlist: {playlist['name']}",
                description=f"{len(files)} songs • {playlist['total_duration']//60}:{playlist['total_duration']%60:02d}",
                color=0x9c27b0
            )
            
            for i, song in enumerate(files[:10]):  # Show first 10 songs
                duration_str = f"{song['duration']//60}:{song['duration']%60:02d}" if song['duration'] else "Unknown"
                embed.add_field(
                    name=f"{i+1}. {song['title'][:25]}{'...' if len(song['title']) > 25 else ''}",
                    value=f"⏱️ {duration_str}",
                    inline=True
                )
            
            if len(files) > 10:
                embed.set_footer(text=f"Showing 10 of {len(files)} songs")
            
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "media":
        result = PlaylistManager.get_available_media()
        
        if result["status"] == "success":
            files = result["files"]
            
            if not files:
                await ctx.send("📁 No local media files found. Play some music first to build your library!")
                return
            
            embed = discord.Embed(
                title="📁 Available Local Media Files",
                description="These files can be added to playlists",
                color=0x2196f3
            )
            
            for i, file in enumerate(files[:10]):  # Show first 10 files
                duration_str = f"{file['duration']//60}:{file['duration']%60:02d}" if file['duration'] else "Unknown"
                embed.add_field(
                    name=f"🎵 {file['title'][:30]}{'...' if len(file['title']) > 30 else ''}",
                    value=f"`{file['filename'][:20]}...` • {duration_str}",
                    inline=True
                )
            
            if len(files) > 10:
                embed.set_footer(text=f"Showing 10 of {len(files)} files")
            
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "shuffle":
        result = PlaylistManager.shuffle_playlist(ctx.guild.id)
        
        if result["status"] == "success":
            await reply_note(ctx, vapor.note("shuffled", str(result["message"]).replace("**", "")))
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "loop":
        if not args:
            await ctx.send("❌ Usage: `!playlist loop <off/single/all>`")
            return
        
        result = PlaylistManager.set_loop_mode(ctx.guild.id, args.strip().lower())
        
        if result["status"] == "success":
            embed = discord.Embed(
                title="🔁 Loop Mode",
                description=result["message"],
                color=0x4caf50
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "skip":
        result = PlaylistManager.skip_song(ctx.guild.id, "next")
        
        if result["status"] == "success":
            # Stop current playback to trigger next song (prefer playlist over !play queue)
            if ctx.guild.voice_client and (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused()):
                playlist_force_after_stop.add(ctx.guild.id)
                ctx.guild.voice_client.stop()
            
            embed = discord.Embed(
                title="⏭️ Skip Song",
                description=result["message"],
                color=0x2196f3
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "prev" or action.lower() == "previous":
        result = PlaylistManager.skip_song(ctx.guild.id, "previous")
        
        if result["status"] == "success":
            # Stop current playback and play the previous song
            if ctx.guild.voice_client and (ctx.guild.voice_client.is_playing() or ctx.guild.voice_client.is_paused()):
                playlist_force_after_stop.add(ctx.guild.id)
                ctx.guild.voice_client.stop()
            
            embed = discord.Embed(
                title="⏮️ Previous Song",
                description=result["message"],
                color=0x2196f3
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")
    
    elif action.lower() == "status":
        status = PlaylistManager.get_playlist_status(ctx.guild.id)
        
        if status["status"] == "active":
            shuffle_icon = "🔀" if status["shuffle"] else "📄"
            loop_icons = {"off": "⏹️", "single": "🔂", "all": "🔁"}
            loop_icon = loop_icons.get(status["loop_mode"], "⏹️")
            
            embed = discord.Embed(
                title="🎵 Current Playlist Status",
                color=0x9c27b0
            )
            
            embed.add_field(
                name="📋 Playlist",
                value=f"**{status['playlist_name']}**",
                inline=True
            )
            
            embed.add_field(
                name="📊 Progress",
                value=f"{status['current_position']}/{status['total_songs']} songs",
                inline=True
            )
            
            embed.add_field(
                name="⏳ Remaining",
                value=f"{status['remaining']} songs",
                inline=True
            )
            
            embed.add_field(
                name="🔀 Shuffle",
                value=f"{shuffle_icon} {'On' if status['shuffle'] else 'Off'}",
                inline=True
            )
            
            embed.add_field(
                name="🔁 Loop Mode",
                value=f"{loop_icon} {status['loop_mode'].title()}",
                inline=True
            )
            
            await ctx.send(embed=embed)
        else:
            await ctx.send("❌ No playlist is currently active. Use `!playlist play <name>` to start one!")
    
    else:
        await ctx.send(f"❌ Unknown action: `{action}`. Use `!playlist` to see available commands.")

@bot.command(name="region", help="Set regional audio settings (staff: diagnose)")
@require_staff("diagnose")
async def set_region(ctx, region: str = None):
    """Set regional audio processing settings"""
    global ffmpeg_options
    
    if not region:
        embed = discord.Embed(
            title="🌍 Regional Audio Settings",
            description="Available regional configurations:",
            color=0x00ff00
        )
        
        embed.add_field(
            name="Current Settings",
            value=f"```{ffmpeg_options['options']}```",
            inline=False
        )
        
        embed.add_field(
            name="Available Regions",
            value=(
                "`!region us` - US/English (48kHz, stereo, 128k)\n"
                "`!region eu` - European (44.1kHz, stereo, 128k)\n"
                "`!region hq` - High Quality (48kHz, stereo, 192k)\n"
                "`!region voice` - Voice optimized (48kHz, mono, 64k)"
            ),
            inline=False
        )
        
        await ctx.send(embed=embed)
        return
    
    region = region.lower()
    
    if region == "us":
        ffmpeg_options = {
            'before_options': '',
            'options': '-vn -f s16le -ar 48000 -ac 2 -acodec pcm_s16le -loglevel warning'
        }
        await ctx.send("🇺🇸 **US/English audio settings applied:** 48kHz, stereo, PCM")
        
    elif region == "eu":
        ffmpeg_options = {
            'before_options': '',
            'options': '-vn -f s16le -ar 44100 -ac 2 -acodec pcm_s16le -loglevel warning'
        }
        await ctx.send("🇪🇺 **European audio settings applied:** 44.1kHz, stereo, PCM")
        
    elif region == "hq":
        ffmpeg_options = {
            'before_options': '',
            'options': '-vn -f s16le -ar 48000 -ac 2 -acodec pcm_s16le -loglevel warning -af volume=1.2'
        }
        await ctx.send("🎵 **High Quality audio settings applied:** 48kHz, stereo, PCM, +20% volume")
        
    elif region == "voice":
        ffmpeg_options = {
            'before_options': '',
            'options': '-vn -f s16le -ar 48000 -ac 1 -acodec pcm_s16le -loglevel warning'
        }
        await ctx.send("🎤 **Voice optimized settings applied:** 48kHz, mono, PCM")
        
    else:
        await ctx.send("❌ **Invalid region!** Use: `us`, `eu`, `hq`, or `voice`")

# ============================================================================
# 🛡️ WHITELIST MANAGEMENT COMMANDS
# ============================================================================

async def resolve_user_id(ctx, user_input):
    """Resolve user input to a Discord user ID (supports both ID and username)"""
    if not user_input:
        return None, None
    
    # Try to parse as user ID first
    try:
        user_id = int(user_input)
        # Try to fetch user to validate ID exists
        try:
            user = await bot.fetch_user(user_id)
            return user_id, user
        except:
            # ID doesn't exist, but still return it for error handling
            return user_id, None
    except ValueError:
        pass
    
    # Try to resolve as username (with or without discriminator)
    user_input = user_input.lower()
    
    # Search through guild members first (faster and more accurate)
    if ctx.guild:
        for member in ctx.guild.members:
            # Check username match
            if member.name.lower() == user_input:
                return member.id, member
            # Check display name match
            if member.display_name.lower() == user_input:
                return member.id, member
            # Check old format with discriminator
            if f"{member.name.lower()}#{member.discriminator}" == user_input:
                return member.id, member
    
    # If not found in guild, try global username search (limited)
    # This is less reliable but covers users not in the current guild
    try:
        # Try to find user by global username (this is limited by Discord API)
        for guild in bot.guilds:
            for member in guild.members:
                if member.name.lower() == user_input:
                    return member.id, member
                if member.display_name.lower() == user_input:
                    return member.id, member
    except:
        pass
    
    return None, None

def _split_user_and_role(text: str):
    """'name operator' / '123456 owner' → (user_token, role_or_None)."""
    parts = (text or "").strip().split()
    if len(parts) >= 2 and tenancy.normalize_staff_role(parts[-1]):
        return " ".join(parts[:-1]), tenancy.normalize_staff_role(parts[-1])
    return text, None


@bot.command(
    name="whitelist",
    help="Manage global staff — owners only: !whitelist [add|remove|check|set] …",
)
@require_owner()
async def whitelist_cmd(ctx, action=None, *, user_input=None):
    """Global staff directory — not per-server DJs. Only owners may change this list."""

    if not action:
        staff = STAFF.all()
        embed = discord.Embed(
            title="🛡️ Arken staff",
            description=(
                f"{len(staff)} staff member(s)\n"
                f"**owner** — full elevation (incl. DJ in every server)\n"
                f"**operator** — guilds / runtime / diagnose — no cross-server DJ"
            ),
            color=0x00ff00,
        )
        if staff:
            lines = []
            for uid, meta in sorted(staff.items(), key=lambda kv: (tenancy.STAFF_ROLE_ORDER.index(kv[1]["role"]) if kv[1]["role"] in tenancy.STAFF_ROLE_ORDER else 99, kv[0])):
                try:
                    user = await bot.fetch_user(int(uid))
                    label = f"**{user.name}**"
                except Exception:
                    label = "Unknown"
                lines.append(f"• {label} (`{uid}`) — `{meta['role']}`")
            embed.add_field(name="Members", value="\n".join(lines[:20]), inline=False)
            if len(lines) > 20:
                embed.set_footer(text=f"… and {len(lines) - 20} more")
        else:
            embed.add_field(name="Members", value="No staff yet.", inline=False)
        embed.add_field(
            name="Commands (owners only)",
            value=(
                "`!whitelist add <user>` — add as **operator** (default)\n"
                "`!whitelist set <user> <owner|operator>` — change role\n"
                "`!whitelist remove <user>` · `!whitelist check <user>`"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)
        return

    action = action.lower()
    if action in ("add", "set", "remove", "check") and not user_input:
        await ctx.send(f"❌ Usage: `!whitelist {action} <user> [role]`")
        return

    if action in ("add", "set"):
        user_token, role = _split_user_and_role(user_input)
        if action == "add":
            role = role or "operator"
            if role == "owner":
                await ctx.send(
                    "❌ New staff are added as **operator** by default. "
                    "Use `!whitelist set <user> owner` to promote someone."
                )
                return
        else:
            role = role or None
        if action == "set" and not role:
            await ctx.send("❌ Usage: `!whitelist set <user> <owner|operator>`")
            return
        user_id, user = await resolve_user_id(ctx, user_token)
        if user_id is None:
            await ctx.send(f"❌ **User not found:** `{user_token}`")
            return
        try:
            applied = STAFF.set_role(user_id, role)
        except ValueError as e:
            await ctx.send(f"❌ {e}")
            return
        who = user.name if user else str(user_id)
        verb = "Added" if action == "add" else "Updated"
        await ctx.send(f"✅ **{verb} {who}** (`{user_id}`) as `{applied}`.")
        print(f"🛡️ {ctx.author} {verb.lower()} staff {who} ({user_id}) role={applied}")
        return

    if action == "remove":
        user_id, user = await resolve_user_id(ctx, user_input)
        if user_id is None:
            await ctx.send(f"❌ **User not found:** `{user_input}`")
            return
        owners = [uid for uid, m in STAFF.all().items() if m.get("role") == "owner"]
        if str(user_id) in owners and len(owners) == 1:
            await ctx.send("❌ **Cannot remove the last owner.**")
            return
        if not STAFF.remove(user_id):
            await ctx.send("⚠️ That user is not on the staff list.")
            return
        who = user.name if user else str(user_id)
        await ctx.send(f"✅ **Removed {who}** (`{user_id}`) from staff.")
        print(f"🛡️ {ctx.author} removed staff {who} ({user_id})")
        return

    if action == "check":
        user_id, user = await resolve_user_id(ctx, user_input)
        if user_id is None:
            await ctx.send(f"❌ **User not found:** `{user_input}`")
            return
        role = staff_role(user_id)
        who = user.name if user else str(user_id)
        if not role:
            await ctx.send(f"❌ **{who}** (`{user_id}`) is not staff.")
            return
        caps = ", ".join(sorted(tenancy.role_capabilities(role))) or "(none)"
        await ctx.send(f"✅ **{who}** (`{user_id}`) — role `{role}`\nCapabilities: {caps}")
        return

    await ctx.send("❌ Use: `add`, `set`, `remove`, or `check`")


@bot.command(name="guilds", help="List Discord servers and approval status (staff: manage_guilds)")
@require_staff("manage_guilds")
async def guilds_cmd(ctx):
    """Show every known server and whether Arken may run there."""
    rows = []
    connected = {g.id: g for g in bot.guilds}
    for gid, info in sorted(GUILD_REGISTRY.all().items(), key=lambda kv: kv[1].get("name") or kv[0]):
        g = connected.get(int(gid))
        mark = "🟢" if info.get("status") == "approved" else ("🟡" if info.get("status") == "pending" else "🔴")
        here = " · connected" if g else ""
        seat = TOKEN_POOL.seat_of(gid) if TOKEN_POOL.enabled() else None
        seat_s = f" · **{seat}**" if seat else ""
        voice = ""
        if g and g.voice_client and g.voice_client.channel:
            src = current_sources.get(g.id)
            title = getattr(src, "title", None) if src else None
            voice = f" · 🔊 {g.voice_client.channel.name}"
            if title:
                voice += f" · {title[:40]}"
        rows.append(
            f"{mark} **{info.get('name') or gid}** (`{gid}`) — {info.get('status')}{seat_s}{here}{voice}"
        )
    for g in bot.guilds:
        if str(g.id) not in GUILD_REGISTRY.all():
            rows.append(f"⚪ **{g.name}** (`{g.id}`) — unknown")
    embed = discord.Embed(
        title="🛡️ Arken servers",
        description="\n".join(rows) if rows else "No servers recorded yet.",
        color=0x00bcd4,
    )
    footer = "!allowguild <id> (this city) · --auto · !leaveguild <id> [--purge]"
    if TOKEN_POOL.enabled():
        footer += " · !tokens"
    embed.set_footer(text=footer)
    await ctx.send(embed=embed)


@bot.command(name="tokens", aliases=["tokenpool", "pool"], help="Show token-pool load balance (staff: manage_guilds)")
@require_staff("manage_guilds")
async def tokens_cmd(ctx):
    if not TOKEN_POOL.enabled():
        await ctx.send(
            "ℹ️ Token pool is **off**. Set `ARKEN_TOKEN_POOL` + `ARKEN_TOKEN_ID` "
            "(one process per Discord bot token) to enable balancing."
        )
        return
    snap = TOKEN_POOL.snapshot()
    here_name = tenancy.display_token_name(TOKEN_ID) if TOKEN_ID else "?"
    lines = [
        f"This process: **{here_name}** (`{TOKEN_ID or '?'}`) · "
        f"cap **{snap['max_guilds_per_token']}** guilds/token",
        f"Assigned guilds: **{snap['assigned_guilds']}** across **{snap['token_count']}** token(s)",
        "",
    ]
    for tok in snap["tokens"]:
        mark = "🔴" if tok["at_cap"] else ("🟢" if tok["slots_free"] else "🟡")
        mine = " ← here" if tok["token_id"] == TOKEN_ID else ""
        city = tok.get("display_name") or tok["token_id"]
        lines.append(
            f"{mark} **{city}** (`{tok['token_id']}`){mine} — "
            f"{tok['guilds']}/{snap['max_guilds_per_token']} guilds · "
            f"{tok['members_total']} members · "
            f"{tok['active_players']} playing · "
            f"score {tok['load_score']}"
        )
        for seat in tok.get("seats") or []:
            label = seat.get("seat") or seat.get("callsign") or "?"
            gname = seat.get("name") or seat.get("guild_id")
            lines.append(f" · {label} — {gname} (`{seat.get('guild_id')}`)")
        free = tok.get("callsigns_free") or []
        if free:
            free_labels = ", ".join(tenancy.display_callsign_name(cs) for cs in free)
            lines.append(f" · free seats: {free_labels}")
    embed = discord.Embed(
        title="⚖️ Token pool",
        description="\n".join(lines),
        color=0x7e57c2,
    )
    embed.set_footer(
        text="City = token · Location = guild seat · sticky until !leaveguild"
    )
    await ctx.send(embed=embed)


def _parse_allowguild_flags(note: str | None) -> tuple[str | None, bool, str | None]:
    """Return (clean_note, auto_balance, force_token) from optional note/flags tail."""
    if not note:
        return None, False, None
    text = note.strip()
    auto = bool(re.search(r"(?:^|\s)--(?:auto|balance)\b", text, re.I))
    force_token = None
    m = re.search(r"(?:^|\s)--token\s+([a-z0-9_-]+)", text, re.I)
    if m:
        force_token = m.group(1).lower()
    clean = re.sub(r"(?:^|\s)--(?:auto|balance)\b", "", text, flags=re.I)
    clean = re.sub(r"(?:^|\s)--token\s+[a-z0-9_-]+", "", clean, flags=re.I).strip()
    return (clean or None), auto, force_token


@bot.command(
    name="allowguild",
    aliases=["approveguild"],
    help="Approve a server; pool city locks when that city's bot joins (or use --auto/--token)",
)
@require_staff("manage_guilds")
async def allowguild_cmd(ctx, guild_id: str = None, *, note: str = None):
    if not guild_id or not guild_id.isdigit():
        await ctx.send(
            "❌ Usage: `!allowguild <server_id> [note]`\n"
            "**Default:** approve only — invite a city bot; `source=invite` locks the token.\n"
            "`!allowguild <id> --auto` — balance before invite. "
            "`!allowguild <id> --token osgiliath` — reserve a city before invite."
        )
        return
    gid = int(guild_id)
    g = bot.get_guild(gid)
    members = _guild_member_count(g) if g else 0
    reg_note, auto_balance, force_token = _parse_allowguild_flags(note)
    assigned = TOKEN_POOL.token_of(gid) if TOKEN_POOL.enabled() else None
    seat = TOKEN_POOL.seat_of(gid) if TOKEN_POOL.enabled() else None
    if TOKEN_POOL.enabled():
        try:
            assign_kw = dict(
                members=members or None,
                name=g.name if g else None,
            )
            if auto_balance:
                assigned = TOKEN_POOL.assign(gid, source="balance", **assign_kw)
                seat = TOKEN_POOL.seat_of(gid)
            elif force_token:
                if assigned and assigned != force_token:
                    await ctx.send(
                        f"❌ Server already locked to **{tenancy.display_token_name(assigned)}** "
                        f"(via invite). Use `!leaveguild {gid} --purge` first."
                    )
                    return
                assigned = TOKEN_POOL.assign(
                    gid, token_id=force_token, source="staff", **assign_kw
                )
                seat = TOKEN_POOL.seat_of(gid)
            elif g is not None and TOKEN_ID and guild_served_here(gid):
                assigned = TOKEN_POOL.assign(
                    gid, token_id=TOKEN_ID, source="staff", **assign_kw
                )
                seat = TOKEN_POOL.seat_of(gid)
            elif assigned:
                seat = TOKEN_POOL.seat_of(gid)
            # else: approve only; wait for invite join to call _pool_claim_from_invite
        except tenancy.PoolFullError as e:
            await ctx.send(f"❌ Cannot approve — token pool is full: {e}")
            return
        except tenancy.CallsignExhaustedError as e:
            await ctx.send(f"❌ Cannot approve — no free callsign on that token: {e}")
            return
        except ValueError as e:
            await ctx.send(f"❌ {e}")
            return
    GUILD_REGISTRY.approve(gid, name=g.name if g else None, note=reg_note)
    if g is not None and seat and guild_served_here(gid):
        await _sync_seat_nickname(g)
    msg = f"✅ Approved server `{gid}`" + (f" (**{g.name}**)" if g else "") + "."
    if seat:
        msg += (
            f" Seat **{seat}** — members see **{seat}** "
            f"in that server (per-server nickname)."
        )
    elif assigned:
        msg += f" Reserved for **{tenancy.display_token_name(assigned)}** (`{assigned}`)."
    if TOKEN_POOL.enabled():
        if assigned and TOKEN_ID and assigned != TOKEN_ID:
            msg += (
                f" Invite the **{tenancy.display_token_name(assigned)}** bot "
                f"(not **{tenancy.display_token_name(TOKEN_ID)}**)."
            )
        elif not assigned:
            msg += (
                " Invite a **city** bot from the developer portal — "
                "the pool locks to whichever bot joins first."
            )
        elif g is None and assigned and not auto_balance and not force_token:
            msg += (
                f" When **{tenancy.display_token_name(assigned)}** joins, "
                f"it will claim this seat."
            )
    await ctx.send(msg)


@bot.command(name="leaveguild", help="Leave a Discord server; optional --purge (staff: manage_guilds)")
@require_staff("manage_guilds")
async def leaveguild_cmd(ctx, guild_id: str = None, *, flags: str = None):
    if not guild_id or not guild_id.isdigit():
        await ctx.send("❌ Usage: `!leaveguild <server_id> [--purge]`")
        return
    gid = int(guild_id)
    purge = bool(flags and "--purge" in flags.lower())
    GUILD_REGISTRY.revoke(gid)
    if TOKEN_POOL.enabled():
        TOKEN_POOL.release(gid)
    g = bot.get_guild(gid)
    left = False
    if g is not None:
        try:
            await g.leave()
            left = True
        except Exception as e:
            await ctx.send(f"⚠️ Revoked, but leave failed: {e}")
            return
    msg = f"👋 Revoked `{gid}`" + (" and left." if left else " (was not connected).")
    if purge:
        with DATA.lock:
            removed = tenancy.purge_guild_data(DATA.data, PLAYLIST_DIR, gid)
            if removed["data"]:
                DATA.save()
        if str(gid) in _settings:
            del _settings[str(gid)]
            try:
                _write_json_atomic(SETTINGS_FILE, _settings)
            except Exception:
                pass
        msg += f" Purged stored data={removed['data']} playlists={removed['playlists_dir']}."
    await ctx.send(msg)


@bot.command(name="openplay", help="Let everyone use DJ controls: !openplay [on|off] (default on)")
@require_server_admin()
async def openplay_cmd(ctx, state: str = None):
    """Server policy. Default is ON for new/unset servers."""
    if state is None:
        on = openplay_enabled(ctx.guild.id)
        await ctx.send(
            f"🔓 Open play is **{'on' if on else 'off'}** in this server "
            f"(default is **on** until changed)."
        )
        return
    val = state.strip().lower() in ("1", "on", "true", "yes", "open")
    set_guild_setting(ctx.guild.id, "openplay", val)
    await ctx.send(
        f"🔓 Open play **{'enabled' if val else 'disabled'}** — "
        f"{'anyone can use DJ controls' if val else 'only DJ role, Manage Server, or Arken owner'}."
    )


@bot.command(name="djrole", help="Set the DJ role for this server: !djrole @Role | off")
@require_server_admin()
async def djrole_cmd(ctx, *, arg: str = None):
    """Server policy: who may use DJ controls when openplay is off."""
    text = (arg or "").strip().lower()
    if not text and not ctx.message.role_mentions:
        rid = guild_setting(ctx.guild.id, "dj_role")
        if not rid:
            await ctx.send("🎛️ No DJ role set. Use `!djrole @Role` or `!djrole off`.")
        else:
            r = ctx.guild.get_role(int(rid))
            await ctx.send(f"🎛️ DJ role: {r.mention if r else rid}")
        return
    if text in ("off", "none", "clear"):
        set_guild_setting(ctx.guild.id, "dj_role", None)
        await ctx.send("🎛️ DJ role cleared.")
        return
    role = ctx.message.role_mentions[0] if ctx.message.role_mentions else None
    if role is None and arg and arg.strip().isdigit():
        role = ctx.guild.get_role(int(arg.strip()))
    if role is None:
        await ctx.send("❌ Usage: `!djrole @Role` or `!djrole off`")
        return
    set_guild_setting(ctx.guild.id, "dj_role", role.id)
    await ctx.send(f"🎛️ DJ role set to {role.mention}. Members with it can use DJ controls.")


@bot.command(name="deps", help="Upgrade Python dependencies (staff: manage_runtime)")
@require_staff("manage_runtime")
async def deps_cmd(ctx, action: str = None):
    """Run pip install -U -r requirements.txt on the host (same Python as the bot)."""
    if not action or action.lower() not in ("update", "upgrade", "sync"):
        await ctx.send(
            "📦 **Usage:** `!deps update` — runs `pip install --upgrade -r requirements.txt` "
            "for this bot’s Python.\n"
            "💡 Enable automatic upgrades on startup with env `ARKEN_AUTO_UPDATE_DEPS=1` "
            "(optional: `ARKEN_AUTO_UPDATE_YTDLP_ONLY=1` for yt-dlp only; "
            "`ARKEN_SKIP_DEP_UPDATE=1` to skip when auto-update is on)."
        )
        return
    await ctx.send("📦 Upgrading dependencies (may take a minute)...")
    from arken_deps import upgrade_from_requirements

    ok = await asyncio.to_thread(upgrade_from_requirements)
    if ok:
        await ctx.send(
            "✅ **Dependencies updated.** Use `!reboot` so all processes pick up new versions."
        )
    else:
        await ctx.send("❌ **Dependency update failed.** Check the machine console for pip output.")

@bot.command(name="reboot", help="Restart the bot (staff: manage_runtime)")
@require_staff("manage_runtime")
async def reboot_bot(ctx):
    """Restart the bot - authorized users only"""
    
    embed = discord.Embed(
        title="🔄 Bot Restart",
        description="Restarting the bot...",
        color=0xff9900
    )
    
    embed.add_field(
        name="Status",
        value="✅ Shutdown initiated by authorized user",
        inline=False
    )
    
    embed.add_field(
        name="Restart Process",
        value=(
            "1. Disconnecting from voice channels\n"
            "2. Saving current state\n"
            "3. Shutting down bot process\n"
            "4. Automatic restart (if configured)"
        ),
        inline=False
    )
    
    embed.set_footer(text=f"Requested by {ctx.author.name}")
    
    await ctx.send(embed=embed)
    
    # Log the reboot request
    print(f"🔄 Bot reboot requested by {ctx.author} ({ctx.author.id})")
    
    try:
        # Check if anything is currently playing
        has_active_playback = False
        for guild in bot.guilds:
            if guild.voice_client and (guild.voice_client.is_playing() or guild.voice_client.is_paused()):
                has_active_playback = True
                break
        
        # Save voice channel state before disconnecting
        save_voice_state()
        
        # Save playback state if there's active playback
        if has_active_playback:
            playback_saved = save_playback_state()
            if playback_saved:
                print("🎵 Active playback detected - cleanup will be bypassed to preserve files")
                print("🔄 Playback and queue will be restored after restart")
            else:
                print("⚠️ Failed to save playback state, proceeding with normal cleanup")
                has_active_playback = False
        
        # Disconnect from all voice channels gracefully
        for guild in bot.guilds:
            if guild.voice_client:
                print(f"🔌 Disconnecting from voice channel in {guild.name}")
                await guild.voice_client.disconnect()
        
        # Conditionally clean up based on playback state
        if has_active_playback:
            # Only clear in-memory state, preserve files for restoration
            print("🎵 Preserving playback state - skipping file cleanup")
            current_sources.clear()
            # Don't clear song_queues and playlist_queues - they're saved in playback state
            # Don't clear notification_channels - they're saved in playback state
        else:
            # Normal cleanup when nothing is playing
            current_sources.clear()
            song_queues.clear()
            playlist_queues.clear()
            notification_channels.clear()
            print("🧹 Cleaned up bot state")
        
        print("🔄 Bot shutting down for restart...")
        
        # Close the bot connection
        await bot.close()
        
    except Exception as e:
        print(f"⚠️ Error during graceful shutdown: {e}")
        # Force shutdown if graceful fails
        await bot.close()

@bot.command(name="rejoin", help="Rejoin saved voice channels (staff: manage_runtime)")
@require_staff("manage_runtime")
async def rejoin_cmd(ctx):
    """Manually rejoin saved voice channels"""
    
    embed = discord.Embed(
        title="🔊 Voice Channel Rejoin",
        description="Attempting to rejoin saved voice channels...",
        color=0x00ff00
    )
    
    await ctx.send(embed=embed)
    
    # Log the rejoin request
    print(f"🔊 Manual rejoin requested by {ctx.author} ({ctx.author.id})")
    
    # Attempt to rejoin channels
    await rejoin_voice_channels()
    
    # Send completion message
    embed = discord.Embed(
        title="✅ Rejoin Complete",
        description="Voice channel rejoin process completed.",
        color=0x00ff00
    )
    
    embed.set_footer(text=f"Requested by {ctx.author.name}")
    await ctx.send(embed=embed)

@bot.command(name="voiceregion", help="Show voice channel region info")
async def voice_region(ctx):
    """Show voice channel region information and explain region changes"""
    
    embed = discord.Embed(
        title="🌍 Voice Channel Region",
        description="Voice channel region information",
        color=0x00ff00
    )
    
    # Get current voice channel info
    if ctx.author.voice and ctx.author.voice.channel:
        voice_channel = ctx.author.voice.channel
        
        # Try to get region info (may not be available in newer Discord API)
        try:
            # This is the old way - may not work in current Discord API
            region = voice_channel.rtc_region if hasattr(voice_channel, 'rtc_region') else None
            if region:
                embed.add_field(
                    name="Current Region",
                    value=f"📍 **{region}**",
                    inline=True
                )
            else:
                embed.add_field(
                    name="Current Region",
                    value="📍 **Auto** (Server default)",
                    inline=True
                )
        except:
            embed.add_field(
                name="Current Region",
                value="📍 **Auto** (Server default)",
                inline=True
            )
        
        embed.add_field(
            name="Voice Channel",
            value=f"🔊 **{voice_channel.name}**",
            inline=True
        )
        
        embed.add_field(
            name="Server",
            value=f"🏠 **{ctx.guild.name}**",
            inline=True
        )
        
    else:
        embed.add_field(
            name="Voice Channel",
            value="❌ **Not in a voice channel**",
            inline=False
        )
    
    # Explain region change situation
    embed.add_field(
        name="⚠️ Region Changes",
        value=(
            "**Voice channel region changes are no longer supported by Discord bots.**\n\n"
            "**To change voice channel region:**\n"
            "1. Right-click the voice channel\n"
            "2. Select 'Edit Channel'\n"
            "3. Go to 'Region Override' section\n"
            "4. Choose your preferred region\n\n"
            "**Available regions:** US East, US West, US South, US Central, Europe (Amsterdam/London/Frankfurt), Asia (Singapore/Hong Kong/Japan), Australia, Brazil"
        ),
        inline=False
    )
    
    embed.add_field(
        name="📋 Available Regions",
        value=(
            "🇺🇸 **US East** (New York)\n"
            "🇺🇸 **US West** (San Francisco)\n"
            "🇺🇸 **US South** (Atlanta)\n"
            "🇺🇸 **US Central** (Chicago)\n"
            "🇪🇺 **Europe** (Amsterdam/London/Frankfurt)\n"
            "🌏 **Asia** (Singapore/Hong Kong/Japan)\n"
            "🇦🇺 **Australia** (Sydney)\n"
            "🇧🇷 **Brazil** (São Paulo)"
        ),
        inline=False
    )
    
    embed.set_footer(text="Region changes require server administrator permissions")
    await ctx.send(embed=embed)

# ============================================================================
# ✨ EXTRAS — audio effects, fade, sleep timer, interactive queue, vote skip, stats + weekly recap,
#    favourites / history, live lyrics, autoplay radio and a "guess the song" game.
#    Pure logic lives in arken_extras.py. Anything needing the network (lyrics, related tracks)
#    goes through the audio processor like every download does: the bot itself never touches the net.
# ============================================================================

DATA = extras.JsonStore(
    Path(aenv.get("DATA_FILE") or _resolve_state_json("arken_data.json", "gondor_data.json"))
)
AUTOPLAY_MAX = max(1, int(aenv.get("AUTOPLAY_MAX", "25")))   # songs in a row without a human request
RECAP_ENABLED = aenv.flag("RECAP")             # weekly "what did we listen to" post
LYRICS_ENABLED = aenv.flag("LYRICS")           # master switch for lyric lookups
GUESS_CLIP_SECONDS = 12
GUESS_ROUND_SECONDS = 35                               # time to answer, counted from the start of the clip
SLEEP_FADE_SECONDS = 15
RECAP_EVERY = 7 * 24 * 3600
QUEUE_PAGE = 8

_no_autoplay = set()      # guilds where the playback that is ending was stopped on purpose (!stop, !kill, sleep)
_autoplay_busy = set()
_autoplay_count = {}      # guild_id -> songs played by autoplay since a person last asked for something
_skip_votes = {}          # guild_id -> (id(source), {user ids})
_sleep_state = {}         # guild_id -> {"mode": "timer" | "end", "ends": unix time, "task": asyncio.Task | None}
_games = {}               # guild_id -> GuessGame
_lyrics_cache = OrderedDict()


def _safe_name(name, limit=24):
    """A display name that is safe to print outside a code block (no pings, no markdown)."""
    text = discord.utils.escape_markdown(discord.utils.escape_mentions(str(name or "")))
    return " ".join(text.split())[:limit]


def _who(user):
    return getattr(user, "display_name", None) or getattr(user, "name", None)


def queue_info_request(kind, **fields):
    """Queue a non-download request for the processor ('lyrics', 'related')."""
    rid = str(uuid.uuid4())[:8]
    data = {"type": kind, "request_id": rid, "timestamp": time.time(), "lane": "foreground", **fields}
    _write_json_atomic(QUEUE_DIR / f"request_{rid}.json", data)
    return rid


class PseudoCtx:
    """Just enough of a commands.Context for calling a command's callback from a button / the autoplay loop."""

    def __init__(self, guild, channel, author, autoplay=False):
        self.guild, self.channel, self.author = guild, channel, author
        self.autoplay = autoplay
        self.bot, self.message, self.command = bot, None, None
        self.me = guild.me
        self.voice_client = guild.voice_client

    async def send(self, *args, **kwargs):
        return await self.channel.send(*args, **kwargs)


def _youtube_ref(item):
    vid = item.get("vid")
    return f"https://www.youtube.com/watch?v={vid}" if vid else item.get("title", "")


def _play_impl():
    """`!play` without its whitelist wrapper: the callers (menus, autoplay) do their own permission checks."""
    return getattr(play.callback, "__wrapped__", play.callback)


async def queue_track(pctx, item):
    """Queue/play one remembered track exactly like `!play` would."""
    await _play_impl()(pctx, query=_youtube_ref(item))


# ---------------------------------------------------------------------------
# Effects, fade
# ---------------------------------------------------------------------------

def guild_effect(guild_id):
    return extras.effect_by_key(guild_setting(guild_id, "effect", "off") or "off")


def guild_fade(guild_id):
    try:
        return max(0.0, min(12.0, float(guild_setting(guild_id, "fade", 0) or 0)))
    except (TypeError, ValueError):
        return 0.0


def make_audio_source(guild_id, path, *, title, duration=0, requester=None, video_id=None, start_offset=0.0,
                      volume=None, clip_seconds=None, game=False):
    """
    The one place playback sources are built: applies the server's sound effect (ffmpeg filter + the speed
    it implies), the fade setting, a start position and who asked for the track.
    """
    flt, speed = ("", 1.0) if game else guild_effect(guild_id)
    opts = dict(ffmpeg_options)
    before = opts.get("before_options", "")
    if start_offset:
        before = f"{before} -ss {float(start_offset):.2f}".strip()
    out = opts["options"]
    if flt:
        out += f' -af "{flt}"'
    if clip_seconds:
        out += f" -t {clip_seconds:g}"
    opts["before_options"], opts["options"] = before, out
    ff = discord.FFmpegPCMAudio(str(path).replace("\\", "/"), executable=get_ffmpeg_path(), **opts)
    return SimpleAudioSource(ff, title=title, duration=duration, file_path=str(path), start_offset=start_offset,
                             volume=volume, speed=speed, requester=requester, video_id=video_id,
                             fade=0.0 if game else guild_fade(guild_id), game=game)


@bot.command(name="effect", aliases=["fx"], help="Sound effect: slowed, vaporwave, nightcore, reverb, bass, 8d, speed 1.25, off")
@require_authorization()
async def effect_cmd(ctx, *, spec: str = None):
    gid = ctx.guild.id
    current = guild_setting(gid, "effect", "off") or "off"
    if spec is None:
        await reply_note(ctx, vapor.note("effect", f"current: {current}",
                                         "options: " + ", ".join(extras.effect_names()) + ", speed 1.25"))
        return
    try:
        desc, live = apply_effect(gid, spec)
    except ValueError as e:
        await ctx.send(f"❌ {e}")
        return
    await reply_note(ctx, vapor.note("effect", desc, "applied to the song playing now" if live else "applies from the next song"))


@bot.command(name="fade", help="Fade songs in and out (seconds, or off)")
@require_authorization()
async def fade_cmd(ctx, seconds: str = None):
    gid = ctx.guild.id
    if seconds is None:
        cur = guild_fade(gid)
        await reply_note(ctx, vapor.note("fade", f"fade: {cur:g}s" if cur else "fade: off",
                                         "each song fades in and out; use `!fade 4` or `!fade off`"))
        return
    text = seconds.strip().lower().rstrip("s")
    if text in ("off", "0", "no", "none"):
        value = 0.0
    else:
        try:
            value = float(text)
        except ValueError:
            await ctx.send("❌ Use a number of seconds (1-12) or `off`.")
            return
        if not 0.5 <= value <= 12:
            await ctx.send("❌ Fade must be between 0.5 and 12 seconds (or `off`).")
            return
    set_guild_setting(gid, "fade", value)
    src = current_sources.get(gid)
    if src is not None and hasattr(src, "fade") and not getattr(src, "game", False):
        src.fade = value
    await reply_note(ctx, vapor.note("fade", f"fade set to {value:g}s" if value else "fade is off",
                                     "songs fade out at the end and the next fades in"))


# ---------------------------------------------------------------------------
# Sleep timer
# ---------------------------------------------------------------------------

def _sleep_cancel(guild_id):
    st = _sleep_state.pop(guild_id, None)
    if st and st.get("task") is not None:
        st["task"].cancel()
    return st is not None


async def _sleep_finish(guild_id, why):
    _sleep_state.pop(guild_id, None)
    if PlaylistManager.get_playlist_status(guild_id)["status"] == "active":
        PlaylistManager.stop_playlist(guild_id)
    clear_song_queue(guild_id)
    stop_current(guild_id)
    for src in list(current_sources.values()):
        if hasattr(src, "master"):
            src.master = 1.0
    channel = get_notification_channel(guild_id)
    if channel is not None:
        try:
            await reply_note(channel, vapor.note("sleep timer", "goodnight", f"playback stopped ({why})"))
        except Exception as e:
            print(f"⚠️ sleep note failed: {e}")
    await update_presence()


async def _sleep_timer(guild_id, seconds):
    try:
        fade = min(SLEEP_FADE_SECONDS, seconds)
        await asyncio.sleep(max(0.0, seconds - fade))
        steps = max(1, int(fade / 0.5))
        for i in range(steps):
            src = current_sources.get(guild_id)
            if src is not None and hasattr(src, "master"):
                src.master = max(0.0, 1.0 - (i + 1) / steps)
            await asyncio.sleep(0.5)
        await _sleep_finish(guild_id, "sleep timer")
    except asyncio.CancelledError:
        src = current_sources.get(guild_id)
        if src is not None and hasattr(src, "master"):
            src.master = 1.0
        raise


@bot.command(name="sleep", help="Sleep timer: !sleep 30m | 1h30m | end (after this song) | off")
@require_authorization()
async def sleep_cmd(ctx, *, spec: str = None):
    gid = ctx.guild.id
    set_notification_channel(gid, ctx.channel)
    if spec is None:
        st = _sleep_state.get(gid)
        if not st:
            await reply_note(ctx, vapor.note("sleep timer", "no timer set", "try `!sleep 30m` or `!sleep end`"))
        elif st["mode"] == "end":
            await reply_note(ctx, vapor.note("sleep timer", "stopping after this song"))
        else:
            await reply_note(ctx, vapor.note("sleep timer", f"stopping in {extras.fmt_span(st['ends'] - time.time())}"))
        return
    try:
        value = extras.parse_sleep(spec)
    except ValueError as e:
        await ctx.send(f"❌ {e}")
        return
    _sleep_cancel(gid)
    if value is None:
        await reply_note(ctx, vapor.note("sleep timer", "timer cancelled"))
    elif value == "end":
        _sleep_state[gid] = {"mode": "end", "ends": 0, "task": None}
        await reply_note(ctx, vapor.note("sleep timer", "stopping after this song", "the queue is cleared when it ends"))
    else:
        _sleep_state[gid] = {"mode": "timer", "ends": time.time() + value,
                             "task": _spawn(_sleep_timer(gid, float(value)))}
        await reply_note(ctx, vapor.note("sleep timer", f"stopping in {extras.fmt_span(value)}",
                                         f"volume fades over the last {min(SLEEP_FADE_SECONDS, value)}s"))


# ---------------------------------------------------------------------------
# Vote skip
# ---------------------------------------------------------------------------

def cast_vote(guild_id, member):
    """Count one skip vote. Returns (ok, text, passed)."""
    vc = _voice_client_for(guild_id)
    src = current_sources.get(guild_id)
    if not vc or src is None or not (vc.is_playing() or vc.is_paused()):
        return False, "Nothing playing", False
    voice = getattr(member, "voice", None)
    if not voice or voice.channel != vc.channel:
        return False, "Join my voice channel to vote", False
    listeners = {m.id for m in vc.channel.members if not m.bot}
    need = extras.votes_needed(len(listeners))
    state = _skip_votes.get(guild_id)
    if not state or state[0] != id(src):
        state = (id(src), set())
        _skip_votes[guild_id] = state
    state[1].add(member.id)
    state[1].intersection_update(listeners)   # people who left no longer count
    have = len(state[1])
    if have >= need:
        _skip_votes.pop(guild_id, None)
        skip_current(guild_id)
        return True, f"vote passed ({have}/{need}) · skipped", True
    return True, f"{have}/{need} votes to skip", False


@bot.command(name="voteskip", aliases=["vs"], help="Vote to skip the current song (majority of listeners)")
async def voteskip_cmd(ctx):
    set_notification_channel(ctx.guild.id, ctx.channel)
    ok, text, passed = cast_vote(ctx.guild.id, ctx.author)
    if not ok:
        await ctx.send(f"❌ {text}")
        return
    await reply_note(ctx, vapor.note("skipped" if passed else "voted", _safe_name(ctx.author.display_name), text))


# ---------------------------------------------------------------------------
# Interactive queue
# ---------------------------------------------------------------------------

class QueueView(discord.ui.View):
    """
    !queue as a live panel: ◀ ▶ page, 🔀 shuffle, 🧹 clear · ⏫ move to top, ⏭ play now, 🗑 remove, 🔄 refresh,
    and a dropdown to choose the track those act on. Browsing is open to everyone; changing needs authorization.
    """
    _guard = NowPlayingView._guard

    def __init__(self, guild_id, page=0):
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self.page = page
        self.selected = None      # absolute (0-based) index in the queue
        self.message = None
        self.header = BadgeHeader()
        self._sel = None
        self._build()

    def _queue(self):
        return song_queues.get(self.guild_id) or []

    def _pages(self):
        return max(1, math.ceil(len(self._queue()) / QUEUE_PAGE))

    def _build(self):
        self.clear_items()
        q = self._queue()
        self.page = max(0, min(self.page, self._pages() - 1))
        if self.selected is not None and self.selected >= len(q):
            self.selected = None
        sec, pri, dan = discord.ButtonStyle.secondary, discord.ButtonStyle.primary, discord.ButtonStyle.danger

        def add(emoji, cb, row, style=sec, disabled=False):
            b = discord.ui.Button(emoji=emoji, style=style, row=row, disabled=disabled)
            b.callback = cb
            self.add_item(b)

        has = self.selected is not None
        add("◀️", self._prev, 0, disabled=self.page <= 0)
        add("▶️", self._next, 0, disabled=self.page >= self._pages() - 1)
        add("🔀", self._shuffle, 0, disabled=len(q) < 2)
        add("🧹", self._clear, 0, dan, disabled=not q)
        add("⏫", self._to_top, 1, disabled=not has or self.selected == 0)
        add("⏭️", self._play_now, 1, pri, disabled=not has)
        add("🗑️", self._remove, 1, disabled=not has)
        add("🔄", self._refresh, 1)
        self._sel = None
        if q:
            start = self.page * QUEUE_PAGE
            options = [discord.SelectOption(label=f"{i + 1}. {s['title']}"[:100], value=str(i),
                                            default=(i == self.selected))
                       for i, s in enumerate(q[start:start + QUEUE_PAGE], start)]
            self._sel = discord.ui.Select(placeholder="pick a track to move, play or remove…", options=options, row=2)
            self._sel.callback = self._pick
            self.add_item(self._sel)

    def render(self):
        q = self._queue()
        start = self.page * QUEUE_PAGE
        rows = [(start + i + 1, s["title"], s.get("duration") or 0) for i, s in enumerate(q[start:start + QUEUE_PAGE])]
        now = getattr(current_sources.get(self.guild_id), "title", None)
        pl = PlaylistManager.get_playlist_status(self.guild_id)
        playlist = pl.get("playlist_name") if pl.get("status") == "active" else None
        total = sum(int(s.get("duration") or 0) for s in q)
        return vapor.queue_card(now, rows, self.page, self._pages(), len(q), total,
                                selected=None if self.selected is None else self.selected + 1, playlist=playlist)

    async def _redraw(self, interaction):
        self._build()
        body = await self.header.sync(interaction.channel, self.render())
        empty = not self._queue()
        await interaction.response.edit_message(content=body, view=None if empty else self)
        if empty:
            self.stop()

    async def _pick(self, interaction):
        if not await self._guard(interaction, need_auth=False):
            return
        self.selected = int(self._sel.values[0])
        await self._redraw(interaction)

    async def _prev(self, interaction):
        if not await self._guard(interaction, need_auth=False):
            return
        self.page, self.selected = self.page - 1, None
        await self._redraw(interaction)

    async def _next(self, interaction):
        if not await self._guard(interaction, need_auth=False):
            return
        self.page, self.selected = self.page + 1, None
        await self._redraw(interaction)

    async def _refresh(self, interaction):
        if not await self._guard(interaction, need_auth=False):
            return
        await self._redraw(interaction)

    async def _to_top(self, interaction):
        if not await self._guard(interaction):
            return
        q, i = self._queue(), self.selected
        if i is not None and 0 <= i < len(q):
            q.insert(0, q.pop(i))
            self.selected, self.page = 0, 0
        await self._redraw(interaction)

    async def _play_now(self, interaction):
        if not await self._guard(interaction):
            return
        q, i = self._queue(), self.selected
        if i is None or not (0 <= i < len(q)):
            await self._redraw(interaction)
            return
        q.insert(0, q.pop(i))
        self.selected, self.page = None, 0
        set_notification_channel(self.guild_id, interaction.channel)
        vc = _voice_client_for(self.guild_id)
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()      # after_playing plays the head of the queue (it comes before any playlist)
        elif vc and vc.is_connected():
            _spawn(play_next_queued_song(self.guild_id))
        await asyncio.sleep(0.7)
        await self._redraw(interaction)

    async def _remove(self, interaction):
        if not await self._guard(interaction):
            return
        q, i = self._queue(), self.selected
        if i is not None and 0 <= i < len(q):
            item = q.pop(i)
            path = item.get("file_path")
            if path and not str(path).startswith("playlists"):
                queue_cleanup_request(path, keep_recent=False)
        self.selected = None
        await self._redraw(interaction)

    async def _shuffle(self, interaction):
        if not await self._guard(interaction):
            return
        random.shuffle(self._queue())
        self.selected, self.page = None, 0
        await self._redraw(interaction)

    async def _clear(self, interaction):
        if not await self._guard(interaction):
            return
        clear_song_queue(self.guild_id)
        self.selected = None
        await self._redraw(interaction)

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


@bot.command(name="queue", aliases=["q"], help="Show the queue (interactive: move, play now, remove)")
async def show_queue(ctx):
    view = QueueView(ctx.guild.id)
    body = await view.header.sync(ctx, view.render())
    view.message = await ctx.send(content=body, view=view if view._queue() else None)
    if not view._queue():
        view.stop()


# ---------------------------------------------------------------------------
# Favourites, history and "pick one to queue" menus
# ---------------------------------------------------------------------------

class PickView(discord.ui.View):
    """A dropdown of remembered tracks (favourites / history); picking one queues it like `!play`."""

    def __init__(self, guild_id, items, owner_id=None):
        super().__init__(timeout=300)
        self.guild_id, self.items, self.owner_id = guild_id, list(items), owner_id
        self.message = None
        self._sel = None
        options = [discord.SelectOption(label=f"{i}. {it['title']}"[:100], value=str(i - 1))
                   for i, it in enumerate(self.items[:25], 1)]
        if options:
            self._sel = discord.ui.Select(placeholder="pick one to queue…", options=options)
            self._sel.callback = self._pick
            self.add_item(self._sel)

    async def _pick(self, interaction):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("❌ This menu belongs to another server.", ephemeral=True)
            return
        if self.owner_id and interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ That's someone else's list. Use `!likes` for yours.",
                                                    ephemeral=True, delete_after=6)
            return
        if not is_authorized(interaction.user.id):
            await interaction.response.send_message("❌ **Access denied.** Queueing needs authorization.",
                                                    ephemeral=True, delete_after=6)
            return
        item = self.items[int(self._sel.values[0])]
        await interaction.response.defer()
        set_notification_channel(self.guild_id, interaction.channel)
        await queue_track(PseudoCtx(interaction.guild, interaction.channel, interaction.user), item)

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


async def _send_list(ctx, label, title, items, owner_id=None):
    text = vapor.list_card(label, title, [(i, it["title"]) for i, it in enumerate(items[:25], 1)])
    header = BadgeHeader()
    body = await header.sync(ctx, text)
    view = PickView(ctx.guild.id, items, owner_id)
    msg = await ctx.send(content=body, view=view if items else None)
    view.message = msg
    return msg


@bot.command(name="like", aliases=["fav"], help="Add the song that's playing to your favourites")
async def like_cmd(ctx):
    src = current_sources.get(ctx.guild.id)
    title = getattr(src, "title", None)
    if not title or getattr(src, "game", False):
        await ctx.send("❌ Nothing playing to like.")
        return
    added = extras.add_like(DATA, ctx.guild.id, ctx.author.id, title, getattr(src, "video_id", None))
    await reply_note(ctx, vapor.note("liked", title, "added to your favourites" if added else "already in your favourites"))


@bot.command(name="unlike", help="Remove a favourite by its number (see !likes)")
async def unlike_cmd(ctx, number: int):
    item = extras.remove_like(DATA, ctx.guild.id, ctx.author.id, number)
    if item is None:
        await ctx.send("❌ No favourite with that number. See `!likes`.")
        return
    await reply_note(ctx, vapor.note("liked", item["title"], "removed from your favourites"))


@bot.command(name="likes", aliases=["favs", "favorites"], help="Your favourites: !likes | !likes play [shuffle] | !likes remove N")
async def likes_cmd(ctx, action: str = None, *, rest: str = None):
    # Favourites are per Discord server — same user in another server has a separate list.
    likes = extras.get_likes(DATA, ctx.guild.id, ctx.author.id)
    act = (action or "").lower()
    if act in ("remove", "rm", "delete"):
        try:
            item = extras.remove_like(DATA, ctx.guild.id, ctx.author.id, int((rest or "").strip()))
        except ValueError:
            item = None
        await ctx.send("❌ Usage: `!likes remove <number>`" if item is None else f"🗑️ Removed **{item['title']}**")
        return
    if act == "play":
        if not member_can_control(ctx.author):
            await ctx.send("❌ **Access denied.** Playing favourites needs DJ / admin / openplay.")
            return
        if not likes:
            await ctx.send("❌ You have no favourites yet. Use `!like` while a song plays.")
            return
        await play_favourites(ctx, likes, shuffle="shuffle" in (rest or "").lower())
        return
    await _send_list(ctx, "liked", f"{_safe_name(ctx.author.display_name)}'s favourites",
                     list(reversed(likes))[:25], owner_id=ctx.author.id)


@bot.command(name="history", help="Recently played songs (pick one to queue again)")
async def history_cmd(ctx):
    plays = extras.recent_plays(DATA, ctx.guild.id, 15)
    await _send_list(ctx, "history", "recently played", [{"title": p["title"], "vid": p.get("vid")} for p in plays])


# ---------------------------------------------------------------------------
# Stats and the weekly recap
# ---------------------------------------------------------------------------

_PERIODS = {"day": (86400, "last 24 hours"), "week": (7 * 86400, "last 7 days"),
            "month": (30 * 86400, "last 30 days"), "all": (0, "all time")}


@bot.command(name="stats", help="Listening stats: !stats [day|week|month|all] | !stats recap on|off")
async def stats_cmd(ctx, period: str = "week", setting: str = None):
    gid = ctx.guild.id
    period = (period or "week").lower()
    if period == "recap":
        if not is_authorized(ctx.author.id):
            await ctx.send("❌ **Access denied.** Changing the recap needs authorization.")
            return
        on = (setting or "").lower() not in ("off", "0", "no", "false")
        set_guild_setting(gid, "recap", on)
        set_notification_channel(gid, ctx.channel)
        await reply_note(ctx, vapor.note("stats", "weekly recap " + ("on" if on else "off"),
                                         "posted here once a week" if on else ""))
        return
    if period not in _PERIODS:
        await ctx.send("❌ Use `!stats day`, `week`, `month` or `all`.")
        return
    span, label = _PERIODS[period]
    s = extras.summarize(DATA, gid, since_ts=(time.time() - span) if span else 0)
    await reply_note(ctx, vapor.stats_card(label, s["tracks"], s["seconds"], s["top_songs"], s["top_people"]),
                     lifetime=NOTE_SECONDS * 5)


async def _recap_tick():
    now = time.time()
    for guild in list(bot.guilds):
        if not guild_setting(guild.id, "recap", True):
            continue
        last = extras.last_recap(DATA, guild.id)
        if not last:
            extras.mark_recap(DATA, guild.id, now)     # start the clock; nothing to report yet
            continue
        if now - last < RECAP_EVERY:
            continue
        s = extras.summarize(DATA, guild.id, since_ts=last)
        extras.mark_recap(DATA, guild.id, now)
        if s["tracks"] < 3:
            continue
        channel = get_notification_channel(guild.id)
        if channel is None and guild_setting(guild.id, "notify_channel"):
            channel = guild.get_channel(int(guild_setting(guild.id, "notify_channel")))
        if channel is None:
            continue
        try:
            await reply_note(channel, vapor.stats_card("this week", s["tracks"], s["seconds"],
                                                       s["top_songs"], s["top_people"]), lifetime=0)
        except Exception as e:
            print(f"⚠️ weekly recap failed for {guild.name}: {e}")


async def _recap_loop():
    await asyncio.sleep(30)
    while True:
        try:
            await _recap_tick()
        except Exception as e:
            print(f"⚠️ recap loop error: {e}")
        await asyncio.sleep(3600)


_background_started = False


async def _start_background_tasks():
    global _background_started
    if _background_started:
        return
    _background_started = True
    if RECAP_ENABLED:
        _spawn(_recap_loop())
    _spawn(_session_loop())


bot.add_listener(_start_background_tasks, "on_ready")


# ---------------------------------------------------------------------------
# Lyrics
# ---------------------------------------------------------------------------

def lyrics_enabled(guild_id):
    return LYRICS_ENABLED and bool(guild_setting(guild_id, "lyrics", False))


async def fetch_lyrics(title, duration=0):
    """{'lines': [(t, text)], 'plain': str, ...} from the processor (cached), or None."""
    key = extras.lyric_query(title).lower()
    if key in _lyrics_cache:
        _lyrics_cache.move_to_end(key)
        return _lyrics_cache[key] or None
    try:
        res = await wait_for_request(queue_info_request("lyrics", title=title, duration=duration or 0))
    except Exception as e:
        print(f"⚠️ lyrics request failed: {e}")
        return None
    if res.get("status") != "success":
        if "no lyrics" in str(res.get("message", "")):
            _lyrics_cache[key] = {}      # remember the miss
        return None
    synced = res.get("synced") or ""
    entry = {"lines": extras.parse_lrc(synced) if synced else [],
             "plain": res.get("plain") or re.sub(r"\[[^\]]*\]\s*", "", synced),
             "track": res.get("track"), "artist": res.get("artist")}
    _lyrics_cache[key] = entry
    while len(_lyrics_cache) > 40:
        _lyrics_cache.popitem(last=False)
    return entry


@bot.command(name="lyrics", help="Live lyrics under the bar: !lyrics on | off | full")
async def lyrics_cmd(ctx, mode: str = None):
    gid = ctx.guild.id
    mode = (mode or "").lower()
    if mode == "full":
        src = current_sources.get(gid)
        title = getattr(src, "title", None)
        if not title:
            await ctx.send("❌ Nothing playing.")
            return
        entry = await fetch_lyrics(title, getattr(src, "duration", 0))
        if not entry or not entry.get("plain"):
            await ctx.send("❌ No lyrics found for this track.")
            return
        await reply_note(ctx, vapor.note("lyrics", title, "full text below"), lifetime=0)
        text = entry["plain"].strip()
        for i in range(0, min(len(text), 5700), 1900):
            await ctx.send(text[i:i + 1900])
        return
    if not is_authorized(ctx.author.id):
        await ctx.send("❌ **Access denied.** Turning live lyrics on or off needs authorization (`!lyrics full` is open).")
        return
    if not LYRICS_ENABLED:
        await ctx.send("❌ Lyrics are disabled (ARKEN_LYRICS=0).")
        return
    on = (not lyrics_enabled(gid)) if mode in ("", "toggle") else mode in ("on", "1", "yes", "true")
    set_guild_setting(gid, "lyrics", on)
    tracker = _np_trackers.get(gid)
    if tracker is not None:
        tracker.force = True
    await reply_note(ctx, vapor.note("lyrics", "live lyrics " + ("on" if on else "off"),
                                     "the current line shows under the bar" if on else ""))


# ---------------------------------------------------------------------------
# Autoplay radio
# ---------------------------------------------------------------------------

def autoplay_enabled(guild_id):
    return bool(guild_setting(guild_id, "autoplay", False))


async def autoplay_next(guild_id, last):
    """The queue ran dry: ask the processor for tracks like the last one and play the first fresh one."""
    if guild_id in _autoplay_busy:
        return
    _autoplay_busy.add(guild_id)
    try:
        guild = bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        channel = get_notification_channel(guild_id)
        if not vc or not vc.is_connected() or channel is None:
            return
        if vc.is_playing() or vc.is_paused() or song_queues.get(guild_id) or guild_id in _games:
            return
        if not [m for m in vc.channel.members if not m.bot]:
            return      # nobody is listening: don't play to an empty room
        n = _autoplay_count.get(guild_id, 0)
        if n >= AUTOPLAY_MAX:
            await reply_note(channel, vapor.note("autoplay", f"paused after {n} songs in a row",
                                                 "ask for something with `!play` to keep going"))
            return
        res = await wait_for_request(queue_info_request(
            "related", video_id=getattr(last, "video_id", None), title=getattr(last, "title", ""), max_items=15))
        if res.get("status") != "success":
            await reply_note(channel, vapor.note("autoplay", "couldn't find a follow-up song", str(res.get("message", ""))))
            return
        recent = extras.recent_video_ids(DATA, guild_id, 40)
        pick = extras.pick_related(res.get("entries"), recent, getattr(last, "video_id", None))
        if pick is None:
            await reply_note(channel, vapor.note("autoplay", "nothing new to suggest"))
            return
        if vc.is_playing() or vc.is_paused() or song_queues.get(guild_id):
            return      # somebody started something while we were looking
        _autoplay_count[guild_id] = n + 1
        author = SimpleNamespace(id=getattr(bot.user, "id", 0), name="autoplay", display_name="autoplay", mention="autoplay", voice=None)
        await _play_impl()(PseudoCtx(guild, channel, author, autoplay=True), query=pick["url"])
    except Exception as e:
        print(f"⚠️ autoplay failed: {e}")
    finally:
        _autoplay_busy.discard(guild_id)


@bot.command(name="autoplay", aliases=["radio"], help="Keep similar songs coming when the queue runs out: on | off")
@require_authorization()
async def autoplay_cmd(ctx, mode: str = None):
    gid = ctx.guild.id
    mode = (mode or "").lower()
    if mode == "":
        await reply_note(ctx, vapor.note("autoplay", "autoplay is " + ("on" if autoplay_enabled(gid) else "off"),
                                         "`!autoplay on` / `!autoplay off`"))
        return
    on = mode in ("on", "1", "yes", "true")
    set_guild_setting(gid, "autoplay", on)
    set_notification_channel(gid, ctx.channel)
    _autoplay_count.pop(gid, None)
    await reply_note(ctx, vapor.note("autoplay", "autoplay " + ("on" if on else "off"),
                                     "when the queue runs out I'll queue similar songs" if on else ""))


# ---------------------------------------------------------------------------
# Guess the song
# ---------------------------------------------------------------------------

class GuessGame:
    def __init__(self, guild, channel, tracks):
        self.guild, self.channel = guild, channel
        self.tracks = tracks
        self.rounds = len(tracks)
        self.scores = {}          # user id -> [name, points]
        self.titles = None        # acceptable answers for the round being played (None between rounds)
        self.winner = None
        self.answered = asyncio.Event()
        self.skip = asyncio.Event()
        self.stopping = False
        self.task = None
        self.prefetch = {}        # round index -> processor request id


def _scoreboard(game):
    ranking = sorted(game.scores.values(), key=lambda v: -v[1])
    return [f"{i}. {name} · {pts}" for i, (name, pts) in enumerate(ranking[:6], 1)] or ["nobody scored"]


async def _wait_first(a, b, timeout):
    tasks = [asyncio.ensure_future(a.wait()), asyncio.ensure_future(b.wait())]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


async def _guess_listener(message):
    if message.author.bot or not message.guild or not message.content:
        return
    game = _games.get(message.guild.id)
    if game is None or game.titles is None or message.channel.id != game.channel.id:
        return
    if message.content.startswith("!") or game.winner is not None:
        return
    if any(extras.guess_matches(message.content, t) for t in game.titles):
        game.winner = message.author
        game.answered.set()


bot.add_listener(_guess_listener, "on_message")


async def _run_game(game):
    gid = game.guild.id

    def prefetch(i):
        if i < game.rounds and i not in game.prefetch:
            game.prefetch[i] = queue_audio_request(game.tracks[i]["url"])

    try:
        prefetch(0)
        await reply_note(game.channel, vapor.note(
            "guess", f"{game.rounds} rounds · {GUESS_CLIP_SECONDS}s clips",
            "type the song or artist in chat", "first correct answer takes the point"), lifetime=15)
        await asyncio.sleep(2)
        for i in range(game.rounds):
            if game.stopping:
                break
            prefetch(i + 1)
            track = game.tracks[i]
            res = await wait_for_request(game.prefetch.pop(i))
            if game.stopping:
                break
            path = res.get("file_path")
            if res.get("status") != "success" or not path or not os.path.exists(path):
                await game.channel.send(f"⚠️ Round {i + 1} couldn't load, skipping it.")
                continue
            vc = game.guild.voice_client
            if not vc or not vc.is_connected():
                break
            title = res.get("title") or track["title"]
            duration = int(res.get("duration") or 0)
            start = random.uniform(0.15, 0.55) * duration if duration >= 75 else 0.0
            game.winner = None
            game.answered.clear()
            game.skip.clear()
            src = make_audio_source(gid, path, title=title, duration=duration, start_offset=start,
                                    clip_seconds=GUESS_CLIP_SECONDS, game=True)
            apply_saved_volume(gid, src)
            current_sources[gid] = src
            game.titles = [title, track["title"]]
            vc.play(src, after=lambda e: after_playing(e, gid))
            await reply_note(game.channel, vapor.note("guess", f"round {i + 1}/{game.rounds}",
                                                      "listen…  what song is this?"), lifetime=GUESS_ROUND_SECONDS)
            await _wait_first(game.answered, game.skip, GUESS_ROUND_SECONDS)
            game.titles = None
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            if game.stopping:
                break
            if game.winner is not None:
                entry = game.scores.setdefault(game.winner.id, [_safe_name(game.winner.display_name), 0])
                entry[1] += 1
                await reply_note(game.channel, vapor.note("winner", f"{entry[0]} got it! (+1)", title,
                                                          f"{entry[0]}: {entry[1]} point" + ("" if entry[1] == 1 else "s")),
                                 lifetime=20)
            else:
                await reply_note(game.channel, vapor.note("guess", "nobody got it", f"it was: {title}"), lifetime=20)
            await asyncio.sleep(4)
        await reply_note(game.channel, vapor.note("winner", "game over", *_scoreboard(game)), lifetime=60)
    except Exception as e:
        print(f"⚠️ guess game error: {e}")
        try:
            await game.channel.send(f"❌ The guessing game hit an error: {e}")
        except Exception:
            pass
    finally:
        game.titles = None
        _games.pop(gid, None)
        vc = game.guild.voice_client
        src = current_sources.get(gid)
        if vc and src is not None and getattr(src, "game", False) and (vc.is_playing() or vc.is_paused()):
            vc.stop()


def guess_pool(guild_id):
    """Songs this server has played plus favourites saved in THIS server only."""
    seen, pool = set(), []

    def add(title, vid):
        key = vid or title
        if not title or key in seen:
            return
        seen.add(key)
        pool.append({"title": title, "vid": vid, "url": _youtube_ref({"title": title, "vid": vid})})

    with DATA.lock:
        for p in DATA.guild(guild_id).get("plays", []):
            add(p.get("title"), p.get("vid"))
        for like in tenancy.guild_likes_pool(DATA.data, guild_id):
            add(like.get("title"), like.get("vid"))
    return pool


@bot.command(name="guess", help="Guess-the-song game: !guess [rounds] [playlist url] | stop | skip")
@require_authorization()
async def guess_cmd(ctx, *args):
    gid = ctx.guild.id
    game = _games.get(gid)
    word = args[0].lower() if args else ""
    if word in ("stop", "end", "quit", "skip"):
        if game is None:
            await ctx.send("❌ No guessing game is running.")
            return
        if word == "skip":
            game.skip.set()
            return
        game.stopping = True
        game.skip.set()
        game.answered.set()
        await reply_note(ctx, vapor.note("guess", "game stopped"))
        return
    if game is not None:
        await ctx.send("❌ A guessing game is already running. `!guess stop` ends it.")
        return
    vc = ctx.guild.voice_client
    if not vc or not _voice_session_usable(vc):
        await ctx.send("❌ I'm not connected to a voice channel! Use `!summon` first.")
        return
    if vc.is_playing() or vc.is_paused() or song_queues.get(gid) or PlaylistManager.get_playlist_status(gid)["status"] == "active":
        await ctx.send("❌ Stop the music first (`!stop`), then start the game.")
        return
    rounds, url = 5, None
    for a in args:
        if a.isdigit():
            rounds = max(1, min(15, int(a)))
        elif a.lower().startswith(("http://", "https://")):
            url = a
    set_notification_channel(gid, ctx.channel)
    if url:
        info = await wait_for_request(queue_playlist_info_request(url, 60))
        if info.get("status") != "success":
            await ctx.send(f"❌ {info.get('message', 'Could not read that playlist')}")
            return
        pool = [{"title": e["title"], "vid": e["id"], "url": e["url"]} for e in info.get("entries", [])]
    else:
        pool = guess_pool(gid)
    if len(pool) < 3:
        await ctx.send("❌ I don't know enough songs yet. Play a few, `!like` some, or use `!guess <playlist url>`.")
        return
    random.shuffle(pool)
    game = GuessGame(ctx.guild, ctx.channel, pool[:rounds])
    _games[gid] = game
    game.task = _spawn(_run_game(game))


# ============================================================================
# 🏠 DASHBOARD + SESSION MEMORY
#    While music plays the bot keeps a small snapshot of the session (what's playing and where, the queue,
#    the rest of a YouTube playlist) as song REFERENCES, not files: tracks are deleted after play, so resuming
#    simply fetches them again. The dashboard posted after !summon (and by !dashboard) can resume that session,
#    play favourites, replay recent songs, quick-play from a pop-up, start a radio, change effects/toggles
#    and show this week's stats.
# ============================================================================

DASHBOARD_ON_SUMMON = aenv.flag("DASHBOARD")      # post the dashboard automatically after !summon
SESSION_MAX_AGE = 14 * 86400                              # a saved session older than this is ignored
SESSION_SAVE_EVERY = 30                                   # seconds between disk writes while nothing changes
FAV_PLAYLIST_URL = "https://www.youtube.com/playlist?list=arken-favourites"      # placeholders: the entries are
SESSION_PLAYLIST_URL = "https://www.youtube.com/playlist?list=arken-last-session"  # supplied, nothing is fetched

_session_saved_at = {}
_session_dirty = set()
_dashboards = {}          # guild_id -> latest DashboardView


def _session_entry(title, vid, duration):
    return {"title": str(title or "Unknown")[:200], "vid": vid, "duration": int(duration or 0)}


def build_session_snapshot(guild_id):
    """What is playing right now plus everything waiting behind it, or None if nothing worth saving."""
    vc = _voice_client_for(guild_id)
    src = current_sources.get(guild_id)
    if not vc or src is None or getattr(src, "game", False):
        return None
    if not (vc.is_playing() or vc.is_paused()) or not getattr(src, "title", None):
        return None
    now = _session_entry(src.title, getattr(src, "video_id", None), getattr(src, "duration", 0))
    now["pos"] = round(float(getattr(src, "elapsed", 0.0)), 1)
    queue = [_session_entry(s.get("title"), s.get("video_id"), s.get("duration"))
             for s in (song_queues.get(guild_id) or [])[:60]]
    playlist = None
    pl = playlist_queues.get(guild_id)
    if pl and pl.get("stream"):       # YouTube imports only: saved on-disk playlists are files already
        songs = pl.get("songs") or []
        idx = int(pl.get("current_index") or 0)
        playlist = {"name": pl.get("playlist_name") or "playlist",
                    "songs": [_session_entry(s.get("title"), s.get("video_id"), s.get("duration"))
                              for s in songs[idx:idx + 150]]}
    return {"t": int(time.time()), "now": now, "queue": queue, "playlist": playlist}


def snapshot_session(guild_id, save=True):
    """Remember the session. Writes to disk when something changed (or every SESSION_SAVE_EVERY seconds)."""
    snap = build_session_snapshot(guild_id)
    if snap is None:
        return False
    with DATA.lock:
        guild = DATA.guild(guild_id)
        old = guild.get("session")
        changed = (not old or old["now"].get("title") != snap["now"]["title"]
                   or len(old.get("queue") or []) != len(snap["queue"]))
        guild["session"] = snap
        due = changed or time.time() - _session_saved_at.get(guild_id, 0) > SESSION_SAVE_EVERY
        if due:
            _session_saved_at[guild_id] = time.time()
            if save:
                DATA.save()
            else:
                _session_dirty.add(guild_id)
    return True


def session_clear(guild_id):
    """Everything finished on its own: nothing left to resume."""
    with DATA.lock:
        if DATA.guild(guild_id).pop("session", None) is not None:
            DATA.save()


def get_session(guild_id):
    s = DATA.guild(guild_id).get("session")
    if not s or not s.get("now") or time.time() - s.get("t", 0) > SESSION_MAX_AGE:
        return None
    return s


def session_entries(session):
    """The saved session as playlist-import entries: current song first, then the queue, then the playlist rest."""
    items = [session["now"]] + list(session.get("queue") or []) + list((session.get("playlist") or {}).get("songs") or [])
    entries, seen = [], set()
    for i, it in enumerate(items):
        key = it.get("vid") or it.get("title")
        if not it.get("title") or key in seen:
            continue
        seen.add(key)
        entries.append({"id": it.get("vid") or f"res{i}", "title": it["title"], "duration": int(it.get("duration") or 0),
                        "url": _youtube_ref(it)})
    return entries


async def _apply_resume_seek(guild_id, title, vid, pos, tries=120):
    """After a resume starts the interrupted song, jump back to where it was."""
    for _ in range(tries):
        await asyncio.sleep(1)
        src = current_sources.get(guild_id)
        if src is not None and not getattr(src, "game", False) and getattr(src, "elapsed", 99) < 6 and \
                (getattr(src, "title", None) == title or (vid and getattr(src, "video_id", None) == vid)):
            seek_current(guild_id, to=pos)
            return True
    return False


async def resume_session(pctx):
    """Re-queue the last session. Returns (ok, message)."""
    gid = pctx.guild.id
    session = get_session(gid)
    if session is None:
        return False, "No previous session saved yet."
    vc = pctx.guild.voice_client
    if not vc or not _voice_session_usable(vc):
        return False, "I'm not connected to a voice channel. Use `!summon` first."
    entries = session_entries(session)
    if not entries:
        return False, "The saved session is empty."
    now = session["now"]
    info = {"status": "success", "title": (session.get("playlist") or {}).get("name") or "last session",
            "entries": entries, "total": len(entries)}
    await _begin_playlist_import(pctx, SESSION_PLAYLIST_URL, preloaded_info=info)
    pos, dur = float(now.get("pos") or 0), float(now.get("duration") or 0)
    if pos >= 20 and (not dur or pos < dur - 15):
        _spawn(_apply_resume_seek(gid, now["title"], now.get("vid"), pos))
    return True, f"{len(entries)} song" + ("" if len(entries) == 1 else "s")


async def play_favourites(ctx, likes, shuffle=False):
    entries = [{"id": it.get("vid") or f"fav{i}", "title": it["title"], "duration": 0, "url": _youtube_ref(it)}
               for i, it in enumerate(likes[-100:])]
    info = {"status": "success", "title": f"{_safe_name(ctx.author.display_name)}'s favourites",
            "entries": entries, "total": len(entries)}
    await _begin_playlist_import(ctx, FAV_PLAYLIST_URL, shuffle=shuffle, preloaded_info=info)


def apply_effect(guild_id, spec):
    """Set the server's effect and apply it to the song playing now. Returns (description, applied_live)."""
    key, _flt, _speed, desc = extras.resolve_effect(spec)       # ValueError for unknown effects
    set_guild_setting(guild_id, "effect", key)
    live = False
    src = current_sources.get(guild_id)
    vc = _voice_client_for(guild_id)
    if src is not None and vc is not None and (vc.is_playing() or vc.is_paused()) and not getattr(src, "game", False):
        live, _info = seek_current(guild_id, to=src.elapsed)    # restart ffmpeg at the same spot with the new filter
    return desc, live


def _radio_seed(guild_id):
    src = current_sources.get(guild_id)
    if src is not None and getattr(src, "title", None) and not getattr(src, "game", False):
        return SimpleNamespace(video_id=getattr(src, "video_id", None), title=src.title)
    plays = extras.recent_plays(DATA, guild_id, 1)
    if plays:
        return SimpleNamespace(video_id=plays[0].get("vid"), title=plays[0]["title"])
    s = get_session(guild_id)
    if s:
        return SimpleNamespace(video_id=s["now"].get("vid"), title=s["now"]["title"])
    return None


class QuickPlayModal(discord.ui.Modal, title="Quick play"):
    query = discord.ui.TextInput(label="Song name, link or playlist URL", placeholder="e.g. daft punk get lucky",
                                 max_length=200)

    def __init__(self, dash):
        super().__init__()
        self.dash = dash

    async def on_submit(self, interaction):
        if not is_authorized(interaction.user.id):
            await interaction.response.send_message("❌ **Access denied.** Playing needs authorization.",
                                                    ephemeral=True, delete_after=6)
            return
        text = str(self.query.value or "").strip()
        if not text:
            await interaction.response.send_message("❌ Type a song name or paste a link.", ephemeral=True, delete_after=6)
            return
        await interaction.response.defer()
        set_notification_channel(self.dash.guild_id, interaction.channel)
        await _play_impl()(PseudoCtx(interaction.guild, interaction.channel, interaction.user), query=text)
        await self.dash.redraw()


class DashboardView(discord.ui.View):
    """
    Home panel: resume last session · favourites (+shuffle) · radio · quick play · stats, a "recently played"
    dropdown, an effect dropdown and autoplay / lyrics / fade toggles. Viewing is open; actions need authorization.
    """
    _guard = NowPlayingView._guard

    def __init__(self, guild_id):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.message = None
        self.header = BadgeHeader()
        self._recent = []
        self._effect_sel = self._recent_sel = None
        self._build()

    # ---- layout ---------------------------------------------------------
    def _playing(self):
        vc = _voice_client_for(self.guild_id)
        return bool(vc and (vc.is_playing() or vc.is_paused()))

    def _build(self):
        self.clear_items()
        gid = self.guild_id
        sec, pri = discord.ButtonStyle.secondary, discord.ButtonStyle.primary
        ok_style = discord.ButtonStyle.success

        def add(label, emoji, cb, row, style=sec, disabled=False):
            b = discord.ui.Button(label=label, emoji=emoji, style=style, row=row, disabled=disabled)
            b.callback = cb
            self.add_item(b)
            return b

        add("Resume", "⏮️", self._resume, 0, pri, disabled=get_session(gid) is None or self._playing())
        add("Favourites", "❤️", self._favs, 0)
        add("Radio", "📻", self._radio, 0)
        add("Shuffle favs", "🔀", self._shuffle_favs, 1)
        add("Quick play", "🔎", self._quick, 1)
        add("Stats", "📊", self._stats, 1)
        plays = extras.recent_plays(DATA, gid, 10)
        self._recent = [{"title": p["title"], "vid": p.get("vid")} for p in plays]
        self._recent_sel = None
        if self._recent:
            self._recent_sel = discord.ui.Select(
                placeholder="🕘 Recently played: pick one to queue…", row=2,
                options=[discord.SelectOption(label=f"{i}. {it['title']}"[:100], value=str(i - 1))
                         for i, it in enumerate(self._recent, 1)])
            self._recent_sel.callback = self._pick_recent
            self.add_item(self._recent_sel)
        current = (guild_setting(gid, "effect", "off") or "off").replace("speed:", "speed ")
        names = list(extras.effect_names()) + ["speed 1.25", "speed 1.5"]
        self._effect_sel = discord.ui.Select(
            placeholder=f"🎛 Effect: {current}", row=3,
            options=[discord.SelectOption(label=n, value=n, default=(n == current),
                                          description=(extras.EFFECTS[n][2] if n in extras.EFFECTS else "playback speed")[:100])
                     for n in names])
        self._effect_sel.callback = self._pick_effect
        self.add_item(self._effect_sel)
        add("Autoplay", "🔁", self._toggle_autoplay, 4, ok_style if autoplay_enabled(gid) else sec)
        add("Lyrics", "🎤", self._toggle_lyrics, 4, ok_style if lyrics_enabled(gid) else sec, disabled=not LYRICS_ENABLED)
        add("Fade", "🌊", self._toggle_fade, 4, ok_style if guild_fade(gid) else sec)

    def render(self):
        gid = self.guild_id
        lines = []
        s = get_session(gid)
        if s:
            more = len(s.get("queue") or []) + len((s.get("playlist") or {}).get("songs") or [])
            lines.append(f"last session: {s['now']['title']}" + (f" (+{more} more)" if more else ""))
            lines.append(f"saved {extras.fmt_span(time.time() - s['t'])} ago")
        else:
            lines.append("last session: nothing saved yet")
        fx = (guild_setting(gid, "effect", "off") or "off").replace("speed:", "x")
        fade = guild_fade(gid)
        lines.append(f"fx {fx} · fade {f'{fade:g}s' if fade else 'off'} · autoplay {'on' if autoplay_enabled(gid) else 'off'}")
        lines.append(f"lyrics {'on' if lyrics_enabled(gid) else 'off'} · volume {int(round(get_volume(gid) * 100))}%")
        week = extras.summarize(DATA, gid, since_ts=time.time() - 7 * 86400)
        lines.append(f"this week: {week['tracks']} tracks" + (f" · {vapor.fmt_long_duration(week['seconds'])}" if week["seconds"] else ""))
        return vapor.note("dashboard", *lines)

    async def redraw(self):
        if self.message is None:
            return
        self._build()
        try:
            body = await self.header.sync(self.message.channel, self.render())
            await self.message.edit(content=body, view=self)
        except discord.HTTPException:
            pass

    async def retire(self):
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        await self.retire()

    # ---- helpers --------------------------------------------------------
    def _pctx(self, interaction):
        set_notification_channel(self.guild_id, interaction.channel)
        return PseudoCtx(interaction.guild, interaction.channel, interaction.user)

    async def _say(self, interaction, text):
        await interaction.response.send_message(f"❌ {text}", ephemeral=True, delete_after=8)

    # ---- buttons --------------------------------------------------------
    async def _resume(self, interaction):
        if not await self._guard(interaction):
            return
        if get_session(self.guild_id) is None:
            return await self._say(interaction, "No previous session saved yet.")
        await interaction.response.defer()
        ok, text = await resume_session(self._pctx(interaction))
        if not ok:
            await interaction.channel.send(f"❌ {text}")
        await self.redraw()

    async def _play_favs(self, interaction, shuffle):
        if not await self._guard(interaction):
            return
        likes = extras.get_likes(DATA, self.guild_id, interaction.user.id)
        if not likes:
            return await self._say(interaction, "You have no favourites yet. Use `!like` while a song plays.")
        await interaction.response.defer()
        await play_favourites(self._pctx(interaction), likes, shuffle)
        await self.redraw()

    async def _favs(self, interaction):
        await self._play_favs(interaction, False)

    async def _shuffle_favs(self, interaction):
        await self._play_favs(interaction, True)

    async def _radio(self, interaction):
        if not await self._guard(interaction):
            return
        seed = _radio_seed(self.guild_id)
        if seed is None:
            return await self._say(interaction, "Play something first so I know what to base the radio on.")
        set_guild_setting(self.guild_id, "autoplay", True)
        _autoplay_count.pop(self.guild_id, None)
        await interaction.response.defer()
        self._pctx(interaction)
        if not self._playing():
            await autoplay_next(self.guild_id, seed)
        await self.redraw()

    async def _quick(self, interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(QuickPlayModal(self))

    async def _stats(self, interaction):
        if not await self._guard(interaction, need_auth=False):
            return
        s = extras.summarize(DATA, self.guild_id, since_ts=time.time() - 7 * 86400)
        await interaction.response.defer()
        await reply_note(interaction.channel, vapor.stats_card("last 7 days", s["tracks"], s["seconds"],
                                                               s["top_songs"], s["top_people"]),
                         lifetime=NOTE_SECONDS * 5)

    # ---- dropdowns ------------------------------------------------------
    async def _pick_recent(self, interaction):
        if not await self._guard(interaction):
            return
        item = self._recent[int(self._recent_sel.values[0])]
        await interaction.response.defer()
        await queue_track(self._pctx(interaction), item)
        await self.redraw()

    async def _pick_effect(self, interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        try:
            apply_effect(self.guild_id, self._effect_sel.values[0])
        except ValueError as e:
            await interaction.channel.send(f"❌ {e}")
        await self.redraw()

    # ---- toggles --------------------------------------------------------
    async def _toggle(self, interaction, key, on_value, off_value, current_on):
        if not await self._guard(interaction):
            return
        set_guild_setting(self.guild_id, key, off_value if current_on else on_value)
        await interaction.response.defer()
        await self.redraw()

    async def _toggle_autoplay(self, interaction):
        _autoplay_count.pop(self.guild_id, None)
        await self._toggle(interaction, "autoplay", True, False, autoplay_enabled(self.guild_id))

    async def _toggle_lyrics(self, interaction):
        await self._toggle(interaction, "lyrics", True, False, lyrics_enabled(self.guild_id))
        tracker = _np_trackers.get(self.guild_id)
        if tracker is not None:
            tracker.force = True

    async def _toggle_fade(self, interaction):
        gid = self.guild_id
        await self._toggle(interaction, "fade", 4.0, 0.0, bool(guild_fade(gid)))
        src = current_sources.get(gid)
        if src is not None and hasattr(src, "fade") and not getattr(src, "game", False):
            src.fade = guild_fade(gid)


async def post_dashboard(dest):
    """Send the dashboard (badge GIF above, panel below); the previous one in this server loses its buttons."""
    gid = _dest_guild_id(dest)
    view = DashboardView(gid)
    old = _dashboards.get(gid)
    if old is not None:
        await old.retire()
    body = await view.header.sync(dest, view.render())
    view.message = await dest.send(content=body, view=view)
    _dashboards[gid] = view
    return view.message


@bot.command(name="dashboard", aliases=["menu", "home"], help="Open the dashboard: resume, favourites, radio, quick settings")
async def dashboard_cmd(ctx):
    set_notification_channel(ctx.guild.id, ctx.channel)
    await post_dashboard(ctx)


async def _session_tick():
    """Snapshot every server that is playing; flush to disk off the event loop."""
    for guild in list(bot.guilds):
        snapshot_session(guild.id, save=False)
    if _session_dirty:
        _session_dirty.clear()
        await asyncio.to_thread(DATA.save)


async def _session_loop():
    await asyncio.sleep(10)
    while True:
        try:
            await _session_tick()
        except Exception as e:
            print(f"⚠️ session snapshot error: {e}")
        await asyncio.sleep(10)


# Run the bot with fresh token
if __name__ == "__main__":
    TOKEN = os.getenv('NEW_DISCORD_TOKEN')
    
    if not TOKEN:
        print("❌ No NEW_DISCORD_TOKEN found!")
        print("💡 Set NEW_DISCORD_TOKEN in .env file")
        exit(1)
    
    from arken_deps import maybe_auto_update_dependencies

    maybe_auto_update_dependencies()

    # Multi-server foundation: move legacy global likes/playlists into the home guild once.
    # Safe to run every boot (idempotent). Set ARKEN_HOME_GUILD if more than one guild already
    # has settings/history and migration would be ambiguous.
    try:
        with DATA.lock:
            report = tenancy.run_data_migration(
                DATA.data,
                settings=_settings,
                playlists_root=str(PLAYLIST_DIR),
                home_guild_env=aenv.get("HOME_GUILD"),
            )
            if report.get("likes_moved"):
                DATA.save()
                print(f"🛡️ Tenancy: moved {report['likes_moved']} favourite(s) into guild {report['home_guild']}")
            pl = report.get("playlists") or {}
            if pl.get("moved"):
                print(f"🛡️ Tenancy: moved {pl['moved']} playlist item(s) into guild {report['home_guild']}")
            if pl.get("conflicts"):
                print(f"⚠️ Tenancy: playlist name conflicts left in place: {pl['conflicts']}")
            if report.get("skipped"):
                print("⚠️ Tenancy: set ARKEN_HOME_GUILD to finish migrating global likes")
    except Exception as e:
        print(f"⚠️ Tenancy migration skipped: {e}")

    print("🚀 Starting Discord Audio Bot...")
    print("🎵 Initializing streaming services...")
    print("📁 Loading audio file management...")
    _ensure_single_streaming_instance()
    _ensure_audio_processor_subprocess()
    bot.run(TOKEN)