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
# Device units: 1 unit ~ 0.15 mm, proportions taken from the device's
# front/side/top views. The window is SKIN_W x SKIN_H * scale.
SKIN_W, SKIN_H = 578, 840
BODY = (30, 32, 518, 787)               # x, y, w, h
BODY_R_TOP, BODY_R_BOT = 12, 30         # small top corners, rounder bottom
GLASS = (35, 37, 508, 400)              # black glass over the whole upper half
SCREEN = (46, 54, 486, 364)             # 4:3 active area (1024x768 panel)
DPAD_C, DPAD_ARM, DPAD_LEN = (146, 547), 48, 142
ABXY_C, ABXY_OFF, ABXY_R = (435, 547), 55, 26

THEMES = {
    # body, glass, buttons, embossed letters, printed text, pill (F1/F2), power button
    'black': dict(body0=(0.135, 0.135, 0.14), body1=(0.095, 0.095, 0.10), rim=(0.22, 0.22, 0.23),
                  glass=(0.03, 0.03, 0.035), btn=(0.11, 0.11, 0.115), btn_hi=(0.19, 0.19, 0.20),
                  btn_dn=(0.26, 0.26, 0.28), emboss=(0.30, 0.30, 0.32), print=(0.36, 0.36, 0.38),
                  pill=(0.62, 0.62, 0.63), power=(0.50, 0.80, 0.70), accent=(0.95, 0.35, 0.45)),
    'grey': dict(body0=(0.50, 0.51, 0.52), body1=(0.42, 0.43, 0.44), rim=(0.60, 0.61, 0.62),
                 glass=(0.03, 0.03, 0.035), btn=(0.30, 0.30, 0.32), btn_hi=(0.40, 0.40, 0.42),
                 btn_dn=(0.18, 0.18, 0.19), emboss=(0.50, 0.50, 0.52), print=(0.26, 0.26, 0.28),
                 pill=(0.82, 0.82, 0.83), power=(0.50, 0.80, 0.70), accent=(0.95, 0.35, 0.45)),
    'white': dict(body0=(0.93, 0.93, 0.92), body1=(0.85, 0.85, 0.84), rim=(0.99, 0.99, 0.99),
                  glass=(0.03, 0.03, 0.035), btn=(0.86, 0.86, 0.85), btn_hi=(0.97, 0.97, 0.96),
                  btn_dn=(0.70, 0.70, 0.69), emboss=(0.62, 0.62, 0.63), print=(0.60, 0.60, 0.61),
                  pill=(0.55, 0.55, 0.57), power=(0.50, 0.80, 0.70), accent=(0.95, 0.35, 0.45)),
}


def build_buttons():
    """name -> (kind, geometry, label).
    kinds: circle (cx, cy, r) | pill/shoulder/side (x, y, w, h, r) | dpad (x, y, w, h)."""
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
    # the two small pills between d-pad and face buttons
    b['F1'] = ('pill', (227, 478, 48, 15, 7.5), 'F1')
    b['F2'] = ('pill', (303, 478, 48, 15, 7.5), 'F2')
    # bottom row: menu, select, start
    b['MENU'] = ('circle', (218, 757, 17), 'MENU')
    b['SELECT'] = ('circle', (290, 757, 17), 'SELECT')
    b['START'] = ('circle', (362, 757, 17), 'START')
    # top edge, left to right: L1 L2 [USB-C] R2 R1 (tabs behind the body)
    b['L'] = ('shoulder', (43, 18, 118, 30, 8), 'L1')
    b['L2'] = ('shoulder', (164, 18, 72, 30, 8), 'L2')
    b['R2'] = ('shoulder', (342, 18, 72, 30, 8), 'R2')
    b['R'] = ('shoulder', (417, 18, 118, 30, 8), 'R1')
    # left edge: volume; right edge: power (the slider above it is decorative)
    b['VOL+'] = ('side', (20, 82, 14, 50, 6), '+')
    b['VOL-'] = ('side', (20, 157, 14, 55, 6), '−')
    b['POWER'] = ('side', (544, 282, 14, 50, 6), 'POWER')
    return b


BUTTONS = build_buttons()
SLIDER = (544, 162, 12, 70, 6)          # right edge, decorative
USB = (262, 22, 54, 26)                 # top edge, between L2 and R2
CLOSE_BTN = (528, 52, 11)               # top-right of the glass (shown on hover)


def rrect4(cr, x, y, w, h, rt, rb):
    """Rounded rectangle with separate top / bottom corner radii."""
    cr.new_sub_path()
    cr.arc(x + w - rt, y + rt, rt, -math.pi / 2, 0)
    cr.arc(x + w - rb, y + h - rb, rb, 0, math.pi / 2)
    cr.arc(x + rb, y + h - rb, rb, math.pi / 2, math.pi)
    cr.arc(x + rt, y + rt, rt, math.pi, 3 * math.pi / 2)
    cr.close_path()


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
        self.pixel_rects = 0      # rectangles with pixel data received so far

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
        self.fb = bytearray(w * h * 4)
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
                            data = self.rx(rw * rh * 4)
                            row, stride = rw * 4, self.w * 4
                            if x == 0 and rw == self.w:      # full-width: one copy
                                self.fb[y * stride:(y + rh) * stride] = data
                            else:
                                fb, o = self.fb, y * stride + x * 4
                                for r in range(rh):
                                    fb[o:o + row] = data[r * row:(r + 1) * row]
                                    o += stride
                            self.pixel_rects += 1
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
            self.img = cairo.ImageSurface.create_for_data(fb, cairo.FORMAT_RGB24, w, h, stride)
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
        self.draw_edge_buttons(cr)
        if self.skin_cache is None:
            self.skin_cache = self.render_body()
        cr.save()
        cr.scale(1 / self.scale, 1 / self.scale)
        cr.set_source_surface(self.skin_cache, 0, 0)
        cr.paint()
        cr.restore()
        self.draw_screen(cr)
        self.draw_controls(cr)
        # close button: always faintly visible, red on hover
        if not getattr(self, 'show_close', True):
            return True
        x, y, r = CLOSE_BTN
        cr.new_path()
        cr.arc(x, y, r, 0, 2 * math.pi)
        if self.hover_close:
            cr.set_source_rgba(0.85, 0.25, 0.25, 0.95)
        else:
            cr.set_source_rgba(1, 1, 1, 0.10)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 1 if self.hover_close else 0.45)
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
        x, y, w, h = BODY
        # soft drop shadow
        if self.transparent:
            for i in range(12, 0, -1):
                rrect4(cr, x - i * 0.5, y + 5 - i * 0.2, w + i, h + i * 0.9, BODY_R_TOP + i, BODY_R_BOT + i)
                cr.set_source_rgba(0, 0, 0, 0.02)
                cr.fill()
        # matte body: gentle vertical gradient, thin lighter rim
        g = cairo.LinearGradient(0, y, 0, y + h)
        g.add_color_stop_rgb(0, *t['body0'])
        g.add_color_stop_rgb(1, *t['body1'])
        rrect4(cr, x, y, w, h, BODY_R_TOP, BODY_R_BOT)
        cr.set_source(g)
        cr.fill_preserve()
        cr.set_source_rgb(*t['rim'])
        cr.set_line_width(1.5)
        cr.stroke()
        # glass panel over the upper half, with a faint reflection
        gx, gy, gw, gh = GLASS
        rrect4(cr, gx, gy, gw, gh, BODY_R_TOP - 4, 3)
        cr.set_source_rgb(*t['glass'])
        cr.fill()
        gl = cairo.LinearGradient(gx, gy, gx + gw * 0.7, gy + gh)
        gl.add_color_stop_rgba(0, 1, 1, 1, 0.07)
        gl.add_color_stop_rgba(0.45, 1, 1, 1, 0.0)
        rrect4(cr, gx, gy, gw, gh, BODY_R_TOP - 4, 3)
        cr.set_source(gl)
        cr.fill()
        # small print under the glass
        cr.set_source_rgb(*t['print'])
        cr.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10)
        cx = 48
        for ch in 'BRICK':
            cr.move_to(cx, gy + gh + 19)
            cr.show_text(ch)
            cx += cr.text_extents(ch).x_advance + 3
        # speaker grilles (bottom corners)
        for gx0 in (96, 432):
            for j in range(3):
                for i in range(9):
                    if j == 1 and i in (0, 8):
                        continue
                    cr.new_sub_path()
                    cr.arc(gx0 + i * 6.5, 733 + j * 6, 1.5, 0, 2 * math.pi)
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.fill()
        # wells under the buttons (slight recess)
        for name, (kind, geo, _) in BUTTONS.items():
            if kind == 'circle':
                cr.new_sub_path()
                cr.arc(geo[0], geo[1], geo[2] + 3, 0, 2 * math.pi)
        cr.set_source_rgba(0, 0, 0, 0.30)
        cr.fill()
        cx, cy = DPAD_C
        a, l = DPAD_ARM + 6, DPAD_LEN / 2 + 3
        cr.new_path()
        self.cross(cr, cx, cy, a, l, 9)
        cr.set_source_rgba(0, 0, 0, 0.30)
        cr.fill()
        return surf

    @staticmethod
    def cross(cr, cx, cy, a, l, r):
        """Plus-shaped outline (one path, rounded arm ends)."""
        hw = a / 2
        pts = [(cx - hw, cy - l), (cx + hw, cy - l), (cx + hw, cy - hw), (cx + l, cy - hw),
               (cx + l, cy + hw), (cx + hw, cy + hw), (cx + hw, cy + l), (cx - hw, cy + l),
               (cx - hw, cy + hw), (cx - l, cy + hw), (cx - l, cy - hw), (cx - hw, cy - hw)]
        outer = {0, 1, 3, 4, 6, 7, 9, 10}   # corners at arm ends get rounded
        n = len(pts)
        cr.new_sub_path()
        for i in range(n):
            p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % n]
            rr = r if i in outer else 3
            # arc_to-like rounding between edges p0->p1 and p1->p2
            d0 = ((p1[0] - p0[0]), (p1[1] - p0[1]))
            d1 = ((p2[0] - p1[0]), (p2[1] - p1[1]))
            l0 = math.hypot(*d0) or 1
            l1 = math.hypot(*d1) or 1
            a_ = (p1[0] - d0[0] / l0 * rr, p1[1] - d0[1] / l0 * rr)
            b_ = (p1[0] + d1[0] / l1 * rr, p1[1] + d1[1] / l1 * rr)
            if i == 0:
                cr.move_to(*a_)
            else:
                cr.line_to(*a_)
            cr.curve_to(p1[0], p1[1], p1[0], p1[1], *b_)
        cr.close_path()

    def text(self, cr, s, x, y, center=True):
        ext = cr.text_extents(s)
        cr.move_to(x - (ext.width / 2 + ext.x_bearing if center else 0), y)
        cr.show_text(s)

    def is_down(self, name):
        return name in self.pressed or name in self.latched

    def latch_mark(self, cr, x, y):
        cr.new_path()
        cr.arc(x, y, 3.5, 0, 2 * math.pi)
        cr.set_source_rgb(*self.theme['accent'])
        cr.fill()

    def draw_edge_buttons(self, cr):
        """Shoulder tabs, side buttons, slider, USB port: drawn behind the body."""
        t = self.theme
        for name in ('L', 'L2', 'R2', 'R', 'VOL+', 'VOL-', 'POWER'):
            kind, (x, y, w, h, r), label = BUTTONS[name]
            dn = self.is_down(name)
            if kind == 'shoulder':
                rrect(cr, x, y + (5 if dn else 0), w, h + 20, r)
            elif x < BODY[0]:     # left edge
                rrect(cr, x + (4 if dn else 0), y, w + 12, h, r)
            else:                 # right edge
                rrect(cr, x - 12 - (4 if dn else 0), y, w + 12, h, r)
            col = t['power'] if name == 'POWER' else (t['btn_dn'] if dn else t['btn'])
            if name == 'POWER' and dn:
                col = tuple(c * 0.75 for c in col)
            cr.set_source_rgb(*col)
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.10)
            cr.set_line_width(1)
            cr.stroke()
            if name in self.latched:
                self.latch_mark(cr, x + w / 2, y + 7 if kind == 'shoulder' else y + h / 2)
        # decorative slider (right edge) and USB-C port (top edge)
        x, y, w, h, r = SLIDER
        rrect(cr, x - 12, y, w + 12, h, r)
        cr.set_source_rgb(*t['btn'])
        cr.fill()
        ux, uy, uw, uh = USB
        cr.rectangle(ux, uy, uw, uh + 20)
        cr.set_source_rgb(*t['body1'])
        cr.fill()
        rrect(cr, ux + 10, uy + 7, uw - 20, 9, 4.5)
        cr.set_source_rgb(0.55, 0.55, 0.57)
        cr.set_line_width(1.6)
        cr.stroke()

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

    def button_fill(self, cr, x, y, r, name):
        """Round button: soft-domed fill; returns the vertical press offset."""
        t = self.theme
        dn = self.is_down(name)
        off = 1.5 if dn else 0
        base = t['btn_dn'] if dn else t['btn']
        g = cairo.RadialGradient(x - r * 0.35, y - r * 0.45 + off, r * 0.1, x, y + off, r)
        g.add_color_stop_rgb(0, *(t['btn_dn'] if dn else t['btn_hi']))
        g.add_color_stop_rgb(1, *base)
        cr.new_path()
        cr.arc(x, y + off, r, 0, 2 * math.pi)
        cr.set_source(g)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.06)
        cr.set_line_width(1)
        cr.stroke()
        return off

    def draw_controls(self, cr):
        t = self.theme
        # d-pad: one cross; the pressed arm is lit
        cx, cy = DPAD_C
        a, l = DPAD_ARM, DPAD_LEN / 2
        g = cairo.LinearGradient(cx - l, cy - l, cx + l, cy + l)
        g.add_color_stop_rgb(0, *t['btn_hi'])
        g.add_color_stop_rgb(1, *t['btn'])
        self.cross(cr, cx, cy, a, l, 7)
        cr.set_source(g)
        cr.fill()
        for name, (dx, dy) in (('UP', (0, -1)), ('DOWN', (0, 1)), ('LEFT', (-1, 0)), ('RIGHT', (1, 0))):
            _, (x, y, w, h), _ = BUTTONS[name]
            if self.is_down(name):
                cr.new_path()
                rrect(cr, x + 1, y + 1, w - 2, h - 2, 6)
                cr.set_source_rgb(*t['btn_dn'])
                cr.fill()
            # small embossed triangle near the arm end
            px, py = cx + dx * (l - 16), cy + dy * (l - 16)
            cr.new_path()
            cr.move_to(px + dx * 5, py + dy * 5)
            cr.line_to(px - dy * 6 - dx * 3, py - dx * 6 - dy * 3)
            cr.line_to(px + dy * 6 - dx * 3, py + dx * 6 - dy * 3)
            cr.close_path()
            cr.set_source_rgb(*t['emboss'])
            cr.fill()
            if name in self.latched:
                self.latch_mark(cr, x + w / 2, y + h / 2)
        cr.new_path()
        cr.arc(cx, cy, 11, 0, 2 * math.pi)       # centre dimple
        cr.set_source_rgba(0, 0, 0, 0.25)
        cr.fill()
        # face buttons with embossed letters
        cr.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        for name in ('X', 'A', 'B', 'Y'):
            _, (x, y, r), label = BUTTONS[name]
            off = self.button_fill(cr, x, y, r, name)
            cr.set_font_size(19)
            cr.set_source_rgba(1, 1, 1, 0.05)
            self.text(cr, label, x + 0.8, y + 7.8 + off)
            cr.set_source_rgb(*t['emboss'])
            self.text(cr, label, x, y + 7 + off)
            if name in self.latched:
                self.latch_mark(cr, x + r * 0.7, y - r * 0.7)
        # bottom row: menu (three dots), select (bar), start (triangle)
        for name in ('MENU', 'SELECT', 'START'):
            _, (x, y, r), label = BUTTONS[name]
            off = self.button_fill(cr, x, y, r, name)
            cr.set_source_rgb(*t['emboss'])
            cr.set_line_width(1.6)
            yy = y + off
            if name == 'MENU':
                for ddx, ddy in ((0, -4), (-4.2, 3), (4.2, 3)):
                    cr.new_sub_path()
                    cr.arc(x + ddx, yy + ddy, 3, 0, 2 * math.pi)
                cr.stroke()
            elif name == 'SELECT':
                cr.new_path()
                rrect(cr, x - 6, yy - 2.5, 12, 5, 1.5)
                cr.stroke()
            else:
                cr.new_path()
                cr.move_to(x - 3.5, yy - 6)
                cr.line_to(x + 5.5, yy)
                cr.line_to(x - 3.5, yy + 6)
                cr.close_path()
                cr.stroke()
            if name in self.latched:
                self.latch_mark(cr, x + r * 0.7, y - r * 0.7)
        # F1 / F2 pills (light grey)
        for name in ('F1', 'F2'):
            _, (x, y, w, h, r), label = BUTTONS[name]
            dn = self.is_down(name)
            off = 1.2 if dn else 0
            col = t['pill']
            g = cairo.LinearGradient(0, y, 0, y + h)
            g.add_color_stop_rgb(0, *(c * (0.8 if dn else 1.12) for c in col))
            g.add_color_stop_rgb(1, *(c * (0.7 if dn else 0.92) for c in col))
            cr.new_path()
            rrect(cr, x, y + off, w, h, r)
            cr.set_source(g)
            cr.fill()
            if name in self.latched:
                self.latch_mark(cr, x + w / 2, y + h / 2)
        # labels for the edge buttons, printed on the body next to them
        cr.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(9)
        cr.set_source_rgba(*t['print'], 0.9)
        for name in ('L', 'L2', 'R2', 'R'):
            _, (x, y, w, h, r), label = BUTTONS[name]
            self.text(cr, label, x + w / 2, y + 10 + (5 if self.is_down(name) else 0))


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
