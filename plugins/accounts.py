import discord

from typing import Literal, TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

RANKS = {
    "0": "basic",
    "1": "authorized",
    "2": "mod",
    "3": "admin",
    "4": "owner"
}

async def setup(bot: "BotCore"):
    from utils import groups
    
    accounts = groups.accounts(bot)
    
    @accounts.command(name="perm_edit", description="Edits a user's rank")
    async def perm_edit(interaction: discord.Interaction, action: Literal['promote', 'demote', 'set'], user: discord.Member, level: Literal['basic', 'authorized', 'mod', 'admin'] | None = None):
        old_rank = bot.accounts[str(user.id)]["perm_level"]
        if action == "promote":
            new_rank = old_rank + 1
        elif action == "deomote":
            new_rank = old_rank - 1
        elif action == "set":
            if level:
                new_rank = 0 if level == 'basic' else 1 if level == 'authorized' else 2 if level == 'mod' else 3 if level == 'admin' else None
            else:
                await bot.discord.send(contents="Must provide level in order to use the set action", response=True, ephemeral=True)
                return
        if bot.accounts[str(interaction.user.id)]["perm_level"] > old_rank and bot.accounts[str(interaction.user.id)]["perm_level"] > new_rank:
            bot.accounts[str(user.id)]["perm_level"] = new_rank
            if old_rank < new_rank:
                await bot.discord.send(contents=f"Successfully promoted <@{user.id}> from {RANKS.get(str(old_rank), None)} to {RANKS.get(str(new_rank), None)}", response=True, ephemeral=True)
            elif old_rank > new_rank:
                await bot.discord.send(contents=f"Successfully demoted <@{user.id}> from {RANKS.get(str(old_rank), None)} to {RANKS.get(str(new_rank), None)}", response=True, ephemeral=True)
            else:
                await bot.discord.send(contents=f"New rank must be different from old rank", response=True, ephemeral=True)
            bot.save_accounts
        elif bot.accounts[str(interaction.user.id)]["perm_level"] <= old_rank:
            await bot.discord.send(contents="Must overrank user to edit rank", response=True, ephemeral=True)
        elif bot.accounts[str(interaction.user.id)]["perm_level"] <= new_rank:
            await bot.discord.send(contents="Must overrank user's new rank to edit rank", response=True, ephemeral=True)