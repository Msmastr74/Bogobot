import discord

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

async def setup(bot: 'BotCore'):
    from utils import groups

    manage = groups.manage(bot)

    @manage.command(name="authorize", description="Add a user to the authorization list", perm_requirement=2)
    async def authorize(interaction: discord.Interaction, user: discord.Member):
        if user.id not in bot.config["authorized_users"]:
            bot.config["authorized_users"].append(user.id)
            await bot.save_config()
            return await bot.discord.send(f"<:Sorted:1495837069996720249> User {user.display_name} has been authorized", response=True)
        else:
            return await bot.discord.send(f"<:Unsorted:1495837051235598346> User {user.display_name} is already authorized", response=True)
    @manage.command(name="deauthorize", description="Revokes a user's authorization", perm_requirement=2)
    async def deauthorize(interaction: discord.Interaction, user: discord.Member):
        if user.id in bot.config["authorized_users"]:
            bot.config["authorized_users"].remove(user.id)
            await bot.save_config()
            return await bot.discord.send(f"<:Sorted:1495837069996720249> User {user.display_name} has been deauthorized", response=True)
        else:
            return await bot.discord.send(f"<:Unsorted:1495837051235598346> User {user.display_name} isnt authorized", response=True)
