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
from types import CoroutineType
from bogobot_core import current_interaction
from logger_pipe import log_subprocess_pipe
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import BotCore

MAXIMUM_FRAMES = 5000
DEFAULT_SCRAMBLE_SHAPE = (10, 10)
MAXIMUM_SCRAMBLE_SHAPE = (30, 30)

class BogoUserError(Exception):
    pass

async def setup(bot: "BotCore"):
    def get_scramble_shape(rows: int | None, columns: int | None) -> tuple[int, int]:
        if rows is None and columns is None:
            return DEFAULT_SCRAMBLE_SHAPE

        if rows is not None and not 1 <= rows <= MAXIMUM_SCRAMBLE_SHAPE[0]:
            raise BogoUserError(
                f"Number of rows must be between 1 and {MAXIMUM_SCRAMBLE_SHAPE[0]}"
            )

        if columns is not None and not 1 <= columns <= MAXIMUM_SCRAMBLE_SHAPE[1]:
            raise BogoUserError(
                f"Number of columns must be between 1 and {MAXIMUM_SCRAMBLE_SHAPE[1]}"
            )

        return rows or 1, columns or 1

    def message_bogoscramble_inputs(
        message: discord.Message,
    ) -> tuple[str, list[discord.Embed], list[Any]]:
        sources: list[discord.Message | discord.MessageSnapshot] = [
            message,
            *message.message_snapshots
        ]

        content_parts = [
            source.content
            for source in sources
            if source.content
        ]
        embeds = [
            embed
            for source in sources
            for embed in source.embeds
        ]
        attachments = [
            attachment
            for source in sources
            for attachment in source.attachments
        ]

        return "\n\n".join(content_parts), embeds, attachments

    async def send_bogoscramble(
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embeds: list[discord.Embed] | None = None,
        attachments: list[Any] | None = None,
        scramble_shape: tuple[int, int] = DEFAULT_SCRAMBLE_SHAPE
    ):
        upload_limit = interaction.filesize_limit

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

        def is_image_content_type(content_type: str) -> bool:
            return content_type.lower().startswith("image/")

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
            rows = max(1, scramble_shape[0])
            columns = max(1, scramble_shape[1])
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
        ) -> discord.File:
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
                    raise BogoUserError(f"Could not read video `{filename}`.")

                frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                if frame_count > MAXIMUM_FRAMES:
                    cap.release()
                    raise BogoUserError(
                        f"{filename} has too many frames ({frame_count} > {MAXIMUM_FRAMES})."
                    )

                fps = cap.get(cv2.CAP_PROP_FPS) or 30
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or width_hint or 0)
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or height_hint or 0)
                if width <= 0 or height <= 0:
                    cap.release()
                    raise BogoUserError(f"Could not read video size for `{filename}`.")

                ffmpeg_logger = None
                try:
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
                    ffmpeg_logger = log_subprocess_pipe(
                        process.stderr,
                        bot.logger.getChild("Bogoscramble"),
                        prefix="ffmpeg",
                    )
                    if process.stdin is None:
                        cap.release()
                        process.kill()
                        raise BogoUserError(f"Could not start video encoder for `{filename}`.")

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
                        raise BogoUserError(f"Could not encode video `{filename}`.")
                    finally:
                        cap.release()
                        with contextlib.suppress(BrokenPipeError, OSError):
                            process.stdin.close()

                    if processed == 0:
                        process.kill()
                        raise BogoUserError(f"Could not read any frames from `{filename}`.")

                    if process.wait(timeout=30) != 0:
                        if ffmpeg_logger is not None:
                            ffmpeg_logger.close()

                        stderr = ffmpeg_logger.text if ffmpeg_logger is not None else ""
                        message = f"Could not encode video `{filename}`."
                        if stderr:
                            message += f"\n```{stderr[:1500]}```"
                        raise BogoUserError(message)
                finally:
                    if ffmpeg_logger is not None:
                        ffmpeg_logger.close()

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

        def embed_fetch_url(proxy: discord.embeds._EmbedMediaProxy) -> tuple[str, bool] | None:
            if proxy.url and proxy.url.startswith("https://cdn.discordapp.com/"):
                return proxy.url, False

            if proxy.proxy_url:
                return proxy.proxy_url, True

            if proxy.url:
                return proxy.url, False

            return None

        def embed_media_jobs(embed: discord.Embed) -> list[tuple[str, str, bool]]:
            jobs: list[tuple[str, str, bool]] = []
            seen: set[str] = set()

            for kind, result in (
                ("image", embed_fetch_url(embed.image)),
                ("thumbnail", embed_fetch_url(embed.thumbnail)),
            ):
                if result is None:
                    continue

                url, use_discord_headers = result
                if not url or url in seen:
                    continue

                seen.add(url)
                jobs.append((kind, url, use_discord_headers))

            return jobs

        def set_embed_media(embed: discord.Embed, kind: str, url: str | None):
            if kind == "thumbnail":
                if embed.url == embed.thumbnail.url:
                    embed.url = None
                embed.set_thumbnail(url=url)
            else:
                if embed.url == embed.image.url:
                    embed.url = None
                embed.set_image(url=url)

        def is_plain_image_embed(embed: discord.Embed) -> bool:
            return embed.type == "image"

        def discord_fetch_headers() -> dict[str, str]:
            headers: dict[str, str] = {
                "User-Agent": bot.http.user_agent
            }

            if bot.http.token is not None:
                headers['Authorization'] = 'Bot ' + bot.http.token

            return headers

        async def read_url(url: str, *, use_discord_headers: bool = False) -> tuple[bytes, str]:
            timeout = aiohttp.ClientTimeout(total=15)
            session: aiohttp.ClientSession | discord.utils._MissingSentinel | None = (
                getattr(bot.http, '_HTTPClient__session', None)
            )

            if not isinstance(session, aiohttp.ClientSession) or session.closed:
                raise BogoUserError("Could not access Discord HTTP session.")

            try:
                async with session.get(
                    url,
                    timeout=timeout,
                    headers=discord_fetch_headers() if use_discord_headers else None,
                ) as response:
                    if response.status >= 400:
                        raise BogoUserError(f"Could not read embed image (HTTP {response.status}).")

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
                raise BogoUserError("Could not read embed image.") from e

        async def bogo_attachment(attachment) -> discord.File | None:
            filename = getattr(attachment, "filename", "attachment")
            content_type = getattr(attachment, "content_type", "") or ""
            width = getattr(attachment, "width", None)
            height = getattr(attachment, "height", None)
            size = getattr(attachment, "size", 0) or 0
            spoiler = bool(getattr(attachment, "spoiler", False))

            if size > upload_limit:
                raise BogoUserError(attachment_too_big_message(filename, size))

            async with attachment_semaphore:
                data = await read_attachment(attachment)
                if data is None:
                    raise BogoUserError(f"Could not read attachment `{filename}`.")

                if len(data) > upload_limit:
                    raise BogoUserError(attachment_too_big_message(filename, len(data)))

                if not is_scrambleable_attachment(filename, content_type):
                    return original_file(data, filename, spoiler)

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
                        raise BogoUserError(f"Could not bogo attachment `{filename}`.")

                    output_size = file_size(file)
                    if output_size is not None and output_size > upload_limit:
                        file.close()
                        raise BogoUserError(attachment_too_big_message(file.filename, output_size))

                    return file
                except BogoUserError:
                    raise
                except Exception as e:
                    raise BogoUserError(f"Could not bogo attachment `{filename}`.") from e

        async def bogo_embed_image(
            index: int,
            embed: discord.Embed,
            kind: str,
            url: str,
            use_discord_headers: bool,
        ) -> discord.File | None:
            try:
                data, content_type = await read_url(
                    url,
                    use_discord_headers=use_discord_headers,
                )
            except BogoUserError:
                set_embed_media(embed, kind, None)
                raise

            filename = filename_from_url(url, f"embed_image_{index}.png")
            if not is_image_content_type(content_type):
                set_embed_media(embed, kind, None)
                raise BogoUserError("Embed media was not an image.")

            try:
                loop = asyncio.get_running_loop()
                file = await loop.run_in_executor(None, bogo_image, data, filename)
            except BogoUserError:
                set_embed_media(embed, kind, None)
                raise
            except Exception as e:
                set_embed_media(embed, kind, None)
                raise BogoUserError(f"Could not bogo embed image `{filename}`.") from e

            output_size = file_size(file)
            if output_size is not None and output_size > upload_limit:
                file.close()
                set_embed_media(embed, kind, None)
                raise BogoUserError(attachment_too_big_message(file.filename, output_size))

            set_embed_media(embed, kind, f"attachment://{file.filename}")
            return file

        async def gather_bogo_files(
            tasks: list[CoroutineType[Any, Any, discord.File | None]]
        ) -> list[discord.File]:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            files: list[discord.File] = []

            for result in results:
                if result is None:
                    continue

                if isinstance(result, BogoUserError):
                    for file in files:
                        file.close()
                    raise result

                if isinstance(result, Exception):
                    for file in files:
                        file.close()
                    bot.logger.warning(result)
                    raise BogoUserError("Could not bogo media.") from result
                
                if isinstance(result, BaseException):
                    raise result

                files.append(result)

            return files

        embeds = embeds or []
        attachments = attachments or []

        if not content and not embeds and not attachments:
            raise BogoUserError("Nothing scrambleable found.")

        content_parts = []

        if scrambled_text := bogo_scramble(content):
            content_parts.append(scrambled_text)
        content = "\n\n".join(content_parts)

        embed_pairs = [
            (embed, scramble_embed(embed))
            for embed in embeds[:10]
        ]
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

        attachment_tasks = [
            bogo_attachment(attachment)
            for attachment in attachments
        ]

        attachment_files = await gather_bogo_files(attachment_tasks)
        files = attachment_files[:10]
        for file in attachment_files[10:]:
            file.close()

        try:
            embed_image_candidates = [
                (index, embed, kind, url, use_discord_headers)
                for index, (original, embed) in enumerate(embed_pairs)
                for kind, url, use_discord_headers in embed_media_jobs(original)
            ]
            available_file_slots = max(0, 10 - len(files))
            for _, embed, kind, _, _ in embed_image_candidates[available_file_slots:]:
                set_embed_media(embed, kind, None)

            embed_image_tasks = [
                bogo_embed_image(index, embed, kind, url, use_discord_headers)
                for index, embed, kind, url, use_discord_headers in embed_image_candidates[:available_file_slots]
            ]
            files.extend(await gather_bogo_files(embed_image_tasks))

            if not content and not embeds and not files:
                raise BogoUserError("Nothing scrambleable found.")

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
            except discord.HTTPException as e:
                raise BogoUserError(
                    f"Sending the scrambled result failed with error: {e}"
                ) from e
        finally:
            try:
                for file in files:
                    file.close()
            except Exception:
                pass

    async def send_bogo_error(interaction: discord.Interaction, error: BogoUserError):
        with contextlib.suppress(discord.NotFound, discord.HTTPException):
            await interaction.delete_original_response()

        await bot.discord.send(
            str(error),
            response=True,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True
        )

    @bot.setup.context_menu(
        name="Bogoscramble",
        perm_requirement=0,
        eph=False,
    )
    async def Bogoscramble(interaction: discord.Interaction, message: discord.Message):
        content, embeds, attachments = message_bogoscramble_inputs(message)

        try:
            await send_bogoscramble(
                interaction,
                content=content,
                embeds=embeds,
                attachments=attachments,
            )
        except BogoUserError as e:
            await send_bogo_error(interaction, e)

    class CustomBogoscrambleModal(discord.ui.Modal, title="Custom Bogoscramble"):
        rows = discord.ui.TextInput(
            label="Rows",
            default=str(DEFAULT_SCRAMBLE_SHAPE[0]),
            required=True,
            max_length=2,
        )
        columns = discord.ui.TextInput(
            label="Columns",
            default=str(DEFAULT_SCRAMBLE_SHAPE[1]),
            required=True,
            max_length=2,
        )

        def __init__(
            self,
            *,
            content: str,
            embeds: list[discord.Embed],
            attachments: list[Any],
        ):
            super().__init__()
            self.content = content
            self.embeds = embeds
            self.attachments = attachments

        async def on_submit(self, interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=False)
            token = current_interaction.set(interaction)

            try:
                try:
                    rows = int(str(self.rows.value).strip())
                    columns = int(str(self.columns.value).strip())
                except ValueError as e:
                    raise BogoUserError("Rows and columns must be whole numbers.") from e

                await send_bogoscramble(
                    interaction,
                    content=self.content,
                    embeds=self.embeds,
                    attachments=self.attachments,
                    scramble_shape=get_scramble_shape(rows, columns),
                )
            except BogoUserError as e:
                await send_bogo_error(interaction, e)
            finally:
                current_interaction.reset(token)
    @bot.setup.context_menu(
        name="Custom Bogoscramble",
        perm_requirement=0,
        eph=False,
        defer=False,
    )
    async def CustomBogoscramble(interaction: discord.Interaction, message: discord.Message):
        content, embeds, attachments = message_bogoscramble_inputs(message)
        await interaction.response.send_modal(
            CustomBogoscrambleModal(
                content=content,
                embeds=embeds,
                attachments=attachments,
            )
        )

    @bot.setup.command(
        name="bogoscramble",
        description="Bogoscramble text and attachments",
        perm_requirement=0,
        defer=False
    )
    async def bogoscramble(
        interaction: discord.Interaction,
        text: str | None = None,
        attachment1: discord.Attachment | None = None,
        rows: int | None = None,
        columns: int | None = None,
        attachment2: discord.Attachment | None = None,
        attachment3: discord.Attachment | None = None,
        attachment4: discord.Attachment | None = None,
        attachment5: discord.Attachment | None = None,
        attachment6: discord.Attachment | None = None,
        attachment7: discord.Attachment | None = None,
        attachment8: discord.Attachment | None = None,
        attachment9: discord.Attachment | None = None,
        attachment10: discord.Attachment | None = None,
        attachment11: discord.Attachment | None = None,
        attachment12: discord.Attachment | None = None,
        attachment13: discord.Attachment | None = None,
        attachment14: discord.Attachment | None = None,
        attachment15: discord.Attachment | None = None,
        attachment16: discord.Attachment | None = None,
        attachment17: discord.Attachment | None = None,
        attachment18: discord.Attachment | None = None,
        attachment19: discord.Attachment | None = None,
        attachment20: discord.Attachment | None = None
    ):
        try:
            scramble_shape = get_scramble_shape(rows, columns)
        except BogoUserError as e:
            await bot.discord.send(
                str(e),
                response=True,
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        attachments = [
            attachment
            for attachment in (
                attachment1,
                attachment2,
                attachment3,
                attachment4,
                attachment5,
                attachment6,
                attachment7,
                attachment8,
                attachment9,
                attachment10,
                attachment11,
                attachment12,
                attachment13,
                attachment14,
                attachment15,
                attachment16,
                attachment17,
                attachment18,
                attachment19,
                attachment20
            )
            if attachment is not None
        ]

        try:
            await send_bogoscramble(
                interaction,
                content=text,
                attachments=attachments,
                scramble_shape=scramble_shape
            )
        except BogoUserError as e:
            await send_bogo_error(interaction, e)
