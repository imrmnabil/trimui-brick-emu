#!/usr/bin/env python3
"""Brick-shaped emulator window: shows the emulated screen and all the
device's buttons (clickable), and forwards the keyboard.

The screen comes from QEMU's built-in VNC server (works with software
rendering and with virgl/egl-headless alike); buttons and keys are sent
back as VNC key events, which reach the guest as virtio-keyboard keys that
brick-hw maps to the Brick's buttons.

  brick-window.py --vnc 127.0.0.1:5900 [--monitor SOCK] [--scale F] [--color black|white|grey]

Mouse:  left button = press/hold a button,  right button = latch it down
        (toggle; for combos), drag the body to move the window,
        Ctrl+wheel = zoom.     Keys: as in README; Ctrl+Q / close button = quit.
"""
import argparse, math, os, socket, struct, sys, threading, time

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GLib
import cairo
import numpy as np

# ---------------------------------------------------------------- buttons
# name: (evdev keycode, X keysym) - the host keys brick-hw maps (see brick-hw.c)
KEYS = {
    'UP': (103, 0xff52), 'DOWN': (108, 0xff54), 'LEFT': (105, 0xff51), 'RIGHT': (106, 0xff53),
    'A': (45, 0x78), 'B': (44, 0x7a), 'X': (31, 0x73), 'Y': (30, 0x61),
    'L': (16, 0x71), 'R': (17, 0x77), 'L2': (18, 0x65), 'R2': (19, 0x72),
    'START': (28, 0xff0d), 'SELECT': (54, 0xffe2), 'MENU': (1, 0xff1b),
    'F1': (2, 0x31), 'F2': (3, 0x32),
    'VOL+': (13, 0x3d), 'VOL-': (12, 0x2d), 'POWER': (25, 0x70),
}
# other host keys that also map to a button (for highlighting)
ALIAS = {96: 'START', 14: 'SELECT', 50: 'MENU', 78: 'VOL+', 74: 'VOL-', 107: 'POWER'}
EVDEV_BTN = {v[0]: k for k, v in KEYS.items()}
EVDEV_BTN.update(ALIAS)

# evdev -> QEMU "qnum" (XT set 1, 0xe0-prefixed keys have bit 7 set); for the
# QEMU extended key event, so keys don't depend on the server's keymap.
EXT_QNUM = {96: 0x9c, 97: 0x9d, 98: 0xb5, 99: 0xb7, 100: 0xb8, 102: 0xc7, 103: 0xc8, 104: 0xc9,
            105: 0xcb, 106: 0xcd, 107: 0xcf, 108: 0xd0, 109: 0xd1, 110: 0xd2, 111: 0xd3,
            125: 0xdb, 126: 0xdc, 127: 0xdd}


def qnum(evdev):
    if 0 < evdev < 89:
        return evdev
    return EXT_QNUM.get(evdev, 0)


# ---------------------------------------------------------------- skin layout
# Device units (~0.155 mm); the window is drawn at SKIN_W x SKIN_H * scale.
SKIN_W, SKIN_H = 560, 830
BODY = (20, 30, 500, 780, 42)           # x, y, w, h, corner radius
BEZEL = (42, 56, 456, 362, 16)
SCREEN = (60, 74, 420, 315)             # 4:3, the 1024x768 panel
DPAD_C, DPAD_ARM, DPAD_LEN = (140, 575), 52, 160
ABXY_C, ABXY_OFF, ABXY_R = (400, 575), 56, 28

THEMES = {
    'black': dict(body0=(0.17, 0.18, 0.19), body1=(0.10, 0.105, 0.11), edge=(0.30, 0.31, 0.33),
                  btn=(0.24, 0.25, 0.27), btn_hi=(0.36, 0.37, 0.40), btn_dn=(0.12, 0.12, 0.13),
                  label=(0.74, 0.76, 0.79), print=(0.55, 0.57, 0.60), accent=(0.95, 0.35, 0.45)),
    'grey': dict(body0=(0.56, 0.57, 0.58), body1=(0.44, 0.45, 0.46), edge=(0.66, 0.67, 0.68),
                 btn=(0.26, 0.27, 0.29), btn_hi=(0.38, 0.39, 0.42), btn_dn=(0.14, 0.14, 0.15),
                 label=(0.90, 0.91, 0.92), print=(0.20, 0.21, 0.22), accent=(0.95, 0.35, 0.45)),
    'white': dict(body0=(0.94, 0.94, 0.93), body1=(0.84, 0.84, 0.83), edge=(0.98, 0.98, 0.98),
                  btn=(0.30, 0.31, 0.33), btn_hi=(0.42, 0.43, 0.46), btn_dn=(0.16, 0.16, 0.17),
                  label=(0.92, 0.92, 0.93), print=(0.35, 0.36, 0.38), accent=(0.95, 0.35, 0.45)),
}


def build_buttons():
    """name -> (kind, geometry, label). kinds: circle(cx,cy,r) pill/rect(x,y,w,h,r) dpad(x,y,w,h,dir)."""
    b = {}
    cx, cy = DPAD_C
    a, l = DPAD_ARM, DPAD_LEN / 2
    b['UP'] = ('dpad', (cx - a / 2, cy - l, a, l - a / 2), 'up')
    b['DOWN'] = ('dpad', (cx - a / 2, cy + a / 2, a, l - a / 2), 'down')
    b['LEFT'] = ('dpad', (cx - l, cy - a / 2, l - a / 2, a), 'left')
    b['RIGHT'] = ('dpad', (cx + a / 2, cy - a / 2, l - a / 2, a), 'right')
    ax, ay = ABXY_C
    b['X'] = ('circle', (ax, ay - ABXY_OFF, ABXY_R), 'X')
    b['A'] = ('circle', (ax + ABXY_OFF, ay, ABXY_R), 'A')
    b['B'] = ('circle', (ax, ay + ABXY_OFF, ABXY_R), 'B')
    b['Y'] = ('circle', (ax - ABXY_OFF, ay, ABXY_R), 'Y')
    b['MENU'] = ('circle', (270, 462, 18), 'MENU')
    b['F1'] = ('circle', (232, 640, 12), 'F1')
    b['F2'] = ('circle', (308, 640, 12), 'F2')
    b['SELECT'] = ('pill', (190, 700, 60, 20, 10), 'SELECT')
    b['START'] = ('pill', (290, 700, 60, 20, 10), 'START')
    # shoulder buttons: tabs on the top edge, behind the body
    b['L2'] = ('shoulder', (44, 12, 88, 30, 9), 'L2')
    b['L'] = ('shoulder', (140, 16, 88, 26, 9), 'L')
    b['R'] = ('shoulder', (312, 16, 88, 26, 9), 'R')
    b['R2'] = ('shoulder', (408, 12, 88, 30, 9), 'R2')
    # right edge: power, volume
    b['POWER'] = ('side', (512, 110, 16, 56, 5), 'POWER')
    b['VOL+'] = ('side', (512, 200, 16, 50, 5), '+')
    b['VOL-'] = ('side', (512, 256, 16, 50, 5), '−')
    return b


BUTTONS = build_buttons()
CLOSE_BTN = (488, 52, 11)  # top-right corner of the body (shown on hover)


def rrect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def hit(name, x, y):
    kind, g, _ = BUTTONS[name]
    if kind == 'circle':
        return (x - g[0]) ** 2 + (y - g[1]) ** 2 <= (g[2] + 6) ** 2
    gx, gy, gw, gh = g[:4]
    pad = 4
    return gx - pad <= x <= gx + gw + pad and gy - pad <= y <= gy + gh + pad


# ---------------------------------------------------------------- VNC client
class VNC(threading.Thread):
    """Minimal RFB 3.8 client: raw encoding + DesktopSize + QEMU extended key events."""

    def __init__(self, host, port, on_frame, on_resize, on_close):
        super().__init__(daemon=True)
        self.host, self.port = host, port
        self.on_frame, self.on_resize, self.on_close = on_frame, on_resize, on_close
        self.sock = None
        self.lock = threading.Lock()
        self.ext_keys = False
        self.w = self.h = 0
        self.fb = None

    def send(self, data):
        if self.sock:
            with self.lock:
                try:
                    self.sock.sendall(data)
                except OSError:
                    pass

    def rx(self, n):
        buf = bytearray(n)
        mv = memoryview(buf)
        got = 0
        while got < n:
            k = self.sock.recv_into(mv[got:], n - got)
            if not k:
                raise EOFError
            got += k
        return buf

    def key(self, evdev, keysym, down):
        if self.ext_keys and qnum(evdev):
            self.send(struct.pack('>BBHII', 255, 0, 1 if down else 0, keysym, qnum(evdev)))
        elif keysym:
            self.send(struct.pack('>BBxxI', 4, 1 if down else 0, keysym))

    def alloc(self, w, h):
        self.w, self.h = w, h
        self.fb = np.zeros((h, w, 4), dtype=np.uint8)
        self.on_resize(w, h, self.fb)

    def run(self):
        deadline = time.time() + 90
        while True:
            try:
                self.sock = socket.create_connection((self.host, self.port), timeout=2)
                break
            except OSError:
                if time.time() > deadline:
                    self.on_close('cannot connect to the emulator display')
                    return
                time.sleep(0.2)
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            self.rx(12)
            self.sock.sendall(b'RFB 003.008\n')
            n = self.rx(1)[0]
            types = self.rx(n)
            if 1 not in types:
                raise RuntimeError('VNC server requires authentication')
            self.sock.sendall(b'\x01')
            if struct.unpack('>I', self.rx(4))[0] != 0:
                raise RuntimeError('VNC security handshake failed')
            self.sock.sendall(b'\x01')  # shared session
            w, h = struct.unpack('>HH', self.rx(4))
            self.rx(16)
            self.rx(struct.unpack('>I', self.rx(4))[0])
            self.alloc(w, h)
            # 32bpp little endian xRGB == cairo FORMAT_RGB24 in memory
            self.send(struct.pack('>BxxxBBBBHHHBBBxxx', 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
            encs = [0, -223, -258]  # raw, DesktopSize, QEMU extended key event
            self.send(struct.pack('>BxH', 2, len(encs)) + b''.join(struct.pack('>i', e) for e in encs))
            self.send(struct.pack('>BBHHHH', 3, 0, 0, 0, w, h))
            while True:
                t = self.rx(1)[0]
                if t == 0:
                    self.rx(1)
                    nr = struct.unpack('>H', self.rx(2))[0]
                    for _ in range(nr):
                        x, y, rw, rh, enc = struct.unpack('>HHHHi', self.rx(12))
                        if enc == 0:
                            data = np.frombuffer(self.rx(rw * rh * 4), dtype=np.uint8).reshape(rh, rw, 4)
                            self.fb[y:y + rh, x:x + rw] = data
                        elif enc == -223:
                            self.alloc(rw, rh)
                        elif enc == -258:
                            self.ext_keys = True
                        else:
                            raise RuntimeError(f'unexpected encoding {enc}')
                    self.on_frame()
                    self.send(struct.pack('>BBHHHH', 3, 1, 0, 0, self.w, self.h))
                elif t == 1:  # colour map (not used with true colour)
                    _, _, n = struct.unpack('>xHH', self.rx(5))
                    self.rx(n * 6)
                elif t == 2:  # bell
                    pass
                elif t == 3:  # server cut text
                    self.rx(3)
                    self.rx(struct.unpack('>I', self.rx(4))[0])
                else:
                    raise RuntimeError(f'unexpected VNC message {t}')
        except (EOFError, OSError):
            self.on_close(None)
        except RuntimeError as e:
            self.on_close(str(e))


# ---------------------------------------------------------------- window
class BrickWindow(Gtk.Window):
    def __init__(self, args):
        super().__init__(title='TrimUI Brick')
        self.args = args
        self.theme = THEMES[args.color]
        self.pressed = set()      # held by mouse / keyboard
        self.latched = set()      # toggled with right click
        self.mouse_btn = None
        self.keys_down = {}       # evdev -> keysym currently down on the guest
        self.hover_close = False
        self.frame_pending = False
        self.img = None
        self.skin_cache = None
        self.message = 'starting…'

        self.set_decorated(False)
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        self.transparent = visual is not None and self.get_screen().is_composited()
        if visual is not None:
            self.set_visual(visual)
        self.scale = args.scale or self.fit_scale()
        self.set_default_size(int(SKIN_W * self.scale), int(SKIN_H * self.scale))
        self.set_resizable(False)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK |
                        Gdk.EventMask.KEY_PRESS_MASK | Gdk.EventMask.KEY_RELEASE_MASK |
                        Gdk.EventMask.FOCUS_CHANGE_MASK)
        self.connect('draw', self.on_draw)
        self.connect('button-press-event', self.on_press)
        self.connect('button-release-event', self.on_release)
        self.connect('motion-notify-event', self.on_motion)
        self.connect('scroll-event', self.on_scroll)
        self.connect('key-press-event', self.on_key)
        self.connect('key-release-event', self.on_key)
        self.connect('focus-out-event', self.on_focus_out)
        self.connect('delete-event', lambda *a: self.quit() or True)

        host, port = args.vnc.rsplit(':', 1)
        self.vnc = VNC(host, int(port), self.frame_cb, self.resize_cb, self.close_cb)
        self.vnc.start()

    def fit_scale(self):
        try:
            mon = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
            area = mon.get_workarea()
            s = min(area.height * 0.92 / SKIN_H, area.width * 0.9 / SKIN_W)
            return max(0.5, min(s, 2.0))
        except Exception:
            return 1.0

    # --- VNC callbacks (from the client thread)
    def resize_cb(self, w, h, fb):
        def apply():
            stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_RGB24, w)
            assert stride == w * 4
            self.img = cairo.ImageSurface.create_for_data(memoryview(fb).cast('B'), cairo.FORMAT_RGB24, w, h, stride)
            self.message = None
            return False
        GLib.idle_add(apply)

    def frame_cb(self):
        if not self.frame_pending:
            self.frame_pending = True
            GLib.idle_add(self.redraw_screen)

    def close_cb(self, err):
        def done():
            if err:
                print(f'brick-window: {err}', file=sys.stderr)
            Gtk.main_quit()
            return False
        GLib.idle_add(done)

    def redraw_screen(self):
        self.frame_pending = False
        if self.img is not None:
            self.img.mark_dirty()
        x, y, w, h = SCREEN
        s = self.scale
        self.queue_draw_area(int(x * s) - 1, int(y * s) - 1, int(w * s) + 3, int(h * s) + 3)
        return False

    # --- input
    def to_skin(self, ev):
        return ev.x / self.scale, ev.y / self.scale

    def button_at(self, x, y):
        for name in BUTTONS:
            if hit(name, x, y):
                return name
        return None

    def set_button(self, name, down):
        evdev, keysym = KEYS[name]
        held = name in self.pressed or name in self.latched
        if down and not held:
            self.vnc.key(evdev, keysym, True)
        if down:
            self.pressed.add(name)
        else:
            self.pressed.discard(name)
            if name not in self.latched and held:
                self.vnc.key(evdev, keysym, False)
        self.queue_draw()

    def on_press(self, w, ev):
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return True
        x, y = self.to_skin(ev)
        if (x - CLOSE_BTN[0]) ** 2 + (y - CLOSE_BTN[1]) ** 2 <= CLOSE_BTN[2] ** 2 + 30:
            self.quit()
            return True
        name = self.button_at(x, y)
        if name and ev.button == 3:
            evdev, keysym = KEYS[name]
            if name in self.latched:
                self.latched.discard(name)
                if name not in self.pressed:
                    self.vnc.key(evdev, keysym, False)
            else:
                if name not in self.pressed:
                    self.vnc.key(evdev, keysym, True)
                self.latched.add(name)
            self.queue_draw()
        elif name and ev.button == 1:
            self.mouse_btn = name
            self.set_button(name, True)
        elif ev.button == 1:
            self.begin_move_drag(ev.button, int(ev.x_root), int(ev.y_root), ev.time)
        return True

    def on_release(self, w, ev):
        if ev.button == 1 and self.mouse_btn:
            self.set_button(self.mouse_btn, False)
            self.mouse_btn = None
        return True

    def on_motion(self, w, ev):
        x, y = self.to_skin(ev)
        hc = (x - CLOSE_BTN[0]) ** 2 + (y - CLOSE_BTN[1]) ** 2 <= (CLOSE_BTN[2] + 14) ** 2
        if hc != self.hover_close:
            self.hover_close = hc
            self.queue_draw()
        # sliding off a held button releases it; sliding onto another presses it (d-pad rolls)
        if self.mouse_btn:
            name = self.button_at(x, y)
            if name != self.mouse_btn:
                self.set_button(self.mouse_btn, False)
                self.mouse_btn = name
                if name:
                    self.set_button(name, True)
        return True

    def on_scroll(self, w, ev):
        if not ev.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        d = -1 if ev.direction == Gdk.ScrollDirection.DOWN else 1 if ev.direction == Gdk.ScrollDirection.UP else 0
        if ev.direction == Gdk.ScrollDirection.SMOOTH:
            d = -1 if ev.delta_y > 0 else 1 if ev.delta_y < 0 else 0
        if d:
            self.scale = max(0.4, min(3.0, self.scale * (1.08 if d > 0 else 1 / 1.08)))
            self.skin_cache = None
            self.resize(int(SKIN_W * self.scale), int(SKIN_H * self.scale))
            self.queue_draw()
        return True

    def on_key(self, w, ev):
        down = ev.type == Gdk.EventType.KEY_PRESS
        if down and ev.state & Gdk.ModifierType.CONTROL_MASK and ev.keyval in (Gdk.KEY_q, Gdk.KEY_Q):
            self.quit()
            return True
        evdev = ev.hardware_keycode - 8
        if down:
            if evdev in self.keys_down:
                return True  # autorepeat: the guest does its own
            self.keys_down[evdev] = ev.keyval
            self.vnc.key(evdev, ev.keyval, True)
        else:
            keysym = self.keys_down.pop(evdev, ev.keyval)
            self.vnc.key(evdev, keysym, False)
        name = EVDEV_BTN.get(evdev)
        if name:
            (self.pressed.add if down else self.pressed.discard)(name)
            self.queue_draw()
        return True

    def on_focus_out(self, *a):
        for evdev, keysym in list(self.keys_down.items()):
            self.vnc.key(evdev, keysym, False)
            name = EVDEV_BTN.get(evdev)
            if name:
                self.pressed.discard(name)
        self.keys_down.clear()
        self.queue_draw()
        return False

    def quit(self):
        if self.args.monitor:
            try:
                s = socket.socket(socket.AF_UNIX)
                s.settimeout(2)
                s.connect(self.args.monitor)
                s.sendall(b'quit\n')
                time.sleep(0.2)
                s.close()
            except OSError:
                pass
        Gtk.main_quit()

    # --- drawing
    def on_draw(self, w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.scale(self.scale, self.scale)
        self.draw_shoulders_and_sides(cr)
        if self.skin_cache is None:
            self.skin_cache = self.render_body()
        cr.save()
        cr.scale(1 / self.scale, 1 / self.scale)
        cr.set_source_surface(self.skin_cache, 0, 0)
        cr.paint()
        cr.restore()
        self.draw_screen(cr)
        self.draw_controls(cr)
        if self.hover_close:
            x, y, r = CLOSE_BTN
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.set_source_rgba(0.85, 0.25, 0.25, 0.95)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            cr.set_line_width(2)
            for dx in (-1, 1):
                cr.move_to(x - 4.5, y - 4.5 * dx)
                cr.line_to(x + 4.5, y + 4.5 * dx)
            cr.stroke()
        return True

    def render_body(self):
        s = self.scale
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(SKIN_W * s) + 1, int(SKIN_H * s) + 1)
        cr = cairo.Context(surf)
        cr.scale(s, s)
        t = self.theme
        x, y, w, h, r = BODY
        # soft shadow
        if self.transparent:
            for i in range(10, 0, -1):
                rrect(cr, x - i * 0.6 + 2, y + 4 - i * 0.3, w + i * 1.2, h + i * 1.2, r + i)
                cr.set_source_rgba(0, 0, 0, 0.022)
                cr.fill()
        # body with a vertical gradient and a light rim
        g = cairo.LinearGradient(0, y, 0, y + h)
        g.add_color_stop_rgb(0, *t['body0'])
        g.add_color_stop_rgb(1, *t['body1'])
        rrect(cr, x, y, w, h, r)
        cr.set_source(g)
        cr.fill_preserve()
        cr.set_source_rgb(*t['edge'])
        cr.set_line_width(2)
        cr.stroke()
        rrect(cr, x + 5, y + 5, w - 10, h - 10, r - 5)
        cr.set_source_rgba(1, 1, 1, 0.035)
        cr.set_line_width(1.5)
        cr.stroke()
        # screen bezel (glass)
        bx, by, bw, bh, br = BEZEL
        rrect(cr, bx, by, bw, bh, br)
        cr.set_source_rgb(0.035, 0.035, 0.04)
        cr.fill()
        gl = cairo.LinearGradient(bx, by, bx + bw * 0.6, by + bh)
        gl.add_color_stop_rgba(0, 1, 1, 1, 0.06)
        gl.add_color_stop_rgba(0.5, 1, 1, 1, 0.0)
        rrect(cr, bx, by, bw, bh, br)
        cr.set_source(gl)
        cr.fill()
        # power LED and printed model name under the screen
        cr.arc(bx + 14, by + bh + 22, 3, 0, 2 * math.pi)
        cr.set_source_rgba(0.25, 0.85, 0.45, 0.9)
        cr.fill()
        cr.set_source_rgb(*t['print'])
        cr.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(13)
        self.text(cr, 'BRICK', bx + bw - 40, by + bh + 26, center=True)
        # d-pad and face-button wells
        cx, cy = DPAD_C
        cr.arc(cx, cy, DPAD_LEN / 2 + 6, 0, 2 * math.pi)
        cr.set_source_rgba(0, 0, 0, 0.16)
        cr.fill()
        ax, ay = ABXY_C
        cr.arc(ax, ay, ABXY_OFF + ABXY_R + 4, 0, 2 * math.pi)
        cr.set_source_rgba(0, 0, 0, 0.16)
        cr.fill()
        # speaker grille
        for i in range(7):
            for j in range(3):
                cr.new_sub_path()
                cr.arc(222 + i * 16 + (8 if j % 2 else 0), 752 + j * 13, 2.6, 0, 2 * math.pi)
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.fill()
        return surf

    def text(self, cr, s, x, y, center=True):
        ext = cr.text_extents(s)
        cr.move_to(x - (ext.width / 2 + ext.x_bearing if center else 0), y)
        cr.show_text(s)

    def is_down(self, name):
        return name in self.pressed or name in self.latched

    def btn_color(self, name):
        t = self.theme
        return t['btn_dn'] if self.is_down(name) else t['btn']

    def draw_shoulders_and_sides(self, cr):
        t = self.theme
        for name in ('L2', 'L', 'R', 'R2', 'POWER', 'VOL+', 'VOL-'):
            kind, (x, y, w, h, r), label = BUTTONS[name]
            dn = self.is_down(name)
            if kind == 'shoulder':
                yy = y + (4 if dn else 0)
                rrect(cr, x, yy, w, h + 20, r)
            else:
                xx = x - (4 if dn else 0)
                rrect(cr, xx - 10, y, w + 10, h, r)
            cr.set_source_rgb(*(t['btn_dn'] if dn else t['btn']))
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.08)
            cr.set_line_width(1)
            cr.stroke()
            if name in self.latched:
                self.latch_mark(cr, x + w / 2, y + 8 if kind == 'shoulder' else y + h / 2)

    def draw_screen(self, cr):
        x, y, w, h = SCREEN
        cr.save()
        cr.rectangle(x, y, w, h)
        cr.clip()
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        if self.img is not None:
            iw, ih = self.img.get_width(), self.img.get_height()
            cr.translate(x, y)
            cr.scale(w / iw, h / ih)
            cr.set_source_surface(self.img, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint()
        elif self.message:
            cr.set_source_rgb(0.5, 0.5, 0.55)
            cr.select_font_face('Sans')
            cr.set_font_size(16)
            self.text(cr, self.message, x + w / 2, y + h / 2)
        cr.restore()

    def latch_mark(self, cr, x, y):
        cr.arc(x, y, 3.5, 0, 2 * math.pi)
        cr.set_source_rgb(*self.theme['accent'])
        cr.fill()

    def draw_controls(self, cr):
        t = self.theme
        cr.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        # shoulder / side labels on the body edge
        cr.set_font_size(11)
        cr.set_source_rgb(*t['print'])
        for name in ('L2', 'L', 'R', 'R2'):
            _, (x, y, w, h, r), label = BUTTONS[name]
            self.text(cr, label, x + w / 2, BODY[1] + 18)
        # d-pad: one cross, the pressed arm darkened
        cx, cy = DPAD_C
        a, l = DPAD_ARM, DPAD_LEN / 2
        cr.new_path()
        rrect(cr, cx - a / 2, cy - l, a, 2 * l, 8)
        rrect(cr, cx - l, cy - a / 2, 2 * l, a, 8)
        cr.set_source_rgb(*t['btn'])
        cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        cr.fill()
        for name, (dx, dy) in (('UP', (0, -1)), ('DOWN', (0, 1)), ('LEFT', (-1, 0)), ('RIGHT', (1, 0))):
            _, (x, y, w, h), _ = BUTTONS[name]
            if self.is_down(name):
                rrect(cr, x, y, w, h, 7)
                cr.set_source_rgb(*t['btn_dn'])
                cr.fill()
            # arrow
            px, py = cx + dx * (l - 20), cy + dy * (l - 20)
            cr.move_to(px + dx * 7, py + dy * 7)
            cr.line_to(px - dy * 8 - dx * 4, py - dx * 8 - dy * 4)
            cr.line_to(px + dy * 8 - dx * 4, py + dx * 8 - dy * 4)
            cr.close_path()
            cr.set_source_rgba(*t['label'], 0.55)
            cr.fill()
            if name in self.latched:
                self.latch_mark(cr, x + w / 2, y + h / 2)
        cr.arc(cx, cy, 10, 0, 2 * math.pi)
        cr.set_source_rgba(0, 0, 0, 0.18)
        cr.fill()
        # round buttons
        for name in ('X', 'A', 'B', 'Y', 'MENU', 'F1', 'F2'):
            _, (x, y, r), label = BUTTONS[name]
            dn = self.is_down(name)
            if not dn:
                cr.arc(x, y + 2.5, r, 0, 2 * math.pi)
                cr.set_source_rgba(0, 0, 0, 0.35)
                cr.fill()
            g = cairo.RadialGradient(x - r * 0.3, y - r * 0.4, r * 0.1, x, y, r)
            base = t['btn_dn'] if dn else t['btn']
            hi = t['btn_dn'] if dn else t['btn_hi']
            g.add_color_stop_rgb(0, *hi)
            g.add_color_stop_rgb(1, *base)
            cr.arc(x, y + (1.5 if dn else 0), r, 0, 2 * math.pi)
            cr.set_source(g)
            cr.fill()
            cr.set_source_rgb(*t['label'])
            if name in ('X', 'A', 'B', 'Y'):
                cr.set_font_size(22)
                self.text(cr, label, x, y + 8 + (1.5 if dn else 0))
            elif name == 'MENU':
                # three-dot logo-free menu mark
                for k in (-6, 0, 6):
                    cr.arc(x + k, y + (1.5 if dn else 0), 2.2, 0, 2 * math.pi)
                    cr.fill()
                cr.set_source_rgb(*t['print'])
                cr.set_font_size(10)
                self.text(cr, 'MENU', x, y + r + 15)
            else:
                cr.set_source_rgb(*t['print'])
                cr.set_font_size(10)
                self.text(cr, label, x, y + r + 14)
            if name in self.latched:
                self.latch_mark(cr, x + r * 0.7, y - r * 0.7)
        # select / start
        for name in ('SELECT', 'START'):
            _, (x, y, w, h, r), label = BUTTONS[name]
            dn = self.is_down(name)
            rrect(cr, x, y + (1.5 if dn else 0), w, h, r)
            cr.set_source_rgb(*self.btn_color(name))
            cr.fill()
            cr.set_source_rgb(*t['print'])
            cr.set_font_size(10)
            self.text(cr, label, x + w / 2, y + h + 15)
            if name in self.latched:
                self.latch_mark(cr, x + w / 2, y + h / 2)
        # side labels
        cr.set_font_size(9)
        cr.set_source_rgb(*t['print'])
        for name in ('POWER', 'VOL+', 'VOL-'):
            _, (x, y, w, h, r), label = BUTTONS[name]
            cr.save()
            cr.translate(BODY[0] + BODY[2] - 12, y + h / 2)
            cr.rotate(math.pi / 2)
            if name == 'POWER':  # power symbol, drawn (fonts may lack U+23FB)
                cr.set_line_width(1.4)
                cr.new_path()
                cr.arc(0, 0, 4.5, -math.pi / 2 + 0.7, 3 * math.pi / 2 - 0.7)
                cr.stroke()
                cr.move_to(0, -6)
                cr.line_to(0, -1)
                cr.stroke()
            else:
                self.text(cr, label, 0, 4)
            cr.restore()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--vnc', default='127.0.0.1:5900', help='QEMU VNC server HOST:PORT')
    ap.add_argument('--monitor', help='QEMU monitor unix socket (closing the window quits QEMU)')
    ap.add_argument('--scale', type=float, help='window scale (default: fit the screen)')
    ap.add_argument('--color', choices=sorted(THEMES), default=os.environ.get('BRICK_SKIN_COLOR', 'black'))
    args = ap.parse_args()
    win = BrickWindow(args)
    win.show_all()
    Gtk.main()


if __name__ == '__main__':
    main()
