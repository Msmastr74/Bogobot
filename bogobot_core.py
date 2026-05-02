import discord
from discord import app_commands
import json
import subprocess
import numpy as np
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
from datetime import datetime
import cv2
import os
import functools
import asyncio
import importlib
import time
import contextvars
import requests

# The "Invisible Baton"
current_interaction: 'contextvars.ContextVar[discord.Interaction | None]' = contextvars.ContextVar("current_interaction", default=None)

class BotCore(discord.Client):
    def __init__(self, config_path='config.json'):
        self.config_path = config_path
        with open(config_path, 'r') as f:
            self.config: dict[str] = json.load(f)
            
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        
        self.CELL_COORDS = (1173, 669, 1190, 683)
        self.STATS_COORDS = {
            "shuffles": (81, 585, 312, 640),
            "comparisons": (331, 585, 551, 640),
            "best_run": (570, 593, 885, 640),
            "shuffles_min": (819, 585, 1043, 640),
            "elapsed_time": (1166, 0, 1180, 75)
        }
        self.THRESHOLD = 165
        self.current_val = "0"
        self.stats_cache = {}
        self.target_channel_id = int(self.config.get('default_channel_id'))
        self.monitor_message = None
        self.last_text_message = None
        
        self.info = self._Info(self)
        self.discord = self._Discord(self)
        self.setup = self._Setup(self)  
        
        self._ocr_lock = asyncio.Lock()
        self._last_ocr_mtime = 0

    class _Info:
        def __init__(self, outer: 'BotCore'): self.outer = outer
        
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
            # Your successful video ID hack
            video_id = self.outer.config.get('youtube_stream_id', 'vzgH2DGhrUA') 
            url = f"https://www.youtube.com/youtubei/v1/updated_metadata?prettyPrint=false"
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
                # print(response.text) # Keep your successful debug line if you want!
                data = response.json()
                raw_seconds = data["frameworkUpdates"]["entityBatchUpdate"]["timestamp"]["seconds"]
                
                # Calling the fixed method
                return self.format_to_ddhhmmss(raw_seconds)
            except (KeyError, requests.RequestException) as e:
                return "00:00:00:00"
        async def get_best_shuffle(self):
            is_new = await self.outer.refresh_ocr_data()
            return self.outer.current_val, is_new
        async def get_stats_all(self):
            try:
                with Image.open('live_720p.png') as img:
                    img.load()
                    
                    # Low-frequency pass: Run the full dictionary only when called
                    for name, coords in self.outer.STATS_COORDS.items():
                        stat_crop = img.crop(coords).convert('L')
                        
                        if name == 'best_run': 
                            text = self.outer._tess_process(stat_crop, "0123456789/")
                            self.outer.stats_cache[name] = text
                        else:
                            digits = self.outer._tess_process(stat_crop, "0123456789") 
                            if digits:
                                self.outer.stats_cache[name] = f"{int(digits):,}"
                            else:
                                self.outer.stats_cache[name] = "0"
                            
                return self.outer.stats_cache
            except Exception as e:
                print(f"Stats Extraction Error: {e}")
                return self.outer.stats_cache

    class _Discord:
        def __init__(self, outer: 'BotCore'):
            self.outer = outer
            self.embeds = self._Embeds(outer)
            self.messages = self._Messages(outer)

        class _Messages:
            def __init__(self, outer: 'BotCore'): self.outer = outer
            async def send(self, contents, response=False):
                interaction = current_interaction.get()
                if response and interaction:
                    self.outer.last_text_message = await interaction.followup.send(contents)
                else:
                    channel = self.outer.get_channel(self.outer.target_channel_id)
                    if channel:
                        self.outer.last_text_message = await channel.send(contents)

            async def edit(self, contents):
                if self.outer.last_text_message:
                    try:
                        await self.outer.last_text_message.edit(content=contents)
                    except discord.NotFound: self.outer.last_text_message = None

            async def delete(self):
                if self.outer.last_text_message:
                    try:
                        await self.outer.last_text_message.delete()
                        self.outer.last_text_message = None
                    except:
                            pass


        class _Embeds:
            def __init__(self, outer: "BotCore"):
                self.outer = outer

            class EmbedHandle:
                def __init__(self, message, embed):
                    self.message = message
                    self.embed = embed

                async def edit(self, contents=None, title=None, author=None, color=None, add_field=False):
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

                if response and interaction:
                    message = await interaction.followup.send(embed=embed, wait=True)
                else:
                    channel = self.outer.get_channel(self.outer.target_channel_id)
                    if not channel:
                        return None

                    message = await channel.send(embed=embed)

                return self.EmbedHandle(message, embed)
    class _Setup:
        def __init__(self, outer): self.outer = outer
        def channel_id(self, new_id): self.outer.target_channel_id = int(new_id)

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
                    if perm_requirement == 0: allowed = True
                    elif perm_requirement == 2 and uid == owner_id: allowed = True
                    elif perm_requirement == 1 and (uid == owner_id or uid in auth_list): allowed = True

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
            
    # OCR STUFF
    async def refresh_ocr_data(self):
        async with self._ocr_lock:
            try:
                mtime = os.path.getmtime('live_720p.png')
                if mtime <= self._last_ocr_mtime: return False
                await asyncio.wait_for(asyncio.to_thread(self._run_ocr), timeout=5.0)
                self._last_ocr_mtime = mtime
                return True
            except Exception as e:
                print(e)
                return False

    def _run_ocr(self):
        try:
            with Image.open('live_720p.png') as img:
                img.load()
                
                # High-frequency pass: Only the Serial Number (CELL)
                cell_crop = img.crop(self.CELL_COORDS).convert('L')
                self.current_val = self._tess_process(cell_crop, "0123456789")
                
        except Exception as e:
            # Silence errors if ffmpeg is currently writing the file
            pass

    async def setup_hook(self):
        if self.config.get('sync', True):
            await self.tree.sync()
            self.config['sync'] = False
            self.save_config()

    async def load_plugins(self, folder_name="plugins"):
        if not os.path.exists(folder_name): os.makedirs(folder_name)
        for filename in os.listdir(folder_name):
            if filename.endswith(".py"):
                mod = importlib.import_module(f"{folder_name}.{filename[:-3]}")
                if hasattr(mod, "setup"): await mod.setup(self)
                print(f"✅ Loaded Plugin: {filename}")
    def save_config(self):
        with open(self.config_path, 'w') as f:
            json.dump(self.config, f, indent=4)
    async def run_bot(self): 
        await self.start(self.config['bot_token'])
        digits = "".join([c for c in self.raw_text if c.isdigit()])
    
        if digits:
            # int() removes leading zeros, :, adds perfect commas
            self.stats_cache[self.name] = f"{int(digits):,}"
        else:
            self.stats_cache[self.name] = "0"

    def _tess_process(self, pil_cell: 'Image.Image', whitelist: str, psm=7):
        # 1. Convert PIL to OpenCV grayscale
        img_array = np.array(pil_cell.convert('L'))

        # 2. Smooth Upscale (4x)
        # Cubic interpolation provides the soft edges Tesseract's LSTM engine uses
        # to distinguish character curves.
        img = cv2.resize(img_array, None, fx=4, fy=4, interpolation=cv2.INTER_LANCZOS4)


        # 3. Invert and Binarize
        # We turn white-on-dark into black-on-white.
        inverted = cv2.bitwise_not(img)
        _, binarized = cv2.threshold(inverted, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)

        final_img = cv2.erode(binarized, np.ones((3, 3), np.uint8), iterations=1)

        # 5. Final Padding
        padded = cv2.copyMakeBorder(final_img, 25, 25, 25, 25, cv2.BORDER_CONSTANT, value=255)

        cv2.imwrite("temp_ocr.png", padded)

        cmd = [
            "tesseract",
            "temp_ocr.png",
            "stdout",
            "--psm", str(psm),
            "--oem", "3",
            "-c", "load_system_dawg=0",
            "-c", "load_freq_dawg=0",
            "-c", f"tessedit_char_whitelist={whitelist}"
        ]

        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode(errors="ignore").strip()

            res = ""
            for char in out:
                if char in whitelist:
                    res += char
            return res

        except subprocess.CalledProcessError:
            return ""

    class _Setup:
        def __init__(self, outer): self.outer = outer
        def channel_id(self, new_id): self.outer.target_channel_id = int(new_id)

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
                    if perm_requirement == 0: allowed = True
                    elif perm_requirement == 2 and uid == owner_id: allowed = True
                    elif perm_requirement == 1 and (uid == owner_id or uid in auth_list): allowed = True

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
