from datetime import datetime, timezone
import itertools
from typing import Any

import discord

from modlog.models import ModlogChange, ModlogEntity, ModlogEvent


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


def thread_entity(thread: discord.Thread | None, *, fallback_id: int | None = None) -> ModlogEntity | None:
    if thread is None:
        if fallback_id is None:
            return None
        return ModlogEntity(id=fallback_id, type="Thread")
    return ModlogEntity(
        id=thread.id,
        type=type(thread).__name__,
        data={
            "name": thread.name,
            "parent_id": thread.parent_id,
            "owner_id": thread.owner_id,
            "archived": thread.archived,
            "locked": thread.locked,
            "invitable": thread.invitable,
            "type": str(thread.type),
        },
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


def component_snapshot(component: discord.Component) -> dict[str, Any]:
    try:
        raw = component.to_dict()
    except Exception:
        return {"type": type(component).__name__, "repr": repr(component)}
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    return {"type": type(component).__name__, "repr": repr(component)}


def sticker_snapshot(sticker: discord.StickerItem) -> dict[str, Any]:
    return {
        "id": sticker.id,
        "name": sticker.name,
        "format": str(sticker.format),
        "url": sticker.url,
    }


def reaction_snapshot(reaction: discord.Reaction) -> dict[str, Any]:
    return {
        "emoji": str(reaction.emoji),
        "count": reaction.count,
        "me": reaction.me,
    }


def partial_emoji_snapshot(emoji: discord.PartialEmoji) -> dict[str, Any]:
    return {
        "id": emoji.id,
        "name": emoji.name,
        "animated": emoji.animated,
        "unicode": emoji.is_unicode_emoji(),
        "custom": emoji.is_custom_emoji(),
        "display": str(emoji),
    }


def thread_member_snapshot(member: discord.ThreadMember) -> dict[str, Any]:
    thread = thread_entity(member.thread)
    return {
        "id": member.id,
        "thread_id": member.thread_id,
        "joined_at": member.joined_at.isoformat(),
        "flags": member.flags,
        "thread": thread.model_dump(exclude_none=True) if thread is not None else None,
    }


def message_reference_snapshot(reference: discord.MessageReference | None) -> dict[str, Any] | None:
    if reference is None:
        return None
    try:
        raw = reference.to_dict()
    except Exception:
        return {
            "message_id": reference.message_id,
            "channel_id": reference.channel_id,
            "guild_id": reference.guild_id,
        }
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    return {
        "message_id": reference.message_id,
        "channel_id": reference.channel_id,
        "guild_id": reference.guild_id,
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
        "components": [component_snapshot(component) for component in message.components],
        "stickers": [sticker_snapshot(sticker) for sticker in message.stickers],
        "reactions": [reaction_snapshot(reaction) for reaction in message.reactions],
        "reference": message_reference_snapshot(message.reference),
        "interaction_metadata": repr(message.interaction_metadata) if message.interaction_metadata is not None else None,
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
        target=None if bulk else user_entity(message.author),
        changes=changes,
        raw={
            "message": message_snapshot(message),
            "before_message": message_snapshot(before) if before is not None else None,
            "bulk": bulk,
            "channel": channel.model_dump(exclude_none=True) if channel is not None else None,
        },
    )


def raw_delete_snapshot(payload: discord.RawMessageDeleteEvent | discord.RawBulkMessageDeleteEvent) -> dict[str, Any]:
    data: dict[str, Any] = {
        "channel_id": payload.channel_id,
        "guild_id": payload.guild_id,
    }
    if isinstance(payload, discord.RawMessageDeleteEvent):
        data.update({
            "message_id": payload.message_id,
            "cached_message": payload.cached_message is not None,
        })
    else:
        data.update({
            "message_ids": sorted(payload.message_ids),
            "cached_message_ids": sorted(message.id for message in payload.cached_messages),
        })
    return data


def minimal_deleted_message_snapshot(
    *,
    message_id: int,
    channel_id: int,
    guild_id: int,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "guild_id": guild_id,
        "cached": False,
    }


def raw_message_delete_event(payload: discord.RawMessageDeleteEvent) -> ModlogEvent | None:
    if payload.guild_id is None:
        return None
    if payload.cached_message is not None:
        event = message_event(action="on_raw_message_delete", message=payload.cached_message)
        if event is None:
            return None
        event.extra = {
            "channel_id": payload.channel_id,
            "cached_message": True,
        }
        event.raw["raw_payload"] = raw_delete_snapshot(payload)
        event.raw["cached_message"] = True
        return event

    return ModlogEvent(
        id=generated_event_id(),
        guild_id=payload.guild_id,
        source="discord_gateway",
        action="on_raw_message_delete",
        imported_at=datetime.now(timezone.utc),
        extra={
            "channel_id": payload.channel_id,
            "cached_message": False,
        },
        raw={
            "message": minimal_deleted_message_snapshot(
                message_id=payload.message_id,
                channel_id=payload.channel_id,
                guild_id=payload.guild_id,
            ),
            "raw_payload": raw_delete_snapshot(payload),
            "bulk": False,
            "cached_message": False,
        },
    )


def raw_bulk_message_delete_events(payload: discord.RawBulkMessageDeleteEvent) -> list[ModlogEvent]:
    if payload.guild_id is None:
        return []

    cached_by_id = {
        message.id: message
        for message in payload.cached_messages
    }
    events: list[ModlogEvent] = []
    for message_id in sorted(payload.message_ids):
        cached_message = cached_by_id.get(message_id)
        if cached_message is not None:
            event = message_event(action="on_bulk_message_delete", message=cached_message, bulk=True)
            if event is None:
                continue
            event.extra = {
                "channel_id": payload.channel_id,
                "cached_message": True,
            }
            event.raw["raw_payload"] = raw_delete_snapshot(payload)
            event.raw["cached_message"] = True
            events.append(event)
            continue

        events.append(ModlogEvent(
            id=generated_event_id(),
            guild_id=payload.guild_id,
            source="discord_gateway",
            action="on_bulk_message_delete",
            imported_at=datetime.now(timezone.utc),
            extra={
                "channel_id": payload.channel_id,
                "cached_message": False,
            },
            raw={
                "message": minimal_deleted_message_snapshot(
                    message_id=message_id,
                    channel_id=payload.channel_id,
                    guild_id=payload.guild_id,
                ),
                "raw_payload": raw_delete_snapshot(payload),
                "bulk": True,
                "cached_message": False,
            },
        ))

    return events


def raw_message_edit_event(payload: discord.RawMessageUpdateEvent, bot_user_id: int | None) -> ModlogEvent | None:
    if payload.guild_id is None:
        return None

    if payload.cached_message and payload.cached_message.flags.loading:
        return None
    if payload.message.author.id == bot_user_id:
        return None

    event = message_event(
        action="on_raw_message_edit",
        message=payload.message,
        before=payload.cached_message,
    )
    if event is None:
        return None
    event.extra = {
        "channel_id": payload.channel_id,
        "cached_message": payload.cached_message is not None,
    }
    event.raw["raw_payload"] = {
        "message_id": payload.message_id,
        "channel_id": payload.channel_id,
        "guild_id": payload.guild_id,
        "data": dict(payload.data),
        "cached_message": payload.cached_message is not None,
    }
    event.raw["cached_message"] = payload.cached_message is not None
    return event


def message_entity(message_id: int, channel_id: int, guild_id: int | None) -> ModlogEntity:
    data: dict[str, Any] = {"channel_id": channel_id}
    if guild_id is not None:
        data["guild_id"] = guild_id
    return ModlogEntity(id=message_id, type="Message", data=data)


def raw_reaction_payload_snapshot(
    payload: (
        discord.RawReactionActionEvent
        | discord.RawReactionClearEvent
        | discord.RawReactionClearEmojiEvent
    ),
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "message_id": payload.message_id,
        "channel_id": payload.channel_id,
        "guild_id": payload.guild_id,
    }
    if isinstance(payload, discord.RawReactionActionEvent):
        data.update({
            "user_id": payload.user_id,
            "message_author_id": payload.message_author_id,
            "event_type": str(payload.event_type),
            "emoji": partial_emoji_snapshot(payload.emoji),
            "burst": payload.burst,
            "burst_colours": [colour.value for colour in payload.burst_colours],
            "type": str(payload.type),
            "member": member_snapshot(payload.member) if payload.member is not None else None,
        })
    elif isinstance(payload, discord.RawReactionClearEmojiEvent):
        data["emoji"] = partial_emoji_snapshot(payload.emoji)
    return data


def raw_reaction_action_event(
    *,
    action: str,
    payload: discord.RawReactionActionEvent,
) -> ModlogEvent | None:
    if payload.guild_id is None:
        return None
    return ModlogEvent(
        id=generated_event_id(),
        guild_id=payload.guild_id,
        source="discord_gateway",
        action=action,
        imported_at=datetime.now(timezone.utc),
        actor=user_entity(payload.member) if payload.member is not None else ModlogEntity(
            id=payload.user_id,
            type="User",
        ),
        target=message_entity(payload.message_id, payload.channel_id, payload.guild_id),
        extra={
            "channel_id": payload.channel_id,
            "message_id": payload.message_id,
            "emoji": str(payload.emoji),
        },
        raw={"raw_payload": raw_reaction_payload_snapshot(payload)},
    )


def raw_reaction_clear_event(payload: discord.RawReactionClearEvent) -> ModlogEvent | None:
    if payload.guild_id is None:
        return None
    return ModlogEvent(
        id=generated_event_id(),
        guild_id=payload.guild_id,
        source="discord_gateway",
        action="on_raw_reaction_clear",
        imported_at=datetime.now(timezone.utc),
        target=message_entity(payload.message_id, payload.channel_id, payload.guild_id),
        extra={
            "channel_id": payload.channel_id,
            "message_id": payload.message_id,
        },
        raw={"raw_payload": raw_reaction_payload_snapshot(payload)},
    )


def raw_reaction_clear_emoji_event(payload: discord.RawReactionClearEmojiEvent) -> ModlogEvent | None:
    if payload.guild_id is None:
        return None
    return ModlogEvent(
        id=generated_event_id(),
        guild_id=payload.guild_id,
        source="discord_gateway",
        action="on_raw_reaction_clear_emoji",
        imported_at=datetime.now(timezone.utc),
        target=message_entity(payload.message_id, payload.channel_id, payload.guild_id),
        extra={
            "channel_id": payload.channel_id,
            "message_id": payload.message_id,
            "emoji": str(payload.emoji),
        },
        raw={"raw_payload": raw_reaction_payload_snapshot(payload)},
    )


def thread_member_join_event(member: discord.ThreadMember) -> ModlogEvent | None:
    thread = member.thread
    if not thread.is_private():
        return None
    return ModlogEvent(
        id=generated_event_id(),
        guild_id=thread.guild.id,
        source="discord_gateway",
        action="on_thread_member_join",
        imported_at=datetime.now(timezone.utc),
        actor=ModlogEntity(id=member.id, type="User"),
        target=thread_entity(thread),
        extra={"thread_id": thread.id},
        raw={"thread_member": thread_member_snapshot(member)},
    )


def raw_thread_member_remove_events(
    payload: discord.RawThreadMembersUpdate,
    thread: discord.Thread | None,
) -> list[ModlogEvent]:
    removed_member_ids = payload.data.get("removed_member_ids", [])
    events: list[ModlogEvent] = []
    if thread is None or not thread.is_private():
        return events

    for member_id in removed_member_ids:
        events.append(ModlogEvent(
            id=generated_event_id(),
            guild_id=payload.guild_id,
            source="discord_gateway",
            action="on_raw_thread_member_remove",
            imported_at=datetime.now(timezone.utc),
            actor=ModlogEntity(id=int(member_id), type="User"),
            target=thread_entity(thread),
            extra={"thread_id": payload.thread_id},
            raw={
                "raw_payload": {
                    "thread_id": payload.thread_id,
                    "guild_id": payload.guild_id,
                    "member_count": payload.member_count,
                    "data": payload.data,
                },
                "member_id": int(member_id),
            },
        ))
    return events


def member_join_event(member: discord.Member | discord.User) -> ModlogEvent | None:
    guild = member.guild if isinstance(member, discord.Member) else None
    if guild is None:
        return None
    return ModlogEvent(
        id=generated_event_id(),
        guild_id=guild.id,
        source="discord_gateway",
        action="on_member_join",
        imported_at=datetime.now(timezone.utc),
        target=user_entity(member),
        raw={"member": member_snapshot(member)},
    )


def raw_member_remove_event(payload: discord.RawMemberRemoveEvent) -> ModlogEvent:
    user = payload.user
    return ModlogEvent(
        id=generated_event_id(),
        guild_id=payload.guild_id,
        source="discord_gateway",
        action="on_raw_member_remove",
        imported_at=datetime.now(timezone.utc),
        target=user_entity(user),
        raw={"member": member_snapshot(user)},
    )


def member_ban_event(guild: discord.Guild, user: discord.User | discord.Member) -> ModlogEvent:
    return ModlogEvent(
        id=generated_event_id(),
        guild_id=guild.id,
        source="discord_gateway",
        action="on_member_ban",
        imported_at=datetime.now(timezone.utc),
        target=user_entity(user),
        raw={"member": member_snapshot(user)},
    )


def member_unban_event(guild: discord.Guild, user: discord.User) -> ModlogEvent:
    return ModlogEvent(
        id=generated_event_id(),
        guild_id=guild.id,
        source="discord_gateway",
        action="on_member_unban",
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
            action="on_member_role_update",
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
            action="on_member_update",
            imported_at=datetime.now(timezone.utc),
            target=user_entity(after),
            changes=changes,
            raw={
                "before_member": member_snapshot(before),
                "after_member": member_snapshot(after),
            },
        ))

    return events
