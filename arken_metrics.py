"""
Lightweight Prometheus textfile metrics for Arken (Grafana/Prometheus later).

No extra dependencies. Each token process can write a snapshot under
ARKEN_STATE_DIR/metrics/arken_<token_id>.prom for node_exporter's textfile
collector, or any scraper that reads .prom files.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path


def _esc_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_pool_prometheus(
    snapshot: dict,
    *,
    token_id: str | None = None,
    heartbeat_ages: dict[str, float] | None = None,
) -> str:
    """Render TokenPool.snapshot() as Prometheus exposition text."""
    lines = [
        "# HELP arken_pool_max_guilds_per_token Soft guild cap per bot token.",
        "# TYPE arken_pool_max_guilds_per_token gauge",
        f"arken_pool_max_guilds_per_token {int(snapshot.get('max_guilds_per_token') or 0)}",
        "# HELP arken_pool_token_count Tokens declared in the pool.",
        "# TYPE arken_pool_token_count gauge",
        f"arken_pool_token_count {int(snapshot.get('token_count') or 0)}",
        "# HELP arken_pool_assigned_guilds Guilds with a sticky token assignment.",
        "# TYPE arken_pool_assigned_guilds gauge",
        f"arken_pool_assigned_guilds {int(snapshot.get('assigned_guilds') or 0)}",
        "# HELP arken_token_guilds Guilds assigned to a token.",
        "# TYPE arken_token_guilds gauge",
        "# HELP arken_token_members_total Approximate member sum for guilds on a token.",
        "# TYPE arken_token_members_total gauge",
        "# HELP arken_token_member_weight Sum of log1p(members) used in load scoring.",
        "# TYPE arken_token_member_weight gauge",
        "# HELP arken_token_active_players Guilds currently playing on a token.",
        "# TYPE arken_token_active_players gauge",
        "# HELP arken_token_active_ratio active_players / guilds (0 if empty).",
        "# TYPE arken_token_active_ratio gauge",
        "# HELP arken_token_slots_free Free guild slots under the cap.",
        "# TYPE arken_token_slots_free gauge",
        "# HELP arken_token_load_score Assigner load score (lower is better).",
        "# TYPE arken_token_load_score gauge",
        "# HELP arken_token_at_cap 1 if this token is at its guild cap.",
        "# TYPE arken_token_at_cap gauge",
        "# HELP arken_token_alive 1 if container heartbeat is fresh (or this process serves that token).",
        "# TYPE arken_token_alive gauge",
        "# HELP arken_token_heartbeat_age_seconds Seconds since last container heartbeat file update.",
        "# TYPE arken_token_heartbeat_age_seconds gauge",
    ]
    ages = heartbeat_ages or {}
    for tok in snapshot.get("tokens") or []:
        tid = _esc_label(tok.get("token_id", ""))
        raw_tid = str(tok.get("token_id", ""))
        labels = f'token_id="{tid}"'
        lines.append(f'arken_token_guilds{{{labels}}} {int(tok.get("guilds") or 0)}')
        lines.append(f'arken_token_members_total{{{labels}}} {int(tok.get("members_total") or 0)}')
        lines.append(f'arken_token_member_weight{{{labels}}} {float(tok.get("member_weight") or 0)}')
        lines.append(f'arken_token_active_players{{{labels}}} {int(tok.get("active_players") or 0)}')
        lines.append(f'arken_token_active_ratio{{{labels}}} {float(tok.get("active_ratio") or 0)}')
        lines.append(f'arken_token_slots_free{{{labels}}} {int(tok.get("slots_free") or 0)}')
        lines.append(f'arken_token_load_score{{{labels}}} {float(tok.get("load_score") or 0)}')
        lines.append(f'arken_token_at_cap{{{labels}}} {1 if tok.get("at_cap") else 0}')
        alive = tok.get("alive")
        if alive is not None:
            lines.append(f"arken_token_alive{{{labels}}} {1 if alive else 0}")
        if raw_tid in ages and ages[raw_tid] is not None:
            lines.append(
                f'arken_token_heartbeat_age_seconds{{{labels}}} {float(ages[raw_tid]):.3f}'
            )
    if token_id:
        lines.append("# HELP arken_process_token_info Which token_id this process serves.")
        lines.append("# TYPE arken_process_token_info gauge")
        lines.append(f'arken_process_token_info{{token_id="{_esc_label(token_id)}"}} 1')
    lines.append(f"# generated {int(time.time())}")
    return "\n".join(lines) + "\n"


def render_process_prometheus(
    *,
    token_id: str,
    guilds_served: int,
    active_players: int,
    voice_connected: int,
    heartbeat_age: float | None,
) -> str:
    """Per-container textfile (no pool-wide duplicate series). Scraped alongside arken_pool.prom."""
    tid = _esc_label(token_id)
    lines = [
        "# HELP arken_process_guilds_served Guilds this process is responsible for.",
        "# TYPE arken_process_guilds_served gauge",
        f'arken_process_guilds_served{{token_id="{tid}"}} {int(guilds_served)}',
        "# HELP arken_process_active_players Local concurrent playback count.",
        "# TYPE arken_process_active_players gauge",
        f'arken_process_active_players{{token_id="{tid}"}} {int(active_players)}',
        "# HELP arken_process_voice_connected Guilds where this process has a voice connection.",
        "# TYPE arken_process_voice_connected gauge",
        f'arken_process_voice_connected{{token_id="{tid}"}} {int(voice_connected)}',
        "# HELP arken_process_info 1 while this container is exporting metrics.",
        "# TYPE arken_process_info gauge",
        f'arken_process_info{{token_id="{tid}"}} 1',
    ]
    if heartbeat_age is not None:
        lines.extend([
            "# HELP arken_process_heartbeat_age_seconds Local heartbeat file age.",
            "# TYPE arken_process_heartbeat_age_seconds gauge",
            f'arken_process_heartbeat_age_seconds{{token_id="{tid}"}} {float(heartbeat_age):.3f}',
        ])
    lines.append(f"# generated {int(time.time())}")
    return "\n".join(lines) + "\n"


class MetricsWriter:
    """Atomic writer for a Prometheus textfile; safe to call from a background loop."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def write(self, text: str) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name("." + self.path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)


def default_metrics_path(state_dir: str | Path, token_id: str | None) -> Path:
    base = Path(state_dir) / "metrics"
    name = f"arken_{token_id or 'default'}.prom"
    return base / name


def process_metrics_path(state_dir: str | Path, token_id: str) -> Path:
    return default_metrics_path(state_dir, f"process_{token_id}")