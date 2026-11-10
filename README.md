# TrimUI Brick emulator

Boots the **official TrimUI Brick (TG3040) firmware** in QEMU on your PC, so
custom firmwares that install on top of it — **NextUI**, **spruceOS**, MinUI,
… — run unmodified, the same way they do on the device: you put the CFW on an
SD card image, the stock OS boots, finds it, and hands over.

```
host keyboard ─► virtio-kbd ─► brick-hw (fake Allwinner PIO regs) ─► stock trimui_inputd ─► "TRIMUI Player1"
stock rootfs (Tina Linux, glibc 2.33) on an emulated 8 GB eMMC  (real GPT layout, mmcblk0)
SD card image (FAT32, mmcblk1p1)  ─► stock runtrimui.sh ─► /mnt/SDCARD/trimui/app/MainUI (CFW hook)
GLES apps ─► SDL "mali" backend ─► brick-emu libEGL shim ─► Mesa (llvmpipe / virgl) ─► /dev/fb0 ─► window
```

## What is emulated

| Brick hardware | Emulator |
|---|---|
| Allwinner A133P, 4× Cortex-A53, 1 GB | QEMU `virt`, 4× cortex-a53, 1 GB (TCG) |
| 8 GB eMMC with the stock partitions | `out/emmc.img` on SDHCI → `mmcblk0p1..p10` + `partitions=` like U-Boot passes |
| SD card slot | your SD image on a 2nd SDHCI → `mmcblk1p1` |
| 1024×768 panel, `/dev/fb0` | virtio-gpu + fbdev emulation (60 Hz flush) |
| Buttons on PIO pins read via `/dev/mem` | `brick-hw` kernel driver serves fake PIO register pages; the **stock** `trimui_inputd` reads them |
| AXP2202 PMIC: power key, battery, USB | `axp2202-pek` input device, `axp2202-battery` / `axp2202-usb` / `AC` power supplies |
| LED animation, rumble motor, speaker | `/sys/class/led_anim`, `/sys/class/motor`, `/sys/class/speaker`, GPIO sysfs (gpio-mockup) |
| `/dev/disp` display engine | stub answering the disp2 ioctls the firmware uses |
| PowerVR GE8300 GLES | Mesa 20.3 (GLES 3.2) behind an fbdev-EGL shim; optional virgl (host GPU) |
| audiocodec (ALSA card 0) | intel-hda, card id `audiocodec` → host PulseAudio/ALSA |
| `/proc/cpuinfo` `hwserial: TG3040…` | printed like the TrimUI kernel (CFWs detect the Brick from it) |
| Wi-Fi (XRadio, nl80211) | `mac80211_hwsim` radio as `wlan0` + a simulated access point (see *Wi-Fi*) |

The stock rootfs is used as-is except for: a preinit hook that mounts the
read-only *additions* disk at `/brick-emu`, and the PowerVR `libEGL/libGLES*`
names pointing at the shim/Mesa there.

## Requirements

* Linux host, x86_64 or aarch64 (tested: Fedora 44 x86_64; earlier WSL2 on
  aarch64), Docker, `curl`, `7z`, `python3`, `sgdisk`, `sfdisk`,
  `mke2fs`/`debugfs` (e2fsprogs).
  Fedora: `sudo dnf install gdisk 7zip e2fsprogs util-linux docker-ce` (or `moby-engine`).
  Debian/Ubuntu: `sudo apt install gdisk 7zip e2fsprogs fdisk docker.io`.
  Everything else (cross compilers, QEMU build deps, arm64 Mesa) lives in the
  builder containers; no binfmt/qemu-user setup is needed on x86_64 hosts.
  SELinux hosts work (bind mounts use `:z`, which relabels the project tree only).
* Recommended: your distro's QEMU (`./brick run` prefers it over the bundled
  build; needed for `--gpu virgl` on NVIDIA, where the bundled QEMU 9.2 fails):
  Fedora: `sudo dnf install qemu-system-aarch64-core qemu-ui-gtk qemu-ui-opengl
  qemu-ui-egl-headless qemu-device-display-virtio-gpu-pci-gl qemu-audio-pa`.
  `BRICK_QEMU=bundled` (or a path) overrides the choice.
* NVIDIA (proprietary driver) + `--gpu virgl`: QEMU's own GTK GL window
  doesn't work there (black on Wayland, abort on X11); the default skin window
  is not affected (it renders offscreen with `egl-headless`).
* A desktop for the window (X11 / Wayland / WSLg). Without one the screen is
  served over VNC on `127.0.0.1:5900`.
* ~15 GB disk for builds and images (images are sparse).

## Quick start

```bash
./brick setup                      # download stock fw from github.com/trimui/firmware_brick, build QEMU, kernel, disks
./brick fetch nextui               # or: ./brick fetch spruce
./brick sd out/sdcard.img 16G cfw/NextUI-*-base.zip   # add ROM/BIOS folders as extra args
./brick run                        # boots out/sdcard.img
./brick manager                    # or: everything from a control window
```

First boot of a CFW runs its installer exactly like on hardware (NextUI and
spruce unpack a lot onto the card — with emulated storage this takes from a few
minutes for NextUI to much longer for spruce's first boot; let it finish).

Other commands:

```bash
./brick run out/sd-spruce.img --emmc out/emmc-spruce.img   # separate internal storage per CFW
./brick run --gpu virgl            # host-GPU accelerated GLES (needs a GL-capable desktop)
./brick run --display vnc          # force VNC (opens vncviewer if installed)
./brick shell                      # root shell on the running Brick (Ctrl-] to detach)
./brick update                     # rebuild emulator parts, keep internal storage
./brick reset                      # factory-reset internal storage (eMMC)
```

The terminal you start `./brick run` in is the Brick's serial console
(press Enter for a shell; `Ctrl-a x` quits QEMU).

## Manager

`./brick manager` opens a control window (System → "Add to app menu" puts it
in the applications menu):

* **Start / Power off / Restart** the Brick (clean shutdown and reboot inside
  the firmware; *Force stop* and *Sleep / wake* in the ☰ menu), choose the SD
  card image, graphics (GPU / software) and the window colour.
* **SD card** tab: browse the card, add files and folders (buttons or drag &
  drop from your file manager), new folder, export, delete. With the emulator
  stopped, files are written straight into the image (fast); while it runs you
  see the live card and uploads go through the emulated Brick (~1–3 MB/s).
* **Screenshot** (saved to `~/Pictures/TrimUI Brick/`), **Shell** (root shell
  in a terminal), **Console** tab with the boot log and a command box.
* **System** tab: download NextUI / spruceOS, create SD cards (size + CFW +
  extra folders), update the emulator, factory-reset internal storage.

The manager also controls a Brick started with `./brick run` in a terminal.

## Wi-Fi

The Brick gets a real (simulated) Wi-Fi radio, `wlan0`, driven by the stock
`wpa_supplicant`/`udhcpc` and the firmware's or CFW's own Wi-Fi menu. The
emulator runs a virtual access point for it:

| | default | change with |
|---|---|---|
| network name | `BrickNet` | `--wifi-ssid NAME` / manager → Wi-Fi… |
| password (WPA2) | `brick1234` | `--wifi-pass PW` (`""` = open network) |
| router | on | `--no-wifi`; live: manager ☰ → Wi-Fi router on / off |

Scan, connect (a wrong password fails like on hardware), DHCP, DNS and
internet through the PC all work. As on the real device the Brick has no
other network: no Wi-Fi, no internet. SSH (`127.0.0.1:2222`) reaches it once
it is on Wi-Fi.

How: two `mac80211_hwsim` radios; the second one and the emulator's wired link
(QEMU user networking) live in a hidden "router" network namespace, bridged,
with the firmware's own `hostapd` (`/brick-emu/bin/wifi-ap`). Settings reach
the guest via QEMU `fw_cfg`. Changing the kernel/hook needs `./brick update`.

## Window

`./brick run` opens a Brick-shaped window: the screen plus every button of the
device, drawn as a clickable skin (`--color black|grey|white`, `--scale F`).

* Left-click a button to press it (hold to keep it pressed; sliding across the
  D-pad rolls between directions). Right-click latches a button down / up —
  for combos such as MENU + a button.
* Drag the body to move the window, Ctrl + mouse wheel to zoom, the red ✕ in
  the top-right corner (on hover) or Ctrl+Q to power off the emulator.
* The keyboard works as below; pressed buttons light up on the skin.

QEMU renders offscreen (on the host GPU with `--gpu virgl`) and the window
reads the screen from QEMU's VNC server on localhost (`tools/brick-window.py`,
needs python3-gobject/GTK 3, pycairo, numpy). `--display gtk|sdl|vnc` gives the
plain QEMU window or VNC instead.

## Controls

| Brick | Keyboard |
|---|---|
| D-pad | arrow keys |
| A / B / X / Y | X / Z / S / A |
| L / R / L2 / R2 | Q / W / E / R |
| START / SELECT | Enter / Right Shift (or Backspace) |
| MENU | Esc (or M) |
| F1 / F2 | 1 / 2 |
| Volume + / − | = / − |
| Power | P (or End) |

USB gamepads passed through to the VM are mapped too (standard evdev layout).

## Layout

```
brick                      main CLI
scripts/                   build-qemu, build-kernel, build-guest, make-emmc, make-sdcard, run
kernel/brick-hw/           the Brick hardware personality driver
kernel/patch-tree.py       small kernel patches (/dev/mem hook, mmc numbering, cpuinfo, fbdev rate)
kernel/brick.config        kernel config fragment
guest/egl-shim/            libEGL replacement (fbdev window semantics on Mesa)
guest/tools/               gltest, evtest, setsid (installed in /brick-emu/bin)
tools/awimg_unpack.py      Allwinner IMAGEWTY (.awimg) unpacker
tools/test-boot.sh …       headless test harness (monitor, console log, shell)
firmware/  cfw/  out/      downloads and generated images (not source)
```

## Performance and limitations

* The Brick is an arm64 machine, so on an x86_64 host QEMU has to translate
  every instruction (TCG; KVM can't help across architectures — and WSL2 has no
  KVM even on arm64). UI and app start-up are several times slower than the
  real device, and software GLES
  (llvmpipe) manages roughly 10–20 full-screen frames per second. `--gpu virgl`
  moves GLES rendering to the host GPU and is the way to get playable speed
  for GL-heavy emulators.
* Bluetooth, suspend, LEDs (state is stored, not shown) and
  rumble are not reproduced.
* Firmware updates through the stock OTA work on the emulated eMMC but would
  replace the rootfs hook; run `./brick update` afterwards.
* SD images must be a power-of-two size (the SD builder rounds up).

## Debugging

* `BRICK_EGL_DEBUG=1` in the guest prints shim decisions and per-frame timings.
* `/brick-emu/bin/evtest` lists input devices / dumps events;
  `/brick-emu/bin/gltest` is an SDL2+GLES display test.
* `tools/test-boot.sh SD.img` boots headless with `build/test/{mon,ctl}.sock`;
  `tools/qmon.py build/test/mon.sock "screendump x.png -f png"` takes a screenshot;
  `tools/gsh.py build/test/ctl.sock 'cmd'` runs a guest command.
