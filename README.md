# Bogobot

Bogobot is a specialized Discord bot designed for monitoring the [24/7 Bogosort Livestream](https://www.youtube.com/live/7Y5eyyUNsYo). The bot now uses the Bogostream stats API by default, with OCR still available as a fallback pipeline for stream-derived statistics and sort-state tracking.

## Prerequisites

Python 3.10+ is required. The dependency scripts install the usual system tools:
FFmpeg, Streamlink, optional libtesseract/Tesseract OCR support, and the Python packages used by the bot.
Android/Termux is a supported environment.

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Msmastr74/Bogobot
   cd Bogobot
   ```
2. Run the dependency script for your environment:
   ```bash
   bash dependencies_android.sh   # For Android/Termux
   # OR
   bash dependencies_macos.sh  # For macOS with Homebrew
   # OR
   bash dependencies_windows.sh  # For Windows
   # OR
   bash dependencies_linux.sh  # For Linux
   ```
## Configuration
Go into `config.json` and provide the main credentials:
 * `bot_token`: Discord bot token.
 * `owner_uid`: Your Discord user ID. This account is forced to the owner rank on startup.
 * `accounts_path`: Optional account database path. Defaults to `accounts.json`.
 * `sync`: Optional one-run force sync for slash commands. The bot also syncs automatically when its command tree changes.
 * `save_live_frame`: Optional debug setting. When true, the bot writes the latest stream frame to `live_720p.png` after each received frame. Defaults to false.
 * `sort_change_threshold`: Optional monitor sensitivity for stream visual changes. Defaults to `0.1`.
 * `sort_section_count`: Optional number of sort sections used for monitor classification. Defaults to `25`.
 * `sort_area_left`, `sort_area_top`, `sort_observed_top`, `sort_area_right`, `sort_area_bottom`: Optional coordinates used to classify red/green sort sections and read sort values.
 * `stats_source`: Optional stats source. Defaults to `api`; set to `ocr` to use OCR-driven stat and sort updates.
 * `bogostream_stats_api_url`: Optional Bogostream stats API endpoint override.
 * `bogostream_stats_api_interval`: Optional API poll interval in seconds. Defaults to `1`.
 * `ocr_enabled`: Optional Tesseract OCR startup toggle. Defaults to true only when `stats_source` is `ocr`.
 * `ocr_concurrency`: Optional number of persistent libtesseract worker threads. Defaults to `2`.
 * `tessdata_path`: Optional directory for bot-managed `eng_fast.traineddata`. Defaults to `tessdata`.
 * `code_sandbox_fuel`: Optional WASI instruction fuel limit for `/python` and `/javascript`. Defaults to `25000000000`.
 * `milestone_initialize_format`: Optional message template for new milestones.
 * `milestone_update_format`: Optional message template for milestone changes.
 * `telemetry_path`: Optional JSONL ("JSON Lines", one JSON record on each line) path for command telemetry. Defaults to `telemetry.jsonl`.
 * `archive`: Optional archive configuration object. It contains compact monitor archive settings and optional visual archive settings, including appendable daily `.ts` recording and old-day remuxing to `mkv`, `mp4`, or `ts`.
 * `bogotree_path`: Optional Bogotree storage path. Defaults to `bogotree.json`.
 * `ai`: Optional AI configuration object. See `AI.md` for setup, provider examples, local Ollama guidance, and prompt/context notes.

`DOCS.md` lists core settings and bot-managed storage fields. `AI.md` covers AI setup and provider examples.

If `local_config.json` exists, `main.py` uses that instead of `config.json`.
This is useful for local testing without changing the main config file.

Milestone templates use Python's `string.Template` syntax:

```json
"milestone_update_format": "<@&role_id> $milestone_name updated from `$old_value` to `$new_value`."
```

## Execution
```bash
python main.py
```

Your terminal output should look similar to this:
```log
May 26 00:51:53.941 WARNING  discord.client  PyNaCl is not installed, voice will NOT be supported
May 26 00:51:53.941 WARNING  discord.client  davey is not installed, voice will NOT be supported
May 26 00:51:54.031 INFO     Bogobot.Plugins Loading plugins...
May 26 00:51:54.033 INFO     Bogobot.Plugins Loaded Plugin: fun.py
May 26 00:51:54.034 INFO     Bogobot.Plugins Loaded Plugin: monitor.py
May 26 00:51:54.036 INFO     Bogobot.Plugins Loaded Plugin: leaderboard.py
May 26 00:51:54.038 INFO     Bogobot.Plugins Loaded Plugin: accounts.py
May 26 00:51:54.041 INFO     Bogobot.Plugins Loaded Plugin: milestones.py
May 26 00:51:54.049 INFO     Bogobot.Plugins Loaded Plugin: utility.py
May 26 00:51:54.049 INFO     Bogobot.Plugins Loaded Plugin: ai.py
May 26 00:51:54.094 INFO     Bogobot.Plugins Loaded Plugin: bogo.py
May 26 00:51:54.103 INFO     Bogobot.Plugins Loaded Plugin: telemetry.py
May 26 00:51:54.105 INFO     Bogobot.Plugins Loaded Plugin: get_stats.py
May 26 00:51:54.106 INFO     Bogobot.Plugins Loaded Plugin: admin.py
May 26 00:51:54.109 INFO     Bogobot.Plugins Loaded Plugin: cbogo.py
May 26 00:51:54.116 INFO     Bogobot.Plugins Loaded Plugin: stats.py
May 26 00:51:54.121 INFO     Bogobot.Plugins Loaded Plugin: archival.py
May 26 00:51:54.127 INFO     Bogobot.Plugins Loaded Plugin: bogoscramble.py
May 26 00:51:54.131 INFO     Bogobot.Plugins Loaded Plugin: bogotree.py
May 26 00:51:54.131 INFO     Bogobot.Plugins Finished loading plugins.
May 26 00:51:54.131 INFO     discord.client  logging in using static token
May 26 00:51:54.469 INFO     Bogobot         Syncing Discord command tree (command tree changed)
May 26 00:51:55.089 INFO     discord.gateway Shard ID None has connected to Gateway (Session ID: cda0c70dfebc1d29c40011f49a3a6149).
May 26 00:51:57.136 INFO     Bogobot         Logged in as Bogobot-Testing#8298 (ID: 1499874423019409599)
May 26 00:51:57.279 INFO     Bogobot         Beginning automatic account creation...
May 26 00:51:57.279 INFO     Bogobot         Automatically created 0 accounts out of 4 members from REDACTED.
May 26 00:51:57.279 INFO     Bogobot         Automatically created 0 accounts out of 17 members from Bogobot development (1495827707085197385).
May 26 00:51:57.280 INFO     Bogobot         Automatic account creation finished. Automatically created a total of 0 accounts out of a total of 21 members from 2 servers.
```

## Features
Bogobot implements several slash commands for stream management and data retrieval:
 * /get_stats: Retrieves current shuffles, comparisons, and calculated uptime.
 * /get_sort: Retrieves the latest observed sort state and frame image when stream data is available.
 * @mention AI and /ai: Bogobot can chat, call registered command actions, and queue passive context requests. See `AI.md`.
 * /archive: Shows archived monitor values.
 * /archive_frame: Shows a visual archive frame by epoch or Discord timestamp.
 * /top, /bottom, /middle: Shows sortoff leaderboard slices.
 * /manage monitor: Starts, stops, or resends a persistent tracking system for stream serial numbers.
 * /manage leaderboard_monitor: Starts, stops, or resends a persistent top leaderboard message.
 * /manage stats_monitor: Starts, stops, or resends a persistent stream-stats message.
 * /manage live_chat: Starts, stops, or resends a persistent YouTube live-chat monitor.
 * /manage video_archive: Starts, stops, restarts, or shows status for visual stream archive recording.
 * /manage milestones: Subscribes/unsubscribes milestone notifications, or spoofs/deletes milestone values.
 * /milestone_info: Shows recent milestone history and frame images.
 * /manage announce: Sends a simple bot-authored announcement.
 * /manage message: Deletes, pins, edits, replies to, or reacts to a message by ID.
 * /manage state: Stops or restarts the bot process.
 * /manage logs and /manage telemetry: Shows recent in-memory logs or command activity.
 * /usage: Shows command usage totals.
 * /avatar and /ping: Small Discord utility commands.
 * /python and /javascript: Execute code in WASI-backed sandboxes, with modal input for longer programs or uploaded source files.
 * /ai_activity: Schedule, trigger, list, or remove AI activity triggers for a channel.
 * /bogo and friends: Grouped roll, shuffle, choice, text bogo, name bogo, and small bogosort utilities, plus top-level /sort and random number helpers.
 * /cbogo: Runs the original collaborative community bogosort puzzle.
 * /bogotree: Advances or resets a collaborative random equalization puzzle.
 * /accounts: Shows account info, manages permission ranks, lists users, and bans/unbans accounts.

Bogobot also writes an append-only monitor archive when observed sort values are available.
Each chunk starts with a JSON header line, then compact `dt,value;` records where `dt`
is centiseconds since the previous archived value.

## Documentation
For technical details regarding the internal API, OCR configuration, channel proxies, and plugin development, refer to `DOCS.md`.
