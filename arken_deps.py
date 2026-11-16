#!/usr/bin/env python3
"""
Central dependency maintenance for Arken (pip / requirements.txt).
Used by optional auto-update on bot startup and by update_ytdlp.py.
"""

import subprocess
import sys
from pathlib import Path

import arken_env as env


def project_root() -> Path:
    return Path(__file__).resolve().parent


def requirements_path() -> Path:
    return project_root() / "requirements.txt"


def upgrade_from_requirements(timeout_sec: int = 600) -> bool:
    """pip install --upgrade -r requirements.txt using the current interpreter."""
    req = requirements_path()
    if not req.is_file():
        print(f"⚠️ arken_deps: missing {req}")
        return False
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "-r",
        str(req),
    ]
    try:
        print(f"📦 pip: {' '.join(cmd)}")
        r = subprocess.run(cmd, timeout=timeout_sec)
        if r.returncode != 0:
            print(f"⚠️ pip exited with code {r.returncode}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print(f"⚠️ pip timed out after {timeout_sec}s")
        return False
    except OSError as e:
        print(f"⚠️ pip failed: {e}")
        return False


def upgrade_ytdlp_only(timeout_sec: int = 300) -> bool:
    """Backward-compatible: only yt-dlp (same as legacy update_ytdlp.py)."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"]
    try:
        print(f"📦 pip: {' '.join(cmd)}")
        subprocess.run(cmd, check=True, timeout=timeout_sec)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        print(f"⚠️ yt-dlp upgrade failed: {e}")
        return False


def maybe_auto_update_dependencies() -> None:
    """
    If ARKEN_AUTO_UPDATE_DEPS is 1/true/on, run upgrade_from_requirements().
    Set ARKEN_AUTO_UPDATE_YTDLP_ONLY=1 to only upgrade yt-dlp (lighter).
    """
    if env.truthy("SKIP_DEP_UPDATE"):
        return
    if not env.truthy("AUTO_UPDATE_DEPS"):
        return
    if env.truthy("AUTO_UPDATE_YTDLP_ONLY"):
        print("📦 ARKEN_AUTO_UPDATE_DEPS: upgrading yt-dlp only")
        upgrade_ytdlp_only()
    else:
        print("📦 ARKEN_AUTO_UPDATE_DEPS: upgrading from requirements.txt")
        upgrade_from_requirements()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Arken dependency maintenance")
    p.add_argument(
        "mode",
        nargs="?",
        default="requirements",
        choices=("requirements", "ytdlp"),
        help="requirements = full requirements.txt; ytdlp = yt-dlp only",
    )
    args = p.parse_args()
    ok = (
        upgrade_ytdlp_only()
        if args.mode == "ytdlp"
        else upgrade_from_requirements()
    )
    sys.exit(0 if ok else 1)