import io
import asyncio
import contextlib
import os
import random
import subprocess
import tempfile
import urllib.parse
import aiohttp
import discord
import cv2
import numpy as np
from PIL import Image, ImageSequence

from typing import Any, Optional, overload

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

SCRAMBLE_TILE_PERCENT = 0.1
MAXIMUM_FRAMES = 5000

class BogoUserError(Exception):
    pass

async def setup(bot: "BotCore"):
    @bot.setup.context_menu(
        name="Bogoscramble",
        perm_requirement=0,
        eph=False
    )
    async def bogoscramble(ctx, message: discord.Message):
        upload_limit = getattr(ctx, "filesize_limit", discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES)

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

        def is_image_attachment(filename: str, content_type: str) -> bool:
            content_type = content_type.lower()
            filename = filename.lower()
            return content_type.startswith("image/") or filename.endswith((
                ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"
            ))

        def is_text_attachment(filename: str, content_type: str) -> bool:
            content_type = content_type.lower()
            filename = filename.lower()
            return content_type.startswith("text/") or filename.endswith((
                ".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml"
            ))

        def is_video_attachment(filename: str, content_type: str) -> bool:
            content_type = content_type.lower()
            filename = filename.lower()
            return content_type.startswith("video/") or filename.endswith((
                ".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"
            ))

        def is_scrambleable_attachment(filename: str, content_type: str) -> bool:
            return (
                is_image_attachment(filename, content_type)
                or is_video_attachment(filename, content_type)
                or is_text_attachment(filename, content_type)
            )

        def format_bytes(size: int) -> str:
            units = ("B", "KB", "MB", "GB")
            value = float(size)
            for unit in units:
                if value < 1024 or unit == units[-1]:
                    return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
                value /= 1024

            return f"{size} B"

        def attachment_too_big_message(filename: str, size: int) -> str:
            return (
                f"{filename} is too large to send "
                f"({format_bytes(size)} > {format_bytes(upload_limit)})."
            )

        def tile_plan(width: int, height: int):
            columns = max(1, round(1 / SCRAMBLE_TILE_PERCENT))
            rows = max(1, round(1 / SCRAMBLE_TILE_PERCENT))
            boxes = [
                (
                    round(column * width / columns),
                    round(row * height / rows),
                    round((column + 1) * width / columns),
                    round((row + 1) * height / rows),
                )
                for row in range(rows)
                for column in range(columns)
            ]
            order = list(range(len(boxes)))
            random.shuffle(order)

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
                tile = frame[sy1:sy2, sx1:sx2]
                dst_width = dx2 - dx1
                dst_height = dy2 - dy1

                if tile.shape[1] != dst_width or tile.shape[0] != dst_height:
                    tile = cv2.resize(
                        tile,
                        (dst_width, dst_height),
                        interpolation=cv2.INTER_NEAREST,
                    )

                output[dy1:dy2, dx1:dx2] = tile

            return output, plan

        def fit_frame(frame: np.ndarray, width: int, height: int) -> np.ndarray:
            frame_height, frame_width = frame.shape[:2]
            if frame_width == width and frame_height == height:
                return frame

            return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)

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
                    frame_count = getattr(image, "n_frames", 1)
                    if frame_count > MAXIMUM_FRAMES:
                        raise BogoUserError(
                            f"{filename} has too many frames ({frame_count} > {MAXIMUM_FRAMES})."
                        )

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
            return discord.File(buffer, filename=f"bogo-{stem}.{extension}")

        def bogo_video(
            data: bytes,
            filename: str,
            width_hint: int | None = None,
            height_hint: int | None = None,
        ) -> discord.File | None:
            suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ".mp4"
            output_extension, video_args = video_output_settings(suffix)
            input_file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            output_file = tempfile.NamedTemporaryFile(suffix=f".{output_extension}", delete=False)
            input_path = input_file.name
            output_path = output_file.name
            input_file.close()
            output_file.close()

            try:
                with open(input_path, "wb") as f:
                    f.write(data)

                cap = cv2.VideoCapture(input_path)
                if not cap.isOpened():
                    bot.logger.warning(f"Could not bogo video {filename}: cv2 could not open it")
                    return None

                frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                if frame_count > MAXIMUM_FRAMES:
                    cap.release()
                    bot.logger.warning(
                        f"Could not bogo video {filename}: too many frames ({frame_count} > {MAXIMUM_FRAMES})"
                    )
                    raise BogoUserError(
                        f"{filename} has too many frames ({frame_count} > {MAXIMUM_FRAMES})."
                    )

                fps = cap.get(cv2.CAP_PROP_FPS) or 30
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or width_hint or 0)
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or height_hint or 0)
                if width <= 0 or height <= 0:
                    cap.release()
                    bot.logger.warning(f"Could not bogo video {filename}: invalid size ({width}x{height})")
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
                        *video_args,
                        "-shortest",
                        *(
                            ["-movflags", "+faststart"]
                            if output_extension in ("mp4", "m4v", "mov")
                            else []
                        ),
                        output_path,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                if process.stdin is None:
                    cap.release()
                    process.kill()
                    bot.logger.warning(f"Could not bogo video {filename}: failed to open ffmpeg pipe")
                    return None

                plans: dict[tuple[int, int], object] = {}
                processed = 0

                try:
                    while True:
                        ok, frame = cap.read()
                        if not ok:
                            break

                        processed += 1
                        if processed > MAXIMUM_FRAMES:
                            process.kill()
                            cap.release()
                            bot.logger.warning(
                                f"Could not bogo video {filename}: exceeded {MAXIMUM_FRAMES} frames while reading"
                            )
                            raise BogoUserError(
                                f"{filename} has too many frames (more than {MAXIMUM_FRAMES})."
                            )

                        h, w = frame.shape[:2]
                        plan = plans.get((w, h))
                        scrambled, plan = scramble_array(frame, plan)
                        plans[(w, h)] = plan
                        scrambled = fit_frame(scrambled, width, height)
                        process.stdin.write(scrambled.tobytes())
                except BrokenPipeError:
                    bot.logger.warning(f"Could not bogo video {filename}: ffmpeg pipe broke")
                    return None
                finally:
                    cap.release()
                    with contextlib.suppress(BrokenPipeError, OSError):
                        process.stdin.close()

                if processed == 0:
                    process.kill()
                    bot.logger.warning(f"Could not bogo video {filename}: cv2 read zero frames")
                    return None

                if process.wait(timeout=30) != 0:
                    stderr = ""
                    if process.stderr is not None:
                        with contextlib.suppress(OSError):
                            stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
                    bot.logger.warning(f"Could not bogo video {filename}: ffmpeg failed: {stderr}")
                    return None

                with open(output_path, "rb") as f:
                    buffer = io.BytesIO(f.read())

                buffer.seek(0)
                stem = filename.rsplit(".", 1)[0] or "video"
                return discord.File(buffer, filename=f"bogo-{stem}.{output_extension}")
            finally:
                try:
                    os.unlink(input_path)
                    os.unlink(output_path)
                except OSError:
                    pass

        def video_output_settings(suffix: str) -> tuple[str, list[str]]:
            if suffix == ".webm":
                return "webm", [
                    "-c:v", "libvpx-vp9",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "libopus",
                    "-b:a", "128k",
                ]

            if suffix == ".avi":
                return "avi", [
                    "-c:v", "mpeg4",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "mp3",
                    "-b:a", "128k",
                ]

            if suffix == ".mkv":
                return "mkv", [
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-b:a", "128k",
                ]

            if suffix == ".mov":
                return "mov", [
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-b:a", "128k",
                ]

            if suffix == ".m4v":
                return "m4v", [
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-b:a", "128k",
                ]

            return "mp4", [
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "128k",
            ]

        def bogo_text(data: bytes, filename: str) -> discord.File:
            text = data.decode("utf-8", errors="replace")
            scrambled = bogo_scramble(text) or ""
            buffer = io.BytesIO(scrambled.encode("utf-8"))
            return discord.File(buffer, filename=f"bogo-{filename}")

        def transform_attachment(
            data: bytes,
            filename: str,
            content_type: str,
            width_hint: int | None = None,
            height_hint: int | None = None,
        ) -> discord.File | None:
            if is_image_attachment(filename, content_type):
                return bogo_image(data, filename)

            if is_video_attachment(filename, content_type):
                return bogo_video(data, filename, width_hint, height_hint)

            if is_text_attachment(filename, content_type):
                return bogo_text(data, filename)

            return None

        attachment_semaphore = asyncio.Semaphore(2)

        def file_size(file: discord.File) -> int | None:
            fp = file.fp
            if not hasattr(fp, "tell") or not hasattr(fp, "seek"):
                return None

            try:
                pos = fp.tell()
                fp.seek(0, os.SEEK_END)
                size = fp.tell()
                fp.seek(pos)
            except (OSError, ValueError):
                return None

            return size

        def original_file(data: bytes, filename: str, spoiler: bool) -> discord.File:
            return discord.File(io.BytesIO(data), filename=filename, spoiler=spoiler)

        async def read_attachment(attachment) -> bytes | None:
            read = getattr(attachment, "read", None)
            if read is None:
                return None

            with contextlib.suppress(TypeError, discord.HTTPException, discord.NotFound, discord.Forbidden):
                return await read(use_cached=True)

            with contextlib.suppress(discord.HTTPException, discord.NotFound, discord.Forbidden):
                return await read()

            return None

        def filename_from_url(url: str, fallback: str) -> str:
            path = urllib.parse.urlparse(url).path
            name = os.path.basename(path)
            return name or fallback

        def embed_proxy_url(proxy) -> str | None:
            return proxy.url or proxy.proxy_url

        def embed_media_jobs(embed: discord.Embed) -> list[tuple[str, str]]:
            jobs: list[tuple[str, str]] = []
            seen: set[str] = set()

            for kind, url in (
                ("image", embed_proxy_url(embed.image)),
                ("thumbnail", embed_proxy_url(embed.thumbnail)),
            ):
                if not url or url in seen:
                    continue

                seen.add(url)
                jobs.append((kind, url))

            return jobs

        def set_embed_media(embed: discord.Embed, kind: str, url: str | None):
            if kind == "thumbnail":
                if embed.url == embed_proxy_url(embed.thumbnail):
                    embed.url = None
                embed.set_thumbnail(url=url)
            else:
                if embed.url == embed_proxy_url(embed.image):
                    embed.url = None
                embed.set_image(url=url)

        def is_plain_image_embed(embed: discord.Embed) -> bool:
            return embed.type == "image"

        async def read_url(url: str) -> tuple[bytes, str] | None:
            timeout = aiohttp.ClientTimeout(total=15)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as response:
                        if response.status >= 400:
                            bot.logger.warning(f"Could not read embed image {url}: HTTP {response.status}")
                            return None

                        content_length = response.headers.get("Content-Length")
                        if content_length:
                            with contextlib.suppress(ValueError):
                                size = int(content_length)
                                if size > upload_limit:
                                    filename = filename_from_url(url, "embed_image.png")
                                    raise BogoUserError(attachment_too_big_message(filename, size))

                        content_type = response.headers.get("Content-Type", "")
                        return await response.read(), content_type
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                bot.logger.warning(f"Could not read embed image {url}: {e}")
                return None

        async def bogo_attachment(attachment) -> tuple[discord.File | None, str | None]:
            filename = getattr(attachment, "filename", "attachment")
            content_type = getattr(attachment, "content_type", "") or ""
            width = getattr(attachment, "width", None)
            height = getattr(attachment, "height", None)
            size = getattr(attachment, "size", 0) or 0
            spoiler = bool(getattr(attachment, "spoiler", False))

            if size > upload_limit:
                return None, attachment_too_big_message(filename, size)

            async with attachment_semaphore:
                data = await read_attachment(attachment)
                if data is None:
                    bot.logger.warning(f"Could not read attachment {filename}")
                    return None, None

                if len(data) > upload_limit:
                    return None, attachment_too_big_message(filename, len(data))

                if not is_scrambleable_attachment(filename, content_type):
                    return original_file(data, filename, spoiler), None

                try:
                    loop = asyncio.get_running_loop()
                    file = await loop.run_in_executor(
                        None,
                        transform_attachment,
                        data,
                        filename,
                        content_type,
                        width,
                        height,
                    )
                    if file is None:
                        bot.logger.warning(f"Could not bogo supported attachment {filename}")
                        return None, None

                    output_size = file_size(file)
                    if output_size is not None and output_size > upload_limit:
                        file.close()
                        return None, attachment_too_big_message(file.filename, output_size)

                    return file, None
                except BogoUserError as e:
                    return None, str(e)
                except Exception as e:
                    bot.logger.warning(f"Could not bogo attachment {filename}: {e}")

            return None, None

        async def bogo_embed_image(
            index: int,
            embed: discord.Embed,
            kind: str,
            url: str,
        ) -> tuple[discord.File | None, str | None]:
            try:
                result = await read_url(url)
            except BogoUserError as e:
                set_embed_media(embed, kind, None)
                return None, str(e)

            if result is None:
                set_embed_media(embed, kind, None)
                return None, None

            data, content_type = result
            filename = filename_from_url(url, f"embed_image_{index}.png")
            if not is_image_attachment(filename, content_type):
                set_embed_media(embed, kind, None)
                return None, None

            try:
                loop = asyncio.get_running_loop()
                file = await loop.run_in_executor(None, bogo_image, data, filename)
            except BogoUserError as e:
                set_embed_media(embed, kind, None)
                return None, str(e)
            except Exception as e:
                bot.logger.warning(f"Could not bogo embed image {filename}: {e}")
                set_embed_media(embed, kind, None)
                return None, None

            output_size = file_size(file)
            if output_size is not None and output_size > upload_limit:
                file.close()
                set_embed_media(embed, kind, None)
                return None, attachment_too_big_message(file.filename, output_size)

            set_embed_media(embed, kind, f"attachment://{file.filename}")
            return file, None

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

        embed_pairs = [
            (embed, scramble_embed(embed))
            for source in sources
            for embed in getattr(source, "embeds", [])
        ][:10]
        omit_embed_indexes = {
            index
            for index, (original, _) in enumerate(embed_pairs)
            if is_plain_image_embed(original)
        }
        embeds = [
            scrambled
            for index, (_, scrambled) in enumerate(embed_pairs)
            if index not in omit_embed_indexes
        ]

        attachment_tasks = []

        for source in sources:
            attachments = getattr(source, "attachments", [])
            for attachment in attachments:
                attachment_tasks.append(bogo_attachment(attachment))

        attachment_results = await asyncio.gather(*attachment_tasks)
        files: list[discord.File] = [
            file
            for file, _ in attachment_results
            if file is not None
        ]
        user_errors = [
            error
            for _, error in attachment_results
            if error is not None
        ]
        files = files[:10]

        embed_image_candidates = [
            (index, embed, kind, url)
            for index, (original, embed) in enumerate(embed_pairs)
            for kind, url in embed_media_jobs(original)
        ]
        available_file_slots = max(0, 10 - len(files))
        for _, embed, kind, _ in embed_image_candidates[available_file_slots:]:
            set_embed_media(embed, kind, None)

        embed_image_tasks = [
            bogo_embed_image(index, embed, kind, url)
            for index, embed, kind, url in embed_image_candidates[:available_file_slots]
        ]
        embed_image_results = await asyncio.gather(*embed_image_tasks)
        embed_image_files = [
            file
            for file, _ in embed_image_results
            if file is not None
        ]
        files.extend(embed_image_files)
        user_errors.extend(
            error
            for _, error in embed_image_results
            if error is not None
        )
        if user_errors:
            error_text = "\n".join(user_errors)
            content = f"{content}\n\n{error_text}" if content else error_text

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
            try:
                await bot.discord.send(**send_kwargs)
            except discord.HTTPException as e:
                bot.logger.warning(f"Could not send bogoscramble result: {e}")
                await bot.discord.send(
                    "Could not send the scrambled result. One of the files may be too large.",
                    response=True,
                    ephemeral=True,
                )
        finally:
            try:
                for file in files:
                    file.close()
            except Exception:
                pass
