"""Moderation log support package."""

from modlog.actions import ACTIONS, ModlogAction, RelatedRule, UndoRule, register
from modlog.related import (
    DEFAULT_WINDOW_SECONDS,
    MESSAGE_BULK_WINDOW_SECONDS,
    audit_log_related_limit,
    cross_source_channel_only,
    cross_source_same_target,
    cross_source_target_and_channel,
    different_action_same_actor,
)
from modlog.undo import (
    _criteria_delete_created_target,
    _criteria_delete_invite,
    _criteria_member_ban,
    _criteria_member_restore_fields,
    _criteria_member_roles_revert,
    _criteria_member_unban,
    _criteria_verification_create,
    _delete_created_target,
    _delete_invite,
    _undo_member_ban,
    _undo_member_restore_fields,
    _undo_member_roles_revert,
    _undo_member_unban,
    _undo_verification_create,
)
from modlog.writer import database_path_from_bot, message_entity, modlog_writer, role_entity


def _register_default_actions() -> None:
    for action_name, candidate_actions, window_seconds, matches in (
        (
            "message_delete",
            frozenset({"on_message_delete"}),
            DEFAULT_WINDOW_SECONDS,
            cross_source_target_and_channel,
        ),
        (
            "message_bulk_delete",
            frozenset({"on_bulk_message_delete"}),
            MESSAGE_BULK_WINDOW_SECONDS,
            cross_source_channel_only,
        ),
        (
            "message_update",
            frozenset({"on_raw_message_edit"}),
            DEFAULT_WINDOW_SECONDS,
            cross_source_target_and_channel,
        ),
        (
            "member_role_update",
            frozenset({"on_member_role_update"}),
            DEFAULT_WINDOW_SECONDS,
            cross_source_same_target,
        ),
        (
            "member_update",
            frozenset({"on_member_update"}),
            DEFAULT_WINDOW_SECONDS,
            cross_source_same_target,
        ),
        (
            "kick",
            frozenset({"on_raw_member_remove"}),
            DEFAULT_WINDOW_SECONDS,
            cross_source_same_target,
        ),
        (
            "ban",
            frozenset({"on_raw_member_remove", "on_member_ban"}),
            DEFAULT_WINDOW_SECONDS,
            cross_source_same_target,
        ),
        (
            "unban",
            frozenset({"on_member_unban"}),
            DEFAULT_WINDOW_SECONDS,
            cross_source_same_target,
        ),
    ):
        register(ModlogAction(
            action_name,
            related=(
                RelatedRule(
                    candidate_actions=candidate_actions,
                    window_seconds=window_seconds,
                    matches=matches,
                    max_related=audit_log_related_limit,
                ),
            ),
        ))

    register(ModlogAction(
        "integration_create",
        "An integration was added to the server.",
        related=(
            RelatedRule(
                candidate_actions=frozenset({"bot_add"}),
                window_seconds=DEFAULT_WINDOW_SECONDS,
                matches=different_action_same_actor,
                max_related=audit_log_related_limit,
            ),
        ),
    ))

    register(ModlogAction(
        "ban",
        "A member was banned from the server.",
        undo=UndoRule(
            _criteria_member_unban,
            _undo_member_unban,
            description="Unban the member.",
        ),
    ))
    register(ModlogAction(
        "unban",
        "A member was unbanned from the server.",
        undo=UndoRule(
            _criteria_member_ban,
            _undo_member_ban,
            description="Ban the member again.",
        ),
    ))
    register(ModlogAction(
        "member_role_update",
        "A member's role set changed.",
        undo=UndoRule(
            _criteria_member_roles_revert,
            _undo_member_roles_revert,
            description="Revert the captured role delta.",
        ),
    ))
    register(ModlogAction(
        "member_update",
        "A member's server profile or moderation state changed.",
        undo=UndoRule(
            _criteria_member_restore_fields,
            _undo_member_restore_fields,
            description="Restore captured member fields.",
        ),
    ))
    for action_name, description in (
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
        register(ModlogAction(
            action_name,
            description,
            undo=UndoRule(
                _criteria_delete_created_target,
                _delete_created_target,
                description="Delete the created object.",
            ),
        ))
    register(ModlogAction(
        "invite_create",
        "An invite was created.",
        undo=UndoRule(
            _criteria_delete_invite,
            _delete_invite,
            description="Delete the created invite.",
        ),
    ))
    register(ModlogAction(
        "verification",
        "Verification panel or roles were changed.",
        undo=UndoRule(
            _criteria_verification_create,
            _undo_verification_create,
            description="Delete the created verification message and restore previous role config.",
        ),
    ))


_register_default_actions()


__all__ = (
    "ACTIONS",
    "ModlogAction",
    "RelatedRule",
    "UndoRule",
    "register",
    "database_path_from_bot",
    "message_entity",
    "modlog_writer",
    "role_entity",
)
