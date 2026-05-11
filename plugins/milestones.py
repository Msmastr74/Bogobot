from collections import Counter, defaultdict, deque
from string import Template
from typing import Literal, TYPE_CHECKING
import io
import time

import discord
from PIL import Image

if TYPE_CHECKING:
    from main import BotCore


MILESTONE_USAGE_TYPE = "milestones"
MILESTONE_WINDOW_SIZE = 60
DEFAULT_MILESTONE_INITIALIZE_FORMAT = "$milestone_name initialized to `$new_value`."
DEFAULT_MILESTONE_UPDATE_FORMAT = "$milestone_name updated from `$old_value` to `$new_value`."


class MilestoneTracker:
    def __init__(self, bot: "BotCore"):
        self.bot = bot
        self.history: defaultdict[str, deque[tuple[str, int, Image.Image | None]]] = defaultdict(
            lambda: deque(maxlen=MILESTONE_WINDOW_SIZE)
        )

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

    def _get_stable_value(self, milestone_name: str) -> str | None:
        """
        Returns the mode of the last 60 updates.

        Until there are exactly 60 collected updates, this returns None.

        If there is a tie for mode, this returns None because the stable value
        is ambiguous.
        """

        history = self.history[milestone_name]

        if len(history) < MILESTONE_WINDOW_SIZE:
            return None

        counts = Counter(val for val, timestamp, img in history)
        most_common = counts.most_common()

        if not most_common:
            return None

        if len(most_common) >= 2 and most_common[0][1] == most_common[1][1]:
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

        files: list[discord.File] = []
        attached_values: list[str] = []
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
                if img:
                    b = io.BytesIO()
                    img.save(b, format="PNG")
                    b.seek(0)
                    safe_val = "".join(c for c in val if c.isalnum() or c in (' ', '_', '-')).rstrip()
                    files.append(discord.File(b, filename=f"frame_{start_idx + idx}_{safe_val}.png"))
                    attached_values.append(f"{start_idx + idx}: <t:{timestamp}:T> `{val}`")

        if files:
            await self.bot.notifications.notify(
                MILESTONE_USAGE_TYPE,
                content=f"{content}\n" + "\n".join(attached_values),
                files=files
            )
            return

        await self.bot.notifications.notify(
            MILESTONE_USAGE_TYPE,
            content=content
        )


async def setup(bot: "BotCore"):
    from utils import groups

    manage = groups.manage(bot)
    milestones = MilestoneTracker(bot)
    bot.milestones = milestones

    @manage.command(
        name="milestone",
        description="Subscribe, unsubscribe, or spoof/delete a milestone",
    )
    async def milestone(
        interaction: discord.Interaction,
        action: Literal["subscribe", "unsubscribe", "spoof"],
        name: str | None = None,
        data: str | None = None,
    ):
        name = name.strip() if name is not None else None
        data = data.strip() if data is not None else None

        if action in ("subscribe", "unsubscribe"):
            if name is not None or data is not None:
                await bot.discord.send(
                    "`name` and `data` are only used with the `spoof` action.",
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
                unsubscribed = await milestones.unsubscribe(channel_id)
                await bot.discord.send(
                    "This channel is no longer subscribed to milestone notifications."
                    if unsubscribed
                    else "This channel is not subscribed to milestone notifications.",
                    response=True,
                )
                return

            subscribed = await milestones.subscribe(channel_id)

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
            deleted = await milestones.delete(name)
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

        for _ in range(MILESTONE_WINDOW_SIZE):
            changed_value = await milestones.update(name, data, timestamp=spoof_timestamp) or changed_value

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

    @milestone.autocomplete("name")
    async def milestone_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[discord.app_commands.Choice[str]]:
        return await milestone_name_choices(current)

    @bot.setup.command(
        name="milestone_info",
        description="Show recent in-memory history for a milestone",
        defer=False
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
        await interaction.response.defer(ephemeral=ephemeral)

        history = milestones.history.get(milestone_name)
        history_items = [f"{idx}: {val} @ {timestamp}" for idx, (val, timestamp, img) in enumerate(history)] if history else []
        history_text = "\n".join(history_items) if history_items else "(empty)"
        current_value = await milestones.get(milestone_name)
        
        files: list[discord.File] = []
        if history:
            history_list = list(history)
            start_idx = max(0, len(history_list) - 10)
            selected_frames = history_list[start_idx:]
            
            for idx, (val, timestamp, img) in enumerate(selected_frames):
                if img:
                    b = io.BytesIO()
                    img.save(b, format="PNG")
                    b.seek(0)
                    safe_val = "".join(c for c in val if c.isalnum() or c in (' ', '_', '-')).rstrip()
                    files.append(discord.File(b, filename=f"frame_{start_idx + idx}_{safe_val}.png"))
        
        kwargs = { 'files': files } if files else {}
        await bot.discord.send(
            f"{milestone_name} current value: `{current_value or 'None'}`\n"
            f"History items: `{len(history_items)}`\n"
            f"```\n{history_text}\n```",
            response=True,
            ephemeral=ephemeral,
            **kwargs
        )

    @milestone_info.autocomplete("milestone_name")
    async def milestone_info_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[discord.app_commands.Choice[str]]:
        return await milestone_name_choices(current)

    async def milestone_name_choices(current: str) -> list[discord.app_commands.Choice[str]]:
        current = current.strip().lower()
        choices = []

        for name in sorted(await milestones.names(), key=str.casefold):
            if current and not name.lower().startswith(current):
                continue

            choices.append(discord.app_commands.Choice(name=name, value=name))

            if len(choices) >= 25:
                break

        return choices

    @bot.init_callback
    async def init():
        await bot.notifications.wait_until_ready()
