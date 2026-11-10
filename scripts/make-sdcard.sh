#!/bin/bash
# Create an emulated SD card (MBR + one FAT32 partition, like a real card).
# Usage: make-sdcard.sh OUT.img SIZE [CFW.zip | DIR]...
#   e.g. make-sdcard.sh out/sd-nextui.img 16G cfw/NextUI-*-base.zip ~/roms-folder
# Archives (.zip/.7z) are extracted to the card root; directories are copied into the card root.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ $# -ge 2 ] || { sed -n 2,6p "$0"; exit 1; }
OUT=$(realpath -m "$1"); SIZE=$2; shift 2
STAGE=$(mktemp -d "$ROOT/build/sdstage.XXXX")
trap 'rm -rf "$STAGE"' EXIT
for src in "$@"; do
  case "$src" in
    *.zip|*.7z) 7z x -y -o"$STAGE" "$src" >/dev/null ;;
    *) cp -a "$src"/. "$STAGE"/ ;;
  esac
done
# built under build/ (bind-mounted into the container; on SELinux hosts only the
# project tree is relabeled, never the destination folder), then moved to OUT
IMG="$STAGE.img"; trap 'rm -rf "$STAGE" "$IMG"' EXIT
truncate -s "$SIZE" "$IMG"
# QEMU's SD card model needs a power-of-two size: round up.
bytes=$(stat -c %s "$IMG"); p=1; while [ $p -lt $bytes ]; do p=$((p*2)); done
[ $p -ne $bytes ] && { echo "rounding card size up to $((p>>30)) GiB"; truncate -s $p "$IMG"; }
echo 'start=2048, type=c' | sfdisk -q "$IMG"
docker run --rm --user "$(id -u):$(id -g)" -v "$ROOT:/work:z" \
  brick-emu-builder bash -ec "
img=/work/build/$(basename "$IMG")
cd /work/build/$(basename "$STAGE")
bytes=\$(( \$(stat -c %s \$img) - 2048*512 ))
mkfs.vfat -F 32 -s 64 -n SDCARD --offset 2048 \$img \$(( bytes / 1024 )) >/dev/null
export MTOOLS_SKIP_CHECK=1
if [ -n \"\$(ls -A)\" ]; then mcopy -s -Q -i \$img@@1M \$(ls -A) ::/ ; fi
mdir -i \$img@@1M ::/ | tail -n +4 | head -20
"
mkdir -p "$(dirname "$OUT")"; rm -f "$OUT"; mv "$IMG" "$OUT"
echo "SD card image: $OUT"
