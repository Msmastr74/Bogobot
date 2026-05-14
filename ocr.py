import asyncio
import ctypes
import ctypes.util
import os
import threading
import urllib.request
from typing import Any

import cv2
import numpy as np
from PIL import Image

OcrCrop = tuple[tuple[int, int, int, int], str, int | None]
OcrResult = tuple[str, float]

TESSDATA_FAST_URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/eng.traineddata"
TESSERACT_LANGUAGE = "eng_fast"


class LibTesseractOCR:
    def __init__(
        self,
        *,
        tessdata_path: str = "tessdata",
        tessdata_fast_url: str = TESSDATA_FAST_URL,
        language: str = TESSERACT_LANGUAGE,
        save_debug: bool = False,
        debug_folder: str = "ocr_debug",
        logger: Any = None,
        library_path: str | None = None,
    ):
        self.tessdata_path = os.path.abspath(tessdata_path)
        self.tessdata_fast_url = tessdata_fast_url
        self.language = language
        self.save_debug = save_debug
        self.debug_folder = debug_folder
        self.logger = logger
        self._api_lock = threading.Lock()
        self._closed = False

        self._ensure_tessdata_fast()
        self._lib = self._load_libtesseract(library_path)
        self._configure_libtesseract()
        self._api = self._lib.TessBaseAPICreate()
        if not self._api:
            raise RuntimeError("Could not create Tesseract API")

        rc = self._lib.TessBaseAPIInit3(
            self._api,
            self.tessdata_path.encode(),
            self.language.encode(),
        )
        if rc != 0:
            self._lib.TessBaseAPIDelete(self._api)
            self._api = None
            raise RuntimeError(
                f"Could not initialize Tesseract language {self.language!r} "
                f"from {self.tessdata_path}"
            )

        self._set_variable("load_system_dawg", "0")
        self._set_variable("load_freq_dawg", "0")

    async def parse_batch(
        self,
        pil_cells: list[Image.Image],
        whitelist: str,
        psm: int = 7,
    ) -> list[OcrResult]:
        if not pil_cells:
            return []

        return await asyncio.to_thread(
            self._parse_batch_sync,
            pil_cells,
            whitelist,
            psm,
        )

    def close(self) -> None:
        with self._api_lock:
            if self._closed:
                return
            self._closed = True
            api = getattr(self, "_api", None)
            if api:
                self._lib.TessBaseAPIDelete(api)
                self._api = None

    def _parse_batch_sync(
        self,
        pil_cells: list[Image.Image],
        whitelist: str,
        psm: int,
    ) -> list[OcrResult]:
        with self._api_lock:
            if self._closed or not self._api:
                raise RuntimeError("Tesseract OCR engine is closed")

            self._set_variable("tessedit_char_whitelist", whitelist)
            self._lib.TessBaseAPISetPageSegMode(self._api, int(psm))

            return [
                self._parse_cell_sync(pil_cell, whitelist)
                for pil_cell in pil_cells
            ]

    def _parse_cell_sync(self, pil_cell: Image.Image, whitelist: str) -> OcrResult:
        processed = preprocess_cell(pil_cell)
        image = np.ascontiguousarray(processed, dtype=np.uint8)
        height, width = image.shape

        self._lib.TessBaseAPISetImage(
            self._api,
            image.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)),
            width,
            height,
            1,
            width,
        )

        if hasattr(self._lib, "TessBaseAPISetSourceResolution"):
            self._lib.TessBaseAPISetSourceResolution(self._api, 300)

        raw_text = self._lib.TessBaseAPIGetUTF8Text(self._api)
        try:
            text = ctypes.string_at(raw_text).decode(errors="ignore") if raw_text else ""
        finally:
            if raw_text:
                self._lib.TessDeleteText(raw_text)

        conf = self._lib.TessBaseAPIMeanTextConf(self._api) / 100.0
        self._lib.TessBaseAPIClear(self._api)

        out = "".join(char for char in text if char in whitelist)
        self._save_ocr_debug(processed, f"{conf:.2f}c_{out or text.strip()}")
        return out, conf

    def _set_variable(self, name: str, value: str) -> None:
        if self._lib.TessBaseAPISetVariable(
            self._api,
            name.encode(),
            value.encode(),
        ) == 0:
            raise RuntimeError(f"Could not set Tesseract variable {name}")

    def _ensure_tessdata_fast(self) -> None:
        tessdata_file = os.path.join(
            self.tessdata_path,
            f"{self.language}.traineddata",
        )

        if os.path.exists(tessdata_file) and os.path.getsize(tessdata_file) > 0:
            return

        os.makedirs(self.tessdata_path, exist_ok=True)
        tmp_path = f"{tessdata_file}.tmp"

        if self.logger:
            self.logger.info(
                f"Downloading {self.language}.traineddata to {self.tessdata_path}"
            )

        try:
            with urllib.request.urlopen(self.tessdata_fast_url, timeout=30) as response:
                status = getattr(response, "status", 200)
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")

                with open(tmp_path, "wb") as f:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)

            os.replace(tmp_path, tessdata_file)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def _load_libtesseract(self, library_path: str | None):
        candidates = [
            library_path,
            ctypes.util.find_library("tesseract"),
            "libtesseract.so",
            "libtesseract.dylib",
        ]

        errors: list[str] = []
        for path in candidates:
            if not path:
                continue
            try:
                return ctypes.CDLL(path)
            except OSError as e:
                errors.append(f"{path}: {e}")

        details = "; ".join(errors) if errors else "no candidates found"
        raise RuntimeError(
            "Could not load libtesseract. Install the system Tesseract "
            "package or set libtesseract_path in config. "
            f"Tried: {details}"
        )

    def _configure_libtesseract(self) -> None:
        self._lib.TessBaseAPICreate.argtypes = []
        self._lib.TessBaseAPICreate.restype = ctypes.c_void_p

        self._lib.TessBaseAPIDelete.argtypes = [ctypes.c_void_p]
        self._lib.TessBaseAPIDelete.restype = None

        self._lib.TessBaseAPIInit3.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        self._lib.TessBaseAPIInit3.restype = ctypes.c_int

        self._lib.TessBaseAPISetVariable.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        self._lib.TessBaseAPISetVariable.restype = ctypes.c_int

        self._lib.TessBaseAPISetPageSegMode.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self._lib.TessBaseAPISetPageSegMode.restype = None

        self._lib.TessBaseAPISetImage.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._lib.TessBaseAPISetImage.restype = None

        self._lib.TessBaseAPIGetUTF8Text.argtypes = [ctypes.c_void_p]
        self._lib.TessBaseAPIGetUTF8Text.restype = ctypes.c_void_p

        self._lib.TessBaseAPIMeanTextConf.argtypes = [ctypes.c_void_p]
        self._lib.TessBaseAPIMeanTextConf.restype = ctypes.c_int

        self._lib.TessBaseAPIClear.argtypes = [ctypes.c_void_p]
        self._lib.TessBaseAPIClear.restype = None

        self._lib.TessDeleteText.argtypes = [ctypes.c_void_p]
        self._lib.TessDeleteText.restype = None

        if hasattr(self._lib, "TessBaseAPISetSourceResolution"):
            self._lib.TessBaseAPISetSourceResolution.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self._lib.TessBaseAPISetSourceResolution.restype = None

    def _save_ocr_debug(
        self,
        processed: np.ndarray,
        text: str,
        max_files: int = 30,
    ) -> None:
        if not self.save_debug:
            return

        success, buffer = cv2.imencode(".png", processed)
        if not success:
            return

        os.makedirs(self.debug_folder, exist_ok=True)
        safe_text = "".join(
            c for c in text if c.isalnum() or c in (" ", "_", "-", ",")
        ).rstrip()
        new_filename = f"ocr_{safe_text}.png"
        new_path = os.path.join(self.debug_folder, new_filename)

        files: list[os.DirEntry[str]] = []
        with os.scandir(self.debug_folder) as entries:
            for entry in entries:
                try:
                    if entry.is_file() and entry.name.startswith("ocr_"):
                        files.append(entry)
                except FileNotFoundError:
                    continue

        if len(files) >= max_files:
            oldest: os.DirEntry[str] | None = None
            oldest_mtime = float("inf")
            for entry in files:
                try:
                    mtime = entry.stat().st_mtime
                    if mtime < oldest_mtime:
                        oldest, oldest_mtime = entry, mtime
                except FileNotFoundError:
                    continue

            if oldest is not None:
                try:
                    os.remove(oldest.path)
                except FileNotFoundError:
                    pass

        with open(new_path, "wb") as f:
            f.write(buffer.tobytes())


def preprocess_cell(
    pil_cell: Image.Image,
    scale: int = 5,
    pad: int = 10,
    stroke_thickness: int = 15,
    threshold: int = 165,
) -> np.ndarray:
    # Scaling + thresholding. Keep gray gaps as background so nearby digits do not merge.
    img = np.array(pil_cell.convert("L"))
    upscaled = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
    _, mask = cv2.threshold(upscaled, threshold, 255, cv2.THRESH_BINARY_INV)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)

    bw = np.ones_like(mask) * 255
    img_h, img_w = mask.shape
    image_area = img_h * img_w
    shells = []

    def draw_inward_stroke(cnt: np.ndarray) -> None:
        contour_mask = np.zeros_like(mask)
        cv2.drawContours(contour_mask, [cnt], -1, 255, thickness=-1)

        kernel = np.ones((stroke_thickness, stroke_thickness), np.uint8)
        inner = cv2.erode(contour_mask, kernel, iterations=1)
        stroke = cv2.subtract(contour_mask, inner)
        bw[stroke > 0] = 0

    for cnt in contours:
        area = cv2.contourArea(cnt)
        x, y, w, h = cv2.boundingRect(cnt)

        if area > (image_area * 0.9):
            continue

        parent_shell = None
        for shell in shells:
            sx, sy, sw, sh = shell["box"]
            if x >= sx - 2 and y >= sy - 2 and (x + w) <= (sx + sw + 2) and (y + h) <= (sy + sh + 2):
                parent_shell = shell
                break

        if parent_shell and parent_shell["type"] == "zero":
            continue

        norm_scale = 100.0 / h if h > 0 else 1
        cnt_norm = ((cnt.astype(np.float32) - [x, y]) * norm_scale).astype(np.float32)

        ellipse_score = 0
        if len(cnt_norm) >= 5:
            _, (major_axis, minor_axis), _ = cv2.fitEllipse(cnt_norm)
            ellipse_area = (np.pi * major_axis * minor_axis) / 4.0
            ellipse_score = cv2.contourArea(cnt_norm) / ellipse_area if ellipse_area > 0 else 0

        hull = cv2.convexHull(cnt)
        hull_area = cv2.contourArea(hull)
        solidity = area / hull_area if hull_area > 0 else 0

        if parent_shell:
            cv2.drawContours(bw, [cnt], -1, 255, thickness=-1)
        else:
            if ellipse_score > 0.88 and solidity > 0.94:
                draw_inward_stroke(cnt)
                shells.append({"box": (x, y, w, h), "type": "zero"})
            else:
                cv2.drawContours(bw, [cnt], -1, 0, thickness=-1)
                shells.append({"box": (x, y, w, h), "type": "normal"})

    bw = cv2.copyMakeBorder(bw, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
    return cv2.dilate(bw, np.ones((2, 2), np.uint8), iterations=1)
