#!/bin/bash
# Boot the emulated TrimUI Brick.
# Usage: run.sh [SDCARD.img] [options]
#   --gpu virgl     use host GPU acceleration (virtio-gpu-gl + virgl)
#   --display D     skin | gtk | sdl | vnc (default: skin - Brick-shaped window with
#                   clickable buttons - if a desktop is available, else vnc)
#   --color C       skin colour: black | grey | white
#   --headless      no window (serial console only); use with --monitor
#   --monitor PATH  QEMU monitor on a unix socket (screendump, sendkey...)
#   --scale N       window scale factor (default: fit)
#   --log FILE      write the serial console to FILE instead of the terminal
#   --serial-sock P serial console on unix socket P (with --log as transcript)
#   --shell-sock P  root shell channel on unix socket P (see ./brick shell)
#   --agent-sock P  command channel for host tools (manager) on unix socket P
#   --emmc IMG      internal storage image (default out/emmc.img)
#   --no-net        no network
#   --wifi-ssid S   simulated Wi-Fi network name (default BrickNet)
#   --wifi-pass P   its WPA2 password, 8-63 chars (default brick1234; "" = open network)
#   --no-wifi       the simulated access point is off (the Brick finds no network)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# user data (images, sockets) and the read-only emulator assets (kernel,
# additions disk) - separate in the AppImage, both in out/ in a source tree
OUT="${BRICK_DATA:-$ROOT}/out"
ASSETS="${BRICK_ASSETS:-$OUT}"
PY="${BRICK_PYTHON:-python3}"
SD=""; GPU=soft; DISPLAY_MODE=""; EMMC=""; HEADLESS=0; MONITOR=""; LOG=""; NET=1; SCALE=""; SERSOCK=""; SHELLSOCK="$OUT/run/shell.sock"; AGENTSOCK="$OUT/run/agent.sock"; COLOR=${BRICK_SKIN_COLOR:-black}
WIFI=1; WIFI_SSID=${BRICK_WIFI_SSID:-BrickNet}; WIFI_PASS=${BRICK_WIFI_PASS-brick1234}
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
    --agent-sock) AGENTSOCK=$2; shift ;;
    --scale) SCALE=$2; shift ;;
    --color) COLOR=$2; shift ;;
    --wifi-ssid) WIFI_SSID=$2; shift ;;
    --wifi-pass) WIFI_PASS=$2; shift ;;
    --no-wifi) WIFI=0 ;;
    -*) echo "unknown option $1"; exit 1 ;;
    *) SD=$1 ;;
  esac
  shift
done
SD=${SD:-$OUT/sdcard.img}
EMMC=${EMMC:-$OUT/emmc.img}
for f in "$ASSETS/Image" "$EMMC" "$ASSETS/brick-emu.img"; do
  [ -f "$f" ] || { echo "missing $f - run ./brick setup first"; exit 1; }
done

PARTS="bootloader@mmcblk0p1:env@mmcblk0p2:env-redund@mmcblk0p3:boot@mmcblk0p4:rootfs@mmcblk0p5:rootfs_data@mmcblk0p6:private@mmcblk0p7:recovery@mmcblk0p8:pstore@mmcblk0p9:UDISK@mmcblk0p10"
CMDLINE="console=ttyAMA0 root=/dev/mmcblk0p5 rootwait init=/sbin/init rdinit=/rdinit loglevel=1 partitions=$PARTS cma=64M gpt=1"
CMDLINE+=" ${BRICK_CMDLINE_EXTRA:-} snd_hda_intel.id=audiocodec gpio_mockup.gpio_mockup_ranges=0,384 video=Virtual-1:1024x768 brick.gpu=$GPU"

args=(
  -name "TrimUI Brick"
  -M virt -cpu cortex-a53 -smp 4 -m 1024 -accel tcg,thread=multi,tb-size=512
  -kernel "$ASSETS/Image" -append "$CMDLINE"
  # eMMC (mmcblk0) and SD card slot (mmcblk1), real SD/MMC protocol over SDHCI
  -device sdhci-pci,id=emmc-host,addr=0x5
  -drive if=none,id=emmc,file="$EMMC",format=raw
  -device sd-card,drive=emmc,bus=/gpex-pcihost/pcie.0/emmc-host/sd-bus
  -device sdhci-pci,id=sd-host,addr=0x6
  # emulator additions (Mesa + EGL shim), read-only
  -drive if=none,id=emu,file="$ASSETS/brick-emu.img",format=raw,readonly=on
  -device virtio-blk-pci,drive=emu,addr=0x7
  -device virtio-keyboard-pci
  -device qemu-xhci -device usb-tablet
)
# simulated Wi-Fi settings for the guest's access point (guest/files/bin/wifi-ap):
# hex SSID + WPA2 PSK derived here, so no character can break the guest side
WIFI_CFG=$("$PY" -I -c '
import hashlib, sys
ssid, pw = sys.argv[1].encode(), sys.argv[2].encode()
if not 1 <= len(ssid) <= 32: sys.exit("Wi-Fi network name must be 1-32 bytes")
if pw and not 8 <= len(pw) <= 63: sys.exit("Wi-Fi password must be 8-63 characters (or empty for an open network)")
print("ssid=" + ssid.hex() + (" psk=" + hashlib.pbkdf2_hmac("sha1", pw, ssid, 4096, 32).hex() if pw else ""))
' "$WIFI_SSID" "$WIFI_PASS") || exit 1
args+=(-fw_cfg "name=opt/brick/wifi,string=enabled=$WIFI $WIFI_CFG")
if [ $WIFI = 1 ]; then
  if [ -n "$WIFI_PASS" ]; then echo "Wi-Fi: network \"$WIFI_SSID\", password \"$WIFI_PASS\"" >&2
  else echo "Wi-Fi: network \"$WIFI_SSID\", open (no password)" >&2; fi
fi
if [ -f "$SD" ]; then
  args+=(-drive if=none,id=sd,file="$SD",format=raw -device sd-card,drive=sd,bus=/gpex-pcihost/pcie.0/sd-host/sd-bus)
else
  echo "note: no SD card image ($SD) - booting stock firmware with an empty slot"
fi

if [ "$GPU" = virgl ]; then
  # WSL: Mesa must use the d3d12 driver (GPU via /dev/dxg); otherwise it probes
  # /dev/dri, falls back to zink and GL in the QEMU window fails.
  if [ -e /dev/dxg ] && [ -z "${GALLIUM_DRIVER:-}" ]; then export GALLIUM_DRIVER=d3d12; fi
  args+=(-device virtio-gpu-gl-pci,xres=1024,yres=768)
  DISP="gtk,gl=on"
else
  args+=(-device virtio-gpu-pci,xres=1024,yres=768)
  DISP="gtk"
fi
# QEMU monitor: always on a socket (default out/run/mon.sock) - used by the
# skin window (quit), the manager (status, power, screenshots) and qmon.py
mkdir -p "$OUT/run"
[ -n "$MONITOR" ] || { MONITOR="$OUT/run/mon.sock"; rm -f "$MONITOR"; }
DESKTOP=0; { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; } && DESKTOP=1
free_port() {  # first free local TCP port from $1
  "$PY" -I -c '
import socket, sys
for p in range(int(sys.argv[1]), int(sys.argv[1]) + 100):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); print(p); break
    except OSError:
        pass
    finally:
        s.close()' "$1"
}
if [ -z "$DISPLAY_MODE" ]; then
  if [ $DESKTOP = 0 ]; then DISPLAY_MODE=vnc
  elif "$PY" -I -c 'import gi, cairo; gi.require_version("Gtk", "3.0")' 2>/dev/null; then DISPLAY_MODE=skin
  # NVIDIA's proprietary driver: QEMU's GTK GL window stays black (Wayland) or
  # aborts (X11), while GPU rendering itself works -> render offscreen
  # (egl-headless) and show it through a local VNC viewer.
  elif [ "$GPU" = virgl ] && [ -e /proc/driver/nvidia/version ]; then DISPLAY_MODE=vnc
  else DISPLAY_MODE=gtk; fi
fi
if [ $HEADLESS = 1 ]; then
  # virtio-gpu-gl needs a GL-capable display backend even without a window
  if [ "$GPU" = virgl ]; then args+=(-display egl-headless); else args+=(-display none); fi
elif [ "$DISPLAY_MODE" = vnc ] || [ "$DISPLAY_MODE" = skin ]; then
  # QEMU renders offscreen (egl-headless on the host GPU for virgl) and serves
  # the screen over VNC on localhost; the skin window / a VNC viewer shows it.
  if [ "$GPU" = virgl ]; then args+=(-display egl-headless); else args+=(-display none); fi
  VNC_PORT=${BRICK_VNC_PORT:-$(free_port 5900)}
  args+=(-vnc 127.0.0.1:$((VNC_PORT - 5900)))
  if [ "$DISPLAY_MODE" = skin ]; then
    # closing the window quits QEMU through the monitor
    "$PY" -I "$ROOT/tools/brick-window.py" --vnc 127.0.0.1:$VNC_PORT --monitor "$MONITOR" \
      --color "$COLOR" ${SCALE:+--scale "$SCALE"} </dev/null >/dev/null 2>"$OUT/run/window.log" &
  else
    echo "screen is on VNC at 127.0.0.1:$VNC_PORT" >&2
    if [ $DESKTOP = 1 ] && command -v vncviewer >/dev/null; then
      # TigerVNC viewer as the emulator window; it exits when QEMU does, and
      # closing it quits QEMU (through the monitor)
      ( for i in $(seq 100); do (exec 3<>/dev/tcp/127.0.0.1/$VNC_PORT) 2>/dev/null && break; sleep 0.1; done
        vncviewer -Shared -AcceptClipboard=0 -SendClipboard=0 -SetPrimary=0 -RemoteResize=0 \
          -ReconnectOnError=0 -Log '*:stderr:0' 127.0.0.1::$VNC_PORT || true
        [ -S "$MONITOR" ] && "$PY" -I "$ROOT/tools/qmon.py" "$MONITOR" quit ) </dev/null >/dev/null 2>&1 &
    fi
  fi
elif [ "$DISPLAY_MODE" = sdl ]; then
  args+=(-display "sdl${DISP#gtk}")
else
  args+=(-display "$DISP,zoom-to-fit=on,show-tabs=off,show-menubar=off")
fi

# audio: PulseAudio / PipeWire-pulse (desktop, WSLg) -> ALSA -> none
if [ -n "${PULSE_SERVER:-}" ] || [ -S "${XDG_RUNTIME_DIR:-/nonexistent}/pulse/native" ]; then AUD=pa
elif [ -e /dev/snd ]; then AUD=alsa; else AUD=none; fi
args+=(-audiodev "$AUD,id=snd0" -device intel-hda -device hda-duplex,audiodev=snd0)

if [ $NET = 1 ]; then
  # forward guest ssh to the first free local port from 2222 (BRICK_SSH_PORT overrides)
  SSH_PORT=${BRICK_SSH_PORT:-$(free_port 2222)}
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
mkdir -p "$(dirname "$SHELLSOCK")" "$(dirname "$AGENTSOCK")"; rm -f "$SHELLSOCK" "$AGENTSOCK"
args+=(-device virtio-serial-pci,id=vser
       -chardev socket,id=shell0,path="$SHELLSOCK",server=on,wait=off
       -chardev socket,id=agent0,path="$AGENTSOCK",server=on,wait=off)
if [ -z "$SERSOCK" ]; then args+=(-chardev null,id=con0); fi
if [ -n "$SERSOCK" ]; then
  # console on a virtio console (hvc0): robust for socket clients
  args+=(-chardev socket,id=con0,path="$SERSOCK",server=on,wait=off${LOG:+,logfile=$LOG} -serial null)
  CMDLINE=${CMDLINE/console=ttyAMA0/console=hvc0}
  for i in "${!args[@]}"; do [ "${args[$i]}" = -append ] && args[$((i+1))]="$CMDLINE"; done
elif [ -n "$LOG" ]; then args+=(-serial file:"$LOG")
else
  args+=(-serial mon:stdio)
  echo "this terminal is the Brick's serial console; Ctrl-a x quits the emulator" >&2
fi
args+=(-device virtconsole,chardev=con0,bus=vser.0,nr=0 -device virtconsole,chardev=shell0,bus=vser.0,nr=1
       -device virtconsole,chardev=agent0,bus=vser.0,nr=2)

# QEMU: BRICK_QEMU=path overrides; otherwise the distro's QEMU if installed (built
# against the host's GTK/virglrenderer/GPU driver - required for virgl on NVIDIA),
# else the bundled build in out/qemu. BRICK_QEMU=bundled forces the bundled one.
QEMU=${BRICK_QEMU:-}
if [ -z "$QEMU" ] && SYSQ=$(command -v qemu-system-aarch64) &&
   { [ "$GPU" != virgl ] || "$SYSQ" -device help 2>/dev/null | grep -q '"virtio-gpu-gl-pci"'; }; then
  QEMU=$SYSQ
fi
if [ -n "$QEMU" ] && [ "$QEMU" != bundled ]; then
  echo "using $("$QEMU" --version | head -1) ($QEMU)" >&2
  exec "$QEMU" "${args[@]}"
fi
[ -x "$OUT/qemu/bin/qemu-system-aarch64" ] || { echo "no QEMU: install your distro's qemu-system-aarch64 or run ./brick setup"; exit 1; }
[ "$GPU" = virgl ] && echo "note: bundled QEMU; if virgl fails (e.g. NVIDIA), install the distro's QEMU (see README)" >&2
export LD_LIBRARY_PATH="$OUT/qemu/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$OUT/qemu/bin/qemu-system-aarch64" "${args[@]}"
