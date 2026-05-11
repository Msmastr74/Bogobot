import discord

from typing import Literal
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

async def setup(bot: 'BotCore'):
    from utils import groups

    manage = groups.manage(bot)

    @manage.command(name="auth", description="Show or set a user's authorization level", perm_requirement=2)
    async def auth(
        interaction: discord.Interaction,
        action: Literal["set", "info"],
        user: discord.Member,
        level: int | None = None,
    ):
        current_level = bot.authorization_level(user.id)

        if action == "info":
            return await bot.discord.send(
                f"{user.mention} has authorization level {current_level}.",
                response=True,
            )

        caller_level = bot.authorization_level(interaction.user.id)

        if level is None:
            return await bot.discord.send("Level is required when setting authorization.", response=True)

        if user.id == bot.config.get("owner_uid"):
            return await bot.discord.send("The owner is always authorization level 3.", response=True)

        if level < 0:
            return await bot.discord.send("Authorization level cannot be negative.", response=True)

        if level >= caller_level:
            return await bot.discord.send(
                f"You can only set levels lower than your own level ({caller_level}).",
                response=True,
            )

        authorized_users = bot.config.setdefault("authorized_users", {})
        if not isinstance(authorized_users, dict):
            authorized_users = {}
            bot.config["authorized_users"] = authorized_users

        if level == 0:
            removed = authorized_users.pop(str(user.id), None) is not None
            await bot.save_config()
            return await bot.discord.send(
                f"{user.mention} has been removed from authorization."
                if removed
                else f"{user.mention} already has authorization level 0.",
                response=True,
            )
        
        if authorized_users.get(str(user.id)) == level:
            return await bot.discord.send(
                f"{user.mention} already has authorization level {level}.",
                response=True,
            )

        authorized_users[str(user.id)] = level
        await bot.save_config()
        return await bot.discord.send(
            f"{user.mention} authorization level set to {level}.",
            response=True,
        )
