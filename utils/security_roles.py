from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from bogobot_core import BotCore


ACCOUNT_KEY = "security_roles"
VERIFIED_ROLE_ID_KEY = "verified_role_id"
QUARANTINE_ROLE_ID_KEY = "quarantine_role_id"


def role_config(bot: "BotCore", guild_id: int | str | None = None) -> dict[str, object]:
    if guild_id is None:
        return {}

    account = bot.accounts.guild(guild_id)
    raw_config = account.get(ACCOUNT_KEY)
    if isinstance(raw_config, dict):
        return raw_config

    return {}


def _role_id(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def verified_role_id(bot: "BotCore", guild: discord.Guild | None = None) -> int | None:
    return _role_id(role_config(bot, guild.id if guild is not None else None).get(VERIFIED_ROLE_ID_KEY))


def quarantine_role_id(bot: "BotCore", guild: discord.Guild | None = None) -> int | None:
    return _role_id(role_config(bot, guild.id if guild is not None else None).get(QUARANTINE_ROLE_ID_KEY))


def configured_role(bot: "BotCore", guild: discord.Guild, key: str) -> discord.Role | None:
    role_id = _role_id(role_config(bot, guild.id).get(key))
    if role_id is None:
        return None
    return guild.get_role(role_id)


def verified_role(bot: "BotCore", guild: discord.Guild) -> discord.Role | None:
    return configured_role(bot, guild, VERIFIED_ROLE_ID_KEY)


def quarantine_role(bot: "BotCore", guild: discord.Guild) -> discord.Role | None:
    return configured_role(bot, guild, QUARANTINE_ROLE_ID_KEY)


def has_role_id(member: discord.Member, role_id: int | None) -> bool:
    return role_id is not None and any(role.id == role_id for role in member.roles)


def manageable_role_error(guild: discord.Guild, role: discord.Role, label: str) -> str | None:
    bot_member = guild.me
    if bot_member is None:
        return f"I cannot inspect my member state for `{label}`."
    if not bot_member.guild_permissions.manage_roles:
        return "I need the `Manage Roles` permission to manage verification/quarantine roles."
    if role.is_default():
        return f"`{label}` cannot be `@everyone`."
    if role.managed:
        return f"`{label}` cannot be an integration-managed role."
    if role >= bot_member.top_role:
        return f"`{label}` must be below my highest role."
    return None


async def set_roles(
    bot: "BotCore",
    *,
    verified: discord.Role,
    quarantine: discord.Role,
) -> None:
    config = dict(role_config(bot, verified.guild.id))
    config[VERIFIED_ROLE_ID_KEY] = verified.id
    config[QUARANTINE_ROLE_ID_KEY] = quarantine.id
    await bot.accounts.guild(verified.guild.id).write(ACCOUNT_KEY, config)


async def set_verified_role(bot: "BotCore", role: discord.Role) -> None:
    config = dict(role_config(bot, role.guild.id))
    config[VERIFIED_ROLE_ID_KEY] = role.id
    await bot.accounts.guild(role.guild.id).write(ACCOUNT_KEY, config)


async def set_quarantine_role(bot: "BotCore", role: discord.Role) -> None:
    config = dict(role_config(bot, role.guild.id))
    config[QUARANTINE_ROLE_ID_KEY] = role.id
    await bot.accounts.guild(role.guild.id).write(ACCOUNT_KEY, config)
