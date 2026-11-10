#!/bin/bash
# Create an emulated SD card (MBR + one FAT32 partition, like a real card).
# Usage: make-sdcard.sh OUT.img SIZE [CFW.zip | CFW.7z | DIR]...
#   e.g. make-sdcard.sh out/sd-nextui.img 16G cfw/NextUI-*-base.zip ~/roms-folder
# Archives (.zip/.7z) are extracted to the card root; directories are copied into the card root.
# Needs mkfs.vfat (dosfstools), mtools and 7z; without them on the host the
# builder container is used.
set -euo pipefail
export PATH="$PATH:/usr/sbin:/sbin"   # disk tools (not on a user PATH on Debian)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${BRICK_DATA:-$ROOT}"
PY="${BRICK_PYTHON:-python3}"
[ $# -ge 2 ] || { sed -n 2,7p "$0"; exit 1; }
OUT=$(realpath -m "$1"); SIZE=$2; shift 2
mkdir -p "$DATA/build"
STAGE=$(mktemp -d "$DATA/build/sdstage.XXXX")
IMG="$STAGE.img"
trap 'rm -rf "$STAGE" "$IMG"' EXIT
NATIVE=0
command -v mkfs.vfat >/dev/null && command -v mcopy >/dev/null && NATIVE=1

for src in "$@"; do
  case "$src" in
    *.zip) "$PY" -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$src" "$STAGE" ;;
    *.7z) "$(command -v 7z || command -v 7zz || echo 7z)" x -y -o"$STAGE" "$src" >/dev/null ;;
    *) cp -a "$src"/. "$STAGE"/ ;;
  esac
done

truncate -s "$SIZE" "$IMG"
# QEMU's SD card model needs a power-of-two size: round up.
bytes=$(stat -c %s "$IMG"); p=1; while [ $p -lt $bytes ]; do p=$((p*2)); done
[ $p -ne $bytes ] && { echo "rounding card size up to $((p>>30)) GiB"; truncate -s $p "$IMG"; }
# MBR: one FAT32 (LBA) partition from sector 2048 to the end
"$PY" -I -c '
import struct, sys, os
img = sys.argv[1]; sectors = os.path.getsize(img) // 512
mbr = bytearray(512)
mbr[440:444] = os.urandom(4)                                       # disk signature
mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\xfe\xff\xff", 0x0c, b"\xfe\xff\xff", 2048, sectors - 2048)
mbr[510:512] = b"\x55\xaa"
with open(img, "r+b") as f: f.write(mbr)
' "$IMG"

fill() {  # IMAGE STAGEDIR - format the partition and copy the staged files
  local img=$1 stage=$2 bytes
  bytes=$(( $(stat -c %s "$img") - 2048 * 512 ))
  mkfs.vfat -F 32 -s 64 -n SDCARD --offset 2048 "$img" $(( bytes / 1024 )) >/dev/null
  export MTOOLS_SKIP_CHECK=1
  ( cd "$stage"; shopt -s dotglob nullglob; items=(*)
    if [ ${#items[@]} -gt 0 ]; then mcopy -s -Q -i "$img@@1M" "${items[@]}" ::/ ; fi )
  mdir -i "$img@@1M" ::/ | tail -n +4 | head -20
}

if [ $NATIVE = 1 ]; then
  fill "$IMG" "$STAGE"
else
  # builder container; only the data tree is bind-mounted (:z relabels it on
  # SELinux hosts - never the destination folder), the image is moved after
  docker run --rm --user "$(id -u):$(id -g)" -v "$DATA:/work:z" brick-emu-builder bash -ec "
    $(declare -f fill)
    fill /work/build/$(basename "$IMG") /work/build/$(basename "$STAGE")"
fi
mkdir -p "$(dirname "$OUT")"; rm -f "$OUT"; mv "$IMG" "$OUT"
echo "SD card image: $OUT"
