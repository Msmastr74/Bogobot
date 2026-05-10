# Bogobot Internal API Documentation

Bogobot is built on a modular architecture that separates core logic from command implementation. This documentation outlines the internal classes and methods available for extending the bot's functionality.

## BotCore Class
The `BotCore` class is the central manager for the bot, handling configuration, command tree synchronization, and plugin loading. It inherits from `discord.Client`.

### Configuration
Configuration is managed via `config.json`. Key fields include:
- `bot_token`: The Discord bot token.
- `authorized_users`: A list of user IDs with elevated permissions.
- `sync`: Optional one-run force sync for the command tree. The bot also syncs automatically when the local command tree hash changes.
- `command_tree_hash`: Stored command tree fingerprint used for automatic sync detection.
- `save_ocr_debug`: Enable/disable saving OCR debug images.
- `debug`: Enable/disable debug logging (loglevel).
- `silence_stream`: Suppress Streamlink/FFmpeg output.
- `channels_path`: Path to the channel usage store. Defaults to `channels.json`.
- `sort_change_threshold`: How much the sort visualization must change before the monitor treats it as a new frame.
- `ocr_concurrency`: Maximum number of Tesseract processes to run at once. Defaults to 2.
- `milestones`: Stores the latest confirmed value for each milestone name.
- `milestone_initialize_format`: Optional Python `Template` string for first-time milestone messages.
- `milestone_update_format`: Optional Python `Template` string for milestone update messages.
- `telemetry_path`: Path to the command telemetry JSONL file. Defaults to `telemetry.jsonl`.
- `telemetry_flush_interval`: Seconds to batch telemetry writes before flushing to disk. Defaults to 2.

`main.py` will use `local_config.json` when it exists. Otherwise it uses `config.json`.

## Info Subclass
The `info` subclass handles data extraction from the livestream using a combination of OCR and API requests.

### Methods
* `get_stats_all()`: Returns a dictionary containing the most recent values for shuffles, comparisons, and calculated shuffles per minute.
* `get_uptime()`: An asynchronous method that fetches the raw epoch timestamp from the YouTube Framework Update API and returns a formatted `DD:HH:MM:SS` string.
* `format_to_ddhhmmss(total_seconds)`: Converts raw seconds into a standardized duration string.

## Discord Subclass
The `discord` subclass provides a simplified interface for interacting with the Discord API, specifically designed for use within plugins.

### Messages
* `send(contents, response=True)`: Sends a plain text message. If `response` is true, it attempts to reply to the current interaction. Returns a MessageHandle object or None.
* `message.edit(contents)`: Edits the message contents.
* `message.delete()`: Deletes the message.

### Embeds
* `send_embed(contents, title, color, footer, response=True)`: Initializes and sends a new embed. Returns an MessageHandle object or None.
* `message.edit_embed(contents, title, author, add_field=False)`: Modifies the embed. Setting `add_field` to true will append a new field instead of editing the main body.

## OCR Implementation
Bogobot utilizes Tesseract OCR for visual data extraction.
- **Coordinates**: Stats are extracted from defined regions of a 720p frame.
- **Processing**: Frames are pre-processed using the Pillow (PIL) library, including grayscale conversion and thresholding, to improve recognition accuracy.
- **Whitelist**: A strict digit-only whitelist is enforced to prevent formatting errors from phantom characters or background noise.
- **Parallelism**: OCR calls are limited by `ocr_concurrency`, so multiple crops can be parsed without starting too many Tesseract processes.
- **Debug frame**: `live_720p.png` is written on each received frame. It is useful for checking crop coordinates and stream state.

## Stream Change Detection
The monitor does not rely only on OCR to decide whether the sort changed. The bot also crops the bar chart area, reduces it to approximate red/green/other pixels, and compares that signature with the previous frame.

`sort_change_threshold` controls how much of that signature must change before the latest cell OCR is treated as new monitor data. A higher value ignores small effects like confetti or compression noise.

## Bot Message Helpers
`EditCoalescer` is used for persistent bot-managed messages such as monitor embeds.

Each coalescer belongs to one message. If several edits are queued before Discord receives them, only the newest pending edit is sent.

`NotificationBroadcaster` handles topic subscriptions and sends notifications to every channel subscribed to a topic. It stores subscriptions in `channels.json` by default.

`Tracker` is the small shared helper underneath this kind of stored Discord state. It loads raw stored IDs, normalizes them, validates live Discord access, and prunes stale entries.

## Milestones
`MilestoneTracker` watches named milestone values and notifies subscribed channels when a value changes. Values are confirmed using a rolling window, so noisy OCR does not immediately publish a milestone.

Milestones are stored by display name:

```json
"milestones": {
  "Best run": "11/25"
}
```

The default messages are:

```text
$milestone_name initialized to `$new_value`.
$milestone_name updated from `$old_value` to `$new_value`.
```

These can be overridden in config. For example, to ping a role on updates:

```json
"milestone_update_format": "<@&role_id> $milestone_name updated from `$old_value` to `$new_value`."
```

The available template variables are `$milestone_name`, `$old_value`, and `$new_value`.

## Telemetry
The telemetry plugin records command completions to a JSONL file. Each line is one completed command event, so new events can be appended without rewriting the whole history.

The plugin keeps a small recent-action buffer for `/manage telemetry` and builds in-memory usage indexes at startup for `/usage`. New command events update those indexes as they arrive, which keeps `/usage` cheap even after the telemetry file grows.

## Plugin System
Plugins are independent Python files located in the `/plugins` directory.

### Creating a Plugin
Each plugin must include a `setup` function to register commands with the `BotCore` instance:

```python
import discord

async def setup(bot):
    @bot.setup.command(name="example", description="An example command", perm_requirement=0)
    async def example(interaction: discord.Interaction):
        await bot.discord.send("Hello World", response=True)

```
### Permission Levels
 * **0 (Public)**: Accessible by all users.
 * **1 (Authorized)**: Accessible by users listed in the authorized_users configuration.
 * **2 (Admin)**: Restricted to administrative tasks.

## Harness
`harness.py` is a small process wrapper for running the bot on a server.

It does three things:
- starts `main.py`
- runs `git pull --ff-only` every 10 seconds
- restarts the bot when the checked-out commit changes

This is useful when the bot is deployed somewhere simple and you want pushes to restart the app without logging in and doing it manually. It only accepts fast-forward pulls; if the server has local changes or git cannot pull cleanly, it keeps the current bot process running and prints the git error.

Run it with:

```bash
python harness.py
```

Stop it with Ctrl+C or SIGTERM. The harness will try SIGINT on the bot first, then terminate/kill it if it does not exit.
