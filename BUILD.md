# Building from source

This guide is for distributions without a package, such as **Ubuntu** and
**Debian**. (On Fedora, install the RPM from the releases page, or see
[Fedora](#fedora) at the end.)

The build runs the cross-compilers and the arm64 libraries in **Docker**
containers, so your system only needs Docker, Python/GTK, a few disk tools and,
ideally, QEMU from your distribution. The first build takes 15–30 minutes and
uses about 15 GB of disk space.

## Requirements

- x86_64 or arm64 Linux with a desktop (X11 or Wayland)
- about 15 GB of free disk space, and 8 GB of RAM or more
- Internet access (Linux, QEMU and Debian packages, and TrimUI's firmware
  are downloaded during the build)

## 1. Install the packages

Ubuntu 24.04 or newer, and Debian 12 or newer:

```bash
sudo apt update
sudo apt install git docker.io python3 python3-gi python3-gi-cairo python3-cairo \
    gir1.2-gtk-3.0 gdisk e2fsprogs dosfstools mtools p7zip-full
```

### QEMU

The emulator prefers your distribution's QEMU if it's installed, and builds
its own otherwise.

| Distribution | What to do |
|---|---|
| Ubuntu 24.04 or newer | `sudo apt install qemu-system-arm qemu-system-gui qemu-system-modules-opengl` (QEMU 8.2) |
| Debian 12 | `sudo apt install qemu-system-arm qemu-system-gui` (QEMU 7.2) |
| Debian 13 / newer Ubuntu | as for Ubuntu 24.04 |
| Ubuntu 22.04 | skip this: its QEMU 6.2 is too old (no PulseAudio sound). `./brick setup` builds QEMU 9.2 for you |

GPU mode (`--gpu virgl`, the manager's default) has been tested with QEMU 10.2.
If the Brick's screen stays black or the emulator stops in GPU mode, choose
**Graphics: Software** in the manager (or `--gpu soft`). On **NVIDIA's
proprietary driver**, GPU mode is known to fail with the QEMU 9.2 that the
build makes for you. Use a distribution QEMU there, or software mode.

### Docker

Let your user run Docker, then **log out and back in**:

```bash
sudo usermod -aG docker $USER
```

Check it with `docker run --rm hello-world`.

## 2. Get the source

```bash
git clone https://github.com/imrmnabil/trimui-brick-emu.git
cd trimui-brick-emu
```

## 3. Build

```bash
./brick setup
```

This does the following:

1. Downloads the stock Brick firmware from
   [TrimUI's GitHub releases](https://github.com/trimui/firmware_brick/releases)
   and unpacks it (`firmware/`).
2. Builds the builder containers (Ubuntu with an arm64 cross-compiler, and
   Debian arm64 libraries).
3. Builds QEMU into `out/qemu`, but only if no `qemu-system-aarch64` is
   installed.
4. Builds the guest Linux kernel (`out/Image`) and the emulator add-on disk
   with Mesa, the EGL shim and the Wi-Fi simulation (`out/brick-emu.img`).
5. Creates the emulated 8 GB internal storage (`out/emmc.img`). The file is
   sparse, so only what's written takes space.

## 4. Run

```bash
./brick manager            # the manager window: start/stop, SD cards, downloads …
```

or from the command line:

```bash
./brick fetch nextui                                   # download NextUI into cfw/
./brick sd out/sdcard.img 16G cfw/NextUI-*-base.zip    # SD card with NextUI
./brick run --gpu virgl                                # boot it (Brick window)
```

The first boot of a CFW runs its installer, which takes a few minutes. To add
the manager to your applications menu, use **System → Add to app menu**.

All the commands are listed in the [README](README.md#command-line), plus
`./brick update`, which rebuilds the kernel and add-on disk after you change
them and keeps your internal storage.

## Troubleshooting

- **`permission denied` from Docker:** log out and back in after `usermod`,
  or run `newgrp docker`.
- **`command not found: sgdisk / mkfs.vfat / debugfs`:** install `gdisk`,
  `dosfstools` and `e2fsprogs`. The scripts also look in `/usr/sbin`.
- **The Brick window doesn't open, but VNC does:** the window needs
  `python3-gi`, `python3-gi-cairo` and `gir1.2-gtk-3.0`. Without them the
  screen is still served on VNC at `127.0.0.1:5900`.
- **The Brick seems frozen:** check free RAM with `free -m`. The emulated Brick
  needs about 1 GB, and it stalls when it gets swapped out.
- **SELinux / AppArmor:** the Docker volume mounts use `:z`, which relabels
  only the project folder.

## Fedora

```bash
sudo dnf install git moby-engine python3-gobject python3-cairo gtk3 gdisk e2fsprogs \
    dosfstools mtools 7zip rpm-build qemu-system-aarch64-core qemu-ui-egl-headless \
    qemu-device-display-virtio-gpu-pci-gl qemu-audio-pa
sudo usermod -aG docker $USER        # then log out and back in
./brick setup
./scripts/build-rpm.sh               # optional: dist/trimui-brick-emu-*.noarch.rpm
```

## What is where

```
brick                    command line (setup, run, manager, sd, fetch, update, reset)
scripts/                 build-kernel, build-guest, build-qemu, make-emmc, make-sdcard, run, build-rpm
kernel/                  brick-hw driver (the Brick's hardware), kernel patches and config
guest/                   EGL shim, guest helpers, Wi-Fi access point, firmware startup hook
tools/                   manager, Brick window, firmware/CFW download, test helpers
docker/                  builder container definitions
packaging/               RPM spec, launcher, desktop entry, icon
firmware/ cfw/ out/      downloads and generated images (not in git)
```
