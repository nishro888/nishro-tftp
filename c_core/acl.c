#include "acl.h"

static int ip_in_cidr(uint32_t ip_be, uint32_t net_be, uint8_t bits) {
    if (bits == 0) return 1;
    if (bits > 32) bits = 32;
    uint32_t ip  = __builtin_bswap32(ip_be);
    uint32_t net = __builtin_bswap32(net_be);
    uint32_t mask = (bits == 32) ? 0xFFFFFFFFu : ((0xFFFFFFFFu << (32 - bits)) & 0xFFFFFFFFu);
    return (ip & mask) == (net & mask);
}

int acl_allowed(const Config *cfg, uint16_t vlan, uint32_t ip_be, int svc) {
    for (size_t i = 0; i < cfg->acl_count; i++) {
        const AclRule *r = &cfg->acl[i];
        int svc_match;
        switch (svc) {
        case SVC_ARP:  svc_match = r->svc_arp;  break;
        case SVC_ICMP: svc_match = r->svc_icmp; break;
        case SVC_TFTP: svc_match = r->svc_tftp; break;
        default:       svc_match = 0;
        }
        if (!svc_match) continue;

        if (r->vlan != 0xFFFF && r->vlan != vlan) continue;
        if (r->mask_bits && !ip_in_cidr(ip_be, r->ip_be, r->mask_bits)) continue;

        return r->allow ? 1 : 0;
    }
    return cfg->acl_default_allow ? 1 : 0;
}
