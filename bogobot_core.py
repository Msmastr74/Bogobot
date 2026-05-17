import discord
from discord import app_commands
import json
import hashlib
from PIL import Image
import os
import functools
import asyncio
import importlib
import contextvars
import time
from typing import Any, Callable, TYPE_CHECKING, Concatenate, cast
from ocr import LibTesseractOCR, TESSDATA_FAST_URL
from stream import StreamHandler
from utils.edit_coalescer import EditCoalescer
from utils.notifications import NotificationBroadcaster
from utils.type import P, T, Coro
from utils.callbacks import CallbackRegistry, AsyncCallback
import logging
from plugins.admin import MEMORY_LOG_HANDLER

CONSOLE_LOG_FORMAT = '[%(asctime)s.%(msecs)03d %(levelname)-8s | %(name)-15s ] %(message)s'
LOG_DATE_FORMAT = '%b %d %H:%M:%S'

class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: "\x1b[90m",
        logging.INFO: "\x1b[36m",
        logging.WARNING: "\x1b[33m",
        logging.ERROR: "\x1b[31m",
        logging.CRITICAL: "\x1b[1;31m",
    }
    RESET = "\x1b[0m"

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        color = self.LEVEL_COLORS.get(record.levelno)
        if not color:
            return message
        return f"{color}{message}{self.RESET}"

CONSOLE_LOG_HANDLER = logging.StreamHandler()
CONSOLE_LOG_HANDLER.setFormatter(ColorFormatter(CONSOLE_LOG_FORMAT, LOG_DATE_FORMAT))
logging.basicConfig(
    level=logging.INFO,
    handlers=[CONSOLE_LOG_HANDLER, MEMORY_LOG_HANDLER],
)
logging.captureWarnings(True)

current_interaction: 'contextvars.ContextVar[discord.Interaction | None]' = contextvars.ContextVar(
    "current_interaction", default=None
)

if TYPE_CHECKING:
    from plugins.milestones import MilestoneTracker
    from plugins.telemetry import CommandTelemetryBase, CommandTelemetryEvent
    from plugins.accounts import Account

class BotCore(discord.Client):
    def __init__(self, config_path='config.json'):
        self.config_path = config_path
        with open(self.config_path, 'r') as f:
            self.config: dict[str, Any] = json.load(f)

        self.accounts_path: str = self.config.get("accounts_path", "accounts.json")
        if not os.path.exists(self.accounts_path):
            with open(self.accounts_path, 'w') as f:
                json.dump({}, f)
        
        with open(self.accounts_path, 'r') as f:
            self.accounts: dict[str, 'Account'] = json.load(f)
        
        self._config_lock = asyncio.Lock()
        self._accounts_lock = asyncio.Lock()
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        
        self.CELL_COORDS = (1170, 665, 1195, 685)
        self.CELL_OFFSET = 37 # x offset per historical cell
        self.SORT_AREA_COORDS = (75, 60, 1205, 575)
        self.SORT_CHANGE_THRESHOLD: float = self.config.get("sort_change_threshold", 0.1)
        self.OCR_CELL_COUNT: int = max(1, int(self.config.get("ocr_cell_count", 2)))

        self.STATS_COORDS: dict[str, 
                                tuple[int, int, int, int] |
                                tuple[int, int, int, int, str] |
                                tuple[int, int, int, int, tuple[str | None, int | None]]] = {
            "shuffles": (81, 610, 312, 640),
            "comparisons": (331, 610, 551, 640),
            "best_run": (645, 610, 730, 640, "0123456789/"),
            "shuffles_sec": (819, 610, 1043, 640),
            "average_best_shuffle": (80, 670, 115, 685, "0123456789."),
            "uptime": (1160, 10, 1260, 30, "0123456789dhm ")
        }
        self.THRESHOLD = 165
        self.current_vals: list[tuple[str, float]] = []
        self._current_vals_updated: bool = False
        self.stats: dict[str, str] = {}
        self.monitor_message = None
        
        self.debug: bool = self.config.get("debug", False)
        self.logger = logging.getLogger("Bogobot")
        loglevel = logging.DEBUG if self.debug else logging.INFO
        self.logger.setLevel(loglevel)
        self.ocr = LibTesseractOCR(
            tessdata_path=self.config.get("tessdata_path", "tessdata"),
            tessdata_fast_url=self.config.get("tessdata_fast_url", TESSDATA_FAST_URL),
            save_debug=self.config.get("save_ocr_debug", False),
            logger=self.logger.getChild("OCR"),
            library_path=self.config.get("libtesseract_path"),
            max_workers=max(1, int(self.config.get("ocr_concurrency", 2))),
        )

        self.stream_handler = StreamHandler(
            url="https://www.youtube.com/live/DgfiqGPmGWY",
            quality="720p",
            on_new_frame=self.on_new_frame,
            fps=float(self.config.get("fps", 1)),
            cookies=self._streamlink_cookies(),
            http_headers=self._streamlink_http_headers(),
            quiet=self.config.get(
                "silence_stream", False) or not self.debug,
            logger=self.logger.getChild("Stream"),
        )
        
        self.discord = self._Discord(self)
        self.setup = self._Setup(self)
        self._last_ocr_refresh: float = 0.0
        
        channel_data = self._load_channels_config()
        async def save_channels(data: dict[str, Any]):
            self.config["channels"] = data
            await self.save_config()
        self.edits = EditCoalescer(
            logger=self.logger.getChild("EditCoalescer"),
        )
        self.notifications = NotificationBroadcaster(
            self, subscriptions=channel_data,
            save_subscriptions=save_channels,
            logger=self.logger.getChild("Notifications"),
        )
        self._connected = False
        
        self.event(self.on_ready)
        self.callbacks = CallbackRegistry()
        
        self.milestones: 'MilestoneTracker | None' = None
    
    async def get_stream_uptime(self):
        raw_seconds = round(time.time())
        seconds_since = int(raw_seconds) - 1776273017
        minutes = seconds_since // 60
        seconds_since = seconds_since % 60
        hours = minutes // 60
        minutes = minutes % 60
        days = hours // 24
        hours = hours % 24
        return f"{days:02}:{hours:02}:{minutes:02}:{seconds_since:02}"

    async def get_best_shuffles(self):
        is_new = self._current_vals_updated
        self._current_vals_updated = False
        return self.current_vals, is_new

    def _save_config_sync(self):
        tmp_path = f"{self.config_path}.tmp"

        with open(tmp_path, 'w') as f:
            json.dump(self.config, f, indent=4)

        os.replace(tmp_path, self.config_path)

    def _load_channels_config(self) -> dict[str, Any]:
        channel_data = self.config.get("channels")

        if isinstance(channel_data, dict):
            return channel_data

        channel_data = {}
        legacy_path = "channels.json"

        if os.path.exists(legacy_path):
            try:
                with open(legacy_path, 'r') as f:
                    legacy_data = json.load(f)
                if isinstance(legacy_data, dict):
                    channel_data = legacy_data
            except (OSError, json.JSONDecodeError):
                channel_data = {}

        self.config["channels"] = channel_data
        self._save_config_sync()
        return channel_data

    def authorization_level(self, user_id: int) -> int:
        if str(user_id) not in self.accounts:
            return 0

        rank = self.accounts[str(user_id)]["perm_level"]
        return rank

    def is_authorized(self, user_id: int, perm_requirement: int) -> bool:
        return self.authorization_level(user_id) >= perm_requirement
    
    def init_callback(self, callback: AsyncCallback[[]]):
        self.callbacks.register('init', callback)
        return callback

    def close_callback(self, callback: AsyncCallback[[]]):
        self.callbacks.register('close', callback)
        return callback

    def _streamlink_cookies(self) -> list[str]:
        cookies = self.config.get("cookies")
        if cookies is None:
            return []
        if isinstance(cookies, dict):
            return [
                f"{name}={value}"
                for name, value in cookies.items()
            ]
        if isinstance(cookies, list):
            return [str(cookie) for cookie in cookies]
        self.logger.warning("Ignoring config cookies because it is not a list or object")
        return []

    def _streamlink_http_headers(self) -> list[str]:
        headers = self.config.get("http_headers")
        if headers is None:
            headers = self.config.get("headers")
        if headers is None:
            return []
        if isinstance(headers, dict):
            return [
                f"{name}={value}"
                for name, value in headers.items()
            ]
        if isinstance(headers, list):
            return [str(header) for header in headers]
        self.logger.warning("Ignoring config http_headers because it is not a list or object")
        return []

    def command_telemetry_callback(
        self,
        callback: AsyncCallback[["CommandTelemetryEvent"]],
    ):
        self.callbacks.register('command_telemetry', callback)
        return callback

    def on_new_frame_callback(
        self,
        callback: AsyncCallback[[Image.Image]]
    ):
        self.callbacks.register('on_new_frame', callback)
        return callback
    
    async def on_new_frame(self, img: Image.Image):
        await self.callbacks.execute_async('on_new_frame', img)
    
    class _Discord:
        def __init__(self, outer: 'BotCore'):
            self.outer = outer
            self._app_emoji_cache: dict[str, discord.Emoji] | None = None

        async def send(
            self,
            contents=None,
            *,
            response=False,
            ephemeral=False,
            safety_filter=False,
            **kwargs,
        ):
            if contents is not None and "content" not in kwargs:
                kwargs["content"] = contents
            if safety_filter:
                kwargs["allowed_mentions"] = discord.AllowedMentions.none()
                kwargs["suppress_embeds"] = True

            message = await self._send(
                response=response,
                ephemeral=ephemeral,
                **kwargs,
            )
            if message is None:
                return None
            
            return self.MessageHandle(
                self.outer,
                message,
                content=kwargs.get("content"),
                embed=kwargs.get("embed"),
            )

        async def send_embed(
            self,
            description=None,
            *,
            embed: discord.Embed | None = None,
            title="embed",
            footer="",
            author="Bogobot",
            color=discord.Color.blue(),
            response=False,
            ephemeral=False,
            **kwargs,
        ):
            """
            Deprecated compatibility helper.

            New bot-authored UI should use discord.ui.LayoutView via
            bot.discord.send(view=...). Keep this for older plugins and
            workflows that intentionally need Discord embeds. New code that
            truly needs an embed should prefer bot.discord.send(embed=...).
            """
            if embed is None:
                embed = discord.Embed(
                    title=title,
                    description=description,
                    color=color,
                )
                embed.set_footer(text=footer)
                embed.set_author(name=author)

            message = await self._send(
                response=response,
                ephemeral=ephemeral,
                embed=embed,
                **kwargs,
            )
            if message is None:
                return None
            
            return self.MessageHandle(self.outer, message, content=kwargs.get("content"), embed=embed)

        async def _send(
            self,
            response=False,
            ephemeral=False,
            **kwargs,
        ):
            interaction = current_interaction.get()

            if response and interaction:
                if interaction.response.is_done():
                    return await interaction.followup.send(wait=True, ephemeral=ephemeral, **kwargs)

                await interaction.response.send_message(ephemeral=ephemeral, **kwargs)
                return await interaction.original_response()

            if interaction and hasattr(interaction.channel, 'send'):
                return await interaction.channel.send(**kwargs) # pyright: ignore

            return None

        class MessageHandle:
            def __init__(
                self,
                outer: "BotCore",
                message: discord.Message,
                content=None,
                embed: discord.Embed | None = None,
            ):
                self.outer = outer
                self.message: discord.Message | None = message
                self.content = content
                self.embed = embed

            @property
            def exists(self) -> bool:
                return self.message is not None
            
            @property
            def message_id(self):
                return self.message.id if self.message else None

            async def edit(self, contents=None, safety_filter=False, **kwargs):
                if not self.message:
                    return

                if contents is not None and "content" not in kwargs:
                    kwargs["content"] = contents
                
                if safety_filter:
                    kwargs["allowed_mentions"] = discord.AllowedMentions.none()
                    kwargs["suppress"] = True

                try:
                    self.content = kwargs.get("content", self.content)
                    if isinstance(kwargs.get("embed"), discord.Embed):
                        self.embed = kwargs["embed"]
                    await self.message.edit(**kwargs)
                except discord.NotFound:
                    self.message = None

            async def edit_embed(
                self,
                description=None,
                *,
                embed: discord.Embed | None = None,
                title=None,
                footer=None,
                author=None,
                color=None,
                add_field=False, name=None, value=None, inline=False,
                **kwargs,
            ):
                """
                Deprecated compatibility helper.

                New bot-authored UI should edit LayoutView payloads through
                message.edit(view=...). Keep this for older plugins and
                workflows that intentionally need Discord embeds. New code
                that truly needs an embed should prefer message.edit(embed=...).
                """
                if not self.message:
                    return

                new_embed = embed or self._updated_embed(
                    description=description,
                    title=title,
                    footer=footer,
                    author=author,
                    color=color,
                    add_field=add_field,
                    name=name,
                    value=value,
                    inline=inline
                )

                try:
                    self.embed = new_embed
                    await self.message.edit(embed=new_embed, **kwargs)
                except discord.NotFound:
                    self.message = None

            def _updated_embed(
                self, description=None, title=None, footer=None, author=None, color=None,
                add_field=False, name=None, value=None, inline=False
            ):
                old = self.embed or discord.Embed()
                new_embed = discord.Embed.from_dict(old.to_dict())
                if title is not None:
                    new_embed.title = title
                if description is not None:
                    new_embed.description = description
                if color is not None:
                    new_embed.colour = color

                if add_field:
                    new_embed.add_field(name=name, value=value, inline=inline)

                current_author = old.author.name if old.author else ""
                new_embed.set_author(name=author or current_author)
                current_footer = old.footer.text if old.footer else ""
                new_embed.set_footer(text=footer or current_footer)

                return new_embed

            async def delete(self):
                if not self.message:
                    return

                try:
                    await self.message.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass
                finally:
                    self.message = None
            
            def _resolve_emoji(self, emoji_data: int | discord.Emoji | 'BotCore._Discord.Emoji' | str | None):
                if isinstance(emoji_data, BotCore._Discord.Emoji):
                    emoji_data = emoji_data.emoji
                emoji = self.outer.get_emoji(emoji_data) if isinstance(emoji_data, int) else emoji_data
                if not emoji:
                    self.outer.logger.warning(f"Emoji with ID {emoji_data} not found.")
                    return None
                return emoji
            
            async def add_reaction(self, emoji_data: int | discord.Emoji | 'BotCore._Discord.Emoji' | str | None):
                if not self.message:
                    return
                
                emoji = self._resolve_emoji(emoji_data)
                if not emoji:
                    return

                try:
                    await self.message.add_reaction(emoji)
                except discord.NotFound:
                    self.message = None
                except discord.Forbidden:
                    pass
            async def remove_reaction(self, emoji_data: int | discord.Emoji | 'BotCore._Discord.Emoji' | str | None):
                if not self.message:
                    return
                
                if not self.outer.user:
                    self.outer.logger.warning("Attempted to remove reaction while `bot.user` is None.")
                    return
                
                emoji = self._resolve_emoji(emoji_data)
                if not emoji:
                    return

                try:
                    await self.message.remove_reaction(emoji, self.outer.user)
                except discord.NotFound:
                    self.message = None
                except discord.Forbidden:
                    pass
        
        async def cleanup_defer_status(self, interaction: discord.Interaction):
            deferred_types = [
                discord.InteractionResponseType.deferred_channel_message,
                discord.InteractionResponseType.deferred_message_update
            ]
            
            if interaction.response.is_done() and interaction.response.type in deferred_types:
                try:
                    msg = await interaction.original_response()
                    if not msg.flags.ephemeral:
                        await interaction.delete_original_response()
                except discord.HTTPException:
                    pass
        
        class Emoji:
            def __init__(self, outer: "BotCore", app_emoji_name: str):
                self.name = app_emoji_name
                self.outer = outer
            
            @property
            def emoji(self) -> discord.Emoji | None:
                return self.outer.discord._get_emoji(self.name)
            
            def __str__(self) -> str:
                if not self.emoji:
                    return f":{self.name}:"
                return str(self.emoji)
        
        def _get_emoji(self, app_emoji_name: str):
            if self._app_emoji_cache is None:
                raise RuntimeError("Emoji cache not initialized")
            return self._app_emoji_cache.get(app_emoji_name.lower())
        
        def get_emoji(self, app_emoji_name: str):
            return self.Emoji(self.outer, app_emoji_name)
        
        async def init(self):
            if self._app_emoji_cache is not None:
                return
            
            self._app_emoji_cache = {}
            emojis = await self.outer.fetch_application_emojis()
            for emoji in emojis:
                self._app_emoji_cache[emoji.name.lower()] = emoji

    async def setup_hook(self):
        command_tree_hash = self._command_tree_hash()
        sync_forced = bool(self.config.get('sync', False))
        sync_needed = self.config.get("command_tree_hash") != command_tree_hash

        if sync_forced or sync_needed:
            reason = "forced" if sync_forced else "command tree changed"
            self.logger.info(f"Syncing Discord command tree ({reason})")
            await self.tree.sync()
            self.config['sync'] = False
            self.config["command_tree_hash"] = command_tree_hash
            await self.save_config()

    def _command_tree_hash(self) -> str:
        commands = [
            command.to_dict(self.tree)
            for command in self.tree.get_commands()
        ]
        payload = json.dumps(commands, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    
    async def on_ready(self):
        assert self.user is not None
        self.logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        
        if self._connected:
            return # Prevent multiple on_ready calls from causing issues
        self._connected = True
        
        await self.discord.init()

        try:
            await self.notifications.initialize()
        except Exception:
            self.logger.exception("Failed initializing notifications")
        
        await self.callbacks.execute_async('init')
        guild_count = 0
        member_count = 0
        added_member_count = 0
        guild_member_count = 0
        added_guild_member_count = 0
        self.logger.info("Beginning automatic account creation...")
        for guild in self.guilds:
            guild_count += 1
            for member in guild.members:
                guild_member_count += 1
                member_count += 1
                if str(member.id) not in self.accounts:
                    added_member_count += 1
                    added_guild_member_count += 1
                    self.accounts[str(member.id)] = {"perm_level": 0}
            self.logger.info(f"Automatically created {added_guild_member_count} accounts out of {guild_member_count} members from {guild.name} ({guild.id})")
            guild_member_count = 0
            added_guild_member_count = 0
        
        owner_uid = str(self.config["owner_uid"])
        for uid, account in self.accounts.items():
            if account["perm_level"] == 4 and uid != owner_uid:
                account["perm_level"] = 3
        if owner_uid in self.accounts:
            self.accounts[owner_uid]["perm_level"] = 4

        await self.save_accounts()
        self.logger.info(f"Automatic account creation finished. Automatically created a total of {added_member_count} accounts out of a total of {member_count} members from {guild_count} servers")

    async def load_plugins(self, folder_name="plugins"):
        logger = self.logger.getChild("Plugins")
        logger.info("Loading plugins...")
        if not os.path.exists(folder_name):
            os.makedirs(folder_name)
        for filename in os.listdir(folder_name):
            if filename.endswith(".py"):
                module_name = f"{folder_name}.{filename[:-3]}"

                try:
                    mod = importlib.import_module(module_name)
                except Exception:
                    logger.exception(
                        f"Importing plugin {module_name} failed with error"
                    )
                else:
                    if hasattr(mod, "setup"):
                        setup: Callable[[BotCore], Coro[None]] = mod.setup
                        try:
                            await setup(self)
                            logger.info(f"Loaded Plugin: {filename}")
                        except Exception:
                            logger.exception(
                                f"Executing {setup.__qualname__} failed with error"
                            )
                    else:
                        logger.info(f"Loaded Plugin: {filename} (no setup function)")
        logger.info("Finished loading plugins.")
    
    async def save_config(self):
        async with self._config_lock:
            self._save_config_sync()

    async def save_accounts(self):
        async with self._accounts_lock:
            tmp_path = f"{self.accounts_path}.tmp"
            with open(tmp_path, 'w') as f:
                json.dump(self.accounts, f, indent=4)
            os.replace(tmp_path, self.accounts_path)

    async def run_bot(self):
        self.stream_handler.async_loop = self.loop
        self.stream_handler.start()
        await self.start(self.config['bot_token'])

    async def close(self):
        self.logger.info("Shutting down bot...")
        await self.callbacks.execute_async('close')
        self.stream_handler.stop()
        self.ocr.close()
        await self.edits.close()
        await self.notifications.close()
        await super().close()

    class _Setup:
        def __init__(self, outer: 'BotCore'):
            self.outer = outer
            self.groups: dict[str, discord.app_commands.Group] = {}
        
        _Callable = Callable[Concatenate[discord.Interaction, P], Coro[T]]
        _Command = discord.app_commands.Command[discord.app_commands.Group, P, T]
        
        def command(
            self, name: str, *, description="No description", perm_requirement=1,
            eph=True, defer=True
        ) -> Callable[[_Callable], _Command]:
            def decorator(
                func: 'BotCore._Setup._Callable'
            ) -> 'BotCore._Setup._Command':
                @functools.wraps(func)
                async def wrapper(interaction: discord.Interaction, *args, **kwargs):
                    await self._run_command(
                        interaction,
                        func,
                        args,
                        kwargs,
                        perm_requirement=perm_requirement,
                        eph=eph,
                        defer=defer,
                    )
                return self.outer.tree.command(
                    name=name, description=description
                )(cast(BotCore._Setup._Callable, wrapper))
            return decorator

        class _CommandGroup:
            def __init__(
                self,
                setup: "BotCore._Setup",
                group: discord.app_commands.Group,
            ):
                self.setup = setup
                self.group = group
            
            def command(
                self, name: str, *, description="No description", perm_requirement=1,
                eph=True, defer=True
            ) -> Callable[['BotCore._Setup._Callable'], 'BotCore._Setup._Command']:
                def decorator(
                    func: 'BotCore._Setup._Callable'
                ) -> 'BotCore._Setup._Command':
                    @functools.wraps(func)
                    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
                        await self.setup._run_command(
                            interaction,
                            func,
                            args,
                            kwargs,
                            perm_requirement=perm_requirement,
                            eph=eph,
                            defer=defer,
                        )
                    return self.group.command(
                        name=name, description=description
                    )(cast(BotCore._Setup._Callable, wrapper))
                return decorator

        def group(
            self,
            name: str | discord.app_commands.Group,
            description="No description",
        ) -> "BotCore._Setup._CommandGroup":
            group = self._get_group(name, description)
            assert group is not None
            return self._CommandGroup(self, group)

        def context_menu(
            self, name: str, *, perm_requirement=1,
            eph=True, defer=True
        ) -> Callable[[
                Callable[[discord.Interaction, discord.Member], Coro[Any]] |
                Callable[[discord.Interaction, discord.User], Coro[Any]] |
                Callable[[discord.Interaction, discord.Message], Coro[Any]] |
                Callable[[discord.Interaction, discord.Member | discord.User],
                         Coro[Any]]
            ], discord.app_commands.ContextMenu]:
            def decorator(func):
                @self.outer.tree.context_menu(name=name)
                @functools.wraps(func)
                async def wrapper(interaction: discord.Interaction, *args, **kwargs):
                    await self._run_command(
                        interaction,
                        func,
                        args,
                        kwargs,
                        perm_requirement=perm_requirement,
                        eph=eph,
                        defer=defer,
                    )
                return wrapper
            return decorator

        def _get_group(
            self,
            group: str | discord.app_commands.Group | None,
            description: str,
        ) -> discord.app_commands.Group | None:
            if group is None:
                return None

            if isinstance(group, discord.app_commands.Group):
                self.groups.setdefault(group.name, group)
                if self.outer.tree.get_command(group.name) is None:
                    self.outer.tree.add_command(group)
                return group

            group_obj = self.groups.get(group)
            if group_obj is None:
                group_obj = discord.app_commands.Group(
                    name=group,
                    description=description,
                )
                self.groups[group] = group_obj
                self.outer.tree.add_command(group_obj)

            return group_obj

        async def _run_command(
            self,
            interaction: discord.Interaction,
            func: Callable[Concatenate[discord.Interaction, ...], Coro[Any]],
            args,
            kwargs,
            *,
            perm_requirement,
            eph,
            defer,
        ):
            started_at = time.monotonic()
            command_obj = interaction.command
            command_name = (
                getattr(command_obj, "qualified_name", None) or
                getattr(command_obj, "name", None) or
                getattr(func, "__name__", "unknown")
            )
            base_event: CommandTelemetryBase = {
                "interaction_id": interaction.id,
                "command": command_name,
                "user_id": interaction.user.id,
                "username": str(interaction.user),
                "channel_id": interaction.channel_id,
                "time": 0
            }
            status = "ok"
            error: str | None = None

            token = current_interaction.set(interaction)
            allowed = self.outer.is_authorized(interaction.user.id, perm_requirement)

            try:
                await self.outer.callbacks.execute_async('command_telemetry', {
                    **base_event,
                    "phase": "start",
                    "time": int(time.time()),
                })
                if not allowed:
                    status = "unauthorized"
                    return await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
                if defer:
                    await interaction.response.defer(ephemeral=(eph))
                await func(interaction, *args, **kwargs)
            except Exception as e:
                status = "error"
                error = f"{type(e).__qualname__}: {str(e)}"
                if isinstance(e, discord.NotFound) and e.code == 10062:
                    status = "error"
                    error = f"Interaction timed out or was deleted. ({error})"
                else:
                    try:
                        if interaction.response.is_done():
                            await self.outer.discord.cleanup_defer_status(interaction)
                            await interaction.followup.send(f"⚠️ Error: {e}", ephemeral=True)
                        else:
                            await interaction.response.send_message(f"⚠️ Error: {e}", ephemeral=True)
                    except discord.HTTPException:
                        pass
            finally:
                await self.outer.callbacks.execute_async('command_telemetry', {
                    **base_event,
                    "phase": "end",
                    "time": int(time.time()),
                    "status": status,
                    "duration_ms": round((time.monotonic() - started_at) * 1000, 1),
                    "error": error,
                })
                current_interaction.reset(token)
