from collections import Counter, defaultdict, deque
from string import Template
from typing import Callable, Literal
import io
import time

import discord
from PIL import Image

from bogobot_core import BotCore
from utils import groups
from utils.ai import AIParam, action
from discord import app_commands


MILESTONE_USAGE_TYPE = "milestones"
MILESTONE_WINDOW_SIZE = 40
MILESTONE_STABLE_RATIO = 0.6
MILESTONE_NOTIFY_LIMIT = 15
MILESTONE_NOTIFY_WINDOW_SECONDS = 5 * 60
MILESTONE_RATELIMIT_MESSAGE = "Rate limit exceeded! Notify the owner or use `/manage milestones ratelimit_reset`."
DEFAULT_MILESTONE_INITIALIZE_FORMAT = "$milestone_name initialized to `$new_value`."
DEFAULT_MILESTONE_UPDATE_FORMAT = "$milestone_name updated from `$old_value` to `$new_value`."


class MilestoneMessageView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str,
        body: str,
        gallery_items: list[tuple[str, str]] | None = None,
    ):
        # Contrary to what you might believe, timeout=None is less expensive 
        # for static layouts. It prevents spinning up an asyncio background task, 
        # and since no elements are dispatchable, discord.py skips registering 
        # this view into the global interaction listener cache entirely.
        super().__init__(timeout=None)

        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"## {title}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(body or "-# No content"))
        if gallery_items:
            container.add_item(discord.ui.Separator())
            gallery = discord.ui.MediaGallery()
            for filename, description in gallery_items[:10]:
                gallery.add_item(
                    media=f"attachment://{filename}",
                    description=description[:256],
                )
            container.add_item(gallery)
        self.add_item(container)


def milestone_image_file_factory(img: Image.Image, filename: str) -> Callable[[], discord.File]:
    def create_file() -> discord.File:
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        return discord.File(buffer, filename=filename)

    return create_file


def safe_milestone_filename_value(value: str) -> str:
    safe_value = "".join(
        c for c in value if c.isalnum() or c in (" ", "_", "-", ",")
    ).rstrip()
    return safe_value or "value"


class MilestoneTracker:
    def __init__(self, bot: BotCore):
        self.bot = bot
        self.history: defaultdict[str, deque[tuple[str, int, Image.Image | None]]] = defaultdict(
            lambda: deque(maxlen=MILESTONE_WINDOW_SIZE)
        )
        self._notify_timestamps: deque[float] = deque()
        self._ratelimited = False
        self._ratelimit_warning_sent = False

    async def _get_state(self) -> dict[str, str]:
        state = self.bot.config.get("milestones")

        if not isinstance(state, dict):
            state = {}
            self.bot.config["milestones"] = state
            await self.bot.save_config()

        normalized = {
            name: value
            for name, value in state.items()
            if (
                isinstance(name, str)
                and name != "current_value"
                and isinstance(value, str)
            )
        }

        if normalized != state:
            self.bot.config["milestones"] = normalized
            await self.bot.save_config()
            state = normalized

        return state

    async def _get_current_value(self, milestone_name: str) -> str | None:
        state = await self._get_state()
        value = state.get(milestone_name)

        if isinstance(value, str):
            return value

        return None

    async def get(self, milestone_name: str) -> str | None:
        return await self._get_current_value(milestone_name)

    async def names(self) -> set[str]:
        state = await self._get_state()
        return set(state) | set(self.history)

    async def _set_current_value(self, milestone_name: str, milestone_value: str) -> None:
        state = await self._get_state()
        state[milestone_name] = milestone_value
        self.bot.config["milestones"] = state
        await self.bot.save_config()

    async def delete(self, milestone_name: str) -> bool:
        state = await self._get_state()
        removed = state.pop(milestone_name, None) is not None
        self.history.pop(milestone_name, None)

        if removed:
            self.bot.config["milestones"] = state
            await self.bot.save_config()

        return removed

    def _format_message(
        self,
        template_key: str,
        default_template: str,
        *,
        milestone_name: str,
        old_value: str | None,
        new_value: str,
    ) -> str:
        template = self.bot.config.get(template_key, default_template)

        if not isinstance(template, str):
            template = default_template

        values = {
            "milestone_name": milestone_name,
            "old_value": old_value or "",
            "new_value": new_value,
        }

        try:
            return Template(template).substitute(values)
        except (KeyError, ValueError):
            self.bot.logger.warning(f"Invalid milestone format in config key {template_key!r}")
            return Template(default_template).substitute(values)

    async def subscribe(self, channel_id: int) -> bool:
        return await self.bot.notifications.subscribe(
            MILESTONE_USAGE_TYPE,
            channel_id,
        )

    async def unsubscribe(self, channel_id: int) -> bool:
        return await self.bot.notifications.unsubscribe(
            MILESTONE_USAGE_TYPE,
            channel_id,
        )

    def reset_ratelimit(self) -> None:
        self._notify_timestamps.clear()
        self._ratelimited = False
        self._ratelimit_warning_sent = False

    def _consume_notify_slot(self) -> bool:
        if self._ratelimited:
            return False

        now = time.monotonic()
        while self._notify_timestamps and now - self._notify_timestamps[0] >= MILESTONE_NOTIFY_WINDOW_SECONDS:
            self._notify_timestamps.popleft()

        if len(self._notify_timestamps) >= MILESTONE_NOTIFY_LIMIT:
            self._ratelimited = True
            return False

        self._notify_timestamps.append(now)
        return True

    async def _notify_ratelimit_exceeded(self) -> None:
        if self._ratelimit_warning_sent:
            return

        self._ratelimit_warning_sent = True
        await self.bot.notifications.notify(
            MILESTONE_USAGE_TYPE,
            content=MILESTONE_RATELIMIT_MESSAGE,
        )

    def _get_stable_value(self, milestone_name: str) -> str | None:
        """
        Returns the stable value from the recent update window.

        API-provided stats are treated as authoritative, so API mode returns
        the latest value immediately. OCR mode keeps the rolling stability
        filter because OCR can be noisy.

        Until there are exactly MILESTONE_WINDOW_SIZE collected updates, this
        returns None.

        The top value must be a supermajority of the window. If there is a tie
        for mode, this returns None because the stable value is ambiguous.
        """

        history = self.history[milestone_name]

        stats_source = str(self.bot.config.get("stats_source", "api")).lower()
        if stats_source in {"api", "event", "events"}:
            if not history:
                return None
            return history[-1][0]

        if len(history) < MILESTONE_WINDOW_SIZE:
            return None

        counts = Counter(val for val, timestamp, img in history)
        most_common = counts.most_common()

        if not most_common:
            return None

        if len(most_common) >= 2 and most_common[0][1] == most_common[1][1]:
            return None

        if most_common[0][1] / len(history) < MILESTONE_STABLE_RATIO:
            return None

        return most_common[0][0]

    async def update(
        self,
        milestone_name: str,
        milestone_value: str,
        *,
        timestamp: int,
        img: Image.Image | None = None,
    ) -> str | None:
        """
        Called by your update stats function.

        Example:
            await bot.milestones.update("Best run", "11/25", timestamp=1710000000)

        The input milestone value is considered one update.

        Nothing happens until 60 updates have been collected in memory. After
        that, the mode of the last 60 updates is considered the stable value.

        If the stable value changes from the previously confirmed value, all
        subscribed milestone channels are notified.

        Returns the newly confirmed value if it changed, otherwise None.
        """

        milestone_name = milestone_name.strip()
        milestone_value = milestone_value.strip()

        if not milestone_name or not milestone_value:
            return None

        self.history[milestone_name].append((milestone_value, timestamp, img))

        stable_value = self._get_stable_value(milestone_name)

        if stable_value is None:
            return None

        current_value = await self._get_current_value(milestone_name)

        if stable_value == current_value:
            return None

        await self._set_current_value(milestone_name, stable_value)

        await self.notify_milestone_change(
            milestone_name=milestone_name,
            old_value=current_value,
            new_value=stable_value,
        )

        return stable_value

    async def notify_milestone_change(
        self,
        *,
        milestone_name: str,
        old_value: str | None,
        new_value: str,
    ) -> None:
        if old_value is None:
            content = self._format_message(
                "milestone_initialize_format",
                DEFAULT_MILESTONE_INITIALIZE_FORMAT,
                milestone_name=milestone_name,
                old_value=old_value,
                new_value=new_value,
            )
        else:
            content = self._format_message(
                "milestone_update_format",
                DEFAULT_MILESTONE_UPDATE_FORMAT,
                milestone_name=milestone_name,
                old_value=old_value,
                new_value=new_value,
            )

        if not self._consume_notify_slot():
            await self._notify_ratelimit_exceeded()
            return

        file_templates: list[Callable[[], discord.File]] = []
        gallery_items: list[tuple[str, str]] = []
        value_lines: list[str] = []
        history = self.history.get(milestone_name)
        if history:
            target_val = new_value
            cluster_idx = -1
            history_list = list(history)
            
            for i in range(len(history_list) - 1):
                if history_list[i][0] == target_val and history_list[i+1][0] == target_val:
                    cluster_idx = i
                    break
            
            if cluster_idx == -1:
                for i in range(len(history_list)):
                    if history_list[i][0] == target_val:
                        cluster_idx = i
                        break

            if cluster_idx == -1:
                start_idx = max(0, (len(history_list) - 10) // 2)
            else:
                start_idx = max(0, cluster_idx - 4)

            end_idx = min(len(history_list), start_idx + 10)
            
            if end_idx - start_idx < 10 and len(history_list) >= 10:
                start_idx = max(0, end_idx - 10)
                
            selected_frames = history_list[start_idx:end_idx]
            
            for idx, (val, timestamp, img) in enumerate(selected_frames):
                frame_index = start_idx + idx
                idx_text = str(frame_index + 1).ljust(len(str(end_idx + 1)))
                line = f"`#{idx_text}` <t:{timestamp}:T> `{val}`"
                if img:
                    filename = f"frame_{frame_index}_{safe_milestone_filename_value(val)}.png"
                    file_templates.append(milestone_image_file_factory(img, filename))
                    gallery_items.append(
                        (
                            filename,
                            f"Frame {frame_index}: {val} at <t:{timestamp}:T>",
                        )
                    )
                    img_idx_text = str(len(file_templates)).ljust(2)
                    line += f" [image `{img_idx_text}`]"
                value_lines.append(line)

        notify_body = f"{content}\n\n" + "\n".join(value_lines) if value_lines else content

        def create_view() -> MilestoneMessageView:
            return MilestoneMessageView(
                title=milestone_name,
                body=notify_body,
                gallery_items=gallery_items,
            )

        if file_templates:
            await self.bot.notifications.notify(
                MILESTONE_USAGE_TYPE,
                create_view=create_view,
                create_files=lambda: list(map(lambda t: t(), file_templates)),
            )
            return

        await self.bot.notifications.notify(
            MILESTONE_USAGE_TYPE,
            create_view=create_view,
        )


async def setup(bot: BotCore):
    manage = groups.manage(bot)
    milestone_tracker = MilestoneTracker(bot)
    bot.milestones = milestone_tracker

    @manage.command(
        name="milestones",
        description="Manage milestone events.",
        capabilities=["milestones.manage"],
    )
    async def milestones(
        interaction: discord.Interaction,
        action: Literal["subscribe", "unsubscribe", "spoof", "ratelimit_reset"],
        name: str | None = None,
        data: str | None = None,
        min_count: int | None = None,
    ):
        name = name.strip() if name is not None else None
        data = data.strip() if data is not None else None

        if action == "ratelimit_reset":
            if name is not None or data is not None or min_count is not None:
                await bot.discord.send(
                    "`name`, `data`, and `min_count` are only used with the `spoof` action.",
                    response=True,
                )
                return

            milestone_tracker.reset_ratelimit()
            await bot.discord.send(
                "Milestone notification rate limit reset.",
                response=True,
            )
            return

        if action in ("subscribe", "unsubscribe"):
            if name is not None or data is not None or min_count is not None:
                await bot.discord.send(
                    "`name`, `data`, and `min_count` are only used with the `spoof` action.",
                    response=True,
                )
                return

            channel_id = interaction.channel_id

            if channel_id is None:
                await bot.discord.send(
                    "Could not determine this channel.",
                    response=True,
                )
                return

            if action == "unsubscribe":
                unsubscribed = await milestone_tracker.unsubscribe(channel_id)
                await bot.discord.send(
                    "This channel is no longer subscribed to milestone notifications."
                    if unsubscribed
                    else "This channel is not subscribed to milestone notifications.",
                    response=True,
                )
                return

            subscribed = await milestone_tracker.subscribe(channel_id)

            if not subscribed:
                await bot.discord.send(
                    "I cannot access this channel.",
                    response=True,
                )
                return

            await bot.discord.send(
                "This channel is now subscribed to milestone notifications.",
                response=True,
            )
            return

        assert action == "spoof"

        if name is None:
            await bot.discord.send(
                "Milestone name is required when spoofing.",
                response=True,
            )
            return

        if not name:
            await bot.discord.send(
                "Milestone name is required.",
                response=True,
            )
            return

        if data is None:
            if min_count is not None:
                await bot.discord.send(
                    "`min_count` is only used when spoofing milestone data.",
                    response=True,
                )
                return

            deleted = await milestone_tracker.delete(name)
            await bot.discord.send(
                f"Deleted `{name}`." if deleted else f"`{name}` does not exist.",
                response=True,
            )
            return

        if not data:
            await bot.discord.send(
                "Milestone data is required when spoofing.",
                response=True,
            )
            return

        changed_value = None
        spoof_timestamp = int(time.time())

        if min_count is not None and min_count < 1:
            await bot.discord.send(
                "`min_count` must be at least 1.",
                response=True,
            )
            return

        minimum_spoofs = min_count or MILESTONE_WINDOW_SIZE
        spoof_count = min(minimum_spoofs, MILESTONE_WINDOW_SIZE)

        for i in range(MILESTONE_WINDOW_SIZE):
            changed_value = await milestone_tracker.update(name, data, timestamp=spoof_timestamp) or changed_value
            if changed_value and i + 1 >= spoof_count:
                break

        if changed_value is None:
            await bot.discord.send(
                f"`{name}` is already `{data}`.",
                response=True,
            )
            return

        await bot.discord.send(
            f"Set `{name}` to `{changed_value}`.",
            response=True,
        )

    @milestones.autocomplete("name")
    async def milestone_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await milestone_name_choices(current)

    @bot.setup.command(
        name="milestone_info",
        description="Show milestone history",
        defer=False
    )
    @action(
        "milestone_info",
        "Show milestone history.",
        params={
            "milestone_name": AIParam(),
            "ephemeral": AIParam(type=bool, required=False, default=True),
        },
    )
    async def milestone_info(
        interaction: discord.Interaction, milestone_name: str, ephemeral: bool = True
    ):
        milestone_name = milestone_name.strip()

        if not milestone_name:
            await bot.discord.send(
                "Milestone name is required.",
                response=True, ephemeral=True
            )
            return
        await bot.discord.defer(ephemeral=ephemeral)

        history = milestone_tracker.history.get(milestone_name)
        history_count = len(history) if history else 0
        current_value = await milestone_tracker.get(milestone_name)
        
        file_templates: list[Callable[[], discord.File]] = []
        gallery_items: list[tuple[str, str]] = []
        history_lines: list[str] = []
        if history:
            history_list = list(history)
            start_idx = max(0, len(history_list) - 10)
            
            for idx, (val, timestamp, img) in enumerate(history_list):
                idx_text = str(idx + 1).ljust(len(str(len(history_list))))
                line = f"`#{idx_text}` <t:{timestamp}:T> `{val}`"
                if idx >= start_idx and img:
                    filename = f"frame_{idx}_{safe_milestone_filename_value(val)}.png"
                    file_templates.append(milestone_image_file_factory(img, filename))
                    gallery_items.append((filename, f"Frame {idx}: {val} at <t:{timestamp}:T>"))
                    img_idx_text = str(len(file_templates)).ljust(2)
                    line += f" [image `{img_idx_text}`]"
                history_lines.append(line)
        
        history_text = "\n".join(history_lines) if history_lines else "(empty)"
        body = (
            f"Current value: `{current_value or 'None'}`\n"
            f"History items: `{history_count}`\n\n"
            f"{history_text}"
        )
        files = [template() for template in file_templates]
        view = MilestoneMessageView(
            title=milestone_name,
            body=body,
            gallery_items=gallery_items,
        )
        await bot.discord.send(
            response=True,
            ephemeral=ephemeral,
            files=files,
            view=view,
        )

    @milestone_info.autocomplete("milestone_name")
    async def milestone_info_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return await milestone_name_choices(current)

    async def milestone_name_choices(current: str) -> list[app_commands.Choice[str]]:
        current = current.strip().lower()
        choices = []

        for name in sorted(await milestone_tracker.names(), key=str.casefold):
            if current and not name.lower().startswith(current):
                continue

            choices.append(app_commands.Choice(name=name, value=name))

            if len(choices) >= 25:
                break

        return choices

    @bot.init_callback
    async def init():
        await bot.notifications.wait_until_ready()
