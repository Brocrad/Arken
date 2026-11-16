# Arken — Discord music bot (Python)

**Arken** is a reliable Discord music fleet bot that plays from YouTube using Python, `yt-dlp`, and `discord.py`. (Separate from **Obselis**, the archive site on the same VPS.)

`ARKEN_*` environment variables configure the bot; legacy `GONDOR_*` names still work.

## 🎵 Features

- 🔍 **Smart YouTube Search** - Search by song name or use direct URLs
- 🎵 **High-Quality Audio** - Reliable audio extraction using yt-dlp
- ⏸️ **Playback Controls** - Pause, resume, and stop functionality
- 🤖 **Slash Commands** - Modern Discord slash command interface
- 🔊 **Voice Channel Support** - Join and play music in voice channels
- 🛡️ **Error Handling** - Graceful error recovery and user feedback

## 🚀 Quick Start

### Prerequisites

1. **Python 3.8+** installed on your system
2. **FFmpeg** installed and in your system PATH
3. **Discord Bot Token** from Discord Developer Portal

### Installation

1. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Install FFmpeg:**
   - **Windows**: Download from https://ffmpeg.org and add to PATH
   - **macOS**: `brew install ffmpeg`
   - **Linux**: `sudo apt install ffmpeg`

3. **Set up your Discord bot:**
   - Go to https://discord.com/developers/applications
   - Create a new application and bot
   - Copy the bot token
   - Add your token to the bot file or set as environment variable

4. **Run the bot:**
   ```bash
   python music_bot.py
   ```

## 🐳 Docker (Linux server)

On the server:

```bash
git clone https://github.com/Brocrad/Arken.git && cd Arken
cp .env.example .env && nano .env                # set NEW_DISCORD_TOKEN
docker compose build
```

Then, **from the PC that has your current data**, push the state to the server (needs `ssh`/`scp`, key auth recommended):

```bash
python deploy/sync_state.py user@your-server                  # add --remote-dir /path/to/Arken, -p PORT, -i KEYFILE
```

It bundles your whitelist, likes/history/stats, per-server settings, playlists **and their stored audio files**,
rewrites Windows paths in the playlist JSON to Linux ones (otherwise the bot drops those songs as "missing"),
copies it over with `scp`, backs up any existing `data/` on the server, installs it and starts the container.
Useful flags: `--authorize <your Discord id>` (build the whitelist from scratch), `--no-audio`, `--clean`
(replace instead of merge), `--no-restart`, `--bundle-only FILE` (no network; copy it yourself).
Your local files are never modified. `media_library/` is only a download cache and is not copied.

```bash
docker compose logs -f                                         # on the server
```

- State lives in `./data` (likes/history/stats, per-server settings, whitelist, saved session, playlists). Back that folder up.
- Stop your local bot first: two bots on one token cause voice 4006 loops.
- If your server user isn't uid/gid 1000, set `ARKEN_UID` / `ARKEN_GID` in `.env` (see `id`) before building.
- `!reboot` works (the container restarts itself). To update the code or yt-dlp: `git pull && docker compose up -d --build`. `!deps update` only lasts until the container is recreated.
- Downloaded audio is a temporary cache inside the container and is not persisted.

### Multi-token pool (optional)

To spread guilds across several Discord bot applications (rate-limit / CPU headroom):

1. Create the bot apps in the Discord developer portal (tokens are not minted by Arken).
2. Set `ARKEN_TOKEN_POOL=gondor,osgiliath,nimloth,morthond` and `ARKEN_MAX_GUILDS_PER_TOKEN=5` in `.env`.
3. Run one process/container per **city-token** with `ARKEN_TOKEN_ID` + that bot’s `NEW_DISCORD_TOKEN` (see `docker-compose.pool.yml`).
4. `!allowguild` auto-assigns the least-loaded token **and** the next free callsign seat (e.g. **Osgiliath · East Bank**). Sticky until `!leaveguild`. Use `!tokens` / `!guilds` to inspect.

Playlists/likes/settings stay isolated by Discord guild id — seats are support labels only. Prometheus textfiles land in `data/metrics/` (`ARKEN_METRICS=0` disables).

## 🎮 Commands

⚠️ **Important:** Use `!summon` first to bring the bot to your voice channel!

All commands use the `!` prefix:

### 🎶 Music Commands (Require !summon first):
- `!play <search or url>` - Play music from YouTube
- `!pause` - Pause the current song
- `!resume` - Resume playback
- `!skip` - Skip to the next track
- `!seek <1:30 | +30 | -15>`, `!forward [seconds]`, `!rewind [seconds]` - Jump around in the current song
- `!volume [0-200 | +10 | -10]` - Show or set the volume (remembered per server)
- `!style [vaporwave|matrix|sunset|synthwave|auto]` - Change the look of the animated badges and summon donut (`auto` takes its colours from each song's cover art)
- `!stop` - Stop the current song (bot stays in channel)

### ✨ Extras
- `!effect [slowed|vaporwave|nightcore|reverb|bass|8d|speed 1.25|off]` - Sound effects, applied live to the song that's playing (remembered per server)
- `!fade [seconds|off]` - Songs fade in and out (a fade-through, not a true overlapping crossfade)
- `!sleep [30m|1h30m|end|off]` - Sleep timer; the volume fades out, `end` stops after the current song
- `!queue` - Interactive queue: page, shuffle, clear, move to top, play now, remove
- `!voteskip` (or the 🗳️ button) - Majority of the people in the voice channel skips; authorized users still use `!skip`
- `!like`, `!likes [play [shuffle] | remove N]`, `!unlike N`, `!history` - Personal favourites and what was played recently (dropdowns queue a song)
- `!lyrics [on|off|full]` - Live lyric line under the progress bar (synced lyrics from LRCLIB); `full` posts the whole text
- `!autoplay [on|off]` - When the queue runs out, keep playing similar songs (stops after 25 in a row, or if nobody is listening)
- `!guess [rounds] [playlist url]` / `!guess stop` / `!guess skip` - Guess-the-song game from songs the server has played and everyone's favourites
- `!stats [day|week|month|all]`, `!stats recap on|off` - Listening stats; a weekly recap is posted automatically
- `!dashboard` (also `!menu`, `!home`) - Home panel, posted automatically when Arken is summoned: **Resume last session**, favourites (+ shuffle), start radio from the last song, a **Quick play** pop-up, stats, a recently-played dropdown, an effect dropdown and autoplay / lyrics / fade toggles. Anyone can open it and view stats; the action buttons need authorization

The now-playing frame also shows who requested the song and the active effect. Bot-side network access stays
zero: lyrics and "similar songs" lookups are done by the audio processor (restart it after updating).
Error messages, `!kill` and a finished playlist show short terminal-style GIFs.

**Session memory:** about every 10 seconds (and on `!stop` / `!kill`) Arken saves what is playing, the queue, the rest of a YouTube playlist and the position, as song *references* (tracks are deleted after playing, so they are downloaded again on resume). **Resume last session** re-queues them; the interrupted song restarts and jumps back to the saved spot if it was 20+ seconds in. A session that ends naturally is cleared; sessions older than 14 days are ignored.

The "now playing" message also has buttons (⏪ 10s, pause/resume, skip, stop, ⏩ 10s, volume −/+), a 🎯 **Jump to…**
pop-up where you type an exact time (`1:30`, `90`, `+30`, `-15`), and a **scrub dropdown** with numbered jump points
(0:00 · 0%, 0:10 · 5%, …). The dropdown's label always shows the live position, e.g. "⏱ 1:23 / 3:20".
Its equalizer bar reacts to the real audio, and shows a live "ends in …" countdown. While a song plays the bot's
profile shows "Listening to <song>".

### ⚙️ Optional environment toggles (set in `.env`, `0` turns a feature off):
`ARKEN_GIFS` (animated badges and terminal scenes branded per server: seat title, `callsign@city` prompt, and `!style` colours), `ARKEN_DONUT` (summon donut), `ARKEN_NP_LIVE` (live progress),
`ARKEN_NP_REACTIVE` (bars follow the audio), `ARKEN_NP_COUNTDOWN` (ends-in line), `ARKEN_NP_BUTTONS` (buttons),
`ARKEN_PRESENCE` (profile status), `ARKEN_COVERS` (pixel-art thumbnail, fetched by the audio processor),
`ARKEN_NOTE_SECONDS` (how long short replies stay, default 12; `0` keeps them),
`ARKEN_FFMPEG` (full path to ffmpeg if it isn't on your PATH), `ARKEN_LYRICS` (lyric lookups),
`ARKEN_RECAP` (weekly recap post), `ARKEN_CRASH_GIF` (terminal crash GIF on errors),
`ARKEN_AUTOPLAY_MAX` (songs in a row autoplay may queue, default 25), `ARKEN_DASHBOARD` (post the dashboard on summon; `!dashboard` always works).
Your favourites, history, stats and saved session live in `arken_data.json` (not committed; legacy `gondor_data.json` is still read if present).

**Naming:** the program is **Arken**. **Gondor** (and `gondor`) appear only for the city **token** and support seats (e.g. **Gondor · Citadel**), not the product.

### 🔧 Control Commands:
- `!summon` - Summon the bot to your voice channel
- `!kill` - Disconnect the bot from voice channel
- `!commands` - Show all available commands

## 💡 Usage Examples

```
# First, summon the bot to your voice channel
!summon

# Then you can use music commands
!play Never Gonna Give You Up
!play The Boys Are Back In Town
!play https://www.youtube.com/watch?v=dQw4w9WgXcQ
!pause
!resume
!stop

# Other commands
!kill      # Disconnect bot completely
!commands  # Show all commands
```

## 🔧 Why Python?

This bot uses Python instead of JavaScript because:

- ✅ **yt-dlp** - The gold standard for YouTube downloading
- ✅ **Better reliability** - Handles YouTube changes automatically
- ✅ **Fewer breakages** - More stable when YouTube updates
- ✅ **Superior audio extraction** - Professional-grade audio processing
- ✅ **discord.py** - Mature Discord library with excellent voice support

## 🛠️ Troubleshooting

### Bot doesn't respond:
- Check bot has proper permissions (Send Messages, Use Slash Commands, Connect, Speak)
- Verify bot token is correct
- Make sure you're in a voice channel when using `/play`

### Audio doesn't play:
- Ensure FFmpeg is installed and in PATH
- Some videos may be region-restricted
- Try different search terms or direct URLs

### yt-dlp errors:
- Update yt-dlp: `pip install --upgrade yt-dlp`
- YouTube occasionally blocks scrapers - this is normal

## 🎵 Perfect for Discord Servers

This Python-based music bot is designed to be:
- **Reliable** - Won't break when YouTube updates
- **Fast** - Quick audio extraction and playback
- **User-friendly** - Simple slash commands
- **Stable** - Robust error handling

Enjoy your working Discord music bot! 🎤🤖
🚀 To Run Your Bot in the Future:
1. Navigate to your project directory: cd `dir here`
2. Activate virtual environment: & .venv/Scripts/Activate.ps1
3. Run the bot: python music_bot.py
