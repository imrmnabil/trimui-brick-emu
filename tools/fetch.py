#!/usr/bin/env python3
"""Download the stock firmware or a custom firmware from GitHub releases
(no curl / 7z needed; progress on stderr).

  fetch.py firmware [TAG] DATA_DIR    stock firmware -> DATA_DIR/firmware/{download,stock,unpacked}
  fetch.py cfw nextui|spruce DATA_DIR CFW release asset -> DATA_DIR/cfw/   (prints the path)
"""
import json, os, re, ssl, subprocess, sys, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {'User-Agent': 'trimui-brick-emu', 'Accept': 'application/vnd.github+json'}
FW_REPO, FW_TAG = 'trimui/firmware_brick', 'v1.1.1-20251126'
CFW = {'nextui': ('LoveRetro/NextUI', r'NextUI-[0-9-]*-base\.zip$'),
       'spruce': ('spruceUI/spruceOS', r'spruceV[0-9.]*\.7z$')}


def _ssl_context():
    """TLS context with the system's CA certificates, wherever the distro
    keeps them (a Python/OpenSSL built elsewhere may not know the path)."""
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs():
        for f in ('/etc/ssl/certs/ca-certificates.crt',            # Debian, Ubuntu, Arch, Gentoo
                  '/etc/pki/tls/certs/ca-bundle.crt',               # Fedora, RHEL
                  '/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem',
                  '/etc/ssl/ca-bundle.pem',                         # openSUSE
                  '/etc/ssl/cert.pem'):                             # Alpine, macOS-like
            if os.path.isfile(f):
                ctx.load_verify_locations(f)
                break
    return ctx


CTX = _ssl_context()


def urlopen(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout, context=CTX)


def release(repo, tag=None):
    url = f'https://api.github.com/repos/{repo}/releases/' + (f'tags/{tag}' if tag else 'latest')
    with urlopen(urllib.request.Request(url, headers=UA), 30) as r:
        return json.load(r)


def download(url, dest):
    if os.path.exists(dest):
        return dest
    part = dest + '.part'
    with urlopen(urllib.request.Request(url, headers={'User-Agent': UA['User-Agent']}), 60) as r:
        total = int(r.headers.get('Content-Length') or 0)
        done = 0
        with open(part, 'wb') as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if total:
                    print(f'\rdownloading {os.path.basename(dest)}: {done * 100 // total}% '
                          f'({done >> 20} / {total >> 20} MB)', end='', file=sys.stderr, flush=True)
    print(file=sys.stderr)
    os.replace(part, dest)
    return dest


def asset(rel, pattern):
    for a in rel.get('assets', []):
        if re.search(pattern, a['name']):
            return a
    raise SystemExit(f'no release asset matching {pattern} in {rel.get("tag_name")}')


def firmware(tag, data):
    rel = release(FW_REPO, tag)
    a = next((a for a in rel['assets'] if 'firmware' in a['name'] and a['name'].endswith('.zip')
              and 'sd_recovery' not in a['name']), None)
    if not a:
        raise SystemExit(f'no firmware zip in {FW_REPO} {tag}')
    dl = os.path.join(data, 'firmware', 'download')
    os.makedirs(dl, exist_ok=True)
    z = download(a['browser_download_url'], os.path.join(dl, a['name']))
    stock = os.path.join(data, 'firmware', 'stock')
    os.makedirs(stock, exist_ok=True)
    print('unpacking the firmware…', file=sys.stderr)
    with zipfile.ZipFile(z) as zf:
        names = [n for n in zf.namelist() if n.endswith('.awimg')]
        if not names:
            raise SystemExit('the firmware zip contains no .awimg image')
        img = zf.extract(names[0], stock)
    subprocess.run([sys.executable, '-I', os.path.join(HERE, 'awimg_unpack.py'), img,
                    os.path.join(data, 'firmware', 'unpacked')], check=True, stdout=subprocess.DEVNULL)
    os.remove(img)   # the unpacked partitions are what is needed
    print(f'stock firmware {tag} unpacked', file=sys.stderr)


def cfw(name, data):
    repo, pat = CFW[name]
    a = asset(release(repo), pat)
    d = os.path.join(data, 'cfw')
    os.makedirs(d, exist_ok=True)
    print(download(a['browser_download_url'], os.path.join(d, a['name'])))


if __name__ == '__main__':
    args = sys.argv[1:]
    if len(args) in (2, 3) and args[0] == 'firmware':
        firmware(args[1] if len(args) == 3 else FW_TAG, args[-1])
    elif len(args) == 3 and args[0] == 'cfw' and args[1] in CFW:
        cfw(args[1], args[2])
    else:
        sys.exit(__doc__)
