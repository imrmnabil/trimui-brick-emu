#!/bin/bash
# Copy shared libraries that QEMU needs but the host lacks into out/qemu/lib.
set -euo pipefail
shopt -s nullglob
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/out/qemu/bin/qemu-system-aarch64"
mkdir -p "$ROOT/out/qemu/lib"
for i in 1 2 3 4 5; do
  missing=$(LD_LIBRARY_PATH="$ROOT/out/qemu/lib" ldd "$BIN" "$ROOT"/out/qemu/lib/*.so* 2>/dev/null | awk '/not found/{print $1}' | sort -u | tr "\n" " " || true)
  [ -z "$missing" ] && break
  echo "bundling: $missing"
  docker run --rm --user "$(id -u):$(id -g)" -v "$ROOT:/work:z" brick-emu-builder bash -c "
    for l in $missing; do p=\$(ls /usr/lib/\$(gcc -print-multiarch)/\$l /lib/\$(gcc -print-multiarch)/\$l /usr/lib/\$(gcc -print-multiarch)/pulseaudio/\$l 2>/dev/null | head -1); [ -n \"\$p\" ] && cp -L \"\$p\" /work/out/qemu/lib/ || echo \"cannot find \$l\"; done"
done
LD_LIBRARY_PATH="$ROOT/out/qemu/lib" ldd "$BIN" | grep "not found" && exit 1
echo "all QEMU libraries resolved"
