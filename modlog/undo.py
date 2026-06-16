from dataclasses import dataclass
from datetime import datetime
from typing import Any

import discord

from modlog import ModlogAction, UndoRule, register
from modlog.actions import ACTIONS
from modlog.audit_log import ModlogChange, ModlogEvent


@dataclass
class ModlogUndoResult:
    success: bool
    title: str
    message: str


@dataclass(frozen=True)
class ModlogReverseAction:
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


def _invite_code(event: ModlogEvent, action: ModlogReverseAction) -> str:
    payload = action.payload or {}
    raw_code = payload.get("invite_code")
    if isinstance(raw_code, str) and raw_code:
        return raw_code
    if event.target is not None:
        if event.target.external_id:
            return event.target.external_id
        for key in ("code", "id"):
            value = event.target.data.get(key)
            if isinstance(value, str) and value:
                return value
    raise ModlogUndoError("This event does not have an invite code.")


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
    possible: bool = False,
    reason: str | None = None,
    **payload: Any,
) -> ModlogReverseAction:
    return ModlogReverseAction(
        possible=possible,
        reason=reason,
        payload={key: value for key, value in payload.items() if value is not None},
    )


def _with_state(
    action: ModlogReverseAction,
    *,
    possible: bool,
    reason: str | None = None,
) -> ModlogReverseAction:
    return ModlogReverseAction(
        possible=possible,
        reason=reason if reason is not None else action.reason,
        payload=action.payload,
    )


async def _target_exists_for_delete(
    guild: discord.Guild,
    event: ModlogEvent,
    action: ModlogReverseAction,
) -> tuple[bool, str | None]:
    payload = action.payload or {}
    target_id = payload.get("target_id")
    if not isinstance(target_id, int):
        return False, "the target id was not captured"

    kind = event.action.removesuffix("_create")
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

    return False, "this undo is not implemented yet"


async def _action_with_current_state(
    guild: discord.Guild,
    event: ModlogEvent,
    action: ModlogReverseAction,
    *,
    operation: str,
) -> ModlogReverseAction:
    if event.guild_id != guild.id:
        return _with_state(action, possible=False, reason="that event belongs to a different server")
    if action.reason is not None and operation not in {"member.roles.revert"}:
        return _with_state(action, possible=False)

    payload = action.payload or {}
    if operation == "invite.delete":
        try:
            invite_code = _invite_code(event, action)
            invites = await guild.invites()
            exists = any(invite.code == invite_code for invite in invites)
            return _with_state(
                action,
                possible=exists,
                reason=None if exists else "the created invite no longer exists",
            )
        except discord.Forbidden:
            return _with_state(action, possible=False, reason="Bogobot cannot verify the created invite")
        except discord.HTTPException as exc:
            return _with_state(action, possible=False, reason=str(exc))
        except ModlogUndoError as exc:
            return _with_state(action, possible=False, reason=str(exc))

    target_id = payload.get("target_id", event.target_id)
    if not isinstance(target_id, int):
        return _with_state(action, possible=False, reason="the target id was not captured")

    try:
        if operation == "member.unban":
            await guild.fetch_ban(discord.Object(id=target_id, type=discord.User))
            return _with_state(action, possible=True)
        if operation == "member.ban":
            return _with_state(action, possible=True)
        if operation == "member.roles.revert":
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
        if operation == "member.restore_fields":
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
        if operation == "created_target.delete":
            exists, reason = await _target_exists_for_delete(guild, event, action)
            return _with_state(action, possible=exists, reason=None if exists else reason)
    except discord.NotFound:
        return _with_state(action, possible=False, reason="the target no longer exists")
    except discord.Forbidden:
        return _with_state(action, possible=False, reason="Bogobot cannot verify the current target state")
    except discord.HTTPException as exc:
        return _with_state(action, possible=False, reason=str(exc))

    return _with_state(action, possible=False, reason="this undo is not implemented yet")


async def reverse_actions_for_event(guild: discord.Guild, event: ModlogEvent) -> list[ModlogReverseAction]:
    action = ACTIONS.get(event.action)
    if action is not None and action.undo_rule is not None:
        reverse = await action.undo_rule.criteria_fn(guild, event)
        if reverse is None:
            return []
        if isinstance(reverse, list):
            return reverse
        if isinstance(reverse, ModlogReverseAction):
            return [reverse]
        raise TypeError(f"Undo criteria for {event.action} returned {type(reverse).__name__}.")

    return [_action(reason="no reverse action is defined for this modlog action")]


async def _criteria_member_unban(guild: discord.Guild, event: ModlogEvent) -> ModlogReverseAction:
    return await _action_with_current_state(
        guild,
        event,
        _action(target_id=event.target_id),
        operation="member.unban",
    )


async def _criteria_member_ban(guild: discord.Guild, event: ModlogEvent) -> ModlogReverseAction:
    return await _action_with_current_state(
        guild,
        event,
        _action(target_id=event.target_id),
        operation="member.ban",
    )


async def _criteria_member_roles_revert(guild: discord.Guild, event: ModlogEvent) -> ModlogReverseAction:
    roles = _change_map(event.changes).get("roles")
    added_roles = roles.new if roles is not None and roles.has_new else None
    removed_roles = roles.old if roles is not None and roles.has_old else None
    return await _action_with_current_state(
        guild,
        event,
        _action(
            target_id=event.target_id,
            add_roles=removed_roles,
            remove_roles=added_roles,
        ),
        operation="member.roles.revert",
    )


async def _criteria_member_restore_fields(guild: discord.Guild, event: ModlogEvent) -> ModlogReverseAction:
    old_values = {
        change.key: change.old
        for change in event.changes
        if change.has_old
    }
    return await _action_with_current_state(
        guild,
        event,
        _action(target_id=event.target_id, old_values=old_values or None),
        operation="member.restore_fields",
    )


async def _criteria_delete_created_target(guild: discord.Guild, event: ModlogEvent) -> ModlogReverseAction:
    return await _action_with_current_state(
        guild,
        event,
        _action(target_id=event.target_id),
        operation="created_target.delete",
    )


async def _criteria_delete_invite(guild: discord.Guild, event: ModlogEvent) -> ModlogReverseAction:
    try:
        return await _action_with_current_state(
            guild,
            event,
            _action(invite_code=_invite_code(event, _action())),
            operation="invite.delete",
        )
    except ModlogUndoError as exc:
        return _action(reason=str(exc))


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
    kind = event.action.removesuffix("_create")
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

    raise ModlogUndoError("This undo is not implemented yet.")


async def _delete_invite(
    guild: discord.Guild,
    event: ModlogEvent,
    action: ModlogReverseAction,
) -> ModlogUndoResult:
    invite_code = _invite_code(event, action)
    invite = next((invite for invite in await guild.invites() if invite.code == invite_code), None)
    if invite is None:
        raise ModlogUndoError("The created invite no longer exists.")
    await invite.delete(reason=f"Undo modlog event {event.id}")
    return ModlogUndoResult(True, "Undo Complete", f"Deleted invite `{invite_code}`.")


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
        action_definition = ACTIONS.get(event.action)
        if action_definition is not None and action_definition.undo_rule is not None:
            result = await action_definition.undo_rule.exec_fn(guild, event, action)
            if isinstance(result, ModlogUndoResult):
                return result
            return ModlogUndoResult(False, "Undo Failed", "The undo handler returned an invalid result.")
    except discord.Forbidden:
        return ModlogUndoResult(False, "Undo Failed", "Bogobot does not have permission to perform this undo.")
    except discord.NotFound:
        return ModlogUndoResult(False, "Undo Failed", "The target no longer exists.")
    except discord.HTTPException as exc:
        return ModlogUndoResult(False, "Undo Failed", str(exc))
    except ModlogUndoError as exc:
        return ModlogUndoResult(False, "Undo Failed", str(exc))

    return ModlogUndoResult(False, "Undo Not Implemented", "This undo is not implemented yet.")


def _register_default_undo_actions() -> None:
    register(ModlogAction(
        name="ban",
        name_text="Member banned",
        desc_text="A member was banned from the server.",
        undo_rule=UndoRule(
            _criteria_member_unban,
            _undo_member_unban,
            description="Unban the member.",
        ),
    ))
    register(ModlogAction(
        name="unban",
        name_text="Member unbanned",
        desc_text="A member was unbanned from the server.",
        undo_rule=UndoRule(
            _criteria_member_ban,
            _undo_member_ban,
            description="Ban the member again.",
        ),
    ))
    register(ModlogAction(
        name="member_role_update",
        name_text="Member roles changed",
        desc_text="A member's role set changed.",
        undo_rule=UndoRule(
            _criteria_member_roles_revert,
            _undo_member_roles_revert,
            description="Revert the captured role delta.",
        ),
    ))
    register(ModlogAction(
        name="member_update",
        name_text="Member updated",
        desc_text="A member's server profile or moderation state changed.",
        undo_rule=UndoRule(
            _criteria_member_restore_fields,
            _undo_member_restore_fields,
            description="Restore captured member fields.",
        ),
    ))
    for action_name, name_text in (
        ("automod_rule_create", "Automod rule created"),
        ("channel_create", "Channel created"),
        ("emoji_create", "Emoji created"),
        ("integration_create", "Integration created"),
        ("role_create", "Role created"),
        ("scheduled_event_create", "Scheduled event created"),
        ("soundboard_sound_create", "Soundboard sound created"),
        ("sticker_create", "Sticker created"),
        ("thread_create", "Thread created"),
    ):
        existing = ACTIONS.get(action_name)
        register(ModlogAction(
            name=action_name,
            name_text=existing.name_text if existing is not None and existing.name_text is not None else name_text,
            desc_text=existing.desc_text if existing is not None else None,
            related_rule=existing.related_rule if existing is not None else None,
            undo_rule=UndoRule(
                _criteria_delete_created_target,
                _delete_created_target,
                description="Delete the created object.",
            ),
        ))
    register(ModlogAction(
        name="invite_create",
        name_text="Invite created",
        desc_text="An invite was created.",
        undo_rule=UndoRule(
            _criteria_delete_invite,
            _delete_invite,
            description="Delete the created invite.",
        ),
    ))


_register_default_undo_actions()
