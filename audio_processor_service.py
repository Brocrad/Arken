#!/usr/bin/env python3
"""
Audio Processing Service (Background Service)
This service handles ALL YouTube processing - completely separate from Discord
Discord never sees yt-dlp, YouTube APIs, or audio processing
"""

import yt_dlp
import os
import json
import time
import hashlib
import asyncio
import signal
import sys
import threading
import atexit
import shutil
import re
import random
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gondor_extras  # pure helpers (lyric title cleanup / best-match picking)


def _gondor_ffmpeg() -> str | None:
    env = os.environ.get("GONDOR_FFMPEG", "").strip()
    if env and os.path.isfile(env):
        return env
    found = shutil.which("ffmpeg")
    return found if found else None


# Cover thumbnails: the bot (which is air-gapped) shows a pixel-art version of the video's thumbnail in
# its "now playing" badge. THIS service fetches it and hands the bot a tiny raw RGB file (48x27, 3888 bytes).
COVERS_ENABLED = os.environ.get("GONDOR_COVERS", "1").strip().lower() not in ("0", "false", "no", "off")
COVER_W, COVER_H = 48, 27          # must match gif_art.COVER_W / COVER_H
COVER_KEEP = 200                   # newest cover files kept in media_library
COVER_WAIT = 4.0                   # max extra seconds a request waits for its cover


def _sanitize_media_query(q: str) -> str:
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


def _normalize_media_url(q: str) -> str:
    """Turn bare youtube.com / youtu.be into https URL for yt-dlp."""
    s = q.strip()
    if s.startswith(("http://", "https://")):
        return s
    if re.match(r"^(?:www\.)?(?:youtube\.com|music\.youtube\.com|youtu\.be)\b", s, re.I):
        return "https://" + s.lstrip("/")
    return s


def _is_direct_media_url(q: str) -> bool:
    s = q.strip()
    if s.startswith(("http://", "https://")):
        return True
    if re.match(r"^(?:www\.)?(?:youtube\.com|music\.youtube\.com|youtu\.be)\b", s, re.I):
        return True
    return False

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
POLL_INTERVAL = 0.2            # dispatcher poll (was 2s)
FG_WORKERS = 2                 # parallel foreground downloads (!play, first playlist track)
BG_THROTTLE_SECONDS = (0.5, 2.0)  # random pause after each background (batch) download
MAX_PLAYLIST_ITEMS = int(os.environ.get("GONDOR_MAX_PLAYLIST_ITEMS", "200"))
RECENT_CACHE_SIZE = int(os.environ.get("GONDOR_RECENT_CACHE", "10"))  # played files kept for instant replay
MEDIA_EXTS = (".webm", ".m4a", ".mp4", ".opus", ".mp3", ".ogg")

_YT_HOST_RE = re.compile(r"(?:^|//)(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)\b", re.I)
_YT_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


def _extract_video_id(url: str):
    """Video id from a YouTube URL (None for non-YouTube or playlist-only URLs)."""
    if not _YT_HOST_RE.search(url):
        return None
    m = _YT_ID_RE.search(url)
    return m.group(1) if m else None


def _atomic_write_json(path, data):
    """Write JSON via temp file + rename so readers never see a half-written file."""
    path = Path(path)
    tmp = path.with_name("." + path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows: reader momentarily has the target open
            time.sleep(0.02 * (attempt + 1))
    os.replace(tmp, path)


def _iter_media_files(media_dir):
    for f in Path(media_dir).iterdir():
        if f.is_file() and f.suffix.lower() in MEDIA_EXTS:
            yield f


def _file_in_playlist(path) -> bool:
    return SmartMediaCleaner()._is_file_in_playlist(path)


class _Progress:
    """Per-request progress beacon (progress_<id>.json) the bot turns into live Discord edits."""

    def __init__(self, queue_dir, request_id):
        self.path = Path(queue_dir) / f"progress_{request_id}.json" if request_id else None
        self.state = {"stage": "extracting", "percent": None}
        self._last_write = 0.0

    def update(self, force=False, **fields):
        self.state.update(fields)
        now = time.time()
        self.state["updated"] = now
        if not self.path:
            return
        if not force and now - self._last_write < 0.25:
            return
        self._last_write = now
        try:
            _atomic_write_json(self.path, self.state)
        except Exception:
            pass

    def hook(self, d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100.0) if total else None
            self.update(
                stage="downloading",
                percent=pct,
                downloaded=done,
                total=total,
                speed=d.get("speed"),
                eta=d.get("eta"),
            )
        elif status == "finished":
            self.update(force=True, stage="finishing", percent=100)

    def pp_hook(self, d):
        self.update(force=True, stage="finishing", percent=100)


# Windows: allow emoji in print() (same issue as streaming bot)
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# Measured against yt-dlp 2026.10.26: 'android'/'web'/'ios' only return 5 *combined* formats
# (no audio-only), so "worstaudio/worst" silently downloaded a ~12MB video. 'tv_embedded'
# exposes real audio-only formats (~3.4MB opus at ~128kbps) and is both smaller and faster.
FAST_CLIENTS = ['tv_embedded']
FALLBACK_CLIENTS = ['android', 'web', 'ios']
AUDIO_FORMAT = os.environ.get(
    "GONDOR_AUDIO_FORMAT", "bestaudio[abr<=130]/bestaudio/worstaudio/worst"
)


def _youtube_ytdlp_opts(clients=None):
    """Options that reduce YouTube 403 / SABR / missing-URL issues vs default web client."""
    return {
        'extractor_args': {
            'youtube': {
                'player_client': list(clients or FAST_CLIENTS),
            },
        },
        'retries': 5,
        'fragment_retries': 5,
        'noplaylist': True,
        'noprogress': True,                 # we report progress ourselves
        'http_chunk_size': 10 * 1024 * 1024,  # avoids per-connection throttling
    }


class SmartMediaCleaner:
    """Air-gapped media cleanup - Discord never sees this"""
    def __init__(self, media_dir="media_library", max_cache_size_mb=100, max_age_days=3):
        self.media_dir = Path(media_dir)
        self.max_cache_size = max_cache_size_mb * 1024 * 1024  # Convert to bytes
        self.max_age_seconds = max_age_days * 24 * 60 * 60
        
    def cleanup_by_age(self):
        """Remove files older than max_age_days (protects playlist files)"""
        cleaned_files = []
        current_time = time.time()
        
        for webm_file in _iter_media_files(self.media_dir):
            file_age = current_time - webm_file.stat().st_mtime
            
            if file_age > self.max_age_seconds:
                # Skip if file is referenced in any playlist
                if self._is_file_in_playlist(webm_file):
                    continue
                
                json_file = webm_file.with_suffix('.json')
                
                try:
                    webm_size = webm_file.stat().st_size
                    webm_file.unlink()
                    if json_file.exists():
                        json_file.unlink()
                    
                    cleaned_files.append({
                        'file': webm_file.name,
                        'size_mb': webm_size / (1024 * 1024),
                        'age_days': file_age / (24 * 60 * 60)
                    })
                except Exception as e:
                    print(f"⚠️ Cleanup failed {webm_file.name}: {e}")
        
        return cleaned_files
    
    def _is_file_in_playlist(self, file_path):
        """Check if a file is referenced in any playlist (protects both original and copied files)"""
        try:
            playlist_dir = Path("playlists")
            if not playlist_dir.exists():
                return False
            
            file_str = str(file_path)
            file_name = Path(file_path).name
            
            for playlist_file in playlist_dir.glob("*.json"):
                try:
                    with open(playlist_file, 'r') as f:
                        playlist_data = json.load(f)
                    
                    for song in playlist_data.get("files", []):
                        # Check if this is the exact file path
                        if song.get("audio_file") == file_str:
                            return True
                        
                        # Check if this is the original file that was copied to playlist
                        # (compare filenames to catch the original before it gets deleted)
                        playlist_file_name = Path(song.get("audio_file", "")).name
                        if playlist_file_name == file_name:
                            return True
                            
                except Exception:
                    continue
            
            return False
        except Exception:
            return False
    
    def cleanup_by_size(self):
        """Remove oldest files to stay under max_cache_size"""
        webm_files = []
        for webm_file in _iter_media_files(self.media_dir):
            stat = webm_file.stat()
            webm_files.append((webm_file, stat.st_mtime, stat.st_size))
        
        webm_files.sort(key=lambda x: x[1])  # Sort by modification time
        current_size = sum(size for _, _, size in webm_files)
        cleaned_files = []
        
        while current_size > self.max_cache_size and webm_files:
            file_path, mod_time, file_size = webm_files.pop(0)
            
            # Skip if file is referenced in any playlist
            if self._is_file_in_playlist(file_path):
                continue
            
            json_file = file_path.with_suffix('.json')
            
            try:
                file_path.unlink()
                if json_file.exists():
                    json_file.unlink()
                
                current_size -= file_size
                cleaned_files.append({
                    'file': file_path.name,
                    'size_mb': file_size / (1024 * 1024)
                })
            except Exception as e:
                print(f"⚠️ Size cleanup failed {file_path.name}: {e}")
        
        return cleaned_files
    
    def cleanup_duplicates(self):
        """Remove duplicate downloads (same video_id, keep newest)"""
        song_groups = {}
        
        for json_file in self.media_dir.glob("*.json"):
            try:
                with open(json_file) as f:
                    metadata = json.load(f)
                
                video_id = metadata.get('video_id', '')
                if video_id:
                    if video_id not in song_groups:
                        song_groups[video_id] = []
                    
                    webm_file = Path(metadata.get('file_path', '')) if metadata.get('file_path') else json_file.with_suffix('.webm')
                    if webm_file.exists():
                        song_groups[video_id].append({
                            'json_file': json_file,
                            'webm_file': webm_file,
                            'timestamp': metadata.get('processed_time', 0)
                        })
            except Exception:
                continue
        
        cleaned_files = []
        for video_id, files in song_groups.items():
            if len(files) > 1:
                files.sort(key=lambda x: x['timestamp'], reverse=True)
                
                for old_file in files[1:]:  # Remove all but newest
                    try:
                        size = old_file['webm_file'].stat().st_size
                        old_file['webm_file'].unlink()
                        old_file['json_file'].unlink()
                        
                        cleaned_files.append({
                            'file': old_file['webm_file'].name,
                            'size_mb': size / (1024 * 1024)
                        })
                    except Exception:
                        continue
        
        return cleaned_files
    
    def run_smart_cleanup(self):
        """Run comprehensive cleanup - completely hidden from Discord"""
        try:
            duplicates = self.cleanup_duplicates()
            aged = self.cleanup_by_age()
            sized = self.cleanup_by_size()
            
            total_cleaned = len(duplicates) + len(aged) + len(sized)
            
            if total_cleaned > 0:
                print(f"🧹 Background cleanup: {total_cleaned} files removed")
                if duplicates:
                    print(f"  🔄 {len(duplicates)} duplicates")
                if aged:
                    print(f"  ⏰ {len(aged)} old files")
                if sized:
                    print(f"  💾 {len(sized)} for size limit")
            
        except Exception as e:
            print(f"⚠️ Background cleanup error: {e}")

class AudioProcessor:
    def __init__(self, quality_mode='optimized'):
        # ALL the YouTube/music processing happens HERE - not in Discord bot
        
        # Quality presets
        if quality_mode == 'optimized':
            # Smallest files for streaming (128kbps MP3)
            self.audio_format_options = {
                **_youtube_ytdlp_opts(),
                'format': AUDIO_FORMAT,
                'quiet': True,
                'no_warnings': True,
                'extractaudio': True,
                'audioformat': 'mp3',
                'audioquality': '5',  # Medium quality
                'socket_timeout': 45,
                'prefer_ffmpeg': True,
                'keepvideo': False,
                'postprocessor_args': ['-ar', '44100', '-ac', '2', '-b:a', '128k'],
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'referer': 'https://www.youtube.com/',
                'nocheckcertificate': True,
                'ignoreerrors': False,
            }
            print("🎯 Audio quality: Optimized for streaming (128kbps MP3)")
            
        elif quality_mode == 'balanced':
            # Balanced quality/size (192kbps)
            self.audio_format_options = {
                **_youtube_ytdlp_opts(),
                'format': 'bestaudio[abr<=192]/bestaudio/best',
                'quiet': True,
                'no_warnings': True,
                'extractaudio': True,
                'audioformat': 'mp3',
                'audioquality': '3',
                'socket_timeout': 45,
                'prefer_ffmpeg': True,
                'keepvideo': False,
                'postprocessor_args': ['-ar', '44100', '-ac', '2', '-b:a', '192k'],
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'referer': 'https://www.youtube.com/',
                'nocheckcertificate': True,
                'ignoreerrors': False,
            }
            print("⚖️ Audio quality: Balanced (192kbps MP3)")
            
        else:  # high quality
            # Best quality available
            self.audio_format_options = {
                **_youtube_ytdlp_opts(),
                'format': 'bestaudio/best',
                'quiet': True,
                'no_warnings': True,
                'extractaudio': True,
                'audioformat': 'best',
                'audioquality': '0',
                'socket_timeout': 45,
                'prefer_ffmpeg': True,
                'keepvideo': False,
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'referer': 'https://www.youtube.com/',
                'nocheckcertificate': True,
                'ignoreerrors': False,
            }
            print("🔊 Audio quality: High quality (best available)")
        
        # Setup directories
        self.media_dir = Path("media_library")
        self.queue_dir = Path("request_queue")  
        self.media_dir.mkdir(exist_ok=True)
        self.queue_dir.mkdir(exist_ok=True)
        self._cover_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cover")
        self._ytdl_tmp = self.media_dir / "_ytdl_tmp"
        self._ytdl_tmp.mkdir(exist_ok=True)

        ff = _gondor_ffmpeg()
        if ff:
            self.audio_format_options["ffmpeg_location"] = ff
        else:
            print("⚠️ ffmpeg not on PATH — set GONDOR_FFMPEG to a full path if downloads fail (WinError 22).")

        self.audio_format_options.update(
            {
                "restrictfilenames": True,
                "windowsfilenames": True,
                "paths": {"temp": str(self._ytdl_tmp.resolve())},
            }
        )

        # Initialize processor with memory management
        self.processor = yt_dlp.YoutubeDL(self.audio_format_options)
        self.request_count = 0  # Track processed requests for memory management
        self._recent_files = []          # replay cache (oldest first)
        self._recent_lock = threading.Lock()
        self._lane_lock = threading.Lock()
        self._bg_busy = 0
        self._retry_delete = set()       # files that were locked when we tried to delete them
    
    def check_network_connectivity(self):
        """Check if we can reach YouTube and other essential services"""
        import socket
        import urllib.request
        
        # Test DNS resolution first
        try:
            socket.gethostbyname('www.google.com')
            print("✅ DNS resolution working")
        except socket.gaierror as e:
            print(f"❌ DNS resolution failed: {e}")
            return False
        
        # Test basic HTTP connectivity
        test_urls = [
            'https://www.google.com',
            'https://www.youtube.com',
            'https://httpbin.org/get'
        ]
        
        for url in test_urls:
            try:
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status == 200:
                        print(f"✅ Can reach {url}")
                        return True
            except Exception as e:
                print(f"⚠️ Cannot reach {url}: {e}")
                continue
        
        print("❌ No network connectivity detected")
        
        # Try to flush DNS cache as last resort
        try:
            print("🔄 Attempting DNS cache flush...")
            import subprocess
            result = subprocess.run(['ipconfig', '/flushdns'], 
                                  capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                print("✅ DNS cache flushed, retrying connectivity...")
                # Try one more time after DNS flush
                try:
                    socket.gethostbyname('www.google.com')
                    print("✅ DNS resolution working after flush")
                    return True
                except:
                    pass
            else:
                print("⚠️ DNS flush failed")
        except Exception as e:
            print(f"⚠️ Could not flush DNS: {e}")
        
        return False
        
        # Initialize air-gapped cleanup system
        self.cleaner = SmartMediaCleaner(
            media_dir=self.media_dir,
            max_cache_size_mb=100,  # 100MB cache limit
            max_age_days=3          # Keep files for 3 days max
        )
        
        # Start background cleanup (completely hidden from Discord)
        self._start_cleanup_scheduler()
        
        # Run initial cleanup
        print("🧹 Running initial cleanup...")
        self.cleaner.run_smart_cleanup()
        
        print("🎵 Audio Processing Service Started")
        print("📁 Audio library management: Active")
        print("🎧 File processing: Online")
        print("🧹 Smart cleanup enabled (100MB limit, 3-day retention)")
        
    def generate_safe_filename(self, query, video_id=None):
        """Generate completely generic filename that reveals nothing"""
        # Create hash of query + video_id for consistent naming
        content = f"{query}_{video_id}" if video_id else query
        hash_object = hashlib.md5(content.encode("utf-8", errors="ignore"))
        file_hash = hash_object.hexdigest()[:12]
        
        # Generic filename with timestamp
        timestamp = int(time.time())
        return f"media_{timestamp}_{file_hash}"
    
    # ------------------------------------------------------------------
    # Cache / recent-file retention
    # ------------------------------------------------------------------

    def _find_cached(self, video_id):
        """Return (metadata, Path) for an already-downloaded video, or None."""
        if not video_id:
            return None
        for jf in self.media_dir.glob("media_*.json"):
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue
            if meta.get("video_id") != video_id:
                continue
            fp = Path(meta.get("file_path", ""))
            if fp.exists() and fp.suffix.lower() in MEDIA_EXTS:
                self._touch_recent(fp)
                return meta, fp
        return None

    def _find_cached_by_query(self, query):
        """Repeat of an identical search text -> reuse the file without even searching."""
        q = " ".join(query.lower().split())
        if not q:
            return None
        for jf in self.media_dir.glob("media_*.json"):
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue
            if " ".join(str(meta.get("original_query", "")).lower().split()) != q:
                continue
            fp = Path(meta.get("file_path", ""))
            if fp.exists() and fp.suffix.lower() in MEDIA_EXTS:
                self._touch_recent(fp)
                return meta, fp
        return None

    def _touch_recent(self, path):
        """Refresh a file's position in the recent list (protects it from eviction)."""
        key = str(Path(path))
        with self._recent_lock:
            if key in self._recent_files:
                self._recent_files.remove(key)
                self._recent_files.append(key)

    def _retain_recent(self, path):
        """
        Delete-after-play with a tiny replay cache: the last RECENT_CACHE_SIZE played
        files stay on disk (so replays / 'previous' are instant); older ones are deleted.
        """
        key = str(Path(path))
        evicted = []
        with self._recent_lock:
            if key in self._recent_files:
                self._recent_files.remove(key)
            self._recent_files.append(key)
            while len(self._recent_files) > RECENT_CACHE_SIZE:
                evicted.append(self._recent_files.pop(0))
        for old in evicted:
            self._delete_media_file(old)

    def _delete_media_file(self, file_path):
        """Delete a media file + its metadata sidecar (never touches playlist-protected files)."""
        try:
            p = Path(file_path)
            if _file_in_playlist(p):
                return
            side = p.with_suffix(".json")
            if p.exists():
                p.unlink()
                print(f"🧹 Cleaned: {p.name}")
            if side.exists():
                side.unlink()
        except PermissionError:
            # Still open (being played / read) -> retry shortly instead of leaking the file
            self._retry_delete.add(str(file_path))
        except Exception as e:
            print(f"⚠️ Cleanup failed for {os.path.basename(str(file_path))}: {e}")

    def _retry_pending_deletes(self):
        for fp in list(self._retry_delete):
            self._retry_delete.discard(fp)
            if os.path.exists(fp):
                self._delete_media_file(fp)

    # ------------------------------------------------------------------
    # Core download path
    # ------------------------------------------------------------------

    def _flat_opts(self, **extra):
        opts = {
            **_youtube_ytdlp_opts(),
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "socket_timeout": 45,
            "nocheckcertificate": True,
        }
        opts.update(extra)
        return opts

    def _resolve_search(self, query):
        """One cheap flat search request -> (video_id, title, duration) or None."""
        with yt_dlp.YoutubeDL(self._flat_opts()) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entries = [e for e in (info or {}).get("entries") or [] if e]
        if not entries:
            return None
        e = entries[0]
        return e.get("id"), e.get("title") or "Unknown", e.get("duration") or 0

    def _download_once(self, target, outtmpl, prog):
        """Single extract+download pass (no separate extraction step), with client fallback."""
        attempts = [
            {},  # fast path: tv_embedded (real audio-only formats)
            {"extractor_args": {"youtube": {"player_client": FALLBACK_CLIENTS}}},
        ]
        last_err = None
        for i, extra in enumerate(attempts):
            opts = {
                **self.audio_format_options,
                "outtmpl": outtmpl,
                "progress_hooks": [prog.hook],
                "postprocessor_hooks": [prog.pp_hook],
                **extra,
            }
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(target, download=True)
                if info and info.get("entries"):
                    info = next((e for e in info["entries"] if e), info)
                return info
            except Exception as e:
                last_err = e
                print(f"❌ Download attempt {i + 1} failed: {e}")
                prog.update(force=True, stage="extracting", percent=None)
        raise last_err

    # ------------------------------------------------------------------
    # Cover thumbnails (optional extra; never blocks or fails a request)
    # ------------------------------------------------------------------

    def _prune_covers(self):
        try:
            files = sorted(self.media_dir.glob("cover_*.rgb"), key=lambda f: f.stat().st_mtime, reverse=True)
            for old in files[COVER_KEEP:]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    def _fetch_cover(self, video_id):
        """Download the YouTube thumbnail and shrink it to COVER_W x COVER_H raw rgb24. Returns the path or None."""
        if not COVERS_ENABLED or not video_id or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", str(video_id)):
            return None
        want = COVER_W * COVER_H * 3
        path = self.media_dir / f"cover_{video_id}.rgb"
        try:
            if path.exists() and path.stat().st_size == want:
                os.utime(path, None)
                return str(path)
            ffmpeg = _gondor_ffmpeg()
            if not ffmpeg:
                return None
            for name, vf in (("mqdefault", ""), ("hqdefault", "crop=iw:ih*3/4,")):  # hq has letterbox bars
                try:
                    req = urllib.request.Request(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg",
                                                 headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=6) as r:
                        img = r.read()
                except Exception:
                    continue
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                proc = subprocess.run(
                    [ffmpeg, "-v", "error", "-i", "pipe:0", "-vf", f"{vf}scale={COVER_W}:{COVER_H}:flags=area",
                     "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                    input=img, capture_output=True, timeout=15, creationflags=flags)
                if len(proc.stdout) == want:
                    tmp = path.with_suffix(".tmp")
                    tmp.write_bytes(proc.stdout)
                    os.replace(tmp, path)
                    self._prune_covers()
                    return str(path)
        except Exception as e:
            print(f"⚠️ cover fetch failed for {video_id}: {e}")
        return None

    def process_audio_request(self, query, request_id=None, lane="foreground"):
        """Resolve + download one track into the media library (+ a small cover file for the bot)."""
        holder = {}
        result = self._process_audio_request(query, request_id, lane, holder)
        try:
            if COVERS_ENABLED and isinstance(result, dict) and result.get("status") == "success":
                fut = holder.get("cover")
                if fut is None and result.get("video_id"):
                    fut = self._cover_pool.submit(self._fetch_cover, result["video_id"])
                cover = fut.result(timeout=COVER_WAIT) if fut is not None else None
                if cover:
                    result["cover_path"] = cover
        except Exception as e:
            print(f"⚠️ cover skipped: {e}")
        return result

    def _process_audio_request(self, query, request_id, lane, holder):
        """Resolve + download one track into the media library."""
        t_start = time.time()
        timings = {}
        prog = _Progress(self.queue_dir, request_id)
        try:
            query = _sanitize_media_query(query)
            print(f"🔍 [{request_id or '-'}] Processing ({lane}): {query}")
            if not query:
                return {"status": "error", "message": "Empty search query"}

            prog.update(force=True, stage="extracting", percent=None)

            video_id = None
            title = "Unknown"
            duration = 0
            direct = _is_direct_media_url(query)

            if direct:
                target = _normalize_media_url(query)
                video_id = _extract_video_id(target)
            else:
                qhit = self._find_cached_by_query(query)
                if qhit:
                    meta, fp = qhit
                    print(f"📁 Already cached (same search): {fp.name}")
                    return {
                        "status": "success",
                        "file_path": str(fp),
                        "title": meta.get("title", "Unknown"),
                        "duration": meta.get("duration", 0),
                        "video_id": meta.get("video_id"),
                        "cached": True,
                    }
                t0 = time.time()
                try:
                    resolved = self._resolve_search(query)
                except Exception as e:
                    print(f"⚠️ Flat search failed ({e}); falling back to search-download")
                    resolved = None
                timings["search"] = time.time() - t0
                if resolved and resolved[0]:
                    video_id, title, duration = resolved
                    target = f"https://www.youtube.com/watch?v={video_id}"
                else:
                    target = f"ytsearch1:{query}"

            if video_id and COVERS_ENABLED:
                holder["cover"] = self._cover_pool.submit(self._fetch_cover, video_id)  # runs alongside the download

            # Cache hit -> instant
            if video_id:
                hit = self._find_cached(video_id)
                if hit:
                    meta, fp = hit
                    print(f"📁 Already cached: {fp.name}")
                    return {
                        "status": "success",
                        "file_path": str(fp),
                        "title": meta.get("title", title),
                        "duration": meta.get("duration", duration),
                        "video_id": video_id,
                        "cached": True,
                    }

            safe_filename = self.generate_safe_filename(query, video_id)
            outtmpl = str((self.media_dir / f"{safe_filename}.%(ext)s").resolve())
            print(f"⬇️ Downloading: {title if title != 'Unknown' else target}")

            t0 = time.time()
            try:
                info = self._download_once(target, outtmpl, prog)
            except Exception as e:
                return {"status": "error", "message": f"Download failed: {e}"}
            timings["download"] = time.time() - t0

            # Locate the file yt-dlp actually wrote
            downloaded_file = None
            for rd in (info or {}).get("requested_downloads") or []:
                cand = rd.get("filepath") or rd.get("_filename")
                if cand and Path(cand).exists():
                    downloaded_file = Path(cand)
                    break
            if downloaded_file is None:
                for ext in [".mp3", ".webm", ".m4a", ".mp4", ".opus", ".ogg"]:
                    cand = self.media_dir / f"{safe_filename}{ext}"
                    if cand.exists():
                        downloaded_file = cand
                        break
            if downloaded_file is None or not downloaded_file.exists():
                return {"status": "error", "message": "Download failed - no file found"}

            info = info or {}
            video_id = info.get("id") or video_id
            title = info.get("title") or title
            duration = info.get("duration") or duration or 0

            size_mb = downloaded_file.stat().st_size / 1024 / 1024
            print(f"✅ Downloaded: {downloaded_file.name} ({size_mb:.2f}MB)")

            metadata = {
                "original_query": query,
                "title": title,
                "duration": duration,
                "video_id": video_id,
                "file_path": str(downloaded_file),
                "processed_time": time.time(),
            }
            with open(self.media_dir / f"{safe_filename}.json", "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)

            return {
                "status": "success",
                "file_path": str(downloaded_file),
                "title": title,
                "duration": duration,
                "video_id": video_id,
                "cached": False,
            }

        except Exception as e:
            print(f"❌ Processing error: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            timings["total"] = time.time() - t_start
            stages = " ".join(f"{k}={v:.1f}s" for k, v in timings.items())
            print(f"⏱️ [{request_id or '-'}] {stages}")

    # ------------------------------------------------------------------
    # Extra lookups for the bot (it is air-gapped, so every network call lives here)
    # ------------------------------------------------------------------

    def process_lyrics(self, title, duration=0):
        """Synced (LRC) or plain lyrics for a track from LRCLIB (free, no key). Result carries the raw text."""
        query = gondor_extras.lyric_query(title)
        if not query:
            return {"status": "error", "message": "no title to search lyrics for"}
        url = "https://lrclib.net/api/search?q=" + urllib.parse.quote(query)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Gondor/1.0 (discord music bot)"})
            with urllib.request.urlopen(req, timeout=10) as r:
                results = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            return {"status": "error", "message": f"lyrics lookup failed: {e}"}
        best = gondor_extras.pick_lyrics(results if isinstance(results, list) else [], duration or 0)
        if not best:
            return {"status": "error", "message": "no lyrics found for this track"}
        return {
            "status": "success",
            "synced": (best.get("syncedLyrics") or "").strip(),
            "plain": (best.get("plainLyrics") or "").strip(),
            "track": best.get("trackName"),
            "artist": best.get("artistName"),
        }

    def process_related(self, video_id, title="", max_items=15):
        """Suggested next tracks: YouTube's own 'mix' for the video, else a search on the title."""
        entries = []
        targets = []
        if video_id and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", str(video_id)):
            targets.append(f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}")
        if title:
            targets.append(f"ytsearch{max_items}:{gondor_extras.lyric_query(title)} mix")
        for target in targets:
            try:
                opts = self._flat_opts(extract_flat="in_playlist", noplaylist=False, playlistend=max_items,
                                       ignoreerrors=True)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(target, download=False)
            except Exception as e:
                print(f"⚠️ related lookup failed ({target[:40]}): {e}")
                continue
            for e in (info or {}).get("entries") or []:
                if not e or not e.get("id") or e["id"] == video_id:
                    continue
                dur = int(e.get("duration") or 0)
                if dur and (dur < 45 or dur > 20 * 60):   # skip shorts and hour-long mixes
                    continue
                entries.append({"id": e["id"], "title": e.get("title") or "Unknown", "duration": dur,
                                "url": f"https://www.youtube.com/watch?v={e['id']}"})
            if entries:
                break
        if not entries:
            return {"status": "error", "message": "no related tracks found"}
        return {"status": "success", "entries": entries}

    def process_playlist_info(self, url, max_items=MAX_PLAYLIST_ITEMS):
        """Flat-extract a YouTube playlist: ids/titles/durations only, nothing downloaded."""
        url = _normalize_media_url(_sanitize_media_query(url))
        m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url)
        if m and m.group(1).startswith(("RD", "UL")):
            return {
                "status": "error",
                "message": "Auto-generated YouTube mixes/radios are endless - use a regular playlist link.",
            }
        opts = self._flat_opts(
            extract_flat="in_playlist",
            noplaylist=False,
            playlistend=max_items,
            ignoreerrors=True,
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            return {"status": "error", "message": f"Could not read playlist: {e}"}
        if not info or not info.get("entries"):
            return {"status": "error", "message": "That link isn't a playlist (or it's empty/private)."}

        entries = []
        for e in info["entries"]:
            if not e or not e.get("id"):
                continue
            t = e.get("title") or ""
            if t in ("[Private video]", "[Deleted video]"):
                continue
            entries.append(
                {
                    "id": e["id"],
                    "title": t or "Unknown",
                    "duration": int(e.get("duration") or 0),
                    "url": f"https://www.youtube.com/watch?v={e['id']}",
                }
            )
        if not entries:
            return {"status": "error", "message": "No playable videos found in that playlist."}
        total = info.get("playlist_count") or len(entries)
        return {
            "status": "success",
            "title": info.get("title") or "YouTube Playlist",
            "playlist_id": info.get("id"),
            "entries": entries,
            "total": max(total, len(entries)),
            "truncated": total > len(entries),
        }

    def _start_cleanup_scheduler(self):
        """Start background cleanup thread - completely hidden from Discord"""
        def cleanup_worker():
            while True:
                try:
                    # Run cleanup every 6 hours
                    time.sleep(6 * 3600)  # 6 hours
                    print("🕐 Running scheduled cleanup...")
                    # self.cleaner.run_smart_cleanup()
                except Exception as e:
                    print(f"⚠️ Scheduled cleanup error: {e}")
        
        cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
        cleanup_thread.start()
        print("⏰ Background cleanup scheduled every 6 hours")
    
    def _start_heartbeat(self):
        """Liveness beacon the bot uses instead of a hard timeout."""
        hb_file = self.queue_dir / ".processor_heartbeat"

        def beat():
            while True:
                try:
                    hb_file.write_text(str(time.time()))
                except Exception:
                    pass
                time.sleep(2)

        threading.Thread(target=beat, daemon=True, name="heartbeat").start()

    def _sweep_stale_queue_files(self):
        """Remove result/progress/tmp files orphaned by crashes or abandoned requests."""
        cutoff = time.time() - 3600
        for pattern in ("result_*.json", "progress_*.json", ".*.tmp", "*.tmp"):
            for f in self.queue_dir.glob(pattern):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except Exception:
                    pass

    def _handle_cleanup_files(self):
        """Immediate cleanup requests from the bot (delete-after-play, startup/shutdown wipes)."""
        for cleanup_file in list(self.queue_dir.glob("cleanup_*.json")):
            try:
                with open(cleanup_file, "r", encoding="utf-8") as f:
                    cleanup_data = json.load(f)
            except json.JSONDecodeError:
                # Possibly mid-write; drop it only if it's clearly stale
                try:
                    if time.time() - cleanup_file.stat().st_mtime > 10:
                        cleanup_file.unlink()
                except Exception:
                    pass
                continue
            except Exception:
                continue

            try:
                cleanup_type = cleanup_data.get("type")
                if cleanup_type == "cleanup":
                    file_path = cleanup_data.get("file_path", "")
                    if file_path and os.path.exists(file_path):
                        if cleanup_data.get("keep_recent", True):
                            self._retain_recent(file_path)
                        else:
                            self._delete_media_file(file_path)
                elif cleanup_type == "cleanup_all":
                    reason = cleanup_data.get("reason", "unknown")
                    print(f"🧹 Running full cleanup ({reason})...")
                    with self._recent_lock:
                        self._recent_files.clear()
                    cleaned = failed = 0
                    for media_file in _iter_media_files(self.media_dir):
                        try:
                            media_file.unlink()
                            side = media_file.with_suffix(".json")
                            if side.exists():
                                side.unlink()
                            cleaned += 1
                        except Exception as err:
                            failed += 1
                            print(f"⚠️ Failed to clean {media_file.name}: {err}")
                    if cleaned:
                        print(f"🧹 Full cleanup complete: {cleaned} files removed")
                    if failed:
                        print(f"⚠️ {failed} files failed to clean")
            except Exception as e:
                print(f"❌ Error processing cleanup {cleanup_file}: {e}")
            finally:
                try:
                    cleanup_file.unlink()
                except Exception:
                    pass

    def _run_request(self, data, lane):
        """Worker-thread entry: run one request and publish its result."""
        rid = data.get("request_id", "")
        try:
            if data.get("type") == "playlist_info":
                result = self.process_playlist_info(
                    data.get("url", ""), int(data.get("max_items") or MAX_PLAYLIST_ITEMS)
                )
            elif data.get("type") == "lyrics":
                result = self.process_lyrics(data.get("title", ""), float(data.get("duration") or 0))
            elif data.get("type") == "related":
                result = self.process_related(data.get("video_id"), data.get("title", ""),
                                              int(data.get("max_items") or 15))
            else:
                query = data.get("query", "")
                if not query:
                    result = {"status": "error", "message": "Empty search query"}
                else:
                    result = self.process_audio_request(query, request_id=rid, lane=lane)
        except Exception as e:
            print(f"❌ Request {rid} crashed: {e}")
            result = {"status": "error", "message": str(e)}

        try:
            _atomic_write_json(self.queue_dir / f"result_{rid}.json", result)
        except Exception as e:
            print(f"❌ Could not write result for {rid}: {e}")
        try:
            (self.queue_dir / f"progress_{rid}.json").unlink()
        except Exception:
            pass

        if lane == "background":
            # Be polite between bulk downloads (stealth pacing)
            time.sleep(random.uniform(*BG_THROTTLE_SECONDS))
            with self._lane_lock:
                self._bg_busy -= 1

    def _dispatch_requests(self, fg_pool, bg_pool):
        """
        Claim request files. Foreground requests are claimed immediately; background
        (playlist batch) requests are claimed one at a time so unclaimed ones stay on
        disk where the bot can still cancel or promote them.
        """
        parsed = []
        for rf in self.queue_dir.glob("request_*.json"):
            try:
                mtime = rf.stat().st_mtime
                with open(rf, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                continue
            except json.JSONDecodeError:
                try:
                    if time.time() - rf.stat().st_mtime > 10:
                        rf.unlink()
                except Exception:
                    pass
                continue
            except Exception:
                continue
            parsed.append((0 if data.get("lane", "foreground") != "background" else 1, mtime, rf, data))

        parsed.sort(key=lambda x: (x[0], x[1]))
        for prio, _mtime, rf, data in parsed:
            if prio == 1:
                with self._lane_lock:
                    if self._bg_busy >= 1:
                        continue
                    self._bg_busy += 1
                try:
                    rf.unlink()
                except FileNotFoundError:
                    # Bot cancelled/promoted it between our read and claim
                    with self._lane_lock:
                        self._bg_busy -= 1
                    continue
                bg_pool.submit(self._run_request, data, "background")
            else:
                try:
                    rf.unlink()
                except FileNotFoundError:
                    continue
                fg_pool.submit(self._run_request, data, "foreground")

    def process_queue(self):
        """Dispatcher loop: cleanup requests, then audio/playlist requests, on a fast poll."""
        self._start_heartbeat()
        self._sweep_stale_queue_files()
        fg_pool = ThreadPoolExecutor(max_workers=FG_WORKERS, thread_name_prefix="fg")
        bg_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bg")
        print(f"⚡ Dispatcher ready ({FG_WORKERS} foreground workers + 1 background lane, poll {POLL_INTERVAL}s)")

        last_retry = time.time()
        while True:
            try:
                self._handle_cleanup_files()
                self._dispatch_requests(fg_pool, bg_pool)
                if self._retry_delete and time.time() - last_retry > 5:
                    last_retry = time.time()
                    self._retry_pending_deletes()
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                print("\n🛑 Shutting down Audio Processing Service")
                fg_pool.shutdown(wait=False, cancel_futures=True)
                bg_pool.shutdown(wait=False, cancel_futures=True)
                break
            except Exception as e:
                print(f"❌ Queue processing error: {e}")
                time.sleep(2)


def signal_handler(signum, frame):
    print("\n🛑 Received shutdown signal")
    sys.exit(0)

def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import subprocess as sp
            r = sp.run(
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


def _exit_if_duplicate_processor():
    """Only one queue consumer; if another is alive, exit quietly so a second bot can share it."""
    queue_dir = Path("request_queue")
    queue_dir.mkdir(exist_ok=True)
    pid_file = queue_dir / ".audio_processor.pid"
    if pid_file.exists():
        try:
            old = int(pid_file.read_text().strip())
            if _pid_exists(old):
                print(f"Audio processor already running (PID {old}) — exiting")
                sys.exit(0)
        except (ValueError, OSError):
            pass
        try:
            pid_file.unlink()
        except OSError:
            pass
    pid_file.write_text(str(os.getpid()))

    def _unlink_pid():
        try:
            if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass

    atexit.register(_unlink_pid)


if __name__ == "__main__":
    _exit_if_duplicate_processor()

    # Setup signal handling
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start processor
    processor = AudioProcessor()
    
    # Test mode - process single request
    if len(sys.argv) > 1:
        test_query = " ".join(sys.argv[1:])
        print(f"🧪 Test mode: Processing '{test_query}'")
        result = processor.process_audio_request(test_query)
        print(f"📋 Result: {result}")
    else:
        # Queue processing mode
        print("🔄 Starting queue processing mode...")
        print("💡 Use 'python audio_processor_service.py \"song name\"' for testing")
        processor.process_queue()