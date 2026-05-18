import numpy as np
import time
from decimal import Decimal, InvalidOperation
from PIL import Image

from bogobot_core import BotCore
from ocr import OcrCrop, OcrResult
import asyncio
import cv2

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


async def setup(bot: BotCore):
    async def update_ocr_data(img: Image.Image, *, sort_changed: bool = True) -> None:
        try:
            async def parse_crops(crops: list[OcrCrop]) -> list[OcrResult]:
                async def parse_crop(crop: OcrCrop) -> OcrResult:
                    return await bot.ocr.parse(
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

    async def update_milestones(img: Image.Image, frame_received_at: float):
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
