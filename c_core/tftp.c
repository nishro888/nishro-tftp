#include "tftp.h"
#include "util.h"
#include <string.h>
#include <stdlib.h>
#include <ctype.h>

#define RD16(p) ((uint16_t)(((p)[0] << 8) | (p)[1]))
static void wr16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }

/* Return pointer to next NUL-terminated C string in buf starting at *pos.
 * Advances *pos past the NUL. Returns NULL if the buffer is exhausted
 * without a terminator. */
static const char *take_zstr(const uint8_t *buf, size_t len, size_t *pos) {
    size_t start = *pos;
    while (*pos < len && buf[*pos]) (*pos)++;
    if (*pos >= len) return NULL;
    const char *s = (const char *)buf + start;
    (*pos)++;              /* skip NUL */
    return s;
}

static int case_eq(const char *a, const char *b) {
    while (*a && *b) {
        int ca = tolower((unsigned char)*a++);
        int cb = tolower((unsigned char)*b++);
        if (ca != cb) return 0;
    }
    return *a == *b;
}

int tftp_parse_request(const uint8_t *buf, size_t len, TftpReq *o) {
    if (len < 4) return -1;
    memset(o, 0, sizeof(*o));
    uint16_t op = RD16(buf);
    if (op != TFTP_RRQ && op != TFTP_WRQ) return -1;
    o->opcode = op;
    size_t pos = 2;
    const char *fn = take_zstr(buf, len, &pos);
    const char *md = take_zstr(buf, len, &pos);
    if (!fn || !md) return -1;
    o->filename = fn;
    o->mode = md;
    /* Remaining: zero or more (opt,val) NUL-delimited pairs. */
    while (pos < len) {
        const char *k = take_zstr(buf, len, &pos);
        const char *v = take_zstr(buf, len, &pos);
        if (!k || !v) break;
        if (case_eq(k, "blksize")) {
            long x = strtol(v, NULL, 10);
            if (x >= TFTP_BLKSIZE_MIN && x <= TFTP_BLKSIZE_MAX) {
                o->has_blksize = 1;
                o->blksize = (uint32_t)x;
            }
        } else if (case_eq(k, "windowsize")) {
            long x = strtol(v, NULL, 10);
            if (x >= 1 && x <= TFTP_WINDOWSIZE_MAX) {
                o->has_windowsize = 1;
                o->windowsize = (uint32_t)x;
            }
        } else if (case_eq(k, "timeout")) {
            long x = strtol(v, NULL, 10);
            if (x >= 1 && x <= TFTP_TIMEOUT_MAX) {
                o->has_timeout = 1;
                o->timeout_sec = (uint32_t)x;
            }
        } else if (case_eq(k, "tsize")) {
            long long x = strtoll(v, NULL, 10);
            if (x >= 0) {
                o->has_tsize = 1;
                o->tsize = (uint64_t)x;
            }
        }
    }
    return 0;
}

size_t tftp_build_data(uint8_t *buf, size_t cap,
                       uint16_t block, const uint8_t *payload, size_t len) {
    if (cap < 4 + len) return 0;
    wr16(buf, TFTP_DATA);
    wr16(buf + 2, block);
    if (len) memcpy(buf + 4, payload, len);
    return 4 + len;
}

size_t tftp_build_ack(uint8_t *buf, size_t cap, uint16_t block) {
    if (cap < 4) return 0;
    wr16(buf, TFTP_ACK);
    wr16(buf + 2, block);
    return 4;
}

size_t tftp_build_error(uint8_t *buf, size_t cap,
                        uint16_t code, const char *msg) {
    const char *m = msg ? msg : "";
    size_t mlen = strlen(m);
    if (cap < 4 + mlen + 1) return 0;
    wr16(buf, TFTP_ERROR);
    wr16(buf + 2, code);
    memcpy(buf + 4, m, mlen);
    buf[4 + mlen] = 0;
    return 4 + mlen + 1;
}

static size_t write_opt(uint8_t *buf, size_t cap, size_t off,
                        const char *key, const char *val) {
    size_t kl = strlen(key) + 1;
    size_t vl = strlen(val) + 1;
    if (off + kl + vl > cap) return 0;
    memcpy(buf + off, key, kl); off += kl;
    memcpy(buf + off, val, vl); off += vl;
    return off;
}

size_t tftp_build_oack(uint8_t *buf, size_t cap, const TftpReq *n) {
    if (cap < 2) return 0;
    wr16(buf, TFTP_OACK);
    size_t off = 2;
    char tmp[32];
    if (n->has_blksize) {
        snprintf(tmp, sizeof tmp, "%u", (unsigned)n->blksize);
        size_t r = write_opt(buf, cap, off, "blksize", tmp);
        if (!r) return 0; off = r;
    }
    if (n->has_windowsize) {
        snprintf(tmp, sizeof tmp, "%u", (unsigned)n->windowsize);
        size_t r = write_opt(buf, cap, off, "windowsize", tmp);
        if (!r) return 0; off = r;
    }
    if (n->has_timeout) {
        snprintf(tmp, sizeof tmp, "%u", (unsigned)n->timeout_sec);
        size_t r = write_opt(buf, cap, off, "timeout", tmp);
        if (!r) return 0; off = r;
    }
    if (n->has_tsize) {
        snprintf(tmp, sizeof tmp, "%llu", (unsigned long long)n->tsize);
        size_t r = write_opt(buf, cap, off, "tsize", tmp);
        if (!r) return 0; off = r;
    }
    return off;
}
