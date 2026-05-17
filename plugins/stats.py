from typing import TYPE_CHECKING, cast
import numpy as np
import time
from PIL import Image

if TYPE_CHECKING:
    from main import BotCore
from ocr import OcrCrop, OcrResult
import asyncio
import cv2

async def setup(bot: 'BotCore'):
    last_sort_signature: np.ndarray | None = None

    async def update_ocr_data(img: Image.Image, *, sort_changed: bool = True) -> None:
        try:
            async def parse_crops(crops: list[OcrCrop]) -> list[OcrResult]:
                async def parse_crop(crop: OcrCrop) -> OcrResult:
                    coords, whitelist, psm = crop
                    
                    width, height = coords[2] - coords[0], coords[3] - coords[1]
                    area = width * height
                    return await bot.ocr.parse(
                        img.crop(coords),
                        whitelist,
                        psm=7 if psm is None else psm,
                        scale=3 if area > 1500 else 6
                    )

                return await asyncio.gather(*[
                    parse_crop(crop)
                    for crop in crops
                ])

            crops: list[OcrCrop] = []
            stats_specs: list[tuple[int, str, str]] = []
            cell_indexes: list[int] = []

            if sort_changed:
                bot.current_vals = []

            for name, coords in bot.STATS_COORDS.items():
                whitelist = "0123456789,"
                psm: int | None = None
                if len(coords) >= 5:
                    extra = coords[4]
                    if isinstance(extra, str):
                        whitelist = extra
                    else:
                        w, psm = extra
                        if w is not None:
                            whitelist = w
                        if psm is not None:
                            psm = int(psm)
                crop_coords = cast(tuple[int, int, int, int], coords[:4])

                stats_specs.append((len(crops), name, whitelist))
                crops.append((crop_coords, whitelist, psm))

            if sort_changed:
                coords = bot.CELL_COORDS
                for _ in range(bot.OCR_CELL_COUNT):
                    cell_indexes.append(len(crops))
                    crops.append((coords, "0123456789", None))
                    coords = (
                        coords[0] - bot.CELL_OFFSET, coords[1],
                        coords[2] - bot.CELL_OFFSET, coords[3]
                    )

            results = await parse_crops(crops)

            for index, name, whitelist in stats_specs:
                text, conf = results[index]
                if not text or conf < 0:
                    continue

                bot.stats[name] = text

            if sort_changed:
                bot.current_vals = [results[index] for index in cell_indexes]
                bot.current_vals.reverse()
                bot._current_vals_updated = True

            bot._last_ocr_refresh = time.time()
        except Exception as e:
            bot.logger.warning(f"OCR processing error: {e}")

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
        current_number = parse_stat_value(current_value)
        next_number = parse_stat_value(milestone_value)

        if (
            current_number is not None
            and next_number is not None
            and next_number < current_number
        ):
            return None

        return await bot.milestones.update(milestone_name, milestone_value, timestamp=timestamp, img=img)

    def parse_stat_value(value: str | None) -> float | None:
        if not value:
            return None

        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None

    def round_stat_down_to_power(value: str | None) -> str | None:
        number = parse_stat_value(value)
        if number is None:
            return None

        number = int(number)
        if number <= 0:
            return None

        power = 10 ** (len(str(number)) - 1)
        return f"{number // power * power:,}"

    def round_stat_down_to_int(value: str | None) -> str | None:
        number = parse_stat_value(value)
        if number is None:
            return None

        return f"{int(number):,}"

    def test_sort_changed(img: Image.Image) -> bool:
        nonlocal last_sort_signature
        crop = img.crop(bot.SORT_AREA_COORDS).convert("RGB")
        rgb = np.array(crop)
        small = cv2.resize(rgb, (160, 72), interpolation=cv2.INTER_AREA).astype(np.int16)

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

        if last_sort_signature is None:
            last_sort_signature = signature
            return True

        changed_ratio: np.float64 = np.count_nonzero(signature != last_sort_signature) / signature.size
        last_sort_signature = signature

        changed = changed_ratio >= bot.SORT_CHANGE_THRESHOLD
        bot.logger.debug(f"Sort visual delta={changed_ratio:.4f}, changed={changed}")
        return changed.item()

    last_frame_monotonic = time.monotonic()
    async def on_new_frame(img: Image.Image):
        nonlocal last_frame_monotonic
        frame_received_at = time.time()

        frame_received_monotonic = time.monotonic()
        dt = frame_received_monotonic - last_frame_monotonic
        last_frame_monotonic = frame_received_monotonic
        bot.logger.debug(f"New frame received (dt={dt:.2f}s)")
        
        if bot.config.get("save_live_frame", False):
            img.save("live_720p.png", format="PNG")
        
        sort_changed_start = time.monotonic()
        sort_changed = test_sort_changed(img)
        bot.logger.debug(f"Sort changed test (dt={time.monotonic() - sort_changed_start:.2f}s)")
        
        update_ocr_start = time.monotonic()
        await update_ocr_data(img, sort_changed=sort_changed)
        bot.logger.debug(f"OCR data updated (dt={time.monotonic() - update_ocr_start:.2f}s)")
        
        if bot.milestones:
            milestones_start = time.monotonic()
            await update_milestones(img, frame_received_at)
            bot.logger.debug(f"Milestones updated (dt={time.monotonic() - milestones_start:.2f}s)")
