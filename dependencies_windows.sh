#!/bin/bash
# This script uses winget (Windows Package Manager) to install binaries.
# Run from an Administrative terminal if winget asks for elevation.
set -e

# Install system binaries.
winget install -e --id Python.Python.3.12
winget install -e --id UB-Mannheim.TesseractOCR
winget install -e --id Gyan.FFmpeg
winget install -e --id BytecodeAlliance.Wasmtime

# Install Python libraries.
# openai provides hosted or OpenAI-compatible AI action selection and replies.
py -3 -m pip install --upgrade pip
py -3 -m pip install discord.py openai numpy Pillow aiohttp streamlink opencv-python pyuca

# Verify libtesseract can be loaded by Python's ctypes path.
py -3 - <<'PY'
import ctypes
import ctypes.util
import os

candidate_paths = [
    ctypes.util.find_library("tesseract"),
    "libtesseract-5.dll",
    "libtesseract-4.dll",
    "libtesseract.dll",
    os.path.join(os.environ.get("ProgramFiles", ""), "Tesseract-OCR", "libtesseract-5.dll"),
    os.path.join(os.environ.get("ProgramFiles", ""), "Tesseract-OCR", "libtesseract-4.dll"),
    os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Tesseract-OCR", "libtesseract-5.dll"),
    os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Tesseract-OCR", "libtesseract-4.dll"),
]

errors = []
dll_directories = []
for path in candidate_paths:
    if not path:
        continue
    try:
        directory = os.path.dirname(path)
        if directory and os.path.isdir(directory) and hasattr(os, "add_dll_directory"):
            dll_directories.append(os.add_dll_directory(directory))
        ctypes.CDLL(path)
        print(f"libtesseract load check passed: {path}")
        break
    except OSError as e:
        errors.append(f"{path}: {e}")
else:
    raise SystemExit("Could not load libtesseract. Tried: " + "; ".join(errors))
PY

echo "If streamlink, ffmpeg, or tesseract are not found in a new shell, restart the terminal so PATH updates apply."
