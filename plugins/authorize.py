import discord

from typing import Literal
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

async def setup(bot: 'BotCore'):
    from utils import groups

    manage = groups.manage(bot)

    @manage.command(name="auth", description="Authorize or deauthorize a user", perm_requirement=2)
    async def auth(
        interaction: discord.Interaction,
        action: Literal["authorize", "deauthorize"],
        user: discord.Member,
    ):
        if action == "authorize":
            if user.id not in bot.config["authorized_users"]:
                bot.config["authorized_users"].append(user.id)
                await bot.save_config()
                return await bot.discord.send(f"{user.mention} has been authorized.", response=True)

            return await bot.discord.send(f"{user.mention} is already authorized.", response=True)

        if user.id in bot.config["authorized_users"]:
            bot.config["authorized_users"].remove(user.id)
            await bot.save_config()
            return await bot.discord.send(f"{user.mention} has been deauthorized.", response=True)

        return await bot.discord.send(f"{user.mention} is not authorized.", response=True)
