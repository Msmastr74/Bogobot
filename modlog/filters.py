from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import discord

from modlog.audit_log import ModlogEvent

if TYPE_CHECKING:
    from modlog.plugin import ModlogView


FilterMode = Literal["on", "grouped", "off"]
TargetKind = Literal["user", "role", "channel", "raw"]

LIMIT_OPTIONS: tuple[int | None, ...] = (10, 20, 50, 80, 150, None)
EVENTS_PER_PAGE = 10


@dataclass
class ModlogTargetFilter:
    kind: TargetKind
    id: int

    def label(self) -> str:
        if self.kind == "user":
            return f"User <@{self.id}> (`{self.id}`)"
        if self.kind == "role":
            return f"Role <@&{self.id}> (`{self.id}`)"
        if self.kind == "channel":
            return f"Channel <#{self.id}> (`{self.id}`)"
        return f"Raw target `{self.id}`"


@dataclass
class ModlogFilters:
    event_modes: dict[str, FilterMode] = field(default_factory=dict)
    actor_id: int | None = None
    target: ModlogTargetFilter | None = None
    limit: int | None = 10

    def copy(self) -> "ModlogFilters":
        return ModlogFilters(
            event_modes=dict(self.event_modes),
            actor_id=self.actor_id,
            target=(
                None if self.target is None else
                ModlogTargetFilter(self.target.kind, self.target.id)
            ),
            limit=self.limit,
        )

    def mode_for(self, action: str) -> FilterMode:
        mode = self.event_modes.get(action)
        if mode is not None:
            return mode
        return "off"

    def set_mode(self, action: str, mode: FilterMode) -> None:
        self.event_modes[action] = mode

    def cycle_mode(self, action: str) -> FilterMode:
        current = self.mode_for(action)
        next_modes: dict[FilterMode, FilterMode] = {
            "on": "grouped",
            "grouped": "off",
            "off": "on",
        }
        next_mode = next_modes[current]
        self.set_mode(action, next_mode)
        return next_mode

    def invert_mode(self, action: str) -> FilterMode:
        current = self.mode_for(action)
        next_modes: dict[FilterMode, FilterMode] = {
            "on": "off",
            "grouped": "grouped",
            "off": "on",
        }
        next_mode = next_modes[current]
        self.set_mode(action, next_mode)
        return next_mode

    def include_anchor(self, event: ModlogEvent) -> bool:
        return self.mode_for(event.action) == "on" and self.matches_entity_filters(event)

    def include_candidate(self, event: ModlogEvent) -> bool:
        return self.mode_for(event.action) != "off"

    def matches_entity_filters(self, event: ModlogEvent) -> bool:
        if self.actor_id is not None and event.actor_id != self.actor_id:
            return False
        if self.target is not None and event.target_id != self.target.id:
            return False
        return True

    def same_as(self, other: "ModlogFilters") -> bool:
        event_names = set(self.event_modes) | set(other.event_modes)
        return (
            all(self.mode_for(name) == other.mode_for(name) for name in event_names)
            and self.actor_id == other.actor_id
            and self.target == other.target
            and self.limit == other.limit
        )

    def summary(self, default: "ModlogFilters | None" = None) -> str:
        lines: list[str] = []
        if default is None:
            lines.append(f"Event types: `{len(self.event_modes)}`")
        else:
            changed_modes = {
                action: mode
                for action, mode in sorted(self.event_modes.items())
                if mode != default.mode_for(action)
            }
            lines.append(f"Event type overrides: `{len(changed_modes)}`")
        lines.append(f"Actor: {f'<@{self.actor_id}> (`{self.actor_id}`)' if self.actor_id is not None else '`any`'}")
        lines.append(f"Target: {self.target.label() if self.target is not None else '`any`'}")
        lines.append(f"Limit: `{self.limit if self.limit is not None else 'unlimited'}`")
        return "\n".join(lines)


DEFAULT_FILTERS = ModlogFilters(event_modes={
    "on_raw_message_delete": "grouped",
    "on_raw_message_edit": "grouped",
    "on_member_update": "grouped"
})


@dataclass
class FilterOwner:
    view: "ModlogView"
    interaction: discord.Interaction


async def refresh_owner_message(owner: FilterOwner) -> None:
    await owner.interaction.edit_original_response(
        view=owner.view,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def update_owner(owner: FilterOwner) -> None:
    owner.view.reset_pagination()
    owner.view.render()
    await refresh_owner_message(owner)


def default_filters_for_events(event_names: tuple[str, ...]) -> ModlogFilters:
    filters = DEFAULT_FILTERS.copy()
    for event_name in event_names:
        filters.event_modes.setdefault(event_name, "on")
    return filters


def default_filters_for_owner(owner: FilterOwner) -> ModlogFilters:
    return default_filters_for_events(owner.view.event_names())


def filter_button_style(filters: ModlogFilters, default: ModlogFilters) -> discord.ButtonStyle:
    return discord.ButtonStyle.secondary if filters.same_as(default) else discord.ButtonStyle.primary


class ModlogFilterButton(discord.ui.Button["ModlogView"]):
    def __init__(self, filters: ModlogFilters, default: ModlogFilters) -> None:
        super().__init__(
            label="Filters",
            style=filter_button_style(filters, default),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None:
            await interaction.response.send_message("Filters are not available right now.", ephemeral=True)
            return
        owner = FilterOwner(view=view, interaction=view.interaction)
        await interaction.response.send_message(
            view=ModlogFilterPanel(owner, owner.view.filters.copy()),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ModlogFilterPanel(discord.ui.LayoutView):
    def __init__(self, owner: FilterOwner, draft: ModlogFilters) -> None:
        super().__init__(timeout=300)
        self.owner = owner
        self.draft = draft
        events_button = discord.ui.Button(label="Event types", style=discord.ButtonStyle.secondary)
        entities_button = discord.ui.Button(label="Actors, targets, limit", style=discord.ButtonStyle.secondary)
        reset_button, apply_button = reset_apply_buttons(self.owner, self.draft)
        events_button.callback = self.open_events
        entities_button.callback = self.open_entities
        reset_button.callback = self.reset_filters
        apply_button.callback = self.apply
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## Modlog Filters"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(self.draft.summary(default_filters_for_owner(self.owner))),
            discord.ui.Separator(),
            discord.ui.ActionRow(events_button, entities_button),
            discord.ui.Separator(),
            discord.ui.ActionRow(reset_button, apply_button),
        ))

    async def open_events(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=ModlogEventFilterView(self.owner, self.draft),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def open_entities(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=ModlogEntityFilterView(self.owner, self.draft),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def reset_filters(self, interaction: discord.Interaction) -> None:
        self.draft = default_filters_for_owner(self.owner)
        await interaction.response.edit_message(
            view=ModlogFilterPanel(self.owner, self.draft),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def apply(self, interaction: discord.Interaction) -> None:
        await apply_filters(interaction, self.owner, self.draft)


def filters_changed(owner: FilterOwner, draft: ModlogFilters) -> bool:
    return not draft.same_as(owner.view.filters)


def reset_apply_buttons(owner: FilterOwner, draft: ModlogFilters) -> tuple[discord.ui.Button, discord.ui.Button]:
    reset_button = discord.ui.Button(
        label="Reset",
        style=discord.ButtonStyle.danger,
        disabled=draft.same_as(default_filters_for_owner(owner)),
    )
    apply_button = discord.ui.Button(
        label="Apply",
        style=discord.ButtonStyle.primary,
        disabled=not filters_changed(owner, draft),
    )
    return reset_button, apply_button


async def apply_filters(interaction: discord.Interaction, owner: FilterOwner, draft: ModlogFilters) -> None:
    owner.view.filters = draft.copy()
    await update_owner(owner)
    await interaction.response.edit_message(
        view=ModlogFilterPanel(owner, owner.view.filters.copy()),
        allowed_mentions=discord.AllowedMentions.none(),
    )


class ModlogEventFilterView(discord.ui.LayoutView):
    def __init__(self, owner: FilterOwner, draft: ModlogFilters, *, page: int = 0) -> None:
        super().__init__(timeout=300)
        self.owner = owner
        self.draft = draft
        self.page = max(0, page)
        self.render()

    def render(self) -> None:
        self.clear_items()
        names = self.owner.view.event_names()
        total_pages = max(1, (len(names) + EVENTS_PER_PAGE - 1) // EVENTS_PER_PAGE)
        self.page = min(self.page, total_pages - 1)
        start = self.page * EVENTS_PER_PAGE
        visible = names[start:start + EVENTS_PER_PAGE]

        container = discord.ui.Container(
            discord.ui.TextDisplay(f"## Event Type Filters · Page {self.page + 1}/{total_pages}"),
            discord.ui.Separator(),
        )
        for name in visible:
            mode = self.draft.mode_for(name)
            button = discord.ui.Button(
                label=mode.title(),
                style={
                    "on": discord.ButtonStyle.success,
                    "grouped": discord.ButtonStyle.primary,
                    "off": discord.ButtonStyle.secondary,
                }[mode],
            )
            button.callback = self._cycle_callback(name)
            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay(f"`{name}`"),
                accessory=button,
            ))

        previous_button = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=self.page <= 0)
        next_button = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=self.page >= total_pages - 1)
        back_button = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        invert_button = discord.ui.Button(label="Invert", style=discord.ButtonStyle.secondary)
        previous_button.callback = self.previous_page
        next_button.callback = self.next_page
        back_button.callback = self.back
        invert_button.callback = self.invert_all
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(previous_button, next_button, back_button, invert_button))
        self.add_item(container)

    def _cycle_callback(self, action: str):
        async def callback(interaction: discord.Interaction) -> None:
            self.draft.cycle_mode(action)
            self.render()
            await interaction.response.edit_message(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return callback

    async def invert_all(self, interaction: discord.Interaction):
        for action in self.owner.view.event_names():
            self.draft.invert_mode(action)
        self.render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def previous_page(self, interaction: discord.Interaction) -> None:
        self.page -= 1
        self.render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def next_page(self, interaction: discord.Interaction) -> None:
        self.page += 1
        self.render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=ModlogFilterPanel(self.owner, self.draft),
            allowed_mentions=discord.AllowedMentions.none(),
        )

class ModlogIdModal(discord.ui.Modal):
    def __init__(
        self,
        owner: FilterOwner,
        draft: ModlogFilters,
        *,
        title: str,
        mode: Literal["actor", "target"],
        target_kind: TargetKind | None = None,
    ) -> None:
        super().__init__(title=title, timeout=300)
        self.owner = owner
        self.draft = draft
        self.mode = mode
        self.target_kind = target_kind
        self.value = discord.ui.TextInput(label="Snowflake ID", max_length=32)
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_value = self.value.value.strip()
        try:
            value = int(raw_value)
        except ValueError:
            await interaction.response.send_message("That is not a valid snowflake ID.", ephemeral=True)
            return
        if self.mode == "actor":
            self.draft.actor_id = value
        else:
            kind: TargetKind = "raw"
            if self.target_kind == "user":
                kind = "user"
            elif self.target_kind == "role":
                kind = "role"
            elif self.target_kind == "channel":
                kind = "channel"
            self.draft.target = ModlogTargetFilter(kind, value)
        await interaction.response.edit_message(
            view=ModlogEntityFilterView(self.owner, self.draft),
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ModlogLimitModal(discord.ui.Modal):
    def __init__(self, owner: FilterOwner, draft: ModlogFilters) -> None:
        super().__init__(title="Modlog Limit", timeout=300)
        self.owner = owner
        self.draft = draft
        self.value = discord.ui.TextInput(
            label="Limit",
            placeholder="10, 20, 50, 80, 150, or unlimited",
            max_length=16,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_value = self.value.value.strip().lower()
        if raw_value in {"unlimited", "none", "all"}:
            self.draft.limit = None
        else:
            try:
                limit = int(raw_value)
            except ValueError:
                await interaction.response.send_message("That is not a valid limit.", ephemeral=True)
                return
            if limit <= 0:
                await interaction.response.send_message("Limit must be greater than 0.", ephemeral=True)
                return
            self.draft.limit = limit
        await interaction.response.edit_message(
            view=ModlogEntityFilterView(self.owner, self.draft),
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ModlogEntityFilterView(discord.ui.LayoutView):
    def __init__(self, owner: FilterOwner, draft: ModlogFilters) -> None:
        super().__init__(timeout=300)
        self.owner = owner
        self.draft = draft
        self.actor_select = discord.ui.UserSelect(placeholder="Filter actor by server user")
        self.target_user_select = discord.ui.UserSelect(placeholder="Filter target by user")
        self.target_role_select = discord.ui.RoleSelect(placeholder="Filter target by role")
        self.target_channel_select = discord.ui.ChannelSelect(placeholder="Filter target by channel")
        self.limit_select = discord.ui.Select(
            placeholder="Page limit",
            options=[
                discord.SelectOption(
                    label=str(value) if value is not None else "Unlimited",
                    value=str(value) if value is not None else "unlimited",
                    default=self.draft.limit == value,
                )
                for value in LIMIT_OPTIONS
            ],
        )
        self.actor_select.callback = self.set_actor
        self.target_user_select.callback = self.set_target_user
        self.target_role_select.callback = self.set_target_role
        self.target_channel_select.callback = self.set_target_channel
        self.limit_select.callback = self.set_limit
        self.render()

    def render(self) -> None:
        self.clear_items()
        actor_id = self.draft.actor_id
        target = self.draft.target
        for option in self.limit_select.options:
            option.default = (
                (self.draft.limit is None and option.value == "unlimited")
                or option.value == str(self.draft.limit)
            )
        actor_id_button = discord.ui.Button(label="Actor ID", style=discord.ButtonStyle.secondary)
        target_id_button = discord.ui.Button(label="Raw Target ID", style=discord.ButtonStyle.secondary)
        clear_actor_button = discord.ui.Button(label="Clear Actor", style=discord.ButtonStyle.secondary, disabled=actor_id is None)
        clear_target_button = discord.ui.Button(label="Clear Target", style=discord.ButtonStyle.secondary, disabled=target is None)
        manual_limit_button = discord.ui.Button(label="Manual Limit", style=discord.ButtonStyle.secondary)
        back_button = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
        actor_id_button.callback = self.open_actor_id
        target_id_button.callback = self.open_target_id
        clear_actor_button.callback = self.clear_actor
        clear_target_button.callback = self.clear_target
        manual_limit_button.callback = self.open_limit
        back_button.callback = self.back

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## Actor, Target, Limit"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(self.draft.summary(default_filters_for_owner(self.owner))),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### Actor"),
            discord.ui.ActionRow(self.actor_select),
            discord.ui.ActionRow(actor_id_button, clear_actor_button),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### Target"),
            discord.ui.ActionRow(self.target_user_select),
            discord.ui.ActionRow(self.target_role_select),
            discord.ui.ActionRow(self.target_channel_select),
            discord.ui.ActionRow(target_id_button, clear_target_button),
            discord.ui.Separator(),
            discord.ui.TextDisplay("### Limit"),
            discord.ui.ActionRow(self.limit_select),
            discord.ui.ActionRow(manual_limit_button),
            discord.ui.Separator(),
            discord.ui.ActionRow(back_button),
        ))

    async def _edit_self(self, interaction: discord.Interaction) -> None:
        self.render()
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def set_actor(self, interaction: discord.Interaction) -> None:
        self.draft.actor_id = self.actor_select.values[0].id
        await self._edit_self(interaction)

    async def set_target_user(self, interaction: discord.Interaction) -> None:
        self.draft.target = ModlogTargetFilter("user", self.target_user_select.values[0].id)
        await self._edit_self(interaction)

    async def set_target_role(self, interaction: discord.Interaction) -> None:
        self.draft.target = ModlogTargetFilter("role", self.target_role_select.values[0].id)
        await self._edit_self(interaction)

    async def set_target_channel(self, interaction: discord.Interaction) -> None:
        self.draft.target = ModlogTargetFilter("channel", self.target_channel_select.values[0].id)
        await self._edit_self(interaction)

    async def set_limit(self, interaction: discord.Interaction) -> None:
        raw_value = self.limit_select.values[0]
        self.draft.limit = None if raw_value == "unlimited" else int(raw_value)
        await self._edit_self(interaction)

    async def open_actor_id(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ModlogIdModal(self.owner, self.draft, title="Actor ID", mode="actor"))

    async def open_target_id(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ModlogIdModal(self.owner, self.draft, title="Raw Target ID", mode="target", target_kind="raw"))

    async def open_limit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ModlogLimitModal(self.owner, self.draft))

    async def clear_actor(self, interaction: discord.Interaction) -> None:
        self.draft.actor_id = None
        await self._edit_self(interaction)

    async def clear_target(self, interaction: discord.Interaction) -> None:
        self.draft.target = None
        await self._edit_self(interaction)

    async def back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=ModlogFilterPanel(self.owner, self.draft),
            allowed_mentions=discord.AllowedMentions.none(),
        )
