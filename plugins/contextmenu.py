import random
import discord

from typing import Any, Optional, overload

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

async def setup(bot: "BotCore"):
    @bot.setup.context_menu(
        name="Bogoscramble",
        perm_requirement=0,
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

        sources: list[Any] = []
        if message.content or message.embeds or message.attachments:
            sources.append(message)

        sources.extend(message.message_snapshots)

        if not sources:
            await bot.discord.send(
                "Nothing scrambleable found.",
                response=True,
                ephemeral=True,
            )
            return

        content_parts = [
            scrambled
            for source in sources
            if (scrambled := bogo_scramble(getattr(source, "content", None)))
        ]
        content = "\n\n".join(content_parts)

        embeds = [
            scramble_embed(embed)
            for source in sources
            for embed in getattr(source, "embeds", [])
        ]
        embeds = embeds[:10]

        files: list[discord.File] = []

        for source in sources:
            attachments = getattr(source, "attachments", [])
            for attachment in attachments:
                to_file = getattr(attachment, "to_file", None)
                if to_file is None:
                    continue

                try:
                    files.append(await to_file())
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    pass

        if not content and not embeds and not files:
            await bot.discord.send(
                "Nothing scrambleable found.",
                response=True,
                ephemeral=True,
            )
            return

        send_kwargs = {
            "content": content or None,
            "allowed_mentions": discord.AllowedMentions.none(),
            "response": True,
        }

        if embeds:
            send_kwargs["embeds"] = embeds

        if files:
            send_kwargs["files"] = files

        try:
            await bot.discord.send(**send_kwargs)
        finally:
            try:
                for file in files:
                    file.close()
            except Exception:
                pass
