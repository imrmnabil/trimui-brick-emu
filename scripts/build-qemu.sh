#!/bin/bash
# Build qemu-system-aarch64 inside the builder container; output in out/qemu.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
QEMU_VER=${QEMU_VER:-9.2.0}
mkdir -p "$ROOT/build" "$ROOT/out"
docker run --rm --user "$(id -u):$(id -g)" -v "$ROOT:/work:z" -w /work/build brick-emu-builder bash -c "
set -e
[ -d qemu-$QEMU_VER ] || { wget -q https://download.qemu.org/qemu-$QEMU_VER.tar.xz && tar xf qemu-$QEMU_VER.tar.xz; }
cd qemu-$QEMU_VER
[ -f build/build.ninja ] || ./configure --prefix=/work/out/qemu --target-list=aarch64-softmmu \
   --enable-gtk --enable-sdl --enable-opengl --enable-virglrenderer --enable-slirp \
   --enable-alsa --enable-pa --enable-fdt=system --disable-docs --disable-werror \
   --disable-xen --disable-spice --disable-rbd --disable-glusterfs --disable-libiscsi \
   --disable-libnfs --disable-capstone --disable-vde --disable-smartcard --disable-usb-redir
make -C build -j\$(nproc) && make -C build install
"
"$ROOT/scripts/bundle-qemu-libs.sh"
echo "QEMU installed to $ROOT/out/qemu"
