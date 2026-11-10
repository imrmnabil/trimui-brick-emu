/*
 * brick-agent: command channel for the host tools (the manager).
 *
 *   brick-agent /dev/hvc2
 *
 * Puts the virtio console into raw mode and feeds it, through a pipe, to a
 * NON-interactive /bin/sh whose output goes back to the console: no prompt,
 * no echo, no line editor (busybox ash's line editor truncates long lines and
 * can stop reading after that), so commands of any length are safe. The shell
 * is restarted if it exits.
 */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	if (argc != 2) {
		fprintf(stderr, "usage: brick-agent TTY\n");
		return 2;
	}
	int tty = open(argv[1], O_RDWR | O_NOCTTY);
	if (tty < 0)
		return perror(argv[1]), 1;
	struct termios t;
	if (tcgetattr(tty, &t) == 0) {
		cfmakeraw(&t);
		tcsetattr(tty, TCSANOW, &t);
	}
	if (fork() > 0)
		return 0;   /* daemonize */
	setsid();
	signal(SIGHUP, SIG_IGN);
	signal(SIGPIPE, SIG_IGN);
	for (;;) {
		int p[2];
		if (pipe(p))
			return 1;
		pid_t sh = fork();
		if (sh == 0) {
			dup2(p[0], 0);
			dup2(tty, 1);
			dup2(tty, 2);
			close(p[0]);
			close(p[1]);
			close(tty);
			execl("/bin/sh", "sh", (char *)0);
			_exit(127);
		}
		close(p[0]);
		char buf[4096];
		for (;;) {
			ssize_t n = read(tty, buf, sizeof(buf));
			if (n < 0 && errno == EINTR)
				continue;
			if (n <= 0) {
				sleep(1);   /* no host connected */
				continue;
			}
			if (write(p[1], buf, n) != n)
				break;      /* the shell went away */
			if (waitpid(sh, NULL, WNOHANG) == sh)
				break;
		}
		close(p[1]);
		waitpid(sh, NULL, 0);
	}
}
