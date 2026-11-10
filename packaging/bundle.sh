#!/bin/bash
# Build the AppImage. Runs inside the brick-emu-appimage container (Ubuntu
# 22.04), started by scripts/build-appimage.sh with the source tree at /work.
#   VERSION=0.1.0 bundle.sh
set -euo pipefail
VERSION=${VERSION:?VERSION not set}
QEMU_VER=${QEMU_VER:-10.2.2}
VIRGL_VER=${VIRGL_VER:-1.3.0}
SRC=/work
W=$SRC/build/appimage
PREFIX=$W/prefix                      # virglrenderer install
A=$W/AppDir
ARCH=x86_64
MULTI=/usr/lib/x86_64-linux-gnu
mkdir -p "$W"
cd "$W"
log() { echo "==> $*"; }

# ------------------------------------------------------------------ build
# recent meson (virglrenderer) + tomli (QEMU's configure on Python 3.10)
[ -x "$W/venv/bin/python3" ] || python3 -m venv "$W/venv"
"$W/venv/bin/python3" -c 'import mesonbuild, tomli, yaml' 2>/dev/null || "$W/venv/bin/pip" -q install meson==1.5.2 tomli pyyaml
export PATH="$W/venv/bin:$PATH"   # meson's python3 (yaml) for virglrenderer's generators
if [ ! -f "$PREFIX/lib/libvirglrenderer.so.1" ]; then
  log "virglrenderer $VIRGL_VER"
  [ -d virglrenderer-$VIRGL_VER ] || curl -fsSL "https://gitlab.freedesktop.org/virgl/virglrenderer/-/archive/$VIRGL_VER/virglrenderer-$VIRGL_VER.tar.bz2" | tar xj
  "$W/venv/bin/meson" setup --wipe virglrenderer-$VIRGL_VER/b virglrenderer-$VIRGL_VER \
    --prefix="$PREFIX" --libdir=lib --buildtype=release -Dtests=false -Dvideo=false -Dvenus=false > "$W/virgl-setup.log" || { tail -20 "$W/virgl-setup.log"; exit 1; }
  ninja -C virglrenderer-$VIRGL_VER/b install >/dev/null
fi
if [ ! -x "$W/qemu-root/usr/bin/qemu-system-aarch64" ]; then
  log "QEMU $QEMU_VER"
  [ -d qemu-$QEMU_VER ] || curl -fsSL "https://download.qemu.org/qemu-$QEMU_VER.tar.xz" | tar xJ
  cd qemu-$QEMU_VER
  # only what the emulator uses: aarch64 virt, virgl via egl-headless, VNC
  # (the skin window), slirp, PulseAudio/ALSA - no QEMU GUI
  PATH="$W/venv/bin:$PATH" PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" ./configure --prefix=/usr \
    --python="$W/venv/bin/python3" --target-list=aarch64-softmmu --disable-modules \
    --enable-opengl --enable-virglrenderer --enable-slirp --enable-vnc --enable-pa --enable-alsa \
    --enable-fdt=system --disable-gtk --disable-sdl --disable-docs --disable-werror --disable-tools \
    --disable-guest-agent --disable-xen --disable-spice --disable-capstone --disable-libusb \
    --disable-usb-redir --disable-smartcard --disable-curses --disable-gnutls --disable-curl \
    --disable-libssh --disable-rbd --disable-glusterfs --disable-libnfs --disable-libiscsi \
    --disable-vde --disable-brlapi --disable-dbus-display --disable-vnc-sasl --disable-vnc-jpeg \
    --disable-linux-io-uring --disable-zstd --disable-bpf > "$W/qemu-configure.log" || { tail -30 "$W/qemu-configure.log"; exit 1; }
  make -C build -j"$(nproc)" > "$W/qemu-build.log" 2>&1 || { tail -30 "$W/qemu-build.log"; exit 1; }
  make -C build install DESTDIR="$W/qemu-root" >/dev/null
  cd "$W"
fi

# ------------------------------------------------------------------ AppDir
log "assembling the AppDir"
rm -rf "$A"
mkdir -p "$A/usr/bin" "$A/usr/lib" "$A/usr/libexec/brick-tools" "$A/usr/share" "$A/opt/brick/assets"
# QEMU (+ only the data files it needs with -kernel boot)
cp "$W/qemu-root/usr/bin/qemu-system-aarch64" "$A/usr/bin/"
mkdir -p "$A/usr/share/qemu"
cp -r "$W/qemu-root/usr/share/qemu/keymaps" "$A/usr/share/qemu/"
cp "$W/qemu-root/usr/share/qemu/"efi-virtio.rom "$W/qemu-root/usr/share/qemu/"*.dtb "$A/usr/share/qemu/" 2>/dev/null || true
cp -P "$PREFIX/lib/"libvirglrenderer.so* "$A/usr/lib/"

# Python 3.10 + PyGObject + pycairo (the skin window and the manager)
cp /usr/bin/python3.10 "$A/usr/bin/python3.10"
cp -a /usr/lib/python3.10 "$A/usr/lib/"
rm -rf "$A/usr/lib/python3.10/"{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,pydoc_data,distutils/tests,site-packages}
find "$A/usr/lib/python3.10" -name __pycache__ -prune -exec rm -rf {} +
mkdir -p "$A/usr/lib/python3/dist-packages"
cp -a /usr/lib/python3/dist-packages/{gi,cairo} "$A/usr/lib/python3/dist-packages/"
cp -a $MULTI/girepository-1.0 "$A/usr/lib/"
# GTK 3 runtime: the libraries the typelibs name, pixbuf loaders, schemas, icons
for l in libgtk-3.so.0 libgdk-3.so.0 libgdk_pixbuf-2.0.so.0 libpango-1.0.so.0 libpangocairo-1.0.so.0 \
         libpangoft2-1.0.so.0 libatk-1.0.so.0 libcairo-gobject.so.2 libharfbuzz.so.0 librsvg-2.so.2 \
         libgirepository-1.0.so.1; do cp -L "$MULTI/$l" "$A/usr/lib/"; done
mkdir -p "$A/usr/lib/gdk-pixbuf-2.0/2.10.0" "$A/usr/lib/gio/modules"
cp -a $MULTI/gdk-pixbuf-2.0/2.10.0/loaders "$A/usr/lib/gdk-pixbuf-2.0/2.10.0/"
cp $MULTI/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders "$A/usr/lib/gdk-pixbuf-2.0/"
mkdir -p "$A/usr/share/glib-2.0/schemas"
cp /usr/share/glib-2.0/schemas/org.gtk.Settings.*.xml "$A/usr/share/glib-2.0/schemas/"
glib-compile-schemas "$A/usr/share/glib-2.0/schemas"
mkdir -p "$A/usr/share/icons"
cp -a /usr/share/icons/Adwaita /usr/share/icons/hicolor "$A/usr/share/icons/"
# python wrapper: GTK environment for our GUI only (host programs started from
# the manager get the original environment back, see brick-manager.py)
cat > "$A/usr/bin/python3" <<'EOF'
#!/bin/sh
U="$(dirname "$(readlink -f "$0")")/.."
if [ -z "${BRICK_GUI_ENV:-}" ]; then
  for v in GI_TYPELIB_PATH GDK_PIXBUF_MODULEDIR GDK_PIXBUF_MODULE_FILE GSETTINGS_SCHEMA_DIR \
           GIO_MODULE_DIR XDG_DATA_DIRS GTK_MODULES GTK_PATH; do
    eval "[ -n \"\${$v+x}\" ] && export BRICK_ORIG_$v=\"\$$v\""
  done
  export BRICK_GUI_ENV="GI_TYPELIB_PATH GDK_PIXBUF_MODULEDIR GDK_PIXBUF_MODULE_FILE GSETTINGS_SCHEMA_DIR GIO_MODULE_DIR XDG_DATA_DIRS GTK_MODULES GTK_PATH"
fi
export GI_TYPELIB_PATH="$U/lib/girepository-1.0"
export GDK_PIXBUF_MODULEDIR="$U/lib/gdk-pixbuf-2.0/2.10.0/loaders"
cache="${XDG_RUNTIME_DIR:-/tmp}/trimui-brick-emu-$(id -u)-pixbuf.cache"
if [ ! -s "$cache" ] || ! grep -qF "$GDK_PIXBUF_MODULEDIR" "$cache"; then
  "$U/lib/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders" > "$cache" 2>/dev/null
fi
export GDK_PIXBUF_MODULE_FILE="$cache"
export GSETTINGS_SCHEMA_DIR="$U/share/glib-2.0/schemas"
export GIO_MODULE_DIR="$U/lib/gio/modules"
export XDG_DATA_DIRS="$U/share:${BRICK_ORIG_XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
unset GTK_MODULES GTK_PATH
exec "$U/bin/python3.10" "$@"
EOF
chmod +x "$A/usr/bin/python3"

# disk tools used by the eMMC / SD card scripts (own dir on PATH)
T="$A/usr/libexec/brick-tools"
cp /usr/sbin/sgdisk /usr/sbin/mke2fs /usr/sbin/e2fsck /usr/sbin/debugfs /usr/sbin/mkfs.fat "$T/"
ln -s mkfs.fat "$T/mkfs.vfat"
cp /usr/bin/mtools "$T/"
for m in mcopy mdir mmd mdel mdeltree minfo mdu mattrib mren mtype; do ln -s mtools "$T/$m"; done
mkdir -p "$A/usr/lib/p7zip"
cp /usr/lib/p7zip/7z /usr/lib/p7zip/7z.so "$A/usr/lib/p7zip/"
cp -r /usr/lib/p7zip/Codecs "$A/usr/lib/p7zip/" 2>/dev/null || true
printf '#!/bin/sh\nexec "$(dirname "$(readlink -f "$0")")/../../lib/p7zip/7z" "$@"\n' > "$T/7z"
chmod +x "$T/7z"

# the emulator: scripts, tools, guest hook, prebuilt kernel + additions disk
B="$A/opt/brick"
cp "$SRC/brick" "$B/"
mkdir -p "$B/scripts" "$B/tools" "$B/guest"
cp "$SRC/scripts/run.sh" "$SRC/scripts/make-emmc.sh" "$SRC/scripts/make-sdcard.sh" "$B/scripts/"
cp "$SRC"/tools/*.py "$B/tools/"
cp -r "$SRC/guest/rootfs-overlay" "$B/guest/"
cp "$SRC/out/Image" "$B/assets/"
cp --sparse=always "$SRC/out/brick-emu.img" "$B/assets/"
cp "$SRC/README.md" "$B/"
echo "$VERSION" > "$B/VERSION"

# AppRun, desktop entry, icon
sed "s/@VERSION@/$VERSION/" "$SRC/packaging/AppRun" > "$A/AppRun"
chmod +x "$A/AppRun"
cp "$SRC/packaging/trimui-brick-emu.desktop" "$A/"
cp "$SRC/packaging/trimui-brick-emu.png" "$A/"
ln -s trimui-brick-emu.png "$A/.DirIcon"
mkdir -p "$A/usr/share/icons/hicolor/256x256/apps"
cp "$SRC/packaging/trimui-brick-emu.png" "$A/usr/share/icons/hicolor/256x256/apps/"

# ------------------------------------------------------------------ libraries
# Bundle every shared library our binaries need, except the base system and
# the GPU / display / audio stack, which must come from the host (drivers).
log "collecting libraries"
EXCLUDE='^(ld-linux.*|linux-vdso.*|libc|libm|libdl|libpthread|librt|libutil|libresolv|libnsl|libanl|libmvec|libBrokenLocale|libthread_db|libnss_.*|libgcc_s|libstdc\+\+|libGL|libEGL|libGLX|libGLdispatch|libOpenGL|libGLESv2|libgbm|libdrm|libX11|libX11-xcb|libxcb.*|libasound|libfontconfig|libfreetype|libexpat|libz|libwayland-client|libwayland-cursor|libwayland-egl|libwayland-server|libgpg-error|libcom_err|libjack|libpipewire-.*|libudev|libuuid|libblkid|libselinux|libmount|libpcre2-8)\.so'
elfs() { find "$A/usr" -type f \( -perm -u+x -o -name '*.so' -o -name '*.so.*' \) -exec sh -c 'head -c4 "$1" | grep -q ELF && echo "$1"' _ {} \; ; }
for pass in 1 2 3 4 5 6; do
  added=0
  for f in $(elfs); do
    LD_LIBRARY_PATH="$A/usr/lib:$PREFIX/lib" ldd "$f" 2>/dev/null | awk '/=> \//{print $1" "$3}' | while read -r n p; do
      echo "$n" | grep -Eq "$EXCLUDE" && continue
      [ -e "$A/usr/lib/$n" ] || { cp -L "$p" "$A/usr/lib/$n"; echo "+ $n"; }
    done
  done > "$W/added.txt"
  [ -s "$W/added.txt" ] || break
done
# relative runpaths ($ORIGIN): no LD_LIBRARY_PATH needed, so host programs
# started from the emulator never see the bundled libraries
for f in $(elfs); do
  rel=$(realpath --relative-to="$(dirname "$f")" "$A/usr/lib")
  patchelf --force-rpath --set-rpath "\$ORIGIN/$rel" "$f" 2>/dev/null || true
done
# missing-library check (host-provided ones are expected to be absent here only
# if excluded)
missing=$(for f in $(elfs); do ldd "$f" 2>/dev/null | awk '/not found/{print $1}'; done | sort -u | grep -Ev "$EXCLUDE" || true)
[ -z "$missing" ] || { echo "unresolved libraries: $missing"; exit 1; }

# ------------------------------------------------------------------ pack
log "packing"
TOOL="$W/appimagetool-$ARCH.AppImage"
[ -x "$TOOL" ] || { curl -fsSL -o "$TOOL" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"; chmod +x "$TOOL"; }
mkdir -p "$SRC/dist"
OUTF="$SRC/dist/TrimUI_Brick_Emulator-$VERSION-$ARCH.AppImage"
rm -f "$OUTF"
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=$ARCH "$TOOL" --no-appstream "$A" "$OUTF" >/dev/null
ls -la "$OUTF"
