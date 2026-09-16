#include <alchemy/task.h>
#include <cobalt/uapi/kernel/thread.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>

#ifndef CONFIG_XENO_COBALT
#error "This example requires the Xenomai Cobalt core"
#endif

enum { TASK_PRIORITY = 50 };

static void cobalt_task(void *argument)
{
    int *result = argument;
    RT_TASK_INFO info;

    *result = rt_task_set_mode(0, T_CONFORMING, NULL);
    if (*result != 0)
        return;
    *result = rt_task_inquire(NULL, &info);
    if (*result != 0)
        return;
    /* XNRELAX means the task is in Linux's secondary execution domain. */
    if ((info.stat.status & XNRELAX) || info.prio != TASK_PRIORITY)
        *result = -EPROTO;
}

static int failed(const char *operation, int error)
{
    fprintf(stderr, "%s: %s (%d)\n", operation, strerror(-error), error);
    return 1;
}

int main(void)
{
    RT_TASK task;
    int result = -EINPROGRESS;
    int ret;

    if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0) {
        perror("mlockall");
        return 1;
    }
    ret = rt_task_create(&task, "cobalt-example", 0, TASK_PRIORITY, T_JOINABLE);
    if (ret != 0)
        return failed("rt_task_create", ret);
    ret = rt_task_start(&task, cobalt_task, &result);
    if (ret != 0) {
        rt_task_delete(&task);
        return failed("rt_task_start", ret);
    }
    ret = rt_task_join(&task);
    if (ret != 0)
        return failed("rt_task_join", ret);
    if (result != 0)
        return failed("Cobalt primary-mode task", result);

    puts("Cobalt task completed in primary mode at priority 50");
    return 0;
}
