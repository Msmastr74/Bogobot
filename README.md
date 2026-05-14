# Bogobot

Bogobot is a specialized Discord bot designed for monitoring the [24/7 Bogosort Livestream](https://www.youtube.com/live/DgfiqGPmGWY). The bot uses OCR and stream-derived timing to provide high-accuracy statistics directly from the stream.

## Prerequisites

Python 3.10+ is required. The dependency scripts install the usual system tools:
Tesseract, FFmpeg, Streamlink, and the Python packages used by the bot.

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
 * `fallback_client`: Optional fallback client after fatal startup/runtime errors. Defaults to true.
 * `save_live_frame`: Optional debug setting. When true, the bot writes the latest stream frame to `live_720p.png` after each received frame. Defaults to false.
 * `milestone_initialize_format`: Optional message template for new milestones.
 * `milestone_update_format`: Optional message template for milestone changes.
 * `telemetry_path`: Optional JSONL ("JSON Lines", one JSON record on each line) path for command telemetry. Defaults to `telemetry.jsonl`.

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

In your terminal output, you should see this:
```log
[08 22:17:05.832 WARNING  | discord.client  ] PyNaCl is not installed, voice will NOT be supported
[08 22:17:05.832 WARNING  | discord.client  ] davey is not installed, voice will NOT be supported
[08 22:17:05.837 INFO     | Bogobot         ] Loaded Plugin: monitor.py
[08 22:17:05.837 INFO     | Bogobot         ] Loaded Plugin: top10.py
[08 22:17:05.838 INFO     | Bogobot         ] Loaded Plugin: getinfo.py
[08 22:17:05.838 INFO     | Bogobot         ] Loaded Plugin: milestones.py
[08 22:17:05.838 INFO     | Bogobot         ] Loaded Plugin: authorize.py
[08 22:17:05.839 INFO     | Bogobot         ] Loaded Plugin: get_all_stats.py
[08 22:17:05.839 INFO     | Bogobot         ] Loaded Plugin: activity.py
[08 22:17:05.843 INFO     | Bogobot         ] Loaded Plugin: telemetry.py
[08 22:17:05.843 INFO     | Bogobot         ] Loaded Plugin: roll.py
[08 22:17:05.844 INFO     | Bogobot         ] Loaded Plugin: contextmenu.py
[08 22:17:05.844 INFO     | discord.client  ] logging in using static token
[08 22:17:06.541 INFO     | discord.gateway ] Shard ID None has connected to Gateway (Session ID: e971cb07a3df4800a5201371544de28f).
[08 22:17:08.545 INFO     | Bogobot         ] Logged in as Bogobot-Testing#8298 (ID: 1499874423019409599)
```

## Features
Bogobot implements several slash commands for stream management and data retrieval:
 * /get_stats: Retrieves current shuffles, comparisons, and calculated uptime.
 * /manage monitor: Starts or stops a persistent tracking system for stream serial numbers.
 * /manage milestones: Subscribes/unsubscribes milestone notifications, or spoofs/deletes milestone values.
 * /roll: A random number generation utility.
 * /accounts: Manages account permission ranks.
## Documentation
For technical details regarding the internal API, OCR configuration, channel proxies, and plugin development, refer to `DOCS.md`.
