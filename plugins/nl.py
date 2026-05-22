import discord

from typing import TypedDict, TYPE_CHECKING
if TYPE_CHECKING:
    from bogobot_core import BotCore
from dataclasses import dataclass

class BotActionParameters(TypedDict, total=False):
    perm_requirement: int

@dataclass(frozen=True, slots=True)
class BotActionContext:
    message: discord.Message
    text: str
    name: str
    score: float

    async def reply(self, *args, **kwargs) -> discord.Message:
        kwargs.setdefault("mention_author", False)
        return await self.message.reply(*args, **kwargs)

def mentioned_message_text(bot: 'BotCore', message: discord.Message) -> str | None:
    if bot.user is None or bot.user not in message.mentions:
        return None

    text = message.content
    for mention in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        text = text.replace(mention, " ")

    text = " ".join(text.split())
    return text or None


async def setup(bot: 'BotCore'):
    from utils.nl import nl, action

    @bot.message_callback
    async def on_message(message: discord.Message):
        if message.author.bot or bot.user is None:
            return

        text = mentioned_message_text(bot, message)
        if text is None:
            return

        match = await nl.match_info(text)
        if match is None:
            return

        if not bot.is_authorized(
            message.author.id,
            match.context.get("perm_requirement", 0),
        ):
            await message.reply(
                "❌ Unauthorized.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            await match.action(BotActionContext(
                message=message,
                text=text,
                name=match.name,
                score=match.score,
            ))
        except Exception:
            bot.logger.exception(
                "NL mention action %s failed for message %s",
                match.name,
                message.id,
            )
            await message.reply(
                "⚠️ That action failed.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    
    @action("ping", "Ping!")
    async def ping(ctx: BotActionContext):
        await ctx.reply("Pong!")
