#!/bin/bash
# For use within the Termux environment
set -e

# Update and upgrade Termux packages
pkg update && pkg upgrade -y

# Install core binaries
# Termux's tesseract package provides the libtesseract shared library used by ocr.py
# clang/make/pkg-config/maturin are used by native Python packages.
# rust is needed because model2vec's tokenizers dependency may build from source.
pkg install python tesseract ffmpeg clang make pkg-config rust maturin -y

# OpenCV is provided as a Termux package; pip opencv-python wheels are not reliable on Android.
pkg install x11-repo -y
pkg install opencv-python -y

# Install Python libraries
# model2vec provides lightweight local NL action matching.
# Its tokenizers dependency may build from source on Termux. Set the Android API
# level explicitly so Android pthread symbols are exposed correctly.
# Pillow may require a moment to compile on mobile devices
ANDROID_API_LEVEL="$(getprop ro.build.version.sdk)"
CXXFLAGS="-lpthread -D__ANDROID_API__=${ANDROID_API_LEVEL}" pip install tokenizers --no-binary :all:
pip install discord.py model2vec numpy Pillow aiohttp streamlink pyuca

# Verify libtesseract can be loaded by Python's ctypes path.
python - <<'PY'
import ctypes
import ctypes.util

path = ctypes.util.find_library("tesseract") or "libtesseract.so"
ctypes.CDLL(path)
print(f"libtesseract load check passed: {path}")
PY
