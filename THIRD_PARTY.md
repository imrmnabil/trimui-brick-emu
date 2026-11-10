# Third-party software

The project's own code is licensed under the GNU General Public License,
version 2 only (see [LICENSE](LICENSE)). Release packages also contain
prebuilt third-party software, listed here with its licenses and where the
corresponding source code is.

**Not included:** the TrimUI Brick stock firmware. It belongs to TrimUI, and
the emulator downloads it from [TrimUI's own GitHub
releases](https://github.com/trimui/firmware_brick/releases) on first start.
Custom firmwares (NextUI, spruceOS) are also downloaded from their projects'
releases on request. This project is not affiliated with TrimUI.

## Guest kernel (`assets/Image`)

| Component | Version | License | Source |
|---|---|---|---|
| Linux | 6.6.60 | GPL-2.0-only | [kernel.org tarball](https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.60.tar.xz) (also attached to each GitHub release), plus this repository's `kernel/` (brick-hw driver, `patch-tree.py`, `brick.config`) and `scripts/build-kernel.sh` |

## Emulator additions disk (`assets/brick-emu.img`)

These are arm64 libraries from Debian 11 "bullseye", copied unmodified apart
from their library search path (`patchelf`) by `scripts/build-guest.sh`. Their
source packages are in the [Debian archive](https://archive.debian.org/debian/pool/main/)
and on [snapshot.debian.org](https://snapshot.debian.org/).

| Debian package | Version | License |
|---|---|---|
| libegl-mesa0, libgbm1, libgl1-mesa-dri, libglapi-mesa (Mesa) | 20.3.5-1+deb11u1 | MIT |
| libglvnd0, libegl1, libgles2 (libglvnd) | 1.3.2-1 | MIT-style |
| libllvm11 (LLVM) | 1:11.0.1-2 | Apache-2.0 WITH LLVM-exception |
| libz3-4 | 4.8.10-1 | MIT |
| libdrm2 | 2.4.104-1 | MIT |
| libvulkan1 | 1.2.162.0-1 | Apache-2.0 |
| libwayland-client0 | 1.18.0-2~exp1.1 | MIT |
| libx11-xcb1 | 2:1.7.2-1+deb11u2 | MIT/X11 |
| libxcb1 | 1.14-3 | MIT/X11 |
| libxau6 | 1:1.0.9-1 | MIT/X11 |
| libxdmcp6 | 1:1.1.2-3 | MIT/X11 |
| libxshmfence1 | 1.3-1 | MIT/X11 |
| libffi7 | 3.3-6 | MIT |
| libexpat1 | 2.2.10-2+deb11u7 | MIT |
| libelf1 (elfutils) | 0.183-1 | LGPL-3.0-or-later OR GPL-2.0-or-later |
| libsensors5 (lm-sensors) | 1:3.6.0-7 | LGPL-2.1-or-later |
| libedit2 | 3.1-20191231-2+b1 | BSD-3-Clause |
| libbsd0 | 0.11.3-1+deb11u1 | BSD-3-Clause and others (permissive) |
| libmd0 | 1.0.3-3 | BSD-3-Clause, ISC, public domain |
| libtinfo6 (ncurses) | 6.2+20201114-2+deb11u2 | MIT/X11 |
| zlib1g | 1:1.2.11.dfsg-2+deb11u2 | Zlib |
| libzstd1 | 1.4.8+dfsg-2.1 | BSD-3-Clause |

The disk also has this project's own guest programs (`guest/`: the EGL shim,
`brick-netns`, `brick-agent`, the `wifi-ap` and `init` scripts and test tools),
which are GPL-2.0-only.

## Used from the host system (not distributed)

QEMU, Python, GTK and the disk tools (gdisk, e2fsprogs, dosfstools, mtools,
7-Zip) come from your distribution's packages.
