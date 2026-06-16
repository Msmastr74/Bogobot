from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord

from modlog.actions import ModlogAction, register
from modlog.audit_log import ModlogChange, ModlogEntity, ModlogEvent
from modlog.database import ModlogDatabase


MODLOG_CONFIG_KEY = "modlog"
DEFAULT_MODLOG_DATABASE_PATH = "modlog.sqlite3"
ModlogWriteCallback = Callable[..., Awaitable[None]]


def database_path_from_bot(bot: Any) -> Path:
    config = getattr(bot, "config", {})
    if isinstance(config, dict):
        modlog_config = config.get(MODLOG_CONFIG_KEY)
        if isinstance(modlog_config, dict):
            path = modlog_config.get("database_path")
            if isinstance(path, str) and path:
                return Path(path)
    return Path(DEFAULT_MODLOG_DATABASE_PATH)


def interaction_user_entity(user: discord.User | discord.Member) -> ModlogEntity:
    return ModlogEntity(
        id=user.id,
        type=type(user).__name__,
        data={
            "name": user.name,
            "display_name": getattr(user, "display_name", None),
            "global_name": getattr(user, "global_name", None),
            "bot": user.bot,
        },
    )


def interaction_channel_entity(interaction: discord.Interaction) -> ModlogEntity | None:
    if interaction.channel_id is None:
        return None
    channel = interaction.channel
    return ModlogEntity(
        id=interaction.channel_id,
        type=type(channel).__name__ if channel is not None else "Channel",
        data={
            "name": getattr(channel, "name", None),
        },
    )


def interaction_raw(interaction: discord.Interaction) -> dict[str, Any]:
    command = interaction.command
    return {
        "interaction_id": interaction.id,
        "command": getattr(command, "qualified_name", None) or getattr(command, "name", None),
        "guild_id": interaction.guild_id,
        "channel_id": interaction.channel_id,
        "user_id": interaction.user.id,
    }


def role_entity(role: discord.Role) -> ModlogEntity:
    return ModlogEntity(
        id=role.id,
        type="Role",
        data={
            "name": role.name,
            "mention": role.mention,
        },
    )


def message_entity(message: discord.Message) -> ModlogEntity:
    return ModlogEntity(
        id=message.id,
        type="Message",
        data={
            "channel_id": message.channel.id,
            "jump_url": message.jump_url,
        },
    )


def modlog_writer(action: ModlogAction) -> ModlogWriteCallback:
    registered_action = register(action)

    async def write(
        interaction: discord.Interaction,
        *,
        target: ModlogEntity | None = None,
        extra: Any = None,
        changes: Sequence[ModlogChange] = (),
        raw: dict[str, Any] | None = None,
    ) -> None:
        if interaction.guild_id is None:
            return

        event_raw = {
            "interaction": interaction_raw(interaction),
        }
        if raw is not None:
            event_raw.update(raw)

        database = ModlogDatabase(database_path_from_bot(interaction.client))
        database.write_event(
            ModlogEvent(
                id=interaction.id,
                guild_id=interaction.guild_id,
                source="bogobot_management",
                action=registered_action.name,
                imported_at=datetime.now(timezone.utc),
                actor=interaction_user_entity(interaction.user),
                target=target if target is not None else interaction_channel_entity(interaction),
                extra=extra,
                changes=list(changes),
                raw=event_raw,
            ),
            replace=True,
        )

    return write
