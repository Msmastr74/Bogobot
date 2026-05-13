import discord

from typing import Literal, TYPE_CHECKING, TypedDict
if TYPE_CHECKING:
    from main import BotCore

RANKS = {
    "0": "basic",
    "1": "authorized",
    "2": "mod",
    "3": "admin",
    "4": "owner"
}

class Account(TypedDict):
    perm_level: int

async def setup(bot: "BotCore"):
    from utils import groups
    
    accounts = groups.accounts(bot)
    
    @accounts.command(name="perm_edit", description="Edits a user's rank")
    async def perm_edit(
        interaction: discord.Interaction, action: Literal['promote', 'demote', 'set'],
        user: discord.Member, level: Literal['basic', 'authorized', 'mod', 'admin'] | None = None
    ):
        if str(user.id) not in bot.accounts:
            await bot.discord.send(contents="User not found in accounts database", response=True, ephemeral=True)
            return
        current_rank = bot.accounts[str(user.id)]["perm_level"]
        new_rank: int | None = None
        if action != "set" and level is not None:
            await bot.discord.send(contents="Level argument should not be provided unless using the set action", response=True, ephemeral=True)
            return
        if action == "promote":
            new_rank = current_rank + 1
        elif action == "deomote":
            new_rank = current_rank - 1
        elif action == "set":
            if level:
                if level == 'basic':
                    new_rank = 0
                elif level == 'authorized':
                    new_rank = 1
                elif level == 'mod':
                    new_rank = 2
                elif level == 'admin':
                    new_rank = 3
                else:
                    await bot.discord.send(contents="Invalid rank level provided", response=True, ephemeral=True)
                    return
            else:
                await bot.discord.send(contents="Must provide level in order to use the set action", response=True, ephemeral=True)
                return
        assert new_rank is not None
        cur_rank_name = RANKS.get(str(current_rank), "Unknown")
        new_rank_name = RANKS.get(str(new_rank), "Unknown")

        own_rank = bot.accounts[str(interaction.user.id)]["perm_level"]
        if own_rank > current_rank and own_rank > new_rank:
            if current_rank == new_rank:
                await bot.discord.send(contents="New rank must be different from old rank", response=True, ephemeral=True)
                return
            bot.accounts[str(user.id)]["perm_level"] = new_rank
            await bot.save_accounts()
            if current_rank < new_rank:
                await bot.discord.send(
                    contents=f"Successfully promoted <@{user.id}> from {cur_rank_name} to {new_rank_name}", response=True, ephemeral=True
                )
            elif current_rank > new_rank:
                await bot.discord.send(
                    contents=f"Successfully demoted <@{user.id}> from {cur_rank_name} to {new_rank_name}", response=True, ephemeral=True
                )
        elif own_rank <= current_rank:
            await bot.discord.send(contents=f"Must overrank {cur_rank_name} to edit rank to {new_rank_name}", response=True, ephemeral=True)
        elif own_rank <= new_rank:
            await bot.discord.send(contents=f"Must overrank {new_rank_name} to edit rank to {new_rank_name}", response=True, ephemeral=True)
    
    @accounts.command(name="perm_info", description="Gets a user's current rank")
    async def perm_info(interaction: discord.Interaction, user: discord.Member):
        if str(user.id) not in bot.accounts:
            await bot.discord.send(contents="User not found in accounts database", response=True, ephemeral=True)
            return
        current_rank = bot.accounts[str(user.id)]["perm_level"]
        current_rank_name = RANKS.get(str(current_rank), "Unknown")
        await bot.discord.send(contents=f"<@{user.id}>'s current rank is {current_rank_name}", response=True, ephemeral=True)
