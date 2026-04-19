#ifndef NISHRO_UTIL_H
#define NISHRO_UTIL_H

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>

#define LOG_DEBUG 0
#define LOG_INFO  1
#define LOG_WARN  2
#define LOG_ERROR 3

extern int g_log_level;

void log_msg(int level, const char *fmt, ...);
#define LOGD(...) do { if (g_log_level <= LOG_DEBUG) log_msg(LOG_DEBUG, __VA_ARGS__); } while (0)
#define LOGI(...) log_msg(LOG_INFO,  __VA_ARGS__)
#define LOGW(...) log_msg(LOG_WARN,  __VA_ARGS__)
#define LOGE(...) log_msg(LOG_ERROR, __VA_ARGS__)

uint64_t now_ms(void);

/* Internet checksum (RFC 1071) over a buffer. */
uint16_t inet_csum(const void *data, size_t len);

/* UDP checksum with pseudo-header. */
uint16_t udp_csum(uint32_t src_ip_be, uint32_t dst_ip_be,
                  const void *udp_hdr_and_payload, size_t len);

/* MAC address helpers. */
int parse_mac(const char *s, uint8_t out[6]);
void mac_to_str(const uint8_t mac[6], char out[18]);

/* IPv4 helpers. */
int parse_ipv4(const char *s, uint32_t *out_be);
void ipv4_to_str(uint32_t ip_be, char out[16]);

/* Safe string copy. */
size_t str_copy(char *dst, const char *src, size_t dst_size);

#endif
