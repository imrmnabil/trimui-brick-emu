#!/usr/bin/env python3
"""Send HMP commands to the emulator's QEMU monitor socket.

  qmon.py SOCK "screendump /tmp/x.png -f png"   run commands, print output
  qmon.py SOCK --press KEY [ms]                 tap a key (qemu sendkey name)
"""
import socket, sys, time

def hmp(sock_path, cmds):
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock_path)
    s.settimeout(3)
    def read_prompt():
        buf = b''
        while not buf.rstrip().endswith(b'(qemu)'):
            try:
                d = s.recv(65536)
            except socket.timeout:
                break
            if not d:
                break
            buf += d
        return buf.decode(errors='replace')
    read_prompt()
    out = []
    for c in cmds:
        s.sendall(c.encode() + b'\n')
        out.append(read_prompt())
    s.close()
    return out

if __name__ == '__main__':
    sock = sys.argv[1]
    if sys.argv[2] == '--press':
        ms = sys.argv[4] if len(sys.argv) > 4 else '150'
        hmp(sock, [f'sendkey {sys.argv[3]} {ms}'])
        time.sleep(int(ms) / 1000 + 0.1)
    else:
        for o in hmp(sock, sys.argv[2:]):
            import re
            o = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', o).replace('\r', '')
            lines = o.split('\n')[1:]  # drop the echoed command line
            lines = [l for l in lines if l.strip() and not l.startswith('(qemu)')]
            if lines:
                print('\n'.join(lines))
