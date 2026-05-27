import discord

from typing import Iterable, Literal
from bogobot_core import BotCore
from utils.accounts import AccountRecord
from utils import groups
from utils.discord import count_characters

Rank = Literal['basic', 'authorized', 'mod', 'admin', 'owner']
NAMES: dict[int, str] = {
    -1: "banned",
    0: "basic",
    1: "authorized",
    2: "mod",
    3: "admin",
    4: "owner"
}
RANK_NUMS: dict[Rank, int] = {
    "basic": 0,
    "authorized": 1,
    "mod": 2,
    "admin": 3,
    "owner": 4
}
class AccountListView(discord.ui.LayoutView):
    def __init__(
        self, *,
        title: str = "Accounts",
        error_text: str = "No accounts found",
        truncated_text: str = "...",
        accounts: Iterable[tuple[str, AccountRecord]]
    ) -> None:
        super().__init__(timeout=None)
        remaining = 3900 # reserve 100
        def count_remaining(text: str) -> bool:
            nonlocal remaining
            remaining -= count_characters(text)
            return remaining >= 0
        title_text = f"## {title}"
        count_remaining(title_text)
        self.add_item(discord.ui.TextDisplay(title_text))
        
        accounts_container = discord.ui.Container()
        found_account = False
        for uid, account in accounts:
            found_account = True
            account_text = f"<@{uid}>: {NAMES.get(account['perm_level'], 'Unknown')}"
            if count_remaining(account_text):
                accounts_container.add_item(
                    discord.ui.TextDisplay(account_text)
                )
            else:
                accounts_container.add_item(
                    discord.ui.TextDisplay(truncated_text)
                )
                break
        if not found_account:
            accounts_container.add_item(
                discord.ui.TextDisplay(error_text)
            )
        self.add_item(accounts_container)

async def setup(bot: BotCore):
    accounts = groups.accounts(bot)

    @bot.connect_callback
    async def load_accounts():
        guild_count = 0
        member_count = 0
        added_member_count = 0
        guild_member_count = 0
        added_guild_member_count = 0
        bot.logger.info("Beginning automatic account creation...")
        for guild in bot.guilds:
            guild_count += 1
            guild_member_ids: list[int] = []
            for member in guild.members:
                guild_member_count += 1
                member_count += 1
                guild_member_ids.append(member.id)
            added_guild_member_count = await bot.accounts.ensure_accounts(guild_member_ids)
            added_member_count += added_guild_member_count
            bot.logger.info(
                f"Automatically created {added_guild_member_count} accounts out of {guild_member_count} members from {guild.name} ({guild.id})."
            )
            guild_member_count = 0
            added_guild_member_count = 0

        await bot.accounts.normalize_owner(bot.config["owner_uid"])
        await bot.save_accounts()
        bot.logger.info(
            f"Automatic account creation finished. Automatically created a total of {added_member_count} accounts out of a total of {member_count} members from {guild_count} servers."
        )
    
    @bot.member_join_callback
    async def on_member_join(member: discord.Member | discord.User):
        count = await bot.accounts.ensure_accounts([member.id])
        await bot.accounts.normalize_owner(bot.config["owner_uid"])
        await bot.save_accounts()
        if count > 0:
            bot.logger.info(
                f"Automatically created an account for <@{member.id}> ({member.name}){f' from guild {member.guild.name} ({member.guild.id})' if isinstance(member, discord.Member) else ''}."
            )
    
    @bot.guild_join_callback
    async def on_guild_join(guild: discord.Guild):
        bot.logger.info(
            f"Bot joined new guild {guild.name} ({guild.id}); restarting automatic account creation..."
        )
        await load_accounts()
    
    @accounts.command(name="perm_edit", description="Edits a user's rank")
    async def perm_edit(
        interaction: discord.Interaction, action: Literal['promote', 'demote', 'set'],
        user: discord.Member, level: Rank | None = None
    ):
        if str(user.id) == str(interaction.user.id):
            # Lockout prevention
            await bot.discord.send(contents="You cannot edit your own rank", response=True, ephemeral=True)
            return
        current_rank = await bot.accounts.permission_level(user.id)
        new_rank: int | None = None
        if action != "set" and level is not None:
            await bot.discord.send(contents="Level argument should not be provided unless using the set action", response=True, ephemeral=True)
            return
        if action == "promote":
            new_rank = current_rank + 1
        elif action == "demote":
            new_rank = current_rank - 1
        elif action == "set":
            if level:
                new_rank = RANK_NUMS[level]
            else:
                await bot.discord.send(contents="Must provide level in order to use the set action", response=True, ephemeral=True)
                return
        assert new_rank is not None
        cur_rank_name = NAMES.get(current_rank, "Unknown")
        new_rank_name = NAMES.get(new_rank, "Unknown")

        result, current_rank, own_rank = await bot.accounts.set_permission_level_if_overranked(
            actor_uid=interaction.user.id,
            target_uid=user.id,
            new_level=new_rank,
        )
        cur_rank_name = NAMES.get(current_rank, "Unknown")
        if result == "ok":
            if current_rank < new_rank:
                await bot.discord.send(
                    contents=f"Successfully promoted <@{user.id}> from {cur_rank_name} to {new_rank_name}", response=True, ephemeral=True
                )
            elif current_rank > new_rank:
                await bot.discord.send(
                    contents=f"Successfully demoted <@{user.id}> from {cur_rank_name} to {new_rank_name}", response=True, ephemeral=True
                )
        elif result == "same":
            await bot.discord.send(contents="New rank must be different from old rank", response=True, ephemeral=True)
        elif result == "actor_not_over_current":
            await bot.discord.send(contents=f"Must overrank {cur_rank_name} to edit rank to {new_rank_name}", response=True, ephemeral=True)
        elif result == "actor_not_over_new":
            await bot.discord.send(contents=f"Must overrank {new_rank_name} to edit rank to {new_rank_name}", response=True, ephemeral=True)
    
    @accounts.command(name="perm_info", description="Gets a user's current rank")
    async def perm_info(interaction: discord.Interaction, user: discord.Member):
        current_rank = await bot.accounts.permission_level(user.id)
        current_rank_name = NAMES.get(current_rank, "Unknown")
        await bot.discord.send(
            contents=f"<@{user.id}>'s current rank is {current_rank_name}",
            response=True, ephemeral=True,
            safety_filter=True
        )
    
    @accounts.command(name="list_users", description="List users in the accounts database")
    async def list_users(
        interaction: discord.Interaction, minimum_rank: Rank | None
    ):
        filtered_accounts: Iterable[tuple[str, AccountRecord]] = await bot.accounts.items()
        is_filtered = minimum_rank is not None
        if is_filtered:
            minimum_rank_num = RANK_NUMS[minimum_rank]
            filtered_accounts = filter(
                lambda x: x[1]["perm_level"] >= minimum_rank_num,
                filtered_accounts
            )
        view = AccountListView(
            title=f"Accounts with rank {minimum_rank} or higher" if 
                is_filtered else "All accounts",
            accounts=filtered_accounts,
            error_text="No accounts found with the specified criteria." if
                is_filtered else "No accounts found.",
        )
        await bot.discord.send(
            view=view,
            response=True, ephemeral=True,
            safety_filter=True
        )
