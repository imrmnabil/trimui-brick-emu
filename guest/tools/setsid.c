/* setsid: run a program in a new session (no controlling terminal). */
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	if (argc < 2) {
		fprintf(stderr, "usage: setsid PROGRAM [ARGS...]\n");
		return 1;
	}
	pid_t pid = fork();

	if (pid < 0)
		return 1;
	if (pid > 0) {	/* wait so callers can sequence commands */
		int st;

		waitpid(pid, &st, 0);
		return WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
	}
	setsid();
	execvp(argv[1], argv + 1);
	perror(argv[1]);
	return 127;
}
