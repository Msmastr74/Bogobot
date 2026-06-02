import numpy as np
import time
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict
from PIL import Image

import aiohttp
from bogobot_core import BotCore
from ocr import OcrCrop, OcrResult
import asyncio
import cv2

BOGOSTREAM_STATS_API_URL = "https://bogo.swapjs.dev/api/stats"
BOGOSTREAM_STATS_API_INTERVAL_SECONDS = 1.0

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


class BogostreamRecordHolder(TypedDict):
    nickname: str
    value: int


class BogostreamStats(TypedDict):
    engine_total: int
    crowd_total: int
    combined_total: int
    engine_rate: int
    crowd_rate: int
    combined_rate: int
    best: int
    best_at: int | None
    tick_best: int
    tick_best_arr: list[int]
    tick_best_source: str
    active_contributors: int
    record_holder: BogostreamRecordHolder | None
    uptime_s: int | None
    contributions_open: bool | None
    solve_confirmed: bool | None


def parse_number(value: str | None) -> Decimal | None:
    if not value:
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
    stats_source = str(bot.config.get("stats_source", "api")).lower()
    api_enabled = stats_source in {"api", "event", "events"}
    api_url = str(bot.config.get("bogostream_stats_api_url", BOGOSTREAM_STATS_API_URL))
    api_interval = max(
        0.25,
        float(bot.config.get(
            "bogostream_stats_api_interval",
            BOGOSTREAM_STATS_API_INTERVAL_SECONDS,
        )),
    )
    api_task: asyncio.Task[None] | None = None
    last_api_sort_values: list[int] | None = None

    def format_count(value: int | str) -> str:
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return "Loading..."

    def normalize_api_stats(raw: Any) -> BogostreamStats | None:
        if not isinstance(raw, dict):
            return None

        def source_text(value: object) -> str:
            text = str(value or "vps")
            return "vps" if text == "engine" else text

        try:
            engine = raw.get("engine")
            crowd = raw.get("crowd")
            combined_tick = raw.get("combined_tick")

            if (
                isinstance(engine, dict)
                and isinstance(crowd, dict)
                and isinstance(combined_tick, dict)
            ):
                tick_best_arr_raw = combined_tick["best_arr"]
                if not isinstance(tick_best_arr_raw, list):
                    return None

                record_holder_raw = raw.get("record_holder")
                record_holder: BogostreamRecordHolder | None = None
                if isinstance(record_holder_raw, dict):
                    record_holder = {
                        "nickname": str(record_holder_raw.get("nickname", "unknown")),
                        "value": int(record_holder_raw.get("value", 0)),
                    }

                return {
                    "engine_total": int(engine["total"]),
                    "crowd_total": int(crowd["total_shuffles"]),
                    "combined_total": int(raw.get("combined_total", int(engine["total"]) + int(crowd["total_shuffles"]))),
                    "engine_rate": round(float(engine["rate"])),
                    "crowd_rate": round(float(crowd["rate"])),
                    "combined_rate": round(float(raw["combined_rate"])),
                    "best": int(raw.get("record", engine.get("best", 0))),
                    "best_at": int(engine["best_at"]) if "best_at" in engine else None,
                    "tick_best": int(combined_tick["best"]),
                    "tick_best_arr": [int(value) for value in tick_best_arr_raw],
                    "tick_best_source": source_text(combined_tick.get("source")),
                    "active_contributors": int(crowd.get("active", 0)),
                    "record_holder": record_holder,
                    "uptime_s": int(engine["uptime_s"]) if "uptime_s" in engine else None,
                    "contributions_open": bool(raw["contributions_open"]) if "contributions_open" in raw else None,
                    "solve_confirmed": bool(raw["solve_confirmed"]) if "solve_confirmed" in raw else None,
                }

            tick_best_arr_raw = raw["tick_best_arr"]
            if not isinstance(tick_best_arr_raw, list):
                return None

            record_holder_raw = raw.get("record_holder")
            record_holder: BogostreamRecordHolder | None = None
            if isinstance(record_holder_raw, dict):
                record_holder = {
                    "nickname": str(record_holder_raw.get("nickname", "unknown")),
                    "value": int(record_holder_raw.get("value", 0)),
                }

            source = str(raw.get("tick_best_source", "vps"))
            return {
                "engine_total": int(raw["engine_total"]),
                "crowd_total": int(raw["crowd_total"]),
                "combined_total": int(raw.get("combined_total", int(raw["engine_total"]) + int(raw["crowd_total"]))),
                "engine_rate": int(raw["engine_rate"]),
                "crowd_rate": int(raw["crowd_rate"]),
                "combined_rate": int(raw["combined_rate"]),
                "best": int(raw["best"]),
                "best_at": int(raw["best_at"]) if "best_at" in raw else None,
                "tick_best": int(raw["tick_best"]),
                "tick_best_arr": [int(value) for value in tick_best_arr_raw],
                "tick_best_source": source_text(source),
                "active_contributors": int(raw["active_contributors"]),
                "record_holder": record_holder,
                "uptime_s": None,
                "contributions_open": bool(raw["contributions_open"]) if "contributions_open" in raw else None,
                "solve_confirmed": bool(raw["solve_confirmed"]) if "solve_confirmed" in raw else None,
            }
        except (KeyError, TypeError, ValueError):
            return None

    def sections_from_sort_values(sort_values: list[int]) -> list[bool]:
        return [
            value == index + 1
            for index, value in enumerate(sort_values[:bot.SORT_SECTION_COUNT])
        ]

    def apply_api_stats(data: BogostreamStats, timestamp: float) -> tuple[list[tuple[bool, int]], int] | None:
        sort_values = data["tick_best_arr"][:bot.SORT_SECTION_COUNT]
        if len(sort_values) < bot.SORT_SECTION_COUNT:
            sort_values.extend([0] * (bot.SORT_SECTION_COUNT - len(sort_values)))

        best_shuffle_sections = sections_from_sort_values(sort_values)
        new_values = list(zip(best_shuffle_sections, sort_values, strict=False))
        best_count = max(0, min(bot.SORT_SECTION_COUNT, int(data["tick_best"])))
        combined_total = data["combined_total"]
        record_holder = data["record_holder"]
        record_holder_text = (
            f"{record_holder['nickname']} ({record_holder['value']}/{bot.SORT_SECTION_COUNT})"
            if record_holder is not None else
            "engine"
        )
        uptime = data["uptime_s"]

        bot.stats.update({
            "shuffles": format_count(combined_total),
            "engine_total": format_count(data["engine_total"]),
            "crowd_total": format_count(data["crowd_total"]),
            "combined_total": format_count(combined_total),
            "comparisons": "N/A",
            "best_run": f"{data['best']}/{bot.SORT_SECTION_COUNT}",
            "best_at": format_count(data["best_at"]) if data["best_at"] is not None else "N/A",
            "tick_best": f"{data['tick_best']}/{bot.SORT_SECTION_COUNT}",
            "tick_best_source": data["tick_best_source"],
            "shuffles_sec": format_count(data["combined_rate"]),
            "engine_rate": format_count(data["engine_rate"]),
            "crowd_rate": format_count(data["crowd_rate"]),
            "active_contributors": format_count(data["active_contributors"]),
            "record_holder": record_holder_text,
            "contributions_open": "Yes" if data["contributions_open"] else "No" if data["contributions_open"] is False else "Unknown",
            "solve_confirmed": "Yes" if data["solve_confirmed"] else "No" if data["solve_confirmed"] is False else "Unknown",
            "average_best_shuffle": "N/A",
            "uptime": format_duration(uptime) if uptime is not None else "N/A",
        })
        bot._last_ocr_refresh = timestamp
        bot.best_shuffle_sections = best_shuffle_sections
        bot.sort_values = sort_values
        bot.new_values = new_values
        return new_values, best_count

    async def fetch_api_stats(session: aiohttp.ClientSession) -> BogostreamStats | None:
        async with session.get(api_url) as response:
            if response.status != 200:
                bot.logger.warning(f"Bogostream stats API returned HTTP {response.status}")
                return None
            data = normalize_api_stats(await response.json())
            if data is None:
                bot.logger.warning("Bogostream stats API returned an unexpected payload shape")
            return data

    async def api_stats_loop() -> None:
        nonlocal last_api_sort_values

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
                        sort_values = bot.sort_values
                        if event is not None and sort_values != last_api_sort_values:
                            last_api_sort_values = list(sort_values)
                            new_values, best_count = event
                            await bot.new_value(new_values, best_count, timestamp=timestamp)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    bot.logger.exception("Bogostream stats API update failed")

                await asyncio.sleep(api_interval)

    async def update_ocr_data(img: Image.Image, *, sort_changed: bool = True) -> None:
        ocr = bot.ocr
        if ocr is None:
            bot.logger.warning("OCR stats source is selected, but OCR is disabled. Set `ocr_enabled` to true to use OCR.")
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
            bot.logger.exception("OCR processing error")

    async def update_milestones(img: Image.Image | None, frame_received_at: float):
        if bot.milestones is None:
            return

        frame_timestamp = int(frame_received_at)
        stats = bot.stats
        best_run = stats.get("best_run")
        if best_run:
            await bot.milestones.update("Best run", best_run, timestamp=frame_timestamp, img=img)

        for milestone_name, stat_name in (
            ("Shuffles", "shuffles"),
            ("Comparisons", "comparisons"),
        ):
            stat_value = round_stat_down_to_power(stats.get(stat_name))
            if stat_value:
                await bot.milestones.update(milestone_name, stat_value, timestamp=frame_timestamp, img=img)

        shuffles_sec = round_stat_down_to_power(stats.get("shuffles_sec"))
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

    def round_stat_down_to_power(value: str | None) -> str | None:
        number = parse_number(value)
        if number is None:
            return None

        number = int(number)
        if number <= 0:
            return None

        power = 10 ** (len(str(number)) - 1)
        return f"{number // power * power:,}"

    def round_stat_down_to_int(value: str | None) -> str | None:
        number = parse_number(value)
        if number is None:
            return None

        return f"{int(number):,}"

    sort_reader = SortSectionReader(bot)

    last_frame_monotonic = time.monotonic()
    @bot.new_frame_callback
    async def new_frame(img: Image.Image):
        nonlocal last_frame_monotonic
        frame_received_at = time.time()

        frame_received_monotonic = time.monotonic()
        dt = frame_received_monotonic - last_frame_monotonic
        last_frame_monotonic = frame_received_monotonic
        bot.logger.debug(f"New frame received (dt={dt:.2f}s)")
        
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
        bot.logger.debug(f"Sort sections analyzed (dt={time.monotonic() - sort_changed_start:.2f}s)")

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
            bot.logger.debug(
                f"New value callbacks executed (dt={time.monotonic() - new_value_start:.2f}s)"
            )
        
        update_ocr_start = time.monotonic()
        await update_ocr_data(img, sort_changed=sort_changed)
        bot.logger.debug(f"OCR data updated (dt={time.monotonic() - update_ocr_start:.2f}s)")
        
        if bot.milestones:
            milestones_start = time.monotonic()
            await update_milestones(img, frame_received_at)
            bot.logger.debug(f"Milestones updated (dt={time.monotonic() - milestones_start:.2f}s)")

    @bot.init_callback
    async def start_api_stats_pipeline():
        nonlocal api_task

        if not api_enabled:
            return

        if api_task is not None and not api_task.done():
            return

        bot.logger.info(f"Using Bogostream stats API pipeline: {api_url}")
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
    def __init__(self, bot: BotCore):
        self.last_signature: np.ndarray | None = None
        self.open_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        self.close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3))
        self.bot = bot

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

        self.bot.logger.debug(
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
            scores.append(self.solid_section_score(mask[:, x1:x2]))

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

        self.bot.logger.debug(f"Sort section scores={scores}, values={values}")
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
        self.bot.logger.debug(f"Sort visual delta={changed_ratio:.4f}, changed={changed}")
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
