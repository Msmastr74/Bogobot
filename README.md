# Bogobot

Bogobot is a specialized Discord bot designed for monitoring the [24/7 Bogosort Livestream](https://www.youtube.com/live/DgfiqGPmGWY). The bot uses OCR and stream-derived timing to provide high-accuracy statistics directly from the stream.

## Prerequisites

Python 3.10+ is required. The dependency scripts install the usual system tools:
libtesseract/Tesseract, FFmpeg, Streamlink, and the Python packages used by the bot.

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
 * `sort_observed_left`, `sort_observed_top`, `sort_observed_right`, `sort_observed_bottom`: Optional thin-strip coordinates used to classify red/green sort sections.
 * `ocr_concurrency`: Optional number of persistent libtesseract worker threads. Defaults to `2`.
 * `tessdata_path`: Optional directory for bot-managed `eng_fast.traineddata`. Defaults to `tessdata`.
 * `milestone_initialize_format`: Optional message template for new milestones.
 * `milestone_update_format`: Optional message template for milestone changes.
 * `telemetry_path`: Optional JSONL ("JSON Lines", one JSON record on each line) path for command telemetry. Defaults to `telemetry.jsonl`.
 * `archive_path`: Optional compact monitor archive path. Defaults to `archive/monitor.bga`.
 * `archive_flush_interval`: Optional seconds between archive flushes. Defaults to `60`.
 * `archive_chunk_event_limit`: Optional maximum monitor values per archive chunk. Defaults to `200`.

`DOCS.md` lists every supported user setting and every bot-managed storage field.

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
[May 16 21:18:31.428 WARNING  | discord.client  ] PyNaCl is not installed, voice will NOT be supported
[May 16 21:18:31.428 WARNING  | discord.client  ] davey is not installed, voice will NOT be supported
[May 16 21:18:31.511 INFO     | Bogobot         ] Loaded Plugin: monitor.py
[May 16 21:18:31.513 INFO     | Bogobot         ] Loaded Plugin: leaderboard.py
[May 16 21:18:31.515 INFO     | Bogobot         ] Loaded Plugin: accounts.py
[May 16 21:18:31.519 INFO     | Bogobot         ] Loaded Plugin: milestones.py
[May 16 21:18:31.521 INFO     | Bogobot         ] Loaded Plugin: utility.py
[May 16 21:18:31.522 INFO     | Bogobot         ] Loaded Plugin: activity.py
[May 16 21:18:31.648 INFO     | Bogobot         ] Loaded Plugin: telemetry.py
[May 16 21:18:31.649 INFO     | Bogobot         ] Loaded Plugin: get_stats.py
[May 16 21:18:31.649 INFO     | Bogobot         ] Loaded Plugin: admin.py
[May 16 21:18:31.651 INFO     | Bogobot         ] Loaded Plugin: stats.py
[May 16 21:18:31.657 INFO     | Bogobot         ] Loaded Plugin: bogoscramble.py
[May 16 21:18:31.658 INFO     | Bogobot         ] Loaded Plugin: roll.py
[May 16 21:18:31.659 INFO     | discord.client  ] logging in using static token
[May 16 21:18:32.036 INFO     | Bogobot         ] Syncing Discord command tree (command tree changed)
[May 16 21:18:32.731 INFO     | discord.gateway ] Shard ID None has connected to Gateway (Session ID: 64e45a6cbc7030530954eb66dac06a9c).
[May 16 21:18:34.733 INFO     | Bogobot         ] Logged in as Bogobot-Testing#8298 (ID: 1499874423019409599)
[May 16 21:18:34.735 INFO     | Bogobot         ] Beginning automatic account creation...
[May 16 21:18:34.735 INFO     | Bogobot         ] Automatically created 0 accounts out of 4 members from REDACTED
[May 16 21:18:34.735 INFO     | Bogobot         ] Automatically created 0 accounts out of 16 members from Bogobot development (1495827707085197385)
[May 16 21:18:34.736 INFO     | Bogobot         ] Automatic account creation finished. Automatically created a total of 0 accounts out of a total of 20 members from 2 servers
```

## Features
Bogobot implements several slash commands for stream management and data retrieval:
 * /get_stats: Retrieves current shuffles, comparisons, and calculated uptime.
 * /archive: Shows archived monitor values.
 * /top, /bottom, /middle: Shows sortoff leaderboard slices.
 * /manage monitor: Starts, stops, or resends a persistent tracking system for stream serial numbers.
 * /manage leaderboard_monitor: Starts, stops, or resends a persistent top leaderboard message.
 * /manage milestones: Subscribes/unsubscribes milestone notifications, or spoofs/deletes milestone values.
 * /milestone_info: Shows recent milestone history and frame images.
 * /manage announce: Sends a simple bot-authored announcement.
 * /manage message: Deletes, pins, edits, replies to, or reacts to a message by ID.
 * /manage state: Stops or restarts the bot process.
 * /manage logs and /manage telemetry: Shows recent in-memory logs or command activity.
 * /usage: Shows command usage totals.
 * /avatar and /ping: Small Discord utility commands.
 * /roll and friends: Random number, choice, sort, shuffle, text bogo, and small bogosort utilities.
 * /accounts: Manages account permission ranks.

Bogobot also writes an append-only monitor archive when observed sort values are available.
Each chunk starts with a JSON header line, then compact `dt,value;` records where `dt`
is centiseconds since the previous archived value.

## Documentation
For technical details regarding the internal API, OCR configuration, channel proxies, and plugin development, refer to `DOCS.md`.
