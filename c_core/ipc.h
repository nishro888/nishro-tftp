#ifndef NISHRO_IPC_H
#define NISHRO_IPC_H

#include <stdint.h>
#include <stddef.h>

/* Poll stdin for incoming JSON lines (non-blocking). On each complete
 * line the callback is invoked with the (buf,len) pointing into an
 * internal buffer that's valid only for the duration of the callback.
 * Returns -1 if stdin closed (parent died -> we should exit). */
typedef void (*ipc_line_cb)(const char *line, size_t len, void *ud);
int ipc_poll(ipc_line_cb cb, void *ud);

/* Emit a JSON line on stdout. Adds trailing newline + flushes. */
void ipc_emit(const char *json);

#endif
