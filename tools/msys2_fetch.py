#!/usr/bin/env python3
"""Fetch an MSYS2 package and its dependency closure (no MSYS2 install needed).

  msys2_fetch.py REPO_DB OUTDIR PKG [PKG...]

REPO_DB is a pacman sync database such as clangarm64.db (a tar archive).
Packages are downloaded next to the database's URL and extracted into OUTDIR.
"""
import io, os, re, subprocess, sys, tarfile, urllib.request

REPO = 'https://repo.msys2.org/mingw/clangarm64/'


def parse_db(path):
    pkgs, provides = {}, {}
    raw = open(path, 'rb').read()
    if raw[:4] == b'\x28\xb5\x2f\xfd':   # zstd-compressed database
        raw = subprocess.run(['zstd', '-dc'], input=raw, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as t:
        for m in t.getmembers():
            if not m.name.endswith('/desc'):
                continue
            fields, key = {}, None
            for line in t.extractfile(m).read().decode().splitlines():
                if line.startswith('%') and line.endswith('%'):
                    key = line.strip('%')
                    fields[key] = []
                elif line and key:
                    fields[key].append(line)
            name = fields['NAME'][0]
            pkgs[name] = fields
            for p in fields.get('PROVIDES', []):
                provides[re.split('[<>=]', p)[0]] = name
    return pkgs, provides


def closure(pkgs, provides, roots):
    seen, todo = set(), list(roots)
    while todo:
        n = re.split('[<>=]', todo.pop())[0]
        n = n if n in pkgs else provides.get(n, n)
        if n in seen:
            continue
        if n not in pkgs:
            print(f'warning: unknown dependency {n}', file=sys.stderr)
            continue
        seen.add(n)
        todo += pkgs[n].get('DEPENDS', [])
    return sorted(seen)


def main():
    db, out, roots = sys.argv[1], sys.argv[2], sys.argv[3:]
    pkgs, provides = parse_db(db)
    names = closure(pkgs, provides, roots)
    cache = os.path.join(os.path.dirname(os.path.abspath(db)), 'pkgcache')
    os.makedirs(cache, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    for n in names:
        fn = pkgs[n]['FILENAME'][0]
        local = os.path.join(cache, fn)
        if not os.path.exists(local):
            print('download', fn)
            urllib.request.urlretrieve(REPO + fn, local + '.part')
            os.replace(local + '.part', local)
        subprocess.run(['tar', '--zstd', '-xf', local, '-C', out,
                        '--exclude=.PKGINFO', '--exclude=.MTREE', '--exclude=.BUILDINFO',
                        '--exclude=.INSTALL'], check=True)
    print(f'{len(names)} packages extracted to {out}')


if __name__ == '__main__':
    main()
