#!/usr/bin/env python3
"""TrimUI Brick emulator manager: start / power off / restart the emulated
Brick, manage SD card images and their files, screenshots, shell, console,
and the maintenance tasks of ./brick (new SD card, CFW download, update,
factory reset).

  brick-manager.py            (or: ./brick manager)

SD card files are written directly into the image (mtools) while the
emulator is stopped, and uploaded through the running Brick (HTTP over the
emulator's user network + the root shell channel) while it is running.
"""
import datetime, http.server, importlib.util, json, os, re, secrets, shlex, shutil, socket
import subprocess, sys, threading, time, urllib.parse

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Pango

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'out')
RUN = os.path.join(OUT, 'run')
MON_SOCK = os.path.join(RUN, 'mon.sock')
SHELL_SOCK = os.path.join(RUN, 'shell.sock')
CONSOLE_LOG = os.path.join(RUN, 'console.log')
QEMU_LOG = os.path.join(RUN, 'qemu.log')
CONFIG = os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config')), 'brick-emu', 'manager.json')
GUEST_SD = '/mnt/SDCARD'
SIZES = ['4G', '8G', '16G', '32G', '64G', '128G']


def human(n):
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or u == 'TB':
            return f'{n:.0f} {u}' if u == 'B' else f'{n:.1f} {u}'
        n /= 1024


def sq(s):
    """single-quote for the guest's ash"""
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- emulator I/O
def hmp(cmds, timeout=3):
    """Run HMP commands on the QEMU monitor; returns outputs (raises OSError if not running)."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(MON_SOCK)

    def prompt():
        buf = b''
        while not buf.rstrip().endswith(b'(qemu)'):
            try:
                d = s.recv(65536)
            except socket.timeout:
                break
            if not d:
                break
            buf += d
        return re.sub(r'\x1b\[[0-9;]*[A-Za-z]|\r', '', buf.decode(errors='replace'))
    try:
        prompt()
        out = []
        for c in cmds:
            s.sendall(c.encode() + b'\n')
            o = prompt()
            out.append('\n'.join(l for l in o.split('\n')[1:] if not l.startswith('(qemu)')))
        return out
    finally:
        s.close()


class ShellBusy(Exception):
    pass


def guest(cmd, timeout=30):
    """Run a command in the Brick's root shell (hvc1); returns (status, output)."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(2)
    try:
        s.connect(SHELL_SOCK)
    except OSError as e:
        raise ShellBusy(f'cannot reach the Brick shell ({e})')
    tag = secrets.token_hex(4)
    # start / end markers are split by quotes so the echoed input never matches
    # (eval: any command text works, e.g. one ending in '&')
    line = f'echo "__S""{tag}__"; eval {sq(cmd)}; echo "__E""{tag}__:$?"\n'
    if len(line) > 1000:
        raise ValueError('command too long for the guest shell')
    s.sendall(b'\n' + line.encode())
    buf = ''
    end = time.time() + timeout
    s.settimeout(0.5)
    m = None
    try:
        while time.time() < end:
            try:
                d = s.recv(65536)
            except socket.timeout:
                continue
            if not d:
                break
            buf += d.decode(errors='replace')
            clean = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|\r', '', buf)
            m = re.search(rf'^__S{tag}__\n(.*?)^__E{tag}__:(\d+)$', clean, re.M | re.S)
            if m:
                return int(m.group(2)), m.group(1)
    finally:
        s.close()
    raise TimeoutError(f'guest command timed out: {cmd[:60]}')


def emulator_state():
    """None if stopped, else dict(sd=path or None, vnc=port or None)."""
    if not os.path.exists(MON_SOCK):
        return None
    try:
        block, vnc = hmp(['info block', 'info vnc'], timeout=2)
    except OSError:
        return None
    st = {'sd': None, 'vnc': None}
    m = re.search(r'^sd \([^)]*\): (.+?) \(raw', block, re.M)
    if m:
        st['sd'] = os.path.realpath(m.group(1))
    m = re.search(r'Server: 127\.0\.0\.1:(\d+)', vnc)
    if m:
        st['vnc'] = int(m.group(1))
    return st


# ---------------------------------------------------------------- SD backends
class Entry:
    def __init__(self, name, is_dir, size, key):
        self.name, self.is_dir, self.size, self.key = name, is_dir, size, key


class ImageSD:
    """SD image accessed with mtools (emulator stopped). Paths use the 8.3
    short names, which never contain mtools wildcard characters."""
    online = False
    MDIR = re.compile(r'^(?P<short>.{8}) (?P<ext>.{3})\s+(?P<size><DIR>|\d+)\s+'
                      r'(?P<date>\d{4}-\d\d-\d\d)\s+(?P<time>\d{1,2}:\d\d)\s*(?P<long>.*)$')

    def __init__(self, img):
        self.img = img
        self.env = dict(os.environ, MTOOLS_SKIP_CHECK='1', LC_ALL='C.UTF-8')

    @property
    def dev(self):
        return f'{self.img}@@1M'

    def root(self):
        return '::/'

    def child(self, parent, entry):
        return parent.rstrip('/') + '/' + entry.key

    def mt(self, *args, check=True):
        r = subprocess.run(args, env=self.env, capture_output=True, text=True)
        if check and r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip() or f'{args[0]} failed')
        return r.stdout

    def list(self, d):
        out = self.mt('mdir', '-a', '-i', self.dev, d)
        res = []
        for line in out.split('\n'):
            m = self.MDIR.match(line)
            if not m:
                continue
            short = m.group('short').rstrip()
            ext = m.group('ext').rstrip()
            if short in ('.', '..'):
                continue
            key = short + ('.' + ext if ext else '')
            name = (m.group('long') or '').strip() or key
            is_dir = m.group('size') == '<DIR>'
            res.append(Entry(name, is_dir, 0 if is_dir else int(m.group('size')), key))
        return res

    def free(self):
        out = self.mt('minfo', '-i', self.dev, '::', check=False)
        cl = re.search(r'cluster size: (\d+) sectors', out)
        fr = re.search(r'free clusters=(\d+)', out)
        if cl and fr:
            return int(fr.group(1)) * int(cl.group(1)) * 512
        return None

    def mkdir(self, d, name):
        if any(e.name.lower() == name.lower() for e in self.list(d)):
            raise RuntimeError(f'"{name}" already exists')
        self.mt('mmd', '-i', self.dev, d.rstrip('/') + '/' + name)

    def delete(self, d, e):
        p = d.rstrip('/') + '/' + e.key
        self.mt('mdeltree' if e.is_dir else 'mdel', '-i', self.dev, p)

    def add(self, d, sources, progress):
        # mcopy -v prints one "Copying NAME" line per file and per folder
        total = sum(sum(len(f) + 1 for _, _, f in os.walk(s)) if os.path.isdir(s) else 1 for s in sources)
        done = 0
        for src in sources:
            p = subprocess.Popen(['mcopy', '-s', '-v', '-Q', '-D', 'o', '-i', self.dev, src, d.rstrip('/') + '/'],
                                 env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            last = ''
            for line in p.stdout:
                line = line.strip()
                last = line or last
                if line.startswith('Copying '):
                    done += 1
                    progress(min(done / max(total, 1), 1.0), line[8:])
            if p.wait():
                raise RuntimeError(f'copying {os.path.basename(src)} failed: {last}')

    def export(self, d, entries, dest, progress):
        for i, e in enumerate(entries):
            progress(i / len(entries), e.name)
            target = os.path.join(dest, e.name)
            if os.path.exists(target):
                raise RuntimeError(f'{target} already exists')
            self.mt('mcopy', '-s', '-n', '-Q', '-i', self.dev, d.rstrip('/') + '/' + e.key, dest + '/')
            # the selected entry itself comes out under its 8.3 name: restore the long one
            short = os.path.join(dest, e.key)
            if e.key != e.name and os.path.exists(short):
                os.rename(short, target)


class FileServer:
    """Serves chosen host files to the guest (http://10.0.2.2:PORT/TOKEN/N)."""

    def __init__(self):
        self.files = {}
        self.token = secrets.token_hex(8)
        files = self.files
        token = self.token

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parts = self.path.strip('/').split('/')
                path = files.get(parts[1]) if len(parts) == 2 and parts[0] == token else None
                if not path or not os.path.isfile(path):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(os.path.getsize(path)))
                self.end_headers()
                with open(path, 'rb') as f:
                    shutil.copyfileobj(f, self.wfile, 1 << 20)

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path):
        n = str(len(self.files))
        self.files[n] = path
        return f'http://10.0.2.2:{self.port}/{self.token}/{n}'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class GuestSD:
    """The SD card of the running Brick, through its root shell."""
    online = True

    def root(self):
        return GUEST_SD

    def child(self, parent, entry):
        return parent.rstrip('/') + '/' + entry.key

    def run(self, cmd, timeout=30):
        st, out = guest(cmd, timeout)
        if st:
            raise RuntimeError(out.strip() or f'command failed ({st})')
        return out

    def list(self, d):
        out = self.run(f'ls -la {sq(d)}')
        res = []
        for line in out.split('\n'):
            f = line.split(None, 8)
            if len(f) < 9 or f[0][0] not in '-d':
                continue
            name = f[8]
            if name in ('.', '..'):
                continue
            is_dir = f[0][0] == 'd'
            res.append(Entry(name, is_dir, 0 if is_dir else int(f[4]), name))
        return res

    def free(self):
        out = self.run(f'df -k {GUEST_SD} | tail -1')
        f = out.split()
        return int(f[3]) * 1024 if len(f) >= 4 and f[3].isdigit() else None

    def mkdir(self, d, name):
        self.run(f'mkdir -p {sq(d.rstrip("/") + "/" + name)}')

    def delete(self, d, e):
        self.run(f'rm -rf {sq(d.rstrip("/") + "/" + e.key)} && sync', timeout=120)

    def ensure_net(self):
        st, out = guest('ifconfig eth0 2>/dev/null | grep -q "inet addr" || '
                        'ifconfig eth0 10.0.2.15 netmask 255.255.255.0 up', 15)
        if st:
            raise RuntimeError('the emulated Brick has no network (started with --no-net?)')

    def add(self, d, sources, progress):
        jobs = []   # (host file, guest path)
        dirs = []
        for src in sources:
            base = os.path.basename(src.rstrip('/'))
            if os.path.isdir(src):
                for top, sub, files in os.walk(src):
                    rel = os.path.relpath(top, src)
                    gdir = d.rstrip('/') + '/' + base + ('' if rel == '.' else '/' + rel)
                    dirs.append(gdir)
                    jobs += [(os.path.join(top, f), gdir + '/' + f) for f in sorted(files)]
            else:
                jobs.append((src, d.rstrip('/') + '/' + base))
        total = sum(os.path.getsize(h) for h, _ in jobs) or 1
        self.ensure_net()
        srv = FileServer()
        try:
            for gd in dirs:
                self.run(f'mkdir -p {sq(gd)}')
            done = 0
            for host, gpath in jobs:
                progress(done / total, os.path.basename(host))
                size = os.path.getsize(host)
                # ~1 MB/s worst case through the emulated card, plus slack
                self.run(f'wget -q -O {sq(gpath + ".part")} {srv.url(host)} && mv -f {sq(gpath + ".part")} {sq(gpath)}',
                         timeout=60 + size / 500_000)
                done += size
            self.run('sync', timeout=300)
        finally:
            srv.close()

    def export(self, d, entries, dest, progress):
        raise RuntimeError('Exporting needs the emulator to be stopped (files are read straight from the image).')


# ---------------------------------------------------------------- the window
class Manager(Gtk.Window):
    def __init__(self):
        super().__init__(title='TrimUI Brick')
        self.set_default_size(920, 640)
        self.cfg = self.load_cfg()
        self.state = None          # emulator_state()
        self.proc = None           # emulator started by us
        self.busy = False
        self.sd_backend = None
        self.cwd = []              # [(name, key)] below the SD root
        self.log_pos = 0

        hb = Gtk.HeaderBar(show_close_button=True, title='TrimUI Brick')
        self.set_titlebar(hb)
        self.hb = hb
        self.b_start = self.hbutton('media-playback-start-symbolic', 'Start', self.on_start, 'suggested-action')
        self.b_off = self.hbutton('system-shutdown-symbolic', 'Power off', self.on_poweroff)
        self.b_restart = self.hbutton('view-refresh-symbolic', 'Restart', self.on_restart)
        for b in (self.b_start, self.b_off, self.b_restart):
            hb.pack_start(b)
        self.b_menu = Gtk.MenuButton(image=Gtk.Image.new_from_icon_name('open-menu-symbolic', Gtk.IconSize.BUTTON))
        menu = Gtk.Menu()
        for label, cb in (('Force stop', self.on_force_stop), ('Sleep / wake (power button)', self.on_sleep),
                          ('Open emulator folder', lambda *a: self.open_uri(ROOT))):
            it = Gtk.MenuItem(label=label)
            it.connect('activate', cb)
            menu.append(it)
        menu.show_all()
        self.b_menu.set_popup(menu)
        hb.pack_end(self.b_menu)
        self.b_shell = self.hbutton('utilities-terminal-symbolic', 'Shell', self.on_shell)
        self.b_shot = self.hbutton('camera-photo-symbolic', 'Screenshot', self.on_screenshot)
        hb.pack_end(self.b_shell)
        hb.pack_end(self.b_shot)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(box)
        # launch options
        opts = Gtk.Box(spacing=8, margin=10)
        box.pack_start(opts, False, False, 0)
        opts.pack_start(Gtk.Label(label='SD card'), False, False, 0)
        self.sd_combo = Gtk.ComboBoxText()
        self.sd_combo.connect('changed', self.on_sd_changed)
        opts.pack_start(self.sd_combo, True, True, 0)
        b = Gtk.Button.new_from_icon_name('document-open-symbolic', Gtk.IconSize.BUTTON)
        b.set_tooltip_text('Use another SD card image…')
        b.connect('clicked', self.on_pick_sd)
        opts.pack_start(b, False, False, 0)
        b = Gtk.Button(label='New SD card…')
        b.connect('clicked', self.on_new_sd)
        opts.pack_start(b, False, False, 0)
        opts.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)
        opts.pack_start(Gtk.Label(label='Graphics'), False, False, 0)
        self.gpu_combo = Gtk.ComboBoxText()
        self.gpu_combo.append('virgl', 'GPU (virgl)')
        self.gpu_combo.append('soft', 'Software')
        self.gpu_combo.set_active_id(self.cfg.get('gpu', 'virgl'))
        opts.pack_start(self.gpu_combo, False, False, 0)
        opts.pack_start(Gtk.Label(label='Colour'), False, False, 0)
        self.color_combo = Gtk.ComboBoxText()
        for c in ('black', 'grey', 'white'):
            self.color_combo.append(c, c.capitalize())
        self.color_combo.set_active_id(self.cfg.get('color', 'black'))
        opts.pack_start(self.color_combo, False, False, 0)

        self.nb = Gtk.Notebook()
        box.pack_start(self.nb, True, True, 0)
        self.nb.append_page(self.build_sd_page(), Gtk.Label(label='SD card'))
        self.nb.append_page(self.build_console_page(), Gtk.Label(label='Console'))
        self.nb.append_page(self.build_system_page(), Gtk.Label(label='System'))

        # status bar: transfer / task progress
        sb = Gtk.Box(spacing=8, margin=6)
        box.pack_start(sb, False, False, 0)
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        sb.pack_start(self.status, True, True, 0)
        self.progress = Gtk.ProgressBar(show_text=True, no_show_all=True)
        self.progress.set_size_request(260, -1)
        sb.pack_end(self.progress, False, False, 0)

        self.fill_sd_combo(self.cfg.get('sd'))
        self.connect('destroy', self.on_destroy)
        self.poll()
        GLib.timeout_add(2000, self.poll)
        GLib.timeout_add(700, self.tail_console)

    # --- helpers
    def hbutton(self, icon, tip, cb, style=None):
        b = Gtk.Button()
        bx = Gtk.Box(spacing=6)
        bx.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON), False, False, 0)
        bx.pack_start(Gtk.Label(label=tip), False, False, 0)
        b.add(bx)
        b.set_tooltip_text(tip)
        if style:
            b.get_style_context().add_class(style)
        b.connect('clicked', cb)
        return b

    def load_cfg(self):
        try:
            with open(CONFIG) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_cfg(self):
        self.cfg.update(gpu=self.gpu_combo.get_active_id(), color=self.color_combo.get_active_id(),
                        sd=self.sd_combo.get_active_id())
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, 'w') as f:
            json.dump(self.cfg, f, indent=1)

    def on_destroy(self, *a):
        self.save_cfg()
        Gtk.main_quit()

    def msg(self, text, kind=Gtk.MessageType.INFO, secondary=None):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind,
                              buttons=Gtk.ButtonsType.OK, text=text)
        if secondary:
            d.format_secondary_text(secondary)
        d.run()
        d.destroy()

    def confirm(self, text, secondary, ok='OK', destructive=False):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.NONE, text=text)
        d.format_secondary_text(secondary)
        d.add_button('Cancel', Gtk.ResponseType.CANCEL)
        b = d.add_button(ok, Gtk.ResponseType.OK)
        b.get_style_context().add_class('destructive-action' if destructive else 'suggested-action')
        r = d.run()
        d.destroy()
        return r == Gtk.ResponseType.OK

    def set_status(self, text):
        self.status.set_text(text)

    def work(self, title, fn, done=None, progress=False):
        """Run fn(report) in a thread; report(fraction, text). done(result, error) in the UI thread."""
        if self.busy:
            self.msg('Please wait', secondary='Another task is still running.')
            return
        self.busy = True
        self.update_buttons()
        self.set_status(title + '…')
        if progress:
            self.progress.set_fraction(0)
            self.progress.set_text('')
            self.progress.show()

        def report(frac, text=''):
            def ui():
                self.progress.set_fraction(frac)
                self.progress.set_text(f'{frac * 100:.0f}%')
                if text:
                    self.set_status(f'{title}: {text}')
                return False
            GLib.idle_add(ui)

        def run():
            res, err = None, None
            try:
                res = fn(report)
            except Exception as e:  # shown to the user
                err = e
            GLib.idle_add(finish, res, err)

        def finish(res, err):
            self.busy = False
            self.progress.hide()
            self.set_status(f'{title}: failed' if err else f'{title}: done')
            self.update_buttons()
            if err:
                self.msg(f'{title} failed', Gtk.MessageType.ERROR, str(err))
            if done:
                done(res, err)
            return False
        threading.Thread(target=run, daemon=True).start()

    def open_uri(self, path):
        Gtk.show_uri_on_window(self, 'file://' + urllib.parse.quote(path), Gdk.CURRENT_TIME)

    # --- emulator state
    def poll(self):
        old = self.state
        self.state = emulator_state()
        if (old is None) != (self.state is None) or (old and self.state and old.get('sd') != self.state.get('sd')):
            self.update_buttons()
            self.choose_backend()
        if self.proc and self.proc.poll() is not None:
            self.proc = None
        return True

    def update_buttons(self):
        run = self.state is not None
        self.b_start.set_sensitive(not run and not self.busy)
        for b in (self.b_off, self.b_restart, self.b_shot, self.b_shell):
            b.set_sensitive(run)
        for w in (self.sd_combo, self.gpu_combo, self.color_combo):
            w.set_sensitive(not run)
        if run:
            sd = self.state.get('sd')
            self.hb.set_subtitle('Running' + (f' — {os.path.basename(sd)}' if sd else ''))
        else:
            self.hb.set_subtitle('Stopped')
        for w in self.sd_buttons:
            w.set_sensitive(not self.busy and self.sd_backend is not None)
        for w in self.sys_buttons:
            w.set_sensitive(not self.busy)

    # --- start / stop
    def on_start(self, *a):
        sd = self.sd_combo.get_active_id()
        if not os.path.exists(os.path.join(OUT, 'Image')):
            self.msg('The emulator is not built yet', secondary='Run ./brick setup in a terminal first.')
            return
        self.save_cfg()
        args = [os.path.join(ROOT, 'scripts', 'run.sh')]
        if sd:
            args.append(sd)
        args += ['--gpu', self.gpu_combo.get_active_id(), '--color', self.color_combo.get_active_id(),
                 '--log', CONSOLE_LOG]
        os.makedirs(RUN, exist_ok=True)
        open(CONSOLE_LOG, 'w').close()
        self.log_pos = 0
        self.console_buf.set_text('')
        with open(QEMU_LOG, 'w') as log:
            self.proc = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         start_new_session=True)
        self.set_status('Starting the Brick…')
        self.b_start.set_sensitive(False)

        def check():
            if self.proc and self.proc.poll() is not None:
                with open(QEMU_LOG) as f:
                    tail = f.read()[-1500:]
                self.msg('The emulator failed to start', Gtk.MessageType.ERROR, tail)
                self.proc = None
                self.update_buttons()
            return False
        GLib.timeout_add(3000, check)

    def wait_stopped(self, secs):
        end = time.time() + secs
        while time.time() < end:
            if emulator_state() is None:
                return True
            time.sleep(0.5)
        return False

    def on_poweroff(self, *a):
        def fn(report):
            try:
                guest('sync; (sleep 1; poweroff) >/dev/null 2>&1 &', 10)
            except (ShellBusy, TimeoutError, OSError):
                pass
            if not self.wait_stopped(40):
                hmp(['quit'])       # the guest did not power off: stop QEMU
                self.wait_stopped(5)
                return 'forced'
        self.work('Power off', fn, lambda r, e: self.poll())

    def on_restart(self, *a):
        def fn(report):
            try:
                st, _ = guest('sync; (sleep 1; reboot) >/dev/null 2>&1 &', 10)
            except (ShellBusy, TimeoutError, OSError):
                hmp(['system_reset'])
        self.work('Restart', fn)

    def on_force_stop(self, *a):
        if self.state is None:
            return
        if self.confirm('Force stop the emulator?', 'Like pulling the battery: unsaved game progress is lost.',
                        'Force stop', destructive=True):
            try:
                hmp(['quit'])
            except OSError:
                pass
            GLib.timeout_add(800, lambda: self.poll() and False)

    def on_sleep(self, *a):
        if self.state:
            try:
                hmp(['sendkey p 200'])
            except OSError:
                pass

    # --- screenshot / shell
    def on_screenshot(self, *a):
        port = self.state and self.state.get('vnc')
        if not port:
            self.msg('Screenshots need the default Brick window (VNC display).')
            return
        spec = importlib.util.spec_from_file_location('brick_window', os.path.join(ROOT, 'tools', 'brick-window.py'))
        bw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bw)
        import cairo

        def fn(report):
            got, closed = threading.Event(), []

            def frame():   # the first update may only acknowledge an encoding: wait for pixels
                if v.pixel_rects:
                    got.set()
            v = bw.VNC('127.0.0.1', port, frame, lambda *a: None, lambda e: (closed.append(e), got.set()))
            v.start()
            if not got.wait(10) or closed or v.fb is None:
                raise RuntimeError('no picture from the emulator')
            h, w, _ = v.fb.shape
            img = cairo.ImageSurface.create_for_data(memoryview(v.fb.copy()).cast('B'), cairo.FORMAT_RGB24, w, h, w * 4)
            pics = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or os.path.expanduser('~')
            d = os.path.join(pics, 'TrimUI Brick')
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, datetime.datetime.now().strftime('brick-%Y%m%d-%H%M%S.png'))
            img.write_to_png(path)
            try:
                v.sock.close()
            except OSError:
                pass
            return path
        self.work('Screenshot', fn, lambda r, e: r and self.set_status(f'Screenshot saved: {r}'))

    def on_shell(self, *a):
        cmd = [sys.executable, '-I', os.path.join(ROOT, 'tools', 'shell.py'), SHELL_SOCK]
        for term in (['ptyxis', '--new-window', '--'], ['gnome-terminal', '--'], ['kgx', '--'],
                     ['konsole', '-e'], ['xfce4-terminal', '-x'], ['alacritty', '-e'], ['kitty'],
                     ['foot'], ['xterm', '-e']):
            if shutil.which(term[0]):
                subprocess.Popen(term + cmd, start_new_session=True)
                return
        self.msg('No terminal emulator found', secondary='Run ./brick shell in a terminal.')

    # ------------------------------------------------------------ SD card page
    def build_sd_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        tb = Gtk.Box(spacing=6)
        page.pack_start(tb, False, False, 0)
        self.b_up = Gtk.Button.new_from_icon_name('go-up-symbolic', Gtk.IconSize.BUTTON)
        self.b_up.set_tooltip_text('Parent folder')
        self.b_up.connect('clicked', self.on_up)
        tb.pack_start(self.b_up, False, False, 0)
        b_refresh = Gtk.Button.new_from_icon_name('view-refresh-symbolic', Gtk.IconSize.BUTTON)
        b_refresh.set_tooltip_text('Refresh')
        b_refresh.connect('clicked', lambda *a: self.refresh())
        tb.pack_start(b_refresh, False, False, 0)
        self.path_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.START)
        self.path_label.get_style_context().add_class('dim-label')
        tb.pack_start(self.path_label, True, True, 6)
        btns = []
        for label, icon, cb in (('Add files…', 'document-new-symbolic', self.on_add_files),
                                ('Add folder…', 'folder-new-symbolic', self.on_add_folder),
                                ('New folder', 'folder-symbolic', self.on_mkdir),
                                ('Export…', 'document-save-symbolic', self.on_export),
                                ('Delete', 'user-trash-symbolic', self.on_delete)):
            b = Gtk.Button()
            bx = Gtk.Box(spacing=4)
            bx.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON), False, False, 0)
            bx.pack_start(Gtk.Label(label=label), False, False, 0)
            b.add(bx)
            b.connect('clicked', cb)
            tb.pack_start(b, False, False, 0)
            btns.append(b)
        self.sd_buttons = btns + [self.b_up, b_refresh]

        self.sd_note = Gtk.Label(xalign=0, wrap=True)
        self.sd_note.get_style_context().add_class('dim-label')
        page.pack_start(self.sd_note, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, str, int, object)   # icon, name, size, kind, sortkey, Entry
        self.view = Gtk.TreeView(model=self.store)
        self.view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        col = Gtk.TreeViewColumn('Name')
        ri = Gtk.CellRendererPixbuf()
        rt = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        col.pack_start(ri, False)
        col.pack_start(rt, True)
        col.add_attribute(ri, 'icon-name', 0)
        col.add_attribute(rt, 'text', 1)
        col.set_expand(True)
        col.set_sort_column_id(4)
        self.view.append_column(col)
        rs = Gtk.CellRendererText(xalign=1)
        c2 = Gtk.TreeViewColumn('Size', rs, text=2)
        self.view.append_column(c2)
        self.view.append_column(Gtk.TreeViewColumn('Type', Gtk.CellRendererText(), text=3))
        self.view.connect('row-activated', self.on_row_activated)
        # drag & drop from the file manager
        self.view.drag_dest_set(Gtk.DestDefaults.ALL, [Gtk.TargetEntry.new('text/uri-list', 0, 0)], Gdk.DragAction.COPY)
        self.view.connect('drag-data-received', self.on_drop)
        sw = Gtk.ScrolledWindow()
        sw.add(self.view)
        page.pack_start(sw, True, True, 0)
        self.free_label = Gtk.Label(xalign=0)
        page.pack_start(self.free_label, False, False, 0)
        return page

    def fill_sd_combo(self, select=None):
        imgs = []
        for f in sorted(os.listdir(OUT)) if os.path.isdir(OUT) else []:
            p = os.path.join(OUT, f)
            if f.endswith('.img') and not f.startswith(('emmc', 'brick-emu')) and os.path.isfile(p):
                imgs.append(p)
        for p in self.cfg.get('extra_sd', []):
            if os.path.isfile(p) and p not in imgs:
                imgs.append(p)
        self.sd_combo.remove_all()
        for p in imgs:
            label = os.path.relpath(p, ROOT) if p.startswith(ROOT + os.sep) else p
            self.sd_combo.append(p, f'{label}  ({human(os.path.getsize(p))})')
        if not imgs:
            self.sd_combo.append('', '(no SD card image — create one with "New SD card…")')
        if select and select in imgs:
            self.sd_combo.set_active_id(select)
        else:
            self.sd_combo.set_active(0)

    def on_pick_sd(self, *a):
        d = Gtk.FileChooserDialog(title='SD card image', transient_for=self, action=Gtk.FileChooserAction.OPEN)
        d.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Use', Gtk.ResponseType.OK)
        flt = Gtk.FileFilter()
        flt.set_name('Disk images')
        flt.add_pattern('*.img')
        d.add_filter(flt)
        if d.run() == Gtk.ResponseType.OK:
            p = d.get_filename()
            ex = self.cfg.setdefault('extra_sd', [])
            if p not in ex:
                ex.append(p)
            self.fill_sd_combo(p)
        d.destroy()

    def on_sd_changed(self, *a):
        self.choose_backend()

    def choose_backend(self):
        if not hasattr(self, 'store'):
            return
        sel = self.sd_combo.get_active_id()
        running_sd = self.state and self.state.get('sd')
        if self.state is not None and (not sel or not running_sd or os.path.realpath(sel) == running_sd):
            self.sd_backend = GuestSD()
            self.sd_note.set_text('The Brick is running: you see its SD card live. Files are uploaded through the '
                                  'emulator (about 1–3 MB/s); power off first for fast copying of big batches.')
        elif sel:
            self.sd_backend = ImageSD(sel)
            self.sd_note.set_text('The emulator is stopped: files are written straight into the SD card image.')
        else:
            self.sd_backend = None
            self.sd_note.set_text('')
        self.cwd = []
        self.update_buttons()
        self.refresh()

    def cur_dir(self):
        d = self.sd_backend.root()
        for _, key in self.cwd:
            d = d.rstrip('/') + '/' + key
        return d

    def refresh(self, keep_status=False):
        self.store.clear()
        if not self.sd_backend:
            self.path_label.set_text('')
            self.free_label.set_text('')
            return
        be, d = self.sd_backend, self.cur_dir()
        self.path_label.set_text('SD card / ' + ' / '.join(n for n, _ in self.cwd))
        self.b_up.set_sensitive(bool(self.cwd))

        def run():
            try:
                entries = be.list(d)
                free = be.free()
                err = None
            except Exception as e:
                entries, free, err = [], None, e
            GLib.idle_add(show, entries, free, err)

        def show(entries, free, err):
            if be is not self.sd_backend:
                return False
            self.store.clear()
            if err:
                self.free_label.set_text(f'Cannot read the SD card: {err}')
                return False
            for e in sorted(entries, key=lambda e: (not e.is_dir, e.name.lower())):
                self.store.append(['folder' if e.is_dir else 'text-x-generic', e.name,
                                   '' if e.is_dir else human(e.size), 'Folder' if e.is_dir else self.kind(e.name),
                                   0, e])
            n = len(entries)
            self.free_label.set_text(f'{n} item{"s" if n != 1 else ""}' + (f'   •   {human(free)} free' if free is not None else ''))
            return False
        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def kind(name):
        ext = os.path.splitext(name)[1].lower().lstrip('.')
        return ext.upper() + ' file' if ext else 'File'

    def selected(self):
        model, paths = self.view.get_selection().get_selected_rows()
        return [model[p][5] for p in paths]

    def on_row_activated(self, view, path, col):
        e = self.store[path][5]
        if e.is_dir:
            self.cwd.append((e.name, e.key))
            self.refresh()

    def on_up(self, *a):
        if self.cwd:
            self.cwd.pop()
            self.refresh()

    def choose(self, title, folder):
        d = Gtk.FileChooserDialog(title=title, transient_for=self,
                                  action=Gtk.FileChooserAction.SELECT_FOLDER if folder else Gtk.FileChooserAction.OPEN)
        d.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Add', Gtk.ResponseType.OK)
        d.set_select_multiple(True)
        res = d.get_filenames() if d.run() == Gtk.ResponseType.OK else []
        d.destroy()
        return res

    def on_add_files(self, *a):
        self.add_sources(self.choose('Add files to the SD card', False))

    def on_add_folder(self, *a):
        self.add_sources(self.choose('Add folders to the SD card', True))

    def on_drop(self, w, ctx, x, y, data, info, t):
        paths = [GLib.filename_from_uri(u)[0] for u in data.get_uris() if u.startswith('file://')]
        self.add_sources(paths)

    def add_sources(self, sources):
        if not sources or not self.sd_backend:
            return
        be, d = self.sd_backend, self.cur_dir()
        where = 'SD card / ' + ' / '.join(n for n, _ in self.cwd)
        self.work(f'Copying to {where}', lambda report: be.add(d, sources, report),
                  lambda r, e: self.refresh(), progress=True)

    def on_mkdir(self, *a):
        if not self.sd_backend:
            return
        d = Gtk.Dialog(title='New folder', transient_for=self, modal=True)
        d.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Create', Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.OK)
        entry = Gtk.Entry(activates_default=True, margin=12)
        d.get_content_area().add(entry)
        d.show_all()
        name = entry.get_text().strip() if d.run() == Gtk.ResponseType.OK else ''
        d.destroy()
        if not name:
            return
        if re.search(r'[\\/:*?"<>|\[\]]', name):
            self.msg('Invalid folder name', secondary='These characters are not allowed: \\ / : * ? " < > | [ ]')
            return
        be, cur = self.sd_backend, self.cur_dir()
        self.work('New folder', lambda r: be.mkdir(cur, name), lambda r, e: self.refresh())

    def on_delete(self, *a):
        sel = self.selected()
        if not sel or not self.sd_backend:
            return
        names = ', '.join(e.name for e in sel[:5]) + (' …' if len(sel) > 5 else '')
        if not self.confirm(f'Delete {len(sel)} item{"s" if len(sel) > 1 else ""} from the SD card?', names,
                            'Delete', destructive=True):
            return
        be, cur = self.sd_backend, self.cur_dir()

        def fn(report):
            for i, e in enumerate(sel):
                report(i / len(sel), e.name)
                be.delete(cur, e)
        self.work('Deleting', fn, lambda r, e: self.refresh(), progress=True)

    def on_export(self, *a):
        sel = self.selected()
        if not sel or not self.sd_backend:
            self.msg('Select files or folders to export first.')
            return
        d = Gtk.FileChooserDialog(title='Export to folder', transient_for=self,
                                  action=Gtk.FileChooserAction.SELECT_FOLDER)
        d.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Export', Gtk.ResponseType.OK)
        dest = d.get_filename() if d.run() == Gtk.ResponseType.OK else None
        d.destroy()
        if dest:
            be, cur = self.sd_backend, self.cur_dir()
            self.work('Exporting', lambda report: be.export(cur, sel, dest, report), progress=True)

    # ------------------------------------------------------------ console page
    def build_console_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.console_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.CHAR)
        self.console_buf = self.console_view.get_buffer()
        sw = Gtk.ScrolledWindow()
        sw.add(self.console_view)
        page.pack_start(sw, True, True, 0)
        row = Gtk.Box(spacing=6)
        page.pack_start(row, False, False, 0)
        row.pack_start(Gtk.Label(label='Run in the Brick:'), False, False, 0)
        self.cmd_entry = Gtk.Entry(placeholder_text='e.g. df -h  /  ls /mnt/SDCARD/Roms  /  dmesg | tail')
        self.cmd_entry.connect('activate', self.on_cmd)
        row.pack_start(self.cmd_entry, True, True, 0)
        b = Gtk.Button(label='Run')
        b.connect('clicked', self.on_cmd)
        row.pack_start(b, False, False, 0)
        return page

    def console_append(self, text):
        end = self.console_buf.get_end_iter()
        self.console_buf.insert(end, text)
        if self.console_buf.get_char_count() > 400_000:
            s = self.console_buf.get_start_iter()
            cut = self.console_buf.get_iter_at_offset(100_000)
            self.console_buf.delete(s, cut)
        self.console_view.scroll_to_iter(self.console_buf.get_end_iter(), 0, False, 0, 0)

    def tail_console(self):
        try:
            with open(CONSOLE_LOG, 'rb') as f:
                f.seek(self.log_pos)
                d = f.read(1 << 20)
                self.log_pos += len(d)
        except OSError:
            return True
        if d:
            text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|\r', '', d.decode(errors='replace'))
            self.console_append(text)
        return True

    def on_cmd(self, *a):
        cmd = self.cmd_entry.get_text().strip()
        if not cmd:
            return
        if self.state is None:
            self.msg('The Brick is not running.')
            return
        self.cmd_entry.set_text('')
        self.console_append(f'\n# {cmd}\n')

        def run():
            try:
                st, out = guest(cmd, 60)
                text = out + ('' if st == 0 else f'[exit status {st}]\n')
            except Exception as e:
                text = f'[{e}]\n'
            GLib.idle_add(lambda: self.console_append(text) and False)
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------ system page
    def build_system_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=16)
        self.sys_buttons = []

        def section(title, text, buttons):
            box = Gtk.Box(spacing=12)
            lb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            t = Gtk.Label(xalign=0)
            t.set_markup(f'<b>{GLib.markup_escape_text(title)}</b>')
            lb.pack_start(t, False, False, 0)
            d = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=70)
            d.get_style_context().add_class('dim-label')
            lb.pack_start(d, False, False, 0)
            box.pack_start(lb, True, True, 0)
            for label, cb, style in buttons:
                b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
                if style:
                    b.get_style_context().add_class(style)
                b.connect('clicked', cb)
                box.pack_start(b, False, False, 0)
                self.sys_buttons.append(b)
            page.pack_start(box, False, False, 0)
            page.pack_start(Gtk.Separator(), False, False, 0)

        section('Custom firmware', 'Download the latest release into cfw/, to put on a new SD card.',
                [('Get NextUI', lambda *a: self.task('Downloading NextUI', ['./brick', 'fetch', 'nextui']), None),
                 ('Get spruceOS', lambda *a: self.task('Downloading spruceOS', ['./brick', 'fetch', 'spruce']), None)])
        section('New SD card', 'Create an empty SD card image or one with a CFW installed on it.',
                [('New SD card…', self.on_new_sd, 'suggested-action')])
        section('Update emulator', 'Rebuild the kernel and emulator add-ons and refresh the system partition. '
                'Settings and internal storage are kept. Takes several minutes.',
                [('Update', lambda *a: self.stopped_task('Updating the emulator', ['./brick', 'update']), None)])
        section('Factory reset', 'Wipe the internal storage (eMMC) back to the stock firmware state. '
                'SD cards are not touched.',
                [('Reset…', self.on_reset, 'destructive-action')])
        section('Desktop integration', 'Add "TrimUI Brick" to the applications menu.',
                [('Add to app menu', self.on_desktop_entry, None)])
        return page

    def task(self, title, cmd, done=None):
        """Run a ./brick command, streaming its output to the console tab."""
        self.nb.set_current_page(1)
        self.console_append(f'\n$ {" ".join(cmd)}\n')

        def fn(report):
            p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, text=True, bufsize=1)
            for line in p.stdout:
                GLib.idle_add(lambda l=line: self.console_append(l) and False)
            if p.wait():
                raise RuntimeError(f'{cmd[0]} {" ".join(cmd[1:])} exited with status {p.returncode} (see Console)')
        self.work(title, fn, done)

    def stopped_task(self, title, cmd):
        if self.state is not None:
            self.msg('Power off the Brick first.')
            return
        self.task(title, cmd)

    def on_reset(self, *a):
        if self.state is not None:
            self.msg('Power off the Brick first.')
            return
        if self.confirm('Factory reset the internal storage?',
                        'All settings and data stored on the emulated eMMC are erased. SD cards are kept.',
                        'Reset', destructive=True):
            self.task('Factory reset', ['./brick', 'reset'])

    def on_desktop_entry(self, *a):
        apps = os.path.join(os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share')), 'applications')
        os.makedirs(apps, exist_ok=True)
        path = os.path.join(apps, 'trimui-brick-emu.desktop')
        with open(path, 'w') as f:
            f.write('[Desktop Entry]\nType=Application\nName=TrimUI Brick\nComment=TrimUI Brick emulator\n'
                    f'Exec={shlex.quote(os.path.join(ROOT, "brick"))} manager\nIcon=input-gaming\n'
                    'Terminal=false\nCategories=Game;Emulator;\n')
        self.set_status(f'Added to the applications menu ({path})')

    def on_new_sd(self, *a):
        d = Gtk.Dialog(title='New SD card', transient_for=self, modal=True)
        d.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Create', Gtk.ResponseType.OK)
        g = Gtk.Grid(row_spacing=10, column_spacing=10, margin=14)
        d.get_content_area().add(g)
        g.attach(Gtk.Label(label='File name', xalign=1), 0, 0, 1, 1)
        name = Gtk.Entry(text='sdcard.img' if not os.path.exists(os.path.join(OUT, 'sdcard.img')) else 'sd-new.img',
                         hexpand=True)
        g.attach(name, 1, 0, 1, 1)
        g.attach(Gtk.Label(label='Size', xalign=1), 0, 1, 1, 1)
        size = Gtk.ComboBoxText()
        for s in SIZES:
            size.append(s, s.replace('G', ' GB'))
        size.set_active_id('16G')
        g.attach(size, 1, 1, 1, 1)
        g.attach(Gtk.Label(label='Install', xalign=1, valign=Gtk.Align.START), 0, 2, 1, 1)
        cfw_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        g.attach(cfw_box, 1, 2, 1, 1)
        checks = []
        cfw_dir = os.path.join(ROOT, 'cfw')
        found = sorted(f for f in os.listdir(cfw_dir) if f.endswith(('.zip', '.7z'))) if os.path.isdir(cfw_dir) else []
        for i, f in enumerate(found):
            c = Gtk.CheckButton(label=f, active=(i == len(found) - 1))
            checks.append((c, os.path.join(cfw_dir, f)))
            cfw_box.pack_start(c, False, False, 0)
        if not found:
            cfw_box.pack_start(Gtk.Label(label='no CFW downloaded yet (System → Get NextUI) — the card will be empty',
                                         xalign=0, wrap=True), False, False, 0)
        extra = []
        xl = Gtk.Label(xalign=0, wrap=True)
        xl.get_style_context().add_class('dim-label')

        def add_extra(*a):
            for p in self.choose('Also copy these folders to the card (e.g. Roms, Bios)', True):
                extra.append(p)
            xl.set_text('also: ' + ', '.join(os.path.basename(p) for p in extra) if extra else '')
        xb = Gtk.Button(label='Also copy folders…', halign=Gtk.Align.START)
        xb.connect('clicked', add_extra)
        g.attach(xb, 1, 3, 1, 1)
        g.attach(xl, 1, 4, 1, 1)
        d.show_all()
        ok = d.run() == Gtk.ResponseType.OK
        fname, sz = name.get_text().strip(), size.get_active_id()
        srcs = [p for c, p in checks if c.get_active()] + extra
        d.destroy()
        if not ok or not fname:
            return
        if not fname.endswith('.img'):
            fname += '.img'
        out = fname if os.path.isabs(fname) else os.path.join(OUT, fname)
        if self.state and self.state.get('sd') == os.path.realpath(out):
            self.msg('That SD card is in use by the running Brick.')
            return
        if os.path.exists(out) and not self.confirm(f'Replace {os.path.basename(out)}?',
                                                    'The existing SD card image and everything on it is deleted.',
                                                    'Replace', destructive=True):
            return
        self.task('Creating SD card', [os.path.join(ROOT, 'scripts', 'make-sdcard.sh'), out, sz] + srcs,
                  lambda r, e: e or self.fill_sd_combo(out))


def main():
    win = Manager()
    win.show_all()
    win.update_buttons()
    Gtk.main()


if __name__ == '__main__':
    main()
