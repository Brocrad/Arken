#!/usr/bin/env python3
"""
Build Gondor's persistent state from this machine and install it on the Linux server.

Run this on the PC that has your current Gondor data (needs Python 3 and OpenSSH `ssh`/`scp`,
both already present on Windows 10/11, macOS and Linux):

    python deploy/sync_state.py user@your-server
    python deploy/sync_state.py user@your-server --remote-dir /srv/Gondor --port 2222 -i ~/.ssh/id_ed25519

What it does
  1. Builds a bundle (a staged COPY; your local files are never modified) containing
       data/user_whitelist.json      who may use admin commands (REQUIRED, or use --authorize)
       data/gondor_data.json         likes, history, stats, saved session
       data/gondor_settings.json     per-server volume/effect/style settings
       data/playlists/               playlist JSON + the stored audio files
     Windows paths inside the playlist JSON (`playlists\\x\\y.webm`, `C:\\...\\media_library\\...`)
     are rewritten to portable ones (`playlists/x/y.webm`). Without that, Linux treats the backslash
     name as a missing file and the bot drops the songs from the playlist.
  2. scp's the bundle to the server.
  3. Over ssh: stops the container, backs up any existing ./data, unpacks the bundle into
     <remote-dir>/data, and starts the container again.

Not copied on purpose: media_library/ (a temporary download cache, rebuilt on demand), gif_cache/
(regenerated), playback_state.json / voice_state.json (machine-specific), and .env (your token --
put that on the server yourself).
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STATE_FILES = ("user_whitelist.json", "gondor_data.json", "gondor_settings.json")
AUDIO_EXTS = {".webm", ".m4a", ".mp4", ".opus", ".mp3", ".ogg", ".wav", ".flac"}

# "playlists\x\y.webm", "media_library/z.webm", "C:\anything\Gondor\media_library\z.webm"
_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/].*?[\\/])?((?:playlists|media_library)[\\/].*)$", re.S)


# --------------------------------------------------------------------------- bundle building

def normalize_paths(obj, counter: list[int]):
    """Recursively rewrite Windows-style Gondor paths inside parsed JSON. counter[0] counts changes."""
    if isinstance(obj, dict):
        return {k: normalize_paths(v, counter) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_paths(v, counter) for v in obj]
    if isinstance(obj, str):
        m = _PATH_RE.match(obj)
        if m:
            new = m.group(1).replace("\\", "/")
            if new != obj:
                counter[0] += 1
            return new
    return obj


def collect_audio_refs(obj, out: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "audio_file" and isinstance(v, str):
                out.append(v)
            else:
                collect_audio_refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_audio_refs(v, out)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def dump_json(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mtime = time.time()
    info.mode = 0o644
    info.uid = info.gid = 0
    tar.addfile(info, io.BytesIO(data))


def add_dir(tar: tarfile.TarFile, arcname: str) -> None:
    info = tarfile.TarInfo(arcname)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.mtime = time.time()
    tar.addfile(info)


def build_bundle(source: Path, out: Path, *, include_audio: bool, authorize: list[int]) -> dict:
    """Write the tar.gz bundle to `out`. Returns a summary dict. Raises SystemExit on fatal problems."""
    summary = {"files": [], "audio_files": 0, "audio_bytes": 0, "rewritten": 0, "missing_audio": [], "warnings": []}
    counter = [0]

    with tarfile.open(out, "w:gz") as tar:
        add_dir(tar, "data")
        add_dir(tar, "data/playlists")

        # --- whitelist (required: with none, nobody can use admin commands) ---
        wl_path = source / "user_whitelist.json"
        if wl_path.is_file():
            wl = load_json(wl_path)
        elif authorize:
            wl = {}
        else:
            raise SystemExit(
                f"error: {wl_path} not found. Without a whitelist nobody can use admin commands on the "
                f"server.\n       Pass your Discord user id(s): --authorize 123456789012345678"
            )
        users = [int(u) for u in wl.get("authorized_users", [])]
        for uid in authorize:
            if uid not in users:
                users.append(uid)
        wl = {"authorized_users": users, "last_updated": time.time()}
        data = dump_json(wl)
        add_bytes(tar, "data/user_whitelist.json", data)
        summary["files"].append(("user_whitelist.json", len(data), f"{len(users)} authorized user(s)"))

        # --- plain state files ---
        for name in STATE_FILES[1:]:
            p = source / name
            if not p.is_file():
                summary["warnings"].append(f"{name} not found locally; the server will start with it empty")
                continue
            try:
                parsed = load_json(p)
            except ValueError as e:
                raise SystemExit(f"error: {p} is not valid JSON ({e}); fix or move it first")
            data = dump_json(parsed) if name != "gondor_data.json" else json.dumps(
                parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            add_bytes(tar, f"data/{name}", data)
            summary["files"].append((name, len(data), ""))

        # --- playlists (JSON path-normalized, audio copied as-is) ---
        pl_root = source / "playlists"
        if not pl_root.is_dir():
            summary["warnings"].append("no playlists/ folder locally; skipping playlists")
        else:
            audio_refs: list[str] = []
            for f in sorted(pl_root.rglob("*")):
                if f.is_dir():
                    continue
                rel = f.relative_to(source).as_posix()          # playlists/groovy/x.webm
                arc = f"data/{rel}"
                if f.suffix.lower() == ".json":
                    try:
                        parsed = load_json(f)
                    except ValueError as e:
                        summary["warnings"].append(f"skipped invalid JSON {rel}: {e}")
                        continue
                    parsed = normalize_paths(parsed, counter)
                    collect_audio_refs(parsed, audio_refs)
                    add_bytes(tar, arc, dump_json(parsed))
                elif f.suffix.lower() in AUDIO_EXTS:
                    if not include_audio:
                        continue
                    tar.add(f, arcname=arc, filter=_clean_owner)
                    summary["audio_files"] += 1
                    summary["audio_bytes"] += f.stat().st_size
                else:
                    tar.add(f, arcname=arc, filter=_clean_owner)
            for ref in audio_refs:
                if not (source / ref).is_file():
                    summary["missing_audio"].append(ref)

    summary["rewritten"] = counter[0]
    return summary


def _clean_owner(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mode = 0o644
    return ti


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


# --------------------------------------------------------------------------- remote side

def build_remote_script(remote_dir: str, bundle_name: str, *, clean: bool, restart: bool) -> str:
    """POSIX sh executed on the server via `ssh host sh -s` (no quoting pitfalls)."""
    return f"""set -eu
REMOTE_DIR={shlex.quote(remote_dir)}
BUNDLE="/tmp/"{shlex.quote(bundle_name)}
CLEAN={1 if clean else 0}
RESTART={1 if restart else 0}

case "$REMOTE_DIR" in
  "~") REMOTE_DIR="$HOME" ;;
  "~/"*) REMOTE_DIR="$HOME/${{REMOTE_DIR#\\~/}}" ;;
esac
if [ ! -d "$REMOTE_DIR" ]; then
  echo "remote: $REMOTE_DIR does not exist - git clone the repo there first" >&2
  exit 2
fi
cd "$REMOTE_DIR"

DC=""
if command -v docker >/dev/null 2>&1; then
  if docker compose version >/dev/null 2>&1; then DC="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then DC="docker-compose"; fi
fi
if [ -n "$DC" ] && [ -f docker-compose.yml ]; then
  echo "remote: stopping gondor"
  $DC stop || true
fi

TS=$(date +%Y%m%d-%H%M%S)
if [ -d data ] && [ -n "$(ls -A data 2>/dev/null)" ]; then
  tar -czf "data.bak-$TS.tgz" data
  echo "remote: backed up existing data to $REMOTE_DIR/data.bak-$TS.tgz"
fi
if [ "$CLEAN" = 1 ]; then
  echo "remote: --clean, removing existing data/"
  rm -rf data
fi

mkdir -p data/playlists
tar -xzf "$BUNDLE" --no-same-owner
rm -f "$BUNDLE"
chmod -R u+rwX data
echo "remote: installed state into $REMOTE_DIR/data"
ls -l data

if [ "$RESTART" = 1 ]; then
  if [ -n "$DC" ] && [ -f docker-compose.yml ] && [ -f .env ]; then
    echo "remote: starting gondor"
    $DC up -d
  else
    echo "remote: not starting (need docker compose, docker-compose.yml and .env in $REMOTE_DIR)"
  fi
fi
"""


def run(cmd: list[str], *, cwd: Path | None = None, stdin: str | None = None) -> None:
    print("$ " + " ".join(shlex.quote(c) for c in cmd))
    # Bytes, not text: on Windows text mode would turn "\n" into "\r\n" and break `sh -s` on the server.
    r = subprocess.run(cmd, cwd=cwd, input=stdin.encode("utf-8") if stdin is not None else None)
    if r.returncode != 0:
        raise SystemExit(f"command failed with exit code {r.returncode}")


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="ssh target, e.g. jasper@192.168.1.50")
    ap.add_argument("--remote-dir", default="~/Gondor", help="Gondor checkout on the server (default ~/Gondor)")
    ap.add_argument("--source", type=Path, default=ROOT, help="local Gondor folder to read (default: this repo)")
    ap.add_argument("-p", "--port", type=int, help="ssh port")
    ap.add_argument("-i", "--identity", help="ssh private key file")
    ap.add_argument("--authorize", type=int, nargs="+", default=[], metavar="DISCORD_ID",
                    help="add Discord user id(s) to the whitelist (creates it if there is no local file)")
    ap.add_argument("--no-audio", action="store_true", help="skip stored audio files (playlist JSON only)")
    ap.add_argument("--clean", action="store_true",
                    help="wipe the server's data/ first (existing data is still backed up); default merges/overwrites")
    ap.add_argument("--no-restart", action="store_true", help="do not stop/start the container")
    ap.add_argument("--bundle-only", type=Path, metavar="FILE",
                    help="just write the bundle to FILE and stop (no network); copy it over yourself")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()

    if not args.bundle_only and not args.target:
        ap.error("give an ssh target (user@host) or use --bundle-only FILE")

    source = args.source.resolve()
    if not (source / "streaming.py").is_file():
        raise SystemExit(f"error: {source} does not look like a Gondor folder (no streaming.py)")

    if args.bundle_only:
        bundle = args.bundle_only.resolve()
        tmp_ctx = None
    else:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="gondor_state_")
        bundle = Path(tmp_ctx.name) / f"gondor_state_{time.strftime('%Y%m%d-%H%M%S')}.tgz"

    try:
        print(f"Building state bundle from {source} ...")
        s = build_bundle(source, bundle, include_audio=not args.no_audio, authorize=args.authorize)

        print("\nBundle contents:")
        for name, size, note in s["files"]:
            print(f"  data/{name:<22} {human(size):>9}  {note}")
        print(f"  data/playlists/        {s['audio_files']} audio file(s), {human(s['audio_bytes'])}")
        print(f"  Windows paths rewritten in playlist JSON: {s['rewritten']}")
        print(f"  Bundle size: {human(bundle.stat().st_size)}")
        for w in s["warnings"]:
            print(f"  warning: {w}")
        if s["missing_audio"] and not args.no_audio:
            print(f"  warning: {len(s['missing_audio'])} playlist song(s) point to audio missing locally "
                  f"(the bot will drop them on the server):")
            for ref in s["missing_audio"][:10]:
                print(f"           {ref}")

        if args.bundle_only:
            print(f"\nWrote {bundle}")
            print("Install manually: copy to the server, then in the Gondor folder run\n"
                  "  docker compose stop; tar -xzf <bundle> --no-same-owner; docker compose up -d")
            return 0

        if not args.yes:
            mode = "REPLACE (after backup)" if args.clean else "merge into (overwrite same names, after backup)"
            ans = input(f"\nInstall on {args.target}:{args.remote_dir}/data - will {mode}. Continue? [y/N] ")
            if ans.strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 1

        ssh_opts: list[str] = []
        scp_opts: list[str] = []
        if args.port:
            ssh_opts += ["-p", str(args.port)]
            scp_opts += ["-P", str(args.port)]
        if args.identity:
            ssh_opts += ["-i", args.identity]
            scp_opts += ["-i", args.identity]

        # scp from the bundle's own folder using a bare filename: a Windows path like C:\... would be
        # parsed by scp as "host C".
        print()
        run(["scp", *scp_opts, bundle.name, f"{args.target}:/tmp/{bundle.name}"], cwd=bundle.parent)
        script = build_remote_script(args.remote_dir, bundle.name, clean=args.clean, restart=not args.no_restart)
        run(["ssh", *ssh_opts, args.target, "sh", "-s"], stdin=script)
        print("\nDone. Watch it start with:  ssh " + args.target + "  'cd " + args.remote_dir + " && docker compose logs -f'")
        return 0
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


if __name__ == "__main__":
    sys.exit(main())
