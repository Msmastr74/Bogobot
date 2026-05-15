# Bogobot Internal API Documentation

Bogobot is built on a modular architecture that separates core logic from command implementation. This documentation outlines the internal classes and methods available for extending the bot's functionality.

## BotCore Class
The `BotCore` class is the central manager for the bot, handling configuration, command tree synchronization, and plugin loading. It inherits from `discord.Client`.

### Configuration
Configuration is managed via `config.json`.

User-edited settings:
- `bot_token`: The Discord bot token.
- `owner_uid`: Discord user ID for the bot owner. On startup, this user is set to account permission level 4.
- `accounts_path`: Optional path to the account database. Defaults to `accounts.json`.
- `sync`: Optional one-run force sync for the command tree. The bot also syncs automatically when the local command tree hash changes, then writes this back to false.
- `debug`: Enable debug logging for Bogobot.
- `silence_stream`: Suppress Streamlink/FFmpeg subprocess output. Defaults to false, but stream output is also quiet unless `debug` is true.
- `cookies`: Optional Streamlink HTTP cookies. Use either a list of `name=value` strings or an object whose keys and values are converted to `name=value`. Each entry is passed as `--http-cookie`.
- `http_headers`: Optional Streamlink HTTP headers. Use either a list of `Name=value` strings or an object whose keys and values are converted to `Name=value`. Each entry is passed as `--http-header`. Useful for `User-Agent` or `Referer`.
- `save_ocr_debug`: Enable saving processed OCR crop images in `ocr_debug/`. Defaults to false.
- `save_live_frame`: Enable writing the latest received stream frame to `live_720p.png`. Defaults to false.
- `sort_change_threshold`: How much the sort visualization must change before the monitor treats it as a new frame. Defaults to 0.05.
- `ocr_concurrency`: Number of OCR worker threads. Each worker owns one persistent libtesseract API instance. Defaults to 1.
- `ocr_cell_count`: Number of latest history cells to OCR when the sort visualization changes. Defaults to 2.
- `tessdata_path`: Local directory for bot-managed Tesseract language data. Defaults to `tessdata`.
- `tessdata_fast_url`: Download URL for the fast English Tesseract model. Defaults to the upstream `tessdata_fast` English model.
- `libtesseract_path`: Optional explicit path to the libtesseract shared library when auto-detection cannot find it.
- `log_capacity`: Number of recent log records kept in memory for `/manage logs`. Defaults to 3000, minimum 100.
- `milestone_initialize_format`: Optional Python `Template` string for first-time milestone messages.
- `milestone_update_format`: Optional Python `Template` string for milestone update messages.
- `telemetry_path`: Path to the command telemetry JSONL file. Defaults to `telemetry.jsonl`.
- `telemetry_flush_interval`: Seconds to batch telemetry writes before flushing to disk. Defaults to 2.

Bot-managed storage:
- `command_tree_hash`: Stored command tree fingerprint used for automatic sync detection.
- `channels`: Notification topic subscriptions by Discord channel ID. Older `channels.json` data is imported into this field when `channels` is missing.
- `monitor_messages`: Persistent monitor message IDs by Discord channel ID.
- `leaderboard_monitor_messages`: Persistent leaderboard monitor message IDs by Discord channel ID.
- `milestones`: Latest confirmed value for each milestone name.

Account storage:
- `accounts.json`, or the file named by `accounts_path`, stores Discord user IDs mapped to account records.
- Each account record currently contains `perm_level`.
- On ready, the bot creates basic accounts for visible guild members and saves the configured `owner_uid` at permission level 4.

`main.py` will use `local_config.json` when it exists. Otherwise it uses `config.json`.

## Info Subclass
The `info` subclass handles data extraction from the livestream using a combination of OCR and API requests.

### Methods
* `get_stats_all()`: Returns a dictionary containing the most recent values for shuffles, comparisons, and calculated shuffles per minute.
* `get_uptime()`: Returns the current calculated stream uptime as a formatted `DD:HH:MM:SS` string.
* `format_to_ddhhmmss(total_seconds)`: Converts raw seconds into a standardized duration string.

## Discord Subclass
The `discord` subclass provides a simplified interface for interacting with the Discord API, specifically designed for use within plugins.

### Messages
* `send(contents, response=True)`: Sends a message. If `response` is true, it attempts to reply to the current interaction. Pass `view=...` for bot-authored `LayoutView` UI. Returns a MessageHandle object or None.
* `message.edit(contents)`: Edits the message contents or kwargs such as `view=...`.
* `message.delete()`: Deletes the message.

### Deprecated Embeds
New bot-authored UI should use static `discord.ui.LayoutView` payloads with `bot.discord.send(view=...)`. New code that truly needs embeds should pass `embed=...` to `send(...)` or `edit(...)` directly.

* `send_embed(description, title, color, footer, response=True)`: Deprecated compatibility helper for sending an embed. Returns a MessageHandle object or None.
* `message.edit_embed(description, title, author, add_field=False, name, value, inline=False)`: Deprecated compatibility helper for modifying an embed. Setting `add_field` to true appends a new field based on name, value, and inline.

## OCR Implementation
Bogobot utilizes libtesseract OCR for visual data extraction.
- **Coordinates**: Stats are extracted from defined regions of a 720p frame.
- **Processing**: Frames are cropped with Pillow (PIL), pre-processed with OpenCV, and passed to persistent libtesseract API instances as raw grayscale image data.
- **OCR model**: The bot ensures `eng_fast.traineddata` exists in `tessdata_path`, downloading it from `tessdata_fast` on first startup when missing. Each OCR worker initializes libtesseract with that tessdata directory and `eng_fast`.
- **Whitelist**: A strict digit-only whitelist is enforced to prevent formatting errors from phantom characters or background noise.
- **OCR calls**: Each crop is sent directly to an OCR worker with its own whitelist and page segmentation mode. There is no subprocess startup or TIFF/PNG piping.
- **Parallelism**: `ocr_concurrency` controls how many OCR workers are started. Workers run in threads, and each thread keeps its own initialized libtesseract API instance.
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

`PersistentChannelMonitor` in `utils.monitoring` packages the common monitor pattern: stored channel-to-message IDs, stale message pruning, start/stop handling, coalesced edits, and a periodic update loop. A monitor plugin only needs to provide an initial message payload and an update payload callback.

Monitor messages use static `LayoutView` payloads. The display logic stays encapsulated in one component, and `timeout=None` keeps discord.py from registering non-interactive static views for background dispatch.

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

## Management Commands
Several management commands use an explicit action parameter instead of separate start/stop style commands:

- `/manage monitor start|stop`: Creates or removes the persistent monitor message in the current channel.
- `/manage leaderboard_monitor start|stop`: Creates or removes a persistent top-leaderboard monitor in the current channel. It uses the same data as `/top` and refreshes about every two minutes.
- `/manage milestones subscribe|unsubscribe`: Adds or removes the current channel from milestone notifications.
- `/manage milestones spoof name [data] [min_count]`: Sets a milestone when `data` is provided, or deletes the milestone when `data` is omitted.
- `/manage milestones ratelimit_reset`: Clears the milestone notification rate limit.
- `/manage state stop|restart|info`: Stops the bot, restarts the current process, or reports which client is active.
- `/manage logs`: Shows recent in-memory bot logs.
- `/manage telemetry [commands]`: Shows recent command activity, optionally filtered by command names.

Account commands live under `/accounts`:

- `/accounts perm_info user`: Shows a user's current account rank.
- `/accounts perm_edit promote|demote user`: Moves a user up or down by one rank.
- `/accounts perm_edit set user level`: Sets a user to `basic`, `authorized`, `mod`, or `admin`.

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
 * **0 (`basic`)**: Default account rank. Public commands should use `perm_requirement=0`.
 * **1 (`authorized`)**: Trusted account rank. This is the default requirement for `bot.setup.command` and grouped commands.
 * **2 (`mod`)**: Elevated management rank.
 * **3 (`admin`)**: Highest manually assignable rank through `/accounts perm_edit`.
 * **4 (`owner`)**: Forced rank for `owner_uid` on startup.

When editing permissions, the caller must overrank both the target's current rank and the new rank.
