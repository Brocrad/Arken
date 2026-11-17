"""Central action tiers for Discord command authorization (no discord.py import)."""
from __future__ import annotations

from enum import Enum


class Action(str, Enum):
    """Who may invoke a command in an approved, served guild."""

    MEMBER = "member"  # any member: likes, history, summon, voteskip view, queue view
    DJ = "dj"  # playback controls: play, skip, pause, volume, playlist play, …
    SERVER_ADMIN = "server_admin"  # !openplay, !djrole, !region (per-guild)
    # Staff capabilities stay on arken_tenancy CAPABILITIES + require_staff in streaming.


MSG_NO_GUILD = "❌ This command only works inside a Discord server."
MSG_NOT_APPROVED = "❌ This server is **{status}**. Staff must run `!allowguild {gid}` before Arken will work here."
MSG_WRONG_TOKEN = (
    "❌ This server is approved but not served by this bot instance.{tip}"
)
MSG_DJ_DENIED = (
    "❌ **Access denied.** Need the DJ role, Manage Server, "
    "`!openplay on`, or be an Arken owner."
)