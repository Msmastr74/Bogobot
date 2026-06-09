from collections.abc import Iterable, Mapping
from typing import Any, Literal, Protocol

import discord

from bogobot_core import BotCore
from plugins.bogotree import BOGOTREE_ACCOUNT_KEY, normalize_user_stats as bogotree_user_stats
from plugins.cbogo import CBOGO_ACCOUNT_KEY, normalize_user_stats as cbogo_user_stats
from plugins.telemetry import format_user_usage, user_usage
from utils import groups
from utils.accounts import (
    LOCAL_ACCOUNTS_KEY,
    Account,
    AccountPermissions,
    AccountRecord,
    PERMISSIONS_KEY,
    default_capabilities,
)
from utils.discord import count_characters


OWNER_CAPABILITY_DEPTH = 100
ACCOUNT_BAN_CAPABILITY = "accounts.ban"
CapabilityPreset = Literal["default", "user", "ai", "moderator", "admin"]
CAPABILITY_PRESETS: dict[CapabilityPreset, tuple[str, ...]] = {
    "default": tuple(default_capabilities()),
    "user": ("commands.*", "user.*"),
    "ai": ("user.ai",),
    "moderator": (
        "ai.activity.manage",
        "ai.activity.trigger",
        "discord.announce",
        "discord.message",
        "games.bogotree.reset",
        "games.cbogo.reset",
        "games.cbogo.reset_last_user",
        "milestones.info",
        "milestones.manage",
        "raid.exempt",
        "raid.manage",
        "raid.unquarantine",
        "telemetry.view",
        "verification.manage",
    ),
    "admin": (
        "ai.activity.manage",
        "ai.activity.trigger",
        "ai.manage",
        "archive.manage",
        "discord.announce",
        "discord.message",
        "games.bogotree.reset",
        "games.cbogo.reset",
        "games.cbogo.reset_last_user",
        "milestones.info",
        "milestones.manage",
        "raid.exempt",
        "raid.manage",
        "raid.unquarantine",
        "system.loglevel",
        "system.logs",
        "telemetry.view",
        "verification.manage",
    ),
}


class ModelDump(Protocol):
    def model_dump(self) -> dict[str, Any]:
        ...


def _account_perms(account: AccountRecord) -> AccountPermissions:
    value = account.get(PERMISSIONS_KEY)
    if isinstance(value, AccountPermissions):
        return value
    if isinstance(value, dict):
        return AccountPermissions.model_validate(value)
    return AccountPermissions()


def _format_capabilities(perms: AccountPermissions) -> str:
    if not perms.capabilities:
        return "None"
    return "\n".join(
        f"`{capability}`: `{depth}`"
        for capability, depth in sorted(perms.capabilities.items())
    )


def _parse_capabilities(value: str | None) -> list[str]:
    if value is None:
        return []
    return list(dict.fromkeys(
        capability.strip()
        for capability in value.split(",")
        if capability.strip()
    ))


def _local_permission_scope_ids(account: Account) -> list[int]:
    raw_local = account.record.get(LOCAL_ACCOUNTS_KEY)
    if not isinstance(raw_local, dict):
        return []

    scope_ids: list[int] = []
    for raw_guild_id, raw_account in raw_local.items():
        if not isinstance(raw_account, dict) or PERMISSIONS_KEY not in raw_account:
            continue
        try:
            scope_ids.append(int(raw_guild_id))
        except (TypeError, ValueError):
            continue
    return scope_ids


async def _write_permissions(
    bot: BotCore,
    uid: int,
    permissions_by_scope: dict[int | None, AccountPermissions],
) -> None:
    async with bot.accounts.lock:
        for scope_id, permissions in permissions_by_scope.items():
            record = bot.accounts._writable_record_locked(str(uid), scope_id)
            record[PERMISSIONS_KEY] = bot.accounts._normalize_permissions(permissions)
        bot.accounts._save_sync()


class AccountListView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str = "Accounts",
        error_text: str = "No accounts found",
        truncated_text: str = "...",
        accounts: Iterable[tuple[str, AccountRecord]],
    ) -> None:
        super().__init__(timeout=None)
        remaining = 3900

        def count_remaining(text: str) -> bool:
            nonlocal remaining
            remaining -= count_characters(text)
            return remaining >= 0

        title_text = f"## {title}"
        count_remaining(title_text)

        accounts_container = discord.ui.Container(
            discord.ui.TextDisplay(title_text),
            discord.ui.Separator(),
        )
        found_account = False
        text = ""
        for uid, account in accounts:
            found_account = True
            perms = _account_perms(account)
            account_text = f"<@{uid}>: `{len(perms.capabilities)}` capabilities"
            if count_remaining(account_text):
                text += account_text + "\n"
            else:
                text += truncated_text + "\n"
                break
        if not found_account:
            accounts_container.add_item(discord.ui.TextDisplay(error_text))
        else:
            accounts_container.add_item(discord.ui.TextDisplay(text[:-1]))
        self.add_item(accounts_container)


class AccountView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        user: discord.Member | discord.User,
        account: Account,
    ) -> None:
        super().__init__(timeout=None)
        container = discord.ui.Container()
        perms = account.permissions

        basic_info = [
            f"Account creation: {discord.utils.format_dt(user.created_at, style='R')}",
        ]
        if isinstance(user, discord.Member):
            joined_at = (
                discord.utils.format_dt(user.joined_at, style="R")
                if user.joined_at is not None else
                "Unknown"
            )
            basic_info.append(f"Joined server: {joined_at}")

        bogotree_data = bogotree_user_stats(account.get(BOGOTREE_ACCOUNT_KEY))
        cbogo_data = cbogo_user_stats(account.get(CBOGO_ACCOUNT_KEY))
        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(f"### {user.mention}"),
            discord.ui.TextDisplay("\n".join(basic_info)),
            accessory=discord.ui.Thumbnail(user.display_avatar.url),
        ))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            f"### Capabilities\n{_format_capabilities(perms)}"
        ))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(format_user_usage(user_usage(user.id, None), False)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self._format_data("Bogotree", bogotree_data)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(self._format_data("Cbogo", cbogo_data)))
        self.add_item(container)

    def _format_data(self, title: str, data: Mapping[str, Any] | ModelDump) -> str:
        if not isinstance(data, Mapping):
            data = data.model_dump()

        text = f"### {title}\n"
        for key, value in data.items():
            if key == "username":
                continue
            if "timestamp" in key:
                value = f"<t:{int(value)}:F>"
            label = key.replace("_", " ").capitalize()
            text += f"{label}: {value}\n"
        return text


async def setup(bot: BotCore) -> None:
    accounts = groups.accounts(bot)
    bot.accounts.capabilities.register(ACCOUNT_BAN_CAPABILITY)
    bot.accounts.capabilities.register(*default_capabilities())

    async def bootstrap_owner() -> None:
        owner_uid = bot.config.get("owner_uid")
        if owner_uid is None:
            return
        owner = bot.accounts[owner_uid]
        owner_perms = owner.permissions
        owner_perms.capabilities["*"] = max(
            owner_perms.capabilities.get("*", -1),
            OWNER_CAPABILITY_DEPTH,
        )
        await owner.write(PERMISSIONS_KEY, owner_perms)

    @bot.connect_callback
    async def load_accounts() -> None:
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

        await bootstrap_owner()
        await bot.save_accounts()
        bot.logger.info(
            f"Automatic account creation finished. Automatically created a total of {added_member_count} accounts out of a total of {member_count} members from {guild_count} servers."
        )

    @bot.member_join_callback
    async def on_member_join(member: discord.Member | discord.User) -> None:
        count = await bot.accounts.ensure_accounts([member.id])
        await bootstrap_owner()
        await bot.save_accounts()
        if count > 0:
            guild_text = (
                f" from guild {member.guild.name} ({member.guild.id})"
                if isinstance(member, discord.Member) else
                ""
            )
            bot.logger.info(
                f"Automatically created an account for <@{member.id}> ({member.name}){guild_text}."
            )

    @bot.guild_join_callback
    async def on_guild_join(guild: discord.Guild) -> None:
        bot.logger.info(
            f"Bot joined new guild {guild.name} ({guild.id}); restarting automatic account creation..."
        )
        await load_accounts()

    @accounts.command(
        name="capability",
        description="Manages account capabilities",
        capabilities=["grant.[any]"],
    )
    async def capability(
        interaction: discord.Interaction,
        action: Literal["grant", "revoke", "reset", "preset"],
        user: discord.Member,
        capabilities: str | None = None,
        preset: CapabilityPreset | None = None,
        depth: int = 0,
    ) -> None:
        if user.id == interaction.user.id:
            await bot.discord.send(
                contents="You cannot edit your own capabilities.",
                response=True,
                ephemeral=True,
            )
            return

        requested_capabilities = _parse_capabilities(capabilities)
        if action == "preset":
            if preset is None:
                await bot.discord.send(
                    contents="A preset name is required for the preset action.",
                    response=True,
                    ephemeral=True,
                )
                return
            requested_capabilities = list(CAPABILITY_PRESETS[preset])
            action = "grant"
        elif preset is not None:
            await bot.discord.send(
                contents="`preset` is only used with the preset action.",
                response=True,
                ephemeral=True,
            )
            return

        if action in ("grant", "revoke") and not requested_capabilities:
            await bot.discord.send(
                contents="At least one capability is required.",
                response=True,
                ephemeral=True,
            )
            return

        unknown_capabilities = [
            capability
            for capability in requested_capabilities
            if capability not in bot.accounts.capabilities
        ]
        if unknown_capabilities:
            await bot.discord.send(
                contents="Unknown capabilities: " + ", ".join(f"`{capability}`" for capability in unknown_capabilities),
                response=True,
                ephemeral=True,
            )
            return

        if any(capability.startswith("server.") for capability in requested_capabilities) and interaction.guild_id is None:
            await bot.discord.send(
                contents="`server.*` capabilities can only be managed inside a server.",
                response=True,
                ephemeral=True,
            )
            return

        scope_ids: set[int | None] = {
            interaction.guild_id if capability.startswith("server.") else None
            for capability in requested_capabilities
        }
        if action == "reset":
            scope_ids = {
                None,
                *_local_permission_scope_ids(bot.accounts[user.id]),
            }

        actor_permissions = {
            scope_id: bot.accounts[interaction.user.id].local(scope_id).permissions
            for scope_id in scope_ids
        }
        target_accounts = {
            scope_id: bot.accounts[user.id].local(scope_id)
            for scope_id in scope_ids
        }
        target_permissions = {
            scope_id: target_accounts[scope_id].permissions
            for scope_id in scope_ids
        }

        required_checks: list[tuple[int | None, str, int, str]] = []
        if action == "reset":
            for scope_id, target_perms in target_permissions.items():
                for capability, current_depth in target_perms.capabilities.items():
                    required_checks.append((scope_id, capability, current_depth, "reset"))
            for capability, default_depth in default_capabilities().items():
                required_checks.append((None, capability, default_depth, "reset"))
        else:
            for capability in requested_capabilities:
                scope_id = interaction.guild_id if capability.startswith("server.") else None
                current_depth = target_permissions[scope_id].depth(capability)
                required_depth = depth if action == "grant" else current_depth
                required_checks.append((scope_id, capability, required_depth, action))

        missing: list[str] = []
        for scope_id, capability, required_depth, check_action in required_checks:
            actor_perms = actor_permissions[scope_id]
            grant_capability = f"grant.{capability}"
            bot.accounts.capabilities.register(grant_capability)
            if not actor_perms.can_use(grant_capability):
                missing.append(f"`{grant_capability}`")
                continue
            if check_action in ("grant", "reset"):
                if not actor_perms.can_grant(capability, depth=required_depth):
                    missing.append(f"`{capability}` depth `{required_depth}`")
            elif not actor_perms.can_revoke(capability, depth=required_depth):
                missing.append(f"`{capability}` depth `{required_depth}`")

        if missing:
            await bot.discord.send(
                contents="Cannot modify capabilities atomically. Missing permission for: " + ", ".join(missing),
                response=True,
                ephemeral=True,
            )
            return

        if action == "reset":
            target_permissions = {
                scope_id: AccountPermissions()
                for scope_id in target_permissions
            }
            target_permissions[None] = AccountPermissions(capabilities=default_capabilities())
            message = f"Reset capabilities for {user.mention}."
        elif action == "grant":
            for capability in requested_capabilities:
                scope_id = interaction.guild_id if capability.startswith("server.") else None
                target_permissions[scope_id].grant(capability, depth=depth)
            capability_text = ", ".join(f"`{capability}`" for capability in requested_capabilities)
            message = f"Granted {capability_text} depth `{depth}` to {user.mention}."
        else:
            for capability in requested_capabilities:
                scope_id = interaction.guild_id if capability.startswith("server.") else None
                target_permissions[scope_id].revoke(capability)
            capability_text = ", ".join(f"`{capability}`" for capability in requested_capabilities)
            message = f"Revoked {capability_text} from {user.mention}."

        await _write_permissions(bot, user.id, target_permissions)

        await bot.discord.send(
            contents=message,
            response=True,
            ephemeral=True,
        )
        return

    @accounts.command(
        name="ban",
        description="Ban or unban a bot account",
        capabilities=[ACCOUNT_BAN_CAPABILITY],
    )
    async def ban(
        interaction: discord.Interaction,
        action: Literal["ban", "unban"],
        user: discord.Member,
    ) -> None:
        if user.id == interaction.user.id:
            await bot.discord.send(
                contents="You cannot ban or unban yourself.",
                response=True,
                ephemeral=True,
            )
            return

        if action == "ban":
            await _write_permissions(
                bot,
                user.id,
                {
                    scope_id: AccountPermissions()
                    for scope_id in (None, *_local_permission_scope_ids(bot.accounts[user.id]))
                },
            )
            await bot.discord.send(
                contents=f"{user.mention} has been banned from bot commands.",
                response=True,
                ephemeral=True,
            )
            return

        await _write_permissions(
            bot,
            user.id,
            {None: AccountPermissions(capabilities=default_capabilities())},
        )
        await bot.discord.send(
            contents=f"{user.mention} has been unbanned with default capabilities.",
            response=True,
            ephemeral=True,
        )

    @accounts.command(
        name="list_users",
        description="List users in the accounts database",
    )
    async def list_users(
        interaction: discord.Interaction,
        capability: str | None = None,
    ) -> None:
        filtered_accounts: Iterable[tuple[str, AccountRecord]] = await bot.accounts.items()
        is_filtered = capability is not None
        if capability is not None:
            filtered_accounts = [
                account_item
                for account_item in filtered_accounts
                if bot.accounts[account_item[0]].local(interaction.guild_id).permissions.can_use(capability)
            ]
        view = AccountListView(
            title=f"Accounts with `{capability}`" if is_filtered else "All accounts",
            accounts=filtered_accounts,
            error_text="No accounts found with the specified criteria." if is_filtered else "No accounts found.",
        )
        await bot.discord.send(
            view=view,
            response=True,
            ephemeral=True,
            safety_filter=True,
        )

    @accounts.command(
        name="info",
        description="Gets information about a user",
        defer=False,
    )
    async def info(
        interaction: discord.Interaction,
        user: discord.Member | discord.User | None = None,
        eph: bool = True,
    ) -> None:
        if user is None:
            user = interaction.user
        account = bot.accounts[user.id].local(interaction.guild_id)
        view = AccountView(user=user, account=account)
        await bot.discord.send(view=view, ephemeral=eph, response=True, safety_filter=True)
