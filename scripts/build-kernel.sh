#!/bin/bash
# Build the emulator guest kernel (Linux LTS + brick-hw) in the builder container.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KVER=${KVER:-6.6.60}
mkdir -p "$ROOT/build" "$ROOT/out"
docker run --rm --user "$(id -u):$(id -g)" -v "$ROOT:/work:z" -w /work/build brick-emu-builder bash -c "
set -e
[ -d linux-$KVER ] || { wget -q https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$KVER.tar.xz && tar xf linux-$KVER.tar.xz; }
cd linux-$KVER
# --- graft brick-hw driver
rm -rf drivers/misc/brick-hw && mkdir -p drivers/misc/brick-hw
cp /work/kernel/brick-hw/brick-hw.c /work/kernel/brick-hw/Kconfig /work/kernel/brick-hw/Makefile drivers/misc/brick-hw/
cp /work/kernel/brick-hw/include/brick-hw.h include/linux/brick-hw.h
grep -q brick-hw drivers/misc/Kconfig || sed -i 's|^endmenu|source \"drivers/misc/brick-hw/Kconfig\"\nendmenu|' drivers/misc/Kconfig
grep -q brick-hw drivers/misc/Makefile || echo 'obj-y += brick-hw/' >> drivers/misc/Makefile
python3 /work/kernel/patch-tree.py .
# --- configure + build
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- defconfig >/dev/null
scripts/kconfig/merge_config.sh -m .config /work/kernel/brick.config >/dev/null
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- olddefconfig >/dev/null
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j\$(nproc) Image
cp arch/arm64/boot/Image /work/out/Image
for o in BRICK_HW MAC80211_HWSIM FW_CFG_SYSFS MMC_SDHCI_PCI DRM_VIRTIO_GPU FB_DEVICE SND_HDA_INTEL GPIO_MOCKUP EXFAT_FS MMC_BLOCK_MINORS STRICT_DEVMEM MODULES FRAMEBUFFER_CONSOLE; do grep -E \"^(# )?CONFIG_\$o[= ]\" .config || echo \"CONFIG_\$o missing\"; done
"
echo "Kernel: $ROOT/out/Image"
