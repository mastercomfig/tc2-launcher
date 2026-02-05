#!/bin/sh

# This is where Python executables install if not installed system-wide
PATH="$HOME/.local/bin:$PATH"

export PATH

export PYTHONOPTIMIZE=1
export PYTHONHASHSEED=0
export PYI_STATIC_ZLIB=1
export OBJECT_MODE=64
pyinstaller --clean build-linux-qt.spec $*
