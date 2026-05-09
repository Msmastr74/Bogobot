import io
import contextlib
import os
import random
import subprocess
import tempfile
import discord
import cv2
import numpy as np
from PIL import Image, ImageSequence

from typing import Any, Optional, overload

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

SCRAMBLE_TILE_PERCENT = 0.1

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

        def is_video_attachment(attachment) -> bool:
            content_type = getattr(attachment, "content_type", "") or ""
            filename = getattr(attachment, "filename", "").lower()
            return content_type.startswith("video/") or filename.endswith((
                ".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"
            ))

        def tile_plan(width: int, height: int):
            tile_width = max(1, round(width * SCRAMBLE_TILE_PERCENT))
            tile_height = max(1, round(height * SCRAMBLE_TILE_PERCENT))
            boxes = [
                (x, y, min(x + tile_width, width), min(y + tile_height, height))
                for y in range(0, height, tile_height)
                for x in range(0, width, tile_width)
            ]
            order = list(range(len(boxes)))
            buckets: dict[tuple[int, int], list[int]] = {}

            for index, (x1, y1, x2, y2) in enumerate(boxes):
                buckets.setdefault((x2 - x1, y2 - y1), []).append(index)

            for indices in buckets.values():
                shuffled = indices.copy()
                random.shuffle(shuffled)
                for dst_index, src_index in zip(indices, shuffled):
                    order[dst_index] = src_index

            return boxes, order

        def scramble_array(frame: np.ndarray, plan=None) -> tuple[np.ndarray, object]:
            height, width = frame.shape[:2]
            if plan is None:
                plan = tile_plan(width, height)

            boxes, order = plan
            output = np.empty_like(frame)

            for dst_box, src_index in zip(boxes, order):
                src_box = boxes[src_index]
                dx1, dy1, dx2, dy2 = dst_box
                sx1, sy1, sx2, sy2 = src_box
                output[dy1:dy2, dx1:dx2] = frame[sy1:sy2, sx1:sx2]

            return output, plan

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

        def webp_frame_durations(data: bytes) -> list[int]:
            if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
                return []

            durations: list[int] = []
            offset = 12

            while offset + 8 <= len(data):
                chunk_type = data[offset:offset + 4]
                chunk_size = int.from_bytes(data[offset + 4:offset + 8], "little")
                chunk_start = offset + 8
                chunk_end = chunk_start + chunk_size

                if chunk_end > len(data):
                    break

                if chunk_type == b"ANMF" and chunk_size >= 16:
                    duration = int.from_bytes(
                        data[chunk_start + 12:chunk_start + 15],
                        "little",
                    )
                    durations.append(max(10, duration))

                offset = chunk_end + (chunk_size % 2)

            return durations

        def bogo_image(data: bytes, filename: str) -> discord.File:
            with Image.open(io.BytesIO(data)) as image:
                image_format, extension = output_format(image, filename)
                buffer = io.BytesIO()
                plan = tile_plan(*image.size)

                is_animated = getattr(image, "is_animated", False) and getattr(image, "n_frames", 1) > 1
                if is_animated and image_format in ("GIF", "WEBP"):
                    frames = []
                    durations = [
                        frame.info.get("duration", image.info.get("duration", 100))
                        for frame in ImageSequence.Iterator(image)
                    ]
                    if image_format == "WEBP":
                        durations = webp_frame_durations(data) or durations

                    for frame in ImageSequence.Iterator(image):
                        arr = np.array(frame.convert("RGBA"))
                        scrambled, _ = scramble_array(arr, plan)
                        frames.append(Image.fromarray(scrambled, "RGBA"))

                    save_kwargs = {
                        "format": image_format,
                        "save_all": True,
                        "append_images": frames[1:],
                        "duration": durations,
                        "loop": image.info.get("loop", 0),
                    }

                    if image_format == "GIF":
                        save_kwargs["disposal"] = image.info.get("disposal", 2)

                    frames[0].save(buffer, **save_kwargs)
                else:
                    arr = np.array(image.convert("RGBA"))
                    scrambled, _ = scramble_array(arr, plan)
                    output = Image.fromarray(scrambled, "RGBA")
                    if image_format == "JPEG":
                        output = output.convert("RGB")

                    output.save(buffer, format=image_format)

                buffer.seek(0)

            stem = filename.rsplit(".", 1)[0] or "image"
            return discord.File(buffer, filename=f"bogo_{stem}.{extension}")

        def bogo_video(data: bytes, filename: str) -> discord.File | None:
            suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ".mp4"
            input_file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            output_file = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
            input_path = input_file.name
            output_path = output_file.name
            input_file.close()
            output_file.close()

            try:
                with open(input_path, "wb") as f:
                    f.write(data)

                cap = cv2.VideoCapture(input_path)
                if not cap.isOpened():
                    return None

                frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                if frame_count > 5000:
                    cap.release()
                    return None

                fps = cap.get(cv2.CAP_PROP_FPS) or 30
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                if width <= 0 or height <= 0:
                    cap.release()
                    return None

                process = subprocess.Popen(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel", "error",
                        "-y",
                        "-f", "rawvideo",
                        "-pix_fmt", "bgr24",
                        "-s", f"{width}x{height}",
                        "-r", str(fps),
                        "-i", "pipe:0",
                        "-i", input_path,
                        "-map", "0:v:0",
                        "-map", "1:a?",
                        "-c:v", "libx264",
                        "-pix_fmt", "yuv420p",
                        "-c:a", "copy",
                        "-shortest",
                        "-movflags", "+faststart",
                        output_path,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if process.stdin is None:
                    cap.release()
                    process.kill()
                    return None

                plans: dict[tuple[int, int], object] = {}
                processed = 0

                try:
                    while True:
                        ok, frame = cap.read()
                        if not ok:
                            break

                        processed += 1
                        if processed > 5000:
                            process.stdin.close()
                            process.kill()
                            cap.release()
                            return None

                        h, w = frame.shape[:2]
                        plan = plans.get((w, h))
                        scrambled, plan = scramble_array(frame, plan)
                        plans[(w, h)] = plan
                        process.stdin.write(scrambled.tobytes())
                except BrokenPipeError:
                    cap.release()
                    return None
                finally:
                    cap.release()
                    with contextlib.suppress(BrokenPipeError, OSError):
                        process.stdin.close()

                if process.wait(timeout=30) != 0:
                    return None

                with open(output_path, "rb") as f:
                    buffer = io.BytesIO(f.read())

                buffer.seek(0)
                stem = filename.rsplit(".", 1)[0] or "video"
                return discord.File(buffer, filename=f"bogo_{stem}.mp4")
            finally:
                try:
                    os.unlink(input_path)
                    os.unlink(output_path)
                except OSError:
                    pass

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

                if is_video_attachment(attachment):
                    return bogo_video(data, filename)

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
