#ifndef NISHRO_TFTP_H
#define NISHRO_TFTP_H

#include <stdint.h>
#include <stddef.h>

#define TFTP_RRQ    1
#define TFTP_WRQ    2
#define TFTP_DATA   3
#define TFTP_ACK    4
#define TFTP_ERROR  5
#define TFTP_OACK   6

#define TFTP_ERR_NOT_DEFINED     0
#define TFTP_ERR_FILE_NOT_FOUND  1
#define TFTP_ERR_ACCESS          2
#define TFTP_ERR_DISK_FULL       3
#define TFTP_ERR_ILLEGAL_OP      4
#define TFTP_ERR_UNKNOWN_TID     5
#define TFTP_ERR_EXISTS          6
#define TFTP_ERR_NO_USER         7
#define TFTP_ERR_OPT_NEG         8

#define TFTP_PORT               69

#define TFTP_BLKSIZE_MIN        8
#define TFTP_BLKSIZE_MAX        65464     /* RFC 2348 */
#define TFTP_BLKSIZE_DEFAULT    512

#define TFTP_WINDOWSIZE_MAX     64        /* RFC 7440: 1..65535; cap for sanity */
#define TFTP_WINDOWSIZE_DEFAULT 1

#define TFTP_TIMEOUT_MAX        255       /* seconds */
#define TFTP_TIMEOUT_DEFAULT    5

/* Parsed request. Filename and mode point into the packet buffer. */
typedef struct {
    int opcode;                          /* RRQ or WRQ */
    const char *filename;                /* NUL-terminated in buffer */
    const char *mode;
    int has_blksize;    uint32_t blksize;
    int has_windowsize; uint32_t windowsize;
    int has_timeout;    uint32_t timeout_sec;
    int has_tsize;      uint64_t tsize;
} TftpReq;

/* Parse a RRQ or WRQ payload (starting at the TFTP opcode). Returns 0
 * on success, -1 on malformed. Output fields point into buf. */
int tftp_parse_request(const uint8_t *buf, size_t len, TftpReq *out);

/* Build a DATA packet. Returns bytes written, 0 on overflow. */
size_t tftp_build_data(uint8_t *buf, size_t buf_cap,
                       uint16_t block, const uint8_t *payload, size_t len);

/* Build an ACK. */
size_t tftp_build_ack(uint8_t *buf, size_t buf_cap, uint16_t block);

/* Build an ERROR. msg may be NULL. */
size_t tftp_build_error(uint8_t *buf, size_t buf_cap,
                        uint16_t code, const char *msg);

/* Build an OACK with the fields for which has_* is set in TftpReq. */
size_t tftp_build_oack(uint8_t *buf, size_t buf_cap, const TftpReq *negotiated);

#endif
