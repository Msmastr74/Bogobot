import numpy as np
import time
from decimal import Decimal, InvalidOperation
from logging import Logger
from PIL import Image

import aiohttp
from bogobot_core import BotCore
from ocr import OcrCrop, OcrResult
import asyncio
import cv2
from pydantic import ValidationError
from utils.schemas import BogostreamApiStats

BOGOSTREAM_STATS_API_URL = "https://bogo.swapjs.dev/api/stats"
BOGOSTREAM_STATS_API_INTERVAL_SECONDS = 1.0
MILESTONE_ROUND_TO_PREFIX = [1, 2, 3, 5]
SORT_SECTION_SIDE_TRIM_RATIO = 0.2

STAT_SUFFIX_POWERS = {
    "": 0,
    "k": 3,
    "m": 6,
    "b": 9,
    "t": 12,
    "q": 15,
    "qa": 15,
    "qd": 15,
    "qi": 18,
    "qt": 18,
    "sx": 21,
    "sp": 24,
    "oc": 27,
    "no": 30,
    "dc": 33,
}


def parse_number(value: object) -> Decimal | None:
    if value is None or value == "":
        return None

    if isinstance(value, int | float | Decimal):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return None
        return number if number.is_finite() else None

    if not isinstance(value, str):
        return None

    compact = value.strip().replace(",", "").replace(" ", "")
    suffix_start = len(compact)
    while suffix_start > 0 and compact[suffix_start - 1].isalpha():
        suffix_start -= 1

    number_text = compact[:suffix_start]
    suffix = compact[suffix_start:].lower()
    if not number_text or suffix not in STAT_SUFFIX_POWERS:
        return None

    try:
        number = Decimal(number_text)
    except InvalidOperation:
        return None

    if not number.is_finite():
        return None

    return number * (Decimal(10) ** STAT_SUFFIX_POWERS[suffix])


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return f"{days:02}:{hours:02}:{minutes:02}:{seconds:02}"


async def setup(bot: BotCore):
    log = bot.logger.getChild("Stats")
    stats_source = str(bot.config.get("stats_source", "api")).lower()
    api_enabled = stats_source in {"api", "event", "events"}
    debug_frame_processing = bool(bot.config.get("debug_frame_processing", False))
    api_url = str(bot.config.get("bogostream_stats_api_url", BOGOSTREAM_STATS_API_URL))
    api_interval = max(
        0.25,
        float(bot.config.get(
            "bogostream_stats_api_interval",
            BOGOSTREAM_STATS_API_INTERVAL_SECONDS,
        )),
    )
    api_task: asyncio.Task[None] | None = None

    def sections_from_sort_values(sort_values: list[int]) -> list[bool]:
        return [
            value == index + 1
            for index, value in enumerate(sort_values[:bot.SORT_SECTION_COUNT])
        ]

    def apply_api_stats(data: BogostreamApiStats, timestamp: float) -> tuple[list[tuple[bool, int]], int] | None:
        sort_values = data.tick_best_arr[:bot.SORT_SECTION_COUNT]
        if len(sort_values) < bot.SORT_SECTION_COUNT:
            sort_values.extend([0] * (bot.SORT_SECTION_COUNT - len(sort_values)))

        best_shuffle_sections = sections_from_sort_values(sort_values)
        new_values = list(zip(best_shuffle_sections, sort_values, strict=False))
        best_count = max(0, min(bot.SORT_SECTION_COUNT, int(data.recent_best.tick_best)))
        bot.stats.update(data.stats_cache())
        bot._last_ocr_refresh = timestamp
        bot.best_shuffle_sections = best_shuffle_sections
        bot.sort_values = sort_values
        bot.new_values = new_values
        return new_values, best_count

    async def fetch_api_stats(session: aiohttp.ClientSession) -> BogostreamApiStats | None:
        async with session.get(api_url) as response:
            if response.status != 200:
                log.warning(f"Bogostream stats API returned HTTP {response.status}")
                return None
            try:
                return BogostreamApiStats.model_validate(
                    await response.json(),
                    context={"section_count": bot.SORT_SECTION_COUNT},
                )
            except ValidationError:
                log.warning("Bogostream stats API returned an unexpected payload shape")
                return None

    async def api_stats_loop() -> None:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                timestamp = time.time()
                try:
                    data = await fetch_api_stats(session)
                    if data is not None:
                        event = apply_api_stats(data, timestamp)
                        if bot.milestones:
                            await update_milestones(None, timestamp)
                        if event is not None:
                            new_values, best_count = event
                            await bot.new_value(new_values, best_count, timestamp=timestamp)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Bogostream stats API update failed")

                await asyncio.sleep(api_interval)

    async def update_ocr_data(img: Image.Image, *, sort_changed: bool = True) -> None:
        ocr = bot.ocr
        if ocr is None:
            log.warning("OCR stats source is selected, but OCR is disabled. Set `ocr_enabled` to true to use OCR.")
            return

        try:
            async def parse_crops(crops: list[OcrCrop]) -> list[OcrResult]:
                async def parse_crop(crop: OcrCrop) -> OcrResult:
                    return await ocr.parse(
                        img.crop(crop.coords),
                        crop.whitelist,
                        psm=7 if crop.psm is None else crop.psm,
                        scale=3 if crop.scale is None else crop.scale,
                        threshold=165 if crop.threshold is None else crop.threshold,
                        close=True if crop.close is None else crop.close,
                        dilate=True if crop.dilate is None else crop.dilate,
                    )

                return await asyncio.gather(*[
                    parse_crop(crop)
                    for crop in crops
                ])

            crops: list[OcrCrop] = []
            stats_specs: list[tuple[int, str, str]] = []

            for name, crop in bot.STATS_COORDS.items():
                stats_specs.append((len(crops), name, crop.whitelist))
                crops.append(crop)

            results = await parse_crops(crops)

            for index, name, whitelist in stats_specs:
                text, conf = results[index]
                if not text or conf < 0:
                    continue

                bot.stats[name] = text

            bot._last_ocr_refresh = time.time()
        except Exception:
            log.exception("OCR processing error")

    async def update_milestones(img: Image.Image | None, frame_received_at: float):
        if bot.milestones is None:
            return

        frame_timestamp = int(frame_received_at)
        stats = bot.stats
        best_run = stats.get("best_run")
        if best_run:
            await bot.milestones.update("Best run", str(best_run), timestamp=frame_timestamp, img=img)

        for milestone_name, stat_name in (
            ("Shuffles", "shuffles"),
            ("Comparisons", "comparisons"),
        ):
            stat_value = round_stat_down_to_power(stats.get(stat_name))
            if stat_value:
                await bot.milestones.update(milestone_name, stat_value, timestamp=frame_timestamp, img=img)

        shuffles_sec = round_stat_down_to_digit(stats.get("shuffles_sec"))
        if shuffles_sec:
            await update_non_decreasing_milestone(
                "Shuffles each second record",
                shuffles_sec,
                timestamp=frame_timestamp,
                img=img,
            )

        average_best_shuffle = round_stat_down_to_int(
            stats.get("average_best_shuffle")
        )
        if average_best_shuffle:
            await update_non_decreasing_milestone(
                "Average best shuffle record",
                average_best_shuffle,
                timestamp=frame_timestamp,
                img=img
            )

    async def update_non_decreasing_milestone(
        milestone_name: str,
        milestone_value: str,
        timestamp: int,
        img: Image.Image | None = None,
    ) -> str | None:
        if bot.milestones is None:
            return None

        current_value = await bot.milestones.get(milestone_name)
        current_number = parse_number(current_value)
        next_number = parse_number(milestone_value)

        if (
            current_number is not None
            and next_number is not None
            and next_number < current_number
        ):
            return None

        return await bot.milestones.update(milestone_name, milestone_value, timestamp=timestamp, img=img)

    def round_stat_down_to_power(value: object) -> str | None:
        number = parse_number(value)
        if number is None:
            return None

        number = int(number)
        if number <= 0:
            return None

        rounded = 0
        for exponent in range(len(str(number))):
            power = 10 ** exponent
            for prefix in MILESTONE_ROUND_TO_PREFIX:
                prefix = int(prefix)
                if prefix <= 0:
                    continue

                candidate = prefix * power
                if rounded < candidate <= number:
                    rounded = candidate

        if rounded <= 0:
            return None
        return f"{rounded:,}"

    def round_stat_down_to_digit(value: object) -> str | None:
        number = parse_number(value)
        if number is None:
            return None

        number = int(number)
        if number <= 0:
            return None

        power = 10 ** (len(str(number)) - 1)
        return f"{number // power * power:,}"

    def round_stat_down_to_int(value: object) -> str | None:
        number = parse_number(value)
        if number is None:
            return None

        return f"{int(number):,}"

    sort_reader = SortSectionReader(bot, logger=log, debug=debug_frame_processing)

    last_frame_monotonic = time.monotonic()
    @bot.new_frame_callback
    async def new_frame(img: Image.Image):
        nonlocal last_frame_monotonic
        frame_received_at = time.time()

        frame_received_monotonic = time.monotonic()
        dt = frame_received_monotonic - last_frame_monotonic
        last_frame_monotonic = frame_received_monotonic
        if debug_frame_processing:
            log.debug(f"New frame received (dt={dt:.2f}s)")
        
        if bot.config.get("save_live_frame", False):
            img.save("live_720p.png", format="PNG")

        if api_enabled:
            return
        
        sort_changed_start = time.monotonic()
        (
            sort_changed,
            best_shuffle_sections,
            sort_values,
            new_values,
        ) = sort_reader.analyze(img)
        if debug_frame_processing:
            log.debug(f"Sort sections analyzed (dt={time.monotonic() - sort_changed_start:.2f}s)")

        if sort_changed:
            bot.best_shuffle_sections = best_shuffle_sections
            bot.sort_values = sort_values
            bot.new_values = new_values
            best_shuffle_count = sum(bot.best_shuffle_sections)

            new_value_start = time.monotonic()
            await bot.new_value(
                bot.new_values,
                best_shuffle_count,
                timestamp=frame_received_at,
            )
            if debug_frame_processing:
                log.debug(
                    f"New value callbacks executed (dt={time.monotonic() - new_value_start:.2f}s)"
                )
        
        update_ocr_start = time.monotonic()
        await update_ocr_data(img, sort_changed=sort_changed)
        if debug_frame_processing:
            log.debug(f"OCR data updated (dt={time.monotonic() - update_ocr_start:.2f}s)")
        
        if bot.milestones:
            milestones_start = time.monotonic()
            await update_milestones(img, frame_received_at)
            if debug_frame_processing:
                log.debug(f"Milestones updated (dt={time.monotonic() - milestones_start:.2f}s)")

    @bot.init_callback
    async def start_api_stats_pipeline():
        nonlocal api_task

        if not api_enabled:
            return

        if api_task is not None and not api_task.done():
            return

        log.info(f"Using Bogostream stats API pipeline: {api_url}")
        api_task = asyncio.create_task(api_stats_loop())

    @bot.close_callback
    async def stop_api_stats_pipeline():
        if api_task is None:
            return

        api_task.cancel()
        try:
            await api_task
        except asyncio.CancelledError:
            pass

class SortSectionReader:
    def __init__(self, bot: BotCore, *, logger: Logger | None = None, debug: bool = False):
        self.last_signature: np.ndarray | None = None
        self.open_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        self.close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
        self.bot = bot
        self.logger = logger if logger is not None else bot.logger.getChild("Stats")
        self.debug = debug

    def analyze(
        self,
        img: Image.Image,
    ) -> tuple[bool, list[bool], list[int], list[tuple[bool, int]]]:
        crop = img.crop(self.bot.SORT_AREA_COORDS).convert("RGB")
        rgb = np.array(crop)
        sort_changed = self.test_changed(rgb)

        if not sort_changed:
            return False, [], [], []

        red_mask, green_mask = self.sort_colour_masks(rgb)
        best_shuffle_sections = self.read_best_shuffle_sections(red_mask, green_mask)
        sort_values = self.read_sort_values(red_mask | green_mask)
        new_values = list(zip(
            best_shuffle_sections,
            sort_values,
            strict=False,
        ))
        return True, best_shuffle_sections, sort_values, new_values

    def read_best_shuffle_sections(
        self,
        red_mask: np.ndarray,
        green_mask: np.ndarray,
    ) -> list[bool]:
        left, top, _right, _bottom = self.bot.SORT_AREA_COORDS
        strip_left, strip_top, strip_right, strip_bottom = (
            self.bot.SORT_OBSERVED_STRIP_COORDS
        )
        x1 = max(0, strip_left - left)
        y1 = max(0, strip_top - top)
        x2 = min(red_mask.shape[1], strip_right - left)
        y2 = min(red_mask.shape[0], strip_bottom - top)
        strip_red = red_mask[y1:y2, x1:x2]
        strip_green = green_mask[y1:y2, x1:x2]
        section_count = self.bot.SORT_SECTION_COUNT
        sections: list[bool] = []

        if strip_red.size == 0 or strip_green.size == 0:
            return [False] * section_count

        for index in range(section_count):
            section_x1 = round(index * strip_red.shape[1] / section_count)
            section_x2 = round((index + 1) * strip_red.shape[1] / section_count)
            red_pixels = strip_red[:, section_x1:section_x2].sum()
            green_pixels = strip_green[:, section_x1:section_x2].sum()
            sections.append(bool(green_pixels > red_pixels))

        if self.debug:
            self.logger.debug(
                f"Sort strip green sections={sum(sections)}/{section_count}"
            )
        return sections

    def read_sort_values(self, colour_mask: np.ndarray) -> list[int]:
        mask = (colour_mask * 255).astype(np.uint8)
        section_count = self.bot.SORT_SECTION_COUNT
        scores: list[tuple[int, int]] = []

        for index in range(section_count):
            x1 = round(index * mask.shape[1] / section_count)
            x2 = round((index + 1) * mask.shape[1] / section_count)
            width = x2 - x1
            trim = round(width * SORT_SECTION_SIDE_TRIM_RATIO)
            section_x1 = min(x2, x1 + trim)
            section_x2 = max(section_x1, x2 - trim)
            scores.append(self.solid_section_score(mask[:, section_x1:section_x2]))

        present_indices = [
            index
            for index, (height, area) in enumerate(scores)
            if height > 0 and area > 0
        ]
        values: list[int] = [0] * section_count

        for value, index in enumerate(
            sorted(present_indices, key=lambda current_index: scores[current_index]),
            start=1,
        ):
            values[index] = value

        if self.debug:
            self.logger.debug(f"Sort section scores={scores}, values={values}")
        return values

    def test_changed(self, sort_rgb: np.ndarray) -> bool:
        small = cv2.resize(
            sort_rgb,
            (160, 72),
            interpolation=cv2.INTER_AREA,
        ).astype(np.int16)
        red = (
            (small[:, :, 0] > small[:, :, 1] + 25) &
            (small[:, :, 0] > small[:, :, 2] + 25) &
            (small[:, :, 0] > 80)
        )
        green = (
            (small[:, :, 1] > small[:, :, 0] + 15) &
            (small[:, :, 1] > small[:, :, 2] + 15) &
            (small[:, :, 1] > 80)
        )

        signature = np.zeros(small.shape[:2], dtype=np.uint8)
        signature[red] = 1
        signature[green] = 2

        if self.last_signature is None:
            self.last_signature = signature
            return True

        changed_ratio: np.float64 = (
            np.count_nonzero(signature != self.last_signature) / signature.size
        )
        self.last_signature = signature

        changed = changed_ratio >= self.bot.SORT_CHANGE_THRESHOLD
        if self.debug:
            self.logger.debug(f"Sort visual delta={changed_ratio:.4f}, changed={changed}")
        return changed.item()

    def sort_colour_masks(self, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        rgb16 = rgb.astype(np.int16)
        red = (
            (rgb16[:, :, 0] > rgb16[:, :, 1] + 25) &
            (rgb16[:, :, 0] > rgb16[:, :, 2] + 25) &
            (rgb16[:, :, 0] > 70)
        )
        green = (
            (rgb16[:, :, 1] > rgb16[:, :, 0] + 10) &
            (rgb16[:, :, 1] > rgb16[:, :, 2] + 10) &
            (rgb16[:, :, 1] > 70)
        )
        return red, green

    def solid_section_score(self, mask: np.ndarray) -> tuple[int, int]:
        if mask.size == 0:
            return (0, 0)

        # Break hairline bridges/noise before measuring the main block.
        cleaned = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.open_kernel)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, self.close_kernel)

        _label_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            cleaned,
            connectivity=8,
        )
        if len(stats) <= 1:
            return (0, 0)

        min_area = max(8, round(mask.shape[0] * mask.shape[1] * 0.001))
        component_stats = [
            stats[index]
            for index in range(1, len(stats))
            if stats[index, cv2.CC_STAT_AREA] >= min_area
        ]
        if not component_stats:
            return (0, 0)

        largest = max(component_stats, key=lambda row: row[cv2.CC_STAT_AREA])
        height = int(largest[cv2.CC_STAT_HEIGHT])
        area = int(largest[cv2.CC_STAT_AREA])
        return (height, area)
