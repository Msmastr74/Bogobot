#!/bin/bash
# For use within the Termux environment
set -e

# Update and upgrade Termux packages
pkg update && pkg upgrade -y

# Install core binaries
# Termux's tesseract package provides the libtesseract shared library used by ocr.py
pkg install python tesseract ffmpeg clang make pkg-config -y

# OpenCV is provided as a Termux package; pip opencv-python wheels are not reliable on Android.
pkg install x11-repo -y
pkg install opencv-python -y

# Install Python libraries
# Pillow may require a moment to compile on mobile devices
pip install discord.py numpy Pillow aiohttp streamlink

# Verify libtesseract can be loaded by Python's ctypes path.
python - <<'PY'
import ctypes
import ctypes.util

path = ctypes.util.find_library("tesseract") or "libtesseract.so"
ctypes.CDLL(path)
print(f"libtesseract load check passed: {path}")
PY
