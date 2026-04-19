#include "packet.h"
#include "pcap_io.h"
#include "acl.h"
#include "util.h"
#include <string.h>

/* Handle an ARP request for our virtual IP. Returns 1 if consumed. */
int arp_handle(PcapIO *io, const Config *cfg, const PktView *p) {
    if (!p->is_arp) return 0;
    if (p->arp_oper != 1) return 0;          /* only requests */
    if (p->arp_tpa_be != cfg->virtual_ip_be) return 0;

    uint16_t v = p->has_vlan ? p->vlan_id : 0;
    if (!acl_allowed(cfg, v, p->arp_spa_be, SVC_ARP)) return 1;

    uint8_t buf[128];
    size_t n = build_arp_reply(
        buf,
        p->arp_sha, p->arp_spa_be,               /* target */
        cfg->virtual_mac, cfg->virtual_ip_be,    /* sender */
        p->has_vlan ? p->vlan_id : 0xFFFF);
    if (n) pcapio_send(io, buf, n);
    return 1;
}
