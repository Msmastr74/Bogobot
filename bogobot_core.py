import discord
from discord import app_commands
import json
import subprocess
import numpy as np
from PIL import Image, ImageOps
from datetime import datetime
import os
import functools
import asyncio
import importlib
import time
import contextvars
import requests

# The "Invisible Baton"
current_interaction = contextvars.ContextVar("current_interaction", default=None)

class BotCore(discord.Client):
    def __init__(self, config_path='config.json'):
        with open(config_path, 'r') as f:
            self.config = json.load(f)
            
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        
        self.CELL_COORDS = (1163, 660, 1200, 690)
        self.STATS_COORDS = {
            "shuffles": (81, 585, 312, 640),
            "comparisons": (331, 585, 551, 640),
            "best_run": (750, 585, 885, 640),
            "shuffles_min": (819, 585, 1043, 640),
            "elapsed_time": (1166, 0, 1180, 75)
        }
        self.THRESHOLD = 165
        self.current_val = "0"
        self.stats_cache = {}
        self.target_channel_id = self.config.get('default_channel_id')
        
        self.monitor_message = None
        self.last_text_message = None
        
        self.info = self._Info(self)
        self.discord = self._Discord(self)
        self.setup = self._Setup(self)  
        
        self._ocr_lock = asyncio.Lock()
        self._last_ocr_mtime = 0

    class _Info:
        def __init__(self, outer): self.outer = outer
        
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
            "videoId": "vzgH2DGhrUA"
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
                with Image.open('live_720p.jpg') as img:
                    img.load()
                    
                    # Low-frequency pass: Run the full dictionary only when called
                    for name, coords in self.STATS_COORDS.items():
                        stat_crop = img.crop(coords).convert('L')
                        
                        raw_text = self._tess_process(stat_crop, "0123456789") 
                        digits = "".join([c for c in raw_text if c.isdigit()])
                        
                        if digits:
                            self.stats_cache[name] = f"{int(digits):,}"
                        else:
                            self.stats_cache[name] = "0"
                            
                return self.stats_cache
            except Exception as e:
                print(f"Stats Extraction Error: {e}")
                return self.stats_cache

    class _Discord:
        def __init__(self, outer):
            self.outer = outer
            self.embeds = self._Embeds(outer)
            self.messages = self._Messages(outer)

        class _Messages:
            def __init__(self, outer): self.outer = outer
            async def send(self, contents, response=False):
                interaction = current_interaction.get()
                if response and interaction:
                    self.outer.last_text_message = await interaction.followup.send(contents)
                else:
                    channel = self.outer.get_channel(self.outer.target_channel_id)
                    if channel: self.outer.last_text_message = await channel.send(contents)

            async def edit(self, contents):
                if self.outer.last_text_message:
                    try: await self.outer.last_text_message.edit(content=contents)
                    except discord.NotFound: self.outer.last_text_message = None

            async def delete(self):
                if self.outer.last_text_message:
                    try:
                        await self.outer.last_text_message.delete()
                        self.outer.last_text_message = None
                    except: pass

        class _Embeds:
            def __init__(self, outer): self.outer = outer
            
            async def send(self, contents, title="embed", footer="", author="Bogobot", color=discord.Color.blue(), response=False):
                interaction = current_interaction.get()
                embed = discord.Embed(title=title, description=contents, color=color)
                embed.set_footer(text=footer)
                embed.set_author(name=author)
                
                # 1. Save to instant local cache
                self.outer._latest_embed = embed 
                
                if response and interaction:
                    self.outer.monitor_message = await interaction.followup.send(embed=embed)
                else:
                    channel = self.outer.get_channel(self.outer.target_channel_id)
                    if channel: self.outer.monitor_message = await channel.send(embed=embed)

            async def edit(self, contents=None, title=None, author=None, color=None, add_field=False):
                if not self.outer.monitor_message: return
                
                # 2. Pull from local cache to avoid the Race Condition
                old = getattr(self.outer, '_latest_embed', self.outer.monitor_message.embeds[0])
                
                if add_field:
                    new_embed = discord.Embed.from_dict(old.to_dict())
                    new_embed.add_field(name=title or "Info", value=contents or "N/A", inline=False)
                else:
                    new_embed = discord.Embed(title=title or old.title, description=contents or old.description, color=color or old.color)
                    for field in old.fields: new_embed.add_field(name=field.name, value=field.value, inline=field.inline)
                
                current_author = old.author.name if old.author else ""
                new_embed.set_author(name=author or current_author)
                
                # 3. Update the instant cache before sending to Discord
                self.outer._latest_embed = new_embed
                
                try: await self.outer.monitor_message.edit(embed=new_embed)
                except discord.NotFound: self.outer.monitor_message = None

            async def delete(self):
                if self.outer.monitor_message:
                    try:
                        await self.outer.monitor_message.delete()
                        self.outer.monitor_message = None
                    except: pass

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
                mtime = os.path.getmtime('live_720p.jpg')
                if mtime <= self._last_ocr_mtime: return False
                await asyncio.wait_for(asyncio.to_thread(self._run_ocr), timeout=5.0)
                self._last_ocr_mtime = mtime
                return True
            except: return False

    def _run_ocr(self):
        try:
            with Image.open('live_720p.jpg') as img:
                img.load()
                
                # High-frequency pass: Only the Serial Number (CELL)
                cell_crop = img.crop(self.CELL_COORDS).convert('L')
                self.current_val = self._tess_process(cell_crop, "0123456789")
                
        except Exception as e:
            # Silence errors if ffmpeg is currently writing the file
            pass

    def _tess_process(self, cell, whitelist):
        cell = ImageOps.autocontrast(cell, cutoff=0.5)
        data = np.array(cell)
        clean = np.where(data > self.THRESHOLD, 255, 0).astype(np.uint8)
        cell = Image.fromarray(clean).resize((cell.width * 10, cell.height * 10), Image.Resampling.NEAREST)
        cell = ImageOps.invert(cell.convert('RGB')).convert('L')
        cell = ImageOps.expand(cell, border=60, fill='white')
        cell.save("temp_ocr.png")
        return subprocess.check_output(['tesseract', "temp_ocr.png", 'stdout', '--psm', '7', '-c', f'tessedit_char_whitelist={whitelist}'], stderr=subprocess.DEVNULL).decode().strip()

    async def setup_hook(self): await self.tree.sync()
    async def load_plugins(self, folder_name="plugins"):
        if not os.path.exists(folder_name): os.makedirs(folder_name)
        for filename in os.listdir(folder_name):
            if filename.endswith(".py"):
                mod = importlib.import_module(f"{folder_name}.{filename[:-3]}")
                if hasattr(mod, "setup"): await mod.setup(self)
                print(f"✅ Loaded Plugin: {filename}")
    def save_config(self):
        with open('config.json', 'w') as f: json.dump(self.config, f, indent=4)
    async def run_bot(self): await self.start(self.config['bot_token'])
= "".join([c for c in raw_text if c.isdigit()])
                
                if digits:
                    # int() removes leading zeros, :, adds perfect commas
                    self.stats_cache[name] = f"{int(digits):,}"
                else:
                    self.stats_cache[name] = "0"

    def _tess_process(self, cell, whitelist):
        cell = ImageOps.autocontrast(cell, cutoff=0.5)
        data = np.array(cell)
        clean = np.where(data > self.THRESHOLD, 255, 0).astype(np.uint8)
        cell = Image.fromarray(clean).resize((cell.width * 10, cell.height * 10), Image.Resampling.NEAREST)
        cell = ImageOps.invert(cell.convert('RGB')).convert('L')
        cell = ImageOps.expand(cell, border=60, fill='white')
        cell.save("temp_ocr.png")
        return subprocess.check_output(['tesseract', "temp_ocr.png", 'stdout', '--psm', '7', '-c', f'tessedit_char_whitelist={whitelist}'], stderr=subprocess.DEVNULL).decode().strip()

    async def setup_hook(self): await self.tree.sync()
    async def load_plugins(self, folder_name="plugins"):
        if not os.path.exists(folder_name): os.makedirs(folder_name)
        for filename in os.listdir(folder_name):
            if filename.endswith(".py"):
                mod = importlib.import_module(f"{folder_name}.{filename[:-3]}")
                if hasattr(mod, "setup"): await mod.setup(self)
                print(f"✅ Loaded Plugin: {filename}")
    def save_config(self):
        with open('config.json', 'w') as f: json.dump(self.config, f, indent=4)
    async def run_bot(self): await self.start(self.config['bot_token'])
