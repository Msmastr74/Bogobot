import re

import discord

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

import time

async def setup(bot: 'BotCore'):
    @bot.setup.command(name="avatar", description="Get the avatar of a user", eph=False, perm_requirement=0)
    async def avatar(interaction: discord.Interaction, user: discord.Member | discord.User | None = None) -> None:
        if user is None:
            user = interaction.user
        embed = discord.Embed(title=f"{user.display_name}'s Avatar:")
        embed.set_image(url=user.display_avatar.url)

        await bot.discord.send_embed(embed=embed, response=True)
    
    @bot.setup.command(name="ping", description="Ping pong", defer=False, perm_requirement=0)
    async def ping(interaction: discord.Interaction):
        now = discord.utils.utcnow() 
        ping_ms = (now - interaction.created_at).total_seconds() * 1000
        if ping_ms > 500:
            color = discord.Colour.red()
        elif ping_ms > 200:
            color = discord.Colour.orange()
        else:
            color = discord.Colour.blue()
        embed = discord.Embed(title="Pong!", color=color, timestamp=now)
        embed.add_field(name="Interaction Latency", value=f"{ping_ms:.2f} ms")
        embed.add_field(name="Gateway Latency", value=f"{bot.latency * 1000:.2f} ms")
        await bot.discord.send_embed(embed=embed, response=True)
