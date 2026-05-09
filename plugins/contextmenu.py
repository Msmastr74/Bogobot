import io
import random
import discord
from PIL import Image, ImageSequence

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

        def is_image_attachment(attachment) -> bool:
            content_type = getattr(attachment, "content_type", "") or ""
            filename = getattr(attachment, "filename", "").lower()
            return content_type.startswith("image/") or filename.endswith((
                ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"
            ))

        def is_text_attachment(attachment) -> bool:
            content_type = getattr(attachment, "content_type", "") or ""
            filename = getattr(attachment, "filename", "").lower()
            return content_type.startswith("text/") or filename.endswith((
                ".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml"
            ))

        def bogo_image(data: bytes, filename: str) -> discord.File:
            def output_format(image: Image.Image, filename: str) -> tuple[str, str]:
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                if ext in ("jpg", "jpeg"):
                    return "JPEG", "jpg"
                if ext == "png":
                    return "PNG", "png"
                if ext == "gif":
                    return "GIF", "gif"
                if ext == "webp":
                    return "WEBP", "webp"
                if ext == "bmp":
                    return "BMP", "bmp"

                fmt = (image.format or "PNG").upper()
                return fmt, fmt.lower().replace("jpeg", "jpg")

            def scramble_frame(frame: Image.Image) -> Image.Image:
                frame = frame.convert("RGBA")
                width, height = frame.size
                tile_size = 32
                tiles = []

                for y in range(0, height, tile_size):
                    for x in range(0, width, tile_size):
                        box = (
                            x,
                            y,
                            min(x + tile_size, width),
                            min(y + tile_size, height),
                        )
                        tiles.append((box, frame.crop(box)))

                shuffled = [tile for _, tile in tiles]
                random.shuffle(shuffled)

                output = Image.new("RGBA", frame.size)
                for (box, _), tile in zip(tiles, shuffled):
                    output.paste(tile, box[:2])

                return output

            with Image.open(io.BytesIO(data)) as image:
                image_format, extension = output_format(image, filename)
                buffer = io.BytesIO()

                is_animated = getattr(image, "is_animated", False) and getattr(image, "n_frames", 1) > 1
                if is_animated and image_format in ("GIF", "WEBP"):
                    frames = [scramble_frame(frame) for frame in ImageSequence.Iterator(image)]
                    durations = [
                        frame.info.get("duration", image.info.get("duration", 100))
                        for frame in ImageSequence.Iterator(image)
                    ]
                    frames[0].save(
                        buffer,
                        format=image_format,
                        save_all=True,
                        append_images=frames[1:],
                        duration=durations,
                        loop=image.info.get("loop", 0),
                        disposal=image.info.get("disposal", 2),
                    )
                else:
                    output = scramble_frame(image)
                    if image_format == "JPEG":
                        output = output.convert("RGB")

                    output.save(buffer, format=image_format)

                buffer.seek(0)

            stem = filename.rsplit(".", 1)[0] or "image"
            return discord.File(buffer, filename=f"bogo_{stem}.{extension}")

        def bogo_text(data: bytes, filename: str) -> discord.File:
            text = data.decode("utf-8", errors="replace")
            scrambled = bogo_scramble(text) or ""
            buffer = io.BytesIO(scrambled.encode("utf-8"))
            return discord.File(buffer, filename=f"bogo_{filename}")

        async def bogo_attachment(attachment) -> discord.File | None:
            read = getattr(attachment, "read", None)
            if read is None:
                return None

            try:
                data = await read()
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                return None

            filename = getattr(attachment, "filename", "attachment")

            try:
                if is_image_attachment(attachment):
                    return bogo_image(data, filename)

                if is_text_attachment(attachment):
                    return bogo_text(data, filename)
            except Exception as e:
                bot.logger.warning(f"Could not bogo attachment {filename}: {e}")

            return None

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
                file = await bogo_attachment(attachment)
                if file is not None:
                    files.append(file)

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
