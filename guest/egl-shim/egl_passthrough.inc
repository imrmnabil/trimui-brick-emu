/* EGL 1.5 entry points forwarded to Mesa unchanged. */
#define FWD(ret, name, params, args)                                       \
	EXPORT ret name params                                             \
	{                                                                  \
		static __typeof__(&name) fp;                               \
		init();                                                    \
		if (!fp)                                                   \
			fp = (__typeof__(fp))sym(#name);                   \
		return fp args;                                            \
	}

FWD(EGLBoolean, eglBindAPI, (EGLenum api), (api))
FWD(EGLBoolean, eglBindTexImage, (EGLDisplay d, EGLSurface s, EGLint b), (d, s, b))
FWD(EGLint, eglClientWaitSync, (EGLDisplay d, EGLSync s, EGLint f, EGLTime t), (d, s, f, t))
FWD(EGLBoolean, eglCopyBuffers, (EGLDisplay d, EGLSurface s, EGLNativePixmapType p), (d, s, p))
FWD(EGLContext, eglCreateContext, (EGLDisplay d, EGLConfig c, EGLContext sc, const EGLint *a), (d, c, sc, a))
FWD(EGLImage, eglCreateImage, (EGLDisplay d, EGLContext c, EGLenum t, EGLClientBuffer b, const EGLAttrib *a), (d, c, t, b, a))
FWD(EGLSurface, eglCreatePbufferFromClientBuffer, (EGLDisplay d, EGLenum t, EGLClientBuffer b, EGLConfig c, const EGLint *a), (d, t, b, c, a))
FWD(EGLSurface, eglCreatePbufferSurface, (EGLDisplay d, EGLConfig c, const EGLint *a), (d, c, a))
FWD(EGLSurface, eglCreatePixmapSurface, (EGLDisplay d, EGLConfig c, EGLNativePixmapType p, const EGLint *a), (d, c, p, a))
FWD(EGLSurface, eglCreatePlatformPixmapSurface, (EGLDisplay d, EGLConfig c, void *p, const EGLAttrib *a), (d, c, p, a))
FWD(EGLSync, eglCreateSync, (EGLDisplay d, EGLenum t, const EGLAttrib *a), (d, t, a))
FWD(EGLBoolean, eglDestroyContext, (EGLDisplay d, EGLContext c), (d, c))
FWD(EGLBoolean, eglDestroyImage, (EGLDisplay d, EGLImage i), (d, i))
FWD(EGLBoolean, eglDestroySync, (EGLDisplay d, EGLSync s), (d, s))
FWD(EGLBoolean, eglGetConfigs, (EGLDisplay d, EGLConfig *c, EGLint n, EGLint *num), (d, c, n, num))
FWD(EGLContext, eglGetCurrentContext, (void), ())
FWD(EGLDisplay, eglGetCurrentDisplay, (void), ())
FWD(EGLSurface, eglGetCurrentSurface, (EGLint r), (r))
FWD(EGLint, eglGetError, (void), ())
FWD(EGLDisplay, eglGetPlatformDisplay, (EGLenum p, void *n, const EGLAttrib *a), (p, n, a))
FWD(EGLBoolean, eglGetSyncAttrib, (EGLDisplay d, EGLSync s, EGLint a, EGLAttrib *v), (d, s, a, v))
FWD(EGLBoolean, eglInitialize, (EGLDisplay d, EGLint *ma, EGLint *mi), (d, ma, mi))
FWD(EGLBoolean, eglMakeCurrent, (EGLDisplay d, EGLSurface dr, EGLSurface rd, EGLContext c), (d, dr, rd, c))
FWD(EGLenum, eglQueryAPI, (void), ())
FWD(EGLBoolean, eglQueryContext, (EGLDisplay d, EGLContext c, EGLint a, EGLint *v), (d, c, a, v))
FWD(const char *, eglQueryString, (EGLDisplay d, EGLint n), (d, n))
FWD(EGLBoolean, eglReleaseTexImage, (EGLDisplay d, EGLSurface s, EGLint b), (d, s, b))
FWD(EGLBoolean, eglReleaseThread, (void), ())
FWD(EGLBoolean, eglSurfaceAttrib, (EGLDisplay d, EGLSurface s, EGLint a, EGLint v), (d, s, a, v))
FWD(EGLBoolean, eglTerminate, (EGLDisplay d), (d))
FWD(EGLBoolean, eglWaitClient, (void), ())
FWD(EGLBoolean, eglWaitGL, (void), ())
FWD(EGLBoolean, eglWaitNative, (EGLint e), (e))
FWD(EGLBoolean, eglWaitSync, (EGLDisplay d, EGLSync s, EGLint f), (d, s, f))
