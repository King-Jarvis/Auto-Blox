#!/bin/sh
# Build mpy-cross, the host compiler that turns device files into .mpy.
# No privilege needed. The console finds it at the path below.
set -e
VERSION=${1:-v1.29.0}
DIR="$HOME/.cache/zero2w-console/micropython"
if [ ! -d "$DIR" ]; then
    git clone --depth 1 --branch "$VERSION" https://github.com/micropython/micropython.git "$DIR"
fi
make -C "$DIR/mpy-cross" -j4
"$DIR/mpy-cross/build/mpy-cross" --version
