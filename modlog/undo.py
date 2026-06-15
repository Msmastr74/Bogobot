from dataclasses import dataclass
from datetime import datetime
from typing import Any

import discord

from modlog.audit_log import ModlogChange, ModlogEvent


@dataclass
class ModlogUndoResult:
    success: bool
    title: str
    message: str


@dataclass(frozen=True)
class ModlogReverseAction:
    kind: str
    possible: bool
    reason: str | None = None
    payload: dict[str, Any] | None = None


class ModlogUndoError(Exception):
    pass


def _target_id(event: ModlogEvent, action: ModlogReverseAction) -> int:
    payload = action.payload or {}
    raw_target_id = payload.get("target_id", event.target_id)
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


def _change_map(changes: list[ModlogChange]) -> dict[str, ModlogChange]:
    return {change.key: change for change in changes}


def _action(
    kind: str,
    *,
    possible: bool = False,
    reason: str | None = None,
    **payload: Any,
) -> ModlogReverseAction:
    return ModlogReverseAction(
        kind=kind,
        possible=possible,
        reason=reason,
        payload={key: value for key, value in payload.items() if value is not None},
    )


def _candidate_reverse_actions(event: ModlogEvent) -> list[ModlogReverseAction]:
    target_id = event.target_id
    by_key = _change_map(event.changes)
    supported_create_deletes = {
        "automod_rule",
        "channel",
        "emoji",
        "integration",
        "role",
        "scheduled_event",
        "soundboard_sound",
        "sticker",
        "thread",
    }

    if event.action == "ban":
        return [_action("member.unban", target_id=target_id)]
    if event.action == "unban":
        return [_action("member.ban", target_id=target_id)]
    if event.action == "kick":
        return [_action("member.invite_back", reason="kicks cannot be undone directly", target_id=target_id)]
    if event.action == "member_role_update":
        roles = by_key.get("roles")
        added_roles = roles.new if roles is not None and roles.has_new else None
        removed_roles = roles.old if roles is not None and roles.has_old else None
        return [_action(
            "member.roles.revert",
            reason="role changes unavailable" if added_roles is None and removed_roles is None else None,
            target_id=target_id,
            add_roles=removed_roles,
            remove_roles=added_roles,
        )]
    if event.action == "member_update":
        old_values = {
            change.key: change.old
            for change in event.changes
            if change.has_old
        }
        return [_action("member.restore_fields", target_id=target_id, old_values=old_values or None)]
    if event.action.endswith("_create"):
        kind = event.action.removesuffix("_create")
        return [_action(
            f"{kind}.delete",
            reason=(
                "created object undo is not implemented for this audit log action"
                if kind not in supported_create_deletes else
                None
            ),
            target_id=target_id,
        )]
    if event.action.endswith("_delete"):
        return [_action(
            f"{event.action.removesuffix('_delete')}.recreate",
            reason="recreating deleted objects requires richer snapshots",
            target_id=target_id,
        )]
    if event.action.endswith("_update"):
        old_values = {
            change.key: change.old
            for change in event.changes
            if change.has_old
        }
        return [_action(
            f"{event.action.removesuffix('_update')}.restore",
            reason="generic update restore is not implemented for this audit log action",
            target_id=target_id,
            old_values=old_values or None,
        )]
    if event.action in {"message_delete", "message_bulk_delete"}:
        return [_action(
            "message.restore_copy",
            reason="audit logs do not include deleted message content",
            target_id=target_id,
        )]
    return [_action("none", reason="no reverse action is defined for this audit log action")]


def _with_state(
    action: ModlogReverseAction,
    *,
    possible: bool,
    reason: str | None = None,
) -> ModlogReverseAction:
    return ModlogReverseAction(
        kind=action.kind,
        possible=possible,
        reason=reason if reason is not None else action.reason,
        payload=action.payload,
    )


async def _target_exists_for_delete(guild: discord.Guild, action: ModlogReverseAction) -> tuple[bool, str | None]:
    payload = action.payload or {}
    target_id = payload.get("target_id")
    if not isinstance(target_id, int):
        return False, "the target id was not captured"

    kind = action.kind.removesuffix(".delete")
    try:
        if kind in {"channel", "thread"}:
            return guild.get_channel_or_thread(target_id) is not None, "the created channel/thread no longer exists"
        if kind == "role":
            return guild.get_role(target_id) is not None, "the created role no longer exists"
        if kind == "emoji":
            if guild.get_emoji(target_id) is not None:
                return True, None
            await guild.fetch_emoji(target_id)
            return True, None
        if kind == "sticker":
            await guild.fetch_sticker(target_id)
            return True, None
        if kind == "scheduled_event":
            if guild.get_scheduled_event(target_id) is not None:
                return True, None
            await guild.fetch_scheduled_event(target_id)
            return True, None
        if kind == "automod_rule":
            await guild.fetch_automod_rule(target_id)
            return True, None
        if kind == "soundboard_sound":
            if guild.get_soundboard_sound(target_id) is not None:
                return True, None
            await guild.fetch_soundboard_sound(target_id)
            return True, None
        if kind == "integration":
            integrations = await guild.integrations()
            return any(integration.id == target_id for integration in integrations), "the created integration no longer exists"
    except discord.NotFound:
        return False, f"the created {kind} no longer exists"
    except discord.Forbidden:
        return False, f"Bogobot cannot verify the created {kind}"
    except discord.HTTPException as exc:
        return False, str(exc)

    return False, f"`{action.kind}` is not implemented yet"


async def _action_with_current_state(guild: discord.Guild, event: ModlogEvent, action: ModlogReverseAction) -> ModlogReverseAction:
    if event.guild_id != guild.id:
        return _with_state(action, possible=False, reason="that event belongs to a different server")
    if action.reason is not None and action.kind not in {"member.roles.revert"}:
        return _with_state(action, possible=False)
    if action.kind == "none":
        return _with_state(action, possible=False)

    payload = action.payload or {}
    target_id = payload.get("target_id", event.target_id)
    if not isinstance(target_id, int):
        return _with_state(action, possible=False, reason="the target id was not captured")

    try:
        if action.kind == "member.unban":
            await guild.fetch_ban(discord.Object(id=target_id, type=discord.User))
            return _with_state(action, possible=True)
        if action.kind == "member.ban":
            return _with_state(action, possible=True)
        if action.kind == "member.roles.revert":
            add_role_ids = _role_ids(payload.get("add_roles"))
            remove_role_ids = _role_ids(payload.get("remove_roles"))
            if not add_role_ids and not remove_role_ids:
                return _with_state(action, possible=False, reason="the role changes were not captured")
            await _fetch_member(guild, target_id)
            missing = [
                role_id
                for role_id in (*add_role_ids, *remove_role_ids)
                if guild.get_role(role_id) is None
            ]
            if missing:
                return _with_state(action, possible=False, reason=f"{len(missing)} changed role(s) no longer exist")
            return _with_state(action, possible=True)
        if action.kind == "member.restore_fields":
            await _fetch_member(guild, target_id)
            old_values = payload.get("old_values")
            if not isinstance(old_values, dict):
                old_values = {
                    change.key: change.old
                    for change in event.changes
                    if change.has_old
                }
            editable = {"nick", "mute", "deaf", "timed_out_until", "communication_disabled_until"} & set(old_values)
            if not editable:
                return _with_state(action, possible=False, reason="no safely restorable member fields were captured")
            return _with_state(action, possible=True)
        if action.kind.endswith(".delete"):
            exists, reason = await _target_exists_for_delete(guild, action)
            return _with_state(action, possible=exists, reason=None if exists else reason)
    except discord.NotFound:
        return _with_state(action, possible=False, reason="the target no longer exists")
    except discord.Forbidden:
        return _with_state(action, possible=False, reason="Bogobot cannot verify the current target state")
    except discord.HTTPException as exc:
        return _with_state(action, possible=False, reason=str(exc))

    return _with_state(action, possible=False, reason=f"`{action.kind}` is not implemented yet")


async def reverse_actions_for_event(guild: discord.Guild, event: ModlogEvent) -> list[ModlogReverseAction]:
    return [
        await _action_with_current_state(guild, event, action)
        for action in _candidate_reverse_actions(event)
    ]


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
    payload = action.payload or {}
    add_role_ids = _role_ids(payload.get("add_roles"))
    remove_role_ids = _role_ids(payload.get("remove_roles"))
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
    payload = action.payload or {}
    old_values = payload.get("old_values")
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

    if kind == "integration":
        integration = next(
            (integration for integration in await guild.integrations() if integration.id == target_id),
            None,
        )
        if integration is None:
            raise ModlogUndoError("The created integration no longer exists.")
        await integration.delete(reason=reason)
        return ModlogUndoResult(True, "Undo Complete", f"Deleted integration `{target_id}`.")

    raise ModlogUndoError(f"`{action.kind}` is not implemented yet.")


async def undo_event(
    guild: discord.Guild,
    event: ModlogEvent,
) -> ModlogUndoResult:
    if event.guild_id != guild.id:
        return ModlogUndoResult(False, "Undo Failed", "That event belongs to a different server.")

    reverse_actions = await reverse_actions_for_event(guild, event)
    action = next((reverse for reverse in reverse_actions if reverse.possible), None)
    if action is None:
        reason = reverse_actions[0].reason if reverse_actions else None
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
