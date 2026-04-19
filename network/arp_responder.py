"""ARP responder - answers 'who has virtual_ip?' requests."""
from __future__ import annotations

import logging

from core.acl import ACL
from core.stats import STATS

from .packet_utils import ParsedPacket, build_arp_reply
from .sniffer import L2Bridge

log = logging.getLogger("nishro.arp")


class ArpResponder:
    def __init__(self, bridge: L2Bridge, virtual_ip: str, virtual_mac: str, acl: ACL):
        self.bridge = bridge
        self.virtual_ip = virtual_ip
        self.virtual_mac = virtual_mac
        self.acl = acl

    def update(self, virtual_ip: str, virtual_mac: str) -> None:
        self.virtual_ip = virtual_ip
        self.virtual_mac = virtual_mac

    def handle(self, p: ParsedPacket) -> bool:
        """Return ``True`` if the packet was an ARP request for us."""
        if not p.is_arp or p.arp_op != 1:  # only requests
            return False
        if p.arp_tpa != self.virtual_ip:
            return False

        STATS.bump("arp_requests")
        denial = self.acl.check("arp", p.vlan_id, p.arp_spa)
        if denial:
            STATS.bump("acl_denied")
            STATS.bump("vlan_denied" if denial == "vlan" else "ip_denied")
            log.debug("ARP denied by %s ACL: spa=%s vlan=%s", denial, p.arp_spa, p.vlan_id)
            return True

        reply = build_arp_reply(p, self.virtual_mac, self.virtual_ip)
        self.bridge.send(reply)
        STATS.bump("arp_replies")
        log.debug("ARP reply -> %s (vlan=%s)", p.arp_spa, p.vlan_id)
        return True
