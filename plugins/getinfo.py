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
        user: discord.User | None = None
    ):
        now = discord.utils.utcnow() 
        interaction_latency = (now - interaction.created_at).total_seconds() * 1000
        
        def choose_color(latency: float) -> discord.Colour:
            if latency > 500:
                return discord.Colour.red()
            elif latency > 200:
                return discord.Colour.orange()
            elif latency < -50:
                return discord.Colour.dark_magenta()
            elif latency < 0:
                return discord.Colour.brand_green()
            elif latency < 50:
                return discord.Colour.green()
            else:
                return discord.Colour.blue()
        
        embed = discord.Embed(title="Pong!", color=choose_color(interaction_latency), timestamp=now)
        embed.add_field(name="Interaction Latency", value=f"{interaction_latency:.2f} ms")
        
        gateway_latency = bot.latency * 1000
        embed.add_field(name="Gateway Latency", value=f"{gateway_latency:.2f} ms")
        message = await bot.discord.send_embed(embed=embed, response=True)
        if message is None:
            return
        await message.add_reaction("🏓")
        
        user_id = user.id if user else interaction.user.id
        user_msg = await bot.wait_for(
            "message",
            check=lambda m: m.author.id == user_id and m.channel.id == interaction.channel_id,
            timeout=60
        )
        if user_msg.nonce is not None:
            try:
                nonce = int(user_msg.nonce)
            except ValueError:
                return
            user_client_time = discord.utils.snowflake_time(nonce)
            msg_created_at = user_msg.created_at
            user_latency = (msg_created_at - user_client_time).total_seconds() * 1000
            await message.edit_embed(
                title="User Latency",
                description=f"{user_latency:.2f} ms",
                add_field=True, inline=True,
                color=choose_color(user_latency)
            )
