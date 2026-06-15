from datetime import datetime, timezone
import itertools
from typing import Any

import discord

from modlog.audit_log import ModlogChange, ModlogEntity, ModlogEvent


_event_counter = itertools.count(1)


def generated_event_id() -> int:
    base = discord.utils.time_snowflake(datetime.now(timezone.utc), high=False)
    return base + (next(_event_counter) % (2**22))


def user_entity(user: discord.abc.User | None) -> ModlogEntity | None:
    if user is None:
        return None
    data: dict[str, Any] = {
        "name": user.name,
        "display_name": getattr(user, "display_name", None),
        "global_name": getattr(user, "global_name", None),
        "bot": user.bot,
    }
    return ModlogEntity(id=user.id, type=type(user).__name__, data=data)


def channel_entity(channel: discord.abc.GuildChannel | discord.Thread | None) -> ModlogEntity | None:
    if channel is None:
        return None
    return ModlogEntity(
        id=channel.id,
        type=type(channel).__name__,
        data={"name": getattr(channel, "name", None)},
    )


def role_snapshot(role: discord.Role) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "position": role.position,
        "managed": role.managed,
    }


def member_snapshot(member: discord.Member | discord.User) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": member.id,
        "name": member.name,
        "display_name": getattr(member, "display_name", None),
        "global_name": getattr(member, "global_name", None),
        "bot": member.bot,
        "created_at": member.created_at.isoformat(),
    }
    if isinstance(member, discord.Member):
        data.update({
            "guild_id": member.guild.id,
            "nick": member.nick,
            "joined_at": member.joined_at.isoformat() if member.joined_at else None,
            "pending": member.pending,
            "timed_out_until": member.timed_out_until.isoformat() if member.timed_out_until else None,
            "roles": [role_snapshot(role) for role in member.roles if role.name != "@everyone"],
        })
    return data


def attachment_snapshot(attachment: discord.Attachment) -> dict[str, Any]:
    return {
        "id": attachment.id,
        "filename": attachment.filename,
        "url": attachment.url,
        "proxy_url": attachment.proxy_url,
        "size": attachment.size,
        "content_type": attachment.content_type,
        "description": attachment.description,
        "spoiler": attachment.is_spoiler(),
    }


def message_snapshot(message: discord.Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "channel_id": message.channel.id,
        "guild_id": message.guild.id if message.guild else None,
        "author": member_snapshot(message.author),
        "content": message.content,
        "clean_content": message.clean_content,
        "created_at": message.created_at.isoformat(),
        "edited_at": message.edited_at.isoformat() if message.edited_at else None,
        "pinned": message.pinned,
        "tts": message.tts,
        "type": str(message.type),
        "jump_url": message.jump_url,
        "attachments": [attachment_snapshot(attachment) for attachment in message.attachments],
        "embeds": [embed.to_dict() for embed in message.embeds],
        "mentions": [user.id for user in message.mentions],
        "role_mentions": [role.id for role in message.role_mentions],
    }


def message_event(
    *,
    action: str,
    message: discord.Message,
    before: discord.Message | None = None,
    bulk: bool = False,
) -> ModlogEvent | None:
    if message.guild is None:
        return None

    channel = (
        channel_entity(message.channel)
        if isinstance(message.channel, discord.abc.GuildChannel | discord.Thread) else
        None
    )
    changes: list[ModlogChange] = []
    if before is not None and before.content != message.content:
        changes.append(ModlogChange(
            key="content",
            old=before.content,
            new=message.content,
            has_old=True,
            has_new=True,
        ))

    return ModlogEvent(
        id=generated_event_id(),
        guild_id=message.guild.id,
        source="discord_gateway",
        action=action,
        imported_at=datetime.now(timezone.utc),
        actor=user_entity(message.author),
        target=user_entity(message.author),
        changes=changes,
        raw={
            "message": message_snapshot(message),
            "before_message": message_snapshot(before) if before is not None else None,
            "bulk": bulk,
            "channel": channel.model_dump(exclude_none=True) if channel is not None else None,
        },
    )


def member_join_event(member: discord.Member | discord.User) -> ModlogEvent | None:
    guild = member.guild if isinstance(member, discord.Member) else None
    if guild is None:
        return None
    return ModlogEvent(
        id=generated_event_id(),
        guild_id=guild.id,
        source="discord_gateway",
        action="member_join",
        imported_at=datetime.now(timezone.utc),
        target=user_entity(member),
        raw={"member": member_snapshot(member)},
    )


def member_remove_event(member: discord.Member | discord.User) -> ModlogEvent | None:
    guild = member.guild if isinstance(member, discord.Member) else None
    if guild is None:
        return None
    return ModlogEvent(
        id=generated_event_id(),
        guild_id=guild.id,
        source="discord_gateway",
        action="member_remove",
        imported_at=datetime.now(timezone.utc),
        target=user_entity(member),
        raw={"member": member_snapshot(member)},
    )


def member_ban_event(guild: discord.Guild, user: discord.User | discord.Member) -> ModlogEvent:
    return ModlogEvent(
        id=generated_event_id(),
        guild_id=guild.id,
        source="discord_gateway",
        action="ban",
        imported_at=datetime.now(timezone.utc),
        target=user_entity(user),
        raw={"member": member_snapshot(user)},
    )


def member_unban_event(guild: discord.Guild, user: discord.User) -> ModlogEvent:
    return ModlogEvent(
        id=generated_event_id(),
        guild_id=guild.id,
        source="discord_gateway",
        action="unban",
        imported_at=datetime.now(timezone.utc),
        target=user_entity(user),
        raw={"member": member_snapshot(user)},
    )


def member_update_events(before: discord.Member, after: discord.Member) -> list[ModlogEvent]:
    events: list[ModlogEvent] = []
    before_roles = {role.id: role for role in before.roles if role.name != "@everyone"}
    after_roles = {role.id: role for role in after.roles if role.name != "@everyone"}
    added_roles = [role_snapshot(after_roles[role_id]) for role_id in sorted(after_roles.keys() - before_roles.keys())]
    removed_roles = [role_snapshot(before_roles[role_id]) for role_id in sorted(before_roles.keys() - after_roles.keys())]

    if added_roles or removed_roles:
        events.append(ModlogEvent(
            id=generated_event_id(),
            guild_id=after.guild.id,
            source="discord_gateway",
            action="member_role_update",
            imported_at=datetime.now(timezone.utc),
            target=user_entity(after),
            changes=[ModlogChange(
                key="roles",
                old=removed_roles,
                new=added_roles,
                has_old=bool(removed_roles),
                has_new=bool(added_roles),
            )],
            raw={
                "before_member": member_snapshot(before),
                "after_member": member_snapshot(after),
            },
        ))

    changes: list[ModlogChange] = []
    for key in ("nick", "pending", "timed_out_until", "display_name"):
        old = getattr(before, key, None)
        new = getattr(after, key, None)
        if isinstance(old, datetime):
            old = old.isoformat()
        if isinstance(new, datetime):
            new = new.isoformat()
        if old != new:
            changes.append(ModlogChange(key=key, old=old, new=new, has_old=True, has_new=True))

    if changes:
        events.append(ModlogEvent(
            id=generated_event_id(),
            guild_id=after.guild.id,
            source="discord_gateway",
            action="member_update",
            imported_at=datetime.now(timezone.utc),
            target=user_entity(after),
            changes=changes,
            raw={
                "before_member": member_snapshot(before),
                "after_member": member_snapshot(after),
            },
        ))

    return events
