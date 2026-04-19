#include "packet.h"
#include "pcap_io.h"
#include "acl.h"
#include "util.h"
#include <string.h>

/* Handle ICMP Echo Request targeting our virtual IP. Returns 1 if
 * consumed. */
int icmp_handle(PcapIO *io, const Config *cfg, const PktView *p) {
    if (!p->is_icmp) return 0;
    if (p->icmp_type != 8) return 0;            /* echo request */
    if (p->dst_ip_be != cfg->virtual_ip_be) return 0;

    uint16_t v = p->has_vlan ? p->vlan_id : 0;
    if (!acl_allowed(cfg, v, p->src_ip_be, SVC_ICMP)) return 1;

    if (p->icmp_len < 8) return 1;
    uint16_t ident = (uint16_t)((p->icmp_hdr[4] << 8) | p->icmp_hdr[5]);
    uint16_t seq   = (uint16_t)((p->icmp_hdr[6] << 8) | p->icmp_hdr[7]);
    const uint8_t *echo_data = p->icmp_hdr + 8;
    size_t echo_len = p->icmp_len - 8;

    uint8_t buf[2048];
    size_t n = build_icmp_echo_reply(
        buf, sizeof buf,
        p->src_mac, cfg->virtual_mac,
        p->has_vlan ? p->vlan_id : 0xFFFF,
        cfg->virtual_ip_be, p->src_ip_be,
        ident, seq, echo_data, echo_len);
    if (n) pcapio_send(io, buf, n);
    return 1;
}
