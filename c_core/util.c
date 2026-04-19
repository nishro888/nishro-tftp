#include "util.h"
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>

int g_log_level = LOG_INFO;

static const char *level_name(int lv) {
    switch (lv) {
    case LOG_DEBUG: return "DEBUG";
    case LOG_INFO:  return "INFO";
    case LOG_WARN:  return "WARN";
    case LOG_ERROR: return "ERROR";
    default:        return "?";
    }
}

void log_msg(int level, const char *fmt, ...) {
    if (level < g_log_level) return;
    SYSTEMTIME st;
    GetLocalTime(&st);
    fprintf(stderr, "%04d-%02d-%02d %02d:%02d:%02d.%03d %-5s ",
            st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond,
            st.wMilliseconds, level_name(level));
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    fflush(stderr);
}

uint64_t now_ms(void) {
    /* Return ms since the Unix epoch (1970-01-01 UTC) so values line
     * up with Python's time.time()*1000 on the other side of IPC.
     * FILETIME counts 100-ns ticks since 1601-01-01; subtract the
     * 11644473600-second offset before converting. */
    FILETIME ft;
    GetSystemTimeAsFileTime(&ft);
    ULARGE_INTEGER u = { .LowPart = ft.dwLowDateTime, .HighPart = ft.dwHighDateTime };
    return (uint64_t)((u.QuadPart - 116444736000000000ULL) / 10000ULL);
}

uint16_t inet_csum(const void *data, size_t len) {
    const uint8_t *p = data;
    uint32_t sum = 0;
    while (len > 1) { sum += (uint16_t)((p[0] << 8) | p[1]); p += 2; len -= 2; }
    if (len) sum += (uint16_t)(p[0] << 8);
    while (sum >> 16) sum = (sum & 0xFFFF) + (sum >> 16);
    return (uint16_t)~sum;
}

uint16_t udp_csum(uint32_t src_ip_be, uint32_t dst_ip_be,
                  const void *udp_hdr_and_payload, size_t len) {
    uint32_t sum = 0;
    const uint8_t *p = (const uint8_t *)&src_ip_be;
    sum += (p[0] << 8) | p[1]; sum += (p[2] << 8) | p[3];
    p = (const uint8_t *)&dst_ip_be;
    sum += (p[0] << 8) | p[1]; sum += (p[2] << 8) | p[3];
    sum += 17;                /* protocol = UDP */
    sum += (uint16_t)len;     /* UDP length */
    p = udp_hdr_and_payload;
    size_t n = len;
    while (n > 1) { sum += (uint16_t)((p[0] << 8) | p[1]); p += 2; n -= 2; }
    if (n) sum += (uint16_t)(p[0] << 8);
    while (sum >> 16) sum = (sum & 0xFFFF) + (sum >> 16);
    uint16_t c = (uint16_t)~sum;
    return c == 0 ? 0xFFFF : c;
}

int parse_mac(const char *s, uint8_t out[6]) {
    unsigned int b[6];
    int n = sscanf(s, "%x:%x:%x:%x:%x:%x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]);
    if (n != 6) n = sscanf(s, "%x-%x-%x-%x-%x-%x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]);
    if (n != 6) return -1;
    for (int i = 0; i < 6; i++) { if (b[i] > 0xFF) return -1; out[i] = (uint8_t)b[i]; }
    return 0;
}

void mac_to_str(const uint8_t mac[6], char out[18]) {
    snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

int parse_ipv4(const char *s, uint32_t *out_be) {
    unsigned int a, b, c, d;
    if (sscanf(s, "%u.%u.%u.%u", &a, &b, &c, &d) != 4) return -1;
    if (a > 255 || b > 255 || c > 255 || d > 255) return -1;
    uint8_t *p = (uint8_t *)out_be;
    p[0] = (uint8_t)a; p[1] = (uint8_t)b; p[2] = (uint8_t)c; p[3] = (uint8_t)d;
    return 0;
}

void ipv4_to_str(uint32_t ip_be, char out[16]) {
    const uint8_t *p = (const uint8_t *)&ip_be;
    snprintf(out, 16, "%u.%u.%u.%u", p[0], p[1], p[2], p[3]);
}

size_t str_copy(char *dst, const char *src, size_t dst_size) {
    if (!dst_size) return 0;
    size_t i = 0;
    for (; i + 1 < dst_size && src[i]; i++) dst[i] = src[i];
    dst[i] = '\0';
    return i;
}
