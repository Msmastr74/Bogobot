import asyncio
import concurrent.futures
import ctypes
import ctypes.util
import os
import queue
import threading
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal, overload
import logging

import cv2
import numpy as np
from PIL import Image

@dataclass(frozen=True)
class OcrCrop:
    left: int
    top: int
    right: int
    bottom: int
    whitelist: str = "0123456789,"
    psm: int | None = None
    scale: int | None = None
    threshold: int | None = None
    close: bool | None = None
    dilate: bool | None = None

    @property
    def coords(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom


OcrResult = tuple[str, float]


@dataclass(frozen=True)
class OcrBox:
    text: str
    confidence: float
    box: tuple[int, int, int, int]


@dataclass(frozen=True)
class OcrDebugInfo:
    raw: str
    filtered: str
    words: list[OcrBox]
    symbols: list[OcrBox]


@dataclass(frozen=True)
class OcrDebugResult:
    text: str
    confidence: float
    debug: OcrDebugInfo


OcrTaskResult = OcrResult | OcrDebugResult
OcrTask = tuple[concurrent.futures.Future[OcrTaskResult], Image.Image, str, int, int, int, bool, bool, bool] | None

TESSDATA_FAST_URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/eng.traineddata"
TESSERACT_LANGUAGE = "eng_fast"
RIL_WORD = 3
RIL_SYMBOL = 4

os.environ["OMP_THREAD_LIMIT"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OMP_WAIT_POLICY"] = "PASSIVE"

class LibTesseractOCR:
    def __init__(
        self,
        *,
        tessdata_path: str = "tessdata",
        tessdata_fast_url: str = TESSDATA_FAST_URL,
        language: str = TESSERACT_LANGUAGE,
        save_debug: bool = False,
        debug_folder: str = "ocr_debug",
        logger: logging.Logger | None = None,
        library_path: str | None = None,
        max_workers: int = 1,
    ):
        self.tessdata_path = os.path.abspath(tessdata_path)
        self.tessdata_fast_url = tessdata_fast_url
        self.language = language
        self.save_debug = save_debug
        self.debug_folder = debug_folder
        self.logger = logger
        self.max_workers = max(1, int(max_workers))
        self._state_lock = threading.Lock()
        self._closed = False
        self._dll_directories: list[Any] = []
        self._tasks: queue.Queue[OcrTask] = queue.Queue()
        self._workers: list[threading.Thread] = []

        self._ensure_tessdata_fast()
        self._lib = self._load_libtesseract(library_path)
        self._configure_libtesseract()

        ready_futures: list[concurrent.futures.Future[None]] = []
        for index in range(self.max_workers):
            ready: concurrent.futures.Future[None] = concurrent.futures.Future()
            worker = threading.Thread(
                target=self._worker_main,
                args=(ready,),
                name=f"BogobotOCR-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)
            ready_futures.append(ready)

        try:
            for ready in ready_futures:
                ready.result()
        except Exception:
            self.close()
            raise

    async def parse(
        self,
        pil_cell: Image.Image,
        whitelist: str,
        psm: int = 7,
        scale: int = 3,
        threshold: int = 165,
        close: bool = True,
        dilate: bool = True,
    ) -> OcrResult:
        result = await self._parse(
            pil_cell,
            whitelist,
            psm=psm,
            scale=scale,
            threshold=threshold,
            close=close,
            dilate=dilate,
            debug=False,
        )
        if isinstance(result, OcrDebugResult):
            return result.text, result.confidence
        return result

    async def parse_debug(
        self,
        pil_cell: Image.Image,
        whitelist: str,
        psm: int = 7,
        scale: int = 3,
        threshold: int = 165,
        close: bool = True,
        dilate: bool = True,
    ) -> OcrDebugResult:
        result = await self._parse(
            pil_cell,
            whitelist,
            psm=psm,
            scale=scale,
            threshold=threshold,
            close=close,
            dilate=dilate,
            debug=True,
        )
        if not isinstance(result, OcrDebugResult):
            raise RuntimeError("OCR debug result was not returned")
        return result

    async def _parse(
        self,
        pil_cell: Image.Image,
        whitelist: str,
        psm: int,
        scale: int,
        threshold: int,
        close: bool,
        dilate: bool,
        debug: bool,
    ) -> OcrTaskResult:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Tesseract OCR engine is closed")

            future: concurrent.futures.Future[OcrTaskResult] = concurrent.futures.Future()
            self._tasks.put((future, pil_cell, whitelist, int(psm), scale, threshold, close, dilate, debug))

        return await asyncio.wrap_future(future)

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True

            for _ in self._workers:
                self._tasks.put(None)

        for worker in self._workers:
            worker.join()

        self._workers.clear()

    def _parse_sync(
        self,
        api: ctypes.c_void_p,
        pil_cell: Image.Image,
        whitelist: str,
        psm: int,
        scale: int,
        threshold: int,
        close: bool,
        dilate: bool,
        debug: bool,
    ) -> OcrTaskResult:
        self._set_variable(api, "tessedit_char_whitelist", whitelist)
        self._lib.TessBaseAPISetPageSegMode(api, int(psm))
        return self._parse_cell_sync(api, pil_cell, whitelist, scale, threshold, close, dilate, debug)

    def _parse_cell_sync(
        self,
        api: ctypes.c_void_p,
        pil_cell: Image.Image,
        whitelist: str,
        scale: int,
        threshold: int,
        close: bool,
        dilate: bool,
        debug: bool,
    ) -> OcrTaskResult:
        processed = preprocess_cell(
            pil_cell,
            scale,
            threshold=threshold,
            close=close,
            dilate=dilate,
            canonicalize_q="Q" in whitelist
        )
        image = np.ascontiguousarray(processed, dtype=np.uint8)
        height, width = image.shape

        self._lib.TessBaseAPISetImage(
            api,
            image.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)),
            width,
            height,
            1,
            width,
        )

        if hasattr(self._lib, "TessBaseAPISetSourceResolution"):
            self._lib.TessBaseAPISetSourceResolution(api, 300)

        raw_text = self._lib.TessBaseAPIGetUTF8Text(api)
        try:
            text = ctypes.string_at(raw_text).decode(errors="ignore") if raw_text else ""
        finally:
            if raw_text:
                self._lib.TessDeleteText(raw_text)

        conf = self._lib.TessBaseAPIMeanTextConf(api) / 100.0
        out = "".join(char for char in text if char in whitelist)
        debug_info = self._ocr_debug_info(api, raw=text, filtered=out) if debug else None
        self._lib.TessBaseAPIClear(api)

        self._save_ocr_debug(processed, f"{conf:.2f}c_{out or text.strip()}")
        if debug_info is not None:
            return OcrDebugResult(out, conf, debug_info)
        return out, conf

    def _create_api(self) -> ctypes.c_void_p:
        api = self._lib.TessBaseAPICreate()
        if not api:
            raise RuntimeError("Could not create Tesseract API")

        rc = self._lib.TessBaseAPIInit3(
            api,
            self.tessdata_path.encode(),
            self.language.encode(),
        )
        if rc != 0:
            self._lib.TessBaseAPIDelete(api)
            raise RuntimeError(
                f"Could not initialize Tesseract language {self.language!r} "
                f"from {self.tessdata_path}"
            )

        self._set_variable(api, "load_system_dawg", "0")
        self._set_variable(api, "load_freq_dawg", "0")
        self._set_variable(api, "load_punc_dawg", "0")
        self._set_variable(api, "load_number_dawg", "0")
        self._set_variable(api, "invert_threshold", "0.0")
        return api

    def _worker_main(self, ready: concurrent.futures.Future[None]) -> None:
        api: ctypes.c_void_p | None = None
        try:
            api = self._create_api()
            ready.set_result(None)

            while True:
                task = self._tasks.get()
                if task is None:
                    return

                future, pil_cell, whitelist, psm, scale, threshold, close, dilate, debug = task
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(
                            self._parse_sync(api, pil_cell, whitelist, psm, scale, threshold, close, dilate, debug)
                        )
                    except Exception as e:
                        future.set_exception(e)
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)

            if self.logger:
                self.logger.exception("OCR worker failed")

            while True:
                try:
                    task = self._tasks.get_nowait()
                except queue.Empty:
                    break
                if task is not None:
                    task[0].set_exception(e)
        finally:
            if api:
                self._lib.TessBaseAPIEnd(api)
                self._lib.TessBaseAPIDelete(api)

    def _set_variable(self, api: ctypes.c_void_p, name: str, value: str) -> None:
        if self._lib.TessBaseAPISetVariable(
            api,
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
            "libtesseract-5.dll",
            "libtesseract-4.dll",
            "libtesseract.dll",
            os.path.join(os.environ.get("ProgramFiles", ""), "Tesseract-OCR", "libtesseract-5.dll"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Tesseract-OCR", "libtesseract-4.dll"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Tesseract-OCR", "libtesseract-5.dll"),
            os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Tesseract-OCR", "libtesseract-4.dll"),
        ]

        errors: list[str] = []
        for path in candidates:
            if not path:
                continue
            try:
                self._add_dll_directory(path)
                return ctypes.CDLL(path)
            except OSError as e:
                errors.append(f"{path}: {e}")

        details = "; ".join(errors) if errors else "no candidates found"
        raise RuntimeError(
            "Could not load libtesseract. Install the system Tesseract "
            "package or set libtesseract_path in config. "
            f"Tried: {details}"
        )

    def _add_dll_directory(self, path: str) -> None:
        if os.name != "nt" or not hasattr(os, "add_dll_directory"):
            return
        directory = os.path.dirname(path)
        if not directory or not os.path.isdir(directory):
            return
        self._dll_directories.append(os.add_dll_directory(directory))

    def _configure_libtesseract(self) -> None:
        self._lib.TessBaseAPICreate.argtypes = []
        self._lib.TessBaseAPICreate.restype = ctypes.c_void_p

        self._lib.TessBaseAPIEnd.argtypes = [ctypes.c_void_p]
        self._lib.TessBaseAPIEnd.restype = None

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

        if hasattr(self._lib, "TessBaseAPIGetIterator"):
            self._lib.TessBaseAPIGetIterator.argtypes = [ctypes.c_void_p]
            self._lib.TessBaseAPIGetIterator.restype = ctypes.c_void_p

        if hasattr(self._lib, "TessPageIteratorBegin"):
            self._lib.TessPageIteratorBegin.argtypes = [ctypes.c_void_p]
            self._lib.TessPageIteratorBegin.restype = None

        if hasattr(self._lib, "TessPageIteratorNext"):
            self._lib.TessPageIteratorNext.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._lib.TessPageIteratorNext.restype = ctypes.c_int

        if hasattr(self._lib, "TessPageIteratorBoundingBox"):
            self._lib.TessPageIteratorBoundingBox.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
            ]
            self._lib.TessPageIteratorBoundingBox.restype = ctypes.c_int

        if hasattr(self._lib, "TessResultIteratorGetUTF8Text"):
            self._lib.TessResultIteratorGetUTF8Text.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self._lib.TessResultIteratorGetUTF8Text.restype = ctypes.c_void_p

        if hasattr(self._lib, "TessResultIteratorConfidence"):
            self._lib.TessResultIteratorConfidence.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self._lib.TessResultIteratorConfidence.restype = ctypes.c_float

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
            c for c in text if c.isalnum() or c in (" ", "_", "-", ",", ".")
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

    def _ocr_debug_info(
        self,
        api: ctypes.c_void_p,
        *,
        raw: str,
        filtered: str,
    ) -> OcrDebugInfo:
        return OcrDebugInfo(
            raw=raw,
            filtered=filtered,
            words=self._ocr_iterator_boxes(api, RIL_WORD),
            symbols=self._ocr_iterator_boxes(api, RIL_SYMBOL),
        )

    def _ocr_iterator_boxes(self, api: ctypes.c_void_p, level: int) -> list[OcrBox]:
        required = (
            "TessBaseAPIGetIterator",
            "TessPageIteratorBegin",
            "TessPageIteratorNext",
            "TessPageIteratorBoundingBox",
            "TessResultIteratorGetUTF8Text",
            "TessResultIteratorConfidence",
        )
        if any(not hasattr(self._lib, name) for name in required):
            return []

        iterator = self._lib.TessBaseAPIGetIterator(api)
        if not iterator:
            return []

        self._lib.TessPageIteratorBegin(iterator)
        boxes: list[OcrBox] = []

        while True:
            text_pointer = self._lib.TessResultIteratorGetUTF8Text(iterator, level)
            text = ""
            try:
                if text_pointer:
                    text = ctypes.string_at(text_pointer).decode(errors="ignore").strip()
            finally:
                if text_pointer:
                    self._lib.TessDeleteText(text_pointer)

            left = ctypes.c_int()
            top = ctypes.c_int()
            right = ctypes.c_int()
            bottom = ctypes.c_int()
            has_box = self._lib.TessPageIteratorBoundingBox(
                iterator,
                level,
                ctypes.byref(left),
                ctypes.byref(top),
                ctypes.byref(right),
                ctypes.byref(bottom),
            )

            if text or has_box:
                confidence = self._lib.TessResultIteratorConfidence(iterator, level)
                boxes.append(
                    OcrBox(
                        text=text,
                        confidence=float(confidence) / 100,
                        box=(left.value, top.value, right.value, bottom.value),
                    )
                )

            if not self._lib.TessPageIteratorNext(iterator, level):
                break

        return boxes


def ocr_threshold_mask(
    pil_cell: Image.Image,
    scale: int = 3,
    threshold: int = 165,
    close: bool = True,
) -> np.ndarray:
    img = np.array(pil_cell.convert("L"))
    upscaled = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
    _, mask = cv2.threshold(upscaled, threshold, 255, cv2.THRESH_BINARY_INV)
    if close:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    return mask


def q_ellipse_stroke_score(
    mask: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    stroke_thickness: int = 13,
) -> float:
    details = q_ellipse_stroke_score_details(mask, x, y, w, h, stroke_thickness)
    return details[0]


def q_ellipse_stroke_score_details(
    mask: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    stroke_thickness: int = 13,
) -> tuple[float, int, int]:
    body_h = max(1, round(h * 0.72))
    glyph = (mask[y:y + body_h, x:x + w] == 0).astype(np.uint8) * 255
    if glyph.size == 0:
        return 0, 0, 0

    ellipse_mask = np.zeros_like(glyph)
    cv2.ellipse(
        ellipse_mask,
        (w // 2, body_h // 2),
        (max(1, round(w * 0.42)), max(1, round(body_h * 0.43))),
        0,
        0,
        360,
        255,
        thickness=1,
    )

    ellipse_pixels = cv2.countNonZero(ellipse_mask)
    if ellipse_pixels == 0:
        return 0, 0, 0

    glyph_nearby = cv2.dilate(glyph, np.ones((2, 2), np.uint8), iterations=1)
    intersection = cv2.bitwise_and(glyph_nearby, ellipse_mask)
    intersection_pixels = cv2.countNonZero(intersection)
    return intersection_pixels / ellipse_pixels, intersection_pixels, ellipse_pixels


@overload
def preprocess_cell(
    pil_cell: Image.Image,
    scale: int = 3,
    pad: int = 15,
    stroke_thickness: int = 13,
    threshold: int = 165,
    close: bool = True,
    dilate: bool = True,
    canonicalize_q: bool = False,
    *,
    debug_draw: Literal[False] = False,
) -> np.ndarray: ...

@overload
def preprocess_cell(
    pil_cell: Image.Image,
    scale: int = 3,
    pad: int = 15,
    stroke_thickness: int = 13,
    threshold: int = 165,
    close: bool = True,
    dilate: bool = True,
    canonicalize_q: bool = False,
    *,
    debug_draw: Literal[True],
) -> tuple[np.ndarray, np.ndarray]: ...

def preprocess_cell(
    pil_cell: Image.Image,
    scale: int = 3,
    pad: int = 15,
    stroke_thickness: int = 13,
    threshold: int = 165,
    close: bool = True,
    dilate: bool = True,
    canonicalize_q: bool = False,
    *,
    debug_draw: bool = False,
) -> np.ndarray | tuple[np.ndarray, np.ndarray]:
    # Scaling + thresholding. Keep gray gaps as background so nearby digits do not merge.
    mask = ocr_threshold_mask(
        pil_cell,
        scale=scale,
        threshold=threshold,
        close=close,
    )

    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)

    bw = np.ones_like(mask) * 255
    img_h, img_w = mask.shape
    image_area = img_h * img_w
    shells = []
    debug_marks: list[tuple[str, tuple[int, int, int, int], float]] = []

    def draw_inward_stroke(cnt: 'cv2.typing.MatLike') -> None:
        contour_mask = np.zeros_like(mask)
        cv2.drawContours(contour_mask, [cnt], -1, 255, thickness=-1)

        kernel = np.ones((stroke_thickness, stroke_thickness), np.uint8)
        inner = cv2.erode(contour_mask, kernel, iterations=1)
        stroke = cv2.subtract(contour_mask, inner)
        bw[stroke > 0] = 0

    def draw_q_tail(x: int, y: int, w: int, h: int) -> None:
        start = (x + round(w * 0.50), y + round(h * 0.58))
        end = (x + round(w * 0.86), y + round(h * 0.95))
        thickness = max(3, stroke_thickness // 3)
        cv2.line(bw, start, end, 0, thickness=thickness, lineType=cv2.LINE_AA)

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
            q_score = q_ellipse_stroke_score(mask, x, y, w, h, stroke_thickness) if canonicalize_q else 0
            if q_score > 0.93:
                cv2.drawContours(bw, [cnt], -1, 0, thickness=-1)
                draw_q_tail(x, y, w, h)
                shells.append({"box": (x, y, w, h), "type": "normal"})
                debug_marks.append(("q", (x, y, w, h), q_score))
            elif ellipse_score > 0.88 and solidity > 0.94:
                draw_inward_stroke(cnt)
                shells.append({"box": (x, y, w, h), "type": "zero"})
                debug_marks.append(("z", (x, y, w, h), q_score))
            else:
                cv2.drawContours(bw, [cnt], -1, 0, thickness=-1)
                shells.append({"box": (x, y, w, h), "type": "normal"})
                debug_marks.append(("n", (x, y, w, h), q_score))

    bw = cv2.copyMakeBorder(bw, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
    if dilate:
        bw = cv2.dilate(
            bw,
            np.array(
                dtype=np.uint8,
                object=[
                    [2, 1, 2],
                    [2, 1, 2],
                    [2, 1, 2],
                ],
            ),
            iterations=1,
        )

    if not debug_draw:
        return bw

    debug_img = cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR)
    for branch, (x, y, w, h), q_score in debug_marks:
        draw_x = x + pad
        draw_y = y + pad
        body_h = max(1, round(h * 0.72))
        colour = {
            "q": (0, 0, 255),
            "z": (255, 0, 0),
            "n": (0, 180, 180),
        }[branch]
        cv2.rectangle(debug_img, (draw_x, draw_y), (draw_x + w, draw_y + h), colour, 1)
        cv2.rectangle(debug_img, (draw_x, draw_y), (draw_x + w, draw_y + body_h), (0, 0, 180), 1)
        cv2.ellipse(
            debug_img,
            (draw_x + w // 2, draw_y + body_h // 2),
            (max(1, round(w * 0.42)), max(1, round(body_h * 0.43))),
            0,
            0,
            360,
            (0, 0, 255),
            1,
        )
        cv2.putText(
            debug_img,
            f"{branch}:{q_score:.2f}",
            (draw_x, max(10, draw_y - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            colour,
            1,
            cv2.LINE_AA,
        )

    return bw, debug_img
