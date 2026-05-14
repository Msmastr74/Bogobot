# Bogobot Internal API Documentation

Bogobot is built on a modular architecture that separates core logic from command implementation. This documentation outlines the internal classes and methods available for extending the bot's functionality.

## BotCore Class
The `BotCore` class is the central manager for the bot, handling configuration, command tree synchronization, and plugin loading. It inherits from `discord.Client`.

### Configuration
Configuration is managed via `config.json`.

User-edited settings:
- `bot_token`: The Discord bot token.
- `owner_uid`: Discord user ID for the bot owner. Permission level 2 commands require this ID.
- `authorized_users`: Discord user IDs mapped to authorization levels. Level 0 is public/no authorization, levels 1 and 2 are configurable, and `owner_uid` is always effective level 3. Older list-style configs are migrated to level 1. This can also be changed with `/manage auth`.
- `sync`: Optional one-run force sync for the command tree. The bot also syncs automatically when the local command tree hash changes, then writes this back to false.
- `debug`: Enable debug logging for Bogobot.
- `silence_stream`: Suppress Streamlink/FFmpeg subprocess output. Defaults to false, but stream output is also quiet unless `debug` is true.
- `cookies`: Optional Streamlink HTTP cookies. Use either a list of `name=value` strings or an object whose keys and values are converted to `name=value`. Each entry is passed as `--http-cookie`.
- `http_headers`: Optional Streamlink HTTP headers. Use either a list of `Name=value` strings or an object whose keys and values are converted to `Name=value`. Each entry is passed as `--http-header`. Useful for `User-Agent` or `Referer`.
- `save_ocr_debug`: Enable saving processed OCR crop images in `ocr_debug/`. Defaults to false.
- `save_live_frame`: Enable writing the latest received stream frame to `live_720p.png`. Defaults to false.
- `sort_change_threshold`: How much the sort visualization must change before the monitor treats it as a new frame. Defaults to 0.05.
- `ocr_concurrency`: Maximum number of concurrent Tesseract batch processes to run at once. Defaults to 4.
- `ocr_cell_count`: Number of latest history cells to OCR when the sort visualization changes. Defaults to 2.
- `fallback_client`: Start the fallback client after a fatal main-bot failure. Defaults to true.
- `log_capacity`: Number of recent log records kept in memory for `/manage logs`. Defaults to 500, minimum 100.
- `milestone_initialize_format`: Optional Python `Template` string for first-time milestone messages.
- `milestone_update_format`: Optional Python `Template` string for milestone update messages.
- `telemetry_path`: Path to the command telemetry JSONL file. Defaults to `telemetry.jsonl`.
- `telemetry_flush_interval`: Seconds to batch telemetry writes before flushing to disk. Defaults to 2.

Bot-managed storage:
- `command_tree_hash`: Stored command tree fingerprint used for automatic sync detection.
- `channels`: Notification topic subscriptions by Discord channel ID. Older `channels.json` data is imported into this field when `channels` is missing.
- `monitor_messages`: Persistent monitor message IDs by Discord channel ID.
- `milestones`: Latest confirmed value for each milestone name.

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
- **Processing**: Frames are cropped with Pillow (PIL), pre-processed with OpenCV, and encoded as temporary PNG files for Tesseract.
- **Whitelist**: A strict digit-only whitelist is enforced to prevent formatting errors from phantom characters or background noise.
- **Batch mode**: Crops with the same whitelist and page segmentation mode are sent to Tesseract together using its native list-file batch input. This reduces process startup overhead while keeping separate batches for crops that require different OCR options.
- **Parallelism**: `ocr_concurrency` limits how many Tesseract batch processes can run at once, not how many individual crops can be read per frame.
- **Latest cells**: When the sort visualization changes, `ocr_cell_count` controls how many recent history cells are read for monitor updates.
- **Debug frame**: If `save_live_frame` is true, `live_720p.png` is written on each received frame. It is useful for checking crop coordinates and stream state, but it is disabled by default to avoid constant disk writes on small systems such as Android/Termux.

## Stream Change Detection
The monitor does not rely only on OCR to decide whether the sort changed. The bot also crops the bar chart area, reduces it to approximate red/green/other pixels, and compares that signature with the previous frame.

`sort_change_threshold` controls how much of that signature must change before the latest cell OCR is treated as new monitor data. A higher value ignores small effects like confetti or compression noise.

## Bot Message Helpers
`EditCoalescer` is used for persistent bot-managed messages such as monitor embeds.

Each coalescer belongs to one message. If several edits are queued before Discord receives them, only the newest pending edit is sent.

`NotificationBroadcaster` handles topic subscriptions and sends notifications to every channel subscribed to a topic. It stores subscriptions in the `channels` section of `config.json`.

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

## Admin Commands
The admin plugin adds `/manage state` and `/manage logs`.

`/manage state` has different permission behavior depending on which client is running:

- **Normal bot**: registered through the plugin command wrapper with permission level 2, so only `owner_uid` can run it. This is stricter than most `/manage` commands, which default to the authorized-user level. `restart` restarts the process, `stop` closes the main bot and starts the fallback client, and `info` reports that the main bot is up.
- **Fallback client**: registered directly on the fallback command tree and checks permission level 1, so `owner_uid` and users in `authorized_users` can run it.

In both modes, `restart` replies with `Restarting...`, waits briefly, closes the Discord client, and then replaces the current process with a fresh invocation of the same Python executable and command-line arguments.

If the main bot fails, or `/manage state stop` is used, and `fallback_client` is enabled, `main.py` starts a fallback client using the same bot token. In fallback mode, `/manage state restart` is available to authorized users and restarts the fallback process the same way, giving maintainers a Discord-side recovery path even when the main command tree is unavailable.

## Management Commands
Several management commands use an explicit action parameter instead of separate start/stop style commands:

- `/manage auth info user`: Shows a user's effective authorization level.
- `/manage auth set user level`: Sets a user's authorization level. Level 0 removes the user from `authorized_users`; set levels must be lower than the caller's effective level.
- `/manage monitor start|stop`: Creates or removes the persistent monitor message in the current channel.
- `/manage milestone subscribe|unsubscribe`: Adds or removes the current channel from milestone notifications.
- `/manage milestone spoof name [data]`: Sets a milestone when `data` is provided, or deletes the milestone when `data` is omitted.

### Creating a Plugin
Each plugin must include a `setup` function to register commands with the `BotCore` instance:

```python
import discord

async def setup(bot):
    @bot.setup.command(name="example", description="An example command", perm_requirement=0)
    async def example(interaction: discord.Interaction):
        await bot.discord.send("Hello World", response=True)

```

### Creating a Grouped Command
Use `utils.groups` to get a shared command group, then register commands with `.command(...)`:

```python
import discord
from typing import Literal

async def setup(bot):
    from utils import groups

    manage = groups.manage(bot)

    @manage.command(
        name="example",
        description="Run a grouped management action",
        perm_requirement=1,
    )
    async def example(
        interaction: discord.Interaction,
        action: Literal["info", "reset"],
    ):
        if action == "info":
            await bot.discord.send("Example info.", response=True)
            return

        await bot.discord.send("Example reset complete.", response=True)
```

`groups.manage(bot)` returns the shared `/manage` group. If the group does not exist yet, it is created and added to the command tree; later plugins reuse the same group object.

### Creating a Group Helper
Shared groups live in `utils/groups.py`. Add a small helper that calls `bot.setup.group(...)`, then import and use that helper from plugins:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from main import BotCore


def tools(bot: "BotCore"):
    return bot.setup.group("tools", "Tool commands")
```

Plugins can then register commands on the group:

```python
async def setup(bot):
    from utils import groups

    tools = groups.tools(bot)

    @tools.command(name="ping", description="Ping the tools group", perm_requirement=0)
    async def ping(interaction):
        await bot.discord.send("Pong.", response=True)
```

Keep group helpers tiny. They should only name and return the shared group; command behavior belongs in plugins.

### Permission Levels
 * **0 (Public)**: Accessible by all users.
 * **1 (Authorized)**: Accessible by users configured at level 1 or higher.
 * **2 (Admin)**: Accessible by users configured at level 2 or higher.
 * **3 (Owner)**: Effective level for `owner_uid`; not stored in `authorized_users`.

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
