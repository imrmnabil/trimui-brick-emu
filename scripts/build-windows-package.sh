#!/bin/bash
# Build a self-contained Windows-on-ARM64 package (no WSL needed to run it):
#   dist/TrimUI-Brick-win-arm64/{Brick.cmd,Brick.ps1,qemu/,images/}
# Usage: build-windows-package.sh [CFW.zip|.7z ...]   (default: latest NextUI in cfw/)
# Prerequisite: ./brick setup (kernel, additions disk and firmware).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
W="$ROOT/build/win"; DIST="$ROOT/dist/TrimUI-Brick-win-arm64"
QIMG="$ROOT/out/qemu/bin/qemu-img"
export LD_LIBRARY_PATH="$ROOT/out/qemu/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
mkdir -p "$W"

# --- Windows QEMU (MSYS2 clangarm64) + DLL closure
curl -sL -o "$W/clangarm64.db" https://repo.msys2.org/mingw/clangarm64/clangarm64.db
python3 -I "$ROOT/tools/msys2_fetch.py" "$W/clangarm64.db" "$W/root" mingw-w64-clang-aarch64-qemu | tail -1
rm -rf "$DIST"; mkdir -p "$DIST/qemu" "$DIST/images"
B="$W/root/clangarm64"
cp "$B/bin/qemu-system-aarch64.exe" "$B/bin/qemu-img.exe" "$B"/bin/*.dll "$DIST/qemu/"
mkdir -p "$DIST/qemu/share" && cp -r "$B/share/qemu" "$DIST/qemu/share/"
# QEMU looks for its data next to the exe in share/ (or via -L)
cp -r "$B/share/qemu/keymaps" "$DIST/qemu/" 2>/dev/null || true
# GTK display support files (optional -Display gtk)
[ -d "$B/lib/gdk-pixbuf-2.0" ] && mkdir -p "$DIST/qemu/lib" && cp -r "$B/lib/gdk-pixbuf-2.0" "$DIST/qemu/lib/"
[ -d "$B/share/glib-2.0/schemas" ] && mkdir -p "$DIST/qemu/share/glib-2.0" && cp -r "$B/share/glib-2.0/schemas" "$DIST/qemu/share/glib-2.0/"

# --- images (qcow2 keeps them small on NTFS)
cp "$ROOT/out/Image" "$DIST/images/Image"
"$QIMG" convert -O qcow2 "$ROOT/out/brick-emu.img" "$DIST/images/brick-emu.qcow2"
"$ROOT/scripts/make-emmc.sh" "$W/emmc-factory.img" >/dev/null
"$QIMG" convert -O qcow2 "$W/emmc-factory.img" "$DIST/images/emmc-factory.qcow2"; rm -f "$W/emmc-factory.img"
cfw=("$@")
[ ${#cfw[@]} -gt 0 ] || cfw=("$(ls -t "$ROOT"/cfw/NextUI-*-base.zip | head -1)")
name=$(basename "${cfw[0]}" | sed 's/\.[^.]*$//')
"$ROOT/scripts/make-sdcard.sh" "$W/sd.img" 8G "${cfw[@]}" >/dev/null
"$QIMG" convert -O qcow2 "$W/sd.img" "$DIST/images/sd-$name.qcow2"; rm -f "$W/sd.img"

# --- launcher + docs (CRLF for Windows)
cp "$ROOT/scripts/windows/Brick.ps1" "$ROOT/scripts/windows/Brick.cmd" "$DIST/"
sed 's/$/\r/' "$ROOT/scripts/windows/README-windows.txt" > "$DIST/README.txt"
du -sh "$DIST"; ls -la "$DIST/images"
echo "package: $DIST"
