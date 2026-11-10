#!/bin/bash
# Boot the emulated TrimUI Brick.
# Usage: run.sh [SDCARD.img] [options]
#   --gpu virgl     use host GPU acceleration (virtio-gpu-gl + virgl)
#   --display D     gtk | sdl | vnc (default: gtk if a desktop is available, else vnc)
#   --headless      no window (serial console only); use with --monitor
#   --monitor PATH  QEMU monitor on a unix socket (screendump, sendkey...)
#   --scale N       window scale factor (default: fit)
#   --log FILE      write the serial console to FILE instead of the terminal
#   --serial-sock P serial console on unix socket P (with --log as transcript)
#   --shell-sock P  root shell channel on unix socket P (see ./brick shell)
#   --emmc IMG      internal storage image (default out/emmc.img)
#   --no-net        no network
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/out"
SD=""; GPU=soft; DISPLAY_MODE=""; EMMC=""; HEADLESS=0; MONITOR=""; LOG=""; NET=1; SCALE=""; SERSOCK=""; SHELLSOCK="$OUT/run/shell.sock"
while [ $# -gt 0 ]; do
  case "$1" in
    --gpu) GPU=$2; shift ;;
    --display) DISPLAY_MODE=$2; shift ;;
    --emmc) EMMC=$2; shift ;;
    --headless) HEADLESS=1 ;;
    --monitor) MONITOR=$2; shift ;;
    --log) LOG=$2; shift ;;
    --no-net) NET=0 ;;
    --serial-sock) SERSOCK=$2; shift ;;
    --shell-sock) SHELLSOCK=$2; shift ;;
    --scale) SCALE=$2; shift ;;
    -*) echo "unknown option $1"; exit 1 ;;
    *) SD=$1 ;;
  esac
  shift
done
SD=${SD:-$OUT/sdcard.img}
EMMC=${EMMC:-$OUT/emmc.img}
for f in "$OUT/Image" "$EMMC" "$OUT/brick-emu.img"; do
  [ -f "$f" ] || { echo "missing $f - run ./brick setup first"; exit 1; }
done

PARTS="bootloader@mmcblk0p1:env@mmcblk0p2:env-redund@mmcblk0p3:boot@mmcblk0p4:rootfs@mmcblk0p5:rootfs_data@mmcblk0p6:private@mmcblk0p7:recovery@mmcblk0p8:pstore@mmcblk0p9:UDISK@mmcblk0p10"
CMDLINE="console=ttyAMA0 root=/dev/mmcblk0p5 rootwait init=/sbin/init rdinit=/rdinit loglevel=1 partitions=$PARTS cma=64M gpt=1"
CMDLINE+=" ${BRICK_CMDLINE_EXTRA:-} snd_hda_intel.id=audiocodec gpio_mockup.gpio_mockup_ranges=0,384 video=Virtual-1:1024x768 brick.gpu=$GPU"

args=(
  -name "TrimUI Brick"
  -M virt -cpu cortex-a53 -smp 4 -m 1024 -accel tcg,thread=multi,tb-size=512
  -kernel "$OUT/Image" -append "$CMDLINE"
  # eMMC (mmcblk0) and SD card slot (mmcblk1), real SD/MMC protocol over SDHCI
  -device sdhci-pci,id=emmc-host,addr=0x5
  -drive if=none,id=emmc,file="$EMMC",format=raw
  -device sd-card,drive=emmc,bus=/gpex-pcihost/pcie.0/emmc-host/sd-bus
  -device sdhci-pci,id=sd-host,addr=0x6
  # emulator additions (Mesa + EGL shim), read-only
  -drive if=none,id=emu,file="$OUT/brick-emu.img",format=raw,readonly=on
  -device virtio-blk-pci,drive=emu,addr=0x7
  -device virtio-keyboard-pci
  -device qemu-xhci -device usb-tablet
)
if [ -f "$SD" ]; then
  args+=(-drive if=none,id=sd,file="$SD",format=raw -device sd-card,drive=sd,bus=/gpex-pcihost/pcie.0/sd-host/sd-bus)
else
  echo "note: no SD card image ($SD) - booting stock firmware with an empty slot"
fi

if [ "$GPU" = virgl ]; then
  echo "note: --gpu virgl is experimental; if the window stays black use the default software mode" >&2
  # WSL: Mesa must use the d3d12 driver (GPU via /dev/dxg); otherwise it probes
  # /dev/dri, falls back to zink and GL in the QEMU window fails.
  if [ -e /dev/dxg ] && [ -z "${GALLIUM_DRIVER:-}" ]; then export GALLIUM_DRIVER=d3d12; fi
  args+=(-device virtio-gpu-gl-pci,xres=1024,yres=768)
  DISP="gtk,gl=on"
else
  args+=(-device virtio-gpu-pci,xres=1024,yres=768)
  DISP="gtk"
fi
if [ -z "$DISPLAY_MODE" ]; then
  if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then DISPLAY_MODE=gtk; else DISPLAY_MODE=vnc; fi
fi
if [ $HEADLESS = 1 ]; then
  args+=(-display none)
elif [ "$DISPLAY_MODE" = vnc ]; then
  args+=(-display none -vnc 127.0.0.1:0)
  echo "No desktop display found: screen is on VNC at 127.0.0.1:5900 (use any VNC viewer)." >&2
elif [ "$DISPLAY_MODE" = sdl ]; then
  args+=(-display "sdl${DISP#gtk}")
else
  args+=(-display "$DISP,zoom-to-fit=on,show-tabs=off,show-menubar=off")
fi

# audio: PulseAudio (WSLg / desktop) -> ALSA -> none
if [ -n "${PULSE_SERVER:-}" ] || [ -S "${XDG_RUNTIME_DIR:-/nonexistent}/pulse/native" ]; then AUD=pa
elif [ -e /dev/snd ]; then AUD=alsa; else AUD=none; fi
args+=(-audiodev "$AUD,id=snd0" -device intel-hda -device hda-duplex,audiodev=snd0)

if [ $NET = 1 ]; then
  # forward guest ssh to the first free local port from 2222 (BRICK_SSH_PORT overrides)
  SSH_PORT=${BRICK_SSH_PORT:-$(python3 -I -c '
import socket
for p in range(2222, 2300):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); print(p); break
    except OSError:
        pass
    finally:
        s.close()')}
  if [ -n "$SSH_PORT" ]; then
    args+=(-netdev user,id=net0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22)
    echo "guest ssh forwarded to 127.0.0.1:$SSH_PORT" >&2
  else
    args+=(-netdev user,id=net0)
  fi
  args+=(-device virtio-net-pci,netdev=net0)
fi
[ -n "$MONITOR" ] && args+=(-monitor unix:"$MONITOR",server,nowait)
# hvc0 = optional console (--serial-sock), hvc1 = root shell channel
mkdir -p "$(dirname "$SHELLSOCK")"; rm -f "$SHELLSOCK"
args+=(-device virtio-serial-pci,id=vser
       -chardev socket,id=shell0,path="$SHELLSOCK",server=on,wait=off)
if [ -z "$SERSOCK" ]; then args+=(-chardev null,id=con0); fi
if [ -n "$SERSOCK" ]; then
  # console on a virtio console (hvc0): robust for socket clients
  args+=(-chardev socket,id=con0,path="$SERSOCK",server=on,wait=off${LOG:+,logfile=$LOG} -serial null)
  CMDLINE=${CMDLINE/console=ttyAMA0/console=hvc0}
  for i in "${!args[@]}"; do [ "${args[$i]}" = -append ] && args[$((i+1))]="$CMDLINE"; done
elif [ -n "$LOG" ]; then args+=(-serial file:"$LOG")
else args+=(-serial mon:stdio); fi
args+=(-device virtconsole,chardev=con0,bus=vser.0,nr=0 -device virtconsole,chardev=shell0,bus=vser.0,nr=1)

export LD_LIBRARY_PATH="$OUT/qemu/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$OUT/qemu/bin/qemu-system-aarch64" "${args[@]}"
