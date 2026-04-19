#include "packet.h"
#include "util.h"
#include <string.h>

#define RD16(p) ((uint16_t)(((p)[0] << 8) | (p)[1]))
#define RD32(p) ((uint32_t)(((p)[0] << 24) | ((p)[1] << 16) | ((p)[2] << 8) | (p)[3]))

static void wr16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }

int pkt_parse(const uint8_t *buf, size_t len, PktView *o) {
    if (len < 14) return -1;
    memset(o, 0, sizeof(*o));
    o->raw = buf;
    o->raw_len = len;
    memcpy(o->dst_mac, buf, 6);
    memcpy(o->src_mac, buf + 6, 6);
    uint16_t et = RD16(buf + 12);
    size_t off = 14;
    if (et == ETH_P_VLAN) {
        if (len < 18) return -1;
        uint16_t tci = RD16(buf + 14);
        o->has_vlan = 1;
        o->vlan_id = tci & 0x0FFF;
        o->vlan_prio = (tci >> 13) & 0x7;
        et = RD16(buf + 16);
        off = 18;
    }
    o->eth_type = et;

    if (et == ETH_P_ARP) {
        if (len < off + 28) return -1;
        const uint8_t *a = buf + off;
        o->is_arp = 1;
        o->arp_oper = RD16(a + 6);
        memcpy(o->arp_sha, a + 8, 6);
        memcpy(&o->arp_spa_be, a + 14, 4);
        memcpy(o->arp_tha, a + 18, 6);
        memcpy(&o->arp_tpa_be, a + 24, 4);
        return 0;
    }

    if (et != ETH_P_IP) return 0;
    if (len < off + 20) return -1;
    const uint8_t *ip = buf + off;
    uint8_t ihl = (ip[0] & 0x0F) * 4;
    if (ihl < 20 || len < off + ihl) return -1;
    o->is_ipv4 = 1;
    o->ip_hdr = ip;
    o->ip_hdr_len = ihl;
    o->ip_proto = ip[9];
    memcpy(&o->src_ip_be, ip + 12, 4);
    memcpy(&o->dst_ip_be, ip + 16, 4);

    size_t ip_total = RD16(ip + 2);
    if (ip_total < ihl || len < off + ip_total) {
        /* trust captured length over header total to be tolerant */
        ip_total = len - off;
    }
    const uint8_t *l4 = ip + ihl;
    size_t l4_len = ip_total - ihl;

    if (o->ip_proto == IP_P_UDP) {
        if (l4_len < 8) return -1;
        o->is_udp = 1;
        o->src_port = RD16(l4);
        o->dst_port = RD16(l4 + 2);
        uint16_t udp_len = RD16(l4 + 4);
        if (udp_len < 8 || udp_len > l4_len) udp_len = (uint16_t)l4_len;
        o->udp_payload = l4 + 8;
        o->udp_payload_len = udp_len - 8;
    } else if (o->ip_proto == IP_P_ICMP) {
        if (l4_len < 8) return -1;
        o->is_icmp = 1;
        o->icmp_type = l4[0];
        o->icmp_code = l4[1];
        o->icmp_hdr = l4;
        o->icmp_len = l4_len;
    }
    return 0;
}

size_t build_ethernet(uint8_t *buf,
                      const uint8_t dst_mac[6], const uint8_t src_mac[6],
                      uint16_t vlan_id, uint16_t ethertype) {
    memcpy(buf, dst_mac, 6);
    memcpy(buf + 6, src_mac, 6);
    if (vlan_id == 0xFFFF) {
        wr16(buf + 12, ethertype);
        return 14;
    }
    wr16(buf + 12, ETH_P_VLAN);
    wr16(buf + 14, vlan_id & 0x0FFF);
    wr16(buf + 16, ethertype);
    return 18;
}

/* Build IPv4 header at buf, return bytes written (20). */
static size_t build_ipv4_hdr(uint8_t *buf, uint16_t total_len,
                             uint8_t proto, uint32_t src_be, uint32_t dst_be) {
    static uint16_t ip_id = 0;
    buf[0] = 0x45;                /* IPv4, IHL=5 */
    buf[1] = 0;                   /* TOS */
    wr16(buf + 2, total_len);
    wr16(buf + 4, ++ip_id);
    wr16(buf + 6, 0x4000);        /* DF */
    buf[8] = 64;                  /* TTL */
    buf[9] = proto;
    wr16(buf + 10, 0);            /* checksum placeholder */
    memcpy(buf + 12, &src_be, 4);
    memcpy(buf + 16, &dst_be, 4);
    uint16_t c = inet_csum(buf, 20);
    wr16(buf + 10, c);
    return 20;
}

size_t build_udp4(uint8_t *buf, size_t buf_len,
                  const uint8_t dst_mac[6], const uint8_t src_mac[6],
                  uint16_t vlan_id,
                  uint32_t src_ip_be, uint32_t dst_ip_be,
                  uint16_t src_port, uint16_t dst_port,
                  const uint8_t *payload, size_t payload_len) {
    size_t need = 18 + 20 + 8 + payload_len;
    if (need > buf_len) return 0;
    size_t off = build_ethernet(buf, dst_mac, src_mac, vlan_id, ETH_P_IP);
    uint16_t ip_total = (uint16_t)(20 + 8 + payload_len);
    off += build_ipv4_hdr(buf + off, ip_total, IP_P_UDP, src_ip_be, dst_ip_be);

    uint8_t *udp = buf + off;
    wr16(udp, src_port);
    wr16(udp + 2, dst_port);
    wr16(udp + 4, (uint16_t)(8 + payload_len));
    /* UDP checksum is optional on IPv4 (RFC 768). Setting 0 skips a
     * full scan over payload on every send, which matters a lot for
     * 1400+ byte TFTP DATA bursts. The IP header checksum is still
     * computed in build_ipv4_hdr. */
    wr16(udp + 6, 0);
    if (payload_len) memcpy(udp + 8, payload, payload_len);
    (void)src_ip_be; (void)dst_ip_be;
    return off + 8 + payload_len;
}

size_t build_arp_reply(uint8_t *buf,
                       const uint8_t target_mac[6], uint32_t target_ip_be,
                       const uint8_t sender_mac[6], uint32_t sender_ip_be,
                       uint16_t vlan_id) {
    size_t off = build_ethernet(buf, target_mac, sender_mac, vlan_id, ETH_P_ARP);
    uint8_t *a = buf + off;
    wr16(a,     1);               /* HW type: Ethernet */
    wr16(a + 2, ETH_P_IP);
    a[4] = 6; a[5] = 4;
    wr16(a + 6, 2);               /* opcode: reply */
    memcpy(a + 8, sender_mac, 6);
    memcpy(a + 14, &sender_ip_be, 4);
    memcpy(a + 18, target_mac, 6);
    memcpy(a + 24, &target_ip_be, 4);
    return off + 28;
}

size_t build_icmp_echo_reply(uint8_t *buf, size_t buf_len,
                             const uint8_t dst_mac[6], const uint8_t src_mac[6],
                             uint16_t vlan_id,
                             uint32_t src_ip_be, uint32_t dst_ip_be,
                             uint16_t ident, uint16_t seq,
                             const uint8_t *echo_data, size_t echo_len) {
    size_t need = 18 + 20 + 8 + echo_len;
    if (need > buf_len) return 0;
    size_t off = build_ethernet(buf, dst_mac, src_mac, vlan_id, ETH_P_IP);
    uint16_t ip_total = (uint16_t)(20 + 8 + echo_len);
    off += build_ipv4_hdr(buf + off, ip_total, IP_P_ICMP, src_ip_be, dst_ip_be);

    uint8_t *icmp = buf + off;
    icmp[0] = 0;                  /* type: echo reply */
    icmp[1] = 0;
    wr16(icmp + 2, 0);            /* checksum placeholder */
    wr16(icmp + 4, ident);
    wr16(icmp + 6, seq);
    if (echo_len) memcpy(icmp + 8, echo_data, echo_len);
    uint16_t c = inet_csum(icmp, 8 + echo_len);
    wr16(icmp + 2, c);
    return off + 8 + echo_len;
}
