from datetime import timedelta
import json
from typing import Any, Iterable

import discord
from discord import app_commands

from bogobot_core import BotCore
from modlog import ModlogAction, database_path_from_bot, modlog_writer
from modlog.actions import ACTIONS
from modlog.audit_log import ModlogEvent, known_actions, normalize_entry, retrieve_and_scan
from modlog.database import ModlogDatabase
from modlog.lifecycle import (
    member_ban_event,
    member_join_event,
    member_unban_event,
    member_update_events,
    raw_bulk_message_delete_events,
    raw_member_remove_event,
    raw_message_delete_event,
    raw_message_edit_event,
    raw_reaction_clear_emoji_event,
    raw_reaction_clear_event,
    raw_thread_member_remove_events,
    thread_member_join_event,
)
from modlog.related import RelatedGroup, RelatedResolver
from modlog.undo import ModlogReverseAction, ModlogUndoResult, reverse_actions_for_event, undo_event
from utils.discord import chunk_text, count_characters


MAX_EVENT_LINES = 10
MAX_ACTION_CHOICES = 25
MAX_EVENTS_PER_PAGE = 10
MODLOG_UNDO_CAPABILITY = "modlog.undo"
MODLOG_VIEW_SENSITIVE_CAPABILITY = "modlog.view_sensitive"
AUDIT_LOG_RESCAN_OVERLAP = timedelta(minutes=10)
DETAIL_VALUE_LIMIT = 1000
MESSAGE_CONTENT_PREVIEW_LIMIT = 2800
write_modlog_undo = modlog_writer(ModlogAction(
    "modlog.undo",
    "A modlog event undo was requested.",
))
MESSAGE_CONTENT_INLINE_LIMIT = 2000
MESSAGE_RECREATE_CONTENT_LIMIT = 2000
RAW_CONTENT_CHUNK_LIMIT = 1900
RELATED_EVENT_DETAIL_LIMIT = 1800
MODLOG_PAGE_FETCH_LIMIT = 100
MODLOG_PAGE_CHAR_LIMIT = 3600
MODLOG_PAGE_ELEMENT_LIMIT = 32
MODLOG_GROUP_CHAR_LIMIT = 1800
GATEWAY_ACTIONS = (
    "on_message_delete",
    "on_bulk_message_delete",
    "on_raw_message_edit",
    "on_raw_reaction_clear",
    "on_raw_reaction_clear_emoji",
    "on_thread_member_join",
    "on_raw_thread_member_remove",
    "on_member_join",
    "on_raw_member_remove",
    "on_member_update",
    "on_member_role_update",
    "on_member_ban",
    "on_member_unban",
)


def audit_action_from_name(name: str | None) -> discord.AuditLogAction | None:
    if name is None or not name.strip():
        return None
    normalized = name.strip()
    for action in known_actions():
        if action.name == normalized:
            return action
    return None


def is_known_action_name(name: str) -> bool:
    return name in GATEWAY_ACTIONS or audit_action_from_name(name) is not None


def action_names() -> tuple[str, ...]:
    names = (
        *GATEWAY_ACTIONS,
        *(action.name for action in known_actions()),
        *(action.name for action in ACTIONS.values()),
    )
    return tuple(dict.fromkeys(names))


async def can_scan_audit_logs(bot: BotCore, guild: discord.Guild) -> bool:
    bot_member = guild.me
    if bot_member is None and bot.user is not None:
        bot_member = guild.get_member(bot.user.id)
    if bot_member is None and bot.user is not None:
        try:
            bot_member = await guild.fetch_member(bot.user.id)
        except discord.HTTPException:
            bot_member = None
    return bot_member is not None and bot_member.guild_permissions.view_audit_log


def entity_text(entity) -> str:
    if entity is None:
        return "Unknown"
    if entity.id is None:
        return entity.type
    if entity.type in {"Member", "User", "ClientUser"}:
        return f"<@{entity.id}> (`{entity.id}`)"
    if entity.type == "Role":
        return f"<@&{entity.id}> (`{entity.id}`)"
    if entity.type in {
        "CategoryChannel",
        "ForumChannel",
        "StageChannel",
        "TextChannel",
        "Thread",
        "VoiceChannel",
    }:
        return f"<#{entity.id}> (`{entity.id}`)"
    return f"{entity.type} `{entity.id}`"


def format_event_line(event: ModlogEvent) -> str:
    return (
        f"`{event.id}` <t:{int(event.created_at.timestamp())}:R> "
        f"`{event.action}` {entity_text(event.actor)} -> {entity_text(event.target)}"
    )


def _short_value(value: object, *, limit: int = DETAIL_VALUE_LIMIT) -> str:
    text = str(value)
    if len(text) <= limit:
        return discord.utils.escape_markdown(text)
    return discord.utils.escape_markdown(text[:limit]) + "... truncated"


def _truncate_display_text(value: str, *, limit: int) -> str:
    if count_characters(value) <= limit:
        return value
    suffix = "\n... truncated"
    budget = max(0, limit - count_characters(suffix))
    current: list[str] = []
    current_length = 0
    for character in value:
        character_length = count_characters(character)
        if current_length + character_length > budget:
            break
        current.append(character)
        current_length += character_length
    return "".join(current) + suffix


def _component_text(component: object) -> list[str]:
    if not isinstance(component, dict):
        return []
    text: list[str] = []
    content = component.get("content")
    if isinstance(content, str) and content:
        text.append(content)
    label = component.get("label")
    if isinstance(label, str) and label:
        text.append(label)
    placeholder = component.get("placeholder")
    if isinstance(placeholder, str) and placeholder:
        text.append(placeholder)
    for child in component.get("components", []):
        text.extend(_component_text(child))
    for item in component.get("items", []):
        text.extend(_component_text(item))
    return text


def _message_payload(event: ModlogEvent, key: str = "message") -> dict[str, object] | None:
    message = event.raw.get(key)
    return message if isinstance(message, dict) else None


def _message_text_parts(message: dict[str, object]) -> list[str]:
    parts: list[str] = []
    for key, title in (("content", "Content"), ("clean_content", "Clean Content")):
        value = message.get(key)
        if isinstance(value, str) and value:
            parts.append(f"### {title}\n{value}")

    embeds = message.get("embeds")
    if isinstance(embeds, list):
        for index, embed in enumerate(embeds, start=1):
            if not isinstance(embed, dict):
                continue
            embed_parts: list[str] = []
            for key in ("title", "description", "url"):
                value = embed.get(key)
                if isinstance(value, str) and value:
                    embed_parts.append(f"{key}: {value}")
            fields = embed.get("fields")
            if isinstance(fields, list):
                for field in fields:
                    if not isinstance(field, dict):
                        continue
                    name = field.get("name")
                    value = field.get("value")
                    if isinstance(name, str) and isinstance(value, str):
                        embed_parts.append(f"{name}: {value}")
            if embed_parts:
                parts.append(f"### Embed {index}\n" + "\n".join(embed_parts))

    components = message.get("components")
    if isinstance(components, list):
        component_text = [
            text
            for component in components
            for text in _component_text(component)
        ]
        if component_text:
            parts.append("### Components\n" + "\n".join(component_text))
    return parts


def _message_plain_text_parts(message: dict[str, object]) -> list[str]:
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        parts.append(content)

    embeds = message.get("embeds")
    if isinstance(embeds, list):
        for embed in embeds:
            if not isinstance(embed, dict):
                continue
            for key in ("title", "description", "url"):
                value = embed.get(key)
                if isinstance(value, str) and value:
                    parts.append(value)
            fields = embed.get("fields")
            if isinstance(fields, list):
                for field in fields:
                    if not isinstance(field, dict):
                        continue
                    name = field.get("name")
                    value = field.get("value")
                    if isinstance(name, str) and name:
                        parts.append(name)
                    if isinstance(value, str) and value:
                        parts.append(value)

    components = message.get("components")
    if isinstance(components, list):
        for component in components:
            parts.extend(_component_text(component))
    return parts


def _message_plain_text(message: dict[str, object]) -> str:
    return "\n".join(_message_plain_text_parts(message))


def _format_message_content_preview(message: dict[str, object]) -> str:
    text = _message_plain_text(message)
    if not text:
        return "Content: [none]"
    return f"Content: {_short_value(_truncate_display_text(text, limit=MESSAGE_CONTENT_INLINE_LIMIT))}"


def _recreate_message_embeds(message: dict[str, object]) -> list[discord.Embed]:
    embeds = message.get("embeds")
    if not isinstance(embeds, list):
        return []

    rebuilt: list[discord.Embed] = []
    for embed in embeds[:10]:
        if not isinstance(embed, dict):
            continue
        embed_type = embed.get("type")
        if embed_type not in (None, "rich"):
            continue
        try:
            rebuilt.append(discord.Embed.from_dict(embed))
        except Exception:
            continue
    return rebuilt


def _component_type(component: dict[str, Any]) -> int | None:
    raw_type = component.get("type")
    if isinstance(raw_type, int):
        return raw_type
    return getattr(raw_type, "value", None)


def _component_media_url(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    media = value.get("media")
    if isinstance(media, dict):
        url = media.get("url")
        return url if isinstance(url, str) else None
    url = value.get("url")
    return url if isinstance(url, str) else None


def _recreate_button(component: dict[str, Any]) -> discord.ui.Button:
    style = discord.ButtonStyle(component.get("style", discord.ButtonStyle.secondary.value))
    return discord.ui.Button(
        style=style,
        label=component.get("label") if isinstance(component.get("label"), str) else None,
        emoji=component.get("emoji") if isinstance(component.get("emoji"), str) else None,
        url=component.get("url") if style is discord.ButtonStyle.link and isinstance(component.get("url"), str) else None,
        custom_id=(
            component.get("custom_id")
            if style is not discord.ButtonStyle.link and isinstance(component.get("custom_id"), str) else
            None
        ),
        disabled=True,
    )


def _select_bounds(component: dict[str, Any]) -> tuple[str, str | None, int, int]:
    custom_id = component.get("custom_id")
    placeholder = component.get("placeholder")
    min_values = component.get("min_values")
    max_values = component.get("max_values")
    return (
        custom_id if isinstance(custom_id, str) else "modlog_disabled_select",
        placeholder if isinstance(placeholder, str) else None,
        min_values if isinstance(min_values, int) else 1,
        max_values if isinstance(max_values, int) else 1,
    )


def _recreate_select_option(option: dict[str, Any]) -> discord.SelectOption | None:
    label = option.get("label")
    if not isinstance(label, str) or not label:
        return None
    value = option.get("value")
    description = option.get("description")
    emoji = option.get("emoji")
    try:
        return discord.SelectOption(
            label=label,
            value=value if isinstance(value, str) else label,
            description=description if isinstance(description, str) else None,
            emoji=emoji if isinstance(emoji, str) else None,
            default=option.get("default", False) is True,
        )
    except Exception:
        return None


def _recreate_select(component: dict[str, Any]) -> discord.ui.Select:
    custom_id, placeholder, min_values, max_values = _select_bounds(component)
    options = component.get("options")
    select_options: list[discord.SelectOption] = []
    if isinstance(options, list):
        for option in options[:25]:
            if isinstance(option, dict) and (select_option := _recreate_select_option(option)) is not None:
                select_options.append(select_option)
    return discord.ui.Select(
        custom_id=custom_id,
        placeholder=placeholder,
        min_values=min_values,
        max_values=max_values,
        options=select_options,
        disabled=True,
    )


def _recreate_entity_select(component: dict[str, Any]) -> discord.ui.Item:
    component_type = _component_type(component)
    custom_id, placeholder, min_values, max_values = _select_bounds(component)
    kwargs = {
        "custom_id": custom_id,
        "placeholder": placeholder,
        "min_values": min_values,
        "max_values": max_values,
        "disabled": True,
    }
    if component_type == discord.ComponentType.user_select.value:
        return discord.ui.UserSelect(**kwargs)
    if component_type == discord.ComponentType.role_select.value:
        return discord.ui.RoleSelect(**kwargs)
    if component_type == discord.ComponentType.mentionable_select.value:
        return discord.ui.MentionableSelect(**kwargs)
    if component_type == discord.ComponentType.channel_select.value:
        return discord.ui.ChannelSelect(**kwargs)
    return _recreate_select(component)


def _recreate_component(component: dict[str, Any]) -> discord.ui.Item | None:
    component_type = _component_type(component)
    if component_type == discord.ComponentType.action_row.value:
        children = [
            item
            for child in component.get("components", [])
            if isinstance(child, dict)
            if (item := _recreate_component(child)) is not None
        ]
        return discord.ui.ActionRow(*children) if children else None
    if component_type == discord.ComponentType.button.value:
        return _recreate_button(component)
    if component_type == discord.ComponentType.select.value:
        return _recreate_select(component)
    if component_type in {
        discord.ComponentType.user_select.value,
        discord.ComponentType.role_select.value,
        discord.ComponentType.mentionable_select.value,
        discord.ComponentType.channel_select.value,
    }:
        return _recreate_entity_select(component)
    if component_type == discord.ComponentType.text_display.value:
        content = component.get("content")
        return discord.ui.TextDisplay(content if isinstance(content, str) else "")
    if component_type == discord.ComponentType.separator.value:
        spacing = component.get("spacing")
        return discord.ui.Separator(
            visible=component.get("visible", True) is not False,
            spacing=discord.SeparatorSpacing.large if spacing == discord.SeparatorSpacing.large.value else discord.SeparatorSpacing.small,
        )
    if component_type == discord.ComponentType.container.value:
        children = [
            item
            for child in component.get("components", [])
            if isinstance(child, dict)
            if (item := _recreate_component(child)) is not None
        ]
        accent_colour = component.get("accent_color", component.get("accent_colour"))
        return discord.ui.Container(
            *children,
            accent_colour=accent_colour if isinstance(accent_colour, int) else None,
            spoiler=component.get("spoiler", False) is True,
        )
    if component_type == discord.ComponentType.section.value:
        children = [
            item
            for child in component.get("components", [])
            if isinstance(child, dict)
            if (item := _recreate_component(child)) is not None
        ]
        accessory_payload = component.get("accessory")
        accessory = _recreate_component(accessory_payload) if isinstance(accessory_payload, dict) else None
        if children and accessory is not None:
            return discord.ui.Section(*children, accessory=accessory)
        return discord.ui.Container(*children) if children else None
    if component_type == discord.ComponentType.thumbnail.value:
        media = _component_media_url(component)
        if media is None:
            return None
        description = component.get("description")
        return discord.ui.Thumbnail(
            media,
            description=description if isinstance(description, str) else None,
            spoiler=component.get("spoiler", False) is True,
        )
    if component_type == discord.ComponentType.media_gallery.value:
        raw_items = component.get("items")
        if not isinstance(raw_items, list):
            return None
        items = []
        for raw_item in raw_items:
            media = _component_media_url(raw_item)
            if media is None:
                continue
            description = raw_item.get("description") if isinstance(raw_item, dict) else None
            items.append(discord.MediaGalleryItem(
                media,
                description=description if isinstance(description, str) else None,
                spoiler=isinstance(raw_item, dict) and raw_item.get("spoiler", False) is True,
            ))
        return discord.ui.MediaGallery(*items) if items else None
    if component_type == discord.ComponentType.file.value:
        media = _component_media_url(component)
        if media is None:
            return None
        return discord.ui.File(media, spoiler=component.get("spoiler", False) is True)
    return None


def _recreate_message_components(message: dict[str, object]) -> list[discord.ui.Item]:
    components = message.get("components")
    if not isinstance(components, list):
        return []
    items: list[discord.ui.Item] = []
    for component in components:
        if isinstance(component, dict) and (item := _recreate_component(component)) is not None:
            items.append(item)
    return items


def _uses_components_v2(components: list[discord.ui.Item]) -> bool:
    return any(not isinstance(component, discord.ui.ActionRow) for component in components)


class RecreatedMessageView(discord.ui.LayoutView):
    def __init__(self, content: str, components: list[discord.ui.Item]) -> None:
        super().__init__(timeout=None)
        if content:
            self.add_item(discord.ui.TextDisplay(content))
        for component in components:
            self.add_item(component)


def _classic_component_view(components: list[discord.ui.Item]) -> discord.ui.View | None:
    view = discord.ui.View(timeout=None)
    for component in components:
        if isinstance(component, discord.ui.ActionRow):
            for child in component.children:
                view.add_item(child)
        else:
            view.add_item(component)
    return view if view.children else None


def _attachment_urls(message: dict[str, object]) -> list[str]:
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        return []
    urls: list[str] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        url = attachment.get("url")
        if isinstance(url, str) and url:
            urls.append(url)
    return urls


def _message_summary(message: dict[str, object]) -> str:
    lines = [
        f"Message ID: `{message.get('id')}`",
        f"Channel: <#{message.get('channel_id')}> (`{message.get('channel_id')}`)",
    ]
    for key, label in (
        ("attachments", "Attachments"),
        ("embeds", "Embeds"),
        ("components", "Components"),
        ("stickers", "Stickers"),
        ("reactions", "Reactions"),
    ):
        value = message.get(key)
        if isinstance(value, list) and value:
            lines.append(f"{label}: `{len(value)}`")
    if message.get("reference"):
        lines.append("Reference: captured")
    if message.get("interaction_metadata"):
        lines.append("Interaction metadata: captured")
    return "\n".join(lines)


class ModlogMessageContentView(discord.ui.LayoutView):
    def __init__(self, event: ModlogEvent) -> None:
        super().__init__(timeout=None)
        message = _message_payload(event)
        container = discord.ui.Container(
            discord.ui.TextDisplay("## Message Content"),
            discord.ui.Separator(),
        )
        if message is None:
            container.add_item(discord.ui.TextDisplay("No captured message payload."))
            self.add_item(container)
            return

        container.add_item(discord.ui.TextDisplay(_message_summary(message)))
        text = "\n\n".join(_message_text_parts(message))
        if text:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(_truncate_display_text(text, limit=MESSAGE_CONTENT_PREVIEW_LIMIT)))

        attachments = message.get("attachments")
        if isinstance(attachments, list) and attachments:
            attachment_lines = []
            for attachment in attachments[:MAX_EVENT_LINES]:
                if isinstance(attachment, dict):
                    attachment_lines.append(
                        f"`{attachment.get('filename')}` "
                        f"({attachment.get('content_type') or 'unknown'}, {attachment.get('size')} bytes)"
                    )
            if attachment_lines:
                container.add_item(discord.ui.Separator())
                container.add_item(discord.ui.TextDisplay("### Attachments\n" + "\n".join(attachment_lines)))

        self.add_item(container)


def _view_event(view: discord.ui.View | discord.ui.LayoutView, event_id: int | None) -> ModlogEvent | None:
    if event_id is None:
        event = getattr(view, "event", None)
        return event if isinstance(event, ModlogEvent) else None
    database = getattr(view, "database", None)
    if isinstance(database, ModlogDatabase):
        return database.read_event(event_id)
    return None


def _can_view_sensitive(interaction: discord.Interaction) -> bool:
    bot = interaction.client
    if not isinstance(bot, BotCore):
        return False
    return bot.accounts[interaction.user.id].local(interaction.guild_id).permissions.can_use(
        MODLOG_VIEW_SENSITIVE_CAPABILITY,
        registry=bot.accounts.capabilities,
    )


class ModlogMessageContentButton(discord.ui.Button):
    def __init__(self, message_key: str = "message", *, event_id: int | None = None) -> None:
        super().__init__(label="View Content", style=discord.ButtonStyle.secondary)
        self.message_key = message_key
        self.event_id = event_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _can_view_sensitive(interaction):
            await interaction.response.send_message(
                f"Missing capability `{MODLOG_VIEW_SENSITIVE_CAPABILITY}`.",
                ephemeral=True,
            )
            return
        view = self.view
        if view is None:
            await interaction.response.send_message("This message payload is not available right now.", ephemeral=True)
            return
        event = _view_event(view, self.event_id)
        if event is None:
            await interaction.response.send_message("This message payload is not available right now.", ephemeral=True)
            return
        message = _message_payload(event, self.message_key)
        if message is None:
            await interaction.response.send_message("No captured message payload.", ephemeral=True)
            return

        content = message.get("content")
        visible_content = content if isinstance(content, str) else ""
        embeds = _recreate_message_embeds(message)
        components = _recreate_message_components(message)
        if not visible_content and not embeds and not components:
            visible_content = "\n".join(_attachment_urls(message))
        if not visible_content and not embeds and not components:
            visible_content = "No visible message content captured."
        visible_content = _truncate_display_text(visible_content, limit=MESSAGE_RECREATE_CONTENT_LIMIT)

        if components:
            if _uses_components_v2(components):
                await interaction.response.send_message(
                    view=RecreatedMessageView(visible_content, components),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                classic_view = _classic_component_view(components)
                if classic_view is not None:
                    await interaction.response.send_message(
                        content=visible_content if visible_content else None,
                        embeds=embeds,
                        view=classic_view,
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    await interaction.response.send_message(
                        content=visible_content if visible_content else None,
                        embeds=embeds,
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            return

        await interaction.response.send_message(
            content=visible_content,
            embeds=embeds,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ModlogRawContentButton(discord.ui.Button):
    def __init__(self, message_key: str = "message", *, event_id: int | None = None) -> None:
        super().__init__(label="View Raw Content", style=discord.ButtonStyle.secondary)
        self.message_key = message_key
        self.event_id = event_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _can_view_sensitive(interaction):
            await interaction.response.send_message(
                f"Missing capability `{MODLOG_VIEW_SENSITIVE_CAPABILITY}`.",
                ephemeral=True,
            )
            return
        view = self.view
        if view is None:
            await interaction.response.send_message("This message payload is not available right now.", ephemeral=True)
            return
        event = _view_event(view, self.event_id)
        if event is None:
            await interaction.response.send_message("This message payload is not available right now.", ephemeral=True)
            return
        message = _message_payload(event, self.message_key)
        if message is None:
            await interaction.response.send_message("No captured message payload.", ephemeral=True)
            return

        raw = json.dumps(message, indent=2, ensure_ascii=False, sort_keys=True)
        chunks = chunk_text(raw, RAW_CONTENT_CHUNK_LIMIT)
        if not chunks:
            chunks = ["{}"]

        await interaction.response.send_message(
            f"```json\n{chunks[0]}\n```",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        for chunk in chunks[1:]:
            await interaction.followup.send(
                f"```json\n{chunk}\n```",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


def _format_gateway_capture(event: ModlogEvent) -> list[str]:
    lines: list[str] = []
    message = event.raw.get("message")
    if isinstance(message, dict):
        lines.append("")
        lines.append("### Captured Message")
        lines.append(f"Message ID: `{message.get('id')}`")
        lines.append(f"Channel: <#{message.get('channel_id')}> (`{message.get('channel_id')}`)")
        lines.append(_format_message_content_preview(message))
        lines.append(_message_summary(message))

    member = event.raw.get("member")
    if isinstance(member, dict):
        lines.append("")
        lines.append("### Captured Member")
        lines.append(f"User: <@{member.get('id')}> (`{member.get('id')}`)")
        if member.get("joined_at"):
            lines.append(f"Joined: `{member.get('joined_at')}`")
        roles = member.get("roles")
        if isinstance(roles, list) and roles:
            role_ids = [
                role.get("id")
                for role in roles
                if isinstance(role, dict) and isinstance(role.get("id"), int)
            ]
            lines.append("Roles: " + ", ".join(f"<@&{role_id}> (`{role_id}`)" for role_id in role_ids[:MAX_EVENT_LINES]))
    return lines


def _nested_get(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def format_event_details(
    event: ModlogEvent,
    *,
    reverse_actions: Iterable[ModlogReverseAction] = (),
    include_gateway_capture: bool = True,
) -> str:
    lines = [
        f"ID: `{event.id}`",
        f"Action: `{event.action}`",
        f"Guild: `{event.guild_id}`",
        f"Created: <t:{int(event.created_at.timestamp())}:F>",
        f"Recorded: <t:{int(event.imported_at.timestamp())}:F>",
        f"Actor: {entity_text(event.actor)}",
        f"Target: {entity_text(event.target)}",
    ]
    if event.action == "modlog.undo":
        original_event_id = _nested_get(event.raw, "undo", "undid_event_id")
        if isinstance(original_event_id, int):
            lines.append(f"Undid Event: `{original_event_id}`")
    if event.reason:
        lines.append(f"Reason: {discord.utils.escape_markdown(event.reason)}")

    if include_gateway_capture:
        lines.extend(_format_gateway_capture(event))

    if event.changes:
        lines.append("")
        lines.append("### Changes")
        for change in event.changes[:MAX_EVENT_LINES]:
            old = repr(change.old)
            new = repr(change.new)
            lines.append(f"`{change.key}`: `{old}` -> `{new}`")
        if len(event.changes) > MAX_EVENT_LINES:
            lines.append(f"-# {len(event.changes) - MAX_EVENT_LINES} more changes")

    reverse_action_list = list(reverse_actions)
    if reverse_action_list:
        lines.append("")
        lines.append("### Reverse Actions")
        action_definition = ACTIONS.get(event.action)
        undo_rule = action_definition.undo if action_definition is not None else None
        for reverse in reverse_action_list:
            state = "possible" if reverse.possible else "not possible"
            reason = f": {discord.utils.escape_markdown(reverse.reason)}" if reverse.reason else ""
            description = "Undo this event."
            if undo_rule is not None:
                if isinstance(undo_rule.description, str):
                    description = undo_rule.description
                else:
                    description = undo_rule.description(event, reverse)
            lines.append(f"{discord.utils.escape_markdown(description)} - {state}{reason}")

    return "\n".join(lines)


class ModlogUndoEventButton(discord.ui.Button["ModlogUndoResultView"]):
    def __init__(self) -> None:
        super().__init__(label="View Undo Event", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            await interaction.response.send_message(
                "The undo event is not available right now.",
                ephemeral=True,
            )
            return
        event = view.database.read_event(view.undo_event_id)
        if event is None:
            await interaction.response.send_message(
                "The undo event could not be found.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=ModlogEventView(event, database=view.database),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ModlogUndoResultView(discord.ui.LayoutView):
    def __init__(
        self,
        result: ModlogUndoResult,
        *,
        database: ModlogDatabase,
        undo_event_id: int,
    ) -> None:
        super().__init__(timeout=None)
        self.database = database
        self.undo_event_id = undo_event_id
        container = discord.ui.Container(
            discord.ui.TextDisplay(f"## {result.title}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(result.message),
        )
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(ModlogUndoEventButton()))
        self.add_item(container)


class ModlogUndoButton(discord.ui.Button["ModlogEventView"]):
    def __init__(self) -> None:
        super().__init__(label="Undo", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        bot = interaction.client
        if view is None or not isinstance(bot, BotCore):
            await interaction.response.send_message(
                "Undo is not available right now.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "Undo can only run in a server.",
                ephemeral=True,
            )
            return

        account = bot.accounts[interaction.user.id].local(interaction.guild.id)
        if not account.permissions.can_use(MODLOG_UNDO_CAPABILITY, registry=bot.accounts.capabilities):
            await interaction.response.send_message(
                f"Missing capability `{MODLOG_UNDO_CAPABILITY}`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await undo_event(interaction.guild, view.event)
        await write_modlog_undo(
            interaction,
            extra={
                "action": "undo",
                "undid_event_id": view.event.id,
                "undo_succeeded": result.success,
                "result_title": result.title,
            },
            raw={
                "undo": {
                    "undid_event_id": view.event.id,
                    "undo_event_id": interaction.id,
                    "success": result.success,
                },
            },
        )
        await interaction.followup.send(
            view=ModlogUndoResultView(
                result,
                database=view.database,
                undo_event_id=interaction.id,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none()
        )


class ModlogGroupDetailsButton(discord.ui.Button["ModlogView"]):
    def __init__(self, event_ids: Iterable[int]) -> None:
        super().__init__(label="Details", style=discord.ButtonStyle.secondary)
        self.event_ids = tuple(event_ids)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            await interaction.response.send_message(
                "This modlog group is not available right now.",
                ephemeral=True,
            )
            return

        events = [
            event
            for event_id in self.event_ids
            if (event := view.database.read_event(event_id)) is not None
        ]
        events.sort(key=lambda event: event.id, reverse=True)
        if not events:
            await interaction.response.send_message("No events found for this group.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Modlog details can only be shown in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        for event in events:
            reverse_actions = await reverse_actions_for_event(interaction.guild, event)
            await interaction.followup.send(
                view=ModlogEventView(event, database=view.database, reverse_actions=reverse_actions),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


def _add_message_snapshot_section(
    container: discord.ui.Container,
    *,
    title: str,
    message: dict[str, object],
    message_key: str,
) -> None:
    container.add_item(discord.ui.TextDisplay(f"### {title}"))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(_format_message_content_preview(message)))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.ActionRow(
        ModlogMessageContentButton(message_key),
        ModlogRawContentButton(message_key),
    ))


class ModlogEventView(discord.ui.LayoutView):
    def __init__(
        self,
        event: ModlogEvent,
        *,
        database: ModlogDatabase,
        reverse_actions: Iterable[ModlogReverseAction] = (),
    ) -> None:
        super().__init__(timeout=None)
        self.event = event
        self.database = database
        self.reverse_actions = tuple(reverse_actions)
        container = discord.ui.Container(
            discord.ui.TextDisplay("## Modlog Event"),
            discord.ui.Separator(),
        )
        before_message = _message_payload(event, "before_message")
        message = _message_payload(event)
        container.add_item(discord.ui.TextDisplay(
            format_event_details(
                event,
                reverse_actions=self.reverse_actions,
                include_gateway_capture=before_message is None or message is None,
            )
        ))
        if before_message is not None and message is not None:
            container.add_item(discord.ui.Separator())
            _add_message_snapshot_section(
                container,
                title="From",
                message=before_message,
                message_key="before_message",
            )
            container.add_item(discord.ui.Separator())
            _add_message_snapshot_section(
                container,
                title="To",
                message=message,
                message_key="message",
            )
        elif message is not None:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.ActionRow(ModlogMessageContentButton(), ModlogRawContentButton()))
        if any(reverse.possible for reverse in self.reverse_actions):
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.ActionRow(ModlogUndoButton()))
        self.add_item(container)


class ModlogEventButton(discord.ui.Button["ModlogView"]):
    def __init__(self, event_id: int) -> None:
        super().__init__(label="Details", style=discord.ButtonStyle.secondary)
        self.event_id = event_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            await interaction.response.send_message(
                "This modlog entry is not available right now.",
                ephemeral=True,
            )
            return

        event = view.database.read_event(self.event_id)
        if event is None:
            await interaction.response.send_message(
                f"No event found for `{self.event_id}`.",
                ephemeral=True,
            )
            return
        if interaction.guild_id is not None and event.guild_id != interaction.guild_id:
            await interaction.response.send_message(
                "That event belongs to a different server.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "Modlog details can only be shown in a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        reverse_actions = await reverse_actions_for_event(interaction.guild, event)
        await interaction.followup.send(
            view=ModlogEventView(event, database=view.database, reverse_actions=reverse_actions),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none()
        )


def format_group_text(group: RelatedGroup, *, limit: int = MODLOG_GROUP_CHAR_LIMIT) -> str:
    lines: list[str] = []
    remaining = 0
    for index, event in enumerate(group.events):
        line = format_event_line(event)
        candidate = "\n".join([*lines, line])
        if lines and count_characters(candidate) > limit:
            remaining = len(group.events) - index
            break
        if not lines and count_characters(candidate) > limit:
            lines.append(_truncate_display_text(line, limit=limit))
            remaining = len(group.events) - index - 1
            break
        lines.append(line)

    if remaining:
        more_line = f"`{remaining}` more"
        candidate = "\n".join([*lines, more_line])
        if count_characters(candidate) <= limit:
            lines.append(more_line)
    return "\n".join(lines)


class RenderBudget:
    def __init__(self, *, chars: int, elems: int) -> None:
        self.chars = chars
        self.elems = elems
        self.used_chars = 0
        self.used_elems = 0

    def count(self, *, chars: int = 0, elems: int = 0) -> bool:
        if self.used_chars + chars > self.chars or self.used_elems + elems > self.elems:
            return False
        self.used_chars += chars
        self.used_elems += elems
        return True


class ModlogView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        database: ModlogDatabase,
        guild_id: int,
        action: str | None,
        actor_id: int | None,
        target_id: int | None,
        page_size: int,
        page_first_id: int | None = None,
        previous_first_ids: Iterable[int] = (),
    ) -> None:
        super().__init__(timeout=900)
        self.database = database
        self.resolver = RelatedResolver()
        self.guild_id = guild_id
        self.action = action
        self.actor_id = actor_id
        self.target_id = target_id
        self.page_size = min(MAX_EVENTS_PER_PAGE, page_size)
        self.page_first_id = page_first_id
        self.previous_first_ids = list(previous_first_ids)
        self.current_first_id: int | None = None
        self.next_first_id: int | None = None
        self.has_next = False
        self.render()

    @property
    def page_number(self) -> int:
        return len(self.previous_first_ids) + 1

    def anchor_events(self) -> list[ModlogEvent]:
        before_id = self.page_first_id + 1 if self.page_first_id is not None else None
        return self.database.query_events(
            guild_id=self.guild_id,
            action=self.action,
            actor_id=self.actor_id,
            target_id=self.target_id,
            before_id=before_id,
            limit=MODLOG_PAGE_FETCH_LIMIT + 1,
        )

    def candidate_events(self, events: list[ModlogEvent]) -> list[ModlogEvent]:
        after_id, before_id = self.resolver.widened_bounds(events)
        if after_id is None and before_id is None:
            return []
        return self.database.query_events(
            guild_id=self.guild_id,
            after_id=after_id,
            before_id=before_id,
            limit=None,
        )

    def render(self) -> None:
        self.clear_items()
        events = self.anchor_events()
        if events:
            self.current_first_id = events[0].id
        visible_events = events[:MODLOG_PAGE_FETCH_LIMIT]
        groups = self.resolver.group(visible_events, self.candidate_events(visible_events))
        rendered_groups: list[RelatedGroup] = []
        rendered_base_ids: set[int] = set()
        base_ids = {event.id for event in events}
        budget = RenderBudget(chars=MODLOG_PAGE_CHAR_LIMIT, elems=MODLOG_PAGE_ELEMENT_LIMIT)
        container = discord.ui.Container(
            discord.ui.TextDisplay(f"## Modlog · Page {self.page_number}"),
            discord.ui.Separator(),
        )
        budget.count(chars=count_characters(f"## Modlog · Page {self.page_number}"), elems=2)

        if groups:
            for group in groups:
                text = format_group_text(group)
                chars = count_characters(text)
                if rendered_groups and not budget.count(chars=chars, elems=3):
                    break
                if not rendered_groups and not budget.count(chars=chars, elems=3):
                    text = _truncate_display_text(text, limit=MODLOG_GROUP_CHAR_LIMIT)
                    budget.count(chars=count_characters(text), elems=3)
                container.add_item(discord.ui.Section(
                    discord.ui.TextDisplay(text),
                    accessory=ModlogGroupDetailsButton(event.id for event in group.events),
                ))
                rendered_groups.append(group)
                rendered_base_ids.update(event.id for event in group.events if event.id in base_ids)
        else:
            container.add_item(discord.ui.TextDisplay("No matching events."))

        if rendered_base_ids:
            last_rendered_base_id = min(rendered_base_ids)
            next_event = next((event for event in events if event.id < last_rendered_base_id), None)
            self.next_first_id = next_event.id if next_event is not None else None
        else:
            self.next_first_id = None
        self.has_next = self.next_first_id is not None

        previous_button = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            disabled=not self.previous_first_ids,
        )
        next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            disabled=not self.has_next,
        )
        refresh_button = discord.ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.secondary,
        )
        previous_button.callback = self.previous_page
        next_button.callback = self.next_page
        refresh_button.callback = self.refresh_page

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(previous_button, next_button, refresh_button))
        self.add_item(container)

    async def previous_page(self, interaction: discord.Interaction) -> None:
        if self.previous_first_ids:
            self.page_first_id = self.previous_first_ids.pop()
        self.render()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def next_page(self, interaction: discord.Interaction) -> None:
        if self.next_first_id is not None:
            if self.current_first_id is not None:
                self.previous_first_ids.append(self.current_first_id)
            self.page_first_id = self.next_first_id
        self.render()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())

    async def refresh_page(self, interaction: discord.Interaction) -> None:
        self.render()
        await interaction.response.edit_message(view=self, allowed_mentions=discord.AllowedMentions.none())


async def action_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    current = current.lower()
    matches = [
        name
        for name in action_names()
        if current in name.lower()
    ]
    return [
        app_commands.Choice(name=name, value=name)
        for name in matches[:MAX_ACTION_CHOICES]
    ]


async def setup(bot: BotCore) -> None:
    database = ModlogDatabase(database_path_from_bot(bot))
    logger = bot.logger.getChild("Modlog")
    bot.accounts.capabilities.register(MODLOG_UNDO_CAPABILITY)

    def record_event(event: ModlogEvent, *, replace: bool = True) -> bool:
        return database.write_event(event, replace=replace)

    @bot.ready_callback
    async def scan_since_last_connect() -> None:
        for guild in bot.guilds:
            if not await can_scan_audit_logs(bot, guild):
                logger.debug("Skipping audit log scan for guild %s: missing View Audit Log", guild.id)
                continue

            after_id = database.max_event_id(guild_id=guild.id)
            after = (
                discord.utils.snowflake_time(after_id) - AUDIT_LOG_RESCAN_OVERLAP
                if after_id is not None else
                None
            )
            try:
                scan = await retrieve_and_scan(
                    guild,
                    limit=None,
                    after=after,
                    oldest_first=True,
                )
            except discord.HTTPException:
                logger.exception("Failed scanning audit logs for guild %s", guild.id)
                continue
            except Exception:
                logger.exception("Unexpected error scanning audit logs for guild %s", guild.id)
                continue

            written = sum(1 for event in scan.events if record_event(event, replace=True))
            if written or scan.stats.scanned:
                logger.info(
                    "Scanned audit logs for guild %s: %s new event(s), %s written",
                    guild.id,
                    scan.stats.scanned,
                    written,
                )

    @bot.audit_log_entry_callback
    async def record_audit_log_entry(entry: discord.AuditLogEntry) -> None:
        record_event(normalize_entry(entry))

    @bot.listen("raw_message_delete")
    async def record_raw_message_delete(payload: discord.RawMessageDeleteEvent) -> None:
        event = raw_message_delete_event(payload)
        if event is not None:
            record_event(event)

    @bot.listen("raw_bulk_message_delete")
    async def record_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent) -> None:
        for event in raw_bulk_message_delete_events(payload):
            record_event(event)

    @bot.listen("raw_message_edit")
    async def record_raw_message_edit(payload: discord.RawMessageUpdateEvent) -> None:
        event = raw_message_edit_event(payload)
        if event is not None:
            record_event(event)

    @bot.listen("raw_reaction_clear")
    async def record_raw_reaction_clear(payload: discord.RawReactionClearEvent) -> None:
        event = raw_reaction_clear_event(payload)
        if event is not None:
            record_event(event)

    @bot.listen("raw_reaction_clear_emoji")
    async def record_raw_reaction_clear_emoji(payload: discord.RawReactionClearEmojiEvent) -> None:
        event = raw_reaction_clear_emoji_event(payload)
        if event is not None:
            record_event(event)

    @bot.listen("thread_member_join")
    async def record_thread_member_join(member: discord.ThreadMember) -> None:
        event = thread_member_join_event(member)
        if event is not None:
            record_event(event)

    @bot.listen("raw_thread_member_remove")
    async def record_raw_thread_member_remove(payload: discord.RawThreadMembersUpdate) -> None:
        guild = bot.get_guild(payload.guild_id)
        thread = guild.get_thread(payload.thread_id) if guild is not None else None
        for event in raw_thread_member_remove_events(payload, thread):
            record_event(event)

    @bot.member_join_callback
    async def record_member_join(member: discord.Member | discord.User) -> None:
        event = member_join_event(member)
        if event is not None:
            record_event(event)

    @bot.listen("raw_member_remove")
    async def record_raw_member_remove(payload: discord.RawMemberRemoveEvent) -> None:
        record_event(raw_member_remove_event(payload))

    @bot.member_update_callback
    async def record_member_update(before: discord.Member, after: discord.Member) -> None:
        for event in member_update_events(before, after):
            record_event(event)

    @bot.member_ban_callback
    async def record_member_ban(guild: discord.Guild, user: discord.User | discord.Member) -> None:
        record_event(member_ban_event(guild, user))

    @bot.member_unban_callback
    async def record_member_unban(guild: discord.Guild, user: discord.User) -> None:
        record_event(member_unban_event(guild, user))

    @bot.setup.command(
        name="modlog",
        description="Browse moderation log events",
        capabilities=["modlog.view"],
        eph=True,
        defer=False,
    )
    async def modlog(
        interaction: discord.Interaction,
        action: str | None = None,
        actor: discord.User | None = None,
        target: discord.User | None = None,
        limit: app_commands.Range[int, 1, 10] = MAX_EVENTS_PER_PAGE,
    ) -> None:
        if interaction.guild is None:
            await bot.discord.send("Modlog can only run in a server.", response=True, ephemeral=True)
            return

        if action is not None and not is_known_action_name(action):
            await bot.discord.send(f"Unknown audit action `{discord.utils.escape_markdown(action)}`.", response=True, ephemeral=True)
            return

        await bot.discord.send(
            view=ModlogView(
                database=database,
                guild_id=interaction.guild.id,
                action=action,
                actor_id=actor.id if actor is not None else None,
                target_id=target.id if target is not None else None,
                page_size=int(limit),
            ),
            response=True,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @modlog.autocomplete("action")
    async def modlog_action_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await action_autocomplete(interaction, current)
