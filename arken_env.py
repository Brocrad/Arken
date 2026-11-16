"""
Arken environment variables (Discord music fleet).

Reads ARKEN_* first, then legacy GONDOR_* (deprecated). Unrelated to Obselis.
"""
from __future__ import annotations

import os


def get(name: str, default: str | None = None) -> str:
    """Lookup ARKEN_<name> or GONDOR_<name>."""
    for prefix in ("ARKEN_", "GONDOR_"):
        raw = os.environ.get(f"{prefix}{name}")
        if raw is not None and str(raw).strip() != "":
            return raw
    if default is None:
        return ""
    return str(default)


def truthy(name: str) -> bool:
    return get(name, "").strip().lower() in ("1", "true", "yes", "on")


def flag(name: str, default: str = "1") -> bool:
    return get(name, default).strip().lower() not in ("0", "false", "no", "off")


def int_value(name: str, default: int) -> int:
    raw = get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def float_value(name: str, default: float) -> float:
    raw = get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default