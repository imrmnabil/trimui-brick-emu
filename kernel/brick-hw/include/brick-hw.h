/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_BRICK_HW_H
#define _LINUX_BRICK_HW_H
struct vm_area_struct;
#ifdef CONFIG_BRICK_HW
int brick_devmem_mmap(struct vm_area_struct *vma);
#else
static inline int brick_devmem_mmap(struct vm_area_struct *vma) { return -ENOENT; }
#endif
#endif
