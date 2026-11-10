#!/bin/bash
# Build the Linux AppImage: dist/TrimUI_Brick_Emulator-VERSION-x86_64.AppImage
#   scripts/build-appimage.sh [VERSION]      (default: packaging/VERSION)
# Needs Docker; builds the kernel / additions disk first if missing.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=${1:-$(cat "$ROOT/packaging/VERSION")}
[ -f "$ROOT/out/Image" ] || "$ROOT/scripts/build-kernel.sh"
[ -f "$ROOT/out/brick-emu.img" ] || "$ROOT/scripts/build-guest.sh"
[ -f "$ROOT/packaging/trimui-brick-emu.png" ] || { echo "missing packaging/trimui-brick-emu.png (python3 packaging/make-icon.py)"; exit 1; }
docker image inspect brick-emu-appimage >/dev/null 2>&1 || docker build -t brick-emu-appimage -f "$ROOT/docker/Dockerfile.appimage" "$ROOT/docker"
docker run --rm -v "$ROOT:/work:z" -e VERSION="$VERSION" brick-emu-appimage bash /work/packaging/bundle.sh
# the container runs as root: hand the outputs back
docker run --rm -v "$ROOT:/work:z" brick-emu-appimage chown -R "$(id -u):$(id -g)" /work/build/appimage /work/dist
