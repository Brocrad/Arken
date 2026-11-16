#!/usr/bin/env python3
"""
Build Arken's persistent state from this machine and install it on the Linux server.

Run this on the PC that has your current Arken data (needs Python 3 and OpenSSH `ssh`/`scp`,
both already present on Windows 10/11, macOS and Linux):

    python deploy/sync_state.py user@your-server
    python deploy/sync_state.py user@your-server --remote-dir /srv/Arken --port 2222 -i ~/.ssh/id_ed25519

What it does
  1. Builds a bundle (a staged COPY; your local files are never modified) containing
       data/user_whitelist.json      bot owners (REQUIRED, or use --authorize)
       data/arken_data.json          likes/history/stats (legacy gondor_data.json is accepted when packing)
       data/arken_settings.json      per-server volume/effect/style settings
       data/allowed_guilds.json      optional server approval registry
       data/token_pool.json          optional multi-token guild assignments
       data/playlists/<guild_id>/    that server's playlists + stored audio
     Windows paths are rewritten and namespaced under playlists/<home_guild>/…
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
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import arken_env as aenv  # noqa: E402
import arken_tenancy as tenancy  # noqa: E402

STATE_FILES = ("user_whitelist.json", "arken_data.json", "arken_settings.json")
AUDIO_EXTS = {".webm", ".m4a", ".mp4", ".opus", ".mp3", ".ogg", ".wav", ".flac"}
_GUILD_DIR_RE = re.compile(r"^\d{15,25}$")

# "playlists\x\y.webm", "media_library/z.webm", "C:\anything\Arken\media_library\z.webm"
_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/].*?[\\/])?((?:playlists|media_library)[\\/].*)$", re.S)


# --------------------------------------------------------------------------- bundle building

def normalize_paths(obj, counter: list[int], home_guild: int | None = None):
    """Rewrite Windows paths; if home_guild is set, namespace playlists/… under that guild."""
    if isinstance(obj, dict):
        return {k: normalize_paths(v, counter, home_guild) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_paths(v, counter, home_guild) for v in obj]
    if isinstance(obj, str):
        m = _PATH_RE.match(obj)
        if m:
            new = m.group(1).replace("\\", "/")
            if home_guild is not None and new.startswith("playlists/"):
                new = tenancy.scope_playlist_path(new, home_guild)
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


def find_state_json(source: Path, primary: str, legacy: str) -> Path | None:
    """Prefer Arken filenames; fall back to legacy names under source/ or source/data/."""
    for name in (primary, legacy):
        for base in (source / "data", source):
            p = base / name
            if p.is_file():
                return p
    return None


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


def build_bundle(
    source: Path,
    out: Path,
    *,
    include_audio: bool,
    authorize: list[int],
    home_guild_env: str | None = None,
) -> dict:
    """Write the tar.gz bundle to `out`. Returns a summary dict. Raises SystemExit on fatal problems."""
    summary = {
        "files": [],
        "audio_files": 0,
        "audio_bytes": 0,
        "rewritten": 0,
        "missing_audio": [],
        "warnings": [],
        "home_guild": None,
        "likes_moved": 0,
    }
    counter = [0]

    settings = {}
    settings_path = find_state_json(source, "arken_settings.json", "gondor_settings.json")
    if settings_path is not None:
        try:
            settings = load_json(settings_path)
        except ValueError:
            settings = {}

    data_obj = {}
    data_path = find_state_json(source, "arken_data.json", "gondor_data.json")
    if data_path is not None:
        try:
            data_obj = load_json(data_path)
        except ValueError as e:
            raise SystemExit(f"error: {data_path} is not valid JSON ({e})")

    home = tenancy.resolve_home_guild(
        home_guild_env or aenv.get("HOME_GUILD"),
        tenancy.historical_guild_ids(data_obj, settings),
    )
    summary["home_guild"] = home
    if home is not None:
        summary["likes_moved"] = tenancy.migrate_likes(data_obj, home)
    elif data_obj.get("users"):
        summary["warnings"].append(
            "global likes present but home guild ambiguous — set ARKEN_HOME_GUILD or --home-guild"
        )

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
        # Preserve v2 staff roles; legacy authorized_users become owners.
        users_map = {}
        if isinstance(wl.get("users"), dict):
            for uid, meta in wl["users"].items():
                if not str(uid).isdigit():
                    continue
                role = tenancy.normalize_staff_role(
                    meta.get("role") if isinstance(meta, dict) else meta
                ) or "owner"
                users_map[str(uid)] = {"role": role}
        for uid in wl.get("authorized_users") or []:
            if str(uid).isdigit() and str(uid) not in users_map:
                users_map[str(uid)] = {"role": "owner"}
        for uid in authorize:
            users_map.setdefault(str(int(uid)), {"role": "owner"})
        wl = {
            "version": 2,
            "users": users_map,
            "authorized_users": [int(u) for u in sorted(users_map, key=int)],
            "last_updated": time.time(),
        }
        payload = dump_json(wl)
        add_bytes(tar, "data/user_whitelist.json", payload)
        summary["files"].append(("user_whitelist.json", len(payload), f"{len(users_map)} staff"))

        # --- arken_data (likes already migrated into home guild when possible) ---
        if data_path is not None or data_obj:
            payload = json.dumps(data_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            add_bytes(tar, "data/arken_data.json", payload)
            note = f"likes→guild {home}" if summary["likes_moved"] else ""
            legacy = " (from gondor_data.json)" if data_path and data_path.name == "gondor_data.json" else ""
            summary["files"].append(("arken_data.json", len(payload), note + legacy))
        else:
            summary["warnings"].append("arken_data.json not found locally; the server will start with it empty")

        if settings_path is not None:
            payload = dump_json(settings)
            add_bytes(tar, "data/arken_settings.json", payload)
            legacy = " (from gondor_settings.json)" if settings_path.name == "gondor_settings.json" else ""
            summary["files"].append(("arken_settings.json", len(payload), legacy))
        else:
            summary["warnings"].append("arken_settings.json not found locally; the server will start with it empty")

        ag = source / "allowed_guilds.json"
        if ag.is_file():
            try:
                payload = dump_json(load_json(ag))
                add_bytes(tar, "data/allowed_guilds.json", payload)
                summary["files"].append(("allowed_guilds.json", len(payload), ""))
            except ValueError as e:
                summary["warnings"].append(f"skipped allowed_guilds.json: {e}")

        tp = source / "token_pool.json"
        if tp.is_file():
            try:
                payload = dump_json(load_json(tp))
                add_bytes(tar, "data/token_pool.json", payload)
                summary["files"].append(("token_pool.json", len(payload), ""))
            except ValueError as e:
                summary["warnings"].append(f"skipped token_pool.json: {e}")

        # --- playlists: legacy playlists/foo → data/playlists/<home>/foo ---
        pl_root = source / "playlists"
        if not pl_root.is_dir():
            summary["warnings"].append("no playlists/ folder locally; skipping playlists")
        else:
            audio_refs: list[str] = []
            for f in sorted(pl_root.rglob("*")):
                if f.is_dir():
                    continue
                rel = f.relative_to(pl_root).as_posix()  # groovy/x.webm or <gid>/groovy/x.webm
                parts = rel.split("/")
                if parts and _GUILD_DIR_RE.match(parts[0]):
                    arc_rel = f"playlists/{rel}"
                    guild_for_paths = int(parts[0])
                elif home is not None:
                    arc_rel = f"playlists/{home}/{rel}"
                    guild_for_paths = home
                else:
                    summary["warnings"].append(
                        f"skipped {rel}: not under a guild id and no home guild resolved"
                    )
                    continue
                arc = f"data/{arc_rel}"
                if f.suffix.lower() == ".json":
                    try:
                        parsed = load_json(f)
                    except ValueError as e:
                        summary["warnings"].append(f"skipped invalid JSON {rel}: {e}")
                        continue
                    parsed = normalize_paths(parsed, counter, guild_for_paths)
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
                # refs are portable playlists/<gid>/… ; also accept legacy relative under source
                if not (source / ref).is_file() and not (ROOT / ref).is_file():
                    # audio may live at source/playlists/<name>/file while ref is namespaced
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
trap 'rm -f "$BUNDLE"' EXIT   # never leave the upload behind, even if a step below fails
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

# --- preflight: fail before touching anything ---
mkdir -p data/playlists 2>/dev/null || true
BAD=$(find data ! -writable 2>/dev/null | head -5)
if [ ! -d data ]; then BAD="$REMOTE_DIR"; fi
if [ -n "$BAD" ]; then
  echo "remote: no write permission here (as user $(id -un)):" >&2
  echo "$BAD" | sed 's/^/          /' >&2
  echo "remote: usually docker created data/ as root. Fix with:" >&2
  echo "          sudo docker compose stop; sudo chown -R $(id -un):$(id -gn) $REMOTE_DIR" >&2
  exit 3
fi

DC=""
if command -v docker >/dev/null 2>&1; then
  if docker compose version >/dev/null 2>&1; then DC="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then DC="docker-compose"; fi
fi
if [ "$RESTART" = 1 ] && [ -n "$DC" ] && ! docker info >/dev/null 2>&1; then
  echo "remote: this user cannot talk to Docker (not in the 'docker' group?). Nothing was changed." >&2
  echo "          sudo usermod -aG docker $(id -un)   # then log out and back in" >&2
  echo "        or: sudo docker compose stop  (on the server), rerun with --no-restart, then sudo docker compose up -d." >&2
  exit 4
fi
if [ -n "$DC" ] && [ "$RESTART" = 1 ] && [ -f docker-compose.yml ]; then
  echo "remote: stopping arken"
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
    echo "remote: starting arken"
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
    ap.add_argument("--remote-dir", default="~/Arken", help="repo checkout on the server (default ~/Arken)")
    ap.add_argument("--source", type=Path, default=ROOT, help="local Arken folder to read (default: this repo)")
    ap.add_argument("-p", "--port", type=int, help="ssh port")
    ap.add_argument("-i", "--identity", help="ssh private key file")
    ap.add_argument("--authorize", type=int, nargs="+", default=[], metavar="DISCORD_ID",
                    help="add Discord user id(s) to the whitelist (creates it if there is no local file)")
    ap.add_argument("--home-guild", metavar="DISCORD_SERVER_ID",
                    help="server that owns legacy likes/playlists (else ARKEN_HOME_GUILD / auto)")
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
        raise SystemExit(f"error: {source} does not look like an Arken checkout (no streaming.py)")

    if args.bundle_only:
        bundle = args.bundle_only.resolve()
        tmp_ctx = None
    else:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="arken_state_")
        bundle = Path(tmp_ctx.name) / f"arken_state_{time.strftime('%Y%m%d-%H%M%S')}.tgz"

    try:
        print(f"Building state bundle from {source} ...")
        s = build_bundle(
            source,
            bundle,
            include_audio=not args.no_audio,
            authorize=args.authorize,
            home_guild_env=args.home_guild,
        )

        print("\nBundle contents:")
        for name, size, note in s["files"]:
            print(f"  data/{name:<22} {human(size):>9}  {note}")
        print(f"  data/playlists/        {s['audio_files']} audio file(s), {human(s['audio_bytes'])}")
        if s.get("home_guild"):
            print(f"  Home guild (legacy likes/playlists): {s['home_guild']}")
        if s.get("likes_moved"):
            print(f"  Favourites moved into home guild: {s['likes_moved']}")
        print(f"  Paths rewritten in playlist JSON: {s['rewritten']}")
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
            print("Install manually: copy to the server, then in the Arken checkout run\n"
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
