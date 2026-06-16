from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re
from typing import Any, Literal, Protocol

import discord
from discord import app_commands

from bogobot_core import BotCore
from plugins.bogotree import BOGOTREE_ACCOUNT_KEY, normalize_user_stats as bogotree_user_stats
from plugins.cbogo import CBOGO_ACCOUNT_KEY, normalize_user_stats as cbogo_user_stats
from plugins.telemetry import format_user_usage, user_usage
from utils import groups
from utils.accounts import (
    BANNED_CAPABILITY,
    Account,
    AccountPermissions,
    AccountRecord,
    PERMISSIONS_KEY,
    default_capabilities,
)
from utils.discord import count_characters


DiscordMentionable = discord.Member | discord.User | discord.Role
OWNER_CAPABILITY_DEPTH = 100
ACCOUNT_BAN_CAPABILITY = "accounts.ban"
MANAGE_CAPABILITIES_CAPABILITY = "capabilities.manage"
MANAGE_PRESETS_CAPABILITY = "capabilities.manage_presets"
CUSTOM_PRESETS_CONFIG_KEY = "account_capability_presets"
SERVER_PRESET_PREFIX = "server."
CAPABILITY_OPERATION_SUFFIXES = {"use", "grant"}
PRESET_NAME_RE = re.compile(r"^(?:server\.)?[A-Za-z0-9_]{1,64}$")
PRESET_CAPABILITY_RE = re.compile(
    r"^\((?P<name>[A-Za-z0-9_]{1,64})\)(?P<operation>\.(?:use|grant))?$"
)
INTERNAL_SERVER_PRESET_CAPABILITY_RE = re.compile(
    r"^\(server:(?P<name>[A-Za-z0-9_]{1,64})\)(?P<operation>\.(?:use|grant))?$"
)
BASE_CAPABILITY_PRESETS: dict[str, tuple[str, ...]] = {
    "default": tuple(default_capabilities()),
    "user": ("commands", "user"),
    "ai": ("user.ai",),
    "moderator": (
        "accounts.ban.use",
        "ai.activity.use",
        "ai.manage.memory.channel.use",
        "games.bogotree.use",
        "games.cbogo.use",
        "milestones.use",
        "raid.use",
        "telemetry.use",
        "verification.use",
        "modlog.view",
        "modlog.view_sensitive"
    ),
    "admin": (
        "accounts.ban",
        "ai",
        "archive",
        "capabilities",
        "discord",
        "games.bogotree",
        "games.cbogo",
        "milestones",
        "raid",
        "system.logs",
        "system.loglevel",
        "telemetry",
        "verification",
        "modlog"
    ),
}


class ModelDump(Protocol):
    def model_dump(self) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class AccountTarget:
    mention: str
    account: Account
    account_id: int
    is_role: bool = False


def _account_perms(account: AccountRecord) -> AccountPermissions:
    value = account.get(PERMISSIONS_KEY)
    if isinstance(value, AccountPermissions):
        return value
    if isinstance(value, dict):
        return AccountPermissions.model_validate(value)
    return AccountPermissions()


def _overriding_capabilities(perms: AccountPermissions, capability: str) -> list[str]:
    capability_base, explicit_operation = AccountPermissions._split_operation(capability)
    operations = (explicit_operation,) if explicit_operation is not None else ("use", "grant")
    depth = perms.capabilities[capability]
    overriding: list[str] = []
    for other_capability, other_depth in perms.capabilities.items():
        if other_capability == capability:
            continue
        if other_depth <= depth:
            continue
        if any(
            AccountPermissions._matches(other_capability, capability_base, operation=operation)
            for operation in operations
        ):
            overriding.append(other_capability)
    return sorted(overriding)


def _display_capability(capability: str) -> str:
    match = INTERNAL_SERVER_PRESET_CAPABILITY_RE.fullmatch(capability)
    if match is not None:
        return f"server.({match['name']}){match['operation'] or ''}"
    return capability


def _format_capabilities(perms: AccountPermissions) -> str:
    if not perms.capabilities:
        return "None"
    lines: list[str] = []
    for capability, depth in sorted(perms.capabilities.items()):
        line = f"`{_display_capability(capability)}`: `{depth}`"
        overriding = _overriding_capabilities(perms, capability)
        if overriding:
            line += " (overridden by " + ", ".join(f"`{_display_capability(item)}`" for item in overriding) + ")"
        lines.append(line)
    return "\n".join(lines)


def _effective_capability_count(
    perms: AccountPermissions,
    registry: Iterable[str],
) -> int:
    registered_capabilities = tuple(registry)
    return sum(
        1
        for capability in registered_capabilities
        if not AccountPermissions.is_reserved_capability(capability)
        if perms.can_use(capability, registry=registered_capabilities)
    )


def _parse_capabilities(value: str | None) -> list[str]:
    if value is None:
        return []
    return list(dict.fromkeys(
        capability.strip()
        for capability in value.split(",")
        if capability.strip()
    ))


def _remaining_autocomplete_token(value: str) -> str:
    return value.rsplit(",", maxsplit=1)[-1].strip()


def _normalize_preset_name(name: str) -> str | None:
    name = name.strip()
    if not PRESET_NAME_RE.fullmatch(name):
        return None
    local_name = name.removeprefix(SERVER_PRESET_PREFIX)
    if local_name == "server":
        return None
    return name


def _is_server_capability(capability: str) -> bool:
    return capability.startswith(SERVER_PRESET_PREFIX)


def _is_preset_capability(capability: str) -> bool:
    local_capability = _strip_server_prefixes(capability) if _is_server_capability(capability) else capability
    return PRESET_CAPABILITY_RE.fullmatch(local_capability) is not None


def _normalize_operation_suffix(capability: str) -> str:
    parts = capability.split(".")
    if not parts or parts[-1] not in CAPABILITY_OPERATION_SUFFIXES:
        return capability
    operation = parts[-1]
    base_parts = parts[:-1]
    while base_parts and base_parts[-1] in CAPABILITY_OPERATION_SUFFIXES:
        base_parts.pop()
    return ".".join([*base_parts, operation]) if base_parts else operation


def _stored_capability(capability: str) -> str:
    is_server = _is_server_capability(capability)
    local_capability = _strip_server_prefixes(capability) if is_server else capability
    preset_match = PRESET_CAPABILITY_RE.fullmatch(local_capability)
    if preset_match is not None and is_server:
        return f"(server:{preset_match['name']}){preset_match['operation'] or ''}"
    return _normalize_operation_suffix(local_capability)


def _management_capability_for_stored(capability: str, scope_id: int | None) -> str:
    if scope_id is None:
        return capability
    match = INTERNAL_SERVER_PRESET_CAPABILITY_RE.fullmatch(capability)
    if match is not None:
        return f"server.({match['name']}){match['operation'] or ''}"
    return f"server.{capability}"


def _scope_id(interaction: discord.Interaction, capability: str) -> int | None:
    if _is_server_capability(capability):
        return interaction.guild_id
    return None


def _custom_presets(bot: BotCore) -> dict[str, tuple[str, ...]]:
    raw_presets = bot.config.get(CUSTOM_PRESETS_CONFIG_KEY)
    if not isinstance(raw_presets, dict):
        return {}

    presets: dict[str, tuple[str, ...]] = {}
    for raw_name, raw_capabilities in raw_presets.items():
        name = _normalize_preset_name(str(raw_name))
        if name is None:
            continue
        if not isinstance(raw_capabilities, list):
            continue
        capabilities = tuple(
            capability
            for raw_capability in raw_capabilities
            if isinstance(raw_capability, str)
            for capability in (raw_capability.strip(),)
            if capability
        )
        if capabilities:
            presets[name] = capabilities
    return presets


def _all_presets(bot: BotCore) -> dict[str, tuple[str, ...]]:
    return {
        **BASE_CAPABILITY_PRESETS,
        **_custom_presets(bot),
    }


def _strip_server_prefixes(capability: str) -> str:
    while capability.startswith(SERVER_PRESET_PREFIX):
        capability = capability.removeprefix(SERVER_PRESET_PREFIX)
    return capability


def _normalize_resolved_preset_capability(capability: str) -> str:
    capability = _strip_server_prefixes(capability.strip())
    capability_base, operation = AccountPermissions._split_operation(capability)
    if operation is None:
        return capability_base
    return _normalize_operation_suffix(f"{capability_base}.{operation}")


def _resolve_preset(bot: BotCore, name: str) -> tuple[str, ...]:
    presets = _all_presets(bot)
    if name.startswith("server:"):
        server_name = f"{SERVER_PRESET_PREFIX}{name.removeprefix('server:')}"
        if server_name in presets:
            return tuple(_normalize_resolved_preset_capability(capability) for capability in presets[server_name])
        return tuple(
            _normalize_resolved_preset_capability(capability)
            for capability in presets.get(name.removeprefix("server:"), ())
        )
    if name in presets:
        return tuple(_normalize_resolved_preset_capability(capability) for capability in presets[name])
    if name.startswith(SERVER_PRESET_PREFIX):
        return tuple(
            _normalize_resolved_preset_capability(capability)
            for capability in presets.get(name.removeprefix(SERVER_PRESET_PREFIX), ())
        )
    return ()


def _completion_options(bot: BotCore, *, include_server: bool) -> list[str]:
    presets = _all_presets(bot)
    normal_presets = [
        preset for preset in presets
        if not preset.startswith(SERVER_PRESET_PREFIX)
    ]
    server_presets = [
        preset.removeprefix(SERVER_PRESET_PREFIX)
        for preset in presets
        if preset.startswith(SERVER_PRESET_PREFIX)
    ]
    registered_capabilities = [
        capability
        for capability in bot.accounts.capabilities
        if not AccountPermissions.is_reserved_capability(capability)
    ]
    options = [
        *registered_capabilities,
        *(f"({preset})" for preset in normal_presets),
    ]
    if include_server:
        options.extend(f"server.{capability}" for capability in registered_capabilities)
        options.extend(f"server.({preset})" for preset in [*normal_presets, *server_presets])
    return sorted(dict.fromkeys(options))


def _is_registered_capability(bot: BotCore, capability: str) -> bool:
    stored_capability = _stored_capability(capability)
    if AccountPermissions.has_preset_segment(stored_capability):
        return _is_preset_capability(capability)
    return (
        stored_capability in bot.accounts.capabilities
    )


async def _save_custom_presets(bot: BotCore, presets: dict[str, tuple[str, ...]]) -> None:
    bot.config[CUSTOM_PRESETS_CONFIG_KEY] = {
        name: list(capabilities)
        for name, capabilities in sorted(presets.items())
    }
    await bot.save_config()


def _local_permission_scope_ids(account: Account) -> list[int]:
    return account.manager._local_scope_ids_locked(
        account.account_type,
        account.uid,
        with_permissions=True,
    )


def _stored_permissions_for_scope(account: Account, scope_id: int | None) -> AccountPermissions:
    if scope_id is None:
        return account.permissions.model_copy(deep=True)

    return account.manager._permissions(account.account_type, account.uid, scope_id).model_copy(deep=True)


def _max_effective_permission_depth(account: Account, scope_id: int | None) -> int:
    if scope_id is not None:
        return account.local(scope_id).permissions.max_depth()
    return max(
        (
            account.local(local_scope_id).permissions.max_depth()
            for local_scope_id in (None, *_local_permission_scope_ids(account))
        ),
        default=-1,
    )


def _management_permissions(bot: BotCore, uid: int, scope_id: int | None) -> AccountPermissions:
    return bot.accounts[uid].local(scope_id).permissions


def _scope_label(scope_id: int | None) -> str:
    return "global" if scope_id is None else "server"


def _account_target(
    bot: BotCore,
    interaction: discord.Interaction,
    *,
    target: DiscordMentionable | None,
    default_user: bool = False,
) -> AccountTarget | None:
    if isinstance(target, discord.Role):
        if interaction.guild_id is None:
            return None
        return AccountTarget(
            mention=target.mention,
            account=bot.accounts.role(target.id).local(interaction.guild_id),
            account_id=target.id,
            is_role=True,
        )
    if target is None:
        if not default_user:
            return None
        target = interaction.user
    if isinstance(target, discord.Member | discord.User):
        return AccountTarget(
            mention=target.mention,
            account=bot.accounts[target.id],
            account_id=target.id,
        )
    return None


def _target_scope_id(
    interaction: discord.Interaction,
    capability: str,
    target: AccountTarget,
) -> int | None:
    if target.is_role:
        return interaction.guild_id
    return _scope_id(interaction, capability)


async def _write_permissions(
    bot: BotCore,
    target: AccountTarget,
    permissions_by_scope: dict[int | None, AccountPermissions],
) -> None:
    async with bot.accounts.lock:
        for scope_id, permissions in permissions_by_scope.items():
            record = bot.accounts._writable_record_locked(
                target.account.account_type,
                target.account.uid,
                scope_id,
            )
            record[PERMISSIONS_KEY] = bot.accounts._normalize_permissions(permissions)
        bot.accounts._save_sync()


class AccountListView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str = "Accounts",
        error_text: str = "No accounts found",
        truncated_text: str = "...",
        accounts: Iterable[tuple[str, int]],
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
        for uid, capability_count in accounts:
            found_account = True
            account_text = f"<@{uid}>: `{capability_count}` capabilities"
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


class RoleAccountView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        role: discord.Role,
        account: Account,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"## {role.mention}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"### Capabilities\n{_format_capabilities(account.permissions)}"),
        ))


class CapabilitiesView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        target: str,
        account: Account,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"## Capabilities for {target}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(_format_capabilities(account.permissions)),
        ))


async def setup(bot: BotCore) -> None:
    accounts = groups.accounts(bot)
    AccountPermissions.configure_presets(lambda name: _resolve_preset(bot, name))
    bot.accounts.capabilities.register(ACCOUNT_BAN_CAPABILITY)
    bot.accounts.capabilities.register(MANAGE_CAPABILITIES_CAPABILITY)
    bot.accounts.capabilities.register(MANAGE_PRESETS_CAPABILITY)
    bot.accounts.capabilities.register(BANNED_CAPABILITY)
    bot.accounts.capabilities.register(*default_capabilities())

    async def bootstrap_owner() -> None:
        owner_uid = bot.config.get("owner_uid")
        if owner_uid is None:
            return
        owner = bot.accounts[owner_uid]
        owner_perms = owner.permissions
        owner_perms.capabilities["[all]"] = max(
            owner_perms.capabilities.get("[all]", -1),
            OWNER_CAPABILITY_DEPTH,
        )
        await owner.write(PERMISSIONS_KEY, owner_perms)

    @bot.ready_callback
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

    async def capability_list_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        token = _remaining_autocomplete_token(current)
        prefix = current[:len(current) - len(current.rsplit(",", maxsplit=1)[-1])]
        options = _completion_options(bot, include_server=interaction.guild_id is not None)
        matches = [
            option
            for option in options
            if token.lower() in option.lower()
            and len(f"{prefix}{option}") <= 100
        ][:25]
        return [
            app_commands.Choice(name=option, value=f"{prefix}{option}")
            for option in matches
        ]

    @accounts.command(
        name="capabilities",
        description="Manages account capabilities",
        capabilities=[MANAGE_CAPABILITIES_CAPABILITY],
    )
    @app_commands.autocomplete(capabilities=capability_list_autocomplete)
    async def capabilities(
        interaction: discord.Interaction,
        action: Literal["grant", "revoke", "reset", "resolve", "show"],
        target: DiscordMentionable | None = None,
        capabilities: str | None = None,
        depth: int = 0,
    ) -> None:
        requested_capabilities = _parse_capabilities(capabilities)
        if action == "resolve":
            if not requested_capabilities:
                await bot.discord.send(
                    contents="At least one capability is required.",
                    response=True,
                    ephemeral=True,
                )
                return
            resolved_lines: list[str] = []
            for capability in requested_capabilities:
                if not _is_registered_capability(bot, capability):
                    resolved_lines.append(f"`{capability}` -> Unknown")
                    continue
                expanded = AccountPermissions.expanded_check_capabilities(
                    _stored_capability(capability),
                    registry=bot.accounts.capabilities,
                )
                if expanded:
                    resolved_lines.append(
                        f"`{capability}` -> " + ", ".join(f"`{item}`" for item in expanded)
                    )
                elif AccountPermissions.has_preset_segment(_stored_capability(capability)):
                    resolved_lines.append(f"`{capability}` -> None")
            await bot.discord.send(
                contents="\n".join(resolved_lines),
                response=True,
                ephemeral=True,
                safety_filter=True,
            )
            return

        account_target = _account_target(
            bot,
            interaction,
            target=target,
            default_user=action == "show",
        )
        if action == "show":
            if account_target is None:
                await bot.discord.send(
                    contents="A user or role target is required.",
                    response=True,
                    ephemeral=True,
                )
                return
            await bot.discord.send(
                view=CapabilitiesView(
                    target=account_target.mention,
                    account=account_target.account.local(interaction.guild_id),
                ),
                response=True,
                ephemeral=True,
                safety_filter=True,
            )
            return

        if account_target is None:
            await bot.discord.send(
                contents="A user or role target is required for this action.",
                response=True,
                ephemeral=True,
            )
            return

        if account_target.is_role and interaction.guild_id is None:
            await bot.discord.send(
                contents="Role capabilities can only be managed inside a server.",
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
            if not _is_registered_capability(bot, capability)
        ]
        if unknown_capabilities:
            await bot.discord.send(
                contents="Unknown capabilities: " + ", ".join(f"`{capability}`" for capability in unknown_capabilities),
                response=True,
                ephemeral=True,
            )
            return

        reserved_capabilities = [
            capability
            for capability in requested_capabilities
            for stored_capability in (_stored_capability(capability),)
            if AccountPermissions.is_reserved_capability(stored_capability)
        ]
        if reserved_capabilities:
            await bot.discord.send(
                contents=(
                    "`banned` and `server.banned` can only be changed through `/accounts ban`: " +
                    ", ".join(f"`{capability}`" for capability in reserved_capabilities)
                ),
                response=True,
                ephemeral=True,
            )
            return

        if account_target.is_role and action in ("grant", "revoke"):
            global_role_capabilities = [
                capability
                for capability in requested_capabilities
                if not _is_server_capability(capability)
            ]
            if global_role_capabilities:
                await bot.discord.send(
                    contents=(
                        "Role targets require explicit server-local capabilities. Use `server.` prefixes: " +
                        ", ".join(f"`server.{capability}`" for capability in global_role_capabilities)
                    ),
                    response=True,
                    ephemeral=True,
                )
                return

        if any(_is_server_capability(capability) for capability in requested_capabilities) and interaction.guild_id is None:
            await bot.discord.send(
                contents="`server.*` capabilities can only be managed inside a server.",
                response=True,
                ephemeral=True,
            )
            return

        scope_ids: set[int | None] = {
            _target_scope_id(interaction, capability, account_target)
            for capability in requested_capabilities
        }
        if action == "reset":
            if account_target.is_role:
                scope_ids = {interaction.guild_id}
            else:
                scope_ids = {
                    None,
                    *_local_permission_scope_ids(account_target.account),
                }

        actor_permissions = {
            scope_id: _management_permissions(bot, interaction.user.id, scope_id)
            for scope_id in scope_ids
        }
        target_accounts = {
            scope_id: account_target.account.local(scope_id)
            for scope_id in scope_ids
        }
        target_permissions = {
            scope_id: target_accounts[scope_id].permissions
            for scope_id in scope_ids
        }

        required_checks: list[tuple[int | None, str, tuple[str, ...], int, str]] = []
        if action == "reset":
            for scope_id, target_perms in target_permissions.items():
                for capability, current_depth in target_perms.capabilities.items():
                    if AccountPermissions.is_reserved_capability(capability):
                        continue
                    checked_capability = _management_capability_for_stored(capability, scope_id)
                    for check_group in AccountPermissions.check_capability_groups(
                        _stored_capability(checked_capability),
                        registry=bot.accounts.capabilities,
                    ):
                        check_required_depth = max(
                            current_depth,
                            max(
                                target_perms.required_modification_depth(check_capability)
                                for check_capability in check_group
                            ),
                        )
                        display_capability = (
                            " or ".join(
                                _management_capability_for_stored(check_capability, scope_id)
                                for check_capability in check_group
                            )
                            if scope_id is not None else
                            " or ".join(check_group)
                        )
                        required_checks.append((
                            scope_id,
                            display_capability,
                            check_group,
                            check_required_depth,
                            "reset",
                        ))
            if not account_target.is_role:
                for capability, default_depth in default_capabilities().items():
                    required_checks.append((None, capability, (capability,), default_depth, "reset"))
        else:
            for capability in requested_capabilities:
                scope_id = _target_scope_id(interaction, capability, account_target)
                stored_capability = _stored_capability(capability)
                current_depth = target_permissions[scope_id].required_modification_depth(stored_capability)
                required_depth = depth if action == "grant" else current_depth
                for check_group in AccountPermissions.check_capability_groups(
                    _stored_capability(capability),
                    registry=bot.accounts.capabilities,
                ):
                    check_required_depth = required_depth
                    display_capability = (
                        " or ".join(f"server.{check_capability}" for check_capability in check_group)
                        if _is_server_capability(capability) else
                        " or ".join(check_group)
                    )
                    required_checks.append((scope_id, display_capability, check_group, check_required_depth, action))

        missing: list[str] = []
        for scope_id, display_capability, stored_capabilities, required_depth, check_action in required_checks:
            actor_perms = actor_permissions[scope_id]
            if check_action in ("grant", "reset"):
                if not any(actor_perms.can_grant(stored_capability, depth=required_depth) for stored_capability in stored_capabilities):
                    missing.append(f"`{display_capability}.grant` depth `{required_depth}` in `{_scope_label(scope_id)}` scope")
            elif not any(actor_perms.can_revoke(stored_capability, depth=required_depth) for stored_capability in stored_capabilities):
                missing.append(f"`{display_capability}.grant` depth `{required_depth}` in `{_scope_label(scope_id)}` scope")

        if missing:
            await bot.discord.send(
                contents="Cannot modify capabilities atomically. Missing permission for: " + ", ".join(missing),
                response=True,
                ephemeral=True,
            )
            return

        if action == "reset":
            target_permissions = {
                scope_id: AccountPermissions(capabilities=permissions.reserved_capabilities())
                for scope_id, permissions in target_permissions.items()
            }
            global_capabilities = {
                **default_capabilities(),
                **target_permissions.get(None, AccountPermissions()).reserved_capabilities(),
            }
            if not account_target.is_role:
                target_permissions[None] = AccountPermissions(capabilities=global_capabilities)
            message = f"Reset capabilities for {account_target.mention}."
        elif action == "grant":
            for capability in requested_capabilities:
                scope_id = _target_scope_id(interaction, capability, account_target)
                target_permissions[scope_id].grant(_stored_capability(capability), depth=depth)
            capability_text = ", ".join(f"`{capability}`" for capability in requested_capabilities)
            message = f"Granted {capability_text} depth `{depth}` to {account_target.mention}."
        else:
            for capability in requested_capabilities:
                scope_id = _target_scope_id(interaction, capability, account_target)
                target_permissions[scope_id].revoke(_stored_capability(capability))
            capability_text = ", ".join(f"`{capability}`" for capability in requested_capabilities)
            message = f"Revoked {capability_text} from {account_target.mention}."

        await _write_permissions(bot, account_target, target_permissions)

        await bot.discord.send(
            contents=message,
            response=True,
            ephemeral=True,
        )
        return

    @accounts.command(
        name="preset",
        description="Manage custom account capability presets",
        capabilities=[MANAGE_PRESETS_CAPABILITY],
    )
    async def preset(
        interaction: discord.Interaction,
        action: Literal["create", "remove", "show"],
        name: str,
        capabilities: str | None = None,
    ) -> None:
        preset_name = _normalize_preset_name(name)
        if preset_name is None:
            await bot.discord.send(
                contents="Preset names must use only letters, numbers, and `_`, with an optional `server.` namespace prefix.",
                response=True,
                ephemeral=True,
            )
            return
        if action == "show":
            display_server_prefix = preset_name.startswith("server.")
            shown_capabilities = _resolve_preset(bot, preset_name)
            if not shown_capabilities:
                await bot.discord.send(
                    contents=f"Preset `{preset_name}` does not exist.",
                    response=True,
                    ephemeral=True,
                )
                return
            if display_server_prefix:
                shown_capabilities = tuple(f"server.{capability}" for capability in shown_capabilities)
            await bot.discord.send(
                contents=", ".join(shown_capabilities),
                response=True,
                ephemeral=True,
            )
            return

        if preset_name in BASE_CAPABILITY_PRESETS:
            await bot.discord.send(
                contents=f"`{preset_name}` is a built-in preset and cannot be changed.",
                response=True,
                ephemeral=True,
            )
            return

        presets = _custom_presets(bot)
        if action == "remove":
            if preset_name not in presets:
                await bot.discord.send(
                    contents=f"Custom preset `{preset_name}` does not exist.",
                    response=True,
                    ephemeral=True,
                )
                return
            del presets[preset_name]
            await _save_custom_presets(bot, presets)
            await bot.discord.send(
                contents=f"Removed custom preset `{preset_name}`.",
                response=True,
                ephemeral=True,
            )
            return

        requested_capabilities = _parse_capabilities(capabilities)
        if not requested_capabilities:
            await bot.discord.send(
                contents="At least one capability is required to create a preset.",
                response=True,
                ephemeral=True,
            )
            return
        unknown_capabilities = [
            capability
            for capability in requested_capabilities
            if not _is_registered_capability(bot, capability)
        ]
        if unknown_capabilities:
            await bot.discord.send(
                contents="Unknown capabilities: " + ", ".join(f"`{capability}`" for capability in unknown_capabilities),
                response=True,
                ephemeral=True,
            )
            return

        reserved_capabilities = [
            capability
            for capability in requested_capabilities
            if AccountPermissions.is_reserved_capability(capability)
        ]
        if reserved_capabilities:
            await bot.discord.send(
                contents=(
                    "Custom presets cannot contain reserved `banned` capabilities: " +
                    ", ".join(f"`{capability}`" for capability in reserved_capabilities)
                ),
                response=True,
                ephemeral=True,
            )
            return

        global_actor_perms = bot.accounts[interaction.user.id].permissions
        missing = [
            f"`{capability}.grant`"
            for capability in requested_capabilities
            for check_capability in AccountPermissions.expanded_check_capabilities(
                _stored_capability(capability),
                registry=bot.accounts.capabilities,
            )
            if not global_actor_perms.can_grant(check_capability)
        ]
        if missing:
            await bot.discord.send(
                contents="Cannot create preset. Missing permission for: " + ", ".join(missing),
                response=True,
                ephemeral=True,
            )
            return

        presets[preset_name] = tuple(requested_capabilities)
        await _save_custom_presets(bot, presets)
        await bot.discord.send(
            contents=(
                f"Saved custom preset `{preset_name}` with " +
                ", ".join(f"`{capability}`" for capability in requested_capabilities) +
                "."
            ),
            response=True,
            ephemeral=True,
        )

    @accounts.command(
        name="ban",
        description="Ban or unban a bot account",
        capabilities=[ACCOUNT_BAN_CAPABILITY],
    )
    async def ban(
        interaction: discord.Interaction,
        action: Literal["ban", "unban"],
        target: DiscordMentionable,
        scope: Literal["global", "server"] = "global",
    ) -> None:
        account_target = _account_target(bot, interaction, target=target, default_user=False)
        if account_target is None:
            await bot.discord.send(
                contents="A user or role target is required.",
                response=True,
                ephemeral=True,
            )
            return

        if account_target.is_role and scope == "global":
            await bot.discord.send(
                contents="Role bans can only be managed in `server` scope.",
                response=True,
                ephemeral=True,
            )
            return

        if not account_target.is_role and account_target.account_id == interaction.user.id:
            await bot.discord.send(
                contents="You cannot ban or unban yourself.",
                response=True,
                ephemeral=True,
            )
            return

        if scope == "server" and interaction.guild_id is None:
            await bot.discord.send(
                contents="Server-local bans can only be managed inside a server.",
                response=True,
                ephemeral=True,
            )
            return

        scope_id = interaction.guild_id if scope == "server" else None
        actor_perms = _management_permissions(bot, interaction.user.id, scope_id)
        stored_target_perms = _stored_permissions_for_scope(account_target.account, scope_id)
        target_max_depth = _max_effective_permission_depth(account_target.account, scope_id)
        actor_ban_depth = actor_perms.effective_depth(ACCOUNT_BAN_CAPABILITY, operation="use")
        if actor_ban_depth <= target_max_depth:
            await bot.discord.send(
                contents=(
                    f"Cannot {action} {account_target.mention}. "
                    f"`{ACCOUNT_BAN_CAPABILITY}` depth `{actor_ban_depth}` must be greater than target max depth `{target_max_depth}`."
                ),
                response=True,
                ephemeral=True,
            )
            return

        if action == "ban":
            stored_target_perms.ban(depth=target_max_depth)
            await _write_permissions(bot, account_target, {scope_id: stored_target_perms})
            await bot.discord.send(
                contents=f"{account_target.mention} has been banned from bot commands in `{scope}` scope.",
                response=True,
                ephemeral=True,
            )
            return

        stored_target_perms.unban()
        await _write_permissions(bot, account_target, {scope_id: stored_target_perms})
        await bot.discord.send(
            contents=f"{account_target.mention} has been unbanned from bot commands in `{scope}` scope.",
            response=True,
            ephemeral=True,
        )

    @accounts.command(
        name="list_users",
        description="List users in the accounts database",
    )
    async def list_users(
        interaction: discord.Interaction,
        capabilities: str | None = None,
    ) -> None:
        account_items = await bot.accounts.items()
        requested_capabilities = _parse_capabilities(capabilities)
        is_filtered = len(requested_capabilities) > 0
        unknown_capabilities = [
            capability
            for capability in requested_capabilities
            if not _is_registered_capability(bot, capability)
        ]
        if unknown_capabilities:
            await bot.discord.send(
                contents="Unknown capabilities: " + ", ".join(f"`{capability}`" for capability in unknown_capabilities),
                response=True,
                ephemeral=True,
            )
            return
        if any(_is_server_capability(capability) for capability in requested_capabilities) and interaction.guild_id is None:
            await bot.discord.send(
                contents="`server.*` capabilities can only be filtered inside a server.",
                response=True,
                ephemeral=True,
            )
            return
        scope_ids = {
            capability: _scope_id(interaction, capability)
            for capability in requested_capabilities
        }
        stored_capabilities = {
            capability: _stored_capability(capability)
            for capability in requested_capabilities
        }
        display_accounts: list[tuple[str, int]] = []
        registry = tuple(bot.accounts.capabilities)
        for uid, _account in account_items:
            if any(
                not bot.accounts[uid].local(scope_ids[capability]).permissions.can_use(
                    stored_capabilities[capability],
                    registry=registry,
                )
                for capability in requested_capabilities
            ):
                continue
            count_scope_id = next(
                (scope_id for scope_id in scope_ids.values() if scope_id is not None),
                None,
            )
            account_permissions = bot.accounts[uid].local(count_scope_id).permissions
            display_accounts.append((
                uid,
                _effective_capability_count(account_permissions, registry),
            ))
        if is_filtered:
            display_accounts.sort(key=lambda item: item[1], reverse=True)
        capability_title = ", ".join(f"`{capability}`" for capability in requested_capabilities)
        view = AccountListView(
            title=f"Accounts with {capability_title}" if is_filtered else "All accounts",
            accounts=display_accounts,
            error_text="No accounts found with the specified criteria." if is_filtered else "No accounts found.",
        )
        await bot.discord.send(
            view=view,
            response=True,
            ephemeral=True,
            safety_filter=True,
        )

    async def send_user_account_info(
        interaction: discord.Interaction,
        user: discord.Member | discord.User,
        *,
        eph: bool = True,
    ) -> None:
        account = bot.accounts[user.id].local(interaction.guild_id)
        await bot.discord.send(
            view=AccountView(user=user, account=account),
            ephemeral=eph,
            response=True,
            safety_filter=True,
        )

    @accounts.command(
        name="info",
        description="Gets information about a user",
        defer=False,
    )
    async def info(
        interaction: discord.Interaction,
        target: DiscordMentionable | None = None,
        eph: bool = True,
    ) -> None:
        account_target = _account_target(bot, interaction, target=target, default_user=True)
        if account_target is None:
            await bot.discord.send(
                contents="A user or role target is required.",
                response=True,
                ephemeral=True,
            )
            return

        if isinstance(target, discord.Role):
            view = RoleAccountView(role=target, account=account_target.account)
        else:
            user = target if isinstance(target, discord.Member | discord.User) else interaction.user
            await send_user_account_info(interaction, user, eph=eph)
            return
        await bot.discord.send(view=view, ephemeral=eph, response=True, safety_filter=True)

    @bot.setup.context_menu(
        name="Account Info",
        defer=False,
    )
    async def AccountInfo(
        interaction: discord.Interaction,
        target: discord.Member | discord.User,
    ) -> None:
        await send_user_account_info(interaction, target)
