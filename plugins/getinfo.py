import discord
import datetime

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

async def setup(bot: 'BotCore'):
    @bot.setup.command(name="avatar", description="Get the avatar of a user", eph=False, perm_requirement=0)
    async def avatar(interaction: discord.Interaction, user: discord.Member | discord.User | None = None) -> None:
        if user is None:
            user = interaction.user
        embed = discord.Embed(title=f"{user.display_name}'s Avatar:")
        embed.set_image(url=user.display_avatar.url)

        await bot.discord.send_embed(embed=embed, response=True)
    
    @bot.setup.command(name="ping", description="Ping pong", defer=False, perm_requirement=0)
    async def ping(
        interaction: discord.Interaction,
        interaction_latency: float | None = None,
        gateway_latency: float | None = None,
        timestamp: float | None = None
    ):
        now = discord.utils.utcnow() 
        ping_ms = (now - interaction.created_at).total_seconds() * 1000
        if interaction_latency is not None:
            ping_ms = interaction_latency
        
        if ping_ms > 500:
            color = discord.Colour.red()
        elif ping_ms > 200:
            color = discord.Colour.orange()
        elif ping_ms < -50:
            color = discord.Colour.magenta()
        elif ping_ms < 0:
            color = discord.Colour.purple()
        elif ping_ms < 50:
            color = discord.Colour.green()
        else:
            color = discord.Colour.blue()
        
        t = datetime.datetime.fromtimestamp(timestamp) if timestamp is not None else now
        embed = discord.Embed(title="Pong!", color=color, timestamp=t)
        embed.add_field(name="Interaction Latency", value=f"{ping_ms:.2f} ms")
        
        gateway_latency_ms = gateway_latency if gateway_latency is not None else bot.latency * 1000
        embed.add_field(name="Gateway Latency", value=f"{gateway_latency_ms:.2f} ms")
        message = await bot.discord.send_embed(embed=embed, response=True)
        if message is not None:
            await message.add_reaction("🏓")
