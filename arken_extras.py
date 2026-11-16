"""
Pure-logic helpers behind Arken's extra features. Nothing here touches Discord or the network, so it
is all unit-testable:

    audio effects   resolve_effect("slowed")            -> ffmpeg filter + speed factor
    sleep timer     parse_sleep("1h30m")                -> seconds ("end" / None for the special cases)
    stats           record_play / summarize             play history + weekly recap numbers
    favourites      add_like / get_likes / remove_like  per-user liked songs, scoped to one guild
    guess the song  guess_matches("lose yourself", "Eminem - Lose Yourself (Official Video)")
    lyrics          parse_lrc / lyric_at / pick_lyrics / lyric_query
    voting          votes_needed
    fades           fade_gain
    autoplay        pick_related
"""

import bisect
import difflib
import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# Audio effects (ffmpeg -af chains).  speed = how fast the SONG advances relative to the output, so the
# bot can keep the "0:42 / 3:10" clock honest when a filter changes the tempo.
# ---------------------------------------------------------------------------

SPEED_MIN, SPEED_MAX = 0.5, 2.0

_RESAMPLED = "aresample=48000"


def _rate(factor):
    """Change speed AND pitch together (the classic slowed / nightcore trick), whatever the input rate."""
    return f"{_RESAMPLED},asetrate={int(48000 * factor)},{_RESAMPLED}"


EFFECTS = {
    "off": ("", 1.0, "no effect"),
    "slowed": (_rate(0.85) + ",aecho=0.8:0.88:60|120:0.35|0.2", 0.85, "slowed + reverb"),
    "vaporwave": (_rate(0.80) + ",aecho=0.8:0.9:80|160:0.4|0.25", 0.80, "vaporwave (slower, lower, dreamy)"),
    "nightcore": (_rate(1.25), 1.25, "nightcore (faster, higher)"),
    "reverb": ("aecho=0.8:0.88:60|120|240:0.4|0.3|0.2", 1.0, "reverb"),
    "bass": ("bass=g=10:f=90:w=0.6,acompressor=threshold=0.25:ratio=3,alimiter=limit=0.95", 1.0, "bass boost"),
    "8d": ("apulsator=hz=0.12", 1.0, "8D (sound drifts left and right)"),
}


def effect_names():
    return tuple(EFFECTS)


def resolve_effect(spec):
    """
    'slowed' | 'bass' | 'off' | 'speed 1.25' | '1.25x' -> (key, ffmpeg filter or '', speed, description).
    Raises ValueError with a human message for anything else.
    """
    text = (spec or "").strip().lower()
    if not text:
        raise ValueError("name an effect: " + ", ".join(EFFECTS) + ", or `speed 1.25`")
    if text in EFFECTS:
        flt, speed, desc = EFFECTS[text]
        return text, flt, speed, desc
    m = re.fullmatch(r"(?:speed\s*)?(\d+(?:\.\d+)?)\s*x?", text)
    if m:
        x = float(m.group(1))
        if not SPEED_MIN <= x <= SPEED_MAX:
            raise ValueError(f"speed must be between {SPEED_MIN} and {SPEED_MAX}")
        if abs(x - 1.0) < 1e-9:
            return "off", "", 1.0, "no effect"
        return f"speed:{x:g}", f"atempo={x:g}", x, f"speed x{x:g}"
    raise ValueError(f"unknown effect '{text}'. Options: " + ", ".join(EFFECTS) + ", or `speed 1.25`")


def effect_by_key(key):
    """Re-create (filter, speed) from a stored effect key ('slowed', 'speed:1.25', ...)."""
    try:
        _, flt, speed, _ = resolve_effect(key.replace("speed:", "speed "))
        return flt, speed
    except Exception:
        return "", 1.0


# ---------------------------------------------------------------------------
# Sleep timer
# ---------------------------------------------------------------------------

def parse_sleep(text):
    """'30' (minutes) | '45m' | '1h' | '1h30m' | '90s' -> seconds; 'end' -> 'end'; 'off' -> None."""
    t = (text or "").strip().lower().replace(" ", "")
    if t in ("off", "cancel", "stop", "none"):
        return None
    if t in ("end", "song", "track"):
        return "end"
    if re.fullmatch(r"\d+", t):
        return int(t) * 60
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", t)
    if m and any(m.groups()):
        h, mi, s = (int(g or 0) for g in m.groups())
        total = h * 3600 + mi * 60 + s
        if total > 0:
            return total
    raise ValueError("use something like `30`, `45m`, `1h30m`, `end` (after this song) or `off`")


def fmt_span(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s" if s else f"{m}m"
    return f"{s}s"


# ---------------------------------------------------------------------------
# Tiny JSON store (stats, history, favourites)
# ---------------------------------------------------------------------------

class JsonStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = {}
        self.load()

    def load(self):
        with self.lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.data = data if isinstance(data, dict) else {}
            except FileNotFoundError:
                self.data = {}
            except Exception as e:
                print(f"⚠️ Could not read {self.path.name}: {e}")
                self.data = {}

    def save(self):
        with self.lock:
            tmp = self.path.with_name("." + self.path.name + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False)
                for attempt in range(6):
                    try:
                        os.replace(tmp, self.path)
                        return
                    except PermissionError:
                        time.sleep(0.02 * (attempt + 1))
                os.replace(tmp, self.path)
            except Exception as e:
                print(f"⚠️ Could not save {self.path.name}: {e}")

    # helpers returning (creating) sub-dicts
    def guild(self, guild_id):
        return self.data.setdefault("guilds", {}).setdefault(str(guild_id), {})

    def user(self, user_id):
        return self.data.setdefault("users", {}).setdefault(str(user_id), {})


# ---------------------------------------------------------------------------
# Stats + history
# ---------------------------------------------------------------------------

MIN_COUNTED_SECONDS = 15
PLAY_CAP = 3000


def record_play(store, guild_id, title, video_id=None, requester=None, seconds=0.0, ts=None):
    """Log a finished/stopped play. Plays shorter than MIN_COUNTED_SECONDS (accidental) are ignored."""
    if not title or seconds < MIN_COUNTED_SECONDS:
        return False
    with store.lock:
        plays = store.guild(guild_id).setdefault("plays", [])
        plays.append({"t": int(ts or time.time()), "title": str(title)[:200], "vid": video_id,
                      "by": (str(requester)[:40] if requester else None), "sec": int(seconds)})
        if len(plays) > PLAY_CAP:
            del plays[: len(plays) - PLAY_CAP]
        store.save()
    return True


def recent_plays(store, guild_id, n=10):
    """Newest first, collapsing immediate repeats of the same song."""
    with store.lock:
        plays = list(store.guild(guild_id).get("plays", []))
    out = []
    for p in reversed(plays):
        if out and out[-1]["title"] == p["title"]:
            continue
        out.append(p)
        if len(out) >= n:
            break
    return out


def recent_video_ids(store, guild_id, n=30):
    with store.lock:
        plays = store.guild(guild_id).get("plays", [])
        return [p.get("vid") for p in plays[-n:] if p.get("vid")]


def summarize(store, guild_id, since_ts=0, top=5):
    with store.lock:
        plays = [p for p in store.guild(guild_id).get("plays", []) if p.get("t", 0) >= since_ts]
    songs, people = {}, {}
    seconds = 0
    for p in plays:
        songs[p["title"]] = songs.get(p["title"], 0) + 1
        if p.get("by"):
            people[p["by"]] = people.get(p["by"], 0) + 1
        seconds += p.get("sec", 0)
    rank = lambda d: sorted(d.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:top]
    return {"tracks": len(plays), "seconds": seconds, "top_songs": rank(songs), "top_people": rank(people)}


def last_recap(store, guild_id):
    with store.lock:
        return store.guild(guild_id).get("last_recap", 0)


def mark_recap(store, guild_id, ts=None):
    with store.lock:
        store.guild(guild_id)["last_recap"] = int(ts or time.time())
        store.save()


# ---------------------------------------------------------------------------
# Favourites — always scoped to one Discord guild (never shared across servers)
# ---------------------------------------------------------------------------

MAX_LIKES = 200


def get_likes(store, guild_id, user_id):
    """Favourites of this user in this server only."""
    import arken_tenancy as tenancy

    with store.lock:
        return list(tenancy.likes_list(store.data, guild_id, user_id))


def add_like(store, guild_id, user_id, title, video_id=None):
    """False if it's already in this server's list for this user."""
    import arken_tenancy as tenancy

    with store.lock:
        likes = tenancy.likes_list(store.data, guild_id, user_id, create=True)
        for item in likes:
            if (video_id and item.get("vid") == video_id) or item.get("title") == title:
                return False
        likes.append({"title": str(title)[:200], "vid": video_id, "t": int(time.time())})
        if len(likes) > MAX_LIKES:
            del likes[0]
        store.save()
    return True


def remove_like(store, guild_id, user_id, index):
    """index is 1-based as shown to the user. Returns the removed item or None."""
    import arken_tenancy as tenancy

    with store.lock:
        likes = tenancy.likes_list(store.data, guild_id, user_id, create=True)
        if 1 <= index <= len(likes):
            item = likes.pop(index - 1)
            store.save()
            return item
    return None


# ---------------------------------------------------------------------------
# Titles, lyrics and the guessing game
# ---------------------------------------------------------------------------

_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]|【[^】]*】|\{[^}]*\}")
_NOISE_WORDS = re.compile(
    r"\b(official|music|lyric|lyrics|video|audio|visuali[sz]er|hd|hq|4k|remaster(?:ed)?|"
    r"slowed|reverb|\+|explicit|clean|full\s+song|topic)\b", re.I)
_FEAT = re.compile(r"\b(?:feat|ft|featuring)\b\.?.*$", re.I)
_DASHES = re.compile(r"\s+[-–—|:]\s+")


def _fold(text):
    text = unicodedata.normalize("NFKD", str(text))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _norm(text):
    text = _fold(text).lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9\u00c0-\uffff ]+", " ", text)
    return " ".join(text.split())


def split_title(title):
    """'Artist - Song (Official Video)' -> ('Artist', 'Song'); no separator -> ('', cleaned title)."""
    t = _FEAT.sub("", _NOISE_WORDS.sub(" ", _BRACKETS.sub(" ", str(title))))
    t = re.sub(r"(?:^|\s)\+(?=\s|$)", " ", t)  # a stray '+' left over from 'Slowed + Reverb'
    t = " ".join(t.split())
    parts = [x.strip(" -–—|:") for x in _DASHES.split(t, maxsplit=1)]
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return "", " ".join(x for x in parts if x)


def guess_matches(guess, title):
    """Does a chat message name this song? Accepts the song part, the whole title, small typos, or a
    sentence that contains the song name. An artist-only guess does not count."""
    g = _norm(guess)
    if len(g) < 2:
        return False
    artist, song = split_title(title)
    candidates = {c for c in (_norm(song), _norm(f"{artist} {song}")) if c}
    for c in candidates:
        if g == c:
            return True
        if len(c) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", g):
            return True
        if len(g) >= 4 and len(c) >= 4:
            if g in c and len(g) >= 0.75 * len(c):
                return True
            if difflib.SequenceMatcher(None, g, c).ratio() >= 0.82:
                return True
    return False


def lyric_query(title):
    artist, song = split_title(title)
    return " ".join(x for x in (artist, song) if x)[:120]


_LRC = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(text):
    """'[01:23.45] words' lines -> sorted [(seconds, words)] (empty lines kept as instrumental gaps)."""
    out = []
    for raw in (text or "").splitlines():
        stamps = _LRC.findall(raw)
        if not stamps:
            continue
        words = _LRC.sub("", raw).strip()
        for mm, ss in stamps:
            out.append((int(mm) * 60 + float(ss), words))
    out.sort(key=lambda x: x[0])
    return out


def lyric_at(lines, t):
    """The line being sung at time t: (index, text) or (None, '') before the first line."""
    if not lines:
        return None, ""
    i = bisect.bisect_right([x[0] for x in lines], t) - 1
    if i < 0:
        return None, ""
    return i, lines[i][1]


def pick_lyrics(results, duration=0):
    """Best LRCLIB search hit: synced lyrics first, closest duration (within 8 s when we know it)."""
    best, best_key = None, None
    for r in results or []:
        synced = (r.get("syncedLyrics") or "").strip()
        plain = (r.get("plainLyrics") or "").strip()
        if not synced and not plain:
            continue
        diff = abs((r.get("duration") or 0) - duration) if duration and r.get("duration") else 0
        if duration and r.get("duration") and diff > 8:
            continue
        key = (0 if synced else 1, diff)
        if best_key is None or key < best_key:
            best, best_key = r, key
    return best


# ---------------------------------------------------------------------------
# Voting, fades, autoplay
# ---------------------------------------------------------------------------

def votes_needed(listeners):
    """Majority of the people listening (the bot doesn't count). 0-1 listeners -> 1 vote."""
    return max(1, listeners // 2 + 1)


def fade_gain(played, remaining, fade):
    """Volume multiplier 0..1: ramps up over the first `fade` seconds and down over the last `fade`."""
    if not fade or fade <= 0:
        return 1.0
    g = 1.0
    if played is not None:
        g = min(g, played / fade)
    if remaining is not None:
        g = min(g, remaining / fade)
    return max(0.0, min(1.0, g))


def pick_related(entries, recent_ids, current_id=None):
    """First suggested track that wasn't played recently (entries: [{'id', 'title', ...}])."""
    seen = set(recent_ids or ())
    if current_id:
        seen.add(current_id)
    for e in entries or []:
        if e.get("id") and e["id"] not in seen:
            return e
    return None
