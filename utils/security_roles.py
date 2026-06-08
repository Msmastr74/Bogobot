from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from bogobot_core import BotCore


CONFIG_KEY = "verification"
VERIFIED_ROLE_ID_KEY = "verified_role_id"
QUARANTINE_ROLE_ID_KEY = "quarantine_role_id"


def role_config(bot: "BotCore") -> dict[str, object]:
    raw = bot.config.get(CONFIG_KEY)
    if isinstance(raw, dict):
        return raw
    config: dict[str, object] = {}
    bot.config[CONFIG_KEY] = config
    return config


def _role_id(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def verified_role_id(bot: "BotCore") -> int | None:
    return _role_id(role_config(bot).get(VERIFIED_ROLE_ID_KEY))


def quarantine_role_id(bot: "BotCore") -> int | None:
    return _role_id(role_config(bot).get(QUARANTINE_ROLE_ID_KEY))


def configured_role(bot: "BotCore", guild: discord.Guild, key: str) -> discord.Role | None:
    role_id = _role_id(role_config(bot).get(key))
    if role_id is None:
        return None
    return guild.get_role(role_id)


def verified_role(bot: "BotCore", guild: discord.Guild) -> discord.Role | None:
    return configured_role(bot, guild, VERIFIED_ROLE_ID_KEY)


def quarantine_role(bot: "BotCore", guild: discord.Guild) -> discord.Role | None:
    return configured_role(bot, guild, QUARANTINE_ROLE_ID_KEY)


def has_role_id(member: discord.Member, role_id: int | None) -> bool:
    return role_id is not None and any(role.id == role_id for role in member.roles)


async def set_roles(
    bot: "BotCore",
    *,
    verified: discord.Role,
    quarantine: discord.Role,
) -> None:
    config = role_config(bot)
    config[VERIFIED_ROLE_ID_KEY] = verified.id
    config[QUARANTINE_ROLE_ID_KEY] = quarantine.id
    await bot.save_config()


async def set_verified_role(bot: "BotCore", role: discord.Role) -> None:
    role_config(bot)[VERIFIED_ROLE_ID_KEY] = role.id
    await bot.save_config()


async def set_quarantine_role(bot: "BotCore", role: discord.Role) -> None:
    role_config(bot)[QUARANTINE_ROLE_ID_KEY] = role.id
    await bot.save_config()
