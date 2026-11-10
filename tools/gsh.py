#!/usr/bin/env python3
"""Run a shell command in the emulated Brick via serial-broker's control socket.
   gsh.py CTRL_SOCK 'command' [timeout_s]"""
import os, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
sys.exit(subprocess.call([sys.executable, '-I', os.path.join(here, 'serial-broker.py'), '--cmd'] + sys.argv[1:]))
