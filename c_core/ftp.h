#ifndef NISHRO_FTP_H
#define NISHRO_FTP_H

#include <stdint.h>
#include <stddef.h>

/* Minimal synchronous FTP client -- control+data, passive mode only,
 * binary transfers, no TLS. Used for RFC 959 fetch/store against the
 * configured FTP server. Blocking calls -- only suitable for use inside
 * a session worker or a short staging step (RRQ prefetch / WRQ upload).
 * For high-throughput scenarios we rely on the TFTP path being the
 * bottleneck, not the FTP side. */

int ftp_download(const char *host, uint16_t port,
                 const char *user, const char *pass,
                 const char *remote_path, const char *local_path);

int ftp_upload  (const char *host, uint16_t port,
                 const char *user, const char *pass,
                 const char *local_path, const char *remote_path);

int ftp_size    (const char *host, uint16_t port,
                 const char *user, const char *pass,
                 const char *remote_path, uint64_t *out_size);

#endif
