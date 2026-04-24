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

class BotCore(discord.Client):
    def __init__(self, config_path='config.json'):
        with open(config_path, 'r') as f:
            self.config = json.load(f)
            
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        
        # Internal State
        self.CELL_COORDS = (1163, 660, 1200, 690)
        self.STATS_COORDS = {
            "shuffles": (130, 845, 230, 875),
            "comparisons": (355, 845, 520, 875),
            "best_run": (500, 845, 570, 875),
            "shuffles_min": (750, 845, 885, 875)
        }
        self.THRESHOLD = 165
        self.current_val = "0"
        self.stats_cache = {}
        self.target_channel_id = self.config.get('default_channel_id')
        self.monitor_message = None
        
        # Namespaces
        self.info = self._Info(self)
        self.discord = self._Discord(self) # Restored
        self.setup = self._Setup(self)  
        
        self._ocr_lock = asyncio.Lock()
        self._last_ocr_mtime = 0

    class _Info:
        def __init__(self, outer): self.outer = outer
        
        async def get_last_shuffle(self):
            is_new = await self.outer.refresh_ocr_data()
            return self.outer.current_val, is_new

        async def get_stats_all(self):
            await self.outer.refresh_ocr_data()
            return self.outer.stats_cache

    class _Discord:
        def __init__(self, outer):
            self.outer = outer
            self.embeds = self._Embeds(outer)

        class _Embeds:
            def __init__(self, outer):
                self.outer = outer

            async def send(self, contents, title="embed", footer="", color=discord.Color.blue()):
                channel = self.outer.get_channel(self.outer.target_channel_id)
                if not channel: return
                embed = discord.Embed(title=title, description=contents, color=color)
                embed.set_footer(text=footer)
                self.outer.monitor_message = await channel.send(embed=embed)

            async def edit(self, contents=None, title=None, color=None):
                """Handles the 404 Unknown Message error if the monitor message is deleted."""
                if not self.outer.monitor_message: return
                
                old = self.outer.monitor_message.embeds[0]
                new_embed = discord.Embed(
                    title=title or old.title, 
                    description=contents or old.description, 
                    color=color or old.color
                )
                new_embed.set_author(name=f"Last Scan: {datetime.now().strftime('%H:%M:%S')}")
                
                try:
                    await self.outer.monitor_message.edit(embed=new_embed)
                except discord.NotFound:
                    # Prevents the 'Unknown Message' crash
                    self.outer.monitor_message = None
                except Exception as e:
                    print(f"Edit Error: {e}")

            async def delete(self, interaction):
                if self.outer.monitor_message:
                    try:
                        await self.outer.monitor_message.delete()
                        self.outer.monitor_message = None
                        await interaction.response.send_message("🗑️ Monitor message cleared.", ephemeral=True)
                    except:
                        pass

    class _Setup:
        def __init__(self, outer): self.outer = outer
        def channel_id(self, new_id): self.outer.target_channel_id = int(new_id)

        def command(self, name, description="No description", perm_requirement=1):
            def decorator(func):
                @self.outer.tree.command(name=name, description=description)
                @functools.wraps(func)
                async def wrapper(interaction: discord.Interaction, *args, **kwargs):
                    # AUTOMATIC DEFER: Stops "Bogobot is thinking"
                    await interaction.response.defer(ephemeral=(perm_requirement != 0))
                    
                    uid = interaction.user.id
                    owner_id = self.outer.config.get("owner_uid")
                    auth_list = self.outer.config.get("authorized_users", [])

                    allowed = False
                    if perm_requirement == 0: allowed = True
                    elif perm_requirement == 2 and uid == owner_id: allowed = True
                    elif perm_requirement == 1 and (uid == owner_id or uid in auth_list): allowed = True

                    if not allowed:
                        return await interaction.followup.send("❌ Unauthorized.", ephemeral=True)
                    
                    try:
                        await func(interaction, *args, **kwargs)
                    except Exception as e:
                        await interaction.followup.send(f"⚠️ Error: {e}")
                return wrapper
            return decorator

    async def refresh_ocr_data(self):
        """Only runs OCR if FFMpeg has updated the file on disk."""
        async with self._ocr_lock:
            try:
                mtime = os.path.getmtime('live_720p.jpg')
                if mtime <= self._last_ocr_mtime:
                    return False
                
                await asyncio.wait_for(asyncio.to_thread(self._run_ocr), timeout=4.0)
                self._last_ocr_mtime = mtime
                return True
            except Exception:
                return False

    def _run_ocr(self):
        with Image.open('live_720p.jpg') as img:
            img.load()
            cell = img.crop(self.CELL_COORDS).convert('L')
            self.current_val = self._tess_process(cell, "0123456789")
            
            for name, coords in self.STATS_COORDS.items():
                stat_crop = img.crop(coords).convert('L')
                whitelist = "0123456789/" if name == "best_run" else "0123456789,"
                self.stats_cache[name] = self._tess_process(stat_crop, whitelist)

    def _tess_process(self, cell, whitelist):
        cell = ImageOps.autocontrast(cell, cutoff=0.5)
        data = np.array(cell)
        clean = np.where(data > self.THRESHOLD, 255, 0).astype(np.uint8)
        cell = Image.fromarray(clean).resize((cell.width * 10, cell.height * 10), Image.Resampling.NEAREST)
        cell = ImageOps.invert(cell.convert('RGB')).convert('L')
        cell = ImageOps.expand(cell, border=60, fill='white')
        cell.save("temp_ocr.png")
        return subprocess.check_output([
            'tesseract', "temp_ocr.png", 'stdout', '--psm', '7', 
            '-c', f'tessedit_char_whitelist={whitelist}'
        ], stderr=subprocess.DEVNULL).decode().strip()

    async def setup_hook(self):
        await self.tree.sync()
        
    async def load_plugins(self, folder_name="plugins"):
        if not os.path.exists(folder_name):
            os.makedirs(folder_name)
        for filename in os.listdir(folder_name):
            if filename.endswith(".py"):
                module_name = f"{folder_name}.{filename[:-3]}"
                module = importlib.import_module(module_name)
                if hasattr(module, "setup"):
                    await module.setup(self)
                print(f"✅ Loaded Plugin: {filename}")

    def save_config(self):
        with open('config.json', 'w') as f:
            json.dump(self.config, f, indent=4)

    async def run_bot(self):
        await self.start(self.config['bot_token'])
