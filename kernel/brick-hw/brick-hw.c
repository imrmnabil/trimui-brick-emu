// SPDX-License-Identifier: GPL-2.0
/*
 * brick-hw: TrimUI Brick (TG3040, Allwinner A133P) hardware personality for
 * a QEMU "virt" guest.  It makes the unmodified stock firmware userspace (and
 * custom firmwares that reuse it, such as NextUI) believe it runs on a Brick:
 *
 *  - Fake Allwinner PIO register pages.  /dev/mem mmaps of the PIO block
 *    (0x0300B000) and R_PIO block (0x07022000) are redirected to RAM pages
 *    whose "data" registers reflect host keyboard / gamepad state.  The stock
 *    trimui_inputd reads buttons exactly this way and builds the
 *    "TRIMUI Player1" uinput gamepad from it.
 *  - Host input translation: every other key-capable input device (the QEMU
 *    virtio keyboard, passthrough gamepads) is grabbed and translated to PIO
 *    bits, so applications only ever see Brick-shaped input devices.
 *  - "axp2202-pek" power key input device (KEY_POWER).
 *  - axp2202-battery / axp2202-usb / AC power supplies.
 *  - /sys/class/{led_anim,motor,speaker} attribute stubs.
 *  - /dev/disp stub that answers the sunxi disp2 ioctls the firmware uses.
 *  - Emulated sleep: /sys/power/state is replaced so that "mem" (NextUI's
 *    deep sleep, the stock firmware's suspend) blocks until the power key is
 *    pressed, like a real suspend/resume, instead of really suspending the
 *    guest - a suspended QEMU guest has no wakeup source and never returns.
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/mm.h>
#include <linux/gfp.h>
#include <linux/input.h>
#include <linux/miscdevice.h>
#include <linux/fs.h>
#include <linux/uaccess.h>
#include <linux/power_supply.h>
#include <linux/platform_device.h>
#include <linux/device/class.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/wait.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>
#include <linux/brick-hw.h>

/* ------------------------------------------------------------------ */
/* Fake PIO                                                            */
/* ------------------------------------------------------------------ */

#define PIO_BASE	0x0300B000UL	/* PA..PH, 0x24 bytes per bank */
#define R_PIO_BASE	0x07022000UL	/* PL.. */
#define PIO_BANK_SIZE	0x24
#define PIO_DAT_OFF	0x10
#define PIO_REGION_PAGES 2

struct fake_region {
	unsigned long phys;
	struct page *pages;
	void *virt;
};

static struct fake_region regions[] = {
	{ .phys = PIO_BASE },
	{ .phys = R_PIO_BASE },
};

static DEFINE_SPINLOCK(pio_lock);

/* pin numbering as used by sunxi userspace: bank * 32 + pin */
#define PIN(bank, n) (((bank) - 'A') * 32 + (n))

enum brick_btn {
	BB_B, BB_Y, BB_SELECT, BB_START, BB_UP, BB_DOWN, BB_LEFT, BB_RIGHT,
	BB_A, BB_X, BB_L, BB_R, BB_L2, BB_R2, BB_MENU, BB_F1, BB_F2,
	BB_VOLUP, BB_VOLDOWN, BB_POWER, BB_COUNT
};

/* Button -> GPIO pin, recovered from the stock trimui_inputd key table. */
static const int btn_pin[BB_COUNT] = {
	[BB_B]       = PIN('D', 13),
	[BB_Y]       = PIN('D', 19),
	[BB_SELECT]  = PIN('H', 15),
	[BB_START]   = PIN('H', 16),
	[BB_UP]      = PIN('D', 17),
	[BB_DOWN]    = PIN('D', 14),
	[BB_LEFT]    = PIN('D', 15),
	[BB_RIGHT]   = PIN('D', 16),
	[BB_A]       = PIN('D', 12),
	[BB_X]       = PIN('D', 18),
	[BB_L]       = PIN('B', 4),
	[BB_R]       = PIN('H', 17),
	[BB_L2]      = PIN('B', 2),
	[BB_R2]      = PIN('B', 5),
	[BB_MENU]    = PIN('H', 14),
	[BB_F1]      = PIN('H', 12),
	[BB_F2]      = PIN('H', 13),
	[BB_VOLUP]   = PIN('D', 21),
	[BB_VOLDOWN] = PIN('D', 20),
	[BB_POWER]   = -1,		/* PMIC key, not a GPIO */
};

static u32 *pio_dat_reg(int pin)
{
	int bank = pin / 32;

	if (bank == 11)	/* PL lives in R_PIO */
		return regions[1].virt + PIO_DAT_OFF;
	return regions[0].virt + bank * PIO_BANK_SIZE + PIO_DAT_OFF;
}

static void pio_set_pressed(int pin, bool pressed)
{
	u32 *reg = pio_dat_reg(pin);
	u32 bit = BIT(pin % 32);
	unsigned long flags;

	/* Buttons are active low (pulled up). */
	spin_lock_irqsave(&pio_lock, flags);
	if (pressed)
		WRITE_ONCE(*reg, READ_ONCE(*reg) & ~bit);
	else
		WRITE_ONCE(*reg, READ_ONCE(*reg) | bit);
	spin_unlock_irqrestore(&pio_lock, flags);
}

int brick_devmem_mmap(struct vm_area_struct *vma)
{
	unsigned long phys = vma->vm_pgoff << PAGE_SHIFT;
	unsigned long size = vma->vm_end - vma->vm_start;
	int i;

	for (i = 0; i < ARRAY_SIZE(regions); i++) {
		struct fake_region *r = &regions[i];
		unsigned long rsize = PIO_REGION_PAGES * PAGE_SIZE;

		if (!r->virt || phys < r->phys || phys + size > r->phys + rsize)
			continue;
		/* Normal cacheable RAM; userspace treats it as registers. */
		return remap_pfn_range(vma, vma->vm_start,
				       page_to_pfn(r->pages) + ((phys - r->phys) >> PAGE_SHIFT),
				       size, vm_get_page_prot(vma->vm_flags));
	}
	return -ENOENT;
}

static int __init fake_pio_init(void)
{
	int i;

	for (i = 0; i < ARRAY_SIZE(regions); i++) {
		regions[i].pages = alloc_pages(GFP_KERNEL | __GFP_ZERO,
					       get_order(PIO_REGION_PAGES * PAGE_SIZE));
		if (!regions[i].pages)
			return -ENOMEM;
		regions[i].virt = page_address(regions[i].pages);
	}
	/* All data lines idle high: nothing pressed. */
	for (i = 0; i < 8; i++)		/* banks PA..PH */
		*(u32 *)(regions[0].virt + i * PIO_BANK_SIZE + PIO_DAT_OFF) = 0xffffffff;
	*(u32 *)(regions[1].virt + PIO_DAT_OFF) = 0xffffffff;	/* PL */
	return 0;
}

/* ------------------------------------------------------------------ */
/* Power key + host input translation                                  */
/* ------------------------------------------------------------------ */

static struct input_dev *pek_dev;

struct keymap_ent {
	unsigned short code;
	unsigned char btn;
};

/*
 * Default host mapping.  Keyboard layout follows common emulator
 * conventions; gamepad codes cover standard evdev pads passed through
 * to the guest.
 */
static const struct keymap_ent keymap[] = {
	/* keyboard */
	{ KEY_UP, BB_UP }, { KEY_DOWN, BB_DOWN },
	{ KEY_LEFT, BB_LEFT }, { KEY_RIGHT, BB_RIGHT },
	{ KEY_X, BB_A }, { KEY_Z, BB_B }, { KEY_S, BB_X }, { KEY_A, BB_Y },
	{ KEY_Q, BB_L }, { KEY_W, BB_R }, { KEY_E, BB_L2 }, { KEY_R, BB_R2 },
	{ KEY_ENTER, BB_START }, { KEY_KPENTER, BB_START },
	{ KEY_RIGHTSHIFT, BB_SELECT }, { KEY_BACKSPACE, BB_SELECT },
	{ KEY_ESC, BB_MENU }, { KEY_M, BB_MENU },
	{ KEY_1, BB_F1 }, { KEY_2, BB_F2 },
	{ KEY_EQUAL, BB_VOLUP }, { KEY_KPPLUS, BB_VOLUP },
	{ KEY_MINUS, BB_VOLDOWN }, { KEY_KPMINUS, BB_VOLDOWN },
	{ KEY_P, BB_POWER }, { KEY_END, BB_POWER },
	{ KEY_POWER, BB_POWER },	/* QEMU system_powerdown (gpio-keys) */
	/* gamepad */
	{ BTN_DPAD_UP, BB_UP }, { BTN_DPAD_DOWN, BB_DOWN },
	{ BTN_DPAD_LEFT, BB_LEFT }, { BTN_DPAD_RIGHT, BB_RIGHT },
	{ BTN_EAST, BB_A }, { BTN_SOUTH, BB_B },
	{ BTN_NORTH, BB_X }, { BTN_WEST, BB_Y },
	{ BTN_TL, BB_L }, { BTN_TR, BB_R }, { BTN_TL2, BB_L2 }, { BTN_TR2, BB_R2 },
	{ BTN_SELECT, BB_SELECT }, { BTN_START, BB_START }, { BTN_MODE, BB_MENU },
	{ BTN_THUMBL, BB_F1 }, { BTN_THUMBR, BB_F2 },
};

/* emulated sleep (see brick_sleep_store) */
static DECLARE_WAIT_QUEUE_HEAD(sleep_wq);
static DEFINE_SPINLOCK(sleep_lock);
static int sleepers;		/* tasks sleeping in /sys/power/state */
static unsigned long wake_seq;	/* bumped by each waking power press */
static bool swallow_release;	/* hide the waking press from userspace */

static void brick_button(int btn, bool pressed)
{
	if (btn == BB_POWER) {
		unsigned long flags;
		bool hide = false;

		spin_lock_irqsave(&sleep_lock, flags);
		if (pressed && sleepers) {
			/* the press wakes the Brick; like the real PMIC wake it is not a key event */
			wake_seq++;
			swallow_release = true;
			hide = true;
		} else if (!pressed && swallow_release) {
			swallow_release = false;
			hide = true;
		}
		spin_unlock_irqrestore(&sleep_lock, flags);
		if (hide) {
			wake_up_all(&sleep_wq);
			return;
		}
		input_report_key(pek_dev, KEY_POWER, pressed);
		input_sync(pek_dev);
		return;
	}
	pio_set_pressed(btn_pin[btn], pressed);
}

static void brick_hat(int axis, int value)
{
	/* Gamepad hats and analog d-pads. */
	if (axis == ABS_HAT0X || axis == ABS_X) {
		int thr = axis == ABS_X ? 16384 : 0;

		if (axis == ABS_X && value >= 0 && value <= 255)	/* 8-bit pads */
			value = (value - 128) * 256;
		brick_button(BB_LEFT, value < -thr);
		brick_button(BB_RIGHT, value > thr);
	} else if (axis == ABS_HAT0Y || axis == ABS_Y) {
		int thr = axis == ABS_Y ? 16384 : 0;

		if (axis == ABS_Y && value >= 0 && value <= 255)
			value = (value - 128) * 256;
		brick_button(BB_UP, value < -thr);
		brick_button(BB_DOWN, value > thr);
	}
}

static void brick_input_event(struct input_handle *handle, unsigned int type,
			      unsigned int code, int value)
{
	int i;

	if (type == EV_ABS) {
		brick_hat(code, value);
		return;
	}
	if (type != EV_KEY || value == 2)	/* ignore autorepeat */
		return;
	for (i = 0; i < ARRAY_SIZE(keymap); i++)
		if (keymap[i].code == code)
			brick_button(keymap[i].btn, value);
}

static bool brick_is_own(struct input_dev *dev)
{
	/* Never grab devices that belong to the emulated Brick itself. */
	if (dev == pek_dev)
		return true;
	if (dev->name && !strncmp(dev->name, "TRIMUI", 6))
		return true;
	/*
	 * Devices without a parent are software devices created inside the
	 * guest (uinput, e.g. trimui_inputd's gamepad).  Host-facing devices
	 * (virtio keyboard, passed-through USB pads) always have a parent.
	 */
	return !dev->dev.parent;
}

static int brick_input_connect(struct input_handler *handler,
			       struct input_dev *dev,
			       const struct input_device_id *id)
{
	struct input_handle *handle;
	int err;

	if (brick_is_own(dev))
		return -ENODEV;

	handle = kzalloc(sizeof(*handle), GFP_KERNEL);
	if (!handle)
		return -ENOMEM;
	handle->dev = dev;
	handle->handler = handler;
	handle->name = "brick-hw";

	err = input_register_handle(handle);
	if (err)
		goto err_free;
	err = input_open_device(handle);
	if (err)
		goto err_unreg;
	/* Hide host devices from guest userspace (evdev). */
	input_grab_device(handle);
	pr_info("brick-hw: mapping host input \"%s\"\n", dev->name);
	return 0;

err_unreg:
	input_unregister_handle(handle);
err_free:
	kfree(handle);
	return err;
}

static void brick_input_disconnect(struct input_handle *handle)
{
	input_release_device(handle);
	input_close_device(handle);
	input_unregister_handle(handle);
	kfree(handle);
}

static const struct input_device_id brick_input_ids[] = {
	{ .flags = INPUT_DEVICE_ID_MATCH_EVBIT, .evbit = { BIT_MASK(EV_KEY) } },
	{ },
};

static struct input_handler brick_input_handler = {
	.event = brick_input_event,
	.connect = brick_input_connect,
	.disconnect = brick_input_disconnect,
	.name = "brick-hw",
	.id_table = brick_input_ids,
};

static int __init brick_pek_init(void)
{
	int err;

	pek_dev = input_allocate_device();
	if (!pek_dev)
		return -ENOMEM;
	pek_dev->name = "axp2202-pek";
	pek_dev->phys = "m1kbd/input2";
	pek_dev->id.bustype = BUS_HOST;
	input_set_capability(pek_dev, EV_KEY, KEY_POWER);
	err = input_register_device(pek_dev);
	if (err)
		input_free_device(pek_dev);
	return err;
}

/* ------------------------------------------------------------------ */
/* Power supplies                                                      */
/* ------------------------------------------------------------------ */

static int bat_capacity = 85;
module_param(bat_capacity, int, 0644);
MODULE_PARM_DESC(bat_capacity, "Reported battery capacity (percent)");
static int usb_online;
module_param(usb_online, int, 0644);
MODULE_PARM_DESC(usb_online, "Charger connected");

static enum power_supply_property bat_props[] = {
	POWER_SUPPLY_PROP_STATUS, POWER_SUPPLY_PROP_PRESENT,
	POWER_SUPPLY_PROP_HEALTH, POWER_SUPPLY_PROP_TECHNOLOGY,
	POWER_SUPPLY_PROP_CAPACITY, POWER_SUPPLY_PROP_VOLTAGE_NOW,
	POWER_SUPPLY_PROP_CURRENT_NOW, POWER_SUPPLY_PROP_TEMP,
	POWER_SUPPLY_PROP_TIME_TO_FULL_NOW, POWER_SUPPLY_PROP_TIME_TO_EMPTY_NOW,
};

static int bat_get(struct power_supply *psy, enum power_supply_property psp,
		   union power_supply_propval *val)
{
	int cap = clamp(bat_capacity, 0, 100);

	switch (psp) {
	case POWER_SUPPLY_PROP_STATUS:
		val->intval = !usb_online ? POWER_SUPPLY_STATUS_DISCHARGING :
			cap >= 100 ? POWER_SUPPLY_STATUS_FULL :
			POWER_SUPPLY_STATUS_CHARGING;
		break;
	case POWER_SUPPLY_PROP_PRESENT:
		val->intval = 1;
		break;
	case POWER_SUPPLY_PROP_HEALTH:
		val->intval = POWER_SUPPLY_HEALTH_GOOD;
		break;
	case POWER_SUPPLY_PROP_TECHNOLOGY:
		val->intval = POWER_SUPPLY_TECHNOLOGY_LIPO;
		break;
	case POWER_SUPPLY_PROP_CAPACITY:
		val->intval = cap;
		break;
	case POWER_SUPPLY_PROP_VOLTAGE_NOW:
		val->intval = 3500000 + cap * 7000;
		break;
	case POWER_SUPPLY_PROP_CURRENT_NOW:
		val->intval = usb_online ? 500000 : -300000;
		break;
	case POWER_SUPPLY_PROP_TEMP:
		val->intval = 300;	/* 30.0 C */
		break;
	case POWER_SUPPLY_PROP_TIME_TO_FULL_NOW:
		val->intval = usb_online ? (100 - cap) * 60 : 0;
		break;
	case POWER_SUPPLY_PROP_TIME_TO_EMPTY_NOW:
		val->intval = cap * 300;
		break;
	default:
		return -EINVAL;
	}
	return 0;
}

static enum power_supply_property online_props[] = { POWER_SUPPLY_PROP_ONLINE };

static int online_get(struct power_supply *psy, enum power_supply_property psp,
		      union power_supply_propval *val)
{
	if (psp != POWER_SUPPLY_PROP_ONLINE)
		return -EINVAL;
	val->intval = !!usb_online;
	return 0;
}

static const struct power_supply_desc psy_descs[] = {
	{ .name = "axp2202-battery", .type = POWER_SUPPLY_TYPE_BATTERY,
	  .properties = bat_props, .num_properties = ARRAY_SIZE(bat_props),
	  .get_property = bat_get },
	{ .name = "axp2202-usb", .type = POWER_SUPPLY_TYPE_USB,
	  .properties = online_props, .num_properties = ARRAY_SIZE(online_props),
	  .get_property = online_get },
	{ .name = "AC", .type = POWER_SUPPLY_TYPE_MAINS,
	  .properties = online_props, .num_properties = ARRAY_SIZE(online_props),
	  .get_property = online_get },
};

/* ------------------------------------------------------------------ */
/* sysfs class attribute stubs (store whatever is written)             */
/* ------------------------------------------------------------------ */

struct stub_attr {
	struct class_attribute attr;
	char value[64];
};

struct stub_class {
	const char *name;
	const char *const *attrs;
	const char *defval;
	struct class *cls;
	struct stub_attr *sattrs;
};

static const char *const led_anim_attrs[] = {
	"enable", "max_scale", "max_scale_lr", "max_scale_f1f2",
	"effect_enable", "effect_lr", "effect_m", "effect_f1", "effect_f2",
	"effect_rgb_hex_lr", "effect_rgb_hex_m", "effect_rgb_hex_f1", "effect_rgb_hex_f2",
	"effect_cycles_lr", "effect_cycles_m", "effect_cycles_f1", "effect_cycles_f2",
	"effect_duration_lr", "effect_duration_m", "effect_duration_f1", "effect_duration_f2",
	"frame_hex", "anim_frames_enable", "anim_frames_hex", "anim_frames_cycles",
	"anim_frames_mask_lr_enable", "anim_frames_mask_m_enable",
	"anim_frames_mask_f1f2_enable", NULL
};
static const char *const motor_attrs[] = { "voltage", NULL };
static const char *const speaker_attrs[] = { "mute", NULL };

static struct stub_class stub_classes[] = {
	{ .name = "led_anim", .attrs = led_anim_attrs, .defval = "0" },
	{ .name = "motor", .attrs = motor_attrs, .defval = "0" },
	{ .name = "speaker", .attrs = speaker_attrs, .defval = "0" },
};

static ssize_t stub_show(const struct class *c, const struct class_attribute *a, char *buf)
{
	struct stub_attr *s = container_of(a, struct stub_attr, attr);

	return sysfs_emit(buf, "%s\n", s->value);
}

static ssize_t stub_store(const struct class *c, const struct class_attribute *a,
			  const char *buf, size_t count)
{
	struct stub_attr *s = container_of((struct class_attribute *)a, struct stub_attr, attr);

	strscpy(s->value, buf, min(count + 1, sizeof(s->value)));
	strim(s->value);
	return count;
}

static int __init stub_classes_init(void)
{
	int i, j, n;

	for (i = 0; i < ARRAY_SIZE(stub_classes); i++) {
		struct stub_class *sc = &stub_classes[i];

		sc->cls = class_create(sc->name);
		if (IS_ERR(sc->cls))
			return PTR_ERR(sc->cls);
		for (n = 0; sc->attrs[n]; n++)
			;
		sc->sattrs = kcalloc(n, sizeof(*sc->sattrs), GFP_KERNEL);
		if (!sc->sattrs)
			return -ENOMEM;
		for (j = 0; j < n; j++) {
			struct stub_attr *s = &sc->sattrs[j];

			sysfs_attr_init(&s->attr.attr);
			s->attr.attr.name = sc->attrs[j];
			s->attr.attr.mode = 0666;
			s->attr.show = stub_show;
			s->attr.store = stub_store;
			strscpy(s->value, sc->defval, sizeof(s->value));
			if (class_create_file(sc->cls, &s->attr))
				pr_warn("brick-hw: %s/%s failed\n", sc->name, sc->attrs[j]);
		}
	}
	return 0;
}

/* ------------------------------------------------------------------ */
/* /dev/disp (sunxi disp2) stub                                        */
/* ------------------------------------------------------------------ */

#define DISP_GET_SCN_WIDTH		0x07
#define DISP_GET_SCN_HEIGHT		0x08
#define DISP_GET_OUTPUT_TYPE		0x09
#define DISP_LCD_SET_BRIGHTNESS		0x102
#define DISP_LCD_GET_BRIGHTNESS		0x103

static unsigned long disp_brightness = 128;

static long disp_ioctl(struct file *f, unsigned int cmd, unsigned long arg)
{
	unsigned long a[4] = { 0 };

	if (arg && copy_from_user(a, (void __user *)arg, sizeof(a)))
		memset(a, 0, sizeof(a));

	switch (cmd) {
	case DISP_GET_SCN_WIDTH:
		return 1024;
	case DISP_GET_SCN_HEIGHT:
		return 768;
	case DISP_GET_OUTPUT_TYPE:
		return 1;	/* LCD */
	case DISP_LCD_SET_BRIGHTNESS:
		disp_brightness = a[1];
		return 0;
	case DISP_LCD_GET_BRIGHTNESS:
		return disp_brightness;
	default:
		return 0;
	}
}

static const struct file_operations disp_fops = {
	.owner = THIS_MODULE,
	.unlocked_ioctl = disp_ioctl,
	.compat_ioctl = disp_ioctl,
};

static struct miscdevice disp_misc = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "disp",
	.fops = &disp_fops,
	.mode = 0666,
};

/* ------------------------------------------------------------------ */
/* Emulated sleep: /sys/power/state                                    */
/* ------------------------------------------------------------------ */

static ssize_t brick_sleep_show(struct kobject *kobj, struct kobj_attribute *attr,
				char *buf)
{
	return sysfs_emit(buf, "freeze mem\n");
}

static ssize_t brick_sleep_store(struct kobject *kobj, struct kobj_attribute *attr,
				 const char *buf, size_t n)
{
	unsigned long seq;
	int err;

	if (!sysfs_streq(buf, "mem") && !sysfs_streq(buf, "freeze") &&
	    !sysfs_streq(buf, "standby"))
		return -EINVAL;
	spin_lock_irq(&sleep_lock);
	sleepers++;
	seq = wake_seq;
	spin_unlock_irq(&sleep_lock);
	pr_info("brick-hw: sleeping until the power key is pressed\n");
	err = wait_event_killable(sleep_wq, READ_ONCE(wake_seq) != seq);
	spin_lock_irq(&sleep_lock);
	sleepers--;
	spin_unlock_irq(&sleep_lock);
	if (err)
		return err;
	pr_info("brick-hw: woken by the power key\n");
	return n;
}

static struct kobj_attribute brick_sleep_attr =
	__ATTR(state, 0644, brick_sleep_show, brick_sleep_store);

static int __init brick_sleep_init(void)
{
	if (!power_kobj)
		return -ENODEV;
	/* drop the kernel's own "state" (removal goes by name) and take its place */
	sysfs_remove_file(power_kobj, &brick_sleep_attr.attr);
	return sysfs_create_file(power_kobj, &brick_sleep_attr.attr);
}

/* ------------------------------------------------------------------ */

static int __init brick_hw_init(void)
{
	int err, i;

	err = fake_pio_init();
	if (err)
		return err;
	err = brick_pek_init();
	if (err)
		return err;
	err = input_register_handler(&brick_input_handler);
	if (err)
		return err;
	for (i = 0; i < ARRAY_SIZE(psy_descs); i++) {
		struct power_supply *psy = power_supply_register_no_ws(NULL, &psy_descs[i], NULL);

		if (IS_ERR(psy))
			pr_warn("brick-hw: power supply %s failed\n", psy_descs[i].name);
	}
	err = stub_classes_init();
	if (err)
		pr_warn("brick-hw: sysfs stubs failed: %d\n", err);
	err = misc_register(&disp_misc);
	if (err)
		pr_warn("brick-hw: /dev/disp failed: %d\n", err);
	err = brick_sleep_init();
	if (err)
		pr_warn("brick-hw: emulated sleep failed: %d\n", err);
	pr_info("brick-hw: TrimUI Brick hardware personality ready\n");
	return 0;
}
late_initcall(brick_hw_init);

MODULE_DESCRIPTION("TrimUI Brick hardware personality for QEMU");
MODULE_LICENSE("GPL");
