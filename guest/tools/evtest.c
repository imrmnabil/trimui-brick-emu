/* evtest: list input devices or dump events.  evtest [/dev/input/eventN] */
#include <dirent.h>
#include <fcntl.h>
#include <linux/input.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	char name[256];

	if (argc < 2) {
		for (int i = 0; i < 32; i++) {
			char p[64];

			snprintf(p, sizeof(p), "/dev/input/event%d", i);
			int fd = open(p, O_RDONLY);
			if (fd < 0)
				continue;
			name[0] = 0;
			ioctl(fd, EVIOCGNAME(sizeof(name)), name);
			printf("%s: %s\n", p, name);
			close(fd);
		}
		return 0;
	}
	int fd = open(argv[1], O_RDONLY);
	if (fd < 0) {
		perror(argv[1]);
		return 1;
	}
	ioctl(fd, EVIOCGNAME(sizeof(name)), name);
	printf("# %s\n", name);
	fflush(stdout);
	struct input_event ev;
	while (read(fd, &ev, sizeof(ev)) == sizeof(ev)) {
		if (ev.type == EV_SYN)
			continue;
		printf("type %d code %d value %d\n", ev.type, ev.code, ev.value);
		fflush(stdout);
	}
	return 0;
}
