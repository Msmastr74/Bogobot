import random
import discord

from typing import Optional, overload

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

async def setup(bot: "BotCore"):
    @bot.setup.command(
        name="Bogoscramble",
        perm_requirement=0,
        mode="context_menu",
        eph=False
    )
    async def bogoscramble(ctx, message: discord.Message):
        @overload
        def bogo_scramble(text: str) -> str: ...
        @overload
        def bogo_scramble(text: Optional[str]) -> Optional[str]: ...
        def bogo_scramble(text: Optional[str]) -> Optional[str]:
            if not text:
                return text

            def scramble_line(line: str) -> str:
                chars = [c for c in line if not c.isspace()]
                random.shuffle(chars)

                scrambled = []
                index = 0

                for char in line:
                    if char.isspace():
                        scrambled.append(char)
                    else:
                        scrambled.append(chars[index])
                        index += 1

                return "".join(scrambled)

            return "".join(
                scramble_line(line)
                for line in text.splitlines(keepends=True)
            )

        def scramble_embed(embed: discord.Embed) -> discord.Embed:
            data = embed.to_dict()

            if "title" in data:
                data["title"] = bogo_scramble(data["title"])

            if "description" in data:
                data["description"] = bogo_scramble(data["description"])

            if "author" in data and "name" in data["author"]:
                data["author"]["name"] = bogo_scramble(data["author"]["name"])

            if "footer" in data and "text" in data["footer"]:
                data["footer"]["text"] = bogo_scramble(data["footer"]["text"])

            if "fields" in data:
                for field in data["fields"]:
                    if "name" in field:
                        field["name"] = bogo_scramble(field["name"])

                    if "value" in field:
                        field["value"] = bogo_scramble(field["value"])

            return discord.Embed.from_dict(data)

        content = bogo_scramble(message.content)

        embeds = [
            scramble_embed(embed)
            for embed in message.embeds[:10]
        ]

        files: list[discord.File] = []

        for attachment in message.attachments:
            try:
                files.append(await attachment.to_file())
            except discord.HTTPException:
                pass

        await bot.discord.send(
            content=content or None,
            embeds=embeds,
            files=files,
            allowed_mentions=discord.AllowedMentions.none(),
            response=True
        )
