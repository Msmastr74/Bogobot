from collections import Counter, deque
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from main import BotCore


MILESTONE_USAGE_TYPE = "milestones"
MILESTONE_WINDOW_SIZE = 60


class MilestoneTracker:
    def __init__(self, bot: "BotCore"):
        self.bot = bot
        self.history: deque[str] = deque(maxlen=MILESTONE_WINDOW_SIZE)

    def _get_state(self) -> dict:
        state = self.bot.config.get("milestones")

        if not isinstance(state, dict):
            state = {}
            self.bot.config["milestones"] = state
            self.bot.save_config()

        if "current_value" in state and not isinstance(state["current_value"], str):
            state["current_value"] = None

        return state

    def _get_current_value(self) -> str | None:
        state = self._get_state()
        value = state.get("current_value")

        if isinstance(value, str):
            return value

        return None

    def _set_current_value(self, value: str) -> None:
        state = self._get_state()
        state["current_value"] = value
        self.bot.config["milestones"] = state
        self.bot.save_config()

    def _get_subscriptions(self) -> dict[str, bool]:
        subscriptions = self.bot.config.get("milestone_channels")

        if not isinstance(subscriptions, dict):
            subscriptions = {}
            self.bot.config["milestone_channels"] = subscriptions
            self.bot.save_config()

        normalized: dict[str, bool] = {}

        for channel_id_str, enabled in subscriptions.items():
            try:
                channel_id_str = str(int(channel_id_str))
            except (TypeError, ValueError):
                continue

            if enabled:
                normalized[channel_id_str] = True

        if normalized != subscriptions:
            self.bot.config["milestone_channels"] = normalized
            self.bot.save_config()

        return normalized

    def _save_subscriptions(self, subscriptions: dict[str, bool]) -> None:
        self.bot.config["milestone_channels"] = subscriptions
        self.bot.save_config()

    async def reconcile_channels(self) -> None:
        """
        Ensure every subscribed milestone channel has a ChannelProxy.

        Removes stale subscriptions if the channel is unavailable.
        """

        subscriptions = self._get_subscriptions()
        stale_channel_ids: list[str] = []

        for channel_id_str in subscriptions:
            try:
                channel_id = int(channel_id_str)
            except ValueError:
                stale_channel_ids.append(channel_id_str)
                continue

            proxy = self.bot.channels.get(channel_id)

            if proxy is None:
                proxy = await self.bot.channels.add_channel(
                    MILESTONE_USAGE_TYPE,
                    channel_id,
                )

            if proxy is None:
                stale_channel_ids.append(channel_id_str)

        if stale_channel_ids:
            subscriptions = self._get_subscriptions()

            for channel_id_str in stale_channel_ids:
                subscriptions.pop(channel_id_str, None)

                try:
                    await self.bot.channels.remove_channel(
                        MILESTONE_USAGE_TYPE,
                        int(channel_id_str),
                    )
                except ValueError:
                    pass

            self._save_subscriptions(subscriptions)

    async def subscribe(self, channel_id: int) -> bool:
        proxy = await self.bot.channels.add_channel(
            MILESTONE_USAGE_TYPE,
            channel_id,
        )

        if proxy is None:
            return False

        subscriptions = self._get_subscriptions()
        subscriptions[str(channel_id)] = True
        self._save_subscriptions(subscriptions)

        return True

    async def unsubscribe(self, channel_id: int) -> bool:
        subscriptions = self._get_subscriptions()
        channel_id_str = str(channel_id)

        if channel_id_str not in subscriptions:
            return False

        subscriptions.pop(channel_id_str, None)
        self._save_subscriptions(subscriptions)

        await self.bot.channels.remove_channel(
            MILESTONE_USAGE_TYPE,
            channel_id,
        )

        return True

    def _get_stable_value(self) -> str | None:
        """
        Returns the mode of the last 60 updates.

        Until there are exactly 60 collected updates, this returns None.

        If there is a tie for mode, this returns None because the stable value
        is ambiguous.
        """

        if len(self.history) < MILESTONE_WINDOW_SIZE:
            return None

        counts = Counter(self.history)
        most_common = counts.most_common()

        if not most_common:
            return None

        if len(most_common) >= 2 and most_common[0][1] == most_common[1][1]:
            return None

        return most_common[0][0]

    async def update(self, best_run: str) -> str | None:
        """
        Called by your update stats function.

        Example:
            await bot.milestones.update("11/25")

        The input best_run is considered one update.

        Nothing happens until 60 updates have been collected in memory. After
        that, the mode of the last 60 updates is considered the stable value.

        If the stable value changes from the previously confirmed value, all
        subscribed milestone channels are notified.

        Returns the newly confirmed value if it changed, otherwise None.
        """

        best_run = best_run.strip()

        if not best_run:
            return None

        self.history.append(best_run)

        stable_value = self._get_stable_value()

        if stable_value is None:
            return None

        current_value = self._get_current_value()

        if stable_value == current_value:
            return None

        self._set_current_value(stable_value)

        await self.notify_milestone_change(
            old_value=current_value,
            new_value=stable_value,
        )

        return stable_value

    async def notify_milestone_change(
        self,
        *,
        old_value: str | None,
        new_value: str,
    ) -> None:
        subscriptions = self._get_subscriptions()
        stale_channel_ids: list[str] = []

        if old_value is None:
            content = f"Milestone initialized: `{new_value}`"
        else:
            content = f"Milestone updated: `{old_value}` → `{new_value}`"

        for channel_id_str in list(subscriptions.keys()):
            try:
                channel_id = int(channel_id_str)
            except ValueError:
                stale_channel_ids.append(channel_id_str)
                continue

            proxy = self.bot.channels.get(channel_id)

            if proxy is None:
                proxy = await self.bot.channels.add_channel(
                    MILESTONE_USAGE_TYPE,
                    channel_id,
                )

            if proxy is None:
                stale_channel_ids.append(channel_id_str)
                continue

            await proxy.send(
                content=content,
                wait=False,
            )

        if stale_channel_ids:
            subscriptions = self._get_subscriptions()

            for channel_id_str in stale_channel_ids:
                subscriptions.pop(channel_id_str, None)

                try:
                    await self.bot.channels.remove_channel(
                        MILESTONE_USAGE_TYPE,
                        int(channel_id_str),
                    )
                except ValueError:
                    pass

            self._save_subscriptions(subscriptions)


async def setup(bot: "BotCore"):
    milestones = MilestoneTracker(bot)
    bot.milestones = milestones

    @bot.setup.command(
        name="subscribe_milestones",
        description="Subscribe this channel to milestone notifications",
    )
    async def subscribe_milestones(interaction: discord.Interaction):
        channel_id = interaction.channel_id

        if channel_id is None:
            await bot.discord.messages.send(
                "Could not determine this channel.",
                response=True,
            )
            return

        subscribed = await milestones.subscribe(channel_id)

        if not subscribed:
            await bot.discord.messages.send(
                "I cannot access this channel.",
                response=True,
            )
            return

        await bot.discord.messages.send(
            "This channel is now subscribed to milestone notifications.",
            response=True,
        )

    @bot.setup.command(
        name="unsubscribe_milestones",
        description="Unsubscribe this channel from milestone notifications",
    )
    async def unsubscribe_milestones(interaction: discord.Interaction):
        channel_id = interaction.channel_id

        if channel_id is None:
            await bot.discord.messages.send(
                "Could not determine this channel.",
                response=True,
            )
            return

        unsubscribed = await milestones.unsubscribe(channel_id)

        if not unsubscribed:
            await bot.discord.messages.send(
                "This channel is not subscribed to milestone notifications.",
                response=True,
            )
            return

        await bot.discord.messages.send(
            "This channel is no longer subscribed to milestone notifications.",
            response=True,
        )

    @bot.init_callback
    async def init():
        await bot.channels.wait_until_ready()
        await milestones.reconcile_channels()
