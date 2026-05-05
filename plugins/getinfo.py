import discord

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

        await interaction.followup.send(embed=embed)
