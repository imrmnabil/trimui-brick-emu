%global debug_package %{nil}
%global __brp_strip %{nil}

Name:           trimui-brick-emu
Version:        %{brick_version}
Release:        1%{?dist}
Summary:        TrimUI Brick handheld emulator (runs the stock firmware and NextUI / spruceOS)
# own code GPL-2.0-only; bundled guest kernel / arm64 Debian libraries: see THIRD_PARTY.md
License:        GPL-2.0-only AND MIT AND Apache-2.0 WITH LLVM-exception AND Apache-2.0 AND LGPL-2.1-or-later AND (LGPL-3.0-or-later OR GPL-2.0-or-later) AND BSD-3-Clause AND ISC AND Zlib
URL:            https://github.com/imrmnabil/trimui-brick-emu
Source0:        %{name}-%{version}.tar
BuildArch:      noarch

# the emulated Brick runs on the distribution's QEMU (GPU via virgl / egl-headless)
Requires:       qemu-system-aarch64-core
Requires:       qemu-device-display-virtio-gpu-pci
Requires:       qemu-device-display-virtio-gpu-pci-gl
Requires:       qemu-ui-egl-headless
Requires:       qemu-audio-pa
# window + manager
Requires:       python3
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk3
# eMMC / SD card images
Requires:       gdisk
Requires:       e2fsprogs
Requires:       dosfstools
Requires:       mtools
Requires:       7zip
Requires:       bash
Requires:       coreutils

%description
Boots the official TrimUI Brick (TG3040) firmware in QEMU, so custom firmwares
that install on top of it (NextUI, spruceOS, ...) run unmodified. Brick-shaped
window with clickable buttons, a manager for SD cards / power / screenshots,
simulated Wi-Fi. The stock firmware is not included: it is downloaded from
TrimUI's GitHub releases on first start.

%prep
%setup -q

%build

%install
d=%{buildroot}%{_datadir}/%{name}
mkdir -p $d/scripts $d/tools $d/guest $d/assets
install -m 0755 brick $d/
install -m 0755 scripts/run.sh scripts/make-emmc.sh scripts/make-sdcard.sh $d/scripts/
install -m 0644 tools/*.py $d/tools/
cp -r guest/rootfs-overlay $d/guest/
install -m 0644 assets/Image $d/assets/
cp --sparse=always assets/brick-emu.img $d/assets/
chmod 0644 $d/assets/brick-emu.img
install -m 0644 VERSION README.md $d/
install -D -m 0755 packaging/trimui-brick-emu %{buildroot}%{_bindir}/%{name}
install -D -m 0644 packaging/trimui-brick-emu.desktop %{buildroot}%{_datadir}/applications/%{name}.desktop
install -D -m 0644 packaging/trimui-brick-emu.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/%{name}.png

%files
%license LICENSE THIRD_PARTY.md
%{_bindir}/%{name}
%{_datadir}/%{name}
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor/256x256/apps/%{name}.png
