#!/bin/bash
# Build the emulated 8GB eMMC from the stock firmware partitions.
# Usage: make-emmc.sh [--update-rootfs] [out/emmc.img]
#   --update-rootfs  only rewrite the rootfs partition of an existing image
#                    (keeps settings/overlay in rootfs_data and UDISK)
set -euo pipefail
UPDATE=0; [ "${1:-}" = --update-rootfs ] && { UPDATE=1; shift; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FW="$ROOT/firmware/unpacked"
OUT=${1:-"$ROOT/out/emmc.img"}
SIZE=${EMMC_SIZE:-8589934592}   # 8 GiB (QEMU needs a power-of-two card size)
[ -f "$FW/rootfs.fex" ] || { echo "run tools/awimg_unpack.py first"; exit 1; }
WORK="$ROOT/build/emmc"; mkdir -p "$WORK" "$(dirname "$OUT")"

# --- rootfs: stock image + emulator hooks
cp "$FW/rootfs.fex" "$WORK/rootfs.img"
DBG="$WORK/debugfs.cmd"
{
  echo "mkdir /brick-emu"
  echo "write $ROOT/guest/rootfs-overlay/lib/preinit/85_brick_emu /lib/preinit/85_brick_emu"
  echo "sif /lib/preinit/85_brick_emu mode 0100644"
  echo "sif /lib/preinit/85_brick_emu uid 0"
  echo "sif /lib/preinit/85_brick_emu gid 0"
  # PowerVR GLES userspace -> emulator EGL shim + Mesa
  for l in libEGL.so libEGL.so.1; do echo "rm /usr/lib/$l"; echo "symlink /usr/lib/$l /brick-emu/libEGL.so.1"; done
  for l in libGLESv2.so libGLESv2.so.2; do echo "rm /usr/lib/$l"; echo "symlink /usr/lib/$l /brick-emu/mesa/libGLESv2.so.2"; done
  for l in libGLES_CM.so libGLES_CM.so.1 libGLESv1_CM.so libGLESv1_CM.so.1; do echo "rm /usr/lib/$l"; echo "symlink /usr/lib/$l /brick-emu/mesa/libGLESv1_CM.so.1"; done
} > "$DBG"
debugfs -w -f "$DBG" "$WORK/rootfs.img" 2>&1 | grep -vE "^debugfs|^rm: File not found" || true
e2fsck -fy "$WORK/rootfs.img" >/dev/null 2>&1 || true

put() { dd if="$2" of="$OUT" bs=512 seek="$1" conv=notrunc,sparse status=none; }
if [ $UPDATE = 1 ] && [ -f "$OUT" ]; then
  put 101376 "$WORK/rootfs.img"
  echo "rootfs partition updated in $OUT"
  exit 0
fi

# --- partition table (same layout as sunxi_gpt.fex, UDISK grows to the end)
rm -f "$OUT"; truncate -s "$SIZE" "$OUT"
sgdisk -Z "$OUT" >/dev/null 2>&1 || true
sgdisk -a 1 \
  -n 1:1024:50175       -c 1:bootloader \
  -n 2:50176:51199      -c 2:env \
  -n 3:51200:52223      -c 3:env-redund \
  -n 4:52224:101375     -c 4:boot \
  -n 5:101376:1248255   -c 5:rootfs \
  -n 6:1248256:5442559  -c 6:rootfs_data \
  -n 7:5442560:5443583  -c 7:private \
  -n 8:5443584:5476351  -c 8:recovery \
  -n 9:5476352:5477375  -c 9:pstore \
  -n 10:5477376:0       -c 10:UDISK \
  "$OUT" >/dev/null

put 1024    "$FW/boot-resource.fex"
put 50176   "$FW/env.fex"
put 51200   "$FW/env.fex"
put 52224   "$FW/boot.fex"
put 101376  "$WORK/rootfs.img"

# Pre-format the data partitions like the firmware's first boot would
# (QEMU's SD model implements discard by writing zeros, which makes the
# stock mkfs.ext4 take ages).  Features limited to what e2fsprogs 1.42 knows.
mkpart() {  # start end label
  local f="$WORK/$3.img"; rm -f "$f"
  truncate -s $(( ($2 - $1 + 1) * 512 )) "$f"
  mke2fs -q -t ext4 -m 0 -O ^metadata_csum,^metadata_csum_seed,^orphan_file -E root_owner=0:0 -L "$3" "$f"
  put "$1" "$f"; rm -f "$f"
}
mkpart 1248256 5442559 rootfs_data
mkpart 5477376 $(( $(stat -c %s "$OUT") / 512 - 34 )) UDISK
echo "eMMC image: $OUT"
sgdisk -p "$OUT" | tail -11
