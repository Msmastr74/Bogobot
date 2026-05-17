import asyncio
import discord
from discord import app_commands

from utils.transformers import ColourTransformer
from bogobot_core import BotCore, current_interaction

class AvatarView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        user: discord.Member | discord.User
    ):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.TextDisplay(f"## {user.mention}'s Avatar")
        )
        self.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    media=user.display_avatar.url,
                    description=f"{user.display_name}'s avatar",
                )
            )
        )

class PingView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        interaction_latency: float,
        gateway_latency: float,
        user_latency: tuple[str, float] | None = None,
    ):
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay("## Pong!"))

        self.add_item(
            self._latency_container(
                "Interaction latency",
                interaction_latency,
            )
        )
        self.add_item(
            self._latency_container(
                "Gateway latency",
                gateway_latency,
            )
        )

        if user_latency is not None:
            label, latency = user_latency
            self.add_item(self._latency_container(label, latency))

    def _latency_container(
        self,
        label: str,
        latency: float,
    ) -> discord.ui.Container:
        return discord.ui.Container(
            discord.ui.TextDisplay(f"**{label}**\n{latency:.2f} ms"),
            accent_colour=self._latency_color(latency),
        )

    def _latency_color(self, latency: float) -> discord.Colour:
        stops = [
            (-50.0, discord.Colour.dark_magenta()),
            (0.0, discord.Colour.brand_green()),
            (50.0, discord.Colour.green()),
            (200.0, discord.Colour.blue()),
            (500.0, discord.Colour.orange()),
            (800.0, discord.Colour.red()),
        ]

        if latency <= stops[0][0]:
            return stops[0][1]

        for (start_value, start_color), (end_value, end_color) in zip(stops, stops[1:]):
            if latency <= end_value:
                ratio = (latency - start_value) / (end_value - start_value)
                return self._mix_color(start_color, end_color, ratio)

        return stops[-1][1]

    def _mix_color(
        self,
        start: discord.Colour,
        end: discord.Colour,
        ratio: float,
    ) -> discord.Colour:
        ratio = max(0.0, min(1.0, ratio))
        start_rgb = start.to_rgb()
        end_rgb = end.to_rgb()
        return discord.Colour.from_rgb(*[
            round(start_channel + (end_channel - start_channel) * ratio)
            for start_channel, end_channel in zip(start_rgb, end_rgb)
        ])

class AnnounceView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        title: str | None,
        message: str | None,
        message_container: bool,
        accent_colour: discord.Colour | None = None,
    ):
        super().__init__(timeout=None)
        if title is not None:
            self.add_item(discord.ui.TextDisplay(f"{title}"))
        if not message:
            return
        if message_container:
            self.add_item(discord.ui.Container(
                discord.ui.TextDisplay(message),
                accent_colour=accent_colour,
            ))
        else:
            self.add_item(discord.ui.TextDisplay(message))

async def setup(bot: BotCore):
    from utils import groups
    manage = groups.manage(bot)

    @bot.setup.command(name="avatar", description="Get the avatar of a user", eph=False, perm_requirement=0)
    async def avatar(interaction: discord.Interaction, user: discord.Member | discord.User | None = None) -> None:
        if user is None:
            user = interaction.user

        await bot.discord.send(
            view=AvatarView(user=user),
            response=True,
        )
    
    @bot.setup.command(name="ping", description="Ping pong", defer=False, perm_requirement=0)
    async def ping(
        interaction: discord.Interaction,
        user: discord.User | discord.Member | None = None
    ):
        now = discord.utils.utcnow() 
        interaction_latency = (now - interaction.created_at).total_seconds() * 1000
        gateway_latency = bot.latency * 1000

        message = await bot.discord.send(
            view=PingView(
                interaction_latency=interaction_latency,
                gateway_latency=gateway_latency,
            ),
            response=True,
            allowed_mentions=discord.AllowedMentions.none()
        )
        if message is None:
            return
        await message.add_reaction("🏓")
        
        user = user or interaction.user
        try:
            user_msg = await bot.wait_for(
                "message",
                check=lambda m: m.author.id == user.id and m.channel.id == interaction.channel_id,
                timeout=60
            )
        except asyncio.TimeoutError:
            return
        if user_msg.nonce is not None:
            try:
                nonce = int(user_msg.nonce)
            except ValueError:
                return
            user_client_time = discord.utils.snowflake_time(nonce)
            msg_created_at = user_msg.created_at
            user_latency = (msg_created_at - user_client_time).total_seconds() * 1000
            await message.edit(
                view=PingView(
                    interaction_latency=interaction_latency,
                    gateway_latency=gateway_latency,
                    user_latency=(
                        f"{user.mention}'s ping",
                        user_latency,
                    ),
                ),
                allowed_mentions=discord.AllowedMentions.none()
            )

    class AnnounceModal(discord.ui.Modal, title="Announcement Message Contents"):
        message = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.long,
            required=False,
            placeholder="Type a message..."
        )
        def __init__(
            self,
            *,
            title: str | None,
            message_container: bool,
            accent_colour: discord.Colour | None
        ):
            super().__init__()
            self.message_title = title
            self.message_container = message_container
            self.accent_colour = accent_colour

        async def on_submit(self, interaction: discord.Interaction) -> None:
            token = current_interaction.set(interaction)
            try:
                if not self.message.value and not self.message_title:
                    await bot.discord.send(
                        contents="The message cannot be empty.",
                        response=True,
                        ephemeral=True
                    )
                    return
                view=AnnounceView(
                    title=self.message_title,
                    message=self.message.value or None,
                    message_container=self.message_container,
                    accent_colour=self.accent_colour,
                )
                try:
                    await bot.discord.send(view=view)
                except discord.Forbidden:
                    await bot.discord.send(view=view, response=True)
                await bot.discord.send(
                    contents="The announcement message was successfully sent.",
                    response=True,
                    ephemeral=True
                )
            finally:
                current_interaction.reset(token)
    
    @manage.command(
        name='announce',
        description='Send a message through the bot.',
        perm_requirement=2,
        defer=False
    )
    async def announce(
        interaction: discord.Interaction,
        title: str | None = None,
        message: str | None = None,
        message_container: bool = False,
        accent_colour: app_commands.Transform[discord.Colour, ColourTransformer] | None = None
    ):
        if message is None:
            await interaction.response.send_modal(
                AnnounceModal(
                    title=title,
                    message_container=message_container,
                    accent_colour=accent_colour
                )
            )
            return
        if not message and not title:
            await bot.discord.send(
                contents="The message cannot be empty.",
                response=True,
                ephemeral=True
            )
            return
        view = AnnounceView(
            title=title,
            message=message,
            message_container=message_container,
            accent_colour=accent_colour,
        )
        try:
            await bot.discord.send(view=view)
        except discord.Forbidden:
            await bot.discord.send(view=view, response=True)
        await bot.discord.send(
            contents="The announcement message was successfully sent.",
            response=True,
            ephemeral=True
        )
