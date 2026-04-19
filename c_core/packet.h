#ifndef NISHRO_PACKET_H
#define NISHRO_PACKET_H

#include <stdint.h>
#include <stddef.h>

#define ETH_P_IP    0x0800
#define ETH_P_ARP   0x0806
#define ETH_P_VLAN  0x8100

#define IP_P_ICMP   1
#define IP_P_UDP    17

/* Parsed packet view -- all fields filled in by packet_parse().
 * Pointers into the original buffer; do not modify the source while
 * the view is in use. */
typedef struct {
    const uint8_t *raw;
    size_t raw_len;

    uint8_t  src_mac[6];
    uint8_t  dst_mac[6];

    int      has_vlan;       /* 1 if 802.1Q tag present */
    uint16_t vlan_id;        /* 0-4094 */
    uint16_t vlan_prio;      /* 0-7 */
    uint16_t eth_type;       /* inner ethertype if VLAN, else outer */

    /* IPv4 view (only if eth_type == IP). */
    int      is_ipv4;
    const uint8_t *ip_hdr;
    size_t   ip_hdr_len;
    uint8_t  ip_proto;
    uint32_t src_ip_be;
    uint32_t dst_ip_be;

    /* UDP view. */
    int      is_udp;
    uint16_t src_port;       /* host order */
    uint16_t dst_port;       /* host order */
    const uint8_t *udp_payload;
    size_t   udp_payload_len;

    /* ICMP view (proto == ICMP). */
    int      is_icmp;
    uint8_t  icmp_type;
    uint8_t  icmp_code;
    const uint8_t *icmp_hdr;
    size_t   icmp_len;

    /* ARP view (eth_type == ARP). */
    int      is_arp;
    uint16_t arp_oper;       /* 1=request, 2=reply */
    uint8_t  arp_sha[6];
    uint32_t arp_spa_be;
    uint8_t  arp_tha[6];
    uint32_t arp_tpa_be;
} PktView;

int pkt_parse(const uint8_t *buf, size_t len, PktView *out);

/* Build helpers -- return total bytes written. buf must be >= 1600.
 * vlan_id == 0xFFFF means "no VLAN tag". */
size_t build_ethernet(uint8_t *buf,
                      const uint8_t dst_mac[6], const uint8_t src_mac[6],
                      uint16_t vlan_id, uint16_t ethertype);

size_t build_udp4(uint8_t *buf, size_t buf_len,
                  const uint8_t dst_mac[6], const uint8_t src_mac[6],
                  uint16_t vlan_id,
                  uint32_t src_ip_be, uint32_t dst_ip_be,
                  uint16_t src_port, uint16_t dst_port,
                  const uint8_t *payload, size_t payload_len);

size_t build_arp_reply(uint8_t *buf,
                       const uint8_t target_mac[6], uint32_t target_ip_be,
                       const uint8_t sender_mac[6], uint32_t sender_ip_be,
                       uint16_t vlan_id);

size_t build_icmp_echo_reply(uint8_t *buf, size_t buf_len,
                             const uint8_t dst_mac[6], const uint8_t src_mac[6],
                             uint16_t vlan_id,
                             uint32_t src_ip_be, uint32_t dst_ip_be,
                             uint16_t ident, uint16_t seq,
                             const uint8_t *echo_data, size_t echo_len);

#endif
