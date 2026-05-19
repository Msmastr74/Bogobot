import asyncio

import cv2
import numpy as np
from PIL import Image

from ocr import (
    LibTesseractOCR,
    OcrBox,
    OcrCrop,
    preprocess_cell,
)


TEST_CROPS = {
    "shuffles": OcrCrop(81, 610, 312, 640, whitelist="0123456789,.KMBTQi"),
    "comparisons": OcrCrop(331, 610, 551, 640, whitelist="0123456789,.KMBTQi"),
    "best_run": OcrCrop(645, 610, 730, 640, whitelist="0123456789/"),
    "shuffles_sec": OcrCrop(819, 610, 1043, 640),
    "average_best_shuffle": OcrCrop(80, 670, 115, 685, whitelist="0123456789."),
    "uptime": OcrCrop(1150, 10, 1260, 30, whitelist="0123456789dhm "),
}


def print_boxes(label: str, boxes: list[OcrBox]) -> None:
    print(f"{label}:")
    if not boxes:
        print("  none")
        return

    for box in boxes:
        print(f"  {box.text!r} conf={box.confidence:.2f} box={box.box}")


def draw_boxes(
    image: np.ndarray,
    boxes: list[OcrBox],
    *,
    colour: tuple[int, int, int],
    label_prefix: str,
) -> None:
    for box in boxes:
        left, top, right, bottom = box.box
        cv2.rectangle(image, (left, top), (right, bottom), colour, 1)
        cv2.putText(
            image,
            f"{label_prefix}{box.text}",
            (left, max(10, top - 3)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            colour,
            1,
            cv2.LINE_AA,
        )


async def test_on_file(
    ocr: LibTesseractOCR,
    file_path: str,
    name: str,
    crop: OcrCrop,
) -> None:
    full_img = Image.open(file_path)
    cell_img = full_img.crop(crop.coords)

    scale = 3 if crop.scale is None else crop.scale
    threshold = 165 if crop.threshold is None else crop.threshold
    close = True if crop.close is None else crop.close
    dilate = True if crop.dilate is None else crop.dilate
    cleaned_img, preprocess_debug_img = preprocess_cell(
        cell_img,
        scale=scale,
        threshold=threshold,
        close=close,
        dilate=dilate,
        canonicalize_q="Q" in crop.whitelist,
        debug_draw=True,
    )
    print(cleaned_img.shape)
    original_cv = cv2.cvtColor(np.array(cell_img), cv2.COLOR_RGB2BGR)

    print(f"Testing file: {file_path}, stat={name}, crop={crop}")
    result = await ocr.parse_debug(
        cell_img,
        crop.whitelist,
        psm=7 if crop.psm is None else crop.psm,
        scale=scale,
        threshold=threshold,
        close=close,
        dilate=dilate,
    )
    print((result.text, result.confidence))
    print(f"raw={result.debug.raw!r}")
    print(f"filtered={result.debug.filtered!r}")
    print_boxes("words", result.debug.words)
    print_boxes("symbols", result.debug.symbols)

    boxed_img = preprocess_debug_img.copy()
    draw_boxes(boxed_img, result.debug.words, colour=(255, 0, 0), label_prefix="W:")
    draw_boxes(boxed_img, result.debug.symbols, colour=(0, 200, 0), label_prefix="")

    cv2.imshow("Original (Cropped)", original_cv)
    cv2.imshow("Processed OCR Input", cleaned_img)
    cv2.imshow("OCR Boxes", boxed_img)

    print("Press any key to continue...")
    cv2.waitKey(0)
    cv2.destroyAllWindows()


async def main() -> None:
    ocr = LibTesseractOCR(tessdata_path="tessdata")
    try:
        for name, crop in TEST_CROPS.items():
            await test_on_file(ocr, "live_720p.png", name, crop)
    finally:
        ocr.close()


if __name__ == "__main__":
    asyncio.run(main())
