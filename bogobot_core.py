import discord
from discord import app_commands
import json
import hashlib
import csv
import io
import numpy as np
from PIL import Image
import cv2
import os
import functools
import asyncio
import importlib
import contextvars
import aiohttp
import time
from typing import Any, Awaitable, Callable, TYPE_CHECKING, Concatenate, Coroutine, ParamSpec, TypeVar, cast
from stream import StreamHandler
from channel_proxy import ChannelProxyManager
import logging
from plugins.healthcheck import MEMORY_LOG_HANDLER

LOG_FORMAT = '[%(asctime)s.%(msecs)03d %(levelname)-8s | %(name)-15s ] %(message)s'
LOG_DATE_FORMAT = '%d %H:%M:%S'

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
CONSOLE_LOG_HANDLER.setFormatter(ColorFormatter())
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[CONSOLE_LOG_HANDLER, MEMORY_LOG_HANDLER],
)
logging.captureWarnings(True)

current_interaction: 'contextvars.ContextVar[discord.Interaction | None]' = contextvars.ContextVar(
    "current_interaction", default=None
)

if TYPE_CHECKING:
    from plugins.milestones import MilestoneTracker
    from plugins.telemetry import CommandTelemetryBase, CommandTelemetryEvent

class BotCore(discord.Client):
    def __init__(self, config_path='config.json'):
        self.config_path = config_path
        with open(self.config_path, 'r') as f:
            self.config: dict[str, Any] = json.load(f)
        self._config_lock = asyncio.Lock()
        
        self.channels_path: str = self.config.get("channels_path", "channels.json")
        if not os.path.exists(self.channels_path):
            with open(self.channels_path, 'w') as f:
                json.dump({}, f)
        
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        
        self.CELL_COORDS = (1170, 665, 1195, 685)
        self.CELL_OFFSET = 37 # x offset per historical cell
        self.SORT_AREA_COORDS = (75, 60, 1205, 575)
        self.SORT_CHANGE_THRESHOLD: float = self.config.get("sort_change_threshold", 0.05)
        self.OCR_CONCURRENCY: int = max(1, int(self.config.get("ocr_concurrency", 2)))
        self.STATS_COORDS: dict[str, tuple[int, int, int, int] | tuple[int, int, int, int, str]] = {
            "shuffles": (81, 610, 312, 640),
            "comparisons": (331, 610, 551, 640),
            "best_run": (645, 610, 730, 640, "0123456789/"),
            "shuffles_min": (819, 610, 1043, 640),
            "elapsed_time": (1166, 0, 1180, 75),
            "average_best_shuffle": (80, 670, 115, 685, "0123456789.")
        }
        self.THRESHOLD = 165
        self.current_vals: list[tuple[str, float]] = []
        self._current_vals_updated: bool = False
        self._last_sort_signature: np.ndarray | None = None
        self.stats_cache: dict[str, str] = {}
        self.monitor_message = None
        
        self.debug: bool = self.config.get("debug", False)
        self.logger = logging.getLogger("Bogobot")
        loglevel = logging.DEBUG if self.debug else logging.INFO
        self.logger.setLevel(loglevel)

        self.stream_handler = StreamHandler(
            url="https://www.youtube.com/live/DgfiqGPmGWY",
            quality="720p",
            on_new_frame=self.on_new_frame,
            fps=1.1,
            quiet=self.config.get(
                "silence_stream", False) or not self.debug,
            logger=self.logger.getChild("Stream"),
        )
        
        self.info = self._Info(self)
        self.discord = self._Discord(self)
        self.setup = self._Setup(self)
        self.command_telemetry_callbacks: list[Callable[["CommandTelemetryEvent"], Awaitable[None] | None]] = []
        self._last_ocr_refresh: float = 0.0
        self._last_frame_ms = time.monotonic()
        
        with open(self.channels_path, 'r') as f:
            channel_data: dict[str, Any] = json.load(f)
        def save_channels(data: dict[str, Any]):
            with open(self.channels_path, 'w') as f:
                json.dump(data, f, indent=4)
        self.channels = ChannelProxyManager(
            self, channel_data=channel_data,
            save_channels=save_channels,
            logger=self.logger.getChild("ChannelProxy")
        )
        self._connected = False
        
        self.event(self.on_ready)
        self.on_ready_callbacks = []
        
        self.milestones: 'MilestoneTracker | None' = None
    
    def init_callback(self, callback: Callable[[], Awaitable[None]]):
        self.on_ready_callbacks.append(callback)

    def command_telemetry_callback(
        self,
        callback: Callable[["CommandTelemetryEvent"], Awaitable[None] | None],
    ):
        self.command_telemetry_callbacks.append(callback)
        return callback

    async def emit_command_telemetry(self, event: "CommandTelemetryEvent"):
        for callback in self.command_telemetry_callbacks:
            try:
                result = callback(event)
                if result is not None:
                    await result
            except Exception as e:
                self.logger.warning(f"Command telemetry callback failed: {e}")

    class _Info:
        def __init__(self, outer: 'BotCore'):
            self.outer = outer
        
        # FIX: Added 'self' as the first argument
        def format_to_ddhhmmss(self, total_seconds):
            seconds = int(total_seconds) - 1776273837
            minutes = seconds // 60
            seconds = seconds % 60
            hours = minutes // 60
            minutes = minutes % 60
            days = hours // 24
            hours = hours % 24
            return f"{days:02}:{hours:02}:{minutes:02}:{seconds:02}"

        async def get_uptime(self):
            url = "https://www.youtube.com/youtubei/v1/updated_metadata?prettyPrint=false"
            payload = {
                "context": {
                    "client": {
                        "hl": "en",
                        "gl": "US",
                        "clientName": "WEB",
                        "clientVersion": "2.20260424.01.00"
                    }
                },
                "videoId": "DgfiqGPmGWY"
            }
            
            try:
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=payload) as response:
                        response.raise_for_status()
                        data = await response.json()
                raw_seconds = data["frameworkUpdates"]["entityBatchUpdate"]["timestamp"]["seconds"]
                
                return self.format_to_ddhhmmss(raw_seconds)
            except (KeyError, aiohttp.ClientError, asyncio.TimeoutError):
                return "00:00:00:00"

        async def get_best_shuffles(self):
            is_new = self.outer._current_vals_updated
            self.outer._current_vals_updated = False
            return self.outer.current_vals, is_new

        async def get_stats_all(self):
            return self.outer.stats_cache
    
    async def on_new_frame(self, img: Image.Image):
        dt = time.monotonic() - self._last_frame_ms
        self._last_frame_ms = time.monotonic()
        self.logger.debug(f"New frame received (dt={dt:.2f}s)")
        
        img.save("live_720p.png", format="PNG")
        sort_changed = self._sort_visual_changed(img)
        await self.update_ocr_data(img, sort_changed=sort_changed)
        dt = time.monotonic() - self._last_frame_ms
        self.logger.debug(f"OCR data updated (dt={dt:.2f}s)")
        
        if self.milestones:
            best_run = self.stats_cache.get("best_run")
            if best_run:
                await self.milestones.update("Best run", best_run)

            for milestone_name, stat_name in (
                ("Shuffles record", "shuffles"),
                ("Comparisons record", "comparisons"),
            ):
                stat_value = self._round_stat_down_to_power(self.stats_cache.get(stat_name))
                if stat_value:
                    await self.milestones.update(milestone_name, stat_value)

            shuffles_sec = self._round_stat_down_to_power(self.stats_cache.get("shuffles_min"))
            if shuffles_sec:
                await self._update_non_decreasing_milestone("Shuffles each second record", shuffles_sec)

            average_best_shuffle = self._round_stat_down_to_int(
                self.stats_cache.get("average_best_shuffle")
            )
            if average_best_shuffle:
                await self._update_non_decreasing_milestone(
                    "Average best shuffle record",
                    average_best_shuffle,
                )

    async def _update_non_decreasing_milestone(
        self,
        milestone_name: str,
        milestone_value: str,
    ) -> str | None:
        if self.milestones is None:
            return None

        current_value = await self.milestones.get(milestone_name)
        current_number = self._parse_stat_value(current_value)
        next_number = self._parse_stat_value(milestone_value)

        if (
            current_number is not None
            and next_number is not None
            and next_number < current_number
        ):
            return None

        return await self.milestones.update(milestone_name, milestone_value)

    def _parse_stat_value(self, value: str | None) -> float | None:
        if not value:
            return None

        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None

    def _round_stat_down_to_power(self, value: str | None) -> str | None:
        number = self._parse_stat_value(value)
        if number is None:
            return None

        number = int(number)
        if number <= 0:
            return None

        power = 10 ** (len(str(number)) - 1)
        return f"{number // power * power:,}"

    def _round_stat_down_to_int(self, value: str | None) -> str | None:
        number = self._parse_stat_value(value)
        if number is None:
            return None

        return f"{int(number):,}"
    
    def _sort_visual_changed(self, img: Image.Image) -> bool:
        crop = img.crop(self.SORT_AREA_COORDS).convert("RGB")
        rgb = np.array(crop)
        small = cv2.resize(rgb, (160, 72), interpolation=cv2.INTER_AREA).astype(np.int16)

        red = (
            (small[:, :, 0] > small[:, :, 1] + 25) &
            (small[:, :, 0] > small[:, :, 2] + 25) &
            (small[:, :, 0] > 80)
        )
        green = (
            (small[:, :, 1] > small[:, :, 0] + 15) &
            (small[:, :, 1] > small[:, :, 2] + 15) &
            (small[:, :, 1] > 80)
        )

        signature = np.zeros(small.shape[:2], dtype=np.uint8)
        signature[red] = 1
        signature[green] = 2

        if self._last_sort_signature is None:
            self._last_sort_signature = signature
            return True

        changed_ratio = np.count_nonzero(signature != self._last_sort_signature) / signature.size
        self._last_sort_signature = signature

        changed = changed_ratio >= self.SORT_CHANGE_THRESHOLD
        self.logger.debug(f"Sort visual delta={changed_ratio:.4f}, changed={changed}")
        return changed.item()
    
    async def update_ocr_data(self, img: Image.Image, *, sort_changed: bool = True):
        try:
            semaphore = asyncio.Semaphore(self.OCR_CONCURRENCY)

            async def parse_crop(coords, whitelist: str):
                async with semaphore:
                    return await self.tesseract_parse(
                        img.crop(coords),
                        whitelist,
                    )

            stats_tasks = []
            stats_specs = []

            for name, coords in self.STATS_COORDS.items():
                whitelist = "0123456789"
                if len(coords) >= 5:
                    whitelist = coords[4]
                    coords = coords[:4]

                stats_specs.append((name, whitelist))
                stats_tasks.append(parse_crop(coords, whitelist))

            stats_results = await asyncio.gather(*stats_tasks)

            for (name, whitelist), (text, conf) in zip(stats_specs, stats_results):
                if not text or conf < 0:
                    continue

                if whitelist != "0123456789":
                    self.stats_cache[name] = text
                else:
                    self.stats_cache[name] = f"{int(text):,}"
            
            if not sort_changed:
                self._last_ocr_refresh = time.time()
                return

            self.current_vals = []
            self._current_vals_updated = True
            coords = self.CELL_COORDS
            cell_tasks = []
            for _ in range(2): # last 2 cells
                cell_tasks.append(parse_crop(coords, "0123456789"))
                coords = (
                    coords[0] - self.CELL_OFFSET, coords[1],
                    coords[2] - self.CELL_OFFSET, coords[3]
                )

            self.current_vals = await asyncio.gather(*cell_tasks)
            self.current_vals.reverse()
            self._last_ocr_refresh = time.time()
        except Exception as e:
            self.logger.warning(f"OCR processing error: {e}")

    class _Discord:
        def __init__(self, outer: 'BotCore'):
            self.outer = outer

        async def send(
            self,
            contents=None,
            *,
            response=False,
            ephemeral=False,
            **kwargs,
        ):
            if contents is not None and "content" not in kwargs:
                kwargs["content"] = contents

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
            contents=None,
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
            if embed is None:
                embed = discord.Embed(
                    title=title,
                    description=contents,
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

            async def edit(self, contents=None, **kwargs):
                if not self.message:
                    return

                if self.embed is not None and any(
                    key in kwargs for key in ("title", "footer", "author", "color", "add_field")
                ):
                    await self.edit_embed(contents, **kwargs)
                    return

                if contents is not None and "content" not in kwargs:
                    kwargs["content"] = contents

                try:
                    self.content = kwargs.get("content", self.content)
                    if isinstance(kwargs.get("embed"), discord.Embed):
                        self.embed = kwargs["embed"]
                    await self.message.edit(**kwargs)
                except discord.NotFound:
                    self.message = None

            async def edit_embed(
                self,
                contents=None,
                *,
                embed: discord.Embed | None = None,
                title=None,
                footer=None,
                author=None,
                color=None,
                add_field=False,
                **kwargs,
            ):
                if not self.message:
                    return

                new_embed = embed or self._updated_embed(
                    contents=contents,
                    title=title,
                    footer=footer,
                    author=author,
                    color=color,
                    add_field=add_field,
                )

                try:
                    self.embed = new_embed
                    await self.message.edit(embed=new_embed, **kwargs)
                except discord.NotFound:
                    self.message = None

            def _updated_embed(self, contents=None, title=None, footer=None, author=None, color=None, add_field=False):
                old = self.embed or discord.Embed()

                if add_field:
                    new_embed = discord.Embed.from_dict(old.to_dict())
                    new_embed.add_field(
                        name=title or "Info",
                        value=contents or "N/A",
                        inline=False,
                    )
                else:
                    new_embed = discord.Embed(
                        title=title or old.title,
                        description=contents or old.description,
                        color=color or old.color,
                    )

                    for field in old.fields:
                        new_embed.add_field(
                            name=field.name,
                            value=field.value,
                            inline=field.inline,
                        )

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
            
            async def add_reaction(self, emoji_data: int | discord.Emoji):
                if not self.message:
                    return

                emoji = self.outer.get_emoji(emoji_data) if isinstance(emoji_data, int) else emoji_data
                if not emoji:
                    self.outer.logger.warning(f"Emoji with ID {emoji_data} not found.")
                    return
                try:
                    await self.message.add_reaction(emoji)
                except discord.NotFound:
                    self.message = None
                except discord.Forbidden:
                    pass

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

        try:
            await self.channels.initialize_channels()
        except Exception as e:
            self.logger.warning(f"Failed initializing channel proxies: {e}")
        
        for callback in self.on_ready_callbacks:
            try:
                await callback()
            except Exception as e:
                self.logger.warning(f"Error in on_ready callback: {e}")

    async def load_plugins(self, folder_name="plugins"):
        if not os.path.exists(folder_name):
            os.makedirs(folder_name)
        for filename in os.listdir(folder_name):
            if filename.endswith(".py"):
                mod = importlib.import_module(f"{folder_name}.{filename[:-3]}")
                if hasattr(mod, "setup"):
                    await mod.setup(self)
                self.logger.info(f"Loaded Plugin: {filename}")
    
    async def save_config(self):
        async with self._config_lock:
            tmp_path = f"{self.config_path}.tmp"

            with open(tmp_path, 'w') as f:
                json.dump(self.config, f, indent=4)

            os.replace(tmp_path, self.config_path)

    async def run_bot(self):
        self.stream_handler.start()
        await self.start(self.config['bot_token'])

    async def close(self):
        self.logger.info("Shutting down bot...")
        self.stream_handler.stop()
        await self.channels.close()
        await super().close() 

    def _preprocess_cell(self, pil_cell: 'Image.Image', scale=5, pad=10, stroke_thickness=5):
        # 1. Scaling + Early Erosion to separate touching pixels
        img = np.array(pil_cell.convert("L"))
        upscaled = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
        eroded = cv2.erode(upscaled, np.ones((3, 3), np.uint8), iterations=1)
        _, mask = cv2.threshold(eroded, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        # 2. Contour Extraction & Sorting
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)
        
        bw = np.ones_like(mask) * 255
        img_h, img_w = mask.shape
        image_area = img_h * img_w
        shells = [] 

        for cnt in contours:
            area = cv2.contourArea(cnt)
            x, y, w, h = cv2.boundingRect(cnt)
            
            # A: Ignore the image border
            if area > (image_area * 0.9):
                continue

            # B: Spatial Containment (Check if inside a Zero or Normal digit)
            parent_shell = None
            for s in shells:
                sx, sy, sw, sh = s['box']
                if x >= sx-2 and y >= sy-2 and (x+w) <= (sx+sw+2) and (y+h) <= (sy+sh+2):
                    parent_shell = s
                    break
            
            # C: Suppression: Don't draw the 'slash' inside a Zero
            if parent_shell and parent_shell['type'] == 'zero':
                continue

            # D: Normalization for scoring
            norm_scale = 100.0 / h if h > 0 else 1
            cnt_norm = ((cnt.astype(np.float32) - [x, y]) * norm_scale).astype(np.float32)

            ellipse_score = 0
            if len(cnt_norm) >= 5:
                _, (MA, ma), _ = cv2.fitEllipse(cnt_norm)
                ellipse_area = (np.pi * MA * ma) / 4.0
                ellipse_score = cv2.contourArea(cnt_norm) / ellipse_area if ellipse_area > 0 else 0

            # E: Solidity Check (Zero = High, 8 = Low due to waist)
            hull = cv2.convexHull(cnt)
            hull_area = cv2.contourArea(hull)
            solidity = area / hull_area if hull_area > 0 else 0

            # G: Hybrid Rendering
            if parent_shell:
                # Hole in 9, 8, etc -> Fill White
                cv2.drawContours(bw, [cnt], -1, 255, thickness=-1)
            else:
                # New Shell -> Determine if it's a 0 or a normal digit
                if ellipse_score > 0.88 and solidity > 0.94:
                    cv2.drawContours(bw, [cnt], -1, 0, stroke_thickness)
                    shells.append({'box': (x, y, w, h), 'type': 'zero'})
                else:
                    cv2.drawContours(bw, [cnt], -1, 0, thickness=-1)
                    shells.append({'box': (x, y, w, h), 'type': 'normal'})

        # 3. Final Polish: Padding + Dilation (thins the black text)
        bw = cv2.copyMakeBorder(bw, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
        bw = cv2.dilate(bw, np.ones((3, 3), np.uint8), iterations=1) 
        
        return bw

    async def tesseract_parse(self, pil_cell: 'Image.Image', whitelist: str, psm=7):
        processed = self._preprocess_cell(pil_cell)

        success, buffer = cv2.imencode(".png", processed)
        if not success:
            raise ValueError("Could not encode image")

        image_bytes = buffer.tobytes()

        cmd = [
            "tesseract",
            "stdin",
            "stdout",
            "--psm", str(psm),
            "--oem", "3",
            "-c", "load_system_dawg=0",
            "-c", "load_freq_dawg=0",
            "-c", f"tessedit_char_whitelist={whitelist}",
            "tsv"
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate(input=image_bytes)
        stderr_text = stderr.decode(errors="ignore").strip()
        
        if process.returncode != 0:
            if stderr_text:
                self.logger.error(f"tesseract: {stderr_text}")
            # Manually raise an error so the loop/bot knows it failed
            raise RuntimeError(
                f"Tesseract failed with code {process.returncode}: {stderr_text}"
            )

        if stderr_text:
            self.logger.debug(f"tesseract: {stderr_text}")

        out, conf = self._parse_tesseract_tsv_stdout(stdout.decode(errors="ignore"))

        res = ""
        for char in out:
            if char in whitelist:
                res += char

        self._save_ocr_debug('ocr_debug', image_bytes, f'{conf:.2f}c_{out}')

        return res, conf
    
    def _parse_tesseract_tsv_stdout(self, stdout: str) -> tuple[str, float]:
        """
        Parse Tesseract TSV stdout and return:
        (combined_text, confidence_0_to_1)

        Confidence is averaged across non-empty text rows with valid conf values.
        Tesseract conf is usually 0-100, with -1 for non-text structural rows.
        """
        rows = csv.DictReader(io.StringIO(stdout), delimiter="\t")

        parts: list[str] = []
        confs: list[float] = []

        for row in rows:
            text = (row.get("text") or "").strip()
            if not text:
                continue

            parts.append(text)

            try:
                conf = float(row.get("conf", "-1"))
            except ValueError:
                conf = -1.0

            if conf >= 0:
                confs.append(conf / 100.0)

        combined_text = "".join(parts)
        avg_conf = sum(confs) / len(confs) if confs else 0.0

        return combined_text, avg_conf
    
    def _save_ocr_debug(self, folder: str, image_data: bytes, text: str, max_files=30):
        if not self.config.get("save_ocr_debug", False):
            return

        safe_text = "".join(c for c in text if c.isalnum() or c in (' ', '_', '-')).rstrip()
        new_filename = f"ocr_{safe_text}.png"
        new_path = os.path.join(folder, new_filename)
        
        # 2. Fast Scan: Get entries and their timestamps in one go
        files: list[os.DirEntry[str]] = []
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_file() and entry.name.startswith("ocr_"):
                        files.append(entry)
                except FileNotFoundError:
                    continue # Skip if file disappeared during scanning

        # 3. Rotate if needed
        if len(files) >= max_files:
            # Find oldest via modification time
            oldest: os.DirEntry[str] | None = None
            oldest_mtime = float('inf')
            for entry in files:
                try:
                    mtime = entry.stat().st_mtime
                    if mtime < oldest_mtime:
                        oldest, oldest_mtime = entry, mtime
                except FileNotFoundError:
                    continue # Skip if file disappeared during stat
            if oldest:
                try:
                    os.remove(oldest.path)
                except FileNotFoundError:
                    pass

        # 4. Write new file
        with open(new_path, "wb") as f:
            f.write(image_data)

    class _Setup:
        def __init__(self, outer: 'BotCore'):
            self.outer = outer
            self.groups: dict[str, discord.app_commands.Group] = {}
        
        T = TypeVar('T')
        P = ParamSpec('P')
        _Callable = Callable[Concatenate[discord.Interaction, P], Coroutine[Any, Any, T]]
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
                )(cast('BotCore._Setup._Callable', wrapper))
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
                    )(cast('BotCore._Setup._Callable', wrapper))
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
                Callable[[discord.Interaction, discord.Member], Coroutine[Any, Any, Any]] |
                Callable[[discord.Interaction, discord.User], Coroutine[Any, Any, Any]] |
                Callable[[discord.Interaction, discord.Message], Coroutine[Any, Any, Any]] |
                Callable[[discord.Interaction, discord.Member | discord.User],
                         Coroutine[Any, Any, Any]]
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
            func: Callable[..., Coroutine[Any, Any, Any]],
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
            uid = interaction.user.id
            owner_id = self.outer.config.get("owner_uid")
            auth_list = self.outer.config.get("authorized_users", [])
            
            allowed = False
            if perm_requirement == 0: 
                allowed = True
            elif perm_requirement == 2 and uid == owner_id: 
                allowed = True
            elif perm_requirement == 1 and (uid == owner_id or uid in auth_list): 
                allowed = True

            try:
                await self.outer.emit_command_telemetry({
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
                error = str(e)
                if interaction.response.is_done():
                    await interaction.followup.send(f"⚠️ Error: {e}", ephemeral=True)
                else:
                    await interaction.response.send_message(f"⚠️ Error: {e}", ephemeral=True)
            finally:
                await self.outer.emit_command_telemetry({
                    **base_event,
                    "phase": "end",
                    "time": int(time.time()),
                    "status": status,
                    "duration_ms": round((time.monotonic() - started_at) * 1000, 1),
                    "error": error,
                })
                current_interaction.reset(token)
