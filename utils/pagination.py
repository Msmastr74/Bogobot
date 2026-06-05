from dataclasses import dataclass
from typing import Any, Generic, Literal
from utils.discord import count_characters
from utils.type import T
import abc

import discord

DISPLAY_TEXT_LIMIT = 3200
DISPLAY_CONTENT_LIMIT = 2800
TRUNCATION_TEXT = "\n... truncated ..."


def truncate_text(text: str, limit: int) -> str:
    if count_characters(text) <= limit:
        return text

    suffix_budget = max(0, limit - count_characters(TRUNCATION_TEXT))
    current: list[str] = []
    current_length = 0
    for character in text:
        character_length = count_characters(character)
        if current_length + character_length > suffix_budget:
            break
        current.append(character)
        current_length += character_length
    return "".join(current) + TRUNCATION_TEXT


@dataclass
class PageSection:
    title: str | None
    body: str
    accent_colour: discord.Colour | int | None = None
    footer: str | None = None
    index: int | None = None


@dataclass
class Page:
    sections: list[PageSection]
    allowed_mentions: discord.AllowedMentions | None = None

    def as_edit_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "content": None,
            "embeds": [],
            "attachments": [],
        }
        if self.allowed_mentions is not None:
            kwargs["allowed_mentions"] = self.allowed_mentions
        return kwargs

    def as_send_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.allowed_mentions is not None:
            kwargs["allowed_mentions"] = self.allowed_mentions
        return kwargs


@dataclass
class SectionRead(Generic[T]):
    section: PageSection
    state: T


class PaginatedView(discord.ui.LayoutView, abc.ABC, Generic[T]):
    def __init__(
        self,
        *,
        initial_state: T,
        timeout: float | None = 300,
    ):
        super().__init__(timeout=timeout)
        self.state = initial_state
        self.current_page: Page | None = None
        self.previous_page_state: T | None = None
        self.next_page_state: T | None = None

    @abc.abstractmethod
    async def next_section(self, state: T) -> SectionRead[T] | None:
        pass

    @abc.abstractmethod
    async def previous_section(self, state: T) -> SectionRead[T] | None:
        pass

    def page_allowed_mentions(self) -> discord.AllowedMentions | None:
        return None

    def empty_sections(self) -> list[PageSection]:
        return [PageSection(title=None, body="No items.")]

    def page_header(self, page: Page) -> str | None:
        titles = [section.title for section in page.sections if section.title]
        if not titles:
            return None
        if all(title == titles[0] for title in titles):
            return f"## {titles[0]}"
        return "\n".join(f"## {title}" for title in dict.fromkeys(titles))

    def page_accent_colour(self, page: Page) -> discord.Colour | int | None:
        for section in page.sections:
            if section.accent_colour is not None:
                return section.accent_colour
        return None

    async def load(self, direction: Literal["next", "previous"] = "next") -> Page:
        sections, page_state = await self._collect_sections(self.state, direction)
        if direction == "previous":
            sections.reverse()
            self.next_page_state = None
            if page_state is not None:
                self.state = page_state
                self.next_page_state = await self._find_page_state(self.state, "next")
        else:
            self.next_page_state = page_state
            
        self.previous_page_state = await self._find_page_state(self.state, "previous")
            
        if not sections:
            sections = self.empty_sections()
        self.current_page = Page(
            sections=sections,
            allowed_mentions=self.page_allowed_mentions(),
        )
        self._render_page(self.current_page)
        self.sync_controls()
        return self.current_page

    async def set_state(
        self,
        interaction: discord.Interaction,
        state: T,
        direction: Literal["next", "previous"] = "next",
    ) -> None:
        self.state = state
        page = await self.load(direction=direction)
        await interaction.response.edit_message(
            view=self,
            **page.as_edit_kwargs(),
        )

    def sync_controls(self) -> None:
        pass

    def add_controls(self) -> None:
        pass

    async def show_previous_page(self, interaction: discord.Interaction) -> None:
        if self.previous_page_state is not None:
            await self.set_state(interaction, self.previous_page_state)

    async def show_next_page(self, interaction: discord.Interaction) -> None:
        if self.next_page_state is not None:
            await self.set_state(interaction, self.next_page_state)

    async def refresh_page(self, interaction: discord.Interaction, state: T | None = None) -> None:
        await self.set_state(interaction, self.state if state is None else state)

    def _render_page(self, page: Page) -> None:
        self.clear_items()
        self.add_item(self.generate_container(page))
        self.add_controls()

    def generate_container(self, page: Page) -> discord.ui.Container:
        c = discord.ui.Container(
            accent_colour=self.page_accent_colour(page),
        )
        header = self.page_header(page)
        remaining = DISPLAY_TEXT_LIMIT
        if header:
            c.add_item(discord.ui.TextDisplay(header))
            c.add_item(discord.ui.Separator())
            remaining -= count_characters(header)

        text = self._page_body_text(page.sections)
        text = truncate_text(text, remaining)

        c.add_item(discord.ui.TextDisplay(text or "\u200d"))
        return c

    def _page_body_text(self, sections: list[PageSection]) -> str:
        return "\n".join(self._section_text(section) for section in sections)

    def _section_text(self, section: PageSection) -> str:
        parts: list[str] = []
        parts.append(section.body or "\u200d")
        if section.footer:
            parts.append(f"-# {section.footer}")
        return "\n".join(parts)

    async def _collect_sections(
        self,
        state: T,
        direction: Literal["next", "previous"],
    ) -> tuple[list[PageSection], T | None]:
        sections: list[PageSection] = []
        current_state = state
        successful_state = state
        remaining = DISPLAY_CONTENT_LIMIT
        reader = self.next_section if direction == "next" else self.previous_section

        while True:
            read = await reader(current_state)
            if read is None:
                if direction == "previous" and sections:
                    return sections, successful_state
                return sections, None

            text = self._section_text(read.section)
            text_len = count_characters(text) + (1 if sections else 0)
            if text_len > remaining and sections:
                return sections, successful_state

            sections.append(read.section)
            current_state = read.state
            successful_state = read.state

            if text_len >= remaining:
                peek = await reader(current_state)
                if peek is not None or direction == "previous":
                    return sections, current_state
                return sections, None

            remaining -= text_len

    async def _find_page_state(
        self,
        state: T,
        direction: Literal["next", "previous"],
    ) -> T | None:
        _sections, page_state = await self._collect_sections(state, direction)
        return page_state
