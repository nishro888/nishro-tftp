#ifndef NISHRO_ACL_H
#define NISHRO_ACL_H

#include "config.h"

#define SVC_ARP  0
#define SVC_ICMP 1
#define SVC_TFTP 2

/* Returns 1 if the (vlan, ip, service) tuple is allowed by the current
 * ACL, 0 if denied. VLAN == 0xFFFF means "no tag / untagged". */
int acl_allowed(const Config *cfg, uint16_t vlan, uint32_t ip_be, int svc);

#endif
