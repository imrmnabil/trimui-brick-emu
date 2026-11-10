/* gltest: SDL2 + GLES2 smoke test for the emulator display path.
 * Run in the guest:  LD_LIBRARY_PATH=/usr/trimui/lib /brick-emu/bin/gltest [seconds]
 * Shows animated colour bars and prints the GL renderer and frame rate. */
#include <SDL2/SDL.h>
#include <GLES2/gl2.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
	int secs = argc > 1 ? atoi(argv[1]) : 5;

	if (SDL_Init(SDL_INIT_VIDEO) < 0) {
		fprintf(stderr, "SDL_Init: %s\n", SDL_GetError());
		return 1;
	}
	SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_ES);
	SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 2);
	SDL_Window *w = SDL_CreateWindow("gltest", 0, 0, 1024, 768, SDL_WINDOW_OPENGL | SDL_WINDOW_SHOWN);
	if (!w) {
		fprintf(stderr, "SDL_CreateWindow: %s\n", SDL_GetError());
		return 1;
	}
	SDL_GLContext c = SDL_GL_CreateContext(w);
	if (!c) {
		fprintf(stderr, "SDL_GL_CreateContext: %s\n", SDL_GetError());
		return 1;
	}
	SDL_GL_SetSwapInterval(1);
	printf("video driver: %s\nGL_RENDERER: %s\nGL_VERSION: %s\n", SDL_GetCurrentVideoDriver(),
	       glGetString(GL_RENDERER), glGetString(GL_VERSION));

	Uint32 start = SDL_GetTicks(), last = start;
	int frames = 0;
	glEnable(GL_SCISSOR_TEST);
	while (SDL_GetTicks() - start < (Uint32)secs * 1000) {
		float t = (SDL_GetTicks() - start) / 1000.0f;
		for (int i = 0; i < 8; i++) {
			glScissor(i * 128, 0, 128, 768);
			glClearColor(0.5f + 0.5f * sinf(t + i), 0.5f + 0.5f * sinf(t * 1.3f + i * 2), 0.5f + 0.5f * sinf(t * 0.7f + i * 3), 1);
			glClear(GL_COLOR_BUFFER_BIT);
		}
		SDL_GL_SwapWindow(w);
		frames++;
		if (SDL_GetTicks() - last >= 1000) {
			printf("%d fps\n", frames);
			frames = 0;
			last = SDL_GetTicks();
		}
	}
	SDL_Quit();
	return 0;
}
