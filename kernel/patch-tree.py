#!/usr/bin/env python3
"""Small source patches applied to the guest kernel tree (idempotent)."""
import re, sys, os
tree = sys.argv[1]

def patch(path, old, new, marker):
    p = os.path.join(tree, path)
    s = open(p).read()
    if marker in s:
        return
    if old not in s:
        sys.exit(f'patch anchor not found in {path}: {old!r}')
    open(p, 'w').write(s.replace(old, new, 1))
    print('patched', path)

# 1. /dev/mem: let brick-hw serve the fake Allwinner PIO register pages.
patch('drivers/char/mem.c',
      'static int mmap_mem(struct file *file, struct vm_area_struct *vma)\n{\n',
      'static int mmap_mem(struct file *file, struct vm_area_struct *vma)\n{\n'
      '\t{\n\t\tint brick_ret = brick_devmem_mmap(vma);\n\n'
      '\t\tif (brick_ret != -ENOENT)\n\t\t\treturn brick_ret;\n\t}\n',
      'brick_devmem_mmap')
patch('drivers/char/mem.c', '#include <linux/mm.h>\n',
      '#include <linux/mm.h>\n#include <linux/brick-hw.h>\n', 'linux/brick-hw.h')

# 2. DRM fbdev emulation: flush mmap'ed framebuffer writes at ~60Hz instead of 20Hz.
p = 'drivers/gpu/drm/drm_fbdev_generic.c'
s = open(os.path.join(tree, p)).read()
if 'HZ / 20' in s:
    open(os.path.join(tree, p), 'w').write(s.replace('HZ / 20', 'HZ / 60'))
    print('patched', p)

# 3. mmcblkN == mmcN: the firmware hard-codes mmcblk0 (eMMC) and mmcblk1 (SD).
patch('drivers/mmc/core/block.c',
      '\tdevidx = ida_simple_get(&mmc_blk_ida, 0, max_devices, GFP_KERNEL);\n',
      '\t/* brick: prefer mmcblk index == host index for stable naming */\n'
      '\tdevidx = ida_simple_get(&mmc_blk_ida, card->host->index, card->host->index + 1, GFP_KERNEL);\n'
      '\tif (devidx < 0)\n'
      '\t\tdevidx = ida_simple_get(&mmc_blk_ida, 0, max_devices, GFP_KERNEL);\n',
      'brick: prefer mmcblk index')

# 4. Probe SDHCI controllers synchronously so mmc0 = eMMC (PCI 00:05) and
#    mmc1 = SD slot (PCI 00:06) on every boot.
patch('drivers/mmc/host/sdhci-pci-core.c',
      '.probe_type = PROBE_PREFER_ASYNCHRONOUS,',
      '.probe_type = PROBE_FORCE_SYNCHRONOUS, /* brick: stable mmc numbering */',
      'brick: stable mmc numbering')

# 5. /proc/cpuinfo "hwserial" line like the TrimUI BSP kernel prints
#    ("TG3040%02X%08X").  NextUI and spruceOS detect the Brick from it.
patch('arch/arm64/kernel/cpuinfo.c',
      'seq_printf(m, "CPU revision\\t: %d\\n\\n", MIDR_REVISION(midr));',
      'seq_printf(m, "CPU revision\\t: %d\\n", MIDR_REVISION(midr));\n'
      '\t\tseq_printf(m, "hwserial\\t: TG3040%02X%08X\\n\\n", 0x01, 0x0B41C000); /* brick */',
      'TG3040%02X%08X')
