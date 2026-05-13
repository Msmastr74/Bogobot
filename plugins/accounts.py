import discord

from typing import Literal, TYPE_CHECKING, TypedDict
if TYPE_CHECKING:
    from main import BotCore

Rank = Literal['basic', 'authorized', 'mod', 'admin', 'owner']
RANKS: dict[int, Rank] = {
    0: "basic",
    1: "authorized",
    2: "mod",
    3: "admin",
    4: "owner"
}
RANK_NUMS: dict[Rank, int] = dict((v, k) for k, v in RANKS.items())

class Account(TypedDict):
    perm_level: int

async def setup(bot: "BotCore"):
    from utils import groups
    
    accounts = groups.accounts(bot)
    
    @accounts.command(name="perm_edit", description="Edits a user's rank")
    async def perm_edit(
        interaction: discord.Interaction, action: Literal['promote', 'demote', 'set'],
        user: discord.Member, level: Rank | None = None
    ):
        if str(user.id) == str(interaction.user.id):
            # Lockout prevention
            await bot.discord.send(contents="You cannot edit your own rank", response=True, ephemeral=True)
            return
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
                new_rank = RANK_NUMS[level]
            else:
                await bot.discord.send(contents="Must provide level in order to use the set action", response=True, ephemeral=True)
                return
        assert new_rank is not None
        cur_rank_name = RANKS.get(current_rank, "Unknown")
        new_rank_name = RANKS.get(new_rank, "Unknown")

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
        current_rank_name = RANKS.get(current_rank, "Unknown")
        await bot.discord.send(
            contents=f"<@{user.id}>'s current rank is {current_rank_name}",
            response=True, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none()
        )
    
    @accounts.command(name="list_users", description="List users in the accounts database")
    async def list_users(
        interaction: discord.Interaction, minimum_rank: Rank | None
    ):
        text = ""
        for uid, info in bot.accounts.items():
            rank_num = info["perm_level"]
            rank_name = RANKS.get(rank_num, "Unknown")
            if minimum_rank is not None and rank_num < RANK_NUMS[minimum_rank]:
                continue
            new_text = text + f"<@{uid}>: {rank_name}\n"
            if len(new_text) > 4000:
                text += "[...truncated...]"
                break
            text = new_text
        if text == "":
            text = "No users found with the specified criteria."
        await bot.discord.send_embed(
            title=f"Accounts with rank {minimum_rank} or higher" if minimum_rank else "All accounts",
            contents=text,
            response=True, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none()
        )
