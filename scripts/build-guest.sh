#!/bin/bash
# Build the guest "emulator additions" disk: EGL shim + Mesa, mounted at /brick-emu.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
docker build -q -t brick-emu-guest -f "$ROOT/docker/Dockerfile.guest" "$ROOT/docker" >/dev/null
STAGE="$ROOT/build/emu-root"
rm -rf "$STAGE" && mkdir -p "$STAGE"
docker run --rm --user "$(id -u):$(id -g)" -v "$ROOT:/work" brick-emu-guest bash -ec '
S=/work/build/emu-root; M=$S/mesa; mkdir -p $M/dri $S/bin
A=/usr/lib/aarch64-linux-gnu
# --- EGL shim (becomes /usr/lib/libEGL.so.1 for the firmware)
gcc -shared -fPIC -O2 -Wall -fvisibility=hidden -Wl,-soname,libEGL.so.1 -Wl,--disable-new-dtags,-rpath,/brick-emu/mesa \
    -I/work/guest/egl-shim -o $S/libEGL.so.1 /work/guest/egl-shim/egl_fbdev_shim.c -ldl -lpthread
# --- Mesa / glvnd
cp -L $A/libEGL.so.1 $M/libEGL_glvnd.so.1
cp -L $A/libGLESv2.so.2 $A/libGLESv1_CM.so.1 $A/libGLdispatch.so.0 $A/libEGL_mesa.so.0 $A/libglapi.so.0 $A/libgbm.so.1 $M/
cp -L $A/dri/swrast_dri.so $M/dri/
for d in kms_swrast virtio_gpu; do ln -sf swrast_dri.so $M/dri/${d}_dri.so; done
# dependency closure, excluding glibc and the toolchain runtime the Brick already has
skip="^(libc|libm|libdl|libpthread|librt|ld-linux-aarch64|libstdc\+\+|libgcc_s)\.so"
for i in 1 2 3 4 5 6; do
  for f in $M/*.so* $M/dri/swrast_dri.so; do
    ldd $f 2>/dev/null | awk "/=> \//{print \$1\" \"\$3}" | while read n p; do
      echo "$n" | grep -Eq "$skip" && continue
      [ -e $M/$n ] || cp -L $p $M/$n
    done
  done
done
patchelf --set-soname libEGL_glvnd.so.1 $M/libEGL_glvnd.so.1
for f in $M/*.so* $M/dri/swrast_dri.so; do patchelf --force-rpath --set-rpath /brick-emu/mesa $f; done
# glvnd vendor json (also selected explicitly by the shim)
mkdir -p $S/glvnd && echo "{\"file_format_version\":\"1.0.0\",\"ICD\":{\"library_path\":\"/brick-emu/mesa/libEGL_mesa.so.0\"}}" > $S/glvnd/50_mesa.json
# --- small test program
gcc -O2 -o $S/bin/gltest /work/guest/tools/gltest.c $(sdl2-config --cflags) -lSDL2 -lGLESv2 -lm
gcc -O2 -o $S/bin/evtest /work/guest/tools/evtest.c
gcc -O2 -o $S/bin/setsid /work/guest/tools/setsid.c
ls $M | wc -l; du -sh $S
'
cp -r "$ROOT/guest/files/." "$STAGE/" 2>/dev/null || true
mkdir -p "$ROOT/out"
rm -f "$ROOT/out/brick-emu.img"
mke2fs -q -t ext4 -L brick-emu -E root_owner=0:0 -d "$STAGE" "$ROOT/out/brick-emu.img" 400M
echo "additions disk: $ROOT/out/brick-emu.img"
