from dataclasses import dataclass
from datetime import datetime
from typing import Any

import discord

from modlog.audit_log import ModlogEvent, ModlogReverseAction


@dataclass
class ModlogUndoResult:
    success: bool
    title: str
    message: str


class ModlogUndoError(Exception):
    pass


def _target_id(event: ModlogEvent, action: ModlogReverseAction) -> int:
    raw_target_id = action.payload.get("target_id", event.target_id)
    if not isinstance(raw_target_id, int):
        raise ModlogUndoError("This event does not have a numeric target id.")
    return raw_target_id


def _role_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []

    role_ids: list[int] = []
    for item in value:
        if isinstance(item, int):
            role_ids.append(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), int):
            role_ids.append(item["id"])
    return role_ids


def _datetime_or_none(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


async def _fetch_member(guild: discord.Guild, user_id: int) -> discord.Member:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    return await guild.fetch_member(user_id)


async def _undo_member_unban(
    guild: discord.Guild,
    event: ModlogEvent,
    action: ModlogReverseAction,
) -> ModlogUndoResult:
    target_id = _target_id(event, action)
    await guild.unban(
        discord.Object(id=target_id, type=discord.User),
        reason=f"Undo modlog event {event.id}",
    )
    return ModlogUndoResult(True, "Undo Complete", f"Unbanned `{target_id}`.")


async def _undo_member_ban(
    guild: discord.Guild,
    event: ModlogEvent,
    action: ModlogReverseAction,
) -> ModlogUndoResult:
    target_id = _target_id(event, action)
    await guild.ban(
        discord.Object(id=target_id, type=discord.User),
        reason=f"Undo modlog event {event.id}",
    )
    return ModlogUndoResult(True, "Undo Complete", f"Banned `{target_id}`.")


async def _undo_member_roles_revert(
    guild: discord.Guild,
    event: ModlogEvent,
    action: ModlogReverseAction,
) -> ModlogUndoResult:
    target_id = _target_id(event, action)
    add_role_ids = _role_ids(action.payload.get("add_roles"))
    remove_role_ids = _role_ids(action.payload.get("remove_roles"))
    if not add_role_ids and not remove_role_ids:
        raise ModlogUndoError("The role changes were not captured.")

    member = await _fetch_member(guild, target_id)
    add_roles = [
        role
        for role_id in add_role_ids
        if (role := guild.get_role(role_id)) is not None
    ]
    remove_roles = [
        role
        for role_id in remove_role_ids
        if (role := guild.get_role(role_id)) is not None
    ]
    missing = len(add_role_ids) + len(remove_role_ids) - len(add_roles) - len(remove_roles)
    if missing:
        raise ModlogUndoError(f"{missing} changed role(s) no longer exist.")

    reason = f"Undo modlog event {event.id}"
    if remove_roles:
        await member.remove_roles(*remove_roles, reason=reason)
    if add_roles:
        await member.add_roles(*add_roles, reason=reason)
    return ModlogUndoResult(
        True,
        "Undo Complete",
        f"Reverted role delta for {member.mention}: "
        f"added `{len(add_roles)}`, removed `{len(remove_roles)}`.",
    )


async def _undo_member_restore_fields(
    guild: discord.Guild,
    event: ModlogEvent,
    action: ModlogReverseAction,
) -> ModlogUndoResult:
    target_id = _target_id(event, action)
    old_values = action.payload.get("old_values")
    if not isinstance(old_values, dict):
        old_values = {
            change.key: change.old
            for change in event.changes
            if change.has_old
        }

    editable: dict[str, Any] = {}
    for source_key, edit_key in (("nick", "nick"), ("mute", "mute"), ("deaf", "deaf")):
        if source_key in old_values:
            editable[edit_key] = old_values[source_key]
    for source_key in ("timed_out_until", "communication_disabled_until"):
        if source_key in old_values:
            editable["timed_out_until"] = _datetime_or_none(old_values[source_key])

    if not editable:
        raise ModlogUndoError("No safely restorable member fields were captured.")

    member = await _fetch_member(guild, target_id)
    await member.edit(
        **editable,
        reason=f"Undo modlog event {event.id}",
    )
    return ModlogUndoResult(
        True,
        "Undo Complete",
        f"Restored `{', '.join(sorted(editable))}` for {member.mention}.",
    )


async def _delete_created_target(
    guild: discord.Guild,
    event: ModlogEvent,
    action: ModlogReverseAction,
) -> ModlogUndoResult:
    target_id = _target_id(event, action)
    kind = action.kind.removesuffix(".delete")
    reason = f"Undo modlog event {event.id}"

    if kind in {"channel", "thread"}:
        channel = guild.get_channel_or_thread(target_id)
        if channel is None:
            raise ModlogUndoError("The created channel/thread no longer exists.")
        await channel.delete(reason=reason)
        return ModlogUndoResult(True, "Undo Complete", f"Deleted `{kind}` `{target_id}`.")

    if kind == "role":
        role = guild.get_role(target_id)
        if role is None:
            raise ModlogUndoError("The created role no longer exists.")
        await role.delete(reason=reason)
        return ModlogUndoResult(True, "Undo Complete", f"Deleted role `{target_id}`.")

    if kind == "emoji":
        emoji = guild.get_emoji(target_id)
        if emoji is None:
            emoji = await guild.fetch_emoji(target_id)
        await emoji.delete(reason=reason)
        return ModlogUndoResult(True, "Undo Complete", f"Deleted emoji `{target_id}`.")

    if kind == "sticker":
        sticker = await guild.fetch_sticker(target_id)
        await sticker.delete(reason=reason)
        return ModlogUndoResult(True, "Undo Complete", f"Deleted sticker `{target_id}`.")

    if kind == "scheduled_event":
        scheduled_event = guild.get_scheduled_event(target_id)
        if scheduled_event is None:
            scheduled_event = await guild.fetch_scheduled_event(target_id)
        await scheduled_event.delete(reason=reason)
        return ModlogUndoResult(True, "Undo Complete", f"Deleted scheduled event `{target_id}`.")

    if kind == "automod_rule":
        rule = await guild.fetch_automod_rule(target_id)
        await rule.delete(reason=reason)
        return ModlogUndoResult(True, "Undo Complete", f"Deleted automod rule `{target_id}`.")

    if kind == "soundboard_sound":
        sound = guild.get_soundboard_sound(target_id)
        if sound is None:
            sound = await guild.fetch_soundboard_sound(target_id)
        await sound.delete(reason=reason)
        return ModlogUndoResult(True, "Undo Complete", f"Deleted soundboard sound `{target_id}`.")

    raise ModlogUndoError(f"`{action.kind}` is not implemented yet.")


async def undo_event(
    guild: discord.Guild,
    event: ModlogEvent,
) -> ModlogUndoResult:
    if event.guild_id != guild.id:
        return ModlogUndoResult(False, "Undo Failed", "That event belongs to a different server.")

    action = next((reverse for reverse in event.reverse_actions if reverse.possible), None)
    if action is None:
        reason = event.reverse_actions[0].reason if event.reverse_actions else None
        return ModlogUndoResult(
            False,
            "Undo Not Available",
            reason or "This event does not have a reversible action.",
        )

    try:
        if action.kind == "member.unban":
            return await _undo_member_unban(guild, event, action)
        if action.kind == "member.ban":
            return await _undo_member_ban(guild, event, action)
        if action.kind == "member.roles.revert":
            return await _undo_member_roles_revert(guild, event, action)
        if action.kind == "member.restore_fields":
            return await _undo_member_restore_fields(guild, event, action)
        if action.kind.endswith(".delete"):
            return await _delete_created_target(guild, event, action)
    except discord.Forbidden:
        return ModlogUndoResult(False, "Undo Failed", "Bogobot does not have permission to perform this undo.")
    except discord.NotFound:
        return ModlogUndoResult(False, "Undo Failed", "The target no longer exists.")
    except discord.HTTPException as exc:
        return ModlogUndoResult(False, "Undo Failed", str(exc))
    except ModlogUndoError as exc:
        return ModlogUndoResult(False, "Undo Failed", str(exc))

    return ModlogUndoResult(False, "Undo Not Implemented", f"`{action.kind}` is not implemented yet.")
