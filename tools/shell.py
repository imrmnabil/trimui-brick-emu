#!/usr/bin/env python3
"""Interactive terminal to the emulated Brick's root shell channel (hvc1).
   shell.py SOCK        (Ctrl-] to detach)"""
import os, select, socket, sys, termios, tty
s = socket.socket(socket.AF_UNIX)
try:
    s.connect(sys.argv[1])
except OSError:
    sys.exit(f"cannot connect to {sys.argv[1]} - is the emulator running?")
print("Connected to the Brick (root shell). Ctrl-] to detach.\r")
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
try:
    tty.setraw(fd)
    s.sendall(b'\n')
    while True:
        r, _, _ = select.select([fd, s], [], [])
        if s in r:
            d = s.recv(65536)
            if not d:
                break
            os.write(sys.stdout.fileno(), d)
        if fd in r:
            d = os.read(fd, 1024)
            if b'\x1d' in d:
                break
            s.sendall(d)
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print()
