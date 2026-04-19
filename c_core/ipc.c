#include "ipc.h"
#include "util.h"
#include <stdio.h>
#include <string.h>
#include <windows.h>
#include <io.h>

#define BUF_CAP  (1024 * 1024)

static char buf[BUF_CAP];
static size_t bi = 0;

void ipc_emit(const char *json) {
    fputs(json, stdout);
    fputc('\n', stdout);
    fflush(stdout);
}

/* Windows stdin does not have select(). We use PeekNamedPipe on the
 * handle for non-blocking reads. Works for both anonymous pipes (our
 * case when spawned by Python) and console. */
int ipc_poll(ipc_line_cb cb, void *ud) {
    HANDLE h = GetStdHandle(STD_INPUT_HANDLE);
    if (h == INVALID_HANDLE_VALUE || h == NULL) return -1;

    DWORD avail = 0;
    if (!PeekNamedPipe(h, NULL, 0, NULL, &avail, NULL)) {
        /* Peek failed -- likely pipe closed (parent exited). */
        return -1;
    }
    while (avail > 0) {
        DWORD want = avail;
        if (want > (DWORD)(BUF_CAP - bi)) want = (DWORD)(BUF_CAP - bi);
        if (!want) {
            /* Buffer full without a newline -- discard oldest half. */
            memmove(buf, buf + BUF_CAP / 2, BUF_CAP / 2);
            bi = BUF_CAP / 2;
            continue;
        }
        DWORD got = 0;
        if (!ReadFile(h, buf + bi, want, &got, NULL) || got == 0) return -1;
        bi += got;
        avail -= got;

        /* Process any complete lines. */
        size_t start = 0;
        for (size_t i = 0; i < bi; i++) {
            if (buf[i] == '\n') {
                size_t end = i;
                while (end > start && (buf[end - 1] == '\r')) end--;
                cb(buf + start, end - start, ud);
                start = i + 1;
            }
        }
        if (start) {
            memmove(buf, buf + start, bi - start);
            bi -= start;
        }
    }
    return 0;
}
