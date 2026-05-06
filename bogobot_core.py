import discord
from discord import app_commands
import json
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
import requests
import time
from typing import Any
from stream import StreamHandler
import logging

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s.%(msecs)03d %(levelname)-8s | %(name)-14s ] %(message)s',
    datefmt='%d %H:%M:%S'
)
logging.captureWarnings(True)

current_interaction: 'contextvars.ContextVar[discord.Interaction | None]' = contextvars.ContextVar(
    "current_interaction", default=None
)

class BotCore(discord.Client):
    def __init__(self, config_path='config.json'):
        self.config_path = config_path
        with open(self.config_path, 'r') as f:
            self.config: dict[str, Any] = json.load(f)
            
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        
        self.CELL_COORDS = (1170, 665, 1195, 685)
        self.CELL_OFFSET = 37 # x offset per historical cell
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
        self.stats_cache: dict[str, str] = {}
        self.monitor_message = None
        
        self.debug: bool = self.config.get("debug", False)
        self.stream_handler = StreamHandler(
            url="https://www.youtube.com/live/DgfiqGPmGWY",
            quality="720p",
            on_new_frame=self.on_new_frame,
            fps=1,
            quiet=not self.debug
        )
        
        self.logger = logging.getLogger("Bogobot")
        loglevel = logging.DEBUG if self.debug else logging.INFO
        self.logger.setLevel(loglevel)
        logging.getLogger().setLevel(loglevel)
        
        self.info = self._Info(self)
        self.discord = self._Discord(self)
        self.setup = self._Setup(self)
        self._last_ocr_mtime: float = 0.0
        self._last_ocr_refresh: float = 0.0

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
                response = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: requests.post(url, json=payload, timeout=10)
                )
                data = response.json()
                raw_seconds = data["frameworkUpdates"]["entityBatchUpdate"]["timestamp"]["seconds"]
                
                return self.format_to_ddhhmmss(raw_seconds)
            except (KeyError, requests.RequestException):
                return "00:00:00:00"

        async def get_best_shuffles(self):
            is_new = self.outer._current_vals_updated
            self.outer._current_vals_updated = False
            return self.outer.current_vals, is_new

        async def get_stats_all(self):
            return self.outer.stats_cache
    
    ms = 0
    async def on_new_frame(self, img: Image.Image):
        if self.ms == 0:
            self.ms = time.monotonic()
        dt = time.monotonic() - self.ms
        self.ms = time.monotonic()
        self.logger.debug(f"New frame received (dt={dt:.2f}s)")
        
        img.save("live_720p.png", format="PNG")
        await self.update_ocr_data(img)
        dt = time.monotonic() - self.ms
        self.logger.debug(f"OCR data updated (dt={dt:.2f}s)")
    
    async def update_ocr_data(self, img: Image.Image):
        try:
            for name, coords in self.STATS_COORDS.items():
                filter: str = "0123456789"
                if len(coords) >= 5:
                    filter = coords[4]
                    coords = coords[:4]
                stat_crop = img.crop(coords)
                
                if filter != "0123456789":
                    text, conf = await self.tesseract_parse(
                        stat_crop, filter
                    )
                    if text and conf >= 0:
                        self.stats_cache[name] = text
                else:
                    digits, conf = await self.tesseract_parse(
                        stat_crop, "0123456789"
                    ) 
                    if digits and conf >= 0:
                        self.stats_cache[name] = f"{int(digits):,}"
            
            self.current_vals = []
            self._current_vals_updated = True
            coords = self.CELL_COORDS
            for _ in range(3): # last 3 cells
                cell_crop = img.crop(coords)
                output, conf = await self.tesseract_parse(
                    cell_crop, "0123456789"
                )
                self.current_vals.append((output, conf))
                coords = (
                    coords[0] - self.CELL_OFFSET, coords[1],
                    coords[2] - self.CELL_OFFSET, coords[3]
                )
            self.current_vals.reverse()
            self._last_ocr_refresh = time.time()
        except Exception as e:
            self.logger.warning(f"OCR processing error: {e}")

    class _Discord:
        def __init__(self, outer: 'BotCore'):
            self.outer = outer
            self.embeds = self._Embeds(outer)
            self.messages = self._Messages(outer)

        class _Messages:
            def __init__(self, outer: "BotCore"):
                self.outer = outer

            async def send(self, contents, response=False):
                interaction = current_interaction.get()

                message: discord.Message | None = None
                if response and interaction:
                    message = await interaction.followup.send(contents, wait=True)
                elif interaction and hasattr(interaction.channel, 'send'):
                    message = await interaction.channel.send(contents) # pyright: ignore
                if message is None:
                    return None

                return self.MessageHandle(self.outer, message)

            class MessageHandle:
                def __init__(self, outer: "BotCore", message: discord.Message):
                    self.outer = outer
                    self.message: discord.Message | None = message

                @property
                def exists(self) -> bool:
                    return self.message is not None

                async def edit(self, contents):
                    if not self.message:
                        return

                    try:
                        await self.message.edit(content=contents)
                    except discord.NotFound:
                        self.message = None

                async def delete(self):
                    if not self.message:
                        return

                    try:
                        await self.message.delete()
                    except Exception:
                        pass
                    finally:
                        self.message = None
                        
                async def add_reaction(self, emoji):
                    if not self.message:
                        return

                    try:
                        await self.message.add_reaction(emoji)
                    except discord.NotFound:
                        self.message = None
                    except Exception:
                        pass

        class _Embeds:
            def __init__(self, outer: "BotCore"):
                self.outer = outer

            class EmbedHandle:
                def __init__(self, message: discord.Message, embed: discord.Embed):
                    self.message: discord.Message | None = message
                    self.embed: discord.Embed = embed
                
                @property
                def message_id(self):
                    return self.message.id if self.message else None

                async def edit(self, contents=None, title=None, footer=None, author=None, color=None, add_field=False):
                    if not self.message:
                        return

                    old = self.embed

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

                    self.embed = new_embed

                    try:
                        await self.message.edit(embed=new_embed)
                    except discord.NotFound:
                        self.message = None

                async def delete(self):
                    if self.message:
                        try:
                            await self.message.delete()
                        except discord.NotFound:
                            pass
                        finally:
                            self.message = None
            async def send(
                self,
                contents,
                title="embed",
                footer="",
                author="Bogobot",
                color=discord.Color.blue(),
                response=False,
            ):
                interaction = current_interaction.get()

                embed = discord.Embed(
                    title=title,
                    description=contents,
                    color=color,
                )
                embed.set_footer(text=footer)
                embed.set_author(name=author)

                message: discord.Message | None = None
                if response and interaction:
                    message = await interaction.followup.send(embed=embed, wait=True)
                elif interaction and hasattr(interaction.channel, 'send'):
                    message = await interaction.channel.send(embed=embed) # pyright: ignore
                if message is None:
                    return None
                
                return self.EmbedHandle(message, embed)

    async def setup_hook(self):
        if self.config.get('sync', True):
            await self.tree.sync()
            self.config['sync'] = False
            self.save_config()

    async def load_plugins(self, folder_name="plugins"):
        if not os.path.exists(folder_name):
            os.makedirs(folder_name)
        for filename in os.listdir(folder_name):
            if filename.endswith(".py"):
                mod = importlib.import_module(f"{folder_name}.{filename[:-3]}")
                if hasattr(mod, "setup"):
                    await mod.setup(self)
                self.logger.info(f"Loaded Plugin: {filename}")
    def save_config(self):
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=4)

    async def run_bot(self):
        self.stream_handler.start()
        await self.start(self.config['bot_token'])

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
        
        if process.returncode != 0:
            # Manually raise an error so the loop/bot knows it failed
            raise RuntimeError(
                f"Tesseract failed with code {process.returncode}: {stderr.decode(errors="ignore")}"
            )

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
        def __init__(self, outer: 'BotCore'): self.outer = outer

        def command(self, name, description="No description", perm_requirement=1, eph=True, defer=True):
            def decorator(func):
                @self.outer.tree.command(name=name, description=description)
                @functools.wraps(func)
                async def wrapper(interaction: discord.Interaction, *args, **kwargs):
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
                        if not allowed:
                            return await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)
                        if defer:
                            await interaction.response.defer(ephemeral=(eph))
                        await func(interaction, *args, **kwargs)
                    except Exception as e:
                        if interaction.response.is_done():
                            await interaction.followup.send(f"⚠️ Error: {e}")
                        else:
                            await interaction.response.send_message(f"⚠️ Error: {e}", ephemeral=True)
                    finally:
                        current_interaction.reset(token)
                return wrapper
            return decorator
