"""Atomic JSON and merge-on-write helpers for multi-container shared data/."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return dict(raw) if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def write_json_atomic(path: Path, data: Any, *, indent: int = 2) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02 * (attempt + 1))
    os.replace(tmp, path)


def merge_json_file(
    path: Path,
    patch: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    indent: int = 2,
) -> None:
    """Read dict JSON (or {}), apply patch, write atomically. Optional flock on Linux."""
    path = Path(path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = None
    try:
        if os.name == "posix":
            import fcntl

            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = open(lock_path, "a", encoding="utf-8")
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        current = load_json_object(path)
        out = patch(current)
        write_json_atomic(path, out, indent=indent)
    finally:
        if fd is not None:
            try:
                import fcntl

                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                fd.close()
            except Exception:
                pass