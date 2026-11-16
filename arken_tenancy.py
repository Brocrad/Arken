"""
Arken multi-server foundation: isolation of every personalization surface by Discord guild.

Pure logic (no discord import) so it can be unit-tested. Built outward: this module is the
foundation; streaming / extras / audio_processor call into it. Higher layers (permissions wiring,
admin API, Grafana) come later.

  StaffDirectory / roles         whitelisted staff with elevation tiers (owner/operator)
  can_control / presence_title   permission rule + safe bot profile status
  GuildRegistry                  which servers may use the bot (approved | pending | revoked)
  TokenPool                      sticky guild→token (+ city·callsign seat) across a bot-token pool
  likes_*                        favourites live under guilds[<id>]["likes"][<user id>] only
  playlist_dir / scope_*         playlists/<guild id>/… only
  migrate_*                      one-off moves of the old global layout into the home guild
  purge_guild_data               delete one server's stored personalization
  run_data_migration             startup entry point for likes + playlists

Isolation rule: anything a Discord server can see is addressed by guild id. Nothing reads another
guild's entry. Ops-only views (dashboard / Grafana) may aggregate later; Discord-facing code may not.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import threading
import time
from pathlib import Path

# Discord snowflakes are 17–20 digits today; 15+ digits = guild folder, so a legacy playlist named
# "2024" is not mistaken for one.
_GUILD_DIR_RE = re.compile(r"^\d{15,25}$")

NEUTRAL_PRESENCE = "in voice · !commands"  # never depends on what any server is playing

# Open play is ON for servers that have never set it — friends' guilds work without config.
DEFAULT_OPENPLAY = True

# --------------------------------------------------------------------------- staff roles (global elevation)

# Capabilities are the atoms. Roles are named bundles. Discord server admin / DJ / openplay are
# separate and only apply inside one guild.
CAPABILITIES = frozenset({
    "manage_staff",    # !whitelist add/remove/role changes
    "manage_guilds",   # !guilds, !allowguild, !leaveguild
    "manage_runtime",  # !reboot, !deps, !rejoin
    "diagnose",        # !diagnose, !nettest, !test, !region
    "global_control",  # playback control in every approved server (bypasses DJ/openplay)
})

# global_control (DJ in every server) is owner-only. Operators run the bot; they do not
# inherit playback power in friends' servers. Per-server DJ is openplay / dj_role / Manage Server.
STAFF_ROLES = {
    "owner": frozenset(CAPABILITIES),
    "operator": frozenset({"manage_guilds", "manage_runtime", "diagnose"}),
}
STAFF_ROLE_ORDER = ("owner", "operator")  # highest first

# Old "trusted" meant cross-server DJ; that elevation is gone. Map leftovers → operator
# so existing whitelist files still load (they can be demoted/removed manually).
_ROLE_ALIASES = {
    "admin": "owner",
    "ops": "operator",
    "trusted": "operator",
    "mod": "operator",
    "moderator": "operator",
    "dj": "operator",
}


def normalize_staff_role(role: str | None) -> str | None:
    if not role:
        return None
    r = str(role).strip().lower()
    r = _ROLE_ALIASES.get(r, r)
    return r if r in STAFF_ROLES else None


def role_capabilities(role: str | None) -> frozenset:
    key = normalize_staff_role(role)
    return STAFF_ROLES[key] if key else frozenset()


def has_capability(role: str | None, capability: str) -> bool:
    return capability in role_capabilities(role)


def can_control(
    *,
    global_control: bool = False,
    is_owner: bool = False,  # legacy alias → treated as global_control
    is_admin: bool = False,
    has_dj_role: bool = False,
    open_play: bool = False,
) -> bool:
    """May this person run playback-affecting (DJ) commands in this server?"""
    return bool(global_control or is_owner or is_admin or has_dj_role or open_play)


def can_server_admin(*, is_staff_owner: bool, is_guild_admin: bool) -> bool:
    """May this person change this server's Arken policy (!openplay, !djrole)?"""
    return bool(is_staff_owner or is_guild_admin)


class StaffDirectory:
    """Global staff list with roles. Persists user_whitelist.json (v2) while keeping authorized_users
    in sync for older tools (sync_state, etc.)."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.users: dict[str, dict] = {}  # uid -> {"role": "owner"|"operator"}
        self.load()

    def load(self) -> None:
        with self.lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except FileNotFoundError:
                self.users = {}
                return
            except Exception as e:
                print(f"⚠️ Could not read {self.path.name}: {e}")
                self.users = {}
                return
            self.users = {}
            if not isinstance(raw, dict):
                return
            # v2: users{id: {role}}
            users = raw.get("users")
            if isinstance(users, dict):
                for uid, meta in users.items():
                    if not str(uid).isdigit():
                        continue
                    if isinstance(meta, dict):
                        role = normalize_staff_role(meta.get("role")) or "owner"
                    elif isinstance(meta, str):
                        role = normalize_staff_role(meta) or "owner"
                    else:
                        role = "owner"
                    self.users[str(uid)] = {"role": role}
            # v1: authorized_users [id, ...] → owner
            for uid in raw.get("authorized_users") or []:
                if str(uid).isdigit() and str(uid) not in self.users:
                    self.users[str(uid)] = {"role": "owner"}

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Keep authorized_users for sync_state / older readers (everyone on the staff list).
            payload = {
                "version": 2,
                "users": {uid: {"role": meta["role"]} for uid, meta in sorted(self.users.items())},
                "authorized_users": [int(uid) for uid in sorted(self.users, key=int)],
                "last_updated": time.time(),
            }
            tmp = self.path.with_name("." + self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    def role_of(self, user_id) -> str | None:
        with self.lock:
            meta = self.users.get(str(user_id))
            return meta.get("role") if meta else None

    def is_staff(self, user_id) -> bool:
        return self.role_of(user_id) is not None

    def has(self, user_id, capability: str) -> bool:
        return has_capability(self.role_of(user_id), capability)

    def all(self) -> dict[str, dict]:
        with self.lock:
            return {k: dict(v) for k, v in self.users.items()}

    def ids(self) -> set[int]:
        with self.lock:
            return {int(uid) for uid in self.users}

    def set_role(self, user_id, role: str) -> str:
        """Add or change a staff member. Returns the normalized role. Raises ValueError if bad role."""
        norm = normalize_staff_role(role)
        if not norm:
            raise ValueError(f"unknown role '{role}'. Use: " + ", ".join(STAFF_ROLE_ORDER))
        with self.lock:
            self.users[str(int(user_id))] = {"role": norm}
            self.save()
        return norm

    def remove(self, user_id) -> bool:
        with self.lock:
            if str(user_id) not in self.users:
                return False
            del self.users[str(user_id)]
            self.save()
            return True


# --------------------------------------------------------------------------- bot status

def presence_title(guild_count: int, titles: list) -> str | None:
    """Song title for 'Listening to …', or None for the neutral status.

    A profile status is global on the Discord account. Only show a title when the bot is in
    exactly one server; otherwise any title would leak across servers.
    """
    if guild_count == 1 and titles:
        return str(titles[0])[:120]
    return None


# --------------------------------------------------------------------------- server registry

class GuildRegistry:
    """Servers allowed to use the bot. Status: approved | pending | revoked."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.guilds: dict[str, dict] = {}
        self.initialized = False  # False until the file has existed once (first run)
        self.load()

    def load(self) -> None:
        with self.lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self.guilds = dict(raw.get("guilds", {})) if isinstance(raw, dict) else {}
                self.initialized = True
            except FileNotFoundError:
                self.guilds, self.initialized = {}, False
            except Exception as e:
                print(f"⚠️ Could not read {self.path.name}: {e}")
                try:
                    shutil.copy2(self.path, self.path.with_name(self.path.name + ".corrupt"))
                except OSError:
                    pass
                self.guilds, self.initialized = {}, True

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name("." + self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "guilds": self.guilds}, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
            self.initialized = True

    def status(self, guild_id) -> str:
        with self.lock:
            return self.guilds.get(str(guild_id), {}).get("status", "unknown")

    def is_approved(self, guild_id) -> bool:
        return self.status(guild_id) == "approved"

    def all(self) -> dict[str, dict]:
        with self.lock:
            return {k: dict(v) for k, v in self.guilds.items()}

    def seed(self, guilds: list) -> int:
        """First run only: approve every server the bot is already in."""
        with self.lock:
            if self.initialized:
                return 0
            for gid, name in guilds:
                self.guilds[str(gid)] = {
                    "status": "approved",
                    "name": name,
                    "added": time.time(),
                    "approved": time.time(),
                    "note": "existing server at first run",
                }
            self.save()
            return len(guilds)

    def seen(self, guild_id, name: str | None = None) -> str:
        """Record a server we met. Unknown ones become pending. Returns resulting status."""
        with self.lock:
            key = str(guild_id)
            entry = self.guilds.get(key)
            if entry is None:
                entry = self.guilds[key] = {
                    "status": "pending",
                    "name": name or "",
                    "added": time.time(),
                }
                self.save()
            elif name and entry.get("name") != name:
                entry["name"] = name
                self.save()
            return entry["status"]

    def approve(self, guild_id, name: str | None = None, note: str | None = None) -> None:
        with self.lock:
            e = self.guilds.setdefault(str(guild_id), {"added": time.time(), "name": name or ""})
            e.update(status="approved", approved=time.time())
            if name:
                e["name"] = name
            if note is not None:
                e["note"] = note
            self.save()

    def revoke(self, guild_id) -> bool:
        with self.lock:
            e = self.guilds.get(str(guild_id))
            if not e:
                return False
            e.update(status="revoked", revoked=time.time())
            self.save()
            return True


# --------------------------------------------------------------------------- token pool (multi-bot balancing)

# Comfortable default from capacity planning: ~5 guilds/token with ~65% assumed concurrent use.
DEFAULT_MAX_GUILDS_PER_TOKEN = 5

# Load score weights. Guild slots dominate; active players and member chatter tilt ties.
DEFAULT_WEIGHT_GUILD = 10.0
DEFAULT_WEIGHT_ACTIVE = 8.0
DEFAULT_WEIGHT_MEMBERS = 1.0  # multiplied by sum(log1p(members)) across assigned guilds

# Naming: token = city (bot), callsign = guild seat on that bot. Display: "Osgiliath · East Bank".
# Data isolation is still by Discord guild id; callsigns are sticky support labels only.
DEFAULT_TOKEN_CALLSIGNS: dict[str, tuple[str, ...]] = {
    "gondor": (
        "citadel",
        "fountain-court",
        "rath-dinen",
        "fen-hollen",
        "first-circle",
    ),
    "osgiliath": (
        "east-bank",
        "west-bank",
        "anduin-bridge",
        "dome-of-stars",
        "causeway",
    ),
    "nimloth": (
        "white-court",
        "sapling-grove",
        "stewards-house",
        "winged-crown",
        "high-hallow",
    ),
    "morthond": (
        "blackroot",
        "stone-of-erech",
        "paths-dead",
        "calembel",
        "tarlangs-neck",
    ),
}

# Fallback seats for custom token ids not in the catalog (still no digits in the label).
FALLBACK_CALLSIGNS: tuple[str, ...] = ("north", "south", "east", "west", "center")

TOKEN_DISPLAY_NAMES: dict[str, str] = {
    "gondor": "Gondor",
    "osgiliath": "Osgiliath",
    "nimloth": "Nimloth",
    "morthond": "Morthond",
}

CALLSIGN_DISPLAY_NAMES: dict[str, str] = {
    "citadel": "Citadel",
    "fountain-court": "Fountain Court",
    "rath-dinen": "Rath Dínen",
    "fen-hollen": "Fen Hollen",
    "first-circle": "First Circle",
    "east-bank": "East Bank",
    "west-bank": "West Bank",
    "anduin-bridge": "Anduin Bridge",
    "dome-of-stars": "Dome of Stars",
    "causeway": "Causeway",
    "white-court": "White Court",
    "sapling-grove": "Sapling Grove",
    "stewards-house": "Stewards' House",
    "winged-crown": "Winged Crown",
    "high-hallow": "High Hallow",
    "blackroot": "Blackroot",
    "stone-of-erech": "Stone of Erech",
    "paths-dead": "Paths of the Dead",
    "calembel": "Calembel",
    "tarlangs-neck": "Tarlang's Neck",
    "north": "North",
    "south": "South",
    "east": "East",
    "west": "West",
    "center": "Center",
}


class PoolFullError(Exception):
    """Every token in the pool is at its guild cap."""


class CallsignExhaustedError(Exception):
    """A token has no free callsign left in its catalog (should match guild cap)."""


def display_token_name(token_id: str | None) -> str:
    if not token_id:
        return "?"
    tid = str(token_id)
    return TOKEN_DISPLAY_NAMES.get(tid, tid.replace("-", " ").title())


def display_callsign_name(callsign: str | None) -> str:
    if not callsign:
        return "?"
    cs = str(callsign)
    return CALLSIGN_DISPLAY_NAMES.get(cs, cs.replace("-", " ").title())


def seat_label(token_id: str | None, callsign: str | None = None) -> str | None:
    """Human seat id, e.g. 'Osgiliath · East Bank'. Token-only if callsign missing."""
    if not token_id:
        return None
    city = display_token_name(token_id)
    if not callsign:
        return city
    return f"{city} · {display_callsign_name(callsign)}"


def callsigns_for_token(token_id: str, *, max_guilds: int = DEFAULT_MAX_GUILDS_PER_TOKEN) -> tuple[str, ...]:
    """Ordered callsign catalog for a city-token (trimmed to max guild seats)."""
    tid = str(token_id).strip().lower()
    catalog = DEFAULT_TOKEN_CALLSIGNS.get(tid) or FALLBACK_CALLSIGNS
    n = max(1, int(max_guilds))
    return tuple(catalog[:n])


def member_weight(member_count: int | float | None) -> float:
    """Compress raw member counts so one mega-server does not dominate linearly."""
    try:
        n = max(0, int(member_count or 0))
    except (TypeError, ValueError):
        n = 0
    return math.log1p(n)


def token_load_score(
    *,
    guild_count: int,
    active_players: int = 0,
    members_total: int = 0,
    member_weights_sum: float | None = None,
    weight_guild: float = DEFAULT_WEIGHT_GUILD,
    weight_active: float = DEFAULT_WEIGHT_ACTIVE,
    weight_members: float = DEFAULT_WEIGHT_MEMBERS,
) -> float:
    """Lower is better. Used by the assigner to pick the least-loaded token."""
    mw = member_weights_sum if member_weights_sum is not None else member_weight(members_total)
    return (
        float(guild_count) * weight_guild
        + float(active_players) * weight_active
        + float(mw) * weight_members
    )


class TokenPool:
    """Sticky guild → bot-token assignment across a pre-created Discord application pool.

    Tokens are never minted here — ops create apps and list their ids. This class balances which
    guilds ride which token (guild count, active players, member weight) and auto-assigns a
    unique callsign seat on that token (city · location), e.g. Osgiliath · East Bank.
    """

    def __init__(
        self,
        path,
        *,
        token_ids: list[str] | None = None,
        max_guilds_per_token: int = DEFAULT_MAX_GUILDS_PER_TOKEN,
        weight_guild: float = DEFAULT_WEIGHT_GUILD,
        weight_active: float = DEFAULT_WEIGHT_ACTIVE,
        weight_members: float = DEFAULT_WEIGHT_MEMBERS,
    ):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.token_ids: list[str] = []
        self.max_guilds_per_token = max(1, int(max_guilds_per_token))
        self.weight_guild = float(weight_guild)
        self.weight_active = float(weight_active)
        self.weight_members = float(weight_members)
        self.assignments: dict[str, dict] = {}  # guild_id -> meta
        self.runtime: dict[str, dict] = {}  # token_id -> {active_players, updated}
        self.load()
        if token_ids:
            self.set_tokens(token_ids)

    def load(self) -> None:
        with self.lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except FileNotFoundError:
                return
            except Exception as e:
                print(f"⚠️ Could not read {self.path.name}: {e}")
                return
            if not isinstance(raw, dict):
                return
            tokens = raw.get("tokens") or []
            self.token_ids = [str(t).strip() for t in tokens if str(t).strip()]
            try:
                self.max_guilds_per_token = max(1, int(raw.get("max_guilds_per_token", self.max_guilds_per_token)))
            except (TypeError, ValueError):
                pass
            asg = raw.get("assignments")
            if isinstance(asg, dict):
                self.assignments = {
                    str(gid): dict(meta) if isinstance(meta, dict) else {"token_id": str(meta)}
                    for gid, meta in asg.items()
                    if str(gid).isdigit()
                }
            rt = raw.get("runtime")
            if isinstance(rt, dict):
                self.runtime = {str(tid): dict(meta) for tid, meta in rt.items() if isinstance(meta, dict)}

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "tokens": list(self.token_ids),
                "max_guilds_per_token": self.max_guilds_per_token,
                "assignments": {k: self.assignments[k] for k in sorted(self.assignments, key=int)},
                "runtime": self.runtime,
                "updated": time.time(),
            }
            tmp = self.path.with_name("." + self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    def enabled(self) -> bool:
        return len(self.token_ids) > 0

    def set_tokens(self, token_ids: list[str]) -> None:
        """Declare the pool membership (order preserved). Persists when changed."""
        cleaned = []
        seen = set()
        for t in token_ids:
            tid = str(t).strip()
            if not tid or tid in seen:
                continue
            seen.add(tid)
            cleaned.append(tid)
        with self.lock:
            if cleaned == self.token_ids:
                return
            self.token_ids = cleaned
            self.save()

    def token_of(self, guild_id) -> str | None:
        with self.lock:
            meta = self.assignments.get(str(guild_id))
            return meta.get("token_id") if meta else None

    def callsign_of(self, guild_id) -> str | None:
        with self.lock:
            meta = self.assignments.get(str(guild_id))
            cs = meta.get("callsign") if meta else None
            return str(cs) if cs else None

    def seat_of(self, guild_id) -> str | None:
        """Support id: 'Gondor · Citadel' (sticky once assigned)."""
        with self.lock:
            meta = self.assignments.get(str(guild_id))
            if not meta:
                return None
            return seat_label(meta.get("token_id"), meta.get("callsign"))

    def assignment_of(self, guild_id) -> dict | None:
        with self.lock:
            meta = self.assignments.get(str(guild_id))
            return dict(meta) if meta else None

    def guilds_on(self, token_id: str) -> list[str]:
        tid = str(token_id)
        with self.lock:
            return [gid for gid, meta in self.assignments.items() if meta.get("token_id") == tid]

    def used_callsigns(self, token_id: str) -> set[str]:
        tid = str(token_id)
        with self.lock:
            out = set()
            for meta in self.assignments.values():
                if meta.get("token_id") == tid and meta.get("callsign"):
                    out.add(str(meta["callsign"]))
            return out

    def pick_callsign(self, token_id: str, *, preferred: str | None = None) -> str:
        """Next free callsign on this token. Raises CallsignExhaustedError if none left."""
        tid = str(token_id)
        catalog = callsigns_for_token(tid, max_guilds=self.max_guilds_per_token)
        used = self.used_callsigns(tid)
        if preferred:
            pref = str(preferred).strip().lower().replace(" ", "-")
            if pref in catalog and pref not in used:
                return pref
        for cs in catalog:
            if cs not in used:
                return cs
        # Catalog shorter than cap or all taken — synthesize unique slug (still no digits).
        for cs in FALLBACK_CALLSIGNS:
            if cs not in used and cs not in catalog:
                return cs
        raise CallsignExhaustedError(
            f"token {tid} has no free callsign "
            f"({len(used)}/{self.max_guilds_per_token} seats used)"
        )

    def _ensure_callsign_locked(self, meta: dict, token_id: str, preferred: str | None = None) -> str:
        """Attach a callsign to meta if missing. Caller holds the lock."""
        existing = meta.get("callsign")
        if existing and meta.get("token_id") == token_id:
            return str(existing)
        cs = self.pick_callsign(token_id, preferred=preferred)
        meta["callsign"] = cs
        return cs

    def _members_weight_sum(self, token_id: str) -> float:
        total = 0.0
        for gid in self.guilds_on(token_id):
            total += member_weight(self.assignments[gid].get("members"))
        return total

    def _members_total(self, token_id: str) -> int:
        total = 0
        for gid in self.guilds_on(token_id):
            try:
                total += max(0, int(self.assignments[gid].get("members") or 0))
            except (TypeError, ValueError):
                pass
        return total

    def active_players(self, token_id: str) -> int:
        with self.lock:
            try:
                return max(0, int(self.runtime.get(str(token_id), {}).get("active_players") or 0))
            except (TypeError, ValueError):
                return 0

    def score(self, token_id: str) -> float:
        with self.lock:
            tid = str(token_id)
            return token_load_score(
                guild_count=len(self.guilds_on(tid)),
                active_players=self.active_players(tid),
                members_total=self._members_total(tid),
                member_weights_sum=self._members_weight_sum(tid),
                weight_guild=self.weight_guild,
                weight_active=self.weight_active,
                weight_members=self.weight_members,
            )

    def has_slot(self, token_id: str) -> bool:
        return len(self.guilds_on(token_id)) < self.max_guilds_per_token

    def pick_token(self) -> str:
        """Least-loaded token with a free guild slot. Raises PoolFullError if none."""
        with self.lock:
            if not self.token_ids:
                raise PoolFullError("token pool is empty — set ARKEN_TOKEN_POOL")
            candidates = [tid for tid in self.token_ids if self.has_slot(tid)]
            if not candidates:
                raise PoolFullError(
                    f"all {len(self.token_ids)} token(s) at cap "
                    f"({self.max_guilds_per_token} guilds each)"
                )
            # Stable tie-break: lowest score, then pool order.
            return min(candidates, key=lambda tid: (self.score(tid), self.token_ids.index(tid)))

    def assign(
        self,
        guild_id,
        *,
        token_id: str | None = None,
        callsign: str | None = None,
        members: int | None = None,
        name: str | None = None,
        force: bool = False,
        source: str | None = None,
    ) -> str:
        """Sticky assign token + callsign. Existing seat kept unless force=True. Returns token_id."""
        key = str(int(guild_id))
        with self.lock:
            existing = self.assignments.get(key)
            if existing and not force:
                dirty = False
                if members is not None:
                    existing["members"] = max(0, int(members))
                    dirty = True
                if name:
                    existing["name"] = name
                    dirty = True
                tid = existing["token_id"]
                if not existing.get("callsign"):
                    self._ensure_callsign_locked(existing, tid, preferred=callsign)
                    dirty = True
                if source and not existing.get("source"):
                    existing["source"] = str(source)
                    dirty = True
                if dirty:
                    self.save()
                return tid

            tid = str(token_id) if token_id else self.pick_token()
            if tid not in self.token_ids:
                raise ValueError(f"unknown token_id '{tid}'")
            if not force and not self.has_slot(tid):
                # Re-pick if a specific full token was requested without force.
                if token_id:
                    raise PoolFullError(f"token {tid} is at guild cap ({self.max_guilds_per_token})")
                tid = self.pick_token()

            # When forcing onto a full token, still allow (manual ops); otherwise require a slot.
            if not self.has_slot(tid) and (existing is None or existing.get("token_id") != tid):
                if not force:
                    raise PoolFullError(f"token {tid} is at guild cap ({self.max_guilds_per_token})")

            # Free the old callsign if moving tokens under force.
            keep_cs = None
            if existing and existing.get("token_id") == tid and existing.get("callsign"):
                keep_cs = existing.get("callsign")

            meta = {
                "token_id": tid,
                "assigned_at": time.time(),
                "members": max(0, int(members or (existing or {}).get("members") or 0)),
            }
            if source:
                meta["source"] = str(source)
            elif existing and existing.get("source"):
                meta["source"] = existing["source"]
            if name or (existing or {}).get("name"):
                meta["name"] = name or existing.get("name")
            # Temporarily drop this guild from assignments so pick_callsign doesn't see its old seat.
            self.assignments.pop(key, None)
            try:
                meta["callsign"] = self.pick_callsign(
                    tid, preferred=callsign or keep_cs
                )
            except CallsignExhaustedError:
                # Restore previous row if we had one, then re-raise.
                if existing:
                    self.assignments[key] = existing
                raise
            self.assignments[key] = meta
            self.save()
            return tid

    def release(self, guild_id) -> bool:
        with self.lock:
            key = str(guild_id)
            if key not in self.assignments:
                return False
            del self.assignments[key]
            self.save()
            return True

    def update_guild_members(
        self, guild_id, members: int, name: str | None = None, *, persist: bool = True
    ) -> bool:
        """Update cached member count. Returns True if something changed."""
        with self.lock:
            meta = self.assignments.get(str(guild_id))
            if not meta:
                return False
            new_members = max(0, int(members))
            dirty = meta.get("members") != new_members
            meta["members"] = new_members
            if name and meta.get("name") != name:
                meta["name"] = name
                dirty = True
            if dirty and persist:
                self.save()
            return dirty

    def set_runtime(self, token_id: str, *, active_players: int, persist: bool = True) -> None:
        with self.lock:
            self.runtime[str(token_id)] = {
                "active_players": max(0, int(active_players)),
                "updated": time.time(),
            }
            if persist:
                self.save()

    def touch(self) -> None:
        """Persist current in-memory pool state (after batched runtime updates)."""
        with self.lock:
            self.save()

    def snapshot(self) -> dict:
        """Ops / Prometheus view of pool balance."""
        with self.lock:
            tokens = []
            for tid in self.token_ids:
                gids = self.guilds_on(tid)
                members = self._members_total(tid)
                active = self.active_players(tid)
                seats = []
                for gid in gids:
                    meta = self.assignments[gid]
                    seats.append({
                        "guild_id": gid,
                        "callsign": meta.get("callsign"),
                        "seat": seat_label(tid, meta.get("callsign")),
                        "name": meta.get("name") or "",
                        "members": int(meta.get("members") or 0),
                    })
                catalog = callsigns_for_token(tid, max_guilds=self.max_guilds_per_token)
                used = self.used_callsigns(tid)
                tokens.append({
                    "token_id": tid,
                    "display_name": display_token_name(tid),
                    "guilds": len(gids),
                    "guild_ids": gids,
                    "seats": seats,
                    "callsigns_free": [cs for cs in catalog if cs not in used],
                    "members_total": members,
                    "member_weight": round(self._members_weight_sum(tid), 4),
                    "active_players": active,
                    "active_ratio": round(active / len(gids), 4) if gids else 0.0,
                    "slots_free": max(0, self.max_guilds_per_token - len(gids)),
                    "load_score": round(self.score(tid), 4),
                    "at_cap": len(gids) >= self.max_guilds_per_token,
                })
            return {
                "enabled": self.enabled(),
                "max_guilds_per_token": self.max_guilds_per_token,
                "token_count": len(self.token_ids),
                "assigned_guilds": len(self.assignments),
                "tokens": tokens,
            }


def parse_token_pool_env(value: str | None) -> list[str]:
    """Parse ARKEN_TOKEN_POOL=t0,t1,t2 (comma/space separated)."""
    if not value:
        return []
    parts = []
    for chunk in value.replace(";", ",").split(","):
        for bit in chunk.split():
            tid = bit.strip()
            if tid:
                parts.append(tid)
    return parts


# --------------------------------------------------------------------------- likes (per server)

def likes_list(data: dict, guild_id, user_id, *, create: bool = False) -> list:
    """Favourites of one user in one server. Empty list if none (unless create=True)."""
    if create:
        guild = data.setdefault("guilds", {}).setdefault(str(guild_id), {})
        by_user = guild.setdefault("likes", {})
        return by_user.setdefault(str(user_id), [])
    guild = data.get("guilds", {}).get(str(guild_id), {})
    return list(guild.get("likes", {}).get(str(user_id), []))


def guild_likes_pool(data: dict, guild_id) -> list:
    """Every favourite saved in THIS server (guessing game). Never other servers."""
    out = []
    for items in data.get("guilds", {}).get(str(guild_id), {}).get("likes", {}).values():
        out.extend(items)
    return out


def migrate_likes(data: dict, home_guild_id) -> int:
    """Old layout: users[<uid>]["likes"] shared across servers → home guild only.

    Idempotent. Returns how many favourites were moved.
    """
    users = data.get("users")
    if not isinstance(users, dict) or home_guild_id is None:
        return 0
    moved = 0
    for uid in list(users.keys()):
        entry = users[uid]
        likes = entry.get("likes") if isinstance(entry, dict) else None
        if likes:
            dest = likes_list(data, home_guild_id, uid, create=True)
            have = {(i.get("vid") or i.get("title")) for i in dest}
            for item in likes:
                key = item.get("vid") or item.get("title")
                if key not in have:
                    dest.append(item)
                    have.add(key)
                    moved += 1
        if isinstance(entry, dict):
            entry.pop("likes", None)
            if not entry:
                del users[uid]
    if not users:
        data.pop("users", None)
    return moved


def resolve_home_guild(env_value: str | None, historical_ids) -> int | None:
    """Which server owns pre-multi-server data (global likes, playlists)?

    ARKEN_HOME_GUILD wins. Else the single guild that already has history/settings.
    None when ambiguous or there is nothing to migrate.
    """
    env_value = (env_value or "").strip()
    if env_value.isdigit():
        return int(env_value)
    ids = {int(i) for i in historical_ids if str(i).isdigit()}
    return next(iter(ids)) if len(ids) == 1 else None


def historical_guild_ids(data: dict, settings: dict | None = None) -> set:
    """Guild ids that already have stored personalization (for home-guild resolution)."""
    ids = set()
    for source in (data.get("guilds", {}), settings or {}):
        for k in source:
            if str(k).isdigit():
                ids.add(str(k))
    return ids


# --------------------------------------------------------------------------- playlists (per server)

def playlist_root(path=None) -> Path:
    return Path(path or "playlists")


def playlist_dir(root, guild_id, *, create: bool = True) -> Path:
    """playlists/<guild id>/ — the only place a server's playlists live."""
    p = Path(root) / str(int(guild_id))
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def is_guild_playlist_path(path_str: str, guild_id=None) -> bool:
    """True if path is under playlists/ (optionally under a specific guild id)."""
    s = str(path_str or "").replace("\\", "/")
    if not s.startswith("playlists/") and "/playlists/" not in s:
        # bare relative "playlists\..." already normalised above; also accept startswith playlists
        if not s.startswith("playlists"):
            return False
    if guild_id is None:
        return True
    needle = f"playlists/{int(guild_id)}/"
    return needle in s or s.startswith(needle)


_PLAYLISTS_AT = re.compile(r"^(?:[A-Za-z]:[\\/].*?[\\/])?(playlists[\\/].*)$", re.S)


def scope_playlist_path(path_str: str, guild_id, root_name: str = "playlists") -> str:
    """Normalise to playlists/<gid>/… (forward slashes). Idempotent."""
    m = _PLAYLISTS_AT.match(path_str or "")
    if not m:
        return path_str
    rel = m.group(1).replace("\\", "/")
    parts = rel.split("/")
    gid = str(guild_id)
    if len(parts) > 1 and parts[1] == gid:
        return rel
    return "/".join([root_name, gid, *parts[1:]])


def fix_playlist_paths(gdir: Path, guild_id) -> int:
    """Rewrite audio_file / metadata_file inside playlist JSON under gdir. Returns files changed."""
    changed = 0
    for pf in gdir.glob("*.json"):
        try:
            data = json.loads(pf.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            continue
        dirty = False
        for song in data["files"]:
            if not isinstance(song, dict):
                continue
            for key in ("audio_file", "metadata_file"):
                v = song.get(key)
                if isinstance(v, str):
                    nv = scope_playlist_path(v, guild_id)
                    if nv != v:
                        song[key] = nv
                        dirty = True
        if dirty:
            tmp = pf.with_name("." + pf.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, pf)
            changed += 1
    return changed


def migrate_playlists(root, home_guild_id) -> dict:
    """Move legacy playlists/* into playlists/<home guild id>/. Idempotent."""
    root = Path(root)
    result = {"moved": 0, "conflicts": [], "paths_fixed": 0}
    if not root.is_dir():
        return result
    legacy = [
        p
        for p in root.iterdir()
        if not p.name.startswith(".") and not (p.is_dir() and _GUILD_DIR_RE.match(p.name))
    ]
    if legacy and home_guild_id:
        dest = playlist_dir(root, home_guild_id)
        log = {}
        for item in sorted(legacy, key=lambda p: (p.is_dir(), p.name)):
            target = dest / item.name
            if target.exists():
                result["conflicts"].append(item.name)
                continue
            shutil.move(str(item), str(target))
            log[str(item.relative_to(root)).replace("\\", "/")] = str(
                target.relative_to(root)
            ).replace("\\", "/")
            result["moved"] += 1
        if log:
            (root / f".tenancy-migration-{int(time.time())}.json").write_text(
                json.dumps({"home_guild": str(home_guild_id), "moved": log}, indent=2),
                encoding="utf-8",
            )
    if root.is_dir():
        for gdir in root.iterdir():
            if gdir.is_dir() and _GUILD_DIR_RE.match(gdir.name):
                result["paths_fixed"] += fix_playlist_paths(gdir, gdir.name)
    return result


# --------------------------------------------------------------------------- tenancy removal + startup

def purge_guild_data(data: dict, playlists_root, guild_id) -> dict:
    """Forget everything stored for one server. Returns what was removed."""
    removed = {"data": False, "playlists_dir": False}
    guilds = data.get("guilds", {})
    if str(guild_id) in guilds:
        del guilds[str(guild_id)]
        removed["data"] = True
    gdir = playlist_dir(playlists_root, guild_id, create=False)
    if gdir.is_dir():
        shutil.rmtree(gdir)
        removed["playlists_dir"] = True
    return removed


def run_data_migration(
    data: dict,
    *,
    settings: dict | None = None,
    playlists_root="playlists",
    home_guild_env: str | None = None,
) -> dict:
    """Startup entry: resolve home guild, migrate likes + playlists. Safe to call every boot."""
    home = resolve_home_guild(home_guild_env, historical_guild_ids(data, settings))
    report = {"home_guild": home, "likes_moved": 0, "playlists": None, "skipped": False}
    if home is None:
        # Still fix paths in any already-namespaced playlist folders.
        report["playlists"] = migrate_playlists(playlists_root, None)
        if data.get("users"):
            report["skipped"] = True
            print(
                "⚠️ Tenancy: global likes still present but home guild is ambiguous. "
                "Set ARKEN_HOME_GUILD=<discord server id> once, then restart."
            )
        return report
    report["likes_moved"] = migrate_likes(data, home)
    report["playlists"] = migrate_playlists(playlists_root, home)
    return report
