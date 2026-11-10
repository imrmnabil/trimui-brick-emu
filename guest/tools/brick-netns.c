/*
 * brick-netns: minimal network-namespace helper for the emulator's virtual
 * Wi-Fi router (the firmware's busybox has no netns support).
 *
 *   brick-netns new                 create a namespace, held by a background
 *                                   process; prints that process's pid
 *   brick-netns move IFNAME PID     move a network interface into PID's namespace
 *   brick-netns exec PID CMD...     run CMD inside PID's network namespace
 *   brick-netns rfkill-guard IDX [CMD...]
 *                                   keep rfkill IDX unblocked (daemon) and run
 *                                   CMD after it had been blocked (a blocked
 *                                   radio's interfaces are closed by cfg80211)
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/if_link.h>
#include <linux/netlink.h>
#include <linux/rfkill.h>
#include <linux/rtnetlink.h>
#include <net/if.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static int usage(void)
{
	fprintf(stderr, "usage: brick-netns new | move IFNAME PID | exec PID CMD...\n");
	return 2;
}

static int cmd_new(void)
{
	int fd[2];
	if (pipe(fd))
		return perror("pipe"), 1;
	pid_t pid = fork();
	if (pid < 0)
		return perror("fork"), 1;
	if (pid == 0) {
		close(fd[0]);
		setsid();
		if (unshare(CLONE_NEWNET)) {
			perror("unshare(CLONE_NEWNET)");
			_exit(1);
		}
		if (write(fd[1], "k", 1) != 1)
			_exit(1);
		close(fd[1]);
		int nul = open("/dev/null", O_RDWR);
		if (nul >= 0) {
			dup2(nul, 0);
			dup2(nul, 1);
			dup2(nul, 2);
		}
		signal(SIGHUP, SIG_IGN);
		for (;;)
			pause();
	}
	close(fd[1]);
	char c;
	if (read(fd[0], &c, 1) != 1)
		return fprintf(stderr, "namespace creation failed\n"), 1;
	printf("%d\n", pid);
	return 0;
}

static int cmd_move(const char *ifname, int pid)
{
	unsigned idx = if_nametoindex(ifname);
	if (!idx)
		return fprintf(stderr, "%s: no such interface\n", ifname), 1;
	int s = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
	if (s < 0)
		return perror("netlink"), 1;
	struct {
		struct nlmsghdr nh;
		struct ifinfomsg ifi;
		char attrs[64];
	} req;
	memset(&req, 0, sizeof(req));
	req.nh.nlmsg_type = RTM_NEWLINK;
	req.nh.nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK;
	req.ifi.ifi_family = AF_UNSPEC;
	req.ifi.ifi_index = idx;
	struct rtattr *rta = (struct rtattr *)req.attrs;
	rta->rta_type = IFLA_NET_NS_PID;
	rta->rta_len = RTA_LENGTH(sizeof(__u32));
	*(__u32 *)RTA_DATA(rta) = pid;
	req.nh.nlmsg_len = NLMSG_LENGTH(sizeof(req.ifi)) + RTA_ALIGN(rta->rta_len);
	if (send(s, &req, req.nh.nlmsg_len, 0) < 0)
		return perror("send"), 1;
	char buf[512];
	int n = recv(s, buf, sizeof(buf), 0);
	if (n < 0)
		return perror("recv"), 1;
	struct nlmsghdr *h = (struct nlmsghdr *)buf;
	if (h->nlmsg_type == NLMSG_ERROR) {
		int err = ((struct nlmsgerr *)NLMSG_DATA(h))->error;
		if (err)
			return fprintf(stderr, "move %s: %s\n", ifname, strerror(-err)), 1;
	}
	return 0;
}

static int cmd_exec(int pid, char **argv)
{
	char path[64];
	snprintf(path, sizeof(path), "/proc/%d/ns/net", pid);
	int fd = open(path, O_RDONLY);
	if (fd < 0)
		return perror(path), 1;
	if (setns(fd, CLONE_NEWNET))
		return perror("setns"), 1;
	close(fd);
	execvp(argv[0], argv);
	perror(argv[0]);
	return 127;
}

static int rfkill_unblock(int fd, unsigned idx)
{
	struct rfkill_event ev;
	memset(&ev, 0, sizeof(ev));
	ev.idx = idx;
	ev.op = RFKILL_OP_CHANGE;
	ev.soft = 0;
	return write(fd, &ev, RFKILL_EVENT_SIZE_V1) == RFKILL_EVENT_SIZE_V1 ? 0 : -1;
}

static int cmd_rfkill_guard(unsigned idx, char **hook)
{
	int fd = open("/dev/rfkill", O_RDWR);
	if (fd < 0)
		return perror("/dev/rfkill"), 1;
	if (rfkill_unblock(fd, idx))
		return perror("rfkill unblock"), 1;
	if (fork() > 0)
		return 0;
	setsid();
	signal(SIGHUP, SIG_IGN);
	signal(SIGCHLD, SIG_IGN);
	for (;;) {
		struct rfkill_event ev;
		ssize_t n = read(fd, &ev, sizeof(ev));
		if (n < (ssize_t)RFKILL_EVENT_SIZE_V1) {
			if (n < 0 && errno == EINTR)
				continue;
			break;
		}
		if (ev.idx == idx && ev.op != RFKILL_OP_DEL && ev.soft) {
			rfkill_unblock(fd, idx);
			if (hook[0] && fork() == 0) {
				execvp(hook[0], hook);
				_exit(127);
			}
		}
		if (ev.idx == idx && ev.op == RFKILL_OP_DEL)
			break;
	}
	return 0;
}

int main(int argc, char **argv)
{
	if (argc >= 3 && !strcmp(argv[1], "rfkill-guard"))
		return cmd_rfkill_guard(atoi(argv[2]), argv + 3);
	if (argc >= 2 && !strcmp(argv[1], "new"))
		return cmd_new();
	if (argc == 4 && !strcmp(argv[1], "move"))
		return cmd_move(argv[2], atoi(argv[3]));
	if (argc >= 4 && !strcmp(argv[1], "exec"))
		return cmd_exec(atoi(argv[2]), argv + 3);
	return usage();
}
