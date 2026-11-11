/*
 * libEGL shim for the TrimUI Brick emulator.
 *
 * The Brick ships PowerVR GE8300 userspace (libEGL/libGLESv2 + NULL WSEGL)
 * that renders straight to /dev/fb0.  SDL2's "mali" video backend (and other
 * fbdev EGL users) hand eglCreateWindowSurface() a pointer to a
 * { unsigned short width, height; } "fbdev window".
 *
 * Under emulation there is no PowerVR GPU, so this library replaces libEGL and
 * implements fbdev window semantics on top of Mesa:
 *
 *  GBM backend (default): Mesa's GBM platform on the virtio-gpu DRM device.
 *    llvmpipe (kms_swrast) or virgl renders into GBM buffers; on swap the
 *    front buffer is mapped and written to /dev/fb0 - no GL read-back.
 *  pbuffer backend (fallback): Mesa surfaceless platform; window surfaces
 *    are pbuffers and swap uses glReadPixels.
 *
 * Swaps are paced to the panel's 60Hz like the real vsync.  Everything else is
 * forwarded to Mesa unchanged.
 *
 * Environment: BRICK_EGL_DEBUG=1 (log + per-frame timings),
 *              BRICK_EGL_BACKEND=gbm|pbuffer, BRICK_EGL_HW=1 (allow virgl).
 */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <gbm.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#ifndef EGL_PLATFORM_SURFACELESS_MESA
#define EGL_PLATFORM_SURFACELESS_MESA 0x31DD
#endif
#ifndef EGL_PLATFORM_GBM_KHR
#define EGL_PLATFORM_GBM_KHR 0x31D7
#endif
#ifndef GL_PIXEL_PACK_BUFFER
#define GL_PIXEL_PACK_BUFFER 0x88EB
#define GL_PIXEL_PACK_BUFFER_BINDING 0x88ED
#endif
#ifndef GL_PACK_ROW_LENGTH
#define GL_PACK_ROW_LENGTH 0x0D02
#endif

#define DRM_IOCTL_DROP_MASTER_ _IO('d', 0x1f)

#define EMU_DIR "/brick-emu"
#define MESA_DIR EMU_DIR "/mesa"
#define MESA_EGL MESA_DIR "/libEGL_glvnd.so.1"

#define EXPORT __attribute__((visibility("default")))

static void *mesa;
static pthread_once_t init_once = PTHREAD_ONCE_INIT;
static int debug;
static enum { BACKEND_GBM, BACKEND_PBUFFER } backend = BACKEND_GBM;

#define LOG(...) do { if (debug) fprintf(stderr, "[brick-egl] " __VA_ARGS__); } while (0)

/* ---- real entry points ------------------------------------------------ */
#define REAL(name) static __typeof__(&name) real_##name
REAL(eglGetPlatformDisplay);
REAL(eglChooseConfig);
REAL(eglGetConfigAttrib);
REAL(eglCreateWindowSurface);
REAL(eglCreatePbufferSurface);
REAL(eglDestroySurface);
REAL(eglSwapBuffers);
REAL(eglSwapInterval);
REAL(eglGetProcAddress);
REAL(eglGetCurrentSurface);
REAL(eglQuerySurface);

/* GBM, loaded from the emulator's Mesa copy */
static struct {
	void *lib;
	struct gbm_device *(*create_device)(int);
	struct gbm_surface *(*surface_create)(struct gbm_device *, uint32_t, uint32_t, uint32_t, uint32_t);
	void (*surface_destroy)(struct gbm_surface *);
	struct gbm_bo *(*lock_front)(struct gbm_surface *);
	void (*release)(struct gbm_surface *, struct gbm_bo *);
	void *(*bo_map)(struct gbm_bo *, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t *, void **);
	void (*bo_unmap)(struct gbm_bo *, void *);
	struct gbm_device *dev;
	int fd;
} gbm = { .fd = -1 };

static void *sym(const char *name)
{
	void *p = mesa ? dlsym(mesa, name) : NULL;

	if (!p)
		fprintf(stderr, "[brick-egl] missing %s\n", name);
	return p;
}

static int cmdline_has(const char *needle)
{
	char buf[4096];
	int fd = open("/proc/cmdline", O_RDONLY), n;

	if (fd < 0)
		return 0;
	n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n <= 0)
		return 0;
	buf[n] = 0;
	return strstr(buf, needle) != NULL;
}

static int gbm_init(void)
{
	gbm.lib = dlopen(MESA_DIR "/libgbm.so.1", RTLD_NOW | RTLD_LOCAL);
	if (!gbm.lib)
		return -1;
#define G(field, name) gbm.field = (__typeof__(gbm.field))dlsym(gbm.lib, name)
	G(create_device, "gbm_create_device");
	G(surface_create, "gbm_surface_create");
	G(surface_destroy, "gbm_surface_destroy");
	G(lock_front, "gbm_surface_lock_front_buffer");
	G(release, "gbm_surface_release_buffer");
	G(bo_map, "gbm_bo_map");
	G(bo_unmap, "gbm_bo_unmap");
	if (!gbm.create_device || !gbm.bo_map)
		return -1;
	/*
	 * Render node: rendering needs no modesetting rights.  Opening the primary
	 * node would make this process DRM master, and then the kernel's fbdev
	 * emulation - which shows our frames - may never set a mode: the panel
	 * stays off ("Display output is not active").
	 */
	gbm.fd = open("/dev/dri/renderD128", O_RDWR | O_CLOEXEC);
	if (gbm.fd < 0) {
		gbm.fd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
		if (gbm.fd < 0)
			return -1;
		ioctl(gbm.fd, DRM_IOCTL_DROP_MASTER_, 0);	/* see above */
	}
	gbm.dev = gbm.create_device(gbm.fd);
	if (!gbm.dev) {
		close(gbm.fd);
		gbm.fd = -1;
		return -1;
	}
	return 0;
}

static void do_init(void)
{
	const char *be = getenv("BRICK_EGL_BACKEND");

	debug = getenv("BRICK_EGL_DEBUG") != NULL;

	/* Point glvnd and Mesa at the emulator's private copy. */
	setenv("__EGL_VENDOR_LIBRARY_FILENAMES", EMU_DIR "/glvnd/50_mesa.json", 1);
	if (!getenv("LIBGL_DRIVERS_PATH"))
		setenv("LIBGL_DRIVERS_PATH", MESA_DIR "/dri", 1);
	if (!getenv("GBM_BACKENDS_PATH"))
		setenv("GBM_BACKENDS_PATH", MESA_DIR, 1);
	int hw = cmdline_has("brick.gpu=virgl") || getenv("BRICK_EGL_HW");

	if (!hw)
		setenv("LIBGL_ALWAYS_SOFTWARE", "1", 0);
	if (!getenv("LP_NUM_THREADS"))
		setenv("LP_NUM_THREADS", "3", 0);

	mesa = dlopen(MESA_EGL, RTLD_NOW | RTLD_LOCAL);
	if (!mesa) {
		fprintf(stderr, "[brick-egl] cannot load %s: %s\n", MESA_EGL, dlerror());
		return;
	}
#define LOAD(name) real_##name = (__typeof__(real_##name))sym(#name)
	LOAD(eglGetPlatformDisplay);
	LOAD(eglChooseConfig);
	LOAD(eglGetConfigAttrib);
	LOAD(eglCreateWindowSurface);
	LOAD(eglCreatePbufferSurface);
	LOAD(eglDestroySurface);
	LOAD(eglSwapBuffers);
	LOAD(eglSwapInterval);
	LOAD(eglGetProcAddress);
	LOAD(eglGetCurrentSurface);
	LOAD(eglQuerySurface);

	/*
	 * Software rendering: llvmpipe -> pbuffer + BGRA read-back is cheapest
	 * (kms_swrast re-maps its dumb buffers every frame, very slow under TCG).
	 * virgl: GBM, the frame is rendered on the host GPU.
	 */
	if (be)
		backend = strcmp(be, "pbuffer") ? BACKEND_GBM : BACKEND_PBUFFER;
	else
		backend = hw ? BACKEND_GBM : BACKEND_PBUFFER;
	if (backend == BACKEND_GBM && gbm_init()) {
		LOG("GBM unavailable, using pbuffer backend\n");
		backend = BACKEND_PBUFFER;
	}
	LOG("Mesa loaded: backend=%s software=%s\n", backend == BACKEND_GBM ? "gbm" : "pbuffer",
	    getenv("LIBGL_ALWAYS_SOFTWARE") ? "yes" : "no");
}

static void init(void)
{
	pthread_once(&init_once, do_init);
}

/* ---- framebuffer ------------------------------------------------------ */
static struct {
	int fd;
	struct fb_var_screeninfo var;
	struct fb_fix_screeninfo fix;
	uint8_t *stage;
	size_t stage_size;
} fb = { .fd = -1 };
static pthread_mutex_t fb_lock = PTHREAD_MUTEX_INITIALIZER;

static int fb_open(void)
{
	if (fb.fd >= 0)
		return 0;
	fb.fd = open("/dev/fb0", O_RDWR | O_CLOEXEC);
	if (fb.fd < 0)
		return -1;
	if (ioctl(fb.fd, FBIOGET_VSCREENINFO, &fb.var) || ioctl(fb.fd, FBIOGET_FSCREENINFO, &fb.fix)) {
		close(fb.fd);
		fb.fd = -1;
		return -1;
	}
	/* light the panel: without fbcon nothing else sets the fbdev mode */
	ioctl(fb.fd, FBIOBLANK, FB_BLANK_UNBLANK);
	LOG("fb0 %ux%u bpp %u stride %u\n", fb.var.xres, fb.var.yres, fb.var.bits_per_pixel, fb.fix.line_length);
	return 0;
}

static uint8_t *stage_buf(size_t size)
{
	if (fb.stage_size < size) {
		free(fb.stage);
		fb.stage = malloc(size);
		fb.stage_size = fb.stage ? size : 0;
	}
	return fb.stage;
}

/*
 * Write an image to the displayed framebuffer.  src rows are top-down;
 * fmt describes the source pixel layout.
 */
enum pixfmt { PF_XRGB8888, PF_XRGB8888_BOTTOMUP, PF_RGBA8888_BOTTOMUP, PF_RGB565 };

static void fb_blit(const uint8_t *src, int W, int H, size_t sstride, enum pixfmt fmt)
{
	int bpp = fb.var.bits_per_pixel / 8;
	size_t dstride = fb.fix.line_length;
	off_t base = (off_t)fb.var.yoffset * dstride;
	int fb32 = bpp == 4 && fb.var.red.offset == 16 && fb.var.blue.offset == 0;

	if (W > (int)fb.var.xres)
		W = fb.var.xres;
	if (H > (int)fb.var.yres)
		H = fb.var.yres;

	/* Fast path: identical layout, one write. */
	if (fmt == PF_XRGB8888 && fb32 && sstride == dstride) {
		pwrite(fb.fd, src, dstride * H, base);
		return;
	}

	size_t orow = (size_t)W * bpp;
	uint8_t *out = stage_buf(orow * H);

	if (!out)
		return;
	for (int y = 0; y < H; y++) {
		int bottomup = fmt == PF_RGBA8888_BOTTOMUP || fmt == PF_XRGB8888_BOTTOMUP;
		const uint8_t *s = bottomup ? src + (size_t)(H - 1 - y) * sstride : src + (size_t)y * sstride;
		uint8_t *d = out + (size_t)y * orow;

		if (bpp == 4) {
			if (fmt == PF_XRGB8888 || fmt == PF_XRGB8888_BOTTOMUP) {
				memcpy(d, s, orow);
			} else if (fmt == PF_RGBA8888_BOTTOMUP) {
				const uint32_t *s32 = (const uint32_t *)s;
				uint32_t *d32 = (uint32_t *)d;

				for (int x = 0; x < W; x++) {	/* ABGR (LE RGBA) -> ARGB */
					uint32_t p = s32[x];

					d32[x] = (p & 0xff00ff00) | (p & 0xff) << 16 | (p >> 16 & 0xff);
				}
			} else {	/* RGB565 -> XRGB8888 */
				const uint16_t *s16 = (const uint16_t *)s;
				uint32_t *d32 = (uint32_t *)d;

				for (int x = 0; x < W; x++) {
					uint32_t p = s16[x];

					d32[x] = 0xff000000 | (p & 0xf800) << 8 | (p & 0x07e0) << 5 | (p & 0x1f) << 3;
				}
			}
		} else if (bpp == 2) {
			uint16_t *d16 = (uint16_t *)d;

			for (int x = 0; x < W; x++) {
				uint8_t r, g, b;

				if (fmt == PF_RGB565) {
					d16[x] = ((const uint16_t *)s)[x];
					continue;
				}
				if (fmt == PF_XRGB8888 || fmt == PF_XRGB8888_BOTTOMUP) {
					b = s[4 * x]; g = s[4 * x + 1]; r = s[4 * x + 2];
				} else {
					r = s[4 * x]; g = s[4 * x + 1]; b = s[4 * x + 2];
				}
				d16[x] = (r >> 3) << 11 | (g >> 2) << 5 | b >> 3;
			}
		}
	}
	if (orow == dstride) {
		pwrite(fb.fd, out, orow * H, base);
	} else {
		for (int y = 0; y < H; y++)
			pwrite(fb.fd, out + (size_t)y * orow, orow, base + (off_t)y * dstride);
	}
}

/* ---- window surfaces -------------------------------------------------- */
struct fbdev_window {
	unsigned short width, height;
};

#define MAX_WIN 16
static struct win {
	EGLDisplay dpy;
	EGLSurface surf;
	struct gbm_surface *gs;
	uint32_t gbm_format;
	int w, h;
	int interval;
	struct timespec next;
} wins[MAX_WIN];
static pthread_mutex_t win_lock = PTHREAD_MUTEX_INITIALIZER;

static struct win *find_win(EGLSurface s)
{
	for (int i = 0; i < MAX_WIN; i++)
		if (wins[i].surf == s && s != EGL_NO_SURFACE)
			return &wins[i];
	return NULL;
}

static long long now_ns(void)
{
	struct timespec t;

	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec * 1000000000LL + t.tv_nsec;
}

static struct {
	long long render, read, write, frames;
} prof;

static void prof_frame(long long t_render, long long t_read, long long t_write)
{
	if (!debug)
		return;
	prof.render += t_render;
	prof.read += t_read;
	prof.write += t_write;
	if (++prof.frames == 60) {
		fprintf(stderr, "[brick-egl] per frame: swap/render %.1fms readback %.1fms fbwrite %.1fms\n",
			prof.render / 60e6, prof.read / 60e6, prof.write / 60e6);
		memset(&prof, 0, sizeof(prof));
	}
}

static void pace(struct win *w)
{
	struct timespec now;
	const long frame_ns = 16666667;

	if (w->interval <= 0)
		return;
	clock_gettime(CLOCK_MONOTONIC, &now);
	if (w->next.tv_sec == 0 ||
	    (now.tv_sec - w->next.tv_sec) * 1000000000L + (now.tv_nsec - w->next.tv_nsec) > 2 * frame_ns) {
		w->next = now;	/* fell behind: resync instead of bursting */
	}
	w->next.tv_nsec += frame_ns * w->interval;
	while (w->next.tv_nsec >= 1000000000L) {
		w->next.tv_nsec -= 1000000000L;
		w->next.tv_sec++;
	}
	clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &w->next, NULL);
}

/* GBM backend: swap, then copy the new front buffer to fb0. */
static EGLBoolean present_gbm(struct win *w)
{
	long long t0 = now_ns(), t1, t2;
	EGLBoolean ok = real_eglSwapBuffers(w->dpy, w->surf);
	struct gbm_bo *bo;

	t1 = now_ns();
	if (!ok || fb_open())
		return ok;
	bo = gbm.lock_front(w->gs);
	if (!bo)
		return ok;

	uint32_t stride = 0;
	void *map_data = NULL;
	uint8_t *px = gbm.bo_map(bo, 0, 0, w->w, w->h, GBM_BO_TRANSFER_READ, &stride, &map_data);

	t2 = now_ns();
	if (px) {
		pthread_mutex_lock(&fb_lock);
		fb_blit(px, w->w, w->h, stride, w->gbm_format == GBM_FORMAT_RGB565 ? PF_RGB565 : PF_XRGB8888);
		pthread_mutex_unlock(&fb_lock);
		gbm.bo_unmap(bo, map_data);
	}
	gbm.release(w->gs, bo);
	prof_frame(t1 - t0, t2 - t1, now_ns() - t2);
	return ok;
}

/* pbuffer backend: read the frame back with GL. */
static void (*gl_ReadPixels)(GLint, GLint, GLsizei, GLsizei, GLenum, GLenum, void *);
static void (*gl_GetIntegerv)(GLenum, GLint *);
static void (*gl_BindFramebuffer)(GLenum, GLuint);
static void (*gl_BindBuffer)(GLenum, GLuint);
static void (*gl_PixelStorei)(GLenum, GLint);
static void (*gl_Finish)(void);
static int read_bgra = -1;

static EGLBoolean present_pbuffer(struct win *w)
{
	GLint old_fbo = 0, old_pbo = 0, old_align = 4, old_rowlen = 0;
	long long t0 = now_ns(), t1;

	if (!gl_ReadPixels) {
#define GLP(var, name) var = (__typeof__(var))real_eglGetProcAddress(name)
		GLP(gl_GetIntegerv, "glGetIntegerv");
		GLP(gl_BindFramebuffer, "glBindFramebuffer");
		GLP(gl_BindBuffer, "glBindBuffer");
		GLP(gl_PixelStorei, "glPixelStorei");
		GLP(gl_ReadPixels, "glReadPixels");
		GLP(gl_Finish, "glFinish");
	}
	if (read_bgra < 0) {
		const char *e = getenv("BRICK_EGL_READ");

		read_bgra = !(e && !strcmp(e, "rgba"));
	}
	if (!gl_ReadPixels || fb_open())
		return EGL_TRUE;

	pthread_mutex_lock(&fb_lock);
	uint8_t *buf = malloc((size_t)w->w * w->h * 4);

	if (!buf) {
		pthread_mutex_unlock(&fb_lock);
		return EGL_TRUE;
	}
	gl_GetIntegerv(GL_FRAMEBUFFER_BINDING, &old_fbo);
	gl_GetIntegerv(GL_PACK_ALIGNMENT, &old_align);
	gl_GetIntegerv(GL_PIXEL_PACK_BUFFER_BINDING, &old_pbo);
	gl_GetIntegerv(GL_PACK_ROW_LENGTH, &old_rowlen);
	if (old_pbo)
		gl_BindBuffer(GL_PIXEL_PACK_BUFFER, 0);
	if (old_rowlen)
		gl_PixelStorei(GL_PACK_ROW_LENGTH, 0);
	if (old_fbo)
		gl_BindFramebuffer(GL_FRAMEBUFFER, 0);
	gl_PixelStorei(GL_PACK_ALIGNMENT, 4);
	long long tr = 0;

	if (debug) {	/* separate rendering from read-back in the profile */
		gl_Finish();
		tr = now_ns();
	}
	gl_ReadPixels(0, 0, w->w, w->h, read_bgra ? GL_BGRA_EXT : GL_RGBA, GL_UNSIGNED_BYTE, buf);
	if (old_fbo)
		gl_BindFramebuffer(GL_FRAMEBUFFER, old_fbo);
	gl_PixelStorei(GL_PACK_ALIGNMENT, old_align);
	if (old_pbo)
		gl_BindBuffer(GL_PIXEL_PACK_BUFFER, old_pbo);
	if (old_rowlen)
		gl_PixelStorei(GL_PACK_ROW_LENGTH, old_rowlen);
	t1 = now_ns();
	fb_blit(buf, w->w, w->h, (size_t)w->w * 4, read_bgra ? PF_XRGB8888_BOTTOMUP : PF_RGBA8888_BOTTOMUP);
	free(buf);
	pthread_mutex_unlock(&fb_lock);
	prof_frame(tr ? tr - t0 : 0, tr ? t1 - tr : t1 - t0, now_ns() - t1);
	return EGL_TRUE;
}

/* ---- exported EGL API (overridden parts) ------------------------------ */

EXPORT EGLDisplay eglGetDisplay(EGLNativeDisplayType native)
{
	(void)native;	/* the fbdev "display" is always the panel */
	init();
	if (!real_eglGetPlatformDisplay)
		return EGL_NO_DISPLAY;
	if (backend == BACKEND_GBM)
		return real_eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm.dev, NULL);
	return real_eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, NULL);
}

/* pbuffer backend only: window configs are really pbuffer configs */
static EGLint *fix_config_attribs(const EGLint *in)
{
	int n = 0, have_surface = 0;

	while (in && in[n] != EGL_NONE)
		n += 2;
	EGLint *out = calloc(n + 3, sizeof(EGLint));
	int o = 0;

	for (int i = 0; i < n; i += 2) {
		EGLint k = in[i], v = in[i + 1];

		if (k == EGL_NATIVE_RENDERABLE || k == EGL_NATIVE_VISUAL_TYPE)
			continue;
		if (k == EGL_SURFACE_TYPE) {
			have_surface = 1;
			if (v != EGL_DONT_CARE && (v & EGL_WINDOW_BIT))
				v = (v & ~EGL_WINDOW_BIT) | EGL_PBUFFER_BIT;
		}
		out[o++] = k;
		out[o++] = v;
	}
	if (!have_surface) {	/* EGL default is EGL_WINDOW_BIT */
		out[o++] = EGL_SURFACE_TYPE;
		out[o++] = EGL_PBUFFER_BIT;
	}
	out[o] = EGL_NONE;
	return out;
}

EXPORT EGLBoolean eglChooseConfig(EGLDisplay dpy, const EGLint *attrib_list, EGLConfig *configs,
				  EGLint config_size, EGLint *num_config)
{
	EGLBoolean r;

	init();
	if (backend == BACKEND_GBM) {
		r = real_eglChooseConfig(dpy, attrib_list, configs, config_size, num_config);
	} else {
		EGLint *a = fix_config_attribs(attrib_list);

		r = real_eglChooseConfig(dpy, a, configs, config_size, num_config);
		free(a);
	}
	LOG("eglChooseConfig -> %d (%d configs)\n", r, num_config ? *num_config : -1);
	return r;
}

EXPORT EGLBoolean eglGetConfigAttrib(EGLDisplay dpy, EGLConfig config, EGLint attribute, EGLint *value)
{
	init();
	EGLBoolean r = real_eglGetConfigAttrib(dpy, config, attribute, value);

	if (backend == BACKEND_PBUFFER && r && attribute == EGL_SURFACE_TYPE && (*value & EGL_PBUFFER_BIT))
		*value |= EGL_WINDOW_BIT;
	return r;
}

static EGLSurface create_window(EGLDisplay dpy, EGLConfig config, void *native, const EGLint *attrib_list)
{
	struct fbdev_window *nw = native;
	struct win nwin = { .dpy = dpy, .interval = 1 };
	EGLSurface s;

	init();
	if (!fb_open()) {
		nwin.w = fb.var.xres;
		nwin.h = fb.var.yres;
	}
	if (nw && nw->width && nw->height) {
		nwin.w = nw->width;
		nwin.h = nw->height;
	}
	if (!nwin.w || !nwin.h) {
		nwin.w = 1024;
		nwin.h = 768;
	}

	if (backend == BACKEND_GBM) {
		EGLint visual = 0;

		real_eglGetConfigAttrib(dpy, config, EGL_NATIVE_VISUAL_ID, &visual);
		nwin.gbm_format = visual ? (uint32_t)visual : GBM_FORMAT_XRGB8888;
		nwin.gs = gbm.surface_create(gbm.dev, nwin.w, nwin.h, nwin.gbm_format, GBM_BO_USE_RENDERING);
		if (!nwin.gs) {
			LOG("gbm_surface_create(%dx%d fmt %08x) failed\n", nwin.w, nwin.h, nwin.gbm_format);
			return EGL_NO_SURFACE;
		}
		s = real_eglCreateWindowSurface(dpy, config, (EGLNativeWindowType)nwin.gs, attrib_list);
		if (s == EGL_NO_SURFACE) {
			gbm.surface_destroy(nwin.gs);
			return s;
		}
	} else {
		EGLint attrs[16];
		int o = 0;

		attrs[o++] = EGL_WIDTH;
		attrs[o++] = nwin.w;
		attrs[o++] = EGL_HEIGHT;
		attrs[o++] = nwin.h;
		for (int i = 0; attrib_list && attrib_list[i] != EGL_NONE && o < 12; i += 2) {
			if (attrib_list[i] == EGL_GL_COLORSPACE || attrib_list[i] == EGL_VG_COLORSPACE) {
				attrs[o++] = attrib_list[i];
				attrs[o++] = attrib_list[i + 1];
			}
		}
		attrs[o] = EGL_NONE;
		s = real_eglCreatePbufferSurface(dpy, config, attrs);
		if (s == EGL_NO_SURFACE)
			return s;
	}
	nwin.surf = s;
	LOG("window surface %dx%d -> %p\n", nwin.w, nwin.h, s);

	pthread_mutex_lock(&win_lock);
	for (int i = 0; i < MAX_WIN; i++) {
		if (wins[i].surf == EGL_NO_SURFACE) {
			wins[i] = nwin;
			break;
		}
	}
	pthread_mutex_unlock(&win_lock);
	return s;
}

EXPORT EGLSurface eglCreateWindowSurface(EGLDisplay dpy, EGLConfig config, EGLNativeWindowType win,
					 const EGLint *attrib_list)
{
	return create_window(dpy, config, (void *)win, attrib_list);
}

EXPORT EGLSurface eglCreatePlatformWindowSurface(EGLDisplay dpy, EGLConfig config, void *native,
						 const EGLAttrib *attrib_list)
{
	(void)attrib_list;
	return create_window(dpy, config, native, NULL);
}

EXPORT EGLSurface eglCreatePlatformWindowSurfaceEXT(EGLDisplay dpy, EGLConfig config, void *native,
						    const EGLint *attrib_list)
{
	return create_window(dpy, config, native, attrib_list);
}

EXPORT EGLBoolean eglDestroySurface(EGLDisplay dpy, EGLSurface surface)
{
	struct gbm_surface *gs = NULL;

	init();
	pthread_mutex_lock(&win_lock);
	struct win *w = find_win(surface);

	if (w) {
		gs = w->gs;
		memset(w, 0, sizeof(*w));
	}
	pthread_mutex_unlock(&win_lock);
	EGLBoolean r = real_eglDestroySurface(dpy, surface);

	if (gs)
		gbm.surface_destroy(gs);
	return r;
}

EXPORT EGLBoolean eglSwapBuffers(EGLDisplay dpy, EGLSurface surface)
{
	init();
	struct win *w = find_win(surface);
	EGLBoolean r;

	if (!w)
		return real_eglSwapBuffers(dpy, surface);
	r = backend == BACKEND_GBM ? present_gbm(w) : present_pbuffer(w);
	pace(w);
	return r;
}

EXPORT EGLBoolean eglSwapInterval(EGLDisplay dpy, EGLint interval)
{
	init();
	struct win *w = find_win(real_eglGetCurrentSurface(EGL_DRAW));

	if (w)
		w->interval = interval;
	/* Pacing is ours; keep Mesa from blocking as well. */
	real_eglSwapInterval(dpy, 0);
	return EGL_TRUE;
}

EXPORT EGLBoolean eglQuerySurface(EGLDisplay dpy, EGLSurface surface, EGLint attribute, EGLint *value)
{
	init();
	if (attribute == EGL_RENDER_BUFFER && find_win(surface)) {
		*value = EGL_BACK_BUFFER;
		return EGL_TRUE;
	}
	return real_eglQuerySurface(dpy, surface, attribute, value);
}

/* Pass-through for the rest of EGL 1.5. */
#include "egl_passthrough.inc"

static const struct {
	const char *name;
	void *fn;
} overrides[] = {
	{ "eglGetDisplay", (void *)eglGetDisplay },
	{ "eglChooseConfig", (void *)eglChooseConfig },
	{ "eglGetConfigAttrib", (void *)eglGetConfigAttrib },
	{ "eglCreateWindowSurface", (void *)eglCreateWindowSurface },
	{ "eglCreatePlatformWindowSurface", (void *)eglCreatePlatformWindowSurface },
	{ "eglCreatePlatformWindowSurfaceEXT", (void *)eglCreatePlatformWindowSurfaceEXT },
	{ "eglDestroySurface", (void *)eglDestroySurface },
	{ "eglSwapBuffers", (void *)eglSwapBuffers },
	{ "eglSwapInterval", (void *)eglSwapInterval },
	{ "eglQuerySurface", (void *)eglQuerySurface },
};

EXPORT __eglMustCastToProperFunctionPointerType eglGetProcAddress(const char *procname)
{
	init();
	for (size_t i = 0; procname && i < sizeof(overrides) / sizeof(overrides[0]); i++)
		if (!strcmp(procname, overrides[i].name))
			return (__eglMustCastToProperFunctionPointerType)overrides[i].fn;
	return real_eglGetProcAddress ? real_eglGetProcAddress(procname) : NULL;
}
