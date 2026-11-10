#!/bin/bash
# Build the RPM: dist/trimui-brick-emu-VERSION-1.<dist>.noarch.rpm
#   scripts/build-rpm.sh [VERSION]      (default: packaging/VERSION)
# Needs rpmbuild (dnf install rpm-build); builds kernel / additions disk if missing.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=${1:-$(cat "$ROOT/packaging/VERSION")}
[ -f "$ROOT/out/Image" ] || "$ROOT/scripts/build-kernel.sh"
[ -f "$ROOT/out/brick-emu.img" ] || "$ROOT/scripts/build-guest.sh"
T="$ROOT/build/rpm"
rm -rf "$T"; mkdir -p "$T/SOURCES" "$T/src/trimui-brick-emu-$VERSION"
S="$T/src/trimui-brick-emu-$VERSION"
# source tree for the package: only what is installed
cd "$ROOT"
cp -r --parents brick scripts/run.sh scripts/make-emmc.sh scripts/make-sdcard.sh tools/*.py \
  guest/rootfs-overlay packaging/trimui-brick-emu packaging/trimui-brick-emu.desktop \
  packaging/trimui-brick-emu.png README.md LICENSE THIRD_PARTY.md "$S/"
mkdir -p "$S/assets"
cp out/Image "$S/assets/"
cp --sparse=always out/brick-emu.img "$S/assets/"
echo "$VERSION" > "$S/VERSION"
sed -i 's|^Exec=.*|Exec=trimui-brick-emu|' "$S/packaging/trimui-brick-emu.desktop"
tar --sparse -C "$T/src" -cf "$T/SOURCES/trimui-brick-emu-$VERSION.tar" "trimui-brick-emu-$VERSION"
rpmbuild -bb --define "_topdir $T" --define "brick_version $VERSION" "$ROOT/packaging/trimui-brick-emu.spec" | tail -3
mkdir -p "$ROOT/dist"
cp "$T"/RPMS/noarch/*.rpm "$ROOT/dist/"
ls -la "$ROOT"/dist/*.rpm
