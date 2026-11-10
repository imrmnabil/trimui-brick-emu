#!/bin/bash
# Test harness: boot headless; console logged to build/test/serial.log,
# root shell (hvc1) driven through build/test/ctl.sock (see tools/gsh.py).
#   test-boot.sh [SD.img] [run.sh options]     |   test-boot.sh --stop
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; T="$ROOT/build/test"; mkdir -p "$T"
stop() {
  for p in qemu broker conlog; do [ -f "$T/$p.pid" ] && kill "$(cat "$T/$p.pid")" 2>/dev/null; rm -f "$T/$p.pid"; done
  sleep 1
}
stop
[ "${1:-}" = --stop ] && exit 0
rm -f "$T"/serial.log "$T"/shell.log "$T"/*.sock
cd "$ROOT"
./scripts/run.sh "${1:-out/sd-nextui.img}" --headless --monitor build/test/mon.sock \
   --serial-sock build/test/ser.sock --shell-sock build/test/shell.sock "${@:2}" > build/test/qemu.log 2>&1 &
echo $! > "$T/qemu.pid"
for i in $(seq 50); do [ -S "$T/ser.sock" ] && [ -S "$T/shell.sock" ] && break; sleep 0.2; done
python3 -I tools/serial-broker.py build/test/ser.sock build/test/ctl-con.sock build/test/serial.log >/dev/null 2>&1 &
echo $! > "$T/conlog.pid"
python3 -I tools/serial-broker.py build/test/shell.sock build/test/ctl.sock build/test/shell.log >/dev/null 2>&1 &
echo $! > "$T/broker.pid"
timeout 240 bash -c "until grep -qa 'Please press Enter' '$T/serial.log' 2>/dev/null; do sleep 2; done" && echo "booted"
