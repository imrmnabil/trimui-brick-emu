#!/usr/bin/env python3
"""Unpack an (unencrypted) Allwinner IMAGEWTY v3 .awimg firmware image."""
import os, struct, sys

def main(img, out):
    os.makedirs(out, exist_ok=True)
    with open(img, 'rb') as f:
        hdr = f.read(0x60)
        assert hdr[:8] == b'IMAGEWTY', 'not an IMAGEWTY image'
        nfiles = struct.unpack_from('<I', hdr, 0x3C)[0]
        for i in range(nfiles):
            f.seek(0x400 + i * 0x400)
            fh = f.read(0x400)
            main_t = fh[8:16].decode().strip()
            sub_t = fh[16:32].decode(errors='replace').strip('\0 ')
            name = fh[0x24:0x124].split(b'\0')[0].decode()
            stored, _, orig, _, off = struct.unpack_from('<IIIII', fh, 0x124)
            f.seek(off)
            data = f.read(orig)
            base = os.path.basename(name)
            with open(os.path.join(out, base), 'wb') as o:
                o.write(data)
            print(f'{main_t:8} {sub_t:16} {base:28} {orig:>12}')

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
