import discord
from utils import tasks
import random
import re
import time
from typing import Sequence

from bogobot_core import BotCore
from utils import groups
from utils.nl import action

class BogonameView(discord.ui.LayoutView):
    def __init__(self, original_name: str, new_name: str) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.TextDisplay('## Bogoname'))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"**Original Name**\n{discord.utils.escape_markdown(original_name)}"),
            discord.ui.TextDisplay(f"**Bogoed name**\n{discord.utils.escape_markdown(new_name)}"),
            accent_colour=discord.Colour.random()
        ))
        self.add_item(discord.ui.TextDisplay(f"-# <t:{int(time.time())}:f>"))

EMOJI_ALIAS_RE = re.compile(r":([A-Za-z0-9_]+):")

async def setup(bot: BotCore):
    bogo = groups.bogo(bot)

    def casual_response(words: Sequence[str]) -> str:
        text = random.choice(words)
        if random.choice([True, False]):
            text = text.capitalize()
        punctuation = random.choice(["", "", "!", ".", "!!"])
        return f"{text}{punctuation}"

    def resolve_guild_emojis(
        text: str,
        guild: discord.Guild | None,
    ) -> str:
        if guild is None:
            return text

        def replace(match: re.Match[str]) -> str:
            emoji = discord.utils.get(guild.emojis, name=match[1])
            if emoji is None:
                return match[0]
            return str(emoji)

        return EMOJI_ALIAS_RE.sub(replace, text)

    async def send_fun_text(
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        await bot.discord.send(
            resolve_guild_emojis(text, interaction.guild),
            response=True,
        )

    conversational_actions: dict[tuple[str, ...], tuple[str, ...]] = {
        ("hello", "hi", "hey"): ("hi", "hello", "hey"),
        ("bye", "goodbye", "see you"): ("bye", "goodbye", "see you"),
        ("you can talk", "can you talk", "can you speak"): ("yes", "yep", "i can"),
        ("thank you", "thanks", "thank you bot"): ("you're welcome", "no problem", "anytime"),
        ("good bot", "nice bot", "great bot"): ("thanks", "thank you", "i try"),
        ("bad bot", "mean bot", "rude bot"): ("sorry", "oops", "i'll do better"),
        ("how are you", "how are you doing", "you okay"): ("good", "doing fine", "pretty good"),
        ("what are you", "who are you", "what is this bot"): ("bogobot", "i'm bogobot", "bot, mostly"),
        ("are you alive", "are you awake", "you there"): ("yes", "i'm here", "awake enough"),
        ("good morning", "morning", "gm"): ("good morning", "morning", "gm"),
        ("good night", "night", "gn"): ("good night", "night", "sleep well"),
        ("lol", "haha", "that is funny"): ("lol", "heh", "nice"),
        ("wow", "whoa", "amazing"): ("wow", "whoa", "neat"),
        ("sorry", "my bad", "oops sorry"): ("it's okay", "no worries", "all good"),
        ("help", "what can you do", "commands"): ("try stats", "try ping", "ask for the leaderboard"),
        ("what is bogosort", "explain bogosort", "bogosort"): ("random sorting", "shuffle until sorted", "chaos sorting"),
        ("love you", "i love you", "ily"): ("aw", "thanks", "likewise"),
        ("are you real", "are you sentient", "are you human"): ("not human", "bot-shaped", "real enough"),
        ("sing", "sing a song", "can you sing"): ("la la", "maybe later", "not well"),
        ("dance", "do a dance", "can you dance"): ("shuffle shuffle", "sort of", "imagine it"),
        ("favorite number", "pick a favorite number", "best number"): ("42", "7", "100"),
        ("favorite color", "best color", "what color do you like"): ("green", "blurple", "sort green"),
        ("tell me something", "say something", "talk to me"): ("something", "bogosort persists", "numbers are moving"),
        ("be quiet", "shush", "stop talking"): ("ok", "quiet mode", "shh"),
        ("wake up", "wake up bot", "rise and shine"): ("awake", "i'm up", "ready"),
    }
    literal_actions: dict[tuple[str, ...], tuple[str, ...]] = {
        (":steamhappy:", ":steamhappybutiaddeditwrong:"): (":steamhappybutiaddeditwrong:",),
        (":steamsadbutialsoaddeditwrong:", ":steamsad:"): (":steamsadbutialsoaddeditwrong:",)
    }

    for phrases, responses in conversational_actions.items():
        @action(phrases[0], *phrases)
        async def conversational_reply(
            interaction: discord.Interaction,
            responses: tuple[str, ...] = responses,
        ):
            await send_fun_text(interaction, casual_response(responses))
    
    for phrases, responses in literal_actions.items():
        @action(phrases[0], *phrases)
        async def literal_reply(
            interaction: discord.Interaction,
            responses: tuple[str, ...] = responses,
        ):
            await send_fun_text(interaction, random.choice(responses))

    @tasks.loop(seconds=15)
    async def update_status():
        if not bot.user:
            return
        text = bot.user.name
        tlist = list(text)
        chance = random.random()
        if chance < 0.1:
            pass # unshuffled
        elif chance < 0.4 and tlist.count('-') > 0:
            left = tlist[0:tlist.index('-')]
            right = tlist[tlist.index('-')+1:]
            random.shuffle(left)
            random.shuffle(right)
            tlist = left + ['-'] + right
        elif chance < 0.4 and text == 'Bogobot':
            left = tlist[0:4]
            right = tlist[4:]
            random.shuffle(left)
            random.shuffle(right)
            tlist = left + right
        else:
            random.shuffle(tlist)
        shuffled_text = ''.join(tlist)
        if bot.is_closed():
            return
        await bot.change_presence(activity=discord.CustomActivity(name=shuffled_text))
    
    @bot.init_callback
    async def init():
        if not update_status.is_running():
            update_status.start()
    
    @bot.close_callback
    async def close():
        if update_status.is_running():
            update_status.cancel()

    @bogo.command(name="name", description="Bogoes your name", perm_requirement=0, defer=False)
    @action(
        "bogo name",
        "bogo name",
        "shuffle my name",
        "scramble my name",
        "bogo my name",
        "bogofy my name",
    )
    async def bogo_name(interaction: discord.Interaction):
        member = interaction.user
        if isinstance(member, discord.User):
            await bot.discord.send(
                "You must be in a guild to use this command!",
                response=True,
                ephemeral=True,
            )
            return
        original_name = member.display_name
        name_list = list(original_name)
        random.shuffle(name_list)
        new_name = ''.join(name_list)
        try:
            await member.edit(nick=new_name)
        except discord.Forbidden:
            await bot.discord.send(
                f"{getattr(bot.user, 'mention', 'The bot')} could not set your nickname.",
                response=True,
                ephemeral=True,
            )
            return
        await bot.discord.send(
            view=BogonameView(original_name, new_name),
            response=True,
            safety_filter=True
        )
