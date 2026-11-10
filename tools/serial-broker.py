#!/usr/bin/env python3
"""Hold ONE persistent connection to the emulator's serial socket (QEMU's
PL011 stalls if clients come and go while the guest prints), log all output,
and accept shell commands on a second unix socket.

  serial-broker.py SER_SOCK CTRL_SOCK LOGFILE      (run in background)
  serial-broker.py --cmd CTRL_SOCK 'command' [timeout]
"""
import os, re, socket, sys, threading, time

def broker(ser_path, ctrl_path, log_path):
    ser = socket.socket(socket.AF_UNIX)
    for _ in range(100):
        try:
            ser.connect(ser_path); break
        except OSError:
            time.sleep(0.2)
    log = open(log_path, 'ab', buffering=0)
    buf = bytearray(); lock = threading.Lock()
    def reader():
        while True:
            d = ser.recv(65536)
            if not d:
                os._exit(0)
            log.write(d)
            with lock:
                buf.extend(d)
                del buf[:-1 << 20]
    threading.Thread(target=reader, daemon=True).start()
    if os.path.exists(ctrl_path):
        os.unlink(ctrl_path)
    srv = socket.socket(socket.AF_UNIX); srv.bind(ctrl_path); srv.listen(4)
    while True:
        c, _ = srv.accept()
        req = c.recv(65536).decode()
        tmo, cmd = req.split('\n', 1)
        tag = f'__E{int(time.time() * 1000) % 1000000}__'
        with lock:
            start = len(buf)
        ser.sendall((cmd + f'; echo {tag[:3]}""{tag[3:]}\n').encode())
        end = time.time() + float(tmo)
        out = ''
        while time.time() < end:
            with lock:
                out = buf[start:].decode(errors='replace').replace('\r', '')
            if re.search(r'^' + re.escape(tag) + r'$', out, re.M):
                break
            time.sleep(0.1)
        lines = out.split('\n')
        try:
            first = next(i for i, l in enumerate(lines) if tag[:3] + '""' in l)
        except StopIteration:
            first = 0
        res = []
        for l in lines[first + 1:]:
            if l.strip() == tag:
                break
            res.append(l)
        c.sendall(re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', '\n'.join(res)).encode())
        c.close()

def cmd(ctrl_path, command, tmo):
    c = socket.socket(socket.AF_UNIX); c.connect(ctrl_path)
    c.sendall(f'{tmo}\n{command}'.encode())
    data = b''
    while True:
        d = c.recv(65536)
        if not d:
            break
        data += d
    print(data.decode(errors='replace'))

if __name__ == '__main__':
    if sys.argv[1] == '--cmd':
        cmd(sys.argv[2], sys.argv[3], float(sys.argv[4]) if len(sys.argv) > 4 else 20)
    else:
        broker(*sys.argv[1:4])
