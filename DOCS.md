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
- `sort_change_threshold`: How much the sort visualization must change before the monitor treats it as a new frame. Defaults to 0.1.
- `sort_section_count`: Number of sort sections to classify for the monitor value. Defaults to 25.
- `sort_area_left`: Left x-coordinate of the sort area and horizontal strip used for sort-section classification. Defaults to 46.
- `sort_observed_top`: Top y-coordinate of the horizontal strip used for sort-section classification. Defaults to 515.
- `sort_area_top`: Top y-coordinate of the sort area. Defaults to 70.
- `sort_area_right`: Right x-coordinate of the sort area and horizontal strip used for sort-section classification. Defaults to 985.
- `sort_area_bottom`: Bottom y-coordinate of the sort area and horizontal strip used for sort-section classification. Defaults to 530.
- `stats_source`: Stats update source. Defaults to `api`. Set to `ocr` to use OCR-driven stat/sort updates.
- `bogostream_stats_api_url`: Bogostream stats API endpoint. Defaults to `https://bogo.swapjs.dev/api/stats`.
- `bogostream_stats_api_interval`: Seconds between Bogostream stats API polls when `stats_source` is `api`. Defaults to 1.
- `ocr_enabled`: Enables Tesseract OCR startup. Defaults to true only when `stats_source` is `ocr`.
- `ocr_concurrency`: Number of OCR worker threads. Each worker owns one persistent libtesseract API instance. Defaults to 2.
- `tessdata_path`: Local directory for bot-managed Tesseract language data. Defaults to `tessdata`.
- `tessdata_fast_url`: Download URL for the fast English Tesseract model. Defaults to the upstream `tessdata_fast` English model.
- `libtesseract_path`: Optional explicit path to the libtesseract shared library when auto-detection cannot find it.
- `code_sandbox_fuel`: WASI instruction fuel limit for `/python` and `/javascript`. Defaults to `25000000000`.
- `log_capacity`: Number of recent log records kept in memory for `/manage logs`. Defaults to 3000, minimum 100.
- `milestone_initialize_format`: Optional Python `Template` string for first-time milestone messages.
- `milestone_update_format`: Optional Python `Template` string for milestone update messages.
- `telemetry_path`: Path to the command telemetry JSONL file. Defaults to `telemetry.jsonl`.
- `telemetry_flush_interval`: Seconds to batch telemetry writes before flushing to disk. Defaults to 2.
- `archive`: Optional archive configuration object. See below for fields.
- `bogotree_path`: Path to the Bogotree puzzle-state JSON file. Defaults to `bogotree.json`.
- `fps`: Frames received per second.
- `ai`: Optional AI configuration object. See `AI.md` for setup, provider examples, and implementation notes.

Bot-managed storage:
- `command_tree_hash`: Stored command tree fingerprint used for automatic sync detection.
- `channels`: Notification topic subscriptions by Discord channel ID. Older `channels.json` data is imported into this field when `channels` is missing.
- `monitor_messages`: Persistent monitor message IDs by Discord channel ID.
- `leaderboard_monitor_messages`: Persistent leaderboard monitor message IDs by Discord channel ID.
- `stats_monitor_messages`: Persistent stats monitor message IDs by Discord channel ID.
- `live_chat_monitor_messages`: Persistent live-chat monitor message IDs by Discord channel ID.
- `ai_schedules`: Scheduled AI activity triggers by Discord channel ID.
- `milestones`: Latest confirmed value for each milestone name.

Bogotree storage:
- `bogotree.json`, or the file named by `bogotree_path`, stores Bogotree puzzle state.
- Per-user Bogotree leaderboard data is stored on accounts under the root-level `bogotree` field.

Account storage:
- `accounts.json`, or the file named by `accounts_path`, stores Discord user IDs mapped to account records.
- Each account record contains `perm_level` plus any plugin-owned root-level annotation fields.
- On ready, the bot creates basic accounts for visible guild members and saves the configured `owner_uid` at permission level 4.

Archive storage:
- `archive/monitor.bga`, or the file named by `archive.path`, stores compact monitor value chunks.
- `archive.video.dir` stores visual archive files. The active current-day file is `.ts`; old days are remuxed to `archive.video.final_format`. The active `.ts` uses a small `.start` sidecar for the first frame timestamp; finalized files store that timestamp as `bogobot_start_timestamp` container metadata.

`main.py` will use `local_config.json` when it exists. Otherwise it uses `config.json`.

Archive configuration:

```json
"archive": {
  "path": "archive/monitor.bga",
  "flush_interval": 60,
  "chunk_event_limit": 200,
  "video": {
    "enabled": false,
    "dir": "archive/video",
    "width": 640,
    "height": 360,
    "fps": 1,
    "crf": 36,
    "preset": "superfast",
    "tune": "animation",
    "keyint": 10,
    "final_format": "mkv"
  }
}
```

The compact monitor archive uses `path`, `flush_interval`, and `chunk_event_limit`. The visual archive records to daily appendable MPEG-TS `.ts` working files in `video.dir` when `video.enabled` is true or `/manage video_archive start` is used. `/manage video_archive start` and `restart` persist `video.enabled: true`; `stop` persists `false`, so recording state survives bot restarts. `video.crf` controls HEVC quality/size; higher values are smaller and lower quality. `36` is an aggressive archive default, not a required value. `video.tune` is passed to FFmpeg's `libx265 -tune` option, with `animation` as the default because the stream is mostly flat-color UI. Set it to `null` or an empty string to omit `-tune`. Current-day `.ts` files stay appendable across stops and restarts. When a recording rolls to a new day or an old `.ts` file is found on startup, the bot remuxes finished `.ts` files to `video.final_format` (`mkv`, `mp4`, or `ts`). The bot still accepts older top-level `archive_*` and `archive_video_*` keys as fallbacks.

## Discord Subclass
The `discord` subclass provides a simplified interface for interacting with the Discord API, specifically designed for use within plugins.

### Messages
* `send(contents, response=True)`: Sends a message. If `response` is true, it attempts to reply to the current interaction. Pass `view=...` for bot-authored `LayoutView` UI, or `embed=...` when an embed is needed. Returns a MessageHandle object or None.
* `message.edit(contents)`: Edits the message contents or kwargs such as `view=...` or `embed=...`.
* `message.delete()`: Deletes the message.

New bot-authored UI should use static `discord.ui.LayoutView` payloads with `bot.discord.send(view=...)`. Embeds are still supported through Discord's native `embed=...` and `embeds=...` send/edit keyword arguments, but the old `send_embed(...)` and `message.edit_embed(...)` compatibility helpers have been removed.

## OCR Implementation
Bogobot uses the Bogostream stats API by default. libtesseract OCR is still available as a fallback by setting `stats_source` to `ocr`; Tesseract only starts when `ocr_enabled` is true, which defaults to true for OCR mode and false for API mode.
- **Coordinates**: Stats are extracted from defined regions of a 720p frame.
- **Processing**: Frames are cropped with Pillow (PIL), pre-processed with OpenCV, and passed to persistent libtesseract API instances as raw grayscale image data.
- **OCR model**: The bot ensures `eng_fast.traineddata` exists in `tessdata_path`, downloading it from `tessdata_fast` on first startup when missing. Each OCR worker initializes libtesseract with that tessdata directory and `eng_fast`.
- **Whitelist**: Stat-specific whitelists are enforced to prevent formatting errors from phantom characters or background noise. Large-number stat crops allow suffixes such as `K`, `M`, `B`, `T`, `Q`, and `Qi`.
- **OCR calls**: Each crop is sent directly to an OCR worker with its own whitelist and page segmentation mode. There is no subprocess startup or TIFF/PNG piping.
- **Parallelism**: `ocr_concurrency` controls how many OCR workers are started. Workers run in threads, and each thread keeps its own initialized libtesseract API instance.
- **Debug frame**: If `save_live_frame` is true, `live_720p.png` is written on each received frame. It is useful for checking crop coordinates and stream state, but it is disabled by default to avoid constant disk writes on small systems such as Android/Termux.

## Stream Stats Pipeline
`plugins/stats.py` owns the live stream data pipeline. Its default source is the Bogostream stats API at `https://bogo.swapjs.dev/api/stats`. Set `stats_source` to `ocr` to use the older frame/OCR pipeline.

In API mode, `stats.py` polls `bogostream_stats_api_url` every `bogostream_stats_api_interval` seconds. The API response supplies lifetime engine/crowd totals, engine/crowd/combined rates, all-time best, the best snapshot in the latest tick, its source, active contributors, and the current record holder. The plugin accepts both the original flat response shape and the newer nested shape with `engine`, `crowd`, and `combined_tick` objects. The tick-best array is converted into `bot.sort_values`; green/red section state is derived by checking whether each section value equals its 1-based index. When that snapshot changes, the plugin emits the existing `bot.new_value(...)` callback so monitor, archive, and stats-monitor integrations continue to use the same event path.

In OCR mode, `stats.py` registers an `@bot.new_frame_callback`, so every decoded stream frame flows through the visual update path.

For each frame in OCR mode, `stats.py`:

- Records frame timing in debug logs.
- Optionally writes `live_720p.png` when `save_live_frame` is true.
- Runs the sort-change detector over `bot.SORT_AREA_COORDS`.
- OCRs the configured stat crops from `bot.STATS_COORDS`.
- Classifies a thin horizontal strip across the sort sections when the sort visualization changed.
- Reads the current sort values from the colored section areas when the sort visualization changed.
- Emits `bot.new_value(new_values, new_value)` when the sort visualization changed.
- Updates `bot.stats` and `bot._last_ocr_refresh`.
- Feeds milestone candidates to `MilestoneTracker` when milestones are enabled.

`bot.stats` is the current text cache for stream-wide values. API mode fills fields such as `shuffles`, `engine_total`, `crowd_total`, `shuffles_sec`, `engine_rate`, `crowd_rate`, `best_run`, `tick_best`, `tick_best_source`, `active_contributors`, and `record_holder`. OCR mode fills fields such as `shuffles`, `comparisons`, `best_run`, `shuffles_sec`, `average_best_shuffle`, and `uptime`. Commands like `/get_stats` read from this cache instead of fetching or OCRing on demand.

`stats.py` keeps the visual sort reader in one `SortSectionReader`. OCR mode uses it for stream events. `/get_sort` also uses this reader lazily from `get_stats.py` against the latest cached video frame, so `/get_sort` shows what the delayed YouTube video currently displays instead of the fresher API state.

`bot.best_shuffle_sections` is the latest per-section green/red classification as `list[bool]`. `sum(bot.best_shuffle_sections)` is the scalar best-shuffle count used by existing monitor/archive displays.

`bot.sort_values` is the latest API/OCR event sort permutation as `list[int]`. In OCR mode, `stats.py` crops `bot.SORT_AREA_COORDS`, masks the red/green sort blocks, cleans the mask with OpenCV morphology, measures the largest solid component in each configured section, and ranks those areas into values. An unreadable section is represented as `0`.

`bot.new_values` combines those two caches as `list[tuple[bool, int]]`, where each tuple is `(is_green, sort_value)` for the same section.

`bot.new_value(new_values, new_value)` publishes the latest calculated monitor event. In API mode it is emitted from changed API tick snapshots. In OCR mode, `new_value` is the number of green sections in `sort_section_count`, calculated from the configured observed strip instead of OCRing tiny history cells. OCR events fire only when the sort-change detector says the bar chart actually changed, which lets `/manage monitor` ignore repeated stale frames from the stream and publish actual state transitions.

`bot._last_ocr_refresh` is the UNIX timestamp of the latest successful stats-cache refresh. The name is historical; it is updated by both API and OCR modes.

### Stream Helpers
`BotCore.get_stream_uptime()` returns a calculated static uptime string in `DD:HH:MM:SS` format. It is based on the known stream start timestamp, not on OCR or the API, so `/get_stats` can show a stable elapsed-time value even when the live source does not provide uptime.

Persistent monitors should subscribe with `@bot.new_value_callback` and keep their own pending queue if they need to publish values on a periodic loop. This avoids shared mutable state and one-shot polling helpers.

## Stream Change Detection
In OCR mode, the monitor does not rely only on text OCR to decide whether the sort changed. The bot also crops the bar chart area, reduces it to approximate red/green/other pixels, and compares that signature with the previous frame.

`sort_change_threshold` controls how much of that signature must change before a new sort-section count is published for monitor data. A higher value ignores small effects like confetti or compression noise.

## Bot Message Helpers
`EditCoalescer` is used for persistent bot-managed messages such as monitor and leaderboard `LayoutView` messages.

Each coalescer belongs to one message. If several edits are queued before Discord receives them, only the newest pending edit is sent.

If Discord reports `NotFound` or `Forbidden` while editing a persistent message, the coalescer records that with `NotFound_or_Forbidden`. `PersistentChannelMonitor` treats that as stale state, removes the stored message ID, and closes the coalescer.

`NotificationBroadcaster` handles topic subscriptions and sends notifications to every channel subscribed to a topic. It stores subscriptions in the `channels` section of `config.json`.

`Tracker` is the small shared helper underneath this kind of stored Discord state. It loads raw stored IDs, normalizes them, validates live Discord access, and prunes stale entries.

`PersistentChannelMonitor` in `utils.monitoring` packages the common monitor state: stored channel-to-message IDs, stale message pruning, start/stop handling, coalesced edits, and a public `tick()` method. A monitor plugin provides an initial message payload and an update payload callback, then decides when to call `tick()`.

Monitor messages use static `LayoutView` payloads. The display logic stays encapsulated in one component, and `timeout=None` keeps discord.py from registering non-interactive static views for background dispatch.

`PersistentChannelMonitor.command(root, *args, **kwargs)` forwards its arguments to `root.command(...)` and registers a standard `action: Literal["start", "stop", "resend"]` command. This lets monitor plugins use either a `bot.setup` command group or any compatible command-decorator object.

Periodic monitors should own their own `utils.tasks.loop` and call `monitor.tick()` inside it. Event-driven monitors can call `tick()` directly from callbacks such as `@bot.new_value_callback`.

The stream monitor updates only after the sort-change test reports a real visual change. Repeated stale stream frames are ignored for monitor history, which keeps the monitor closer to actual state transitions than to the currently displayed stream frame.

## Milestones
`MilestoneTracker` watches named milestone values and notifies subscribed channels when a value changes. API-provided values are treated as authoritative and publish the latest value immediately. OCR-provided values still use a rolling stability window, so noisy OCR does not immediately publish a milestone.

Milestone history can include frame images when updates come from OCR or manually supplied images. API-mode milestone updates do not attach images because the API state is fresher than the delayed YouTube video frame, so an automatic frame would be misleading.

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

## Accounts
Account storage and permission logic live in `utils.accounts`, separate from the `/accounts` command plugin.

`AccountManager` owns the account JSON file, normalizes account records, and serializes writes through an async lock. It provides permission helpers such as `authorization_level(...)`, `is_authorized(...)`, and the atomic rank-edit helper used by `/accounts perm_edit`. The `/accounts` plugin registers a `connect_callback` to hydrate visible guild member accounts on every Discord ready event.

`accounts[uid]` returns an `Account` handle. Missing accounts read as a fake account with `perm_level: 0`. `account[key]` reads root-level account data, `await account.write(key, value)` creates the account if needed and writes under the manager lock, and `account.lock` exposes that lock for larger grouped operations. Plugin annotations are root-level account fields beside `perm_level`.

## Plugin System
Plugins are independent Python files located in the `/plugins` directory.

Plugins can register lifecycle callbacks through decorators on `BotCore`:

- `@bot.init_callback`: Runs after Discord login/setup, commonly used to initialize persistent monitors.
- `@bot.connect_callback`: Runs on every Discord ready event, before the one-time connected guard. Use it for state that should refresh after reconnects.
- `@bot.close_callback`: Runs during bot shutdown.
- `@bot.new_frame_callback`: Runs for each received stream frame. In OCR mode, `stats.py` uses this for visual sort detection, OCR, and milestone updates.
- `@bot.new_value_callback`: Runs when a plugin publishes a new observed sort value with `bot.new_value(...)`. The callback receives `new_values: list[tuple[bool, int]]`, `new_value: int`, and the observation timestamp as a Python epoch-time `float`.
- `@bot.command_telemetry_callback`: Runs for command telemetry events.
- `@bot.message_callback`: Runs for Discord messages after a plugin attaches `bot.on_message` as a Discord event.

Plugins can also register AI actions for @mentions and `/ai` with `@utils.ai.action(...)`. See `AI.md` for the AI action API, passive context requests, and runtime behavior.

Current plugin responsibilities:

- `accounts.py`: `/accounts` permission commands.
- `admin.py`: `/manage state`, `/manage logs`, and `/manage message`.
- `archival.py`: compact append-only archive for observed monitor values and `/archive`.
- `bogo.py`: random utility commands and small bogosort toys under `/bogo`, plus top-level `/sort` and random helpers.
- `bogotree.py`: collaborative random equalization puzzle.
- `bogoscramble.py`: Bogoscramble message/media utilities.
- `cbogo.py`: original collaborative community bogosort puzzle and leaderboard.
- `code_sandbox.py`: `/python` and `/javascript` WASI sandbox commands.
- `fun.py`: bot status bogoname loop and `/bogo name`.
- `get_stats.py`: `/get_stats`, `/get_sort`, and `/manage stats_monitor`.
- `leaderboard.py`: `/top`, `/bottom`, `/middle`, and `/manage leaderboard_monitor`.
- `live_chat.py`: `/manage live_chat` YouTube live-chat monitor.
- `milestones.py`: milestone tracking, notifications, `/manage milestones`, and `/milestone_info`.
- `monitor.py`: `/manage monitor`.
- `ai.py`: @mention and `/ai` dispatch, command execution, passive context request handling, and AI response history.
- `ai_activity.py`: scheduled or manual AI activity triggers.
- `stats.py`: Bogostream API/OCR stats cache updates, sort-state events, and milestone value feeding.
- `telemetry.py`: command telemetry collection, `/manage telemetry`, and `/usage`.
- `utility.py`: `/avatar`, `/ping`, and `/manage announce`.
- `utils/accounts.py`: Account storage, permission checks, and account annotations.
- `utils/transformers.py`: Slash-command transformers such as `ColourTransformer` and `IntTransformer`.

## Admin Commands
The admin plugin adds `/manage state` and `/manage logs`.

## Management Commands
Several management commands use an explicit action parameter instead of separate start/stop style commands:

- `/manage monitor start|stop|resend`: Creates, removes, or resends the persistent monitor message in the current channel. `resend` requires an existing accessible monitor message, sends the replacement first, then deletes the old message.
- `/manage leaderboard_monitor start|stop|resend`: Creates, removes, or resends a persistent top-leaderboard monitor in the current channel. It uses the same data as `/top` and refreshes about every two minutes. `resend` sends the replacement first, then deletes the old message.
- `/manage stats_monitor start|stop|resend`: Creates, removes, or resends a persistent stream-stats monitor in the current channel. It uses the same data as `/get_stats` and updates when new stream stats are available.
- `/manage live_chat start|stop|resend`: Creates, removes, or resends a persistent YouTube live-chat monitor in the current channel. It reads from the configured `TARGET_VIDEO_ID` through `pytchat` and retries with backoff.
- `/manage milestones subscribe|unsubscribe`: Adds or removes the current channel from milestone notifications.
- `/manage milestones spoof name [data] [min_count]`: Sets a milestone when `data` is provided, or deletes the milestone when `data` is omitted.
- `/manage milestones ratelimit_reset`: Clears the milestone notification rate limit.
- `/manage announce [title] [message] [message_container] [attachments_container] [accent_colour] [message_id] [attachment_1..attachment_10]`: Sends a bot-authored announcement with up to 10 files, or edits a bot-authored announcement in the current channel when `message_id` is provided. Announcement edits keep existing attachments and reject new attachment uploads. `message_id` uses `IntTransformer` because Discord snowflakes are too large for slash-command integer options. When `message` is omitted, Discord opens a modal for longer message entry. Media attachments render through `MediaGallery`; other attachments render as Components v2 file items. `attachments_container` places those attachment components inside the accent container. `accent_colour` accepts hex colours like `#57f287` or supported `discord.Colour` names such as `brand_green`, `red`, or `blurple`. Requires admin level 3.
- `/manage message delete|react|unreact|pin|unpin|edit|reply message_id [channel_id] [emoji] [content]`: Deletes, reacts to, removes a reaction from, pins, unpins, edits, or replies to a message using a partial message reference. `channel_id` defaults to the current channel. `emoji` is required for `react` and `unreact`; `content` is required for `edit` and `reply`. `delete` is restricted to messages sent by the bot.
- `/manage state stop|restart`: Stops the bot or restarts the current process. This command is owner-only.
- `/manage logs`: Shows recent in-memory bot logs.
- `/manage telemetry [commands]`: Shows recent command activity, optionally filtered by command names.
- `/manage video_archive start|stop|restart|status`: Starts, stops, restarts, or inspects visual stream archive recording. Video archive files are daily appendable MPEG-TS files in `archive.video.dir`.
- `/archive`: Shows archived monitor values with a public paginated view.
- `/archive_frame time`: Extracts a visual archive frame. `time` accepts epoch seconds, epoch milliseconds, `<t:...>`, or `<t:...:*>`.
- `/top`, `/bottom`, `/middle`: Shows leaderboard slices using `LayoutView` messages.
- `/get_stats`: Shows the current stream stats cache using a `LayoutView` message.
- `/get_sort`: Shows the latest color-extracted sort state from the currently cached video frame, including that frame image when available. This intentionally follows the delayed YouTube video instead of the fresher API state.
- `/python [code]` and `/javascript [code]`: Execute code in WASI-backed sandboxes. If code is omitted, a modal supports longer input and one uploaded source file. Output is chunked and truncated to Discord-safe limits.
- `/ai_activity schedule when purpose`: Schedules a one-off or recurring AI activity trigger in the current channel. `when` accepts relative times, Unix/Discord/ISO timestamps, or structured fields like `hour:12 minute:30`.
- `/ai_activity trigger purpose`: Runs an AI activity trigger immediately in the current channel.
- `/ai_activity list`: Lists scheduled AI activities for the current channel.
- `/ai_activity remove id`: Removes a scheduled AI activity by ID.
- `/milestone_info milestone_name [ephemeral]`: Shows the current milestone value and recent in-memory history, with recent frame images when available. API-mode history usually has no images.
- `/usage [commands]`: Shows command usage totals from telemetry.
- `/avatar [user]`: Shows a user's avatar.
- `/ping [user]`: Shows bot latency and can add a user latency measurement from that user's next message.
- `/randint`, `/randfloat`, `/randbool`, `/randlist`, `/sort`: Randomization and text/list utilities. `/sort` supports `numerical` and Unicode-collated `lexicographic` modes.
- `/bogo roll|bogo|shuffle|choice|name|sort|sort-list|sort-listr|sort-lexicographic`: Dice roll, text bogo, shuffle/choice, name bogo, and small animated bogosort commands. `/bogo sort-lexicographic` normalizes items with Unicode NFC and sorts strings with `pyuca` Unicode collation.
- `/cbogo [run|info|leaderboard|reset|reset_last_user] [target]`: Advances the original collaborative community bogosort puzzle, shows puzzle info, shows leaderboards, or resets the puzzle. `reset` requires mod rank.
- `/bogotree [run|info|leaderboard|reset] [target]`: Advances a collaborative random equalization puzzle. `info` shows state and pseudocode; `leaderboard` shows per-user calls, simulated steps, and best score, optionally forcing `target` into each leaderboard section; `reset` requires mod rank.

Account commands live under `/accounts`:

- `/accounts perm_edit promote|demote user`: Moves a user up or down by one rank.
- `/accounts perm_edit set user level`: Sets a user to `basic`, `authorized`, `mod`, or `admin`.
- `/accounts list_users [minimum_rank]`: Lists accounts, optionally filtered to users at or above a rank.
- `/accounts ban_mgr ban|unban user`: Bans or unbans an account by setting its permission level below/above the banned state.
- `/accounts info [user] [eph]`: Shows account information for a user, defaulting to the caller.

### Creating a Plugin
Each plugin must include a `setup` function to register commands with the `BotCore` instance:

```python
import discord
from bogobot_core import BotCore

async def setup(bot: BotCore):
    @bot.setup.command(name="example", description="An example command", perm_requirement=0)
    async def example(interaction: discord.Interaction):
        await bot.discord.send("Hello World", response=True)

```

### Creating a Grouped Command
Use `utils.groups` to get a shared command group, then register commands with `.command(...)`:

```python
import discord
from typing import Literal
from utils import groups

async def setup(bot):
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
from bogobot_core import BotCore

def tools(bot: BotCore):
    return bot.setup.group("tools", "Tool commands")
```

Plugins can then register commands on the group:

```python
from utils import groups
async def setup(bot):
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
