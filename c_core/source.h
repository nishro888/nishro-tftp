#ifndef NISHRO_SOURCE_H
#define NISHRO_SOURCE_H

#include <stdint.h>
#include <stddef.h>

/* A file source abstracts "where does this filename's content come
 * from". For TFTP RRQ we need sequential read; for WRQ we need
 * sequential write + a finalize step (which may upload to FTP). */

typedef struct FileSource FileSource;

FileSource *src_open_read(const char *filename, uint64_t *out_size);
FileSource *src_open_write(const char *filename);

/* Read next chunk. Returns bytes read, 0 on EOF, -1 on error. */
int src_read(FileSource *s, void *buf, size_t max);

/* Seek to byte offset (RRQ retransmission support). */
int src_seek(FileSource *s, uint64_t offset);

int src_write(FileSource *s, const void *buf, size_t len);

/* Commit (for WRQ): flush, close local, upload to FTP if f::/ftp://
 * destination. Returns 0 on success, -1 on error. Frees the source. */
int src_commit_write(FileSource *s);

/* Abort (for RRQ close or WRQ cancel). Frees the source. */
void src_close(FileSource *s);

#endif
