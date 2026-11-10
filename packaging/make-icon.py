#!/usr/bin/env python3
"""Render the application icon (packaging/trimui-brick-emu.png, 256x256) from
the skin of tools/brick-window.py, with a plain gradient on the screen.
Needs a desktop session (GTK).   python3 packaging/make-icon.py"""
import importlib.util, os
import cairo

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('bw', os.path.join(HERE, '..', 'tools', 'brick-window.py'))
bw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bw)


class Args:
    vnc = '127.0.0.1:1'
    monitor = None
    scale = 1.0
    color = 'black'


win = bw.BrickWindow(Args)
# screen content: a calm teal gradient
scr = cairo.ImageSurface(cairo.FORMAT_RGB24, 1024, 768)
c = cairo.Context(scr)
g = cairo.RadialGradient(512, 330, 40, 512, 384, 700)
g.add_color_stop_rgb(0, 0.16, 0.55, 0.58)
g.add_color_stop_rgb(1, 0.05, 0.16, 0.25)
c.set_source(g)
c.paint()
win.img = scr
win.message = None
win.show_close = False
skin = cairo.ImageSurface(cairo.FORMAT_ARGB32, bw.SKIN_W, bw.SKIN_H)
win.on_draw(win, cairo.Context(skin))

size = 256
icon = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
c = cairo.Context(icon)
s = (size - 8) / bw.SKIN_H
c.translate((size - bw.SKIN_W * s) / 2, (size - bw.SKIN_H * s) / 2)
c.scale(s, s)
c.set_source_surface(skin, 0, 0)
c.get_source().set_filter(cairo.FILTER_BEST)
c.paint()
icon.write_to_png(os.path.join(HERE, 'trimui-brick-emu.png'))
print('wrote', os.path.join(HERE, 'trimui-brick-emu.png'))
