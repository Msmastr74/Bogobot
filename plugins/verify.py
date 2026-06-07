import time
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import cast

import discord

from bogobot_core import BotCore
from utils import groups
from utils.captcha import CaptchaChallenge, OcclusionPathCaptchaGenerator, is_expired, verify_answer


VERIFIED_ROLE_NAME = "Verified"
QUARANTINED_ROLE_NAME = "Quarantined"
VERIFY_COOLDOWN_SECONDS = 60.0
VERIFY_CAPTCHA_TIMEOUT_SECONDS = 120.0
VERIFY_BUTTON_CUSTOM_ID = "bogobot:verify"


@dataclass
class VerifyCooldown:
    expires_at: float
    duration: float


@dataclass
class VerifyCaptchaSession:
    challenge: CaptchaChallenge
    attempts: int = 0


def _find_role(member: discord.Member, name: str) -> discord.Role | None:
    return discord.utils.get(member.roles, name=name)


def _has_role(member: discord.Member, name: str) -> bool:
    return _find_role(member, name) is not None


async def _get_or_create_verified_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)
    if role is not None:
        return role
    return await guild.create_role(
        name=VERIFIED_ROLE_NAME,
        reason="Bogobot verification role setup",
    )


class VerifyPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        cooldowns: MutableMapping[int, VerifyCooldown],
        generator: OcclusionPathCaptchaGenerator,
    ) -> None:
        super().__init__(timeout=None)
        self.cooldowns = cooldowns
        self.generator = generator

        verify_button = discord.ui.Button(
            label="Verify",
            style=discord.ButtonStyle.primary,
            custom_id=VERIFY_BUTTON_CUSTOM_ID,
        )
        verify_button.callback = self.verify

        self.add_item(discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay("## Verification"),
                discord.ui.TextDisplay(
                    "Click the button and answer the captcha to receive the "
                    f"`{VERIFIED_ROLE_NAME}` role."
                ),
                accessory=verify_button,
            )
        ))

    async def verify(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Verification only works inside a server.",
                ephemeral=True,
            )
            return

        blocked_message = self._blocked_message(member)
        if blocked_message is not None:
            await interaction.response.send_message(blocked_message, ephemeral=True)
            return

        now = time.monotonic()
        cooldown = self.cooldowns.get(member.id)
        if cooldown is not None and cooldown.expires_at > now:
            remaining = int(cooldown.expires_at - now + 0.999)
            await interaction.response.send_message(
                f"Please wait {remaining}s before trying verification again.",
                ephemeral=True,
            )
            return

        cooldown_duration = (
            cooldown.duration * 2
            if cooldown is not None else
            VERIFY_COOLDOWN_SECONDS
        )
        self.cooldowns[member.id] = VerifyCooldown(
            expires_at=now + cooldown_duration,
            duration=cooldown_duration,
        )
        challenge = self.generator.generate()
        captcha_file = discord.File(
            challenge.image_path,
            filename=f"verify_captcha_{challenge.challenge_id}.png",
        )
        await interaction.response.send_message(
            view=VerifyCaptchaView(
                cooldowns=self.cooldowns,
                session=VerifyCaptchaSession(challenge=challenge),
            ),
            file=captcha_file,
            ephemeral=True,
        )

    def _blocked_message(self, member: discord.Member) -> str | None:
        if _has_role(member, VERIFIED_ROLE_NAME):
            return "You are already verified."
        if _has_role(member, QUARANTINED_ROLE_NAME):
            return "You cannot verify while quarantined."
        return None


class VerifyCaptchaView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        cooldowns: MutableMapping[int, VerifyCooldown],
        session: VerifyCaptchaSession,
    ) -> None:
        super().__init__(timeout=None)
        self.cooldowns = cooldowns
        self.session = session

        answer_button = discord.ui.Button(
            label="Answer",
            style=discord.ButtonStyle.primary,
        )
        answer_button.callback = self.answer

        self.add_item(discord.ui.Container(
            discord.ui.Section(
                discord.ui.TextDisplay("## Verification Captcha"),
                discord.ui.TextDisplay(session.challenge.prompt),
                accessory=answer_button,
            ),
            discord.ui.Separator(),
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    media=f"attachment://verify_captcha_{session.challenge.challenge_id}.png",
                    description="Verification captcha",
                )
            ),
        ))

    async def answer(self, interaction: discord.Interaction) -> None:
        if is_expired(self.session.challenge):
            await interaction.response.send_message(
                "That captcha expired. Please try verification again.",
                ephemeral=True,
            )
            return

        if self.session.attempts >= self.session.challenge.max_attempts:
            await interaction.response.send_message(
                "That captcha has no attempts left. Please try verification again later.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            VerifyCaptchaModal(
                cooldowns=self.cooldowns,
                session=self.session,
            )
        )


class VerifyCaptchaModal(discord.ui.Modal, title="Verification Captcha"):
    def __init__(
        self,
        *,
        cooldowns: MutableMapping[int, VerifyCooldown],
        session: VerifyCaptchaSession,
    ) -> None:
        super().__init__()
        self.cooldowns = cooldowns
        self.session = session
        self.answer = discord.ui.TextInput(
            required=True,
            max_length=32,
            placeholder="A, B, C, D, E, or F",
        )
        self.add_item(discord.ui.Label(text="Exit letter", component=self.answer))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Verification only works inside a server.",
                ephemeral=True,
            )
            return

        if _has_role(member, VERIFIED_ROLE_NAME):
            await interaction.response.send_message(
                "You are already verified.",
                ephemeral=True,
            )
            return
        if _has_role(member, QUARANTINED_ROLE_NAME):
            await interaction.response.send_message(
                "You cannot verify while quarantined.",
                ephemeral=True,
            )
            return

        if is_expired(self.session.challenge):
            await interaction.response.send_message(
                "That captcha expired. Please try verification again.",
                ephemeral=True,
            )
            return

        self.session.attempts += 1
        if not verify_answer(self.session.challenge, self.answer.value):
            await interaction.response.send_message(
                "That captcha answer was not correct. Please try verification again after the cooldown.",
                ephemeral=True,
            )
            return

        try:
            verified_role = await _get_or_create_verified_role(guild)
            await member.add_roles(
                verified_role,
                reason="Bogobot verification captcha passed",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I cannot create or assign the `{VERIFIED_ROLE_NAME}` role.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "Verification failed while assigning the role.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"You are now verified with the `{VERIFIED_ROLE_NAME}` role.",
            ephemeral=True,
        )
        self.cooldowns.pop(member.id, None)


async def setup(bot: BotCore) -> None:
    cooldowns: dict[int, VerifyCooldown] = {}
    generator = OcclusionPathCaptchaGenerator(
        expires_in_seconds=int(VERIFY_CAPTCHA_TIMEOUT_SECONDS),
    )
    manage = groups.manage(bot)
    bot.add_view(VerifyPanelView(cooldowns=cooldowns, generator=generator))

    @manage.command(
        name="create_verification",
        description="Create a persistent verification message",
        perm_requirement=2,
    )
    async def create_verification(interaction: discord.Interaction) -> None:
        if not hasattr(interaction.channel, "send"):
            await bot.discord.send(
                "The bot cannot send messages in this channel!",
                ephemeral=True,
                response=True
            )
            return
        channel = cast('discord.abc.MessageableChannel', interaction.channel)
        await channel.send(
            view=VerifyPanelView(cooldowns=cooldowns, generator=generator),
        )
        await bot.discord.send(
            "Successfully sent a persistent verification prompt.",
            ephemeral=True,
            response=True
        )
