#include "ftp.h"
#include "util.h"
#include <winsock2.h>
#include <ws2tcpip.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <ctype.h>

#define CTRL_BUF 1024

static SOCKET tcp_connect(const char *host, uint16_t port) {
    struct addrinfo hints = {0}, *res = NULL;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    char pstr[8];
    snprintf(pstr, sizeof pstr, "%u", port);
    if (getaddrinfo(host, pstr, &hints, &res) != 0 || !res) return INVALID_SOCKET;
    SOCKET s = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (s == INVALID_SOCKET) { freeaddrinfo(res); return INVALID_SOCKET; }
    if (connect(s, res->ai_addr, (int)res->ai_addrlen) < 0) {
        closesocket(s); freeaddrinfo(res); return INVALID_SOCKET;
    }
    freeaddrinfo(res);
    return s;
}

/* Read one FTP reply line into buf (NUL terminated). Consumes bytes
 * through a '\n'. Returns the numeric code, or -1 on error. Handles
 * multi-line replies where first line is "NNN-" by continuing until
 * a line starts with "NNN " (space). */
static int ftp_read_reply(SOCKET s, char *buf, size_t cap) {
    char line[CTRL_BUF];
    size_t o = 0;
    int code = -1;
    for (;;) {
        size_t li = 0;
        for (;;) {
            char c;
            int r = recv(s, &c, 1, 0);
            if (r != 1) return -1;
            if (li + 1 < sizeof line) line[li++] = c;
            if (c == '\n') break;
        }
        line[li] = 0;
        if (o + li < cap) { memcpy(buf + o, line, li); o += li; buf[o] = 0; }
        if (li >= 4 && isdigit((unsigned char)line[0]) &&
            isdigit((unsigned char)line[1]) && isdigit((unsigned char)line[2])) {
            int c = (line[0]-'0')*100 + (line[1]-'0')*10 + (line[2]-'0');
            if (code < 0) code = c;
            if (line[3] == ' ' && c == code) return code;
        }
    }
}

static int ftp_send_cmd(SOCKET s, const char *fmt, ...) {
    char buf[CTRL_BUF];
    va_list ap; va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof buf - 3, fmt, ap);
    va_end(ap);
    if (n < 0) return -1;
    buf[n++] = '\r'; buf[n++] = '\n';
    return send(s, buf, n, 0) == n ? 0 : -1;
}

static int ftp_expect(SOCKET s, int want) {
    char rb[CTRL_BUF];
    int c = ftp_read_reply(s, rb, sizeof rb);
    if (c != want) {
        LOGW("ftp: expected %d got %d (%s)", want, c, rb);
        return -1;
    }
    return 0;
}

static int ftp_expect_range(SOCKET s, int lo, int hi, char *reply, size_t rcap) {
    int c = ftp_read_reply(s, reply, rcap);
    if (c < lo || c > hi) { LOGW("ftp: want %d..%d got %d", lo, hi, c); return -1; }
    return c;
}

static int ftp_login(SOCKET s, const char *user, const char *pass) {
    char rb[CTRL_BUF];
    if (ftp_read_reply(s, rb, sizeof rb) != 220) return -1;
    if (ftp_send_cmd(s, "USER %s", user ? user : "anonymous") < 0) return -1;
    int c = ftp_expect_range(s, 200, 399, rb, sizeof rb);
    if (c < 0) return -1;
    if (c == 331) {
        if (ftp_send_cmd(s, "PASS %s", pass ? pass : "") < 0) return -1;
        if (ftp_expect_range(s, 200, 299, rb, sizeof rb) < 0) return -1;
    }
    if (ftp_send_cmd(s, "TYPE I") < 0) return -1;
    if (ftp_expect(s, 200) < 0) return -1;
    return 0;
}

/* Parse PASV reply: 227 Entering Passive Mode (h1,h2,h3,h4,p1,p2). */
static int parse_pasv(const char *reply, char *ip_out, uint16_t *port_out) {
    const char *p = strchr(reply, '(');
    if (!p) return -1;
    unsigned h[6];
    if (sscanf(p + 1, "%u,%u,%u,%u,%u,%u", &h[0], &h[1], &h[2], &h[3], &h[4], &h[5]) != 6)
        return -1;
    snprintf(ip_out, 16, "%u.%u.%u.%u", h[0], h[1], h[2], h[3]);
    *port_out = (uint16_t)((h[4] << 8) | h[5]);
    return 0;
}

static SOCKET ftp_pasv_connect(SOCKET ctrl) {
    if (ftp_send_cmd(ctrl, "PASV") < 0) return INVALID_SOCKET;
    char rb[CTRL_BUF];
    if (ftp_read_reply(ctrl, rb, sizeof rb) != 227) return INVALID_SOCKET;
    char ip[16]; uint16_t port;
    if (parse_pasv(rb, ip, &port) < 0) return INVALID_SOCKET;
    return tcp_connect(ip, port);
}

int ftp_size(const char *host, uint16_t port,
             const char *user, const char *pass,
             const char *remote_path, uint64_t *out_size) {
    SOCKET ctrl = tcp_connect(host, port);
    if (ctrl == INVALID_SOCKET) return -1;
    int rv = -1;
    if (ftp_login(ctrl, user, pass) < 0) goto done;
    if (ftp_send_cmd(ctrl, "SIZE %s", remote_path) < 0) goto done;
    char rb[CTRL_BUF];
    int c = ftp_read_reply(ctrl, rb, sizeof rb);
    if (c != 213) goto done;
    const char *p = rb + 4;
    while (*p == ' ') p++;
    *out_size = strtoull(p, NULL, 10);
    ftp_send_cmd(ctrl, "QUIT");
    rv = 0;
done:
    closesocket(ctrl);
    return rv;
}

int ftp_download(const char *host, uint16_t port,
                 const char *user, const char *pass,
                 const char *remote_path, const char *local_path) {
    SOCKET ctrl = tcp_connect(host, port);
    if (ctrl == INVALID_SOCKET) { LOGE("ftp connect %s:%u failed", host, port); return -1; }
    int rv = -1;
    FILE *fp = NULL;
    SOCKET data = INVALID_SOCKET;
    if (ftp_login(ctrl, user, pass) < 0) goto done;
    data = ftp_pasv_connect(ctrl);
    if (data == INVALID_SOCKET) { LOGE("ftp PASV connect failed"); goto done; }
    if (ftp_send_cmd(ctrl, "RETR %s", remote_path) < 0) goto done;
    char rb[CTRL_BUF];
    int c = ftp_read_reply(ctrl, rb, sizeof rb);
    if (c < 100 || c >= 200) { LOGW("ftp RETR rejected: %d %s", c, rb); goto done; }
    fp = fopen(local_path, "wb");
    if (!fp) { LOGE("ftp: open local %s failed", local_path); goto done; }
    char buf[16384];
    for (;;) {
        int n = recv(data, buf, sizeof buf, 0);
        if (n == 0) break;
        if (n < 0) { LOGE("ftp data recv failed"); goto done; }
        if (fwrite(buf, 1, (size_t)n, fp) != (size_t)n) { LOGE("ftp local write failed"); goto done; }
    }
    closesocket(data); data = INVALID_SOCKET;
    if (ftp_read_reply(ctrl, rb, sizeof rb) / 100 != 2) goto done;
    ftp_send_cmd(ctrl, "QUIT");
    rv = 0;
done:
    if (data != INVALID_SOCKET) closesocket(data);
    if (fp) fclose(fp);
    closesocket(ctrl);
    if (rv < 0 && local_path) remove(local_path);
    return rv;
}

int ftp_upload(const char *host, uint16_t port,
               const char *user, const char *pass,
               const char *local_path, const char *remote_path) {
    SOCKET ctrl = tcp_connect(host, port);
    if (ctrl == INVALID_SOCKET) return -1;
    int rv = -1;
    FILE *fp = NULL;
    SOCKET data = INVALID_SOCKET;
    if (ftp_login(ctrl, user, pass) < 0) goto done;
    data = ftp_pasv_connect(ctrl);
    if (data == INVALID_SOCKET) goto done;
    if (ftp_send_cmd(ctrl, "STOR %s", remote_path) < 0) goto done;
    char rb[CTRL_BUF];
    int c = ftp_read_reply(ctrl, rb, sizeof rb);
    if (c < 100 || c >= 200) { LOGW("ftp STOR rejected: %d %s", c, rb); goto done; }
    fp = fopen(local_path, "rb");
    if (!fp) goto done;
    char buf[16384];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, fp)) > 0) {
        const char *p = buf; size_t left = n;
        while (left) {
            int s = send(data, p, (int)left, 0);
            if (s <= 0) goto done;
            p += s; left -= (size_t)s;
        }
    }
    closesocket(data); data = INVALID_SOCKET;
    if (ftp_read_reply(ctrl, rb, sizeof rb) / 100 != 2) goto done;
    ftp_send_cmd(ctrl, "QUIT");
    rv = 0;
done:
    if (data != INVALID_SOCKET) closesocket(data);
    if (fp) fclose(fp);
    closesocket(ctrl);
    return rv;
}
