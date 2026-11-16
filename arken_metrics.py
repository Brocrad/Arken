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


def render_pool_prometheus(snapshot: dict, *, token_id: str | None = None) -> str:
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
    ]
    for tok in snapshot.get("tokens") or []:
        tid = _esc_label(tok.get("token_id", ""))
        labels = f'token_id="{tid}"'
        lines.append(f'arken_token_guilds{{{labels}}} {int(tok.get("guilds") or 0)}')
        lines.append(f'arken_token_members_total{{{labels}}} {int(tok.get("members_total") or 0)}')
        lines.append(f'arken_token_member_weight{{{labels}}} {float(tok.get("member_weight") or 0)}')
        lines.append(f'arken_token_active_players{{{labels}}} {int(tok.get("active_players") or 0)}')
        lines.append(f'arken_token_active_ratio{{{labels}}} {float(tok.get("active_ratio") or 0)}')
        lines.append(f'arken_token_slots_free{{{labels}}} {int(tok.get("slots_free") or 0)}')
        lines.append(f'arken_token_load_score{{{labels}}} {float(tok.get("load_score") or 0)}')
        lines.append(f'arken_token_at_cap{{{labels}}} {1 if tok.get("at_cap") else 0}')
    if token_id:
        lines.append("# HELP arken_process_token_info Which token_id this process serves.")
        lines.append("# TYPE arken_process_token_info gauge")
        lines.append(f'arken_process_token_info{{token_id="{_esc_label(token_id)}"}} 1')
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