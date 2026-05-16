import asyncio

import cv2
import numpy as np
from PIL import Image

from ocr import LibTesseractOCR, preprocess_cell


async def test_on_file(
    ocr: LibTesseractOCR,
    file_path: str,
    coords: tuple[int, int, int, int],
    whitelist: str = "0123456789,",
) -> None:
    x1, y1, x2, y2 = coords
    full_img = Image.open(file_path)
    cell_img = full_img.crop((x1, y1, x2, y2))

    width, height = x2 - x1, y2 - y1
    area = width * height
    cleaned_img = preprocess_cell(cell_img, scale=3 if area > 1500 else 6)
    print(cleaned_img.shape)
    original_cv = cv2.cvtColor(np.array(cell_img), cv2.COLOR_RGB2BGR)

    cv2.imshow("Original (Cropped)", original_cv)
    cv2.imshow("Processed OCR Input", cleaned_img)

    print(f"Testing file: {file_path}, coords={coords}")
    print(await ocr.parse(cell_img, whitelist, psm=7))

    print("Press any key to continue...")
    cv2.waitKey(0)
    cv2.destroyAllWindows()


async def main() -> None:
    ocr = LibTesseractOCR(tessdata_path="tessdata")
    try:
        await test_on_file(ocr, "live_720p.png", (1170, 665, 1195, 685))
        await test_on_file(ocr, "live_720p.png", (81, 610, 312, 640))
        await test_on_file(ocr, "live_720p.png", (331, 610, 551, 640))
        await test_on_file(ocr, "live_720p.png", (645, 610, 730, 640), "01234568789/")
        await test_on_file(ocr, "live_720p.png", (819, 610, 1043, 640))
    finally:
        ocr.close()


if __name__ == "__main__":
    asyncio.run(main())
